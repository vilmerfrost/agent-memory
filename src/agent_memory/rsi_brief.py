"""Mission 005 Part E: the RSI morning brief, `amem rsi brief` (docs/rsi/m005-plan.md, 2.5 and 2.6).

Input: one night report, `<root>/nights/<night>/report.json` (format `rsi-night/1`, written by `amem rsi night`;
by default the latest night that has a report), the RSI rows it points to (rsi_proposals, rsi_eval_results,
rsi_candidates, rsi_experiments, rsi_artifacts) and the staged artifacts under `<root>/artifacts/<H>/`.

Output: a markdown brief, or `--json` (format `rsi-brief/1`). Every section is always there: sessions analyzed,
important patterns, candidates considered, experiments performed, improvements that passed (one block per staged
proposal: what would change with a diff summary, why, supporting evidence, baseline vs candidate, risk and
confidence, the artifact hash, Apply / Reject / Details), failed or inconclusive experiments, model and provider
costs, service health, pending human decisions (earlier nights too).

Read-only: nothing here writes the sandbox or the database (the DB session is read-only). Apply / Reject /
Details are the existing commands: `amem rsi decide <id> apply|reject --hash <H>` (Vilmer's terminal only) and
`amem rsi show <id>`. `--hash` binds the decision to the exact tested artifact: `decide` records nothing when the
proposal points at other bytes. There is no other approval path.

Apply is offered only for a fully evaluated improvement (Mission 005.5, `apply_blockers`): verdict IMPROVES, an
artifact that verifies, every required evaluation passing, a sandbox-replay record bound to the hash that says
`improves` with an independent judge (Codex or Claude, at least one pair judged) and live Jev that answered, and a
pending proposal for exactly that hash in the database. Anything else (NO_EFFECT, WORSE, INCONCLUSIVE, a stand-in
judge or fixture Jev, an unverified or unregistered artifact, database rows not read) shows the reasons instead of
an Apply command. Reject and Details always stay. Costs show their basis: provider-reported vs local estimate.

Free text from the report and the rows (pattern summaries, hypotheses, reasons, titles, notes) is untrusted data.
It is redacted, flattened to one line and stripped of backticks and table pipes, so it cannot add a heading, a
command or a code span to the brief. Ids and hashes must match strict patterns or the report is refused.

Phone: `push_brief` sends one summary through the existing ntfy publisher (`edits_push._publish`, with its macOS
fallback) and refuses unless AMEM_RSI_BRIEF_PUSH=1. Mission 005 never sets it. The text is built from counts,
enums, short hashes, file names and ids only, then scrubbed again (home paths, emails, secrets). The per-proposal
Apply / Reject phone buttons stay `amem rsi push`.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_memory import config, coverage as CV, edits_push, night_coverage as NC, redact, rsi_artifact as RA, store

REPORT_FORMAT = "rsi-night/1"
BRIEF_FORMAT = "rsi-brief/1"
NIGHT_STATUSES = ("ok", "partial", "aborted", "budget_exhausted", "preflight_failed")
SKIP_REASONS = ("prerequisites", "credentials")                       # rsi_night.SKIP_REASONS
EXPERIMENT_STATUSES = ("staged", "not_promotable", "failed", "interrupted", "skipped_budget", "deferred_policy")
VERDICTS = ("IMPROVES", "NO_EFFECT", "WORSE", "INCONCLUSIVE")        # rsi_verdict.VERDICTS (plan 2.1)
RISKS = ("low", "medium", "high")
HEALTHY = ("ok", "fixture", "live")
NEUTRAL = ("skipped",)                                                # not used tonight: not an outage
PASSING = RA.PASSING
DEFAULT_SUITES = ("static", "sandbox-replay")                         # rsi_approval.DEFAULT_SUITES
PUSH_ENV = "AMEM_RSI_BRIEF_PUSH"
PUSH_OFF = f"refused: phone notifications are off; nothing was sent (set {PUSH_ENV}=1 to enable them after activation)"
COUNTS = ("sessions", "deviations", "eval_failures", "routine_failures")
SECTIONS = ("Sessions analyzed", "Important patterns", "Candidates considered", "Experiments performed",
            "Improvements that passed", "Failed or inconclusive experiments", "Model and provider costs",
            "Service health", "Pending human decisions")

NIGHT_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}")
EXPERIMENT_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,60}")               # rsi_experiments.id check
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,31}")
FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_PATTERNS, MAX_CANDIDATES, MAX_REFS, MAX_METRICS, MAX_EXCERPT, MAX_NOTES = 8, 12, 10, 14, 12, 6
PUSH_ITEMS = 5
REPLAY_SUITE = "sandbox-replay"
LIVE_JUDGES = ("codex", "claude")       # an independent model judge; stub / none are stand-ins
COST_KINDS = {"reported": "provider-reported", "table": "estimate", "default": "estimate", "call": "estimate",
              "unknown-usage": "estimate", "cli-cap": "estimate", "free": "free", "none": "free",
              "on-call": "free"}             # rsi_budget.COST_KINDS (the brief only reads the report)

# C0/C1 controls, line/paragraph separators and bidi overrides (written with chr() to keep the source ASCII)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f" + chr(0x2028) + chr(0x2029) + chr(0x202A) + "-"
                   + chr(0x202E) + chr(0x2066) + "-" + chr(0x2069) + "]")
_WS = re.compile(r"\s+")
_HOME_PATH = re.compile(r"(?:/Users|/home|/root|/private/var|/var/folders)/[^\s'\"`),;]*")
_TILDE_PATH = re.compile(r"(?<![\w/])~[A-Za-z0-9_.-]*/[^\s'\"`),;]*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


class ReportError(Exception):
    """The night report is missing or is not a valid rsi-night/1 report; the message says why."""


class PushRefused(Exception):
    """Phone notifications are off (AMEM_RSI_BRIEF_PUSH is not 1). Nothing was sent."""


# --------------------------------------------------------------------------- untrusted text


def text(v: Any, cap: int = 300) -> str:
    """Untrusted free text for the brief: redacted, one line, no backticks / table pipes / link syntax, capped."""
    if v is None:
        return ""
    s = _WS.sub(" ", _CTRL.sub(" ", redact.redact(str(v)))).strip()
    s = s.replace("`", "'").replace("|", "/").replace("](", "] (")
    return s if len(s) <= cap else s[: cap - 3] + "..."


def diff_line(v: str, cap: int = 160) -> str:
    """One diff excerpt line: like text(), but keeps the inner spacing."""
    s = _CTRL.sub(" ", redact.redact(v)).replace("\t", "    ").replace("`", "'").rstrip()
    return s if len(s) <= cap else s[: cap - 3] + "..."


def scrub(s: str) -> str:
    """Phone text: no secrets, no paths under a home directory, no emails."""
    s = redact.redact(s)
    home = str(Path.home())
    if len(home) > 1:
        s = s.replace(home, "[path]")
    s = _HOME_PATH.sub("[path]", s)
    s = _TILDE_PATH.sub("[path]", s)
    return _EMAIL.sub("[email]", s)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f"{round(v, 4):g}"
    return text(v, 40)


def usd(v: Any) -> str:
    return f"${float(v):.2f}" if _num(v) else "$?"


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _when(v: Any) -> str:
    return text(str(_iso(v))[:16].replace("T", " "), 20) if v else "-"


def _sentence(s: str) -> str:
    return s if not s or s[-1] in ".!?" else s + "."


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# --------------------------------------------------------------------------- the night report


def find_report(root: Path, night: str | None = None) -> tuple[Path, list[str]]:
    """(path, warnings). With `night`: that night's report or ReportError. Without: the latest night directory
    that has a report.json; newer directories without one are named in the warnings."""
    nights = root / "nights"
    if night is not None:
        if not NIGHT_RE.fullmatch(night):
            raise ReportError(f"bad night name {night!r} (expected a night key like 2026-10-18)")
        path = nights / night / "report.json"
        if not path.is_file():
            raise ReportError(f"no report for night {night}: nights/{night}/report.json is missing")
        return path, []
    names = sorted((d.name for d in nights.iterdir() if d.is_dir() and NIGHT_RE.fullmatch(d.name)),
                   reverse=True) if nights.is_dir() else []
    warnings = []
    for name in names:
        path = nights / name / "report.json"
        if path.is_file():
            return path, warnings
        warnings.append(f"night {name} has no report.json (still running, or it stopped before writing one)")
    raise ReportError("no night reports: nights/<night>/report.json is missing under the RSI sandbox"
                      + (f" ({'; '.join(warnings)})" if warnings else ""))


def _check_map(obj: Any, name: str, keys: tuple[str, ...], p: list[str]) -> None:
    for k in keys:
        v = obj.get(k)
        if v is not None and not _count(v) and not (_num(v) and v >= 0 and k in ("usd", "total_usd", "cost_usd",
                                                                                  "reported_usd", "estimated_usd")):
            p.append(f"{name}.{k}: not a non-negative number")


def validate_report(rep: Any) -> list[str]:
    """Problems with an rsi-night/1 report (contract 2.5); empty means valid. Ids and hashes are checked
    strictly because the brief turns them into commands."""
    if not isinstance(rep, dict):
        return ["not a JSON object"]
    if rep.get("format") != REPORT_FORMAT:
        return [f"format is {rep.get('format')!r}, expected {REPORT_FORMAT!r}"]
    p: list[str] = []
    for key, typ in (("night", str), ("status", str), ("checkpoint", dict), ("delta", dict), ("patterns", list),
                     ("candidates", list), ("experiments", list), ("costs", dict), ("health", dict),
                     ("pending_decisions", list)):
        if not isinstance(rep.get(key), typ):
            p.append(f"{key}: missing or not a {'JSON object' if typ is dict else typ.__name__}")
    if p:
        return p
    if not NIGHT_RE.fullmatch(rep["night"]):
        p.append(f"night {rep['night']!r} is not a night key")
    if rep["status"] not in NIGHT_STATUSES:
        p.append(f"status {rep['status']!r} is not one of {', '.join(NIGHT_STATUSES)}")
    if "notes" in rep and not (isinstance(rep["notes"], list) and all(isinstance(n, str) for n in rep["notes"])):
        p.append("notes: not a list of strings")
    sk = rep.get("skipped")                               # optional: the night job did not start the night
    if sk is not None and not (isinstance(sk, dict) and sk.get("reason") in SKIP_REASONS
                               and isinstance(sk.get("summary", ""), str) and isinstance(sk.get("items", []), list)):
        p.append(f"skipped: not an object with reason {' or '.join(SKIP_REASONS)}, a summary and items")
    pw = rep.get("prerequisite_warnings")
    if pw is not None and not (isinstance(pw, list) and all(isinstance(w, str) for w in pw)):
        p.append("prerequisite_warnings: not a list of strings")
    pr = rep.get("prerequisites")                         # optional (R2): the night's own prerequisite checks
    if pr is not None and not (isinstance(pr, dict) and isinstance(pr.get("skill_refresh", {}), dict)):
        p.append("prerequisites: not an object with a skill_refresh object")
    if rep.get("coverage") is not None:                   # optional (R4): the coverage rows the night wrote
        p += NC.report_problems(rep["coverage"])
        if isinstance(rep["coverage"], dict) and rep["coverage"].get("run_key") != rep["night"]:
            p.append("coverage.run_key: not the report's night")
    _check_map(rep["delta"], "delta", COUNTS, p)
    for name in ("patterns", "candidates"):
        p += [f"{name}[{i}]: not a JSON object" for i, x in enumerate(rep[name]) if not isinstance(x, dict)]
    for i, c in enumerate(rep["candidates"]):             # optional (2026-10-25): why each idea was thrown out
        if isinstance(c, dict) and c.get("hypotheses_rejected") is not None and not (
                isinstance(c["hypotheses_rejected"], list) and all(isinstance(x, dict) for x in c["hypotheses_rejected"])):
            p.append(f"candidates[{i}].hypotheses_rejected: not a list of objects")
    bl = rep.get("blocked")                               # optional (2026-10-25): ideas no experiment can use
    if bl is not None and not (isinstance(bl, dict) and isinstance(bl.get("groups"), dict)
                               and isinstance(bl.get("targets"), list)):
        p.append("blocked: not an object with groups and targets")
    for i, e in enumerate(rep["experiments"]):
        where = f"experiments[{i}]"
        if not isinstance(e, dict):
            p.append(f"{where}: not a JSON object")
            continue
        eid, st, h, pid = e.get("experiment_id"), e.get("status"), e.get("artifact_hash"), e.get("proposal_id")
        if eid is not None and not (isinstance(eid, str) and EXPERIMENT_RE.fullmatch(eid)):
            p.append(f"{where}.experiment_id: not an experiment id")
        if st not in EXPERIMENT_STATUSES:
            p.append(f"{where}.status {st!r} is not one of {', '.join(EXPERIMENT_STATUSES)}")
        if e.get("verdict") is not None and e["verdict"] not in VERDICTS:
            p.append(f"{where}.verdict {e['verdict']!r} is not one of {', '.join(VERDICTS)}")
        if h is not None and not (isinstance(h, str) and RA.HEX64.fullmatch(h)):
            p.append(f"{where}.artifact_hash: not a sha256 hex digest")
        if pid is not None and not (isinstance(pid, str) and UUID_RE.fullmatch(pid)):
            p.append(f"{where}.proposal_id: not a proposal uuid")
        if st == "staged" and (e.get("verdict") != "IMPROVES" or not h or not pid or not eid):
            p.append(f"{where}: staged needs verdict IMPROVES, an experiment_id, an artifact_hash and a proposal_id")
        if e.get("risk") is not None and e["risk"] not in RISKS:
            p.append(f"{where}.risk {e['risk']!r} is not one of {', '.join(RISKS)}")
        c = e.get("confidence")
        if c is not None and not (_num(c) and 0 <= c <= 1):
            p.append(f"{where}.confidence: not a number in [0, 1]")
        for k, typ in (("arms", dict), ("hypothesis", dict), ("verdict_reasons", list), ("evidence_refs", list),
                       ("judge", dict), ("jev", dict), ("evidence_measurements", list), ("unlinked_refs", list),
                       ("judge_pairs", list), ("test_setup", dict)):
            if e.get(k) is not None and not isinstance(e[k], typ):
                p.append(f"{where}.{k}: wrong type")
        _check_map(e, where, ("cost_usd",), p)
    costs = rep["costs"]
    for k in ("by_basis", "model_bases"):                 # optional (Mission 005.5): where each USD came from
        v = costs.get(k, {})
        if not isinstance(v, dict) or not all(isinstance(x, dict) for x in v.values()):
            p.append(f"costs.{k}: not an object of objects")
    _check_map(costs, "costs", ("reported_usd", "estimated_usd"), p)
    for k in ("by_model", "providers"):
        v = costs.get(k, {})
        if not isinstance(v, dict) or not all(isinstance(x, dict) for x in v.values()):
            p.append(f"costs.{k}: not an object of objects")
            continue
        for name, row in v.items():
            _check_map(row, f"costs.{k}.{name}", ("calls", "tokens_in", "tokens_out", "requests", "usd"), p)
    _check_map(costs, "costs", ("sandbox_runs", "total_usd"), p)
    if not all(isinstance(v, str) for v in rep["health"].values()):
        p.append("health: values must be strings")
    for i, pid in enumerate(rep["pending_decisions"]):
        if not (isinstance(pid, str) and UUID_RE.fullmatch(pid)):
            p.append(f"pending_decisions[{i}]: not a proposal uuid")
    return p


def load_report(path: Path) -> tuple[dict[str, Any], str]:
    """(report, sha256 of its bytes). ReportError unless it is a valid rsi-night/1 report for its directory."""
    rel = f"nights/{path.parent.name}/report.json"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReportError(f"cannot read {rel} ({type(exc).__name__})") from None
    try:
        rep = json.loads(raw)
    except ValueError:
        raise ReportError(f"{rel} is not valid JSON") from None
    problems = validate_report(rep)
    if problems:
        raise ReportError(f"{rel} is not a valid {REPORT_FORMAT} report: " + "; ".join(problems[:8])
                          + (f"; and {len(problems) - 8} more" if len(problems) > 8 else ""))
    if rep["night"] != path.parent.name:
        raise ReportError(f"{rel} says night {rep['night']!r}, but it is in the directory of night {path.parent.name!r}")
    return rep, hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- database rows (read-only)


@dataclass
class Db:
    status: str = "skipped"                  # ok | unavailable | skipped
    detail: str = ""
    proposals: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)          # staged, unexpired proposal ids, oldest first
    evals: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stored: dict[str, Any] = field(default_factory=dict)       # artifact hash -> rsi_artifacts.files
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)   # by id and by dedupe_key
    experiments: dict[str, dict[str, Any]] = field(default_factory=dict)
    backlog: int | None = None               # candidates kept for future nights (new | deferred)
    watch: list[dict[str, Any]] = field(default_factory=list)   # deviations to look at by hand (_WATCH_SQL)


# The watch list (folded in from the deviations-daily task, 2026-10-26): deviations of the last WATCH_HOURS
# (72 on Mondays, so the weekend is covered) that need Vilmer's eyes rather than an experiment:
# - first-step: a required first step (name the session, read the design system / lessons / SKILL.md) came after
#   a write, build or eval;
# - guard-bypass: a push-and-merge-policy deviation Jev left as Vilmer's own override, whose text reads like
#   getting around a denied command or a guard.
WATCH_HOURS, WATCH_MAX = 24, 8
# One SQL literal: the old digest joined three literals as '…' '…', and SQL reads '' inside a literal as a quote
# character, so its regex wanted a stray ' and almost never matched.
_FIRST_STEP = ("(d.rule_id = 'design-system' or d.what ~* '("
               "nam(e|ing) (this|the) session|session title|set(ting)? the title|step 0"
               "|(read|load|nam)[a-z]* [^.]{0,60}(first|at the start|before (any|it|step))"
               "|(first|at the start of [^.]{0,30}),? [^.]{0,30}(read|load|nam))')")
_OVERRIDE = ("(d.rule_id = 'push-and-merge-policy' and d.jev_verdict = 'leave' "
             "and coalesce(d.jev_confidence, 0) >= 0.5)")
_BYPASS = "d.what ~* '(denied|deny|guard|blocked|bypass|get(ting)? around|work(ed)? around|split)'"
_WATCH_SQL = f"""
select d.created_at, d.session_id, s.title, d.rule_id, d.instruction_source, left(d.what, 200) as what,
       case when {_FIRST_STEP} then 'first-step' else 'guard-bypass' end as kind
