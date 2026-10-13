"""Rule catalog (rule_catalog.py) and its use in the deviation finder: parsing, snapping,
the writer's rule list, recurrence across spellings, `deviations reref` and the migration.

All instruction files here are synthetic markdown written by the tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest

from agent_memory import deviations, rule_catalog, sessions_ingest
from agent_memory.rule_catalog import Rule, build, parse_markdown, snap
from conftest import base_env, run_amem
from test_deviations_support import (  # noqa: F401
    NOW,
    FakeJev,
    FakeWriter,
    Home,
    envelope,
    fake_jev,
    finding,
    make_all_kinds,
    setup_home,
    writer,
)

Writer = Callable[..., FakeWriter]

PARENT_LIKE = """# Parent role

You coordinate workers.

## Modes
Talk or work.

## Safety guards
- Never read secret files yourself.
- Never push main.

## DoD
Every brief has one.
"""

DISPATCH_LIKE = """---
name: dispatch
description: parent work mode
---

# Dispatch

## 0. Lock
Take the lock.

```bash
# not a heading: inside a fence
```

## 4. Spawn
Spawn workers.

### Brief
A brief has a task.

#### Deep detail
Folds into Brief.

### Siblings
List them.

## 9. Clean
Remove worktrees.

## 10. Record
Write state.
"""


def _ids(rules: list[Rule]) -> list[str]:
    return [r.id for r in rules]


# --------------------------------------------------------------------------- parsing


def test_title_intro_and_level_two_rules() -> None:
    rules = parse_markdown(PARENT_LIKE)
    assert [(r.id, r.title) for r in rules] == [
        ("intro", "Parent role (intro)"), ("modes", "Modes"), ("safety-guards", "Safety guards"), ("dod", "DoD")]


def test_numbered_and_nested_headings_frontmatter_and_fences() -> None:
    rules = build([DISPATCH_LIKE])
    assert [(r.id, r.title) for r in rules] == [
        ("lock", "0. Lock"),
        ("spawn", "4. Spawn"),
        ("spawn/brief", "4. Spawn > Brief"),
        ("spawn/siblings", "4. Spawn > Siblings"),
        ("clean", "9. Clean"),
        ("record", "10. Record"),
    ]  # no intro (frontmatter is not text), the fenced `#` is no heading, the #### folds into Brief


def test_no_headings_is_one_whole_rule() -> None:
    assert build(["Run the suite every night.\nReport failures.\n"]) == [Rule("whole", "(whole file)")]
    assert build(["# Nightly check\nRun the suite.\n"]) == [Rule("whole", "Nightly check")]


def test_duplicate_titles_get_unique_ids() -> None:
    text = "## Notes\na\n## Rules\n### Notes\nb\n## Notes\nc\n"
    assert _ids(build([text])) == ["notes", "rules", "rules/notes", "notes-2"]


def test_several_top_headings_are_all_rules() -> None:
    assert _ids(build(["# One\na\n# Two\n## Two a\nb\n"])) == ["one", "two", "two/two-a"]


def test_multi_file_source_keeps_ids_unique() -> None:
    rules = build(["# Demo repo\nRun tests.\n", "# Local\nNever touch the hosted DB.\n"])
    assert [(r.id, r.title) for r in rules] == [("whole", "Demo repo"), ("whole-2", "Local")]


def test_slug_drops_step_numbers_and_punctuation() -> None:
    assert rule_catalog.slug("0b. Profile") == "profile"
    assert rule_catalog.slug("Step 3: Verify") == "verify"
    assert rule_catalog.slug("Vilmer's `QOL` Commands") == "vilmers-qol-commands"
    assert rule_catalog.slug("4.") == "4"
    assert rule_catalog.slug("2026 roadmap") == "2026-roadmap"


def test_sha_changes_with_the_text() -> None:
    a = rule_catalog.sha([PARENT_LIKE])
    assert len(a) == 64 and a == rule_catalog.sha([PARENT_LIKE]) and a != rule_catalog.sha([PARENT_LIKE + "x"])


# --------------------------------------------------------------------------- snapping


def test_snap_exact_id() -> None:
    m = snap("safety-guards", build([PARENT_LIKE]))
    assert (m.rule_id, m.rule_title, m.how, m.score) == ("safety-guards", "Safety guards", "exact", 1.0)
    assert snap("SPAWN/BRIEF", build([DISPATCH_LIKE])).how == "exact"


def test_snap_near_spellings() -> None:
    parent, dispatch = build([PARENT_LIKE]), build([DISPATCH_LIKE])
    assert snap("Safety guards", parent).rule_id == "safety-guards"
    assert snap("Safty guards", parent).rule_id == "safety-guards"           # typo
    assert snap("4. Spawn > Brief", dispatch).rule_id == "spawn/brief"        # title path
    assert snap("Spawn / Siblings", dispatch).rule_id == "spawn/siblings"
    assert snap("Brief", dispatch).rule_id == "spawn/brief"                   # child title alone
    assert snap("Parent role", parent).rule_id == "intro"
    m = snap("Safety guards", parent)
    assert m.how == "snapped" and m.score == 1.0


def test_snap_unmatched_and_ambiguous() -> None:
    parent = build([PARENT_LIKE])
    m = snap("Something else entirely", parent)
    assert (m.rule_id, m.rule_title, m.how) == ("unmatched", None, "unmatched")
    dup = build(["## Rules\n### Notes\na\n## Other\n### Notes\nb\n"])
    assert snap("Notes", dup).rule_id == "unmatched"          # two rules fit equally
    assert snap("Other > Notes", dup).rule_id == "other/notes"
    assert snap("", parent).rule_id == "unmatched"
    assert snap("anything", []).rule_id == "unmatched"


def test_snap_whole_takes_every_ref() -> None:
    m = snap("Run the suite step", build(["Run the suite.\n"]))
    assert (m.rule_id, m.how) == ("whole", "snapped")


@pytest.mark.parametrize(("catalog_text", "variants", "rule_id"), [
    (PARENT_LIKE, ["Safety guards", "Safety guards: Never read [file] files yourself"], "safety-guards"),
    (DISPATCH_LIKE, ["10. Record", "10. Record / 9. Clean"], "record"),
])
def test_real_world_variant_pairs_map_to_one_id(catalog_text: str, variants: list[str], rule_id: str) -> None:
    catalog = build([catalog_text])
    assert {snap(v, catalog).rule_id for v in variants} == {rule_id}


def test_rule_key() -> None:
    assert rule_catalog.rule_key("safety-guards", "Safety guards") == "safety-guards"
    assert rule_catalog.rule_key("unmatched", "Odd Ref") == "ref:odd ref"
    assert rule_catalog.rule_key(None, "Odd Ref") == "ref:odd ref"


# --------------------------------------------------------------------------- writer prompt + resolve


def _loaded(name: str, text: str) -> deviations.LoadedSource:
    return deviations.LoadedSource(name, text, build([text]), rule_catalog.sha([text]))


def test_rules_block_is_numbered_ids_and_titles() -> None:
    block = deviations.rules_block([_loaded("parent-md", PARENT_LIKE)])
    assert block.splitlines() == ["--- parent-md ---", "1. intro — Parent role (intro)", "2. modes — Modes",
                                  "3. safety-guards — Safety guards", "4. dod — DoD"]


def test_resolve_snaps_flags_and_merges_spellings() -> None:
    src = _loaded("parent-md", PARENT_LIKE)
    raw = deviations.parse_findings(json.dumps({"deviations": [
        finding("parent-md", "safety-guards", 2),
        finding("parent-md", "Safety guards: Never read .env files yourself", 2),  # same rule, same turn
        finding("parent-md", "Some made up rule", 3),
        finding("parent-md", "Modes section", None),
    ]}), {"parent-md"})
    out = deviations.resolve(raw, [src])
    assert [(f.rule_id, f.ref_match, f.turn) for f in out] == [
        ("safety-guards", "exact", 2), ("unmatched", "unmatched", 3), ("modes", "snapped", None)]
    assert out[0].instruction_sha == src.sha and out[1].meta() == {"ref_match": "unmatched", "ref_score": 0.0}
    assert out[1].rule_title is None and out[1].instruction_ref == "Some made up rule"


def test_short_rule_id_is_a_valid_ref() -> None:
    (f,) = deviations.parse_findings(json.dumps({"deviations": [finding(ref="dod")]}), {"claude-md:global"})
    assert f.instruction_ref == "dod"


# --------------------------------------------------------------------------- database


@pytest.fixture
def db(setup_home: Home, pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:  # noqa: F811
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    (setup_home.claude / "CLAUDE.md").write_text(PARENT_LIKE)
    (setup_home.claude / "skills" / "dispatch" / "SKILL.md").write_text(DISPATCH_LIKE)
    yield pg_url


def _q(url: str, sql: str, *args: object) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, args).fetchall()


SPELLINGS = ["Safety guards", "Safety guards: Never read .env files yourself", "safety-guards",
             "Safety Guards", "safety guards / modes"]


def _spelling_writer() -> Callable[[str], str]:
    """Each session's writer reply names the same rule with another spelling."""
    calls = iter(range(100))

    def reply(prompt: str) -> str:
        i = next(calls)
        items = [finding(ref=SPELLINGS[i % len(SPELLINGS)])]
        if "--- skill:dispatch ---" in prompt:
            items.append(finding("skill:dispatch", "10. Record / 9. Clean", 2))
        return envelope({"deviations": items})
    return reply


