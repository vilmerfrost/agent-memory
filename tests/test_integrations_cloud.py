"""Mission 004B, cloud-first hardening: explicit TLS, one-attempt OTLP export, pseudonymous ClickHouse labels,
client-side daily limits, and record-level dead letters in the sink loop."""

from __future__ import annotations

import hashlib
import json
import ssl
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from integrations_mock import MockService

from agent_memory.integrations import clickhouse as CH, evidence as E, http, langfuse as LF, pastdev as PD, sync

APP_PW = "cloud-test-pw-0123456789"
KEY = "k" * 40


# ---------------------------------------------------------------- TLS


def test_tls_context_is_explicit_and_verifying(tmp_path: Path) -> None:
    ctx = http.tls_context({})
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname and ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    bad = tmp_path / "ca.pem"
    bad.write_text("not a certificate")
    with pytest.raises(ssl.SSLError):
        http.tls_context({http.CA_FILE_ENV: str(bad)})


def test_https_requests_carry_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _R:
        status, headers = 200, {}

        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> _R:
            return self

        def __exit__(self, *a: Any) -> None:
            pass

    def fake_urlopen(req: Any, timeout: float, context: Any = None) -> _R:
        seen["context"] = context
        return _R()
    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen)
    http.request("GET", "https://example.invalid/x")
    assert isinstance(seen["context"], ssl.SSLContext) and seen["context"].verify_mode == ssl.CERT_REQUIRED


# ---------------------------------------------------------------- Langfuse: one attempt for span export


def test_otlp_export_never_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LF.LangfuseClient("https://cloud.langfuse.example", "pk-lf-test", "sk-lf-test")
    calls: list[dict[str, Any]] = []

    def fake_call(method: str, path: str, body: Any = None, *, retries: int | None = None, timeout: float | None = None):
        calls.append({"path": path, "retries": retries})
        return http.Response(200, b"{}", {})
    monkeypatch.setattr(client, "_call", fake_call)
    client.export_spans({"resourceSpans": []})
    assert calls == [{"path": LF.OTLP_PATH, "retries": 0}]


# ---------------------------------------------------------------- ClickHouse: pseudonyms and team masking


def test_pseudonyms_are_keyed_stable_and_never_the_name() -> None:
    a = CH.pseudonym("agent-memory", {CH.PSEUDONYM_ENV: KEY})
    assert a == CH.pseudonym("agent-memory", {CH.PSEUDONYM_ENV: KEY}) and a.startswith("p_") and len(a) == 18
    assert "agent" not in a
    assert CH.pseudonym("agent-memory", {CH.PSEUDONYM_ENV: "j" * 40}) != a          # another key, another pseudonym
    assert CH.pseudonym("agent-memory", {}) is None and CH.pseudonym("agent-memory", {CH.PSEUDONYM_ENV: "short"}) is None
    # Mission 004C (F9): `team` is a value like any other (the mask comes from the row's class, not the value)
    assert CH.pseudonym("team", {}) is None and CH.pseudonym("team", {CH.PSEUDONYM_ENV: KEY}).startswith("p_")


