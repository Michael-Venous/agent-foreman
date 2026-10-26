import json,tempfile,time,unittest
from unittest.mock import patch
from supervisor import Supervisor
from test_supervisor import IsolatedSupervisorTest,FakeHapi
import transient_recovery as tr

class RecoveryTests(IsolatedSupervisorTest):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.api=FakeHapi();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.p=self.s.dispatch({'op':'register','name':'test','repo':self.tmp.name,'objective':'task'})['id'];self.s.dispatch({'op':'resume','project':self.p})
  self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',('w',self.p,'codex','n','s',self.tmp.name,'managed',0))
  self.api.session['metadata']={'flavor':'codex','codexSessionId':'n'}
  self.now=1000;self.clock=patch('transient_recovery.time.time',side_effect=lambda:self.now);self.clock.start()
 def tearDown(self):self.clock.stop();self.s.db.close();self.tmp.cleanup()
 def failure(self,turn='turn1',mid='error1',text='Codex error: exceeded retry limit, last status: 429 Too Many Requests',at=None):
  return {'id':mid,'seq':1,'createdAt':(at or self.now)*1000,'content':{'role':'agent','content':{'type':'codex','data':{'id':'codex:n:'+turn+':hash:task_failed','type':'message','message':text}}}}
 def observe(self,msgs=None):
  if msgs is not None:self.api.messages=msgs
  tr.observe(self.s,'s',self.api.session,self.api.messages,self.s.worker('w'))
 def row(self,sid='s'):
  rows=self.s.rows('SELECT * FROM transient_recovery WHERE session=?',(sid,));return rows[0] if rows else None
 def due(self):self.now=self.row()['due']+1
 def test_exact_parent_identity_not_prose_children_tools_retry_or_wrong_native(self):
  good=self.failure();self.assertEqual(tr.classify(good,'n')['kind'],'rate')
  for change in [{'id':'codex:n:t:hash:agent_message'},{'scope_role':'child'},{'scope':{'role':'child'}},{'willRetry':True},{'id':'codex:other:t:hash:task_failed'}]:
   m=self.failure();m['content']['content']['data'].update(change);self.assertIsNone(tr.classify(m,'n'))
  good['content']['role']='user';self.assertIsNone(tr.classify(good,'n'))
  self.assertIsNone(tr.classify(self.failure(text='Authentication failed'),'n'))
  child={'id':'childquota','content':{'role':'agent','content':{'type':'codex','data':{'type':'thread_goal_updated','scope_role':'child','goal':{'status':'usageLimited'}}}}}
  self.assertIsNone(tr.classify(child,'n'))
  self.assertIsNone(tr.classify(self.failure(text='Task blocked waiting for permission'),'n'))
  self.assertEqual(tr.classify(self.failure(text='Selected model is at capacity'),'n')['kind'],'capacity')
  self.assertEqual(tr.classify(self.failure(text='429 usage limit exceeded, weekly quota resets tomorrow'),'n')['kind'],'quota')
 def test_pair_dedup_deadline_and_restart_persistence(self):
  a=self.failure();self.observe([a]);first=self.row();self.assertGreaterEqual(first['due'],1060)
  self.now=1002;self.observe([a,self.failure(mid='terminal2')]);self.assertEqual(self.row()['due'],first['due'])
  self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='transient-wait'")),1)
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api);self.assertEqual(self.row(),first)
  tr.recover(self.s);self.assertEqual(self.api.sent,[])
  self.due();tr.recover(self.s);tr.recover(self.s);self.assertEqual(len(self.api.sent),1)
  self.assertEqual(self.row()['attempts'],1);self.assertEqual(self.row()['state'],'sent')
  self.assertTrue(self.api.sent[0]['text'].startswith('Provider recovery: continue the interrupted request'))
 def test_backoff_new_failed_invocation_without_retry_storm(self):
  self.observe([self.failure()]);self.due();tr.recover(self.s)
  self.now+=2;self.observe([self.failure('turn2','next')]);second=self.row()
  self.assertGreaterEqual(second['due']-self.now,120);self.assertEqual(second['attempts'],1)
  tr.recover(self.s);self.assertEqual(len(self.api.sent),1)
  self.due();tr.recover(self.s);self.assertEqual(len(self.api.sent),2)
 def test_uncertain_or_missing_intake_never_resends(self):
  self.observe([self.failure()]);self.due();original=self.api.call
  def call(path,data=None,method=None):
   if path=='/sessions/s/messages':self.api.sent.append(data);raise TimeoutError('lost')
   return original(path,data,method)
  self.api.call=call
  tr.recover(self.s)
  self.assertEqual(self.row()['state'],'uncertain');self.now+=9999
  tr.recover(self.s);self.assertEqual(len(self.api.sent),1)
 def test_stops_holds_busy_offline_identity_and_background_jobs_block_without_restart(self):
  for state in [{'manualPausedAt':1},{'active':False},{'thinking':True},{'backgroundTaskCount':1},{'agentState':{'requests':{'a':{}}}},{'metadata':{'codexSessionId':'other'}}]:
   with self.subTest(state=state):
    self.s.db.execute('DELETE FROM transient_recovery')
    self.api.session.update(active=True,thinking=False,backgroundTaskCount=0,manualPausedAt=None,agentState={},metadata={'codexSessionId':'n','flavor':'codex'})
    self.observe([self.failure()]);self.due();self.api.session.update(state);tr.recover(self.s);self.assertEqual(self.api.sent,[])
  self.api.session.update(metadata={'codexSessionId':'n'},agentState={})
  for sql in ["UPDATE projects SET manual=1", "UPDATE projects SET manual=0,status='paused'", "UPDATE projects SET status='running';"]:
   if sql.endswith(';'):continue
   self.s.db.execute(sql);tr.recover(self.s);self.assertEqual(self.api.sent,[])
 def test_new_input_cancels_even_if_uninvoked_and_old_failure_does_not_rearm(self):
  error=self.failure();self.observe([error]);self.now+=2
  self.api.messages=[error,{'id':'new','localId':'human','createdAt':self.now*1000,'invokedAt':None,'content':{'role':'user','content':{'text':'changed request'}}}]
  self.observe();self.assertEqual(self.row()['state'],'cancelled');self.now+=999;tr.recover(self.s);self.observe();self.assertEqual(self.api.sent,[])
 def test_unresolved_outbox_input_blocks_automatic_continuation(self):
  error=self.failure();self.observe([error]);self.s.db.execute('INSERT INTO outbox VALUES(?,?,?,?,?)',('wake','s','events','uncertain',self.now))
  self.api.messages=[error,{'id':'wake-message','localId':'wake','createdAt':(self.now+1)*1000,'invokedAt':None,'content':{'role':'user','content':{'text':'events'}}}]
  self.due();tr.recover(self.s);self.assertEqual(self.api.sent,[])
 def test_positive_output_settles_provider_recovery_not_goal_completion(self):
  error=self.failure();self.observe([error]);self.due();tr.recover(self.s)
  self.now+=2;self.observe([error,{'id':'work','createdAt':self.now*1000,'content':{'role':'agent','content':{'type':'codex','data':{'type':'tool-call','name':'CodexBash','id':'codex:n:new:call:exec_command_begin'}}}}])
  self.assertEqual(self.row()['state'],'done');self.assertFalse(tr.blocked(self.s,'s'))
 def test_quota_stays_on_separate_existing_reset_policy(self):
  m=self.failure(text='Usage limit exceeded, quota resets tomorrow');self.observe([m]);self.assertIsNone(self.row())
  self.s.observe_usage_limit(self.s.worker('w'),m);self.assertEqual(self.s.one('SELECT * FROM usage_recovery')['state'],'waiting')
  self.assertGreaterEqual(self.s.one('SELECT * FROM usage_recovery')['due'],self.now+3600)
 def test_manager_failed_wake_is_gated_and_one_recovery_preserves_pending_events(self):
  self.s.set('manager','s');error=self.failure();self.api.messages=[error]
  ev=self.s.event('pending',self.p,'w','check-in','important task')
  self.s.notify_manager();self.s.notify_manager();self.assertEqual(self.api.sent,[]);self.assertTrue(tr.blocked(self.s,'s'))
  self.now=self.row()['due']+1;tr.recover(self.s);self.s.notify_manager();self.assertEqual(len(self.api.sent),1)
  self.assertIn('unacknowledged supervisor',self.api.sent[0]['text'])
  self.assertEqual(self.s.one("SELECT acked FROM events WHERE event_key='pending'")['acked'],0)
 def test_manager_manual_stop_and_quota_never_get_transient_nudges(self):
  self.s.set('manager','s');self.api.messages=[self.failure()];self.s.notify_manager();self.due()
  self.api.session['manualPausedAt']=2;tr.recover(self.s);self.s.notify_manager();self.assertEqual(self.api.sent,[])
  self.api.session['manualPausedAt']=None;self.now+=1;self.api.messages=[self.failure('quota','quota',text='Weekly usage limit exceeded')]
  self.s.notify_manager();self.assertEqual(self.row()['state'],'quota-wait');self.now+=99999;tr.recover(self.s);self.assertEqual(self.api.sent,[])
 def test_global_dispatch_spacing(self):
  self.observe([self.failure()]);self.due();tr.recover(self.s)
  self.assertGreater(float(self.s.get('transient-next-dispatch')),self.now)

 def test_known_manager_quota_reset_and_retry_after_are_respected(self):
  self.s.set('manager','s');m=self.failure(text='Quota usage limit exceeded')
  m['content']['content']['data']['resetsAt']=5000
  self.api.messages=[m];self.s.notify_manager();self.assertEqual(self.row()['due'],5060)
  tr.recover(self.s);self.assertEqual(self.api.sent,[])
  self.s.db.execute('DELETE FROM transient_recovery');self.s.set('manager','')
  m=self.failure();m['content']['content']['data']['retryAfterSeconds']=300
  self.observe([m]);self.assertGreaterEqual(self.row()['due'],self.now+300)
 def test_supervisor_retry_provenance_preserves_dependency_watch_input_epoch(self):
  from coordination import watch_messages
  self.observe([self.failure()]);self.due();tr.recover(self.s);local=self.row()['local_id']
  self.assertEqual(self.s.one('SELECT session FROM outbox WHERE id=?',(local,))['session'],'s')
  messages=[{'id':'human','localId':'human-input','content':{'role':'user'}},{'id':'retry','localId':local,'content':{'role':'user'}}]
  self.assertEqual([m['id'] for m in watch_messages(self.s,self.s.worker('w'),messages)],['human'])
 def test_global_spacing_actually_defers_another_conversation(self):
  self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',('w2',self.p,'codex','n2','t',self.tmp.name+'/other','managed',0))
  other={'id':'t','active':True,'thinking':False,'metadata':{'flavor':'codex','codexSessionId':'n2'},'agentState':{}}
  failure=self.failure('turn-other','other');failure['content']['content']['data']['id']='codex:n2:turn-other:hash:task_failed'
  tr.observe(self.s,'t',other,[failure],self.s.worker('w2'));self.observe([self.failure()])
  original=self.api.call
  def call(path,data=None,method=None):
   if path=='/sessions/t':return {'session':other}
   if path=='/sessions/t/messages?limit=100':return {'messages':[failure]}
   if path=='/sessions/t/messages':self.api.sent.append({**data,'target':'t'});return {'ok':True}
   if path=='/sessions/t/messages/queued-state':return {'queuedLocalIds':[],'indeterminateLocalIds':[],'invokedLocalMessages':[]}
   return original(path,data,method)
  self.api.call=call;self.now=max(r['due'] for r in self.s.rows('SELECT due FROM transient_recovery'))+1
  tr.recover(self.s);tr.recover(self.s);self.assertEqual(len(self.api.sent),1)
  self.now+=31;tr.recover(self.s);self.assertEqual(len(self.api.sent),2)
  self.assertNotEqual(self.api.sent[0]['localId'],self.api.sent[1]['localId'])
 def test_sending_crash_does_not_allocate_another_attempt(self):
  self.observe([self.failure()]);self.s.db.execute("UPDATE transient_recovery SET state='sending',local_id='before-crash'")
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api);self.now+=5000;tr.recover(self.s)
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.row()['local_id'],'before-crash')

 def test_release_watch_off_project_hold_and_manager_hold_are_enforced(self):
  for change in ['release','off','hold']:
   self.s.db.execute('DELETE FROM transient_recovery');self.s.db.execute("UPDATE workers SET state='managed'");self.s.db.execute('UPDATE projects SET manual=0')
   self.s.set('watch:w',json.dumps({'mode':'events','note':''}));self.api.session.update(manualPausedAt=None,active=True,thinking=False)
   self.observe([self.failure(mid=change)]);self.due()
   if change=='release':self.s.db.execute("UPDATE workers SET state='released'")
   if change=='off':self.s.set('watch:w',json.dumps({'mode':'off','note':''}))
   if change=='hold':self.s.db.execute('UPDATE projects SET manual=1')
   tr.recover(self.s);self.assertEqual(self.api.sent,[])
 def test_healthy_output_retires_only_retry_bookkeeping(self):
  self.observe([self.failure()]);self.s.event('real-completion',self.p,'w','finished','deliverable')
  self.now+=2;self.observe([self.failure(at=1000),{'id':'progress','createdAt':self.now*1000,'content':{'role':'agent','content':{'type':'codex','data':{'id':'codex:n:t:message:agent_message','type':'message','message':'Actual output'}}}}])
  self.assertEqual(self.s.one("SELECT acked FROM events WHERE kind='transient-wait'")['acked'],1)
  self.assertEqual(self.s.one("SELECT acked FROM events WHERE event_key='real-completion'")['acked'],0)

if __name__=='__main__':unittest.main()
