"""Langfuse sandbox tracing (Mission 004A; the Mission 002 blueprint's Langfuse contract).

Langfuse (local, self-hosted v4) receives ONLY sandbox executions and investigator calls, never a
production session. `check()` refuses any record without a valid experiment id (refs.EXPERIMENT_RE) or
marked with an env other than `sandbox`; the sink also refuses a run whose session Supabase marks as
something else.

What one run becomes (all ids deterministic, so a retry finds the same records):

    trace   id = refs.trace_id(run_key)   session = experiment id   user = "rsi"   tags = [arm, kind]
            metadata (str -> str, <= 200 chars): amem_session_id = refs.session_uuid(run_key), baseline_id,
            artifact_hash, proposal_id, run_key, arm
    spans   root (type agent): status, timing, the agent's final result (redacted, <= 500 chars)
            one generation per model (Claude) or per turn (Codex): model, usage, cost
            one tool span per tool call: the tool NAME and its status, never its input or output
    scores  check_passed (BOOLEAN), judge_winner, eval_verdict (CATEGORICAL); id = <trace id>-<name>
    dataset optional: dataset rsi-<experiment>, one item per case (id rsi-<hash>), one experiment run per arm
            (<experiment>-<arm>); only rep 0 is linked (Langfuse assumes no repetitions in an experiment)

Text policy: no prompts, no tool inputs or outputs, no file contents, no paths, no stderr. The only text
is the agent's final result string, passed through agent_memory.redact.redact and cut to 500 chars.
Upload happens after the run, from the stored run log, so it works even when Langfuse was down.

API path (checked against the official docs on 2026-10-16, plain HTTP through integrations.http, no SDK):

- Traces, spans, generations: OTLP/HTTP JSON `POST /api/public/otel/v1/traces`, Basic auth
  `public:secret`, header `x-langfuse-ingestion-version: 4`. The docs call it "the supported path for
  trace ingestion". The legacy `POST /api/public/ingestion` trace/span/generation events are deprecated:
  self-hosted v4 in its default `events_only` write mode answers 207 with a 400 for every event type
  except `score-create`. Trace-wide attributes are copied to every span (v4 filters per observation).
- Re-export: the v4 migration guide says "do not export another record with the same span ID" and that
  v4 "does not reliably deduplicate repeated records on the read path". So `upload_run` first asks the
  Observations API whether the trace exists and exports the spans only when it does not. Scores are
  re-sent; they upsert (same id, name and timestamp; the event timestamp is the run's end time).
- Scores: `score-create` events on `POST /api/public/ingestion` (207 multi-status). The compatibility
  page says score ingestion stays "fully supported" in v4. `POST /api/public/scores` exists too, but its
  body changed (one object in older specs, an array in the current main-branch spec), so it is not used.
- Reads: `GET /api/public/v2/observations` (filter traceId or sessionId) and `GET /api/public/v3/scores`.
  `GET /api/public/traces/{id}` and the v1/v2 score reads answer 404 on v4 `events_only`.
- Datasets: `GET`/`POST /api/public/v2/datasets` and `POST /api/public/dataset-items` (upsert on id).
  A dataset run is the `langfuse.experiment.*` span attributes on every span of the item's trace;
  `POST /api/public/dataset-run-items` "must not be used with Langfuse v4".
- Health: `GET /api/public/projects` with the keys (proves reachability and auth in one call).

Docs: https://langfuse.com/docs/api-and-data-platform/features/public-api
      https://langfuse.com/docs/compatibility
      https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4
      https://langfuse.com/integrations/native/opentelemetry
      https://langfuse.com/integrations/native/opentelemetry/migration-to-v4
      https://langfuse.com/integrations/native/opentelemetry/experiments
      https://langfuse.com/faq/all/experiment-runs-not-visible-v4
      https://langfuse.com/docs/api-and-data-platform/features/observations-api
      https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk
      OpenAPI: https://github.com/langfuse/langfuse/blob/main/web/public/generated/api/openapi.yml
               (a v4.36+ server serves its own at /api/openapi.yaml)

Not verified against a real server (only against tests/test_integrations_langfuse.py's mock): the OTLP
JSON attribute mapping, the score upsert key on the ingestion path, the dataset create/get shapes, and
the Observations v2 / Scores v3 response fields. Check them once against the local v4 server.

Known limits: a re-run of the same run_key (a new sandbox run id, same case and rep) keeps the first
attempt's spans in Langfuse (spans are never re-exported); only its scores and its rsi_evidence metrics
follow the newer attempt. The existence check reads what Langfuse already shows, so a retry within the
ingestion delay could still export twice.

Run logs: the sink reads `<sandbox root>/experiments/<id>/logs/<run_id>.json` (RunResult.to_json),
`<run_id>.stdout` (the agent's own output) and the arm's entry in `sanitize-report.json` (baseline,
artifact). The orchestrator may add `<run_id>.rsi.json` with {case, rep, kind, env, proposal_id,
check_passed, judge_winner, eval_verdict}; without it case = run_id and rep = 0.

Config (env only, never a .env file): AMEM_LANGFUSE_HOST (https, or http to localhost),
AMEM_LANGFUSE_PUBLIC_KEY, AMEM_LANGFUSE_SECRET_KEY. Unconfigured: health is unavailable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import psycopg

from agent_memory import redact as _redact
from agent_memory.integrations import http, refs
from agent_memory.integrations.evidence import EvidenceRef, Health, NamedQuery
from agent_memory.integrations.sync import Batch

SOURCE = "lf"
SINK = "langfuse"
ENV = "sandbox"
KINDS = ("sandbox", "investigator")
USER_ID = "rsi"
TABLE = "sandbox_runs"                 # the sink's one cursor key
RESULT_MAX = 500
META_MAX = 200
SCORES = ("check_passed", "judge_winner", "eval_verdict")
VERDICTS = ("pass", "improves", "no-effect", "worse", "fail", "inconclusive")
TOOL_STATUSES = ("completed", "failed", "declined", "denied", "in_progress", "incomplete", "unknown")
TOOL_FAILED = frozenset({"failed", "declined", "denied", "incomplete"})

OTLP_PATH = "/api/public/otel/v1/traces"
INGESTION_PATH = "/api/public/ingestion"
OBSERVATIONS_PATH = "/api/public/v2/observations"
SCORES_PATH = "/api/public/v3/scores"
DATASETS_PATH = "/api/public/v2/datasets"
DATASET_ITEMS_PATH = "/api/public/dataset-items"
HEALTH_PATH = "/api/public/projects"

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,99}")     # model and tool names
_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")          # mcp server / tool parts
_RUN_TS_RE = re.compile(r"-(\d{8}T\d{6}Z)-")                       # rsi_sandbox run ids: A-20261016T101500Z-1a2b3c
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class LangfuseError(Exception):
    pass


class Refused(LangfuseError):
    """A record that must never reach Langfuse (not a sandbox record)."""


def clean_text(text: Any) -> str | None:
    """The only text that may leave: redacted first, then cut to 500 characters."""
    if not isinstance(text, str) or not text.strip():
        return None
    return _redact.redact(text)[:RESULT_MAX]


def _name(value: Any, default: str = "unknown") -> str:
    return value if isinstance(value, str) and _NAME_RE.fullmatch(value) else default


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _nanos(dt: datetime) -> str:
    return str((dt - _EPOCH) // timedelta(microseconds=1) * 1000)


def _meta_value(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)[:META_MAX]


# ---------------------------------------------------------------- what a run is


@dataclass
class Generation:
    model: str
    usage: dict[str, int]                                   # Langfuse usage_details keys (input, output, total, ...)
    cost_usd: float | None = None
    extra: dict[str, int] = field(default_factory=dict)     # counters Langfuse must not price (reasoning, cache writes)


@dataclass
class ToolCall:
    name: str                  # the tool's NAME only, never its input or output
    status: str                # one of TOOL_STATUSES


@dataclass
class AgentOutput:
    agent: str
    result: str | None = None
    is_error: bool = False
    num_turns: int | None = None
    generations: list[Generation] = field(default_factory=list)
    tools: list[ToolCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.result = clean_text(self.result)


@dataclass
class SandboxRun:
    experiment_id: str
    arm: str
    case: str
    rep: int
    run_id: str
    started_at: datetime
    duration_s: float = 0.0
    kind: str = "sandbox"
    env: str = ENV
    agent: str = "unknown"
    exit_code: int | None = None
    timed_out: bool = False
    is_error: bool = False
    num_turns: int | None = None
    result: str | None = None              # always redacted and <= 500 chars (see __post_init__)
    generations: list[Generation] = field(default_factory=list)
    tools: list[ToolCall] = field(default_factory=list)
    baseline_id: str | None = None
    artifact_hash: str | None = None
    proposal_id: str | None = None
    check_passed: bool | None = None
    judge_winner: str | None = None
    eval_verdict: str | None = None

    def __post_init__(self) -> None:
        self.result = clean_text(self.result)

    @property
    def run_key(self) -> str:
        return refs.run_key(self.experiment_id, self.arm, self.case, self.rep)

    @property
    def trace_id(self) -> str:
        return refs.trace_id(self.run_key)

    @property
    def session_id(self) -> str:
        return refs.session_uuid(self.run_key)

    @property
    def ended_at(self) -> datetime:
        return self.started_at + timedelta(seconds=max(float(self.duration_s or 0), 0.0))

    @property
    def failed(self) -> bool:
        return bool(self.timed_out or self.is_error or (self.exit_code not in (0, None)))

    def tokens(self) -> tuple[int, int]:
        """(total, output) over every generation."""
        total = sum(g.usage.get("total", sum(v for k, v in g.usage.items() if k != "total")) for g in self.generations)
        return total, sum(g.usage.get("output", 0) for g in self.generations)

    def cost(self) -> float | None:
        costs = [g.cost_usd for g in self.generations if g.cost_usd is not None]
        return round(sum(costs), 8) if costs else None

    def trace_metadata(self) -> dict[str, str]:
        """Ids only, each checked against its shape, so no free text can ride along."""
        raw = {"amem_session_id": self.session_id, "baseline_id": _label(self.baseline_id),
               "artifact_hash": _label(self.artifact_hash, _HASH_RE), "proposal_id": _label(self.proposal_id, _UUID_RE),
               "run_key": self.run_key, "arm": self.arm}
        return {k: v for k, v in ((k, _meta_value(v)) for k, v in raw.items()) if v is not None}

    def metrics(self) -> dict[str, Any]:
        """Numbers and labels only (rsi_evidence.metrics, the sink's row). No text."""
        total, output = self.tokens()
        m = {"amem_session_id": self.session_id, "experiment_id": self.experiment_id, "arm": self.arm, "case": self.case,
             "rep": self.rep, "kind": self.kind, "agent": self.agent, "run_id": self.run_id,
             "tokens_input": total - output, "tokens_output": output, "tokens_total": total, "cost_usd": self.cost(),
             "generations": len(self.generations), "tools": len(self.tools),
             "tool_errors": sum(t.status in TOOL_FAILED for t in self.tools), "exit_code": self.exit_code,
             "timed_out": self.timed_out, "is_error": self.is_error, "duration_s": self.duration_s,
             "num_turns": self.num_turns, "check_passed": self.check_passed, "judge_winner": self.judge_winner,
             "eval_verdict": self.eval_verdict}
        return {k: v for k, v in m.items() if v is not None}


def check(run: SandboxRun) -> None:
    """Raise Refused unless this is a sandbox record that may go to Langfuse."""
    if run.env != ENV:
        raise Refused(f"not a sandbox record (env={str(run.env)[:20]!r})")
    if not isinstance(run.experiment_id, str) or not refs.EXPERIMENT_RE.fullmatch(run.experiment_id):
        raise Refused(f"no valid experiment id ({str(run.experiment_id)[:40]!r})")
    if run.kind not in KINDS:
        raise Refused(f"unknown kind {str(run.kind)[:20]!r}")
    if not isinstance(run.run_id, str) or not _PART_RE.fullmatch(run.run_id):
        raise Refused("bad run id")
    try:
        run.run_key  # noqa: B018 - validates arm, case and rep
    except refs.RefError as exc:
        raise Refused(str(exc)) from None
    if run.judge_winner is not None and not (run.judge_winner == "tie" or re.fullmatch(r"[A-Z]", run.judge_winner)):
        raise Refused("judge_winner is an arm letter or 'tie'")
    if run.eval_verdict is not None and run.eval_verdict not in VERDICTS:
        raise Refused(f"eval_verdict is one of {', '.join(VERDICTS)}")
    if run.check_passed is not None and not isinstance(run.check_passed, bool):
        raise Refused("check_passed is a boolean")


# ---------------------------------------------------------------- parsers


def parse_claude_json(text: str, model: str | None = None) -> AgentOutput:
    """Claude Code `--output-format json`: one result object. Keeps numbers, model names, denied tool NAMES
    and the final result string; drops everything else (permission_denials[].tool_input, session ids ...)."""
    obj: Any = None
    try:
        obj = json.loads(text)
    except ValueError:
        for line in reversed(text.splitlines()):
            try:
                cand = json.loads(line)
            except ValueError:
                continue
            if isinstance(cand, dict) and cand.get("type") == "result":
                obj = cand
                break
    if not isinstance(obj, dict) or obj.get("type") != "result":
        raise ValueError("no Claude result object")
    gens: list[Generation] = []
    for m, mu in (obj.get("modelUsage") or {}).items():
        if not isinstance(mu, dict):
            continue
        inp, out = _int(mu.get("inputTokens")), _int(mu.get("outputTokens"))
        cr, cc = _int(mu.get("cacheReadInputTokens")), _int(mu.get("cacheCreationInputTokens"))
        gens.append(Generation(_name(m), _claude_usage(inp, out, cr, cc), _float(mu.get("costUSD"))))
    if not gens and isinstance(obj.get("usage"), dict):
        u = obj["usage"]
        gens.append(Generation(_name(model), _claude_usage(_int(u.get("input_tokens")), _int(u.get("output_tokens")),
                                                           _int(u.get("cache_read_input_tokens")),
                                                           _int(u.get("cache_creation_input_tokens"))),
                               _float(obj.get("total_cost_usd"))))
    tools = [ToolCall(_name(d.get("tool_name")), "denied") for d in obj.get("permission_denials") or []
             if isinstance(d, dict)]
    subtype = obj.get("subtype")
    is_error = bool(obj.get("is_error")) or (isinstance(subtype, str) and subtype.startswith("error"))
    turns = obj.get("num_turns")
    return AgentOutput("claude", obj.get("result") if isinstance(obj.get("result"), str) else None, is_error,
                       turns if isinstance(turns, int) and not isinstance(turns, bool) else None, gens, tools)


def _claude_usage(inp: int, out: int, cache_read: int, cache_write: int) -> dict[str, int]:
    u = {"input": inp, "output": out}
    if cache_read:
        u["cache_read_input_tokens"] = cache_read
    if cache_write:
        u["cache_creation_input_tokens"] = cache_write
    u["total"] = inp + out + cache_read + cache_write
    return u


_CODEX_TOOLS = ("command_execution", "file_change", "mcp_tool_call", "collab_tool_call", "web_search")


def _codex_tool_name(item: Mapping[str, Any]) -> str:
    kind = str(item.get("type"))
    if kind == "mcp_tool_call":
        server, tool = item.get("server"), item.get("tool")
        if all(isinstance(x, str) and _PART_RE.fullmatch(x) for x in (server, tool)):
            return f"mcp:{server}/{tool}"
    if kind == "collab_tool_call" and isinstance(item.get("tool"), str) and _PART_RE.fullmatch(item["tool"]):
        return f"collab:{item['tool']}"
    return kind


def parse_codex_jsonl(text: str, model: str | None = None) -> AgentOutput:
    """Codex `exec --json` events. Keeps per-turn usage, tool item NAMES and statuses, and the last
    agent_message; drops commands, outputs, file changes, reasoning and earlier messages."""
    order: list[str] = []
    tools: dict[str, ToolCall] = {}
    gens: list[Generation] = []
    result: str | None = None
    is_error, turns, seen = False, 0, False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        seen = True
        t = ev.get("type")
        if t == "turn.started":
            turns += 1
        elif t == "turn.completed":
            u = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            inp, cached, out = _int(u.get("input_tokens")), _int(u.get("cached_input_tokens")), _int(u.get("output_tokens"))
            usage = {"input": max(inp - cached, 0), "output": out}
            if cached:
                usage["input_cached_tokens"] = cached
            usage["total"] = max(inp, cached) + out
            extra = {k: _int(u.get(k)) for k in ("cache_write_input_tokens", "reasoning_output_tokens") if _int(u.get(k))}
            gens.append(Generation(_name(model), usage, None, extra))
        elif t in ("turn.failed", "error"):
            is_error = True
        elif t in ("item.started", "item.updated", "item.completed"):
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            kind = item.get("type")
            if kind == "agent_message" and t == "item.completed" and isinstance(item.get("text"), str):
                result = item["text"]
            elif kind in _CODEX_TOOLS:
                iid = str(item.get("id") or f"_{len(order)}")
                if iid not in tools:
                    order.append(iid)
                status = item.get("status")
                if not isinstance(status, str):
                    status = "completed" if t == "item.completed" else "in_progress"
                tools[iid] = ToolCall(_codex_tool_name(item), status if status in TOOL_STATUSES else "unknown")
    if not seen:
        raise ValueError("no Codex events")
    calls = [ToolCall(tools[i].name, "incomplete" if tools[i].status == "in_progress" else tools[i].status) for i in order]
    return AgentOutput("codex", result, is_error, turns or None, gens, calls)


def _command(argv: Sequence[Any]) -> tuple[str, list[str]]:
    """(agent, its args) from a docker argv: the first token named claude or codex."""
    for i, tok in enumerate(argv):
        if isinstance(tok, str) and tok.rsplit("/", 1)[-1] in ("claude", "codex"):
            return tok.rsplit("/", 1)[-1], [str(a) for a in argv[i + 1:]]
    return "unknown", []


def _model_flag(args: Sequence[str]) -> str | None:
    """`-m X`, `--model X`, `--model=X` or Codex's `-c model=X`. Only a value shaped like a model name."""
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a in ("-m", "--model"):
            v = nxt
        elif a.startswith("--model="):
            v = a.split("=", 1)[1]
        elif a in ("-c", "--config") and nxt.startswith("model="):
            v = nxt.split("=", 1)[1].strip("\"'")
        else:
            continue
        return v if _NAME_RE.fullmatch(v) else None
    return None


def parse_agent_output(agent: str, text: str, model: str | None = None) -> AgentOutput:
    """The agent's stdout; an empty or unreadable output is an output without generations, never a crash."""
    parsers = {"claude": (parse_claude_json,), "codex": (parse_codex_jsonl,)}.get(agent,
                                                                                  (parse_claude_json, parse_codex_jsonl))
    for parse in parsers:
        try:
            return parse(text, model)
        except ValueError:
            continue
    return AgentOutput(agent)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def load_run_log(path: Path | str) -> SandboxRun:
    """One sandbox run from `experiments/<id>/logs/<run_id>.json` (rsi_sandbox RunResult.to_json) and its
    neighbours: `<run_id>.stdout`, the optional `<run_id>.rsi.json`, and `../sanitize-report.json`.
    The argv is read only for the agent name and a model flag; stderr is never read."""
    path = Path(path)
    meta = json.loads(path.read_text())
    if not isinstance(meta, dict) or meta.get("run_id") != path.stem:
        raise ValueError(f"not a sandbox run log: {path.name}")
    run_id, arm = str(meta["run_id"]), str(meta.get("arm") or "")
    exp_dir = path.parent.parent
    side = _read_json(path.with_name(f"{run_id}.rsi.json"))
    side = side if isinstance(side, dict) else {}
    report = _read_json(exp_dir / "sanitize-report.json")
    arm_rep = report.get(arm) if isinstance(report, dict) and isinstance(report.get(arm), dict) else {}
    agent, args = _command(meta.get("argv") or [])
    try:
        stdout = path.with_name(f"{run_id}.stdout").read_text(errors="replace")
    except OSError:
        stdout = ""
    out = parse_agent_output(agent, stdout, _model_flag(args))
    duration = _float(meta.get("duration_s")) or 0.0
    m = _RUN_TS_RE.search(run_id)
    if m:
        started = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    else:
        started = datetime.fromtimestamp(path.stat().st_mtime, UTC) - timedelta(seconds=duration)
    rep = side.get("rep", 0)
    exit_code = meta.get("exit_code")
    return SandboxRun(
        experiment_id=str(meta.get("experiment") or exp_dir.name), arm=arm, case=str(side.get("case") or run_id),
        rep=rep if isinstance(rep, int) and not isinstance(rep, bool) else -1, run_id=run_id, started_at=started,
        duration_s=duration, kind=str(side.get("kind") or "sandbox"), env=str(side.get("env") or ENV),
        agent=out.agent if out.agent != "unknown" else agent,
        exit_code=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
        timed_out=bool(meta.get("timed_out")), is_error=out.is_error, num_turns=out.num_turns, result=out.result,
        generations=out.generations, tools=out.tools,
        baseline_id=arm_rep.get("baseline") or side.get("baseline_id"),
        artifact_hash=arm_rep.get("artifact_hash") or side.get("artifact_hash"), proposal_id=side.get("proposal_id"),
        check_passed=side.get("check_passed"), judge_winner=side.get("judge_winner"),
        eval_verdict=side.get("eval_verdict"))


# ---------------------------------------------------------------- deterministic ids and payloads


def span_id(key: str, part: str) -> str:
    """16 hex (W3C span-id size), the same on every retry."""
    return hashlib.sha256(f"{key}#{part}".encode()).hexdigest()[:16]


def score_id(trace: str, name: str) -> str:
    return f"{trace}-{name}"


def dataset_name(experiment_id: str) -> str:
    return f"rsi-{experiment_id}"


def dataset_item_id(experiment_id: str, case: str) -> str:
    return "rsi-" + hashlib.sha256(f"{experiment_id}/{case}".encode()).hexdigest()[:32]


def experiment_run_id(experiment_id: str, arm: str) -> str:
    return f"{experiment_id}-{arm}"


def _attrs(d: Mapping[str, Any]) -> list[dict[str, Any]]:
    """OTLP JSON attributes: strings, string arrays and booleans only (numbers travel inside JSON strings)."""
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):
            val: dict[str, Any] = {"boolValue": v}
        elif isinstance(v, (list, tuple)):
            val = {"arrayValue": {"values": [{"stringValue": str(x)} for x in v]}}
        else:
            val = {"stringValue": str(v)}
        out.append({"key": k, "value": val})
    return out


