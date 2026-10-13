"""Dispatch tasks from `<parent-dir>/<repo>/tasks.jsonl` into `runs` (docs/tasks.md).

`improve record` appends one JSON object per finished dispatch to
`<parent-dir>/<repo>/tasks.jsonl`. Each worker's checkpoint file is
`<parent-dir>/<repo>/checkpoints/<worker name>.md`. Both are read only.

One `runs` row per task:

- routine = source = `dispatch`, source_key = `dispatch:<task id>`.
- status: DONE -> pass, PARTIAL and BLOCKED -> fail, anything else (or none)
  -> error with the raw word in `meta.raw_status`.
- started_at / finished_at = `started` / `ended`; summary = one deterministic
  line; failure_reason = the joined `blocked_partial_reasons`.
- meta: repo (always set), task_id, the task counters, `workers` (compact, with
  a bounded `checkpoint_excerpt` each) and `log_source = "tasks.jsonl"`.

Missing or mistyped fields never fail a line: numbers and flags become null,
lists `[]`, maps `{}`. A line is unparsed only when it is not a JSON object or
has no task id.

Idempotency: `meta.content_sha256` hashes the row as it would be written. A
re-ingest inserts nothing and changes nothing unless that hash differs (the
task line or a checkpoint excerpt changed); then the row's columns are
refreshed and its meta keys merged (other keys in meta are kept).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from agent_memory import redact as _redact
from agent_memory import store

ROUTINE = "dispatch"
SOURCE = "dispatch"
LOG_SOURCE = "tasks.jsonl"
KEY_PREFIX = "dispatch:"
TASKS_FILE = "tasks.jsonl"
DEFAULT_PARENT_DIR = Path.home() / ".claude" / "parent"
STATUS_MAP = {"DONE": "pass", "PARTIAL": "fail", "BLOCKED": "fail"}
EXCERPT_MAX = 1500
SECTION_KEYS = ("Decisions", "Denied", "Open")

TASK_INTS = ("duration_s", "tokens", "follow_ups", "nudges", "denials", "escalations",
             "merge_conflicts", "clarifying_questions", "n_workers")
TASK_BOOLS = ("dod_first_pass", "integration_first_pass")
TASK_STRS = ("session", "parent_model", "parent_effort")

WORKER_STRS = ("name", "agent_id", "model", "effort", "final_status", "first_status")
WORKER_INTS = ("denials", "tool_errors", "repeat_cmds", "fg_sleeps", "tokens", "duration_s",
               "handbacks", "nudges", "follow_ups", "open_questions")
WORKER_BOOLS = ("lessons_read_first",)
WORKER_LISTS = ("brief_gaps", "reasons")
WORKER_DICTS = ("denied_shapes", "errors")

_WS_RE = re.compile(r"\s+")
_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,127}")
_FIELD_RE = re.compile(r"([A-Z][A-Za-z /+-]{0,30}):(?:\s|$)")
_BLOCK_SPLIT_RE = re.compile(r"(?m)^(?=#{1,6} )")


@dataclass
class TaskRun:
    source_key: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    summary: str
    failure_reason: str | None
    meta: dict[str, Any]
    path: Path
    line_no: int


@dataclass
class TasksParse:
    runs: list[TaskRun] = field(default_factory=list)
    unparsed: list[tuple[Path, int]] = field(default_factory=list)  # (file, 1-based line)
    files: list[Path] = field(default_factory=list)  # tasks.jsonl files read (one per repo)
    duplicates: int = 0  # earlier lines whose task id appears again (the last line wins)
    incomplete: list[Path] = field(default_factory=list)  # files whose unterminated last line was skipped

    @property
    def repos(self) -> int:
        return len(self.files)


@dataclass
class IngestReport:
    new: int = 0
    updated: int = 0  # present, but the task line or an excerpt changed: refreshed
    unchanged: int = 0

    @property
    def present(self) -> int:
        return self.updated + self.unchanged


# ---------------------------------------------------------------- field coercion


def _int(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = _WS_RE.sub(" ", value).strip()
    return text or None


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [text for text in (_str(v) if isinstance(v, str) else _str(json.dumps(v)) for v in value) if text]


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items()}


def parse_ts(value: Any) -> datetime | None:
    """ISO 8601 (`2026-10-10T16:28:36.176Z`). No offset means UTC. Anything else: None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _flag(value: bool | None) -> str:
    return "null" if value is None else ("true" if value else "false")


