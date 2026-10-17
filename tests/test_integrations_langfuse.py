"""Mission 004A Langfuse adapter: parsers, deterministic ids, the sandbox guard, the text policy (a leak
test), the OTLP + ingestion round trip against a local Langfuse v4 stand-in, the sink (amem_app on a
disposable Postgres) and the retriever. Nothing leaves 127.0.0.1."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import socket
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import psycopg
import pytest

from agent_memory.integrations import egress, evidence as E, langfuse as L, refs, sync
from integrations_mock import MockService, Request

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)       # the retriever's clock: test runs are dated 2026-10-16

PK, SK = "pk-lf-test-0000", "sk-lf-test-1111"
APP_PW = "lf-test-pw-0123456789"
EXP = "exp-20261017-01"
ART = "ab" * 32

# Markers that must never leave the machine.
PROMPT_M = "PROMPT-MARKER-7f3a"
TOOLIN_M = "TOOLINPUT-MARKER-91c2"
FILE_M = "FILECONTENT-MARKER-5d0e"
PATH_M = "FILEPATH-MARKER-b44a"
REASON_M = "REASONING-MARKER-0c1d"
EARLY_M = "EARLYMSG-MARKER-e9f8"
STDERR_M = "STDERR-MARKER-77aa"
SECRET = "sk-test-Zx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9"
MARKERS = (PROMPT_M, TOOLIN_M, FILE_M, PATH_M, REASON_M, EARLY_M, STDERR_M, SECRET)


# ---------------------------------------------------------------- fixtures modeled on the real shapes (2026-10-16)


def claude_result(result: str = "Wrote sandbox-ok-claude to /work/status.txt") -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 2790, "duration_api_ms": 2100,
            "num_turns": 2, "result": result, "session_id": "0b3e7d2a-8c1f-4f6e-9a2b-1c3d4e5f6a7b",
            "total_cost_usd": 0.00048,
            "usage": {"input_tokens": 12, "cache_creation_input_tokens": 900, "cache_read_input_tokens": 3000,
                      "output_tokens": 40, "service_tier": "standard"},
            "modelUsage": {"claude-haiku-5-5": {"inputTokens": 12, "outputTokens": 40, "cacheReadInputTokens": 3000,
                                                "cacheCreationInputTokens": 900, "webSearchRequests": 0,
                                                "costUSD": 0.00048, "contextWindow": 200000}},
            "permission_denials": [{"tool_name": "Write", "tool_use_id": "toolu_01",
                                    "tool_input": {"file_path": f"/work/{PATH_M}.txt", "content": TOOLIN_M}}],
            "uuid": "5c9f7a51-0000-4000-8000-000000000001"}


def codex_events(final: str = "Done: wrote sandbox-ok-codex") -> list[dict[str, Any]]:
    return [
        {"type": "thread.started", "thread_id": "019a2b3c-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": f"**Plan** {REASON_M}"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": f"Reading {EARLY_M}"}},
        {"type": "item.started", "item": {"id": "item_2", "type": "command_execution",
                                          "command": f"bash -lc 'cat {TOOLIN_M}'", "aggregated_output": "",
                                          "exit_code": None, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "command_execution",
                                            "command": f"bash -lc 'cat {TOOLIN_M}'", "aggregated_output": FILE_M,
                                            "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_3", "type": "file_change",
                                            "changes": [{"path": f"/work/{PATH_M}.txt", "kind": "add"}],
                                            "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_4", "type": "mcp_tool_call", "server": "docs", "tool": "search",
                                            "arguments": {"q": TOOLIN_M}, "result": None,
                                            "error": {"message": FILE_M}, "status": "failed"}},
        {"type": "item.completed", "item": {"id": "item_5", "type": "web_search", "query": TOOLIN_M,
                                            "action": {"type": "search"}}},
        {"type": "item.started", "item": {"id": "item_6", "type": "command_execution", "command": TOOLIN_M,
                                          "aggregated_output": "", "exit_code": None, "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "item_7", "type": "agent_message", "text": final}},
        {"type": "turn.completed", "usage": {"input_tokens": 25000, "cached_input_tokens": 12500,
                                             "cache_write_input_tokens": 0, "output_tokens": 73,
                                             "reasoning_output_tokens": 20}},
    ]


def codex_text(final: str = "Done: wrote sandbox-ok-codex") -> str:
    return "Reading prompt from stdin...\n" + "\n".join(json.dumps(e) for e in codex_events(final)) + "\n"


def docker_argv(*command: str) -> list[str]:
    return ["docker", "run", "--rm", "--name", "rsi-w-exp-20261017-01-A-1a2b3c", "--read-only", "--memory", "2g",
            "--label", "rsi.arm=A", "--mount", f"type=bind,src=/Users/x/sandbox/experiments/{EXP}/runs/{PATH_M},dst=/work",
            "-e", "ANTHROPIC_API_KEY", "sha256:0123abcd", *command]


CLAUDE_ARGV = docker_argv("claude", "-p", f"Write the status word. {PROMPT_M}", "--permission-mode", "dontAsk",
                          "--output-format", "json", "--model", "claude-haiku-5-5")
CODEX_ARGV = docker_argv("codex", "-a", "never", "exec", "--json", "-m", "gpt-6-luna", "-s", "danger-full-access",
                         f"Write the status word. {PROMPT_M}")


def write_run(root: Path, *, exp: str = EXP, arm: str = "A", run_id: str | None = None, stdout: str = "",
              argv: list[str] | None = None, sidecar: dict[str, Any] | None = None,
              report: dict[str, Any] | None = None, exit_code: int = 0, duration: float = 2.8) -> Path:
    exp_dir = root / "experiments" / exp
    logs = exp_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    run_id = run_id or f"{arm}-20261016T101500Z-1a2b3c"
    meta = {"experiment": exp, "arm": arm, "run_id": run_id, "container": "rsi-w-x", "exit_code": exit_code,
            "timed_out": False, "duration_s": duration, "stdout": "", "stderr": "",
            "run_dir": str(exp_dir / "runs" / PATH_M), "log_dir": str(logs), "argv": argv or CLAUDE_ARGV}
    (logs / f"{run_id}.json").write_text(json.dumps(meta, indent=2) + "\n")
    (logs / f"{run_id}.stdout").write_text(stdout)
    (logs / f"{run_id}.stderr").write_text(f"warning: {STDERR_M} {SECRET}\n")
    if sidecar is not None:
        (logs / f"{run_id}.rsi.json").write_text(json.dumps(sidecar))
    if report is not None:
        (exp_dir / "sanitize-report.json").write_text(json.dumps(report))
    return logs / f"{run_id}.json"


def base_run(**kw: Any) -> L.SandboxRun:
    d: dict[str, Any] = {"experiment_id": EXP, "arm": "A", "case": "a-split", "rep": 0,
                         "run_id": "A-20261016T101500Z-1a2b3c",
                         "started_at": datetime(2026, 10, 16, 10, 15, tzinfo=UTC), "duration_s": 2.8, "agent": "claude",
                         "exit_code": 0, "result": "ok",
                         "generations": [L.Generation("claude-haiku-5-5", {"input": 1, "output": 2, "total": 3}, 0.001)],
                         "tools": [L.ToolCall("Write", "completed")], "check_passed": True}
    return L.SandboxRun(**{**d, **kw})


# ---------------------------------------------------------------- a Langfuse v4 stand-in


def _unwrap(v: dict[str, Any]) -> Any:
    if "arrayValue" in v:
        return [_unwrap(x) for x in v["arrayValue"].get("values", [])]
    for k in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if k in v:
            return v[k]
    return None


def _ns_iso(ns: str) -> str:
    return datetime.fromtimestamp(int(ns) / 1e9, UTC).isoformat().replace("+00:00", "Z")


class FakeLangfuse:
    """The v4 endpoints the adapter uses. Observations are appended, never merged (v4 "does not reliably
    deduplicate repeated records"), so a duplicate export shows up as a duplicate row. Scores (POST
    /api/public/scores, an array) update in place on the same id and timestamp, as the Cloud spec says;
    unknown fields are rejected in a 207. Observation reads honor the start-time window, and
    experiment-items (fromStartTime required) lists the root spans that carry langfuse.experiment.*."""

    def __init__(self, svc: MockService):
        self.svc = svc
        self.observations: list[dict[str, Any]] = []
        self.scores: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.datasets: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.auth = "Basic " + base64.b64encode(f"{PK}:{SK}".encode()).decode()
        r = svc.route
        r("GET", L.HEALTH_PATH, self._guard(lambda q: (200, {"data": [{"id": "proj-1", "name": "rsi"}]})))
        r("POST", L.OTLP_PATH, self._guard(self._otlp))
        r("POST", L.SCORES_WRITE_PATH, self._guard(self._create_scores))
        r("GET", L.EXPERIMENT_ITEMS_PATH, self._guard(self._experiment_items))
        r("GET", L.OBSERVATIONS_PATH, self._guard(self._list_obs))
        r("GET", L.SCORES_PATH, self._guard(self._list_scores))
        r("POST", L.DATASETS_PATH, self._guard(self._create_ds))
        r("GET", L.DATASETS_PATH + "/", self._guard(self._get_ds))
        r("POST", L.DATASET_ITEMS_PATH, self._guard(self._item))

    def _guard(self, h: Any) -> Any:
        def inner(req: Request) -> Any:
            hdrs = {k.lower(): v for k, v in req.headers.items()}
            if hdrs.get("authorization") != self.auth:
                return 401, {"message": "Invalid credentials"}
            return h(req)
        return inner

    def _otlp(self, req: Request) -> Any:
        hdrs = {k.lower(): v for k, v in req.headers.items()}
        assert hdrs.get("content-type") == "application/json" and hdrs.get("x-langfuse-ingestion-version") == "4"
        for rs in req.json["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                for s in ss["spans"]:
                    self.observations.append(self._obs(s))
        return 200, {}

    @staticmethod
    def _obs(s: dict[str, Any]) -> dict[str, Any]:
        a = {x["key"]: _unwrap(x["value"]) for x in s.get("attributes", [])}
        pre = "langfuse.observation.metadata."
        cost = json.loads(a["langfuse.observation.cost_details"]) if "langfuse.observation.cost_details" in a else {}
        code = (s.get("status") or {}).get("code")
        return {"id": s["spanId"], "traceId": s["traceId"], "parentObservationId": s.get("parentSpanId"),
                "type": str(a.get("langfuse.observation.type", "span")).upper(), "name": s.get("name"),
                "startTime": _ns_iso(s["startTimeUnixNano"]), "endTime": _ns_iso(s["endTimeUnixNano"]),
                "level": a.get("langfuse.observation.level") or ("ERROR" if code == 2 else "DEFAULT"),
                "metadata": {k[len(pre):]: v for k, v in a.items() if k.startswith(pre)},
                "model": a.get("langfuse.observation.model.name"),
                "usageDetails": json.loads(a.get("langfuse.observation.usage_details") or "{}"),
                "costDetails": cost, "totalCost": cost.get("total"), "sessionId": a.get("langfuse.session.id"),
                "userId": a.get("langfuse.user.id"), "tags": a.get("langfuse.trace.tags") or [],
                "environment": a.get("langfuse.environment"), "traceName": a.get("langfuse.trace.name"),
                "output": a.get("langfuse.observation.output"), "attributes": a}

    def _list_obs(self, req: Request) -> Any:
        rows = list(reversed(self.observations))                  # newest first, like the real API
        for p, k in (("traceId", "traceId"), ("sessionId", "sessionId")):
            if req.q(p):
                rows = [o for o in rows if o[k] == req.q(p)]
        if req.q("isRootObservation") == "true":
            rows = [o for o in rows if not o["parentObservationId"]]
        if req.q("fromStartTime"):
            rows = [o for o in rows if o["startTime"] >= req.q("fromStartTime")]
        if req.q("toStartTime"):
            rows = [o for o in rows if o["startTime"] < req.q("toStartTime")]
        start, limit = int(req.q("cursor") or 0), int(req.q("limit") or 50)
        page = rows[start:start + limit]
        nxt = str(start + limit) if start + limit < len(rows) else None
        return 200, {"data": page, "meta": {"cursor": nxt}}

    SCORE_FIELDS = {"id", "traceId", "sessionId", "observationId", "datasetRunId", "name", "value", "comment",
                    "metadata", "environment", "queueId", "dataType", "configId", "source", "timestamp"}

    def _create_scores(self, req: Request) -> Any:
        assert isinstance(req.json, list), "the scores endpoint takes an array"
        errors = []
        for b in req.json:
            if set(b) - self.SCORE_FIELDS or not {"name", "value"} <= set(b):
                errors.append({"message": f"invalid score {b.get('id')}"})
                continue
            self.scores[(b["id"], b["name"], b["timestamp"])] = b
        if errors:
            return 207, {"accepted": len(req.json) - len(errors), "rejected": len(errors), "errors": errors}
        return 202, {"message": "Accepted"}

    def _experiment_items(self, req: Request) -> Any:
        if not req.q("fromStartTime"):
            return 400, {"message": "fromStartTime is required"}
        items = []
        for o in reversed(self.observations):
            a = o["attributes"]
            if o["parentObservationId"] or "langfuse.experiment.id" not in a or o["startTime"] < req.q("fromStartTime"):
                continue
            if req.q("datasetId") and a.get("langfuse.experiment.dataset.id") != req.q("datasetId"):
                continue
            scores = [{"id": s["id"], "name": s["name"], "dataType": s["dataType"],
                       "value": bool(s["value"]) if s["dataType"] == "BOOLEAN" else s["value"]}
                      for s in self.scores.values() if s["traceId"] == o["traceId"]]
            items.append({"id": o["id"], "traceId": o["traceId"], "startTime": o["startTime"], "endTime": o["endTime"],
                          "level": o["level"], "environment": o["environment"],
                          "experimentId": a["langfuse.experiment.id"], "experimentName": a["langfuse.experiment.name"],
                          "experimentItemId": a["langfuse.experiment.item.id"],
                          **({"scores": scores} if "scores" in (req.q("fields") or "") else {})})
        return 200, {"data": items, "meta": {"cursor": None}}

    def _list_scores(self, req: Request) -> Any:
        rows = [s for s in self.scores.values() if not req.q("traceId") or s["traceId"] == req.q("traceId")]
        data = [{"id": s["id"], "traceId": s["traceId"], "name": s["name"], "dataType": s["dataType"],
                 "value": bool(s["value"]) if s["dataType"] == "BOOLEAN" else s["value"], "timestamp": s["timestamp"]}
                for s in rows]
        return 200, {"data": data, "meta": {"limit": int(req.q("limit") or 50), "cursor": None}}

    def _create_ds(self, req: Request) -> Any:
        name = req.json["name"]
        ds = self.datasets.setdefault(name, {"id": "ds-" + hashlib.sha256(name.encode()).hexdigest()[:12], "name": name})
        return 200, ds

    def _get_ds(self, req: Request) -> Any:
        ds = self.datasets.get(unquote(req.path.rsplit("/", 1)[1]))
        return (200, ds) if ds else (404, {"message": "Dataset not found"})

    def _item(self, req: Request) -> Any:
        self.items[req.json["id"]] = req.json
        return 200, {**req.json, "status": "ACTIVE"}

    def bodies(self) -> str:
        return "\n".join(r.body.decode("utf-8", "replace") for r in self.svc.requests)


def client_for(svc: MockService, secret: str = SK) -> L.LangfuseClient:
    return L.LangfuseClient(svc.url, PK, secret, sleep=lambda s: None)


def _spans(body: dict[str, Any]) -> list[dict[str, Any]]:
    return body["resourceSpans"][0]["scopeSpans"][0]["spans"]


# ---------------------------------------------------------------- parsers and the loader


def test_parse_claude_json_keeps_numbers_and_names_only() -> None:
    out = L.parse_claude_json(json.dumps(claude_result()))
    assert (out.agent, out.num_turns, out.is_error) == ("claude", 2, False)
    (g,) = out.generations
    assert g.model == "claude-haiku-5-5" and g.cost_usd == 0.00048
    assert g.usage == {"input": 12, "output": 40, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 900,
                       "total": 3952}
    assert out.tools == [L.ToolCall("Write", "denied")]                  # a denial is a tool call: name + status
    assert out.result == "Wrote sandbox-ok-claude to /work/status.txt"
    assert not any(m in repr(out) for m in MARKERS)
    # No modelUsage: the top-level usage and cost, the model from the --model flag. A noise line first.
    obj = {k: v for k, v in claude_result().items() if k != "modelUsage"} | {"subtype": "error_max_turns"}
    out2 = L.parse_claude_json("npm notice\n" + json.dumps(obj), model="claude-haiku-5-5")
    assert out2.is_error and out2.generations[0].model == "claude-haiku-5-5" and out2.generations[0].cost_usd == 0.00048
    with pytest.raises(ValueError):
        L.parse_claude_json('{"type": "assistant"}')


def test_parse_codex_jsonl_keeps_tool_names_statuses_usage_and_the_last_message() -> None:
    out = L.parse_codex_jsonl(codex_text(), model="gpt-6-luna")
    assert out.agent == "codex" and out.num_turns == 1 and not out.is_error
    assert [(t.name, t.status) for t in out.tools] == [
        ("command_execution", "completed"), ("file_change", "completed"), ("mcp:docs/search", "failed"),
        ("web_search", "completed"), ("command_execution", "incomplete")]
    (g,) = out.generations
    assert g.model == "gpt-6-luna" and g.cost_usd is None
    assert g.usage == {"input": 12500, "output": 73, "input_cached_tokens": 12500, "total": 25073}
    assert g.extra == {"reasoning_output_tokens": 20}                    # not priced twice: kept out of usage
    assert out.result == "Done: wrote sandbox-ok-codex"
    assert not any(m in repr(out) for m in MARKERS)
    failed = L.parse_codex_jsonl(codex_text() + json.dumps({"type": "turn.failed", "error": {"message": "401"}}))
    assert failed.is_error and failed.generations[0].model == "unknown"
    with pytest.raises(ValueError):
        L.parse_codex_jsonl("Reading prompt from stdin...\n")


def test_load_run_log_reads_the_sandbox_files(tmp_path: Path) -> None:
    path = write_run(tmp_path, arm="B", run_id="B-20261016T101500Z-1a2b3c", stdout=codex_text(), argv=CODEX_ARGV,
                     sidecar={"case": "a-split", "rep": 1, "eval_verdict": "improves"},
                     report={"A": {"baseline": "bl-0123456789ab", "artifact_hash": None},
                             "B": {"baseline": "bl-0123456789ab", "artifact_hash": ART}}, duration=6.6)
    run = L.load_run_log(path)
    assert (run.experiment_id, run.arm, run.case, run.rep, run.agent) == (EXP, "B", "a-split", 1, "codex")
    assert run.generations[0].model == "gpt-6-luna"                   # from `-m` in the argv
    assert run.started_at == datetime(2026, 10, 16, 10, 15, tzinfo=UTC)
    assert run.ended_at == run.started_at + timedelta(seconds=6.6)
    assert (run.baseline_id, run.artifact_hash, run.eval_verdict, run.env) == ("bl-0123456789ab", ART, "improves", "sandbox")
    assert run.run_key == f"{EXP}/B/a-split/1" and run.trace_id == refs.trace_id(run.run_key)
    plain = L.load_run_log(write_run(tmp_path, arm="A", run_id="A-custom-run", stdout=json.dumps(claude_result())))
    assert (plain.case, plain.rep, plain.agent, plain.baseline_id) == ("A-custom-run", 0, "claude", "bl-0123456789ab")
    assert plain.started_at < datetime.now(UTC)                       # no timestamp in the run id: file mtime
    with pytest.raises(ValueError):
        L.load_run_log(path.with_name("B-20261016T101500Z-1a2b3c.rsi.json"))   # a sidecar is not a run log


# ---------------------------------------------------------------- ids, guard, text policy


def test_ids_are_deterministic_and_uploads_never_duplicate(tmp_path: Path) -> None:
    path = write_run(tmp_path, stdout=json.dumps(claude_result()),
                     sidecar={"case": "a-split", "rep": 0, "check_passed": True, "judge_winner": "B"})
    run, again = L.load_run_log(path), L.load_run_log(path)
    assert L.build_spans(run) == L.build_spans(again) and L.build_scores(run) == L.build_scores(again)
    assert run.trace_id == refs.trace_id(f"{EXP}/A/a-split/0") and run.session_id == refs.session_uuid(run.run_key)
    spans = _spans(L.build_spans(run))
    assert len({s["spanId"] for s in spans}) == len(spans) == 3            # root, one generation, one tool
    assert all(s["traceId"] == run.trace_id for s in spans)
    root = spans[0]["spanId"]
    assert "parentSpanId" not in spans[0] and all(s["parentSpanId"] == root for s in spans[1:])
    with MockService() as svc:
        lf = FakeLangfuse(svc)
        first, second = L.upload_run(run, client_for(svc)), L.upload_run(again, client_for(svc))
        assert (first.spans, first.existed, first.scores) == (3, False, 2)
        assert (second.spans, second.existed, second.scores) == (0, True, 2)   # spans once, scores upsert
        ids = [o["id"] for o in lf.observations]
        assert sorted(ids) == sorted(s["spanId"] for s in spans)
        assert sorted(k[0] for k in lf.scores) == [f"{run.trace_id}-check_passed", f"{run.trace_id}-judge_winner"]
        a, b = svc.requests_to("POST", L.SCORES_WRITE_PATH)
        assert a.body == b.body and len(svc.requests_to("POST", L.OTLP_PATH)) == 1


@pytest.mark.parametrize("change, reason", [
    ({"env": "prod"}, "not a sandbox record"), ({"env": "eval"}, "not a sandbox record"),
    ({"experiment_id": ""}, "experiment id"), ({"experiment_id": "prod-session-1"}, "experiment id"),
    ({"kind": "chat"}, "kind"), ({"rep": -1}, "bad case or rep"), ({"arm": "AB"}, "arm"),
    ({"judge_winner": "maybe"}, "judge_winner"), ({"eval_verdict": "great"}, "eval_verdict"),
    ({"run_id": "A run/../x"}, "bad run id"), ({"check_passed": "yes"}, "check_passed"),
])
def test_guard_refuses_anything_but_a_sandbox_record(change: dict[str, Any], reason: str) -> None:
    run = dataclasses.replace(base_run(), **change)
    with MockService() as svc:
        FakeLangfuse(svc)
        with pytest.raises(L.Refused, match=reason):
            L.upload_run(run, client_for(svc))
        assert svc.requests == []                                         # refused before any request


def test_leak_no_prompt_tool_input_path_file_or_secret_leaves(tmp_path: Path) -> None:
    long_result = f"Wrote the file. key={SECRET} and {SECRET} " + "x" * 1000
    runs = [L.load_run_log(write_run(tmp_path, arm="A", stdout=json.dumps(claude_result(long_result)),
                                     sidecar={"case": "c1", "check_passed": True})),
            L.load_run_log(write_run(tmp_path, arm="B", stdout=codex_text(final=f"done {SECRET} " + "y" * 900),
                                     argv=CODEX_ARGV, sidecar={"case": "c1", "check_passed": False}))]
    # Ids that are not id-shaped never ride along in metadata.
    runs.append(dataclasses.replace(runs[0], case="c2", baseline_id=f"bl {PROMPT_M}", artifact_hash=FILE_M,
                                    proposal_id=EARLY_M))
    with MockService() as svc:
        lf = FakeLangfuse(svc)
        for run in runs:
            L.upload_run(run, client_for(svc), datasets=True)
        sent = lf.bodies()
        assert len(svc.requests_to("POST", L.OTLP_PATH)) == 3 and sent
        for m in MARKERS:
            assert m not in sent, m
        assert all(o["output"] is None for o in lf.observations)          # no agent text by default
        assert "Wrote the file" not in sent and "x" * 50 not in sent and "y" * 50 not in sent
    for run in runs:                                                      # nor into rsi_evidence
        ev = json.dumps(L.evidence_for(run).to_json())
        assert not any(m in ev for m in MARKERS) and "x" * 50 not in ev


# Values that must never reach Langfuse through any field. Each is shaped to pass the old name/id patterns.
SENSITIVE = {
    "abs-path": "Users/vilmer/.ssh/id_rsa",
    "work-path": f"work/{PATH_M}.txt",
    "deep-path": "src/billing/stripe_keys.py",
    "anthropic-key": "sk-ant-api03-Zx9Zx9Zx9Zx9Zx9Zx9",
    "langfuse-key": "sk-lf-0b3e7d2a-8c1f-4f6e-9a2b-1c3d4e5f6a7b",
    "github-token": "ghp_Zx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9Zx9",
    "aws-key-id": "AKIAZX9ZX9ZX9ZX9ZX9Z",
    "email": "vilmer@example.com",
    "token-run": "Zx9" * 12,
}


def test_final_result_text_stays_local_by_default(tmp_path: Path) -> None:
    """The agent's final response may quote paths, keys, emails or task text: none of it leaves by default,
    neither through upload_run nor through the sink (which has no opt-in)."""
    text = (f"Done. Edited /Users/vilmer/Projects/app/{PATH_M}.py, used {SENSITIVE['anthropic-key']}, "
            f"mailed {SENSITIVE['email']}. FINAL-PROSE-MARKER-3e1f")
    p = write_run(tmp_path, arm="A", stdout=json.dumps(claude_result(text)), sidecar={"case": "c1", "check_passed": True})
    run = L.load_run_log(p)
    assert run.result and "FINAL-PROSE-MARKER-3e1f" in run.result                # parsed and kept locally
    assert "langfuse.observation.output" not in json.dumps(L.build_spans(run))
    pieces = ("FINAL-PROSE-MARKER-3e1f", "/Users/vilmer", PATH_M, SENSITIVE["anthropic-key"], SENSITIVE["email"],
              "Done. Edited")
    with MockService() as svc:                                            # upload_run, as a caller would
        lf = FakeLangfuse(svc)
        L.upload_run(run, client_for(svc), datasets=True)
        assert lf.observations and all(o["output"] is None for o in lf.observations)
        assert not [p for p in pieces if p in lf.bodies()]
    with MockService() as svc:                                            # the sink, as `amem sync run` does
        lf = FakeLangfuse(svc)
        sink = L.LangfuseSink(client_for(svc), tmp_path)
        for batch in sink.batches(_NoSupabase(), {}, replay=True):
            sink.push(batch)
        assert [u.spans for u in sink.uploads] == [3]
        assert all(o["output"] is None for o in lf.observations)
        assert not [p for p in pieces if p in lf.bodies()]


def test_opt_in_result_is_still_redacted_capped_and_root_only(tmp_path: Path) -> None:
    long_result = f"Wrote the file. key={SECRET} and {SECRET} " + "x" * 1000
    run = L.load_run_log(write_run(tmp_path, arm="A", stdout=json.dumps(claude_result(long_result)),
                                   sidecar={"case": "c1", "check_passed": True}))
    with MockService() as svc:
        lf = FakeLangfuse(svc)
        L.upload_run(run, client_for(svc), include_result=True)
        outputs = [o["output"] for o in lf.observations if o["output"] is not None]
        assert len(outputs) == 1 and len(outputs[0]) <= L.RESULT_MAX and "[redacted]" in outputs[0]
        assert SECRET not in lf.bodies()
        assert all(o["output"] is None for o in lf.observations if o["parentObservationId"])


class _NoSupabase:
    """The lookups LangfuseSink.batches makes: no session row, no earlier lf:trace link, no team repo."""

    def execute(self, *_: Any) -> Any:
        return self

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []


def _inject(where: str, value: str) -> L.SandboxRun:
    run = base_run(judge_winner="B", eval_verdict="worse")
    gen = run.generations[0]
    return {
        "model": lambda: dataclasses.replace(run, generations=[dataclasses.replace(gen, model=value)]),
        "tool": lambda: dataclasses.replace(run, tools=[L.ToolCall(value, "completed")]),
        "tool-status": lambda: dataclasses.replace(run, tools=[L.ToolCall("Write", value)]),
        "agent": lambda: dataclasses.replace(run, agent=value),
        "usage-key": lambda: dataclasses.replace(run, generations=[dataclasses.replace(gen, usage={value: 1, "total": 1})]),
        "extra-key": lambda: dataclasses.replace(run, generations=[dataclasses.replace(gen, extra={value: 1})]),
        "baseline": lambda: dataclasses.replace(run, baseline_id=value),
    }[where]()


@pytest.mark.parametrize("value", list(SENSITIVE.values()), ids=list(SENSITIVE))
@pytest.mark.parametrize("where", ["model", "tool", "tool-status", "agent", "usage-key", "extra-key", "baseline"])
def test_sensitive_text_never_leaves_through_a_trace_field(where: str, value: str) -> None:
    run = _inject(where, value)
    with MockService() as svc:
        lf = FakeLangfuse(svc)
        res = L.upload_run(run, client_for(svc), datasets=True)
        sent = lf.bodies()
        assert res.spans == 3 and lf.observations                         # the run still goes, minus the value
        assert value not in sent and unquote(value) not in unquote(sent)
        for part in value.split("/"):
            if len(part) > 8:
                assert part not in sent, part


@pytest.mark.parametrize("change, reason", [
    ({"experiment_id": "exp-20261017-sk-ant-api03Zx9Zx9Zx9"}, "experiment id looks sensitive"),
    ({"experiment_id": "exp-20261017-" + "Zx9" * 12}, "experiment id looks sensitive"),
    ({"case": SENSITIVE["anthropic-key"]}, "case looks sensitive"),
    ({"case": SENSITIVE["github-token"]}, "case looks sensitive"),
    ({"case": SENSITIVE["aws-key-id"]}, "case looks sensitive"),
    ({"run_id": "A-20261016T101500Z-sk-ant-api03Zx9Zx9Zx9"}, "run id looks sensitive"),
    ({"exit_code": f"exit /Users/vilmer/{PATH_M}"}, "exit_code is an integer"),
    ({"num_turns": SENSITIVE["email"]}, "num_turns is an integer"),
    ({"duration_s": "2.8s /work/x"}, "duration_s is a number"),
])
def test_a_run_whose_identity_or_numbers_carry_text_is_refused(change: dict[str, Any], reason: str) -> None:
    run = dataclasses.replace(base_run(), **change)
    with MockService() as svc:
        FakeLangfuse(svc)
        with pytest.raises(L.Refused, match=reason) as refused:
            L.upload_run(run, client_for(svc), datasets=True)
        assert svc.requests == []
    assert all(str(v) not in str(refused.value) for v in change.values())    # the refusal never echoes it


def test_sensitive_names_from_real_agent_output_never_leave(tmp_path: Path) -> None:
    """The same values arriving the way they really would: Claude modelUsage keys and denied tool names,
    a Codex MCP server/tool name, and a --model flag in the argv."""
    claude = claude_result("ok")
    claude["modelUsage"] = {SENSITIVE["anthropic-key"]: claude["modelUsage"]["claude-haiku-5-5"],
                            SENSITIVE["email"]: claude["modelUsage"]["claude-haiku-5-5"]}
    claude["permission_denials"] = [{"tool_name": SENSITIVE["abs-path"], "tool_input": {}},
                                    {"tool_name": SENSITIVE["github-token"], "tool_input": {}}]
    pa = write_run(tmp_path, arm="A", stdout=json.dumps(claude), sidecar={"case": "c1"})
    events = codex_events() + [{"type": "item.completed", "item": {
        "id": "item_9", "type": "mcp_tool_call", "server": "sk-ant-api03Zx9Zx9Zx9Zx9", "tool": "search",
        "arguments": {}, "result": None, "status": "completed"}}]
    codex = "\n".join(json.dumps(e) for e in events) + "\n"
    argv = [a if a != "gpt-6-luna" else "AKIAZX9ZX9ZX9ZX9ZX9Z" for a in CODEX_ARGV]
    pb = write_run(tmp_path, arm="B", stdout=codex, argv=argv, sidecar={"case": "c1"})
    runs = [L.load_run_log(pa), L.load_run_log(pb)]
    with MockService() as svc:
        lf = FakeLangfuse(svc)
        for run in runs:
            L.upload_run(run, client_for(svc))
        sent = lf.bodies()
        assert len(lf.observations) == sum(1 + len(r.generations) + len(r.tools) for r in runs)
        for v in (SENSITIVE["anthropic-key"], SENSITIVE["email"], SENSITIVE["abs-path"], SENSITIVE["github-token"],
                  "sk-ant-api03Zx9Zx9Zx9Zx9", "AKIAZX9ZX9ZX9ZX9ZX9Z", *MARKERS):
            assert v not in sent, v
        assert L.REDACTED in {o["name"] for o in lf.observations}


@pytest.mark.parametrize("name", ["claude-haiku-5-5", "gpt-6-luna", "us.anthropic.claude-opus-5-5", "openai/gpt-6",
                                  "claude-3@20240229", "Write", "mcp__docs__search", "mcp:docs/search",
                                  "collab:spawn", "command_execution", "web_search"])
def test_ordinary_model_and_tool_names_still_pass(name: str) -> None:
    assert not L.sensitive(name) and L._out_name(name) == name


# ---------------------------------------------------------------- round trip and datasets


def test_round_trip_trace_query_returns_the_amem_session_and_scores(tmp_path: Path) -> None:
    a = L.load_run_log(write_run(tmp_path, arm="A", run_id="A-20261016T101500Z-aaaaaa", stdout=json.dumps(claude_result()),
                                 sidecar={"case": "a-split", "check_passed": True, "judge_winner": "B"},
                                 report={"A": {"baseline": "bl-0123456789ab"}, "B": {"baseline": "bl-0123456789ab",
                                                                                   "artifact_hash": ART}}))
    a2 = L.load_run_log(write_run(tmp_path, arm="A", run_id="A-20261016T102000Z-cccccc", stdout=json.dumps(claude_result()),
                                  sidecar={"case": "b-split", "check_passed": True, "judge_winner": "tie"}))
    b = L.load_run_log(write_run(tmp_path, arm="B", run_id="B-20261016T101500Z-bbbbbb", stdout=codex_text(),
                                 argv=CODEX_ARGV, exit_code=1,
                                 sidecar={"case": "a-split", "check_passed": False, "eval_verdict": "worse"}))
    with MockService() as svc:
        FakeLangfuse(svc)
        client = client_for(svc)
        for run in (a, a2, b):
            L.upload_run(run, client)
        r = L.LangfuseRetriever(client, now=lambda: NOW)
        (res,) = E.gather("trace", {"trace_id": a.trace_id}, [r])
        assert res.status == "ok"
        (ev,) = res.refs
        assert ev.ref == f"lf:trace/{a.trace_id}" and ev.kind == "trace" and ev.source == "lf"
        assert ev.metrics["amem_session_id"] == a.session_id == refs.session_uuid(f"{EXP}/A/a-split/0")
        assert ev.subject_refs == [f"amem:experiment/{EXP}"]
        m = ev.metrics
        assert (m["tokens_total"], m["tokens_output"], m["cost_usd"], m["generations"], m["tools"]) == \
            (3952, 40, 0.00048, 1, 1)
        assert (m["check_passed"], m["judge_winner"], m["arm"], m["tags"], m["level"]) == \
            (True, "B", "A", ["A", "sandbox"], "DEFAULT")
        (evb,) = E.gather("trace", {"trace_id": b.trace_id}, [r])[0].refs
        assert evb.subject_refs == [f"amem:experiment/{EXP}", f"amem:artifact/{ART}"]
        assert (evb.metrics["level"], evb.metrics["tool_errors"], evb.metrics["eval_verdict"]) == ("ERROR", 2, "worse")
        assert E.gather("trace", {"trace_id": "0" * 32}, [r])[0].refs == []
        assert E.gather("trace", {"trace_id": "not-hex"}, [r])[0].status == "bad-params"
        (agg,) = E.gather("experiment_scores", {"experiment_id": EXP}, [r])
        assert agg.status == "ok" and [x.ref for x in agg.refs] == [f"lf:dataset_run/{EXP}-A", f"lf:dataset_run/{EXP}-B"]
        ma, mb = (x.metrics for x in agg.refs)
        assert (ma["traces"], ma["check_passed_rate"], ma["judge_winner"]) == (2, 1.0, {"B": 1, "tie": 1})
        assert (mb["traces"], mb["check_passed_rate"], mb["eval_verdict"]) == (1, 0.0, {"worse": 1})
        assert all(x.subject_refs == [f"amem:experiment/{EXP}"] for x in agg.refs)


def test_datasets_link_one_experiment_run_per_arm(tmp_path: Path) -> None:
    runs = [L.load_run_log(write_run(tmp_path, arm=arm, run_id=f"{arm}-20261016T10150{rep}Z-1a2b3c",
                                     stdout=json.dumps(claude_result()), sidecar={"case": "c1", "rep": rep}))
            for arm, rep in (("A", 0), ("B", 0), ("A", 1))]
    with MockService() as svc:
        lf = FakeLangfuse(svc)
        for run in runs:
            L.upload_run(run, client_for(svc), datasets=True)
        L.upload_run(runs[0], client_for(svc), datasets=True)             # a retry
        assert list(lf.datasets) == [f"rsi-{EXP}"] and len(svc.requests_to("POST", L.DATASETS_PATH)) == 1
        assert list(lf.items) == [L.dataset_item_id(EXP, "c1")]
        ds = lf.datasets[f"rsi-{EXP}"]["id"]
        for run, exp_run in ((runs[0], f"{EXP}-A"), (runs[1], f"{EXP}-B")):
            obs = [o for o in lf.observations if o["traceId"] == run.trace_id]
            root = next(o["id"] for o in obs if not o["parentObservationId"])
            want = {"langfuse.experiment.id": exp_run, "langfuse.experiment.name": exp_run,
                    "langfuse.experiment.dataset.id": ds, "langfuse.experiment.item.id": L.dataset_item_id(EXP, "c1"),
                    "langfuse.experiment.item.root_observation_id": root}
            assert len(obs) == 3 and all({k: o["attributes"].get(k) for k in want} == want for o in obs)
        rep1 = [o for o in lf.observations if o["traceId"] == runs[2].trace_id]
        assert rep1 and not any("langfuse.experiment.id" in o["attributes"] for o in rep1)   # no repetitions in a run


def test_rejections_and_missing_config_raise() -> None:
    run = base_run()
    with pytest.raises(L.LangfuseError, match="not configured"):
        L.upload_run(run, L.LangfuseClient.from_env({}))
    with MockService() as svc:
        svc.route("GET", L.OBSERVATIONS_PATH, lambda r: (200, {"data": [], "meta": {"cursor": None}}))
        svc.route("POST", L.OTLP_PATH, lambda r: (200, {"partialSuccess": {"rejectedSpans": 2, "errorMessage": "bad"}}))
        with pytest.raises(L.LangfuseError, match="rejected 2 span"):
            L.upload_run(run, client_for(svc))
        svc.route("POST", L.OTLP_PATH, lambda r: (200, {}))
        svc.route("POST", L.SCORES_WRITE_PATH, lambda r: (207, {"accepted": 0, "rejected": 1,
                                                                 "errors": [{"message": "bad value"}]}))
        with pytest.raises(L.Rejected, match="scores rejected 1 of 1: bad value"):
            L.upload_run(run, client_for(svc))


# ---------------------------------------------------------------- health and the retriever when down


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_retriever_unavailable_when_unconfigured_wrong_or_down() -> None:
    (res,) = E.gather("trace", {"trace_id": "0" * 32}, [L.LangfuseRetriever(L.LangfuseClient.from_env({}))])
    assert (res.status, res.detail) == ("unavailable", "not configured")
    for host in ("http://example.com", "http://localhost.evil.test", "ftp://localhost", "https://u:p@lf.example.com"):
        c = L.LangfuseClient.from_env({"AMEM_LANGFUSE_HOST": host, "AMEM_LANGFUSE_PUBLIC_KEY": PK,
                                       "AMEM_LANGFUSE_SECRET_KEY": SK})
        assert not c.health().ok and "AMEM_LANGFUSE_HOST" in c.health().detail, host
    ok = L.LangfuseClient.from_env({"AMEM_LANGFUSE_HOST": "https://lf.example.com/", "AMEM_LANGFUSE_PUBLIC_KEY": PK,
                                    "AMEM_LANGFUSE_SECRET_KEY": SK})
    assert ok.problem is None and ok.host == "https://lf.example.com" and SK not in repr(ok)
    down = L.LangfuseClient(f"http://127.0.0.1:{_closed_port()}", PK, SK, sleep=lambda s: None)
    (res,) = E.gather("experiment_scores", {"experiment_id": EXP}, [L.LangfuseRetriever(down)])
    assert (res.status, res.detail) == ("unavailable", "unreachable")
    assert not L.LangfuseSink(down).health().ok
    with MockService() as svc:
        FakeLangfuse(svc)
        assert client_for(svc).health().ok
        assert client_for(svc, secret="sk-lf-wrong").health().detail == "HTTP 401"


# ---------------------------------------------------------------- the sink (amem_app, disposable Postgres)


def _clean(conn: psycopg.Connection) -> None:
    conn.execute("delete from rsi_checkpoints where job = 'sync:langfuse'")
    conn.execute("delete from rsi_evidence where system = 'langfuse'")
    conn.execute("delete from sessions where project_dir = '/lf-test'")


@pytest.fixture
def app(pg_url: str) -> Iterator[str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{APP_PW}'")
        _clean(conn)
    yield pg_url.replace("postgres:postgres@", f"amem_app:{APP_PW}@", 1)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        _clean(conn)


def _tree(root: Path) -> tuple[Path, Path]:
    report = {"A": {"baseline": "bl-0123456789ab", "artifact_hash": None},
              "B": {"baseline": "bl-0123456789ab", "artifact_hash": ART}}
    pa = write_run(root, arm="A", run_id="A-20261016T101500Z-aaaaaa", stdout=json.dumps(claude_result()),
                   sidecar={"case": "a-split", "rep": 0, "check_passed": True, "judge_winner": "B"}, report=report)
    pb = write_run(root, arm="B", run_id="B-20261016T101500Z-bbbbbb", stdout=codex_text(), argv=CODEX_ARGV,
                   sidecar={"case": "a-split", "rep": 0, "check_passed": False})
    write_run(root, exp="t-iso-1a2b3c", arm="A", run_id="A-20261016T101500Z-dddddd", stdout=json.dumps(claude_result()))
    return pa, pb


def test_sink_records_trace_evidence_and_is_idempotent(app: str, tmp_path: Path) -> None:
    pa, pb = _tree(tmp_path)
    a, b = L.load_run_log(pa), L.load_run_log(pb)
    with MockService() as svc, psycopg.connect(app) as conn:
        lf = FakeLangfuse(svc)
        sink = L.LangfuseSink(client_for(svc), tmp_path)
        rep = sync.run(sink, conn, environ={})
        assert (rep.status, rep.batches, rep.rows, rep.failed) == ("ok", 3, 2, 0)
        assert [r[0] for r in sink.refused] == ["t-iso-1a2b3c/A-20261016T101500Z-dddddd"]
        assert "experiment id" in sink.refused[0][1]
        rows = conn.execute("select external_ref, kind, subject_refs, metrics, summary from rsi_evidence "
                            "where system = 'langfuse' order by metrics->>'arm'").fetchall()
        assert [r[0] for r in rows] == [f"lf:trace/{a.trace_id}", f"lf:trace/{b.trace_id}"]
        (_, kind, subj_a, ma, summary), (_, _, subj_b, mb, _) = rows
        assert kind == "trace" and subj_a == [f"amem:experiment/{EXP}"] and subj_b[1] == f"amem:artifact/{ART}"
        assert (ma["tokens_total"], ma["cost_usd"], ma["check_passed"], ma["amem_session_id"]) == \
            (3952, 0.00048, True, a.session_id)
        assert (mb["tokens_total"], mb["tools"], mb["tool_errors"], mb["check_passed"]) == (25073, 5, 2, False)
        assert a.run_key in summary and not any(m in json.dumps([list(r) for r in rows]) for m in MARKERS)
        n_obs, n_scores = len(lf.observations), len(lf.scores)
        assert n_obs == 3 + 7 and n_scores == 3 and not any(m in lf.bodies() for m in MARKERS)
        mtime_ns, last = sync.load(conn, "langfuse")["watermarks"]["sandbox_runs"].split("|")
        assert len(mtime_ns) == 20 and last in {f"{EXP}/A-20261016T101500Z-aaaaaa", f"{EXP}/B-20261016T101500Z-bbbbbb",
                                                "t-iso-1a2b3c/A-20261016T101500Z-dddddd"}

        rep2 = sync.run(sink, conn, environ={})                          # nothing new
        assert (rep2.status, rep2.batches) == ("ok", 0)
        rep3 = sync.run(sink, conn, replay=True, environ={})            # backfill: same ids, nothing duplicated
        assert rep3.batches == 3 and len(lf.observations) == n_obs and len(lf.scores) == n_scores
        assert conn.execute("select count(*) from rsi_evidence where system = 'langfuse'").fetchone()[0] == 2

        side = pb.with_name("B-20261016T101500Z-bbbbbb.rsi.json")      # the judge writes scores later
        side.write_text(json.dumps({"case": "a-split", "rep": 0, "check_passed": False, "eval_verdict": "worse"}))
        later = time.time() + 30
        os.utime(side, (later, later))
        rep4 = sync.run(sink, conn, environ={})
        assert rep4.batches == 1 and len(lf.observations) == n_obs and len(lf.scores) == n_scores + 1
        assert sink.uploads[-1].existed and sink.uploads[-1].spans == 0
        (mb2,) = conn.execute("select metrics from rsi_evidence where external_ref = %s",
                              (f"lf:trace/{b.trace_id}",)).fetchone()
        assert mb2["eval_verdict"] == "worse"
        (st,) = [s for s in sync.status(conn) if s["sink"] == "langfuse"]
        assert st["status"] == "done" and st["tables"] == ["sandbox_runs"]


def test_sink_refuses_a_run_whose_session_supabase_marks_prod(app: str, pg_url: str, tmp_path: Path) -> None:
    pa, pb = _tree(tmp_path)
    a, b = L.load_run_log(pa), L.load_run_log(pb)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("insert into sessions (session_id, kind, project_dir, transcript_bytes, env) values "
                     "(%s, 'chat', '/lf-test', 1, 'prod')", (a.session_id,))
    with MockService() as svc, psycopg.connect(app) as conn:
        lf = FakeLangfuse(svc)
        sink = L.LangfuseSink(client_for(svc), tmp_path)
        rep = sync.run(sink, conn, environ={})
        assert rep.status == "ok" and len(sink.refused) == 2
        assert any("env=prod" in why for _, why in sink.refused)
        assert {o["traceId"] for o in lf.observations} == {b.trace_id}
        assert conn.execute("select count(*) from rsi_evidence where external_ref = %s",
                            (f"lf:trace/{a.trace_id}",)).fetchone()[0] == 0


def test_sink_down_moves_nothing(app: str, tmp_path: Path) -> None:
    _tree(tmp_path)
    down = L.LangfuseClient(f"http://127.0.0.1:{_closed_port()}", PK, SK, sleep=lambda s: None)
    with psycopg.connect(app) as conn:
        rep = sync.run(L.LangfuseSink(down, tmp_path), conn, environ={})
        assert (rep.status, rep.detail) == ("unavailable", "unreachable")
        assert "watermarks" not in sync.load(conn, "langfuse")
        assert sync.run(L.LangfuseSink(down, tmp_path), conn, environ={"AMEM_SYNC_LANGFUSE": "off"}).status == "off"


def test_egress_deny_terms_and_team_repos_are_sensitive(app: str, pg_url: str, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Mission 004C (F3): a term on AMEM_EGRESS_DENY, or a team repo the sink reads from Supabase, never leaves."""
    monkeypatch.setattr(egress, "_team", egress.NONE)                     # restored after the test
    monkeypatch.setenv(egress.DENY_ENV, "Acme-Secret")
    assert L.sensitive("acme-secret-model") and L._out_name("ACME-SECRET-tool") == L.REDACTED
    assert not L.sensitive("claude-haiku-5-5") and not L.sensitive("pipeline-poc-tuned")      # not read yet
    with pytest.raises(L.Refused, match="case looks sensitive"):
        L.upload_run(base_run(case="acme-secret-case"), L.LangfuseClient("http://127.0.0.1:1", PK, SK))
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("insert into sessions (session_id, kind, project_dir, repo, project, transcript_bytes, env, meta) "
                     "values ('lf-team', 'chat', '/lf-test', 'pipeline-poc', 'pipeline-poc', 1, 'prod', '{\"team\": true}')")
    claude = claude_result("ok")
    claude["modelUsage"] = {"pipeline-poc-tuned": claude["modelUsage"]["claude-haiku-5-5"]}
    claude["permission_denials"] = [{"tool_name": "acme-secret-tool", "tool_input": {}}]
    write_run(tmp_path, arm="A", stdout=json.dumps(claude), sidecar={"case": "c1"})
    write_run(tmp_path, arm="B", run_id="B-20261016T101500Z-bbbbbb", stdout=json.dumps(claude_result()),
              sidecar={"case": "pipeline-poc-case"})
    with MockService() as svc, psycopg.connect(app) as conn:
        lf = FakeLangfuse(svc)
        sink = L.LangfuseSink(client_for(svc), tmp_path)
        rep = sync.run(sink, conn, environ={})
        sent = (lf.bodies() + json.dumps([r.query for r in svc.requests])).lower()
        assert rep.status == "ok" and len(lf.observations) == 3                   # run A went, minus the names
        assert "pipeline-poc" not in sent and "acme-secret" not in sent
        assert {o["name"] for o in lf.observations} >= {L.REDACTED}
        assert [why for _, why in sink.refused] == ["case looks sensitive"]       # run B: its identity names the repo


