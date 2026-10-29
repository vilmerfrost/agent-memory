"""Data-based time and usage estimates for parent dispatch runs.

A run is dispatch -> brief: `amem dispatch start` when the parent spawns its
workers, `amem dispatch end` when it briefs Vilmer. The parent chat idles in
between, so its window is not the run.

start/end stay local and fast: stdlib only, no database, Vitals read in parallel
with a short timeout. They append schema-versioned JSONL records to
`<claude home>/parent/<repo>/dispatch-events.jsonl`; the timing collector uploads
them (timing_store.ingest_spools(kind="dispatch")). `refresh` computes actuals from
those records plus tasks.jsonl rounds and writes the lookup file
`<claude home>/parent/dispatch-estimates.json` that start reads.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Iterable
import uuid

SCHEMA = 1
METHOD = "dispatch-estimates/1"
SPOOL = "dispatch-events.jsonl"
LOOKUP = "dispatch-estimates.json"
MAX_LINE = 32768
VITALS_TIMEOUT = 0.5
SAME_WINDOW_S = 300          # Vitals rounds rolling reset times; a real reset moves hours
MIN_BUCKET_RUNS = 3          # time: fewer runs in a worker bucket -> use the tool's pooled runs
MIN_CLEAN_USAGE = 5          # usage: fewer clean runs in a bucket -> token ratio fallback
DEFAULT_MINUTES = 30         # only when no run has ever been recorded
TOOLS = ("claude", "codex")
PRIMARY_LIMIT = {"claude": "claude:session", "codex": "codex:week"}
LIMIT_LABEL = {"claude:session": "Claude 5h", "claude:week": "Claude week",
               "codex:week": "Codex week", "codex:session": "Codex 5h"}
STATUSES = ("done", "partial", "blocked", "abandoned", "failed")
_NS = uuid.UUID("6f1e3a52-5d0c-4c51-9d1e-6a8c0d2b7f41")


# ---------------------------------------------------------------- paths and names

def parent_dir() -> Path:
    home = os.environ.get("AMEM_CLAUDE_HOME", "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".claude") / "parent"


def safe_name(value: str, limit: int = 120) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._:@ -]", "_", value.strip())[:limit].strip()
    if not cleaned or cleaned in {".", ".."} or cleaned.startswith("."):
        cleaned = "_" + cleaned.lstrip(".")
    return cleaned


def repo_dir(repo: str, parent: Path | None = None) -> Path:
    return (parent or parent_dir()) / safe_name(repo, 200)


def open_path(repo: str, session: str, parent: Path | None = None) -> Path:
    return repo_dir(repo, parent) / "dispatch-open" / (safe_name(session) + ".json")


def bucket(workers: int) -> str:
    return "1" if workers <= 1 else "2" if workers == 2 else "3-4" if workers <= 4 else "5+"


def resolve_session(session: str | None, tool: str | None) -> tuple[str | None, str]:
    """(session id, parent tool). --session/--tool win; then env, Codex before Claude."""
    env = os.environ
    found = session or env.get("AMEM_TIMING_SESSION_ID", "").strip() or None
    if not found and env.get("CODEX_THREAD_ID", "").strip():
        found = "codex:" + env["CODEX_THREAD_ID"].strip()
    if not found and env.get("CLAUDE_CODE_SESSION_ID", "").strip():
        found = env["CLAUDE_CODE_SESSION_ID"].strip()
    if tool not in TOOLS:
        tool = "codex" if found and found.startswith("codex:") else "claude"
    return found, tool


def account_hash(account: Any) -> str:
    text = str(account or "unknown").strip().lower()
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


# ---------------------------------------------------------------- Vitals

def read_vitals(timeout: float = VITALS_TIMEOUT) -> tuple[dict | None, dict | None, list[str]]:
    """`vitals limits --json` and `vitals agents --json` in parallel. Never raises."""
    binary = shutil.which("vitals")
    if not binary:
        return None, None, ["Vitals not found on PATH"]
    procs: dict[str, subprocess.Popen] = {}
    notes: list[str] = []
    for name in ("limits", "agents"):
        try:
            procs[name] = subprocess.Popen([binary, name, "--json"], stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except OSError:
            notes.append(f"Vitals {name} could not start")
    deadline = time.monotonic() + timeout
    out: dict[str, dict | None] = {"limits": None, "agents": None}
    for name, proc in procs.items():
        try:
            raw, _ = proc.communicate(timeout=max(0.02, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=0.2)
            except subprocess.TimeoutExpired:   # a grandchild still holds the pipe; leave it
                pass
            notes.append(f"Vitals {name} timed out")
            continue
        try:
            data = json.loads(raw) if proc.returncode == 0 else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            out[name] = data
        else:
            notes.append(f"Vitals {name} unreadable")
    return out["limits"], out["agents"], notes


LATE_TIMEOUT = 15.0
SLOW_NOTE = "Vitals limits slow: usage snapshot follows in the background"


def late_snapshot(notes: list[str], spool: Path, run_id: str, phase: str) -> None:
    """`vitals limits` blocks for seconds when its data is about a minute old (it asks the
    providers again). Instead of waiting, a detached helper takes the snapshot and
    appends it to the spool; its taken_at is Vitals' own measurement time."""
    if "Vitals limits timed out" not in notes:
        return
    import sys
    try:
        subprocess.Popen([sys.executable, "-m", "agent_memory.dispatch_estimates", "late-snapshot",
                          str(spool), run_id, phase], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return
    notes[notes.index("Vitals limits timed out")] = SLOW_NOTE


def _late_main(argv: list[str]) -> int:
    spool, run_id, phase = Path(argv[0]), argv[1], argv[2]
    if spool.name != SPOOL or spool.is_symlink() or phase not in ("start", "end"):
        return 2
    binary = shutil.which("vitals")
    if not binary:
        return 1
    try:
        out = subprocess.run([binary, "limits", "--json"], capture_output=True, timeout=LATE_TIMEOUT,
                             stdin=subprocess.DEVNULL)
        data = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 1
    snaps = limit_snapshots(data if isinstance(data, dict) else None, str(uuid.UUID(run_id)), phase)
    for snap in snaps:
        snap["late"] = True
    append_records(spool, snaps)
    return 0


def limit_snapshots(data: dict | None, run_id: str, phase: str) -> list[dict]:
    """Plan limits as snapshot records. Emails become a short hash; the lead block is dropped."""
    if not isinstance(data, dict):
        return []
    measured = parse_ts(data.get("measured_at")) or now()
    rows = []
    for plan in data.get("plans") or []:
        if not isinstance(plan, dict) or not isinstance(plan.get("tool"), str):
            continue
        tool = plan["tool"].strip().lower()[:40]
        acct = account_hash(plan.get("account"))
        asked = plan.get("asked_minutes_ago")
        taken = measured - timedelta(minutes=asked) if isinstance(asked, (int, float)) and 0 <= asked < 1440 else measured
        for lim in plan.get("limits") or []:
            if not isinstance(lim, dict):
                continue
            used, kind, name = lim.get("used_percent"), lim.get("kind"), lim.get("name")
            if not isinstance(used, (int, float)) or isinstance(used, bool) or not 0 <= used <= 1000:
                continue
            if not isinstance(kind, str) or not isinstance(name, str) or not kind or not name:
                continue
            rows.append({"v": SCHEMA, "type": "snapshot", "run_id": run_id, "phase": phase,
                         "snapshot_id": str(uuid.uuid5(_NS, f"{run_id}|{phase}|{tool}|{acct}|{name}")),
                         "tool": tool, "account_hash": acct, "limit_kind": kind[:40],
                         "limit_name": name[:120], "used_percent": used,
                         "resets_at": iso(parse_ts(lim.get("resets_at"))), "taken_at": iso(taken),
                         "source": "vitals"})
    return rows


def concurrency(agents: dict | None, own_session: str | None, parent_tool: str) -> dict[str, int] | None:
    """Other agent sessions that were working: by tool. None when Vitals gave nothing.

    Claude rows carry session_id and status; Codex rows carry neither, so a Codex
    process counts when it uses at least 2% CPU, minus the Codex parent itself.
    """
    if not isinstance(agents, dict) or not isinstance(agents.get("sessions"), list):
        return None
    counts = {"claude": 0, "codex": 0}
    own = (own_session or "").removeprefix("codex:")
    for row in agents["sessions"]:
        if not isinstance(row, dict):
            continue
        agent = str(row.get("agent", "")).lower()
        if "claude" in agent:
            if row.get("status") == "working" and row.get("session_id") != own:
                counts["claude"] += 1
        elif "codex" in agent:
            cpu = row.get("cpu_percent_with_helpers")
            if row.get("status") == "working" or (isinstance(cpu, (int, float)) and cpu >= 2.0):
                counts["codex"] += 1
    if parent_tool == "codex" and counts["codex"]:
        counts["codex"] -= 1
    return counts


# ---------------------------------------------------------------- spool

def append_records(path: Path, records: Iterable[dict]) -> None:
    lines = []
    for record in records:
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        if len(line) > MAX_LINE:
            raise ValueError("dispatch record too long")
        lines.append(line.encode())
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("dispatch spool cannot be a symlink")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, b"".join(lines))
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError(f"{path.name} cannot be a symlink")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, sort_keys=True, separators=(",", ":"))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def read_spool(path: Path) -> list[dict]:
    rows = []
    try:
        if path.is_symlink():
            return rows
        with path.open(encoding="utf-8", errors="replace") as source:
            while line := source.readline(MAX_LINE + 1):
                if len(line) > MAX_LINE or not line.endswith("\n"):
                    while line and not line.endswith("\n"):
                        line = source.readline(MAX_LINE + 1)
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("v") == SCHEMA and isinstance(row.get("run_id"), str):
                    rows.append(row)
    except OSError:
        pass
    return rows


