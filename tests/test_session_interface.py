"""`sessions.interface` (docs/sessions-live.md, "Interface"): the one pure rule, and every path that
writes it: transcript ingest, cloud digests, the live ping, the spool and `amem sessions interface`.
The row shapes in CLAUDE_SHAPES and CODEX_SHAPES are the ones the hosted table holds."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import cli, session_interface, session_interface_backfill, session_live, sessions_ingest
from codex_support import Rollout, codex_home, make_codex_kinds, subagent_source, tid
from conftest import base_env, run_amem
from guarded_support import claude_session, guarded_home, plain_sdk_events, session_id, t3_events, write_jsonl, write_roots
from test_deviations_support import NOW, Home, chat_events, setup_home, write_transcript  # noqa: F401
from test_session_live import SID, _ping_cli, _row, db, dead_url, parent, payload, transcript  # noqa: F401
from test_session_tokens import session_events

RSI = "rsi-native-session/1"
T3_THREAD = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
derive = session_interface.derive


@pytest.fixture(autouse=True)
def plain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run inside an agent inherits these. Each test sets the ones it is about."""
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.delenv("T3CODE_THREAD_ID", raising=False)


# --------------------------------------------------------------------------- the pure rule


CLAUDE_SHAPES: list[tuple[dict[str, Any], str]] = [
    ({"entrypoints": ["claude-desktop"]}, "claude-desktop"),
    ({"entrypoints": ["sdk-cli"]}, "sdk"),
    ({"entrypoints": ["sdk-ts"]}, "sdk"),
    ({"entrypoints": ["cli"]}, "cli"),
    ({"entrypoints": ["claude-vscode"]}, "ide"),
    ({"entrypoints": ["cli", "sdk-cli"]}, "cli"),
    ({"entrypoints": ["claude-desktop", "sdk-cli"]}, "claude-desktop"),
    ({"entrypoints": ["sdk-ts"], "client": "t3"}, "t3-code"),
    ({"entrypoints": ["sdk-ts"], "claude_root": "t3-0123456789ab"}, "t3-code"),
    ({"entrypoints": ["sdk-ts"], "claude_root": "t3"}, "t3-code"),
    ({"cloud": True, "prompts": 2}, "cloud"),
    ({"cloud": True, "entrypoints": ["sdk-ts"], "client": "t3"}, "cloud"),
    ({"source": RSI}, "sdk"),
    ({}, "unknown"),
]

CODEX_SHAPES: list[tuple[dict[str, Any], str]] = [
    ({"originator": "Codex Desktop", "source": "vscode"}, "chatgpt-desktop"),
    ({"originator": "T3 Code", "source": "vscode"}, "t3-code"),
    ({"originator": "codex_exec", "source": "exec"}, "sdk"),
    ({"originator": "codex_exec", "source": "subagent"}, "sdk"),
    ({"originator": "codex-tui", "source": "vscode"}, "cli"),
    ({"originator": "zeron-native", "source": "vscode"}, "unknown"),
    ({"source": RSI}, "sdk"),
    ({"originator": None, "codex_root": "t3-transitions-abc"}, "t3-code"),
    ({"originator": "zeron-native", "codex_root": "t3-transitions-abc"}, "unknown"),     # an originator decides
    ({"source": "exec", "codex_root": "t3"}, "sdk"),                                     # rule 4 before rule 5
    ({"source": "vscode"}, "unknown"),                                                   # source alone names no app
    ({}, "unknown"),
]


@pytest.mark.parametrize(("meta", "want"), CLAUDE_SHAPES)
def test_claude_rows(meta: dict[str, Any], want: str) -> None:
    assert derive("claude", meta, session_id="abc") == want


@pytest.mark.parametrize(("meta", "want"), CODEX_SHAPES)
def test_codex_rows(meta: dict[str, Any], want: str) -> None:
    assert derive("codex", meta, session_id="codex:abc") == want


def test_a_cloud_id_is_cloud_without_any_meta() -> None:
    assert derive("claude", {}, session_id="cloud:abc") == "cloud"


@pytest.mark.parametrize("entrypoints", [["mobile"], ["remote-control"], ["Claude-Desktop"], [""], [7], "cli", None])
def test_an_unlisted_entrypoint_is_unknown_not_a_guess(entrypoints: Any) -> None:
    assert derive("claude", {"entrypoints": entrypoints}) == "unknown"


