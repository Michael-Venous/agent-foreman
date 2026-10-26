"""Node-graph management: the hub's active workspace edges decide who is managed and by whom.

Edges are owner -> child. The overseer (the promoted manager) roots every workspace. A child
stays managed until its edge is removed; an idle child's edge sleeps (no scans, no reports).
Nothing here sends work, resumes, stops or changes goals.
"""
import json
import time
from urllib.parse import quote, urlencode
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEED_NOTE = 'Managed before the node graph; seeded as an overseer child'
MID_LEVEL_OPS = {'assignment_delivery', 'steer_assignment', 'assign', 'answer', 'interrupt', 'remind', 'cancel_queued', 'cancel_reminder', 'read', 'read_native',
                 'events', 'ack', 'status', 'requests', 'digest', 'sessions', 'reminders', 'node_graph', 'node_spawn',
                 'node_detach', 'gpu', 'node_sync', 'node_tree', 'node_edit'}
# A node controls the graph below itself: only these node_edit actions, only on chats beneath it.
SUBTREE_EDIT_ACTIONS = ('layout', 'connect', 'disconnect', 'remove')


def active(s):
    return s.get('node-graph-active') == '1'


def overseer(s):
    return s.get('manager')


def owner(s, wid):
    return s.get('owner:' + wid)


def fetch(s):
    """Validate the authoritative tree before any registry/ownership mutation."""
    try:
        graph = s.api.call('/node-graph')
        if not isinstance(graph, dict) or not isinstance(graph.get('graph'), dict) or not isinstance(graph.get('activeWorkspaceId'), str):
            raise ValueError('Malformed node graph response')
        edges = graph['graph'].get('edges')
        if not isinstance(edges, list) or not isinstance(graph['graph'].get('nodes'), list):
            raise ValueError('Incomplete node graph response')
        parents = {}
        for edge in edges:
            if not isinstance(edge, dict) or not all(isinstance(edge.get(k), str) and edge[k] for k in ('owner', 'child')):
                raise ValueError('Malformed ownership edge')
            if edge['child'] in parents or edge['child'] == overseer(s):
                raise ValueError('Conflicting ownership edges')
            parents[edge['child']] = edge['owner']
        done = set()
        for child in parents:
            path = set();current = child
            while current in parents and current not in done:
                if current in path:raise ValueError('Ownership cycle')
                path.add(current);current = parents[current]
            done.update(path)
        s.set('node-graph-active', '1')
        return graph
    except Exception as error:
        # A transient 404 after successful discovery must not revert to classic
        # retirement or route queued events using stale ownership.
        if not active(s) and ('404' in str(error) or 'Not Found' in str(error)):
            s.set('node-graph-active', '0')
        elif active(s):
            if not s.get('node-graph-unavailable'):
                s.set('node-graph-unavailable', time.time())
                s.event('graph-unavailable:' + str(time.time_ns()), None, None, 'graph-error',
                        'Ownership tree unavailable or invalid. Child updates remain queued until verified reconciliation; no work was replayed.')
        raise


def has_local_work(summary):
    """Fresh verified process liveness survives a disconnected chat controller."""
    now = time.time() * 1000
    return any(isinstance(p, dict) and isinstance(p.get('checkedAt'), (int, float)) and 0 <= now - p['checkedAt'] <= 15000
               for p in (summary or {}).get('backgroundProcesses', []))


def idle(summary):
    if not summary:return True
    if has_local_work(summary):return False
    if not summary.get('active'):return True
    return not (summary.get('thinking') or summary.get('backgroundTaskCount', 0) or summary.get('pendingRequestsCount', 0))


def worker_for(s, session):
    rows = s.rows("SELECT * FROM workers WHERE session=? AND state NOT IN ('launch-failed') ORDER BY created DESC", (session,))
    return rows[0] if rows else None


def set_edge(s, owner_id, child, created_by):
    return s.api.call('/node-graph/edges', {'owner': owner_id, 'child': child, 'createdBy': created_by})


def remove_edge(s, child):
    return s.api.call('/node-graph/edges/remove', {'child': child})


