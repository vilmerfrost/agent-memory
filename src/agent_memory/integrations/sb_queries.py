"""Supabase named queries for the investigator's questions (Mission 004B; see questions.py).

Supabase is the authority, so these answer from the rows themselves: nothing leaves the database and
every result's ref is an `amem:` ref. They join the two named queries of evidence.SupabaseRetriever
(`ref`, `evidence_for`) under source `sb`. Free text (a proposal title, a decision reason) is scrubbed
(sanitize.scrub_text) and cut before it is shown; numbers and labels are the measurement.

    recurring_deviations  rules with at least min_n deviations in prod sessions in the last `days`
    attempts_for          rsi proposals (via their candidate's rule_key) and improve edits for one rule,
                          or the proposals for one artifact
    decisions_for         an experiment's status and verdict, and every decision on its proposals (or on one)
    evals_for             rsi_eval_results for one experiment or artifact
    eval_by_model         eval verdicts per suite x model x effort in the last `days`
    promotion_history     one proposal's timeline, and its rule's deviations in equal windows before and after
                          the promotion (or the decision when it was never promoted)

A rule key is `<instruction_source>:<rule_id>`, as in ClickHouse (`claude-md:global:zsh-quote-globs`): rule
ids are slugs, so the last ':' splits it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import psycopg

from agent_memory.integrations import refs
from agent_memory.integrations.evidence import EvidenceRef, NamedQuery
from agent_memory.integrations.sanitize import scrub_text

RULE_P = r"[A-Za-z0-9][A-Za-z0-9._@-]{0,99}(:[A-Za-z0-9][A-Za-z0-9._@-]{0,99}){1,3}"
EXP_P = refs.EXPERIMENT_RE.pattern
UUID_P = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
HASH_P = r"[0-9a-f]{64}"
DAYS_P = r"[1-9][0-9]{0,2}"
N_P = r"[1-9][0-9]{0,2}"
TEXT_MAX = 160
PROD = "coalesce(s.env, 'prod') not in ('sandbox', 'eval')"

SPECS: dict[str, NamedQuery] = {
    "recurring_deviations": NamedQuery("recurring_deviations", {"days": DAYS_P, "min_n": N_P, "limit": N_P},
                                       "rules with >= min_n deviations in prod sessions (newest deviation as ref)"),
    "attempts_for": NamedQuery("attempts_for", {"rule": RULE_P, "artifact": HASH_P, "limit": N_P},
                               "proposals and improve edits for a rule key, or proposals for an artifact"),
    "decisions_for": NamedQuery("decisions_for", {"experiment": EXP_P, "proposal": UUID_P, "limit": N_P},
                                "an experiment's status/verdict and the decisions on its proposals"),
    "evals_for": NamedQuery("evals_for", {"experiment": EXP_P, "artifact": HASH_P, "limit": N_P},
                            "eval results (suite, model, effort, verdict) for an experiment or artifact"),
    "eval_by_model": NamedQuery("eval_by_model", {"days": DAYS_P, "limit": N_P},
                                "eval verdict counts per suite x model x effort"),
    "promotion_history": NamedQuery("promotion_history", {"proposal": UUID_P, "window_days": DAYS_P},
                                    "a proposal's timeline and its rule's deviations before vs after", ("proposal",)),
}


def _text(v: Any) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    return scrub_text(v)[0][:TEXT_MAX]


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


def _split_rule(rule: str) -> tuple[str, str]:
    source, _, rule_id = rule.rpartition(":")
    return source, rule_id


def _clean(m: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in m.items():
        if v is None:
            continue
        if isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, float):
            v = round(v, 4)
        out[k] = v
    return out


def recurring_deviations(conn: psycopg.Connection, p: Mapping[str, str]) -> list[EvidenceRef]:
    rows = conn.execute(
        f"""select d.instruction_source || ':' || coalesce(d.rule_id, 'unknown') as rule_key, count(*) as n,
                   count(distinct d.session_id) as sessions, avg(d.followed_grade) as followed_avg,
                   avg(d.objective_result) as objective_avg, max(d.created_at) as last_at,
                   (array_agg(d.id::text order by d.created_at desc))[1:5] as sample_ids
            from deviations d left join sessions s on s.session_id = d.session_id
            where d.created_at >= now() - make_interval(days => %s) and {PROD}
            group by 1 having count(*) >= %s order by n desc, rule_key limit %s""",
        (int(p.get("days", "30")), int(p.get("min_n", "3")), int(p.get("limit", "20")))).fetchall()
    out = []
    for rule_key, n, sessions, followed, objective, last_at, samples in rows:
        subjects = [refs.amem("deviation", i) for i in samples]
        out.append(EvidenceRef(subjects[0], "sb", "pattern", subjects,
                               _clean({"rule_key": rule_key, "n": n, "sessions": sessions, "followed_avg": followed,
                                       "objective_avg": objective, "window_days": int(p.get("days", "30"))}),
                               None, _iso(last_at)))
    return out


def attempts_for(conn: psycopg.Connection, p: Mapping[str, str]) -> list[EvidenceRef]:
    if not (p.get("rule") or p.get("artifact")):
        raise ValueError("attempts_for: give rule or artifact")
    limit = int(p.get("limit", "20"))
    out: list[EvidenceRef] = []
    rows = conn.execute(
        """select p.id::text, p.status, p.confidence, p.title, p.result, p.experiment_id, p.artifact_hash, c.rule_key,
                  p.created_at, p.decided_at, p.promoted_at, p.reverted_at
           from rsi_proposals p left join rsi_candidates c on c.id = p.candidate_id
           where (%(rule)s::text is not null and c.rule_key = %(rule)s) or (%(art)s::text is not null and p.artifact_hash = %(art)s)
           order by p.created_at desc limit %(limit)s""",
        {"rule": p.get("rule"), "art": p.get("artifact"), "limit": limit}).fetchall()
    for pid, status, conf, title, result, exp, art, rule_key, created, decided, promoted, reverted in rows:
        subjects = [refs.amem("proposal", pid)] + ([refs.amem("experiment", exp)] if exp else [])
        out.append(EvidenceRef(refs.amem("proposal", pid), "sb", "attempt", subjects,
                               _clean({"status": status, "confidence": conf, "result": result, "rule_key": rule_key,
                                       "artifact_hash": art, "created_at": created, "decided_at": decided,
                                       "promoted_at": promoted, "reverted_at": reverted}),
                               _text(title), _iso(created)))
    if p.get("rule"):
        source, rule_id = _split_rule(p["rule"])
        rows = conn.execute(
            """select id::text, status, verdict, jev_confidence, rule_title, created_at, pushed_at, reverted_at
               from edits where instruction_source = %s and rule_id = %s order by created_at desc limit %s""",
            (source, rule_id, limit)).fetchall()
        for eid, status, verdict, conf, title, created, pushed, reverted in rows:
            ref = refs.amem("edit", eid)
            out.append(EvidenceRef(ref, "sb", "attempt", [ref],
                                   _clean({"status": status, "verdict": verdict, "confidence": conf,
                                           "rule_key": p["rule"], "created_at": created, "pushed_at": pushed,
                                           "reverted_at": reverted}),
                                   _text(title), _iso(created)))
    return out[:limit]


def decisions_for(conn: psycopg.Connection, p: Mapping[str, str]) -> list[EvidenceRef]:
    exp, prop = p.get("experiment"), p.get("proposal")
    if not (exp or prop):
        raise ValueError("decisions_for: give experiment or proposal")
    out: list[EvidenceRef] = []
    if exp:
        row = conn.execute("select id, status, verdict, kind, created_at, finished_at from rsi_experiments where id = %s",
                           (exp,)).fetchone()
        if row:
            ref = refs.amem("experiment", row[0])
            out.append(EvidenceRef(ref, "sb", "experiment", [ref],
                                   _clean({"status": row[1], "verdict": row[2], "kind": row[3], "created_at": row[4],
                                           "finished_at": row[5]}), None, _iso(row[5] or row[4])))
    rows = conn.execute(
        """select d.id::text, d.action, d.source, d.verified, d.reason, d.decided_at, p.id::text, p.status, p.title,
                  p.experiment_id, c.status, c.decided_reason
           from rsi_decisions d join rsi_proposals p on p.id = d.proposal_id
           left join rsi_candidates c on c.id = p.candidate_id
           where (%(exp)s::text is not null and p.experiment_id = %(exp)s) or (%(prop)s::text is not null and p.id::text = %(prop)s)
           order by d.decided_at desc limit %(limit)s""",
        {"exp": exp, "prop": prop, "limit": int(p.get("limit", "20"))}).fetchall()
    for did, action, source, verified, reason, at, pid, pstatus, title, pexp, cstatus, creason in rows:
        subjects = [refs.amem("decision", did), refs.amem("proposal", pid)]
        if pexp:
            subjects.append(refs.amem("experiment", pexp))
        why = _text(reason) or _text(creason)
        out.append(EvidenceRef(refs.amem("decision", did), "sb", "decision", subjects,
                               _clean({"action": action, "source": source, "verified": verified,
                                       "proposal_status": pstatus, "candidate_status": cstatus,
                                       "proposal_title": _text(title), "has_reason": why is not None}),
                               why, _iso(at)))
    return out


def evals_for(conn: psycopg.Connection, p: Mapping[str, str]) -> list[EvidenceRef]:
    exp, art = p.get("experiment"), p.get("artifact")
    if not (exp or art):
        raise ValueError("evals_for: give experiment or artifact")
    rows = conn.execute(
        """select id::text, artifact_hash, experiment_id, suite, suite_version, model, effort, verdict, created_at
           from rsi_eval_results
           where (%(exp)s::text is not null and experiment_id = %(exp)s) or (%(art)s::text is not null and artifact_hash = %(art)s)
           order by created_at desc limit %(limit)s""",
        {"exp": exp, "art": art, "limit": int(p.get("limit", "50"))}).fetchall()
    out = []
    for eid, art_, exp_, suite, version, model, effort, verdict, created in rows:
        subjects = [refs.amem("eval_result", eid), refs.amem("artifact", art_)]
        if exp_:
            subjects.append(refs.amem("experiment", exp_))
        out.append(EvidenceRef(refs.amem("eval_result", eid), "sb", "eval", subjects,
                               _clean({"suite": suite, "suite_version": version, "model": model, "effort": effort,
                                       "verdict": verdict, "failed": verdict in ("fail", "worse")}),
                               None, _iso(created)))
    return out


def eval_by_model(conn: psycopg.Connection, p: Mapping[str, str]) -> list[EvidenceRef]:
    rows = conn.execute(
        """select suite, model, effort, count(*) as n,
                  count(*) filter (where verdict in ('pass', 'improves')) as good,
                  count(*) filter (where verdict in ('fail', 'worse')) as bad, max(created_at),
                  (array_agg(id::text order by created_at desc))[1:5]
           from rsi_eval_results where created_at >= now() - make_interval(days => %s)
           group by suite, model, effort order by n desc limit %s""",
        (int(p.get("days", "90")), int(p.get("limit", "50")))).fetchall()
    out = []
    for suite, model, effort, n, good, bad, last, samples in rows:
        subjects = [refs.amem("eval_result", i) for i in samples]
        out.append(EvidenceRef(subjects[0], "sb", "aggregate", subjects,
                               _clean({"suite": suite, "model": model, "effort": effort, "n": n, "good": good,
                                       "bad": bad, "good_rate": round(good / n, 4) if n else None}),
                               None, _iso(last)))
    return out


def promotion_history(conn: psycopg.Connection, p: Mapping[str, str]) -> list[EvidenceRef]:
    window = int(p.get("window_days", "14"))
    row = conn.execute(
        """select p.id::text, p.status, p.experiment_id, p.title, c.rule_key, p.created_at, p.decided_at, p.promoted_at,
                  p.reverted_at, p.result
           from rsi_proposals p left join rsi_candidates c on c.id = p.candidate_id where p.id::text = %s""",
        (p["proposal"],)).fetchone()
    if not row:
        return []
    pid, status, exp, title, rule_key, created, decided, promoted, reverted, result = row
    m: dict[str, Any] = {"status": status, "rule_key": rule_key, "created_at": created, "decided_at": decided,
                         "promoted_at": promoted, "reverted_at": reverted, "result": result, "window_days": window}
    pivot = promoted or decided
    if rule_key and pivot:
        source, rule_id = _split_rule(rule_key)
        counts = conn.execute(
            f"""select count(*) filter (where d.created_at >= %(t)s - make_interval(days => %(w)s) and d.created_at < %(t)s),
                       count(*) filter (where d.created_at >= %(t)s and d.created_at < %(t)s + make_interval(days => %(w)s)),
                       count(distinct s.session_id) filter (where s.created_at >= %(t)s - make_interval(days => %(w)s)
                                                            and s.created_at < %(t)s),
                       count(distinct s.session_id) filter (where s.created_at >= %(t)s
                                                            and s.created_at < %(t)s + make_interval(days => %(w)s))
                from sessions s left join deviations d on d.session_id = s.session_id
                     and d.instruction_source = %(src)s and d.rule_id = %(rid)s
                where {PROD} and s.created_at >= %(t)s - make_interval(days => %(w)s)
                  and s.created_at < %(t)s + make_interval(days => %(w)s)""",
            {"t": pivot, "w": window, "src": source, "rid": rule_id}).fetchone()
        before, after, s_before, s_after = (int(x or 0) for x in counts)
        observed = (datetime.now(pivot.tzinfo) - pivot).days
        m.update({"pivot": "promoted_at" if promoted else "decided_at", "deviations_before": before,
                  "deviations_after": after, "sessions_before": s_before, "sessions_after": s_after,
                  "rate_before": round(before / s_before, 4) if s_before else None,
                  "rate_after": round(after / s_after, 4) if s_after else None,
                  "after_days_observed": max(0, min(observed, window))})
    subjects = [refs.amem("proposal", pid)] + ([refs.amem("experiment", exp)] if exp else [])
    return [EvidenceRef(refs.amem("proposal", pid), "sb", "change", subjects, _clean(m), _text(title),
                        _iso(promoted or decided or created))]


RUN: dict[str, Callable[[psycopg.Connection, Mapping[str, str]], list[EvidenceRef]]] = {
    "recurring_deviations": recurring_deviations, "attempts_for": attempts_for, "decisions_for": decisions_for,
    "evals_for": evals_for, "eval_by_model": eval_by_model, "promotion_history": promotion_history,
}
