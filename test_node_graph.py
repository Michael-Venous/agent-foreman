"""Node-graph management; fake hub only, isolated state (no live API, no GPU)."""
import json, tempfile, time, unittest
from supervisor import Supervisor
from test_supervisor import IsolatedSupervisorTest

OVERSEER = 'over'


class FakeNodeHub:
    """Enough of HAPI for node reconciliation: sessions, messages, graph edges and wakes."""
    def __init__(self):
        self.sessions = {}; self.messages = {}; self.sent = []; self.edges = {}; self.nodes = set(); self.calls = []
        self.workspace = 'default-ws'; self.spawned = 0
        self.add(OVERSEER, flavor='claude')

    def add(self, sid, flavor='codex', **state):
        self.sessions[sid] = {'id': sid, 'active': True, 'thinking': False, 'backgroundTaskCount': 0, 'pendingRequestsCount': 0,
                              'metadata': {'flavor': flavor, 'path': '/tmp', 'name': sid.title(), 'codexSessionId': 'n-' + sid}, 'agentState': {}, **state}
        self.messages[sid] = []

    def graph(self):
        return {'activeWorkspaceId': self.workspace, 'workspaces': [], 'overseer': OVERSEER,
                'graph': {'workspaceId': self.workspace, 'nodes': [{'sessionId': n, 'x': None, 'y': None, 'addedAt': 0} for n in sorted(self.nodes)],
                          'edges': [{'owner': o, 'child': c, 'createdAt': 0, 'createdBy': 'user'} for c, o in self.edges.items()]}}

    def call(self, path, data=None, method=None):
        self.calls.append(path)
        if path == '/node-graph': return self.graph()
        if path == '/node-graph/edges':
            self.edges[data['child']] = data['owner']; self.nodes |= {data['child']}; return self.graph()
        if path == '/node-graph/edges/remove':
            self.edges.pop(data['child'], None); return self.graph()
        if path == '/sessions': return {'sessions': [{k: v for k, v in s.items() if k != 'agentState'} for s in self.sessions.values()]}
        if path == '/machines': return {'machines': [{'id': 'm'}]}
        if path == '/machines/m/spawn':
            self.spawned += 1; sid = 'spawned%d' % self.spawned; self.add(sid); return {'type': 'success', 'sessionId': sid}
        parts = path.split('?')[0].split('/')
        sid = parts[2] if len(parts) > 2 else None
        if sid not in self.sessions: raise ValueError('HTTP 404 ' + path)
        if len(parts) == 3:
            if method == 'PATCH': return {'ok': True}
            return {'session': self.sessions[sid]}
        if parts[3] == 'messages' and len(parts) == 4:
            if data is None: return {'messages': self.messages[sid], 'page': {'hasMore': False}}
            self.sent.append((sid, data)); return {'ok': True}
        if parts[3] == 'goal': return {'goal': None}
        if parts[3] == 'messages' and parts[4] == 'queued-state':
            return {'queuedLocalIds': [], 'indeterminateLocalIds': [], 'invokedLocalMessages': [{'localId': i, 'invokedAt': 1} for i in data['localIds']]}
        raise ValueError(path)


def reply(sid, seq, text='Done.'):
    return {'id': '%s-%d' % (sid, seq), 'seq': seq, 'createdAt': time.time() * 1000,
            'content': {'role': 'agent', 'content': {'type': 'output', 'data': {'type': 'task_complete', 'last_agent_message': text}}}}


