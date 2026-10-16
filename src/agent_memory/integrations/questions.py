"""The investigator's six questions over the evidence facade (Mission 004B, brief section 9).

    amem evidence ask <question> [--param k=v ...] [--record] [--json]
    amem evidence questions

A question is a fixed plan of named queries (no free-form query path, as everywhere in the facade):
Supabase first, then ClickHouse, Langfuse and past.dev. A second phase may follow up on what the first
found (the failing traces an experiment's links name; the rule a promoted proposal changed). Each step
keeps its own retrieval status, so a source that is down or unconfigured shows as `unavailable` and
never blocks the answer.

Every finding carries: the source service, the authoritative Supabase ref (an `amem:` subject ref that was
re-read from Supabase just now), the timestamp, the evidence type, measurement metadata (basis, n,
confidence or rank where the source has one) and a status:
    authoritative  the row is in Supabase itself
    linked         an external finding whose every amem: subject resolves in Supabase
    stale          an external finding with a subject that no longer resolves (not usable)
    unlinked       an external aggregate with no amem: subject (context only)

`--record` stores linked external findings in rsi_evidence (idempotent on (system, external_ref), valid
for RECORD_VALID_DAYS). That is the only write: a provider's answer becomes evidence for a human or the
investigator to weigh, never a change to proposals, decisions, instructions or schedules.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg

from agent_memory.integrations import refs, sb_queries
from agent_memory.integrations.evidence import EvidenceRef, QueryResult, Retriever, exists, record, run_query
from agent_memory.integrations.pastdev import QUERY_RE

SERVICE = {"sb": "supabase", "ch": "clickhouse", "lf": "langfuse", "pd": "pastdev", "ncm": "ncm"}
RECORD_VALID_DAYS = 30
MAX_TRACE_FOLLOWUPS = 3
_BASIS = {"pattern": "aggregate", "aggregate": "aggregate", "metric": "aggregate", "attempt": "record",
          "decision": "record", "experiment": "record", "change": "record", "row": "record", "eval": "eval",
          "trace": "trace", "score": "score", "decision-memory": "semantic-recall", "research-run": "measurement"}
_N_KEYS = ("n", "n_sessions", "n_deviations", "traces", "n_runs", "sessions")


@dataclass(frozen=True)
class Step:
    source: str
    query: str
    params: Mapping[str, str]
    why: str


Plan = Callable[[Mapping[str, str], Sequence[tuple[Step, QueryResult]], int], list[Step]]


@dataclass(frozen=True)
class Question:
    name: str
    text: str
    params: Mapping[str, str]                    # param -> regex
    plan: Plan
    required: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()                 # at least one of these

    def check(self, params: Mapping[str, str]) -> dict[str, str]:
        out = {}
        for k, v in params.items():
            pat = self.params.get(k)
            if pat is None:
                raise ValueError(f"{self.name}: unknown param {k!r}")
            if not re.fullmatch(pat, v):
                raise ValueError(f"{self.name}: bad value for {k!r}")
            out[k] = v
        missing = [k for k in self.required if k not in out]
        if missing:
            raise ValueError(f"{self.name}: missing param(s) {', '.join(missing)}")
        if self.any_of and not any(k in out for k in self.any_of):
            raise ValueError(f"{self.name}: give one of {', '.join(self.any_of)}")
        return out


def _found(done: Sequence[tuple[Step, QueryResult]], query: str) -> list[EvidenceRef]:
    return [ev for st, res in done if st.query == query and res.status == "ok" for ev in res.refs]


def _words(rule: str) -> str | None:
    """A rule key as a recall query: its rule id's words (`claude-md:global:zsh-quote-globs` -> `zsh quote globs`)."""
    q = " ".join(re.split(r"[-_.]+", rule.rpartition(":")[2])).strip()
    return q if q and re.fullmatch(QUERY_RE, q) else None


def _failed(m: Mapping[str, Any]) -> bool:
    return (m.get("check_passed") is False or bool(m.get("tool_errors")) or bool(m.get("timed_out"))
            or bool(m.get("is_error")) or m.get("exit_code") not in (0, None))


