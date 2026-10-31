"""`amem sessions ping`: live `sessions` rows from Claude Code hooks (docs/sessions-live.md).

`deploy/hooks/amem-session-hook.sh` saves the hook's JSON payload to a temp file and starts
`amem sessions ping --input FILE --delete-input` in the background. This module:

- SessionStart: upserts a minimal row (session_id, tool claude, cwd -> repo, project_dir,
  started_at, a best-effort kind). An existing row keeps its kind and facts.
- Stop / SessionEnd: reads the transcript's new lines only (byte offset and running totals
  per session in `<claude home>/parent/amem-session-state.json`), then writes the token
  columns, `turns`, `last_activity_at` and `meta.subagent_tokens`. A Stop less than 20 s
  after the last token update of the same session is skipped (SessionEnd never is).
- Stores labels and numbers only, never transcript text. The payload file is deleted
  after reading.

It never fails the session: every error is one log line and exit 0, a hard timeout ends
the process, and a ping that cannot reach the database is appended to
`<claude home>/parent/amem-session-spool.jsonl`. The next ping, or `amem sessions ingest`,
flushes the spool.

Where it writes: only a remote Postgres (`AMEM_DB_URL` set and not local, the
AMEM_REQUIRE_REMOTE rule of config.remote_write_error). No remote configured (no
AMEM_DB_URL, a local host, or the REST backend): spool only. `AMEM_PING_ALLOW_LOCAL=1`
allows a local database (tests only).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_memory import claude_roots, config, session_resume, session_tokens, sessions_ingest

EVENTS = ("SessionStart", "Stop", "SessionEnd")
DEBOUNCE_SECONDS = 20.0
HARD_TIMEOUT_SECONDS = 25
CONNECT_TIMEOUT_SECONDS = 5
LOCK_WAIT_SECONDS = 5.0
STATE_KEEP_DAYS = 14
PAYLOAD_MAX_BYTES = 4_000_000
SPOOL_MAX_BYTES = 5_000_000
ALLOW_LOCAL_ENV = "AMEM_PING_ALLOW_LOCAL"

STATE_NAME = "amem-session-state.json"
SPOOL_NAME = "amem-session-spool.jsonl"
LOCK_NAME = "amem-session.lock"

_SID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_LABEL_RE = re.compile(r"[\w .:/@+-]{1,100}")
RECORD_VERSION = 1


def parent_dir() -> Path:
    """`<claude home>/parent` (AMEM_CLAUDE_HOME in tests, else ~/.claude/parent)."""
    return sessions_ingest.claude_home() / "parent"


def _iso(ts: datetime | None) -> str | None:
    return ts.astimezone(UTC).isoformat() if ts else None


def _label(value: Any) -> str | None:
    return value if isinstance(value, str) and _LABEL_RE.fullmatch(value) else None


# ---------------------------------------------------------------- payload


@dataclass
class Ping:
    """What one hook payload says. Validated; nothing else from the payload is kept."""

    event: str
    session_id: str
    cwd: str
    transcript: Path | None
    source: str | None = None       # SessionStart: startup | resume | clear | compact | fork
    model: str | None = None        # SessionStart: the active model (optional field)
    agent_type: str | None = None   # `claude --agent <name>`


def parse_payload(raw: str | bytes) -> Ping | None:
    """The hook payload, or None when it is not a SessionStart / Stop / SessionEnd payload
    of a main session (a subagent's payload carries `agent_id` and is left to ingest)."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    event, sid = data.get("hook_event_name"), data.get("session_id")
    if event not in EVENTS or not isinstance(sid, str) or not _SID_RE.fullmatch(sid) or data.get("agent_id"):
        return None
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
    transcript = None
    tp = data.get("transcript_path")
    if isinstance(tp, str) and tp.endswith(".jsonl"):
        transcript = Path(tp).expanduser()
    return Ping(event, sid, cwd, transcript, _label(data.get("source")), _label(data.get("model")),
                _label(data.get("agent_type")))


# ---------------------------------------------------------------- where to write


def target_db() -> tuple[str | None, str]:
    """(dsn, "") when a ping may write, else (None, why). Never returns or prints a DSN in `why`."""
    try:
        active = config.backend()
    except config.BackendError:
        return None, "bad AMEM_BACKEND"
    if active != "postgres":
        return None, f"backend {active} (sessions need Postgres)"
    raw = os.environ.get("AMEM_DB_URL", "").strip()
    if not raw:
        return None, "no AMEM_DB_URL (no remote configured)"
    try:
        hosts = config.db_hosts(raw)
    except ValueError:
        return None, "AMEM_DB_URL is not a valid connection string"
    if any(config._is_local_host(h) for h in hosts) and os.environ.get(ALLOW_LOCAL_ENV, "").strip() != "1":
        return None, "AMEM_DB_URL is local (no remote configured)"
    return raw, ""


# ---------------------------------------------------------------- lock, state, spool


@contextmanager
def locked(wait: float | None = None) -> Iterator[bool]:
    """The one lock for the state and spool files. Yields False when another ping held it
    for `wait` seconds (default LOCK_WAIT_SECONDS)."""
    d = parent_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    try:
        deadline = time.monotonic() + (LOCK_WAIT_SECONDS if wait is None else wait)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield got
    finally:
        if got:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_state() -> dict[str, Any]:
    try:
        data = json.loads((parent_dir() / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any], now: datetime) -> None:
    """Atomic write; entries not updated for STATE_KEEP_DAYS are dropped."""
    cutoff = (now - timedelta(days=STATE_KEEP_DAYS)).timestamp()
    keep = {k: v for k, v in state.items() if isinstance(v, dict) and float(v.get("updated", 0) or 0) >= cutoff}
    path = parent_dir() / STATE_NAME
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, separators=(",", ":"))
    os.replace(tmp, path)


