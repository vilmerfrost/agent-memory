"""TEST_POSTGRES_URL (conftest.external_pg_url) only accepts an empty database on loopback."""

from __future__ import annotations

import pytest

from conftest import external_pg_url


@pytest.mark.parametrize("host", ["db.example.supabase.co", "10.0.0.5", "pooler.supabase.com"])
def test_a_non_loopback_host_is_refused_before_connecting(host: str) -> None:
    with pytest.raises(RuntimeError, match="loopback"):
        external_pg_url(f"postgresql://postgres:x@{host}:5432/postgres")


def test_a_database_with_tables_is_refused(pg_url: str) -> None:
    # pg_url is migrated, so it already holds the schema: never a target for a fresh suite.
    with pytest.raises(RuntimeError, match="empty database"):
        external_pg_url(pg_url)
