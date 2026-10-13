### 15:00 — Recon and design
Did: read lessons, compose, hubctl, stack-test, tests, hub-deploy doc; pulled tecnativa/docker-socket-proxy v0.4.1, willfarrell/autoheal 1.2.0, haproxy 3.2.4-alpine and read their configs/scripts.
Findings: tecnativa cannot be limited to restart: POST=1 is global, CONTAINERS=1 (needed for the list) then allows /containers/create and /containers/<id>/exec; ALLOW_RESTARTS also allows stop+kill. autoheal 1.2.0 speaks TCP only as https with client certs, so it needs a unix socket.
Next: own haproxy config (official haproxy image, digest-pinned) on a unix socket in a volume shared only with autoheal; both network_mode none; profile `autoheal`, HUB_AUTOHEAL in hub.env; test-only gunicorn hook to stop the outbox thread.
Assumptions: unix-socket volume is a stricter equivalent of "internal network shared only with autoheal".

### 22:23 — Implemented, first live run
Did: compose (haproxy proxy + autoheal, profile autoheal, hub label), autoheal-proxy.cfg, hubctl HUB_AUTOHEAL, hub.env.example, stack-test `autoheal` cmd + stack_autoheal.py + test-only gunicorn hook, unit tests, hub-deploy.md, threat model (asset, Boundary 6, HT23, open item 16). Live: 32 disallowed proxy calls -> 403; hub /healthz 503 -> unhealthy -> autoheal restart -> healthy/200. Only the docker-events check failed (Docker 29 has no .Status) -> fixed, re-running.
Next: rerun autoheal check, smoke, down; full pytest + npm test; commit.
Assumptions: none new.

### 22:32 — Done
Did: stack-test autoheal RESULT: PASS (events: health_status unhealthy, kill, stop, die, start, restart, healthy); smoke RESULT: PASS; clean (down -v, state removed); pytest 2271 passed 12 skipped; npm test green; committed ba05824.
Next: none
Status: DONE