def _span(sid: str, parent: str | None, trace: str, name: str, start: datetime, end: datetime,
          attrs: Mapping[str, Any], error: str | None = None) -> dict[str, Any]:
    s: dict[str, Any] = {"traceId": trace, "spanId": sid, "name": name, "kind": 1, "startTimeUnixNano": _nanos(start),
                         "endTimeUnixNano": _nanos(end), "attributes": _attrs(attrs),
                         "status": {"code": 2, "message": error} if error else {"code": 1}}
    if parent:
        s["parentSpanId"] = parent
    return s


def build_spans(run: SandboxRun, dataset_id: str | None = None) -> dict[str, Any]:
    """The OTLP/HTTP JSON body for one run. Tool span times are ordinal (spread evenly over the run):
    neither CLI reports per-tool timestamps in these output formats."""
    key, tid = run.run_key, run.trace_id
    root = span_id(key, "root")
    start, end = run.started_at, run.ended_at
    tmeta = run.trace_metadata()
    common: dict[str, Any] = {"langfuse.trace.name": f"rsi.{run.kind}.{run.agent}", "langfuse.session.id": run.experiment_id,
                              "langfuse.user.id": USER_ID, "langfuse.trace.tags": [run.arm, run.kind],
                              "langfuse.environment": ENV,
                              **{f"langfuse.trace.metadata.{k}": v for k, v in tmeta.items()}}
    if dataset_id and run.rep == 0:
        exp_run = experiment_run_id(run.experiment_id, run.arm)
        common.update({"langfuse.experiment.id": exp_run, "langfuse.experiment.name": exp_run,
                       "langfuse.experiment.dataset.id": dataset_id,
                       "langfuse.experiment.item.id": dataset_item_id(run.experiment_id, run.case),
                       "langfuse.experiment.item.root_observation_id": root})
    root_meta = {**tmeta, "run_id": run.run_id, "case": run.case, "rep": run.rep, "kind": run.kind, "agent": run.agent,
                 "exit_code": run.exit_code, "timed_out": run.timed_out, "duration_s": run.duration_s,
                 "num_turns": run.num_turns}
    error = ("timed out" if run.timed_out else f"exit {run.exit_code}" if run.exit_code not in (0, None)
             else "agent error" if run.is_error else None)
    spans = [_span(root, None, tid, f"rsi.{run.kind}", start, end,
                   {**common, "langfuse.observation.type": "agent", "langfuse.observation.output": run.result,
                    "langfuse.observation.level": "ERROR" if error else "DEFAULT",
                    **{f"langfuse.observation.metadata.{k}": _meta_value(v) for k, v in root_meta.items()}}, error)]
    for i, g in enumerate(run.generations):
        spans.append(_span(span_id(key, f"generation/{i}"), root, tid, g.model, start, end, {
            **common, "langfuse.observation.type": "generation", "langfuse.observation.model.name": g.model,
            "langfuse.observation.usage_details": json.dumps(g.usage, sort_keys=True),
            "langfuse.observation.cost_details": json.dumps({"total": g.cost_usd}) if g.cost_usd is not None else None,
            **{f"langfuse.observation.metadata.{k}": str(v) for k, v in g.extra.items()}}))
    step = (end - start) / len(run.tools) if run.tools else timedelta(0)
    for i, t in enumerate(run.tools):
        bad = t.status if t.status in TOOL_FAILED else None
        spans.append(_span(span_id(key, f"tool/{i}"), root, tid, t.name, start + step * i, start + step * (i + 1), {
            **common, "langfuse.observation.type": "tool", "langfuse.observation.level": "ERROR" if bad else "DEFAULT",
            "langfuse.observation.metadata.status": t.status}, bad))
    return {"resourceSpans": [{"resource": {"attributes": _attrs({"service.name": "amem-rsi"})},
                               "scopeSpans": [{"scope": {"name": "agent_memory.integrations.langfuse", "version": "1"},
                                               "spans": spans}]}]}


