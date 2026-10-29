"""Usage of runs that overlapped other sessions: the run's part is its token share of the window.

Shares come from real local records only (Claude transcript messages, Codex token_count
events inside the run's window). No tokens of the run in the window: no share, no number.
"""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

import pytest

from agent_memory import dispatch_estimates as D, dispatch_share as S
from test_dispatch_estimates import T0, add_run, home  # noqa: F401  (fixture)
from test_dispatch_seed import ev, write_rollout


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AMEM_CODEX_HOME", "AMEM_PROJECTS_DIR"):
        monkeypatch.delenv(name, raising=False)


def msg(minute: float, mid: str, inp: int, out: int, created: int = 0, read: int = 999) -> dict:
    return {"type": "assistant", "timestamp": D.iso(T0 + timedelta(minutes=minute)),
            "message": {"id": mid, "usage": {"input_tokens": inp, "output_tokens": out,
                                             "cache_creation_input_tokens": created,
                                             "cache_read_input_tokens": read}}}


def transcript(projects: Path, rel: str, lines: list[dict]) -> Path:
    path = projects / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


def claude_fixture(projects: Path, session: str) -> None:
    """Window minute 0..20. Own: parent 100+10 and 50+5+20 (latest line of m2 wins),
    subagent 200+20. Other session: 400+40. Outside the window: ignored. Cache reads: ignored."""
    transcript(projects, f"-p/{session}.jsonl", [
        msg(1, "m1", 100, 10), msg(2, "m2", 1, 1), msg(3, "m2", 50, 5, created=20), msg(30, "m3", 9999, 9)])
    transcript(projects, f"-p/{session}/subagents/agent-x.jsonl", [msg(5, "s1", 200, 20)])
    transcript(projects, "-q/other.jsonl", [msg(10, "o1", 400, 40), msg(-5, "o0", 7777, 7)])


