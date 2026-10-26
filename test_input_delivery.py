"""Busy-turn delivery integration: all effects isolated from live HAPI/state."""
import json
import time
from unittest.mock import patch

import test_supervisor as fixtures


class InputDelivery(fixtures.IsolatedSupervisorTest):
 setUp = fixtures.Tests.setUp
 tearDown = fixtures.Tests.tearDown

 def send(self, **options):
  return self.s.dispatch({'op':'assign','worker':'w','key':'correction','text':'Finish current iteration, then stop','user_requested':True,**options})

 def test_priority_is_explicit_and_sent_is_not_applied(self):
  self.api.session['thinking']=True
  result=self.send(delivery_mode='steer')
  self.assertEqual(self.api.sent[0]['deliveryMode'],'steer')
  self.assertEqual(self.api.sent[0]['localId'],result['id'])
  self.assertNotEqual(result['delivery'].get('state'),'applied')
  intent=json.loads(self.s.get('assignment-input:'+result['id']))
  self.assertEqual((intent['session'],intent['native_id'],intent['priority']),('s','native','steer'))
  same=self.send(delivery_mode='steer')
  self.assertEqual(same['id'],result['id']);self.assertEqual(len(self.api.sent),1)

 def test_default_queue_and_metadata_commands_do_not_gain_priority(self):
  result=self.send(text='/goal Finish current iteration, then stop')
  self.assertEqual(self.api.sent[0]['text'],'/goal Finish current iteration, then stop')
  self.assertEqual(self.api.sent[0]['deliveryMode'],'queue')
  for options in ({'text':'/goal x'}, {'goal':True}, {'text':'/compact'}):
   with self.assertRaisesRegex(ValueError,'metadata'):
    self.send(key='reject'+str(options),delivery_mode='steer',**options)
  self.assertEqual(len(self.api.sent),1)

 def test_codex_rejects_claude_only_boundary_mode(self):
  with self.assertRaisesRegex(ValueError,'only for Claude'):self.send(delivery_mode='next')
  self.assertEqual(self.api.sent,[])

 def test_old_claude_controller_cannot_invoke_legacy_interrupt_fallback(self):
  self.s.db.execute("UPDATE workers SET runtime='claude' WHERE id='w'")
  for mode in ('steer','next'):
   with self.assertRaisesRegex(ValueError,'legacy interrupt'):self.send(delivery_mode=mode)
  self.assertEqual(self.api.sent,[])

 def test_send_warning_preserved_without_retry_or_claimed_delivery(self):
  original=self.api.call
  def api(path,data=None,method=None):
   result=original(path,data,method)
   return {'deliveryWarning':'Saved; native steer was not confirmed'} if path=='/sessions/s/messages' else result
  self.api.call=api
  result=self.send(delivery_mode='steer')
  self.assertIn('not confirmed',result['deliveryWarning'])
  self.assertEqual(result['state'],'sent');self.assertEqual(len(self.api.sent),1)

 def test_fresh_waiting_scope_change_notifies_once_without_interrupt_or_age_spam(self):
  self.api.session['thinking']=True
  worker=self.s.worker('w')
  with patch('supervisor.time.time',return_value=1000):self.s.observe_context(worker,self.api.session,[])
  prompt={'id':'queued-correction','localId':'correction','seq':1,'createdAt':1001000,'invokedAt':None,
          'content':{'role':'user','meta':{'deliveryMode':'queue'},'content':{'type':'text','text':'Stop after current iteration'}}}
  with patch('supervisor.time.time',return_value=1002):self.s.observe_context(worker,self.api.session,[prompt])
  observations=self.s.rows("SELECT * FROM events WHERE kind='observation'")
  self.assertEqual(len(observations),1)
  change=json.loads(observations[0]['detail'])['changes'][0]
  self.assertEqual(change['category'],'input-delivery');self.assertEqual(change['delivery']['state'],'queued')
  self.assertIn('not confirmed steering',change['reason'])
  with patch('supervisor.time.time',return_value=1020):self.s.observe_context(worker,self.api.session,[prompt])
  self.assertEqual(self.s.rows("SELECT * FROM events WHERE kind='observation'"),observations)
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.s.rows('SELECT * FROM assignments'),[])

 def test_metadata_goal_and_pending_prompt_are_separate_from_public_reply(self):
  now=time.time()*1000
  self.api.messages=[
   {'id':'goal','localId':'goal-id','seq':1,'createdAt':now,'invokedAt':now+1,'content':{'role':'user','content':{'text':'/goal Stop after current iteration'}}},
   {'id':'runtime','seq':2,'createdAt':now+2,'content':{'role':'agent','content':{'type':'event','data':{'type':'message','message':'{"goal":{"status":"blocked","objective":"Stop"}}'}}}},
   {'id':'prompt','localId':'prompt-id','seq':3,'createdAt':now+3,'invokedAt':None,'content':{'role':'user','meta':{'deliveryMode':'queue'},'content':{'text':'Finish current iteration, then stop'}}}]
  digest=self.s.dispatch({'op':'digest','worker':'w'})
  self.assertIsNone(digest['latest_reply'])
  states={item['localId']:item for item in digest['input_delivery']}
  self.assertEqual(states['prompt-id']['state'],'queued')
  self.assertFalse(states['goal-id']['appliedToCurrentTurn'])
  self.assertNotEqual(states['goal-id']['state'],'steered')
  self.assertEqual(self.api.sent,[])

 def test_exact_assignment_worker_required_for_both_controls(self):
  for op in ('assignment_delivery','steer_assignment'):
   with self.assertRaisesRegex(ValueError,'Exact worker'):self.s.dispatch({'op':op,'assignment':'some-id'})

 def test_queued_to_steered_receipt_notifies_once_without_claiming_compliance(self):
  self.api.session['thinking']=True;worker=self.s.worker('w')
  prompt={'id':'input','localId':'same-input','seq':1,'createdAt':1001000,'invokedAt':None,
          'content':{'role':'user','meta':{'deliveryMode':'queue'},'content':{'type':'text','text':'Finish current iteration, then stop'}}}
  with patch('supervisor.time.time',return_value=1000):self.s.observe_context(worker,self.api.session,[])
  with patch('supervisor.time.time',return_value=1002):self.s.observe_context(worker,self.api.session,[prompt])
  self.s.db.execute('UPDATE events SET acked=1')
  prompt['invokedAt']=1003000
  prompt['content']['meta']['deliveryReceipt']={'consumedAt':1003000,'steered':True}
  with patch('supervisor.time.time',return_value=1004):self.s.observe_context(worker,self.api.session,[prompt])
  event=self.s.one('SELECT * FROM events WHERE acked=0')
  change=json.loads(event['detail'])['changes'][0]
  self.assertEqual(change['delivery']['state'],'steered')
  self.assertEqual(change['delivery']['compliance'],'unverified')
  with patch('supervisor.time.time',return_value=1020):self.s.observe_context(worker,self.api.session,[prompt])
  self.assertEqual(self.s.rows('SELECT * FROM events WHERE acked=0'),[event])
  self.assertEqual(self.api.sent,[])

 def test_muted_input_transitions_are_seeded_without_replay_when_reporting_returns(self):
  self.api.session['thinking']=True;worker=self.s.worker('w')
  self.s.dispatch({'op':'watch','worker':'w','mode':'off'})
  prompt={'id':'muted','localId':'muted-input','seq':1,'createdAt':1001000,'invokedAt':None,
          'content':{'role':'user','content':{'type':'text','text':'Current correction'}}}
  with patch('supervisor.time.time',return_value=1002):self.s.observe_context(worker,self.api.session,[prompt])
  self.s.dispatch({'op':'watch','worker':'w','mode':'events'})
  with patch('supervisor.time.time',return_value=1003):self.s.observe_context(worker,self.api.session,[prompt])
  self.assertEqual(self.s.rows('SELECT * FROM events'),[]);self.assertEqual(self.api.sent,[])
