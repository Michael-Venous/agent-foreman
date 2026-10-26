import json
import tempfile
import time
import unittest
from unittest.mock import patch
from supervisor import Supervisor
from test_supervisor import FakeHapi, IsolatedSupervisorTest
from coordination import observe, signals, waiting_snapshot, latest_instruction, instruction_changed, is_instruction_message


def reply(text, seq):
    return {'id': 'm'+str(seq), 'seq': seq, 'createdAt': seq*1000,
            'content': {'role': 'agent', 'content': {'type': 'codex', 'data': {'type': 'message', 'message': text}}}}


def prompt(seq):
    return {'id': 'u'+str(seq), 'seq': seq, 'createdAt': seq*1000,
            'content': {'role': 'user', 'content': {'text': 'Changed allocation'}}}


def peer_prompt(seq, *, echo=False):
    message = prompt(seq)
    message['content']['meta'] = {'sentFrom':'cli' if echo else 'webapp', 'source':'peer', 'sourceSessionId':'peer-session'}
    if echo:
        message['content']['meta']['isTranscriptEcho'] = True
        message['content'] = {'type':'plaintext','data':message['content']}
    return message


class CoordinationTests(IsolatedSupervisorTest):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.api = FakeHapi()
        self.s = Supervisor(self.tmp.name+'/db', self.api)
        self.p = self.s.dispatch({'op': 'register', 'name': 'Terrain', 'repo': self.tmp.name, 'objective': 'Keep goal'})['id']
        self.q = self.s.dispatch({'op': 'register', 'name': 'Drift', 'repo': self.tmp.name, 'objective': 'Existing revision'})['id']
        for p in (self.p, self.q): self.s.dispatch({'op': 'resume', 'project': p})
        self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)', ('w', self.p, 'claude', 'n', 's', self.tmp.name, 'managed', time.time()-100))
        self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)', ('waiter', self.q, 'claude', 'n2', 'wait', self.tmp.name+'/other', 'managed', time.time()-100))
        self.s.dispatch({'op': 'claim', 'project': self.p, 'resource': 'main/GPU-test'})
        self.waiter = {'id': 'wait', 'active': True, 'thinking': False, 'metadata': {}, 'agentState': {}}
        self.wait_messages = [prompt(1)]
        self.api.messages = [prompt(1), reply('Working on a render', 2)]
        original = self.api.call
        def call(path, data=None, method=None):
            if path == '/sessions/wait': return {'session': self.waiter}
            if path.startswith('/sessions/wait/messages?'): return {'messages': self.wait_messages}
            return original(path, data, method)
        self.api.call = call
        self.api.session['goal'] = {'status': 'active', 'objective': 'Preserve native goal'}

    def tearDown(self): self.s.db.close(); self.tmp.cleanup()

    def scan(self, now):
        with patch('supervisor.time.time', return_value=now):
            self.s.observe_context(self.s.worker('w'), self.api.session, self.api.messages)

    def candidates(self):
        return [c for e in self.s.rows("SELECT detail FROM events WHERE kind='observation' AND acked=0") for c in json.loads(e['detail'])['changes']]

    def watch(self):
        return self.s.dispatch({'op': 'handoff_watch', 'key': 'drift-revision', 'source_worker': 'w', 'waiting_worker': 'waiter', 'resource': 'main/GPU-test', 'user_requested': True, 'basis': 'User asks to hand GPU to existing drift revision'})

    def test_gpu_release_alerts_immediately_under_active_goal(self):
        self.scan(1000)
        self.api.messages.append(reply('All GPU jobs stopped. The 3090 Ti is free.', 3))
        self.scan(1001)
        self.assertIn('resource-release', [c.get('category') for c in self.candidates()])
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.api.session['goal']['status'], 'active')

    def test_paraphrased_waits_and_free_reports_do_not_spam_or_trigger_fallback(self):
        self.scan(1000); self.api.session['thinking'] = True
        for seq, text in enumerate(['Terrain stays paused until drift finishes. The 3090 Ti is free.', 'Terrain is still on hold. The GPU remains free.', 'I will resume when you confirm drift is done. The GPU stays free.'], 3):
            self.api.messages.append(reply(text, seq)); self.scan(1000+seq)
        categories = [c.get('category') for c in self.candidates()]
        self.assertEqual(categories.count('resource-release'), 1)
        self.assertEqual(categories.count('dependency-wait'), 1)
        self.assertEqual(categories.count('native-wait-loop'), 1)
        self.assertEqual(self.s.progress_snapshot(self.s.worker('w'))['status'], 'waiting for dependency')
        self.s.db.execute('UPDATE events SET acked=1'); self.scan(10000)
        self.assertEqual(self.candidates(), []); self.assertEqual(self.api.sent, [])
        self.s.db.close(); self.s = Supervisor(self.tmp.name+'/db', self.api); self.scan(11000)
        self.assertEqual(self.candidates(), [])

    def test_new_instruction_rearms_conditions_and_stops_old_wait_status(self):
        self.scan(1000); self.api.messages.append(reply('Terrain stays paused; GPU is free.', 3)); self.scan(1001)
        self.s.db.execute('UPDATE events SET acked=1')
        self.api.messages.append(prompt(4)); self.scan(1002)
        self.assertNotEqual(self.s.progress_snapshot(self.s.worker('w'))['status'], 'waiting for dependency')
        self.api.messages.append(reply('The GPU is free.', 5)); self.scan(1003)
        self.assertEqual([c.get('category') for c in self.candidates()], ['resource-release'])

    def test_initial_history_is_not_replayed(self):
        self.api.messages.append(reply('Terrain stays paused. GPU is free.', 3)); self.scan(1000); self.scan(1001)
        self.assertEqual(self.candidates(), [])

    def test_real_tool_after_wait_is_working(self):
        self.api.session['thinking'] = True
        self.api.messages += [reply('Waiting for GPU handoff.', 3), {'id': 'tool', 'seq': 4, 'content': {'role': 'agent', 'content': {'type': 'codex', 'data': {'type': 'tool-call', 'name': 'Bash'}}}}]
        self.assertEqual(self.s.progress_snapshot(self.s.worker('w'))['status'], 'running')

    def test_waiting_for_own_background_render_is_real_work(self):
        self.api.session.update(thinking=False, backgroundTaskCount=1)
        self.api.messages.append(reply('Waiting for the GPU render to finish.', 3))
        self.assertEqual(self.s.progress_snapshot(self.s.worker('w'))['status'], 'background')

    def test_busy_or_manually_paused_manager_is_not_interrupted(self):
        self.s.set('manager', 's'); self.scan(1000)
        self.api.messages.append(reply('GPU is free.', 3)); self.scan(1001)
        self.api.session['thinking'] = True; self.s.notify_manager()
        self.assertEqual(self.api.sent, [])
        self.api.session.update(thinking=False, manualPausedAt=123); self.s.notify_manager()
        self.assertEqual(self.api.sent, [])
        self.api.session.pop('manualPausedAt'); self.s.notify_manager()
        self.assertEqual(len(self.api.sent), 1)
        self.s.notify_manager(); self.assertEqual(len(self.api.sent), 1)

    def test_wait_watch_links_release_to_existing_task_once(self):
        record = self.watch(); self.assertEqual(record, self.watch())
        self.scan(1000); self.api.messages.append(reply('The GPU is free.', 3)); self.scan(1001)
        linked = [c for c in self.candidates() if c.get('category') == 'handoff-review']
        self.assertEqual(len(linked), 1); self.assertEqual(linked[0]['waiting_worker'], 'waiter')
        self.assertEqual(json.loads(self.s.get('handoff-watch:drift-revision'))['state'], 'review-required')
        self.assertEqual(self.api.sent, []); self.assertEqual(self.s.rows('SELECT * FROM assignments'), [])
        self.assertEqual(self.s.one('SELECT project FROM resources')['project'], self.p)

    def test_new_source_instruction_cancels_old_watch_without_dispatch(self):
        self.watch(); self.scan(1000)
        self.api.messages += [prompt(3), reply('GPU is free for personal use.', 4)]; self.scan(1001)
        self.assertEqual(json.loads(self.s.get('handoff-watch:drift-revision'))['state'], 'cancelled')
        self.assertNotIn('handoff-review', [c.get('category') for c in self.candidates()])

    def manager_note(self, seq, worker='w', state='sent'):
        message = prompt(seq)
        message['localId'] = 'manager-note-' + str(seq)
        message['content']['content']['text'] = 'Queue update: drift still gets GPU next; preserve current scope.'
        self.s.db.execute('INSERT INTO assignments(id,worker,task_key,text,state,created,goal) VALUES(?,?,?,?,?,?,0)',
                          (message['localId'], worker, message['localId'], message['content']['content']['text'], state, time.time()))
        return message

    def test_manager_coordination_after_registration_survives_and_links_release_once(self):
        self.watch(); self.scan(1000)
        self.api.messages.append(self.manager_note(3))
        self.wait_messages.append(self.manager_note(4, 'waiter'))
        self.scan(1001)
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'], 'waiting')
        # Reload and an echo of the same delivery must not change provenance.
        self.s.db.close(); self.s = Supervisor(self.tmp.name+'/db', self.api)
        echo = dict(self.api.messages[-1], id='echo', seq=5)
        self.api.messages.extend([echo, reply('The GPU is free.', 6)])
        self.scan(1002); self.scan(1003)
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'], 'review-required')
        self.assertEqual(len([c for c in self.candidates() if c.get('category')=='handoff-review']), 1)
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.s.one('SELECT project FROM resources')['project'], self.p)
        self.assertEqual(self.api.session['goal']['status'], 'active')
        self.assertEqual(self.s.one('SELECT count(*) AS n FROM assignments')['n'], 2)

    def test_manager_assignment_origin_survives_delivery_state_changes(self):
        for state in ('sending', 'sent', 'uncertain', 'finished', 'failed', 'cancelled'):
            with self.subTest(state=state):
                self.s.db.execute("DELETE FROM settings WHERE key='handoff-watch:drift-revision'")
                self.api.messages = [prompt(1), reply('Working', 2)]
                self.watch()
                note = self.manager_note(10 + len(self.s.rows('SELECT * FROM assignments')), state=state)
                self.api.messages.append(note); self.scan(1000)
                self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'], 'waiting')
                # Watch exclusion must not erase ordinary instruction/queue semantics.
                self.assertTrue(is_instruction_message(note))
                self.assertEqual(latest_instruction(self.api.messages)['id'], note['id'])

    def test_supported_assign_after_watch_preserves_dependency_and_assignment(self):
        self.watch()
        assignment = self.s.dispatch({'op':'assign','worker':'w','key':'coordination-note',
                                     'text':'Queue update: drift remains next; keep current scope.', 'user_requested':True})
        payload = self.api.sent[-1]
        message = dict(prompt(3), localId=payload['localId'], invokedAt=None)
        message['content']['content']['text'] = payload['text']
        self.api.messages.append(message); self.scan(1000)
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'], 'waiting')
        self.assertEqual(payload['localId'], assignment['id'])
        self.assertEqual(self.s.one('SELECT state FROM assignments')['state'], 'sent')
        self.assertTrue(is_instruction_message(message))
        self.assertIsNone(message['invokedAt'])

    def test_later_manager_note_does_not_mask_human_steer_on_either_participant(self):
        for participant in ('source', 'waiting'):
            with self.subTest(participant=participant):
                self.s.db.execute("DELETE FROM settings WHERE key='handoff-watch:drift-revision'")
                self.api.messages = [prompt(1), reply('Working', 2)]; self.wait_messages = [prompt(1)]
                self.watch()
                messages = self.api.messages if participant=='source' else self.wait_messages
                messages.extend([prompt(30), self.manager_note(31 if participant=='source' else 32, 'w' if participant=='source' else 'waiter')])
                self.scan(1001)
                record = self.s.dispatch({'op':'handoff_watches'})[0]
                self.assertEqual(record['state'], 'cancelled')
                self.assertEqual(record['reason'], 'Participant user instruction changed: u30')
        self.assertEqual(self.api.sent, [])

    def test_foreign_assignment_and_unrecognized_local_id_still_cancel(self):
        for foreign in (True, False):
            with self.subTest(foreign=foreign):
                self.s.db.execute("DELETE FROM settings WHERE key='handoff-watch:drift-revision'")
                self.api.messages = [prompt(1), reply('Working', 2)]; self.watch()
                message = self.manager_note(40, 'waiter') if foreign else dict(prompt(41), localId='unrecognized')
                self.api.messages.append(message); self.scan(1000)
                self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'], 'cancelled')

    def test_coordination_does_not_defeat_stop_release_or_explicit_cancellation(self):
        for change in ('pause', 'release', 'binding', 'cancel'):
            with self.subTest(change=change):
                self.s.db.execute("DELETE FROM settings WHERE key='handoff-watch:drift-revision'")
                self.api.messages = [prompt(1), reply('Working', 2)]
                self.api.session.pop('manualPausedAt', None)
                self.s.db.execute("UPDATE workers SET state='managed',session='s' WHERE id='w'")
                self.watch(); self.api.messages.append(self.manager_note(50 + len(self.s.rows('SELECT * FROM assignments'))))
                if change=='pause': self.api.session['manualPausedAt'] = 123
                if change=='release': self.s.db.execute("UPDATE workers SET state='released' WHERE id='w'")
                if change=='binding': self.s.db.execute("UPDATE workers SET session='other' WHERE id='w'")
                if change=='cancel': self.s.dispatch({'op':'handoff_watch','key':'drift-revision','cancel':True})
                from coordination import update_watches
                update_watches(self.s, self.s.worker('w'), [], self.api.messages, self.api.session)
                self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'], 'cancelled')
        self.assertEqual(self.api.sent, [])

    def test_new_waiter_instruction_and_manual_pause_cancel_watch(self):
        for change in ('new-input', 'pause', 'release'):
            with self.subTest(change=change):
                self.s.db.execute("DELETE FROM settings WHERE key='handoff-watch:drift-revision'")
                self.wait_messages = [prompt(1)]; self.waiter.pop('manualPausedAt', None)
                self.s.db.execute("UPDATE workers SET state='managed' WHERE id='waiter'")
                self.watch()
                if change == 'new-input': self.wait_messages.append(prompt(4))
                if change == 'pause': self.waiter['manualPausedAt'] = 123
                if change == 'release': self.s.db.execute("UPDATE workers SET state='released' WHERE id='waiter'")
                from coordination import update_watches
                result = update_watches(self.s, self.s.worker('w'), [{'category': 'resource-release', 'message': 'm3'}], self.api.messages, self.api.session)
                self.assertEqual(result, []); self.assertEqual(json.loads(self.s.get('handoff-watch:drift-revision'))['state'], 'cancelled')
        self.assertEqual(self.api.sent, [])

    def test_paused_source_suppresses_events_and_cancels_watch(self):
        self.watch(); self.scan(1000); self.s.db.execute("UPDATE projects SET status='paused' WHERE id=?", (self.p,))
        self.api.messages.append(reply('GPU is free.', 3)); self.scan(1001)
        self.assertEqual(self.candidates(), []); self.assertEqual(json.loads(self.s.get('handoff-watch:drift-revision'))['state'], 'cancelled')

    def test_watch_cancel_and_registration_are_durable(self):
        self.watch(); self.s.db.close(); self.s = Supervisor(self.tmp.name+'/db', self.api)
        self.assertEqual(self.watch()['state'], 'waiting')
        self.s.dispatch({'op': 'handoff_watch', 'key': 'drift-revision', 'cancel': True})
        self.assertEqual(self.watch()['state'], 'cancelled'); self.assertEqual(self.api.sent, [])

    def test_successful_resource_transfer_marks_watch_without_work_completion(self):
        self.watch()
        from coordination import complete_watches
        complete_watches(self.s, self.p, self.q, 'main/GPU-test')
        self.assertEqual(self.s.dispatch({'op': 'handoff_watches'})[0]['state'], 'transferred')
        self.assertEqual(self.api.session['goal']['status'], 'active'); self.assertEqual(self.api.sent, [])

    def test_changed_resource_owner_supersedes_watch_without_a_release_message(self):
        self.watch(); self.s.db.execute('UPDATE resources SET project=?', (self.q,))
        self.scan(1000)
        self.assertEqual(self.s.dispatch({'op': 'handoff_watches'})[0]['state'], 'superseded')
        self.assertEqual(self.candidates(), [])

    def test_peer_pings_and_provenance_preserving_echoes_keep_the_watch_and_do_not_replay_work(self):
        self.watch(); self.scan(1000)
        self.api.messages.extend([peer_prompt(3),peer_prompt(4,echo=True)])
        self.wait_messages.extend([peer_prompt(5),peer_prompt(6,echo=True)])
        self.scan(1001)
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'waiting')
        self.api.messages.append(reply('The GPU is free.',7)); self.scan(1002); self.scan(1003)
        linked = [c for c in self.candidates() if c.get('category')=='handoff-review']
        self.assertEqual(len(linked),1)
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'review-required')
        self.assertEqual(self.api.sent,[])
        self.assertEqual(self.s.rows('SELECT * FROM assignments'),[])
        self.assertEqual(self.s.one('SELECT project FROM resources')['project'],self.p)
        self.assertEqual(self.api.session['goal']['status'],'active')

    def test_peer_pings_do_not_reset_release_deduplication_or_waiting_status(self):
        self.scan(1000)
        self.api.messages.append(reply('Terrain stays paused until drift finishes. GPU is free.',3)); self.scan(1001)
        self.api.messages.extend([peer_prompt(4),peer_prompt(5,echo=True)])
        self.assertTrue(waiting_snapshot(self.api.messages))
        self.api.messages.append(reply('The GPU remains free.',6)); self.scan(1002)
        self.assertEqual([c.get('category') for c in self.candidates()].count('resource-release'),1)
        self.assertEqual(latest_instruction(self.api.messages)['id'],'u1')

    def test_peer_only_paginated_window_does_not_invalidate_old_watch_watermarks(self):
        messages = [peer_prompt(100),peer_prompt(101,echo=True)]
        self.assertIsNone(latest_instruction(messages))
        self.assertFalse(instruction_changed({'sourceAfterSeq':2},'source',messages))
        self.assertFalse(instruction_changed({'sourceUser':'original'},'source',messages))

    def test_human_input_is_preserved_even_when_its_text_looks_like_peer_output(self):
        self.watch(); self.scan(1000)
        human = prompt(3)
        human['content']['text'] = '[peer update] Please keep the GPU for my personal use.'
        human['content']['meta'] = {'sentFrom':'webapp'}
        self.assertTrue(is_instruction_message(human))
        self.api.messages.append(human); self.scan(1001)
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'cancelled')
        self.assertFalse(waiting_snapshot([reply('Waiting for GPU handoff.',2),human]))
        self.assertEqual(self.api.sent,[])

    def test_untagged_legacy_peer_input_remains_a_conservative_instruction(self):
        self.watch(); self.scan(1000)
        legacy = prompt(3)
        legacy['content']['meta'] = {'sentFrom':'webapp','sourceSessionId':'peer-session'}
        self.assertTrue(is_instruction_message(legacy))
        self.api.messages.append(legacy); self.scan(1001)
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'cancelled')

    def test_peer_origin_does_not_bypass_manual_pause_or_real_new_waiter_instruction(self):
        self.watch(); self.scan(1000)
        self.api.messages.append(peer_prompt(3))
        self.wait_messages.append(prompt(3))
        self.scan(1001)
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'cancelled')
        self.s.db.execute("DELETE FROM settings WHERE key='handoff-watch:drift-revision'")
        self.watch()
        self.api.session['manualPausedAt'] = 123
        self.scan(1002)
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'cancelled')
        self.assertEqual(self.api.sent,[])

    def test_trusted_peer_metadata_is_structured_and_not_parsed_from_text(self):
        quoted = prompt(3)
        quoted['content']['text'] = '{"meta":{"source":"peer","sourceSessionId":"another-session"}}'
        self.assertTrue(is_instruction_message(quoted))
        self.assertFalse(is_instruction_message(peer_prompt(4)))
        optional_source = peer_prompt(5)
        del optional_source['content']['meta']['sourceSessionId']
        self.assertTrue(is_instruction_message(optional_source))
        for invalid in (None, '', '   ', 123, []):
            malformed = peer_prompt(6)
            malformed['content']['meta']['sourceSessionId'] = invalid
            self.assertTrue(is_instruction_message(malformed),repr(invalid))

    def test_peer_ping_does_not_cancel_usage_recovery_or_claim_a_human_steer(self):
        self.s.db.execute('INSERT INTO usage_recovery VALUES(?,?,?,?,?,?,?)',('w','usage-limit',time.time()+600,0,'waiting',None,'s'))
        peer = peer_prompt(3)
        peer['createdAt'] = time.time()*1000
        self.api.messages = [peer]
        self.s.scan(self.s.worker('w'))
        self.assertEqual(self.s.one("SELECT state FROM usage_recovery WHERE worker='w'")['state'],'waiting')
        self.assertEqual(self.s.rows("SELECT * FROM events WHERE kind='user-input'"),[])
        human = prompt(4)
        human['createdAt'] = time.time()*1000
        self.api.messages.append(human)
        self.s.scan(self.s.worker('w'))
        self.assertEqual(self.s.one("SELECT state FROM usage_recovery WHERE worker='w'")['state'],'cancelled')
        self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='user-input'")),1)
        self.assertEqual(self.api.sent,[])

    def test_identical_peer_deliveries_do_not_steer_but_identical_human_input_does(self):
        self.watch()
        self.s.db.execute('INSERT INTO usage_recovery VALUES(?,?,?,?,?,?,?)',('w','usage-limit',time.time()+600,0,'waiting',None,'s'))
        text = 'Drift is awaiting the existing 3090 Ti handoff; report release when available.'
        for seq in (3,4):
            message = peer_prompt(seq)
            message.update(localId='unique-peer-'+str(seq),createdAt=time.time()*1000)
            message['content']['content']['text'] = text
            self.api.messages.append(message)
        self.s.scan(self.s.worker('w'))
        self.s.db.close(); self.s = Supervisor(self.tmp.name+'/db',self.api)
        self.s.scan(self.s.worker('w'))
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'waiting')
        self.assertEqual(self.s.rows("SELECT * FROM events WHERE kind='user-input'"),[])
        self.assertEqual(self.s.one("SELECT state FROM usage_recovery WHERE worker='w'")['state'],'waiting')
        human = prompt(5)
        human.update(localId='human-message',createdAt=time.time()*1000)
        human['content']['content']['text'] = text
        human['content']['meta'] = {'sentFrom':'webapp'}
        self.api.messages.append(human); self.s.scan(self.s.worker('w'))
        self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'cancelled')
        self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='user-input'")),1)
        self.assertEqual(self.s.one("SELECT state FROM usage_recovery WHERE worker='w'")['state'],'cancelled')
        self.assertEqual(self.api.sent,[])
        self.assertEqual(self.api.session['goal']['status'],'active')

    def test_queued_peer_work_still_blocks_idle_retirement_without_changing_instruction_epoch(self):
        done = {'id':'done','seq':2,'createdAt':time.time()*1000,'content':{'role':'agent','content':{'type':'codex','data':{'type':'task_complete','last_agent_message':'Finished the requested work.'}}}}
        peer = peer_prompt(3)
        peer.update(localId='pending-peer',invokedAt=None,createdAt=time.time()*1000)
        self.api.messages = [prompt(1),done,peer]
        self.api.session['goal'] = {'status':'complete'}
        original = self.api.call
        def call(path,data=None,method=None):
            if path.endswith('/queued-state'):
                return {'queuedLocalIds':['pending-peer'],'indeterminateLocalIds':[],'invokedLocalMessages':[]}
            return original(path,data,method)
        self.api.call = call
        self.s.set('retirement-candidate:w',json.dumps({'message':'done','at':time.time()-10,'notices':[]}))
        self.s.retire_finished(self.s.worker('w'),self.api.session,self.api.messages)
        self.assertEqual(self.s.worker('w')['state'],'managed')
        self.assertEqual(latest_instruction(self.api.messages)['id'],'u1')
        self.assertEqual(self.api.sent,[])

    def test_paused_native_goal_does_not_finish_assignment(self):
        self.s.db.execute('INSERT INTO assignments(id,worker,task_key,text,state,created,seen_busy,goal) VALUES(?,?,?,?,?,?,?,?)', ('a', 'w', 'g', '/goal keep it', 'sent', 1, 1, 1))
        self.api.invoked = ['a']; self.api.session['metadata'] = {'claudeSessionId': 'n'}
        with patch.object(self.s, 'goal_state', return_value='paused'):
            self.s.scan(self.s.worker('w'))
        self.assertEqual(self.s.one("SELECT state FROM assignments WHERE id='a'")['state'], 'sent')
        self.assertEqual(self.api.sent, [])

    def test_signal_detection_does_not_treat_future_release_as_done(self):
        for text in ['GPU is not free.', 'Once the GPU is free I can start.', 'When the 3090 Ti is free, resume.', 'If GPU is free, run.']:
            self.assertFalse(signals(text)['resource-release'], text)
        self.assertFalse(waiting_snapshot([reply('Waiting for GPU handoff.', 1), prompt(2)]))

if __name__ == '__main__': unittest.main()