def test_scan_stores_rule_ids_and_counts_recurrence_across_spellings(
        setup_home: Home, db: str, writer: Writer, fake_jev: FakeJev) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    w = writer(default=_spelling_writer())
    report = deviations.scan(now=NOW)
    assert report.errors == 0 and report.found == 6
    # The writer saw the numbered rule list of every source.
    assert "3. safety-guards — Safety guards" in w.prompts[0]
    assert "6. record — 10. Record" in next(p for p in w.prompts if "--- skill:dispatch ---" in p)
    states = [s for s in fake_jev.states if "instruction_source: claude-md:global" in s]
    assert [int(s.split("recurrence: ", 1)[1].split()[0]) for s in states] == [0, 1, 2, 3, 4]
    assert all("instruction_rule: safety-guards" in s and "rule_title: Safety guards" in s for s in states)
    rows = _q(db, "select distinct rule_id, rule_title, length(instruction_sha) from deviations "
                  "where instruction_source = 'claude-md:global'")
    assert rows == [("safety-guards", "Safety guards", 64)]
    assert _q(db, "select rule_id, meta ->> 'ref_match' from deviations where instruction_source = 'skill:dispatch'") \
        == [("record", "snapped")]
    (top, _) = deviations.candidates(now=NOW)
    assert (top["rule_id"], top["sessions"], top["count"]) == ("safety-guards", 5, 5)
    assert len(top["refs"]) == len(SPELLINGS)


