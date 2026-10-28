"""User-directed removal of an exact GPU claim; never controls GPU work."""
import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid

from resource_handoff import assert_resource_available, current_user_authorization

_KEY = os.urandom(32)


def install(s):
    # Persist generations even when a row is deleted and recreated with the
    # same owner. Triggers cover normal claim, release and handoff paths.
    s.db.executescript('''
CREATE TABLE IF NOT EXISTS gpu_claim_revision(name TEXT PRIMARY KEY, revision INTEGER NOT NULL);
INSERT OR IGNORE INTO gpu_claim_revision SELECT name, 1 FROM resources WHERE instr(name, '/GPU-') > 0;
CREATE TRIGGER IF NOT EXISTS gpu_claim_revision_insert AFTER INSERT ON resources WHEN instr(NEW.name, '/GPU-') > 0 BEGIN
 INSERT INTO gpu_claim_revision VALUES(NEW.name, 1) ON CONFLICT(name) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER IF NOT EXISTS gpu_claim_revision_delete AFTER DELETE ON resources WHEN instr(OLD.name, '/GPU-') > 0 BEGIN
 INSERT INTO gpu_claim_revision VALUES(OLD.name, 1) ON CONFLICT(name) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER IF NOT EXISTS gpu_claim_revision_update AFTER UPDATE ON resources BEGIN
 INSERT INTO gpu_claim_revision SELECT OLD.name, 1 WHERE instr(OLD.name, '/GPU-') > 0 ON CONFLICT(name) DO UPDATE SET revision=revision+1;
 INSERT INTO gpu_claim_revision SELECT NEW.name, 1 WHERE NEW.name != OLD.name AND instr(NEW.name, '/GPU-') > 0 ON CONFLICT(name) DO UPDATE SET revision=revision+1;
END;
''')


def _identity(s, request):
    resource = request.get('resource')
    if not isinstance(resource, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/GPU-[0-9a-fA-F-]{36}', resource):
        raise ValueError('Exact host/GPU-UUID required')
    try:
        if str(uuid.UUID(resource.split('/GPU-', 1)[1])) != resource.split('/GPU-', 1)[1].lower():
            raise ValueError()
    except ValueError:
        raise ValueError('Exact host/GPU-UUID required') from None
    project = request.get('project')
    if not isinstance(project, str) or not project:
        raise ValueError('Exact owner project required')
    assert_resource_available(s, resource)
    rows = s.rows('SELECT rowid AS row_id, name, project FROM resources WHERE name=?', (resource,))
    if len(rows) != 1 or rows[0]['project'] != project:
        raise ValueError('Exact GPU claim and owner not found or changed')
    revision = s.one('SELECT revision FROM gpu_claim_revision WHERE name=?', (resource,))['revision']
    return {**rows[0], 'revision': revision, 'actor': request.get('actor')}


def _token(identity, reviewed_at):
    data = json.dumps([identity, reviewed_at], sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(_KEY, data, hashlib.sha256).hexdigest()


def unclaim(s, request):
    if request.get('actor') and request['actor'] != s.get('manager'):
        raise ValueError('Only the overseer may administer GPU claims')
    if request.get('commit') is not True:
        identity = _identity(s, request); now = time.time()
        return {'readOnly': True, 'committed': False, 'claim': identity,
                'reviewed_at': now, 'review_token': _token(identity, now),
                'notice': 'Claim only; GPU use and job outcome were not inspected or changed.'}
    if not current_user_authorization(request):
        raise ValueError('Fresh current user authorization and basis required')
    reviewed = request.get('reviewed_at'); identity = request.get('claim'); token = request.get('review_token')
    if type(reviewed) not in (int, float) or not math.isfinite(reviewed) or not 0 <= time.time()-reviewed <= 300:
        raise ValueError('Fresh claim preview required')
    if not isinstance(identity, dict) or not isinstance(token, str) or not hmac.compare_digest(token, _token(identity, reviewed)):
        raise ValueError('Claim preview invalid or supervisor restarted')
    s.db.execute('BEGIN IMMEDIATE')
    try:
        if _identity(s, request) != identity:
            raise ValueError('GPU claim changed since preview')
        deleted = s.db.execute('DELETE FROM resources WHERE rowid=? AND name=? AND project=?',
                               (identity['row_id'], identity['name'], identity['project'])).rowcount
        if deleted != 1:
            raise ValueError('GPU claim changed during commit')
        audit = {'claim': identity, 'basis': request['basis'].strip(), 'at': time.time(),
                 'effect': 'claim removed; GPU usage and job outcome not asserted'}
        s.set('gpu-unclaim-audit:' + identity['name'] + ':' + str(time.time_ns()), json.dumps(audit))
        s.db.execute('COMMIT')
    except Exception:
        s.db.execute('ROLLBACK')
        raise
    return {'committed': True, **audit}
