"""`amem rsi …` (docs/rsi/approval.md, docs/rsi/targets.md, docs/rsi/freeze.md).

Read-only: targets, baseline check, freeze-status, verify-decision, show.
Writes the RSI sandbox only: baseline create, tracking-plan (scripts, never run here).
Writes RSI tables: propose, push, decide (Vilmer's TTY), decide-poll (the single promotion worker), checkpoint.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from agent_memory import rsi_approval as AP, rsi_artifact as RA, rsi_targets as T, store
from agent_memory.cli_deviations import _read_guard, _write_guard


def _out(args: argparse.Namespace, obj: dict, line: str, code: int = 0) -> int:
    print(json.dumps(obj, sort_keys=True, default=str) if getattr(args, "json", False) else line)
    return code


def cmd_targets(args: argparse.Namespace) -> int:
    m = T.targets_manifest(T.describe(T.discover(include_schedules=not args.no_schedules)))
    untracked = [f["path"] for f in m["files"] if not f.get("tracked")]
    return _out(args, m, f"rsi targets: {len(m['files'])} files, {len(untracked)} not in git: "
                         + ", ".join(untracked[:8]) + (" …" if len(untracked) > 8 else ""))


def cmd_baseline_create(args: argparse.Namespace) -> int:
    try:
        bid, bdir = T.create_baseline(out=args.out.expanduser() if args.out else None)
    except (T.BaselineError, OSError) as exc:
        print(f"amem: rsi baseline create: refused: {exc}", file=sys.stderr)
        return 1
    return _out(args, {"baseline_id": bid, "dir": str(bdir)}, f"rsi baseline: {bid} -> {T.tilde(bdir)}")


def _baseline_dir(s: str) -> Path:
    p = Path(s).expanduser()
    return p if p.is_dir() else T.baselines_dir() / s


def cmd_baseline_check(args: argparse.Namespace) -> int:
    try:
        r = T.check_baseline(_baseline_dir(args.baseline))
    except (T.BaselineError, OSError, ValueError) as exc:
        print(f"amem: rsi baseline check: {exc}", file=sys.stderr)
        return 2
    line = (f"rsi baseline {r['baseline_id']}: {r['unchanged']}/{r['files']} unchanged"
            + (f"; changed: {', '.join(r['changed'])}" if r["changed"] else "")
            + (f"; missing: {', '.join(r['missing'])}" if r["missing"] else ""))
    return _out(args, r, line, 1 if r["drift"] else 0)


def cmd_tracking_plan(args: argparse.Namespace) -> int:
    plan = T.tracking_plan()
    out = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    for name in ("apply", "inverse"):
        p = out / f"tracking-{name}.sh"
        p.write_text(plan[name])
        p.chmod(0o700)
    (out / "tracking-plan.json").write_text(json.dumps(plan["items"], indent=1) + "\n")
    return _out(args, {"items": plan["items"], "dir": str(out)},
                f"rsi tracking-plan: {len(plan['items'])} files -> {T.tilde(out)}/tracking-apply.sh (+ inverse). Not run.")


def cmd_freeze_status(args: argparse.Namespace) -> int:
    st = T.freeze_status()
    line = ("FROZEN" + (" (stage-only: amem-daily stages proposals, nothing applies them)" if st["stage_only"] else "")
            if st["frozen"] else "NOT FROZEN: " + "; ".join(st["reasons"]))
    return _out(args, st, f"rsi freeze-status: {line}", 0 if st["frozen"] else 1)


def _artifact(s: str) -> Path:
    p = Path(s).expanduser()
    return p if p.is_dir() else RA.artifacts_dir() / s


def cmd_propose(args: argparse.Namespace) -> int:
    if (code := _write_guard("rsi propose")) is not None:
        return code
    try:
        with store.connect() as conn:
            pid = AP.create_proposal(conn, _artifact(args.artifact), title=args.title, summary=args.summary or "",
                                     rationale={"why": args.why or "", "tests": args.tests or ""},
                                     risk={"level": args.risk}, confidence=args.confidence,
                                     expected={"effect": args.expected or ""}, evidence_refs=args.evidence or [])
    except (RA.ArtifactError, psycopg.Error, OSError) as exc:
        print(f"amem: rsi propose failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return _out(args, {"proposal_id": pid}, f"rsi propose: {pid} staged (push with `amem rsi push`)")


def cmd_push(args: argparse.Namespace) -> int:
    if (code := _write_guard("rsi push")) is not None:
        return code
    res = AP.push_proposals(push=not args.no_push)
    return _out(args, {"pushed": res}, f"rsi push: {len(res)} proposal(s) " + " ".join(res))


def cmd_decide(args: argparse.Namespace) -> int:
    if (code := _write_guard("rsi decide")) is not None:
        return code
    try:
        from agent_memory import credentials
        admin = credentials.admin_db_url() if credentials.admin_present() else None   # dialog once split
    except credentials.AdminUnavailable as exc:
        print(f"amem: rsi decide: {exc}", file=sys.stderr)
        return 2
    try:
        with store.connect(admin) as conn:
            row = conn.execute("select artifact_hash, title, status from rsi_proposals where id::text = %s",
                               (args.proposal,)).fetchone()
            if row is None:
                print("amem: rsi decide: no such proposal", file=sys.stderr)
                return 1
            h, title, status = row
            if args.hash and args.hash != h:
                print("amem: rsi decide: --hash differs from the proposal's artifact; nothing recorded", file=sys.stderr)
                return 1
            if AP.agent_session_reason() or not sys.stdin.isatty():
                print("amem: rsi decide: refused outside Vilmer's own terminal (agent session or no TTY)", file=sys.stderr)
                return 2
            print(f"{args.action.upper()} proposal: {title}\nartifact {h}\nstatus {status}")
            typed = input(f"Type the first 8 characters of the artifact hash to {args.action}: ").strip()
            if typed != h[:8]:
                print("amem: rsi decide: confirmation did not match; nothing recorded", file=sys.stderr)
                return 1
            res = AP.record_cli_decision(conn, args.proposal, args.action, h, reason=args.reason)
    except AP.NotHuman as exc:
        print(f"amem: rsi decide: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"amem: rsi decide failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"rsi decide: {res} (the promotion worker acts within minutes)")
    return 0 if res in ("recorded", "already") else 1


def cmd_decide_page(args: argparse.Namespace) -> int:
    url = args.url or AP.decide_url()
    if not url:
        print("amem: rsi decide-page: no decide URL (set AMEM_RSI_DECIDE_URL or pass --url)", file=sys.stderr)
        return 1
    try:
        html = AP.render_decide_page(url)
    except (ValueError, OSError) as exc:
        print(f"amem: rsi decide-page: {exc}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(html, encoding="utf-8")
    print(f"rsi decide-page: wrote {args.out / 'index.html'} (endpoint {url}). Host it over HTTPS and set "
          "AMEM_RSI_DECIDE_PAGE to its URL; pass the same origin to `amem rsi totp-enroll --origin`.")
    return 0


def cmd_totp_enroll(args: argparse.Namespace) -> int:
    """Create the TOTP secret, show it once for the authenticator, confirm one code, then store it as the
    amem-decide function secret RSI_TOTP_SECRET (rsi_totp.set_function_secrets). Vilmer's terminal only."""
    import shutil
    import subprocess

    from agent_memory import rsi_totp
    if AP.agent_session_reason() or not sys.stdin.isatty() or not sys.stdout.isatty():
        print("amem: rsi totp-enroll: refused outside Vilmer's own terminal (agent session or no TTY)", file=sys.stderr)
        return 2
    secret = rsi_totp.new_secret()
    uri = rsi_totp.otpauth_uri(secret)
    qr = shutil.which("qrencode")
    if qr:
        subprocess.run([qr, "-t", "ansiutf8"], input=uri.encode(), check=False)   # stdin: the secret stays out of argv
    print(f"Add this to your authenticator app (scan the QR code, or enter the key by hand):\n  {secret}")
    typed = input("Type the 6-digit code it shows now: ").strip()
    if rsi_totp.verify(secret, typed) is None:
        print("amem: rsi totp-enroll: that code does not match; nothing was set", file=sys.stderr)
        return 1
    if args.no_set:
        print("Code confirmed. Not stored (--no-set): store RSI_TOTP_SECRET on amem-decide yourself.")
        return 0
    values = {"RSI_TOTP_SECRET": secret, **({"RSI_DECIDE_ORIGIN": args.origin} if args.origin else {})}
    code = rsi_totp.set_function_secrets(values, project_ref=args.project_ref)
    if code != 0:
        print(f"amem: rsi totp-enroll: the Supabase CLI refused (exit {code}); log in first, then run this again",
              file=sys.stderr)
        return 1
    print("rsi totp-enroll: done. Phone Apply / Undo now need a code from your authenticator.")
    return 0


