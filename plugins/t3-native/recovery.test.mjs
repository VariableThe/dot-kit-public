import test from 'node:test';
import assert from 'node:assert/strict';
import { runAndMonitor } from './active-monitor.mjs';
const accepted={threadId:'thread',runId:'run1',projectId:'project'};
const baseArgs={environmentId:'env',clientRequestId:'stable-key',message:'TASK'};
const context={parentTask:'parent',acceptanceCriteria:'OK',startPosition:90};
const wait=(runId,extra={})=>({threadId:'thread',runId,status:'completed',timedOut:false,resultRun:{runId,status:'completed'},...extra});
const read=(runId,extra={})=>({thread:{threadId:'thread',latestRunId:runId,activeRunId:null,pendingRequestCount:0},recentRuns:[{runId,status:'completed'}],items:[{itemId:'reply',runId,type:'assistant_message',status:'completed',text:'RESULT',textTruncated:false,nextTextOffset:null}],nextPosition:156,hasMore:false,...extra});
function harness({prior={state:'watching',environmentId:'env',threadId:'thread',runId:'run1',dispatchAttempted:true,startPosition:90,generation:1,leaseUntil:1},responses=[wait('run6'),read('run6')],releaseError=false,adoptReply}={}){
 let record={operationId:'op',...prior};const calls=[];const events=[];let currentReleaseError=releaseError;
 const journal=async(action,data)=>{
  events.push(action==='checkpoint'?data.fields.state:action);
  if(action==='claim'){record={...record,owner:data.owner,generation:(record.generation??0)+1};return{acquired:true,generation:record.generation,record:{...record}};}
  if(action==='checkpoint'){Object.assign(record,data.fields);return{record:{...record}};}
  if(action==='adopt'){
   if(!record.verifiedResultRunIds?.includes(data.runId))throw Object.assign(Error('not verified'),{code:'successor_not_verified'});
   if(record.continuationEvidenceRef!==data.evidenceRef)throw Object.assign(Error('proof'),{code:'continuation_proof_required'});
   const origin=record.originRunId??record.runId;const lineage=[...(record.lineageRunIds??[]),origin,record.runId,data.runId].filter((v,i,a)=>a.indexOf(v)===i);
   record={...record,originRunId:origin,lineageRunIds:lineage,runId:data.runId,state:'watching',resultRunId:null,taskComplete:false,waits:0,lastReadPosition:record.startPosition??-1};
   return{record:adoptReply?adoptReply({...record}):{...record}};
  }
  if(action==='release'){if(currentReleaseError)throw Error('release failed');record.leaseUntil=0;return{record};}
  throw Error('unsupported '+action);
 };
 const callTool=async(name,args)=>{calls.push({name,args});const x=responses.shift();if(x instanceof Error)throw x;if(typeof x==='function')return x();return structuredClone(x);};
 const options={method:'resume',args:undefined,continuation:{verifiedResultRunIds:['run6'],evidenceRef:'parent-read:evidence-1'},adoptRunId:'run6'};
 return{calls,events,respond:(...items)=>responses.push(...items),setReleaseError:value=>{currentReleaseError=value;},get record(){return record;},run:(over={})=>runAndMonitor({operationId:'op',owner:'worker',args:baseArgs,context,journal,callTool,...options,...over})};
}
test('explicit evidence-backed adoption watches successor without launch and preserves lineage',async()=>{
 const h=harness();const r=await h.run();assert.equal(r.state,'ready_for_review');assert.deepEqual(h.calls.map(c=>c.name),['t3_thread_wait','t3_thread_read']);assert.equal(h.calls[0].args.runId,'run6');assert.equal(h.calls[1].args.afterPosition,90);assert.equal(h.record.runId,'run6');assert.equal(h.record.originRunId,'run1');assert.equal(r.accepted.originRunId,'run1');assert.equal(r.accepted.runId,'run6');assert.deepEqual(h.record.lineageRunIds,['run1','run6']);assert.equal(h.record.state,'review_pending');
});
test('a changed result run is not adopted without explicit verified continuation',async()=>{
 const h=harness({responses:[{...wait('run1'),resultRun:{runId:'run6',status:'completed'}}]});const r=await h.run({continuation:undefined,adoptRunId:undefined});assert.equal(r.state,'result_run_changed');assert.deepEqual(h.calls.map(c=>c.name),['t3_thread_wait']);assert.equal(h.record.runId,'run1');assert.equal(h.record.state,'result_run_changed');
});
test('adopted result is not ready while a newer run is active',async()=>{
 const h=harness({responses:[wait('run6'),read('run6',{thread:{threadId:'thread',latestRunId:'run7',activeRunId:'run7',pendingRequestCount:0},recentRuns:[{runId:'run6',status:'completed'},{runId:'run7',status:'running'}]})]});const r=await h.run();assert.equal(r.state,'newer_work_active');assert.equal(h.record.state,'newer_work_active');assert.deepEqual(h.calls.map(c=>c.name),['t3_thread_wait','t3_thread_read']);
});
test('adoption requires an explicit verified successor and evidence',async()=>{
 await assert.rejects(()=>harness().run({continuation:{verifiedResultRunIds:['other'],evidenceRef:'parent-read:evidence-1'}}),/Adoption/);
 await assert.rejects(()=>harness().run({continuation:undefined}),/Adoption/);
});
test('malformed adoption identity or lineage blocks before any T3 observation',async()=>{
 for (const corrupt of [
   rec=>({...rec,originRunId:'attacker'}),
   rec=>({...rec,lineageRunIds:rec.lineageRunIds.slice(0,1)}),
   rec=>({...rec,threadId:'other'}),
   rec=>({...rec,runId:'other'}),
   rec=>({...rec,state:'review_pending',resultRunId:'run6'}),
 ]) {
   const h=harness({adoptReply:corrupt});const r=await h.run();
   assert.equal(r.state,'accepted_watch_blocked');assert.equal(r.errorCode,'adopt_invalid_response');
   assert.equal(h.calls.length,0);assert.equal(h.record.state,'watching');
 }
});
test('instrumentation reports only phase, connector name and timestamp',async()=>{
 const events=[];const h=harness();await h.run({instrument:event=>events.push(event)});
 assert.deepEqual(events.map(event=>`${event.event}:${event.name??''}`),['call_start:t3_thread_wait','call_end:t3_thread_wait','call_start:t3_thread_read','call_end:t3_thread_read']);
 for (const event of events) assert.deepEqual(Object.keys(event).sort(),['at','event','name'].filter(key=>key in event).sort());
 await assert.rejects(()=>h.run({instrument:'not-a-function'}),/instrumentation/);
});
test('lost final persistence is explicit uncertainty and remains resumable without relaunch',async()=>{
 const h=harness({releaseError:true});const r=await h.run();
 assert.equal(r.state,'watch_blocked');assert.equal(r.errorCode,'release_uncertain');assert.equal(r.releaseState,'release_uncertain');
 assert.equal(h.record.state,'review_pending');assert.equal(h.record.leaseUntil,1);
 const before=h.calls.length;h.setReleaseError(false);h.respond(wait('run6'),read('run6'));const again=await h.run();
 assert.equal(again.state,'ready_for_review');assert.equal(h.calls.length,before+2);assert.equal(h.calls.filter(c=>c.name==='t3_thread_launch').length,0);assert.equal(h.record.state,'review_pending');
});
test('failed read before final persistence leaves a blocked resumable record without launch',async()=>{
 const h=harness({responses:[wait('run6'),Object.assign(Error('offline'),{code:'network_error'}),Object.assign(Error('offline'),{code:'network_error'}),Object.assign(Error('offline'),{code:'network_error'}),Object.assign(Error('offline'),{code:'network_error'})]});
 const r=await h.run();assert.equal(r.state,'watch_blocked');assert.equal(h.record.state,'watch_blocked');assert.equal(h.calls.filter(c=>c.name==='t3_thread_launch').length,0);
});
