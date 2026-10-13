
### 20:16 — scoped design
Did: read legacy.py, surface.mirror_legacy_report, e2e tests, threat model item 5.
Next: add DATAOPS_HUB_LEGACY_MIRROR_INSTALLATIONS allowlist in hub/surface.py (default empty = mirror nothing, counter not_allowed), tests, docs.
Assumptions: mirror logic lives in hub/surface.py (not in owned list but it is the code legacy mirror calls); env.example has no hub config so docs go to hub-deploy.md only.

### 20:19 — done
Did: allowlist DATAOPS_HUB_LEGACY_MIRROR_INSTALLATIONS in hub/surface.py (fail closed, counter not_allowed), 19 new tests, docs + threat model item 5; pytest 1880 passed/11 skipped, npm test exit 0; live check on PORT 8100 (allowed stored, 2 unlisted not_allowed, all 202). Committed 855c4ff.
Next: none
Status: DONE
Assumptions: deploy/hub compose does not yet pass the new env var (worker-deploy/parent owns it); default empty list means existing deployments stop mirroring until set.
