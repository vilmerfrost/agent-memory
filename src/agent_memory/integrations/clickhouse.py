"""ClickHouse adapter: local analytics over numbers, labels and ids, never text (Mission 004A; blueprint C).

    sink       ClickHouseSink (sync.Sink, name "clickhouse"): Supabase rows -> one table per entity
    retriever  ClickHouseRetriever (source "ch"): four fixed, named, parameterized queries
    verify     per-day row counts per table, Supabase vs ClickHouse, and a parity flag

What may leave. Each table has an explicit allowlist (TABLES). A column that is not listed is never even
selected from Supabase: no titles, summaries, what/why/how, prompts, reasons, refs, paths or jsonb. A label
(model, project, rule id, status ...) must match LABEL_RE (no spaces, at most 128 chars) or it ships as
null, so free text in a label column cannot leak. A row whose key is not a valid ref id is dropped, so
`ch:<table>/<id>` always maps 1:1 to `amem:<entity>/<id>` and no rsi_evidence link is written per row.
Mission 004C: a label, id or pseudonym value that matches the egress deny list (egress.py: AMEM_EGRESS_DENY
plus every team repo and project in Supabase) or is email-shaped ships as null, and a row whose key does
is dropped; a deviation's repo-scoped instruction source is masked (see REPO_PREFIXES).

Shape. Database `amem` (AMEM_CLICKHOUSE_DB), one `ReplacingMergeTree(version)` table per entity,
`ORDER BY id`, `SETTINGS non_replicated_deduplication_window = 1000` (a plain MergeTree keeps no insert
dedup log without it, so insert_deduplication_token would do nothing). No PARTITION BY: one INSERT is one
block, so one token covers the whole batch. Reads use FINAL.

ClickHouse Cloud (Mission 004B; `cloud` is on for a `*.clickhouse.cloud` host or AMEM_CLICKHOUSE_CLOUD=1):
- The DDL has no SETTINGS clause. Cloud turns ReplacingMergeTree into SharedReplacingMergeTree, which is
  replicated, so it keeps an insert dedup log by default and the non-replicated setting does not apply.
- No auto-schema. amem writes as a least-privilege user (SELECT, INSERT on the database only), so it cannot
  run DDL. Two separate owner steps in the Cloud SQL console: `amem sync schema --sink clickhouse --cloud
  --part admin` (database and tables), then `--part writer --hash-from-env` (the user, identified by the
  SHA-256 of AMEM_CLICKHOUSE_KEY, so the console never sees the password). `amem sync grants --sink
  clickhouse` then probes, as the writer, that it can read and insert and nothing else.
- An idle service wakes on the first request; the docs say "a few tens of seconds" and suggest a 1-2 min
  timeout: the health ping waits up to WAKE_TIMEOUT_S and inserts get a longer timeout. A stopped (not idle)
  service fails the ping, so the run is `unavailable` and the cursor stays.
- Plain http is refused for any host but loopback. Cloud listens on HTTPS 8443.
Every insert sends async_insert=0, so the dedup token applies to the insert itself (Cloud 26.4 turned
async inserts on by default).

Write path. One HTTP POST per batch:

    POST {url}/?query=INSERT INTO amem.<t> FORMAT JSONEachRow&insert_deduplication_token=<sha256(table, body)>
    X-ClickHouse-User / X-ClickHouse-Key headers; body: one JSON object per line

The same batch sent twice (an HTTP retry after a lost response) is stored once. `version` is the extraction
time in epoch ms (monotonic in this process), so the newest extraction of a row wins in FINAL.

Cursor (the sync watermark, owned here): `<iso ts>|<id>` per table, over `(changed, id)` in that order,
where `changed` is the newest timestamp the row carries (sessions: created, ended, last activity, tokens,
outcome; runs: created, finished; proposals: created, decided, promoted, reverted; else the creation time).
Some columns change without any timestamp (deviations.objective_result / rule_id on `deviations reref`,
jev verdicts, skill_calls.session_id on a late match), and a row can commit after a later row's timestamp.
So every incremental run also re-sends the rows at or behind the watermark whose `changed` is inside a
lookback window (AMEM_CLICKHOUSE_LOOKBACK_DAYS, default 7, 0 = off). ReplacingMergeTree keeps the newest
version, so a re-send is harmless. Silent updates older than the window heal with `--replay` (a backfill).
The append-only tables (rsi_decisions, rsi_eval_results) have no lookback.

Reads. Only the named queries in QUERIES: fixed SQL with ClickHouse query parameters (`{name:Type}` in the
SQL, values as `param_<name>` URL parameters, each checked first by the core NamedQuery regex). `limit` is
applied here, never interpolated. Results are EvidenceRef metrics (`ch:q.<query>/<key>`, summary None).

Checked against the official docs on 2026-10-16:
- HTTP interface: POST for INSERT with the query in the URL and the data in the body; X-ClickHouse-User /
  X-ClickHouse-Key headers; the `database` URL parameter; any setting as a URL parameter; GET /ping answers
  "Ok."; wait_end_of_query=1 buffers the whole response, so a query error gets a proper status code.
  https://clickhouse.com/docs/interfaces/http
- Query parameters: `{<name>: <datatype>}` placeholders, values as `param_<name>` URL parameters.
  https://clickhouse.com/docs/sql-reference/syntax (section "Defining and Using Query Parameters")
- insert_deduplication_token takes priority over the data hash; a non-replicated MergeTree deduplicates
  nothing unless non_replicated_deduplication_window > 0 (default 0); the window counts inserts.
  https://clickhouse.com/docs/guides/developer/deduplicating-inserts-on-retries
- ReplacingMergeTree(ver): per ORDER BY key the highest version survives (a tie keeps the last insert);
  merges are eventual, so read with FINAL.
  https://clickhouse.com/docs/engines/table-engines/mergetree-family/replacingmergetree
- quantileExact(0.5) over 0..9 returns 5 (the mock in the tests uses that rule).
  https://clickhouse.com/docs/sql-reference/aggregate-functions/reference/quantileexact
Tested against a local mock only (tests/test_integrations_clickhouse.py): the SQL has not run on a real server.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from agent_memory import config as amem_config
from agent_memory.integrations import egress, http, refs
from agent_memory.integrations.evidence import EvidenceRef, Health, NamedQuery
from agent_memory.integrations.sync import Batch

DEFAULT_DB = "amem"
DEDUP_WINDOW = 1000        # inserts remembered per table for insert_deduplication_token
WAKE_TIMEOUT_S = 120.0     # Cloud: an idle service wakes on the first request ("a few tens of seconds")
WRITER_USER = "amem_writer"
BATCH_SIZE = 500
LOOKBACK_DAYS = 7
FETCH_LIMIT = 1000         # the fixed LIMIT in every named query; the `limit` param cuts further here
DB_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+=\[\]-]{0,127}")
_NOT_REF = re.compile(r"[^A-Za-z0-9._:/@-]")


# ---------------------------------------------------------------- config and HTTP client


@dataclass(frozen=True)
class Config:
    url: str = ""
    user: str = ""
    key: str = field(default="", repr=False)
    db: str = DEFAULT_DB
    lookback_days: int = LOOKBACK_DAYS
    cloud: bool = False

    @property
    def problem(self) -> str:
        if not self.url:
            return "not configured (AMEM_CLICKHOUSE_URL)"
        if not DB_RE.fullmatch(self.db):
            return "bad AMEM_CLICKHOUSE_DB"
        if not self.url.startswith("https://") and _host(self.url) not in _LOOPBACK:
            return "refusing plain http to a remote host (use https)"
        return ""


_LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _host(url: str) -> str:
    rest = url.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1]
    if rest.startswith("["):
        return rest[: rest.find("]") + 1]
    return rest.rsplit(":", 1)[0].lower()


def config(environ: Mapping[str, str] | None = None) -> Config:
    """AMEM_CLICKHOUSE_URL / _USER / _KEY / _DB / _LOOKBACK_DAYS. No URL: unconfigured (health unavailable)."""
    if environ is None:
        amem_config.load_env()
        environ = os.environ

    def get(name: str) -> str:
        return (environ.get(name) or "").strip()

    try:
        lookback = int(get("AMEM_CLICKHOUSE_LOOKBACK_DAYS") or LOOKBACK_DAYS)
    except ValueError:
        lookback = LOOKBACK_DAYS
    url = get("AMEM_CLICKHOUSE_URL").rstrip("/")
    cloud = _host(url).endswith(".clickhouse.cloud") or get("AMEM_CLICKHOUSE_CLOUD") == "1"
    return Config(url, get("AMEM_CLICKHOUSE_USER"), get("AMEM_CLICKHOUSE_KEY"),
                  get("AMEM_CLICKHOUSE_DB") or DEFAULT_DB, max(0, min(lookback, 90)), cloud)


class Client:
    """The ClickHouse HTTP interface: ping, DDL, idempotent inserts and parameterized selects."""

    def __init__(self, cfg: Config | None = None, *, timeout: float | None = None, retries: int = 2,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg if cfg is not None else config()
        self.timeout = timeout if timeout is not None else (30.0 if self.cfg.cloud else 10.0)
        self.retries, self.sleep = retries, sleep

    def _check(self) -> None:
        if self.cfg.problem:
            raise http.HttpError(None, self.cfg.problem)

    def _post(self, params: Mapping[str, str], body: bytes) -> http.Response:
        self._check()
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if self.cfg.user:
            headers["X-ClickHouse-User"] = self.cfg.user
        if self.cfg.key:
            headers["X-ClickHouse-Key"] = self.cfg.key
        url = f"{self.cfg.url}/" + (f"?{urlencode(params, quote_via=quote)}" if params else "")
        return http.request("POST", url, headers=headers, body=body, timeout=self.timeout, retries=self.retries,
                            sleep=self.sleep)

    def ping(self) -> None:
        self._check()
        wait = WAKE_TIMEOUT_S if self.cfg.cloud else min(self.timeout, 3.0)     # the ping is what wakes Cloud
        r = http.request("GET", f"{self.cfg.url}/ping", timeout=wait, retries=0, sleep=self.sleep)
        if not r.text().startswith("Ok"):
            raise http.HttpError(r.status, "unexpected /ping answer")

    def command(self, statement: str) -> None:
        self._post({}, statement.encode("utf-8"))

    def insert(self, table: str, body: bytes, token: str) -> None:
        if table not in TABLE_NAMES:
            raise ValueError(f"not a ClickHouse table: {table!r}")
        self._post({"query": f"INSERT INTO {self.cfg.db}.{table} FORMAT JSONEachRow",
                    "insert_deduplication_token": token, "async_insert": "0"}, body)

    def select(self, statement: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        p = {"database": self.cfg.db, "wait_end_of_query": "1", "output_format_json_quote_64bit_integers": "0"}
        p.update({f"param_{k}": v for k, v in params.items()})
        r = self._post(p, statement.encode("utf-8"))
        return [json.loads(line) for line in r.text().splitlines() if line.strip()]


# ---------------------------------------------------------------- tables and allowlists


@dataclass(frozen=True)
class Col:
    name: str           # the ClickHouse column (and the alias in the Supabase select)
    kind: str           # id | label | ts | int | float | bool
    expr: str = ""      # Supabase SQL expression; default: the column of the same name


@dataclass(frozen=True)
class Table:
    name: str                   # Supabase table = ClickHouse table = cursor key
    key: str                    # Supabase key column, shipped as `id`
    day: str                    # the column verify counts per day
    changed: str                # SQL: when the row last changed (the cursor)
    cols: tuple[Col, ...]       # the allowlist; nothing else is selected
    lookback: bool = True       # False for append-only tables
    source: str = ""            # the Supabase table when it differs from `name` (Mission 004B: research_runs)
    where: str = ""             # a fixed SQL filter on the source rows

    @property
    def src(self) -> str:
        return self.source or self.name

    @property
    def allowlist(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.cols)


def _cols(spec: str, **exprs: str) -> tuple[Col, ...]:
    """'kind:a,b kind:c' -> Cols; exprs maps a column to its Supabase expression."""
    out = []
    for group in spec.split():
        kind, names = group.split(":")
        out += [Col(n, kind, exprs.get(n, "")) for n in names.split(",")]
    return tuple(out)


# Project and repo names are not sent as such (Mission 004B: ClickHouse Cloud is a third party). A team row
# (an employer repo: sessions.meta.team, see sessions_ingest) sends the literal 'team'; any other value becomes
# a keyed pseudonym (HMAC-SHA256 with AMEM_PSEUDONYM_KEY, which never leaves the Mac). No key: null.
_TEAM_MASK = "case when meta ->> 'team' = 'true' then 'team' else {col} end"
_SKILL_TEAM_MASK = ("case when exists (select 1 from sessions s where s.session_id = skill_calls.session_id "
                    "and s.meta ->> 'team' = 'true') then 'team' else project end")
PSEUDONYM_ENV = "AMEM_PSEUDONYM_KEY"


def pseudonym(value: str, environ: Mapping[str, str] | None = None) -> str | None:
    if value == "team":
        return "team"
    key = ((os.environ if environ is None else environ).get(PSEUDONYM_ENV) or "").strip()
    if len(key) < 32:
        return None
    return "p_" + hmac.new(key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


# Mission 004C (F1): a deviation's instruction source names a repo when it is a repo's own file
# (`claude-md:<repo>`, `agents-md:<repo>`; deviations.repo_scoped), and its rule_id is a slug of a heading in
# that file. A team row sends `<prefix>:team` and no rule_id; any other repo-scoped source sends
# `<prefix>:<pseudonym of the repo>` (no key: null). Global, skill, agent and routine sources stay as they are.
REPO_PREFIXES = ("claude-md", "agents-md")
_GLOBAL_SOURCES = frozenset(f"{p}:global" for p in REPO_PREFIXES)
_PSEUDO_RE = re.compile(r"p_[0-9a-f]{16}")


def repo_scoped(source: str) -> bool:
    """deviations.repo_scoped (that module imports the whole ingest stack, so the rule is repeated here)."""
    return source.startswith(tuple(f"{p}:" for p in REPO_PREFIXES)) and source not in _GLOBAL_SOURCES


_DEV_REPO_SCOPED = ("((starts_with(instruction_source, 'claude-md:') or starts_with(instruction_source, 'agents-md:')) "
                    "and instruction_source not in ('claude-md:global', 'agents-md:global'))")
_DEV_TEAM = ("(meta ->> 'team' = 'true' or exists (select 1 from sessions s where s.session_id = deviations.session_id "
             "and s.meta ->> 'team' = 'true'))")
_DEV_SOURCE = (f"case when {_DEV_REPO_SCOPED} and {_DEV_TEAM} then split_part(instruction_source, ':', 1) || ':team' "
               "else instruction_source end")
_DEV_RULE = f"case when {_DEV_REPO_SCOPED} and {_DEV_TEAM} then null else rule_id end"


def source_value(source: str, environ: Mapping[str, str] | None = None) -> str | None:
    """An instruction source as ClickHouse holds it: a repo-scoped source's repo becomes its pseudonym."""
    if not repo_scoped(source):
        return source
    prefix, repo = source.split(":", 1)
    if repo == "team" or _PSEUDO_RE.fullmatch(repo):
        return source
    p = pseudonym(repo, environ)
    return f"{prefix}:{p}" if p else None


