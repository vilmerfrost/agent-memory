"""Usage seed for dispatch estimates, from real history only.

`amem dispatch refresh` runs this; `start` and `end` never do (no rollout scan, no
database). Every number it writes traces to source records listed next to it in the
lookup file. Too little data gives no number, never a default.

1. Codex tokens per 1% of a plan limit, from local Codex rollouts
   (`<codex home>/sessions` and `archived_sessions`, `rollout-*.jsonl`). The percent is
   account-wide and rollouts overlap in time, so one rollout's own ratio is wrong.
   Instead: one global timeline. All `token_count` token deltas of all rollouts are
   merged by time. The percent observations are grouped per limit window (`resets_at`).
   Inside one window, an interval runs from the first time one percent value was seen
   to the first time a value at least 3 points higher was seen, and its tokens are the
   deltas of ALL rollouts in (start, end]. Anchoring on the first sighting of a new
   whole percent keeps the rounding error small; 3 points keep it under about a third.
2. Tokens per dispatch round, from the `sessions` table: worker rows of each
   tasks.jsonl round (DbSource.tokens). A round with no matched worker row is skipped.

Not covered: Codex use from other devices or Codex cloud is not in local rollouts, so
an interval with such use shows too few tokens per 1%. An interval with no local tokens
at all is dropped (the use came from elsewhere).
"""
from __future__ import annotations

from bisect import bisect_right
import json
import os
from pathlib import Path
from typing import Any, Iterable

from agent_memory import dispatch_estimates as D

SEED_METHOD = "usage-seed/1"
CODEX_SOURCE = "Codex rollouts"
ROUNDS_SOURCE = "sessions table: worker rows of tasks.jsonl rounds"
MIN_POINTS = 3                # an interval must span at least 3 percentage points
CODEX_DIRS = ("sessions", "archived_sessions")
WINDOW_NAMES = {10080: "week", 300: "session"}
MAX_EVENT_LINE = 1 << 20      # token_count lines are about 1 KB; skip anything huge


def codex_home(arg: Path | None = None) -> Path:
    """--codex-home, else AMEM_CODEX_HOME, else `.codex` next to the Claude home.
    Following the Claude home means a test with a tmp AMEM_CLAUDE_HOME never reads ~/.codex."""
    if arg is not None:
        return Path(arg).expanduser()
    env = os.environ.get("AMEM_CODEX_HOME", "").strip()
    return Path(env).expanduser() if env else D.parent_dir().parent.parent / ".codex"


def rollout_files(home: Path) -> list[Path]:
    files: list[Path] = []
    for sub in CODEX_DIRS:
        root = home / sub
        if root.is_dir() and not root.is_symlink():
            files += [p for p in root.rglob("rollout-*.jsonl") if p.is_file() and not p.is_symlink()]
    return sorted(files)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _usage(raw: Any) -> tuple[int, int, int, int] | None:
    """(input, cached input, output, total) of a cumulative `total_token_usage`."""
    if not isinstance(raw, dict):
        return None
    inp, out = _int(raw.get("input_tokens")), _int(raw.get("output_tokens"))
    cached = _int(raw.get("cached_input_tokens", 0))
    if inp is None or out is None or cached is None:
        return None
    total = _int(raw.get("total_tokens"))
    return inp, cached, out, total if total is not None else inp + out


def limit_key(limit_id: str, window_minutes: int) -> str:
    name = WINDOW_NAMES.get(window_minutes, f"{window_minutes}m")
    return f"codex:{name}" if limit_id == "codex" else f"codex:{name}:{limit_id}"


