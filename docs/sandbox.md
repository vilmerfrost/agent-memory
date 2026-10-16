# RSI sandbox: isolated workers for config experiments

Mission 003. An agent tests a candidate config change (a skill, a CLAUDE.md edit, a hook)
in a throwaway Docker container, not on Vilmer's machine. The container cannot read
host files, cannot see host secrets, and can reach only the model APIs.

The old pattern (`nightly-trials/runner`: `claude -p` in a temp dir) ran as the same user,
with the same HOME and the same network. That is not isolation. This is.

Code: `sandbox/rsi_sandbox.py` (CLI and library, stdlib only), `sandbox/images/`,
`sandbox/tests/`.

```
macOS host                                   Docker Desktop VM (linux/arm64)
~/sandbox/experiments/<id>/                 ┌── rsi-internal (internal, no gateway) ──┐
  home-A ─── bind rw ──▶ /home/rsi          │  worker ──CONNECT host:443──▶ proxy ─────┼── rsi-egress ──▶ internet
  runs/<run> ─ bind rw ─▶ /work             │  (no route out, no DNS out)   allowlist  │    (bridge)     api.anthropic.com
  logs/  (runner only, never mounted)       └──────────────────────────────────────────┘                 api.openai.com
```

## Layout

`rsi_sandbox.py init` creates `~/sandbox/`, every directory mode 700:

| Path | Holds |
|---|---|
| `baselines/<baseline_id>/` | A snapshot of a home (`.claude/`, `.codex/`, `.agents/`). Only read. |
| `experiments/<id>/home-A`, `home-B` | One sanitized home per arm. Mounted rw at `/home/rsi`. |
| `experiments/<id>/runs/<run_id>/` | One dir per run. Mounted rw at `/work` (the workdir). |
| `experiments/<id>/logs/` | stdout, stderr and meta of each run. Written by the runner, never mounted. |
| `experiments/<id>/sanitize-report.json` | What `prepare-arm` removed, per arm. Not mounted. |
| `artifacts/`, `stack/`, `db/`, `briefs/` | Reserved for later missions. |
| `run/` | Locks (`<exp>.<arm>.lock`) and container records (`containers/`). |

The encrypted transcript archive (`~/Library/Application Support/amem-archive`,
env `AMEM_ARCHIVE_DIR`) stays outside `~/sandbox`. The mount guard refuses it.

## Commands

```
python3 sandbox/rsi_sandbox.py init
python3 sandbox/rsi_sandbox.py build          # both images; writes sandbox/images/lock.json
python3 sandbox/rsi_sandbox.py net-up         # networks + egress proxy (idempotent)
python3 sandbox/rsi_sandbox.py prepare-arm --experiment E --baseline B --arm A [--hook-experiment]
python3 sandbox/rsi_sandbox.py run --experiment E --arm A --timeout 900 \
    --credentials keychain:rsi-sandbox/anthropic -- claude -p "..." --permission-mode dontAsk \
    --permission-prompts none --output-format json --settings '{"disableAllHooks":true}'
python3 sandbox/rsi_sandbox.py cleanup [--max-age-hours 6]
python3 sandbox/rsi_sandbox.py recover
python3 sandbox/rsi_sandbox.py net-down
```

As a library: `rsi_sandbox.Sandbox().run(experiment, arm, [cmd...], timeout_s=..., credentials=...)`
returns a `RunResult` (exit code, timed_out, duration, stdout/stderr tails, the docker argv).

## Images

Both images are pinned. `build` records the local image digests in `sandbox/images/lock.json`,
and `run` / `net-up` start containers by that digest (`--pull never`), not by tag.
Builds use `--provenance=false --sbom=false`, so a cached rebuild gives the same digest.

