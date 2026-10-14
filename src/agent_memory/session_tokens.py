"""Token counts per session (docs/sessions-live.md, "What is stored").

Claude Code transcripts: every assistant line carries `message.usage`. One API response is
written as several lines (one per content block), each repeating the usage of the same
`message.id`. A streamed response can also write a partial usage first (a small
`output_tokens`) and the final usage on a later line. So per message id the latest line
wins: it replaces what an earlier line of the same id added. Lines without an id count
once each. `<synthetic>` messages (no API call) are left out.

Codex rollouts: `event_msg` / `token_count` events hold the thread's cumulative totals
(`info.total_token_usage`); the last one is the thread's total. Codex counts cached input
inside `input_tokens`, so it is split here: `input = input_tokens - cached_input_tokens`,
`cache_read = cached_input_tokens`, `cache_creation = cache_write_input_tokens`. The model
is the one of the latest `turn_context`; each event's growth is credited to it. A forked
subagent's copy of the parent history is skipped (codex_sessions.own_events).

`ClaudeScanner` is incremental: it remembers the byte offset it read up to and its running
totals, so a hook ping reads only the new lines of a transcript. Only labels and numbers
are kept. No transcript text.

A resumed Claude Code session writes a new transcript that starts with a copy of the earlier
session's lines. Older versions keep the earlier `sessionId` on those lines (a resumed session's
subagent files are copied whole the same way); newer ones write the new `sessionId` but keep each
line's `uuid` (session_resume). Given `own_session`, the scanner skips lines whose `sessionId` is
another id; given `skip_uuids` (session_resume.Resume.copied), lines whose `uuid` is in it. The
copied lines add no tokens, turns, time, model/effort profile or outcome signals (the earlier
session's row has them). Only a copied `custom-title` still names the session, as a fallback
until the session sets its own.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The stdlib-only digest module holds the shared rules (one implementation for the Mac and the cloud):
# title redaction + cap, the outcome signal tracker and the outcome rules.
from agent_memory.amem_digest import OUTCOMES, SignalTracker, clean_title  # noqa: F401
from agent_memory.amem_digest import outcome as outcome_of

FIELDS =("input", "output", "cache_read", "cache_creation")
SYNTHETIC_MODEL = "<synthetic>"
UNKNOWN_MODEL = "unknown"
RECENT_IDS = 64          # message ids remembered for the latest-line-wins rule (lines of one id are adjacent)
STATE_VERSION = 5       # 2: title, profile (model, effort, time), outcome signals; 3: copied lines of a resumed session left out; 4: commit counted at the result, on proof; 5: lines copied with a new sessionId left out (by uuid)


IDLE_GAP_SECONDS = 600   # a gap between two events longer than this is idle time, not active time
_EFFORT_RE = re.compile(r"[a-z][a-z0-9_-]{0,30}")


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _count(value: Any) -> int | None:
    """A non-negative number as an int, None when missing or not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def is_model(name: Any) -> bool:
    """A real model name: not `<synthetic>`, not the `unknown` placeholder."""
    return isinstance(name, str) and bool(name) and name != UNKNOWN_MODEL and not name.startswith("<")


def effort_of(value: Any) -> str | None:
    """An effort level (`high`, `medium`, ...), else None. Never guessed."""
    if isinstance(value, str) and _EFFORT_RE.fullmatch(value.strip().lower()):
        return value.strip().lower()
    return None


def claude_effort(e: dict[str, Any]) -> str | None:
    """The effort of one Claude Code assistant line: `perTurnEffort`, else `effort`."""
    return effort_of(e.get("perTurnEffort")) or effort_of(e.get("effort"))


def _model(value: Any) -> str:
    if isinstance(value, str) and value.strip() and len(value) <= 100:
        return value.strip()
    return UNKNOWN_MODEL


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


@dataclass
class Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_creation

    def add(self, other: Tokens, sign: int = 1) -> None:
        for f in FIELDS:
            setattr(self, f, max(getattr(self, f) + sign * getattr(other, f), 0))

    def as_list(self) -> list[int]:
        return [getattr(self, f) for f in FIELDS]

    def as_dict(self) -> dict[str, int]:
        return {f: getattr(self, f) for f in FIELDS}

    @classmethod
    def of(cls, values: Any) -> Tokens:
        if isinstance(values, dict):
            return cls(*(_int(values.get(f)) for f in FIELDS))
        if isinstance(values, list) and len(values) == len(FIELDS):
            return cls(*(_int(v) for v in values))
        return cls()


def claude_usage(usage: Any) -> Tokens:
    """The four counts of one Claude API `usage` object."""
    u = usage if isinstance(usage, dict) else {}
    return Tokens(_int(u.get("input_tokens")), _int(u.get("output_tokens")),
                  _int(u.get("cache_read_input_tokens")), _int(u.get("cache_creation_input_tokens")))


