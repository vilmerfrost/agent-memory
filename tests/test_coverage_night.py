"""amem-rsi-night's coverage and consumption rows (data-use plan R4, src/agent_memory/night_coverage.py) and the
morning brief's verdict (rsi_brief.coverage_verdict).

Disposable Postgres (conftest pg_url), temp HOME and RSI_SANDBOX_DIR. The night runs its fixture/stub stages with the
fake replay and staging of tests/rsi_night_support.py, but the REAL delta and evidence pack (rsi_evidence_pack):
ClickHouse, Langfuse and past.dev are stand-ins with the real named-query specs. No network, no hosted DB.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import coverage as CV, night_coverage as NC, rsi_brief as BR, rsi_evidence_pack as EP
from agent_memory import rsi_night as N
from agent_memory.integrations import clickhouse as CH, evidence as E, langfuse as LF, pastdev as PD
from rsi_night_support import RULE_A, RULE_B, adapters, clean_db, make_home, seed_rule, strip_agent_env

NIGHT = "2026-10-26"
CLOSED_DB = "postgresql://postgres:postgres@127.0.0.1:1/x"
SPECS = {"ch": {n: q.spec for n, q in CH.QUERIES.items()}, "lf": LF.LangfuseRetriever._QUERIES,
         "pd": PD.PastDevRetriever._QUERIES}


class Stub:
    """A provider stand-in with the real query specs: canned answers, a health switch, an optional failure."""

    def __init__(self, source: str, answers: Mapping[str, list[E.EvidenceRef]] | None = None, *,
                 healthy: bool = True, raises: bool = False) -> None:
        self.source, self._answers, self.healthy, self.raises = source, dict(answers or {}), healthy, raises
        self.calls: list[str] = []

    def health(self) -> E.Health:
        return E.Health(self.healthy, "" if self.healthy else "not configured")

    def named_queries(self) -> Mapping[str, E.NamedQuery]:
        return SPECS[self.source]

    def query(self, name: str, params: Mapping[str, str]) -> list[E.EvidenceRef]:
        self.calls.append(name)
        if self.raises:
            raise RuntimeError("provider exploded")
        return list(self._answers.get(name, []))


class CovEvidence(N.PackEvidence):
    """The real delta (settle off) with `s1` provider stand-ins, and the real pack with `s4` stand-ins (Supabase is the
    night's own database). `s4=None`: the providers the environment configures (none in tests: unavailable)."""

    def __init__(self, db_url: str, *, s1: Mapping[str, Any] | None = None, s4: Mapping[str, Any] | None = None,
                 limits: EP.DeltaLimits | None = None) -> None:
        super().__init__(db_url=db_url, retrievers=s4)
        self.s1, self.limits = s1, limits or EP.DeltaLimits(settle_s=0)

    def delta(self, since: str | None, *, conn: Any) -> Any:
        return EP.delta_since(since, conn=conn, limits=self.limits, retrievers=self.s1)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    monkeypatch.delenv("AMEM_COVERAGE_WRITE", raising=False)
    strip_agent_env(monkeypatch)
    make_home(tmp_path, monkeypatch)
    clean_db(pg_url)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("delete from coverage_snapshots")
        conn.execute("delete from routine_consumption")
    return SimpleNamespace(url=pg_url, sandbox=sandbox, tmp=tmp_path)


# --------------------------------------------------------------------------- helpers


def run(env: SimpleNamespace, ev: N.PackEvidence, night: str = NIGHT, **kw: Any) -> dict[str, Any]:
    rep = N.run_night(night=night, db_url=env.url, adapters=adapters(evidence=ev, **kw))
    assert N.validate_report(rep) == [], N.validate_report(rep)
    assert BR.validate_report(rep) == [], BR.validate_report(rep)
    return rep


def uses(url: str, night: str = NIGHT) -> dict[str, dict[str, Any]]:
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from routine_consumption where job = %s and run_key = %s", (NC.JOB, night))
        return {r["dataset"]: r for r in cur.fetchall()}


def snaps(url: str, night: str = NIGHT) -> dict[str, dict[str, Any]]:
    """The snapshot of each dataset for the night's own input window."""
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select s.* from coverage_snapshots s join routine_consumption c on c.dataset = s.dataset "
                    "and c.window_start = s.window_start and c.window_end = s.window_end "
                    "where c.job = %s and c.run_key = %s", (NC.JOB, night))
        return {r["dataset"]: r for r in cur.fetchall()}


