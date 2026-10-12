"""Lessons: the lessons.md format, the `lessons` table, retrieval, Jev gates and scoring.

lessons.md format (what `amem lessons import` reads and `amem lessons export` writes):

    ### <title>
    Id: <slug>
    Evals: <text>
    Promoted-to: <text>
    Evidence: <text, may continue over more lines and blank-line paragraphs>
    Status: trial | kept | retired | promoted     (`reverted` is read as `retired`)
    Since: YYYY-MM-DD

- Text before the first `### ` heading is a preamble and is ignored.
- A key starts a field. Every following line that is not a key continues that field.
- Optional keys the export adds when the table has the data: `Rule:` (when the rule
  differs from the title), `Repo:`, `Task:` (scope) and `Source-runs:` (run ids).
- A `# ` or `## ` heading ends the current lesson.
- A missing `Id:` becomes a slug of the title. A missing `Status:` becomes `trial`.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_memory import jev, store

STATUSES = ("trial", "kept", "retired", "promoted")
STATUS_ALIASES = {"reverted": "retired"}
ACTIVE_STATUSES = ("trial", "kept", "promoted")

# Export order. Import accepts any order.
_KEYS = ("Id", "Rule", "Repo", "Task", "Evals", "Promoted-to", "Evidence", "Source-runs", "Status", "Since")
_KEY_RE = re.compile(r"^(" + "|".join(re.escape(k) for k in _KEYS) + r"):(?: (.*)|)$")
_TITLE_RE = re.compile(r"^### +(.+?)\s*$")
_SECTION_RE = re.compile(r"^#{1,2} ")


class LessonsError(RuntimeError):
    """A lessons.md file or a lessons command input is invalid."""


@dataclass
class Lesson:
    slug: str
    title: str
    rule: str
    status: str = "trial"
    scope_repo: str | None = None
    scope_task: str | None = None
    source_run_ids: list[uuid.UUID] = field(default_factory=list)
    evidence: str | None = None
    evals: str | None = None
    promoted_to: str | None = None
    since: date | None = None

    def row(self) -> tuple[Any, ...]:
        return (self.slug, self.title, self.rule, self.scope_repo, self.scope_task, self.status,
                list(self.source_run_ids), self.evidence, self.evals, self.promoted_to, self.since)


# --- lessons.md parse / render ---------------------------------------------------------


def slugify(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "lesson"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    lines = [line.rstrip() for line in value.split("\n")]
    text = "\n".join(lines).strip("\n").strip()
    return text or None


def _status(raw: str | None, title: str) -> str:
    if raw is None:
        return "trial"
    word = raw.strip().lower()
    word = STATUS_ALIASES.get(word, word)
    if word not in STATUSES:
        raise LessonsError(f"lesson {title!r}: unknown Status {raw!r} (expected one of {', '.join(STATUSES)})")
    return word


def _since(raw: str | None, title: str) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise LessonsError(f"lesson {title!r}: Since is not a YYYY-MM-DD date: {raw!r}") from None


def _run_ids(raw: str | None, title: str) -> list[uuid.UUID]:
    if raw is None:
        return []
    try:
        return [uuid.UUID(part.strip()) for part in re.split(r"[,\s]+", raw) if part.strip()]
    except ValueError:
        raise LessonsError(f"lesson {title!r}: Source-runs holds a value that is not a uuid") from None


def _build(title: str, fields: dict[str, str], preface: list[str]) -> Lesson:
    f = {k: _clean(v) for k, v in fields.items()}
    rule = f.get("Rule") or _clean("\n".join(preface)) or title
    return Lesson(
        slug=(f.get("Id") or slugify(title)),
        title=title,
        rule=rule,
        status=_status(f.get("Status"), title),
        scope_repo=f.get("Repo"),
        scope_task=f.get("Task"),
        source_run_ids=_run_ids(f.get("Source-runs"), title),
        evidence=f.get("Evidence"),
        evals=f.get("Evals"),
        promoted_to=f.get("Promoted-to"),
        since=_since(f.get("Since"), title),
    )


def parse_markdown(text: str) -> list[Lesson]:
    """Parse lessons.md text. Raises LessonsError on bad Status/Since or a repeated Id."""
    lessons: list[Lesson] = []
    title: str | None = None
    fields: dict[str, str] = {}
    preface: list[str] = []
    current: str | None = None

    def flush() -> None:
        if title is not None:
            lessons.append(_build(title, fields, preface))

    for raw in text.splitlines():
        line = raw.rstrip("\r")
        heading = _TITLE_RE.match(line)
        if heading:
            flush()
            title, fields, preface, current = heading.group(1), {}, [], None
            continue
        if _SECTION_RE.match(line):
            flush()
            title, fields, preface, current = None, {}, [], None
            continue
        if title is None:
            continue  # preamble
        key = _KEY_RE.match(line)
        if key:
            current = key.group(1)
            if current in fields:
                raise LessonsError(f"lesson {title!r}: key {current}: appears twice")
            fields[current] = key.group(2) or ""
        elif current is None:
            preface.append(line)
        else:
            fields[current] += "\n" + line
    flush()

    seen: set[str] = set()
    for lesson in lessons:
        if lesson.slug in seen:
            raise LessonsError(f"Id {lesson.slug!r} appears more than once")
        seen.add(lesson.slug)
    return lessons


EXPORT_HEADER = """# Lessons

