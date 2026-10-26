"""Read-only coordination signals and durable dependency watches; never dispatch work."""
import json
import re
import time


def signals(text):
    text = re.sub(r'[*_`]', '', text or '').replace('’', "'").lower()
    gpu = r'(?:gpu|rtx\s*\d{3,4}(?:\s*(?:ti|super))?|\d{3,4}\s*(?:ti|super))'
    # Reported candidates only; the resource_handoff operation checks real hardware.
    release = bool(re.search(r'\b' + gpu + r'\s+(?:(?:is|stays|remains)\s+)?(?:now\s+|still\s+)?free\b', text)
                   or re.search(r'\ball\s+(?:my\s+)?gpu\s+jobs\s+(?:are\s+|have\s+)?stopped\b', text))
    hypothetical = re.search(r'\b(?:if|once|when|until)\b[^.!?\n]{0,60}\b' + gpu + r'\b[^.!?\n]{0,20}\bfree\b', text)
    release = release and not hypothetical
    waiting = bool(re.search(r"\b(?:stays?|still|remains?|keeping)\s+(?:\w+\s+){0,2}(?:paused|on hold)\b|\b(?:paused|on hold)\b[^.!?\n]{0,100}\buntil\b|\b(?:waiting|wait)\b[^.!?\n]{0,100}\b(?:handoff|go-ahead|gpu (?:availability|reservation)|gpu (?:to be |is )?(?:free|available))\b|\b(?:resume|continue)\b[^.!?\n]{0,50}\b(?:when|once)\s+(?:you|drift|terrain)\b", text))
    return {'resource-release': release, 'dependency-wait': waiting}


def is_instruction_message(message):
    """Public user input changes instruction epochs; trusted peer delivery does not.

    Origin is transport metadata, never inferred from text or sentFrom. Peer pings
    retain the user role for provider delivery, and CLI echoes must preserve this
    marker through their original queue lineage. Untagged historical input stays
    conservative: it can still be a real human or manager instruction.
    """
    from supervisor import content
    record = content(message)
    meta = record.get('meta')
    return record.get('role') == 'user' and not (
        isinstance(meta, dict) and meta.get('source') == 'peer'
        and isinstance(meta.get('sourceSessionId'), str) and bool(meta['sourceSessionId'].strip())
    )


def observe(supervisor, worker, messages, previous):
    """One candidate per condition per instruction epoch, persisted by observation."""
    from supervisor import content
    from resource_handoff import public_reply
    state = dict(previous or {}); candidates = []
    for m in sorted(messages, key=lambda x: (x.get('seq', 0), x.get('createdAt', 0))):
        seq = m.get('seq', 0)
        if content(m).get('role') == 'user':
            if is_instruction_message(m) and seq > state.get('user_seq', -1):
                state = {'user_seq': seq, 'user': m['id'], 'seen': [], 'wait_messages': []}
            continue
        if seq <= state.get('user_seq', -1):
            continue
        text = public_reply(m)
        if not text:
            continue
        found = signals(text)
        # Initial attachment seeds state silently; subsequent evidence is actionable.
        for kind, present in found.items():
            if not present or kind in state.get('seen', []):
                continue
            state.setdefault('seen', []).append(kind)
            if previous is not None:
                candidates.append({'category': kind, 'message': m['id'], 'text': text[:1200],
                                   'reason': 'Worker reports resource released; inspect waiting handoff and verify current GPU ownership/availability' if kind == 'resource-release' else 'Worker reports waiting for a dependency; preserve goal and review handoff, do not replay work'})
        if found['dependency-wait']:
            wait_messages = state.setdefault('wait_messages', [])
            if m['id'] not in wait_messages:
                wait_messages.append(m['id']); state['wait_messages'] = wait_messages[-3:]
            if len(state['wait_messages']) >= 3 and 'native-wait-loop' not in state.get('seen', []):
                state.setdefault('seen', []).append('native-wait-loop')
                if previous is not None:
                    candidates.append({'category': 'native-wait-loop', 'message': m['id'],
                                       'reason': 'Repeated dependency-wait replies: a native goal may be consuming turns while blocked. Inspect native pause support; never clear, complete or replay the goal automatically.'})
    return state, candidates


def waiting_snapshot(messages):
    """Only public waiting newer than any user instruction or real tool activity."""
    from supervisor import content
    from resource_handoff import public_reply
    for m in sorted(messages, key=lambda x: (x.get('seq', 0), x.get('createdAt', 0)), reverse=True):
        c = content(m); inner = c.get('content', {})
        if c.get('role') == 'user':
            if is_instruction_message(m):
                return False
            continue
        if not isinstance(inner, dict):
            continue
        data = inner.get('data') if isinstance(inner.get('data'), dict) else {}
        msg = data.get('message') if isinstance(data.get('message'), dict) else {}
        parts = msg.get('content') if isinstance(msg.get('content'), list) else []
        if data.get('type') in ('tool-call', 'tool_call') or any(isinstance(p, dict) and p.get('type') == 'tool_use' for p in parts):
            return False
        text = public_reply(m)
        if text and not text.startswith('Claude AI usage limit '):
            return signals(text)['dependency-wait']
    return False