def spool_append(record: dict[str, Any]) -> bool:
    """Append one record (one line, one write). False when the spool is over its cap."""
    path = parent_dir() / SPOOL_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > SPOOL_MAX_BYTES:
            return False
    except OSError:
        pass
    line = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return True


def _valid_record(r: Any) -> bool:
    return (isinstance(r, dict) and r.get("v") == RECORD_VERSION and isinstance(r.get("session_id"), str)
            and bool(_SID_RE.fullmatch(r["session_id"])) and isinstance(r.get("project_dir"), str)
            and r.get("kind") in sessions_ingest.KINDS)


def flush_spool(*, db_url: str | None = None, _locked: bool = False) -> int:
    """Write every spooled record (one transaction) and empty the spool. Returns the records
    written. A database error puts the records back and raises. Another flush holding the
    lock: returns 0."""
    if not _locked:
        with locked(wait=10.0) as got:
            return flush_spool(db_url=db_url, _locked=True) if got else 0
    d = parent_dir()
    spool, work = d / SPOOL_NAME, d / (SPOOL_NAME + ".flushing")
    if not work.exists():
        if not spool.exists():
            return 0
        os.replace(spool, work)  # pings that append from now on start a new spool file
    data = work.read_bytes()
    records = []
    for line in data.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if _valid_record(rec):
            records.append(rec)
    try:
        if records:
            with sessions_ingest.store.connect(db_url, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
                for rec in records:
                    conn.execute(PING_SQL, record_params(rec))
    except BaseException:
        fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, data if data.endswith(b"\n") or not data else data + b"\n")
        finally:
            os.close(fd)
        work.unlink(missing_ok=True)
        raise
    work.unlink(missing_ok=True)
    return len(records)


# ---------------------------------------------------------------- the row


def _total(alias: str) -> str:
    return (f"({alias}.tokens_input + {alias}.tokens_output + {alias}.tokens_cache_read "
            f"+ {alias}.tokens_cache_creation)")


# Token totals of a session only grow, so a ping never lowers them: a spooled ping flushed
# after a newer one (or after `sessions ingest`) changes nothing. Kind and facts of an
# existing row stay (ingest is the full read); meta is merged.
_NEWER = f"(excluded.tokens_input is not null and {_total('excluded')} >= coalesce({_total('sessions')}, -1))"

# Title, profile and outcome follow the token rule (a newer ping wins, a late spooled one only
# fills what is empty); category and project of an existing row stay (ingest knows the real kind).
_PROFILE = ("title", *sessions_ingest.PROFILE_COLS, "outcome", "outcome_evidence")


def _keep_commits(expr: str) -> str:
    """`expr` (the new outcome_evidence) plus the stored `commits` when it has none: a ping
    never reads commits (docs/sessions-live.md, "Commits"), so it must not drop the ones
    `sessions ingest` stored."""
    return (f"case when sessions.outcome_evidence ? 'commits' and not ({expr} ? 'commits') "
            f"then {expr} || jsonb_build_object('commits', sessions.outcome_evidence -> 'commits') else {expr} end")


def _profile_set(c: str) -> str:
    newer = f"coalesce(excluded.{c}, sessions.{c})"
    if c == "outcome_evidence":
        newer = _keep_commits(newer)
    return f"{c} = case when {_NEWER} then {newer} else coalesce(sessions.{c}, excluded.{c}) end"


