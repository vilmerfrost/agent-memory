"""A synthetic RSI dataset (Mission 004C): one coherent, made-up story for the ClickHouse C1 verification and
for the shared-intelligence demo of `amem evidence ask`.

    amem synth seed --db-url URL [--anchor YYYY-MM-DD] [--out DIR] [--json]

Nothing here is real. Every row is synthetic and says so: `meta.synthetic` where a table has a meta column,
`SYNTHETIC` in its text, `synth` in its ids and labels (sessions `synth-claude-01`, `codex:synth-01`, models
`synth-claude-opus`, routines `synth-nightly-deviations`, rule ids `synth-...` or skill `synth-debugging`).

The story, ending the day before the anchor date (UTC):
- 14 production sessions (6 Claude and 3 Codex in the owner's own repos, 2 team-flagged work sessions in
  `pipeline-poc`, and 3 of unknown ownership in `client-notes`: no remote, a non-GitHub remote ingestion read no
  owner from, and an owner that is not an owner account) and 2 sandbox sessions (the two runs of experiment
  exp-20261017-c2synth, arms A and B, case a-split, rep 0).
- Ownership (Mission 004C, F8; egress.py) is stored as sessions_ingest stores it: own sessions carry
  `team: false` and `remote_owner` = the default owner account, team sessions `team: true`, private ones no team
  flag or an unknown owner. Private rows ship `private` to ClickHouse (project, repo, a repo-scoped rule source
  `claude-md:private` with no rule id, a repo-local skill label as null) and never reach past.dev (an edit on a
  private deviation, a lesson scoped to the private repo). The unmatched skill call is private too (F7).
- Rule R1 `skill:synth-debugging:reproduce-before-fix` is failed again and again: 10 deviations in 7 prod
  sessions, 4 Claude and 3 Codex. R2 (`claude-md:global:synth-quote-globs`) is broken without harm; R3 (Codex
  only) is rare.
- An earlier fix for R1 was already tried twice: an improve edit (reverted), then candidate -> proposal ->
  experiment exp-20261017-c2synth -> eval results (no-effect on Claude, worse on Codex) -> a reject decision.
  The experiment's two sandbox runs are linked in rsi_evidence to the two synthetic Langfuse traces that C2
  created (trace id = refs.trace_id(refs.run_key(...))): arm A passed, arm B failed with tool errors.
- Runs (a nightly routine on a fail streak, an ingest, a lessons harvest), scores, skill calls, two lessons and
  three lesson decisions, a team proposal and edit (never sent to past.dev), one NCM research run.

Leak markers. Every text and identifying column (titles, summaries, prompts, paths, emails, person, repo and
branch names, a fake `sk-ant-` key, jsonb) holds a unique marker `ZZLEAK-<nnnn>`. Label and id columns (which
ClickHouse receives by design) never hold one. Each marker has a policy:
    strict          must never leave Supabase (ClickHouse, Langfuse, past.dev)
    decision_text   a non-team, non-private decision record's text: past.dev receives it after sanitizing, by
                    design; ClickHouse and Langfuse never do
gold.json lists every marker with its table, column, row key, policy and team and private flags.

Determinism. Ids are uuid5 over fixed names; times are offsets from the anchor (default: today, UTC); markers
are numbered in build order. The same anchor always gives the same rows and the same gold.json.

Safety. `seed` refuses any database whose host is not localhost, 127.0.0.1 or ::1 (never the hosted DB),
refuses a database that holds rows in the tables it fills, and on a database it already seeded only re-writes
gold.json (the sentinel is rsi_checkpoints job `synth:seed`).

gold.json: per-table row counts, per-day counts per ClickHouse table (`amem sync verify`), the expected
results of the ClickHouse named queries (as ClickHouseRetriever returns them, with the default owner accounts),
the expected Supabase answers of the story, the past.dev records that may be sent, the team and private rows,
the ownership counts (now, and under the pre-F8 rule), the Langfuse traces and the markers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from agent_memory.integrations import clickhouse as _ch, egress, refs

DATASET = "amem-synth"
VERSION = 1
NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/vilmerfrost/agent-memory/synth/v1")
MARKER_PREFIX = "ZZLEAK-"
STRICT, DECISION = "strict", "decision_text"
SENTINEL = ("synth:seed", f"{DATASET}/v{VERSION}")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

EXP_C2 = "exp-20261017-c2synth"          # the C2 experiment whose two synthetic traces live in Langfuse
C2_CASE, C2_REP = "a-split", 0
EXP_NCM = "exp-20261017-synth-ncm"
NCM_RUN = "ncm-synth-sudoku-easy"
BASELINE = "synth-baseline-1"
R1 = ("skill:synth-debugging", "reproduce-before-fix")
R2 = ("claude-md:global", "synth-quote-globs")
R3 = ("agents-md:global", "synth-no-compound-cmds")
R_PRIVATE = ("claude-md:@repo", "synth-client-deploy-gate")    # `@repo`: the session's own repo (repo-scoped)
NIGHTLY, INGEST, HARVEST = "synth-nightly-deviations", "synth-skills-ingest", "synth-lessons-harvest"
# Mission 004C (F8): ownership as sessions_ingest stores it, judged with the default owner accounts.
OWNER = egress.DEFAULT_OWNER_ACCOUNTS[0]
OWN, TEAM = "own", "team"
PRIVATE_CASES = ("no-remote", "non-github-remote", "unknown-owner")

# The tables seed fills, in insert order (foreign keys first).
TABLES = ("sessions", "deviations", "runs", "scores", "skill_calls", "lessons", "lesson_candidates",
          "rsi_candidates", "rsi_artifacts", "rsi_experiments", "rsi_proposals", "rsi_decisions",
          "rsi_eval_results", "edits", "rsi_evidence")
KEY = {"sessions": "session_id", "rsi_artifacts": "hash", "lesson_candidates": "run_id"}
JSONB = {"sessions": {"skills", "instructions", "meta", "models", "efforts", "outcome_evidence"},
         "deviations": {"jev_answers", "meta"}, "runs": {"meta"}, "scores": {"probabilities"},
         "rsi_candidates": {"metrics", "jev", "meta"}, "rsi_artifacts": {"manifest", "files"},
         "rsi_experiments": {"hypothesis", "meta"}, "rsi_proposals": {"rationale", "risk", "expected", "meta"},
         "rsi_eval_results": {"record"}, "edits": {"meta"}, "rsi_evidence": {"metrics"}}

# ClickHouse tables: the Supabase source rows and the day column `amem sync verify` counts per UTC day.
CH_DAY = {"sessions": ("sessions", "created_at"), "runs": ("runs", "created_at"),
          "deviations": ("deviations", "created_at"), "scores": ("scores", "created_at"),
          "skill_calls": ("skill_calls", "created_at"), "rsi_proposals": ("rsi_proposals", "created_at"),
          "rsi_decisions": ("rsi_decisions", "decided_at"), "research_runs": ("rsi_evidence", "observed_at"),
          "rsi_eval_results": ("rsi_eval_results", "created_at")}


class UnsafeTarget(ValueError):
    """The database is not a local, disposable one."""


class SeedError(RuntimeError):
    pass


# ---------------------------------------------------------------- the target guard


def db_host(db_url: str) -> str:
    """The host part only (never the password)."""
    try:
        info = conninfo_to_dict(db_url)
    except psycopg.Error:
        return "invalid"
    return str(info.get("host") or info.get("hostaddr") or "none")


def check_local(db_url: str) -> str:
    """Raise UnsafeTarget unless every host (and hostaddr) of the URL is loopback. Returns the host."""
    try:
        info = conninfo_to_dict(db_url)
    except psycopg.Error as exc:
        raise UnsafeTarget("not a database URL") from exc
    hosts = [h.strip().strip("[]").lower() for h in str(info.get("host") or "").split(",") if h.strip()]
    addrs = [h.strip().lower() for h in str(info.get("hostaddr") or "").split(",") if h.strip()]
    if not hosts and not addrs:
        raise UnsafeTarget("no host in the URL: give localhost or 127.0.0.1 explicitly")
    bad = [h for h in hosts + addrs if h not in LOCAL_HOSTS]
    if bad:
        raise UnsafeTarget(f"refusing a non-local database host ({bad[0][:60]}): synthetic data goes to a "
                           "disposable local Postgres only")
    return (hosts or addrs)[0]


# ---------------------------------------------------------------- the dataset


@dataclass
class Dataset:
    anchor: date
    rows: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    markers: list[dict[str, Any]] = field(default_factory=list)
    team: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    private: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))   # F8: unknown ownership
    ids: dict[str, Any] = field(default_factory=dict)            # named ids of the story

    def counts(self) -> dict[str, int]:
        return {t: len(self.rows.get(t, [])) for t in TABLES}


def uid(name: str) -> uuid.UUID:
    return uuid.uuid5(NS, name)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def rule_key(rule: tuple[str, str]) -> str:
    return f"{rule[0]}:{rule[1]}"


def trace_ids() -> dict[str, dict[str, str]]:
    """The two C2 runs: run_key, Langfuse trace id and sandbox session id per arm."""
    out = {}
    for arm in ("A", "B"):
        key = refs.run_key(EXP_C2, arm, C2_CASE, C2_REP)
        out[arm] = {"run_key": key, "trace_id": refs.trace_id(key), "session_id": refs.session_uuid(key)}
    return out


# sid, tool, category, model, effort, outcome, ownership (own | team | a private case), days before anchor,
# hour (UTC), topic
_SESSIONS = (
    ("synth-claude-01", "claude", "chat", "synth-claude-opus", "high", "shipped", OWN, 20, 9,
     "fix a flaky import test"),
    ("synth-claude-02", "claude", "worker", "synth-claude-opus", "high", "failed", OWN, 18, 14,
     "patch the retry loop"),
    ("synth-claude-03", "claude", "chat", "synth-claude-sonnet", "medium", "pushed", OWN, 15, 10,
     "rename the config loader"),
    ("synth-claude-04", "claude", "worker", "synth-claude-opus", "high", "partial", OWN, 11, 16,
     "debug the date parser"),
    ("synth-claude-05", "claude", "parent", "synth-claude-sonnet", "medium", "committed", OWN, 6, 11,
     "split the sync job"),
    ("synth-claude-06", "claude", "chat", "synth-claude-opus", "high", "shipped", OWN, 3, 13,
     "tidy the CLI help"),
    ("codex:synth-01", "codex", "chat", "synth-gpt-codex", "high", "failed", OWN, 17, 15,
     "fix the cache key bug"),
    ("codex:synth-02", "codex", "worker", "synth-gpt-codex", "high", "shipped", OWN, 12, 9,
     "add a parser test"),
    ("codex:synth-03", "codex", "chat", "synth-gpt-codex", "medium", "abandoned", OWN, 5, 17,
     "chase a timeout"),
    ("synth-team-01", "claude", "worker", "synth-claude-opus", "high", "shipped", TEAM, 14, 10,
     "pipeline retry guard"),
    ("codex:synth-team-02", "codex", "worker", "synth-gpt-codex", "high", "failed", TEAM, 8, 15,
     "pipeline schema drift"),
    ("synth-private-01", "claude", "chat", "synth-claude-opus", "high", "shipped", "no-remote", 13, 11,
     "draft the client export notes"),
    ("codex:synth-private-02", "codex", "worker", "synth-gpt-codex", "medium", "partial", "non-github-remote", 9, 14,
     "patch the vendor sync script"),
    ("synth-private-03", "claude", "worker", "synth-claude-sonnet", "medium", "committed", "unknown-owner", 4, 10,
     "rename the report fields"),
)
# session, rule, followed, result, objective (None: not measured), minutes after the nightly scan started
_DEVIATIONS = (
    ("synth-claude-01", R1, 0.25, 0.5, 0.5, 20), ("synth-claude-01", R1, 0.125, 0.25, 0.5, 45),
    ("synth-claude-02", R1, 0.25, 0.25, 0.0, 30),
    ("codex:synth-01", R1, 0.125, 0.25, 0.0, 15), ("codex:synth-01", R1, 0.25, 0.5, 0.5, 50),
    ("synth-claude-04", R1, 0.375, 0.5, 0.5, 25), ("synth-claude-04", R1, 0.25, 0.5, None, 55),
    ("synth-team-01", R1, 0.25, 0.5, 0.5, 35),
    ("codex:synth-03", R1, 0.125, 0.25, 0.0, 20),
    ("codex:synth-team-02", R1, 0.25, 0.25, 0.5, 40),
    ("synth-claude-03", R2, 0.5, 0.75, 1.0, 15),
    ("synth-claude-05", R2, 0.25, 0.75, 1.0, 20), ("synth-claude-05", R2, 0.375, 0.75, None, 40),
    ("synth-claude-06", R2, 0.25, 0.75, 1.0, 30),
    ("codex:synth-02", R3, 0.5, 0.75, 1.0, 25), ("codex:synth-03", R3, 0.25, 0.5, None, 45),
    ("sandbox:B", R1, 0.5, 0.5, 0.5, 10),
    ("synth-private-01", R_PRIVATE, 0.25, 0.5, 0.5, 30),
)
_JEV = {R1: "edit_instruction", R2: "leave", R3: "enforce", R_PRIVATE: "edit_instruction"}
# routine, source, [(days before anchor, status)], hour, base duration (s), duration step (s)
_RUNS = (
    (NIGHTLY, "launchd", ((8, "pass"), (7, "pass"), (6, "pass"), (5, "fail"), (4, "error"), (3, "skip"),
                          (2, "fail"), (1, "fail")), 3, 40, 5),
    (INGEST, "launchd", ((4, "pass"), (3, "pass"), (2, "pass"), (1, "pass")), 4, 12, 1),
    (HARVEST, "scheduled-task", ((16, "pass"), (12, "pass"), (9, "pass")), 5, 90, 0),
)
# skill, session (None: a call no session matched), minutes after the session started
_SKILL_CALLS = (
    ("synth-debugging", "synth-claude-01", 10), ("synth-debugging", "synth-claude-01", 40),
    ("synth-debugging", "synth-claude-02", 10), ("synth-debugging", "synth-claude-04", 10),
    ("synth-debugging", "synth-team-01", 10), ("synth-debugging", "codex:synth-01", 10),
    ("synth-debugging", "codex:synth-03", 10), ("synth-debugging", "codex:synth-team-02", 10),
    ("synth-dispatch", "synth-claude-03", 5), ("synth-dispatch", "synth-claude-05", 5),
    ("synth-dispatch", "synth-claude-06", 5),
    ("synth-handoff", "codex:synth-02", 50), ("synth-handoff", "synth-claude-06", 55),
    ("synth-handoff", None, 0),                                         # no session matched: private (F7)
    ("synth-debugging", "synth-private-01", 10),                        # a known skill: its label ships
    ("synth-client-release", "codex:synth-private-02", 20),             # a repo-local skill: label null
)


class _Builder:
    def __init__(self, anchor: date):
        self.ds = Dataset(anchor)
        self.midnight = datetime(anchor.year, anchor.month, anchor.day, tzinfo=UTC)

    def ts(self, days: float, hour: float = 0, minute: float = 0) -> datetime:
        return self.midnight - timedelta(days=days) + timedelta(hours=hour, minutes=minute)

    def mk(self, table: str, column: str, key: Any, policy: str = STRICT, team: bool = False,
           private: bool = False) -> str:
        m = f"{MARKER_PREFIX}{len(self.ds.markers) + 1:04d}"
        self.ds.markers.append({"marker": m, "table": table, "column": column, "key": str(key), "policy": policy,
                                "team": team, "private": private})
        return m

    def add(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        self.ds.rows[table].append(row)
        return row

    # ------------------------------------------------------------ sessions and deviations

    def sessions(self) -> None:
        start_of: dict[str, datetime] = {}
        for i, (sid, tool, cat, model, effort, outcome, owner, d, h, topic) in enumerate(_SESSIONS):
            start_of[sid] = self._session(i, sid, tool, cat, model, effort, outcome, owner, self.ts(d, h), topic,
                                          env="prod")
        for j, (arm, t) in enumerate(sorted(trace_ids().items())):
            sid = t["session_id"]
            start_of[f"sandbox:{arm}"] = self._session(len(_SESSIONS) + j, sid, "claude", "eval", "synth-claude-sonnet",
                                                       "low", None, OWN, self.ts(10, 12, 30 * j),
                                                       f"sandbox run {arm} of {EXP_C2}", env="sandbox", kind="headless",
                                                       experiment=EXP_C2)
            self.ds.ids[f"sandbox_session_{arm}"] = sid
        self.ds.ids["session_start"] = start_of

    def _session(self, i: int, sid: str, tool: str, cat: str, model: str, effort: str, outcome: str | None, owner: str,
                 start: datetime, topic: str, *, env: str, kind: str | None = None,
                 experiment: str | None = None) -> datetime:
        team, private = owner == TEAM, owner in PRIVATE_CASES

        def mk(col: str) -> str:
            return self.mk("sessions", col, sid, STRICT, team, private)

        active = 900.0 + 150 * i
        end = start + timedelta(seconds=active + 600)
        name = "pipeline-poc" if team else "client-notes" if private else "own-app"
        proj = f"{name}-{mk('project')}"
        repo = f"acme-pipeline-poc-{mk('repo')}" if team else f"{name}-{mk('repo')}"
        # ownership as sessions_ingest.team_info stores it (Mission 004C, F8)
        ownership: dict[str, Any]
        if owner == OWN:
            ownership = {"team": False, "remote_owner": OWNER}
        elif team:
            ownership = {"team": True}
        else:       # no remote and a remote it read no owner from both store nothing; an unknown owner, team false
            ownership = {"synthetic_case": owner}
            if owner == "unknown-owner":
                ownership.update(team=False, remote_owner=f"synth-other-{mk('meta.remote_owner')}")
        meta: dict[str, Any] = {"synthetic": True, "dataset": DATASET, **ownership,
                                "branch": f"feature/{mk('meta.branch')}",
                                "author_email": f"sam.synth.{mk('meta.author_email')}@example.com",
                                "person": f"Sam Synth {mk('meta.person')}",
                                "first_prompt": f"SYNTHETIC prompt: please {topic} ({mk('meta.first_prompt')})"}
        if sid == "synth-claude-02":
            meta["api_key"] = f"sk-ant-api03-{mk('meta.api_key')}-SYNTHETICKEY000000"
        turns = 12 + i
        row = {"session_id": sid, "kind": kind or cat, "tool": tool, "repo": repo,
               "project_dir": f"/Users/synth-dev/work/{mk('project_dir')}/{name}",
               "started_at": start, "ended_at": end, "last_activity_at": end, "transcript_bytes": 40_000 + 1000 * i,
               "skills": [s for s, sess, _ in _SKILL_CALLS if sess == sid] or [],
               "instructions": [f"/Users/synth-dev/.claude/CLAUDE.md#{mk('instructions')}"],
               "meta": meta, "scanned_at": end + timedelta(minutes=2), "created_at": start,
               "tokens_input": 20_000 + 1500 * i, "tokens_output": 3000 + 250 * i, "tokens_cache_read": 60_000 + 4000 * i,
               "tokens_cache_creation": 2000 + 100 * i, "models": {model: {"calls": turns}}, "turns": turns,
               "tokens_updated_at": end + timedelta(minutes=1),
               "title": f"SYNTHETIC: {topic} {mk('title')}", "title_updated_at": end,
               "category": cat, "project": proj, "model": model, "model_last": model, "effort": effort,
               "effort_last": effort, "efforts": {effort: turns}, "duration_seconds": active + 600,
               "active_seconds": active, "api_seconds": active / 2, "tool_seconds": active / 4,
               "lines_added": 40 + 5 * i, "lines_removed": 8 + i, "outcome": outcome,
               "outcome_evidence": ({"commits": [{"message": f"SYNTHETIC commit: {topic} {mk('outcome_evidence')}"}]}
                                    if outcome in ("shipped", "pushed", "committed") else {}),
               "outcome_at": end if outcome else None,
               "summary": f"SYNTHETIC summary: {topic}; reviewed by Sam Synth {mk('summary')}",
               "summary_updated_at": end, "env": env, "experiment_id": experiment}
        self.add("sessions", row)
        if team:
            self.ds.team["sessions"].append(sid)
        if private:
            self.ds.private["sessions"].append(sid)
        return start

    def ownership(self, sid: str | None) -> str:
        """own | team | private of a session id (None or unknown: private), by egress.classify."""
        s = next((r for r in self.ds.rows["sessions"] if r["session_id"] == sid), None) if sid else None
        return egress.classify(s["meta"], (OWNER,)) if s else egress.PRIVATE

    def deviations(self) -> None:
        sessions = {r["session_id"]: r for r in self.ds.rows["sessions"]}
        start_of = self.ds.ids["session_start"]
        per_session: dict[str, int] = defaultdict(int)
        for sess, rule, followed, result, objective, minute in _DEVIATIONS:
            sid = self.ds.ids["sandbox_session_B"] if sess == "sandbox:B" else sess
            k = per_session[sid]
            per_session[sid] += 1
            did = uid(f"deviation:{sid}:{k}")
            s = sessions[sid]
            team = bool(s["meta"].get("team"))
            private = self.ownership(sid) == egress.PRIVATE

            def mk(col: str, did: uuid.UUID = did, team: bool = team, private: bool = private) -> str:
                return self.mk("deviations", col, did, STRICT, team, private)

            why = f"SYNTHETIC: it trusted the stack trace; flagged by Sam Synth {mk('why')}"
            if sid == "synth-claude-02":
                why += f"; it pasted token sk-ant-api03-{mk('why.api_key')}-SYNTHETICKEY000000 into the log"
            if sid == "codex:synth-01" and k == 0:
                why += f"; ask sam.synth.{mk('why.email')}@example.com"
            src, rid = rule
            src = src.replace("@repo", s["repo"])               # a repo-scoped source names the session's repo
            self.add("deviations", {
                "id": did, "source_key": f"synth-dev-{mk('source_key')}", "session_id": sid, "instruction_source": src,
                "instruction_ref": f"~/.agents/skills/synth-debugging/SKILL.md#L{12 + k} {mk('instruction_ref')}",
                "turn": 3 + k, "what": f"SYNTHETIC: changed code before reproducing the failure {mk('what')}",
                "why": why, "how": f"SYNTHETIC: ran the tests only after the edit ({mk('how')})",
                "followed_grade": followed, "result_grade": result, "jev_verdict": _JEV[rule], "jev_confidence": 0.75,
                "jev_mode": "fixture", "jev_answers": {"q1": f"SYNTHETIC answer {mk('jev_answers')}"},
                "writer_model": "synth-writer", "created_at": start_of[sess] + timedelta(hours=2, minutes=minute),
                "rule_id": rid, "rule_title": f"SYNTHETIC rule title {mk('rule_title')}",
                "instruction_sha": _sha(f"synth:{src}"), "meta": {"synthetic": True, "team": team, "note": mk("meta")},
                "result_outcome": s["outcome"], "objective_result": objective})
            if team:
                self.ds.team["deviations"].append(str(did))
            if private:
                self.ds.private["deviations"].append(str(did))

    # ------------------------------------------------------------ runs, scores, skill calls, lessons

    def runs(self) -> None:
        for routine, source, plan, hour, base, step in _RUNS:
            for i, (d, status) in enumerate(plan):
                rid = uid(f"run:{routine}:{d}")
                start = self.ts(d, hour, 7)
                end = start + timedelta(seconds=base + step * i)

                def mk(col: str, rid: uuid.UUID = rid) -> str:
                    return self.mk("runs", col, rid)

                failure = None
                if status in ("fail", "error"):
                    failure = f"SYNTHETIC: step 3 timed out, see /Users/synth-dev/logs/{mk('failure_reason')}.log"
                    if d == 2:
                        failure += f" (env had sk-ant-api03-{mk('failure_reason.api_key')}-SYNTHETICKEY000000)"
                self.add("runs", {"id": rid, "routine": routine, "source": source, "started_at": start,
                                  "finished_at": end, "status": status,
                                  "summary": f"SYNTHETIC {routine}: {status} {mk('summary')}", "failure_reason": failure,
                                  "report_path": f"/Users/synth-dev/reports/{mk('report_path')}.md",
                                  "meta": {"synthetic": True, "host": f"synth-mac-{mk('meta')}"}, "created_at": end,
                                  "source_key": f"synth-run-{mk('source_key')}"})
                self.ds.ids[f"run:{routine}:{d}"] = rid

    def scores(self) -> None:
        for r in [r for r in self.ds.rows["runs"] if r["routine"] == NIGHTLY]:
            sid = uid(f"score:{r['id']}")
            good = r["status"] == "pass"
            self.add("scores", {
                "id": sid, "run_id": r["id"], "question": "synth-healthy", "kind": "choice",
                "value": f"SYNTHETIC {'yes' if good else 'no'} {self.mk('scores', 'value', sid)}",
                "value_num": 1.0 if good else 0.0, "confidence": 0.875,
                "probabilities": {f"SYNTHETIC {self.mk('scores', 'probabilities', sid)}": 0.875}, "jev_mode": "fixture",
                "created_at": r["finished_at"] + timedelta(minutes=1)})

    def skill_calls(self) -> None:
        sessions = {r["session_id"]: r for r in self.ds.rows["sessions"]}
        for i, (skill, sess, minute) in enumerate(_SKILL_CALLS):
            cid = uid(f"skill_call:{i}")
            s = sessions.get(sess) if sess else None
            team = bool(s and s["meta"].get("team"))
            private = self.ownership(sess) == egress.PRIVATE           # no matched session: private too (F7)

            def mk(col: str, cid: uuid.UUID = cid, team: bool = team, private: bool = private) -> str:
                return self.mk("skill_calls", col, cid, STRICT, team, private)

            at = (s["started_at"] if s else self.ts(2, 20)) + timedelta(minutes=minute)
            tool = s["tool"] if s else "claude"
            proj = f"{'pipeline-poc' if team else 'client-notes' if private else 'own-app'}-{mk('project')}"
            self.add("skill_calls", {"id": cid, "ts": at, "skill": skill, "invoked_by": tool if s else "me",
                                     "agent": tool, "cwd": f"/Users/synth-dev/work/{mk('cwd')}", "project": proj,
                                     "backfill": False, "source_key": f"synth-skill-{mk('source_key')}",
                                     "session_id": sess, "created_at": at + timedelta(minutes=1),
                                     "logged_session": f"synth-log-{mk('logged_session')}"})
            if team:
                self.ds.team["skill_calls"].append(str(cid))
            if private:
                self.ds.private["skill_calls"].append(str(cid))

    def lessons(self) -> None:
        """Two lessons: one scoped to an own repo (its decision may go to past.dev) and, Mission 004C (F8), one
        scoped to the private `client-notes` repo (never sent)."""
        mk = self.mk
        repo_of = {r["session_id"]: r["repo"] for r in self.ds.rows["sessions"]}
        lid, plid = uid("lesson:repro-first"), uid("lesson:client-export")
        for les, slug, title, rule, scope, d, private in (
                (lid, "synth-repro-first", "reproduce the failure before the fix",
                 "write a failing test before you edit code", repo_of["synth-claude-01"], 9, False),
                (plid, "synth-client-export", "check the export columns first", "diff the column list before a run",
                 repo_of["synth-private-01"], 12, True)):
            self.add("lessons", {
                "id": les, "slug": slug,
                "title": f"SYNTHETIC: {title} {mk('lessons', 'title', les, STRICT if private else DECISION, private=private)}",
                "rule": f"SYNTHETIC: {rule} {mk('lessons', 'rule', les, private=private)}",
                "scope_repo": scope, "status": "trial", "source_run_ids": [self.ds.ids[f"run:{HARVEST}:{d}"]],
                "evidence": f"SYNTHETIC: seen in three sessions, notes in /Users/synth-dev/"
                            f"{mk('lessons', 'evidence', les, private=private)}",
                "evals": "synth-eval", "since": self.ds.anchor - timedelta(days=d),
                "created_at": self.ts(d, 5, 5), "updated_at": self.ts(d, 5, 5)})
            if private:
                self.ds.private["lessons"].append(str(les))
        self.ds.ids["lesson"] = lid
        for d, decision, why, lesson in ((9, "accept", "the repro step was skipped in three sessions", lid),
                                         (16, "reject", "a one-off, not a pattern", None),
                                         (12, "accept", "the export broke twice on a renamed column", plid)):
            run = self.ds.ids[f"run:{HARVEST}:{d}"]
            private = lesson == plid
            self.add("lesson_candidates", {
                "run_id": run, "decision": decision,
                "reason": f"SYNTHETIC: {why} "
                          f"{mk('lesson_candidates', 'reason', run, STRICT if private else DECISION, private=private)}",
                "lesson_id": lesson, "considered_at": self.ts(d, 5, 10)})
            if private:
                self.ds.private["lesson_candidates"].append(str(run))

    # ------------------------------------------------------------ the RSI story

    def rsi(self) -> None:
        mk = self.mk
        devs = self.ds.rows["deviations"]
        team_devs = set(self.ds.team["deviations"])
        r1_own = [d for d in devs if (d["instruction_source"], d["rule_id"]) == R1 and str(d["id"]) not in team_devs
                  and self._prod(d["session_id"])]
        r1_team = [d for d in devs if (d["instruction_source"], d["rule_id"]) == R1 and str(d["id"]) in team_devs]
        r2 = [d for d in devs if (d["instruction_source"], d["rule_id"]) == R2]
        dev_ref = lambda d: refs.amem("deviation", str(d["id"]))       # noqa: E731
        r1key = rule_key(R1)
        tr = trace_ids()

        # candidates
        c1, c2 = uid("candidate:r1"), uid("candidate:r1:team")
        self.add("rsi_candidates", {
            "id": c1, "dedupe_key": f"synth:{r1key}", "source": "deviations", "kind": "edit_instruction",
            "target": f"~/.agents/skills/synth-debugging/SKILL.md {mk('rsi_candidates', 'target', c1)}",
            "rule_key": r1key, "evidence_refs": [dev_ref(d) for d in r1_own],
            "metrics": {"synthetic": True, "n": len(r1_own), "note": mk("rsi_candidates", "metrics", c1)},
            "jev": {"verdict": "edit_instruction", "note": mk("rsi_candidates", "jev", c1)}, "rank": 0.875,
            "status": "rejected",
            "decided_reason": f"SYNTHETIC: arm B was worse in the sandbox {mk('rsi_candidates', 'decided_reason', c1)}",
            "seen_count": len(r1_own), "first_seen_at": self.ts(13, 3, 30), "last_seen_at": self.ts(11, 3, 30),
            "last_considered_at": self.ts(11, 4), "meta": {"synthetic": True}})
        self.add("rsi_candidates", {
            "id": c2, "dedupe_key": f"synth-team:{r1key}", "source": "deviations", "kind": "edit_instruction",
            "target": f"pipeline-poc runbook {mk('rsi_candidates', 'target', c2, team=True)}", "rule_key": r1key,
            "evidence_refs": [dev_ref(d) for d in r1_team],
            "metrics": {"synthetic": True, "team": True, "note": mk("rsi_candidates", "metrics", c2, team=True)},
            "jev": None, "rank": 0.5, "status": "retired", "decided_reason": None, "seen_count": len(r1_team),
            "first_seen_at": self.ts(8, 3, 30), "last_seen_at": self.ts(8, 3, 30), "last_considered_at": None,
            "meta": {"synthetic": True, "team": True}})
        self.ds.team["rsi_candidates"].append(str(c2))

        # artifacts
        arts = {}
        for name, team, created in (("r1", False, self.ts(11, 6)), ("team", True, self.ts(7, 6))):
            manifest = {"format": "rsi-artifact/1", "synthetic": True, "rule": r1key, "team": team,
                        "change": f"SYNTHETIC: add a 'reproduce first' line ({name})"}
            canon = _canon(manifest)
            h = _sha(canon)
            manifest["change"] += f" {mk('rsi_artifacts', 'manifest', h, team=team)}"
            canon = _canon(manifest)
            h2 = _sha(canon)
            self.ds.markers[-1]["key"] = h2                       # the marker's row key is the final hash
            self.add("rsi_artifacts", {
                "hash": h2, "manifest_canonical": canon, "manifest": manifest,
                "files": {"SKILL.md": {"path": f"~/.agents/skills/synth-debugging/SKILL.md#"
                                               f"{mk('rsi_artifacts', 'files', h2, team=team)}",
                                       "sha256": _sha(f"synth-skill-{name}")}},
                "experiment_id": EXP_C2 if name == "r1" else None, "baseline_id": BASELINE, "created_at": created})
            arts[name] = h2
            if team:
                self.ds.team["rsi_artifacts"].append(h2)
        art1 = arts["r1"]

        # experiments
        self.add("rsi_experiments", {
            "id": EXP_C2, "night": self.ts(11, 6).date().isoformat(), "kind": "edit",
            "hypothesis": {"text": "SYNTHETIC: one 'reproduce before fix' line cuts repeat deviations "
                                   + mk("rsi_experiments", "hypothesis", EXP_C2, DECISION), "rule": r1key},
            "baseline_id": BASELINE, "status": "evaluated", "verdict": "worse",
            "meta": {"synthetic": True, "case": C2_CASE, "arms": ["A", "B"],
                     "note": mk("rsi_experiments", "meta", EXP_C2)},
            "created_at": self.ts(11, 6), "finished_at": self.ts(10, 13)})
        self.add("rsi_experiments", {
            "id": EXP_NCM, "night": self.ts(4, 8).date().isoformat(), "kind": "research",
            "hypothesis": {"text": "SYNTHETIC: NCM solves sudoku-easy at 0.98 or better "
                                   + mk("rsi_experiments", "hypothesis", EXP_NCM, DECISION)},
            "baseline_id": None, "status": "evaluated", "verdict": "pass",
            "meta": {"synthetic": True, "note": mk("rsi_experiments", "meta", EXP_NCM)},
            "created_at": self.ts(4, 8), "finished_at": self.ts(4, 9)})

        # proposals and the decision
        p1, p2 = uid("proposal:r1"), uid("proposal:r1:team")
        self.add("rsi_proposals", {
            "id": p1, "artifact_hash": art1, "experiment_id": EXP_C2, "candidate_id": c1,
            "title": f"SYNTHETIC: require a failing repro before any fix {mk('rsi_proposals', 'title', p1)}",
            "summary": f"SYNTHETIC: add one line to the debugging skill: reproduce first "
                       f"{mk('rsi_proposals', 'summary', p1, DECISION)}",
            "rationale": {"kind": "edit_instruction",
                          "why": f"SYNTHETIC: the same rule failed in seven sessions "
                                 f"{mk('rsi_proposals', 'rationale.why', p1, DECISION)}; ask sam.synth."
                                 f"{mk('rsi_proposals', 'rationale.why.email', p1)}@example.com"},
            "evidence_refs": [dev_ref(d) for d in r1_own], "risk": {"note": mk("rsi_proposals", "risk", p1)},
            "confidence": 0.625, "expected": {"deviations": "fewer", "note": mk("rsi_proposals", "expected", p1)},
            "targets": [f"~/.agents/skills/synth-debugging/SKILL.md#{mk('rsi_proposals', 'targets', p1)}"],
            "status": "rejected", "change_id": f"synth-change-{mk('rsi_proposals', 'change_id', p1)}",
            "result": f"SYNTHETIC: arm B worse on Codex {mk('rsi_proposals', 'result', p1, DECISION)}",
            "meta": {"synthetic": True}, "created_at": self.ts(10, 14), "expires_at": self.ts(7, 14),
            "decided_at": self.ts(9, 9), "promoted_at": None, "reverted_at": None})
        self.add("rsi_proposals", {
            "id": p2, "artifact_hash": arts["team"], "experiment_id": None, "candidate_id": c2,
            "title": f"SYNTHETIC team: pipeline-poc retry guard {mk('rsi_proposals', 'title', p2, team=True)}",
            "summary": f"SYNTHETIC team change {mk('rsi_proposals', 'summary', p2, team=True)}",
            "rationale": {"why": f"SYNTHETIC team reason {mk('rsi_proposals', 'rationale.why', p2, team=True)}"},
            "evidence_refs": [dev_ref(d) for d in r1_team], "risk": None, "confidence": 0.5, "expected": None,
            "targets": [f"/Users/synth-dev/work/pipeline-poc/{mk('rsi_proposals', 'targets', p2, team=True)}"],
            "status": "expired", "change_id": None, "result": None,
            "meta": {"synthetic": True, "team": True, "note": mk("rsi_proposals", "meta", p2, team=True)},
            "created_at": self.ts(7, 9), "expires_at": self.ts(4, 9), "decided_at": None, "promoted_at": None,
            "reverted_at": None})
        self.ds.team["rsi_proposals"].append(str(p2))
        d1 = uid("decision:p1")
        self.add("rsi_decisions", {
            "id": d1, "proposal_id": p1, "action": "reject", "artifact_hash": art1, "source": "cli",
            "verified": "cli-tty",
            "reason": f"SYNTHETIC: arm B was worse in the sandbox replay {mk('rsi_decisions', 'reason', d1, DECISION)}",
            "decided_at": self.ts(9, 9), "expires_at": self.ts(8, 9)})

        # eval results of the experiment
        evals = {}
        for name, model, verdict, minute in (("claude", "synth-claude-sonnet", "no-effect", 0),
                                             ("codex", "synth-gpt-codex", "worse", 30)):
            eid = uid(f"eval:{EXP_C2}:{name}")
            record = {"format": "rsi-eval/1", "synthetic": True, "arm": "B", "case": C2_CASE,
                      "note": mk("rsi_eval_results", "record", eid)}
            self.add("rsi_eval_results", {
                "id": eid, "artifact_hash": art1, "experiment_id": EXP_C2, "baseline_id": BASELINE,
                "suite": "synth-sandbox-replay", "suite_version": "1", "model": model, "effort": "low",
                "verdict": verdict, "record": record, "record_sha": _sha(_canon(record)),
                "created_at": self.ts(10, 13, minute)})
            evals[str(eid)] = {"model": model, "verdict": verdict}

        # improve edits: an earlier, reverted attempt at R1; a team one; a kept one for R2; and (Mission 004C, F8)
        # one built on the private session's repo-scoped deviation (never sent to past.dev)
        e1, e2, e3, e4 = uid("edit:r1"), uid("edit:r1:team"), uid("edit:r2"), uid("edit:private")
        priv_devs = [d for d in devs if d["rule_id"] == R_PRIVATE[1]]
        priv_rule = (priv_devs[0]["instruction_source"], R_PRIVATE[1])
        for eid, rule, status, team, private, ds_, created, pushed, reverted in (
                (e1, R1, "reverted", False, False, [d["id"] for d in r1_own[:3]], self.ts(16, 5), self.ts(16, 6),
                 self.ts(15, 6)),
                (e2, R1, "suggested", True, False, [d["id"] for d in r1_team], self.ts(7, 5), None, None),
                (e3, R2, "kept", False, False, [d["id"] for d in r2[:1]], self.ts(5, 5), self.ts(5, 6), None),
                (e4, priv_rule, "suggested", False, True, [d["id"] for d in priv_devs], self.ts(12, 5), None, None)):
            policy = STRICT if team or private else DECISION
            self.add("edits", {
                "id": eid, "improve_id": f"synth-improve-{mk('edits', 'improve_id', eid, team=team, private=private)}",
                "status": status,
                "target_path": f"/Users/synth-dev/.agents/skills/synth-debugging/SKILL.md#"
                               f"{mk('edits', 'target_path', eid, team=team, private=private)}",
                "instruction_source": rule[0], "rule_id": rule[1],
                "rule_title": f"SYNTHETIC rule title {mk('edits', 'rule_title', eid, team=team, private=private)}",
                "instruction_sha_before": _sha(f"synth-before:{eid}"), "instruction_sha_after": _sha(f"synth-after:{eid}"),
                "deviation_ids": ds_, "verdict": "edit_instruction", "jev_confidence": 0.625,
                "why": f"SYNTHETIC: sessions skipped the step this rule asks for "
                       f"{mk('edits', 'why', eid, policy, team, private)}",
                "summary": f"SYNTHETIC: added one line to the rule {mk('edits', 'summary', eid, policy, team, private)}",
                "pushed_at": pushed, "reverted_at": reverted,
                "meta": {"synthetic": True, "team": team, "kind": "add-line",
                         "branch": f"improve/{mk('edits', 'meta.branch', eid, team=team, private=private)}"},
                "created_at": created})
            if team:
                self.ds.team["edits"].append(str(eid))
            if private:
                self.ds.private["edits"].append(str(eid))

        # evidence: the two C2 Langfuse traces, and one NCM research run
        for arm, passed in (("A", True), ("B", False)):
            t = tr[arm]
            subjects = [refs.amem("experiment", EXP_C2)]
            if arm == "B":
                subjects += [refs.amem("artifact", art1), refs.amem("proposal", str(p1))]
            at = self.ts(10, 12, 30 * (arm == "B") + 20)
            self.add("rsi_evidence", {
                "id": uid(f"evidence:lf:{arm}"), "system": "langfuse", "kind": "trace", "subject_refs": subjects,
                "summary": f"sandbox run {t['run_key']} (claude), traced in Langfuse",
                "metrics": {"synthetic": True, "amem_session_id": t["session_id"], "experiment_id": EXP_C2,
                            "arm": arm, "case": C2_CASE, "rep": C2_REP, "kind": "sandbox", "agent": "claude",
                            "run_id": f"{arm}-synth-c2", "check_passed": passed, "tool_errors": 0 if passed else 2,
                            "exit_code": 0 if passed else 1, "eval_verdict": "pass" if passed else "worse"},
                "external_ref": refs.lf("trace", t["trace_id"]), "observed_at": at, "valid_until": None,
                "created_at": at})
        nid = uid("evidence:ncm")
        self.add("rsi_evidence", {
            "id": nid, "system": "ncm", "kind": "research-run", "subject_refs": [refs.amem("experiment", EXP_NCM)],
            "summary": f"SYNTHETIC NCM run on sudoku-easy {mk('rsi_evidence', 'summary', nid)}",
            "metrics": {"synthetic": True, "task": "sudoku-easy", "status": "ok", "solved_rate": 0.995, "cell_rate": 0.999,
                        "valid_rate": 1.0, "cost_usd": 0.05, "cost_gpu_minutes": 3.5, "runtime_s": 210.0,
                        "hw_gpu": "NVIDIA L4", "provider": "modal", "repo_commit": "synth0000000",
                        "note": mk("rsi_evidence", "metrics", nid)},
            "external_ref": refs.ncm_run(NCM_RUN), "observed_at": self.ts(4, 9), "valid_until": None,
            "created_at": self.ts(4, 9)})
        self.ds.ids.update({"rule": r1key, "candidate": str(c1), "proposal": str(p1), "team_proposal": str(p2),
                            "decision": str(d1), "artifact": art1, "edits": [str(e1), str(e2)], "edit_r2": str(e3),
                            "edit_private": str(e4), "evals": evals,
                            "r1_own_deviations": [str(d["id"]) for d in r1_own]})

    def _prod(self, sid: str) -> bool:
        return any(r["session_id"] == sid and r["env"] == "prod" for r in self.ds.rows["sessions"])


def build(anchor: date) -> Dataset:
    b = _Builder(anchor)
    b.sessions()
    b.deviations()
    b.runs()
    b.scores()
    b.skill_calls()
    b.lessons()
    b.rsi()
    b.ds.ids.pop("session_start", None)
    b.ds.team = dict(b.ds.team)
    b.ds.private = dict(b.ds.private)
    return b.ds


# ---------------------------------------------------------------- gold: what each check must find


def _round(v: Any) -> Any:
    """As ClickHouseRetriever returns a number (clickhouse._metric): floats to 4 decimals."""
    if isinstance(v, float):
        return round(v, 4) if math.isfinite(v) else None
    return v


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _qexact(values: Iterable[float | None], level: float) -> float | None:
    """quantileExact(level): the value at index floor(level * n) of the sorted values (NULLs skipped)."""
    v = sorted(x for x in values if x is not None)
    return v[min(int(level * len(v)), len(v) - 1)] if v else None


def _avg(values: Iterable[float | None]) -> float | None:
    v = [x for x in values if x is not None]
    return sum(v) / len(v) if v else None


_NOT_REF = re.compile(r"[^A-Za-z0-9._:/@-]")


def _part(v: Any) -> str:
    return _NOT_REF.sub("_", str(v)).replace("/", "_") if v not in (None, "") else "unknown"


def _qref(name: str, key: str) -> str:
    return f"ch:q.{name}/{_NOT_REF.sub('_', key)}"


def _tokens(s: Mapping[str, Any]) -> int:
    return sum(int(s[k] or 0) for k in ("tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_creation"))


class _CH:
    """The rows as ClickHouse holds them after a sync, and the named queries' semantics on them (the SQL in
    clickhouse.QUERIES, as the 004A/B docs describe it). `today` stands for now('UTC')."""

    def __init__(self, ds: Dataset, today: date):
        self.ds, self.today = ds, today
        self.prod = {s["session_id"]: s for s in ds.rows["sessions"] if (s["env"] or "prod") not in ("sandbox", "eval")}
        self.excluded = {s["session_id"] for s in ds.rows["sessions"] if s["env"] in ("sandbox", "eval")}
        # Mission 004C (F8): the ownership class of every session (default owner accounts), and the skill label
        # each call ships with (a non-own call: only a skill also called in an own session)
        self.cls = {s["session_id"]: egress.classify(s["meta"], (OWNER,)) for s in ds.rows["sessions"]}
        own_skills = {k["skill"] for k in ds.rows["skill_calls"] if self.cls.get(k["session_id"]) == egress.OWN}
        self.skill = {str(k["id"]): k["skill"] if self.cls.get(k["session_id"]) == egress.OWN or k["skill"] in own_skills
                      else None for k in ds.rows["skill_calls"]}

    def rule_key(self, d: Mapping[str, Any]) -> str:
        """`<source>:<rule_id or unknown>` as ClickHouse holds it: a team or private row's repo-scoped source is
        `<prefix>:<class>` with no rule id. An own repo-scoped source would ship as a keyed pseudonym, which gold
        cannot know, so the dataset has none."""
        cls = egress.TEAM if d["meta"].get("team") else self.cls.get(d["session_id"], egress.PRIVATE)
        src, rid = d["instruction_source"], d["rule_id"]
        if _ch.repo_scoped(src):
            if cls == egress.OWN:
                raise ValueError(f"own repo-scoped source in the synthetic dataset: {d['id']}")
            src, rid = f"{src.split(':', 1)[0]}:{cls}", None
        return f"{src}:{rid or 'unknown'}"

    def rule_gap(self, days: int = 90, min_n: int = 3, rule: str = "") -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for d in self.ds.rows["deviations"]:
            key = self.rule_key(d)
            if d["created_at"].date() >= self.today - timedelta(days=days) and d["session_id"] not in self.excluded \
                    and (not rule or key == rule):
                groups[key].append(d)
        out = []
        for key, ds in groups.items():
            obj = [d["objective_result"] for d in ds if d["objective_result"] is not None]
            if len(ds) < min_n or not obj:
                continue
            f, o = _avg(d["followed_grade"] for d in ds), _avg(obj)
            m = {"rule_key": key, "n_deviations": len(ds), "n_sessions": len({d["session_id"] for d in ds}),
                 "followed_avg": f, "result_avg": _avg(d["result_grade"] for d in ds), "objective_avg": o,
                 "objective_n": len(obj), "gap": o - f}
            m = {k: _round(v) for k, v in m.items()}
            m["flag"] = m["followed_avg"] <= 0.5 and m["objective_avg"] >= 0.8
            out.append({"ref": _qref("rule_gap", key), "metrics": m, "_order": (-(o - f), key)})
        return out

    def rule_weekly_rate(self, weeks: int = 8, rule: str = "") -> list[dict[str, Any]]:
        started = {sid: (s["started_at"] or s["created_at"]).date() for sid, s in self.prod.items()}
        totals: dict[date, int] = defaultdict(int)
        for d in started.values():
            totals[_monday(d)] += 1
        cutoff = _monday(self.today) - timedelta(weeks=weeks)
        groups: dict[tuple[date, str], list[dict[str, Any]]] = defaultdict(list)
        for d in self.ds.rows["deviations"]:
            key = self.rule_key(d)
            if d["session_id"] in started and _monday(started[d["session_id"]]) >= cutoff and (not rule or key == rule):
                groups[(_monday(started[d["session_id"]]), key)].append(d)
        out = []
        for (wk, key), ds in groups.items():
            hit = len({d["session_id"] for d in ds})
            m = {"week_start": wk.isoformat(), "rule_key": key, "n_deviations": len(ds), "sessions_hit": hit,
                 "sessions_total": totals[wk], "rate": _round(hit / totals[wk]) if totals[wk] else None}
            out.append({"ref": _qref("rule_weekly_rate", f"{key}@{wk.isoformat()}"), "metrics": m,
                        "_order": (-wk.toordinal(), -len(ds), key)})
        return out

    def tokens_by_outcome(self, days: int = 30) -> list[dict[str, Any]]:
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for s in self.prod.values():
            if (s["started_at"] or s["created_at"]).date() >= self.today - timedelta(days=days):
                groups[(s["outcome"], s["category"], s["model"], s["effort"])].append(s)
        out = []
        for (o, c, mo, e), ss in groups.items():
            tot = sum(_tokens(s) for s in ss)
            m = {"outcome": o, "category": c, "model": mo, "effort": e, "n_sessions": len(ss), "tokens_total": tot,
                 "tokens_out": sum(int(s["tokens_output"] or 0) for s in ss),
                 "active_total_s": _round(float(sum(s["active_seconds"] or 0 for s in ss))),
                 "active_p50_s": _round(_qexact((s["active_seconds"] for s in ss), 0.5))}
            out.append({"ref": _qref("tokens_by_outcome", "/".join(_part(x) for x in (o, c, mo, e))), "metrics": m,
                        "_order": (-tot, -len(ss))})
        return out

    def skill_outcomes(self, days: int = 30, min_n: int = 3) -> list[dict[str, Any]]:
        pairs = {(k["session_id"], self.skill[str(k["id"])]) for k in self.ds.rows["skill_calls"]
                 if self.skill[str(k["id"])] and k["session_id"] and k["ts"].date() >= self.today - timedelta(days=days)}
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for sid, skill in sorted(pairs):
            if sid in self.prod:
                groups[(skill, self.prod[sid]["model"])].append(self.prod[sid])
        out = []
        for (skill, model), ss in groups.items():
            if len(ss) < min_n:
                continue
            done = sum(s["outcome"] in ("shipped", "pushed", "committed") for s in ss)
            m = {"skill": skill, "model": model, "n_sessions": len(ss), "n_done": done,
                 "done_rate": round(done / len(ss), 4), "tokens_avg": round(sum(_tokens(s) for s in ss) / len(ss), 1)}
            out.append({"ref": _qref("skill_outcomes", f"{_part(skill)}/{_part(model)}"), "metrics": m,
                        "sessions": sorted(s["session_id"] for s in ss),
                        "_order": (-m["done_rate"], -len(ss), skill, model)})
        return out

    def routine_streaks(self, days: int = 30) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.ds.rows["runs"]:
            if r["status"] != "skip" and r["started_at"].date() >= self.today - timedelta(days=days):
                groups[r["routine"]].append(r)
        out = []
        for name, rs in groups.items():
            rs = sorted(rs, key=lambda r: r["started_at"])
            rev = [r["status"] for r in reversed(rs)]
            last_pass = rev.index("pass") + 1 if "pass" in rev else 0
            durs = [(r["finished_at"] - r["started_at"]).total_seconds() for r in rs]
            streak = len(rev) if last_pass == 0 else last_pass - 1
            fails = sum(s in ("fail", "error") for s in rev)
            m = {"routine": name, "n_runs": len(rs), "fails": fails, "fail_streak": streak, "last_status": rev[0],
                 "duration_p50_s": _round(_qexact(durs, 0.5)), "duration_p95_s": _round(_qexact(durs, 0.95))}
            out.append({"ref": _qref("routine_streaks", name), "metrics": m,
                        "subjects": [refs.amem("run", str(rs[-1]["id"]))], "_order": (-streak, -fails, name)})
        return out

    def research_runs(self, days: int = 90, task: str = "") -> list[dict[str, Any]]:
        out = []
        for e in self.ds.rows["rsi_evidence"]:
            m = e["metrics"]
            if e["system"] != "ncm" or (task and m.get("task") != task) \
                    or e["observed_at"].date() < self.today - timedelta(days=days):
                continue
            metrics = {"run_id": e["external_ref"][len("ncm:run/"):], "task": m.get("task"), "status": m.get("status"),
                       "gpu": str(m.get("hw_gpu")).replace(" ", "_"), "provider": m.get("provider"),
                       "solved_rate": m.get("solved_rate"), "cell_rate": m.get("cell_rate"),
                       "valid_rate": m.get("valid_rate"), "cost_usd": m.get("cost_usd"),
                       "gpu_minutes": m.get("cost_gpu_minutes"), "runtime_s": m.get("runtime_s")}
            out.append({"ref": _qref("research_runs", metrics["run_id"]),
                        "metrics": {k: _round(v) for k, v in metrics.items() if v is not None},
                        "subjects": [refs.amem("evidence", str(e["id"]))], "_order": (-e["observed_at"].timestamp(),)})
        return out


# name, params (as strings, as the CLI sends them)
GOLD_QUERIES: tuple[tuple[str, dict[str, str]], ...] = (
    ("rule_gap", {"days": "30", "min_n": "3"}),
    ("rule_weekly_rate", {"rule": rule_key(R1), "weeks": "8"}),
    ("rule_weekly_rate", {"weeks": "8"}),
    ("tokens_by_outcome", {"days": "30"}),
    ("skill_outcomes", {"days": "30", "min_n": "1"}),
    ("skill_outcomes", {"days": "30", "min_n": "3"}),
    ("routine_streaks", {"days": "30"}),
    ("research_runs", {"days": "90"}),
)


def _expected(ch: _CH, name: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
    p = {k: (v if k in ("rule", "task", "project") else int(v)) for k, v in params.items() if k != "limit"}
    rows = sorted(getattr(ch, name)(**p), key=lambda r: r["_order"])
    return [{k: v for k, v in r.items() if k != "_order"} for r in rows][:int(params.get("limit", "50"))]


def gold(ds: Dataset, *, today: date | None = None) -> dict[str, Any]:
    """Everything a check needs to know about the seeded data. `today`: the day the queries run (default: the
    anchor). The windows hold the whole story while today is at most 8 days after the anchor."""
    today = today or ds.anchor
    rows = ds.rows
    per_day: dict[str, dict[str, int]] = {}
    for t, (src, day) in CH_DAY.items():
        n: dict[str, int] = defaultdict(int)
        for r in rows[src]:
            if t == "research_runs" and r.get("system") != "ncm":
                continue
            n[r[day].astimezone(UTC).date().isoformat()] += 1
        per_day[t] = dict(sorted(n.items()))
    ch = _CH(ds, today)
    tr = trace_ids()
    prod = ch.prod
    r1 = rule_key(R1)
    r1_devs = [d for d in rows["deviations"] if f"{d['instruction_source']}:{d['rule_id']}" == r1
               and d["session_id"] in prod]
    r1_sessions = {d["session_id"] for d in r1_devs}
    recurring = {}
    for rk in sorted({f"{d['instruction_source']}:{d['rule_id']}" for d in rows["deviations"]}):
        ds_ = [d for d in rows["deviations"] if f"{d['instruction_source']}:{d['rule_id']}" == rk and d["session_id"] in prod]
        if len(ds_) >= 3:
            ss = {d["session_id"] for d in ds_}
            recurring[rk] = {"n": len(ds_), "sessions": len(ss),
                             "claude_sessions": sum(prod[s]["tool"] == "claude" for s in ss),
                             "codex_sessions": sum(prod[s]["tool"] == "codex" for s in ss)}
    team_sessions = set(ds.team.get("sessions", []))
    team_rows = {k: sorted(v) for k, v in ds.team.items()}
    private_rows = {k: sorted(v) for k, v in ds.private.items()}
    private_runs = set(private_rows.get("lesson_candidates", []))
    pastdev_send = sorted([refs.amem("proposal", ds.ids["proposal"]), refs.amem("decision", ds.ids["decision"]),
                           refs.amem("experiment", EXP_C2), refs.amem("experiment", EXP_NCM),
                           refs.amem("edit", ds.ids["edits"][0]), refs.amem("edit", ds.ids["edit_r2"])]
                          + [refs.amem("run", str(lc["run_id"])) for lc in rows["lesson_candidates"]
                             if str(lc["run_id"]) not in private_runs])
    refuse_private = sorted([refs.amem("edit", e) for e in private_rows.get("edits", [])]
                            + [refs.amem("run", r) for r in private_runs])

    def classes(rule: Any) -> dict[str, dict[str, int]]:
        """Rows per ownership class in the three tables that carry one, under `rule(session id) -> class`."""
        out: dict[str, dict[str, int]] = {}
        for t, sid_of, team_of in (("sessions", lambda r: r["session_id"], lambda r: False),
                                   ("deviations", lambda r: r["session_id"], lambda r: bool(r["meta"].get("team"))),
                                   ("skill_calls", lambda r: r["session_id"], lambda r: False)):
            n = dict.fromkeys(egress.CLASSES, 0)
            for r in rows[t]:
                n[egress.TEAM if team_of(r) else rule(sid_of(r))] += 1
            out[t] = n
        return out

    def before(sid: str | None) -> str:                 # the pre-F8 rule: anything not flagged team was own
        return egress.TEAM if sid and ch.cls.get(sid) == egress.TEAM else egress.OWN
    return {
        "dataset": DATASET, "version": VERSION, "synthetic": True, "anchor": ds.anchor.isoformat(),
        "queries_valid_until": (ds.anchor + timedelta(days=8)).isoformat(),
        "counts": ds.counts(),
        "clickhouse": {
            "tables": {t: sum(per_day[t].values()) for t in CH_DAY},
            "verify": per_day,
            "queries": [{"name": n, "params": p, "rows": _expected(ch, n, p)} for n, p in GOLD_QUERIES],
            "team": {"sessions": len(team_sessions),
                     "skill_calls": sum(1 for k in rows["skill_calls"] if k["session_id"] in team_sessions)},
            "private": {t: len(private_rows.get(t, [])) for t in ("sessions", "deviations", "skill_calls")},
        },
        "ownership": {"owner_accounts": [OWNER],
                      "after": classes(lambda sid: ch.cls.get(sid, egress.PRIVATE) if sid else egress.PRIVATE),
                      "before": classes(before)},
        "questions": {"repeat_failures": {
            "params": {"days": "30", "min_n": "3"}, "top_rule": r1, "recurring": recurring,
            "r1": {"n": len(r1_devs), "sessions": len(r1_sessions),
                   "claude_sessions": sum(prod[s]["tool"] == "claude" for s in r1_sessions),
                   "codex_sessions": sum(prod[s]["tool"] == "codex" for s in r1_sessions)},
            "attempts": sorted([refs.amem("proposal", ds.ids["proposal"]), refs.amem("proposal", ds.ids["team_proposal"])]
                               + [refs.amem("edit", e) for e in ds.ids["edits"]]),
            "experiments": [EXP_C2], "evals": ds.ids["evals"],
            "trace_links": {arm: refs.lf("trace", t["trace_id"]) for arm, t in tr.items()},
            "failing_traces": [refs.lf("trace", tr["B"]["trace_id"])],
        }},
        "langfuse": {"experiment": EXP_C2, "case": C2_CASE, "rep": C2_REP,
                     "traces": {arm: {**t, "check_passed": arm == "A"} for arm, t in tr.items()}},
        "pastdev": {"send": pastdev_send,
                    "refuse_team": sorted([refs.amem("proposal", ds.ids["team_proposal"]),
                                           refs.amem("edit", ds.ids["edits"][1])]),
                    "refuse_private": refuse_private},
        "team": team_rows,
        "private": private_rows,
        "markers": ds.markers,
    }


# ---------------------------------------------------------------- seed


def _jsonable(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    return v


def _insert(conn: psycopg.Connection, table: str, row: Mapping[str, Any]) -> None:
    cols = list(row)
    vals = [Jsonb(_jsonable(row[c])) if c in JSONB.get(table, ()) and row[c] is not None else row[c] for c in cols]
    q = sql.SQL("insert into {} ({}) values ({})").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols)))
    conn.execute(q, vals)


def _migrated(conn: psycopg.Connection) -> bool:
    names = ["schema_migrations", *TABLES, "rsi_checkpoints"]
    row = conn.execute("select count(*) from unnest(%s::text[]) t(n) where to_regclass('public.' || n) is not null",
                       (names,)).fetchone()
    return bool(row and row[0] == len(names))


def _sentinel(conn: psycopg.Connection) -> dict[str, Any] | None:
    row = conn.execute("select meta from rsi_checkpoints where job = %s and run_key = %s", SENTINEL).fetchone()
    return dict(row[0]) if row else None


def db_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {t: int(conn.execute(sql.SQL("select count(*) from {}").format(sql.Identifier(t))).fetchone()[0])
            for t in TABLES}


def write_gold(g: Mapping[str, Any], out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / "gold.json"
    path.write_text(json.dumps(_jsonable(g), indent=1, sort_keys=True) + "\n")
    return path


def seed(db_url: str, anchor: date | None = None, *, out: Path | str = "synth-out",
         connect: Callable[..., psycopg.Connection] = psycopg.connect) -> dict[str, Any]:
    """Seed a migrated, empty, local database (or reuse one this function seeded) and write gold.json.
    Returns a report: status (seeded | reused), host, anchor, counts, gold path."""
    host = check_local(db_url)
    with connect(db_url) as conn:
        if not _migrated(conn):
            raise SeedError("the database is not migrated (run `amem db migrate` against it first)")
        prior = _sentinel(conn)
        if prior is not None:
            stored = date.fromisoformat(str(prior.get("anchor")))
            if anchor is not None and anchor != stored:
                raise SeedError(f"already seeded with anchor {stored}; use a fresh database for anchor {anchor}")
            ds = build(stored)
            have = db_counts(conn)
            if any(have[t] < n for t, n in ds.counts().items()):
                raise SeedError("the synthetic rows were changed or removed since seeding; use a fresh database")
            status = "reused"
        else:
            ds = build(anchor or datetime.now(UTC).date())
            busy = {t: n for t, n in db_counts(conn).items() if n}
            if busy:
                raise SeedError(f"refusing to mix synthetic rows into a database that holds data "
                                f"({', '.join(f'{t}={n}' for t, n in sorted(busy.items()))}); use a fresh one")
            for t in TABLES:
                for row in ds.rows.get(t, []):
                    _insert(conn, t, row)
            conn.execute("insert into rsi_checkpoints (job, run_key, status, finished_at, meta) values "
                         "(%s, %s, 'done', now(), %s)",
                         (*SENTINEL, Jsonb({"dataset": DATASET, "version": VERSION, "synthetic": True,
                                            "anchor": ds.anchor.isoformat(), "counts": ds.counts()})))
            conn.commit()
            status = "seeded"
    path = write_gold(gold(ds), Path(out))
    return {"status": status, "host": host, "dataset": DATASET, "version": VERSION, "synthetic": True,
            "anchor": ds.anchor.isoformat(), "counts": ds.counts(), "markers": len(ds.markers), "gold": str(path)}