from deviations d left join sessions s using (session_id)
where d.created_at > now() - make_interval(hours => %s)
  and ({_FIRST_STEP} or ({_OVERRIDE} and {_BYPASS}))
order by d.created_at limit {WATCH_MAX}
"""


def watch_hours(now: datetime | None = None) -> int:
    return WATCH_HOURS * 3 if (now or datetime.now()).astimezone().weekday() == 0 else WATCH_HOURS


_PROPOSALS_SQL = """
select p.id::text as id, p.artifact_hash, p.experiment_id, p.title, p.summary, p.rationale, p.evidence_refs, p.risk,
       p.confidence, p.expected, p.targets, p.status, p.created_at, p.expires_at,
       (p.status = 'staged' and p.expires_at > now()) as pending, e.night as night, e.verdict as experiment_verdict
from rsi_proposals p left join rsi_experiments e on e.id = p.experiment_id
where p.id::text = any(%s) or (p.status = 'staged' and p.expires_at > now())
order by p.created_at, p.id
"""


def read_db(rep: Mapping[str, Any], *, db_url: str | None = None) -> Db:
    """The rows behind the report, read in a read-only transaction. Never raises for a database problem: the
    brief then says the rows were not read."""
    try:
        if db_url is None and config.backend() != "postgres":
            return Db("skipped", "the REST backend has no RSI tables; database rows not read")
    except config.BackendError as exc:
        return Db("skipped", str(exc))
    exps = rep["experiments"]
    pids = sorted({x for x in [*(e.get("proposal_id") for e in exps), *rep["pending_decisions"]] if x})
    hashes = {e["artifact_hash"] for e in exps if e.get("artifact_hash")}
    cids = sorted({str(c["id"]) for c in rep["candidates"] if c.get("id")})
    keys = sorted({str(k) for k in [*(c.get("key") for c in rep["candidates"]), *(e.get("candidate_key") for e in exps)] if k})
    eids = sorted({e["experiment_id"] for e in exps if e.get("experiment_id")})
    db = Db("ok")
    try:
        with store.connect(db_url, connect_timeout=10) as conn:
            conn.read_only = True
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(_PROPOSALS_SQL, (pids,))
                for r in cur.fetchall():
                    db.proposals[r["id"]] = r
                    hashes.add(r["artifact_hash"])
                    if r["pending"]:
                        db.pending.append(r["id"])
                hs = sorted(hashes)
                cur.execute("select artifact_hash, suite, suite_version, verdict, model, effort from rsi_eval_results "
                            "where artifact_hash = any(%s) order by artifact_hash, suite, created_at", (hs,))
                for r in cur.fetchall():
                    db.evals.setdefault(r.pop("artifact_hash"), []).append(r)
                cur.execute("select hash, files from rsi_artifacts where hash = any(%s)", (hs,))
                db.stored = {r["hash"]: r["files"] for r in cur.fetchall()}
                cur.execute("select id::text as id, dedupe_key, status, seen_count, rank from rsi_candidates "
                            "where id::text = any(%s) or dedupe_key = any(%s)", (cids, keys))
                for r in cur.fetchall():
                    db.candidates[r["id"]] = db.candidates[r["dedupe_key"]] = r
                cur.execute("select count(*) as n from rsi_candidates where status in ('new', 'deferred')")
                db.backlog = int(cur.fetchone()["n"])
                cur.execute("select id, night, status, verdict from rsi_experiments where id = any(%s)", (eids,))
                db.experiments = {r["id"]: r for r in cur.fetchall()}
                cur.execute(_WATCH_SQL, (watch_hours(),))
                db.watch = cur.fetchall()
    except psycopg.Error as exc:
        return Db("unavailable", type(exc).__name__)
    return db


# --------------------------------------------------------------------------- the coverage verdict (data-use R4)

# "No problem found" only when coverage.verdict says all_clear for the night's required datasets
# (night_coverage.requirements); any other answer says why it is not one.
VERDICT_HEADLINES = {"all_clear": "No problem found", "findings": "Findings", "incomplete": "Analysis incomplete",
                     "not_recorded": "Verdict not recorded", "unknown": "Verdict unknown"}
MAX_GAPS = 8


def _verdict(conclusion: str, detail: str = "", **kw: Any) -> dict[str, Any]:
    return {"conclusion": conclusion, "headline": VERDICT_HEADLINES[conclusion], "detail": text(detail, 200),
            "findings": 0, "gaps": [], "empty": [], **kw}


def report_verdict(rep: Mapping[str, Any]) -> dict[str, Any] | None:
    """The verdict the report alone decides (nothing recorded), or None when the database must be read."""
    cov = rep.get("coverage")
    if not isinstance(cov, dict):
        why = ("the night did not run" if rep.get("status") == "preflight_failed" and rep.get("skipped")
               else "the night report has no coverage record")
        return _verdict("not_recorded", why)
    if cov.get("enabled") is not True:
        return _verdict("not_recorded", "coverage writing was off for this night (AMEM_COVERAGE_WRITE=0)")
    return None


def coverage_verdict(rep: Mapping[str, Any], *, db_url: str | None = None) -> dict[str, Any]:
    """coverage.verdict for the night's job and run key, read-only. A database problem makes it `unknown`, never
    clear."""
    early = report_verdict(rep)
    if early is not None:
        return early
    try:
        if db_url is None and config.backend() != "postgres":
            return _verdict("unknown", "the REST backend has no coverage tables")
        reqs = NC.requirements(rep["coverage"].get("required") or [])
        with store.connect(db_url, connect_timeout=10) as conn:
            conn.read_only = True
            v = CV.verdict(conn, job=NC.JOB, run_key=rep["night"], required=reqs)
            rows = {r["dataset"]: r["status"] for r in CV.consumption(conn, job=NC.JOB, run_key=rep["night"], limit=200)}
    except (psycopg.Error, config.BackendError, ValueError) as exc:
        return _verdict("unknown", f"coverage not read ({type(exc).__name__})")
    gaps = [{"dataset": text(g.dataset, 80), "distinction": g.distinction, "detail": text(g.detail, 120)}
            for g in v.gaps]
    empty = [text(d, 80) for d in v.datasets if rows.get(d) == "analysis_not_run"] if v.all_clear else []
    return _verdict(v.conclusion, findings=v.findings, gaps=gaps, empty=empty)


def gap_list(gaps: list[Mapping[str, Any]]) -> str:
    """Gaps, short: one entry per dataset with its distinctions and the first specific reason."""
    by: dict[str, list[Mapping[str, Any]]] = {}
    for g in gaps:
        by.setdefault(str(g.get("dataset")), []).append(g)
    out = []
    for ds, gs in list(by.items())[:MAX_GAPS]:
        why = next((str(g["detail"]) for g in gs if g.get("detail")
                    and not str(g["detail"]).startswith(("consumption status", "snapshot status"))), "")
        out.append(f"{ds} {', '.join(dict.fromkeys(str(g.get('distinction')) for g in gs))}"
                   + (f" ({text(why, 70)})" if why else ""))
    return "; ".join(out) + (f"; and {len(by) - MAX_GAPS} more datasets" if len(by) > MAX_GAPS else "")


def verdict_line(v: Mapping[str, Any]) -> str:
    """The verdict as one markdown line. Only all_clear says "No problem found"."""
    gaps = v.get("gaps") or []
    listed = gap_list(gaps)
    c = v.get("conclusion")
    if c == "all_clear":
        empty = f" Nothing new to check in: {', '.join(v['empty'])}." if v.get("empty") else ""
        return f"**Verdict: No problem found.** Every dataset the night's conclusions need was analysed.{empty}"
    if c == "findings":
        return (f"**Verdict: Findings ({v.get('findings', 0)}).** See the patterns and experiments below."
                + (f" Coverage gaps: {listed}." if gaps else ""))
    if c == "incomplete":
        return f"**Verdict: Analysis incomplete.** Missing evidence, so this is not an all clear: {listed or '-'}."
    return f"**{v.get('headline') or 'Verdict unknown'}:** {v.get('detail') or '-'}. This is not an all clear."


# --------------------------------------------------------------------------- building blocks


def actions(proposal_id: str, artifact_hash: str | None) -> dict[str, str]:
    """The existing approval commands. `--hash` binds Apply / Reject to the tested artifact."""
    bind = f" --hash {artifact_hash}" if artifact_hash else ""
    return {"apply": f"amem rsi decide {proposal_id} apply{bind}",
            "reject": f"amem rsi decide {proposal_id} reject{bind}",
            "details": f"amem rsi show {proposal_id}"}


def _small(d: Any, keys: tuple[str, ...]) -> dict[str, Any] | None:
    """A judge / Jev summary reduced to scalars (numbers, enums, short text)."""
    if not isinstance(d, dict):
        return None
    out = {k: (text(d[k], 60) if isinstance(d[k], str) else d[k]) for k in keys
           if k in d and (d[k] is None or isinstance(d[k], (bool, int, float, str)))}
    return out or None


JUDGE_FIELDS = ("mode", "judged", "wins_a", "wins_b", "ties", "errors", "b_winrate")
JEV_FIELDS = ("mode", "status", "effect", "confidence", "harness_problem")


def evaluation(adir: Path, h: str) -> dict[str, Any]:
    """What the sandbox-replay record bound to artifact `h` says (written by the sandbox, not by a model): its
    verdict, the agent, the judge's and Jev's summaries, and whether the judge and Jev were independent (a live
    model judge that judged at least one pair, and live Jev that answered)."""
    recs = [r for r in RA.eval_records(adir) if r.get("suite") == REPLAY_SUITE and r.get("artifact_hash") == h]
    if not recs:
        return {"recorded": False, "verdict": None, "agent": None, "judge": None, "jev": None, "independent": False}
    res = _obj(recs[-1].get("results"))
    judge, jev = _small(res.get("judge"), JUDGE_FIELDS), _small(res.get("jev"), JEV_FIELDS)
    independent = bool(judge and judge.get("mode") in LIVE_JUDGES and _count(judge.get("judged")) and judge["judged"] > 0
                       and jev and jev.get("mode") == "live" and jev.get("status") == "ok")
    return {"recorded": True, "verdict": text(recs[-1].get("verdict"), 20), "agent": text(res.get("agent"), 20) or None,
            "judge": judge, "jev": jev, "independent": independent}


def judgment_text(judge: Any, jev: Any) -> str:
    """One line: the blind judge's and Jev's results, or what is missing."""
    parts = []
    if isinstance(judge, dict) and judge.get("mode"):
        n, wb, wa = judge.get("judged"), judge.get("wins_b"), judge.get("wins_a")
        rate = judge.get("b_winrate")
        ties = judge.get("ties")
        parts.append(f"judge {text(judge['mode'], 20)}: candidate won {_fmt(wb)}/{_fmt(n)}, baseline {_fmt(wa)}"
                     + (f", ties {_fmt(ties)}" if _count(ties) and ties else "")
                     + (f", B win rate {rate:.2f}" if _num(rate) else ""))
    else:
        parts.append("judge: not recorded")
    if isinstance(jev, dict) and jev.get("mode"):
        conf = jev.get("confidence")
        parts.append(f"Jev {text(jev['mode'], 20)}: {text(jev.get('effect') or jev.get('status') or '?', 30)}"
                     + (f" ({conf:.2f})" if _num(conf) else ""))
    else:
        parts.append("Jev: not recorded")
    return "; ".join(parts)


def provider_summaries(value: Any) -> list[dict[str, Any]]:
    """Separate provider evidence for display only. Never manufacture pooled judge/Jev statistics."""
    if not isinstance(value, list):
        return []
    out = []
    for p in value[:4]:
        if not isinstance(p, dict):
            continue
        arms = _obj(p.get("arms"))
        a, b = _obj(arms.get("A")), _obj(arms.get("B"))
        out.append({"provider": text(p.get("provider"), 40) or "unknown",
                    "verdict": text(p.get("verdict"), 30) or None,
                    "baseline_pass": arm_rate(arms, "A"), "candidate_pass": arm_rate(arms, "B"),
                    "baseline_checked": a.get("checked") if _count(a.get("checked")) else None,
                    "candidate_checked": b.get("checked") if _count(b.get("checked")) else None,
                    "judge": _small(p.get("judge"), JUDGE_FIELDS), "jev": _small(p.get("jev"), JEV_FIELDS)})
    return out


def experiment_judgments(e: Mapping[str, Any]) -> str | None:
    providers = provider_summaries(e.get("provider_results"))
    if providers:
        return " | ".join(f"{p['provider']}: {judgment_text(p['judge'], p['jev'])}" for p in providers)
    return judgment_text(e.get("judge"), e.get("jev")) if e.get("judge") or e.get("jev") else None


def apply_blockers(*, verdict: Any, change: Mapping[str, Any], ev: Mapping[str, Any], db: Db, proposal: Mapping[str, Any],
                   h: str, missing_suites: list[str]) -> list[str]:
    """Why Apply is not offered for one staged artifact; empty = Apply may be shown. Apply needs all of: verdict
    IMPROVES, an artifact that verifies (and matches the database copy), every required evaluation passing, a
    sandbox-replay record bound to the hash with verdict `improves` from an independent judge and live Jev, and,
    in the database, a pending proposal for exactly this hash."""
    out = []
    if verdict != "IMPROVES":
        out.append(f"verdict {verdict or 'none'} is not IMPROVES")
    if not change.get("verified"):
        out.append("the artifact does not verify")
    elif change.get("matches_db") is False:
        out.append("the local artifact differs from the database copy")
    if missing_suites:
        out.append(f"required evaluation(s) missing or not passing: {', '.join(missing_suites)}")
    if not ev.get("recorded"):
        out.append("no sandbox-replay record is bound to this hash")
    else:
        if ev.get("verdict") != "improves":
            out.append(f"the bound sandbox-replay record says {ev.get('verdict') or 'nothing'}, not improves")
        if not ev.get("independent"):
            j, v = ev.get("judge") or {}, ev.get("jev") or {}
            out.append(f"not independently evaluated (judge {j.get('mode') or 'none'}, Jev {v.get('mode') or 'none'}"
                       f"/{v.get('status') or 'none'}): stand-in evaluations never offer Apply")
    if db.status != "ok":
        out.append("the proposal could not be confirmed in the database (rows not read)")
    else:
        if not proposal:
            out.append("no proposal row in the database")
        else:
            if proposal.get("artifact_hash") != h:
                out.append("the proposal points at another artifact")
            if proposal.get("status") != "staged" or not proposal.get("pending"):
                out.append(f"the proposal is not pending (now {proposal.get('status')})")
        if h not in db.stored:
            out.append("the artifact is not registered in the database")
    return out


def gated_actions(proposal_id: str, artifact_hash: str | None, blockers: list[str]) -> dict[str, str | None]:
    """actions(), with Apply removed (None) while anything blocks it. Reject and Details always stay."""
    a: dict[str, str | None] = dict(actions(proposal_id, artifact_hash))
    if blockers or not artifact_hash:
        a["apply"] = None
    return a


def required_suites() -> list[str]:
    from agent_memory import rsi_approval      # its criteria.json reader; read-only
    return rsi_approval.required_suites()


def _decode(b: bytes) -> str:
    return b.decode("utf-8", "replace")


def file_change(f: Mapping[str, Any], before: bytes | None, after: bytes) -> dict[str, Any]:
    """One file of an artifact: lines added / removed and a short excerpt of the changed lines."""
    a_lines = _decode(after).splitlines()
    out: dict[str, Any] = {"path": text(f.get("path"), 200), "kind": text(f.get("kind"), 40) or None,
                           "rule_id": text(f.get("rule_id"), 120) or None, "before_bytes": None,
                           "after_bytes": len(after), "added": len(a_lines), "removed": 0, "excerpt": [], "note": None}
    if before is None:
        out["note"] = "the before bytes are not in the artifact; every line counts as added"
        out["excerpt"] = [diff_line("+" + ln) for ln in a_lines[:MAX_EXCERPT]]
        return out
    out["before_bytes"] = len(before)
    b_lines = _decode(before).splitlines()
    added = removed = 0
    excerpt: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b_lines, a_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        changed = [("-", ln) for ln in b_lines[i1:i2]] + [("+", ln) for ln in a_lines[j1:j2]]
        removed += i2 - i1
        added += j2 - j1
        excerpt += [diff_line(sign + ln) for sign, ln in changed][: MAX_EXCERPT - len(excerpt)]
    out.update(added=added, removed=removed, excerpt=excerpt)
    return out


def artifact_change(adir: Path, stored: Any) -> dict[str, Any]:
    """What the staged artifact would change, verified the way the promotion worker verifies it."""
    out: dict[str, Any] = {"dir": f"artifacts/{adir.name}", "verified": False, "error": None, "matches_db": None,
                           "files": []}
    try:
        m, _, after = RA.load_artifact(adir)
    except RA.ArtifactError as exc:
        out["error"] = text(exc, 200)
        return out
    out["verified"] = True
    if stored is not None:
        try:
            out["matches_db"] = {k: v.decode() for k, v in after.items()} == stored
        except UnicodeDecodeError:
            out["matches_db"] = False
    for f in m["files"]:
        bsha = str(f.get("before_sha256") or "")
        bpath = adir / "before" / bsha
        before = bpath.read_bytes() if RA.HEX64.fullmatch(bsha) and bpath.is_file() else None
        if before is not None and RA.sha256_bytes(before) != bsha:
            before = None
        out["files"].append(file_change(f, before, after[f["after_sha256"]]))
    return out


def _flat(d: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (d.items() if isinstance(d, dict) else []):
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if v2 is None or isinstance(v2, (bool, int, float, str)):
                    out[f"{k}.{k2}"] = v2
        elif v is None or isinstance(v, (bool, int, float, str)):
            out[str(k)] = v
    return out


def compare_arms(arms: Any) -> list[dict[str, Any]]:
    """Baseline (arm A) vs candidate (arm B), metric by metric, with the change for numbers."""
    a, b = (_flat((arms or {}).get(x)) if isinstance(arms, dict) else {} for x in ("A", "B"))
    rows = []
    for k in sorted(set(a) | set(b))[:MAX_METRICS]:
        va, vb = a.get(k), b.get(k)
        delta = round(vb - va, 6) if _num(va) and _num(vb) else None
        rows.append({"name": text(k, 60), "baseline": va if _num(va) or va is None or isinstance(va, bool) else text(va, 40),
                     "candidate": vb if _num(vb) or vb is None or isinstance(vb, bool) else text(vb, 40), "delta": delta})
    return rows


def _refs(*lists: Any, cap: int = MAX_REFS) -> list[str]:
    seen: list[str] = []
    for lst in lists:
        for r in lst if isinstance(lst, list) else []:
            t = text(r, 120)
            if t and t not in seen:
                seen.append(t)
    return seen[:cap]


def _obj(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _basename(t: Any) -> str:
    name = PurePosixPath(str(t)).name
    return name if FILE_RE.fullmatch(name) else "file"


def _evals_for(h: str, db: Db, adir: Path) -> tuple[list[dict[str, Any]], str]:
    if db.status == "ok":
        rows = db.evals.get(h, [])
        return [{"suite": text(r["suite"], 40), "verdict": r["verdict"], "model": text(r.get("model"), 60) or None}
                for r in rows], "database"
    recs = [r for r in RA.eval_records(adir) if r.get("artifact_hash") == h]
    return [{"suite": text(r.get("suite"), 40), "verdict": text(r.get("verdict"), 20), "model": text(r.get("model"), 60) or None}
            for r in recs], "artifact"


def proposal_block(e: Mapping[str, Any], db: Db, artifacts_root: Path, suites: list[str]) -> dict[str, Any]:
    """One staged proposal: what, why, evidence, baseline vs candidate, risk, the hash, the actions."""
    pid, h = e["proposal_id"], e["artifact_hash"]
    p = db.proposals.get(pid) or {}
    hyp = e.get("hypothesis") or {}
    adir = artifacts_root / h
    change = artifact_change(adir, db.stored.get(h) if db.status == "ok" else None)
    evals, source = _evals_for(h, db, adir)
    warnings: list[str] = []
    if not change["verified"]:
        warnings.append(f"the artifact does not verify ({change['error']}): do not apply; promotion refuses it")
    elif change["matches_db"] is False:
        warnings.append("the local artifact bytes differ from the database copy: promotion refuses it")
    if db.status == "ok":
        if not p:
            warnings.append("no proposal row with this id in the database")
        else:
            if p["artifact_hash"] != h:
                warnings.append(f"the proposal points at another artifact ({p['artifact_hash'][:12]}); the actions are "
                                "bound to the tested hash, so decide records nothing")
            if p["status"] != "staged" or not p["pending"]:
                warnings.append(f"the proposal is no longer pending (now {p['status']})")
        if h not in db.stored:
            warnings.append("the artifact is not registered in the database")
    passing = {x["suite"] for x in evals if x["verdict"] in PASSING}
    missing = [s for s in suites if s not in passing]
    if missing:
        warnings.append(f"required evaluation(s) missing or not passing: {', '.join(missing)}; promotion refuses it")
    ev = evaluation(adir, h)
    if not ev["recorded"]:           # shown from the report; without the bound record it never counts as independent
        ev.update(judge=_small(e.get("judge"), JUDGE_FIELDS), jev=_small(e.get("jev"), JEV_FIELDS))
    providers = provider_summaries(e.get("provider_results"))
    if providers:
        ev["provider_results"] = providers  # display from report; does not change independent/apply blockers
    blockers = apply_blockers(verdict=e.get("verdict"), change=change, ev=ev, db=db, proposal=p, h=h,
                              missing_suites=missing)
    risk = e.get("risk") or _obj(p.get("risk")).get("level")
    conf = e.get("confidence") if e.get("confidence") is not None else p.get("confidence")
    return {
        "experiment_id": e["experiment_id"], "candidate_key": text(e.get("candidate_key"), 120) or None,
        "proposal_id": pid, "title": text(p.get("title") or hyp.get("what") or e["experiment_id"], 120),
        "what": text(hyp.get("what") or p.get("summary"), 600), "target": text(e.get("target"), 200) or None,
        "change": change,
        "why": text(hyp.get("why") or _obj(p.get("rationale")).get("why"), 800),
        "evidence_refs": _refs(e.get("evidence_refs"), p.get("evidence_refs")),
        "baseline_vs_candidate": {"verdict": e["verdict"], "verdict_reasons": [text(r, 200) for r in (e.get("verdict_reasons") or [])[:6]],
                                  "metrics": compare_arms(e.get("arms")), "evals": evals, "evals_from": source,
                                  "required_suites": suites},
        "evaluation": ev,
        "risk": risk if risk in RISKS else None, "confidence": conf if _num(conf) else None,
        "artifact_hash": h, "proposal_status": p.get("status") if p else None, "expires_at": _iso(p.get("expires_at")),
        "cost_usd": e.get("cost_usd"), "actions": gated_actions(pid, h, blockers), "apply_blocked": blockers,
        "warnings": warnings,
    }


WHY_NOT = {
    "not_promotable": "only IMPROVES can be staged",
    "failed": "the experiment failed before it could be judged",
    "interrupted": "interrupted when the night was aborted; nothing was staged",
    "skipped_budget": "skipped: the budget ran out before it ran",
    "deferred_policy": "deferred: one experiment per candidate per night; its next hypothesis remains pending",
}


def failed_item(e: Mapping[str, Any]) -> dict[str, Any]:
    hyp = e.get("hypothesis") or {}
    v = e.get("verdict")
    why = WHY_NOT.get(e["status"], "")
    decision = e.get("budget_decision")
    if e["status"] == "skipped_budget" and isinstance(decision, dict):
        why = (f"stopped by budget limit {decision.get('resource') or 'unknown'}: "
               f"{text(decision.get('detail'), 200) or e.get('skip_reason') or 'no detail recorded'}")
    return {"experiment_id": e.get("experiment_id"), "candidate_key": text(e.get("candidate_key"), 120) or None,
            "verdict": v, "status": e["status"], "why_not_staged": why,
            "reasons": [text(r, 200) for r in (e.get("verdict_reasons") or [])[:6]],
            "skip_reason": text(e.get("skip_reason"), 120) or None,
            "budget_decision": e.get("budget_decision") if isinstance(e.get("budget_decision"), dict) else None,
            "what": text(hyp.get("what"), 300), "artifact_hash": e.get("artifact_hash"), "cost_usd": e.get("cost_usd"),
            "judgments": experiment_judgments(e)}


def arm_rate(arms: Any, arm: str) -> float | None:
    """The arm's check pass rate (rsi_replay arms: check_pass_rate; older reports: pass_rate)."""
    a = _obj(_obj(arms).get(arm))
    for k in ("check_pass_rate", "pass_rate"):
        if _num(a.get(k)):
            return float(a[k])
    return None


