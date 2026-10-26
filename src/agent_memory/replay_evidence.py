"""Bounded ingestion of verified replay observations, including non-improving
experiments. Evidence persistence never creates proposals or authorization.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent_memory import attempt_links as AL, rsi_artifact as RA, rsi_approval as AP, store


@dataclass
class Report:
    experiments: int = 0
    artifacts: int = 0
    evaluations: int = 0
    attempts: int = 0
    native_sessions: int = 0
    unresolved_native: int = 0
    missing_artifacts: int = 0
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def register_evaluations(conn, adir: Path, experiment_id: str) -> dict[str, Any]:
    """Validate every record before registering immutable tested bytes.
    All verdicts are ingested. A raw improves record still proves only the
    artifact it tested; it does not override intended-intervention or approval
    gates in the orchestrator.
    """
    if adir.is_symlink() or (adir / "manifest.json").is_symlink():
        raise RA.ArtifactError("symlinked artifact metadata")
    manifest, digest, _ = RA.load_artifact(adir)
    if any((adir / "files" / f['after_sha256']).is_symlink() for f in manifest['files']):
        raise RA.ArtifactError("symlinked artifact content")
    if manifest.get("experiment_id") != experiment_id:
        raise RA.ArtifactError("replay artifact belongs to another experiment")
    records = RA.eval_records(adir)
    for rec in records:
        if (rec.get("artifact_hash"), rec.get("experiment_id"), rec.get("baseline_id")) != (
                digest, experiment_id, manifest.get("baseline_id")):
            raise RA.ArtifactError("evaluation artifact/experiment/baseline binding mismatch")
        if rec.get("verdict") not in ("pass", "improves", "no-effect", "worse", "fail", "inconclusive"):
            raise RA.ArtifactError("invalid evaluation verdict")
    before = conn.execute("select count(*) from rsi_eval_results where artifact_hash=%s", (digest,)).fetchone()[0]
    AP.register_artifact(conn, adir)
    after = conn.execute("select count(*) from rsi_eval_results where artifact_hash=%s", (digest,)).fetchone()[0]
    return {"artifact_hash": digest, "evaluations_added": after - before, "evaluations_available": len(records)}


def ingest(conn, *, root: Path | None = None, limit: int = 100) -> Report:
    """Existing daily owner recovers committed experiments' evidence from disk.
    Each experiment has its own savepoint. Missing/invalid files are explicit
    gaps; they never produce an invented passing evaluation or native session.
    """
    root = (root or RA.sandbox_dir()).resolve()
    report = Report()
    rows = conn.execute("select id,meta->>'artifact_hash' from rsi_experiments "
                        "order by created_at desc,id limit %s", (max(1, min(limit, 500)),)).fetchall()
    for exp, digest in rows:
        report.experiments += 1
        if digest and RA.HEX64.fullmatch(digest):
            adir = root / "artifacts" / digest
            if not adir.is_dir():
                report.missing_artifacts += 1
            else:
                try:
                    if adir.is_symlink() or not adir.resolve().is_relative_to(root):
                        raise RA.ArtifactError("artifact path escapes sandbox")
                    with conn.transaction():
                        result = register_evaluations(conn, adir, exp)
                    report.artifacts += 1
                    report.evaluations += result["evaluations_added"]
                except Exception as exc:
                    report.failures.append(f"{exp}: evaluation ingestion {type(exc).__name__}")
        # Only known replay run records beneath this experiment are read. The
        # model's workspace and stdout are not an identity authority.
        base = root / "replays" / exp
        files = sorted(base.glob("runs/*.json")) + sorted(base.glob("*/runs/*.json"))
        for path in files[:1000]:
            if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > 2_000_000:
                continue
            try:
                row = json.loads(path.read_text())
                if not isinstance(row, dict) or row.get("arm") not in AL.ATTEMPT_ARMS:
                    continue
                at = AL.Attempt(exp, row["arm"], row["case"], row["rep"], row.get("profile") or None)
                directory_profile = None if path.parent.parent == base else path.parent.parent.name
                if directory_profile != at.profile:
                    raise ValueError("replay profile directory mismatch")
                expected_run = AL._run_id_for(exp + (f"-{at.profile}" if at.profile else ""), at.case, at.arm, at.rep)
                if row.get("run_id") != expected_run or path.stem != expected_run:
                    raise ValueError("run identity mismatch")
                # Older records have no native id. Preserve that uncertainty.
                data = {"experiment_id": exp, "profile": at.profile, "arm": at.arm, "case": at.case,
                        "rep": at.rep, "run_id": expected_run, "replay_record": path.relative_to(root).as_posix(),
                        "agent": row.get("agent"), "native_session_id": row.get("native_session_id"),
                        "model": row.get("model"), "started_at": row.get("started_at"),
                        "duration_s": row.get("duration_s"), "error": bool(row.get("error"))}
                with conn.transaction():
                    report.attempts += AL.record_runner(conn, [data], source="repair")
                linked = conn.execute("select session_id from rsi_attempts where id=%s", (at.key,)).fetchone()
                if linked and linked[0]:
                    report.native_sessions += 1
                else:
                    report.unresolved_native += 1
            except Exception as exc:
                report.failures.append(f"{exp}: attempt ingestion {type(exc).__name__}")
    AL.reconcile_native_sessions(conn)
    return report


def run(*, root: Path | None = None, db_url: str | None = None, limit: int = 100) -> Report:
    with store.connect(db_url) as conn:
        return ingest(conn, root=root, limit=limit)
