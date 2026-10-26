#!/usr/bin/env python3
"""Operator helpers; does not change CLI authentication or permission policies."""
import sys,subprocess,json,sqlite3,shutil,os,time
from pathlib import Path
from hapi_api import Hapi
ROOT=Path(__file__).resolve().parent
SERVICES=['hapi-hub','hapi-runner','agent-manager']
def control(obj):
 return json.loads(subprocess.check_output(['python3',str(ROOT/'managerctl.py'),json.dumps(obj)],text=True))
if len(sys.argv)!=2:sys.exit('Usage: ops.py status|login|backup|stop|start')
cmd=sys.argv[1]
if cmd=='status':
 subprocess.run(['systemctl','--user','--no-pager','--lines=0','status',*SERVICES]);print(json.dumps(control({'op':'status'}),indent=2))
elif cmd=='login':
 local_path=ROOT/'state/local-config.json'
 local=json.loads(local_path.read_text()) if local_path.exists() else {}
 print('URL: '+os.environ.get('HAPI_PUBLIC_URL',local.get('public_url','http://127.0.0.1:3006')))
 print('HAPI access token (keep private): '+json.loads((Path.home()/'.hapi/settings.json').read_text())['cliApiToken'])
elif cmd=='backup':
 dest=ROOT/'state/backups'/time.strftime('%Y%m%d-%H%M%S');dest.mkdir(parents=True,mode=0o700)
 for src,name in [(ROOT/'state/manager.sqlite','manager.sqlite'),(Path.home()/'.hapi/hapi.db','hapi.db')]:
  with sqlite3.connect(f'file:{src}?mode=ro',uri=True) as source,sqlite3.connect(dest/name) as target:source.backup(target)
 shutil.copy2(Path.home()/'.hapi/settings.json',dest/'hapi-settings.json')
 for p in dest.iterdir():p.chmod(0o600)
 print(dest)
elif cmd=='stop':
 state=control({'op':'status'})
 for p in state['projects']:
  if p['status']!='completed':control({'op':'pause','project':p['id']})
 subprocess.run(['systemctl','--user','stop','agent-manager'],check=True)
 with sqlite3.connect(ROOT/'state/manager.sqlite') as db:manager=db.execute("SELECT value FROM settings WHERE key='manager'").fetchone()[0]
 h=Hapi()
 for sid in {manager}|{w['session'] for w in state['workers'] if w['session']}:
  s=h.call('/sessions/'+sid)['session']
  if s.get('active'):
   if s.get('thinking'):h.call('/sessions/'+sid+'/abort',{})
   h.call('/sessions/'+sid+'/archive',{})
 subprocess.run(['systemctl','--user','stop','hapi-runner','hapi-hub'],check=True)
 print('Control services stopped; managed sessions archived with history preserved. Projects paused.')
elif cmd=='start':subprocess.run(['systemctl','--user','start',*SERVICES],check=True)
else:sys.exit('Unknown command')
