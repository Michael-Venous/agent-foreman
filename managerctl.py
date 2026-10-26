#!/usr/bin/env python3
import json
import os
import sys
import time
import uuid
from pathlib import Path

USAGE = 'Usage: managerctl.py JSON | managerctl.py --file FILE | managerctl.py - (stdin); see TOOLS.md'


def read_command(args, stdin=None):
    if len(args) == 2 and args[0] == '--file':
        command = json.loads(Path(args[1]).read_text())
    elif len(args) == 1:
        command = json.loads((stdin or sys.stdin).read() if args[0] == '-' else args[0])
    else:
        raise ValueError(USAGE)
    if not isinstance(command, dict) or not isinstance(command.get('op'), str) or not command['op']:
        raise ValueError('Expected a JSON object with an op')
    return command


def stamp_command(command, environ=None):
    command = dict(command)
    if command.get('op') in ('resume_chat', 'steer_assignment', 'steer_queued', 'supersede_assignment', 'resource_handoff', 'resource_reservation', 'cancel_queued'):
        command['requested_at'] = time.time()
    # Actor is always the calling HAPI chat, never supplied by JSON, a file or stdin.
    command.pop('actor', None)
    actor = (os.environ if environ is None else environ).get('HAPI_SESSION_ID')
    if actor:
        command['actor'] = actor
    return command


def main(args):
    if args in (['-h'], ['--help']):
        print(USAGE)
        return 0
    try:
        command = stamp_command(read_command(args))
    except (ValueError, OSError) as error:
        sys.exit(str(error))
    directory = Path(__file__).resolve().parent / 'manager/control'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = str(uuid.uuid4())
    temporary, request, output = (directory / (key + suffix) for suffix in ('.tmp', '.req', '.out'))
    temporary.write_text(json.dumps(command)); temporary.chmod(0o600); temporary.rename(request)
    for _ in range(600):
        if output.exists():
            result = json.loads(output.read_text()); output.unlink(); print(json.dumps(result, indent=2))
            return 1 if 'error' in result else 0
        time.sleep(.5)
    sys.exit('Control request timed out. Do not repeat mutations blindly: inspect status and request ' + key)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
