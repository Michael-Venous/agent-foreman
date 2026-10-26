"""Overseer graph controls: no live API, worker messages or native controllers."""
import copy
import json
import tempfile
from unittest.mock import patch
from supervisor import Supervisor
from test_supervisor import IsolatedSupervisorTest
from test_node_graph import FakeNodeHub, OVERSEER


class NodeTreeControls(IsolatedSupervisorTest):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.api = FakeNodeHub()
        self.s = Supervisor(self.tmp.name + '/db', self.api); self.s.set('manager', OVERSEER)
        self.api.add('leaf', thinking=True); self.api.add('lone', active=False)
        graph = self.api.graph(); graph['nodes'] = [{'sessionId': 'leaf', 'x': 0, 'y': 40, 'addedAt': 1}, {'sessionId': 'lone', 'x': None, 'y': None, 'addedAt': 2}]
        graph['edges'] = [{'owner': OVERSEER, 'child': 'leaf', 'createdAt': 1, 'createdBy': 'user'}]
        self.tree = {'activeWorkspaceId': self.api.workspace, 'overseer': OVERSEER, 'pausedAt': {'lone': 42},
                     'workspaces': [{'workspace': {'id': self.api.workspace, 'name': 'Live', 'kind': 'default'}, 'version': 'a' * 64, 'graph': graph},
                                    {'workspace': {'id': 'other', 'name': 'Saved', 'kind': 'preset'}, 'version': 'b' * 64, 'graph': {**copy.deepcopy(graph), 'workspaceId': 'other'}}]}
        self.calls = []; original = self.api.call
        def call(path, data=None, method=None):
            self.calls.append((path, copy.deepcopy(data)))
            if path.startswith('/node-graph/tree'):
                result = copy.deepcopy(self.tree)
                if '?workspaceId=other' in path: result['workspaces'] = result['workspaces'][1:]
                return result
            if path.endswith('/edit'): return {'version': 'c' * 64, 'dryRun': data['dryRun'], 'managementChanged': False}
            return original(path, data, method)
        self.mock = patch.object(self.api, 'call', side_effect=call); self.mock.start()

    def tearDown(self):
        self.mock.stop(); self.s.db.close(); self.tmp.cleanup()

    def test_full_tree_has_every_workspace_hierarchy_labels_and_stop_without_polling_histories(self):
        before = list(self.s.db.iterdump())
        result = self.s.dispatch({'op': 'node_tree', 'actor': OVERSEER})
        self.assertEqual(len(result['workspaces']), 2)
        active, saved = result['workspaces']
        self.assertTrue(active['active']); self.assertFalse(saved['active'])
        self.assertEqual(active['routing']['edges'], [{'owner': OVERSEER, 'child': 'leaf'}])
        self.assertEqual(active['children'], {OVERSEER: ['leaf']}); self.assertEqual(active['lone'], ['lone'])
        self.assertEqual(active['graph']['nodes'][0]['title'], 'Leaf')
        self.assertEqual(active['graph']['nodes'][0]['status'], 'Running')
        self.assertEqual(active['graph']['nodes'][1]['status'], 'Stopped')
        self.assertEqual(active['graph']['nodes'][1]['pausedAt'], 42)
        self.assertEqual([p for p, _ in self.calls], ['/node-graph/tree', '/sessions'])
        self.assertEqual(list(self.s.db.iterdump()), before); self.assertEqual(self.api.sent, [])

    def test_summary_and_workspace_filter_keep_versions_for_later_edits(self):
        result = self.s.dispatch({'op': 'node_tree', 'workspace': 'other', 'summary': True, 'actor': OVERSEER})
        self.assertEqual(len(result['workspaces']), 1)
        saved = result['workspaces'][0]
        self.assertEqual(saved['version'], 'b' * 64); self.assertEqual(saved['counts']['nodes'], 2)
        self.assertNotIn('graph', saved)
        self.assertEqual(self.calls[0][0], '/node-graph/tree?workspaceId=other')

    def test_edit_sends_one_scoped_versioned_request_and_never_reconciles_or_messages(self):
        changes = [{'action': 'layout', 'positions': {'leaf': {'x': 4, 'y': 5}}}]
        result = self.s.dispatch({'op': 'node_edit', 'actor': OVERSEER, 'workspace': 'other', 'version': 'b' * 64, 'changes': changes, 'dryRun': True})
        self.assertTrue(result['dryRun'])
        self.assertEqual(self.calls, [('/node-graph/workspaces/other/edit', {'version': 'b' * 64, 'changes': changes, 'dryRun': True, 'createdBy': OVERSEER, 'preserveConnections': True})])
        self.assertEqual(self.api.sent, [])

    def test_non_overseer_cannot_use_controls_even_before_node_mode_initialization(self):
        for op in ('node_tree', 'node_edit'):
            with self.assertRaisesRegex(ValueError, 'Only the overseer'):
                self.s.dispatch({'op': op, 'actor': 'leaf'})
        self.assertEqual(self.calls, [])

    def test_invalid_and_native_control_actions_are_rejected_without_api_calls(self):
        valid = {'op': 'node_edit', 'workspace': 'other', 'version': 'b' * 64, 'changes': [{'action': 'disconnect', 'child': 'leaf'}], 'actor': OVERSEER}
        for override in ({'version': ''}, {'workspace': None}, {'changes': []}, {'dryRun': 'false'}, *({'changes': [{'action': action}]} for action in ('resume', 'stop', 'send', 'activate'))):
            with self.assertRaises(ValueError): self.s.dispatch({**valid, **override})
        self.assertEqual(self.calls, [])

    def test_conflict_is_returned_without_retry_or_bypass(self):
        with patch.object(self.api, 'call', side_effect=RuntimeError('409: Node tree changed')) as request:
            with self.assertRaisesRegex(RuntimeError, '409'):
                self.s.dispatch({'op': 'node_edit', 'workspace': 'other', 'version': 'b' * 64, 'changes': [{'action': 'rename', 'name': 'New'}], 'actor': OVERSEER})
            request.assert_called_once()
        self.assertEqual(self.api.sent, [])

    def test_legacy_graph_read_discovers_new_controls_without_a_wake(self):
        result = self.s.dispatch({'op': 'node_graph', 'actor': OVERSEER})
        self.assertEqual(result['controls']['read'], 'node_tree')
        self.assertEqual(result['controls']['edit'], 'node_edit')
        self.assertEqual(self.api.sent, [])

    def test_agent_tree_edits_protect_nodes_and_ownership_unless_user_explicitly_requests_removal(self):
        command = {'op': 'node_edit', 'workspace': 'other', 'version': 'b' * 64,
                   'changes': [{'action': 'disconnect', 'child': 'leaf'}], 'actor': OVERSEER}
        for extra, protected in (({}, True), ({'user_requested': True}, True),
                                 ({'user_requested': True, 'basis': 'User asks to disconnect leaf'}, False)):
            self.s.dispatch({**command, **extra})
            self.assertEqual(self.calls[-1][1]['preserveConnections'], protected)
        self.s.dispatch({key: value for key, value in command.items() if key != 'actor'})
        self.assertFalse(self.calls[-1][1]['preserveConnections'])
        self.assertEqual(self.api.sent, [])