Generated by `amem lessons export` from the agent-memory `lessons` table.
Each lesson: `Id:`, `Evals:`, `Evidence:`, `Status: trial | kept | retired | promoted`, `Since:`.
"""


def render_markdown(lessons: Iterable[Lesson]) -> str:
    parts = [EXPORT_HEADER]
    for l in lessons:
        lines = [f"### {l.title}", f"Id: {l.slug}"]
        if l.rule != l.title:
            lines.append(f"Rule: {l.rule}")
        if l.scope_repo:
            lines.append(f"Repo: {l.scope_repo}")
        if l.scope_task:
            lines.append(f"Task: {l.scope_task}")
        if l.evals:
            lines.append(f"Evals: {l.evals}")
        if l.promoted_to:
            lines.append(f"Promoted-to: {l.promoted_to}")
        if l.evidence:
            lines.append(f"Evidence: {l.evidence}")
        if l.source_run_ids:
            lines.append("Source-runs: " + ", ".join(str(r) for r in l.source_run_ids))
        lines.append(f"Status: {l.status}")
        if l.since:
            lines.append(f"Since: {l.since.isoformat()}")
        parts.append("\n".join(lines) + "\n")
    return "\n".join(parts)


# --- table access --------------------------------------------------------------------------

_COLUMNS = ("slug, title, rule, scope_repo, scope_task, status, source_run_ids, "
            "evidence, evals, promoted_to, since")

_UPSERT_SQL = f"""
insert into lessons ({_COLUMNS}, created_at, updated_at)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp(), clock_timestamp())
on conflict (slug) do update set
    title = excluded.title, rule = excluded.rule,
    scope_repo = excluded.scope_repo, scope_task = excluded.scope_task,
    status = excluded.status, source_run_ids = excluded.source_run_ids,
    evidence = excluded.evidence, evals = excluded.evals,
    promoted_to = excluded.promoted_to, since = excluded.since,
    updated_at = now()
where (lessons.title, lessons.rule, lessons.scope_repo, lessons.scope_task, lessons.status,
       lessons.source_run_ids, lessons.evidence, lessons.evals, lessons.promoted_to, lessons.since)
      is distinct from
      (excluded.title, excluded.rule, excluded.scope_repo, excluded.scope_task, excluded.status,
       excluded.source_run_ids, excluded.evidence, excluded.evals, excluded.promoted_to, excluded.since)