def test_claude_window_counts_own_and_all_tokens(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    claude_fixture(projects, "parent-1")
    got = S.claude_window(projects, T0, T0 + timedelta(minutes=20), "parent-1")
    own = 110 + 75 + 220
    assert got == {"own": own, "total": own + 440, "sessions": 2, "files": 3}


def test_codex_window_counts_own_thread_and_children(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    write_rollout(codex, "parent", [ev(0, (100, 0, 10)), ev(5, (600, 100, 60))], meta={"id": "parent"})
    write_rollout(codex, "child", [ev(0, (0, 0, 0)), ev(6, (300, 0, 30))],
                  meta={"id": "child", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}})
    write_rollout(codex, "other", [ev(0, (0, 0, 0)), ev(7, (900, 0, 90)), ev(40, (5000, 0, 9))], meta={"id": "other"})
    got = S.codex_window(codex, T0 + timedelta(minutes=1), T0 + timedelta(minutes=20), "codex:parent")
    # parent: (600-100+60) - (100+10) = 450; child 330; other 990 (the minute-40 event is outside)
    assert got["own"] == 450 + 330 and got["total"] == 450 + 330 + 990 and got["sessions"] == 3


def test_busy_run_is_split_by_token_share_and_labelled(home: Path) -> None:
    projects = home / "projects"
    parent = home / "parent"
    sessions = []
    for i in range(3):          # 3 busy runs, +10 points each, the run made 40% of the window's tokens
        session = f"busy-{i}"
        sessions.append(session)
        add_run(parent, workers=2, minutes=20, used=(10, 20), others=2, day=i, session=session)
        start = T0 + timedelta(days=i)
        transcript(projects, f"-p/{session}.jsonl",
                   [{**msg(0, f"a{i}", 360, 40), "timestamp": D.iso(start + timedelta(minutes=2))}])
        transcript(projects, f"-q/other-{i}.jsonl",
                   [{**msg(0, f"b{i}", 540, 60), "timestamp": D.iso(start + timedelta(minutes=3))}])
    add_run(parent, workers=2, minutes=20, used=(10, 15), others=0, day=5)        # clean: counted as is
    lookup = D.refresh(parent, codex=False)
    entry = lookup["usage"]["claude:session"]
    assert entry["buckets"]["2"]["n"] == 1                       # clean-only stats are unchanged
    assert entry["measured"]["2"]["n"] == 4 and entry["attributed"]["2"] == 3
    assert entry["measured"]["2"]["median"] == pytest.approx(4.0)  # 10 points x 0.4, three times; 5 once
    measured = [s["share"] for s in lookup["shares"].values() if s["share"] is not None]
    assert len(measured) == 3 and {round(s, 2) for s in measured} == {0.4}   # the clean run has no transcript
    est = D.estimate(D.load_lookup(parent), "claude", 2)
    assert est["usage"]["claude:session"]["method"] == "measured_runs"
    assert "(4 runs, 3 split by token share)" in D.expect_line(est, "claude")


def test_vitals_clean_run_with_other_tokens_is_split_too(home: Path) -> None:
    """Vitals status misses short bursts: transcripts decide whether the window was the run's alone."""
    parent, projects = home / "parent", home / "projects"
    rid = add_run(parent, workers=1, minutes=20, used=(10, 20), others=0, session="looked-clean")
    transcript(projects, "-p/looked-clean.jsonl", [{**msg(0, "a", 700, 50), "timestamp": D.iso(T0 + timedelta(minutes=1))}])
    transcript(projects, "-q/burst.jsonl", [{**msg(0, "b", 230, 20), "timestamp": D.iso(T0 + timedelta(minutes=2))}])
    lookup = D.refresh(parent, codex=False)
    entry = lookup["usage"]["claude:session"]
    assert lookup["shares"][rid]["share"] == 0.75 and lookup["clean_usage_runs"] == 0
    assert "1" not in entry["buckets"] and entry["measured"]["1"]["median"] == pytest.approx(7.5)


def test_only_the_run_spent_tokens_stays_clean(home: Path) -> None:
    parent, projects = home / "parent", home / "projects"
    add_run(parent, workers=1, minutes=20, used=(10, 14), others=0, session="alone")
    transcript(projects, "-p/alone.jsonl", [{**msg(0, "a", 700, 50), "timestamp": D.iso(T0 + timedelta(minutes=1))}])
    entry = D.refresh(parent, codex=False)["usage"]["claude:session"]
    assert entry["buckets"]["1"]["median"] == 4 and not entry["attributed"]


def test_no_own_tokens_means_no_share_and_no_number(home: Path) -> None:
    parent, projects = home / "parent", home / "projects"
    for i in range(3):
        add_run(parent, workers=1, minutes=20, used=(10, 20), others=1, day=i, session=f"quiet-{i}")
        transcript(projects, f"-q/other-{i}.jsonl",
                   [{**msg(0, f"b{i}", 500, 50), "timestamp": D.iso(T0 + timedelta(days=i, minutes=3))}])
    lookup = D.refresh(parent, codex=False)
    assert all(s["share"] is None and s["why_none"] for s in lookup["shares"].values())
    assert "claude:session" not in lookup["usage"]
    assert "split by token share" not in D.expect_line(D.estimate(lookup, "claude", 1), "claude")


def test_share_is_cached_after_transcripts_are_pruned(home: Path) -> None:
    parent, projects = home / "parent", home / "projects"
    rid = add_run(parent, workers=1, minutes=20, used=(10, 20), others=1, session="kept")
    transcript(projects, "-p/kept.jsonl", [{**msg(0, "k", 90, 10), "timestamp": D.iso(T0 + timedelta(minutes=1))}])
    transcript(projects, "-q/o.jsonl", [{**msg(0, "o", 90, 10), "timestamp": D.iso(T0 + timedelta(minutes=1))}])
    assert D.refresh(parent, codex=False)["shares"][rid]["share"] == 0.5
    for path in projects.glob("**/*.jsonl"):
        path.unlink()
    assert D.refresh(parent, codex=False)["shares"][rid]["share"] == 0.5


def test_missed_rounds_counts_rounds_without_a_dispatch_run(home: Path) -> None:
    parent = home / "parent"
    add_run(parent, workers=1, minutes=20, session="covered")
    rounds = [{"session": "covered", "started": T0 + timedelta(minutes=1)},
              {"session": "skipped-session", "started": T0 + timedelta(hours=2)},
              {"session": "older", "started": T0 - timedelta(days=3)}]
    rows = [{"id": str(i), "session": r["session"], "repo": "demo", "started": D.iso(r["started"]),
             "ended": D.iso(r["started"] + timedelta(minutes=10)), "n_workers": 1} for i, r in enumerate(rounds)]
    (parent / "demo").mkdir(parents=True, exist_ok=True)
    (parent / "demo" / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    lookup = D.refresh(parent, codex=False)
    assert lookup["missed"]["rounds"] == 1 and lookup["missed"]["sessions"] == ["skipped-"]
    assert "Missed: 1 dispatch round(s)" in D.refresh_line(lookup)
