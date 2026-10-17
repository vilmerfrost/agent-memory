"""Mission 004C: the C3 runner (scripts/c3_pastdev.py) against a fake past.dev (C3Fake, no network, no key) and the
synthetic dataset in a fresh database: every step passes on a correct service; a missing or high cap refuses the run
before anything is sent; a wrong send set stops it unsent; a foreign recall ref, a changed source id, a charged
replay, a sticky delete, a reachable bad host, a wrong meter and a marker in the request log each fail the right
step; the budget guard stops the run (by the local rule and by the meter); --dry-run touches nothing."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import synth
from agent_memory.integrations import pastdev as PD
from integrations_mock import MockService, Request
from synth_support import fresh_db, load_script
from test_integrations_pastdev import KEY, Clock, FakePastDev

c3 = load_script("c3_pastdev")
AT = "2026-10-17T09:00:00Z"


class C3Fake(FakePastDev):
    """FakePastDev plus what C3 reads: GET /usage with both caps and a credit meter (an ingest at the local rule,
    0.1 per recall; an idempotent replay is free), the free POST /ingest/estimate (no log row), a request log
    (GET /requests, /requests/{id}) and a recall that answers from the live data points. The knobs plant faults."""

    def __init__(self, svc: MockService, *, credit_cap: float | None = 250, ingestion_cap: float | None = 100):
        super().__init__(svc)
        self.credit_cap, self.ingestion_cap = credit_cap, ingestion_cap
        self.tenths = 0                      # credits x 10 (no float drift)
        self.ingested = 0
        self.recalls = 0
        self.log: list[dict[str, Any]] = []
        self.meter = 1.0                     # a wrong meter: /usage reports credits x meter
        self.extra = 0.0                     # credits the meter adds once anything was ingested
        self.replay_charges = False          # a replay with the same key is billed again
        self.sticky = False                  # recall keeps answering deleted ids
        self.ghosts: list[str] = []          # source ids every recall answers first
        self.stored_leak = ""                # text planted in every stored request-log payload
        for method, path, h in (("GET", "/api/v1/usage", self.usage), ("POST", "/api/v1/ingest/estimate", self.estimate),
                                ("GET", "/api/v1/requests", self.request_log)):
            svc.route(method, path, self._auth(h))

    def credits(self) -> float:
        return self.tenths / 10 * self.meter + (self.extra if self.ingested else 0.0)

    def _log(self, req: Request, status: int, payload: Any) -> None:
        self.log.append({"id": f"req_{len(self.log) + 1:04d}", "method": req.method, "path": req.path,
                         "status": status, "request": {"body": req.json}, "response": {"body": payload}})

    def ingest(self, req: Request) -> Any:
        body = req.json
        replay = body.get("idempotencyKey") in self.keys
        status, receipt = super().ingest(req)
        if status in (200, 202) and (not replay or self.replay_charges):
            items = body["items"] if replay else [it for it, r in zip(body["items"], receipt["items"], strict=True)
                                                  if not r["unchanged"]]
            self.tenths += 10 * (PD.local_credits(items) if items else 0)
            self.ingested += len(items)
        self._log(req, status, receipt)
        return status, receipt

    def recall(self, req: Request) -> Any:
        self.tenths += 1
        self.recalls += 1
        pts = dict(self.points)
        if self.sticky:
            pts.update({d: {"content": "kind: edit", "label": "edit"} for d in self.deleted})
        ids = self.ghosts + sorted(pts)
        results = [{"id": str(uuid.uuid4()), "rank": i + 1, "occurredAt": AT,
                    "content": str((pts.get(sid) or {}).get("content") or "a memory"),
                    "artifact": {"id": str(uuid.uuid4()), "kind": "memory", "occurredAt": AT},
                    "sources": [{"sourceId": sid, "occurredAt": AT, "label": (pts.get(sid) or {}).get("label"),
                                 "excerpts": []}]} for i, sid in enumerate(ids)]
        ans = {"asOf": AT, "usedEvidenceTokens": 0, "results": results[:int(req.json.get("limit") or 100)]}
        self._log(req, 200, ans)
        return 200, ans

    def delete(self, req: Request) -> Any:
        status, payload = super().delete(req)
        self._log(req, status, payload)
        return status, payload

    def usage(self, req: Request) -> Any:
        c = self.credits()
        cap, icap = self.credit_cap, self.ingestion_cap
        return 200, {"month": {"from": "2026-10-01", "to": "2026-10-31", "resetsAt": "2026-11-01T00:00:00Z"},
                     "balance": {"left": 150_000 - c, "included": 150_000, "bought": 0, "rolledOver": 0, "spent": c},
                     "project": {"slug": "amem-rsi", "credits": c, "ingestions": self.ingested, "recalls": self.recalls,
                                 "creditCap": cap, "creditCapLeft": None if cap is None else cap - c,
                                 "ingestionCap": icap, "ingestionCapLeft": None if icap is None else icap - self.ingested},
                     "metered": False}

    def estimate(self, req: Request) -> Any:
        items = req.json["items"]
        return 200, {"dataPoints": len(items), "bytes": sum(len(it["content"].encode()) for it in items),
                     "credits": PD.local_credits(items), "batches": 1, "fits": True, "fitsFirst": True,
                     "balance": {"left": 150_000, "projectCapLeft": None, "ingestionCapReached": False}}

    def request_log(self, req: Request) -> Any:
        rid = req.path[len("/api/v1/requests"):].strip("/")
        if not rid:
            return 200, {"requests": [{k: e[k] for k in ("id", "method", "path", "status")} for e in reversed(self.log)]}
        e = next((e for e in self.log if e["id"] == rid), None)
        if e is None:
            return 404, {"code": "not-found", "status": 404}
        return 200, {**e, "note": self.stored_leak} if self.stored_leak else e


@pytest.fixture(scope="module")
def seeded(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    with fresh_db(pg_url) as url:
        rep = synth.seed(url, datetime.now(UTC).date(), out=tmp_path_factory.mktemp("c3-gold"))
        yield url, Path(rep["gold"])


@pytest.fixture
def fresh(seeded: tuple[str, Path]) -> tuple[str, dict[str, Any]]:
    """Every run starts like the real one: no past.dev links, no cursor, the seeded text of the changed record."""
    url, gold_path = seeded
    gold = json.loads(gold_path.read_text())
    e3 = next(r for r in synth.build(date.fromisoformat(gold["anchor"])).rows["edits"] if r["id"] == synth.uid("edit:r2"))
    with psycopg.connect(url) as conn:
        conn.execute("delete from rsi_evidence where system = 'pastdev'")
        conn.execute("delete from rsi_checkpoints where job like 'sync:%'")
        conn.execute("update edits set summary = %s where id = %s", (e3["summary"], e3["id"]))
    return url, gold


@pytest.fixture
def pdev() -> Iterator[C3Fake]:
    with MockService() as svc:
        yield C3Fake(svc)


def run(url: str, gold: dict[str, Any], fake: C3Fake, **kw: Any) -> dict[str, Any]:
    clock = Clock()                                   # waits and polls advance a fake clock: no real sleeping
    kw.setdefault("bad_url", "http://127.0.0.1:1/api/v1")
    return c3.run_c3(url, gold, environ={}, cfg=fake.cfg(), sleep=clock.sleep, clock=clock, **kw)


def _steps(rep: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in rep["steps"]}


def _failed(rep: dict[str, Any]) -> list[str]:
    return sorted(s["name"] for s in rep["steps"] if not s["ok"])


def _strict(gold: dict[str, Any]) -> list[str]:
    return [m["marker"] for m in gold["markers"] if m["policy"] != synth.DECISION]


# ---------------------------------------------------------------- the pass


def test_c3_passes_every_step_against_a_correct_fake(fresh: tuple[str, dict[str, Any]], pdev: C3Fake) -> None:
    url, gold = fresh
    rep = run(url, gold, pdev)
    steps = _steps(rep)
    assert list(steps) == list(c3.STEPS) and rep["ok"] is True, {k: v["detail"] for k, v in steps.items() if not v["ok"]}
    pd = gold["pastdev"]
    send = pd["send"]
    plan = steps["plan"]["data"]
    assert plan["send"] == len(send) == 8 and plan["plan"]["refused"] == 4 == len(pd["refuse_team"] + pd["refuse_private"])
    assert plan["estimate"]["credits"] == plan["local_credits"] >= len(send)
    (est, est_change) = pdev.svc.requests_to("POST", "/api/v1/ingest/estimate")
    assert sorted(it["id"] for it in est.json["items"]) == send                    # only the planned sanitized set
    first = pdev.ingest_requests()[:8]
    assert sorted(it["id"] for r in first for it in r.json["items"]) == send and all(len(r.json["items"]) == 1 for r in first)
    assert est.json["items"] == [it for r in first for it in r.json["items"]]       # the estimate priced what was sent
    sr = steps["sync_run"]["data"]
    assert sr["ingestions"] == 8 and sr["receipts"] == {"202": 8} and sr["links"] == 8 and sr["settled"] is True
    assert all(q["ok"] and q["source_ids"] == 8 and q["hit_at_5"] for q in steps["recall"]["data"]["queries"])
    dup = steps["duplicates"]["data"]
    assert dup["second_run"] == {"rows": 0, "sends": 0}
    assert dup["replay"] == {"requests": 8, "new_ingestions": 0, "credits": 0.0}
    assert dup["change"]["credits"] == dup["change"]["local_credits"] == dup["change"]["estimate"] >= 1
    assert est_change.json["items"][0]["id"] == dup["change"]["ref"] and "C3 duplicate check" in \
        est_change.json["items"][0]["content"]
    fp = steps["failure_path"]["data"]
    assert fp["bad_host"]["status"] == "unavailable"
    assert fp["cap"] == {"status": "capped", "sends": 1, "backoff": [], "links_unchanged": True}
    dele = steps["deletion"]["data"]
    assert dele["delete_all"] == {"deleted": 8, "gone": 0, "calls": 1} and dele["statuses"] == [202]
    assert dele["retired"] == 8 and dele["recall_rounds"] == 1 and pdev.points == {}
    m = steps["metrics"]["data"]
    assert m["credits"]["within_10pct"] and m["credits"]["recalls"] == 6 and m["ingest_s"]["n"] == 17
    assert rep["credits"]["used"] == pytest.approx(pdev.credits()) == pytest.approx(m["credits"]["local_estimate"])
    leak = steps["leak_scan"]["data"]
    assert leak["bodies"]["hits"] == [] and leak["bodies"]["blocked_by_tripwire"] == []
    assert leak["bodies"]["decision_text_markers"] > 0                       # not vacuous: decision text does go
    assert leak["request_log"]["ingest_payloads_found"] >= 8 and leak["request_log"]["hits"] == []
    answers = leak["recall_answers"]
    assert (answers["scanned"], answers["hits"]) == (6, []) and answers["decision_text_markers"] > 0
    wire = "\n".join(r.path + r.body.decode() for r in pdev.svc.requests)
    assert not [mk for mk in _strict(gold) if mk in wire] and "sk-ant-" not in wire and "@example.com" not in wire
    assert not set(pd["refuse_team"] + pd["refuse_private"]) & set(pdev.sent_ids())
    assert {r.headers["Authorization"] for r in pdev.svc.requests} == {f"Bearer {KEY}"}
    assert KEY not in json.dumps(rep) and pdev.retries == [] and rep["synthetic"] is True
    with psycopg.connect(url) as conn:
        links = PD._links(conn)
        assert len(links) == 8 and all(m["deleted"] for m in links.values())
        assert conn.execute("select count(*) from rsi_checkpoints where job = 'sync:c3-capcheck'").fetchone()[0] == 0


# ---------------------------------------------------------------- refusals before anything is sent


@pytest.mark.parametrize("caps", [{"credit_cap": None}, {"ingestion_cap": None}, {"credit_cap": 251},
                                  {"ingestion_cap": 101}])
def test_c3_refuses_a_project_without_both_caps(fresh: tuple[str, dict[str, Any]], caps: dict[str, Any]) -> None:
    url, gold = fresh
    with MockService() as svc:
        fake = C3Fake(svc, **caps)
        rep = run(url, gold, fake)
        sent = [(r.method, r.path) for r in svc.requests]
    assert rep["ok"] is False and rep["refused"] and [s["name"] for s in rep["steps"]] == ["preflight"]
    assert rep["steps"][0]["detail"].startswith("REFUSED") and sent == [("GET", "/api/v1/usage")]


def test_c3_main_exit_codes_and_other_preflight_refusals(fresh: tuple[str, dict[str, Any]], seeded: tuple[str, Path],
                                                         monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    url, gold = fresh
    gold_path = seeded[1]
    out = tmp_path / "report.json"
    with MockService() as svc:
        fake = C3Fake(svc, credit_cap=None)
        monkeypatch.setattr(c3, "BASE_URL", fake.base)
        monkeypatch.setenv(PD.KEY_ENV, KEY)
        assert c3.main(["--db-url", url, "--gold", str(gold_path), "--out", str(out)]) == 2      # no credit cap
        assert json.loads(out.read_text())["refused"] and KEY not in out.read_text()
        fake.credit_cap, fake.ingested = 250, 3                                  # prior ingestions this month
        rep = c3.run_c3(url, gold, environ={}, cfg=fake.cfg())
        assert not rep["ok"] and "already has 3 ingestion" in rep["steps"][0]["detail"] and "refused" not in rep
        assert c3.main(["--db-url", url, "--gold", str(gold_path), "--out", str(out)]) == 1
        assert not svc.requests_to("POST", "/api/v1/ingest/batch") and not svc.requests_to("POST", "/api/v1/ingest/estimate")
    unconfigured = c3.run_c3(url, gold, environ={})
    assert not unconfigured["ok"] and "not configured" in unconfigured["steps"][0]["detail"]

    def no(*a: object, **k: object) -> psycopg.Connection:
        raise AssertionError("connected")

    remote = c3.run_c3("postgresql://u:p@db.example.com/x", gold, environ={}, cfg=PD.Config(KEY, "http://127.0.0.1:9"),
                       connect=no)
    assert remote["refused"] and [s["name"] for s in remote["steps"]] == ["preflight"]
    assert c3.main(["--db-url", "postgresql://u:p@db.example.com/x", "--gold", str(gold_path)]) == 2
    assert c3.main(["--db-url", url, "--gold", str(tmp_path / "missing.json")]) == 2
    with pytest.raises(ValueError):
        c3.run_c3(url, gold, environ={}, max_credits=101)


def test_c3_a_wrong_send_set_stops_the_run_before_anything_is_sent(fresh: tuple[str, dict[str, Any]],
                                                                   pdev: C3Fake) -> None:
    url, gold = fresh
    bad = copy.deepcopy(gold)
    bad["pastdev"]["send"] = sorted(bad["pastdev"]["send"] + [f"amem:edit/{uuid.UUID(int=0)}"])
    rep = run(url, bad, pdev)
    steps = _steps(rep)
    assert steps["preflight"]["ok"] and not steps["plan"]["ok"] and "send set differs" in steps["plan"]["detail"]
    assert all(steps[n]["detail"].startswith("skipped: plan failed") for n in c3.STEPS[2:-1])
    assert not steps["leak_scan"]["ok"] and "request log not read" in steps["leak_scan"]["detail"]
    assert [r for r in pdev.svc.requests if r.method == "POST"] == []           # no estimate, no ingest


def test_c3_tripwire_blocks_a_strict_marker_before_it_is_sent(fresh: tuple[str, dict[str, Any]], pdev: C3Fake) -> None:
    """The sanitizer does not know ZZLEAK markers; if one reached a record that is sent (here: a strict marker in an
    own edit's summary), the outgoing-body scan stops the run before the estimate leaves."""
    url, gold = fresh
    marker = _strict(gold)[0]
    with psycopg.connect(url) as conn:
        conn.execute("update edits set summary = %s where id = %s", (f"SYNTHETIC: planted {marker}", synth.uid("edit:r2")))
    rep = run(url, gold, pdev)
    steps = _steps(rep)
    assert not steps["plan"]["ok"] and "leak tripwire" in steps["plan"]["detail"] and "leak tripwire" in rep["stopped"]
    assert steps["leak_scan"]["data"]["bodies"]["blocked_by_tripwire"] and not steps["leak_scan"]["ok"]
    assert all(steps[n]["detail"].startswith("skipped: leak tripwire") for n in c3.STEPS[2:-1])
    assert [r for r in pdev.svc.requests if r.method == "POST"] == []
    assert marker not in "\n".join(r.body.decode() for r in pdev.svc.requests)


# ---------------------------------------------------------------- each step's fail case


def test_c3_catches_a_changed_source_id(fresh: tuple[str, dict[str, Any]], pdev: C3Fake) -> None:
    url, gold = fresh
    ingest = pdev.ingest

    def upper(req: Request) -> Any:
        status, receipt = ingest(req)
        if status in (200, 202):
            receipt = {**receipt, "items": [{**it, "sourceId": it["sourceId"].upper()} for it in receipt["items"]]}
        return status, receipt

    pdev.svc.route("POST", "/api/v1/ingest/batch", pdev._auth(upper))
    steps = _steps(run(url, gold, pdev))
    assert not steps["sync_run"]["ok"] and "sourceId differs" in steps["sync_run"]["detail"]
    assert steps["plan"]["ok"]


def test_c3_catches_a_foreign_recall_ref_a_sticky_delete_and_a_reachable_bad_host(
        fresh: tuple[str, dict[str, Any]], pdev: C3Fake) -> None:
    url, gold = fresh
    ghost = f"amem:proposal/{uuid.uuid4()}"
    pdev.ghosts, pdev.sticky = [ghost], True
    with MockService() as other:                       # answers health for any key: the "bad" host is reachable
        other.route("GET", "/api/v1/ingest/", lambda r: (404, {"code": "not-found", "status": 404}))
        rep = run(url, gold, pdev, bad_url=other.url + "/api/v1")
        keys = {r.headers.get("Authorization") for r in other.requests}
    steps = _steps(rep)
    assert _failed(rep) == ["deletion", "failure_path", "recall"], {k: v["detail"] for k, v in steps.items()}
    assert all(q["foreign"] == [ghost] for q in steps["recall"]["data"]["queries"])
    assert steps["deletion"]["data"]["recall_rounds"] == 3 and steps["deletion"]["data"]["still_returned"]
    assert "bad host: status=ok" in steps["failure_path"]["detail"] and keys == {f"Bearer {c3.DUMMY_KEY}"}


def test_c3_catches_a_charged_replay_and_a_marker_in_the_request_log(fresh: tuple[str, dict[str, Any]],
                                                                       pdev: C3Fake) -> None:
    url, gold = fresh
    marker = _strict(gold)[0]
    pdev.replay_charges, pdev.stored_leak = True, f"note {marker}"
    rep = run(url, gold, pdev)
    steps = _steps(rep)
    assert _failed(rep) == ["duplicates", "leak_scan", "metrics"], {k: v["detail"] for k, v in steps.items()}
    assert "replay" in steps["duplicates"]["detail"] and steps["duplicates"]["data"]["replay"]["credits"] > 0
    assert not steps["metrics"]["data"]["credits"]["within_10pct"]
    leak = steps["leak_scan"]["data"]
    assert leak["request_log"]["hits"] == [marker] and leak["bodies"]["hits"] == [] and leak["recall_answers"]["hits"] == []


def test_c3_catches_a_meter_that_disagrees_with_the_estimate(fresh: tuple[str, dict[str, Any]], pdev: C3Fake) -> None:
    url, gold = fresh
    pdev.meter = 1.5
    rep = run(url, gold, pdev)
    steps = _steps(rep)
    assert "metrics" in _failed(rep) and "OUTSIDE" in steps["metrics"]["detail"]
    assert "changed record" in steps["duplicates"]["detail"]          # the exact-cost check sees it too
    assert steps["sync_run"]["ok"] and steps["deletion"]["ok"] and steps["leak_scan"]["ok"]


# ---------------------------------------------------------------- the budget guard


def test_c3_budget_guard_stops_before_the_local_rule_passes_the_limit(fresh: tuple[str, dict[str, Any]],
                                                                       pdev: C3Fake) -> None:
    url, gold = fresh
    rep = run(url, gold, pdev, max_credits=3)
    steps = _steps(rep)
    assert steps["plan"]["ok"] and not steps["sync_run"]["ok"] and "budget guard" in steps["sync_run"]["detail"]
    assert all(steps[n]["detail"].startswith("skipped: budget guard") for n in c3.STEPS[3:-1])
    assert "budget guard" in rep["stopped"] and pdev.credits() <= 3 and 1 <= len(pdev.ingest_requests()) < 8
    assert not pdev.svc.requests_to("POST", "/api/v1/recall") and not pdev.svc.requests_to("POST", "/api/v1/data-points/delete")
    assert steps["leak_scan"]["data"]["bodies"]["hits"] == []          # what did go was still scanned


def test_c3_budget_guard_trips_on_the_meter(fresh: tuple[str, dict[str, Any]], pdev: C3Fake) -> None:
    url, gold = fresh
    pdev.extra = 150.0                                     # the meter jumps once anything was ingested
    rep = run(url, gold, pdev)
    steps = _steps(rep)
    assert not steps["sync_run"]["ok"] and "/usage reports" in rep["stopped"]
    assert len(pdev.ingest_requests()) == 1                # read before every spending call: stopped at the second
    assert all(steps[n]["detail"].startswith("skipped") for n in c3.STEPS[3:-1])
    n = len(pdev.svc.requests)
    assert pdev.svc.requests[n - 1].path == "/api/v1/usage"     # nothing was sent after the meter read


# ---------------------------------------------------------------- dry run and units


def test_c3_dry_run_touches_nothing(seeded: tuple[str, Path], capsys: pytest.CaptureFixture[str],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    url, gold_path = seeded
    with MockService() as svc:
        monkeypatch.setattr(c3, "BASE_URL", svc.url + "/api/v1")
        monkeypatch.setenv(PD.KEY_ENV, KEY)
        monkeypatch.setattr(c3.psycopg, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("db")))
        assert c3.main(["--db-url", url, "--gold", str(gold_path), "--dry-run"]) == 0
        text = capsys.readouterr().out
        assert c3.main(["--db-url", url, "--gold", str(gold_path), "--dry-run", "--json"]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert svc.requests == []
    assert [s["name"] for s in plan["steps"]] == list(c3.STEPS) and plan["pastdev"]["key_set"] is True
    assert (plan["send"], plan["refuse_team"], plan["refuse_private"]) == (8, 2, 2) and plan["markers"]["strict"] > 400
    assert plan["credits_upper_bound"] <= c3.MAX_RUN_CREDITS
    assert KEY not in text + json.dumps(plan) and "postgres:postgres" not in text and "nothing will be touched" in text
    assert c3.main(["--db-url", "postgresql://u:p@db.example.com/x", "--gold", str(gold_path), "--dry-run"]) == 2
    assert not (gold_path.parent / "c3-report.json").exists()


def test_needles_allow_decision_text_and_catch_everything_else() -> None:
    team = f"amem:edit/{uuid.UUID(int=7)}"
    gold = {"markers": [{"marker": "ZZLEAK-0001", "policy": synth.STRICT},
                        {"marker": "ZZLEAK-0002", "policy": synth.DECISION}],
            "pastdev": {"refuse_team": [team], "refuse_private": []}}
    n = c3.Needles.from_gold(gold, KEY)
    assert n.scan("hypothesis: ZZLEAK-0002 cuts deviations") == ([], 1)
    assert n.scan("ZZLEAK-0001 zzleak ZZLEAK-9999")[0] == ["ZZLEAK-0001", "zzleak", "ZZLEAK-9999"]
    assert n.scan("mail sam@EXAMPLE.com token sk-ant-api03")[0] == ["sk-ant-", "@example.com"]
    assert n.scan(f"id {team.split('/')[1]}")[0] == [team] and n.scan(team)[0] == [team, team]
    assert n.scan(f"Bearer {KEY}")[0] == [] and n.scan(f"x {KEY}", outgoing=True)[0] == ["the project key"]
    assert c3._pct([], 0.5) is None and c3._pct([3.0, 1.0, 2.0], 0.5) == 2.0
    assert c3._pct([float(i) for i in range(1, 21)], 0.95) == 19.0


def test_the_guard_charges_only_new_ingestions() -> None:
    g = c3.Guard(10)
    item = {"id": "amem:edit/x", "content": "x" * 351}                    # 2 credits
    g.charge("POST", "/ingest/batch", {"items": [item]}, {"ingestionId": "i1", "items": [{"unchanged": False}]})
    g.charge("POST", "/ingest/batch", {"items": [item]}, {"ingestionId": "i1", "items": [{"unchanged": False}]})
    g.charge("POST", "/ingest/batch", {"items": [item]}, {"ingestionId": "i2", "unchanged": True, "items": [{}]})
    g.charge("POST", "/recall", {"query": "q"}, {"results": []})
    assert g.spent == pytest.approx(2.1) and g.cost("POST", "/ingest/batch", {"items": [item]}) == 2.0
    g.baseline = 5.0
    g.observe(9.0)
    assert g.used == 4.0 and g.stopped is None
    with pytest.raises(c3.Stop, match="budget guard"):
        g.before(7.0)
    with pytest.raises(c3.Stop):
        g.before(0.0)                                                     # stopped: nothing more is sent