def rule_param(rule: str, environ: Mapping[str, str] | None = None) -> str:
    """A rule key (`<source>:<rule_id>`) as ClickHouse holds it, so `rule` filters keep working on the Mac's
    names: `claude-md:agent-memory:x` -> `claude-md:p_...:x`. A key that already holds a pseudonym (from an
    earlier answer), `team` or `global` is sent as it is. Raises ValueError without a pseudonym key."""
    prefix, sep, rest = rule.partition(":")
    if not sep or prefix not in REPO_PREFIXES:
        return rule
    repo, sep2, tail = rest.partition(":")
    if repo in ("global", "team") or _PSEUDO_RE.fullmatch(repo):
        return rule
    p = pseudonym(repo, environ)
    if p is None:
        raise ValueError(f"{PSEUDONYM_ENV} is not set: cannot filter by a repo's rule")
    return f"{prefix}:{p}{sep2}{tail}"


_NCM_FLOATS = {m: m for m in ("solved_rate", "cell_rate", "valid_rate", "rouge1", "rouge2", "gen_ppl",
                                "sacrebleu_cased", "sacrebleu_lower", "chrf", "length_ratio", "unigram_entropy")}
_NCM_FLOATS.update({"rouge_l": "rougeL", "cost_usd": "cost_usd", "gpu_minutes": "cost_gpu_minutes",
                    "runtime_s": "runtime_s"})