returning (xmax = 0) as inserted
"""


def upsert(lessons: Iterable[Lesson], *, db_url: str | None = None) -> tuple[int, int, int]:
    """Upsert by slug in one transaction. Returns (new, updated, unchanged)."""
    new = updated = unchanged = 0
    with store.connect(db_url) as conn:
        for lesson in lessons:
            got = conn.execute(_UPSERT_SQL, lesson.row()).fetchone()
            if got is None:
                unchanged += 1
            elif got[0]:
                new += 1
            else:
                updated += 1
    return new, updated, unchanged


def _from_row(row: dict[str, Any]) -> Lesson:
    return Lesson(
        slug=row["slug"], title=row["title"], rule=row["rule"], status=row["status"],
        scope_repo=row["scope_repo"], scope_task=row["scope_task"],
        source_run_ids=list(row["source_run_ids"] or []), evidence=row["evidence"],
        evals=row["evals"], promoted_to=row["promoted_to"], since=row["since"],
    )


def all_lessons(*, db_url: str | None = None) -> list[Lesson]:
    """Every lesson, in insertion order (an import keeps the file order)."""
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"select {_COLUMNS} from lessons order by created_at, slug")
        return [_from_row(r) for r in cur.fetchall()]


def get_lesson(slug: str, *, db_url: str | None = None) -> Lesson | None:
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"select {_COLUMNS} from lessons where slug = %s", (slug,))
        row = cur.fetchone()
    return _from_row(row) if row else None


# --- retrieval: `amem lessons for` ---------------------------------------------------------

_STATUS_ORDER = "case l.status when 'promoted' then 0 when 'kept' then 1 else 2 end"

_FOR_SQL = f"""
select l.id, l.slug, l.title, l.rule, l.status, l.scope_repo, l.scope_task,
       ts_rank(l.tsv, %(q)s::tsquery) as rank
from lessons l
where l.tsv @@ %(q)s::tsquery
  and l.status in ('trial', 'kept', 'promoted')
  and (l.scope_repo is null or l.scope_repo = %(repo)s)
  and (l.scope_task is null or position(lower(l.scope_task) in lower(%(task)s)) > 0)
