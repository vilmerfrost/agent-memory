"""Mission 004C: the `repeat_failures` question and the `--record` changes (questions.py), on the synthetic
dataset in a fresh database. ClickHouse, Langfuse and past.dev are stand-ins with the real named-query specs, so
the plan, the follow-ups, the statuses and the re-reads are under test; each adapter's SQL/HTTP is tested in its
own file."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import cli, synth
from agent_memory.integrations import clickhouse as CH, evidence as E, langfuse as LF, pastdev as PD, questions as Q


@pytest.fixture(scope="module")
def world(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, dict[str, Any]]]:
    from synth_support import fresh_db

    with fresh_db(pg_url) as url:
        rep = synth.seed(url, datetime.now(UTC).date(), out=tmp_path_factory.mktemp("q-gold"))
        yield url, json.loads(Path(rep["gold"]).read_text())


class _Stub:
    def __init__(self, source: str, specs: Mapping[str, E.NamedQuery], answers: Mapping[str, list[E.EvidenceRef]],
                 healthy: bool = True):
        self.source, self._specs, self._answers, self._healthy = source, specs, answers, healthy
        self.calls: list[tuple[str, dict[str, str]]] = []

    def health(self) -> E.Health:
        return E.Health(self._healthy, "" if self._healthy else "not configured")

    def named_queries(self) -> Mapping[str, E.NamedQuery]:
        return self._specs

    def query(self, name: str, params: Mapping[str, str]) -> list[E.EvidenceRef]:
        self.calls.append((name, dict(params)))
        return list(self._answers.get(name, []))


def _retrievers(url: str, gold: dict[str, Any], healthy: bool = True) -> dict[str, Any]:
    story = gold["questions"]["repeat_failures"]
    rule, exp = story["top_rule"], story["experiments"][0]
    tid_b = story["failing_traces"][0].split("/", 1)[1]
    with psycopg.connect(url) as conn:
        dev = conn.execute("select id::text from deviations where instruction_source || ':' || rule_id = %s "
                           "order by id limit 1", (rule,)).fetchone()[0]
    prop = next(r for r in story["attempts"] if r.startswith("amem:proposal/"))
    ch = _Stub("ch", {n: q.spec for n, q in CH.QUERIES.items()}, {
        "rule_gap": [E.EvidenceRef(f"ch:q.rule_gap/{rule}", "ch", "metric", [f"amem:deviation/{dev}"],
                                   {"rule_key": rule, "n_deviations": 10, "n_sessions": 7, "gap": 0.1083})],
        "rule_weekly_rate": [E.EvidenceRef(f"ch:q.rule_weekly_rate/{rule}@2026-10-05", "ch", "metric",
                                           [f"amem:deviation/{dev}"], {"rate": 0.75})]}, healthy)
    lf = _Stub("lf", LF.LangfuseRetriever._QUERIES, {
        "experiment_scores": [E.EvidenceRef(f"lf:dataset_run/{exp}-B", "lf", "score", [f"amem:experiment/{exp}"],
                                            {"traces": 1, "check_passed_rate": 0.0})],
        "trace": [E.EvidenceRef(f"lf:trace/{tid_b}", "lf", "trace", [f"amem:experiment/{exp}"],
                                {"arm": "B", "check_passed": False, "tool_errors": 2, "level": "ERROR"})]}, healthy)
    pd = _Stub("pd", PD.PastDevRetriever._QUERIES, {
        "recall": [E.EvidenceRef(f"past:{prop}", "pd", "decision-memory", [prop], {"rank": 1})]}, healthy)
    return {"sb": E.SupabaseRetriever(lambda: psycopg.connect(url)), "ch": ch, "lf": lf, "pd": pd}


def _status(ans: Q.Answer) -> list[tuple[str, str]]:
    return [(f"{st.source}.{st.query}", res.status) for st, res in ans.steps]


def test_repeat_failures_follows_the_top_rule_to_the_failing_trace(world: tuple[str, dict[str, Any]]) -> None:
    url, gold = world
    story = gold["questions"]["repeat_failures"]
    rs = _retrievers(url, gold)
    ans = Q.ask("repeat_failures", {}, rs, lambda: psycopg.connect(url))
    assert _status(ans) == [("sb.recurring_deviations", "ok"), ("ch.rule_gap", "ok"), ("sb.attempts_for", "ok"),
                            ("ch.rule_weekly_rate", "ok"), ("pd.recall", "ok"), ("sb.evals_for", "ok"),
                            ("sb.evidence_for", "ok"), ("lf.experiment_scores", "ok"), ("lf.trace", "ok")]
    params = {f"{st.source}.{st.query}": dict(st.params) for st, _ in ans.steps}
    assert params["sb.attempts_for"] == {"rule": story["top_rule"]}
    assert params["pd.recall"] == {"query": "reproduce before fix", "limit": "10"}
    assert params["sb.evals_for"] == {"experiment": story["experiments"][0]}
    assert rs["lf"].calls[-1] == ("trace", {"trace_id": story["failing_traces"][0].split("/", 1)[1]})  # B only
    (top,) = [f for f in ans.findings if f["step"] == "sb.recurring_deviations"
              and f["metrics"]["rule_key"] == story["top_rule"]]
    assert {k: top["metrics"][k] for k in ("n", "sessions", "claude_sessions", "codex_sessions")} == story["r1"]
    assert top["measure"]["n"] == 10 and top["status"] == "authoritative" and top["timestamp"]
    assert sorted(f["ref"] for f in ans.findings if f["step"] == "sb.attempts_for") == story["attempts"]
    live = [f for f in ans.findings if f["step"] == "lf.trace"]
    assert live and live[0]["status"] == "linked"
    assert ans.providers() == {"supabase": "ok", "clickhouse": "ok", "pastdev": "ok", "langfuse": "ok"}
    assert ans.to_json()["providers"] == ans.providers()


def test_repeat_failures_with_everything_else_unconfigured(world: tuple[str, dict[str, Any]]) -> None:
    url, gold = world
    rs = _retrievers(url, gold, healthy=False)
    ans = Q.ask("repeat_failures", {}, rs, lambda: psycopg.connect(url))
    assert dict(_status(ans)) == {"sb.recurring_deviations": "ok", "ch.rule_gap": "unavailable",
                                  "sb.attempts_for": "ok", "ch.rule_weekly_rate": "unavailable",
                                  "pd.recall": "unavailable", "sb.evals_for": "ok", "sb.evidence_for": "ok",
                                  "lf.experiment_scores": "unavailable", "lf.trace": "unavailable"}
    assert all(not s.calls for k, s in rs.items() if k != "sb")              # down: never queried
    assert ans.providers() == {"supabase": "ok", "clickhouse": "unavailable", "pastdev": "unavailable",
                               "langfuse": "unavailable"}
    nothing = Q.ask("repeat_failures", {}, {"sb": rs["sb"]}, lambda: psycopg.connect(url))   # no retriever at all
    assert {s: st for s, st in _status(nothing) if not s.startswith("sb.")} == {
        "ch.rule_gap": "unavailable", "ch.rule_weekly_rate": "unavailable", "pd.recall": "unavailable",
        "lf.experiment_scores": "unavailable", "lf.trace": "unavailable"}


def test_repeat_failures_with_a_rule_param(world: tuple[str, dict[str, Any]]) -> None:
    url, gold = world
    rs = _retrievers(url, gold)
    rule = "claude-md:global:synth-quote-globs"
    ans = Q.ask("repeat_failures", {"rule": rule, "days": "30"}, rs, lambda: psycopg.connect(url))
    params = {f"{st.source}.{st.query}": dict(st.params) for st, _ in ans.steps}
    assert params["ch.rule_gap"] == {"days": "30", "min_n": "3", "rule": rule}
    assert params["sb.attempts_for"] == {"rule": rule} and params["pd.recall"]["query"] == "synth quote globs"
    edits = [f for f in ans.findings if f["step"] == "sb.attempts_for"]
    assert [f["metrics"]["status"] for f in edits] == ["kept"]
    # the stand-in recall names a proposal (no experiment): no phase 2 for this rule's own attempts
    assert "lf.trace" not in params
    with pytest.raises(ValueError, match="bad value"):
        Q.QUESTIONS["repeat_failures"].check({"rule": "x' or 1=1"})


def test_record_writes_one_answer_row_and_never_rewrites_stored_links(world: tuple[str, dict[str, Any]]) -> None:
    url, gold = world
    rs = _retrievers(url, gold, healthy=False)
    with psycopg.connect(url) as conn:
        links_before = conn.execute("select id, metrics, observed_at from rsi_evidence where system = 'langfuse' "
                                    "order by id").fetchall()
        n_before = conn.execute("select count(*) from rsi_evidence").fetchone()[0]
    ans = Q.ask("repeat_failures", {"days": "29"}, rs, lambda: psycopg.connect(url))
    assert [f for f in ans.findings if f["step"] == "sb.evidence_for" and f["status"] == "linked"]   # read back
    with psycopg.connect(url) as conn:
        ids = Q.record_findings(conn, ans)
        assert len(ids) == 1 and Q.record_findings(conn, ans) == ids                     # idempotent
        again = Q.ask("repeat_failures", {"days": "29"}, rs, lambda: psycopg.connect(url))
        assert any(f["type"] == Q.ANSWER_KIND for f in again.findings)                   # the answer reads back ...
        assert Q.record_findings(conn, again) == ids                                     # ... but is never cited
        assert conn.execute("select count(*) from rsi_evidence").fetchone()[0] == n_before + 1
        assert conn.execute("select id, metrics, observed_at from rsi_evidence where system = 'langfuse' "
                            "order by id").fetchall() == links_before                   # untouched
        system, kind, ext, subjects, metrics, valid = conn.execute(
            "select system, kind, external_ref, subject_refs, metrics, valid_until is not null from rsi_evidence "
            "where id = %s", (ids[0],)).fetchone()
    assert (system, kind, valid) == ("supabase", "answer", True) and ext.startswith("answer:repeat_failures/")
    assert metrics["question"] == "repeat_failures" and metrics["params"] == {"days": "29"}
    assert metrics["providers"] == {"supabase": "ok", "clickhouse": "unavailable", "pastdev": "unavailable",
                                    "langfuse": "unavailable"}
    assert metrics["by_status"]["authoritative"] >= 8 and metrics["findings"] == len(again.findings)  # last write
    story = gold["questions"]["repeat_failures"]
    assert f"amem:experiment/{story['experiments'][0]}" in subjects and all(s.startswith("amem:") for s in subjects)
    assert "ZZLEAK" not in json.dumps(metrics)


def test_record_keeps_live_findings_but_not_pastdev_sink_links(world: tuple[str, dict[str, Any]]) -> None:
    url, gold = world
    rs = _retrievers(url, gold)
    prop = rs["pd"]._answers["recall"][0].subject_refs[0]
    sink_link = {"sha256": "a" * 64, "kind": "proposal", "status": "completed"}
    with psycopg.connect(url) as conn:                        # the link the past.dev sink wrote when it sent `prop`
        E.record(conn, E.EvidenceRef(f"past:{prop}", "pd", "decision-memory", [prop], sink_link, "proposal"))
        conn.commit()
    ans = Q.ask("repeat_failures", {"days": "28"}, rs, lambda: psycopg.connect(url))
    assert any(f["step"] == "pd.recall" and f["status"] == "linked" for f in ans.findings)
    with psycopg.connect(url) as conn:
        ids = Q.record_findings(conn, ans)
        rows = {str(i): (s, k, e) for i, s, k, e in conn.execute(
            "select id, system, kind, external_ref from rsi_evidence where id = any(%s::uuid[])", (ids,)).fetchall()}
        kept = conn.execute("select metrics from rsi_evidence where system = 'pastdev' and external_ref = %s",
                            (f"past:{prop}",)).fetchone()[0]
    # live: ch rule_gap + rule_weekly_rate, lf experiment_scores + trace; plus the answer row, last
    assert sorted(s for s, _, _ in rows.values()) == ["clickhouse", "clickhouse", "langfuse", "langfuse", "supabase"]
    assert rows[ids[-1]][1] == Q.ANSWER_KIND
    assert kept == sink_link                                  # the sink's sha256 survives an ask --record


def test_cli_lists_and_asks_the_new_question(world: tuple[str, dict[str, Any]], capsys: pytest.CaptureFixture[str],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    url, gold = world
    monkeypatch.setenv("AMEM_DB_URL", url)
    monkeypatch.setattr(CH.amem_config, "load_env", lambda: None)
    assert cli.main(["evidence", "questions"]) == 0
    assert "repeat_failures:" in capsys.readouterr().out
    assert cli.main(["evidence", "ask", "repeat_failures", "--param", "days=27", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["providers"] == {"supabase": "ok", "clickhouse": "unavailable", "pastdev": "unavailable",
                                "langfuse": "unavailable"}
    assert out["recorded"] == 0 and {f["status"] for f in out["findings"]} == {"authoritative", "linked"}