def _num_expr(key: str) -> str:
    return f"case when jsonb_typeof(metrics -> '{key}') = 'number' then (metrics ->> '{key}')::float8 end"


TABLES: tuple[Table, ...] = (
    Table("sessions", "session_id", "created_at",
          "greatest(created_at, ended_at, last_activity_at, tokens_updated_at, outcome_at)",
          _cols("id:id label:kind,tool,env,category,model,effort,outcome pseudo:project,repo id:experiment_id "
                "ts:created_at,started_at,ended_at,last_activity_at,outcome_at "
                "int:transcript_bytes,turns,tokens_input,tokens_output,tokens_cache_read,tokens_cache_creation "
                "float:duration_seconds,active_seconds,api_seconds,tool_seconds int:lines_added,lines_removed",
                id="session_id", project=_TEAM_MASK.format(col="project"), repo=_TEAM_MASK.format(col="repo"))),
    Table("runs", "id", "created_at", "greatest(created_at, finished_at)",
          _cols("id:id label:routine,source,status ts:started_at,finished_at,created_at float:duration_s",
                duration_s="extract(epoch from finished_at - started_at)")),
    Table("deviations", "id", "created_at", "created_at",
          _cols("id:id,session_id source:instruction_source label:rule_id id:instruction_sha int:turn "
                "float:followed_grade,result_grade,objective_result label:result_outcome,jev_verdict "
                "float:jev_confidence label:jev_mode,writer_model ts:created_at",
                instruction_source=_DEV_SOURCE, rule_id=_DEV_RULE)),
    Table("scores", "id", "created_at", "created_at",
          _cols("id:id,run_id label:question,kind float:value_num,confidence label:jev_mode ts:created_at")),
    Table("skill_calls", "id", "created_at", "created_at",
          _cols("id:id ts:ts label:skill,invoked_by,agent pseudo:project bool:backfill id:session_id ts:created_at",
                project=_SKILL_TEAM_MASK)),
    Table("rsi_proposals", "id", "created_at", "greatest(created_at, decided_at, promoted_at, reverted_at)",
          _cols("id:id,artifact_hash,experiment_id,candidate_id label:status float:confidence "
                "ts:created_at,expires_at,decided_at,promoted_at,reverted_at")),
    Table("rsi_decisions", "id", "decided_at", "decided_at",
          _cols("id:id,proposal_id label:action id:artifact_hash label:source,verified ts:decided_at,expires_at"),
          lookback=False),
    # Mission 004B: NCM research runs (public benchmark metrics) from their rsi_evidence rows. ch:research_runs/<id>
    # maps to amem:evidence/<id>. Only the contract's metric names, cost, hardware and pins; never the summary.
    Table("research_runs", "id", "observed_at", "observed_at",
          _cols("id:id label:run_id,task,status,gpu,provider,repo_commit,checkpoint_revision,dataset_revision "
                "float:" + ",".join(_NCM_FLOATS) + " ts:observed_at",
                run_id="substring(external_ref from 9)", task="metrics ->> 'task'", status="metrics ->> 'status'",
                gpu="replace(metrics ->> 'hw_gpu', ' ', '_')", provider="metrics ->> 'provider'", repo_commit="metrics ->> 'repo_commit'",
                checkpoint_revision="metrics ->> 'checkpoint_revision'",
                dataset_revision="metrics ->> 'dataset_revision'",
                **{k: _num_expr(src) for k, src in _NCM_FLOATS.items()}),
          source="rsi_evidence", where="system = 'ncm'"),
    Table("rsi_eval_results", "id", "created_at", "created_at",
          _cols("id:id,artifact_hash,experiment_id,baseline_id label:suite,suite_version,model,effort,verdict "
                "id:record_sha ts:created_at"),
          lookback=False),
)
TABLE_NAMES = frozenset(t.name for t in TABLES)
_TABLE = {t.name: t for t in TABLES}