def test_unmatched_ref_is_stored_flagged_and_grouped_by_its_ref(
        setup_home: Home, db: str, writer: Writer, fake_jev: FakeJev) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    writer(default=envelope({"deviations": [finding(ref="A rule nobody wrote")]}))
    report = deviations.scan(session_id="chat-1", now=NOW)
    assert report.outcomes[0].line().endswith("unjudged=0 unmatched_refs=1")
    assert _q(db, "select rule_id, rule_title, meta ->> 'ref_match' from deviations") == \
        [("unmatched", None, "unmatched")]
    assert "instruction_rule: unmatched" in fake_jev.states[0]
    (c,) = deviations.candidates(now=NOW)
    assert c["rule_id"] == "unmatched" and deviations.rule_label(c) == "A rule nobody wrote [unmatched]"


def _clear_refs(url: str) -> None:
    """Make the stored rows look like rows from before the rule catalog."""
    with psycopg.connect(url) as conn:
        conn.execute("update deviations set rule_id = null, rule_title = null, instruction_sha = null, "
                     "meta = '{}'::jsonb")


def test_reref_backfills_old_rows_and_is_idempotent(
        setup_home: Home, db: str, writer: Writer, fake_jev: FakeJev) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    writer(default=_spelling_writer())
    deviations.scan(now=NOW)
    _clear_refs(db)
    with psycopg.connect(db) as conn:  # a row whose source is gone today
        conn.execute("insert into deviations (source_key, session_id, instruction_source, instruction_ref, "
                     "what, why, how, followed_grade, result_grade) values "
                     "('dev:gone', 'chat-1', 'skill:gone', 'Old rule', 'w', 'y', 'h', 0.5, 0.5)")
    # Before reref, each spelling is its own group: no recurrence visible.
    assert len([c for c in deviations.candidates(now=NOW) if c["instruction_source"] == "claude-md:global"]) == 4
    calls_before = len(fake_jev.states)

    dry = deviations.reref(dry_run=True)
    assert dry.summary() == ("reref: rows=7 exact=1 snapped=5 unmatched=0 no_source=1 changed=6 "
                             "objective=1 (dry run, nothing written)")
    assert _q(db, "select count(*) from deviations where rule_id is not null") == [(0,)]

    done = deviations.reref()
    # objective=1: the hand-made dev:gone row had no result_outcome yet (its session has one).
    assert done.summary() == "reref: rows=7 exact=1 snapped=5 unmatched=0 no_source=1 changed=6 objective=1"
    assert any(ln.startswith('claude-md:global: "Safety guards: Never read [file] files yourself" -> '
                             "safety-guards (Safety guards) snapped") for ln in done.lines())
    assert any(ln.startswith('skill:dispatch: "10. Record / 9. Clean" -> record (10. Record) snapped')
               for ln in done.lines())
    assert _q(db, "select distinct rule_id from deviations where instruction_source = 'claude-md:global'") == \
        [("safety-guards",)]
    assert _q(db, "select rule_id from deviations where source_key = 'dev:gone'") == [(None,)]

    again = deviations.reref()
    assert again.changed == 0 and again.summary().endswith("changed=0")
    assert len(fake_jev.states) == calls_before  # no Jev call
    globals_ = [c for c in deviations.candidates(now=NOW) if c["instruction_source"] == "claude-md:global"]
    assert [(c["rule_id"], c["sessions"]) for c in globals_] == [("safety-guards", 5)]