def counts(url: str) -> tuple[int, int]:
    with psycopg.connect(url) as conn:
        return (conn.execute("select count(*) from coverage_snapshots").fetchone()[0],
                conn.execute("select count(*) from routine_consumption").fetchone()[0])


def brief(env: SimpleNamespace, night: str = NIGHT, db_url: str | None = None) -> tuple[dict[str, Any], str]:
    b = BR.make_brief(night=night, root=env.sandbox, db_url=db_url or env.url)
    return b, BR.render_markdown(b)


def deviation_ids(url: str, rule: str) -> list[str]:
    with psycopg.connect(url) as conn:
        return [str(r[0]) for r in conn.execute("select id from deviations where rule_id = %s order by id", (rule,))]


def healthy_s4(url: str) -> dict[str, Stub]:
    dev = deviation_ids(url, RULE_A[0])[0]
    rule = f"claude-md:global:{RULE_A[0]}"
    return {"ch": Stub("ch", {"rule_gap": [E.EvidenceRef(f"ch:q.rule_gap/{rule}", "ch", "metric", [f"amem:deviation/{dev}"],
                                                         {"rule_key": rule, "n_deviations": 3, "n_sessions": 3})]}),
            "lf": Stub("lf"),
            "pd": Stub("pd", {"recall": [E.EvidenceRef("past:amem:proposal/00000000-0000-4000-8000-00000000f00d", "pd",
                                                       "decision-memory", ["amem:proposal/00000000-0000-4000-8000-00000000f00d"],
                                                       {"rank": 1})]})}


def assert_report_matches_rows(rep: Mapping[str, Any], url: str) -> None:
    cov, u = rep["coverage"], uses(url, rep["night"])
    assert cov["enabled"] is True and cov["failed"] == 0 and cov["run_key"] == rep["night"]
    assert cov["datasets"] == {d: r["status"] for d, r in u.items()}
    assert sum(cov["consumption"].values()) == len(u)
    assert sum(cov["snapshots"].values()) == len(snaps(url, rep["night"]))
    for r in u.values():
        assert r["status"] in CV.CONSUMPTION_STATUSES and r["used"] <= r["considered"] <= r["queried"]


# --------------------------------------------------------------------------- a full fixture night


