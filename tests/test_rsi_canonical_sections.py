"""Parser-bound section contracts; all model responses are local stand-ins."""
import hashlib
import json

import pytest

from agent_memory import edits_sections as ES, rsi_night as N
from test_rsi_intervention_contract import TARGET, hyp, reply, isolated_home

SOURCE = '# Rules\n\n## Summary\n\n- Keep the summary short.\n\n---\n\n## Other\n\n- Leave this alone.\n'


def reference(payloads, **changes):
    edit = dict(section_id='summary', before_sha256=payloads['summary']['before_sha256'],
                after='## Summary\n\n- Write a two-line summary.\n\n---')
    edit.update(changes)
    return json.dumps({'hypotheses': [dict(what='Use a two-line summary', why='Make the output precise',
                                         target=TARGET, edit=edit)]})


def test_parser_bound_separator_contract_reaches_one_writer_and_preserves_neighbour(monkeypatch):
    monkeypatch.setattr(N.ET, 'pinned_sections', lambda *a: False)
    payloads = N.canonical_section_payloads(SOURCE, 10_000)
    canonical = ES.section_text(SOURCE, 'summary')
    assert canonical.endswith('\n\n---')
    assert payloads['summary']['text'] == canonical
    assert payloads['summary']['before_sha256'] == hashlib.sha256(canonical.encode()).hexdigest()
    bound = N.bind_canonical_sections(reference(payloads), payloads, TARGET)
    hyps, rejected = N.parse_hypotheses(bound, allowlist=[TARGET], cap=1)
    assert not rejected and hyps[0].edit['before'] == canonical
    calls = []
    def writer(prompt):
        from agent_memory.rsi_budget import Usage
        calls.append(prompt)
        return reply(hyps[0].edit['after']), Usage()
    from agent_memory.rsi_budget import Budget, BudgetConfig
    result = N.WriterImplementer({}, call=writer).implement(hyps[0], old_text=SOURCE, experiment_id='exp-test',
        baseline_id='bl-test', budget=Budget(BudgetConfig.from_dict({'max_usd': 0,
        'model_calls': {'claude-opus-5-5': 1}, 'subscription_models': ['claude-opus-5-5'], 'max_subscription_tokens': 10000})), context={})
    assert len(calls) == 1 and result.contract_verified
    assert result.text == SOURCE.replace('- Keep the summary short.', '- Write a two-line summary.')


@pytest.mark.parametrize('changes', [{'before_sha256':'0'*64}, {'section_id':'hidden'}, {'before_sha256':None}])
def test_wrong_or_hidden_reference_cannot_bind(changes):
    payloads = N.canonical_section_payloads(SOURCE, 10_000)
    bound = N.bind_canonical_sections(reference(payloads, **changes), payloads, TARGET)
    hyps, rejected = N.parse_hypotheses(bound, allowlist=[TARGET], cap=1)
    assert not hyps and rejected[0]['code'] == 'bad_edit_contract'


def test_budget_publishes_only_complete_source_sections():
    payloads = N.canonical_section_payloads(SOURCE + '\n' + 'x'*20_000, 500)
    assert len(json.dumps(payloads, ensure_ascii=True)) <= 500
    assert 'summary' in payloads and 'other' not in payloads
    assert payloads['summary']['text'] == ES.section_text(SOURCE, 'summary')
    assert N.canonical_section_payloads(SOURCE, 1) == {}


@pytest.mark.parametrize('kind', ['missing_before_separator', 'stale_prose', 'missing_after_separator'])
def test_legacy_malformed_or_stale_contract_never_calls_writer(monkeypatch, kind):
    monkeypatch.setattr(N.ET, 'pinned_sections', lambda *a: False)
    before = ES.section_text(SOURCE, 'summary')
    after = before.replace('Keep the summary short.', 'Write a two-line summary.')
    if kind == 'missing_before_separator':
        before = before[:-5]
    elif kind == 'stale_prose':
        before = before.replace('short', 'brief')
    else:
        after = after[:-5]
    h = hyp(before=before, after=after)
    def forbidden(*a):
        raise AssertionError('unsafe contract reached writer')
    from agent_memory.rsi_budget import Budget, BudgetConfig
    got = N.WriterImplementer({}, call=forbidden).implement(h, old_text=SOURCE, experiment_id='exp-test',
        baseline_id='bl-test', budget=Budget(BudgetConfig.from_dict({})))
    assert got.implementer == 'stub-fallback' and not got.contract_verified
    assert ('stale' if kind == 'stale_prose' else 'separator') in got.fallback_reason


def test_fresh_model_cannot_bypass_payload_using_a_legacy_byte_contract():
    legacy = json.dumps({'hypotheses': [hyp().as_dict()]})
    hyps, rejected = N.parse_hypotheses(N.bind_canonical_sections(legacy, {}, TARGET), allowlist=[TARGET], cap=1)
    assert not hyps and rejected[0]['code'] == 'bad_edit_contract'


