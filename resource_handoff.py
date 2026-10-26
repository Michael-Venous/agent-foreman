"""Explicit, evidence-reviewed GPU lease handoff; never changes work or goals."""
from coordination import is_instruction_message
import csv
import io
import json
import math
import hashlib
import os
import shlex
import re
import subprocess
import time
from pathlib import Path


def current_user_authorization(request):
    stamp = request.get('requested_at')
    return (request.get('user_requested') is True
            and isinstance(request.get('basis'), str) and bool(request['basis'].strip())
            and type(stamp) in (int, float) and math.isfinite(stamp)
            and 0 <= time.time() - stamp <= 300)


def chrome_gpu_identity(pid):
    """Identify one installed Chrome GPU child, never a process-name exemption.

    This is review evidence only. Without a fresh allocation-specific approval
    even a verified Chrome client continues to block the strict compute guard.
    """
    try:
        proc = Path('/proc') / str(pid)
        before = (proc / 'stat').read_text()
        fields = before.rsplit(') ', 1)[1].split()
        executable = str((proc / 'exe').resolve(strict=True))
        if executable != '/opt/google/chrome/chrome' or proc.stat().st_uid != os.getuid():
            return None
        argv_bytes = (proc / 'cmdline').read_bytes()
        argv = argv_bytes.rstrip(b'\0').split(b'\0')
        # Chrome rewrites argv to a single process-title string on Linux.
        tokens = shlex.split(argv[0].decode()) if len(argv) == 1 else [a.decode() for a in argv]
        if not tokens or tokens[0] != executable or tokens.count('--type=gpu-process') != 1:
            return None
        parent = Path('/proc') / fields[1]
        parent_stat = (parent / 'stat').read_text()
        parent_fields = parent_stat.rsplit(') ', 1)[1].split()
        if (str((parent / 'exe').resolve(strict=True)) != executable
                or parent.stat().st_uid != os.getuid()):
            return None
        binary = (proc / 'exe').stat()
        identity = {'pid': pid, 'bootId': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                    'startTicks': fields[19], 'executable': executable,
                    'executableDevice': binary.st_dev, 'executableInode': binary.st_ino,
                    'executableMtimeNs': binary.st_mtime_ns, 'executableSize': binary.st_size,
                    'commandHash': hashlib.sha256(argv_bytes).hexdigest(),
                    'parentPid': int(fields[1]), 'parentStartTicks': parent_fields[19]}
        if ((proc / 'stat').read_text().rsplit(') ', 1)[1].split()[19] != fields[19]
                or (parent / 'stat').read_text().rsplit(') ', 1)[1].split()[19] != parent_fields[19]):
            return None
        return identity
    except (OSError, ValueError, IndexError):
        return None


def reviewed_gpu_snapshot(gpu, request):
    """Allow only explicitly reviewed identities, retaining actual busy evidence."""
    approvals = request.get('incidental_clients', [])
    if not isinstance(approvals, list) or len(approvals) > 4:
        raise ValueError('incidental_clients must be a list of at most four exact process reviews')
    if not approvals:
        return {**gpu, 'handoffBlocked': gpu['busy'], 'incidentalClients': []}
    if not current_user_authorization(request):
        raise ValueError('Incidental client sharing requires current explicit user authorization and basis')
    reviewed = []; pids = set()
    for approval in approvals:
        if not isinstance(approval, dict) or type(approval.get('pid')) is not int or approval['pid'] <= 0:
            raise ValueError('Each incidental review requires an exact positive PID')
        pid = approval['pid']
        if pid in pids or approval.get('resource') != gpu['resource']:
            raise ValueError('Duplicate or wrong-GPU incidental client review')
        purpose = approval.get('purpose')
        if not isinstance(purpose, str) or len(purpose.strip()) < 8:
            raise ValueError('Incidental client review requires its actual reviewed purpose')
        process = next((p for p in gpu['processes'] if p['pid'] == pid), None)
        identity = process.get('incidentalIdentity') if process else None
        if not identity or approval.get('identity') != identity:
            raise ValueError('Incidental Chrome identity changed or is unverified; inspect a fresh GPU snapshot')
        pids.add(pid)
        reviewed.append({'pid': pid, 'resource': gpu['resource'], 'identity': identity, 'purpose': purpose.strip()})
    return {**gpu, 'incidentalClients': reviewed,
            'handoffBlocked': any(not p['desktop'] and p['pid'] not in pids for p in gpu['processes'])}


