import pytest
from agent_memory.verification_evidence import assess,TARGET_FIELDS


def target():
    return dict(zip(TARGET_FIELDS,('repo','head','worktree','build','instance','http://127.0.0.1:1','/','tab1')))


def test_pass_needs_target_and_observed_behavior():
    expected=target()
    assert assess(expected,expected,criterion={'key':'counter','expected':1,'actual':1},evidence_ref='browser:tab1:1')['result']=='pass'
    assert assess(expected,expected,criterion={'key':'counter','expected':1},evidence_ref='browser:tab1:1')['result']=='unknown'
    assert assess(expected,expected,criterion=None,evidence_ref='screenshot:only')['result']=='unknown'


@pytest.mark.parametrize('field',TARGET_FIELDS)
def test_missing_binding_is_never_verified(field):
    observed=target();observed.pop(field)
    result=assess(target(),observed,criterion={'key':'counter','expected':1,'actual':1},evidence_ref='browser')
    assert result['result']=='unknown' and result['binding_status']=='incomplete'


@pytest.mark.parametrize('field',TARGET_FIELDS)
def test_wrong_target_is_never_a_pass(field):
    observed=target();observed[field]='other'
    result=assess(target(),observed,criterion={'key':'counter','expected':1,'actual':1},evidence_ref='browser')
    assert result['result']=='fail' and result['mismatch']==[field]
