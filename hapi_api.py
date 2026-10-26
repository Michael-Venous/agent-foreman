import json,time,threading,urllib.request,urllib.error
from pathlib import Path
BASE='http://127.0.0.1:3006'
class Hapi:
 def __init__(self): self.token=None; self.until=0; self.lock=threading.RLock()
 def auth(self):
  with self.lock:
   if self.token and time.time()<self.until:return
   secret=json.loads((Path.home()/'.hapi/settings.json').read_text())['cliApiToken']
   req=urllib.request.Request(BASE+'/api/auth',data=json.dumps({'accessToken':secret}).encode(),headers={'Content-Type':'application/json'})
   with urllib.request.urlopen(req,timeout=20) as r:self.token=json.load(r)['token']
   self.until=time.time()+10800
 def call(self,path,data=None,method=None,_refreshed=False):
  self.auth()
  req=urllib.request.Request(BASE+'/api'+path,data=None if data is None else json.dumps(data).encode(),headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},method=method)
  try:
   with urllib.request.urlopen(req,timeout=120) as r:return json.load(r)
  except urllib.error.HTTPError as e:
   if e.code==401 and not _refreshed:
    self.until=0
    return self.call(path,data,method,_refreshed=True)
   raise RuntimeError(f'{e.code}: {e.read().decode()}') from e
if __name__=='__main__':
 import sys
 print(json.dumps(Hapi().call(sys.argv[1],json.loads(sys.argv[2]) if len(sys.argv)>2 else None),indent=2))
