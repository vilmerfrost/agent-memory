"""Jev scoring of routine runs, human labels, escalations, calibration and drift.

See docs/scoring.md for the rules. Postgres only (psycopg) in V1.

The state string Jev reads for one run is built by `states.format_state` (re-exported
here): a generic shape for most routines and own shapes for dispatch, the canary
selftest and evals-parent. See agent_memory/states.py and docs/scoring.md.

Escalation rule (per question file, `escalate_on`): a run escalates when a listed
question's confidence is strictly below `threshold`. For the pass_question the
confidence is the *verdict confidence* (`QuestionSet.verdict_confidence`): how sure
Jev is of up vs down, not of the exact label.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import jev, store
from agent_memory.questions import QuestionCache, QuestionSet
from agent_memory.states import (  # noqa: F401  (re-exported: callers use scoring.format_state)
    DISPATCH_ROUTINE,
    format_dispatch_state,
    format_state,
)




# --------------------------------------------------------------------------- score


@dataclass
class ScoreReport:
    scored: int = 0
    skipped: int = 0  # unscored runs whose routine has no question file
    missing: list[tuple[str, str, str]] = field(default_factory=list)  # (run_id, routine, message)
    failed: list[tuple[str, str, str]] = field(default_factory=list)  # other Jev errors
    candidates: int = 0  # unscored runs read (the coverage funnel's `queried`)
    over_limit: int = 0  # unscored runs with a question file left for the next run (`limit` reached)


def candidate_runs(
    *, routine: str | None = None, since: datetime | None = None, db_url: str | None = None
) -> list[dict[str, Any]]:
    """Unscored runs, newest first."""
    clauses = ["not exists (select 1 from scores s where s.run_id = r.id)"]
    params: list[Any] = []
    if routine is not None:
        clauses.append("r.routine = %s")
        params.append(routine)
    if since is not None:
        clauses.append("r.started_at >= %s")
        params.append(as_utc(since))
    sql = (
        "select r.id, r.routine, r.status, r.summary, r.failure_reason, r.meta, r.started_at "
        f"from runs r where {' and '.join(clauses)} "
        "order by r.started_at desc nulls last, r.created_at desc, r.id"
    )
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _score_rows(results: dict[str, jev.JevResult]) -> list[tuple[Any, ...]]:
    rows = []
    for r in results.values():
        if r.kind == "choice":
            value, value_num = str(r.value), None
        else:
            value_num = float(r.value)
            value = repr(value_num)
        rows.append((r.name, r.kind, value, value_num, r.confidence, Jsonb(dict(r.probabilities))))
    return rows


def save_scores(run_id: Any, results: dict[str, jev.JevResult], mode: str, *, db_url: str | None = None,
                replace: bool = False) -> None:
    """Write one run's scores in one transaction. `replace` (rescoring) first deletes
    every score row of the run, so a run never holds old and new rows side by side and
    questions dropped from the file disappear too."""
    with store.connect(db_url) as conn:
        if replace:
            conn.execute("delete from scores where run_id = %s", (run_id,))
        for name, kind, value, value_num, confidence, probs in _score_rows(results):
            conn.execute(
                """
                insert into scores (run_id, question, kind, value, value_num, confidence,
                                    probabilities, jev_mode)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (run_id, question) do nothing
                """,
                (run_id, name, kind, value, value_num, confidence, probs, mode),
            )


def score_runs(
    *,
    routine: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
    cache: QuestionCache | None = None,
    db_url: str | None = None,
) -> ScoreReport:
    """Score unscored runs that have a question file, newest first, at most `limit` of them.

    A missing fixture (fixture mode) is counted and the run stays unscored, so a
    later `amem score` retries it. Any other Jev error for one run (SDK, network,
    invalid response, corrupt fixture; `jev.ask` raises them all as `JevError`) is
    recorded in `failed`, the run stays unscored, and scoring goes on with the next
    run. Runs with no question file are counted as skipped.
    """
    cache = cache or QuestionCache()
    report = ScoreReport()
    attempts = 0
    mode = jev.mode()
    for run in candidate_runs(routine=routine, since=since, db_url=db_url):
        report.candidates += 1
        qset = cache.get(run["routine"])
        if qset is None:
            report.skipped += 1
            continue
        if attempts >= limit:
            report.over_limit += 1
            continue
        attempts += 1
        try:
            results = jev.ask(format_state(run), qset.jev_questions())
        except jev.JevFixtureMissing as exc:
            report.missing.append((str(run["id"]), run["routine"], str(exc)))
            continue
        except jev.JevError as exc:
            report.failed.append((str(run["id"]), run["routine"], str(exc)))
            continue
        save_scores(run["id"], results, mode, db_url=db_url)
        report.scored += 1
    return report


# --------------------------------------------------------------------------- rescore


@dataclass
class RescoreReport(ScoreReport):
    comparisons: list[dict[str, Any]] = field(default_factory=list)  # one per rescored run


GO_LIVE_HINT = "set TYPESAFE_API_KEY and AMEM_JEV_LIVE=1 to rescore with live Jev"


class RescoreRefused(Exception):
    """Fixture-mode rescore would replace live Jev scores. Nothing was changed."""

    def __init__(self, routine: str, mode: str, live_run_ids: list[str]) -> None:
        self.routine = routine
        self.mode = mode
        self.live_run_ids = live_run_ids
        super().__init__(
            f"refusing to rescore {routine!r} in jev mode {mode}: {len(live_run_ids)} run(s) hold "
            f"live Jev scores that fixture answers would replace ({', '.join(live_run_ids[:5])}"
            f"{', ...' if len(live_run_ids) > 5 else ''}); {GO_LIVE_HINT}, or pass --allow-fixture "
            "to replace them anyway")


def _runs_with_live_scores(run_ids: list[Any], *, db_url: str | None = None) -> list[str]:
    if not run_ids:
        return []
    with store.connect(db_url) as conn:
        rows = conn.execute(
            "select distinct run_id from scores where jev_mode = 'live' and run_id = any(%s)",
            (list(run_ids),),
        ).fetchall()
    live = {r[0] for r in rows}
    return [str(rid) for rid in run_ids if rid in live]  # keep the newest-first order


def _answer_view(scores: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["question"]: {"value": s["value"], "confidence": s["confidence"]} for s in scores}


def _any_low(qset: QuestionSet, scores: list[dict[str, Any]]) -> list[str]:
    """The old (V1) rule: every choice/score question whose raw Jev confidence is below threshold."""
    return sorted(s["question"] for s in scores if s["kind"] in ("choice", "score")
                  and s["confidence"] is not None and s["confidence"] < qset.threshold)


def _stored_view(results: dict[str, jev.JevResult]) -> list[dict[str, Any]]:
    """Jev results in the shape `scored_runs` returns for stored rows."""
    return [{"question": name, "kind": kind, "value": value, "value_num": value_num,
             "confidence": confidence, "probabilities": dict(probs.obj)}
            for name, kind, value, value_num, confidence, probs in _score_rows(results)]


def rescore_runs(
    *,
    routine: str,
    since: datetime | None = None,
    limit: int = 100,
    cache: QuestionCache | None = None,
    db_url: str | None = None,
    allow_fixture: bool = False,
) -> RescoreReport:
    """Ask Jev again about already scored runs of one routine and replace their scores.

    Guard: in fixture mode, when any selected run holds a score row with
    jev_mode='live', raise `RescoreRefused` before any change (fixture answers must
    never silently replace live ones), unless `allow_fixture` is set.

    The runs: the routine's scored runs (labeled or not), newest first, started at or
    after `since`, at most `limit`. Each run's new answers replace all its old score
    rows in one transaction (`save_scores(replace=True)`), so a run never has duplicate
    or mixed rows. A missing fixture or a Jev error keeps the old rows untouched.

    `comparisons` holds, per rescored run, the old and new answers and three
    escalation flags, so the same runs can be compared before and after a state or
    question change: `escalated_before_old_rule` (V1: any choice/score raw confidence
    under threshold, on the old answers), `escalates_before` and `escalates_after`
    (the current `escalate_on` rule on the old and the new answers).
    """
    cache = cache or QuestionCache()
    report = RescoreReport()
    qset = cache.get(routine)
    if qset is None:
        return report
    mode = jev.mode()
    runs = [r for r in scored_runs(routine=routine, db_url=db_url)
            if since is None or (r["started_at"] is not None and r["started_at"] >= as_utc(since))]
    runs = runs[:limit]
    if mode == "fixture" and not allow_fixture:
        live = _runs_with_live_scores([r["id"] for r in runs], db_url=db_url)
        if live:
            raise RescoreRefused(routine, mode, live)
    for run in runs:
        try:
            results = jev.ask(format_state(run), qset.jev_questions())
        except jev.JevFixtureMissing as exc:
            report.missing.append((str(run["id"]), routine, str(exc)))
            continue
        except jev.JevError as exc:
            report.failed.append((str(run["id"]), routine, str(exc)))
            continue
        save_scores(run["id"], results, mode, db_url=db_url, replace=True)
        report.scored += 1
        new = _stored_view(results)
        report.comparisons.append({
            "run_id": str(run["id"]),
            "routine": routine,
            "started_at": run["started_at"].isoformat() if run["started_at"] else None,
            "before": _answer_view(run["scores"]),
            "after": _answer_view(new),
            "escalated_before_old_rule": bool(_any_low(qset, run["scores"])),
            "escalates_before": bool(qset.low_answers(run["scores"])),
            "escalates_after": bool(qset.low_answers(new)),
            "low_after": qset.low_answers(new),
        })
    return report


# --------------------------------------------------------------------------- label


def set_label(run_id: uuid.UUID, verdict: str, note: str | None, *, db_url: str | None = None) -> bool:
    """Insert or overwrite the run's label. Returns True when it was new."""
    if verdict not in ("up", "down"):
        raise ValueError(f"verdict must be up or down, got {verdict!r}")
    with store.connect(db_url) as conn:
        row = conn.execute(
            """
            insert into labels (run_id, verdict, note) values (%s, %s, %s)
            on conflict (run_id) do update
                set verdict = excluded.verdict, note = excluded.note, labeled_at = now()
            returning (xmax = 0) as inserted
            """,
            (run_id, verdict, note),
        ).fetchone()
    assert row is not None
    return bool(row[0])


