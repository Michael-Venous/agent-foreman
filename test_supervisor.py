import unittest,unittest.mock,tempfile,time,json
from supervisor import Supervisor
from pathlib import Path
import supervisor

class IsolatedSupervisorTest(unittest.TestCase):
 def run(self,result=None):
  with tempfile.TemporaryDirectory() as root, unittest.mock.patch.object(supervisor,'STATE',Path(root)/'state'), unittest.mock.patch.object(supervisor,'CONTROL',Path(root)/'control'):
   return super().run(result)

class FakeHapi:
 def __init__(self):self.messages=[];self.sent=[];self.session={'id':'s','active':True,'thinking':False,'metadata':{},'agentState':{}};self.invoked=[]
 def call(self,path,data=None,method=None):
  if path=='/sessions/s':return {'session':self.session}
  if path=='/sessions/s/goal':return {'goal':self.session.get('goal')}
  if path.startswith('/sessions/s/messages?'):return {'messages':self.messages,'page':{'hasMore':False}}
  if path.endswith('/queued-state'):return {'queuedLocalIds':[],'indeterminateLocalIds':[],'invokedLocalMessages':[{'localId':i,'invokedAt':1} for i in self.invoked]}
  if path=='/sessions/s/messages':self.sent.append(data);return {'ok':True}
  if path=='/sessions':return {'sessions':[]}
  raise ValueError(path)
