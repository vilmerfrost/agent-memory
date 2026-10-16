"""Mission 004C: the evidence-ask demo (scripts/demo_evidence_ask.py) on the synthetic dataset, with every external
provider unconfigured: they answer `unavailable` (never an empty success), every finding carries source, ref,
sample size, timestamp and statuses, the output is bannered SYNTHETIC, and --record writes exactly one
rsi_evidence row (the structured finding). The ask runs as the real CLI in a subprocess."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from synth_support import fresh_db, load_script

demo = load_script("demo_evidence_ask")


def test_demo_with_unconfigured_providers_records_one_row(pg_url: str, tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    with fresh_db(pg_url) as url:
        with psycopg.connect(url) as conn:
            assert conn.execute("select count(*) from rsi_evidence").fetchone()[0] == 0
        assert demo.main(["--db-url", url, "--out", str(tmp_path), "--json"]) == 0      # seeds, asks, records
        rep = json.loads(capsys.readouterr().out)
        with psycopg.connect(url) as conn:
            total = conn.execute("select count(*) from rsi_evidence").fetchone()[0]
        assert demo.main(["--db-url", url, "--out", str(tmp_path), "--no-record"]) == 0  # reuses the seeded data
        text = capsys.readouterr().out
    gold = json.loads((tmp_path / "gold.json").read_text())
    assert rep["synthetic"] is True and rep["banner"].startswith("SYNTHETIC DATA") and rep["question"] == "repeat_failures"
    assert rep["dataset"]["status"] == "seeded" and rep["dataset"]["anchor"] == gold["anchor"]
    assert rep["providers"] == {"supabase": "ok", "clickhouse": "unavailable", "langfuse": "unavailable",
                                "pastdev": "unavailable"}
    down = [s for s in rep["steps"] if not s["step"].startswith("sb.")]
    assert down and all(s["status"] == "unavailable" and s["found"] == 0 and s["detail"] for s in down)
    assert {s["step"] for s in down} == {"ch.rule_gap", "ch.rule_weekly_rate", "pd.recall", "lf.experiment_scores",
                                         "lf.trace"}
    for f in rep["findings"]:
        assert set(f) >= {"source", "service", "ref", "supabase_ref", "sample_size", "timestamp", "provider_status",
                          "status", "step"}
        assert f["provider_status"] == "ok" and f["timestamp"] and f["supabase_ref"]
    story = gold["questions"]["repeat_failures"]
    top = next(f for f in rep["findings"] if f["step"] == "sb.recurring_deviations" and f["sample_size"] == 10)
    assert top["status"] == "authoritative"
    assert sorted(f["ref"] for f in rep["findings"] if f["step"] == "sb.attempts_for") == story["attempts"]
    assert {f["ref"] for f in rep["findings"] if f["service"] == "langfuse"} == set(story["trace_links"].values())
    assert any("4 Claude, 3 Codex" in line for line in rep["summary"])
    rec = rep["recorded"]
    assert rec["requested"] and rec["new_rows"] == 1 and rec["cli_count"] == 1 and total == 3 + 1
    ((row),) = rec["rows"]
    assert (row["system"], row["kind"]) == ("supabase", "answer") and row["external_ref"].startswith("answer:repeat_")
    assert text.count("SYNTHETIC DATA") == 2 and "Recorded:" not in text and "dataset reused" in text
    assert "ZZLEAK" not in json.dumps(rep) + text


def test_demo_refuses_a_remote_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert demo.main(["--db-url", "postgresql://u:hunter2@db.example.com:5432/postgres"]) == 2
    err = capsys.readouterr().err
    assert "non-local" in err and "hunter2" not in err
