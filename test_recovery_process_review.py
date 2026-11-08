import json,unittest
from unittest.mock import patch
import recovery_process_review as review
from test_transient_recovery import RecoveryTests
import transient_recovery as tr

class InventoryTests(unittest.TestCase):
 def setUp(self):
  self.init={'pid':1,'ppid':8,'start':10,'uid':100000,'exe':'/usr/bin/dash','argv':['/bin/sh','/usr/bin/entrypoint']}
  self.sleep={'pid':2,'ppid':1,'start':11,'uid':100000,'exe':'/usr/bin/sleep','argv':['sleep','15']}
 def test_only_exact_idle_inventory_allowed(self):
  review.validate_inventory([self.init,self.sleep],self.init)
  for jobs in ([self.sleep], [self.init,self.sleep,{'pid':3,'ppid':1,'start':15,'uid':100000,'exe':'/usr/bin/python','argv':['python']}], [self.init,{**self.sleep,'argv':['sleep','600']}], [self.init,{**self.sleep,'ppid':9}], [{**self.init,'start':99},self.sleep]):
   with self.subTest(jobs=jobs),self.assertRaises(ValueError):review.validate_inventory(jobs,self.init)

class ReviewedRetryTests(RecoveryTests):
 def setup_review(self):
  self.observe([self.failure(text='Selected model is at capacity')]);self.due()
  self.api.session['backgroundTaskCount']=1
  self.api.session['backgroundProcesses']=[{'id':'boot:10:1','pid':10}]
  self.s.set('transient-process-review:s',json.dumps({'identity':{'session':'s','native':'n','worker':'w','episode':'turn1','monitorPid':10,'containerId':'x'},'digest':'d','expires':self.now+500}))
 def test_approved_retry_is_single_and_survives_supervisor_restart(self):
  self.setup_review();self.s.db.close();self.s=type(self.s)(self.tmp.name+'/db',self.api)
  with patch('recovery_process_review.snapshot',return_value={'identity':{'session':'s','native':'n','worker':'w','episode':'turn1','monitorPid':10,'containerId':'x'}}), patch('recovery_process_review.digest',return_value='d'):
   tr.recover(self.s);tr.recover(self.s)
  self.assertEqual(len(self.api.sent),1);self.assertIsNone(self.s.get('transient-process-review:s'));self.assertEqual(self.row()['state'],'sent')
 def test_changed_identity_native_terminal_or_inventory_fail_closed(self):
  self.setup_review()
  for result in ({'identity':{'session':'s','native':'n','worker':'w','episode':'turn1','monitorPid':10,'containerId':'x','monitorStart':999}}, ValueError('new terminal'), ValueError('new container work')):
   with self.subTest(result=result),patch('recovery_process_review.snapshot',side_effect=result if isinstance(result,Exception) else None,return_value=result),patch('recovery_process_review.digest',return_value='d'):
    tr.recover(self.s);self.assertEqual(self.api.sent,[])
 def test_pause_approval_and_queued_input_still_block(self):
  for flag in ('pause','approval','queue'):
   self.setup_review()
   if flag=='pause':self.api.session['manualPausedAt']=1
   if flag=='approval':self.api.session['agentState']={'requests':{'approval':{}}}
   if flag=='queue':self.api.messages.append({'id':'x','localId':'x','createdAt':(self.now+1)*1000,'invokedAt':None,'content':{'role':'user','content':{'text':'change'}}})
   with patch('recovery_process_review.approved',return_value=True):tr.recover(self.s)
   self.assertEqual(self.api.sent,[])
   self.api.session.update(manualPausedAt=None,agentState={});self.s.db.execute('DELETE FROM transient_recovery')
 def test_episode_and_expired_review_are_rejected(self):
  self.setup_review();r=self.row();r['episode']='new';self.assertFalse(review.approved(self.s,r))
  self.now+=600;self.assertFalse(review.approved(self.s,self.row()))
 def test_authorize_requires_fresh_matching_review(self):
  self.setup_review();ev={'identity':{'session':'s','native':'n','worker':'w','episode':'turn1'}}
  with patch('recovery_process_review.review',return_value={'digest':'abc','evidence':ev}):
   with self.assertRaises(ValueError):review.authorize(self.s,{'requested_at':self.now,'reviewed_at':self.now,'digest':'abc'})
   with self.assertRaises(ValueError):review.authorize(self.s,{'user_requested':True,'basis':'user asked','requested_at':self.now,'reviewed_at':self.now,'digest':'wrong'})
   with self.assertRaises(ValueError):review.authorize(self.s,{'user_requested':True,'basis':'user asked','requested_at':self.now,'reviewed_at':self.now-301,'digest':'abc'})

 def test_review_token_is_fresh_and_bound_to_exact_identity(self):
  self.setup_review();identity={'session':'s','native':'n','worker':'w','episode':'turn1','monitorPid':10,'containerId':'x'}
  with patch('recovery_process_review.snapshot',return_value={'identity':identity}):
   req={'worker':'w','pid':10,'container_id':'x'};result=review.review(self.s,req)
   command={**req,'user_requested':True,'basis':'current user request','requested_at':self.now,'reviewed_at':result['reviewedAt'],'digest':result['digest'],'review_token':result['reviewToken']}
   with self.assertRaises(ValueError):review.authorize(self.s,{**command,'digest':'changed'})
   self.assertTrue(review.authorize(self.s,command)['oneAttempt'])
   with patch.object(review,'_REVIEW_KEY',b'new supervisor'):
    with self.assertRaises(ValueError):review.authorize(self.s,command)
