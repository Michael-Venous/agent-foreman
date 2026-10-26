"""Input parsing and actor stamping only: no live control requests."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from managerctl import read_command, stamp_command


class ManagerControlInput(unittest.TestCase):
    def test_keeps_legacy_json_argument(self):
        self.assertEqual(read_command(['{"op":"node_tree"}']), {'op': 'node_tree'})

    def test_reads_large_unicode_file_without_shell_quoting(self):
        command = {'op': 'node_edit', 'changes': [{'action': 'annotation', 'value': "It's a note ★\n" * 20000}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'tree edit.json'; path.write_text(json.dumps(command))
            self.assertGreater(path.stat().st_size, 128 * 1024)
            self.assertEqual(read_command(['--file', str(path)]), command)

    def test_reads_stdin(self):
        self.assertEqual(read_command(['-'], io.StringIO('{"op":"node_tree","summary":true}')), {'op': 'node_tree', 'summary': True})

    def test_rejects_bad_input_without_submitting_a_request(self):
        for args in ([], ['a', 'b'], ['null'], ['[]'], ['{}'], ['{"op":5}'], ['{"op":""}']):
            with self.assertRaises(ValueError): read_command(args)

    def test_all_input_modes_use_real_actor_and_do_not_mutate_the_input(self):
        command = {'op': 'node_edit', 'actor': 'spoofed-overseer'}
        stamped = stamp_command(command, {'HAPI_SESSION_ID': 'actual-child'})
        self.assertEqual(stamped['actor'], 'actual-child'); self.assertEqual(command['actor'], 'spoofed-overseer')
        self.assertNotIn('actor', stamp_command(command, {}))

    def test_native_resume_and_resource_commands_still_receive_a_fresh_timestamp(self):
        for op in ('resume_chat', 'steer_assignment', 'steer_queued', 'supersede_assignment', 'resource_handoff', 'resource_reservation', 'cancel_queued'):
            with patch('managerctl.time.time', return_value=123):
                self.assertEqual(stamp_command({'op': op, 'requested_at': 1}, {})['requested_at'], 123)
        self.assertNotIn('requested_at', stamp_command({'op': 'node_edit'}, {}))
