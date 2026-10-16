"""Skill usage: ingest `~/.agents/skill-usage/log.jsonl`, match calls to sessions, score skills.

docs/skills.md has the full rules. In short:

- Log line: one JSON object, `{"t", "skill", "by", "agent", "cwd", "backfill"?, "session_id"?}`. `t` is ISO 8601
  with an offset (`...Z` or `+00:00`). `by` is claude | codex | me. Anything else is unparsed.
- `source_key = sha256(canonical JSON)` (sorted keys, no spaces). A byte-for-byte repeat of an
  earlier line is the same call logged twice: it is dropped and counted as a duplicate.
- Session match: a line's own `session_id` (logged by track.py since 2026-10-14; Codex ids get the
  `codex:` prefix) links the call directly when that sessions row exists. Otherwise: same tool
  (`sessions.tool = agent`), the call's cwd is the session's cwd or any cwd it visited
  (`meta.cwds`), and `ts` inside `started_at - 1 min .. last activity + 1 min`. When several
  match, the latest start wins. Unmatched calls keep `session_id` null and are retried on every
  ingest and refresh.
- Score of a (skill, bucket): mean outcome weight of the matched sessions, pulled toward a
  prior of 0.5 with weight 2: `(sum(w) + 0.5 * 2) / (n_scored + 2)`.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import routines_log, sessions_ingest, store

DEFAULT_LOG = Path("~/.agents/skill-usage/log.jsonl")
INVOKERS = ("claude", "codex", "me")

# Outcome weights (sessions.outcome). `active` and null are not final: not scored.
WEIGHTS: dict[str, float] = {
    "shipped": 1.0,
    "pushed": 0.9,
    "committed": 0.75,
    "partial": 0.4,
    "no-change": 0.25,
    "abandoned": 0.1,
    "failed": 0.0,
}
PRIOR = 0.5
PRIOR_WEIGHT = 2
ALL_BUCKET = "*"
CACHE_VERSION = 1


@dataclass
class SkillCall:
    ts: datetime
    skill: str
    invoked_by: str
    agent: str
    cwd: str | None
    project: str
    backfill: bool
    source_key: str
    line_no: int
    logged_session: str | None = None  # the session id track.py logged, as a sessions.session_id


@dataclass
class ParseResult:
    calls: list[SkillCall] = field(default_factory=list)
    unparsed: list[int] = field(default_factory=list)  # 1-based line numbers
    duplicates: int = 0  # lines identical to an earlier line: dropped
    incomplete_tail: bool = False  # last line had no newline and was not whole JSON


def canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def source_key(obj: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def project_for(cwd: str | None) -> str:
    """The project bucket of a cwd, by the sessions rule (`sessions_ingest.project_of`)."""
    if not cwd:
        return sessions_ingest.UNKNOWN_PROJECT
    return sessions_ingest.project_of(cwd, kind="chat")


def parse_line(text: str, line_no: int) -> SkillCall | None:
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    t, skill, by, agent = obj.get("t"), obj.get("skill"), obj.get("by"), obj.get("agent")
    if not (isinstance(t, str) and isinstance(skill, str) and skill.strip()
            and by in INVOKERS and isinstance(agent, str) and agent.strip()):
        return None
    ts = routines_log.parse_ts(t.replace("Z", "+00:00") if t.endswith("Z") else t)
    if ts is None:
        return None
    cwd = obj.get("cwd") if isinstance(obj.get("cwd"), str) and obj.get("cwd") else None
    sid = obj.get("session_id") if isinstance(obj.get("session_id"), str) and obj.get("session_id").strip() else None
    if sid and agent.strip() == "codex" and not sid.startswith("codex:"):
        sid = "codex:" + sid  # sessions stores Codex threads as codex:<id>
    return SkillCall(
        ts=ts, skill=skill.strip(), invoked_by=by, agent=agent.strip(), cwd=cwd,
        project=project_for(cwd), backfill=obj.get("backfill") is True,
        source_key=source_key(obj), line_no=line_no, logged_session=sid,
    )


def parse_lines(lines: list[str]) -> ParseResult:
    result = ParseResult()
    seen: set[str] = set()
    for line_no, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text:
            continue
        call = parse_line(text, line_no)
        if call is None:
            result.unparsed.append(line_no)
        elif call.source_key in seen:
            result.duplicates += 1
        else:
            seen.add(call.source_key)
            result.calls.append(call)
    return result


def parse_file(path: Path) -> ParseResult:
    """Parse the log. A last line without a newline counts only when it is whole JSON."""
    lines, has_tail = routines_log.read_lines(path)
    incomplete = False
    if has_tail:
        tail = path.read_text(encoding="utf-8", errors="replace").rsplit("\n", 1)[-1]
        try:
            json.loads(tail)
            lines.append(tail)
        except ValueError:
            incomplete = True
    result = parse_lines(lines)
    result.incomplete_tail = incomplete
    return result


# ------------------------------------------------------------------ store

_INSERT_SQL = """
insert into skill_calls (ts, skill, invoked_by, agent, cwd, project, backfill, source_key, logged_session)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (source_key) do nothing
returning id
"""

# Fill session_id for calls that have none yet. Rule: docs/skills.md, "Session match".
MATCH_SQL = """
with direct as (
    update skill_calls c set session_id = s.session_id
    from sessions s
    where c.session_id is null and c.logged_session is not null and s.session_id = c.logged_session
    returning c.id
),
m as (
    select c.id,
           (select s.session_id from sessions s
             where s.tool = c.agent
               and (s.meta ->> 'cwd' = c.cwd or coalesce(s.meta -> 'cwds', '[]'::jsonb) ? c.cwd)
               and s.started_at is not null
               and c.ts >= s.started_at - interval '1 minute'
               and c.ts <= greatest(s.started_at, s.ended_at, s.last_activity_at) + interval '1 minute'
             order by s.started_at desc, s.session_id
             limit 1) as sid
    from skill_calls c
    where c.session_id is null and c.cwd is not null
      and c.id not in (select id from direct)
),
fuzzy as (
    update skill_calls c set session_id = m.sid
    from m where m.id = c.id and m.sid is not null
    returning c.id
)
select (select count(*) from direct) + (select count(*) from fuzzy)
"""


def match_sessions(conn: Any) -> int:
    """Link unmatched calls to their session. Returns how many calls got a session now."""
    return int(conn.execute(MATCH_SQL).fetchone()[0])


@dataclass
class IngestReport:
    new: int = 0
    present: int = 0
    matched: int = 0


def ingest(calls: list[SkillCall], *, db_url: str | None = None) -> IngestReport:
    """Insert calls keyed by source_key (one transaction), then match sessions."""
    report = IngestReport()
    with store.connect(db_url) as conn:
        for c in calls:
            got = conn.execute(_INSERT_SQL, (c.ts, c.skill, c.invoked_by, c.agent, c.cwd, c.project,
                                             c.backfill, c.source_key, c.logged_session)).fetchone()
            if got is None:
                report.present += 1
            else:
                report.new += 1
        report.matched = match_sessions(conn)
    return report


# ------------------------------------------------------------------ scores


def score_of(outcomes: dict[str, int]) -> tuple[int, float]:
    """(n_scored, score) for {outcome: sessions}. Unknown and non-final outcomes are left out."""
    n = sum(v for k, v in outcomes.items() if k in WEIGHTS)
    total = sum(WEIGHTS[k] * v for k, v in outcomes.items() if k in WEIGHTS)
    return n, round((total + PRIOR * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT), 4)


def buckets_of(row: dict[str, Any]) -> list[str]:
    """Buckets one call counts in: all, its project (the session's when matched), its category."""
    project = row.get("session_project") or row.get("project") or sessions_ingest.UNKNOWN_PROJECT
    out = [ALL_BUCKET, f"project:{project}"]
    if row.get("session_id") and row.get("category"):
        out.append(f"category:{row['category']}")
    return out


def compute_scores(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score rows from call rows (`skill, project, session_id, session_project, category, outcome`)."""
    calls: Counter[tuple[str, str]] = Counter()
    sessions: dict[tuple[str, str], dict[str, str | None]] = {}
    for r in rows:
        for b in buckets_of(r):
            key = (r["skill"], b)
            calls[key] += 1
            if r.get("session_id"):
                sessions.setdefault(key, {})[r["session_id"]] = r.get("outcome")
    out = []
    for key in sorted(calls):
        by_sid = sessions.get(key, {})
        outcomes = dict(Counter(o for o in by_sid.values() if o))
        n_scored, score = score_of(outcomes)
        out.append({"skill": key[0], "bucket": key[1], "n_calls": calls[key], "n_sessions": len(by_sid),
                    "n_scored": n_scored, "outcomes": outcomes, "score": score})
    return out


_CALL_ROWS_SQL = """
select c.skill, c.project, c.session_id, s.project as session_project, s.category, s.outcome
from skill_calls c left join sessions s on s.session_id = c.session_id
where coalesce(s.env, 'prod') <> 'sandbox'
"""


def refresh_scores(*, db_url: str | None = None) -> tuple[int, int]:
    """Match sessions, then rewrite skill_scores in one transaction. Returns (matched, score rows)."""
    with store.connect(db_url) as conn:
        matched = match_sessions(conn)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_CALL_ROWS_SQL)
            rows = cur.fetchall()
        scores = compute_scores(rows)
        conn.execute("delete from skill_scores")
        with conn.cursor() as cur:
            cur.executemany(
                "insert into skill_scores (skill, bucket, n_calls, n_sessions, n_scored, outcomes, score)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                [(s["skill"], s["bucket"], s["n_calls"], s["n_sessions"], s["n_scored"],
                  Jsonb(s["outcomes"]), s["score"]) for s in scores],
            )
    return matched, len(scores)


def load_scores(*, db_url: str | None = None) -> list[dict[str, Any]]:
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from skill_scores order by skill, bucket")
        return cur.fetchall()


# ------------------------------------------------------------------ cache


def cache_payload(scores: list[dict[str, Any]], *, generated_at: datetime | None = None) -> dict[str, Any]:
    """The JSON cache (docs/skills.md, "Cache"): scores per skill and bucket."""
    generated_at = generated_at or datetime.now(timezone.utc)
    refreshed = [s["refreshed_at"] for s in scores if s.get("refreshed_at")]
    skills: dict[str, dict[str, Any]] = {}
    for s in scores:
        skills.setdefault(s["skill"], {})[s["bucket"]] = {
            "score": s["score"], "n_calls": s["n_calls"], "n_sessions": s["n_sessions"],
            "n_scored": s["n_scored"], "outcomes": dict(s["outcomes"] or {}),
        }
    return {
        "version": CACHE_VERSION,
        "generated_at": generated_at.isoformat(),
        "refreshed_at": max(refreshed).isoformat() if refreshed else None,
        "score_rule": {"weights": WEIGHTS, "prior": PRIOR, "prior_weight": PRIOR_WEIGHT},
        "skills": skills,
    }


def write_cache(path: Path, payload: dict[str, Any]) -> None:
    """Write atomically (temp file + rename), so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ suggest

_SUGGEST_SQL = """
select skill, count(*) as n
from skill_calls
where invoked_by = 'me' and ts >= now() - make_interval(days => %s)
group by skill
having count(*) >= %s
order by n desc, skill
"""


def suggest(days: int, minimum: int, *, db_url: str | None = None) -> list[tuple[str, int]]:
    """Skills Vilmer ran by hand at least `minimum` times in the last `days` days."""
    with store.connect(db_url) as conn:
        return [(r[0], int(r[1])) for r in conn.execute(_SUGGEST_SQL, (days, minimum)).fetchall()]