_CH_TYPES = {"id": "Nullable(String)", "label": "LowCardinality(Nullable(String))",
             "pseudo": "LowCardinality(Nullable(String))", "source": "LowCardinality(Nullable(String))",
             "ts": "Nullable(DateTime64(6, 'UTC'))", "int": "Nullable(Int64)", "float": "Nullable(Float64)",
             "bool": "Nullable(Bool)"}


def _ch_type(t: Table, c: Col) -> str:
    if c.name == "id":
        return "String"
    if c.name == t.day:
        return "DateTime64(6, 'UTC')"
    return _CH_TYPES[c.kind]


def schema_sql(db: str = DEFAULT_DB, *, cloud: bool = False) -> list[str]:
    """One statement per item (the HTTP interface runs one statement per request)."""
    if not DB_RE.fullmatch(db):
        raise ValueError(f"bad database name: {db!r}")
    out = [f"CREATE DATABASE IF NOT EXISTS {db}"]
    settings = "" if cloud else f"\nSETTINGS non_replicated_deduplication_window = {DEDUP_WINDOW}"
    for t in TABLES:
        cols = ",\n    ".join(f"{c.name} {_ch_type(t, c)}" for c in t.cols)
        out.append(f"CREATE TABLE IF NOT EXISTS {db}.{t.name} (\n    {cols},\n    version UInt64\n)\n"
                   f"ENGINE = ReplacingMergeTree(version)\nORDER BY id{settings}")
    return out


PASSWORD_RULE = "12+ characters with an uppercase letter, a lowercase letter, a digit and a special character"
KEY_ENV = "AMEM_CLICKHOUSE_KEY"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def password_ok(password: str) -> bool:
    """ClickHouse Cloud's password rule, checked here before hashing (the password is never printed)."""
    return (len(password) >= 12 and any(c.isupper() for c in password) and any(c.islower() for c in password)
            and any(c.isdigit() for c in password) and any(not c.isalnum() for c in password))


def password_sha256(environ: Mapping[str, str] | None = None) -> str:
    """SHA-256 hex of AMEM_CLICKHOUSE_KEY (the env the app loads itself). Raises ValueError without ever
    echoing the value."""
    amem_config.load_env()
    key = ((os.environ if environ is None else environ).get(KEY_ENV) or "").strip()
    if not key:
        raise ValueError(f"{KEY_ENV} is not set")
    if not password_ok(key):
        raise ValueError(f"{KEY_ENV} does not meet the Cloud rule: {PASSWORD_RULE}")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def writer_sql(db: str = DEFAULT_DB, user: str = WRITER_USER, *, sha256_hex: str | None = None) -> list[str]:
    """The least-privilege user amem writes and reads as: SELECT and INSERT on `db` only. No CREATE, ALTER,
    DROP, TRUNCATE, user or grant management, and no other database, so a stolen key cannot change the
    schema or read anything else; no `readonly` setting, which would block INSERT. `IDENTIFIED WITH
    sha256_hash`: the console and its query log see the hash, never the password. Without `sha256_hex`
    the statement carries a placeholder."""
    if not DB_RE.fullmatch(db) or not DB_RE.fullmatch(user):
        raise ValueError("bad database or user name")
    if sha256_hex is not None and not _SHA256_RE.fullmatch(sha256_hex):
        raise ValueError("sha256_hex is 64 lowercase hex characters")
    secret = sha256_hex or "<SHA-256 hex of the password>"
    return [f"CREATE USER IF NOT EXISTS {user} IDENTIFIED WITH sha256_hash BY '{secret}' DEFAULT DATABASE {db}",
            f"GRANT SELECT, INSERT ON {db}.* TO {user}"]


def _denied(exc: http.HttpError) -> bool:
    return "ACCESS_DENIED" in str(exc) or "Code: 497" in str(exc)


def check_grants(client: Client) -> list[dict[str, Any]]:
    """G-CH2 verification, run as the writer over HTTP. Each probe is harmless even if it unexpectedly
    works: the insert copies zero rows, the ALTER deletes `WHERE 0`, the probe table uses the Null engine,
    the probe user has `HOST NONE` and a random hash. One row per probe: {probe, expect, got, ok}."""
    db, user, t = client.cfg.db, client.cfg.user, TABLES[0].name
    tag = os.urandom(4).hex()
    out: list[dict[str, Any]] = []

    def probe(name: str, expect: str, statement: str,
              judge: Callable[[list[dict[str, Any]]], bool] | None = None) -> None:
        rows: list[dict[str, Any]] = []
        try:
            if "FORMAT JSONEachRow" in statement:
                rows = client.select(statement, {})
            else:
                client.command(statement)
            got = "allowed"
            ok = expect == "allowed" and (judge is None or judge(rows))
            if judge is not None:
                got = f"allowed: {json.dumps(rows, sort_keys=True)[:200]}"
        except http.HttpError as exc:
            got = "denied" if _denied(exc) else f"error: {str(exc)[:160]}"
            ok = got == expect
        out.append({"probe": name, "expect": expect, "got": got, "ok": ok})

    own = {f"GRANT SELECT, INSERT ON {db}.* TO {user}"}
    probe("grants", "allowed", "SHOW GRANTS FORMAT JSONEachRow",
          lambda rows: {next(iter(r.values()), "") for r in rows} == own)
    probe("select", "allowed", f"SELECT count() AS n FROM {db}.{t} FORMAT JSONEachRow")
    probe("insert", "allowed", f"INSERT INTO {db}.{t} SELECT * FROM {db}.{t} WHERE 0")
    probe("databases", "allowed", "SELECT name FROM system.databases FORMAT JSONEachRow",
          lambda rows: {r.get("name") for r in rows} <= {db, "system", "information_schema", "INFORMATION_SCHEMA"})
    probe("create database", "denied", f"CREATE DATABASE {db}_probe_{tag}")
    probe("create table", "denied", f"CREATE TABLE {db}.probe_{tag} (a Int8) ENGINE = Null")
    probe("alter", "denied", f"ALTER TABLE {db}.{t} DELETE WHERE 0")
    probe("create user", "denied",
          f"CREATE USER probe_{tag} IDENTIFIED WITH sha256_hash BY '{os.urandom(32).hex()}' HOST NONE")
    probe("grant", "denied", f"GRANT SELECT ON {db}.* TO {user}")
    probe("users", "denied", "SELECT count() AS n FROM system.users FORMAT JSONEachRow")
    probe("query log", "denied", "SELECT count() AS n FROM system.query_log FORMAT JSONEachRow")
    return out


