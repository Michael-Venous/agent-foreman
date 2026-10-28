#!/usr/bin/env python3
"""Single-manager HAPI supervisor. SQLite transactions precede all side effects."""
import re,json,sqlite3,time,uuid,threading,urllib.request,subprocess,os,fcntl,logging,hashlib,math
from pathlib import Path
from hapi_api import Hapi,BASE
from coordination import is_instruction_message
ROOT=Path(__file__).resolve().parent
STATE=ROOT/'state'; CONTROL=ROOT/'manager/control'
log=logging.getLogger('manager')
DELIVERED=json.dumps({'delivered':True,'note':'Result already returned to its caller; not stored.'})
LOST_AFTER=300
LONG_INSTRUCTION=1200
def uid():return str(uuid.uuid4())
def native(s):
 m=s.get('metadata') or {}
 return m.get('codexSessionId') or m.get('claudeSessionId') or m.get('sessionId')
def live_controller(s):
 pid=(s.get('metadata') or {}).get('hostPid')
 if not isinstance(pid,int):return False
 try:
  cmd=(Path('/proc')/str(pid)/'cmdline').read_bytes()
  return b'hapi' in cmd
 except (FileNotFoundError,PermissionError,ProcessLookupError):return False
def clip(value,limit=2000):
 # Native records can embed whole files/images; keep structure, shorten long strings.
 if isinstance(value,str):return value if len(value)<=limit else value[:limit]+'…[%d chars clipped]'%(len(value)-limit)
 if isinstance(value,list):return [clip(v,limit) for v in value]
 if isinstance(value,dict):return {k:clip(v,limit) for k,v in value.items()}
 return value
def request_questions(request):
 """Questions of a pending AskUserQuestion / request_user_input, with options."""
 args=request.get('arguments') or {}
 out=[]
 for i,q in enumerate(args.get('questions') or []):
  if not isinstance(q,dict):continue
  options=[o.get('label') if isinstance(o,dict) else str(o) for o in (q.get('options') or [])]
  out.append({'key':q.get('id') or str(i),'question':q.get('question') or q.get('header') or '','options':options,'multi':bool(q.get('multiSelect'))})
 return out
def summarize_request(rid,request):
 tool=request.get('tool') or 'tool';qs=request_questions(request)
 if qs:return 'Question %s (%s): %s'%(rid,tool,' | '.join(q['question']+(' ['+'/'.join(q['options'])+']' if q['options'] else '') for q in qs))[:400]
 args=request.get('arguments') or {}
 what=args.get('command') or args.get('file_path') or args.get('path') or json.dumps(args)[:120]
 return ('Approval %s: %s %s'%(rid,tool,what))[:300]
def content(m):
 c=m.get('content',{})
 if c.get('type')=='plaintext':c=c.get('data',{})
 return c if isinstance(c,dict) else {}
class Supervisor:
 def __init__(self,db=None,api=None):
  STATE.mkdir(mode=0o700,exist_ok=True);CONTROL.mkdir(mode=0o700,exist_ok=True)
  self.db=sqlite3.connect(db or STATE/'manager.sqlite',check_same_thread=False,isolation_level=None)
  self.db.row_factory=sqlite3.Row;self.lock=threading.RLock();self.api=api or Hapi();self.wake=threading.Event()
  self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT,repo TEXT,objective TEXT,next_steps TEXT DEFAULT '',blockers TEXT DEFAULT '',checkpoint TEXT DEFAULT '',status TEXT DEFAULT 'paused',manual INTEGER DEFAULT 0,integration_owner TEXT DEFAULT '',budget INTEGER DEFAULT 8);
CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,project TEXT,runtime TEXT,native_id TEXT,session TEXT,path TEXT,state TEXT,created REAL);
CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY,worker TEXT,task_key TEXT UNIQUE,text TEXT,state TEXT,created REAL,seen_busy INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE,project TEXT,worker TEXT,kind TEXT,detail TEXT,acked INTEGER DEFAULT 0,wake_id TEXT);
CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS resources(name TEXT PRIMARY KEY,project TEXT);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,session TEXT,text TEXT,state TEXT,created REAL);
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,result TEXT);
CREATE TABLE IF NOT EXISTS usage_recovery(worker TEXT PRIMARY KEY,source TEXT,due REAL,attempts INTEGER DEFAULT 0,state TEXT,local_id TEXT,session TEXT);
CREATE TABLE IF NOT EXISTS reminders(id TEXT PRIMARY KEY,due REAL,every REAL,worker TEXT,project TEXT,note TEXT,active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS reminder_deliveries(reminder_id TEXT NOT NULL,due REAL NOT NULL,status TEXT NOT NULL,event_key TEXT,updated_at REAL NOT NULL,next_attempt_at REAL,error TEXT,reason TEXT,PRIMARY KEY(reminder_id,due));
''')
  import transient_recovery
  transient_recovery.install(self)
  # 'checkout' (default) holds the worker's directory; 'external' declares no checkout access.
  for column in ("scope TEXT DEFAULT 'checkout'","goal INTEGER DEFAULT 0"):
   try:self.db.execute('ALTER TABLE assignments ADD COLUMN '+column)
   except sqlite3.OperationalError as e:
    if 'duplicate column' not in str(e):raise
  self.db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('reporting-default',json.dumps({'enabled':True,'fallback_minutes':30,'important':True})))
  pending={p.stem for p in CONTROL.glob('*.req')}
  for r in self.rows('SELECT id FROM requests WHERE length(result)>4096'):
   if r['id'] not in pending:self.db.execute('UPDATE requests SET result=? WHERE id=?',(DELIVERED,r['id']))
 def rows(self,q,args=()):return [dict(r) for r in self.db.execute(q,args)]
 def one(self,q,args=()):
  rows=self.rows(q,args)
  if not rows:raise ValueError('Record not found')
  return rows[0]
 def get(self,k):
  r=self.db.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone();return r[0] if r else None
 def set(self,k,v):self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,str(v)))
 def event(self,key,p,w,kind,detail):self.db.execute('INSERT OR IGNORE INTO events(event_key,project,worker,kind,detail) VALUES(?,?,?,?,?)',(key,p,w,kind,str(detail)))
 def finish(self,a,w,state,detail):
  self.db.execute('BEGIN IMMEDIATE')
  try:
   self.db.execute('UPDATE assignments SET state=? WHERE id=?',(state,a['id']))
   self.event('result:'+a['id'],w['project'],w['id'],state,detail)
   self.db.execute('COMMIT')
  except Exception:self.db.execute('ROLLBACK');raise
 def project(self,p):return self.one('SELECT * FROM projects WHERE id=?',(p,))
 def worker(self,w):return self.one('SELECT * FROM workers WHERE id=?',(w,))
 def workspace(self,w):return str(Path(self.get('workspace:'+w['id']) or w['path']).resolve())
 def checkout_busy(self,w):
  path=Path(self.workspace(w))
  others=self.rows("SELECT DISTINCT other.* FROM workers other JOIN assignments a ON a.worker=other.id WHERE other.id!=? AND a.state IN ('sent','sending','uncertain') AND COALESCE(a.scope,'checkout')='checkout'",(w['id'],))
  return any(path==other or path in other.parents or other in path.parents for other in (Path(self.workspace(x)) for x in others))
 def permission_mode(self,runtime,profile):
  if profile not in ('full','ask'):raise ValueError('permission must be full or ask')
  return ('bypassPermissions' if runtime=='claude' else 'yolo') if profile=='full' else 'default'
 def worker_permission(self,w):return self.get('worker_permission:'+w['id']) or 'full'
 def running(self,p):
  x=self.project(p)
  if x['status']!='running' or x['manual']:raise ValueError('Project is paused/completed or held for direct user intervention')
  return x
 def discover(self,runtime):
  out=[]
  if runtime=='codex':
   p=Path.home()/'.codex/state_5.sqlite'
   c=sqlite3.connect(f'file:{p}?mode=ro',uri=True);c.row_factory=sqlite3.Row
   out=[dict(r) for r in c.execute('SELECT id,cwd,title,updated_at FROM threads ORDER BY updated_at DESC LIMIT 100')];c.close()
  elif runtime=='claude':
   for p in (Path.home()/'.claude/projects').glob('*/*.jsonl'):
    try:
     with p.open() as f:
      for line in f:
       d=json.loads(line)
       if d.get('cwd'):
        out.append({'id':d.get('sessionId',p.stem),'cwd':d['cwd'],'updated_at':p.stat().st_mtime});break
    except (ValueError,OSError):pass
   out=sorted(out,key=lambda x:x['updated_at'],reverse=True)[:100]
  else:raise ValueError('runtime must be codex or claude')
  return out
 def session(self,s):return self.api.call('/sessions/'+s)['session']
 def resume_workspace_witness(self,w,pending,resumed,pid):
  """Confirm an expected assigned-workspace launch, never an arbitrary cwd change."""
  import socket
  meta=resumed.get('metadata') or {};expected=pending.get('workspace_path')
  # Legacy resume requests captured the stale registered launch directory only.
  # Permit their already-configured checkout only when that is exactly the old
  # worker.path. All other mismatches remain uncertain for explicit inspection.
  if not expected:
   if pending.get('launch_path')!=w['path']:return None
   expected=self.workspace(w)
  if expected!=self.workspace(w) or meta.get('host')!=socket.gethostname():return None
  if pending.get('source_machine') and meta.get('machineId')!=pending['source_machine']:return None
  if w['runtime']!='codex' or type(pid) is not int or pid<=0:return None
  try:
   proc=Path('/proc')/str(pid);before=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
   if proc.stat().st_uid!=os.getuid():return None
   actual=(proc/'cwd').resolve(strict=True);wanted=Path(expected).resolve(strict=True)
   if actual!=wanted or Path(meta.get('path') or '').resolve(strict=True)!=wanted:return None
   if pending['native'] not in (proc/'cmdline').read_bytes().decode(errors='replace').split('\0'):return None
   after=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
   if before!=after:return None
   return {'path':str(actual),'pid':pid,'startTicks':after,'host':meta['host'],'native':pending['native'],'basis':'Configured workspace + exact native CLI argument + same-user local controller cwd/start identity'}
  except (OSError,ValueError,IndexError):return None

 def reconcile_authorized_resume(self,w):
  key='resume-pending:'+w['id'];pending=json.loads(self.get(key) or 'null')
  if not pending:return True
  if w['state']!='managed' or self.get('unmanaged:'+w['id']):return False
  if not pending.get('native') or (w['native_id'] and w['native_id']!=pending['native']):return False
  target=pending.get('target')
  # The assigned checkout is not the native launch cwd. Find duplicate native
  # controllers across all paths before confirming the exact resume target.
  active=[r['id'] for r in self.api.call('/sessions')['sessions'] if r.get('active')]
  candidates=list(dict.fromkeys(([target] if target else [])+active))
  matches=[]
  for sid in candidates:
   session=self.session(sid)
   if session.get('active') and native(session)==pending['native']:
    if session.get('manualPausedAt') is not None:return False
    matches.append(session)
  if len(matches)!=1:return False
  resumed=matches[0];sid=resumed['id'];pid=(resumed.get('metadata') or {}).get('hostPid')
  if target and sid!=target:return False
  if type(pid) is not int or pid<=0:return False
  witness=None
  if pending.get('launch_path') and (resumed.get('metadata') or {}).get('path')!=pending['launch_path']:
   witness=self.resume_workspace_witness(w,pending,resumed,pid)
   if not witness:return False
   fresh=self.session(sid);metadata=fresh.get('metadata') or {}
   if not fresh.get('active') or fresh.get('manualPausedAt') is not None or native(fresh)!=pending['native'] or metadata.get('hostPid')!=pid or metadata.get('path')!=(resumed.get('metadata') or {}).get('path'):return False
  self.db.execute('BEGIN IMMEDIATE')
  try:
   self.db.execute('UPDATE workers SET session=? WHERE id=?',(sid,w['id']))
   self.set('host:'+w['id'],pid)
   if self.get('manager')==pending['source']:self.set('manager',sid)
   self.set('resume-authorization:'+pending['source'],json.dumps({**pending,'state':'reconciled','target':sid,**({'workspaceWitness':witness} if witness else {})}))
   self.db.execute('DELETE FROM settings WHERE key=?',(key,))
   self.db.execute('COMMIT')
  except Exception:self.db.execute('ROLLBACK');raise
  return True
 def scan(self,w,retire=False):
  if self.get('unmanaged:'+w['id']):return
  if not w['session']:return
  if self.get('resume-pending:'+w['id']):
   if not self.reconcile_authorized_resume(w):return
   w=self.worker(w['id'])
  s=self.session(w['session']);msgs=[];query='?limit=100'
  for _ in range(100):
   page=self.api.call('/sessions/'+w['session']+'/messages'+query);batch=page['messages'];msgs.extend(batch)
   if not batch or any(self.db.execute('SELECT 1 FROM seen WHERE id=?',(m['id'],)).fetchone() for m in batch):break
   cursor=page.get('page',{})
   if not cursor.get('hasMore') or cursor.get('nextBeforeSeq') is None:break
   query='?limit=100&beforeSeq='+str(cursor['nextBeforeSeq'])+'&beforeAt='+str(cursor['nextBeforeAt'])
  if self.user_restarted(w,s):return s
  history=set(json.loads(self.get('native_history:'+w['id']) or '[]'))
  n=native(s)
  if n and w['native_id'] and n!=w['native_id']:
   self.db.execute("UPDATE projects SET status='paused',blockers=? WHERE id=?",('Native session identity mismatch; inspect controller before further work',w['project']))
   self.event('mismatch:'+w['id']+':'+n,w['project'],w['id'],'identity-mismatch','HAPI session reports native '+n+' instead of registered '+w['native_id']+'; project paused. See reconcile_adopt.')
   raise ValueError('Native session identity mismatch; refusing to replace registered history ID')
  if n:self.db.execute('UPDATE workers SET native_id=? WHERE id=?',(n,w['id']))
  for m in sorted(msgs,key=lambda m:(m.get('createdAt',0),m.get('seq',0))):
   mid=m['id']
   if self.db.execute('SELECT 1 FROM seen WHERE id=?',(mid,)).fetchone():continue
   c=content(m);local=m.get('localId');role=c.get('role');inner=c.get('content',{})
   own=bool(local and self.db.execute('SELECT 1 FROM assignments WHERE id=?',(local,)).fetchone())
   # Old history is imported read-only, not treated as a new intervention. Codex
   # import stamps replayed prompts with import time; pre-adoption client IDs prove history.
   recent=m.get('createdAt',0)/1000 >= w['created'] and local not in history
   recovery_own=bool(local and (self.db.execute('SELECT 1 FROM usage_recovery WHERE local_id=?',(local,)).fetchone() or self.db.execute('SELECT 1 FROM transient_recovery WHERE local_id=?',(local,)).fetchone()))
   if recent and role=='agent':self.observe_usage_limit(w,m)
   if is_instruction_message(m) and not own and not recovery_own and recent:
    self.db.execute("UPDATE usage_recovery SET state='cancelled' WHERE worker=?",(w['id'],))
    # The user may steer or set goals in manager chats; monitoring continues.
    # One note per gap between manager assignments keeps the manager informed, not flooded.
    last=self.rows('SELECT id FROM assignments WHERE worker=? ORDER BY created DESC LIMIT 1',(w['id'],))
    said=(inner.get('text') if isinstance(inner,dict) else '') or ''
    from assignment_delivery import describe_message
    delivery=describe_message(m,include_wait=False)
    self.event('steer:'+w['id']+':'+(last[0]['id'] if last else 'start'),w['project'],w['id'],'user-input',
     json.dumps({'message':mid,'delivery':delivery,'note':'Direct user input; queue/control consumption is not active-turn steering. Inspect the exact input before assigning more.'}))
   waiting=bool(self.rows("SELECT worker FROM usage_recovery WHERE worker=? AND state NOT IN ('done','cancelled')",(w['id'],)))
   if not waiting and isinstance(inner,dict) and inner.get('type')=='event':
    ev=inner.get('data',{}).get('type')
    if ev in ('ready','error','abort-restore') and recent:
     for a in self.rows("SELECT * FROM assignments WHERE worker=? AND state IN ('sent','sending','uncertain') AND created<=?",(w['id'],m.get('createdAt',0)/1000)):
      # Require assignment consumption before accepting a ready event.
      q=self.api.call('/sessions/'+w['session']+'/messages/queued-state',{'localIds':[a['id']]})
      if any(i['localId']==a['id'] for i in q.get('invokedLocalMessages',[])):
       if a['goal'] and ev=='ready':continue  # a goal keeps running turns until it completes
       state='finished' if ev=='ready' else 'failed'
       self.finish(a,w,state,'Turn ended; inspect results before follow-up or objective completion.')
   self.db.execute('INSERT OR IGNORE INTO seen VALUES(?)',(mid,))
  import transient_recovery
  transient_recovery.observe(self,w['session'],s,msgs,w)
  active=self.rows("SELECT * FROM assignments WHERE worker=? AND state IN ('sent','sending','uncertain')",(w['id'],))
  for a in active:
   if self.rows("SELECT worker FROM usage_recovery WHERE worker=? AND state NOT IN ('done','cancelled')",(w['id'],)):continue
   # Ready events are advisory/droppable. Recover terminal outcomes from structured
   # runtime records after proven prompt consumption, never from idle alone.
   if not s.get('thinking'):
    q=self.api.call('/sessions/'+w['session']+'/messages/queued-state',{'localIds':[a['id']]})
    invoked=next((x.get('invokedAt',0)/1000 for x in q.get('invokedLocalMessages',[]) if x['localId']==a['id']),None)
    if not invoked and self.assignment_superseded(w,a,q):continue
    outcome=None;detail='Terminal runtime record reconciled; inspect evidence before following up.'
    if invoked and a['goal']:
     goal=self.goal_state(w,max(invoked,a['created']))
     if goal in ('complete','completed','cleared'):
      outcome='finished';detail='Goal '+goal+'; inspect evidence before following up or completing the objective.'
    elif invoked:
     if w['runtime']=='codex' and w['native_id']:
      try:
       from datetime import datetime
       tail=self.dispatch({'op':'read_native','worker':w['id']})['tail']
       for record in tail:
        kind=record.get('payload',{}).get('type')
        if record.get('type')=='event_msg' and kind in ('task_complete','task_failed','turn_aborted') and datetime.fromisoformat(record['timestamp'].replace('Z','+00:00')).timestamp()>=max(invoked,a['created']):
         outcome='finished' if kind=='task_complete' else 'failed'
      except (ValueError,OSError,KeyError,TypeError):pass
     elif w['runtime']=='claude':
      for message in msgs:
       data=content(message).get('content',{}).get('data',{})
       if message.get('createdAt',0)/1000>=max(invoked,a['created']) and data.get('type')=='system' and data.get('subtype')=='turn_duration':outcome='finished'
    if outcome:
     self.finish(a,w,outcome,detail)
   if s.get('thinking'):self.db.execute('UPDATE assignments SET seen_busy=1 WHERE id=?',(a['id'],))
   if not s.get('active'):
    self.event('disconnect:'+a['id'],w['project'],w['id'],'disconnected','Transport disconnected. Reconcile before retry; assignment not resent.')
  for rid,request in ((s.get('agentState') or {}).get('requests') or {}).items():
   self.event('approval:'+rid,w['project'],w['id'],'input-required',summarize_request(rid,request)+' Answer in the chat, or with managerctl answer when the user tells you what to answer.')
  self.observe_context(w,s,msgs)
  if retire:self.retire_finished(w,s,msgs)
  return s
 def sweep_superseded(self):
  """Idle chats are not scanned (their edge sleeps), so settle stale assignments here, at most every 10 minutes.
  Only records change: no message, goal, pause, resource or controller is touched."""
  if time.time()-float(self.get('superseded-sweep-at') or 0)<600:return
  self.set('superseded-sweep-at',time.time())
  for a in self.rows("SELECT * FROM assignments WHERE state='sent' ORDER BY created"):
   if time.time()-a['created']<6*3600:continue
   try:
    w=self.worker(a['worker'])
    if w['state']=='released':
     # Same meaning as release(): the worker left management with this task outstanding.
     self.db.execute("UPDATE assignments SET state='released' WHERE id=? AND state='sent'",(a['id'],))
     self.set('superseded:'+a['id'],json.dumps({'at':time.time(),'note':'Worker released; assignment no longer tracked.'}));continue
    if not w['session']:continue
    q=self.api.call('/sessions/'+w['session']+'/messages/queued-state',{'localIds':[a['id']]})
    if not any(x['localId']==a['id'] for x in q.get('invokedLocalMessages',[])):self.assignment_superseded(w,a,q)
    elif a['goal']:self.goal_replaced(w,a)
   except Exception as e:log.warning('superseded sweep %s: %s',a['id'],e)
 def goal_replaced(self,w,a):
  """A thread has one native goal: an older goal assignment is moot once a newer one was delivered."""
  newer=self.rows("SELECT id FROM assignments WHERE worker=? AND goal=1 AND created>? AND state IN ('sent','finished','failed') ORDER BY created DESC LIMIT 1",(w['id'],a['created']))
  if not newer:return False
  q=self.api.call('/sessions/'+w['session']+'/messages/queued-state',{'localIds':[newer[0]['id']]})
  if not any(x['localId']==newer[0]['id'] for x in q.get('invokedLocalMessages',[])):return False
  self.db.execute("UPDATE assignments SET state='superseded' WHERE id=? AND state='sent'",(a['id'],))
  self.set('superseded:'+a['id'],json.dumps({'at':time.time(),'by':newer[0]['id'],'note':'A newer goal was delivered to this chat; this older goal assignment is replaced.'}))
  return True
 def assignment_superseded(self,w,a,q):
  """Settle an old assignment the hub has no record of once later work for the same chat completed.
  Input is consumed in order, so an unrecorded, unqueued instruction older than finished work cannot
  still be pending. Without this it blocks every later assignment forever. History is kept."""
  if q.get('queuedLocalIds') or q.get('indeterminateLocalIds') or a['state']!='sent':return False
  if time.time()-a['created']<6*3600:return False
  later=self.rows("SELECT id,state FROM assignments WHERE worker=? AND created>? AND state IN ('finished','failed') ORDER BY created LIMIT 1",(w['id'],a['created']))
  if not later:return False
  self.db.execute("UPDATE assignments SET state='superseded' WHERE id=? AND state='sent'",(a['id'],))
  self.set('superseded:'+a['id'],json.dumps({'at':time.time(),'by':later[0]['id'],'note':'No hub record (not queued, not invoked) and later work finished; no longer pending.'}))
  return True
 def observe_usage_limit(self,w,m):
  """Only provider records, never user text/tool output, may schedule recovery."""
  inner=content(m).get('content',{});data=inner.get('data',{})
  if not isinstance(data,dict) or data.get('isSidechain'):return
  import transient_recovery
  classified=transient_recovery.classify(m,w.get('native_id'))
  if classified and classified['kind']!='quota':return
  reset=None
  info=data.get('rate_limit_info',{})
  if data.get('type')=='rate_limit_event' and info.get('status')=='rejected':reset=info.get('resetsAt')
  message=data.get('message',{})
  parts=message.get('content',[]) if isinstance(message,dict) else []
  for part in parts if isinstance(parts,list) else []:
   if not isinstance(part,dict) or part.get('type')!='text':continue
   match=re.fullmatch(r'Claude AI usage limit reached\|(\d+)\|[^\n]*',part.get('text',''))
   if match:reset=int(match[1])
  # Codex structured errors and Claude API-error messages may lack a reset timestamp.
  error=data.get('error',{})
  code=error.get('code') if isinstance(error,dict) else None
  rejected=code in ('usage_limit_reached','rate_limit_exceeded') or data.get('error')=='rate_limit'
  if data.get('isApiErrorMessage'):
   rejected=rejected or any(isinstance(x,dict) and re.search(r"(usage limit|hit your limit|rate limit)",x.get('text',''),re.I) for x in parts)
  rejected=rejected or (data.get('type')=='thread_goal_updated' and (data.get('goal') or {}).get('status')=='usageLimited')
  if data.get('type') in ('error','task_failed'):
   rejected=rejected or data.get('codex_error_info')=='usageLimitExceeded' or bool(re.search(r"(usage limit|hit your usage limit|rate limit exceeded)",str(data.get('error','')),re.I))
  rejected=rejected or bool(classified and classified['kind']=='quota')
  if reset is None and not rejected:return
  old=self.rows('SELECT * FROM usage_recovery WHERE worker=?',(w['id'],))
  old=old[0] if old else None
  if old and old['source']==m['id']:return
  attempts=old['attempts'] if old and old['state']!='cancelled' else 0
  delay=min(6*3600,3600*2**min(attempts,3))
  due=max(time.time()+60,float(reset)+60) if isinstance(reset,(float,int)) else time.time()+delay
  self.db.execute("INSERT OR REPLACE INTO usage_recovery VALUES(?,?,?,?,?,?,?)",(w['id'],m['id'],due,attempts,'waiting',None,w['session']))
  # A limit is not assignment completion. Retain ownership until recovery concludes.
  self.event('usage:'+m['id'],w['project'],w['id'],'usage-wait','Usage exhausted; supervisor will continue the same chat after '+time.strftime('%Y-%m-%d %H:%M UTC',time.gmtime(due))+'. Do not send a competing retry.')
 def recover_usage(self):
  for r in self.rows("SELECT * FROM usage_recovery WHERE state IN ('waiting','sending','uncertain','sent','resume-check')"):
   if self.manager_paused():return
   w=self.worker(r['worker']);p=self.project(w['project'])
   if w['state']!='managed' or p['status']!='running' or p['manual'] or self.watch(w['id'])['mode']=='off':continue
   if self.session(w['session']).get('manualPausedAt') is not None:continue
   if r['state']=='resume-check':
    resumed=self.session(r['session'])
    if not resumed.get('active') or not native(resumed):continue
    if native(resumed)!=w['native_id']:
     self.db.execute("UPDATE usage_recovery SET state='identity-mismatch' WHERE worker=?",(w['id'],))
     self.event('usage-mismatch:'+r['source'],w['project'],w['id'],'identity-mismatch','Recovery identity mismatch; inspect before further work.')
     continue
    self.db.execute('UPDATE workers SET session=? WHERE id=?',(r['session'],w['id']))
    self.set('host:'+w['id'],(resumed.get('metadata') or {}).get('hostPid',''))
    self.db.execute("UPDATE usage_recovery SET state='waiting' WHERE worker=?",(w['id'],))
    continue
   if r['state']!='waiting':
    q=self.api.call('/sessions/'+r['session']+'/messages/queued-state',{'localIds':[r['local_id']]})
    if any(x['localId']==r['local_id'] for x in q.get('invokedLocalMessages',[])):
     self.db.execute("UPDATE usage_recovery SET state='done' WHERE worker=?",(w['id'],))
    # Unknown delivery is never resent. Same localId stays durable across restarts.
    continue
   if time.time()<r['due']:continue
   if self.checkout_busy(w):continue
   s=self.session(w['session'])
   if self.user_restarted(w,s):continue
   if s.get('thinking') or (s.get('agentState') or {}).get('requests') or (s.get('agentState') or {}).get('controlledByUser'):continue
   if not s.get('active'):
    if live_controller(s):continue
    # Persist before resume: ambiguous controller creation must never be repeated.
    self.db.execute("UPDATE usage_recovery SET state='resume-uncertain' WHERE worker=?",(w['id'],))
    self.event('usage-resume:'+r['source'],w['project'],w['id'],'usage-resume','Resuming the same native chat after usage reset; if outcome is uncertain, inspect before retrying.')
    result=self.api.call('/sessions/'+w['session']+'/resume',{})
    if result.get('type')!='success':continue
    self.db.execute("UPDATE usage_recovery SET state='resume-check',session=? WHERE worker=?",(result['sessionId'],w['id']))
    continue
   if time.time()<float(self.get('transient-next-dispatch') or 0):continue
   self.set('transient-next-dispatch',time.time()+30)
   aid=uid()
   self.db.execute("UPDATE usage_recovery SET state='sending',local_id=?,session=?,attempts=attempts+1 WHERE worker=?",(aid,w['session'],w['id']))
   text='Usage reset recovery: continue your interrupted task from its last checkpoint. First check current state and completed work; do not repeat completed actions. Preserve the latest user instructions and goal. If the task is already complete or cancelled, stop.'
   try:self.api.call('/sessions/'+w['session']+'/messages',{'text':text,'localId':aid})
   except Exception:
    self.db.execute("UPDATE usage_recovery SET state='uncertain' WHERE worker=?",(w['id'],));raise
   self.db.execute("UPDATE usage_recovery SET state='sent' WHERE worker=?",(w['id'],))
 def retention(self,wid):
  return json.loads(self.get('retention:'+wid) or '{"mode":"until-idle","reason":""}')
 def end_management(self,w,completed=None,reason=None):
  """Release oversight only. Node connections never retire on completion."""
  if completed and self.get('node-graph-active')=='1':return
  if self.get('unmanaged:'+w['id']):
   if completed is None:
    self.db.execute('DELETE FROM settings WHERE key=?',('retirement-notify:'+w['id'],))
    self.db.execute('UPDATE events SET acked=1 WHERE worker=?',(w['id'],))
   return
  watch=self.watch(w['id'])
  self.db.execute('BEGIN IMMEDIATE')
  try:
   self.set('unmanaged-watch:'+w['id'],json.dumps(watch))
   self.set('unmanaged:'+w['id'],json.dumps({'at':time.time(),'session':w['session'],'automatic':completed is not None}))
   self.set('watch:'+w['id'],json.dumps({'mode':'off','note':'Finished idle chat automatically unmanaged' if completed else 'User unmanaged this chat'}))
   self.db.execute("UPDATE workers SET state='released' WHERE id=?",(w['id'],))
   self.db.execute("UPDATE usage_recovery SET state='cancelled' WHERE worker=?",(w['id'],))
   self.cancel_worker_reminders(w['id'],reason or ('Oversight automatically ended' if completed else 'Explicitly unmanaged'))
   self.db.execute('UPDATE events SET acked=1 WHERE worker=?',(w['id'],))
   if completed and not completed.get('reported'):
    key='retired:'+w['id']+':'+completed['message']
    self.event(key,w['project'],w['id'],'finished-unmanaged','Work ended and chat is idle; oversight released automatically. This does not prove the entire project complete. Latest public result: '+(completed['reply'] or 'Inspect the chat result.'))
    self.set('retirement-notify:'+w['id'],key if watch['mode']=='events' else '')
   self.db.execute('DELETE FROM settings WHERE key=?',('retirement-candidate:'+w['id'],))
   self.db.execute('COMMIT')
  except Exception:self.db.execute('ROLLBACK');raise
 def result_needs_followup(self,text):
  """Conservative public-result guard. A successful turn can end at a blocker."""
  text=re.sub(r'[*_`]', '', text or '').replace('’', "'")
  from coordination import signals
  return signals(text)['dependency-wait'] or bool(re.search(r"\b(waiting for your|need your|awaiting (?:user|your)|blocked|requires? your|please\s+(?!note\b)|(?:could|can|would) you\b|(?:couldn't|cannot|can't|unable to)\s+(?:access|continue|proceed|finish|complete|connect|verify)|(?:reply|respond|let me know)\s+(?:when|once|with|ready)|(?:not yet|still not)\s+(?:complete|finished|available))",text,re.I))
 def retire_finished(self,w,s,msgs):
  """Two idle observations after a successful terminal record; never stop runtime work."""
  key='retirement-candidate:'+w['id']
  def held():
   self.db.execute('DELETE FROM settings WHERE key=?',(key,))
  if self.get('node-graph-active')=='1':return held()
  p=self.project(w['project'])
  from coordination import holds_retirement, waiting_snapshot
  if holds_retirement(self,w['id']) or waiting_snapshot(msgs):return held()
  # Important unsent evidence must reach the manager before automatic release.
  if any(json.loads(e['detail']).get('changes') for e in self.rows("SELECT detail FROM events WHERE worker=? AND kind='observation' AND acked=0 AND wake_id IS NULL",(w['id'],))):return held()
  if w['state']!='managed' or self.get('unmanaged:'+w['id']) or w['session']==self.get('manager') or self.watch(w['id'])['mode']=='off':return held()
  if self.retention(w['id'])['mode']=='keep' or p['status']=='paused' or p['manual'] or self.manager_paused():return held()
  state=s.get('agentState') or {}
  if not s.get('active') or s.get('thinking') or s.get('backgroundTaskCount',0) or s.get('manualPausedAt') is not None or state.get('requests') or state.get('controlledByUser') or state.get('claudeReadyMessages'):return held()
  if self.rows("SELECT id FROM assignments WHERE worker=? AND state IN ('sent','sending','uncertain')",(w['id'],)):return held()
  if self.rows("SELECT worker FROM usage_recovery WHERE worker=? AND state NOT IN ('done','cancelled')",(w['id'],)):return held()
  # Explicit future checks are a reason to keep oversight. Routine observation
  # schedules alone must not turn a completed task into perpetual management.
  if self.rows("SELECT id FROM reminders WHERE active=1 AND (worker=? OR project=?) AND id NOT IN (SELECT substr(key,length('reminder-progress:')+1) FROM settings WHERE key LIKE 'reminder-progress:%' AND value='1')",(w['id'],w['project'])):return held()
  terminal=None;latest_user=None;pending=[]
  ordered=sorted(msgs,key=lambda m:(m.get('seq',0),m.get('createdAt',0)))
  for index,m in enumerate(ordered):
   c=content(m);inner=c.get('content',{});data=inner.get('data',{}) if isinstance(inner,dict) else {}
   if is_instruction_message(m):latest_user=index
   if c.get('role')=='user' and m.get('localId') and not m.get('invokedAt'):pending.append(m['localId'])
   if c.get('role')!='agent' or not isinstance(data,dict) or data.get('isSidechain'):continue
   kind=data.get('type');status=(data.get('goal') or {}).get('status')
   success=kind=='task_complete' or (inner.get('type')=='event' and kind=='ready') or (kind=='system' and data.get('subtype')=='turn_duration') or (kind=='thread_goal_updated' and status in ('complete','completed'))
   failed=kind in ('task_failed','turn_aborted') or (inner.get('type')=='event' and kind in ('error','abort-restore'))
   if success or failed:terminal={'index':index,'message':m['id'],'success':success}
  if not terminal or not terminal['success'] or (latest_user is not None and latest_user>terminal['index']):return held()
  if pending:
   q=self.api.call('/sessions/'+w['session']+'/messages/queued-state',{'localIds':pending})
   if q.get('queuedLocalIds') or q.get('indeterminateLocalIds'):return held()
  snap=self.progress_snapshot(w,s,msgs,reply_limit=None)
  if self.result_needs_followup(snap['latest_reply']):return held()
  try:goal=self.api.call('/sessions/'+w['session']+'/goal')['goal']
  except Exception:return held()  # unknown native goal state is not completion
  if goal and goal.get('status') not in ('complete','completed','cleared'):return held()
  old=json.loads(self.get(key) or '{}')
  if old.get('message')!=terminal['message']:
   self.set(key,json.dumps({'message':terminal['message'],'at':time.time(),'notices':[e['id'] for e in self.rows("SELECT id FROM events WHERE worker=? AND acked=0 AND wake_id IS NULL AND kind IN ('finished','observation')",(w['id'],))]}));return
  if time.time()-old['at']<5:return
  # Refresh immediately before registry mutation to catch a new turn/user input.
  fresh=self.session(w['session']);agent=fresh.get('agentState') or {}
  if not fresh.get('active') or fresh.get('thinking') or fresh.get('backgroundTaskCount',0) or fresh.get('manualPausedAt') is not None or agent.get('requests') or agent.get('claudeReadyMessages') or agent.get('controlledByUser'):return held()
  if w['native_id'] and native(fresh) and native(fresh)!=w['native_id']:return held()
  notice=self.get('completion-notice:'+w['id']+':'+terminal['message'])
  reported=bool(notice and self.rows("SELECT id FROM outbox WHERE id=? AND state IN ('sending','sent','uncertain','invoked')",(notice,))) or any(self.one('SELECT wake_id FROM events WHERE id=?',(eid,))['wake_id'] for eid in old.get('notices',[]))
  self.end_management(w,{'message':terminal['message'],'reply':(snap['latest_reply'] or '')[:600],'reported':reported})
 def current_terminal_message(self,w):
  """Correlate a completion wake to the latest terminal record after user input."""
  messages=self.api.call('/sessions/'+w['session']+'/messages?limit=100')['messages']
  terminal=None
  for m in sorted(messages,key=lambda m:(m.get('seq',0),m.get('createdAt',0))):
   c=content(m);inner=c.get('content',{});data=inner.get('data',{}) if isinstance(inner,dict) else {}
   if is_instruction_message(m):terminal=None
   if c.get('role')!='agent' or not isinstance(data,dict) or data.get('isSidechain'):continue
   kind=data.get('type')
   if kind=='task_complete' or (inner.get('type')=='event' and kind=='ready') or (kind=='system' and data.get('subtype')=='turn_duration') or (kind=='thread_goal_updated' and (data.get('goal') or {}).get('status') in ('complete','completed')):terminal=m['id']
  return terminal
 def retirement_deliverable(self,e):
  return bool(e['worker'] and e['kind']=='finished-unmanaged' and self.get('retirement-notify:'+e['worker'])==e['event_key'])
 def publish_managed(self,path=None):
  """Chats the manager actively follows, for the web UI (distinct running indicator)."""
  out={}
  for w in self.rows("SELECT w.id,w.session,w.project,p.name,p.status,p.manual FROM workers w JOIN projects p ON p.id=w.project WHERE w.session IS NOT NULL AND w.state='managed'"):
   mode=self.watch(w['id'])['mode']
   if mode!='off':
    out[w['session']]={'role':'worker','worker':w['id'],'project':w['name'],'watch':mode,'projectStatus':w['status'],'manualHold':bool(w['manual'])}
    if self.get('node-graph-active')=='1':out[w['session']].update(owner=self.get('owner:'+w['id']) or self.get('manager'),asleep=bool(self.get('asleep:'+w['id'])))
    owner=self.get('owner:'+w['id']) or self.get('manager')
    observed=json.loads(self.get('observation:'+w['id']) or '{}').get('observedAt')
    asleep=bool(self.get('asleep:'+w['id']))
    owner_state=getattr(self,'_oversight_owners',{}).get(owner,'unknown')
    policy=self.reporting(w['id'])
    state='held' if w['status']=='paused' or w['manual'] else 'quiet' if mode!='events' or not policy['enabled'] else 'idle' if asleep else 'attention' if self.get('scan-error:'+w['id']) else 'watching' if observed and time.time()-observed<=120 else 'unknown'
    pending=self.one('SELECT COUNT(*) AS n FROM events WHERE worker=? AND acked=0',(w['id'],))['n']
    out[w['session']]['oversight']={'pendingUpdates':pending,'state':state,'observedAt':observed,'publishedAt':time.time(),'owner':owner,'ownerState':owner_state,'notifications':bool(mode=='events' and policy['enabled']),'fallbackMinutes':policy['fallback_minutes']}

  if self.get('manager'):out[self.get('manager')]={'role':'manager'}
  path=Path(path or STATE/'managed-sessions.json');tmp=path.with_suffix('.tmp')
  tmp.write_text(json.dumps({'updatedAt':time.time(),'sessions':out}));tmp.chmod(0o600);tmp.replace(path)
  return out
 def watch(self,wid):
  return json.loads(self.get('watch:'+wid) or '{"mode":"events","note":""}')
 def reporting(self,wid=None):
  base=json.loads(self.get('reporting-default'))
  override=json.loads(self.get('reporting:'+wid) or '{}') if wid else {}
  return {**base,**override}
 def reporting_allowed(self,w):
  p=self.project(w['project'])
  return w['state']=='managed' and p['status']!='paused' and not p['manual'] and self.watch(w['id'])['mode']=='events' and self.reporting(w['id'])['enabled']
 def important_change(self,text,context):
  """Conservative evidence candidates, not a semantic claim of a breakthrough."""
  if not text:return None
  lower=text.lower()
  if re.search(r'\b(breakthrough|root cause identified|root cause found|unblocked|major regression|data loss)\b',lower) and not re.search(r'\b(no|not|without|if|hopefully)\b',lower):
   return 'Reported significant change; manager must assess context'
  if re.search(r'\b(waiting for your (?:input|approval|decision)|need your (?:input|approval|decision))\b',lower):
   return 'Reported need for user input; inspect the exact question'
  ratio_claim=re.search(r'(\d+(?:\.\d+)?)\s*[x×]\s*(?:faster|speedup|slower|throughput)',lower)
  if ratio_claim and float(ratio_claim[1])>=2 and not re.search(r'\b(not|target|aim|goal|hope|expect|would|could|if)\b',lower):
   return 'Large reported performance ratio; verify comparable workloads'
  # Compare explicit same-unit timing/throughput measurements, never arbitrary numbers.
  timing=r'(ms|milliseconds?|s|seconds?|fps)'
  match=re.search(r'(?:from\s+)?(\d+(?:\.\d+)?)\s*'+timing+r'\s+(?:to|→|->)\s*(\d+(?:\.\d+)?)\s*'+timing+r'\b',lower)
  if match and not re.search(r'\b(target|aim|goal|hope|expect|would|could|if)\b',lower):
   a,ua,b,ub=float(match[1]),match[2],float(match[3]),match[4]
   if a>0 and b>0:
    if ua=='fps' and ub=='fps':ratio=b/a
    elif ua!='fps' and ub!='fps':ratio=(a/(1000 if ua.startswith('m') else 1))/(b/(1000 if ub.startswith('m') else 1))
    else:return None
    if ratio>=2 or ratio<=0.5:
     stalled=any(re.search(r'\b(stagnat|stuck|plateau|no improvement)',x.lower()) for x in context)
     return ('Large reported benchmark change after reported stagnation' if stalled else 'Large reported benchmark change')+'; verify comparable workloads'
  return None
 def observe_context(self,w,s,msgs):
  """Silent scans reuse fetched context. Only selected evidence becomes a manager event."""
  key='observation:'+w['id'];now=time.time();old=json.loads(self.get(key) or '{}')
  if old.get('suppressed'):old={}
  ordered=sorted(msgs,key=lambda m:(m.get('seq',0),m.get('createdAt',0)))
  watermark=max([m.get('seq',0) for m in ordered]+[old.get('seq',0)])
  snap=self.progress_snapshot(w,s,ordered)
  state={**old,'seq':watermark,'snapshot':snap,'observedAt':now,'startedAt':old.get('startedAt',now),
   'backgroundProcesses':[p['id'] for p in s.get('backgroundProcesses',[]) if isinstance(p,dict) and isinstance(p.get('id'),str)]}
  from coordination import update_watches
  watch_candidates=update_watches(self,w,[],ordered,s)
  if not self.reporting_allowed(w):
   # Do not replay activity accrued during a pause/quiet/off period on re-enable.
   self.discard_routine_updates(w['id'])
   state.pop('pendingNativeFinish',None);state['startedAt']=now;state['suppressed']=True;self.set(key,json.dumps(state));return
  policy=self.reporting(w['id']);context=old.get('context',[]);reported=old.get('reported',[]);candidates=[]
  failure=s.get('lastFailure')
  import transient_recovery
  retrying=transient_recovery.blocked(self,w['session']) # provider rejection with a pending automatic retry
  if policy['important'] and not s.get('thinking') and isinstance(failure,dict):
   version=hashlib.sha256(json.dumps(failure,sort_keys=True).encode()).hexdigest()
   if old.get('failureNotice')!=version and not retrying:
    candidates.append({'category':'runtime-failure','reason':'Runtime reported a failed invocation. Inspect the saved error and checkpoint; idle controller does not mean successful completion. No automatic retry was sent.', 'failure':failure})
   state['failureNotice']=version
  elif not failure:state.pop('failureNotice',None)
  # Native snapshots bind a process handle to its exact originating item/PID.
  # Only two fresh complete snapshots can establish a disappearance; timeouts,
  # stale snapshots and a different controller/thread are never completion.
  from native_background import fresh_snapshot, intent_key
  native_now=fresh_snapshot(s,now);native_old=old.get('codexNativeBackground')
  if native_now:
   state.pop('codexNativeUnconfirmed',None)
   state['codexNativeBackground']=native_now
   if (policy['important'] and s.get('manualPausedAt') is None and native_old and native_old.get('terminals')
       and now-native_old.get('checkedAt',0)/1000>45 and any(k not in native_now['terminals'] for k in native_old['terminals'])):
    candidates.append({'category':'native-terminal-unconfirmed','reason':'A native terminal disappeared during an observation gap. Its outcome is unconfirmed; do not assume completion or replay work.'})
   if policy['important'] and s.get('manualPausedAt') is None and native_old and native_old.get('threadId')==native_now['threadId'] and native_now['checkedAt']>native_old.get('checkedAt',0) and 0<=now-native_old.get('checkedAt',0)/1000<=45:
    for identity,job in native_old.get('terminals',{}).items():
     if identity in native_now['terminals']:continue
     raw=self.get(intent_key(w['id'],job,native_now['threadId']));intent=json.loads(raw) if raw else None
     if s.get('thinking') and (not intent or intent.get('intent')!='review'):continue
     candidates.append({'category':'native-terminal-ended','reason':'Native terminal is no longer listed; inspect exit and any declared review before claiming success. No worker was resumed.',
                        'terminal':job,'intent':intent})
  else:
   state.pop('codexNativeBackground',None)
   if policy['important'] and s.get('active') and s.get('manualPausedAt') is None and native_old and native_old.get('terminals') and not old.get('codexNativeUnconfirmed'):
    candidates.append({'category':'native-terminal-unconfirmed','reason':'The last native terminal snapshot expired or became unavailable. Background work status is unconfirmed; do not assume completion or replay work.'})
    state['codexNativeUnconfirmed']=True
  from coordination import observe as observe_coordination, update_watches
  prior_coordination=old.get('coordination')
  if prior_coordination is None and old:
   prior_coordination=observe_coordination(self,w,[m for m in ordered if m.get('seq',0)<=old.get('seq',0)],None)[0]
  coordination,coordination_candidates=observe_coordination(self,w,ordered,prior_coordination)
  state['coordination']=coordination
  # Delivery transitions are separate from goal metadata and model output. Seed
  # historical inputs silently; alert once if fresh input is waiting on busy work.
  from assignment_delivery import summarize_inputs
  inputs=summarize_inputs(ordered,include_wait=False)
  state['inputStates']={i.get('localId') or i.get('messageId'):i['state'] for i in inputs}
  if policy['important'] and old and s.get('thinking'):
   for item in inputs:
    identity=item.get('localId') or item.get('messageId');before=old.get('inputStates',{}).get(identity)
    fresh=(item.get('createdAt') or 0)>old.get('observedAt',now)*1000
    if before==item['state'] or (before is None and not fresh):continue
    own=item.get('localId')
    # The supervisor's own recovery/wake messages are not user steering; their receipts are not news.
    if own and (self.rows('SELECT 1 FROM transient_recovery WHERE local_id=?',(own,)) or self.rows('SELECT 1 FROM usage_recovery WHERE local_id=?',(own,)) or self.rows('SELECT 1 FROM outbox WHERE id=?',(own,))):continue
    if item['state'] in ('queued','indeterminate','unconfirmed'):
     reason='Input is waiting while the worker is busy; it is not confirmed steering. Inspect delivery before claiming the scope changed.'
    elif item.get('goal'):
     reason='Goal control was consumed/acknowledged; saved metadata does not confirm current-turn steering or resume a blocked goal.'
    elif item['state'] in ('consumed','steered'):
     reason='Runtime input receipt changed; receipt is not proof of compliance. Read subsequent work before reporting the requested change as applied.'
    else:continue
    candidates.append({'category':'input-delivery','reason':reason,'message':item.get('messageId'),'delivery':item})
  if policy['important']:
   candidates.extend(watch_candidates)
   candidates.extend(coordination_candidates)
   candidates.extend(update_watches(self,w,coordination_candidates,ordered,s))
  message_versions={};background_exits=[];native_background_exits=[]
  from background_evidence import claude_completed
  for m in ordered:
   c=content(m);d=c.get('content',{});data=d.get('data',{}) if isinstance(d,dict) else {}
   if not isinstance(data,dict) or c.get('role')!='agent' or data.get('isSidechain'):continue
   single=self.progress_snapshot(w,s,[m]);text=single['latest_reply']
   version=hashlib.sha256(json.dumps([text,data.get('type'),data.get('subtype'),data.get('goal')],sort_keys=True).encode()).hexdigest()
   message_versions[m['id']]=version
   if old and m.get('seq',0)<=old.get('seq',0) and old.get('message_versions',{}).get(m['id'])==version:continue
   output=data.get('output')
   if data.get('type')=='tool-call-result' and isinstance(output,dict) and output.get('type')=='exec_command_end' and isinstance(output.get('exit_code'),int) and m.get('seq',0)>old.get('seq',0):
    background_exits.append({'message':m['id'],'callId':data.get('callId'),'exitCode':output['exit_code']})
   if text:
    fingerprint=hashlib.sha256(text.encode()).hexdigest()
    reason=self.important_change(text,context) if old and policy['important'] else None
    if reason and fingerprint not in reported:
     candidates.append({'reason':reason,'text':text,'message':m['id']});reported.append(fingerprint)
    if not old:reported.append(fingerprint)
    context=(context+[text])[-12:]
   if m.get('seq',0)>old.get('seq',0):
    receipt=claude_completed(m)
    if receipt:native_background_exits.append(receipt)
   kind=data.get('type')
   # A turn end is evidence to review, never proof a larger goal is complete.
   terminal=kind in ('task_complete','task_failed','turn_aborted') or (kind=='thread_goal_updated' and (data.get('goal') or {}).get('status') in ('complete','completed')) or (d.get('type')=='event' and kind in ('ready','error','abort-restore')) or (kind=='system' and data.get('subtype')=='turn_duration')
   if old and terminal:state['terminal']={'message':m['id'],'at':now}
  goal=None;goal_known=False
  result_id=self.one("SELECT COALESCE(MAX(id),0) AS id FROM events WHERE worker=? AND event_key LIKE 'result:%'",(w['id'],))['id']
  terminal=state.get('terminal')
  if terminal and not s.get('thinking'):
   if now-terminal['at']<300 and result_id<=old.get('result_id',0):
    # Goal-driven turn boundaries are routine, not completion notifications.
    if not goal_known:
     try:goal=self.api.call('/sessions/'+w['session']+'/goal').get('goal')
     except Exception:goal={'status':'unknown'}
    if (not goal or goal.get('status') not in ('active','running','in_progress','usageLimited')) and not retrying:
     candidates.append({'reason':'Runtime turn/goal ended; inspect outcome, do not assume whole task complete','message':terminal['message']})
   state.pop('terminal',None)
  state['result_id']=result_id
  # A conversational turn can finish hours before its render/review processes.
  # Their final transition to idle has no new model terminal record. Nominate a
  # review, not success or an automatic continuation. Count-only corrections
  # (e.g. stale TaskStop accounting) must not manufacture completion notices.
  prior_background=old.get('snapshot',{}).get('status')=='background'
  idle_after=snap['status'] in ('idle','waiting for dependency','disconnected') and not s.get('backgroundTaskCount',0)
  pending_native=old.get('pendingNativeFinish')
  # The count patch and native receipt can arrive in either order. Keep only a
  # short, silent correlation window, never resurrecting a task or dispatching work.
  if prior_background and idle_after and (old.get('snapshot',{}).get('backgroundEvidence') or {}).get('reportedTasks',0)>0 and not native_background_exits:
   state['pendingNativeFinish']=now
  elif not idle_after or (pending_native and now-pending_native>300):state.pop('pendingNativeFinish',None)
  native_after=bool(native_background_exits and (prior_background or (pending_native and 0<=now-pending_native<=300)))
  if policy['important'] and idle_after and ((prior_background and (old.get('backgroundProcesses') or background_exits)) or native_after):
   state.pop('pendingNativeFinish',None)
   native_terminal_changes=[c for c in candidates if c.get('category')=='native-terminal-ended']
   candidates=[c for c in candidates if c.get('category')!='native-terminal-ended']
   candidates.append({'category':'background-ended','reason':'Background work is no longer reported; inspect exit evidence and any pending final review before declaring completion or arranging a scoped follow-up. No worker was resumed.',
    'previousProcesses':old.get('backgroundProcesses',[]),'exits':background_exits,'nativeTaskExits':native_background_exits,'nativeTerminals':native_terminal_changes})
  if old.get('snapshot',{}).get('status') in ('running','background') and snap['status']=='disconnected':
   candidates.append({'reason':'Transport disconnected during work; outcome unknown, do not restart automatically'})
  # Successful ordinary prompts do not reactivate a blocked native goal.
  # Observe authoritative saved state at idle, even without a new terminal event.
  # One candidate per blocked episode; no retry, goal mutation or idle polling.
  if w['runtime']=='codex' and not s.get('thinking'):
   try:
    goal=self.api.call('/sessions/'+w['session']+'/goal').get('goal');goal_known=True
   except Exception:pass
   if goal_known:
    state['nativeGoal']={'status':goal.get('status'), 'updatedAt':goal.get('updatedAt'), 'objective':str(goal.get('objective') or '')[:1000], 'scope':'saved-goal-only'} if isinstance(goal,dict) else None
    snap['nativeGoal']=state['nativeGoal']
    blocked=isinstance(goal,dict) and goal.get('status')=='blocked'
    if blocked and snap['status']=='idle' and policy['important']:
     episode=hashlib.sha256(json.dumps([goal.get('objective'),goal.get('updatedAt')],sort_keys=True).encode()).hexdigest()
     if old.get('blockedGoalNotice')!=episode:
      candidates.append({'category':'native-goal-blocked','reason':'Saved native goal remains blocked; current-task relevance is unverified. Later requests can finish while an older goal stays blocked. Compare this objective/date with the latest authorized work before treating it as a task blocker. Never resume an obsolete goal merely to clear its label. No automatic retry was sent.', 'goalStatus':'blocked','goalUpdatedAt':goal.get('updatedAt'),'scope':'saved-goal-only','currentTaskRelevance':'unverified'})
     state['blockedGoalNotice']=episode
    elif not blocked:state.pop('blockedGoalNotice',None)
  state['context']=context[-12:];state['reported']=reported[-64:];state['message_versions']=dict(list(message_versions.items())[-150:])
  # Explicit progress/reminder schedules override the routine fallback, not important evidence.
  scheduled=bool(self.rows('SELECT id FROM reminders WHERE active=1 AND every IS NOT NULL AND worker=?',(w['id'],)))
  last=float(self.get('reporting-last:'+w['id']) or state['startedAt'])
  working=snap['status'] in ('running','background')
  # Waiting time never counts toward the next routine update after work resumes.
  since=old.get('routineSince',old.get('startedAt',now)) if old.get('snapshot',{}).get('status') in ('running','background') else now
  state['routineSince']=since if working else now
  due=now-max(last,state['routineSince'])>=policy['fallback_minutes']*60
  if not working:self.discard_routine_updates(w['id'])
  if candidates or (due and working and not scheduled):
   self.queue_observation(w,snap,candidates,now)
  self.set(key,json.dumps(state))
 def queue_observation(self,w,snap,candidates,now):
  previous=self.rows("SELECT * FROM events WHERE worker=? AND kind='observation' AND acked=0 AND wake_id IS NULL ORDER BY id DESC LIMIT 1",(w['id'],))
  prior=json.loads(previous[0]['detail']).get('changes',[]) if previous else []
  changes=(prior+candidates)[-8:]
  detail=json.dumps({'observationOnly':True,'observedAt':now,'trackerStatus':self.project(w['project'])['status'],'changes':changes,'reason':'important' if changes else 'routine fallback',**snap})
  if previous:self.db.execute('UPDATE events SET detail=? WHERE id=?',(detail,previous[0]['id']))
  else:self.event('observation:'+w['id']+':'+uid(),w['project'],w['id'],'observation',detail)
 def progress_snapshot(self,w,s=None,msgs=None,reply_limit=600):
  """Read public conversation evidence only; never prompt a busy worker for status."""
  s=s if s is not None else self.session(w['session'])
  if w['native_id'] and native(s) and native(s)!=w['native_id']:raise ValueError('Native identity mismatch')
  msgs=msgs if msgs is not None else self.api.call('/sessions/'+w['session']+'/messages?limit=100')['messages']
  from resource_handoff import public_reply
  reply=None;step=None;evidence=None
  for m in sorted(msgs,key=lambda m:(m.get('seq',0),m.get('createdAt',0)),reverse=True):
   c=content(m);d=c.get('content',{})
   if c.get('role')!='agent' or not isinstance(d,dict):continue
   data=d.get('data') if isinstance(d.get('data'),dict) else {}
   # Claude tool results and reasoning are not public status reports.
   msg=data.get('message') if isinstance(data.get('message'),dict) else {}
   if data.get('type') in ('user','reasoning','thinking','agent_reasoning','reasoning_delta') or msg.get('role')=='user' or data.get('isSidechain'):continue
   parts=msg.get('content') if isinstance(msg.get('content'),list) else []
   text=public_reply(m,include_terminal=True)
   if isinstance(text,str) and text.startswith('Claude AI usage limit '):text=None
   if not reply and isinstance(text,str) and text.strip():
    reply=' '.join(text.split())[:reply_limit];evidence=m.get('id')
   if not step:
    tool=next((x for x in reversed(parts) if isinstance(x,dict) and x.get('type')=='tool_use'),None)
    if tool:
     step=str(tool.get('name','tool'))[:100]
     args=tool.get('input') if isinstance(tool.get('input'),dict) else {}
     current=next((t.get('activeForm') or t.get('content') or t.get('step') for t in (args.get('todos') or args.get('plan') or []) if isinstance(t,dict) and t.get('status')=='in_progress'),None)
     description=current or args.get('description')
     if isinstance(description,str):step+=': '+' '.join(description.split())[:200]
    elif data.get('type') in ('tool-call','tool_call'):step=str(data.get('name') or data.get('toolName') or 'tool')[:100]
   if reply and step:break
  requests=(s.get('agentState') or {}).get('requests') or {}
  status=self.progress_status(w,s)
  from coordination import waiting_snapshot
  if status in ('running','idle') and not s.get('backgroundTaskCount',0) and waiting_snapshot(msgs):status='waiting for dependency'
  processes=s.get('backgroundProcesses') or []
  background={'reportedTasks':max(0,(s.get('backgroundTaskCount') or 0)-len(processes)),'localProcesses':[{'pid':p.get('pid'),'command':p.get('command'),'checkedAt':p.get('checkedAt')} for p in processes[:8]],'limits':'Process liveness is not useful progress or render health; helpers and servers can remain alive while idle. Remote/detached work may be invisible.'}
  from native_background import fresh_snapshot, intent_key
  native_jobs=fresh_snapshot(s)
  native_intents=[]
  if native_jobs:
   for job in native_jobs['terminals'].values():
    raw=self.get(intent_key(w['id'],job,native_jobs['threadId']))
    native_intents.append({**job,'intent':json.loads(raw) if raw else None})
  return {'failure':s.get('lastFailure'),'backgroundEvidence':background,'nativeBackground':{'snapshot':native_jobs,'jobs':native_intents},'status':status,'latest_reply':reply,'latest_tool':step,'evidence_message':evidence,
   'pending_requests':[summarize_request(k,v) for k,v in sorted(requests.items())]}
 def progress_status(self,w,s):
  """Wait states outrank stale runtime activity flags; this never resumes work."""
  p=self.project(w['project']);agent=s.get('agentState') or {}
  if s.get('manualPausedAt') is not None or p['status']=='paused' or p['manual']:return 'paused'
  if self.rows("SELECT worker FROM usage_recovery WHERE worker=? AND state NOT IN ('done','cancelled')",(w['id'],)):return 'waiting for usage reset'
  if agent.get('requests'):return 'waiting for input'
  import transient_recovery
  if not s.get('thinking') and transient_recovery.blocked(self,w['session']):return 'waiting for transient retry'
  if not s.get('thinking') and s.get('lastFailure'):return 'failed'
  from node_graph import has_local_work
  if not s.get('active'):return 'background' if has_local_work(s) else 'disconnected'
  if agent.get('controlledByUser'):return 'waiting under user control'
  return 'running' if s.get('thinking') else 'background' if s.get('backgroundTaskCount',0)>0 else 'idle'
 def routine_update(self,e):
  return e['kind']=='progress' or (e['kind']=='observation' and not json.loads(e['detail']).get('changes'))
 def discard_routine_updates(self,wid):
  # Keep significant evidence, and never withdraw a message already delivered.
  for e in self.rows("SELECT * FROM events WHERE worker=? AND kind IN ('progress','observation') AND acked=0 AND wake_id IS NULL",(wid,)):
   if self.routine_update(e):self.db.execute('UPDATE events SET acked=1 WHERE id=?',(e['id'],))
 def progress_event(self,r,w):
  p=self.project(w['project'])
  # Completed trackers may have new direct work; do not reopen their old scope.
  if w['state']!='managed' or p['status']=='paused' or p['manual'] or self.watch(w['id'])['mode']=='off':return
  snap=self.progress_snapshot(w)
  if snap['status'] not in ('running','background'):
   self.discard_routine_updates(w['id']);return
  comparable={k:v for k,v in snap.items() if k!='evidence_message'}
  signature=hashlib.sha256(json.dumps(comparable,sort_keys=True).encode()).hexdigest()
  key='progress-last:'+w['id']
  if self.get(key)==signature:return
  detail=json.dumps({'observationOnly':True,'trackerStatus':p['status'],**snap},ensure_ascii=False)
  # A busy/offline manager gets the newest unsent snapshot, not a backlog of ticks.
  self.db.execute('SAVEPOINT reminder_progress')
  try:
   self.db.execute("UPDATE events SET acked=1 WHERE worker=? AND kind='progress' AND acked=0 AND wake_id IS NULL",(w['id'],))
   self.event('progress:'+r['id']+':'+str(r['due']),w['project'],w['id'],'progress',detail)
   self.set(key,signature);self.db.execute('RELEASE reminder_progress')
   return 'progress:'+r['id']+':'+str(r['due'])
  except Exception:
   self.db.execute('ROLLBACK TO reminder_progress');self.db.execute('RELEASE reminder_progress');raise
 def progress_deliverable(self,e):
  w=self.worker(e['worker']);p=self.project(w['project'])
  if w['state']!='managed' or p['status']=='paused' or p['manual'] or self.watch(w['id'])['mode']=='off':return False
  if e['kind']=='observation' and not self.reporting_allowed(w):return False
  # Recheck at delivery: a queued routine report can outlive the work it describes.
  if self.routine_update(e) and self.progress_snapshot(w)['status'] not in ('running','background'):
   self.discard_routine_updates(w['id']);return False
  # Never queue a second status nudge while the first has not been consumed.
  return not self.rows("SELECT o.id FROM outbox o JOIN events e ON e.wake_id=o.id WHERE e.worker=? AND e.kind IN ('progress','observation') AND o.state IN ('sending','sent','uncertain')",(w['id'],))
 def reminder_audit(self,r,status,event_key=None,error=None,reason=None,next_attempt_at=None):
  self.db.execute("""INSERT INTO reminder_deliveries VALUES(?,?,?,?,?,?,?,?)
ON CONFLICT(reminder_id,due) DO UPDATE SET status=excluded.status,event_key=COALESCE(excluded.event_key,reminder_deliveries.event_key),updated_at=excluded.updated_at,next_attempt_at=excluded.next_attempt_at,error=excluded.error,reason=excluded.reason""",
   (r['id'],r['due'],status,event_key,time.time(),next_attempt_at,error,reason))
 def reminder_info(self,r,history=False):
  audits=self.rows('SELECT * FROM reminder_deliveries WHERE reminder_id=? ORDER BY due DESC',(r['id'],))
  current=next((a for a in audits if a['due']==r['due']),audits[0] if audits else None)
  if not current:current={'status':'legacy-pending' if r['active'] else 'legacy-inactive','reason':'No historical delivery/cancellation audit was retained; inactive schedules are never automatically replayed.'}
  value={**r,'progress':self.get('reminder-progress:'+r['id'])=='1','delivery':current}
  if history:value['delivery_history']=audits
  if current.get('event_key'):
   events=self.rows('SELECT acked,wake_id FROM events WHERE event_key=?',(current['event_key'],))
   if events:
    e=events[0];value['delivery']={**current,'event_acked':bool(e['acked']),'wake_id':e['wake_id']}
    if e['wake_id']:value['delivery']['wake_state']=(self.rows('SELECT state FROM outbox WHERE id=?',(e['wake_id'],)) or [{'state':'unknown'}])[0]['state']
  return value
 def cancel_reminder_record(self,r,reason):
  self.db.execute('UPDATE reminders SET active=0 WHERE id=?',(r['id'],))
  # Cancellation withdraws only unsent notices. An accepted/uncertain wake keeps
  # its stable identity and must not be silently resent or claimed withdrawn.
  for prefix in ('remind:','progress:'):
   start=prefix+r['id']+':'
   self.db.execute('UPDATE events SET acked=1 WHERE substr(event_key,1,?)=? AND wake_id IS NULL',(len(start),start))
  self.reminder_audit(r,'cancelled',reason=reason)
 def cancel_worker_reminders(self,wid,reason):
  for r in self.rows('SELECT * FROM reminders WHERE worker=?',(wid,)):
   pending=any(self.rows('SELECT id FROM events WHERE substr(event_key,1,?)=? AND acked=0 AND wake_id IS NULL',(len(prefix+r['id']+':'),prefix+r['id']+':')) for prefix in ('remind:','progress:'))
   if r['active'] or pending:self.cancel_reminder_record(r,reason)
 def deliver_reminders(self):
  now=time.time()
  for candidate in self.rows('SELECT * FROM reminders WHERE active=1 AND due<=?',(now,)):
   audits=self.rows('SELECT * FROM reminder_deliveries WHERE reminder_id=? AND due=?',(candidate['id'],candidate['due']))
   if audits and (audits[0]['next_attempt_at'] or 0)>now:continue
   try:
    self.db.execute('BEGIN IMMEDIATE')
    r=self.one('SELECT * FROM reminders WHERE id=?',(candidate['id'],))
    if not r['active'] or r['due']!=candidate['due']:
     self.db.execute('COMMIT');continue
    if self.get('reminder-progress:'+r['id'])=='1':
     key=self.progress_event(r,self.worker(r['worker']))
    else:
     project=r['project'] or (self.rows('SELECT project FROM workers WHERE id=?',(r['worker'],)) or [{'project':None}])[0]['project']
     key='remind:'+r['id']+':'+str(int(r['due']))
     self.event(key,project,r['worker'],'check-in','Scheduled check-in: '+(r['note'] or 'review progress'))
    if key:
     # Verify persistence before consuming the occurrence, including a dedup hit.
     self.one('SELECT id FROM events WHERE event_key=?',(key,))
    self.reminder_audit(r,'delivered' if key else 'skipped',key,reason='Durable event queued; manager handling is tracked separately' if key else 'No eligible changed progress; no notice sent')
    if r['every']:
     due=max(r['due']+r['every'],time.time()+60)
     self.db.execute('UPDATE reminders SET due=? WHERE id=?',(due,r['id']))
     self.reminder_audit({**r,'due':due},'scheduled',reason='Next recurring occurrence')
    else:self.db.execute('UPDATE reminders SET active=0 WHERE id=?',(r['id'],))
    self.db.execute('COMMIT')
   except Exception as e:
    if self.db.in_transaction:self.db.execute('ROLLBACK')
    log.warning('reminder %s delivery failed; retained for retry: %s',candidate['id'],e)
    try:self.reminder_audit(candidate,'failed',error=str(e)[:1000],reason='Event/schedule transaction rolled back',next_attempt_at=time.time()+60)
    except Exception as audit_error:log.warning('reminder %s failure audit unavailable: %s',candidate['id'],audit_error)
 def goal_state(self,w,since):
  """Latest native goal status after an assignment was invoked: active, complete, paused, cleared..."""
  state=None
  if w['runtime']=='codex':
   msgs=self.api.call('/sessions/'+w['session']+'/messages?limit=100')['messages']
   for m in sorted(msgs,key=lambda m:m.get('seq',0)):
    data=content(m).get('content',{}).get('data',{})
    if not isinstance(data,dict) or m.get('createdAt',0)/1000<since-5:continue
    if data.get('type')=='thread_goal_updated':state=(data.get('goal') or {}).get('status') or state
    elif data.get('type')=='thread_goal_cleared':state='cleared'
  else:
   from datetime import datetime
   try:tail=self.dispatch({'op':'read_native','worker':w['id']})['tail']
   except (ValueError,OSError):return None
   for r in tail:
    att=r.get('attachment') or {}
    try:when=datetime.fromisoformat(r['timestamp'].replace('Z','+00:00')).timestamp()
    except (KeyError,ValueError,AttributeError):continue
    if att.get('type')=='goal_status' and when>=since-5:state='complete' if att.get('met') else 'active'
    text=(r.get('message') or {}).get('content')
    if when>=since-5 and isinstance(text,str) and '<command-name>/goal</command-name>' in text and re.search(r'<command-args>\s*clear\s*</command-args>',text):state='cleared'

  return state
 def user_restarted(self,w,s):
  """True (and the worker is released) when the user stopped and restarted this chat."""
  meta=s.get('metadata') or {};pid=meta.get('hostPid');key='host:'+w['id']
  if s.get('active') and isinstance(pid,int):
   prev=self.get(key)
   # A hub restart keeps the controller process; only a new process is a restart.
   # Node children stay managed until removed from the graph, even after a user restart.
   if prev and prev!=str(pid) and self.get('node-graph-active')=='1':
    self.set(key,pid);return False
   if prev and prev!=str(pid):
    self.release(w,'You stopped and restarted this chat yourself (new controller process); the manager no longer monitors it.');return True
   self.set(key,pid);return False
  if not s.get('active') and w['native_id']:
   for other in self.api.call('/sessions')['sessions']:
    if other['id']==w['session'] or not other.get('active') or (other.get('metadata') or {}).get('path') not in (None,w['path']):continue
    if native(self.session(other['id']))==w['native_id']:
     self.release(w,'You restarted this chat yourself as HAPI session '+other['id']+'; the manager no longer monitors it.');return True
  return False
 def release(self,w,detail):
  self.db.execute('BEGIN IMMEDIATE')
  try:
   self.db.execute("UPDATE workers SET state='released' WHERE id=?",(w['id'],))
   self.db.execute("UPDATE assignments SET state='released' WHERE worker=? AND state IN ('sent','sending','uncertain')",(w['id'],))
   self.event('released:'+w['id']+':'+str(int(time.time())),w['project'],w['id'],'released',detail)
   self.db.execute('COMMIT')
  except Exception:self.db.execute('ROLLBACK');raise
 def dispatch(self,o):
  op=o['op']
  if op=='background_intent':
   from native_background import set_intent
   return set_intent(self,o)
  import node_graph
  node_graph.actor_check(self,o)
  result=node_graph.dispatch(self,o)
  if result is not None:return result
  if op=='handoff_watches':return [json.loads(r['value']) for r in self.rows("SELECT value FROM settings WHERE key LIKE 'handoff-watch:%'")]
  if op=='handoff_watch':
   from coordination import watch
   return watch(self,o)
  if op=='resource_reservation':
   from resource_handoff import resource_reservation
   return resource_reservation(self,o)
  if op=='resource_handoff':
   from resource_handoff import resource_handoff
   return resource_handoff(self,o)
  if op=='resume_chat':
   if o.get('user_requested') is not True or not isinstance(o.get('basis'),str) or not o['basis'].strip() or not isinstance(o.get('requested_at'),(int,float)) or not 0<=time.time()-o['requested_at']<=300:raise ValueError('resume_chat requires a current explicit user request and quoted basis (fresh managerctl submission)')
   sid=self.worker(o['worker'])['session'] if o.get('worker') else o.get('session')
   matches=self.rows('SELECT * FROM workers WHERE session=?',(sid,))
   if not sid or (sid!=self.get('manager') and not matches):raise ValueError('Resume target must be the exact registered worker or manager session')
   if o.get('session') and o['session']!=sid:raise ValueError('Session binding changed')
   if matches and self.get('resume-pending:'+matches[0]['id']):
    if not self.reconcile_authorized_resume(matches[0]):raise ValueError('Previous authorized resume remains uncertain; no second controller requested')
    return {'type':'success','sessionId':self.worker(matches[0]['id'])['session'],'reconciled':True}
   observed=self.session(sid)
   if matches and matches[0]['native_id'] and native(observed) and matches[0]['native_id']!=native(observed):raise ValueError('Native identity mismatch; inspect before resume')
   # Only this current request can clear this exact chat. No assignment/watch,
   # project resume, automatic recovery or wake uses this operation.
   self.set('resume-authorization:'+sid,json.dumps({'basis':o['basis'].strip(),'at':time.time(),'state':'requested'}))
   self.api.call('/sessions/'+sid+'/manual-resume',{'explicitUserResume':True,'origin':'manager','basis':o['basis'].strip()})
   if o.get('reopen',True) and not observed.get('active'):
    if live_controller(observed):raise ValueError('Pause cleared; existing controller still present, do not create a duplicate')
    pending={'source':sid,'native':native(observed) or (matches[0]['native_id'] if matches else None),'launch_path':(observed.get('metadata') or {}).get('path'),'basis':o['basis'].strip(),'at':time.time(),**({'workspace_path':self.workspace(matches[0]),'source_machine':(observed.get('metadata') or {}).get('machineId')} if matches else {})}
    if matches and not pending['native']:raise ValueError('Native identity required before restarting a managed chat')
    if matches:self.set('resume-pending:'+matches[0]['id'],json.dumps(pending))
    result=self.api.call('/sessions/'+sid+'/resume',{})
    if result.get('type')!='success':raise ValueError('Pause cleared; resume failed or uncertain: '+str(result))
    if matches:
     pending['target']=result['sessionId'];self.set('resume-pending:'+matches[0]['id'],json.dumps(pending))
     if not self.reconcile_authorized_resume(matches[0]):raise ValueError('Resume accepted; identity/controller confirmation pending. Do not restart again.')
    elif sid==self.get('manager'):self.set('manager',result['sessionId'])
    return result
   return {'ok':True,'sessionId':sid,'active':observed.get('active'),'pauseCleared':True}
  if op=='unmanage':
   w=self.worker(o['worker'])
   if not o.get('session') or w['session']!=o['session']:raise ValueError('Exact current session binding required')
   self.end_management(w)
   if node_graph.active(self):
    # In the node graph, unmanaging a chat is removing its owner edge; the node stays lone.
    try:node_graph.remove_edge(self,w['session'])
    except Exception as e:log.warning('node edge removal %s: %s',w['id'],e)
    for key in ('node-edge-seen:','owner:','asleep:'):self.db.execute('DELETE FROM settings WHERE key=?',(key+w['id'],))
   self.publish_managed()
   return {'ok':True,'worker':w['id'],'session':w['session'],'state':'released'}
  if op=='retention':
   w=self.worker(o['worker']);policy=self.retention(w['id'])
   if 'mode' in o:
    if o['mode'] not in ('until-idle','keep'):raise ValueError('retention mode must be until-idle or keep')
    reason=str(o.get('reason','')).strip()
    if o['mode']=='keep' and not reason:raise ValueError('Keeping an idle chat managed requires a recorded reason')
    policy={'mode':o['mode'],'reason':reason[:500]}
    self.set('retention:'+w['id'],json.dumps(policy))
    self.db.execute('DELETE FROM settings WHERE key=?',('retirement-candidate:'+w['id'],))
   return {'worker':w['id'],**policy}
  if op in ('assign','steer_assignment','launch','attach','adopt','answer','resume','remind'):
   candidates=[self.worker(o['worker'])] if o.get('worker') else self.rows('SELECT * FROM workers WHERE session=?',(o.get('session'),))
   if any(self.get('unmanaged:'+w['id']) for w in candidates):raise ValueError('Chat explicitly unmanaged; use manage with current user authorization first')
   if self.manager_paused():raise ValueError('Manager manually paused; explicit user resume in HAPI is required')
   if o.get('worker'):
    sid=self.worker(o['worker'])['session']
    if sid and self.session(sid).get('manualPausedAt') is not None:raise ValueError('Worker manually paused; explicit user resume in HAPI is required')
  if op=='supersede_assignment':
   from assignment_reconciliation import supersede_assignment
   return supersede_assignment(self,o)
  if op in ('assignment_delivery','steer_assignment'):
   from assignment_delivery import inspect_assignment, promote_assignment
   if not o.get('worker') or not o.get('assignment'):raise ValueError('Exact worker and assignment required')
   w=self.worker(o['worker']);a=self.one('SELECT * FROM assignments WHERE id=?',(o['assignment'],))
   if a['worker']!=w['id']:raise ValueError('Assignment does not belong to the requested worker')
   return promote_assignment(self,o) if op=='steer_assignment' else inspect_assignment(self,w,a,historical=o.get('historical') is True)
  if op=='status':
   result={t:self.rows('SELECT * FROM '+t) for t in ('projects','workers','assignments','resources','events','outbox','usage_recovery','transient_recovery')}
   from resource_handoff import personal_reservation
   for resource in result['resources']:
    reservation=personal_reservation(self,resource['name'])
    if reservation:resource['reservation']=reservation
   result['handoff_watches']=self.dispatch({'op':'handoff_watches'})
   result['reminders']=self.dispatch({'op':'reminders'})
   for w in result['workers']:w['workspace']=self.workspace(w)
   return result
  if op=='cancel_queued':
   if not o.get('assignment'):
    from queued_input import cancel_reviewed
    return cancel_reviewed(self,o)
   a=self.one('SELECT * FROM assignments WHERE id=?',(o['assignment'],));w=self.worker(a['worker'])
   if not w['session']:raise ValueError('Worker has no bound session')
   result=self.api.call('/sessions/'+w['session']+'/messages/'+a['id'],method='DELETE')
   if result.get('status')=='cancelled':self.db.execute("UPDATE assignments SET state='cancelled' WHERE id=?",(a['id'],))
   return result
  if op=='queued_input':
   from queued_input import review
   return review(self,o)
  if op=='native_queue':
   from queued_input import native_queue
   return native_queue(self,o)
  if op=='steer_queued':
   from queued_input import steer_reviewed
   return steer_reviewed(self,o)
  if op=='workspace':
   w=self.worker(o['worker']);path=Path(o['path']).expanduser()
   if not path.is_absolute() or not path.is_dir():raise ValueError('Workspace must be an existing absolute directory')
   if self.rows("SELECT id FROM assignments WHERE worker=? AND state IN ('sent','sending','uncertain') AND COALESCE(scope,'checkout')='checkout'",(w['id'],)):raise ValueError('Unresolved checkout assignment; cannot move its workspace lease')
   if w['session']:
    session=self.session(w['session'])
    if session.get('thinking') or (session.get('agentState') or {}).get('requests') or (session.get('agentState') or {}).get('controlledByUser'):raise ValueError('Worker busy or awaiting approval; cannot change workspace')
   self.set('workspace:'+w['id'],str(path.resolve()))
   return {'worker':w['id'],'workspace':self.workspace(w),'session_cwd':w['path']}
  if op=='permissions':
   w=self.worker(o['worker']);profile=o['permission'];mode=self.permission_mode(w['runtime'],profile)
   if w['session']:self.api.call('/sessions/'+w['session']+'/permission-mode',{'mode':mode})
   self.set('worker_permission:'+w['id'],profile)
   return {'worker':w['id'],'permission':profile,'mode':mode}
  if op=='sessions':return self.api.call('/sessions')
  if op=='discover':return self.discover(o['runtime'])
  if op=='inspect_native_history':return self.inspect_native_history(self.worker(o['worker']))
  if op=='read_native':
   w=self.worker(o['worker'])
   path=self.native_path(w)
   from collections import deque
   with path.open() as f:lines=deque(f,maxlen=100)
   return {'native_id':w['native_id'],'path':str(path),'tail':[clip(json.loads(x)) for x in lines]}
  if op=='read':return {'session':self.session(o['session']),'messages':self.api.call('/sessions/'+o['session']+'/messages?limit=100')}
  if op=='events':return self.rows('SELECT * FROM events WHERE acked=0')
  if op=='ack':
   for i in o['ids']:self.db.execute('UPDATE events SET acked=1 WHERE id=?',(i,))
   return {'ok':True}
  if op=='register':
   path=Path(o['repo']).resolve(strict=True)
   if not path.is_dir():raise ValueError('Repository must be a directory')
   p=uid();self.db.execute('INSERT INTO projects(id,name,repo,objective) VALUES(?,?,?,?)',(p,o['name'],str(path),o.get('objective','')));return self.project(p)
  if op in ('update','pause','resume','complete','claim','release'):
   p=self.project(o['project'])
   if op in ('claim','release'):
    from resource_handoff import assert_resource_available
    assert_resource_available(self,o['resource'])
   if op=='update':
    for k in ('objective','next_steps','blockers','checkpoint','integration_owner'):
     if k in o:
      self.db.execute(f'UPDATE projects SET {k}=? WHERE id=?',(o[k],p['id']))
      if k=='objective' and o[k]!=p[k]:self.db.execute('UPDATE projects SET budget=8 WHERE id=?',(p['id'],))
   elif op in ('pause','resume','complete'):
    if op=='complete' and self.rows("SELECT a.id FROM assignments a JOIN workers w ON w.id=a.worker WHERE w.project=? AND a.state IN ('sent','sending','uncertain')",(p['id'],)):raise ValueError('Workers still have unresolved assignments')
    # Only an explicit resume returns control from a manual hold.
    self.db.execute('UPDATE projects SET status=?,manual=CASE WHEN ? THEN 0 ELSE manual END WHERE id=?',({'pause':'paused','resume':'running','complete':'completed'}[op],op=='resume',p['id']))
    # A completed objective no longer needs its machines/GPUs.
    if op=='complete':self.db.execute('DELETE FROM resources WHERE project=?',(p['id'],))
    if 'checkpoint' in o:self.db.execute('UPDATE projects SET checkpoint=? WHERE id=?',(o['checkpoint'],p['id']))
   elif op=='claim':
    existing=self.rows('SELECT * FROM resources WHERE name=?',(o['resource'],))
    if existing and existing[0]['project']!=p['id']:raise ValueError('Resource owned by '+existing[0]['project'])
    self.db.execute('INSERT OR IGNORE INTO resources VALUES(?,?)',(o['resource'],p['id']))
   else:
    if self.rows("SELECT a.id FROM assignments a JOIN workers w ON w.id=a.worker WHERE w.project=? AND a.state IN ('sent','sending','uncertain')",(p['id'],)):raise ValueError('Wait for current assignments before releasing resource; for a GPU handoff preserving goals, use resource_handoff with reviewed worker evidence')
    self.db.execute('DELETE FROM resources WHERE name=? AND project=?',(o['resource'],p['id']))
   return self.project(p['id'])
  if op in ('launch','attach'):
   p=self.project(o['project']);rt=o['runtime']
   if rt not in ('codex','claude'):raise ValueError('Unsupported runtime')
   w=uid();path=p['repo'];nid=o.get('native_id')
   profile=o.get('permission','full');permission_mode=self.permission_mode(rt,profile)
   self.set('worker_permission:'+w,profile)
   if op=='attach':
    uuid.UUID(nid)
    if self.rows('SELECT id FROM workers WHERE runtime=? AND native_id=?',(rt,nid)):raise ValueError('Native conversation is already registered; reuse its worker ID')
    if rt=='codex':
     c=sqlite3.connect('file:'+str(Path.home()/'.codex/state_5.sqlite')+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
     matches=[dict(x) for x in c.execute('SELECT id,cwd FROM threads WHERE id=?',(nid,))];c.close()
    else:
     matches=[]
     for file in (Path.home()/'.claude/projects').glob('*/'+nid+'.jsonl'):
      with file.open() as f:
       for line in f:
        record=json.loads(line)
        if record.get('cwd'):matches.append({'id':nid,'cwd':record['cwd']});break
    if not matches:raise ValueError('Exact native session ID not found in local inventory')
    if len(matches)!=1:raise ValueError('Native history is ambiguous; reconcile duplicate histories first')
    original_cwd=Path(matches[0]['cwd']).resolve()
    if original_cwd!=Path(path).resolve():
     # Keep the original resume location; project repo and session cwd are distinct.
     # No relocation, rewritten transcript, or implicit mismatch bypass.
     if rt!='claude' or not o.get('session_cwd') or Path(o['session_cwd']).resolve()!=original_cwd:
      raise ValueError('Native session repository mismatch; for Claude supply verified session_cwd to retain original history location')
     if not original_cwd.is_dir():raise ValueError('Original session directory no longer exists')
     path=str(original_cwd)
    if not o.get('user_requested'):
     # Chats the user opened in HAPI are theirs; take one over only when the user asks.
     for other in self.api.call('/sessions')['sessions']:
      if (other.get('metadata') or {}).get('path') not in (None,str(original_cwd)):continue
      if native(self.session(other['id']))==nid:
       raise ValueError('This conversation is a chat the user opened in HAPI ('+other['id']+'). Attach it only on the user\'s explicit instruction, with "user_requested": true.')
    self.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',(w,p['id'],rt,nid,None,path,'registered',time.time()));return self.worker(w)
   self.running(p['id'])
   if self.rows("SELECT id FROM workers WHERE project=? AND state IN ('launching','adopting')",(p['id'],)):raise ValueError('Unresolved launch/adoption exists; reconcile before launching another worker')
   if o.get('worktree'):
    if not p['integration_owner']:raise ValueError('Set integration_owner before parallel worktrees')
    path=str(ROOT/'state/worktrees'/w);Path(path).parent.mkdir(exist_ok=True)
    subprocess.run(['git','-C',p['repo'],'worktree','add','-b','manager/'+w,path],check=True,capture_output=True)
   elif self.rows("SELECT id FROM workers WHERE path=? AND state!='registered'",(path,)):raise ValueError('Existing worker owns this checkout; use a worktree')
   machines=self.api.call('/machines')['machines']
   if len(machines)!=1:raise ValueError('Expected exactly one local runner')
   self.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)',(w,p['id'],rt,None,None,path,'launching',time.time()))
   result=self.api.call('/machines/'+machines[0]['id']+'/spawn',{'directory':path,'agent':rt,'startingMode':'remote','permissionMode':permission_mode})
   if result.get('type')!='success':
    self.db.execute("UPDATE workers SET state='launch-failed' WHERE id=?",(w,));raise ValueError(str(result))
   sid=result['sessionId'];self.db.execute("UPDATE workers SET session=?,state='managed' WHERE id=?",(sid,w))
   self.api.call('/sessions/'+sid,{'name':p['name']+' · '+rt},'PATCH');return self.worker(w)
  if op=='reconcile_inactive':return self.reconcile_inactive(o['worker'],o.get('reason',''))
  if op=='adopt':return self.adopt(o['worker'])
  if op=='reporting':
   wid=o.get('worker')
   if wid:self.worker(wid)
   key='reporting:'+wid if wid else 'reporting-default'
   value=json.loads(self.get(key) or '{}')
   if o.get('reset') and wid:value={}
   for field in ('enabled','important'):
    if field in o:
     if not isinstance(o[field],bool):raise ValueError(field+' must be boolean')
     value[field]=o[field]
   if 'fallback_minutes' in o:
    minutes=float(o['fallback_minutes'])
    if not math.isfinite(minutes) or minutes<5:raise ValueError('fallback_minutes must be finite and at least 5')
    value['fallback_minutes']=minutes
   if o.get('replace_reminder'):
    if not wid:raise ValueError('replace_reminder requires worker')
    r=self.one('SELECT * FROM reminders WHERE id=?',(o['replace_reminder'],))
    if r['worker']!=wid or not r['active']:raise ValueError('Expected active reminder for this worker')
    if not r['every'] or r['every']!=60*value.get('fallback_minutes',self.reporting(wid)['fallback_minutes']):raise ValueError('Preserve the selected reminder cadence')
    self.db.execute('BEGIN IMMEDIATE')
    try:
     self.cancel_reminder_record(r,'Replaced by reporting fallback configuration')
     # Preserve the next scheduled deadline, then reset only when a report is delivered.
     self.set('reporting-last:'+wid,r['due']-r['every'])
     self.set(key,json.dumps(value));self.db.execute('COMMIT')
    except Exception:self.db.execute('ROLLBACK');raise
   else:self.set(key,json.dumps(value))
   return {'worker':wid,'effective':self.reporting(wid),'override':value if wid else None,'replacedReminder':o.get('replace_reminder')}

  if op=='watch':
   # How closely the manager follows a chat, per the user's wishes: events (default),
   # quiet (tracked; no automatic wakes; report when asked), off (not scanned at all).
   ws=[self.worker(o['worker'])] if o.get('worker') else self.rows('SELECT * FROM workers WHERE project=?',(self.project(o['project'])['id'],))
   mode=o.get('mode','events')
   if mode not in ('events','quiet','off'):raise ValueError('mode must be events, quiet or off')
   for w in ws:
    if self.watch(w['id'])['mode']!=mode:self.db.execute('DELETE FROM settings WHERE key=?',('observation:'+w['id'],))
    self.set('watch:'+w['id'],json.dumps({'mode':mode,'note':str(o.get('note',''))[:500]}))
   return [{'worker':w['id'],**self.watch(w['id'])} for w in ws]
  if op=='remind':
   every=60*float(o['every_minutes']) if o.get('every_minutes') else None
   if every is not None and (not math.isfinite(every) or every<5*60):raise ValueError('every_minutes must be at least 5 and finite')
   old=self.one('SELECT * FROM reminders WHERE id=?',(o['id'],)) if o.get('id') else None
   if o.get('progress'):
    w=self.worker(o['worker'])
    if not w['session']:raise ValueError('Progress requires an existing HAPI session; use attach, not adopt')
    if old and old['worker']!=w['id']:raise ValueError('Reminder worker mismatch')
    # One progress schedule per worker, including retries of the configuration command.
    existing=self.rows("SELECT r.* FROM reminders r JOIN settings s ON s.key='reminder-progress:'||r.id WHERE r.worker=? AND r.active=1 AND s.value='1'",(w['id'],))
    if existing and old and existing[0]['id']!=old['id']:raise ValueError('Worker already has a progress reminder')
    if not old and existing:old=existing[0]
   due=time.time()+60*float(o['in_minutes']) if 'in_minutes' in o else float(o['at']) if 'at' in o else old['due'] if old else time.time()+(every or 0)
   if not math.isfinite(due):raise ValueError('Reminder time must be finite')
   rid=old['id'] if old else uid()
   worker=o.get('worker',old['worker'] if old else None);project=o.get('project',old['project'] if old else None)
   note=str(o.get('note',old['note'] if old else ''))[:500]
   if old and 'every_minutes' not in o:every=old['every']
   self.db.execute('BEGIN IMMEDIATE')
   try:
    if old and old['due']!=due:self.cancel_reminder_record(old,'Schedule replaced by a new explicit reminder request')
    if old:self.db.execute('UPDATE reminders SET due=?,every=?,worker=?,project=?,note=?,active=1 WHERE id=?',(due,every,worker,project,note,rid))
    else:self.db.execute('INSERT INTO reminders VALUES(?,?,?,?,?,?,1)',(rid,due,every,worker,project,note))
    if 'progress' in o:self.set('reminder-progress:'+rid,'1' if o['progress'] else '0')
    current=self.one('SELECT * FROM reminders WHERE id=?',(rid,))
    self.reminder_audit(current,'scheduled',reason='Explicit reminder request')
    self.db.execute('COMMIT')
   except Exception:self.db.execute('ROLLBACK');raise
   return self.reminder_info(current)
  if op=='reminders':
   conditions=[];args=[]
   if not o.get('include_inactive'):conditions.append('active=1')
   if o.get('id'):conditions.append('id=?');args.append(o['id'])
   query='SELECT * FROM reminders'+(' WHERE '+' AND '.join(conditions) if conditions else '')+' ORDER BY due'
   return [self.reminder_info(r,o.get('history',False)) for r in self.rows(query,args)]
  if op=='cancel_reminder':
   r=self.one('SELECT * FROM reminders WHERE id=?',(o['id'],))
   self.db.execute('BEGIN IMMEDIATE')
   try:
    self.cancel_reminder_record(r,'Explicit cancel_reminder request')
    self.db.execute('COMMIT')
   except Exception:self.db.execute('ROLLBACK');raise
   return {'ok':True,'reminder':self.reminder_info(self.one('SELECT * FROM reminders WHERE id=?',(r['id'],)))}
  if op in ('requests','answer'):
   sid=self.worker(o['worker'])['session'] if o.get('worker') else o['session']
   pending=(self.session(sid).get('agentState') or {}).get('requests') or {}
   if op=='requests':return [{'id':rid,'tool':r.get('tool'),'summary':summarize_request(rid,r),'questions':request_questions(r)} for rid,r in pending.items()]
   request=pending.get(o['request'])
   if request is None:raise ValueError('Request is no longer pending (answered in the chat, or expired)')
   if not str(o.get('basis','')).strip():raise ValueError('basis required: quote the user instruction or delegation this answer follows')
   tool=request.get('tool') or ''
   if 'answers' in o:
    qs=request_questions(request);given=o['answers']
    if isinstance(given,list):
     if len(given)!=len(qs):raise ValueError('Provide one answer list per question (%d)'%len(qs))
     given={q['key']:(a if isinstance(a,list) else [a]) for q,a in zip(qs,given)}
     # Codex request_user_input uses nested answers keyed by question id.
     if tool=='request_user_input':given={k:{'answers':v} for k,v in given.items()}
    body={'answers':given}
   else:
    decision=o.get('decision','approved')
    if decision not in ('approved','approved_for_session','denied','abort'):raise ValueError('decision must be approved, approved_for_session, denied or abort')
    body={'decision':decision}
   path='/sessions/'+sid+'/permissions/'+o['request']+('/deny' if body.get('decision') in ('denied','abort') else '/approve')
   result=self.api.call(path,body)
   w=self.rows('SELECT * FROM workers WHERE session=?',(sid,))
   self.event('answered:'+o['request'],w[0]['project'] if w else None,w[0]['id'] if w else None,'answered','Manager answered '+summarize_request(o['request'],request)[:200]+' per: '+str(o['basis'])[:200])
   self.db.execute('UPDATE events SET acked=1 WHERE event_key=?',('answered:'+o['request'],))
   return {'ok':True,'result':result}
  if op=='digest':
   # On-demand update for quiet/hands-off chats: pending events plus the latest reply.
   w=self.worker(o['worker'])
   evs=self.rows('SELECT * FROM events WHERE worker=? AND acked=0 ORDER BY id',(w['id'],))
   s=self.session(w['session']) if w['session'] else {}
   msgs=self.api.call('/sessions/'+w['session']+'/messages?limit=100')['messages'] if w['session'] else []
   progress=self.progress_snapshot(w,s,msgs,reply_limit=1500) if w['session'] else None
   from assignment_delivery import summarize_inputs
   return {'worker':w['id'],'watch':self.watch(w['id']),'active':s.get('active'),'thinking':s.get('thinking'),'pending_requests':len((s.get('agentState') or {}).get('requests') or {}),'events':evs,'latest_reply':progress['latest_reply'] if progress else None,'progress':progress,'input_delivery':summarize_inputs(msgs)}
  if op=='manage':
   # Registry-only monitoring. Never resumes, sends input, clears goals or holds.
   w=self.worker(o['worker'])
   if w['state'] not in ('managed','released'):raise ValueError('Worker must already have a managed or released session')
   if not w['session']:raise ValueError('No bound session to observe')
   if self.get('unmanaged:'+w['id']) and (o.get('user_requested') is not True or o.get('resume_management') is not True):raise ValueError('Explicit current user request with resume_management required')
   observed=self.session(w['session'])
   if w['native_id'] and native(observed) and native(observed)!=w['native_id']:raise ValueError('Native identity mismatch')
   if self.get('unmanaged:'+w['id']):
    self.db.execute('DELETE FROM settings WHERE key=?',('retirement-notify:'+w['id'],))
    self.set('watch:'+w['id'],self.get('unmanaged-watch:'+w['id']) or json.dumps({'mode':'events','note':''}))
    self.db.execute('DELETE FROM settings WHERE key=?',('unmanaged:'+w['id'],))
   if w['state']=='released':
    self.db.execute('DELETE FROM settings WHERE key=?',('observation:'+w['id'],))
    self.db.execute("UPDATE workers SET state='managed' WHERE id=?",(w['id'],))
    self.db.execute('DELETE FROM settings WHERE key=?',('host:'+w['id'],))
   if node_graph.active(self) and w['session']!=self.get('manager'):
    # Managing a chat in the node graph is an owner edge (overseer unless one is named).
    parent=o.get('owner') or self.get('manager')
    node_graph.set_edge(self,parent,w['session'],o.get('actor') or 'overseer')
    self.set('node-edge-seen:'+w['id'],'1');self.set('owner:'+w['id'],parent)
   return {**self.worker(w['id']),'observationOnly':True,'projectStatus':self.project(w['project'])['status'],'watch':self.watch(w['id'])}
  if op=='stop_cleared_controller':return self.stop_cleared_controller(o['worker'],o['session'])
  if op=='reconcile_adopt':return self.reconcile_adopt(o['worker'],bool(o.get('rebind')))
  if op in ('assign','interrupt'):
   w=self.worker(o['worker'])
   if not w['session']:raise ValueError('Worker registered only; adopt after external controller stops')
   # user_requested: the user explicitly asked for this message. Guardrails that exist to
   # keep *autonomous* work in check (pause/hold, budget, busy, prior unresolved task) yield;
   # dedupe keys, disconnection and same-checkout protection never do.
   ur=bool(o.get('user_requested'))
   if w['state']=='released' and not ur:raise ValueError('The user stopped and restarted this chat; it is no longer managed. Message it only when the user asks (user_requested), or use manage on their instruction.')
   if op=='interrupt':
    if o.get('pause',True):self.db.execute("UPDATE projects SET status='paused' WHERE id=?",(w['project'],))
    return self.api.call('/sessions/'+w['session']+'/abort',{})
   old=self.rows('SELECT * FROM assignments WHERE task_key=?',(o['key'],))
   if old:
    from assignment_delivery import inspect_assignment
    try:delivery=inspect_assignment(self,w,old[0])
    except Exception:delivery={'state':'unconfirmed','note':'Delivery inspection unavailable; do not resend'}
    return {**old[0],'delivery':delivery}
   s=self.session(w['session']) if w['state']=='released' else self.scan(w)
   if s is None:raise ValueError('Worker observation or authorized resume confirmation is pending; inspect the exact native/controller binding before retrying. No assignment sent.')
   p=self.project(w['project']) if ur else self.running(w['project'])
   import transient_recovery
   if not ur and transient_recovery.blocked(self,w['session']):raise ValueError('Transient provider recovery pending; do not send a competing retry')
   if not ur and self.rows("SELECT worker FROM usage_recovery WHERE worker=? AND state NOT IN ('done','cancelled')",(w['id'],)):raise ValueError('Usage recovery pending; supervisor will continue after reset')
   manager=self.get('manager')
   if manager and not ur:
    for m in self.api.call('/sessions/'+manager+'/messages?limit=100')['messages']:
     if is_instruction_message(m) and not m.get('invokedAt') and not self.db.execute('SELECT 1 FROM outbox WHERE id=?',(m.get('localId'),)).fetchone():
      raise ValueError('New user instruction is queued for manager; process it before automatic assignments')
   scope=o.get('scope','checkout')
   if scope not in ('checkout','external'):raise ValueError('scope must be checkout or external')
   # External-only work (email/browser) neither takes nor is blocked by a checkout;
   # checkout work still excludes other unresolved checkout work at the same path.
   if scope=='checkout' and self.checkout_busy(w):raise ValueError('Another worker is using this checkout; use a separate worktree, or scope external for work that does not access it')
   if not s.get('active'):raise ValueError('Disconnected worker; reconcile and resume explicitly')
   if not ur:
    if p['budget']<=0:raise ValueError('Autonomous budget reached. Report checkpoint and request renewed scope (or send with user_requested when the user asked).')
    if s.get('thinking') or (s.get('agentState') or {}).get('controlledByUser') or (s.get('agentState') or {}).get('requests'):raise ValueError('Worker busy, controlled externally, or awaiting approval (user_requested queues it after the current turn)')
    if self.rows("SELECT id FROM assignments WHERE worker=? AND state IN ('sending','sent','uncertain')",(w['id'],)):raise ValueError('Unresolved assignment exists; never duplicate')
    if len(self.rows("SELECT id FROM assignments WHERE worker=? AND state='failed'",(w['id'],)))>=3:raise ValueError('Repeated failures; report blocker')
   aid=uid();text=o['text'];goal=bool(o.get('goal'))
   mode=o.get('delivery_mode','queue')
   if mode not in ('queue','steer','next'):raise ValueError('delivery_mode must be queue, steer or next')
   if mode!='queue' and w['runtime']=='claude' and ((s.get('metadata') or {}).get('capabilities') or {}).get('claudeInputPriorities') is not True:raise ValueError('Claude controller lacks native priority delivery; refusing legacy interrupt fallback')
   if mode=='next' and w['runtime']!='claude':raise ValueError('next is supported only for Claude; Codex uses queue or steer')
   if mode!='queue' and w['runtime'] not in ('codex','claude'):raise ValueError('Priority delivery is not supported for this runtime')
   # Native slash commands are control payloads. Appending context turns
   # `/goal clear` into a new goal and can start an expensive stop-hook loop.
   command=bool(re.match(r'^/[A-Za-z][\w-]*(?:\s|$)',text.lstrip()))
   if mode!='queue' and (command or goal):raise ValueError('Slash/goal commands update control metadata; send a separate actual prompt to steer a busy turn')
   if command and goal:raise ValueError('goal=true requires an objective, not a slash command; send the command with goal=false')
   if not command:
    claims=self.rows('SELECT name FROM resources WHERE project=?',(p['id'],))
    if claims:text+='\nReserved for this project: '+', '.join(c['name'] for c in claims)
    from resource_handoff import personal_reservation
    personal=[r['name'] for r in self.rows('SELECT name FROM resources WHERE project IS NULL') if personal_reservation(self,r['name'])]
    if personal:text+='\nPersonally reserved by user; do not use: '+', '.join(personal)
    if scope=='checkout' and self.workspace(w)!=str(Path(w['path']).resolve()):text+='\nWorking checkout: '+self.workspace(w)+' (the original session launch directory is preserved for history only).'
    if scope=='external':text+="\nExternal-only: another chat is editing "+w['path']+"; don't change files there (ask first if you need to)."
   if goal:
    # Native /goal keeps working across turns; the supervisor waits for its completion.
    text='/goal '+text
    if len(text)>4000:raise ValueError('Goal objective too long (4000 characters max); shorten the task text')
   self.db.execute('INSERT INTO assignments(id,worker,task_key,text,state,created,scope,goal) VALUES(?,?,?,?,?,?,?,?)',(aid,w['id'],o['key'],text,'sending',time.time(),scope,int(goal)))
   if not ur:self.db.execute('UPDATE projects SET budget=budget-1 WHERE id=?',(p['id'],))
   self.set('assignment-input:'+aid,json.dumps({'worker':w['id'],'session':w['session'],'native_id':w['native_id'],'priority':mode,'controlOnly':bool(goal or re.match(r'^/goal(?:\s|$)',text.lstrip(),re.I))}))
   try:accepted=self.api.call('/sessions/'+w['session']+'/messages',{'text':text,'localId':aid,'deliveryMode':mode})
   except Exception:
    self.db.execute("UPDATE assignments SET state='uncertain' WHERE id=?",(aid,));raise
   # Released chats are not scanned, so their delivery is not tracked further.
   self.db.execute('UPDATE assignments SET state=? WHERE id=?',('untracked' if w['state']=='released' else 'sent',aid))
   result=self.one('SELECT * FROM assignments WHERE id=?',(aid,))
   from assignment_delivery import inspect_assignment
   try:result['delivery']=inspect_assignment(self,w,result)
   except Exception:result['delivery']={'state':'unconfirmed','note':'Saved; delivery inspection unavailable. Do not resend blindly.'}
   if accepted.get('deliveryWarning'):result['deliveryWarning']=accepted['deliveryWarning']
   # Long instructions tend to carry the manager's own assumptions as hard rules.
   if len(o['text'])>LONG_INSTRUCTION:result['warning']='Instruction is %d characters. Keep worker instructions short: the user\'s request in their words plus needed context; label your own ideas as optional.'%len(o['text'])
   return result
  raise ValueError('Unknown operation')
 def reconcile_inactive(self,wid,reason):
  if not isinstance(reason,str) or not reason.strip():raise ValueError('A reviewed recovery reason is required')
  w=self.worker(wid);s=self.session(w['session'])
  if s.get('active') or s.get('thinking') or live_controller(s):raise ValueError('Worker still active or has a controller')
  if not w['native_id'] or native(s)!=w['native_id']:raise ValueError('Exact native identity must match before reconciliation')
  for row in self.api.call('/sessions')['sessions']:
   other=self.session(row['id'])
   if native(other)==w['native_id'] and (other.get('active') or live_controller(other)):raise ValueError('Matching native controller exists')
  for proc in Path('/proc').glob('[0-9]*'):
   try:
    cmd=(proc/'cmdline').read_bytes()
    if w['native_id'].encode() in cmd and (b'codex' in cmd or b'hapi' in cmd or b'claude' in cmd):raise ValueError('Matching native process exists')
   except (FileNotFoundError,PermissionError,ProcessLookupError):pass
  if w['runtime']=='codex':
   lockpath=Path.home()/'.codex/thread-writer-locks'/(w['native_id']+'.lock')
   if lockpath.exists():
    with lockpath.open('r') as f:
     try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
     except BlockingIOError:raise ValueError('Native session writer lock is held')
     fcntl.flock(f,fcntl.LOCK_UN)
  assignments=self.rows("SELECT * FROM assignments WHERE worker=? AND state IN ('sent','sending','uncertain')",(wid,))
  if any(a['state']!='sent' or not a['seen_busy'] for a in assignments):raise ValueError('Unconfirmed dispatch needs manual delivery reconciliation')
  if assignments:
   state=self.api.call('/sessions/'+w['session']+'/messages/queued-state',{'localIds':[a['id'] for a in assignments]})
   if not isinstance(state.get('queuedLocalIds'),list) or not isinstance(state.get('indeterminateLocalIds'),list):raise ValueError('Queue delivery state unavailable')
   if state.get('queuedLocalIds') or state.get('indeterminateLocalIds'):raise ValueError('Assignments remain queued or delivery is indeterminate')
  for a in assignments:self.finish(a,w,'interrupted','Controller exited; outcome not assumed complete. No replay. Recovery review: '+reason)
  return {'worker':wid,'reconciled':[a['id'] for a in assignments],'state':'interrupted','resumed':False}
 def inspect_native_history(self,w):
  if w['runtime']!='codex':raise ValueError('Native paginated history checks apply to Codex')
  try:
   result=subprocess.run([str(Path.home()/'.bun/bin/bun'),str(ROOT/'deploy/check-native-history.ts'),w['native_id']],capture_output=True,text=True,timeout=30)
   report=json.loads(result.stdout)
  except (subprocess.TimeoutExpired,ValueError,OSError) as error:raise ValueError('Native history check unavailable; no controller started: '+str(error)) from error
  if report.get('nativeId')!=w['native_id'] or report.get('status') not in ('ready','blocked'):raise ValueError('Native history inspection returned no verified identity; no controller started')
  return report
 def require_native_history(self,w):
  if w['runtime']!='codex':return
  with self.native_path(w).open() as file:
   line=file.readline(2*1024*1024)
  try:metadata=json.loads(line).get('payload') or {}
  except ValueError:return  # Preserve existing legacy path; it has no paginated lineage to validate.
  if metadata.get('history_mode')!='paginated':return
  report=self.inspect_native_history(w)
  if report['status']!='ready':raise ValueError('Native history blocked before adoption: '+report.get('error','unknown lineage failure')+'; restore original lineage. No controller started.')
 def adopt(self,wid):
  w=self.worker(wid)
  if w['session']:
   s=self.session(w['session'])
   if s.get('active'):return w
   if live_controller(s):raise ValueError('Existing session controller still runs but is disconnected; restore its connection before resuming')
   if self.rows("SELECT id FROM assignments WHERE worker=? AND state IN ('sent','sending','uncertain')",(wid,)):raise ValueError('Unresolved assignment; inspect process and history before any resume')
   self.require_native_history(w)
   self.db.execute('DELETE FROM settings WHERE key=?',('host:'+wid,))
   result=self.api.call('/sessions/'+w['session']+'/resume',{})
   if result.get('type')!='success':raise ValueError(str(result))
   self.db.execute('UPDATE workers SET session=? WHERE id=?',(result['sessionId'],wid));return self.worker(wid)
  if w['state']=='adopting':raise ValueError('Adoption outcome unresolved; reconcile existing process before retry (reconcile_adopt)')
  unit='manager-adopt-'+wid;previous=self.unit_state(unit)
  if previous.get('LoadState')=='loaded':
   # Only a conclusively dead earlier attempt may release its unit name.
   if previous.get('ActiveState')!='failed' or previous.get('MainPID','0')!='0':raise ValueError('Earlier adoption service '+unit+' is still loaded; inspect it before retry')
   subprocess.run(['systemctl','--user','reset-failed',unit],check=True,capture_output=True)
  managed_claude_pids=set()
  # HAPI chats can exist outside the project registry. Known controllers for
  # OTHER native IDs are not competing writers for this conversation.
  for other in self.api.call('/sessions')['sessions']:
   info=self.session(other['id']);metadata=info.get('metadata') or {}
   if native(info)==w['native_id'] and (info.get('active') or live_controller(info)):
    raise ValueError('This native conversation already has a HAPI controller; reuse that session rather than starting another')
   pid=metadata.get('hostPid')
   if metadata.get('flavor')=='claude' and native(info) and pid:
    managed_claude_pids.add(pid)
  # Conservatively refuse native adoption while any unrelated controller exists for runtime.
  # This intentionally favors history preservation over guessing ownership from terminals.
  for proc in Path('/proc').glob('[0-9]*'):
   try:
    cmd=(proc/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
    exe=(proc/'exe').resolve().name
    comm=(proc/'comm').read_text().strip()
    if w['runtime']=='claude' and (comm=='claude' or exe.startswith('claude')):
     ancestor=int(proc.name);owned=False
     for _ in range(12):
      if ancestor in managed_claude_pids:owned=True;break
      if ancestor<=1:break
      status=(Path('/proc')/str(ancestor)/'status').read_text()
      ancestor=int(next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:')))
     if not owned:raise ValueError('External Claude controller is running; close it before adopting a native session')
    if w['runtime']=='codex' and exe=='codex' and w['native_id'] in cmd:raise ValueError('Native Codex controller for this ID is running; close it before adoption')
   except (FileNotFoundError,PermissionError,ProcessLookupError):pass
  if w['runtime']=='codex':
   lockpath=Path.home()/'.codex/thread-writer-locks'/(w['native_id']+'.lock')
   if lockpath.exists():
    with lockpath.open('r') as f:
     try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
     except BlockingIOError:raise ValueError('Native session writer lock is held; close original controller before adoption')
     fcntl.flock(f,fcntl.LOCK_UN)
  self.require_native_history(w)
  args=[str(ROOT/'vendor/hapi'),w['runtime']];mode=['--permission-mode',self.permission_mode(w['runtime'],self.worker_permission(w))]
  # Codex: the shared runtime's terminal owner requires a TTY. Runner tag selects
  # the headless owner HAPI's own runner uses for resume; the runner never kills
  # unknown shared roots (concurrentClients). Claude keeps terminal: the runner
  # would terminate an unknown runner-tagged Claude child.
  # HAPI codex only parses `resume ID` as the first argument; later it starts a new thread.
  if w['runtime']=='codex':args+=['resume',w['native_id'],'--started-by','runner',*mode]
  else:args+=[*mode,'--resume',w['native_id'],'--started-by','terminal','--hapi-starting-mode','remote']
  if w['runtime']=='codex':self.set('native_history:'+wid,json.dumps(self.codex_prompt_ids(w)))
  self.db.execute('DELETE FROM settings WHERE key=?',('host:'+wid,))
  self.db.execute("UPDATE workers SET state='adopting',created=? WHERE id=?",(time.time(),wid))
  try:subprocess.run(['systemd-run','--user','--unit='+unit,'--property=WorkingDirectory='+w['path'],'--setenv=PATH='+str(Path.home()/'.local/bin')+':/usr/bin',*args],check=True,capture_output=True)
  except subprocess.CalledProcessError as e:
   if self.unit_state(unit).get('LoadState')!='not-found':raise ValueError('Adoption service start uncertain: inspect '+unit+'; do not retry blindly')
   self.db.execute("UPDATE workers SET state='registered' WHERE id=?",(wid,))
   raise ValueError('Adoption service was not created: '+(e.stderr or b'').decode(errors='replace').strip())
  controller_pid=int(subprocess.check_output(['systemctl','--user','show',unit,'--property=MainPID','--value'],text=True).strip() or 0)
  for _ in range(30):
   for s in self.api.call('/sessions')['sessions']:
    full=self.session(s['id'])
    metadata=full.get('metadata') or {}
    exact_process=controller_pid>0 and metadata.get('hostPid')==controller_pid and metadata.get('path')==w['path'] and metadata.get('flavor')==w['runtime']
    # Inactive stale rows for the same history are not proof of this adoption.
    # Codex binds its thread before reporting; only the exact history ID proves resume.
    if full.get('active') and (native(full)==w['native_id'] or (w['runtime']!='codex' and not native(full) and exact_process)):
     self.db.execute("UPDATE workers SET session=?,state='managed' WHERE id=?",(s['id'],wid));return self.worker(wid)
   time.sleep(1)
  raise ValueError('Adoption outcome uncertain: inspect service '+unit+'; do not retry blindly')
 def native_path(self,w):
  if w['runtime']=='codex':
   c=sqlite3.connect('file:'+str(Path.home()/'.codex/state_5.sqlite')+'?mode=ro',uri=True)
   r=c.execute('SELECT rollout_path FROM threads WHERE id=?',(w['native_id'],)).fetchone();c.close()
   if not r:raise ValueError('Native history not found')
   return Path(r[0])
  matches=list((Path.home()/'.claude/projects').glob('*/'+w['native_id']+'.jsonl'))
  if len(matches)!=1:raise ValueError('Native history path is ambiguous or missing')
  return matches[0]
 def codex_prompt_ids(self,w):
  ids=set()
  with self.native_path(w).open() as f:
   for line in f:
    try:item=json.loads(line).get('payload',{}).get('item') or {}
    except (ValueError,AttributeError):continue
    if isinstance(item,dict) and item.get('type')=='UserMessage' and item.get('client_id'):ids.add(item['client_id'])
  return sorted(ids)
 def unit_state(self,unit):
  r=subprocess.run(['systemctl','--user','show',unit,'--property=LoadState,ActiveState,SubState,Result,MainPID,ExecMainStatus'],capture_output=True,text=True)
  if r.returncode:raise ValueError('Cannot inspect service '+unit)
  return dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
 def native_processes(self,nid):
  found=[]
  for proc in Path('/proc').glob('[0-9]*'):
   try:
    if nid.encode() in (proc/'cmdline').read_bytes():found.append(int(proc.name))
   except (FileNotFoundError,PermissionError,ProcessLookupError):pass
  return [pid for pid in found if pid!=os.getpid()]
 def codex_owner_alive(self,nid):
  # HAPI shared-runtime ownership; unreadable records or live PIDs are not conclusive.
  for f in (Path.home()/'.codex/hapi-runtime-owners').glob('*.json'):
   try:d=json.loads(f.read_text())
   except (ValueError,OSError):return True
   if any(x.get('threadId')==nid and x.get('active') for x in d.get('sessions',{}).values()):
    if any(isinstance(pid,int) and (Path('/proc')/str(pid)).exists() for pid in (d.get('pid'),d.get('serverPid'))):return True
  return False
 def session_optional(self,sid):
  try:return self.session(sid)
  except RuntimeError as error:
   if isinstance(error.__cause__,urllib.error.HTTPError) and error.__cause__.code==404:return None
   raise
 def stop_cleared_controller(self,wid,sid):
  # Explicit repair only: argv proves controller ownership, never current native identity.
  w=self.worker(wid);s=self.session(sid);meta=s.get('metadata') or {};state=s.get('agentState') or {}
  if w['runtime']!='claude' or w['state']!='managed' or self.session_optional(w['session']) is not None:raise ValueError('Expected a missing registered Claude session')
  if native(s) or s.get('thinking') or state.get('requests') or state.get('controlledByUser'):raise ValueError('Controller is not an idle cleared conversation')
  cmd=(Path('/proc')/str(meta.get('hostPid'))/'cmdline').read_bytes().split(b'\0')
  if b'--resume' not in cmd or cmd[cmd.index(b'--resume')+1]!=w['native_id'].encode() or not live_controller(s):raise ValueError('Controller does not belong to registered history')
  messages=self.api.call('/sessions/'+sid+'/messages?limit=100')['messages']
  users=[m for m in messages if is_instruction_message(m)]
  if not users or content(max(users,key=lambda m:m.get('seq',0))).get('content',{}).get('text')!='/clear':raise ValueError('Latest user instruction is not /clear; inspect before stopping')
  if any(content(m).get('role')=='user' and not m.get('invokedAt') for m in messages):raise ValueError('Pending user messages; inspect before stopping')
  self.api.call('/sessions/'+sid+'/archive',{})
  return {'worker':wid,'stopped_cleared_session':sid,'next':'Verify controller exit, then reconcile_adopt with rebind true and adopt original history'}
 def reconcile_adopt(self,wid,o_rebind=False):
  """Resolve an 'adopting' worker from evidence only: bind an exact live session,
  or return a conclusively dead attempt to 'registered'. Anything else refuses."""
  w=self.worker(wid);unit='manager-adopt-'+wid
  if w['state']=='managed' and w['session'] and o_rebind:
   old=self.session_optional(w['session'])
   if old and (old.get('active') or live_controller(old)):raise ValueError('Registered controller still runs; cannot rebind')
   live=[]
   for item in self.api.call('/sessions')['sessions']:
    info=self.session_optional(item['id'])
    if info and native(info)==w['native_id']:
     if info.get('active'):live.append(item['id'])
     elif live_controller(info):raise ValueError('A disconnected controller for this history still runs')
   if len(live)>1:raise ValueError('Multiple active sessions claim registered history')
   if live:
    self.db.execute('UPDATE workers SET session=? WHERE id=?',(live[0],wid));self.db.execute('DELETE FROM settings WHERE key=?',('host:'+wid,))
    self.event('adopt-rebound:'+wid+':'+live[0],w['project'],wid,'adoption-reconciled','Exact live native identity confirmed; original assignments preserved.')
    return {'worker':self.worker(wid),'outcome':'rebound'}
   if old is not None or w['runtime']!='claude':raise ValueError('No exact live native identity; inspect before recovery')
   state=self.unit_state(unit)
   if state.get('ActiveState') not in ('inactive','failed') or state.get('MainPID','0')!='0':raise ValueError('Adoption service still runs or has uncertain state')
   if self.native_processes(w['native_id']):raise ValueError('A process still references this native history; stop it before recovery')
   self.native_path(w)  # Original history must still exist; never replace it with a fresh chat.
   self.db.execute("UPDATE workers SET session=NULL,state='registered' WHERE id=?",(wid,))
   self.db.execute('DELETE FROM settings WHERE key=?',('host:'+wid,))
   self.event('adopt-missing:'+wid+':'+w['session'],w['project'],wid,'adoption-reconciled','Deleted HAPI binding released after controller exit; original history and assignments preserved. Adopt exact native conversation next.')
   return {'worker':self.worker(wid),'outcome':'registered'}
  if w['state']=='managed' and w['session']:
   # A bound session that reports different history was never this conversation.
   s=self.session(w['session'])
   if not native(s) or native(s)==w['native_id']:raise ValueError('Worker session matches its registered history; nothing to reconcile')
   if s.get('active') or live_controller(s):raise ValueError('Mis-bound session is still controlled; stop its controller before reconciliation')
   if self.unit_state(unit).get('ActiveState') in ('active','activating','deactivating','reloading'):raise ValueError('Adoption service '+unit+' still runs')
   if self.rows("SELECT id FROM assignments WHERE worker=? AND state IN ('sent','sending','uncertain')",(wid,)):raise ValueError('Unresolved assignment; inspect before reconciliation')
   detail='Unbound session '+w['session']+' (native '+native(s)+') which did not resume registered history '+w['native_id']+'. Worker returned to registered.'
   self.db.execute('BEGIN IMMEDIATE')
   try:
    self.db.execute("UPDATE workers SET session=NULL,state='registered' WHERE id=? AND session=?",(wid,w['session']))
    self.event('adopt-misbound:'+wid+':'+w['session'],w['project'],wid,'adoption-misbound',detail)
    self.db.execute('COMMIT')
   except Exception:self.db.execute('ROLLBACK');raise
   subprocess.run(['systemctl','--user','reset-failed',unit],capture_output=True)
   return {'worker':self.worker(wid),'outcome':'registered','detail':detail}
  if w['state']!='adopting' or w['session']:raise ValueError('Worker has no unresolved adoption')
  matches=[]
  for other in self.api.call('/sessions')['sessions']:
   info=self.session(other['id'])
   if native(info)==w['native_id']:
    if info.get('active'):matches.append(other['id'])
    elif live_controller(info):raise ValueError('A disconnected controller for this history still runs; outcome uncertain')
  if len(matches)==1:
   self.db.execute("UPDATE workers SET session=?,state='managed' WHERE id=?",(matches[0],wid))
   self.event('adopt-reconciled:'+wid+':'+matches[0],w['project'],wid,'adoption-reconciled','Late adoption confirmed by exact native ID.')
   return {'worker':self.worker(wid),'outcome':'managed'}
  if matches:raise ValueError('Multiple active HAPI sessions claim this history; resolve duplicates manually')
  state=self.unit_state(unit)
  if state.get('ActiveState')!='failed' or state.get('MainPID','0')!='0':
   raise ValueError('Adoption service '+unit+' is not conclusively failed ('+state.get('LoadState','?')+'/'+state.get('ActiveState','?')+'); do not retry')
  if self.native_processes(w['native_id']):raise ValueError('A process still references this native history; outcome uncertain')
  if self.rows("SELECT id FROM assignments WHERE worker=? AND state IN ('sent','sending','uncertain')",(wid,)):raise ValueError('Unresolved assignment; inspect before reconciliation')
  if w['runtime']=='codex':
   if self.codex_owner_alive(w['native_id']):raise ValueError('A live HAPI Codex runtime owns this history; outcome uncertain')
   lockpath=Path.home()/'.codex/thread-writer-locks'/(w['native_id']+'.lock')
   if lockpath.exists():
    with lockpath.open('r') as f:
     try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
     except BlockingIOError:raise ValueError('Native session writer lock is held; outcome uncertain')
     fcntl.flock(f,fcntl.LOCK_UN)
  detail='Adoption service failed (result='+state.get('Result','?')+', status='+state.get('ExecMainStatus','?')+'); no controller, process or writer remains. Worker returned to registered; adopt may be retried.'
  self.db.execute('BEGIN IMMEDIATE')
  try:
   self.db.execute("UPDATE workers SET state='registered' WHERE id=? AND state='adopting' AND session IS NULL",(wid,))
   self.event('adopt-failed:'+wid+':'+str(time.time()),w['project'],wid,'adoption-failed',detail)
   self.db.execute('COMMIT')
  except Exception:self.db.execute('ROLLBACK');raise
  # Unit name is released by the next adopt if this best-effort cleanup fails.
  subprocess.run(['systemctl','--user','reset-failed',unit],capture_output=True)
  return {'worker':self.worker(wid),'outcome':'registered','detail':detail}
 def reconcile(self):
  import node_graph
  from coordination import reconcile_registry
  try:reconcile_registry(self)
  except Exception as e:log.warning('handoff watch registry: %s',e)
  summaries=None
  try:
   node_graph.reconcile(self);summaries=node_graph.summaries(self)
  except Exception as e:
   if node_graph.active(self):log.warning('node graph: %s',e)
  nodes=node_graph.active(self)
  # Read-only owner availability is separate from successful worker observation.
  # Publishing itself performs no network calls or runtime actions.
  self._oversight_owners={}
  owners={self.get('owner:'+w['id']) or self.get('manager') for w in self.rows("SELECT id FROM workers WHERE state='managed'") if self.watch(w['id'])['mode']!='off'}
  for owner in owners-{None,''}:
   try:
    current=self.session(owner)
    self._oversight_owners[owner]='paused' if current.get('manualPausedAt') is not None else 'available' if current.get('active') else 'offline'
   except Exception:self._oversight_owners[owner]='unknown'
  workers=self.rows("SELECT * FROM workers WHERE session IS NOT NULL AND state!='released' ORDER BY created,id")
  try:scan_limit=max(1,min(100,int(os.environ.get('HAPI_OBSERVATION_BATCH_SIZE','24'))))
  except ValueError:scan_limit=24
  if nodes and workers:
   cursor=self.get('node-scan-cursor');index=next((i+1 for i,w in enumerate(workers) if w['id']==cursor),0)%len(workers)
   workers=workers[index:]+workers[:index]
  scanned_count=0
  for w in workers:
   if self.watch(w['id'])['mode']=='off':continue
   if self.rows("SELECT worker FROM usage_recovery WHERE worker=? AND state='resume-check'",(w['id'],)):continue
   # Node mode: an idle child's edge sleeps after one final scan; no polling until it runs again.
   if nodes and summaries is not None and w['session']!=self.get('manager') and not node_graph.should_scan(self,w,summaries.get(w['session'])):continue
   if nodes:
    if scanned_count>=scan_limit:break
    scanned_count+=1;self.set('node-scan-cursor',w['id'])
   # Node children stay managed until removed; classic mode retires finished idle chats.
   try:
    scanned=self.scan(w,retire=not nodes)
    if scanned is not None:self.db.execute('DELETE FROM settings WHERE key=?',('scan-error:'+w['id'],))
    if nodes and scanned is not None:node_graph.scan_completed(self,w,scanned)
   except Exception as e:
    if not self.get('scan-error:'+w['id']):
     failed=time.time();self.set('scan-error:'+w['id'],failed)
     self.event('watch-error:'+w['id']+':'+str(failed),w['project'],w['id'],'observer-error','Unable to observe this chat. Inspect monitoring before relying on background completion/failure follow-up. No worker was restarted or messaged.')
    log.warning('scan %s: %s',w['id'],e)
  # Each item is isolated: one broken session must not stop reconciliation or manager wakes.
  try:
   # Recover spawn response loss by exact path/runtime, never by title or latest session.
   for w in self.rows("SELECT * FROM workers WHERE state='launching'"):
    try:
     matches=[]
     for s in self.api.call('/sessions')['sessions']:
      m=s.get('metadata') or {}
      if m.get('path')==w['path'] and m.get('flavor')==w['runtime'] and s.get('createdAt',0)/1000>=w['created']-1:matches.append(s)
     if len(matches)==1:self.db.execute("UPDATE workers SET session=?,state='managed' WHERE id=?",(matches[0]['id'],w['id']))
     else:self.event('spawn:'+w['id'],w['project'],w['id'],'uncertain-launch','Inspect live processes and HAPI before retry. No automatic second spawn.')
    except Exception as e:log.warning('launch reconcile %s: %s',w['id'],e)
   # Resolve uncertain sends from hub queue/invocation evidence, without resending.
   for a in self.rows("SELECT a.*,w.session FROM assignments a JOIN workers w ON w.id=a.worker WHERE a.state IN ('sending','uncertain') AND w.state!='released'"):
    try:
     q=self.api.call('/sessions/'+a['session']+'/messages/queued-state',{'localIds':[a['id']]})
     if a['id'] in q.get('queuedLocalIds',[]) or any(x['localId']==a['id'] for x in q.get('invokedLocalMessages',[])):
      self.db.execute("UPDATE assignments SET state='sent' WHERE id=?",(a['id'],))
    except Exception as e:log.warning('assignment reconcile %s: %s',a['id'],e)
   for a in self.rows("SELECT * FROM outbox WHERE state IN ('sending','uncertain','sent')"):
    try:
     q=self.api.call('/sessions/'+a['session']+'/messages/queued-state',{'localIds':[a['id']]})
     if not all(isinstance(q.get(key),list) for key in ('queuedLocalIds','indeterminateLocalIds','invokedLocalMessages')):raise ValueError('Incomplete delivery state; preserving unresolved wake')
     if any(x['localId']==a['id'] for x in q['invokedLocalMessages']):
      self.db.execute("UPDATE outbox SET state='invoked' WHERE id=?",(a['id'],))
     elif a['id'] in q['indeterminateLocalIds']:
      # Dispatch is unresolved, regardless of age. Never allocate another localId.
      self.db.execute("UPDATE outbox SET state='uncertain' WHERE id=?",(a['id'],))
     elif a['id'] in q['queuedLocalIds']:self.db.execute("UPDATE outbox SET state='sent' WHERE id=?",(a['id'],))
     elif a['state']=='sent':
      # A previously accepted wake is now absent: cancellation/withdrawal, not
      # permission to replay it. Settle its events so later observations can flow.
      self.db.execute('BEGIN IMMEDIATE')
      try:
       self.db.execute("UPDATE outbox SET state='cancelled' WHERE id=?",(a['id'],))
       self.db.execute('UPDATE events SET acked=1 WHERE wake_id=?',(a['id'],))
       self.db.execute('COMMIT')
      except Exception:self.db.execute('ROLLBACK');raise
     elif a['state'] in ('sending','uncertain') and time.time()-a['created']>LOST_AFTER:
      # The hub never received this batch: release its events for a later wake.
      self.db.execute('BEGIN IMMEDIATE')
      try:
       self.db.execute("UPDATE outbox SET state='lost' WHERE id=?",(a['id'],))
       self.db.execute('UPDATE events SET wake_id=NULL WHERE wake_id=? AND acked=0',(a['id'],))
       self.db.execute('COMMIT')
      except Exception:self.db.execute('ROLLBACK');raise
    except Exception as e:log.warning('outbox reconcile %s: %s',a['id'],e)
  finally:
   try:self.sweep_superseded()
   except Exception as e:log.warning('superseded sweep: %s',e)
   try:self.deliver_reminders()
   except Exception as e:log.warning('reminders: %s',e)
   try:self.publish_managed()
   except Exception as e:log.warning('publish managed: %s',e)
   try:self.recover_usage()
   except Exception as e:log.warning('usage recovery: %s',e)
   try:
    import transient_recovery
    transient_recovery.recover(self)
   except Exception as e:log.warning('transient recovery: %s',e)
   try:self.notify_manager()
   except Exception as e:log.warning('manager notification: %s',e)
 def manager_paused(self):
  sid=self.get('manager')
  return bool(sid and self.session(sid).get('manualPausedAt') is not None)
 def notify_manager(self):
  """Deliver pending events: overseer events (and classic mode) to the manager, node events to each direct owner."""
  import node_graph
  sid=self.get('manager')
  if not sid:return
  import transient_recovery
  try:transient_recovery.observe_owner(self,sid,self.session(sid))
  except Exception as error:log.warning('manager recovery observation: %s',error)
  pending=[]
  for e in self.rows('SELECT * FROM events WHERE acked=0 AND wake_id IS NULL ORDER BY id'):
   try:
    eligible=self.retirement_deliverable(e) or ((not e['worker'] or not self.get('unmanaged:'+e['worker'])) and (e['kind'] in ('check-in','progress') or not e['worker'] or self.watch(e['worker'])['mode']=='events') and (e['kind'] not in ('progress','observation') or self.progress_deliverable(e)))
    if eligible:pending.append(e)
   except Exception as error:
    # A failed freshness check affects this event only, not other workers' input
    # requests/completions. Keep it pending for a later successful observation.
    log.warning('event %s deliverability: %s',e['id'],error)
  live_edges=None
  if node_graph.active(self):
   try:live_edges={e['child']:e['owner'] for e in node_graph.fetch(self)['graph']['edges']}
   except Exception:live_edges={} # Fail closed for child routing; global diagnostics still reach root.
  groups={}
  for e in pending:
   try:groups.setdefault(node_graph.recipient(self,e,live_edges) or sid,[]).append(e)
   except ValueError:continue # A verified tree is required for child routing; keep event durable.
  for recipient,events in groups.items():
   try:self.wake_owner(recipient,events[:20],recipient==sid)
   except Exception as error:log.warning('wake %s: %s',recipient,error)
 def wake_owner(self,sid,events,overseer):
  s=self.session(sid)
  import transient_recovery
  if sid!=self.get('manager'):
   try:transient_recovery.observe_owner(self,sid,s)
   except Exception:return # fail closed before waking an unobserved owner
  if transient_recovery.blocked(self,sid):return
  if s.get('manualPausedAt') is not None:return
  if not s.get('active'):
   # Only the overseer is reconnected automatically; mid-level owners keep events until they run.
   if not overseer:return
   # Hub disconnects do not immediately justify a second controller.
   since=float(self.get('manager_offline_since') or time.time());self.set('manager_offline_since',since)
   if time.time()-since<60 or live_controller(s):return
   result=self.api.call('/sessions/'+sid+'/resume',{})
   if result.get('type')!='success':return
   sid=result['sessionId'];self.set('manager',sid);self.set('manager_offline_since',time.time());return
  if overseer:self.set('manager_offline_since',time.time())
  if s.get('thinking') or (s.get('agentState') or {}).get('requests'):return
  if not events:return
  completions={}
  for e in events:
   if e['worker'] and (e['kind']=='finished' or (e['kind']=='observation' and any(c.get('reason','').startswith('Runtime turn/goal ended') for c in json.loads(e['detail']).get('changes',[])))):
    try:
     terminal=self.current_terminal_message(self.worker(e['worker']))
     if terminal:completions[e['worker']]=terminal
    except Exception as error:log.warning('completion correlation %s: %s',e['id'],error)
  aid=uid();text=self.batch_text(events)
  if not overseer:
   text=text.replace('[supervisor update; not a user instruction]','[supervisor update for chats you own in the node graph; not a user instruction. Manage them with '+str(ROOT/'managerctl.py')+'; see '+str(ROOT/'manager/NODES.md')+']',1)
  self.db.execute('BEGIN IMMEDIATE')
  try:
   self.db.execute('INSERT INTO outbox VALUES(?,?,?,?,?)',(aid,sid,text,'sending',time.time()))
   for wid,terminal in completions.items():self.set('completion-notice:'+wid+':'+terminal,aid)
   for ev in events:
    self.db.execute('UPDATE events SET wake_id=? WHERE id=?',(aid,ev['id']))
    if ev['worker']:self.set('reporting-last:'+ev['worker'],time.time())
   self.db.execute('COMMIT')
  except: self.db.execute('ROLLBACK');raise
  try:self.api.call('/sessions/'+sid+'/messages',{'text':text,'localId':aid})
  except Exception:
   self.db.execute("UPDATE outbox SET state='uncertain' WHERE id=?",(aid,));raise
  self.db.execute("UPDATE outbox SET state='sent' WHERE id=?",(aid,))
 def batch_text(self,events):
  """Compact wake message: one line per event; handling rules live in manager/AGENTS.md."""
  lines=[]
  for ev in events:
   p=self.rows('SELECT name FROM projects WHERE id=?',(ev['project'],))
   w=self.rows('SELECT runtime FROM workers WHERE id=?',(ev['worker'],))
   task=self.rows('SELECT task_key FROM assignments WHERE id=?',(ev['event_key'].split(':',1)[-1],)) if ev['event_key'].startswith('result:') else []
   detail=' '.join(str(ev['detail']).split())
   parts=['#%d %s'%(ev['id'],ev['kind']),p[0]['name'] if p else (ev['project'] or '-')[:8]]
   if ev['worker']:parts.append('%s:%s'%(w[0]['runtime'] if w else 'worker',ev['worker'][:8]))
   if task:parts.append('task='+task[0]['task_key'])
   if ev['kind'] in ('progress','observation'):
    snap=json.loads(ev['detail']);detail='Read-only; '+snap['status']+'; latest tool: '+str(snap['latest_tool'] or 'unknown')+'; latest public update: '+str(snap['latest_reply'] or 'No public update available')+'; pending input: '+str(len(snap['pending_requests']))+'; tracker: '+snap['trackerStatus']
   if ev['kind']=='observation':
    detail+='; report reason: '+snap['reason']+'; evidence changes: '+json.dumps(snap['changes'],ensure_ascii=False)
   limit=1800 if ev['kind']=='observation' else 900 if ev['kind']=='progress' else 160
   parts.append(detail if len(detail)<=limit else detail[:limit-1]+'…')
   note=self.watch(ev['worker'])['note'] if ev['worker'] else ''
   if note:parts.append('user preference: '+note[:160])
   lines.append(' · '.join(parts))
  ids=','.join(str(ev['id']) for ev in events)
  return '[supervisor update; not a user instruction] %d event%s; ack ids %s after handling\n'%(len(events),'' if len(events)==1 else 's',ids)+'\n'.join(lines)
 def sse(self):
  while True:
   try:
    self.api.auth();headers={'Authorization':'Bearer '+self.api.token,'Accept-Encoding':'identity'}
    cursor=self.get('sse_cursor')
    if cursor:headers['Last-Event-ID']=cursor
    req=urllib.request.Request(BASE+'/api/events?all=true&visibility=hidden',headers=headers)
    with urllib.request.urlopen(req,timeout=45) as r:
     eventid=None
     for raw in r:
      line=raw.decode().strip()
      if line.startswith('id:'):eventid=line[3:].strip()
      if line.startswith('data:'):
       ev=json.loads(line[5:]);kind=ev.get('type')
       if kind not in ('heartbeat','toast'):
        # REST reconciliation is durable and idempotent; leave cursor behind until completed.
        self.wake.set()
        if eventid:
         with self.lock:self.set('sse_seen_cursor',eventid)
   except Exception as e:log.warning('SSE reconnect: %s',e);time.sleep(3)
 def run(self):
  threading.Thread(target=self.sse,daemon=True).start();last=0
  while True:
   with self.lock:
    for req in CONTROL.glob('*.req'):
     key=req.stem;cached=self.rows('SELECT result FROM requests WHERE id=?',(key,))
     if cached:result=json.loads(cached[0]['result'])
     else:
      # An interrupted request is deliberately left uncertain, never executed twice.
      self.db.execute('INSERT INTO requests VALUES(?,?)',(key,json.dumps({'error':'Request interrupted/uncertain; reconcile status before retry'})))
      try:result=self.dispatch(json.loads(req.read_text()))
      except Exception as e:log.exception('control request');result={'error':str(e)}
      self.db.execute('UPDATE requests SET result=? WHERE id=?',(json.dumps(result),key))
     out=CONTROL/(key+'.out');tmp=CONTROL/(key+'.result-tmp');tmp.write_text(json.dumps(result));tmp.chmod(0o600);tmp.replace(out);req.unlink()
     # Delivered: keep only the dedupe marker, not the (possibly huge) payload.
     self.db.execute('UPDATE requests SET result=? WHERE id=?',(DELIVERED,key))
    if self.wake.is_set() or time.time()-last>30:
     self.wake.clear()
     try:
      self.reconcile()
      cursor=self.get('sse_seen_cursor')
      if cursor:self.set('sse_cursor',cursor)
     except Exception as e:log.warning('reconciliation: %s',e)
     last=time.time()
   time.sleep(.5)
if __name__=='__main__':
 logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
 lock=open(STATE/'supervisor.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 Supervisor().run()
