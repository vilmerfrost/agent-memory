"""A/B report per lesson: pass rate of runs with the lesson vs runs without it.

See docs/ab.md. Postgres only in V1.

- Scope: `lessons.RUN_SCOPE_SQL`, the same run scope `lessons score` uses for its
  baseline (non-skip runs in the lesson's repo scope), optionally cut by `since`.
- Arm `with` (A): in-scope runs linked to the lesson by a `lesson_uses.run_id`.
  Arm `without` (B): every other in-scope run.
- Outcome per run, most trusted first: the human label (up = pass, down = fail),
  then Jev's pass_question answer when its confidence >= the routine's threshold,
  then `runs.status` (pass = pass, fail/error = fail). Each run records its source.
- Uncertainty: a Wilson 95% interval per arm and a pooled two-proportion z score.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from agent_memory import lessons, scoring, store
from agent_memory.questions import QuestionCache

DEFAULT_N = 10
DEFAULT_MARGIN = 0.1
Z95 = 1.959963984540054
SOURCES = ("label", "jev", "status")
ARMS = ("with", "without")


class AbError(RuntimeError):
    """An `ab report` input is invalid (for example an unknown lesson)."""


_RUNS_SQL = f"""
select r.id, r.routine, r.status, r.started_at, l.verdict as label,
       (select json_agg(json_build_object('question', s.question, 'value', s.value,
                                          'value_num', s.value_num, 'confidence', s.confidence))
          from scores s where s.run_id = r.id) as scores
from runs r
left join labels l on l.run_id = r.id
where {lessons.RUN_SCOPE_SQL}
  and (%(since)s::timestamptz is null or coalesce(r.started_at, r.created_at) >= %(since)s::timestamptz)
order by r.started_at nulls last, r.created_at, r.id
"""

_USED_SQL = "select distinct run_id from lesson_uses where lesson_id = %s and run_id is not null"


@dataclass(frozen=True)
class Outcome:
    run_id: uuid.UUID
    routine: str
    status: str
    passed: bool
    source: str  # label | jev | status
    confidence: float | None  # Jev's pass_question confidence, when the run has one
    threshold: float | None  # the routine's threshold, when it has a question file
    low_confidence: bool  # a Jev grade below threshold, no label: decided by status


def resolve(run: dict[str, Any], cache: QuestionCache) -> Outcome:
    """Decide one run's outcome: label > confident Jev grade > status."""
    qset = cache.get(run["routine"])
    answer = scoring._pass_answer(run, qset) if qset is not None and run.get("scores") else None
    confidence = None if answer is None or answer["confidence"] is None else float(answer["confidence"])
    threshold = qset.threshold if qset is not None else None

    def out(passed: bool, source: str, low: bool = False) -> Outcome:
        return Outcome(run["id"], run["routine"], run["status"], passed, source, confidence, threshold, low)

    if run.get("label") in ("up", "down"):
        return out(run["label"] == "up", "label")
    if qset is not None and answer is not None and confidence is not None:
        if confidence >= qset.threshold:
            return out(qset.jev_verdict(answer["value"], answer["value_num"]) == "up", "jev")
        return out(run["status"] == "pass", "status", low=True)
    return out(run["status"] == "pass", "status")


# --------------------------------------------------------------------------- statistics


