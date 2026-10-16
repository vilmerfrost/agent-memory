"""The investigator's questions over the evidence facade (Mission 004B, brief section 9; 004C added
`repeat_failures`).

    amem evidence ask <question> [--param k=v ...] [--record] [--json]
    amem evidence questions

A question is a fixed plan of named queries (no free-form query path, as everywhere in the facade):
Supabase first, then ClickHouse, Langfuse and past.dev. Later phases (at most MAX_PHASES in all) may follow
up on what the earlier ones found (the failing traces an experiment's links name; the rule a promoted
proposal changed; the experiments that already tested a change to a repeatedly failed rule). Each step
keeps its own retrieval status, so a source that is down or unconfigured shows as `unavailable` and
never blocks the answer.

Every finding carries: the source service, the authoritative Supabase ref (an `amem:` subject ref that was
re-read from Supabase just now), the timestamp, the evidence type, measurement metadata (basis, n,
confidence or rank where the source has one) and a status:
    authoritative  the row is in Supabase itself
    linked         an external finding whose every amem: subject resolves in Supabase
    stale          an external finding with a subject that no longer resolves (not usable)
    unlinked       an external aggregate with no amem: subject (context only)

`--record` writes rsi_evidence rows only (idempotent on (system, external_ref), valid for RECORD_VALID_DAYS):
- each linked external finding that a provider answered live in this ask (a link that a Supabase step read
  back from rsi_evidence is not a new observation, so it is never re-written);
- one `answer` row (system supabase): the structured finding, i.e. the question, its params, the status per
  provider, the steps, the counts per finding status and the Supabase refs the answer rests on.
That is the only write: an answer becomes evidence for a human or the investigator to weigh, never a change
to proposals, decisions, instructions or schedules.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
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
MAX_PHASES = 4
MAX_TRACE_FOLLOWUPS = 3
MAX_EXPERIMENTS = 2             # repeat_failures: earlier experiments followed up per ask
ANSWER_KIND = "answer"
MAX_ANSWER_REFS = 50
_BASIS = {"pattern": "aggregate", "aggregate": "aggregate", "metric": "aggregate", "attempt": "record",
          "decision": "record", "experiment": "record", "change": "record", "row": "record", "eval": "eval",
          "trace": "trace", "score": "score", "decision-memory": "semantic-recall", "research-run": "measurement",
          ANSWER_KIND: "answer"}
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


def _trace_followups(done: Sequence[tuple[Step, QueryResult]]) -> list[Step]:
    """lf.trace for the failing runs among the stored lf:trace links (each trace once, at most 3)."""
    bad: list[str] = []
    for ev in _found(done, "evidence_for"):
        if ev.ref.startswith("lf:trace/") and _failed(ev.metrics) and ev.ref not in bad:
            bad.append(ev.ref)
    return [Step("lf", "trace", {"trace_id": refs.parse(ref).id}, "a failing run's trace, read live")
            for ref in bad[:MAX_TRACE_FOLLOWUPS]]


def _failed_traces(p: Mapping[str, str], done: Sequence[tuple[Step, QueryResult]], phase: int) -> list[Step]:
    exp = p["experiment"]
    if phase == 0:
        return [Step("sb", "evals_for", {"experiment": exp}, "the eval verdicts"),
                Step("sb", "evidence_for", {"ref": refs.amem("experiment", exp), "limit": "50"},
                     "the trace links stored in Supabase"),
                Step("lf", "experiment_scores", {"experiment_id": exp}, "score aggregates per arm")]
    return _trace_followups(done) if phase == 1 else []


def _after(p: Mapping[str, str], done: Sequence[tuple[Step, QueryResult]], phase: int) -> list[Step]:
    prop = p["proposal"]
    if phase == 0:
        return [Step("sb", "promotion_history", {"proposal": prop, "window_days": p.get("window_days", "14")},
                     "the timeline and the rule's deviations before vs after"),
                Step("sb", "evidence_for", {"ref": refs.amem("proposal", prop), "limit": "20"},
                     "evidence already linked to the proposal")]
    if phase > 1:
        return []
    hist = _found(done, "promotion_history")
    rule = hist[0].metrics.get("rule_key") if hist else None
    if isinstance(rule, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}", rule):
        return [Step("ch", "rule_weekly_rate", {"rule": rule, "weeks": "8"}, "the rule's weekly rate, from ClickHouse")]
    return []


def _top_rule(done: Sequence[tuple[Step, QueryResult]]) -> str | None:
    """The rule most sessions keep failing: Supabase's patterns first (sessions, then deviations), else
    ClickHouse's rule_gap rows. Only a rule key the follow-up queries accept."""
    def ok(rule: Any) -> bool:
        return isinstance(rule, str) and re.fullmatch(sb_queries.RULE_P, rule) is not None

    def num(v: Any) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    for query, sessions, n in (("recurring_deviations", "sessions", "n"), ("rule_gap", "n_sessions", "n_deviations")):
        rows = [ev.metrics for ev in _found(done, query) if ok(ev.metrics.get("rule_key"))]
        if rows:
            best = min(rows, key=lambda m: (-num(m.get(sessions)), -num(m.get(n)), m["rule_key"]))
            return str(best["rule_key"])
    return None


