"""`sessions.interface`: which app a session ran in (docs/sessions-live.md, "Interface").

One pure function, no database. Transcript ingest, the live ping and `amem sessions interface` all
call `derive`. First match wins.

Claude Code:
1. cloud            meta.cloud is true, or a `cloud:` id
2. t3-code          meta.client = t3, or meta.claude_root is a T3 root
3. claude-desktop   entrypoints has claude-desktop
4. ide              entrypoints has claude-vscode
5. cli              entrypoints has cli
6. sdk              an entrypoint starts with sdk, or meta.source starts with rsi-native-session/
7. unknown          everything else, an entrypoint not listed here too: nothing is guessed

Codex:
1. t3-code          originator T3 Code
2. chatgpt-desktop  originator Codex Desktop
3. cli              originator codex-tui
4. sdk              originator codex_exec, or source exec, or source starts with rsi-native-session/
5. t3-code          no originator and meta.codex_root is a T3 root
6. unknown          everything else (zeron-native too). `source` alone never names the app: `vscode`
                    appears for Codex Desktop, T3 Code, codex-tui and zeron-native

A mixed row takes the app a person used, not the SDK: [claude-desktop, sdk-cli] is claude-desktop.

A subagent uses its own evidence first. `sdk` and `unknown` are not real evidence: a subagent of a T3
session has the same `sdk-ts` entrypoint as its parent. So those two take the parent's value when the
parent's value is known.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_memory import claude_roots

T3_CODE, CLAUDE_DESKTOP, CHATGPT_DESKTOP = "t3-code", "claude-desktop", "chatgpt-desktop"
IDE, CLI, SDK, CLOUD, UNKNOWN = "ide", "cli", "sdk", "cloud", "unknown"
VALUES = (T3_CODE, CLAUDE_DESKTOP, CHATGPT_DESKTOP, IDE, CLI, SDK, CLOUD, UNKNOWN)
WEAK = (SDK, UNKNOWN)   # no proof of an app: they never replace a stored stronger value

_RSI_SOURCE = "rsi-native-session/"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _root_is_t3(label: Any) -> bool:
    return isinstance(label, str) and claude_roots.is_t3(label)


def _claude(meta: Mapping[str, Any], session_id: str | None) -> str:
    if meta.get("cloud") is True or _text(session_id).startswith("cloud:"):
        return CLOUD
    if meta.get("client") == "t3" or _root_is_t3(meta.get("claude_root")):
        return T3_CODE
    raw = meta.get("entrypoints")
    entrypoints = [e for e in raw if isinstance(e, str)] if isinstance(raw, list) else []
    if "claude-desktop" in entrypoints:
        return CLAUDE_DESKTOP
    if "claude-vscode" in entrypoints:
        return IDE
    if "cli" in entrypoints:
        return CLI
    if any(e.startswith("sdk") for e in entrypoints) or _text(meta.get("source")).startswith(_RSI_SOURCE):
        return SDK
    return UNKNOWN


def _codex(meta: Mapping[str, Any]) -> str:
    originator, source = _text(meta.get("originator")), _text(meta.get("source"))
    if originator == "T3 Code":
        return T3_CODE
    if originator == "Codex Desktop":
        return CHATGPT_DESKTOP
    if originator == "codex-tui":
        return CLI
    if originator == "codex_exec" or source == "exec" or source.startswith(_RSI_SOURCE):
        return SDK
    if not originator and _root_is_t3(meta.get("codex_root")):
        return T3_CODE
    return UNKNOWN


def derive(tool: str | None, meta: Mapping[str, Any] | None, *, session_id: str | None = None,
           parent: str | None = None) -> str:
    """The session's interface. `parent`: the parent session's interface, for a subagent row."""
    facts = meta if isinstance(meta, Mapping) else {}
    own = _codex(facts) if tool == "codex" else _claude(facts, session_id)
    return parent if own in WEAK and parent is not None and parent != UNKNOWN else own


def keep_stronger(stored: str, new: str) -> str:
    """SQL for the value an update stores. Null is no value. `unknown` and `sdk` never replace a
    stored stronger value (`sdk` does replace `unknown`). Otherwise the new value wins."""
    return (f"case when {new} is null then {stored} when {stored} is null then {new} "
            f"when {new} = '{UNKNOWN}' then {stored} "
            f"when {new} = '{SDK}' and {stored} <> '{UNKNOWN}' then {stored} else {new} end")


# The one update rule of both upserts (sessions_ingest._UPSERT_SQL, session_live.PING_SQL).
STORED_OR_NEW_SQL = keep_stronger("sessions.interface", "excluded.interface")
