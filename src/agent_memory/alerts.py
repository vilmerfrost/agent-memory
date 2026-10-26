"""Phone alerts the existing pushes missed, through the same ntfy channel (`edits_push._publish`, notify.env).

Since 2026-10-26 the morning has one push: the 07:30 brief job's `push_morning`. It carries
- the night's result, when there is something to say (skipped, partly run, tested, or a decision waits);
- what amem-daily used to push itself before it moved to 00:40: new escalations (counts and routine names only,
  never run summaries, failures or ids), slips of the safety rules (working around a guard, the push and merge
  policy, the safety guards), and staged proposals on their last day.
A quiet night with none of those sends nothing; a quiet night with new escalations still pushes.

Each part goes out once: escalation ids are marked pushed in amem-daily's state file after ntfy answered 200, the
rows record the rest (`rsi_proposals.meta.reminded`, `deviations.meta.alert`). `remind_proposals` (with re-armed
buttons) and `push_safety_slips` stay as stand-alone commands. Messages carry plain words, rule names and file
names only, then `redact`, like the other pushes. Raises nothing that should stop a routine: callers log a warning.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import edits_push, redact, store
from agent_memory import rsi_approval as AP

REMIND_WITHIN = timedelta(hours=30)   # the daily 07:30 brief push catches every proposal on its last day
SAFETY_RULES = ("approval-without-handing-work-back", "safety-guards", "push-and-merge-policy")
SAFETY_DAYS = 3
SAFETY_LINES = 5

VERDICT_WORDS = {
    "WORSE": "made things worse, nothing changed",
    "NO_EFFECT": "no clear difference, nothing changed",
    "INCONCLUSIVE": "the test could not tell, nothing changed",
    "IMPROVES": "it won and waits for your OK",
}


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _words(s: str) -> str:
    return re.sub(r"[-_]+", " ", re.sub(r"^\d+\.\s*", "", s)).strip()


def file_name(source: str) -> str:
    """The file a rule lives in, as Night Desk names it ("CLAUDE.md", "Meltdown skill", "worker.md")."""
    if source == "claude-md:global":
        return "CLAUDE.md"
    if source == "agents-md:global":
        return "AGENTS.md"
    kind, _, name = source.partition(":")
    if kind == "parent-md":
        return "PARENT.md"
    if kind == "skill":
        return f"{_cap(_words(name))} skill"
    if kind == "agent":
        return f"{name}.md"
    if kind == "routine":
        return f"{name} routine"
    return _cap(_words(source)) or "a rule file"


def rule_name(key: str) -> str:
    """"dev:skill:meltdown:intro" -> "Meltdown skill › Intro"."""
    k = key.removeprefix("dev:")
    source, _, rule = k.rpartition(":")
    section = "whole file" if rule in ("", "whole") else _cap(_words(rule.split("/")[-1]))
    return f"{file_name(source or k)} › {section}"


# --------------------------------------------------------------------------- proposals on their last day


def remind_proposals(*, db_url: str | None = None, push: bool = True, notify: dict[str, str] | None = None,
                     now: datetime | None = None) -> list[str]:
    """One reminder per staged proposal that expires within REMIND_WITHIN and was not reminded yet. Its tokens
    are re-armed, so the reminder's buttons work and the first push's buttons stop working."""
    out: list[str] = []
    now = now or datetime.now(UTC)
    url, page = AP.decide_url(), AP.decide_page()
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from rsi_proposals where status = 'staged' and expires_at > %s and expires_at <= %s "
                    "and coalesce(meta->>'reminded', '') = '' order by expires_at", (now, now + REMIND_WITHIN))
        for p in cur.fetchall():
            if not push:
                out.append("off")
                continue
            tokens = AP.issue_tokens(conn, str(p["id"]), p["artifact_hash"])
            hours = max(1, round((p["expires_at"] - now).total_seconds() / 3600))

            def payload(topic: str, p: dict[str, Any] = p, tokens: dict[str, str] = tokens,
                        hours: int = hours) -> dict[str, Any]:
                pay = AP.proposal_payload(topic, p, tokens, url, page)
                pay["title"] = f"Expires in {hours} h · {pay['title']}"[:120]
                pay["priority"] = 4
                pay["tags"] = ["hourglass_flowing_sand"]
                return pay

            res = edits_push._publish(payload, f"Expires in {hours} h: {p['title']}"[:120],
                                      f"amem rsi show {p['id']}", notify)
            conn.execute("update rsi_proposals set meta = meta || %s where id = %s",
                         (Jsonb({"reminded": res}), p["id"]))
            out.append(res)
    return out


# --------------------------------------------------------------------------- the night's result


def _skip_words(b: Mapping[str, Any]) -> str:
    for n in b.get("notes") or []:
        m = re.match(r"skipped by amem-rsi-night: prerequisite ([\w-]+): (last run \w+|still running)", str(n))
        if m and m.group(2) == "still running":
            return (f"the deviation scan ({m.group(1)}) was still running" if m.group(1) == "amem-daily"
                    else f"the routine it depends on ({m.group(1)}) was still running")
        if m:
            return ("the deviation scan (amem-daily) failed" if m.group(1) == "amem-daily"
                    else f"the routine it depends on ({m.group(1)}) did not pass")
    sk = b.get("skipped") or {}
    return str(sk.get("summary") or "a check before the night failed")


def night_message(b: Mapping[str, Any]) -> tuple[str, str] | None:
    """(title, body) for the night, or None for a quiet night (it ran, tested nothing, nothing waits)."""
    night, status = str(b.get("night") or "?"), str(b.get("status") or "?")
    pending = int((b.get("pending_decisions") or {}).get("total") or 0)
    exps = list((b.get("experiments") or {}).get("items") or [])
    total = (b.get("costs") or {}).get("total_usd")
    cost = f"Cost ${float(total):.2f}." if total is not None else ""
    lines: list[str] = []
    if status == "preflight_failed":
        title = f"Night {night}: skipped"
        lines.append(f"Skipped: {_skip_words(b)}. Nothing was read or tested.")
    else:
        if not exps and status == "ok" and not pending:
            return None
        for x in exps:
            verdict = str(x.get("verdict") or "")
            lines.append(f"Tested {rule_name(str(x.get('candidate_key') or '?'))}: "
                         f"{VERDICT_WORDS.get(verdict, 'the test did not finish')}.")
        if not exps:
            lines.append("Nothing was tested.")
        if status != "ok":
            lines.append(f"The night only partly ran ({status.replace('_', ' ')}).")
        title = f"Night {night}: " + (lines[0].rstrip(".") if exps else status.replace("_", " "))
    degraded = [str(s) for s in ((b.get("health") or {}).get("degraded") or [])]
    if degraded:
        lines.append("Not working: " + ", ".join(degraded[:4]) + ".")
    lines.append(f"Waiting for your OK: {pending}." if pending else "Nothing waits for your OK.")
    if cost:
        lines.append(cost)
    lines.append("Details: Night Desk, Last night.")
    return redact.redact(title)[:120], redact.redact("\n".join(lines))[:1500]


def push_night(b: Mapping[str, Any], *, notify: dict[str, str] | None = None,
               extra: MorningExtras | None = None) -> str:
    """'phone' | 'mac' when sent, 'quiet' when the night and `extra` have nothing to say. `extra`: what
    amem-daily used to push on its own (new escalations, safety slips, proposals on their last day), so the
    morning brings one notification (push_morning)."""
    msg = morning_message(b, extra) if extra is not None else night_message(b)
    if msg is None:
        return "quiet"
    title, body = msg
    priority = 4 if extra is not None and (extra.safety or extra.expiring) else 3
    tags = ["warning"] if extra is not None and extra.safety else ["crescent_moon"]
    return edits_push._publish(lambda topic: {"topic": topic, "title": title, "message": body, "priority": priority,
                                              "tags": tags}, title, body, notify)


# --------------------------------------------------------------------------- the one morning push


@dataclass
class MorningExtras:
    """What joins the night's push: new escalations (amem-daily's counts-and-routines push: `escalations` is the
    open count, `new_rows` the ones not pushed yet), safety-rule slips not alerted yet, and staged proposals on
    their last day not reminded yet. Ids never reach the message."""
    open_escalations: int = 0
    new_rows: list[dict[str, Any]] = field(default_factory=list)
    kept_ids: set[str] = field(default_factory=set)
    safety: list[dict[str, Any]] = field(default_factory=list)
    expiring: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # a part that could not be read (a warning, not a failure)

    def empty(self) -> bool:
        return not (self.new_rows or self.safety or self.expiring)


def escalation_lines(new_rows: list[Mapping[str, Any]], open_count: int) -> tuple[str, str]:
    """(title, line) for new escalations: counts and routine names only (the old amem-daily push)."""
    from agent_memory import daily
    rows = [dict(r) for r in new_rows]
    return daily.message(rows, open_count)


def expiring_lines(rows: list[Mapping[str, Any]], now: datetime) -> list[str]:
    out = []
    for p in rows[:SAFETY_LINES]:
        hours = max(1, round((p["expires_at"] - now).total_seconds() / 3600))
        title = re.sub(r"\s+", " ", str(p.get("title") or "a proposal")).strip()[:120]
        out.append(f"- Expires in {hours} h: {title}")
    if len(rows) > SAFETY_LINES:
        out.append(f"- and {len(rows) - SAFETY_LINES} more")
    return out


def morning_message(b: Mapping[str, Any], extra: MorningExtras,
                    now: datetime | None = None) -> tuple[str, str] | None:
    """The night's message plus amem-daily's parts, or None when there is nothing at all to say. The title is the
    night's when it has one, else the most urgent part's (safety, then last-day proposals, then escalations)."""
    now = now or datetime.now(UTC)
    night = night_message(b)
    parts: list[tuple[str, list[str]]] = []          # (title, lines)
    if extra.safety:
        s_title, s_body = safety_message(extra.safety)
        parts.append((s_title, [s_title + ":"] + [ln for ln in s_body.splitlines() if ln.startswith("- ")]))
    if extra.expiring:
        n = len(extra.expiring)
        e_title = f"{n} proposal{'s' if n != 1 else ''} on the last day"
        parts.append((e_title, [e_title + " (decide in Night Desk):"] + expiring_lines(extra.expiring, now)))
    if extra.new_rows:
        x_title, x_body = escalation_lines(extra.new_rows, extra.open_escalations)
        parts.append((x_title, [x_body]))
    if night is None and not parts:
        return None
    if night is not None:
        title, lines = night[0], night[1].splitlines()
        details = [ln for ln in lines if ln.startswith("Details:")]
        lines = [ln for ln in lines if not ln.startswith("Details:")]
    else:
        title = parts[0][0] if len(parts) == 1 else f"amem morning: {parts[0][0]} + {len(parts) - 1} more"
        lines, details = [], []
    for _, part in parts:
        if lines:
            lines.append("")
        lines += part
    lines += details or ["Details: Night Desk."]
    return redact.redact(title)[:120], redact.redact("\n".join(lines))[:2000]


def gather_morning(*, db_url: str | None = None, state_file: Any = None,
                   now: datetime | None = None) -> MorningExtras:
    """Read the parts amem-daily no longer pushes. Each part that fails is noted in `problems` and left out; the
    night's push still goes. Postgres only, and AMEM_REQUIRE_REMOTE applies (a local database never pushes)."""
    from agent_memory import config, daily, scoring
    now = now or datetime.now(UTC)
    x = MorningExtras()
    try:
        backend = config.backend()
        refused = config.remote_write_error(for_backend="postgres") if backend == "postgres" else None
    except config.BackendError as exc:
        backend, refused = "?", str(exc)
    if backend != "postgres" or refused is not None:
        x.problems.append(f"morning parts: needs the Postgres backend ({'remote required' if refused else backend})")
        return x
    try:
        rows = scoring.escalations(db_url=db_url)
        state = daily.state_path(state_file)
        dr = daily.DailyResult()
        before = daily.load_state(state, dr)
        x.new_rows, x.kept_ids = daily.plan_push(rows, before)
        x.open_escalations = len(rows)
        x.problems += dr.failures
    except Exception as exc:  # noqa: BLE001 - one part only
        x.problems.append(f"escalations: {type(exc).__name__}: {str(exc)[:160]}")
    try:
        with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("select id, instruction_source, rule_id, rule_title, what from deviations "
                        "where created_at > now() - %s and coalesce(meta->>'alert', '') = '' and ("
                        + " or ".join(["rule_id like %s"] * len(SAFETY_RULES)) + ") order by created_at",
                        (timedelta(days=SAFETY_DAYS), *[f"%{r}" for r in SAFETY_RULES]))
            x.safety = cur.fetchall()
            cur.execute("select id, title, expires_at from rsi_proposals where status = 'staged' and expires_at > %s "
                        "and expires_at <= %s and coalesce(meta->>'reminded', '') = '' order by expires_at",
                        (now, now + REMIND_WITHIN))
            x.expiring = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - one part only
        x.problems.append(f"alerts: {type(exc).__name__}: {str(exc)[:160]}")
    return x


