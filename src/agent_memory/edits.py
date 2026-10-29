"""`amem edits run`: the edit step of the v2 loop (docs/edits.md, roadmap v2 E4).

Per candidate from the deviations report (deviations.candidates, the `deviations report
--json` groups), highest Jev confidence tier first, then the most costly rule (`ordered`):

1. Filter: Jev verdict edit_instruction or enforce, confidence >= --min-confidence, not a
   team repo (`team: true`), a real rule id, not already handled at this file version, not
   in cooldown (a kept/trial edit in the last AMEM_EDITS_COOLDOWN_DAYS, unless the
   scoreboard says `more`; scoreboard.active_cooldown), and no failed `improve apply` in that
   window (recent_failed_apply; a failed apply restores the original file, restore_target).
2. Target (edits_targets.resolve): edit the real file, suggest (`improve suggest`) for repo
   files and files outside improve's allowed targets, or skip. A file or rule pinned in
   improve's pins.json is skipped here, before any writer call or row (improve would refuse it).
3. Edit: the writer (edits_writer) rewrites the one rule section, it is validated and
   spliced into a temp copy, its kind label is checked against the diff (edits_kind), and
   `improve apply --json` decides: kept, trial,
   auto-reverted or refused. One `edits` row records it; kept/trial edits get an undo
   token (only its sha256 is stored) and a push with an Undo button (edits_push).

Writer and improve calls need AMEM_EDITS_LIVE=1; without it `run` is a dry run (plan lines
only, nothing called or written). One candidate's error never stops the others.
`call_writer` (edits_writer) and `run_command` are module attributes so tests replace them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import (
    config,
    deviations,
    distill,
    edits_sections,
    edits_targets,
    edits_writer,
    rule_catalog,
    scoreboard,
    store,
    tokens_report,
)
from agent_memory.sessions_ingest import agent_config_dir

LIVE_ENV = "AMEM_EDITS_LIVE"
STAGE_ENV = "AMEM_EDITS_MODE"   # "stage" = RSI V1 approval-first: improve stage + a proposal, never improve apply
IMPROVE_ENV = "AMEM_IMPROVE_BIN"
VERDICTS = ("edit_instruction", "enforce")
DEFAULT_MIN_CONFIDENCE = 0.6
DEFAULT_LIMIT = 5
DEFAULT_DAYS = deviations.DEFAULT_DAYS
IMPROVE_TIMEOUT = 3_600.0      # improve apply runs evals
IMPROVE_STATUSES = ("kept", "trial", "auto-reverted", "refused")
# Exit codes the contract allows per status (auto-reverted: 0; today's improve exits 1).
_EXIT_OK = {"kept": {0}, "trial": {0}, "auto-reverted": {0, 1}, "refused": {2}}
REASON_MAX = 300


def stage_mode() -> bool:
    """AMEM_EDITS_MODE=stage: the edit becomes a staged proposal (docs/rsi/approval.md); production is untouched."""
    config.load_env()
    return os.environ.get(STAGE_ENV, "").strip() == "stage"


def live() -> bool:
    config.load_env()
    return os.environ.get(LIVE_ENV, "").strip() == "1"


def _one_line(text: Any, limit: int = REASON_MAX) -> str:
    out = " ".join(str(text).split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def file_sha(text: str) -> str:
    """Same as the deviations' instruction_sha for a one-file source."""
    return rule_catalog.sha([text])


# --------------------------------------------------------------------------- improve


def improve_bin() -> str:
    """AMEM_IMPROVE_BIN (tests), else ~/agent-config/bin/improve."""
    config.load_env()
    env = os.environ.get(IMPROVE_ENV, "").strip()
    return env or str(agent_config_dir() / "bin" / "improve")


KILL_GRACE = 10.0   # seconds between SIGTERM and SIGKILL to improve's process group


