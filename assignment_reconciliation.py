"""Retire exact superseded instructions, without claiming their execution/outcome."""
import hashlib
import json
import re
import time
from assignment_delivery import _binding, _number, _record, inspect_assignment, describe_message
from public_history import read_inputs
from queued_input import input_hash, native_queue
from resource_handoff import current_user_authorization


def assignment_hash(assignment):
    """Hash exact saved assignment text as UTF-8, including all appended context."""
    return hashlib.sha256(assignment['text'].encode('utf-8')).hexdigest()


def supersede_assignment(s, request):
    if not current_user_authorization(request):
        raise ValueError('Supersession requires current explicit user authorization and basis')
    if type(request.get('commit', False)) is not bool:
        raise ValueError('commit must be a boolean')
    for key in ('worker', 'session', 'native_id', 'assignment', 'assignment_hash', 'superseded_by',
                'replacement_local_id', 'replacement_hash', 'resource', 'reason'):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise ValueError('Exact assignment/replacement/resource review required: ' + key)
    if len(request['reason'].strip()) < 16:
        raise ValueError('Record the semantic supersession reason')
    w = s.worker(request['worker'])
    a = s.one('SELECT * FROM assignments WHERE id=?', (request['assignment'],))
    _binding(s, w, a)
    if request['session'] != w['session'] or request['native_id'] != w['native_id']:
        raise ValueError('Supersession binding changed')
    if assignment_hash(a) != request['assignment_hash']:
        raise ValueError('Assignment content changed')
    if a.get('goal') or re.match(r'^/[A-Za-z][\w-]*(?:\s|$)', a['text'].lstrip()):
        raise ValueError('Goal/control assignments cannot be superseded with this operation')
    if not request['resource'].startswith(('main/GPU-', 'render-pc/GPU-')):
        raise ValueError('Exact GPU allocation resource required')
    raw = s.get('assignment-supersession:' + a['id'])
    if a['state'] == 'superseded':
        audit = json.loads(raw or '{}')
        if all(audit.get(k) == request[k] for k in ('worker', 'session', 'native_id', 'assignment', 'assignment_hash',
                'superseded_by', 'replacement_local_id', 'replacement_hash', 'resource')):
            return {**audit, 'alreadySuperseded': True}
        raise ValueError('Assignment already superseded under a different review')
    if a['state'] != 'sent' or raw:
        raise ValueError('Only settled sent instructions can be reviewed; sending/uncertain dispatch requires reconciliation')
    if request.get('commit') and s.manager_paused():
        raise ValueError('Manager manually paused; no reconciliation committed')
    resource = s.one('SELECT * FROM resources WHERE name=?', (request['resource'],))
    if resource.get('project') != w['project']:
        raise ValueError('Reviewed resource is not held by this worker project')
    saved_intent = s.get('assignment-input:' + a['id'])

    def evidence():
        _binding(s, w, a)
        messages, coverage = read_inputs(s.api, w['session'], message_ids=[request['superseded_by']])
        replacement = next((m for m in messages if m['id'] == request['superseded_by']), None)
        if not replacement or replacement.get('localId') != request['replacement_local_id'] or input_hash(replacement) != request['replacement_hash']:
            raise ValueError('Exact replacement input content/identity unavailable or changed')
        if _record(replacement).get('role') != 'user' or describe_message(replacement)['control']:
            raise ValueError('Replacement must be an ordinary actual user input, not saved goal/control metadata')
        created = replacement.get('createdAt')
        if not _number(created) or created <= a['created'] * 1000 or replacement.get('scheduledAt') is not None:
            raise ValueError('Replacement must follow the original assignment and be unscheduled')
        proof = s.api.call('/sessions/' + w['session'] + '/messages/queued-state',
                           {'localIds': [a['id'], request['replacement_local_id']]})
        if not all(isinstance(proof.get(k), list) for k in ('queuedLocalIds', 'indeterminateLocalIds', 'invokedLocalMessages')):
            raise ValueError('Queue/receipt inspection incomplete')
        if any(i in proof['queuedLocalIds'] or i in proof['indeterminateLocalIds'] for i in (a['id'], request['replacement_local_id'])):
            raise ValueError('Original/replacement input is queued or uncertain; supersession never discards live input')
        receipts = [r for r in proof['invokedLocalMessages'] if r.get('localId') == request['replacement_local_id'] and _number(r.get('invokedAt'))]
        if len(receipts) != 1 or not created <= receipts[0]['invokedAt'] <= time.time() * 1000:
            raise ValueError('Replacement requires one authoritative exact intake receipt')
        old = inspect_assignment(s, w, a, historical=True)
        if old['state'] in ('queued', 'indeterminate') or old.get('reason'):
            raise ValueError('Original delivery conflicts with safe supersession review')
        # Missing old receipt is not consumption/cancellation. Independently
        # verify no live native queued copy can later apply the obsolete scope.
        if w['runtime'] != 'codex':
            raise ValueError('Exact unknown-delivery reconciliation currently supports Codex only')
        queue = native_queue(s, {'worker': w['id']})
        if any(item.get('clientUserMessageId') in (a['id'], request['replacement_local_id']) for item in queue['items']):
            raise ValueError('Original/replacement input remains in native queue')
        if (s.worker(w['id']) != w or s.one('SELECT * FROM assignments WHERE id=?', (a['id'],)) != a
                or s.get('assignment-input:' + a['id']) != saved_intent):
            raise ValueError('Assignment or binding changed during review')
        _binding(s, w, a)
        final_messages, _ = read_inputs(s.api, w['session'], message_ids=[request['superseded_by']])
        final_replacement = next((m for m in final_messages if m['id'] == request['superseded_by']), None)
        if (not final_replacement or input_hash(final_replacement) != request['replacement_hash']
                or final_replacement.get('localId') != request['replacement_local_id']
                or final_replacement.get('createdAt') != created):
            raise ValueError('Replacement changed during review')
        return {'replacementMessage': replacement['id'], 'replacementInvokedAt': receipts[0]['invokedAt'],
                'replacementCreatedAt': created, 'priorDelivery': old, 'historyCoverage': coverage,
                'nativeQueueVerifiedAbsent': True}

    proof = evidence()
    report = {k: request[k] for k in ('worker', 'session', 'native_id', 'assignment', 'assignment_hash',
              'superseded_by', 'replacement_local_id', 'replacement_hash', 'resource', 'reason', 'basis')}
    report.update(ready=True, committed=False, state='superseded', priorAssignmentState=a['state'], **proof,
                  outcome='unverified', deliveryUnchanged=True, goalUnchanged=True)
    if not request.get('commit'):
        return report
    fresh = evidence()
    # Newer activity is allowed, changed exact intake/old receipt is not.
    for key in ('replacementMessage', 'replacementInvokedAt', 'replacementCreatedAt'):
        if fresh[key] != proof[key]:
            raise ValueError('Receipt changed during review; ownership and assignment unchanged')
    for key in ('state', 'messageId', 'localId', 'invokedAt', 'control', 'bindingVerified', 'native_id'):
        if fresh['priorDelivery'].get(key) != proof['priorDelivery'].get(key):
            raise ValueError('Original delivery changed during review')
    if s.manager_paused() or not current_user_authorization(request):
        raise ValueError('Manager paused or current authorization expired during review')
    report.update(committed=True, at=time.time(), actor=request.get('actor'))
    s.db.execute('BEGIN IMMEDIATE')
    try:
        if (s.one('SELECT * FROM assignments WHERE id=?', (a['id'],)) != a
                or s.worker(w['id']) != w or s.one('SELECT * FROM resources WHERE name=?', (request['resource'],)) != resource):
            raise ValueError('Assignment changed before reconciliation commit')
        changed = s.db.execute("UPDATE assignments SET state='superseded' WHERE id=? AND state='sent'", (a['id'],)).rowcount
        if changed != 1:
            raise ValueError('Assignment state changed')
        s.set('assignment-supersession:' + a['id'], json.dumps(report))
        s.set('assignment-supersession-audit:' + a['id'] + ':' + str(time.time_ns()), json.dumps(report))
        s.db.execute('COMMIT')
    except Exception:
        s.db.execute('ROLLBACK')
        raise
    return report
