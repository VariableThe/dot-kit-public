import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('journal', Path(__file__).with_name('completion-journal.py'))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

def claim(op='one', owner='worker', method='launch', **extra):
    request={'action':'claim','operationId':op,'owner':owner,'method':method,'args':{'environmentId':'env','clientRequestId':op,'message':'PRIVATE PROMPT','title':'test'},'context':{'parentTask':'parent','acceptanceCriteria':'PRIVATE CRITERIA'}}
    if method == 'send':
        request['args']={'environmentId':'env','clientRequestId':op,'threadId':'thread','message':'PRIVATE PROMPT'}
    if method == 'resume':
        request.pop('args'); request.pop('context')
    request.update(extra)
    return request

def race(directory, owner, queue):
    try:
        queue.put(M.Journal(directory).execute(claim(owner=owner))['acquired'])
    except M.JournalError as error:
        queue.put(str(error))

class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'store'
        self.clock=[1000]
        self.journal=M.Journal(self.path,now=lambda:self.clock[0])
        self.addCleanup(self.temp.cleanup)
    def mutate(self,action='checkpoint',**extra):
        return self.journal.execute({'action':action,'operationId':'one','owner':'worker','generation':1,**extra})
    def test_atomic_private_redacted(self):
        self.journal.execute(claim())
        self.mutate(fields={'state':'dispatching','dispatchAttempted':True})
        raw=(self.path/'journal.json').read_text()
        self.assertNotIn('PRIVATE',raw)
        self.assertNotIn('message',raw)
        self.assertEqual((self.path/'journal.json').stat().st_mode & 0o777,0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777,0o700)
        self.assertEqual(list(self.path.glob('.journal-*')),[])
        self.assertEqual(json.loads(raw)['records']['one']['dispatchAttempted'],True)
    def test_failed_replace_preserves_previous_record(self):
        self.journal.execute(claim())
        before=(self.path/'journal.json').read_bytes()
        with patch.object(M.os,'replace',side_effect=OSError('simulated')):
            with self.assertRaises(OSError):
                self.mutate(fields={'state':'watching'})
        self.assertEqual((self.path/'journal.json').read_bytes(),before)
        self.assertEqual(list(self.path.glob('.journal-*')),[])
    def test_hash_mismatch(self):
        self.journal.execute(claim()); self.mutate('release')
        changed=claim(); changed['args']['message']='different'
        with self.assertRaisesRegex(M.JournalError,'request_mismatch'):
            self.journal.execute(changed)
    def test_owner_contention(self):
        self.journal.execute(claim())
        with self.assertRaisesRegex(M.JournalError,'owner_busy'):
            self.journal.execute(claim(owner='other'))
    def test_thread_contention(self):
        self.journal.execute(claim(method='send'))
        with self.assertRaisesRegex(M.JournalError,'thread_busy'):
            self.journal.execute(claim(op='two',method='send'))
    def test_acceptance_thread_conflict_does_not_lose_dispatch(self):
        self.journal.execute(claim(op='two',method='send'))
        self.journal.execute(claim())
        self.mutate(fields={'state':'dispatching','dispatchAttempted':True})
        with self.assertRaisesRegex(M.JournalError,'thread_busy'):
            self.mutate(fields={'state':'accepted','threadId':'thread','runId':'run'})
        record=self.journal.execute({'action':'read','operationId':'one'})['record']
        self.assertTrue(record['dispatchAttempted'])
        self.assertEqual(record['state'],'dispatching')
    def test_expiry_fences_previous_owner(self):
        self.journal.execute(claim())
        self.clock[0]+=301
        result=self.journal.execute(claim(method='resume',owner='replacement'))
        self.assertEqual(result['generation'],2)
        with self.assertRaisesRegex(M.JournalError,'stale_owner'):
            self.mutate(fields={'state':'watching'})
    def test_heartbeat_extends_lease(self):
        self.journal.execute(claim()); self.clock[0]+=200
        record=self.mutate(fields={'state':'watching'})['record']
        self.assertEqual(record['leaseUntil'],1500)
    def test_uncertain_dispatch_preserved(self):
        self.journal.execute(claim())
        self.mutate(fields={'state':'dispatching','dispatchAttempted':True})
        self.mutate(fields={'state':'dispatch_uncertain'})
        self.mutate('release')
        result=self.journal.execute(claim(method='resume'))
        self.assertTrue(result['record']['dispatchAttempted'])
        self.assertEqual(result['record']['state'],'dispatch_uncertain')
        with self.assertRaisesRegex(M.JournalError,'dispatch_already_attempted'):
            self.journal.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':2,'fields':{'dispatchAttempted':True}})
    def test_delivery_uncertainty_cannot_retry(self):
        self.journal.execute(claim()); self.mutate(fields={'state':'review_pending'})
        self.mutate('delivery',state='delivery_pending')
        self.mutate('delivery',state='delivery_uncertain')
        with self.assertRaisesRegex(M.JournalError,'invalid_delivery_transition'):
            self.mutate('delivery',state='delivery_pending')
        with self.assertRaisesRegex(M.JournalError,'delivery_locked'):
            self.mutate(fields={'state':'review_pending'})
    def test_delivery_success(self):
        self.journal.execute(claim()); self.mutate(fields={'state':'review_pending'})
        self.mutate('delivery',state='delivery_pending')
        record=self.mutate('delivery',state='delivered',receiptId='message-123')['record']
        self.assertEqual(record['receiptId'],'message-123')
        self.assertFalse(record['taskComplete'])
    def test_unknown_fields_and_secrets_rejected(self):
        self.journal.execute(claim())
        for fields in ({'prompt':'secret'},{'token':'secret'},{'taskComplete':True},{'link':'https://example.com/?token=secret'}):
            with self.assertRaises(M.JournalError):
                self.mutate(fields=fields)
    def test_identity_immutable(self):
        self.journal.execute(claim())
        self.mutate(fields={'threadId':'thread','runId':'run'})
        with self.assertRaisesRegex(M.JournalError,'identity_changed'):
            self.mutate(fields={'runId':'other'})
    def test_wrapper_metadata(self):
        request=claim(); request['context']['startPosition']=-1
        result=self.journal.execute(request)
        self.assertEqual(result['record']['parentTask'],'parent')
        self.assertEqual(result['record']['startPosition'],-1)
        self.mutate(fields={'state':'retrying_read','resultRunId':None,'attempt':1,'startPosition':-1})
    def test_native_escaped_run_id(self):
        self.journal.execute(claim())
        run='run:thread:mcp%3Alaunch%3Aabc:ordinal:1'
        result=self.mutate(fields={'threadId':'thread','runId':run,'resultRunId':run,'handledResultRunIds':[run]})
        self.assertEqual(result['record']['runId'],run)
    def test_request_key_deduplicated_across_operations(self):
        self.journal.execute(claim())
        request=claim(op='two'); request['args']['clientRequestId']='one'
        with self.assertRaisesRegex(M.JournalError,'request_key_in_use'):
            self.journal.execute(request)
        self.mutate('release')
        with self.assertRaisesRegex(M.JournalError,'request_key_in_use'):
            self.journal.execute(request)
    def test_request_key_scoped_to_environment(self):
        self.journal.execute(claim())
        request=claim(op='two'); request['args']['clientRequestId']='one'; request['args']['environmentId']='other-env'
        self.assertTrue(self.journal.execute(request)['acquired'])
    def test_continuation_requires_same_update_proof(self):
        self.journal.execute(claim())
        self.mutate(fields={'continuationEvidenceRef':'evidence:old'})
        with self.assertRaisesRegex(M.JournalError,'continuation_proof_required'):
            self.mutate(fields={'verifiedResultRunIds':['continuation-run']})
        result=self.mutate(fields={'verifiedResultRunIds':['continuation-run'],'continuationEvidenceRef':'evidence:verified-lineage'})
        self.assertEqual(result['record']['verifiedResultRunIds'],['continuation-run'])
        self.assertEqual(result['record']['continuationEvidenceRef'],'evidence:verified-lineage')
    def test_continuation_empty_list_needs_no_proof(self):
        self.journal.execute(claim())
        self.mutate(fields={'verifiedResultRunIds':[]})
    def test_owned_thread_send_rejected_before_dispatch(self):
        self.journal.execute(claim())
        self.mutate(fields={'state':'accepted','threadId':'thread','runId':'run'})
        with self.assertRaisesRegex(M.JournalError,'thread_busy'):
            self.journal.execute(claim(op='follow-up',method='send'))
        records=self.journal.execute({'action':'list'})['records']
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['operationId'],'one')
    def test_reads_make_no_mutations(self):
        self.journal.execute(claim())
        original_open=M.os.open
        def read_only_open(path, flags, *args, **kwargs):
            self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND),0)
            return original_open(path,flags,*args,**kwargs)
        with patch.object(Path,'mkdir',side_effect=AssertionError('mkdir forbidden')), patch.object(M.os,'open',side_effect=read_only_open), patch.object(M.fcntl,'flock',side_effect=AssertionError('lock forbidden')):
            self.assertEqual(len(self.journal.execute({'action':'list'})['records']),1)
            self.assertEqual(self.journal.execute({'action':'read','operationId':'one'})['record']['operationId'],'one')
    def test_missing_store_read_does_not_create_directory(self):
        with patch.object(Path,'mkdir',side_effect=AssertionError('mkdir forbidden')):
            self.assertEqual(self.journal.execute({'action':'list'}),{'records':[]})
            with self.assertRaisesRegex(M.JournalError,'not_found'):
                self.journal.execute({'action':'read','operationId':'one'})
        self.assertFalse(self.path.exists())
    def test_ttl_floor(self):
        with self.assertRaises(M.JournalError):
            self.journal.execute(claim(ttlSeconds=179))
    def test_real_process_contention(self):
        queue=multiprocessing.Queue()
        processes=[multiprocessing.Process(target=race,args=(self.path,str(i),queue)) for i in range(4)]
        for process in processes: process.start()
        results=[queue.get(timeout=10) for _ in processes]
        for process in processes: process.join(10)
        self.assertEqual(results.count(True),1)
        self.assertEqual(results.count('owner_busy'),3)
    def test_symlink_store_rejected(self):
        self.journal.execute(claim())
        (self.path/'journal.json').unlink()
        (self.path/'journal.json').symlink_to('/dev/null')
        with self.assertRaises(OSError):
            self.journal.execute({'action':'list'})

if __name__ == '__main__':
    unittest.main()
