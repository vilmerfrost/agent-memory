"""`amem rsi outcome`: did the changes Vilmer approved help in daily use? (docs/rsi/outcome.md)

Runs daily at 09:20 (launchd com.vilmerfrost.amem-rsi-outcome). Read-only toward every rule file: it measures and
advises, it never undoes anything.

Applied changes:
- a night proposal promoted through Night Desk (`rsi_proposals.status = promoted`, not reverted): its rule is the
  candidate's `rule_key` (or, for an old morning proposal, the rule its `edits` row names); applied at `promoted_at`;
- an older edit-loop edit that was kept (`edits.status in (kept, trial)`, not reverted, no Undo requested): its
  `instruction_source` + `rule_id`; applied at `created_at` (the row is written just before `improve apply`).

For each change: two windows of the same length W around the apply time t, [t - W, t) and [t, t + W), with
W = min(WINDOW_DAYS, days since t). Only real sessions count: prod (not sandbox or eval), already scanned for
deviations, and that loaded the rule's file (`sessions.instructions` holds its instruction source). Per window:
the sessions n, the rule's deviations k in them, and the rate k / n (deviations per session).

Verdicts (`judge`):
- too-early: applied less than MIN_DAYS ago, or fewer than MIN_SESSIONS sessions in either window;
- helped: the after-share of the deviations is low by a one-sided exact test (given k_b + k_a deviations, the after
  count is binomial with p = n_a / (n_a + n_b)) at p <= ALPHA, and the rate fell by at least MIN_EFFECT;
- worse: the same test the other way, and the rate rose by at least MIN_EFFECT (or from zero);
- no-change: anything else (also: no deviations of the rule in either window).
`worse` always records a suggested Undo; `no-change` does once the full WINDOW_DAYS window has been observed
(UNDO_AFTER_DAYS). Never automatic: Night Desk shows the advice next to its Undo button.

Writes: one `rsi_outcomes` row per change (the latest verdict), `rsi-outcome-<date>.md` and `.json` in the reports
dir, and one routines.log line `amem-rsi-outcome PASS|FAIL changes=N helped=.. no_change=.. worse=.. too_early=..
suggest_undo=.. <secs>s report=<path>` plus `amem-rsi-outcome-fail <item>` lines (the format routine-audit reads).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import redact, store

ROUTINE = "amem-rsi-outcome"
FORMAT = "rsi-outcome/1"
WINDOW_DAYS = 14.0        # each window at most this long
MIN_DAYS = 3.0            # a change younger than this is too early
MIN_SESSIONS = 5          # per window
ALPHA = 0.10              # one-sided exact test
MIN_EFFECT = 0.25         # the rate must move by at least a quarter
UNDO_AFTER_DAYS = 14.0    # no-change suggests Undo once the full window was observed
VERDICTS = ("helped", "no-change", "worse", "too-early")
PROD = "coalesce(s.env, 'prod') not in ('sandbox', 'eval')"


@dataclass
class Change:
    kind: str                 # proposal | edit
    id: str
    instruction_source: str
    rule_id: str
    applied_at: datetime
    title: str | None = None

    @property
    def rule_key(self) -> str:
        return f"{self.instruction_source}:{self.rule_id}"

    def undo_hint(self) -> str:
        if self.kind == "proposal":
            return f"Night Desk: Undo on this change (or `amem rsi decide {self.id} undo` in your terminal)"
        return f"Night Desk: Undo on this edit (or `amem edits undo {self.id}` in your terminal)"


@dataclass
class Outcome:
    change: Change
    window_days: float
    sessions_before: int = 0
    sessions_after: int = 0
    deviations_before: int = 0
    deviations_after: int = 0
    rate_before: float | None = None
    rate_after: float | None = None
    p_value: float | None = None
    verdict: str = "too-early"
    reason: str = ""
    suggest_undo: bool = False
    observed_days: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        c = d.pop("change")
        c["applied_at"] = self.change.applied_at.isoformat(timespec="seconds")
        return {"change_kind": c["kind"], "change_id": c["id"], "rule_key": self.change.rule_key, "title": c["title"],
                "instruction_source": c["instruction_source"], "rule_id": c["rule_id"],
                "applied_at": c["applied_at"], **d,
                "undo_hint": self.change.undo_hint() if self.suggest_undo else None}


@dataclass
class Report:
    checked_at: datetime
    outcomes: list[Outcome] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)     # changes the check could not measure (no rule)
    failures: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = {v: 0 for v in VERDICTS}
        for o in self.outcomes:
            c[o.verdict] += 1
        return c

    @property
    def suggest_undo(self) -> int:
        return sum(1 for o in self.outcomes if o.suggest_undo)

    @property
    def status(self) -> str:
        return "FAIL" if self.failures else "PASS"


# ---------------------------------------------------------------- the statistics


def _binom_tail(k: int, n: int, p: float, *, upper: bool) -> float:
    """P(X >= k) (upper) or P(X <= k), X ~ Binomial(n, p). Exact; n is small (deviation counts)."""
    if n <= 0:
        return 1.0
    p = min(max(p, 0.0), 1.0)
    ks = range(k, n + 1) if upper else range(0, k + 1)
    total = 0.0
    for i in ks:
        if p in (0.0, 1.0):
            total += 1.0 if (p == 0.0 and i == 0) or (p == 1.0 and i == n) else 0.0
        else:
            total += math.comb(n, i) * p ** i * (1 - p) ** (n - i)
    return min(1.0, total)


def judge(k_b: int, n_b: int, k_a: int, n_a: int, observed_days: float) -> tuple[str, float | None, str, bool]:
    """(verdict, p_value, reason, suggest_undo) for k_b deviations in n_b sessions before, k_a in n_a after."""
    if observed_days < MIN_DAYS:
        return ("too-early", None, f"applied {observed_days:.1f} days ago; the check needs {MIN_DAYS:g} days", False)
    if n_b < MIN_SESSIONS or n_a < MIN_SESSIONS:
        return ("too-early", None, f"{n_b} sessions before and {n_a} after loaded the rule's file; the check needs "
                                   f"{MIN_SESSIONS} on each side", False)
    full = observed_days >= UNDO_AFTER_DAYS
    rate_b, rate_a = k_b / n_b, k_a / n_a
    total = k_b + k_a
    if total == 0:
        return ("no-change", None, "no deviations of this rule before or after the change"
                + ("; the full window was observed" if full else ""), full)
    share = n_a / (n_a + n_b)
    p_low = _binom_tail(k_a, total, share, upper=False)
    p_high = _binom_tail(k_a, total, share, upper=True)
    rates = f"{rate_b:.2f} -> {rate_a:.2f} deviations per session ({k_b}/{n_b} before, {k_a}/{n_a} after)"
    if p_low <= ALPHA and rate_a <= rate_b * (1 - MIN_EFFECT):
        return ("helped", round(p_low, 4), f"fewer deviations: {rates}", False)
    if p_high <= ALPHA and (rate_a >= rate_b * (1 + MIN_EFFECT) if rate_b > 0 else rate_a > 0):
        return ("worse", round(p_high, 4), f"more deviations: {rates}", True)
    p = round(min(p_low, p_high), 4)
    return ("no-change", p, f"no clear difference: {rates}" + (f"; no effect after {observed_days:.0f} days"
                                                               if full else ""), full)


# ---------------------------------------------------------------- the changes and their windows


def split_rule(key: str) -> tuple[str, str] | None:
    k = key.removeprefix("dev:")
    source, _, rule = k.rpartition(":")
    return (source, rule) if source and rule else None


def applied_changes(conn: Any) -> tuple[list[Change], list[str]]:
    """(changes, skipped): promoted night proposals and kept edits. A proposal whose rule cannot be named is
    skipped with a reason."""
    out: list[Change] = []
    skipped: list[str] = []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select p.id::text as id, p.title, p.promoted_at, c.rule_key, c.dedupe_key,
                              e.instruction_source as e_source, e.rule_id as e_rule
                       from rsi_proposals p
                       left join rsi_candidates c on c.id = p.candidate_id
                       left join lateral (select instruction_source, rule_id from edits
                                          where meta->>'proposal_id' = p.id::text order by created_at desc limit 1) e
                              on true
                       where p.status = 'promoted' and p.promoted_at is not null and p.reverted_at is null
                       order by p.promoted_at, p.id""")
        for r in cur.fetchall():
            rule = (split_rule(r["rule_key"]) if r["rule_key"] else None) or \
                   ((r["e_source"], r["e_rule"]) if r["e_source"] and r["e_rule"] else None) or \
                   (split_rule(r["dedupe_key"]) if r["dedupe_key"] else None)
            if rule is None:
                skipped.append(f"proposal {r['id']}: no rule to measure (no candidate rule_key or edits row)")
                continue
            out.append(Change("proposal", r["id"], rule[0], rule[1], r["promoted_at"], r["title"]))
        cur.execute("""select id::text as id, coalesce(rule_title, rule_id) as title, created_at, instruction_source,
                              rule_id
                       from edits
                       where status in ('kept', 'trial') and reverted_at is null and undo_requested_at is null
                       order by created_at, id""")
        for r in cur.fetchall():
            out.append(Change("edit", r["id"], r["instruction_source"], r["rule_id"], r["created_at"], r["title"]))
    return out, skipped


