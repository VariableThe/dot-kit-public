#!/usr/bin/env python3
"""Small explicit batch of adjacent journal transitions. One reviewed command.

Each step still runs through the existing Journal.execute, so every step has its
own validation, lock, atomic fsync write and owner/generation fence. The batch is
NOT transactional: steps stop at the first failure and earlier steps remain
committed. A lost reply is uncertain; callers must reconcile with a read-only
record and never replay a native mutation. Only metadata transitions checkpoint
and release are accepted; claim, delivery, list and arbitrary actions are not.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

MAX_REQUEST_BYTES = 2_000_000
MAX_STEPS = 8
ALLOWED = frozenset(('checkpoint', 'release'))


def load_journal():
    path = Path(__file__).with_name('completion-journal.py')
    spec = importlib.util.spec_from_file_location('completion_journal', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


J = load_journal()


def execute_batch(module, directory, request, now=None):
    if not isinstance(request, dict) or request.get('action') != 'batch':
        return {'ok': False, 'error': {'code': 'invalid_request'}}
    if set(request) - {'action', 'operationId', 'owner', 'generation', 'steps'}:
        return {'ok': False, 'error': {'code': 'invalid_request'}}
    steps = request.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return {'ok': False, 'error': {'code': 'invalid_request'}}
    if not module.identifier(request.get('operationId')) or not module.identifier(request.get('owner')):
        return {'ok': False, 'error': {'code': 'invalid_request'}}
    if type(request.get('generation')) is not int:
        return {'ok': False, 'error': {'code': 'invalid_request'}}
    results = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) - {'action', 'fields'}:
            return {'ok': False, 'error': {'code': 'invalid_request'}, 'step': index,
                    'completed': len(results), 'results': results}
        action = step.get('action')
        if action not in ALLOWED:
            return {'ok': False, 'error': {'code': 'unsupported_batch_step'}, 'step': index,
                    'completed': len(results), 'results': results}
        single = {'action': action, 'operationId': request['operationId'],
                  'owner': request['owner'], 'generation': request['generation']}
        if action == 'checkpoint':
            fields = step.get('fields')
            if not isinstance(fields, dict):
                return {'ok': False, 'error': {'code': 'invalid_request'}, 'step': index,
                        'completed': len(results), 'results': results}
            single['fields'] = fields
        try:
            journal = module.Journal(directory, now=now) if now else module.Journal(directory)
            results.append(journal.execute(single))
        except module.JournalError as error:
            return {'ok': False, 'error': {'code': str(error)}, 'step': index,
                    'completed': len(results), 'results': results}
        except Exception:
            # The failing step's atomic replace may have committed before an fsync
            # error. Do not claim rollback; the caller must reconcile.
            return {'ok': False, 'error': {'code': 'journal_error'}, 'step': index,
                    'completed': len(results), 'results': results, 'uncertain': True}
    return {'ok': True, 'completed': len(results), 'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('too large')
        result = execute_batch(J, args.directory, json.loads(raw))
    except Exception:
        result = {'ok': False, 'error': {'code': 'invalid_request'}}
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result.get('ok') is True else 1


if __name__ == '__main__':
    sys.exit(main())
