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
        with client.read_budget(.1),pytest.raises(http.HttpError):
            client._call('GET','/api/public/projects')
        assert time.monotonic()-started<.45 and finished.wait(.4)


def test_expired_budget_starts_no_request():
    with drip_server() as (url,finished):
        with pytest.raises(http.HttpError,match='deadline'):
            http.request('GET',url,total_timeout=0)
        assert not finished.is_set()
