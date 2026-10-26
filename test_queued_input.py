import copy
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from supervisor import Supervisor
from test_supervisor import FakeHapi, IsolatedSupervisorTest
from queued_input import input_hash


class InputTests(IsolatedSupervisorTest):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.api=FakeHapi();self.s=Supervisor(self.tmp.name+'/db',self.api)
        self.project=self.s.dispatch({'op':'register','name':'Terrain','repo':self.tmp.name})['id']
        self.s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',('w',self.project,'codex','native','s',self.tmp.name,'managed',time.time()))
        self.api.session.update(metadata={'codexSessionId':'native'},goal={'status':'paused','objective':'Keep original'},manualPausedAt=123)
        now=time.time()*1000
        self.old={'id':'old','localId':'queued-id','createdAt':now-10000,'invokedAt':None,'content':{'role':'user','content':{'type':'text','text':'Old allocation'}}}
        self.new={'id':'new','localId':'accepted-id','createdAt':now-5000,'invokedAt':now-4000,'content':{'role':'user','content':{'type':'text','text':'New allocation superseding old allocation'}}}
        self.api.messages=[self.old,self.new];self.deletes=[];self.reply={'status':'cancelled','localId':'queued-id'}
        self.proof={'queuedLocalIds':['queued-id'],'indeterminateLocalIds':[],'invokedLocalMessages':[{'localId':'accepted-id','invokedAt':now-4000}]}
        original=self.api.call
        def call(path,data=None,method=None):
            if path=='/sessions/s/messages?limit=200':return {'messages':self.api.messages}
            if path.endswith('/queued-state'):return self.proof
            if method=='DELETE':self.deletes.append(path);return copy.deepcopy(self.reply)
            return original(path,data,method)
        self.api.call=call
        self.request={'op':'cancel_queued','worker':'w','session':'s','native_id':'native','message':'old','local_id':'queued-id','input_hash':input_hash(self.old),
                      'superseded_by':'new','replacement_hash':input_hash(self.new),'reason':'Reviewed old allocation explicitly superseded by later current user direction',
                      'basis':'Current user authorizes this exact superseded-input cancellation','user_requested':True,'requested_at':time.time()}

    def tearDown(self):self.s.db.close();self.tmp.cleanup()

    def test_read_only_review_keeps_input_and_every_goal_assignment_unchanged(self):
        result=self.s.dispatch({'op':'queued_input','worker':'w'})
        self.assertEqual(result['inputs'][0]['message'],'old');self.assertEqual(result['inputs'][0]['input_hash'],input_hash(self.old))
        self.assertEqual(self.deletes,[]);self.assertIsNone(self.old['invokedAt']);self.assertEqual(self.api.sent,[])

    def test_only_native_confirmed_cancellation_is_reported_and_work_is_preserved(self):
        before={t:self.s.rows('SELECT * FROM '+t) for t in ('workers','projects','assignments','resources','events','outbox','reminders')}
        runtime=copy.deepcopy(self.api.session)
        result=self.s.dispatch(self.request)
        self.assertEqual(result['status'],'cancelled');self.assertEqual(self.deletes,['/sessions/s/messages/old'])
        self.assertEqual(self.api.session,runtime);self.assertIsNone(self.old['invokedAt']);self.assertEqual(self.api.sent,[])
        for t,rows in before.items():self.assertEqual(self.s.rows('SELECT * FROM '+t),rows)
        audit=self.s.rows("SELECT value FROM settings WHERE key LIKE 'queued-input-cancel:%:audit:%'")
        self.assertEqual(json.loads(audit[0]['value'])['replacementInvokedAt'],self.new['invokedAt'])

    def test_busy_invoked_and_missing_cancel_identity_never_become_cancel_success(self):
        for status in ('busy','invoked'):
            self.reply={'status':status,'localId':'queued-id'}
            self.assertEqual(self.s.dispatch(self.request)['status'],status)
        self.reply={'status':'cancelled','localId':None}
        self.assertEqual(self.s.dispatch(self.request)['status'],'uncertain')
        self.assertIsNone(self.old['invokedAt'])

    def test_timeout_is_audited_uncertain_and_never_blindly_repeated(self):
        original=self.api.call
        def fail(path,data=None,method=None):
            if method=='DELETE':self.deletes.append(path);raise TimeoutError()
            return original(path,data,method)
        self.api.call=fail
        with self.assertRaisesRegex(ValueError,'outcome unknown'):self.s.dispatch(self.request)
        self.assertEqual(self.s.dispatch(self.request)['status'],'uncertain');self.assertEqual(len(self.deletes),1)

    def test_receipts_not_prose_and_no_cancellation_of_genuine_unreplaced_input(self):
        for bad in ([],[{'localId':'different','invokedAt':self.new['invokedAt']}],[{'localId':'accepted-id','invokedAt':None}]):
            self.proof['invokedLocalMessages']=bad
            with self.assertRaises(ValueError):self.s.dispatch(self.request)
        self.proof['queuedLocalIds']=['queued-id','accepted-id']
        with self.assertRaises(ValueError):self.s.dispatch(self.request)
        self.assertEqual(self.deletes,[])

    def test_current_authorization_and_exact_review_are_required(self):
        for change in ({'user_requested':False},{'requested_at':time.time()-301},{'basis':''},{'reason':'blocking'},
                       {'input_hash':'edited'},{'replacement_hash':'edited'},{'local_id':'other'}, {'message':'missing'},
                       {'session':'wrong'},{'native_id':'wrong'}):
            with self.assertRaises(ValueError):self.s.dispatch({**self.request,**change})
        self.assertEqual(self.deletes,[])

    def test_newer_input_and_goal_control_cannot_be_discarded_using_older_directive(self):
        self.old['createdAt']=self.new['createdAt']+1
        with self.assertRaises(ValueError):self.s.dispatch(self.request)
        self.old['createdAt']=1;self.new['content']['content']['text']='/goal update a goal'
        with self.assertRaises(ValueError):self.s.dispatch({**self.request,'replacement_hash':input_hash(self.new)})
        self.assertEqual(self.deletes,[])

    def test_manager_manual_pause_stays_authoritative(self):
        with patch.object(self.s,'manager_paused',return_value=True):
            with self.assertRaisesRegex(ValueError,'manually paused'):self.s.dispatch(self.request)
        self.assertEqual(self.deletes,[]);self.assertEqual(self.api.session['manualPausedAt'],123)

    def test_wrong_native_binding_and_changed_content_refuse(self):
        self.api.session['metadata']['codexSessionId']='other'
        with self.assertRaisesRegex(ValueError,'binding changed'):self.s.dispatch(self.request)
        self.assertEqual(self.deletes,[])

    def test_already_consumed_input_is_never_cancelled(self):
        self.old['invokedAt']=time.time()*1000
        self.assertEqual(self.s.dispatch(self.request)['status'],'invoked');self.assertEqual(self.deletes,[])

    def test_edit_during_review_is_refused_before_cancel_transport(self):
        original=self.api.call;reads=0
        def call(path,data=None,method=None):
            nonlocal reads
            result=original(path,data,method)
            if path.endswith('messages?limit=200'):
                reads+=1
                if reads>1:
                    result=copy.deepcopy(result);result['messages'][0]['content']['content']['text']='A new genuine instruction'
            return result
        self.api.call=call
        with self.assertRaisesRegex(ValueError,'changed during review'):self.s.dispatch(self.request)
        self.assertEqual(self.deletes,[])

    def test_exact_input_lookup_pages_older_messages_and_rejects_epoch_changes(self):
        original=self.api.call;change_epoch=False;calls=[]
        def call(path,data=None,method=None):
            if path.startswith('/sessions/s/messages?'):
                calls.append(path)
                if 'beforeAt' not in path:return {'messages':[], 'page':{'hasMore':True,'epoch':2,'nextBeforeAt':1000,'nextBeforeSeq':100}}
                return {'messages':self.api.messages,'page':{'hasMore':False,'epoch':3 if change_epoch else 2}}
            return original(path,data,method)
        self.api.call=call
        self.assertEqual(self.s.dispatch({'op':'queued_input','worker':'w','message':'old'})['inputs'][0]['message'],'old')
        self.assertIn('beforeAt=1000&beforeSeq=100',calls[1]);self.assertNotIn('epoch=',calls[1])
        change_epoch=True
        with self.assertRaisesRegex(ValueError,'history changed'):self.s.dispatch({'op':'queued_input','worker':'w','message':'old'})
        self.assertEqual(self.deletes,[])

    def test_review_is_overseer_scoped_in_node_mode(self):
        self.s.set('node-graph-active','1');self.s.set('manager','root')
        with self.assertRaisesRegex(ValueError,'Only the overseer'):self.s.dispatch({'op':'queued_input','worker':'w','actor':'other'})
        self.assertEqual(self.s.dispatch({'op':'queued_input','worker':'w','actor':'root'})['worker'],'w')


