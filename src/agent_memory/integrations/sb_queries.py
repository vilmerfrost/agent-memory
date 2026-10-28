"""Supabase named queries for the investigator's questions (Mission 004B; see questions.py).

Supabase is the authority, so these answer from the rows themselves: nothing leaves the database and
every result's ref is an `amem:` ref. They join the two named queries of evidence.SupabaseRetriever
(`ref`, `evidence_for`) under source `sb`. Free text (a proposal title, a decision reason) is scrubbed
(sanitize.scrub_text) and cut before it is shown; numbers and labels are the measurement.

    recurring_deviations  rules with at least min_n deviations in prod sessions in the last `days` (and how many
                          of those sessions were Claude and how many Codex)
    attempts_for          rsi proposals (via their candidate's rule_key) and improve edits for one rule,
                          or the proposals for one artifact
    decisions_for         an experiment's status and verdict, and every decision on its proposals (or on one)
    evals_for             rsi_eval_results for one experiment or artifact
    eval_by_model         eval verdicts per suite x model x effort in the last `days`
    promotion_history     one proposal's timeline, and its rule's deviations in equal windows before and after
                          the promotion (or the decision when it was never promoted)

Mission 005 (evidence packs, rsi_evidence_pack.py), one rule at a time:
    rule_pattern          one rule's deviations in prod sessions in the last `days` (n, sessions per tool, grades)
    jev_for               the Jev evaluations of a rule: its deviations' verdicts and confidence, its candidates'
                          Jev calls, and for a routine rule (`routine:<name>:<rule>`) the routine runs' Jev scores
    experiments_for       experiments that tested a change to a rule (or a candidate key): status, verdict and
                          eval verdict counts

A rule key is `<instruction_source>:<rule_id>`, as in ClickHouse (`claude-md:global:zsh-quote-globs`): rule
ids are slugs, so the last ':' splits it.
"""

from __future__ import annotations

import re
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
KEY_P = r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}"     # a candidate dedupe key (Mission 005)
LABEL_P = r"[A-Za-z0-9][A-Za-z0-9._:@+=-]{0,79}"   # a label read back from a jsonb or text column (Mission 005)
TEXT_MAX = 160
PROD = "coalesce(s.env, 'prod') not in ('sandbox', 'eval')"

