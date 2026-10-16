"""RSI V1 approval-first: proposals, decision tokens, decisions, the single promotion worker, the morning brief
push, durable candidates, shared evidence and job checkpoints (docs/rsi/approval.md).

Only `decide_poll` causes production changes, and only by running agent-config's `improve promote` /
`improve revert-change` for a decision row that a human made (phone token via the edge function amem-decide,
or the TTY-only CLI). `improve promote` re-confirms the decision here (`verify_decision`) and refuses inside
agent sessions. The edge function records decisions only.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import config, edits, edits_push, redact, rsi_artifact as RA, rsi_targets, store

DEFAULT_SUITES = ("static", "sandbox-replay")
TOKEN_TTL = timedelta(hours=72)
DECISION_TTL = timedelta(hours=24)
IMPROVE_TIMEOUT = 600.0
MAX_ATTEMPTS = 3
AGENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "RSI_SANDBOX", "IMPROVE_SESSION")


def agent_session_reason(environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    for k in AGENT_ENV:
        if env.get(k):
            return f"{k} is set"
    for k, v in env.items():
        if k.startswith("CODEX_") and k != "CODEX_HOME" and v:
            return f"{k} is set"
    return None


def required_suites() -> list[str]:
    try:
        crit = json.loads((rsi_targets.agent_config_dir() / "improve" / "criteria.json").read_text())
        return list((crit.get("promotion") or {}).get("required_suites") or DEFAULT_SUITES)
    except (OSError, ValueError):
        return list(DEFAULT_SUITES)


def token_hash(token: str, proposal_id: str, artifact_hash: str, action: str) -> str:
    """Same binding as the SQL function amem_rsi_record_decision."""
    return RA.sha256_bytes(f"{token}:{proposal_id}:{artifact_hash}:{action}".encode())


# -- artifacts, proposals, tokens -------------------------------------------------------------------
def register_artifact(conn: psycopg.Connection, adir: Path) -> str:
    """Insert the exact tested bytes (idempotent) and its evaluation records. Returns the hash."""
    m, h, after = RA.load_artifact(adir)
    conn.execute(
        "insert into rsi_artifacts (hash, manifest_canonical, manifest, files, experiment_id, baseline_id) "
        "values (%s, %s, %s, %s, %s, %s) on conflict (hash) do nothing",
        (h, RA.canonical(m).decode(), Jsonb(m), Jsonb({k: v.decode() for k, v in after.items()}),
         m.get("experiment_id"), m["baseline_id"]))
    for rec in RA.eval_records(adir):
        if rec.get("artifact_hash") != h:
            continue
        conn.execute(
            "insert into rsi_eval_results (artifact_hash, experiment_id, baseline_id, suite, suite_version, model, effort, "
            "verdict, record, record_sha) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
            (h, rec.get("experiment_id"), rec.get("baseline_id"), rec.get("suite"), str(rec.get("suite_version", "1")),
             rec.get("model"), rec.get("effort"), rec.get("verdict"), Jsonb(rec), RA.sha256_bytes(RA.canonical(rec))))
    return h


def create_proposal(conn: psycopg.Connection, adir: Path, *, title: str, summary: str = "", rationale: dict | None = None,
                    evidence_refs: list[str] | None = None, risk: dict | None = None, confidence: float | None = None,
                    expected: dict | None = None, candidate_id: str | None = None,
                    ttl: timedelta = TOKEN_TTL) -> str:
    h = register_artifact(conn, adir)
    m = json.loads((adir / "manifest.json").read_text())
    row = conn.execute(
        "insert into rsi_proposals (artifact_hash, experiment_id, candidate_id, title, summary, rationale, evidence_refs, "
        "risk, confidence, expected, targets, expires_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now() + %s) returning id",
        (h, m.get("experiment_id"), candidate_id, redact.redact(title)[:200], redact.redact(summary)[:2000],
         Jsonb(rationale or {}), evidence_refs or [], Jsonb(risk) if risk else None, confidence,
         Jsonb(expected) if expected else None, [f["path"] for f in m["files"]], ttl)).fetchone()
    return str(row[0])


def issue_tokens(conn: psycopg.Connection, proposal_id: str, artifact_hash: str,
                 actions: tuple[str, ...] = ("apply", "reject"), ttl: timedelta = TOKEN_TTL) -> dict[str, str]:
    """Fresh tokens for the push. Only their bound hashes are stored. A used token is never re-armed."""
    out = {}
    for a in actions:
        tok = secrets.token_urlsafe(24)
        armed = conn.execute("select amem_rsi_arm_token(%s, %s, %s, %s)",       # insert, or re-arm while unused
                             (proposal_id, a, token_hash(tok, proposal_id, artifact_hash, a), ttl)).fetchone()[0]
        if armed:
            out[a] = tok
    return out


def record_phone_decision(conn: psycopg.Connection, proposal_id: str, action: str, artifact_hash: str, token: str,
                          totp_step: int | None = None, *, phone: bool = False) -> str:
    """What the edge function does (tests and the local stack). phone=True calls the TOTP-gated function the
    deployed edge function uses (Apply / Undo need a verified step); otherwise the owner-only token function."""
    if phone:
        return conn.execute("select amem_rsi_record_phone_decision(%s, %s, %s, %s, %s)",
                            (proposal_id, action, artifact_hash, token, totp_step)).fetchone()[0]
    return conn.execute("select amem_rsi_record_decision(%s, %s, %s, %s)",
                        (proposal_id, action, artifact_hash, token)).fetchone()[0]


class NotHuman(Exception):
    pass


def record_cli_decision(conn: psycopg.Connection, proposal_id: str, action: str, artifact_hash: str, *,
                        reason: str | None = None, stdin_isatty: bool | None = None,
                        environ: dict[str, str] | None = None) -> str:
    """Vilmer's terminal only: a TTY, outside every agent session. The caller has asked him to confirm."""
    why = agent_session_reason(environ)
    tty = sys.stdin.isatty() if stdin_isatty is None else stdin_isatty
    if why or not tty:
        raise NotHuman(f"refused: decisions come from Vilmer's terminal or phone, not from {why or 'a non-interactive process'}")
    return conn.execute("select amem_rsi_record_cli_decision(%s, %s, %s, %s)",
                        (proposal_id, action, artifact_hash, redact.redact(reason or "")[:500] or None)).fetchone()[0]