def cmd_decide_poll(args: argparse.Namespace) -> int:
    if (code := _write_guard("rsi decide-poll")) is not None:
        return code
    try:
        rep = AP.decide_poll(push=not args.no_push)
    except psycopg.Error as exc:
        print(f"amem: rsi decide-poll failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    obj = {"handled": rep.handled, "expired": rep.expired, "stale": rep.stale, "busy": rep.busy, "recover": rep.recover}
    bad = any(o.get("outcome") == "needs-attention" for o in rep.recover)
    return _out(args, obj, rep.line() + (" recover=needs-attention" if bad else ""), 1 if bad else 0)


def cmd_verify_decision(args: argparse.Namespace) -> int:
    if (code := _read_guard("rsi verify-decision")) is not None:
        return code
    try:
        d = json.loads(Path(args.decision).read_text())
        with store.connect() as conn:
            ok, why = AP.verify_decision(conn, d)
    except (OSError, ValueError, psycopg.Error) as exc:
        ok, why, d = False, f"{type(exc).__name__}", {}
    return _out(args, {"ok": ok, "reason": None if ok else why, "decision_id": d.get("decision_id") if isinstance(d, dict) else None},
                f"rsi verify-decision: {'ok' if ok else 'NOT OK: ' + why}", 0 if ok else 1)


def cmd_show(args: argparse.Namespace) -> int:
    if (code := _read_guard("rsi show")) is not None:
        return code
    with store.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select p.*, (select json_agg(json_build_object('suite', e.suite, 'verdict', e.verdict, 'model', e.model)) "
                    "from rsi_eval_results e where e.artifact_hash = p.artifact_hash) as evals from rsi_proposals p "
                    "where p.id::text = %s", (args.proposal,))
        p = cur.fetchone()
    if p is None:
        print("amem: rsi show: no such proposal", file=sys.stderr)
        return 1
    return _out(args, dict(p), "\n".join(f"{k}: {v}" for k, v in p.items() if k not in ("meta",)))


def cmd_brief(args: argparse.Namespace) -> int:
    """The morning brief (rsi_brief). Read-only. --push refuses unless AMEM_RSI_BRIEF_PUSH=1 (off in Mission 005)."""
    from agent_memory import rsi_brief as BR
    if args.push and not BR.push_allowed():
        print(f"amem: rsi brief: {BR.PUSH_OFF}", file=sys.stderr)
        return 2
    try:
        brief = BR.make_brief(night=args.night, root=args.root.expanduser() if args.root else None)
    except BR.ReportError as exc:
        print(f"amem: rsi brief: {exc}", file=sys.stderr)
        return 1
    if args.push:
        brief["push"] = BR.push_brief(brief)
    print(json.dumps(brief, sort_keys=True, default=str) if args.json else BR.render_markdown(brief))
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    guard = _write_guard if args.op == "mark" else _read_guard
    if (code := guard("rsi checkpoint")) is not None:
        return code
    key = args.run_key or AP.night_key()
    with store.connect() as conn:
        if args.op == "mark":
            AP.checkpoint(conn, args.job, key, args.status)
            return _out(args, {"job": args.job, "run_key": key, "status": args.status}, f"rsi checkpoint: {args.job} {key} {args.status}")
        done = AP.upstream_done(conn, args.job, key)
    return _out(args, {"job": args.job, "run_key": key, "done": done}, f"rsi checkpoint: {args.job} {key} {'done' if done else 'not done'}",
                0 if done else 1)


def register(sub: argparse._SubParsersAction) -> None:
    rsi = sub.add_parser("rsi", help="RSI V1 approval-first: targets, baselines, proposals, decisions, promotion worker")
    s = rsi.add_subparsers(dest="rsi_cmd", required=True)

    from agent_memory import rsi_night                 # `amem rsi night` (Mission 005, rsi_night.register)
    rsi_night.register(s)
    from agent_memory import rsi_evidence_providers    # `amem rsi evidence-probe` (Mission 005.5, read-only)
    rsi_evidence_providers.register(s)
    from agent_memory import rsi_schedule              # `amem rsi night-job` / `brief-job` (Mission 005.5)
    rsi_schedule.register(s)

    p = s.add_parser("targets",help="production files the loop may target (sha256, git state); read-only")
    p.add_argument("--no-schedules", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_targets)

    b = s.add_parser("baseline", help="baseline snapshots under the RSI sandbox")
    bs = b.add_subparsers(dest="baseline_cmd", required=True)
    p = bs.add_parser("create", help="read-only copy of the targets into ~/sandbox/baselines/<id>")
    p.add_argument("--out", type=Path)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_baseline_create)
    p = bs.add_parser("check", help="drift between a baseline and the live files (exit 1 on drift)")
    p.add_argument("baseline")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_baseline_check)

    p = s.add_parser("tracking-plan", help="write apply + inverse scripts that git-track untracked targets (not run)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tracking_plan)

    p = s.add_parser("freeze-status", help="are the automated production writers frozen? (exit 0 frozen, 1 not)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_freeze_status)

    p = s.add_parser("propose", help="register a staged artifact and create a proposal")
    p.add_argument("--artifact", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--summary")
    p.add_argument("--why")
    p.add_argument("--tests")
    p.add_argument("--risk", choices=["low", "medium", "high"], default="medium")
    p.add_argument("--confidence", type=float)
    p.add_argument("--expected")
    p.add_argument("--evidence", action="append")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_propose)

    p = s.add_parser("push", help="issue decision tokens and push each new staged proposal (Apply / Reject)")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_push)

    p = s.add_parser("decide", help="Vilmer's terminal only: record apply / reject / undo for a proposal")
    p.add_argument("proposal")
    p.add_argument("action", choices=["apply", "reject", "undo"])
    p.add_argument("--hash")
    p.add_argument("--reason")
    p.set_defaults(func=cmd_decide)

    p = s.add_parser("decide-page", help="write the static decide page (Apply needs a TOTP code) for hosting")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--url", default=None, help="the amem-decide URL (default: AMEM_RSI_DECIDE_URL or the project URL)")
    p.set_defaults(func=cmd_decide_page)

    p = s.add_parser("totp-enroll", help="Vilmer's terminal only: the TOTP secret for phone Apply / Undo")
    p.add_argument("--project-ref", default=None)
    p.add_argument("--origin", default=None, help="the decide page origin, stored as RSI_DECIDE_ORIGIN (CORS)")
    p.add_argument("--no-set", action="store_true", help="confirm a code only; store nothing")
    p.set_defaults(func=cmd_totp_enroll)

    p = s.add_parser("decide-poll", help="the single promotion worker: handle pending decisions")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_decide_poll)

    p = s.add_parser("verify-decision", help="(improve promote) confirm a decision file against the database")
    p.add_argument("--decision", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify_decision)

    p = s.add_parser("show", help="one proposal with its evaluations")
    p.add_argument("proposal")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = s.add_parser("brief", help="the morning brief for a night report (read-only; Apply / Reject / Details commands)")
    p.add_argument("--night", help="night key (default: the latest night with a report)")
    p.add_argument("--root", type=Path, help="the RSI sandbox root (default: RSI_SANDBOX_DIR or ~/sandbox)")
    p.add_argument("--push", action="store_true", help="also send a summary push (refused unless AMEM_RSI_BRIEF_PUSH=1)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_brief)

    p = s.add_parser("checkpoint", help="dependency-aware scheduling: mark or check a job's run")
    p.add_argument("op", choices=["mark", "check"])
    p.add_argument("job")
    p.add_argument("--run-key")
    p.add_argument("--status", choices=["running", "done", "failed", "skipped"], default="done")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_checkpoint)
