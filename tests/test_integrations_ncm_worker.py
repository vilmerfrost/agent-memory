"""Mission 004B: the NCM worker side (ncm_worker.py), the contract's optional fields, the Modal script's caps
(read statically: modal is not installed here and nothing runs) and `amem evidence record-ncm`."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import cli
from agent_memory.integrations import ncm, ncm_worker as W

APP_PW = "ncm-worker-pw-0123456789"
EXP = "exp-20261020-ncm"
SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "ncm" / "modal_sudoku.py"


def merge_output(k: int = 1990, n: int = 2000, cell: float = 99.91, valid: float = 100.0) -> str:
    """scripts/eval_merge.py's two lines, with its exact format string."""
    return ("loading shards\nsolved %   solved  cell %  valid %\n"
            f"{100 * k / n:9.2f} {k:4d}/{n} {cell:7.2f} {valid:8.2f}\n")


def sha_text(**override: str) -> str:
    pins = {**W.PINNED_SHA256, **override}
    return "".join(f"{h}  {f}\n" for f, h in pins.items())


def build(**kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(run_id="ncm-20261020-sudoku-easy", experiment_id=EXP, stdout=merge_output(), exit_code=0,
                                repo_commit=W.REPO_COMMIT, sha256sum_text=sha_text(), seconds=180.0,
                                gpu_name="NVIDIA L4", started_at="2026-10-20T10:00:00+00:00",
                                finished_at="2026-10-20T10:03:00+00:00")
    args.update(kw)
    return W.build_results(**args)


def test_an_ok_run_passes_the_contract() -> None:
    res = build()
    run = ncm.parse_results(res)
    assert run.status == "ok" and run.reason is None
    assert run.metrics == {"solved_rate": 0.995, "cell_rate": 0.9991, "valid_rate": 1.0}
    assert run.cost == {"gpu_minutes": 3.0, "usd": 0.046}                      # 180 s x $0.000222 x 1.15 (eu)
    assert run.hardware == {"device": "cuda", "gpu": "NVIDIA L4", "gpu_count": 1}
    assert set(run.checkpoint_sha256) == set(W.CHECKPOINT_FILES) and set(run.dataset_sha256) == set(W.DATASET_FILES)
    assert (run.checkpoint_revision, run.dataset_revision) == (W.CHECKPOINT_REVISION, W.DATASET_REVISION)
    assert run.runtime_s == 180.0 and run.provider == "modal" and run.limitations
    assert any(W.REPO_COMMIT in c for c in run.commands) and any(W.DATASET_REVISION in c for c in run.commands)
    assert run.commands[-1].endswith("--out /results/ncm-20261020-sudoku-easy")
    ev = ncm.to_evidence(run)
    assert ev.ref == "ncm:run/ncm-20261020-sudoku-easy" and ev.subject_refs == [f"amem:experiment/{EXP}"]
    assert ev.metrics["dataset_revision"] == W.DATASET_REVISION and ev.metrics["cost_usd"] == 0.046
    assert ev.summary is None


@pytest.mark.parametrize("change, reason", [
    ({"sha256sum_text": sha_text(**{"sudoku/easy/generator.pt": "0" * 64})}, "sha256 mismatch for sudoku/easy/generator.pt"),
    ({"sha256sum_text": "".join(f"{h}  {f}\n" for f, h in W.CHECKPOINT_FILES.items())}, "no sha256 for sudoku/easy/puzzles"),
    ({"repo_commit": "f" * 40}, "repo commit is not the pinned one"),
    ({"exit_code": 1}, "evaluate.py exited 1"),
    ({"exit_code": None, "stdout": ""}, "evaluate.py timed out"),
    ({"stdout": "Traceback: CUDA out of memory"}, "no metrics row"),
    ({"stdout": merge_output(k=2001)}, "no metrics row"),                      # k > n is not a result
])
def test_anything_off_pin_or_broken_is_failed_with_no_metrics(change: dict[str, Any], reason: str) -> None:
    res = build(**change)
    run = ncm.parse_results(res)                                              # a failed run still passes the contract
    assert run.status == "failed" and run.reason is not None and run.reason.startswith(reason) and run.metrics == {}


def test_a_given_failure_wins_over_everything_else() -> None:
    run = ncm.parse_results(build(failure="the Modal call raised RemoteError:\n  container  lost"))
    assert run.status == "failed" and run.metrics == {}                       # the stdout row is ignored
    assert run.reason == "the Modal call raised RemoteError: container lost"   # one line
    assert len(build(failure="x" * 900)["reason"]) == 300


def test_the_eval_environment_is_pinned_exactly() -> None:
    names = {p.split("==")[0] for p in W.PIP}
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==\d[0-9A-Za-z.]*", p) for p in W.PIP), W.PIP   # no ranges, no extras
    assert {"torch", "transformers", "huggingface_hub", "hydra-core", "omegaconf"} <= names
    assert W.build_commands()[1] == "pip install " + " ".join(W.PIP)
    freeze = "torch==2.8.0\ntransformers==4.57.1\n"
    line = W.freeze_line(freeze)
    assert line == f"pip freeze sha256 {hashlib.sha256(freeze.encode()).hexdigest()} (pip-freeze.txt in the run folder)"
    assert W.freeze_line("") == W.freeze_line(" \n") == "pip freeze was not recorded"
    assert line in ncm.parse_results(build(extra_limitations=[line])).limitations   # fits the contract


def test_parsers_and_cost() -> None:
    assert W.parse_sudoku(merge_output(2000, 2000, 100.0, 100.0)) == {"solved_rate": 1.0, "cell_rate": 1.0, "valid_rate": 1.0}
    assert W.parse_sudoku("solved %   solved  cell %  valid %\n  50.00  1/2  101.00  100.00") is None
    assert W.parse_sha256sum(f"{'a' * 64}  ./x/y.pt\n{'b' * 64} *z.pt\nnoise\n") == {"x/y.pt": "a" * 64, "z.pt": "b" * 64}
    assert W.gpu_cost(722, "L4", "eu") == {"gpu_minutes": 12.033, "usd": 0.1843}   # start + run + idle caps
    assert build(gpu_name="L4\n; rm -rf /")["hardware"]["gpu"] == "L4"          # a strange name falls back


def test_contract_checks_the_optional_fields() -> None:
    ok = build()
    for bad in ({"checkpoint_revision": "main"}, {"dataset_sha256": {"../x": "0" * 64}}, {"runtime_s": -1},
                {"limitations": ["x" * 201]}, {"provider": "a\nb"}):
        with pytest.raises(ncm.ResultsError):
            ncm.parse_results({**ok, **bad})


CPU_USD_PER_CORE_S, MEM_USD_PER_GIB_S = 0.0000131, 0.00000222    # modal.com/pricing; billed on max(request, usage)


def script_constants() -> dict[str, Any]:
    """Module-level constants of the Modal script, both `X = 1` and `X, Y = 1, 2`."""
    consts: dict[str, Any] = {}
    for n in ast.parse(SCRIPT.read_text()).body:
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant):
                consts[t.id] = n.value.value
            elif isinstance(t, ast.Tuple) and isinstance(n.value, ast.Tuple):
                consts.update({e.id: v.value for e, v in zip(t.elts, n.value.elts, strict=True)
                               if isinstance(e, ast.Name) and isinstance(v, ast.Constant)})
    return consts


