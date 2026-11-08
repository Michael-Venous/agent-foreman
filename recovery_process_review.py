"""Exact, episode-scoped review of a Distrobox idle monitor; never controls a process."""
import hashlib,hmac,json,os,re,subprocess,time
from pathlib import Path
from resource_handoff import current_user_authorization

ROOT=Path(__file__).resolve().parent
_REVIEW_KEY=os.urandom(32)

def command_json(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=15,check=True)
 return json.loads(p.stdout)

def proc(pid):
 p=Path('/proc')/str(pid)
 raw=(p/'stat').read_text().rsplit(') ',1)[1].split()
 args=(p/'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
 result={'pid':pid,'ppid':int(raw[1]),'start':int(raw[19]),'uid':p.stat().st_uid,
         'exe':os.readlink(p/'exe'), 'argv':[a.decode() for a in args]}
 if (p/'stat').read_text().rsplit(') ',1)[1].split()[19]!=raw[19]:raise ValueError('Process changed during inspection')
 return result

def validate_inventory(items,init):
 if len(items)!=2 or len({item['pid'] for item in items})!=len(items):raise ValueError('Incomplete container inventory')
 found=False
 for item in items:
  if item['pid']==init['pid']:
   if item!=init:raise ValueError('Container init changed')
   found=True
  elif item['ppid']!=init['pid'] or item['exe'] not in ('/usr/bin/sleep','/bin/sleep') or item['argv']!=['sleep','15'] or item['uid']!=init['uid']:raise ValueError('Container has additional or unknown work')
 if not found:raise ValueError('Container init missing')

def snapshot(s,r,pid,cid):
 from supervisor import native
 if not re.fullmatch(r'[0-9a-f]{64}',cid) or type(pid)!=int or pid<=0:raise ValueError('Exact container ID and PID required')
 session=s.session(r['session']);meta=session.get('metadata') or {};agent=session.get('agentState') or {}
 if (r['state']!='waiting' or r['kind'] not in ('capacity','rate') or not r['worker'] or not session.get('active') or session.get('thinking') or session.get('manualPausedAt') is not None or native(session)!=r['native'] or agent.get('requests') or agent.get('controlledByUser') or agent.get('claudeReadyMessages')):raise ValueError('Recovery or session is not eligible')
 if session.get('backgroundTaskCount')!=1:raise ValueError('Expected exactly one background process')
 ps=session.get('backgroundProcesses')
 if not isinstance(ps,list) or len(ps)!=1 or ps[0].get('pid')!=pid:raise ValueError('Background inventory does not match')
 checked=ps[0].get('checkedAt')
 if type(checked) not in (int,float) or not -1<=time.time()-checked/1000<=15:raise ValueError('Background inventory is stale or missing')
 monitor=proc(pid)
 if ps[0].get('id')!=f"{Path('/proc/sys/kernel/random/boot_id').read_text().strip()}:{pid}:{monitor['start']}":raise ValueError('Background process start identity changed')
 env=(Path('/proc')/str(pid)/'environ').read_bytes().split(b'\0')
 if monitor['uid']!=os.getuid() or monitor['exe']!='/usr/bin/conmon' or b'HAPI_SESSION_ID='+r['session'].encode() not in env or b'CODEX_THREAD_ID='+r['native'].encode() not in env:raise ValueError('Monitor session identity does not match')
 argv=monitor['argv'];matches=[argv[i+1] for i,v in enumerate(argv[:-1]) if v=='-c']
 if matches!=[cid]:raise ValueError('Monitor container identity does not match')
 info=command_json(['podman','inspect',cid])
 if not isinstance(info,list) or len(info)!=1:raise ValueError('Container inventory unavailable')
 c=info[0];state=c.get('State') or {};config=c.get('Config') or {}
 if c.get('Id')!=cid or not state.get('Running') or state.get('Paused') or state.get('ConmonPid')!=pid or config.get('Entrypoint')!=['/usr/bin/entrypoint'] or (config.get('Labels') or {}).get('manager')!='distrobox':raise ValueError('Container identity or state changed')
 init=proc(state['Pid']); cg=state.get('CgroupPath')
 if init['ppid']!=pid or not cg or not cg.endswith('/libpod-'+cid+'.scope'):raise ValueError('Container init identity mismatch')
 if len(init['argv'])<4 or init['argv'][:2]!=['/bin/sh','/usr/bin/entrypoint'] or '--init' not in init['argv'] or init['argv'][init['argv'].index('--init')+1]!='0':raise ValueError('Container is not the expected persistent init')
 script=Path('/proc')/str(init['pid'])/'root/usr/bin/entrypoint'
 script_bytes=script.read_bytes()
 # Refuse unknown init implementations: these markers establish the known Distrobox keepalive loop.
 if b'while true' not in script_bytes or b'sleep 15' not in script_bytes:raise ValueError('Unknown container init implementation')
 group=Path('/sys/fs/cgroup')/cg.lstrip('/')
 def inventory():
  ids=set()
  def onerror(e):raise e
  for directory,dirs,files in os.walk(group,onerror=onerror):
   if 'cgroup.procs' not in files:raise ValueError('Incomplete container inventory')
   ids.update(int(x) for x in (Path(directory)/'cgroup.procs').read_text().split())
  if not ids or init['pid'] not in ids:raise ValueError('Container inventory missing')
  result=[proc(i) for i in sorted(ids)]
  validate_inventory(result,init)
  return result
 first=inventory()
 bg=command_json([str(Path.home()/'.bun/bin/bun'),str(ROOT/'deploy/read-native-background.ts'),r['session'],r['native']])
 if bg.get('readOnly') is not True or bg.get('terminalCount')!=0 or bg.get('terminals')!=[]:raise ValueError('Native terminals are active or unavailable')
 queue=command_json([str(Path.home()/'.bun/bin/bun'),str(ROOT/'deploy/read-native-queue.ts'),r['session'],r['native']])
 if queue.get('readOnly') is not True or queue.get('session')!=r['session'] or queue.get('native_id')!=r['native'] or queue.get('items')!=[]:raise ValueError('Native queue is not empty or unavailable')
 second=inventory()
 if first!=second or proc(pid)!=monitor:raise ValueError('Process inventory changed during inspection')
 controller=proc(meta.get('hostPid'))
 if controller['uid']!=os.getuid():raise ValueError('Controller identity unavailable')
 boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
 identity={'session':r['session'],'native':r['native'],'worker':r['worker'],'episode':r['episode'],'kind':r['kind'],'boot':boot,'controllerPid':meta.get('hostPid'),'controllerStart':controller['start'],'monitorPid':pid,'monitorStart':monitor['start'],'containerId':cid,'containerStartedAt':state.get('StartedAt'),'initPid':init['pid'],'initStart':init['start'],'cgroup':cg,'entrypointSha256':hashlib.sha256(script_bytes).hexdigest()}
 if not identity['controllerPid'] or not identity['episode'] or not identity['containerStartedAt']:raise ValueError('Incomplete identity')
 return {'identity':identity,'containerProcesses':second,'nativeTerminals':0,'nativeQueue':0}

def digest(identity):return hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def review(s,o):
 rows=s.rows('SELECT * FROM transient_recovery WHERE worker=? AND state=?',(o.get('worker'),'waiting'))
 if len(rows)!=1:raise ValueError('Exact waiting recovery not found')
 ev=snapshot(s,rows[0],o.get('pid'),o.get('container_id',''))
 reviewed=time.time();d=digest(ev['identity'])
 return {'readOnly':True,'authorized':False,'reviewedAt':reviewed,'digest':d,'reviewToken':hmac.new(_REVIEW_KEY,(d+':'+str(reviewed)).encode(),hashlib.sha256).hexdigest(),'evidence':ev}

def authorize(s,o):
 if not current_user_authorization(o):raise ValueError('Current explicit user authorization and basis required')
 t=o.get('reviewed_at')
 if type(t) not in (int,float) or not 0<=time.time()-t<=300:raise ValueError('Fresh review required')
 submitted=o.get('digest');token=o.get('review_token')
 if not isinstance(submitted,str) or not isinstance(token,str) or not hmac.compare_digest(token,hmac.new(_REVIEW_KEY,(submitted+':'+str(t)).encode(),hashlib.sha256).hexdigest()):raise ValueError('Review token invalid or supervisor restarted')
 result=review(s,o)
 if submitted!=result['digest']:raise ValueError('Reviewed identity changed')
 identity=result['evidence']['identity']
 key='transient-process-review:'+identity['session']
 s.set(key,json.dumps({'identity':identity,'digest':result['digest'],'expires':time.time()+300,'basis':o['basis'].strip()}))
 return {'authorized':True,'session':identity['session'],'episode':identity['episode'],'expiresIn':300,'oneAttempt':True}

def approved(s,r):
 key='transient-process-review:'+r['session'];raw=s.get(key)
 if not raw:return False
 try:
  a=json.loads(raw);ident=a['identity']
  if time.time()>a['expires'] or ident['episode']!=r['episode'] or ident['worker']!=r['worker'] or ident['native']!=r['native']:return False
  ev=snapshot(s,r,ident['monitorPid'],ident['containerId'])
  return digest(ev['identity'])==a['digest'] and ev['identity']==ident
 except Exception:return False