- **Worker** (`images/worker/Dockerfile`): `node:24-trixie-slim@sha256:7f486cab…`, plus
  `ca-certificates curl git jq python3` from Debian, plus `@anthropic-ai/claude-code@2.1.295`
  and `@openai/codex@0.162.0` (both exist on npm; Claude Code ships its native linux-arm64
  binary as an npm optional dependency). User `rsi`, uid/gid 10001, HOME `/home/rsi`.
  The `node` user is deleted. No sudo. Every setuid/setgid bit is stripped. The base image's
  entrypoint script is dropped (`ENTRYPOINT []`), so the runner's command runs as given.
  Baked env: `DISABLE_AUTOUPDATER`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
  `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING` (all `1`).
- **Egress** (`images/egress/`): `python:3.14-alpine@sha256:f6a589d4…`, `egress_proxy.py`
  (stdlib only, under 400 lines) and `allowlist.txt`, both baked in. User 10002.

Apt package versions are not pinned (Debian drops old versions from the mirror). `lock.json`
records the versions of the named packages and a hash of the full dpkg manifest, so drift
shows up on the next build. Full apt pinning would need snapshot.debian.org.

## What a worker gets

`run` starts one fresh container per command:

- `--rm`, `--read-only` rootfs, `--tmpfs /tmp` (512 MB; Docker adds `noexec`).
- Two writable bind mounts: the arm's home → `/home/rsi`, the run dir → `/work`.
  Optional read-only mounts (`--ro-mount SRC:/mnt/x`) only from the experiment dir or `baselines/`.
- `--cap-drop ALL`, `--security-opt no-new-privileges`, Docker's default seccomp profile,
  `--user 10001:10001`, `--init`.
- `--memory` = `--memory-swap` (no swap), `--cpus`, `--pids-limit`. Defaults: 2g, 2, 512.
- A wall-clock timeout (default 1200 s, max 4 h). On timeout the runner runs `docker kill`.
- `--network rsi-internal` only.
- Env: only `HOME`, `CLAUDE_CONFIG_DIR=/home/rsi/.claude`, `CODEX_HOME=/home/rsi/.codex`,
  `HTTPS_PROXY`/`HTTP_PROXY=http://rsi-egress-proxy:3128`, `NO_PROXY`, `EXPERIMENT_ID`, `RSI_ARM`,
  plus named credentials. Never the host env. The docker CLI itself also gets a minimal env.
- Labels: `rsi.sandbox=1`, `rsi.role=worker`, `rsi.experiment`, `rsi.arm`, `rsi.run`, `rsi.deadline`.
- One lock per experiment arm (`run/<exp>.<arm>.lock`), so two runs never share a home at once.

### Mount guard

`check_mount` resolves the real path first (symlinks), compares case-insensitively (APFS), and
refuses:

- `~/.claude`, `~/.claude.json`, `~/.codex`, `~/.agents`, `~/agent-config`, `~/Projects`,
  `~/Library` (and so the archive dir), `$AMEM_ARCHIVE_DIR`, `~/.ssh`, `~/.config`, `~/.docker`,
  `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.netrc`, `~/.npmrc`, `~/.git-credentials`,
  `/var/run/docker.sock`, `/run/docker.sock`;
- any path above one of those (so `~` and `/` are refused too);
- any path that contains `.env`;
- any writable mount other than the arm's home and its run dir;
- any read-only mount outside `experiments/<id>/` or `baselines/`;
- relative paths, paths with `,` `:` `=` or newlines (no `--mount` option injection),
  and anything that is not an existing directory.

The guard runs before Docker is touched.

## Egress allowlist

The proxy (`images/egress/egress_proxy.py`) is the only way out. Rules, all fail closed:

1. Only `CONNECT host:443`. Plain HTTP, other methods and other ports get 403.
2. The host must be a DNS name exactly on the allowlist. IP literals (also `16843009`, `0x7f.1`,
   `[::1]`) get 403.
3. The proxy resolves the name itself. Every answer must be public (`ipaddress.is_global`, minus
   NAT64/6to4/Teredo). One private, loopback, link-local, CGNAT or Docker-internal answer → 403.
   It then connects to an address it checked, so DNS rebinding has no window.
4. Inside the tunnel, the TLS ClientHello SNI must equal the CONNECT host. This blocks
   SNI-based domain fronting.
5. One log line per decision: `<time> <ALLOW|DENY(reason)|ERROR(reason)> host:port`.
   No headers, no payload, no client address.