def latest_instruction(messages):
    users = [m for m in messages if is_instruction_message(m)]
    return max(users, key=lambda m: (m.get('seq', 0), m.get('createdAt', 0))) if users else None


def instruction_changed(record, field, messages):
    latest = latest_instruction(messages)
    if latest is None:
        return False  # A paginated window with no instruction is not a new instruction.
    if field + 'AfterSeq' in record:
        return latest.get('seq', 0) > record[field + 'AfterSeq']
    return latest['id'] != record.get(field + 'User')  # Existing watches, without a watermark.


def watch_messages(s, worker, messages):
    """Keep human steering separate from this manager's recorded deliveries.

    Assignment IDs are durable queue lineage, not a text/source heuristic. Keep
    filtering local to dependency watches: an assignment remains real pending
    work and a new observation epoch elsewhere. Unknown/foreign IDs stay input.
    Filter before selecting latest, so a manager note cannot hide a human steer.
    """
    ids = {m.get('localId') for m in messages if is_instruction_message(m) and m.get('localId')}
    if not ids:
        return messages
    known = {row['id'] for row in s.rows(
        'SELECT id FROM assignments WHERE worker=? AND id IN (' + ','.join('?' for _ in ids) + ')',
        (worker['id'], *ids))}
    # Durable supervisor recovery input continues the same request, not human steering.
    known.update(row['id'] for row in s.rows(
        'SELECT id FROM outbox WHERE session=? AND id IN (' + ','.join('?' for _ in ids) + ')',
        (worker['session'], *ids)))
    return [m for m in messages if not (is_instruction_message(m) and m.get('localId') in known)]


def transition(s, name, record, state, reason):
    if record['state'] == state:
        return
    record.update(state=state, reason=reason, updatedAt=time.time())
    s.set(name, json.dumps(record))
    # A coordination lifecycle event is about the dependency, not a worker nudge.
    # Keep it deliverable when the participant was paused/released; manager pauses
    # still gate every wake. The stable key prevents repeated cancellation spam.
    s.event('handoff-watch:'+record['key']+':'+state, None, None, 'handoff-watch-'+state,
            json.dumps({'key':record['key'], 'source':record['source'], 'waiter':record['waiter'],
                        'resource':record['resource'], 'reason':reason}))


def holds_retirement(s, worker_id):
    return any(worker_id in (r['source'], r['waiter']) and r['state'] in ('waiting','review-required')
               for r in (json.loads(row['value']) for row in s.rows("SELECT value FROM settings WHERE key LIKE 'handoff-watch:%'")))


def watch(supervisor, request):
    s = supervisor; key = request.get('key')
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', key):
        raise ValueError('Stable handoff watch key required')
    name = 'handoff-watch:' + key
    old = json.loads(s.get(name) or 'null')
    if request.get('cancel') is True:
        if old and old['state'] in ('waiting','review-required'):
            transition(s, name, old, 'cancelled', 'Explicit watch cancellation')
        return old
    if old:
        return old  # Idempotent registration; never re-arm an old instruction implicitly.
    source = s.worker(request['source_worker']); waiter = s.worker(request['waiting_worker'])
    if source['id'] == waiter['id'] or source['project'] == waiter['project']:
        raise ValueError('Handoff requires different workers/projects')
    if request.get('user_requested') is not True or not request.get('basis', '').strip():
        raise ValueError('Current user handoff instruction and basis required')
    for w in (source, waiter):
        if not s.reporting_allowed(w) or s.get('unmanaged:' + w['id']) or s.session(w['session']).get('manualPausedAt') is not None:
            raise ValueError('Both workers must already be managed with enabled event reporting and no project/manual pause')
    resource = request['resource']
    from resource_handoff import assert_resource_available
    assert_resource_available(s, resource)
    owner = s.rows('SELECT project FROM resources WHERE name=?', (resource,))
    if not owner or owner[0]['project'] != source['project']:
        raise ValueError('Source does not own this resource')
    source_messages = s.api.call('/sessions/' + source['session'] + '/messages?limit=100')['messages']
    waiter_messages = s.api.call('/sessions/' + waiter['session'] + '/messages?limit=100')['messages']
    record = {'sourceUser': (latest_instruction(watch_messages(s, source, source_messages)) or {}).get('id'), 'waitingUser': (latest_instruction(watch_messages(s, waiter, waiter_messages)) or {}).get('id'),
              'sourceAfterSeq': max((m.get('seq',0) for m in source_messages),default=0), 'waitingAfterSeq': max((m.get('seq',0) for m in waiter_messages),default=0), 'key': key, 'state': 'waiting', 'source': source['id'], 'waiter': waiter['id'],
              'sourceSession': source['session'], 'waitingSession': waiter['session'],
              'resource': resource, 'basis': request['basis'], 'createdAt': time.time()}
    s.set(name, json.dumps(record)); return record


