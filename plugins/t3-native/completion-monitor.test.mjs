import test from 'node:test';
import assert from 'node:assert/strict';
import { monitorCompletion } from './completion-monitor.mjs';

const target={environmentId:'test-environment',threadId:'test-thread',runId:'test-run'};
const wait=(timedOut=false)=>({threadId:target.threadId,runId:target.runId,status:'completed',timedOut,...timedOut?{}:{resultRun:{runId:target.runId,status:'completed'}}});
const read=()=>({thread:{threadId:target.threadId,latestRunId:target.runId,activeRunId:null,pendingRequestCount:0},recentRuns:[{runId:target.runId,status:'completed'}],items:[{itemId:'reply',runId:target.runId,type:'assistant_message',status:'completed',text:'DONE',textTruncated:false,nextTextOffset:null}],nextPosition:4,hasMore:false});
function harness(responses, options={}) {
 const calls=[],records=[],delays=[];
 const callTool=async(name,args)=>{calls.push({name,args}); const r=responses.shift(); if(r instanceof Error)throw r; if(typeof r==='function')return r(name,args); return r;};
 return {calls,records,delays,run:()=>monitorCompletion({target,callTool,checkpoint:async r=>records.push(r),sleep:async ms=>delays.push(ms),...options})};
}
test('terminal event is immediately read and returned for review',async()=>{
 const h=harness([wait(),read()]);const r=await h.run(); assert.equal(r.state,'ready_for_review'); assert.equal(r.taskComplete,false);
 assert.deepEqual(h.calls.map(x=>x.name),['t3_thread_wait','t3_thread_read']); assert.equal(h.calls[0].args.untilDelegatedWorkSettled,true); assert.equal(h.calls[0].args.runId,target.runId);
 assert.ok(h.records.every(x=>x.taskComplete===false && !('text' in x))); assert.equal(h.records.at(-1).state,'ready_for_review');
});
test('parent completed plus timeout renews wait rather than finishing',async()=>{
 const h=harness([wait(true),read(),wait(),read()]);const r=await h.run();assert.equal(r.waits,2);assert.equal(r.state,'ready_for_review');assert.equal(h.records[1].state,'waiting');
});
test('pending approval ends watcher as blocked, not done',async()=>{
 const r=read();r.thread.pendingRequestCount=1;const h=harness([wait(true),r]);assert.equal((await h.run()).state,'blocked');assert.equal(h.calls.length,2);
});
test('failed run returns its evidence without follow-up writes',async()=>{
 const w=wait();w.status='failed';const h=harness([w,read()]);assert.equal((await h.run()).state,'run_ended_unsuccessfully');assert.ok(h.calls.every(c=>['t3_thread_wait','t3_thread_read'].includes(c.name)));
});
test('transient reads retry with bounded backoff only',async()=>{
 const h=harness([Object.assign(new Error('private detail'),{code:'ETIMEDOUT'}),wait(),read()]);assert.equal((await h.run()).state,'ready_for_review');assert.deepEqual(h.delays,[1000]);assert.equal(JSON.stringify(h.records).includes('private detail'),false);
});
test('authentication denial is not retried or repaired',async()=>{
 const h=harness([{isError:true,structuredContent:{code:'auth_error'}}]);const r=await h.run();assert.equal(r.state,'watch_blocked');assert.equal(r.errorCode,'auth_error');assert.equal(h.calls.length,1);
});
test('repeated transient errors return unfinished ownership',async()=>{
 const e=()=>Object.assign(new Error('offline'),{code:'connection_error'});const h=harness([e(),e(),e()]);assert.equal((await h.run()).state,'watch_blocked');assert.equal(h.calls.length,3);assert.deepEqual(h.delays,[1000,2000]);
});
test('budget exhaustion remains incomplete',async()=>{
 const h=harness([wait(true),read()],{maxWaits:1});assert.equal((await h.run()).state,'watch_incomplete');
});
test('cancellation preserves unfinished record without writes',async()=>{
 const c=new AbortController();c.abort();const h=harness([],{signal:c.signal});assert.equal((await h.run()).state,'watch_stopped');assert.equal(h.calls.length,0);
});
test('another run is not silently adopted',async()=>{
 const w=wait();w.runId='unrelated-run';const h=harness([w,read()]);assert.equal((await h.run()).state,'run_changed');
});
test('pages and truncated final text are retrieved with exact offsets',async()=>{
 const first=read();first.items=[];first.hasMore=true;first.nextPosition=2;
 const second=read();second.items[0].text='DO';second.items[0].textTruncated=true;second.items[0].nextTextOffset=2;
 const part=read();part.items[0].text='NE';
 const h=harness([wait(),first,second,part]);const r=await h.run();assert.equal(r.state,'ready_for_review');assert.equal(r.read.items[0].text,'DONE');assert.equal(h.calls[3].args.textOffset,2);
});
test('pagination budget exhaustion is an explicit bounded limit',async()=>{
 const r=read();r.items=[];r.hasMore=true;r.nextPosition=2;
 const h=harness([wait(),r],{maxPages:1,target:{...target,afterPosition:0}});
 const result=await h.run();
 assert.equal(result.state,'watch_blocked');assert.equal(result.errorCode,'pagination_budget_exhausted');
 assert.equal(h.records.at(-1).lastReadPosition,2);assert.equal(h.records.at(-1).resultRunId,null);
});
test('a resumed pagination pass preserves the original boundary and re-reads it',async()=>{
 const first=read();first.items=[];first.hasMore=true;first.nextPosition=2;
 const limited=harness([wait(),first],{maxPages:1,target:{...target,afterPosition:0}});
 const blocked=await limited.run();assert.equal(blocked.state,'watch_blocked');assert.equal(blocked.errorCode,'pagination_budget_exhausted');
 assert.equal(limited.records.at(-1).lastReadPosition,2);

 const firstAgain=read();firstAgain.items=[];firstAgain.hasMore=true;firstAgain.nextPosition=2;
 const resumed=harness([wait(),firstAgain,read()],{maxPages:2,target:{...target,afterPosition:0}});
 const result=await resumed.run();assert.equal(result.state,'ready_for_review');
 assert.equal(resumed.calls[1].args.afterPosition,0,'resume keeps the immutable original boundary');
 assert.equal(resumed.calls[2].args.afterPosition,2);
});
test('already handled final run is deduplicated without a result read',async()=>{
 const h=harness([wait()],{handledResultRunIds:[target.runId]});
 const result=await h.run();assert.equal(result.state,'already_handled');assert.deepEqual(h.calls.map(c=>c.name),['t3_thread_wait']);
 assert.equal(h.records.at(-1).state,'already_handled');assert.equal(h.records.at(-1).resultRunId,target.runId);
});
test('handled dedup does not bypass changed-result, failed-result or lineage gates',async()=>{
 const changed=wait();changed.resultRun.runId='other';
 const changedRun=harness([changed],{handledResultRunIds:['other']});
 assert.equal((await changedRun.run()).state,'result_run_changed');
 assert.deepEqual(changedRun.calls.map(c=>c.name),['t3_thread_wait']);

 const failed=wait();failed.status='failed';
 const failedRun=harness([failed,read()],{handledResultRunIds:[target.runId]});
 assert.equal((await failedRun.run()).state,'run_ended_unsuccessfully');

 const verified=wait();verified.resultRun.runId='other';
 const verifiedRun=harness([verified],{handledResultRunIds:['other'],target:{...target,verifiedResultRunIds:['other']}});
 assert.equal((await verifiedRun.run()).state,'already_handled');
 assert.deepEqual(verifiedRun.calls.map(c=>c.name),['t3_thread_wait']);
});
test('checkpoint callback is required and failure cannot disappear',async()=>{
 await assert.rejects(()=>monitorCompletion({target,callTool:async()=>wait()}),/checkpoint/);
 await assert.rejects(()=>monitorCompletion({target,callTool:async()=>wait(),checkpoint:async()=>{throw Error('disk unavailable');}}),/disk unavailable/);
});

