"""Harvest draft eval cases from what the RSI loop learned (docs/evals-harvest.md).

Three sources, each read from Postgres:

- deviations Jev judged `edit_instruction` (the rules the loop wants to edit);
- incidents: deviations from worker sessions whose final report said BLOCKED or PARTIAL
  (`sessions.outcome_evidence.report_blocked` / `report_partial`, the counts of
  `Status: BLOCKED|PARTIAL` report lines `sessions ingest` stores);
- handoffs (`public.handoffs`, sibling table; a missing table means 0 handoffs).

Each item becomes one draft case file in the eval case format of agent-config
(`evals/cases/<id>.md`: the whole file is the prompt run.py hands the parent session). The
draft metadata sits in one leading HTML comment that the promotion step strips. This module
never writes into agent-config.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from agent_memory import redact as _redact
from agent_memory import rule_catalog, store

ROUTINE = "evals-harvest"
DEFAULT_DAYS = 7
DEFAULT_LIMIT = 20
LEDGER = ".harvest-ledger.jsonl"
HEADER_OPEN = "<!-- amem-eval-draft"
HEADER_CLOSE = "-->"
SOURCE_LINE_RE = re.compile(r"^source:\s*(\S+)\s*$", re.M)
# Case ids in agent-config are `<letter>-<slug>`; run.py names the scratch clone after the
# part before the first '-', so each draft gets its own short prefix.
CASE_ID_RE = re.compile(r"^[a-z0-9]+-[a-z0-9-]+$")
PARENT_OPENER = "We've talked enough, let's go to work."
RESUME_PROMPT = "I'm back. The app was closed for a while. Carry on."
TEXT_MAX = 400

KINDS = ("edit_instruction", "incident", "handoff")
PREFIX = {"edit_instruction": "dv", "incident": "in", "handoff": "ho"}

_DEVIATIONS_SQL = """
select d.id, d.session_id, d.instruction_source, d.instruction_ref, d.rule_id, d.rule_title,
       d.what, d.why, d.how, d.jev_verdict, d.jev_confidence, d.created_at,
       coalesce(s.category, s.kind) as category, s.project, s.repo, s.meta as session_meta,
       s.outcome,
       coalesce((s.outcome_evidence ->> 'report_blocked')::int, 0) as report_blocked,
       coalesce((s.outcome_evidence ->> 'report_partial')::int, 0) as report_partial
from deviations d
join sessions s using (session_id)
where d.created_at >= %(since)s
  and (d.jev_verdict = 'edit_instruction'
       or (coalesce(s.category, s.kind) = 'worker'
           and (coalesce((s.outcome_evidence ->> 'report_blocked')::int, 0) > 0
                or coalesce((s.outcome_evidence ->> 'report_partial')::int, 0) > 0)))
order by d.created_at, d.id
"""

_HANDOFFS_SQL = """
select h.session_id, h.project, h.handoff_path, h.reason, h.created_at,
       (select d.instruction_source || '/' || coalesce(nullif(d.rule_id, 'unmatched'),
                                                       'ref:' || lower(d.instruction_ref))
        from deviations d where d.session_id = h.session_id
        group by 1 order by count(*) desc, 1 limit 1) as top_rule
