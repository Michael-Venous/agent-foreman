"""All queue evidence/RPC calls isolated; never reads or changes live HAPI."""
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from assignment_delivery import describe_message, summarize_inputs, inspect_assignment, promote_assignment, validate_priority
from supervisor import Supervisor
from test_supervisor import FakeHapi, IsolatedSupervisorTest


def user(local='a', *, invoked=None, mode='queue', text='Correct scope', receipt=None, **extra):
    meta = {'deliveryMode': mode}
    if receipt is not None:
        meta['deliveryReceipt'] = receipt
    return {'id': 'message-' + local, 'localId': local, 'seq': 1, 'createdAt': 1000,
            'invokedAt': invoked, 'content': {'role': 'user', 'meta': meta, 'content': {'type': 'text', 'text': text}}, **extra}


def ack(local='a', **extra):
    return {'id': 'goal-ack', 'seq': 2, 'createdAt': 2000, 'content': {'role': 'agent', 'content': {'type': 'event', 'data': {'type': 'goal-command-ack', 'localId': local, 'action': 'set', 'goalStatus': 'blocked', 'appliedToCurrentTurn': False, **extra}}}}


class Descriptions(unittest.TestCase):
    def test_consumption_and_intent_never_claim_application(self):
        self.assertEqual(describe_message(user(), now=31)['state'], 'queued')
        self.assertEqual(describe_message(user(), now=31)['waitSeconds'], 30)
        self.assertNotIn('waitSeconds', describe_message(user(), include_wait=False))
        self.assertEqual(describe_message(user(mode='steer', invoked=2000))['state'], 'consumed')
        self.assertEqual(describe_message(user(mode='steer', invoked=2000))['compliance'], 'unverified')
        self.assertEqual(describe_message(user(invoked=2000, receipt={'consumedAt': 2000, 'steered': True}))['state'], 'steered')
        self.assertEqual(describe_message(user(steered=True, invoked=2000))['state'], 'consumed')
        self.assertEqual(describe_message(user(deliveryState='indeterminate'))['state'], 'indeterminate')
        self.assertEqual(describe_message(user(status='cancelled'))['state'], 'cancelled')
        legacy = user(); legacy.pop('invokedAt')
        self.assertEqual(describe_message(legacy)['state'], 'unknown')

    def test_goal_ack_is_control_consumed_not_saved_or_current_turn_applied(self):
        goal = describe_message(user(invoked=2000, mode='steer', text='/goal Changed objective', receipt={'consumedAt': 2000, 'steered': True}))
        self.assertEqual(goal['state'], 'control-consumed'); self.assertFalse(goal['appliedToCurrentTurn'])
        self.assertEqual(describe_message(user(text='STOP after this iteration', invoked=2000))['state'], 'consumed')
        self.assertEqual(describe_message(user(text='/custom expanded to prompt', invoked=2000, receipt={'consumedAt': 2000, 'steered': True}))['state'], 'steered')
        self.assertEqual(describe_message(user(invoked=2000, receipt={'consumedAt': 2000, 'controlOnly': True}))['state'], 'control-consumed')
        self.assertNotIn('savedStateConfirmed', describe_message(ack())['goalAcknowledgement'])
        for confirmed in (True, False):
            self.assertIs(describe_message(ack(savedStateConfirmed=confirmed))['goalAcknowledgement']['savedStateConfirmed'], confirmed)

    def test_summary_is_bounded_text_free_and_enriches_exact_goal_ack(self):
        goal = user('goal', text='/goal PRIVATE OBJECTIVE'); goal['seq'] = 1
        receipt = ack('goal', savedStateConfirmed=False)
        pending = user('pending', text='PRIVATE CORRECTION'); pending['seq'] = 3
        result = summarize_inputs([goal, receipt, pending], now=100, include_wait=False)
        self.assertEqual(len(result), 2)
        states = {r['localId']: r for r in result}
        self.assertEqual(states['pending']['state'], 'queued')
        self.assertEqual(states['goal']['state'], 'control-consumed')
        self.assertFalse(states['goal']['goalAcknowledgement']['savedStateConfirmed'])
        self.assertFalse(states['goal']['appliedToCurrentTurn'])
        self.assertNotIn('PRIVATE', json.dumps(result)); self.assertNotIn('waitSeconds', json.dumps(result))
        self.assertEqual(describe_message({'createdAt': None})['createdAt'], None)

    def test_summary_prefers_corrections_over_later_peer_notices_and_retains_receipt_changes(self):
        correction = user('correction'); correction['seq'] = 1
        peer = user('peer'); peer['seq'] = 2
        peer['content']['meta'].update(source='peer', sourceSessionId='peer-session')
        states = {d['localId']: d for d in summarize_inputs([correction, peer], include_wait=False)}
        self.assertEqual(list(states), ['correction'])
        correction['invokedAt'] = 3000
        correction['content']['meta']['deliveryReceipt'] = {'consumedAt': 3000, 'steered': True}
        goal = user('goal', text='/goal Saved objective', invoked=3000); goal['seq'] = 3
        states = {d['localId']: d for d in summarize_inputs([correction, peer, goal], include_wait=False)}
        self.assertEqual(len(states), 3)
        self.assertEqual(states['correction']['state'], 'steered')
        self.assertEqual(states['peer']['state'], 'queued')
        self.assertEqual(states['goal']['state'], 'control-consumed')
        self.assertNotIn('waitSeconds', json.dumps(states))
        # A text prefix or partial metadata is never classified as peer origin.
        untagged = user('direct', text='[peer] direct correction'); untagged['seq'] = 4
        untagged['content']['meta'].update(source='peer')
        self.assertEqual(summarize_inputs([correction, peer, untagged])[0]['localId'], 'direct')

    def test_priority_never_uses_legacy_claude_abort_fallback(self):
        self.assertEqual(validate_priority({}, 'queue', 'claude'), 'queue')
        for mode in ('steer', 'next'):
            with self.assertRaisesRegex(ValueError, 'implicit Stop'):
                validate_priority({}, mode, 'claude')
            self.assertEqual(validate_priority({'metadata': {'flavor': 'claude', 'capabilities': {'claudeInputPriorities': True}}}, mode), mode)
        self.assertEqual(validate_priority({}, 'steer', 'codex'), 'steer')
        with self.assertRaises(ValueError): validate_priority({}, 'next', 'codex')


