#!/usr/bin/env python3
"""Mission 005.5 mock rehearsal of the first unattended night (src/agent_memory/rsi_rehearsal.py).

    uv run python scripts/m0055_rehearsal.py                 # gate -> night S0-S9 -> brief, stand-in stages, $0
    uv run python scripts/m0055_rehearsal.py --paid-plan     # the live first night's paid calls and estimate (no run)

Needs Docker (a throwaway Postgres and the rsi-worker image) and an agent-config checkout with `improve stage`.
Everything happens in a temp work dir; no env file, hosted DB, paid model call, schedule or production file is
touched. The production fingerprint before and after is printed and must be equal.
"""

import os
import sys

os.environ["AMEM_NO_DOTENV"] = "1"          # before agent_memory is imported: no env file, ever

from agent_memory import rsi_rehearsal  # noqa: E402

if __name__ == "__main__":
    sys.exit(rsi_rehearsal.main())
