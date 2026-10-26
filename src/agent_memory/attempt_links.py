"""Experiment-attempt identity links (plan Revision 2, R1; docs/data-use/relationships.md).

One canonical identity per sandbox experiment attempt (arm A or B of one case and repetition):

    (experiment_id, profile, arm, case, rep)
    key                 refs.run_key(experiment_id, arm, case_key, rep)
                        case_key = "<profile>.<case>" for a provider-profile replay (claude / codex), else the case
    attempt_session_id  refs.session_uuid(key)

The runner carries it from the start: rsi_replay's RunRecord.session_id, the `.rsi.json` sidecar (and so the
Langfuse trace's amem_session_id) and the night's `rsi_attempts` row (source runner) all use the same key.
Before this, replay derived the session id without the profile, so a two-provider replay gave the claude and the
codex attempt of the same case the same id (`session_id_collision` below).

Historical attempts: `amem coverage repair-links` rebuilds rows only from explicit identifiers:
  replay record   <root>/replays/<exp>[/<profile>]/runs/<run_id>.json   (arm, case, rep; the dir names exp/profile)
  sandbox log     <root>/experiments/<exp>/logs/<run_id>.json           (experiment, arm) + its `.rsi.json` sidecar
                  (case, rep); a log without a sidecar names no case or rep
  workspace       <root>/experiments/<exp>/runs/<run_id>/               (only a run id)
  Langfuse trace  rsi_evidence (system langfuse, kind trace) metrics    (experiment_id, arm, case, rep, run_id)
  session         sessions.session_id equal to an attempt_session_id
Sources of one run id are merged by that exact id. A run whose sources name no full identity, or disagree, gets an
`unresolved` row with the reason. Start times are never compared: two records close in time stay apart.

Not attempts (excluded, counted): judge runs (arm J), check runs (arm K, `-chk`), implementer runs (arm I) and
eval-harness sessions (env eval, no experiment id; agent-config's ~/.cache/agent-evals clones).

Repair is dry-run by default. `--apply` writes in one transaction and first saves an undo manifest (each row's
previous and new value); `undo(manifest)` restores exactly that and refuses when a row changed since. Applying the
same plan twice changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb

from agent_memory.integrations import refs

ATTEMPT_ARMS = ("A", "B")
NOT_ATTEMPT_ARMS = {"J": "judge run", "K": "check run", "I": "implementer run"}
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
CASE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
EVAL_HARNESS_SQL = r"cache[-/](agent|skill)-evals"          # project_dir of an eval-harness clone (sessions_ingest)
EVAL_HARNESS_RE = re.compile(EVAL_HARNESS_SQL)
MANIFEST_FORMAT = "amem-repair-links/1"
COLS = ("id", "status", "experiment_id", "profile", "arm", "case_id", "rep", "attempt_session_id", "run_id",
        "session_id", "source", "evidence", "reason")
ALL_COLS = (*COLS, "created_at", "updated_at")
DETAIL_CAP = 50


class RepairError(Exception):
    """A refused repair or undo (bad manifest, changed rows, hosted target without confirmation)."""


# --------------------------------------------------------------------------- the identity


def case_key(case: str, profile: str | None) -> str:
    return f"{profile}.{case}" if profile else case


@dataclass(frozen=True)
class Attempt:
    experiment_id: str
    arm: str
    case: str
    rep: int
    profile: str | None = None

    def __post_init__(self) -> None:
        if self.arm not in ATTEMPT_ARMS:
            raise refs.RefError(f"arm {self.arm!r} is not an attempt arm")
        if self.profile is not None and not PROFILE_RE.fullmatch(self.profile):
            raise refs.RefError(f"bad profile {self.profile!r}")
        if not CASE_RE.fullmatch(self.case):
            raise refs.RefError(f"bad case {self.case!r}")
        self.key  # noqa: B018 - validates experiment id and rep

    @property
    def key(self) -> str:
        return refs.run_key(self.experiment_id, self.arm, case_key(self.case, self.profile), self.rep)

    @property
    def session_id(self) -> str:
        return refs.session_uuid(self.key)


def session_id_for(experiment_id: str, arm: str, case: str, rep: int, profile: str | None = None) -> str | None:
    """The canonical attempt session uuid; None when the parts are not a valid identity."""
    try:
        return refs.session_uuid(refs.run_key(experiment_id, arm, case_key(case, profile), rep))
    except refs.RefError:
        return None


def native_session_id(provider: Any, raw: Any) -> str | None:
    """A trusted client's explicit actor identity, distinct from our attempt UUID.
    Never accept a model-written id or derive one from nearby timestamps.
    """
    if provider not in ("claude", "codex") or not isinstance(raw, str):
        return None
    value = raw.removeprefix("codex:") if provider == "codex" else raw
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        return None
    return "codex:" + value if provider == "codex" else value


def runner_identity(experiment_id: str, run: Any, profile: str | None) -> dict[str, Any]:
    """What the runner hands the night for one agent run (an rsi_replay RunRecord)."""
    rel = Path("replays") / experiment_id / (profile or "") / "runs" / f"{run.run_id}.json"
    return {"experiment_id": experiment_id, "profile": profile or None, "arm": run.arm, "case": run.case,
            "rep": run.rep, "run_id": run.run_id, "replay_record": rel.as_posix(), "error": bool(run.error),
            "native_session_id": native_session_id(getattr(run, "agent", None), getattr(run, "native_session_id", None)),
            "agent": getattr(run, "agent", None), "model": getattr(run, "model", None),
            "started_at": getattr(run, "started_at", None), "duration_s": getattr(run, "duration_s", None)}


def record_native_session(conn: psycopg.Connection, attempt: Attempt, a: Mapping[str, Any]) -> str | None:
    """Ingest a real native-client session observation, not an invented transcript.
    An absent client id never falls back to the logical attempt uuid. Sandbox
    observations have zero transcript bytes and explicitly lack transcript and
    acceptance evidence, so they cannot enter production grading or summaries.
    """
    provider = a.get("agent")
    native = native_session_id(provider, a.get("native_session_id"))
    if native is None:
        return None
    from agent_memory.execution_timing import number, timestamp
    started = timestamp(a.get("started_at"))
    duration = number(a.get("duration_s"))
    ended = started + timedelta(seconds=duration) if started is not None and duration is not None else None
    metadata = {"source": "rsi-native-session/1", "rsi_native_observation": True,
                "env": "sandbox", "experiment_id": attempt.experiment_id,
                "attempt_ref": attempt.key, "native_identity_source": "trusted_client_response",
                "transcript_available": False, "acceptance": "unverified",
                "execution_result": "failed" if a.get('error') else "completed",
                "model_authority": "requested_configuration" if a.get('error') else "verified_client_response"}
    conn.execute("""insert into sessions(session_id,tool,kind,project_dir,env,experiment_id,
                    started_at,ended_at,duration_seconds,model,meta)
                    values(%s,%s,'headless','rsi-native-session','sandbox',%s,%s,%s,%s,%s,%s)
                    on conflict(session_id) do nothing""",
                 (native, provider, attempt.experiment_id, started, ended, duration,
                  a.get("model") if isinstance(a.get("model"), str) else None, Jsonb(metadata)))
    got = conn.execute("select env,experiment_id,meta from sessions where session_id=%s", (native,)).fetchone()
    if got and got[0] == "sandbox" and got[1] == attempt.experiment_id:
        previous_attempt = got[2].get("attempt_ref") if isinstance(got[2], dict) else None
        if previous_attempt in (None, attempt.key):
            return native
    return None  # A real actor collision stays visible; never relabel a prod session.


def record_runner(conn: psycopg.Connection, attempts: Iterable[Mapping[str, Any]], *, source: str = "runner") -> int:
    """Write the runner's attempts (source runner), idempotently. Returns the rows inserted or changed."""
    if source not in ("runner", "repair"):
        raise ValueError("invalid attempt evidence source")
    n = 0
    for a in attempts:
        at = Attempt(str(a["experiment_id"]), str(a["arm"]), str(a["case"]), int(a["rep"]), a.get("profile") or None)
        ev = {"replay_record": a.get("replay_record"), **({"run_error": True} if a.get("error") else {}),
              **({"provider": a['agent']} if a.get('agent') in ('claude','codex') else {})}
        native = native_session_id(a.get("agent"), a.get("native_session_id"))
        sid = None
        if native:
            previous = conn.execute("select evidence->>'native_session_id' from rsi_attempts where id=%s", (at.key,)).fetchone()
            if previous and previous[0] not in (None, native):
                raise refs.RefError("conflicting native identity for the same attempt")
            ev["native_session_id"] = native
            ev["native_identity_source"] = "trusted_client_response"
            sid = record_native_session(conn, at, a)
            if sid is None:
                ev["native_identity_unavailable"] = "existing_session_scope_or_attempt_conflict"
        else:
            ev["native_identity_unavailable"] = "client_id_not_recorded"
        row = {"id": at.key, "status": "linked", "experiment_id": at.experiment_id, "profile": at.profile,
               "arm": at.arm, "case_id": at.case, "rep": at.rep, "attempt_session_id": at.session_id,
               "run_id": a.get("run_id"), "session_id": sid, "source": source,
               "evidence": {k: v for k, v in ev.items() if v is not None}, "reason": None}
        cur = conn.execute(
            f"insert into rsi_attempts ({', '.join(COLS)}) values ({', '.join(['%s'] * len(COLS))}) "
            "on conflict (id) do update set status = excluded.status, experiment_id = excluded.experiment_id, "
            "profile = excluded.profile, arm = excluded.arm, case_id = excluded.case_id, rep = excluded.rep, "
            "attempt_session_id = excluded.attempt_session_id, run_id = excluded.run_id, "
            "session_id = coalesce(rsi_attempts.session_id, excluded.session_id), "
            "source = case when rsi_attempts.source='runner' then 'runner' else excluded.source end, "
            "evidence = rsi_attempts.evidence || excluded.evidence, reason = null, updated_at = now() "
            "where (rsi_attempts.status, rsi_attempts.run_id, rsi_attempts.attempt_session_id) "
            "is distinct from (excluded.status, excluded.run_id, excluded.attempt_session_id) "
            "or (rsi_attempts.source<>'runner' and excluded.source='runner') "
            "or (rsi_attempts.session_id is null and excluded.session_id is not null) "
            "or not (rsi_attempts.evidence @> excluded.evidence)",
            _params(row))
        n += cur.rowcount
    return n