def wilson(passed: int, total: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for a pass rate (95% by default). None for an empty arm."""
    if total == 0:
        return None
    p = passed / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def z_score(pass_a: int, n_a: int, pass_b: int, n_b: int) -> float | None:
    """Pooled two-proportion z score of rate A - rate B. |z| >= 1.96 ~ significant at 5%."""
    if n_a == 0 or n_b == 0:
        return None
    pooled = (pass_a + pass_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if se == 0:
        return 0.0  # both arms all pass or all fail: no difference
    return round((pass_a / n_a - pass_b / n_b) / se, 3)


def verdict(n_a: int, pass_a: int, n_b: int, pass_b: int, *, n: int, margin: float) -> str:
    """`insufficient` when an arm has < n runs; else WIN / LOSS beyond the margin, else TIE.

    A difference exactly at the margin is a TIE. The difference is rounded to 9
    places first, so float noise (0.4 - 0.3 = 0.10000000000000003) cannot flip it.
    """
    if n_a < n or n_b < n:
        return "insufficient"
    diff = round(pass_a / n_a - pass_b / n_b, 9)
    if diff > margin:
        return "WIN"
    if diff < -margin:
        return "LOSS"
    return "TIE"


def _arm(outcomes: list[Outcome]) -> dict[str, Any]:
    total = len(outcomes)
    passed = sum(o.passed for o in outcomes)
    ci = wilson(passed, total)
    return {
        "runs": total,
        "pass": passed,
        "pass_rate": round(passed / total, 4) if total else None,
        "ci95": list(ci) if ci else None,
        "sources": {s: sum(o.source == s for o in outcomes) for s in SOURCES},
    }


# --------------------------------------------------------------------------- report


def report(
    *,
    slug: str | None = None,
    repo: str | None = None,
    n: int = DEFAULT_N,
    margin: float = DEFAULT_MARGIN,
    since: datetime | None = None,
    cache: QuestionCache | None = None,
    db_url: str | None = None,
) -> list[dict[str, Any]]:
    """One entry per lesson (active lessons, or the one `slug`), in `lessons score` order."""
    cache = cache or QuestionCache()
    out: list[dict[str, Any]] = []
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = lessons.scored_lessons(cur, repo, slug)
        if slug is not None and not rows:
            where = f" scoped to repo {repo!r}" if repo is not None else ""
            raise AbError(f"no lesson with Id {slug!r}{where}")
        scopes: dict[str | None, list[Outcome]] = {}
        for lesson in rows:
            scope = lesson["scope_repo"]
            if scope not in scopes:
                cur.execute(_RUNS_SQL, {"repo": scope, "since": since})
                scopes[scope] = [resolve(r, cache) for r in cur.fetchall()]
            cur.execute(_USED_SQL, (lesson["id"],))
            used = {r["run_id"] for r in cur.fetchall()}
            arms: dict[str, list[Outcome]] = {"with": [], "without": []}
            for o in scopes[scope]:
                arms["with" if o.run_id in used else "without"].append(o)
            out.append(_entry(lesson, arms, n=n, margin=margin, since=since))
    return out


def _entry(lesson: dict[str, Any], arms: dict[str, list[Outcome]], *, n: int, margin: float,
           since: datetime | None) -> dict[str, Any]:
    a, b = _arm(arms["with"]), _arm(arms["without"])
    diff = (round(a["pass"] / a["runs"] - b["pass"] / b["runs"], 4)
            if a["runs"] and b["runs"] else None)
    low = [
        {"run_id": str(o.run_id), "routine": o.routine, "arm": arm, "status": o.status,
         "confidence": round(o.confidence, 4) if o.confidence is not None else None,
         "threshold": o.threshold}
        for arm in ARMS for o in sorted(arms[arm], key=lambda o: (o.confidence or 0.0, str(o.run_id)))
        if o.low_confidence
    ]
    return {
        "slug": lesson["slug"],
        "status": lesson["status"],
        "scope_repo": lesson["scope_repo"],
        "since": since.isoformat() if since else None,
        "n": n,
        "margin": margin,
        "verdict": verdict(a["runs"], a["pass"], b["runs"], b["pass"], n=n, margin=margin),
        "with": a,
        "without": b,
        "diff": diff,
        "z": z_score(a["pass"], a["runs"], b["pass"], b["runs"]),
        "low_confidence": low,
        "runs": [
            {"run_id": str(o.run_id), "arm": arm, "outcome": "pass" if o.passed else "fail",
             "source": o.source}
            for arm in ARMS for o in arms[arm]
        ],
    }


# --------------------------------------------------------------------------- text output


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def render_text(entry: dict[str, Any]) -> str:
    """A compact table for one lesson."""
    scope = entry["scope_repo"] or "all repos"
    head = (f"{entry['slug']}  [{entry['status']}, scope: {scope}]  verdict {entry['verdict']}  "
            f"(n>={entry['n']}, margin {entry['margin']:.2f}"
            + (f", since {entry['since']}" if entry["since"] else "") + ")")
    lines = [head, f"  {'arm':<8} {'runs':>5} {'pass':>5} {'rate':>5}  {'95% CI':<11} "
                   f"{'label':>5} {'jev':>4} {'status':>6}"]
    for arm in ARMS:
        s = entry[arm]
        ci = f"{s['ci95'][0]:.2f}-{s['ci95'][1]:.2f}" if s["ci95"] else "-"
        src = s["sources"]
        lines.append(f"  {arm:<8} {s['runs']:>5} {s['pass']:>5} {_rate(s['pass_rate']):>5}  {ci:<11} "
                     f"{src['label']:>5} {src['jev']:>4} {src['status']:>6}")
    diff = "-" if entry["diff"] is None else f"{entry['diff']:+.2f}"
    z = "-" if entry["z"] is None else f"{entry['z']:+.2f}"
    lines.append(f"  diff {diff}  z {z}")
    if entry["low_confidence"]:
        lines.append("  low confidence, label these (amem label <run_id> up|down):")
        for low in entry["low_confidence"]:
            conf = "-" if low["confidence"] is None else f"{low['confidence']:.2f}"
            lines.append(f"    {low['run_id']}  {low['routine']}  conf {conf} < {low['threshold']}  "
                         f"{low['arm']}  status {low['status']}")
    return "\n".join(lines)
