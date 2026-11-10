/** Execute one authorized mutation and immediately await its exact run. No credentials. */
import { payload } from './completion-core.mjs';
import { monitorCompletion } from './completion-monitor.mjs';

const METHODS = new Map([['launch','t3_thread_launch'], ['send','t3_thread_send']]);
const safeCode = error => typeof error?.code === 'string' && /^[A-Za-z0-9_:-]{1,80}$/.test(error.code) ? error.code : 'operation_error';
const nonempty = x => typeof x === 'string' && x.trim().length > 0;

/**
 * journal(action, fields) calls the durable, fenced local journal, not store().
 * Only this entry point may issue an authorized T3 launch or send in this workflow.
 * It never retries a mutation and never reports user delivery automatically.
 */
export async function runAndMonitor({
  operationId, owner, method, args, context, journal, callTool,
  signal, continuation, monitorOptions = {}, now = Date.now,
}) {
  if (![operationId,owner].every(nonempty) || !['launch','send','resume'].includes(method))
    throw new TypeError('Operation ID, native owner and launch/send/resume method are required');
  if (typeof journal !== 'function' || typeof callTool !== 'function')
    throw new TypeError('Durable journal and existing native connector callbacks are required');
  if (method !== 'resume') {
    if (!args || !nonempty(args.environmentId) || !nonempty(args.clientRequestId) || !nonempty(args.message))
      throw new TypeError('Verified environment, stable clientRequestId and authorized message are required');
    if (method === 'send' && !nonempty(args.threadId)) throw new TypeError('A verified send target is required');
    if (!context || !nonempty(context.parentTask) || !nonempty(context.acceptanceCriteria))
      throw new TypeError('Parent task and acceptance criteria are required');
  }
  if (method !== 'resume') { args=JSON.parse(JSON.stringify(args)); context=JSON.parse(JSON.stringify(context)); }
  if (continuation && (method!=='resume' || !Array.isArray(continuation.verifiedResultRunIds) ||
      !continuation.verifiedResultRunIds.every(nonempty) || !nonempty(continuation.evidenceRef)))
    throw new TypeError('Continuation requires resume, verified IDs and an authoritative evidence reference');
  let claim;
  try { claim = await journal('claim',{operationId,owner,method,...method==='resume'?{}:{args,context}}); }
  catch (error) { return {state:'watch_not_started',errorCode:safeCode(error),operationId,taskComplete:false}; }
  if (claim?.acquired !== true || !Number.isInteger(claim.generation) || !claim.record)
    return {state:'watch_not_started',errorCode:claim?.error?.code ?? 'journal_claim_failed',operationId,taskComplete:false};
  const fence = {operationId,owner,generation:claim.generation};
  let record=claim.record;
  let accepted = record.threadId && record.runId ? {threadId:record.threadId,runId:record.runId,projectId:record.projectId,link:record.link} : null;
  let dispatched=false;
  const checkpoint = async fields => {
    const response=await journal('checkpoint',{...fence,fields});
    if (!response?.record) throw Object.assign(new Error('Durable checkpoint failed'),{code:response?.error?.code ?? 'checkpoint_failed'});
    record=response.record;
    return record;
  };
  const finish = async result => ({...result,operationId,owner,generation:claim.generation,accepted,taskComplete:false});
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
      try { raw=await callTool(METHODS.get(method),args); }
      catch (error) {
        await checkpoint({state:'dispatch_uncertain',errorCode:safeCode(error)});
        return finish({state:'dispatch_uncertain',errorCode:safeCode(error),next:'Inspect the original operation before retrying; retain clientRequestId.'});
      }
      const response=payload(raw);
      if (!response || !nonempty(response.threadId) || !nonempty(response.runId) ||
          (method==='send' && response.threadId!==args.threadId)) {
        await checkpoint({state:'dispatch_uncertain',errorCode:'invalid_dispatch_response'});
        return finish({state:'dispatch_uncertain',errorCode:'invalid_dispatch_response',next:'Do not retry an unconfirmed mutation. Reconcile the exact request.'});
      }
      accepted=Object.fromEntries(['threadId','runId','projectId','link','messageId','taskId'].filter(k=>nonempty(response[k])).map(k=>[k,response[k]]));
      await checkpoint({state:'accepted',...Object.fromEntries(Object.entries(accepted).filter(([key])=>key!=='link')),startPosition:Number.isInteger(context.startPosition)?context.startPosition:-1});
    }
    if (continuation) await checkpoint({verifiedResultRunIds:continuation.verifiedResultRunIds,continuationEvidenceRef:continuation.evidenceRef});
    const target={verifiedResultRunIds:record.verifiedResultRunIds ?? [],environmentId:record.environmentId,threadId:accepted.threadId,runId:accepted.runId,
      afterPosition:Number.isInteger(record.startPosition)?record.startPosition:-1};
    // Callers cannot override target, persistence, connector or handling through options.
    const allowedOptions=Object.fromEntries(['timeoutMs','maxWaits','maxReadAttempts','maxPages'].filter(k=>monitorOptions[k]!==undefined).map(k=>[k,monitorOptions[k]]));
    const result=await monitorCompletion({
      ...allowedOptions,target,signal,now,
      handledResultRunIds:record.handledResultRunIds ?? [],
      checkpoint:fields=>checkpoint(Object.fromEntries(Object.entries(fields).filter(([key])=>key!=='environmentId'))),
      callTool:async(name,readArgs)=>{
        // Fence before every wait/read, including result retrieval. Never use a stale lease.
        await checkpoint({state:'watching',observedAt:new Date(now()).toISOString()});
        return callTool(name,readArgs);
      },
    });
    if (result.state==='ready_for_review') await checkpoint({state:'review_pending',resultRunId:result.resultRunId});
    return finish(result);
  } catch (error) {
    // An accepted mutation is retained even when its checkpoint fails. Never hide it.
    return finish({state:accepted?'accepted_watch_blocked':dispatched?'dispatch_uncertain':'watch_blocked',
      errorCode:safeCode(error),next:'Preserve this operation and recover the same run. Do not relaunch.'});
  } finally {
    try { await journal('release',fence); } catch { /* Lease expiry fences recovery; result remains unfinished. */ }
  }
}