def test_a_fixture_night_writes_rows_that_match_its_report(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    seed_rule(env.url, RULE_B, "b")
    rep = run(env, CovEvidence(env.url, s4=healthy_s4(env.url)))
    assert rep["status"] == "ok" and rep["checkpoint"]["advanced"], rep["notes"]
    assert_report_matches_rows(rep, env.url)
    u, s = uses(env.url), snaps(env.url)
    dev = u[NC.DEVIATIONS]
    assert (dev["queried"], dev["considered"], dev["used"]) == (rep["delta"]["deviations"], 5, 5)
    assert dev["findings"] == len(rep["patterns"]) == 2 and dev["status"] == "findings"
    assert dev["evidence_refs"] and all(r.startswith("amem:deviation/") for r in dev["evidence_refs"])
    assert (dev["window_start"], dev["window_end"]) == (s[NC.DEVIATIONS]["window_start"], s[NC.DEVIATIONS]["window_end"])
    assert dev["analysis_version"].startswith("rsi-night/1 config ") and dev["source_checkpoint"].startswith("rsi-night")
    assert (s[NC.DEVIATIONS]["status"], s[NC.DEVIATIONS]["eligible"], s[NC.DEVIATIONS]["collected"]) == ("complete", 5, 5)
    ses = u[NC.SESSIONS]       # counted, not analysed: never used_no_finding
    assert (ses["status"], ses["queried"], ses["used"]) == ("analysis_not_run", rep["delta"]["sessions"], 0)
    for ds in (NC.ROUTINE_FAILURES, NC.EVAL_FAILURES, NC.EXPERIMENTS, NC.EVIDENCE):     # nothing new in the window
        assert (u[ds]["status"], u[ds]["queried"], s[ds]["status"]) == ("analysis_not_run", 0, "no_eligible_data")
    # S4: the packs of the selected candidates asked Supabase, ClickHouse and past.dev (Langfuse had no step)
    ch, pd = u[NC.PROVIDER_DATASET["clickhouse"]], u[NC.PROVIDER_DATASET["pastdev"]]
    assert ch["status"] == "findings" and ch["used"] >= 1 and ch["evidence_refs"][0].startswith("ch:q.rule_gap/")
    assert s[NC.PROVIDER_DATASET["clickhouse"]]["status"] == "complete"
    assert "ch.rule_gap" in s[NC.PROVIDER_DATASET["clickhouse"]]["measured_fields"]
    assert pd["used"] == 0 and pd["status"] == "read_excluded" and sum(pd["skipped"].values()) >= 1   # context only
    assert NC.PROVIDER_DATASET["langfuse"] not in u
    assert set(rep["coverage"]["required"]) == {*NC.BASE_REQUIRED, *(NC.PROVIDER_DATASET[p] for p in
                                                                    ("supabase", "clickhouse", "pastdev"))}
    b, md = brief(env)
    assert b["verdict"]["conclusion"] == "findings" and b["verdict"]["findings"] >= 2
    assert "**Verdict: Findings (" in md and "No problem found" not in md
    assert "evidence_links:pastdev read_excluded" in md               # the gaps are still listed


def test_a_rerun_of_the_same_night_adds_no_rows(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    night = N.Night(N.load_config(), adapters(evidence=CovEvidence(env.url, s4=healthy_s4(env.url))), night=NIGHT,
                    db_url=env.url, install_signals=False)
    rep = night.run()
    before = counts(env.url)
    with psycopg.connect(env.url) as conn:
        stamps = conn.execute("select max(updated_at) from routine_consumption").fetchone()[0]
    assert before[0] > 0 and before[1] > 0
    again = N.run_night(night=NIGHT, db_url=env.url, adapters=adapters(evidence=CovEvidence(env.url)))
    assert again["status"] == "preflight_failed" and "coverage" not in again        # a done night is never rewritten
    # replaying the same ledger: every upsert is unchanged
    with psycopg.connect(env.url, autocommit=True) as conn:
        summary, problems = night.cov.write(conn, status=rep["status"], reason="replay")
    assert problems == [] and summary["datasets"] == rep["coverage"]["datasets"]
    assert counts(env.url) == before
    with psycopg.connect(env.url) as conn:
        assert conn.execute("select max(updated_at) from routine_consumption").fetchone()[0] == stamps


# --------------------------------------------------------------------------- the eight distinctions stay apart


def test_empty_window_missing_collection_and_failed_analysis_are_three_statuses(
        env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    run(env, CovEvidence(env.url), night="2026-10-24")                                  # empty window
    assert snaps(env.url, "2026-10-24")[NC.DEVIATIONS]["status"] == "no_eligible_data"
    assert uses(env.url, "2026-10-24")[NC.DEVIATIONS]["status"] == "analysis_not_run"

    seed_rule(env.url, RULE_A, "a")
    seed_rule(env.url, RULE_B, "b")
    rep = run(env, CovEvidence(env.url, limits=EP.DeltaLimits(settle_s=0, per_kind=2)), night="2026-10-25")
    s = snaps(env.url, "2026-10-25")[NC.DEVIATIONS]                                      # the delta's page limit
    assert (s["status"], s["eligible"], s["collected"]) == ("not_collected", 5, rep["delta"]["deviations"])
    assert "rest is read next night" in s["detail"]
    assert_report_matches_rows(rep, env.url)

    from agent_memory import deviations

    def broken(**_: Any) -> Any:
        raise RuntimeError("scan store unreadable")
    monkeypatch.setattr(deviations, "candidates", broken)
    seed_rule(env.url, RULE_B, "b2", sessions=1)
    run(env, CovEvidence(env.url), night=NIGHT)
    u = uses(env.url)[NC.DEVIATIONS]
    assert u["status"] == "analysis_failed" and "scan store unreadable" in u["detail"] and u["findings"] == 0
    assert uses(env.url)[NC.ROUTINE_FAILURES]["status"] == "analysis_not_run"       # other sources go on
    _, md = brief(env)
    assert "deviations:deviations analysis_failed" in md and "No problem found" not in md


# --------------------------------------------------------------------------- providers and the brief


@pytest.mark.parametrize(("stub", "status"), [(dict(healthy=False), "analysis_not_run"),
                                              (dict(raises=True), "analysis_failed")])
def test_an_unavailable_provider_is_never_used_no_finding(env: SimpleNamespace, stub: dict[str, Any],
                                                          status: str) -> None:
    s1 = {"ch": Stub("ch", **stub), "lf": Stub("lf"), "pd": Stub("pd")}
    rep = run(env, CovEvidence(env.url, s1=s1))
    assert_report_matches_rows(rep, env.url)
    ds = NC.PROVIDER_DATASET["clickhouse"]
    u, s = uses(env.url)[ds], snaps(env.url)[ds]
    assert u["status"] == status and u["used"] == 0 and "ch.rule_gap" in u["detail"]
    assert s["status"] == "measurement_unavailable" and "ch.rule_gap" in s["unmeasured_fields"]
    assert ds in rep["coverage"]["required"]
    b, md = brief(env)
    assert b["verdict"]["conclusion"] == "incomplete"
    assert any(g["dataset"] == ds for g in b["verdict"]["gaps"])
    assert "**Verdict: Analysis incomplete.**" in md and f"{ds} measurement_unavailable" in md
    assert "No problem found" not in md
    _, body = BR.notification(b)
    assert "Verdict: Analysis incomplete" in body


def test_an_owner_switched_off_provider_is_not_required(env: SimpleNamespace) -> None:
    class Off(Stub):
        owner_off = True

        def health(self) -> E.Health:
            return E.Health(False, "switched off (AMEM_CLICKHOUSE=off)")
    rep = run(env, CovEvidence(env.url, s1={"ch": Off("ch"), "lf": Stub("lf"), "pd": Stub("pd")}))
    ds = NC.PROVIDER_DATASET["clickhouse"]
    assert uses(env.url)[ds]["status"] == "analysis_not_run" and ds not in rep["coverage"]["required"]
    assert ds not in snaps(env.url)


def _seed_quiet_window(url: str) -> str:
    """Nothing new to check except a finished experiment (history for a tested candidate) and one ClickHouse link
    whose sample is below the night's threshold: data the night uses without a finding."""
    exp = "exp-20261026-hist01"
    with psycopg.connect(url) as conn:
        conn.execute("insert into rsi_candidates (dedupe_key, source, kind, target, rule_key, status) values "
                     "('dev:claude-md:global:quiet', 'deviations', 'edit_instruction', '~/.claude/CLAUDE.md', "
                     "'claude-md:global:quiet', 'tested')")
        conn.execute("insert into rsi_experiments (id, night, kind, status, verdict, meta, finished_at) values "
                     "(%s, '2026-10-25', 'edit_instruction', 'evaluated', 'NO_EFFECT', %s, now())",
                     (exp, Jsonb({"candidate_key": "dev:claude-md:global:quiet"})))
        conn.execute("insert into rsi_evidence (system, kind, subject_refs, metrics, external_ref) values "
                     "('clickhouse', 'metric', %s, %s, 'ch:q.rule_gap/claude-md:global:quiet')",
                     ([f"amem:experiment/{exp}"], Jsonb({"n_sessions": 2, "gap": 0.1})))
    return exp


def test_all_required_datasets_complete_and_used_without_findings_is_no_problem_found(env: SimpleNamespace) -> None:
    exp = _seed_quiet_window(env.url)
    s1 = {"ch": Stub("ch", {"rule_gap": [E.EvidenceRef("ch:q.rule_gap/claude-md:global:quiet", "ch", "metric",
                                                       [f"amem:experiment/{exp}"], {"n_sessions": 2})]}),
          "lf": Stub("lf"), "pd": Stub("pd")}
    rep = run(env, CovEvidence(env.url, s1=s1))
    assert rep["status"] == "ok" and rep["experiments"] == []
    assert_report_matches_rows(rep, env.url)
    u, s = uses(env.url), snaps(env.url)
    for ds in (NC.EXPERIMENTS, NC.EVIDENCE, NC.PROVIDER_DATASET["clickhouse"]):
        assert (u[ds]["status"], s[ds]["status"], u[ds]["findings"]) == ("used_no_finding", "complete", 0), ds
    b, md = brief(env)
    assert b["verdict"]["conclusion"] == "all_clear" and b["verdict"]["gaps"] == []
    assert md.split("\n")[4].startswith("**Verdict: No problem found.**")
    assert "Nothing new to check in: deviations:deviations" in md
    _, body = BR.notification(b)
    assert "Verdict: No problem found." in body


def test_the_switch_off_writes_nothing_and_the_brief_says_not_recorded(env: SimpleNamespace,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMEM_COVERAGE_WRITE", "0")
    _seed_quiet_window(env.url)
    rep = run(env, CovEvidence(env.url))
    assert counts(env.url) == (0, 0)
    assert rep["coverage"]["enabled"] is False and rep["coverage"]["datasets"] == {}
    b, md = brief(env)
    assert b["verdict"]["conclusion"] == "not_recorded" and "AMEM_COVERAGE_WRITE=0" in md
    assert "**Verdict not recorded:**" in md and "No problem found" not in md


def test_an_unreadable_coverage_table_makes_the_verdict_unknown(env: SimpleNamespace) -> None:
    _seed_quiet_window(env.url)
    run(env, CovEvidence(env.url))
    b, md = brief(env, db_url=CLOSED_DB)
    assert b["verdict"]["conclusion"] == "unknown" and "coverage not read" in md and "No problem found" not in md


# --------------------------------------------------------------------------- coverage never fails the night


def test_a_failing_coverage_write_never_changes_the_night(env: SimpleNamespace,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    seed_rule(env.url, RULE_A, "a")

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("coverage table gone")
    monkeypatch.setattr(CV, "record_consumption", boom)
    rep = run(env, CovEvidence(env.url, s4=healthy_s4(env.url)))
    assert rep["status"] == "ok" and rep["checkpoint"]["advanced"] and N.EXIT[rep["status"]] == 0
    assert rep["coverage"]["consumption"] == {} and rep["coverage"]["failed"] > 0
    assert rep["coverage"]["snapshots"]                                 # each record has its own savepoint
    assert any(n.startswith("coverage consumption ") and "coverage table gone" in n for n in rep["notes"])
    assert counts(env.url)[1] == 0
    b, md = brief(env)
    assert b["verdict"]["conclusion"] in ("incomplete", "findings") and "No problem found" not in md


@pytest.mark.parametrize("where", ["records", "feed"])
def test_a_ledger_bug_never_changes_the_night(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
                                              where: str) -> None:
    seed_rule(env.url, RULE_A, "a")

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("ledger bug")
    monkeypatch.setattr(NC.Ledger, "records" if where == "records" else "_step", boom)
    rep = run(env, CovEvidence(env.url, s4=healthy_s4(env.url)))
    assert rep["status"] == "ok" and rep["checkpoint"]["advanced"]
    assert any(n.startswith("coverage not recorded") and "ledger bug" in n for n in rep["notes"])
    assert counts(env.url) == (0, 0)                     # nothing half-counted is written


# --------------------------------------------------------------------------- report validation


def test_report_validation_checks_the_coverage_object(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    rep = run(env, CovEvidence(env.url, s4=healthy_s4(env.url)))
    bad = {**rep, "coverage": {**rep["coverage"], "datasets": {"Not A Key": "findings"}}}
    assert any("coverage.datasets" in e for e in N.validate_report(bad))
    assert any("coverage.datasets" in e for e in BR.validate_report(bad))
    wrong = {**rep, "coverage": {**rep["coverage"], "consumption": {"all_clear": 1}}}
    assert any("coverage.consumption" in e for e in N.validate_report(wrong))
    other = {**rep, "coverage": {**rep["coverage"], "run_key": "2026-01-01"}}
    assert "coverage.run_key must be the night" in N.validate_report(other)
    assert NC.report_problems([]) == ["coverage must be an object"]


def test_requirements_keep_the_base_datasets_whatever_the_report_says() -> None:
    names = [r.dataset for r in NC.requirements(["evidence_links:clickhouse", "sessions:sessions", "x:y"])]
    assert names == [*NC.BASE_REQUIRED, "evidence_links:clickhouse"]
    assert all(r.allow_no_eligible for r in NC.requirements([]))