def reconcile_native_sessions(conn: psycopg.Connection) -> int:
    """Late arrival reconciliation using explicit client identity only.
    This never creates sessions, reassigns an existing link, or matches by time.
    """
    cur = conn.execute("""
        update rsi_attempts a set session_id=s.session_id, updated_at=now()
        from sessions s where a.status='linked' and a.session_id is null
          and a.evidence->>'native_identity_source'='trusted_client_response'
          and a.evidence->>'native_session_id'=s.session_id
          and s.env='sandbox' and s.experiment_id=a.experiment_id
        """)
    return cur.rowcount


# --------------------------------------------------------------------------- observations


@dataclass
class Obs:
    kind: str                      # replay_record | sandbox_log | workspace | langfuse_trace
    locator: str                   # path relative to the sandbox root, or a ref
    experiment_id: str | None
    run_id: str | None
    attempt: Attempt | None = None
    excluded: str | None = None    # not an attempt: why
    problem: str | None = None     # attempt-like, but this source names no full identity: why
    arm: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _run_id_for(prefix: str, case: str, arm: str, rep: int) -> str:
    from agent_memory.rsi_replay import run_id_for
    return run_id_for(prefix, case, arm, rep)


def _attempt(exp: str | None, arm: Any, case: Any, rep: Any, profile: str | None) -> Attempt | str:
    if not isinstance(case, str) or not isinstance(rep, int) or isinstance(rep, bool) or not isinstance(exp, str):
        return "experiment, case or rep missing"
    try:
        return Attempt(exp, str(arm), case, rep, profile)
    except refs.RefError as exc:
        return f"not a valid attempt identity ({str(exc)[:80]})"


