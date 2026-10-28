import json
import unittest
from unittest.mock import patch
import test_supervisor as fixtures
from native_background import fresh_snapshot

def snapshot(when, jobs, thread='native'):
    return {'threadId':thread,'checkedAt':when*1000,'terminals':[{'processId':p,'itemId':i,'osPid':pid} for p,i,pid in jobs]}

class Native(fixtures.IsolatedSupervisorTest):
    setUp=fixtures.Tests.setUp
    tearDown=fixtures.Tests.tearDown
    observe=fixtures.Tests.observe
    observations=fixtures.Tests.observations
    set_public=fixtures.Tests.set_public
    public_progress=fixtures.Tests.public_progress
    def set_snap(self,when,jobs):
        self.api.session['metadata']={'flavor':'codex','codexSessionId':'native','codexBackgroundTerminals':snapshot(when,jobs)}
    def test_intent_bound_to_caller_live_process_and_no_action(self):
        self.set_snap(1000,[('42','item',123)])
        with patch('native_background.time.time',return_value=1000):
            result=self.s.dispatch({'op':'background_intent','actor':'s','process_id':'42','intent':'review','note':'Inspect render'})
            self.assertEqual(result['recorded']['itemId'],'item')
            for req in ({'actor':'other','process_id':'42','intent':'review'}, {'actor':'s','process_id':'99','intent':'review'}, {'actor':'s','session':'other','process_id':'42','intent':'review'}, {'actor':'s','process_id':'42','intent':'restart'}):
                with self.assertRaises(ValueError):self.s.dispatch({'op':'background_intent',**req})
        self.assertEqual(self.api.sent,[])
    def test_live_disappearance_reports_review_once_with_no_prompt(self):
        self.api.session.update(thinking=False,backgroundTaskCount=1)
        self.set_snap(1000,[('42','item',123)]);self.set_public('Render running.',1)
        with patch('native_background.time.time',return_value=1000):self.s.dispatch({'op':'background_intent','actor':'s','process_id':'42','intent':'review','note':'Inspect output'})
        self.observe(1000)
        self.set_snap(1010,[]);self.api.session['backgroundTaskCount']=0;self.observe(1010)
        changes=json.loads(self.observations()[0]['detail'])['changes']
        item=next(x for x in changes if x.get('category')=='native-terminal-ended')
        self.assertEqual(item['terminal']['osPid'],123);self.assertEqual(item['intent']['intent'],'review')
        self.s.db.execute('UPDATE events SET acked=1');self.observe(1011)
        self.assertEqual(self.observations(),[]);self.assertEqual(self.api.sent,[])
    def test_native_and_local_evidence_are_one_final_review_event(self):
        self.api.session.update(thinking=False,backgroundTaskCount=1,backgroundProcesses=[{'id':'boot:123:1','pid':123,'checkedAt':1000000}])
        self.set_snap(1000,[('42','item',123)]);self.set_public('Render running.',1);self.observe(1000)
        self.set_snap(1010,[]);self.api.session.update(backgroundTaskCount=0,backgroundProcesses=[]);self.observe(1010)
        changes=json.loads(self.observations()[0]['detail'])['changes']
        self.assertEqual(len(changes),1);self.assertEqual(changes[0]['category'],'background-ended')
        self.assertEqual(changes[0]['nativeTerminals'][0]['terminal']['processId'],'42')
    def test_expired_or_changed_thread_never_claims_terminal_exit(self):
        self.api.session.update(thinking=False,backgroundTaskCount=1)
        self.set_snap(1000,[('42','item',123)]);self.set_public('Work running.',1);self.observe(1000)
        self.set_snap(1100,[]);self.api.session['backgroundTaskCount']=0;self.observe(1100)
        changes=json.loads(self.observations()[0]['detail'])['changes']
        self.assertEqual(changes[0]['category'],'native-terminal-unconfirmed')
        self.s.db.execute('UPDATE events SET acked=1'); self.observe(1101)
        self.assertEqual(self.observations(),[])
    def test_stale_snapshot_cannot_authorize_intent(self):
        self.set_snap(1000,[('42','item',123)])
        with patch('native_background.time.time',return_value=1100):
            with self.assertRaises(ValueError):self.s.dispatch({'op':'background_intent','actor':'s','process_id':'42','intent':'review'})
    def test_paused_or_released_reporting_does_not_wake(self):
        self.api.session.update(thinking=False,backgroundTaskCount=1)
        self.set_snap(1000,[('42','item',123)]);self.set_public('Render running.',1);self.observe(1000)
        self.s.set('watch:w',json.dumps({'mode':'off','note':''}))
        self.set_snap(1010,[]);self.api.session.update(backgroundTaskCount=0,manualPausedAt=1);self.observe(1010)
        self.assertEqual(self.observations(),[]);self.assertEqual(self.api.sent,[])

if __name__=='__main__':unittest.main()