def build_scores(run: SandboxRun) -> list[dict[str, Any]]:
    """`score-create` ingestion events. Same id, name and timestamp on every retry, so Langfuse upserts."""
    ts, tid = _iso(run.ended_at), run.trace_id
    values: list[tuple[str, Any, str]] = []
    if run.check_passed is not None:
        values.append(("check_passed", 1.0 if run.check_passed else 0.0, "BOOLEAN"))
    if run.judge_winner is not None:
        values.append(("judge_winner", run.judge_winner, "CATEGORICAL"))
    if run.eval_verdict is not None:
        values.append(("eval_verdict", run.eval_verdict, "CATEGORICAL"))
    out = []
    for name, value, dtype in values:
        sid = score_id(tid, name)
        out.append({"id": f"{sid}-create", "timestamp": ts, "type": "score-create",
                    "body": {"id": sid, "traceId": tid, "name": name, "value": value, "dataType": dtype,
                             "environment": ENV}})
    return out


# ---------------------------------------------------------------- the client


def host_problem(host: str | None) -> str | None:
    if not host:
        return "not configured"
    u = urlsplit(host)
    if u.username or u.password or u.query or u.fragment or not u.hostname:
        return "AMEM_LANGFUSE_HOST must be a plain base URL"
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")):
        return None
    return "AMEM_LANGFUSE_HOST must be https, or http to localhost"