def _profile_from_run_id(exp: str, run_id: str, case: str, arm: str, rep: int) -> tuple[bool, str | None]:
    """(matches, profile): the run id is the runner's own id for these parts, with or without a profile."""
    if _run_id_for(exp, case, arm, rep) == run_id:
        return True, None
    tail = f"-{case}-{arm}-r{rep}"
    if run_id.startswith(exp + "-") and run_id.endswith(tail):
        p = run_id[len(exp) + 1:-len(tail)]
        if PROFILE_RE.fullmatch(p) and _run_id_for(f"{exp}-{p}", case, arm, rep) == run_id:
            return True, p
    return False, None


def replay_records(root: Path) -> list[Obs]:
    out: list[Obs] = []
    base = root / "replays"
    if not base.is_dir():
        return out
    for exp_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        exp = exp_dir.name
        dirs = [(None, exp_dir)] + [(p.name, p) for p in sorted(exp_dir.iterdir())
                                    if p.is_dir() and p.name not in ("runs", "judge") and (p / "runs").is_dir()]
        for profile, d in dirs:
            meta = _read_json(d / "replay.json")
            named = meta.get("experiment_id") if isinstance(meta, dict) else None
            for f in sorted((d / "runs").glob("*.json")) if (d / "runs").is_dir() else []:
                rel = f.relative_to(root).as_posix()
                rec = _read_json(f)
                if not isinstance(rec, dict) or rec.get("run_id") != f.stem:
                    out.append(Obs("replay_record", rel, exp, f.stem, problem="unreadable run record"))
                    continue
                arm = rec.get("arm")
                o = Obs("replay_record", rel, exp, f.stem, arm=arm if isinstance(arm, str) else None,
                        extra={"recorded_session_id": rec.get("session_id")})
                if arm in NOT_ATTEMPT_ARMS:
                    o.excluded = NOT_ATTEMPT_ARMS[arm]
                elif named is not None and named != exp:
                    o.problem = f"replay.json names experiment {str(named)[:60]}, its directory {exp}"
                else:
                    got = _attempt(exp, arm, rec.get("case"), rec.get("rep"), profile)
                    if isinstance(got, str):
                        o.problem = f"run record: {got}"
                    elif _run_id_for(exp + (f"-{profile}" if profile else ""), got.case, got.arm, got.rep) != f.stem:
                        o.problem = "run record: its run id does not match its case, arm and rep"
                    else:
                        o.attempt = got
                out.append(o)
    return out


