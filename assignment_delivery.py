"""Read-only delivery evidence and explicitly authorized promotion of an existing input.

A queue ACK is not instruction compliance. Goal control ACKs are never proof
that the current inference turn received a new objective. No function resends,
resumes, aborts, changes a goal, or clears a manual pause.
"""
import json
import math
import re
import time
from urllib.parse import quote


def _object(value):
    return value if isinstance(value, dict) else {}


def _record(message):
    value = _object(_object(message).get('content'))
    for _ in range(3):
        if value.get('type') == 'plaintext':
            value = _object(value.get('data'))
        else:
            break
    return value


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _string(value, limit=200):
    return value[:limit] if isinstance(value, str) else None


def _command(message):
    record = _record(message)
    if record.get('role') != 'user':
        return None
    inner = record.get('content')
    text = inner if isinstance(inner, str) else _object(inner).get('text')
    match = re.match(r'^/([A-Za-z][\w-]*)(?:\s|$)', text.strip()) if isinstance(text, str) else None
    return match.group(1).lower() if match else None


def _goal_ack(message):
    record = _record(message)
    inner = _object(record.get('content'))
    data = _object(inner.get('data'))
    if record.get('role') != 'agent' or inner.get('type') != 'event' or data.get('type') != 'goal-command-ack':
        return None
    local = _string(data.get('localId'))
    if not local:
        return None
    result = {'localId': local, 'appliedToCurrentTurn': False}
    if isinstance(data.get('savedStateConfirmed'), bool):
        result['savedStateConfirmed'] = data['savedStateConfirmed']
    for key in ('action', 'goalStatus', 'threadId', 'currentTurnId'):
        value = _string(data.get(key))
        if value is not None:
            result[key] = value
    return result


def describe_message(message, now=None, include_wait=True):
    """Bounded public transport metadata only; never message text or hidden content.

    ``now`` is epoch seconds, like supervisor clocks; wire timestamps remain ms.
    Only a durable meta.deliveryReceipt.steered=true proves active-turn steering.
    """
    now = time.time() if now is None else now
    message = _object(message)
    record = _record(message)
    meta = _object(record.get('meta'))
    receipt = _object(meta.get('deliveryReceipt'))
    consumed = receipt.get('consumedAt') if _number(receipt.get('consumedAt')) else message.get('invokedAt')
    consumed = consumed if _number(consumed) else None
    command = _command(message)
    ack = _goal_ack(message)
    goal = command == 'goal' or ack is not None
    control = goal or receipt.get('controlOnly') is True or ack is not None
    priority = meta.get('deliveryMode') if meta.get('deliveryMode') in ('queue', 'steer', 'next') else None
    result = {'messageId': _string(message.get('id')), 'localId': _string(message.get('localId')),
              'priority': priority, 'state': 'unknown', 'control': control, 'goal': goal,
              'createdAt': message.get('createdAt') if _number(message.get('createdAt')) else None,
              'invokedAt': consumed}
    if ack:
        result.update(localId=ack['localId'], state='control-consumed', goalAcknowledgement=ack)
    elif consumed is not None:
        result['state'] = 'control-consumed' if control else 'steered' if receipt.get('steered') is True else 'consumed'
        result['compliance'] = 'unverified'
    elif message.get('status') == 'cancelled' or message.get('cancelled') is True:
        result['state'] = 'cancelled'
    elif message.get('deliveryState') == 'indeterminate':
        result['state'] = 'indeterminate'
    elif record.get('role') == 'user' and result['localId'] and message.get('invokedAt', 'absent') is None and message.get('status') != 'failed':
        result['state'] = 'queued'
    if meta.get('source') == 'peer' and isinstance(meta.get('sourceSessionId'), str) and meta['sourceSessionId'].strip():
        result.update(source='peer', sourceSessionId=_string(meta['sourceSessionId']))
    if goal:
        result['appliedToCurrentTurn'] = False
    scheduled = message.get('scheduledAt')
    if _number(scheduled):
        result['scheduledAt'] = scheduled
    if include_wait and result['state'] in ('queued', 'indeterminate') and result['createdAt'] is not None:
        result['waitSeconds'] = max(0, int(now - max(result['createdAt'], scheduled if _number(scheduled) else 0) / 1000))
    return result