_PROFILE_SET = ",\n        ".join(_profile_set(c) for c in _PROFILE)

PING_SQL = f"""
insert into sessions (session_id, tool, kind, repo, project_dir, started_at, ended_at, meta,
                      tokens_input, tokens_output, tokens_cache_read, tokens_cache_creation, models, turns,
                      last_activity_at, tokens_updated_at, category, project, {', '.join(_PROFILE)},
                      title_updated_at, outcome_at)
values (%s, 'claude', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        case when %s::bigint is null then null else now() end, %s, %s, {', '.join(['%s'] * len(_PROFILE))},
        case when %s::text is null then null else now() end, case when %s::text is null then null else now() end)
on conflict (session_id) do update
    set category = coalesce(sessions.category, excluded.category),
        project = coalesce(sessions.project, excluded.project),
        title_updated_at = case when {_NEWER} and excluded.title is not null
                                     and excluded.title is distinct from sessions.title
                                then now() else sessions.title_updated_at end,
        outcome_at = case when {_NEWER} and excluded.outcome is not null
                               and excluded.outcome is distinct from sessions.outcome
                          then now() else sessions.outcome_at end,
        {_PROFILE_SET},
        repo = coalesce(sessions.repo, excluded.repo),
        started_at = least(sessions.started_at, excluded.started_at),
        ended_at = greatest(sessions.ended_at, excluded.ended_at),
        meta = sessions.meta || excluded.meta,
        tokens_input = case when {_NEWER} then excluded.tokens_input else sessions.tokens_input end,
        tokens_output = case when {_NEWER} then excluded.tokens_output else sessions.tokens_output end,
        tokens_cache_read = case when {_NEWER} then excluded.tokens_cache_read else sessions.tokens_cache_read end,
        tokens_cache_creation = case when {_NEWER} then excluded.tokens_cache_creation
                                     else sessions.tokens_cache_creation end,
        models = case when {_NEWER} then excluded.models else sessions.models end,
        turns = case when {_NEWER} then excluded.turns else sessions.turns end,
        last_activity_at = greatest(sessions.last_activity_at, excluded.last_activity_at),
        tokens_updated_at = case when {_NEWER} then now() else sessions.tokens_updated_at end
"""


def _ts(value: Any) -> datetime | None:
    return sessions_ingest.parse_ts(value)


def record_params(r: dict[str, Any]) -> tuple[Any, ...]:
    from psycopg.types.json import Jsonb

    tokens = r.get("tokens") if isinstance(r.get("tokens"), list) and len(r["tokens"]) == 4 else None
    t = session_tokens.Tokens.of(tokens).as_list() if tokens is not None else [None] * 4
    models = r.get("models") if isinstance(r.get("models"), dict) and tokens is not None else None
    turns = r.get("turns") if isinstance(r.get("turns"), int) and tokens is not None else None
    meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
    repo = r.get("repo") if isinstance(r.get("repo"), str) else None
    category = r.get("category") if r.get("category") in sessions_ingest.CATEGORIES else None
    project = r.get("project") if isinstance(r.get("project"), str) and r.get("project") else None
    title = session_tokens.clean_title(r.get("title"))
    prof = sessions_ingest.profile_values(sessions_ingest.cloud_profile(r["profile"])
                                          if isinstance(r.get("profile"), dict) else None)
    out = r.get("outcome") if isinstance(r.get("outcome"), dict) else {}
    name = out.get("name") if out.get("name") in session_tokens.OUTCOMES else None
    raw_evidence = out.get("evidence") if isinstance(out.get("evidence"), dict) else None
    if raw_evidence is not None and raw_evidence.get("method_version") != 2:
        name = None  # Old queued invocation labels remain unverified; do not upgrade them.
    evidence = Jsonb(raw_evidence) if raw_evidence is not None else None
    return (r["session_id"], r["kind"], repo, r["project_dir"], _ts(r.get("started_at")), _ts(r.get("ended_at")),
            Jsonb(meta), *t, Jsonb(models) if models is not None else None, turns,
            _ts(r.get("last_activity_at")), t[0], category, project, title,
            *(prof[c] for c in sessions_ingest.PROFILE_COLS), name, evidence, title, name)


