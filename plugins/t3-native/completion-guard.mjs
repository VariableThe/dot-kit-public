#!/usr/bin/env node
/** Pure completion evidence classifier. No network, writes, or notifications. */
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const ACTIVE = new Set(['preparing', 'queued', 'starting', 'running', 'waiting']);
const FAILED = new Set(['failed', 'cancelled', 'interrupted', 'rolled_back']);
const answer = (state, next, resultRunId = null) => ({ state, next, resultRunId, taskComplete: false });
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);

// Accept decoded payloads and current MCP structuredContent/text envelopes.
export function payload(value) {
  for (let depth = 0; depth < 6; depth += 1) {
    if (!isObject(value) || value.isError === true) return null;
    if (isObject(value.structuredContent)) { value = value.structuredContent; continue; }
    if (Array.isArray(value.content)) {
      const text = value.content.find(item => item.type === 'text' && typeof item.text === 'string');
      if (text) { try { value = JSON.parse(text.text); continue; } catch { return null; } }
    }
    return value;
  }
  return null;
}

export function classifyCompletion(input) {
  const expected = input?.expected;
  if (!expected || !['environmentId', 'threadId', 'runId'].every(key => typeof expected[key] === 'string' && expected[key].length > 0))
    return answer('invalid_target', 'Restore the verified environment, thread and original run IDs.');
  const wait = payload(input.wait);
  if (!wait) return answer('read_error', 'Preserve the watch record. Diagnose the read error; do not resend work or claim completion.');
  if (wait.threadId !== expected.threadId)
    return answer('target_mismatch', 'Reconcile the target before any further action.');
  if (wait.runId === null || wait.status === 'idle')
    return answer('no_run', 'Inspect the exact thread. Idle is not successful completion.');
  if (wait.runId !== expected.runId)
    return answer('run_changed', 'Verify restart or continuation lineage, then update the watched run explicitly.');
  const read = input.read === undefined ? null : payload(input.read);
  if (input.read !== undefined && !read)
    return answer('read_error', 'Recover the thread read before classifying its result.');
  if (read && read.thread?.threadId !== expected.threadId)
    return answer('target_mismatch', 'Reconcile the thread read before any further action.');
  if (read && (read.thread.activeRunId !== null && typeof read.thread.activeRunId !== 'string' ||
      !Number.isInteger(read.thread.pendingRequestCount) || read.thread.pendingRequestCount < 0 ||
      !Array.isArray(read.recentRuns) || !Array.isArray(read.items)))
    return answer('invalid_evidence', 'Recover complete thread activity, pending-request and run metadata.');
  if (typeof wait.timedOut !== 'boolean')
    return answer('invalid_evidence', 'Get a valid wait response; do not infer completion from a missing timeout field.');
  if (read?.thread?.pendingRequestCount > 0 || wait.status === 'waiting')
    return answer('blocked', 'Read the pending question or approval and report it. Do not approve permissions automatically.');
  if (wait.timedOut) {
    if (wait.next && expected.callerOwnsDelegatedTask === true)
      return answer('await_native_wake', 'Follow the owning T3 parent wake contract. Keep the durable record pending.');
    if (wait.next)
      return answer('unexpected_wake', 'Verify caller ownership. An outside caller must not abandon its watcher based on this instruction.');
    return answer('waiting', 'Renew the bounded event wait. A timed-out completed turn can still have delegated work.');
  }
  if (FAILED.has(wait.status))
    return answer('run_ended_unsuccessfully', 'Read the failure and report the blocker or take only an already-authorized recovery step.', wait.runId);
  if (wait.status !== 'completed')
    return answer('invalid_evidence', 'Reconcile a nonterminal, non-timeout response. Do not mark work done.');
  if (expected.untilDelegatedWorkSettled !== true || !isObject(wait.resultRun))
    return answer('needs_settled_wait', 'Wait again with untilDelegatedWorkSettled:true. A completed turn alone is insufficient.');
  const result = wait.resultRun;
  if (typeof result.runId !== 'string' || result.runId.length === 0)
    return answer('invalid_evidence', 'Recover the actual final result run ID.');
  if (result.runId !== expected.runId && !(expected.verifiedResultRunIds ?? []).includes(result.runId))
    return answer('result_run_changed', 'Verify this is the delegated-work continuation, not unrelated newer work, before adding it to verifiedResultRunIds.', result.runId);
  if (FAILED.has(result.status))
    return answer('run_ended_unsuccessfully', 'Read and report the terminal result; the task is not successful.', result.runId);
  if (result.status !== 'completed')
    return answer('invalid_evidence', 'Wait until the final result run has ended.');
  if (!read) return answer('needs_result_read', 'Read the final result run and required artifacts.', result.runId);
  if (read.thread.activeRunId || (read.recentRuns ?? []).some(run => ACTIVE.has(run.status)))
    return answer('newer_work_active', 'Reconcile current work without steering or interrupting it. Do not use an old result as the new outcome.', result.runId);
  if (read.thread.latestRunId !== result.runId)
    return answer('newer_result', 'Reconcile the newer run before reporting or following up.', result.runId);
  const run = (read.recentRuns ?? []).find(run => run.runId === result.runId);
  if (!run || run.status !== 'completed')
    return answer('needs_result_read', 'Read enough run metadata to verify the returned final result run.', result.runId);
  if (read.hasMore !== false)
    return answer('needs_result_read', 'Continue item pagination with nextPosition before deciding the final result is available.', result.runId);
  const replies = (read.items ?? []).filter(item => item.runId === result.runId && item.type === 'assistant_message');
  const reply = replies.at(-1);
  if (!reply || reply.status !== 'completed' || typeof reply.text !== 'string' || !reply.text.trim())
    return answer('needs_result_read', 'Retrieve the completed assistant reply for the exact final result run.', result.runId);
  if (reply.textTruncated !== false || reply.nextTextOffset != null)
    return answer('needs_result_read', 'Recover the remaining text by itemId and textOffset.', result.runId);
  if ((input.handledResultRunIds ?? []).includes(result.runId))
    return answer('already_handled', 'Do not duplicate the result or send a duplicate follow-up.', result.runId);
  return answer('ready_for_review', 'Verify acceptance criteria and artifacts, report useful findings, then record delivery or the next authorized step.', result.runId);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { process.stdout.write(JSON.stringify(classifyCompletion(JSON.parse(readFileSync(0, 'utf8'))), null, 2) + '\n'); }
  catch { process.stderr.write('Invalid JSON input. No completion decision made.\n'); process.exitCode = 2; }
}
