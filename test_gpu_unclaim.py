import copy
import tempfile
import time
import unittest
from unittest.mock import patch

from supervisor import Supervisor
from test_supervisor import FakeHapi, IsolatedSupervisorTest
from managerctl import stamp_command

GPU = 'main/GPU-64a43df7-51bc-56f4-957a-98c3793a2a45'
REMOTE = 'render-pc/GPU-64a43df7-51bc-56f4-957a-98c3793a2a46'


class GpuUnclaimTests(IsolatedSupervisorTest):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.api = FakeHapi()
        self.s = Supervisor(self.tmp.name + '/db', self.api)
        self.project = self.s.dispatch({'op': 'register', 'name': 'Paused', 'repo': self.tmp.name, 'objective': 'Keep goal'})['id']
        self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)', ('w', self.project, 'claude', 'n', 's', self.tmp.name, 'managed', 1))
        self.s.db.execute('INSERT INTO assignments(id,worker,task_key,text,state,created,goal) VALUES(?,?,?,?,?,?,?)', ('a','w','goal','/goal original','sent',1,1))
        self.s.db.execute('INSERT INTO resources VALUES(?,?)', (GPU, self.project))
        self.s.db.execute('INSERT INTO resources VALUES(?,?)', ('browser', self.project))
        self.api.session.update(manualPausedAt=123, goal={'status':'paused','objective':'Keep goal'})
        self.req = {'op':'gpu_unclaim', 'project':self.project, 'resource':GPU}

    def tearDown(self):
        self.s.db.close(); self.tmp.cleanup()

    def commit(self, preview, **changes):
        return self.s.dispatch({**self.req, **preview, 'commit':True, 'user_requested':True,
                                'basis':'User explicitly clears GPU claims', 'requested_at':time.time(), **changes})

    def test_paused_goal_preserved_no_probes_or_process_effects(self):
        before = {t:self.s.rows('SELECT * FROM '+t) for t in ('projects','workers','assignments','events','outbox')}
        session = copy.deepcopy(self.api.session)
        with patch.object(self.api, 'call', side_effect=AssertionError('no API calls')), \
             patch('subprocess.run', side_effect=AssertionError('no processes')), \
             patch('os.kill', side_effect=AssertionError('no signals')):
            review = self.s.dispatch(self.req)
            self.assertTrue(review['readOnly']); self.assertFalse(review['committed'])
            result = self.commit(review)
        self.assertTrue(result['committed']); self.assertIn('not asserted', result['effect'])
        self.assertEqual(self.s.rows('SELECT name FROM resources'), [{'name':'browser'}])
        for table, value in before.items():self.assertEqual(self.s.rows('SELECT * FROM '+table),value)
        self.assertEqual(self.api.session,session); self.assertEqual(self.api.sent,[])
        self.assertEqual(len(self.s.rows("SELECT * FROM settings WHERE key LIKE 'gpu-unclaim-audit:%'")),1)

    def test_changed_row_and_aba_fail(self):
        review = self.s.dispatch(self.req)
        self.s.db.execute('DELETE FROM resources WHERE name=?',(GPU,))
        self.s.db.execute('INSERT INTO resources VALUES(?,?)',(GPU,self.project))
        with self.assertRaisesRegex(ValueError,'changed'): self.commit(review)
        self.assertEqual(self.s.one('SELECT project FROM resources WHERE name=?',(GPU,))['project'],self.project)

    def test_wrong_gpu_unauthorized_expired_and_bad_actor_refused(self):
        self.s.db.execute('INSERT INTO resources VALUES(?,?)',(REMOTE,self.project))
        review = self.s.dispatch(self.req)
        for change in ({'resource':REMOTE}, {'user_requested':False}, {'basis':''}, {'requested_at':time.time()-301},
                       {'actor':'non-overseer'}, {'review_token':'fake'}, {'reviewed_at':time.time()-301}):
            with self.assertRaises(ValueError):self.commit(review, **change)
        with self.assertRaisesRegex(ValueError,'GPU-UUID'):
            self.s.dispatch({**self.req,'resource':'browser'})
        self.assertEqual(len(self.s.rows('SELECT * FROM resources')),3)

    def test_preview_invalid_after_restart_and_managerctl_stamps(self):
        review = self.s.dispatch(self.req)
        import gpu_unclaim
        with patch.object(gpu_unclaim,'_KEY',b'a different supervisor key'):
            with self.assertRaisesRegex(ValueError,'invalid'):self.commit(review)
        with patch('managerctl.time.time',return_value=123):
            self.assertEqual(stamp_command({'op':'gpu_unclaim','requested_at':1},{})['requested_at'],123)


if __name__ == '__main__': unittest.main()