_COUNTS_SQL = f"""
with s as (
    select s.session_id, coalesce(s.started_at, s.created_at) as at
    from sessions s
    where {PROD} and s.scanned_at is not null and s.instructions ? %(src)s
      and coalesce(s.started_at, s.created_at) >= %(t)s - %(w)s
      and coalesce(s.started_at, s.created_at) < %(t)s + %(w)s
), d as (
    select session_id, count(*) as n from deviations
    where instruction_source = %(src)s and rule_id = %(rid)s and session_id in (select session_id from s)
    group by session_id
)
select count(*) filter (where s.at < %(t)s), count(*) filter (where s.at >= %(t)s),
       coalesce(sum(d.n) filter (where s.at < %(t)s), 0), coalesce(sum(d.n) filter (where s.at >= %(t)s), 0)
from s left join d using (session_id)
"""


def measure(conn: Any, c: Change, now: datetime) -> Outcome:
    observed = max(0.0, (now - c.applied_at).total_seconds() / 86400)
    w = min(WINDOW_DAYS, observed)
    o = Outcome(c, window_days=round(w, 3), observed_days=round(observed, 3))
    if w > 0:
        n_b, n_a, k_b, k_a = (int(x or 0) for x in conn.execute(
            _COUNTS_SQL, {"src": c.instruction_source, "rid": c.rule_id, "t": c.applied_at,
                          "w": timedelta(days=w)}).fetchone())
        o.sessions_before, o.sessions_after, o.deviations_before, o.deviations_after = n_b, n_a, k_b, k_a
        o.rate_before = round(k_b / n_b, 4) if n_b else None
        o.rate_after = round(k_a / n_a, 4) if n_a else None
    o.verdict, o.p_value, o.reason, o.suggest_undo = judge(o.deviations_before, o.sessions_before,
                                                           o.deviations_after, o.sessions_after, observed)
    o.reason = redact.redact(o.reason)[:500]
    return o