def _experiments_tried(done: Sequence[tuple[Step, QueryResult]]) -> list[str]:
    """Experiments named by the attempts (and by recalled decisions), in the order found."""
    out: list[str] = []
    for ev in _found(done, "attempts_for") + _found(done, "recall"):
        for s in ev.subject_refs:
            r = refs.parse(s)
            if r.kind == "experiment" and refs.EXPERIMENT_RE.fullmatch(r.id) and r.id not in out:
                out.append(r.id)
    return out[:MAX_EXPERIMENTS]


def _repeat(p: Mapping[str, str], done: Sequence[tuple[Step, QueryResult]], phase: int) -> list[Step]:
    """Mission 004C: why sessions keep failing the same rule, and whether a similar change was tested.
    0: the patterns (Supabase, with Claude vs Codex sessions) and the followed-vs-outcome gap (ClickHouse);
    1: for the top rule (or `rule`): earlier attempts, similar decisions (past.dev), the weekly rate;
    2: per experiment those attempts name: eval verdicts, stored trace links, Langfuse score aggregates;
    3: the failing runs' traces, read live."""
    d, n = p.get("days", "30"), p.get("min_n", "3")
    if phase == 0:
        gap = {"days": d, "min_n": n, **({"rule": p["rule"]} if p.get("rule") else {})}
        return [Step("sb", "recurring_deviations", {"days": d, "min_n": n},
                     "rules several sessions keep failing, with Claude vs Codex sessions"),
                Step("ch", "rule_gap", gap, "per rule: followed grade vs objective outcome")]
    if phase == 1:
        rule = p.get("rule") or _top_rule(done)
        if not rule:
            return []
        steps = [Step("sb", "attempts_for", {"rule": rule}, "proposals and improve edits that already targeted the rule"),
                 Step("ch", "rule_weekly_rate", {"rule": rule, "weeks": "8"}, "the rule's weekly rate, from ClickHouse")]
        words = _words(rule)
        if words:
            steps.append(Step("pd", "recall", {"query": words, "limit": "10"}, "decision memory: similar past decisions"))
        return steps
    if phase == 2:
        steps = []
        for exp in _experiments_tried(done):
            steps += [Step("sb", "evals_for", {"experiment": exp}, "the eval verdicts of an earlier test"),
                      Step("sb", "evidence_for", {"ref": refs.amem("experiment", exp), "limit": "50"},
                           "the sandbox trace links stored in Supabase"),
                      Step("lf", "experiment_scores", {"experiment_id": exp}, "score aggregates per arm")]
        return steps
    return _trace_followups(done) if phase == 3 else []


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
    Question("repeat_failures", "Why do sessions keep failing the same instruction, and was a similar change tested?",
             {"days": sb_queries.DAYS_P, "min_n": sb_queries.N_P, "rule": sb_queries.RULE_P}, _repeat),
)}


