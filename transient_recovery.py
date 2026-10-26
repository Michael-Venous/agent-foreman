"""Durable, active-controller-only recovery. Never restart, change goal/model or replay input."""
import hashlib,json,logging,math,re,time,uuid
log=logging.getLogger('manager')


def timing(d):
 error=d.get('error') if isinstance(d.get('error'),dict) else {}
 info=d.get('rate_limit_info') if isinstance(d.get('rate_limit_info'),dict) else {}
 reset=d.get('resetsAt') or error.get('resetsAt') or info.get('resetsAt')
 retry=d.get('retryAfterSeconds',error.get('retryAfterSeconds'))
 reset=float(reset) if isinstance(reset,(int,float)) and not isinstance(reset,bool) and math.isfinite(reset) else None
 if reset and reset>100000000000:reset/=1000
 retry=float(retry) if isinstance(retry,(int,float)) and not isinstance(retry,bool) and math.isfinite(retry) and 0<=retry<=21600 else None
 return {'reset':reset,'retryAfter':retry}

def classify(message,native=None):
 from supervisor import content
 c=content(message);inner=c.get('content') or {};d=inner.get('data') or {} if isinstance(inner,dict) else {}
 if c.get('role')!='agent' or not isinstance(d,dict):return None
 if d.get('isSidechain') or d.get('parent_tool_use_id') or d.get('agentId') or d.get('agent_id') or d.get('scope_role')=='child' or d.get('scopeRole')=='child' or (isinstance(d.get('scope'),dict) and d['scope'].get('role')=='child'):return None
 if d.get('type')=='thread_goal_updated' and isinstance(d.get('goal'),dict) and d['goal'].get('status')=='usageLimited':
  if native and d['goal'].get('threadId') not in (None,native):return None
  return {'kind':'quota','text':'Native goal usage limit','episode':'quota:'+str(d['goal'].get('updatedAt') or message['id']),**timing(d)}
 identity=str(d.get('id') or message.get('localId') or '')
 shared=inner.get('type')=='codex' and d.get('type')=='message' and identity.startswith('codex:') and identity.endswith(':task_failed')
 if not shared and d.get('type') not in ('error','task_failed'):return None
 if d.get('willRetry') or d.get('will_retry') or d.get('retryable'):return None
 if shared:
  parts=identity.split(':')
  if len(parts)<5 or (native and parts[1]!=native):return None
  turn=parts[2]
 else:
  thread=d.get('thread_id') or d.get('threadId')
  if native and thread and thread!=native:return None
  turn=d.get('turn_id') or d.get('turnId')
 error=d.get('error');text=str(d.get('message') or (error.get('message') if isinstance(error,dict) else error) or '')
 code=str(d.get('codex_error_info') or (error.get('code') if isinstance(error,dict) else '') or '')
 if re.search(r'usage.?limit|quota|weekly.limit|daily.limit|token.?budget|insufficient.quota|hit your.*limit|resets? (?:at|in)',text+' '+code,re.I):return {'kind':'quota','text':text[:500],'episode':str(turn or message['id']),**timing(d)}
 kind='rate' if re.search(r'\b429\b|too many requests|rate.?limit',text+' '+code,re.I) else 'capacity' if re.search(r'at capacity|overload|server.?busy|temporarily unavailable|\b50[23]\b',text+' '+code,re.I) else None
 if not kind:return None
 # The error and turn-completed projection can have distinct message ids but one invocation.
 episode=str(turn or ('unscoped:'+hashlib.sha256((kind+text).encode()).hexdigest()))
 return {'kind':kind,'text':text[:500],'episode':episode,**timing(d)}


STREAK_WINDOW=7200   # failures this close together are one outage; manual messages do not reset the backoff
ESCALATE_EVERY=4     # quiet retries: report the first failure, then every 4th, never each one


def provider(session):
 return (session.get('metadata') or {}).get('flavor') or 'unknown'