HEADLINES = {
    "ok": "The night finished.",
    "partial": "The night finished in part: some stages did not complete (see Service health).",
    "aborted": "The night was aborted: experiments that were running are marked interrupted.",
    "budget_exhausted": "The night stopped early: the budget ran out, and the remaining work was skipped.",
    "preflight_failed": "The night did not run: preflight failed (see Service health).",
}
SKIP_FALLBACK = {"prerequisites": "a prerequisite routine did not complete",
                 "credentials": "the credentials and budget check failed"}


def skip_info(rep: Mapping[str, Any]) -> dict[str, Any] | None:
    """The night job's skip (report["skipped"]): it never started the night, so nothing was read or checked."""
    sk = rep.get("skipped")
    if rep.get("status") != "preflight_failed" or not isinstance(sk, dict) or sk.get("reason") not in SKIP_REASONS:
        return None
    return {"reason": sk["reason"], "summary": text(sk.get("summary"), 300) or SKIP_FALLBACK[sk["reason"]],
            "items": [text(i, 260) for i in (sk.get("items") or [])[:MAX_NOTES] if isinstance(i, str)]}


WRITER_NEVER_RAN = "writer never ran"     # rsi_night.WRITER_NEVER_RAN: the report's own note, shown as a warning


def writer_info(rep: Mapping[str, Any]) -> dict[str, Any] | None:
    """The report's optional `writer` tally (2026-10-26): counts and the top fallback reason, or None."""
    w = rep.get("writer")
    if not isinstance(w, dict) or not all(_count(w.get(k)) for k in ("experiments", "wrote", "fell_back")):
        return None
    top = next((text(x.get("reason"), 200) for x in w.get("reasons") or [] if isinstance(x, dict)), "") or None
    return {"experiments": w["experiments"], "wrote": w["wrote"], "fell_back": w["fell_back"],
            "all_fell_back": w.get("all_fell_back") is True, "top_reason": top}


