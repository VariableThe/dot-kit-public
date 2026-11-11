/** Execute one authorized mutation and immediately await its exact run. No credentials. */
import { payload } from './completion-core.mjs';
import { monitorCompletion } from './completion-monitor.mjs';

const METHODS = new Map([['launch','t3_thread_launch'], ['send','t3_thread_send']]);
const MONITOR_LIMITS = Object.freeze({timeoutMs:[1,60000], maxWaits:[1,3600], maxReadAttempts:[1,5], maxPages:[1,100]});
const safeCode = error => typeof error?.code === 'string' && /^[A-Za-z0-9_:-]{1,80}$/.test(error.code) ? error.code : 'operation_error';
const nonempty = x => typeof x === 'string' && x.trim().length > 0;
const batchReplyError = code => Object.assign(new Error('Incomplete journal batch reply'),{code});
const validateMonitorOptions = options => {
  if (!options || typeof options !== 'object' || Array.isArray(options))
    throw new TypeError('Monitor options must be an object');
  for (const [key,[low,high]] of Object.entries(MONITOR_LIMITS))
    if (options[key] !== undefined && (!Number.isInteger(options[key]) || options[key] < low || options[key] > high))
      throw new TypeError(`Invalid ${key}`);
};

/** Verify the exact fenced record before treating a release as confirmed. */
const releaseConfirmed = (record, fence, expected) => {
  if (!record || record.operationId!==fence.operationId || record.owner!==fence.owner ||
      record.generation!==fence.generation || record.environmentId!==expected.environmentId ||
      record.leaseUntil!==0) return false;
  if (expected.threadId!==undefined && record.threadId!==expected.threadId) return false;
  if (expected.runId!==undefined && record.runId!==expected.runId) return false;
  if (expected.resultRunId!==undefined && record.resultRunId!==expected.resultRunId) return false;
  if (expected.state!==undefined && record.state!==expected.state) return false;
  return true;
};

/** Reject any reply that does not prove every requested step committed. */
const validateBatchReply = (batch, steps, fence, verify) => {
  if (!batch || batch.ok!==true || batch.completed!==steps.length ||
      !Array.isArray(batch.results) || batch.results.length!==steps.length)
    throw batchReplyError('batch_invalid_response');
  const records=steps.map((step,index)=>{
    if (!['checkpoint','release'].includes(step.action)) throw batchReplyError('batch_invalid_response');
    const record=batch.results[index]?.record;
    if (!record) throw batchReplyError('batch_invalid_record');
    if (record.operationId!==fence.operationId || record.owner!==fence.owner || record.generation!==fence.generation)
      throw batchReplyError('batch_record_mismatch');
    if (step.action==='checkpoint' && record.state!==step.fields?.state)
      throw batchReplyError('batch_record_mismatch');
    if (step.action==='checkpoint')
      for (const [key,value] of Object.entries(step.fields ?? {}))
        if (record[key]!==value) throw batchReplyError('batch_record_mismatch');
    if (step.action==='release' && record.leaseUntil!==0)
      throw batchReplyError('batch_record_mismatch');
    return record;
  });
  if (verify) verify(records);
  return records;
};

/**
 * journal(action, fields) calls the durable, fenced local journal, not store().
 * Only this entry point may issue an authorized T3 launch or send in this workflow.
 * It never retries a mutation and never reports user delivery automatically.
 */