def ensure_schema(client: Client) -> int:
    stmts = schema_sql(client.cfg.db, cloud=client.cfg.cloud)
    for s in stmts:
        client.command(s)
    return len(stmts)


def _is_id(s: str) -> bool:
    # The ref grammar decides what an id may hold, so ch:<table>/<id> and amem:<entity>/<id> stay valid refs.
    return refs.is_ref(f"ch:x/{s}")


_TEXT_KINDS = frozenset({"label", "pseudo", "source", "id"})


def _value(kind: str, v: Any, deny: egress.Deny = egress.NONE) -> Any:
    if v is None:
        return None
    if kind in _TEXT_KINDS:             # Mission 004C (F3): a denied or email-shaped value never leaves, as is
        s = str(v)                      # or as a pseudonym: it ships as null
        if deny.match(s) or egress.email_like(s):
            return None
    if kind == "label":
        return s if LABEL_RE.fullmatch(s) else None
    if kind == "pseudo":                # label-shaped only: free text stays null, as for labels
        return pseudonym(s) if LABEL_RE.fullmatch(s) else None
    if kind == "source":                # a label whose repo part becomes a pseudonym (F1)
        return source_value(s) if LABEL_RE.fullmatch(s) else None
    if kind == "id":
        return s if _is_id(s) else None
    if kind == "ts":
        return v.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    if kind == "int":
        return int(v)
    if kind == "float":
        f = float(v)
        return f if math.isfinite(f) else None
    if kind == "bool":
        return bool(v)
    raise ValueError(f"unknown column kind {kind!r}")


def map_row(t: Table, raw: Mapping[str, Any], version: int, deny: egress.Deny = egress.NONE) -> dict[str, Any] | None:
    """The allowlist mapper: only t.cols, each coerced by kind; None when the key is not a valid id, or is
    denied or email-shaped (egress)."""
    out = {c.name: _value(c.kind, raw.get(c.name), deny) for c in t.cols}
    if not out.get("id"):
        return None
    out["version"] = version
    return out


def body_of(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for r in rows)


def token_of(table: str, body: bytes) -> str:
    return hashlib.sha256(table.encode("utf-8") + b"\n" + body).hexdigest()


_version_lock = threading.Lock()
_last_version = 0


def next_version() -> int:
    """Extraction version: epoch ms, strictly increasing within this process."""
    global _last_version
    with _version_lock:
        _last_version = max(time.time_ns() // 1_000_000, _last_version + 1)
        return _last_version


Mark = tuple[datetime, str]


def format_mark(mark: Mark) -> str:
    return f"{mark[0].astimezone(UTC).isoformat()}|{mark[1]}"


def parse_mark(text: str | None) -> Mark | None:
    """None for no (or an unreadable) watermark: the table is sent again from the start, which is idempotent."""
    if not text or "|" not in text:
        return None
    ts, _, key = text.partition("|")
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)), key


# ---------------------------------------------------------------- the sink


class ClickHouseSink:
    """sync.Sink for ClickHouse. Batches hold allowlisted rows only; batch.evidence stays empty."""

    name = "clickhouse"

    def __init__(self, client: Client | None = None, *, batch_size: int = BATCH_SIZE, lookback_days: int | None = None,
                 now: Callable[[], datetime] | None = None, tables: Sequence[Table] = TABLES, auto_schema: bool = True):
        self.client = client if client is not None else Client()
        self.batch_size = batch_size
        self.lookback_days = self.client.cfg.lookback_days if lookback_days is None else lookback_days
        self._now = now or (lambda: datetime.now(UTC))
        self.tables = tuple(tables)
        self.auto_schema = auto_schema and not self.client.cfg.cloud      # Cloud: the owner runs the DDL once
        self._schema_ready = False
        self.deny = egress.NONE         # the egress deny list, read at the start of every batches() call

    def health(self) -> Health:
        if self.client.cfg.problem:
            return Health(False, self.client.cfg.problem)
        try:
            self.client.ping()
            if self.auto_schema and not self._schema_ready:
                ensure_schema(self.client)          # IF NOT EXISTS: cheap and idempotent
                self._schema_ready = True
        except http.HttpError as exc:
            return Health(False, str(exc)[:200])
        return Health(True)

    def batches(self, conn: psycopg.Connection, watermarks: Mapping[str, str], *, replay: bool) -> Iterator[Batch]:
        version = next_version()
        self.deny = egress.load(conn)
        since = None if replay or self.lookback_days <= 0 else self._now() - timedelta(days=self.lookback_days)
        for t in self.tables:
            mark = None if replay else parse_mark(watermarks.get(t.name))
            last = mark
            for rows in self._pages(conn, t, after=mark):
                last = (rows[-1]["_changed"], rows[-1]["_key"])
                yield self._batch(t, rows, version, last)
            if since is not None and t.lookback and mark is not None:
                # rows sent by an earlier run that may have changed without a timestamp (see module doc)
                for rows in self._pages(conn, t, since=since, upto=mark):
                    yield self._batch(t, rows, version, last or mark)

    def _pages(self, conn: psycopg.Connection, t: Table, *, after: Mark | None = None, since: datetime | None = None,
               upto: Mark | None = None) -> Iterator[list[dict[str, Any]]]:
        changed = sql.SQL(t.changed)
        key = sql.SQL('{}::text collate "C"').format(sql.Identifier(t.key))
        cols = sql.SQL(", ").join(
            sql.SQL("{} as {}").format(sql.SQL(c.expr) if c.expr else sql.Identifier(c.name), sql.Identifier(c.name))
            for c in t.cols)
        while True:
            conds: list[sql.Composable] = [sql.SQL(t.where)] if t.where else []
            args: list[Any] = []
            if after is not None:
                conds.append(sql.SQL("({}, {}) > (%s, %s)").format(changed, key))
                args += [after[0], after[1]]
            if since is not None:
                conds.append(sql.SQL("{} >= %s").format(changed))
                args.append(since)
            if upto is not None:
                conds.append(sql.SQL("({}, {}) <= (%s, %s)").format(changed, key))
                args += [upto[0], upto[1]]
            where = sql.SQL(" where ") + sql.SQL(" and ").join(conds) if conds else sql.SQL("")
            q = sql.SQL("select {cols}, {changed} as _changed, {key} as _key from {table}{where} "
                        "order by _changed, _key limit %s").format(
                cols=cols, changed=changed, key=key, table=sql.Identifier(t.src), where=where)
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(q, [*args, self.batch_size]).fetchall()
            if not rows:
                return
            yield rows
            if len(rows) < self.batch_size:
                return
            after = (rows[-1]["_changed"], rows[-1]["_key"])

    def _batch(self, t: Table, rows: Sequence[Mapping[str, Any]], version: int, mark: Mark) -> Batch:
        mapped = [m for m in (map_row(t, r, version, self.deny) for r in rows) if m is not None]
        return Batch(t.name, mapped, format_mark(mark), key=token_of(t.name, body_of(mapped)) if mapped else "")

    def split(self, batch: Batch) -> list[Batch]:
        """One row per batch, each with its own dedup token (sync dead-letters only the refused rows)."""
        return [Batch(batch.table, [r], batch.watermark, [], key="") for r in batch.rows]

    def push(self, batch: Batch) -> None:
        if not batch.rows:
            return
        body = body_of(batch.rows)
        self.client.insert(batch.table, body, batch.key or token_of(batch.table, body))