def summarize_inputs(messages, now=None, include_wait=True):
    """Latest ordinary input, pending input and goal metadata (at most three).

    Non-peer corrections take precedence over later peer notices in each input
    slot. Peers remain real queued work; this preference only shapes the report.
    """
    pending = peer_pending = latest = peer_latest = goal = None
    acknowledgements = {}
    ordered = sorted((_object(m) for m in messages), key=lambda m: (m.get('seq') or 0, m.get('createdAt') or 0))[-200:]
    for message in ordered:
        receipt = _goal_ack(message)
        if receipt:
            acknowledgements[receipt['localId']] = (message, receipt)
    for message in ordered:
        description = describe_message(message, now, include_wait)
        receipt = acknowledgements.get(description['localId'])
        if description['goal'] and receipt:
            description.update(state='control-consumed', goalAcknowledgement=receipt[1], appliedToCurrentTurn=False)
            description.pop('waitSeconds', None)
        peer = description.get('source') == 'peer'
        if _record(message).get('role') == 'user' and not description['goal']:
            if peer:
                peer_latest = description
            else:
                latest = description
        if description['state'] in ('queued', 'indeterminate'):
            if peer:
                peer_pending = description
            else:
                pending = description
        if description['goal']:
            # Prefer original user identity when its exact typed ACK is present.
            if not _goal_ack(message) or not goal or goal['localId'] != description['localId']:
                goal = description
    result = []
    seen = set()
    for description in (latest or peer_latest, pending or peer_pending, goal):
        if not description:
            continue
        identity = description['localId'] or description['messageId']
        if identity and identity not in seen:
            result.append(description); seen.add(identity)
    return result


def validate_priority(session, mode, runtime=None):
    """Reject older Claude's implicit stop-and-send fallback; never authorize abort."""
    if mode not in ('queue', 'steer', 'next'):
        raise ValueError('delivery_mode must be queue, steer or next')
    if mode == 'queue':
        return mode
    metadata = _object(session.get('metadata'))
    flavor = metadata.get('flavor') or runtime
    capabilities = _object(metadata.get('capabilities'))
    if flavor == 'claude':
        if capabilities.get('claudeInputPriorities') is not True:
            raise ValueError('Native priority unavailable on this Claude controller; implicit Stop is forbidden')
    elif mode == 'next':
        raise ValueError('next priority requires a capable Claude controller')
    elif flavor not in ('codex', 'pi', 'cursor'):
        raise ValueError('Native priority unavailable for this controller')
    elif flavor == 'cursor' and (metadata.get('cursorSessionProtocol') == 'stream-json' or
                                (not metadata.get('cursorSessionProtocol') and metadata.get('cursorSessionId'))):
        raise ValueError('Native priority unavailable for legacy Cursor controller')
    return mode


def _binding(s, worker, assignment):
    from supervisor import native
    if assignment.get('worker') != worker.get('id'):
        raise ValueError('Assignment does not belong to this worker')
    sid = worker.get('session')
    if not sid:
        raise ValueError('Worker has no bound session')
    session = s.session(sid)
    if session.get('id') != sid:
        raise ValueError('Worker session binding changed')
    actual = native(session)
    if not actual or not worker.get('native_id') or actual != worker['native_id']:
        raise ValueError('Worker native identity unavailable or changed')
    raw = s.get('assignment-input:' + assignment['id'])
    if raw:
        try:
            intent = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError('Assignment input binding is invalid') from None
        if not isinstance(intent, dict):
            raise ValueError('Assignment input binding is invalid')
        expected_native = intent.get('native_id', intent.get('native'))
        if intent.get('session') != sid or expected_native != actual or intent.get('worker', worker['id']) != worker['id']:
            raise ValueError('Assignment input binding changed')
    return session


