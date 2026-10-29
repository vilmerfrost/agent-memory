"""Usage seed for dispatch estimates: Codex tokens per 1% from rollouts, tokens per round
from the sessions table. Real records only: every number traces to a source record, and
thin data gives "no usage history", never a default."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import dispatch_estimates as D, dispatch_seed as S, dispatch_store
from conftest import run_amem
from synth_support import fresh_db
from test_dispatch_estimates import add_run, cli_env, home  # noqa: F401  (fixture)

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
R1 = 1793777442          # weekly window 1 (resets_at epoch, as Codex writes it)
R2 = R1 + 7 * 86400      # the next week


@pytest.fixture(autouse=True)
def _no_codex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMEM_CODEX_HOME", raising=False)     # the default follows the tmp Claude home


def ev(minute: float, total: tuple[int, int, int] | None = None, pct: float | None = None, *,
       resets: float = R1, window: int = 10080, ordinal: int | None = None) -> dict:
    """One `token_count` event: total = cumulative (input, cached input, output)."""
    info = None if total is None else {"total_token_usage": {
        "input_tokens": total[0], "cached_input_tokens": total[1], "output_tokens": total[2],
        "reasoning_output_tokens": 0, "total_tokens": total[0] + total[2]}}
    limits = None if pct is None else {"limit_id": "codex", "secondary": None,
                                       "primary": {"used_percent": pct, "window_minutes": window,
                                                   "resets_at": resets}}
    return {"timestamp": D.iso(T0 + timedelta(minutes=minute)), "ordinal": ordinal, "type": "event_msg",
            "payload": {"type": "token_count", "info": info, "rate_limits": limits}}


def write_rollout(codex: Path, name: str, events: list[dict], meta: dict | None = None,
                  sub: str = "sessions") -> Path:
    path = codex / sub / "2026" / "10" / "20" / f"rollout-2026-10-20T09-00-00-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"timestamp": D.iso(T0), "ordinal": 0, "type": "session_meta", "payload": meta or {"id": name}}]
    lines += [{"timestamp": D.iso(T0), "type": "response_item", "payload": {"type": "message"}}]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines + events))
    return path


def full_fixture(codex: Path) -> None:
    """Overlapping sessions, a session without rate_limits, a forked copy, a window reset
    and a short (<3 point) tail. Expected intervals (fresh tokens per 1%):
      week 1: 11%@10 -> 14%@50  a+b+c = 550+2300+550+2000+3300 = 8700 / 3 = 2900
              14%@50 -> 17%@70  a+e   = 1100+550+2200          = 3850 / 3 = 1283
              17%@70 -> 18%@80  1 point: dropped
      week 2:  2%@110 -> 5%@120 d     = 3000                   = 3000 / 3 = 1000
    """
    write_rollout(codex, "a", [ev(0, (1000, 0, 100), 10), ev(10, (3000, 1000, 300), 11),
                               ev(30, (6000, 2000, 600), 13), ev(50, (9000, 2000, 900), 14),
                               ev(60, (10000, 2000, 1000), 15), ev(70, (12000, 2000, 1200), 17),
                               ev(80, (14000, 2000, 1400), 18)])
    write_rollout(codex, "b", [ev(20, (500, 0, 50), 12, resets=R1 + 2),     # Vitals-style jitter: same window
                               ev(40, (1500, 500, 100), 13)])
    write_rollout(codex, "c", [ev(45, (2000, 0, 0))])                       # no rate_limits at all
    # A forked subagent: ordinals below 5 copy the parent's history (counted once, in "a").
    write_rollout(codex, "e", [ev(50, (9000, 2000, 900), 14, ordinal=3), ev(50, (9000, 2000, 900), 14, ordinal=4),
                               ev(65, (9500, 2000, 950), 15, ordinal=5)],
                  meta={"id": "e", "subagent_history_start_ordinal": 5})
    write_rollout(codex, "d", [ev(100, (1000, 0, 0), 1, resets=R2), ev(110, (2000, 0, 0), 2, resets=R2),
                               ev(120, (5000, 0, 0), 5, resets=R2)], sub="archived_sessions")


def walk_numbers(seed: dict) -> None:
    """No number without a source record."""
    rate = seed["codex_rate"]
    for entry in rate["limits"].values():
        for iv in entry["intervals"]:
            assert iv["start"]["file"] and iv["end"]["file"] and iv["start"]["at"] < iv["end"]["at"]
            assert iv["token_events"] >= 1 and iv["rollouts"] >= 1 and iv["fresh"] > 0
            assert iv["points"] >= S.MIN_POINTS
        if entry["fresh_per_pct"] is not None:
            assert entry["n"] >= D.MIN_SEED_INTERVALS == len(entry["intervals"])
            assert entry["source"] == S.CODEX_SOURCE and entry["method_version"] == S.SEED_METHOD
        else:
            assert entry["total_per_pct"] is None and "too thin" in entry["why_none"]
    for tool, table in seed["round_tokens"].items():
        assert table["source"] == S.ROUNDS_SOURCE and table["method_version"] == S.SEED_METHOD
        for b, stats in table["buckets"].items():
            mine = [r for r in table["rounds"] if b == "all" or r["bucket"] == b]
            assert stats["n"] == len(mine) >= 1 and all(r["worker_rows"] >= 1 for r in mine)


# ---------------------------------------------------------------- Codex rollouts


def test_codex_rate_uses_one_global_timeline(tmp_path: Path) -> None:
    codex = tmp_path / ".codex"
    full_fixture(codex)
    rate = S.codex_rate(codex)
    assert rate["rollouts"] == 5 and rate["unreadable"] == 0
    week = rate["limits"]["codex:week"]
    assert week["windows"] == 2 and week["dropped_short"] == 1 and week["dropped_no_local_tokens"] == 0
    got = [(iv["start"]["pct"], iv["end"]["pct"], iv["fresh"], iv["fresh_per_pct"], iv["total_per_pct"],
            iv["rollouts"]) for iv in week["intervals"]]
    assert got == [(11, 14, 8700, 2900, 3400, 3), (14, 17, 3850, 1283, 1283, 2), (2, 5, 3000, 1000, 1000, 1)]
    first = week["intervals"][0]
    assert first["start"]["file"].endswith("-a.jsonl") and first["start"]["at"] == "2026-10-20T09:10:00Z"
    assert first["end"]["at"] == "2026-10-20T09:50:00Z" and first["token_events"] == 5
    assert week["fresh_per_pct"] == {"median": 1283, "p20": 1113, "p80": 2253}
    assert week["total_per_pct"] == {"median": 1283, "p20": 1113, "p80": 2553}
    walk_numbers({"codex_rate": rate, "round_tokens": {}})


def test_thin_rollouts_give_no_usage_history(home: Path) -> None:
    codex = home.parent / ".codex"           # the default: next to the Claude home
    write_rollout(codex, "a", [ev(0, (1000, 0, 0), 10), ev(5, (2000, 0, 0), 11), ev(20, (9000, 0, 0), 14),
                               ev(30, (9500, 0, 0), 15)])
    write_rollout(codex, "q", [ev(40, (100, 0, 0))])                        # tokens, no percent at all
    lookup = D.refresh()
    week = lookup["seed"]["codex_rate"]["limits"]["codex:week"]
    assert week["n"] == 1 and week["fresh_per_pct"] is None and week["intervals"][0]["fresh"] == 7000
    assert any("codex:week: no usage history (too thin" in n for n in lookup["notes"])
    walk_numbers(lookup["seed"])
    line = D.start("demo", 2, tool="codex", session="codex:t")["line"]
    assert "Codex week use: no usage history, now 63% used" in line


def test_no_rollouts_and_no_rate_limits_give_no_numbers(tmp_path: Path) -> None:
    assert S.codex_rate(tmp_path / "missing") == {
        "source": S.CODEX_SOURCE, "method_version": S.SEED_METHOD, "home": str(tmp_path / "missing"),
        "rollouts": 0, "unreadable": 0, "token_events": 0, "limits": {}}
    codex = tmp_path / ".codex"
    write_rollout(codex, "a", [ev(i, (1000 * (i + 1), 0, 0)) for i in range(10)])
    rate = S.codex_rate(codex)
    assert rate["token_events"] == 10 and rate["limits"] == {}
    assert "no rate limits seen" in D.seed_line({"codex_rate": rate})


def test_counter_restart_and_bad_lines_are_handled(tmp_path: Path) -> None:
    codex = tmp_path / ".codex"
    path = write_rollout(codex, "a", [ev(0, (5000, 0, 0), 10), ev(10, (100, 0, 0), 11),   # restart: 100 new
                                      ev(20, (400, 0, 0), 12), ev(30, (900, 0, 0), 14)])
    with path.open("a") as out:
        out.write('{"type":"event_msg","payload":{"type":"token_count"\n')               # torn line
        out.write(json.dumps(ev(40, (-5, 0, 0), 20)) + "\n")                             # negative: no tokens
    deltas, obs = S.scan_rollout(path, "a")
    assert [d[1] for d in deltas] == [5000, 100, 300, 500]
    assert [o[3] for o in obs] == [10, 11, 12, 14, 20]


# ---------------------------------------------------------------- estimates and lines


def seeded_lookup(rate: dict, rounds: dict) -> dict:
    return {"v": D.SCHEMA, "time": {}, "usage": {}, "tokens": {},
            "seed": {"codex_rate": rate, "round_tokens": rounds}}


def test_estimate_divides_round_tokens_by_tokens_per_pct(tmp_path: Path) -> None:
    codex = tmp_path / ".codex"
    full_fixture(codex)
    rounds = {"codex": {"buckets": {"2": {"n": 3, "median_fresh": 12830, "p80_fresh": 20528}}},
              "claude": {"buckets": {"2": {"n": 2, "median_fresh": 5}, "all": {"n": 3, "median_fresh": 1_200_000,
                                                                              "p80_fresh": 1_500_000}}}}
    lookup = seeded_lookup(S.codex_rate(codex), rounds)
    est = D.estimate(lookup, "codex", 2)
    assert est["usage"]["codex:week"] == {"pct": 10.0, "slow_pct": 16.0, "method": "seed", "basis": 3,
                                          "rounds": 3, "source": "Codex rollouts"}
    assert ", ~10% of Codex week (seed: Codex rollouts, 3 intervals x 3 rounds)" in D.expect_line(est, "codex")
    claude = D.estimate(lookup, "claude", 2)       # bucket 2 has 2 rounds: the pooled rounds answer
    assert claude["usage"] == {} and claude["tokens_typical"]["scope"] == "all sizes"
    assert ("Claude 5h use: no % history yet, ~1.2M fresh tokens typical (3 rounds), now 25% used"
            in D.expect_line(claude, "claude", {"claude:session": 25}))
    no_rounds = D.estimate(seeded_lookup(lookup["seed"]["codex_rate"], {}), "codex", 2)
    assert no_rounds["usage"] == {}
    assert ("Codex week use: no round token history yet, 1% ~ 1k fresh tokens (seed: Codex rollouts, 3 intervals)"
            in D.expect_line(no_rounds, "codex"))
    thin = D.estimate(seeded_lookup(lookup["seed"]["codex_rate"], {"codex": {"buckets": {
        "2": {"n": 2, "median_fresh": 9000}, "all": {"n": 2, "median_fresh": 9000}}}}), "codex", 2)
    assert thin["usage"] == {} and thin["tokens_typical"] is None


def test_clean_runs_override_the_seed(home: Path) -> None:
    codex = home.parent / ".codex"
    full_fixture(codex)
    lookup = D.refresh()
    lookup["seed"]["round_tokens"] = {"codex": {"buckets": {"2": {"n": 3, "median_fresh": 12830}}}}
    assert D.estimate(lookup, "codex", 2)["usage"]["codex:week"]["method"] == "seed"
    for day in range(5):
        add_run(home / "parent", tool="codex", workers=2, used=(10, 12), day=day)
    lookup = D.refresh()
    lookup["seed"]["round_tokens"] = {"codex": {"buckets": {"2": {"n": 3, "median_fresh": 12830}}}}
    usage = D.estimate(lookup, "codex", 2)["usage"]["codex:week"]
    assert usage == {"pct": 2.0, "slow_pct": 2.0, "method": "clean_runs", "basis": 5}


def test_start_and_end_never_scan_rollouts(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    full_fixture(home.parent / ".codex")
    D.refresh()

    def boom(*_: Any, **__: Any) -> None:
        raise AssertionError("start/end must not scan rollouts")

    monkeypatch.setattr(S, "codex_rate", boom)
    monkeypatch.setattr(S, "rollout_files", boom)
    monkeypatch.setattr(S, "scan_rollout", boom)
    result = D.start("demo", 2, tool="codex", session="codex:x")
    assert "no round token history yet, 1% ~ 1k fresh tokens" in result["line"]
    assert D.end(result["run_id"])["ended"]


def test_cli_refresh_flags_and_timing(home: Path, tmp_path: Path) -> None:
    codex = tmp_path / "elsewhere"
    full_fixture(codex)
    env = cli_env(home, tmp_path, AMEM_DB_URL="postgresql://nobody@127.0.0.1:1/none")
    out = run_amem("dispatch", "refresh", "--no-db", "--codex-home", str(codex), env=env)
    assert out.returncode == 0, out.stderr
    assert "Seed: codex:week 1% = 1k fresh tokens (p20 1k, p80 2k), 1k total (3 intervals, 5 rollouts)" in out.stdout
    assert "note: database not used (--no-db)" in out.stdout and "refresh took " in out.stdout
    saved = json.loads((home / "parent" / D.LOOKUP).read_text())
    assert saved["seed"]["codex_rate"]["limits"]["codex:week"]["n"] == 3
    assert {"time", "usage", "tokens", "runs", "v"} <= set(saved)
    skipped = run_amem("dispatch", "refresh", "--no-db", "--no-codex", env=env)
    assert skipped.returncode == 0 and json.loads((home / "parent" / D.LOOKUP).read_text())["seed"]["codex_rate"] is None


# ---------------------------------------------------------------- tokens per round (database)


@pytest.fixture(scope="module")
def db(pg_url: str):
    with fresh_db(pg_url) as url:
        yield url


def add_worker(conn: psycopg.Connection, sid: str, parent: str, tool: str, at: datetime, fresh: int) -> None:
    conn.execute("insert into sessions(session_id,kind,tool,project_dir,started_at,meta,tokens_input,"
                 "tokens_output,tokens_cache_creation,tokens_cache_read,tokens_updated_at) "
                 "values (%s,'worker',%s,'p',%s,%s::jsonb,%s,1,2,1000,now())",
                 (sid, tool, at, json.dumps({"parent_session": parent}), fresh - 3))


def write_rounds(parent: Path, rounds: list[tuple[str, int, int, list[int]]]) -> None:
    """(session, day, workers, worker fresh tokens): one tasks.jsonl line per round."""
    path = parent / "demo" / "tasks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        for session, day, workers, _ in rounds:
            start = T0 + timedelta(days=day)
            out.write(json.dumps({"session": session, "repo": "demo", "n_workers": workers,
                                  "started": D.iso(start), "ended": D.iso(start + timedelta(minutes=40))}) + "\n")


def test_round_tokens_from_the_sessions_table(db: str, home: Path) -> None:
    parent = home / "parent"
    full_fixture(home.parent / ".codex")
    rounds = [("par1", 0, 3, [600_000, 600_000]), ("par2", 1, 3, [1_000_000]), ("par3", 2, 4, [1_500_000]),
              ("par4", 3, 3, []),                                           # no worker rows: skipped
              ("codex:p1", 4, 2, [12830]), ("codex:p2", 5, 2, [6000, 415]), ("codex:p3", 6, 2, [25660])]
    write_rounds(parent, rounds)
    with psycopg.connect(db, autocommit=True) as conn:
        for session, day, _, fresh in rounds:
            tool = "codex" if session.startswith("codex:") else "claude"
            for i, tokens in enumerate(fresh):
                add_worker(conn, f"{session}-w{i}", session, tool, T0 + timedelta(days=day, minutes=5 + i), tokens)
        add_worker(conn, "late", "par1", "claude", T0 + timedelta(hours=5), 9_999_999)   # outside the round
        lookup = D.refresh(db=dispatch_store.DbSource(conn))
    seed = lookup["seed"]
    claude = seed["round_tokens"]["claude"]
    assert claude["buckets"]["3-4"] == {"n": 3, "median_fresh": 1_200_000, "p80_fresh": 1_380_000,
                                        "median_cache_read": 1000}
    assert [r["session"] for r in claude["rounds"]] == ["par1", "par2", "par3"]
    assert claude["rounds"][0]["worker_rows"] == 2 and claude["rounds"][0]["cache_read"] == 2000
    assert seed["round_tokens"]["codex"]["buckets"]["2"]["median_fresh"] == 12830
    assert "round tokens: 1 round(s) without worker rows skipped" in lookup["notes"]
    walk_numbers(seed)
    line = D.start("demo", 3, session="new")["line"]
    assert "Claude 5h use: no % history yet, ~1.2M fresh tokens typical (3 rounds), now 25% used" in line
    codex_line = D.start("demo", 2, tool="codex", session="codex:new")["line"]
    assert ", ~10% of Codex week (seed: Codex rollouts, 3 intervals x 3 rounds), now 63% used" in codex_line
    est = [r for r in json.loads("[" + ",".join((parent / "demo" / D.SPOOL).read_text().splitlines()) + "]")
           if r["type"] == "estimate"][-1]
    assert est["est_usage"] == {"codex:week": 10.0} and est["usage_method"] == {"codex:week": "seed"}

    offline = D.refresh()                       # no database: keep the last result, say so
    assert offline["seed"]["round_tokens"] == seed["round_tokens"]
    assert "round tokens skipped: no database" in offline["notes"]
    assert any(n.startswith("round tokens kept from ") for n in offline["notes"])

    class Broken:
        def test_medians(self) -> dict:
            return {}

        def tokens(self, run: dict) -> None:
            raise psycopg.OperationalError("gone")

    broken = D.refresh(db=Broken())
    assert "round tokens skipped: OperationalError" in broken["notes"]
    assert broken["seed"]["round_tokens"] == seed["round_tokens"]