def run_command(argv: list[str], timeout: float = IMPROVE_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """Run improve in its own session. On a timeout the whole process group gets SIGTERM
    (improve's finally can release its eval lock), then SIGKILL after KILL_GRACE, so eval
    children never outlive it; then TimeoutExpired. Tests replace this (or AMEM_IMPROVE_BIN)."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise subprocess.TimeoutExpired(argv, timeout) from None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _kill_group(proc: subprocess.Popen[str]) -> None:
    for sig, grace in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            proc.communicate(timeout=grace)   # drains the pipes; returns once improve itself exits
        except subprocess.TimeoutExpired:
            continue
        if sig == signal.SIGTERM:             # improve exited; its children may not have: kill what is left
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        break


class ImproveError(RuntimeError):
    """improve could not be run, or its answer breaks the contract."""


@dataclass
class ImproveResult:
    id: str | None
    status: str
    reason: str
    revert: str | None


def _improve(args: list[str], timeout: float = IMPROVE_TIMEOUT) -> subprocess.CompletedProcess[str]:
    try:
        return run_command([improve_bin(), *args], timeout)
    except subprocess.TimeoutExpired:
        raise ImproveError(f"improve {args[0]} timed out after {timeout:g}s") from None
    except OSError as exc:
        raise ImproveError(f"cannot run improve: {type(exc).__name__}: {exc}") from None


def parse_apply(proc: subprocess.CompletedProcess[str]) -> ImproveResult:
    """The `improve apply --json` contract: one JSON line {"id", "status", "reason", "revert"};
    exit 0 for kept/trial/auto-reverted, 2 for refused. Anything else raises ImproveError."""
    obj = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if isinstance(cand, dict):
                obj = cand
                break
    if obj is None:
        tail = _one_line(proc.stderr or proc.stdout or "no output", 160)
        raise ImproveError(f"improve apply gave no JSON line (exit {proc.returncode}): {tail}")
    status = obj.get("status")
    if status not in IMPROVE_STATUSES:
        raise ImproveError(f"improve apply: unknown status {status!r} (exit {proc.returncode})")
    if proc.returncode not in _EXIT_OK[status]:
        raise ImproveError(f"improve apply: status {status} with exit {proc.returncode}")
    cid = obj.get("id")
    if cid is not None and not isinstance(cid, str):
        raise ImproveError("improve apply: id is not a string")
    if status in ("kept", "trial") and not cid:
        raise ImproveError(f"improve apply: status {status} without a change id")
    revert = obj.get("revert")
    return ImproveResult(cid or None, status, _one_line(obj.get("reason") or ""),
                         revert if isinstance(revert, str) else None)


def improve_rule_id(source: str, rule_id: str) -> str:
    """improve's --rule-id (kebab-case, 3-61 chars). Namespaced by the source, because improve
    blocks a reverted rule id everywhere and catalog ids (`intro`, `whole`) repeat across files."""
    raw = f"{source}--{rule_id}".lower().replace("/", "--").replace(":", "-")
    out = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    if len(out) > 61:
        out = out[:52].rstrip("-") + "-" + hashlib.sha256(raw.encode()).hexdigest()[:8]
    return out if len(out) >= 3 else (out + "-rule")


def evidence(cand: dict[str, Any]) -> str:
    return f"dev:{cand.get('count', 0)} deviations, {cand.get('sessions', 0)} sessions"


# --------------------------------------------------------------------------- order
# docs/edits.md, "Order": Jev confidence tier first, then cost, then the report's own order.

TIER_WIDTH = 0.1


def confidence_tier(cand: dict[str, Any]) -> float:
    """The mean Jev confidence rounded down to its 0.1 band (0.74 -> 0.7); -1 when unjudged."""
    conf = (cand.get("jev") or {}).get("confidence_mean")
    if conf is None:
        return -1.0
    return round(math.floor(round(float(conf) / TIER_WIDTH, 9)) * TIER_WIDTH, 1)


def cost_score(cand: dict[str, Any]) -> float:
    """deviations.cost_of: total tokens / 1M + active seconds / 60 of the deviating sessions."""
    cost = cand.get("cost") if isinstance(cand.get("cost"), dict) else {}
    return float(cost.get("score") or 0.0)


def ordered(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Highest confidence tier first; inside a tier, the most costly rule first. Ties keep the
    report's order (most recurring first)."""
    return sorted(cands, key=lambda c: (-confidence_tier(c), -cost_score(c)))


def cost_text(cand: dict[str, Any]) -> str | None:
    cost = cand.get("cost") if isinstance(cand.get("cost"), dict) else None
    if cost is None:
        return None
    return (f"{tokens_report.human(int(cost.get('tokens') or 0))} tok/"
            f"{tokens_report.hms(float(cost.get('active_seconds') or 0))}")


# --------------------------------------------------------------------------- database


def handled(conn: psycopg.Connection, source: str, rule_id: str, current_sha: str | None,
            cand_shas: list[str]) -> bool:
    """Already edited (or suggested, or tried) at this version of the instruction.

    Handled shas = every edits row's instruction_sha_before for this source + rule. The
    candidate is handled when the file's current sha is one of them (also after an undo,
    which brings the old sha back), or when every sha its deviations were found on is."""
    rows = conn.execute("select instruction_sha_before from edits where instruction_source = %s and rule_id = %s",
                        (source, rule_id)).fetchall()
    if not rows:
        return False
    before = {r[0] for r in rows if r[0]}
    if current_sha and current_sha in before:
        return True
    if cand_shas:
        return set(cand_shas) <= before
    return not current_sha


_INSERT_SQL = """
insert into edits (status, target_path, instruction_source, rule_id, rule_title, instruction_sha_before,
                   deviation_ids, verdict, jev_confidence, why, summary, meta)
values (%(status)s, %(target_path)s, %(source)s, %(rule_id)s, %(rule_title)s, %(sha_before)s,
        %(deviation_ids)s::uuid[], %(verdict)s, %(confidence)s, %(why)s, %(summary)s, %(meta)s)
returning id
"""


def insert_edit(conn: psycopg.Connection, cand: dict[str, Any], *, status: str, target: Path | None,
                sha_before: str | None, why: str | None = None, summary: str | None = None,
                meta: dict[str, Any] | None = None) -> uuid.UUID:
    conf = (cand.get("jev") or {}).get("confidence_mean")
    ids = [str(i) for i in cand.get("deviation_ids") or [] if _is_uuid(str(i))]
    row = conn.execute(_INSERT_SQL, {
        "status": status, "target_path": edits_targets.short(target) if target else None,
        "source": cand["instruction_source"], "rule_id": cand["rule_id"], "rule_title": cand.get("rule_title"),
        "sha_before": sha_before, "deviation_ids": ids, "verdict": cand.get("verdict"),
        "confidence": None if conf is None else max(0.0, min(1.0, float(conf))), "why": why, "summary": summary,
        "meta": Jsonb(meta or {}),
    }).fetchone()
    assert row is not None
    return row[0]


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False


def update_edit(conn: psycopg.Connection, edit_id: uuid.UUID, *, meta: dict[str, Any] | None = None,
                **fields: Any) -> None:
    sets = [f"{k} = %({k})s" for k in fields]
    params: dict[str, Any] = dict(fields)
    if meta:
        sets.append("meta = meta || %(meta)s")
        params["meta"] = Jsonb(meta)
    params["id"] = edit_id
    conn.execute(f"update edits set {', '.join(sets)} where id = %(id)s", params)


_LIST_SQL = """
select id, created_at, status, instruction_source, rule_id, rule_title, target_path, improve_id, summary,
       pushed_at, undo_requested_at, reverted_at, meta->>'kind' as kind, meta->>'kind_reason' as kind_reason,
       meta->>'kind_writer' as kind_writer, meta->>'kind_check' as kind_check
from edits where created_at >= %s order by created_at desc, id
"""


def list_edits(*, days: int = 28, now: datetime | None = None, db_url: str | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_LIST_SQL, (now - timedelta(days=days),))
        return cur.fetchall()


def list_line(r: dict[str, Any]) -> str:
    when = r["created_at"].astimezone().strftime("%Y-%m-%d %H:%M")
    rule = f"{r['instruction_source']} / {r['rule_title'] or r['rule_id']} [{r['rule_id']}]"
    extra = f" improve={r['improve_id']}" if r["improve_id"] else ""
    kind, writer = r.get("kind"), r.get("kind_writer")
    mismatch = bool(kind and writer and writer != kind)
    if kind:
        extra += f" kind={kind}" + (f" writer_kind={writer}" if mismatch else "")
    text = (f"{r['id']} {when} {r['status']} {rule} -> {r['target_path'] or '-'}{extra}"
            + (f": {r['summary']}" if r["summary"] else ""))
    reason = f"{r.get('kind_check')}; writer said {writer}" if mismatch and r.get("kind_check") else r.get("kind_reason")
    if kind and reason:
        text += f" ({kind}: {reason})"
    return text


# --------------------------------------------------------------------------- run


@dataclass
class Outcome:
    source: str
    rule_id: str | None
    status: str            # plan-edit | plan-suggest | skip | error | kept | trial | auto-reverted |
                           # refused | suggested | skipped | proposed
    reason: str = ""
    target: str = "-"
    edit_id: uuid.UUID | None = None
    improve_id: str | None = None
    rule_title: str | None = None
    summary: str | None = None
    why: str | None = None
    token: str | None = field(default=None, repr=False)   # kept/trial only; never stored, never printed
    push: str | None = None                               # phone | mac | off
    kind: str | None = None                               # contract | hop | prose (checked against the diff)
    kind_reason: str | None = None                        # the writer's reason, or the check's on a mismatch
    kind_writer: str | None = None                        # the writer's own label
    cost: str | None = None                               # `<tokens> tok/<active time>` of the deviating sessions

    def line(self) -> str:
        rule = f"{self.source} {self.rule_id or '-'}"
        head = {"plan-edit": "plan edit", "plan-suggest": "plan suggest"}.get(self.status, self.status)
        text = f"{head} {rule} -> {self.target}"
        if self.cost:
            text += f" cost={self.cost}"
        if self.edit_id:
            text += f" edit={self.edit_id}"
        if self.improve_id:
            text += f" improve={self.improve_id}"
        if self.kind:
            text += f" kind={self.kind}"
            if self.kind_writer and self.kind_writer != self.kind:
                text += f" writer_kind={self.kind_writer}"
        if self.push:
            text += f" push={self.push}"
        return text + (f": {self.reason}" if self.reason else "")


@dataclass
class Report:
    dry_run: bool = False
    outcomes: list[Outcome] = field(default_factory=list)
    deferred: int = 0       # candidates left for the next run (--limit)

    def count(self, *statuses: str) -> int:
        return sum(o.status in statuses for o in self.outcomes)

    @property
    def edits(self) -> int:
        """Edits tried: improve answered (kept/trial/auto-reverted/refused) or its answer was garbage."""
        return self.count("kept", "trial", "auto-reverted", "refused", "proposed")

    @property
    def kept(self) -> int:
        return self.count("kept", "trial")

    @property
    def reverted(self) -> int:
        return self.count("auto-reverted")

    @property
    def suggested(self) -> int:
        return self.count("suggested")

    @property
    def errors(self) -> int:
        return self.count("error", "proposed")

    def summary(self) -> str:
        if self.dry_run:
            return (f"edits (dry run, nothing called or written): plan_edit={self.count('plan-edit')} "
                    f"plan_suggest={self.count('plan-suggest')} skip={self.count('skip')}"
                    + (f" deferred={self.deferred}" if self.deferred else ""))
        return (f"edits: edits={self.edits} kept={self.kept} reverted={self.reverted} refused={self.count('refused')} "
                f"suggested={self.suggested} skipped={self.count('skipped')} errors={self.errors}"
                + (f" staged={self.count('staged')}" if self.count("staged") else "")
                + (f" deferred={self.deferred}" if self.deferred else ""))


@dataclass
class _Plan:
    cand: dict[str, Any]
    action: str                       # edit | suggest | skip
    reason: str = ""
    target: Path | None = None
    text: str | None = None           # the file's text (edit)
    sha: str | None = None


def _conf(cand: dict[str, Any]) -> float | None:
    v = (cand.get("jev") or {}).get("confidence_mean")
    return None if v is None else float(v)


def plan_candidate(conn: psycopg.Connection, cand: dict[str, Any], *, min_confidence: float,
                   now: datetime | None = None) -> _Plan | None:
    """None: not an edit candidate at all (verdict leave / unjudged). A rule with a kept or
    trial edit inside the cooldown window is skipped unless its scoreboard verdict is `more`."""
    if cand.get("verdict") not in VERDICTS:
        return None
    conf = _conf(cand)
    if conf is None or conf < min_confidence:
        return _Plan(cand, "skip", f"jev confidence {'-' if conf is None else f'{conf:.2f}'} < {min_confidence:.2f}")
    if cand.get("team") is True:
        return _Plan(cand, "skip", "team repo: never changed")
    rule_id = cand.get("rule_id")
    if not rule_id or rule_id == rule_catalog.UNMATCHED:
        return _Plan(cand, "skip", "no rule id (run `amem deviations reref`)")
    t = edits_targets.resolve(cand["instruction_source"], cand.get("target_paths"), rule_id)
    if t.action == edits_targets.SKIP:
        return _Plan(cand, "skip", t.reason)
    # improve refuses pinned files and rules; skip them here, before any writer call or row.
    pins = edits_targets.pins()
    pin_reason = edits_targets.pinned_file(t.path, pins)
    if pin_reason is not None:
        return _Plan(cand, "skip", f"file pinned ({pin_reason})", t.path)
    rule_pin = edits_targets.pinned_rule(improve_rule_id(cand["instruction_source"], rule_id), p=pins)
    if rule_pin is not None:
        return _Plan(cand, "skip", f"rule {rule_pin[0]} pinned ({rule_pin[1]})", t.path)
    text = sha = None
    if t.path is not None and t.path.is_file() and not t.path.name.lower().startswith(".env"):
        try:
            text = t.path.read_text(encoding="utf-8")
            sha = file_sha(text)
        except (OSError, UnicodeDecodeError):
            text = sha = None
    if handled(conn, cand["instruction_source"], rule_id, sha, list(cand.get("instruction_shas") or [])):
        return _Plan(cand, "skip", "already handled at this version", t.path)
    lifted = ""
    cd = scoreboard.active_cooldown(conn, cand["instruction_source"], rule_id, now=now)
    if cd is not None and cd.cooldown_until:
        if not cd.cooldown_lifted:
            return _Plan(cand, "skip", f"cooldown until {cd.cooldown_until[:10]} (last edit {cd.status} "
                                       f"{cd.edited_at[:10]}, scoreboard: {cd.verdict})", t.path)
        lifted = f"; cooldown lifted: scoreboard says more since the {cd.edited_at[:10]} edit"
    failed = recent_failed_apply(conn, cand["instruction_source"], rule_id, now=now)
    if failed is not None:
        until = (failed[0] + timedelta(days=scoreboard.cooldown_days())).astimezone(UTC).date().isoformat()
        return _Plan(cand, "skip", f"cooldown until {until} (improve apply failed {failed[0].date().isoformat()}: "
                                   f"{_one_line(failed[1], 120)})", t.path)
    if t.action == edits_targets.SUGGEST:
        return _Plan(cand, "suggest", t.reason + lifted, t.path, text, sha)
    if text is None:
        return _Plan(cand, "skip", "target file unreadable", t.path)
    try:
        section = edits_sections.section_text(text, rule_id)
    except edits_sections.SectionError as exc:
        return _Plan(cand, "skip", f"{exc} (the file changed since the scan)", t.path)
    rule_pin = edits_targets.pinned_rule("", section, pins)
    if rule_pin is not None:
        return _Plan(cand, "skip", f"rule {rule_pin[0]} pinned ({rule_pin[1]})", t.path)
    if deviations.redact(section) != section:
        return _Plan(cand, "skip", "the rule holds secret-shaped text: not sent to the writer", t.path)
    return _Plan(cand, "edit", f"{cand['verdict']} {conf:.2f}, {evidence(cand)}{lifted}", t.path, text, sha)


def _suggest(conn: psycopg.Connection, p: _Plan) -> Outcome:
    c = p.cand
    out = Outcome(c["instruction_source"], c["rule_id"], "error", target=edits_targets.short(p.target),
                  rule_title=c.get("rule_title"))
    title = f"Agent edit suggested: {c.get('rule_title') or c['rule_id']} ({c['instruction_source']})"
    ex = (c.get("examples") or [{}])[0].get("why") or ""
    why = _one_line(f"Jev {c['verdict']} ({_conf(c) or 0:.2f}): agents keep deviating from this rule. "
                    f"Not edited here: {p.reason}." + (f" Likely cause: {ex}" if ex else ""), 400)
    args = ["suggest", "--title", title, "--why", why, "--evidence", evidence(c)]
    if p.target is not None:
        args += ["--target", edits_targets.short(p.target)]
    try:
        proc = _improve(args, timeout=120)
    except ImproveError as exc:
        out.reason = str(exc)
        return out
    if proc.returncode != 0:
        out.reason = f"improve suggest exit {proc.returncode}: {_one_line(proc.stderr or proc.stdout, 160)}"
        return out
    out.edit_id = insert_edit(conn, c, status="suggested", target=p.target, sha_before=p.sha, why=why,
                              summary=_one_line(title, 140), meta={"reason": p.reason})
    out.status, out.reason, out.why = "suggested", p.reason, why
    return out


def _edit(db: _Db, p: _Plan, *, days: int, timeout: float) -> Outcome:
    c, conn = p.cand, db.conn
    assert p.target is not None and p.text is not None
    out = Outcome(c["instruction_source"], c["rule_id"], "error", target=edits_targets.short(p.target),
                  rule_title=c.get("rule_title"))
    section = edits_sections.find(p.text, c["rule_id"])
    prev, nxt = edits_sections.neighbours(p.text, c["rule_id"])
    prompt = edits_writer.writer_prompt({**c, "_days": days}, target=out.target, section=section,
                                        section_text=edits_sections.section_text(p.text, c["rule_id"]),
                                        prev=deviations.redact(prev), nxt=deviations.redact(nxt), days=days)
    try:
        envelope = edits_writer.call_writer(prompt, timeout=timeout)
        proposal = edits_writer.parse_proposal(distill.parse_envelope(envelope), file_text=p.text,
                                               rule_id=c["rule_id"], cand=c)
    except distill.WriterError as exc:
        out.reason = f"writer: {exc}"
        return out
    except edits_writer.NoChange:
        out.edit_id = insert_edit(conn, c, status="skipped", target=p.target, sha_before=p.sha,
                                  meta={"reason": "the writer proposed no change"})
        out.status, out.reason = "skipped", "the writer proposed no change"
        return out
    except (distill.DraftError, edits_writer.ProposalError, edits_sections.SectionError) as exc:
        out.reason = _one_line(f"writer output: {exc}")
        return out
    # The file must not have changed while the writer ran.
    try:
        if file_sha(p.target.read_text(encoding="utf-8")) != p.sha:
            out.reason = "the target file changed while the writer ran; retried next run"
            return out
    except OSError as exc:
        out.reason = f"target unreadable: {exc}"
        return out
    out.why, out.summary = proposal.why, proposal.summary
    out.kind, out.kind_reason, out.kind_writer = proposal.kind, proposal.kind_shown, proposal.kind_writer
    if stage_mode():
        return _stage_edit(db, p, proposal, out)
    edit_id = insert_edit(conn, c, status="proposed", target=p.target, sha_before=p.sha, why=proposal.why,
                          summary=proposal.summary,
                          meta={"topic": proposal.topic, "kind": proposal.kind, "kind_reason": proposal.kind_reason,
                                "kind_writer": proposal.kind_writer, "kind_check": proposal.kind_check,
                                "kind_mismatch": proposal.kind_mismatch, "writer_model": edits_writer.writer_model()})
    conn.commit()   # crash-safe: the row exists before improve touches the file
    out.edit_id = edit_id
    fd, tmp = tempfile.mkstemp(prefix="amem-edit-", suffix=p.target.suffix or ".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(proposal.new_text)
        proc = _improve(["apply", "--target", str(p.target), "--rule-id", improve_rule_id(c["instruction_source"],
                         c["rule_id"]), "--new-file", tmp, "--topic", proposal.topic, "--why", proposal.why,
                         "--evidence", evidence(c), "--json"])
        result = parse_apply(proc)
    except Exception as exc:  # timeout, kill, garbage reply: improve may have written the target already
        err = str(exc) if isinstance(exc, ImproveError) else f"{type(exc).__name__}: {exc}"
        restore, restore_note = restore_target(p.target, p.text, proposal.new_text)
        meta: dict[str, Any] = {"error": _one_line(err), "apply_failed": True, "restore": restore}
        if restore_note:
            meta["restore_note"] = _one_line(restore_note)
        out.status = "proposed"     # counts as an error (Report.errors), so the daily step fails
        out.reason = _one_line(f"improve: {err}; target {restore}" + (f" ({restore_note})" if restore_note else ""))
        if restore == RESTORE_FAILED:
            _loud(f"edits: {edit_id} {out.target}: improve apply failed and the target was NOT restored: "
                  f"{restore_note}. Check the file by hand.")
        db_err = _persist(db, lambda conn: update_edit(conn, edit_id, meta=meta))
        if db_err:
            out.reason = _one_line(f"{out.reason}; edit row not updated: {db_err}")
            _loud(f"edits: {edit_id}: could not record the failed apply: {db_err}")
        return out
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    fields: dict[str, Any] = {"status": result.status, "improve_id": result.id}
    if result.status in ("kept", "trial"):
        try:
            fields["instruction_sha_after"] = file_sha(p.target.read_text(encoding="utf-8"))
        except OSError:
            fields["instruction_sha_after"] = file_sha(proposal.new_text)
    upd_meta = {"improve": {"reason": result.reason, "revert": result.revert}}
    # improve may have run for an hour: the connection can be dead by now. The result must survive.
    db_err = _persist(db, lambda conn: update_edit(conn, edit_id, meta=upd_meta, **fields))
    if db_err:
        undo = result.revert or (f"improve revert {result.id}" if result.id else "nothing to revert")
        out.status, out.improve_id = "error", result.id
        out.reason = _one_line(f"improve {result.id} {result.status} but edit row {edit_id} not updated "
                               f"({db_err}); no undo button: undo by hand with `{undo}`")
        _loud(f"edits: {out.reason}")
        return out
    out.status, out.improve_id, out.reason = result.status, result.id, result.reason or proposal.summary
    return out


def _stage_edit(db: _Db, p: _Plan, proposal: Any, out: Outcome) -> Outcome:
    """Approval-first: a baseline of the target, `improve stage` (artifact + static evaluation), a proposal
    row. The production file is only read. Promotion needs a human decision and the sandbox evaluation."""
    from agent_memory import rsi_approval, rsi_targets   # local import: rsi_approval imports this module

    c, conn = p.cand, db.conn
    assert p.target is not None
    try:
        bid, bdir = rsi_targets.create_baseline([p.target])
    except (rsi_targets.BaselineError, OSError) as exc:
        out.reason = _one_line(f"stage: baseline refused: {exc}")
        return out
    key = rsi_targets.tilde(p.target)
    exp = f"exp-{datetime.now(UTC):%Y%m%d}-edit-{file_sha(key + c['rule_id'])[:8]}"
    fd, tmp = tempfile.mkstemp(prefix="amem-stage-", suffix=p.target.suffix or ".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(proposal.new_text)
        rid = improve_rule_id(c["instruction_source"], c["rule_id"])
        proc = _improve(["stage", "--json", "--experiment", exp, "--baseline", str(bdir), "--change", f"{key}={tmp}",
                         "--rule-id", f"{key}={rid}", "--kind", "edit_instruction"])
    except Exception as exc:  # noqa: BLE001 - staging never writes production; any failure is this candidate's error
        out.reason = _one_line(f"improve stage: {type(exc).__name__}: {exc}")
        return out
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    res = next((json.loads(ln) for ln in reversed(proc.stdout.strip().splitlines()) if ln.startswith("{")), {})
    if res.get("status") != "staged":
        out.status, out.reason = "refused", _one_line(f"improve stage refused: {res.get('reason') or proc.returncode}")
        insert_edit(conn, c, status="refused", target=p.target, sha_before=p.sha, why=proposal.why,
                    summary=proposal.summary, meta={"mode": "stage", "stage": res})
        return out
    h = res["artifact_hash"]
    pid = rsi_approval.create_proposal(
        conn, Path(res["artifact_dir"]), title=f"Edit {c.get('rule_title') or c['rule_id']}", summary=proposal.summary,
        rationale={"why": proposal.why, "tests": "static checks passed; sandbox replay pending", "kind": proposal.kind},
        risk={"level": "low" if p.target.suffix == ".md" else "medium"}, confidence=_conf(c),
        expected={"effect": f"fewer deviations on {c['instruction_source']} / {c['rule_id']}"},
        evidence_refs=[f"amem:deviation/{d}" for d in (c.get("deviation_ids") or [])][:20])
    out.edit_id = insert_edit(conn, c, status="staged", target=p.target, sha_before=p.sha, why=proposal.why,
                              summary=proposal.summary,
                              meta={"mode": "stage", "proposal_id": pid, "artifact_hash": h, "baseline_id": bid,
                                    "experiment_id": exp, "kind": proposal.kind, "writer_model": edits_writer.writer_model()})
    out.status, out.reason = "staged", f"staged as proposal {pid} (artifact {h[:12]}); awaiting a human decision"
    return out


RESTORED = "restored"
RESTORE_UNCHANGED = "unchanged"
RESTORE_FAILED = "restore_failed"
PERSIST_TRIES = 3
PERSIST_DELAY = 2.0     # seconds, times the attempt number; tests set 0


def _loud(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def restore_target(target: Path, original: str, new_text: str) -> tuple[str, str]:
    """After a failed `improve apply`: put the original text back. Only overwrite the file when
    it holds exactly the original (nothing to do) or the proposed text (improve's own write),
    so a concurrent edit by someone else is never clobbered. (status, note)."""
    try:
        now_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return RESTORE_FAILED, f"target unreadable: {type(exc).__name__}: {exc}"
    if now_text == original:
        return RESTORE_UNCHANGED, ""
    if now_text != new_text:
        return RESTORE_FAILED, "target holds text that is neither the original nor the proposal"
    try:
        target.write_text(original, encoding="utf-8")
        back = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return RESTORE_FAILED, f"write failed: {type(exc).__name__}: {exc}"
    if back != original:
        return RESTORE_FAILED, "the restored file does not read back as the original"
    return RESTORED, ""


class _Db:
    """The run's connection, replaceable: a long improve call can outlive it (pooler idle timeout)."""

    def __init__(self, db_url: str | None) -> None:
        self.url = db_url
        self.conn = store.connect(db_url)

    def reconnect(self) -> None:
        self.close()
        self.conn = store.connect(self.url)

    def ensure(self) -> None:
        if self.conn.closed or self.conn.broken:
            self.reconnect()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - a dead connection may fail to close
            pass


def _persist(db: _Db, write: Any) -> str | None:
    """Run `write(conn)` and commit, reconnecting and retrying on a database error. None on
    success, else the last error (the caller logs it loudly)."""
    last: Exception | None = None
    for attempt in range(PERSIST_TRIES):
        try:
            if attempt:
                time.sleep(PERSIST_DELAY * attempt)
                db.reconnect()
            else:
                db.ensure()
            write(db.conn)
            db.conn.commit()
            return None
        except psycopg.Error as exc:
            last = exc
            try:
                db.conn.rollback()
            except psycopg.Error:
                pass
    return _one_line(f"{type(last).__name__}: {last}", 160)


def recent_failed_apply(conn: psycopg.Connection, source: str, rule_id: str, *,
                        now: datetime | None = None) -> tuple[datetime, str] | None:
    """The newest failed `improve apply` on this rule inside the cooldown window: (when, error)."""
    days = scoreboard.cooldown_days()
    if days <= 0:
        return None
    now = now or datetime.now(UTC)
    row = conn.execute("select created_at, meta->>'error' from edits where instruction_source = %s and rule_id = %s "
                       "and status = 'proposed' and meta->>'apply_failed' = 'true' and created_at > %s "
                       "order by created_at desc limit 1", (source, rule_id, now - timedelta(days=days))).fetchone()
    return None if row is None else (row[0], row[1] or "")


def run(*, days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT, min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        dry_run: bool = False, push: bool = True, timeout: float = edits_writer.DEFAULT_TIMEOUT,
        now: datetime | None = None, notify: dict[str, str] | None = None, db_url: str | None = None) -> Report:
    """Plan and (unless dry_run) apply edits. Raises only before the loop (the candidate
    query). Each candidate's failure is that candidate's `error` outcome. The connection is
    replaced when it dies (a long improve call can outlive it), so later candidates still run."""
    from agent_memory import edits_push  # edits_push imports daily, which imports this module lazily

    report = Report(dry_run=dry_run)
    cands = ordered(deviations.candidates(days=days, now=now, db_url=db_url))
    acted = 0

    def add(o: Outcome, cand: dict[str, Any]) -> None:
        o.cost = cost_text(cand)
        report.outcomes.append(o)

    db = _Db(db_url)
    try:
        for cand in cands:
            try:
                db.ensure()
                p = plan_candidate(db.conn, cand, min_confidence=min_confidence, now=now)
            except Exception as exc:  # never block: this candidate's error
                add(Outcome(cand.get("instruction_source", "?"), cand.get("rule_id"), "error",
                            _one_line(f"plan: {type(exc).__name__}: {exc}")), cand)
                _rollback(db)
                continue
            if p is None:
                continue
            if p.action == "skip":
                add(Outcome(cand["instruction_source"], cand.get("rule_id"), "skip", p.reason,
                            edits_targets.short(p.target)), cand)
                continue
            if acted >= limit:
                report.deferred += 1
                continue
            acted += 1
            if dry_run:
                add(Outcome(cand["instruction_source"], cand["rule_id"], f"plan-{p.action}",
                            p.reason, edits_targets.short(p.target)), cand)
                continue
            try:
                o = _suggest(db.conn, p) if p.action == "suggest" else _edit(db, p, days=days, timeout=timeout)
                if not (db.conn.closed or db.conn.broken):   # else _edit already said loudly what was lost
                    db.conn.commit()
            except Exception as exc:  # DB or anything unexpected: this candidate only
                _rollback(db)
                o = Outcome(cand["instruction_source"], cand["rule_id"], "error",
                            _one_line(f"{type(exc).__name__}: {exc}"), edits_targets.short(p.target))
            if o.status in ("kept", "trial") and o.edit_id is not None:
                try:
                    o.push = edits_push.push_new_edit(db.conn, o, push=push, notify=notify)
                    db.conn.commit()
                except Exception as exc:  # a failed push never undoes the edit
                    _rollback(db)
                    o.push = "error"
                    o.reason = _one_line(f"{o.reason}; push: {type(exc).__name__}")
            add(o, cand)
    finally:
        db.close()
    return report


def _rollback(db: _Db) -> None:
    try:
        db.conn.rollback()
    except psycopg.Error:
        pass