def seed(s, graph):
    """Once: classic managed workers become overseer children; the overseer stays the root."""
    if s.get('node-graph-seeded') or not overseer(s):
        return False
    children = {e['child'] for e in graph['graph']['edges']}
    for w in s.rows("SELECT * FROM workers WHERE session IS NOT NULL AND state='managed'"):
        if w['session'] == overseer(s) or w['session'] in children or s.watch(w['id'])['mode'] == 'off':
            continue
        set_edge(s, overseer(s), w['session'], 'supervisor-seed')
    s.set('node-graph-seeded', json.dumps({'at': time.time(), 'note': SEED_NOTE}))
    return True


def remanage(s, w):
    """Edge present: the edge itself is the current authorization to manage this chat."""
    if s.get('unmanaged:' + w['id']):
        s.set('watch:' + w['id'], s.get('unmanaged-watch:' + w['id']) or json.dumps({'mode': 'events', 'note': ''}))
        for key in ('unmanaged:', 'retirement-notify:', 'retirement-candidate:'):
            s.db.execute('DELETE FROM settings WHERE key=?', (key + w['id'],))
    if w['state'] == 'released':
        for key in ('observation:', 'host:', 'asleep:'):
            s.db.execute('DELETE FROM settings WHERE key=?', (key + w['id'],))
        s.db.execute("UPDATE workers SET state='managed' WHERE id=?", (w['id'],))
    if s.watch(w['id'])['mode'] == 'off':
        s.set('watch:' + w['id'], json.dumps({'mode': 'events', 'note': ''}))


def adopt_session(s, session, owner_id):
    """A graph edge to a chat the supervisor never registered: register it observation-only."""
    from supervisor import native, uid
    current = s.session(session)
    meta = current.get('metadata') or {}
    runtime = meta.get('flavor') or 'claude'
    if runtime not in ('codex', 'claude'):
        raise ValueError('Unsupported runtime for management: ' + str(runtime))
    path = meta.get('path') or str(ROOT)
    title = meta.get('name') or (meta.get('summary') if isinstance(meta.get('summary'), dict) else {}).get('text')
    project = uid()
    s.db.execute('INSERT INTO projects(id,name,repo,objective,status) VALUES(?,?,?,?,?)',
                 (project, 'Node · ' + (title or session[:8]), path, 'Graph node managed by its owner ' + owner_id[:8], 'running'))
    wid = uid()
    s.db.execute('INSERT INTO workers VALUES(?,?,?,?,?,?,?,?)', (wid, project, runtime, native(current), session, path, 'managed', time.time()))
    return s.worker(wid)


def reconcile(s):
    """Match the registry to the active workspace. Returns {session: summary} for sleep checks."""
    graph = fetch(s)
    if seed(s, graph):
        graph = fetch(s)
    root = overseer(s)
    edges = {e['child']: e['owner'] for e in graph['graph']['edges'] if e['child'] != root}
    s.set('node-graph-cache', json.dumps({'workspace': graph['activeWorkspaceId'], 'edges': edges, 'at': time.time()}))
    for w in s.rows("SELECT * FROM workers WHERE session IS NOT NULL AND state='managed'"):
        if w['session'] == root or w['session'] in edges:
            continue
        if s.get('node-edge-seen:' + w['id']):
            # Edge removed (or the workspace switched): stop managing quietly; work is untouched.
            s.end_management(w,reason='Disconnected from the active node graph or workspace')
            s.set('watch:' + w['id'], json.dumps({'mode': 'off', 'note': 'Removed from the active node graph'}))
            for key in ('node-edge-seen:', 'owner:', 'asleep:'):
                s.db.execute('DELETE FROM settings WHERE key=?', (key + w['id'],))
        elif s.watch(w['id'])['mode'] != 'off':
            # Newly managed through a classic operation: it becomes an overseer child.
            set_edge(s, root, w['session'], 'overseer')
            edges[w['session']] = root
            s.set('node-edge-seen:' + w['id'], '1'); s.set('owner:' + w['id'], root)
    for child, owner_id in edges.items():
        # One invalid/import-incompatible child must not starve other branches.
        s.db.execute('SAVEPOINT node_child')
        try:
            w = worker_for(s, child)
            if w and w['state'] in ('registered', 'launching', 'adopting'):
                s.db.execute('RELEASE node_child');continue
            if not w:w = adopt_session(s, child, owner_id)
            elif w['state'] == 'released' or s.get('unmanaged:' + w['id']) or s.watch(w['id'])['mode'] == 'off':remanage(s, w)
            s.set('node-edge-seen:' + w['id'], '1')
            if owner(s, w['id']) != owner_id:s.set('owner:' + w['id'], owner_id)
            s.db.execute('DELETE FROM settings WHERE key=?', ('node-registration-error:' + child,))
            s.db.execute('RELEASE node_child')
        except Exception:
            s.db.execute('ROLLBACK TO node_child');s.db.execute('RELEASE node_child')
            key = 'node-registration-error:' + child
            if not s.get(key):
                s.set(key,time.time())
                s.event('node-registration-error:' + child + ':' + str(time.time_ns()), None, None, 'node-error',
                        'Cannot register/observe graph node ' + child + '. Other branches continue; inspect this exact chat before relying on its oversight.')
    s.db.execute("DELETE FROM settings WHERE key='node-graph-unavailable'")
    return graph