class NodeGraphTests(IsolatedSupervisorTest):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.hub = FakeNodeHub()
        self.s = Supervisor(self.tmp.name + '/db', self.hub); self.s.set('manager', OVERSEER)
        self.p = self.s.dispatch({'op': 'register', 'name': 'Classic', 'repo': self.tmp.name, 'objective': 'x'})['id']
        self.s.dispatch({'op': 'resume', 'project': self.p})

    def tearDown(self): self.s.db.close(); self.tmp.cleanup()

    def worker(self, wid, sid, state='managed'):
        self.hub.add(sid)
        self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)', (wid, self.p, 'codex', 'n-' + sid, sid, self.tmp.name, state, time.time() - 100))

    def state(self, wid): return self.s.worker(wid)['state']

    def wakes(self, sid): return [d for target, d in self.hub.sent if target == sid]

    def test_classic_managed_workers_seed_as_overseer_children_once(self):
        self.worker('w1', 'c1')
        self.s.reconcile()
        self.assertEqual(self.hub.edges, {'c1': OVERSEER})
        self.assertEqual(self.s.get('owner:w1'), OVERSEER); self.assertEqual(self.state('w1'), 'managed')
        self.hub.edges.clear(); self.s.reconcile()  # user removed the edge; seeding never re-adds it
        self.assertEqual(self.hub.edges, {}); self.assertEqual(self.state('w1'), 'released')

    def test_edge_removal_releases_quietly_and_a_new_edge_remanages(self):
        self.worker('w1', 'c1'); self.s.reconcile()
        self.s.event('result:x', self.p, 'w1', 'finished', 'pending result')
        del self.hub.edges['c1']; self.s.reconcile()
        self.assertEqual(self.state('w1'), 'released'); self.assertEqual(self.wakes(OVERSEER), [])
        self.hub.edges['c1'] = OVERSEER; self.s.reconcile()
        self.assertEqual(self.state('w1'), 'managed'); self.assertEqual(self.s.watch('w1')['mode'], 'events')
        self.assertIsNone(self.s.get('unmanaged:w1'))

    def test_graph_edge_to_unregistered_chat_registers_it_observation_only(self):
        self.s.set('node-graph-seeded', '1'); self.hub.add('manual')
        self.hub.edges['manual'] = OVERSEER; self.s.reconcile()
        w = self.s.rows("SELECT * FROM workers WHERE session='manual'")[0]
        self.assertEqual(w['state'], 'managed'); self.assertEqual(w['native_id'], 'n-manual')
        self.assertEqual(self.s.project(w['project'])['status'], 'running'); self.assertEqual(self.hub.sent, [])

    def test_idle_child_edge_sleeps_after_one_final_scan_and_wakes_on_new_work(self):
        self.worker('w1', 'c1'); self.s.reconcile()
        count = lambda: sum(1 for c in self.hub.calls if c.startswith('/sessions/c1/messages'))
        before = count(); self.s.reconcile(); self.s.reconcile()
        self.assertEqual(count(), before)  # asleep: no message polling
        self.assertTrue(self.s.publish_managed()['c1']['asleep'])
        self.hub.sessions['c1']['thinking'] = True; self.s.reconcile()
        self.assertGreater(count(), before); self.assertFalse(self.s.publish_managed()['c1']['asleep'])
        self.hub.sessions['c1']['thinking'] = False; mid = count(); self.s.reconcile(); self.s.reconcile()
        self.assertEqual(count(), mid + 1)  # one final scan after work ends, then asleep again

    def test_late_failure_wakes_idle_observation_once_without_starting_worker(self):
        self.worker('w1', 'c1');self.s.reconcile()
        count=lambda:sum(1 for c in self.hub.calls if c.startswith('/sessions/c1/messages'))
        before=count()
        self.hub.sessions['c1'].update(lastFailureAt=123,lastFailure={'kind':'failed','message':'transport failure','at':123})
        self.s.reconcile();after=count();self.assertGreater(after,before)
        self.s.reconcile();self.s.reconcile();self.assertEqual(count(),after)
        events=self.s.rows("SELECT detail FROM events WHERE worker='w1' AND kind='observation'")
        self.assertTrue(any('runtime-failure' in x['detail'] for x in events))
        self.assertEqual(len(self.hub.sent),1);self.assertEqual(self.hub.sent[0][0],OVERSEER)
        self.assertIn('runtime-failure',self.hub.sent[0][1]['text'])

    def test_background_work_and_pending_input_keep_the_edge_awake(self):
        self.worker('w1', 'c1'); self.s.reconcile(); self.s.reconcile()
        for state in ({'backgroundTaskCount': 1}, {'pendingRequestsCount': 1}):
            self.hub.sessions['c1'].update(state); self.s.reconcile()
            self.assertIsNone(self.s.get('asleep:w1'))
            self.hub.sessions['c1'].update(backgroundTaskCount=0, pendingRequestsCount=0)

    def test_failed_final_background_scan_retries_before_sleep_without_worker_input(self):
        self.worker('w1', 'c1');self.hub.sessions['c1'].update(backgroundTaskCount=1,
            backgroundProcesses=[{'id':'boot:job:1','pid':123,'checkedAt':1000000}])
        self.s.reconcile();self.assertIsNone(self.s.get('asleep:w1'))
        self.hub.sessions['c1'].update(backgroundTaskCount=0,backgroundProcesses=[])
        original=self.hub.call;failed=False
        def transient(path,data=None,method=None):
            nonlocal failed
            if path.startswith('/sessions/c1/messages') and not failed:
                failed=True;raise RuntimeError('Transient final-scan failure')
            return original(path,data,method)
        self.hub.call=transient;self.s.reconcile()
        self.assertTrue(failed);self.assertIsNone(self.s.get('asleep:w1'))
        self.s.reconcile();self.assertIsNotNone(self.s.get('asleep:w1'))
        events=self.s.rows("SELECT * FROM events WHERE worker='w1' AND kind='observation'")
        self.assertEqual(len(events),1);self.assertIn('background-ended',events[0]['detail'])
        self.assertEqual(self.hub.edges,{'c1':OVERSEER});self.assertEqual(len(self.wakes(OVERSEER)),2)
        self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE worker='w1' AND kind='observer-error'")),1)
        self.assertEqual(self.wakes('c1'),[])
        count=len(self.hub.calls);self.s.reconcile()
        self.assertFalse(any(p.startswith('/sessions/c1/messages') for p in self.hub.calls[count:]))

    def test_finished_idle_child_is_never_auto_retired(self):
        self.worker('w1', 'c1'); self.hub.messages['c1'] = [reply('c1', 1)]
        for _ in range(4):
            self.s.set('retirement-candidate:w1', json.dumps({'message': 'c1-1', 'at': time.time() - 60}))
            self.s.reconcile()
        self.assertEqual(self.state('w1'), 'managed')

    def test_graph_detach_cancels_future_reminder_with_audit_and_reattach_never_replays_it(self):
        from unittest.mock import patch
        self.worker('w1', 'c1'); self.s.reconcile()
        reminder = self.s.dispatch({'op': 'remind', 'worker': 'w1', 'in_minutes': 15, 'note': 'Check capacity before scoped continuation'})
        self.hub.edges.pop('c1'); self.s.reconcile()
        self.assertEqual(self.state('w1'), 'released')
        audit = self.s.one('SELECT * FROM reminder_deliveries WHERE reminder_id=?', (reminder['id'],))
        self.assertEqual(audit['status'], 'cancelled')
        self.assertIn('node graph or workspace', audit['reason'])
        self.hub.edges['c1'] = OVERSEER; self.s.reconcile()
        self.assertEqual(self.state('w1'), 'managed')
        with patch('supervisor.time.time', return_value=reminder['due'] + 1):
            self.s.deliver_reminders(); self.s.notify_manager()
        self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?', (reminder['id'],))['active'], 0)
        self.assertEqual(self.s.rows('SELECT * FROM events WHERE event_key LIKE ?', ('remind:' + reminder['id'] + ':%',)), [])
        self.assertEqual(self.hub.sent, [])
        self.assertEqual(self.s.rows('SELECT * FROM assignments'), [])

    def test_events_go_to_the_direct_owner_and_overseer_children_to_the_overseer(self):
        self.worker('mid', 'midchat'); self.worker('leaf', 'leafchat'); self.s.set('node-graph-seeded', '1')
        self.hub.edges.update({'midchat': OVERSEER, 'leafchat': 'midchat'}); self.s.reconcile()
        self.s.event('a', self.p, 'leaf', 'input-required', 'leaf question')
        self.s.event('b', self.p, 'mid', 'input-required', 'mid question')
        self.s.notify_manager()
        self.assertIn('leaf question', self.wakes('midchat')[0]['text']); self.assertNotIn('mid question', self.wakes('midchat')[0]['text'])
        self.assertIn('chats you own in the node graph', self.wakes('midchat')[0]['text'])
        self.assertIn('mid question', self.wakes(OVERSEER)[0]['text']); self.assertNotIn('leaf question', self.wakes(OVERSEER)[0]['text'])

    def test_disconnected_mid_owner_keeps_events_and_is_not_resumed(self):
        self.worker('mid', 'midchat'); self.worker('leaf', 'leafchat'); self.s.set('node-graph-seeded', '1')
        self.hub.edges.update({'midchat': OVERSEER, 'leafchat': 'midchat'}); self.s.reconcile()
        self.hub.sessions['midchat']['active'] = False
        self.s.event('a', self.p, 'leaf', 'input-required', 'leaf question'); self.s.notify_manager()
        self.assertEqual(self.hub.sent, []); self.assertNotIn('/sessions/midchat/resume', self.hub.calls)
        self.assertEqual(self.s.one("SELECT wake_id FROM events WHERE event_key='a'")['wake_id'], None)

    def test_mid_level_agents_act_only_on_their_own_children(self):
        self.worker('mid', 'midchat'); self.worker('leaf', 'leafchat'); self.worker('other', 'otherchat'); self.s.set('node-graph-seeded', '1')
        self.hub.edges.update({'midchat': OVERSEER, 'leafchat': 'midchat', 'otherchat': OVERSEER}); self.s.reconcile()
        as_mid = lambda **o: self.s.dispatch({**o, 'actor': 'midchat'})
        with self.assertRaisesRegex(ValueError, 'own children'): as_mid(op='assign', worker='other', text='x', key='k1')
        with self.assertRaisesRegex(ValueError, 'Only the overseer'): as_mid(op='unmanage', worker='leaf', session='leafchat')
        with self.assertRaisesRegex(ValueError, 'Only the overseer'): as_mid(op='node_edge', owner='midchat', child='otherchat')
        with self.assertRaisesRegex(ValueError, 'below you'): as_mid(op='node_detach', child='otherchat')
        self.s.event('o', self.p, 'other', 'finished', 'other result')
        with self.assertRaisesRegex(ValueError, 'own children'): as_mid(op='ack', ids=[self.s.one("SELECT id FROM events WHERE event_key='o'")['id']])
        as_mid(op='assign', worker='leaf', text='continue', key='k2')
        as_mid(op='node_detach', child='leafchat', user_requested=True, basis='User asked to disconnect leafchat')
        self.assertNotIn('leafchat', self.hub.edges); self.assertEqual(self.state('leaf'), 'released')
        # The overseer keeps full control.
        self.s.dispatch({'op': 'node_edge', 'owner': 'midchat', 'child': 'otherchat', 'actor': OVERSEER})
        self.assertEqual(self.hub.edges['otherchat'], 'midchat'); self.assertEqual(self.s.get('owner:other'), 'midchat')

    def test_mid_level_spawn_creates_its_own_child(self):
        self.worker('mid', 'midchat'); self.s.set('node-graph-seeded', '1'); self.hub.edges['midchat'] = OVERSEER; self.s.reconcile()
        with self.assertRaisesRegex(ValueError, 'use a worktree'):
            self.s.dispatch({'op': 'node_spawn', 'actor': 'midchat', 'name': 'Shared checkout', 'directory': self.tmp.name, 'runtime': 'codex'})
        import os; os.mkdir(self.tmp.name + '/sub')
        result = self.s.dispatch({'op': 'node_spawn', 'actor': 'midchat', 'name': 'Sub task', 'directory': self.tmp.name + '/sub', 'runtime': 'codex', 'text': 'Start the sub task'})
        self.assertEqual(self.wakes(result['worker']['session'])[0]['text'], 'Start the sub task')
        self.assertEqual(result['owner'], 'midchat'); self.assertEqual(self.hub.edges[result['worker']['session']], 'midchat')
        self.s.reconcile(); self.assertEqual(self.s.get('owner:' + result['worker']['id']), 'midchat')

    def test_gpu_reservations_are_advisory_and_mid_level_requests_reach_the_overseer(self):
        self.worker('mid', 'midchat'); self.s.set('node-graph-seeded', '1'); self.hub.edges['midchat'] = OVERSEER; self.s.reconcile()
        self.s.set('resource-reservation:main/GPU-x', json.dumps({'resource': 'main/GPU-x', 'reserved': True, 'owner': 'user/personal'}))
        first = self.s.dispatch({'op': 'gpu', 'action': 'reserve', 'gpu': 'render-pc 3080 Ti', 'holder': 'drift'})
        self.assertTrue(first['recorded'])
        clash = self.s.dispatch({'op': 'gpu', 'action': 'reserve', 'gpu': 'render-pc 3080 ti', 'holder': 'terrain'})
        self.assertFalse(clash['recorded']); self.assertIn('drift', clash['warning'])
        self.assertTrue(self.s.dispatch({'op': 'gpu', 'action': 'reserve', 'gpu': 'render-pc 3080 Ti', 'holder': 'terrain', 'force': True})['recorded'])
        self.assertEqual(self.s.dispatch({'op': 'gpu'})['personal'][0]['owner'], 'user/personal')
        self.s.dispatch({'op': 'gpu', 'action': 'reserve', 'gpu': '4060 Ti', 'actor': 'midchat', 'note': 'preview render'})
        self.assertEqual(self.s.one("SELECT kind FROM events WHERE kind='gpu-request'")['kind'], 'gpu-request')
        self.assertFalse(any(r['gpu'] == '4060 Ti' for r in self.s.dispatch({'op': 'gpu'})['reservations']))
        self.s.notify_manager(); self.assertIn('4060 Ti', self.wakes(OVERSEER)[0]['text'])

    def test_completion_cleanup_unmanage_from_overseer_is_rejected_without_changes(self):
        self.worker('w1', 'c1'); self.s.reconcile()
        before = list(self.s.db.iterdump()); calls = list(self.hub.calls)
        for command in ({'op': 'unmanage', 'worker': 'w1', 'session': 'c1'}, {'op': 'node_detach', 'child': 'c1'}):
            for invalid in ({}, {'user_requested': True}, {'user_requested': 'true', 'basis': 'Done'}, {'user_requested': True, 'basis': ' '}):
                with self.assertRaisesRegex(ValueError, 'stay connected'):
                    self.s.dispatch({**command, **invalid, 'actor': OVERSEER})
        self.assertEqual(list(self.s.db.iterdump()), before)
        self.assertEqual(self.hub.calls, calls); self.assertEqual(self.hub.edges, {'c1': OVERSEER})
        self.assertEqual(self.hub.sent, [])

    def test_explicit_current_user_disconnect_through_overseer_still_works(self):
        for op in ('unmanage', 'node_detach'):
            with self.subTest(op=op):
                self.worker(op, op); self.s.reconcile()
                self.s.dispatch({'op': op, 'worker': op, 'session': op, 'child': op, 'actor': OVERSEER,
                                 'user_requested': True, 'basis': 'User asks to disconnect this exact node'})
                self.assertNotIn(op, self.hub.edges); self.assertEqual(self.state(op), 'released')
                self.assertEqual(self.hub.sent, [])

    def test_stale_retirement_call_cannot_release_connected_idle_node(self):
        self.worker('w1', 'c1'); self.s.reconcile()
        before = list(self.s.db.iterdump())
        self.s.retire_finished(self.s.worker('w1'), self.hub.sessions['c1'], [reply('c1', 1)])
        self.s.end_management(self.s.worker('w1'), {'message': 'c1-1', 'reply': 'Done.'})
        self.assertEqual(list(self.s.db.iterdump()), before)
        self.assertEqual(self.state('w1'), 'managed'); self.assertEqual(self.hub.edges, {'c1': OVERSEER})

    def test_classic_manage_and_unmanage_map_to_owner_edges(self):
        self.worker('w1', 'c1'); self.s.reconcile()
        self.s.dispatch({'op': 'unmanage', 'worker': 'w1', 'session': 'c1'})
        self.assertNotIn('c1', self.hub.edges); self.s.reconcile(); self.assertEqual(self.state('w1'), 'released')
        self.s.dispatch({'op': 'manage', 'worker': 'w1', 'user_requested': True, 'resume_management': True})
        self.assertEqual(self.hub.edges['c1'], OVERSEER); self.s.reconcile(); self.assertEqual(self.state('w1'), 'managed')

    def test_user_restart_keeps_node_management(self):
        self.worker('w1', 'c1'); self.hub.sessions['c1']['metadata']['hostPid'] = 101; self.s.reconcile()
        self.hub.sessions['c1'].update(thinking=True); self.hub.sessions['c1']['metadata']['hostPid'] = 202; self.s.reconcile()
        self.assertEqual(self.state('w1'), 'managed')

    def test_workspace_switch_releases_old_children_and_manages_new_ones(self):
        self.worker('a', 'achat'); self.worker('b', 'bchat', state='released'); self.s.set('node-graph-seeded', '1')
        self.hub.edges['achat'] = OVERSEER; self.s.reconcile()
        self.hub.workspace = 'preset'; self.hub.edges = {'bchat': OVERSEER}; self.s.reconcile()
        self.assertEqual(self.state('a'), 'released'); self.assertEqual(self.state('b'), 'managed')
        self.assertEqual(self.hub.sent, [])

    def test_hub_without_node_graph_keeps_classic_mode(self):
        hub = self.hub; original = hub.call
        def classic(path, data=None, method=None):
            if path.startswith('/node-graph'): raise ValueError('HTTP Error 404: Not Found')
            return original(path, data, method)
        hub.call = classic; self.worker('w1', 'c1'); self.s.reconcile()
        self.assertEqual(self.s.get('node-graph-active'), '0'); self.assertNotIn('owner', self.s.publish_managed()['c1'])

    def test_bad_child_does_not_starve_other_branches_or_leave_partial_registration(self):
        self.s.set('node-graph-seeded','1');self.hub.add('bad',flavor='unsupported');self.hub.add('healthy')
        self.hub.edges.update({'bad':OVERSEER,'healthy':OVERSEER})
        self.s.reconcile();self.s.reconcile()
        self.assertEqual(len(self.s.rows("SELECT * FROM workers WHERE session='healthy'")),1)
        self.assertEqual(len(self.s.rows("SELECT * FROM workers WHERE session='bad'")),0)
        self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='node-error'")),1)
        self.assertEqual(len(self.s.rows('SELECT * FROM projects')),2) # fixture project + healthy only
        self.assertFalse(any(path.endswith('/resume') for path in self.hub.calls))

    def test_offline_controller_does_not_sleep_a_verified_background_job(self):
        self.worker('w1','c1');self.hub.sessions['c1'].update(active=False,backgroundTaskCount=1,
            backgroundProcesses=[{'id':'boot:1:start','pid':1,'checkedAt':time.time()*1000}])
        self.s.reconcile();self.s.reconcile();self.assertIsNone(self.s.get('asleep:w1'))
        self.hub.sessions['c1'].update(backgroundTaskCount=0,backgroundProcesses=[]);self.s.reconcile()
        changes=[c for e in self.s.rows("SELECT * FROM events WHERE worker='w1' AND kind='observation'") for c in json.loads(e['detail'])['changes']]
        self.assertTrue(any(c.get('category')=='background-ended' for c in changes))
        self.assertIsNotNone(self.s.get('asleep:w1'));self.assertEqual(self.wakes('c1'),[])
        self.assertNotIn('/sessions/c1/resume',self.hub.calls)

    def test_unavailable_tree_preserves_child_events_without_classic_fallback(self):
        self.worker('leaf','leafchat');self.s.reconcile();self.hub.sent=[]
        self.s.event('leaf-result',self.p,'leaf','finished','exact result')
        original=self.hub.call
        def unavailable(path,data=None,method=None):
            if path=='/node-graph':raise ValueError('HTTP Error 404: Not Found')
            return original(path,data,method)
        self.hub.call=unavailable;self.s.reconcile()
        self.assertEqual(self.s.get('node-graph-active'),'1');self.assertEqual(self.state('leaf'),'managed')
        self.assertIsNone(self.s.one("SELECT wake_id FROM events WHERE event_key='leaf-result'")['wake_id'])
        self.hub.call=original;self.s.reconcile()
        self.assertIsNotNone(self.s.one("SELECT wake_id FROM events WHERE event_key='leaf-result'")['wake_id'])
        self.assertEqual(sum('exact result' in message['text'] for _,message in self.hub.sent),1)

    def test_current_tree_routes_reparented_results_and_blocks_former_owner_controls(self):
        self.worker('mid','midchat');self.worker('leaf','leafchat');self.s.set('node-graph-seeded','1')
        self.hub.edges.update({'midchat':OVERSEER,'leafchat':'midchat'});self.s.reconcile();self.hub.sent=[]
        self.hub.edges['leafchat']=OVERSEER # before the next registry reconciliation
        with self.assertRaisesRegex(ValueError,'current graph'):
            self.s.dispatch({'op':'assign','worker':'leaf','actor':'midchat','text':'stale work','key':'stale'})
        self.s.event('fresh-owner',self.p,'leaf','finished','new owner only');self.s.notify_manager()
        self.assertEqual(self.wakes('midchat'),[]);self.assertEqual(len(self.wakes(OVERSEER)),1)
        self.assertIn('new owner only',self.wakes(OVERSEER)[0]['text'])
        self.s.notify_manager();self.assertEqual(len(self.wakes(OVERSEER)),1)

    def test_indirect_reminder_id_does_not_bypass_branch_authority(self):
        self.worker('mid','midchat');self.worker('other','otherchat');self.s.set('node-graph-seeded','1')
        self.hub.edges.update({'midchat':OVERSEER,'otherchat':OVERSEER});self.s.reconcile()
        reminder=self.s.dispatch({'op':'remind','worker':'other','in':900,'note':'keep this'})
        with self.assertRaisesRegex(ValueError,'own children'):
            self.s.dispatch({'op':'cancel_reminder','id':reminder['id'],'actor':'midchat'})
        self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(reminder['id'],))['active'],1)

    def test_assignment_only_cancellation_checks_real_owner_even_with_a_spoofed_worker(self):
        self.worker('mid','midchat');self.worker('leaf','leafchat');self.worker('other','otherchat');self.s.set('node-graph-seeded','1')
        self.hub.edges.update({'midchat':OVERSEER,'leafchat':'midchat','otherchat':OVERSEER});self.s.reconcile()
        assignment=self.s.dispatch({'op':'assign','worker':'other','key':'other-task','text':'isolated fake task'})
        before=list(self.hub.calls)
        with self.assertRaisesRegex(ValueError,'own children'):
            self.s.dispatch({'op':'cancel_queued','assignment':assignment['id'],'worker':'leaf','actor':'midchat'})
        self.assertEqual(self.s.one('SELECT state FROM assignments WHERE id=?',(assignment['id'],))['state'],'sent')
        self.assertFalse(any('/messages' in c and 'queued-state' not in c for c in self.hub.calls[len(before):]))

    def test_cycle_snapshot_is_rejected_before_registry_mutation(self):
        import node_graph
        self.worker('a','achat');self.worker('b','bchat');self.s.reconcile()
        before=list(self.s.db.execute('SELECT id,state FROM workers ORDER BY id'))
        self.hub.edges={'achat':'bchat','bchat':'achat'}
        with self.assertRaisesRegex(ValueError,'cycle'):node_graph.reconcile(self.s)
        self.assertEqual(list(self.s.db.execute('SELECT id,state FROM workers ORDER BY id')),before)

    def test_large_tree_observation_is_bounded_and_fair_without_launching_agents(self):
        import unittest.mock
        self.s.set('node-graph-seeded','1')
        for i in range(60):
            sid='work'+str(i);self.worker('w'+str(i),sid);self.hub.sessions[sid]['thinking']=True
            self.hub.edges[sid]=OVERSEER
        with unittest.mock.patch.dict('os.environ',{'HAPI_OBSERVATION_BATCH_SIZE':'12'}):
            for _ in range(5):
                before=len([c for c in self.hub.calls if '/messages?' in c]);self.s.reconcile()
                self.assertLessEqual(len([c for c in self.hub.calls if '/messages?' in c])-before,12)
        self.assertTrue(all(self.s.get('observation:w'+str(i)) for i in range(60)))
        self.assertEqual(self.hub.spawned,0);self.assertEqual(self.hub.sent,[])


if __name__ == '__main__':
    unittest.main()