def headline(rep: Mapping[str, Any]) -> str:
    sk = skip_info(rep)
    if sk:
        return _sentence(f"The night did not run: {sk['summary']}")
    return HEADLINES[rep["status"]]


def skill_refresh_warning(rep: Mapping[str, Any]) -> list[str]:
    """A stale, failed or missing skill refresh (report.prerequisites.skill_refresh, data-use plan R2) as one
    prerequisite warning. Fresh, or a report without the field: none."""
    from agent_memory import skill_refresh as SR
    pr = rep.get("prerequisites")
    w = SR.warning(pr.get("skill_refresh")) if isinstance(pr, Mapping) else None
    return [text(w, 300)] if w else []


def build_brief(rep: Mapping[str, Any], db: Db, *, artifacts_root: Path, report: dict[str, str] | None = None,
                warnings: list[str] | None = None, now: datetime | None = None,
                verdict: dict[str, Any] | None = None) -> dict[str, Any]:
    """`verdict`: coverage_verdict's answer (make_brief reads it); without one, only what the report decides, else
    unknown (never clear)."""
    if verdict is None:
        verdict = report_verdict(rep) or _verdict("unknown", "coverage verdict not read")
    exps = rep["experiments"]
    suites = required_suites()
    passed = [proposal_block(e, db, artifacts_root, suites) for e in exps if e["status"] == "staged"]
    failed = [failed_item(e) for e in exps if e["status"] != "staged"]
    policy_deferred = sum(e["status"] == "deferred_policy" for e in exps)
    d = rep["delta"]
    skipped = skip_info(rep)
    sessions = {"since": text(d.get("since"), 40) or None, **{k: d.get(k) or 0 for k in COUNTS}}
    patterns = [{"key": text(x.get("key"), 80), "summary": text(x.get("summary"), 240),
                 "n": x.get("n") if _count(x.get("n")) else None,
                 "sessions": x.get("sessions") if _count(x.get("sessions")) else None,
                 "refs": _refs(x.get("refs"), cap=5)} for x in rep["patterns"][:MAX_PATTERNS]]
    cands = []
    for c in rep["candidates"]:
        row = db.candidates.get(str(c.get("id"))) or db.candidates.get(str(c.get("key"))) or {}
        cands.append({"id": text(c.get("id"), 60) or None, "key": text(c.get("key"), 120), "source": text(c.get("source"), 40),
                      "rank": c.get("rank") if _num(c.get("rank")) else None, "selected": c.get("selected") is True,
                      "reason": text(c.get("reason"), 200), "status": row.get("status"), "seen_count": row.get("seen_count")})
    cands.sort(key=lambda c: (not c["selected"], -(c["rank"] or 0.0)))
    experiments = [{"experiment_id": e.get("experiment_id"), "candidate_key": text(e.get("candidate_key"), 120) or None,
                    "verdict": e.get("verdict"), "status": e["status"], "cost_usd": e.get("cost_usd"),
                    "db_status": (db.experiments.get(e.get("experiment_id") or "") or {}).get("status"),
                    "baseline_pass": arm_rate(e.get("arms"), "A"), "candidate_pass": arm_rate(e.get("arms"), "B"),
                    "judge": _small(e.get("judge"), JUDGE_FIELDS), "jev": _small(e.get("jev"), JEV_FIELDS),
                    **({"provider_results": provider_summaries(e["provider_results"])}
                       if provider_summaries(e.get("provider_results")) else {})}
                   for e in exps]
    c = rep["costs"]
    mb = _obj(c.get("model_bases"))
    costs = {"by_model": [{"model": text(k, 60), **{f: v.get(f) or 0 for f in ("calls", "tokens_in", "tokens_out", "usd")},
                           "bases": {text(b, 20): u for b, u in sorted(_obj(mb.get(k)).items()) if _num(u)}}
                          for k, v in sorted((c.get("by_model") or {}).items())],
             "sandbox_runs": c.get("sandbox_runs") or 0,
             "providers": [{"name": text(k, 40), "requests": v.get("requests") or 0, "usd": v.get("usd") or 0}
                           for k, v in sorted((c.get("providers") or {}).items())],
             "by_basis": [{"basis": text(k, 20),
                           "kind": v.get("kind") if v.get("kind") in ("provider-reported", "estimate", "free")
                           else COST_KINDS.get(str(k), "estimate"),          # the report's own label wins
                           "records": v.get("records") or 0, "usd": v.get("usd") or 0.0}
                          for k, v in sorted(_obj(c.get("by_basis")).items())],
             "reported_usd": c.get("reported_usd") if _num(c.get("reported_usd")) else None,
             "estimated_usd": c.get("estimated_usd") if _num(c.get("estimated_usd")) else None,
             "total_usd": c.get("total_usd") or 0.0, "budget_exhausted": rep["status"] == "budget_exhausted"}
    services = {text(k, 32): text(v, 80) for k, v in rep["health"].items()}
    writer = writer_info(rep)
    writer_warning = ([f"{WRITER_NEVER_RAN}: {writer['fell_back']}/{writer['experiments']} experiments fell back to the "
                       f"stub change ({writer['top_reason'] or 'no reason recorded'}). No writer change was tested."]
                      if writer and writer["all_fell_back"] else [])
    health = {"services": services,
              "degraded": sorted(k for k, v in services.items() if v not in HEALTHY + NEUTRAL),
              "database": {"status": db.status, "detail": db.detail},
              "warnings": writer_warning + list(warnings or []),
              "reasons": {text(k, 32): [text(x, 160) for x in v[:3]]
                          for k, v in _obj(rep.get("health_reasons")).items() if isinstance(v, list)}}
    pending = pending_items(rep, db, artifacts_root=artifacts_root, suites=suites)
    n_pending = len(pending["items"])
    notes = [text(n, 200) for n in (rep.get("notes") or [])          # the writer's note is shown as a warning instead
             if not (writer_warning and str(n).startswith(WRITER_NEVER_RAN))][:MAX_NOTES]
    ck = rep["checkpoint"]
    return {
        "format": BRIEF_FORMAT, "night": rep["night"], "status": rep["status"],
        "generated_at": (now or datetime.now(UTC)).isoformat(), "report": report or {},
        "started_at": text(rep.get("started_at"), 40) or None, "finished_at": text(rep.get("finished_at"), 40) or None,
        "checkpoint": {"before": text(ck.get("before"), 40) or None, "after": text(ck.get("after"), 40) or None,
                       "advanced": ck.get("advanced") is True},
        "headline": headline(rep), "skipped": skipped, "verdict": verdict,
        "prerequisite_warnings": [text(w, 300) for w in (rep.get("prerequisite_warnings") or [])[:MAX_NOTES]]
                                 + skill_refresh_warning(rep),
        "summary": (f"{_plural(sum(1 for x in passed if x['actions']['apply']), 'improvement')} ready to apply"
                    + (f" ({len(passed) - sum(1 for x in passed if x['actions']['apply'])} more staged but not "
                       "ready)" if any(not x["actions"]["apply"] for x in passed) else "")
                    + f", {len(failed) - policy_deferred} failed or inconclusive"
                    + (f", {policy_deferred} hypotheses deferred by policy "
                       f"({sum(bool(e.get('experiment_id')) for e in exps)} experiments executed)"
                       if policy_deferred else "")
                    + f", {_plural(n_pending, 'decision')} waiting for you. "
                    f"Total cost {usd(costs['total_usd'])}."),
        "notes": notes,
        "sessions": sessions,
        "patterns": {"total": len(rep["patterns"]), "items": patterns},
        "watch": {"hours": watch_hours(now), "status": db.status,
                  "items": [{"kind": w.get("kind"), "rule": text(w.get("rule_id"), 80),
                             "source": text(w.get("instruction_source"), 60),
                             "session": text(w.get("title") or w.get("session_id"), 80),
                             "what": text(w.get("what"), 200)} for w in db.watch]},
        "candidates": {"total": len(cands), "selected": sum(x["selected"] for x in cands), "backlog": db.backlog,
                       "items": cands[:MAX_CANDIDATES]},
        "experiments": {"total": len(exps), "items": experiments},
        "passed": passed, "failed": failed, "costs": costs, "health": health, "pending_decisions": pending,
        "sections": list(SECTIONS),
    }