def summaries(s):
    return {x['id']: x for x in s.api.call('/sessions')['sessions']}


def should_scan(s, w, summary):
    """Busy children are scanned; an idle child gets one final scan, then its edge sleeps."""
    key = 'asleep:' + w['id']
    if not idle(summary):
        s.db.execute('DELETE FROM settings WHERE key=?', (key,))
        return True
    sleeping=s.get(key)
    if sleeping:
        # One final read for a newly projected terminal error, even if the chat
        # was already asleep when the failure arrived. No periodic idle scans.
        previous=json.loads(sleeping)
        if not isinstance(previous,dict):previous={}
        return (summary or {}).get('lastFailureAt') != previous.get('failureAt')
    return True


def scan_completed(s, w, session):
    """Sleep only after a successful final observation, never before an API failure."""
    summary = {**session, 'pendingRequestsCount': session.get('pendingRequestsCount', 0)
               or len((session.get('agentState') or {}).get('requests') or {})}
    key = 'asleep:' + w['id']
    if idle(summary):
        s.set(key, json.dumps({'at': time.time(), 'failureAt': session.get('lastFailureAt')}))
    else:
        s.db.execute('DELETE FROM settings WHERE key=?', (key,))


def asleep(s, wid):
    return bool(s.get('asleep:' + wid))


def recipient(s, event, edges=None):
    """Events go to the child's direct owner; overseer children (and global events) to the overseer."""
    root = overseer(s)
    if not active(s) or not event['worker']:
        return root
    if edges is not None:
        child = s.worker(event['worker'])['session']
        if child not in edges:raise ValueError('Child not owned in the current tree; keep event queued')
        return edges[child]
    if s.get('node-graph-unavailable'):raise ValueError('Ownership unavailable; preserve queued child event')
    return owner(s, event['worker']) or root


def descendants(edges, root):
    """Every chat strictly below root in an owner->child tree ({child: owner})."""
    kids = {}
    for child, owner_id in edges.items():kids.setdefault(owner_id, []).append(child)
    below, queue = set(), list(kids.get(root, []))
    while queue:
        node = queue.pop()
        if node in below or node == root:continue
        below.add(node); queue.extend(kids.get(node, []))
    return below


def subtree_of(s, actor):
    """(active workspace id, descendants of actor, current {child: owner})."""
    graph = fetch(s)
    edges = {e['child']: e['owner'] for e in graph['graph']['edges']}
    return graph['activeWorkspaceId'], descendants(edges, actor), edges