class Tests(IsolatedSupervisorTest):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.api=FakeHapi();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.p=self.s.dispatch({'op':'register','name':'test','repo':self.tmp.name,'objective':'test'})['id']
  self.s.dispatch({'op':'resume','project':self.p})
  self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',('w',self.p,'codex','native','s',self.tmp.name,'managed',time.time()-10))
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def assign(self,key='key'):return self.s.dispatch({'op':'assign','worker':'w','text':'do task','key':key})
 def limit(self,mid='limit',warning=False):
  text='Claude AI usage limit '+('warning' if warning else 'reached')+'|'+str(int(time.time()+600))+'|five_hour'
  return {'id':mid,'createdAt':time.time()*1000,'content':{'role':'agent','content':{'type':'output','data':{'type':'assistant','message':{'content':[{'type':'text','text':text}]}}}}}
 def test_publish_managed_includes_paused_projects_without_runtime_actions(self):
  self.s.db.execute("UPDATE projects SET status='paused',manual=1 WHERE id=?",(self.p,))
  with unittest.mock.patch.object(self.api,'call',side_effect=AssertionError('Publishing must not call a runtime')):
   out=self.s.publish_managed(self.tmp.name+'/managed.json')
   self.assertEqual(out['s']['worker'],'w');self.assertEqual(out['s']['projectStatus'],'paused');self.assertTrue(out['s']['manualHold'])
   self.assertEqual(self.s.project(self.p)['status'],'paused');self.assertEqual(self.s.project(self.p)['manual'],1)
   self.s.set('watch:w',json.dumps({'mode':'off','note':'user pause'}))
   self.assertNotIn('s',self.s.publish_managed(self.tmp.name+'/managed.json'))
   self.s.set('watch:w',json.dumps({'mode':'events','note':''}))
   self.s.db.execute("UPDATE workers SET state='released' WHERE id='w'")
   self.assertNotIn('s',self.s.publish_managed(self.tmp.name+'/managed.json'))
  self.assertEqual(self.api.sent,[])
 def test_published_oversight_requires_recent_observation_and_records_holds_errors_and_owner(self):
  now=time.time();self.s.set('manager','manager-session')
  self.s._oversight_owners={'manager-session':'paused'}
  self.s.set('observation:w',json.dumps({'observedAt':now}))
  with unittest.mock.patch.object(self.api,'call',side_effect=AssertionError('Publication must never invoke runtime')):
   state=lambda:self.s.publish_managed(self.tmp.name+'/managed.json')['s']['oversight']
   self.assertEqual(state()['state'],'watching');self.assertEqual(state()['ownerState'],'paused')
   self.assertEqual(state()['observedAt'],now);self.assertTrue(state()['notifications'])
   self.s.set('scan-error:w',now);self.assertEqual(state()['state'],'attention')
   self.s.db.execute("DELETE FROM settings WHERE key='scan-error:w'")
   self.s.set('observation:w',json.dumps({'observedAt':now-121}));self.assertEqual(state()['state'],'unknown')
   self.s.set('asleep:w','1');self.assertEqual(state()['state'],'idle')
   self.s.db.execute("UPDATE projects SET manual=1 WHERE id=?",(self.p,));self.assertEqual(state()['state'],'held')
  self.assertEqual(self.api.sent,[])

 def test_observation_failure_queues_one_warning_per_episode_without_replaying_work(self):
  with unittest.mock.patch.object(self.s,'scan',side_effect=RuntimeError('probe unavailable')):
   self.s.reconcile();self.s.reconcile()
  events=self.s.rows("SELECT * FROM events WHERE kind='observer-error'")
  self.assertEqual(len(events),1);self.assertIsNotNone(self.s.get('scan-error:w'));self.assertEqual(self.api.sent,[])
  self.s.reconcile();self.assertIsNone(self.s.get('scan-error:w'));self.assertEqual(self.api.sent,[])

 def test_manual_stop_blocks_usage_recovery_even_after_restart(self):
  self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.s.db.execute('UPDATE usage_recovery SET due=0')
  self.api.session['manualPausedAt']=123
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.s.recover_usage();self.assertEqual(self.api.sent,[])
  self.assertEqual(self.s.one('SELECT * FROM usage_recovery')['state'],'waiting')
 def test_manual_stop_beats_old_user_requested_assignment(self):
  self.api.session['manualPausedAt']=123
  with self.assertRaisesRegex(ValueError,'manually paused'):
   self.s.dispatch({'op':'assign','worker':'w','text':'old authorization','key':'old','user_requested':True})
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.s.rows('SELECT * FROM assignments'),[])
 def test_paused_manager_never_wakes_or_reconnects(self):
  self.s.set('manager','s');self.s.set('manager_offline_since',0)
  self.s.event('reminder',self.p,'w','check-in','old reminder')
  self.api.session['manualPausedAt']=123
  for active in (True,False):
   self.api.session['active']=active
   self.s.notify_manager()
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.s.rows('SELECT * FROM outbox'),[])
  self.assertIsNone(self.s.one('SELECT * FROM events')['wake_id'])
 def test_paused_manager_blocks_worker_recovery_and_control(self):
  self.s.set('manager','s');self.api.session['manualPausedAt']=123
  with self.assertRaisesRegex(ValueError,'Manager manually paused'):
   self.s.dispatch({'op':'assign','worker':'w','text':'old request','key':'old','user_requested':True})
  self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.s.db.execute('UPDATE usage_recovery SET due=0')
  self.s.recover_usage();self.assertEqual(self.api.sent,[])
 def test_project_resume_does_not_clear_chat_manual_stop(self):
  self.api.session['manualPausedAt']=123
  self.s.dispatch({'op':'resume','project':self.p})
  with self.assertRaisesRegex(ValueError,'manually paused'):self.assign()
  self.assertEqual(self.api.session['manualPausedAt'],123)
 def test_explicit_manager_resume_requires_current_user_basis_and_exact_target(self):
  self.api.session['manualPausedAt']=123;self.s.set('manager','s')
  for request in ({'op':'resume_chat','requested_at':time.time(),'worker':'w'}, {'op':'resume_chat','requested_at':time.time(),'worker':'w','user_requested':True}, {'op':'resume_chat','requested_at':time.time(),'worker':'w','basis':'old standing permission'}):
   with self.assertRaisesRegex(ValueError,'current explicit'):self.s.dispatch(request)
  original=self.api.call;calls=[]
  def api(path,data=None,method=None):
   calls.append((path,data))
   if path=='/sessions/s/manual-resume':self.api.session['manualPausedAt']=None;return {'ok':True}
   return original(path,data,method)
  self.api.call=api
  result=self.s.dispatch({'op':'resume_chat','requested_at':time.time(),'worker':'w','session':'s','user_requested':True,'basis':'User now explicitly asks to continue this chat'})
  self.assertTrue(result['pauseCleared']);self.assertEqual(self.api.sent,[])
  self.assertEqual(calls[-1][1]['origin'],'manager');self.assertEqual(calls[-1][1]['basis'],'User now explicitly asks to continue this chat')
  self.assertIsNone(self.api.session['manualPausedAt'])
  with self.assertRaisesRegex(ValueError,'registered'):self.s.dispatch({'op':'resume_chat','requested_at':time.time(),'session':'other','user_requested':True,'basis':'User asks to resume other'})
 def test_explicit_resume_chat_refuses_duplicate_controller(self):
  self.api.session.update(active=False,manualPausedAt=1)
  original=self.api.call;calls=[]
  def api(path,data=None,method=None):
   calls.append(path)
   if path.endswith('/manual-resume'):return {'ok':True}
   return original(path,data,method)
  self.api.call=api
  with unittest.mock.patch('supervisor.live_controller',return_value=True):
   with self.assertRaisesRegex(ValueError,'duplicate'):self.s.dispatch({'op':'resume_chat','requested_at':time.time(),'worker':'w','user_requested':True,'basis':'User explicitly asks restart'})
  self.assertNotIn('/sessions/s/resume',calls)
 def test_explicit_resume_rejects_native_mismatch_before_clearing(self):
  self.api.session.update(manualPausedAt=1,metadata={'codexSessionId':'different-native'})
  with self.assertRaisesRegex(ValueError,'identity mismatch'):
   self.s.dispatch({'op':'resume_chat','worker':'w','requested_at':time.time(),'user_requested':True,'basis':'Restart this exact chat'})
  self.assertEqual(self.api.session['manualPausedAt'],1)
 def test_stale_queued_resume_cannot_clear_a_manual_pause(self):
  self.api.session['manualPausedAt']=123
  with self.assertRaisesRegex(ValueError,'fresh managerctl'):
   self.s.dispatch({'op':'resume_chat','worker':'w','requested_at':time.time()-301,'user_requested':True,'basis':'Earlier user request'})
  self.assertEqual(self.api.session['manualPausedAt'],123)
 def pending_resume(self,**updates):
  self.api.session['metadata']={'codexSessionId':'native','hostPid':123,'path':'/original/launch/cwd'}
  pending={'source':'s','target':'s','native':'native','basis':'Current user asks to resume','at':time.time(),**updates}
  self.s.set('resume-pending:w',json.dumps(pending));self.s.set('host:w',99)
  return pending
 def test_resumed_native_in_other_checkout_dispatches_queue_and_steer_once(self):
  for mode in ('queue','steer'):
   with self.subTest(mode=mode):
    self.pending_resume();before=dict(self.s.worker('w'));project=dict(self.s.project(self.p))
    result=self.s.dispatch({'op':'assign','worker':'w','key':mode,'text':'fixture task','delivery_mode':mode,'user_requested':True})
    self.assertEqual(result['state'],'sent');self.assertIsNone(self.s.get('resume-pending:w'))
    self.assertEqual(self.s.get('host:w'),'123');self.assertEqual(dict(self.s.worker('w')),before)
    self.assertEqual(dict(self.s.project(self.p)),project)
    self.assertEqual(json.loads(self.s.get('resume-authorization:s'))['state'],'reconciled')
    self.assertEqual(self.api.sent[-1]['deliveryMode'],mode)
    self.s.dispatch({'op':'assign','worker':'w','key':mode,'text':'must not resend','user_requested':True})
  self.assertEqual(len(self.api.sent),2);self.assertEqual(len(self.s.rows('SELECT * FROM assignments')),2)
 def test_pending_resume_dispatch_fails_cleanly_without_assignment_or_send(self):
  self.pending_resume();self.api.session['active']=False
  budget=self.s.project(self.p)['budget']
  for mode in ('queue','steer'):
   with self.subTest(mode=mode),self.assertRaisesRegex(ValueError,'confirmation is pending.*No assignment sent'):
    self.s.dispatch({'op':'assign','worker':'w','key':mode,'text':'fixture task','delivery_mode':mode})
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.s.rows('SELECT * FROM assignments'),[])
  self.assertEqual(self.s.project(self.p)['budget'],budget);self.assertIsNotNone(self.s.get('resume-pending:w'))
 def test_resume_detects_duplicate_native_across_launch_directories(self):
  self.pending_resume();original=self.api.call
  def api(path,data=None,method=None):
   if path=='/sessions':return {'sessions':[self.api.session,{'id':'other','active':True}]}
   if path=='/sessions/other':return {'session':{'id':'other','active':True,'metadata':{'codexSessionId':'native','hostPid':456,'path':'/different/cwd'}}}
   return original(path,data,method)
  with unittest.mock.patch.object(self.api,'call',side_effect=api):
   self.assertFalse(self.s.reconcile_authorized_resume(self.s.worker('w')))
  self.assertIsNotNone(self.s.get('resume-pending:w'));self.assertEqual(self.s.get('host:w'),'99');self.assertEqual(self.api.sent,[])
 def test_resume_launch_identity_pause_and_controller_guards(self):
  for case in ('launch_path','native','worker_native','pid','pause','unmanaged'):
   with self.subTest(case=case):
    self.s.db.execute("UPDATE workers SET native_id='native' WHERE id='w'")
    self.s.db.execute("DELETE FROM settings WHERE key='unmanaged:w'")
    self.api.session['manualPausedAt']=None
    self.pending_resume(launch_path='/original/launch/cwd')
    if case=='launch_path':self.api.session['metadata']['path']='/unexpected/cwd'
    if case=='native':self.api.session['metadata']['codexSessionId']='other'
    if case=='worker_native':self.s.db.execute("UPDATE workers SET native_id='other' WHERE id='w'")
    if case=='pid':self.api.session['metadata']['hostPid']=None
    if case=='pause':self.api.session['manualPausedAt']=123
    if case=='unmanaged':self.s.set('unmanaged:w',1)
    self.assertFalse(self.s.reconcile_authorized_resume(self.s.worker('w')))
    self.assertIsNotNone(self.s.get('resume-pending:w'));self.assertEqual(self.s.get('host:w'),'99')
  self.assertEqual(self.api.sent,[])
 def test_resume_with_recorded_native_launch_path_reconciles(self):
  self.pending_resume(launch_path='/original/launch/cwd')
  self.assertTrue(self.s.reconcile_authorized_resume(self.s.worker('w')))
  self.assertEqual(self.s.worker('w')['path'],self.tmp.name);self.assertEqual(self.api.sent,[])
 def test_workspace_resume_uses_local_native_process_witness_without_prompt_or_restart(self):
  import subprocess,socket,sys
  workspace=Path(self.tmp.name)/'continuation';workspace.mkdir()
  self.s.set('workspace:w',workspace)
  process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)','native'],cwd=workspace)
  wrong_process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)','different-native'],cwd=workspace)
  try:
   for recorded in (False,True):
    self.pending_resume(launch_path=self.tmp.name,**({'workspace_path':str(workspace),'source_machine':'machine'} if recorded else {}))
    self.api.session['metadata'].update(path=str(workspace),host=socket.gethostname(),hostPid=process.pid,machineId='machine')
    before=dict(self.s.worker('w'));self.api.session['goal']={'status':'paused','objective':'preserve exact old goal'}
    self.assertTrue(self.s.reconcile_authorized_resume(self.s.worker('w')))
    audit=json.loads(self.s.get('resume-authorization:s'));self.assertEqual(audit['launch_path'],self.tmp.name)
    self.assertEqual(audit['workspaceWitness']['path'],str(workspace));self.assertEqual(audit['workspaceWitness']['pid'],process.pid)
    self.assertEqual(dict(self.s.worker('w')),before);self.assertEqual(self.api.sent,[])
    self.assertEqual(self.api.session['goal']['status'],'paused');self.assertEqual(self.s.rows('SELECT * FROM assignments'),[])
   for case in ('remote','changed_workspace','missing_native_arg','unexpected_path'):
    with self.subTest(case=case):
     self.pending_resume(launch_path=self.tmp.name,workspace_path=str(workspace),source_machine='machine')
     self.api.session['metadata'].update(path=str(workspace),host=socket.gethostname(),hostPid=process.pid,machineId='machine')
     if case=='remote':self.api.session['metadata']['host']='other-host'
     if case=='changed_workspace':self.s.set('workspace:w',self.tmp.name)
     if case=='missing_native_arg':self.api.session['metadata']['hostPid']=wrong_process.pid
     if case=='unexpected_path':self.api.session['metadata']['path']=str(Path(self.tmp.name)/'unexpected-third-cwd')
     self.assertFalse(self.s.reconcile_authorized_resume(self.s.worker('w')))
     self.assertIsNotNone(self.s.get('resume-pending:w'));self.s.set('workspace:w',workspace)
   self.assertEqual(self.api.sent,[])
  finally:
   process.terminate();process.wait(timeout=5);wrong_process.terminate();wrong_process.wait(timeout=5)

 def test_unmanage_stops_polling_reminders_recovery_and_old_assignments(self):
  a=self.assign();self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.s.db.execute('UPDATE usage_recovery SET due=0')
  self.s.dispatch({'op':'remind','worker':'w','after_minutes':1,'note':'old check'})
  self.s.event('old-wake',self.p,'w','check-in','old check')
  with unittest.mock.patch.object(self.s,'publish_managed'):
   self.s.dispatch({'op':'unmanage','worker':'w','session':'s'})
  self.assertEqual(self.s.worker('w')['state'],'released');self.assertEqual(self.s.watch('w')['mode'],'off')
  self.assertEqual(self.s.one('SELECT * FROM assignments WHERE id=?',(a['id'],))['state'],'sent')
  self.assertEqual(self.s.one('SELECT * FROM usage_recovery')['state'],'cancelled')
  self.assertEqual(self.s.one('SELECT * FROM reminders')['active'],0)
  with unittest.mock.patch.object(self.api,'call',side_effect=AssertionError('Unmanaged worker must not be polled')),unittest.mock.patch.object(self.s,'publish_managed'):
   self.s.scan(self.s.worker('w'));self.s.recover_usage();self.s.reconcile()
   with self.assertRaisesRegex(ValueError,'unmanaged'):self.s.dispatch({'op':'assign','worker':'w','key':'old','text':'continue','user_requested':True})
  self.assertEqual(len(self.api.sent),1)
 def test_unmanage_is_idempotent_and_explicit_manage_is_required(self):
  with unittest.mock.patch.object(self.s,'publish_managed'):
   self.s.dispatch({'op':'unmanage','worker':'w','session':'s'});self.s.dispatch({'op':'unmanage','worker':'w','session':'s'})
  with self.assertRaisesRegex(ValueError,'current user'):self.s.dispatch({'op':'manage','worker':'w'})
  with self.assertRaisesRegex(ValueError,'binding'):self.s.dispatch({'op':'unmanage','worker':'w','session':'wrong'})
  self.s.dispatch({'op':'manage','worker':'w','user_requested':True,'resume_management':True})
  self.assertEqual(self.s.worker('w')['state'],'managed');self.assertEqual(self.s.watch('w')['mode'],'events')
  self.assertEqual(self.api.sent,[])
 def test_reconcile_inactive_preserves_assignments_without_replay(self):
  a=self.assign();self.api.session['active']=False;self.api.session['metadata']['codexSessionId']='reconcile-test-'+str(id(self));self.s.db.execute('UPDATE workers SET native_id=?',(self.api.session['metadata']['codexSessionId'],))
  self.s.db.execute('UPDATE assignments SET seen_busy=1')
  self.s.reconcile_inactive('w','User requested recovery after controller exit')
  row=self.s.one('SELECT * FROM assignments WHERE id=?',(a['id'],))
  self.assertEqual(row['state'],'interrupted');self.assertEqual(row['text'],a['text']);self.assertEqual(len(self.api.sent),1)
 def test_reconcile_inactive_refuses_active_or_uncertain(self):
  self.assign()
  with self.assertRaisesRegex(ValueError,'active'):self.s.reconcile_inactive('w','reviewed')
  self.api.session['active']=False;self.api.session['metadata']['codexSessionId']='reconcile-test-'+str(id(self));self.s.db.execute('UPDATE workers SET native_id=?',(self.api.session['metadata']['codexSessionId'],))
  self.s.db.execute("UPDATE assignments SET state='uncertain',seen_busy=1")
  with self.assertRaisesRegex(ValueError,'Unconfirmed'):self.s.reconcile_inactive('w','reviewed')
 def test_usage_warning_does_not_restart(self):
  self.s.observe_usage_limit(self.s.worker('w'),self.limit(warning=True))
  self.assertEqual(self.s.rows('SELECT * FROM usage_recovery'),[])
 def test_usage_reset_waits_then_sends_once_across_restart(self):
  a=self.assign();self.api.invoked=[a['id']];self.api.messages=[self.limit()]
  self.s.scan(self.s.worker('w'));self.s.recover_usage()
  self.assertEqual(len(self.api.sent),1)
  self.assertEqual(self.s.one('SELECT * FROM assignments')['state'],'sent')
  self.s.db.execute('UPDATE usage_recovery SET due=0');self.s.recover_usage()
  self.assertEqual(len(self.api.sent),2)
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api);self.s.recover_usage()
  self.assertEqual(len(self.api.sent),2)
  self.api.invoked.append(self.api.sent[-1]['localId']);self.s.recover_usage()
  self.assertEqual(self.s.one('SELECT * FROM usage_recovery')['state'],'done')
 def test_usage_recovery_respects_pause_busy_and_release(self):
  self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.s.db.execute('UPDATE usage_recovery SET due=0')
  self.api.session['thinking']=True;self.s.recover_usage();self.assertEqual(self.api.sent,[])
  self.api.session['thinking']=False;self.s.dispatch({'op':'pause','project':self.p});self.s.recover_usage();self.assertEqual(self.api.sent,[])
  self.s.dispatch({'op':'resume','project':self.p});self.s.release(self.s.worker('w'),'user restart');self.s.recover_usage();self.assertEqual(self.api.sent,[])
 def test_usage_uncertain_send_never_duplicates(self):
  self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.s.db.execute('UPDATE usage_recovery SET due=0')
  old=self.api.call
  def call(path,data=None,method=None):
   if path=='/sessions/s/messages':self.api.sent.append(data);raise TimeoutError('lost')
   return old(path,data,method)
  self.api.call=call
  with self.assertRaises(TimeoutError):self.s.recover_usage()
  self.s.recover_usage();self.assertEqual(len(self.api.sent),1)
 def test_user_message_cancels_usage_recovery(self):
  self.s.observe_usage_limit(self.s.worker('w'),self.limit())
  self.api.messages=[{'id':'stop','createdAt':time.time()*1000,'content':{'role':'user','content':{'type':'text','text':'stop'}}}]
  self.s.scan(self.s.worker('w'));self.s.db.execute('UPDATE usage_recovery SET due=0');self.s.recover_usage();self.assertEqual(self.api.sent,[])
 def test_usage_reconnect_preserves_native_identity(self):
  self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.s.db.execute('UPDATE usage_recovery SET due=0')
  self.api.session['active']=False;old=self.api.call;resumes=[]
  def call(path,data=None,method=None):
   if path=='/sessions/s/resume':
    resumes.append(path);self.api.session.update(active=True,metadata={'codexSessionId':'native','hostPid':123});return {'type':'success','sessionId':'s'}
   return old(path,data,method)
  self.api.call=call;self.s.recover_usage();self.s.recover_usage();self.s.recover_usage()
  self.assertEqual(len(resumes),1);self.assertEqual(len(self.api.sent),1);self.assertEqual(self.s.worker('w')['state'],'managed')
 def test_codex_usage_goal_and_error_back_off_without_reset(self):
  for i,data in enumerate([{'type':'thread_goal_updated','goal':{'status':'usageLimited'}},{'type':'task_failed','codex_error_info':'usageLimitExceeded','error':'quota'}]):
   m={'id':str(i),'content':{'role':'agent','content':{'type':'codex','data':data}}}
   self.s.observe_usage_limit(self.s.worker('w'),m)
   self.assertGreater(self.s.one('SELECT * FROM usage_recovery')['due'],time.time()+3500)
 def test_recovery_does_not_race_another_checkout_worker(self):
  self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.s.db.execute('UPDATE usage_recovery SET due=0')
  self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',('other',self.p,'codex','other','other',self.tmp.name,'managed',time.time()))
  self.s.db.execute("INSERT INTO assignments(id,worker,task_key,text,state,created) VALUES('other','other','other','work','sent',?)",(time.time(),))
  self.s.recover_usage();self.assertEqual(self.api.sent,[])
 def test_resume_timeout_is_never_repeated(self):
  self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.s.db.execute('UPDATE usage_recovery SET due=0');self.api.session['active']=False
  old=self.api.call;calls=[]
  def call(path,data=None,method=None):
   if path.endswith('/resume'):calls.append(path);raise TimeoutError('unknown controller')
   return old(path,data,method)
  self.api.call=call
  with self.assertRaises(TimeoutError):self.s.recover_usage()
  self.s.recover_usage();self.assertEqual(len(calls),1)
 def test_worker_permission_defaults_and_overrides(self):
  self.assertEqual(self.s.worker_permission(self.s.worker('w')),'full')
  self.assertEqual(self.s.permission_mode('claude','full'),'bypassPermissions')
  self.assertEqual(self.s.permission_mode('codex','full'),'yolo')
  calls=[];old=self.api.call
  def api(path,data=None,method=None):
   if path=='/sessions/s/permission-mode':calls.append(data);return {'ok':True}
   return old(path,data,method)
  self.api.call=api
  self.s.dispatch({'op':'permissions','worker':'w','permission':'ask'})
  self.assertEqual(calls,[{'mode':'default'}])
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.assertEqual(self.s.worker_permission(self.s.worker('w')),'ask')
  with self.assertRaisesRegex(ValueError,'full or ask'):self.s.dispatch({'op':'permissions','worker':'w','permission':'unexpected'})
 def test_failed_permission_change_does_not_claim_success(self):
  def fail(*args,**kwargs):raise RuntimeError('offline')
  self.api.call=fail
  with self.assertRaisesRegex(RuntimeError,'offline'):self.s.dispatch({'op':'permissions','worker':'w','permission':'ask'})
  self.assertIsNone(self.s.get('worker_permission:w'))
 def test_launch_passes_full_or_explicit_ask_to_runtime(self):
  self.s.db.execute("DELETE FROM workers WHERE id='w'")
  payloads=[];old=self.api.call
  def api(path,data=None,method=None):
   if path=='/machines':return {'machines':[{'id':'machine'}]}
   if path=='/machines/machine/spawn':payloads.append(data);return {'type':'success','sessionId':'s'}
   if path=='/sessions/s' and method=='PATCH':return {'ok':True}
   return old(path,data,method)
  self.api.call=api
  for runtime,profile,expected in [('claude','full','bypassPermissions'),('codex','full','yolo'),('claude','ask','default')]:
   self.s.db.execute('DELETE FROM workers')
   request={'op':'launch','project':self.p,'runtime':runtime}
   if profile=='ask':request['permission']='ask'
   worker=self.s.dispatch(request)
   self.assertEqual(payloads[-1]['permissionMode'],expected)
   self.assertEqual(self.s.worker_permission(worker),profile)
 def test_pause_blocks_followup(self):
  self.s.dispatch({'op':'pause','project':self.p})
  with self.assertRaisesRegex(ValueError,'paused'):self.assign()
  self.assertEqual(self.api.sent,[])
 def test_duplicate_assignment_survives_restart(self):
  a=self.assign();self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.assertEqual(a['id'],self.assign()['id']);self.assertEqual(len(self.api.sent),1)
 def test_unknown_delivery_never_retried(self):
  original=self.api.call
  def uncertain(path,data=None,method=None):
   if path=='/sessions/s/messages':self.api.sent.append(data);raise TimeoutError('unknown')
   return original(path,data,method)
  self.api.call=uncertain
  with self.assertRaises(TimeoutError):self.assign()
  self.assertEqual(self.assign()['state'],'uncertain');self.assertEqual(len(self.api.sent),1)
  with self.assertRaisesRegex(ValueError,'Unresolved'):self.assign('another')
 def test_completion_is_not_objective_completion(self):
  a=self.assign();self.api.invoked=[a['id']]
  self.api.messages=[{'id':'ready','seq':2,'createdAt':time.time()*1000,'content':{'role':'agent','content':{'type':'event','data':{'type':'ready'}}}}]
  self.s.scan(self.s.worker('w'));self.s.scan(self.s.worker('w'))
  self.assertEqual(self.s.project(self.p)['status'],'running')
  self.assertEqual(self.s.one('SELECT * FROM assignments')['state'],'finished')
  self.assertEqual(len(self.s.rows('SELECT * FROM events')),1)
 def test_user_can_steer_a_managed_worker_without_a_hold(self):
  self.api.messages=[{'id':'user','seq':2,'createdAt':time.time()*1000,'localId':'human','content':{'role':'user','content':{'type':'text','text':'change plan'}}}]
  a=self.assign()  # monitoring and assignment continue
  self.assertEqual(a['state'],'sent')
  p=self.s.project(self.p);self.assertEqual((p['manual'],p['status']),(0,'running'))
  self.assertEqual([(e['kind'],'Direct user input' in e['detail']) for e in self.s.dispatch({'op':'events'})],[('user-input',True)])
 def test_dropped_ready_recovered_from_native_terminal_record(self):
  from datetime import datetime,timezone
  a=self.assign();self.api.invoked=[a['id']];original=self.s.dispatch
  def dispatch(o):
   if o['op']=='read_native':return {'tail':[{'timestamp':datetime.now(timezone.utc).isoformat(),'type':'event_msg','payload':{'type':'task_complete'}}]}
   return original(o)
  self.s.dispatch=dispatch
  self.s.scan(self.s.worker('w'))
  self.assertEqual(self.s.one('SELECT * FROM assignments')['state'],'finished')
  self.assertEqual(self.s.project(self.p)['status'],'running')
 def test_busy_worker_refused(self):
  self.api.session['thinking']=True
  with self.assertRaisesRegex(ValueError,'busy'):self.assign()
 def test_native_identity_cannot_silently_change(self):
  self.api.session['metadata']={'codexSessionId':'different-history'}
  with self.assertRaisesRegex(ValueError,'identity mismatch'):self.s.scan(self.s.worker('w'))
  self.assertEqual(self.s.worker('w')['native_id'],'native')
  self.assertEqual(self.s.project(self.p)['status'],'paused')
 def test_unresolved_adoption_cannot_spawn_twice(self):
  self.s.db.execute("UPDATE workers SET session=NULL,state='adopting' WHERE id='w'")
  with self.assertRaisesRegex(ValueError,'unresolved'):self.s.adopt('w')
 def other_editor(self,scope='checkout'):
  # A second worker launched from the same directory with unresolved work.
  self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',('other',self.p,'claude','other-native','s-other',self.tmp.name,'managed',time.time()))
  self.s.db.execute("INSERT INTO assignments(id,worker,task_key,text,state,created,scope) VALUES('busy','other','busy-key','edit files','sent',?,?)",(time.time(),scope))
 def test_separate_workspaces_allow_shared_launch_directory_and_persist(self):
  from pathlib import Path
  left=Path(self.tmp.name)/'left';right=Path(self.tmp.name)/'right';left.mkdir();right.mkdir()
  self.other_editor();self.s.set('workspace:other',str(right))
  self.s.dispatch({'op':'workspace','worker':'w','path':str(left)})
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  a=self.assign();self.assertIn('Working checkout: '+str(left),a['text'])
  with self.assertRaisesRegex(ValueError,'Unresolved checkout'):self.s.dispatch({'op':'workspace','worker':'w','path':str(right)})
 def test_workspace_aliases_and_nested_directories_still_exclude(self):
  from pathlib import Path
  root=Path(self.tmp.name);nested=root/'nested';nested.mkdir();alias=root/'alias';alias.symlink_to(nested)
  self.other_editor();self.s.set('workspace:other',str(nested))
  for path in (root,nested,alias):
   self.s.dispatch({'op':'workspace','worker':'w','path':str(path)})
   with self.assertRaisesRegex(ValueError,'using this checkout'):self.assign()
 def test_usage_recovery_uses_actual_workspaces(self):
  from pathlib import Path
  left=Path(self.tmp.name)/'left';right=Path(self.tmp.name)/'right';left.mkdir();right.mkdir()
  self.other_editor();self.s.set('workspace:other',str(right))
  self.s.dispatch({'op':'workspace','worker':'w','path':str(left)})
  self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.s.db.execute('UPDATE usage_recovery SET due=0')
  self.s.recover_usage();self.assertEqual(len(self.api.sent),1)
 def test_cancel_queued_keeps_invoked_or_uncertain_assignments(self):
  a=self.assign();old=self.api.call;status='busy'
  def call(path,data=None,method=None):
   if method=='DELETE':return {'status':status}
   return old(path,data,method)
  self.api.call=call
  self.s.dispatch({'op':'cancel_queued','assignment':a['id']})
  self.assertEqual(self.s.one('SELECT state FROM assignments')['state'],'sent')
  status='cancelled';self.s.dispatch({'op':'cancel_queued','assignment':a['id']})
  self.assertEqual(self.s.one('SELECT state FROM assignments')['state'],'cancelled')
 def test_busy_worker_cannot_move_workspace(self):
  self.api.session['thinking']=True
  with self.assertRaisesRegex(ValueError,'busy'):self.s.dispatch({'op':'workspace','worker':'w','path':self.tmp.name})
 def test_concurrent_checkout_editors_still_denied(self):
  self.other_editor()
  with self.assertRaisesRegex(ValueError,'using this checkout'):self.assign()
  with self.assertRaisesRegex(ValueError,'using this checkout'):self.s.dispatch({'op':'assign','worker':'w','text':'edit','key':'k2','scope':'checkout'})
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.s.project(self.p)['budget'],8)
 def test_external_only_work_allowed_beside_checkout_editor(self):
  self.other_editor();self.s.dispatch({'op':'claim','project':self.p,'resource':'extra-pc/GPU0'})
  a=self.s.dispatch({'op':'assign','worker':'w','text':'send outreach emails','key':'mail','scope':'external'})
  self.assertEqual((a['state'],a['scope']),('sent','external'))
  sent=self.api.sent[0]['text']
  self.assertIn('External-only',sent);self.assertIn(self.tmp.name,sent);self.assertIn('extra-pc/GPU0',sent)
  self.assertEqual(self.s.one("SELECT state FROM assignments WHERE id='busy'")['state'],'sent')
 def test_external_assignment_does_not_hold_checkout(self):
  self.other_editor('external')
  self.assertEqual(self.assign()['scope'],'checkout')
 def test_external_scope_keeps_other_guards(self):
  self.other_editor()
  ext=lambda key:self.s.dispatch({'op':'assign','worker':'w','text':'email','key':key,'scope':'external'})
  with self.assertRaisesRegex(ValueError,'scope must be'):self.s.dispatch({'op':'assign','worker':'w','text':'x','key':'bad','scope':'none'})
  self.api.session['thinking']=True
  with self.assertRaisesRegex(ValueError,'busy'):ext('busy-try')
  self.api.session['thinking']=False
  self.s.dispatch({'op':'pause','project':self.p})
  with self.assertRaisesRegex(ValueError,'paused'):ext('paused-try')
  self.s.dispatch({'op':'resume','project':self.p});self.s.db.execute('UPDATE projects SET manual=1 WHERE id=?',(self.p,))
  with self.assertRaisesRegex(ValueError,'held'):ext('held-try')
  self.s.db.execute('UPDATE projects SET manual=0 WHERE id=?',(self.p,))
  first=ext('mail')
  self.assertEqual(ext('mail')['id'],first['id'])  # same key is deduplicated, never resent
  with self.assertRaisesRegex(ValueError,'Unresolved assignment'):ext('mail-2')
  self.assertEqual(len(self.api.sent),1)
 def test_scope_column_migrates_existing_database(self):
  import sqlite3
  path=self.tmp.name+'/old.db';c=sqlite3.connect(path)
  c.executescript("CREATE TABLE assignments(id TEXT PRIMARY KEY,worker TEXT,task_key TEXT UNIQUE,text TEXT,state TEXT,created REAL,seen_busy INTEGER DEFAULT 0);INSERT INTO assignments VALUES('old','x','k','t','sent',1,0);");c.commit();c.close()
  old=Supervisor(path,self.api)
  self.assertEqual(old.one("SELECT scope FROM assignments WHERE id='old'")['scope'],'checkout')
  old.db.close();Supervisor(path,self.api).db.close()  # idempotent
 def test_pause_keeps_manual_hold_and_only_resume_clears_it(self):
  self.s.db.execute('UPDATE projects SET manual=1 WHERE id=?',(self.p,))
  self.s.dispatch({'op':'pause','project':self.p})
  self.assertEqual(self.s.project(self.p)['manual'],1)
  self.s.dispatch({'op':'resume','project':self.p})
  self.assertEqual(self.s.project(self.p)['manual'],0)
 def test_complete_releases_resource_claims(self):
  self.s.dispatch({'op':'claim','project':self.p,'resource':'main/GPU0'})
  self.s.dispatch({'op':'complete','project':self.p,'checkpoint':'done'})
  self.assertEqual(self.s.rows('SELECT * FROM resources'),[])
 def test_failed_machine_check_leaves_no_stuck_launch(self):
  old=self.api.call
  self.s.db.execute("UPDATE workers SET state='registered' WHERE id='w'")
  self.api.call=lambda path,data=None,method=None:{'machines':[]} if path=='/machines' else old(path,data,method)
  with self.assertRaisesRegex(ValueError,'exactly one'):self.s.dispatch({'op':'launch','project':self.p,'runtime':'codex'})
  self.assertEqual(self.s.rows("SELECT id FROM workers WHERE state='launching'"),[])
 def test_identity_mismatch_wakes_manager_once(self):
  self.api.session['metadata']={'codexSessionId':'different-history'}
  for _ in range(3):
   with self.assertRaises(ValueError):self.s.scan(self.s.worker('w'))
  self.assertEqual([e['kind'] for e in self.s.dispatch({'op':'events'})],['identity-mismatch'])
 def test_steering_note_once_per_gap_between_manager_assignments(self):
  now=time.time()*1000+1000;user={'role':'user','content':{'type':'text','text':'hi'}}
  goal={'role':'user','content':{'type':'text','text':'/goal finish the deck'}}
  self.api.messages=[{'id':'m1','createdAt':now,'content':goal},{'id':'m2','createdAt':now+1,'content':user}]
  self.s.scan(self.s.worker('w'))
  events=self.s.dispatch({'op':'events'})
  self.assertEqual([e['kind'] for e in events],['user-input']);self.assertIn('active-turn steering',events[0]['detail']);self.assertNotIn('You set a goal',events[0]['detail'])
  self.assign('k1')
  self.api.messages.append({'id':'m3','createdAt':now+2,'content':user});self.s.scan(self.s.worker('w'))
  self.assertEqual([e['kind'] for e in self.s.dispatch({'op':'events'})],['user-input','user-input'])
 def test_user_stop_and_restart_releases_worker(self):
  self.api.session['metadata']={'hostPid':100}
  self.s.scan(self.s.worker('w'))
  self.api.session['active']=False;self.s.scan(self.s.worker('w'))  # stopped: still monitored
  self.assertEqual(self.s.worker('w')['state'],'managed')
  self.api.session['active']=True;self.s.scan(self.s.worker('w'))  # hub reconnect: same process
  self.assertEqual(self.s.worker('w')['state'],'managed')
  self.assign('before')
  self.api.session['metadata']={'hostPid':200};self.s.scan(self.s.worker('w'))
  self.assertEqual(self.s.worker('w')['state'],'released')
  self.assertEqual(self.s.one("SELECT state FROM assignments WHERE task_key='before'")['state'],'released')
  self.assertIn('released',[e['kind'] for e in self.s.dispatch({'op':'events'})])
  with self.assertRaisesRegex(ValueError,'no longer managed'):self.assign('after')
  self.s.reconcile()  # released workers are not scanned again
  self.assertEqual(self.s.dispatch({'op':'manage','worker':'w'})['state'],'managed')
  self.s.scan(self.s.worker('w'));self.assertEqual(self.s.worker('w')['state'],'managed')
 def test_restart_as_new_hapi_session_releases_worker(self):
  self.api.session['active']=False;old=self.api.call
  def api(path,data=None,method=None):
   if path=='/sessions':return {'sessions':[{'id':'s2','active':True,'metadata':{'path':self.tmp.name}}]}
   if path=='/sessions/s2':return {'session':{'id':'s2','active':True,'metadata':{'codexSessionId':'native'}}}
   return old(path,data,method)
  self.api.call=api;self.s.scan(self.s.worker('w'))
  self.assertEqual(self.s.worker('w')['state'],'released')
 def test_goal_assignment_waits_for_goal_completion(self):
  self.s.db.execute("UPDATE workers SET runtime='codex' WHERE id='w'")
  a=self.s.dispatch({'op':'assign','worker':'w','text':'ship the release','key':'g1','goal':True})
  self.assertTrue(self.api.sent[-1]['text'].startswith('/goal ship the release'));self.assertEqual(a['goal'],1)
  self.api.invoked=[a['id']];now=time.time()*1000+500
  ready={'role':'agent','content':{'type':'event','data':{'type':'ready'}}}
  goal=lambda status,i:{'id':'g'+str(i),'seq':i,'createdAt':now+i,'content':{'role':'agent','content':{'type':'codex','data':{'type':'thread_goal_updated','goal':{'status':status}}}}}
  self.api.messages=[{'id':'r1','seq':1,'createdAt':now,'content':ready},goal('active',2)]
  self.s.scan(self.s.worker('w'))
  self.assertEqual(self.s.one("SELECT state FROM assignments WHERE id=?",(a['id'],))['state'],'sent')  # first turn ended, goal active
  self.api.messages.append(goal('complete',3));self.s.scan(self.s.worker('w'))
  self.assertEqual(self.s.one("SELECT state FROM assignments WHERE id=?",(a['id'],))['state'],'finished')
  self.assertIn('Goal complete',[e['detail'] for e in self.s.dispatch({'op':'events'}) if e['kind']=='finished'][0])
 def test_claude_goal_completion_from_native_goal_status(self):
  self.s.db.execute("UPDATE workers SET runtime='claude' WHERE id='w'")
  a=self.s.dispatch({'op':'assign','worker':'w','text':'tidy docs','key':'g2','goal':True})
  self.api.invoked=[a['id']]
  stamp=lambda dt:time.strftime('%Y-%m-%dT%H:%M:%S',time.gmtime(time.time()+dt))+'.000Z'
  tail=[{'type':'attachment','timestamp':stamp(1),'attachment':{'type':'goal_status','met':False}}]
  self.s.dispatch=(lambda orig:(lambda o:{'tail':tail} if o['op']=='read_native' else orig(o)))(self.s.dispatch)
  self.s.scan(self.s.worker('w'));self.assertEqual(self.s.one('SELECT state FROM assignments WHERE id=?',(a['id'],))['state'],'sent')
  tail.append({'type':'attachment','timestamp':stamp(2),'attachment':{'type':'goal_status','met':True}})
  self.s.scan(self.s.worker('w'));self.assertEqual(self.s.one('SELECT state FROM assignments WHERE id=?',(a['id'],))['state'],'finished')
 def test_slash_commands_are_verbatim_even_with_context_annotations(self):
  self.s.set('workspace:w',self.tmp.name+'/different-checkout')
  self.s.db.execute('INSERT INTO resources VALUES(?,?)',('test-gpu',self.p))
  for scope in ('checkout','external'):
   for command in ('/goal clear','/goal','/compact','/clear','/goal build a small feature'):
    with self.subTest(scope=scope,command=command):
     a=self.s.dispatch({'op':'assign','worker':'w','text':command,'key':scope+command,'scope':scope,'user_requested':True})
     self.assertEqual(self.api.sent[-1]['text'],command)
     self.assertEqual(a['text'],command)
 def test_goal_flag_rejects_slash_commands_without_delivery(self):
  with self.assertRaisesRegex(ValueError,'not a slash command'):
   self.s.dispatch({'op':'assign','worker':'w','text':'/goal clear','key':'nested-goal','goal':True})
  self.assertEqual(self.api.sent,[])
  self.assertEqual(self.s.rows('SELECT * FROM assignments'),[])
 def test_normal_assignment_keeps_workspace_context(self):
  self.s.set('workspace:w',self.tmp.name+'/different-checkout')
  self.assign()
  self.assertIn('Working checkout: '+self.tmp.name+'/different-checkout',self.api.sent[-1]['text'])
 def test_native_claude_goal_clear_finishes_pending_goal(self):
  self.s.db.execute("UPDATE workers SET runtime='claude' WHERE id='w'")
  a=self.s.dispatch({'op':'assign','worker':'w','text':'tidy docs','key':'goal-clear-test','goal':True})
  self.api.invoked=[a['id']]
  stamp=time.strftime('%Y-%m-%dT%H:%M:%S',time.gmtime(time.time()+2))+'.000Z'
  record={'type':'user','timestamp':stamp,'message':{'content':'<command-name>/goal</command-name><command-args>clear</command-args>'}}
  self.s.dispatch=(lambda orig:(lambda o:{'tail':[record]} if o['op']=='read_native' else orig(o)))(self.s.dispatch)
  self.s.scan(self.s.worker('w'))
  self.assertEqual(self.s.one('SELECT state FROM assignments WHERE id=?',(a['id'],))['state'],'finished')
  self.assertIn('Goal cleared',[e['detail'] for e in self.s.dispatch({'op':'events'}) if e['kind']=='finished'][0])
 def test_goal_text_length_limited(self):
  with self.assertRaisesRegex(ValueError,'too long'):self.s.dispatch({'op':'assign','worker':'w','text':'x'*4000,'key':'long','goal':True})
  self.assertEqual(self.api.sent,[])
 def test_attach_refuses_users_hapi_chat_without_explicit_request(self):
  import uuid
  from unittest.mock import patch
  from pathlib import Path
  nid=str(uuid.uuid4());home=Path(self.tmp.name)/'home';hist=home/'.claude/projects/x';hist.mkdir(parents=True)
  (hist/(nid+'.jsonl')).write_text(json.dumps({'sessionId':nid,'cwd':self.tmp.name})+'\n')
  old=self.api.call
  def api(path,data=None,method=None):
   if path=='/sessions':return {'sessions':[{'id':'mine','metadata':{'path':self.tmp.name}}]}
   if path=='/sessions/mine':return {'session':{'id':'mine','active':True,'metadata':{'claudeSessionId':nid}}}
   return old(path,data,method)
  self.api.call=api
  with patch('supervisor.Path.home',return_value=home):
   req={'op':'attach','project':self.p,'runtime':'claude','native_id':nid}
   with self.assertRaisesRegex(ValueError,'chat the user opened'):self.s.dispatch(req)
   self.assertEqual(self.s.dispatch(dict(req,user_requested=True))['native_id'],nid)
 def test_compact_batch_names_project_worker_and_task(self):
  self.s.db.execute("INSERT INTO assignments(id,worker,task_key,text,state,created) VALUES('a1','w','outreach-v1','x','finished',1)")
  self.s.event('result:a1',self.p,'w','finished','Turn ended; inspect results before follow-up or objective completion.')
  self.s.event('approval:r1',self.p,'w','input-required','Approval/input pending in worker HAPI interface; manager cannot approve. '+'x'*300)
  self.s.set('manager','s');self.s.notify_manager()
  text=self.api.sent[-1]['text'];lines=text.split('\n')
  self.assertTrue(lines[0].startswith('[supervisor update; not a user instruction] 2 events; ack ids'))
  self.assertRegex(lines[1],r'^#\d+ finished · test · codex:w · task=outreach-v1 · Turn ended')
  self.assertLessEqual(len(lines[2]),260);self.assertNotIn('event_key',text);self.assertNotIn('wake_id',text)
 def test_reconcile_failures_are_isolated_and_lost_batches_are_released(self):
  import supervisor
  self.s.event('result:x',self.p,'w','finished','done')
  self.s.db.execute("INSERT INTO outbox VALUES('old','gone','t','uncertain',?)",(time.time()-supervisor.LOST_AFTER-1,))
  self.s.db.execute("UPDATE events SET wake_id='old'")
  self.s.db.execute("INSERT INTO outbox VALUES('broken','missing-session','t','sent',?)",(time.time(),))
  old=self.api.call
  def api(path,data=None,method=None):
   if path=='/sessions/missing-session/messages/queued-state':raise ValueError('session not found')
   if path=='/sessions/gone/messages/queued-state':return {'queuedLocalIds':[],'indeterminateLocalIds':[],'invokedLocalMessages':[]}
   return old(path,data,method)
  self.api.call=api;self.s.set('manager','s')
  self.s.reconcile()
  self.assertEqual(self.s.one("SELECT state FROM outbox WHERE id='old'")['state'],'lost')
  self.assertEqual(len(self.api.sent),1)  # the released event still reached the manager
  self.assertIn('#',self.api.sent[0]['text'])
 def test_native_tail_is_clipped(self):
  from supervisor import clip
  record={'type':'event_msg','payload':{'type':'task_complete','big':'x'*50000,'items':['y'*3000]}}
  out=clip(record)
  self.assertEqual(out['payload']['type'],'task_complete')
  self.assertLess(len(json.dumps(out)),5000)
 def test_delivered_request_payloads_are_compacted_at_startup(self):
  import supervisor
  self.s.db.execute("INSERT INTO requests VALUES('big',?)",(json.dumps({'tail':'x'*10000}),))
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.assertEqual(json.loads(self.s.one("SELECT result FROM requests WHERE id='big'")['result'])['delivered'],True)
 def test_user_requested_message_bypasses_autonomy_limits_not_safety(self):
  self.s.dispatch({'op':'pause','project':self.p});self.s.db.execute('UPDATE projects SET budget=0 WHERE id=?',(self.p,))
  self.api.session['thinking']=True
  with self.assertRaisesRegex(ValueError,'paused'):self.assign('auto')
  a=self.s.dispatch({'op':'assign','worker':'w','text':'user says: also check X','key':'u1','user_requested':True})
  self.assertEqual(a['state'],'sent');self.assertEqual(self.s.project(self.p)['budget'],0)
  b=self.s.dispatch({'op':'assign','worker':'w','text':'and Y','key':'u2','user_requested':True})  # queued behind the first
  self.assertEqual(b['state'],'sent')
  self.assertEqual(self.s.dispatch({'op':'assign','worker':'w','text':'dup','key':'u1','user_requested':True})['id'],a['id'])
  self.api.session['active']=False
  with self.assertRaisesRegex(ValueError,'Disconnected'):self.s.dispatch({'op':'assign','worker':'w','text':'z','key':'u3','user_requested':True})
 def test_user_requested_keeps_same_checkout_protection(self):
  self.other_editor()
  with self.assertRaisesRegex(ValueError,'using this checkout'):self.s.dispatch({'op':'assign','worker':'w','text':'edit','key':'u','user_requested':True})
 def test_released_chat_only_messaged_on_user_request(self):
  self.s.db.execute("UPDATE workers SET state='released' WHERE id='w'")
  with self.assertRaisesRegex(ValueError,'no longer managed'):self.assign()
  self.assertEqual(self.s.dispatch({'op':'assign','worker':'w','text':'x','key':'r','user_requested':True})['state'],'untracked')
 def test_interrupt_can_leave_project_running(self):
  calls=[];old=self.api.call
  self.api.call=lambda path,data=None,method=None:calls.append(path) or {'ok':True} if path.endswith('/abort') else old(path,data,method)
  self.s.dispatch({'op':'interrupt','worker':'w','pause':False})
  self.assertEqual(self.s.project(self.p)['status'],'running');self.assertEqual(calls,['/sessions/s/abort'])
 def test_quiet_watch_holds_events_for_on_demand_digest(self):
  self.s.set('manager','s')
  self.assertEqual(self.s.dispatch({'op':'watch','worker':'w','mode':'quiet','note':'hands off until I ask'})[0]['mode'],'quiet')
  self.s.event('result:x',self.p,'w','finished','Turn ended')
  self.s.notify_manager();self.assertEqual(self.api.sent,[])
  self.api.messages=[{'id':'a1','seq':1,'createdAt':1,'content':{'role':'agent','content':{'type':'codex','data':{'type':'message','message':'Deck is 80% done'}}}}]
  d=self.s.dispatch({'op':'digest','worker':'w'})
  self.assertEqual([e['kind'] for e in d['events']],['finished']);self.assertEqual(d['latest_reply'],'Deck is 80% done')
  self.s.dispatch({'op':'watch','worker':'w','mode':'events','note':'only tell me about breakthroughs'})
  self.s.notify_manager()
  self.assertIn('user preference: only tell me about breakthroughs',self.api.sent[0]['text'])
 def test_off_watch_stops_scanning(self):
  scanned=[];self.s.scan=lambda w,**kwargs:scanned.append(w['id']);self.s.notify_manager=lambda:None
  self.s.reconcile();self.assertEqual(scanned,['w'])
  self.s.dispatch({'op':'watch','project':self.p,'mode':'off'})
  self.s.reconcile();self.assertEqual(scanned,['w'])
  with self.assertRaisesRegex(ValueError,'mode must be'):self.s.dispatch({'op':'watch','worker':'w','mode':'sometimes'})
 def test_reminders_wake_even_quiet_chats_and_repeat(self):
  self.s.set('manager','s');self.s.dispatch({'op':'watch','worker':'w','mode':'quiet'})
  with self.assertRaisesRegex(ValueError,'at least 5'):self.s.dispatch({'op':'remind','in_minutes':0,'every_minutes':1,'worker':'w'})
  r=self.s.dispatch({'op':'remind','in_minutes':0,'every_minutes':120,'worker':'w','note':'check deck progress'})
  self.s.deliver_reminders();self.s.notify_manager()
  self.assertIn('check-in',self.api.sent[0]['text']);self.assertIn('check deck progress',self.api.sent[0]['text'])
  nxt=self.s.one('SELECT due FROM reminders WHERE id=?',(r['id'],))['due'];self.assertGreater(nxt,time.time()+119*60)
  self.s.dispatch({'op':'cancel_reminder','id':r['id']});self.assertEqual(self.s.dispatch({'op':'reminders'}),[])
 def public_progress(self,text='Building terrain preview',step='Running erosion benchmark'):
  self.api.messages=[{'id':'progress1','seq':10,'createdAt':time.time()*1000,'content':{'role':'agent','content':{'type':'output','data':{'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':text},{'type':'tool_use','name':'Bash','input':{'description':step,'command':'secret command excluded'}}]}}}}}]
 def test_progress_ignores_usage_warning_and_tool_result_text(self):
  self.public_progress()
  for seq,kind,text in [(11,'assistant','Claude AI usage limit warning|123|83|seven_day'),(12,'user','tool output is not a progress report')]:
   self.api.messages.append({'id':str(seq),'seq':seq,'content':{'role':'agent','content':{'type':'output','data':{'type':kind,'message':{'role':kind,'content':[{'type':'text','text':text}]}}}}})
  self.assertEqual(self.s.progress_snapshot(self.s.worker('w'))['latest_reply'],'Building terrain preview')
 def separate_manager(self):
  self.manager_session={'id':'manager','active':True,'thinking':False,'agentState':{}}
  original=self.api.call
  def call(path,data=None,method=None):
   if path=='/sessions/manager':return {'session':self.manager_session}
   if path=='/sessions/manager/messages':self.api.sent.append(data);return {'ok':True}
   return original(path,data,method)
  self.api.call=call;self.s.set('manager','manager')
 def progress_reminder(self):
  return self.s.dispatch({'op':'remind','worker':'w','in_minutes':0,'every_minutes':10,'progress':True})
 def due(self,r):self.s.db.execute('UPDATE reminders SET due=? WHERE id=?',(time.time()-1,r['id']))
 def test_manage_busy_is_read_only_idempotent_and_preserves_completed_scope(self):
  self.api.session['thinking']=True;self.s.db.execute("UPDATE projects SET status='completed',manual=1")
  self.s.dispatch({'op':'watch','worker':'w','mode':'off'})
  calls=[];old=self.api.call
  def read_only(path,data=None,method=None):
   self.assertIsNone(data);calls.append(path);return old(path,data,method)
  self.api.call=read_only
  for _ in range(2):self.assertTrue(self.s.dispatch({'op':'manage','worker':'w'})['observationOnly'])
  self.assertEqual(calls,['/sessions/s','/sessions/s']);self.assertEqual(self.s.project(self.p)['status'],'completed');self.assertEqual(self.s.project(self.p)['manual'],1);self.assertEqual(self.s.watch('w')['mode'],'off');self.assertEqual(self.api.sent,[])
 def test_manage_released_busy_chat_only_restores_observation(self):
  self.s.db.execute("UPDATE workers SET state='released'");self.s.db.execute("UPDATE projects SET status='paused',manual=1")
  self.api.session['thinking']=True;self.api.session['goal']={'status':'active','objective':'Keep direct task'}
  old=self.api.call
  def get_only(path,data=None,method=None):
   self.assertIsNone(data);self.assertIn(method,(None,'GET'));return old(path,data,method)
  self.api.call=get_only
  self.s.dispatch({'op':'manage','worker':'w'})
  self.assertEqual(self.s.worker('w')['state'],'managed');self.assertEqual(self.api.session['goal']['objective'],'Keep direct task');self.assertTrue(self.api.session['thinking']);self.assertEqual(self.s.project(self.p)['status'],'paused');self.assertEqual(self.s.project(self.p)['manual'],1);self.assertEqual(self.api.sent,[])
 def test_progress_completed_tracker_busy_worker_without_assignments(self):
  self.api.session['thinking']=True;self.s.db.execute("UPDATE projects SET status='completed'");self.public_progress()
  r=self.progress_reminder();self.s.deliver_reminders()
  e=self.s.one("SELECT * FROM events WHERE kind='progress'");snap=json.loads(e['detail'])
  self.assertEqual(snap['status'],'running');self.assertEqual(snap['latest_reply'],'Building terrain preview');self.assertIn('erosion benchmark',snap['latest_tool']);self.assertNotIn('secret',e['detail']);self.assertEqual(self.api.sent,[])
  self.assertEqual(self.s.project(self.p)['status'],'completed');self.assertEqual(self.s.rows('SELECT * FROM assignments'),[])
  self.due(r);self.s.deliver_reminders();self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='progress'")),1)
 def test_progress_coalesces_changes_respects_cadence_and_restarts(self):
  self.api.session['thinking']=True;self.public_progress();r=self.progress_reminder();self.s.deliver_reminders()
  self.public_progress('Preview rendered');self.s.deliver_reminders();self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='progress'")),1)
  self.due(r);self.s.deliver_reminders();pending=self.s.rows("SELECT * FROM events WHERE kind='progress' AND acked=0");self.assertEqual(len(pending),1);self.assertIn('Preview rendered',pending[0]['detail'])
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api);self.due(r);self.s.deliver_reminders();self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='progress'")),2)
 def test_progress_preserves_pause_release_off_manual_and_quiet(self):
  self.api.session['thinking']=True;self.public_progress();r=self.progress_reminder()
  for sql in ["UPDATE projects SET status='paused'", "UPDATE projects SET status='running',manual=1", "UPDATE projects SET manual=0", "UPDATE workers SET state='released'"]:
   self.s.db.execute(sql)
   if sql=="UPDATE projects SET manual=0":self.s.dispatch({'op':'watch','worker':'w','mode':'off'})
   self.due(r);self.s.deliver_reminders();self.assertEqual(self.s.rows("SELECT * FROM events WHERE kind='progress'"),[])
  self.s.db.execute("UPDATE workers SET state='managed'");self.s.dispatch({'op':'watch','worker':'w','mode':'quiet'});self.due(r);self.s.deliver_reminders();self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='progress'")),1)
 def test_existing_reminder_upgrade_and_repeated_config_do_not_duplicate(self):
  r=self.s.dispatch({'op':'remind','worker':'w','in_minutes':10,'every_minutes':10,'note':'Keep scope'})
  upgraded=self.s.dispatch({'op':'remind','id':r['id'],'worker':'w','progress':True})
  self.assertEqual(upgraded['due'],r['due']);self.assertEqual(upgraded['note'],'Keep scope');self.assertEqual(upgraded['every'],600)
  again=self.s.dispatch({'op':'remind','worker':'w','progress':True,'every_minutes':15})
  self.assertEqual(again['id'],r['id']);self.assertEqual(len(self.s.dispatch({'op':'reminders'})),1)
 def test_progress_does_not_interrupt_busy_manager_or_duplicate_pending_wake(self):
  self.api.session['thinking']=True;self.public_progress();self.separate_manager();r=self.progress_reminder();self.s.deliver_reminders()
  self.manager_session['thinking']=True;self.s.notify_manager();self.assertEqual(self.api.sent,[])
  self.manager_session['thinking']=False;self.s.notify_manager();self.assertEqual(len(self.api.sent),1);self.assertIn('erosion benchmark',self.api.sent[0]['text'])
  self.public_progress('New preview');self.due(r);self.s.deliver_reminders();self.s.notify_manager();self.assertEqual(len(self.api.sent),1)
  self.s.db.execute("UPDATE outbox SET state='invoked'");self.s.notify_manager();self.assertEqual(len(self.api.sent),2)
 def test_quiet_event_backlog_does_not_starve_scheduled_progress(self):
  self.api.session['thinking']=True;self.separate_manager();self.s.dispatch({'op':'watch','worker':'w','mode':'quiet'})
  for i in range(210):self.s.event('old:'+str(i),self.p,'w','finished','old quiet event')
  self.public_progress();self.progress_reminder();self.s.deliver_reminders();self.s.notify_manager()
  self.assertEqual(len(self.api.sent),1);self.assertIn('progress',self.api.sent[0]['text']);self.assertNotIn('old quiet event',self.api.sent[0]['text'])
 def observe(self,when):
  with unittest.mock.patch('supervisor.time.time',return_value=when):self.s.observe_context(self.s.worker('w'),self.api.session,self.api.messages)
 def observations(self):return self.s.rows("SELECT * FROM events WHERE kind='observation' AND acked=0")
 def set_public(self,text,seq):
  self.public_progress(text);self.api.messages[0]['seq']=seq;self.api.messages[0]['id']='m'+str(seq)
 def test_corrected_stopped_task_count_silences_kept_idle_chat_and_discards_pending_fallback(self):
  self.s.set('retention:w',json.dumps({'mode':'keep','reason':'User requests continued management through idle checkpoints'}))
  self.api.session.update(thinking=False,backgroundTaskCount=1)
  self.set_public('Nothing else is running, and the summary above is still current.',1)
  self.observe(1000);self.observe(2800);self.assertEqual(len(self.observations()),1)
  # Hub reconciles the exact successful TaskStop receipt, not a prose inference.
  self.api.session['backgroundTaskCount']=0
  self.separate_manager();self.s.notify_manager()
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.observations(),[])
  for now in (2801,4600,6400,10000):self.observe(now)
  self.assertEqual(self.observations(),[])
  self.assertEqual(self.s.worker('w')['state'],'managed')
  self.assertEqual(self.s.retention('w')['mode'],'keep')
 def test_corrected_old_background_tasks_preserve_new_inference_and_thirty_minute_fallback(self):
  self.api.session.update(thinking=False,backgroundTaskCount=2)
  self.set_public('Preview finished; GPU is free.',1);self.observe(1000)
  self.api.session['backgroundTaskCount']=0;self.observe(1100)
  self.api.session['thinking']=True;self.set_public('Working on the new revision.',2);self.observe(1200)
  self.observe(2999);self.assertEqual(self.observations(),[])
  self.observe(3000);self.assertEqual(len(self.observations()),1)
  self.assertEqual(json.loads(self.observations()[0]['detail'])['status'],'running')
  self.assertEqual(self.api.sent,[])
 def test_reporting_defaults_persist_and_keep_overrides(self):
  self.assertEqual(self.s.reporting('w'),{'enabled':True,'fallback_minutes':30,'important':True})
  self.s.dispatch({'op':'reporting','worker':'w','fallback_minutes':45,'important':False})
  self.s.dispatch({'op':'reporting','fallback_minutes':60})
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.assertEqual(self.s.reporting('w')['fallback_minutes'],45);self.assertFalse(self.s.reporting('w')['important']);self.assertEqual(self.s.reporting()['fallback_minutes'],60)
 def background_jobs(self,ids):
  self.api.session.update(thinking=False,backgroundTaskCount=len(ids),backgroundProcesses=[{'id':i,'pid':123,'checkedAt':1000000} for i in ids])
 def test_background_review_exit_notifies_once_after_conversational_turn_finished(self):
  self.background_jobs(['boot:render:1','boot:review:2'])
  self.api.session['goal']={'status':'blocked','objective':'Finish and review animations'}
  self.set_public('Animations running; final review queued.',1);self.observe(1000)
  self.background_jobs(['boot:review:2']);self.observe(1100)
  self.assertEqual(self.observations(),[])  # review still runs after renderer exits
  self.background_jobs([]);self.observe(1150)
  self.assertEqual(len(self.observations()),1,'Final background review must not silently become idle')
  notice=json.loads(self.observations()[0]['detail']);self.assertEqual(notice['status'],'idle')
  self.assertEqual(notice['changes'][0]['category'],'background-ended')
  self.assertEqual(notice['changes'][0]['previousProcesses'],['boot:review:2'])
  self.assertIn('pending final review',notice['changes'][0]['reason'])
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.api.session['goal']['status'],'blocked')
  self.s.db.execute('UPDATE events SET acked=1')
  for now in (1151,2951,6000):self.observe(now)
  self.assertEqual(self.observations(),[])
 def test_background_handoff_survives_supervisor_reload_and_waits_for_manager(self):
  self.background_jobs(['boot:job:1']);self.set_public('Final review queued.',1);self.observe(1000)
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.background_jobs([]);self.observe(1100);self.separate_manager()
  self.manager_session['manualPausedAt']=123;self.s.notify_manager();self.assertEqual(self.api.sent,[])
  self.manager_session.pop('manualPausedAt');self.manager_session['thinking']=True
  self.s.notify_manager();self.assertEqual(self.api.sent,[])
  self.manager_session['thinking']=False;self.s.notify_manager()
  self.assertEqual(len(self.api.sent),1);self.assertIn('background-ended',self.api.sent[0]['text'])
  self.s.notify_manager();self.assertEqual(len(self.api.sent),1)
 def test_native_background_exit_receipt_supports_remote_jobs_without_process_probe(self):
  self.api.session.update(thinking=False,backgroundTaskCount=1)
  self.set_public('Job running after turn.',1);self.observe(1000)
  self.api.session['backgroundTaskCount']=0
  self.api.messages.append({'id':'exit','seq':2,'content':{'role':'agent','content':{'type':'codex','data':{
   'type':'tool-call-result','callId':'render','output':{'type':'exec_command_end','exit_code':1}}}}})
  self.observe(1100);change=json.loads(self.observations()[0]['detail'])['changes'][0]
  self.assertEqual(change['category'],'background-ended');self.assertEqual(change['exits'][0]['exitCode'],1)
  self.assertEqual(self.api.sent,[])
 def test_background_count_or_prose_alone_does_not_create_handoff(self):
  self.api.session.update(thinking=False,backgroundTaskCount=1)
  self.set_public('Animations running; final review queued.',1);self.observe(1000)
  self.api.session['backgroundTaskCount']=0;self.observe(1100)
  self.assertEqual(self.observations(),[])
 def test_background_handoff_respects_waits_reporting_and_pause_without_replaying(self):
  cases=[{'manualPausedAt':123},{'active':False},{'agentState':{'requests':{'q':{'tool':'approval'}}}},{}]
  for index,wait in enumerate(cases):
   with self.subTest(wait=wait):
    self.s.db.execute('DELETE FROM settings WHERE key=?',('observation:w',));self.s.db.execute('DELETE FROM events')
    self.api.session.update(active=True,agentState={},manualPausedAt=None)
    self.s.set('reporting:w',json.dumps({'important':index!=3}))
    self.background_jobs(['boot:job:1']);self.set_public('Rendering',1);self.observe(1000)
    self.background_jobs([]);self.api.session.update(wait);self.observe(1100)
    changes=[c for e in self.observations() for c in json.loads(e['detail'])['changes']]
    self.assertEqual(any(c.get('category')=='background-ended' for c in changes),wait.get('active') is False)
    self.api.session.update(active=True,agentState={},manualPausedAt=None);self.observe(1200)
    changes=[c for e in self.observations() for c in json.loads(e['detail'])['changes']]
    self.assertEqual(any(c.get('category')=='background-ended' for c in changes),wait.get('active') is False);self.assertEqual(self.api.sent,[])
 def test_background_probe_disappearance_is_uncertain_and_new_work_gets_its_own_notice(self):
  self.background_jobs(['boot:job:1']);self.set_public('Review queued.',1);self.observe(1000)
  self.background_jobs([]);self.observe(1100)
  change=json.loads(self.observations()[0]['detail'])['changes'][0]
  self.assertEqual(change['exits'],[]);self.assertIn('no longer reported',change['reason'])
  self.s.db.execute('UPDATE events SET acked=1');self.background_jobs(['boot:job:2']);self.observe(1200)
  self.background_jobs([]);self.observe(1300)
  self.assertEqual(len(self.observations()),1)
  self.assertEqual(json.loads(self.observations()[0]['detail'])['changes'][0]['previousProcesses'],['boot:job:2'])
 def test_silent_changes_and_unchanged_fallback_only_after_thirty_minutes(self):
  self.api.session['thinking']=True;self.set_public('Working on materials',1);self.observe(1000)
  self.set_public('Working on controls',2);self.observe(1100);self.observe(2799);self.assertEqual(self.observations(),[]);self.assertEqual(self.api.sent,[])
  self.observe(2800);self.assertEqual(len(self.observations()),1);self.assertEqual(json.loads(self.observations()[0]['detail'])['reason'],'routine fallback')
  self.observe(2900);self.assertEqual(len(self.observations()),1)
  self.separate_manager()
  with unittest.mock.patch('supervisor.time.time',return_value=2900):self.s.notify_manager()
  self.api.session['thinking']=True;self.observe(4699);self.assertEqual(len(self.observations()),1)
  self.s.db.execute("UPDATE outbox SET state='invoked'");self.s.db.execute('UPDATE events SET acked=1');self.observe(4700);self.assertEqual(len(self.observations()),1)
 def test_benchmark_breakthrough_after_stagnation_and_no_duplicate_replay(self):
  self.api.session['thinking']=True;self.set_public('Benchmark stuck with no improvement',1);self.observe(1000)
  self.set_public('Benchmark dropped from 23 s to 7.5 s on the same scene',2);self.observe(1001)
  events=self.observations();self.assertEqual(len(events),1);detail=json.loads(events[0]['detail']);self.assertIn('stagnation',detail['changes'][0]['reason']);self.assertEqual(self.api.sent,[])
  self.s.db.execute('UPDATE events SET acked=1');self.set_public('Benchmark dropped from 23 s to 7.5 s on the same scene',3);self.observe(1002);self.assertEqual(self.observations(),[])
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api);self.observe(1003);self.assertEqual(self.observations(),[])
 def test_small_or_hypothetical_benchmarks_are_silent(self):
  self.api.session['thinking']=True;self.set_public('Working',1);self.observe(1000)
  for seq,text in enumerate(['Benchmark from 10 s to 9 s','We hope to go from 23 s to 7 s','If the root cause found is correct, we can fix it'],2):self.set_public(text,seq);self.observe(1000+seq)
  self.assertEqual(self.observations(),[])
 def test_streamed_message_update_same_seq_is_observed(self):
  self.api.session['thinking']=True;self.set_public('Benchmark result pending',1);self.observe(1000)
  self.set_public('Benchmark from 10 s to 2 s',1);self.observe(1001);self.assertEqual(len(self.observations()),1)
 def test_terminal_failure_notifies_once_without_terminal_message_and_survives_reload(self):
  self.api.session.update(lastFailure={'kind':'capacity','message':'Model unavailable','at':123},lastFailureAt=123)
  self.observe(1000)
  event=json.loads(self.observations()[0]['detail'])
  self.assertEqual(event['status'],'failed');self.assertEqual(event['changes'][0]['category'],'runtime-failure')
  self.assertEqual(event['failure']['kind'],'capacity')
  self.s.db.execute('UPDATE events SET acked=1')
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.observe(2000);self.assertEqual(self.observations(),[])
  self.api.session['active']=False;self.assertEqual(self.s.progress_status(self.s.worker('w'),self.api.session),'failed')
  self.api.session['manualPausedAt']=1;self.assertEqual(self.s.progress_status(self.s.worker('w'),self.api.session),'paused')
  self.api.session.update(active=True,manualPausedAt=None,thinking=True,lastFailure=None)
  self.observe(2100);self.assertEqual(self.s.progress_status(self.s.worker('w'),self.api.session),'running')
  self.api.session.update(thinking=False,lastFailure={'kind':'failed','message':'Transport failed','at':456})
  self.observe(2200);self.assertEqual(len(self.observations()),1);self.assertEqual(self.api.sent,[])
 def test_blocked_native_goal_is_authoritative_after_successful_ordinary_work(self):
  self.api.session.update(thinking=True,goal={'objective':'Release','status':'blocked','updatedAt':123})
  self.set_public('Working successfully',1);self.observe(1000)
  self.assertEqual(self.observations(),[])
  self.api.session['thinking']=False;self.set_public('Checkpoint saved; full release goal stays active.',2)
  self.observe(1100)
  event=json.loads(self.observations()[0]['detail'])
  self.assertEqual(event['nativeGoal']['status'],'blocked')
  self.assertEqual(event['nativeGoal']['objective'],'Release');self.assertEqual(event['nativeGoal']['scope'],'saved-goal-only')
  self.assertEqual(event['changes'][0]['currentTaskRelevance'],'unverified')
  self.assertEqual(event['changes'][0]['category'],'native-goal-blocked')
  self.s.db.execute('UPDATE events SET acked=1')
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.observe(1200);self.observe(4000);self.assertEqual(self.observations(),[])
  self.api.session['goal']['status']='active';self.observe(4100)
  self.api.session['goal']['status']='blocked';self.observe(4200)
  self.assertEqual(len(self.observations()),1)
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.api.session['goal']['status'],'blocked')
 def test_blocked_goal_observation_respects_manual_stop_and_real_background_work(self):
  self.api.session.update(goal={'objective':'Release','status':'blocked','updatedAt':123},manualPausedAt=1)
  self.observe(1000);self.assertEqual(self.observations(),[])
  self.api.session['manualPausedAt']=None;self.background_jobs(['render:1']);self.observe(1100)
  self.assertEqual(self.observations(),[])
  self.background_jobs([]);self.observe(1200)
  changes=[c for e in self.observations() for c in json.loads(e['detail'])['changes']]
  self.assertTrue(any(c.get('category')=='native-goal-blocked' for c in changes));self.assertEqual(self.api.sent,[])
 def test_direct_work_completion_without_assignment_waits_for_idle(self):
  self.api.session['thinking']=True;self.set_public('Working',1);self.observe(1000)
  self.api.messages.append({'id':'ready','seq':2,'content':{'role':'agent','content':{'type':'event','data':{'type':'ready'}}}})
  self.observe(1001);self.assertEqual(self.observations(),[])
  self.api.session['thinking']=False;self.observe(1002);self.assertIn('Runtime turn/goal ended',self.observations()[0]['detail'])
  self.s.db.execute('UPDATE events SET acked=1');self.observe(1003);self.assertEqual(self.observations(),[])
 def test_reporting_observes_completed_but_not_paused_released_quiet_or_off(self):
  self.api.session['thinking']=True;self.set_public('Working',1);self.s.db.execute("UPDATE projects SET status='completed'");self.observe(1000);self.observe(2800);self.assertEqual(len(self.observations()),1)
  self.s.db.execute('UPDATE events SET acked=1')
  for mode in ['quiet','off']:
   self.s.dispatch({'op':'watch','worker':'w','mode':mode});self.set_public('Breakthrough',3);self.observe(5000);self.assertEqual(self.observations(),[])
  self.s.dispatch({'op':'watch','worker':'w','mode':'events'});self.s.db.execute("UPDATE projects SET status='paused'");self.observe(8000);self.assertEqual(self.observations(),[])
  self.s.db.execute("UPDATE projects SET status='running'");self.s.db.execute("UPDATE workers SET state='released'");self.observe(10000);self.assertEqual(self.observations(),[])
 def test_reminder_override_and_migration_preserve_deadline_without_duplicates(self):
  self.api.session['thinking']=True;self.set_public('Working',1);self.observe(1000)
  r=self.s.dispatch({'op':'remind','worker':'w','at':4000,'every_minutes':30});self.observe(2800);self.assertEqual(self.observations(),[])
  self.s.dispatch({'op':'reporting','worker':'w','fallback_minutes':30,'replace_reminder':r['id']});self.assertEqual(self.s.dispatch({'op':'reminders'}),[])
  self.observe(3999);self.assertEqual(self.observations(),[]);self.observe(4000);self.assertEqual(len(self.observations()),1)
 def test_input_needed_existing_event_is_immediate_and_deduplicated(self):
  self.api.session['thinking']=True;self.api.session['agentState']={'requests':{'q':{'tool':'request_user_input','arguments':{'questions':[{'id':'x','question':'Choose target'}]}}}}
  self.s.scan(self.s.worker('w'));self.s.scan(self.s.worker('w'))
  self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='input-required'")),1);self.assertEqual(self.api.sent,[])
 def test_significant_ratio_input_need_and_hidden_reasoning_filter(self):
  self.assertIsNotNone(self.s.important_change('Measured a 4x speedup on the benchmark',[]))
  self.assertIsNone(self.s.important_change('We hope for a 4x speedup',[]))
  self.assertIsNotNone(self.s.important_change('Waiting for your approval before proceeding',[]))
  self.api.messages=[{'id':'r','seq':1,'content':{'role':'agent','content':{'type':'codex','data':{'type':'reasoning','message':'Breakthrough from 10 s to 2 s'}}}}]
  self.assertIsNone(self.s.progress_snapshot(self.s.worker('w'))['latest_reply'])
 def test_paused_context_is_not_replayed_when_observation_returns(self):
  self.api.session['thinking']=True;self.set_public('Working',1);self.observe(1000)
  self.s.db.execute("UPDATE projects SET status='paused'");self.set_public('Breakthrough from 10 s to 2 s',2);self.observe(1001)
  self.s.db.execute("UPDATE projects SET status='running'");self.observe(1002);self.assertEqual(self.observations(),[])
 def test_default_disabled_override_and_background_fallback(self):
  self.api.session['backgroundTaskCount']=1;self.set_public('Benchmark running in background',1);self.observe(1000);self.observe(2800)
  self.assertEqual(json.loads(self.observations()[0]['detail'])['status'],'background')
  self.s.db.execute('UPDATE events SET acked=1');self.s.dispatch({'op':'reporting','worker':'w','enabled':False});self.set_public('Breakthrough',2);self.observe(2801);self.assertEqual(self.observations(),[])
 def test_active_goal_turn_boundaries_are_not_completion_alerts(self):
  self.api.session['thinking']=True;self.api.session['goal']={'status':'active'};self.set_public('Working',1);self.observe(1000)
  self.api.messages.append({'id':'ready','seq':2,'content':{'role':'agent','content':{'type':'event','data':{'type':'ready'}}}})
  self.api.session['thinking']=False;self.observe(1001);self.assertEqual(self.observations(),[])
  self.api.session['goal']={'status':'complete'};self.api.messages.append({'id':'done','seq':3,'content':{'role':'agent','content':{'type':'codex','data':{'type':'thread_goal_updated','goal':{'status':'complete'}}}}});self.observe(1002)
  self.assertEqual(len(self.observations()),1)
 def test_pending_progress_is_suppressed_after_release(self):
  self.api.session['thinking']=True;self.public_progress();self.separate_manager();self.progress_reminder();self.s.deliver_reminders();self.s.db.execute("UPDATE workers SET state='released'");self.s.notify_manager();self.assertEqual(self.api.sent,[])
 def test_waiting_states_never_generate_routine_fallback(self):
  self.set_public('Working',1)
  for extra in ({}, {'thinking':True,'agentState':{'requests':{'q':{'tool':'request_user_input'}}}}, {'thinking':True,'manualPausedAt':123}, {'thinking':True,'active':False}, {'thinking':True,'agentState':{'controlledByUser':True}}):
   with self.subTest(extra=extra):
    self.api.session={'id':'s','active':True,'thinking':False,'agentState':{},**extra}
    self.observe(1000);self.observe(10000);self.observe(20000)
    self.assertEqual(self.observations(),[])
  self.assertEqual(self.api.sent,[])
 def test_usage_wait_overrides_stale_activity_and_survives_restart(self):
  self.api.session.update(thinking=True,backgroundTaskCount=3);self.set_public('Working',1);self.observe(1000)
  self.s.observe_usage_limit(self.s.worker('w'),self.limit())
  recovery=self.s.one('SELECT * FROM usage_recovery')
  self.observe(2800);self.observe(10000)
  self.assertEqual(self.observations(),[])
  self.assertEqual(self.s.progress_snapshot(self.s.worker('w'))['status'],'waiting for usage reset')
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api);self.observe(20000)
  self.assertEqual(self.observations(),[]);self.assertEqual(self.s.one('SELECT * FROM usage_recovery'),recovery)
  self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='usage-wait'")),1);self.assertEqual(self.api.sent,[])
 def test_wait_cancels_only_unsent_routine_and_resets_active_cadence(self):
  self.api.session['thinking']=True;self.set_public('Working',1);self.observe(1000);self.observe(2800)
  self.assertEqual(len(self.observations()),1)
  self.api.session['thinking']=False;self.observe(2801);self.assertEqual(self.observations(),[])
  self.s.dispatch({'op':'reporting','worker':'w','fallback_minutes':45})
  self.observe(20000);self.api.session['thinking']=True;self.observe(20001);self.assertEqual(self.observations(),[])
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.observe(22700);self.assertEqual(self.observations(),[]);self.observe(22701);self.assertEqual(len(self.observations()),1)
 def test_meaningful_waiting_change_remains_deliverable_without_repeated_ticks(self):
  self.api.session['thinking']=True;self.set_public('Working',1);self.observe(1000)
  self.api.session['thinking']=False;self.set_public('Waiting for your decision before proceeding',2);self.observe(1001)
  self.observe(10000);self.assertEqual(len(self.observations()),1)
  self.separate_manager();self.s.notify_manager();self.assertEqual(len(self.api.sent),1)
  self.observe(20000);self.s.notify_manager();self.assertEqual(len(self.api.sent),1)
 def test_delivery_rechecks_wait_even_before_next_observation(self):
  self.api.session['thinking']=True;self.set_public('Working',1);self.observe(1000);self.observe(2800)
  self.separate_manager();self.api.session['thinking']=False;self.s.notify_manager()
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.observations(),[])
 def test_delivery_rechecks_new_usage_limit_with_stale_thinking(self):
  self.api.session['thinking']=True;self.set_public('Working',1);self.observe(1000);self.observe(2800)
  self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.separate_manager();self.s.notify_manager()
  self.assertEqual(self.observations(),[]);self.assertEqual(len(self.api.sent),1)
  self.assertIn('usage-wait',self.api.sent[0]['text']);self.assertNotIn('routine fallback',self.api.sent[0]['text'])
 def test_progress_reminders_skip_waits_but_explicit_daily_check_survives(self):
  self.public_progress();r=self.progress_reminder();self.s.deliver_reminders()
  self.assertEqual(self.s.rows("SELECT * FROM events WHERE kind='progress'"),[])
  self.api.session['thinking']=True;self.due(r);self.s.deliver_reminders()
  self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='progress' AND acked=0")),1)
  self.s.observe_usage_limit(self.s.worker('w'),self.limit());self.due(r);self.s.deliver_reminders()
  self.assertEqual(self.s.rows("SELECT * FROM events WHERE kind='progress' AND acked=0"),[])
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(r['id'],))['active'],1)
  daily=self.s.dispatch({'op':'remind','worker':'w','in_minutes':0,'every_minutes':1440,'note':'Authorized daily email check'})
  self.s.deliver_reminders()
  self.assertEqual(len(self.s.rows("SELECT * FROM events WHERE kind='check-in'")),1)
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(daily['id'],))['active'],1)
  self.assertEqual(self.api.sent,[])
 def test_question_event_without_assignment_and_answers(self):
  ask={'tool':'AskUserQuestion','arguments':{'questions':[{'question':'Which venue?','options':[{'label':'SwampHacks'},{'label':'Other'}]},{'question':'Send now?','options':[{'label':'Yes'},{'label':'No'}]}]}}
  self.api.session['agentState']={'requests':{'q1':ask,'p1':{'tool':'Bash','arguments':{'command':'rm -rf build'}}}}
  self.s.scan(self.s.worker('w'))
  details=[e['detail'] for e in self.s.dispatch({'op':'events'}) if e['kind']=='input-required']
  self.assertTrue(any('Which venue? [SwampHacks/Other]' in d for d in details));self.assertTrue(any('rm -rf build' in d for d in details))
  listed=self.s.dispatch({'op':'requests','worker':'w'})
  self.assertEqual(listed[0]['questions'][0]['options'],['SwampHacks','Other'])
  calls=[];old=self.api.call
  def api(path,data=None,method=None):
   if '/permissions/' in path:calls.append((path,data));return {'ok':True}
   return old(path,data,method)
  self.api.call=api
  with self.assertRaisesRegex(ValueError,'basis'):self.s.dispatch({'op':'answer','worker':'w','request':'q1','answers':[['SwampHacks'],['No']]})
  with self.assertRaisesRegex(ValueError,'one answer list per question'):self.s.dispatch({'op':'answer','worker':'w','request':'q1','answers':[['SwampHacks']],'basis':'user: SwampHacks, not yet'})
  self.s.dispatch({'op':'answer','worker':'w','request':'q1','answers':[['SwampHacks'],'No'],'basis':'user: SwampHacks, not yet'})
  self.s.dispatch({'op':'answer','worker':'w','request':'p1','decision':'denied','basis':'user said never delete build'})
  self.assertEqual(calls,[('/sessions/s/permissions/q1/approve',{'answers':{'0':['SwampHacks'],'1':['No']}}),('/sessions/s/permissions/p1/deny',{'decision':'denied'})])
  with self.assertRaisesRegex(ValueError,'no longer pending'):self.s.dispatch({'op':'answer','worker':'w','request':'gone','decision':'approved','basis':'x'})
 def test_codex_request_user_input_answers_are_nested_by_question_id(self):
  self.api.session['agentState']={'requests':{'c1':{'tool':'request_user_input','arguments':{'questions':[{'id':'deadline','question':'Deadline?','options':[{'label':'Friday'}]}]}}}}
  calls=[];old=self.api.call
  self.api.call=lambda path,data=None,method=None:calls.append(data) or {'ok':True} if '/permissions/' in path else old(path,data,method)
  self.s.dispatch({'op':'answer','worker':'w','request':'c1','answers':[['Friday']],'basis':'user: Friday'})
  self.assertEqual(calls,[{'answers':{'deadline':{'answers':['Friday']}}}])
 def test_long_instruction_is_sent_with_a_conciseness_warning(self):
  short=self.assign('short');self.assertNotIn('warning',short)
  self.api.invoked=[short['id']];self.s.db.execute("UPDATE assignments SET state='finished'")
  long=self.s.dispatch({'op':'assign','worker':'w','text':'x'*1500,'key':'long'})
  self.assertEqual(long['state'],'sent');self.assertIn('1500 characters',long['warning'])
 def test_supervisor_additions_are_one_short_line(self):
  self.s.dispatch({'op':'claim','project':self.p,'resource':'main/GPU0'})
  self.s.dispatch({'op':'assign','worker':'w','text':'email Bob','key':'e','scope':'external'})
  extra=self.api.sent[-1]['text'].split('\n')[1:]
  self.assertEqual(extra[0],'Reserved for this project: main/GPU0')
  self.assertTrue(extra[1].startswith('External-only:'));self.assertLess(len(extra[1]),160+len(self.tmp.name))
 def test_managed_sessions_published_for_the_ui(self):
  self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',('r',self.p,'claude','n2','s-released',self.tmp.name,'released',time.time()))
  self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',('o',self.p,'claude','n3','s-off',self.tmp.name,'managed',time.time()))
  self.s.dispatch({'op':'watch','worker':'o','mode':'off'});self.s.set('manager','m-session')
  path=self.tmp.name+'/managed.json';out=self.s.publish_managed(path)
  self.assertEqual(out['s']['oversight']['state'],'unknown')
  self.assertEqual({sid:{k:v for k,v in entry.items() if k!='oversight'} for sid,entry in out.items()},{'s':{'role':'worker','worker':'w','project':'test','watch':'events','projectStatus':'running','manualHold':False},'m-session':{'role':'manager'}})
  self.assertEqual(json.load(open(path))['sessions'],out)
 def test_resource_claim_exclusive(self):
  other=self.s.dispatch({'op':'register','name':'other','repo':self.tmp.name})['id']
  self.s.dispatch({'op':'claim','project':self.p,'resource':'extra-pc/GPU0'})
  with self.assertRaisesRegex(ValueError,'owned'):self.s.dispatch({'op':'claim','project':other,'resource':'extra-pc/GPU0'})