Allowed:

| Host | Why |
|---|---|
| `api.anthropic.com` | Claude Code's Messages API (sandbox API key). |
| `api.openai.com` | Codex with an API key (`CODEX_API_KEY`): `/v1/responses`, HTTPS and WebSocket. |

Blocked on purpose (observed in the proxy log on 2026-10-16, both CLIs still work):

| Host | Who asked | Why blocked |
|---|---|---|
| `http-intake.logs.us5.datadoghq.com` | Claude Code telemetry, when the baked `DISABLE_*` flags are unset | Third-party log intake. Not needed. |
| `sentry.io`, `statsig.anthropic.com` | Older Claude Code error reports / flags | Not seen with 2.1.295; not needed. |
| `chatgpt.com` | Codex (models refresh, ChatGPT-account features) | ChatGPT login = personal OAuth, which the sandbox never uses. |
| `github.com`, `api.github.com` | Codex at startup | A huge write surface (gists, repos). Not needed to run. |

With the baked flags, Claude Code 2.1.295 contacts only `api.anthropic.com`.

## prepare-arm (sanitizer)

`prepare-arm` copies `baselines/<B>/` into `experiments/<E>/home-<arm>/` and cleans it.
It never writes to the baseline. It never opens a `.env*` file.

**RSI formats** (docs/rsi/spec.md, docs/rsi/targets.md):
- An `rsi-baseline/1` dir (one with `manifest.json`) is verified first: the id matches the manifest,
  every copy matches its sha256, and no unlisted file exists. The home is built from its `files/`,
  with file modes from the manifest. A dir without a manifest is a raw home snapshot, as before.
- `--artifact <hash>` builds the **candidate arm**. The artifact must verify (canonical manifest, dir
  name = hash, every after file). It must be staged against this baseline and this experiment, and
  each `before_sha256` must equal the baseline copy. The after bytes are added before sanitizing.
  The report records `overlay` (artifact sha vs. home sha per file) and `exact`.
- `record-eval` writes `artifacts/<hash>/evals/<suite>.json` (`rsi-eval/1`, runner `rsi_sandbox`).
  It refuses unless the candidate arm is `exact` and was built from this artifact, and the control arm
  is the same baseline without an artifact. A change the sanitizer alters (for example a
  `settings.json` with hooks, without `--hook-experiment`) therefore cannot get a sandbox evaluation.

- **Not copied:** symlinks (they could point at host files), special files, `.env*`,
  `.claude/.credentials.json`, `.codex/auth.json`, `.codex/history.jsonl`, every `.mcp.json`,
  `.claude/plugins/` (plugins and marketplaces), `.claude/hooks/`, and session state
  (`.claude/projects`, `shell-snapshots`, `todos`, `statsig`, `file-history`, `debug`,
  `session-env`, `ide`; `.codex/sessions`, `archived_sessions`, `log`).
- **Claude settings** (`.claude/settings.json`, `settings.local.json`, nested
  `.claude/settings*.json`): strip `hooks`, `statusLine`, `mcpServers`,
  `enableAllProjectMcpServers`, `enabledMcpjsonServers`, `enabledPlugins`, `plugins`,
  `extraKnownMarketplaces`, `env`, and the command helpers `apiKeyHelper`, `awsAuthRefresh`,
  `awsCredentialExport`, `otelHeadersHelper`, `fileSuggestion`.
- **`.claude.json`** (global state): the same keys plus `oauthAccount`, `primaryApiKey`,
  `customApiKeyResponses`, `projects`.
- **Codex `config.toml`:** strip `[mcp_servers.*]` (also dotted and inline forms), `notify`,
  `[shell_environment_policy]` (its env block) and `hooks`. stdlib has no TOML writer, so this is
  line-based; then `tomllib` re-parses the result and refuses it if any of those keys is left.
- **Then:** `disableAllHooks: true` in `.claude/settings.json`, unless `--hook-experiment`.
  A hook experiment keeps `hooks` and `.claude/hooks/`; everything else is still stripped.
