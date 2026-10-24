"""Mission 004C (F3): the egress deny list (AMEM_EGRESS_DENY plus team repos read from Supabase)."""

from __future__ import annotations

import psycopg
import pytest

from agent_memory.integrations import egress
from agent_memory.integrations.sanitize import sanitize_record

OWN = "0b3e7d2a-8c1f-4f6e-9a2b-1c3d4e5f6a7b"
OWN_META = '{"team": false, "remote_owner": "vilmerfrost", "remote_host": "github.com"}'


def test_env_terms_are_case_insensitive_substrings() -> None:
    d = egress.env_terms({egress.DENY_ENV: " Acme , ,globex-corp"})
    assert d.terms == ("acme", "globex-corp")
    assert d.match("ACME-portal") and d.match("x Globex-Corp y") and not d.match("globex") and not d.match(None)
    assert egress.env_terms({}).terms == () and not egress.env_terms({}).match("anything")
    assert egress.env_terms({egress.DENY_ENV: "ab"}).match("lab")          # an explicit term is used as given


def test_team_terms_skip_generic_placeholders_and_short_names() -> None:
    d = egress.team_terms(["pipeline-poc", "evals", "scratch", "home", "unknown", "team", "global", "routine:daily",
                           "ab", None, " X-Repo "])
    assert d.terms == ("pipeline-poc", "x-repo")
    assert (d | egress.env_terms({egress.DENY_ENV: "acme"})).terms == ("acme", "pipeline-poc", "x-repo")


def test_team_terms_skip_agent_worktree_names() -> None:
    """2026-10-24: a subagent session stored with repo `agent-<id>` (its .claude/worktrees folder) made that name a
    term, and the term is inside the session's own id `<parent>/agent-<id>`, so ClickHouse dropped the row."""
    d = egress.team_terms(["agent-a0846dac3b03cb2f4", "AGENT-A05A5BFB2F142D702", "agent-memory", "agent-tools"])
    assert d.terms == ("agent-memory", "agent-tools")
    assert not d.match("7ff5f480-77aa-496c-9b6a-bd96d4d4c395/agent-a0846dac3b03cb2f4")
    assert egress.env_terms({egress.DENY_ENV: "agent-a0846dac3b03cb2f4"}).match("x/agent-a0846dac3b03cb2f4")


def test_email_like() -> None:
    assert egress.email_like("vilmer@example.com") and egress.email_like("a b x.y@corp.io")
    assert not egress.email_like("claude-3@20240229") and not egress.email_like(None) and not egress.email_like("a@b")


def test_load_reads_every_team_repo_and_project(pg_url: str) -> None:
    rows = (("eg-team-1", "pipeline-poc", "pipeline-poc", '{"team": true}'),
            ("eg-team-2", None, "acme-portal", '{"team": true}'),
            ("eg-team-3", "scratch", "scratch", '{"team": true}'),
            ("eg-own", "agent-memory", "agent-memory", OWN_META))
    with psycopg.connect(pg_url, autocommit=True) as conn:
        for sid, repo, project, meta in rows:
            conn.execute("insert into sessions (session_id, kind, project_dir, repo, project, transcript_bytes, env, meta) "
                         "values (%s, 'chat', '/eg', %s, %s, 1, 'prod', %s::jsonb)", (sid, repo, project, meta))
        try:
            d = egress.load(conn, {egress.DENY_ENV: "Globex"})
        finally:
            conn.execute("delete from sessions where project_dir = '/eg'")
    assert {"pipeline-poc", "acme-portal", "globex"} <= set(d.terms)
    assert "agent-memory" not in d.terms and "scratch" not in d.terms


def test_current_joins_env_terms_and_the_team_names_a_sink_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(egress, "_team", egress.NONE)
    monkeypatch.setenv(egress.DENY_ENV, "acme")
    assert egress.current().terms == ("acme",)
    egress.use_team_names(egress.team_terms(["pipeline-poc"]))
    assert egress.current().terms == ("acme", "pipeline-poc")
    monkeypatch.delenv(egress.DENY_ENV)
    assert egress.current().terms == ("pipeline-poc",)


