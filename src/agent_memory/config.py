"""Environment loading and settings.

Env files are optional. Load order: `.env.local`, then `.env`, from the repo root.
Values already in the process environment always win (override=False), and
`.env.local` wins over `.env` because it loads first.
Set `AMEM_NO_DOTENV=1` to skip env files entirely (tests do this).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
JEV_FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "jev"

_loaded = False


def load_env() -> None:
    """Load `.env.local` then `.env` once, if present. Never required."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if os.environ.get("AMEM_NO_DOTENV") == "1":
        return
    for name in (".env.local", ".env"):
        path = REPO_ROOT / name
        if path.is_file():
            load_dotenv(path, override=False)


def db_url() -> str:
    load_env()
    return os.environ.get("AMEM_DB_URL") or DEFAULT_DB_URL


def typesafe_api_key() -> str:
    load_env()
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def jev_live_flag() -> bool:
    load_env()
    return os.environ.get("AMEM_JEV_LIVE", "").strip() == "1"


def jev_fixtures_dir() -> Path:
    load_env()
    override = os.environ.get("AMEM_JEV_FIXTURES_DIR")
    return Path(override) if override else JEV_FIXTURES_DIR