def actor_check(s, o):
    """Mid-level agents manage their own children and the graph below them; the overseer/user keep full control."""
    actor = o.get('actor')
    # Subtree control needs a live node graph; before node mode starts there is no ownership to scope to.
    if actor and actor != overseer(s) and o['op'] in ('node_tree', 'node_edit') and not active(s):
        raise ValueError('Only the overseer may use ' + o['op'] + ' until the node graph is active')
    if not actor or actor == overseer(s) or not active(s):
        return
    op = o['op']
    if op not in MID_LEVEL_OPS:
        raise ValueError('Only the overseer may use ' + op + ' in the node graph')
    if op == 'node_detach':
        # Any level below you may be detached (with the same explicit-user-request rule as direct children).
        _, below, _ = subtree_of(s, actor)
        if o.get('child') in below:return
        raise ValueError('You can only detach chats below you in the graph')
    if op in ('node_tree', 'node_edit'):return  # scoped to the actor's subtree in dispatch
    target = None
    if op == 'cancel_queued' and o.get('assignment'):
        target = s.worker(s.one('SELECT worker FROM assignments WHERE id=?',(o['assignment'],))['worker'])
    elif op == 'cancel_reminder':
        reminder = s.one('SELECT worker FROM reminders WHERE id=?',(o['id'],))
        if not reminder['worker']:raise ValueError('Only the overseer may cancel global reminders')
        target = s.worker(reminder['worker'])
    elif o.get('worker'):
        target = s.worker(o['worker'])
    elif o.get('session') and op not in ('read',):
        target = worker_for(s, o['session'])
    elif op == 'node_detach':
        target = worker_for(s, o.get('child', ''))
        if not target and o.get('child'):
            graph = fetch(s)
            if {e['child']:e['owner'] for e in graph['graph']['edges']}.get(o['child']) != actor:
                raise ValueError('You can only detach your own children')
            return
    live_edges = None
    if op in {'assign','steer_assignment','answer','interrupt','remind','cancel_queued','cancel_reminder','ack','node_detach'} and (target or op in {'ack','node_detach'}):
        current = fetch(s)
        live_edges = {edge['child']: edge['owner'] for edge in current['graph']['edges']}
    if target and live_edges is not None and live_edges.get(target['session']) != actor:
        raise ValueError('You can only manage your own children in the current graph')
    if op == 'ack':
        for event_id in o.get('ids', []):
            row = s.rows('SELECT worker FROM events WHERE id=?', (event_id,))
            if row and (not row[0]['worker'] or owner(s, row[0]['worker']) != actor or (live_edges is not None and live_edges.get(s.worker(row[0]['worker'])['session']) != actor)):
                raise ValueError('You can only acknowledge events of your own children')
    if target and owner(s, target['id']) != actor:
        raise ValueError('You can only manage your own children')


def gpu_ledger(s):
    return json.loads(s.get('gpu-advisory') or '[]')


def gpu(s, o):
    """Advisory GPU reservations the overseer tracks; warnings only, never a hard block."""
    action = o.get('action', 'list'); ledger = gpu_ledger(s)
    personal = [json.loads(r['value']) for r in s.rows("SELECT value FROM settings WHERE key LIKE 'resource-reservation:%' AND key NOT LIKE '%:audit:%'")]
    view = {'reservations': ledger, 'personal': [p for p in personal if p.get('reserved')]}
    if action == 'list':
        return view
    name = str(o.get('gpu', '')).strip()
    if not name:
        raise ValueError('gpu name required, e.g. "main 3090 Ti"')
    actor = o.get('actor')
    if action == 'request' or (action == 'reserve' and actor and actor != overseer(s)):
        # Mid-level agents ask; the overseer decides and records.
        w = worker_for(s, actor) if actor else None
        s.event('gpu-request:' + (actor or 'user') + ':' + str(time.time_ns()), w['project'] if w else None, w['id'] if w else None,
                'gpu-request', 'Requests GPU "' + name + '"' + (': ' + str(o.get('note'))[:300] if o.get('note') else '') + '. Advisory: check current reservations and the user\'s allocation.')
        return {'requested': True, **view}
    holder = str(o.get('holder') or actor or 'user')
    conflicts = [r for r in ledger if r['gpu'].lower() == name.lower() and r['holder'] != holder]
    if action == 'reserve':
        if conflicts and o.get('force') is not True:
            return {'recorded': False, 'warning': 'Already reserved by ' + ', '.join(r['holder'] for r in conflicts) + '; pass force:true to record anyway', **view}
        ledger = [r for r in ledger if not (r['gpu'].lower() == name.lower() and r['holder'] == holder)]
        ledger.append({'gpu': name, 'holder': holder, 'note': str(o.get('note', ''))[:300], 'at': time.time(), 'by': actor or 'user'})
        s.set('gpu-advisory', json.dumps(ledger))
        return {'recorded': True, 'warning': 'Shared with ' + ', '.join(r['holder'] for r in conflicts) if conflicts else None, 'reservations': ledger, 'personal': view['personal']}
    if action == 'release':
        ledger = [r for r in ledger if not (r['gpu'].lower() == name.lower() and (r['holder'] == holder or o.get('all') is True))]
        s.set('gpu-advisory', json.dumps(ledger))
        return {'released': True, 'reservations': ledger, 'personal': view['personal']}
    raise ValueError('gpu action must be list, reserve, release or request')


