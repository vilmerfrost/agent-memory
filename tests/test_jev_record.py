"""`amem jev record`: one live call saved as a fixture. The SDK client is mocked; no live call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typesafe_sdk

from agent_memory import cli, cli_cloud, jev
from conftest import base_env, run_amem

QUESTIONS_TOML = """
threshold = 0.7            # scoring keys are ignored by jev record
pass_question = "verdict"
pass_options = ["ok"]

[[questions]]
name = "verdict"
kind = "choice"
prompt = "Did the routine do its job?"
[questions.options]
ok = "All work done"
needs_action = "Something needs a human"

[[questions]]
name = "severity"
kind = "score"
prompt = "How bad is it?"
levels = ["Fine", "Minor", "Major"]

[[questions]]
name = "flag"
kind = "noul"
prompt = "Does a human need to look?"
[questions.criteria]
true = "Yes"
false = "No"
"""
STATE = "Routine cloud-audit finished. 9 checks passed, 1 failed."
RESPONSE = {
    "model": "jev-2026-09",
    "usage": {"input_tokens": 120, "output_tokens": 12},
    "answers": {
        "verdict": {"type": "choice", "choice": "needs_action", "confidence": 0.8,
                    "probabilities": {"ok": 0.2, "needs_action": 0.8}},
        "severity": {"type": "score", "score": 1.2, "confidence": 0.7,
                     "legend": {"0": "Fine", "1": "Minor", "2": "Major"},
                     "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3}},
        "flag": {"type": "noul", "noul": 0.66},
    },
}
DUMMY_KEY = "dummy-typesafe-key-never-print"


class FakeClient:
    calls: list[dict[str, Any]] = []
    response: dict[str, Any] = RESPONSE

    def __init__(self, api_key: str) -> None:
        assert api_key == DUMMY_KEY

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def system_one(self, state: Any, questions: dict[str, Any], model: str | None = None) -> Any:
        FakeClient.calls.append({"state": state, "questions": questions, "model": model})
        return typesafe_sdk.SystemOneResponse.model_validate_json(json.dumps(FakeClient.response))


@pytest.fixture
def qfile(tmp_path: Path) -> Path:
    path = tmp_path / "q.toml"
    path.write_text(QUESTIONS_TOML)
    return path


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    monkeypatch.setenv("TYPESAFE_API_KEY", DUMMY_KEY)
    monkeypatch.setenv("AMEM_JEV_LIVE", "1")
    FakeClient.calls = []
    FakeClient.response = RESPONSE
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", FakeClient)
    return FakeClient


def test_load_questions_maps_schema(qfile: Path) -> None:
    qs = cli_cloud.load_questions(qfile)
    assert qs == [
        jev.choice("verdict", "Did the routine do its job?", {"ok": "All work done",
                                                              "needs_action": "Something needs a human"}),
        jev.score("severity", "How bad is it?", ["Fine", "Minor", "Major"]),
        jev.noul("flag", "Does a human need to look?", {"true": "Yes", "false": "No"}),
    ]


@pytest.mark.parametrize(
    "toml",
    ["", "[[questions]]\nname='a'\nkind='choice'\n", "[[questions]]\nname='a'\nkind='score'\nlevels=[]\n",
     "[[questions]]\nname='a'\nkind='nope'\n", "[[questions]]\nkind='noul'\n", "not = [toml"],
)
def test_load_questions_rejects_bad_files(tmp_path: Path, toml: str) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(toml)
    with pytest.raises(jev.JevError):
        cli_cloud.load_questions(path)


def test_record_writes_fixture_that_fixture_mode_reads(live: type[FakeClient], qfile: Path, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "fixtures"
    state_file = tmp_path / "state.txt"
    state_file.write_text(STATE)
    assert cli.main(["jev", "record", "--questions", str(qfile), "--state", f"@{state_file}",
                     "--out", str(out)]) == 0
    questions = cli_cloud.load_questions(qfile)
    path = out / f"{jev.fixture_key(STATE, questions)}.json"
    assert capsys.readouterr().out.strip() == str(path)
    (call,) = live.calls
    assert call["state"] == STATE
    assert list(call["questions"]) == ["verdict", "severity", "flag"]
    assert call["model"] == "jev-1.14.0"   # pinned: TYPESAFE_DEFAULT_MODEL cannot swap it

    body = json.loads(path.read_text())
    assert body["answers"] == RESPONSE["answers"]
    assert body["model"] == "jev-2026-09" and body["usage"] == RESPONSE["usage"]
    assert body["_note"].startswith("Recorded live by `amem jev record`")

    # Fixture mode (no key, no flag) now answers from the recorded file.
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.delenv("AMEM_JEV_LIVE")
    monkeypatch.setenv("AMEM_JEV_FIXTURES_DIR", str(out))
    results = jev.ask(STATE, questions)
    assert results["verdict"].value == "needs_action"
    assert results["severity"].value == pytest.approx(1.2)
    assert results["flag"].confidence is None

    # A second record refuses to overwrite without --force (and makes no call).
    monkeypatch.setenv("TYPESAFE_API_KEY", DUMMY_KEY)
    monkeypatch.setenv("AMEM_JEV_LIVE", "1")
    assert cli.main(["jev", "record", "--questions", str(qfile), "--state", STATE, "--out", str(out)]) == 1
    assert "--force" in capsys.readouterr().err
    assert len(live.calls) == 1


def test_record_writes_nothing_on_a_mismatched_answer(live: type[FakeClient], qfile: Path,
                                                      tmp_path: Path) -> None:
    live.response = {**RESPONSE, "answers": {k: v for k, v in RESPONSE["answers"].items() if k != "flag"}}
    out = tmp_path / "fixtures"
    assert cli.main(["jev", "record", "--questions", str(qfile), "--state", STATE, "--out", str(out)]) == 1
    assert not out.exists()


@pytest.mark.parametrize("extra", [{}, {"TYPESAFE_API_KEY": DUMMY_KEY}, {"AMEM_JEV_LIVE": "1"}])
def test_record_needs_live_mode(qfile: Path, tmp_path: Path, extra: dict[str, str]) -> None:
    out = tmp_path / "fixtures"
    result = run_amem("jev", "record", "--questions", str(qfile), "--state", STATE, "--out", str(out),
                      env=base_env(**extra))
    assert result.returncode == cli_cloud.NOT_LIVE_EXIT
    assert "TYPESAFE_API_KEY" in result.stderr and "AMEM_JEV_LIVE=1" in result.stderr
    assert DUMMY_KEY not in result.stdout + result.stderr
    assert not out.exists()


def test_jev_ask_pins_the_latest_model_even_with_an_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    class Stop(Exception):
        pass

    class Capture:
        def __init__(self, api_key: str) -> None:
            pass

        def __enter__(self) -> Capture:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def system_one(self, state: Any, questions: dict[str, Any], model: str | None = None) -> Any:
            seen.append(model)
            raise Stop

    monkeypatch.setenv("TYPESAFE_API_KEY", DUMMY_KEY)
    monkeypatch.setenv("AMEM_JEV_LIVE", "1")
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "jev-2025-01")
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", Capture)
    with pytest.raises(Stop):
        jev.ask("state: x", [jev.noul("flag", "Is it flagged?")])
    assert seen == ["jev-1.14.0"]
