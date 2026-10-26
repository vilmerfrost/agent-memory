"""Production outcome evidence uses a disposable DB; no model, files or promotion calls."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import production_feedback as F, rsi_evidence_pack as P, rsi_outcome as O
from agent_memory.integrations import evidence as E
from test_rsi_approval import rdb, sandbox  # noqa: F401
from test_rsi_outcome import promoted

NOW = datetime(2026, 10, 26, 8, 20, tzinfo=UTC)
SOURCE, RULE, TARGET = "skill:pepe", "lock", "~/.agents/skills/pepe/SKILL.md"
BEFORE, AFTER = hashlib.sha256(b"before").hexdigest(), hashlib.sha256(b"after").hexdigest()
CID = "0f81e263-a3b0-4b1d-b3b9-a1bc027eaac5"


def outcome(verdict: str = "worse") -> O.Outcome:
    change = O.Change("edit", CID, SOURCE, RULE, NOW-timedelta(days=10), "Lock", TARGET, BEFORE, AFTER)
    return O.Outcome(change, 10, sessions_before=10, sessions_after=10,
                     deviations_before=1, deviations_after=8, rate_before=.1, rate_after=.8,
                     p_value=.02, verdict=verdict, observed_days=10)


def complete(o: O.Outcome) -> dict[str, Any]:
    return {"status": "complete", "gaps": [], "scope": "stored_rows_only", "cohort": {
        side: {"eligible": getattr(o, f"sessions_{side}"), "scanned": getattr(o, f"sessions_{side}"),
               "version_verified": getattr(o, f"sessions_{side}"),
               "deviations": getattr(o, f"deviations_{side}"),
               "verified_deviations": getattr(o, f"deviations_{side}"), "session_set_sha256": "a"*64}
        for side in ("before", "after")}}


def persisted(o: O.Outcome | None = None) -> dict[str, Any]:
    o = o or outcome()
    return {"id": CID, **F.observation(o, NOW, complete(o))}


@pytest.mark.parametrize("verdict", ["helped", "worse", "no-change"])
def test_valid_observations_are_advisory_and_noncausal(verdict: str) -> None:
    row = persisted(outcome(verdict))
    got = F.assess(row, AFTER, NOW)
    assert got["status"] == "ok"
    assert got["metrics"]["advisory_only"] is True
    assert got["metrics"]["design"] == "observational_before_after"
    assert got["metrics"]["verdict"] == verdict


@pytest.mark.parametrize("mutation,sha,now,reason", [
    ("expired", AFTER, NOW+timedelta(days=8), "stale_observation"),
    ("old_window", AFTER, NOW, "stale_observation"),
    ("none", BEFORE, NOW, "instruction_version_mismatch"),
    ("none", None, NOW, "instruction_version_mismatch"),
    ("incomplete", AFTER, NOW, "coverage_incomplete"),
    ("forged_complete", AFTER, NOW, "coverage_incomplete"),
    ("mixed", AFTER, NOW, "outcome_not_conclusive"),
])
def test_stale_mismatched_and_incomplete_evidence_is_not_support(mutation, sha, now, reason) -> None:
    row = persisted()
    if mutation == "old_window":
        row["metrics"]["window"]["after_end"] = (NOW-timedelta(days=8)).isoformat()
    if mutation == "incomplete":
        row["metrics"]["coverage"]["status"] = "incomplete"
    if mutation == "forged_complete":
        row["metrics"]["coverage"]["cohort"]["after"]["version_verified"] = 0
    if mutation == "mixed":
        row["metrics"]["verdict"] = "mixed"
    got = F.assess(row, sha, now)
    assert got["status"] != "ok" and got["reason"] == reason


def test_generation_is_stable_when_only_collection_time_changes() -> None:
    o = outcome()
    a = F.observation(o, NOW, complete(o))
    b = F.observation(o, NOW+timedelta(hours=1), complete(o))
    assert a["metrics"]["generation"] == b["metrics"]["generation"]
    assert a["external_ref"] != b["external_ref"]  # both actual observations retained


def test_same_observation_in_another_timezone_is_idempotent() -> None:
    from datetime import timezone
    o = outcome()
    a = F.observation(o, NOW, complete(o))
    b = F.observation(o, NOW.astimezone(timezone(timedelta(hours=2))), complete(o))
    assert a["external_ref"] == b["external_ref"]


@pytest.mark.parametrize("key", ["coverage", "window", "intervention", "measurements"])
def test_malformed_observation_is_not_upgraded_or_executed(key: str) -> None:
    row = persisted()
    row["metrics"][key] = ["not", "an", "object"]
    assert F.assess(row, AFTER, NOW)["status"] != "ok"


def candidate(**meta: Any) -> dict[str, Any]:
    return {"rule_key": f"{SOURCE}:{RULE}", "dedupe_key": f"dev:{SOURCE}:{RULE}", "status": "rejected",
            "last_considered_at": NOW-timedelta(days=1), "meta": meta}


def test_terminal_reconsideration_requires_explicit_new_version_or_new_cohort() -> None:
    finding = F.assess(persisted(), AFTER, NOW)
    old = candidate(last_tested_instruction_sha256=BEFORE)
    assert F.reconsider(old, finding, AFTER, now=NOW).reason == "tested_instruction_version_changed"
    assert F.reconsider(old, finding, AFTER, now=NOW).allowed
    unchanged = candidate(last_tested_instruction_sha256=AFTER, production_feedback_sessions_after=10)
    assert not F.reconsider(unchanged, finding, AFTER, now=NOW).allowed
    new_cohort = candidate(last_tested_instruction_sha256=AFTER, production_feedback_sessions_after=5)
    assert F.reconsider(new_cohort, finding, AFTER, now=NOW).reason == "new_verified_production_cohort"
    assert F.reconsider(new_cohort, finding, AFTER, now=NOW).allowed
    unknown_history = candidate()
    assert not F.reconsider(unknown_history, finding, AFTER, now=NOW).allowed
    already = candidate(last_tested_instruction_sha256=BEFORE,
                        production_feedback_generation=finding["metrics"]["generation"])
    assert F.reconsider(already, finding, AFTER, now=NOW).reason == "generation_already_considered"
    assert not F.reconsider(already, finding, AFTER, now=NOW).allowed
    assert not F.reconsider(old, finding, BEFORE, now=NOW).allowed
    assert not F.reconsider(old, finding, AFTER, now=NOW+timedelta(days=8)).allowed
    old["last_considered_at"] = NOW
    assert F.reconsider(old, finding, AFTER, now=NOW).reason == "not_newer_than_candidate_consideration"


def test_positive_observation_does_not_reopen_a_failed_idea() -> None:
    finding = F.assess(persisted(outcome("helped")), AFTER, NOW)
    before = candidate(last_tested_instruction_sha256=BEFORE)
    saved = copy.deepcopy(before)
    result = F.reconsider(before, finding, AFTER, now=NOW)
    assert not result.allowed and result.reason == "positive_observation_is_context_only"
    assert before == saved


def seed(conn: psycopg.Connection, *, authority: str | None = "observed", unscanned: bool = False,
         version: str | None = None) -> O.Outcome:
    o = outcome()
    conn.execute("insert into edits(id,instruction_source,rule_id,status,target_path,instruction_sha_before,"
                 "instruction_sha_after,created_at) values(%s,%s,%s,'kept',%s,%s,%s,%s)",
                 (CID, SOURCE, RULE, TARGET, BEFORE, AFTER, o.change.applied_at))
    for side in ("before", "after"):
        expected = BEFORE if side == "before" else AFTER
        deviations = 1 if side == "before" else 8
        for i in range(10):
            sid = f"feedback-{side}-{i}"
            at = o.change.applied_at+timedelta(days=(-9+i*.8) if side == "before" else (1+i*.8))
            entries = [{"name": SOURCE, "kind": "skill", "path": TARGET, "authority": "observed",
                        "sha256": version or expected}]
            conn.execute("insert into sessions(session_id,kind,project_dir,started_at,instructions,instruction_sources,scanned_at,env)"
                         " values(%s,'chat','/fixture',%s,%s,%s,%s,'prod')",
                         (sid, at, Jsonb([SOURCE]), Jsonb(entries), None if unscanned and i == 9 else at))
            if i < deviations:
                conn.execute("insert into deviations(source_key,session_id,instruction_source,instruction_ref,rule_id,"
                             "what,why,how,followed_grade,result_grade,instruction_sha,instruction_authority) "
                             "values(%s,%s,%s,%s,%s,'w','y','h',0.2,0.5,%s,%s)",
                             (sid, sid, SOURCE, RULE, RULE, expected, authority))
    return o


def test_history_is_append_only_idempotent_and_latest_projection_compatible(rdb: str) -> None:  # noqa:F811
    with psycopg.connect(rdb) as conn:
        o = seed(conn)
        O.store_outcomes(conn, [o], NOW)
        first = conn.execute("select id,metrics from rsi_evidence where kind=%s", (F.KIND,)).fetchone()
        O.store_outcomes(conn, [o], NOW)
        assert conn.execute("select count(*) from rsi_evidence where kind=%s", (F.KIND,)).fetchone() == (1,)
        O.store_outcomes(conn, [o], NOW+timedelta(hours=1))
        assert conn.execute("select count(*) from rsi_evidence where kind=%s", (F.KIND,)).fetchone() == (2,)
        assert conn.execute("select metrics from rsi_evidence where id=%s", (first[0],)).fetchone()[0] == first[1]
        assert conn.execute("select count(*) from rsi_outcomes").fetchone() == (1,)
        assert o.feedback["coverage"]["status"] == "complete"
        report = O.render_markdown(O.Report(NOW, outcomes=[o]))
        assert "observational associations" in report and "coverage complete" in report
        assert o.feedback["evidence_ref"] in report
        found = F.for_candidate(conn, candidate(), AFTER, now=NOW+timedelta(hours=1))
        assert len(found) == 1 and all(f["status"] == "ok" for f in found)
        assert found[0]["observed_at"] == (NOW+timedelta(hours=1)).isoformat()


@pytest.mark.parametrize("authority,unscanned,version,gap", [
    (None, False, None, "deviation_authority_unverified"),
    ("unverified", False, None, "deviation_authority_unverified"),
    ("observed", True, None, "scan_incomplete"),
    ("observed", False, "c"*64, "instruction_version_unverified"),
])
def test_real_cohort_coverage_never_upgrades_missing_authority(rdb: str, authority, unscanned, version, gap) -> None:  # noqa:F811
    with psycopg.connect(rdb) as conn:
        o = seed(conn, authority=authority, unscanned=unscanned, version=version)
        ref = F.store_observation(conn, o, NOW)
        assert ref.startswith("amem:evidence/")
        assert o.feedback["coverage"]["status"] == "incomplete"
        assert any(gap in g for g in o.feedback["coverage"]["gaps"])
        assert F.for_candidate(conn, candidate(), AFTER, now=NOW)[0]["status"] != "ok"


def test_composite_source_is_not_mistaken_for_exact_single_file_exposure(rdb: str) -> None:  # noqa:F811
    with psycopg.connect(rdb) as conn:
        o = seed(conn)
        conn.execute("update sessions set instruction_sources=instruction_sources || %s", (Jsonb([
            {"name": SOURCE, "kind": "skill", "path": "~/another.md", "authority": "observed", "sha256": "b"*64}
        ]),))
        cov = F.coverage(conn, o)
        assert cov["status"] == "incomplete"
        assert "after_instruction_version_unverified" in cov["gaps"]


def test_evidence_pack_reads_validated_outcomes_without_writes(rdb: str) -> None:  # noqa:F811
    with psycopg.connect(rdb) as conn:
        o = seed(conn)
        ref = F.store_observation(conn, o, NOW)
    connect = lambda: psycopg.connect(rdb)
    c = {**candidate(), "target": TARGET, "evidence_refs": [ref]}
    with psycopg.connect(rdb) as conn:
        before = conn.execute("select count(*) from rsi_evidence").fetchone()
    pack = P.build_pack(c, retrievers={"sb": E.SupabaseRetriever(connect)}, connect=connect,
                        current_instruction_sha256=AFTER, now=lambda: NOW)
    pf = [f for f in pack.findings if f.kind == F.KIND]
    assert pf and all(f.status == "ok" and f.measurements["advisory_only"] is True for f in pf)
    assert any(s["step"] == "sb.production_outcomes" for s in pack.steps)
    wrong = P.build_pack(c, retrievers={"sb": E.SupabaseRetriever(connect)}, connect=connect,
                         current_instruction_sha256=BEFORE, now=lambda: NOW)
    assert all(f.status != "ok" for f in wrong.findings if f.ref == ref)
    unbound = P.build_pack(c, retrievers={"sb": E.SupabaseRetriever(connect)}, connect=connect, now=lambda: NOW)
    assert not any(f.status == "ok" for f in unbound.findings if f.ref == ref)
    with psycopg.connect(rdb) as conn:
        assert conn.execute("select count(*) from rsi_evidence").fetchone() == before


def test_delta_does_not_treat_subject_resolution_as_production_validation(rdb: str) -> None:  # noqa:F811
    with psycopg.connect(rdb) as conn:
        o = seed(conn)
        F.store_observation(conn, o, NOW)
        row = conn.execute("select id::text,created_at,system,kind,external_ref,subject_refs,metrics,observed_at,summary "
                           "from rsi_evidence where kind=%s", (F.KIND,)).fetchone()
        finding = P._evidence_finding(row, {f"amem:edit/{CID}"}, 200)
        assert finding.status == "unlinked" and finding.authority is None
        assert "current instruction version" in finding.detail


def test_skill_proposal_uses_persisted_writer_section_not_synthetic_manifest_rule(rdb: str, sandbox) -> None:  # noqa:F811
    exp = "exp-20261016-fedcba09"
    pid = promoted(rdb, sandbox, f"{SOURCE}:{RULE}", NOW-timedelta(days=10), exp, "Skill update")
    with psycopg.connect(rdb) as conn:
        conn.execute("update rsi_candidates set dedupe_key=%s,rule_key=null where id="
                     "(select candidate_id from rsi_proposals where id=%s)", (SOURCE, pid))
        conn.execute("insert into rsi_experiments(id,kind,meta) values(%s,'edit_instruction',%s)",
                     (exp, Jsonb({"implementation": {"section_id": RULE}, "implementer": "writer"})))
        changes, skipped = O.applied_changes(conn)
        got = next(c for c in changes if c.id == pid)
        assert (got.instruction_source, got.rule_id) == (SOURCE, RULE)
        conn.execute("update rsi_experiments set meta='{}' where id=%s", (exp,))
        changes, skipped = O.applied_changes(conn)
        assert not any(c.id == pid for c in changes)
        assert any(pid in why and "no rule to measure" in why for why in skipped)


def test_observation_storage_and_local_cost_measurement(rdb: str) -> None:  # noqa:F811
    with psycopg.connect(rdb) as conn:
        o = seed(conn)
        before = conn.execute("select pg_total_relation_size('rsi_evidence')").fetchone()[0]
        started = time.perf_counter()
        F.store_observation(conn, o, NOW)
        elapsed_ms = (time.perf_counter()-started)*1000
        payload_bytes, column_bytes = conn.execute("select octet_length(metrics::text),pg_column_size(metrics) "
                                                  "from rsi_evidence where kind=%s", (F.KIND,)).fetchone()
        allocated = conn.execute("select pg_total_relation_size('rsi_evidence')").fetchone()[0]-before
        assert payload_bytes < 10_000 and column_bytes > 0
        print(json.dumps({"local_fixture_sessions": 20, "observation_write_ms": round(elapsed_ms, 3),
                          "metrics_json_bytes": payload_bytes, "metrics_column_bytes": column_bytes,
                          "allocated_relation_delta_bytes": allocated, "model_calls": 0,
                          "limitation": "one disposable DB sample; allocation includes page/index granularity"}))