def store_outcomes(conn: Any, outcomes: list[Outcome], now: datetime) -> None:
    for o in outcomes:
        c = o.change
        conn.execute(
            """insert into rsi_outcomes (change_kind, change_id, rule_key, instruction_source, rule_id, title, applied_at,
                   checked_at, first_checked_at, window_days, sessions_before, sessions_after, deviations_before,
                   deviations_after, rate_before, rate_after, p_value, verdict, reason, suggest_undo, undo_hint, meta)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (change_kind, change_id) do update set
                   rule_key = excluded.rule_key, instruction_source = excluded.instruction_source,
                   rule_id = excluded.rule_id, title = excluded.title, applied_at = excluded.applied_at,
                   checked_at = excluded.checked_at, window_days = excluded.window_days,
                   sessions_before = excluded.sessions_before, sessions_after = excluded.sessions_after,
                   deviations_before = excluded.deviations_before, deviations_after = excluded.deviations_after,
                   rate_before = excluded.rate_before, rate_after = excluded.rate_after, p_value = excluded.p_value,
                   verdict = excluded.verdict, reason = excluded.reason, suggest_undo = excluded.suggest_undo,
                   undo_hint = excluded.undo_hint,
                   meta = rsi_outcomes.meta || excluded.meta""",
            (c.kind, c.id, c.rule_key, c.instruction_source, c.rule_id,
             redact.redact(c.title)[:200] if c.title else None, c.applied_at, now, now, o.window_days,
             o.sessions_before, o.sessions_after, o.deviations_before, o.deviations_after, o.rate_before, o.rate_after,
             o.p_value, o.verdict, o.reason, o.suggest_undo, c.undo_hint() if o.suggest_undo else None,
             Jsonb({"observed_days": o.observed_days, "format": FORMAT,
                    "thresholds": {"window_days": WINDOW_DAYS, "min_days": MIN_DAYS, "min_sessions": MIN_SESSIONS,
                                   "alpha": ALPHA, "min_effect": MIN_EFFECT, "undo_after_days": UNDO_AFTER_DAYS}})))


