"""Early subscription readiness checks, charged to the night's existing budget."""
from pathlib import Path
import tempfile
import uuid
import time

from agent_memory import rsi_subscription as sub
from agent_memory.rsi_subscription_config import auth_for, usage_fields


def roles(cfg):
    found = []
    for name, mode, settings in (
        ('hypothesizer', 'claude-cli', cfg.hypothesizer),
        ('implementer', 'writer', cfg.implementer),
    ):
        if cfg.stages.get(name) == mode and settings.get('auth_mode') == 'subscription':
            found.append(('claude', settings, False))
    if cfg.stages.get('replay_agent') in ('claude', 'codex'):
        profiles = cfg.replay.get('provider_profiles') or {cfg.stages['replay_agent']: {}}
        for provider, profile in profiles.items():
            settings = {**cfg.replay, **profile}
            if settings.get('auth_mode') == 'subscription':
                found.append((provider, settings, True))
            if settings.get('judge_auth_mode') == 'subscription':
                judge = {k.removeprefix('judge_'): v for k,v in settings.items() if k.startswith('judge_')}
                found.append((cfg.stages['judge'], judge, False))
    unique = {}
    for provider, settings, tools in found:
        key = (provider, settings['model'], settings['effort'], settings['auth']['home'], tools)
        unique[key] = (provider, settings, tools)
    return list(unique.values())


def check(cfg, budget):
    selected = roles(cfg)
    receipts = []
    # All metadata checks precede every inference probe.
    for provider, settings, tools in selected:
        auth = auth_for(provider, settings['auth'], tools=tools)
        try:
            receipt = sub.preflight(provider, model=settings['model'], effort=settings['effort'], auth=auth)
        except sub.SubscriptionError as exc:
            raise sub.SubscriptionError(f"{provider} {'agent' if tools else 'judge/writer'} metadata: {exc}", inference_started=False) from None
        receipts.append({'provider': provider, 'tools': tools, **receipt})
    for (provider, settings, tools), receipt in zip(selected, receipts):
        model = settings['model']
        reservation = budget.take_call(model, stage='client-readiness')
        unstarted, usage, ok = True, {}, False
        started = time.monotonic()
        tool_id = None
        try:
            with tempfile.TemporaryDirectory(prefix='rsi-readiness-') as tmp:
                workspace, arm = Path(tmp)/'workspace', Path(tmp)/'arm'
                workspace.mkdir(); arm.mkdir()
                nonce = uuid.uuid4().hex
                prompt = ('Use task_shell exactly once to execute: printf '+nonce+' > /work/readiness.txt. '
                          'Then reply OK.' if tools else 'Reply with exactly OK.')
                auth = auth_for(provider, settings['auth'], tools=tools)
                if tools:
                    rid = 'readiness-' + nonce
                    budget.take_sandbox_run(rid)
                    tool_id = rid
                unstarted = False
                result = sub.run(provider, prompt, model=model, effort=settings['effort'],
                                 workspace=workspace.resolve(), arm_home=arm.resolve(),
                                 auth=auth, tools_allowed=tools,
                                 max_tool_calls=1, timeout_s=min(90, max(1,int(budget.remaining_s()))))
                usage = usage_fields(result.usage)
                if tools and (not (workspace/'readiness.txt').is_file() or
                              (workspace/'readiness.txt').read_text() != nonce):
                    raise sub.SubscriptionError('client_tool_probe_failed', inference_started=True)
                if not tools and result.text.strip() != 'OK':
                    raise sub.SubscriptionError('client_response_probe_failed', inference_started=True)
                receipt.update(status='ready', usage=usage, evidence=result.evidence)
                ok = True
        except sub.SubscriptionError as exc:
            unstarted = exc.inference_started is False
            raise sub.SubscriptionError(f"{provider} {'agent' if tools else 'judge/writer'} probe: {exc}", inference_started=exc.inference_started) from None
        finally:
            if tool_id:
                if unstarted:
                    budget.release_sandbox_runs([tool_id], reason='readiness inference never started')
                else:
                    budget.record_run(tool_id, model='none', duration_s=time.monotonic()-started, exit_code=0 if ok else 1)
            if unstarted:
                budget.cancel_call(reservation)
            else:
                budget.record_call(model, stage='client-readiness', reservation_id=reservation,
                                   duration_s=time.monotonic()-started, ok=ok, **usage)
    return receipts
