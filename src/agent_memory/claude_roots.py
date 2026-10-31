"""The transcript roots (docs/sessions-live.md, "Transcript roots").

`claude_home()` stays `~/.claude`: `parent/` state lives there. Transcripts may also live in other
homes. They are listed in one plain, non-secret file, `<claude home>/parent/claude-roots.tsv`:

    # label<TAB>home (Claude: its projects/ folder is scanned)<TAB>codex (optional: a Codex home)
    t3<TAB>/Users/me/.t3/some-runtime/claude
    t3<TAB>/Users/me/control/native/*/home
    t3-admissions<TAB>/Users/me/control/owner-admissions/*/home<TAB>codex

Labels match `[a-z0-9][a-z0-9_-]*`; `default` and `claude` are reserved. Each tool has its own
labels. A path component that is exactly `*` matches one folder level, so a guarded session's new
hash folder needs no file edit. Each match is one root, labelled `<label>-<short segment>`; the
label depends only on the matched name, so it does not change between runs. A bad line, or a
duplicate label or path, is skipped with one warning and stops nothing. No file, `--projects-dir`
or AMEM_PROJECTS_DIR: the one default Claude root; no file, `--codex-home` or AMEM_CODEX_HOME: the
one default Codex home. Both as before this module existed.

A row of an added root stores its label in `meta.claude_root` or `meta.codex_root`. A missing value
means the default root, so default rows stay byte-identical. Archive records of an added root use
the source `claude:<label>` or `codex:<label>`; default records keep `claude` and `codex`.

Standard library only: archive.py imports this module and imports no network library, `config`
or `sessions_ingest` (tests/test_archive.py). Callers that read env files load them first.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

ROOTS_FILE = "claude-roots.tsv"
DEFAULT_SOURCE = "claude"
CODEX_SOURCE = "codex"
CODEX_DIRS = ("sessions", "archived_sessions")
RESERVED = ("default", DEFAULT_SOURCE)
LABEL_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
# An unknown label resolves here: a path below a device file, which can never exist.
MISSING = Path(os.devnull) / "amem-unknown-claude-root"
ANY_FOLDER = "*"
T3_LABEL = "t3"
_DIGEST_SEGMENT_RE = re.compile(r"[0-9a-f]{32,}")
SHORT_SEGMENT = 12

_warned: set[str] = set()


@dataclass(frozen=True)
class ClaudeRoot:
    label: str | None      # None: the default root
    projects: Path         # the folder of `<project>/*.jsonl`

    @property
    def source(self) -> str:
        """The archive source: `claude`, or `claude:<label>` for an added root."""
        return DEFAULT_SOURCE if self.label is None else f"{DEFAULT_SOURCE}:{self.label}"


@dataclass(frozen=True)
class CodexRoot:
    label: str | None      # None: the default home
    home: Path             # the folder of `sessions/` and `archived_sessions/`

    @property
    def source(self) -> str:
        """The archive source: `codex`, or `codex:<label>` for an added home."""
        return CODEX_SOURCE if self.label is None else f"{CODEX_SOURCE}:{self.label}"


@dataclass
class _Listing:
    claude: list[ClaudeRoot] = field(default_factory=list)
    codex: list[CodexRoot] = field(default_factory=list)
    bases: list[Path] = field(default_factory=list)   # the fixed folder before each pattern's first `*`
    claude_real: dict[Path, str] = field(default_factory=dict)   # real projects folder -> label
    codex_real: dict[Path, str] = field(default_factory=dict)    # real Codex home -> label
    folders: dict[str, list[Path]] | None = None      # project folder name -> folders (one_listing)


_frozen: _Listing | None = None


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


def default_codex(arg: Path | None = None) -> Path:
    """--codex-home, else AMEM_CODEX_HOME, else `.codex` next to the Claude home."""
    if arg is not None:
        return arg.expanduser()
    return _env_path("AMEM_CODEX_HOME") or claude_home().parent / ".codex"


def roots_file() -> Path:
    return claude_home() / "parent" / ROOTS_FILE


def _entry(line: str) -> tuple[str, Path, bool]:
    """(label, home, a Codex home?) of one line. Raises ValueError with the reason."""
    label, *rest = line.split("\t")
    where = rest[0].strip() if rest else ""
    mark = rest[1].strip() if len(rest) > 1 else ""
    if not where or len(rest) > 2 or mark not in ("", CODEX_SOURCE):
        raise ValueError("expected <label><TAB><path>, then optional <TAB>codex")
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
    if any(ANY_FOLDER in part and part != ANY_FOLDER for part in home.parts):
        raise ValueError("a `*` must be a whole path component")
    return label, home, mark == CODEX_SOURCE


def _matched_label(label: str, segments: list[str]) -> str:
    """`<label>-<short segment>`. The first characters of one digest segment, else a hash of the
    names: dated names and time-ordered UUIDs share their first characters. Only the matched names
    decide it, so sibling folders never change a label."""
    text = "-".join(segments)
    if len(segments) == 1 and _DIGEST_SEGMENT_RE.fullmatch(text):
        return f"{label}-{text[:SHORT_SEGMENT]}"
    return f"{label}-{hashlib.sha256(os.fsencode(text)).hexdigest()[:SHORT_SEGMENT]}"


def _has_transcripts(home: Path, codex: bool) -> bool:
    return any((home / sub).is_dir() for sub in CODEX_DIRS) if codex else (home / "projects").is_dir()


def _expand(label: str, pattern: Path, codex: bool) -> list[tuple[str, Path]]:
    """(label, home) of every folder a pattern matches, sorted. A match with no transcript folder
    of this tool is no root: a Claude session's folder has no rollouts, a Codex one no projects."""
    found: list[tuple[Path, list[str]]] = [(Path(pattern.anchor), [])]
    for part in pattern.parts[1:]:
        if part != ANY_FOLDER:
            found = [(folder / part, segments) for folder, segments in found]
            continue
        below: list[tuple[Path, list[str]]] = []
        for folder, segments in found:
            try:
                with os.scandir(folder) as it:
                    names = sorted(e.name for e in it if e.is_dir() and not e.name.startswith("."))
            except OSError:
                continue
            below += [(folder / name, [*segments, name]) for name in names]
        found = below
    return [(_matched_label(label, segments), home) for home, segments in found if _has_transcripts(home, codex)]