def review_input_receipts(s, session_id, page, release_at):
    """Resolve a stale page only from authoritative intake receipts, never prose.

    Native queue absence is ambiguous; missing receipts and all genuine queued
    input still block. This never cancels input or manufactures consumption.
    """
    from supervisor import content
    pending = [m for m in page if content(m).get('role') == 'user' and m.get('localId') and not m.get('invokedAt')]
    if not pending:
        return []
    ids = [m['localId'] for m in pending]
    states = s.api.call('/sessions/' + session_id + '/messages/queued-state', {'localIds': ids})
    if states.get('queuedLocalIds') != [] or states.get('indeterminateLocalIds') != []:
        raise ValueError('Pending input, including peer notices; inspect queue before handoff: ' + ','.join(ids))
    receipts = states.get('invokedLocalMessages')
    if not isinstance(receipts, list):
        raise ValueError('Pending input has no authoritative intake receipts')
    reviewed = []
    for message in pending:
        matches = [r for r in receipts if isinstance(r, dict) and r.get('localId') == message['localId']]
        when = matches[0].get('invokedAt') if len(matches) == 1 else None
        if type(when) not in (int, float) or not math.isfinite(when) or when <= 0:
            raise ValueError('Pending input has no unique authoritative intake receipt: ' + message['localId'])
        if when < message.get('createdAt', 0) or when / 1000 > release_at:
            raise ValueError('Release evidence must follow confirmed intake of pending input')
        reviewed.append({'message': message['id'], 'localId': message['localId'], 'invokedAt': when})
    return reviewed


def desktop_compositor(pid, name):
    if name != '/usr/bin/kwin_wayland':
        return False
    proc = Path('/proc') / str(pid)
    try:
        return str((proc / 'exe').resolve(strict=True)) == name
    except OSError:
        # Hardened KWin denies exe inspection. Its systemd service membership is
        # independently readable; don't elevate or weaken process protections.
        try:
            result = subprocess.run(['systemctl', '--user', 'show', 'plasma-kwin_wayland.service',
                                     '--property=ControlGroup', '--value'], capture_output=True, text=True, timeout=5, check=True)
            group = result.stdout.strip()
            return bool(group.endswith('/plasma-kwin_wayland.service')
                        and (proc / 'comm').read_text().strip() == 'kwin_wayland'
                        and ('0::' + group) in (proc / 'cgroup').read_text().splitlines())
        except (OSError, subprocess.SubprocessError):
            return False