order by rank desc, (l.scope_repo is not null) desc, {_STATUS_ORDER}, l.slug
limit %(limit)s
"""


def _relaxed_query(conn: psycopg.Connection, task: str) -> str | None:
    """websearch_to_tsquery(task), with AND relaxed to OR so any matching term retrieves.

    A query with a negation (`-word`) stays strict, so the exclusion keeps its meaning.
    Returns None when the task has no searchable terms (only stop words, say).
    """
    row = conn.execute("select websearch_to_tsquery('english', %s)::text", (task,)).fetchone()
    text = row[0] if row else ""
    if not text:
        return None
    if "!" in text:
        return text
    return text.replace(" & ", " | ")


def memory_enabled() -> bool:
    """`AMEM_MEMORY=off` is the A/B switch: retrieval returns nothing and records nothing."""
    return os.environ.get("AMEM_MEMORY", "").strip().lower() not in ("off", "0", "false", "no")


DEFAULT_MIN_RELATIVE_RANK = 0.2


def lessons_for(repo: str, task: str, *, limit: int = 5, run_id: uuid.UUID | None = None,
                min_relative_rank: float = DEFAULT_MIN_RELATIVE_RANK,
                db_url: str | None = None) -> list[dict[str, Any]]:
    """Rank in-scope active lessons for a task and record one lesson_uses row per hit.

    Hits ranked below `min_relative_rank` x the top rank are dropped: with OR matching,
    a lesson that shares one common word with the task is noise in a brief.
    """
    with store.connect(db_url) as conn:
        q = _relaxed_query(conn, task)
        if q is None:
            return []
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_FOR_SQL, {"q": q, "repo": repo, "task": task, "limit": limit})
            hits = cur.fetchall()
        if hits:
            floor = float(hits[0]["rank"]) * min_relative_rank
            hits = [h for h in hits if float(h["rank"]) >= floor]
        for hit in hits:
            conn.execute(
                "insert into lesson_uses (lesson_id, run_id, context) values (%s, %s, %s)",
                (hit["id"], run_id, task),
            )
    return hits


def render_brief_block(hits: list[dict[str, Any]]) -> str:
    """Markdown ready to paste into a worker brief."""
    lines = ["## Lessons from past runs", "", "Follow these unless the task says otherwise:", ""]
    for h in hits:
        text = f"**{h['title']}**"
        if h["rule"] and h["rule"] != h["title"]:
            text += f": {h['rule']}"
        lines.append(f"- {text} (`{h['slug']}`, {h['status']})")
    return "\n".join(lines) + "\n"


# --- Jev write gate: `amem lesson propose` ---------------------------------------------

PROPOSE_QUESTIONS = [
    jev.noul("reusable", "Is there a reusable lesson in this run?"),
    jev.score("generalizable", "How generalizable (0-1)?",
              ["Specific to this one run", "Applies to many future runs"]),
]
FRICTION_QUESTIONS = [jev.noul("recurring", "Is this a recurring friction, not a one-off?")]


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise LessonsError(f"{name} must be a number, got {raw!r}") from None


def get_run(run_id: uuid.UUID, *, db_url: str | None = None) -> dict[str, Any] | None:
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from runs where id = %s", (run_id,))
        return cur.fetchone()


def _failures(run: dict[str, Any]) -> list[str]:
    raw = (run.get("meta") or {}).get("failures") or []
    return [str(x) for x in raw if str(x).strip()]


def propose_state(run: dict[str, Any], repo: str | None, task: str | None) -> str:
    """Deterministic Jev state for a run. No ids or timestamps, so fixtures stay stable."""
    lines = [
        f"Routine run: {run['routine']}",
        f"Status: {run['status']}",
        f"Repo: {repo or '-'}",
        f"Task: {task or '-'}",
        f"Summary: {run.get('summary') or '-'}",
        f"Failure reason: {run.get('failure_reason') or '-'}",
        "Failures:",
    ]
    fails = _failures(run)
    lines.extend(f"- {f}" for f in fails or ["none"])
    return "\n".join(lines)


def _short(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def template_lesson(run: dict[str, Any], repo: str | None, task: str | None,
                    results: dict[str, jev.JevResult]) -> Lesson:
    """V1: the lesson text is a template over the run, not an LLM call."""
    routine = run["routine"]
    fails = _failures(run)
    point = _short(run.get("failure_reason") or (fails[0] if fails else None)
                   or run.get("summary") or f"{run['status']} run")
    where = task or (f"work in {repo}" if repo else f"a {routine} run")
    if run["status"] == "pass":
        title = f"{routine}: keep doing what worked: {point}"
        rule = f"In {where}, repeat what worked in a past {routine} run: {point}."
    else:
        title = f"{routine}: do not repeat: {point}"
        rule = f"Before you finish {where}, check that this does not happen again: {point}. A past {routine} run hit it."
    started = run.get("started_at")
    day = started.date() if started is not None else date.today()
    reusable, general = results["reusable"], results["generalizable"]
    evidence_parts = [f"Proposed from run {run['id']} ({routine}, {run['status']}, {day.isoformat()})."]
    if run.get("summary"):
        evidence_parts.append(f"Summary: {_short(run['summary'], 300)}.")
    if fails:
        evidence_parts.append("Failures: " + "; ".join(_short(f, 200) for f in fails[:5]) + ".")
    evidence_parts.append(
        f"Jev gate: reusable={float(reusable.value):.2f}, generalizable={float(general.value):.2f}"
        f" (confidence {general.confidence or 0:.2f})."
    )
    return Lesson(
        slug=f"{slugify(routine, 40)}-{str(run['id'])[:8]}",
        title=title,
        rule=rule,
        status="trial",
        scope_repo=repo,
        scope_task=task,
        source_run_ids=[run["id"]],
        evidence=" ".join(evidence_parts),
        since=day,
    )


def insert_new(lesson: Lesson, *, db_url: str | None = None) -> bool:
    """Insert a proposed lesson. False when that slug already exists (left untouched)."""
    with store.connect(db_url) as conn:
        got = conn.execute(
            f"insert into lessons ({_COLUMNS}) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "on conflict (slug) do nothing returning id",
            lesson.row(),
        ).fetchone()
    return got is not None


def friction_state(text: str, seen: int | None) -> str:
    state = f"Friction report: {text.strip()}"
    if seen is not None:
        state += f"\nSeen {seen} time(s) so far."
    return state


# --- scoring: `amem lessons score` --------------------------------------------------------

_USES_SQL = """
select lu.run_id,
       coalesce(nullif(lu.outcome, 'unknown'),
                case r.status when 'pass' then 'pass' when 'fail' then 'fail' when 'error' then 'fail' end)
           as outcome