# --------------------------------------------------------------------------- shared reads


def scored_runs(*, routine: str | None = None, db_url: str | None = None) -> list[dict[str, Any]]:
    """Every scored run with its scores (list) and label (or None), newest first."""
    params: list[Any] = []
    where = ""
    if routine is not None:
        where = "where r.routine = %s"
        params.append(routine)
    sql = f"""
        select r.id, r.routine, r.status, r.summary, r.failure_reason, r.meta, r.started_at,
               l.verdict as label, l.note as label_note,
               json_agg(json_build_object(
                   'question', s.question, 'kind', s.kind, 'value', s.value,
                   'value_num', s.value_num, 'confidence', s.confidence,
                   'probabilities', s.probabilities) order by s.question) as scores
        from runs r
        join scores s on s.run_id = r.id
        left join labels l on l.run_id = r.id
        {where}
        group by r.id, l.verdict, l.note
        order by r.started_at desc nulls last, r.created_at desc, r.id
    """
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _pass_answer(run: dict[str, Any], qset: QuestionSet) -> dict[str, Any] | None:
    for s in run["scores"]:
        if s["question"] == qset.pass_question:
            return s
    return None


# --------------------------------------------------------------------------- escalations


def escalations(
    *, routine: str | None = None, cache: QuestionCache | None = None, db_url: str | None = None
) -> list[dict[str, Any]]:
    """Unlabeled scored runs where an `escalate_on` question's confidence is below the
    routine's threshold (`QuestionSet.low_answers`; the pass_question uses its verdict
    confidence). Strictly below: a confidence equal to the threshold does not escalate.
    """
    cache = cache or QuestionCache()
    out = []
    for run in scored_runs(routine=routine, db_url=db_url):
        if run["label"] is not None:
            continue
        qset = cache.get(run["routine"])
        if qset is None:
            continue
        low = qset.low_answers(run["scores"])
        if not low:
            continue
        pa = _pass_answer(run, qset)
        out.append({
            "run_id": str(run["id"]),
            "routine": run["routine"],
            "started_at": run["started_at"].isoformat() if run["started_at"] else None,
            "status": run["status"],
            "summary": run["summary"],
            "failures": (run["meta"] or {}).get("failures") or [],
            "threshold": qset.threshold,
            "low": low,
            "jev_verdict": qset.jev_verdict(pa["value"], pa["value_num"]) if pa else None,
            "answers": {s["question"]: s["value"] for s in run["scores"]},
        })
    return out


