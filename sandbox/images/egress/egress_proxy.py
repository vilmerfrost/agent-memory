#!/usr/bin/env python3
"""rsi egress proxy: a small HTTPS CONNECT proxy with a hostname allowlist. Stdlib only.

Workers have no route to the internet. This proxy is their only way out.

Rules. Every rule fails closed.
  1. Only `CONNECT host:443`. Every other method (plain HTTP) gets 403.
  2. host must be a DNS name, exactly on the allowlist. IP literals get 403.
  3. The proxy resolves host itself. Every address must be public (`is_global`).
     It then connects to an address it checked. It never resolves again,
     so DNS rebinding has no window.
  4. The first client bytes in the tunnel must be a TLS ClientHello whose
     SNI equals host. This blocks SNI-based domain fronting.
  5. One log line per decision: time, verdict, host:port. Never headers or payload.
"""
from __future__ import annotations

import ipaddress
import os
import re
import select
import signal
import socket
import socketserver
import sys
import threading
import time

LISTEN_HOST = os.environ.get("RSI_PROXY_LISTEN", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("RSI_PROXY_PORT", "3128"))
ALLOWLIST_PATH = os.environ.get("RSI_ALLOWLIST", "/etc/rsi/allowlist.txt")

ALLOWED_PORTS = frozenset({443})
HEAD_MAX = 8192            # bytes of request head we read before giving up
HEAD_TIMEOUT = 10          # seconds to send the request head / ClientHello
CONNECT_TIMEOUT = 10       # seconds for the upstream TCP connect
IDLE_TIMEOUT = 900         # seconds without traffic before a tunnel closes
MAX_CLIENTS = 64
TLS_RECORD_MAX = 16384 + 2048

# A DNS name: lower-case labels of a-z, 0-9 and '-', 1-63 chars, 253 chars total.
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
HOST_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")
# Names that getaddrinfo would read as an IPv4 address (1.2.3.4, 16843009, 0x7f.1).
NUMERIC_RE = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+)){0,3}$")

# Prefixes that embed an IPv4 address or are not plain unicast, denied even
# when `is_global` says yes (NAT64 64:ff9b::/96 is "global").
EXTRA_DENY_NETS = tuple(ipaddress.ip_network(n) for n in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32",
))


class Deny(Exception):
    """A refused request. str(e) is the short reason code that goes in the log."""


# ---------------------------------------------------------------- pure checks

def load_allowlist(path: str) -> frozenset[str]:
    names = set()
    with open(path, encoding="ascii") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip().lower().rstrip(".")
            if not line:
                continue
            if not HOST_RE.match(line) or NUMERIC_RE.match(line):
                raise ValueError(f"bad allowlist entry: {line!r}")
            names.add(line)
    if not names:
        raise ValueError("allowlist is empty")
    return frozenset(names)


def parse_target(method: str, target: str) -> tuple[str, int]:
    """Check the request line. Returns (host, port) or raises Deny."""
    if method != "CONNECT":
        raise Deny("method")
    host, sep, port_s = target.rpartition(":")
    if not sep or not port_s.isdigit() or not host:
        raise Deny("bad_target")
    port = int(port_s)
    host = host.lower().rstrip(".")
    if host.startswith("[") or ":" in host:
        raise Deny("ip_literal")
    try:
        ipaddress.ip_address(host)
        raise Deny("ip_literal")
    except ValueError:
        pass
    if NUMERIC_RE.match(host):
        raise Deny("ip_literal")
    if not HOST_RE.match(host):
        raise Deny("bad_host")
    if port not in ALLOWED_PORTS:
        raise Deny("port")
    return host, port


def check_allowed(host: str, allowlist: frozenset[str]) -> None:
    if host not in allowlist:
        raise Deny("not_allowlisted")


def is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in EXTRA_DENY_NETS):
            return False
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_unspecified
                                       or ip.is_reserved or ip.is_loopback
                                       or ip.is_link_local or ip.is_private)


def system_resolver(host: str, port: int) -> list[tuple[int, tuple]]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return [(fam, sockaddr) for fam, _t, _p, _c, sockaddr in infos]


