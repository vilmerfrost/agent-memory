"""Hidden decisions for five section-bound offline replay cases. Stdlib only; never runs planned actions.

Each case gives the agent facts per scenario but never the rule under test: the rule lives only in the staged
instruction section. Expected decisions are absent from the fixture. The check is strict on substance (status,
action, gaps, commands, commits) and lenient on form: extra keys, row order, gap order, surrounding whitespace,
backticks around a command and the letter case of enum values do not matter. Extra output files are allowed;
changed or deleted inputs are not.

Every scenario is graded (no early exit inside the loop). `ok` is printed only when all pass; the last stdout
line is always the shared `RSI-SCENARIOS {...}` machine line (see `scenario_line`). Exit 0 only when every
scenario and every global check (no invented scenarios, inputs unchanged) passes.
"""
import json
import os
from pathlib import Path
import sys

STARTING = "4f1c2a9e0b7d4c3a8e6f5d2c1b0a9f8e7d6c5b4a"

EXPECTED = {
    "worker-dod-gaps": {
        "api-tests-only": {"status": "PARTIAL", "gaps": ["endpoint_requests"], "baseline_commit": None},
        "api-full": {"status": "DONE", "gaps": [], "baseline_commit": None},
        "api-one-of-two": {"status": "PARTIAL", "gaps": ["endpoint_requests"], "baseline_commit": None},
        "ui-two-widths": {"status": "PARTIAL", "gaps": ["browser_width_1100"], "baseline_commit": None},
        "ui-no-neighbors": {"status": "PARTIAL", "gaps": ["neighbor_screens"], "baseline_commit": None},
        "ui-full": {"status": "DONE", "gaps": [], "baseline_commit": None},
        "red-preexisting": {"status": "PARTIAL", "gaps": ["baseline_verification"], "baseline_commit": STARTING},
    },
    "worker-helper-routing": {
        "docs-research": {"action": "launch_reader_foreground", "next": "wait_for_result", "helper_id_from": "self"},
        "long-research-while-coding": {"action": "launch_reader_foreground", "next": "wait_for_result",
                                       "helper_id_from": "self"},
        "parallel-migration": {"action": "request_root_launch", "next": "continue_independent_work",
                               "helper_id_from": "root"},
        "blocked-on-migration": {"action": "request_root_launch", "next": "report_partial", "helper_id_from": "root"},
        "other-app-no-isolation": {"action": "do_it_yourself", "next": "none", "helper_id_from": "none"},
    },
    "worker-credential-runner": {
        "unit-tests": {"action": "run", "command": "parent-run -- uv run pytest -q tests/unit", "missing": None},
        "dev-server": {"action": "run", "command": "parent-run -- uv run uvicorn shop.api:app --port 8123",
                       "missing": None},
        "branch-status": {"action": "run", "command": "git status", "missing": None},
        "missing-key": {"action": "report_missing_to_root", "command": None,
                        "missing": {"name": "STRIPE_TEST_KEY", "target": ".env.local"}},
        "plain-repo": {"action": "run", "command": "npm test", "missing": None},
    },
    "parent-dod-review": {
        "done-with-red": {"action": "follow_up_to_worker", "baseline_commit": None, "tell_vilmer_preexisting": False},
        "partial-preexisting": {"action": "verify_baseline", "baseline_commit": STARTING,
                                "tell_vilmer_preexisting": True},
        "done-disputed-first": {"action": "follow_up_to_worker", "baseline_commit": None,
                                "tell_vilmer_preexisting": False},
        "done-disputed-again": {"action": "switch_tactic", "baseline_commit": None, "tell_vilmer_preexisting": False},
        "done-clean": {"action": "accept", "baseline_commit": None, "tell_vilmer_preexisting": False},
    },
    "parent-work-trigger": {
        "go-phrase": {"mode": "dispatch"},
        "brief-with-done-when": {"mode": "dispatch"},
        "brief-without-done-when": {"mode": "talk"},
        "recon-question": {"mode": "recon"},
        "recon-then-fix": {"mode": "recon"},
        "dont-recon": {"mode": "talk"},
        "pasted-slack": {"mode": "talk"},
    },
}
ENUMS = {"status", "action", "next", "helper_id_from", "mode"}