def best_kind(project_dir: str, cwd: str) -> str:
    """Kind from what a hook knows: eval (eval clone / cwd), headless (`claude -p` / SDK, by
    CLAUDE_CODE_ENTRYPOINT), else chat. `sessions ingest` sets the real kind later."""
    if sessions_ingest.SKIP_PROJECT_RE.search(project_dir) or (cwd and sessions_ingest.EVAL_CWD_RE.search(cwd)):
        return "eval"
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        return "headless"
    return "chat"


def _project_dir(p: Ping) -> str:
    if p.transcript is not None and p.transcript.parent.name:
        return p.transcript.parent.name
    return re.sub(r"[^A-Za-z0-9]", "-", p.cwd) or "unknown"


def _scan_subagents(p: Ping, entry: dict[str, Any]) -> dict[str, int] | None:
    """Incremental token sums of `<session>/subagents/agent-*.jsonl` next to the transcript."""
    if p.transcript is None:
        return None
    sub_dir = p.transcript.parent / p.transcript.stem / "subagents"
    if not sub_dir.is_dir():
        return None
    subs = entry.setdefault("subs", {})
    total, n = session_tokens.Tokens(), 0
    for f in sorted(sub_dir.glob("agent-*.jsonl")):
        if (subs.get(f.stem) or {}).get("copy"):
            continue   # a pure copy of an earlier session's subagent (session_resume): not this session's
        sc = session_tokens.ClaudeScanner(subs.get(f.stem), own_session=p.session_id)  # a copied file adds 0
        if sc.offset == 0:   # a first read: skip lines copied from an earlier file (session_resume)
            resume = session_resume.resume_of(f)
            if resume.pure_copy(sessions_ingest.load_events(f), p.session_id):
                subs[f.stem] = {"copy": True}
                continue
            sc.skip_uuids = resume.copied
        sc.scan_file(f)
        subs[f.stem] = sc.state()
        total.add(sc.summary.tokens)
        n += 1
    return {**total.as_dict(), "sessions": n} if n else None


