"""RSI V1 artifact hashing and verification, agent-memory side (docs/rsi/spec.md).

The same canonical JSON and test vector as agent-config `lib/rsi_artifact.py`, so a hash computed here equals
the one `improve stage` wrote. Read-only helpers: this module never writes production files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

ARTIFACT_FORMAT = "rsi-artifact/1"
EVAL_FORMAT = "rsi-eval/1"
DECISION_FORMAT = "rsi-decision/1"
BASELINE_FORMAT = "rsi-baseline/1"
PASSING = ("pass", "improves")
HEX64 = re.compile(r"[0-9a-f]{64}")

EXAMPLE_MANIFEST: dict[str, Any] = {
    "format": ARTIFACT_FORMAT, "experiment_id": "exp-20261020-01", "baseline_id": "bl-000000000000",
    "proposal_kind": "edit_instruction", "activation": "none",
    "files": [{"path": "~/.claude/CLAUDE.md", "kind": "instruction", "rule_id": "claude-md-global--closing-line",
               "before_sha256": "0" * 64, "after_sha256": "f" * 64, "mode": "0644"}],
}
EXAMPLE_HASH = "3aa4f3e553c4fb750539e6b8e46604e661e244005c0d2bd7b8eed31b145c30ef"  # same vector in agent-config


class ArtifactError(Exception):
    """The artifact does not verify; the message says why."""


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def artifact_hash(manifest: dict[str, Any]) -> str:
    return sha256_bytes(canonical(manifest))


def sandbox_dir() -> Path:
    return Path(os.environ.get("RSI_SANDBOX_DIR") or Path.home() / "sandbox").expanduser()


def artifacts_dir() -> Path:
    return Path(os.environ.get("RSI_ARTIFACTS_DIR") or sandbox_dir() / "artifacts").expanduser()


def load_artifact(adir: Path) -> tuple[dict[str, Any], str, dict[str, bytes]]:
    """(manifest, hash, {after_sha256: bytes}). Raises ArtifactError unless the dir name, the canonical
    manifest and every after file verify."""
    try:
        raw = (adir / "manifest.json").read_bytes()
        m = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ArtifactError(f"unreadable manifest ({type(exc).__name__})") from None
    if not isinstance(m, dict) or m.get("format") != ARTIFACT_FORMAT or not isinstance(m.get("files"), list):
        raise ArtifactError("not an rsi-artifact/1 manifest")
    if raw.rstrip(b"\n") != canonical(m):
        raise ArtifactError("manifest.json is not canonical JSON")
    h = artifact_hash(m)
    if adir.name != h:
        raise ArtifactError("artifact dir name does not equal its manifest hash")
    after: dict[str, bytes] = {}
    for f in m["files"]:
        sha = f.get("after_sha256", "")
        if not HEX64.fullmatch(str(sha)):
            raise ArtifactError("bad after_sha256")
        try:
            b = (adir / "files" / sha).read_bytes()
        except OSError:
            raise ArtifactError(f"{f.get('path')}: after bytes missing") from None
        if sha256_bytes(b) != sha:
            raise ArtifactError(f"{f.get('path')}: after bytes changed since staging")
        after[sha] = b
    return m, h, after


def eval_records(adir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted((adir / "evals").glob("*.json")) if (adir / "evals").is_dir() else []:
        try:
            r = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("format") == EVAL_FORMAT:
            out.append(r)
    return out
