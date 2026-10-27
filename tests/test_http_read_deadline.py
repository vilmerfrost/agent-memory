from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
import time

import pytest

from agent_memory.integrations import http, langfuse as LF


@contextmanager
def drip_server(status=200, chunked=False, headers_drip=False):
    finished=Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            try:
                if headers_drip:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Test: ');self.wfile.flush()
                    for _ in range(40):
                        self.wfile.write(b'a');self.wfile.flush();time.sleep(.02)
                    self.wfile.write(b'\r\nContent-Length: 2\r\n\r\n{}');self.wfile.flush()
                    return
                self.send_response(status)
                self.send_header('Transfer-Encoding','chunked') if chunked else self.send_header('Content-Length','80')
                self.end_headers()
                for _ in range(40):
                    self.wfile.write(b'1\r\na\r\n' if chunked else b'{}');self.wfile.flush();time.sleep(.02)
                if chunked:self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
            finally:finished.set()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    thread=Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}',finished
    finally:server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('status,chunked,headers_drip',[(200,False,False),(503,False,False),(200,True,False),(200,False,True)])
def test_deadline_aborts_dripping_success_error_chunked_and_headers(status,chunked,headers_drip):
    with drip_server(status,chunked,headers_drip) as (url,finished):
        started=time.monotonic()
        with pytest.raises(http.HttpError):
            http.request('GET',url,timeout=1,retries=2,total_timeout=.1)
        elapsed=time.monotonic()-started
        assert .07<=elapsed<.45  # peer streams for .8s; no retry or continuing transport read
        assert finished.wait(.4)


def test_langfuse_budget_reaches_transport_body_not_only_next_page():
    with drip_server() as (url,finished):
        client=LF.LangfuseClient(url,'fixture','fixture',min_interval=0,retries=2)
        started=time.monotonic()
        with client.read_budget(.1),pytest.raises(LF.ReadDeadline):
            client._call('GET','/api/public/projects')
        assert time.monotonic()-started<.45 and finished.wait(.4)


def test_expired_budget_starts_no_request():
    with drip_server() as (url,finished):
        with pytest.raises(http.HttpError,match='deadline'):
            http.request('GET',url,total_timeout=0)
        assert not finished.is_set()


def test_non_budgeted_truncated_post_is_not_newly_retried(monkeypatch):
    from http.client import IncompleteRead
    calls=[]
    class Response:
        status=200;headers={}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):calls.append(1);raise IncompleteRead(b'fixture')
    monkeypatch.setattr(http.urllib.request,'urlopen',lambda *args,**kwargs:Response())
    with pytest.raises(IncompleteRead):
        http.request('POST','http://127.0.0.1/recall',body={},retries=3,sleep=lambda _:None)
    assert len(calls)==1


def test_https_proxy_connect_headers_share_the_deadline(monkeypatch):
    finished=Event();arrived=Event()
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_CONNECT(self):
            arrived.set()
            try:
                self.wfile.write(b'HTTP/1.1 200 Connection established\r\nX-Test: ');self.wfile.flush()
                for _ in range(40):
                    self.wfile.write(b'a');self.wfile.flush();time.sleep(.02)
                self.wfile.write(b'\r\n\r\n');self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError):pass
            finally:finished.set()
    server=ThreadingHTTPServer(('127.0.0.1',0),Proxy);server.daemon_threads=True
    thread=Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
    monkeypatch.setattr(http.urllib.request,'getproxies',lambda:{'https':f'http://127.0.0.1:{server.server_port}'})
    monkeypatch.setattr(http.urllib.request,'proxy_bypass',lambda _:False)
    try:
        start=time.monotonic()
        with pytest.raises(http.HttpError):
            http.request('GET','https://target.invalid/',timeout=1,total_timeout=.15,retries=0)
        assert time.monotonic()-start<.5 and arrived.is_set() and finished.wait(.4)
    finally:server.shutdown();server.server_close();thread.join()


def test_tls_handshake_socket_is_registered_before_handshake():
    import socketserver
    finished=Event()
    class SilentTLS(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                self.request.settimeout(2)
                while self.request.recv(4096):pass
            finally:finished.set()
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),SilentTLS);server.daemon_threads=True
    thread=Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
    try:
        start=time.monotonic()
        with pytest.raises(http.HttpError):
            http.request('GET',f'https://127.0.0.1:{server.server_address[1]}/',timeout=1,total_timeout=.15,retries=0)
        assert time.monotonic()-start<.5 and finished.wait(.4)
    finally:server.shutdown();server.server_close();thread.join()