@dataclass
class LangfuseClient:
    host: str | None = None
    public_key: str | None = None
    secret_key: str | None = field(default=None, repr=False)
    timeout: float = 10.0
    retries: int = 2
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **kw: Any) -> LangfuseClient:
        env = os.environ if environ is None else environ
        host = (env.get("AMEM_LANGFUSE_HOST") or "").strip().rstrip("/") or None
        return cls(host, (env.get("AMEM_LANGFUSE_PUBLIC_KEY") or "").strip() or None,
                   (env.get("AMEM_LANGFUSE_SECRET_KEY") or "").strip() or None, **kw)

    @property
    def problem(self) -> str | None:
        if not (self.host and self.public_key and self.secret_key):
            return "not configured"
        return host_problem(self.host)

    def _headers(self) -> dict[str, str]:
        auth = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode("ascii")
        return {"Authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4", "Accept": "application/json"}

    def _call(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, body: Any = None,
              retries: int | None = None, timeout: float | None = None) -> http.Response:
        if self.problem:
            raise LangfuseError(self.problem)
        url = f"{self.host}{path}" + (f"?{urlencode(params)}" if params else "")
        return http.request(method, url, headers=self._headers(), body=body, timeout=timeout or self.timeout,
                            retries=self.retries if retries is None else retries, sleep=self.sleep)

    @staticmethod
    def _json(r: http.Response) -> Any:
        try:
            return r.json()
        except ValueError:
            return None

    def health(self) -> Health:
        if self.problem:
            return Health(False, self.problem)
        try:
            self._call("GET", HEALTH_PATH, retries=0, timeout=min(self.timeout, 3.0))
        except http.HttpError as exc:
            return Health(False, f"HTTP {exc.status}" if exc.status else "unreachable")
        return Health(True)

    def export_spans(self, body: Mapping[str, Any]) -> None:
        data = self._json(self._call("POST", OTLP_PATH, body=body)) or {}
        partial = data.get("partialSuccess") if isinstance(data, dict) else None
        rejected = int((partial or {}).get("rejectedSpans") or 0)
        if rejected:
            raise LangfuseError(f"OTLP rejected {rejected} span(s)")

    def ingest(self, events: Sequence[Mapping[str, Any]]) -> None:
        data = self._json(self._call("POST", INGESTION_PATH, body={"batch": list(events)})) or {}
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            ids = ", ".join(f"{e.get('id')}:{e.get('status')}" for e in errors[:5] if isinstance(e, dict))
            raise LangfuseError(f"ingestion rejected {len(errors)} event(s): {ids}"[:300])

    def _pages(self, path: str, params: Mapping[str, Any], max_pages: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max_pages):
            data = self._json(self._call("GET", path, params={**params, **({"cursor": cursor} if cursor else {})}))
            if not isinstance(data, dict):
                break
            out.extend(x for x in data.get("data") or [] if isinstance(x, dict))
            cursor = (data.get("meta") or {}).get("cursor") if isinstance(data.get("meta"), dict) else None
            if not cursor:
                break
        return out

    def observations(self, max_pages: int = 10, **params: Any) -> list[dict[str, Any]]:
        return self._pages(OBSERVATIONS_PATH, params, max_pages)

    def scores(self, max_pages: int = 5, **params: Any) -> list[dict[str, Any]]:
        return self._pages(SCORES_PATH, params, max_pages)

    def trace_exists(self, trace: str) -> bool:
        return bool(self.observations(max_pages=1, traceId=trace, fields="core", limit=1))

    def ensure_dataset(self, name: str) -> str:
        """The dataset's Langfuse id (created once; v4 needs Dataset.id on experiment spans)."""
        try:
            data = self._json(self._call("GET", f"{DATASETS_PATH}/{quote(name, safe='')}"))
        except http.HttpError as exc:
            if exc.status != 404:
                raise
            data = self._json(self._call("POST", DATASETS_PATH, body={
                "name": name, "description": "RSI sandbox experiment cases (amem)", "metadata": {"source": "amem-rsi"}}))
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise LangfuseError("dataset response has no id")
        return data["id"]

    def upsert_dataset_item(self, dataset: str, item_id: str, input_: Mapping[str, Any],
                            metadata: Mapping[str, Any]) -> None:
        self._call("POST", DATASET_ITEMS_PATH, body={"datasetName": dataset, "id": item_id, "input": dict(input_),
                                                     "metadata": dict(metadata)})


