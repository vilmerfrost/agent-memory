"""Data-family registry (docs/data-use/data-families.md, plan section 3).

Every data family names its producers, owner job, storage, consumers, retention and exclusion policy.
Every table and view that supabase/migrations creates belongs to exactly one family, or is listed in
EXCLUDED with a reason. `check()` parses the migrations and reports any table that is neither, so a new
table cannot slip in unclassified (tests/test_data_use_registry.py).

Consumers are only the ones the code points to. `kind` says how a consumer uses the data:
  analysis     an automatic routine reads it to produce findings or candidates
  export       an automatic sync copies it to an external system
  report       an automatic report or Night Desk shows it
  operational  a job's own bookkeeping (cursors, retries, gates, auth)
  manual       a CLI command a person runs on demand
A learning family with no `analysis` consumer is a Stage E gap (`gaps()`).

The markdown is generated from this module (`render_markdown`); the test fails when the checked-in doc
differs, so the two cannot drift.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from agent_memory import config

ConsumerKind = Literal["analysis", "export", "report", "operational", "manual"]
CONSUMER_KINDS: tuple[str, ...] = ("analysis", "export", "report", "operational", "manual")
NONE_FOUND = "none found"

# Tables the migration runner creates in code, not in supabase/migrations (store.migrate).
RUNTIME_TABLES = frozenset({"schema_migrations"})


@dataclass(frozen=True)
class Consumer:
    name: str            # job or command
    where: str           # module or file the claim points to
    kind: ConsumerKind


@dataclass(frozen=True)
class Family:
    key: str
    title: str
    purpose: str
    producers: tuple[str, ...]
    owner_job: str
    tables: tuple[str, ...] = ()        # public tables (bare name) or schema-qualified non-public tables
    views: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()       # family data that lives as columns of another family's table
    local: tuple[str, ...] = ()         # local files
    external: tuple[str, ...] = ()      # external sinks
    consumers: tuple[Consumer, ...] = ()
    retention: str = ""
    exclusion: str = ""
    learning_input: bool = True         # False: never a general learning input (auth, credentials)

    def consumer_kinds(self) -> set[str]:
        return {c.kind for c in self.consumers}


def _c(name: str, where: str, kind: ConsumerKind) -> Consumer:
    return Consumer(name, where, kind)


NIGHT = "amem-rsi-night (amem rsi night-job)"
DAILY = "amem-daily (amem daily)"
BRIEF = "amem-rsi-brief (amem rsi brief-job)"
OUTCOME = "amem-rsi-outcome (amem rsi outcome)"
DECIDE = "amem-rsi-decide (amem rsi decide-job)"
NIGHT_DESK = "Night Desk (external web app, owner JWT; dashboard_owner_read policy)"
SYNC_CH = "sync clickhouse (amem daily step_sync)"
SYNC_PD = "sync pastdev (amem daily step_sync)"
SYNC_LF = "sync langfuse (amem daily step_sync)"
HARVEST = "evals harvest (agent-config nightly caller, outside this repo)"

NO_RAW = "No transcript text, secrets or raw private content: only redacted, capped fields leave the producer."

FAMILIES: tuple[Family, ...] = (
    Family(
        key="sessions",
        title="Sessions, outcomes and summaries",
        purpose="Find recurring problems and assess task results",
        producers=("sessions_ingest.ingest (amem sessions ingest, inside amem daily step_deviations)",
                   "session_live (amem sessions ping, Claude Code hook)",
                   "session_summary (redacted 1-3 sentence summaries)",
                   "tools/amem_digest.py (cloud routine/chat POST to cloud_digests)"),
        owner_job=DAILY,
        tables=("sessions", "cloud_digests"),
        views=("session_groups", "dashboard_sessions"),
        local=("~/.claude/projects/**/*.jsonl (Claude transcripts, read in place)",
               "~/.codex/sessions/**/rollout-*.jsonl (Codex transcripts, read in place)",
               "~/.claude/parent/amem-session-spool.jsonl (ping spool)",
               "amem archive (encrypted transcript archive, docs/archive.md)"),
        external=("ClickHouse sessions (pseudonymized columns)",),
        consumers=(_c("deviations scan", "src/agent_memory/deviations.py", "analysis"),
                   _c(NIGHT, "src/agent_memory/rsi_evidence_pack.py", "analysis"),
                   _c(OUTCOME, "src/agent_memory/rsi_outcome.py", "analysis"),
                   _c(HARVEST, "src/agent_memory/evals_harvest.py", "analysis"),
                   _c(BRIEF, "src/agent_memory/rsi_brief.py", "report"),
                   _c(SYNC_CH, "src/agent_memory/integrations/clickhouse.py", "export"),
                   _c(NIGHT_DESK, "supabase/migrations/20261024230827_night_desk.sql", "report"),
                   _c("amem sessions find / tokens report", "src/agent_memory/session_find.py", "manual")),
        retention="Rows kept indefinitely (no deletion job in this repo). Transcripts stay on disk; "
                  "`amem archive prune` ages out local archive copies.",
        exclusion=NO_RAW + " Sessions with env sandbox/eval are kept out of production evaluations "
                  "(sessions.env). Night Desk sees only granted columns (no paths, meta or summaries).",
    ),
    Family(
        key="models_costs",
        title="Models, effort, tokens and costs",
        purpose="Compare cost and outcome for comparable work",
        producers=("sessions_ingest / session_live (tokens_*, models, model, effort, efforts columns)",
                   "rsi_budget (night cost ledger in the night report)"),
        owner_job=DAILY,
        columns=("sessions.tokens_input", "sessions.tokens_output", "sessions.tokens_cache_read",
                 "sessions.tokens_cache_creation", "sessions.models", "sessions.model", "sessions.effort",
                 "sessions.efforts", "rsi_nights.costs"),
        local=("config/rsi-prices.json (versioned price table)",),
        external=("ClickHouse sessions token columns",),
        consumers=(_c(SYNC_CH, "src/agent_memory/integrations/clickhouse.py", "export"),
                   _c("night budget", "src/agent_memory/rsi_budget.py", "operational"),
                   _c(BRIEF, "src/agent_memory/rsi_brief.py", "report"),
                   _c("amem sessions tokens", "src/agent_memory/tokens_report.py", "manual")),
        retention="Same as sessions (columns of that table).",
        exclusion="Measured cost, estimated cost and subscription tokens are kept apart; token counts alone "
                  "never justify a cheaper model.",
    ),
    Family(
        key="command_timings",
        title="Command and tool timings",
        purpose="Find bottlenecks and repeated failed operations",
        producers=("timing collector (deploy/hooks/amem-timing-hook.py -> timing_collector)",
                   "timing_sync (transcript reconstruction)", "timing_store.ingest_spools (amem timing ingest)"),
        owner_job="timing collector (amem timing collect, local service)",
        tables=("execution_spans",),
        views=("session_timing_summary", "session_parent_timing", "session_timing_coverage"),
        local=("~/.claude/parent/**/timing-events.jsonl (spool)", "timing cursor file next to the spool"),
        consumers=(_c("profile refresh (sessions.api_seconds / tool_seconds)", "src/agent_memory/timing_store.py",
                      "operational"),
                   _c("amem timing report / coverage", "src/agent_memory/cli_timing.py", "manual")),
        retention="No pruning found: spans are kept indefinitely.",
        exclusion="Operation names and categories only; no command text or output. Reconstructed and "
                  "estimated spans (measurement_kind) never count as measured latency.",
    ),
    Family(
        key="test_timings",
        title="Test runs and individual test timings",
        purpose="Detect slow tests, regressions and worker imbalance",
        producers=("scripts/integration_tests.py + test_timings.Capture (pytest plugin)",
                   "test_timings.upload / flush_queue (via the timing collector)"),
        owner_job="timing collector under parent-run",
        tables=("test_timing_runs", "test_timing_cases"),
        local=("<git dir>/test-logs/*.timings.json (batches beside each log)", "timing upload queue run-*.json"),
        consumers=(_c("retention prune", "src/agent_memory/timing_collector.py", "operational"),
                   _c("amem timing tests", "src/agent_memory/test_timings.py", "manual")),
        retention="Case rows 30 days (test_timings.prune, run by the collector); run summaries kept "
                  "(details_retained=false after pruning).",
        exclusion="Measurements and node ids only, never test logs or captured output.",
    ),
    Family(
        key="skills",
        title="Skill calls and scores",
        purpose="Identify weak or ineffective skills",
        producers=("amem ingest skills (~/.agents/skill-usage/log.jsonl; records rsi_checkpoints "
                   "skills-ingest/latest with the log read time)",
                   "amem skills scores --refresh (records rsi_checkpoints skills-refresh/<run> with its input window)"),
        owner_job="rsi-nightly skill refresh (agent-config routine, outside this repo)",
        tables=("skill_calls", "skill_scores"),
        local=("~/.agents/skill-usage/log.jsonl",),
        external=("ClickHouse skill_calls",),
        consumers=(_c(NIGHT + " discovery", "src/agent_memory/rsi_night.py", "analysis"),
                   _c(NIGHT + " skill_refresh prerequisite", "src/agent_memory/skill_refresh.py", "analysis"),
                   _c(SYNC_CH, "src/agent_memory/integrations/clickhouse.py", "export"),
                   _c("amem skills scores / suggest", "src/agent_memory/skills.py", "manual")),
        retention="skill_calls kept indefinitely; skill_scores rewritten on every refresh.",
        exclusion="Skill name, project bucket and cwd only; no prompts.",
    ),
    Family(
        key="deviations",
        title="Instruction sources and deviations",
        purpose="Assess adherence against the correct instructions",
        producers=("amem deviations scan (amem daily step_deviations)", "amem deviations reref",
                   "sessions_ingest (instruction_sources capture: authority, version, texts)",
                   "amem sessions instruction-sources --apply (historical backfill, bounded and resumable)"),
        owner_job=DAILY,
        tables=("deviations", "instruction_texts"),
        columns=("sessions.instructions", "sessions.skills", "sessions.instruction_sources",
                 "deviations.instruction_authority"),
        local=("instruction files read by rule_catalog (CLAUDE.md, AGENTS.md, skills) at scan time",
               "~/.claude/plugins/installed_plugins.json (plugin skill resolution, read only)",
               "git history of tracked instruction files (versions in force at a session start)",
               "~/.claude/parent/amem-daily-state.json (pushed ids)"),
        external=("ClickHouse deviations", "past.dev (through proposals/edits)"),
        consumers=(_c(NIGHT, "src/agent_memory/rsi_evidence_pack.py", "analysis"),
                   _c(OUTCOME, "src/agent_memory/rsi_outcome.py", "analysis"),
                   _c(HARVEST, "src/agent_memory/evals_harvest.py", "analysis"),
                   _c("amem daily alerts / escalations", "src/agent_memory/alerts.py", "report"),
                   _c(BRIEF, "src/agent_memory/rsi_brief.py", "report"),
                   _c(SYNC_CH, "src/agent_memory/integrations/clickhouse.py", "export"),
                   _c(NIGHT_DESK, "supabase/migrations/20261024230827_night_desk.sql", "report")),
        retention="Kept indefinitely. instruction_sha is the hash of the text graded against: the recorded version "
                  "when deviations.instruction_authority is observed or reconstructed, today's file when it is "
                  "unverified (plan section 5). instruction_texts is insert-only and content-addressed.",
        exclusion="The writer's own words only: no transcript quotes, paths, code or secrets. instruction_texts "
                  "holds instruction files only (never transcript text), redacted, at most 100 kB each, only from "
                  "the Claude / Codex homes, agent-config and the session's own repo and ancestor memory files; "
                  "never env files.",
    ),
    Family(
        key="routine_runs",
        title="Routine runs, scores and labels",
        purpose="Detect recurring failures and weak evaluations",
        producers=("amem log-run / store.insert_run (every routine)", "amem ingest routines-log (amem daily step_ingest)",
                   "amem ingest tasks (dispatch runs, amem daily step_tasks)", "amem score (amem daily step_score)",
                   "amem label (manual)"),
        owner_job=DAILY,
        tables=("runs", "scores", "labels"),
        local=("~/.claude/parent/routines.log", "cloud routines.log on the routine-reports branch"),
        external=("ClickHouse runs, scores",),
        consumers=(_c("amem daily escalations", "src/agent_memory/scoring.py", "analysis"),
                   _c("amem lessons distill (amem daily step_distill)", "src/agent_memory/distill.py", "analysis"),
                   _c(NIGHT, "src/agent_memory/rsi_evidence_pack.py", "analysis"),
                   _c(SYNC_CH, "src/agent_memory/integrations/clickhouse.py", "export"),
                   _c(NIGHT_DESK, "supabase/migrations/20261024230827_night_desk.sql", "report"),
                   _c("amem ab report", "src/agent_memory/ab.py", "manual")),
        retention="Kept indefinitely (dedupe merges duplicates, amem db dedupe).",
        exclusion="Summaries and failure reasons are one line each; report_path points at a local file.",
    ),
    Family(
        key="coordination",
        title="Tasks, handoffs and worker incidents",
        purpose="Find coordination failures and missing checks",
        producers=("amem handoffs log / resume (Stop hook and handoff skill)",
                   "amem ingest tasks (dispatch tasks land in runs, source dispatch)",
                   "sessions ingest (worker report_blocked / report_partial counts in outcome_evidence)"),
        owner_job=DAILY,
        tables=("handoffs",),
        columns=("runs (source = dispatch)", "sessions.outcome_evidence.report_blocked/report_partial"),
        local=("~/.claude/parent/<repo>/tasks.jsonl", "handoff files (handoffs.handoff_path)",
               "~/.claude/parent/<repo>/checkpoints/*.md"),
        consumers=(_c(HARVEST, "src/agent_memory/evals_harvest.py", "analysis"),
                   _c("amem lessons distill (dispatch runs)", "src/agent_memory/distill.py", "analysis"),
                   _c("amem handoffs list", "src/agent_memory/handoffs.py", "manual")),
        retention="Kept indefinitely.",
        exclusion="Handoff and checkpoint text stays in local files; only paths, reasons and counts are stored.",
    ),
    Family(
        key="lessons",
        title="Lessons and lesson-use history",
        purpose="Preserve useful conclusions and assess their value",
        producers=("amem lessons import / distill", "amem lessons for (logs lesson_uses)"),
        owner_job=DAILY,
        tables=("lessons", "lesson_uses", "lesson_candidates"),
        local=("~/.claude/parent/lessons.md (round trip)",),
        external=("past.dev lesson_candidates",),
        consumers=(_c("amem lessons for (retrieval into briefs)", "src/agent_memory/lessons.py", "manual"),
                   _c(SYNC_PD, "src/agent_memory/integrations/pastdev.py", "export"),
                   _c("amem ab report", "src/agent_memory/ab.py", "manual")),
        retention="Kept indefinitely; retired lessons keep their row (status retired).",
        exclusion="Lesson text is written by the distill template or a person; no transcript text.",
    ),
    Family(
        key="experiments",
        title="Experiments, evaluations and outcomes",
        purpose="Compare attempts and avoid repeating failed approaches",
        producers=("amem rsi night (experiments, candidates, artifacts, eval results)",
                   "rsi_replay sandbox runs", "amem rsi night experiment stage (rsi_attempts at creation)",
                   "amem coverage repair-links (historical rsi_attempts, explicit identifiers only, undo manifest)",
                   "amem rsi outcome (rsi_outcomes)"),
        owner_job=NIGHT,
        tables=("rsi_experiments", "rsi_candidates", "rsi_artifacts", "rsi_eval_results", "rsi_attempts",
                "rsi_outcomes"),
        local=("<RSI sandbox>/experiments/<id>/logs/*.json (sandbox run logs and .rsi.json sidecars)",
               "<RSI sandbox>/replays/<id>[/<profile>]/runs/*.json (replay run records)",
               "<RSI sandbox>/nights/<night>/report.json", "bench/rsi-replay cases"),
        external=("Langfuse traces and scores (sandbox runs)", "past.dev experiments", "ClickHouse rsi_eval_results"),
        consumers=(_c(NIGHT + " discovery", "src/agent_memory/rsi_night.py", "analysis"),
                   _c(NIGHT + " evidence pack", "src/agent_memory/rsi_evidence_pack.py", "analysis"),
                   _c(BRIEF, "src/agent_memory/rsi_brief.py", "report"),
                   _c(SYNC_LF, "src/agent_memory/integrations/langfuse.py", "export"),
                   _c(SYNC_PD, "src/agent_memory/integrations/pastdev.py", "export"),
                   _c(SYNC_CH, "src/agent_memory/integrations/clickhouse.py", "export"),
                   _c(NIGHT_DESK, "supabase/migrations/20261024230827_night_desk.sql", "report"),
                   _c("amem coverage links / repair-links (attempt links)", "src/agent_memory/attempt_links.py",
                      "manual")),
        retention="Kept indefinitely. Artifacts and eval results are append-only (rsi_immutable trigger). "
                  "Candidates are never deleted, only retired. rsi_attempts: runner rows are never deleted; a "
                  "repair's rows are removed only by its own undo or when a later repair resolves them.",
        exclusion="Sandbox sessions only; production transcripts never enter an experiment. rsi_attempts holds ids, "
                  "sandbox-relative paths and refs, never run output. Eval-harness sessions are not attempts.",
    ),
    Family(
        key="decisions",
        title="Proposals, decisions, edits and undo history",
        purpose="Understand what was accepted, rejected or reversed",
        producers=("amem rsi night / edits writer (proposals, staged edits)", "amem-decide edge function and "
                   "amem rsi decide (decisions)", "amem rsi decide-job (decision results, undo)",
                   "amem edits undo / undo-poll"),
        owner_job=DECIDE,
        tables=("rsi_proposals", "rsi_decisions", "rsi_decision_results", "edits", "undo_requests"),
        views=("rsi_decision_status", "edit_undo_status"),
        external=("past.dev proposals, decisions, edits", "ClickHouse rsi_proposals, rsi_decisions"),
        consumers=(_c(NIGHT + " evidence pack (history)", "src/agent_memory/rsi_evidence_pack.py", "analysis"),
                   _c(OUTCOME, "src/agent_memory/rsi_outcome.py", "analysis"),
                   _c("proposal supersede", "src/agent_memory/rsi_supersede.py", "operational"),
                   _c(SYNC_PD, "src/agent_memory/integrations/pastdev.py", "export"),
                   _c(SYNC_CH, "src/agent_memory/integrations/clickhouse.py", "export"),
                   _c(NIGHT_DESK, "supabase/migrations/20261025213700_rsi_decision_status.sql", "report")),
        retention="Kept indefinitely. Decisions are append-only; proposals expire by status (72 h), never by "
                  "deletion.",
        exclusion="Undo token hashes stay owner-only and never reach the browser or a learning input.",
    ),
    Family(
        key="reports_checkpoints",
        title="Reports, ledger events and checkpoints",
        purpose="Establish progress, freshness and recovery state",
        producers=("amem rsi night / brief (rsi_checkpoints, nights)", "amem sync run (sync:<sink> cursors)",
                   "dashboard ingest (rsi_nights, report_snapshots, ledger_events)"),
        owner_job=BRIEF,
        tables=("rsi_checkpoints", "rsi_nights", "report_snapshots", "ledger_events"),
        local=("~/.claude/parent/reports/*", "<RSI sandbox>/nights/<night>/report.json"),
        consumers=(_c("night/brief dependency checks", "src/agent_memory/rsi_night.py", "operational"),
                   _c("sync cursors", "src/agent_memory/integrations/sync.py", "operational"),
                   _c(NIGHT_DESK, "supabase/migrations/20261024230827_night_desk.sql", "report")),
        retention="Checkpoints and nights kept indefinitely; report_snapshots one row per kind and date.",
        exclusion="Summaries only; report bodies stay in local files.",
    ),
    Family(
        key="night_read_state",
        title="Night Desk read status",
        purpose="Sync the owner's unread report inbox across devices",
        producers=("Night Desk explicit Mark as read / Mark unread actions (owner session)",),
        owner_job="Night Desk owner",
        tables=("night_reads",),
        consumers=(_c("Night Desk inbox (external web app, owner JWT)",
                      "supabase/migrations/20261026160937_night_desk_reads.sql", "report"),),
        retention="Kept until marked unread or the referenced night is removed.",
        exclusion="Never a learning input. Reading does not resolve failures or approve changes; automation has no access.",
        learning_input=False,
    ),
    Family(
        key="coverage",
        title="Coverage snapshots and routine consumption",
        purpose="Record what each routine read, used and skipped, and what evidence was missing",
        producers=("amem-daily: deviations scan, score, sync (coverage_daily.py)",
                   "rsi-skills: amem skills scores --refresh (coverage_daily.py)",
                   "rsi-harvest: amem evals harvest (coverage_daily.py)",
                   "amem-rsi-night: S1 delta and S4 evidence pack (night_coverage.py)"),
        owner_job="each consuming routine, through coverage.record_best_effort (R4)",
        tables=("coverage_snapshots", "routine_consumption"),
        consumers=(_c("amem-rsi-brief coverage verdict", "src/agent_memory/rsi_brief.py", "report"),
                   _c("amem coverage (contract checks)", "src/agent_memory/cli_coverage.py", "manual")),
        retention="One row per dataset and window, and per job run and dataset (natural keys, upserts). "
                  "No per-row audit trail.",
        exclusion="Counts, checkpoints and at most 20 evidence refs per row; never row contents. Reporting "
                  "input (Stage F), not a learning input.",
        learning_input=False,
    ),
    Family(
        key="evidence_links",
        title="External evidence links",
        purpose="Confirm identity, relevance and current availability",
        producers=("amem sync run (evidence links per pushed batch)", "evidence providers (rsi_evidence_providers)",
                   "amem integrations ncm record"),
        owner_job=NIGHT,
        tables=("rsi_evidence",),
        external=("ClickHouse", "Langfuse", "past.dev", "NCM"),
        consumers=(_c(NIGHT + " evidence pack", "src/agent_memory/rsi_evidence_pack.py", "analysis"),
                   _c("evidence.resolve (re-read amem: subject refs)", "src/agent_memory/integrations/evidence.py",
                      "operational"),
                   _c(SYNC_CH + " (research_runs)", "src/agent_memory/integrations/clickhouse.py", "export")),
        retention="Kept indefinitely; valid_until marks staleness, not deletion.",
        exclusion="Summaries sanitized and capped at 2000 chars; external results support a finding only "
                  "after their amem: refs resolve in Supabase.",
    ),
    Family(
        key="auth_recovery",
        title="Authentication and recovery records",
        purpose="Support access control and operational health",
        producers=("amem rsi push (decision tokens)", "amem-decide edge function (TOTP, passkeys, challenges)",
                   "amem rsi totp-enroll / passkey enroll"),
        owner_job=DECIDE,
        tables=("rsi_decision_tokens", "rsi_totp_used", "rsi_totp_failures", "rsi_passkeys",
                "rsi_passkey_enroll_codes", "rsi_web_challenges", "amem_private.dashboard_owner"),
        consumers=(_c("decision functions (token, TOTP, passkey checks)", "src/agent_memory/rsi_approval.py",
                      "operational"),
                   _c("amem-decide edge function", "supabase/functions/amem-decide", "operational")),
        retention="Tokens and challenges expire by time; TOTP steps and failures are kept for replay and "
                  "lockout checks.",
        exclusion="Never a learning input. Owner-only: amem_app reads only the status columns of "
                  "rsi_decision_tokens (never token_hash) and nothing else here. A routine may expose a bounded "
                  "count (e.g. failed codes), never the records.",
        learning_input=False,
    ),
    Family(
        key="config_assets",
        title="Schema and configuration assets",
        purpose="Verify that jobs use compatible contracts",
        producers=("amem db migrate (schema_migrations)", "amem cloud publish-script (amem_assets)"),
        owner_job="amem db migrate (by hand after a merge)",
        tables=("schema_migrations", "amem_assets"),
        local=("supabase/migrations/*.sql", "config/*.json (night, prices, verdict policy, evidence providers)",
               "~/agent-config/improve/criteria.json, pins.json (read only)"),
        consumers=(_c("migration runner", "src/agent_memory/store.py", "operational"),
                   _c("cloud setup script", "deploy/cloud/setup.sh", "operational"),
                   _c("night config load", "src/agent_memory/rsi_night.py", "operational"),
                   _c("data-family registry check", "src/agent_memory/data_families.py", "operational")),
        retention="Kept for the life of the schema.",
        exclusion="Configuration, not behaviour data: never a learning input by itself.",
        learning_input=False,
    ),
    Family(
        key="eval_drafts",
        title="Evaluation drafts",
        purpose="Turn learned failures into regression cases",
        producers=("amem evals harvest (draft case files and the harvest ledger)",),
        owner_job=HARVEST,
        local=("<drafts dir>/*.md (agent-config evals case format)", "<drafts dir>/.harvest-ledger.jsonl"),
        consumers=(_c("agent-config promotion step (outside this repo)", "docs/evals-harvest.md", "manual"),),
        retention="Files stay until promoted or deleted by hand; the harvest cap bounds new drafts per run.",
        exclusion="Drafts are redacted; their instructions are data, not tasks (plan section 10).",
    ),
)

# Tables that belong to no family on purpose: name -> reason. Empty today: every table has a family.
EXCLUDED: dict[str, str] = {}


# --------------------------------------------------------------------------- lookups


def family(key: str) -> Family:
    for f in FAMILIES:
        if f.key == key:
            return f
    raise KeyError(key)


def assignments() -> dict[str, list[str]]:
    """Table or view -> the families that claim it (a correct registry has exactly one each)."""
    out: dict[str, list[str]] = {}
    for f in FAMILIES:
        for name in (*f.tables, *f.views):
            out.setdefault(name, []).append(f.key)
    return out


def gaps() -> list[Family]:
    """Learning families with no automatic analysis consumer (the Stage E gaps)."""
    return [f for f in FAMILIES if f.learning_input and "analysis" not in f.consumer_kinds()]


# --------------------------------------------------------------------------- migrations


_COMMENT = re.compile(r"--[^\n]*")
_NAME = r"(?:(?P<schema>[a-z_][a-z0-9_]*)\.)?(?P<name>[a-z_][a-z0-9_]*)"
_CREATE_TABLE = re.compile(rf"\bcreate\s+(?:unlogged\s+)?table\s+(?:if\s+not\s+exists\s+)?{_NAME}", re.I)
_CREATE_VIEW = re.compile(rf"\bcreate\s+(?:or\s+replace\s+)?(?:materialized\s+)?view\s+(?:if\s+not\s+exists\s+)?{_NAME}",
                          re.I)
_RENAME = re.compile(rf"\balter\s+table\s+(?:if\s+exists\s+)?{_NAME}\s+rename\s+to\s+(?P<new>[a-z_][a-z0-9_]*)", re.I)
_DROP = re.compile(rf"\bdrop\s+(?P<kind>table|view|materialized\s+view)\s+(?:if\s+exists\s+)?{_NAME}", re.I)


def _qualified(m: re.Match[str]) -> str:
    schema = (m.group("schema") or "public").lower()
    name = m.group("name").lower()
    return name if schema == "public" else f"{schema}.{name}"


@dataclass
class Schema:
    tables: set[str] = field(default_factory=set)
    views: set[str] = field(default_factory=set)


def migration_schema(directory: Path | None = None) -> Schema:
    """Tables and views the migrations leave behind, in file order (creates, renames and drops)."""
    out = Schema()
    for path in sorted((directory or config.MIGRATIONS_DIR).glob("*.sql")):
        text = _COMMENT.sub("", path.read_text())
        events: list[tuple[int, str, re.Match[str]]] = []
        for kind, pat in (("table", _CREATE_TABLE), ("view", _CREATE_VIEW), ("rename", _RENAME), ("drop", _DROP)):
            events += [(m.start(), kind, m) for m in pat.finditer(text)]
        for _, kind, m in sorted(events, key=lambda e: e[0]):
            name = _qualified(m)
            if kind == "table":
                out.tables.add(name)
            elif kind == "view":
                out.views.add(name)
            elif kind == "rename":
                new = name.rsplit(".", 1)[0] + "." + m.group("new").lower() if "." in name else m.group("new").lower()
                if name in out.tables:
                    out.tables.discard(name)
                    out.tables.add(new)
            else:
                (out.tables if m.group("kind").lower() == "table" else out.views).discard(name)
    return out


def check(directory: Path | None = None) -> list[str]:
    """Problems that make the registry incomplete or wrong; empty when every table has one home."""
    schema = migration_schema(directory)
    known = schema.tables | schema.views | RUNTIME_TABLES
    claimed = assignments()
    problems: list[str] = []
    for name in sorted(schema.tables | schema.views):
        homes = claimed.get(name, [])
        if name in EXCLUDED:
            if homes:
                problems.append(f"{name}: excluded but also assigned to {', '.join(homes)}")
        elif not homes:
            problems.append(f"{name}: created by a migration but assigned to no family and not excluded")
        elif len(homes) > 1:
            problems.append(f"{name}: assigned to more than one family ({', '.join(homes)})")
    for name, homes in sorted(claimed.items()):
        if name not in known:
            problems.append(f"{name}: assigned to {', '.join(homes)} but no migration creates it")
    for name in sorted(EXCLUDED):
        if name not in known:
            problems.append(f"{name}: excluded but no migration creates it")
        if not EXCLUDED[name].strip():
            problems.append(f"{name}: excluded without a reason")
    keys = [f.key for f in FAMILIES]
    for k in sorted({k for k in keys if keys.count(k) > 1}):
        problems.append(f"family key {k} is used twice")
    for f in FAMILIES:
        for attr in ("purpose", "owner_job", "retention", "exclusion"):
            if not getattr(f, attr).strip():
                problems.append(f"{f.key}: {attr} is empty")
        if not f.producers:
            problems.append(f"{f.key}: no producer")
        if not (f.tables or f.views or f.columns or f.local or f.external):
            problems.append(f"{f.key}: no storage")
        for c in f.consumers:
            if c.kind not in CONSUMER_KINDS:
                problems.append(f"{f.key}: consumer {c.name} has unknown kind {c.kind}")
    return problems


# --------------------------------------------------------------------------- output


def as_json() -> dict:
    return {
        "families": [{**asdict(f), "consumer_kinds": sorted(f.consumer_kinds()),
                      "stage_e_gap": f in gaps()} for f in FAMILIES],
        "excluded": dict(EXCLUDED),
        "gaps": [f.key for f in gaps()],
    }


def _lines(items: tuple[str, ...]) -> str:
    return "; ".join(f"`{i}`" if " " not in i else i for i in items) if items else "none"


def render_markdown() -> str:
    out = ["# Data families", "",
           "Generated from `src/agent_memory/data_families.py` by `amem coverage families --markdown`. "
           "Do not edit by hand: `tests/test_data_use_registry.py` fails when this file and the registry differ.",
           "",
           "Every table and view in `supabase/migrations` belongs to exactly one family below "
           "(or to the exclusion list). Consumer kinds: analysis (an automatic routine draws findings from it), "
           "export (an automatic sync copies it out), report (an automatic report shows it), operational "
           "(bookkeeping), manual (a CLI command a person runs).",
           "",
           "## Stage E gaps", "",
           "Learning families with no automatic analysis consumer:", ""]
    out += [f"- **{f.title}** (`{f.key}`)" for f in gaps()] or ["- none"]
    for f in FAMILIES:
        out += ["", f"## {f.title}", "", f"- Key: `{f.key}`", f"- Purpose: {f.purpose}",
                f"- Producers: {'; '.join(f.producers)}", f"- Owner job: {f.owner_job}",
                f"- Tables: {_lines(f.tables)}", f"- Views: {_lines(f.views)}"]
        if f.columns:
            out.append(f"- Columns elsewhere: {_lines(f.columns)}")
        out += [f"- Local files: {'; '.join(f.local) or 'none'}",
                f"- External sinks: {'; '.join(f.external) or 'none'}",
                "- Consumers:" if f.consumers else f"- Consumers: {NONE_FOUND}"]
        out += [f"  - {c.kind}: {c.name} (`{c.where}`)" for c in f.consumers]
        if f.learning_input and "analysis" not in f.consumer_kinds():
            out.append(f"  - analysis: {NONE_FOUND} (Stage E gap)")
        out += [f"- Retention: {f.retention}", f"- Exclusion: {f.exclusion}",
                f"- Learning input: {'yes' if f.learning_input else 'no'}"]
    out += ["", "## Excluded tables", ""]
    out += [f"- `{k}`: {v}" for k, v in sorted(EXCLUDED.items())] or ["- none: every table has a family"]
    return "\n".join(out) + "\n"