def mark_morning(extra: MorningExtras, result: str, *, db_url: str | None = None, state_file: Any = None,
                 now: datetime | None = None) -> list[str]:
    """After the push: escalation ids become pushed only on 'phone' (a Mac fallback retries tomorrow, as amem-daily
    did); safety rows and reminded proposals record the result (sent once, as before). Returns problems."""
    from agent_memory import daily
    problems: list[str] = []
    now = now or datetime.now(UTC)
    if extra.new_rows or extra.kept_ids:
        pushed = set(extra.kept_ids)
        if result == "phone":
            pushed |= {str(r["run_id"]) for r in extra.new_rows}
        try:
            daily.save_state(daily.state_path(state_file), pushed, now.astimezone())
        except OSError as exc:
            problems.append(f"escalations: cannot write the state file: {type(exc).__name__}")
    if extra.safety or extra.expiring:
        try:
            with store.connect(db_url) as conn:
                if extra.safety:
                    conn.execute("update deviations set meta = coalesce(meta, '{}'::jsonb) || %s where id = any(%s)",
                                 (Jsonb({"alert": result}), [r["id"] for r in extra.safety]))
                if extra.expiring:
                    conn.execute("update rsi_proposals set meta = meta || %s where id = any(%s)",
                                 (Jsonb({"reminded": result}), [r["id"] for r in extra.expiring]))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"alerts: cannot mark what was sent: {type(exc).__name__}")
    return problems