def test_sdk_ts_without_t3_evidence_stays_sdk() -> None:
    assert derive("claude", {"entrypoints": ["sdk-ts"], "claude_root": "other"}) == "sdk"
    assert derive("claude", {"entrypoints": ["sdk-ts"], "client": "other"}) == "sdk"


def test_a_subagent_keeps_real_evidence_of_its_own() -> None:
    assert derive("claude", {"entrypoints": ["claude-desktop"]}, parent="t3-code") == "claude-desktop"
    assert derive("claude", {"entrypoints": ["sdk-ts"], "client": "t3"}, parent="claude-desktop") == "t3-code"
    assert derive("codex", {"originator": "Codex Desktop"}, parent="t3-code") == "chatgpt-desktop"


def test_a_subagent_with_weak_evidence_takes_its_parents_value() -> None:
    assert derive("claude", {"entrypoints": ["sdk-ts"]}, parent="t3-code") == "t3-code"
    assert derive("claude", {}, parent="claude-desktop") == "claude-desktop"
    assert derive("codex", {"originator": "codex_exec"}, parent="chatgpt-desktop") == "chatgpt-desktop"
    assert derive("claude", {}, parent="sdk") == "sdk"


@pytest.mark.parametrize("parent", [None, "unknown"])
def test_an_unknown_parent_changes_nothing(parent: str | None) -> None:
    assert derive("claude", {"entrypoints": ["sdk-ts"]}, parent=parent) == "sdk"
    assert derive("claude", {}, parent=parent) == "unknown"


def test_bad_meta_is_unknown_not_a_crash() -> None:
    assert derive("claude", None) == "unknown"
    assert derive(None, {"claude_root": 7, "cloud": "true"}) == "unknown"
    assert derive("codex", {"originator": 7, "source": {"subagent": {}}, "codex_root": 3}) == "unknown"


def test_a_value_is_always_a_listed_one() -> None:
    for meta, want in (*CLAUDE_SHAPES, *CODEX_SHAPES):
        assert want in session_interface.VALUES, meta


# --------------------------------------------------------------------------- the update rule


@pytest.mark.parametrize(("stored", "new", "want"), [
    (None, None, None), (None, "sdk", "sdk"), (None, "unknown", "unknown"), (None, "t3-code", "t3-code"),
    ("t3-code", None, "t3-code"), ("t3-code", "sdk", "t3-code"), ("t3-code", "unknown", "t3-code"),
    ("claude-desktop", "sdk", "claude-desktop"), ("cloud", "unknown", "cloud"),
    ("sdk", "unknown", "sdk"), ("sdk", "sdk", "sdk"), ("unknown", "unknown", "unknown"),
    ("unknown", "sdk", "sdk"), ("unknown", "cli", "cli"), ("sdk", "t3-code", "t3-code"),
    ("cli", "claude-desktop", "claude-desktop"), ("t3-code", "cli", "cli"),
])
def test_keep_stronger(db: str, stored: str | None, new: str | None, want: str | None) -> None:  # noqa: F811
    sql = session_interface.keep_stronger("%(stored)s::text", "%(new)s::text")
    with psycopg.connect(db) as conn:
        assert conn.execute(f"select {sql}", {"stored": stored, "new": new}).fetchone() == (want,)


# --------------------------------------------------------------------------- ingest


def _collect(home: Home, *, tools: tuple[str, ...] = ("claude",)) -> list[sessions_ingest.SessionRow]:
    codex = codex_home(home) if "codex" in tools else None
    return sessions_ingest.collect(home.projects, days=7, now=NOW, codex=codex, tools=tools)[0]


def _values(url: str) -> dict[str, str | None]:
    with psycopg.connect(url) as conn:
        return dict(conn.execute("select session_id, interface from sessions").fetchall())


def _stored(url: str) -> dict[str, dict[str, Any]]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return {r["session_id"]: r for r in conn.execute("select * from sessions").fetchall()}


def _set(url: str, sid: str, value: str | None) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("update sessions set interface = %s where session_id = %s", (value, sid))