def sandbox_logs(root: Path) -> list[Obs]:
    out: list[Obs] = []
    for f in sorted((root / "experiments").glob("*/logs/*.json")) if (root / "experiments").is_dir() else []:
        if f.name.endswith(".rsi.json"):
            continue
        exp, rel = f.parent.parent.name, f.relative_to(root).as_posix()
        meta = _read_json(f)
        if not isinstance(meta, dict) or meta.get("run_id") != f.stem:
            out.append(Obs("sandbox_log", rel, exp, f.stem, problem="unreadable sandbox run log"))
            continue
        run_id, arm = f.stem, meta.get("arm")
        o = Obs("sandbox_log", rel, exp, run_id, arm=arm if isinstance(arm, str) else None)
        named = meta.get("experiment")
        if run_id.endswith("-chk") or arm == "K":
            o.excluded, o.extra["attempt_run_id"] = NOT_ATTEMPT_ARMS["K"], run_id.removesuffix("-chk")
        elif arm in NOT_ATTEMPT_ARMS:
            o.excluded = NOT_ATTEMPT_ARMS[arm]
        elif arm not in ATTEMPT_ARMS:
            o.problem = f"run log: unknown arm {str(arm)[:8]!r}"
        elif named is not None and named != exp:
            o.problem = f"run log names experiment {str(named)[:60]}, its directory {exp}"
        else:
            side_path = f.with_name(f"{run_id}.rsi.json")
            side = _read_json(side_path)
            if not isinstance(side, dict):
                o.problem = "run log without a sidecar: case and rep are not explicit"
            else:
                o.extra["sidecar"] = side_path.relative_to(root).as_posix()
                o.problem = _from_sidecar(o, exp, run_id, str(arm), side)
        out.append(o)
    return out


def _from_sidecar(o: Obs, exp: str, run_id: str, arm: str, side: Mapping[str, Any]) -> str | None:
    """Fill o.attempt from a sidecar; return a problem when the sidecar names no consistent identity."""
    if side.get("experiment_id") not in (None, exp):
        return f"sidecar names experiment {str(side.get('experiment_id'))[:60]}, its directory {exp}"
    if side.get("arm") not in (None, arm):
        return "sidecar and run log name different arms"
    raw, rep, profile = side.get("case"), side.get("rep"), side.get("profile")
    if not isinstance(raw, str) or not isinstance(rep, int) or isinstance(rep, bool):
        return "sidecar without case or rep"
    case = raw
    if isinstance(profile, str) and raw.startswith(profile + "."):
        case = raw[len(profile) + 1:]
    elif profile is None and "." in raw:
        profile, case = raw.split(".", 1)
    if profile is None:
        ok, profile = _profile_from_run_id(exp, run_id, case, arm, rep) if CASE_RE.fullmatch(case) else (False, None)
        if not ok:
            return "sidecar case and rep do not match the run id"
    got = _attempt(exp, arm, case, rep, profile if isinstance(profile, str) else None)
    if isinstance(got, str):
        return f"sidecar: {got}"
    o.attempt = got
    return None


def workspaces(root: Path) -> list[Obs]:
    out: list[Obs] = []
    for d in sorted((root / "experiments").glob("*/runs/*")) if (root / "experiments").is_dir() else []:
        if not d.is_dir():
            continue
        exp, rel = d.parent.parent.name, d.relative_to(root).as_posix()
        o = Obs("workspace", rel, exp, d.name)
        if d.name.endswith("-chk"):
            o.excluded, o.extra["attempt_run_id"] = NOT_ATTEMPT_ARMS["K"], d.name.removesuffix("-chk")
        else:
            o.problem = "workspace only: no record names arm, case and rep"
        out.append(o)
    return out


def langfuse_traces(conn: psycopg.Connection) -> list[Obs]:
    out: list[Obs] = []
    rows = conn.execute("select external_ref, metrics from rsi_evidence where system = 'langfuse' and kind = 'trace' "
                        "order by external_ref").fetchall()
    for ref, m in rows:
        m = m if isinstance(m, dict) else {}
        exp, run_id, arm = m.get("experiment_id"), m.get("run_id"), m.get("arm")
        o = Obs("langfuse_trace", str(ref), exp if isinstance(exp, str) else None,
                run_id if isinstance(run_id, str) else None, arm=arm if isinstance(arm, str) else None,
                extra={"recorded_session_id": m.get("amem_session_id")})
        raw, rep = m.get("case"), m.get("rep")
        if arm in NOT_ATTEMPT_ARMS:
            o.excluded = NOT_ATTEMPT_ARMS[arm]
        elif not (isinstance(raw, str) and o.experiment_id and o.run_id):
            o.problem = "trace metadata lacks experiment, run id, case or rep"
        elif raw == o.run_id:
            o.problem = "trace exported without a sidecar: its case and rep are the sink's defaults"
            o.extra["langfuse_default_case"] = True
        else:
            profile, case = raw.split(".", 1) if "." in raw else (None, raw)
            got = _attempt(o.experiment_id, arm, case, rep, profile)
            if isinstance(got, str):
                o.problem = f"trace: {got}"
            elif m.get("amem_session_id") not in (None, got.session_id):
                o.problem = "trace session id does not match its run key"
            else:
                o.attempt = got
        out.append(o)
    return out