def streak(s,sid):
 v=json.loads(s.get('transient-streak:'+sid) or '{}')
 return int(v.get('n',0)) if time.time()-v.get('at',0)<=STREAK_WINDOW else 0


def bump_streak(s,sid):
 n=streak(s,sid)+1;s.set('transient-streak:'+sid,json.dumps({'n':n,'at':time.time()}));return n


def provider_until(s,flavor):
 return float(s.get('transient-provider:'+flavor) or 0)


def bump_provider(s,flavor):
 """Shared outage window: one provider's rejections space out every chat's retries, not just this chat's."""
 key='transient-provider-n:'+flavor;v=json.loads(s.get(key) or '{}')
 n=int(v.get('n',0))+1 if time.time()-v.get('at',0)<=3600 else 1
 s.set(key,json.dumps({'n':n,'at':time.time()}))
 until=time.time()+min(1800,120*2**min(n-1,4));s.set('transient-provider:'+flavor,str(until));return until


def recovered(s,sid,session):
 """Real model output after a failure ends the outage for this chat and its provider."""
 for key in ('transient-streak:'+sid,'transient-provider-n:'+provider(session),'transient-provider:'+provider(session)):
  s.db.execute('DELETE FROM settings WHERE key=?',(key,))


def install(s):
 s.db.execute('''CREATE TABLE IF NOT EXISTS transient_recovery(session TEXT PRIMARY KEY,native TEXT NOT NULL,worker TEXT,episode TEXT,source TEXT,failed_at REAL,due REAL,attempts INTEGER DEFAULT 0,state TEXT,local_id TEXT,kind TEXT)''')


def settle(s,sid,state):
 s.db.execute('UPDATE transient_recovery SET state=? WHERE session=?',(state,sid))
 # Retire only undelivered retry bookkeeping, never a worker completion/input event.
 s.db.execute("UPDATE events SET acked=1 WHERE event_key LIKE ? AND kind='transient-wait' AND wake_id IS NULL",('transient:'+sid+':%',))


def input_is_new(s,m,after,own=None):
 from supervisor import content
 if content(m).get('role')!='user' or m.get('createdAt',0)/1000<=after:return False
 local=m.get('localId')
 if local==own:return False
 if local and s.rows('SELECT id FROM outbox WHERE id=?',(local,)):return False
 if local and s.rows('SELECT session FROM transient_recovery WHERE local_id=?',(local,)):return False
 return True


