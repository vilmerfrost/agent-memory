#!/usr/bin/env python3
"""Mission 005 end-to-end synthetic demo of the nightly RSI investigator (src/agent_memory/rsi_demo.py).

    uv run python scripts/m005_demo.py                      # the success scenario (IMPROVES -> staged -> brief)
    uv run python scripts/m005_demo.py --scenario all       # success, no-effect, regression
    uv run python scripts/m005_demo.py --providers none     # ClickHouse / Langfuse / past.dev not configured

Needs Docker (a throwaway Postgres and the rsi-worker image) and an agent-config checkout with `improve stage`.
Everything happens in a temp work dir; no env file, hosted DB, paid model call or production file is touched.
"""

import os
import sys

os.environ["AMEM_NO_DOTENV"] = "1"          # before agent_memory is imported: no env file, ever

from agent_memory import rsi_demo  # noqa: E402

if __name__ == "__main__":
    sys.exit(rsi_demo.main())