def scan_rollout(path: Path, rel: str) -> tuple[list[tuple], list[tuple]]:
    """The rollout's own token_count events as (deltas, observations).

    deltas: (ts, fresh, total, file, ordinal); fresh = input - cached + output.
    observations: (limit key, resets_at, ts, used_percent, file, ordinal).
    A forked subagent copies its parent's history (ordinal < subagent_history_start_ordinal);
    those copies only set the counter baseline, so the parent's tokens are not counted twice.
    """
    deltas: list[tuple] = []
    obs: list[tuple] = []
    start_ord: int | None = None
    meta_seen = False
    prev: tuple[int, int, int, int] | None = None
    with path.open("rb") as source:
        for raw in source:
            if not meta_seen and b'"session_meta"' in raw[:200]:
                meta_seen = True
                try:
                    payload = json.loads(raw).get("payload")
                except (ValueError, AttributeError):
                    continue
                start = payload.get("subagent_history_start_ordinal") if isinstance(payload, dict) else None
                start_ord = start if isinstance(start, int) and not isinstance(start, bool) else None
                continue
            if b'"token_count"' not in raw or len(raw) > MAX_EVENT_LINE:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "event_msg":
                continue
            payload = rec.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            ts = D.parse_ts(rec.get("timestamp"))
            if ts is None:
                continue
            ordinal = rec.get("ordinal") if isinstance(rec.get("ordinal"), int) else None
            copied = start_ord is not None and ordinal is not None and ordinal < start_ord
            info = payload.get("info")
            now = _usage(info.get("total_token_usage")) if isinstance(info, dict) else None
            if now is not None:
                delta = tuple(a - b for a, b in zip(now, prev or (0, 0, 0, 0)))
                if min(delta) < 0:          # the counter started over: all of it is new
                    delta = now
                prev = now
                fresh = max(delta[0] - delta[1], 0) + delta[2]
                if not copied and (fresh or delta[3]):
                    deltas.append((ts, fresh, delta[3], rel, ordinal))
            if copied:
                continue
            limits = payload.get("rate_limits")
            if not isinstance(limits, dict):
                continue
            limit_id = limits.get("limit_id") if isinstance(limits.get("limit_id"), str) else "codex"
            for slot in ("primary", "secondary"):
                lim = limits.get(slot)
                if not isinstance(lim, dict):
                    continue
                pct, window, resets = lim.get("used_percent"), lim.get("window_minutes"), lim.get("resets_at")
                if not _num(pct) or not 0 <= pct <= 100 or _int(window) in (None, 0) or not _num(resets):
                    continue
                obs.append((limit_key(limit_id, window), float(resets), ts, float(pct), rel, ordinal))
    return deltas, obs


def _windows(rows: list[tuple]) -> list[list[tuple]]:
    """Group observations of one limit by resets_at (values within SAME_WINDOW_S are one window)."""
    out: list[list[tuple]] = []
    for row in sorted(rows, key=lambda r: r[1]):
        if out and row[1] - out[-1][-1][1] <= D.SAME_WINDOW_S:
            out[-1].append(row)
        else:
            out.append([row])
    return out


def _point(row: tuple) -> dict:
    return {"file": row[4], "at": D.iso(row[2]), "ordinal": row[5], "pct": row[3]}


def _spread(values: list[float]) -> dict[str, Any]:
    return {"median": round(D.quantile(values, .5)), "p20": round(D.quantile(values, .2)),
            "p80": round(D.quantile(values, .8))}


