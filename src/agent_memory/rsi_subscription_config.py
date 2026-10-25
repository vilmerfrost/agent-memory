"""Non-secret subscription configuration shared by the night and replay adapters."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import shutil

MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6.1-sol"}
EFFORT = "high"
AUTH_MODES = ("api", "subscription")


def validate_role(provider: str, model: str | None, effort: str | None, mode: str,
                  auth: Mapping | None, *, credentials: str = "none") -> None:
    if mode not in AUTH_MODES:
        raise ValueError("auth_mode must be api or subscription")
    if mode == "subscription":
        if provider not in MODELS or model != MODELS[provider] or effort != EFFORT:
            raise ValueError("subscription roles require the approved exact model and high effort")
        if credentials != "none":
            raise ValueError("subscription roles cannot include API credentials or fallback")
        if not isinstance(auth, Mapping) or not isinstance(auth.get("home"), str) or not auth["home"]:
            raise ValueError("subscription roles require a dedicated auth.home")
        if set(auth) - {"home", "client", "docker", "docker_host"}:
            raise ValueError("unknown subscription auth configuration (never put secrets in config)")


def auth_for(provider: str, settings: Mapping, *, tools: bool):
    from agent_memory.rsi_subscription import SubscriptionAuth, SubscriptionError
    from agent_memory.rsi_replay import sandbox_module
    def executable(key: str, default: str) -> Path:
        raw = settings.get(key) or shutil.which(default)
        if not isinstance(raw, str) or not raw:
            raise SubscriptionError(f"{key}_unavailable")
        path = Path(raw).expanduser().resolve()
        if not path.is_absolute() or not path.is_file():
            raise SubscriptionError(f"{key}_unavailable")
        return path
    home = Path(str(settings.get("home") or "")).expanduser().absolute()
    def claude_token():
        # Only the trusted client loads this file, written by the native secret popup.
        # Never print it or include it in the snapshot, task process or report.
        import os, stat
        from dotenv import dotenv_values
        secret = home / ".env"
        if secret.is_symlink() or not secret.is_file() or secret.stat().st_uid != os.getuid():
            raise SubscriptionError("subscription_auth_unavailable")
        if stat.S_IMODE(secret.stat().st_mode) & 0o077:
            raise SubscriptionError("subscription_secret_permissions")
        value = dotenv_values(secret, interpolate=False).get("CLAUDE_CODE_OAUTH_TOKEN")
        if not value:
            raise SubscriptionError("subscription_auth_unavailable")
        return value
    return SubscriptionAuth(home=home, client=executable("client", provider),
                            docker=executable("docker", "docker") if tools else None,
                            worker_image=sandbox_module().image_id("worker") if tools else None,
                            docker_host=settings.get("docker_host"),
                            claude_token=claude_token if provider == "claude" else None)


def usage_fields(usage: Mapping) -> dict[str, int]:
    def count(key):
        value = usage.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid subscription token usage")
        return value
    # Runtime adapters normalize Codex into these same names. Claude input excludes cache tokens.
    cached, written = count("cache_read_input_tokens"), count("cache_creation_input_tokens")
    if count("input_tokens") + cached + written + count("output_tokens") == 0:
        raise ValueError("subscription token usage was not recorded")
    return {"tokens_in": count("input_tokens") + cached + written,
            "tokens_out": count("output_tokens"), "tokens_cached": cached, "tokens_cache_write": written}


def credential_problem(provider: str, settings: Mapping) -> str | None:
    """Inspect only metadata/status, never authentication file contents or token values."""
    import json
    from agent_memory import rsi_subscription as sub
    try:
        auth = auth_for(provider, settings, tools=False)
        home = sub._private_home(auth.home)
        (home / "tmp").mkdir(mode=0o700, exist_ok=True)
        env = sub._clean_env(auth, provider)
        command = [str(auth.client), "auth", "status"] if provider == "claude" else [str(auth.client), "login", "status"]
        code, out, err = sub._bridge().bounded_process(command, env=env, cwd=str(home), timeout_s=15, max_output=65536)
        if provider == "claude":
            status = json.loads(out)
            valid = status.get("loggedIn") and status.get("authMethod") in ("claude.ai", "oauth_token")
        else:
            valid = "Logged in using ChatGPT" in out + err
        if code or not valid:
            return "subscription login required in dedicated auth home"
    except (OSError, ValueError, sub.SubscriptionError):
        return "dedicated subscription auth home or client unavailable"
    except Exception:
        return "subscription authentication status unavailable"
    return None
