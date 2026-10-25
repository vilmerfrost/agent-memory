"""Night Desk's idempotent, allowlisted report and ledger ingestion.

No authentication credentials, secret findings, transcript content or eval traces
are copied. Missing cost stays null; it is never silently converted to zero.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from agent_memory import redact, rsi_brief, store

KINDS = {'evals': 'evals-parent', 'evals-parent': 'evals-parent', 'skill-evals': 'skill-evals',
         'improve': 'improve', 'secret-scan': 'secret-scan', 'config-check': 'config-check',
         'routine-audit': 'routine-audit'}
FILE = re.compile(r'^(evals-parent|evals|skill-evals|improve|secret-scan|config-check|routine-audit)-(\d{4}-\d{2}-\d{2})(?:-[^.]+)?\.json$')
CASE_FIELDS = ('id', 'name', 'status', 'verdict', 'seconds', 'attempts', 'cost_usd')
LEDGER_FIELDS = ('event', 'id', 't', 'target', 'rule', 'rule_id', 'topic', 'why', 'reason',
                 'revert_reason', 'status', 'kind', 'summary', 'artifact_hash', 'decision_id', 'files', 'commit')
CHANGE_CONTEXT_FIELDS = ('target', 'rule_id', 'topic', 'why', 'files', 'artifact_hash', 'decision_id')
USAGE_FIELDS = ('sessions', 'active_seconds', 'api_seconds', 'usd', 'cost_usd')
TOKEN_FIELDS = ('input', 'output', 'cache_read', 'cache_creation', 'total')


def clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact.redact(value)
    if isinstance(value, list):
        return [clean(x) for x in value]
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    return value


def number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(count(v) for v in value.values())
    if isinstance(value, list):
        return len(value)
    return int(value) if number(value) else 0


def cases(value: Any) -> list[dict]:
    """Take terminal test cases only. Parent totals must not double-count children."""
    result = []
    if isinstance(value, list):
        for v in value:
            result.extend(cases(v))
    elif isinstance(value, dict):
        children = [value[k] for k in ('results', 'fixtures', 'skills', 'cases') if k in value]
        if children:
            for child in children:
                result.extend(cases(child))
        elif any(k in value for k in ('status', 'verdict', 'cost_usd')):
            result.append(clean({k: value[k] for k in CASE_FIELDS if k in value}))
        else:
            for v in value.values():
                result.extend(cases(v))
    return result


def snapshot(kind: str, documents: list[tuple[Path, dict]]) -> tuple[dict, float | None]:
    if kind == 'secret-scan':
        # Never persist findings, paths, snippets, fingerprints or any free text.
        return ({k: sum(count(d.get(k)) for _, d in documents)
                 for k in ('findings', 'new', 'errors', 'unreadable', 'repos')}, None)
    items = []
    costs = []
    complete = []
    source_counts = []
    for path, doc in documents:
        rows = cases(doc)
        items.extend({**row, "report": clean(path.name)} for row in rows)
        cost = doc.get('cost_usd')
        measured_total = number(cost)
        if not measured_total:
            values = [x['cost_usd'] for x in rows if number(x.get('cost_usd'))]
            cost = sum(values) if values else None
            complete.append(bool(rows) and len(values) == len(rows))
        else:
            complete.append(True)
        costs.append(cost)
        source_counts.append({'report': path.name, 'cases': len(rows), 'cost_usd': cost,
                              **{k: doc[k] for k in ('pass', 'fail', 'inconclusive') if number(doc.get(k))}})
    known = sum(c for c in costs if c is not None)
    summary = {'cases': items, 'sources': source_counts, 'known_cost_usd': round(known, 6),
               'cost_complete': all(complete)}
    if kind == 'routine-audit':
        summary['cases'] = [clean({'status': row[0], 'name': row[1]})
                            for _, doc in documents for row in doc.get('rows', [])
                            if isinstance(row, list) and len(row) >= 2]
        summary['problem_count'] = sum(len(doc.get('problems', [])) for _, doc in documents)
    return summary, round(known, 6) if any(c is not None for c in costs) else None


def upsert_night(brief: dict, report: dict, *, db_url: str | None = None) -> None:
    errors = rsi_brief.validate_report(report)
    if errors or brief.get('night') != report.get('night') or brief.get('format') != rsi_brief.BRIEF_FORMAT:
        raise ValueError('invalid or mismatched night report')
    with store.connect(db_url) as conn:
        conn.execute('''insert into public.rsi_nights (night,status,headline,brief,report,costs,checkpoint)
          values (%s,%s,%s,%s,%s,%s,%s) on conflict (night) do update set
          status=excluded.status,headline=excluded.headline,brief=excluded.brief,report=excluded.report,
          costs=excluded.costs,checkpoint=excluded.checkpoint,updated_at=now()''',
          (brief['night'], brief['status'], brief['headline'], Jsonb(clean(brief)), Jsonb(clean(report)),
           Jsonb(clean(report['costs'])), Jsonb(clean(report['checkpoint']))))


def sync_reports(reports: Path, ledger: Path, *, db_url: str | None = None) -> dict:
    grouped = defaultdict(list)
    errors = []
    for path in sorted(reports.glob('*.json')):
        match = FILE.fullmatch(path.name)
        if not match:
            continue
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError('expected object')
            grouped[(KINDS[match[1]], match[2])].append((path, value))
        except (ValueError, OSError):
            errors.append(path.name)
    events = []
    contexts = {}
    if ledger.exists():
        lines = ledger.read_text().splitlines(keepends=True)
        for index, line in enumerate(lines):
            if not line.endswith('\n') and index == len(lines) - 1:
                continue  # writer has not committed its last line yet
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict) or not isinstance(raw.get('event'), str):
                    raise ValueError('invalid event')
                digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                summary = {k: raw[k] for k in LEDGER_FIELDS if k in raw}
                change_id = raw.get('id')
                if isinstance(change_id, str):
                    previous = contexts.get(change_id, {})
                    if previous:
                        # Explicitly label inherited identity; it is not this event's own reason/time.
                        summary['change_context'] = dict(previous)
                    contexts[change_id] = {**previous, **{k: raw[k] for k in CHANGE_CONTEXT_FIELDS if k in raw}}
                usage = raw.get('cost')
                if isinstance(usage, dict):
                    summary['cost'] = {k: usage[k] for k in USAGE_FIELDS if number(usage.get(k))}
                    tokens = usage.get('tokens')
                    if isinstance(tokens, dict):
                        summary['cost']['tokens'] = {k: tokens[k] for k in TOKEN_FIELDS if number(tokens.get(k))}
                if isinstance(raw.get('window'), dict):
                    summary['window'] = {k: raw['window'][k] for k in ('start', 'end') if isinstance(raw['window'].get(k), str)}
                events.append((digest, raw['event'], raw.get('t'), clean(summary)))
            except ValueError:
                errors.append(f'ledger line {index + 1}')
    with store.connect(db_url) as conn:
        for (kind, date), docs in grouped.items():
            summary, cost = snapshot(kind, docs)
            conn.execute('''insert into public.report_snapshots (kind,date,summary,cost_usd,report_path)
             values (%s,%s,%s,%s,%s) on conflict (kind,date) do update set summary=excluded.summary,
             cost_usd=excluded.cost_usd,report_path=excluded.report_path,updated_at=now()''',
             (kind, date, Jsonb(summary), cost, '\n'.join(str(p) for p, _ in docs)))
        for eid, event, at, summary in events:
            conn.execute('''insert into public.ledger_events (id,event,occurred_at,summary)
             values (%s,%s,%s,%s) on conflict (id) do update set summary=excluded.summary
             where public.ledger_events.summary is distinct from excluded.summary''', (eid, event, at, Jsonb(summary)))
    return {'reports': len(grouped), 'ledger_events': len(events), 'errors': errors}


def backfill_nights(reports: Path, root: Path, *, db_url: str | None = None) -> dict:
    result = {'nights': [], 'errors': []}
    for path in sorted((root / 'nights').glob('*/report.json')):
        night = path.parent.name
        if night < '2026-10-18' or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', night):
            continue
        try:
            report, _ = rsi_brief.load_report(path)
            saved = reports / f'rsi-brief-{night}.json'
            brief = json.loads(saved.read_text()) if saved.exists() else rsi_brief.make_brief(night=night, root=root, db_url=db_url)
            upsert_night(brief, report, db_url=db_url)
            result['nights'].append(night)
        except (ValueError, OSError, rsi_brief.ReportError):
            result['errors'].append(night)
    return result


def cmd_sync(args) -> int:
    from agent_memory import config
    if config.backend() != 'postgres' or config.remote_write_error('postgres'):
        print('dashboard sync requires the configured Postgres backend')
        return 1
    result = sync_reports(args.reports.expanduser(), args.ledger.expanduser())
    if args.backfill:
        result['backfill'] = backfill_nights(args.reports.expanduser(), args.root.expanduser())
    print(json.dumps(result))
    return int(bool(result['errors'] or result.get('backfill', {}).get('errors')))


def register(ingest_sub) -> None:
    p = ingest_sub.add_parser('dashboard', help='sync report summaries and improve ledger for Night Desk')
    p.add_argument('--reports', type=Path, default=Path('~/.claude/parent/reports'))
    p.add_argument('--ledger', type=Path, default=Path('~/agent-config/improve/ledger.jsonl'))
    p.add_argument('--root', type=Path, default=Path('~/sandbox'))
    p.add_argument('--backfill', action='store_true')
    p.set_defaults(func=cmd_sync)