def test_ingest_writes_the_column_for_claude_rows(setup_home: Home, db: str) -> None:  # noqa: F811
    project = setup_home.project()
    write_transcript(project / f"{session_id(1)}.jsonl", chat_events(str(setup_home.repo)))
    write_jsonl(project / f"{session_id(2)}.jsonl", t3_events(session_id(2)))
    write_jsonl(project / f"{session_id(3)}.jsonl", plain_sdk_events(session_id(3)))
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db, tools=("claude",))
    assert _values(db) == {session_id(1): "claude-desktop", session_id(2): "t3-code", session_id(3): "sdk"}


def test_ingest_writes_the_column_for_codex_rows(setup_home: Home, db: str) -> None:  # noqa: F811
    kinds = make_codex_kinds(setup_home)
    Rollout(tid(6), str(setup_home.repo), originator="T3 Code").user("Hi.", 1).busy().write(setup_home)
    Rollout(tid(7), str(setup_home.repo), originator="codex-tui").user("Hi.", 1).busy().write(setup_home)
    Rollout(tid(8), str(setup_home.repo), originator="zeron-native").user("Hi.", 1).busy().write(setup_home)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db, codex=codex_home(setup_home),
                           tools=("codex",))
    got = _values(db)
    assert got[kinds["chat"]] == "chatgpt-desktop" and got[kinds["headless"]] == "sdk"
    assert (got[f"codex:{tid(6)}"], got[f"codex:{tid(7)}"], got[f"codex:{tid(8)}"]) == ("t3-code", "cli", "unknown")


def test_ingest_writes_the_column_for_cloud_rows(setup_home: Home, db: str) -> None:  # noqa: F811
    with psycopg.connect(db) as conn:
        conn.execute("insert into cloud_digests (source_key, kind, routine, session_id, digest, started_at, ended_at, "
                     "meta) values ('cloud:abc', 'routine', 'nightly-trials', 'abc', 'digest', %s, %s, %s)",
                     (NOW, NOW, Jsonb({"prompts": 1})))
    report = sessions_ingest.ingest_cloud(db_url=db, now=NOW)
    assert report.new == 1 and _values(db) == {"cloud:abc": "cloud"}


def test_a_re_ingest_with_no_change_writes_nothing(setup_home: Home, db: str) -> None:  # noqa: F811
    write_transcript(setup_home.project() / f"{session_id(1)}.jsonl", chat_events(str(setup_home.repo)))
    rows = _collect(setup_home)
    assert sessions_ingest.upsert(rows, db_url=db, now=NOW) == (1, 0)
    with psycopg.connect(db) as conn:
        before = conn.execute("select xmin::text from sessions").fetchall()
    assert sessions_ingest.upsert(rows, db_url=db, now=NOW) == (0, 0)
    with psycopg.connect(db) as conn:
        assert conn.execute("select xmin::text from sessions").fetchall() == before


def test_a_re_ingest_that_only_differs_in_interface_writes(setup_home: Home, db: str) -> None:  # noqa: F811
    write_transcript(setup_home.project() / f"{session_id(1)}.jsonl", chat_events(str(setup_home.repo)))
    rows = _collect(setup_home)
    sessions_ingest.upsert(rows, db_url=db, now=NOW)
    _set(db, session_id(1), None)
    assert sessions_ingest.upsert(rows, db_url=db, now=NOW) == (0, 1)
    _set(db, session_id(1), "unknown")
    assert sessions_ingest.upsert(rows, db_url=db, now=NOW) == (0, 1)
    assert _values(db) == {session_id(1): "claude-desktop"}


def test_a_stored_stronger_value_survives_a_weak_ingest_without_a_write(setup_home: Home, db: str) -> None:  # noqa: F811
    write_jsonl(setup_home.project() / f"{session_id(1)}.jsonl", plain_sdk_events(session_id(1)))
    rows = _collect(setup_home)
    sessions_ingest.upsert(rows, db_url=db, now=NOW)
    _set(db, session_id(1), "t3-code")
    assert sessions_ingest.upsert(rows, db_url=db, now=NOW) == (0, 0)
    assert _values(db) == {session_id(1): "t3-code"}


def _subagent(home: Home, parent_sid: str, name: str = "agent-a1") -> Path:
    return write_jsonl(home.project() / parent_sid / "subagents" / f"{name}.jsonl", plain_sdk_events(parent_sid))


