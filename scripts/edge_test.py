#!/usr/bin/env python3
"""Run an edge function's Deno tests in the pinned official Deno image (no Deno on the host).

    parent-run -- python3 scripts/edge_test.py amem-decide                 # unit tests (*_test.ts, no e2e)
    parent-run -- python3 scripts/edge_test.py amem-decide --update-lock   # type-check all, rewrite deno.lock
    python3 scripts/edge_test.py amem-decide --e2e --env K=V ...           # tests/test_web_decide.py uses this

Image: DENO_IMAGE below, pinned by tag and digest. Dependencies resolve from the function's deno.lock
(`--frozen`), so tests and `supabase functions deploy` load the same library code. The Deno cache lives in
<tmp>/amem-deno-cache. Exit code: Deno's (2 when Docker or the image is missing).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DENO_TAG = "2.9.7"
DENO_DIGEST = "sha256:fa335acdf6b72106eda2cb6a8cb5f4187e7630e357467489db4b2e7352d5e432"
DENO_IMAGE = f"denoland/deno:{DENO_TAG}@{DENO_DIGEST}"
REPO = Path(__file__).resolve().parents[1]
FUNCTIONS = REPO / "supabase" / "functions"


def image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", DENO_IMAGE], capture_output=True, timeout=60)
    return r.returncode == 0


def command(function: str, *, e2e: bool = False, update_lock: bool = False, env: list[str] | None = None,
            files: list[str] | None = None) -> list[str]:
    fdir = FUNCTIONS / function
    if not (fdir / "deno.json").is_file():
        raise SystemExit(f"edge_test: {fdir} has no deno.json")
    cache = Path(tempfile.gettempdir()) / "amem-deno-cache"
    cache.mkdir(exist_ok=True)
    cmd = ["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway",
           "-v", f"{fdir}:/fn{'' if update_lock else ':ro'}", "-v", f"{cache}:/deno-dir", "-e", "DENO_DIR=/deno-dir",
           "-w", "/fn"]
    for kv in env or []:
        cmd += ["-e", kv]
    if update_lock:          # type-check every file (tests included) and record every dependency in deno.lock
        return cmd + [DENO_IMAGE, "deno", "check", "--frozen=false", *sorted(p.name for p in fdir.glob("*.ts"))]
    cmd += [DENO_IMAGE, "deno", "test", "--frozen"]
    if e2e:
        cmd += ["--allow-env", "--allow-net", *(files or ["web_e2e_test.ts"])]
    else:
        cmd += [*(files or sorted(p.name for p in fdir.glob("*_test.ts") if p.name != "web_e2e_test.ts"))]
    return cmd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("function")
    ap.add_argument("--e2e", action="store_true", help="run web_e2e_test.ts (needs --env AMEM_E2E_*)")
    ap.add_argument("--update-lock", action="store_true")
    ap.add_argument("--env", action="append", default=[], help="K=V passed into the container")
    ap.add_argument("files", nargs="*")
    a = ap.parse_args(argv)
    if not image_present():
        print(f"edge_test: Docker or the image {DENO_IMAGE} is missing (docker pull it first)", file=sys.stderr)
        return 2
    return subprocess.run(command(a.function, e2e=a.e2e, update_lock=a.update_lock, env=a.env, files=a.files)).returncode


if __name__ == "__main__":
    sys.exit(main())
