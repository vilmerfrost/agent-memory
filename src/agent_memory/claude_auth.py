"""Private subscription credentials for host-side Claude jobs. Never log token values."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from dotenv import dotenv_values

from agent_memory import config

AUTH_FAILURE = "Claude subscription authentication unavailable; renew the dedicated automation token"


class AuthUnavailable(RuntimeError):
    pass


@dataclass
class Batch:
    failed: bool = False


_batch: ContextVar[Batch | None] = ContextVar("claude_auth_batch", default=None)


@contextmanager
def batch():
    state = Batch()
    reset = _batch.set(state)
    try:
        yield state
    finally:
        _batch.reset(reset)


def failed() -> bool:
    state = _batch.get()
    return bool(state and state.failed)


def mark_failed() -> None:
    state = _batch.get()
    if state is not None:
        state.failed = True


def is_auth_error(message: str) -> bool:
    text = message.lower()
    return any(s in text for s in (
        "failed to authenticate", "oauth session expired", "oauth token has expired",
        "oauth token revoked", "invalid authentication credentials", "authentication_error",
        "oauth token is invalid", "api error: 401", "invalid bearer token",
        "not logged in", "please run /login", "please login again",
        AUTH_FAILURE.lower(),
    ))


def environment(base: dict[str, str]) -> dict[str, str]:
    """Inject only into the child, covering writers, summaries and legacy host stages.

    Tests disable automatic discovery with AMEM_NO_DOTENV. An explicit home still
    exercises the same loader. No fallback to a personal login or API billing.
    """
    home_arg = os.environ.get("AMEM_CLAUDE_AUTH_HOME")
    if not home_arg and os.environ.get("AMEM_NO_DOTENV") == "1":
        return dict(base)
    home = Path(home_arg) if home_arg else config.REPO_ROOT / ".subscription-auth" / "claude"
    secret = home / ".env"
    try:
        if not home.is_absolute() or home.is_symlink() or home.resolve() != home:
            raise AuthUnavailable(AUTH_FAILURE)
        for path in (home, secret):
            info = path.lstat()
            if path.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise AuthUnavailable(AUTH_FAILURE)
        if not secret.is_file():
            raise AuthUnavailable(AUTH_FAILURE)
        token = dotenv_values(secret, interpolate=False).get("CLAUDE_CODE_OAUTH_TOKEN")
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
            raise AuthUnavailable(AUTH_FAILURE)
    except (OSError, ValueError):
        raise AuthUnavailable(AUTH_FAILURE) from None
    env = {k: v for k, v in base.items() if k not in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
        "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    )}
    env.update(CLAUDE_CODE_OAUTH_TOKEN=token, CLAUDE_CONFIG_DIR=str(home / ".claude"))
    return env