- **Report:** every removal and every setting goes into `experiments/<E>/sanitize-report.json`.
  Invalid JSON or TOML in the baseline stops the copy and removes the half-made home.

Belt and braces: pass `--settings '{"disableAllHooks":true}'` on the `claude` command line too.
Note that `disableAllHooks` also disables hooks in skills and agents frontmatter.

## Credentials

A provider supplies named credentials. Names are limited to `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY` and `CODEX_API_KEY`. There is no OAuth-token name on purpose.

| Spec | Use |
|---|---|
| `none` | Default. No credential. |
| `env-file:<path>` | Tests only. `NAME=value` lines. Refuses `.env*` names and files under forbidden paths. |
| `keychain:<service>/<account>` | Production. Runs `/usr/bin/security find-generic-password -s <service> -a <account> -w`. `--credential-name` picks the env name (default `ANTHROPIC_API_KEY`). Only `rsi-sandbox/anthropic` and `rsi-sandbox/openai` are allowed (`KEYCHAIN_ITEMS`), so a wrong spec can never pull a personal login into a worker. A safeguard against mistakes: a hostile process as the same user can call `security` itself. |

The value goes only into the env of the `docker run` process. The argv has `-e NAME` with no
value, so it never shows in `ps`, logs or `RunResult.argv`. The Docker daemon does see the value
(it is in `docker inspect` while the container lives); that is the same user's daemon.

Production flow:

1. Create a **dedicated sandbox API key** in a separate Anthropic workspace (and an OpenAI
   project for Codex) with a **monthly spend cap**. Never Vilmer's personal OAuth token,
   never `~/.codex/auth.json`.
2. Store it once in the login Keychain. **Do not use the `-w` prompt for long keys:** it keeps only the
   first 128 characters (an OpenAI `sk-proj-` key was cut this way on 2026-10-16 and failed with 401).
   Copy the key, then pipe it in as hex (`-U` replaces an existing item; the key never reaches argv):
   `printf 'add-generic-password -U -s rsi-sandbox -a openai -X %s\n' "$(pbpaste | tr -d '\r\n' | xxd -p | tr -d '\n')" | security -i`,
   then `pbcopy < /dev/null`. Same with `-a anthropic`.
3. The runner fetches it per run with `--credentials keychain:rsi-sandbox/anthropic` (Codex:
   `keychain:rsi-sandbox/openai --credential-name CODEX_API_KEY`). An item created by `security` trusts
   `security`, so the fetch shows no dialog (observed 2026-10-16). Any process as the same user can read it
   the same way: the spend cap is what bounds a leak.
4. Rotate the key when an experiment looks wrong. The spend cap bounds the damage of a leak.

## Cleanup and recovery

- A run removes its container (`--rm`, then `docker rm -f` in `finally`), its lock and its
  container record, even on timeout or Ctrl-C.