# ---------------------------------------------------------------- upload


@dataclass
class UploadResult:
    trace_id: str
    spans: int = 0                 # spans exported now; 0 when the trace was already there
    scores: int = 0
    existed: bool = False
    dataset_item: str | None = None


def upload_run(run: SandboxRun, client: LangfuseClient, *, datasets: bool = False) -> UploadResult:
    """Send one stored run. Idempotent: spans only when the trace is not in Langfuse yet, scores upsert,
    dataset and item are get-or-create / upsert by deterministic id."""
    check(run)
    res = UploadResult(run.trace_id)
    dataset_id = None
    if datasets and run.rep == 0:
        name = dataset_name(run.experiment_id)
        dataset_id = client.ensure_dataset(name)
        res.dataset_item = dataset_item_id(run.experiment_id, run.case)
        client.upsert_dataset_item(name, res.dataset_item, {"experiment_id": run.experiment_id, "case": run.case},
                                   {"source": "amem-rsi"})
    res.existed = client.trace_exists(run.trace_id)
    if not res.existed:
        body = build_spans(run, dataset_id)
        client.export_spans(body)
        res.spans = len(body["resourceSpans"][0]["scopeSpans"][0]["spans"])
    events = build_scores(run)
    if events:
        client.ingest(events)
        res.scores = len(events)
    return res


