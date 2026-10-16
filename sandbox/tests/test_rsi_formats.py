"""RSI V1 formats in the runner: rsi-baseline/1 homes, the candidate arm built from a staged rsi-artifact/1,
and rsi-eval/1 records bound to the exact tested artifact. No Docker needed."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import rsi_sandbox as rs

EXAMPLE_MANIFEST = {
    "format": "rsi-artifact/1", "experiment_id": "exp-20261020-01", "baseline_id": "bl-000000000000",
    "proposal_kind": "edit_instruction", "activation": "none",
    "files": [{"path": "~/.claude/CLAUDE.md", "kind": "instruction", "rule_id": "claude-md-global--closing-line",
               "before_sha256": "0" * 64, "after_sha256": "f" * 64, "mode": "0644"}],
}
EXAMPLE_HASH = "3aa4f3e553c4fb750539e6b8e46604e661e244005c0d2bd7b8eed31b145c30ef"   # same vector in both repos

CLAUDE_MD = b"# Global\n\n## Closing line\n\n- End with a short summary.\n"
HOOK_SH = b"#!/bin/sh\necho hi\n"
EXP = "exp-20261016-01"


@pytest.fixture
def sb(tmp_path):
    s = rs.Sandbox(tmp_path / "sandbox")
    s.init()
    return s


def make_baseline(sb: rs.Sandbox, files: dict[str, tuple[bytes, str]]) -> str:
    """The layout agent-memory `amem rsi baseline create` writes."""
    entries = sorted(({"path": k, "sha256": rs._sha(b), "size": len(b), "mode": mode, "kind": "instruction"}
                      for k, (b, mode) in files.items()), key=lambda f: f["path"])
    body = {"format": rs.BASELINE_FORMAT, "created_at": "2026-10-16T00:00:00+00:00", "files": entries}
    bid = "bl-" + rs._sha(rs.canonical(body))[:12]
    d = sb.baselines / bid
    for k, (b, _) in files.items():
        p = d / "files" / k[2:]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)
    (d / "manifest.json").write_text(json.dumps({**body, "baseline_id": bid}, indent=1, sort_keys=True) + "\n")
    for p in sorted(d.rglob("*"), reverse=True):
        os.chmod(p, 0o555 if p.is_dir() else 0o444)
    os.chmod(d, 0o555)
    return bid


def make_artifact(sb: rs.Sandbox, bid: str, changes: dict[str, tuple[bytes | None, bytes]], exp: str = EXP) -> str:
    entries = sorted(({"path": k, "kind": "instruction", "rule_id": "demo", "mode": "0644",
                       "before_sha256": rs._sha(b) if b is not None else None, "after_sha256": rs._sha(a)}
                      for k, (b, a) in changes.items()), key=lambda f: f["path"])
    m = {"format": rs.ARTIFACT_FORMAT, "experiment_id": exp, "baseline_id": bid, "proposal_kind": "edit_instruction",
         "activation": "none", "files": entries}
    h = rs._sha(rs.canonical(m))
    d = sb.artifacts / h
    (d / "files").mkdir(parents=True)
    (d / "evals").mkdir()
    (d / "manifest.json").write_bytes(rs.canonical(m) + b"\n")
    for _, a in changes.values():
        (d / "files" / rs._sha(a)).write_bytes(a)
    return h


def test_hash_vector_matches_both_repos():
    assert rs._sha(rs.canonical(EXAMPLE_MANIFEST)) == EXAMPLE_HASH


def test_rsi_baseline_becomes_the_home_with_modes(sb):
    bid = make_baseline(sb, {"~/.claude/CLAUDE.md": (CLAUDE_MD, "0644"), "~/.claude/hooks/h.sh": (HOOK_SH, "0755")})
    rep = sb.prepare_arm(EXP, bid, "A", hook_experiment=True)
    home = sb.arm_home(EXP, "A")
    assert (home / ".claude" / "CLAUDE.md").read_bytes() == CLAUDE_MD
    assert not (home / "files").exists() and not (home / "manifest.json").exists()
    assert stat.S_IMODE((home / ".claude" / "hooks" / "h.sh").stat().st_mode) & 0o100
    assert rep["baseline_format"] == rs.BASELINE_FORMAT and rep["artifact_hash"] is None
    assert not list(sb.experiment_dir(EXP).glob(".stage-*"))                 # the stage is removed


def test_tampered_or_padded_baseline_is_refused(sb):
    bid = make_baseline(sb, {"~/.claude/CLAUDE.md": (CLAUDE_MD, "0644")})
    d = sb.baselines / bid
    os.chmod(d / "files" / ".claude", 0o755)
    extra = d / "files" / ".claude" / "settings.json"
    extra.write_text("{}")
    with pytest.raises(rs.SandboxError, match="unlisted files"):
        sb.prepare_arm(EXP, bid, "A")
    extra.unlink()
    f = d / "files" / ".claude" / "CLAUDE.md"
    os.chmod(f, 0o644)
    f.write_bytes(CLAUDE_MD + b"sneaked\n")
    with pytest.raises(rs.SandboxError, match="missing or changed"):
        sb.prepare_arm(EXP, bid, "A")
    assert not sb.arm_home(EXP, "A").exists()


def test_candidate_arm_holds_the_exact_artifact_bytes(sb):
    bid = make_baseline(sb, {"~/.claude/CLAUDE.md": (CLAUDE_MD, "0644")})
    new = CLAUDE_MD.replace(b"a short summary", b"the tools you used")
    skill = b"---\nname: demo\n---\n# Demo\n"
    h = make_artifact(sb, bid, {"~/.claude/CLAUDE.md": (CLAUDE_MD, new), "~/.agents/skills/demo/SKILL.md": (None, skill)})
    sb.prepare_arm(EXP, bid, "A")
    rep = sb.prepare_arm(EXP, bid, "B", artifact=h)
    home_b = sb.arm_home(EXP, "B")
    assert (home_b / ".claude" / "CLAUDE.md").read_bytes() == new
    assert (home_b / ".agents" / "skills" / "demo" / "SKILL.md").read_bytes() == skill
    assert (sb.arm_home(EXP, "A") / ".claude" / "CLAUDE.md").read_bytes() == CLAUDE_MD     # control unchanged
    assert rep["exact"] is True and rep["artifact_hash"] == h
    assert (sb.baselines / bid / "files" / ".claude" / "CLAUDE.md").read_bytes() == CLAUDE_MD   # baseline unchanged


def test_candidate_arm_refusals(sb):
    bid = make_baseline(sb, {"~/.claude/CLAUDE.md": (CLAUDE_MD, "0644")})
    new = CLAUDE_MD + b"- more\n"
    stale = make_artifact(sb, bid, {"~/.claude/CLAUDE.md": (b"older bytes\n", new)})
    with pytest.raises(rs.SandboxError, match="stale artifact"):
        sb.prepare_arm(EXP, bid, "B", artifact=stale)
    other = make_artifact(sb, bid, {"~/.claude/CLAUDE.md": (CLAUDE_MD, new)}, exp="exp-20261016-99")
    with pytest.raises(rs.SandboxError, match="belongs to experiment"):
        sb.prepare_arm(EXP, bid, "B", artifact=other)
    h = make_artifact(sb, bid, {"~/.claude/CLAUDE.md": (CLAUDE_MD, new)})
    (sb.artifacts / h / "files" / rs._sha(new)).write_bytes(new + b"sneaked\n")
    with pytest.raises(rs.SandboxError, match="changed since staging"):
        sb.prepare_arm(EXP, bid, "B", artifact=h)
    with pytest.raises(rs.SandboxError, match="path must start with"):
        rs._home_rel("/etc/passwd")
    with pytest.raises(rs.SandboxError, match="unsafe path"):
        rs._home_rel("~/../.ssh/id_ed25519")
    assert not sb.arm_home(EXP, "B").exists()


def test_record_eval_is_bound_to_the_tested_artifact(sb):
    bid = make_baseline(sb, {"~/.claude/CLAUDE.md": (CLAUDE_MD, "0644")})
    new = CLAUDE_MD + b"- more\n"
    h = make_artifact(sb, bid, {"~/.claude/CLAUDE.md": (CLAUDE_MD, new)})
    with pytest.raises(rs.SandboxError, match="no sanitize report"):
        sb.record_eval(artifact=h, experiment=EXP, arm="B", suite="sandbox-replay", verdict="pass")
    sb.prepare_arm(EXP, bid, "A")
    sb.prepare_arm(EXP, bid, "B")                                           # not from the artifact
    with pytest.raises(rs.SandboxError, match="not prepared from this artifact"):
        sb.record_eval(artifact=h, experiment=EXP, arm="B", suite="sandbox-replay", verdict="pass")
    sb.prepare_arm(EXP, bid, "B", artifact=h, force=True)
    sb.prepare_arm(EXP, bid, "A", artifact=h, force=True)                   # control must be the plain baseline
    with pytest.raises(rs.SandboxError, match="control home-A"):
        sb.record_eval(artifact=h, experiment=EXP, arm="B", suite="sandbox-replay", verdict="pass")
    sb.prepare_arm(EXP, bid, "A", force=True)
    out = sb.record_eval(artifact=h, experiment=EXP, arm="B", suite="sandbox-replay", verdict="pass",
                         results={"cases": 12, "passed": 12}, model="claude-opus-5-5", effort="medium")
    rec = json.loads(out.read_text())
    assert out == sb.artifacts / h / "evals" / "sandbox-replay.json"
    assert out.read_bytes() == rs.canonical(rec) + b"\n"
    assert (rec["artifact_hash"], rec["baseline_id"], rec["experiment_id"], rec["verdict"]) == (h, bid, EXP, "pass")
    assert rec["results"]["candidate_arm"] == "B" and rec["model"] == "claude-opus-5-5"
    with pytest.raises(rs.SandboxError, match="exists"):
        sb.record_eval(artifact=h, experiment=EXP, arm="B", suite="sandbox-replay", verdict="fail")
    with pytest.raises(rs.SandboxError, match="bad suite or verdict"):
        sb.record_eval(artifact=h, experiment=EXP, arm="B", suite="sandbox-replay", verdict="great")


def test_sanitized_settings_artifact_cannot_be_evaluated_as_staged(sb):
    settings = b'{"hooks": {}}\n'
    bid = make_baseline(sb, {"~/.claude/settings.json": (settings, "0644")})
    new = b'{"hooks": {}, "env": {"X": "1"}}\n'
    h = make_artifact(sb, bid, {"~/.claude/settings.json": (settings, new)})
    sb.prepare_arm(EXP, bid, "A")
    rep = sb.prepare_arm(EXP, bid, "B", artifact=h)                         # hooks off + env stripped
    assert rep["exact"] is False
    with pytest.raises(rs.SandboxError, match="exact bytes"):
        sb.record_eval(artifact=h, experiment=EXP, arm="B", suite="sandbox-replay", verdict="pass")


def test_cli_record_eval(sb, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rs, "DEFAULT_ROOT", sb.root)
    bid = make_baseline(sb, {"~/.claude/CLAUDE.md": (CLAUDE_MD, "0644")})
    h = make_artifact(sb, bid, {"~/.claude/CLAUDE.md": (CLAUDE_MD, CLAUDE_MD + b"- x\n")})
    assert rs.main(["prepare-arm", "--experiment", EXP, "--baseline", bid, "--arm", "A"]) == 0
    assert rs.main(["prepare-arm", "--experiment", EXP, "--baseline", bid, "--arm", "B", "--artifact", h]) == 0
    res = tmp_path / "r.json"
    res.write_text(json.dumps({"cases": 3}))
    assert rs.main(["record-eval", "--artifact", h, "--experiment", EXP, "--suite", "sandbox-replay",
                    "--verdict", "pass", "--results", str(res)]) == 0
    assert json.loads((sb.artifacts / h / "evals" / "sandbox-replay.json").read_text())["results"]["cases"] == 3
    assert rs.main(["prepare-arm", "--experiment", EXP, "--baseline", bid, "--arm", "C", "--artifact", "x" * 64]) == 2
    assert "64-hex" in capsys.readouterr().err
