"""An old assignment with no hub record must not block later assignments forever. Fake hub only."""
import time,unittest
import test_supervisor as fixtures

class Superseded(fixtures.IsolatedSupervisorTest):
 setUp=fixtures.Tests.setUp
 tearDown=fixtures.Tests.tearDown
 def add(self,aid,state,hours,key=None):
  self.s.db.execute("INSERT INTO assignments(id,worker,task_key,text,state,created,seen_busy,goal) VALUES(?,?,?,?,?,?,?,?)",(aid,'w',key or aid,'task',state,time.time()-hours*3600,1,0))
 def state(self,aid):return self.s.one('SELECT state FROM assignments WHERE id=?',(aid,))['state']
 def scan(self):self.s.scan(self.s.worker('w'))
 def test_old_unrecorded_assignment_is_settled_once_later_work_finished(self):
  self.add('old','sent',30);self.add('later','finished',20);self.scan()
  self.assertEqual(self.state('old'),'superseded');self.assertEqual(self.state('later'),'finished')
  self.assertIn('later',self.s.get('superseded:old'))
  # It no longer blocks a new autonomous assignment.
  self.api.session.update(thinking=False,active=True)
  self.s.dispatch({'op':'assign','worker':'w','text':'next','key':'next','user_requested':False})
 def test_history_and_other_cases_are_preserved(self):
  self.add('recent','sent',1);self.add('recent-later','finished',0.5);self.scan();self.assertEqual(self.state('recent'),'sent')   # too recent
  self.add('alone','sent',30,'alone');self.s.db.execute("DELETE FROM assignments WHERE id IN ('recent','recent-later')");self.scan();self.assertEqual(self.state('alone'),'sent') # nothing later finished
  self.add('later2','finished',10);original=self.api.call
  self.api.call=lambda path,data=None,method=None:{'queuedLocalIds':['alone'],'indeterminateLocalIds':[],'invokedLocalMessages':[]} if path.endswith('/queued-state') else original(path,data,method)
  self.scan();self.assertEqual(self.state('alone'),'sent')   # hub still has it queued
  self.api.call=lambda path,data=None,method=None:{'queuedLocalIds':[],'indeterminateLocalIds':['alone'],'invokedLocalMessages':[]} if path.endswith('/queued-state') else original(path,data,method)
  self.scan();self.assertEqual(self.state('alone'),'sent')   # delivery unresolved: never assume
  self.assertEqual(self.api.sent,[])

 def test_sweep_settles_for_a_chat_that_is_not_scanned(self):
  self.add('old','sent',30);self.add('later','finished',20)
  self.s.sweep_superseded();self.assertEqual(self.state('old'),'superseded')
  self.add('old2','sent',31);self.s.sweep_superseded();self.assertEqual(self.state('old2'),'sent')  # rate limited: next sweep in 10 min
  self.s.set('superseded-sweep-at',0);self.s.sweep_superseded();self.assertEqual(self.state('old2'),'superseded')

 def test_older_goal_assignment_is_replaced_by_a_delivered_newer_goal_only(self):
  self.s.db.execute("INSERT INTO assignments(id,worker,task_key,text,state,created,seen_busy,goal) VALUES('g1','w','g1','/goal a','sent',?,1,1)",(time.time()-30*3600,))
  self.s.db.execute("INSERT INTO assignments(id,worker,task_key,text,state,created,seen_busy,goal) VALUES('g2','w','g2','/goal b','sent',?,1,1)",(time.time()-20*3600,))
  self.api.invoked=['g1','g2']
  self.s.sweep_superseded();self.assertEqual(self.state('g1'),'superseded');self.assertEqual(self.state('g2'),'sent')   # newest goal stays tracked
  self.s.db.execute("INSERT INTO assignments(id,worker,task_key,text,state,created,seen_busy,goal) VALUES('g0','w','g0','/goal old','sent',?,1,1)",(time.time()-40*3600,))
  self.api.invoked=['g0','g2'];self.s.set('superseded-sweep-at',0)
  original=self.api.call
  self.api.call=lambda path,data=None,method=None:{'queuedLocalIds':[],'indeterminateLocalIds':[],'invokedLocalMessages':[{'localId':'g0','invokedAt':1}]} if path.endswith('/queued-state') else original(path,data,method)
  self.s.sweep_superseded();self.assertEqual(self.state('g0'),'sent')   # newer goal has no delivery evidence: keep

 def test_assignments_of_released_workers_are_closed_like_release_does(self):
  self.add('rel','sent',30);self.s.db.execute("UPDATE workers SET state='released' WHERE id='w'")
  self.s.sweep_superseded();self.assertEqual(self.state('rel'),'released');self.assertIn('released',self.s.get('superseded:rel'))
  self.assertEqual(self.s.worker('w')['state'],'released');self.assertEqual(self.api.sent,[])
if __name__=='__main__':unittest.main()
