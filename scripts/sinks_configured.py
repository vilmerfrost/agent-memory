"""Which integration sinks this checkout could reach, without any network call (Mission 004C merge plan).

    uv run --directory ~/Projects/agent-memory --quiet python scripts/sinks_configured.py

Loads the env files the way `amem` does (`.env.local`, then `.env`, of the checkout this code runs from) and
prints, per sink, only yes/no facts: whether its settings are present and whether its kill switch is set.
Never prints a value, never opens a socket, never touches a database. Exit 0 when no sink is configured,
1 when at least one is (then `amem sync run --sink <it>` from this checkout could send).
"""

from __future__ import annotations

import os
import socket
import sys


def _refuse(self: socket.socket, addr: object) -> None:
    raise OSError("sinks_configured: no network")


socket.socket.connect = _refuse  # type: ignore[method-assign]

from agent_memory import config  # noqa: E402
from agent_memory.integrations import clickhouse, langfuse, pastdev, sync  # noqa: E402


def _set(name: str) -> str:
    return "set" if (os.environ.get(name) or "").strip() else "unset"


def main() -> int:
    config.load_env()
    ch = clickhouse.config(os.environ)
    lf = langfuse.LangfuseClient.from_env(os.environ)
    pd = pastdev.config(os.environ)
    rows = {
        "clickhouse": (not ch.problem, ch.problem or f"url present, user {'set' if ch.user else 'unset'}, "
                                                     f"key {'set' if ch.key else 'unset'}"),
        "langfuse": (lf.problem is None, lf.problem or "host/public/secret present"),
        "pastdev": (pd is not None, "project key present" if pd is not None else "no valid AMEM_PASTDEV_KEY"),
    }
    configured = 0
    for sink, (ok, why) in rows.items():
        off = sync.switched_off(sink, os.environ)
        configured += ok and not off
        print(f"{sink:<10} configured={'yes' if ok else 'no':<3} kill_switch={'off' if off else 'unset'}  ({why})")
    for name in ("AMEM_PSEUDONYM_KEY", "AMEM_EGRESS_DENY", "AMEM_OWNER_ACCOUNTS", "AMEM_TLS_CA_FILE", "RSI_SANDBOX_DIR"):
        print(f"{name:<20} {_set(name)}")
    print(f"sinks that could send from this checkout: {configured}")
    return 1 if configured else 0


if __name__ == "__main__":
    sys.exit(main())