@pytest.fixture
def app(pg_url: str) -> Iterator[str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{APP_PW}'")
        conn.execute("delete from rsi_checkpoints where job like 'sync:%'")
        # cloud-own: an own session as ingestion stores one (Mission 004C, F8: `{}` is unknown, so private)
        for sid, project, meta in (("cloud-own", "agent-memory", '{"team": false, "remote_owner": "vilmerfrost"}'),
                                   ("cloud-team", "pipeline-poc", '{"team": true}')):
            conn.execute("insert into sessions (session_id, kind, project_dir, project, repo, transcript_bytes, env, meta) "
                         "values (%s, 'chat', '/x', %s, %s, 1, 'prod', %s::jsonb) on conflict (session_id) do update "
                         "set project = excluded.project, repo = excluded.repo, meta = excluded.meta",
                         (sid, project, project, meta))
    yield pg_url.replace("postgres:postgres@", f"amem_app:{APP_PW}@", 1)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("delete from rsi_checkpoints where job like 'sync:%'")
        conn.execute("delete from rsi_evidence where external_ref like 'past:cloud-test-%'")
        conn.execute("delete from sessions where session_id in ('cloud-own', 'cloud-team')")


def _session_rows(app: str) -> dict[str, dict[str, Any]]:
    sink = CH.ClickHouseSink(CH.Client(CH.Config("https://ch.example.invalid:8443", "amem_sync", "x")))
    with psycopg.connect(app) as conn:
        rows = [r for b in sink.batches(conn, {}, replay=True) if b.table == "sessions" for r in b.rows]
    return {r["id"]: r for r in rows if r["id"] in ("cloud-own", "cloud-team")}


def test_team_rows_send_team_and_own_rows_send_a_pseudonym(app: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CH.PSEUDONYM_ENV, KEY)
    rows = _session_rows(app)
    assert rows["cloud-team"]["project"] == rows["cloud-team"]["repo"] == "team"
    assert rows["cloud-own"]["project"] == CH.pseudonym("agent-memory", {CH.PSEUDONYM_ENV: KEY})
    assert all("agent-memory" not in str(v) and "pipeline-poc" not in str(v) for r in rows.values() for v in r.values())
    monkeypatch.delenv(CH.PSEUDONYM_ENV)
    rows = _session_rows(app)
    assert rows["cloud-own"]["project"] is None and rows["cloud-team"]["project"] == "team"   # no key: no name


# ---------------------------------------------------------------- sink loop: daily limits and dead letters


class _Sink:
    name = "pastdev"

    def __init__(self, n: int = 4, *, bad: tuple[str, ...] = (), status: int = 400, splittable: bool = True):
        self.n, self.bad, self.status, self.pushed = n, set(bad), status, []
        if not splittable:
            self.split = None  # type: ignore[assignment]

    def health(self) -> E.Health:
        return E.Health(True)

    def batches(self, conn: psycopg.Connection, watermarks: Mapping[str, str], *, replay: bool) -> Iterator[sync.Batch]:
        if watermarks.get("t") == "done":
            return
        rows = [{"id": f"r{i}"} for i in range(self.n)]
        yield sync.Batch("t", rows, "done", [E.EvidenceRef(f"past:cloud-test-{r['id']}", "pd", "x",
                                                           ["amem:session/cloud-own"]) for r in rows])

    def split(self, batch: sync.Batch) -> list[sync.Batch]:
        return [sync.Batch(batch.table, [r], batch.watermark,
                           [e for e in batch.evidence if e.ref == f"past:cloud-test-{r['id']}"]) for r in batch.rows]

    def push(self, batch: sync.Batch) -> None:
        if any(r["id"] in self.bad for r in batch.rows):
            raise http.HttpError(self.status, "refused")
        self.pushed.append([r["id"] for r in batch.rows])


def test_a_refused_record_is_dead_lettered_and_the_rest_lands(app: str) -> None:
    s = _Sink(bad=("r2",))
    with psycopg.connect(app) as conn:
        rep = sync.run(s, conn, environ={})
        assert (rep.status, rep.rows, rep.dead_letters) == ("ok", 3, 1)
        assert s.pushed == [["r0"], ["r1"], ["r3"]]
        meta = sync.load(conn, "pastdev")
        assert meta["watermarks"] == {"t": "done"}                    # the table moved on
        assert [d["key"] for d in meta["dead_letters"]] == ["r2"] and "400" in meta["dead_letters"][0]["error"]
        assert sync.status(conn)[0]["dead_letters"] == 1
        n = conn.execute("select count(*) from rsi_evidence where external_ref like 'past:cloud-test-%'").fetchone()[0]
        assert n == 3                                                 # links only for what landed


def _reset(pg_url: str) -> None:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("delete from rsi_checkpoints where job like 'sync:%'")
        conn.execute("delete from rsi_evidence where external_ref like 'past:cloud-test-%'")


def test_auth_or_overload_errors_back_off_instead_of_dead_lettering(app: str, pg_url: str) -> None:
    for status in (401, 403, 429, 503):
        _reset(pg_url)
        with psycopg.connect(app) as conn:
            rep = sync.run(_Sink(bad=("r0", "r1", "r2", "r3"), status=status), conn, environ={})
            meta = sync.load(conn, "pastdev")
            assert rep.status == "failed" and rep.dead_letters == 0, status
            assert "t" not in meta.get("watermarks", {}) and meta["failures"]["t"]["attempts"] == 1


def test_unsplittable_refusal_dead_letters_the_whole_batch(app: str) -> None:
    with psycopg.connect(app) as conn:
        rep = sync.run(_Sink(n=2, bad=("r0",), splittable=False), conn, environ={})
        assert rep.dead_letters == 2 and sync.load(conn, "pastdev")["watermarks"] == {"t": "done"}


def test_client_side_daily_limit_caps_and_resets_next_day(app: str, pg_url: str) -> None:
    t0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    env = {"AMEM_SYNC_PASTDEV_MAX_ROWS_PER_DAY": "3"}
    with psycopg.connect(app) as conn:
        rep = sync.run(_Sink(n=4), conn, now=t0, environ=env)
        assert rep.status == "capped" and rep.rows == 0 and "3 rows" in rep.detail
        assert "t" not in sync.load(conn, "pastdev").get("watermarks", {})
        rep = sync.run(_Sink(n=2), conn, now=t0, environ=env)
        assert rep.status == "ok" and rep.usage == {"day": "2026-10-17", "rows": 2, "requests": 1}
        _reset(pg_url)
        sync.run(_Sink(n=2), conn, now=t0, environ=env)
        rep = sync.run(_Sink(n=2), conn, now=t0 + timedelta(seconds=1), environ={"AMEM_SYNC_PASTDEV_MAX_REQUESTS_PER_DAY": "1"})
        assert rep.status in ("ok", "capped")
        rep = sync.run(_Sink(n=2), conn, now=t0 + timedelta(days=1), environ=env)       # a new day, a new allowance
        assert rep.usage["day"] == "2026-10-18"


def test_limits_have_conservative_defaults() -> None:
    assert sync.limits("pastdev", {}) == (300, 200)
    assert sync.limits("clickhouse", {"AMEM_SYNC_CLICKHOUSE_MAX_ROWS_PER_DAY": "10"}) == (10, 2_000)
    assert sync.limits("pastdev", {"AMEM_SYNC_PASTDEV_MAX_ROWS_PER_DAY": "-1"}) == (300, 200)   # junk keeps the default


# ---------------------------------------------------------------- the adapters' split()


def test_adapters_split_one_record_per_batch() -> None:
    ch = CH.ClickHouseSink(CH.Client(CH.Config("https://ch.example.invalid:8443", "u", "k")))
    parts = ch.split(sync.Batch("runs", [{"id": "a"}, {"id": "b"}], "w"))
    assert [p.rows for p in parts] == [[{"id": "a"}], [{"id": "b"}]] and all(p.key == "" for p in parts)
    pd = PD.PastDevSink(PD.Config("https://api.past.dev/api/v1", "past_sk_test"))
    evs = [E.EvidenceRef("past:amem:proposal/p1", "pd", "decision-memory", ["amem:proposal/p1"]),
           E.EvidenceRef("past:amem:proposal/p2", "pd", "decision-memory", ["amem:proposal/p2"])]
    parts = pd.split(sync.Batch("rsi_proposals", [{"id": "amem:proposal/p1"}, {"id": "amem:proposal/p2"}], "w", evs, "K"))
    assert [[e.ref for e in p.evidence] for p in parts] == [["past:amem:proposal/p1"], ["past:amem:proposal/p2"]]
    assert len({p.key for p in parts}) == 2 and "K" not in {p.key for p in parts}
    assert pd.split(sync.Batch(PD.RETRACT, [{"id": "x"}, {"id": "y"}], "w")) != [] and len(
        pd.split(sync.Batch(PD.RETRACT, [{"id": "x"}, {"id": "y"}], "w"))) == 1


# ---------------------------------------------------------------- past.dev: async rejections (docs 2026-10-16)


class _FakePD:
    """Duck-typed past.dev client: one queued ingestion whose polls follow `states`."""

    def __init__(self, states: list[dict[str, Any]]):
        self.states, self.retries = list(states), 0

    def ingest_batch(self, items: list[dict[str, Any]], key: str) -> dict[str, Any]:
        return {"status": "queued", "ingestionId": "00000000-0000-4000-8000-000000000001",
                "items": [{"ordinal": i, "sourceId": it["id"]} for i, it in enumerate(items)]}

    def ingestion(self, ingestion_id: str) -> dict[str, Any]:
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def retry_ingestion(self, ingestion_id: str) -> dict[str, Any]:
        self.retries += 1
        return {}


def _pd_batch() -> sync.Batch:
    rows = [{"id": "amem:proposal/p1", "content": "x"}]
    return sync.Batch("rsi_proposals", rows, "w", [E.EvidenceRef("past:amem:proposal/p1", "pd", "decision-memory",
                                                                  ["amem:proposal/p1"])], "K")


def test_content_rejected_is_dead_lettered_without_a_split() -> None:
    """Mission 004C (G-PD0): a split would re-send content past.dev already holds (answered `unchanged`)."""
    fake = _FakePD([{"status": "failed", "failure": {"code": "content-rejected"}}])
    sink = PD.PastDevSink(PD.Config("past_sk_test"), client=fake, poll_s=5.0, sleep=lambda s: None)  # type: ignore[arg-type]
    with pytest.raises(sync.Refused) as ei:
        sink.push(_pd_batch())
    assert ei.value.rows == [{"id": "amem:proposal/p1"}] and fake.retries == 0 and not sync._record_error(ei.value)
    (ev,) = ei.value.evidence
    assert (ev.metrics["status"], ev.metrics["failure"], ev.metrics["ingestion_id"]) == \
        ("rejected", "content-rejected", "00000000-0000-4000-8000-000000000001")


def test_other_ingestion_failures_get_a_free_retry_and_stay_pending() -> None:
    fake = _FakePD([{"status": "failed", "failure": {"code": "model-unavailable"}}, {"status": "completed"}])
    sink = PD.PastDevSink(PD.Config("past_sk_test"), client=fake, poll_s=5.0, sleep=lambda s: None)  # type: ignore[arg-type]
    b = _pd_batch()
    sink.push(b)
    assert fake.retries == 1 and (b.evidence[0].metrics["status"], b.evidence[0].metrics["retries"]) == ("completed", 1)
    fake = _FakePD([{"status": "failed", "failure": {"code": "internal"}}])
    sink = PD.PastDevSink(PD.Config("past_sk_test"), client=fake, poll_s=5.0, sleep=lambda s: None)  # type: ignore[arg-type]
    b = _pd_batch()
    sink.push(b)                                      # failed again after the retry: pending, the next run checks
    assert fake.retries == 1 and (b.evidence[0].metrics["status"], b.evidence[0].metrics["failure"]) == ("queued", "internal")


# ---------------------------------------------------------------- ClickHouse Cloud


def test_cloud_is_detected_and_plain_http_is_refused_for_remote_hosts() -> None:
    cfg = CH.config({"AMEM_CLICKHOUSE_URL": "https://abc123.eu-central-1.aws.clickhouse.cloud:8443"})
    assert cfg.cloud and not cfg.problem
    assert CH.config({"AMEM_CLICKHOUSE_URL": "https://ch.example.org:8443", "AMEM_CLICKHOUSE_CLOUD": "1"}).cloud
    assert not CH.config({"AMEM_CLICKHOUSE_URL": "http://127.0.0.1:8123"}).cloud
    for url in ("http://127.0.0.1:8123", "http://localhost:8123", "http://[::1]:8123"):
        assert not CH.config({"AMEM_CLICKHOUSE_URL": url}).problem
    for url in ("http://abc.clickhouse.cloud:8123", "http://ch.example.org", "http://user@evil.example:80"):
        assert "plain http" in CH.config({"AMEM_CLICKHOUSE_URL": url}).problem
    assert CH.Client(cfg).timeout == 30.0 and CH.Client(CH.Config("http://127.0.0.1:1")).timeout == 10.0


def test_cloud_schema_has_no_non_replicated_settings_and_no_auto_ddl() -> None:
    for s in CH.schema_sql(cloud=True)[1:]:
        assert "ENGINE = ReplacingMergeTree(version)" in s and "SETTINGS" not in s and s.endswith("ORDER BY id")
    assert all("non_replicated_deduplication_window" in s for s in CH.schema_sql()[1:])
    cloud = CH.Client(CH.Config("https://x.clickhouse.cloud:8443", cloud=True))
    assert not CH.ClickHouseSink(cloud).auto_schema and CH.ClickHouseSink(CH.Client(CH.Config("http://127.0.0.1:1"))).auto_schema


def test_writer_user_is_least_privilege_and_has_no_password() -> None:
    create, grant = CH.writer_sql()
    assert create == ("CREATE USER IF NOT EXISTS amem_writer IDENTIFIED WITH sha256_hash BY "
                      "'<SHA-256 hex of the password>' DEFAULT DATABASE amem")
    assert grant == "GRANT SELECT, INSERT ON amem.* TO amem_writer"
    sha = "ab" * 32
    assert f"BY '{sha}' DEFAULT DATABASE amem" in CH.writer_sql(sha256_hex=sha)[0]
    for bad in ({"user": "x; drop"}, {"db": "a;b"}, {"sha256_hex": "AB" * 32}, {"sha256_hex": "x' OR 1"}):
        with pytest.raises(ValueError):
            CH.writer_sql(**bad)


@pytest.mark.parametrize("pw, ok", [("Aa1!aaaaaaaa", True), ("Aa1!aaaaaaa", False), ("aa1!aaaaaaaa", False),
                                    ("AA1!AAAAAAAA", False), ("Aaa!aaaaaaaa", False), ("Aa1aaaaaaaaa", False)])
def test_cloud_password_rule(pw: str, ok: bool) -> None:
    assert CH.password_ok(pw) is ok


def test_cli_schema_parts_and_hash_from_env_never_print_the_password(
        capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import cli
    monkeypatch.setattr(CH.http, "request", lambda *a, **k: pytest.fail("schema must not touch the network"))
    monkeypatch.setenv("AMEM_NO_DOTENV", "1")
    assert cli.main(["sync", "schema", "--sink", "clickhouse", "--cloud", "--part", "admin"]) == 0
    admin = capsys.readouterr().out
    assert admin.count("CREATE TABLE") == len(CH.TABLES) and "USER" not in admin and "GRANT" not in admin
    pw = "Zx9!secret-Pw-for-test"
    monkeypatch.setenv(CH.KEY_ENV, pw)
    assert cli.main(["sync", "schema", "--sink", "clickhouse", "--cloud", "--part", "writer", "--hash-from-env"]) == 0
    cap = capsys.readouterr()
    sha = hashlib.sha256(pw.encode()).hexdigest()
    assert f"IDENTIFIED WITH sha256_hash BY '{sha}'" in cap.out and "CREATE TABLE" not in cap.out
    assert pw not in cap.out + cap.err
    monkeypatch.setenv(CH.KEY_ENV, "weakpassword1")
    assert cli.main(["sync", "schema", "--sink", "clickhouse", "--part", "writer", "--hash-from-env"]) == 2
    cap = capsys.readouterr()
    assert "does not meet the Cloud rule" in cap.err and "weakpassword1" not in cap.out + cap.err
    monkeypatch.delenv(CH.KEY_ENV)
    assert cli.main(["sync", "schema", "--sink", "clickhouse", "--part", "writer", "--hash-from-env"]) == 2


def _fake_clickhouse(svc: MockService, allow: set[str]) -> None:
    """Answers like ClickHouse over HTTP: ACCESS_DENIED (403, code 497) unless the statement's probe is allowed."""
    def kind(stmt: str) -> str:
        for k in ("SHOW GRANTS", "system.databases", "system.users", "system.query_log", "CREATE DATABASE",
                  "CREATE TABLE", "ALTER TABLE", "CREATE USER", "GRANT SELECT", "INSERT INTO", "SELECT count()"):
            if k in stmt:
                return k
        return "?"

    def handle(r: Any) -> tuple[int, Any]:
        k = kind(r.body.decode())
        if k not in allow:
            return 403, "Code: 497. DB::Exception: amem_writer: Not enough privileges. (ACCESS_DENIED)"
        rows = {"SHOW GRANTS": [{"GRANTS FOR amem_writer": "GRANT SELECT, INSERT ON amem.* TO amem_writer"}],
                "system.databases": [{"name": "amem"}, {"name": "system"}, {"name": "INFORMATION_SCHEMA"}],
                "SELECT count()": [{"n": 0}]}.get(k, [])
        return 200, "".join(json.dumps(x) + "\n" for x in rows)
    svc.route("GET", "/ping", lambda r: (200, "Ok.\n"))
    svc.route("POST", "/", handle)


def test_grants_probe_passes_only_for_select_and_insert() -> None:
    least = {"SHOW GRANTS", "system.databases", "SELECT count()", "INSERT INTO"}
    with MockService() as svc:
        _fake_clickhouse(svc, least)
        rows = CH.check_grants(CH.Client(CH.Config(svc.url, "amem_writer", "k"), retries=0))
        assert [r["probe"] for r in rows if not r["ok"]] == [] and len(rows) == 11
        sent = " ".join(q.body.decode() for q in svc.requests)
        assert "ENGINE = Null" in sent and "HOST NONE" in sent and "WHERE 0" in sent      # harmless if they ran
    with MockService() as svc:                                    # a writer that can also create tables fails
        _fake_clickhouse(svc, least | {"CREATE TABLE", "system.query_log"})
        bad = {r["probe"] for r in CH.check_grants(CH.Client(CH.Config(svc.url, "amem_writer", "k"), retries=0))
               if not r["ok"]}
        assert bad == {"create table", "query log"}


def test_cloud_ping_waits_for_wake_and_inserts_are_synchronous(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, float]] = []

    class _Resp:
        status = 200

        def text(self) -> str:
            return "Ok.\n"

    def fake(method: str, url: str, *, timeout: float, **kw: Any) -> _Resp:
        calls.append((method, url, timeout))
        return _Resp()
    monkeypatch.setattr(CH.http, "request", fake)
    c = CH.Client(CH.Config("https://x.clickhouse.cloud:8443", "amem_writer", "k", cloud=True))
    c.ping()
    c.insert("sessions", b"{}\n", "tok")
    assert calls[0][2] == CH.WAKE_TIMEOUT_S
    assert "async_insert=0" in calls[1][1] and "insert_deduplication_token=tok" in calls[1][1]
    CH.Client(CH.Config("http://127.0.0.1:1")).ping()
    assert calls[2][2] == 3.0


def test_cli_prints_cloud_schema_and_sends_nothing(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import cli
    monkeypatch.setattr(CH.http, "request", lambda *a, **k: pytest.fail("schema must not touch the network"))
    assert cli.main(["sync", "schema", "--sink", "clickhouse", "--cloud"]) == 0
    out = capsys.readouterr().out
    assert "CREATE DATABASE IF NOT EXISTS amem;" in out and "SETTINGS" not in out
    assert "GRANT SELECT, INSERT ON amem.* TO amem_writer;" in out
    assert cli.main(["sync", "schema", "--sink", "clickhouse", "--db", "a;b"]) == 2


# ---------------------------------------------------------------- HTTP: Retry-After and the loopback check


def test_plain_http_only_to_a_parsed_loopback_host() -> None:
    for url in ("http://127.0.0.1:1/x", "http://localhost:8123", "http://[::1]:8123/", "https://example.org"):
        assert http.plain_http_ok(url), url
    for url in ("http://localhost.evil.test/x", "http://127.0.0.1.evil.test", "http://example.org", "ftp://localhost"):
        assert not http.plain_http_ok(url), url
        with pytest.raises(http.HttpError, match="plain http"):
            http.request("GET", url)


def test_retry_after_is_honored_up_to_a_minute() -> None:
    waits: list[float] = []
    with MockService() as svc:
        calls = iter([(429, {"m": "slow down"}, {"Retry-After": "7"}), (200, {"ok": True})])
        svc.route("GET", "/x", lambda r: next(calls))
        assert http.request("GET", f"{svc.url}/x", sleep=waits.append).json() == {"ok": True}
        assert waits == [7.0]
        svc.route("GET", "/y", lambda r: (429, {"m": "later"}, {"Retry-After": "3600"}))
        with pytest.raises(http.HttpError, match="Retry-After 3600s"):
            http.request("GET", f"{svc.url}/y", sleep=waits.append)
        assert waits == [7.0] and len(svc.requests_to("GET", "/y")) == 1      # no wait, no retry
    assert http.retry_after({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None   # date form: backoff


# ---------------------------------------------------------------- Langfuse Cloud: throttle, windows, record errors


def test_langfuse_cloud_throttles_every_call_but_the_otlp_export(monkeypatch: pytest.MonkeyPatch) -> None:
    t = [100.0]
    slept: list[float] = []
    paths: list[str] = []

    class _R:
        status, body, headers = 200, b"{}", {}

        def json(self) -> Any:
            return {}

    def fake(method: str, url: str, **kw: Any) -> _R:
        paths.append(url.split("cloud.langfuse.com", 1)[1].split("?", 1)[0])
        return _R()
    monkeypatch.setattr(LF.http, "request", fake)
    c = LF.LangfuseClient("https://cloud.langfuse.com", "pk-lf-t", "sk-lf-t", sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)),
                          clock=lambda: t[0])
    assert c.cloud and c.interval == LF.CLOUD_MIN_INTERVAL_S
    c.observations(traceId="0" * 32)
    c.export_spans({"resourceSpans": []})
    c.scores(traceId="0" * 32)
    assert paths == [LF.OBSERVATIONS_PATH, LF.OTLP_PATH, LF.SCORES_PATH]
    assert slept == [pytest.approx(LF.CLOUD_MIN_INTERVAL_S)]             # only between the two general calls
    assert LF.LangfuseClient("https://us.cloud.langfuse.com").cloud and not LF.LangfuseClient("https://lf.example.com").cloud
    assert LF.LangfuseClient("http://127.0.0.1:1").interval == 0.0
    env = {"AMEM_LANGFUSE_HOST": "https://cloud.langfuse.com", "AMEM_LANGFUSE_MIN_INTERVAL_S": "5"}
    assert LF.LangfuseClient.from_env(env).interval == 5.0


def test_langfuse_reads_are_bounded_to_the_visible_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    c = LF.LangfuseClient("https://cloud.langfuse.com", "pk", "sk", min_interval=0)
    monkeypatch.setattr(c, "_pages", lambda path, params, max_pages: seen.append({"path": path, **params}) or [])
    start = datetime(2026, 10, 16, 10, 15, tzinfo=UTC)
    c.trace_exists("0" * 32, start, start + timedelta(minutes=5))
    assert seen[-1]["fromStartTime"] == "2026-10-15T10:15:00.000Z" and seen[-1]["toStartTime"] == "2026-10-17T10:20:00.000Z"
    monkeypatch.setattr(c, "dataset_id", lambda name: None)
    r = LF.LangfuseRetriever(c, now=lambda: datetime(2026, 11, 20, tzinfo=UTC))
    r.query("trace", {"trace_id": "0" * 32})
    r.query("experiment_scores", {"experiment_id": "exp-20261017-01"})
    assert all(q["fromStartTime"] == "2026-10-21T00:00:00.000Z" for q in seen[1:]) and len(seen) == 3


def test_a_rejected_span_or_score_is_a_record_error() -> None:
    assert sync._record_error(LF.Rejected("scores rejected 1 of 1"))
    assert not sync._record_error(LF.LangfuseError("not configured"))


# ---------------------------------------------------------------- provider caps, the dry run, past.dev pricing


def test_a_provider_cap_stops_the_run_without_backoff(app: str, pg_url: str) -> None:
    for status, body in ((402, "out-of-credits"), (429, '{"code":"project-cap-reached"}')):
        _reset(pg_url)

        class _Capped(_Sink):
            def push(self, batch: sync.Batch) -> None:
                raise http.HttpError(status, body)
        with psycopg.connect(app) as conn:
            rep = sync.run(_Capped(), conn, environ={})
            meta = sync.load(conn, "pastdev")
            assert (rep.status, rep.failed, rep.dead_letters) == ("capped", 0, 0), status
            assert rep.detail.startswith("provider cap: HTTP") and not meta.get("failures")
            assert "t" not in (meta.get("watermarks") or {})
    assert not sync.provider_cap(http.HttpError(429, ""))                 # a plain rate limit is not a cap


def test_plan_counts_without_pushing_and_cannot_write(app: str) -> None:
    class _Priced(_Sink):
        def cost(self, batch: sync.Batch) -> int:
            return 2 * len(batch.rows)

    s = _Priced(n=5)
    with psycopg.connect(app) as conn:
        out = sync.plan(s, conn, environ={"AMEM_SYNC_PASTDEV_MAX_ROWS_PER_DAY": "2"})
        assert s.pushed == [] and conn.read_only is False
        assert out["tables"]["t"]["rows"] == 5 and out["tables"]["t"]["batches"] == 1 and out["cost"] == 10
        assert (out["rows"], out["requests"], out["days_at_limit"]) == (5, 1, 3)

        class _Writer(_Sink):
            def batches(self, conn: psycopg.Connection, watermarks: Mapping[str, str], *, replay: bool) -> Iterator[sync.Batch]:
                conn.execute("insert into rsi_checkpoints (job, run_key, status) values ('sync:x', 'cursor', 'done')")
                yield from ()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            sync.plan(_Writer(), conn)
        assert conn.read_only is False


def test_pastdev_local_credits_follow_the_pricing_rule() -> None:
    assert PD.local_credits([{"content": "x" * 350}, {"content": "x" * 351}, {"content": ""}]) == 1 + 2 + 1
    assert PD.local_credits([{"content": "\u00e5" * 175}]) == 1 and PD.local_credits([{"content": "\u00e5" * 176}]) == 2
    s = PD.PastDevSink(PD.Config("past_sk_" + "x" * 20, "http://127.0.0.1:1"))
    assert s.cost(sync.Batch(PD.RETRACT, [{"id": "amem:proposal/x"}], "w")) == 0


def test_pastdev_estimate_and_usage_calls() -> None:
    with MockService() as svc:
        svc.route("POST", "/api/v1/ingest/estimate", lambda r: (200, {"credits": 3, "fits": True, "dataPoints": 1}))
        svc.route("GET", "/api/v1/usage", lambda r: (200, {"month": {}, "balance": {"left": 149990, "spent": 10},
                                                           "project": {"credits": 10, "creditCap": 2000}, "metered": True}))
        c = PD.Client(PD.Config("past_sk_" + "x" * 20, svc.url + "/api/v1"), sleep=lambda s: None)
        assert c.estimate([{"id": "amem:proposal/x", "content": "kind: proposal"}])["credits"] == 3
        (req,) = svc.requests_to("POST", "/api/v1/ingest/estimate")
        assert req.json == {"items": [{"id": "amem:proposal/x", "content": "kind: proposal"}], "points": "none"}
        assert c.usage()["project"]["creditCap"] == 2000
