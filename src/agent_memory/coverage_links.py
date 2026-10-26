"""Relationship contract check (plan section 5 "Relationships", docs/data-use/relationships.md).

For each relationship it counts, from the existing link columns only:
  eligible      child rows that should carry the link
  linked        the link is set and its target row exists
  unlinked      the link is not set (null, empty or missing key): visible, never guessed
  unresolvable  the link is set but its target row is missing (late arrival or dangling)

Read-only: one READ ONLY transaction, static SQL, nothing is written or repaired. The one repair so far is
`amem coverage repair-links` (attempt_links.py) for experiment attempts.
A relationship whose tables are missing reports status `missing_table` instead of failing the rest.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import psycopg

from agent_memory import attempt_links as AL

Enforced = str   # "foreign key" | "none" | "partial"


@dataclass(frozen=True)
class Relationship:
    key: str
    title: str
    child: str             # the table whose rows carry the link
    link: str              # the link column or expression
    target: str            # what the link points at
    enforced: Enforced
    unlinked_visible: str  # how an unlinked row shows up
    tables: tuple[str, ...]
    sql: str               # returns eligible, linked, unlinked, unresolvable (bigint) and optional extra columns
    excluded: str = ""     # rows that look related but are not eligible, and why (counted in the extra columns)


def _rel(key: str, title: str, child: str, link: str, target: str, enforced: Enforced, visible: str,
         tables: tuple[str, ...], sql: str, excluded: str = "") -> Relationship:
    return Relationship(key, title, child, link, target, enforced, visible, tables, " ".join(sql.split()), excluded)


_EXPERIMENT_REF = "'^amem:experiment/'"

RELATIONSHIPS: tuple[Relationship, ...] = (
    _rel("parent_worker_sessions", "parent and worker sessions", "sessions (kind or category worker)",
         "sessions.parent_session_id (generated from meta.parent_session)", "sessions.session_id", "none",
         "worker row with parent_session_id null", ("sessions",), """
         select count(*),
                count(*) filter (where s.parent_session_id is not null and p.session_id is not null),
                count(*) filter (where s.parent_session_id is null),
                count(*) filter (where s.parent_session_id is not null and p.session_id is null)
         from public.sessions s left join public.sessions p on p.session_id = s.parent_session_id
         where s.kind = 'worker' or s.category = 'worker'"""),
    _rel("session_commands", "sessions and commands", "execution_spans", "execution_spans.session_id",
         "sessions.session_id", "none", "span with session_id null", ("execution_spans", "sessions"), """
         select count(*),
                count(*) filter (where e.session_id is not null and s.session_id is not null),
                count(*) filter (where e.session_id is null),
                count(*) filter (where e.session_id is not null and s.session_id is null)
         from public.execution_spans e left join public.sessions s on s.session_id = e.session_id"""),
    _rel("command_test_runs", "commands and test runs", "test_timing_runs", "test_timing_runs.parent_span_id",
         "execution_spans.span_id", "none", "test run with parent_span_id null",
         ("test_timing_runs", "execution_spans"), """
         select count(*),
                count(*) filter (where r.parent_span_id is not null and e.span_id is not null),
                count(*) filter (where r.parent_span_id is null),
                count(*) filter (where r.parent_span_id is not null and e.span_id is null)
         from public.test_timing_runs r left join public.execution_spans e on e.span_id = r.parent_span_id"""),
    _rel("session_test_runs", "sessions and test runs", "test_timing_runs", "test_timing_runs.session_id",
         "sessions.session_id", "none", "test run with session_id null", ("test_timing_runs", "sessions"), """
         select count(*),
                count(*) filter (where r.session_id is not null and s.session_id is not null),
                count(*) filter (where r.session_id is null),
                count(*) filter (where r.session_id is not null and s.session_id is null)
         from public.test_timing_runs r left join public.sessions s on s.session_id = r.session_id"""),
    _rel("test_run_cases", "test runs and individual tests", "test_timing_cases", "test_timing_cases.run_id",
         "test_timing_runs.run_id", "foreign key",
         "cannot be unlinked (not null FK); a run missing case rows shows as runs_missing_cases",
         ("test_timing_cases", "test_timing_runs"), """
         select (select count(*) from public.test_timing_cases), (select count(*) from public.test_timing_cases),
                0::bigint, 0::bigint,
                (select count(*) from public.test_timing_runs r where r.details_retained and r.tests_reported >
                    (select count(*) from public.test_timing_cases c where c.run_id = r.run_id)) as runs_missing_cases"""),
    _rel("experiment_attempt_sessions", "experiments and attempts (sandbox experiment attempts)",
         "rsi_attempts, plus sessions (env sandbox or experiment_id set) that no rsi_attempts row covers",
         "rsi_attempts.experiment_id with status linked (attempt key and attempt_session_id)", "rsi_experiments.id",
         "none", "rsi_attempts row with status unresolved (and its reason), or a sandbox session with no attempt row",
         ("rsi_attempts", "sessions", "rsi_experiments"), f"""
         with a as (
             select t.experiment_id, t.status = 'linked' as identified from public.rsi_attempts t
             union all
             select s.experiment_id, false from public.sessions s
             where (s.env = 'sandbox' or s.experiment_id is not null)
               and not exists (select 1 from public.rsi_attempts t
                               where t.session_id = s.session_id or t.attempt_session_id = s.session_id))
         select count(*),
                count(*) filter (where a.identified and x.id is not null),
                count(*) filter (where not a.identified),
                count(*) filter (where a.identified and x.id is null),
                (select count(*) from public.sessions s where s.env = 'eval' and s.experiment_id is null
                 and s.project_dir ~ '{AL.EVAL_HARNESS_SQL}') as excluded_eval_harness,
                (select count(*) from public.sessions s where s.env = 'eval' and s.experiment_id is null
                 and s.project_dir !~ '{AL.EVAL_HARNESS_SQL}') as excluded_eval_other
         from a left join public.rsi_experiments x on x.id = a.experiment_id""",
         excluded="sessions with env eval and no experiment_id are eval runs, not experiment attempts "
                  "(eval-harness clones under ~/.cache/agent-evals or skill-evals, and other eval sessions); "
                  "judge (J), check (K) and implementer (I) sandbox runs are not attempts either"),
    _rel("experiment_eval_results", "experiments and evaluation results", "rsi_eval_results",
         "rsi_eval_results.experiment_id", "rsi_experiments.id", "none", "eval result with experiment_id null",
         ("rsi_eval_results", "rsi_experiments"), """
         select count(*),
                count(*) filter (where r.experiment_id is not null and x.id is not null),
                count(*) filter (where r.experiment_id is null),
                count(*) filter (where r.experiment_id is not null and x.id is null)
         from public.rsi_eval_results r left join public.rsi_experiments x on x.id = r.experiment_id"""),
    _rel("experiment_traces", "experiments and traces (Langfuse)", "rsi_evidence (system langfuse, kind trace)",
         "rsi_evidence.subject_refs amem:experiment/<id>", "rsi_experiments.id", "none",
         "trace evidence without an amem:experiment ref", ("rsi_evidence", "rsi_experiments"), f"""
         with t as (select e.id, (select substr(r, 17) from unnest(e.subject_refs) r where r ~ {_EXPERIMENT_REF}
                                  limit 1) as exp
                    from public.rsi_evidence e where e.system = 'langfuse' and e.kind = 'trace')
         select count(*),
                count(*) filter (where t.exp is not null and x.id is not null),
                count(*) filter (where t.exp is null),
                count(*) filter (where t.exp is not null and x.id is null)
         from t left join public.rsi_experiments x on x.id = t.exp"""),
    _rel("experiment_scores", "experiments and scores (Langfuse)", "rsi_evidence (system langfuse, kind score)",
         "rsi_evidence.subject_refs amem:experiment/<id>", "rsi_experiments.id", "none",
         "score evidence without an amem:experiment ref", ("rsi_evidence", "rsi_experiments"), f"""
         with t as (select e.id, (select substr(r, 17) from unnest(e.subject_refs) r where r ~ {_EXPERIMENT_REF}
                                  limit 1) as exp
                    from public.rsi_evidence e where e.system = 'langfuse' and e.kind = 'score')
         select count(*),
                count(*) filter (where t.exp is not null and x.id is not null),
                count(*) filter (where t.exp is null),
                count(*) filter (where t.exp is not null and x.id is null)
         from t left join public.rsi_experiments x on x.id = t.exp"""),
    _rel("decision_proposals", "decisions and proposals", "rsi_decisions", "rsi_decisions.proposal_id",
         "rsi_proposals.id", "foreign key", "cannot be unlinked (not null FK)", ("rsi_decisions", "rsi_proposals"), """
         select count(*), count(p.id), 0::bigint, count(*) - count(p.id)
         from public.rsi_decisions d left join public.rsi_proposals p on p.id = d.proposal_id"""),
    _rel("decision_results", "decisions and their outcomes", "rsi_decisions",
         "rsi_decision_results.decision_id (result row per decision)", "rsi_decision_results", "partial",
         "decision with no result row (state waiting in rsi_decision_status)",
         ("rsi_decisions", "rsi_decision_results"), """
         select count(*), count(r.decision_id), count(*) - count(r.decision_id), 0::bigint
         from public.rsi_decisions d left join public.rsi_decision_results r on r.decision_id = d.id"""),
    _rel("proposal_edits", "proposals and staged edits", "edits (meta.mode = stage)", "edits.meta->>'proposal_id'",
         "rsi_proposals.id", "none", "staged edit without meta.proposal_id", ("edits", "rsi_proposals"), """
         select count(*),
                count(*) filter (where e.meta ? 'proposal_id' and p.id is not null),
                count(*) filter (where not (e.meta ? 'proposal_id') or e.meta->>'proposal_id' = ''),
                count(*) filter (where e.meta->>'proposal_id' <> '' and p.id is null)
         from public.edits e left join public.rsi_proposals p on p.id::text = e.meta->>'proposal_id'
         where e.meta->>'mode' = 'stage' or e.status = 'staged'"""),
    _rel("edit_undo_requests", "edits and undo requests", "undo_requests", "undo_requests.edit_id", "edits.id",
         "foreign key", "cannot be unlinked (not null FK); an open request shows as outcome_pending",
         ("undo_requests", "edits"), """
         select count(*), count(e.id), 0::bigint, count(*) - count(e.id),
                count(*) filter (where u.handled_at is null) as outcome_pending
         from public.undo_requests u left join public.edits e on e.id = u.edit_id"""),
    _rel("change_outcomes", "applied changes and outcome checks", "rsi_outcomes",
         "rsi_outcomes.change_kind + change_id", "rsi_proposals.id or edits.id", "none",
         "an outcome row is always keyed; a dangling change_id shows as unresolvable",
         ("rsi_outcomes", "rsi_proposals", "edits"), """
         select count(*),
                count(*) filter (where (o.change_kind = 'proposal' and exists (select 1 from public.rsi_proposals p
                                          where p.id::text = o.change_id))
                                    or (o.change_kind = 'edit' and exists (select 1 from public.edits e
                                          where e.id::text = o.change_id))),
                0::bigint,
                count(*) filter (where not ((o.change_kind = 'proposal' and exists (select 1 from public.rsi_proposals p
                                          where p.id::text = o.change_id))
                                    or (o.change_kind = 'edit' and exists (select 1 from public.edits e
                                          where e.id::text = o.change_id))))
         from public.rsi_outcomes o"""),
)


def relationship(key: str) -> Relationship:
    for r in RELATIONSHIPS:
        if r.key == key:
            return r
    raise KeyError(key)


def counts(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """One dict per relationship: the contract fields plus eligible / linked / unlinked / unresolvable.

    Needs an idle connection: the counts run in their own READ ONLY transaction, always rolled back."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("coverage_links.counts needs an idle connection (it opens its own read-only transaction)")
    out: list[dict[str, Any]] = []
    with conn.transaction(force_rollback=True):
        conn.execute("set transaction read only")
        for r in RELATIONSHIPS:
            row: dict[str, Any] = {k: v for k, v in asdict(r).items() if k not in ("sql", "tables")}
            missing = [t for t in r.tables if conn.execute("select to_regclass(%s)", (f"public.{t}",)).fetchone()[0]
                       is None]
            if missing:
                row.update(status="missing_table", missing=missing, eligible=None, linked=None, unlinked=None,
                           unresolvable=None)
                out.append(row)
                continue
            cur = conn.execute(r.sql)
            values = cur.fetchone()
            names = [d.name for d in cur.description]
            eligible, linked, unlinked, unresolvable = (int(v) for v in values[:4])
            row.update(eligible=eligible, linked=linked, unlinked=unlinked, unresolvable=unresolvable,
                       status="ok" if eligible == linked else "gaps")
            row.update({names[i]: int(values[i]) for i in range(4, len(values))})
            out.append(row)
    return out
