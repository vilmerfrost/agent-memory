"""Mission 004A: the NCM research-run contract (results.json) and its evidence mapping. No network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory.integrations import evidence as E, ncm

OK: dict[str, Any] = {
    "run_id": "ncm-20261020-sudoku-easy", "experiment_id": "exp-20261020-ncm", "task": "sudoku-easy", "status": "ok",
    "repo_commit": "a" * 40, "checkpoint_sha256": {"sudoku/easy/model.pt": "b" * 64},
    "commands": ["python evaluate.py --config config/eval/sudoku-easy.yaml --checkpoint-dir artifacts/sudoku-easy"],
    "hardware": {"device": "cuda", "gpu": "L4", "gpu_count": 1},
    "metrics": {"solved_rate": 0.91, "cell_rate": 0.98, "valid_rate": 0.99},
    "cost": {"gpu_minutes": 7.5, "usd": 0.1}, "started_at": "2026-10-20T10:00:00Z", "finished_at": "2026-10-20T10:08:00Z",
}


def test_ok_run_parses_and_maps_to_metrics_only_evidence() -> None:
    run = ncm.parse_results(OK)
    ev = ncm.to_evidence(run)
    assert ev.ref == "ncm:run/ncm-20261020-sudoku-easy" and ev.source == "ncm" and ev.kind == "research-run"
    assert ev.subject_refs == ["amem:experiment/exp-20261020-ncm"] and ev.summary is None
    assert ev.metrics["solved_rate"] == 0.91 and ev.metrics["cost_usd"] == 0.1 and ev.metrics["hw_gpu"] == "L4"
    assert "commands" not in ev.metrics          # commands and checkpoint hashes stay in the evidence folder


def test_not_run_needs_a_reason_and_no_metrics() -> None:
    nr = {"run_id": "ncm-20261020-tinystories", "task": "tinystories", "status": "not-run",
          "reason": "decision B2 not taken: no GPU approved"}
    ev = ncm.to_evidence(ncm.parse_results(nr))
    assert ev.metrics == {"task": "tinystories", "status": "not-run"} and ev.summary.startswith("decision B2")
    with pytest.raises(ncm.ResultsError, match="needs a reason"):
        ncm.parse_results({**nr, "reason": " "})
    with pytest.raises(ncm.ResultsError, match="never described as working"):
        ncm.parse_results({**nr, "metrics": {"gen_ppl": 30.0}})


@pytest.mark.parametrize("patch, msg", [
    ({"task": "imagenet"}, "unknown task"),
    ({"metrics": {"solved_rate": 0.9, "accuracy": 1.0}}, "not reported by sudoku-easy"),
    ({"metrics": {"solved_rate": float("nan")}}, "finite"),
    ({"metrics": {"solved_rate": True}}, "finite"),
    ({"metrics": {}}, "at least one metric"),
    ({"repo_commit": "main"}, "40-hex"),
    ({"checkpoint_sha256": {"/Users/x/model.pt": "b" * 64}}, "relative file names"),
    ({"checkpoint_sha256": {}}, "checkpoint hashes"),
    ({"commands": []}, "exact commands"),
    ({"commands": ["a\nrm -rf ~"]}, "one-line"),
    ({"hardware": {"device": "tpu"}}, "hardware.device"),
    ({"hardware": {"device": "cuda", "hostname": "box.local"}}, "device, gpu, gpu_count only"),
    ({"cost": {"usd": -1}}, "non-negative"),
    ({"run_id": "sudoku"}, "run_id"),
    ({"experiment_id": "exp-1"}, "experiment_id"),
    ({"prompt": "anything"}, "unknown field"),
])
def test_strict_validation(patch: dict[str, Any], msg: str) -> None:
    with pytest.raises(ncm.ResultsError, match=msg):
        ncm.parse_results({**OK, **patch})


def test_load_results_file(tmp_path: Path) -> None:
    p = tmp_path / "results.json"
    p.write_text(json.dumps(OK))
    assert ncm.load_results(p).task == "sudoku-easy"
    p.write_text("[1, 2]")
    with pytest.raises(ncm.ResultsError, match="one object"):
        ncm.load_results(p)
    with pytest.raises(ncm.ResultsError, match="unreadable"):
        ncm.load_results(tmp_path / "missing.json")


def test_evidence_records_idempotently_as_amem_app(pg_url: str) -> None:
    pw = "ncm-test-pw-0123456789"
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{pw}'")
        conn.execute("delete from rsi_evidence where system = 'ncm'")
    app = pg_url.replace("postgres:postgres@", f"amem_app:{pw}@", 1)
    ev = ncm.to_evidence(ncm.parse_results(OK))
    try:
        with psycopg.connect(app) as conn:
            assert E.record(conn, ev) == E.record(conn, ev)
            conn.commit()
            row = conn.execute("select system, kind, subject_refs, metrics->>'solved_rate' from rsi_evidence "
                               "where external_ref = %s", (ev.ref,)).fetchone()
        assert row == ("ncm", "research-run", ["amem:experiment/exp-20261020-ncm"], "0.91")
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute("delete from rsi_evidence where system = 'ncm'")