# ---------------------------------------------------------------- the retriever: four named queries

_RULE_P = r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}"
_LABEL_P = LABEL_RE.pattern
_DAYS_P = r"[1-9][0-9]{0,2}"
_WEEKS_P = r"[1-9][0-9]?"
_N_P = r"[1-9][0-9]{0,2}"
GAP_FOLLOWED_MAX = 0.5      # rule_gap flag: followed this badly ...
GAP_OBJECTIVE_MIN = 0.8     # ... yet the objective outcome was this good

RULE_WEEKLY_RATE_SQL = """\
SELECT d.week_start AS week_start, d.rule_key AS rule_key, d.n_deviations AS n_deviations,
       d.sessions_hit AS sessions_hit, w.n_sessions AS sessions_total,
       if(w.n_sessions = 0, NULL, d.sessions_hit / w.n_sessions) AS rate, d.sample_ids AS sample_ids
FROM (
    SELECT toMonday(s.started) AS week_start,
           concat(v.instruction_source, ':', ifNull(v.rule_id, 'unknown')) AS rule_key,
           count() AS n_deviations, uniqExact(v.session_id) AS sessions_hit,
           arraySlice(arraySort(groupArray(v.id)), 1, 3) AS sample_ids
    FROM (SELECT id, session_id, instruction_source, rule_id FROM deviations FINAL) AS v
    INNER JOIN (SELECT id, ifNull(started_at, created_at) AS started FROM sessions FINAL
                WHERE ifNull(env, 'prod') NOT IN ('sandbox', 'eval')) AS s ON v.session_id = s.id
    WHERE toMonday(s.started) >= toMonday(now('UTC')) - toIntervalWeek({weeks:UInt16})
      AND ({rule:String} = '' OR concat(v.instruction_source, ':', ifNull(v.rule_id, 'unknown')) = {rule:String})
    GROUP BY week_start, rule_key
) AS d
LEFT JOIN (
    SELECT toMonday(ifNull(started_at, created_at)) AS wk, count() AS n_sessions FROM sessions FINAL
    WHERE ifNull(env, 'prod') NOT IN ('sandbox', 'eval')
    GROUP BY wk
) AS w ON d.week_start = w.wk
ORDER BY week_start DESC, n_deviations DESC, rule_key
LIMIT 1000
FORMAT JSONEachRow"""

TOKENS_BY_OUTCOME_SQL = """\
SELECT outcome, category, model, effort, count() AS n_sessions,
       sum(ifNull(tokens_input, 0) + ifNull(tokens_output, 0) + ifNull(tokens_cache_read, 0)
           + ifNull(tokens_cache_creation, 0)) AS tokens_total,
       sum(ifNull(tokens_output, 0)) AS tokens_out,
       sum(ifNull(active_seconds, 0)) AS active_total_s,
       quantileExact(0.5)(active_seconds) AS active_p50_s
FROM sessions FINAL
WHERE ifNull(env, 'prod') NOT IN ('sandbox', 'eval')
  AND toDate(ifNull(started_at, created_at)) >= toDate(now('UTC')) - {days:UInt16}
  AND ({project:String} = '' OR ifNull(project, '') = {project:String})
GROUP BY outcome, category, model, effort
ORDER BY tokens_total DESC, n_sessions DESC
LIMIT 1000
FORMAT JSONEachRow"""

SKILL_OUTCOMES_SQL = """\
SELECT k.skill AS skill, s.model AS model, count() AS n_sessions,
       countIf(s.outcome IN ('shipped', 'pushed', 'committed')) AS n_done,
       round(n_done / n_sessions, 4) AS done_rate,
       round(avg(ifNull(s.tokens_input, 0) + ifNull(s.tokens_output, 0) + ifNull(s.tokens_cache_read, 0)
                 + ifNull(s.tokens_cache_creation, 0)), 1) AS tokens_avg,
       groupArray(5)(s.id) AS sample_ids
FROM (SELECT DISTINCT session_id, skill FROM skill_calls FINAL
      WHERE skill IS NOT NULL AND session_id IS NOT NULL AND toDate(ts) >= toDate(now('UTC')) - {days:UInt16}) AS k
INNER JOIN (SELECT id, model, outcome, tokens_input, tokens_output, tokens_cache_read, tokens_cache_creation
            FROM sessions FINAL WHERE ifNull(env, 'prod') NOT IN ('sandbox', 'eval')) AS s ON s.id = k.session_id
GROUP BY skill, model
HAVING n_sessions >= {min_n:UInt16}
ORDER BY done_rate DESC, n_sessions DESC, skill, model
LIMIT 1000
FORMAT JSONEachRow"""

RESEARCH_RUNS_SQL = """\
SELECT id, run_id, task, status, gpu, provider, solved_rate, cell_rate, valid_rate, rouge1, rouge2, rouge_l, gen_ppl,
       sacrebleu_cased, chrf, unigram_entropy, cost_usd, gpu_minutes, runtime_s,
       toString(observed_at) AS observed
FROM research_runs FINAL
WHERE ({task:String} = '' OR task = {task:String})
  AND toDate(observed_at) >= toDate(now('UTC')) - {days:UInt16}
ORDER BY observed_at DESC
LIMIT 1000
FORMAT JSONEachRow"""

