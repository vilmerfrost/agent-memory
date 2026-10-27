"""Saved historical provider records omitted profile/native ID/start clock.

Shapes reproduce the three real 2026-10-25 provider replay directories, without
copying model output or connecting to the hosted store.
"""
from __future__ import annotations

import json

import psycopg
import pytest

from agent_memory import attempt_links as AL, replay_evidence as RE
from synth_support import fresh_db


@pytest.fixture(scope="module")
def db(pg_url):
    with fresh_db(pg_url) as url:
        yield url


def write_run(root, exp, directory_profile, *, arm="A", rep=0, **changes):
    profile = directory_profile
    directory = root / "replays" / exp
    if profile:
        directory /= profile
    (directory / "runs").mkdir(parents=True, exist_ok=True)
    run_id = AL._run_id_for(exp + ("-" + profile if profile else ""), "anatomy-plan", arm, rep)
    row = {"run_id": run_id, "session_id": AL.session_id_for(exp, arm, "anatomy-plan", rep),
           "agent": profile or "claude", "arm": arm, "case": "anatomy-plan", "rep": rep,
           "duration_s": 42.525, "model": "requested-model", "error": None,
           "runtime": {"transport": "official-subscription-client"}, **changes}
    path = directory / "runs" / (run_id + ".json")
    path.write_text(json.dumps(row))
    (directory / "replay.json").write_text(json.dumps({"experiment_id": exp,
        "config": {"profile": profile, "agent": profile or "claude"}}))
    return path, row


def experiment(conn, exp):
    conn.execute("insert into rsi_experiments(id,kind,status) values(%s,'fixture','evaluated')", (exp,))


def test_historical_missing_profiles_recover_exactly_without_native_identity(db, tmp_path):
    exp = "exp-20261025-history-profiles"
    for provider in ("claude", "codex"):
        for arm in ("A", "B"):
            for rep in range(3):
                write_run(tmp_path, exp, provider, arm=arm, rep=rep)
    with psycopg.connect(db) as c:
        experiment(c, exp)
        before = c.execute("select count(*) from sessions").fetchone()[0]
        first = RE.ingest(c, root=tmp_path)
        assert first.attempt_records == 12 and first.attempts == 12
        assert first.recovered_profiles == first.legacy_logical_ids == first.missing_start == 12
        assert first.failures == [] and first.failures_total == 0
        assert first.native_sessions == 0 and first.unresolved_native == 12
        assert first.unresolved_native_reasons == {"client_id_not_recorded": 12}
        rows = c.execute("select profile,attempt_session_id,session_id from rsi_attempts "
                         "where experiment_id=%s", (exp,)).fetchall()
        assert len({r[1] for r in rows}) == 12  # old profile-less UUID collisions do not survive
        assert all(r[2] is None for r in rows)
        assert c.execute("select count(*) from sessions").fetchone()[0] == before
        second = RE.ingest(c, root=tmp_path)
        assert second.attempts == 0 and second.attempt_records == 12
        assert second.unresolved_native == 12 and second.failures == []


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("fault,code", [
    ("row_profile", "replay_profile_mismatch"),
    ("config_profile", "replay_config_profile_mismatch"),
    ("config_experiment", "replay_experiment_mismatch"),
    ("config_provider", "replay_provider_mismatch"),
    ("missing_config", "legacy_profile_unconfirmed"),
    ("run_id", "replay_run_identity_mismatch"),
    ("logical_id", "logical_attempt_id_mismatch"),
])
def test_exact_identity_conflicts_are_rejected(tmp_path, provider, fault, code):
    exp = "exp-20261025-history-conflict"
    path, row = write_run(tmp_path, exp, provider)
    metadata = path.parent.parent / "replay.json"
    if fault == "row_profile":
        row["profile"] = "codex" if provider == "claude" else "claude"
    elif fault == "run_id":
        row["run_id"] += "-other"
    elif fault == "logical_id":
        row["session_id"] = "unrelated-actor"
    elif fault == "missing_config":
        metadata.unlink()
    else:
        config = json.loads(metadata.read_text())
        if fault == "config_profile":
            config["config"]["profile"] = "other"
        elif fault == "config_provider":
            config["config"]["agent"] = "other"
        else:
            config["experiment_id"] = "exp-another"
        metadata.write_text(json.dumps(config))
    path.write_text(json.dumps(row))
    with pytest.raises(RE.RecoveryError) as err:
        RE.attempt_record(path, root=tmp_path, experiment_id=exp)
    assert err.value.code == code


def test_unprofiled_legacy_record_and_current_native_record_both_work(db, tmp_path):
    old, new = "exp-20261025-history-unprofiled", "exp-20261025-history-current"
    write_run(tmp_path, old, None)
    at = AL.Attempt(new, "A", "anatomy-plan", 0, "codex")
    write_run(tmp_path, new, "codex", profile="codex", session_id=at.session_id,
              native_session_id="native-observed-client", started_at="2026-10-27T01:00:00Z")
    with psycopg.connect(db) as c:
        experiment(c, old)
        experiment(c, new)
        report = RE.ingest(c, root=tmp_path)
        assert report.attempt_records == 2 and report.recovered_profiles == 0
        assert report.native_sessions == 1 and report.unresolved_native == 1
        assert report.missing_start == 1 and report.failures == []
        assert c.execute("select session_id from rsi_attempts where experiment_id=%s", (new,)).fetchone() == (
            "codex:native-observed-client",)