def test_private_repo_names_are_sensitive_too(app: str, pg_url: str, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Mission 004C (F8): sandbox runs carry no repo, so nothing is masked by ownership here; but the names the sink
    reads now include repos of unknown ownership (private), never an own repo's name."""
    monkeypatch.setattr(egress, "_team", egress.NONE)                     # restored after the test
    with psycopg.connect(pg_url, autocommit=True) as conn:
        for sid, name, meta in (("lf-private", "client-notes", "{}"),
                                ("lf-own", "agent-memory",
                                 '{"team": false, "remote_owner": "vilmerfrost", "remote_host": "github.com"}')):
            conn.execute("insert into sessions (session_id, kind, project_dir, repo, project, transcript_bytes, env, meta) "
                         "values (%s, 'chat', '/lf-test', %s, %s, 1, 'prod', %s::jsonb)", (sid, name, name, meta))
    assert not L.sensitive("client-notes-tuned")                                     # not read yet
    with MockService() as svc, psycopg.connect(app) as conn:
        FakeLangfuse(svc)
        assert sync.run(L.LangfuseSink(client_for(svc), tmp_path), conn, environ={}).status == "ok"
    assert L.sensitive("client-notes-tuned") and not L.sensitive("agent-memory-tuned")


def test_since_skips_runs_that_started_before_it(app: str, tmp_path: Path) -> None:
    """Mission 004C (F4): `--since`. The _tree runs started on 2026-10-16."""
    _tree(tmp_path)
    with MockService() as svc, psycopg.connect(app) as conn:
        lf = FakeLangfuse(svc)
        sink = L.LangfuseSink(client_for(svc), tmp_path, since=datetime(2026, 10, 17, tzinfo=UTC))
        rep = sync.run(sink, conn, environ={})
        assert rep.status == "ok" and rep.rows == 0 and lf.observations == [] and len(sink.skipped) == 2
        sink = L.LangfuseSink(client_for(svc), tmp_path, since=datetime(2026, 10, 16, tzinfo=UTC))
        rep = sync.run(sink, conn, replay=True, environ={})
        assert rep.rows == 2 and sink.skipped == [] and len(lf.observations) == 10


# ---------------------------------------------------------------- Langfuse Cloud (Mission 004B)


def test_a_run_supabase_knows_as_exported_is_never_exported_again(app: str, tmp_path: Path) -> None:
    """Hobby hides data older than 30 days, so Langfuse cannot answer "is this trace there?" for an old run.
    The rsi_evidence link (written only after a successful upload) answers it instead."""
    pa, _ = _tree(tmp_path)
    with MockService() as svc, psycopg.connect(app) as conn:
        lf = FakeLangfuse(svc)
        assert sync.run(L.LangfuseSink(client_for(svc), tmp_path), conn, environ={}).batches == 3
        n_obs = len(lf.observations)
        lf.observations.clear()                                          # hidden: past the visible window
        rep = sync.run(L.LangfuseSink(client_for(svc), tmp_path), conn, replay=True, environ={})
        assert rep.status == "ok" and lf.observations == [] and n_obs == 10
        assert not svc.requests_to("POST", L.OTLP_PATH)[2:]              # the first run's two exports only


def test_experiment_scores_use_experiment_items_when_a_dataset_exists(tmp_path: Path) -> None:
    runs = [L.load_run_log(write_run(tmp_path, arm=arm, run_id=f"{arm}-20261016T10150{i}Z-1a2b3c",
                                     stdout=json.dumps(claude_result()), sidecar={"case": f"c{i}", "check_passed": ok}))
            for i, (arm, ok) in enumerate((("A", True), ("A", False), ("B", True)))]
    with MockService() as svc:
        FakeLangfuse(svc)
        for run in runs:
            L.upload_run(run, client_for(svc), datasets=True)
        before = len(svc.requests_to("GET", L.SCORES_PATH))
        (res,) = E.gather("experiment_scores", {"experiment_id": EXP}, [L.LangfuseRetriever(client_for(svc), now=lambda: NOW)])
        assert res.status == "ok" and [x.ref for x in res.refs] == [f"lf:dataset_run/{EXP}-A", f"lf:dataset_run/{EXP}-B"]
        ma, mb = (x.metrics for x in res.refs)
        assert (ma["traces"], ma["check_passed_rate"], ma["via"]) == (2, 0.5, "experiment-items")
        assert (mb["traces"], mb["check_passed_rate"]) == (1, 1.0) and "truncated" not in ma
        assert len(svc.requests_to("GET", L.SCORES_PATH)) == before             # no per-trace score calls
        (item_req,) = svc.requests_to("GET", L.EXPERIMENT_ITEMS_PATH)
        assert item_req.q("fromStartTime") == "2026-09-17T12:00:00.000Z" and item_req.q("fields") == "core,scores"


def test_experiment_scores_without_a_dataset_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "MAX_SCORED_TRACES", 1)
    runs = [L.load_run_log(write_run(tmp_path, arm="A", run_id=f"A-20261016T10150{i}Z-1a2b3c",
                                     stdout=json.dumps(claude_result()), sidecar={"case": f"c{i}", "check_passed": True}))
            for i in range(3)]
    with MockService() as svc:
        FakeLangfuse(svc)
        for run in runs:
            L.upload_run(run, client_for(svc))
        (res,) = E.gather("experiment_scores", {"experiment_id": EXP}, [L.LangfuseRetriever(client_for(svc), now=lambda: NOW)])
        (ev,) = res.refs
        assert (ev.metrics["traces"], ev.metrics["truncated"], ev.metrics["via"]) == (1, True, "observations")


def test_a_rejected_score_dead_letters_the_run_and_the_rest_lands(app: str, tmp_path: Path) -> None:
    _tree(tmp_path)
    with MockService() as svc, psycopg.connect(app) as conn:
        lf = FakeLangfuse(svc)
        real = lf._create_scores

        def picky(req: Any) -> Any:
            if any(b.get("name") == "judge_winner" for b in req.json):
                return 207, {"accepted": 1, "rejected": 1, "errors": [{"message": "score config mismatch"}]}
            return real(req)
        svc.route("POST", L.SCORES_WRITE_PATH, lf._guard(picky))
        rep = sync.run(L.LangfuseSink(client_for(svc), tmp_path), conn, environ={})
        assert (rep.status, rep.dead_letters, rep.failed) == ("ok", 1, 0)
        meta = sync.load(conn, "langfuse")
        (dl,) = meta["dead_letters"]
        assert dl["table"] == "sandbox_runs" and "scores rejected 1 of 2" in dl["error"]
        assert conn.execute("select count(*) from rsi_evidence where system = 'langfuse'").fetchone()[0] == 1