def test_a_subagent_takes_its_parents_value_in_one_ingest(setup_home: Home, db: str) -> None:  # noqa: F811
    sid = session_id(1)
    write_jsonl(setup_home.project() / f"{sid}.jsonl", t3_events(sid))
    _subagent(setup_home, sid)
    rows = _collect(setup_home)
    assert [r.session_id for r in rows if "/" in r.session_id] == [f"{sid}/agent-a1"]
    # The subagent is first in the list: the order inside one run does not matter.
    sessions_ingest.upsert(sorted(rows, key=lambda r: "/" not in r.session_id), db_url=db, now=NOW)
    assert _values(db) == {sid: "t3-code", f"{sid}/agent-a1": "t3-code"}
    assert sessions_ingest.upsert(rows, db_url=db, now=NOW) == (0, 0)                  # and a replay changes nothing


def test_a_subagent_finds_a_parent_stored_by_an_earlier_run(setup_home: Home, db: str) -> None:  # noqa: F811
    sid = session_id(1)
    assert session_live.ping(payload("SessionStart", setup_home, sid=sid), now=NOW, db_url=db) == "written"
    _set(db, sid, "claude-desktop")
    _subagent(setup_home, sid)
    rows = [r for r in _collect(setup_home) if "/" in r.session_id]
    sessions_ingest.upsert(rows, db_url=db, now=NOW)
    assert _values(db)[f"{sid}/agent-a1"] == "claude-desktop"


def test_a_parent_that_arrives_later_reaches_its_stored_subagents(setup_home: Home, db: str) -> None:  # noqa: F811
    sid = session_id(1)
    write_jsonl(setup_home.project() / f"{sid}.jsonl", t3_events(sid))
    _subagent(setup_home, sid)
    rows = _collect(setup_home)
    sessions_ingest.upsert([r for r in rows if "/" in r.session_id], db_url=db, now=NOW)
    assert _values(db) == {f"{sid}/agent-a1": "sdk"}
    sessions_ingest.upsert([r for r in rows if "/" not in r.session_id], db_url=db, now=NOW)
    assert _values(db) == {sid: "t3-code", f"{sid}/agent-a1": "t3-code"}


def test_a_codex_subagents_originator_is_its_own_evidence(setup_home: Home, db: str) -> None:  # noqa: F811
    cwd = str(setup_home.repo)
    Rollout(tid(1), cwd, originator="T3 Code").user("Split it.", 1).spawn("w", 2).busy().write(setup_home)
    Rollout(tid(2), cwd, originator="Codex Desktop", source=subagent_source(tid(1), "/root/w"), thread_source="subagent",
            agent_path="/root/w").busy().write(setup_home)
    Rollout(tid(3), cwd, originator="codex_exec", source=subagent_source(tid(1), "/root/x"), thread_source="subagent",
            agent_path="/root/x").busy().write(setup_home)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db, codex=codex_home(setup_home),
                           tools=("codex",))
    got = _values(db)
    assert (got[f"codex:{tid(1)}"], got[f"codex:{tid(2)}"], got[f"codex:{tid(3)}"]) == ("t3-code", "chatgpt-desktop",
                                                                                       "t3-code")


def test_a_native_observation_row_keeps_working(setup_home: Home, db: str) -> None:  # noqa: F811
    """A row of attempt_links keeps its env; its transcript fills the interface."""
    sid = session_id(1)
    with psycopg.connect(db) as conn:
        conn.execute("insert into sessions (session_id, kind, project_dir, env, experiment_id, meta) "
                     "values (%s, 'headless', 'rsi-native-session', 'sandbox', 'exp-one', %s)",
                     (sid, Jsonb({"source": RSI, "rsi_native_observation": True})))
    write_jsonl(setup_home.project() / f"{sid}.jsonl", plain_sdk_events(sid))
    assert sessions_ingest.upsert(_collect(setup_home), db_url=db, now=NOW) == (0, 1)
    row = _stored(db)[sid]
    assert (row["interface"], row["env"], row["experiment_id"]) == ("sdk", "sandbox", "exp-one")
    assert row["meta"]["transcript_available"] is True


# --------------------------------------------------------------------------- the live ping