def _base(pattern: Path) -> Path:
    fixed = pattern.parts[:pattern.parts.index(ANY_FOLDER)]
    return Path(*fixed)


def _read() -> _Listing:
    """The file's valid entries in file order; a plain path whether its folder exists today or not,
    a pattern as the folders it matches now. An entry whose real path is the default root's, or an
    earlier entry's, is left out: one folder, one root."""
    out = _Listing()
    path = roots_file()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return out
    except OSError as exc:
        _warn(f"{path}: not read ({type(exc).__name__}); using the default roots only")
        return out
    labels: dict[bool, set[str]] = {False: set(), True: set()}
    folders = {False: {_real(default_projects()), _real(claude_home() / "projects")}, True: {_real(default_codex())}}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            label, home, codex = _entry(line)
            pattern = ANY_FOLDER in home.parts
            found = _expand(label, home, codex) if pattern else [(label, home)]
        except ValueError as exc:   # also a NUL byte in the path
            _warn(f"{path}:{n}: skipped: {exc}")
            continue
        if pattern:
            out.bases.append(_base(home))
            if not out.bases[-1].is_dir():   # a typo; an empty folder is normal
                _warn(f"{path}:{n}: {home}: no folder at {out.bases[-1]}")
        for name, where in found:
            folder = where if codex else where / "projects"
            try:
                if name in labels[codex]:
                    raise ValueError(f"duplicate label {name!r}")
                real = _real(folder)
                if real in folders[codex]:
                    raise ValueError(f"{folder} is a root already listed")
            except ValueError as exc:   # also a NUL byte in the path
                _warn(f"{path}:{n}: skipped: {exc}")
                continue
            labels[codex].add(name)
            folders[codex].add(real)
            if codex:
                out.codex.append(CodexRoot(name, where))
                out.codex_real[real] = name
            else:
                out.claude.append(ClaudeRoot(name, folder))
                out.claude_real[real] = name
    return out


