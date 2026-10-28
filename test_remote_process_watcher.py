import importlib.util
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace

spec=importlib.util.spec_from_file_location('remote_watcher',Path(__file__).parent/'deploy/watch-remote-process.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
BOOT='11111111-1111-4111-8111-111111111111'

class Watcher(unittest.TestCase):
 def test_probe_uses_fixed_ssh_args_and_quotes_remote_code(self):
  calls=[]
  def run(args,**kw):calls.append((args,kw));return SimpleNamespace(returncode=0,stdout='alive\n')
  self.assertEqual(m.probe('agent@remote',123,BOOT,456,run),'alive')
  args,opts=calls[0];self.assertEqual(args[:5],['ssh','-oBatchMode=yes','-oConnectTimeout=5','-T','--'])
  self.assertEqual(args[5],'agent@remote');self.assertTrue(args[6].endswith(' 123 '+BOOT+' 456'))
  self.assertEqual(opts['timeout'],12)
 def test_unknown_never_means_gone(self):
  for output,code in [('gone',1),('something',0),('',255)]:
   self.assertEqual(m.probe('host',123,BOOT,456,lambda *a,**k:SimpleNamespace(returncode=code,stdout=output)),'unknown')
  self.assertEqual(m.probe('host',123,BOOT,456,lambda *a,**k:(_ for _ in ()).throw(subprocess.TimeoutExpired('ssh',12))),'unknown')
 def test_rejects_options_shell_injection_and_invalid_identity(self):
  for host in ('-oProxyCommand=evil','host;touch-x','host name','host$(id)'):
   with self.assertRaises(ValueError):m.probe(host,1,BOOT,1)
  with self.assertRaises(ValueError):m.probe('host',1,'bad',1)
  with self.assertRaises(ValueError):m.probe('host',0,BOOT,1)
 def test_only_exact_disappearance_completes(self):
  for statuses,expected in [(['alive','gone'],0),(['unknown'],75)]:
   values=iter(statuses); clock=iter([0,1,2,3,4])
   self.assertEqual(m.watch('host',1,BOOT,2,5,30,lambda *a:next(values),lambda:next(clock),lambda _:None),expected)
 def test_identify_requires_valid_complete_identity(self):
  valid=SimpleNamespace(returncode=0,stdout='{"bootId":"'+BOOT+'","startTicks":456}')
  self.assertEqual(m.identify('remote',123,lambda *a,**k:valid),{'bootId':BOOT,'startTicks':456})
  for response in [SimpleNamespace(returncode=1,stdout=''),SimpleNamespace(returncode=0,stdout='{}'),SimpleNamespace(returncode=0,stdout='not-json')]:
   self.assertIsNone(m.identify('remote',123,lambda *a,**k:response))
 def test_remote_probe_code_checks_real_pid_identity_without_signalling(self):
  import os
  boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
  pid=os.getpid();start=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
  def run(ticks):
   return subprocess.run(['python3','-c',m.REMOTE,str(pid),boot,str(ticks)],capture_output=True,text=True,check=False)
  identity=subprocess.run(['python3','-c',m.REMOTE,str(pid),'-','-'],capture_output=True,text=True,check=False)
  import json
  self.assertEqual(json.loads(identity.stdout),{'bootId':boot,'startTicks':int(start)})
  self.assertEqual(run(start).stdout.strip(),'alive')
  self.assertEqual(run(int(start)+1).stdout.strip(),'gone')
 def test_deadline_does_not_claim_success(self):
  ticks=iter([0,2]);self.assertEqual(m.watch('host',1,BOOT,2,5,1,lambda *a:'alive',lambda:next(ticks),lambda _:None),75)

if __name__=='__main__':unittest.main()
