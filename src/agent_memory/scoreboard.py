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

Cooldown (docs/edits.md, "Cooldown"): a kept or trial edit holds its rule (source + rule id)
for AMEM_EDITS_COOLDOWN_DAYS (default 7) days. `edits run` skips the rule until then, unless
this scoreboard's verdict for that edit is `more`. Reverted, refused and auto-reverted edits
start no cooldown. Workflow search is noisy; re-editing a rule before its last edit had time
to show an effect is the sharp-regression case (MASS).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_memory import config, store

DEFAULT_DAYS = 28
MIN_SESSIONS = 5
FEWER_RATIO = 0.75
MORE_RATIO = 1.25
MORE_MIN_DELTA = 0.05
APPLIED = ("kept", "trial", "reverted")
COOLDOWN_ENV = "AMEM_EDITS_COOLDOWN_DAYS"
DEFAULT_COOLDOWN_DAYS = 7
COOLDOWN_STATUSES = ("kept", "trial")   # reverted / refused / auto-reverted start no cooldown

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
    cooldown_until: str | None = None   # kept/trial edit still inside its cooldown window (UTC iso)
    cooldown_lifted: bool = False       # ... but the verdict is `more`: the rule may be re-edited

    def cooldown_text(self) -> str:
        if not self.cooldown_until:
            return ""
        if self.cooldown_lifted:
            return "cooldown lifted (more)"
        return f"cooldown until {self.cooldown_until[:10]}"

    def line(self) -> str:
        def rate(r: float | None, n: int, s: int) -> str:
            return f"{'-' if r is None else f'{r:.2f}'} per session ({n} in {s})"
        cd = self.cooldown_text()
        return (f"- {self.instruction_source} / {self.rule_title or self.rule_id} [{self.rule_id}] "
                f"({self.status}, {self.edited_at[:10]}, {self.target_path or '-'}): "
                f"before {rate(self.before_rate, self.before_deviations, self.before_sessions)} -> "
                f"after {rate(self.after_rate, self.after_deviations, self.after_sessions)}: {self.verdict}"
                + (f"; {cd}" if cd else ""))


def cooldown_days() -> int:
    """AMEM_EDITS_COOLDOWN_DAYS (0 turns the cooldown off), else 7. A bad value is the default."""
    config.load_env()
    raw = os.environ.get(COOLDOWN_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_COOLDOWN_DAYS
    except ValueError:
        return DEFAULT_COOLDOWN_DAYS
    return value if value >= 0 else DEFAULT_COOLDOWN_DAYS


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


def score_edit(conn: psycopg.Connection, e: dict[str, Any], *, days: int, now: datetime,
               cooldown: int | None = None) -> Score:
    """One edit row (the _EDITS_SQL columns) scored against its before / after windows."""
    at = e["created_at"]
    end = e["reverted_at"] or now
    base = {"src": e["instruction_source"], "rule": e["rule_id"], "sha": e["instruction_sha_before"]}
    before = {**base, "lo": at - timedelta(days=days), "hi": at, "after": False}
    after = {**base, "lo": at, "hi": end, "after": True}
    bd = conn.execute(_DEV_SQL, before).fetchone()[0]
    ad = conn.execute(_DEV_SQL, after).fetchone()[0]
    bs = conn.execute(_SESSIONS_SQL, before).fetchone()[0]
    as_ = conn.execute(_SESSIONS_SQL, after).fetchone()[0]
    v = verdict(bd, bs, ad, as_)
    s = Score(str(e["id"]), e["instruction_source"], e["rule_id"], e["rule_title"], e["status"],
              e["target_path"], e["improve_id"], at.isoformat(timespec="seconds"),
              bd, bs, ad, as_, _rate(bd, bs), _rate(ad, as_), v)
    cooldown = cooldown_days() if cooldown is None else cooldown
    if e["status"] in COOLDOWN_STATUSES and cooldown > 0:
        until = at + timedelta(days=cooldown)
        if until > now:
            s.cooldown_until = until.astimezone(UTC).isoformat(timespec="seconds")
            # `edits run` decides on the default window's verdict; show the same decision here.
            ref = v if days == DEFAULT_DAYS else score_edit(conn, e, days=DEFAULT_DAYS, now=now, cooldown=0).verdict
            s.cooldown_lifted = ref == "more"
    return s


def scores(*, days: int = DEFAULT_DAYS, now: datetime | None = None, db_url: str | None = None) -> list[Score]:
    now = now or datetime.now(UTC)
    cooldown = cooldown_days()
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_EDITS_SQL, (list(APPLIED), now - timedelta(days=days)))
        return [score_edit(conn, e, days=days, now=now, cooldown=cooldown) for e in cur.fetchall()]


_LATEST_SQL = """
select id, status, instruction_source, rule_id, rule_title, target_path, instruction_sha_before, improve_id,
       created_at, reverted_at
from edits where instruction_source = %s and rule_id = %s and status = any(%s) and created_at > %s
order by created_at desc, id limit 1
"""


def active_cooldown(conn: psycopg.Connection, source: str, rule_id: str, *, now: datetime | None = None,
                    cooldown: int | None = None) -> Score | None:
    """The newest kept/trial edit on this rule inside the cooldown window, scored (its
    `cooldown_lifted` says whether the verdict is `more`); None when the rule is free."""
    now = now or datetime.now(UTC)
    cooldown = cooldown_days() if cooldown is None else cooldown
    if cooldown <= 0:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_LATEST_SQL, (source, rule_id, list(COOLDOWN_STATUSES), now - timedelta(days=cooldown)))
        e = cur.fetchone()
    if e is None:
        return None
    return score_edit(conn, e, days=DEFAULT_DAYS, now=now, cooldown=cooldown)


def text_lines(items: list[Score], days: int) -> list[str]:
    counts = {v: sum(s.verdict == v for s in items) for v in ("fewer", "same", "more", "too early")}
    in_cooldown = sum(bool(s.cooldown_until) and not s.cooldown_lifted for s in items)
    head = (f"edits scoreboard (last {days} days): {len(items)} edit{'s' if len(items) != 1 else ''}, "
            f"{counts['fewer']} fewer, {counts['same']} same, {counts['more']} more, {counts['too early']} too early"
            + (f", {in_cooldown} in cooldown" if in_cooldown else ""))
    if not items:
        return [head, "No applied edits in this window yet."]
    return [head, *(s.line() for s in items)]


def as_json(items: list[Score], days: int, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    return {"generated_at": now.isoformat(timespec="seconds"), "window_days": days,
            "min_sessions": MIN_SESSIONS, "edits": [asdict(s) for s in items]}
