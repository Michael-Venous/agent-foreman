"""Exact queued-input review and acknowledged supersession; never fake intake."""
import hashlib
import json
import time
import subprocess
from pathlib import Path
from urllib.parse import quote
from resource_handoff import current_user_authorization


def input_hash(message):
    from supervisor import content
    return hashlib.sha256(json.dumps(content(message).get('content'), sort_keys=True,
                                     separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def native_queue(s, request):
    """Read native queue via existing authenticated runtime; no history scan."""
    from supervisor import native
    w = s.worker(request['worker'])
    if w['runtime'] != 'codex' or not w['session'] or not w['native_id']:
        raise ValueError('Native queue listing requires a bound existing Codex worker')
    current = s.session(w['session'])
    if current.get('id') != w['session'] or native(current) != w['native_id']:
        raise ValueError('Native queue binding changed')
    root = Path(__file__).resolve().parent
    try:
        result = subprocess.run([str(Path.home()/'.bun/bin/bun'), str(root/'deploy/read-native-queue.ts'),
                                 w['session'], w['native_id']], cwd=root/'vendor/hapi-source/cli',
                                capture_output=True, text=True, timeout=60, check=True)
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError('Native queue read failed; no thread loaded or input changed') from None
    latest = s.session(w['session'])
    if (not isinstance(value, dict) or latest.get('id') != w['session'] or native(latest) != w['native_id']
            or s.worker(w['id']) != w or value.get('session') != w['session'] or value.get('native_id') != w['native_id']
            or value.get('readOnly') is not True or not isinstance(value.get('items'), list)):
        raise ValueError('Native queue response/binding changed')
    return {**value, 'worker': w['id']}


def bound_input(s, request):
    from supervisor import native
    w = s.worker(request['worker'])
    if not w['session']:
        raise ValueError('No bound worker session')
    session = s.session(w['session'])
    if session.get('id') != w['session'] or not w['native_id'] or native(session) != w['native_id']:
        raise ValueError('Worker native/session binding changed')
    if request.get('session') not in (None, w['session']) or request.get('native_id') not in (None, w['native_id']):
        raise ValueError('Requested input binding changed')
    from public_history import read_inputs
    wanted = [request[k] for k in ('message', 'superseded_by') if request.get(k)]
    messages, _ = read_inputs(s.api, w['session'], message_ids=wanted)
    return w, messages



def review(s, request):
    from supervisor import content
    w, messages = bound_input(s, request)
    inputs = [m for m in messages if content(m).get('role') == 'user' and m.get('localId')]
    if request.get('message'):
        inputs = [m for m in inputs if m['id'] == request['message']]
    else:
        inputs = [m for m in inputs if not m.get('invokedAt')]
    ids = [m['localId'] for m in inputs]
    state = s.api.call('/sessions/' + w['session'] + '/messages/queued-state', {'localIds': ids}) if ids else {}
    return {'worker': w['id'], 'session': w['session'], 'native_id': w['native_id'],
            'coverage': 'paginated public history, up to 20000 messages; missing items are not cancellation/consumption proof',
            'inputs': [{'message': m['id'], 'local_id': m['localId'], 'input_hash': input_hash(m),
                        'createdAt': m.get('createdAt'), 'invokedAt': m.get('invokedAt'),
                        'content': content(m).get('content')} for m in inputs], 'queue': state}


def cancel_reviewed(s, request):
    """One explicitly cancelled or proven superseded item, via native cancel ACK.

    The operator either supplies the current user's explicit cancellation, or
    reviews supersession against a later genuinely consumed ordinary prompt.
    """
    from supervisor import content
    if not current_user_authorization(request):
        raise ValueError('Exact-input cancellation requires current explicit user authorization and basis')
    if s.manager_paused():
        raise ValueError('Manager manually paused')
    for key in ('worker', 'session', 'native_id', 'message', 'local_id', 'input_hash'):
        if not isinstance(request.get(key), str) or not request[key]:
            raise ValueError('Exact input cancellation requires ' + key)
    if not isinstance(request.get('reason'), str) or len(request['reason'].strip()) < 16:
        raise ValueError('Explain the current explicit disposition for this exact reviewed input')
    w, messages = bound_input(s, request)
    candidates = [m for m in messages if m['id'] == request['message'] and m.get('localId') == request['local_id']
                  and content(m).get('role') == 'user']
    if len(candidates) != 1:
        raise ValueError('Exact input not found in current public page; no cancellation attempted')
    message = candidates[0]
    if input_hash(message) != request['input_hash']:
        raise ValueError('Input edited since review')
    if message.get('invokedAt'):
        return {'status': 'invoked', 'message': message['id'], 'localId': message['localId']}
    witness = {}
    if request.get('disposition') == 'cancel':
        proof = s.api.call('/sessions/' + w['session'] + '/messages/queued-state', {'localIds': [message['localId']]})
        if not all(isinstance(proof.get(k), list) for k in ('queuedLocalIds', 'indeterminateLocalIds', 'invokedLocalMessages')):
            raise ValueError('Queued-input inspection incomplete')
        if proof.get('queuedLocalIds') != [message['localId']] or proof.get('indeterminateLocalIds') != []:
            raise ValueError('Exact reviewed cancellation requires confirmed queued input')
        if any(p.get('localId') == message['localId'] for p in proof.get('invokedLocalMessages', [])):
            raise ValueError('Conflicting intake evidence; inspect without cancelling')
        witness = {'reviewedDisposition': 'cancel'}
    else:
        if not request.get('superseded_by') or not request.get('replacement_hash'):
            raise ValueError('Supply disposition:cancel for a current explicit cancellation, or exact supersession witness fields')
        replacements = [m for m in messages if m['id'] == request['superseded_by'] and content(m).get('role') == 'user'
                        and m.get('localId') and m['id'] != message['id']]
        if len(replacements) != 1 or input_hash(replacements[0]) != request['replacement_hash']:
            raise ValueError('Exact superseding input is missing or changed')
        replacement = replacements[0]
        body = content(replacement).get('content')
        text = body if isinstance(body, str) else body.get('text', '') if isinstance(body, dict) else ''
        if not text.strip() or text.lstrip().startswith('/') or replacement.get('createdAt', 0) < message.get('createdAt', 0):
            raise ValueError('Superseding input must be a later ordinary prompt, not goal/control metadata')
        ids = [message['localId'], replacement['localId']]
        proof = s.api.call('/sessions/' + w['session'] + '/messages/queued-state', {'localIds': ids})
        if (proof.get('indeterminateLocalIds') != [] or proof.get('queuedLocalIds') != [message['localId']]):
            raise ValueError('Target must be confirmed queued, replacement consumed; unknown/invoked states require review')
        receipts = [p for p in proof.get('invokedLocalMessages', []) if p.get('localId') == replacement['localId']]
        when = receipts[0].get('invokedAt') if len(receipts) == 1 else None
        if type(when) not in (int, float) or not when or not replacement.get('createdAt', 0) <= when <= time.time() * 1000:
            raise ValueError('Superseding input lacks exact confirmed intake evidence')
        witness = {'superseded_by': replacement['id'], 'replacement_hash': request['replacement_hash'], 'replacementInvokedAt': when}
    key = 'queued-input-cancel:' + w['session'] + ':' + message['id'] + ':' + request['input_hash']
    prior = json.loads(s.get(key) or 'null')
    if prior and prior.get('state') in ('dispatching', 'uncertain'):
        return {'status': 'uncertain', 'message': message['id'], 'note': 'Prior cancellation outcome unknown; inspect, never resend blindly'}
    current_worker, latest = bound_input(s, request)
    current = [m for m in latest if m['id'] == message['id'] and m.get('localId') == message['localId']]
    if current_worker != w or len(current) != 1 or input_hash(current[0]) != request['input_hash']:
        raise ValueError('Input or worker changed during review; inspect again')
    if current[0].get('invokedAt'):
        return {'status': 'invoked', 'message': message['id'], 'localId': message['localId']}
    if s.manager_paused():
        raise ValueError('Manager manually paused during review')
    audit = {'state': 'dispatching', 'worker': w['id'], 'session': w['session'], 'native_id': w['native_id'],
             'message': message['id'], 'local_id': message['localId'], 'input_hash': request['input_hash'],
             **witness, 'basis': request['basis'], 'reason': request['reason'], 'at': time.time()}
    # Preserve intent/content hash before transport. No assignment or goal outcome changes.
    s.set(key, json.dumps(audit))
    try:
        result = s.api.call('/sessions/' + quote(w['session'], safe='') + '/messages/' + quote(message['id'], safe=''), method='DELETE')
    except Exception:
        audit['state'] = 'uncertain'; s.set(key, json.dumps(audit))
        raise ValueError('Cancellation outcome unknown; inspect input state, do not retry blindly') from None
    status = result.get('status')
    if status == 'cancelled' and result.get('localId') != message['localId']:
        status = 'uncertain'
    audit.update(state=status if status in ('cancelled', 'invoked', 'busy') else 'uncertain', result=result)
    s.set(key, json.dumps(audit))
    s.set(key + ':audit:' + str(time.time_ns()), json.dumps(audit))
    return {'status': audit['state'], 'message': message['id'], 'localId': message['localId'], 'result': result}


def steer_reviewed(s, request):
    """Promote the existing exact input; never resend, abort or invent intake."""
    from supervisor import content
    from assignment_delivery import _promotion_guard
    if not current_user_authorization(request):
        raise ValueError('Exact-input steering requires current explicit user authorization and basis')
    for key in ('worker', 'session', 'native_id', 'message', 'local_id', 'input_hash'):
        if not isinstance(request.get(key), str) or not request[key]:
            raise ValueError('Exact input steering requires ' + key)
    if not isinstance(request.get('reason'), str) or len(request['reason'].strip()) < 16:
        raise ValueError('Explain why this exact existing input should reach the current turn')
    w, messages = bound_input(s, request)
    binding = {'id': request['local_id'], 'worker': w['id']}
    _promotion_guard(s, w, binding)
    matches = [m for m in messages if m['id'] == request['message'] and m.get('localId') == request['local_id']
               and content(m).get('role') == 'user']
    if len(matches) != 1 or input_hash(matches[0]) != request['input_hash']:
        raise ValueError('Exact reviewed input missing or edited')
    message = matches[0]
    body = content(message).get('content')
    text = body if isinstance(body, str) else body.get('text', '') if isinstance(body, dict) else ''
    if not text.strip() or text.lstrip().startswith('/') or message.get('scheduledAt') is not None:
        raise ValueError('Only unscheduled ordinary input may be steered; goals/controls are preserved')
    def proof():
        value = s.api.call('/sessions/' + w['session'] + '/messages/queued-state', {'localIds': [message['localId']]})
        if not all(isinstance(value.get(k), list) for k in ('queuedLocalIds', 'indeterminateLocalIds', 'invokedLocalMessages')):
            raise ValueError('Input intake inspection incomplete')
        receipts = [p for p in value['invokedLocalMessages'] if p.get('localId') == message['localId']]
        receipt = receipts[0] if len(receipts) == 1 else None
        when = receipt.get('invokedAt') if receipt else None
        if receipt and (type(when) not in (int, float) or when <= 0 or not message.get('createdAt', 0) <= when <= time.time() * 1000):
            raise ValueError('Invalid intake receipt')
        if len(receipts) > 1 or (receipt and (value['queuedLocalIds'] or value['indeterminateLocalIds'])):
            raise ValueError('Conflicting intake evidence')
        return value, receipt
    state, receipt = proof()
    if receipt:
        return {'status': 'invoked', 'localId': message['localId'], 'acknowledged': True, 'receipt': receipt,
                'resent': False, 'aborted': False, 'resumed': False}
    if state['queuedLocalIds'] != [message['localId']] or state['indeterminateLocalIds']:
        raise ValueError('Existing input is not definitely queued; no steering attempted')
    key = 'queued-input-steer:' + w['session'] + ':' + message['id'] + ':' + request['input_hash']
    prior = json.loads(s.get(key) or 'null')
    if prior and prior.get('state') != 'rpc-rejected':
        return {'status': prior['state'], 'rpcAccepted': prior.get('rpcAccepted', False), 'acknowledged': False,
                'localId': message['localId'], 'note': 'Inspect receipt; accepted/unknown steering is never repeated',
                'resent': False, 'aborted': False, 'resumed': False}
    fresh, latest = bound_input(s, request)
    current = [m for m in latest if m['id'] == message['id'] and m.get('localId') == message['localId']]
    if fresh != w or len(current) != 1 or input_hash(current[0]) != request['input_hash']:
        raise ValueError('Input or worker changed during review')
    _promotion_guard(s, fresh, binding)
    attempt = {'state': 'attempting', 'rpcAccepted': False, 'worker': w['id'], 'session': w['session'],
               'native_id': w['native_id'], 'message': message['id'], 'local_id': message['localId'],
               'input_hash': request['input_hash'], 'basis': request['basis'], 'reason': request['reason'], 'at': time.time()}
    s.set(key, json.dumps(attempt))
    try:
        result = s.api.call('/sessions/' + quote(w['session'], safe='') + '/messages/' + quote(message['id'], safe='') + '/steer', {}, method='POST')
        status = result.get('status')
        accepted = ((status == 'steered' and result.get('localId') == message['localId'])
                    or (status == 'invoked' and isinstance(result.get('message'), dict)
                        and result['message'].get('localId') == message['localId']))
        rejected = status == 'failed' and result.get('localId') in (None, message['localId'])
        attempt.update(state='rpc-accepted' if accepted else 'rpc-rejected' if rejected else 'uncertain',
                       rpcAccepted=accepted, result=result)
    except Exception:
        attempt.update(state='uncertain')
    s.set(key, json.dumps(attempt)); s.set(key + ':audit:' + str(time.time_ns()), json.dumps(attempt))
    try:
        _, receipt = proof()
    except Exception:
        receipt = None
    return {'status': attempt['state'], 'rpcAccepted': attempt['rpcAccepted'], 'acknowledged': receipt is not None,
            'receipt': receipt, 'localId': message['localId'], 'result': attempt.get('result'),
            'resent': False, 'aborted': False, 'resumed': False}