- If the runner dies, the container keeps running. Its `rsi.deadline` label says when it is overdue.
- `recover`: kills worker containers past `rsi.deadline`, releases stale locks in `run/`
  (dead pid on this host, or past the lock's own expiry), prunes container records.
- `cleanup --max-age-hours N`: removes worker containers past their deadline, older than N hours,
  or already exited; prunes records; deletes `run/*.tmp` older than an hour.
- Both act only on containers labelled `rsi.sandbox=1` and `rsi.role=worker`. The proxy
  (`rsi.role=egress`) is left alone; `net-down` removes it.

## Threat model

The worker runs code an agent wrote, from a config an agent changed. Assume it is hostile.

**Protected:** host files (dotfiles, repos, the transcript archive, Keychain), host secrets
(env, auth files), the Docker socket, the Mac and its LAN, the Docker VM and other containers'
published ports, cloud metadata, and the internet apart from the two model APIs.
Also protected: the other arm's home (A cannot see B), and the runner's logs.

**Trusted:** the macOS user, Docker Desktop and its VM kernel, the egress proxy, the image
build inputs (Docker Hub base images, Debian, npm).

**Not protected (accepted):** see Known limits.

## Verified vs not verified

Verified by `sandbox/tests/` on 2026-10-16 (Docker Desktop 29.8.1, VM kernel 7.0.14-linuxkit,
linux/arm64). Each probe runs inside a real worker started through `Sandbox.run`. Each "cannot
connect" probe has a positive control (the same probe from a normal container or the host).

- Filesystem: `/Users`, `/Volumes`, `/host`, `/private`, `/mnt` hold nothing; canaries in the
  other arm's home and in an unmounted dir are invisible by name and by content scan, while a
  canary in the mounted home is found; the mount table has exactly two host mounts
  (`/home/rsi`, `/work`); rootfs is `ro` (EROFS everywhere but `/tmp`, `/home/rsi`, `/work`);
  no file in the worker has the sha256 of the real `~/.claude/CLAUDE.md`.
- Secrets: the container's initial env (`/proc/1/environ`) is the allowlist plus the image's own
  `ENV`; host canary vars (`RSI_HOST_CANARY_TOKEN`, `SUPABASE_DB_URL`) do not arrive; the only sensitive-looking names are the dummy keys passed on purpose; no Docker
  socket and no unix socket at all on the worker's filesystems.
- Privileges: uid 10001, `NoNewPrivs: 1`, all capability sets 0, seccomp filter mode, no setuid files.
- Resources: `memory.max`, `memory.swap.max`, `pids.max`, `cpu.max` match the request;
  `sleep 600` with a 6 s timeout is killed and the container is gone (the live container showed
  ReadonlyRootfs, CapDrop ALL, no-new-privileges, `rsi-internal`, the labels, two rw binds);
  `recover` kills an overdue container and releases a stale lock; `cleanup` keeps a young worker
  and removes it once it is older than the age limit.
- Proxy container: user 10002, read-only rootfs, CapDrop ALL, no-new-privileges, only
  `rsi-egress` + `rsi-internal`, no published ports, image digest = `lock.json`.
- Network: no direct TCP/UDP to 1.1.1.1, 8.8.8.8 or Anthropic's IPs; no path to
  host.docker.internal, Docker Desktop's own HTTP proxy (`http.docker.internal:3128`), the VM
  (a listener in the VM netns, via 172.17.0.1 and the rsi-egress gateway), the Mac's LAN IP, or
  169.254.169.254; external DNS fails; `curl --noproxy '*'` fails; through the proxy,
  api.anthropic.com completes a verified TLS handshake and answers 401; example.com, `*.supabase.co`,
  IP literals, ports 80/8443 and plain HTTP get 403; a mismatched SNI is cut; the resolver refuses
  an allowlisted name with a private answer (unit test, and with the real resolver on `localhost`).
- Headless CLIs: `claude -p … --permission-prompts none` with a dummy key ends on its own in about
  1 s with a 401; `codex -a never exec … --ephemeral` with a dummy key ends in about 25 s with a 401.
  Neither waits for input (stdin is closed).

**Finding fixed along the way:** a plain `internal: true` network is not enough on Docker
Desktop. The bridge still gets a gateway IP inside the VM, and anything listening in the VM
answers on it (a test listener on `0.0.0.0` in the VM netns was reachable from the internal
network). `net-up` creates `rsi-internal` with `com.docker.network.bridge.inhibit_ipv4=true`,
so the bridge has no IP and the worker has no L3 path to the VM. A test guards this.

**Verified with real keys (Mission 003.5 Phase H, 2026-10-16):** spend-capped sandbox keys from the real
Keychain, a synthetic home whose `CLAUDE.md` / `AGENTS.md` held a status word, 300 s / 1 GB / 1 CPU /
256 pids per run.

- Claude Code 2.1.295, `claude-haiku-5-5`, `--max-budget-usd 0.25`, tools Read and Write: exit 0 in 2.8 s,
  2 turns, $0.00048; it wrote exactly `sandbox-ok-claude` to `/work/status.txt`.
- Codex 0.162.0, `gpt-6-luna`, `model_reasoning_effort=low`, **`-s danger-full-access`**: exit 0 in 6.6 s,
  about 25k input tokens (half cached) and 73 output; it wrote exactly `sandbox-ok-codex`.
- **Codex's own sandbox does not work in the worker.** With `-s workspace-write` the model ran and read its
  `AGENTS.md`, but every tool write failed ("Failed to write file /work/status.txt"), while Codex itself
  wrote its `-o` file to the same dir. Use `-s danger-full-access` inside the container: the container and
  the proxy are the boundary (everything above still applies).
- Proxy, all runs: ALLOW only `api.anthropic.com` / `api.openai.com`; Codex's `chatgpt.com`,
  `ab.chatgpt.com`, `github.com` and `api.github.com` were denied, and it still worked.

**Not verified:**

- Kernel or Docker Desktop escapes, and side channels. Not tested; see limits.
- IPv6 paths: the networks are IPv4-only and the worker has no IPv6 route; not probed further.

## Known limits

- **Shared kernel.** All containers share the Docker Desktop VM's Linux kernel. A kernel or
  runc/containerd escape reaches the VM, and from there Docker Desktop's file sharing of `/Users`.
  Mitigations: non-root, no caps, no-new-privileges, seccomp, read-only rootfs, pinned images.
  Not a VM-per-run boundary.
- **The proxy is a trust point.** A proxy bug is an egress bug. It is small, stdlib-only,
  non-root, read-only, no caps. It cannot see inside TLS, so:
  - an agent can send data to an attacker's own account on an allowed API (its own Anthropic
    or OpenAI key, in the request body). Only a TLS-terminating, key-injecting proxy would stop
    that (the worker would never hold a key). Future work.
  - Host-header domain fronting inside TLS is not detectable here (SNI fronting is blocked).
