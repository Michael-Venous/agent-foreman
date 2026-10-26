"""Regression fixes from the independent Opus review; fake APIs and isolated state."""
import json
import time
from unittest.mock import patch
import test_supervisor as fixtures

class DeliveryRepairs(fixtures.IsolatedSupervisorTest):
 setUp = fixtures.Tests.setUp
 tearDown = fixtures.Tests.tearDown

 def test_dead_worker_routine_report_does_not_block_another_input_event(self):
  self.s.set('manager','s')
  self.s.db.execute("INSERT INTO workers VALUES('dead',?,'codex','dead-native','deleted',?,'managed',?)",(self.p,self.tmp.name,time.time()))
  snap={'status':'running','latest_reply':'x','latest_tool':None,'evidence_message':None,'pending_requests':[],'trackerStatus':'running','changes':[],'reason':'routine fallback'}
  self.s.event('routine',self.p,'dead','observation',json.dumps(snap))
  self.s.event('input',self.p,'w','input-required','Need an answer')
  self.s.notify_manager()
  self.assertEqual(len(self.api.sent),1)
  self.assertIn('input-required',self.api.sent[0]['text'])
  self.assertIsNone(self.s.one("SELECT wake_id FROM events WHERE event_key='routine'")['wake_id'])

 def old_wake(self,state):
  self.s.set('manager','s')
  self.s.event('old-event',self.p,'w','finished','Old result')
  self.s.db.execute("INSERT INTO outbox VALUES('old','s','old wake',?,?)",(state,time.time()-3600))
  self.s.db.execute("UPDATE events SET wake_id='old'")

 def test_indeterminate_wake_never_replayed_after_timeout_or_restart(self):
  self.old_wake('uncertain');original=self.api.call
  def api(path,data=None,method=None):
   if path.endswith('/queued-state'):return {'queuedLocalIds':[],'indeterminateLocalIds':['old'],'invokedLocalMessages':[]}
   return original(path,data,method)
  self.api.call=api
  for _ in range(2):
   self.s.reconcile()
   self.assertEqual(self.s.one("SELECT state FROM outbox WHERE id='old'")['state'],'uncertain')
   self.assertEqual(self.api.sent,[])
   from supervisor import Supervisor
   self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)

 def test_cancelled_sent_wake_settles_without_replay_and_new_progress_flows(self):
  self.old_wake('sent')
  snap=self.s.progress_snapshot(self.s.worker('w'),self.api.session,[])
  self.s.queue_observation(self.s.worker('w'),snap,[{'category':'input-required'}],time.time())
  self.s.reconcile()
  self.assertEqual(self.s.one("SELECT state FROM outbox WHERE id='old'")['state'],'cancelled')
  self.assertEqual(self.s.one("SELECT acked FROM events WHERE event_key='old-event'")['acked'],1)
  self.assertEqual(len(self.api.sent),1)
  self.assertNotIn('Old result',self.api.sent[0]['text'])
  self.assertIn('input-required',self.api.sent[0]['text'])

 def test_incomplete_delivery_reply_cannot_settle_or_replay_a_wake(self):
  self.old_wake('uncertain');original=self.api.call
  self.api.call=lambda path,data=None,method=None: {} if path.endswith('/queued-state') else original(path,data,method)
  self.s.reconcile()
  self.assertEqual(self.s.one("SELECT state FROM outbox WHERE id='old'")['state'],'uncertain')
  self.assertEqual(self.api.sent,[])


import test_coordination as coordination_fixtures
from coordination import update_watches, signals