# ---------------------------------------------------------------- F8: unknown repo ownership is private


def test_owner_accounts_default_env_and_validation() -> None:
    from agent_memory import sessions_ingest

    assert egress.PERSONAL_OWNERS_ENV == sessions_ingest.PERSONAL_OWNERS_ENV            # no drift from ingestion
    assert egress.DEFAULT_OWNER_ACCOUNTS == sessions_ingest.DEFAULT_PERSONAL_OWNERS
    assert egress.owner_accounts({}) == ("vilmerfrost",)
    assert egress.owner_accounts({egress.PERSONAL_OWNERS_ENV: "Vilmer-Frost, frost-labs"}) == ("frost-labs", "vilmer-frost")
    assert egress.owner_accounts({egress.OWNER_ACCOUNTS_ENV: "frost-labs", egress.PERSONAL_OWNERS_ENV: "x"}) == ("frost-labs",)
    # a name that could break out of a SQL literal is dropped: fewer accounts only ever means more private
    assert egress.owner_accounts({egress.OWNER_ACCOUNTS_ENV: "ok,x'; drop table sessions; --, a b,"}) == ("ok",)
    assert egress.owner_accounts({egress.OWNER_ACCOUNTS_ENV: "'"}) == ()
    assert egress.bind_owners(f"= any({egress.OWNERS_TOKEN})", ["ok", "x'y"]) == "= any(array['ok']::text[])"
    assert egress.bind_owners(egress.OWNERS_TOKEN, []) == "array[]::text[]"


GH = {"remote_host": "github.com"}
CASES = (   # (meta as sessions_ingest stores it, class)
    ({"team": True, "team_reason": "remote-owner", "remote_owner": "acme", **GH}, "team"),
    ({"team": True, "team_reason": "remote-owner", "remote_owner": "acme", "remote_host": "gitlab.acme.io"}, "team"),
    ({"team": True, "team_reason": "profile"}, "team"),
    ({"team": True, "team_reason": "same-repo"}, "team"),
    ({"team": "true"}, "team"),
    ({"team": False, "remote_owner": "vilmerfrost", **GH}, "own"),
    ({"team": False, "remote_owner": "VilmerFrost", "remote_host": "GitHub.com"}, "own"),   # both case-insensitive
    ({}, "private"),                                                          # no remote / not a git repo
    ({"cwd": "/Users/x/work/tool", "note": "a remote whose owner could not be read"}, "private"),
    ({"team": False}, "private"),                                             # no owner stored: unknown
    ({"team": False, "remote_owner": "old-handle", **GH}, "private"),        # an owner that is not an account
    ({"team": False, "remote_owner": None, **GH}, "private"),
    ({"remote_owner": "vilmerfrost", **GH}, "private"),                      # no explicit team = false
    # Mission 004C (backfill prep): own needs a github.com remote, so the owner's account name elsewhere is not own
    ({"team": False, "remote_owner": "vilmerfrost", "remote_host": "gitlab.example.com"}, "private"),
    ({"team": False, "remote_owner": "vilmerfrost", "remote_host": "github.com.evil.example"}, "private"),
    ({"team": False, "remote_owner": "vilmerfrost", "remote_host": "github-work"}, "private"),   # an ssh alias
    ({"team": False, "remote_owner": "vilmerfrost"}, "private"),             # stored before the host was kept
    ({"team": False, "remote_owner": "vilmerfrost", "remote_host": None}, "private"),
    ({"team": False, "remote_owner": "vilmerfrost", "remote_host": ""}, "private"),
    (None, "private"),
)