def spawn(s, o):
    """New child chat for the acting owner (or a given owner when the overseer acts)."""
    actor = o.get('actor')
    parent = o.get('owner') if (not actor or actor == overseer(s)) and o.get('owner') else (actor or overseer(s))
    if actor and actor != overseer(s) and active(s) and o.get('owner') and o['owner'] != actor:
        # Create children at any level below you, never elsewhere in the graph.
        _, below, _ = subtree_of(s, actor)
        if o['owner'] not in below:raise ValueError('You can only create children for yourself or chats below you')
        parent = o['owner']
    if not parent:
        raise ValueError('No owner for the new child')
    name = str(o.get('name', '')).strip()
    if not name:
        raise ValueError('Child name required')
    project = s.dispatch({'op': 'register', 'name': name, 'repo': o['directory'], 'objective': str(o.get('objective', ''))[:2000]})
    s.dispatch({'op': 'resume', 'project': project['id']})
    if o.get('worktree'):
        # A checkout already used by another worker needs its own worktree; the owner integrates it.
        s.dispatch({'op': 'update', 'project': project['id'], 'integration_owner': parent})
    w = s.dispatch({'op': 'launch', 'project': project['id'], 'runtime': o.get('runtime', 'claude'),
                    'permission': o.get('permission', 'full'), 'worktree': bool(o.get('worktree'))})
    set_edge(s, parent, w['session'], actor or 'overseer')
    s.set('node-edge-seen:' + w['id'], '1'); s.set('owner:' + w['id'], parent)
    result = {'worker': w, 'owner': parent}
    if o.get('text'):
        result['assignment'] = s.dispatch({'op': 'assign', 'worker': w['id'], 'text': o['text'], 'key': o.get('key') or 'spawn:' + w['id']})
    return result


def disconnect_authorized(o):
    # UI controls are direct user actions (no agent actor). Agent calls must cite a
    # current explicit instruction; completion/idle and old retention rules are not one.
    return not o.get('actor') or (o.get('user_requested') is True and isinstance(o.get('basis'), str) and bool(o['basis'].strip()))


CONNECTION_POLICY = 'Idle/completed nodes stay connected. Disconnect only on an explicit current user request; agent calls require user_requested:true and basis quoting that request.'


EDIT_ACTIONS = ('add', 'remove', 'layout', 'connect', 'disconnect', 'routing', 'annotation', 'rename')