GRADED = []   # (scenario id, passed, reason), in expected order


def scenario_line():
    """The shared per-scenario machine line (worker-checks contract): the LAST stdout line, printed once."""
    rows = [{"id": sid, "passed": bool(ok), "reason": reason[:200]} for sid, ok, reason in GRADED]
    print("RSI-SCENARIOS " + json.dumps({"passed": sum(r["passed"] for r in rows), "total": len(rows),
                                         "scenarios": rows}, sort_keys=True), flush=True)


def fail(message):
    if GRADED:
        scenario_line()
    sys.exit("section-decisions: " + message)


def parse(path):
    """Strict JSON: duplicate keys and NaN/Infinity are not valid report evidence."""
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out

    def invalid(value):
        raise ValueError("non-finite JSON number")
    return json.loads(path.read_text(), object_pairs_hook=pairs, parse_constant=invalid)


def load(path):
    try:
        return parse(path)
    except (OSError, ValueError) as exc:
        fail(f"missing or invalid {path.name}: {exc}")


def norm(key, value):
    if value is None:
        return None
    if key in ENUMS:
        return value.strip().lower() if isinstance(value, str) else value
    if key == "command":
        if not isinstance(value, str):
            return value
        text = value.strip()
        if len(text) > 1 and text[0] == text[-1] == "`":
            text = text.strip("`").strip()
        return " ".join(text.split())
    if key == "gaps":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            return value
        return sorted({x.strip().lower() for x in value})
    if key == "baseline_commit":
        return value.strip().lower() if isinstance(value, str) else value
    if key == "missing":
        if not isinstance(value, dict):
            return value
        return {k: (value.get(k).strip() if isinstance(value.get(k), str) else value.get(k))
                for k in ("name", "target")}
    return value


def same(key, want, got):
    if isinstance(want, bool):
        return got is want
    return json.dumps(norm(key, want), sort_keys=True) == json.dumps(norm(key, got), sort_keys=True)


fixture = Path(os.environ["FIXTURE_DIR"])
cid = load(fixture / "scenarios.json").get("case")
if cid not in EXPECTED:
    fail("unknown case")
expected = EXPECTED[cid]


def unreadable(message):
    GRADED.extend((sid, False, message) for sid in expected)
    fail(cid + ": " + message)


try:
    got = parse(Path("result.json"))
except (OSError, ValueError) as exc:
    unreadable(f"missing or invalid result.json: {exc}"[:200])
if not isinstance(got, dict) or not isinstance(got.get("scenarios"), list):
    unreadable("expected an object with a scenarios array")
rows = got["scenarios"]
if not all(isinstance(r, dict) and isinstance(r.get("id"), str) for r in rows):
    unreadable("invalid scenario row")
by_id, duplicated = {}, set()
for r in rows:
    if r["id"] in by_id:
        duplicated.add(r["id"])
    by_id[r["id"]] = r
extra = sorted(set(by_id) - set(expected))
failed = 0
for sid, want in expected.items():
    row = by_id.get(sid)
    if row is None or sid in duplicated:
        GRADED.append((sid, False, "scenario missing" if row is None else "duplicate rows"))
        failed += 1
        continue
    wrong = [k for k in want if k not in row or not same(k, want[k], row[k])]
    GRADED.append((sid, not wrong, "wrong " + ",".join(wrong) if wrong else "decision and evidence match"))
    failed += bool(wrong)
if extra:
    fail(cid + ": invented scenarios " + ",".join(extra))
for p in fixture.rglob("*"):
    if p.is_file():
        rel = p.relative_to(fixture)
        if not Path(rel).is_file() or Path(rel).is_symlink() or Path(rel).read_bytes() != p.read_bytes():
            fail(cid + ": changed or missing input " + rel.as_posix())
if failed:
    fail(f"{cid}: {failed} of {len(expected)} scenarios wrong")
print("ok")
scenario_line()