class CrossDirectoryAttach(IsolatedSupervisorTest):
 def test_explicit_original_claude_directory_preserves_history(self):
  from pathlib import Path
  from unittest.mock import patch
  import json,uuid
  with tempfile.TemporaryDirectory() as folder:
   home=Path(folder); original=home/'original';original.mkdir();repo=home/'actual';repo.mkdir()
   nid=str(uuid.uuid4());history=home/'.claude/projects/original';history.mkdir(parents=True)
   transcript=history/(nid+'.jsonl');data=json.dumps({'sessionId':nid,'cwd':str(original)})+'\n';transcript.write_text(data)
   s=Supervisor(home/'db',FakeHapi());p=s.dispatch({'op':'register','name':'test','repo':str(repo)})['id']
   with patch('supervisor.Path.home',return_value=home):
    request={'op':'attach','project':p,'runtime':'claude','native_id':nid}
    with self.assertRaisesRegex(ValueError,'mismatch'):s.dispatch(request)
    with self.assertRaisesRegex(ValueError,'mismatch'):s.dispatch(dict(request,session_cwd=str(repo)))
    worker=s.dispatch(dict(request,session_cwd=str(original)))
    self.assertEqual(worker['path'],str(original));self.assertIsNone(worker['session'])
    self.assertEqual(s.project(p)['repo'],str(repo));self.assertEqual(transcript.read_text(),data)
    with self.assertRaisesRegex(ValueError,'already registered'):s.dispatch(dict(request,session_cwd=str(original)))
   s.db.close()