def tree(s, o):
    query = '?' + urlencode({'workspaceId': o['workspace']}) if o.get('workspace') else ''
    result = s.api.call('/node-graph/tree' + query)
    live = summaries(s)
    workers = {w['session']: w for w in s.rows("SELECT * FROM workers WHERE session IS NOT NULL AND state != 'launch-failed' ORDER BY created")}
    paused = result.get('pausedAt', {})
    def details(sid, active_workspace, has_owner=False):
        summary = live.get(sid) or {}; meta = summary.get('metadata') or {}; worker = workers.get(sid)
        summary_title = meta['summary'].get('text') if isinstance(meta.get('summary'), dict) else None
        title = meta.get('name') or summary_title
        managed = bool(active_workspace and has_owner and worker and worker['state'] == 'managed' and not s.get('unmanaged:' + worker['id']) and s.watch(worker['id'])['mode'] != 'off')
        if sid in paused: status = 'Stopped'
        elif has_local_work(summary) and not summary.get('active'): status = 'Processes'
        elif summary.get('thinking') or summary.get('pendingRequestsCount'): status = 'Running'
        elif summary.get('active') and summary.get('backgroundTaskCount'): status = 'Background'
        elif summary.get('active'): status = 'Idle'
        else: status = 'Inactive' if summary else 'Unavailable'
        return {'title': title or sid[:8], 'runtime': meta.get('flavor'), 'status': status,
                'active': bool(summary.get('active')), 'thinking': bool(summary.get('thinking')),
                'backgroundTaskCount': summary.get('backgroundTaskCount', 0), 'pendingRequestsCount': summary.get('pendingRequestsCount', 0),
                'worker': worker['id'] if worker else None, 'managed': managed,
                'asleep': bool(managed and asleep(s, worker['id'])), 'pausedAt': paused.get(sid)}
    result['controls'] = {'read': 'node_tree', 'edit': 'node_edit', 'actions': list(EDIT_ACTIONS), 'versionRequired': True, 'dryRun': True, 'documentation': str(ROOT / 'manager/NODES.md'), 'connectionPolicy': CONNECTION_POLICY}
    result['root'] = {'sessionId': result.get('overseer'), **details(result['overseer'], True)} if result.get('overseer') else None
    for snapshot in result['workspaces']:
        graph = snapshot['graph']; active_workspace = snapshot['workspace']['id'] == result['activeWorkspaceId']
        owned = {edge['child'] for edge in graph['edges']}
        for node in graph['nodes']: node.update(details(node['sessionId'], active_workspace, node['sessionId'] in owned))
        children = {}
        for edge in graph['edges']: children.setdefault(edge['owner'], []).append(edge['child'])
        snapshot['routing'] = {'junctions': graph.get('junctions', []), 'edges': [{key: edge[key] for key in ('owner', 'child', 'via') if key in edge} for edge in graph['edges']]}
        snapshot['children'] = children
        snapshot['lone'] = [node['sessionId'] for node in graph['nodes'] if node['sessionId'] not in owned and node['sessionId'] != result.get('overseer')]
        snapshot['active'] = active_workspace
        if o.get('summary') is True:
            snapshot['counts'] = {key: len(graph.get(key, [])) for key in ('nodes', 'edges', 'junctions', 'annotations')}
            for key in ('graph', 'children', 'lone', 'routing'): snapshot.pop(key, None)
    return result


def scoped_tree(s, o):
    """A node's view of the active workspace: itself and everything below it, nothing else."""
    actor = o['actor']
    workspace, below, _ = subtree_of(s, actor)
    result = tree(s, {'workspace': workspace})
    inside = below | {actor}
    result['workspaces'] = [w for w in result['workspaces'] if w['workspace']['id'] == workspace]
    for snapshot in result['workspaces']:
        graph = snapshot['graph']
        graph['nodes'] = [n for n in graph['nodes'] if n['sessionId'] in inside]
        graph['edges'] = [e for e in graph['edges'] if e['child'] in below and e['owner'] in inside]
        graph['junctions'] = [j for j in graph.get('junctions', []) if j.get('owner') in inside]
        graph['annotations'] = []
        snapshot['children'] = {k: [c for c in v if c in below] for k, v in snapshot.get('children', {}).items() if k in inside}
        snapshot['lone'] = []
        snapshot['routing'] = {'junctions': graph['junctions'], 'edges': [{key: e[key] for key in ('owner', 'child', 'via') if key in e} for e in graph['edges']]}
    result['root'] = {'sessionId': actor, 'scope': 'subtree'}
    result['controls'] = {'read': 'node_tree', 'edit': 'node_edit', 'scope': 'subtree', 'actions': list(SUBTREE_EDIT_ACTIONS), 'versionRequired': True, 'dryRun': True,
                          'create': 'node_spawn (owner: yourself or any chat below you)', 'documentation': str(ROOT / 'manager/NODES.md'), 'connectionPolicy': CONNECTION_POLICY}
    result.pop('overseer', None)
    return result


