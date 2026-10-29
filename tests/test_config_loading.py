"""Configuration consumers must not observe another thread's partial startup."""
import threading

import pytest

from agent_memory import config
from agent_memory.integrations import clickhouse


def test_concurrent_client_waits_for_complete_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_loaded", False)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("AMEM_NO_DOTENV")
    (tmp_path / ".env.local").touch()
    (tmp_path / ".env").touch()
    entered, release, observing, done = (threading.Event() for _ in range(4))
    calls, results, errors = [], [], []

    def load(path, *, override):
        calls.append((path.name, override))
        if path.name == ".env.local":
            entered.set()
            assert release.wait(3)
            monkeypatch.setenv("AMEM_CLICKHOUSE_URL", "http://localhost:8123")

    monkeypatch.setattr(config, "load_dotenv", load)

    def first():
        try:
            config.load_env()
        except BaseException as exc:
            errors.append(exc)

    def second():
        observing.set()
        try:
            results.append(clickhouse.config().url)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    a, b = threading.Thread(target=first), threading.Thread(target=second)
    a.start()
    try:
        assert entered.wait(3)
        b.start()
        assert observing.wait(3)
        returned_before_ready = done.wait(.1)
    finally:
        release.set()
        a.join(3)
        if b.ident is not None:
            b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert not errors
    assert not returned_before_ready, "client read configuration before loading finished"
    assert results == ["http://localhost:8123"]
    assert calls == [(".env.local", False), (".env", False)]


def test_failed_load_is_not_marked_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_loaded", False)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("AMEM_NO_DOTENV")
    (tmp_path / ".env.local").touch()
    calls = []

    def load(path, *, override):
        calls.append(path.name)
        if len(calls) == 1:
            raise OSError("fixture load failed")

    monkeypatch.setattr(config, "load_dotenv", load)
    with pytest.raises(OSError, match="fixture load failed"):
        config.load_env()
    assert not config._loaded
    config.load_env()
    assert calls == [".env.local", ".env.local"]
    assert config._loaded


def test_disabled_dotenv_does_not_touch_files(monkeypatch):
    monkeypatch.setattr(config, "_loaded", False)
    monkeypatch.setenv("AMEM_NO_DOTENV", "1")
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **kw: pytest.fail("dotenv disabled"))
    config.load_env()
    assert config._loaded
