"""Credential redaction: one table of fake secrets, every entry point, and ordinary text.

The fake values are built from pieces so secret scanners do not flag this file.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent_memory import amem_digest, deviations, redact, tasks_ingest
from test_deviations_support import user

# (text, the secret part that must disappear, text that must stay)
SAMPLES: list[tuple[str, str, str]] = [
    ("key " + "sk-" + "proj-Abc123def456ghi789", "proj-Abc123def456ghi789", "key "),
    ("anthropic " + "sk-" + "ant-api03-abcdefghijklmnop", "ant-api03-abcdefghijklmnop", "anthropic "),
    ("stripe " + "sk_" + "live_4eC39HqLyjWDarjtT1zdp7dc", "4eC39HqLyjWDarjtT1zdp7dc", "stripe "),
    ("gh " + "ghp_" + "ABCDEFGHIJKLMNOPQRST1234", "ABCDEFGHIJKLMNOPQRST1234", "gh "),
    ("pat " + "github_pat_" + "11ABCDEFG0abcdefghijkl", "11ABCDEFG0abcdefghijkl", "pat "),
    ("slack " + "xoxb-" + "1234567890-abcdefghij", "1234567890-abcdefghij", "slack "),
    ("gitlab " + "glpat-" + "abcdefghij1234567890", "abcdefghij1234567890", "gitlab "),
    ("supabase " + "sb_" + "secret_AbCdEf1234567890xyz", "AbCdEf1234567890xyz", "supabase "),
    ("jwt " + "eyJ" + "hbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.c2lnbmF0dXJlX3BhcnQ",
     "eyJyb2xlIjoic2VydmljZV9yb2xlIn0", "jwt "),
    ("db postgresql://postgres:" + "hunter2pass" + "@db.example.com:5432/x", "hunter2pass", "@db.example.com"),
    ("aws " + "AKIA" + "ZZZZ2222YYYY3333", "ZZZZ2222YYYY3333", "aws "),
    ("sts " + "ASIA" + "QQQQ4444WWWW5555", "QQQQ4444WWWW5555", "sts "),
    ("google " + "AIza" + "SyD-abcdefghijklmnopqrstuvwxyz12345", "SyD-abcdefghijklmnopqrstuvwxyz12345", "google "),
    ("password=" + "hunter2!xyz", "hunter2!xyz", "password="),
    ("DB_PASSWORD: " + "'correct-horse-battery'", "correct-horse-battery", "DB_PASSWORD: "),
    ('{"api_key": "' + "abcd1234efgh5678" + '"}', "abcd1234efgh5678", '"api_key": "'),
    ("export AWS_SECRET_ACCESS_KEY=" + "wJalrXUtnFEMI/K7MDENG/bPxRfiCY", "wJalrXUtnFEMI/K7MDENG", "AWS_SECRET_ACCESS_KEY="),
    ("secret: " + "s3cr3t-value-here", "s3cr3t-value-here", "secret: "),
    ("token = " + "tok_live_9f8e7d6c", "tok_live_9f8e7d6c", "token = "),
    ("Authorization: Bearer " + "abc.def123.ghi456jkl", "abc.def123.ghi456jkl", "Authorization: Bearer "),
    ("curl -H 'Authorization: Basic " + "dXNlcjpwYXNzd29yZDEy'", "dXNlcjpwYXNzd29yZDEy", "Authorization: Basic "),
    ("the token " + "a1b2c3d4e5f6g7h8i9j0", "a1b2c3d4e5f6g7h8i9j0", "the token "),
    ("-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow" + "IBAAKCAQEA\n-----END RSA PRIVATE KEY----- done",
     "MIIEowIBAAKCAQEA", " done"),
]

# Ordinary text: nothing is redacted.
PLAIN = [
    "the token count went up by 1200",
    "Fill in the password field, then press Submit.",
    "bearer authentication is described in RFC 6750",
    "max_tokens=4096 and input_tokens: 123",
    "the secret sauce is caching",
    "Authorization header missing",
    "api_key: str",
    "see https://example.com/path?x=1",
    "skills are listed in skills/README.md",
]


def _digest(text: str) -> str:
    return deviations.build_digest([user(text, 0)])


def _cloud_digest(text: str) -> str:
    return amem_digest.build_digest([user(text, 0)])


def _excerpt(text: str) -> str:
    return tasks_ingest.checkpoint_excerpt("Decisions: " + text) or ""


ENTRY_POINTS: dict[str, Callable[[str], str]] = {
    "redact.redact": redact.redact,
    "tasks_ingest.redact": tasks_ingest.redact,
    "tasks_ingest.checkpoint_excerpt": _excerpt,
    "deviations.redact": deviations.redact,
    "deviations.build_digest": _digest,
    "amem_digest.redact": amem_digest.redact,
    "amem_digest.build_digest": _cloud_digest,
}


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize(("text", "secret", "kept"), SAMPLES, ids=[s[0][:24] for s in SAMPLES])
def test_every_entry_point_redacts_the_sample(entry: str, text: str, secret: str, kept: str) -> None:
    out = ENTRY_POINTS[entry](text)
    assert secret not in out
    assert "[redacted]" in out
    assert kept in out


@pytest.mark.parametrize("entry", ["redact.redact", "deviations.redact", "amem_digest.redact"])
@pytest.mark.parametrize("text", PLAIN)
def test_ordinary_text_is_unchanged(entry: str, text: str) -> None:
    assert ENTRY_POINTS[entry](text) == text


def test_only_the_value_goes_when_a_key_name_is_kept() -> None:
    text = "run with password=hunter2!xyz and the token count stays"
    assert redact.redact(text) == "run with password=[redacted] and the token count stays"


def test_redacting_twice_changes_nothing() -> None:
    for text, _, _ in SAMPLES:
        once = redact.redact(text)
        assert redact.redact(once) == once


def test_cloud_copy_matches_the_package() -> None:
    assert ([(p.pattern, p.flags) for p in amem_digest.REDACT_RES]
            == [(p.pattern, p.flags) for p in redact.REDACT_RES])
    for text in [s[0] for s in SAMPLES] + PLAIN:
        assert amem_digest.redact(text) == redact.redact(text)
