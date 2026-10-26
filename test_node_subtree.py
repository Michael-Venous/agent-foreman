"""A node controls the graph below itself: scoped node_tree/node_edit, node_spawn and node_detach.
Fake hub only; isolated state; no live API."""
import time, unittest
from test_node_graph import FakeNodeHub, NodeGraphTests, OVERSEER
from test_supervisor import IsolatedSupervisorTest

VERSION = 'a' * 64


class SubtreeHub(FakeNodeHub):
    def __init__(self):
        super().__init__(); self.edits = []
    def call(self, path, data=None, method=None):
        if path.startswith('/node-graph/tree'):
            g = self.graph()
            return {'activeWorkspaceId': self.workspace, 'overseer': OVERSEER, 'pausedAt': {},
                    'workspaces': [{'workspace': {'id': self.workspace, 'name': 'Default'}, 'version': VERSION,
                                    'graph': {**g['graph'], 'junctions': [], 'annotations': [{'id': 'note', 'kind': 'text', 'text': 'outside', 'color': 'slate', 'x': 0, 'y': 0, 'width': 1, 'height': 1}]},
                                    'children': {}, 'lone': []}]}
        if path.endswith('/edit'):
            self.edits.append(data)
            if not data['dryRun']:
                for c in data['changes']:
                    if c['action'] == 'connect': self.edges[c['child']] = c['owner']
                    if c['action'] == 'disconnect': self.edges.pop(c['child'], None)
            return {'dryRun': data['dryRun'], 'version': VERSION, 'managementChanged': not data['dryRun']}
        return super().call(path, data, method)


