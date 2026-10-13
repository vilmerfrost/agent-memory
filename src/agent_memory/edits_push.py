"""Phone push per edit, with an Undo button, and the undo poll (docs/edits.md, "Push and undo").

Push (kept / trial edits): ntfy, published as JSON to the server root (topic and server from
~/.claude/parent/notify.env, like `amem daily`; the topic is never printed). The message has
an `http` action "Undo" that POSTs {edit_id, token} to the edge function `amem-undo`
(supabase/functions/amem-undo). The token is random per edit; only its sha256 is stored
(edits.undo_token_hash). No topic, or ntfy fails: a macOS notification naming
`amem edits undo <id>` instead.

Undo poll (`amem edits undo-poll`, every 15 minutes from agent-config's amem-ingest):
pending undo_requests -> `improve revert <improve id> --reason "phone undo"` -> request
handled, edit status `reverted`, a short confirmation push. A failed revert is recorded and
retried; after MAX_ATTEMPTS it is closed as failed and pushed. A busy revert (improve's write
lock held, exit 75) is retried without counting as an attempt.

The ntfy and osascript calls go through `daily.http_post` / `daily.mac_notify` (tests
replace them; no network in tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import config, daily, edits, store

UNDO_URL_ENV = "AMEM_EDITS_UNDO_URL"
FUNCTION_PATH = "/functions/v1/amem-undo"
MAX_ATTEMPTS = 3
REVERTABLE = ("kept", "trial")
# `improve revert` waits IMPROVE_REVERT_WAIT s for its write lock (an apply may be running
# evals), then exits 75 "busy" with nothing changed. A busy revert is retried next poll and
# never counts as an attempt. amem-ingest kills the whole undo-poll after 120 s
# (AMEM_UNDO_POLL_TIMEOUT), so improve must give up first: wait 60 s, our own kill at 100 s.
REVERT_WAIT_ENV = "IMPROVE_REVERT_WAIT"
REVERT_WAIT = 60
REVERT_TIMEOUT = 100
EX_BUSY = 75


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def project_url() -> str | None:
    """AMEM_SUPABASE_URL, else the hosted project URL derived from AMEM_DB_URL
    (`db.<ref>.supabase.co`, or a pooler DSN whose user is `postgres.<ref>`)."""
    base = config.supabase_url()
    if base:
        return base
    config.load_env()
    dsn = os.environ.get("AMEM_DB_URL", "").strip()
    if not dsn:
        return None
    try:
        info = conninfo_to_dict(dsn)
    except psycopg.Error:
        return None
    host, user = str(info.get("host") or ""), str(info.get("user") or "")
    ref = None
    if host.startswith("db.") and host.endswith(".supabase.co"):
        ref = host[3:-len(".supabase.co")]
    elif host.endswith(".pooler.supabase.com") and user.startswith("postgres."):
        ref = user.split(".", 1)[1]
    if not ref or not ref.isalnum():
        return None
    return f"https://{ref}.supabase" + ".co"


def undo_url() -> str | None:
    """AMEM_EDITS_UNDO_URL (tests, local stack), else <project URL>/functions/v1/amem-undo."""
    config.load_env()
    override = os.environ.get(UNDO_URL_ENV, "").strip()
    if override:
        return override
    base = project_url()
    return base.rstrip("/") + FUNCTION_PATH if base else None


def short_name(target: str | None) -> str:
    """`dispatch/SKILL.md` for ~/.agents/skills/dispatch/SKILL.md, `CLAUDE.md`, ..."""
    if not target or target == "-":
        return "-"
    p = Path(target)
    return f"{p.parent.name}/{p.name}" if p.name in ("SKILL.md",) else p.name


def edit_message(o: edits.Outcome, *, with_cli_undo: bool) -> tuple[str, str]:
    title = f"Agent edit: {o.rule_title or o.rule_id}"
    kind = f"Kind: {o.kind}" + (f" ({o.kind_reason})" if o.kind_reason else "") if o.kind else ""
    lines = [o.summary or "", f"Why: {o.why}" if o.why else "", kind, f"File: {short_name(o.target)}"]
    if o.status == "trial":
        lines.append("Trial: the evals could not prove it yet.")
    if with_cli_undo:
        lines.append(f"Undo: amem edits undo {o.edit_id}")
    return title, "\n".join(ln for ln in lines if ln)


def ntfy_payload(topic: str, title: str, body: str, *, url: str | None, edit_id: Any, token: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"topic": topic, "title": title, "message": body, "priority": 3, "tags": ["pencil2"]}
    if url:
        payload["actions"] = [{"action": "http", "label": "Undo", "url": url, "method": "POST",
                               "headers": {"Content-Type": "application/json"},
                               "body": json.dumps({"edit_id": str(edit_id), "token": token}), "clear": True}]
    return payload


def _publish(payload_for: Any, title: str, mac_body: str, notify: dict[str, str] | None) -> str:
    """ntfy JSON publish ('phone') else macOS ('mac'). `payload_for(topic)` builds the JSON.
    Raises nothing. Never prints the topic."""
    env = daily.read_notify_env(daily.NOTIFY_ENV) if notify is None else notify
    topic = (env.get("NTFY_TOPIC") or "").strip()
    if topic:
        server = (env.get("NTFY_SERVER") or daily.DEFAULT_NTFY_SERVER).strip().rstrip("/")
        try:
            status = daily.http_post(server + "/", json.dumps(payload_for(topic)).encode("utf-8"),
                                     {"Content-Type": "application/json"})
        except Exception:
            status = None
        if status == 200:
            return "phone"
    try:
        daily.mac_notify(title, mac_body)
    except Exception:
        pass
    return "mac"


def push_new_edit(conn: psycopg.Connection, o: edits.Outcome, *, push: bool = True,
                  notify: dict[str, str] | None = None) -> str:
    """Store the undo token's hash on the edit and push it. 'phone' | 'mac' | 'off'."""
    token = secrets.token_urlsafe(24)
    conn.execute("update edits set undo_token_hash = %s where id = %s", (token_hash(token), o.edit_id))
    if not push:
        return "off"
    url = undo_url()
    title, body = edit_message(o, with_cli_undo=url is None)
    _, mac_body = edit_message(o, with_cli_undo=True)
    result = _publish(lambda topic: ntfy_payload(topic, title, body, url=url, edit_id=o.edit_id, token=token),
                      title, mac_body, notify)
    conn.execute("update edits set pushed_at = now(), meta = meta || %s where id = %s",
                 (Jsonb({"push": result}), o.edit_id))
    return result


