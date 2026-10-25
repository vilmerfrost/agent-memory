"""`amem rsi decide-job`: the unattended promotion worker (docs/rsi/activation.md, step 10b). Prepared, never
installed by code: launchd com.vilmerfrost.amem-rsi-decide (deploy/launchd/) runs it every 300 s, so a decision
recorded from Night Desk, the phone or the terminal lands on the Mac within 5 minutes.

Each run, in order:
1. Refuses agent sessions (exit 2, nothing written): it is a routine, never an agent's tool.
2. One job at a time: a non-blocking flock on <RSI sandbox>/run/rsi-decide-job.lock. Held: SKIP (overlap).
   (`decide_poll` keeps its own worker lock, and each edit undo request is claimed with a row lock.)
3. Credentials, the same way as the other routines: `.env.local` (config.load_env), AMEM_DB_URL, and
   AMEM_REQUIRE_REMOTE=1 from the plist. Then the login check: the job refuses (FAIL, nothing handled) to run under
   any login that could create a human decision (rsi_approval.login_problems). After `amem admin
   split-credentials` that login is amem_app (docs/rsi/least-privilege.md, A1).
4. `rsi_approval.decide_poll`: apply / reject / undo decisions -> `improve promote` / `improve revert-change`.
5. `edits_push.undo_poll`: the older edit loop's undo requests (Night Desk, phone, CLI) -> `improve revert`.
6. One routines.log line `amem-rsi-decide PASS|FAIL|SKIP login=... decisions=... undo_requests=...` plus one
   `-fail` line per problem and one `-warn` line per refused decision or undo: the format routine-audit reads.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import config, rsi_approval as AP, rsi_artifact as RA, store
from agent_memory.rsi_schedule import JobResult, _one_line, append_log, log_lines, routines_log_path

ROUTINE = "amem-rsi-decide"
LOCK_NAME = "rsi-decide-job.lock"
EXIT_AGENT = 2


@contextmanager
def job_lock(root: Path | None = None) -> Iterator[bool]:
    run = (root or RA.sandbox_dir()) / "run"
    run.mkdir(parents=True, exist_ok=True)
    fd = os.open(run / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
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


def credential_problems() -> list[str]:
    """`credentials ...` items: the database URL is set and, under AMEM_REQUIRE_REMOTE=1, remote. Presence only."""
    config.load_env()
    if not os.environ.get("AMEM_DB_URL", "").strip():
        return ["credentials database: AMEM_DB_URL is not set"]
    try:
        if config.backend() != "postgres":
            return ["credentials database: the worker needs the Postgres backend (AMEM_DB_URL)"]
    except config.BackendError as exc:
        return [f"credentials database: {exc}"]
    err = config.remote_write_error(for_backend="postgres")
    return [f"credentials database: {err.replace('amem: ', '')}"] if err else []


def _counts(items: list[dict[str, Any]], key: str = "result") -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[x[key]] = out.get(x[key], 0) + 1
    return out


def run_decide_job(*, db_url: str | None = None, push: bool = True, notify: dict[str, str] | None = None,
                   routines_log: Path | None = None, now: datetime | None = None, write_log: bool = True,
                   environ: dict[str, str] | None = None) -> JobResult | None:
    """The scheduled worker (module docstring). None: refused inside an agent session (nothing ran, no log line)."""
    if AP.agent_session_reason(environ) is not None:
        return None
    from agent_memory import edits_push
    t0 = time.monotonic()
    fixed = now is not None
    now = (now or datetime.now()).astimezone()
    log = routines_log_path(routines_log)
    res = JobResult(ROUTINE, "PASS", AP.night_key(now))

    def finish() -> JobResult:
        res.lines = log_lines(res, now=now if fixed else datetime.now().astimezone(), secs=int(time.monotonic() - t0))
        if write_log:
            append_log(log, res.lines)
        return res

    with job_lock() as got:
        if not got:
            res.status, res.warnings = "SKIP", ["overlap: another decide-job holds the lock"]
            return finish()
        creds = [] if db_url else credential_problems()
        if creds:
            res.status, res.items = "FAIL", creds
            return finish()
        try:
            with store.connect(db_url) as conn:
                login, unsafe = AP.login_problems(conn)
        except psycopg.Error as exc:
            res.status, res.items = "FAIL", [f"database: cannot connect ({type(exc).__name__})"]
            return finish()
        res.fields["login"] = login
        if unsafe:
            res.status = "FAIL"
            res.items = [f"credentials login: {u}; refusing to run unattended (see docs/rsi/least-privilege.md)"
                         for u in unsafe]
            return finish()
        try:
            rep = AP.decide_poll(db_url=db_url, push=push, notify=notify)
        except Exception as exc:  # noqa: BLE001 - one routines.log FAIL line, the undo step still runs
            res.status = "FAIL"
            res.items.append(f"decide-poll: {type(exc).__name__}")
            rep = None
        if rep is not None:
            res.fields["decisions"] = "busy" if rep.busy else len(rep.handled)
            for k, v in sorted(_counts(rep.handled).items()):
                res.fields[f"d_{k}"] = v
            res.fields["expired"], res.fields["stale"] = rep.expired, rep.stale
            for o in rep.recover:
                if o.get("outcome") == "needs-attention":
                    res.status = "FAIL"
                    res.items.append(f"recover: {_one_line(json.dumps(o, sort_keys=True, default=str), 200)}")
            res.warnings += [f"decision {h['decision']}: {h['result']}" + (f" ({_one_line(h['reason'], 160)})"
                                                                             if h.get("reason") else "")
                             for h in rep.handled if h["result"] in ("refused", "expired", "conflict")]
            res.detail["decide"] = {"handled": rep.handled, "busy": rep.busy, "recover": rep.recover}
        try:
            undo = edits_push.undo_poll(push=push, notify=notify, db_url=db_url)
        except Exception as exc:  # noqa: BLE001 - a FAIL line, never a silent crash
            res.status = "FAIL"
            res.items.append(f"undo-poll: {type(exc).__name__}")
        else:
            res.fields["undo_requests"] = len(undo.outcomes)
            for k, v in sorted(_counts([{"result": o.result} for o in undo.outcomes]).items()):
                res.fields[f"u_{k}"] = v
            res.warnings += [f"edit undo {o.line()}" for o in undo.outcomes if o.result == "failed"]
            res.detail["undo"] = [{"edit_id": str(o.edit_id), "result": o.result, "detail": o.detail}
                                  for o in undo.outcomes]
        return finish()


def cmd_decide_job(args: argparse.Namespace) -> int:
    reason = AP.agent_session_reason()
    if reason:
        print(f"amem: rsi decide-job: refused inside an agent session ({reason}); it runs from launchd or "
              "Vilmer's terminal only", file=sys.stderr)
        return EXIT_AGENT
    res = run_decide_job(push=not args.no_push, routines_log=args.routines_log)
    assert res is not None
    if args.json:
        print(json.dumps({"status": res.status, "items": res.items, "warnings": res.warnings, "fields": res.fields,
                          "lines": res.lines, "detail": res.detail}, sort_keys=True, default=str))
    else:
        print("\n".join(res.lines))
    return res.exit_code


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("decide-job", help="the scheduled promotion worker: login check, decide-poll and the edit "
                                          "undo-poll; one routines.log line (launchd every 300 s; prepared, not installed)")
    p.add_argument("--no-push", action="store_true", help="handle decisions, send no phone push")
    p.add_argument("--routines-log", type=Path, default=None, help="default AMEM_ROUTINES_LOG or ~/.claude/parent/routines.log")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_decide_job)