def _recurring(p: Mapping[str, str], done: Sequence[tuple[Step, QueryResult]], phase: int) -> list[Step]:
    if phase:
        return []
    d, n = p.get("days", "30"), p.get("min_n", "3")
    return [Step("sb", "recurring_deviations", {"days": d, "min_n": n}, "deviation patterns, counted in Supabase"),
            Step("ch", "rule_gap", {"days": d, "min_n": n}, "per rule: followed grade vs objective outcome"),
            Step("ch", "routine_streaks", {"days": d}, "routines that fail in a row")]


def _best(p: Mapping[str, str], done: Sequence[tuple[Step, QueryResult]], phase: int) -> list[Step]:
    if phase:
        return []
    d, n = p.get("days", "30"), p.get("min_n", "3")
    steps = [Step("sb", "eval_by_model", {"days": p.get("days", "90")}, "eval verdicts per suite x model x effort"),
             Step("ch", "skill_outcomes", {"days": d, "min_n": n}, "skill x model: share of sessions that shipped"),
             Step("ch", "tokens_by_outcome", {"days": d}, "tokens and time per outcome x model x effort")]
    if p.get("experiment"):
        steps.append(Step("lf", "experiment_scores", {"experiment_id": p["experiment"]}, "the experiment's arms"))
    return steps


def _attempted(p: Mapping[str, str], done: Sequence[tuple[Step, QueryResult]], phase: int) -> list[Step]:
    if phase:
        return []
    steps = []
    if p.get("rule") or p.get("artifact"):
        steps.append(Step("sb", "attempts_for", {k: p[k] for k in ("rule", "artifact") if k in p},
                          "proposals and improve edits for this rule or artifact"))
    text = p.get("query") or (_words(p["rule"]) if p.get("rule") else None)
    if text:
        steps.append(Step("pd", "recall", {"query": text, "limit": "10"}, "decision memory: similar past decisions"))
    return steps


def _why_rejected(p: Mapping[str, str], done: Sequence[tuple[Step, QueryResult]], phase: int) -> list[Step]:
    if phase:
        return []
    steps = [Step("sb", "decisions_for", {k: p[k] for k in ("experiment", "proposal") if k in p},
                  "the decisions, their reasons and the experiment's verdict")]
    if p.get("experiment"):
        steps += [Step("lf", "experiment_scores", {"experiment_id": p["experiment"]}, "what the sandbox arms scored"),
                  Step("pd", "recall", {"query": f"why was experiment {p['experiment']} rejected", "limit": "10"},
                       "decision memory about this experiment")]
    return steps


def _failed_traces(p: Mapping[str, str], done: Sequence[tuple[Step, QueryResult]], phase: int) -> list[Step]:
    exp = p["experiment"]
    if phase == 0:
        return [Step("sb", "evals_for", {"experiment": exp}, "the eval verdicts"),
                Step("sb", "evidence_for", {"ref": refs.amem("experiment", exp), "limit": "50"},
                     "the trace links stored in Supabase"),
                Step("lf", "experiment_scores", {"experiment_id": exp}, "score aggregates per arm")]
    bad = [ev for ev in _found(done, "evidence_for") if ev.ref.startswith("lf:trace/") and _failed(ev.metrics)]
    return [Step("lf", "trace", {"trace_id": refs.parse(ev.ref).id}, "a failing run's trace, read live")
            for ev in bad[:MAX_TRACE_FOLLOWUPS]]


def _after(p: Mapping[str, str], done: Sequence[tuple[Step, QueryResult]], phase: int) -> list[Step]:
    prop = p["proposal"]
    if phase == 0:
        return [Step("sb", "promotion_history", {"proposal": prop, "window_days": p.get("window_days", "14")},
                     "the timeline and the rule's deviations before vs after"),
                Step("sb", "evidence_for", {"ref": refs.amem("proposal", prop), "limit": "20"},
                     "evidence already linked to the proposal")]
    hist = _found(done, "promotion_history")
    rule = hist[0].metrics.get("rule_key") if hist else None
    if isinstance(rule, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}", rule):
        return [Step("ch", "rule_weekly_rate", {"rule": rule, "weeks": "8"}, "the rule's weekly rate, from ClickHouse")]
    return []