# ---------------------------------------------------------------- checkpoint excerpt


# Excerpts are stored in runs.meta in the hosted database, so credential-shaped strings
# are redacted before an excerpt is built (agent_memory.redact).
redact = _redact.redact


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _sections(lines: list[str]) -> dict[str, str]:
    """The last `Decisions:` / `Denied:` / `Open:` block of each kind. A block runs
    until a blank line, a heading or the next `Field:` line."""
    found: dict[str, str] = {}
    i = 0
    while i < len(lines):
        m = _FIELD_RE.match(lines[i])
        if m and m.group(1) in SECTION_KEYS:
            j = i + 1
            while (j < len(lines) and lines[j].strip() and not lines[j].startswith("#")
                   and not _FIELD_RE.match(lines[j])):
                j += 1
            found[m.group(1)] = "\n".join(lines[i:j]).strip()
            i = j
            continue
        i += 1
    return found


def checkpoint_excerpt(text: str, limit: int = EXCERPT_MAX) -> str | None:
    """At most `limit` chars of a checkpoint file.

    Prefers the report sections (`Decisions:`, `Denied:`, `Open:`, last of each, in
    that order). Without them: the newest `### ...` checkpoint blocks that fit,
    whole (the final block holds the Status line). A last block longer than
    `limit` keeps its head. Credential-shaped strings are redacted first.
    """
    text = redact(text.replace("\r\n", "\n")).strip()
    if not text:
        return None
    sections = _sections(text.split("\n"))
    if sections:
        return _clip("\n".join(sections[k] for k in SECTION_KEYS if k in sections), limit)
    blocks = [b.strip() for b in _BLOCK_SPLIT_RE.split(text) if b.strip()]
    kept: list[str] = []
    size = 0
    for block in reversed(blocks):
        extra = len(block) + (2 if kept else 0)
        if size + extra > limit:
            break
        kept.insert(0, block)
        size += extra
    return _clip("\n\n".join(kept) if kept else blocks[-1], limit)


def read_excerpt(repo_dir: Path, name: str | None, limit: int = EXCERPT_MAX) -> str | None:
    """Excerpt of `<repo_dir>/checkpoints/<name>.md`. None when the name is not a
    plain file name or the file is missing or unreadable."""
    if not name or not _SAFE_NAME_RE.fullmatch(name):
        return None
    path = repo_dir / "checkpoints" / f"{name}.md"
    try:
        if not path.is_file():
            return None
        return checkpoint_excerpt(path.read_text(encoding="utf-8", errors="replace"), limit)
    except OSError:
        return None


# ---------------------------------------------------------------- one task


def compact_worker(raw: dict[str, Any], repo_dir: Path) -> dict[str, Any]:
    w: dict[str, Any] = {}
    for k in WORKER_STRS:
        w[k] = _str(raw.get(k))
    for k in WORKER_INTS:
        w[k] = _int(raw.get(k))
    for k in WORKER_BOOLS:
        w[k] = _bool(raw.get(k))
    for k in WORKER_LISTS:
        w[k] = _list(raw.get(k))
    for k in WORKER_DICTS:
        w[k] = _dict(raw.get(k))
    w["checkpoint_excerpt"] = read_excerpt(repo_dir, w["name"])
    return w


def summary_line(repo: str, final_status: str | None, task: dict[str, Any],
                 workers: list[dict[str, Any]], reasons: list[str]) -> str:
    """`junk-orchestra: BLOCKED, 1 worker, dod_first_pass=false, denials=1 (heredoc), reasons=denied-command`."""
    n = task.get("n_workers")
    if n is None:
        n = len(workers)
    denials = task.get("denials")
    if denials is None:
        denials = sum(w["denials"] or 0 for w in workers)
    shapes = sorted({s for w in workers for s in w["denied_shapes"]})
    line = (f"{repo}: {final_status or 'UNKNOWN'}, {n} worker{'' if n == 1 else 's'}, "
            f"dod_first_pass={_flag(task.get('dod_first_pass'))}, denials={denials}")
    if shapes:
        line += f" ({', '.join(shapes)})"
    if reasons:
        line += f", reasons={','.join(reasons)}"
    return line