for (const [label,alter,expected] of [
 ['newer active run',r=>{r.thread.latestRunId='new-run';r.thread.activeRunId='new-run';r.recentRuns.push({runId:'new-run',status:'running'});},'newer_work_active'],
 ['pending request',r=>{r.thread.pendingRequestCount=1;},'blocked'],
 ['missing activity metadata',r=>{delete r.thread.activeRunId;},'invalid_evidence'],
 ['invalid run metadata',r=>{r.recentRuns={};},'invalid_evidence'],
]) {
 test(`text recovery revalidates ${label} from its latest snapshot`,async()=>{
  const first=read();first.items[0].text='DO';first.items[0].textTruncated=true;first.items[0].nextTextOffset=2;
  const part=read();part.items[0].text='NE';alter(part);
  const h=harness([wait(),first,part]);const result=await h.run();assert.equal(result.state,expected);assert.notEqual(result.state,'ready_for_review');
 });
}
test('text recovery target mismatch fails closed',async()=>{
 const first=read();first.items[0].text='DO';first.items[0].textTruncated=true;first.items[0].nextTextOffset=2;
 const part=read();part.thread.threadId='other-thread';
 const h=harness([wait(),first,part]);assert.equal((await h.run()).state,'watch_blocked');
});
test('abort while the final read is in flight overrides ready evidence',async()=>{
 const controller=new AbortController();
 const h=harness([wait(),()=>{controller.abort();return read();}],{signal:controller.signal});
 const result=await h.run();assert.equal(result.state,'watch_stopped');assert.equal(h.records.at(-1).state,'watch_stopped');
});
test('abort during the terminal checkpoint overrides ready handoff',async()=>{
 const controller=new AbortController();const records=[];
 const h=harness([wait(),read()],{signal:controller.signal,checkpoint:async record=>{records.push(record);if(record.state==='ready_for_review')controller.abort();}});
 const result=await h.run();assert.equal(result.state,'watch_stopped');assert.equal(records.at(-1).state,'watch_stopped');
});
for (const [name,alter,expected] of [
 ['wrong thread',w=>{w.threadId='other';},'target_mismatch'],
 ['wrong original run',w=>{w.runId='other';},'run_changed'],
 ['unverified continuation',w=>{w.resultRun.runId='other';},'result_run_changed'],
 ['unverified failed continuation',w=>{w.resultRun={runId:'other',status:'failed'};},'result_run_changed'],
]) test(`pre-read gate rejects ${name} without acknowledging a child result`,async()=>{
 const w=wait();alter(w);const h=harness([w]);const r=await h.run();assert.equal(r.state,expected);assert.deepEqual(h.calls.map(c=>c.name),['t3_thread_wait']);
});

test('fresh-run negative sentinel is never sent as a native API cursor',async()=>{
 const h=harness([wait(),read()],{target:{...target,afterPosition:-1}});assert.equal((await h.run()).state,'ready_for_review');assert.equal('afterPosition' in h.calls[1].args,false);
});
