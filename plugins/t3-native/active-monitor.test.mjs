import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { runAndMonitor } from './active-monitor.mjs';
const args={environmentId:'env',clientRequestId:'stable-key',scratch:true,title:'Test',message:'Authorized bounded test'};
const accepted={threadId:'thread',runId:'run',projectId:'project',status:'running',link:'https://example.test/thread'};
const wait={threadId:'thread',runId:'run',status:'completed',timedOut:false,resultRun:{runId:'run',status:'completed'}};
const read={thread:{threadId:'thread',latestRunId:'run',activeRunId:null,pendingRequestCount:0},recentRuns:[{runId:'run',status:'completed'}],items:[{itemId:'reply',runId:'run',type:'assistant_message',status:'completed',text:'OK',textTruncated:false,nextTextOffset:null}],nextPosition:4,hasMore:false};
function harness({responses=[accepted,wait,read],prior={},journalError,options={}}={}) {
 const events=[],calls=[],records=[]; let record={operationId:'op',environmentId:'env',state:'dispatch_pending',dispatchAttempted:false,...prior};
 const journal=async(action,data)=>{
  events.push(action==='checkpoint'?data.fields.state:action);
  if(journalError) await journalError(action,data);
  if(action==='claim') return {acquired:true,generation:1,record:structuredClone(record)};
  if(action==='checkpoint'){Object.assign(record,data.fields);records.push(structuredClone(record));return {record:structuredClone(record)};}
  if(action==='release')return {record};
 };
 const callTool=async(name,request)=>{events.push(name);calls.push({name,args:structuredClone(request)});const x=responses.shift();if(x instanceof Error)throw x;return typeof x==='function'?x(name,request):structuredClone(x);};
 return {events,calls,records,run:()=>runAndMonitor({operationId:'op',owner:'worker',method:'launch',args,context:{parentTask:'/root/test',acceptanceCriteria:'Exact OK'},journal,callTool,...options})};
}
test('authorized launch immediately enters actual wait/read before return',async()=>{
 const h=harness();const r=await h.run();assert.equal(r.state,'ready_for_review');assert.equal(r.taskComplete,false);assert.equal(r.accepted.link,accepted.link);
 assert.deepEqual(h.calls.map(c=>c.name),['t3_thread_launch','t3_thread_wait','t3_thread_read']);assert.equal(h.records.at(-1).state,'review_pending');
 assert.ok(h.events.indexOf('dispatching')<h.events.indexOf('t3_thread_launch'));assert.ok(h.events.indexOf('accepted')<h.events.indexOf('t3_thread_wait'));
 assert.equal(h.calls[1].args.runId,'run');assert.equal(h.calls[1].args.untilDelegatedWorkSettled,true);assert.equal(h.calls[2].args.afterPosition,undefined);
 assert.ok(h.records.every(x=>!('link' in x)&&!('message' in x)));
});
test('authorized send uses same immediate watch route',async()=>{
 const h=harness({options:{method:'send',args:{...args,threadId:'thread',mode:'queue'}}});assert.equal((await h.run()).state,'ready_for_review');assert.equal(h.calls[0].name,'t3_thread_send');
});
test('initial persistence fails before any mutation',async()=>{
 const h=harness({journalError:action=>{if(action==='claim')throw Error('disk unavailable');}});assert.equal((await h.run()).state,'watch_not_started');assert.equal(h.calls.length,0);
});
test('pre-dispatch checkpoint failure makes zero native calls',async()=>{
 const h=harness({journalError:(a,d)=>{if(a==='checkpoint'&&d.fields.state==='dispatching')throw Error('disk unavailable');}});assert.equal((await h.run()).state,'watch_blocked');assert.equal(h.calls.length,0);
});
test('lost response remains uncertain and never retries mutation',async()=>{
 const h=harness({responses:[Object.assign(Error('private error'),{code:'ETIMEDOUT'})]});assert.equal((await h.run()).state,'dispatch_uncertain');assert.equal(h.calls.length,1);assert.equal(h.calls[0].args.clientRequestId,'stable-key');assert.equal(h.records.at(-1).state,'dispatch_uncertain');assert.equal(JSON.stringify(h.records).includes('private error'),false);
});
test('crash after intent does not repeat dispatch on recovery',async()=>{
 const h=harness({prior:{dispatchAttempted:true},options:{method:'resume',args:undefined}});assert.equal((await h.run()).state,'dispatch_uncertain');assert.equal(h.calls.length,0);
});
test('accepted crash recovery monitors exact run without launch',async()=>{
 const h=harness({prior:{...accepted,environmentId:'env',startPosition:7,dispatchAttempted:true},responses:[wait,read],options:{method:'resume',args:undefined}});assert.equal((await h.run()).state,'ready_for_review');assert.deepEqual(h.calls.map(c=>c.name),['t3_thread_wait','t3_thread_read']);assert.equal(h.calls[1].args.afterPosition,7);
});
test('accepted IDs survive failed acceptance checkpoint in returned blocker',async()=>{
 const h=harness({journalError:(a,d)=>{if(a==='checkpoint'&&d.fields.state==='accepted')throw Error('disk unavailable');}});const r=await h.run();assert.equal(r.state,'accepted_watch_blocked');assert.equal(r.accepted.runId,'run');assert.equal(h.calls.length,1);
});
test('stale fence before wait prevents extra connector call',async()=>{
 let watching=0;const h=harness({journalError:(a,d)=>{if(a==='checkpoint'&&d.fields.state==='watching'&&++watching===2)throw Object.assign(Error('stale'),{code:'stale_owner'});}});assert.notEqual((await h.run()).state,'ready_for_review');assert.equal(h.calls.length,1);
});
test('an error envelope is uncertain, not permission to retry',async()=>{
 const h=harness({responses:[{isError:true,structuredContent:{code:'auth_error'}}]});assert.equal((await h.run()).state,'dispatch_uncertain');assert.equal(h.calls.length,1);
});
test('mismatched send target cannot be watched silently',async()=>{
 const h=harness({options:{method:'send',args:{...args,threadId:'other'}}});assert.equal((await h.run()).state,'dispatch_uncertain');assert.equal(h.calls.length,1);
});
test('idle creation with no run cannot claim monitoring',async()=>{
 const h=harness({responses:[{...accepted,runId:null}]});assert.equal((await h.run()).state,'dispatch_uncertain');assert.equal(h.calls.length,1);
});
test('cancel before dispatch performs no native operation',async()=>{
 const signal={aborted:true};const h=harness({options:{signal}});assert.equal((await h.run()).state,'watch_stopped');assert.equal(h.calls.length,0);
});
test('cancel during dispatch keeps accepted ID and does not relaunch',async()=>{
 const signal={aborted:false};const h=harness({responses:[()=>{signal.aborted=true;return accepted;}],options:{signal}});const r=await h.run();assert.equal(r.state,'watch_stopped');assert.equal(r.accepted.runId,'run');assert.equal(h.calls.length,1);
});
test('delivery uncertainty is not automatically retried or relabeled',async()=>{
 const h=harness({prior:{...accepted,state:'delivery_uncertain'},options:{method:'resume'}});assert.equal((await h.run()).state,'delivery_uncertain');assert.equal(h.calls.length,0);
});
test('monitor options cannot override exact target or connector',async()=>{
 const h=harness({options:{monitorOptions:{target:{runId:'bad'},callTool:()=>{throw Error('wrong');},checkpoint:()=>{throw Error('wrong');}}}});assert.equal((await h.run()).state,'ready_for_review');assert.equal(h.calls[1].args.runId,'run');
});
test('lease is released but final review remains undelivered',async()=>{
 const h=harness();await h.run();assert.equal(h.events.at(-1),'release');assert.equal(h.records.at(-1).state,'review_pending');assert.ok(h.records.every(r=>r.state!=='delivered'));
});
test('generated isolate bundle exposes same executable workflow without Node',async()=>{
 const source=await readFile(new URL('./active-runtime.js',import.meta.url),'utf8');
 const runtime=vm.runInNewContext(source,{setTimeout});assert.equal(typeof runtime.runAndMonitor,'function');
 const trace=[];let record={environmentId:'env'};
 const result=await runtime.runAndMonitor({operationId:'bundle-op',owner:'worker',method:'launch',args,context:{parentTask:'task-1',acceptanceCriteria:'OK'},journal:async(a,d)=>{trace.push(a);if(a==='claim')return {acquired:true,generation:1,record};if(a==='checkpoint'){record={...record,...d.fields};return {record};}return {};},callTool:async name=>{trace.push(name);return name==='t3_thread_launch'?accepted:name==='t3_thread_wait'?wait:read;}});
 assert.equal(result.state,'ready_for_review');assert.ok(trace.includes('t3_thread_wait'));assert.equal(record.state,'review_pending');
});
test('verified continuation is persisted only on explicit evidence-backed resume',async()=>{
 const w={...wait,resultRun:{runId:'continuation',status:'completed'}};
 const rd={...read,thread:{...read.thread,latestRunId:'continuation'},recentRuns:[{runId:'continuation',status:'completed'}],items:[{...read.items[0],runId:'continuation'}]};
 const h=harness({prior:{...accepted,environmentId:'env',dispatchAttempted:true},responses:[w,rd],options:{method:'resume',continuation:{verifiedResultRunIds:['continuation'],evidenceRef:'tool-result:lineage-1'}}});
 assert.equal((await h.run()).state,'ready_for_review');assert.equal(h.records.at(-1).continuationEvidenceRef,'tool-result:lineage-1');assert.equal(h.records.at(-1).resultRunId,'continuation');
});
test('continuation cannot be inferred on fresh launch or without evidence',async()=>{
 await assert.rejects(()=>harness({options:{continuation:{verifiedResultRunIds:['other'],evidenceRef:'proof'}}}).run(),/Continuation/);
 await assert.rejects(()=>harness({options:{method:'resume',continuation:{verifiedResultRunIds:['other']}}}).run(),/Continuation/);
});
