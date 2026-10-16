# syntax=docker/dockerfile:1
# rsi worker: runs one agent command for one experiment arm. Non-root, no sudo, no setuid.
# Base pinned by digest (node:24-trixie-slim, multi-arch index, pulled 2026-10-16).
FROM node:24-trixie-slim@sha256:7f486cabe892bff8fef04c0f7da8dba84a560be3e4e2740b7a86afc57ebec315

ARG CLAUDE_CODE_VERSION=2.1.295
ARG CODEX_VERSION=0.162.0

LABEL rsi.sandbox="1" rsi.role="worker"

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq python3 \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit --no-update-notifier \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && rm -rf /root/.npm /tmp/*

# One fixed non-root user. Remove the image's default `node` user so uid 1000 is unused.
# Strip every setuid/setgid bit: with no-new-privileges they do nothing anyway.
RUN userdel -r node 2>/dev/null || true \
    && groupadd -g 10001 rsi \
    && useradd -u 10001 -g 10001 -m -d /home/rsi -s /bin/bash rsi \
    && mkdir -p /work && chown 10001:10001 /work \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
    && ! command -v sudo

# Non-secret behaviour flags, baked in so every run gets the same CLI behaviour.
# They stop update checks, telemetry and error reports, which the proxy blocks anyway.
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false

# No base-image entrypoint script: the runner's command runs as given.
ENTRYPOINT []
USER 10001:10001
WORKDIR /work
CMD ["bash"]