# --------------------------------------------------------------------------- the inventory


@dataclass
class Found:
    attempt: Attempt
    run_ids: list[str]
    evidence: dict[str, Any]
    sources: set[str]
    session_id: str | None = None


@dataclass
class Unresolved:
    id: str
    reason: str
    experiment_id: str | None = None
    arm: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Inventory:
    root: Path
    attempts: dict[str, Found] = field(default_factory=dict)           # key -> identified attempt
    unresolved: list[Unresolved] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)             # reason -> distinct runs
    sessions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)   # old-eligible class -> rows
    experiments: list[str] = field(default_factory=list)
    db_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    divergences: dict[str, int] = field(default_factory=dict)


_EV_KEY = {"replay_record": "replay_record", "sandbox_log": "sandbox_log", "workspace": "workspace",
           "langfuse_trace": "langfuse"}


def _db_rows(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    cur = conn.execute(f"select {', '.join(ALL_COLS)} from rsi_attempts order by id")
    return {r[0]: dict(zip(ALL_COLS, r)) for r in cur.fetchall()}


def inventory(conn: psycopg.Connection, root: Path) -> Inventory:
    """Everything known about attempts, merged by exact run id. Reads only (files and the database)."""
    inv = Inventory(root=root)
    obs = [*replay_records(root), *sandbox_logs(root), *workspaces(root), *langfuse_traces(conn)]
    groups: dict[tuple[str, str], list[Obs]] = {}
    for o in obs:
        if o.run_id is None:
            inv.unresolved.append(Unresolved(f"unresolved:{o.kind}:{o.locator}"[:300], o.problem or "no run id",
                                             o.experiment_id, o.arm, None, None, {_EV_KEY[o.kind]: o.locator}))
            continue
        groups.setdefault((o.experiment_id or "", o.run_id), []).append(o)
    checks: list[tuple[str, str]] = []
    for (exp, run_id), group in sorted(groups.items()):
        excluded = sorted({o.excluded for o in group if o.excluded})
        if excluded:
            reason = next((r for r in excluded if r in NOT_ATTEMPT_ARMS.values()), excluded[0])
            inv.excluded[reason] = inv.excluded.get(reason, 0) + 1
            if reason == NOT_ATTEMPT_ARMS["K"]:
                checks.append((exp, run_id.removesuffix("-chk")))
            continue
        evidence: dict[str, Any] = {}
        for o in group:
            evidence.setdefault(_EV_KEY[o.kind], o.locator)
            if "sidecar" in o.extra:
                evidence["sidecar"] = o.extra["sidecar"]
            if o.extra.get("langfuse_default_case"):
                evidence["langfuse_default_case"] = True
        keys = {o.attempt.key: o.attempt for o in group if o.attempt is not None}
        if len(keys) == 1:
            (at,) = keys.values()
            recorded = {o.extra.get("recorded_session_id") for o in group} - {None, at.session_id}
            if recorded:
                evidence["recorded_session_id"] = sorted(recorded)[0]
            f = inv.attempts.get(at.key)
            if f is None:
                inv.attempts[at.key] = Found(at, [run_id], evidence, {o.kind for o in group})
            else:                                   # a re-run of the same attempt under another run id
                f.run_ids.append(run_id)
                f.sources |= {o.kind for o in group}
                f.evidence.setdefault("other_run_ids", []).append(run_id)
            continue
        arm = next((o.arm for o in group if o.arm), None)
        if len(keys) > 1:
            reason = "sources name different identities: " + "; ".join(sorted(keys))[:400]
        else:
            reason = "; ".join(sorted({o.problem for o in group if o.problem}))[:480] or "no explicit identity"
        inv.unresolved.append(Unresolved(f"unresolved:run:{exp}/{run_id}"[:300], reason, exp or None, arm, run_id,
                                         None, evidence))
    # A check log whose attempt is nowhere else is still evidence of an attempt we cannot name.
    known = {(f.attempt.experiment_id, r) for f in inv.attempts.values() for r in f.run_ids}
    known |= {(u.experiment_id or "", u.run_id) for u in inv.unresolved if u.run_id}
    for exp, rid in sorted(set(checks) - known):
        inv.unresolved.append(Unresolved(f"unresolved:run:{exp}/{rid}"[:300],
                                         "only a check run exists: no record names case and rep", exp or None, None,
                                         rid, None, {"check_log": f"experiments/{exp}/logs/{rid}-chk.json"}))
    inv.db_rows = _db_rows(conn)
    for row in inv.db_rows.values():             # attempts written by the runner (or an earlier repair)
        if row["status"] != "linked":
            continue
        if row["id"] in inv.attempts:
            inv.attempts[row["id"]].sources.add("supabase")
            continue
        try:
            at = Attempt(row["experiment_id"], row["arm"], row["case_id"], row["rep"], row["profile"])
        except refs.RefError:
            continue
        if at.key == row["id"]:
            inv.attempts[at.key] = Found(at, [row["run_id"]] if row["run_id"] else [], dict(row["evidence"] or {}),
                                         {"supabase"}, row["session_id"])
    _sessions(conn, inv)
    inv.experiments = [r[0] for r in conn.execute("select id from rsi_experiments order by id").fetchall()]
    inv.divergences = {
        "session_id_collision": sum(1 for f in inv.attempts.values() if "recorded_session_id" in f.evidence),
        "langfuse_default_case": sum(1 for f in inv.attempts.values() if f.evidence.get("langfuse_default_case"))
        + sum(1 for u in inv.unresolved if u.evidence.get("langfuse_default_case")),
        "experiment_row_missing": len({f.attempt.experiment_id for f in inv.attempts.values()} - set(inv.experiments)),
    }
    return inv


def _sessions(conn: psycopg.Connection, inv: Inventory) -> None:
    """The old eligible set (sessions with env sandbox or eval, or an experiment id), one class per row."""
    by_sid = {f.attempt.session_id: f for f in inv.attempts.values()}
    rows = conn.execute("select session_id, env, experiment_id, project_dir from sessions "
                        "where env in ('sandbox', 'eval') or experiment_id is not null order by session_id").fetchall()
    for sid, env, exp, project_dir in rows:
        item = {"session_id": sid, "env": env, "experiment_id": exp}
        f = by_sid.get(sid)
        if f is not None:
            f.session_id = sid
            f.sources.add("session")
            cls = "sandbox_attempt"
        elif env == "eval" and exp is None:
            if EVAL_HARNESS_RE.search(project_dir or ""):
                cls, item["reason"] = "eval_harness_session", "eval-harness session: an eval case run, not an attempt"
            else:
                cls, item["reason"] = "ambiguous", "env eval outside the eval-harness paths, no experiment id"
        else:
            cls = "unresolved"
            item["reason"] = (f"session names experiment {exp} but no attempt (arm, case, rep); its id is no attempt "
                              f"session id" if exp else "sandbox session without experiment id or attempt identifier")
            inv.unresolved.append(Unresolved(f"unresolved:session:{sid}"[:300], item["reason"], exp, None, None, sid,
                                             {"session": sid}))
        inv.sessions.setdefault(cls, []).append(item)


# --------------------------------------------------------------------------- classify (read-only)


def _read_only(conn: psycopg.Connection) -> Any:
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("attempt_links needs an idle connection (it opens its own transaction)")
    return conn.transaction(force_rollback=True)


def classify(conn: psycopg.Connection, root: Path) -> dict[str, Any]:
    """Counts per class, from files under `root` and the database, in one READ ONLY transaction."""
    with _read_only(conn):
        conn.execute("set transaction read only")
        inv = inventory(conn, root)
    found = list(inv.attempts.values())
    with_attempts = {f.attempt.experiment_id for f in found}
    old = {k: len(v) for k, v in sorted(inv.sessions.items())}
    linked_in_db = sum(1 for k in inv.attempts if inv.db_rows.get(k, {}).get("status") == "linked")
    per_exp: dict[str, int] = {}
    for f in found:
        per_exp[f.attempt.experiment_id] = per_exp.get(f.attempt.experiment_id, 0) + 1
    return {
        "sandbox_root": str(root), "sandbox_root_found": root.is_dir(),
        "old_eligible": {"total": sum(old.values()), **old},
        "classes": {
            "eval_harness_session": old.get("eval_harness_session", 0),
            "sandbox_attempt": len(found),
            "experiment_without_attempts": len(set(inv.experiments) - with_attempts),
            "ambiguous": old.get("ambiguous", 0),
            "unresolved": len(inv.unresolved),
        },
        "sandbox_attempt_sources": {
            "on_disk": sum(1 for f in found if f.sources & {"replay_record", "sandbox_log", "workspace"}),
            "langfuse": sum(1 for f in found if "langfuse_trace" in f.sources),
            "supabase": sum(1 for f in found if f.sources & {"supabase", "session"}),
            "linked_in_rsi_attempts": linked_in_db,
            "not_in_rsi_attempts": len(found) - linked_in_db,
        },
        "excluded_runs": dict(sorted(inv.excluded.items())),
        "divergences": inv.divergences,
        "attempts_per_experiment": dict(sorted(per_exp.items())),
        "experiments_without_attempts": sorted(set(inv.experiments) - with_attempts)[:DETAIL_CAP],
        "unresolved": [{"id": u.id, "reason": u.reason} for u in inv.unresolved][:DETAIL_CAP],
        "ambiguous": inv.sessions.get("ambiguous", [])[:DETAIL_CAP],
    }


# --------------------------------------------------------------------------- repair


def desired_rows(inv: Inventory) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, f in sorted(inv.attempts.items()):
        if not f.evidence and f.session_id is None:
            continue                          # known only from rsi_attempts itself: nothing to write
        at = f.attempt
        ev = {**f.evidence, **({"session": f.session_id} if f.session_id else {})}
        out[key] = {"id": key, "status": "linked", "experiment_id": at.experiment_id, "profile": at.profile,
                    "arm": at.arm, "case_id": at.case, "rep": at.rep, "attempt_session_id": at.session_id,
                    "run_id": sorted(f.run_ids)[0] if f.run_ids else None, "session_id": f.session_id,
                    "source": "repair", "evidence": ev, "reason": None}
    for u in inv.unresolved:
        out[u.id] = {"id": u.id, "status": "unresolved", "experiment_id": u.experiment_id, "profile": None,
                     "arm": u.arm if u.arm and re.fullmatch(r"[A-Z]", u.arm) else None, "case_id": None, "rep": None,
                     "attempt_session_id": None, "run_id": u.run_id, "session_id": u.session_id, "source": "repair",
                     "evidence": u.evidence, "reason": u.reason}
    return out


@dataclass
class Plan:
    inserts: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)          # stale unresolved repair rows, now resolved
    unchanged: int = 0
    kept_runner: int = 0
    linked: int = 0
    unresolved: int = 0

    def summary(self) -> dict[str, Any]:
        return {"insert": len(self.inserts), "update": len(self.updates), "delete": len(self.deletes),
                "unchanged": self.unchanged, "kept_runner": self.kept_runner, "linked": self.linked,
                "unresolved": self.unresolved}