def inspect_assignment(supervisor, worker, assignment, messages=None, historical=False):
    """Read exact bound queue/receipt evidence; failed reads remain unknown, never replay."""
    s = supervisor
    worker = s.worker(worker) if isinstance(worker, str) else dict(worker)
    assignment = s.one('SELECT * FROM assignments WHERE id=?', (assignment,)) if isinstance(assignment, str) else dict(assignment)
    raw_intent = s.get('assignment-input:' + assignment['id'])
    try:
        intent = _object(json.loads(raw_intent)) if raw_intent else {}
    except (ValueError, TypeError):
        intent = {}
    priority = intent.get('priority', intent.get('mode', intent.get('delivery_mode')))
    seed = describe_message({'localId': assignment['id'], 'content': {'role': 'user', 'content': {'type': 'text', 'text': assignment.get('text') or ''}, 'meta': {'deliveryMode': priority}}}, include_wait=False)
    if assignment.get('goal'):
        seed.update(goal=True, control=True, appliedToCurrentTurn=False)
    if intent.get('controlOnly') is True:
        seed['control'] = True
    result = {**seed, 'assignment': assignment['id'], 'worker': worker['id'], 'session': worker.get('session'),
              'state': 'unknown', 'rpcAccepted': False}
    result['assignmentState'] = assignment.get('state')
    audit = s.get('assignment-supersession:' + assignment['id'])
    if audit:
        try: result['supersession'] = json.loads(audit)
        except (ValueError, TypeError): pass
    if assignment.get('worker') != worker['id']:
        raise ValueError('Assignment does not belong to this worker')
    try:
        promotion = _object(json.loads(s.get('assignment-promotion:' + assignment['id']) or 'null'))
        if promotion.get('worker') == worker['id'] and promotion.get('session') == worker.get('session'):
            result.update(rpcAccepted=promotion.get('rpcAccepted') is True, promotionState=promotion.get('state'))
    except (ValueError, TypeError):
        pass  # Malformed audit data is not a delivery receipt.
    try:
        session = _binding(s, worker, assignment)
        result['bindingVerified'] = True
        result['native_id'] = worker['native_id']
        if messages is None:
            if historical:
                from public_history import read_inputs
                messages, result['historyCoverage'] = read_inputs(s.api, worker['session'], local_ids=[assignment['id']])
            else:
                messages = s.api.call('/sessions/' + quote(worker['session'], safe='') + '/messages?limit=200')['messages']
        matches = [m for m in messages if m.get('localId') == assignment['id'] and _record(m).get('role') == 'user']
        result['messageLocated'] = bool(matches)
        description = describe_message(matches[0], include_wait=True) if len(matches) == 1 else None
        if len(matches) > 1:
            # Multiple imported/echo rows may share lineage. They are acceptable
            # only when all durable delivery descriptors agree.
            candidates = [describe_message(m) for m in matches]
            terminal = [d for d in candidates if d['state'] in ('consumed', 'steered', 'control-consumed')]
            description = terminal[-1] if terminal else candidates[0]
        if description:
            result.update(description)
            result['goal'] = seed['goal'] or description['goal']
            result['control'] = seed['control'] or description['control']
        goal_ack = next((_goal_ack(m) for m in reversed(messages) if (_goal_ack(m) or {}).get('localId') == assignment['id']), None)
        if goal_ack:
            result.update(state='control-consumed', goal=True, control=True, appliedToCurrentTurn=False, goalAcknowledgement=goal_ack)
        proof = s.api.call('/sessions/' + quote(worker['session'], safe='') + '/messages/queued-state', {'localIds': [assignment['id']]})
        if not all(isinstance(proof.get(k), list) for k in ('queuedLocalIds', 'indeterminateLocalIds', 'invokedLocalMessages')):
            result.update(state='unknown', reason='Queue inspection incomplete')
            return result
        queued = assignment['id'] in proof['queuedLocalIds']
        uncertain = assignment['id'] in proof['indeterminateLocalIds']
        invoked = [r for r in proof['invokedLocalMessages'] if r.get('localId') == assignment['id'] and _number(r.get('invokedAt'))]
        if sum((bool(queued), bool(uncertain), bool(invoked))) > 1:
            result.update(state='unknown', reason='Conflicting queue evidence')
        elif uncertain:
            result['state'] = 'indeterminate'
        elif queued:
            if result['state'] in ('consumed', 'steered', 'control-consumed'):
                result.update(state='unknown', reason='Conflicting delivery and queue evidence')
            else:
                result['state'] = 'queued'
        elif invoked:
            result['invokedAt'] = invoked[0]['invokedAt']
            result['state'] = 'control-consumed' if result.get('control') else 'steered' if description and description['state'] == 'steered' else 'consumed'
            result['compliance'] = 'unverified'
        elif result['state'] not in ('consumed', 'steered', 'control-consumed'):
            result['state'] = 'cancelled' if assignment.get('state') == 'cancelled' else 'unknown'
        if assignment.get('state') == 'cancelled' and result['state'] == 'queued':
            result.update(state='unknown', reason='Cancelled assignment still has queue evidence')
        if result.get('goal'):
            result['appliedToCurrentTurn'] = False
        result['active'] = session.get('active') is True
        return result
    except Exception as error:
        result.update(state='unknown', bindingVerified=False, reason=str(error)[:200] if isinstance(error, ValueError) else 'Delivery inspection unavailable')
        return result