from lesson_uses lu left join runs r on r.id = lu.run_id
where lu.lesson_id = %s
order by lu.injected_at
"""

_BASELINE_SQL = """
select count(*) filter (where r.status = 'pass') as passed, count(*) as total
from runs r
where r.status <> 'skip'
  and (%(repo)s::text is null or coalesce(r.meta ->> 'repo', r.routine) = %(repo)s)
  and not exists (select 1 from lesson_uses lu where lu.lesson_id = %(id)s and lu.run_id = r.id)
"""


def _rate(passed: int, total: int) -> float | None:
    return round(passed / total, 4) if total else None


def score(min_uses: int, *, apply: bool = False, repo: str | None = None,
          db_url: str | None = None) -> list[dict[str, Any]]:
    """Pass rate with the lesson vs the same-scope baseline without it, per active lesson.

    A use counts when its outcome is known: `lesson_uses.outcome` if pass/fail, else the
    linked run's status (pass -> pass, fail/error -> fail). One run counts once.
    Baseline: non-skip runs in the lesson's repo scope (`runs.meta.repo`, else the
    routine name; every run for a null scope) that never had this lesson injected.
    Decision: `insufficient` (uses < min_uses), `no_baseline`, `keep` (beats baseline),
    else `retire` (trial/kept) or `review` (promoted lessons are never auto-retired).
    """
    out: list[dict[str, Any]] = []
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        where = "status in ('trial', 'kept', 'promoted')"
        params: tuple[Any, ...] = ()
        if repo is not None:
            where += " and scope_repo = %s"
            params = (repo,)
        cur.execute(f"select id, slug, status, scope_repo from lessons where {where} "
                    "order by created_at, slug", params)
        for lesson in cur.fetchall():
            cur.execute(_USES_SQL, (lesson["id"],))
            seen_runs: set[uuid.UUID] = set()
            used = used_pass = 0
            for use in cur.fetchall():
                if use["outcome"] not in ("pass", "fail"):
                    continue
                if use["run_id"] is not None:
                    if use["run_id"] in seen_runs:
                        continue
                    seen_runs.add(use["run_id"])
                used += 1
                used_pass += use["outcome"] == "pass"
            cur.execute(_BASELINE_SQL, {"repo": lesson["scope_repo"], "id": lesson["id"]})
            base = cur.fetchone() or {"passed": 0, "total": 0}
            with_rate, base_rate = _rate(used_pass, used), _rate(base["passed"], base["total"])
            if used < min_uses:
                decision = "insufficient"
            elif base_rate is None:
                decision = "no_baseline"
            elif with_rate is not None and with_rate > base_rate:
                decision = "keep"
            else:
                decision = "review" if lesson["status"] == "promoted" else "retire"
            applied = False
            if apply and decision == "retire":
                conn.execute("update lessons set status = 'retired', updated_at = now() where id = %s",
                             (lesson["id"],))
                applied = True
            out.append({
                "slug": lesson["slug"], "status": "retired" if applied else lesson["status"],
                "scope_repo": lesson["scope_repo"], "uses": used, "uses_pass": used_pass,
                "with_pass_rate": with_rate, "baseline_runs": base["total"],
                "baseline_pass": base["passed"], "baseline_pass_rate": base_rate,
                "decision": decision, "applied": applied,
            })
    return out