def _same(row: Mapping[str, Any], want: Mapping[str, Any]) -> bool:
    return all(row.get(c) == want.get(c) for c in COLS)


def plan(inv: Inventory) -> Plan:
    p = Plan()
    want = desired_rows(inv)
    for key, w in want.items():
        p.linked += w["status"] == "linked"
        p.unresolved += w["status"] == "unresolved"
        have = inv.db_rows.get(key)
        if have is None:
            p.inserts.append(w)
        elif have["source"] == "runner":
            p.kept_runner += 1               # written when the attempt ran: repair never rewrites it
        elif _same(have, w):
            p.unchanged += 1
        else:
            p.updates.append(w)
    linked_runs = {(r["experiment_id"], r["run_id"]) for r in (*want.values(), *inv.db_rows.values())
                   if r["status"] == "linked" and r["run_id"]}
    linked_sessions = {r["session_id"] for r in (*want.values(), *inv.db_rows.values())
                       if r["status"] == "linked" and r["session_id"]}
    for key, have in inv.db_rows.items():
        if key in want or have["source"] != "repair" or have["status"] != "unresolved":
            continue
        if (have["experiment_id"], have["run_id"]) in linked_runs or have["session_id"] in linked_sessions:
            p.deletes.append(key)
    return p


def _params(row: Mapping[str, Any]) -> list[Any]:
    return [Jsonb(row[c]) if c == "evidence" else row[c] for c in COLS]