class WatchRepairs(fixtures.IsolatedSupervisorTest):
 setUp = coordination_fixtures.CoordinationTests.setUp
 tearDown = coordination_fixtures.CoordinationTests.tearDown
 watch = coordination_fixtures.CoordinationTests.watch

 def test_instruction_scrolling_out_of_page_does_not_cancel_watch(self):
  self.watch()
  self.api.messages=[coordination_fixtures.reply('render step',i) for i in range(10,110)]
  update_watches(self.s,self.s.worker('w'),[],self.api.messages,self.api.session)
  self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'waiting')
  self.api.messages.append(coordination_fixtures.prompt(110))
  update_watches(self.s,self.s.worker('w'),[],self.api.messages,self.api.session)
  self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'cancelled')
  update_watches(self.s,self.s.worker('w'),[],self.api.messages,self.api.session)
  self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='handoff-watch-cancelled'")),1)

 def test_paused_registration_rejected_before_creating_watch(self):
  for role in ('source','waiter'):
   project=self.p if role=='source' else self.q
   self.s.db.execute("UPDATE projects SET status='paused' WHERE id=?",(project,))
   with self.assertRaisesRegex(ValueError,'pause'):self.watch()
   self.assertEqual(self.s.dispatch({'op':'handoff_watches'}),[])
   self.s.db.execute("UPDATE projects SET status='running' WHERE id=?",(project,))
  self.waiter['manualPausedAt']=1
  with self.assertRaisesRegex(ValueError,'pause'):self.watch()

 def test_watch_holds_source_and_waiter_retirement_and_invalidates_review(self):
  self.watch()
  for wid,session,msgs in [('w',self.api.session,self.api.messages),('waiter',self.waiter,self.wait_messages)]:
   self.s.set('retirement-candidate:'+wid,json.dumps({'message':'done','at':time.time()-10}))
   self.s.retire_finished(self.s.worker(wid),session,msgs)
   self.assertEqual(self.s.worker(wid)['state'],'managed')
   self.assertIsNone(self.s.get('retirement-candidate:'+wid))
  record=json.loads(self.s.get('handoff-watch:drift-revision'));record['state']='review-required';self.s.set('handoff-watch:drift-revision',json.dumps(record))
  self.waiter['manualPausedAt']=1
  update_watches(self.s,self.s.worker('w'),[],self.api.messages,self.api.session)
  self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'cancelled')
  self.assertEqual(self.api.sent,[])

 def test_genuine_background_work_outranks_waiting_for_a_later_stage(self):
  self.api.session['backgroundTaskCount']=1
  self.api.messages.append(coordination_fixtures.reply('Render launched. Waiting for the GPU handoff before the next stage.',3))
  self.assertEqual(self.s.progress_snapshot(self.s.worker('w'))['status'],'background')

 def test_released_source_cancels_watch_without_waiting_for_another_release_message(self):
  self.watch()
  self.s.db.execute("UPDATE workers SET state='released' WHERE id='w'")
  from coordination import reconcile_registry
  reconcile_registry(self.s)
  self.assertEqual(self.s.dispatch({'op':'handoff_watches'})[0]['state'],'cancelled')
  self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='handoff-watch-cancelled'")),1)

 def test_plain_frame_counts_are_not_gpu_release_signals(self):
  self.assertFalse(signals('2000 free frames remain')['resource-release'])
  for text in ['GPU is free','RTX 3090 is free','3090 Ti is free']:
   self.assertTrue(signals(text)['resource-release'])

 def test_wait_and_important_release_evidence_hold_retirement_without_watch(self):
  self.assertTrue(self.s.result_needs_followup('Paused until terrain releases the GPU; I will resume once the GPU is free.'))
  self.s.event('release',self.p,'w','observation',json.dumps({'changes':[{'category':'resource-release'}]}))
  self.s.retire_finished(self.s.worker('w'),self.api.session,[])
  self.assertEqual(self.s.worker('w')['state'],'managed')
  self.assertEqual(self.s.one("SELECT acked FROM events WHERE event_key='release'")['acked'],0)

