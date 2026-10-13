"""`amem edits run` against a throwaway database: planning, the improve contract, rows, push.

The writer is a stand-in, improve is a fake script (AMEM_IMPROVE_BIN), ntfy and osascript
are stand-ins (daily_io). Nothing real is called or written outside tmp_path.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from agent_memory import distill, edits, edits_push, scoreboard
from conftest import run_amem
from test_daily_support import DailyIO, Push, daily_io  # noqa: F401
from test_edits_support import (  # noqa: F401
    SKILL_MD,
    SPAWN_NEW,
    EHome,
    apply_answer,
    edit_rows,
    ehome,
    envelope,
    ewriter,
    home_env,
    proposal,
    seed,
    sha_of,
)
from test_scoring_support import ScoringDb, sdb  # noqa: F401

NOTIFY = {"NTFY_TOPIC": "secret-topic-xyz", "NTFY_SERVER": "https://ntfy.example.test"}


@pytest.fixture
def live(sdb: ScoringDb, monkeypatch: pytest.MonkeyPatch) -> ScoringDb:  # noqa: F811
    monkeypatch.setenv(edits.LIVE_ENV, "1")
    monkeypatch.setenv("AMEM_EDITS_UNDO_URL", "https://fn.example.test/functions/v1/amem-undo")
    return sdb


def seed_spawn(url: str, ehome: EHome, **kw: Any) -> list[str]:  # noqa: F811
    return seed(url, "skill:dispatch", "spawn", "4. Spawn", sha=sha_of(ehome.skill), **kw)


# --------------------------------------------------------------------------- planning


def test_dry_run_plans_and_writes_nothing(sdb: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                          capsys: pytest.CaptureFixture[str]) -> None:
    w = ewriter()
    seed_spawn(sdb.url, ehome, n=3)
    seed(sdb.url, "claude-md:demo-repo", "whole", "Demo repo", n=2, cwd=str(ehome.repo), repo="demo-repo")
    seed(sdb.url, "parent-md", "merge", "Merge", n=2, confidence=0.4)
    seed(sdb.url, "parent-md", "safety-guards", "Safety guards", n=1, verdict="leave")
    seed(sdb.url, "claude-md:global", "unmatched", None, n=1)
    r = edits.run(dry_run=True, db_url=sdb.url)
    lines = [o.line() for o in r.outcomes]
    assert any(ln.startswith("plan edit skill:dispatch spawn -> ") and ".agents/skills/dispatch/SKILL.md" in ln
               and "edit_instruction 0.80, dev:3 deviations, 3 sessions" in ln for ln in lines), lines
    assert any(ln.startswith("plan suggest claude-md:demo-repo whole") and "repo file" in ln for ln in lines), lines
    assert any(ln.startswith("skip parent-md merge") and "confidence 0.40 < 0.60" in ln for ln in lines), lines
    assert any(ln.startswith("skip claude-md:global unmatched") for ln in lines), lines
    assert not any("safety-guards" in ln for ln in lines)          # leave: not a candidate at all
    assert r.summary().startswith("edits (dry run, nothing called or written): plan_edit=1 plan_suggest=1 skip=2")
    assert w.prompts == [] and ehome.improve.calls() == [] and edit_rows(sdb.url) == []
    assert ehome.skill.read_text() == SKILL_MD


def test_team_candidates_are_skipped(sdb: ScoringDb, ehome: EHome, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    seed_spawn(sdb.url, ehome)
    real = edits.deviations.candidates

    def with_team(**kw: Any) -> list[dict[str, Any]]:
        return [{**c, "team": True} for c in real(**kw)]

    monkeypatch.setattr(edits.deviations, "candidates", with_team)
    (o,) = edits.run(dry_run=True, db_url=sdb.url).outcomes
    assert o.status == "skip" and o.reason == "team repo: never changed"


def test_limit_defers(sdb: ScoringDb, ehome: EHome) -> None:  # noqa: F811
    seed_spawn(sdb.url, ehome, n=3)
    seed(sdb.url, "claude-md:global", "shell-habits", "Shell Habits", n=2, sha=sha_of(ehome.global_md))
    r = edits.run(dry_run=True, limit=1, db_url=sdb.url)
    assert [o.status for o in r.outcomes] == ["plan-edit"] and r.deferred == 1


# --------------------------------------------------------------------------- live edits


def test_kept_edit_full_contract(live: ScoringDb, ehome: EHome, ewriter: Any, daily_io: DailyIO,  # noqa: F811
                                 capsys: pytest.CaptureFixture[str]) -> None:
    push = daily_io.use_push(Push(status=200))
    dev_ids = seed_spawn(live.url, ehome, n=3)
    before = sha_of(ehome.skill)
    w = ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-0042"))
    r = edits.run(db_url=live.url, notify=NOTIFY)
    (o,) = r.outcomes
    assert o.status == "kept" and o.improve_id == "c-0042" and o.push == "phone", o.line()
    assert r.edits == 1 and r.kept == 1 and r.errors == 0
    # improve apply got exactly the contract's arguments.
    (call,) = ehome.improve.calls()
    assert call[0] == "apply" and call[-1] == "--json"
    opt = {call[i]: call[i + 1] for i in range(1, len(call) - 1, 2)}
    assert opt["--target"] == str(ehome.skill.resolve())
    assert opt["--rule-id"] == "skill-dispatch--spawn"
    assert opt["--topic"] == "worker spawn brief"
    assert opt["--evidence"] == "dev:3 deviations, 3 sessions"
    assert opt["--why"].startswith("Agents skipped the branch name")
    new_text = ehome.improve.last_new_file()
    assert new_text == SKILL_MD.replace("Each brief names the branch.\n",
                                        "Each brief names the branch on its first line, before the task text.\n")
    assert ehome.skill.read_text() == new_text            # the fake improve kept it
    assert len(w.prompts) == 1 and "## 4. Spawn" in w.prompts[0]
    # The row.
    (row,) = edit_rows(live.url)
    assert row["status"] == "kept" and row["improve_id"] == "c-0042"
    assert row["instruction_sha_before"] == before and row["instruction_sha_after"] == sha_of(ehome.skill)
    assert sorted(str(i) for i in row["deviation_ids"]) == sorted(dev_ids)
    assert row["target_path"].endswith(".agents/skills/dispatch/SKILL.md")
    assert row["verdict"] == "edit_instruction" and abs(row["jev_confidence"] - 0.8) < 1e-6
    assert row["summary"] == "Spawn rule now names the branch line explicitly." and row["pushed_at"] is not None
    assert row["meta"]["improve"] == {"reason": "evals ok", "revert": "improve revert c-0042"}
    assert row["meta"]["kind"] == "contract" and row["meta"]["writer_model"] == "opus"
    assert row["meta"]["kind_reason"] == "The brief must now carry the branch as a checkable first line."
    assert " kind=contract" in o.line()
    # The push: JSON to the server root, Undo action with the token; only its hash is stored.
    (post,) = push.posts
    assert post["url"] == "https://ntfy.example.test/" and post["headers"] == {"Content-Type": "application/json"}
    payload = json.loads(post["body"])
    assert payload["topic"] == "secret-topic-xyz" and payload["title"] == "Agent edit: 4. Spawn"
    assert "Spawn rule now names the branch line explicitly." in payload["message"]
    assert "Why: Agents skipped the branch name" in payload["message"] and "File: dispatch/SKILL.md" in payload["message"]
    assert "Kind: contract (The brief must now carry the branch as a checkable first line.)" in payload["message"]
    (action,) = payload["actions"]
    assert action["action"] == "http" and action["label"] == "Undo" and action["method"] == "POST"
    assert action["url"] == "https://fn.example.test/functions/v1/amem-undo"
    body = json.loads(action["body"])
    assert body["edit_id"] == str(row["id"]) and len(body["token"]) >= 16
    assert row["undo_token_hash"] == hashlib.sha256(body["token"].encode()).hexdigest()
    with psycopg.connect(live.url) as conn:
        dump = json.dumps([list(r) for r in conn.execute("select * from edits").fetchall()], default=str)
    assert body["token"] not in dump
    out = capsys.readouterr()
    assert "secret-topic-xyz" not in out.out + out.err


def test_second_run_skips_the_handled_rule(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                           daily_io: DailyIO, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    seed_spawn(live.url, ehome, n=3)
    w = ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-1"))
    edits.run(db_url=live.url, push=False)
    (o,) = edits.run(db_url=live.url, push=False).outcomes
    assert o.status == "skip" and o.reason == "already handled at this version"
    assert len(w.prompts) == 1 and len(ehome.improve.calls()) == 1
    # A new deviation on the edited text is new evidence, but the fresh kept edit holds the
    # rule in its cooldown first; without a cooldown the rule is a candidate again.
    seed(live.url, "skill:dispatch", "spawn", "4. Spawn", n=1, sha=sha_of(ehome.skill))
    (o,) = edits.run(dry_run=True, db_url=live.url).outcomes
    assert o.status == "skip" and o.reason.startswith("cooldown until ")
    monkeypatch.setenv(scoreboard.COOLDOWN_ENV, "0")
    (o,) = edits.run(dry_run=True, db_url=live.url).outcomes
    assert o.status == "plan-edit"


@pytest.mark.parametrize("answer,status", [
    (apply_answer("trial", "c-7"), "trial"),
    (apply_answer("auto-reverted", "c-8"), "auto-reverted"),
    (apply_answer("auto-reverted", "c-9", exit=1), "auto-reverted"),
    (apply_answer("refused", None, reason="weekly cap reached"), "refused"),
])
def test_improve_statuses(live: ScoringDb, ehome: EHome, ewriter: Any, daily_io: DailyIO,  # noqa: F811
                          answer: dict[str, Any], status: str) -> None:
    push = daily_io.use_push(Push(status=200))
    seed_spawn(live.url, ehome)
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", answer)
    r = edits.run(db_url=live.url, notify=NOTIFY)
    (o,) = r.outcomes
    (row,) = edit_rows(live.url)
    assert o.status == row["status"] == status
    assert r.errors == 0 and r.edits == 1
    if status == "trial":
        assert len(push.posts) == 1 and "Trial: the evals could not prove it yet." in json.loads(push.posts[0]["body"])["message"]
        assert row["instruction_sha_after"] == sha_of(ehome.skill) and row["undo_token_hash"]
    else:
        assert push.posts == [] and row["undo_token_hash"] is None and row["instruction_sha_after"] is None
        assert ehome.skill.read_text() == SKILL_MD
    if status == "auto-reverted":
        assert r.reverted == 1
    if status == "refused":
        assert o.reason == "weekly cap reached"


@pytest.mark.parametrize("answer,match", [
    ({"stdout": "improve apply: KEPT (abc)\n", "exit": 0}, "no JSON line"),
    ({"stdout": '{"id": "c-1", "status": "maybe"}\n', "exit": 0}, "unknown status"),
    ({"stdout": '{"id": "c-1", "status": "kept"}\n', "exit": 2}, "status kept with exit 2"),
    ({"stdout": '{"id": null, "status": "kept"}\n', "exit": 0}, "without a change id"),
    ({"stdout": "", "stderr": "Traceback: boom", "exit": 1}, "no JSON line"),
])
def test_improve_garbage_is_an_error_and_keeps_a_proposed_row(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                                               answer: dict[str, Any], match: str) -> None:
    seed_spawn(live.url, ehome)
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", answer)
    r = edits.run(db_url=live.url, push=False)
    (o,) = r.outcomes
    assert o.status == "proposed" and match in o.reason and r.errors == 1
    (row,) = edit_rows(live.url)
    assert row["status"] == "proposed" and match in row["meta"]["error"]
    # Not retried at this version: the row marks it handled.
    assert edits.run(dry_run=True, db_url=live.url).outcomes[0].reason == "already handled at this version"


def test_missing_improve_is_an_error(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(edits.IMPROVE_ENV, str(ehome.root / "no-improve"))
    seed_spawn(live.url, ehome)
    ewriter(envelope(proposal(SPAWN_NEW)))
    (o,) = edits.run(db_url=live.url, push=False).outcomes
    assert o.status == "proposed" and "cannot run improve" in o.reason


def test_one_failing_candidate_does_not_stop_the_others(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                                         daily_io: DailyIO) -> None:  # noqa: F811
    seed(live.url, "claude-md:global", "shell-habits", "Shell Habits", n=4, sha=sha_of(ehome.global_md))
    seed_spawn(live.url, ehome, n=3)
    ewriter(distill.WriterError("exit 1: rate limited"), envelope("not json at all"),
            default=envelope(proposal(SPAWN_NEW)))
    seed(live.url, "parent-md", "merge", "Merge", n=2, sha=None)
    ehome.improve.queue("apply", apply_answer("kept", "c-5"))
    r = edits.run(db_url=live.url, push=False)
    by = {o.rule_id: o for o in r.outcomes}
    assert by["shell-habits"].status == "error" and "writer: exit 1: rate limited" in by["shell-habits"].reason
    assert by["spawn"].status == "error" and "writer output" in by["spawn"].reason
    # The third candidate still ran; the default reply does not fit its section, so it errors too...
    assert by["merge"].status == "error" and "heading" in by["merge"].reason
    assert r.errors == 3 and edit_rows(live.url) == []


def test_errors_then_success(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    seed(live.url, "claude-md:global", "shell-habits", "Shell Habits", n=4, sha=sha_of(ehome.global_md))
    seed_spawn(live.url, ehome, n=3)
    ewriter(distill.WriterError("timed out after 300s"), envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-6"))
    r = edits.run(db_url=live.url, push=False)
    assert [o.status for o in r.outcomes] == ["error", "kept"]
    assert [row["rule_id"] for row in edit_rows(live.url)] == ["spawn"]


def test_writer_no_change_is_skipped(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    seed_spawn(live.url, ehome)
    ewriter(envelope(proposal("## 4. Spawn\nSpawn one worker per task.\nEach brief names the branch.\n")))
    (o,) = edits.run(db_url=live.url, push=False).outcomes
    assert o.status == "skipped" and ehome.improve.calls() == []
    assert edit_rows(live.url)[0]["status"] == "skipped"


def test_file_changed_while_writer_ran(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    seed_spawn(live.url, ehome)

    def reply(prompt: str) -> str:
        ehome.skill.write_text(SKILL_MD + "\n## 6. Clean\nRemove worktrees.\n")
        return envelope(proposal(SPAWN_NEW))

    ewriter(reply)
    (o,) = edits.run(db_url=live.url, push=False).outcomes
    assert o.status == "error" and "changed while the writer ran" in o.reason and ehome.improve.calls() == []


def test_suggestion_for_a_repo_file(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    w = ewriter()
    seed(live.url, "claude-md:demo-repo", "whole", "Demo repo", n=2, cwd=str(ehome.repo), repo="demo-repo")
    ehome.improve.queue("suggest", {"stdout": "improve suggest: logged\n", "exit": 0})
    r = edits.run(db_url=live.url, push=False)
    (o,) = r.outcomes
    assert o.status == "suggested" and r.suggested == 1 and w.prompts == []
    (call,) = ehome.improve.calls()
    opt = {call[i]: call[i + 1] for i in range(1, len(call) - 1, 2)}
    assert call[0] == "suggest"
    assert opt["--title"] == "Agent edit suggested: Demo repo (claude-md:demo-repo)"
    assert opt["--evidence"] == "dev:2 deviations, 2 sessions" and opt["--target"].endswith("demo-repo/CLAUDE.md")
    assert "repo file" in opt["--why"]
    assert edit_rows(live.url)[0]["status"] == "suggested"
    assert ehome.repo.joinpath("CLAUDE.md").read_text() == "# Demo repo\nRun tests with pytest.\n"
    # Not suggested twice.
    assert edits.run(db_url=live.url, push=False).outcomes[0].status == "skip"


def test_suggest_failure_is_an_error(live: ScoringDb, ehome: EHome) -> None:  # noqa: F811
    seed(live.url, "agents-md:global", "style", "Style", n=2)
    ehome.improve.queue("suggest", {"stdout": "", "stderr": "need --title", "exit": 1})
    (o,) = edits.run(db_url=live.url, push=False).outcomes
    assert o.status == "error" and "improve suggest exit 1" in o.reason and edit_rows(live.url) == []


def test_new_tables_have_rls(sdb: ScoringDb) -> None:  # noqa: F811
    with psycopg.connect(sdb.url) as conn:
        rows = dict(conn.execute("select c.relname, c.relrowsecurity from pg_class c join pg_namespace n "
                                 "on n.oid = c.relnamespace where n.nspname = 'public' and c.relname in "
                                 "('edits', 'undo_requests')").fetchall())
    assert rows == {"edits": True, "undo_requests": True}


# --------------------------------------------------------------------------- CLI


def test_cli_without_live_flag_is_a_dry_run(sdb: ScoringDb, ehome: EHome) -> None:  # noqa: F811
    seed_spawn(sdb.url, ehome)
    res = run_amem("edits", "run", env={**sdb.env, **home_env(ehome)})
    assert res.returncode == 0, res.stderr
    assert "AMEM_EDITS_LIVE is not 1, so this is a dry run" in res.stderr
    assert "plan edit skill:dispatch spawn" in res.stdout
    assert ehome.improve.calls() == [] and edit_rows(sdb.url) == []


def test_cli_list(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    seed_spawn(live.url, ehome)
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-11"))
    edits.run(db_url=live.url, push=False)
    rows = edits.list_edits(days=1, now=datetime.now(UTC) + timedelta(minutes=1), db_url=live.url)
    (line,) = [edits.list_line(r) for r in rows]
    assert " kept skill:dispatch / 4. Spawn [spawn] -> " in line and "improve=c-11 kind=contract" in line
    assert line.endswith("(contract: The brief must now carry the branch as a checkable first line.)")
    res = run_amem("edits", "list", "--days", "2", env={**live.env, **home_env(ehome)})
    assert res.returncode == 0 and "edits (last 2 days): 1" in res.stdout and " kind=contract" in res.stdout


def test_prose_kind_is_allowed_and_shown(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    seed_spawn(live.url, ehome)
    ewriter(envelope(proposal(SPAWN_NEW, kind="prose", kind_reason="No output or order to tighten here.")))
    ehome.improve.queue("apply", apply_answer("trial", "c-12"))
    (o,) = edits.run(db_url=live.url, push=False).outcomes
    assert o.status == "trial" and o.kind == "prose"
    (row,) = edit_rows(live.url)
    assert row["meta"]["kind"] == "prose"
    title, body = edits_push.edit_message(o, with_cli_undo=False)
    assert "Kind: prose (No output or order to tighten here.)" in body


# --------------------------------------------------------------------------- cooldown


def add_edit_row(url: str, status: str, created: datetime, rule: str = "spawn") -> None:
    """An earlier edit on the rule, made on another version of the file (so it is not `handled`)."""
    with psycopg.connect(url) as conn:
        conn.execute("insert into edits (status, instruction_source, rule_id, rule_title, target_path, "
                     "instruction_sha_before, improve_id, created_at) values (%s, 'skill:dispatch', %s, '4. Spawn', "
                     "'~/.agents/skills/dispatch/SKILL.md', %s, 'c-old', %s)", (status, rule, "a" * 64, created))


def quiet_sessions(url: str, n: int, started: datetime, prefix: str) -> None:
    with psycopg.connect(url) as conn:
        for i in range(n):
            conn.execute("insert into sessions (session_id, kind, project_dir, started_at, instructions, scanned_at) "
                         "values (%s, 'chat', 'p', %s, '[\"skill:dispatch\"]', now())",
                         (f"{prefix}-{i}", started + timedelta(hours=i)))


def test_cooldown_skips_a_recently_kept_rule(sdb: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    w = ewriter()
    now = datetime.now(UTC)
    created = now - timedelta(days=2)
    add_edit_row(sdb.url, "kept", created)
    seed_spawn(sdb.url, ehome, n=3)
    (o,) = edits.run(dry_run=True, db_url=sdb.url).outcomes
    until = (created + timedelta(days=7)).date().isoformat()
    assert o.status == "skip" and o.line().startswith("skip skill:dispatch spawn -> "), o.line()
    assert o.reason.startswith(f"cooldown until {until} (last edit kept {created.date().isoformat()}"), o.reason
    assert w.prompts == [] and ehome.improve.calls() == []
    # A trial edit holds the rule too; an edit older than the cooldown does not.
    with psycopg.connect(sdb.url) as conn:
        conn.execute("update edits set status = 'trial'")
    assert edits.run(dry_run=True, db_url=sdb.url).outcomes[0].reason.startswith("cooldown until ")
    with psycopg.connect(sdb.url) as conn:
        conn.execute("update edits set created_at = %s", (now - timedelta(days=8),))
    assert edits.run(dry_run=True, db_url=sdb.url).outcomes[0].status == "plan-edit"
    # The window is configurable.
    monkeypatch.setenv(scoreboard.COOLDOWN_ENV, "10")
    assert edits.run(dry_run=True, db_url=sdb.url).outcomes[0].reason.startswith("cooldown until ")
    monkeypatch.delenv(scoreboard.COOLDOWN_ENV)
    # The CLI dry run shows it.
    with psycopg.connect(sdb.url) as conn:
        conn.execute("update edits set created_at = %s", (created,))
    res = run_amem("edits", "run", "--dry-run", env={**sdb.env, **home_env(ehome)})
    assert res.returncode == 0 and "skip skill:dispatch spawn -> " in res.stdout
    assert f"cooldown until {until}" in res.stdout and "skip=1" in res.stdout


@pytest.mark.parametrize("status", ["reverted", "refused", "auto-reverted", "suggested", "skipped", "proposed"])
def test_unapplied_edits_start_no_cooldown(sdb: ScoringDb, ehome: EHome, status: str) -> None:  # noqa: F811
    add_edit_row(sdb.url, status, datetime.now(UTC) - timedelta(days=1))
    seed_spawn(sdb.url, ehome, n=3)
    (o,) = edits.run(dry_run=True, db_url=sdb.url).outcomes
    assert o.status == "plan-edit", o.line()
    assert "cooldown" not in o.reason


def test_cooldown_lifted_when_the_scoreboard_says_more(sdb: ScoringDb, ehome: EHome) -> None:  # noqa: F811
    now = datetime.now(UTC)
    created = now - timedelta(days=3)
    add_edit_row(sdb.url, "kept", created)
    # before: 3 deviations in 6 sessions (0.50); after: 5 in 5 on the new text (1.00) -> more
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=3, sha="a" * 64, started=created - timedelta(days=5))
    quiet_sessions(sdb.url, 3, created - timedelta(days=4), "before")
    seed_spawn(sdb.url, ehome, n=5, started=now - timedelta(days=1))
    (s,) = scoreboard.scores(db_url=sdb.url)
    assert s.verdict == "more" and s.cooldown_lifted and s.line().endswith("; cooldown lifted (more)")
    assert "in cooldown" not in scoreboard.text_lines([s], 28)[0]
    (o,) = edits.run(dry_run=True, db_url=sdb.url).outcomes
    assert o.status == "plan-edit", o.line()
    assert "cooldown lifted: scoreboard says more" in o.reason
    # `fewer` / `same` / `too early` keep the cooldown: 5 in 45 after (0.11) is fewer.
    quiet_sessions(sdb.url, 40, now - timedelta(days=2), "after-quiet")
    (s,) = scoreboard.scores(db_url=sdb.url)
    assert s.verdict == "fewer" and not s.cooldown_lifted and "; cooldown until " in s.line()
    assert edits.run(dry_run=True, db_url=sdb.url).outcomes[0].reason.startswith("cooldown until ")
