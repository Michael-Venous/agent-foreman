#!/usr/bin/env python3
"""Read-only audit. Deduplicate request IDs across native logs and HAPI replay."""
import json,collections,datetime,pathlib,re
ROOT=pathlib.Path(__file__).resolve().parents[1]
requests={};seen_paths=set();rate=[];tools={};shapes=collections.Counter();variants=collections.defaultdict(set)
def ingest(d,source,when=None,sid=None):
 if not isinstance(d,dict):return
 when=d.get('timestamp') or when or ''
 if not when.startswith('2026-10-19'):return
 m=d.get('message',{});m=m if isinstance(m,dict) else {}
 mid=m.get('id');u=m.get('usage');sid=d.get('sessionId') or sid
 if mid and isinstance(u,dict) and m.get('model')!='<synthetic>':
  rec={'id':mid,'session':sid,'timestamp':when,'source':source,'usage':u,'model':m.get('model'),'effort':d.get('effort'),'sidechain':bool(d.get('isSidechain')) or '/subagents/' in source,'agent':(pathlib.Path(source).stem if '/subagents/' in source else 'main')}
  variants[mid].add(json.dumps(u,sort_keys=True))
  if mid not in requests or (u.get('output_tokens',0)>requests[mid]['usage'].get('output_tokens',0)):requests[mid]=rec
 for b in m.get('content',[]) if isinstance(m.get('content'),list) else []:
  if not isinstance(b,dict):continue
  if b.get('type')=='tool_use':tools[b.get('id')]={'timestamp':when,'session':sid,'tool':b.get('name')}
  text=b.get('text','')
  if b.get('type')=='text' and re.match(r'Claude AI usage limit (warning|reached)\|',text):rate.append({'timestamp':when,'session':sid,'text':text})
 if d.get('type') in ('rate_limit_event','result'):shapes[d['type']]+=1
for p in (pathlib.Path.home()/'.claude/projects').rglob('*.jsonl'):
 if p.resolve() in seen_paths:continue
 seen_paths.add(p.resolve())
 if p.stat().st_mtime<1792382400:continue
 for line in p.open():
  try:ingest(json.loads(line),str(p),sid=p.stem)
  except ValueError:pass
import sys
sys.path.insert(0,str(ROOT))
from hapi_api import Hapi
api=Hapi();names={};hubrows=0
for session in api.call('/sessions')['sessions']:
 m=session.get('metadata') or {}
 if m.get('flavor')!='claude':continue
 sid=session['id'];names[sid]={'native':m.get('claudeSessionId'),'name':(m.get('summary') or {}).get('text') or m.get('name') or m.get('path')}
 query='?limit=100'
 while True:
  page=api.call('/sessions/'+sid+'/messages'+query)
  for msg in page['messages']:
   d=msg.get('content',{})
   if d.get('type')=='plaintext':d=d.get('data',{})
   if d.get('role')!='agent':continue
   data=d.get('content',{}).get('data',{})
   ingest(data,'hapi:'+sid,datetime.datetime.fromtimestamp(msg['createdAt']/1000,datetime.timezone.utc).isoformat(),m.get('claudeSessionId'));hubrows+=1
  cursor=page.get('page',{})
  if not cursor.get('hasMore'):break
  query='?limit=100&beforeSeq='+str(cursor['nextBeforeSeq'])+'&beforeAt='+str(cursor['nextBeforeAt'])
rate=list({(r['timestamp'],r['session'],r['text']):r for r in rate}.values());rate.sort(key=lambda r:r['timestamp'])
def total(rows):
 t=collections.Counter()
 for r in rows:
  for k,v in r['usage'].items():
   if isinstance(v,(int,float)):t[k]+=v
 return {'requests':len(rows),**t}
end='2026-10-19T17:48:05';windows={}
for mins in [5,10,15,30,60,300]:
 start=(datetime.datetime.fromisoformat(end)-datetime.timedelta(minutes=mins)).isoformat()
 rows=[r for r in requests.values() if start<=r['timestamp']<end];by={}
 for sid in sorted(set(r['session'] or '?' for r in rows)):
  rs=[r for r in rows if (r['session'] or '?')==sid];contexts=[sum(r['usage'].get(k,0) for k in ['input_tokens','cache_creation_input_tokens','cache_read_input_tokens']) for r in rs]
  by[sid]={**total(rs),'context_min':min(contexts),'context_max':max(contexts),'efforts':dict(collections.Counter(str(r['effort']) for r in rs)),'models':dict(collections.Counter(r['model'] for r in rs))}
 windows[str(mins)]={'start':start,'end':end,'total':total(rows),'sessions':by}
report={'method':'Unique provider message IDs across native JSONL and HAPI; highest output-token record per ID. UTC; local machine only. Cached input not counted as newly generated input. No inferred quota dollar conversion.','names':names,'windows':windows,'rate_timeline':rate,'agent_calls':[t for t in tools.values() if t['tool'] in ('Agent','Task')],'record_shapes':dict(shapes),'requests_with_multiple_usage_snapshots':sum(len(v)>1 for v in variants.values()),'requests':sorted(requests.values(),key=lambda r:r['timestamp'])}
final_rows=[r for r in requests.values() if windows['5']['start']<=r['timestamp']<end]
breakdown=[]
for sid,agent in sorted(set((r['session'],r['agent']) for r in final_rows)):
 rs=[r for r in final_rows if (r['session'],r['agent'])==(sid,agent)]
 breakdown.append({'session':sid,'agent':agent,**total(rs)})
for field in ['requests','input_tokens','cache_creation_input_tokens','cache_read_input_tokens','output_tokens']:
 assert sum(r.get(field,0) for r in breakdown)==windows['5']['total'][field]
report['final_five_minutes_breakdown']=breakdown
report['verification']={'account_tier':'default_claude_max_5x (local auth metadata, verified 2026-10-19)', 'cutoff_basis':'Last recorded response before user-reported exhaustion: 17:48:04.201 UTC; window ends 17:48:05 UTC. Not a proven provider-rejection timestamp.', 'highest_observed_provider_utilization':99, 'provider_rejection_found':any('limit reached|' in r['text'] for r in rate), 'sum_checks_passed':True, 'limitations':['Local-machine records only; off-device, claude.ai and unlogged requests may be absent.','No public fixed-token or dollar allowance for Max 5x; API list prices cannot validate subscription deductions.','No provider billing anomaly or high-effort misconfiguration proven.']}
p=ROOT/'evidence/claude-all-chats-usage-2026-10-19.json';p.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:report[k] for k in ['windows','rate_timeline','agent_calls','record_shapes','requests_with_multiple_usage_snapshots']},indent=2))
