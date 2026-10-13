"""Parse `~/.claude/parent/routines.log` into `runs` rows.

Line shapes (space separated, one event per line):

- Summary: `<ts> <routine> <STATUS> <free text>`. STATUS is an upper-case word
  (PASS, FAIL, SKIP, ERROR, or others such as TIE, WIN, INCONCLUSIVE).
  The free text may hold `k=v` pairs, a duration like `10s` and `report=<path>`.
- Detail: `<ts> <routine>-fail <free text>`. It belongs to the latest summary
  line with the same routine and the same ts.

Mapping rules:

- One row per summary line. PASS/FAIL/SKIP/ERROR map to pass/fail/skip/error.
  Any other status word maps to `error`, with the raw word in `meta.raw_status`.
- `meta` holds the parsed `k=v` pairs (numbers become numbers), `duration_s`,
  `log_source = "routines.log"` and `failures` (the detail texts, in order).
- Orphan detail lines (no summary with that routine and ts) are kept: each
  (ts, routine) group becomes one `fail` row with `meta.orphan_detail = true`.
- Lines that fit no shape (bad ts, no status word) are counted as unparsed and
  never written. Blank lines are ignored.
- `source = "cloud"` when the report path points into a cloud report folder
  (`.../reports/cloud/...` or `/root/...`), else `launchd`.
- `source_key = sha256(routine + "\\n" + canonical line)`. The canonical line is
  the raw line with every `report=<path>` token cut to `report=<basename>`.
  Why: one run reaches the store by two paths. `ingest git-ref` reads the line
  as published; `routine-sync` copies it into the local log with `report=`
  rewritten to `~/.claude/parent/reports/cloud/<basename>`. Both copies get the
  same key, so whichever ingest comes first wins and the other adds nothing.
- A byte-identical raw line that appears again in the file gets `"\\n#<n>"`
  appended (n = 2, 3, ...), so true repeats are kept and keys stay stable while
  the file only grows. n counts raw lines, not canonical lines: a
  routine-sync copy next to its original in the same file is the same run, so
  it gets the same key and is dropped (counted in `ParseResult.duplicates`).
- A last line without a trailing newline may still be mid-write. It is skipped
  and picked up by the next ingest.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_SOURCE = "routines.log"
STATUS_MAP = {"PASS": "pass", "FAIL": "fail", "SKIP": "skip", "ERROR": "error"}
DETAIL_SUFFIX = "-fail"

_STATUS_RE = re.compile(r"[A-Z][A-Z_]*")
_KV_RE = re.compile(r"([A-Za-z_][\w.-]*)=(\S*)")
_DURATION_RE = re.compile(r"(\d+)s")
_INT_RE = re.compile(r"-?\d+")
_FLOAT_RE = re.compile(r"-?\d+\.\d+")
_RESERVED = {"log_source", "failures", "duration_s", "raw_status", "orphan_detail"}


@dataclass
class ParsedRun:
    routine: str
    status: str
    started_at: datetime
    summary: str | None
    report_path: str | None
    source: str
    meta: dict[str, Any]
    source_key: str
    line_no: int


@dataclass
class ParseResult:
    runs: list[ParsedRun] = field(default_factory=list)
    unparsed: list[int] = field(default_factory=list)  # 1-based line numbers
    details: int = 0  # detail lines attached to a summary
    orphan_details: int = 0  # detail lines with no summary (kept as orphan rows)
    incomplete_tail: bool = False  # last line had no newline and was skipped
    duplicates: int = 0  # summary lines with the key of an earlier line (same run): dropped


def parse_ts(value: str) -> datetime | None:
    """`2026-10-12T09:00:14+0200` (or `+02:00`, `Z`). Must carry an offset."""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        pass
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


def _value(raw: str) -> Any:
    if _INT_RE.fullmatch(raw):
        return int(raw)
    if _FLOAT_RE.fullmatch(raw):
        return float(raw)
    return raw


def parse_free_text(text: str) -> tuple[dict[str, Any], str | None]:
    """k=v pairs (minus `report`) and a duration, plus the report path."""
    meta: dict[str, Any] = {}
    report: str | None = None
    for token in text.split():
        kv = _KV_RE.fullmatch(token)
        if kv:
            key, val = kv.groups()
            if key == "report":
                report = val or None
            elif key not in _RESERVED:
                meta[key] = _value(val)
            continue
        dur = _DURATION_RE.fullmatch(token)
        if dur and "duration_s" not in meta:
            meta["duration_s"] = int(dur.group(1))
    return meta, report


def _source(report: str | None) -> str:
    if report and ("/reports/cloud/" in report or report.startswith("/root/")):
        return "cloud"
    return "launchd"


_REPORT_RE = re.compile(r"(?<!\S)report=(\S*)")


def canonical_line(line: str) -> str:
    """The line with each `report=<path>` cut to `report=<basename>` (the part routine-sync keeps)."""
    return _REPORT_RE.sub(lambda m: "report=" + m.group(1).rsplit("/", 1)[-1], line)


def source_key(routine: str, line: str, occurrence: int = 1) -> str:
    """The dedupe key of a raw line: sha256(routine, canonical line, occurrence suffix)."""
    material = f"{routine}\n{canonical_line(line)}"
    if occurrence > 1:
        material += f"\n#{occurrence}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def legacy_source_key(routine: str, line: str, occurrence: int = 1) -> str:
    """The key used before canonical lines: sha256 of the raw line. Only `amem db dedupe-runs`
    needs it, to recognise rows stored with the old key."""
    material = f"{routine}\n{line}"
    if occurrence > 1:
        material += f"\n#{occurrence}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_lines(lines: list[str]) -> ParseResult:
    result = ParseResult()
    seen: Counter[tuple[str, str]] = Counter()
    keys: dict[str, ParsedRun] = {}  # summary rows by source_key
    summaries: dict[tuple[str, str], ParsedRun] = {}
    orphans: dict[tuple[str, str], ParsedRun] = {}

    def key_for(routine: str, line: str) -> str:
        seen[(routine, line)] += 1
        return source_key(routine, line, seen[(routine, line)])

    for line_no, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split(" ", 2)
        ts = parse_ts(parts[0]) if len(parts) >= 2 else None
        if ts is None or not parts[1]:
            result.unparsed.append(line_no)
            continue
        ts_raw, token = parts[0], parts[1]
        rest = parts[2] if len(parts) == 3 else ""
        third, _, text = rest.partition(" ")
        is_status = bool(_STATUS_RE.fullmatch(third))

        if token.endswith(DETAIL_SUFFIX) and len(token) > len(DETAIL_SUFFIX):
            parent = token[: -len(DETAIL_SUFFIX)]
            group = (ts_raw, parent)
            if group in summaries or not is_status:
                if group in summaries:
                    summaries[group].meta["failures"].append(rest)
                    result.details += 1
                else:
                    if group not in orphans:
                        orphans[group] = ParsedRun(
                            routine=parent, status="fail", started_at=ts, summary=None,
                            report_path=None, source="launchd",
                            meta={"log_source": LOG_SOURCE, "orphan_detail": True, "failures": []},
                            source_key=key_for(parent, line), line_no=line_no,
                        )
                        result.runs.append(orphans[group])
                    orphans[group].meta["failures"].append(rest)
                    result.orphan_details += 1
                continue

        if not is_status:
            result.unparsed.append(line_no)
            continue

        key = key_for(token, line)
        if key in keys:
            # A routine-sync copy of an earlier line (same ts, routine, verdict and fields;
            # only the report dir differs). Same run: details that follow attach to the
            # earlier row.
            summaries[(ts_raw, token)] = keys[key]
            result.duplicates += 1
            continue
        kv, report = parse_free_text(text)
        meta: dict[str, Any] = {**kv, "log_source": LOG_SOURCE, "failures": []}
        status = STATUS_MAP.get(third)
        if status is None:
            status = "error"
            meta["raw_status"] = third
        run = ParsedRun(
            routine=token, status=status, started_at=ts, summary=text or None,
            report_path=report, source=_source(report), meta=meta,
            source_key=key, line_no=line_no,
        )
        summaries[(ts_raw, token)] = keys[key] = run
        result.runs.append(run)
    return result


def read_lines(path: Path) -> tuple[list[str], bool]:
    """Complete lines of the file, and whether an unterminated last line was dropped."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    tail = lines.pop()  # "" when the file ends with a newline
    return lines, bool(tail)


def parse_file(path: Path) -> ParseResult:
    lines, incomplete = read_lines(path)
    result = parse_lines(lines)
    result.incomplete_tail = incomplete
    return result