def push_morning(b: Mapping[str, Any], *, notify: dict[str, str] | None = None, db_url: str | None = None,
                 state_file: Any = None, now: datetime | None = None) -> tuple[str, MorningExtras, list[str]]:
    """The brief job's one morning push: the night's result plus amem-daily's parts. A quiet night with new
    escalations (or a safety slip, or a last-day proposal) still pushes. (result, parts, problems)."""
    extra = gather_morning(db_url=db_url, state_file=state_file, now=now)
    result = push_night(b, notify=notify, extra=extra)
    problems = list(extra.problems)
    if result != "quiet":
        problems += mark_morning(extra, result, db_url=db_url, state_file=state_file, now=now)
    return result, extra, problems


# --------------------------------------------------------------------------- safety-rule slips


def safety_message(rows: list[Mapping[str, Any]]) -> tuple[str, str]:
    n = len(rows)
    lines = []
    for r in rows[:SAFETY_LINES]:
        section = _cap(_words(str(r.get("rule_title") or str(r.get("rule_id") or "").split("/")[-1])))
        what = re.sub(r"\s+", " ", str(r.get("what") or "")).strip()
        lines.append(f"- {file_name(str(r.get('instruction_source') or ''))} › {section}: {what[:180]}")
    if n > SAFETY_LINES:
        lines.append(f"- and {n - SAFETY_LINES} more")
    lines.append("Details: Night Desk, Last night, What went wrong this week.")
    title = f"Safety rule broken: {n} slip{'s' if n != 1 else ''}"
    return title, redact.redact("\n".join(lines))[:2000]


def push_safety_slips(*, db_url: str | None = None, push: bool = True,
                      notify: dict[str, str] | None = None) -> str | None:
    """One push for the safety-rule slips of the last SAFETY_DAYS that were not alerted yet.
    None when there are none, 'off' when push is off (rows stay unmarked)."""
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select id, instruction_source, rule_id, rule_title, what from deviations "
                    "where created_at > now() - %s and coalesce(meta->>'alert', '') = '' and ("
                    + " or ".join(["rule_id like %s"] * len(SAFETY_RULES)) + ") order by created_at",
                    (timedelta(days=SAFETY_DAYS), *[f"%{r}" for r in SAFETY_RULES]))
        rows = cur.fetchall()
        if not rows:
            return None
        if not push:
            return "off"
        title, body = safety_message(rows)
        res = edits_push._publish(lambda topic: {"topic": topic, "title": title, "message": body, "priority": 4,
                                                 "tags": ["warning"]}, title, body, notify)
        conn.execute("update deviations set meta = coalesce(meta, '{}'::jsonb) || %s where id = any(%s)",
                     (Jsonb({"alert": res}), [r["id"] for r in rows]))
        return res
