"""Bounded ingestion of verified replay observations, including non-improving
experiments. Evidence persistence never creates proposals or authorization.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent_memory import attempt_links as AL, rsi_artifact as RA, rsi_approval as AP, store, redact as RD


@dataclass
class Report:
    experiments: int = 0
    artifacts: int = 0
    evaluations: int = 0
    attempts: int = 0
    attempt_records: int = 0
    recovered_profiles: int = 0
    legacy_logical_ids: int = 0
    excluded_records: int = 0
    missing_start: int = 0
    native_sessions: int = 0
    unresolved_native: int = 0
    missing_artifacts: int = 0
    failures: list[str] = field(default_factory=list)
    failures_total: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    unresolved_native_reasons: dict[str, int] = field(default_factory=dict)

    def failure(self, code: str, exp: str, path: str, exc: Exception) -> None:
        """Bounded details with complete aggregate counts; no raw run output."""
        self.failures_total += 1
        self.failure_counts[code] = self.failure_counts.get(code, 0) + 1
        if len(self.failures) < 50:
            reason = RD.redact(str(exc)).replace("\n", " ")[:160]
            self.failures.append(RD.redact(f"{exp}: {code} {type(exc).__name__}: {reason} [{path[:240]}]")[:600])

    def unresolved(self, reason: str) -> None:
        self.unresolved_native += 1
        self.unresolved_native_reasons[reason] = self.unresolved_native_reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "failure_details_omitted": max(0, self.failures_total - len(self.failures))}


def register_evaluations(conn, adir: Path, experiment_id: str) -> dict[str, Any]:
    """Validate every record before registering immutable tested bytes.
    All verdicts are ingested. A raw improves record still proves only the
    artifact it tested; it does not override intended-intervention or approval
    gates in the orchestrator.
    """
    if adir.is_symlink() or (adir / "manifest.json").is_symlink():
        raise RA.ArtifactError("symlinked artifact metadata")
    if (adir/'files').is_symlink():
        raise RA.ArtifactError("symlinked artifact content directory")
    try:
        header=json.loads((adir/'manifest.json').read_bytes())
    except (OSError,ValueError):
        raise RA.ArtifactError('unreadable artifact metadata') from None
    files=header.get('files',[]) if isinstance(header,dict) else []
    if any(isinstance(f,dict) and RA.HEX64.fullmatch(str(f.get('after_sha256','')))
           and (adir/'files'/f['after_sha256']).is_symlink() for f in files):
        raise RA.ArtifactError("symlinked artifact content")
    manifest, digest, _ = RA.load_artifact(adir)
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


class RecoveryError(ValueError):
    """A saved record lacks a consistent exact identity; code is bounded vocabulary."""
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _read_record(path: Path, root: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise RecoveryError("unsafe_replay_path", "replay metadata symlink or sandbox escape")
    if path.stat().st_size > 2_000_000:
        raise RecoveryError("oversized_replay_record", "replay metadata exceeds 2000000 bytes")
    try:
        row = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RecoveryError("unreadable_replay_record", "replay metadata is not a readable JSON object") from exc
    if not isinstance(row, dict):
        raise RecoveryError("unreadable_replay_record", "replay metadata is not a JSON object")
    return row


def attempt_record(path: Path, *, root: Path, experiment_id: str) -> tuple[AL.Attempt, dict[str, Any], bool, bool]:
    """Validate current and historical run records using exact producer evidence.

    Old provider runs omitted row.profile but recorded it in the directory,
    replay config and run ID. Recover that explicit profile, not a native actor
    ID. Historical session_id is a logical UUID and never a native fallback.
    """
    base = root / "replays" / experiment_id
    directory = path.parent.parent
    if path.parent.name != "runs" or directory not in (base, base / directory.name):
        raise RecoveryError("replay_directory_mismatch", "run record is outside its experiment replay directory")
    profile = None if directory == base else directory.name
    if any(p.is_symlink() for p in (base, directory, path.parent)):
        raise RecoveryError("unsafe_replay_path", "symlinked replay directory")
    row = _read_record(path, root)
    if row.get("arm") in AL.NOT_ATTEMPT_ARMS:
        raise RecoveryError("non_attempt_record", "judge/check/implementer record is excluded from A/B attempts")
    if row.get("arm") not in AL.ATTEMPT_ARMS:
        raise RecoveryError("invalid_attempt_arm", "run record is not an A/B attempt")
    stated = row.get("profile")
    if stated is not None and stated != profile:
        raise RecoveryError("replay_profile_mismatch", "run profile differs from its exact replay directory")
    recovered = stated is None and profile is not None
    meta_path = directory / "replay.json"
    metadata = _read_record(meta_path, root) if meta_path.exists() else None
    if metadata is not None:
        if metadata.get("experiment_id") != experiment_id:
            raise RecoveryError("replay_experiment_mismatch", "replay metadata names another experiment")
        cfg = metadata.get("config")
        if isinstance(cfg, dict):
            if cfg.get("profile") is not None and cfg["profile"] != profile:
                raise RecoveryError("replay_config_profile_mismatch", "replay config profile differs from its directory")
            if cfg.get("agent") is not None and row.get("agent") is not None and cfg["agent"] != row["agent"]:
                raise RecoveryError("replay_provider_mismatch", "run provider differs from its replay config")
    if recovered and (metadata is None or not isinstance(metadata.get("config"), dict)
                      or metadata["config"].get("profile") != profile):
        raise RecoveryError("legacy_profile_unconfirmed", "missing run profile needs matching replay config and directory")
    try:
        if not isinstance(row.get("rep"), int) or isinstance(row["rep"], bool) or row["rep"] < 0:
            raise ValueError("repetition must be a non-negative integer, not a boolean")
        at = AL.Attempt(experiment_id, row["arm"], row["case"], row["rep"], profile)
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError("invalid_attempt_identity", "case, repetition or profile is not a valid attempt identity") from exc
    expected_run = AL._run_id_for(experiment_id + (f"-{profile}" if profile else ""), at.case, at.arm, at.rep)
    if row.get("run_id") != expected_run or path.stem != expected_run:
        raise RecoveryError("replay_run_identity_mismatch", "filename and run ID must match experiment/profile/case/arm/repetition")
    logical = row.get("session_id")
    legacy = logical is not None and profile is not None and logical == AL.session_id_for(
        experiment_id, at.arm, at.case, at.rep, None)
    if logical is not None and logical != at.session_id and not legacy:
        raise RecoveryError("logical_attempt_id_mismatch", "recorded logical session ID matches neither canonical nor known legacy attempt UUID")
    data = {"experiment_id": experiment_id, "profile": profile, "arm": at.arm, "case": at.case,
            "rep": at.rep, "run_id": expected_run, "replay_record": path.relative_to(root).as_posix(),
            "agent": row.get("agent"), "native_session_id": row.get("native_session_id"),
            "model": row.get("model"), "started_at": row.get("started_at"),
            "duration_s": row.get("duration_s"), "error": bool(row.get("error"))}
    return at, data, recovered, legacy


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
                    report.failure("evaluation_ingestion", exp, adir.relative_to(root).as_posix(), exc)
        # Only known replay run records beneath this experiment are read. The
        # model's workspace and stdout are not an identity authority.
        base = root / "replays" / exp
        files = sorted(base.glob("runs/*.json")) + sorted(base.glob("*/runs/*.json"))
        if len(files) > 1000:
            report.failure("replay_file_limit", exp, base.relative_to(root).as_posix(),
                           ValueError(f"{len(files) - 1000} records beyond the 1000-file experiment bound"))
        for path in files[:1000]:
            try:
                at, data, recovered, legacy = attempt_record(path, root=root, experiment_id=exp)
                with conn.transaction():
                    report.attempts += AL.record_runner(conn, [data], source="repair")
                report.attempt_records += 1
                report.recovered_profiles += int(recovered)
                report.legacy_logical_ids += int(legacy)
                if data.get("started_at") is None:
                    report.missing_start += 1
                linked = conn.execute("select a.session_id,a.evidence,s.env,s.experiment_id,s.meta "
                    "from rsi_attempts a left join sessions s on s.session_id=a.session_id where a.id=%s",
                    (at.key,)).fetchone()
                evidence = linked[1] if linked and isinstance(linked[1], dict) else {}
                native = AL.native_session_id(evidence.get('provider'), evidence.get('native_session_id'))
                if linked and native and linked[0] == native and evidence.get('native_identity_source') == 'trusted_client_response' \
                        and linked[2] == 'sandbox' and linked[3] == exp and isinstance(linked[4], dict) \
                        and linked[4].get('attempt_ref') in (None, at.key):
                    report.native_sessions += 1
                else:
                    reason = ("native_session_scope_or_attempt_conflict" if native
                              else "client_id_not_recorded" if data['native_session_id'] is None
                              else "invalid_native_identity")
                    report.unresolved(reason)
            except Exception as exc:
                code = exc.code if isinstance(exc, RecoveryError) else "attempt_ingestion"
                if code == "non_attempt_record":
                    report.excluded_records += 1
                else:
                    report.failure(code, exp, path.relative_to(root).as_posix(), exc)
    AL.reconcile_native_sessions(conn)
    return report


def run(*, root: Path | None = None, db_url: str | None = None, limit: int = 100) -> Report:
    with store.connect(db_url) as conn:
        return ingest(conn, root=root, limit=limit)


def night_records(conn, *, night: str, start, end):
    """Expected completed replays, not just rows already in the eval table,
    provide the denominator. An empty eval table is not automatically healthy.
    """
    from agent_memory import coverage as CV
    from agent_memory.integrations import refs
    experiments = conn.execute("select id,meta from rsi_experiments where night=%s and status='evaluated'", (night,)).fetchall()
    good = 0
    evidence = []
    expected_clients = {}
    for exp, meta in experiments:
        suites = {r[0] for r in conn.execute("select suite from rsi_eval_results where experiment_id=%s "
                                           "and suite like 'sandbox-replay%%'", (exp,))}
        providers = [p.get('provider') for p in meta.get('provider_results', []) if isinstance(p, dict) and p.get('provider')]
        count = 0
        for provider in meta.get('provider_results', []):
            if isinstance(provider, dict) and provider.get('provider') in ('claude','codex'):
                for arm in (provider.get('arms') or {}).values():
                    runs = arm.get('runs') if isinstance(arm, dict) else None
                    count += runs if isinstance(runs,int) and not isinstance(runs,bool) and runs>=0 else 0
        if not providers and (meta.get('test_setup') or {}).get('agent') in ('claude','codex'):
            count = meta.get('replay_runs') if isinstance(meta.get('replay_runs'),int) else 0
        expected_clients[exp] = count
        expected = {'sandbox-replay-'+p for p in providers} if providers else {'sandbox-replay'}
        good += expected <= suites
        evidence.append(refs.amem('experiment',exp))
    n = len(experiments)
    attempts = conn.execute("""select e.id,count(*),count(s.session_id)
        from rsi_attempts a join rsi_experiments e on e.id=a.experiment_id
        left join sessions s on s.session_id=a.session_id and s.env='sandbox' and s.experiment_id=a.experiment_id
          and a.evidence->>'native_identity_source'='trusted_client_response'
        where e.night=%s and (a.profile in ('claude','codex') or a.evidence->>'provider' in ('claude','codex'))
        group by e.id""", (night,)).fetchall()
    captured = {exp:(int(n),int(linked)) for exp,n,linked in attempts}
    native_total = sum(max(expected_clients.get(exp,0),captured.get(exp,(0,0))[0])
                       for exp in set(expected_clients)|set(captured))
    native_collected = sum(n for n,_ in captured.values())
    native_linked = sum(linked for _,linked in captured.values())
    records = []
    for dataset,total,collected,linked,detail in (
            ('experiments:replay-evidence',n,int(good),int(good),'completed replay experiments with all expected provider suites'),
            ('experiments:native-attempt-sessions',native_total,native_collected,native_linked,
             'real-client attempt observations with explicit native session identity; stub/eval harness excluded')):
        gaps = total-linked
        records += [CV.Snapshot(dataset,start,end,detail,total,collected,linked,collected-linked,
                                measured_fields=('expected_population','exact_linkage'), last_analysed_at=end,
                                source_versions={'collector':'replay-evidence/1'}),
                    CV.Consumption('amem-rsi-night',night,dataset,'replay-evidence/1',start,end,
                                   queried=total,considered=total,used=total,findings=gaps,
                                   evidence_refs=evidence[:20] if total else (),
                                   detail=f'{linked}/{total} expected observations linked; missing stays unknown')]
    return records
