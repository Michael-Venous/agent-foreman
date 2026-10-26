"""Reminder lifecycle regressions; temporary SQLite and FakeHapi only."""
import json
import time
from unittest.mock import patch

import node_graph
import test_supervisor as fixtures
from supervisor import Supervisor


class Reminders(fixtures.IsolatedSupervisorTest):
 setUp = fixtures.Tests.setUp
 tearDown = fixtures.Tests.tearDown

 def reminder(self, **changes):
  request = {'op':'remind','worker':'w','in_minutes':0,'note':'isolated check',**changes}
  if 'at' in changes and 'in_minutes' not in changes:request.pop('in_minutes')
  return self.s.dispatch(request)

 def events_for(self, rid):
  return self.s.rows('SELECT * FROM events WHERE event_key LIKE ? ORDER BY id',('remind:'+rid+':%',))

 def audit_for(self, rid):
  return self.s.rows('SELECT * FROM reminder_deliveries WHERE reminder_id=? ORDER BY due',(rid,))

 def test_success_atomically_persists_event_consumes_oneoff_and_audits(self):
  r = self.reminder()
  self.s.deliver_reminders()
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(r['id'],))['active'],0)
  events = self.events_for(r['id'])
  self.assertEqual(len(events),1)
  audit = self.audit_for(r['id'])
  self.assertEqual(len(audit),1)
  self.assertEqual(audit[0]['status'],'delivered')
  self.assertEqual(audit[0]['due'],r['due'])
  self.assertEqual(audit[0]['event_key'],events[0]['event_key'])
  self.assertEqual(self.api.sent,[])  # Event persistence is not worker/manager invocation.

 def test_event_insert_failure_leaves_oneoff_active_same_due_and_backs_off(self):
  r = self.reminder()
  with patch.object(self.s,'event',side_effect=RuntimeError('injected event insert failure')):
   self.s.deliver_reminders()
  current = self.s.one('SELECT * FROM reminders WHERE id=?',(r['id'],))
  self.assertEqual(current['active'],1)
  self.assertEqual(current['due'],r['due'])
  self.assertEqual(self.events_for(r['id']),[])
  audit = self.audit_for(r['id'])[0]
  self.assertEqual(audit['status'],'failed')
  self.assertGreater(audit['next_attempt_at'],time.time())
  self.assertIn('injected event insert failure',audit['error'])
  # An SSE burst must not cause an immediate tight retry loop.
  with patch.object(self.s,'event',side_effect=AssertionError('backoff ignored')):
   self.s.deliver_reminders()
  with patch('supervisor.time.time',return_value=audit['next_attempt_at']+1):
   self.s.deliver_reminders();self.s.deliver_reminders()
  self.assertEqual(len(self.events_for(r['id'])),1)
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(r['id'],))['active'],0)
  self.assertEqual(self.audit_for(r['id'])[0]['status'],'delivered')
  self.assertEqual(self.api.sent,[])

 def test_failure_after_event_insert_rolls_back_event_and_schedule_consumption(self):
  r = self.reminder()
  original = self.s.event
  def fail_after_insert(*args):
   original(*args)
   raise RuntimeError('injected failure after event insert')
  with patch.object(self.s,'event',side_effect=fail_after_insert):
   self.s.deliver_reminders()
  self.assertEqual(self.events_for(r['id']),[])
  current = self.s.one('SELECT * FROM reminders WHERE id=?',(r['id'],))
  self.assertEqual((current['active'],current['due']),(1,r['due']))
  self.assertEqual(self.audit_for(r['id'])[0]['status'],'failed')

 def test_one_failed_due_reminder_does_not_starve_another(self):
  bad = self.reminder(note='bad check')
  good = self.reminder(note='good check')
  original = self.s.event
  def fail_one(key,*args):
   if key.startswith('remind:'+bad['id']+':'):raise RuntimeError('isolated reminder failure')
   return original(key,*args)
  with patch.object(self.s,'event',side_effect=fail_one):
   self.s.deliver_reminders()
  self.assertEqual(self.events_for(bad['id']),[])
  self.assertEqual(len(self.events_for(good['id'])),1)
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(bad['id'],))['active'],1)
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(good['id'],))['active'],0)

 def test_repeating_occurrences_are_exactly_once_across_restart(self):
  with patch('supervisor.time.time',return_value=2000):
   r = self.reminder(at=1900,every_minutes=5)
   self.s.deliver_reminders()
   self.s.deliver_reminders()
   self.assertEqual(len(self.events_for(r['id'])),1)
   first_next = self.s.one('SELECT due FROM reminders WHERE id=?',(r['id'],))['due']
   self.assertEqual(first_next,2200)
   self.s.db.close(); self.s = Supervisor(self.tmp.name+'/db',self.api)
   self.s.deliver_reminders()
   self.assertEqual(len(self.events_for(r['id'])),1)
  with patch('supervisor.time.time',return_value=2300):
   self.s.deliver_reminders();self.s.deliver_reminders()
  self.assertEqual(len(self.events_for(r['id'])),2)
  audits = self.audit_for(r['id'])
  self.assertEqual([x['due'] for x in audits if x['status']=='delivered'],[1900,2200])
  self.assertEqual(audits[-1]['status'],'scheduled')
  self.assertEqual(self.api.sent,[])

 def test_cancel_settles_both_unsent_kinds_for_exact_id_only(self):
  with patch('supervisor.uid',side_effect=['shared','shared-extra']):
   target = self.reminder(in_minutes=30)
   other = self.reminder(in_minutes=30)
  for rid in (target['id'],other['id']):
   self.s.event('remind:'+rid+':100',self.p,'w','check-in','pending reminder')
   self.s.event('progress:'+rid+':100',self.p,'w','progress','{}')
  self.s.dispatch({'op':'cancel_reminder','id':target['id']})
  for kind in ('remind','progress'):
   self.assertEqual(self.s.one('SELECT acked FROM events WHERE event_key=?',(kind+':shared:100',))['acked'],1)
   self.assertEqual(self.s.one('SELECT acked FROM events WHERE event_key=?',(kind+':shared-extra:100',))['acked'],0)
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(other['id'],))['active'],1)
  self.assertTrue(any(x['status']=='cancelled' and x['reason'] for x in self.audit_for(target['id'])))
  self.assertEqual(self.api.sent,[])

 def test_cancel_does_not_withdraw_or_replay_an_already_accepted_wake(self):
  self.s.set('manager','s')
  r = self.reminder()
  self.s.deliver_reminders();self.s.notify_manager()
  event = self.events_for(r['id'])[0]
  self.assertIsNotNone(event['wake_id'])
  self.assertEqual(len(self.api.sent),1)
  self.s.dispatch({'op':'cancel_reminder','id':r['id']})
  self.assertEqual(self.events_for(r['id'])[0]['wake_id'],event['wake_id'])
  self.assertEqual(self.s.one('SELECT state FROM outbox WHERE id=?',(event['wake_id'],))['state'],'sent')
  self.s.deliver_reminders();self.s.notify_manager()
  self.assertEqual(len(self.api.sent),1)

 def test_pending_reminder_waits_for_explicit_manager_unpause_then_wakes_once(self):
  self.s.set('manager','s')
  r = self.reminder()
  self.api.session['manualPausedAt']=123
  self.s.deliver_reminders();self.s.notify_manager();self.s.notify_manager()
  self.assertEqual(self.api.sent,[])
  self.assertIsNone(self.events_for(r['id'])[0]['wake_id'])
  self.api.session.pop('manualPausedAt')  # Simulates the user's explicit Resume action.
  self.s.notify_manager();self.s.notify_manager()
  self.assertEqual(len(self.api.sent),1)
  self.assertIsNotNone(self.events_for(r['id'])[0]['wake_id'])

 def test_unmanage_cancels_schedule_and_remanage_never_rearms_it(self):
  r = self.reminder(in_minutes=30)
  self.s.end_management(self.s.worker('w'))
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(r['id'],))['active'],0)
  audit = self.audit_for(r['id'])
  self.assertTrue(any(x['status']=='cancelled' and x['reason'] for x in audit))
  node_graph.remanage(self.s,self.s.worker('w'))
  self.assertEqual(self.s.worker('w')['state'],'managed')
  with patch('supervisor.time.time',return_value=r['due']+1):self.s.deliver_reminders()
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(r['id'],))['active'],0)
  self.assertEqual(self.events_for(r['id']),[])
  self.assertEqual(self.api.sent,[])

 def test_progress_savepoint_rolls_back_prior_ack_new_event_and_signature_on_failure(self):
  r = self.reminder(progress=True)
  self.s.event('old-progress',self.p,'w','progress','{}')
  snapshot = {'status':'running','latest_reply':'New progress','latest_tool':None,'pending_requests':[]}
  original = self.s.event
  def fail_after_insert(*args):
   original(*args)
   raise RuntimeError('progress transaction interrupted')
  with patch.object(self.s,'progress_snapshot',return_value=snapshot), patch.object(self.s,'event',side_effect=fail_after_insert):
   self.s.deliver_reminders()
  self.assertEqual(self.s.one('SELECT acked FROM events WHERE event_key=?',('old-progress',))['acked'],0)
  self.assertEqual(len(self.s.rows('SELECT * FROM events')),1)
  self.assertIsNone(self.s.get('progress-last:w'))
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(r['id'],))['active'],1)
  self.assertEqual(self.audit_for(r['id'])[0]['status'],'failed')
  self.assertEqual(self.api.sent,[])

 def test_idle_progress_occurrence_is_audited_skipped_without_a_wake_or_retry(self):
  r = self.reminder(progress=True)
  with patch.object(self.s,'progress_snapshot',return_value={'status':'idle'}):
   self.s.deliver_reminders();self.s.deliver_reminders()
  self.assertEqual(self.s.rows('SELECT * FROM events'),[])
  self.assertEqual(self.s.one('SELECT active FROM reminders WHERE id=?',(r['id'],))['active'],0)
  audit = self.audit_for(r['id'])[0]
  self.assertEqual(audit['status'],'skipped')
  self.assertIsNone(audit['event_key'])
  self.assertIsNone(audit['next_attempt_at'])
  self.assertEqual(self.api.sent,[])