ROUTINE_STREAKS_SQL = """\
SELECT routine, n_runs, fails, if(last_pass = 0, toInt64(length(rev)), toInt64(last_pass) - 1) AS fail_streak,
       rev[1] AS last_status, duration_p50_s, duration_p95_s, last_run_id
FROM (
    SELECT routine, count() AS n_runs, countIf(status IN ('fail', 'error')) AS fails,
           arrayReverse(arrayMap(x -> x.2, arraySort(x -> x.1, groupArray((started_at, status))))) AS rev,
           indexOf(rev, 'pass') AS last_pass,
           quantileExact(0.5)(duration_s) AS duration_p50_s, quantileExact(0.95)(duration_s) AS duration_p95_s,
           argMax(id, started_at) AS last_run_id
    FROM runs FINAL
    WHERE status != 'skip' AND toDate(started_at) >= toDate(now('UTC')) - {days:UInt16}
      AND ({routine:String} = '' OR routine = {routine:String})
    GROUP BY routine
)
ORDER BY fail_streak DESC, fails DESC, routine
LIMIT 1000
FORMAT JSONEachRow"""

RULE_GAP_SQL = """\
SELECT rule_key, n_deviations, n_sessions, followed_avg, result_avg, objective_avg, objective_n,
       objective_avg - followed_avg AS gap, sample_ids
FROM (
    SELECT concat(instruction_source, ':', ifNull(rule_id, 'unknown')) AS rule_key,
           count() AS n_deviations, uniqExact(session_id) AS n_sessions,
           avg(followed_grade) AS followed_avg, avg(result_grade) AS result_avg,
           avg(objective_result) AS objective_avg, count(objective_result) AS objective_n,
           arraySlice(arraySort(groupArray(id)), 1, 3) AS sample_ids
    FROM deviations FINAL
    WHERE toDate(created_at) >= toDate(now('UTC')) - {days:UInt16}
      AND session_id NOT IN (SELECT id FROM sessions FINAL WHERE env IN ('sandbox', 'eval'))
      AND ({rule:String} = '' OR concat(instruction_source, ':', ifNull(rule_id, 'unknown')) = {rule:String})
    GROUP BY rule_key
)
WHERE n_deviations >= {min_n:UInt32} AND objective_n > 0
ORDER BY gap DESC, rule_key
LIMIT 1000
FORMAT JSONEachRow"""


def _part(v: Any) -> str:
    return _NOT_REF.sub("_", str(v)).replace("/", "_") if v not in (None, "") else "unknown"


@dataclass(frozen=True)
class Query:
    spec: NamedQuery
    sql: str
    defaults: Mapping[str, str]                 # every param the SQL uses gets a value; `limit` stays here
    key: Callable[[Mapping[str, Any]], str | None]
    metrics: tuple[str, ...]                    # output columns copied into EvidenceRef.metrics
    subject: tuple[str, str] | None = None      # (amem entity, output column holding its id or ids)


QUERIES: dict[str, Query] = {
    "rule_weekly_rate": Query(
        NamedQuery("rule_weekly_rate", {"rule": _RULE_P, "weeks": _WEEKS_P, "limit": _N_P},
                   "weekly deviation rate per rule: sessions with a deviation / prod sessions that week"),
        RULE_WEEKLY_RATE_SQL, {"rule": "", "weeks": "8", "limit": "50"},
        lambda r: f"{r['rule_key']}@{r['week_start']}" if r.get("rule_key") and r.get("week_start") else None,
        ("week_start", "rule_key", "n_deviations", "sessions_hit", "sessions_total", "rate"),
        ("deviation", "sample_ids")),
    "tokens_by_outcome": Query(
        NamedQuery("tokens_by_outcome", {"project": _LABEL_P, "days": _DAYS_P, "limit": _N_P},
                   "tokens and active time per outcome x category x model x effort (prod sessions)"),
        TOKENS_BY_OUTCOME_SQL, {"project": "", "days": "30", "limit": "50"},
        lambda r: "/".join(_part(r.get(c)) for c in ("outcome", "category", "model", "effort")),
        ("outcome", "category", "model", "effort", "n_sessions", "tokens_total", "tokens_out", "active_total_s",
         "active_p50_s")),
    "skill_outcomes": Query(
        NamedQuery("skill_outcomes", {"days": _DAYS_P, "min_n": _N_P, "limit": _N_P},
                   "per skill x model: prod sessions that used the skill, share shipped/pushed/committed, tokens"),
        SKILL_OUTCOMES_SQL, {"days": "30", "min_n": "3", "limit": "50"},
        lambda r: f"{_part(r.get('skill'))}/{_part(r.get('model'))}" if r.get("skill") else None,
        ("skill", "model", "n_sessions", "n_done", "done_rate", "tokens_avg"),
        ("session", "sample_ids")),
    "research_runs": Query(
        NamedQuery("research_runs", {"task": _LABEL_P, "days": _DAYS_P, "limit": _N_P},
                   "NCM research runs: task metrics, status, GPU, cost (public benchmarks only)"),
        RESEARCH_RUNS_SQL, {"task": "", "days": "90", "limit": "50"},
        lambda r: r.get("run_id") or None,
        ("run_id", "task", "status", "gpu", "provider", "solved_rate", "cell_rate", "valid_rate", "rouge1", "rouge2",
         "rouge_l", "gen_ppl", "sacrebleu_cased", "chrf", "unigram_entropy", "cost_usd", "gpu_minutes", "runtime_s"),
        ("evidence", "id")),
    "routine_streaks": Query(
        NamedQuery("routine_streaks", {"routine": _LABEL_P, "days": _DAYS_P, "limit": _N_P},
                   "per routine: current fail streak (fail/error since the last pass, skips ignored), "
                   "duration p50/p95"),
        ROUTINE_STREAKS_SQL, {"routine": "", "days": "30", "limit": "50"},
        lambda r: r.get("routine") or None,
        ("routine", "n_runs", "fails", "fail_streak", "last_status", "duration_p50_s", "duration_p95_s"),
        ("run", "last_run_id")),
    "rule_gap": Query(
        NamedQuery("rule_gap", {"rule": _RULE_P, "days": _DAYS_P, "min_n": _N_P, "limit": _N_P},
                   "per rule: followed grade vs objective outcome (gap = objective - followed), prod sessions"),
        RULE_GAP_SQL, {"rule": "", "days": "90", "min_n": "3", "limit": "50"},
        lambda r: r.get("rule_key") or None,
        ("rule_key", "n_deviations", "n_sessions", "followed_avg", "result_avg", "objective_avg", "objective_n", "gap"),
        ("deviation", "sample_ids")),
}
_NUM = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?")


