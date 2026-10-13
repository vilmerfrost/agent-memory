### 20:31 — Recon done, design chosen
Did: read lessons, worker command form, hub compose/nginx/oauth2-proxy/hubctl, operator_auth, surface routes, admin_cli, webhook fixtures. Checked Dex v2.44.0 source: staticPasswords and mock connectors emit no per-user groups.
Next: write deploy/hub/test/ (compose override, Dex LDAP config, glauth config, stack-test.sh, stack_smoke.py), bring stack up on 127.0.0.1:8100/8101/8102.
Assumptions: fake IdP = Dex v2.44.0 (LDAP connector) + glauth v2.5.4 (users + groups), both digest-pinned; issuer http://dex:8102/dex, host clients map dex:8102 -> 127.0.0.1; separate compose project dataops-hub-stacktest, state dir var/hub/stack-test (gitignored), hub image tag dataops-hub:stacktest.

### 20:37 — Stack up, smoke passes
Did: added deploy/hub/test/{docker-compose.idp-test.yml,dex-idp-test.yaml,glauth-test.cfg,stack-test.sh,stack_smoke.py} and tests/dataops/test_hub_stack_live.py. Fixed two test-wiring issues: oauth2-proxy needs scope "groups" for Dex (test override env), glauth has no uid/cn attrs (match cn->memberUid, name by ou). Smoke: all 6 DoD checks PASS.
Next: docs how-to in docs/hub-deploy.md, live pytest with env var, full pytest + npm test, clean rerun from scratch, down -v, commit.
Assumptions: prod files unchanged; scope gap is IdP-specific so documented, not changed in prod.

### 20:45 — Suites green, clean rerun in progress
Did: docs section "Local stack test" in docs/hub-deploy.md (also dropped stale "pinned by tag" note). pytest 1890 passed / 12 skipped; npm test passed. clean -> up from scratch running.
Next: smoke + live pytest on fresh stack, curl evidence, down -v, commit, report.
Assumptions: none new.

### 20:52 — Done
Did: fresh clean->up->smoke all PASS, live pytest 9 passed, curl checks 302/401, stack-test.sh down (down -v --remove-orphans) done. Committed ddf7d74 on worktree-agent-aba499b9f1e378f29.
Next: none
Status: DONE