class DeliveryApi(FakeHapi):
    def __init__(self):
        super().__init__()
        self.session.update(thinking=True, metadata={'flavor': 'codex', 'codexSessionId': 'native'})
        self.manager = {'id': 'manager', 'active': True, 'metadata': {}, 'agentState': {}}
        self.messages = [user()]
        self.queue = {'queuedLocalIds': ['a'], 'indeterminateLocalIds': [], 'invokedLocalMessages': []}
        self.calls = []
        self.response = {'status': 'steered', 'localId': 'a'}
        self.on_steer = None
        self.on_queue = None
        self.fail_reads = False

    def call(self, path, data=None, method=None):
        self.calls.append((path, data, method))
        if path == '/sessions/manager': return {'session': self.manager}
        if path == '/sessions/s/messages/a/steer':
            if self.on_steer: self.on_steer()
            if isinstance(self.response, Exception): raise self.response
            return self.response
        if path.endswith('/queued-state'):
            if self.on_queue: self.on_queue()
            if self.fail_reads: raise TimeoutError('private transport detail')
            if data != {'localIds': ['a']}: raise AssertionError('Queue inspection must use exact localId')
            return self.queue
        if path.startswith('/sessions/s/messages?'):
            if self.fail_reads: raise TimeoutError('private transport detail')
            return {'messages': self.messages}
        return super().call(path, data, method)