def _json_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: (v.astimezone(UTC).isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}


def _fetch(conn: psycopg.Connection, key: str) -> dict[str, Any] | None:
    r = conn.execute(f"select {', '.join(ALL_COLS)} from rsi_attempts where id = %s", (key,)).fetchone()
    return dict(zip(ALL_COLS, r)) if r else None


def db_label(url: str) -> str:
    """host:port/dbname, no user or password (for the manifest and the hosted check)."""
    u = urlparse(url)
    return f"{u.hostname or 'local'}:{u.port or 5432}{u.path or ''}"


def is_local(url: str) -> bool:
    host = urlparse(url).hostname
    return host in (None, "", "localhost", "127.0.0.1", "::1") or (host or "").startswith("/")


def default_manifest_dir() -> Path:
    return Path(os.environ.get("AMEM_REPAIR_LINKS_DIR") or Path.home() / ".local" / "state" / "amem" / "repair-links")


def repair(conn: psycopg.Connection, root: Path, *, apply: bool = False, manifest_dir: Path | None = None,
           db: str = "") -> dict[str, Any]:
    """Dry run (default): the plan, nothing written. apply: one transaction, the undo manifest first."""
    if not apply:
        with _read_only(conn):
            conn.execute("set transaction read only")
            p = plan(inventory(conn, root))
        return {"mode": "dry-run", "planned": p.summary(), "manifest": None,
                "changes": [r["id"] for r in (*p.inserts, *p.updates)][:DETAIL_CAP] + p.deletes[:DETAIL_CAP]}
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("repair needs an idle connection")
    path: Path | None = None
    with conn.transaction():
        conn.execute("lock table rsi_attempts in share row exclusive mode")
        p = plan(inventory(conn, root))
        changes: list[dict[str, Any]] = []
        for w in p.inserts:
            cur = conn.execute(f"insert into rsi_attempts ({', '.join(COLS)}) values ({', '.join(['%s'] * len(COLS))}) "
                               f"returning {', '.join(ALL_COLS)}", _params(w))
            changes.append({"id": w["id"], "op": "insert", "before": None,
                            "after": _json_row(dict(zip(ALL_COLS, cur.fetchone())))})
        for w in p.updates:
            before = _fetch(conn, w["id"])
            cur = conn.execute(f"update rsi_attempts set ({', '.join(COLS[1:])}, updated_at) = "
                               f"({', '.join(['%s'] * (len(COLS) - 1))}, now()) where id = %s "
                               f"returning {', '.join(ALL_COLS)}", [*_params(w)[1:], w["id"]])
            changes.append({"id": w["id"], "op": "update", "before": _json_row(before),
                            "after": _json_row(dict(zip(ALL_COLS, cur.fetchone())))})
        for key in p.deletes:
            before = _fetch(conn, key)
            conn.execute("delete from rsi_attempts where id = %s", (key,))
            changes.append({"id": key, "op": "delete", "before": _json_row(before), "after": None})
        if changes:
            path = _write_manifest(manifest_dir or default_manifest_dir(), db, root, changes)
    return {"mode": "apply", "planned": p.summary(), "manifest": str(path) if path else None,
            "changes": [c["id"] for c in changes][:DETAIL_CAP]}


