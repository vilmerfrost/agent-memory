# syntax=docker/dockerfile:1
# rsi egress proxy. Stdlib-only Python CONNECT proxy with a baked-in allowlist.
# Base pinned by digest (python:3.14-alpine, linux/arm64 + multi-arch index, pulled 2026-10-16).
FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72

LABEL rsi.sandbox="1" rsi.role="egress"

RUN adduser -D -H -u 10002 -s /sbin/nologin rsiproxy \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
    && mkdir -p -m 0755 /opt/rsi /etc/rsi

COPY --chmod=0444 egress_proxy.py /opt/rsi/egress_proxy.py
COPY --chmod=0444 allowlist.txt /etc/rsi/allowlist.txt

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10002:10002
EXPOSE 3128
ENTRYPOINT ["python3", "-I", "/opt/rsi/egress_proxy.py"]
