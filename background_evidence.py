"""Extract native Claude background completions, never guesses from prose."""
import re

_TASK_ID = re.compile(r'^[\w-]+$')

def claude_completed(message):
    outer = message.get('content') or {}
    if not isinstance(outer, dict):
        return None
    if outer.get('type') == 'plaintext':
        outer = outer.get('data') or {}
    if not isinstance(outer, dict) or outer.get('role') != 'agent':
        return None
    inner = outer.get('content') or {}
    if not isinstance(inner, dict) or inner.get('type') != 'output':
        return None
    data = inner.get('data') or {}
    if not isinstance(data, dict):
        return None
    if data.get('type') == 'system' and data.get('subtype') == 'task_notification':
        task, status = data.get('task_id'), data.get('status')
    elif data.get('type') == 'user':
        native = data.get('message') or {}
        blocks = native.get('content', []) if isinstance(native, dict) else []
        if isinstance(blocks, str):
            blocks = [{'type': 'text', 'text': blocks}]
        task = status = None
        for block in blocks if isinstance(blocks, list) else []:
            text = block.get('text', '') if isinstance(block, dict) and block.get('type') == 'text' else ''
            if not isinstance(text, str) or not text.lstrip().startswith('<task-notification>'):
                continue
            id_match = re.search(r'<task-id>\s*([\w-]+)\s*</task-id>', text)
            status_match = re.search(r'<status>\s*(completed|failed)\s*</status>', text)
            if id_match and status_match:
                task, status = id_match.group(1), status_match.group(1)
                break
    else:
        return None
    if isinstance(task, str) and _TASK_ID.fullmatch(task) and status in ('completed', 'failed'):
        return {'taskId': task, 'status': status, 'message': message.get('id')}
    return None
