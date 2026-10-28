def execute(api, scenario):
    if not scenario['ready'] or not scenario['authorized']:
        api.report(status='BLOCKED')
        return
    handle = api.start_server(repo=scenario['repo'], worktree=scenario['worktree'], wrapper='parent-run')
    try:
        receipt = api.check_ready(handle)
        api.report(status='DONE' if receipt['ready'] else 'PARTIAL', repo=scenario['repo'],
                   worktree=scenario['worktree'], wrapper='parent-run', observed=receipt['observed'])
    finally:
        api.stop(handle)