def build_record(p: Ping, entry: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The row a ping writes. Stop / SessionEnd advance `entry` (the session's scanner state)."""
    project_dir = _project_dir(p)
    cwd, _scratch = sessions_ingest.work_cwd([p.cwd] if p.cwd else [], project_dir)
    repo = sessions_ingest.repo_of(cwd)
    meta: dict[str, Any] = {"cwd": cwd or None, "live": True}
    root_label = claude_roots.label_of(p.transcript) if p.transcript is not None else None
    if root_label is not None:
        meta["claude_root"] = root_label
    meta.update(sessions_ingest.team_info(cwd, repo))
    if p.agent_type:
        meta["agent_type"] = p.agent_type
    kind = best_kind(project_dir, cwd)
    rec: dict[str, Any] = {"v": RECORD_VERSION, "event": p.event, "session_id": p.session_id,
                           "kind": kind, "repo": repo, "project_dir": project_dir,
                           "started_at": _iso(now), "ended_at": None, "last_activity_at": _iso(now),
                           "tokens": None, "models": None, "turns": None, "meta": meta,
                           "category": sessions_ingest.category_of(kind),
                           "project": sessions_ingest.project_of(cwd, kind=kind, scratch_only=_scratch,
                                                                 project_dir=project_dir, repo=repo)}
    if p.event == "SessionStart":
        if p.source:
            meta["start_source"] = p.source
        if p.model:
            meta["model"] = p.model
        # A resumed session: the title the earlier scans saw (no transcript read here).
        rec["title"] = session_tokens.ClaudeScanner(entry.get("scan")).summary.title
        return rec
    sc = session_tokens.ClaudeScanner(entry.get("scan"), own_session=p.session_id)  # resumed: own lines only
    if p.transcript is not None and p.transcript.is_file():
        if sc.offset == 0 or p.transcript.stat().st_size < sc.offset:
            # A first read of a resumed transcript: its copied lines (session_resume) are the
            # earlier session's. The copy is the file's start, so later reads need no skip set.
            resume = session_resume.resume_of(p.transcript)
            sc.skip_uuids = resume.copied
            if resume.resumed_from:
                meta["resumed_from"] = resume.resumed_from
        sc.scan_file(p.transcript)
    entry["scan"] = sc.state()
    s = sc.summary
    last = max(s.last_at, now) if s.last_at else now
    name, evidence = s.outcome(now)
    rec.update(started_at=_iso(s.first_at or now), ended_at=_iso(s.last_at), last_activity_at=_iso(last),
               tokens=s.tokens.as_list(), turns=s.turns,
               models={k: dict(v) for k, v in sorted(s.models.items())},
               title=s.title, profile=s.profile(), outcome={"name": name, "evidence": evidence})
    subs = _scan_subagents(p, entry)
    if subs is not None:
        meta["subagent_tokens"] = subs
    if p.event == "SessionEnd":
        meta["ended"] = True
    return rec


def ping(raw: str | bytes, *, now: datetime | None = None, db_url: str | None = None) -> str:
    """Handle one hook payload. Returns a short status (for the log line). Raises nothing
    the caller must handle except a timeout, which `main` catches."""
    now = now or datetime.now(UTC)
    p = parse_payload(raw)
    if p is None:
        return "ignored"
    target, why = (db_url, "") if db_url else target_db()
    with locked() as got:
        if not got:
            if p.event == "Stop":
                return "skipped (busy)"
            # No state without the lock: spool the minimal row (the next Stop or ingest has the tokens).
            rec = build_record(replace(p, event="SessionStart"), {}, now)
            return "spooled (busy)" if spool_append(rec) else "dropped (spool full)"
        state = load_state()
        entry = state.get(p.session_id) if isinstance(state.get(p.session_id), dict) else {}
        last = float(entry.get("scanned", 0) or 0)
        if p.event == "Stop" and now.timestamp() - last < DEBOUNCE_SECONDS:
            return "debounced"
        rec = build_record(p, entry, now)
        entry["updated"] = now.timestamp()
        if p.event != "SessionStart":
            entry["scanned"] = now.timestamp()
        state[p.session_id] = entry
        save_state(state, now)
        if target is None:
            return f"spooled ({why})" if spool_append(rec) else "dropped (spool full)"
        try:
            flushed = flush_spool(db_url=target, _locked=True)
            with sessions_ingest.store.connect(target, connect_timeout=CONNECT_TIMEOUT_SECONDS) as conn:
                conn.execute(PING_SQL, record_params(rec))
        except Exception as exc:  # the database is down or slow: keep the ping for later
            ok = spool_append(rec)
            return f"spooled (db error: {type(exc).__name__})" if ok else "dropped (spool full)"
        return "written" + (f" (+{flushed} from spool)" if flushed else "")


# ---------------------------------------------------------------- command


class _Timeout(Exception):
    pass


def _on_alarm(_signum: int, _frame: Any) -> None:
    raise _Timeout()


def _read_input(path: Path | None, delete: bool) -> bytes:
    try:
        if path is None:
            return sys.stdin.buffer.read(PAYLOAD_MAX_BYTES)
        with open(path, "rb") as fh:
            return fh.read(PAYLOAD_MAX_BYTES)
    finally:
        if path is not None and delete:
            try:
                path.unlink()
            except OSError:
                pass


class _QuietParser(argparse.ArgumentParser):
    """A bad argument is one log line (main prints it), not a usage dump and exit 2."""

    def error(self, message: str) -> Any:  # type: ignore[override]
        raise SystemExit(0)


def parser() -> argparse.ArgumentParser:
    p = _QuietParser(prog="amem sessions ping",
                                description="Upsert this Claude Code session's row from a hook payload "
                                            "(docs/sessions-live.md). Always exits 0.")
    p.add_argument("--input", type=Path, default=None, help="payload file (default: stdin)")
    p.add_argument("--delete-input", action="store_true", help="delete the payload file after reading it")
    p.add_argument("--timeout", type=int, default=HARD_TIMEOUT_SECONDS, help="hard timeout in seconds (default 25)")
    p.add_argument("--setsid", action="store_true", help="start a new session first (the hook script passes it)")
    return p


def main(argv: list[str] | None = None) -> int:
    """`amem sessions ping`. Exit 0 always; one log line on stderr."""
    started = time.monotonic()
    status = "error"
    sid = "-"
    event = "-"
    try:
        args = parser().parse_args(argv)
    except SystemExit:
        print("amem-ping: bad arguments", file=sys.stderr)
        return 0
    try:
        if args.setsid:
            try:
                os.setsid()  # leave the hook's process group: a group kill does not reach the ping
            except OSError:
                pass
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, _on_alarm)
            signal.alarm(max(1, args.timeout))
        raw = _read_input(args.input, args.delete_input)
        p = parse_payload(raw)
        if p is not None:
            sid, event = p.session_id[:8], p.event
        config.load_env()
        status = ping(raw)
    except _Timeout:
        status = "timeout"
    except BaseException as exc:  # never fail the session
        status = f"error ({type(exc).__name__})"
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    took = time.monotonic() - started
    print(f"amem-ping {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} {event} {sid} {status} {took:.2f}s",
          file=sys.stderr)
    return 0
