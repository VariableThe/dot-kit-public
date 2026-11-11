"""Batch protocol tests: per-step durability, partial execution and fences."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

BASE = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), BASE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


J = load('completion-journal')
B = load('completion-journal-batch')


def claim(op='one', owner='worker', method='launch'):
    return {'action': 'claim', 'operationId': op, 'owner': owner, 'method': method,
            'args': {'environmentId': 'env', 'clientRequestId': op, 'message': 'PRIVATE'},
            'context': {'parentTask': 'parent', 'acceptanceCriteria': 'PRIVATE'}}


def batch(gen=1, owner='worker', steps=None):
    return {'action': 'batch', 'operationId': 'one', 'owner': owner, 'generation': gen,
            'steps': steps if steps is not None else [
                {'action': 'checkpoint', 'fields': {'state': 'accepted', 'threadId': 'thread-1', 'runId': 'run-1'}},
                {'action': 'checkpoint', 'fields': {'state': 'watching'}},
            ]}


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'store'
        self.clock = [1000]
        self.journal = J.Journal(self.directory, now=lambda: self.clock[0])
        self.journal.execute(claim())
        self.addCleanup(self.temp.cleanup)

    def run_batch(self, request):
        return B.execute_batch(J, self.directory, request, now=lambda: self.clock[0])

    def test_adjacent_batches_commit_in_order(self):
        first = self.run_batch(batch())
        self.assertTrue(first['ok'])
        self.assertEqual(first['completed'], 2)
        self.assertEqual([r['record']['state'] for r in first['results']], ['accepted', 'watching'])
        second = self.run_batch(batch(steps=[
            {'action': 'checkpoint', 'fields': {'state': 'review_pending', 'resultRunId': 'run-1'}},
            {'action': 'release'},
        ]))
        self.assertTrue(second['ok'])
        record = self.journal.execute({'action': 'read', 'operationId': 'one'})['record']
        self.assertEqual(record['state'], 'review_pending')
        self.assertEqual(record['leaseUntil'], 0)
        self.assertEqual((self.directory / 'journal.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('PRIVATE', (self.directory / 'journal.json').read_text())

    def test_first_step_failure_writes_nothing(self):
        result = self.run_batch(batch(gen=99))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'stale_owner')
        self.assertEqual(result['completed'], 0)
        record = self.journal.execute({'action': 'read', 'operationId': 'one'})['record']
        self.assertEqual(record['state'], 'dispatch_pending')
        self.assertEqual(record['generation'], 1)

    def test_second_step_failure_keeps_first_step(self):
        result = self.run_batch(batch(steps=[
            {'action': 'checkpoint', 'fields': {'state': 'accepted', 'threadId': 'thread-1', 'runId': 'run-1'}},
            {'action': 'checkpoint', 'fields': {'state': 'watching', 'taskComplete': True}},
        ]))
        self.assertFalse(result['ok'])
        self.assertEqual(result['step'], 1)
        self.assertEqual(result['completed'], 1)
        record = self.journal.execute({'action': 'read', 'operationId': 'one'})['record']
        self.assertEqual(record['state'], 'accepted', 'committed prefix is never rolled back')

    def test_partial_persistence_is_uncertain_and_reconciled(self):
        original = J.Journal.save
        calls = {'count': 0}
        def flaky(journal, data):
            calls['count'] += 1
            if calls['count'] == 2:
                original(journal, data)  # atomic replace happened
                raise OSError('simulated after-replace failure')
            return original(journal, data)
        J.Journal.save = flaky
        try:
            result = self.run_batch(batch())
        finally:
            J.Journal.save = original
        self.assertFalse(result['ok'])
        self.assertTrue(result.get('uncertain'))
        self.assertEqual(result['completed'], 1)
        record = self.journal.execute({'action': 'read', 'operationId': 'one'})['record']
        self.assertEqual(record['state'], 'watching', 'caller must reconcile, not assume rollback')

    def test_owner_conflict_and_lease_expiry(self):
        result = self.run_batch(batch(owner='intruder'))
        self.assertEqual(result['error']['code'], 'stale_owner')
        self.assertEqual(result['completed'], 0)
        self.clock[0] = 1200  # lease from 1000 + 180 is still valid
        self.assertTrue(self.run_batch(batch())['ok'])
        self.clock[0] = 1500
        expired = self.run_batch(batch(gen=1, steps=[{'action': 'checkpoint', 'fields': {'state': 'watching'}}]))
        self.assertEqual(expired['error']['code'], 'stale_owner')

    def test_wrong_target_and_unsupported_steps_rejected(self):
        self.run_batch(batch())
        changed = self.run_batch(batch(steps=[
            {'action': 'checkpoint', 'fields': {'state': 'watching', 'threadId': 'thread-2'}},
        ]))
        self.assertEqual(changed['error']['code'], 'identity_changed')
        for action in ('claim', 'read', 'delivery', 'list'):
            rejected = self.run_batch(batch(steps=[{'action': action}]))
            self.assertEqual(rejected['error']['code'], 'unsupported_batch_step')
        self.assertEqual(self.run_batch({'action': 'batch', 'operationId': 'one', 'owner': 'worker',
                                         'generation': 1, 'steps': []})['error']['code'], 'invalid_request')


if __name__ == '__main__':
    unittest.main()
