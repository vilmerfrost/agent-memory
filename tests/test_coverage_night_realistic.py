"""amem-rsi-night coverage on realistic data (data-use plan R4): the synthetic RSI story (agent_memory.synth: prod and
sandbox sessions, deviations of three rules, a failing routine streak, eval failures, an experiment with Langfuse
links, evidence rows) in its own fresh database, then four nights:

    1  the story's first night, every provider answering          -> findings (deviations, routine streak, evals)
    2  nothing new, ClickHouse down, Langfuse switched off          -> analysis incomplete; Langfuse not required
    3  a quiet window: one finished experiment and one small link -> no problem found
    4  a burst of new deviations past the delta's page limit       -> deviations not_collected

Run with `-s` to see each night's rows and the brief's verdict line (the R4 evidence run)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import night_coverage as NC, rsi_brief as BR, rsi_evidence_pack as EP, rsi_night as N, synth
from agent_memory.integrations import evidence as E
from rsi_night_support import RULE_A, RULE_B, adapters, make_home, seed_rule, strip_agent_env
from synth_support import fresh_db
from test_coverage_night import CovEvidence, Stub

ANCHOR = datetime.now(UTC).date()
# Keep these simulated nights after the seed story. Fixed October keys collide
# with seed experiments as its real-time anchor advances (e.g. 29 October).
NIGHTS = tuple((ANCHOR + timedelta(days=i)).isoformat() for i in range(4))
DS = synth.build(ANCHOR)
R1 = synth.rule_key(synth.R1)


@pytest.fixture
def story(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> Iterator[SimpleNamespace]:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.delenv("AMEM_COVERAGE_WRITE", raising=False)
    strip_agent_env(monkeypatch)
    make_home(tmp_path, monkeypatch)
    with fresh_db(pg_url) as url:
        monkeypatch.setenv("AMEM_DB_URL", url)
        synth.seed(url, ANCHOR, out=tmp_path / "synth")
        yield SimpleNamespace(url=url, sandbox=sandbox)


def show(url: str, rep: dict[str, Any], sandbox: Path) -> tuple[dict[str, Any], str]:
    night = rep["night"]
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select dataset, status, queried, considered, used, findings, skipped, evidence_refs, detail, "
                    "window_start, window_end from routine_consumption where job = %s and run_key = %s order by dataset",
                    (NC.JOB, night))
        uses = cur.fetchall()
        cur.execute("select s.dataset, s.status, s.eligible, s.collected, s.unmeasured_fields, s.detail "
                    "from coverage_snapshots s join routine_consumption c on c.dataset = s.dataset and "
                    "c.window_start = s.window_start and c.window_end = s.window_end where c.job = %s and c.run_key = %s "
                    "order by s.dataset", (NC.JOB, night))
        snaps = cur.fetchall()
    b = BR.make_brief(night=night, root=sandbox, db_url=url)
    md = BR.render_markdown(b)
    print(f"\n===== night {night}: status {rep['status']}, delta {rep['delta']}")
    print(f"report.coverage: consumption={rep['coverage']['consumption']} snapshots={rep['coverage']['snapshots']} "
          f"failed={rep['coverage']['failed']}")
    if uses:
        print(f"window: {uses[0]['window_start']} .. {uses[0]['window_end']}")
    for u in uses:
        print(f"  use  {u['dataset']:<30} {u['status']:<17} q={u['queried']} c={u['considered']} u={u['used']} "
              f"f={u['findings']} skipped={u['skipped']} refs={len(u['evidence_refs'])} detail={(u['detail'] or '')[:90]}")
    for s in snaps:
        print(f"  snap {s['dataset']:<30} {s['status']:<23} eligible={s['eligible']} collected={s['collected']} "
              f"unmeasured={s['unmeasured_fields']} detail={(s['detail'] or '')[:80]}")
    print("brief:\n  " + "\n  ".join(md.split("\n")[2:5]))
    return b, md


def healthy() -> dict[str, Stub]:
    dev = f"amem:deviation/{DS.ids['r1_own_deviations'][0]}"
    return {"ch": Stub("ch", {"rule_gap": [E.EvidenceRef(f"ch:q.rule_gap/{R1}", "ch", "metric", [dev],
                                                         {"rule_key": R1, "n_deviations": 10, "n_sessions": 7})]}),
            "lf": Stub("lf"), "pd": Stub("pd")}


def test_four_nights_on_the_synthetic_story(story: SimpleNamespace) -> None:
    def night(key: str, ev: CovEvidence) -> dict[str, Any]:
        rep = N.run_night(night=key, db_url=story.url, adapters=adapters(evidence=ev))
        assert N.validate_report(rep) == [] and BR.validate_report(rep) == [], N.validate_report(rep)
        return rep

    # 1: the story's first night
    r1 = night(NIGHTS[0], CovEvidence(story.url, s1=healthy()))
    b1, md1 = show(story.url, r1, story.sandbox)
    assert r1["delta"]["deviations"] > 0 and r1["patterns"]
    assert b1["verdict"]["conclusion"] == "findings" and "No problem found" not in md1

    # 2: nothing new; ClickHouse down, Langfuse switched off by the owner (not required)
    class Off(Stub):
        owner_off = True

        def health(self) -> E.Health:
            return E.Health(False, "switched off (AMEM_LANGFUSE=off)")
    r2 = night(NIGHTS[1], CovEvidence(story.url, s1={"ch": Stub("ch", healthy=False), "lf": Off("lf"),
                                                        "pd": Stub("pd")}))
    b2, md2 = show(story.url, r2, story.sandbox)
    assert b2["verdict"]["conclusion"] == "incomplete" and "**Verdict: Analysis incomplete.**" in md2
    assert NC.PROVIDER_DATASET["clickhouse"] in r2["coverage"]["required"]
    assert NC.PROVIDER_DATASET["langfuse"] not in r2["coverage"]["required"]

    # 3: a quiet window: a finished experiment for a tested candidate and one ClickHouse link below the threshold
    with psycopg.connect(story.url) as conn:
        key = conn.execute("select dedupe_key from rsi_candidates order by dedupe_key limit 1").fetchone()[0]
        conn.execute("update rsi_candidates set status = 'tested' where dedupe_key = %s", (key,))
        conn.execute("insert into rsi_experiments (id, night, kind, status, verdict, meta, finished_at) values "
                     "('exp-20261024-quiet1', %s, 'edit_instruction', 'evaluated', 'NO_EFFECT', %s, now())",
                     (NIGHTS[1], Jsonb({"candidate_key": key})))
        conn.execute("insert into rsi_evidence (system, kind, subject_refs, metrics, external_ref) values "
                     "('clickhouse', 'metric', %s, %s, %s)",
                     (["amem:experiment/exp-20261024-quiet1"], Jsonb({"n_sessions": 2}), f"ch:q.rule_gap/{R1}@quiet"))
    r3 = night(NIGHTS[2], CovEvidence(story.url, s1={"ch": Stub("ch"), "lf": Stub("lf"), "pd": Stub("pd")}))
    b3, md3 = show(story.url, r3, story.sandbox)
    assert b3["verdict"]["conclusion"] == "all_clear" and "**Verdict: No problem found.**" in md3

    # 4: a burst of deviations past the page limit: the window was not fully collected
    seed_rule(story.url, RULE_A, "burst-a")
    seed_rule(story.url, RULE_B, "burst-b")
    r4 = night(NIGHTS[3], CovEvidence(story.url, s1={"ch": Stub("ch"), "lf": Stub("lf"), "pd": Stub("pd")},
                                         limits=EP.DeltaLimits(settle_s=0, per_kind=3)))
    b4, md4 = show(story.url, r4, story.sandbox)
    with psycopg.connect(story.url) as conn:
        st = conn.execute("select s.status, s.eligible, s.collected from coverage_snapshots s join routine_consumption c "
                          "using (dataset, window_start, window_end) where c.run_key = %s and dataset = %s",
                          (NIGHTS[3], NC.DEVIATIONS)).fetchone()
    assert st[0] == "not_collected" and st[1] == 5 and st[2] < 5
    assert "No problem found" not in md4