def _write_manifest(directory: Path, db: str, root: Path, changes: Sequence[Mapping[str, Any]]) -> Path:
    """Written and fsynced before the transaction commits; removed again if writing fails."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = {"format": MANIFEST_FORMAT, "table": "rsi_attempts", "db": db, "sandbox_root": str(root),
            "created_at": datetime.now(UTC).isoformat(), "changes": list(changes)}
    text = json.dumps(body, indent=2, sort_keys=True, default=str)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"repair-links-{stamp}-{hashlib.sha256(text.encode()).hexdigest()[:8]}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def undo(conn: psycopg.Connection, manifest: Path, *, db: str = "") -> dict[str, Any]:
    """Restore every row of the manifest to its `before` value, exactly. Refuses (nothing changes) when any row is
    no longer what the repair left; reports `already undone` when every row already equals `before`."""
    body = _read_json(manifest)
    if not isinstance(body, dict) or body.get("format") != MANIFEST_FORMAT or body.get("table") != "rsi_attempts":
        raise RepairError(f"not a repair-links manifest: {manifest.name}")
    if db and body.get("db") and body["db"] != db:
        raise RepairError(f"the manifest was written for {body['db']}, not {db}")
    changes = body.get("changes") or []
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("undo needs an idle connection")
    with conn.transaction():
        conn.execute("lock table rsi_attempts in share row exclusive mode")
        current = {c["id"]: _json_row(_fetch(conn, c["id"])) for c in changes}
        if changes and all(current[c["id"]] == c["before"] for c in changes):
            return {"mode": "undo", "restored": 0, "status": "already undone"}
        moved = [c["id"] for c in changes if current[c["id"]] != c["after"]]
        if moved:
            raise RepairError(f"{len(moved)} row(s) changed since the repair (first: {moved[0]}); nothing restored")
        for c in reversed(changes):
            b = c["before"]
            if b is None:
                conn.execute("delete from rsi_attempts where id = %s", (c["id"],))
                continue
            vals = [Jsonb(b[k]) if k == "evidence" else b[k] for k in ALL_COLS]
            casts = ["%s::timestamptz" if k in ("created_at", "updated_at") else "%s" for k in ALL_COLS]
            if c["after"] is None:
                conn.execute(f"insert into rsi_attempts ({', '.join(ALL_COLS)}) values ({', '.join(casts)})", vals)
            else:
                conn.execute(f"update rsi_attempts set ({', '.join(ALL_COLS[1:])}) = ({', '.join(casts[1:])}) "
                             "where id = %s", [*vals[1:], c["id"]])
    return {"mode": "undo", "restored": len(changes), "status": "restored"}
