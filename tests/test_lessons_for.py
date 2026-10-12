"""`amem lessons for`: full-text ranking, scope filters, use recording, AMEM_MEMORY=off."""

from __future__ import annotations

import uuid

import psycopg

from agent_memory import lessons, store
from conftest import run_amem


def _word() -> str:
    """A letters-only token no other test (or real lesson) contains."""
    return "zq" + "".join(chr(97 + int(c, 16)) for c in uuid.uuid4().hex[:10])


def _lesson(slug: str, title: str, *, rule: str | None = None, evidence: str | None = None,
            repo: str | None = None, task: str | None = None, status: str = "trial") -> lessons.Lesson:
    return lessons.Lesson(slug=slug, title=title, rule=rule or title, evidence=evidence,
                          scope_repo=repo, scope_task=task, status=status)


def _uses(pg_url: str, slugs: list[str]) -> list[tuple]:
    with psycopg.connect(pg_url) as conn:
        return conn.execute(
            "select l.slug, u.run_id, u.context, u.outcome from lesson_uses u join lessons l on l.id = u.lesson_id "
            "where l.slug = any(%s) order by u.injected_at, l.slug", (slugs,),
        ).fetchall()


def _setup(tag: str, w1: str, w2: str, repo: str) -> list[str]:
    rows = [
        _lesson(f"{tag}-best", f"Check {w1} and {w2} before you commit", repo=repo, status="kept",
                evidence=f"{w1} broke twice"),
        _lesson(f"{tag}-global", f"Global note about {w1}"),
        _lesson(f"{tag}-weak", "Unrelated title", evidence=f"one mention of {w1}", repo=repo),
        _lesson(f"{tag}-other-repo", f"{w1} {w2} in another repo", repo=f"{repo}-other"),
        _lesson(f"{tag}-retired", f"{w1} {w2} retired advice", repo=repo, status="retired"),
        _lesson(f"{tag}-migrations", f"{w1} during migrations", repo=repo, task="migration"),
    ]
    lessons.upsert(rows)
    return [r.slug for r in rows]


def test_ranking_scope_and_recorded_uses(db_env: dict[str, str], pg_url: str) -> None:
    tag, w1, w2, repo = f"for-{uuid.uuid4().hex[:6]}", _word(), _word(), f"repo-{uuid.uuid4().hex[:6]}"
    slugs = _setup(tag, w1, w2, repo)
    task = f"{w1} {w2}"  # only test-owned words, so no other lesson in the shared db can match

    result = run_amem("lessons", "for", "--repo", repo, "--task", task, "--min-relative-rank", "0", env=db_env)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.startswith("## Lessons from past runs\n")
    order = [s for s in (f"{tag}-best", f"{tag}-global", f"{tag}-weak") if f"`{s}`" in out]
    assert order == [f"{tag}-best", f"{tag}-global", f"{tag}-weak"]
    assert out.index(f"`{tag}-best`") < out.index(f"`{tag}-global`") < out.index(f"`{tag}-weak`")
    for excluded in ("other-repo", "retired", "migrations"):
        assert f"`{tag}-{excluded}`" not in out
    assert f"- **Check {w1} and {w2} before you commit** (`{tag}-best`, kept)" in out

    uses = _uses(pg_url, slugs)
    assert sorted(u[0] for u in uses) == sorted([f"{tag}-best", f"{tag}-global", f"{tag}-weak"])
    assert all(u[1] is None and u[2] == task and u[3] is None for u in uses)


def test_task_scope_and_limit_and_run_link(db_env: dict[str, str], pg_url: str) -> None:
    tag, w1, w2, repo = f"for-{uuid.uuid4().hex[:6]}", _word(), _word(), f"repo-{uuid.uuid4().hex[:6]}"
    _setup(tag, w1, w2, repo)
    run_id = store.insert_run("lessons-for-run", "pass")

    task = f"run the {w1} migration"
    result = run_amem("lessons", "for", "--repo", repo, "--task", task, "--limit", "10",
                      "--run", str(run_id), env=db_env)
    assert result.returncode == 0, result.stderr
    assert f"`{tag}-migrations`" in result.stdout  # task contains its scope_task phrase
    uses = _uses(pg_url, [f"{tag}-migrations"])
    assert [(u[1], u[2]) for u in uses] == [(run_id, task)]

    one = run_amem("lessons", "for", "--repo", repo, "--task", f"{w1} {w2}", "--limit", "1", env=db_env)
    assert one.stdout.count("\n- ") == 1 and f"`{tag}-best`" in one.stdout


def test_memory_off_prints_and_records_nothing(db_env: dict[str, str], pg_url: str) -> None:
    tag, w1, w2, repo = f"for-{uuid.uuid4().hex[:6]}", _word(), _word(), f"repo-{uuid.uuid4().hex[:6]}"
    slugs = _setup(tag, w1, w2, repo)
    off = run_amem("lessons", "for", "--repo", repo, "--task", f"{w1} {w2}", env=dict(db_env, AMEM_MEMORY="off"))
    assert off.returncode == 0, off.stderr
    assert off.stdout == "" and off.stderr == ""
    assert _uses(pg_url, slugs) == []
    on = run_amem("lessons", "for", "--repo", repo, "--task", f"{w1} {w2}", env=db_env)
    assert f"`{tag}-best`" in on.stdout
    assert len(_uses(pg_url, slugs)) == on.stdout.count("\n- ") >= 1


def test_weak_tail_is_cut_by_default(db_env: dict[str, str], pg_url: str) -> None:
    # `weak` shares one word with the task, and only in its evidence: noise in a brief.
    tag, w1, w2, repo = f"for-{uuid.uuid4().hex[:6]}", _word(), _word(), f"repo-{uuid.uuid4().hex[:6]}"
    slugs = _setup(tag, w1, w2, repo)
    cut = run_amem("lessons", "for", "--repo", repo, "--task", f"{w1} {w2}", env=db_env)
    assert f"`{tag}-best`" in cut.stdout
    assert f"`{tag}-weak`" not in cut.stdout
    assert f"{tag}-weak" not in [u[0] for u in _uses(pg_url, slugs)]  # a cut hit is not recorded
    by_env = run_amem("lessons", "for", "--repo", repo, "--task", f"{w1} {w2}",
                      env=dict(db_env, AMEM_LESSONS_MIN_RELATIVE_RANK="0"))
    assert f"`{tag}-weak`" in by_env.stdout


def test_no_match_and_stopwords_only(db_env: dict[str, str]) -> None:
    for task in (f"{_word()} {_word()}", "the and of"):
        result = run_amem("lessons", "for", "--repo", "agent-memory", "--task", task, env=db_env)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""


def test_negation_keeps_query_strict(db_env: dict[str, str]) -> None:
    tag, w1, w2, repo = f"for-{uuid.uuid4().hex[:6]}", _word(), _word(), f"repo-{uuid.uuid4().hex[:6]}"
    _setup(tag, w1, w2, repo)
    result = run_amem("lessons", "for", "--repo", repo, "--task", f"{w1} -{w2}", "--limit", "10", env=db_env)
    assert f"`{tag}-global`" in result.stdout
    assert f"`{tag}-best`" not in result.stdout  # mentions the excluded word