def push_note(title: str, body: str, notify: dict[str, str] | None = None) -> str:
    return _publish(lambda topic: {"topic": topic, "title": title, "message": body, "priority": 3,
                                   "tags": ["leftwards_arrow_with_hook"]}, title, body, notify)


# --------------------------------------------------------------------------- undo


@dataclass
class UndoOutcome:
    edit_id: uuid.UUID
    result: str           # reverted | nothing | failed | retry
    detail: str = ""

    def line(self) -> str:
        return f"{self.result} edit={self.edit_id}" + (f": {self.detail}" if self.detail else "")


@dataclass
class PollReport:
    outcomes: list[UndoOutcome] = field(default_factory=list)

    def count(self, result: str) -> int:
        return sum(o.result == result for o in self.outcomes)

    def summary(self) -> str:
        return (f"undo-poll: requests={len(self.outcomes)} reverted={self.count('reverted')} "
                f"nothing={self.count('nothing')} retry={self.count('retry')} failed={self.count('failed')} "
                f"busy={self.count('busy')}")


_PENDING_SQL = """
select u.id as request_id, u.edit_id, u.attempts, u.source, u.requested_at,
       e.status, e.improve_id, e.rule_id, e.rule_title, e.target_path
from undo_requests u join edits e on e.id = u.edit_id
where u.handled_at is null {only}
order by u.requested_at, u.id
"""


def _revert(improve_id: str, reason: str) -> subprocess.CompletedProcess[str]:
    """`improve revert`, bounded: improve waits at most REVERT_WAIT s for its write lock (then
    exit 75 busy), and the process is killed after REVERT_TIMEOUT s; both stay under
    amem-ingest's 120 s undo-poll timeout."""
    argv = [edits.improve_bin(), "revert", improve_id, "--reason", reason]
    env = {**os.environ, REVERT_WAIT_ENV: str(REVERT_WAIT)}
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=REVERT_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise edits.ImproveError(f"improve revert timed out after {REVERT_TIMEOUT}s") from None
    except OSError as exc:
        raise edits.ImproveError(f"cannot run improve: {type(exc).__name__}: {exc}") from None