class DispositionTests(IsolatedSupervisorTest):
    def setUp(self):
        InputTests.setUp(self)
        self.request={k:v for k,v in self.request.items() if k not in ('superseded_by','replacement_hash')}
        self.request.update(disposition='cancel',reason='Root current user explicitly reviewed this exact obsolete allocation and asked cancellation')
    tearDown=InputTests.tearDown

    def test_current_explicit_cancel_needs_no_fabricated_replacement_intake(self):
        self.api.messages.remove(self.new)
        self.proof['invokedLocalMessages']=[]
        self.assertEqual(self.s.dispatch(self.request)['status'],'cancelled')
        self.assertIsNone(self.old['invokedAt']);self.assertEqual(self.api.sent,[])

    def test_explicit_cancel_still_refuses_wrong_scope_hash_pause_unknown_or_missing_authority(self):
        for change in ({'basis':''},{'user_requested':False},{'input_hash':'edited'},{'session':'other'}):
            with self.assertRaises(ValueError):self.s.dispatch({**self.request,**change})
        self.proof.pop('invokedLocalMessages')
        with self.assertRaisesRegex(ValueError,'incomplete'):self.s.dispatch(self.request)
        self.assertEqual(self.deletes,[])

    def test_explicit_cancel_does_not_override_confirmed_intake_or_uncertainty(self):
        self.proof['invokedLocalMessages']=[{'localId':'queued-id','invokedAt':time.time()*1000}]
        with self.assertRaises(ValueError):self.s.dispatch(self.request)
        self.assertEqual(self.deletes,[])

