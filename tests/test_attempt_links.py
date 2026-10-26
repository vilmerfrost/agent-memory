"""Experiment-attempt identity links (R1; docs/data-use/relationships.md, "Experiment attempts").

A disposable database and a temp RSI sandbox root shaped like the real one on 2026-10-26: a stub replay with
agent logs and sidecars, judge and check runs; a two-provider subscription replay with only run records, check
logs and workspaces; an old-format run log whose only clue is the timestamp in its id; a sidecar that contradicts
its run id. Eval-harness sessions, a sandbox session that started next to an attempt, and Langfuse trace evidence
sit in the database. Nothing outside tmp_path and the test database is read or written.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from agent_memory import attempt_links as AL, coverage_links as CL, rsi_night as N, rsi_replay as R
from agent_memory.integrations import refs
from conftest import base_env, run_amem
from rsi_night_support import KEY_A, RULE_A, adapters, clean_db, make_home, seed_rule, strip_agent_env
from rsi_replay_support import FakeBudget, HostSandbox, setup_root

DB = "attempt_links"
APP_PW = "app-test-password-0123456789"
OWNER = "00000000-0000-4000-8000-0000000000aa"
STRANGER = "00000000-0000-4000-8000-0000000000bb"
MIGRATION = Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "20261026171500_rsi_attempts.sql"

EXP_A = "exp-20261024-aaaa1111"   # stub replay: agent logs with (old-shape) sidecars, a judge run, check runs
EXP_B = "exp-20261025-bbbb2222"   # claude + codex subscription replay: run records, check logs, workspaces only
EXP_C = "exp-20261016-cccc3333"   # old run-id format, no sidecar, no record: only a timestamp in the id
EXP_D = "exp-20261025-dddd4444"   # a sidecar that contradicts its run id
EXP_E = "exp-20261025-eeee5555"   # an experiment row that never reached the replay
OLD_RUN = "B-20261016T101500Z-bbbbbb"


def rid(exp: str, case: str, arm: str, rep: int, profile: str | None = None, suffix: str = "") -> str:
    return R.run_id_for(exp + (f"-{profile}" if profile else ""), case, arm, rep, suffix)


def put(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def run_log(root: Path, exp: str, run_id: str, arm: str) -> None:
    put(root / "experiments" / exp / "logs" / f"{run_id}.json",
        {"experiment": exp, "arm": arm, "run_id": run_id, "container": f"rsi-w-{exp}-{arm}-abc123", "exit_code": 0,
         "timed_out": False, "duration_s": 4.2, "stdout": "", "stderr": ""})


def sidecar(root: Path, exp: str, run_id: str, **kw: Any) -> None:
    put(root / "experiments" / exp / "logs" / f"{run_id}.rsi.json",
        {"kind": "sandbox", "env": "sandbox", "proposal_id": None, "check_passed": True, **kw})


def run_record(root: Path, exp: str, profile: str | None, case: str, arm: str, rep: int) -> None:
    run_id = rid(exp, case, arm, rep, profile)
    d = root / "replays" / exp / (profile or "")
    put(d / "replay.json", {"format": "rsi-replay/1", "experiment_id": exp})
    # The session id these records carry was derived without the profile (the pre-R1 collision).
    put(d / "runs" / f"{run_id}.json", {"run_id": run_id, "arm": arm, "case": case, "rep": rep, "agent": profile or "stub",
                                        "session_id": refs.session_uuid(refs.run_key(exp, arm, case, rep)),
                                        "check_run_id": f"{run_id}-chk"})


def make_sandbox(root: Path) -> None:
    for arm in "AB":
        r = rid(EXP_A, "fix-add", arm, 0)
        run_log(root, EXP_A, r, arm)
        sidecar(root, EXP_A, r, case="fix-add", rep=0, judge_winner="A")         # the shape on disk before R1
        run_log(root, EXP_A, f"{r}-chk", "K")
        run_record(root, EXP_A, None, "fix-add", arm, 0)
        (root / "experiments" / EXP_A / "runs" / r).mkdir(parents=True)
        (root / "experiments" / EXP_A / "runs" / f"{r}-chk").mkdir(parents=True)
    run_log(root, EXP_A, rid(EXP_A, "fix-add", "J", 0), "J")
    run_log(root, EXP_A, rid(EXP_A, "docs-typo", "A", 0), "A")                     # no sidecar, no record
    for profile in ("claude", "codex"):
        for arm in "AB":
            r = rid(EXP_B, "pepe-lock", arm, 0, profile)
            run_record(root, EXP_B, profile, "pepe-lock", arm, 0)
            run_log(root, EXP_B, f"{r}-chk", "K")
            (root / "experiments" / EXP_B / "runs" / r).mkdir(parents=True)
    run_log(root, EXP_C, OLD_RUN, "B")
    r = rid(EXP_D, "fix-add", "A", 0)
    run_log(root, EXP_D, r, "A")
    sidecar(root, EXP_D, r, case="fix-add", rep=1)


def canonical(exp: str, arm: str, case: str, rep: int, profile: str | None = None) -> str:
    return AL.Attempt(exp, arm, case, rep, profile).session_id


def seed(url: str) -> None:
    clean_db(url)
    with psycopg.connect(url) as c:
        c.execute("delete from rsi_attempts")
        for i in range(3):
            c.execute("insert into sessions (session_id, kind, project_dir, env, started_at) values "
                      "(%s, 'eval', %s, 'eval', '2026-10-20T10:00:00Z')",
                      (f"eval-{i}", f"-Users-v--cache-agent-evals-eval-jo-{i}"))
        c.execute("insert into sessions (session_id, kind, project_dir, env) values "
                  "('probe-1', 'eval', '-Users-v--cache-sbx-1', 'eval')")
        # A sandbox session that started one second after an attempt: time is no identifier, so it stays unresolved.
        c.execute("insert into sessions (session_id, kind, project_dir, env, started_at) values "
                  "('sbx-near', 'headless', '-Users-v-sandbox', 'sandbox', '2026-10-24T23:30:36Z')")
        c.execute("insert into sessions (session_id, kind, project_dir, env, experiment_id, started_at) values "
                  "(%s, 'headless', '-Users-v-sandbox', 'sandbox', %s, '2026-10-24T23:30:35Z')",
                  (canonical(EXP_A, "A", "fix-add", 0), EXP_A))
        for x in (EXP_A, EXP_B, EXP_E):
            c.execute("insert into rsi_experiments (id, kind) values (%s, 'edit_instruction')", (x,))
        b, j, d = rid(EXP_A, "fix-add", "B", 0), rid(EXP_A, "fix-add", "J", 0), rid(EXP_A, "docs-typo", "A", 0)
        for ref, m in ((b, {"case": "fix-add", "rep": 0, "arm": "B", "amem_session_id": canonical(EXP_A, "B", "fix-add", 0)}),
                       (j, {"case": j, "rep": 0, "arm": "J"}),
                       (d, {"case": d, "rep": 0, "arm": "A"})):                    # exported without a sidecar
            c.execute("insert into rsi_evidence (system, kind, subject_refs, external_ref, metrics) values "
                      "('langfuse', 'trace', %s, %s, %s)",
                      ([f"amem:experiment/{EXP_A}"], "lf:trace/" + refs.trace_id(ref),
                       json.dumps({"experiment_id": EXP_A, "run_id": ref, "kind": "sandbox", **m})))


@pytest.fixture(scope="module")
def url(pg_url: str) -> Iterator[str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"drop database if exists {DB} with (force)")
        conn.execute(f"create database {DB}")
    u = pg_url.rsplit("/", 1)[0] + f"/{DB}"
    try:
        assert run_amem("db", "migrate", env=base_env(AMEM_DB_URL=u)).returncode == 0
        yield u
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(f"drop database if exists {DB} with (force)")


@pytest.fixture
def world(url: str, tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "sandbox"
    make_sandbox(root)
    seed(url)
    manifests = tmp_path / "manifests"
    env = base_env(AMEM_DB_URL=url, RSI_SANDBOX_DIR=str(root), AMEM_REPAIR_LINKS_DIR=str(manifests))
    return SimpleNamespace(url=url, root=root, manifests=manifests, env=env)


def amem(w: SimpleNamespace, *args: str) -> Any:
    out = run_amem(*args, "--json", env=w.env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def tables(url: str) -> dict[str, str]:
    """A content hash per public table (statistics counters lag; contents do not)."""
    with psycopg.connect(url) as conn:
        names = [r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public'")]
        return {t: conn.execute(f"select md5(coalesce(string_agg(x::text, '|' order by x::text), '')) "
                                f"from public.{t} x").fetchone()[0] for t in names}


def attempts(url: str) -> dict[str, dict[str, Any]]:
    with psycopg.connect(url) as conn:
        cur = conn.execute("select * from rsi_attempts order by id")
        cols = [d.name for d in cur.description]
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


def attempt_link(url: str) -> dict[str, Any]:
    with psycopg.connect(url, autocommit=True) as conn:
        return {r["key"]: r for r in CL.counts(conn)}["experiment_attempt_sessions"]


# --------------------------------------------------------------------------- identity


def test_the_identity_separates_providers_and_keeps_the_unprofiled_id() -> None:
    a, b = AL.Attempt(EXP_B, "A", "pepe-lock", 0, "claude"), AL.Attempt(EXP_B, "A", "pepe-lock", 0, "codex")
    assert a.key == f"{EXP_B}/A/claude.pepe-lock/0" and a.session_id != b.session_id
    plain = AL.Attempt(EXP_A, "A", "fix-add", 0)
    assert plain.session_id == refs.session_uuid(refs.run_key(EXP_A, "A", "fix-add", 0))   # Langfuse ids unchanged
    assert R.session_id_for(EXP_B, "A", "pepe-lock", 0, "claude") == a.session_id
    assert R.session_id_for(EXP_A, "A", "fix-add", 0) == plain.session_id
    for bad in (("J", "fix-add", 0, None), ("A", "fix-add", -1, None), ("A", "Fix_Add", 0, None), ("A", "x", 0, "Bad")):
        with pytest.raises(refs.RefError):
            AL.Attempt(EXP_A, *bad)


# --------------------------------------------------------------------------- classification


def test_eval_harness_sessions_are_not_counted_as_attempts(world: SimpleNamespace) -> None:
    link = attempt_link(world.url)
    # eligible: the two sandbox sessions only; the four eval sessions are excluded and counted
    assert (link["eligible"], link["linked"], link["unlinked"], link["unresolvable"]) == (2, 0, 2, 0)
    assert (link["excluded_eval_harness"], link["excluded_eval_other"]) == (3, 1)
    out = amem(world, "coverage", "links", "--classify")
    assert out["old_eligible"] == {"total": 6, "eval_harness_session": 3, "ambiguous": 1, "sandbox_attempt": 1,
                                   "unresolved": 1}
    assert out["classes"]["eval_harness_session"] == 3 and out["classes"]["ambiguous"] == 1
    assert [a["session_id"] for a in out["ambiguous"]] == ["probe-1"]


def test_sandbox_logs_with_and_without_sidecars_classify_correctly(world: SimpleNamespace) -> None:
    before = tables(world.url)
    out = amem(world, "coverage", "links", "--classify")
    assert tables(world.url) == before                                    # read-only
    assert out["classes"] == {"eval_harness_session": 3, "sandbox_attempt": 6, "experiment_without_attempts": 1,
                              "ambiguous": 1, "unresolved": 4}
    assert out["attempts_per_experiment"] == {EXP_A: 2, EXP_B: 4}
    assert out["experiments_without_attempts"] == [EXP_E]
    assert out["sandbox_attempt_sources"] == {"on_disk": 6, "langfuse": 1, "supabase": 1,
                                              "linked_in_rsi_attempts": 0, "not_in_rsi_attempts": 6}
    assert out["excluded_runs"] == {"check run": 6, "judge run": 1}
    assert out["divergences"] == {"session_id_collision": 4, "langfuse_default_case": 1, "experiment_row_missing": 0}
    reasons = {u["id"]: u["reason"] for u in out["unresolved"]}
    assert "without a sidecar" in reasons[f"unresolved:run:{EXP_A}/{rid(EXP_A, 'docs-typo', 'A', 0)}"]
    assert "do not match the run id" in reasons[f"unresolved:run:{EXP_D}/{rid(EXP_D, 'fix-add', 'A', 0)}"]
    text = run_amem("coverage", "links", "--classify", env=world.env)
    assert text.returncode == 0 and "sandbox_attempt=6" in text.stdout and "unresolved=4" in text.stdout


def test_an_attempt_whose_only_clue_is_a_nearby_timestamp_stays_unresolved(world: SimpleNamespace) -> None:
    out = amem(world, "coverage", "links", "--classify")
    reasons = {u["id"]: u["reason"] for u in out["unresolved"]}
    assert "not explicit" in reasons[f"unresolved:run:{EXP_C}/{OLD_RUN}"]          # a log named by its start time
    assert "without experiment id or attempt identifier" in reasons["unresolved:session:sbx-near"]
    amem(world, "coverage", "repair-links", "--apply")
    rows = attempts(world.url)
    for key in (f"unresolved:run:{EXP_C}/{OLD_RUN}", "unresolved:session:sbx-near"):
        assert rows[key]["status"] == "unresolved" and rows[key]["reason"] and rows[key]["attempt_session_id"] is None
    linked = [r for r in rows.values() if r["status"] == "linked"]
    assert all(r["session_id"] != "sbx-near" for r in linked)
    assert {r["run_id"] for r in linked}.isdisjoint({OLD_RUN})
    # the session whose id is the attempt's own id is linked by that id
    a = rows[f"{EXP_A}/A/fix-add/0"]
    assert a["session_id"] == canonical(EXP_A, "A", "fix-add", 0) == a["attempt_session_id"]


def test_a_session_naming_only_its_experiment_and_an_unreadable_record_stay_unresolved(world: SimpleNamespace) -> None:
    """Two more real shapes: ingest sets sessions.experiment_id from a cwd under experiments/<id> (no arm, case or
    rep), and a run record cut short by an interrupted night."""
    with psycopg.connect(world.url) as c:
        c.execute("insert into sessions (session_id, kind, project_dir, env, experiment_id) values "
                  "('sbx-cwd', 'headless', '-Users-v-sandbox-experiments', 'sandbox', %s)", (EXP_B,))
    cut = world.root / "replays" / EXP_B / "claude" / "runs" / f"{rid(EXP_B, 'pepe-lock', 'A', 1, 'claude')}.json"
    cut.write_text('{"run_id": "exp-')
    out = amem(world, "coverage", "links", "--classify")
    reasons = {u["id"]: u["reason"] for u in out["unresolved"]}
    assert reasons["unresolved:session:sbx-cwd"].startswith(f"session names experiment {EXP_B} but no attempt")
    assert reasons[f"unresolved:run:{EXP_B}/{cut.stem}"] == "unreadable run record"
    assert out["classes"]["sandbox_attempt"] == 6 and out["classes"]["unresolved"] == 6
    amem(world, "coverage", "repair-links", "--apply")
    row = attempts(world.url)["unresolved:session:sbx-cwd"]
    assert (row["status"], row["experiment_id"], row["session_id"]) == ("unresolved", EXP_B, "sbx-cwd")


# --------------------------------------------------------------------------- repair


def test_repair_dry_run_writes_nothing(world: SimpleNamespace) -> None:
    before = tables(world.url)
    out = amem(world, "coverage", "repair-links")
    assert out["mode"] == "dry-run" and out["manifest"] is None
    assert out["planned"] == {"insert": 10, "update": 0, "delete": 0, "unchanged": 0, "kept_runner": 0, "linked": 6,
                              "unresolved": 4}
    assert tables(world.url) == before and not world.manifests.exists()


def test_apply_then_undo_restores_rows_exactly_and_apply_twice_changes_nothing(world: SimpleNamespace) -> None:
    b_key, stale = f"{EXP_A}/B/fix-add/0", f"unresolved:run:{EXP_A}/{rid(EXP_A, 'fix-add', 'A', 0)}"
    with psycopg.connect(world.url) as c:
        # an earlier repair's row (updated now), an unresolved row this repair resolves (deleted), a runner row (kept)
        c.execute("insert into rsi_attempts (id, status, experiment_id, arm, case_id, rep, attempt_session_id, run_id,"
                  " source, evidence, created_at, updated_at) values (%s, 'linked', %s, 'B', 'fix-add', 0, %s, %s,"
                  " 'repair', '{\"sandbox_log\": \"old\"}', '2026-10-20T01:00:00Z', '2026-10-20T01:00:00Z')",
                  (b_key, EXP_A, canonical(EXP_A, "B", "fix-add", 0), rid(EXP_A, "fix-add", "B", 0)))
        c.execute("insert into rsi_attempts (id, status, experiment_id, run_id, source, reason, created_at, updated_at)"
                  " values (%s, 'unresolved', %s, %s, 'repair', 'no sidecar', '2026-10-20T01:00:00Z',"
                  " '2026-10-20T01:00:00Z')", (stale, EXP_A, rid(EXP_A, "fix-add", "A", 0)))
        AL.record_runner(c, [{"experiment_id": EXP_B, "profile": "claude", "arm": "A", "case": "pepe-lock", "rep": 0,
                              "run_id": rid(EXP_B, "pepe-lock", "A", 0, "claude"), "replay_record": "x"}])
    before_db, before_rows = tables(world.url), attempts(world.url)
    link_before = attempt_link(world.url)

    first = amem(world, "coverage", "repair-links", "--apply")
    assert first["planned"] == {"insert": 8, "update": 1, "delete": 1, "unchanged": 0, "kept_runner": 1, "linked": 6,
                                "unresolved": 4}
    manifest = Path(first["manifest"])
    assert manifest.parent == world.manifests and oct(manifest.stat().st_mode & 0o777) == "0o600"
    after_db, after_rows = tables(world.url), attempts(world.url)
    assert after_rows[b_key]["evidence"]["sandbox_log"].endswith(f"{rid(EXP_A, 'fix-add', 'B', 0)}.json")
    assert after_rows[b_key]["evidence"]["langfuse"].startswith("lf:trace/") and stale not in after_rows
    assert after_rows[f"{EXP_B}/A/claude.pepe-lock/0"]["source"] == "runner"
    link = attempt_link(world.url)
    # 6 linked attempts + 4 unresolved rows; the attempt session is covered by its row now
    assert (link["eligible"], link["linked"], link["unlinked"], link["unresolvable"]) == (10, 6, 4, 0)
    assert (link_before["eligible"], link_before["linked"]) == (5, 2)     # 3 rows + 2 uncovered sandbox sessions

    second = amem(world, "coverage", "repair-links", "--apply")                       # replay: no change
    assert second["manifest"] is None and second["planned"]["insert"] == second["planned"]["update"] == 0
    assert second["planned"]["delete"] == 0 and second["planned"]["unchanged"] == 9
    assert tables(world.url) == after_db and len(list(world.manifests.iterdir())) == 1
    again = amem(world, "coverage", "links", "--classify")
    assert again["classes"]["sandbox_attempt"] == 6 and again["sandbox_attempt_sources"]["linked_in_rsi_attempts"] == 6

    undone = amem(world, "coverage", "repair-links", "--undo", str(manifest))
    assert undone == {"mode": "undo", "restored": 10, "status": "restored"}
    assert tables(world.url) == before_db and attempts(world.url) == before_rows       # timestamps included
    assert amem(world, "coverage", "repair-links", "--undo", str(manifest))["status"] == "already undone"


def test_undo_refuses_when_a_row_changed_after_the_repair(world: SimpleNamespace) -> None:
    manifest = amem(world, "coverage", "repair-links", "--apply")["manifest"]
    with psycopg.connect(world.url) as c:
        c.execute("update rsi_attempts set evidence = evidence || '{\"note\": 1}' where id = %s", (f"{EXP_A}/A/fix-add/0",))
    changed = tables(world.url)
    out = run_amem("coverage", "repair-links", "--undo", manifest, env=world.env)
    assert out.returncode == 1 and "changed since the repair" in out.stderr
    assert tables(world.url) == changed


def test_a_hosted_database_needs_an_explicit_flag(world: SimpleNamespace) -> None:
    env = {**world.env, "AMEM_DB_URL": "postgresql://amem_app:x@db.example.invalid:5432/postgres"}
    out = run_amem("coverage", "repair-links", "--apply", env=env)
    assert out.returncode == 2 and "not a local database" in out.stderr and "x@" not in out.stderr
    assert not world.manifests.exists()


# --------------------------------------------------------------------------- at creation (the runner path)


def test_replay_records_the_attempt_identity_in_its_run_records_and_sidecars(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    exp = "exp-20261026-ffff6666"
    root, bid, h = setup_root(tmp_path, monkeypatch, "closing-line", experiment=exp)
    real = R.run_replay
    monkeypatch.setattr(R, "run_replay", lambda *a, **kw: real(*a, **kw, sandbox=HostSandbox(root)))
    budget = FakeBudget()
    budget.cfg = SimpleNamespace(max_ab_pairs=10)                   # type: ignore[attr-defined]
    rr = N.SandboxReplayer({"replay_agent": "stub", "judge": "stub", "replay_jev": "fixture"},
                           {"cases": ["fix-add"], "reps": 2}).replay(artifact_hash=h, experiment_id=exp,
                                                                    baseline_id=bid, budget=budget)
    assert rr.verdict in N.VERDICTS and rr.runs == 4
    assert sorted((a["arm"], a["rep"]) for a in rr.attempts) == [("A", 0), ("A", 1), ("B", 0), ("B", 1)]
    for a in rr.attempts:
        sid = canonical(exp, a["arm"], "fix-add", a["rep"])
        rec = json.loads((root / a["replay_record"]).read_text())
        side = json.loads((root / "experiments" / exp / "logs" / f"{a['run_id']}.rsi.json").read_text())
        assert rec["session_id"] == side["attempt_session_id"] == sid
        assert (side["experiment_id"], side["arm"], side["case"], side["profile"]) == (exp, a["arm"], "fix-add", None)


def test_an_attempt_created_through_the_night_is_linked_at_creation(url: str, tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    night = "2026-10-26"
    exp = f"exp-{night.replace('-', '')}-{N._sha16(KEY_A)[:8]}"         # the id the night gives KEY_A's experiment
    root, bid, h = setup_root(tmp_path, monkeypatch, "closing-line", experiment=exp)
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    strip_agent_env(monkeypatch)
    make_home(tmp_path, monkeypatch)
    clean_db(url)
    with psycopg.connect(url) as c:
        c.execute("delete from rsi_attempts")
    seed_rule(url, RULE_A, "a")
    real = R.run_replay

    def replay_synthetic(_h: str, *a: Any, **kw: Any) -> Any:
        # the night's own artifact targets the temp HOME; the replay runs the synthetic artifact staged for `exp`
        return real(h, *a, **{**kw, "baseline_id": bid, "sandbox": HostSandbox(root)})

    monkeypatch.setattr(R, "run_replay", replay_synthetic)
    replayer = N.SandboxReplayer({"replay_agent": "stub", "judge": "stub", "replay_jev": "fixture"},
                                 {"cases": ["fix-add"], "reps": 1})
    rep = N.run_night(night=night, db_url=url, adapters=adapters(replayer=replayer))
    assert [e["experiment_id"] for e in rep["experiments"] if e["experiment_id"]] == [exp], rep["notes"]
    rows = attempts(url)
    assert set(rows) == {f"{exp}/A/fix-add/0", f"{exp}/B/fix-add/0"}
    for key, r in rows.items():
        assert (r["status"], r["source"], r["experiment_id"]) == ("linked", "runner", exp)
        assert r["attempt_session_id"] == canonical(exp, r["arm"], "fix-add", 0)
        assert (root / r["evidence"]["replay_record"]).is_file()
    link = attempt_link(url)
    assert link["linked"] == 2 and link["unresolvable"] == 0
    # the repair sees them as written at creation and changes nothing
    with psycopg.connect(url, autocommit=True) as conn:
        plan = AL.repair(conn, root)["planned"]
    assert plan["kept_runner"] == 2 and plan["insert"] == plan["update"] == plan["delete"] == 0
    assert not any("attempt identities not recorded" in n for n in rep["notes"])


# --------------------------------------------------------------------------- access


def identity(c: psycopg.Connection, role: str, uid: str | None = None) -> None:
    c.execute("reset role")
    c.execute("select set_config('request.jwt.claim.sub', %s, true)", (uid or "",))
    c.execute(f"set local role {role}")


def test_rls_is_on_and_only_the_owner_roles_get_access(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("delete from rsi_attempts")
        c.execute(f"alter role amem_app with login password '{APP_PW}'")
    with psycopg.connect(url) as c:
        AL.record_runner(c, [{"experiment_id": EXP_A, "arm": "A", "case": "fix-add", "rep": 0, "run_id": "r"}])
        c.execute("insert into rsi_attempts (id, status, source, reason) values ('unresolved:x', 'unresolved', "
                  "'repair', 'why')")
        assert c.execute("select relrowsecurity from pg_class where relname = 'rsi_attempts'").fetchone() == (True,)
        for role in ("anon", "authenticated", "service_role", "stranger"):
            c.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname='{role}') then "
                      f"create role {role}; end if; end $$")
        c.execute("create schema if not exists auth")
        c.execute("create or replace function auth.uid() returns uuid language sql stable as "
                  "$$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$")
        c.execute("grant usage on schema auth, public to anon, authenticated, service_role, stranger, rsi_reader")
        c.execute("grant execute on function public.amem_owner_uid() to authenticated")
        c.execute("insert into amem_private.dashboard_owner values (true, %s) "
                  "on conflict (singleton) do update set uid = excluded.uid", (OWNER,))
        c.execute(MIGRATION.read_text())                                     # re-run: now the auth branch applies
        for role in ("anon", "service_role", "stranger", "rsi_reader"):
            identity(c, role)
            c.execute("savepoint s")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("select * from rsi_attempts")
            c.execute("rollback to savepoint s")
        identity(c, "authenticated", STRANGER)
        assert c.execute("select count(*) from rsi_attempts").fetchone() == (0,)
        identity(c, "authenticated", OWNER)
        assert c.execute("select count(*) from rsi_attempts").fetchone() == (2,)
        c.execute("savepoint s")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("delete from rsi_attempts")
        c.execute("rollback to savepoint s")
        identity(c, "amem_app")
        assert c.execute("select count(*) from rsi_attempts").fetchone() == (2,)
        c.execute("delete from rsi_attempts")                                 # only the repair row is deletable
        assert c.execute("select source from rsi_attempts").fetchall() == [("runner",)]
        c.execute("reset role")
        c.rollback()


def test_the_doc_and_registry_name_the_table() -> None:
    from agent_memory import data_families as DF
    assert DF.assignments()["rsi_attempts"] == ["experiments"] and DF.check() == []
    doc = (Path(__file__).resolve().parents[1] / "docs" / "data-use" / "relationships.md").read_text()
    for word in ("rsi_attempts", "repair-links", "--classify", "eval_harness_session", "unresolved", "--undo"):
        assert word in doc, word