def check(*, db_url: str | None = None, now: datetime | None = None, write_db: bool = True) -> Report:
    """Measure every applied change; store the verdicts unless write_db is False. A database error is a failure
    in the report (never raised)."""
    now = now if now is not None and now.tzinfo is not None else (now or datetime.now()).astimezone()
    rep = Report(checked_at=now)
    try:
        with store.connect(db_url) as conn:
            changes, rep.skipped = applied_changes(conn)
            for c in changes:
                rep.outcomes.append(measure(conn, c, now))
            if write_db:
                store_outcomes(conn, rep.outcomes, now)
    except Exception as exc:  # noqa: BLE001 - the routine logs a FAIL line instead
        rep.failures.append(f"database: {type(exc).__name__}: {redact.redact(str(exc))[:200]}")
    return rep


# ---------------------------------------------------------------- report files and the routines.log line


VERDICT_WORDS = {"helped": "helped", "no-change": "no change", "worse": "worse", "too-early": "too early"}


def render_markdown(rep: Report) -> str:
    c = rep.counts()
    lines = [f"# Outcome check: {rep.checked_at:%Y-%m-%d %H:%M}", "",
             f"{len(rep.outcomes)} applied change(s): {c['helped']} helped, {c['no-change']} no change, "
             f"{c['worse']} worse, {c['too-early']} too early. Suggested Undo: {rep.suggest_undo}.", "",
             f"Rule: deviations per session in real sessions that loaded the rule's file, {WINDOW_DAYS:g} days (at "
             f"most) before vs after the apply time; at least {MIN_SESSIONS} sessions each side and {MIN_DAYS:g} "
             f"days; helped / worse need a one-sided exact test at p <= {ALPHA:g} and a {MIN_EFFECT:.0%} change. "
             "Nothing is undone automatically.", ""]
    order = {"worse": 0, "no-change": 1, "helped": 2, "too-early": 3}
    for o in sorted(rep.outcomes, key=lambda o: (order[o.verdict], o.change.applied_at)):
        ch = o.change
        lines.append(f"## {VERDICT_WORDS[o.verdict].capitalize()}: {ch.title or ch.rule_key}")
        lines.append(f"- Change: {ch.kind} `{ch.id}`, rule `{ch.rule_key}`, applied "
                     f"{ch.applied_at:%Y-%m-%d %H:%M} ({o.observed_days:.1f} days ago)")
        lines.append(f"- Windows: {o.window_days:.1f} days each; before {o.deviations_before} deviation(s) in "
                     f"{o.sessions_before} session(s), after {o.deviations_after} in {o.sessions_after}"
                     + (f"; p = {o.p_value:g}" if o.p_value is not None else ""))
        lines.append(f"- Why: {o.reason}")
        if o.suggest_undo:
            lines.append(f"- Suggested: Undo. {ch.undo_hint()}")
        lines.append("")
    if not rep.outcomes:
        lines += ["No applied change to check.", ""]
    for s in rep.skipped:
        lines.append(f"- Not measured: {s}")
    for f in rep.failures:
        lines.append(f"- FAILED: {f}")
    return redact.redact("\n".join(lines).rstrip() + "\n")