def test_cli_reref_and_rules(setup_home: Home, db: str, writer: Writer, fake_jev: FakeJev) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    writer(default=_spelling_writer())
    deviations.scan(now=NOW)
    _clear_refs(db)
    env = base_env(AMEM_DB_URL=db, AMEM_CLAUDE_HOME=str(setup_home.claude),
                   AMEM_AGENT_CONFIG_DIR=str(setup_home.agent_config))
    dry = run_amem("deviations", "reref", "--dry-run", env=env)
    assert dry.returncode == 0, dry.stderr
    assert dry.stdout.splitlines()[-1].endswith("changed=6 (dry run, nothing written)")
    real = run_amem("deviations", "reref", env=env)
    assert real.returncode == 0 and real.stdout.splitlines()[-1].endswith("changed=6")
    assert run_amem("deviations", "reref", env=env).stdout.splitlines()[-1].endswith("changed=0")
    remote = run_amem("deviations", "reref", env={**env, "AMEM_REQUIRE_REMOTE": "1"})
    assert remote.returncode == 3

    rules = run_amem("deviations", "rules", str(setup_home.claude / "CLAUDE.md"), env=base_env())
    assert rules.returncode == 0, rules.stderr
    assert rules.stdout.splitlines()[1:] == ["1. intro — Parent role (intro)", "2. modes — Modes",
                                             "3. safety-guards — Safety guards", "4. dod — DoD"]
    env_file = run_amem("deviations", "rules", str(setup_home.repo / ".env"), env=base_env())
    assert env_file.returncode == 1 and "do-not-read-me" not in env_file.stdout + env_file.stderr


def test_migration_adds_columns_and_keeps_rls(db: str) -> None:
    cols = {r[0] for r in _q(db, "select column_name from information_schema.columns "
                                 "where table_name = 'deviations'")}
    assert {"rule_id", "rule_title", "instruction_sha", "meta"} <= cols
    assert _q(db, "select relrowsecurity from pg_class where relname = 'deviations'") == [(True,)]
    migration = (deviations.config.MIGRATIONS_DIR / "20261016020000_deviation_refs.sql").read_text()
    with psycopg.connect(db) as conn:
        conn.execute(migration)  # re-runnable
        conn.execute("insert into sessions (session_id, kind, project_dir) values ('m', 'chat', 'p')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into deviations (source_key, session_id, instruction_source, instruction_ref, "
                         "what, why, how, followed_grade, result_grade, instruction_sha) "
                         "values ('k', 'm', 's', 'r', 'w', 'y', 'h', 0.5, 0.5, 'not-a-sha')")
        conn.rollback()


def test_rules_cli_needs_no_database(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text(DISPATCH_LIKE)
    res = run_amem("deviations", "rules", str(f), env=base_env())
    assert res.returncode == 0, res.stderr
    assert "6. record — 10. Record" in res.stdout.splitlines()