def reconcile_registry(s):
    """Registry changes must settle watches even when a released worker is not scanned."""
    for row in s.rows("SELECT key,value FROM settings WHERE key LIKE 'handoff-watch:%'"):
        record = json.loads(row['value'])
        if record['state'] not in ('waiting','review-required'):
            continue
        participants = [s.rows('SELECT * FROM workers WHERE id=?',(record[field],)) for field in ('source','waiter')]
        if any(not rows for rows in participants):
            transition(s,row['key'],record,'cancelled','Participant no longer registered'); continue
        source,waiter = (rows[0] for rows in participants)
        if (not s.reporting_allowed(source) or not s.reporting_allowed(waiter)
            or s.get('unmanaged:'+source['id']) or s.get('unmanaged:'+waiter['id'])
            or source['session'] != record['sourceSession'] or waiter['session'] != record['waitingSession']):
            transition(s,row['key'],record,'cancelled','Participant management, binding or reporting changed'); continue
        owner = s.rows('SELECT project FROM resources WHERE name=?',(record['resource'],))
        if not owner or owner[0]['project'] != source['project']:
            transition(s,row['key'],record,'superseded','Resource owner changed')


def update_watches(supervisor, worker, candidates, messages, session):
    """Link a new release candidate to an existing waiter once, not an LLM retry loop."""
    s = supervisor
    releases = [c for c in candidates if c.get('category') == 'resource-release']
    linked = []
    for row in s.rows("SELECT key,value FROM settings WHERE key LIKE 'handoff-watch:%'"):
        record = json.loads(row['value'])
        if worker['id'] not in (record['source'], record['waiter']) or record['state'] not in ('waiting','review-required'):
            continue
        field = 'source' if record['source'] == worker['id'] else 'waiting'
        reason = watch_invalidation(s, record, field, worker, messages, session)
        if reason:
            transition(s, row['key'], record, 'cancelled', reason); continue
        if record['source'] != worker['id'] or not s.reporting(worker['id'])['important']:
            continue
        owner = s.rows('SELECT project FROM resources WHERE name=?', (record['resource'],))
        if not owner or owner[0]['project'] != worker['project']:
            transition(s, row['key'], record, 'superseded', 'Resource owner changed'); continue
        current_releases = releases
        if not current_releases:
            from resource_handoff import public_reply
            latest = next((m for m in sorted(messages, key=lambda x: x.get('seq', 0), reverse=True) if public_reply(m)), None)
            if latest and signals(public_reply(latest))['resource-release'] and 0 <= time.time() - latest.get('createdAt', 0)/1000 <= 1800:
                current_releases = [{'message': latest['id']}]
        waiter = s.worker(record['waiter'])
        waiter_session = s.session(waiter['session'])
        waiter_messages = s.api.call('/sessions/' + waiter['session'] + '/messages?limit=100')['messages']
        reason = watch_invalidation(s, record, 'waiting', waiter, waiter_messages, waiter_session)
        if reason:
            transition(s, row['key'], record, 'cancelled', reason); continue
        if not current_releases or record['state'] == 'review-required':
            continue
        # Candidate means inspect, not hardware verified, permission to resume, or dispatch.
        record.update(state='review-required', evidence=current_releases[-1]['message'], updatedAt=time.time())
        s.set(row['key'], json.dumps(record))
        linked.append({'category': 'handoff-review', 'message': record['evidence'], 'resource': record['resource'],
                       'waiting_worker': waiter['id'], 'key': record['key'],
                       'reason': 'Pending handoff may be ready. Review current user allocation, pauses, release evidence and resource_handoff; never replay the waiting task.'})
    return linked


def watch_invalidation(s, record, field, worker, messages, session):
    # Check binding before trusting assignment IDs associated with that worker.
    if worker['session'] != record[field + 'Session']:
        return 'Participant binding changed: ' + worker['id']
    if session.get('manualPausedAt') is not None:
        return 'Participant manually paused: ' + worker['id']
    if worker['state'] != 'managed' or not s.reporting_allowed(worker) or s.get('unmanaged:' + worker['id']):
        return 'Participant management, reporting or project pause changed: ' + worker['id']
    filtered = watch_messages(s, worker, messages)
    if instruction_changed(record, field, filtered):
        return 'Participant user instruction changed: ' + str((latest_instruction(filtered) or {}).get('id'))
    return None


def complete_watches(supervisor, source, target, resource):
    """Called inside the owner-transfer transaction; completion of lease, not work."""
    s = supervisor
    for row in s.rows("SELECT key,value FROM settings WHERE key LIKE 'handoff-watch:%'"):
        record = json.loads(row['value'])
        if record['resource'] != resource or record['state'] not in ('waiting', 'review-required'):
            continue
        if s.worker(record['source'])['project'] == source and s.worker(record['waiter'])['project'] == target:
            record.update(state='transferred', updatedAt=time.time())
            s.set(row['key'], json.dumps(record))