def evidence_for(run: SandboxRun) -> EvidenceRef:
    subjects = [refs.amem("experiment", run.experiment_id)]
    if run.artifact_hash and _HASH_RE.fullmatch(run.artifact_hash):
        subjects.append(refs.amem("artifact", run.artifact_hash))
    if run.proposal_id and _UUID_RE.fullmatch(run.proposal_id):
        subjects.append(refs.amem("proposal", run.proposal_id))
    return EvidenceRef(refs.lf("trace", run.trace_id), SOURCE, "trace", subjects, run.metrics(),
                       f"{run.kind} run {run.run_key} ({run.agent}), traced in Langfuse", _iso(run.ended_at))


# ---------------------------------------------------------------- the sink


class LangfuseSink:
    """sync.Sink over a directory of sandbox run logs (not Supabase rows: Langfuse gets only sandbox runs).

    One cursor key (`sandbox_runs`); watermark `<mtime ns, 20 digits>|<experiment>/<run_id>`, where mtime is
    the newer of the run log and its `.rsi.json`, so scores written later re-send the run (scores only:
    the trace is already there). A refused or unreadable log is a batch with no rows: nothing leaves and
    the watermark moves past it (listed in `refused`)."""

    name = SINK

    def __init__(self, client: LangfuseClient, root: Path | str | None = None, *, datasets: bool = False):
        self.client = client
        self.root = Path(root).expanduser() if root else Path.home() / "sandbox"
        self.datasets = datasets
        self.refused: list[tuple[str, str]] = []
        self.uploads: list[UploadResult] = []
        self._runs: dict[str, SandboxRun] = {}

    def health(self) -> Health:
        return self.client.health()

    def _logs(self) -> list[tuple[str, Path]]:
        out = []
        for p in (self.root / "experiments").glob("*/logs/*.json"):
            if p.name.endswith(".rsi.json"):
                continue
            side = p.with_name(f"{p.stem}.rsi.json")
            mtime = max(p.stat().st_mtime_ns, side.stat().st_mtime_ns if side.exists() else 0)
            out.append((f"{mtime:020d}|{p.parent.parent.name}/{p.stem}", p))
        return sorted(out)

    def _guard_session(self, conn: psycopg.Connection, run: SandboxRun) -> None:
        row = conn.execute("select coalesce(env, 'prod') from sessions where session_id = %s",
                           (run.session_id,)).fetchone()
        if row and row[0] != ENV:
            raise Refused(f"session {run.session_id} is marked env={row[0]}")
        if run.artifact_hash and not run.proposal_id:
            row = conn.execute("select id::text from rsi_proposals where artifact_hash = %s and "
                               "(experiment_id is null or experiment_id = %s) order by created_at desc limit 1",
                               (run.artifact_hash, run.experiment_id)).fetchone()
            if row:
                run.proposal_id = row[0]

    def batches(self, conn: psycopg.Connection, watermarks: Mapping[str, str], *, replay: bool) -> Iterator[Batch]:
        mark = "" if replay else watermarks.get(TABLE, "")
        for wm, path in self._logs():
            if wm <= mark:
                continue
            try:
                run = load_run_log(path)
                check(run)
                self._guard_session(conn, run)
            except (LangfuseError, refs.RefError, ValueError, OSError) as exc:
                self.refused.append((wm.split("|", 1)[1], str(exc)[:200]))
                yield Batch(TABLE, [], wm)
                continue
            self._runs[run.trace_id] = run
            yield Batch(TABLE, [{"trace_id": run.trace_id, **run.metrics()}], wm, [evidence_for(run)], key=run.trace_id)

    def push(self, batch: Batch) -> None:
        if not batch.key:
            return
        self.uploads.append(upload_run(self._runs[batch.key], self.client, datasets=self.datasets))
        self._runs.pop(batch.key, None)