- **Workers on `rsi-internal` can reach each other.** Concurrent arms could talk if one listens.
  Fix later with one internal network per run.
- **No disk quota on bind mounts.** A worker can fill `~/sandbox` (the host disk). `/tmp` is capped.
- **Docker daemon sees credentials** (`docker inspect`) while a container runs.
- **Linux, not macOS.** The worker runs linux-arm64 builds of the CLIs. macOS-only behaviour
  (Keychain auth, Seatbelt sandbox, `open`, mac paths in configs, Apple tools) is not reproduced.
  Results that depend on it will differ from Vilmer's machine.
- **Image inputs float a little.** Base images and npm versions are pinned; apt packages are not.
- **TOCTOU on host paths.** The guard checks a path, then Docker mounts it. Only host processes
  could swap it in between; the worker cannot.

## Tier M (macOS-only, not built)

For experiments that need real macOS behaviour, a "Tier M" would run the CLI on the Mac itself:

- A separate macOS user account (own HOME, own Keychain), not Vilmer's, started with
  `sudo -u rsi-sandbox` or a launchd job. File isolation then comes from Unix permissions, not
  from mounts. Vilmer's home must be `chmod 700` (it is not by default on macOS: `~` is 755).
- `sandbox-exec` (Seatbelt) profiles to deny file and network access. Deprecated by Apple but
  still what Claude Code's own macOS sandbox uses.
- Network: a pf anchor or the same egress proxy on loopback, with the user's traffic forced
  through it (pf `user` rules). Weaker than an internal Docker network.
- Or a full macOS VM (Virtualization.framework, e.g. Tart): strong isolation, slow, ~40 GB disk.

Tier M has more fidelity and less isolation. The Docker tier ("Tier L") is the default.

## Reproduce the verification

```
cd <repo>
python3 sandbox/rsi_sandbox.py init
python3 sandbox/rsi_sandbox.py build
python3 sandbox/rsi_sandbox.py net-up
python3 -m pytest sandbox/tests -s -rA -p no:cacheprovider     # EVIDENCE lines per test
python3 sandbox/rsi_sandbox.py net-down                        # optional
```

The suite creates and removes its own `t-iso-*` experiment and `t-base-*` baseline, plus short-lived
test containers labelled `rsi.sandbox=1` (one on `--network host` as the VM-listener control).
Without Docker or `lock.json`, the Docker tests skip and the unit tests still run.
