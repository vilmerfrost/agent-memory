"""ClickHouse adapter (Mission 004A): schema, allowlists, idempotent inserts, cursor, replay + verify, named queries.

A fake ClickHouse HTTP server (FakeClickHouse on tests/integrations_mock.MockService) stores inserted
JSONEachRow rows per table, honours insert_deduplication_token and answers the fixed SELECTs (routed on the
exact SQL text, computed from the stored rows with FINAL semantics). Synthetic rows go into the disposable
Postgres as the owner; every sync and verify runs as amem_app. Nothing leaves 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from agent_memory import sessions_ingest
from agent_memory.integrations import clickhouse as CH, egress, evidence as E, sync
from integrations_mock import MockService, Request

APP_PW = "ch-test-pw-0123456789"
CH_USER, CH_KEY = "ch-test-user", "ch-test-key-not-a-real-secret"
TABLES = [t.name for t in CH.TABLES]
APPEND_ONLY = ("rsi_decision_results", "rsi_decisions", "rsi_decision_tokens", "rsi_eval_results", "rsi_proposals",
               "rsi_artifacts")
PLAIN = ("skill_calls", "scores", "deviations", "runs", "sessions")


# ---------------------------------------------------------------- fake ClickHouse


def _ts(s: str | None) -> datetime | None:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC) if s else None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _qexact(values: list[float], level: float) -> float | None:
    v = sorted(x for x in values if x is not None)
    return v[min(int(level * len(v)), len(v) - 1)] if v else None


def _avg(values: list[float | None]) -> float | None:
    v = [x for x in values if x is not None]
    return sum(v) / len(v) if v else None


@dataclass
class FakeClickHouse:
    svc: MockService
    rows: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    tokens: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    ddl: list[str] = field(default_factory=list)
    commit_then_fail: int = 0          # store the insert, then answer 503 (a lost response)

    def __post_init__(self) -> None:
        self.svc.route("GET", "/ping", lambda r: (200, "Ok.\n"))
        self.svc.route("POST", "/", self._post)

    # FINAL: per id the highest version; a tie keeps the last insert
    def final(self, table: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        best: dict[str, dict[str, Any]] = {}
        for r in self.rows[table]:
            if r["id"] not in best or r["version"] >= best[r["id"]]["version"]:
                best[r["id"]] = r
        return [r for r in best.values() if include_deleted or not r.get("_amem_deleted", 0)]

    def by_id(self, table: str) -> dict[str, dict[str, Any]]:
        return {r["id"]: r for r in self.final(table)}

    def inserts(self) -> list[Request]:
        return [r for r in self.svc.requests if r.method == "POST" and (r.q("query") or "").startswith("INSERT")]

    def _post(self, req: Request) -> tuple[int, str]:
        q = req.q("query")
        if q is not None:
            m = re.fullmatch(r"INSERT INTO amem\.([a-z_]+) FORMAT JSONEachRow", q)
            if not m:
                return 400, "unexpected query param"
            table, token = m.group(1), req.q("insert_deduplication_token") or ""
            if token and token in self.tokens[table]:
                return 200, ""                       # deduplicated: same token, stored once
            self.tokens[table].add(token)
            self.rows[table] += [json.loads(line) for line in req.body.decode().splitlines() if line]
            if self.commit_then_fail:
                self.commit_then_fail -= 1
                return 503, "injected: committed, response lost"
            return 200, ""
        text = req.body.decode()
        if text.startswith(("CREATE ", "ALTER ")):
            self.ddl.append(text)
            return 200, ""
        if req.q("database") != "amem":
            return 400, "no database"
        if re.fullmatch(r"SELECT _amem_deleted FROM [a-z_]+ LIMIT 0 FORMAT JSONEachRow", text):
            return 200, ""
        if text.startswith("SELECT id, _amem_deleted FROM "):
            m = re.fullmatch(r"SELECT id, _amem_deleted FROM ([a-z_]+) FINAL WHERE id > \{after:String\} ORDER BY id LIMIT \{limit:UInt32\} FORMAT JSONEachRow", text)
            if m:
                rows = sorted((r for r in self.final(m.group(1), include_deleted=True) if r['id'] > (req.q('param_after') or '')), key=lambda r:r['id'])
                return 200, self._jsonl([{'id': r['id'], '_amem_deleted': r.get('_amem_deleted', 0)} for r in rows[:int(req.q('param_limit') or 500)]])
        for t, s in CH.VERIFY_SQL.items():
            if text == s:
                return 200, self._jsonl(self._verify(t, req.q("param_since") or ""))
        for name, qd in CH.QUERIES.items():
            if text == qd.sql:
                return 200, self._jsonl(getattr(self, f"_q_{name}")(req))
        return 400, "unknown statement"

    @staticmethod
    def _jsonl(rows: list[dict[str, Any]]) -> str:
        return "".join(json.dumps(r) + "\n" for r in rows)

    def _verify(self, table: str, since: str) -> list[dict[str, Any]]:
        day = next(t.day for t in CH.TABLES if t.name == table)
        n: dict[str, int] = defaultdict(int)
        for r in self.final(table):
            d = _ts(r[day]).date().isoformat()
            if d >= since:
                n[d] += 1
        return [{"d": d, "n": str(c)} for d, c in sorted(n.items())]      # 64-bit ints may arrive quoted

    def _prod_sessions(self) -> dict[str, dict[str, Any]]:
        return {s["id"]: s for s in self.final("sessions") if (s["env"] or "prod") not in ("sandbox", "eval")}

    @staticmethod
    def _rule(d: dict[str, Any]) -> str:
        return f"{d['instruction_source']}:{d['rule_id'] or 'unknown'}"

    def _q_rule_weekly_rate(self, req: Request) -> list[dict[str, Any]]:
        weeks, rule = int(req.q("param_weeks") or 0), req.q("param_rule") or ""
        cutoff = _monday(datetime.now(UTC).date()) - timedelta(weeks=weeks)
        sessions = self._prod_sessions()
        started = {i: _ts(s["started_at"] or s["created_at"]).date() for i, s in sessions.items()}
        totals: dict[date, int] = defaultdict(int)
        for d in started.values():
            totals[_monday(d)] += 1
        groups: dict[tuple[date, str], list[dict[str, Any]]] = defaultdict(list)
        for d in self.final("deviations"):
            if d["session_id"] in started and _monday(started[d["session_id"]]) >= cutoff \
                    and (not rule or self._rule(d) == rule):
                groups[(_monday(started[d["session_id"]]), self._rule(d))].append(d)
        out = []
        for (wk, rk), ds in groups.items():
            hit = len({d["session_id"] for d in ds})
            out.append({"week_start": wk.isoformat(), "rule_key": rk, "n_deviations": len(ds), "sessions_hit": hit,
                        "sessions_total": totals[wk], "rate": hit / totals[wk] if totals[wk] else None,
                        "sample_ids": sorted(d["id"] for d in ds)[:3]})
        return sorted(out, key=lambda r: (r["week_start"], r["n_deviations"]), reverse=True)

    def _q_tokens_by_outcome(self, req: Request) -> list[dict[str, Any]]:
        days, project = int(req.q("param_days") or 0), req.q("param_project") or ""
        cutoff = datetime.now(UTC).date() - timedelta(days=days)
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for s in self._prod_sessions().values():
            if _ts(s["started_at"] or s["created_at"]).date() >= cutoff and (not project or s["project"] == project):
                groups[(s["outcome"], s["category"], s["model"], s["effort"])].append(s)
        out = []
        for (o, c, m, e), ss in groups.items():
            tok = sum((s["tokens_input"] or 0) + (s["tokens_output"] or 0) + (s["tokens_cache_read"] or 0)
                      + (s["tokens_cache_creation"] or 0) for s in ss)
            out.append({"outcome": o, "category": c, "model": m, "effort": e, "n_sessions": len(ss),
                        "tokens_total": tok, "tokens_out": sum(s["tokens_output"] or 0 for s in ss),
                        "active_total_s": sum(s["active_seconds"] or 0 for s in ss),
                        "active_p50_s": _qexact([s["active_seconds"] for s in ss], 0.5)})
        return sorted(out, key=lambda r: (r["tokens_total"], r["n_sessions"]), reverse=True)

    def _q_research_runs(self, req: Request) -> list[dict[str, Any]]:
        task, days = req.q("param_task") or "", int(req.q("param_days") or 0)
        cutoff = datetime.now(UTC).date() - timedelta(days=days)
        rows = [r for r in self.final("research_runs") if (not task or r["task"] == task)
                and _ts(r["observed_at"]).date() >= cutoff]
        return [{**{k: v for k, v in r.items() if k != "version"}, "observed": r["observed_at"]}
                for r in sorted(rows, key=lambda r: r["observed_at"], reverse=True)]

    def _q_skill_outcomes(self, req: Request) -> list[dict[str, Any]]:
        days, min_n = int(req.q("param_days") or 0), int(req.q("param_min_n") or 0)
        cutoff = datetime.now(UTC).date() - timedelta(days=days)
        prod = self._prod_sessions()
        pairs = {(k["session_id"], k["skill"]) for k in self.final("skill_calls")
                 if k["skill"] and k["session_id"] and _ts(k["ts"]).date() >= cutoff}
        groups: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
        for sid, skill in sorted(pairs):
            if sid in prod:
                groups[(skill, prod[sid]["model"])].append(prod[sid])
        out = []
        for (skill, model), ss in groups.items():
            done = sum(s["outcome"] in ("shipped", "pushed", "committed") for s in ss)
            tok = [(s["tokens_input"] or 0) + (s["tokens_output"] or 0) + (s["tokens_cache_read"] or 0)
                   + (s["tokens_cache_creation"] or 0) for s in ss]
            if len(ss) >= min_n:
                out.append({"skill": skill, "model": model, "n_sessions": len(ss), "n_done": done,
                            "done_rate": round(done / len(ss), 4), "tokens_avg": round(sum(tok) / len(tok), 1),
                            "sample_ids": [s["id"] for s in ss][:5]})
        return sorted(out, key=lambda r: (-r["done_rate"], -r["n_sessions"], r["skill"], r["model"] or ""))

    def _q_routine_streaks(self, req: Request) -> list[dict[str, Any]]:
        days, routine = int(req.q("param_days") or 0), req.q("param_routine") or ""
        cutoff = datetime.now(UTC).date() - timedelta(days=days)
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.final("runs"):
            if r["status"] != "skip" and r["started_at"] and _ts(r["started_at"]).date() >= cutoff \
                    and (not routine or r["routine"] == routine):
                groups[r["routine"]].append(r)
        out = []
        for name, rs in groups.items():
            rev = [r["status"] for r in sorted(rs, key=lambda r: r["started_at"], reverse=True)]
            last_pass = rev.index("pass") + 1 if "pass" in rev else 0
            durs = [r["duration_s"] for r in rs]
            out.append({"routine": name, "n_runs": len(rs), "fails": sum(s in ("fail", "error") for s in rev),
                        "fail_streak": len(rev) if last_pass == 0 else last_pass - 1, "last_status": rev[0],
                        "duration_p50_s": _qexact(durs, 0.5), "duration_p95_s": _qexact(durs, 0.95),
                        "last_run_id": max(rs, key=lambda r: r["started_at"])["id"]})
        return sorted(out, key=lambda r: (-r["fail_streak"], -r["fails"], r["routine"]))

    def _q_rule_gap(self, req: Request) -> list[dict[str, Any]]:
        days, rule, min_n = int(req.q("param_days") or 0), req.q("param_rule") or "", int(req.q("param_min_n") or 0)
        cutoff = datetime.now(UTC).date() - timedelta(days=days)
        excluded = {s["id"] for s in self.final("sessions") if s["env"] in ("sandbox", "eval")}
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for d in self.final("deviations"):
            if _ts(d["created_at"]).date() >= cutoff and d["session_id"] not in excluded \
                    and (not rule or self._rule(d) == rule):
                groups[self._rule(d)].append(d)
        out = []
        for rk, ds in groups.items():
            obj = [d["objective_result"] for d in ds if d["objective_result"] is not None]
            f, o = _avg([d["followed_grade"] for d in ds]), _avg(obj)
            if len(ds) >= min_n and obj:
                out.append({"rule_key": rk, "n_deviations": len(ds), "n_sessions": len({d["session_id"] for d in ds}),
                            "followed_avg": f, "result_avg": _avg([d["result_grade"] for d in ds]),
                            "objective_avg": o, "objective_n": len(obj), "gap": o - f,
                            "sample_ids": sorted(d["id"] for d in ds)[:3]})
        return sorted(out, key=lambda r: (-r["gap"], r["rule_key"]))


@pytest.fixture
def fake() -> Iterator[FakeClickHouse]:
    with MockService() as svc:
        yield FakeClickHouse(svc)


def client_for(url: str, **kw: Any) -> CH.Client:
    return CH.Client(CH.Config(url, CH_USER, CH_KEY), sleep=lambda s: None, **kw)


# ---------------------------------------------------------------- disposable Postgres: synthetic rows with markers


class Seed:
    """Synthetic rows in every synced table. Every text column (and every jsonb / array) holds a unique marker."""

    def __init__(self, owner_url: str):
        self.url = owner_url
        self.markers: list[str] = []
        self.now = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
        self.ids: dict[str, list[str]] = defaultdict(list)

    def mk(self, tag: str) -> str:
        m = f"ZZLEAK{tag}{secrets.token_hex(5)}"
        self.markers.append(m)
        return m

    def ins(self, conn: psycopg.Connection, table: str, returning: str = "id", **cols: Any) -> str:
        q = sql.SQL("insert into {} ({}) values ({}) returning {}").format(
            sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols)),
            sql.SQL(", ").join(sql.Placeholder() * len(cols)), sql.Identifier(returning))
        key = str(conn.execute(q, list(cols.values())).fetchone()[0])
        self.ids[table].append(key)
        return key

    def session(self, conn: psycopg.Connection, sid: str, *, env: str = "prod", outcome: str = "shipped",
                category: str = "chat", tokens: int = 1000, active: float = 600.0, created: datetime | None = None,
                **extra: Any) -> str:
        t = self.now - timedelta(hours=1)
        cols = dict(session_id=sid, kind=category, tool="claude", project_dir=self.mk("pdir"), repo="agent-memory",
                    started_at=t, ended_at=t + timedelta(minutes=30), last_activity_at=t + timedelta(minutes=30),
                    transcript_bytes=12345, skills=Jsonb([self.mk("skill")]), instructions=Jsonb([self.mk("instr")]),
                    # an own session, as sessions_ingest stores one (Mission 004C, F8: unknown ownership is private)
                    meta=Jsonb({"team": False, "remote_owner": "vilmerfrost", "remote_host": "github.com",
                                "note": self.mk("smeta")}),
                    tokens_input=tokens, tokens_output=tokens // 2,
                    tokens_cache_read=tokens * 3, tokens_cache_creation=10, models=Jsonb({self.mk("models"): {"calls": 1}}),
                    turns=4, tokens_updated_at=t + timedelta(minutes=31), title=self.mk("title"),
                    title_updated_at=t, category=category, project="chtest", model="claude-test-1", model_last=self.mk("ml"),
                    effort="high", effort_last=self.mk("el"), efforts=Jsonb({self.mk("efforts"): 3}),
                    duration_seconds=1800.0, active_seconds=active, api_seconds=300.0, tool_seconds=200.0,
                    lines_added=10, lines_removed=2, outcome=outcome,
                    outcome_evidence=Jsonb({"commits": [{"message": self.mk("commit")}]}), outcome_at=t,
                    summary=self.mk("summary"), summary_updated_at=t, env=env, experiment_id="exp-20261016-01")
        if created is not None:
            cols["created_at"] = created
        cols.update(extra)
        return self.ins(conn, "sessions", "session_id", **cols)

    def deviation(self, conn: psycopg.Connection, sid: str, source: str, rule: str, followed: float,
                  objective: float | None, **extra: Any) -> str:
        cols = dict(source_key=self.mk("dkey"), session_id=sid, instruction_source=source, instruction_ref=self.mk("iref"),
                    turn=3, what=self.mk("what"), why=self.mk("why"), how=self.mk("how"), followed_grade=followed,
                    result_grade=0.5, jev_verdict="enforce", jev_confidence=0.7, jev_mode="fixture",
                    jev_answers=Jsonb({"a": self.mk("jeva")}), writer_model="claude-test-1", rule_id=rule,
                    rule_title=self.mk("rtitle"), instruction_sha=hashlib.sha256(b"x").hexdigest(),
                    meta=Jsonb({"m": self.mk("dmeta")}), result_outcome="shipped", objective_result=objective)
        cols.update(extra)
        return self.ins(conn, "deviations", **cols)

    def run(self, conn: psycopg.Connection, routine: str, status: str, minutes_ago: int, dur: int) -> str:
        start = self.now - timedelta(minutes=minutes_ago)
        return self.ins(conn, "runs", routine=routine, source="launchd", started_at=start,
                        finished_at=start + timedelta(seconds=dur), status=status, summary=self.mk("rsum"),
                        failure_reason=self.mk("rfail"), report_path=f"/Users/x/{self.mk('rpath')}.md",
                        meta=Jsonb({"m": self.mk("rmeta")}), source_key=self.mk("rkey"))

    def load(self) -> Seed:
        with psycopg.connect(self.url) as conn:
            s1 = self.session(conn, "chs-1", outcome="shipped", tokens=1000, active=600.0)
            s2 = self.session(conn, "chs-2", outcome="shipped", tokens=3000, active=1200.0)
            s3 = self.session(conn, "chs-3", outcome="failed", category="worker", tokens=500, active=60.0,
                              created=self.now - timedelta(days=1))
            # a label column holding free text: shipped as null
            self.session(conn, "chs-4", outcome="pushed", repo=f"{self.mk('repo')} with spaces")
            sbx = self.session(conn, "chs-sbx", env="sandbox", tokens=99999)
            for sid, f, o in ((s1, 0.30, 0.90), (s2, 0.25, 0.95), (s3, 0.40, 0.85), (s1, 0.35, 0.90)):
                self.deviation(conn, sid, "claude-md:global", "explaining/core-rule", f, o)
            for sid, f, o in ((s1, 0.80, 0.40), (s2, 0.85, 0.35), (s3, 0.90, 0.45)):
                self.deviation(conn, sid, "skill:dispatch", "spawn", f, o)
            self.deviation(conn, s2, "skill:dispatch", "spawn", 0.9, None, writer_model=f"model {self.mk('wm')}")
            self.deviation(conn, sbx, "claude-md:global", "explaining/core-rule", 1.0, 0.0)   # sandbox: left out
            r1 = self.run(conn, "chtest-daily", "pass", 240, 10)
            self.run(conn, "chtest-daily", "fail", 180, 20)
            self.run(conn, "chtest-daily", "skip", 150, 5)
            self.last_run = self.run(conn, "chtest-daily", "error", 120, 40)
            self.run(conn, "chtest-ok", "pass", 100, 7)
            self.ins(conn, "scores", run_id=r1, question="pass", kind="choice", value=self.mk("sval"), value_num=None,
                     confidence=0.9, probabilities=Jsonb({self.mk("prob"): 0.9}), jev_mode="fixture")
            self.ins(conn, "skill_calls", ts=self.now, skill="dispatch", invoked_by="me", agent="claude",
                     cwd=f"/Users/x/{self.mk('cwd')}", project="chtest", source_key=self.mk("skey"), session_id=s1,
                     logged_session=self.mk("lsess"))
            manifest = json.dumps({"format": "rsi-artifact/1", "n": secrets.token_hex(4)}, separators=(",", ":"))
            art = self.ins(conn, "rsi_artifacts", "hash", hash=hashlib.sha256(manifest.encode()).hexdigest(),
                           manifest_canonical=manifest, manifest=Jsonb(json.loads(manifest)), files=Jsonb({}),
                           baseline_id="base-1")
            prop = self.ins(conn, "rsi_proposals", artifact_hash=art, experiment_id="exp-20261016-01",
                            title=self.mk("ptitle"), summary=self.mk("psum"), rationale=Jsonb({"r": self.mk("prat")}),
                            evidence_refs=[self.mk("pref")], risk=Jsonb({"k": self.mk("prisk")}), confidence=0.6,
                            expected=Jsonb({"e": self.mk("pexp")}), targets=[f"~/.claude/{self.mk('ptgt')}"],
                            change_id=self.mk("pchg"), result=self.mk("pres"), meta=Jsonb({"m": self.mk("pmeta")}))
            self.ins(conn, "rsi_decisions", proposal_id=prop, action="reject", artifact_hash=art, source="cli",
                     verified="cli-tty", reason=self.mk("reason"), expires_at=self.now + timedelta(days=1))
            # Mission 004B: an NCM research run's evidence row ships to research_runs; any other evidence never does
            ncm = self.ins(conn, "rsi_evidence", system="ncm", kind="research-run", subject_refs=["amem:experiment/exp-20261016-01"],
                           summary=self.mk("ncmsum"), external_ref="ncm:run/ncm-20261016-sudoku-easy",
                           metrics=Jsonb({"task": "sudoku-easy", "status": "ok", "solved_rate": 0.995, "cell_rate": 0.999,
                                          "valid_rate": 1.0, "cost_usd": 0.05, "cost_gpu_minutes": 3.1, "hw_gpu": "NVIDIA L4",
                                          "provider": "modal", "note": self.mk("ncmnote")}))
            self.ids["research_runs"].append(ncm)
            self.ins(conn, "rsi_evidence", system="langfuse", kind="trace", subject_refs=[], summary=self.mk("lfsum"),
                     external_ref="lf:trace/" + "cd" * 16, metrics=Jsonb({"tokens_total": 1}))
            self.ins(conn, "rsi_eval_results", artifact_hash=art, experiment_id="exp-20261016-01", baseline_id="base-1",
                     suite="static", suite_version="1", model="none", effort="none", verdict="pass",
                     record=Jsonb({"format": "rsi-eval/1", "note": self.mk("record")}),
                     record_sha=hashlib.sha256(b"rec").hexdigest())
        return self


def _wipe(owner_url: str) -> None:
    with psycopg.connect(owner_url) as conn:
        conn.execute("set session_replication_role = replica")     # superuser-only: skips the append-only triggers
        for t in APPEND_ONLY:
            conn.execute(sql.SQL("delete from {}").format(sql.Identifier(t)))
        conn.execute("set session_replication_role = origin")
        for t in PLAIN:
            conn.execute(sql.SQL("delete from {}").format(sql.Identifier(t)))
        conn.execute("delete from rsi_checkpoints where job like 'sync:%'")
        conn.execute("delete from rsi_evidence where system in ('ncm', 'langfuse')")


@pytest.fixture
def app(pg_url: str) -> Iterator[str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{APP_PW}'")
    _wipe(pg_url)
    yield pg_url.replace("postgres:postgres@", f"amem_app:{APP_PW}@", 1)
    _wipe(pg_url)


@pytest.fixture
def seed(pg_url: str, app: str) -> Seed:
    return Seed(pg_url).load()


def counts(seed: Seed) -> dict[str, int]:
    return {t: len(seed.ids[t]) for t in TABLES}


# ---------------------------------------------------------------- schema and allowlists


TEXT_COLUMNS = {"title", "summary", "what", "why", "how", "instruction_ref", "rule_title", "failure_reason",
                "report_path", "reason", "rationale", "targets", "evidence_refs", "result", "record", "meta", "cwd",
                "project_dir", "outcome_evidence", "value", "probabilities", "jev_answers", "source_key", "skills",
                "instructions", "models", "efforts", "risk", "expected", "change_id", "manifest", "files"}



@pytest.fixture(autouse=True)
def _pseudonym_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CH.PSEUDONYM_ENV, "t" * 40)

def test_schema_is_replacing_merge_tree_with_dedup_window() -> None:
    stmts = CH.schema_sql()
    assert stmts[0] == "CREATE DATABASE IF NOT EXISTS amem" and len(stmts) == 1 + len(CH.TABLES)
    for t, s in zip(CH.TABLES, stmts[1:], strict=True):
        assert s.startswith(f"CREATE TABLE IF NOT EXISTS amem.{t.name} (")
        assert "ENGINE = ReplacingMergeTree(version)" in s and "\nORDER BY id\n" in s
        m = re.search(r"non_replicated_deduplication_window = (\d+)", s)
        assert m and int(m.group(1)) > 0
        cols = re.findall(r"^    ([a-z_0-9]+) ", s, re.M)
        assert cols == [*t.allowlist, "version"] and not TEXT_COLUMNS & set(cols)
        assert "    id String," in s and "version UInt64" in s
    with pytest.raises(ValueError):
        CH.schema_sql("amem; drop")


def test_map_row_keeps_only_the_allowlist() -> None:
    t = next(t for t in CH.TABLES if t.name == "deviations")
    raw = {"id": "d1", "session_id": "s1", "what": "secret words", "instruction_source": "claude-md:global",
           "rule_id": "has spaces in it", "followed_grade": 0.3, "objective_result": float("nan"),
           "created_at": datetime(2026, 10, 16, 9, 0, tzinfo=UTC)}
    out = CH.map_row(t, raw, 7)
    assert out is not None and set(out) == {*t.allowlist, "version"} and "what" not in out
    assert out["rule_id"] is None and out["objective_result"] is None and out["version"] == 7
    assert out["created_at"] == "2026-10-16 09:00:00.000000"
    assert CH.map_row(t, {**raw, "id": "bad id"}, 7) is None          # not a ref id: dropped


def test_ensure_schema_sends_one_statement_per_request_with_auth(fake: FakeClickHouse) -> None:
    n = CH.ensure_schema(client_for(fake.svc.url))
    assert n == len(fake.ddl) == 1 + len(CH.TABLES)
    assert not any(statement.startswith('ALTER') for statement in fake.ddl)
    assert all('_amem_deleted UInt8 DEFAULT 0' in statement for statement in fake.ddl[1:])
    for r in fake.svc.requests_to("POST", "/"):
        h = {k.lower(): v for k, v in r.headers.items()}                  # header names are case-insensitive
        assert h.get("x-clickhouse-user") == CH_USER and h.get("x-clickhouse-key") == CH_KEY
        assert CH_KEY not in r.path and not any(CH_KEY in v for vs in r.query.values() for v in vs)


# ---------------------------------------------------------------- the leak test


def test_no_text_ever_leaves(seed: Seed, app: str, fake: FakeClickHouse) -> None:
    sink = CH.ClickHouseSink(client_for(fake.svc.url), batch_size=3)
    with psycopg.connect(app) as conn:
        rep = sync.run(sink, conn, replay=True, environ={})
    assert rep.status == "ok" and rep.failed == 0
    assert {t: len(fake.by_id(t)) for t in TABLES} == counts(seed)       # non-vacuous: every row arrived
    assert len(seed.markers) > 50
    wire = "\n".join(f"{r.method} {r.path} {json.dumps(r.query)} {json.dumps(r.headers)} "
                     f"{r.body.decode('utf-8', 'replace')}" for r in fake.svc.requests)
    leaked = [m for m in seed.markers if m in wire]
    assert leaked == [] and "ZZLEAK" not in wire
    assert fake.by_id("sessions")["chs-4"]["repo"] is None                # free text in a label column: null
    assert fake.by_id("sessions")["chs-1"]["repo"] == CH.pseudonym("agent-memory")   # Mission 004B: a keyed pseudonym
    with psycopg.connect(app) as conn:                                     # no per-row rsi_evidence links
        assert conn.execute("select count(*) from rsi_evidence where system = 'clickhouse'").fetchone()[0] == 0


# ---------------------------------------------------------------- idempotency and the cursor


def test_same_batch_twice_is_stored_once(seed: Seed, app: str, fake: FakeClickHouse) -> None:
    sink = CH.ClickHouseSink(client_for(fake.svc.url))
    with psycopg.connect(app) as conn:
        batch = next(b for b in sink.batches(conn, {}, replay=True) if b.table == "deviations")
    sink.push(batch)
    sink.push(batch)
    ins = fake.inserts()
    assert len(ins) == 2 and ins[0].q("insert_deduplication_token") == ins[1].q("insert_deduplication_token") == batch.key
    assert len(fake.rows["deviations"]) == len(batch.rows) == len(seed.ids["deviations"])
    # a lost response: the server stored the rows, the client retries the same body and token
    fake.rows.clear()
    fake.tokens.clear()
    fake.commit_then_fail = 1
    sink.push(batch)
    assert len(fake.inserts()) == 4 and len(fake.rows["deviations"]) == len(batch.rows)


def test_incremental_watermark(seed: Seed, pg_url: str, app: str, fake: FakeClickHouse) -> None:
    sink = CH.ClickHouseSink(client_for(fake.svc.url), batch_size=2, lookback_days=0)
    with psycopg.connect(app) as conn:
        rep = sync.run(sink, conn, environ={})
        assert rep.status == "ok" and rep.rows == sum(counts(seed).values())
        marks = sync.load(conn, "clickhouse")["watermarks"]
        assert set(TABLES) <= set(marks) and all(CH.parse_mark(marks[t]) for t in TABLES)
        assert all(k in TABLES or k.endswith(":reconcile") or k == "ch:schedule" for k in marks)
        assert marks["sessions"].endswith("|chs-sbx")                 # newest change, ties broken by id
        n_inserts = len(fake.inserts())
        assert sync.run(sink, conn, environ={}).rows == 0 and len(fake.inserts()) == n_inserts   # nothing new
    with psycopg.connect(pg_url) as conn:                                  # owner: one new row, one update
        new_dev = seed.deviation(conn, "chs-3", "skill:dispatch", "spawn", 0.5, 0.5)
        conn.execute("update sessions set outcome = 'abandoned', outcome_at = now() where session_id = 'chs-2'")
    with psycopg.connect(app) as conn:
        rep = sync.run(sink, conn, environ={})
        assert rep.rows == 2
        assert len(fake.inserts()) == n_inserts + 2  # control-only reconciliation checkpoints do not INSERT
    assert {r["id"] for r in fake.rows["deviations"][-1:]} == {new_dev}
    assert fake.by_id("sessions")["chs-2"]["outcome"] == "abandoned"
    assert len(fake.by_id("deviations")) == len(seed.ids["deviations"])


def test_lookback_resends_silent_updates(seed: Seed, pg_url: str, app: str, fake: FakeClickHouse) -> None:
    dev = seed.ids["deviations"][0]
    with psycopg.connect(app) as conn:
        sync.run(CH.ClickHouseSink(client_for(fake.svc.url), lookback_days=0), conn, environ={})
    with psycopg.connect(pg_url) as conn:                                  # `deviations reref`: no timestamp moves
        conn.execute("update deviations set objective_result = 0.11 where id = %s", (dev,))
    with psycopg.connect(app) as conn:
        assert sync.run(CH.ClickHouseSink(client_for(fake.svc.url), lookback_days=0), conn, environ={}).rows == 0
        assert fake.by_id("deviations")[dev]["objective_result"] != 0.11   # missed without the lookback
        rep = sync.run(CH.ClickHouseSink(client_for(fake.svc.url), lookback_days=7), conn, environ={})
        n_mutable = sum(len(seed.ids[t.name]) for t in CH.TABLES if t.lookback)
        assert rep.rows == n_mutable                                       # append-only tables also recover late commits
    assert fake.by_id("deviations")[dev]["objective_result"] == 0.11
    assert all(len(fake.by_id(t)) == n for t, n in counts(seed).items())


# ---------------------------------------------------------------- replay backfill + verify


def test_replay_backfill_and_verify_parity(seed: Seed, app: str, fake: FakeClickHouse) -> None:
    client = client_for(fake.svc.url)
    with psycopg.connect(app) as conn:
        sync.run(CH.ClickHouseSink(client, lookback_days=0), conn, environ={})
        sync.run(CH.ClickHouseSink(client), conn, replay=True, environ={})        # backfill again: no duplicates
        rep = CH.verify(conn, 3, client=client)
    assert rep.status == "ok" and rep.parity and rep.mismatches() == []
    assert sorted(v[0] for v in rep.counts["sessions"].values()) == [1, 4]          # two days: chs-3 is a day older
    assert all(sb == ch for d in rep.counts.values() for sb, ch in d.values())
    assert sum(sum(v[0] for v in d.values()) for d in rep.counts.values()) == sum(counts(seed).values())
    gone = seed.ids["deviations"][0]
    today = fake.by_id("deviations")[gone]["created_at"][:10]
    fake.rows["deviations"] = [r for r in fake.rows["deviations"] if r["id"] != gone]
    with psycopg.connect(app) as conn:
        bad = CH.verify(conn, 3, client=client)
    n = len(seed.ids["deviations"])
    assert not bad.parity and bad.mismatches() == [("deviations", today, n, n - 1)]
    assert "parity=no" in bad.line() and bad.to_json()["mismatches"] == [["deviations", today, n, n - 1]]
    with psycopg.connect(app) as conn:
        assert CH.verify(conn, 3, client=CH.Client(CH.Config())).status == "unavailable"


# ---------------------------------------------------------------- named queries


def _synced(seed: Seed, app: str, fake: FakeClickHouse) -> CH.ClickHouseRetriever:
    client = client_for(fake.svc.url)
    with psycopg.connect(app) as conn:
        assert sync.run(CH.ClickHouseSink(client), conn, replay=True, environ={}).status == "ok"
    return CH.ClickHouseRetriever(client)


def test_named_queries_send_params_and_return_metrics(seed: Seed, app: str, fake: FakeClickHouse) -> None:
    r = _synced(seed, app, fake)
    for name, q in CH.QUERIES.items():                     # every param the SQL uses is sent; limit never is
        for p in q.defaults:
            assert (f"{{{p}:" in q.sql) == (p != "limit"), (name, p)
    (gap,) = E.gather("rule_gap", {"min_n": "3"}, [r])
    assert gap.status == "ok", gap.detail
    req = fake.svc.requests[-1]
    assert req.body.decode() == CH.RULE_GAP_SQL and req.q("param_days") == "90" and req.q("param_min_n") == "3"
    assert "param_limit" not in req.query and req.q("database") == "amem" and req.q("wait_end_of_query") == "1"
    top, other = gap.refs
    assert top.ref == "ch:q.rule_gap/claude-md:global:explaining/core-rule" and top.summary is None
    assert top.metrics["n_deviations"] == 4 and top.metrics["followed_avg"] == 0.325          # sandbox row left out
    assert top.metrics["objective_avg"] == 0.9 and top.metrics["gap"] == 0.575 and top.metrics["flag"] is True
    assert other.metrics["rule_key"] == "skill:dispatch:spawn" and other.metrics["flag"] is False
    assert other.metrics["objective_n"] == 3 and other.metrics["n_deviations"] == 4
    assert top.subject_refs and all(s.startswith("amem:deviation/") for s in top.subject_refs)
    with psycopg.connect(app) as conn:
        assert all(E.usable(conn, ev) for ev in gap.refs)                  # subjects re-read from Supabase

    (rate,) = E.gather("rule_weekly_rate", {"rule": "claude-md:global:explaining/core-rule", "weeks": "4"}, [r])
    assert fake.svc.requests[-1].q("param_rule") == "claude-md:global:explaining/core-rule"
    assert fake.svc.requests[-1].q("param_weeks") == "4"
    assert rate.status == "ok" and sum(e.metrics["n_deviations"] for e in rate.refs) == 4
    assert all(e.ref.startswith("ch:q.rule_weekly_rate/claude-md:global:explaining/core-rule@") for e in rate.refs)
    assert all(0 < e.metrics["rate"] <= 1 for e in rate.refs)

    (tok,) = E.gather("tokens_by_outcome", {"project": "chtest", "days": "7"}, [r])
    by_ref = {e.ref: e.metrics for e in tok.refs}
    shipped = by_ref["ch:q.tokens_by_outcome/shipped/chat/claude-test-1/high"]
    assert shipped["n_sessions"] == 2 and shipped["tokens_total"] == (1000 + 500 + 3000 + 10) + (3000 + 1500 + 9000 + 10)
    assert shipped["active_total_s"] == 1800.0 and shipped["active_p50_s"] == 1200.0     # sandbox session left out

    (streak,) = E.gather("routine_streaks", {"routine": "chtest-daily"}, [r])
    (ev,) = streak.refs
    assert ev.ref == "ch:q.routine_streaks/chtest-daily" and ev.subject_refs == [f"amem:run/{seed.last_run}"]
    assert ev.metrics == {"routine": "chtest-daily", "n_runs": 3, "fails": 2, "fail_streak": 2, "last_status": "error",
                          "duration_p50_s": 20.0, "duration_p95_s": 40.0}
    (lim,) = E.gather("routine_streaks", {"limit": "1"}, [r])
    assert len(lim.refs) == 1 and lim.refs[0].metrics["routine"] == "chtest-daily"

    (rr,) = E.gather("research_runs", {"task": "sudoku-easy"}, [r])           # Mission 004B: NCM via ClickHouse
    (ev,) = rr.refs
    assert ev.ref == "ch:q.research_runs/ncm-20261016-sudoku-easy" and ev.subject_refs[0].startswith("amem:evidence/")
    assert (ev.metrics["solved_rate"], ev.metrics["cost_usd"], ev.metrics["gpu"]) == (0.995, 0.05, "NVIDIA_L4")
    with psycopg.connect(app) as conn:
        assert E.usable(conn, ev)                                          # the evidence row re-reads in Supabase

    (sk,) = E.gather("skill_outcomes", {"min_n": "1"}, [r])                # Mission 004B: skill x model
    assert fake.svc.requests[-1].body.decode() == CH.SKILL_OUTCOMES_SQL and fake.svc.requests[-1].q("param_min_n") == "1"
    (ev,) = sk.refs
    assert ev.ref == "ch:q.skill_outcomes/dispatch/claude-test-1" and ev.subject_refs == ["amem:session/chs-1"]
    assert (ev.metrics["n_sessions"], ev.metrics["n_done"], ev.metrics["done_rate"]) == (1, 1, 1.0)
    assert E.gather("skill_outcomes", {"min_n": "2"}, [r])[0].refs == []


def test_named_queries_reject_bad_params_before_any_request(seed: Seed, app: str, fake: FakeClickHouse) -> None:
    r = _synced(seed, app, fake)
    before = len(fake.svc.requests)
    for name, params in (("rule_gap", {"rule": "x' OR 1=1 --"}), ("rule_gap", {"days": "0"}),
                         ("rule_weekly_rate", {"weeks": "100"}), ("tokens_by_outcome", {"project": "a b"}),
                         ("routine_streaks", {"sql": "SELECT 1"}), ("rule_gap", {"limit": "1000"})):
        (res,) = E.gather(name, params, [r])
        assert res.status == "bad-params", (name, params)
    assert E.gather("free_sql", {"q": "SELECT * FROM sessions"}, [r])[0].status == "unknown-query"
    assert len(fake.svc.requests) == before
    with pytest.raises(ValueError):
        r.query("rule_gap", {"rule": "x y"})                               # direct calls are checked too


def test_retriever_and_sink_unavailable_when_unconfigured_or_down(app: str) -> None:
    off = CH.ClickHouseRetriever(CH.Client(CH.config({})))
    (res,) = E.gather("rule_gap", {}, [off])
    assert res.status == "unavailable" and "not configured" in res.detail
    assert not CH.ClickHouseSink(CH.Client(CH.config({}))).health().ok
    assert CH.config({"AMEM_CLICKHOUSE_URL": "http://127.0.0.1:1", "AMEM_CLICKHOUSE_DB": "x;y"}).problem
    with MockService() as svc:
        url = svc.url
        svc.route("GET", "/ping", lambda r: (503, "starting"))
        (res,) = E.gather("rule_gap", {}, [CH.ClickHouseRetriever(client_for(url))])
        assert res.status == "unavailable" and "503" in res.detail
    down = client_for(url)                                                 # the server is gone
    (res,) = E.gather("rule_gap", {}, [CH.ClickHouseRetriever(down)])
    assert res.status == "unavailable"
    with psycopg.connect(app) as conn:
        rep = sync.run(CH.ClickHouseSink(down), conn, environ={})
        assert rep.status == "unavailable" and sync.load(conn, "clickhouse").get("watermarks") is None
        assert sync.run(CH.ClickHouseSink(down), conn, environ={"AMEM_SYNC_CLICKHOUSE": "off"}).status == "off"


# ---------------------------------------------------------------- Mission 004C (F1): repo-scoped deviation sources


def _wire(fake: FakeClickHouse) -> str:
    return "\n".join(f"{r.method} {r.path} {json.dumps(r.query)} {json.dumps(r.headers)} "
                     f"{r.body.decode('utf-8', 'replace')}" for r in fake.svc.requests)


def test_team_repo_rules_never_leave_and_own_repo_sources_are_pseudonyms(seed: Seed, pg_url: str, app: str,
                                                                         fake: FakeClickHouse) -> None:
    with psycopg.connect(pg_url) as conn:
        team = seed.session(conn, "chs-team", repo="pipeline-poc", project="pipeline-poc",
                            meta=Jsonb({"team": True, "note": seed.mk("tmeta")}))
        t1 = seed.deviation(conn, team, "claude-md:pipeline-poc", "pipeline-poc-deploy-gate", 0.2, 0.9)
        t2 = seed.deviation(conn, team, "agents-md:pipeline-poc", "pipeline-poc-review", 0.2, 0.9)
        tg = seed.deviation(conn, team, "claude-md:global", "explaining/core-rule", 0.2, 0.9)   # the owner's own rule
        own = [seed.deviation(conn, "chs-1", "claude-md:agent-memory", "own-rule", f, 0.9) for f in (0.1, 0.2, 0.3)]
    with psycopg.connect(app) as conn:
        assert sync.run(CH.ClickHouseSink(client_for(fake.svc.url)), conn, replay=True, environ={}).status == "ok"
    wire = _wire(fake)
    assert "pipeline-poc" not in wire and "agent-memory" not in wire
    devs = fake.by_id("deviations")
    assert (devs[t1]["instruction_source"], devs[t1]["rule_id"]) == ("claude-md:team", None)
    assert (devs[t2]["instruction_source"], devs[t2]["rule_id"]) == ("agents-md:team", None)
    assert (devs[tg]["instruction_source"], devs[tg]["rule_id"]) == ("claude-md:global", "explaining/core-rule")
    pseudo = CH.pseudonym("agent-memory")
    assert pseudo and all((devs[d]["instruction_source"], devs[d]["rule_id"]) == (f"claude-md:{pseudo}", "own-rule")
                          for d in own)
    # the named queries still take the Mac's names: the rule param is translated before it leaves
    r = CH.ClickHouseRetriever(client_for(fake.svc.url))
    (gap,) = E.gather("rule_gap", {"rule": "claude-md:agent-memory:own-rule", "min_n": "3"}, [r])
    assert fake.svc.requests[-1].q("param_rule") == f"claude-md:{pseudo}:own-rule"
    assert gap.status == "ok" and [e.ref for e in gap.refs] == [f"ch:q.rule_gap/claude-md:{pseudo}:own-rule"]
    (rate,) = E.gather("rule_weekly_rate", {"rule": "claude-md:agent-memory:own-rule"}, [r])
    assert fake.svc.requests[-1].q("param_rule") == f"claude-md:{pseudo}:own-rule"
    assert rate.status == "ok" and sum(e.metrics["n_deviations"] for e in rate.refs) == 3
    (again,) = E.gather("rule_gap", {"rule": f"claude-md:{pseudo}:own-rule", "min_n": "3"}, [r])   # an answer's key
    assert [e.ref for e in again.refs] == [e.ref for e in gap.refs]
    (glob,) = E.gather("rule_gap", {"rule": "claude-md:global:explaining/core-rule"}, [r])
    assert fake.svc.requests[-1].q("param_rule") == "claude-md:global:explaining/core-rule" and glob.refs
    assert "pipeline-poc" not in _wire(fake) and "agent-memory" not in _wire(fake)


def test_rule_param_and_repo_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import deviations

    for s in ("claude-md:global", "agents-md:global", "claude-md:x", "agents-md:pipeline-poc", "skill:dispatch",
              "parent-md", "claude-md", "routine:daily", "agent:worker"):
        assert CH.repo_scoped(s) == deviations.repo_scoped(s), s
    assert CH.rule_param("skill:dispatch:spawn") == "skill:dispatch:spawn"
    assert CH.rule_param("claude-md:team:unknown") == "claude-md:team:unknown"
    assert CH.rule_param("agents-md:x") == f"agents-md:{CH.pseudonym('x')}"
    monkeypatch.delenv(CH.PSEUDONYM_ENV)
    with pytest.raises(ValueError, match=CH.PSEUDONYM_ENV):
        CH.rule_param("claude-md:agent-memory:own-rule")
    assert CH.source_value("claude-md:agent-memory") is None and CH.source_value("claude-md:global") == "claude-md:global"


# ---------------------------------------------------------------- Mission 004C (F3): the egress deny list


def test_denied_and_email_shaped_values_never_leave(seed: Seed, pg_url: str, app: str, fake: FakeClickHouse,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(egress.DENY_ENV, "Acme-Secret, globex")
    with psycopg.connect(pg_url) as conn:
        seed.session(conn, "chs-team", repo="pipeline-poc", project="pipeline-poc", meta=Jsonb({"team": True}))
        # the same repo on a session ingestion did not flag, an email-shaped label, a denied row key
        stray = seed.session(conn, "chs-stray", repo="pipeline-poc", project="pipeline-poc", model="vilmer@example.com")
        seed.session(conn, "chs-acme-secret-1")
        seed.run(conn, "pipeline-poc-nightly", "pass", 30, 5)
        seed.run(conn, "globex-sync", "pass", 20, 5)
        seed.ins(conn, "skill_calls", ts=seed.now, skill="acme-secret-helper", invoked_by="me", agent="claude",
                 project="x", source_key=seed.mk("skey"), session_id=stray)
    with psycopg.connect(app) as conn:
        assert sync.run(CH.ClickHouseSink(client_for(fake.svc.url)), conn, replay=True, environ={}).status == "ok"
    wire = _wire(fake).lower()
    for term in ("pipeline-poc", "acme-secret", "globex", "vilmer@example.com"):
        assert term not in wire, term
    sessions = fake.by_id("sessions")
    assert "chs-acme-secret-1" not in sessions                                   # a denied row key: the row is dropped
    assert (sessions["chs-stray"]["repo"], sessions["chs-stray"]["project"], sessions["chs-stray"]["model"]) == \
        (None, None, None)
    assert sessions["chs-team"]["repo"] == "team"
    assert sessions["chs-1"]["repo"] == CH.pseudonym("agent-memory") and sessions["chs-1"]["model"] == "claude-test-1"
    assert sorted(r["routine"] or "-" for r in fake.final("runs")) == ["-", "-", *["chtest-daily"] * 4, "chtest-ok"]
    assert sorted(k["skill"] or "-" for k in fake.final("skill_calls")) == ["-", "dispatch"]
    t = next(t for t in CH.TABLES if t.name == "runs")
    deny = egress.env_terms({egress.DENY_ENV: "acme"})
    assert CH.map_row(t, {"id": "r-acme-1", "routine": "x"}, 1, deny) is None
    assert CH.map_row(t, {"id": "r1", "routine": "ACME-daily", "source": "a@b.co"}, 1, deny)["routine"] is None


# ---------------------------------------------------------------- Mission 004C (F8, F7, F9): unknown ownership


PRIVATE_METAS = {           # sessions of unknown ownership, as sessions_ingest stores them
    "chs-noremote": {},                                                  # no remote, or not a git repo
    "chs-nongithub": {"cwd": "/Users/x/work/tool"},                      # a remote whose owner it could not read
    "chs-otherowner": {"team": False, "remote_owner": "old-handle"},     # an owner that is not an owner account
    # Mission 004C (backfill prep): the owner's account name on another host, and a row stored before the host was
    "chs-gitlab": {"team": False, "remote_owner": "vilmerfrost", "remote_host": "gitlab.example.com"},
    "chs-nohost": {"team": False, "remote_owner": "vilmerfrost"},
}


def _call(seed: Seed, conn: psycopg.Connection, skill: str, project: str, sid: str | None) -> str:
    return seed.ins(conn, "skill_calls", ts=seed.now, skill=skill, invoked_by="me", agent="claude", project=project,
                    source_key=seed.mk("skey"), session_id=sid)


def test_unknown_ownership_ships_private_everywhere(seed: Seed, pg_url: str, app: str, fake: FakeClickHouse) -> None:
    with psycopg.connect(pg_url) as conn:
        priv = [seed.session(conn, sid, repo=f"client-notes-{i}", project=f"client-notes-{i}",
                             meta=Jsonb({**m, "note": seed.mk("pmeta")}))
                for i, (sid, m) in enumerate(PRIVATE_METAS.items())]
        devs = [seed.deviation(conn, sid, f"claude-md:client-notes-{i}", "client-deploy-gate", 0.2, 0.9)
                for i, sid in enumerate(priv)]
        glob = seed.deviation(conn, priv[0], "claude-md:global", "explaining/core-rule", 0.2, 0.9)   # the owner's rule
        own = seed.deviation(conn, "chs-1", "claude-md:agent-memory", "own-rule", 0.2, 0.9)
        calls = {"known": _call(seed, conn, "dispatch", "client-notes-0", priv[0]),         # chs-1 calls it too
                 "repo_skill": _call(seed, conn, "client-notes-release", "client-notes-1", priv[1]),
                 "unmatched": _call(seed, conn, "dispatch", "client-notes-0", None),          # F7
                 "unmatched_repo_skill": _call(seed, conn, "client-notes-helper", "agent-memory", None)}
    sink = CH.ClickHouseSink(client_for(fake.svc.url))
    with psycopg.connect(app) as conn:
        assert sync.run(sink, conn, replay=True, environ={}).status == "ok"
    wire = _wire(fake)
    for name in ("client-notes", "client-deploy-gate", "agent-memory", "old-handle", "ZZLEAK"):
        assert name not in wire, name
    sessions = fake.by_id("sessions")
    assert all((sessions[sid]["project"], sessions[sid]["repo"]) == ("private", "private") for sid in priv)
    assert (sessions["chs-1"]["project"], sessions["chs-1"]["repo"]) == (CH.pseudonym("chtest"), CH.pseudonym("agent-memory"))
    d = fake.by_id("deviations")
    assert all((d[x]["instruction_source"], d[x]["rule_id"]) == ("claude-md:private", None) for x in devs)
    assert (d[glob]["instruction_source"], d[glob]["rule_id"]) == ("claude-md:global", "explaining/core-rule")
    assert (d[own]["instruction_source"], d[own]["rule_id"]) == (f"claude-md:{CH.pseudonym('agent-memory')}", "own-rule")
    k = fake.by_id("skill_calls")
    assert {n: (k[i]["project"], k[i]["skill"]) for n, i in calls.items()} == {
        "known": ("private", "dispatch"), "repo_skill": ("private", None),
        "unmatched": ("private", "dispatch"), "unmatched_repo_skill": ("private", None)}
    (own_call,) = (i for i in seed.ids["skill_calls"] if i not in calls.values())
    assert (k[own_call]["project"], k[own_call]["skill"]) == (CH.pseudonym("chtest"), "dispatch")
    # what `amem sync plan` reports: rows per class
    assert sink.masked["tables"] == {"sessions": {"own": 5, "team": 0, "private": 5},
                                     "deviations": {"own": 10, "team": 0, "private": 6},
                                     "skill_calls": {"own": 1, "team": 0, "private": 4}}
    assert (sink.masked["team"], sink.masked["private"], sink.masked["owner_accounts"]) == (0, 15, ["vilmerfrost"])


def test_a_subagent_session_named_after_its_own_worktree_is_sent(seed: Seed, pg_url: str, app: str,
                                                                  fake: FakeClickHouse) -> None:
    """2026-10-24: sessions parity was off by 8. Subagent sessions that ran in Claude Code's own worktrees
    (`<repo>/.claude/worktrees/agent-<id>`) were stored with repo = `agent-<id>` (sessions_ingest.repo_of took the
    folder after `worktrees`) and no known remote (the worktree is gone), so private. Their repo name became a
    withheld term, which is a substring of the session's own id `<parent>/agent-<id>`: map_row dropped the row
    while `amem sync verify` counted it. A sibling in another worktree (repo `agent-memory`) was sent."""
    parent = "7ff5f480-77aa-496c-9b6a-bd96d4d4c395"
    lost, sibling = f"{parent}/agent-a0846dac3b03cb2f4", f"{parent}/agent-a831fa63c89294d84"
    created = seed.now - timedelta(hours=2)
    with psycopg.connect(pg_url) as conn:
        seed.session(conn, lost, repo="agent-a0846dac3b03cb2f4", project="agent-memory", meta=Jsonb({}),
                     created=created)
        seed.session(conn, sibling, created=created)
    sink = CH.ClickHouseSink(client_for(fake.svc.url))
    with psycopg.connect(app) as conn:
        assert sync.run(sink, conn, replay=True, environ={}).status == "ok"
        rep = CH.verify(conn, 3, client=client_for(fake.svc.url))
    sessions = fake.by_id("sessions")
    assert {lost, sibling} <= set(sessions) and sink.dropped.get("sessions", 0) == 0
    assert (sessions[lost]["project"], sessions[lost]["repo"]) == ("private", "private")      # still masked
    assert rep.parity and rep.mismatches() == []
    assert sessions_ingest.repo_of("/Users/x/Projects/agent-memory/.claude/worktrees/agent-a0846dac3b03cb2f4") == \
        "agent-memory"


def test_plan_contents_previews_the_exact_rows_read_only(seed: Seed, pg_url: str, app: str, fake: FakeClickHouse,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Mission 004C backfill review: `amem sync plan --sink clickhouse --contents` in a read-only session lists each
    row as it will be inserted (without `version`), with its class; a run then inserts exactly those rows."""
    with psycopg.connect(pg_url) as conn:
        seed.session(conn, "chs-gl", meta=Jsonb({"team": False, "remote_owner": "vilmerfrost",
                                                 "remote_host": "gitlab.example.com"}))
        seed.session(conn, "chs-acme-1")                                  # a denied key: dropped, never listed
    monkeypatch.setenv(egress.DENY_ENV, "acme")
    sink = CH.ClickHouseSink(client_for(fake.svc.url))
    with psycopg.connect(app, options="-c default_transaction_read_only=on") as conn:
        out = sync.plan(sink, conn, replay=True, contents=True)
        private = {r["ref"] for r in sync.plan(sink, conn, replay=True, contents=True, cls="private")["records"]}
    assert fake.svc.requests == []
    recs = {r["ref"]: r for r in out["records"]}
    assert out["records_total"] == out["rows"] == len(recs) and "ch:sessions/chs-acme-1" not in recs
    assert recs["ch:sessions/chs-1"]["class"] == "own" and recs["ch:sessions/chs-gl"]["class"] == "private"
    assert recs["ch:sessions/chs-gl"]["payload"]["project"] == "private"
    assert all(r["class"] is None for r in recs.values() if r["table"] == "runs")
    for r in recs.values():
        assert "version" not in r["payload"] and r["sha256"] == hashlib.sha256(json.dumps(
            r["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert out["refused_records"] == [{"table": "sessions", "count": 1,
                                       "reasons": ["key is not a valid id, or names a denied term or an email"]}]
    assert "ch:sessions/chs-gl" in private and "ch:sessions/chs-1" not in private
    with psycopg.connect(app) as conn:
        assert sync.run(CH.ClickHouseSink(client_for(fake.svc.url)), conn, replay=True, environ={}).status == "ok"
    for ref, r in recs.items():
        table, rid = ref[len("ch:"):].split("/", 1)
        assert {k: v for k, v in fake.by_id(table)[rid].items() if k != "version"} == r["payload"], ref


def test_a_project_named_team_or_private_is_pseudonymized_not_taken_for_a_mask(seed: Seed, pg_url: str, app: str,
                                                                             fake: FakeClickHouse) -> None:
    """F9: the mask comes from the row's ownership class (`_own`), never from the value."""
    with psycopg.connect(pg_url) as conn:
        for name in ("team", "private"):
            seed.session(conn, f"chs-named-{name}", repo=name, project=name)        # own sessions named like a mask
        d1 = seed.deviation(conn, "chs-named-team", "claude-md:team", "real-rule", 0.2, 0.9)
        d2 = seed.deviation(conn, "chs-named-private", "agents-md:private", "real-rule", 0.2, 0.9)
        seed.session(conn, "chs-team", repo="pipeline-poc", project="pipeline-poc", meta=Jsonb({"team": True}))
    with psycopg.connect(app) as conn:
        assert sync.run(CH.ClickHouseSink(client_for(fake.svc.url)), conn, replay=True, environ={}).status == "ok"
    s = fake.by_id("sessions")
    for name in ("team", "private"):
        p = CH.pseudonym(name)
        assert p and p.startswith("p_") and (s[f"chs-named-{name}"]["project"], s[f"chs-named-{name}"]["repo"]) == (p, p)
    assert s["chs-team"]["project"] == "team"
    d = fake.by_id("deviations")
    assert (d[d1]["instruction_source"], d[d1]["rule_id"]) == (f"claude-md:{CH.pseudonym('team')}", "real-rule")
    assert (d[d2]["instruction_source"], d[d2]["rule_id"]) == (f"agents-md:{CH.pseudonym('private')}", "real-rule")
    t = next(t for t in CH.TABLES if t.name == "sessions")
    assert CH.map_row(t, {"id": "x", "project": "team", "_own": "own"}, 1)["project"] == CH.pseudonym("team")
    assert CH.map_row(t, {"id": "x", "project": "p_0123456789abcdef", "_own": "private"}, 1)["project"] == "private"
    assert CH.map_row(t, {"id": "x", "project": "agent-memory"}, 1)["project"] == "private"     # no class: private
    assert CH.map_row(t, {"id": "x", "project": "agent-memory", "_own": "weird"}, 1)["project"] == "private"


def test_plan_counts_rows_masked_as_team_and_private(seed: Seed, pg_url: str, app: str) -> None:
    with psycopg.connect(pg_url) as conn:
        seed.session(conn, "chs-team", repo="pipeline-poc", project="pipeline-poc", meta=Jsonb({"team": True}))
        seed.session(conn, "chs-unknown", repo="client-notes", project="client-notes", meta=Jsonb({}))
    offline = CH.Client(CH.Config("https://ch.example.invalid:8443", "amem_writer", "k"))
    with psycopg.connect(app) as conn:
        out = sync.plan(CH.ClickHouseSink(offline), conn, replay=True)
        narrow = sync.plan(CH.ClickHouseSink(offline, owners=()), conn, replay=True)   # AMEM_OWNER_ACCOUNTS narrowed
    assert out["masked"]["tables"]["sessions"] == {"own": 5, "team": 1, "private": 1}
    assert (out["masked"]["team"], out["masked"]["private"], out["masked"]["owner_accounts"]) == (1, 1, ["vilmerfrost"])
    assert narrow["masked"]["tables"]["sessions"] == {"own": 0, "team": 1, "private": 6}
    assert narrow["masked"]["owner_accounts"] == [] and narrow["rows"] == out["rows"]          # masked, not dropped


# ---------------------------------------------------------------- Mission 004C (F4): --tables and --since


def test_sink_narrowed_to_tables_and_since(seed: Seed, pg_url: str, app: str, fake: FakeClickHouse) -> None:
    old = seed.ids["runs"][0]
    with psycopg.connect(pg_url) as conn:
        conn.execute("update runs set created_at = now() - interval '10 days', started_at = now() - interval '10 days', "
                     "finished_at = now() - interval '10 days' where id = %s", (old,))
    since = datetime.now(UTC) - timedelta(days=5)
    sink = CH.ClickHouseSink(client_for(fake.svc.url), tables=CH.tables_named(["skill_calls", "runs"]), since=since)
    assert [t.name for t in sink.tables] == ["runs", "skill_calls"]
    with psycopg.connect(app) as conn:
        rep = sync.run(sink, conn, replay=True, environ={})
        assert rep.status == "ok" and set(sync.load(conn, "clickhouse")["watermarks"]) - {"ch:schedule"} == {"runs", "skill_calls", "runs:reconcile", "skill_calls:reconcile"}
    assert {r.q("query").split()[2] for r in fake.inserts()} == {"amem.runs", "amem.skill_calls"}
    runs = fake.by_id("runs")
    assert old not in runs and len(runs) == len(seed.ids["runs"]) - 1 and len(fake.by_id("skill_calls")) == 1
    with psycopg.connect(app) as conn:
        assert sync.run(CH.ClickHouseSink(client_for(fake.svc.url)), conn, environ={}).status == "ok"
        assert old not in fake.by_id("runs")                                # behind the cursor, as documented
        marks = sync.load(conn, "clickhouse")["watermarks"]
        assert set(TABLES) <= set(marks)
        # a narrowed replay without --since sends the old row, and keeps the other tables' cursors
        sync.run(CH.ClickHouseSink(client_for(fake.svc.url), tables=CH.tables_named(["runs"])), conn, replay=True,
                 environ={})
        after = sync.load(conn, "clickhouse")["watermarks"]
    assert old in fake.by_id("runs") and set(TABLES) <= set(after)
    assert {t: m for t, m in after.items() if t in TABLES and t != "runs"} == {t: m for t, m in marks.items() if t in TABLES and t != "runs"}
    with pytest.raises(ValueError, match="unknown table.*nope"):
        CH.tables_named(["runs", "nope"])


def test_config_from_env_never_shows_the_key() -> None:
    cfg = CH.config({"AMEM_CLICKHOUSE_URL": "http://localhost:8123/", "AMEM_CLICKHOUSE_USER": "u",
                     "AMEM_CLICKHOUSE_KEY": CH_KEY, "AMEM_CLICKHOUSE_LOOKBACK_DAYS": "3"})
    assert cfg.url == "http://localhost:8123" and cfg.db == "amem" and cfg.lookback_days == 3 and not cfg.problem
    assert CH_KEY not in repr(cfg)
    assert CH.config({"AMEM_CLICKHOUSE_LOOKBACK_DAYS": "nope"}).lookback_days == CH.LOOKBACK_DAYS
    mark = (datetime(2026, 10, 16, 9, 0, 0, 123456, tzinfo=UTC), "codex:019a|x")
    assert CH.parse_mark(CH.format_mark(mark)) == mark and CH.parse_mark("garbage") is None


@pytest.mark.parametrize('table', ['rsi_decisions', 'rsi_eval_results'])
def test_append_only_late_commit_is_recovered(seed: Seed, pg_url: str, app: str,
                                             fake: FakeClickHouse, table: str) -> None:
    """An older open transaction commits after a newer exported row."""
    with psycopg.connect(pg_url) as owner:
        art = owner.execute('select hash from rsi_artifacts limit 1').fetchone()[0]
        proposal = owner.execute('select id from rsi_proposals limit 1').fetchone()[0]
    def insert(conn, suffix):
        if table == 'rsi_decisions':
            return str(conn.execute("insert into rsi_decisions(proposal_id,action,artifact_hash,source,verified,expires_at,decided_at) "
                "values(%s,%s,%s,'cli','cli-tty',now()+interval '1 day',%s) returning id",
                (proposal, 'apply' if suffix == 'old' else 'undo', art,
                 datetime.now(UTC)-timedelta(days=90) if suffix == 'old' else datetime.now(UTC))).fetchone()[0])
        return str(conn.execute("insert into rsi_eval_results(artifact_hash,baseline_id,suite,suite_version,verdict,record,record_sha,created_at) "
            "values(%s,'base','late','1','pass','{\"format\":\"rsi-eval/1\"}',%s,%s) returning id",
            (art, hashlib.sha256(suffix.encode()).hexdigest(),
             datetime.now(UTC)-timedelta(days=90) if suffix == 'old' else datetime.now(UTC))).fetchone()[0])
    with psycopg.connect(pg_url) as older:
        missing = insert(older, 'old')
        with psycopg.connect(pg_url) as newer:
            recent = insert(newer, 'new')
        with psycopg.connect(app) as conn:
            sink = CH.ClickHouseSink(client_for(fake.svc.url), tables=CH.tables_named([table]))
            assert sync.run(sink, conn, environ={}).status == 'ok'
            assert recent in fake.by_id(table) and missing not in fake.by_id(table)
        older.commit()
    with psycopg.connect(app) as conn:
        assert sync.run(sink, conn, environ={}).status == 'ok'
    assert missing in fake.by_id(table)


def test_source_deletion_is_tombstoned_and_removed_from_queries(seed: Seed, pg_url: str, app: str,
                                                              fake: FakeClickHouse) -> None:
    target = seed.last_run
    sink = CH.ClickHouseSink(client_for(fake.svc.url), tables=CH.tables_named(['runs']), lookback_days=0)
    with psycopg.connect(app) as conn:
        assert sync.run(sink, conn, environ={}).status == 'ok'
    assert target in fake.by_id('runs')
    with psycopg.connect(pg_url) as owner:
        owner.execute('delete from runs where id=%s', (target,))
    with psycopg.connect(app) as conn:
        assert sync.run(sink, conn, environ={}).status == 'ok'
        assert CH.verify(conn, client=client_for(fake.svc.url), tables=sink.tables).parity
    assert target not in fake.by_id('runs')
    assert any(r['id'] == target and r['_amem_deleted'] == 1 for r in fake.rows['runs'])
    result = E.run_query(CH.ClickHouseRetriever(client_for(fake.svc.url)), 'routine_streaks', {})
    assert result.status == 'ok'
    assert all('_amem_deleted = 0' in q.sql for q in CH.QUERIES.values())
    assert all('_amem_deleted = 0' in q for q in CH.VERIFY_SQL.values())
    with psycopg.connect(pg_url) as owner:
        owner.execute("insert into runs(id,routine,source,status,started_at,created_at) values(%s,'restored','launchd','pass',now()-interval '90 days',now()-interval '90 days')", (target,))
    with psycopg.connect(app) as conn:
        assert sync.run(sink, conn, environ={}).status == 'ok'
    assert fake.by_id('runs')[target]['routine'] == 'restored'
    assert fake.by_id('runs')[target]['_amem_deleted'] == 0


def test_reconciliation_never_deletes_on_failed_source_lookup(fake: FakeClickHouse) -> None:
    from types import SimpleNamespace
    client = client_for(fake.svc.url)
    fake.rows['runs'] = [{'id': 'known-run', 'version': 1, 'created_at': '2026-10-29 00:00:00.000000'}]
    sink = CH.ClickHouseSink(client, tables=CH.tables_named(['runs']))
    def unavailable(*a, **k):
        raise psycopg.OperationalError('source unavailable')
    with pytest.raises(psycopg.OperationalError):
        list(sink._reconcile(SimpleNamespace(execute=unavailable), sink.tables[0], 2, None))
    assert fake.by_id('runs')['known-run'].get('_amem_deleted', 0) == 0 and not fake.inserts()


def test_cloud_old_schema_fails_closed_without_owner_upgrade(fake: FakeClickHouse) -> None:
    original = fake._post
    def old_schema(req):
        if req.body.decode().startswith('SELECT _amem_deleted'):
            return 500, 'UNKNOWN_IDENTIFIER: _amem_deleted'
        return original(req)
    fake.svc.route('POST', '/', old_schema)
    client = CH.Client(CH.Config(fake.svc.url, CH_USER, CH_KEY, cloud=True), retries=0)
    sink = CH.ClickHouseSink(client, tables=CH.tables_named(['runs']))
    state = sink.health()
    assert not state.ok and 'owner upgrade' in state.detail
    assert fake.ddl == [] and fake.inserts() == []
    assert all('ADD COLUMN IF NOT EXISTS _amem_deleted UInt8 DEFAULT 0' in s for s in CH.schema_upgrade_sql())


def test_single_batch_budget_rotates_and_resumes_families(seed: Seed, app: str, fake: FakeClickHouse, monkeypatch) -> None:
    monkeypatch.setenv('AMEM_SYNC_CLICKHOUSE_MAX_ROWS_PER_DAY', '1')
    monkeypatch.setenv('AMEM_SYNC_CLICKHOUSE_MAX_REQUESTS_PER_DAY', '1')
    visited = []
    with psycopg.connect(app) as conn:
        for day in range(40):
            sink = CH.ClickHouseSink(client_for(fake.svc.url), tables=CH.tables_named(['sessions', 'runs']),
                                     lookback_days=0)
            assert sink.batch_size == 1
            report = sync.run(sink, conn, now=seed.now + timedelta(days=day))
            assert report.usage['requests'] <= 1 and report.usage['rows'] <= 1
            visited.append(sync.load(conn, 'clickhouse')['watermarks']['ch:schedule'])
    assert any(p.startswith('sessions') for p in visited[:4])
    assert any(p.startswith('runs') for p in visited[:4])
    assert len(fake.by_id('sessions')) == len(seed.ids['sessions'])
    assert len(fake.by_id('runs')) == len(seed.ids['runs'])


def test_durable_lookback_cursor_continues_after_one_batch(seed: Seed, app: str, fake: FakeClickHouse) -> None:
    table = CH.tables_named(['runs'])[0]
    with psycopg.connect(app) as conn:
        sink = CH.ClickHouseSink(client_for(fake.svc.url), tables=[table], batch_size=1)
        assert sync.run(sink, conn, environ={}).status == 'ok'
        mark = CH.parse_mark(sync.load(conn, 'clickhouse')['watermarks']['runs'])
        first = next(sink._lookback(conn, table, CH.next_version(), mark, seed.now-timedelta(days=7), None))
        second = next(sink._lookback(conn, table, CH.next_version(), mark, seed.now-timedelta(days=7), first.watermark))
    assert first.rows[0]['id'] != second.rows[0]['id']
    assert CH.parse_mark(json.loads(second.watermark)['after']) > CH.parse_mark(json.loads(first.watermark)['after'])
