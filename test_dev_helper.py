"""Real isolated child processes: deadline cleanup must not touch other work."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).parent/'deploy/run-dev-helper.py'
spec = importlib.util.spec_from_file_location('dev_helper', SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class DevHelperTests(unittest.TestCase):
    def test_preserves_success_and_failure_exit_status_without_shell_expansion(self):
        for code in (0, 7):
            result = subprocess.run([sys.executable, str(SCRIPT), '--timeout', '5', '--', sys.executable, '-c',
                                     'import sys; assert sys.argv[1] == "$(must-remain-literal)"; sys.exit('+str(code)+')',
                                     '$(must-remain-literal)'], capture_output=True)
            self.assertEqual(result.returncode, code, result.stderr)

    @unittest.skipUnless(sys.platform == 'linux', 'PID disappearance check uses /proc')
    def test_deadline_reaps_only_direct_helper_even_if_it_ignores_term(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            with tempfile.TemporaryDirectory() as directory:
                pidfile = Path(directory)/'helper.pid'
                program = 'import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path('+repr(str(pidfile))+').write_text(str(os.getpid())); time.sleep(30)'
                result = subprocess.run([sys.executable, str(SCRIPT), '--timeout', '.4', '--grace', '.1', '--',
                                         sys.executable, '-c', program], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 124, result.stderr)
                self.assertIn(b'direct process', result.stderr)
                self.assertTrue(pidfile.exists())
                self.assertFalse((Path('/proc')/pidfile.read_text()).exists())
                self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate(); unrelated.wait()

    def test_preserves_explicit_role_and_binding_without_defaulting_real_tests_to_helper(self):
        for role in (None, 'helper', 'work'):
            environment = dict(os.environ)
            environment.pop('HAPI_PROCESS_ROLE', None)
            if role is not None:
                environment['HAPI_PROCESS_ROLE'] = role
            environment.update(HAPI_SESSION_ID='fixture-only', CODEX_THREAD_ID='fixture-thread')
            program = 'import os,json; print(json.dumps([os.getenv(k) for k in ("HAPI_PROCESS_ROLE","HAPI_SESSION_ID","CODEX_THREAD_ID")]))'
            result = subprocess.run([sys.executable, str(SCRIPT), '--timeout', '5', '--', sys.executable, '-c', program],
                                    env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [role, 'fixture-only', 'fixture-thread'])

    @unittest.skipUnless(sys.platform == 'linux', 'PID disappearance check uses /proc')
    def test_stopping_launcher_also_reaps_its_direct_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory)/'helper.pid'
            program = 'import os,time; from pathlib import Path; Path('+repr(str(pidfile))+').write_text(str(os.getpid())); time.sleep(30)'
            runner = subprocess.Popen([sys.executable, str(SCRIPT), '--timeout', '10', '--', sys.executable, '-c', program])
            try:
                deadline = time.monotonic()+5
                while not pidfile.exists() and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue(pidfile.exists())
                runner.terminate()
                self.assertEqual(runner.wait(timeout=5), 143)
                self.assertFalse((Path('/proc')/pidfile.read_text()).exists())
            finally:
                if runner.poll() is None:
                    runner.kill();runner.wait()

    def test_rejects_unbounded_deadlines(self):
        for value in ('0', '-1', 'nan', 'inf', '86401'):
            with self.assertRaises(Exception):
                helper.positive_seconds(value)