# ---------------------------------------------------------------- the retriever


def _str_meta(meta: Any) -> dict[str, str]:
    return {str(k): v for k, v in meta.items() if isinstance(v, str)} if isinstance(meta, dict) else {}


def _label(value: Any, pattern: re.Pattern[str] = _LABEL_RE) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value >= 1
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "1.0", "false", "0", "0.0"):
        return value.strip().lower() in ("true", "1", "1.0")
    return None


def _score_values(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for s in rows:
        name = s.get("name")
        if name not in SCORES or name in out:
            continue
        v = s.get("stringValue") if s.get("stringValue") is not None else s.get("value")
        val = _as_bool(v) if name == "check_passed" else _label(v)
        if val is not None:
            out[name] = val
    return out


def _subjects(session: Any, meta: Mapping[str, str]) -> list[str]:
    out = []
    if isinstance(session, str) and refs.EXPERIMENT_RE.fullmatch(session):
        out.append(refs.amem("experiment", session))
    if _label(meta.get("artifact_hash"), _HASH_RE):
        out.append(refs.amem("artifact", meta["artifact_hash"]))
    if _label(meta.get("proposal_id"), _UUID_RE):
        out.append(refs.amem("proposal", meta["proposal_id"]))
    return out


class LangfuseRetriever:
    """Source `lf`. Two named queries, no free-form path:

    - `trace` (trace_id): tokens, cost, tool counts, scores and amem_session_id (the round-trip check);
    - `experiment_scores` (experiment_id): per arm, the trace count and the score aggregates.
    Values read back from Langfuse are labels or numbers only, each checked against a pattern."""

    source = SOURCE
    _QUERIES = {
        "trace": NamedQuery("trace", {"trace_id": refs.TRACE_RE.pattern},
                            "one sandbox trace: tokens, cost, tools, scores, amem_session_id", ("trace_id",)),
        "experiment_scores": NamedQuery("experiment_scores", {"experiment_id": refs.EXPERIMENT_RE.pattern},
                                        "score aggregates per arm for one experiment", ("experiment_id",)),
    }

    def __init__(self, client: LangfuseClient):
        self.client = client

    def health(self) -> Health:
        return self.client.health()

    def named_queries(self) -> Mapping[str, NamedQuery]:
        return self._QUERIES

    def query(self, name: str, params: Mapping[str, str]) -> list[EvidenceRef]:
        if name == "trace":
            return self._trace(params["trace_id"])
        return self._experiment(params["experiment_id"])

    def _trace(self, tid: str) -> list[EvidenceRef]:
        rows = self.client.observations(traceId=tid, fields="core,basic,model,usage,metadata,trace_context", limit=100)
        rows = [o for o in rows if o.get("traceId") == tid]
        if not rows:
            return []
        root = next((o for o in rows if not o.get("parentObservationId")), rows[0])
        meta = _str_meta(root.get("metadata"))
        gens = [o for o in rows if str(o.get("type")).upper() == "GENERATION"]
        tools = [o for o in rows if str(o.get("type")).upper() == "TOOL"]
        total = output = 0
        cost = 0.0
        for g in gens:
            ud = g.get("usageDetails") if isinstance(g.get("usageDetails"), dict) else {}
            nums = {k: v for k, v in ud.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            total += int(nums.get("total", sum(v for k, v in nums.items() if k != "total")))
            output += int(nums.get("output", 0))
            c = g.get("totalCost")
            if c is None and isinstance(g.get("costDetails"), dict):
                c = g["costDetails"].get("total")
            cost += float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else 0.0
        tags = [t for t in root.get("tags") or [] if _label(t)] if isinstance(root.get("tags"), list) else []
        metrics = {"amem_session_id": _label(meta.get("amem_session_id"), _UUID_RE),
                   "session_id": _label(root.get("sessionId")), "arm": _label(meta.get("arm"), re.compile(r"[A-Z]")),
                   "tags": tags, "level": _label(root.get("level")), "observations": len(rows),
                   "generations": len(gens), "tools": len(tools),
                   "tool_errors": sum(str(o.get("level")) == "ERROR" for o in tools),
                   "tokens_total": total, "tokens_output": output, "tokens_input": total - output,
                   "cost_usd": round(cost, 8), **_score_values(self.client.scores(traceId=tid, limit=100))}
        return [EvidenceRef(refs.lf("trace", tid), SOURCE, "trace", _subjects(root.get("sessionId"), meta),
                            {k: v for k, v in metrics.items() if v is not None},
                            observed_at=_label(root.get("startTime"), re.compile(r"[0-9T:.+Z-]{10,40}")))]

    def _experiment(self, exp: str) -> list[EvidenceRef]:
        roots = self.client.observations(sessionId=exp, isRootObservation="true",
                                         fields="core,basic,metadata,trace_context", limit=1000)
        arms: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for r in roots:
            tid = r.get("traceId")
            if not isinstance(tid, str) or not refs.TRACE_RE.fullmatch(tid) or tid in seen or r.get("parentObservationId"):
                continue
            seen.add(tid)
            meta = _str_meta(r.get("metadata"))
            arm = meta.get("arm")
            if not (isinstance(arm, str) and re.fullmatch(r"[A-Z]", arm)):
                continue
            a = arms.setdefault(arm, {"traces": 0, "check_passed_n": 0, "check_passed_true": 0,
                                      "judge_winner": {}, "eval_verdict": {}})
            a["traces"] += 1
            vals = _score_values(self.client.scores(traceId=tid, limit=100))
            if "check_passed" in vals:
                a["check_passed_n"] += 1
                a["check_passed_true"] += int(vals["check_passed"])
            for k in ("judge_winner", "eval_verdict"):
                if k in vals:
                    a[k][vals[k]] = a[k].get(vals[k], 0) + 1
        out = []
        for arm, a in sorted(arms.items()):
            n = a["check_passed_n"]
            metrics = {"experiment_id": exp, "arm": arm, **a,
                       "check_passed_rate": round(a["check_passed_true"] / n, 4) if n else None}
            out.append(EvidenceRef(refs.lf("dataset_run", experiment_run_id(exp, arm)), SOURCE, "score",
                                   [refs.amem("experiment", exp)], {k: v for k, v in metrics.items() if v is not None}))
        return out