def _pending_gate(pid: str, h: str | None, verdict: Any, db: Db, artifacts_root: Path | None,
                  suites: list[str] | None) -> tuple[list[str], dict[str, Any] | None]:
    """(Apply blockers, evaluation) for one pending proposal: the same rule as a this-night block."""
    if not h or not RA.HEX64.fullmatch(str(h)) or artifacts_root is None:
        return ["no tested artifact hash for this proposal"], None
    adir = artifacts_root / h
    change = artifact_change(adir, db.stored.get(h) if db.status == "ok" else None)
    evals, _ = _evals_for(h, db, adir)
    passing = {x["suite"] for x in evals if x["verdict"] in PASSING}
    missing = [s for s in (suites if suites is not None else list(DEFAULT_SUITES)) if s not in passing]
    ev = evaluation(adir, h)
    return apply_blockers(verdict=verdict, change=change, ev=ev, db=db, proposal=db.proposals.get(pid) or {}, h=h,
                          missing_suites=missing), ev


def pending_items(rep: Mapping[str, Any], db: Db, *, artifacts_root: Path | None = None,
                  suites: list[str] | None = None) -> dict[str, Any]:
    """Every proposal that waits for Vilmer (staged, not expired), this night's and earlier nights'. Each item
    carries its exact artifact hash and evidence refs; Apply only when nothing blocks it (apply_blockers)."""
    tested = {e["proposal_id"]: e for e in rep["experiments"] if e.get("proposal_id") and e.get("artifact_hash")}
    items: list[dict[str, Any]] = []
    gone: list[dict[str, Any]] = []
    if db.status == "ok":
        for pid in db.pending:
            p = db.proposals[pid]
            e = tested.get(pid) or {}
            h = e.get("artifact_hash") or p["artifact_hash"]
            verdict = e.get("verdict") or p.get("experiment_verdict")
            blockers, ev = _pending_gate(pid, h, verdict, db, artifacts_root, suites)
            items.append({"proposal_id": pid, "title": text(p["title"], 120),
                          "night": text(p.get("night"), 40) or p["created_at"].date().isoformat(),
                          "this_night": pid in tested, "status": p["status"], "created_at": _iso(p["created_at"]),
                          "expires_at": _iso(p["expires_at"]), "artifact_hash": h,
                          "risk": _obj(p.get("risk")).get("level"),
                          "confidence": p.get("confidence"), "files": [_basename(t) for t in p.get("targets") or []],
                          "evidence_refs": _refs(e.get("evidence_refs"), p.get("evidence_refs")),
                          "judgments": judgment_text((ev or {}).get("judge"), (ev or {}).get("jev")) if ev else None,
                          "actions": gated_actions(pid, h, blockers), "apply_blocked": blockers})
        for pid in rep["pending_decisions"]:
            if pid not in db.pending:
                gone.append({"proposal_id": pid, "status": db.proposals[pid]["status"] if pid in db.proposals else "not found"})
    else:
        for pid in rep["pending_decisions"]:
            e = tested.get(pid) or {}
            blockers, ev = _pending_gate(pid, e.get("artifact_hash"), e.get("verdict"), db, artifacts_root, suites)
            items.append({"proposal_id": pid, "title": "", "night": rep["night"], "this_night": True,
                          "status": "unknown (database rows not read)", "created_at": None, "expires_at": None,
                          "artifact_hash": e.get("artifact_hash"), "risk": None, "confidence": None, "files": [],
                          "evidence_refs": _refs(e.get("evidence_refs")),
                          "judgments": judgment_text((ev or {}).get("judge"), (ev or {}).get("jev")) if ev else None,
                          "actions": gated_actions(pid, e.get("artifact_hash"), blockers), "apply_blocked": blockers})
    return {"total": len(items), "items": items, "no_longer_pending": gone,
            "with_apply": sum(1 for i in items if i["actions"]["apply"])}