def report_json(rep: Report) -> dict[str, Any]:
    return {"format": FORMAT, "checked_at": rep.checked_at.isoformat(timespec="seconds"), "status": rep.status,
            "counts": rep.counts(), "suggest_undo": rep.suggest_undo, "skipped": rep.skipped,
            "failures": rep.failures, "outcomes": [o.as_dict() for o in rep.outcomes],
            "thresholds": {"window_days": WINDOW_DAYS, "min_days": MIN_DAYS, "min_sessions": MIN_SESSIONS,
                           "alpha": ALPHA, "min_effect": MIN_EFFECT, "undo_after_days": UNDO_AFTER_DAYS}}


def write_reports(rep: Report, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    day = rep.checked_at.strftime("%Y-%m-%d")
    md, js = out_dir / f"rsi-outcome-{day}.md", out_dir / f"rsi-outcome-{day}.json"
    md.write_text(render_markdown(rep), encoding="utf-8")
    js.write_text(json.dumps(report_json(rep), sort_keys=True, default=str, indent=1) + "\n", encoding="utf-8")
    return md, js


def run_job(*, db_url: str | None = None, now: datetime | None = None, out_dir: Path | None = None,
            routines_log: Path | None = None, write_log: bool = True, write_db: bool = True) -> tuple[Report, list[str]]:
    """The scheduled check: measure, store, write the reports, append one routines.log line."""
    from agent_memory import rsi_schedule as S
    t0 = time.monotonic()
    rep = check(db_url=db_url, now=now, write_db=write_db)
    out = Path(out_dir or S.DEFAULT_REPORTS_DIR).expanduser()
    report: str | None = None
    try:
        md, _ = write_reports(rep, out)
        report = str(md)
    except OSError as exc:
        rep.failures.append(f"report: cannot write {out}: {type(exc).__name__}")
    c = rep.counts()
    res = S.JobResult(ROUTINE, rep.status, rep.checked_at.strftime("%Y-%m-%d"), items=list(rep.failures),
                      warnings=list(rep.skipped),
                      fields={"changes": len(rep.outcomes), "helped": c["helped"], "no_change": c["no-change"],
                              "worse": c["worse"], "too_early": c["too-early"], "suggest_undo": rep.suggest_undo},
                      report=report)
    lines = S.log_lines(res, now=rep.checked_at, secs=int(time.monotonic() - t0))
    if write_log:
        S.append_log(S.routines_log_path(routines_log), lines)
    return rep, lines


def cmd_outcome(args: argparse.Namespace) -> int:
    from agent_memory.cli_deviations import _read_guard, _write_guard
    guard = _read_guard if args.dry_run else _write_guard
    if (code := guard("rsi outcome")) is not None:
        return code
    now = None
    if args.now:
        now = datetime.fromisoformat(args.now)
        now = now if now.tzinfo else now.astimezone()
    if args.dry_run:
        rep = check(now=now, write_db=False)
        print(json.dumps(report_json(rep), sort_keys=True, default=str) if args.json else render_markdown(rep))
        return 0 if rep.status == "PASS" else 1
    rep, lines = run_job(now=now, out_dir=args.out, routines_log=args.routines_log)
    if args.json:
        print(json.dumps({**report_json(rep), "lines": lines}, sort_keys=True, default=str))
    else:
        print("\n".join(lines))
    return 0 if rep.status == "PASS" else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("outcome", help="did applied changes help? compare each rule's deviations per session before "
                                       "and after the apply; store verdicts, write a report, one routines.log line "
                                       "(launchd 09:20; never undoes anything)")
    p.add_argument("--out", type=Path, default=None, help="report dir (default ~/.claude/parent/reports)")
    p.add_argument("--routines-log", type=Path, default=None)
    p.add_argument("--now", default=None, help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true", help="measure and print; write nothing (no DB, files or log)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_outcome)


if __name__ == "__main__":  # pragma: no cover
    sys.exit("use `amem rsi outcome`")
