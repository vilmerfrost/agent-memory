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
                native=AL.native_session_id(data['agent'],data['native_session_id'])
                if linked and linked[0] and native and linked[0]==native:
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