class AssignmentDelivery(IsolatedSupervisorTest):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.api = DeliveryApi()
        self.s = Supervisor(self.tmp.name + '/db', self.api)
        self.project = self.s.dispatch({'op': 'register', 'name': 'Test', 'repo': self.tmp.name, 'objective': 'Preserve task'})['id']
        self.s.dispatch({'op': 'resume', 'project': self.project})
        self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)', ('w', self.project, 'codex', 'native', 's', self.tmp.name, 'managed', time.time()-10))
        self.s.db.execute('INSERT INTO assignments(id,worker,task_key,text,state,created,goal) VALUES(?,?,?,?,?,?,?)', ('a', 'w', 'task', 'Correct scope', 'sent', time.time()-10, 0))
        self.s.set('manager', 'manager')
        self.intent()

    def tearDown(self): self.s.db.close(); self.tmp.cleanup()

    def intent(self, **changes):
        self.s.set('assignment-input:a', json.dumps({'worker': 'w', 'session': 's', 'native_id': 'native', 'priority': 'queue', **changes}))

    def inspect(self, messages=None): return inspect_assignment(self.s, 'w', 'a', messages)

    def request(self, **changes): return {'worker': 'w', 'assignment': 'a', 'user_requested': True, 'basis': 'User now explicitly asks to deliver existing correction', 'requested_at': time.time(), **changes}

    def writes(self): return [c for c in self.api.calls if c[2] == 'POST' or c[0] == '/sessions/s/messages']

    def test_exact_queue_proof_and_consumption_classification(self):
        self.assertEqual(self.inspect()['state'], 'queued')
        self.api.queue = {'queuedLocalIds': [], 'indeterminateLocalIds': ['a'], 'invokedLocalMessages': []}
        self.assertEqual(self.inspect()['state'], 'indeterminate')
        self.api.messages = [user(invoked=2000, mode='steer')]
        self.api.queue = {'queuedLocalIds': [], 'indeterminateLocalIds': [], 'invokedLocalMessages': [{'localId': 'a', 'invokedAt': 2000}]}
        self.assertEqual(self.inspect()['state'], 'consumed')
        self.api.messages = [user(invoked=2000, receipt={'consumedAt': 2000, 'steered': True})]
        self.assertEqual(self.inspect()['state'], 'steered')
        self.assertEqual(self.writes(), [])

    def test_goal_outside_recent_window_keeps_control_only_semantics(self):
        self.s.db.execute("UPDATE assignments SET text='/goal New objective',goal=1 WHERE id='a'")
        self.api.messages = []
        self.api.queue = {'queuedLocalIds': [], 'indeterminateLocalIds': [], 'invokedLocalMessages': [{'localId': 'a', 'invokedAt': 2000}]}
        result = self.inspect()
        self.assertEqual(result['state'], 'control-consumed'); self.assertFalse(result['appliedToCurrentTurn'])
        self.api.messages = [ack(savedStateConfirmed=False)]
        result = self.inspect()
        self.assertFalse(result['goalAcknowledgement']['savedStateConfirmed'])
        with self.assertRaisesRegex(ValueError, 'Slash/goal'):
            promote_assignment(self.s, self.request())
        self.assertEqual(self.writes(), [])

    def test_identity_changed_or_malformed_binding_remains_unknown(self):
        for changes in ({'session': 'other'}, {'native_id': 'other'}, {'worker': 'other'}):
            self.intent(**changes)
            self.assertEqual(self.inspect()['state'], 'unknown')
            with self.assertRaises(ValueError): promote_assignment(self.s, self.request())
        self.s.set('assignment-input:a', '[]')
        self.assertEqual(self.inspect()['state'], 'unknown')
        with self.assertRaisesRegex(ValueError, 'binding'):
            promote_assignment(self.s, self.request())
        self.assertEqual(self.writes(), [])

    def test_read_failure_and_conflicting_queue_evidence_never_prove_delivery(self):
        self.api.fail_reads = True
        self.assertEqual(self.inspect()['state'], 'unknown')
        self.api.fail_reads = False
        self.api.queue['invokedLocalMessages'] = [{'localId': 'a', 'invokedAt': 2}]
        self.assertEqual(self.inspect()['state'], 'unknown')
        self.assertEqual(self.writes(), [])

    def test_promotion_persists_before_rpc_and_does_not_equate_rpc_acceptance_with_ack(self):
        self.api.on_steer = lambda: self.assertEqual(json.loads(self.s.get('assignment-promotion:a'))['state'], 'attempting')
        result = promote_assignment(self.s, self.request())
        self.assertTrue(result['rpcAccepted']); self.assertFalse(result['acknowledged'])
        self.assertTrue(result['delivery']['rpcAccepted'])
        self.assertEqual(result['delivery']['state'], 'queued')
        self.assertEqual(self.writes(), [('/sessions/s/messages/a/steer', {}, 'POST')])
        self.assertFalse(result['resent']); self.assertFalse(result['aborted']); self.assertFalse(result['resumed'])
        with self.assertRaisesRegex(ValueError, 'Prior promotion'):
            promote_assignment(self.s, self.request())
        self.assertEqual(len(self.writes()), 1)

    def test_confirmed_receipt_after_rpc_is_steering_proof(self):
        def acknowledge():
            self.api.messages = [user(invoked=3000, receipt={'consumedAt': 3000, 'steered': True})]
            self.api.queue = {'queuedLocalIds': [], 'indeterminateLocalIds': [], 'invokedLocalMessages': [{'localId': 'a', 'invokedAt': 3000}]}
        self.api.on_steer = acknowledge
        result = promote_assignment(self.s, self.request())
        self.assertTrue(result['acknowledged']); self.assertEqual(result['delivery']['state'], 'steered')
        self.assertEqual(len(self.writes()), 1)

    def test_uncertain_attempt_survives_reload_and_cannot_be_blindly_repeated(self):
        self.api.response = TimeoutError('network lost')
        self.assertEqual(promote_assignment(self.s, self.request())['promotionState'], 'uncertain')
        self.s.db.close(); self.s = Supervisor(self.tmp.name + '/db', self.api)
        with self.assertRaisesRegex(ValueError, 'Prior promotion'):
            promote_assignment(self.s, self.request())
        self.assertEqual(len(self.writes()), 1)

    def test_current_authorization_required_and_plain_stop_text_is_never_an_abort(self):
        for changes in ({'user_requested': False}, {'basis': ''}, {'requested_at': time.time()-301}, {'requested_at': time.time()+1}, {'worker': None}):
            with self.assertRaises(ValueError): promote_assignment(self.s, self.request(**changes))
        self.s.db.execute("UPDATE assignments SET text='STOP after this iteration' WHERE id='a'")
        promote_assignment(self.s, self.request())
        self.assertEqual(self.writes(), [('/sessions/s/messages/a/steer', {}, 'POST')])

    def test_manual_stop_or_hold_never_cleared_by_promotion(self):
        cases = ['manager-pause', 'manager-offline', 'worker-pause', 'worker-offline', 'project-pause', 'project-manual', 'unmanaged', 'released']
        for case in cases:
            with self.subTest(case=case):
                self.api.manager.update(active=True); self.api.manager.pop('manualPausedAt', None)
                self.api.session.update(active=True); self.api.session.pop('manualPausedAt', None)
                self.s.db.execute("UPDATE projects SET status='running',manual=0 WHERE id=?", (self.project,))
                self.s.db.execute("UPDATE workers SET state='managed' WHERE id='w'")
                self.s.db.execute("DELETE FROM settings WHERE key='unmanaged:w'")
                if case == 'manager-pause': self.api.manager['manualPausedAt'] = 123
                if case == 'manager-offline': self.api.manager['active'] = False
                if case == 'worker-pause': self.api.session['manualPausedAt'] = 123
                if case == 'worker-offline': self.api.session['active'] = False
                if case == 'project-pause': self.s.db.execute("UPDATE projects SET status='paused' WHERE id=?", (self.project,))
                if case == 'project-manual': self.s.db.execute("UPDATE projects SET manual=1 WHERE id=?", (self.project,))
                if case == 'unmanaged': self.s.set('unmanaged:w', '1')
                if case == 'released': self.s.db.execute("UPDATE workers SET state='released' WHERE id='w'")
                with self.assertRaises(ValueError): promote_assignment(self.s, self.request())
        self.assertEqual(self.writes(), [])

    def test_completed_tracker_allows_current_busy_correction_without_reopening_it(self):
        self.s.db.execute("UPDATE projects SET status='completed',objective='Keep original objective' WHERE id=?", (self.project,))
        result = promote_assignment(self.s, self.request())
        self.assertTrue(result['rpcAccepted'])
        self.assertEqual(self.s.project(self.project)['status'], 'completed')
        self.assertEqual(self.s.project(self.project)['objective'], 'Keep original objective')
        self.assertEqual(self.writes(), [('/sessions/s/messages/a/steer', {}, 'POST')])

    def test_scheduled_cancelled_uncertain_dispatching_or_legacy_claude_never_promoted(self):
        for state in ('sending', 'uncertain', 'cancelled', 'failed'):
            self.s.db.execute('UPDATE assignments SET state=? WHERE id=?', (state, 'a'))
            with self.assertRaisesRegex(ValueError, 'dispatch'): promote_assignment(self.s, self.request())
        self.s.db.execute("UPDATE assignments SET state='sent' WHERE id='a'")
        self.api.messages = [user(scheduledAt=2000)]
        with self.assertRaisesRegex(ValueError, 'unscheduled'): promote_assignment(self.s, self.request())
        self.api.messages = [user()]; self.api.session['metadata']['flavor'] = 'claude'
        with self.assertRaisesRegex(ValueError, 'implicit Stop'): promote_assignment(self.s, self.request())
        self.assertEqual(self.writes(), [])

    def test_queue_proof_without_original_message_cannot_prove_unscheduled_promotion(self):
        self.api.messages = []
        result = self.inspect()
        self.assertEqual(result['state'], 'queued')
        self.assertFalse(result['messageLocated'])
        with self.assertRaisesRegex(ValueError, 'unscheduled'):
            promote_assignment(self.s, self.request())
        self.assertEqual(self.writes(), [])

    def test_binding_race_after_inspection_is_rejected_before_rpc(self):
        self.api.on_queue = lambda: self.s.db.execute("UPDATE workers SET session='changed' WHERE id='w'")
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            promote_assignment(self.s, self.request())
        self.assertEqual(self.writes(), [])