def test_unlinked_reason_is_optional_and_preserves_legacy_explanation():
    from types import SimpleNamespace
    legacy = SimpleNamespace(ref='pastdev:1', source='pastdev', status='unlinked', detail='another target')
    current = SimpleNamespace(**vars(legacy), exclusion_reason='unrelated_target')
    result = N.evidence_detail([legacy, current])['unlinked_refs']
    assert result[0] == {'ref': 'pastdev:1', 'source': 'pastdev', 'why': 'another target'}
    assert result[1] == {**result[0], 'exclusion_reason': 'unrelated_target'}


def test_live_adapter_binds_only_complete_displayed_payload_under_prompt_limit(monkeypatch):
    from agent_memory import distill
    text = SOURCE + '\n' + 'large omitted body\n' * 2000
    seen = []
    def fake_cli(argv, prompt, **kwargs):
        seen.append(prompt)
        payloads = json.loads(prompt.split('Canonical sections (JSON, exact section bytes including any trailing separator):\n', 1)[1].split('\n\nEND-EVIDENCE', 1)[0])
        return reference(payloads)
    monkeypatch.setattr(distill, 'run_cli', fake_cli)
    monkeypatch.setattr(distill, 'parse_envelope', lambda text: text)
    from agent_memory.rsi_budget import Budget, BudgetConfig
    adapter = N.ClaudeCliHypothesizer({'model':'opus', 'max_prompt_chars':6000})
    result = adapter.reply({'target':TARGET, 'rule_key':'dev:summary'}, evidence_text='END-EVIDENCE',
        targets=[TARGET], cap=1, target_text=text,
        budget=Budget(BudgetConfig.from_dict({'max_usd':5, 'model_calls':{'opus':1}})))
    assert len(seen) == 1 and len(seen[0]) <= 6000 and seen[0].endswith('END-EVIDENCE\n')
    assert adapter.last_left_out > 0
    hyps, rejected = N.parse_hypotheses(result, allowlist=[TARGET], cap=1)
    assert not rejected and hyps[0].edit['before'] == ES.section_text(text, 'summary')


def test_preferred_section_larger_than_one_third_takes_room_before_file_context(monkeypatch):
    from agent_memory import distill
    text = '# Rules\n\n## Summary\n\n' + '- Keep this requirement.\n'*240 + '\n---\n\n## Other\n\n- Leave this alone.\n'
    before = ES.section_text(text, 'summary')
    assert 5000 < len(before) < 8000
    seen = []
    evidence = 'BEGIN-EVIDENCE nonce\ncontext\nEND-EVIDENCE nonce'
    def fake_cli(argv, prompt, **kwargs):
        seen.append(prompt)
        payloads = json.loads(prompt.split('Canonical sections (JSON, exact section bytes including any trailing separator):\n', 1)[1].split('\n\nBEGIN-EVIDENCE', 1)[0])
        assert set(payloads) == {'summary'} and payloads['summary']['text'] == before
        return reference(payloads)
    monkeypatch.setattr(distill, 'run_cli', fake_cli)
    monkeypatch.setattr(distill, 'parse_envelope', lambda text: text)
    from agent_memory.rsi_budget import Budget, BudgetConfig
    adapter = N.ClaudeCliHypothesizer({'model':'opus', 'max_prompt_chars':12_000})
    result = adapter.reply({'target':TARGET, 'rule_key':'dev:summary'}, evidence_text=evidence,
        targets=[TARGET], cap=1, target_text=text,
        budget=Budget(BudgetConfig.from_dict({'max_usd':5, 'model_calls':{'opus':1}})))
    assert len(seen) == 1 and len(seen[0]) <= 12_000 and evidence in seen[0]
    hyps, rejected = N.parse_hypotheses(result, allowlist=[TARGET], cap=1)
    assert not rejected and hyps[0].edit['before'] == before


def test_preferred_section_that_cannot_fit_defers_without_model_or_unrelated_payload(monkeypatch):
    from agent_memory import distill
    text = '# Rules\n\n## Summary\n\n' + '- Keep this requirement.\n'*240 + '\n## Other\n\n- Leave this alone.\n'
    monkeypatch.setattr(distill, 'run_cli', lambda *a, **kw: pytest.fail('model must not run'))
    assert N.canonical_section_payloads(text, 500, 'dev:summary') == {}
    from agent_memory.rsi_budget import Budget, BudgetConfig
    adapter = N.ClaudeCliHypothesizer({'model':'opus', 'max_prompt_chars':4000})
    with pytest.raises(ValueError, match='candidate section cannot fit.*no model call'):
        adapter.reply({'target':TARGET, 'rule_key':'dev:summary'}, evidence_text='BEGIN-EVIDENCE nonce\nEND-EVIDENCE nonce',
            targets=[TARGET], cap=1, target_text=text, budget=Budget(BudgetConfig.from_dict({})))
    assert adapter.last_left_out == len(text)