def codex_usage(usage: Any) -> Tokens:
    """The four counts of one Codex `total_token_usage` / `last_token_usage` object."""
    u = usage if isinstance(usage, dict) else {}
    cached = _int(u.get("cached_input_tokens"))
    return Tokens(max(_int(u.get("input_tokens")) - cached, 0), _int(u.get("output_tokens")), cached,
                  _int(u.get("cache_write_input_tokens")))


@dataclass
class Summary:
    """Token totals of one session. `models`: {model: {input, output, cache_read, cache_creation, calls}}."""

    tokens: Tokens = field(default_factory=Tokens)
    models: dict[str, dict[str, int]] = field(default_factory=dict)
    turns: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    title: str | None = None   # the session's name (Claude Code `custom-title`, the last one wins)
    # Model and effort per reply (docs/sessions-live.md, "Model, effort, duration").
    model_last: str | None = None            # the model of the latest reply
    model_seen: dict[str, int] = field(default_factory=dict)   # model -> sequence number of its latest reply
    efforts: dict[str, int] = field(default_factory=dict)      # effort level -> replies
    effort_last: str | None = None
    effort_seen: dict[str, int] = field(default_factory=dict)
    seq: int = 0
    # Time. active: the sum of the gaps between consecutive events, gaps over IDLE_GAP left out.
    prev_at: datetime | None = None
    active_seconds: float = 0.0
    # Claude Code `cost-state` lines (the last one wins); None: no such line.
    api_ms: int | None = None
    tool_ms: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    # Outcome signals (amem_digest.SignalTracker): names and counts, and how the session stands.
    signals: dict[str, int] = field(default_factory=dict)
    end: str | None = None

    def outcome(self, now: datetime | None = None, kind: str | None = None) -> tuple[str, dict[str, Any]]:
        """(outcome, evidence) as of `now` (docs/sessions-live.md, "Outcome"). `kind`: the
        session kind (`worker` has its own rule)."""
        return outcome_of(self.signals, self.end, self.last_at, now, kind)

    @property
    def calls(self) -> int:
        return sum(m.get("calls", 0) for m in self.models.values())

    @property
    def model(self) -> str | None:
        """The main model: the most output tokens; a tie goes to the most recent one."""
        named = [m for m in self.models if is_model(m)]
        if not named:
            return None
        return max(named, key=lambda m: (self.models[m].get("output", 0), self.model_seen.get(m, -1), m))

    @property
    def effort(self) -> str | None:
        """The main effort: the level on the most replies; a tie goes to the most recent one."""
        if not self.efforts:
            return None
        return max(self.efforts, key=lambda e: (self.efforts[e], self.effort_seen.get(e, -1), e))

    @property
    def duration_seconds(self) -> float | None:
        if self.first_at is None or self.last_at is None:
            return None
        return round((self.last_at - self.first_at).total_seconds(), 3)

    def reply(self, model: str | None, effort: str | None) -> None:
        """One reply (an API response) with its model and effort level."""
        self.seq += 1
        if model is not None and is_model(model):
            self.model_last = model
            self.model_seen[model] = self.seq
        if effort is not None:
            self.efforts[effort] = self.efforts.get(effort, 0) + 1
            self.effort_last = effort
            self.effort_seen[effort] = self.seq

    def tick(self, ts: datetime | None) -> None:
        """An event time in file order: first/last and the active time."""
        if ts is None:
            return
        self.seen(ts)
        if self.prev_at is not None:
            gap = (ts - self.prev_at).total_seconds()
            if 0 < gap <= IDLE_GAP_SECONDS:
                self.active_seconds += gap
        if self.prev_at is None or ts > self.prev_at:
            self.prev_at = ts

    def cost_state(self, e: dict[str, Any]) -> None:
        self.api_ms = _count(e.get("totalAPIDuration"))
        self.tool_ms = _count(e.get("totalToolDuration"))
        self.lines_added = _count(e.get("totalLinesAdded"))
        self.lines_removed = _count(e.get("totalLinesRemoved"))

    def profile(self) -> dict[str, Any]:
        """Model, effort and time facts (the sessions columns of the same names)."""
        return {
            "model": self.model, "model_last": self.model_last,
            "effort": self.effort, "effort_last": self.effort_last, "efforts": dict(sorted(self.efforts.items())),
            "duration_seconds": self.duration_seconds,
            "active_seconds": round(self.active_seconds, 3) if self.first_at is not None else None,
            "api_seconds": None if self.api_ms is None else round(self.api_ms / 1000, 3),
            "tool_seconds": None if self.tool_ms is None else round(self.tool_ms / 1000, 3),
            "lines_added": self.lines_added, "lines_removed": self.lines_removed,
        }

    def credit(self, model: str, t: Tokens, sign: int = 1, calls: int = 0) -> None:
        self.tokens.add(t, sign)
        m = self.models.setdefault(model, {**Tokens().as_dict(), "calls": 0})
        for f in FIELDS:
            m[f] = max(m[f] + sign * getattr(t, f), 0)
        m["calls"] += calls

    def seen(self, ts: datetime | None) -> None:
        if ts is not None:
            self.first_at = min(self.first_at, ts) if self.first_at else ts
            self.last_at = max(self.last_at, ts) if self.last_at else ts

    def totals_dict(self) -> dict[str, Any]:
        """{input, output, cache_read, cache_creation, models} (the cloud digest shape)."""
        return {**self.tokens.as_dict(), "models": {k: dict(v) for k, v in sorted(self.models.items())}}


