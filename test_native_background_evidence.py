import json
import unittest
import test_supervisor as fixtures
from background_evidence import claude_completed


def event(seq, task='a', status='completed'):
    return {'id': 'receipt'+str(seq), 'seq': seq, 'content': {'role': 'agent', 'content': {'type': 'output', 'data': {
        'type': 'system', 'subtype': 'task_notification', 'task_id': task, 'status': status}}}}

class Receipts(unittest.TestCase):
    def test_only_structured_agent_terminal_receipts(self):
        self.assertEqual(claude_completed(event(1))['taskId'], 'a')
        self.assertEqual(claude_completed(event(1, status='failed'))['status'], 'failed')
        for status in ('running', 'stopped', 'cancelled'):
            self.assertIsNone(claude_completed(event(1, status=status)))
        e = event(1); e['content']['role'] = 'user'
        self.assertIsNone(claude_completed(e))
        text = event(2); text['content']['content']['data'] = {'type':'user','message':{'content':[{'type':'text','text':'<task-notification><task-id>a</task-id><status>completed</status></task-notification>'}]}}
        self.assertEqual(claude_completed(text)['taskId'], 'a')
        text['content']['content']['data']['message']['content'][0]['text'] = 'example: <task-notification><task-id>a</task-id><status>completed</status></task-notification>'
        self.assertIsNone(claude_completed(text))

class Observation(fixtures.IsolatedSupervisorTest):
    setUp = fixtures.Tests.setUp
    tearDown = fixtures.Tests.tearDown
    observe = fixtures.Tests.observe
    observations = fixtures.Tests.observations
    set_public = fixtures.Tests.set_public
    public_progress = fixtures.Tests.public_progress
    def test_claude_completion_after_idle_turn_notifies_once_without_sending(self):
        self.s.db.execute("UPDATE workers SET runtime='claude' WHERE id='w'")
        self.api.session.update(thinking=False,backgroundTaskCount=1)
        self.set_public('Render running, review afterward.', 1); self.observe(1000)
        self.api.session['backgroundTaskCount'] = 0; self.api.messages.append(event(2)); self.observe(1100)
        changes = json.loads(self.observations()[0]['detail'])['changes']
        self.assertEqual(changes[0]['nativeTaskExits'][0]['taskId'], 'a')
        self.s.db.execute('UPDATE events SET acked=1'); self.observe(1200)
        self.assertEqual(self.observations(), []); self.assertEqual(self.api.sent, [])
    def test_receipt_arriving_after_count_update_still_notifies_once(self):
        self.s.db.execute("UPDATE workers SET runtime='claude' WHERE id='w'")
        self.api.session.update(thinking=False, backgroundTaskCount=1)
        self.set_public('Render running.', 1); self.observe(1000)
        self.api.session['backgroundTaskCount']=0; self.observe(1100)
        self.assertEqual(self.observations(), [])
        self.api.messages.append(event(2)); self.observe(1101)
        self.assertEqual(json.loads(self.observations()[0]['detail'])['changes'][0]['nativeTaskExits'][0]['taskId'], 'a')
        self.s.db.execute('UPDATE events SET acked=1'); self.observe(1102)
        self.assertEqual(self.observations(), []); self.assertEqual(self.api.sent, [])
    def test_other_active_task_prevents_final_handoff(self):
        self.s.db.execute("UPDATE workers SET runtime='claude' WHERE id='w'")
        self.api.session.update(thinking=False, backgroundTaskCount=2)
        self.set_public('Two renders running.', 1); self.observe(1000)
        self.api.session['backgroundTaskCount']=1; self.api.messages.append(event(2)); self.observe(1100)
        self.assertEqual(self.observations(), [])
        self.api.session['backgroundTaskCount']=0; self.api.messages.append(event(3, 'b', 'failed')); self.observe(1200)
        changes=json.loads(self.observations()[0]['detail'])['changes']
        self.assertEqual(changes[0]['nativeTaskExits'][0]['status'], 'failed')
        self.assertEqual(self.api.sent, [])
    def test_manual_pause_does_not_dispatch_or_generate_updates(self):
        self.api.session.update(thinking=False, backgroundTaskCount=1)
        self.set_public('Render running.', 1); self.observe(1000)
        self.api.session.update(backgroundTaskCount=0,manualPausedAt=1); self.api.messages.append(event(2)); self.observe(1100)
        self.assertEqual(self.observations(), []); self.assertEqual(self.api.sent, [])
    def test_count_correction_without_receipt_remains_silent(self):
        self.api.session.update(thinking=False,backgroundTaskCount=1)
        self.set_public('No work left.', 1); self.observe(1000)
        self.api.session['backgroundTaskCount']=0; self.observe(1100)
        self.assertEqual(self.observations(), [])