def _content_sha(fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def task_run(obj: Any, repo_dir: Path, path: Path, line_no: int) -> TaskRun | None:
    """One task object to a run row. None when it is not an object or has no id."""
    if not isinstance(obj, dict):
        return None
    task_id = _str(obj.get("id"))
    if task_id is None:
        return None
    repo = _str(obj.get("repo")) or repo_dir.name
    final_status = _str(obj.get("final_status"))
    status = STATUS_MAP.get((final_status or "").upper(), "error")
    raw_workers = obj.get("workers")
    workers = [compact_worker(w, repo_dir) for w in (raw_workers if isinstance(raw_workers, list) else [])
               if isinstance(w, dict)]
    reasons = _list(obj.get("blocked_partial_reasons"))

    # raw_status is always present (null unless mapped to error), so a merge on
    # re-ingest never keeps a stale one.
    meta: dict[str, Any] = {"repo": repo, "task_id": task_id, "final_status": final_status,
                            "raw_status": final_status if status == "error" else None}
    for k in TASK_STRS:
        meta[k] = _str(obj.get(k))
    for k in TASK_INTS:
        meta[k] = _int(obj.get(k))
    for k in TASK_BOOLS:
        meta[k] = _bool(obj.get(k))
    meta["blocked_partial_reasons"] = reasons
    meta["workers"] = workers
    meta["log_source"] = LOG_SOURCE

    started, finished = parse_ts(obj.get("started")), parse_ts(obj.get("ended"))
    summary = summary_line(repo, final_status, meta, workers, reasons)
    failure_reason = ", ".join(reasons) or None
    meta["content_sha256"] = _content_sha({
        "status": status, "started_at": started, "finished_at": finished,
        "summary": summary, "failure_reason": failure_reason, "meta": meta,
    })
    return TaskRun(KEY_PREFIX + task_id, status, started, finished, summary, failure_reason,
                   meta, path, line_no)


# ---------------------------------------------------------------- files


def discover(parent_dir: Path) -> list[Path]:
    """`<parent_dir>/*/tasks.jsonl`, sorted by repo folder name."""
    if not parent_dir.is_dir():
        return []
    return sorted(p for p in parent_dir.glob(f"*/{TASKS_FILE}") if p.is_file())


def parse_file(path: Path, result: TasksParse) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    tail = lines.pop()  # "" when the file ends with a newline
    if tail.strip():
        # Unterminated last line: keep it only when it is already a whole object.
        try:
            json.loads(tail)
            lines.append(tail)
        except ValueError:
            result.incomplete.append(path)
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            result.unparsed.append((path, line_no))
            continue
        run = task_run(obj, path.parent, path, line_no)
        if run is None:
            result.unparsed.append((path, line_no))
        else:
            result.runs.append(run)


def parse_parent(parent_dir: Path) -> TasksParse:
    """Every repo's tasks.jsonl. A task id seen twice keeps its last line."""
    result = TasksParse()
    for path in discover(parent_dir):
        result.files.append(path)
        parse_file(path, result)
    by_key: dict[str, TaskRun] = {}
    for run in result.runs:
        if run.source_key in by_key:
            result.duplicates += 1
            del by_key[run.source_key]  # re-insert so the order follows the last line
        by_key[run.source_key] = run
    result.runs = list(by_key.values())
    return result


# ---------------------------------------------------------------- database

_UPSERT_SQL = """
insert into runs (routine, source, started_at, finished_at, status, summary, failure_reason,
                  meta, source_key)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (source_key) do update
    set started_at = excluded.started_at,
        finished_at = excluded.finished_at,
        status = excluded.status,
        summary = excluded.summary,
        failure_reason = excluded.failure_reason,
        meta = runs.meta || excluded.meta
    where runs.meta ->> 'content_sha256' is distinct from excluded.meta ->> 'content_sha256'
returning (xmax = 0) as inserted
"""


def ingest(runs: list[TaskRun], *, db_url: str | None = None) -> IngestReport:
    """Upsert task rows keyed by source_key, in one transaction (Postgres only)."""
    report = IngestReport()
    with store.connect(db_url) as conn:
        for r in runs:
            got = conn.execute(
                _UPSERT_SQL,
                (ROUTINE, SOURCE, r.started_at, r.finished_at, r.status, r.summary,
                 r.failure_reason, Jsonb(r.meta), r.source_key),
            ).fetchone()
            if got is None:
                report.unchanged += 1
            elif got[0]:
                report.new += 1
            else:
                report.updated += 1
    return report


def report_line(parsed: TasksParse, report: IngestReport) -> str:
    return (f"ingested {report.new} new, {report.present} already present, "
            f"{len(parsed.unparsed)} unparsed ({parsed.repos} repos)")