def _metric(v: Any) -> Any:
    """Numbers and labels only (64-bit ints may arrive quoted)."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return round(v, 4) if math.isfinite(v) else None
    if isinstance(v, str):
        if _NUM.fullmatch(v):
            return int(v) if re.fullmatch(r"-?[0-9]+", v) else _metric(float(v))
        return v if LABEL_RE.fullmatch(v) else None
    return None


class ClickHouseRetriever:
    """evidence.Retriever for ClickHouse: only QUERIES, parameters checked by run_query before any call."""

    source = "ch"

    def __init__(self, client: Client | None = None, *, now: Callable[[], datetime] | None = None):
        self.client = client if client is not None else Client()
        self._now = now or (lambda: datetime.now(UTC))

    def health(self) -> Health:
        if self.client.cfg.problem:
            return Health(False, self.client.cfg.problem)
        try:
            self.client.ping()
        except http.HttpError as exc:
            return Health(False, str(exc)[:200])
        return Health(True)

    def named_queries(self) -> Mapping[str, NamedQuery]:
        return {n: q.spec for n, q in QUERIES.items()}

    def query(self, name: str, params: Mapping[str, str]) -> list[EvidenceRef]:
        q = QUERIES[name]
        p = {**q.defaults, **q.spec.check(params)}       # checked again: query() may be called directly
        if p.get("project"):            # ClickHouse holds pseudonyms only: translate on the Mac, never send the name
            pseudo = pseudonym(p["project"])
            if pseudo is None:
                raise ValueError(f"{PSEUDONYM_ENV} is not set: cannot filter by project")
            p["project"] = pseudo
        if p.get("rule"):               # the same for a repo-scoped rule key (F1)
            p["rule"] = rule_param(p["rule"])
        limit = int(p.pop("limit"))
        observed = self._now().astimezone(UTC).isoformat(timespec="seconds")
        out: list[EvidenceRef] = []
        for row in self.client.select(q.sql, p):
            key = q.key(row)
            ref = f"ch:q.{name}/{_NOT_REF.sub('_', key)}" if key else ""
            if not refs.is_ref(ref):
                continue
            subjects: list[str] = []
            if q.subject:
                entity, col = q.subject
                ids = row.get(col)
                for i in ids if isinstance(ids, list) else [ids]:
                    if isinstance(i, str) and _is_id(i):
                        subjects.append(refs.amem(entity, i))
            metrics = {m: _metric(row.get(m)) for m in q.metrics}
            if name == "rule_gap":
                f, o = metrics.get("followed_avg"), metrics.get("objective_avg")
                metrics["flag"] = isinstance(f, float | int) and isinstance(o, float | int) \
                    and f <= GAP_FOLLOWED_MAX and o >= GAP_OBJECTIVE_MIN
            out.append(EvidenceRef(ref, "ch", "metric", subjects, metrics, None, observed))
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------- verify: per-day parity

VERIFY_SQL = {t.name: (f"SELECT toString(toDate({t.day})) AS d, count() AS n FROM {t.name} FINAL "
                       f"WHERE toDate({t.day}) >= {{since:Date}} GROUP BY d ORDER BY d FORMAT JSONEachRow")
              for t in TABLES}


@dataclass
class VerifyReport:
    since: str
    days: int
    status: str = "ok"                                       # ok | unavailable | error
    counts: dict[str, dict[str, list[int]]] = field(default_factory=dict)   # table -> day -> [supabase, clickhouse]
    parity: bool = False
    detail: str = ""

    def mismatches(self) -> list[tuple[str, str, int, int]]:
        return [(t, d, sb, ch) for t, per_day in self.counts.items() for d, (sb, ch) in sorted(per_day.items())
                if sb != ch]

    def to_json(self) -> dict[str, Any]:
        return {"since": self.since, "days": self.days, "status": self.status, "parity": self.parity,
                "detail": self.detail, "counts": self.counts,
                "mismatches": [list(m) for m in self.mismatches()]}

    def line(self) -> str:
        if self.status != "ok":
            return f"verify clickhouse: {self.status} ({self.detail})"
        bad = self.mismatches()
        tail = "" if not bad else " " + ", ".join(f"{t}@{d} sb={sb} ch={ch}" for t, d, sb, ch in bad[:5])
        return f"verify clickhouse since {self.since}: parity={'yes' if self.parity else 'no'}{tail}"


def verify(conn: psycopg.Connection, days: int = 30, *, client: Client | None = None, today: date | None = None,
           tables: Sequence[Table] = TABLES) -> VerifyReport:
    """Rows per UTC day and table since `days - 1` days before today, Supabase vs ClickHouse (FINAL).

    Supabase counts every row, so a row the mapper dropped (a key that is not a ref id) shows as a mismatch.
    """
    if not 1 <= days <= 366:
        raise ValueError("days is 1..366")
    client = client if client is not None else Client()
    since = (today or datetime.now(UTC).date()) - timedelta(days=days - 1)
    rep = VerifyReport(since.isoformat(), days)
    if client.cfg.problem:
        rep.status, rep.detail = "unavailable", client.cfg.problem
        return rep
    try:
        client.ping()
    except http.HttpError as exc:
        rep.status, rep.detail = "unavailable", str(exc)[:200]
        return rep
    for t in tables:
        per_day: dict[str, list[int]] = {}
        q = sql.SQL("select to_char(({day} at time zone 'UTC')::date, 'YYYY-MM-DD'), count(*) from {table} "
                    "where {day} >= (%s::date)::timestamp at time zone 'UTC'{extra} group by 1").format(
            day=sql.Identifier(t.day), table=sql.Identifier(t.src),
            extra=sql.SQL(f" and ({t.where})") if t.where else sql.SQL(""))
        for d, n in conn.execute(q, (since,)).fetchall():
            per_day.setdefault(d, [0, 0])[0] = int(n)
        try:
            ch_rows = client.select(VERIFY_SQL[t.name], {"since": since.isoformat()})
        except http.HttpError as exc:
            rep.status, rep.detail = "error", f"{t.name}: {str(exc)[:200]}"
            return rep
        for r in ch_rows:
            per_day.setdefault(str(r["d"]), [0, 0])[1] = int(r["n"])
        rep.counts[t.name] = per_day
    rep.parity = not rep.mismatches()
    return rep