class ResumeRepairs(fixtures.IsolatedSupervisorTest):
 setUp = fixtures.Tests.setUp
 tearDown = fixtures.Tests.tearDown

 def test_authorized_resume_same_or_new_hapi_id_keeps_management(self):
  for target in ('s','new'):
   with self.subTest(target=target):
    self.s.db.execute("UPDATE workers SET session='s',state='managed' WHERE id='w'")
    self.s.set('host:w',101)
    self.api.session.update(active=False,manualPausedAt=1,metadata={'hostPid':101,'codexSessionId':'native'})
    original=fixtures.FakeHapi.call.__get__(self.api);calls=[]
    def api(path,data=None,method=None):
     calls.append(path)
     if path=='/sessions/s/manual-resume':self.api.session['manualPausedAt']=None;return {'ok':True}
     if path=='/sessions/s/resume':
      self.api.session.update(id=target,active=True,metadata={'hostPid':202,'codexSessionId':'native'})
      return {'type':'success','sessionId':target}
     if path=='/sessions/new':return {'session':self.api.session}
     if path.startswith('/sessions/new/messages?'):return {'messages':[],'page':{'hasMore':False}}
     return original(path,data,method)
    self.api.call=api
    with patch('supervisor.live_controller',return_value=False):
     self.s.dispatch({'op':'resume_chat','worker':'w','requested_at':time.time(),'user_requested':True,'basis':'User now asks to resume this chat'})
    self.s.scan(self.s.worker('w'))
    self.assertEqual(self.s.worker('w')['state'],'managed')
    self.assertEqual(self.s.worker('w')['session'],target)
    self.assertEqual(self.s.get('host:w'),'202')
    self.assertEqual(calls.count('/sessions/s/resume'),1)

 def test_uncertain_resume_reconciles_once_without_relaunch_and_respects_new_pause_or_duplicates(self):
  self.s.set('resume-pending:w',json.dumps({'source':'s','native':'native','basis':'Current explicit user resume','at':time.time(),'target':'new'}))
  original=self.api.call;calls=[]
  target={'id':'new','active':True,'manualPausedAt':42,'metadata':{'hostPid':202,'codexSessionId':'native','path':self.tmp.name}}
  duplicate={**target,'id':'duplicate','manualPausedAt':None,'metadata':{**target['metadata'],'hostPid':303}}
  sessions=[target]
  def api(path,data=None,method=None):
   calls.append(path)
   if path=='/sessions':return {'sessions':sessions}
   if path=='/sessions/new':return {'session':target}
   if path=='/sessions/duplicate':return {'session':duplicate}
   return original(path,data,method)
  self.api.call=api
  self.assertFalse(self.s.reconcile_authorized_resume(self.s.worker('w')))
  self.assertEqual(self.s.worker('w')['session'],'s')
  target['manualPausedAt']=None;sessions.append(duplicate)
  self.assertFalse(self.s.reconcile_authorized_resume(self.s.worker('w')))
  sessions.pop()
  result=self.s.dispatch({'op':'resume_chat','worker':'w','user_requested':True,'basis':'User repeats current resume request','requested_at':time.time()})
  self.assertTrue(result['reconciled']);self.assertEqual(self.s.worker('w')['session'],'new')
  self.assertFalse(any(path.endswith('/resume') or path.endswith('/manual-resume') for path in calls))



import test_retention as retention_fixtures

class CompletionRepairs(fixtures.IsolatedSupervisorTest):
 setUp = fixtures.Tests.setUp
 tearDown = fixtures.Tests.tearDown
 terminal = retention_fixtures.Retention.terminal
 manager = retention_fixtures.Retention.manager
 ready_to_retire = retention_fixtures.Retention.ready_to_retire
 scan = retention_fixtures.Retention.scan

 def test_already_delivered_completion_is_not_repeated_after_background_drains(self):
  self.manager();self.terminal()
  self.api.session['backgroundTaskCount']=1
  self.s.event('result:a',self.p,'w','finished','Finished work')
  self.s.notify_manager()
  self.assertEqual(len(self.api.sent),1)
  self.api.session['backgroundTaskCount']=0
  self.scan();self.ready_to_retire();self.scan();self.s.notify_manager()
  self.assertEqual(self.s.worker('w')['state'],'released')
  self.assertEqual(len(self.api.sent),1)
