import copy
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from supervisor import Supervisor
from test_supervisor import FakeHapi, IsolatedSupervisorTest
from resource_handoff import gpu_snapshot, public_reply, desktop_compositor, chrome_gpu_identity, reviewed_gpu_snapshot

GPU = 'main/GPU-64a43df7-51bc-56f4-957a-98c3793a2a45'
QUOTE = 'The main 3090 Ti is released; no more jobs will use it until reassigned.'

class HandoffTests(IsolatedSupervisorTest):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = FakeHapi()
        self.s = Supervisor(self.tmp.name + '/db', self.api)
        self.source = self.s.dispatch({'op': 'register', 'name': 'Terrain', 'repo': self.tmp.name, 'objective': 'Preserve native goal'})['id']
        self.target = self.s.dispatch({'op': 'register', 'name': 'Drift', 'repo': self.tmp.name, 'objective': 'User revision'})['id']
        self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)', ('w', self.source, 'claude', 'native', 's', self.tmp.name, 'managed', time.time()-100))
        self.s.dispatch({'op': 'claim', 'project': self.source, 'resource': GPU})
        self.s.db.execute('INSERT INTO assignments(id,worker,task_key,text,state,created,seen_busy,goal) VALUES(?,?,?,?,?,?,?,?)', ('a', 'w', 'goal', '/goal original objective', 'sent', time.time()-60, 1, 1))
        self.api.invoked = ['a']
        self.api.session.update(metadata={'claudeSessionId': 'native'}, manualPausedAt=123, goal={'status': 'paused', 'objective': 'Preserve native goal'})
        self.api.messages = [{'id': 'ack', 'seq': 2, 'createdAt': (time.time()-1)*1000, 'content': {'role': 'agent', 'content': {'type': 'output', 'data': {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': QUOTE}]}}}}}]
        self.request = {'op': 'resource_handoff', 'project': self.source, 'to_project': self.target, 'resource': GPU,
                        'acknowledgements': [{'worker': 'w', 'session': 's', 'native_id': 'native', 'message': 'ack', 'quote': QUOTE}],
                        'user_requested': True, 'basis': 'User now requests GPU handoff from terrain to drift', 'requested_at': time.time()}
        self.free = {'resource': GPU, 'busy': False, 'processes': [], 'observedAt': time.time()}
        self.probe = patch('resource_handoff.gpu_snapshot', return_value=self.free).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.s.db.close(); self.tmp.cleanup()

    def call(self, **changes):
        return self.s.dispatch({**self.request, **changes})

    def owner(self):
        return self.s.one('SELECT project FROM resources WHERE name=?', (GPU,))['project']

    def test_personal_reservation_blocks_claim_release_handoff_without_touching_work(self):
        before = {table: self.s.rows('SELECT * FROM ' + table) for table in ('workers', 'projects', 'assignments', 'reminders', 'usage_recovery', 'outbox', 'events')}
        runtime = copy.deepcopy(self.api.session)
        request = {'op': 'resource_reservation', 'resource': GPU, 'reserved': True, 'user_requested': True,
                   'basis': 'User personally reserves main 3090 Ti; no jobs or transfers', 'requested_at': time.time()}
        result = self.s.dispatch(request)
        self.assertEqual(result['owner'], 'user/personal'); self.assertEqual(result['previousProject'], self.source)
        self.assertIsNone(self.owner())
        for table, value in before.items(): self.assertEqual(self.s.rows('SELECT * FROM ' + table), value)
        self.assertEqual(self.api.session, runtime); self.assertEqual(self.api.sent, [])
        self.s.db.close(); self.s = Supervisor(self.tmp.name + '/db', self.api)
        for op in ('claim', 'release'):
            with self.assertRaisesRegex(ValueError, 'personally reserved'):
                self.s.dispatch({'op': op, 'resource': GPU, 'project': self.source})
        with self.assertRaisesRegex(ValueError, 'personally reserved'): self.call(commit=True)
        for changes in ({'user_requested': False}, {'basis': ''}, {'requested_at': time.time()-301}):
            with self.assertRaisesRegex(ValueError, 'current explicit'):
                self.s.dispatch({**request, 'reserved': False, **changes})
        self.assertTrue(self.s.dispatch({'op': 'resource_reservation', 'resource': GPU})['reserved'])
        self.s.dispatch({**request, 'reserved': False, 'basis': 'User explicitly returns GPU to availability', 'requested_at': time.time()})
        self.assertEqual(self.s.rows('SELECT * FROM resources WHERE name=?', (GPU,)), [])
        self.assertEqual(self.api.sent, [])

    def test_heartbeat_changes_do_not_block_handoff_but_work_changes_do(self):
        original = self.api.call
        reads = 0
        change = {'activeAt': 999, 'thinkingAt': 999}
        def call(path, data=None, method=None):
            nonlocal reads
            result = copy.deepcopy(original(path, data, method))
            if path == '/sessions/s':
                reads += 1
                if reads > 1: result['session'].update(change)
            return result
        self.api.call = call
        self.assertTrue(self.call(commit=True)['committed'])
        self.s.db.execute('UPDATE resources SET project=? WHERE name=?', (self.source, GPU))
        for change in ({'thinking': True}, {'backgroundTaskCount': 1}, {'manualPausedAt': 456}):
            reads = 0
            with self.assertRaisesRegex(ValueError, 'Worker changed'): self.call(commit=True)
            self.assertEqual(self.owner(), self.source)

    def test_fresh_release_can_scroll_out_without_new_worker_prompt_but_newer_instruction_still_blocks(self):
        original=self.api.call
        release=copy.deepcopy(self.api.messages[0]);newer=[]
        def call(path,data=None,method=None):
            if path=='/sessions/s/messages?limit=100':return {'messages':newer}
            if path=='/sessions/s/messages?limit=200':return {'messages':newer+[release],'page':{'epoch':10,'hasMore':False}}
            return original(path,data,method)
        self.api.call=call
        self.assertTrue(self.call(commit=True)['committed'])
        self.s.db.execute('UPDATE resources SET project=? WHERE name=?',(self.source,GPU))
        newer.append({'id':'new-user','seq':1000,'localId':'new-user','createdAt':time.time()*1000,'invokedAt':time.time()*1000,
                      'content':{'role':'user','content':{'type':'text','text':'Use main GPU again'}}})
        self.assertFalse(self.call()['ready']);self.assertEqual(self.owner(),self.source);self.assertEqual(self.api.sent,[])

    def test_review_is_read_only_and_old_goal_does_not_block(self):
        with self.assertRaisesRegex(ValueError, 'current assignments'):
            self.s.dispatch({'op': 'release', 'project': self.source, 'resource': GPU})
        result = self.call()
        self.assertTrue(result['ready']); self.assertFalse(result['committed'])
        self.assertEqual(self.owner(), self.source); self.assertIsNone(self.s.get('resource-handoff:' + GPU))
        self.assertEqual(self.api.sent, [])

    def test_atomic_transfer_preserves_goals_pauses_management_and_assignments(self):
        before = {table: self.s.rows('SELECT * FROM ' + table) for table in ('workers', 'projects', 'assignments', 'reminders', 'usage_recovery', 'outbox', 'events')}
        runtime = copy.deepcopy(self.api.session)
        self.s.set('manager', 's')  # Even a paused manager is never resumed by a lease change.
        self.assertTrue(self.call(commit=True)['committed'])
        self.assertEqual(self.owner(), self.target)
        for table, value in before.items(): self.assertEqual(self.s.rows('SELECT * FROM ' + table), value)
        self.assertEqual(self.api.session, runtime); self.assertEqual(self.api.sent, [])
        self.assertTrue(json.loads(self.s.get('resource-handoff:' + GPU))['committed'])
        self.s.db.close(); self.s = Supervisor(self.tmp.name + '/db', self.api)
        self.assertEqual(self.owner(), self.target)
        with self.assertRaisesRegex(ValueError, 'owner changed'): self.call(commit=True)

    def test_busy_elsewhere_can_relinquish_specific_gpu(self):
        self.api.session.update(thinking=True, backgroundTaskCount=1)
        self.assertTrue(self.call(commit=True)['committed'])
        self.assertTrue(self.api.session['thinking']); self.assertEqual(self.api.sent, [])

    def test_gpu_busy_blocks_even_with_valid_release_reply(self):
        self.probe.return_value = {**self.free, 'busy': True, 'processes': [{'pid': 999, 'name': 'blender'}]}
        r = self.call(commit=True)
        self.assertFalse(r['committed']); self.assertFalse(r['ready']); self.assertEqual(self.owner(), self.source)

    def test_fresh_gpu_probe_catches_raced_job(self):
        self.probe.side_effect = [self.free, {**self.free, 'busy': True}]
        with self.assertRaisesRegex(ValueError, 'became busy'): self.call(commit=True)
        self.assertEqual(self.owner(), self.source)

    def test_no_ack_or_wrong_binding_or_quote_fails(self):
        for changes in ({'acknowledgements': []}, {'acknowledgements': [{**self.request['acknowledgements'][0], 'native_id': 'other'}]}, {'acknowledgements': [{**self.request['acknowledgements'][0], 'quote': 'An invented release statement'}]}):
            self.assertFalse(self.call(commit=True, **changes)['committed'])
        self.assertEqual(self.owner(), self.source)

    def test_stale_ack_or_newer_instruction_fails(self):
        self.api.messages[0]['createdAt'] -= 1801*1000
        self.assertFalse(self.call()['ready'])
        self.api.messages[0]['createdAt'] = (time.time()-1)*1000
        self.api.messages.append({'id': 'new', 'seq': 3, 'createdAt': time.time()*1000, 'content': {'role': 'user', 'content': {'text': 'Use the main GPU again'}}})
        self.assertFalse(self.call()['ready'])

    def test_peer_notice_after_release_is_not_a_new_human_handoff_instruction(self):
        self.api.messages.append({'id': 'peer', 'seq': 3, 'createdAt': time.time()*1000,
            'content': {'role': 'user', 'content': {'text': 'Manager is coordinating the existing handoff'},
                        'meta': {'source': 'peer', 'sourceSessionId': 'other-worker'}}})
        self.assertTrue(self.call()['ready'])
        # Missing provenance remains conservative; message text cannot bypass it.
        self.api.messages[-1]['content']['meta'].pop('sourceSessionId')
        self.assertFalse(self.call()['ready'])

    def test_queued_peer_input_still_blocks_transfer_until_proven_consumed(self):
        peer = {'id': 'peer', 'seq': 3, 'createdAt': time.time()*1000, 'localId': 'peer-local', 'invokedAt': None,
            'content': {'role': 'user', 'content': {'text': 'Existing handoff progress'},
                        'meta': {'source': 'peer', 'sourceSessionId': 'other-worker'}}}
        self.api.messages.append(peer)
        result = self.call()
        self.assertFalse(result['ready'])
        self.assertIn('Pending input', str(result))
        self.assertEqual(self.owner(), self.source)
        peer['invokedAt'] = time.time()*1000
        self.assertTrue(self.call()['ready'])
        self.assertEqual(self.owner(), self.source)  # Dry-run never changes a lease.

    def test_known_manager_assignment_still_blocks_handoff_until_consumed_and_released(self):
        from coordination import watch_messages
        note = {'id':'coordination','seq':1,'createdAt':(time.time()-3)*1000,
                'localId':'manager-note','invokedAt':None,
                'content':{'role':'user','content':{'text':'Drift still gets GPU next.'}}}
        self.s.db.execute('INSERT INTO assignments(id,worker,task_key,text,state,created,goal) VALUES(?,?,?,?,?,?,0)',
                          ('manager-note','w','coordination','Drift still gets GPU next.','sent',time.time()-3))
        self.api.messages.insert(0,note)
        self.assertNotIn(note,watch_messages(self.s,self.s.worker('w'),self.api.messages))
        result = self.call(commit=True)
        self.assertFalse(result['committed']); self.assertIn('Pending input',str(result))
        self.assertEqual(self.owner(),self.source)
        note['invokedAt'] = (time.time()-2)*1000
        self.api.invoked.append('manager-note')
        self.assertTrue(self.call()['ready'])
        self.assertEqual(self.owner(),self.source)
        self.assertEqual(self.api.sent,[])

    def test_uncertain_and_unconfirmed_assignments_fail(self):
        for state in ('sending', 'uncertain'):
            self.s.db.execute('UPDATE assignments SET state=?', (state,))
            self.assertFalse(self.call()['ready'])
        self.s.db.execute("UPDATE assignments SET state='sent'")
        self.api.invoked = []
        self.assertFalse(self.call()['ready'])

    def test_ack_must_follow_assignment_invocation(self):
        original = self.api.call
        def call(path, data=None, method=None):
            if path.endswith('/queued-state'):
                return {'queuedLocalIds': [], 'indeterminateLocalIds': [], 'invokedLocalMessages': [{'localId': 'a', 'invokedAt': time.time()*1000}]}
            return original(path, data, method)
        self.api.call = call
        self.assertFalse(self.call()['ready'])

    def test_queued_and_indeterminate_delivery_fail_closed(self):
        original = self.api.call
        for q in ({}, {'queuedLocalIds': ['a'], 'indeterminateLocalIds': []}, {'queuedLocalIds': [], 'indeterminateLocalIds': ['a']}):
            self.api.call = lambda path, data=None, method=None: q if path.endswith('/queued-state') else original(path, data, method)
            self.assertFalse(self.call()['ready'])

    def test_pending_native_ready_messages_fail(self):
        self.api.session['agentState'] = {'claudeReadyMessages': [{'id': 'pending'}]}
        self.assertFalse(self.call()['ready'])

    def test_all_source_workers_need_evidence(self):
        self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)', ('w2', self.source, 'claude', 'native', 's', self.tmp.name, 'managed', time.time()))
        self.assertFalse(self.call()['ready'])

    def test_worker_change_during_commit_fails(self):
        original = self.api.call; count = 0
        def call(path, data=None, method=None):
            nonlocal count
            result = copy.deepcopy(original(path, data, method))
            if path == '/sessions/s':
                count += 1
                if count == 2: result['session']['metadata']['claudeSessionId'] = 'other'
            return result
        self.api.call = call
        with self.assertRaisesRegex(ValueError, 'changed during review'): self.call(commit=True)
        self.assertEqual(self.owner(), self.source)

    def test_message_change_during_commit_fails(self):
        original = self.api.call; count = 0
        def call(path, data=None, method=None):
            nonlocal count
            result = copy.deepcopy(original(path, data, method))
            if '/messages?' in path:
                count += 1
                if count == 2: result['messages'].append({'id': 'newer'})
            return result
        self.api.call = call
        with self.assertRaisesRegex(ValueError, 'changed during review'): self.call(commit=True)
        self.assertEqual(self.owner(), self.source)

    def test_current_authorization_required(self):
        for changes in ({'user_requested': False}, {'basis': ''}, {'requested_at': 1}, {'requested_at': float('nan')}):
            with self.assertRaisesRegex(ValueError, 'current user authorization'): self.call(commit=True, **changes)
        self.assertEqual(self.owner(), self.source)

    def test_probe_failure_changes_nothing(self):
        self.probe.side_effect = subprocess.TimeoutExpired('nvidia-smi', 10)
        with self.assertRaises(subprocess.TimeoutExpired): self.call(commit=True)
        self.assertEqual(self.owner(), self.source)

    def test_audit_failure_rolls_back_claim(self):
        with patch.object(self.s, 'set', side_effect=RuntimeError('audit failed')):
            with self.assertRaisesRegex(RuntimeError, 'audit failed'): self.call(commit=True)
        self.assertEqual(self.owner(), self.source)

    def test_user_tool_result_and_reasoning_cannot_be_ack(self):
        good = copy.deepcopy(self.api.messages[0])
        for kind in ('user', 'reasoning', 'thinking', 'tool_result'):
            self.api.messages[0] = copy.deepcopy(good)
            self.api.messages[0]['content']['content']['data']['type'] = kind
            self.assertFalse(self.call()['ready'])
        self.api.messages[0] = copy.deepcopy(good)
        self.api.messages[0]['content']['role'] = 'user'
        self.assertFalse(self.call()['ready'])

    def incidental(self):
        identity = {'pid': 123, 'bootId': 'boot', 'startTicks': '456', 'executable': '/opt/google/chrome/chrome',
                    'executableInode': 3, 'commandHash': 'hash', 'parentStartTicks': '100'}
        process = {'pid': 123, 'name': '/opt/google/chrome/chrome', 'desktop': False, 'incidentalIdentity': identity}
        approval = {'pid': 123, 'resource': GPU, 'identity': identity, 'purpose': 'Existing PC flip browser GPU context; preserve tabs'}
        return process, approval

    def test_reviewed_chrome_allows_explicit_sharing_but_never_reports_gpu_free(self):
        process, approval = self.incidental()
        self.probe.return_value = {**self.free, 'busy': True, 'processes': [process]}
        before = {t: self.s.rows('SELECT * FROM '+t) for t in ('workers','projects','assignments','events','reminders','outbox','usage_recovery')}
        runtime = copy.deepcopy(self.api.session)
        report = self.call(incidental_clients=[approval])
        self.assertTrue(report['ready']); self.assertFalse(report['committed']); self.assertTrue(report['gpu']['busy'])
        self.assertFalse(report['gpu']['handoffBlocked']); self.assertEqual(self.owner(), self.source)
        report = self.call(commit=True, incidental_clients=[approval])
        self.assertTrue(report['committed']); self.assertEqual(report['leaseMode'], 'shared-with-reviewed-incidental-clients')
        self.assertTrue(report['gpu']['busy']); self.assertEqual(self.owner(), self.target)
        self.assertEqual(self.api.session, runtime); self.assertEqual(self.api.sent, [])
        for table, rows in before.items(): self.assertEqual(self.s.rows('SELECT * FROM '+table), rows)
        audit = self.s.rows("SELECT value FROM settings WHERE key LIKE 'resource-handoff-audit:%'")
        self.assertEqual(len(audit),1); self.assertEqual(json.loads(audit[0]['value'])['gpu']['incidentalClients'][0], approval)

    def test_chrome_not_exempt_by_name_or_identity_without_explicit_review(self):
        process, approval = self.incidental()
        self.probe.return_value = {**self.free, 'busy': True, 'processes': [process]}
        self.assertFalse(self.call()['ready'])
        for change in ({'user_requested': False}, {'basis': ''}, {'requested_at': time.time()-301}):
            with self.assertRaisesRegex(ValueError, 'current explicit'):
                self.call(incidental_clients=[approval], **change)
        self.assertEqual(self.owner(),self.source)

    def test_approved_chrome_never_exempts_another_compute_client(self):
        process, approval = self.incidental()
        self.probe.return_value = {**self.free, 'busy': True, 'processes': [process, {'pid': 999, 'name': 'blender', 'desktop': False}]}
        self.assertFalse(self.call(commit=True, incidental_clients=[approval])['ready'])
        self.assertEqual(self.owner(),self.source)
        self.probe.return_value['processes'][1]['name'] = '/opt/google/chrome/chrome --type=gpu-process'
        self.assertFalse(self.call(incidental_clients=[approval])['ready'])

    def test_identity_change_pid_reuse_other_gpu_and_unknown_browser_fail_closed(self):
        process, approval = self.incidental()
        self.probe.return_value = {**self.free, 'busy': True, 'processes': [process]}
        for change in ({'resource':'main/GPU-other'}, {'identity':{**approval['identity'], 'startTicks':'reused'}}, {'identity':None}):
            with self.assertRaises(ValueError): self.call(incidental_clients=[{**approval, **change}])
        self.probe.return_value['processes'][0].pop('incidentalIdentity')
        with self.assertRaisesRegex(ValueError, 'unverified'): self.call(incidental_clients=[approval])
        self.assertEqual(self.owner(),self.source)

    def test_malformed_duplicate_or_unreviewed_client_specs_never_relax_guard(self):
        process, approval = self.incidental()
        self.probe.return_value={**self.free,'busy':True,'processes':[process]}
        for spec in (None,{},[approval,approval],[{**approval,'pid':True}],[{**approval,'pid':-1}],
                     [{**approval,'purpose':''}],[{**approval,'pid':999}]):
            with self.assertRaises(ValueError):self.call(incidental_clients=spec)
        self.assertEqual(self.owner(),self.source)

    def test_second_probe_revalidates_review_and_catches_racing_compute(self):
        process, approval = self.incidental(); first = {**self.free,'busy':True,'processes':[process]}
        for second in ({**first,'processes':[{**process,'incidentalIdentity':{**process['incidentalIdentity'],'bootId':'new'}}]},
                       {**first,'processes':[process,{'pid':9,'name':'blender','desktop':False}]}):
            self.probe.side_effect = [first,second]
            with self.assertRaises(ValueError):self.call(commit=True,incidental_clients=[approval])
            self.assertEqual(self.owner(),self.source)
        self.assertEqual(self.api.sent,[])

    def test_reviewed_browser_does_not_relax_real_queued_input_or_source_evidence(self):
        process, approval = self.incidental()
        self.probe.return_value = {**self.free,'busy':True,'processes':[process]}
        self.api.messages.append({'id':'input','localId':'real','invokedAt':None,'seq':1,'createdAt':1,
                                  'content':{'role':'user','content':{'text':'Earlier genuine instruction'}}})
        result = self.call(commit=True, incidental_clients=[approval])
        self.assertFalse(result['ready']); self.assertIn('Pending input',str(result['blockers']))
        self.assertEqual(self.owner(),self.source)

    def test_stale_page_resolved_only_by_exact_authoritative_intake_receipt(self):
        original=self.api.call; now=time.time()*1000
        notice={'id':'notice','localId':'peer','seq':1,'createdAt':now-3000,'invokedAt':None,
                'content':{'role':'user','content':{'text':'Same old coordination information'}}}
        self.api.messages.insert(0,notice)
        receipts=[{'localId':'a','invokedAt':1},{'localId':'peer','invokedAt':now-2000}]
        self.api.call=lambda path,data=None,method=None: {'queuedLocalIds':[],'indeterminateLocalIds':[],'invokedLocalMessages':receipts} if path.endswith('/queued-state') else original(path,data,method)
        report=self.call()
        self.assertTrue(report['ready']);self.assertEqual(report['acknowledgements'][0]['intakeReceipts'][0]['localId'],'peer')
        self.assertIsNone(notice['invokedAt']);self.assertEqual(self.owner(),self.source);self.assertEqual(self.api.sent,[])
        # Missing ID, duplicates, invalid timestamps, and intake AFTER release never count.
        for bad in ([],[{'localId':'other','invokedAt':now-2000}],
                    [{'localId':'peer','invokedAt':None}], [{'localId':'peer','invokedAt':float('nan')}],
                    [{'localId':'peer','invokedAt':now-4000}], [{'localId':'peer','invokedAt':now}],
                    [{'localId':'peer','invokedAt':now-2000}]*2):
            receipts[:]=[{'localId':'a','invokedAt':1}]+bad
            self.assertFalse(self.call()['ready'])
        self.assertIsNone(notice['invokedAt'])

    def test_queued_input_wins_even_when_reply_or_receipt_seems_to_match(self):
        original=self.api.call
        self.api.messages.insert(0,{'id':'notice','localId':'peer','seq':1,'createdAt':1,'invokedAt':None,
                'content':{'role':'user','content':{'text':QUOTE}}})
        self.api.call=lambda path,data=None,method=None: {'queuedLocalIds':['peer'],'indeterminateLocalIds':[],
                'invokedLocalMessages':[{'localId':'peer','invokedAt':1}]} if path.endswith('/queued-state') else original(path,data,method)
        result=self.call(commit=True)
        self.assertFalse(result['committed']);self.assertEqual(self.owner(),self.source);self.assertEqual(self.api.sent,[])

class ProbeTests(unittest.TestCase):
    def test_exact_gpu_and_compute_processes(self):
        uuid = GPU.split('/')[1]
        outputs = [uuid + ', RTX 3090 Ti\n', uuid + ', 10, /usr/bin/kwin_wayland\n' + uuid + ', 11, blender\n']
        with patch('resource_handoff.subprocess.run', side_effect=[subprocess.CompletedProcess([], 0, x) for x in outputs]), patch('resource_handoff.Path.resolve', side_effect=[Path('/usr/bin/kwin_wayland'), Path('/opt/blender/blender')]):
            result = gpu_snapshot(GPU)
            self.assertTrue(result['busy']); self.assertTrue(result['processes'][0]['desktop'])
            self.assertFalse(result['processes'][1]['desktop'])

    def test_spoofed_compositor_title_not_ignored(self):
        uuid = GPU.split('/')[1]
        with patch('resource_handoff.subprocess.run', side_effect=[subprocess.CompletedProcess([], 0, uuid+', GPU\n'), subprocess.CompletedProcess([], 0, uuid+', 10, /usr/bin/kwin_wayland\n')]), patch('resource_handoff.Path.resolve', return_value=Path('/tmp/custom-job')):
            self.assertTrue(gpu_snapshot(GPU)['busy'])

    def test_hardened_compositor_verified_by_systemd_membership(self):
        group = '/user.slice/user-1000.slice/fixture-3b1e1096ea@example.com/session.slice/plasma-kwin_wayland.service'
        with patch('resource_handoff.Path.resolve', side_effect=PermissionError()), patch('resource_handoff.subprocess.run', return_value=subprocess.CompletedProcess([], 0, group+'\n')), patch('resource_handoff.Path.read_text', side_effect=['kwin_wayland\n', '0::'+group+'\n']):
            self.assertTrue(desktop_compositor(10, '/usr/bin/kwin_wayland'))
        with patch('resource_handoff.Path.resolve', side_effect=PermissionError()), patch('resource_handoff.subprocess.run', return_value=subprocess.CompletedProcess([], 0, group+'\n')), patch('resource_handoff.Path.read_text', side_effect=['kwin_wayland\n', '0::/different.scope\n']):
            self.assertFalse(desktop_compositor(10, '/usr/bin/kwin_wayland'))

    def test_public_stream_snapshots_supported_but_live_reasoning_excluded(self):
        message = {'content': {'role': 'agent', 'content': {'type': 'codex', 'data': {'type': 'message', 'message': QUOTE, 'live': False}}}}
        self.assertEqual(public_reply(message), QUOTE)
        message['content']['content']['data']['live'] = True
        self.assertEqual(public_reply(message), '')
        message['content']['content']['data'].update(type='reasoning', live=False)
        self.assertEqual(public_reply(message), '')

    def test_unsupported_gpu_and_bad_evidence_refused(self):
        with self.assertRaisesRegex(ValueError, 'Only exact'): gpu_snapshot('render-pc/' + GPU.split('/')[1])
        for output in ('', 'GPU-other, GPU\n'):
            with patch('resource_handoff.subprocess.run', return_value=subprocess.CompletedProcess([], 0, output)):
                with self.assertRaises(ValueError): gpu_snapshot(GPU)

class ChromeIdentityTests(unittest.TestCase):
    def inspect(self, *, exe='/opt/google/chrome/chrome', title=None, owner=1000, parent_exe=None, after_start='123'):
        title = title if title is not None else '/opt/google/chrome/chrome --type=gpu-process --user-data-dir=/profile'
        # /proc stat tail: state,ppid,...,starttime at index19.
        def stat(start): return '10 (chrome) S 20 ' + '0 '*17 + start + ' 0'
        reads = iter([stat('123'),stat('50'),'boot',stat(after_start),stat('50')])
        with patch('resource_handoff.os.getuid',return_value=1000), patch('resource_handoff.Path.read_text',side_effect=lambda:next(reads)), \
             patch('resource_handoff.Path.read_bytes',return_value=title.encode()+b'\0'), \
             patch('resource_handoff.Path.resolve',side_effect=[Path(exe),Path(parent_exe or exe)]), \
             patch('resource_handoff.Path.stat') as st:
            st.return_value.st_uid=owner;st.return_value.st_dev=1;st.return_value.st_ino=2;st.return_value.st_mtime_ns=3;st.return_value.st_size=4
            return chrome_gpu_identity(10)

    def test_real_executable_gpu_child_and_parent_generation_bound(self):
        identity=self.inspect()
        self.assertEqual(identity['startTicks'],'123');self.assertEqual(identity['parentStartTicks'],'50')
        self.assertEqual(identity['executableInode'],2);self.assertEqual(identity['bootId'],'boot')
        self.assertNotIn('/profile',str(identity));self.assertEqual(len(identity['commandHash']),64)

    def test_spoofed_title_wrong_executable_owner_parent_and_raced_pid_refused(self):
        for kw in ({'exe':'/tmp/chrome'}, {'owner':2000}, {'parent_exe':'/usr/bin/python'},
                   {'title':'/opt/google/chrome/chrome --type=renderer'}, {'after_start':'124'}):
            self.assertIsNone(self.inspect(**kw))
        with patch('resource_handoff.Path.read_text',side_effect=PermissionError()):self.assertIsNone(chrome_gpu_identity(10))

    def test_normal_nul_separated_argv_supported(self):
        with patch('resource_handoff.shlex.split',side_effect=AssertionError('not a rewritten title')):
            self.assertIsNotNone(self.inspect(title='/opt/google/chrome/chrome\0--type=gpu-process'))

if __name__ == '__main__': unittest.main()