class SteerTests(IsolatedSupervisorTest):
    def setUp(self):
        InputTests.setUp(self)
        self.api.session['manualPausedAt']=None
        self.s.dispatch({'op':'resume','project':self.project})
        self.calls=[];self.response={'status':'steered','localId':'queued-id'};self.receipt=False
        original=self.api.call
        def call(path,data=None,method=None):
            if path=='/sessions/manager':return {'session':{'id':'manager','active':True}}
            if path.endswith('/steer'):
                self.calls.append((path,data,method))
                if isinstance(self.response,Exception):raise self.response
                if self.receipt:self.proof={'queuedLocalIds':[],'indeterminateLocalIds':[],'invokedLocalMessages':[{'localId':'queued-id','invokedAt':time.time()*1000}]}
                return self.response
            return original(path,data,method)
        self.api.call=call
        self.s.set('manager','manager')
        self.request={k:v for k,v in self.request.items() if k not in ('superseded_by','replacement_hash')}
        self.request.update(op='steer_queued',reason='Root reviewed this valid current allocation notice; deliver the existing exact message without interruption')
    tearDown=InputTests.tearDown

    def test_promotes_existing_message_without_resend_abort_goal_or_assignment_changes(self):
        before=copy.deepcopy(self.api.session)
        tables={t:self.s.rows('SELECT * FROM '+t) for t in ('assignments','workers','projects','resources','events','outbox')}
        result=self.s.dispatch(self.request)
        self.assertEqual(self.calls,[('/sessions/s/messages/old/steer',{},'POST')])
        self.assertTrue(result['rpcAccepted']);self.assertFalse(result['acknowledged'])
        self.assertEqual(result['status'],'rpc-accepted');self.assertEqual(self.api.session,before)
        self.assertEqual(self.api.sent,[]);self.assertEqual(self.deletes,[]);self.assertIsNone(self.old['invokedAt'])
        for t,rows in tables.items():self.assertEqual(self.s.rows('SELECT * FROM '+t),rows)
        self.assertFalse(result['resent']);self.assertFalse(result['aborted']);self.assertFalse(result['resumed'])

    def test_only_actual_receipt_proves_intake_and_repeated_request_never_steers_again(self):
        self.receipt=True
        result=self.s.dispatch(self.request)
        self.assertTrue(result['acknowledged']);self.assertEqual(result['receipt']['localId'],'queued-id')
        self.assertEqual(self.s.dispatch(self.request)['status'],'invoked');self.assertEqual(len(self.calls),1)

    def test_pending_receipt_and_timeout_never_repeat_promotion(self):
        result=self.s.dispatch(self.request)
        self.assertTrue(result['rpcAccepted']);self.assertFalse(result['acknowledged'])
        self.s.dispatch(self.request);self.assertEqual(len(self.calls),1)
        # Separate exact item audit, simulating a new reviewed message.
        self.old['id']='other';self.request['message']='other';self.response=TimeoutError()
        self.assertEqual(self.s.dispatch(self.request)['status'],'uncertain')
        self.s.dispatch(self.request);self.assertEqual(len(self.calls),2)

    def test_mismatched_native_ack_is_unknown_not_consumed(self):
        self.response={'status':'steered','localId':'unrelated'}
        r=self.s.dispatch(self.request);self.assertFalse(r['rpcAccepted']);self.assertFalse(r['acknowledged'])
        self.assertEqual(r['status'],'uncertain')

    def test_manual_pause_project_hold_release_and_legacy_stop_fallback_block(self):
        self.api.session['manualPausedAt']=123
        with self.assertRaisesRegex(ValueError,'paused'):self.s.dispatch(self.request)
        self.api.session['manualPausedAt']=None
        self.s.dispatch({'op':'pause','project':self.project})
        with self.assertRaisesRegex(ValueError,'paused'):self.s.dispatch(self.request)
        self.s.dispatch({'op':'resume','project':self.project})
        self.s.db.execute("UPDATE workers SET state='released' WHERE id='w'")
        with self.assertRaisesRegex(ValueError,'managed'):self.s.dispatch(self.request)
        self.s.db.execute("UPDATE workers SET state='managed' WHERE id='w'")
        self.api.session['metadata']['flavor']='claude'
        with self.assertRaisesRegex(ValueError,'Stop is forbidden'):self.s.dispatch(self.request)
        self.assertEqual(self.calls,[])

    def test_controls_schedules_edits_unknown_input_and_stale_permissions_block(self):
        for change in ({'user_requested':False},{'requested_at':time.time()-301},{'input_hash':'edited'},{'local_id':'other'}):
            with self.assertRaises(ValueError):self.s.dispatch({**self.request,**change})
        self.old['content']['content']['text']='/goal resume';self.request['input_hash']=input_hash(self.old)
        with self.assertRaises(ValueError):self.s.dispatch(self.request)
        self.old['content']['content']['text']='Valid actual input';self.old['scheduledAt']=time.time()*1000;self.request['input_hash']=input_hash(self.old)
        with self.assertRaises(ValueError):self.s.dispatch(self.request)
        self.old.pop('scheduledAt');self.proof['indeterminateLocalIds']=['queued-id']
        with self.assertRaises(ValueError):self.s.dispatch(self.request)
        self.assertEqual(self.calls,[])

    def test_false_receipts_are_never_accepted(self):
        self.proof={'queuedLocalIds':[],'indeterminateLocalIds':[],'invokedLocalMessages':[{'localId':'queued-id','invokedAt':None}]}
        with self.assertRaises(ValueError):self.s.dispatch(self.request)
        self.assertEqual(self.calls,[])

