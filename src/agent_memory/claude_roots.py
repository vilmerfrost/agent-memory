"""The Claude transcript roots (docs/sessions-live.md, "Transcript roots").

`claude_home()` stays `~/.claude`: `parent/` state lives there. Transcripts may also live in other
Claude homes. They are listed in one plain, non-secret file, `<claude home>/parent/claude-roots.tsv`:

    # label<TAB>Claude home (its projects/ folder is scanned)
    t3<TAB>/Users/me/.t3/some-runtime/claude

Labels match `[a-z0-9][a-z0-9_-]*`; `default` and `claude` are reserved. A bad line, or a duplicate
label or path, is skipped with one warning and stops nothing. No file, `--projects-dir` or
AMEM_PROJECTS_DIR: the one default root, as before this module existed.

A row of an added root stores its label in `meta.claude_root`. A missing value means the default
root, so default rows stay byte-identical. Archive records of an added root use the source
`claude:<label>`; default records keep `claude`.

Standard library only: archive.py imports this module and imports no network library, `config`
or `sessions_ingest` (tests/test_archive.py). Callers that read env files load them first.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOTS_FILE = "claude-roots.tsv"
DEFAULT_SOURCE = "claude"
RESERVED = ("default", DEFAULT_SOURCE)
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
# An unknown label resolves here: a path below a device file, which can never exist.
MISSING = Path(os.devnull) / "amem-unknown-claude-root"

_warned: set[str] = set()


@dataclass(frozen=True)
class ClaudeRoot:
    label: str | None      # None: the default root
    projects: Path         # the folder of `<project>/*.jsonl`

    @property
    def source(self) -> str:
        """The archive source: `claude`, or `claude:<label>` for an added root."""
        return DEFAULT_SOURCE if self.label is None else f"{DEFAULT_SOURCE}:{self.label}"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def _real(path: Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(path)))


def _warn(text: str) -> None:
    """One line on stderr, once per process: the file is read for every row."""
    if text not in _warned:
        _warned.add(text)
        print(f"amem: {text}", file=sys.stderr)


def claude_home() -> Path:
    """AMEM_CLAUDE_HOME (tests), else ~/.claude. The sessions_ingest rule, without env files."""
    return _env_path("AMEM_CLAUDE_HOME") or Path.home() / ".claude"


def default_projects(arg: Path | None = None) -> Path:
    """--projects-dir, else AMEM_PROJECTS_DIR, else <claude home>/projects."""
    if arg is not None:
        return arg.expanduser()
    return _env_path("AMEM_PROJECTS_DIR") or claude_home() / "projects"


def roots_file() -> Path:
    return claude_home() / "parent" / ROOTS_FILE


def _entry(line: str) -> tuple[str, Path]:
    """(label, projects folder) of one line. Raises ValueError with the reason."""
    label, tab, where = line.partition("\t")
    where = where.strip()
    if not tab or not where:
        raise ValueError("expected <label><TAB><path>")
    if not LABEL_RE.fullmatch(label):
        raise ValueError(f"bad label {label!r}")
    if label in RESERVED:
        raise ValueError(f"reserved label {label!r}")
    try:
        home = Path(where).expanduser()
    except RuntimeError:
        raise ValueError("the path cannot be expanded") from None
    if not home.is_absolute():
        raise ValueError("the path is not absolute")
    return label, home / "projects"


def listed() -> list[ClaudeRoot]:
    """The file's valid entries in file order, whether their folder exists today or not. An entry
    whose real path is the default root's, or an earlier entry's, is left out: one folder, one root."""
    path = roots_file()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError as exc:
        _warn(f"{path}: not read ({type(exc).__name__}); using the default Claude root only")
        return []
    labels: set[str] = set()
    folders = {_real(default_projects()), _real(claude_home() / "projects")}
    out: list[ClaudeRoot] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            label, projects = _entry(line)
            if label in labels:
                raise ValueError(f"duplicate label {label!r}")
            real = _real(projects)
            if real in folders:
                raise ValueError(f"{projects} is a root already listed")
        except ValueError as exc:   # also a NUL byte in the path
            _warn(f"{path}:{n}: skipped: {exc}")
            continue
        labels.add(label)
        folders.add(real)
        out.append(ClaudeRoot(label, projects))
    return out


def claude_roots(projects: Path | None = None) -> list[ClaudeRoot]:
    """The roots to read now, in order: the default root (present or not, as before), then the
    listed roots with a projects folder. A listed root that is not there is skipped for this
    run; its transcripts are unknown, not deleted. An explicit folder (`projects`,
    AMEM_PROJECTS_DIR) means exactly that one root."""
    first = ClaudeRoot(None, default_projects(projects))
    if projects is not None or _env_path("AMEM_PROJECTS_DIR") is not None:
        return [first]
    out = [first]
    for root in listed():
        if root.projects.is_dir():
            out.append(root)
        else:
            _warn(f"Claude root {root.label}: no folder at {root.projects}; skipped this run")
    return out


def resolve(label: object = None) -> Path:
    """The projects folder of a stored `meta.claude_root`. Missing means the default root. A label
    the file no longer lists gives MISSING, never the default root: a same-ID file there is another
    transcript."""
    if label is None:
        return default_projects()
    return next((root.projects for root in listed() if root.label == label), MISSING)


def label_of(path: Path) -> str | None:
    """The label of the listed root that really holds `path`; None for anything else."""
    roots = listed()
    if not roots:
        return None
    real = _real(path)
    for root in roots:
        folder = _real(root.projects)
        if folder == real or folder in real.parents:
            return root.label
    return None