def test_failure_diagnostics_are_actionable_and_bounded(db, tmp_path):
    exp = "exp-20261025-history-bounded"
    directory = tmp_path / "replays" / exp / "runs"
    directory.mkdir(parents=True)
    for n in range(60):
        (directory / f"bad-{n}.json").write_text("not json")
    with psycopg.connect(db) as c:
        experiment(c, exp)
        report = RE.ingest(c, root=tmp_path)
    assert report.failures_total == 60 and len(report.failures) == 50
    assert report.failure_counts == {"unreadable_replay_record": 60}
    assert report.as_dict()["failure_details_omitted"] == 10
    assert "unreadable_replay_record" in report.failures[0]
    assert "replays/exp-20261025-history-bounded/runs/bad-" in report.failures[0]


def test_late_legacy_record_preserves_stronger_trusted_native_evidence(db, tmp_path):
    exp = "exp-20261025-history-preserved"
    path, _ = write_run(tmp_path, exp, "claude")
    at = AL.Attempt(exp, "A", "anatomy-plan", 0, "claude")
    current = {"experiment_id": exp, "profile": "claude", "arm": "A", "case": "anatomy-plan", "rep": 0,
               "agent": "claude", "run_id": path.stem, "native_session_id": "preserved-native-client",
               "started_at": "2026-10-27T01:00:00Z", "duration_s": 42.525,
               "replay_record": path.relative_to(tmp_path).as_posix()}
    with psycopg.connect(db) as c:
        experiment(c, exp)
        AL.record_runner(c, [current])
        # Reproduce a stale availability reason written by an older late repair.
        c.execute("update rsi_attempts set evidence=evidence || "
                  "'{\"native_identity_unavailable\":\"client_id_not_recorded\"}'::jsonb where id=%s", (at.key,))
        before = c.execute("select started_at from sessions where session_id='preserved-native-client'").fetchone()
        report = RE.ingest(c, root=tmp_path)
        assert report.native_sessions == 1 and report.unresolved_native == 0 and report.failures == []
        sid, ev, source = c.execute("select session_id,evidence,source from rsi_attempts where id=%s", (at.key,)).fetchone()
        assert sid == ev["native_session_id"] == "preserved-native-client" and source == "runner"
        assert ev["native_identity_source"] == "trusted_client_response"
        assert "native_identity_unavailable" not in ev
        assert c.execute("select started_at from sessions where session_id='preserved-native-client'").fetchone() == before
        assert RE.ingest(c, root=tmp_path).attempts == 0


def test_native_collision_is_not_reintroduced_by_late_reconciliation(db, tmp_path):
    exp = "exp-20261025-history-collision"
    at = AL.Attempt(exp, "A", "anatomy-plan", 0, "claude")
    a = {"experiment_id": exp, "profile": "claude", "arm": "A", "case": "anatomy-plan", "rep": 0,
         "agent": "claude", "run_id": "run-collision", "native_session_id": "shared-native-client"}
    with psycopg.connect(db) as c:
        # The same provider actor already belongs to another attempt in this experiment.
        c.execute("insert into sessions(session_id,kind,project_dir,env,experiment_id,meta) "
                  "values('shared-native-client','headless','fixture','sandbox',%s,'{\"attempt_ref\":\"another-attempt\"}')", (exp,))
        AL.record_runner(c, [a])
        assert AL.reconcile_native_sessions(c) == 0
        assert c.execute("select session_id from rsi_attempts where id=%s", (at.key,)).fetchone() == (None,)
        with pytest.raises(AL.refs.RefError, match="conflicting native identity"):
            AL.record_runner(c, [{**a, "native_session_id": "different-native-client"}])


def test_existing_other_session_link_is_never_silently_overridden(db):
    exp = "exp-20261025-history-link-conflict"
    a = {"experiment_id": exp, "profile": "claude", "arm": "A", "case": "anatomy-plan", "rep": 0,
         "agent": "claude", "run_id": "conflicting-link", "native_session_id": None}
    at = AL.Attempt(exp, "A", "anatomy-plan", 0, "claude")
    with psycopg.connect(db) as c:
        AL.record_runner(c, [a])
        c.execute("update rsi_attempts set session_id='unrelated-session' where id=%s", (at.key,))
        with pytest.raises(AL.refs.RefError, match="existing attempt session link"):
            AL.record_runner(c, [{**a, "native_session_id": "new-trusted-native"}])
        assert c.execute("select session_id from rsi_attempts where id=%s", (at.key,)).fetchone() == (
            "unrelated-session",)


def test_unsafe_metadata_and_boolean_repetition_are_not_accepted(tmp_path):
    exp = "exp-20261025-history-unsafe"
    path, row = write_run(tmp_path, exp, "claude")
    row["rep"] = True
    path.write_text(json.dumps(row))
    with pytest.raises(RE.RecoveryError) as err:
        RE.attempt_record(path, root=tmp_path, experiment_id=exp)
    assert err.value.code == "invalid_attempt_identity"
    path.unlink()
    path.symlink_to(tmp_path / "outside.json")
    with pytest.raises(RE.RecoveryError) as err:
        RE.attempt_record(path, root=tmp_path, experiment_id=exp)
    assert err.value.code == "unsafe_replay_path"