if __name__=='__main__':unittest.main()


class NativeQueueTests(IsolatedSupervisorTest):
    setUp=InputTests.setUp
    tearDown=InputTests.tearDown
    def test_native_listing_is_bound_read_only_and_detached_without_runtime_mutations(self):
        from subprocess import CompletedProcess
        value={'session':'s','native_id':'native','readOnly':True,'items':[]}
        before=self.api.session.copy()
        with patch('queued_input.subprocess.run',return_value=CompletedProcess([],0,json.dumps(value),'')) as run:
            result=self.s.dispatch({'op':'native_queue','worker':'w'})
        self.assertEqual(result['items'],[]);self.assertEqual(self.api.session,before);self.assertEqual(self.api.sent,[])
        args,kwargs=run.call_args
        self.assertEqual(args[0][-2:],['s','native']);self.assertTrue(args[0][1].endswith('deploy/read-native-queue.ts'))
        self.assertTrue(kwargs['check']);self.assertEqual(kwargs['timeout'],60)

    def test_native_listing_rejects_wrong_id_mutation_flag_malformed_or_failed_response(self):
        import subprocess
        value={'session':'s','native_id':'native','readOnly':True,'items':[]}
        for invalid in ({**value,'native_id':'other'},{**value,'readOnly':False},[],{**value,'items':None}):
            with patch('queued_input.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(invalid),'')):
                with self.assertRaises(ValueError):self.s.dispatch({'op':'native_queue','worker':'w'})
        with patch('queued_input.subprocess.run',side_effect=subprocess.TimeoutExpired([],60)):
            with self.assertRaises(ValueError):self.s.dispatch({'op':'native_queue','worker':'w'})
        self.assertEqual(self.api.sent,[])