def test_the_modal_script_caps_cost_and_blocks_the_network() -> None:
    c = script_constants()
    assert (c["GPU"], c["REGION"]) == ("L4", "eu")
    assert (c["TIMEOUT_S"], c["STARTUP_TIMEOUT_S"], c["SCALEDOWN_S"], c["MAX_CONTAINERS"]) == (600, 120, 2, 1)
    assert c["EVAL_TIMEOUT_S"] == 480
    assert c["EVAL_TIMEOUT_S"] + 5 * c["STEP_TIMEOUT_S"] < c["TIMEOUT_S"]   # five helper commands: a failed result is still written
    tree = ast.parse(SCRIPT.read_text())
    (fn,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "evaluate"]
    kw = {k.arg: k.value for k in fn.decorator_list[0].keywords}
    assert isinstance(kw["retries"], ast.Constant) and kw["retries"].value == 0
    assert isinstance(kw["block_network"], ast.Constant) and kw["block_network"].value is True
    assert {k: v.id for k, v in kw.items() if isinstance(v, ast.Name)} == {
        "image": "image", "gpu": "GPU", "timeout": "TIMEOUT_S", "startup_timeout": "STARTUP_TIMEOUT_S",
        "scaledown_window": "SCALEDOWN_S", "max_containers": "MAX_CONTAINERS", "region": "REGION"}
    assert "volumes" in kw
    text = SCRIPT.read_text()
    assert "secret" not in text.lower().replace("no amem data, credentials", "")   # no Modal secrets are mounted


