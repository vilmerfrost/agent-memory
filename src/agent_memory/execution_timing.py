"""Timing facts only: no prompts, command arguments, results or credentials are retained."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import re
from typing import Any, Iterable

CATEGORIES = frozenset({"workflow", "setup", "test_full", "test_targeted", "command", "tool", "api",
                        "model_output", "review_wait", "approval_wait", "unclassified"})
STATUSES = frozenset({"running", "passed", "failed", "error", "interrupted", "unknown"})
KINDS = frozenset({"measured", "provider_reported", "observed", "estimated", "unknown"})
LABEL = re.compile(r"[A-Za-z0-9_.:/@+ -]{1,100}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9_.:/@+-]{1,200}\Z")
META_KEYS = frozenset({"head", "config", "repo", "turn_id", "request_id", "attempt", "model",
                       "clock", "duration_source", "tests", "passed", "failed", "skipped", "errors",
                       "workers", "log_hash", "log_ref", "worktree", "operation_id", "http_status", "timing_unavailable",
                       "identity_source", "identity_unavailable", "actor_scope", "api_scope", "endpoint"})


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else result.replace(tzinfo=UTC)
    except ValueError:
        return None


def epoch_ms(value: Any) -> datetime | None:
    n = number(value)
    if n is None:
        return None
    try:
        return datetime.fromtimestamp(n / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def label(value: Any, fallback: str = "unknown") -> str:
    return value if isinstance(value, str) and LABEL.fullmatch(value) else fallback


def identity(value: Any) -> str | None:
    return value if isinstance(value, str) and IDENTITY.fullmatch(value) else None


def key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def category_for_command(command: Any) -> str:
    """Classify without keeping command text. Full-versus-targeted is a label, not gate evidence."""
    text = " ".join(map(str, command)) if isinstance(command, list) else str(command or "")
    if "integration_tests.py" in text or "parent-integrate test" in text:
        return "test_full"
    if re.search(r"\bpytest\b|\bselftests\b|\b(?:npm|bun|pnpm)\s+(?:run\s+)?test\b", text):
        return "test_targeted" if re.search(r"\btests?/|::|\s(?:-k|--lf|--last-failed)\b", text) else "test_full"
    return "command"


def clean_meta(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for name in META_KEYS:
        v = value.get(name)
        if isinstance(v, bool) or number(v) is not None:
            out[name] = v
        elif isinstance(v, str) and len(v) <= 160 and re.fullmatch(r"[A-Za-z0-9_.:/@+ -]*", v):
            out[name] = v
    return out


@dataclass
class Span:
    span_id: str
    session_id: str | None
    tool: str
    category: str
    operation: str
    started_at: datetime | None
    ended_at: datetime | None
    elapsed_ms: float | None
    status: str
    measurement_kind: str
    source: str
    parent_span_id: str | None = None
    exit_code: int | None = None
    method_version: int = 1
    meta: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        d = asdict(self)
        for name in ("started_at", "ended_at"):
            d[name] = d[name].isoformat() if d[name] is not None else None
        return d


def parse_record(d: Any) -> Span | None:
    if not isinstance(d, dict) or not identity(d.get("span_id")) or len(d["span_id"]) > 160:
        return None
    if d.get("category") not in CATEGORIES or d.get("status") not in STATUSES or d.get("measurement_kind") not in KINDS:
        return None
    if d.get("tool") not in ("claude", "codex", "unknown"):
        return None
    start, end = timestamp(d.get("started_at")), timestamp(d.get("ended_at"))
    if (d.get("started_at") is not None and start is None) or (d.get("ended_at") is not None and end is None):
        return None
    elapsed = number(d.get("elapsed_ms"))
    if (d.get("elapsed_ms") is not None and elapsed is None) or (start and end and end < start):
        return None
    sid, parent = identity(d.get("session_id")), identity(d.get("parent_span_id"))
    if ((d.get("session_id") is not None and sid is None) or parent == d["span_id"]
            or (d.get("parent_span_id") is not None and parent is None)):
        return None
    code = d.get("exit_code")
    if not isinstance(code, int) or isinstance(code, bool) or not -(2**31) <= code < 2**31:
        code = None
    version = d.get('method_version',1)
    if isinstance(version,bool) or not isinstance(version,int) or not 0 < version <= 1000:
        return None
    meta = clean_meta(d.get("meta"))
    # Older collectors interpreted OTLP's unset (zero) clock as the Unix epoch.
    # Normalize retained spool records too, so a replay cannot restore that date.
    if d.get("source") == "codex.otel" and end is not None and end.timestamp() == 0:
        start = end = None
        meta["clock"] = "unavailable"
    return Span(d["span_id"], sid, d["tool"], d["category"], label(d.get("operation")), start, end,
                elapsed, d["status"], d["measurement_kind"], label(d.get("source")), parent, code,
                method_version=version, meta=meta)


def native_duration(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    seconds, nanos = number(value.get("secs")), number(value.get("nanos"))
    return seconds * 1000 + nanos / 1_000_000 if seconds is not None and nanos is not None and nanos < 1_000_000_000 else None


def codex_spans(events: list[dict[str, Any]], session_id: str) -> list[Span]:
    from agent_memory import codex_sessions as C

    found: dict[str, Span] = {}
    for event in C.own_events(events):
        p = C.payload(event)
        if event.get("type") != "event_msg" or p.get("type") != "item_completed":
            continue
        item = p.get("item")
        if not isinstance(item, dict) or not identity(item.get("id")):
            continue
        kind = item.get("type")
        tools = {"CommandExecution", "McpToolCall", "DynamicToolCall", "FileChange", "WebSearch"}
        if kind not in tools | {"AgentMessage", "Reasoning"}:
            continue
        start, end = epoch_ms(p.get("started_at_ms")), epoch_ms(p.get("completed_at_ms"))
        elapsed = native_duration(item.get("duration"))
        if elapsed is None:
            elapsed = number(item.get("durationMs"))
        if end is None:
            end = timestamp(event.get("timestamp"))
        if start is None and end is not None and elapsed is not None:
            start = end - timedelta(milliseconds=elapsed)
        if start is None or end is None or end < start:
            continue
        measured = "provider_reported" if elapsed is not None else "observed"
        elapsed = elapsed if elapsed is not None else (end - start).total_seconds() * 1000
        category = "model_output" if kind in {"AgentMessage", "Reasoning"} else "tool"
        if kind == "CommandExecution":
            category = category_for_command(item.get("command"))
        status = "failed" if item.get("status") in {"Failed", "failed"} or item.get("exit_code") not in (None, 0) else "passed"
        if item.get('error') or item.get('success') is False or (isinstance(item.get('result'),dict) and item['result'].get('isError')):
            status = 'failed'
        if item.get("status") in {"Interrupted", "interrupted"}:
            status = "interrupted"
        code = item.get("exit_code")
        sid = key(session_id, "codex.item", item["id"])
        found[sid] = Span(sid, session_id, "codex", category, label(kind), start, end, elapsed, status,
                          measured, "codex.item", exit_code=code if isinstance(code, int) else None,
                          meta=clean_meta({"turn_id": p.get("turn_id"), "duration_source": "native_duration" if measured == "provider_reported" else "item_bounds"}))
    return list(found.values())


def claude_spans(events: list[dict[str, Any]], session_id: str) -> list[Span]:
    calls: dict[str, tuple[datetime, str, str]] = {}
    found: dict[str, Span] = {}
    for e in events:
        at = timestamp(e.get("timestamp"))
        msg = e.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if at is None or not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            call_id = identity(b.get("id") if b.get("type") == "tool_use" else b.get("tool_use_id"))
            if not call_id:
                continue
            if b.get("type") == "tool_use":
                name = label(b.get("name"))
                inp = b.get("input") or {}
                category = category_for_command(inp.get("command")) if name == "Bash" and isinstance(inp, dict) else "tool"
                calls[call_id] = (at, name, category)
            elif b.get("type") == "tool_result" and call_id in calls:
                start, name, category = calls[call_id]
                if at < start:
                    continue
                sid = key(session_id, "claude.tool", call_id)
                found[sid] = Span(sid, session_id, "claude", category, name, start, at,
                                  (at-start).total_seconds()*1000, "failed" if b.get("is_error") else "passed",
                                  "observed", "claude.tool", meta={"duration_source": "request_to_result"})
    return list(found.values())


def otlp_attributes(rows: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for entry in rows if isinstance(rows, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            continue
        value = entry.get("value")
        if isinstance(value, dict):
            for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
                if kind in value:
                    out[entry["key"]] = value[kind]
                    break
    return out


def otlp_session(locations: list[dict[str, Any]]) -> tuple[str | None, str]:
    """Use explicit conversation/thread identity only. Never infer an actor from
    request time, process identity, or the collector's own session environment.
    Scope attributes occur in native OTLP exporters as well as resource/record
    attributes; disagreement is a visible capture gap, not last-value-wins.
    """
    keys = ("thread.id", "thread_id", "conversation.id", "conversation_id",
            "codex.thread.id", "codex.thread_id", "codex.conversation_id")
    values: dict[str, str] = {}
    malformed = False
    for attrs in locations:
        for name in keys:
            if name not in attrs:
                continue
            raw = identity(attrs[name])
            if raw is None:
                malformed = True
                continue
            sid = raw if raw.startswith("codex:") else "codex:" + raw
            values[sid] = name
    if len(values) > 1:
        return None, "conflicting_explicit_thread_ids"
    if malformed:
        return None, "invalid_explicit_thread_id"
    if not values:
        return None, "explicit_thread_id_missing"
    sid, field_name = next(iter(values.items()))
    return sid, "otel_attribute:" + field_name


def otlp_spans(payload: Any) -> list[Span]:
    """Allowlist API measurements; discard prompts, tool arguments/results and auth data."""
    found: dict[str, Span] = {}
    def mapping(value): return value if isinstance(value, dict) else {}
    def sequence(value): return value if isinstance(value, list) else []

    for resource in sequence(mapping(payload).get("resourceLogs")):
        if not isinstance(resource, dict):
            continue
        common = otlp_attributes(mapping(resource.get("resource")).get("attributes"))
        for scope in sequence(resource.get("scopeLogs")):
            if not isinstance(scope, dict):
                continue
            scoped = otlp_attributes(mapping(scope.get("scope")).get("attributes"))
            for rec in sequence(scope.get("logRecords")):
                if not isinstance(rec, dict):
                    continue
                record_attrs = otlp_attributes(rec.get("attributes"))
                attrs = {**common, **scoped, **record_attrs}
                name = attrs.get("event.name") or attrs.get("event") or mapping(rec.get("body")).get("stringValue")
                if name not in {"codex.api_request", "codex.websocket_request"}:
                    continue
                duration = attrs.get("duration_ms")
                if duration is None and isinstance(attrs.get("duration"), str):
                    match = re.fullmatch(r"([0-9.]+)(ms|s)", attrs['duration'])
                    if match:
                        try: duration = float(match[1]) * (1000 if match[2] == 's' else 1)
                        except ValueError: duration = None
                if isinstance(duration, str):
                    try: duration = float(duration)
                    except ValueError: duration = None
                elapsed = number(duration)
                if elapsed is None:
                    continue
                end, clock, clock_key = None, 'unavailable', rec.get('timeUnixNano')
                for field_name, clock_name in (('timeUnixNano', 'provider_event_end'),
                                               ('observedTimeUnixNano', 'provider_observed')):
                    try:
                        raw_clock = rec.get(field_name)
                        nanos = int(raw_clock) if not isinstance(raw_clock, bool) else 0
                        if nanos <= 0:
                            continue
                        end = datetime.fromtimestamp(nanos / 1e9, UTC)
                    except (ValueError, TypeError, OverflowError, OSError):
                        continue
                    clock, clock_key = clock_name, raw_clock
                    break
                session, identity_basis = otlp_session([common, scoped, record_attrs])
                # Native model-catalog requests are process-wide control traffic.
                # They carry no conversation identity and are not inference time.
                catalog = attrs.get("endpoint") in ("/models", "models")
                if catalog:
                    session, identity_basis = None, "session_not_applicable"
                api_scope = "client_control" if catalog else "session" if session else "unknown"
                if clock != "provider_event_end":
                    event_clock = attrs.get("event.timestamp")
                    try:
                        observed = datetime.fromisoformat(event_clock.replace("Z", "+00:00")) if isinstance(event_clock,str) else None
                    except ValueError:
                        observed = None
                    if observed is not None and observed.tzinfo is not None:
                        end, clock = observed.astimezone(UTC), "provider_event_timestamp"
                http_status = attrs.get("http.response.status_code", attrs.get("status"))
                meta = clean_meta({"model": attrs.get("model"), "attempt": attrs.get("attempt"),
                                   "http_status": http_status, "request_id": attrs.get("request.id")})
                meta['api_scope'] = api_scope
                if catalog:
                    meta['endpoint'] = 'models'
                # A native request/trace identity permits a later exact actor
                # assignment without creating a second measurement. Anonymous
                # legacy events keep the previous key contract.
                request_id = identity(attrs.get("request.id") or attrs.get("request_id"))
                trace, native_span = identity(rec.get("traceId")), identity(rec.get("spanId"))
                native_trace = bool(trace and native_span and trace.strip('0') and native_span.strip('0'))
                span_id = (key("codex.otel.request/2", name, request_id, trace, native_span,
                               attrs.get("attempt")) if request_id or native_trace
                           else key(session, name, clock_key, rec.get("traceId"), rec.get("spanId"), elapsed, meta))
                success = attrs.get("success")
                http = str(http_status or "")
                status = ("failed" if success is False or success == "false" or (http.isdigit() and int(http)>=400)
                          else "passed" if success is True or success == "true" or (http.isdigit() and 200<=int(http)<400)
                          else "unknown")
                meta.update(duration_source='otel_duration_ms', clock=clock)
                meta["identity_source" if session else "identity_unavailable"] = identity_basis
                found[span_id] = Span(span_id, session, "codex", "unclassified" if catalog else "api", name, end-timedelta(milliseconds=elapsed) if end else None,
                                      end, elapsed, status, "provider_reported", "codex.otel", method_version=3, meta=meta)
    return list(found.values())


def union_seconds(spans: Iterable[Span]) -> float:
    intervals = sorted((s.started_at, s.ended_at) for s in spans
                       if s.started_at is not None and s.ended_at is not None and s.ended_at >= s.started_at)
    result = 0.0
    left = right = None
    for start, end in intervals:
        if left is None:
            left, right = start, end
        elif start <= right:
            right = max(right, end)
        else:
            result += (right-left).total_seconds()
            left, right = start, end
    return result + ((right-left).total_seconds() if left is not None else 0.0)