T3_ROWS = [("sdk-ts", T3_THREAD, "t3-code", "chat", "t3"), ("sdk-cli", T3_THREAD, "sdk", "headless", None),
           ("sdk-ts", None, "sdk", "headless", None), ("sdk-ts", "  ", "sdk", "headless", None),
           ("claude-desktop", None, "claude-desktop", "chat", None), ("cli", T3_THREAD, "cli", "chat", None),
           ("claude-vscode", None, "ide", "chat", None), ("mobile", None, "unknown", "chat", None),
           (None, None, "unknown", "chat", None)]


@pytest.mark.parametrize(("entrypoint", "thread", "want", "kind", "client"), T3_ROWS)
def test_a_ping_writes_the_column(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch, entrypoint: str | None,  # noqa: F811
                                  thread: str | None, want: str, kind: str, client: str | None) -> None:
    if entrypoint:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
    if thread:
        monkeypatch.setenv("T3CODE_THREAD_ID", thread)
    assert session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=db) == "written"
    row = _row(db)
    assert row is not None and (row["interface"], row["kind"], row["meta"].get("client")) == (want, kind, client)
    assert "entrypoints" not in row["meta"]   # ingest owns that key


def test_a_stop_ping_writes_the_column_too(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    transcript(setup_home, session_events(str(setup_home.repo)))
    assert session_live.ping(payload("Stop", setup_home), now=NOW, db_url=db) == "written"
    assert _row(db)["interface"] == "cli"  # type: ignore[index]


def test_a_ping_from_a_t3_root_is_t3_code_for_any_entrypoint(setup_home: Home, db: str, tmp_path: Path,  # noqa: F811
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    control = tmp_path / "control"
    write_roots(setup_home.claude, f"t3\t{control}/native/*/home")
    path = claude_session(guarded_home(control, "native", "thread-a"), session_id(1))
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    body = json.dumps({"session_id": session_id(1), "transcript_path": str(path), "cwd": str(setup_home.repo),
                       "hook_event_name": "SessionStart", "source": "startup"})
    assert session_live.ping(body, now=NOW, db_url=db) == "written"
    row = _row(db, session_id(1))
    assert row is not None and (row["interface"], row["kind"], row["meta"]["client"]) == ("t3-code", "chat", "t3")


def test_an_eval_ping_stays_an_eval_inside_t3(setup_home: Home, db: str, tmp_path: Path,  # noqa: F811
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-ts")
    monkeypatch.setenv("T3CODE_THREAD_ID", T3_THREAD)
    body = json.dumps({"session_id": SID, "cwd": str(tmp_path / ".cache" / "agent-evals" / "run-1"),
                       "hook_event_name": "SessionStart", "source": "startup"})
    assert session_live.ping(body, now=NOW, db_url=db) == "written"
    row = _row(db)
    assert row is not None and (row["kind"], row["interface"]) == ("eval", "t3-code")


@pytest.mark.parametrize("later", [None, "sdk-ts", "sdk-cli", "mobile"])
def test_a_stored_t3_code_survives_a_later_weak_ping(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                     later: str | None) -> None:
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-ts")
    monkeypatch.setenv("T3CODE_THREAD_ID", T3_THREAD)
    session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=db)
    monkeypatch.delenv("T3CODE_THREAD_ID")
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT")
    if later:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", later)
    transcript(setup_home, session_events(str(setup_home.repo)))
    assert session_live.ping(payload("Stop", setup_home), now=NOW + timedelta(minutes=1), db_url=db) == "written"
    assert _row(db)["interface"] == "t3-code"  # type: ignore[index]


def test_a_stronger_ping_replaces_a_stored_weak_value(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=db)
    assert _row(db)["interface"] == "unknown"  # type: ignore[index]
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=db)
    assert _row(db)["interface"] == "sdk"  # type: ignore[index]
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=db)
    assert _row(db)["interface"] == "claude-desktop"  # type: ignore[index]