def verify_decision(conn: psycopg.Connection, d: dict[str, Any], now: datetime | None = None) -> tuple[bool, str]:
    """`improve promote` asks this: the decision file must match a stored, unexpired apply decision for the
    proposal's exact artifact, and the proposal must be approved or promoting."""
    if not isinstance(d, dict) or d.get("format") != RA.DECISION_FORMAT or d.get("action") != "apply":
        return False, "not an apply decision"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select d.*, p.status as p_status, p.artifact_hash as p_hash from rsi_decisions d "
                    "join rsi_proposals p on p.id = d.proposal_id where d.id::text = %s", (str(d.get("decision_id")),))
        row = cur.fetchone()
    if row is None:
        return False, "no such decision row"
    if str(row["proposal_id"]) != str(d.get("proposal_id")) or row["action"] != "apply":
        return False, "decision row is for another proposal or action"
    if not (row["artifact_hash"] == row["p_hash"] == d.get("artifact_hash")):
        return False, "artifact hash differs between the decision, the proposal and the file"
    if row["expires_at"] <= (now or datetime.now(UTC)):
        return False, "decision expired"
    if row["p_status"] not in ("approved", "promoting"):
        return False, f"proposal is {row['p_status']}"
    if row["verified"] != d.get("verified") or row["source"] != d.get("source"):
        return False, "decision source differs from the stored row"
    return True, "ok"


# -- the promotion worker ---------------------------------------------------------------------------
@dataclass
class PollReport:
    handled: list[dict[str, Any]] = field(default_factory=list)
    expired: int = 0
    stale: int = 0
    busy: bool = False
    recover: list[dict[str, Any]] = field(default_factory=list)

    def line(self) -> str:
        if self.busy:
            return "rsi decide-poll: busy (another worker holds the lock)"
        res = {}
        for h in self.handled:
            res[h["result"]] = res.get(h["result"], 0) + 1
        bits = " ".join(f"{k}={v}" for k, v in sorted(res.items())) or "nothing to do"
        return f"rsi decide-poll: {bits} expired={self.expired} stale={self.stale}"


