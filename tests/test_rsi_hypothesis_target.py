"""S5's view of the target file (2026-10-26).

Night 2026-10-25: the hypothesizer prompt cut every target at 12,000 characters, mid-file and silently. The real
pepe SKILL.md is 12,370 characters, so the cut fell right after the opening fence of its Return mode block, and the
hypothesis proposed fixing an "opened, empty output block" that is complete in the file. anatomy (31,097) was
shown at 39%. These tests use files of the real sizes and shapes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_memory import rsi_night as N
from agent_memory.rsi_budget import Budget, BudgetConfig
from test_rsi_e2e_wiring import FAKE_CLAUDE

REPO = Path(__file__).resolve().parents[1]


def skill_file(size: int, fence_at: int) -> str:
    """A SKILL.md-shaped file of `size` characters with a fenced block opening near `fence_at` (pepe's shape)."""
    head = "---\nname: demo\ndescription: demo skill\n---\n\n# Demo\n\n"
    parts, i = [head], 0
    while sum(map(len, parts)) < fence_at - 200:
        parts.append(f"## Phase {i}\n\n" + ("Do the step carefully and report back. " * 20) + "\n\n")
        i += 1
    parts.append("## Return mode\n\nReturn this block and stop:\n\n```text\nIDEA 1: ...\nIDEA 2: ...\n```\n\n")
    while sum(map(len, parts)) < size:
        parts.append(f"## Tail {i}\n\n" + ("Keep it short. " * 30) + "\n\n")
        i += 1
    return "".join(parts)


def prompt_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: dict, target_text: str) -> tuple[str, N.ClaudeCliHypothesizer]:
    script, seen = tmp_path / "claude", tmp_path / "prompt.txt"
    script.write_text(FAKE_CLAUDE.replace("sys.stdin.read()", f"open({str(seen)!r}, 'w').write(sys.stdin.read())")
                      .format(py=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("AMEM_EDITS_CLAUDE", str(script))
    monkeypatch.setenv("FAKE_ENV_LOG", str(tmp_path / "env.json"))
    evidence = "BEGIN-EVIDENCE nonce\n" + ('{"i": 1}\n' * 300) + "END-EVIDENCE nonce\n"
    hyp = N.ClaudeCliHypothesizer(cfg)
    b = Budget(BudgetConfig.from_dict({"max_usd": 5.0, "model_calls": {"opus": 2}}))
    hyp.reply({"dedupe_key": "skill:demo", "target": "~/.agents/skills/demo/SKILL.md"}, evidence_text=evidence,
              targets=["~/.agents/skills/demo/SKILL.md"], cap=2, target_text=target_text, budget=b)
    prompt = seen.read_text()
    assert "END-EVIDENCE nonce" in prompt and evidence in prompt
    return prompt, hyp


def subscription_hypothesizer_cfg() -> dict:
    """The live profile's prompt limits (its model and auth stay out: the fake `claude` and this budget stand in)."""
    cfg = json.loads((REPO / "config" / "rsi-night.subscription.json").read_text())["hypothesizer"]
    return {k: cfg[k] for k in ("max_prompt_chars", "max_target_chars") if k in cfg}


@pytest.mark.parametrize("size,fence_at", [(12_370, 11_900), (10_283, 9_000), (31_097, 12_000)])
def test_real_size_skill_files_reach_the_hypothesizer_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            size: int, fence_at: int) -> None:
    text = skill_file(size, fence_at)
    prompt, hyp = prompt_for(tmp_path, monkeypatch, subscription_hypothesizer_cfg(), text)
    body = prompt.split("<<<FILE\n", 1)[1].split("FILE>>>", 1)[0]
    assert body == text + "\n" and hyp.last_left_out == 0
    assert "[truncated:" not in body


def test_a_file_too_big_is_cut_at_a_heading_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = skill_file(12_370, 11_900)
    prompt, hyp = prompt_for(tmp_path, monkeypatch, {"max_target_chars": 12_000}, text)
    body = prompt.split("<<<FILE\n", 1)[1].split("FILE>>>", 1)[0]
    assert hyp.last_left_out > 0
    assert body.count("```") % 2 == 0                       # never an open fence
    assert "[truncated: the file continues for" in body and "Return mode" in body.split("[truncated:", 1)[1]
    shown = body.split("\n[truncated:", 1)[0]
    assert text.startswith(shown) and text[len(shown):].startswith("## ")   # cut exactly at a heading
    assert 'do not judge or propose changes to what you were not shown' in prompt


def test_fit_target_text_without_headings_cuts_at_a_line() -> None:
    text = "line of text\n" * 2_000
    shown, left = N.fit_target_text(text, 5_000)
    assert len(shown) <= 5_000 and left > 0 and "[truncated:" in shown
    assert shown.split("\n[truncated:", 1)[0].endswith("line of text\n")
    assert N.fit_target_text("short", 100) == ("short", 0)


def test_subscription_profiles_give_the_hypothesizer_room_for_the_largest_skill() -> None:
    for name in ("rsi-night.subscription.json", "rsi-night.subscription-claude.json", "rsi-night.subscription-codex.json"):
        cfg = json.loads((REPO / "config" / name).read_text())["hypothesizer"]
        assert int(cfg["max_prompt_chars"]) >= 64_000, name