def resolve_public(host: str, port: int, resolver=system_resolver) -> list[tuple[int, tuple]]:
    """Resolve host. Every address must be public, else Deny. IPv4 first."""
    try:
        addrs = resolver(host, port)
    except (socket.gaierror, UnicodeError, OSError):
        raise Deny("dns_fail") from None
    if not addrs:
        raise Deny("dns_fail")
    for _fam, sockaddr in addrs:
        if not is_public(sockaddr[0]):
            raise Deny("private_address")
    return sorted(addrs, key=lambda a: a[0] != socket.AF_INET)


def parse_sni(data: bytes) -> str | None:
    """Return the SNI host name of one TLS ClientHello record, or None.

    Raises ValueError when data is not one complete ClientHello record.
    """
    if len(data) < 5 or data[0] != 0x16:
        raise ValueError("not_tls")
    rec_len = int.from_bytes(data[3:5], "big")
    body = data[5:5 + rec_len]
    if len(body) != rec_len or len(body) < 4 or body[0] != 0x01:
        raise ValueError("not_client_hello")
    hs_len = int.from_bytes(body[1:4], "big")
    if hs_len + 4 > len(body):
        raise ValueError("fragmented")
    p, end = 4 + 2 + 32, 4 + hs_len          # skip version and random
    p += 1 + body[p]                          # session id
    p += 2 + int.from_bytes(body[p:p + 2], "big")   # cipher suites
    p += 1 + body[p]                          # compression methods
    if p >= end:
        return None
    ext_end = p + 2 + int.from_bytes(body[p:p + 2], "big")
    p += 2
    if ext_end > end:
        raise ValueError("bad_extensions")
    while p + 4 <= ext_end:
        etype = int.from_bytes(body[p:p + 2], "big")
        elen = int.from_bytes(body[p + 2:p + 4], "big")
        p += 4
        if etype == 0:                        # server_name
            ext = body[p:p + elen]
            q, list_end = 2, 2 + int.from_bytes(ext[0:2], "big")
            while q + 3 <= list_end:
                ntype, nlen = ext[q], int.from_bytes(ext[q + 1:q + 3], "big")
                q += 3
                if ntype == 0:
                    return ext[q:q + nlen].decode("ascii").lower().rstrip(".")
                q += nlen
            return None
        p += elen
    return None


# ---------------------------------------------------------------- logging

_log_lock = threading.Lock()


def safe_target(host: str | None, port: int | str | None) -> str:
    """host:port for the log. Only a valid name or a normalized IP, never raw client bytes."""
    h = "<none>"
    if host:
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
            h = f"[{ip}]" if ip.version == 6 else str(ip)
        except ValueError:
            h = host if HOST_RE.match(host) else "<invalid>"
    p = str(port) if str(port).isdigit() and len(str(port)) <= 5 else "?"
    return f"{h}:{p}"


def log_decision(verdict: str, target: str, out=None) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _log_lock:
        print(f"{ts} {verdict} {target}", file=out or sys.stdout, flush=True)


# ---------------------------------------------------------------- server