def spool_paths(parent: Path | None = None) -> list[Path]:
    root = parent or parent_dir()
    return sorted(p for p in root.glob("*/" + SPOOL) if p.is_file() and not p.is_symlink())


# ---------------------------------------------------------------- statistics

def quantile(values: list[float], q: float) -> float | None:
    data = sorted(v for v in values if v is not None and math.isfinite(v))
    if not data:
        return None
    pos = (len(data) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def _stats(values: list[float]) -> dict[str, Any]:
    return {"n": len(values), "median": _round(quantile(values, .5)), "p80": _round(quantile(values, .8))}


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------- estimate

def load_lookup(parent: Path | None = None) -> dict | None:
    data = read_json((parent or parent_dir()) / LOOKUP)
    return data if isinstance(data, dict) and data.get("v") == SCHEMA else None


def estimate(lookup: dict | None, tool: str, workers: int) -> dict[str, Any]:
    """Time and usage estimate for one new run, from the lookup file only."""
    b = bucket(workers)
    result: dict[str, Any] = {"bucket": b, "minutes": None, "slow_minutes": None, "basis_runs": 0,
                              "time_scope": None, "usage": {}, "usage_method": None,
                              "usage_basis": 0, "method_version": METHOD}
    time_table = (lookup or {}).get("time", {})
    for scope, stats in ((f"{tool} {b} workers", time_table.get(tool, {}).get(b)),
                         (f"{tool}, all sizes", time_table.get(tool, {}).get("all")),
                         ("all parents", time_table.get("all", {}).get("all"))):
        if isinstance(stats, dict) and stats.get("n", 0) and stats.get("median") is not None:
            if scope.startswith(f"{tool} {b}") and stats["n"] < MIN_BUCKET_RUNS:
                continue
            result.update(minutes=max(1, round(stats["median"])), basis_runs=stats["n"], time_scope=scope,
                          slow_minutes=max(1, round(stats.get("p80") or stats["median"])))
            break
    usage_table = (lookup or {}).get("usage", {})
    tokens = (lookup or {}).get("tokens", {}).get(tool, {})
    for key, info in usage_table.items():
        if not key.startswith(tool + ":") or not isinstance(info, dict):
            continue
        clean = info.get("buckets", {}).get(b) or {}
        ratio = info.get("pct_per_mtoken")
        tok = (tokens.get(b) or tokens.get("all") or {}).get("median_fresh")
        if clean.get("n", 0) >= MIN_CLEAN_USAGE and clean.get("median") is not None:
            result["usage"][key] = {"pct": clean["median"], "slow_pct": clean.get("p80"),
                                    "method": "clean_runs", "basis": clean["n"]}
        elif ratio is not None and tok:
            result["usage"][key] = {"pct": _round(ratio * tok / 1e6, 1), "slow_pct": None,
                                    "method": "token_ratio", "basis": info.get("n_ratio", 0)}
    return result


def _pct(value: float) -> str:
    return "<1%" if value < 1 else f"{round(value)}%"


def expect_line(est: dict[str, Any], tool: str, current: dict[str, float] | None = None,
                notes: Iterable[str] = ()) -> str:
    """One ready line: `Expect: ~25 min (slow case 40), ~6% of Claude 5h — based on 14 past runs`."""
    key = PRIMARY_LIMIT[tool]
    label = LIMIT_LABEL.get(key, key)
    if est["minutes"] is None:
        head = f"Expect: no history yet: ~{DEFAULT_MINUTES} min (default guess)"
    else:
        head = f"Expect: ~{est['minutes']} min (slow case {est['slow_minutes']})"
    usage = est["usage"].get(key)
    now_used = (current or {}).get(key)
    now_text = f", now {round(now_used)}% used" if now_used is not None else ""
    if usage:
        tail = " from tokens" if usage["method"] == "token_ratio" else ""
        head += f", ~{_pct(usage['pct'])} of {label}{tail}{now_text}"
    else:
        head += f", {label} use: no usage history{now_text}"
    if est["minutes"] is not None:
        scope = "" if est["time_scope"] and " workers" in est["time_scope"] else f" ({est['time_scope']})"
        head += f" — based on {est['basis_runs']} past run{'s' if est['basis_runs'] != 1 else ''}{scope}"
    notes = [n for n in notes if n]
    if notes:
        head += "; " + "; ".join(notes)
    return head


def current_usage(snapshots: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in snapshots:
        if s["limit_kind"] in ("session", "week"):
            key = f"{s['tool']}:{s['limit_kind']}"
            out[key] = max(out.get(key, 0), float(s["used_percent"]))
    return out


# ---------------------------------------------------------------- start / end

def start(repo: str, workers: int, *, brief_chars: int | None = None, files_claimed: int | None = None,
          tool: str | None = None, session: str | None = None, parent: Path | None = None) -> dict[str, Any]:
    parent = parent or parent_dir()
    session, tool = resolve_session(session, tool)
    run_id = str(uuid.uuid4())
    at = now()
    limits, agents, notes = read_vitals()
    snaps = limit_snapshots(limits, run_id, "start")
    if limits is not None and not snaps:
        notes.append("Vitals shows no plan limits")
    elif limits is not None and not any(s["tool"] == tool for s in snaps):
        notes.append(f"Vitals shows no {tool.title()} limits")
    conc = concurrency(agents, session, tool)
    lookup = load_lookup(parent)
    if lookup is None:
        notes.append("no estimates file yet (run `amem dispatch refresh`)")
    est = estimate(lookup, tool, workers)
    if not session:
        notes.append("no session id: pass --run-id to `amem dispatch end`")
    open_file = open_path(repo, session or "unknown", parent)
    spool = repo_dir(repo, parent) / SPOOL
    records: list[dict] = []
    previous = read_json(open_file)
    if isinstance(previous, dict) and isinstance(previous.get("run_id"), str):
        # A run that never ended: keep its start, close it as abandoned (no brief time).
        records.append(_end_record(previous, at, "abandoned", None, None))
    start_rec = {"v": SCHEMA, "type": "start", "run_id": run_id, "repo": repo, "session": session,
                 "tool": tool, "at": iso(at), "n_workers": workers, "brief_chars": brief_chars,
                 "files_claimed": files_claimed,
                 "concurrent": None if conc is None else sum(conc.values()), "concurrent_by_tool": conc,
                 "notes": notes[:5]}
    records.append(start_rec)
    records.extend(snaps)
    records.append({"v": SCHEMA, "type": "estimate", "run_id": run_id, "at": iso(at),
                    "est_minutes": est["minutes"], "est_slow_minutes": est["slow_minutes"],
                    "est_usage": {k: v["pct"] for k, v in est["usage"].items()},
                    "basis_runs": est["basis_runs"], "method_version": METHOD,
                    "time_scope": est["time_scope"]})
    append_records(spool, records)
    write_json(open_file, {**start_rec, "est_minutes": est["minutes"], "est_slow_minutes": est["slow_minutes"]})
    late_snapshot(notes, spool, run_id, "start")      # after the start record: upload order
    return {"run_id": run_id, "line": expect_line(est, tool, current_usage(snaps), notes),
            "estimate": est, "session": session, "tool": tool}


def _end_record(opened: dict, at: datetime, status: str, test_ms: int | None,
                conc: dict[str, int] | None) -> dict:
    start_conc = opened.get("concurrent_by_tool") if isinstance(opened.get("concurrent_by_tool"), dict) else None
    merged = None
    if conc is not None or start_conc is not None:
        merged = {t: max(int((conc or {}).get(t, 0)), int((start_conc or {}).get(t, 0))) for t in TOOLS}
    return {"v": SCHEMA, "type": "end", "run_id": opened["run_id"], "repo": opened.get("repo"),
            "session": opened.get("session"), "tool": opened.get("tool"),
            "dispatched_at": opened.get("at"), "n_workers": opened.get("n_workers"),
            "brief_chars": opened.get("brief_chars"), "files_claimed": opened.get("files_claimed"),
            "at": iso(at) if status != "abandoned" else None, "ended_at": iso(at), "status": status,
            "integration_test_ms": test_ms,
            "concurrent": None if merged is None else sum(merged.values()), "concurrent_by_tool": merged}


def find_open(run_id: str | None, session: str | None, parent: Path | None = None) -> Path | None:
    parent = parent or parent_dir()
    candidates = sorted(parent.glob("*/dispatch-open/*.json"))
    for path in candidates:
        if path.is_symlink():
            continue
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        if run_id and data.get("run_id") == run_id:
            return path
        if not run_id and session and data.get("session") == session:
            return path
    return None


def end(run_id: str | None = None, *, session: str | None = None, status: str = "done",
        integration_test_ms: int | None = None, parent: Path | None = None) -> dict[str, Any]:
    """Close the open run. Idempotent: a second call finds nothing open and changes nothing."""
    parent = parent or parent_dir()
    if not run_id:
        session, _ = resolve_session(session, None)
    path = find_open(run_id, session, parent)
    if path is None:
        return {"run_id": run_id, "line": "No open dispatch run" + (f" {run_id}" if run_id else
                f" for session {session}" if session else "") + ": nothing to end.", "ended": False}
    opened = read_json(path)
    at = now()
    limits, agents, notes = read_vitals()
    snaps = limit_snapshots(limits, opened["run_id"], "end")
    conc = concurrency(agents, opened.get("session"), opened.get("tool") or "claude")
    record = _end_record(opened, at, status, integration_test_ms, conc)
    append_records(path.parent.parent / SPOOL, [record, *snaps])
    path.unlink(missing_ok=True)
    late_snapshot(notes, path.parent.parent / SPOOL, opened["run_id"], "end")
    started = parse_ts(opened.get("at"))
    minutes = (at - started).total_seconds() / 60 if started else None
    run = {"tool": opened.get("tool"), "concurrent_by_tool": record["concurrent_by_tool"],
           "concurrent": record["concurrent"],
           "start": [s for s in read_spool(path.parent.parent / SPOOL)
                     if s.get("type") == "snapshot" and s.get("run_id") == opened["run_id"]
                     and s.get("phase") == "start"], "end": snaps}
    deltas = usage_deltas(run)
    key = PRIMARY_LIMIT.get(opened.get("tool") or "claude", "claude:session")
    line = f"Actual: {round(minutes) if minutes is not None else '?'} min"
    if opened.get("est_minutes") is not None:
        line += f" (estimate {opened['est_minutes']}, slow case {opened['est_slow_minutes']})"
    if SLOW_NOTE in notes:
        line += f", {LIMIT_LABEL.get(key, key)} use: end snapshot pending (see `amem dispatch report`)"
        notes = [n for n in notes if n != SLOW_NOTE]
    elif key in deltas:
        d = deltas[key]
        line += f", {LIMIT_LABEL.get(key, key)} +{_round(d['delta'], 1)}%"
        line += " (clean)" if d["clean"] else " (not clean: " + d["why"] + ")"
    else:
        line += f", {LIMIT_LABEL.get(key, key)} use not measured"
    if notes:
        line += "; " + "; ".join(notes)
    return {"run_id": opened["run_id"], "line": line, "ended": True, "minutes": minutes, "usage": deltas}


# ---------------------------------------------------------------- actuals

def usage_deltas(run: dict) -> dict[str, dict]:
    """Per limit key: the largest same-window delta over accounts, and whether it is clean."""
    ends = {(s["tool"], s["account_hash"], s["limit_name"]): s for s in run.get("end", [])}
    by_tool = run.get("concurrent_by_tool") if isinstance(run.get("concurrent_by_tool"), dict) else None
    out: dict[str, dict] = {}
    for s in run.get("start", []):
        e = ends.get((s["tool"], s["account_hash"], s["limit_name"]))
        if e is None:
            continue
        kind = s["limit_kind"]
        key = f"{s['tool']}:{kind}" if kind in ("session", "week") else f"{s['tool']}:{kind}:{s['limit_name']}"
        rs, re_ = parse_ts(s.get("resets_at")), parse_ts(e.get("resets_at"))
        same = (rs is None and re_ is None) or (rs is not None and re_ is not None
                                                and abs((re_ - rs).total_seconds()) <= SAME_WINDOW_S)
        others = by_tool.get(s["tool"]) if by_tool is not None else run.get("concurrent")
        delta = float(e["used_percent"]) - float(s["used_percent"])
        why = ("limit reset during the run" if not same else
               "concurrency unknown" if others is None else
               f"{others} other {s['tool'].title()} session(s) working" if others else "")
        item = {"delta": max(0.0, delta) if same else delta, "clean": same and others == 0,
                "same_window": same, "why": why}
        prev = out.get(key)
        if prev is None or (item["same_window"], item["delta"]) > (prev["same_window"], prev["delta"]):
            out[key] = item
    return out


def collect_runs(parent: Path | None = None, repo: str | None = None) -> dict[str, dict]:
    runs: dict[str, dict] = {}
    for path in spool_paths(parent):
        for row in read_spool(path):
            run = runs.setdefault(row["run_id"], {"run_id": row["run_id"], "start": [], "end": []})
            kind = row.get("type")
            if kind == "start":
                run.update(repo=row.get("repo"), session=row.get("session"), tool=row.get("tool"),
                           dispatched_at=row.get("at"), n_workers=row.get("n_workers"),
                           start_concurrent=row.get("concurrent_by_tool"))
            elif kind == "end":
                for k in ("repo", "session", "tool", "n_workers"):
                    run.setdefault(k, row.get(k))
                run.setdefault("dispatched_at", row.get("dispatched_at"))
                run.update(briefed_at=row.get("at"), status=row.get("status"),
                           integration_test_ms=row.get("integration_test_ms"),
                           concurrent=row.get("concurrent"), concurrent_by_tool=row.get("concurrent_by_tool"))
            elif kind == "snapshot" and row.get("phase") in ("start", "end"):
                run[row["phase"]].append(row)
            elif kind == "estimate":
                run["estimate"] = row
            elif kind == "tokens":
                run.update(tokens_fresh=row.get("tokens_fresh"), tokens_cache_read=row.get("tokens_cache_read"))
    if repo:
        runs = {k: v for k, v in runs.items() if v.get("repo") == repo}
    for run in runs.values():
        a, b = parse_ts(run.get("dispatched_at")), parse_ts(run.get("briefed_at"))
        run["minutes"] = (b - a).total_seconds() / 60 if a and b and b >= a else None
    return runs


def task_rounds(parent: Path | None = None) -> list[dict]:
    """tasks.jsonl rounds (first spawn -> last handback). They have no brief time."""
    rounds = []
    for path in sorted((parent or parent_dir()).glob("*/tasks.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        for row in read_spool_plain(path):
            started, ended = parse_ts(row.get("started")), parse_ts(row.get("ended"))
            workers = row.get("n_workers")
            if not started or not ended or ended < started or not isinstance(workers, int):
                continue
            session = str(row.get("session") or "")
            model = str(row.get("parent_model") or "")
            tool = "codex" if session.startswith("codex:") or model.startswith(("gpt", "codex")) else "claude"
            rounds.append({"repo": row.get("repo") or path.parent.name, "session": session, "tool": tool,
                           "started": started, "ended": ended, "n_workers": workers,
                           "minutes_raw": (ended - started).total_seconds() / 60})
    return rounds


def read_spool_plain(path: Path) -> list[dict]:
    rows = []
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            while line := source.readline(MAX_LINE * 8 + 1):
                if not line.endswith("\n") and len(line) > MAX_LINE * 8:
                    while line and not line.endswith("\n"):
                        line = source.readline(MAX_LINE * 8 + 1)
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def _covered(round_: dict, runs: Iterable[dict]) -> bool:
    for run in runs:
        a, b = parse_ts(run.get("dispatched_at")), parse_ts(run.get("briefed_at"))
        if a and run.get("session") == round_["session"] and a - timedelta(minutes=10) <= round_["started"] \
                and (b is None or round_["started"] <= b):
            return True
    return False


def build_lookup(runs: dict[str, dict], rounds: list[dict], *, test_ms: dict[str, float] | None = None,
                 made_at: datetime | None = None) -> dict[str, Any]:
    test_ms = dict(test_ms or {})
    measured: dict[str, list[float]] = {}    # measured integration tests beat the database median
    for run in runs.values():
        if isinstance(run.get("integration_test_ms"), (int, float)) and run.get("repo"):
            measured.setdefault(run["repo"], []).append(float(run["integration_test_ms"]))
    for repo, values in measured.items():
        test_ms[repo] = quantile(values, .5)
    samples: list[tuple[str, str, float, bool]] = []     # tool, bucket, minutes, backfill
    finished = [r for r in runs.values() if r.get("minutes") is not None and r.get("status") != "abandoned"
                and r.get("tool") in TOOLS and isinstance(r.get("n_workers"), int)]
    for r in finished:
        samples.append((r["tool"], bucket(r["n_workers"]), r["minutes"], False))
    backfilled = 0
    for rd in rounds:
        if _covered(rd, runs.values()):
            continue
        extra = (test_ms.get(rd["repo"]) or 0) / 60000     # a repo without a known test time adds nothing
        samples.append((rd["tool"], bucket(rd["n_workers"]), rd["minutes_raw"] + extra, True))
        backfilled += 1
    time_table: dict[str, dict[str, dict]] = {}
    for tool in TOOLS:
        mine = [s for s in samples if s[0] == tool]
        if not mine:
            continue
        table = {}
        for b in ("1", "2", "3-4", "5+", "all"):
            vals = [s for s in mine if b == "all" or s[1] == b]
            if vals:
                table[b] = {**_stats([v[2] for v in vals]), "backfill": sum(v[3] for v in vals)}
        time_table[tool] = table
    if samples:
        time_table["all"] = {"all": {**_stats([s[2] for s in samples]), "backfill": sum(s[3] for s in samples)}}

    usage: dict[str, dict] = {}
    tokens: dict[str, dict[str, dict]] = {}
    for r in finished:
        r["deltas"] = usage_deltas(r)
        tf = r.get("tokens_fresh")
        if isinstance(tf, (int, float)) and tf > 0:
            tokens.setdefault(r["tool"], {}).setdefault(bucket(r["n_workers"]), []).append(float(tf))
            tokens[r["tool"]].setdefault("all", []).append(float(tf))
        for key, d in r["deltas"].items():
            if not d["clean"]:
                continue
            entry = usage.setdefault(key, {"buckets": {}, "_ratios": []})
            entry["buckets"].setdefault(bucket(r["n_workers"]), []).append(d["delta"])
            entry["buckets"].setdefault("all", []).append(d["delta"])
            if isinstance(tf, (int, float)) and tf > 0:
                entry["_ratios"].append(d["delta"] / (tf / 1e6))
    for key, entry in usage.items():
        ratios = entry.pop("_ratios")
        entry["buckets"] = {b: _stats(v) for b, v in entry["buckets"].items()}
        entry["pct_per_mtoken"] = _round(quantile(ratios, .5), 4)
        entry["n_ratio"] = len(ratios)
    token_table = {tool: {b: {"n": len(v), "median_fresh": _round(quantile(v, .5), 0)} for b, v in by.items()}
                   for tool, by in tokens.items()}
    return {"v": SCHEMA, "method_version": METHOD, "made_at": iso(made_at or now()),
            "runs": len(finished), "backfill_rounds": backfilled,
            "clean_usage_runs": sum(any(d["clean"] for d in r["deltas"].values()) for r in finished),
            "time": time_table, "usage": usage, "tokens": token_table,
            "test_ms": {k: _round(v, 0) for k, v in test_ms.items() if v is not None}}


def refresh(parent: Path | None = None, *, repo: str | None = None,
            db: Any = None) -> dict[str, Any]:
    """Recompute the lookup file. `db` (optional) supplies tokens and test medians.

    db: an object with `test_medians() -> dict[repo, ms]` and
    `tokens(run) -> (fresh, cache_read, workers) | None`; errors are skipped.
    """
    parent = parent or parent_dir()
    previous = load_lookup(parent) or {}
    runs = collect_runs(parent)
    test_ms = dict(previous.get("test_ms") or {})
    notes = []
    if db is not None:
        try:
            test_ms.update(db.test_medians())
        except Exception as exc:   # offline or schema drift: keep the previous medians
            notes.append(f"test medians skipped: {type(exc).__name__}")
        added = []
        for run in runs.values():
            if run.get("tokens_fresh") is not None or run.get("minutes") is None or not run.get("session"):
                continue
            if repo and run.get("repo") != repo:
                continue
            try:
                found = db.tokens(run)
            except Exception as exc:
                notes.append(f"tokens skipped: {type(exc).__name__}")
                break
            if found:
                fresh, cache_read, workers = found
                run.update(tokens_fresh=fresh, tokens_cache_read=cache_read)
                added.append((run, {"v": SCHEMA, "type": "tokens", "run_id": run["run_id"],
                                    "tokens_fresh": fresh, "tokens_cache_read": cache_read,
                                    "sessions_counted": workers, "scope": "workers"}))
        for run, record in added:
            append_records(repo_dir(run["repo"], parent) / SPOOL, [record])
    lookup = build_lookup(runs, task_rounds(parent), test_ms=test_ms)
    write_json(parent / LOOKUP, lookup)
    lookup["notes"] = notes
    return lookup


def refresh_line(lookup: dict) -> str:
    parts = []
    for tool in TOOLS:
        table = lookup["time"].get(tool)
        if table:
            parts.append(tool + " " + ", ".join(f"{b}:n={v['n']} med={round(v['median'])}m p80={round(v['p80'])}m"
                                                for b, v in table.items() if b != "all"))
    usage = ", ".join(f"{k} clean n={v['buckets'].get('all', {}).get('n', 0)}" for k, v in lookup["usage"].items())
    return (f"Refreshed: {lookup['runs']} measured runs, {lookup['backfill_rounds']} backfill rounds, "
            f"{lookup['clean_usage_runs']} clean usage runs. " + "; ".join(parts)
            + (f". Usage: {usage}" if usage else ". Usage: no clean runs yet"))


def report(parent: Path | None = None, limit: int = 20) -> list[dict]:
    runs = [r for r in collect_runs(parent).values() if r.get("minutes") is not None]
    runs.sort(key=lambda r: r.get("dispatched_at") or "", reverse=True)
    out = []
    for r in runs[:limit]:
        est = r.get("estimate") or {}
        deltas = usage_deltas(r)
        key = PRIMARY_LIMIT.get(r.get("tool") or "claude", "claude:session")
        actual_use = deltas.get(key)
        est_use = (est.get("est_usage") or {}).get(key)
        out.append({"run_id": r["run_id"], "repo": r.get("repo"), "tool": r.get("tool"),
                    "workers": r.get("n_workers"), "dispatched_at": r.get("dispatched_at"),
                    "est_minutes": est.get("est_minutes"), "actual_minutes": _round(r["minutes"], 1),
                    "time_ratio": _round(r["minutes"] / est["est_minutes"], 2) if est.get("est_minutes") else None,
                    "limit": key, "est_pct": est_use,
                    "actual_pct": _round(actual_use["delta"], 1) if actual_use else None,
                    "clean": actual_use["clean"] if actual_use else None})
    return out


if __name__ == "__main__":   # the detached late-snapshot helper only
    import sys
    if sys.argv[1:2] == ["late-snapshot"] and len(sys.argv) == 5:
        try:
            sys.exit(_late_main(sys.argv[2:]))
        except (OSError, ValueError):
            sys.exit(1)
    sys.exit(2)
