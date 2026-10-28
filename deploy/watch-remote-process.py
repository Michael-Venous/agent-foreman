#!/usr/bin/env python3
"""Read-only bounded watcher for a known Linux process on an SSH host.

No process or machine is modified. Exit 0 means the specific process is gone;
75 means observation failed/timed out. Neither says whether its work succeeded.
"""
import argparse
import re
import shlex
import subprocess
import sys
import time

REMOTE = r'''import json,os,pathlib,sys
pid,expected_boot,expected_start=sys.argv[1:]
try:
 boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
except OSError:
 print('unknown');sys.exit(75)
if expected_boot!='-' and boot!=expected_boot:
 print('gone');sys.exit(0)
try:
 folder=pathlib.Path('/proc',pid)
 if folder.stat().st_uid!=os.getuid():
  print('unknown');sys.exit(75)
 stat=(folder/'stat').read_text()
 fields=stat.rsplit(')',1)[1].split()
 if expected_boot=='-' and expected_start=='-':
  if fields[0] in ('Z','X','x'):
   print('unknown');sys.exit(75)
  print(json.dumps({'bootId':boot,'startTicks':int(fields[19])}));sys.exit(0)
 if fields[19]!=expected_start or fields[0] in ('Z','X','x'):
  print('gone')
 else: print('alive')
except FileNotFoundError:
 print('gone')
except (OSError,IndexError,ValueError):
 print('unknown');sys.exit(75)
'''

def valid_host(host):
    # No ssh options or shell metacharacters; aliases, user@host and IPv6 are OK.
    return bool(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,253}', host))

def identify(host, pid, runner=subprocess.run):
    """Return identity only for a currently live same-user process on that host."""
    if not valid_host(host) or not isinstance(pid, int) or pid <= 0:
        raise ValueError('Invalid host or PID')
    remote = ' '.join(shlex.quote(x) for x in ['python3', '-c', REMOTE, str(pid), '-', '-'])
    try:
        result = runner(['ssh','-oBatchMode=yes','-oConnectTimeout=5','-T','--',host,remote],
                        capture_output=True, text=True, timeout=12, check=False)
        if result.returncode != 0: return None
        import json
        data = json.loads(result.stdout)
        boot = data['bootId']; start = data['startTicks']
        if not isinstance(boot, str) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', boot): return None
        if not isinstance(start, int) or isinstance(start, bool) or start <= 0: return None
        return {'bootId': boot, 'startTicks': start}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None

def probe(host, pid, boot_id, start_ticks, runner=subprocess.run):
    if not valid_host(host) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', boot_id):
        raise ValueError('Invalid host or boot ID')
    if not isinstance(pid, int) or pid <= 0 or not isinstance(start_ticks, int) or start_ticks <= 0:
        raise ValueError('PID and start ticks must be positive integers')
    # OpenSSH invokes a remote shell; quote every remote argument individually.
    remote = ' '.join(shlex.quote(x) for x in ['python3', '-c', REMOTE, str(pid), boot_id, str(start_ticks)])
    try:
        result = runner(['ssh','-oBatchMode=yes','-oConnectTimeout=5','-T','--',host,remote],
                        capture_output=True, text=True, timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'
    value = result.stdout.strip()
    return value if result.returncode == 0 and value in ('alive','gone') else 'unknown'

def watch(host, pid, boot_id, start_ticks, interval, timeout, probe_fn=probe, clock=time.monotonic, sleep=time.sleep):
    deadline=clock()+timeout
    while True:
        state=probe_fn(host,pid,boot_id,start_ticks)
        if state == 'gone':
            print('The identified process is absent from the observed process namespace. Job success is unconfirmed.',flush=True)
            return 0
        if state == 'unknown':
            print('Remote process status is unconfirmed; SSH or process inspection failed.',flush=True)
            return 75
        remaining=deadline-clock()
        if remaining<=0:
            print('Watcher expired; remote process status is unconfirmed.',flush=True)
            return 75
        sleep(min(interval,remaining))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True);parser.add_argument('--pid',required=True,type=int)
    parser.add_argument('--identify',action='store_true');parser.add_argument('--boot-id');parser.add_argument('--start-ticks',type=int)
    parser.add_argument('--interval',type=int,default=30);parser.add_argument('--timeout',type=int,default=21600)
    args=parser.parse_args()
    if args.identify:
        if args.boot_id or args.start_ticks:parser.error('--identify cannot be combined with identity arguments')
        import json
        try: identity=identify(args.host,args.pid)
        except ValueError as error:parser.error(str(error))
        if identity is None:
            print('Remote identity is unconfirmed.',file=sys.stderr);return 75
        print(json.dumps(identity));return 0
    if not args.boot_id or not args.start_ticks:parser.error('provide --boot-id and --start-ticks, or use --identify')
    if not 5<=args.interval<=3600 or not 1<=args.timeout<=86400:parser.error('interval must be 5–3600 seconds; timeout must be 1–86400 seconds')
    try:return watch(args.host,args.pid,args.boot_id,args.start_ticks,args.interval,args.timeout)
    except ValueError as error:parser.error(str(error))

if __name__=='__main__':sys.exit(main())