QUESTIONS: dict[str, Question] = {q.name: q for q in (
    Question("recurring_failures", "What recurring failure patterns exist?",
             {"days": sb_queries.DAYS_P, "min_n": sb_queries.N_P}, _recurring),
    Question("best_combinations", "Which model or skill combinations perform better?",
             {"days": sb_queries.DAYS_P, "min_n": sb_queries.N_P, "experiment": sb_queries.EXP_P}, _best),
    Question("attempted_before", "Has this improvement been attempted before?",
             {"rule": sb_queries.RULE_P, "artifact": sb_queries.HASH_P, "query": QUERY_RE}, _attempted,
             any_of=("rule", "artifact", "query")),
    Question("why_rejected", "Why was a previous experiment rejected?",
             {"experiment": sb_queries.EXP_P, "proposal": sb_queries.UUID_P}, _why_rejected,
             any_of=("experiment", "proposal")),
    Question("failed_eval_traces", "Which sandbox traces explain a failed evaluation?",
             {"experiment": sb_queries.EXP_P}, _failed_traces, required=("experiment",)),
    Question("after_approval", "What changed after an approved improvement?",
             {"proposal": sb_queries.UUID_P, "window_days": sb_queries.DAYS_P}, _after, required=("proposal",)),
)}


@dataclass
class Answer:
    question: str
    text: str
    params: dict[str, str]
    steps: list[tuple[Step, QueryResult]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    asked_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"question": self.question, "text": self.text, "params": self.params, "asked_at": self.asked_at,
                "steps": [{"source": st.source, "service": SERVICE.get(st.source, st.source), "query": st.query,
                           "params": dict(st.params), "why": st.why, "status": res.status, "detail": res.detail,
                           "found": len(res.refs)} for st, res in self.steps],
                "findings": self.findings}


def _measure(ev: EvidenceRef) -> dict[str, Any]:
    m = ev.metrics
    out: dict[str, Any] = {"basis": _BASIS.get(ev.kind, ev.kind)}
    n = next((m[k] for k in _N_KEYS if isinstance(m.get(k), int)), None)
    if n is not None:
        out["n"] = n
    for k in ("confidence", "jev_confidence", "rank", "check_passed_rate", "done_rate", "good_rate", "rate", "gap"):
        if isinstance(m.get(k), int | float) and not isinstance(m.get(k), bool):
            out[k] = m[k]
    return out


def finding(conn: psycopg.Connection, step: Step, ev: EvidenceRef) -> dict[str, Any]:
    """One result in the brief's shape. External subjects are re-read from Supabase now."""
    if ev.source == "sb":
        resolved, status = list(ev.subject_refs) or [ev.ref], "authoritative"
    else:
        resolved = [s for s in ev.subject_refs if exists(conn, s)]
        status = ("linked" if len(resolved) == len(ev.subject_refs) else "stale") if ev.subject_refs else "unlinked"
    return {"source": ev.source, "service": SERVICE.get(ev.source, ev.source), "step": f"{step.source}.{step.query}",
            "ref": ev.ref, "supabase_ref": resolved[0] if resolved else None, "supabase_refs": resolved,
            "timestamp": ev.observed_at, "type": ev.kind, "measure": _measure(ev), "status": status,
            "metrics": ev.metrics, "summary": ev.summary}


def ask(name: str, params: Mapping[str, str], retrievers: Mapping[str, Retriever | None],
        connect: Callable[[], psycopg.Connection], *, now: Callable[[], datetime] | None = None) -> Answer:
    q = QUESTIONS[name]
    p = q.check(params)
    ans = Answer(q.name, q.text, p, asked_at=(now or (lambda: datetime.now(UTC)))().isoformat(timespec="seconds"))
    for phase in (0, 1):
        for st in q.plan(p, ans.steps, phase):
            r = retrievers.get(st.source)
            res = run_query(r, st.query, st.params) if r is not None else QueryResult(st.source, "unavailable",
                                                                                      detail="no retriever")
            ans.steps.append((st, res))
    with connect() as conn:
        ans.findings = [finding(conn, st, ev) for st, res in ans.steps if res.status == "ok" for ev in res.refs]
    return ans


def record_findings(conn: psycopg.Connection, ans: Answer, *, valid_days: int = RECORD_VALID_DAYS) -> list[str]:
    """Store linked external findings as rsi_evidence rows (the only write; idempotent). Returns their ids."""
    ids = []
    for f in ans.findings:
        if f["source"] == "sb" or f["status"] != "linked":
            continue
        ev = EvidenceRef(f["ref"], f["source"], f["type"], list(f["supabase_refs"]),
                         {**f["metrics"], "question": ans.question}, f["summary"], f["timestamp"])
        ids.append(record(conn, ev, valid_days=valid_days))
    conn.commit()
    return ids