# --------------------------------------------------------------------------- calibrate

DEFAULT_MIN_LABELS = 10
DEFAULT_TARGET = 0.9


def suggest_threshold(
    pairs: list[tuple[float, bool]], *, min_labels: int = DEFAULT_MIN_LABELS, target: float = DEFAULT_TARGET
) -> tuple[float | None, str | None]:
    """(threshold, note) from (confidence, agreed) pairs.

    The suggestion is the lowest confidence t such that the runs with
    confidence >= t number at least `min_labels` and agree at a rate >= target.
    """
    if len(pairs) < min_labels:
        return None, f"not enough labels ({len(pairs)} < {min_labels})"
    for t in sorted({c for c, _ in pairs}):
        subset = [a for c, a in pairs if c >= t]
        if len(subset) < min_labels:
            break
        if sum(subset) / len(subset) >= target:
            return t, None
    return None, f"no confidence level reaches {target:.0%} agreement over >= {min_labels} labels"


def calibrate(
    *,
    routine: str | None = None,
    min_labels: int = DEFAULT_MIN_LABELS,
    target: float = DEFAULT_TARGET,
    cache: QuestionCache | None = None,
    db_url: str | None = None,
) -> list[dict[str, Any]]:
    """Per routine with a question file: Jev vs label agreement and a suggested threshold."""
    cache = cache or QuestionCache()
    routines = [routine] if routine is not None else cache.routines()
    runs = scored_runs(routine=routine, db_url=db_url)
    out = []
    for name in routines:
        qset = cache.get(name)
        if qset is None:
            continue
        pairs: list[tuple[float, bool]] = []
        counts = {"jev_up_label_up": 0, "jev_up_label_down": 0, "jev_down_label_up": 0, "jev_down_label_down": 0}
        for run in runs:
            if run["routine"] != name or run["label"] is None:
                continue
            pa = _pass_answer(run, qset)
            conf = qset.answer_confidence(pa) if pa is not None else None
            if pa is None or conf is None:
                continue
            jv = qset.jev_verdict(pa["value"], pa["value_num"])
            counts[f"jev_{jv}_label_{run['label']}"] += 1
            pairs.append((float(conf), jv == run["label"]))
        agree = sum(a for _, a in pairs)
        suggested, note = suggest_threshold(pairs, min_labels=min_labels, target=target)
        out.append({
            "routine": name,
            "pass_question": qset.pass_question,
            "labels": len(pairs),
            "agree": agree,
            "agreement": round(agree / len(pairs), 4) if pairs else None,
            **counts,
            "threshold": qset.threshold,
            "suggested_threshold": suggested,
            "note": note,
        })
    return out


