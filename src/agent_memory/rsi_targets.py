"""Production targets, baseline snapshots, drift detection, the git-tracking plan and the freeze check
(Mission 003, docs/rsi/targets.md). Everything here only READS production files. Baselines are written
under the RSI sandbox (~/sandbox/baselines). The tracking plan is generated as scripts, never executed here.
"""

from __future__ import annotations

import glob
import json
import os
import plistlib
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_memory import redact, rsi_artifact as RA

TARGETS_FORMAT = "rsi-targets/1"
MAX_BYTES = 2_000_000
NEVER = (".env", "auth.json", ".netrc", "credentials", "id_rsa", ".pem", ".key", "notify.env")


def home() -> Path:
    return Path.home()


def agent_config_dir() -> Path:
    env = os.environ.get("AMEM_AGENT_CONFIG_DIR", "").strip()
    return Path(env).expanduser() if env else home() / "agent-config"


def tilde(p: Path) -> str:
    s, h = str(p), str(home())
    for base in (str(Path(h).resolve()), h):
        if s == base or s.startswith(base + "/"):
            return "~" + s[len(base):]
    return s


def untilde(key: str) -> Path:
    return home() / key[2:] if key.startswith("~/") else Path(key)


def _kind(key: str, real: Path) -> str:
    if real.name == "settings.json":
        return "settings"
    if real.suffix == ".plist":
        return "schedule"
    if "/hooks/" in key or "/hooks/" in str(real):
        return "hook"
    if real.name == "lessons.md":
        return "lessons"
    if "/skills/" in key:
        return "skill"
    if "/scheduled-tasks/" in key:
        return "instruction"
    return "instruction" if real.suffix == ".md" else "other"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20)


def git_state(real: Path) -> dict[str, Any]:
    """{repo, tracked, dirty} of the real file; repo None when it is not inside a git work tree."""
    r = _git(real.parent, "rev-parse", "--show-toplevel")
    if r.returncode:
        return {"repo": None, "tracked": False, "dirty": False}
    root = Path(r.stdout.strip()).resolve()
    rel = str(real.relative_to(root))
    tracked = _git(root, "ls-files", "--error-unmatch", "--", rel).returncode == 0
    dirty = bool(_git(root, "status", "--porcelain", "--", rel).stdout.strip()) if tracked else False
    return {"repo": tilde(root), "tracked": tracked, "dirty": dirty}