def observe(s,sid,session,messages,worker=None):
 from supervisor import native,content
 nid=native(session)
 if not nid:return
 if session.get('manualPausedAt') is not None:
  settle(s,sid,'cancelled');return
 row=s.rows('SELECT * FROM transient_recovery WHERE session=?',(sid,));old=row[0] if row else None
 failures=[]
 for m in messages:
  found=classify(m,nid)
  if found and (found['kind']!='quota' or worker is None or sid==s.get('manager')):failures.append((m.get('createdAt',0)/1000,m,found))
 if failures:
  at,m,f=max(failures,key=lambda item:(item[0],item[1].get('seq',0)))
  # Historical import/reconnect is not permission to revive old work.
  if (old or time.time()-at<=600) and not any(input_is_new(s,x,at,old and old['local_id']) for x in messages):
   if not old or old['native']!=nid or old['episode']!=f['episode'] or (old['state']=='done' and at>old['failed_at']):
    if old and old['native']!=nid:return # identity changes require human reconciliation
    attempts=old['attempts'] if old and old['state'] not in ('done','cancelled') else 0
    # A chat or manager message cancels a retry but does not end the outage: keep the backoff growing.
    failures=bump_streak(s,sid);attempts=max(attempts,failures-1)
    base=min(1800,60*2**min(attempts,5));jitter=int(hashlib.sha256((sid+f['episode']).encode()).hexdigest()[:8],16)%max(1,base//5)
    due=max(time.time()+60,f['reset']+60) if f.get('reset') is not None else None if f['kind']=='quota' else time.time()+max(base,float(f.get('retryAfter') or 0))+jitter
    if due is not None and f['kind']!='quota':due=max(due,bump_provider(s,provider(session)))
    s.db.execute('INSERT OR REPLACE INTO transient_recovery VALUES(?,?,?,?,?,?,?,?,?,?,?)',(sid,nid,worker and worker['id'],f['episode'],m['id'],at,due,attempts,'quota-wait' if due is None else 'waiting',None,f['kind']))
    key='transient:'+sid+':'+f['episode']
    s.event(key,worker and worker['project'],worker and worker['id'],'transient-wait',json.dumps({'session':sid,'kind':f['kind'],'needsHumanInput':due is None,'retryAt':due,'consecutiveFailures':failures,'reason':f['text'],'policy':'One checkpoint continuation, same active native conversation. No restart/goal/model change. Unknown intake is never replayed.'}))
    # Expected outage retries are bookkeeping: report the first failure, a human-needed quota wait, and every
    # ESCALATE_EVERY-th repeat. The rest stay in the audit table without waking the owner.
    if due is not None and failures>1 and failures%ESCALATE_EVERY:s.db.execute('UPDATE events SET acked=1 WHERE event_key=?',(key,))
    old=s.one('SELECT * FROM transient_recovery WHERE session=?',(sid,))
 if not old:return
 if old['native']!=nid:return
 if any(input_is_new(s,m,old['failed_at'],old['local_id']) for m in messages):
  settle(s,sid,'cancelled');return
 # Actual model output/tool activity after the failure is recovery evidence, not task completion.
 for m in messages:
  if m.get('createdAt',0)/1000<=old['failed_at']:continue
  c=content(m);d=(c.get('content') or {}).get('data') or {}
  if c.get('role')!='agent' or not isinstance(d,dict) or d.get('scope_role')=='child' or d.get('isSidechain'):continue
  ident=str(d.get('id') or '')
  if (ident.startswith('codex:'+nid+':') and ident.endswith((':agent_message',':exec_command_begin',':mcp_tool_call_begin'))) and (d.get('message') or d.get('type')=='tool-call'):
   settle(s,sid,'done');s.db.execute('UPDATE transient_recovery SET attempts=0 WHERE session=?',(sid,));recovered(s,sid,session);return


def blocked(s,sid):
 return bool(s.rows("SELECT session FROM transient_recovery WHERE session=? AND state NOT IN ('done','cancelled')",(sid,)))


def observe_owner(s,sid,session):
 if (session.get('metadata') or {}).get('flavor')!='codex':return
 signature=[(session.get('metadata') or {}).get('codexSessionId'),session.get('thinking'),session.get('manualPausedAt'),session.get('lastFailureAt')]
 key='transient-owner-probe:'+sid;previous=json.loads(s.get(key) or '{}')
 if previous.get('signature')==signature and time.time()-previous.get('at',0)<30:return
 messages=s.api.call('/sessions/'+sid+'/messages?limit=100')['messages']
 s.set(key,json.dumps({'signature':signature,'at':time.time()}))
 w=s.rows('SELECT * FROM workers WHERE session=?',(sid,))
 observe(s,sid,session,messages,w[0] if w else None)


def recover(s):
 from supervisor import native
 if s.manager_paused():return
 # Overseer first; a single global dispatch slot avoids synchronized model retries.
 rows=s.rows("SELECT * FROM transient_recovery WHERE state NOT IN ('done','cancelled') ORDER BY due")
 manager=s.get('manager');rows.sort(key=lambda r:r['session']!=manager)
 for r in rows:
  try:
   if recover_one(s,r,manager):break
  except Exception as error:log.warning('transient recovery %s: %s',r['session'],error)


def recover_one(s,r,manager):
 from supervisor import native
 sid=r['session'];session=s.session(sid)
 if session.get('manualPausedAt') is not None:
  settle(s,sid,'cancelled');return False
 if native(session)!=r['native']:return False
 if not session.get('active'):return False # never launch/restart a controller
 if r['worker']:
  w=s.worker(r['worker']);p=s.project(w['project'])
  if w['session']!=sid or w['native_id']!=r['native'] or w['state']!='managed' or p['status']!='running' or p['manual'] or s.watch(w['id'])['mode']=='off':return False
  if s.rows("SELECT worker FROM usage_recovery WHERE worker=? AND state NOT IN ('done','cancelled')",(w['id'],)):return False
  owner=s.get('owner:'+w['id']) or manager
  if owner and owner!=sid and s.session(owner).get('manualPausedAt') is not None:return False
  if s.get('unmanaged:'+w['id']) or s.get('resume-pending:'+w['id']) or (sid!=manager and s.checkout_busy(w)):return False
 if r['state']=='quota-wait':return False
 if r['state']=='waiting' and time.time()<provider_until(s,provider(session)):return False # shared provider outage window
 if r['state']!='waiting':
  q=s.api.call('/sessions/'+sid+'/messages/queued-state',{'localIds':[r['local_id']]})
  if any(i['localId']==r['local_id'] for i in q.get('invokedLocalMessages',[])):
   s.db.execute("UPDATE transient_recovery SET state='awaiting-outcome' WHERE session=?",(sid,))
  return False # including uncertain or absent: never allocate another localId
 if time.time()<r['due'] or time.time()<float(s.get('transient-next-dispatch') or 0):return False
 agent=session.get('agentState') or {}
 if session.get('thinking') or session.get('backgroundTaskCount') or agent.get('requests') or agent.get('controlledByUser') or agent.get('claudeReadyMessages'):return False
 messages=s.api.call('/sessions/'+sid+'/messages?limit=100')['messages']
 if any(input_is_new(s,m,r['failed_at'],r['local_id']) for m in messages):
  settle(s,sid,'cancelled');return False
 # No continuation may jump ahead of any unresolved input, including supervisor wakes.
 pending=[m.get('localId') for m in messages if __import__('supervisor').content(m).get('role')=='user' and m.get('localId') and m.get('invokedAt') is None]
 if pending:
  q=s.api.call('/sessions/'+sid+'/messages/queued-state',{'localIds':pending})
  consumed={i['localId'] for i in q.get('invokedLocalMessages',[])}
  if any(i not in consumed for i in pending):return False
 fresh=s.session(sid)
 if not fresh.get('active') or fresh.get('manualPausedAt') is not None or fresh.get('thinking') or native(fresh)!=r['native'] or (fresh.get('metadata') or {}).get('hostPid')!=(session.get('metadata') or {}).get('hostPid'):return False
 local=str(uuid.uuid4())
 text='Provider recovery: continue the interrupted request from its latest checkpoint in this conversation. Check completed work and the latest user scope first; do not replay completed actions. Preserve all pauses, goals and resource allocations. If completed, cancelled or awaiting human input, stop and report that. '+('Handle any unacknowledged supervisor events already in context; do not resend worker assignments merely because a wake failed.' if sid==manager else '')
 s.db.execute('BEGIN IMMEDIATE')
 try:
  s.db.execute("UPDATE transient_recovery SET state='sending',local_id=?,attempts=attempts+1 WHERE session=?",(local,sid))
  s.db.execute('INSERT INTO outbox VALUES(?,?,?,?,?)',(local,sid,text,'sending',time.time()))
  s.set('transient-next-dispatch',time.time()+30)
  s.db.execute('COMMIT')
 except Exception:s.db.execute('ROLLBACK');raise
 try:s.api.call('/sessions/'+sid+'/messages',{'text':text,'localId':local})
 except Exception:
  s.db.execute("UPDATE transient_recovery SET state='uncertain' WHERE session=?",(sid,));s.db.execute("UPDATE outbox SET state='uncertain' WHERE id=?",(local,));raise
 s.db.execute("UPDATE transient_recovery SET state='sent' WHERE session=?",(sid,));s.db.execute("UPDATE outbox SET state='sent' WHERE id=?",(local,))
 return True
