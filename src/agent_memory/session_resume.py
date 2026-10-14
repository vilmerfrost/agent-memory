"""Resume chains of Claude Code transcripts (docs/sessions-live.md, "Resumed sessions").

When Claude Desktop restarts a chat's CLI with `--resume`, Claude Code writes a new transcript
`<new id>.jsonl` that starts with a copy of the earlier transcript's lines. The copy keeps each
line's `uuid` and `timestamp` but writes the new `sessionId`, so the `sessionId` rule of
session_tokens does not see it. (Older Claude Code versions kept the earlier `sessionId`; that
rule still covers those.)

The rule here:

- Chain: the transcripts of one project folder whose first line `uuid` is the same. Main
  transcripts (`<project>/*.jsonl`) and subagent files (`<project>/*/subagents/agent-*.jsonl`)
  form separate chains.
- Order: the file's birth time (`st_birthtime`, macOS), else its mtime; then the file name. The
  copy is always written after the transcript it copies. The order never depends on which files
  an ingest reads or in which order.
- A transcript's copied lines: lines whose `uuid` is in an earlier transcript of its chain. Every
  other line is its own (lines without a `uuid` too). So each line counts once, for the earliest
  transcript that holds it, whatever the ingest order. A re-ingest gives the same result.
- `resumed_from`: the session id of the earlier transcript it resumed: the one that shares the
  most lines with it (a tie: the smaller one, then the later one).
- A pure copy: some lines are copied (by `uuid` or, older shape, by a foreign `sessionId`) and no
  own line is a user or assistant message. `sessions ingest` stores no row for it.

Reads only `uuid`, `sessionId` and `type`. Results are cached per file (path, mtime, size).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIRST_UUID_LINES = 50   # the first uuid is on one of the first lines (a few lines carry none)
MESSAGE_TYPES = ("user", "assistant")

_FileKey = tuple[str, int, int]
_first_cache: dict[_FileKey, str | None] = {}
_uuid_cache: dict[_FileKey, frozenset[str]] = {}


def _key(path: Path) -> _FileKey | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def read_lines(path: Path, limit: int | None = None) -> Iterable[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if limit is not None and i >= limit:
                    return
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _uuid(e: dict[str, Any]) -> str | None:
    u = e.get("uuid")
    return u if isinstance(u, str) and u else None


def first_uuid(path: Path) -> str | None:
    key = _key(path)
    if key is None:
        return None
    if key not in _first_cache:
        _first_cache[key] = next((u for e in read_lines(path, FIRST_UUID_LINES) if (u := _uuid(e))), None)
    return _first_cache[key]


def uuids(path: Path) -> frozenset[str]:
    key = _key(path)
    if key is None:
        return frozenset()
    if key not in _uuid_cache:
        _uuid_cache[key] = frozenset(u for e in read_lines(path) if (u := _uuid(e)))
    return _uuid_cache[key]


def order_key(path: Path) -> tuple[float, str]:
    try:
        st = path.stat()
    except OSError:
        return (float("inf"), path.name)
    born = getattr(st, "st_birthtime", None)
    return (float(born) if born is not None else st.st_mtime, path.name)


def _is_subagent(path: Path) -> bool:
    return path.parent.name == "subagents"


def _siblings(path: Path) -> list[Path]:
    if _is_subagent(path):
        return sorted(path.parent.parent.parent.glob("*/subagents/agent-*.jsonl"))
    return sorted(path.parent.glob("*.jsonl"))


def session_id_of(path: Path) -> str:
    """The `sessions.session_id` of a transcript file (sessions_ingest.session_row)."""
    return f"{path.parent.parent.name}/{path.stem}" if _is_subagent(path) else path.stem


@dataclass(frozen=True)
class Resume:
    """What a transcript copied from earlier transcripts of its chain."""

    copied: frozenset[str] = frozenset()   # uuids of the earlier transcripts
    resumed_from: str | None = None        # session id of the earlier transcript it resumed

    def is_copied(self, e: dict[str, Any], own_session: str | None = None) -> bool:
        """A line of an earlier transcript: its uuid is in one, or (older shape) it carries another
        `sessionId` than `own_session`."""
        u = _uuid(e)
        if u is not None and u in self.copied:
            return True
        sid = e.get("sessionId")
        return own_session is not None and isinstance(sid, str) and bool(sid) and sid != own_session

    def own(self, events: list[dict[str, Any]], own_session: str | None = None) -> list[dict[str, Any]]:
        return [e for e in events if not self.is_copied(e, own_session)]

    def pure_copy(self, events: list[dict[str, Any]], own_session: str | None = None) -> bool:
        """Some lines are copied and no own line is a message: nothing new happened here."""
        copied = own_message = False
        for e in events:
            if self.is_copied(e, own_session):
                copied = True
            elif e.get("type") in MESSAGE_TYPES:
                own_message = True
        return copied and not own_message


NONE = Resume()


def resume_of(path: Path) -> Resume:
    """The resume facts of one transcript file: empty when it starts no chain with an earlier file."""
    first = first_uuid(path)
    if first is None:
        return NONE
    mine = order_key(path)
    earlier = sorted((p for p in _siblings(path)
                      if p != path and order_key(p) < mine and first_uuid(p) == first), key=order_key)
    if not earlier:
        return NONE
    mine_uuids = uuids(path)
    copied: set[str] = set()
    for p in earlier:
        copied |= uuids(p)
    # The transcript it resumed: the most shared lines; a tie goes to the smallest file (a fork
    # of A made after A -> B -> C shares as much with each, and is A's), then the latest.
    parent = max(enumerate(earlier), key=lambda ip: (len(uuids(ip[1]) & mine_uuids), -len(uuids(ip[1])), ip[0]))[1]
    return Resume(frozenset(copied), session_id_of(parent))
