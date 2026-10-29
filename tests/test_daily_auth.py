import json

from agent_memory import daily, deviations, distill, questions
from test_daily_support import daily_io  # noqa: F401
from test_deviations_support import setup_home, writer, fake_jev, make_all_kinds  # noqa: F401
from test_scoring_support import sdb  # noqa: F401
from test_deviations_daily import NOW


def test_auth_failure_stops_first_session_preserves_timestamp_and_retry_allowance(
    sdb, daily_io, setup_home, writer, fake_jev, monkeypatch,
):
    monkeypatch.setenv(deviations.LIVE_ENV, "1")
    monkeypatch.setenv("AMEM_QUESTIONS_DIR", str(questions.QUESTIONS_DIR))
    ids = make_all_kinds(setup_home)
    calls = writer(default=distill.WriterAuthError("dedicated automation token unavailable"))
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "FAIL"
    assert r.auth_failed and r.deviations.auth_failed
    assert len(calls.prompts) == 1
    assert len(r.deviations.outcomes) == 1
    assert r.deviations.pending == len(ids) - 1  # eval sessions are intentionally excluded
    state = json.loads(daily_io.state.read_text())
    assert daily.DEVIATIONS_KEY not in state
    from agent_memory import store
    with store.connect(sdb.url) as conn:
        rows = conn.execute("select scanned_at, meta from sessions").fetchall()
    assert all(row[0] is None and not row[1].get("scan_errors") for row in rows)
