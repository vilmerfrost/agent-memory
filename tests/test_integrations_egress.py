"""Mission 004C (F3): the egress deny list (AMEM_EGRESS_DENY plus team repos read from Supabase)."""

from __future__ import annotations

import psycopg
import pytest

from agent_memory.integrations import egress
from agent_memory.integrations.sanitize import sanitize_record

OWN = "0b3e7d2a-8c1f-4f6e-9a2b-1c3d4e5f6a7b"


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


def test_email_like() -> None:
    assert egress.email_like("vilmer@example.com") and egress.email_like("a b x.y@corp.io")
    assert not egress.email_like("claude-3@20240229") and not egress.email_like(None) and not egress.email_like("a@b")


def test_load_reads_every_team_repo_and_project(pg_url: str) -> None:
    rows = (("eg-team-1", "pipeline-poc", "pipeline-poc", '{"team": true}'),
            ("eg-team-2", None, "acme-portal", '{"team": true}'),
            ("eg-team-3", "scratch", "scratch", '{"team": true}'),
            ("eg-own", "agent-memory", "agent-memory", '{"team": false}'))
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


def test_sanitize_refuses_a_record_that_names_a_denied_term() -> None:
    deny = egress.team_terms(["pipeline-poc"]) | egress.env_terms({egress.DENY_ENV: "acme"})
    base = {"kind": "proposal", "ref": f"amem:proposal/{OWN}", "hypothesis": "quote zsh globs"}
    assert sanitize_record(base, deny)[0] is not None
    for field, text in (("hypothesis", "port the Pipeline-POC deploy gate"), ("reason", "asked by ACME"),
                        ("rule", "claude-md:pipeline-poc/deploy"),
                        ("reason", "see ~/StupidHuman/pipeline-poc/notes.md")):     # scrubbed to [path], still refused
        assert sanitize_record({**base, field: text}, deny) == (None, ["deny"]), (field, text)
        assert sanitize_record({**base, field: text})[0] is not None                 # no deny list: sent as before
