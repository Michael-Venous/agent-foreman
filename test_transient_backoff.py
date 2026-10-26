"""Quieter, shared retry backoff for provider rejections (429/capacity). Fake hub only."""
import json,time,unittest
from test_supervisor import IsolatedSupervisorTest
import test_transient_recovery as base
import transient_recovery as tr


class BackoffTests(IsolatedSupervisorTest):
 setUp=base.RecoveryTests.setUp
 tearDown=base.RecoveryTests.tearDown
 failure=base.RecoveryTests.failure
 observe=base.RecoveryTests.observe
 row=base.RecoveryTests.row

 def events(self,kind='transient-wait'):
  return self.s.rows("SELECT * FROM events WHERE kind=? ORDER BY id",(kind,))

 def test_outage_backoff_survives_human_messages_that_cancel_a_retry(self):
  first=self.failure(at=1000);self.observe([first]);self.assertEqual(self.row()['attempts'],0)
  # A human message cancels the pending retry (existing rule) and then fails too: still the same outage.
  self.now+=5;human={'id':'h','localId':'human','createdAt':self.now*1000,'invokedAt':self.now*1000,'content':{'role':'user','content':{'text':'status?'}}}
  self.observe([first,human]);self.assertEqual(self.row()['state'],'cancelled')
  self.now+=5;self.observe([first,human,self.failure('turn2','e2',at=self.now)]);second=self.row()
  self.assertEqual(second['attempts'],1);self.assertGreaterEqual(second['due']-self.now,120)
  self.now+=5;self.observe([first,human,self.failure('turn3','e3',at=self.now)]);self.assertGreaterEqual(self.row()['attempts'],2)

 def test_quiet_events_first_failure_then_every_fourth_only(self):
  loud=[]
  for n in range(1,9):
   self.now+=3;self.observe([self.failure('turn%d'%n,'e%d'%n,at=self.now)])
   row=self.s.rows("SELECT * FROM events WHERE event_key=?",('transient:s:turn%d'%n,))[0]
   if not row['acked']:loud.append(n)
   self.s.db.execute("UPDATE transient_recovery SET state='cancelled' WHERE session='s'") # next failure is a new episode in the same outage
  self.assertEqual(loud,[1,4,8]);self.assertEqual(json.loads(self.events()[3]['detail'])['consecutiveFailures'],4)

 def test_quota_waits_that_need_a_human_are_never_quiet(self):
  self.s.set('manager','s') # quota waits are tracked for the manager chat; workers use usage recovery
  self.observe([self.failure(text='429 usage limit exceeded, weekly quota resets tomorrow')])
  self.assertEqual([e['acked'] for e in self.events()],[0]);self.assertTrue(json.loads(self.events()[0]['detail'])['needsHumanInput'])

 def test_provider_outage_window_holds_retries_even_if_a_row_is_due(self):
  self.observe([self.failure()]);until=tr.provider_until(self.s,'codex');self.assertGreater(until,self.now)
  self.s.db.execute("UPDATE transient_recovery SET due=? WHERE session='s'",(self.now,)) # row says due, provider says wait
  tr.recover(self.s);self.assertEqual(self.api.sent,[])
  self.now=until+1;tr.recover(self.s);self.assertEqual(len(self.api.sent),1)

 def test_provider_window_grows_with_repeated_rejections_and_is_capped(self):
  windows=[]
  for n in range(1,9):
   self.now+=3;self.observe([self.failure('turn%d'%n,'e%d'%n,at=self.now)])
   windows.append(round(tr.provider_until(self.s,'codex')-self.now))
   self.s.db.execute("UPDATE transient_recovery SET state='cancelled' WHERE session='s'")
  self.assertEqual(windows[:5],[120,240,480,960,1800]);self.assertTrue(all(w<=1800 for w in windows))

 def test_recovery_output_clears_streak_and_provider_window(self):
  self.observe([self.failure()]);self.assertGreater(tr.streak(self.s,'s'),0)
  ok={'id':'ok','seq':9,'createdAt':(self.now+5)*1000,'content':{'role':'agent','content':{'type':'codex','data':{'id':'codex:n:turn2:h:agent_message','type':'message','message':'continuing'}}}}
  self.now+=10;self.observe([self.failure(),ok])
  self.assertEqual(tr.streak(self.s,'s'),0);self.assertEqual(tr.provider_until(self.s,'codex'),0.0);self.assertEqual(self.row()['state'],'done')

 def test_supervisor_does_not_report_expected_rejection_noise_while_a_retry_is_pending(self):
  w=self.s.worker('w');self.s.set('watch:w',json.dumps({'mode':'events','note':''}))
  self.s.set('reporting-default',json.dumps({'enabled':True,'fallback_minutes':30,'important':True}))
  failure={'kind':'capacity','message':'Codex error: exceeded retry limit, last status: 429','at':self.now*1000}
  self.api.session.update(lastFailure=failure,thinking=False)
  recovery_local='recovery-msg'
  receipt={'id':'rcpt','seq':3,'localId':recovery_local,'createdAt':(self.now+1)*1000,'invokedAt':(self.now+1)*1000,'content':{'role':'user','content':{'text':'Provider recovery: continue'}}}
  def notes():
   return [c for e in self.s.rows("SELECT detail FROM events WHERE kind='observation'") for c in json.loads(e['detail']).get('changes',[])]
  # With a pending automatic retry the failure and the supervisor's own recovery receipt are not reported.
  self.observe([self.failure()]);self.s.db.execute("UPDATE transient_recovery SET local_id=? WHERE session='s'",(recovery_local,))
  self.s.observe_context(w,self.api.session,[self.failure(),receipt]);self.s.observe_context(w,self.api.session,[self.failure(),receipt])
  self.assertEqual([c for c in notes() if c.get('category') in ('runtime-failure','input-delivery')],[])
  # Without a pending retry (e.g. human stopped/cancelled it) the failure is reported again.
  self.s.db.execute("UPDATE transient_recovery SET state='cancelled' WHERE session='s'")
  self.api.session['lastFailure']={**failure,'message':failure['message']+' (second)'}
  self.s.observe_context(w,self.api.session,[self.failure()])
  self.assertIn('runtime-failure',[c.get('category') for c in notes()])


if __name__=='__main__':unittest.main()