class SubtreeTests(IsolatedSupervisorTest):
    tearDown = NodeGraphTests.tearDown
    worker = NodeGraphTests.worker
    state = NodeGraphTests.state

    def setUp(self):
        NodeGraphTests.setUp(self)
        self.hub.__class__ = SubtreeHub; self.hub.edits = []
        # overseer -> mid -> (leaf -> grand), overseer -> other
        for wid, sid in (('mid', 'midchat'), ('leaf', 'leafchat'), ('grand', 'grandchat'), ('other', 'otherchat')): self.worker(wid, sid)
        self.s.set('node-graph-seeded', '1')
        self.hub.edges.update({'midchat': OVERSEER, 'leafchat': 'midchat', 'grandchat': 'leafchat', 'otherchat': OVERSEER}); self.hub.nodes |= set(self.hub.edges); self.s.reconcile()
        self.mid = lambda **o: self.s.dispatch({**o, 'actor': 'midchat'})
        self.edit = lambda *changes, **o: self.mid(op='node_edit', workspace=self.hub.workspace, version=VERSION, changes=list(changes), **o)

    def test_tree_shows_only_the_actors_subtree_in_the_active_workspace(self):
        view = self.mid(op='node_tree')
        snap = view['workspaces'][0]
        self.assertEqual({n['sessionId'] for n in snap['graph']['nodes']}, {'midchat', 'leafchat', 'grandchat'})
        self.assertEqual({(e['owner'], e['child']) for e in snap['graph']['edges']}, {('midchat', 'leafchat'), ('leafchat', 'grandchat')})
        self.assertEqual(view['controls']['scope'], 'subtree'); self.assertEqual(view['root']['sessionId'], 'midchat')
        self.assertNotIn('overseer', view); self.assertEqual(snap['graph']['annotations'], []); self.assertEqual(snap['lone'], [])
        full = self.s.dispatch({'op': 'node_tree', 'actor': OVERSEER})
        self.assertIn('otherchat', {n['sessionId'] for n in full['workspaces'][0]['graph']['nodes']})  # overseer view unchanged

    def test_connect_and_layout_below_you_reach_the_hub_and_dry_run_is_preserved(self):
        self.edit({'action': 'connect', 'owner': 'midchat', 'child': 'grandchat'}, {'action': 'layout', 'positions': {'leafchat': {'x': 1, 'y': 2}}}, dryRun=True)
        self.assertTrue(self.hub.edits[-1]['dryRun']); self.assertEqual(self.hub.edges['grandchat'], 'leafchat')
        self.assertEqual(self.hub.edits[-1]['createdBy'], 'midchat')
        self.edit({'action': 'connect', 'owner': 'midchat', 'child': 'grandchat'})
        self.assertEqual(self.hub.edges['grandchat'], 'midchat')

    def test_edits_outside_the_subtree_or_workspace_wide_actions_are_refused_before_the_hub(self):
        bad = [({'action': 'connect', 'owner': 'midchat', 'child': 'otherchat'}, 'below you'),
               ({'action': 'connect', 'owner': 'otherchat', 'child': 'leafchat'}, 'below you'),
               ({'action': 'connect', 'owner': 'leafchat', 'child': 'leafchat'}, 'below you'),
               ({'action': 'connect', 'owner': 'midchat', 'child': 'midchat'}, 'below you'),
               ({'action': 'disconnect', 'child': 'otherchat'}, 'below you'),
               ({'action': 'remove', 'ids': ['leafchat', 'otherchat']}, 'below you'),
               ({'action': 'layout', 'positions': {'otherchat': {'x': 0, 'y': 0}}}, 'Layout only'),
               ({'action': 'add', 'sessionId': 'otherchat'}, 'only'), ({'action': 'routing', 'value': {}}, 'only'),
               ({'action': 'annotation', 'id': 'n', 'value': None}, 'only'), ({'action': 'rename', 'name': 'x'}, 'only')]
        for change, message in bad:
            with self.assertRaisesRegex(ValueError, message): self.edit(change)
        with self.assertRaisesRegex(ValueError, 'active workspace'): self.mid(op='node_edit', workspace='elsewhere', version=VERSION, changes=[{'action': 'layout', 'positions': {}}])
        self.assertEqual(self.hub.edits, [])

    def test_disconnect_below_you_still_needs_an_explicit_user_request_to_apply(self):
        self.edit({'action': 'disconnect', 'child': 'grandchat'}, dryRun=True)
        self.assertTrue(self.hub.edits[-1]['preserveConnections'])  # hub keeps ownership without an explicit request
        self.edit({'action': 'disconnect', 'child': 'grandchat'}, user_requested=True, basis='User asked to disconnect grandchat')
        self.assertFalse(self.hub.edits[-1]['preserveConnections'])

    def test_detach_and_spawn_work_at_any_level_below_but_not_elsewhere(self):
        import os; os.mkdir(self.tmp.name + '/deep'); os.mkdir(self.tmp.name + '/other')
        made = self.mid(op='node_spawn', owner='leafchat', name='Deep child', directory=self.tmp.name + '/deep', runtime='codex')
        self.assertEqual(made['owner'], 'leafchat'); self.assertEqual(self.hub.edges[made['worker']['session']], 'leafchat')
        with self.assertRaisesRegex(ValueError, 'below you'): self.mid(op='node_spawn', owner='otherchat', name='Nope', directory=self.tmp.name + '/other', runtime='codex')
        self.mid(op='node_detach', child='grandchat', user_requested=True, basis='User asked to disconnect grandchat')
        self.assertNotIn('grandchat', self.hub.edges)
        with self.assertRaisesRegex(ValueError, 'below you'): self.mid(op='node_detach', child='otherchat', user_requested=True, basis='x')

    def test_the_actor_cannot_hand_chats_to_itself_from_above_or_edit_its_own_owner(self):
        with self.assertRaisesRegex(ValueError, 'below you'): self.edit({'action': 'connect', 'owner': OVERSEER, 'child': 'leafchat'})
        with self.assertRaisesRegex(ValueError, 'below you'): self.edit({'action': 'disconnect', 'child': 'midchat'})


if __name__ == '__main__':
    unittest.main()
