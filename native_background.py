"""Read-only native-terminal evidence and explicit per-session intent annotations."""
import hashlib
import json
import time


def fresh_snapshot(session, now=None):
    now = time.time() if now is None else now
    meta = session.get('metadata') or {}
    snap = meta.get('codexBackgroundTerminals') or {}
    if (not session.get('active') or meta.get('flavor') != 'codex' or not meta.get('codexSessionId')
            or snap.get('threadId') != meta.get('codexSessionId') or not isinstance(snap.get('checkedAt'), (int, float))
            or not -1 <= now - snap['checkedAt']/1000 <= 45 or not isinstance(snap.get('terminals'), list)):
        return None
    rows = {}
    for terminal in snap['terminals']:
        if not isinstance(terminal, dict) or not isinstance(terminal.get('itemId'), str) or not isinstance(terminal.get('processId'), str):
            return None
        key = json.dumps([terminal['itemId'], terminal['processId']], separators=(',', ':'))
        if key in rows:
            return None
        rows[key] = {'itemId': terminal['itemId'], 'processId': terminal['processId'], 'osPid': terminal.get('osPid')}
    return {'checkedAt': snap['checkedAt'], 'threadId': snap['threadId'], 'terminals': rows}


def intent_key(worker, terminal, thread_id):
    identity = thread_id + '\0' + terminal['itemId'] + '\0' + terminal['processId']
    return 'background-intent:' + worker + ':' + hashlib.sha256(identity.encode()).hexdigest()


def set_intent(supervisor, request):
    """Annotate only the calling session's current exact native terminal; never control it."""
    actor = request.get('actor')
    if not isinstance(actor, str) or not actor:
        raise ValueError('background_intent requires an originating HAPI session')
    if request.get('session') not in (None, actor) or request.get('worker'):
        raise ValueError('background_intent can annotate only the calling session')
    workers = supervisor.rows('SELECT * FROM workers WHERE session=? AND state=?', (actor, 'managed'))
    if len(workers) != 1:
        raise ValueError('Calling session must be an unambiguous managed worker')
    worker = workers[0]
    if supervisor.get('unmanaged:'+worker['id']):
        raise ValueError('Session is unmanaged')
    live = supervisor.session(actor)
    snap = fresh_snapshot(live)
    process_id = request.get('process_id')
    if not isinstance(process_id, str) or not process_id or snap is None:
        raise ValueError('A fresh native snapshot and exact process_id are required')
    matches = [row for row in snap['terminals'].values() if row['processId'] == process_id]
    if len(matches) != 1:
        raise ValueError('The process handle is not uniquely present in the fresh native snapshot')
    intent = request.get('intent')
    if intent not in ('review', 'independent', 'unknown'):
        raise ValueError('intent must be review, independent, or unknown')
    note = request.get('note', '')
    if not isinstance(note, str) or len(note) > 500:
        raise ValueError('note must be text of at most 500 characters')
    record = {'session':actor, 'threadId':snap['threadId'], **matches[0], 'intent':intent, 'note':note, 'updatedAt':time.time()}
    supervisor.set(intent_key(worker['id'], matches[0], snap['threadId']), json.dumps(record))
    return {'recorded':record, 'effect':'annotation only; no process or conversation was changed'}
