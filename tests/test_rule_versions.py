"""rule_versions: when a rule's own text last changed, from git history (docs/rsi/hand-edits.md). No database."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agent_memory import rule_versions as RV

DOC = "# Worker\n\n## Rules\n\n- a\n\n## Push\n\n- b\n"


def _commit(repo: Path, msg: str, when: int) -> None:
    env = {**os.environ, "GIT_AUTHOR_DATE": f"@{when} +0000", "GIT_COMMITTER_DATE": f"@{when} +0000"}
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", msg],
                   check=True, env=env)


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    f = repo / "worker.md"
    f.write_text(DOC)
    subprocess.run(["git", "-C", str(repo), "add", "worker.md"], check=True)
    _commit(repo, "base", 1_000_000)
    return repo, f


def test_only_a_change_to_the_rule_itself_counts(tmp_path: Path) -> None:
    repo, f = _repo(tmp_path)
    f.write_text(DOC.replace("- b", "- b, reworded"))             # another rule changes
    _commit(repo, "push edit", 2_000_000)
    assert RV.changes(f, "rules") == []                            # Rules unchanged since its first commit
    assert RV.changed_at(f, "push") == datetime.fromtimestamp(2_000_000, UTC)
    f.write_text(f.read_text().replace("- a", "- a, by hand"))
    _commit(repo, "my edit", 3_000_000)
    assert RV.changed_at(f, "rules") == datetime.fromtimestamp(3_000_000, UTC)
    assert RV.changes(f, "push") == [datetime.fromtimestamp(2_000_000, UTC)]


def test_an_uncommitted_edit_counts_at_the_file_mtime(tmp_path: Path) -> None:
    repo, f = _repo(tmp_path)
    f.write_text(DOC.replace("- a", "- a, not committed yet"))
    os.utime(f, (4_000_000, 4_000_000))
    assert RV.changed_at(f, "rules") == datetime.fromtimestamp(4_000_000, UTC)


def test_no_history_means_no_filter(tmp_path: Path) -> None:
    loose = tmp_path / "CLAUDE.md"
    loose.write_text(DOC)
    assert RV.changed_at(loose, "rules") is None                   # not in git
    _, f = _repo(tmp_path)
    assert RV.changed_at(f, "no-such-rule") is None
    assert RV.since([str(f)], None) is None
    assert RV.since([str(loose), str(tmp_path / "gone.md")], "rules") is None


def test_since_takes_the_newest_change_over_the_files(tmp_path: Path) -> None:
    repo, f = _repo(tmp_path)
    f.write_text(DOC.replace("- a", "- a2"))
    _commit(repo, "edit", 5_000_000)
    g = repo / "other.md"
    g.write_text(DOC)
    subprocess.run(["git", "-C", str(repo), "add", "other.md"], check=True)
    _commit(repo, "other base", 6_000_000)
    g.write_text(DOC.replace("- a", "- a3"))
    _commit(repo, "other edit", 7_000_000)
    assert RV.since([str(f), str(g)], "rules") == datetime.fromtimestamp(7_000_000, UTC)
