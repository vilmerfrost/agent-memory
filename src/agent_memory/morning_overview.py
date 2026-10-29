"""Evidence-bound morning narration. Pure functions; never executes suggested actions."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import re
from pathlib import Path

from agent_memory import rsi_brief

VERSION = 'morning-overview/1'
ZONE = ZoneInfo('Europe/Stockholm')
MAX_CHARS = 9000
SERVICES = json.loads(Path(__file__).with_name('morning_remedies.json').read_text())
MESSENGERS = {'routine-audit', 'config-check'}
CORRECTIONS = {
    key: 'Later checker defects make the recorded improvement or regression conclusion unreliable. Nothing was applied from this experiment.'
    for key in ('exp-20261027-5a8bda33', 'exp-20261027-7c731284', 'exp-20261027-90b819e4')
}


def clean(value, limit=350):
    # Existing scrub removes secret patterns, home paths and emails. Free text is
    # source data, never a prompt or a command. No raw model prose is narrated.
    return rsi_brief.scrub(rsi_brief.text(value, limit)).strip()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def moment(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)


def window(now):
    local = moment(now).astimezone(ZONE)
    day = local.date()
    # After noon the morning still belongs to this calendar day. The nightly
    # report's key is the preceding date, independent of the host timezone.
    start = datetime.combine(day - timedelta(days=1), time(18), ZONE)
    end = min(local, datetime.combine(day, time(12), ZONE))
    return (day - timedelta(days=1)).isoformat(), start, end


def remedy(routine, reason, recovered=False):
    if recovered:
        return ('No repair is indicated by the latest passing result.', '', 'Check the next scheduled result remains passing.', 'monitor')
    lower = reason.lower()
    service_id = next((key for key in SERVICES if key == routine or (key not in ('auth','mac','database') and key in lower)), None)
    if 'docker' in lower:
        return ('Check whether Docker is running on the Mac.', 'If it is running, inspect the worker’s Docker connection.', 'Check docker info and a fresh Docker heartbeat in Night Desk; this does not verify the whole night.', 'investigate')
    if re.search(r'quota|rate.?limit|usage.?limit|allowance', lower):
        return ('Check the provider’s allowance and reset time.', 'Wait for an explicitly reported limit to reset before retrying.', 'Check the relevant connection, then the next routine result.', 'check')
    if re.search(r'401|403|credential|unauthori[sz]ed|sign.?in|authentication', lower):
        return ('Check the named service’s account sign-in and permissions on the Mac.', 'If sign-in has expired, reconnect through its normal sign-in flow.', 'Test that service again and check the next routine result.', 'check')
    if re.search(r'timeout|timed out', lower):
        return ('Check that the Mac is awake and online, then inspect the service response time.', 'If the service is unavailable, wait until it is ready before one retry.', 'Check that the affected routine completes on its next authorized run.', 'investigate')
    if routine == 'secret-scan':
        return ('Review the private secret-scan report on the Mac.', 'Determine whether the finding is real before changing a credential.', 'Run the focused secret check after the reviewed fix.', 'investigate')
    if service_id:
        return (SERVICES[service_id]['step'], '', 'Check the relevant Night Desk connection, then a new result from the affected routine.', 'check')
    return ('Open this routine’s details and use the investigation prompt to find the cause.', 'Inspect the recorded report before making a change.', 'After the reviewed fix, check a new passing result for this routine.', 'investigate')


def snapshot(night_row, runs, *, now, annotations=None, extras=None, ingestion_fresh=False, truncated=False):
    night, start, end = window(now)
    as_of = moment(now)
    row = night_row if night_row and night_row.get('night') == night else {}
    brief, report = row.get('brief') or {}, row.get('report') or {}
    corrections = {**CORRECTIONS, **(annotations or {})}
    missing = []
    if not row:
        missing.append('The expected morning report is not available. The night’s conclusion is unknown.')
    if not ingestion_fresh:
        missing.append('The latest routine upload could not be confirmed. Some recent runs may be missing.')
    if truncated:
        missing.append('The routine read reached its limit. This is not a complete list of runs.')
    if any(extra.get('unavailable') for extra in (extras or [])):
        missing.append('Some extra morning alerts could not be read.')
    grouped = {}
    for run in runs:
        try:
            at = moment(run['started_at'])
        except (KeyError, ValueError, TypeError):
            missing.append('A routine record had no usable time.')
            continue
        if start <= at < end and at <= as_of:
            grouped.setdefault(str(run.get('routine') or 'Unknown routine'), []).append(run)
    facts = []
    def fact(id, text, section, refs=None):
        facts.append(dict(id=id, text=text, section=section, evidence_refs=refs or []))
    local = end.astimezone(ZONE)
    fact('cutoff', f'This is your morning overview for {local.strftime("%d %B %Y")}, with recorded results through {local.strftime("%H:%M")} Stockholm time.', 'opening')
    if missing:
        fact('coverage', 'This overview has limited coverage. ' + ' '.join(dict.fromkeys(missing)), 'opening')
    status = str(row.get('status') or 'unknown')
    statuses = {'ok': 'The nightly job finished.', 'partial': 'The night finished only part of its work.',
                'aborted': 'The night was aborted.', 'budget_exhausted': 'The night stopped at its resource limit.',
                'preflight_failed': 'The night was skipped because a prerequisite check did not pass.', 'unknown': 'No conclusion is available for the expected night.'}
    fact('night-status', statuses.get(status, 'The night has no recognized final status.'), 'outcome', [f'night:{night}'])
    sessions = (brief.get('sessions') or {}).get('sessions')
    if isinstance(sessions, int) and not isinstance(sessions, bool) and sessions >= 0 and status != 'preflight_failed':
        fact('sessions', f'The report analyzed {sessions} sessions in its recorded checkpoint window. That window can extend beyond last night.', 'outcome', [f'night:{night}'])
    for index, pattern in enumerate((brief.get('patterns') or {}).get('items') or []):
        if index >= 3:
            break
        summary = clean(pattern.get('summary'), 260)
        if summary:
            fact(f'pattern:{index}', 'A recorded pattern: ' + summary, 'outcome', [f'night:{night}'])
    warnings = brief.get('prerequisite_warnings') or []
    for index, warning in enumerate(warnings[:3]):
        fact(f'warning:{index}', 'A prerequisite warning was recorded: ' + clean(warning, 220), 'skips', [f'night:{night}'])
    issues = []
    for routine, history in sorted(grouped.items()):
        history.sort(key=lambda r: (moment(r['started_at']), str(r.get('id', ''))), reverse=True)
        failed = [r for r in history if r.get('status') in ('fail', 'error')]
        latest = history[0]
        if not failed:
            if latest.get('status') == 'skip':
                fact('skip:' + routine, f'{clean(routine, 80)} was skipped. Recorded reason: {clean(latest.get("failure_reason") or latest.get("summary")) or "No reason was recorded."}', 'skips', [f'run:{latest.get("id")}'])
            continue
        recent = failed[0]
        failures = (recent.get('meta') or {}).get('failures') or []
        reason = clean(recent.get('failure_reason') or '; '.join(str(x) for x in failures[:3]))
        recovered = latest.get('status') == 'pass'
        next_step, alternative, verify, kind = remedy(routine, reason, recovered)
        state = 'recovered' if recovered else 'skipped_after_failure' if latest.get('status') == 'skip' else 'failing' if latest.get('status') in ('fail', 'error') else 'unknown'
        issue_id = 'routine:' + routine
        refs = [f'run:{r.get("id")}' for r in failed]
        issue = dict(id=issue_id, routine=clean(routine, 80), failed_run_ids=[str(r.get('id')) for r in failed],
                     failure_count=len(failed), latest_result=latest.get('status'), state=state,
                     recovered_at=str(latest.get('started_at')) if recovered else None, recorded_reason=reason or 'No reason was recorded.',
                     cause_certainty='recorded' if reason else 'unknown', next_step=next_step,
                     alternative_if_needed=alternative, verification_step=verify, action_kind=kind,
                     evidence_refs=refs, report_path=clean(recent.get('report_path')),
                     messenger=routine in MESSENGERS)
        issues.append(issue)
        outcome = 'It subsequently passed.' if recovered else 'The latest run was skipped; recovery is not confirmed.' if state == 'skipped_after_failure' else 'The latest result is still a failure.' if state == 'failing' else 'Its current recovery is unknown.'
        detail = f'Recorded reason: {reason}.' if reason else 'The report did not record why it failed.'
        fact(issue_id, f'{clean(routine, 80)} failed {len(failed)} time{"s" if len(failed) != 1 else ""}. {outcome} {detail} {next_step} {alternative} To verify: {verify}', 'recovered' if recovered else 'failures', refs)
    for exp in report.get('experiments') or []:
        ident = str(exp.get('experiment_id') or '')
        if not ident:
            continue
        text = corrections.get(ident)
        if text:
            detail = clean(text, 600)
        elif exp.get('status') in ('deferred_policy', 'skipped_budget'):
            detail = 'This experiment did not execute because of policy or a resource limit.'
        elif exp.get('status') in ('failed', 'interrupted'):
            detail = 'Experiment execution failed or was interrupted. ' + (clean(exp.get('skip_reason') or '; '.join(exp.get('verdict_reasons') or [])) or 'No specific cause was recorded.') + ' Inspect its experiment details before deciding on a repair.'
        else:
            detail = {'IMPROVES': 'The stored comparison reported improvement; acceptance and your approval are separate requirements.', 'WORSE': 'The comparison reported a regression.', 'NO_EFFECT': 'The comparison found no clear difference.', 'INCONCLUSIVE': 'The comparison was inconclusive.'}.get(exp.get('verdict'), 'No supported comparison conclusion was recorded.')
        fact('experiment:' + ident, detail, 'outcome', ['experiment:' + ident])
    decisions = (brief.get('pending_decisions') or {}).get('items') or []
    if decisions:
        fact('decisions', f'{len(decisions)} decisions were waiting when the brief was saved. Review their current status in Night Desk before acting.', 'decisions', [f'night:{night}'])
    if extras:
        for index, extra in enumerate(extras):
            fact(f'alert:{index}', clean(extra.get('text'), 500), 'decisions', extra.get('evidence_refs') or [])
    open_issues = [i for i in issues if i['state'] != 'recovered' and not i['messenger']]
    if not open_issues and not missing and not decisions and not extras:
        fact('closing', 'No unresolved routine failure or pending decision was found in the available results.', 'closing')
    else:
        fact('closing', 'Your next steps and their source details are listed beside this transcript in Night Desk.', 'closing')
    result = dict(format=VERSION, night=night, as_of=as_of.isoformat(), window_start=start.isoformat(), window_end=end.isoformat(),
                  brief_generated_at=brief.get('generated_at'), coverage='limited' if missing else 'complete',
                  coverage_reasons=missing, status=status, facts=facts, issues=issues,
                  annotations=corrections, decisions=decisions, remedy_version=VERSION + ':' + digest(SERVICES)[:12])
    result['input_digest'] = digest({key: value for key, value in result.items() if key not in ('as_of', 'window_end', 'facts')} | {'facts': [f for f in facts if f['id'] != 'cutoff']})
    return result


def render(data, outline=None):
    """The model can order facts inside a section, never add or edit speech text."""
    facts = {f['id']: f for f in data['facts']}
    order = list(facts)
    if outline is not None:
        proposed = outline.get('fact_ids')
        if not isinstance(proposed, list) or len(proposed) != len(facts) or set(proposed) != set(facts):
            raise ValueError('outline_coverage')
        order = proposed
    sections = ('opening', 'outcome', 'failures', 'recovered', 'skips', 'decisions', 'closing')
    script = '\n\n'.join(facts[id]['text'] for section in sections for id in order if facts[id]['section'] == section)
    if len(script) > MAX_CHARS:
        raise ValueError('script_too_long')
    return script


def investigation_prompt(issue):
    return f'Investigate the scheduled routine {issue["routine"]}. Recorded reason: {issue["recorded_reason"]}. Evidence: {", ".join(issue["evidence_refs"])}. Read the current report first, explain the root cause, and propose the smallest fix before changing anything. Verify with a new result. Keep secrets private.'
