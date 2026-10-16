# RSI V1 formats: artifact, evaluation, decision

Mission 003. Shared by `agent-config/bin/improve` (stage, promote, recover, revert-change) and
`agent-memory` (proposals, decisions, the promotion worker). Both sides compute the same hashes.
A test vector in each repo proves it (`artifact_hash(EXAMPLE) == EXAMPLE_HASH`).

## Canonical JSON and hashes

- Canonical JSON: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, UTF-8.
- `sha256(...)` is the full lowercase hex digest. It is never shortened in a binding. Short forms
  appear only in display text.

## Artifact (`rsi-artifact/1`)

An artifact is the exact set of file contents that was tested. It is immutable once written.

```
<root>/<artifact_hash>/
  manifest.json          canonical manifest (below); its sha256 is the artifact hash
  files/<after_sha256>   the after bytes of each file
  before/<before_sha256> the baseline bytes each change was made against (for review and diff)
  evals/<suite>.json     evaluation records bound to this hash (added later; NOT part of the hash)
```

`<root>` is `$RSI_SANDBOX_DIR/artifacts` (default `~/sandbox/artifacts`). Files and dirs are made
read-only after writing (0444 / 0555).

Manifest:

```json
{
  "format": "rsi-artifact/1",
  "experiment_id": "exp-20261020-01",
  "baseline_id": "bl-3f2a9c1d0e4b",
  "proposal_kind": "edit_instruction",
  "activation": "none",
  "files": [
    {"path": "~/.claude/CLAUDE.md", "kind": "instruction", "rule_id": "claude-md-global--closing-line",
     "before_sha256": "<64 hex>", "after_sha256": "<64 hex>", "mode": "0644"}
  ]
}
```

- `files` is sorted by `path`. Every path is `~`-relative, and no path appears twice.
- `before_sha256` is the sha256 of the baseline file the candidate was made from. In V1 it must exist
  (no new files) and `after_sha256` must exist (no deletions).
- `activation`: `none` (the file is read live) or `manual` (launchd, hooks: a human runs the printed
  command after promotion).
- `artifact_hash = sha256(canonical(manifest))`.

## Evaluation record (`rsi-eval/1`)

```json
{
  "format": "rsi-eval/1",
  "artifact_hash": "<64 hex>", "experiment_id": "…", "baseline_id": "…",
  "suite": "static" | "sandbox-static" | "sandbox-replay" | "…",
  "suite_version": "1",
  "model": "claude-opus-5-5" | "none", "effort": "high" | "none",
  "verdict": "pass" | "improves" | "no-effect" | "worse" | "fail" | "inconclusive",
  "started_at": "ISO", "finished_at": "ISO",
  "runner": "improve 1 / rsi_sandbox 1",
  "results": {}
}
```

A record binds to exactly one artifact. Promotion requires one passing record per required suite,
where `artifact_hash` and `baseline_id` equal the manifest's. Passing verdicts: `pass`, `improves`.

Writers: `improve stage` writes `static`. `rsi_sandbox.py record-eval` writes sandbox suites. It refuses
unless the candidate arm was built by `prepare-arm --artifact <hash>`, holds the artifact's exact bytes
after sanitizing, and the control arm is the same baseline without the artifact.

Required suites come from `criteria.json` `promotion.required_suites` (default `["static", "sandbox-replay"]`).
`sandbox-replay` needs model credentials in the sandbox. Until it exists, nothing is promotable
(fail-closed).

## Decision (`rsi-decision/1`)

The promotion worker writes this after it verified a decision row in the database, and passes it to
`improve promote`:

```json
{
  "format": "rsi-decision/1",
  "decision_id": "<uuid>", "proposal_id": "<uuid>",
  "action": "apply",
  "artifact_hash": "<64 hex>",
  "decided_at": "ISO", "expires_at": "ISO",
  "source": "phone" | "cli",
  "verified": "edge-token" | "cli-tty"
}
```

`improve promote` refuses when any of these hold:
- the action is not `apply`;
- the hash differs from the artifact on disk or from `--expect-hash`;
- the decision has expired;
- the decision id was already consumed for another artifact;
- a required evaluation is missing;
- a live file matches neither `before_sha256` nor `after_sha256` (stale baseline or conflict);
- the process runs inside an agent session (`CLAUDECODE`, `CODEX_*`, `IMPROVE_SESSION`, `RSI_SANDBOX`).

## Promotion write-ahead log

`~/.claude/parent/improve/wal/<change_id>.json` (not in git). Fields: `change_id`, `op` (promote or revert),
`artifact_hash`, `decision_id`, `state` (`intent` → `written` → `done` | `aborted`), and `files`
(path, real path, before_sha256, after_sha256, snapshot). The WAL is fsynced before the first production
write.

`improve recover`, for each WAL that is not finished:
- all files at their target sha → finish (ledger + commit);
- all files at their source sha → abort;
- mixed → restore every file from the snapshot, then abort and report.

## Change ids

Promoted changes get ids `prm-YYYYMMDD-N` in the improve ledger. The `promoted` event carries the
artifact hash, the decision id and the file list. `improve revert-change <prm-id>` reverses one
promoted change exactly, or reports a conflict and changes nothing.
