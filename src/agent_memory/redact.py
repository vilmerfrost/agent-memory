"""One credential redaction for everything that leaves this machine.

Task excerpts (tasks_ingest: runs.meta), transcript digests, instruction texts and
deviation fields (deviations) are all stored in the hosted database, so each one is
redacted here first.

A pattern may have a named group `keep`: that prefix (a key name, `Bearer `) stays and only
the value after it becomes `[redacted]`. Other patterns replace the whole match.

amem_digest.py is a standalone copy for cloud sessions and keeps its own copy of
REDACT_RES and redact(); tests/test_redact.py checks that both stay the same.
"""

from __future__ import annotations

import re

MARK = "[redacted]"

# Key names in `name = value` / `name: value`. A prefix like AWS_ or DB_ is allowed.
_KEY_NAMES = (r"(?:api[_-]?key|secret|password|passwd|pwd|token|auth[_-]?token|access[_-]?key"
              r"|private[_-]?key|service[_-]?role[_-]?key|authorization|credentials?)")

REDACT_RES = (
    # Prefixed tokens: OpenAI/Anthropic/Stripe sk-/sk_, GitHub, GitLab, Slack, Supabase secret keys.
    re.compile(r"\b(?:sk-|sk_|ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|glpat-|xox[abposr]-|sb_secret_)[A-Za-z0-9_-]{8,}"),
    # JWTs (Supabase service-role and anon keys are JWTs too).
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"),
    # user:password@ in a URL.
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    # AWS access key ids (long-lived AKIA, temporary ASIA).
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Google API keys.
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    # PEM private key blocks (to the end of the text when the END line is missing).
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    # Authorization: Bearer xxx / Basic xxx. The value needs a digit, so "bearer authentication" stays.
    re.compile(r"(?i)(?P<keep>\b(?:bearer|basic)\s+)(?=[A-Za-z0-9_\-.~+/=]*\d)[A-Za-z0-9_\-.~+/=]{12,}"),
    # api_key=..., PASSWORD: "...", AWS_SECRET_ACCESS_KEY=... The key name stays.
    re.compile(r"(?i)(?P<keep>\b[\w-]*?" + _KEY_NAMES + r"(?:[_-]?(?:key|value|id))?['\"]?\s*[:=]\s*['\"]?)"
               r"[^\s'\"`,;(){}\[\]<>]{8,}"),
    # "token abc123..." without a separator: only long values with a letter and a digit.
    re.compile(r"(?i)(?P<keep>\b(?:api[_-]?key|secret|password|passwd|token)\s+)"
               r"(?=[A-Za-z0-9_\-./+=]*\d)(?=[A-Za-z0-9_\-./+=]*[A-Za-z])[A-Za-z0-9_\-./+=]{16,}"),
)


def _replace(m: re.Match[str]) -> str:
    keep = m.group("keep") if "keep" in m.re.groupindex else None
    return (keep or "") + MARK


def redact(text: str) -> str:
    """`text` with every credential-shaped string replaced by [redacted]."""
    for pattern in REDACT_RES:
        text = pattern.sub(_replace, text)
    return text