def test_a_stored_t3_code_from_a_ping_survives_ingest(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """The transcript of this session has no T3 evidence, so ingest alone would say sdk."""
    sid = session_id(1)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-ts")
    monkeypatch.setenv("T3CODE_THREAD_ID", T3_THREAD)
    session_live.ping(payload("SessionStart", setup_home, sid=sid), now=NOW, db_url=db)
    write_jsonl(setup_home.project() / f"{sid}.jsonl", plain_sdk_events(sid))
    sessions_ingest.upsert(_collect(setup_home), db_url=db, now=NOW)
    row = _stored(db)[sid]
    assert (row["interface"], row["meta"]["client"]) == ("t3-code", "t3")


def test_ingest_replaces_a_weak_ping_with_its_stronger_value(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    sid = session_id(1)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-ts")
    session_live.ping(payload("SessionStart", setup_home, sid=sid), now=NOW, db_url=db)
    assert _stored(db)[sid]["interface"] == "sdk"
    write_jsonl(setup_home.project() / f"{sid}.jsonl", t3_events(sid))
    sessions_ingest.upsert(_collect(setup_home), db_url=db, now=NOW)
    assert _stored(db)[sid]["interface"] == "t3-code"


# --------------------------------------------------------------------------- the spool


def test_an_interface_passes_through_the_spool(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    assert session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=dead_url()).startswith("spooled")
    spool = parent(setup_home) / session_live.SPOOL_NAME
    assert json.loads(spool.read_text())["interface"] == "claude-desktop"
    assert session_live.flush_spool(db_url=db) == 1
    assert _row(db)["interface"] == "claude-desktop"  # type: ignore[index]


@pytest.mark.parametrize("stored", [None, "t3-code"])
def test_a_spool_record_without_the_key_still_flushes(setup_home: Home, db: str, stored: str | None) -> None:  # noqa: F811
    session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=dead_url())
    spool = parent(setup_home) / session_live.SPOOL_NAME
    rec = json.loads(spool.read_text())
    rec.pop("interface")
    spool.write_text(json.dumps(rec) + "\n")
    if stored:
        session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=dead_url())
        spool.write_text(json.dumps(rec) + "\n")
        with psycopg.connect(db) as conn:
            conn.execute("insert into sessions (session_id, kind, project_dir, interface) values (%s, 'chat', 'x', %s)",
                         (SID, stored))
    assert session_live.flush_spool(db_url=db) == 1
    row = _row(db)
    assert row is not None and row["interface"] == stored and row["repo"] == "demo-repo"


@pytest.fixture
def no_column(db: str) -> Iterator[str]:  # noqa: F811
    """The window between the code merge and the migration."""
    with psycopg.connect(db) as conn:
        conn.execute("alter table sessions rename column interface to interface_later")
    try:
        yield db
    finally:
        with psycopg.connect(db) as conn:
            conn.execute("alter table sessions rename column interface_later to interface")


def test_a_ping_before_the_migration_spools_and_loses_nothing(setup_home: Home, no_column: str,  # noqa: F811
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    status = session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=no_column)
    assert status == "spooled (db error: UndefinedColumn)"
    status = session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=no_column)
    assert status == "spooled (db error: UndefinedColumn)"     # the failed flush put the first record back
    spool = parent(setup_home) / session_live.SPOOL_NAME
    assert len(spool.read_text().splitlines()) == 2 and not spool.with_name(spool.name + ".flushing").exists()
    with psycopg.connect(no_column) as conn:
        conn.execute("alter table sessions rename column interface_later to interface")
    try:
        assert session_live.flush_spool(db_url=no_column) == 2
        assert _row(no_column)["interface"] == "claude-desktop"  # type: ignore[index]
    finally:
        with psycopg.connect(no_column) as conn:
            conn.execute("alter table sessions rename column interface to interface_later")


# --------------------------------------------------------------------------- the backfill


def _insert(url: str, sid: str, meta: dict[str, Any], *, tool: str = "claude", value: str | None = None) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into sessions (session_id, tool, kind, project_dir, env, meta, interface, last_activity_at) "
                     "values (%s, %s, 'chat', '/si-test', 'prod', %s, %s, %s)",
                     (sid, tool, Jsonb(meta), value, NOW))


