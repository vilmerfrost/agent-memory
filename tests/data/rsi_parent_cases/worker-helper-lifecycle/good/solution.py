def execute(api, scenario):
    if not scenario['registered']:
        api.report(status='BLOCKED', helper=scenario['helper'], parent_session=scenario['parent_session'])
        return
    handle = api.spawn(scenario['helper'])
    result = api.wait(handle)
    api.report(status='DONE' if result['status'] == 'completed' else 'PARTIAL',
               helper=scenario['helper'], parent_session=scenario['parent_session'],
               worker_id=handle['worker_id'], result_status=result['status'])