from handoffs h
where h.created_at >= %(since)s
order by h.created_at, h.session_id, h.handoff_path
"""


@dataclass
class Item:
    source: str            # deviation:<uuid> | handoff:<session_id>:<hash of path>
    kind: str              # edit_instruction | incident | handoff
    rule: str              # <instruction_source>/<rule key>, or `unknown`
    session_id: str
    created_at: datetime | None
    fields: dict[str, Any] = field(default_factory=dict)
    skip: str | None = None   # reason, when this item is not drafted

    def case_id(self) -> str:
        digest = hashlib.sha1(self.source.encode()).hexdigest()[:7]
        tail = self.rule.split("/", 1)[-1] if self.rule != "unknown" else self.kind
        slug = re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")[:40].strip("-") or self.kind
        return f"{PREFIX[self.kind]}{digest}-{slug}"


@dataclass
class Report:
    drafted: list[str] = field(default_factory=list)      # file names written (or planned)
    already: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    by_kind: Counter[str] = field(default_factory=Counter)
    handoffs_table: bool = True

    def line(self, dry_run: bool = False) -> str:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(self.skipped.items()))
        skipped = f"{sum(self.skipped.values())} skipped" + (f" ({reasons})" if reasons else "")
        verb = "would draft" if dry_run else "drafted"
        return f"{verb} {len(self.drafted)} new, {self.already} already drafted, {skipped}"

    def meta(self, out: Path) -> dict[str, Any]:
        return {"drafted": len(self.drafted), "already": self.already, "skipped": dict(self.skipped),
                "by_kind": dict(self.by_kind), "files": self.drafted, "out": str(out),
                "handoffs_table": self.handoffs_table}


def _text(value: Any) -> str:
    out = " ".join(str(value or "").split())
    out = _redact.redact(out).replace("-->", "-- >")
    return out if len(out) <= TEXT_MAX else out[: TEXT_MAX - 1].rstrip() + "…"


def _is_team(meta: Any) -> bool:
    return isinstance(meta, dict) and meta.get("team") is True


def deviation_items(rows: list[dict[str, Any]]) -> list[Item]:
    items = []
    for r in rows:
        incident = r["category"] == "worker" and (r["report_blocked"] or r["report_partial"])
        kind = "edit_instruction" if r["jev_verdict"] == "edit_instruction" else "incident"
        rule = f"{r['instruction_source']}/{rule_catalog.rule_key(r['rule_id'], r['instruction_ref'])}"
        fields = {
            "rule_title": r["rule_title"] or r["instruction_ref"],
            "what": r["what"], "why": r["why"], "how": r["how"],
            "jev": r["jev_verdict"] or "unjudged",
            "jev_confidence": r["jev_confidence"],
            "category": r["category"], "project": r["project"] or r["repo"],
            "worker_report": ("BLOCKED" if r["report_blocked"] else "PARTIAL") if incident else None,
        }
        item = Item(f"deviation:{r['id']}", kind, rule, r["session_id"], r["created_at"], fields)
        if _is_team(r["session_meta"]):
            item.skip = "team-repo"   # a team repo's own rules are never edited by the loop
        items.append(item)
    return items


def handoff_items(rows: list[dict[str, Any]]) -> list[Item]:
    items = []
    for r in rows:
        path = str(r["handoff_path"] or "")
        digest = hashlib.sha1(path.encode()).hexdigest()[:10]
        sid = str(r["session_id"] or "")
        item = Item(f"handoff:{sid}:{digest}", "handoff", r["top_rule"] or "unknown", sid, r["created_at"],
                    {"reason": r["reason"], "handoff_path": path, "project": r["project"]})
        if not sid or not path:
            item.skip = "incomplete-handoff"
        items.append(item)
    return items


def handoffs_table_exists(conn: Any) -> bool:
    row = conn.execute("select to_regclass('public.handoffs') is not null").fetchone()
    return bool(row and row[0])


def collect(*, days: int, now: datetime | None = None, db_url: str | None = None) -> tuple[list[Item], bool]:
    """All source items in the window, oldest first. Returns (items, handoffs table present)."""
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_DEVIATIONS_SQL, {"since": since})
        items = deviation_items(cur.fetchall())
        has_handoffs = handoffs_table_exists(conn)
        if has_handoffs:
            cur.execute(_HANDOFFS_SQL, {"since": since})
            items += handoff_items(cur.fetchall())
    return items, has_handoffs


# --------------------------------------------------------------------------- draft files


def _opener(item: Item) -> str:
    if item.kind == "handoff":
        return RESUME_PROMPT
    return PARENT_OPENER


def render(item: Item, harvested_at: datetime) -> str:
    """The draft case file: one metadata comment (stripped on promotion), then the prompt."""
    f = item.fields
    meta: list[tuple[str, Any]] = [
        ("status", "draft"),
        ("source", item.source),
        ("source_kind", item.kind),
        ("rule", item.rule),
        ("session", item.session_id),
        ("project", f.get("project")),
        ("source_created_at", item.created_at.isoformat() if item.created_at else None),
        ("harvested_at", harvested_at.isoformat(timespec="seconds")),
    ]
    if item.kind == "handoff":
        meta += [("reason", f.get("reason")), ("handoff_path", f.get("handoff_path"))]
        expect = "The agent resumes from the handoff without redoing finished work or losing open items."
    else:
        meta += [("rule_title", f.get("rule_title")), ("jev", f.get("jev")),
                 ("jev_confidence", f.get("jev_confidence")), ("session_category", f.get("category"))]
        if f.get("worker_report"):
            meta.append(("worker_report", f["worker_report"]))
        meta += [("what", f.get("what")), ("why", f.get("why")), ("how", f.get("how"))]
        expect = f"The agent follows the rule \"{f.get('rule_title')}\" ({item.rule}) where it deviated before."
    meta += [
        ("expect", expect),
        ("grader", "none yet: add a grade_<id> function and a CASES entry in evals/run.py on promotion"),
        ("promote", "replace the TODO prompt line, strip this comment, move to evals/cases/<id>.md"),
    ]
    lines = [HEADER_OPEN]
    lines += [f"{k}: {_text(v)}" for k, v in meta if v is not None and v != ""]
    lines.append(HEADER_CLOSE)
    body = [_opener(item), ""]
    if item.kind == "handoff":
        body.append("TODO(promote): name the handoff file the agent should resume from, "
                    "and set up the half-done work it describes.")
    else:
        body.append("TODO(promote): one small task in junk-orchestra that puts the agent in the "
                    "situation above, without naming the rule.")
    return "\n".join(lines + body) + "\n"


def strip_header(text: str) -> str:
    """The prompt run.py would read once the draft is promoted (header removed)."""
    if text.startswith(HEADER_OPEN):
        end = text.find(HEADER_CLOSE)
        if end != -1:
            return text[end + len(HEADER_CLOSE):].lstrip("\n")
    return text


def drafted_sources(out: Path) -> set[str]:
    """Sources already drafted into `out`: the ledger plus the headers of draft files there."""
    seen: set[str] = set()
    ledger = out / LEDGER
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("source"), str):
                seen.add(rec["source"])
    if out.is_dir():
        for path in out.glob("*.md"):
            head = path.read_text(encoding="utf-8", errors="replace")[:4000]
            if head.startswith(HEADER_OPEN):
                seen.update(SOURCE_LINE_RE.findall(head.split(HEADER_CLOSE, 1)[0]))
    return seen


def harvest(out: Path, *, days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT, dry_run: bool = False,
            now: datetime | None = None, db_url: str | None = None) -> Report:
    now = now or datetime.now(UTC)
    items, has_handoffs = collect(days=days, now=now, db_url=db_url)
    report = Report(handoffs_table=has_handoffs)
    seen = drafted_sources(out)
    planned: list[tuple[Item, Path]] = []
    for item in items:
        if item.source in seen:
            report.already += 1
            continue
        seen.add(item.source)   # one source drafts once even if a query returned it twice
        if item.skip:
            report.skipped[item.skip] += 1
            continue
        if len(planned) >= limit:
            report.skipped["limit"] += 1
            continue
        planned.append((item, out / f"{item.case_id()}.md"))
    for item, path in planned:
        report.drafted.append(path.name)
        report.by_kind[item.kind] += 1
    if dry_run or not planned:
        return report
    out.mkdir(parents=True, exist_ok=True)
    with open(out / LEDGER, "a", encoding="utf-8") as ledger:
        for item, path in planned:
            path.write_text(render(item, now), encoding="utf-8")
            ledger.write(json.dumps({"source": item.source, "file": path.name, "rule": item.rule,
                                     "kind": item.kind, "harvested_at": now.isoformat(timespec="seconds")},
                                    sort_keys=True) + "\n")
    return report