export async function runAndMonitor({
  operationId, owner, method, args, context, journal, callTool, journalBatch,
  adoptRunId, signal, continuation, monitorOptions = {}, now = Date.now, instrument,
}) {
  if (![operationId,owner].every(nonempty) || !['launch','send','resume'].includes(method))
    throw new TypeError('Operation ID, native owner and launch/send/resume method are required');
  if (typeof journal !== 'function' || typeof callTool !== 'function')
    throw new TypeError('Durable journal and existing native connector callbacks are required');
  if (journalBatch!==undefined && typeof journalBatch!=='function')
    throw new TypeError('A journal batch callback must be a function');
  if (instrument!==undefined && typeof instrument!=='function')
    throw new TypeError('An instrumentation callback must be a function');
  const emit = event => { if (instrument) { try { instrument(event); } catch { /* instrumentation never affects delivery */ } } };
  if (method !== 'resume') {
    if (!args || !nonempty(args.environmentId) || !nonempty(args.clientRequestId) || !nonempty(args.message))
      throw new TypeError('Verified environment, stable clientRequestId and authorized message are required');
    if (method === 'send' && !nonempty(args.threadId)) throw new TypeError('A verified send target is required');
    if (!context || !nonempty(context.parentTask) || !nonempty(context.acceptanceCriteria))
      throw new TypeError('Parent task and acceptance criteria are required');
  }
  if (method !== 'resume') { args=JSON.parse(JSON.stringify(args)); context=JSON.parse(JSON.stringify(context)); }
  validateMonitorOptions(monitorOptions);
  if (continuation && (method!=='resume' || !Array.isArray(continuation.verifiedResultRunIds) ||
      !continuation.verifiedResultRunIds.every(nonempty) || !nonempty(continuation.evidenceRef)))
    throw new TypeError('Continuation requires resume, verified IDs and an authoritative evidence reference');
  if (adoptRunId!==undefined && (method!=='resume' || !nonempty(adoptRunId) ||
      !continuation?.verifiedResultRunIds?.includes(adoptRunId)))
    throw new TypeError('Adoption requires resume, one verified successor ID and continuation evidence');
  let claim;
  try { claim = await journal('claim',{operationId,owner,method,...method==='resume'?{}:{args,context}}); }
  catch (error) { return {state:'watch_not_started',errorCode:safeCode(error),operationId,taskComplete:false}; }
  if (claim?.acquired !== true || !Number.isInteger(claim.generation) || !claim.record)
    return {state:'watch_not_started',errorCode:claim?.error?.code ?? 'journal_claim_failed',operationId,taskComplete:false};
  const fence = {operationId,owner,generation:claim.generation};
  let record=claim.record;
  let accepted = record.threadId && record.runId ? {threadId:record.threadId,runId:record.runId,projectId:record.projectId,link:record.link} : null;
  let dispatched=false;
  let batchWatching=false;
  let leaseFinalized=false;
  let lastOutcome=null;
  const checkpoint = async fields => {
    const response=await journal('checkpoint',{...fence,fields});
    if (!response?.record) throw Object.assign(new Error('Durable checkpoint failed'),{code:response?.error?.code ?? 'checkpoint_failed'});
    record=response.record;
    return record;
  };
  const finish = async result => {
    lastOutcome={...result,operationId,owner,generation:claim.generation,accepted,taskComplete:false};
    return lastOutcome;
  };
  try {
    if (['delivered','delivery_pending','delivery_uncertain'].includes(record.state))
      return finish({state:record.state,next:'Reconcile parent delivery; do not resend or relaunch.'});
    if (signal?.aborted) {
      await checkpoint({state:'watch_stopped'});
      return finish({state:'watch_stopped'});
    }
    if (!accepted) {
      if (method==='resume' || record.dispatchAttempted===true) {
        await checkpoint({state:'dispatch_uncertain'});
        return finish({state:'dispatch_uncertain',next:'Reconcile this operation using its saved request ID. No automatic mutation retry.'});
      }
      // Save intent before invoking the mutation. Crash in/after the call is uncertain.
      await checkpoint({state:'dispatching',dispatchAttempted:true,startedAt:new Date(now()).toISOString()});
      dispatched=true;
      let raw;
      emit({event:'dispatch_start',method,at:now()});
      try { raw=await callTool(METHODS.get(method),args); }
      catch (error) {
        emit({event:'dispatch_end',method,at:now()});
        await checkpoint({state:'dispatch_uncertain',errorCode:safeCode(error)});
        return finish({state:'dispatch_uncertain',errorCode:safeCode(error),next:'Inspect the original operation before retrying; retain clientRequestId.'});
      }
      emit({event:'dispatch_end',method,at:now()});
      const response=payload(raw);
      if (!response || !nonempty(response.threadId) || !nonempty(response.runId) ||
          (method==='send' && response.threadId!==args.threadId)) {
        await checkpoint({state:'dispatch_uncertain',errorCode:'invalid_dispatch_response'});
        return finish({state:'dispatch_uncertain',errorCode:'invalid_dispatch_response',next:'Do not retry an unconfirmed mutation. Reconcile the exact request.'});
      }
      accepted=Object.fromEntries(['threadId','runId','projectId','link','messageId','taskId'].filter(k=>nonempty(response[k])).map(k=>[k,response[k]]));
      const acceptedFields={...Object.fromEntries(Object.entries(accepted).filter(([key])=>key!=='link')),startPosition:Number.isInteger(context.startPosition)?context.startPosition:-1};
      if (journalBatch) {
        // Adjacent metadata only, immediately after native acceptance. No native
        // effect lies between these two steps. A partial/lost reply is uncertain.
        try {
          const watchingAt=new Date(now()).toISOString();
          const startPosition=acceptedFields.startPosition;
          const acceptedSteps=[
            {action:'checkpoint',fields:{state:'accepted',...acceptedFields}},
            {action:'checkpoint',fields:{state:'watching',threadId:accepted.threadId,runId:accepted.runId,
              startedAt:watchingAt,observedAt:watchingAt,startPosition,lastReadPosition:startPosition,
              waits:0,taskComplete:false}},
          ];
          const batch=await journalBatch(acceptedSteps,fence);
          const records=validateBatchReply(batch,acceptedSteps,fence,([acceptedRecord,watchingRecord])=>{
            if (acceptedRecord.environmentId!==record.environmentId ||
                acceptedRecord.threadId!==accepted.threadId || acceptedRecord.runId!==accepted.runId ||
                acceptedRecord.startPosition!==startPosition ||
                watchingRecord.environmentId!==record.environmentId ||
                watchingRecord.threadId!==accepted.threadId || watchingRecord.runId!==accepted.runId ||
                watchingRecord.startPosition!==startPosition || watchingRecord.lastReadPosition!==startPosition ||
                watchingRecord.waits!==0 || watchingRecord.taskComplete!==false)
              throw batchReplyError('batch_record_mismatch');
          });
          record=records.at(-1);
          batchWatching=true;
        } catch (error) {
          return finish({state:'accepted_watch_blocked',errorCode:safeCode(error),next:'Native acceptance is retained. Reconcile the journal record read-only; do not redeliver and do not retry the batch.'});
        }
      } else {
        await checkpoint({state:'accepted',...acceptedFields});
      }
    }
    if (continuation) await checkpoint({verifiedResultRunIds:continuation.verifiedResultRunIds,continuationEvidenceRef:continuation.evidenceRef});
    if (adoptRunId!==undefined) {
      // Explicit, evidence-backed adoption of an already-observed successor run.
      // The original run stays in originRunId/lineageRunIds; no native mutation occurs.
      if (!accepted) throw Object.assign(new Error('No accepted run to adopt'),{code:'not_accepted'});
      const expectedOrigin=record.originRunId ?? record.runId;
      const priorLineage=Array.isArray(record.lineageRunIds)?[...record.lineageRunIds]:[];
      const adopted=await journal('adopt',{...fence,runId:adoptRunId,evidenceRef:continuation.evidenceRef});
      const adoptedRecord=adopted?.record;
      const invalidAdoption=()=>Object.assign(new Error('Malformed adoption reply'),{code:'adopt_invalid_response'});
      if (!adoptedRecord) throw Object.assign(new Error('Adoption was not persisted'),{code:'adopt_failed'});
      if (adoptedRecord.operationId!==fence.operationId || adoptedRecord.owner!==fence.owner ||
          adoptedRecord.generation!==fence.generation || adoptedRecord.environmentId!==record.environmentId || adoptedRecord.threadId!==accepted.threadId ||
          adoptedRecord.runId!==adoptRunId || adoptedRecord.originRunId!==expectedOrigin ||
          adoptedRecord.state!=='watching' || adoptedRecord.resultRunId!=null ||
          adoptedRecord.taskComplete!==false || adoptedRecord.continuationEvidenceRef!==continuation.evidenceRef ||
          !Array.isArray(adoptedRecord.verifiedResultRunIds) ||
          !adoptedRecord.verifiedResultRunIds.includes(adoptRunId) ||
          !Array.isArray(adoptedRecord.lineageRunIds) ||
          !adoptedRecord.lineageRunIds.includes(expectedOrigin) ||
          !adoptedRecord.lineageRunIds.includes(adoptRunId) ||
          priorLineage.length>adoptedRecord.lineageRunIds.length ||
          priorLineage.some((runId,index)=>adoptedRecord.lineageRunIds[index]!==runId))
        throw invalidAdoption();
      record=adoptedRecord;
      accepted={...accepted,runId:record.runId,originRunId:record.originRunId,lineageRunIds:record.lineageRunIds};
    }
    const target={verifiedResultRunIds:record.verifiedResultRunIds ?? [],environmentId:record.environmentId,threadId:accepted.threadId,runId:accepted.runId,
      afterPosition:Number.isInteger(record.startPosition)?record.startPosition:-1};
    // Callers cannot override target, persistence, connector or handling through options.
    const allowedOptions=Object.fromEntries(['timeoutMs','maxWaits','maxReadAttempts','maxPages'].filter(k=>monitorOptions[k]!==undefined).map(k=>[k,monitorOptions[k]]));
    const result=await monitorCompletion({
      ...allowedOptions,target,signal,now,
      handledResultRunIds:record.handledResultRunIds ?? [],
      skipInitialWatching:batchWatching,
      checkpoint:fields=>checkpoint(Object.fromEntries(Object.entries(fields).filter(([key])=>key!=='environmentId'))),
      callTool:async(name,readArgs)=>{
        // Fence before every wait/read, including result retrieval. Never use a stale lease.
        await checkpoint({state:'watching',observedAt:new Date(now()).toISOString()});
        emit({event:'call_start',name,at:now()});
        try { return await callTool(name,readArgs); }
        finally { emit({event:'call_end',name,at:now()}); }
      },
    });
    if (result.state==='ready_for_review') {
      if (journalBatch) {
        // Adjacent terminal metadata: validated result, review handoff, lease close.
        // The batch includes release, so never attempt a second uncertain release.
        leaseFinalized=true;
        try {
          const terminalSteps=[
            {action:'checkpoint',fields:{state:'review_pending',resultRunId:result.resultRunId}},
            {action:'release'},
          ];
          const batch=await journalBatch(terminalSteps,fence);
          validateBatchReply(batch,terminalSteps,fence,([reviewRecord,releasedRecord])=>{
            if (reviewRecord.environmentId!==record.environmentId ||
                reviewRecord.threadId!==accepted.threadId || reviewRecord.runId!==accepted.runId ||
                reviewRecord.resultRunId!==result.resultRunId ||
                releasedRecord.environmentId!==record.environmentId ||
                releasedRecord.threadId!==accepted.threadId || releasedRecord.runId!==accepted.runId ||
                releasedRecord.resultRunId!==result.resultRunId || releasedRecord.state!=='review_pending')
              throw batchReplyError('batch_record_mismatch');
          });
        } catch (error) {
          return finish({state:'watch_blocked',errorCode:safeCode(error),resultRunId:result.resultRunId,releaseState:'batch_uncertain',next:'Terminal journal batch is uncertain or partial. Reconcile the record with a read-only read before delivery; do not redeliver and do not retry the batch.'});
        }
      } else {
        await checkpoint({state:'review_pending',resultRunId:result.resultRunId});
      }
    }
    return finish(result);
  } catch (error) {
    // An accepted mutation is retained even when its checkpoint fails. Never hide it.
    return finish({state:accepted?'accepted_watch_blocked':dispatched?'dispatch_uncertain':'watch_blocked',
      errorCode:safeCode(error),next:'Preserve this operation and recover the same run. Do not relaunch.'});
  } finally {
    if (!leaseFinalized) {
      try {
        const released=await journal('release',fence);
        const expected={environmentId:record.environmentId,threadId:accepted?.threadId ?? record.threadId,
          runId:accepted?.runId ?? record.runId,resultRunId:lastOutcome?.resultRunId ?? record.resultRunId,
          state:lastOutcome?.state==='ready_for_review'?'review_pending':undefined};
        if (!releaseConfirmed(released?.record,fence,expected))
          throw Object.assign(new Error('Lease release was not confirmed'),{code:'release_unconfirmed'});
      } catch (error) {
        // A lost reply may still have committed the release, so this is uncertain,
        // not a reason to retry. Never let a ready handoff hide an unconfirmed lease.
        if (lastOutcome) {
          lastOutcome.releaseState='release_uncertain';
          lastOutcome.releaseErrorCode=safeCode(error);
          if (lastOutcome.state==='ready_for_review') {
            lastOutcome.state='watch_blocked';
            lastOutcome.errorCode='release_uncertain';
            lastOutcome.next='Final evidence remains at review_pending, but lease release was not confirmed. Reconcile the record read-only; do not redeliver, force a release, replay a batch, or retry dispatch.';
          }
        }
      }
    }
  }
}