SPECS: dict[str, NamedQuery] = {
    "recurring_deviations": NamedQuery("recurring_deviations", {"days": DAYS_P, "min_n": N_P, "limit": N_P},
                                       "rules with >= min_n deviations in prod sessions, sessions per tool "
                                       "(newest deviation as ref)"),
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
    # Mission 005: one rule's evidence for an evidence pack
    "rule_pattern": NamedQuery("rule_pattern", {"rule": RULE_P, "days": DAYS_P},
                               "one rule's deviations in prod sessions: n, sessions per tool, grades", ("rule",)),
    "jev_for": NamedQuery("jev_for", {"rule": RULE_P, "days": DAYS_P, "limit": N_P},
                          "Jev evaluations of a rule: deviation verdicts, candidate calls, routine run scores",
                          ("rule",)),
    "experiments_for": NamedQuery("experiments_for", {"rule": RULE_P, "key": KEY_P, "limit": N_P},
                                  "experiments that tested a change to a rule or candidate: verdict, eval counts"),
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
    """Mission 004C: also the sessions per tool (Claude vs Codex) that hit the rule."""
    rows = conn.execute(
        f"""select d.instruction_source || ':' || coalesce(d.rule_id, 'unknown') as rule_key, count(*) as n,
                   count(distinct d.session_id) as sessions, avg(d.followed_grade) as followed_avg,
                   avg(d.objective_result) as objective_avg, max(d.created_at) as last_at,
                   (array_agg(d.id::text order by d.created_at desc))[1:5] as sample_ids,
                   count(distinct d.session_id) filter (where s.tool = 'claude') as claude_sessions,
                   count(distinct d.session_id) filter (where s.tool = 'codex') as codex_sessions
            from deviations d left join sessions s on s.session_id = d.session_id
            where d.created_at >= now() - make_interval(days => %s) and {PROD}
            group by 1 having count(*) >= %s order by n desc, rule_key limit %s""",
        (int(p.get("days", "30")), int(p.get("min_n", "3")), int(p.get("limit", "20")))).fetchall()
    out = []
    for rule_key, n, sessions, followed, objective, last_at, samples, claude, codex in rows:
        subjects = [refs.amem("deviation", i) for i in samples]
        out.append(EvidenceRef(subjects[0], "sb", "pattern", subjects,
                               _clean({"rule_key": rule_key, "n": n, "sessions": sessions, "claude_sessions": claude,
                                       "codex_sessions": codex, "followed_avg": followed,
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
        """select r.id::text, r.artifact_hash, r.experiment_id, r.suite, r.suite_version, r.model, r.effort, r.verdict, r.created_at,
                  (select case when e.meta->>'implementer'='stub-fallback' then 'false'::jsonb
                               else e.meta->'intended_intervention' end from rsi_experiments e where e.id=r.experiment_id)
           from rsi_eval_results r
           where (%(exp)s::text is not null and experiment_id = %(exp)s) or (%(art)s::text is not null and artifact_hash = %(art)s)
           order by created_at desc limit %(limit)s""",
        {"exp": exp, "art": art, "limit": int(p.get("limit", "50"))}).fetchall()
    out = []
    for eid, art_, exp_, suite, version, model, effort, verdict, created, intended in rows:
        subjects = [refs.amem("eval_result", eid), refs.amem("artifact", art_)]
        if exp_:
            subjects.append(refs.amem("experiment", exp_))
        out.append(EvidenceRef(refs.amem("eval_result", eid), "sb", "eval", subjects,
                               _clean({"suite": suite, "suite_version": version, "model": model, "effort": effort,
                                       "verdict": verdict, "failed": verdict in ("fail", "worse"),
                                       "intended_intervention": intended}),
                               None, _iso(created)))
    return out


def eval_by_model(conn: psycopg.Connection, p: Mapping[str, str]) -> list[EvidenceRef]:
    rows = conn.execute(
        """select suite, model, effort, count(*) as n,
                  count(*) filter (where verdict in ('pass', 'improves')) as good,
                  count(*) filter (where verdict in ('fail', 'worse')) as bad, max(created_at),
                  (array_agg(id::text order by created_at desc))[1:5]
           from rsi_eval_results r where created_at >= now() - make_interval(days => %s)
             and not exists(select 1 from rsi_experiments e where e.id=r.experiment_id
                            and (e.meta->>'intended_intervention'='false' or e.meta->>'implementer'='stub-fallback'))
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


# ---------------------------------------------------------------- Mission 005: one rule, for an evidence pack


def _label(v: Any) -> str | None:
    """A label read back from a text or jsonb column: kept only when it is label-shaped (no spaces, no text)."""
    return v if isinstance(v, str) and re.fullmatch(LABEL_P, v) else None


def rule_pattern(conn: psycopg.Connection, p: Mapping[str, str]) -> list[EvidenceRef]:
    """One aggregate over the rule's deviations in prod sessions (the index on (instruction_source, rule_id)).
    A rule id `unknown` matches the deviations without one, as recurring_deviations names them."""
    source, rule_id = _split_rule(p["rule"])
    days = int(p.get("days", "30"))
    row = conn.execute(
        f"""select count(*), count(distinct d.session_id),
                   count(distinct d.session_id) filter (where s.tool = 'claude'),
                   count(distinct d.session_id) filter (where s.tool = 'codex'),
                   avg(d.followed_grade), avg(d.result_grade), avg(d.objective_result), count(d.objective_result),
                   min(d.created_at), max(d.created_at),
                   (array_agg(d.id::text order by d.created_at desc, d.id))[1:5]
            from deviations d left join sessions s on s.session_id = d.session_id
            where d.instruction_source = %s and coalesce(d.rule_id, 'unknown') = %s
              and d.created_at >= now() - make_interval(days => %s) and {PROD}""",
        (source, rule_id, days)).fetchone()
    if not row or not row[0]:
        return []
    n, sessions, claude, codex, followed, result, objective, objective_n, first_at, last_at, samples = row
    subjects = [refs.amem("deviation", i) for i in samples]
    return [EvidenceRef(subjects[0], "sb", "pattern", subjects,
                        _clean({"rule_key": p["rule"], "n": n, "sessions": sessions, "claude_sessions": claude,
                                "codex_sessions": codex, "followed_avg": followed, "result_avg": result,
                                "objective_avg": objective, "objective_n": objective_n, "first_at": first_at,
                                "window_days": days}),
                        None, _iso(last_at))]


def jev_for(conn: psycopg.Connection, p: Mapping[str, str]) -> list[EvidenceRef]:
    """Jev's calls on a rule: one aggregate over its deviations' jev columns (prod sessions, last `days`), each
    candidate's Jev verdict for the rule, and for a routine rule the routine runs' Jev scores per question."""
    source, rule_id = _split_rule(p["rule"])
    days, limit = int(p.get("days", "30")), int(p.get("limit", "5"))
    out: list[EvidenceRef] = []
    row = conn.execute(
        f"""select count(*), count(d.jev_verdict),
                   count(*) filter (where d.jev_verdict = 'edit_instruction'),
                   count(*) filter (where d.jev_verdict = 'enforce'), count(*) filter (where d.jev_verdict = 'leave'),
                   avg(d.jev_confidence), count(*) filter (where d.jev_mode = 'live'),
                   count(*) filter (where d.jev_mode = 'fixture'), max(d.created_at),
                   (array_agg(d.id::text order by d.created_at desc, d.id) filter (where d.jev_verdict is not null))[1:5]
            from deviations d left join sessions s on s.session_id = d.session_id
            where d.instruction_source = %s and coalesce(d.rule_id, 'unknown') = %s
              and d.created_at >= now() - make_interval(days => %s) and {PROD}""",
        (source, rule_id, days)).fetchone()
    if row and row[1]:
        n, judged, edit, enforce, leave, conf, live, fixture, last_at, samples = row
        counts = {"edit_instruction": edit, "enforce": enforce, "leave": leave}
        top = max(counts, key=lambda k: (counts[k], k)) if any(counts.values()) else None
        subjects = [refs.amem("deviation", i) for i in samples]
        out.append(EvidenceRef(subjects[0], "sb", "jev", subjects,
                               _clean({"rule_key": p["rule"], "basis": "deviations", "n": n, "judged": judged,
                                       "unjudged": n - judged, **counts, "top_verdict": top, "confidence_avg": conf,
                                       "live": live, "fixture": fixture, "window_days": days}),
                               None, _iso(last_at)))
    rows = conn.execute(
        """select id::text, status, rank, jev ->> 'verdict',
                  case when jsonb_typeof(jev -> 'confidence') = 'number' then (jev ->> 'confidence')::float8 end,
                  seen_count, last_seen_at
           from rsi_candidates where rule_key = %s and jev is not null
           order by last_seen_at desc, id limit %s""", (p["rule"], limit)).fetchall()
    for cid, status, rank, verdict, conf, seen, last in rows:
        ref = refs.amem("candidate", cid)
        out.append(EvidenceRef(ref, "sb", "jev", [ref],
                               _clean({"rule_key": p["rule"], "basis": "candidate", "candidate_status": status,
                                       "rank": rank, "jev_verdict": _label(verdict), "jev_confidence": conf,
                                       "seen_count": seen}),
                               None, _iso(last)))
    if source.startswith("routine:"):
        rows = conn.execute(
            """select sc.question, count(*), avg(sc.value_num), avg(sc.confidence),
                      count(*) filter (where sc.jev_mode = 'live'), max(sc.created_at),
                      (array_agg(sc.id::text order by sc.created_at desc, sc.id))[1:5]
               from scores sc join runs r on r.id = sc.run_id
               where r.routine = %s and sc.created_at >= now() - make_interval(days => %s)
               group by sc.question order by count(*) desc, sc.question limit %s""",
            (source.split(":", 1)[1], days, limit)).fetchall()
        for question, n, value, conf, live, last, samples in rows:
            subjects = [refs.amem("score", i) for i in samples]
            out.append(EvidenceRef(subjects[0], "sb", "jev", subjects,
                                   _clean({"rule_key": p["rule"], "basis": "routine-scores",
                                           "question": _label(question), "n": n, "value_avg": value,
                                           "confidence_avg": conf, "live": live, "window_days": days}),
                                   None, _iso(last)))
    return out


def experiments_for(conn: psycopg.Connection, p: Mapping[str, str]) -> list[EvidenceRef]:
    """Experiments that tested a change to the rule: named by the experiment itself (hypothesis.rule,
    meta.rule_key, meta.candidate_key) or by a proposal of one of the rule's candidates. Newest first."""
    rule, key = p.get("rule"), p.get("key")
    if not (rule or key):
        raise ValueError("experiments_for: give rule or key")
    rows = conn.execute(
        """with ex as (
               select e.id from rsi_experiments e
               where (%(rule)s::text is not null and (e.hypothesis ->> 'rule' = %(rule)s or e.meta ->> 'rule_key' = %(rule)s))
                  or (%(key)s::text is not null and e.meta ->> 'candidate_key' = %(key)s)
               union
               select p.experiment_id from rsi_proposals p join rsi_candidates c on c.id = p.candidate_id
               where p.experiment_id is not null
                 and ((%(rule)s::text is not null and c.rule_key = %(rule)s)
                      or (%(key)s::text is not null and c.dedupe_key = %(key)s)))
           select e.id, e.status, e.verdict, e.kind, e.baseline_id, e.created_at, e.finished_at,
                  count(r.id), count(r.id) filter (where r.verdict in ('pass', 'improves')),
                  count(r.id) filter (where r.verdict in ('fail', 'worse')),
                  count(r.id) filter (where r.verdict = 'no-effect'),
                  count(r.id) filter (where r.verdict = 'inconclusive')
           from rsi_experiments e join ex on ex.id = e.id
           left join rsi_eval_results r on r.experiment_id = e.id
           group by e.id order by coalesce(e.finished_at, e.created_at) desc, e.id limit %(limit)s""",
        {"rule": rule, "key": key, "limit": int(p.get("limit", "5"))}).fetchall()
    out = []
    for eid, status, verdict, kind, baseline, created, finished, evals, good, bad, none_, inconclusive in rows:
        ref = refs.amem("experiment", eid)
        out.append(EvidenceRef(ref, "sb", "experiment", [ref],
                               _clean({"rule_key": rule, "status": status, "verdict": _label(verdict),
                                       "kind": _label(kind), "baseline_id": _label(baseline), "evals": evals,
                                       "evals_good": good, "evals_bad": bad, "evals_no_effect": none_,
                                       "evals_inconclusive": inconclusive, "created_at": created,
                                       "finished_at": finished}),
                               None, _iso(finished or created)))
    return out


RUN: dict[str, Callable[[psycopg.Connection, Mapping[str, str]], list[EvidenceRef]]] = {
    "recurring_deviations": recurring_deviations, "attempts_for": attempts_for, "decisions_for": decisions_for,
    "evals_for": evals_for, "eval_by_model": eval_by_model, "promotion_history": promotion_history,
    "rule_pattern": rule_pattern, "jev_for": jev_for, "experiments_for": experiments_for,
}
