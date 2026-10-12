from __future__ import annotations

from conftest import base_env, run_amem

DUMMY_KEY = "dummy-key-must-never-print-0123456789"


def test_doctor_fixture_mode_with_key_but_no_live_flag() -> None:
    env = base_env(
        TYPESAFE_API_KEY=DUMMY_KEY,
        AMEM_DB_URL="postgresql://postgres:secretpw@127.0.0.1:1/postgres",
    )
    result = run_amem("doctor", env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "jev_mode=fixture", "backend=postgres", "db_host=127.0.0.1", "db=unreachable", "require_remote=off",
    ]
    assert DUMMY_KEY not in result.stdout + result.stderr
    assert "secretpw" not in result.stdout + result.stderr


def test_doctor_default_db_url_is_local_supabase() -> None:
    result = run_amem("doctor", env=base_env())
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "jev_mode=fixture"
    assert lines[1] == "backend=postgres"
    assert lines[2] == "db_host=127.0.0.1"
    assert lines[3] in ("db=reachable", "db=unreachable")
    assert lines[4] == "require_remote=off"


def test_doctor_reachable(db_env: dict[str, str]) -> None:
    result = run_amem("doctor", env=db_env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "jev_mode=fixture", "backend=postgres", "db_host=127.0.0.1", "db=reachable", "require_remote=off",
    ]


def test_doctor_require_remote_on() -> None:
    env = base_env(AMEM_REQUIRE_REMOTE="1", AMEM_DB_URL="postgresql://postgres:secretpw@127.0.0.1:1/postgres")
    result = run_amem("doctor", env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "require_remote=on"
    assert "secretpw" not in result.stdout + result.stderr
