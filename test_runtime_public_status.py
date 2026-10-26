"""Public status extraction distinguishes assistant output from runtime control events."""
import json
import unittest

from coordination import waiting_snapshot
from resource_handoff import public_reply
import test_supervisor as fixtures


def message(kind, data, seq=1):
 return {'id':'m'+str(seq),'seq':seq,'createdAt':seq*1000,
         'content':{'role':'agent','content':{'type':kind,'data':data}}}


# Shape of live message68bc7848: /goal's visible status serializes an objective.
GOAL_JSON = json.dumps({'goal':{'objective':'Prepare CPU work now; wait manager GPU handoff before sim/render, no competing jobs.','status':'active'}})


class RuntimePublicStatus(fixtures.IsolatedSupervisorTest):
 setUp = fixtures.Tests.setUp
 tearDown = fixtures.Tests.tearDown

 def test_goal_runtime_json_is_not_public_assistant_prose(self):
  runtime = message('event',{'type':'message','message':GOAL_JSON})
  self.assertEqual(public_reply(runtime),'')
  self.assertFalse(waiting_snapshot([runtime]))
  snapshot = self.s.progress_snapshot(self.s.worker('w'),self.api.session,[runtime])
  self.assertIsNone(snapshot['latest_reply'])
  self.assertIsNone(snapshot['evidence_message'])
  self.assertEqual(snapshot['status'],'idle')

 def test_runtime_goal_does_not_override_cpu_activity_after_a_real_wait(self):
  messages = [message('codex',{'type':'message','message':'Waiting for GPU handoff.'},1),
              message('codex',{'type':'tool-call','name':'Bash','input':{'description':'CPU scene preparation'}},2),
              message('event',{'type':'message','message':GOAL_JSON},3)]
  self.api.session['thinking'] = True
  snapshot = self.s.progress_snapshot(self.s.worker('w'),self.api.session,messages)
  self.assertEqual(snapshot['status'],'running')
  self.assertEqual(snapshot['latest_tool'],'Bash')
  self.assertEqual(snapshot['latest_reply'],'Waiting for GPU handoff.')
  self.assertEqual(snapshot['evidence_message'],'m1')

 def test_runtime_goal_does_not_hide_claude_cpu_tool_activity(self):
  tool = message('output',{'type':'assistant','message':{'role':'assistant','content':[{'type':'tool_use','name':'Bash','input':{'description':'Preparing CPU mesh'}}]}},2)
  messages = [tool,message('event',{'type':'message','message':GOAL_JSON},3)]
  self.api.session['thinking'] = True
  snapshot = self.s.progress_snapshot(self.s.worker('w'),self.api.session,messages)
  self.assertEqual(snapshot['status'],'running')
  self.assertEqual(snapshot['latest_tool'],'Bash: Preparing CPU mesh')
  self.assertIsNone(snapshot['latest_reply'])

 def test_legitimate_assistant_json_remains_available_in_all_supported_prose_envelopes(self):
  codex = message('codex',{'type':'message','message':GOAL_JSON,'live':False})
  claude = message('output',{'type':'assistant','message':{'role':'assistant','content':[{'type':'text','text':GOAL_JSON}]}})
  direct = {'id':'direct','seq':1,'content':{'role':'agent','content':{'type':'text','text':GOAL_JSON}}}
  for reply in (codex,claude,direct):
   with self.subTest(reply=reply):
    self.assertEqual(public_reply(reply),GOAL_JSON)
    snapshot = self.s.progress_snapshot(self.s.worker('w'),self.api.session,[reply],reply_limit=None)
    self.assertEqual(snapshot['latest_reply'],GOAL_JSON)
    self.assertEqual(snapshot['evidence_message'],reply['id'])

 def test_runtime_status_and_tool_results_never_supply_wait_or_release_prose(self):
  for envelope,data in [('event',{'type':'message','message':'GPU is free. Waiting for GPU handoff.'}),
                        ('codex',{'type':'tool-call-result','message':'GPU is free. Waiting for GPU handoff.'}),
                        ('output',{'type':'user','message':{'role':'user','content':[{'type':'text','text':'GPU is free.'}]}})]:
   with self.subTest(envelope=envelope,data=data):
    runtime = message(envelope,data)
    self.assertEqual(public_reply(runtime),'')
    self.assertFalse(waiting_snapshot([runtime]))
    self.assertIsNone(self.s.progress_snapshot(self.s.worker('w'),self.api.session,[runtime])['latest_reply'])

 def test_terminal_summary_retained_without_becoming_new_public_wait_evidence(self):
  final = message('codex',{'type':'task_complete','last_agent_message':'Waiting for GPU handoff.'})
  self.assertEqual(public_reply(final),'')
  self.assertEqual(public_reply(final,include_terminal=True),'Waiting for GPU handoff.')
  snapshot = self.s.progress_snapshot(self.s.worker('w'),self.api.session,[final])
  self.assertEqual(snapshot['latest_reply'],'Waiting for GPU handoff.')
  self.assertEqual(snapshot['status'],'idle')
  self.assertFalse(waiting_snapshot([final]))

 def test_repeated_terminal_reply_does_not_override_more_recent_cpu_activity(self):
  messages = [message('codex',{'type':'message','message':'Waiting for GPU handoff.'},1),
              message('codex',{'type':'tool-call','name':'Bash'},2),
              message('codex',{'type':'task_complete','last_agent_message':'Waiting for GPU handoff.'},3)]
  self.api.session['thinking'] = True
  snapshot = self.s.progress_snapshot(self.s.worker('w'),self.api.session,messages)
  self.assertEqual(snapshot['latest_reply'],'Waiting for GPU handoff.')
  self.assertEqual(snapshot['latest_tool'],'Bash')
  self.assertEqual(snapshot['status'],'running')
  self.assertFalse(waiting_snapshot(messages))

 def test_live_stream_and_reasoning_are_not_promoted_to_public_status(self):
  for data in ({'type':'message','message':'GPU is free.','live':True},
               {'type':'reasoning','message':'Waiting for GPU handoff.'}):
   runtime = message('codex',data)
   self.assertEqual(public_reply(runtime),'')
   self.assertIsNone(self.s.progress_snapshot(self.s.worker('w'),self.api.session,[runtime])['latest_reply'])


if __name__ == '__main__':unittest.main()