@pytest.fixture
def stored_rows(db: str) -> str:  # noqa: F811
    """One row per hosted shape, plus rows that are no candidates."""
    for n, (meta, _want) in enumerate(CLAUDE_SHAPES):
        _insert(db, f"c{n}", meta)
    for n, (meta, _want) in enumerate(CODEX_SHAPES):
        _insert(db, f"codex:{n}", meta, tool="codex")
    _insert(db, "t3-parent", {"entrypoints": ["sdk-ts"], "client": "t3"})
    _insert(db, "t3-parent/agent-a1", {"entrypoints": ["sdk-ts"], "parent_session": "t3-parent"})   # sorts after: fine
    _insert(db, "a-sub", {"entrypoints": ["sdk-ts"], "parent_session": "z-parent"})                 # sorts before
    _insert(db, "z-parent", {"entrypoints": ["claude-desktop"]})
    _insert(db, "a-deep", {"parent_session": "a-sub"})                                              # two levels up
    _insert(db, "s-sub", {"entrypoints": ["sdk-ts"], "parent_session": "s-parent"})
    _insert(db, "s-parent", {"entrypoints": ["sdk-ts"]}, value="t3-code")                           # a stored parent
    _insert(db, "own", {"entrypoints": ["claude-desktop"], "parent_session": "t3-parent"})
    _insert(db, "done", {"entrypoints": ["sdk-ts"]}, value="t3-code")                               # no candidate
    return db


def _plan(url: str) -> session_interface_backfill.Plan:
    return session_interface_backfill.plan(db_url=url)


def test_the_plan_covers_every_shape_and_puts_parents_first(stored_rows: str) -> None:
    got = {r.session_id: r.interface for r in _plan(stored_rows).rows}
    for n, (meta, want) in enumerate(CLAUDE_SHAPES):
        assert got[f"c{n}"] == want, meta
    for n, (meta, want) in enumerate(CODEX_SHAPES):
        assert got[f"codex:{n}"] == want, meta
    assert got["t3-parent/agent-a1"] == "t3-code" and got["a-sub"] == "claude-desktop"
    assert got["a-deep"] == "claude-desktop" and got["s-sub"] == "t3-code" and got["own"] == "claude-desktop"
    assert "done" not in got and "s-parent" not in got


def test_a_parent_cycle_ends(db: str) -> None:  # noqa: F811
    _insert(db, "a", {"parent_session": "b"})
    _insert(db, "b", {"parent_session": "a", "entrypoints": ["cli"]})
    assert {r.session_id: r.interface for r in _plan(db).rows} == {"a": "cli", "b": "cli"}


def test_the_dry_run_is_read_only_and_writes_nothing(stored_rows: str) -> None:
    before = _stored(stored_rows)
    ro = stored_rows + ("&" if "?" in stored_rows else "?") + "options=-c%20default_transaction_read_only%3Don"
    plan = _plan(ro)
    counts = plan.counts()
    assert counts["candidates"] == len(CLAUDE_SHAPES) + len(CODEX_SHAPES) + 7 == len(plan.rows)
    assert sum(v for k, v in counts.items() if k != "candidates") == counts["candidates"]
    assert counts["unknown"] > 0 and counts["t3-code"] > 0
    assert _stored(stored_rows) == before


def test_apply_changes_only_the_column_and_a_second_apply_changes_nothing(stored_rows: str, tmp_path: Path) -> None:
    before = _stored(stored_rows)
    with psycopg.connect(stored_rows) as conn:
        xmin = conn.execute("select xmin::text from sessions where session_id = 'done'").fetchone()
    plan = _plan(stored_rows)
    assert session_interface_backfill.apply(plan, tmp_path / "a.json", db_url=stored_rows) == len(plan.rows) > 0
    after = _stored(stored_rows)
    assert set(after) == set(before)
    for sid, row in after.items():
        assert {**row, "interface": None} == {**before[sid], "interface": None}, sid
        assert row["interface"] is not None
    with psycopg.connect(stored_rows) as conn:
        assert conn.execute("select xmin::text from sessions where session_id = 'done'").fetchone() == xmin
    assert _plan(stored_rows).rows == []
    assert session_interface_backfill.apply(_plan(stored_rows), tmp_path / "b.json", db_url=stored_rows) == 0
    assert session_interface_backfill.apply(plan, tmp_path / "c.json", db_url=stored_rows) == 0  # a stale plan too
    assert _stored(stored_rows) == after