def make_brief(*, night: str | None = None, root: Path | None = None, db_url: str | None = None,
               now: datetime | None = None) -> dict[str, Any]:
    """The brief for one night. ReportError when the report is missing or invalid."""
    base = RA.sandbox_dir() if root is None else root
    artifacts_root = RA.artifacts_dir() if root is None else root / "artifacts"
    path, warnings = find_report(base, night)
    rep, sha = load_report(path)
    db = read_db(rep, db_url=db_url)
    return build_brief(rep, db, artifacts_root=artifacts_root, warnings=warnings, now=now,
                       report={"path": f"nights/{path.parent.name}/report.json", "sha256": sha},
                       verdict=coverage_verdict(rep, db_url=db_url))


# --------------------------------------------------------------------------- markdown


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head), *("| " + " | ".join(r) + " |" for r in rows)]


def _signed(v: Any) -> str:
    return ("0" if v == 0 else f"{v:+g}") if _num(v) else "-"


def _proposal_md(i: int, b: dict[str, Any]) -> list[str]:
    out = [f"### {i}. {b['title']}", ""]
    if not b["actions"].get("apply"):
        out.append("- **Status:** staged, NOT ready to apply (see Apply below). It is not an approved improvement.")
    for w in b["warnings"]:
        out.append(f"- **Warning:** {w}.")
    ch = b["change"]
    what = _sentence(b["what"]) or "(no description)"
    out.append(f"- **What would change:** {what}" + (f" Target: {b['target']}." if b["target"] else ""))
    if ch["verified"]:
        for f in ch["files"]:
            label = ", ".join(x for x in (f["kind"], f["rule_id"] and f"rule {f['rule_id']}") if x)
            out.append(f"  - {f['path']}" + (f" ({label})" if label else "") + f": +{f['added']} -{f['removed']} lines"
                       + (f". {f['note']}." if f["note"] else "."))
            if f["excerpt"]:
                out += ["", "    ```diff", *(f"    {ln}" for ln in f["excerpt"]), "    ```", ""]
    out.append(f"- **Why:** {b['why'] or '(no rationale recorded)'}")
    refs = ", ".join(f"`{r}`" for r in b["evidence_refs"]) or "none recorded"
    out.append(f"- **Supporting evidence:** {refs}")
    bvc = b["baseline_vs_candidate"]
    reasons = "; ".join(bvc["verdict_reasons"])
    out.append(f"- **Baseline vs candidate:** verdict {bvc['verdict']}" + (f" ({reasons})" if reasons else "") + ".")
    if bvc["metrics"]:
        out += ["", *("  " + ln for ln in _table(["metric", "baseline (A)", "candidate (B)", "change"],
                                                  [[m["name"], _fmt(m["baseline"]), _fmt(m["candidate"]), _signed(m["delta"])]
                                                   for m in bvc["metrics"]])), ""]
    evals = ", ".join(f"{x['suite']} {x['verdict']}" for x in bvc["evals"]) or "none"
    out.append(f"  - Evaluations ({bvc['evals_from']}): {evals}. Required: {', '.join(bvc['required_suites'])}.")
    conf = f"{b['confidence']:.2f}" if b["confidence"] is not None else "?"
    out.append(f"- **Risk and confidence:** risk {b['risk'] or '?'}, confidence {conf}.")
    state = "verified" if ch["verified"] else "DOES NOT VERIFY"
    if ch["matches_db"] is True:
        state += ", matches the database copy"
    out.append(f"- **Artifact hash:** `{b['artifact_hash']}` ({state}; {ch['dir']}).")
    ev = b.get("evaluation") or {}
    src = "the bound sandbox-replay record" if ev.get("recorded") else "the night report (no bound record)"
    if ev.get("provider_results"):
        for p in ev["provider_results"]:
            out.append(f"- **{p['provider']} provider judgments:** {judgment_text(p['judge'], p['jev'])} "
                       "(night report; providers are not pooled).")
        out.append("- Provider display does not replace a bound independent evaluation or authorize Apply.")
    else:
        out.append(f"- **Codex and Jev judgments:** {judgment_text(ev.get('judge'), ev.get('jev'))} (from {src}; "
                   + ("independent" if ev.get("independent") else "NOT independent") + ").")
    out.append(f"- **Proposal:** `{b['proposal_id']}`, {b['proposal_status'] or 'status unknown'}"
               + (f", expires {_when(b['expires_at'])} UTC" if b["expires_at"] else "") + ".")
    a = b["actions"]
    out += [_apply_md(a, b.get("apply_blocked") or []), f"- **Reject:** `{a['reject']}`", f"- **Details:** `{a['details']}`"]
    return out


