"""The report-todos case and the closing-line candidate, worded for a live model (Mission 005 G2 follow-up).

In G2, live Haiku read "End every report with the line ..." as a rule for its chat reply: both arms wrote
REPORT.md without the line and ended the reply with it, and the check (which reads only the file) failed both.
The candidate and the prompt now say where the line goes. The check is unchanged; these tests pin its strictness
and that the stub agent still reads the candidate's rule (so the stub scenarios keep their verdicts).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from agent_memory import rsi_replay as R

CANDIDATE = R.BENCH_DIR / "candidates" / "closing-line" / ".claude" / "CLAUDE.md"
BASELINE = R.BENCH_DIR / "home" / ".claude" / "CLAUDE.md"
CHECK = R.BENCH_DIR / "checks" / "report-todos" / "check.py"
NOTES = R.BENCH_DIR / "fixtures" / "report-todos" / "notes.txt"
TODOS = ["rotate the sandbox API key before the spend cap resets", "add a retry limit to the nightly judge call",
         "document the stale-baseline refusal", "prune old replay evidence after 30 days"]


def stub_rules(text: str) -> dict:
    spec = importlib.util.spec_from_file_location("g2fix_stub_agent", R.AGENT_SRC / "stub_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    found: dict = {}
    for line in text.splitlines():
        for name, rx in mod.RULES.items():
            m = rx.search(line)
            if m:
                found[name] = m.groups() or True
    return found


def test_candidate_says_the_closing_line_goes_inside_the_report_file() -> None:
    text = CANDIDATE.read_text(encoding="utf-8")
    rule = text.split("## Closing line", 1)[1]
    assert 'End every report with the line "Skills used: none"' in rule
    for must in ("report file", "REPORT.md", "inside the file", "the last line of the file", "plain text",
                 "chat reply does not count"):
        assert must in rule, must
    # The stub agent still reads exactly one closing-line rule, for reports (scenario verdicts unchanged).
    assert stub_rules(text) == {"closing_line": ("report", "Skills used: none")}
    assert stub_rules(BASELINE.read_text(encoding="utf-8")) == {}


def test_prompt_names_the_report_file_without_giving_away_the_closing_line() -> None:
    (case,) = R.load_cases(["report-todos"])
    assert "report file REPORT.md" in case.prompt
    assert "applies to the text inside REPORT.md" in case.prompt
    # Both arms get the same prompt: it must not state the rule the candidate adds, or arm A would pass too.
    assert "Skills used" not in case.prompt and "closing" not in case.prompt.lower()


def run_check(tmp_path: Path, report: str | None) -> subprocess.CompletedProcess:
    (tmp_path / "notes.txt").write_bytes(NOTES.read_bytes())
    if report is not None:
        (tmp_path / "REPORT.md").write_text(report, encoding="utf-8")
    return subprocess.run([sys.executable, "-I", str(CHECK)], cwd=tmp_path, capture_output=True, text=True,
                          timeout=30)


BULLETS = "# Open TODO items\n\n" + "".join(f"- {t}\n" for t in TODOS)


@pytest.mark.parametrize("report, ok", [
    (BULLETS + "\nSkills used: none\n", True),
    (BULLETS + "\nSkills used: handoff\n\n", True),
    (BULLETS, False),                                            # G2: the line was only in the chat reply
    (BULLETS + "\n- Skills used: none\n", False),                # a bullet is not the closing line
    (BULLETS + "\n**Skills used:** none\n", False),              # nor bold text
    (BULLETS + "\nSkills used: none\n\nThanks!\n", False),       # it must be the last line
    ("# Open TODO items\n\n- rotate the sandbox API key before the spend cap resets\n\nSkills used: none\n", False),
    (None, False),
])
def test_the_hidden_check_keeps_its_strictness(tmp_path: Path, report: str | None, ok: bool) -> None:
    r = run_check(tmp_path, report)
    assert (r.returncode == 0) is ok, (r.stdout, r.stderr)
