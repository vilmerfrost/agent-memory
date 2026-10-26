"""Coverage snapshots, routine consumption and the "all clear" guard (docs/data-use/coverage.md).

A throwaway Postgres (conftest pg_url). The routine role amem_app records and reads; the API roles, a
signed-in non-owner and unrelated roles get nothing; the same input twice leaves one row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from agent_memory import config, coverage as CV, store

APP_PW = "app-test-password-0123456789"
MIGRATION = config.MIGRATIONS_DIR / "20261026150000_data_use_coverage.sql"
OWNER = "00000000-0000-4000-8000-0000000000aa"
STRANGER = "00000000-0000-4000-8000-0000000000bb"
T0 = datetime(2026, 10, 25, tzinfo=UTC)
T1 = T0 + timedelta(days=1)


def as_app(pg_url: str) -> str:
    return pg_url.replace("postgres:postgres@", f"amem_app:{APP_PW}@", 1)


@pytest.fixture
def db(pg_url: str) -> dict[str, str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{APP_PW}'")
        conn.execute("delete from coverage_snapshots")
        conn.execute("delete from routine_consumption")
    return {"owner": pg_url, "app": as_app(pg_url)}


def snap(dataset: str = "sessions", **kw: object) -> CV.Snapshot:
    base: dict[str, object] = dict(dataset=dataset, window_start=T0, window_end=T1,
                                   denominator="prod sessions modified in the window", eligible=10, collected=10,
                                   linked=10, unlinked=0, measured_fields=("tokens_input", "duration_seconds"))
    base.update(kw)
    return CV.Snapshot(**base)  # type: ignore[arg-type]


def use(dataset: str = "sessions", **kw: object) -> CV.Consumption:
    base: dict[str, object] = dict(job="amem-rsi-night", run_key="2026-10-25", dataset=dataset,
                                   analysis_version="night-v3", window_start=T0, window_end=T1,
                                   source_checkpoint="sessions.updated<=2026-10-26T00:00Z", queried=10,
                                   considered=8, used=8, skipped={"env_sandbox": 2}, findings=0)
    base.update(kw)
    return CV.Consumption(**base)  # type: ignore[arg-type]


def count(url: str, table: str) -> int:
    with psycopg.connect(url) as conn:
        return conn.execute(f"select count(*) from {table}").fetchone()[0]


# --------------------------------------------------------------------------- round trip and replay


def test_snapshot_and_consumption_round_trip_as_the_routine_role(db: dict[str, str]) -> None:
    run_id = store.insert_run("amem-rsi-night", "pass", db_url=db["app"])
    with psycopg.connect(db["app"]) as conn:
        assert CV.record_snapshot(conn, snap(pending=1, rejected=2, expired=3, last_collected_at=T1,
                                             source_versions={"sessions_ingest": "v7"},
                                             checkpoints={"sync:clickhouse": "2026-10-25T23:00Z|x"},
                                             run_id=run_id)) == "inserted"
        refs = ["amem:session/abc-1", "amem:deviation/00000000-0000-4000-8000-000000000001", "lf:trace/" + "a" * 32]
        assert CV.record_consumption(conn, use(findings=2, evidence_refs=refs, run_id=run_id)) == "inserted"
        s = CV.snapshots(conn, "sessions")[0]
        c = CV.consumption(conn, job="amem-rsi-night", run_key="2026-10-25")[0]
    assert (s["status"], s["eligible"], s["collected"], s["linked"], s["unlinked"]) == ("complete", 10, 10, 10, 0)
    assert (s["pending"], s["rejected"], s["expired"], s["denominator"]) == (1, 2, 3, "prod sessions modified in the window")
    assert s["measured_fields"] == ["tokens_input", "duration_seconds"] and s["unmeasured_fields"] == []
    assert s["source_versions"] == {"sessions_ingest": "v7"} and s["run_id"] == run_id and s["last_collected_at"] == T1
    assert (c["status"], c["queried"], c["considered"], c["used"], c["findings"]) == ("findings", 10, 8, 8, 2)
    assert c["skipped"] == {"env_sandbox": 2} and c["evidence_refs"] == refs and c["run_id"] == run_id
    assert c["analysis_version"] == "night-v3" and c["source_checkpoint"].startswith("sessions.updated")


def test_replay_is_idempotent_and_a_change_updates_in_place(db: dict[str, str]) -> None:
    with psycopg.connect(db["app"]) as conn:
        assert CV.record_snapshot(conn, snap()) == "inserted"
        assert CV.record_snapshot(conn, snap()) == "unchanged"
        assert CV.record_consumption(conn, use()) == "inserted"
        assert CV.record_consumption(conn, use()) == "unchanged"
        assert CV.record_snapshot(conn, snap(collected=9, linked=9)) == "updated"      # a later measurement
        assert CV.record_consumption(conn, use(used=7)) == "updated"
    assert count(db["owner"], "coverage_snapshots") == 1 and count(db["owner"], "routine_consumption") == 1
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute("select status, collected from coverage_snapshots").fetchone() == ("not_collected", 9)
        assert conn.execute("select used from routine_consumption").fetchone() == (7,)


def test_each_of_the_eight_distinctions_is_recorded_and_read_back(db: dict[str, str]) -> None:
    snaps = {"no_eligible_data": snap("d.none", eligible=0, collected=0, linked=0),
             "not_collected": snap("d.missing", collected=6, linked=6),
             "collected_unlinked": snap("d.unlinked", linked=7, unlinked=3),
             "measurement_unavailable": snap("d.unmeasured", unmeasured_fields=("api_seconds",))}
    uses = {"read_excluded": use("d.excluded", considered=0, used=0, skipped={"env_sandbox": 10}),
            "used_no_finding": use("d.clean"),
            "analysis_not_run": use("d.notrun", queried=0, considered=0, used=0, skipped={}),
            "analysis_failed": use("d.failed", used=0, considered=0, status="analysis_failed",
                                   detail="ClickHouse query timed out")}
    with psycopg.connect(db["app"]) as conn:
        for want, s in snaps.items():
            assert s.resolved_status() == want                           # derived from the counts
            CV.record_snapshot(conn, s)
        for want, u in uses.items():
            assert u.resolved_status() == want
            CV.record_consumption(conn, u)
        got = {r["status"] for r in CV.snapshots(conn)} | {r["status"] for r in CV.consumption(conn)}
    assert got == set(CV.DISTINCTIONS) and len(CV.DISTINCTIONS) == 8
    assert CV.Snapshot(dataset="x", window_start=T0, window_end=T1, denominator="d", eligible=None).resolved_status() \
        == "measurement_unavailable"


def test_the_database_refuses_inconsistent_or_unbounded_rows(db: dict[str, str]) -> None:
    bad = [
        "insert into coverage_snapshots (dataset, window_start, window_end, denominator, eligible, collected, unlinked,"
        " status) values ('x', now(), now(), 'd', 5, 5, 2, 'complete')",                      # unlinked but complete
        "insert into coverage_snapshots (dataset, window_start, window_end, denominator, eligible, status)"
        " values ('x', now(), now(), 'd', null, 'no_eligible_data')",                         # unknown, not empty
        "insert into coverage_snapshots (dataset, window_start, window_end, denominator, eligible, status)"
        " values ('x', now(), now(), 'd', 3, 'all good')",                                    # free text
        "insert into coverage_snapshots (dataset, window_start, window_end, denominator, eligible, collected, status)"
        " values ('x', now(), now() - interval '1 day', 'd', 0, 0, 'no_eligible_data')",       # window backwards
        "insert into routine_consumption (job, run_key, dataset, analysis_version, queried, considered, used, findings,"
        " status) values ('j', 'r', 'x', 'v', 5, 5, 5, 0, 'findings')",                       # findings without one
        "insert into routine_consumption (job, run_key, dataset, analysis_version, queried, considered, used, status)"
        " values ('j', 'r', 'x', 'v', 1, 2, 0, 'read_excluded')",                             # considered > queried
        "insert into routine_consumption (job, run_key, dataset, analysis_version, queried, considered, used, status,"
        " evidence_refs) values ('j', 'r', 'x', 'v', 1, 1, 1, 'used_no_finding',"
        " (select array_agg('amem:run/' || g) from generate_series(1, 21) g))",                 # 21 evidence refs
    ]
    for sql in bad:
        with psycopg.connect(db["app"]) as conn, pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(sql)
    with pytest.raises(ValueError, match="at most 20 evidence refs"):
        use(evidence_refs=[f"amem:run/{i}" for i in range(21)]).validate()
    with pytest.raises(ValueError, match="not an evidence ref"):
        use(evidence_refs=["see the night report"]).validate()
    with pytest.raises(ValueError, match="explicit denominator"):
        snap(denominator=" ").validate()
    with pytest.raises(ValueError, match="used <= considered <= queried"):
        use(used=9).validate()


# --------------------------------------------------------------------------- the "all clear" guard


def record(url: str, snaps: list[CV.Snapshot], uses: list[CV.Consumption]) -> None:
    with psycopg.connect(url) as conn:
        for s in snaps:
            CV.record_snapshot(conn, s)
        for u in uses:
            CV.record_consumption(conn, u)


def guard(url: str, *required: str | CV.Requirement) -> CV.Verdict:
    with psycopg.connect(url) as conn:
        return CV.verdict(conn, job="amem-rsi-night", run_key="2026-10-25", required=list(required))


def test_all_clear_only_with_complete_coverage_and_a_clean_analysis(db: dict[str, str]) -> None:
    record(db["app"], [snap("sessions"), snap("deviations", window_start=T0 - timedelta(days=7))],
           [use("sessions"), use("deviations")])
    v = guard(db["app"], "sessions", "deviations")
    assert v.all_clear and v.conclusion == "all_clear" and v.gaps == ()


def test_all_clear_is_refused_when_a_required_dataset_is_missing(db: dict[str, str]) -> None:
    record(db["app"], [snap("sessions")], [use("sessions")])
    v = guard(db["app"], "sessions", "test_timing_cases")
    assert not v.all_clear and v.conclusion == "incomplete"
    assert {(g.dataset, g.distinction) for g in v.gaps} == {("test_timing_cases", "not_collected"),
                                                            ("test_timing_cases", "analysis_not_run")}


@pytest.mark.parametrize("status,kw", [
    ("analysis_failed", dict(used=0, considered=0, status="analysis_failed", detail="query timed out")),
    ("analysis_not_run", dict(queried=0, considered=0, used=0, skipped={})),
    ("read_excluded", dict(considered=0, used=0, skipped={"env_sandbox": 10})),
])
def test_all_clear_is_refused_when_the_analysis_did_not_produce_evidence(db: dict[str, str], status: str,
                                                                          kw: dict[str, object]) -> None:
    record(db["app"], [snap("sessions"), snap("deviations")], [use("sessions"), use("deviations", **kw)])
    v = guard(db["app"], "sessions", "deviations")
    assert v.conclusion == "incomplete" and [(g.dataset, g.distinction) for g in v.gaps] == [("deviations", status)]


def test_coverage_gaps_block_all_clear_and_findings_are_never_all_clear(db: dict[str, str]) -> None:
    record(db["app"], [snap("sessions", linked=8, unlinked=2), snap("execution_spans", eligible=None, collected=None,
                                                                     linked=None, unlinked=None)],
           [use("sessions"), use("execution_spans", findings=1)])
    v = guard(db["app"], "sessions", "execution_spans")
    assert v.conclusion == "findings" and v.findings == 1                   # findings stand, the gaps stay listed
    assert {(g.dataset, g.distinction) for g in v.gaps} == {("sessions", "collected_unlinked"),
                                                            ("execution_spans", "measurement_unavailable")}


def test_a_snapshot_must_cover_the_input_window(db: dict[str, str]) -> None:
    record(db["app"], [snap("sessions", window_start=T0 + timedelta(hours=12))], [use("sessions")])
    v = guard(db["app"], "sessions")
    assert [(g.distinction, g.detail) for g in v.gaps] == [("not_collected", "no coverage snapshot covers the input window")]


def test_no_eligible_data_blocks_unless_the_requirement_allows_it(db: dict[str, str]) -> None:
    record(db["app"], [snap("handoffs", eligible=0, collected=0, linked=0)],
           [use("handoffs", queried=0, considered=0, used=0, skipped={})])
    assert [g.distinction for g in guard(db["app"], "handoffs").gaps] == ["no_eligible_data"]
    assert guard(db["app"], CV.Requirement("handoffs", allow_no_eligible=True)).all_clear


def test_the_guard_needs_a_required_dataset() -> None:
    with pytest.raises(ValueError, match="at least one required dataset"):
        CV.evaluate([], {}, {})


# --------------------------------------------------------------------------- access


def identity(c: psycopg.Connection, role: str, uid: str | None = None) -> None:
    c.execute("reset role")
    c.execute("select set_config('request.jwt.claim.sub', %s, true)", (uid or "",))
    c.execute(f"set local role {role}")


def test_rls_is_on_and_only_the_owner_roles_get_access(db: dict[str, str]) -> None:
    record(db["owner"], [snap()], [use()])
    with psycopg.connect(db["owner"]) as c:
        rls = dict(c.execute("select relname, relrowsecurity from pg_class where relname in "
                             "('coverage_snapshots', 'routine_consumption')").fetchall())
        assert rls == {"coverage_snapshots": True, "routine_consumption": True}
        # Supabase-like roles and an owner, inside this transaction only (rolled back below).
        for role in ("anon", "authenticated", "service_role", "stranger"):
            c.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname='{role}') then "
                      f"create role {role}; end if; end $$")
        c.execute("create schema if not exists auth")
        c.execute("create or replace function auth.uid() returns uuid language sql stable as "
                  "$$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$")
        c.execute("grant usage on schema auth, public to anon, authenticated, service_role, stranger, rsi_reader")
        c.execute("grant execute on function public.amem_owner_uid() to authenticated")   # as Night Desk's migration does on Supabase
        c.execute("insert into amem_private.dashboard_owner values (true, %s) "
                  "on conflict (singleton) do update set uid = excluded.uid", (OWNER,))
        c.execute(MIGRATION.read_text())                                     # re-run: now the auth branch applies
        for table in ("coverage_snapshots", "routine_consumption"):
            for role in ("anon", "service_role", "stranger", "rsi_reader"):
                identity(c, role)
                c.execute("savepoint s")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(f"select * from {table}")
                c.execute("rollback to savepoint s")
            identity(c, "authenticated", STRANGER)                            # signed in, not the owner
            assert c.execute(f"select count(*) from {table}").fetchone() == (0,)
            identity(c, "authenticated", OWNER)
            assert c.execute(f"select count(*) from {table}").fetchone() == (1,)
            c.execute("savepoint s")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"delete from {table}")                              # the owner's browser reads only
            c.execute("rollback to savepoint s")
            identity(c, "amem_app")
            assert c.execute(f"select count(*) from {table}").fetchone() == (1,)
            c.execute("savepoint s")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"delete from {table}")                              # the routine role cannot delete
            c.execute("rollback to savepoint s")
        c.execute("reset role")
        c.rollback()


def test_the_migration_reruns_cleanly(pg_url: str) -> None:
    with psycopg.connect(pg_url) as c:
        c.execute(MIGRATION.read_text())
        c.execute(MIGRATION.read_text())
        assert c.execute("select count(*) from pg_policies where tablename = 'coverage_snapshots'").fetchone()[0] >= 3
        c.rollback()


def test_the_doc_names_every_status() -> None:
    doc = (Path(__file__).resolve().parents[1] / "docs" / "data-use" / "coverage.md").read_text()
    for s in (*CV.SNAPSHOT_STATUSES, *CV.CONSUMPTION_STATUSES):
        assert f"`{s}`" in doc, s