class Adoption(IsolatedSupervisorTest):
 """Headless native adoption and evidence-only recovery of failed attempts."""
 def setUp(self):
  import uuid
  from pathlib import Path
  from unittest.mock import patch
  self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name)
  self.sessions={};self.api=FakeHapi();old=self.api.call
  def call(path,data=None,method=None):
   if path=='/sessions':return {'sessions':[{'id':k} for k in self.sessions]}
   if path.startswith('/sessions/') and path[10:] in self.sessions:return {'session':self.sessions[path[10:]]}
   return old(path,data,method)
  self.api.call=call;self.s=Supervisor(self.home/'db',self.api)
  self.p=self.s.dispatch({'op':'register','name':'t','repo':self.tmp.name})['id']
  self.nid=str(uuid.uuid4());self.units={};self.commands=[]
  self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',('w',self.p,'codex',self.nid,None,self.tmp.name,'registered',time.time()))
  (self.home/'empty.jsonl').write_text('');self.s.native_path=lambda w:self.home/'empty.jsonl'
  self.s.unit_state=lambda unit:dict(self.units.get(unit,{'LoadState':'not-found','ActiveState':'inactive','MainPID':'0'}))
  def run(cmd,**kw):
   self.commands.append(cmd)
   if cmd[0]=='systemd-run' and self.on_start:self.on_start()
   if cmd[:3]==['systemctl','--user','reset-failed']:self.units.pop(cmd[3],None)
  self.on_start=None
  for target,value in (('supervisor.subprocess.run',run),('supervisor.subprocess.check_output',lambda *a,**k:'0'),('supervisor.time.sleep',lambda x:None),('supervisor.Path.home',lambda:self.home)):
   patcher=patch(target,value);patcher.start();self.addCleanup(patcher.stop)
 def tearDown(self):self.s.db.close();self.tmp.cleanup()
 def started(self):
  self.on_start=lambda:self.sessions.__setitem__('new',{'active':True,'metadata':{'codexSessionId':self.nid}})
 def test_missing_paginated_lineage_blocks_before_worker_state_or_controller_changes(self):
  (self.home/'empty.jsonl').write_text(json.dumps({'payload':{'history_mode':'paginated','history_base':{'thread_id':self.nid}}})+'\n')
  before=dict(self.s.worker('w'))
  with unittest.mock.patch.object(self.s,'inspect_native_history',return_value={'nativeId':self.nid,'status':'blocked','error':'missing source rollout'}):
   with self.assertRaisesRegex(ValueError,'blocked before adoption.*missing source rollout'):self.s.adopt('w')
  self.assertEqual(dict(self.s.worker('w')),before)
  self.assertFalse(any(c[0]=='systemd-run' for c in self.commands));self.assertEqual(self.api.sent,[])
 def test_inactive_bound_adoption_checks_history_before_clearing_host_or_resuming(self):
  (self.home/'empty.jsonl').write_text(json.dumps({'payload':{'history_mode':'paginated'}})+'\n')
  self.s.db.execute("UPDATE workers SET session='old' WHERE id='w'")
  self.sessions['old']={'active':False,'metadata':{'codexSessionId':self.nid}}
  self.s.set('host:w',123)
  with unittest.mock.patch.object(self.s,'inspect_native_history',return_value={'nativeId':self.nid,'status':'blocked','error':'missing source rollout'}):
   with self.assertRaisesRegex(ValueError,'blocked before adoption'):self.s.adopt('w')
  self.assertEqual(self.s.get('host:w'),'123');self.assertEqual(self.s.worker('w')['session'],'old')
  self.assertEqual(self.api.sent,[])
 def test_native_history_check_never_accepts_another_identity(self):
  from types import SimpleNamespace
  with unittest.mock.patch('supervisor.subprocess.run',return_value=SimpleNamespace(stdout=json.dumps({'nativeId':'other','status':'ready'}))):
   with self.assertRaisesRegex(ValueError,'verified identity'):self.s.inspect_native_history(self.s.worker('w'))
 def test_codex_adoption_is_headless_and_claude_stays_terminal(self):
  self.started();w=self.s.adopt('w')
  cmd=next(c for c in self.commands if c[0]=='systemd-run')
  # `resume ID` must directly follow `codex`, or HAPI starts a new thread.
  self.assertEqual(cmd[cmd.index('codex')+1:],['resume',self.nid,'--started-by','runner','--permission-mode','yolo']);self.assertNotIn('terminal',cmd)
  self.assertEqual((w['state'],w['session'],w['native_id']),('managed','new',self.nid))
  self.s.db.execute("UPDATE workers SET runtime='claude',session=NULL,state='registered',native_id=? WHERE id='w'",(self.nid,))
  self.sessions.clear();self.commands.clear();self.on_start=lambda:self.sessions.__setitem__('c',{'active':True,'metadata':{'claudeSessionId':self.nid}})
  with unittest.mock.patch('supervisor.Path.glob',return_value=[]):self.s.adopt('w')
  cmd=next(c for c in self.commands if c[0]=='systemd-run')
  self.assertEqual(cmd[cmd.index('claude')+1:],['--permission-mode','bypassPermissions','--resume',self.nid,'--started-by','terminal','--hapi-starting-mode','remote'])
 def test_inactive_stale_session_is_not_adoption_success(self):
  self.sessions['old']={'active':False,'metadata':{'codexSessionId':self.nid}}
  with self.assertRaisesRegex(ValueError,'uncertain'):self.s.adopt('w')
  self.assertEqual(self.s.worker('w')['state'],'adopting')
 def test_failed_attempt_reconciles_then_adopts_exact_history(self):
  self.s.db.execute("UPDATE workers SET state='adopting' WHERE id='w'")
  unit='manager-adopt-w';self.units[unit]={'LoadState':'loaded','ActiveState':'failed','MainPID':'0','Result':'exit-code','ExecMainStatus':'1'}
  self.s.native_processes=lambda nid:[]
  with self.assertRaisesRegex(ValueError,'unresolved'):self.s.adopt('w')
  r=self.s.dispatch({'op':'reconcile_adopt','worker':'w'})
  self.assertEqual((r['outcome'],r['worker']['state'],r['worker']['native_id']),('registered','registered',self.nid))
  self.assertIn(['systemctl','--user','reset-failed',unit],self.commands)
  self.assertEqual([e['kind'] for e in self.s.dispatch({'op':'events'})],['adoption-failed'])
  with self.assertRaisesRegex(ValueError,'no unresolved'):self.s.reconcile_adopt('w')
  self.started();self.assertEqual(self.s.adopt('w')['session'],'new')
 def test_uncertain_attempts_refuse_reconciliation(self):
  self.s.db.execute("UPDATE workers SET state='adopting' WHERE id='w'")
  unit='manager-adopt-w';dead={'LoadState':'loaded','ActiveState':'failed','MainPID':'0'}
  self.s.native_processes=lambda nid:[]
  for state in ({'LoadState':'loaded','ActiveState':'active','MainPID':'42'},{'LoadState':'not-found','ActiveState':'inactive','MainPID':'0'}):
   self.units[unit]=state
   with self.assertRaisesRegex(ValueError,'not conclusively failed'):self.s.reconcile_adopt('w')
  self.units[unit]=dead;self.s.native_processes=lambda nid:[123]
  with self.assertRaisesRegex(ValueError,'still references'):self.s.reconcile_adopt('w')
  self.s.native_processes=lambda nid:[]
  owners=self.home/'.codex/hapi-runtime-owners';owners.mkdir(parents=True)
  import json,os
  (owners/'r.json').write_text(json.dumps({'pid':os.getpid(),'sessions':{'x':{'threadId':self.nid,'active':True}}}))
  with self.assertRaisesRegex(ValueError,'live HAPI Codex runtime'):self.s.reconcile_adopt('w')
  (owners/'r.json').unlink();locks=self.home/'.codex/thread-writer-locks';locks.mkdir()
  (locks/(self.nid+'.lock')).write_text('')
  def held(f,op):raise BlockingIOError  # native writer owns the lock
  with unittest.mock.patch('supervisor.fcntl.flock',held):
   with self.assertRaisesRegex(ValueError,'writer lock'):self.s.reconcile_adopt('w')
  self.sessions['live']={'active':False,'metadata':{'codexSessionId':self.nid,'hostPid':1}}
  with unittest.mock.patch('supervisor.live_controller',return_value=True):
   with self.assertRaisesRegex(ValueError,'disconnected controller'):self.s.reconcile_adopt('w')
  self.assertEqual(self.s.worker('w')['state'],'adopting')
  self.assertNotIn(['systemctl','--user','reset-failed',unit],self.commands)
 def test_codex_new_thread_from_own_process_is_not_success(self):
  def start():
   self.sessions['wrong']={'active':True,'metadata':{'codexSessionId':'other-thread','hostPid':0,'path':self.tmp.name,'flavor':'codex'}}
   self.sessions['blank']={'active':True,'metadata':{'hostPid':0,'path':self.tmp.name,'flavor':'codex'}}
  self.on_start=start
  with unittest.mock.patch('supervisor.subprocess.check_output',return_value='0'):
   with self.assertRaisesRegex(ValueError,'uncertain'):self.s.adopt('w')
  self.assertIsNone(self.s.worker('w')['session'])
 def test_misbound_session_unbinds_only_after_controller_stops(self):
  self.s.db.execute("UPDATE workers SET state='managed',session='bad' WHERE id='w'")
  self.sessions['bad']={'active':True,'metadata':{'codexSessionId':'other-thread'}}
  with self.assertRaisesRegex(ValueError,'still controlled'):self.s.reconcile_adopt('w')
  self.sessions['bad']['active']=False;self.units['manager-adopt-w']={'LoadState':'loaded','ActiveState':'active','MainPID':'5'}
  with self.assertRaisesRegex(ValueError,'still runs'):self.s.reconcile_adopt('w')
  self.units.clear();r=self.s.reconcile_adopt('w')
  self.assertEqual((r['worker']['state'],r['worker']['session'],r['worker']['native_id']),('registered',None,self.nid))
  self.sessions['ok']={'active':True,'metadata':{'codexSessionId':self.nid}}
  self.s.db.execute("UPDATE workers SET state='managed',session='ok' WHERE id='w'")
  with self.assertRaisesRegex(ValueError,'nothing to reconcile'):self.s.reconcile_adopt('w')
 def test_imported_codex_history_is_not_manual_intervention(self):
  import json
  rollout=self.home/'rollout.jsonl';rollout.write_text(json.dumps({'type':'event_msg','payload':{'type':'item_completed','item':{'type':'UserMessage','client_id':'old-prompt'}}})+'\n')
  self.s.native_path=lambda w:rollout
  self.s.dispatch({'op':'resume','project':self.p});self.started();self.s.adopt('w')
  now=time.time()*1000+1000;user={'role':'user','content':{'type':'text','text':'x'}}
  self.api.session=self.sessions['new'];self.sessions['s']=self.api.session
  self.api.messages=[{'id':'m1','localId':'old-prompt','createdAt':now,'content':user}]
  self.s.db.execute("UPDATE workers SET session='s' WHERE id='w'");self.s.scan(self.s.worker('w'))
  self.assertEqual(self.s.dispatch({'op':'events'}),[])
  self.api.messages.append({'id':'m2','localId':'typed-now','createdAt':now,'content':user});self.s.scan(self.s.worker('w'))
  self.assertEqual([e['kind'] for e in self.s.dispatch({'op':'events'})],['user-input'])
 def missing_managed_claude(self):
  import urllib.error
  self.s.db.execute("UPDATE workers SET state='managed',runtime='claude',session='deleted' WHERE id='w'")
  old=self.api.call
  def call(path,data=None,method=None):
   if path=='/sessions/deleted':
    raise RuntimeError('404') from urllib.error.HTTPError(path,404,'missing',{},None)
   return old(path,data,method)
  self.api.call=call;self.s.native_processes=lambda nid:[]
 def test_deleted_binding_recovers_without_losing_pending_goal(self):
  self.missing_managed_claude()
  self.s.db.execute("INSERT INTO assignments(id,worker,task_key,state,goal) VALUES('pending','w','goal','sent',1)")
  r=self.s.reconcile_adopt('w',True)
  self.assertEqual((r['outcome'],r['worker']['native_id']),('registered',self.nid))
  self.assertEqual(self.s.one("SELECT state FROM assignments WHERE id='pending'")['state'],'sent')
 def test_missing_binding_refuses_live_process_or_unknown_api_failure(self):
  self.missing_managed_claude();self.s.native_processes=lambda nid:[123]
  with self.assertRaisesRegex(ValueError,'still references'):self.s.reconcile_adopt('w',True)
  self.s.session=lambda sid:(_ for _ in ()).throw(RuntimeError('503 unavailable'))
  with self.assertRaisesRegex(RuntimeError,'503'):self.s.reconcile_adopt('w',True)
 def test_rebind_requires_current_native_metadata(self):
  self.missing_managed_claude()
  self.sessions['cleared']={'active':True,'metadata':{'hostPid':123}}
  self.s.native_processes=lambda nid:[123]
  with self.assertRaisesRegex(ValueError,'still references'):self.s.reconcile_adopt('w',True)
  self.sessions['cleared']['metadata']['claudeSessionId']=self.nid
  self.assertEqual(self.s.reconcile_adopt('w',True)['worker']['session'],'cleared')
 def test_stop_cleared_controller_only_for_idle_confirmed_clear(self):
  from unittest.mock import patch
  self.missing_managed_claude();self.sessions['s']=self.api.session
  old=self.api.call;stopped=[]
  def call(path,data=None,method=None):
   if path=='/sessions/s/archive':stopped.append(path);return {'ok':True}
   return old(path,data,method)
  self.api.call=call
  msg={'seq':1,'invokedAt':1,'content':{'role':'user','content':{'text':'/clear'}}}
  self.api.messages=[msg]
  with patch('supervisor.Path.read_bytes',return_value=b'hapi\0--resume\0'+self.nid.encode()+b'\0'),patch('supervisor.live_controller',return_value=True):
   self.api.session['thinking']=True
   with self.assertRaisesRegex(ValueError,'idle cleared'):self.s.stop_cleared_controller('w','s')
   self.api.session['thinking']=False;msg['invokedAt']=None
   with self.assertRaisesRegex(ValueError,'Pending'):self.s.stop_cleared_controller('w','s')
   msg['invokedAt']=1;msg['content']['content']['text']='continue'
   with self.assertRaisesRegex(ValueError,'not /clear'):self.s.stop_cleared_controller('w','s')
   msg['content']['content']['text']='/clear';self.s.stop_cleared_controller('w','s')
  self.assertEqual(stopped,['/sessions/s/archive'])
 def test_late_success_binds_exact_session_without_respawn(self):
  self.s.db.execute("UPDATE workers SET state='adopting' WHERE id='w'")
  self.sessions['late']={'active':True,'metadata':{'codexSessionId':self.nid}}
  r=self.s.reconcile_adopt('w')
  self.assertEqual((r['outcome'],r['worker']['session']),('managed','late'))
  self.assertFalse(any(c[0]=='systemd-run' for c in self.commands))
 def test_loaded_running_unit_blocks_new_adoption(self):
  self.units['manager-adopt-w']={'LoadState':'loaded','ActiveState':'active','MainPID':'9'}
  with self.assertRaisesRegex(ValueError,'still loaded'):self.s.adopt('w')
  self.assertEqual(self.s.worker('w')['state'],'registered')
 def test_unstarted_service_returns_to_registered(self):
  import subprocess
  def fail(cmd,**kw):
   if cmd[0]=='systemd-run':raise subprocess.CalledProcessError(1,cmd,stderr=b'boom')
  with unittest.mock.patch('supervisor.subprocess.run',fail):
   with self.assertRaisesRegex(ValueError,'not created'):self.s.adopt('w')
  self.assertEqual(self.s.worker('w')['state'],'registered')

if __name__=='__main__':unittest.main()
