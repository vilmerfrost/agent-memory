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
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FIELDS = ("input", "output", "cache_read", "cache_creation")
SYNTHETIC_MODEL = "<synthetic>"
UNKNOWN_MODEL = "unknown"
RECENT_IDS = 64          # message ids remembered for the latest-line-wins rule (lines of one id are adjacent)
STATE_VERSION = 1


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


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

    @property
    def calls(self) -> int:
        return sum(m.get("calls", 0) for m in self.models.values())

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

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self._reset()
        if state:
            self._load(state)

    def _reset(self) -> None:
        self.summary = Summary()
        self.offset = 0
        self.size_seen = 0
        self.recent: OrderedDict[str, tuple[str, Tokens]] = OrderedDict()

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
            "recent": [[mid, model, t.as_list()] for mid, (model, t) in self.recent.items()],
        }

    # -------------------------------------------------------------- feeding

    def feed(self, e: dict[str, Any]) -> None:
        s = self.summary
        s.seen(_ts(e.get("timestamp")))
        if e.get("type") == "assistant":
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


def claude_summary(events: list[dict[str, Any]]) -> Summary:
    """Token totals of a whole Claude Code transcript (already loaded)."""
    sc = ClaudeScanner()
    for e in events:
        sc.feed(e)
    return sc.summary


def scan_claude_file(path: Path) -> Summary:
    sc = ClaudeScanner()
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
    for e in codex_sessions.own_events(events):
        s.seen(_ts(e.get("timestamp")))
        p = codex_sessions.payload(e)
        if e.get("type") == "turn_context" and isinstance(p.get("model"), str):
            model = _model(p["model"])
        elif e.get("type") == "event_msg" and p.get("type") == "token_count":
            info = p.get("info") if isinstance(p.get("info"), dict) else {}
            if not isinstance(info.get("total_token_usage"), dict):
                continue
            now = codex_usage(info["total_token_usage"])
            delta = Tokens(*(a - b for a, b in zip(now.as_list(), prev.as_list(), strict=True)))
            if min(delta.as_list()) < 0:  # the counter started over: all of it is new
                delta = now
            if delta.total:
                s.credit(model, delta, calls=1)
            prev = now
    return s
