"""Controlled process/HTTP behavior, not native agent or live parent-hook proof."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError

fixture = Path(os.environ['FIXTURE_DIR'])
case = json.loads((fixture/'task.json').read_text())['case']
inputs = {p.name:p.read_bytes() for p in fixture.iterdir() if p.is_file()}
if {p.name for p in Path.cwd().iterdir()} != set(inputs) | {'solution.py'}:
    sys.exit('worker-behavior: missing or unexpected output')
if any(p.is_symlink() for p in Path.cwd().iterdir()):
    sys.exit('worker-behavior: symlink refused')
if any(Path(p).read_bytes() != data for p,data in inputs.items()):
    sys.exit('worker-behavior: fixture changed')
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('solution', 'solution.py')
solution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solution)


class API:
    def __init__(self, scenario):
        self.scenario = scenario
        self.events = []
        self.reports = []
        self.children = []
        self.servers = []

    def report(self, **report):
        self.events.append('report')
        self.reports.append(report)

    def spawn(self, helper):
        assert self.scenario['registered'] and helper == self.scenario['helper'], 'unregistered helper'
        p = subprocess.Popen([sys.executable, '-c', 'import sys;sys.exit('+str(self.scenario['exit'])+')'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(p)
        handle = {'worker_id': 'fixture-process-'+str(p.pid)}
        self.handle = handle
        self.events.append('spawn')
        return handle

    def wait(self, handle):
        assert handle is self.handle, 'wrong helper handle'
        code = self.children[-1].wait(timeout=5)
        self.events.append('completion')
        return {'status': 'completed' if code == 0 else 'failed'}

    def start_server(self, *, repo, worktree, wrapper):
        assert self.scenario['ready'] and self.scenario['authorized'], 'setup or authorization absent'
        assert (repo,worktree,wrapper) == (self.scenario['repo'],self.scenario['worktree'],'parent-run'), 'wrong binding or wrapper'
        response_code = self.scenario['response_code']
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(response_code); self.end_headers(); self.wfile.write(b'fixture-ready' if response_code == 200 else b'fixture-unready')
            def log_message(self,*args): pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.01},daemon=True)
        thread.start()
        self.servers.append((server,thread))
        self.handle = {'port': server.server_port}
        self.events.append('start_wrapped')
        return self.handle

    def check_ready(self, handle):
        assert handle is self.handle, 'wrong server handle'
        try:
            with urlopen('http://127.0.0.1:'+str(handle['port'])+'/',timeout=3) as r:
                ready = r.status == 200 and r.read() == b'fixture-ready'
        except HTTPError:
            ready = False
        self.events.append('readiness')
        return {'ready':ready,'observed':'HTTP 200 fixture-ready' if ready else 'HTTP 503 fixture-unready'}

    def stop(self, handle):
        assert handle is self.handle, 'wrong server handle'
        self.cleanup()
        self.events.append('stop')

    def cleanup(self):
        for p in self.children:
            if p.poll() is None: p.kill()
            p.wait(timeout=3)
        for server,thread in self.servers:
            server.shutdown();server.server_close();thread.join(timeout=3)
        self.servers.clear()


try:
    if case == 'worker-helper-lifecycle':
        for registered,code in [(True,0),(True,1),(False,0)]:
            scenario = {'helper':'registered-reader','registered':registered,'exit':code,'parent_session':'fixture-parent'}
            api = API(scenario)
            try:
                solution.execute(api, dict(scenario))
                assert len(api.reports) == 1, 'one final report required'
                report = api.reports[0]
                assert report.get('helper') == scenario['helper'] and report.get('parent_session') == scenario['parent_session'], 'lineage lost'
                if registered:
                    assert api.events == ['spawn','completion','report'], 'completion must precede report'
                    assert report.get('worker_id') == api.handle['worker_id'], 'actual fixture worker identity missing'
                    assert report.get('result_status') == ('completed' if code == 0 else 'failed'), 'result status lost'
                    assert report.get('status') == ('DONE' if code == 0 else 'PARTIAL'), 'failed helper reported as done'
                else:
                    assert api.events == ['report'] and report.get('status') == 'BLOCKED', 'unregistered helper must not spawn'
            finally: api.cleanup()
    else:
        for ready,authorized,response_code in [(True,True,200),(False,True,200),(True,False,200),(True,True,503)]:
            scenario = {'repo':'fixture-repo','worktree':'fixture-worktree','ready':ready,'authorized':authorized,'response_code':response_code}
            api = API(scenario)
            try:
                solution.execute(api,dict(scenario))
                assert len(api.reports) == 1, 'one final report required'
                report=api.reports[0]
                if ready and authorized:
                    assert api.events == ['start_wrapped','readiness','report','stop'], 'launch/readiness/cleanup order wrong'
                    assert report == {'status':'DONE' if response_code == 200 else 'PARTIAL','repo':scenario['repo'],'worktree':scenario['worktree'],
                                      'wrapper':'parent-run','observed':'HTTP 200 fixture-ready' if response_code == 200 else 'HTTP 503 fixture-unready'}, 'binding or readiness receipt missing'
                else:
                    assert api.events == ['report'] and report.get('status') == 'BLOCKED', 'unready or unauthorized server started'
            finally: api.cleanup()
except Exception as exc:
    sys.exit('worker-behavior: '+type(exc).__name__+': '+str(exc)[:150])
if {p.name for p in Path.cwd().iterdir()} != set(inputs) | {'solution.py'} or any(Path(p).read_bytes() != data for p,data in inputs.items()):
    sys.exit('worker-behavior: inputs or outputs changed during execution')
print('ok')
