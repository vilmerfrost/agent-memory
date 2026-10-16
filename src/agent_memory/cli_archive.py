"""`amem archive init|run|verify|restore|prune|status|key-export|key-import` (docs/archive.md).

Exit codes: 0 ok, 1 a problem, 2 usage (argparse). Output is counts, ids and relative paths
only. No network library is imported here or in `archive`.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from agent_memory import archive


def _positive(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def _version(value: str) -> int | str:
    return "latest" if value == "latest" else _positive(value)


def _iso(value: str):  # noqa: ANN202 - argparse type
    try:
        return archive.parse_iso(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value!r}") from None


def _guard(name: str, fn: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    def wrapped(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except archive.ArchiveError as exc:
            print(f"amem: archive {name}: {exc}", file=sys.stderr)
        except OSError as exc:  # strerror only: the filename may be a private path
            print(f"amem: archive {name}: {type(exc).__name__}: {exc.strerror or 'I/O error'}", file=sys.stderr)
        return 1
    return wrapped


def cmd_init(args: argparse.Namespace) -> int:
    backend = archive.init(reuse_key=args.reuse_key)
    print(f"archive init: archive created, key {archive.DEFAULT_KEY_ID} in the {backend} backend")
    print("next: run `amem archive key-export` in a terminal and store the recovery string "
          "in your password manager")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    rep = archive.run(projects=args.projects_dir, codex=args.codex_home, dry_run=args.dry_run)
    print(rep.line())
    return 1 if rep.errors else 0


def cmd_verify(args: argparse.Namespace) -> int:
    rep = archive.verify(sample=args.sample)
    for line in rep.lines():
        print(line)
    return 0 if rep.ok else 1


def cmd_restore(args: argparse.Namespace) -> int:
    rep = archive.restore(args.session, args.to, version=args.version, as_of=args.as_of)
    print(f"archive restore: files={len(rep.files)} skipped={rep.skipped}")
    for rel in rep.files:
        print(f"  {rel}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    print(archive.prune(days=args.days, dry_run=args.dry_run).line())
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    rep = archive.status()
    print(rep.line())
    return 1 if rep.manifest_ok is False else 0


def cmd_key_export(args: argparse.Namespace) -> int:
    if not sys.stdout.isatty() and not args.allow_non_tty:
        print("amem: archive key-export: stdout is not a terminal; run it yourself in a terminal so the "
              "key does not land in a log or an agent transcript (--allow-non-tty overrides)", file=sys.stderr)
        return 1
    print(archive.export_key())
    return 0


def cmd_key_import(_args: argparse.Namespace) -> int:
    if sys.stdin.isatty():
        text = getpass.getpass("recovery string (input hidden): ")
    else:
        text = sys.stdin.readline()
    kid, action = archive.import_key(text)
    print(f"archive key-import: key {kid} {action}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("archive", help="encrypted local archive of Claude Code and Codex transcripts "
                                       "(docs/archive.md)")
    a = p.add_subparsers(dest="archive_command", required=True)

    q = a.add_parser("init", help="create the archive key and dir (refuses to overwrite either)")
    q.add_argument("--reuse-key", action="store_true",
                   help="start a new archive with an existing key of the same id")
    q.set_defaults(func=_guard("init", cmd_init))

    q = a.add_parser("run", help="archive new transcript versions; prints counts only")
    q.add_argument("--dry-run", action="store_true", help="write nothing, need no key, still count")
    q.add_argument("--projects-dir", type=Path, default=None, help="Claude Code projects dir")
    q.add_argument("--codex-home", type=Path, default=None, help="Codex home dir")
    q.set_defaults(func=_guard("run", cmd_run))

    q = a.add_parser("verify", help="decrypt objects, check sha256, the manifest chain and the index")
    q.add_argument("--sample", type=_positive, default=None, help="check N random objects (default: all)")
    q.set_defaults(func=_guard("verify", cmd_verify))

    q = a.add_parser("restore", help="write an archived session's files into DIR")
    q.add_argument("--session", required=True, help="session id (claude: <id> or <parent>/agent-<id>; "
                                                    "codex: codex:<thread id>)")
    q.add_argument("--to", type=Path, required=True, help="target dir (never a source root or the archive)")
    sel = q.add_mutually_exclusive_group()
    sel.add_argument("--version", type=_version, default="latest", help="latest (default) or a version number")
    sel.add_argument("--as-of", type=_iso, default=None, help="the newest version first seen at or before ISO")
    q.set_defaults(func=_guard("restore", cmd_restore))

    q = a.add_parser("prune", help="drop sources not modified for --days, then unreferenced objects")
    q.add_argument("--days", type=_positive, default=archive.DEFAULT_DAYS)
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=_guard("prune", cmd_prune))

    q = a.add_parser("status", help="counts, size, last run, key present (yes/no)")
    q.set_defaults(func=_guard("status", cmd_status))

    q = a.add_parser("key-export", help="print the key's recovery string (for your password manager)")
    q.add_argument("--allow-non-tty", action="store_true", help="print even when stdout is not a terminal")
    q.set_defaults(func=_guard("key-export", cmd_key_export))

    q = a.add_parser("key-import", help="store a key from its recovery string (read from stdin)")
    q.set_defaults(func=_guard("key-import", cmd_key_import))