# --------------------------------------------------------------------------- drift

DEFAULT_DELTA = 0.2
DEFAULT_MIN_RUNS = 5
_DURATION_RE = re.compile(r"(\d+)([dh])")


def parse_duration(value: str) -> timedelta:
    m = _DURATION_RE.fullmatch(value.strip())
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"duration must look like 7d or 12h, got {value!r}")
    n = int(m.group(1))
    return timedelta(days=n) if m.group(2) == "d" else timedelta(hours=n)


def as_utc(value: datetime) -> datetime:
    """A naive datetime is read as UTC; an aware one is kept as is."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def drift(
    *,
    now: datetime,
    window: timedelta,
    baseline: timedelta,
    routine: str | None = None,
    delta: float = DEFAULT_DELTA,
    min_runs: int = DEFAULT_MIN_RUNS,
    cache: QuestionCache | None = None,
    db_url: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(flags, checks). Window = [now - window, now). Baseline = the period just before it.

    Metrics, both on a 0..1 scale:
    - pass_rate: share of runs with status pass (skip runs left out). Every routine.
    - jev_pass: mean Jev pass signal of the pass_question (QuestionSet.pass_signal)
      over scored runs. Only routines with a question file.
    A metric flags when both periods hold >= min_runs samples and
    baseline - window > delta.
    """
    cache = cache or QuestionCache()
    now = as_utc(now)
    w_start = now - window
    b_start = w_start - baseline
    params: list[Any] = [b_start, now]
    extra = ""
    if routine is not None:
        extra = "and r.routine = %s"
        params.append(routine)
    sql = f"""
        select r.id, r.routine, r.status, r.started_at,
               (select json_agg(json_build_object('question', s.question, 'value', s.value,
                                                  'value_num', s.value_num,
                                                  'probabilities', s.probabilities))
                from scores s where s.run_id = r.id) as scores
        from runs r
        where r.started_at >= %s and r.started_at < %s {extra}
    """
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    per: dict[str, dict[str, dict[str, list[float]]]] = {}
    for row in rows:
        period = "window" if row["started_at"] >= w_start else "baseline"
        bucket = per.setdefault(row["routine"], {
            "pass_rate": {"window": [], "baseline": []},
            "jev_pass": {"window": [], "baseline": []},
        })
        if row["status"] != "skip":
            bucket["pass_rate"][period].append(1.0 if row["status"] == "pass" else 0.0)
        qset = cache.get(row["routine"])
        if qset is not None and row["scores"]:
            for s in row["scores"]:
                if s["question"] == qset.pass_question:
                    sig = qset.pass_signal(s["value_num"], s["probabilities"] or {})
                    if sig is not None:
                        bucket["jev_pass"][period].append(sig)

    flags, checks = [], []
    for name in sorted(per):
        for metric, periods in per[name].items():
            wv, bv = periods["window"], periods["baseline"]
            if metric == "jev_pass" and cache.get(name) is None:
                continue
            w_mean, b_mean = _mean(wv), _mean(bv)
            enough = len(wv) >= min_runs and len(bv) >= min_runs
            drop = round(b_mean - w_mean, 4) if enough and w_mean is not None and b_mean is not None else None
            entry = {
                "routine": name,
                "metric": metric,
                "window_n": len(wv),
                "window_value": round(w_mean, 4) if w_mean is not None else None,
                "baseline_n": len(bv),
                "baseline_value": round(b_mean, 4) if b_mean is not None else None,
                "drop": drop,
                "delta": delta,
                "min_runs": min_runs,
                "enough_samples": enough,
            }
            checks.append(entry)
            if drop is not None and drop > delta:
                flags.append(entry)
    return flags, checks