def _is_prompt(e: dict[str, Any]) -> bool:
    """A user line a person typed (sessions_ingest's rule: no tool result, no meta, no injected block)."""
    from agent_memory.sessions_ingest import is_human, text_of  # local: sessions_ingest imports this module

    if e.get("type") != "user" or e.get("isMeta"):
        return False
    m = e.get("message") if isinstance(e.get("message"), dict) else {}
    content = m.get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    return is_human(text_of(content))


class ClaudeScanner:
    """Running token totals of one Claude Code transcript, fed line by line or incrementally from a file."""

    def __init__(self, state: dict[str, Any] | None = None, *, own_session: str | None = None,
                 skip_uuids: frozenset[str] = frozenset()) -> None:
        self.own_session = own_session  # None: count every line
        self.skip_uuids = skip_uuids    # uuids of lines copied from an earlier transcript (session_resume)
        self._reset()
        if state:
            self._load(state)

    def _reset(self) -> None:
        self.summary = Summary()
        self.offset = 0
        self.size_seen = 0
        self.recent: OrderedDict[str, tuple[str, Tokens]] = OrderedDict()
        self.tracker = SignalTracker()
        self.summary.signals = self.tracker.signals

    # -------------------------------------------------------------- state

    def _load(self, s: dict[str, Any]) -> None:
        if s.get("v") != STATE_VERSION:
            return
        self.offset = _int(s.get("offset"))
        self.size_seen = _int(s.get("size"))
        self.summary.tokens = Tokens.of(s.get("tokens"))
        models = s.get("models") if isinstance(s.get("models"), dict) else {}
        self.summary.models = {str(k): {**Tokens.of(v).as_dict(), "calls": _int(v.get("calls"))}
                               for k, v in models.items() if isinstance(v, dict)}
        self.summary.turns = _int(s.get("turns"))
        self.summary.first_at = _ts(s.get("first_at"))
        self.summary.last_at = _ts(s.get("last_at"))
        self.summary.title = clean_title(s.get("title"))
        p = s.get("profile") if isinstance(s.get("profile"), dict) else {}
        sm = self.summary
        sm.model_last = p.get("model_last") if is_model(p.get("model_last")) else None
        sm.model_seen = {str(k): _int(v) for k, v in (p.get("model_seen") or {}).items() if is_model(k)}
        sm.efforts = {e: _int(v) for k, v in (p.get("efforts") or {}).items() if (e := effort_of(k))}
        sm.effort_last = effort_of(p.get("effort_last"))
        sm.effort_seen = {e: _int(v) for k, v in (p.get("effort_seen") or {}).items() if (e := effort_of(k))}
        sm.seq = _int(p.get("seq"))
        sm.prev_at = _ts(p.get("prev_at"))
        sm.active_seconds = float(p.get("active") or 0) if isinstance(p.get("active"), (int, float)) else 0.0
        for name in ("api_ms", "tool_ms", "lines_added", "lines_removed"):
            setattr(sm, name, _count(p.get(name)))
        self.tracker = SignalTracker(s.get("outcome"))
        sm.signals, sm.end = self.tracker.signals, self.tracker.end
        for item in s.get("recent") or []:
            if isinstance(item, list) and len(item) == 3 and isinstance(item[0], str):
                self.recent[item[0]] = (_model(item[1]), Tokens.of(item[2]))

    def state(self) -> dict[str, Any]:
        s = self.summary
        return {
            "v": STATE_VERSION, "offset": self.offset, "size": self.size_seen,
            "tokens": s.tokens.as_list(),
            "models": {k: dict(v) for k, v in s.models.items()},
            "turns": s.turns,
            "first_at": s.first_at.isoformat() if s.first_at else None,
            "last_at": s.last_at.isoformat() if s.last_at else None,
            "title": s.title,
            "profile": {"model_last": s.model_last, "model_seen": dict(s.model_seen), "efforts": dict(s.efforts),
                        "effort_last": s.effort_last, "effort_seen": dict(s.effort_seen), "seq": s.seq,
                        "prev_at": s.prev_at.isoformat() if s.prev_at else None, "active": s.active_seconds,
                        "api_ms": s.api_ms, "tool_ms": s.tool_ms, "lines_added": s.lines_added,
                        "lines_removed": s.lines_removed},
            "outcome": self.tracker.state(),
            "recent": [[mid, model, t.as_list()] for mid, (model, t) in self.recent.items()],
        }

    # -------------------------------------------------------------- feeding

    def copied(self, e: dict[str, Any]) -> bool:
        """A line of another session (the copied history of a resumed session)."""
        if self.skip_uuids and isinstance(e.get("uuid"), str) and e["uuid"] in self.skip_uuids:
            return True
        sid = e.get("sessionId")
        return self.own_session is not None and isinstance(sid, str) and bool(sid) and sid != self.own_session

    def feed(self, e: dict[str, Any]) -> None:
        s = self.summary
        if self.copied(e):
            if e.get("type") == "custom-title":
                s.title = clean_title(e.get("customTitle")) or s.title
            return
        s.tick(_ts(e.get("timestamp")))
        self.tracker.feed(e)
        s.signals, s.end = self.tracker.signals, self.tracker.end
        if e.get("type") == "custom-title":
            s.title = clean_title(e.get("customTitle")) or s.title
        elif e.get("type") == "cost-state":
            s.cost_state(e)
        elif e.get("type") == "assistant":
            m = e.get("message") if isinstance(e.get("message"), dict) else {}
            if not isinstance(m.get("usage"), dict):
                return
            model = _model(m.get("model"))
            if model == SYNTHETIC_MODEL:
                return
            t = claude_usage(m["usage"])
            mid = m.get("id") if isinstance(m.get("id"), str) and m.get("id") else None
            if mid is not None and mid in self.recent:
                old_model, old = self.recent.pop(mid)
                s.credit(old_model, old, -1, calls=-1)
            else:
                s.reply(model, claude_effort(e))  # a new response (later lines of one id are the same reply)
            s.credit(model, t, calls=1)
            if mid is not None:
                self.recent[mid] = (model, t)
                while len(self.recent) > RECENT_IDS:
                    self.recent.popitem(last=False)
        elif _is_prompt(e):
            s.turns += 1

    def scan_file(self, path: Path) -> int:
        """Read the complete lines after the stored offset. Returns the bytes read. A file
        shorter than the stored offset (replaced or rewritten) is read again from the start."""
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        if size < self.offset:
            self._reset()
        read = 0
        with open(path, "rb") as fh:
            fh.seek(self.offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break  # a line still being written: the next scan reads it whole
                read += len(raw)
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    self.feed(obj)
        self.offset += read
        self.size_seen = size
        return read


def claude_summary(events: list[dict[str, Any]], *, own_session: str | None = None,
                   skip_uuids: frozenset[str] = frozenset()) -> Summary:
    """Token totals of a whole Claude Code transcript (already loaded). `own_session`,
    `skip_uuids`: count only that session's lines (see the module doc on resumed sessions)."""
    sc = ClaudeScanner(own_session=own_session, skip_uuids=skip_uuids)
    for e in events:
        sc.feed(e)
    return sc.summary


def scan_claude_file(path: Path, *, own_session: str | None = None,
                     skip_uuids: frozenset[str] = frozenset()) -> Summary:
    sc = ClaudeScanner(own_session=own_session, skip_uuids=skip_uuids)
    sc.scan_file(path)
    return sc.summary


def codex_summary(events: list[dict[str, Any]]) -> Summary:
    """Token totals of a Codex rollout from its `token_count` events (the thread's own ones)."""
    from agent_memory import codex_sessions

    s = Summary()
    model = UNKNOWN_MODEL
    meta = codex_sessions.meta_of(events)
    if isinstance(meta.get("model"), str):
        model = _model(meta["model"])
    prev = Tokens()
    effort: str | None = None
    for e in codex_sessions.own_events(events):
        s.tick(_ts(e.get("timestamp")))
        p = codex_sessions.payload(e)
        if e.get("type") == "turn_context":
            if isinstance(p.get("model"), str):
                model = _model(p["model"])
            effort = effort_of(p.get("effort")) or effort_of(p.get("reasoning_effort")) or effort
        elif e.get("type") == "event_msg" and p.get("type") == "token_count":
            info = p.get("info") if isinstance(p.get("info"), dict) else {}
            if not isinstance(info.get("total_token_usage"), dict):
                continue
            now = codex_usage(info["total_token_usage"])
            delta = Tokens(*(a - b for a, b in zip(now.as_list(), prev.as_list(), strict=True)))
            if min(delta.as_list()) < 0:  # the counter started over: all of it is new
                delta = now
            if delta.total:
                s.reply(model, effort)
                s.credit(model, delta, calls=1)
            prev = now
    s.signals, s.end = codex_sessions.outcome_signals(events)
    return s