@dataclass
class Answer:
    question: str
    text: str
    params: dict[str, str]
    steps: list[tuple[Step, QueryResult]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    asked_at: str = ""

    def providers(self) -> dict[str, str]:
        """One status per service over its steps: the shared status, or `partial` when its steps differ."""
        seen: dict[str, set[str]] = {}
        for st, res in self.steps:
            seen.setdefault(SERVICE.get(st.source, st.source), set()).add(res.status)
        return {svc: next(iter(s)) if len(s) == 1 else "partial" for svc, s in seen.items()}

    def to_json(self) -> dict[str, Any]:
        return {"question": self.question, "text": self.text, "params": self.params, "asked_at": self.asked_at,
                "providers": self.providers(),
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
    for phase in range(MAX_PHASES):
        for st in q.plan(p, ans.steps, phase):
            r = retrievers.get(st.source)
            res = run_query(r, st.query, st.params) if r is not None else QueryResult(st.source, "unavailable",
                                                                                      detail="no retriever")
            ans.steps.append((st, res))
    with connect() as conn:
        ans.findings = [finding(conn, st, ev) for st, res in ans.steps if res.status == "ok" for ev in res.refs]
    return ans


def answer_row(ans: Answer) -> dict[str, Any]:
    """The structured finding as rsi_evidence fields: the Supabase refs the answer rests on (authoritative and
    linked findings), the status per provider, the steps, and the first finding of each step that answered.
    Numbers, labels and refs only. The external_ref is a digest of (question, params, findings, day), so the
    same ask on the same day updates one row. An earlier answer that the ask read back (sb.evidence_for) is
    shown as a finding but left out of the digest, the subjects and `top`, so answers never cite answers."""
    own = [f for f in ans.findings if f["type"] != ANSWER_KIND]
    subjects: list[str] = []
    for f in own:
        if f["status"] in ("authoritative", "linked"):
            subjects += [r for r in f["supabase_refs"] if r not in subjects and refs.is_ref(r)
                         and refs.parse(r).scheme == "amem"]
    top: list[dict[str, Any]] = []
    for f in own:
        if not any(t["step"] == f["step"] for t in top):
            top.append({"step": f["step"], "ref": f["ref"], "type": f["type"], "status": f["status"],
                        "measure": f["measure"]})
    providers = ans.providers()
    metrics = {"question": ans.question, "params": dict(ans.params), "asked_at": ans.asked_at, "providers": providers,
               "steps": [{"step": f"{st.source}.{st.query}", "status": res.status, "found": len(res.refs)}
                         for st, res in ans.steps],
               "findings": len(ans.findings), "by_status": dict(Counter(f["status"] for f in ans.findings)),
               "subjects_total": len(subjects), "top": top}
    digest = hashlib.sha256(json.dumps({"q": ans.question, "p": dict(ans.params), "day": ans.asked_at[:10],
                                        "refs": sorted(f["ref"] for f in own)},
                                       sort_keys=True).encode("utf-8")).hexdigest()[:24]
    summary = (f"{ans.text} ({ans.question}): {len(ans.findings)} finding(s); "
               + ", ".join(f"{svc}={st}" for svc, st in providers.items()))
    return {"system": "supabase", "kind": ANSWER_KIND, "subject_refs": subjects[:MAX_ANSWER_REFS],
            "summary": summary, "metrics": metrics, "external_ref": f"answer:{ans.question}/{digest}"}


def record_findings(conn: psycopg.Connection, ans: Answer, *, valid_days: int = RECORD_VALID_DAYS) -> list[str]:
    """Store the live linked external findings and one answer row in rsi_evidence (the only write;
    idempotent). Returns their ids, the answer row's last.

    Not written (Mission 004C fixes):
    - a finding from a Supabase step (`sb.evidence_for` reads back stored lf:/ch: links): re-recording it would
      overwrite the link's metrics and move its observed_at without any provider having been asked;
    - a past.dev recall: its ref `past:<amem ref>` is the past.dev sink's own link key, so recording it would
      replace the sink's sha256 and make the next sync re-send the record. A recall only points back at a
      Supabase row; the answer row keeps it (its `top` and subjects)."""
    from agent_memory import rsi_approval   # lazy: rsi_approval imports a lot

    ids = []
    for f in ans.findings:
        if f["source"] in ("sb", "pd") or f["step"].startswith("sb.") or f["status"] != "linked":
            continue
        ev = EvidenceRef(f["ref"], f["source"], f["type"], list(f["supabase_refs"]),
                         {**f["metrics"], "question": ans.question}, f["summary"], f["timestamp"])
        ids.append(record(conn, ev, valid_days=valid_days))
    ids.append(rsi_approval.add_evidence(conn, **answer_row(ans), valid_days=valid_days))
    conn.commit()
    return ids
