"""No live API or filesystem writes: fixtures inherit the isolated state root."""
import json,time,unittest
from unittest.mock import patch
import test_supervisor as fixtures

class Retention(fixtures.IsolatedSupervisorTest):
 setUp=fixtures.Tests.setUp
 tearDown=fixtures.Tests.tearDown
 def terminal(self,provider='codex',failure=False):
  self.s.db.execute('UPDATE workers SET runtime=? WHERE id=?',(provider,'w'))
  data={'type':'task_failed' if failure else 'task_complete','last_agent_message':'Finished the requested work.'} if provider=='codex' else {'type':'system','subtype':'turn_duration'}
  self.api.messages=[{'id':'done','seq':2,'createdAt':time.time()*1000,'content':{'role':'agent','content':{'type':'output','data':data}}}]
 def ready_to_retire(self):
  self.s.set('retirement-candidate:w',json.dumps({'message':'done','at':time.time()-6}))
 def scan(self):self.s.scan(self.s.worker('w'),retire=True)
 def assert_kept(self):
  self.ready_to_retire();self.scan();self.assertEqual(self.s.worker('w')['state'],'managed');self.assertEqual(self.api.sent,[])
 def test_default_requires_terminal_and_two_separate_idle_observations(self):
  self.scan();self.assertEqual(self.s.worker('w')['state'],'managed')
  self.terminal();self.scan();self.assertEqual(self.s.worker('w')['state'],'managed')
  # The unsent completion observation must survive until handled.
  self.assertIsNone(self.s.get('retirement-candidate:w'))
  self.s.db.execute('UPDATE events SET acked=1');self.scan()
  self.assertIsNotNone(self.s.get('retirement-candidate:w'))
  self.ready_to_retire();self.scan()
  self.assertEqual(self.s.worker('w')['state'],'released');self.assertEqual(self.s.watch('w')['mode'],'off')
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.s.project(self.p)['status'],'running')
  self.assertEqual(self.s.one("SELECT kind FROM events WHERE acked=0")['kind'],'finished-unmanaged')
  self.assertEqual(self.s.publish_managed(),{})
  with self.assertRaisesRegex(ValueError,'unmanaged'):self.s.dispatch({'op':'assign','worker':'w','text':'old authorization','key':'old','user_requested':True})
 def test_no_second_completion_notice_after_first_report_was_delivered(self):
  self.manager();self.terminal();self.s.event('result:a',self.p,'w','finished','Review completion')
  self.scan();candidate=json.loads(self.s.get('retirement-candidate:w'))
  self.s.notify_manager();self.assertEqual(len(self.api.sent),1)
  candidate['at']=time.time()-6;self.s.set('retirement-candidate:w',json.dumps(candidate));self.scan();self.s.notify_manager()
  self.assertEqual(len(self.api.sent),1);self.assertEqual(self.s.worker('w')['state'],'released')
 def test_retention_reason_persists_and_does_not_resume_paused_work(self):
  self.api.session['manualPausedAt']=123
  self.s.dispatch({'op':'retention','worker':'w','mode':'keep','reason':'Wait for GPU dependency'})
  from supervisor import Supervisor
  self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
  self.assertEqual(self.s.dispatch({'op':'retention','worker':'w'})['reason'],'Wait for GPU dependency')
  self.assertEqual(self.api.session['manualPausedAt'],123);self.assertEqual(self.api.sent,[])
 def test_claude_successful_terminal_retires_and_preserves_history(self):
  self.terminal('claude');self.ready_to_retire();self.scan()
  self.assertEqual(self.s.worker('w')['state'],'released');self.assertEqual(len(self.api.messages),1)
 def test_completed_tracker_can_finish_new_direct_work(self):
  self.s.dispatch({'op':'complete','project':self.p});self.terminal();self.ready_to_retire();self.scan()
  self.assertEqual(self.s.worker('w')['state'],'released');self.assertEqual(self.s.project(self.p)['status'],'completed')
 def test_running_background_input_ready_and_manual_pause_prevent_retirement(self):
  self.terminal()
  for change in ({'thinking':True},{'backgroundTaskCount':1},{'agentState':{'requests':{'r':{}}}},{'agentState':{'claudeReadyMessages':[{'state':'waiting'}]}},{'manualPausedAt':123},{'active':False}):
   with self.subTest(change=change),patch.dict(self.api.session,change):self.assert_kept()
 def test_paused_and_manually_held_projects_preserved(self):
  self.terminal()
  for status,manual in [('paused',0),('running',1)]:
   self.s.db.execute('UPDATE projects SET status=?,manual=? WHERE id=?',(status,manual,self.p));self.assert_kept()
 def test_goals_remain_managed_until_completed(self):
  self.terminal()
  for state in ('active','paused','usageLimited','unknown'):
   with self.subTest(state=state),patch.dict(self.api.session,{'goal':{'status':state}}):self.assert_kept()
  self.api.session['goal']={'status':'complete'};self.ready_to_retire();self.scan();self.assertEqual(self.s.worker('w')['state'],'released')
 def test_unavailable_goal_status_is_not_treated_as_finished(self):
  self.terminal();original=self.api.call
  def api(path,data=None,method=None):
   if path.endswith('/goal'):raise OSError('temporarily unavailable')
   return original(path,data,method)
  self.api.call=api;self.assert_kept()
 def test_user_message_after_terminal_keeps_current_scope(self):
  self.terminal();self.api.messages.append({'id':'new','seq':3,'createdAt':time.time()*1000,'content':{'role':'user','content':{'text':'Continue with another task'}}});self.assert_kept()
 def test_queued_and_uncertain_input_are_not_finished(self):
  self.terminal();self.api.messages.insert(0,{'id':'queued','localId':'q','seq':1,'createdAt':time.time()*1000,'content':{'role':'user','content':{'text':'Next task'}}})
  original=self.api.call
  for field in ('queuedLocalIds','indeterminateLocalIds'):
   def api(path,data=None,method=None):
    if path.endswith('/queued-state'):return {'queuedLocalIds':[],'indeterminateLocalIds':[],'invokedLocalMessages':[],field:['q']}
    return original(path,data,method)
   self.api.call=api;self.assert_kept()
 def test_unresolved_assignment_keeps_oversight(self):
  self.terminal();self.s.db.execute("INSERT INTO assignments(id,worker,task_key,text,state,created) VALUES('a','w','open','x','sent',?)",(time.time(),));self.assert_kept()
 def test_usage_recovery_keeps_oversight(self):
  self.terminal();self.s.db.execute("INSERT INTO usage_recovery VALUES('w','source',0,0,'waiting',NULL,'s')");self.assert_kept()
 def test_explicit_recurring_followup_keeps_oversight(self):
  self.terminal();self.s.dispatch({'op':'remind','worker':'w','every_minutes':1440,'note':'User requested daily email checks'});self.assert_kept()
 def test_routine_progress_reminder_does_not_keep_finished_work_forever(self):
  self.terminal();self.s.dispatch({'op':'remind','worker':'w','every_minutes':30,'progress':True});self.ready_to_retire();self.scan()
  self.assertEqual(self.s.worker('w')['state'],'released');self.assertEqual(self.s.one('SELECT active FROM reminders')['active'],0)
 def test_keep_override_requires_reason_and_default_can_be_restored(self):
  self.terminal()
  with self.assertRaisesRegex(ValueError,'reason'):self.s.dispatch({'op':'retention','worker':'w','mode':'keep'})
  self.s.dispatch({'op':'retention','worker':'w','mode':'keep','reason':'User requested a dependent follow-up'});self.assert_kept()
  self.s.dispatch({'op':'retention','worker':'w','mode':'until-idle'});self.ready_to_retire();self.scan();self.assertEqual(self.s.worker('w')['state'],'released')
 def test_failure_and_user_input_text_preserve_attention(self):
  self.terminal(failure=True);self.assert_kept()
  self.terminal();self.api.messages[0]['content']['content']['data']['last_agent_message']='I need your approval to proceed.';self.assert_kept()
 def test_plain_text_browser_blocker_keeps_management_without_structured_question(self):
  self.terminal()
  self.api.messages[0]['content']['content']['data']['last_agent_message']='Firefox is running without automation, so I couldn’t access the assignment. Its requirements and exact deadline remain unverified; no completed draft exists yet, and nothing was submitted. Please fully quit Firefox, open **Firefox — Agent Control**, and reply **ready**.'
  self.assert_kept()
  self.assertIsNone(self.s.get('unmanaged:w'))
  self.api.messages[0]['content']['content']['data']['last_agent_message']='Progress detail. '*80+'Please open Firefox and reply ready.'
  self.assert_kept()
 def test_followup_guard_handles_plain_text_requests_but_allows_completed_results(self):
  for text in ('Please unlock the vault.', 'Can you choose the target?', 'I am unable to connect to the course.', 'Reply ready once Firefox is open.', 'Let me know when access is available.', 'The work is not yet complete.'):
   with self.subTest(text=text):self.assertTrue(self.s.result_needs_followup(text))
  for text in ('Finished the requested work.', 'Please note: the draft is complete and saved for review.', 'Access is fixed and the assignment is ready.'):
   with self.subTest(text=text):self.assertFalse(self.s.result_needs_followup(text))
 def test_explicit_remanage_is_required_and_does_not_resume(self):
  self.terminal();self.ready_to_retire();self.scan()
  with self.assertRaisesRegex(ValueError,'current user'):self.s.dispatch({'op':'manage','worker':'w'})
  self.s.dispatch({'op':'manage','worker':'w','user_requested':True,'resume_management':True})
  self.assertEqual(self.s.worker('w')['state'],'managed');self.assertEqual(self.api.sent,[])
 def manager(self,paused=False):
  self.s.set('manager','m');original=self.api.call
  def api(path,data=None,method=None):
   if path=='/sessions/m':return {'session':{'active':True,'thinking':False,'manualPausedAt':1 if paused else None}}
   if path=='/sessions/m/messages':self.api.sent.append(data);return {'ok':True}
   return original(path,data,method)
  self.api.call=api
 def test_one_final_completion_notice_then_no_more_worker_observation(self):
  self.manager();self.terminal();self.ready_to_retire();self.scan();self.s.notify_manager();self.s.notify_manager()
  self.assertEqual(len(self.api.sent),1);self.assertIn('finished-unmanaged',self.api.sent[0]['text'])
  with patch.object(self.api,'call',side_effect=AssertionError('Must not poll released worker')):self.scan()
 def test_manual_unmanage_suppresses_pending_automatic_completion_notice(self):
  self.manager();self.terminal();self.ready_to_retire();self.scan()
  self.s.dispatch({'op':'unmanage','worker':'w','session':'s'});self.s.notify_manager()
  self.assertEqual(self.api.sent,[]);self.assertEqual(self.s.rows('SELECT id FROM events WHERE acked=0'),[])
 def test_quiet_policy_stays_silent_on_retirement(self):
  self.manager();self.s.dispatch({'op':'watch','worker':'w','mode':'quiet'});self.terminal();self.ready_to_retire();self.scan();self.s.notify_manager()
  self.assertEqual(self.s.worker('w')['state'],'released');self.assertEqual(self.api.sent,[])
 def test_paused_manager_never_changes_worker_oversight(self):
  self.manager(paused=True);self.terminal();self.assert_kept()
 def test_reconcile_calls_retirement_with_current_evidence(self):
  self.terminal();self.ready_to_retire();self.s.reconcile();self.assertEqual(self.s.worker('w')['state'],'released')
 def test_new_work_at_final_refresh_preserves_management(self):
  self.terminal();self.ready_to_retire();original=self.api.call;reads=0
  def api(path,data=None,method=None):
   nonlocal reads
   if path=='/sessions/s':
    reads+=1
    if reads>=2:return {'session':{**self.api.session,'thinking':True}}
   return original(path,data,method)
  self.api.call=api;self.scan();self.assertEqual(self.s.worker('w')['state'],'managed')

if __name__=='__main__':unittest.main()
