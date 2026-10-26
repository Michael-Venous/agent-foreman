#!/usr/bin/env python3
"""Run a finite development helper with a deadline; supervise only its direct PID.

Use HAPI_PROCESS_ROLE=helper in the launching environment only for support
infrastructure. Ordinary tests/render jobs retain their normal work accounting.
No shell, shared-browser shutdown, process-tree kill or service control is used.
"""
import argparse
import math
import signal
import subprocess
import sys


class HelperStopped(Exception):
    def __init__(self, signum):
        self.signum = signum


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0 or seconds > 86400:
        raise argparse.ArgumentTypeError('Expected seconds greater than zero and at most 86400')
    return seconds


def stop_direct_child(child, grace):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def run(command, timeout, grace=3):
    child = subprocess.Popen(command)
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print('Development helper exceeded its deadline; stopping its direct process.', file=sys.stderr)
        stop_direct_child(child, grace)
        return 124
    except KeyboardInterrupt:
        stop_direct_child(child, grace)
        return 130
    except HelperStopped as stop:
        stop_direct_child(child, grace)
        return 128 + stop.signum


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=positive_seconds, required=True)
    parser.add_argument('--grace', type=positive_seconds, default=3)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    command = options.command[1:] if options.command[:1] == ['--'] else options.command
    if not command:
        parser.error('A command is required after --')
    def stopped(signum, frame):
        raise HelperStopped(signum)
    previous = signal.signal(signal.SIGTERM, stopped)
    try:
        return run(command, options.timeout, options.grace)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    sys.exit(main())