def test_apply_rechecks_at_write_time(db: str, tmp_path: Path) -> None:  # noqa: F811
    _insert(db, "r1", {"entrypoints": ["sdk-ts"]})
    _insert(db, "r2", {"entrypoints": ["cli"]})
    plan = _plan(db)
    _set(db, "r1", "t3-code")                                       # a ping arrives between plan and write
    log = tmp_path / "a.json"
    assert session_interface_backfill.apply(plan, log, db_url=db) == 1
    assert _values(db) == {"r1": "t3-code", "r2": "cli"}
    assert json.loads(log.read_text())["rows"] == {"r2": "cli"}     # the log holds only what it wrote


def test_a_failed_log_write_writes_nothing(db: str, tmp_path: Path) -> None:  # noqa: F811
    _insert(db, "r1", {"entrypoints": ["cli"]})
    (tmp_path / "file").write_text("")
    with pytest.raises(OSError):
        session_interface_backfill.apply(_plan(db), tmp_path / "file" / "log.json", db_url=db)
    assert _values(db) == {"r1": None}


def test_revert_undoes_only_what_apply_wrote(db: str, tmp_path: Path) -> None:  # noqa: F811
    _insert(db, "r1", {"entrypoints": ["sdk-ts"]})
    _insert(db, "r2", {"entrypoints": ["cli"]})
    _insert(db, "r3", {"entrypoints": ["cli"]}, value="claude-desktop")
    before = _stored(db)
    log = tmp_path / "a.json"
    session_interface_backfill.apply(_plan(db), log, db_url=db)
    _set(db, "r1", "t3-code")                                       # ingest changed it after the backfill
    assert session_interface_backfill.revert(log, db_url=db) == 1
    assert _values(db) == {"r1": "t3-code", "r2": None, "r3": "claude-desktop"}
    _set(db, "r1", None)
    assert _stored(db) == before
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(ValueError):
        session_interface_backfill.revert(tmp_path / "bad.json", db_url=db)


def test_sessions_interface_cli(stored_rows: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                tmp_path: Path) -> None:
    monkeypatch.setenv("AMEM_DB_URL", stored_rows)
    assert cli.main(["sessions", "interface"]) == 0
    out = capsys.readouterr().out
    assert "t3-code=" in out and "dry run: nothing written" in out
    assert cli.main(["sessions", "interface", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["dry_run"] is True and doc["applied"] is None and doc["log"] is None
    assert doc["counts"]["candidates"] == len(doc["rows"]) > 0
    assert cli.main(["sessions", "interface", "--apply", "--revert", "x"]) == 2
    capsys.readouterr()
    log = tmp_path / "interface.json"
    assert cli.main(["sessions", "interface", "--apply", "--log", str(log)]) == 0
    assert "applied: " in capsys.readouterr().out and log.is_file()
    assert _values(stored_rows)["t3-parent/agent-a1"] == "t3-code"
    assert cli.main(["sessions", "interface", "--apply", "--log", str(tmp_path / "second.json")]) == 0
    assert "applied: 0 row(s)" in capsys.readouterr().out
    res = run_amem("sessions", "interface", "--revert", str(log), "--json", env=base_env(AMEM_DB_URL=stored_rows))
    assert res.returncode == 0 and json.loads(res.stdout)["reverted"] == doc["counts"]["candidates"]
    assert _values(stored_rows)["t3-parent/agent-a1"] is None and _values(stored_rows)["done"] == "t3-code"


# --------------------------------------------------------------------------- the real command


@pytest.mark.parametrize(("env", "want", "kind"), [
    ({"CLAUDE_CODE_ENTRYPOINT": "sdk-ts", "T3CODE_THREAD_ID": T3_THREAD}, "t3-code", "chat"),
    ({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "T3CODE_THREAD_ID": T3_THREAD}, "sdk", "headless"),
    ({"CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}, "sdk", "headless"),
    ({"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}, "claude-desktop", "chat"),
])
def test_the_real_ping_command(setup_home: Home, db: str, env: dict[str, str], want: str, kind: str) -> None:  # noqa: F811
    res = _ping_cli(payload("SessionStart", setup_home), AMEM_DB_URL=db, AMEM_PING_ALLOW_LOCAL="1",
                    AMEM_CLAUDE_HOME=str(setup_home.claude), **env)
    assert res.returncode == 0 and " SessionStart 5e55a0b1 written " in res.stderr, res.stderr
    row = _row(db)
    assert row is not None and (row["interface"], row["kind"]) == (want, kind)