def _listing() -> _Listing:
    return _frozen if _frozen is not None else _read()


@contextmanager
def one_listing() -> Iterator[None]:
    """One reading of the file and its patterns for a whole run: each row of an ingest asks for the
    roots, and a pattern can match thousands of folders. A folder made during the run waits for the
    next run."""
    global _frozen
    if _frozen is not None:
        yield
        return
    _frozen = _read()
    try:
        yield
    finally:
        _frozen = None


def listed() -> list[ClaudeRoot]:
    """The file's Claude roots (_read)."""
    return _listing().claude


def listed_codex() -> list[CodexRoot]:
    """The file's Codex homes (_read)."""
    return _listing().codex


def pattern_bases() -> list[Path]:
    """The fixed folder of each pattern: every folder a pattern may match later is below it."""
    return _listing().bases


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


def codex_roots(home: Path | None = None) -> list[CodexRoot]:
    """The Codex homes to read now, in order: the default home (present or not, as before), then
    the listed homes that hold a sessions folder. An explicit home (`home`, AMEM_CODEX_HOME) means
    exactly that one. A Codex home is never derived from a Claude root."""
    first = CodexRoot(None, default_codex(home))
    if home is not None or _env_path("AMEM_CODEX_HOME") is not None:
        return [first]
    out = [first]
    for root in listed_codex():
        if _has_transcripts(root.home, True):
            out.append(root)
        else:
            _warn(f"Codex root {root.label}: no sessions folder at {root.home}; skipped this run")
    return out


def project_folders(name: str) -> list[Path]:
    """The folders called `name` in every Claude root; nothing with one root."""
    if _frozen is None:
        roots = claude_roots()
        return [f for r in roots if (f := r.projects / name).is_dir()] if len(roots) > 1 else []
    if _frozen.folders is None:
        roots = claude_roots()
        _frozen.folders = {}
        for root in roots if len(roots) > 1 else ():
            try:
                with os.scandir(root.projects) as it:
                    for e in it:
                        if e.is_dir():
                            _frozen.folders.setdefault(e.name, []).append(root.projects / e.name)
            except OSError:
                continue
    return _frozen.folders.get(name, [])


def resolve(label: object = None) -> Path:
    """The projects folder of a stored `meta.claude_root`. Missing means the default root. A label
    the file no longer lists, or a pattern's folder that is gone, gives MISSING, never the default
    root: a same-ID file there is another transcript."""
    if label is None:
        return default_projects()
    return next((root.projects for root in listed() if root.label == label), MISSING)


def resolve_codex(label: object = None) -> Path:
    """The Codex home of a stored `meta.codex_root`, by the rule of `resolve`."""
    if label is None:
        return default_codex()
    return next((root.home for root in listed_codex() if root.label == label), MISSING)


def _label_below(path: Path, roots: dict[Path, str]) -> str | None:
    """A walk up from `path`, not a walk over the roots: a pattern can match thousands of them."""
    if not roots:
        return None
    real = _real(path)
    return next((roots[p] for p in (real, *real.parents) if p in roots), None)


def label_of(path: Path) -> str | None:
    """The label of the listed Claude root that really holds `path`; None for anything else."""
    return _label_below(path, _listing().claude_real)


def codex_label_of(path: Path) -> str | None:
    """The label of the listed Codex home that really holds `path`; None for anything else."""
    return _label_below(path, _listing().codex_real)


def is_t3(label: str | None) -> bool:
    """A root the operator named as T3 Code's: label `t3`, or one that starts with `t3-`."""
    return label is not None and (label == T3_LABEL or label.startswith(f"{T3_LABEL}-"))