def gpu_snapshot(resource):
    """Inspect local CUDA/compute clients. Desktop graphics alone are not a lease."""
    if not re.fullmatch(r'main/GPU-[0-9a-fA-F-]{36}', resource):
        raise ValueError('Only exact main/GPU-UUID local compute leases are supported')
    uuid = resource.split('/', 1)[1]
    def query(fields, kind):
        result = subprocess.run(['nvidia-smi', '-i', uuid, '--query-' + kind + '=' + fields,
                                 '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=10, check=True)
        return list(csv.reader(io.StringIO(result.stdout), skipinitialspace=True))
    gpus = query('uuid,name', 'gpu')
    if len(gpus) != 1 or len(gpus[0]) != 2 or gpus[0][0] != uuid:
        raise ValueError('GPU identity could not be verified')
    processes = []
    for row in query('gpu_uuid,pid,process_name', 'compute-apps'):
        if len(row) != 3 or row[0] != uuid or not row[1].isdigit():
            raise ValueError('GPU process evidence is incomplete')
        pid = int(row[1]); desktop = desktop_compositor(pid, row[2])
        process = {'pid': pid, 'name': row[2], 'desktop': desktop}
        if not desktop:
            identity = chrome_gpu_identity(pid)
            if identity:
                process['incidentalIdentity'] = identity
                # NVIDIA may return Chrome's entire argv; retain only the executable.
                process['name'] = identity['executable']
        processes.append(process)
    return {'resource': resource, 'name': gpus[0][1], 'observedAt': time.time(),
            'processes': processes, 'busy': any(not p['desktop'] for p in processes)}


def release_page(s, session, message):
    """Exact still-fresh release may scroll out during continuing CPU work."""
    page = s.api.call('/sessions/' + session + '/messages?limit=100')['messages']
    if any(m.get('id') == message for m in page):
        return page
    from public_history import read_inputs
    messages, _ = read_inputs(s.api, session, message_ids=[message])
    return messages


def public_reply(message, *, include_terminal=False):
    from supervisor import content
    c = content(message); inner = c.get('content', {})
    if c.get('role') != 'agent' or not isinstance(inner, dict):
        return ''
    # Runtime event messages (goal/usage JSON, ready/error notices) are control
    # output, even when their data.type is 'message'. Determine provenance from
    # the envelope; an assistant may legitimately reply with JSON of any shape.
    if inner.get('type') not in ('text', 'codex', 'output'):
        return ''
    data = inner.get('data') if isinstance(inner.get('data'), dict) else {}
    msg = data.get('message') if isinstance(data.get('message'), dict) else {}
    if data.get('isSidechain') or data.get('type') in ('user', 'reasoning', 'thinking', 'agent_reasoning', 'reasoning_delta') or msg.get('role') == 'user':
        return ''
    if data.get('live'):
        return ''
    if inner.get('type') == 'text':
        return inner['text'] if isinstance(inner.get('text'), str) else ''
    if data.get('type') == 'message' and isinstance(data.get('message'), str):
        return data['message']
    if data.get('type') == 'assistant':
        parts = msg.get('content') if isinstance(msg.get('content'), list) else []
        return '\n'.join(p.get('text', '') for p in parts if isinstance(p, dict) and p.get('type') == 'text')
    # Terminal snapshots can repeat an older final reply. Summaries may use
    # that field, but it must not become fresh public coordination evidence.
    if include_terminal and data.get('type') == 'task_complete' and isinstance(data.get('last_agent_message'), str):
        return data['last_agent_message']
    return ''



def handoff_runtime_identity(session):
    """Compare safety-relevant state, excluding harmless heartbeat timestamps."""
    from supervisor import native
    agent = session.get('agentState') or {}
    metadata = session.get('metadata') or {}
    return {'native': native(session), 'active': session.get('active'), 'thinking': session.get('thinking'),
            'manualPausedAt': session.get('manualPausedAt'), 'backgroundTaskCount': session.get('backgroundTaskCount',0),
            'hostPid': metadata.get('hostPid'), 'machineId': metadata.get('machineId'), 'path': metadata.get('path'),
            'requests': agent.get('requests'), 'ready': agent.get('claudeReadyMessages'),
            'controlledByUser': agent.get('controlledByUser')}


def personal_reservation(s, resource):
    record = json.loads(s.get('resource-reservation:' + resource) or 'null')
    return record if record and record.get('reserved') else None


def assert_resource_available(s, resource):
    if personal_reservation(s, resource):
        raise ValueError('Resource personally reserved by user; only a new explicit user release can remove this reservation')


def resource_reservation(s, request):
    """Coordinator-only user reservation. Never probes, starts, stops or transfers a job."""
    resource = request.get('resource', '')
    if not re.fullmatch(r'main/GPU-[0-9a-fA-F-]{36}', resource):
        raise ValueError('Exact main/GPU-UUID required')
    key = 'resource-reservation:' + resource
    old = json.loads(s.get(key) or 'null')
    if 'reserved' not in request:
        return old
    stamp = request.get('requested_at'); basis = request.get('basis')
    if (type(request['reserved']) is not bool or request.get('user_requested') is not True
        or not isinstance(basis, str) or not basis.strip() or not isinstance(stamp, (int, float))
        or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= 300):
        raise ValueError('Reservation changes require current explicit user authorization, basis and fresh managerctl submission')
    reserved = request['reserved']
    s.db.execute('BEGIN IMMEDIATE')
    try:
        owner = s.rows('SELECT project FROM resources WHERE name=?', (resource,))
        prior = owner[0]['project'] if owner else None
        record = {'resource': resource, 'reserved': reserved, 'owner': 'user/personal' if reserved else None,
                  'previousProject': prior if prior is not None else (old or {}).get('previousProject'),
                  'basis': basis.strip(), 'at': time.time()}
        if reserved:
            # A NULL project is deliberately not a lease held by any worker project.
            s.db.execute('INSERT INTO resources(name,project) VALUES(?,NULL) ON CONFLICT(name) DO UPDATE SET project=NULL', (resource,))
        elif old and old.get('reserved'):
            # Releasing personal reservation never restores an old project or schedules work.
            s.db.execute('DELETE FROM resources WHERE name=? AND project IS NULL', (resource,))
        s.set(key, json.dumps(record))
        s.set(key + ':audit:' + str(time.time_ns()), json.dumps(record))
        s.db.execute('COMMIT')
    except Exception:
        s.db.execute('ROLLBACK'); raise
    return record


def resource_handoff(supervisor, request):
    """Read-only by default. Commit atomically changes a single recorded claim."""
    from supervisor import content, native
    s = supervisor
    source = s.project(request['project'])['id']
    target = s.project(request['to_project'])['id']
    if source == target:
        raise ValueError('Source and destination projects must differ')
    assert_resource_available(s, request['resource'])
    resource = request['resource']; commit = request.get('commit') is True
    basis = request.get('basis'); stamp = request.get('requested_at')
    if commit and (request.get('user_requested') is not True or not isinstance(basis, str) or not basis.strip()
                   or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= 300):
        raise ValueError('Commit requires current user authorization, basis and fresh managerctl submission')
    owner = s.rows('SELECT project FROM resources WHERE name=?', (resource,))
    if not owner or owner[0]['project'] != source:
        raise ValueError('Resource owner changed; inspect before handoff')
    # Preserve even historical goal assignments: a resource lease is not a task outcome.
    workers = s.rows("SELECT DISTINCT w.* FROM workers w LEFT JOIN assignments a ON a.worker=w.id WHERE w.project=? AND (w.state IN ('managed','launching','adopting') OR a.state IN ('sent','sending','uncertain'))", (source,))
    evidence = request.get('acknowledgements', [])
    if not isinstance(evidence, list) or any(not isinstance(e, dict) for e in evidence):
        raise ValueError('acknowledgements must be a list')
    if len({e.get('worker') for e in evidence}) != len(evidence):
        raise ValueError('Duplicate worker acknowledgement')
    if any(e.get('worker') not in {w['id'] for w in workers} for e in evidence):
        raise ValueError('Acknowledgement is not for a current source worker')
    blockers = []; reviewed = []; snapshots = {}; message_pages = {}; assignment_rows = {}
    if not workers:
        blockers.append({'reason': 'No bound source workers to verify; use ordinary release after review'})
    for w in workers:
        try:
            if not w['session'] or w['state'] in ('launching', 'adopting'):
                raise ValueError('Unresolved controller binding')
            current = s.session(w['session'])
            if not w['native_id'] or native(current) != w['native_id']:
                raise ValueError('Exact native identity must match')
            snapshots[w['id']] = current
            agent = current.get('agentState') or {}
            if agent.get('requests') or agent.get('claudeReadyMessages') or agent.get('controlledByUser'):
                raise ValueError('Pending input/ready messages or external control require review')
            ack = next((e for e in evidence if e.get('worker') == w['id']), None)
            if not ack or ack.get('session') != w['session'] or ack.get('native_id') != w['native_id']:
                raise ValueError('Fresh relinquishment acknowledgement with exact worker/session/native identity required')
            # Paginate to exact evidence; age, newer instructions and receipt guards still apply.
            page = release_page(s, w['session'], ack.get('message'))
            message_pages[w['id']] = page
            message = next((m for m in page if m['id'] == ack.get('message')), None)
            quote = ack.get('quote')
            if not message or not isinstance(quote, str) or len(quote.strip()) < 16 or quote not in public_reply(message):
                raise ValueError('Quoted release evidence must match a current public assistant reply')
            when = message.get('createdAt', 0) / 1000
            if not 0 <= time.time() - when <= 1800:
                raise ValueError('Release evidence expired; inspect current worker output')
            intake_review = review_input_receipts(s, w['session'], page, when)
            if any(is_instruction_message(m) and (m.get('createdAt', 0) >= message['createdAt'] or m.get('seq', 0) > message.get('seq', 0)) for m in page):
                raise ValueError('A newer user/manager instruction invalidates this acknowledgement')
            assignments = s.rows("SELECT * FROM assignments WHERE worker=? AND state IN ('sent','sending','uncertain')", (w['id'],))
            assignment_rows[w['id']] = assignments
            if any(a['state'] != 'sent' for a in assignments):
                raise ValueError('Dispatch sending/uncertain; reconcile delivery first')
            if assignments:
                q = s.api.call('/sessions/' + w['session'] + '/messages/queued-state', {'localIds': [a['id'] for a in assignments]})
                if q.get('queuedLocalIds') != [] or q.get('indeterminateLocalIds') != []:
                    raise ValueError('Queued or uncertain assignment delivery')
                invoked = {x['localId']: x.get('invokedAt', 0) / 1000 for x in q.get('invokedLocalMessages', [])}
                if any(not invoked.get(a['id']) or not max(a['created'], invoked[a['id']]) <= when for a in assignments):
                    raise ValueError('Release evidence must follow confirmed invocation of every outstanding assignment')
            reviewed.append({'worker': w['id'], 'session': w['session'], 'native_id': w['native_id'],
                             'message': message['id'], 'quote': quote, 'createdAt': message['createdAt'],
                             'preservedAssignments': [a['id'] for a in assignments], 'intakeReceipts': intake_review})
        except (ValueError, KeyError, TypeError) as error:
            blockers.append({'worker': w['id'], 'reason': str(error)})
    gpu = reviewed_gpu_snapshot(gpu_snapshot(resource), request)
    if gpu['handoffBlocked']:
        blockers.append({'resource': resource, 'reason': 'GPU still has compute clients; no handoff'})
    report = {'ready': not blockers, 'committed': False, 'resource': resource, 'project': source,
              'to_project': target, 'blockers': blockers, 'gpu': gpu, 'acknowledgements': reviewed,
              'leaseMode': 'shared-with-reviewed-incidental-clients' if gpu['incidentalClients'] else 'compute-reservation'}
    if not commit or blockers:
        return report
    # No prompts, scans, resume/abort, goal edits or assignment outcome changes.
    # Reread runtime versions before the last GPU probe and atomic owner update.
    for w in workers:
        ack = next(e for e in evidence if e.get('worker') == w['id'])
        latest = release_page(s, w['session'], ack.get('message'))
        outstanding = s.rows("SELECT * FROM assignments WHERE worker=? AND state IN ('sent','sending','uncertain')", (w['id'],))
        if s.worker(w['id']) != w or handoff_runtime_identity(s.session(w['session'])) != handoff_runtime_identity(snapshots[w['id']]) or latest != message_pages[w['id']] or outstanding != assignment_rows[w['id']]:
            raise ValueError('Worker changed during review; retry with fresh evidence')
    gpu = reviewed_gpu_snapshot(gpu_snapshot(resource), request)
    if gpu['handoffBlocked']:
        raise ValueError('GPU became busy during handoff; ownership unchanged')
    report.update(committed=True, gpu=gpu, basis=basis.strip(), at=time.time())
    s.db.execute('BEGIN IMMEDIATE')
    try:
        assert_resource_available(s, resource)
        changed = s.db.execute('UPDATE resources SET project=? WHERE name=? AND project=?', (target, resource, source)).rowcount
        if changed != 1:
            raise ValueError('Resource owner changed during review')
        from coordination import complete_watches
        complete_watches(s, source, target, resource)
        # Durable audit without a wake/nudge to either worker or manager.
        s.set('resource-handoff:' + resource, json.dumps(report))
        s.set('resource-handoff-audit:' + resource + ':' + str(time.time_ns()), json.dumps(report))
        s.db.execute('COMMIT')
    except Exception:
        s.db.execute('ROLLBACK')
        raise
    return report