@pytest.mark.parametrize("meta, cls", CASES)
def test_classify_only_positive_ownership_is_own(meta: object, cls: str) -> None:
    assert egress.classify(meta, egress.DEFAULT_OWNER_ACCOUNTS) == cls
    assert egress.classify(meta, ()) == ("team" if cls == "team" else "private")       # no accounts: nothing is own


def test_class_sql_is_the_same_rule_as_classify(pg_url: str) -> None:
    import json

    with psycopg.connect(pg_url) as conn:
        for meta, cls in CASES:
            q = egress.bind_owners(f"select {egress.class_sql('%(m)s::jsonb')}", egress.DEFAULT_OWNER_ACCOUNTS)
            assert conn.execute(q, {"m": json.dumps(meta)}).fetchone()[0] == cls, meta
        conn.rollback()
        with pytest.raises(psycopg.errors.UndefinedColumn):          # unbound SQL fails closed, never "own"
            conn.execute(f"select {egress.class_sql(chr(39) + '{}' + chr(39) + '::jsonb')}")


def test_withheld_names_are_team_and_private_but_never_an_own_name(pg_url: str) -> None:
    rows = (("eg-p-1", "client-notes", "client-notes", "{}"),                                    # no remote
            ("eg-p-2", "agent-memory", "agent-memory", "{}"),                                    # own name too
            ("eg-p-3", "old-tool", "old-tool", '{"team": false, "remote_owner": "old-handle"}'),
            ("eg-p-4", "scratch", "private", "{}"),                                              # placeholders
            ("eg-own", "agent-memory", "agent-memory", OWN_META),
            ("eg-p-5", "gitlab-tool", "gitlab-tool",                                             # not on github.com
             '{"team": false, "remote_owner": "vilmerfrost", "remote_host": "gitlab.example.com"}'),
            ("eg-team", "pipeline-poc", "pipeline-poc", '{"team": true}'))
    with psycopg.connect(pg_url, autocommit=True) as conn:
        for sid, repo, project, meta in rows:
            conn.execute("insert into sessions (session_id, kind, project_dir, repo, project, transcript_bytes, env, meta) "
                         "values (%s, 'chat', '/eg', %s, %s, 1, 'prod', %s::jsonb)", (sid, repo, project, meta))
        try:
            d = egress.load(conn, {})
            narrowed = egress.team_names(conn, owners=())                 # no accounts: every name is withheld
        finally:
            conn.execute("delete from sessions where project_dir = '/eg'")
    assert {"client-notes", "old-tool", "pipeline-poc", "gitlab-tool"} <= set(d.terms)
    assert not {"agent-memory", "scratch", "private"} & set(d.terms)
    assert {"agent-memory", "client-notes", "old-tool", "pipeline-poc"} <= set(narrowed.terms)


def test_sanitize_refuses_a_private_record() -> None:
    base = {"kind": "edit", "ref": f"amem:edit/{OWN}", "hypothesis": "quote zsh globs"}
    assert sanitize_record(base)[0] is not None
    assert sanitize_record({**base, "private": True}) == (None, ["private"])
    assert sanitize_record({**base, "private": True, "team": True}) == (None, ["team"])


def test_sanitize_refuses_a_record_that_names_a_denied_term() -> None:
    deny = egress.team_terms(["pipeline-poc"]) | egress.env_terms({egress.DENY_ENV: "acme"})
    base = {"kind": "proposal", "ref": f"amem:proposal/{OWN}", "hypothesis": "quote zsh globs"}
    assert sanitize_record(base, deny)[0] is not None
    for field, text in (("hypothesis", "port the Pipeline-POC deploy gate"), ("reason", "asked by ACME"),
                        ("rule", "claude-md:pipeline-poc/deploy"),
                        ("reason", "see ~/StupidHuman/pipeline-poc/notes.md")):     # scrubbed to [path], still refused
        assert sanitize_record({**base, field: text}, deny) == (None, ["deny"]), (field, text)
        assert sanitize_record({**base, field: text})[0] is not None                 # no deny list: sent as before