def _apply_md(a: Mapping[str, Any], blockers: list[str], indent: str = "", bold: bool = True) -> str:
    label = "**Apply:**" if bold else "Apply:"
    if a.get("apply"):
        return f"{indent}- {label} `{a['apply']}`"
    return f"{indent}- {label} not offered: " + "; ".join(text(x, 160) for x in blockers[:4]) + "."


def usd4(v: Any) -> str:
    return f"${float(v):.4f}" if _num(v) else "$?"


def render_markdown(b: dict[str, Any]) -> str:
    out = [f"# RSI morning brief: night {b['night']}", "", f"{b['headline']} {b['summary']}"]
    ck = b["checkpoint"]
    sk = b.get("skipped")
    not_run = "not analyzed: the night did not run" if sk else ""
    if sk:
        run = (f"Skipped at {b['started_at'] or '?'} by the night job ({sk['reason']}). Checkpoint not read or "
               "advanced: the next night starts from the last advanced checkpoint.")
    else:
        run = f"Run {b['started_at'] or '?'} to {b['finished_at'] or '?'}. "
        run += (f"Checkpoint advanced: {ck['before'] or 'start'} -> {ck['after']}." if ck["advanced"]
                else f"Checkpoint not advanced (stays at {ck['before'] or 'start'}): the next night reads this window "
                     "again.")
    out.append(run)
    if b.get("verdict"):
        out.append(verdict_line(b["verdict"]))
    for n in b["notes"]:
        out.append(f"- Note: {n}")
    for w in b.get("prerequisite_warnings") or []:
        out.append(f"- Warning: {w}")
    for w in b["health"]["warnings"]:
        out.append(f"- Warning: {w}")
    review=b.get('telemetry_review')
    if isinstance(review,dict):
        out += ['', '## Deterministic daily telemetry', '',
                f"Status: {text(review.get('status'))}. {text(review.get('detail'))}"]
        if review.get('status')=='recorded':
            out += [f"Stored window: {review.get('stored_spans')} spans and {review.get('stored_test_runs')} test runs. "
                    "Captured rows are not all eligible source events. Cumulative work is not wall time.",
                    f"Report: {text(review.get('report_ref'))}."]
            for item in review.get('findings',[])[:5]:
                out += ['- '+text(item.get('summary'),240),
                        '  Evidence: '+', '.join(text(ref,140) for ref in item.get('evidence_refs',[])[:3]),
                        '  Limit: '+text(item.get('limitation'),300)]

    s = b["sessions"]
    out += ["", "## Sessions analyzed", ""]
    since = f" since {s['since']}" if s["since"] else ""
    if sk:
        out.append(f"- Sessions, deviations, eval failures and routine failures: {not_run}.")
    else:
        out.append(f"- {_plural(s['sessions'], 'new or changed session')}{since}." if s["sessions"]
                   else f"- No new sessions{since}.")
        out.append(f"- {_plural(s['deviations'], 'deviation')}, {_plural(s['eval_failures'], 'eval failure')}, "
                   f"{_plural(s['routine_failures'], 'routine failure')}.")

    out += ["", "## Important patterns", ""]
    pats = b["patterns"]
    for p in pats["items"]:
        size = ", ".join(x for x in (p["n"] is not None and f"n={p['n']}", p["sessions"] is not None and
                                     _plural(p["sessions"], "session")) if x)
        refs = ", ".join(f"`{r}`" for r in p["refs"])
        out.append(f"- **{p['key'] or 'pattern'}**" + (f" ({size})" if size else "") + f": {_sentence(p['summary']) or '-'}"
                   + (f" Evidence: {refs}." if refs else ""))
    if not pats["items"]:
        out.append(f"- {not_run[:1].upper()}{not_run[1:]}." if sk else "- None found.")
    elif pats["total"] > len(pats["items"]):
        out.append(f"- And {pats['total'] - len(pats['items'])} more (see the report).")
    w = b.get("watch") or {}
    if w.get("items"):
        out.append(f"- Watch (last {w['hours']}h, check by hand): " + str(len(w["items"])) + " deviation(s).")
        label = {"first-step": "first step skipped", "guard-bypass": "possible guard bypass"}
        for it in w["items"]:
            out.append(f"  - {label.get(it['kind'], it['kind'])}: {it['session'] or '?'} · {it['source']} "
                       f"{it['rule']}: {_sentence(it['what']) or '-'}")

    out += ["", "## Candidates considered", ""]
    cd = b["candidates"]
    backlog = f" Backlog: {_plural(cd['backlog'], 'candidate')} kept for future nights." if cd["backlog"] is not None else ""
    out.append(f"None considered: the night did not run.{backlog}" if sk
               else f"{cd['total']} considered, {cd['selected']} selected for an experiment.{backlog}")
    if cd["items"]:
        out += ["", *_table(["rank", "candidate", "source", "selected", "status", "reason"],
                            [[_fmt(x["rank"]), x["key"], x["source"], "yes" if x["selected"] else "no",
                              text(x["status"], 20) or "-", x["reason"] or "-"] for x in cd["items"]])]
        if cd["total"] > len(cd["items"]):
            out.append(f"\nAnd {cd['total'] - len(cd['items'])} more (see the report).")

    out += ["", "## Experiments performed", ""]
    ex = b["experiments"]["items"]
    if ex:
        out += _table(["experiment", "candidate", "baseline (A) pass", "candidate (B) pass", "verdict", "status", "cost"],
                      [[x["experiment_id"] or "(not started)", x["candidate_key"] or "-", _fmt(x["baseline_pass"]),
                        _fmt(x["candidate_pass"]), x["verdict"] or "-", x["status"],
                        usd(x["cost_usd"]) if x["cost_usd"] is not None else "-"] for x in ex])
        out.append("")
        for x in ex:
            if x["experiment_id"] and x.get("provider_results"):
                for p in x["provider_results"]:
                    rate_a = f"{100 * p['baseline_pass']:.0f}%" if _num(p["baseline_pass"]) else "unknown"
                    rate_b = f"{100 * p['candidate_pass']:.0f}%" if _num(p["candidate_pass"]) else "unknown"
                    out.append(f"- {x['experiment_id']}, {p['provider']}: baseline pass {rate_a} "
                               f"({_fmt(p['baseline_checked'])} checked), candidate pass {rate_b} "
                               f"({_fmt(p['candidate_checked'])} checked); {p['verdict'] or 'verdict unknown'}; "
                               f"{judgment_text(p['judge'], p['jev'])}. Providers are not pooled.")
            elif x["experiment_id"] and (x["judge"] or x["jev"]):
                out.append(f"- {x['experiment_id']}: {judgment_text(x['judge'], x['jev'])}.")
            elif x["experiment_id"] and x["verdict"]:
                out.append(f"- {x['experiment_id']}: judge and Jev results not in the report.")
    else:
        out.append("- None ran: the night did not run." if sk else "- None ran.")

    out += ["", "## Improvements that passed", ""]
    for i, blk in enumerate(b["passed"], 1):
        out += ([""] if i > 1 else []) + _proposal_md(i, blk)
    if not b["passed"]:
        out.append("- None this night.")

    out += ["", "## Failed or inconclusive experiments", ""]
    for f in b["failed"]:
        reasons = "; ".join(f["reasons"])
        out.append(f"- {f['experiment_id'] or '(not started)'}" + (f" ({f['candidate_key']})" if f["candidate_key"] else "")
                   + f": {f['verdict'] or 'no verdict'}, {f['status'].replace('_', ' ')}. {f['why_not_staged'][:1].upper()}"
                   + f"{f['why_not_staged'][1:]}."
                   + (f" Reasons: {reasons}." if reasons else "") + (f" Hypothesis: {_sentence(f['what'])}" if f["what"] else "")
                   + (f" Judgments: {f['judgments']}." if f.get("judgments") else ""))
    if not b["failed"]:
        out.append("- None.")

    out += ["", "## Model and provider costs", ""]
    co = b["costs"]
    if co["by_model"]:
        out += _table(["model", "calls", "tokens in", "tokens out", "USD", "cost basis"],
                      [[m["model"], _fmt(m["calls"]), _fmt(m["tokens_in"]), _fmt(m["tokens_out"]), usd(m["usd"]),
                        ", ".join(f"{k} {usd4(v)}" for k, v in m["bases"].items()) or "not recorded"]
                       for m in co["by_model"]]) + [""]
    else:
        out.append("- No model calls.")
    prov = ", ".join(f"{p['name']} {_plural(p['requests'], 'request')} ({usd(p['usd'])})" for p in co["providers"])
    out.append(f"- Sandbox runs: {co['sandbox_runs']}. Providers: {prov or 'none'}.")
    out.append(f"- Total: {usd(co['total_usd'])}." + (" The budget ran out: the night stopped at this total."
                                                       if co["budget_exhausted"] else ""))
    if co["reported_usd"] is not None or co["estimated_usd"] is not None:
        out.append(f"- Provider-reported: {usd4(co['reported_usd'])} (the provider's CLI reported it; not reconciled "
                   f"with an invoice). Local estimates: {usd4(co['estimated_usd'])} (config/rsi-prices.json or a "
                   "per-run cap). No cost here is verified provider billing.")
        if co["by_basis"]:
            out.append("- By basis: " + ", ".join(f"{x['basis']} ({x['kind']}) {_plural(x['records'], 'record')} "
                                                  f"{usd4(x['usd'])}" for x in co["by_basis"]) + ".")
    elif sk:
        out.append("- Cost basis: none needed: the night did not run, so it made no model or provider call.")
    else:
        out.append("- Cost basis: not recorded in this report (reported vs estimated unknown).")

    out += ["", "## Service health", ""]
    h = b["health"]
    if sk:
        out.append("- Services: not checked: the night did not run.")
    else:
        for k, v in h["services"].items():
            why = "; ".join(h.get("reasons", {}).get(k) or [])
            out.append(f"- {k}: {v}" + (" (DEGRADED)" if k in h["degraded"] else "") + (f": {why}" if why else ""))
        if not h["services"]:
            out.append("- The report lists no services.")
    db = h["database"]
    out.append(f"- Database rows for this brief: {db['status']}" + (f" ({db['detail']})" if db["detail"] else "") + ".")

    out += ["", "## Pending human decisions", ""]
    pd = b["pending_decisions"]
    for it in pd["items"]:
        bits = [f"night {it['night']}"] + (["this night"] if it["this_night"] else [])
        if it["risk"]:
            bits.append(f"risk {text(it['risk'], 10)}")
        if _num(it["confidence"]):
            bits.append(f"confidence {it['confidence']:.2f}")
        if it["expires_at"]:
            bits.append(f"expires {_when(it['expires_at'])} UTC")
        files = f" Files: {', '.join(it['files'])}." if it["files"] else ""
        out.append(f"- **{it['title'] or it['proposal_id']}** ({', '.join(bits)}), {it['status']}.{files}")
        out.append(f"  - Artifact: `{it['artifact_hash']}`" if it["artifact_hash"] else "  - Artifact: unknown")
        refs = ", ".join(f"`{r}`" for r in it.get("evidence_refs") or [])
        out.append(f"  - Evidence: {refs or 'none recorded'}")
        if it.get("judgments"):
            out.append(f"  - Judgments: {it['judgments']}")
        a = it["actions"]
        out += [_apply_md(a, it.get("apply_blocked") or [], indent="  ", bold=False), f"  - Reject: `{a['reject']}`",
                f"  - Details: `{a['details']}`"]
    if not pd["items"]:
        out.append("- Nothing waits for you.")
    else:
        out.append(f"- Apply offered for {pd['with_apply']} of {_plural(pd['total'], 'proposal')}: only fully "
                   "evaluated IMPROVES proposals (independent judge and live Jev, verified artifact) get one.")
    for g in pd["no_longer_pending"]:
        out.append(f"- No longer pending: `{g['proposal_id']}` is now {g['status']}.")
    out += ["", "Apply and Reject run in your own terminal (they refuse in agent sessions and ask for the first 8 "
                "characters of the hash). Phone buttons: `amem rsi push`.", ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- phone (off in Mission 005)


def push_allowed(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(PUSH_ENV) or "").strip() == "1"


def _word(v: str) -> str:
    m = re.match(r"[a-z][a-z_-]{0,19}", v.lower())
    return m.group(0) if m else "degraded"


def notification(b: Mapping[str, Any]) -> tuple[str, str]:
    """(title, body) for the phone: counts, enums, short hashes, file names and ids only, then scrubbed."""
    pd = b["pending_decisions"]
    s = b["sessions"]
    ready = sum(1 for x in b["passed"] if x["actions"].get("apply"))
    sk = b.get("skipped")
    lines = [f"Night {b['status']}. Ready to apply {ready}, staged not ready {len(b['passed']) - ready}, "
             f"failed or inconclusive {sum(x.get('status') != 'deferred_policy' for x in b['failed'])}.",
             f"Not run: skipped by the night job ({sk['reason']}); nothing analyzed. Cost {usd(b['costs']['total_usd'])}."
             if sk and sk.get("reason") in SKIP_REASONS else
             f"Sessions {int(s['sessions'])}, candidates {int(b['candidates']['total'])}, "
             f"experiments {int(b['experiments']['total'])}. Cost {usd(b['costs']['total_usd'])}."]
    v = b.get("verdict") or {}
    if v.get("conclusion") in VERDICT_HEADLINES:
        n = len(v.get("gaps") or [])
        lines.append(f"Verdict: {VERDICT_HEADLINES[v['conclusion']]}"
                     + (f" ({int(v.get('findings') or 0)})" if v["conclusion"] == "findings" else "")
                     + (f", {_plural(n, 'coverage gap')}" if n else "") + ".")
    deg = [f"{k} {_word(b['health']['services'][k])}" for k in b["health"]["degraded"] if NAME_RE.fullmatch(k)]
    other = len(b["health"]["degraded"]) - len(deg)
    if deg or other:
        lines.append("Degraded: " + ", ".join(deg + ([f"{other} other"] if other else [])) + ".")
    if any(str(w).startswith(WRITER_NEVER_RAN) for w in b["health"]["warnings"]):
        lines.append("Writer never ran: every experiment used the stub change.")
    if b["health"]["database"]["status"] != "ok":
        lines.append("Database rows not read.")
    for it in pd["items"][:PUSH_ITEMS]:
        files = ", ".join(f for f in it["files"] if FILE_RE.fullmatch(f)) or "proposal"
        h = it["artifact_hash"][:12] if it["artifact_hash"] and RA.HEX64.fullmatch(it["artifact_hash"]) else "?"
        pid = it["proposal_id"] if UUID_RE.fullmatch(str(it["proposal_id"])) else "?"
        lines.append(f"- {files} ({h}): amem rsi show {pid}" + ("" if it["actions"].get("apply") else " (no Apply)"))
    if len(pd["items"]) > PUSH_ITEMS:
        lines.append(f"- and {len(pd['items']) - PUSH_ITEMS} more")
    if pd["items"]:
        lines.append("Apply / Reject: amem rsi decide <id> apply|reject in your terminal, or the buttons from amem rsi push.")
    night = b["night"] if NIGHT_RE.fullmatch(str(b["night"])) else "?"
    return scrub(f"RSI brief {night}: {len(pd['items'])} to decide")[:120], scrub("\n".join(lines))[:3500]


def push_brief(b: Mapping[str, Any], *, notify: dict[str, str] | None = None,
               environ: Mapping[str, str] | None = None) -> str:
    """One summary push ('phone' | 'mac'), through the existing ntfy publisher. PushRefused (nothing sent)
    unless AMEM_RSI_BRIEF_PUSH=1."""
    if not push_allowed(environ):
        raise PushRefused(PUSH_OFF)
    title, body = notification(b)
    return edits_push._publish(lambda topic: {"topic": topic, "title": title, "message": body, "priority": 3,
                                              "tags": ["sunrise"]}, title, body, notify)