def _expand_criteria(ac: Path) -> list[Path]:
    try:
        crit = json.loads((ac / "improve" / "criteria.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for t in crit.get("allowed_targets", []):
        base = Path(str(ac) + t[1:]) if t.startswith("@/") else untilde(t) if t.startswith("~/") else Path(t)
        if t.endswith("/"):
            out += [p for p in sorted(base.glob("**/*"))
                    if p.is_file() and (p.suffix in (".md", ".sh", ".py", ".json") or p.name == "SKILL.md")]
        else:
            out.append(base)
    return out


def discover(*, include_schedules: bool = True) -> list[Path]:
    """Every production file the RSI loop may ever target (see docs/rsi/writers.md), as given paths."""
    h, ac = home(), agent_config_dir()
    paths = [h / ".claude" / "CLAUDE.md", h / ".codex" / "AGENTS.md", ac / "parent" / "PARENT.md", ac / "lessons.md",
             ac / "settings.json"]
    paths += [Path(p) for p in sorted(glob.glob(str(h / ".claude" / "scheduled-tasks" / "*" / "SKILL.md")))]
    paths += [Path(p) for p in sorted(glob.glob(str(ac / "agents" / "*.md")))]
    paths += [Path(p) for p in sorted(glob.glob(str(h / ".agents" / "skills" / "*" / "SKILL.md")))]
    paths += [Path(p) for p in sorted(glob.glob(str(ac / "parent" / "skills" / "*" / "SKILL.md")))]
    paths += [p for p in _expand_criteria(ac) if "/hooks/" in str(p)]
    if include_schedules:
        paths += [Path(p) for p in sorted(glob.glob(str(ac / "launchd" / "*.plist")))]
    seen, out = set(), []
    for p in paths:
        key = tilde(p)
        if key in seen or not p.is_file() or any(n in p.name for n in NEVER):
            continue
        seen.add(key)
        out.append(p)
    return out


@dataclass
class Target:
    key: str
    real: Path
    kind: str
    sha256: str
    size: int
    mode: str
    symlink: bool
    git: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.key, "real": tilde(self.real), "kind": self.kind, "sha256": self.sha256,
                "size": self.size, "mode": self.mode, "symlink": self.symlink, **self.git}


def describe(paths: list[Path] | None = None, *, with_git: bool = True) -> list[Target]:
    out = []
    for p in paths if paths is not None else discover():
        real = p.resolve()
        b = real.read_bytes()
        if len(b) > MAX_BYTES:
            continue
        out.append(Target(tilde(p), real, _kind(tilde(p), real), RA.sha256_bytes(b), len(b),
                          f"0{real.stat().st_mode & 0o777:o}", p.is_symlink(), git_state(real) if with_git else {}))
    return out


def targets_manifest(targets: list[Target]) -> dict[str, Any]:
    files = sorted((t.as_dict() for t in targets), key=lambda f: f["path"])
    return {"format": TARGETS_FORMAT, "targets_id": RA.artifact_hash({"files": files})[:16],
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "files": files}


# -- baselines ------------------------------------------------------------------------------------
class BaselineError(Exception):
    pass


def baselines_dir() -> Path:
    return RA.sandbox_dir() / "baselines"


def secret_problems(key: str, data: bytes) -> list[str]:
    """A baseline is mounted read-only into sandbox containers: refuse anything secret-shaped."""
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return [f"{key}: not UTF-8 text"]
    return [] if redact.redact(text) == text else [f"{key}: holds a secret-shaped string (redact.py)"]


def create_baseline(paths: list[Path] | None = None, *, out: Path | None = None) -> tuple[str, Path]:
    """Copy the targets into <out>/<baseline_id>/files/<path without ~/> + manifest.json (rsi-baseline/1).
    Reads production files only. Read-only result. Idempotent per content (same files -> same dir reused only
    when created_at matches; a new snapshot gets a new id)."""
    targets = describe(paths, with_git=False)
    probs = []
    datas: dict[str, bytes] = {}
    for t in targets:
        if not t.key.startswith("~/"):
            probs.append(f"{t.key}: outside HOME (RSI baselines cover ~/ paths only)")
            continue
        b = t.real.read_bytes()
        if RA.sha256_bytes(b) != t.sha256:
            probs.append(f"{t.key}: changed while reading")
        probs += secret_problems(t.key, b)
        datas[t.key] = b
    if probs:
        raise BaselineError("; ".join(probs))
    files = [{"path": t.key, "sha256": t.sha256, "size": t.size, "mode": t.mode, "kind": t.kind}
             for t in sorted(targets, key=lambda t: t.key)]
    body = {"format": RA.BASELINE_FORMAT, "created_at": datetime.now(UTC).isoformat(timespec="seconds"), "files": files}
    bid = "bl-" + RA.artifact_hash(body)[:12]
    root = out or baselines_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = root / bid
    if final.exists():
        return bid, final
    tmp = Path(tempfile.mkdtemp(prefix=f".{bid}-", dir=root))
    for f in files:
        dst = tmp / "files" / f["path"][2:]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(datas[f["path"]])
    (tmp / "manifest.json").write_text(json.dumps({**body, "baseline_id": bid}, indent=1, sort_keys=True) + "\n")
    for p in sorted(tmp.rglob("*"), reverse=True):
        os.chmod(p, 0o555 if p.is_dir() else 0o444)
    os.chmod(tmp, 0o555)
    os.rename(tmp, final)
    return bid, final


def load_baseline(bdir: Path) -> dict[str, Any]:
    m = json.loads((bdir / "manifest.json").read_text())
    body = {k: v for k, v in m.items() if k != "baseline_id"}
    if m.get("format") != RA.BASELINE_FORMAT or m.get("baseline_id") != "bl-" + RA.artifact_hash(body)[:12]:
        raise BaselineError(f"{bdir.name}: manifest does not verify")
    for f in m["files"]:
        if RA.sha256_bytes((bdir / "files" / f["path"][2:]).read_bytes()) != f["sha256"]:
            raise BaselineError(f"{bdir.name}: copy of {f['path']} changed")
    return m


def check_baseline(bdir: Path) -> dict[str, Any]:
    """Drift between a baseline and the live files: {baseline_id, unchanged, changed[], missing[]}."""
    m = load_baseline(bdir)
    changed, missing, same = [], [], 0
    for f in m["files"]:
        live = _sha_or_none(untilde(f["path"]))
        if live is None:
            missing.append(f["path"])
        elif live != f["sha256"]:
            changed.append(f["path"])
        else:
            same += 1
    return {"baseline_id": m["baseline_id"], "files": len(m["files"]), "unchanged": same, "changed": changed,
            "missing": missing, "drift": bool(changed or missing)}


def _sha_or_none(p: Path) -> str | None:
    try:
        return RA.sha256_bytes(p.read_bytes())
    except OSError:
        return None


def artifact_staleness(manifest: dict[str, Any]) -> str:
    """fresh (every live file at before_sha) | applied (all at after_sha) | stale (anything else)."""
    states = []
    for f in manifest.get("files", []):
        live = _sha_or_none(untilde(f["path"]))
        states.append("before" if live == f["before_sha256"] else "after" if live == f["after_sha256"] else "other")
    if states and all(s == "before" for s in states):
        return "fresh"
    if states and all(s == "after" for s in states):
        return "applied"
    return "stale"


# -- git-tracking plan (generated, never executed here) ----------------------------------------------
TRACK = (("~/.claude/CLAUDE.md", "home/claude/CLAUDE.md", "link"),
         ("~/.codex/AGENTS.md", "home/codex/AGENTS.md", "link"))


def tracking_plan() -> dict[str, Any]:
    """Apply + inverse scripts that bring the untracked production targets under agent-config git.
    CLAUDE.md and AGENTS.md: moved into agent-config and symlinked back (like settings.json today).
    Scheduled-task prompts: MIRRORED (copied, not linked), because the Desktop app owns and may rewrite
    those files [assumption]; drift is caught by `amem rsi baseline check`."""
    ac, h = agent_config_dir(), home()
    ts = "$(date +%Y%m%d-%H%M%S)"
    items = []
    for key, rel, how in TRACK:
        live = untilde(key)
        if live.is_file() and not live.is_symlink():
            items.append({"key": key, "repo_path": rel, "how": how, "sha256": _sha_or_none(live)})
    for p in sorted(glob.glob(str(h / ".claude" / "scheduled-tasks" / "*" / "SKILL.md"))):
        live = Path(p)
        name = live.parent.name
        items.append({"key": tilde(live), "repo_path": f"home/scheduled-tasks/{name}/SKILL.md", "how": "mirror",
                      "sha256": _sha_or_none(live)})
    q = shlex.quote
    apply = ["#!/bin/bash", "# Generated by `amem rsi tracking-plan`. Run from Vilmer's terminal after approval.",
             "set -euo pipefail", f"AC={q(str(ac))}", f"BK=$HOME/.claude/parent/tracking-backup/{ts}", 'mkdir -p "$BK"',
             'sha() { shasum -a 256 "$1" | cut -d" " -f1; }']
    inverse = ["#!/bin/bash", "# Inverse of the tracking plan: restores the original files. Pass the backup dir.",
               "set -euo pipefail", f"AC={q(str(ac))}", 'BK="${1:?usage: tracking-inverse.sh <backup dir>}"',
               'sha() { shasum -a 256 "$1" | cut -d" " -f1; }']
    for it in items:
        live = '"$HOME"/' + q(it["key"][2:])
        repo = '"$AC"/' + q(it["repo_path"])
        bk = '"$BK"/' + q(it["key"][2:])
        apply += [f"# {it['key']} -> {it['repo_path']} ({it['how']})",
                  f'[ "$(sha {live})" = {q(it["sha256"])} ] || {{ echo "changed since the plan: {it["key"]}"; exit 1; }}',
                  f"mkdir -p \"$(dirname {repo})\" \"$(dirname {bk})\"", f"cp -p {live} {repo}", f"cp -p {live} {bk}",
                  f'[ "$(sha {repo})" = {q(it["sha256"])} ]']
        inverse += [f"# {it['key']}"]
        if it["how"] == "link":
            apply += [f"ln -sfn {repo} {live}.rsi-link && mv -f {live}.rsi-link {live}",
                      f'[ -L {live} ] && [ "$(sha {live})" = {q(it["sha256"])} ]',
                      f"grep -qxF {q(it['repo_path'] + chr(9) + it['key'] + chr(9) + 'ac' + chr(9) + 'all')} \"$AC/links.tsv\" || "
                      f"printf '%s\\t%s\\tac\\tall\\n' {q(it['repo_path'])} {q(it['key'])} >> \"$AC/links.tsv\""]
            inverse += [f"[ -L {live} ] && rm -f {live}", f"cp -p {bk} {live}",
                        f'[ "$(sha {live})" = "$(sha {bk})" ]',
                        f"grep -vxF {q(it['repo_path'] + chr(9) + it['key'] + chr(9) + 'ac' + chr(9) + 'all')} \"$AC/links.tsv\" > \"$AC/links.tsv.tmp\" || true",
                        'mv -f "$AC/links.tsv.tmp" "$AC/links.tsv"']
        else:
            inverse += [f"# mirror only: {it['key']} was never moved"]
    apply += ['git -C "$AC" add -- home links.tsv',
              'git -C "$AC" commit -qm "rsi: track production instruction files (home/, links.tsv)" -- home links.tsv',
              'echo "tracked; backup in $BK"']
    inverse += ['git -C "$AC" rm -rq --cached -- home >/dev/null 2>&1 || true',
                'git -C "$AC" commit -qm "rsi: undo tracking plan" -- home links.tsv || true', 'echo "restored from $BK"']
    return {"items": items, "apply": "\n".join(apply) + "\n", "inverse": "\n".join(inverse) + "\n"}


# -- freeze status (read-only) --------------------------------------------------------------------
WRITER_TASKS = ("ab-lessons-monthly", "workflow-improve-weekly")


def scheduled_tasks_state() -> dict[str, dict[str, Any]]:
    pat = str(home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions" / "*" / "*" / "scheduled-tasks.json")
    out: dict[str, dict[str, Any]] = {}
    for f in glob.glob(pat):
        try:
            items = json.loads(Path(f).read_text()).get("scheduledTasks") or []
        except (OSError, ValueError):
            continue
        for t in items if isinstance(items, list) else []:
            if isinstance(t, dict) and t.get("id"):
                out[t["id"]] = {"enabled": t.get("enabled"), "permissionMode": t.get("permissionMode")}
    return out


def freeze_status() -> dict[str, Any]:
    ac, h = agent_config_dir(), home()
    reasons: list[str] = []
    checks: dict[str, Any] = {}
    staging = False
    label = "com.vilmerfrost.amem-daily"
    for name, path in (("installed_plist", h / "Library" / "LaunchAgents" / f"{label}.plist"),
                       ("repo_plist", ac / "launchd" / f"{label}.plist")):
        try:
            env = plistlib.loads(path.read_bytes()).get("EnvironmentVariables") or {}
            live, mode_env = env.get("AMEM_EDITS_LIVE") == "1", str(env.get("AMEM_EDITS_MODE") or "").strip()
            checks[name] = {"AMEM_EDITS_LIVE": env.get("AMEM_EDITS_LIVE"), "AMEM_EDITS_MODE": env.get("AMEM_EDITS_MODE")}
            if live and mode_env == "stage":       # edits.stage_mode(): proposals only, production is only read
                staging = True
            elif live:
                reasons.append(f"{name}: AMEM_EDITS_LIVE=1 (amem-daily edits step is live)")
        except (OSError, plistlib.InvalidFileException):
            checks[name] = {"error": "unreadable"}
    try:
        crit = json.loads((ac / "improve" / "criteria.json").read_text())
    except (OSError, ValueError):
        crit = {}
    cap, mode = crit.get("max_changes_per_week"), crit.get("apply_mode", "legacy")
    checks["criteria"] = {"max_changes_per_week": cap, "apply_mode": mode}
    if not (cap == 0 or mode == "stage"):
        reasons.append(f"criteria.json: max_changes_per_week={cap} apply_mode={mode} (improve apply can write)")
    tasks = scheduled_tasks_state()
    ab_guard = "def stage_only" in (ac / "evals" / "ab-lesson").read_text() if (ac / "evals" / "ab-lesson").exists() else False
    checks["ab_lesson_stage_guard"] = ab_guard
    for t in WRITER_TASKS:
        st = tasks.get(t, {})
        checks[f"task:{t}"] = st
        if t == "ab-lessons-monthly" and st.get("enabled") and not (ab_guard and mode == "stage"):
            reasons.append("ab-lessons-monthly is enabled and writes lessons.md directly")
    # frozen = no automated writer can change production. stage_only = some writer is on, but only stages.
    return {"frozen": not reasons, "stage_only": not reasons and staging, "reasons": reasons, "checks": checks}
