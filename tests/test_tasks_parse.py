"""`tasks_ingest` parsing: real tasks.jsonl fixtures, status mapping, missing fields,
checkpoint excerpts. No database.

tests/fixtures/tasks/ is a read-only copy of ~/.claude/parent/<repo>/tasks.jsonl and
<repo>/checkpoints/*.md for the three repos that had tasks (41 lines, 51 workers).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_memory import tasks_ingest
from agent_memory.tasks_ingest import EXCERPT_MAX, checkpoint_excerpt, parse_parent

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tasks"
REPOS = ["junk-orchestra", "pipeline-poc", "simplitics drift check"]
META_KEYS = {
    "repo", "task_id", "session", "parent_model", "parent_effort", "duration_s", "tokens",
    "dod_first_pass", "integration_first_pass", "follow_ups", "nudges", "denials", "escalations",
    "merge_conflicts", "clarifying_questions", "n_workers", "workers", "log_source",
}
WORKER_KEYS = {
    "name", "agent_id", "model", "effort", "final_status", "first_status", "denials", "denied_shapes",
    "errors", "brief_gaps", "reasons", "tool_errors", "repeat_cmds", "lessons_read_first", "tokens",
    "duration_s", "handbacks", "checkpoint_excerpt",
}


def _write_repo(parent: Path, repo: str, lines: list[Any], *, raw: str | None = None,
                checkpoints: dict[str, str] | None = None) -> Path:
    folder = parent / repo
    (folder / "checkpoints").mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else "".join(json.dumps(x) + "\n" for x in lines)
    (folder / "tasks.jsonl").write_text(text, encoding="utf-8")
    for name, body in (checkpoints or {}).items():
        (folder / "checkpoints" / f"{name}.md").write_text(body, encoding="utf-8")
    return folder


def _task(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "t-1", "repo": "r", "final_status": "DONE", "started": "2026-10-10T16:28:36.176Z",
        "ended": "2026-10-10T16:28:56.128Z", "n_workers": 1, "denials": 0, "dod_first_pass": True,
        "blocked_partial_reasons": [], "workers": [{"name": "w1", "final_status": "DONE"}],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- real fixtures


def test_real_fixture_files_parse_every_line() -> None:
    parsed = parse_parent(FIXTURES)
    assert [p.parent.name for p in parsed.files] == REPOS
    assert parsed.repos == 3
    assert parsed.unparsed == [] and parsed.incomplete == [] and parsed.duplicates == 0
    assert len(parsed.runs) == 41
    assert len({r.source_key for r in parsed.runs}) == 41
    assert sum(len(r.meta["workers"]) for r in parsed.runs) == 51
    statuses = [r.status for r in parsed.runs]
    assert statuses.count("pass") == 39 and statuses.count("fail") == 2
    for r in parsed.runs:
        assert r.source_key == r.meta["canonical_source_key"]
        assert r.meta["legacy_source_key"] == f"dispatch:{r.meta['task_id']}"
        assert META_KEYS <= set(r.meta)
        assert r.meta["repo"] in REPOS  # meta.repo is always set (lessons scope)
        assert r.meta["log_source"] == "tasks.jsonl"
        assert r.started_at is not None and r.started_at.tzinfo is not None
        assert r.finished_at is not None and r.finished_at >= r.started_at
        assert r.summary.startswith(f"{r.meta['repo']}: ")
        for w in r.meta["workers"]:
            assert WORKER_KEYS <= set(w)
            excerpt = w["checkpoint_excerpt"]
            assert excerpt is None or 0 < len(excerpt) <= EXCERPT_MAX


def test_blocked_task_maps_to_the_contract() -> None:
    by_id = {r.meta["task_id"]: r for r in parse_parent(FIXTURES).runs}
    r = by_id["t-d2dce9a006"]
    assert r.status == "fail"
    assert r.summary == ("junk-orchestra: BLOCKED, 1 worker, dod_first_pass=false, denials=1 (heredoc), "
                         "reasons=denied-command")
    assert r.failure_reason == "denied-command"
    assert r.started_at.isoformat() == "2026-10-10T16:28:36.176000+00:00"
    assert r.finished_at.isoformat() == "2026-10-10T16:28:56.128000+00:00"
    m = r.meta
    assert (m["repo"], m["task_id"], m["final_status"], m["raw_status"]) == (
        "junk-orchestra", "t-d2dce9a006", "BLOCKED", None)
    assert (m["session"], m["parent_model"], m["parent_effort"]) == (
        "7ff5f480-77aa-496c-9b6a-bd96d4d4c395", "claude-opus-5-5", "medium")
    assert (m["duration_s"], m["tokens"], m["denials"], m["escalations"], m["n_workers"]) == (19, 79026, 1, 1, 1)
    assert m["dod_first_pass"] is False and m["integration_first_pass"] is None
    (w,) = m["workers"]
    assert (w["name"], w["agent_id"], w["final_status"], w["first_status"]) == (
        "worker-d", "a0846dac3b03cb2f4", "BLOCKED", "BLOCKED")
    assert w["denied_shapes"] == {"heredoc": 1} and w["errors"] == {"isolation-refusal": 1}
    assert w["reasons"] == ["denied-command"] and w["lessons_read_first"] is False
    assert w["brief_gaps"] == ["vilmer-words", "dod", "siblings", "skills", "standing-rules"]
    assert w["checkpoint_excerpt"] is None  # junk-orchestra has no worker-d.md: skipped


def test_real_checkpoint_excerpt_attached_and_bounded() -> None:
    runs = parse_parent(FIXTURES).runs
    workers = {w["name"]: w for r in runs for w in r.meta["workers"]}
    long_text = (FIXTURES / "simplitics drift check" / "checkpoints" / "worker-dev-snapshot.md").read_text()
    assert len(long_text) > EXCERPT_MAX
    excerpt = workers["worker-dev-snapshot"]["checkpoint_excerpt"]
    assert len(excerpt) <= EXCERPT_MAX
    assert excerpt.startswith("### ") and excerpt.endswith("Status: DONE")  # the newest whole blocks
    assert excerpt in long_text
    short = (FIXTURES / "junk-orchestra" / "checkpoints" / "worker-stats.md").read_text()
    assert workers["worker-stats"]["checkpoint_excerpt"] == short.strip()


def test_parse_is_deterministic() -> None:
    a = [(r.source_key, r.meta["content_sha256"]) for r in parse_parent(FIXTURES).runs]
    b = [(r.source_key, r.meta["content_sha256"]) for r in parse_parent(FIXTURES).runs]
    assert a == b


# ---------------------------------------------------------------- status mapping


@pytest.mark.parametrize(("raw", "status", "raw_status"), [
    ("DONE", "pass", None),
    ("PARTIAL", "fail", None),
    ("BLOCKED", "fail", None),
    ("done", "pass", None),
    ("ABANDONED", "error", "ABANDONED"),
    (None, "error", None),
    (7, "error", None),
])
def test_status_mapping(tmp_path: Path, raw: Any, status: str, raw_status: str | None) -> None:
    task = _task(final_status=raw)
    if raw is None:
        del task["final_status"]
    _write_repo(tmp_path, "r", [task])
    (r,) = parse_parent(tmp_path).runs
    assert r.status == status
    assert r.meta["raw_status"] == raw_status
    word = raw if isinstance(raw, str) else "UNKNOWN"
    assert r.summary.startswith(f"r: {word}, 1 worker, ")


# ---------------------------------------------------------------- missing / odd fields


def test_minimal_line_with_only_an_id(tmp_path: Path) -> None:
    _write_repo(tmp_path, "my-repo", [{"id": "t-min"}])
    (r,) = parse_parent(tmp_path).runs
    assert r.meta["legacy_source_key"] == "dispatch:t-min" and r.status == "error"
    assert r.meta["repo"] == "my-repo"  # falls back to the folder name
    assert r.started_at is None and r.finished_at is None and r.failure_reason is None
    assert r.summary == "my-repo: UNKNOWN, 0 workers, dod_first_pass=null, denials=0"
    for key in ("session", "parent_model", "tokens", "duration_s", "n_workers", "dod_first_pass"):
        assert r.meta[key] is None
    assert r.meta["workers"] == [] and r.meta["blocked_partial_reasons"] == []


def test_mistyped_fields_are_coerced(tmp_path: Path) -> None:
    task = _task(
        id="t-odd", tokens="many", dod_first_pass="yes", started="not a time", ended="2026-10-10T18:00:00",
        blocked_partial_reasons="scope-creep", n_workers=None, denials=None,
        workers=[{"name": "w1", "denials": 2, "denied_shapes": ["heredoc"], "errors": None,
                  "reasons": "x", "brief_gaps": None, "lessons_read_first": "no"},
                 {"name": "w2", "denials": True, "denied_shapes": {"cd-chain": 1, "heredoc": 1}},
                 "not a worker"],
    )
    _write_repo(tmp_path, "r", [task])
    (r,) = parse_parent(tmp_path).runs
    assert r.meta["tokens"] is None and r.meta["dod_first_pass"] is None
    assert r.started_at is None
    assert r.finished_at.isoformat() == "2026-10-10T18:00:00+00:00"  # no offset: UTC
    assert r.failure_reason == "scope-creep"
    w1, w2 = r.meta["workers"]
    assert w1["denied_shapes"] == {} and w1["errors"] == {} and w1["reasons"] == ["x"]
    assert w1["brief_gaps"] == [] and w1["lessons_read_first"] is None
    assert w2["denials"] is None  # a bool is not a count
    # n_workers and denials fall back to the worker list in the summary.
    assert r.summary == "r: DONE, 2 workers, dod_first_pass=null, denials=2 (cd-chain, heredoc), reasons=scope-creep"


def test_unparsed_lines_and_blank_lines(tmp_path: Path) -> None:
    raw = "\n".join([
        json.dumps(_task(id="t-a")),
        "",
        "{not json",
        json.dumps([1, 2]),
        json.dumps({"repo": "r", "final_status": "DONE"}),  # no id
        json.dumps({"id": "   "}),
        json.dumps(_task(id="t-b")),
    ]) + "\n"
    folder = _write_repo(tmp_path, "r", [], raw=raw)
    parsed = parse_parent(tmp_path)
    assert [r.meta["task_id"] for r in parsed.runs] == ["t-a", "t-b"]
    path = folder / "tasks.jsonl"
    assert parsed.unparsed == [(path, 3), (path, 4), (path, 5), (path, 6)]


def test_unterminated_last_line(tmp_path: Path) -> None:
    whole = json.dumps(_task(id="t-a")) + "\n" + json.dumps(_task(id="t-b"))
    _write_repo(tmp_path, "whole", [], raw=whole)  # complete object without a newline: kept
    _write_repo(tmp_path, "partial", [], raw=json.dumps(_task(id="t-c")) + "\n" + '{"id": "t-d", "rep')
    parsed = parse_parent(tmp_path)
    assert sorted(r.meta["task_id"] for r in parsed.runs) == ["t-a", "t-b", "t-c"]
    assert parsed.unparsed == []
    assert [p.parent.name for p in parsed.incomplete] == ["partial"]


def test_repeated_task_id_keeps_the_last_line(tmp_path: Path) -> None:
    _write_repo(tmp_path, "r", [_task(id="t-a", final_status="PARTIAL"), _task(id="t-b"),
                                _task(id="t-a", final_status="DONE")])
    parsed = parse_parent(tmp_path)
    assert parsed.duplicates == 1
    assert [(r.meta["task_id"], r.status) for r in parsed.runs] == [("t-b", "pass"), ("t-a", "pass")]
    assert len(parsed.runs[-1].meta["observations"]) == 2


def test_missing_parent_dir_and_folders_without_tasks(tmp_path: Path) -> None:
    assert parse_parent(tmp_path / "nope").runs == []
    (tmp_path / "empty-repo" / "checkpoints").mkdir(parents=True)
    (tmp_path / "routines.log").write_text("x\n")
    parsed = parse_parent(tmp_path)
    assert parsed.runs == [] and parsed.repos == 0


# ---------------------------------------------------------------- checkpoint excerpts


def test_excerpt_prefers_report_sections() -> None:
    text = (
        "### 10:00 — start\nDid: lots\nNext: more\n\n"
        "Status: DONE\nSummary: did it\n"
        "Decisions: old decision\n"
        "Open: first open\n\n"
        "Decisions: chose X because Y\n  and a continuation line\n"
        "Denied: heredoc in git commit\n"
        "Evidence: pytest green\n"
    )
    assert checkpoint_excerpt(text) == (
        "Decisions: chose X because Y\n  and a continuation line\n"
        "Denied: heredoc in git commit\n"
        "Open: first open"
    )


def test_excerpt_without_sections_keeps_newest_whole_blocks() -> None:
    blocks = [f"### 10:{i:02d} — step {i}\nDid: " + "x" * 400 + f"\nNext: step {i + 1}" for i in range(6)]
    text = "\n\n".join(blocks) + "\nStatus: DONE\n"
    out = checkpoint_excerpt(text)
    assert len(out) <= EXCERPT_MAX
    assert out.startswith("### 10:03") and out.endswith("Status: DONE")
    assert "### 10:02" not in out


@pytest.mark.parametrize("limit", [50, 200, EXCERPT_MAX])
def test_excerpt_is_always_bounded(limit: int) -> None:
    huge_block = "### 10:00 — one huge block\n" + "word " * 2000
    out = checkpoint_excerpt(huge_block, limit)
    assert len(out) <= limit and out.startswith("### 10:00") and out.endswith("...")
    sections = "Decisions: " + "d" * 5000 + "\nOpen: o\n"
    assert len(checkpoint_excerpt(sections, limit)) <= limit
    assert checkpoint_excerpt("   \n\n", limit) is None


def test_excerpt_redacts_credential_shapes() -> None:
    text = ("Did: used sk-ant-api03-abcdefghijklmnop and ghp_ABCDEFGHIJKLMNOPQRST, "
            "postgresql://postgres:hunter2@db.example:5432/x, "
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig_part")
    out = checkpoint_excerpt(text)
    for secret in ("abcdefghijklmnop", "ABCDEFGHIJKLMNOPQRST", "hunter2", "eyJzdWIiOiIxMjM0NTY3ODkwIn0"):
        assert secret not in out
    assert "postgresql://[redacted]@db.example" in out


def test_worker_names_that_are_not_file_names_get_no_excerpt(tmp_path: Path) -> None:
    (tmp_path / "secret.md").write_text("Decisions: must never be read\n")
    workers = [{"name": n} for n in ("../secret", "w/../../secret", ".hidden", "ok-worker", None)]
    _write_repo(tmp_path, "r", [_task(workers=workers)],
                checkpoints={"ok-worker": "Decisions: fine\n", ".hidden": "Decisions: hidden\n"})
    (r,) = parse_parent(tmp_path).runs
    assert [w["checkpoint_excerpt"] for w in r.meta["workers"]] == [None, None, None, "Decisions: fine", None]


def test_cli_registers_ingest_tasks() -> None:
    from agent_memory import cli

    args = cli.build_parser().parse_args(["ingest", "tasks", "--parent-dir", "/tmp/p"])
    assert args.parent_dir == Path("/tmp/p")
    assert cli.build_parser().parse_args(["ingest", "tasks"]).parent_dir is None
    assert tasks_ingest.DEFAULT_PARENT_DIR == Path.home() / ".claude" / "parent"