@contextmanager
def worker_lock() -> Iterator[bool]:
    """One promotion worker at a time on this Mac (flock); the DB advisory lock covers other machines."""
    run = RA.sandbox_dir() / "run"
    run.mkdir(parents=True, exist_ok=True)
    fd = os.open(run / "rsi-decide.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _improve(args: list[str], timeout: float = IMPROVE_TIMEOUT) -> tuple[int, dict[str, Any], str]:
    try:
        p = edits.run_command([edits.improve_bin(), *args], timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, {}, "timeout"
    except OSError as exc:
        return 127, {}, type(exc).__name__
    res = next((json.loads(ln) for ln in reversed(p.stdout.strip().splitlines()) if ln.startswith("{")), {})
    return p.returncode, res, (p.stderr or "")[-300:]


def housekeeping(conn: psycopg.Connection) -> tuple[int, int]:
    """Expire staged proposals past their TTL; mark staged proposals whose live files moved as stale."""
    exp = conn.execute("update rsi_proposals set status = 'expired' where status = 'staged' and expires_at <= now()").rowcount
    stale = 0
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select p.id, a.manifest from rsi_proposals p join rsi_artifacts a on a.hash = p.artifact_hash "
                    "where p.status = 'staged'")
        rows = cur.fetchall()
    for r in rows:
        if rsi_targets.artifact_staleness(r["manifest"]) == "stale":
            conn.execute("update rsi_proposals set status = 'stale', result = 'live files changed after staging' where id = %s",
                         (r["id"],))
            stale += 1
    return exp, stale


_PENDING_SQL = """
select d.*, p.status as p_status, p.artifact_hash as p_hash, p.change_id, p.title,
       coalesce(r.attempts, 0) as attempts
from rsi_decisions d
join rsi_proposals p on p.id = d.proposal_id
left join rsi_decision_results r on r.decision_id = d.id
where r.decision_id is null or (r.handled_at is null and r.attempts < %(max)s)
order by d.decided_at, d.id
"""


def _result(conn: psycopg.Connection, decision_id: Any, result: str, *, handled: bool, change_id: str | None = None,
            detail: str = "") -> None:
    conn.execute(
        "insert into rsi_decision_results (decision_id, result, change_id, detail, attempts, handled_at) "
        "values (%s, %s, %s, %s, 1, case when %s then now() end) on conflict (decision_id) do update set "
        "result = excluded.result, change_id = coalesce(excluded.change_id, rsi_decision_results.change_id), "
        "detail = excluded.detail, attempts = rsi_decision_results.attempts + 1, handled_at = excluded.handled_at",
        (decision_id, result, change_id, redact.redact(detail)[:500], handled))


def _apply(conn: psycopg.Connection, d: dict[str, Any], push: bool, notify: dict[str, str] | None) -> dict[str, Any]:
    h = d["artifact_hash"]
    if d["p_hash"] != h:
        _result(conn, d["id"], "refused", handled=True, detail="decision hash differs from the proposal")
        return {"decision": str(d["id"]), "result": "refused"}
    if d["expires_at"] <= datetime.now(UTC):
        _result(conn, d["id"], "expired", handled=True, detail="decision expired before promotion")
        conn.execute("update rsi_proposals set status = 'expired' where id = %s and status = 'approved'", (d["proposal_id"],))
        return {"decision": str(d["id"]), "result": "expired"}
    adir = RA.artifacts_dir() / h
    try:
        m, h2, after = RA.load_artifact(adir)
        db = conn.execute("select files from rsi_artifacts where hash = %s", (h,)).fetchone()
        same = h2 == h and db is not None and {k: v.decode() for k, v in after.items()} == db[0]
    except RA.ArtifactError as exc:
        same, why = False, str(exc)
    else:
        why = "local artifact bytes differ from the stored artifact"
    if not same:
        _result(conn, d["id"], "refused", handled=True, detail=why)
        conn.execute("update rsi_proposals set status = 'failed', result = %s where id = %s", (why[:300], d["proposal_id"]))
        return {"decision": str(d["id"]), "result": "refused"}
    decision = {"format": RA.DECISION_FORMAT, "decision_id": str(d["id"]), "proposal_id": str(d["proposal_id"]),
                "action": "apply", "artifact_hash": h, "decided_at": d["decided_at"].isoformat(),
                "expires_at": d["expires_at"].isoformat(), "source": d["source"], "verified": d["verified"]}
    conn.execute("update rsi_proposals set status = 'promoting' where id = %s and status = 'approved'", (d["proposal_id"],))
    conn.commit()                                          # improve promote re-reads the decision through amem
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(decision, fh)
        dfile = fh.name
    os.chmod(dfile, 0o600)
    try:
        code, res, err = _improve(["promote", "--json", "--artifact", h, "--expect-hash", h, "--decision", dfile])
    finally:
        Path(dfile).unlink(missing_ok=True)
    status = res.get("status")
    if status in ("promoted", "already-promoted"):
        conn.execute("update rsi_proposals set status = 'promoted', change_id = %s, promoted_at = coalesce(promoted_at, now()), "
                     "result = %s where id = %s", (res.get("id"), status, d["proposal_id"]))
        _result(conn, d["id"], "promoted", handled=True, change_id=res.get("id"))
        _notify_applied(conn, push, notify, d, res.get("id"))
        return {"decision": str(d["id"]), "result": "promoted", "change_id": res.get("id")}
    if code == 75 or "busy" in (res.get("reason") or "") or code in (124, 127):
        conn.execute("update rsi_proposals set status = 'approved' where id = %s and status = 'promoting'", (d["proposal_id"],))
        _result(conn, d["id"], "retry", handled=d["attempts"] + 1 >= MAX_ATTEMPTS, detail=res.get("reason") or err)
        return {"decision": str(d["id"]), "result": "retry"}
    reason = res.get("reason") or err or f"improve exit {code}"
    new_status = "stale" if "stale baseline" in reason else "failed"
    conn.execute("update rsi_proposals set status = %s, result = %s where id = %s", (new_status, reason[:300], d["proposal_id"]))
    _result(conn, d["id"], "refused", handled=True, detail=reason)
    _notify(push, notify, f"Not applied: {d['title']}", f"Promotion refused: {redact.redact(reason)[:300]}")
    return {"decision": str(d["id"]), "result": "refused", "reason": reason}


def _undo(conn: psycopg.Connection, d: dict[str, Any], push: bool, notify: dict[str, str] | None) -> dict[str, Any]:
    if d["p_status"] != "promoted" or not d["change_id"]:
        _result(conn, d["id"], "nothing", handled=True, detail=f"proposal is {d['p_status']}")
        return {"decision": str(d["id"]), "result": "nothing"}
    code, res, err = _improve(["revert-change", d["change_id"], "--json", "--reason", "phone undo" if d["source"] == "phone" else "cli undo"])
    status = res.get("status")
    if status in ("reverted", "already-reverted"):
        conn.execute("update rsi_proposals set status = 'reverted', reverted_at = coalesce(reverted_at, now()) where id = %s",
                     (d["proposal_id"],))
        _result(conn, d["id"], "reverted", handled=True, change_id=d["change_id"])
        _notify(push, notify, f"Undone: {d['title']}", f"{d['change_id']} reverted to the previous version.")
        return {"decision": str(d["id"]), "result": "reverted"}
    if status == "conflict":
        _result(conn, d["id"], "conflict", handled=True, detail="; ".join(res.get("conflicts") or []))
        _notify(push, notify, f"Undo conflict: {d['title']}", "A file changed after promotion; nothing was overwritten.")
        return {"decision": str(d["id"]), "result": "conflict"}
    _result(conn, d["id"], "retry", handled=d["attempts"] + 1 >= MAX_ATTEMPTS, detail=res.get("reason") or err)
    return {"decision": str(d["id"]), "result": "retry"}


def _notify_applied(conn: psycopg.Connection, push: bool, notify: dict[str, str] | None, d: dict[str, Any],
                    change_id: str | None) -> None:
    """'Applied' push. With a decide page: an Undo button (TOTP-gated, its own token). Else: the terminal command."""
    title, body = f"Applied: {d['title']}", f"Promoted as {change_id}."
    page = decide_page()
    if not push:
        return
    if not page:
        edits_push.push_note(title, body + f" Undo: `amem rsi decide {d['proposal_id']} undo` in your terminal.", notify)
        return
    tok = issue_tokens(conn, str(d["proposal_id"]), d["artifact_hash"], ("undo",))
    p = {"id": d["proposal_id"], "artifact_hash": d["artifact_hash"], "title": d["title"]}
    actions = ([{"action": "view", "label": "Undo", "url": page_link(page, p, "undo", tok["undo"]), "clear": True}]
               if "undo" in tok else [])
    edits_push._publish(lambda topic: {"topic": topic, "title": title, "message": body, "priority": 3,
                                       "tags": ["white_check_mark"], **({"actions": actions} if actions else {})},
                        title, body, notify)


def _notify(push: bool, notify: dict[str, str] | None, title: str, body: str) -> None:
    if push:
        edits_push.push_note(title, body, notify)


def decide_poll(*, db_url: str | None = None, push: bool = True, notify: dict[str, str] | None = None) -> PollReport:
    """Handle every pending decision once. The only path that promotes or reverts RSI changes."""
    rep = PollReport()
    with worker_lock() as got:
        if not got:
            rep.busy = True
            return rep
        _, rec, _ = _improve(["recover", "--json"], timeout=120)
        rep.recover = rec.get("outcomes") or []
        with store.connect(db_url) as conn:
            if not conn.execute("select pg_try_advisory_lock(hashtext('amem_rsi_decide'))").fetchone()[0]:
                rep.busy = True
                return rep
            try:
                rep.expired, rep.stale = housekeeping(conn)
                conn.commit()
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(_PENDING_SQL, {"max": MAX_ATTEMPTS})
                    pending = cur.fetchall()
                for d in pending:
                    if d["action"] == "reject":
                        _result(conn, d["id"], "rejected", handled=True)
                        out = {"decision": str(d["id"]), "result": "rejected"}
                    elif d["action"] == "apply":
                        out = _apply(conn, d, push, notify)
                    else:
                        out = _undo(conn, d, push, notify)
                    conn.commit()
                    rep.handled.append(out)
            finally:
                conn.execute("select pg_advisory_unlock(hashtext('amem_rsi_decide'))")
    return rep


# -- the morning brief push ------------------------------------------------------------------------------
def decide_url() -> str | None:
    config.load_env()
    if os.environ.get("AMEM_RSI_DECIDE_URL"):
        return os.environ["AMEM_RSI_DECIDE_URL"].strip()
    base = edits_push.project_url()
    return f"{base}/functions/v1/amem-decide" if base else None


def decide_page() -> str | None:
    """The static decide page (docs/rsi/least-privilege.md, A3) where Apply needs a TOTP code. None: no phone Apply."""
    config.load_env()
    v = os.environ.get("AMEM_RSI_DECIDE_PAGE", "").strip()
    return v if v.startswith("https://") else None


PAGE_TEMPLATE = config.REPO_ROOT / "web" / "rsi-decide" / "index.html"


def render_decide_page(url: str) -> str:
    """The static decide page with its one allowed endpoint filled in (CSP connect-src + fetch target)."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise ValueError("the decide URL must be a plain https:// URL")
    origin = f"https://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    html = PAGE_TEMPLATE.read_text(encoding="utf-8")
    return html.replace("__AMEM_DECIDE_ORIGIN__", origin).replace("__AMEM_DECIDE_URL__", url)


def page_link(page: str, p: dict[str, Any], action: str, token: str) -> str:
    """Everything after `#`: the browser never sends the fragment to a server, so the token stays out of logs."""
    from urllib.parse import urlencode
    frag = urlencode({"p": str(p["id"]), "h": p["artifact_hash"], "a": action, "t": token, "n": str(p.get("title") or "")[:80]})
    return f"{page.split('#')[0]}#{frag}"


def proposal_payload(topic: str, p: dict[str, Any], tokens: dict[str, str], url: str | None,
                     page: str | None = None) -> dict[str, Any]:
    """One ntfy message per proposal: what, why, tests, risk, files, expected. Reject is one tap (it can only
    stop a change). Apply opens the decide page, which needs the TOTP code; without a page, Apply is the
    terminal only (`amem rsi decide <id> apply`)."""
    lines = [p.get("summary") or "", f"Why: {(p.get('rationale') or {}).get('why', '')}".rstrip(": "),
             f"Tests: {(p.get('rationale') or {}).get('tests', 'see brief')}",
             f"Risk: {(p.get('risk') or {}).get('level', '?')} · confidence {p.get('confidence') if p.get('confidence') is not None else '?'}",
             f"Files: {', '.join(Path(t).name for t in p.get('targets') or [])}",
             f"Expected: {(p.get('expected') or {}).get('effect', '?')}",
             f"Artifact {p['artifact_hash'][:12]} · expires {str(p.get('expires_at'))[:16]}"]
    payload: dict[str, Any] = {"topic": topic, "title": f"Proposal: {p['title']}"[:120],
                               "message": redact.redact("\n".join(ln for ln in lines if ln.strip()))[:3500],
                               "priority": 3, "tags": ["test_tube"]}
    actions: list[dict[str, Any]] = []
    if page and "apply" in tokens:
        actions.append({"action": "view", "label": "Review & apply", "url": page_link(page, p, "apply", tokens["apply"]),
                        "clear": True})
    if url and "reject" in tokens:
        actions.append({"action": "http", "label": "Reject", "url": url, "method": "POST",
                        "headers": {"Content-Type": "application/json"},
                        "body": json.dumps({"proposal_id": str(p["id"]), "artifact_hash": p["artifact_hash"],
                                            "action": "reject", "token": tokens["reject"]}), "clear": True})
    if actions:
        payload["actions"] = actions
    if not page:
        payload["message"] = (payload["message"] + f"\nApply: run `amem rsi decide {p['id']} apply` in your terminal.")[:3800]
    return payload


def push_proposals(*, db_url: str | None = None, push: bool = True, notify: dict[str, str] | None = None) -> list[str]:
    """For each staged proposal without live tokens: new tokens + one push. Returns the push results."""
    out = []
    url, page = decide_url(), decide_page()
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from rsi_proposals p where status = 'staged' and expires_at > now() and not exists "
                    "(select 1 from rsi_decision_tokens t where t.proposal_id = p.id and t.expires_at > now()) order by created_at")
        for p in cur.fetchall():
            tokens = issue_tokens(conn, str(p["id"]), p["artifact_hash"])
            if not push:
                out.append("off")
                continue
            res = edits_push._publish(lambda topic, p=p, tokens=tokens: proposal_payload(topic, p, tokens, url, page),
                                      f"Proposal: {p['title']}", f"amem rsi show {p['id']}", notify)
            conn.execute("update rsi_proposals set meta = meta || %s where id = %s", (Jsonb({"push": res}), p["id"]))
            out.append(res)
    return out


# -- durable candidates, shared evidence, checkpoints -----------------------------------------------------------
def upsert_candidate(conn: psycopg.Connection, *, dedupe_key: str, source: str, kind: str, target: str | None = None,
                     rule_key: str | None = None, evidence_refs: list[str] | None = None,
                     metrics: dict | None = None) -> str:
    """A sighting: new candidates start `new`; known ones keep their status and history and get the new evidence."""
    row = conn.execute(
        "insert into rsi_candidates (dedupe_key, source, kind, target, rule_key, evidence_refs, metrics) "
        "values (%s,%s,%s,%s,%s,%s,%s) on conflict (dedupe_key) do update set last_seen_at = now(), "
        "seen_count = rsi_candidates.seen_count + 1, metrics = excluded.metrics, "
        "evidence_refs = (select array(select distinct unnest(rsi_candidates.evidence_refs || excluded.evidence_refs))), "
        "status = case when rsi_candidates.status = 'retired' then 'new' else rsi_candidates.status end returning id",
        (dedupe_key, source, kind, target, rule_key, evidence_refs or [], Jsonb(metrics or {}))).fetchone()
    return str(row[0])


def add_evidence(conn: psycopg.Connection, *, system: str, kind: str, subject_refs: list[str], summary: str = "",
                 metrics: dict | None = None, external_ref: str | None = None, valid_days: int | None = None) -> str:
    row = conn.execute(
        "insert into rsi_evidence (system, kind, subject_refs, summary, metrics, external_ref, valid_until) "
        "values (%s,%s,%s,%s,%s,%s, case when %s::int is null then null else now() + make_interval(days => %s::int) end) "
        "on conflict (system, external_ref) do update set metrics = excluded.metrics, summary = excluded.summary, "
        "observed_at = now() returning id",
        (system, kind, subject_refs, redact.redact(summary)[:2000] or None, Jsonb(metrics or {}), external_ref,
         valid_days, valid_days)).fetchone()
    return str(row[0])


def checkpoint(conn: psycopg.Connection, job: str, run_key: str, status: str, meta: dict | None = None,
               last_event_id: int | None = None) -> None:
    conn.execute(
        "insert into rsi_checkpoints (job, run_key, status, finished_at, last_event_id, meta) values "
        "(%s, %s, %s, case when %s in ('done', 'failed', 'skipped') then now() end, %s, %s) "
        "on conflict (job, run_key) do update set status = excluded.status, finished_at = excluded.finished_at, "
        "last_event_id = coalesce(excluded.last_event_id, rsi_checkpoints.last_event_id), meta = rsi_checkpoints.meta || excluded.meta",
        (job, run_key, status, status, last_event_id, Jsonb(meta or {})))


def upstream_done(conn: psycopg.Connection, job: str, run_key: str) -> bool:
    row = conn.execute("select status from rsi_checkpoints where job = %s and run_key = %s", (job, run_key)).fetchone()
    return bool(row and row[0] == "done")


def night_key(now: datetime | None = None) -> str:
    """The night a run belongs to: the local date at noon-to-noon (00:00-11:59 counts as the previous night)."""
    local = (now or datetime.now(UTC)).astimezone()
    return (local - timedelta(hours=12)).strftime("%Y-%m-%d")