def test_the_worst_case_run_costs_at_most_25_cents() -> None:
    """Modal bills a container from start to stop: at most startup_timeout + timeout + scaledown_window. CPU
    and memory are billed too (assumed at most 2 physical cores and 8 GiB), all times the eu multiplier."""
    c = script_constants()
    billed_s = c["STARTUP_TIMEOUT_S"] + c["TIMEOUT_S"] + c["SCALEDOWN_S"]
    gpu = W.gpu_cost(billed_s, c["GPU"], c["REGION"])
    cpu_mem = billed_s * (2 * CPU_USD_PER_CORE_S + 8 * MEM_USD_PER_GIB_S) * W.REGION_MULTIPLIER[c["REGION"]]
    assert billed_s == 722 and gpu == {"gpu_minutes": 12.033, "usd": 0.1843}
    assert round(cpu_mem, 4) == 0.0365 and round(gpu["usd"] + cpu_mem, 4) == 0.2208 <= 0.25


# ---------------------------------------------------------------- record-ncm (disposable Postgres, amem_app)


@pytest.fixture
def app(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{APP_PW}'")
        conn.execute("insert into rsi_experiments (id, kind) values (%s, 'research') on conflict do nothing", (EXP,))
    url = pg_url.replace("postgres:postgres@", f"amem_app:{APP_PW}@", 1)
    monkeypatch.setenv("AMEM_DB_URL", url)
    yield url
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("set session_replication_role = replica")
        conn.execute("delete from rsi_evidence where system = 'ncm'")
        conn.execute("delete from rsi_experiments where id = %s", (EXP,))


def test_record_ncm(app: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "results.json"
    path.write_text(json.dumps(build()))
    assert cli.main(["evidence", "record-ncm", str(path), "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["evidence_id"] is None
    with psycopg.connect(app) as conn:
        assert conn.execute("select count(*) from rsi_evidence where system = 'ncm'").fetchone() == (0,)
    assert cli.main(["evidence", "record-ncm", str(path)]) == 0
    assert "recorded ncm:run/ncm-20261020-sudoku-easy status=ok" in capsys.readouterr().out
    with psycopg.connect(app) as conn:
        ((metrics, subjects),) = conn.execute("select metrics, subject_refs from rsi_evidence where system = 'ncm'").fetchall()
    assert metrics["solved_rate"] == 0.995 and subjects == [f"amem:experiment/{EXP}"]
    path.write_text(json.dumps(build(experiment_id="exp-20261020-unknown")))
    assert cli.main(["evidence", "record-ncm", str(path)]) == 1                # not registered: refused
    path.write_text(json.dumps({**build(), "status": "great"}))
    assert cli.main(["evidence", "record-ncm", str(path)]) == 2                # not the contract