def _read_head(conn: socket.socket) -> tuple[bytes, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf and b"\n\n" not in buf:
        if len(buf) > HEAD_MAX:
            raise Deny("head_too_large")
        chunk = conn.recv(4096)
        if not chunk:
            raise Deny("closed")
        buf += chunk
    sep = b"\r\n\r\n" if b"\r\n\r\n" in buf else b"\n\n"
    head, _, rest = buf.partition(sep)
    return head, rest


def _read_client_hello(conn: socket.socket, buf: bytes) -> bytes:
    while len(buf) < 5:
        chunk = conn.recv(4096)
        if not chunk:
            raise Deny("closed")
        buf += chunk
    need = 5 + int.from_bytes(buf[3:5], "big")
    if buf[0] != 0x16 or need > 5 + TLS_RECORD_MAX:
        raise Deny("not_tls")
    while len(buf) < need:
        chunk = conn.recv(8192)
        if not chunk:
            raise Deny("closed")
        buf += chunk
    return buf


def _target_from_head(method: str, target: str, head: bytes) -> tuple[str | None, str | None]:
    """Best effort host and port for the log line of a refused request."""
    if method == "CONNECT":
        h, _, p = target.rpartition(":")
        return h.lower().strip("[]"), p
    m = re.match(r"^[a-z]+://([^/:?#@]+)(?::(\d+))?", target, re.I)
    if m:
        return m.group(1).lower(), m.group(2) or "80"
    m = re.search(rb"\r?\nhost:\s*([^\r\n:]+)(?::(\d+))?", head, re.I)
    if m:
        return m.group(1).decode("latin-1").lower(), (m.group(2) or b"80").decode()
    return None, None


def _send(conn: socket.socket, status: str) -> None:
    try:
        conn.sendall(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    except OSError:
        pass


def relay(a: socket.socket, b: socket.socket, idle: float = IDLE_TIMEOUT) -> None:
    socks = [a, b]
    while True:
        readable, _, broken = select.select(socks, [], socks, idle)
        if broken or not readable:
            return
        for s in readable:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                return
            try:
                (b if s is a else a).sendall(data)
            except OSError:
                return


class ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, allowlist: frozenset[str], resolver=system_resolver, log_out=None):
        self.allowlist = allowlist
        self.resolver = resolver
        self.log_out = log_out
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(addr, ProxyHandler)


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        srv: ProxyServer = self.server  # type: ignore[assignment]
        conn: socket.socket = self.request
        if not srv.slots.acquire(blocking=False):
            _send(conn, "503 Service Unavailable")
            log_decision("DENY(busy)", "<none>:?", srv.log_out)
            return
        try:
            self._handle(srv, conn)
        finally:
            srv.slots.release()

    def _handle(self, srv: ProxyServer, conn: socket.socket) -> None:
        conn.settimeout(HEAD_TIMEOUT)
        method, target, head = "", "", b""
        host = port = None
        try:
            head, rest = _read_head(conn)
            line = head.split(b"\n", 1)[0].decode("latin-1").strip()
            parts = line.split(" ")
            if len(parts) != 3 or not parts[2].startswith("HTTP/"):
                raise Deny("bad_request")
            method, target = parts[0].upper(), parts[1]
            host, port = parse_target(method, target)
            check_allowed(host, srv.allowlist)
            addrs = resolve_public(host, port, srv.resolver)
        except Deny as e:
            h, p = (host, port) if host else _target_from_head(method, target, head)
            _send(conn, "403 Forbidden" if str(e) != "closed" else "400 Bad Request")
            log_decision(f"DENY({e})", safe_target(h, p), srv.log_out)
            return
        except (OSError, UnicodeError):
            log_decision("DENY(read_error)", "<none>:?", srv.log_out)
            return

        upstream = None
        for fam, sockaddr in addrs:
            s = socket.socket(fam, socket.SOCK_STREAM)
            s.settimeout(CONNECT_TIMEOUT)
            try:
                s.connect(sockaddr)
                upstream = s
                break
            except OSError:
                s.close()
        if upstream is None:
            _send(conn, "502 Bad Gateway")
            log_decision("ERROR(upstream_unreachable)", safe_target(host, port), srv.log_out)
            return

        try:
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            hello = _read_client_hello(conn, rest)
            try:
                sni = parse_sni(hello)
            except ValueError:
                raise Deny("not_tls") from None
            if sni is None:
                raise Deny("no_sni")
            if sni != host:
                raise Deny("sni_mismatch")
            upstream.sendall(hello)
        except Deny as e:
            log_decision(f"DENY({e})", safe_target(host, port), srv.log_out)
            upstream.close()
            return
        except OSError:
            log_decision("DENY(read_error)", safe_target(host, port), srv.log_out)
            upstream.close()
            return

        log_decision("ALLOW", safe_target(host, port), srv.log_out)
        conn.settimeout(None)
        upstream.settimeout(None)
        try:
            relay(conn, upstream)
        finally:
            upstream.close()


def main() -> int:
    allowlist = load_allowlist(ALLOWLIST_PATH)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with ProxyServer((LISTEN_HOST, LISTEN_PORT), allowlist) as srv:
        print(f"START port={LISTEN_PORT} allowlist={','.join(sorted(allowlist))}", flush=True)
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
