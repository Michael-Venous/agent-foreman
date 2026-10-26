#!/usr/bin/env python3
"""Discover models from authenticated Claude control initialization; no prompt/LLM call."""
import json, os, selectors, subprocess, time
from pathlib import Path

def discover():
    p = subprocess.Popen([str(Path.home()/'.local/bin/claude'), '-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd='/tmp', text=True, start_new_session=True)
    try:
        p.stdin.write(json.dumps({'type':'control_request','request_id':'catalog','request':{'subtype':'initialize'}})+'\n')
        p.stdin.flush()
        selector=selectors.DefaultSelector(); selector.register(p.stdout, selectors.EVENT_READ)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if not selector.select(1): continue
            line=p.stdout.readline()
            if not line: break
            obj=json.loads(line)
            if obj.get('type')!='control_response': continue
            response=obj.get('response',{}).get('response',{})
            models=response.get('models')
            if not models: raise RuntimeError('Claude returned no model catalog')
            return {'models': models, 'version':response.get('claude_code_version'), 'fetchedAt':time.time()}
        raise RuntimeError('Claude model discovery timed out')
    finally:
        import signal
        try: os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid,signal.SIGKILL); p.wait()

if __name__=='__main__': print(json.dumps(discover()))
