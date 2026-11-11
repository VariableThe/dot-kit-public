import importlib.util, json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
SPEC=importlib.util.spec_from_file_location('journal',Path(__file__).with_name('completion-journal.py'))
M=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(M)
def claim(op='one',owner='worker',method='launch'):
    req={'action':'claim','operationId':op,'owner':owner,'method':method}
    if method!='resume':
        req.update(args={'environmentId':'env','clientRequestId':op,'message':'PRIVATE PROMPT','title':'test'},
                   context={'parentTask':'parent','acceptanceCriteria':'PRIVATE CRITERIA'})
    return req
class RecoveryJournalTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.path=Path(self.t.name)/'store';self.clock=[1000];self.j=M.Journal(self.path,now=lambda:self.clock[0])
    def accept(self):
        self.j.execute(claim())
        self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':1,'fields':{'state':'accepted','threadId':'thread','runId':'run1','startPosition':90}})
        return 1
    def test_adopt_verified_successor_preserves_origin_and_lineage(self):
        gen=self.accept()
        self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':gen,'fields':{'state':'watching','verifiedResultRunIds':['run6'],'continuationEvidenceRef':'parent-read:evidence-1'}})
        rec=self.j.execute({'action':'adopt','operationId':'one','owner':'worker','generation':gen,'runId':'run6','evidenceRef':'parent-read:evidence-1'})['record']
        self.assertEqual(rec['runId'],'run6');self.assertEqual(rec['originRunId'],'run1');self.assertEqual(rec['lineageRunIds'],['run1','run6'])
        self.assertEqual(rec['threadId'],'thread');self.assertEqual(rec['state'],'watching');self.assertIsNone(rec['resultRunId']);self.assertEqual(rec['lastReadPosition'],90);self.assertEqual(rec['waits'],0)
    def test_adopt_requires_verified_successor_and_matching_evidence(self):
        gen=self.accept()
        with self.assertRaisesRegex(M.JournalError,'successor_not_verified'):
            self.j.execute({'action':'adopt','operationId':'one','owner':'worker','generation':gen,'runId':'run6','evidenceRef':'proof'})
        self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':gen,'fields':{'verifiedResultRunIds':['run6'],'continuationEvidenceRef':'proof'}})
        with self.assertRaisesRegex(M.JournalError,'continuation_proof_required'):
            self.j.execute({'action':'adopt','operationId':'one','owner':'worker','generation':gen,'runId':'run6','evidenceRef':'other'})
    def test_adopt_atomically_clears_stale_review_evidence_and_survives_reopen(self):
        gen=self.accept()
        self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':gen,'fields':{'state':'watching','verifiedResultRunIds':['run6','run7'],'continuationEvidenceRef':'proof'}})
        self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':gen,'fields':{'state':'review_pending','resultRunId':'run6'}})
        self.j.execute({'action':'adopt','operationId':'one','owner':'worker','generation':gen,'runId':'run7','evidenceRef':'proof'})
        reopened=M.Journal(self.path,now=lambda:self.clock[0]).execute({'action':'read','operationId':'one'})['record']
        self.assertEqual(reopened['state'],'watching');self.assertIsNone(reopened['resultRunId']);self.assertFalse(reopened['taskComplete'])
        self.assertEqual(reopened['originRunId'],'run1');self.assertEqual(reopened['lineageRunIds'],['run1','run7'])
    def test_repeated_adoption_is_append_only_and_checkpoint_cannot_rewrite_lineage(self):
        gen=self.accept()
        self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':gen,'fields':{'state':'watching','verifiedResultRunIds':['run6'],'continuationEvidenceRef':'proof'}})
        self.j.execute({'action':'adopt','operationId':'one','owner':'worker','generation':gen,'runId':'run6','evidenceRef':'proof'})
        self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':gen,'fields':{'verifiedResultRunIds':['run6','run7'],'continuationEvidenceRef':'proof'}})
        self.j.execute({'action':'adopt','operationId':'one','owner':'worker','generation':gen,'runId':'run7','evidenceRef':'proof'})
        rec=self.j.execute({'action':'read','operationId':'one'})['record']
        self.assertEqual(rec['originRunId'],'run1');self.assertEqual(rec['lineageRunIds'],['run1','run6','run7']);self.assertEqual(rec['runId'],'run7')
        for fields in ({'originRunId':'attacker'},{'lineageRunIds':['attacker']}):
            with self.assertRaisesRegex(M.JournalError,'lineage_controlled'):
                self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':gen,'fields':fields})
        self.assertEqual(self.j.execute({'action':'read','operationId':'one'})['record']['lineageRunIds'],['run1','run6','run7'])
    def test_inventory_separates_expired_lease_from_thread_safe_claim(self):
        self.j.execute(claim(op='old'));self.j.execute({'action':'checkpoint','operationId':'old','owner':'worker','generation':1,'fields':{'state':'watching','threadId':'thread','runId':'run1','startPosition':0}});self.j.execute({'action':'release','operationId':'old','owner':'worker','generation':1})
        self.j.execute(claim(op='live'));self.j.execute({'action':'checkpoint','operationId':'live','owner':'worker','generation':1,'fields':{'state':'watching','threadId':'thread','runId':'run2','startPosition':0}})
        rows={r['operationId']:r for r in self.j.execute({'action':'inventory'})['records']}
        old=rows['old'];live=rows['live']
        self.assertTrue(old['leaseExpired']);self.assertTrue(old['threadBusy']);self.assertFalse(old['safeToClaim'])
        self.assertEqual(old['blockingOperationIds'],['live']);self.assertEqual(old['blockingOwnerIds'],['worker']);self.assertEqual(old['requiredAction'],'thread_busy_do_not_claim')
        self.assertFalse(live['leaseExpired']);self.assertFalse(live['safeToClaim']);self.assertEqual(live['requiredAction'],'leave_owner_running')
    def test_inventory_only_marks_pre_dispatch_intent_as_safe_to_claim(self):
        self.j.execute(claim(op='pre'))
        row=self.j.execute({'action':'inventory'})['records'][0]
        self.assertTrue(row['leaseExpired'] is False);self.assertFalse(row['safeToClaim']);self.assertEqual(row['requiredAction'],'leave_owner_running')
        self.j.execute({'action':'release','operationId':'pre','owner':'worker','generation':1})
        row=self.j.execute({'action':'inventory'})['records'][0]
        self.assertTrue(row['leaseExpired']);self.assertTrue(row['safeToClaim']);self.assertEqual(row['requiredAction'],'resume_before_dispatch')
    def test_inventory_requires_wake_ownership_verification(self):
        for state in ('await_native_wake','unexpected_wake'):
            self.j.execute(claim(op=state));self.j.execute({'action':'checkpoint','operationId':state,'owner':'worker','generation':1,'fields':{'state':state}});self.j.execute({'action':'release','operationId':state,'owner':'worker','generation':1})
        rows={r['operationId']:r for r in self.j.execute({'action':'inventory'})['records']}
        for state in ('await_native_wake','unexpected_wake'):
            self.assertEqual(rows[state]['requiredAction'],'verify_wake_ownership_do_not_observe');self.assertFalse(rows[state]['safeToClaim'])
    def test_adopt_is_fenced_and_cannot_touch_delivery(self):
        gen=self.accept()
        self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':gen,'fields':{'verifiedResultRunIds':['run6'],'continuationEvidenceRef':'proof'}})
        self.clock[0]+=301
        with self.assertRaisesRegex(M.JournalError,'stale_owner'):
            self.j.execute({'action':'adopt','operationId':'one','owner':'worker','generation':gen,'runId':'run6','evidenceRef':'proof'})
        self.assertEqual(self.j.execute({'action':'read','operationId':'one'})['record']['state'],'accepted')
    def test_inventory_is_read_only_sanitized_and_categorizes_recovery(self):
        gen=self.accept()
        self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':gen,'fields':{'state':'review_pending','resultRunId':'run1'}})
        self.j.execute({'action':'release','operationId':'one','owner':'worker','generation':gen})
        before=(self.path/'journal.json').read_bytes()
        original_open=M.os.open
        def read_only_open(path,flags,*args,**kwargs):
            self.assertEqual(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND),0)
            return original_open(path,flags,*args,**kwargs)
        with patch.object(Path,'mkdir',side_effect=AssertionError('mkdir forbidden')), patch.object(M.os,'open',side_effect=read_only_open), patch.object(M.fcntl,'flock',side_effect=AssertionError('lock forbidden')):
            inv=self.j.execute({'action':'inventory'})
        self.assertEqual(inv['count'],1);self.assertEqual(inv['records'][0]['requiredAction'],'review_result_do_not_redeliver')
        self.assertTrue(inv['records'][0]['leaseExpired']);self.assertTrue(inv['records'][0]['leaseAvailable']);self.assertFalse(inv['records'][0]['safeToClaim']);self.assertEqual(inv['records'][0]['requiredAction'],'review_result_do_not_redeliver')
        self.assertNotIn('PRIVATE',json.dumps(inv));self.assertNotIn('parentTask',json.dumps(inv));self.assertNotIn('requestHash',json.dumps(inv));self.assertEqual((self.path/'journal.json').read_bytes(),before)
    def test_inventory_reports_delivery_uncertain_and_dispatch_uncertain(self):
        self.j.execute(claim());self.j.execute({'action':'checkpoint','operationId':'one','owner':'worker','generation':1,'fields':{'state':'dispatch_uncertain','errorCode':'ETIMEDOUT'}});self.j.execute({'action':'release','operationId':'one','owner':'worker','generation':1})
        inv=self.j.execute({'action':'inventory'})
        self.assertEqual(inv['records'][0]['requiredAction'],'reconcile_exact_request')
        self.assertEqual(inv['records'][0]['errorCode'],'ETIMEDOUT')
        self.j.execute(claim(op='two'));self.j.execute({'action':'checkpoint','operationId':'two','owner':'worker','generation':1,'fields':{'state':'review_pending','resultRunId':'run2'}});self.j.execute({'action':'delivery','operationId':'two','owner':'worker','generation':1,'state':'delivery_pending'});self.j.execute({'action':'delivery','operationId':'two','owner':'worker','generation':1,'state':'delivery_uncertain'});self.j.execute({'action':'release','operationId':'two','owner':'worker','generation':1})
        inv=self.j.execute({'action':'inventory'})['records']
        self.assertEqual(next(r for r in inv if r['operationId']=='two')['requiredAction'],'reconcile_uncertain_delivery')
if __name__=='__main__':unittest.main()
