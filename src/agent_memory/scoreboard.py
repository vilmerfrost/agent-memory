"""`amem edits scoreboard`: is the loop working? (docs/edits.md, roadmap v2 E5).

Per applied edit (status kept, trial or reverted) made in the last N days:

- before: deviations on the same instruction source + rule id in sessions that started in
  the N days before the edit, per scanned session that had the source in force.
- after: the same from the edit until now (or until it was reverted), counting only
  deviations not found on the old text (instruction_sha differs from instruction_sha_before).
- verdict: `too early` until both windows hold MIN_SESSIONS scanned sessions; else `fewer`
  (after < 75% of before), `more` (after > 125% of before and at least 0.05 more per
  session), or `same`.

Plain text by default (readable in a Claude chat), `--json` for the numbers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row

from agent_memory import store

DEFAULT_DAYS = 28
MIN_SESSIONS = 5
FEWER_RATIO = 0.75
MORE_RATIO = 1.25
MORE_MIN_DELTA = 0.05
APPLIED = ("kept", "trial", "reverted")

_EDITS_SQL = """
select id, status, instruction_source, rule_id, rule_title, target_path, instruction_sha_before, improve_id,
       created_at, reverted_at
from edits where status = any(%s) and created_at >= %s
order by created_at desc, id
"""

_DEV_SQL = """
select count(*) from deviations d join sessions s using (session_id)
where d.instruction_source = %(src)s and d.rule_id = %(rule)s
  and coalesce(s.started_at, s.created_at) >= %(lo)s and coalesce(s.started_at, s.created_at) < %(hi)s
  and (not %(after)s or d.instruction_sha is null or d.instruction_sha is distinct from %(sha)s)
"""

_SESSIONS_SQL = """
select count(*) from sessions s
where s.scanned_at is not null and s.kind <> 'eval' and s.instructions ? %(src)s
  and coalesce(s.started_at, s.created_at) >= %(lo)s and coalesce(s.started_at, s.created_at) < %(hi)s
"""


@dataclass
class Score:
    edit_id: str
    instruction_source: str
    rule_id: str
    rule_title: str | None
    status: str
    target_path: str | None
    improve_id: str | None
    edited_at: str
    before_deviations: int
    before_sessions: int
    after_deviations: int
    after_sessions: int
    before_rate: float | None
    after_rate: float | None
    verdict: str

    def line(self) -> str:
        def rate(r: float | None, n: int, s: int) -> str:
            return f"{'-' if r is None else f'{r:.2f}'} per session ({n} in {s})"
        return (f"- {self.instruction_source} / {self.rule_title or self.rule_id} [{self.rule_id}] "
                f"({self.status}, {self.edited_at[:10]}, {self.target_path or '-'}): "
                f"before {rate(self.before_rate, self.before_deviations, self.before_sessions)} -> "
                f"after {rate(self.after_rate, self.after_deviations, self.after_sessions)}: {self.verdict}")


def _rate(dev: int, sessions: int) -> float | None:
    return round(dev / sessions, 3) if sessions else None


def verdict(before_dev: int, before_sessions: int, after_dev: int, after_sessions: int) -> str:
    if before_sessions < MIN_SESSIONS or after_sessions < MIN_SESSIONS:
        return "too early"
    b, a = before_dev / before_sessions, after_dev / after_sessions
    if a < b * FEWER_RATIO:
        return "fewer"
    if a > b * MORE_RATIO and a - b >= MORE_MIN_DELTA:
        return "more"
    return "same"


def scores(*, days: int = DEFAULT_DAYS, now: datetime | None = None, db_url: str | None = None) -> list[Score]:
    now = now or datetime.now(UTC)
    out: list[Score] = []
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_EDITS_SQL, (list(APPLIED), now - timedelta(days=days)))
        for e in cur.fetchall():
            at = e["created_at"]
            end = e["reverted_at"] or now
            base = {"src": e["instruction_source"], "rule": e["rule_id"], "sha": e["instruction_sha_before"]}
            before = {**base, "lo": at - timedelta(days=days), "hi": at, "after": False}
            after = {**base, "lo": at, "hi": end, "after": True}
            bd = conn.execute(_DEV_SQL, before).fetchone()[0]
            ad = conn.execute(_DEV_SQL, after).fetchone()[0]
            bs = conn.execute(_SESSIONS_SQL, before).fetchone()[0]
            as_ = conn.execute(_SESSIONS_SQL, after).fetchone()[0]
            out.append(Score(str(e["id"]), e["instruction_source"], e["rule_id"], e["rule_title"], e["status"],
                             e["target_path"], e["improve_id"], at.isoformat(timespec="seconds"),
                             bd, bs, ad, as_, _rate(bd, bs), _rate(ad, as_), verdict(bd, bs, ad, as_)))
    return out


def text_lines(items: list[Score], days: int) -> list[str]:
    counts = {v: sum(s.verdict == v for s in items) for v in ("fewer", "same", "more", "too early")}
    head = (f"edits scoreboard (last {days} days): {len(items)} edit{'s' if len(items) != 1 else ''}, "
            f"{counts['fewer']} fewer, {counts['same']} same, {counts['more']} more, {counts['too early']} too early")
    if not items:
        return [head, "No applied edits in this window yet."]
    return [head, *(s.line() for s in items)]


def as_json(items: list[Score], days: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    return {"generated_at": now.isoformat(timespec="seconds"), "window_days": days,
            "min_sessions": MIN_SESSIONS, "edits": [asdict(s) for s in items]}
