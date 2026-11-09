import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { classifyCompletion as classify, payload } from './completion-guard.mjs';

function evidence() {
  return {
    expected: { environmentId: 'host-a', threadId: 'thread-a', runId: 'run-a', untilDelegatedWorkSettled: true },
    wait: { threadId: 'thread-a', runId: 'run-a', status: 'completed', timedOut: false, resultRun: {runId:'run-a', status:'completed'} },
    read: {
      thread: { threadId:'thread-a', latestRunId:'run-a', activeRunId:null, pendingRequestCount:0 },
      recentRuns: [{runId:'run-a', status:'completed'}], hasMore:false,
      items: [{runId:'run-a', type:'assistant_message', status:'completed', text:'Finished. Tests passed.', textTruncated:false, nextTextOffset:null}]
    }
  };
}
const state = value => classify(value).state;

test('coherent result is ready for review, never task complete', () => {
  const result=classify(evidence()); assert.equal(result.state,'ready_for_review'); assert.equal(result.taskComplete,false);
});
test('completed parent with live delegated work stays waiting', () => {
  const e=evidence(); e.wait.timedOut=true; assert.equal(state(e),'waiting');
});
test('completed parent without settled-work evidence needs another wait', () => {
  const e=evidence(); delete e.wait.resultRun; assert.equal(state(e),'needs_settled_wait');
  e.wait.resultRun={runId:'run-a',status:'completed'}; e.expected.untilDelegatedWorkSettled=false; assert.equal(state(e),'needs_settled_wait');
});
test('a wake hint cannot silently strand an outside caller', () => {
  const e=evidence(); e.wait.timedOut=true; e.wait.next='Completion wakes this thread';
  assert.equal(state(e),'unexpected_wake'); e.expected.callerOwnsDelegatedTask=true; assert.equal(state(e),'await_native_wake');
});
test('approval and question blockers are not success', () => {
  const e=evidence(); e.read.thread.pendingRequestCount=1; assert.equal(state(e),'blocked');
  e.read.thread.pendingRequestCount=0; e.wait.status='waiting'; e.wait.timedOut=true; assert.equal(state(e),'blocked');
});
for (const status of ['failed','cancelled','interrupted','rolled_back']) {
  test(`terminal ${status} is not successful completion`, () => {
    const e=evidence(); e.wait.status=status; const result=classify(e); assert.equal(result.state,'run_ended_unsuccessfully'); assert.equal(result.taskComplete,false);
  });
}
test('idle or missing run cannot satisfy a task', () => {
  const e=evidence(); e.wait.status='idle'; e.wait.runId=null; assert.equal(state(e),'no_run');
});
test('different thread and restarted run require explicit reconciliation', () => {
  const e=evidence(); e.wait.threadId='thread-b'; assert.equal(state(e),'target_mismatch');
  e.wait.threadId='thread-a'; e.wait.runId='run-b'; assert.equal(state(e),'run_changed');
});
test('a different result run needs verified continuation lineage', () => {
  const e=evidence(); e.wait.resultRun.runId='run-b'; assert.equal(state(e),'result_run_changed');
  e.expected.verifiedResultRunIds=['run-b']; e.read.thread.latestRunId='run-b'; e.read.recentRuns=[{runId:'run-b',status:'completed'}]; e.read.items[0].runId='run-b';
  assert.equal(state(e),'ready_for_review');
});
test('unread or truncated result needs reading', () => {
  const e=evidence(); delete e.read; assert.equal(state(e),'needs_result_read');
  for (const alter of [e=>e.read.hasMore=true,e=>e.read.items=[],e=>e.read.items[0].textTruncated=true,e=>e.read.items[0].nextTextOffset=12,e=>e.read.items[0].text='',e=>e.read.items[0].status='running']) {
    const e=evidence(); alter(e); assert.equal(state(e),'needs_result_read');
  }
});
test('new human work must not be confused with the tracked result', () => {
  const e=evidence(); e.read.thread.activeRunId='run-b'; assert.equal(state(e),'newer_work_active');
  e.read.thread.activeRunId=null; e.read.thread.latestRunId='run-b'; assert.equal(state(e),'newer_result');
});
test('handled results do not generate duplicate handoffs', () => {
  const e=evidence(); e.handledResultRunIds=['run-a']; assert.equal(state(e),'already_handled');
});
test('missing and error responses do not imply work stopped or completed', () => {
  const e=evidence(); e.wait={isError:true, content:[{type:'text',text:'timeout'}]}; assert.equal(state(e),'read_error');
  e.wait=evidence().wait; e.read={isError:true}; assert.equal(state(e),'read_error');
});
test('MCP envelopes decode without swallowing errors', () => {
  const e=evidence(); e.wait={structuredContent:e.wait}; e.read={content:[{type:'text',text:JSON.stringify(e.read)}]}; assert.equal(state(e),'ready_for_review');
  assert.equal(payload({structuredContent:{isError:true}}),null);
});
test('missing target and ambiguous timeout fields are rejected', () => {
  const e=evidence(); delete e.expected.runId; assert.equal(state(e),'invalid_target');
  e.expected.runId='run-a'; delete e.wait.timedOut; assert.equal(state(e),'invalid_evidence');
});
test('CLI accepts stdin evidence and rejects invalid JSON without a decision', () => {
  const cli=new URL('./completion-guard.mjs',import.meta.url);
  const ok=spawnSync(process.execPath,[cli.pathname],{input:JSON.stringify(evidence()),encoding:'utf8'});
  assert.equal(ok.status,0); assert.equal(JSON.parse(ok.stdout).state,'ready_for_review');
  const bad=spawnSync(process.execPath,[cli.pathname],{input:'not json',encoding:'utf8'});
  assert.equal(bad.status,2); assert.equal(bad.stdout,'');
});

test('incomplete activity metadata cannot look like an idle verified result', () => {
  for (const alter of [e=>delete e.read.thread.activeRunId,e=>delete e.read.thread.pendingRequestCount,e=>e.read.recentRuns={},e=>e.read.items=null]) {
    const e=evidence(); alter(e); assert.equal(state(e),'invalid_evidence');
  }
});

for (const status of ['failed','cancelled','interrupted','rolled_back']) {
 test(`unverified ${status} result cannot declare another run unsuccessful`,()=>{
  const e=evidence();e.wait.resultRun={runId:'unrelated-run',status};assert.equal(state(e),'result_run_changed');
  e.expected.verifiedResultRunIds=['unrelated-run'];assert.equal(state(e),'run_ended_unsuccessfully');
 });
}
