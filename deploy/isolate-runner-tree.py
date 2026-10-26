#!/usr/bin/env python3
"""Move a reviewed live runner child tree to a scope without signalling it.

Requires exact PID generation and source cgroup. Never starts/resumes a chat,
changes its inputs, or moves processes already in other units. New controllers
isolate themselves at startup; this is a one-time migration for older trees.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess


def identity(pid):
    p = Path('/proc') / str(pid)
    # comm may contain spaces/parentheses; starttime is field22 after last ')'.
    stat = (p / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'ppid': int(stat[1]), 'start': stat[19],
            'cgroup': (p / 'cgroup').read_text().strip().split('::', 1)[-1],
            'uid': p.stat().st_uid}


def tree(root):
    rows = {}
    for p in Path('/proc').iterdir():
        if p.name.isdigit():
            try: rows[int(p.name)] = identity(int(p.name))
            except (OSError, ValueError, IndexError): pass
    selected = {root}
    while True:
        expanded = selected | {pid for pid, row in rows.items() if row['ppid'] in selected}
        if expanded == selected: break
        selected = expanded
    return [rows[pid] for pid in sorted(selected) if pid in rows]


def migrate(pid, start, source, call=subprocess.run):
    root = identity(pid)
    if root['start'] != str(start) or root['cgroup'] != source or root['uid'] != os.getuid():
        raise ValueError('Root PID generation, owner or source cgroup changed; no migration')
    if not source.endswith('/hapi-runner.service'):
        raise ValueError('Only a reviewed runner child tree may be migrated')
    runner_pid = int(subprocess.check_output(['systemctl', '--user', 'show', 'hapi-runner.service', '-p', 'MainPID', '--value'], text=True))
    if pid == runner_pid or root['ppid'] != runner_pid:
        raise ValueError('Expected a direct runner child, never the runner itself')
    unit = f'hapi-session-migrated-{pid}-{start}.scope'
    moved = []
    for attempt in range(8):
        if identity(pid)['start'] != str(start):
            raise ValueError('Root exited/reused during migration; inspect result before retry')
        candidates = [r for r in tree(pid) if r['cgroup'] == source and r['uid'] == os.getuid()]
        # Revalidate each generation immediately before the scoped mutation.
        candidates = [r for r in candidates if identity(r['pid']) == r]
        if not candidates:
            return {'unit': unit, 'root': pid, 'moved': moved, 'verified': True}
        ids = [str(r['pid']) for r in candidates]
        prefix = ['busctl', '--user', 'call', 'org.freedesktop.systemd1', '/org/freedesktop/systemd1', 'org.freedesktop.systemd1.Manager']
        args = (['StartTransientUnit', 'ssa(sv)a(sa(sv))', unit, 'fail', '3', 'PIDs', 'au', str(len(ids)), *ids,
                 'Description', 's', 'HAPI migrated independent controller and jobs', 'CollectMode', 's', 'inactive-or-failed', '0']
                if attempt == 0 else ['AttachProcessesToUnit', 'ssau', unit, '', str(len(ids)), *ids])
        call(prefix + args, check=True, capture_output=True, text=True, timeout=15)
        if attempt == 0:
            # Join the asynchronous StartTransientUnit job before rescanning.
            call(['systemctl', '--user', 'start', unit], check=True, capture_output=True, text=True, timeout=15)
        moved.extend(r['pid'] for r in candidates)
    raise RuntimeError('Tree kept spawning during migration; migration partial, inspect before retry')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--expected-start', required=True)
    parser.add_argument('--source-cgroup', required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.pid, args.expected_start, args.source_cgroup), indent=2))