def check_subtree_edit(s, o):
    """Reject any change that touches a chat outside the actor's subtree or an action that is workspace-wide."""
    actor = o['actor']
    workspace, below, _ = subtree_of(s, actor)
    if o.get('workspace') != workspace:raise ValueError('Subtree edits apply to the active workspace')
    inside = below | {actor}
    for change in o.get('changes') or []:
        action = change.get('action') if isinstance(change, dict) else None
        if action not in SUBTREE_EDIT_ACTIONS:
            raise ValueError('Below your level you may only: ' + ', '.join(SUBTREE_EDIT_ACTIONS) + ' (spawn new chats with node_spawn)')
        if action == 'layout' and not (isinstance(change.get('positions'), dict) and set(change['positions']) <= inside):
            raise ValueError('Layout only for yourself and chats below you')
        if action == 'connect' and not (change.get('child') in below and change.get('owner') in inside and change.get('child') != change.get('owner')):
            raise ValueError('Connect only re-owns chats below you, under you or another chat below you')
        if action == 'disconnect' and change.get('child') not in below:raise ValueError('Disconnect only chats below you')
        if action == 'remove' and not (isinstance(change.get('ids'), list) and change['ids'] and set(change['ids']) <= below):
            raise ValueError('Remove only chats below you')


def edit(s, o):
    actor = o.get('actor')
    if actor and actor != overseer(s) and active(s):check_subtree_edit(s, o)
    workspace, version, changes = o.get('workspace'), o.get('version'), o.get('changes')
    if not isinstance(workspace, str) or not workspace or not isinstance(version, str) or len(version) != 64 or any(c not in '0123456789abcdef' for c in version):
        raise ValueError('node_edit requires an exact workspace id and version from node_tree')
    if not isinstance(changes, list) or not 1 <= len(changes) <= 100 or any(not isinstance(change, dict) or change.get('action') not in EDIT_ACTIONS for change in changes):
        raise ValueError('Expected 1-100 graph changes: ' + ', '.join(EDIT_ACTIONS))
    if not isinstance(o.get('dryRun', False), bool): raise ValueError('dryRun must be a boolean')
    # The hub validates shapes, namespace, topology, busy removal and the version in one transaction.
    # It queues one graph reconciliation only after an applied active-workspace topology change.
    return s.api.call('/node-graph/workspaces/' + quote(workspace, safe='') + '/edit',
                      {'version': version, 'changes': changes, 'dryRun': o.get('dryRun', False), 'createdBy': o.get('actor') or 'user', 'preserveConnections': not disconnect_authorized(o)})


def dispatch(s, o):
    op = o['op']
    if active(s) and op in ('unmanage', 'node_detach') and not disconnect_authorized(o):
        raise ValueError(CONNECTION_POLICY)
    if op == 'node_sync':
        graph = reconcile(s); s.publish_managed()
        return {'ok': True, 'workspace': graph['activeWorkspaceId']}
    if op == 'node_tree':
        if o.get('actor') and o['actor'] != overseer(s) and active(s):return scoped_tree(s, o)
        return tree(s, o)
    if op == 'node_edit':
        return edit(s, o)
    if op == 'node_graph':
        graph = fetch(s)
        graph['controls'] = {'read': 'node_tree', 'edit': 'node_edit', 'scope': 'overseer', 'versionRequired': True, 'dryRun': True, 'documentation': str(ROOT / 'manager/NODES.md'), 'connectionPolicy': CONNECTION_POLICY}
        workers = {w['session']: w for w in s.rows('SELECT * FROM workers WHERE session IS NOT NULL') if w['state'] != 'launch-failed'}
        for node in graph['graph']['nodes']:
            w = workers.get(node['sessionId'])
            node['worker'] = w['id'] if w else None
            node['managed'] = bool(w and w['state'] == 'managed')
            node['asleep'] = bool(w and asleep(s, w['id']))
        return graph
    if op == 'node_edge':
        if o.get('actor') and o['actor'] != overseer(s):
            raise ValueError('Mid-level agents can spawn or detach children, not attach existing chats')
        graph = set_edge(s, o['owner'], o['child'], o.get('actor') or 'overseer'); reconcile(s); s.publish_managed()
        return graph
    if op == 'node_detach':
        graph = remove_edge(s, o['child']); reconcile(s); s.publish_managed()
        return graph
    if op == 'node_spawn':
        result = spawn(s, o); s.publish_managed()
        return result
    if op == 'gpu':
        return gpu(s, o)
    return None
