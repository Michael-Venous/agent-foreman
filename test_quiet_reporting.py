import json
import unittest
from unittest.mock import patch
import test_supervisor as fixtures

class QuietReporting(fixtures.IsolatedSupervisorTest):
 setUp=fixtures.Tests.setUp
 tearDown=fixtures.Tests.tearDown
 observe=fixtures.Tests.observe
 observations=fixtures.Tests.observations
 set_public=fixtures.Tests.set_public
 public_progress=fixtures.Tests.public_progress
 def quiet_default(self):
  # Existing installations can retain the old persisted schema without a DB migration.
  self.s.set('reporting-default',json.dumps({'enabled':True,'fallback_minutes':30,'important':True}))
 def test_default_has_status_changes_but_no_routine_or_semantic_wakes(self):
  self.quiet_default();self.api.session['thinking']=True
  self.set_public('Working.',1);self.observe(1000)
  self.set_public('A breakthrough happened.',2);self.observe(1500);self.observe(10000)
  self.assertEqual(self.observations(),[])
  self.api.session['thinking']=False;self.observe(10001)
  self.assertEqual(json.loads(self.observations()[0]['detail'])['changes'][0]['category'],'status-changed')
  self.s.db.execute('UPDATE events SET acked=1');self.observe(20000)
  self.assertEqual(self.observations(),[]);self.assertEqual(self.api.sent,[])
 def test_interval_turns_on_routine_and_can_be_disabled_without_hiding_failures(self):
  self.quiet_default();self.s.dispatch({'op':'reporting','worker':'w','fallback_minutes':10})
  self.assertTrue(self.s.reporting('w')['routine_enabled'])
  self.api.session['thinking']=True;self.set_public('Running.',1);self.observe(1000);self.observe(1600)
  self.assertEqual(len(self.observations()),1)
  self.s.dispatch({'op':'reporting','worker':'w','routine_enabled':False})
  self.assertFalse(self.s.progress_deliverable(self.observations()[0]));self.assertEqual(self.observations(),[])
  self.api.session.update(thinking=False,lastFailure={'kind':'failed','message':'error','at':1601});self.observe(1601)
  changes=json.loads(self.observations()[0]['detail'])['changes']
  self.assertTrue(any(x['category']=='runtime-failure' for x in changes))
 def test_status_wake_explains_followup_policy_without_prompting_child(self):
  self.quiet_default();self.api.session['thinking']=True;self.set_public('Working.',1);self.observe(1000)
  self.api.session['thinking']=False;self.observe(1001)
  text=self.s.batch_text(self.observations())
  self.assertIn('If unfinished and safe',text);self.assertIn('if blocked, tell the user',text)
  self.assertIn('do not repeat without new evidence',text);self.assertEqual(self.api.sent,[])
 def test_legacy_per_chat_override_and_reset(self):
  self.quiet_default();self.s.set('reporting:w',json.dumps({'fallback_minutes':45}))
  self.assertTrue(self.s.reporting('w')['routine_enabled']);self.assertEqual(self.s.reporting('w')['fallback_minutes'],45)
  self.s.dispatch({'op':'reporting','worker':'w','reset':True})
  self.assertFalse(self.s.reporting('w')['routine_enabled'])
 def test_status_request_is_read_only_and_never_prompts_child(self):
  self.quiet_default();self.api.session['thinking']=True;self.set_public('Current progress.',1)
  before=self.s.db.total_changes
  result=self.s.dispatch({'op':'check_status','worker':'w'})
  self.assertEqual(result['latest_reply'],'Current progress.');self.assertEqual(result['progress']['status'],'running')
  self.assertEqual(self.api.sent,[]);self.assertEqual(before,self.s.db.total_changes)
 def test_released_and_paused_never_resume_or_wake_from_status(self):
  self.quiet_default();self.api.session['thinking']=True;self.set_public('Working',1);self.observe(1000)
  self.api.session.update(thinking=False,manualPausedAt=1)
  self.s.db.execute("UPDATE projects SET status='paused' WHERE id=?",(self.p,));self.observe(1100)
  self.assertEqual(self.observations(),[]);self.assertEqual(self.api.sent,[])

class StatusAccess(fixtures.IsolatedSupervisorTest):
 def test_mid_level_manager_can_read_only_its_child(self):
  import test_node_graph as graph
  test=graph.NodeGraphTests('test_classic_managed_workers_seed_as_overseer_children_once');test.setUp()
  try:
   test.worker('mid','midchat');test.worker('leaf','leafchat');test.worker('other','otherchat')
   test.s.set('node-graph-active','1');test.s.set('owner:leaf','midchat');test.s.set('owner:other','over')
   result=test.s.dispatch({'op':'check_status','worker':'leaf','actor':'midchat'})
   self.assertEqual(result['worker'],'leaf');self.assertEqual(test.hub.sent,[])
   with self.assertRaisesRegex(ValueError,'own children'):test.s.dispatch({'op':'check_status','worker':'other','actor':'midchat'})
  finally:test.tearDown()

if __name__=='__main__':unittest.main()