def _handle(conn: psycopg.Connection, r: dict[str, Any], *, push: bool, notify: dict[str, str] | None) -> UndoOutcome:
    if r["status"] not in REVERTABLE or not r["improve_id"]:
        detail = f"nothing to revert (status {r['status']})"
        conn.execute("update undo_requests set handled_at = now(), result = %s where id = %s",
                     (detail, r["request_id"]))
        conn.execute("update edits set undo_requested_at = coalesce(undo_requested_at, %s) where id = %s",
                     (r["requested_at"], r["edit_id"]))
        return UndoOutcome(r["edit_id"], "nothing", detail)
    reason = "phone undo" if r["source"] == "phone" else "manual undo"
    attempts = int(r["attempts"]) + 1
    try:
        proc = _revert(str(r["improve_id"]), reason)
        ok, why = proc.returncode == 0, edits._one_line(proc.stderr or proc.stdout or "", 200)
        why = f"exit {proc.returncode}: {why}" if not ok else why
        busy = not ok and (proc.returncode == EX_BUSY or "busy" in (proc.stderr or "").lower())
    except edits.ImproveError as exc:
        ok, busy, why = False, False, str(exc)
    if busy:  # improve's write lock is held (an apply's evals): nothing changed, not an attempt
        detail = f"busy, retrying: {why}"
        conn.execute("update undo_requests set result = %s where id = %s", (detail[:500], r["request_id"]))
        conn.commit()
        return UndoOutcome(r["edit_id"], "busy", detail)
    label = r["rule_title"] or r["rule_id"]
    if ok:
        conn.execute("update undo_requests set handled_at = now(), result = 'reverted', attempts = %s where id = %s",
                     (attempts, r["request_id"]))
        conn.execute("update edits set status = 'reverted', reverted_at = now(), "
                     "undo_requested_at = coalesce(undo_requested_at, %s) where id = %s",
                     (r["requested_at"], r["edit_id"]))
        conn.commit()
        if push:
            push_note(f"Agent edit undone: {label}",
                      f"Reverted {short_name(r['target_path'])} (improve {r['improve_id']}).", notify)
        return UndoOutcome(r["edit_id"], "reverted", f"improve {r['improve_id']}")
    detail = f"failed (attempt {attempts}): {why}"
    closed = attempts >= MAX_ATTEMPTS
    conn.execute("update undo_requests set attempts = %s, result = %s, handled_at = case when %s then now() end "
                 "where id = %s", (attempts, detail[:500], closed, r["request_id"]))
    conn.commit()
    if closed and push:
        push_note(f"Agent edit undo failed: {label}",
                  f"improve revert {r['improve_id']} failed {attempts} times. Run it by hand.", notify)
    return UndoOutcome(r["edit_id"], "failed" if closed else "retry", detail)


def undo_poll(*, push: bool = True, notify: dict[str, str] | None = None, edit_id: uuid.UUID | None = None,
              db_url: str | None = None) -> PollReport:
    """Handle every pending undo request (or only `edit_id`'s). One request's failure never
    stops the others."""
    report = PollReport()
    only = "and u.edit_id = %(edit)s" if edit_id is not None else ""
    with store.connect(db_url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_PENDING_SQL.format(only=only), {"edit": edit_id})
            rows = cur.fetchall()
        for r in rows:
            try:
                report.outcomes.append(_handle(conn, r, push=push, notify=notify))
                conn.commit()
            except Exception as exc:  # DB hiccup: this request is retried next poll
                try:
                    conn.rollback()
                except psycopg.Error:
                    pass
                report.outcomes.append(UndoOutcome(r["edit_id"], "retry", f"{type(exc).__name__}: {exc}"))
    return report


class UndoError(RuntimeError):
    pass


def request_undo(edit_id: uuid.UUID, *, db_url: str | None = None) -> None:
    """Queue a local (cli) undo. A request that is closed without a revert is reopened.
    Same SQL function as the phone's edge function (amem_request_undo)."""
    with store.connect(db_url) as conn:
        (state,) = conn.execute("select amem_request_undo(%s, 'cli')", (edit_id,)).fetchone()
        if state == "missing":
            raise UndoError(f"no edit {edit_id}")
