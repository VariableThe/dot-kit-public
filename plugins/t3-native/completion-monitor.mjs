/** Caller-owned T3 wait/read loop. Child-result reads may acknowledge delivery. */
import { classifyCompletion, payload } from './completion-core.mjs';

const TRANSIENT = new Set(['timeout', 'connection_error', 'network_error', 'deadline_exceeded', 'ECONNRESET', 'ETIMEDOUT']);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const integer = (value, low, high, name) => {
  if (!Number.isInteger(value) || value < low || value > high) throw new TypeError(`Invalid ${name}`);
  return value;
};
function toolError(value) {
  if (value?.isError === true) {
    const nested = value.structuredContent;
    const code = nested?.error?.code ?? nested?.code ?? 'tool_error';
    return Object.assign(new Error('T3 read failed'), { code });
  }
  return null;
}

/**
 * callTool(name,args) must use the caller's existing authorized T3 connector.
 * checkpoint(record) must persist metadata under the caller's single-owner lease.
 * This module never launches/sends, reports to a user, or marks delivery handled.
 */
export async function monitorCompletion({
  target, callTool, checkpoint, signal, sleep = pause, now = Date.now,
  timeoutMs = 60000, maxWaits = 60, maxReadAttempts = 3, maxPages = 50,
  handledResultRunIds = [],
}) {
  if (!target || !['environmentId','threadId','runId'].every(k => typeof target[k] === 'string' && target[k]))
    throw new TypeError('Verified environment, thread and run IDs are required');
  if (typeof callTool !== 'function' || typeof checkpoint !== 'function')
    throw new TypeError('Existing connector and durable checkpoint callbacks are required');
  integer(timeoutMs, 1, 60000, 'timeoutMs'); integer(maxWaits, 1, 3600, 'maxWaits');
  integer(maxReadAttempts, 1, 5, 'maxReadAttempts'); integer(maxPages, 1, 100, 'maxPages');
  const expected = {...target, untilDelegatedWorkSettled:true};
  const identity = {environmentId:target.environmentId, threadId:target.threadId, runId:target.runId};
  const startedAt = new Date(now()).toISOString();
  let cursor = target.afterPosition ?? null;
  let waits = 0;
  const save = async (state, extra = {}) => checkpoint({...identity, startedAt, observedAt:new Date(now()).toISOString(), state, startPosition:target.afterPosition ?? null, lastReadPosition:cursor, waits, taskComplete:false, ...extra});
  const stopped = () => ({state:'watch_stopped',next:'Preserve the unfinished record and return ownership to the parent.'});
  const finish = async (decision, evidence = {}) => {
    let outcome = signal?.aborted ? stopped() : decision;
    await save(outcome.state, {resultRunId:outcome.resultRunId ?? null});
    // Cancellation during checkpoint persistence must win over a ready handoff.
    if (signal?.aborted && outcome.state !== 'watch_stopped') {
      outcome = stopped();
      await save(outcome.state, {resultRunId:null});
    }
    return {...outcome, ...evidence, taskComplete:false, waits, startedAt, observedAt:new Date(now()).toISOString()};
  };
  const call = async (name, args) => {
    for (let attempt=0; attempt<maxReadAttempts; attempt+=1) {
      if (signal?.aborted) throw Object.assign(new Error('Monitoring stopped'), {code:'aborted'});
      try {
        const response = await callTool(name, {environmentId:target.environmentId, ...args});
        if (signal?.aborted) throw Object.assign(new Error('Monitoring stopped'), {code:'aborted'});
        const error = toolError(response); if (error) throw error;
        const data = payload(response);
        if (!data) throw Object.assign(new Error('Invalid T3 read response'), {code:'invalid_response'});
        return data;
      } catch(error) {
        if (!TRANSIENT.has(error?.code) || attempt+1 === maxReadAttempts) throw error;
        await save('retrying_read', {errorCode:error.code, attempt:attempt+1});
        await sleep(Math.min(1000 * 2**attempt, 5000));
      }
    }
  };
  const readPage = afterPosition => call('t3_thread_read', {
    threadId:target.threadId, view:'messages', limit:50, maxCharsPerItem:5000, runLimit:10,
    ...(Number.isInteger(afterPosition) && afterPosition >= 0 ? {afterPosition} : {}),
  });
  const finalRead = async resultRunId => {
    const items = []; let position = target.afterPosition ?? null; let last;
    for (let page=0; page<maxPages; page+=1) {
      last = await readPage(position);
      if (last.thread?.threadId !== target.threadId || !Array.isArray(last.items))
        throw Object.assign(new Error('Invalid thread identity or items'), {code:'invalid_response'});
      items.push(...last.items);
      if (Number.isInteger(last.nextPosition)) cursor=last.nextPosition;
      if (last.hasMore === false) break;
      if (!Number.isInteger(last.nextPosition) || last.nextPosition === position)
        throw Object.assign(new Error('Pagination did not advance'), {code:'invalid_response'});
      position=last.nextPosition;
    }
    if (last?.hasMore === false) {
      const reply=items.findLast(item => item.runId===resultRunId && item.type==='assistant_message');
      if (reply?.textTruncated === true) {
        let offset=reply.nextTextOffset; let text=reply.text ?? ''; let done=false;
        for (let page=0; page<maxPages && Number.isInteger(offset); page+=1) {
          const part=await call('t3_thread_read',{threadId:target.threadId,itemId:reply.itemId,textOffset:offset,maxCharsPerItem:5000});
          if (part.thread?.threadId !== target.threadId)
            throw Object.assign(new Error('Text recovery changed the thread identity'), {code:'invalid_response'});
          // Offset reads are newer snapshots too. Never reuse stale idle metadata.
          last = {...last,thread:part.thread,recentRuns:part.recentRuns};
          const item=part.items?.find(item=>item.itemId===reply.itemId && item.runId===resultRunId);
          if (!item || typeof item.text !== 'string') break;
          text+=item.text;
          if (item.textTruncated===false && item.nextTextOffset==null) {done=true;break;}
          if (!Number.isInteger(item.nextTextOffset) || item.nextTextOffset<=offset) break;
          offset=item.nextTextOffset;
        }
        if (done) Object.assign(reply,{text,textTruncated:false,nextTextOffset:null});
      }
    }
    return {...last,items};
  };
  // Checkpoint failure must stop the watch, never silently discard durability.
  await save('watching');
  try {
    for (;waits<maxWaits;) {
      if (signal?.aborted) return finish({state:'watch_stopped',next:'Preserve the unfinished record and return ownership to the parent.'});
      waits+=1;
      const wait=await call('t3_thread_wait',{threadId:target.threadId,runId:target.runId,timeoutMs,untilDelegatedWorkSettled:true});
      const waitReceivedAt=new Date(now()).toISOString();
      // Reading a child can acknowledge delivery. Validate identity BEFORE reading.
      const beforeRead=classifyCompletion({expected,wait,handledResultRunIds});
      if (!['waiting','blocked','run_ended_unsuccessfully','needs_result_read'].includes(beforeRead.state))
        return finish(beforeRead,{wait,waitReceivedAt});
      // Read immediately in this execution, with a validated result identity.
      const read=wait.timedOut===true ? await readPage(cursor) : await finalRead(beforeRead.resultRunId ?? wait.runId);
      if (Number.isInteger(read.nextPosition)) cursor=read.nextPosition;
      const decision=classifyCompletion({expected,wait,read,handledResultRunIds});
      if (decision.state==='waiting') {await save('waiting');continue;}
      return finish(decision,{wait,read,waitReceivedAt});
    }
    return finish({state:'watch_incomplete',next:'The execution budget ended. Reattach a watcher to the same run or report a blocker; do not claim completion.'});
  } catch(error) {
    // No raw transport error text or arguments are persisted or returned.
    const code=typeof error?.code==='string' && /^[A-Za-z0-9_:-]{1,80}$/.test(error.code) ? error.code : 'read_or_checkpoint_error';
    return finish({state:code==='aborted'?'watch_stopped':'watch_blocked',errorCode:code,next:'Preserve the record. Diagnose the blocker and resume this exact run when permitted; do not resend or relaunch work.'});
  }
}