def _promotion_guard(s, worker, assignment):
    if s.manager_paused():
        raise ValueError('Manager manually paused')
    manager = s.get('manager')
    if not manager or not s.session(manager).get('active'):
        raise ValueError('Manager is not active')
    if worker.get('state') != 'managed' or s.get('unmanaged:' + worker['id']):
        raise ValueError('Worker is not managed')
    project = s.project(worker['project'])
    if project.get('status') == 'paused' or project.get('manual'):
        raise ValueError('Project is paused or manually held')
    session = _binding(s, worker, assignment)
    if not session.get('active') or session.get('manualPausedAt') is not None:
        raise ValueError('Worker inactive or manually paused')
    agent = _object(session.get('agentState'))
    if agent.get('controlledByUser') or agent.get('requests'):
        raise ValueError('Worker under direct control or waiting for input')
    validate_priority(session, 'steer', worker.get('runtime'))
    return session


def promote_assignment(supervisor, request):
    """Promote one proven queued localId once, only on a fresh explicit user request."""
    s, o = supervisor, request
    now = time.time()
    if o.get('user_requested') is not True or not isinstance(o.get('basis'), str) or not o['basis'].strip() or not _number(o.get('requested_at')) or not 0 <= now - o['requested_at'] <= 300:
        raise ValueError('Promotion requires a current explicit user request, basis and fresh requested_at')
    if not isinstance(o.get('worker'), str) or not isinstance(o.get('assignment'), str):
        raise ValueError('Exact worker and assignment IDs required')
    worker = s.worker(o['worker'])
    assignment = s.one('SELECT * FROM assignments WHERE id=?', (o['assignment'],))
    _promotion_guard(s, worker, assignment)
    if assignment.get('state') != 'sent':
        raise ValueError('Assignment dispatch is cancelled, uncertain or not settled')
    text = assignment.get('text') or ''
    if re.match(r'^/[A-Za-z][\w-]*(?:\s|$)', text.lstrip()):
        raise ValueError('Slash/goal controls cannot be promoted as active-turn instructions')
    delivery = inspect_assignment(s, worker, assignment)
    if delivery['state'] != 'queued' or not delivery.get('messageLocated') or delivery.get('scheduledAt') is not None or delivery.get('control'):
        raise ValueError('Assignment is not definitely queued unscheduled ordinary input')
    # Recheck the live binding and pause state after the queue inspection.
    fresh = s.worker(worker['id'])
    if fresh.get('session') != worker.get('session') or fresh.get('native_id') != worker.get('native_id'):
        raise ValueError('Worker binding changed during inspection')
    _promotion_guard(s, fresh, assignment)
    key = 'assignment-promotion:' + assignment['id']
    with s.lock:
        s.db.execute('BEGIN IMMEDIATE')
        try:
            old = json.loads(s.get(key) or 'null')
            if old and old.get('state') != 'rpc-rejected':
                raise ValueError('Prior promotion is unresolved or already accepted; inspect receipt before any retry')
            attempt = {'assignment': assignment['id'], 'worker': worker['id'], 'session': worker['session'],
                       'native_id': worker['native_id'], 'state': 'attempting', 'requestedAt': o['requested_at'],
                       'attemptedAt': now, 'basis': o['basis'].strip()[:1000], 'rpcAccepted': False}
            s.set(key, json.dumps(attempt))
            s.db.execute('COMMIT')
        except Exception:
            s.db.execute('ROLLBACK')
            raise
    try:
        response = s.api.call('/sessions/' + quote(worker['session'], safe='') + '/messages/' + quote(assignment['id'], safe='') + '/steer', {}, method='POST')
        status = response.get('status') if isinstance(response, dict) else None
        local = response.get('localId') if isinstance(response, dict) else None
        if status == 'steered' and local == assignment['id']:
            attempt.update(state='rpc-accepted', rpcAccepted=True)
        elif status == 'invoked' and _object(response.get('message')).get('localId') == assignment['id']:
            attempt.update(state='rpc-accepted', rpcAccepted=True)
        elif status == 'failed' and local in (None, assignment['id']):
            attempt.update(state='rpc-rejected', rpcAccepted=False)
        else:
            attempt.update(state='uncertain')
    except Exception:
        attempt.update(state='uncertain')
    s.set(key, json.dumps(attempt))
    after = inspect_assignment(s, worker, assignment)
    return {'assignment': assignment['id'], 'worker': worker['id'], 'session': worker['session'],
            'rpcAccepted': attempt['rpcAccepted'], 'promotionState': attempt['state'],
            'acknowledged': after['state'] in ('consumed', 'steered', 'control-consumed'),
            'delivery': after, 'resent': False, 'aborted': False, 'resumed': False}
