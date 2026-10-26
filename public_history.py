"""Bounded, exact public-history pagination. Missing input is never a receipt."""
from urllib.parse import quote, urlencode


def read_inputs(api, session, *, message_ids=(), local_ids=(), max_pages=100):
    wanted_messages, wanted_locals = set(message_ids), set(local_ids)
    messages, cursors = {}, set()
    epoch = None
    path = '/sessions/' + quote(session, safe='') + '/messages?limit=200'
    for number in range(max_pages):
        result = api.call(path)
        if not isinstance(result.get('messages'), list):
            raise ValueError('Public history response incomplete')
        meta = result.get('page') or {}
        if number == 0:
            epoch = meta.get('epoch')
        elif meta.get('reset') or meta.get('epoch') != epoch:
            raise ValueError('Conversation history changed during input inspection')
        for message in result['messages']:
            messages[message['id']] = message
        found_locals = {m.get('localId') for m in messages.values()}
        found = (wanted_messages or wanted_locals) and wanted_messages <= messages.keys() and wanted_locals <= found_locals
        if found or not meta.get('hasMore'):
            return list(messages.values()), {'pages': number + 1, 'epoch': epoch,
                'complete': not bool(meta.get('hasMore')), 'exactMatchesFound': bool(found)}
        at, seq = meta.get('nextBeforeAt'), meta.get('nextBeforeSeq')
        if type(at) not in (int, float) or type(seq) is not int or (at, seq) in cursors:
            raise ValueError('Input history pagination incomplete or stalled')
        cursors.add((at, seq))
        # HAPI accepts epoch only with an AFTER cursor. Compare epochs in the
        # responses for backward pagination instead of sending an invalid query.
        path = '/sessions/' + quote(session, safe='') + '/messages?' + urlencode({'limit': 200, 'beforeAt': at, 'beforeSeq': seq})
    raise ValueError('Public input inspection exceeded bounded history; no receipt inferred')
