"""Exact supersession under current user authority; no live operations."""
import copy
import json
import tempfile
import time
from unittest.mock import patch
from supervisor import Supervisor
from test_supervisor import FakeHapi, IsolatedSupervisorTest
from assignment_reconciliation import assignment_hash
from queued_input import input_hash
from public_history import read_inputs

GPU='main/GPU-exact'


class SupersessionTests(IsolatedSupervisorTest):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.api=FakeHapi();self.s=Supervisor(self.tmp.name+'/db',self.api)
        self.p=self.s.dispatch({'op':'register','name':'Terrain','repo':self.tmp.name})['id']
        self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',('w',self.p,'codex','native','s',self.tmp.name,'managed',time.time()-100))
        self.s.db.execute('INSERT INTO resources VALUES(?,?)',(GPU,self.p))
        now=time.time()
        self.s.db.execute('INSERT INTO assignments(id,worker,task_key,text,state,created,seen_busy,goal) VALUES(?,?,?,?,?,?,?,?)',('old','w','old-allocation','Old exact allocation including appended context','sent',now-100,1,0))
        self.s.db.execute('INSERT INTO assignments(id,worker,task_key,text,state,created,seen_busy,goal) VALUES(?,?,?,?,?,?,?,?)',('goal','w','goal','/goal Retain objective','sent',now-120,1,1))
        self.api.session.update(metadata={'codexSessionId':'native'},manualPausedAt=123,goal={'status':'active','objective':'Retain objective'})
        self.new={'id':'replacement','localId':'new-local','createdAt':(now-10)*1000,'invokedAt':(now-9)*1000,'scheduledAt':None,'content':{'role':'user','content':{'type':'text','text':'Current exact replacement allocation'}}}
        self.api.messages=[self.new];self.proof={'queuedLocalIds':[],'indeterminateLocalIds':[],'invokedLocalMessages':[{'localId':'new-local','invokedAt':self.new['invokedAt']}]}
        original=self.api.call
        def call(path,data=None,method=None):
            if path.endswith('/queued-state'):return copy.deepcopy(self.proof)
            if method or (data is not None and not path.endswith('/queued-state')):raise AssertionError('No runtime mutation allowed')
            return original(path,data,method)
        self.api.call=call
        self.queue={'session':'s','native_id':'native','readOnly':True,'items':[]}
        self.native_patch=patch('assignment_reconciliation.native_queue',side_effect=lambda *args:copy.deepcopy(self.queue));self.native_patch.start()
        self.request={'op':'supersede_assignment','worker':'w','session':'s','native_id':'native','assignment':'old','assignment_hash':assignment_hash(self.assignment()),
                      'superseded_by':'replacement','replacement_local_id':'new-local','replacement_hash':input_hash(self.new),'resource':GPU,
                      'reason':'User reviewed exact allocation and superseded it with the current consolidated allocation',
                      'basis':'Current user explicitly supersedes this exact historical allocation instruction','user_requested':True,'requested_at':now}

    def tearDown(self):self.native_patch.stop();self.s.db.close();self.tmp.cleanup()
    def assignment(self):return self.s.one("SELECT * FROM assignments WHERE id='old'")
    def call(self,**changes):return self.s.dispatch({**self.request,**changes})

    def test_review_is_read_only_unknown_delivery_is_never_consumption(self):
        before={t:self.s.rows('SELECT * FROM '+t) for t in ('workers','projects','assignments','resources','events','outbox','settings')}
        result=self.call();self.assertTrue(result['ready']);self.assertFalse(result['committed'])
        self.assertEqual(result['priorDelivery']['state'],'unknown');self.assertEqual(result['outcome'],'unverified')
        for table,rows in before.items():self.assertEqual(self.s.rows('SELECT * FROM '+table),rows)
        self.assertEqual(self.api.sent,[])

    def test_commit_supersedes_only_exact_instruction_and_preserves_unknown_audit_after_restart(self):
        before={t:self.s.rows('SELECT * FROM '+t) for t in ('workers','projects','resources','events','outbox','reminders')};runtime=copy.deepcopy(self.api.session)
        result=self.call(commit=True);self.assertTrue(result['committed']);self.assertEqual(self.assignment()['state'],'superseded')
        self.assertEqual(self.s.one("SELECT * FROM assignments WHERE id='goal'")['state'],'sent')
        self.assertEqual(self.s.get('assignment-supersession:old'),json.dumps(result))
        self.assertEqual(result['priorDelivery']['state'],'unknown');self.assertEqual(result['priorAssignmentState'],'sent')
        self.assertTrue(self.call(commit=True)['alreadySuperseded'])
        for table,rows in before.items():self.assertEqual(self.s.rows('SELECT * FROM '+table),rows)
        self.assertEqual(self.api.session,runtime);self.assertEqual(self.api.sent,[])
        self.s.db.close();self.s=Supervisor(self.tmp.name+'/db',self.api)
        out=self.s.dispatch({'op':'assignment_delivery','worker':'w','assignment':'old','historical':True})
        self.assertEqual(out['state'],'unknown');self.assertEqual(out['assignmentState'],'superseded')
        self.assertEqual(out['supersession']['priorDelivery']['state'],'unknown')

    def test_authority_binding_hash_and_exact_replacement_required(self):
        for changes in ({'user_requested':False},{'requested_at':time.time()-301},{'basis':''},{'session':'other'},{'native_id':'other'},
                        {'assignment_hash':'wrong'},{'replacement_hash':'wrong'},{'replacement_local_id':'other'},{'superseded_by':'missing'},
                        {'resource':'main/GPU-unclaimed'},{'reason':'force'},{'commit':'true'}):
            with self.assertRaises((ValueError,KeyError)):self.call(**changes)
        self.assertEqual(self.assignment()['state'],'sent')

    def test_goals_commands_sending_and_uncertain_dispatch_are_never_retired(self):
        for updates in ({'goal':1},{'text':'/goal original'},{'state':'sending'},{'state':'uncertain'}):
            original=self.assignment()
            for k,v in updates.items():self.s.db.execute('UPDATE assignments SET '+k+'=? WHERE id=?',(v,'old'))
            with self.assertRaises(ValueError):self.call(assignment_hash=assignment_hash(self.assignment()))
            for k in updates:self.s.db.execute('UPDATE assignments SET '+k+'=? WHERE id=?',(original[k],'old'))
        self.assertEqual(self.assignment()['state'],'sent')

    def test_no_prose_or_missing_wrong_future_or_precreation_receipt_can_replace_intake(self):
        cases=[[],[{'localId':'wrong','invokedAt':self.new['invokedAt']}],[{'localId':'new-local','invokedAt':None}],
               [{'localId':'new-local','invokedAt':self.new['createdAt']-1}],[{'localId':'new-local','invokedAt':(time.time()+100)*1000}]]
        for receipts in cases:
            self.proof['invokedLocalMessages']=receipts
            with self.assertRaises(ValueError):self.call(commit=True)
        self.assertEqual(self.assignment()['state'],'sent')

    def test_queued_uncertain_or_live_native_inputs_are_not_discarded(self):
        for key in ('queuedLocalIds','indeterminateLocalIds'):
            for identity in ('old','new-local'):
                self.proof[key]=[identity]
                with self.assertRaises(ValueError):self.call(commit=True)
            self.proof[key]=[]
        for identity in ('old','new-local'):
            self.queue['items']=[{'clientUserMessageId':identity}]
            with self.assertRaises(ValueError):self.call(commit=True)
        self.assertEqual(self.assignment()['state'],'sent')

    def test_replacement_control_schedule_and_reordered_input_refused(self):
        for change in ({'createdAt':1},{'scheduledAt':time.time()*1000+1000},
                       {'content':{'role':'user','content':{'type':'text','text':'/goal change objective'}}}):
            original=copy.deepcopy(self.new);self.new.update(change)
            with self.assertRaises(ValueError):self.call(commit=True,replacement_hash=input_hash(self.new))
            self.new.clear();self.new.update(original)

    def test_edit_or_binding_change_during_commit_refuses_and_pause_never_unlatches(self):
        original=self.api.call;reads=0
        def call(path,data=None,method=None):
            nonlocal reads
            if path.endswith('/queued-state'):
                reads+=1
                if reads>=3:self.new['content']['content']['text']='Edited genuine input'
            return original(path,data,method)
        self.api.call=call
        with self.assertRaises(ValueError):self.call(commit=True)
        self.assertEqual(self.assignment()['state'],'sent');self.assertEqual(self.api.session['manualPausedAt'],123)
        with patch.object(self.s,'manager_paused',return_value=True):
            with self.assertRaisesRegex(ValueError,'manually paused'):self.call(commit=True)
        self.assertEqual(self.assignment()['state'],'sent')

    def test_actor_guard_prevents_worker_from_retiring_assignments(self):
        with patch('node_graph.active',return_value=True),patch('node_graph.overseer',return_value='root'):
            with self.assertRaisesRegex(ValueError,'Only the overseer'):self.call(actor='worker-child')

    def test_history_pages_past_4000_and_epoch_or_stalled_cursor_never_proves_delivery(self):
        original=self.api.call
        count=0
        def call(path,data=None,method=None):
            nonlocal count
            if '/messages?' in path:
                count+=1
                if count<23:return {'messages':[], 'page':{'epoch':1,'hasMore':True,'nextBeforeAt':100000-count,'nextBeforeSeq':100000-count}}
                return {'messages':[self.new], 'page':{'epoch':1,'hasMore':False}}
            return original(path,data,method)
        self.api.call=call
        msgs,coverage=read_inputs(self.api,'s',message_ids=['replacement'])
        self.assertEqual(coverage['pages'],23);self.assertEqual(msgs,[self.new])
        self.api.call=lambda *a,**kw:{'messages':[], 'page':{'epoch':1,'hasMore':True,'nextBeforeAt':100,'nextBeforeSeq':100}}
        with self.assertRaisesRegex(ValueError,'stalled'):read_inputs(self.api,'s',message_ids=['missing'])

    def test_handoff_excludes_only_retired_instruction_while_goal_stays_active_and_unknown_audit_remains(self):
        self.call(commit=True)
        target=self.s.dispatch({'op':'register','name':'SampleFluid','repo':self.tmp.name})['id']
        now=time.time()*1000
        release='The main 3090 Ti is released; no more local GPU jobs will be started.'
        self.api.messages.append({'id':'release','seq':999,'createdAt':now-1000,'content':{'role':'agent','content':{'type':'codex','data':{'type':'message','message':release}}}})
        self.proof['invokedLocalMessages'].append({'localId':'goal','invokedAt':now-100000})
        request={'op':'resource_handoff','project':self.p,'to_project':target,'resource':GPU,'commit':True,
                 'acknowledgements':[{'worker':'w','session':'s','native_id':'native','message':'release','quote':release}],
                 'user_requested':True,'basis':'Current user explicitly allocates the main GPU to SampleFluid','requested_at':time.time()}
        with patch('resource_handoff.gpu_snapshot',return_value={'resource':GPU,'busy':False,'processes':[],'observedAt':time.time()}):
            result=self.s.dispatch(request)
        self.assertTrue(result['committed'])
        self.assertEqual(result['acknowledgements'][0]['preservedAssignments'],['goal'])
        self.assertEqual(self.assignment()['state'],'superseded')
        self.assertEqual(json.loads(self.s.get('assignment-supersession:old'))['priorDelivery']['state'],'unknown')
        self.assertEqual(self.api.session['goal']['status'],'active');self.assertEqual(self.api.session['manualPausedAt'],123)
        self.assertEqual(self.api.sent,[])

    def test_historical_receipt_lookup_uses_exact_paginated_input_without_guessing_outcome(self):
        original=self.api.call
        old={'id':'old-native-echo','localId':'old','createdAt':self.assignment()['created']*1000,'invokedAt':self.new['createdAt']-1000,
             'content':{'role':'user','content':{'type':'text','text':self.assignment()['text']}}}
        def call(path,data=None,method=None):
            if '/messages?' in path:
                if 'beforeAt' not in path:return {'messages':[self.new],'page':{'epoch':2,'hasMore':True,'nextBeforeAt':123,'nextBeforeSeq':123}}
                return {'messages':[old],'page':{'epoch':2,'hasMore':False}}
            return original(path,data,method)
        self.api.call=call
        result=self.s.dispatch({'op':'assignment_delivery','worker':'w','assignment':'old','historical':True})
        self.assertEqual(result['state'],'consumed');self.assertEqual(result['invokedAt'],old['invokedAt'])
        self.assertEqual(result['compliance'],'unverified');self.assertEqual(result['historyCoverage']['pages'],2)
        self.assertEqual(self.assignment()['state'],'sent');self.assertEqual(self.api.sent,[])
