"""Temporary loopback fixture for real-browser verification pilot. No production
app, account, external network, credentials or database. Stop with Ctrl-C.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import uuid

ROOT=Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.run(['git','-C',str(ROOT),*args],capture_output=True,text=True,check=True).stdout.strip()


revision=git('rev-parse','HEAD')
source_sha=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
BUILD={"repo_id":"agent-memory","revision":revision,"worktree":str(ROOT),
       "build_id":hashlib.sha256((revision+source_sha+str(ROOT)).encode()).hexdigest(),
       "source_sha256":source_sha,"server_instance":str(uuid.uuid4()),"scope":"controlled_browser_fixture"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def do_GET(self):
        if self.path not in ('/','/unbound','/stale','/build.json'):
            self.send_error(404)
            return
        build=dict(BUILD)
        if self.path=='/stale':
            build['build_id']='intentionally-stale-fixture'
        if self.path=='/build.json':
            body=json.dumps(build).encode();kind='application/json'
        else:
            marker='' if self.path=='/unbound' else '<script type="application/json" id="build">'+json.dumps(build)+'</script>'
            body=('<!doctype html><meta charset="utf-8"><title>Agent-Memory Verification Fixture</title>'
                  '<main><h1>Verification Fixture</h1><p>Controlled test target. No production data.</p>'
                  '<button id="increment" onclick="document.getElementById(\'count\').textContent=Number(document.getElementById(\'count\').textContent)+1">Increment</button>'
                  '<output id="count">0</output>'+marker+'</main>').encode();kind='text/html; charset=utf-8'
        self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)


if __name__=='__main__':
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    BUILD['origin']='http://127.0.0.1:'+str(server.server_port)
    print(json.dumps({"fixture":BUILD,"url":BUILD['origin']+'/'}),flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