def codex_rate(home: Path) -> dict[str, Any]:
    """Tokens per 1% per Codex limit, with every interval's source records."""
    files = rollout_files(home)
    deltas: list[tuple] = []
    obs: list[tuple] = []
    unreadable = 0
    for path in files:
        try:
            d, o = scan_rollout(path, str(path.relative_to(home)))
        except OSError:
            unreadable += 1
            continue
        deltas += d
        obs += o
    deltas.sort(key=lambda r: (r[0], r[3], r[4] or 0))
    times = [r[0] for r in deltas]
    cum_fresh, cum_total = [0], [0]
    for r in deltas:
        cum_fresh.append(cum_fresh[-1] + r[1])
        cum_total.append(cum_total[-1] + r[2])
    by_key: dict[str, list[tuple]] = {}
    for row in obs:
        by_key.setdefault(row[0], []).append(row)
    limits: dict[str, dict] = {}
    for key, rows in sorted(by_key.items()):
        intervals: list[dict] = []
        short = no_tokens = 0
        windows = _windows(rows)
        for window in windows:
            window.sort(key=lambda r: (r[2], r[3]))
            rises: list[tuple] = []        # first sighting of each new, higher percent
            top: float | None = None
            for row in window:
                if top is not None and row[3] > top:
                    rises.append(row)
                top = row[3] if top is None else max(top, row[3])
            i = 0
            while i < len(rises):
                a = rises[i]
                j = next((k for k in range(i + 1, len(rises)) if rises[k][3] - a[3] >= MIN_POINTS), None)
                if j is None:
                    short += i + 1 < len(rises)
                    break
                b = rises[j]
                lo, hi = bisect_right(times, a[2]), bisect_right(times, b[2])
                fresh, total = cum_fresh[hi] - cum_fresh[lo], cum_total[hi] - cum_total[lo]
                points = b[3] - a[3]
                if fresh <= 0:
                    no_tokens += 1
                else:
                    intervals.append({"start": _point(a), "end": _point(b), "points": points,
                                      "fresh": fresh, "total": total,
                                      "fresh_per_pct": round(fresh / points), "total_per_pct": round(total / points),
                                      "token_events": hi - lo,
                                      "rollouts": len({deltas[k][3] for k in range(lo, hi)})})
                i = j
        entry: dict[str, Any] = {"source": CODEX_SOURCE, "method_version": SEED_METHOD, "n": len(intervals),
                                 "windows": len(windows), "observations": len(rows),
                                 "dropped_short": short, "dropped_no_local_tokens": no_tokens,
                                 "fresh_per_pct": None, "total_per_pct": None, "intervals": intervals}
        if len(intervals) >= D.MIN_SEED_INTERVALS:
            entry["fresh_per_pct"] = _spread([iv["fresh_per_pct"] for iv in intervals])
            entry["total_per_pct"] = _spread([iv["total_per_pct"] for iv in intervals])
        else:
            entry["why_none"] = (f"too thin: {len(intervals)} interval(s) of >= {MIN_POINTS} points, "
                                 f"need {D.MIN_SEED_INTERVALS}")
        limits[key] = entry
    return {"source": CODEX_SOURCE, "method_version": SEED_METHOD, "home": str(home),
            "rollouts": len(files), "unreadable": unreadable, "token_events": len(deltas),
            "limits": limits}


def round_tokens(rounds: Iterable[dict], db: Any) -> tuple[dict[str, Any], list[str]]:
    """Worker tokens per tasks.jsonl round, per parent tool and worker bucket.

    db.tokens(run) -> (fresh, cache_read, worker rows) | None; fresh = input + output +
    cache creation. A round without a matched worker row is skipped, never zeroed.
    """
    per: dict[str, list[dict]] = {}
    skipped = 0
    notes: list[str] = []
    for rd in rounds:
        if not rd.get("session"):
            skipped += 1
            continue
        try:
            found = db.tokens({"session": rd["session"], "dispatched_at": D.iso(rd["started"]),
                               "briefed_at": D.iso(rd["ended"])})
        except Exception as exc:        # offline or schema drift: say so, keep nothing half-read
            return {}, [f"round tokens skipped: {type(exc).__name__}"]
        if not found:
            skipped += 1
            continue
        fresh, cache_read, rows = found
        per.setdefault(rd["tool"], []).append({
            "repo": rd["repo"], "session": rd["session"], "started": D.iso(rd["started"]),
            "ended": D.iso(rd["ended"]), "workers": rd["n_workers"], "bucket": D.bucket(rd["n_workers"]),
            "worker_rows": rows, "fresh": fresh, "cache_read": cache_read})
    table: dict[str, Any] = {}
    for tool, recs in per.items():
        buckets: dict[str, dict] = {}
        for b in ("1", "2", "3-4", "5+", "all"):
            mine = [r for r in recs if b == "all" or r["bucket"] == b]
            if not mine:
                continue
            fresh = [float(r["fresh"]) for r in mine]
            buckets[b] = {"n": len(mine), "median_fresh": round(D.quantile(fresh, .5)),
                          "p80_fresh": round(D.quantile(fresh, .8)),
                          "median_cache_read": round(D.quantile([float(r["cache_read"]) for r in mine], .5))}
        table[tool] = {"source": ROUNDS_SOURCE, "method_version": SEED_METHOD, "buckets": buckets,
                       "rounds": recs}
    if skipped:
        notes.append(f"round tokens: {skipped} round(s) without worker rows skipped")
    return table, notes
