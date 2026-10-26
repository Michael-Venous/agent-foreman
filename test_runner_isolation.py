import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('runner_tree', Path(__file__).parent / 'deploy/isolate-runner-tree.py')
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class RunnerTreeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.source = '/user.slice/app.slice/hapi-runner.service'
        self.root = {'pid': 200, 'ppid': 100, 'start': '42', 'cgroup': self.source, 'uid': scope.os.getuid()}

    def test_rejects_reused_root_before_any_mutation(self):
        call = Mock()
        with patch.object(scope, 'identity', return_value=self.root):
            with self.assertRaisesRegex(ValueError, 'generation'):
                scope.migrate(200, 'old', self.source, call)
        call.assert_not_called()

    def test_rejects_runner_itself_and_other_parents(self):
        call = Mock()
        with patch.object(scope, 'identity', return_value=self.root), patch.object(scope.subprocess, 'check_output', return_value='200'):
            with self.assertRaisesRegex(ValueError, 'direct runner child'):
                scope.migrate(200, '42', self.source, call)
        call.assert_not_called()

    def test_moves_only_exact_tree_members_still_in_source_and_joins_scope_job(self):
        child = dict(self.root, pid=201, ppid=200, start='43')
        other_unit = dict(self.root, pid=202, ppid=200, start='44', cgroup='/user.slice/browser.service')
        moved = False
        calls = []
        def identity(pid):
            row = {200: self.root, 201: child, 202: other_unit}[pid]
            return dict(row, cgroup='/user.slice/hapi-session-migrated-200-42.scope') if moved and pid != 202 else row
        def call(args, **kwargs):
            nonlocal moved
            calls.append(args)
            if args[0] == 'systemctl': moved = True
        with patch.object(scope, 'identity', side_effect=identity), patch.object(scope, 'tree', side_effect=lambda pid: [identity(x) for x in [200, 201, 202]]), patch.object(scope.subprocess, 'check_output', return_value='100'):
            result = scope.migrate(200, '42', self.source, call)
        self.assertTrue(result['verified'])
        self.assertEqual(result['moved'], [200, 201])
        self.assertIn('200', calls[0])
        self.assertIn('201', calls[0])
        self.assertNotIn('202', calls[0])
        self.assertEqual(calls[1][:3], ['systemctl', '--user', 'start'])
        self.assertNotIn('kill', str(calls))

    def test_wrong_source_never_migrates_browser_or_other_units(self):
        call = Mock()
        root = dict(self.root, cgroup='/user.slice/browser.service')
        with patch.object(scope, 'identity', return_value=root):
            with self.assertRaisesRegex(ValueError, 'Only a reviewed runner'):
                scope.migrate(200, '42', root['cgroup'], call)
        call.assert_not_called()


if __name__ == '__main__': unittest.main()
