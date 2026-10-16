"""Mission 004A: the sanitizer for decision records that leave for past.dev (no database, no network)."""

from __future__ import annotations

import pytest

from agent_memory.integrations.sanitize import CAP_BYTES, FIELD_CAPS, sanitize_record, scrub_text, secret_like

OWN = "0b3e7d2a-8c1f-4f6e-9a2b-1c3d4e5f6a7b"
FOREIGN = "11111111-2222-4333-8444-555555555555"


def rec(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {"kind": "proposal", "ref": f"amem:proposal/{OWN}"}
    base.update(fields)
    return base


def content_of(**fields: object) -> str:
    content, reasons = sanitize_record(rec(**fields))
    assert content is not None, reasons
    return content


# ---------------------------------------------------------------- scrubbers


@pytest.mark.parametrize(("text", "leak", "mark"), [
    ("see /Users/vilmer/Projects/agent-memory/src/x.py now", "/Users", "[path]"),
    ("cache in /private/tmp/claude-502/scratch", "/private", "[path]"),
    ("from ~/Projects/agent-memory", "~/", "[path]"),
    ("from ~vilmer/notes", "~vilmer", "[path]"),
    ("C:\\Users\\vilmer\\secret.txt", "C:\\", "[path]"),
    ("C:/Users/vilmer/x", "C:/", "[path]"),
    ("\\\\fileserver\\share\\x", "fileserver", "[path]"),
    ("$HOME/.claude/settings.json", "$HOME", "[path]"),
    ("Users/vilmerfrost/Projects without the slash", "vilmerfrost", "[path]"),
    ("edit .claude/skills/dispatch now", ".claude", "[path]"),
    ("touched src/agent_memory/edits.py today", "src/agent", "[path]"),
    ("rule claude-md:global//Users/leak/x", "/Users", "[path]"),
    ("full-width \uff0fUsers\uff0fvilmer\uff0fx", "vilmer", "[path]"),
    ("mail vilmer@example.com please", "vilmer@", "[email]"),
    ("git@github.com:me/repo", "github.com", "[email]"),
    ("ping root@localhost", "root@", "[email]"),
    ("open https://evil.example.com/x?token=1", "evil.example", "[url]"),
    ("file:///etc/passwd", "/etc", "[url]"),
    ("call api.past.dev for it", "past.dev", "[host]"),
    ("the box Vilmers-MacBook-Pro.local", "MacBook", "[host]"),
    ("db at db.corp.internal:5432", "corp.internal", "[host]"),
    ("three labels a1.b2.zzz", "a1.b2", "[host]"),
    ("listen on localhost:8080", "localhost", "[host]"),
    ("addr 192.168.1.20:5432", "192.168", "[ip]"),
    ("addr fe80::1 and 2001:db8::7", "fe80", "[ip]"),
    (f"other {FOREIGN} id", FOREIGN, "[id]"),
    (f"upper {FOREIGN.upper()} id", FOREIGN.upper(), "[id]"),
    ("artifact 1a2b3c4d5e6f7a8b", "1a2b3c4d", "[id]"),
    ("commit abc1234 landed", "abc1234", "[id]"),
    ("trace 0123456789abcdef0123456789abcdef", "0123456789abcdef", "[id]"),
    ("tool toolu_01ABCdefGHIjklMNOpqr", "toolu_01", "[id]"),
])
def test_scrubbers_remove_what_must_not_leave(text: str, leak: str, mark: str) -> None:
    out = content_of(reason=text)
    assert leak not in out and mark in out


def test_redact_runs_first_and_keeps_the_key_name() -> None:
    out = content_of(reason="api_key=abcdefgh12345678 and sk-ant-api03-abcdefghijklmnop and ghp_abcdefghijklmnop1234")
    assert "abcdefgh12345678" not in out and "sk-ant" not in out and "ghp_" not in out
    assert "api_key=[redacted]" in out
    assert "redacted:reason" in sanitize_record(rec(reason="token: abcdefgh12345678"))[1]


@pytest.mark.parametrize("text", [
    "rule claude-md:global/shell-habits", "skill:dispatch/spawn", "CLAUDE.md and AGENTS.md and SKILL.md",
    "sync.py and Node.js", "v1.2.3 at 12:30:45 on 2026-10-16", "rsi_proposals.status", "e.g. and i.e.",
    "fewer deviations on claude-md:global / dod-gate", "jev confidence 0.42 < 0.60", "A/B test and/or 1/2",
    "token is expired", "the secret: none", "decided 20261016", "exp-20261017-01",
])
def test_benign_text_survives(text: str) -> None:
    content, reasons = sanitize_record(rec(reason=text))
    assert content is not None and f"reason: {text}" in content and reasons == []


def test_the_own_ref_id_is_kept_everywhere() -> None:
    out = content_of(reason=f"staged as proposal {OWN} (see amem:proposal/{OWN.upper()}); not {FOREIGN}")
    assert f"ref: amem:proposal/{OWN}" in out
    assert f"staged as proposal {OWN}" in out and f"amem:proposal/{OWN}" in out.split("reason: ")[1]
    assert FOREIGN not in out and "[id]" in out
    exp = sanitize_record({"kind": "experiment", "ref": "amem:experiment/exp-20261016-edit-1a2b3c4d",
                           "reason": "rerun exp-20261016-edit-1a2b3c4d, not exp-20261016-edit-9f8e7d6c"})[0]
    assert exp is not None and "rerun exp-20261016-edit-1a2b3c4d," in exp and "9f8e7d6c" not in exp


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(("text", "why"), [
    ("key past_sk_abcdef123456", "past-key"),
    ("mgmt past_mk_abcdef123456", "past-key"),
    ("stripe pk_live_abcd1234", "stripe-key"),
    ("hook whsec_abcdefgh1234", "webhook-secret"),
    ("npm npm_abcdefghijklmnopqrstu123", "npm-token"),
    ("hf hf_abcdefghijklmnopqrstu123", "hf-token"),
    ("-----BEGIN CERTIFICATE----- MIIB", "pem"),
    ("pwd=abc123", "assignment"),
    ("password: hunt3r!", "assignment"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "bearer"),
    ("blob AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "high-entropy"),
    ("zero-width past\u200b_sk_abcdef123456", "past-key"),
])
def test_secret_shapes_refuse_the_record(text: str, why: str) -> None:
    content, reasons = sanitize_record(rec(hypothesis="fine", reason=text))
    assert content is None and reasons == [f"secret:{why}:reason"]


def test_redact_known_private_key_is_redacted_not_sent() -> None:
    content = content_of(reason="-----BEGIN RSA PRIVATE KEY----- MIIEow -----END RSA PRIVATE KEY----- done")
    assert "MIIEow" not in content and "[redacted]" in content


@pytest.mark.parametrize("record", [
    rec(team=True), rec(meta={"team": True}), {"kind": "transcript", "ref": f"amem:proposal/{OWN}"},
    {"kind": "proposal", "ref": f"amem:edit/{OWN}"}, {"kind": "proposal", "ref": "ch:deviations/x"},
    {"kind": "lesson", "ref": "amem:run/Not.A.Uuid"}, {"kind": "lesson", "ref": "amem:session/vilmer@example.com"},
    {"kind": "decision", "ref": "amem:decision/../../etc"}, {"kind": "edit", "ref": None},
])
def test_team_items_and_foreign_kinds_or_refs_are_refused(record: dict[str, object]) -> None:
    content, reasons = sanitize_record(record)
    assert content is None and reasons and reasons[0] in ("team", "kind", "ref")


def test_meta_team_false_or_missing_is_not_a_team_item() -> None:
    assert sanitize_record(rec(team=False, meta={"team": "true"}))[0] is not None


# ---------------------------------------------------------------- cap, shape and determinism


def test_fields_are_cut_to_their_cap() -> None:
    content, reasons = sanitize_record(rec(reason="word " * 400))
    assert content is not None and "trimmed:reason" in reasons
    line = next(ln for ln in content.splitlines() if ln.startswith("reason: "))
    assert len(line) - len("reason: ") <= FIELD_CAPS["reason"] and line.endswith("...")


def test_a_cut_never_leaves_half_a_placeholder() -> None:
    text = "x" * (FIELD_CAPS["reason"] - 8) + " /Users/a/b " + "y" * 50
    out = content_of(reason=text).split("reason: ")[1]
    assert out.endswith("...") and "[pa..." not in out and "[path" not in out[-12:-3]


def test_a_record_over_the_byte_cap_is_refused() -> None:
    full = {f: "w " * 400 for f in ("rule", "hypothesis", "change_kind", "result", "decision", "reason")}
    assert sanitize_record(rec(**full)) == (None, ["cap"])
    wide = {f: "\u00e5\u00e4\u00f6 " * 200 for f in ("hypothesis", "result", "reason")}     # 2-byte letters
    assert sanitize_record(rec(**wide)) == (None, ["cap"])
    ok = sanitize_record(rec(hypothesis="h" * 300, reason="r" * 300))[0]
    assert ok is not None and len(ok.encode()) <= CAP_BYTES


def test_shape_is_fixed_field_order_and_one_line_per_field() -> None:
    content = content_of(night="2026-10-16", reason="multi\nline\ttext", decision="apply", rule="claude-md:global/x",
                         hypothesis="h", change_kind="edit_instruction", result="promoted: kept", extra="never sent")
    assert content.splitlines() == [
        "kind: proposal", f"ref: amem:proposal/{OWN}", "rule: claude-md:global/x", "hypothesis: h",
        "change_kind: edit_instruction", "result: promoted: kept", "decision: apply", "reason: multi line text",
        "night: 2026-10-16"]
    content, reasons = sanitize_record(rec(night="/Users/x/2026"))
    assert content is not None and "night" not in content and "dropped:night" in reasons


def test_output_is_deterministic_and_stable() -> None:
    fields = dict(rule="claude-md:global/x", hypothesis=f"see /Users/v/x and {FOREIGN}", reason="mail a@b.co",
                  night="2026-10-16")
    a = sanitize_record(rec(**fields))
    b = sanitize_record(dict(reversed(list(rec(**fields).items()))))
    assert a == b and a[0] is not None
    for line in a[0].splitlines()[2:]:                      # a second pass changes nothing
        value = line.split(": ", 1)[1]
        assert scrub_text(value, OWN)[0] == value


def test_scrub_text_and_secret_like_helpers() -> None:
    assert scrub_text("a  \n b") == ("a b", [])
    assert scrub_text("x /tmp/y")[1] == ["path"]
    assert secret_like("past_sk_abcdef123456") == "past-key"
    assert secret_like("sk-ant-api03-abcdefghijklmnop") == "credential"
    assert secret_like("nothing here") is None
