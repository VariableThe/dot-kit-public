import test from 'node:test';
import assert from 'node:assert/strict';
import { runAndMonitor } from './active-monitor.mjs';
import { createToolBridge } from './exec-bridge.mjs';

const names = ['t3_thread_launch', 't3_thread_send', 't3_thread_wait', 't3_thread_read'];

function inMemoryJournal() {
  let record = { state: 'dispatch_pending', dispatchAttempted: false, environmentId: 'env-1', operationId: 'op-batch-1', owner: 'worker', generation: 0, leaseUntil: 0 };
  const calls = [];
  const apply = (fields, fence) => { record = { ...record, ...fields, ...(fence ? { operationId: fence.operationId, owner: fence.owner, generation: fence.generation } : {}) }; return { record: { ...record } }; };
  const journal = async (action, fields = {}) => {
    calls.push({ action, fields });
    if (action === 'claim') {
      record = { ...record, operationId: fields.operationId, owner: fields.owner, generation: 1, leaseUntil: 1, state: 'dispatch_pending', dispatchAttempted: false };
      return { acquired: true, generation: 1, record: { ...record } };
    }
    if (action === 'checkpoint') return apply(fields.fields, { operationId: fields.operationId, owner: fields.owner, generation: fields.generation });
    if (action === 'release') { record = { ...record, leaseUntil: 0 }; return { record: { ...record } }; }
    throw new Error('unsupported journal action');
  };
  const batch = async (steps, fence) => {
    calls.push({ action: 'batch', steps, fence });
    const results = [];
    for (const step of steps) {
      if (step.action === 'checkpoint') results.push(apply(step.fields, fence));
      else if (step.action === 'release') { record = { ...record, leaseUntil: 0 }; results.push({ record: { ...record } }); }
      else throw Object.assign(new Error('unsupported batch step'), { code: 'unsupported_batch_step' });
    }
    return { ok: true, completed: results.length, results };
  };
  return { calls, journal, batch, get record() { return record; } };
}

function happyTools() {
  const calls = [];
  const toolMap = {
    t3_thread_launch: async args => { calls.push('launch'); return { threadId: 'thread-1', runId: 'run-1', projectId: 'proj-1' }; },
    t3_thread_send: async args => { calls.push('send'); return { threadId: 'thread-1', runId: 'run-1' }; },
    t3_thread_wait: async args => {
      calls.push('wait');
      return { threadId: 'thread-1', runId: 'run-1', status: 'completed', timedOut: false, resultRun: { runId: 'run-1', status: 'completed' } };
    },
    t3_thread_read: async args => {
      calls.push('read');
      return {
        thread: { threadId: 'thread-1', activeRunId: null, pendingRequestCount: 0, latestRunId: 'run-1' },
        recentRuns: [{ runId: 'run-1', status: 'completed' }],
        items: [{ runId: 'run-1', type: 'assistant_message', status: 'completed', text: 'RESULT', textTruncated: false }],
        hasMore: false,
        nextPosition: 5,
      };
    },
  };
  return { calls, toolMap };
}

const base = {
  operationId: 'op-batch-1', owner: 'worker', method: 'launch',
  args: { environmentId: 'env-1', clientRequestId: 'op-batch-1', message: 'TASK' },
  context: { parentTask: 'parent', acceptanceCriteria: 'criteria', startPosition: -1 },
  monitorOptions: { timeoutMs: 1000, maxWaits: 3 },
};

const bridge = (store, tools) => ({ journal: store.journal, callTool: (name, args) => tools.toolMap[name](args) });
const flattened = calls => calls.flatMap(call => call.action === 'batch'
  ? call.steps.map(step => `${step.action}:${step.fields?.state ?? ''}`)
  : [`${call.action}:${call.fields?.fields?.state ?? ''}`]);

test('happy path keeps the same journal transition order and uses 7 batch commands vs 9 stable calls', async () => {
  const stable = inMemoryJournal(); const stableTools = happyTools();
  const stableResult = await runAndMonitor({ ...base, ...bridge(stable, stableTools) });
  assert.equal(stableResult.state, 'ready_for_review');
  assert.equal(stable.calls.length, 9);
  assert.deepEqual(stableTools.calls, ['launch', 'wait', 'read']);

  const batched = inMemoryJournal(); const batchedTools = happyTools();
  const batchedResult = await runAndMonitor({ ...base, ...bridge(batched, batchedTools), journalBatch: batched.batch });
  assert.equal(batchedResult.state, 'ready_for_review');
  assert.equal(batched.calls.length, 7);
  assert.equal(batched.calls.filter(c => c.action === 'batch').length, 2);
  assert.deepEqual(batchedTools.calls, ['launch', 'wait', 'read']);
  assert.deepEqual(flattened(batched.calls), flattened(stable.calls));
  const stableInitialWatching = stable.calls.find(c => c.action === 'checkpoint' && c.fields.fields?.state === 'watching').fields.fields;
  const batchInitialWatching = batched.calls.find(c => c.action === 'batch').steps[1].fields;
  for (const key of ['state','threadId','runId','startPosition','lastReadPosition','waits','taskComplete'])
    assert.equal(batchInitialWatching[key], stableInitialWatching[key], `initial watching ${key}`);
  assert.ok(!Number.isNaN(Date.parse(batchInitialWatching.startedAt)));
  assert.equal(batchInitialWatching.startedAt, batchInitialWatching.observedAt);
  assert.equal(batched.record.state, 'review_pending');
  assert.equal(batched.record.leaseUntil, 0);
  assert.equal(stable.record.state, 'review_pending');
  assert.equal(stable.record.leaseUntil, 0);
});

test('accepted batch failure blocks monitoring, retains acceptance and does not retry the mutation', async () => {
  const store = inMemoryJournal(); const tools = happyTools();
  let launches = 0;
  const original = tools.toolMap.t3_thread_launch;
  tools.toolMap.t3_thread_launch = async args => { launches += 1; return original(args); };
  const journalBatch = async steps => {
    store.calls.push({ action: 'batch', steps });
    throw Object.assign(new Error('lost'), { code: 'journal_invalid_response', batch: { step: 1, completed: 1 } });
  };
  const result = await runAndMonitor({ ...base, ...bridge(store, tools), journalBatch });
  assert.equal(result.state, 'accepted_watch_blocked');
  assert.equal(result.errorCode, 'journal_invalid_response');
  assert.equal(launches, 1);
  assert.deepEqual(tools.calls, ['launch']);
  assert.equal(store.calls.filter(c => c.action === 'batch').length, 1);
  assert.equal(store.calls.some(c => c.action === 'release'), true, 'safe one-shot lease release still runs');
});

test('terminal batch failure reports a blocker, does not redeliver and does not retry release', async () => {
  const store = inMemoryJournal(); const tools = happyTools();
  let batches = 0;
  const journalBatch = async (steps, fence) => {
    batches += 1;
    if (batches === 1) return store.batch(steps, fence);
    throw Object.assign(new Error('partial'), { code: 'journal_error', batch: { step: 0, completed: 0, uncertain: true } });
  };
  const result = await runAndMonitor({ ...base, ...bridge(store, tools), journalBatch });
  assert.equal(result.state, 'watch_blocked');
  assert.equal(result.resultRunId, 'run-1');
  assert.equal(result.errorCode, 'journal_error');
  assert.equal(result.releaseState, 'batch_uncertain');
  assert.deepEqual(tools.calls, ['launch', 'wait', 'read']);
  assert.equal(batches, 2);
  assert.equal(store.calls.filter(c => c.action === 'batch').length, 1);
  assert.equal(store.calls.filter(c => c.action === 'release').length, 0, 'uncertain terminal batch is not replayed');
});

test('pending approval and cancellation do not enter the terminal batch', async () => {
  for (const mode of ['blocked', 'aborted']) {
    const store = inMemoryJournal(); const tools = happyTools();
    let batches = 0;
    const journalBatch = async (steps, fence) => { batches += 1; return store.batch(steps, fence); };
    tools.toolMap.t3_thread_wait = async () => ({ threadId: 'thread-1', runId: 'run-1', status: mode === 'blocked' ? 'waiting' : 'completed', timedOut: mode === 'blocked', resultRun: mode === 'blocked' ? null : { runId: 'run-1', status: 'completed' } });
    const controller = new AbortController();
    if (mode === 'aborted') controller.abort();
    const result = await runAndMonitor({ ...base, ...bridge(store, tools), journalBatch, signal: controller.signal });
    assert.ok(!['ready_for_review', 'review_pending'].includes(result.state));
    assert.equal(batches, mode === 'aborted' ? 0 : 1, `${mode}: no terminal batch`);
    assert.equal(store.calls.filter(c => c.action === 'release').length > 0, true);
  }
});

test('a read-only read never becomes a batch mutation', async () => {
  const store = inMemoryJournal();
  await assert.rejects(() => store.batch([{ action: 'read' }], { operationId: 'op-batch-1', owner: 'worker', generation: 1 }),
    e => e.code === 'unsupported_batch_step');
});

test('batch bridge runs one reviewed command and preserves partial/lost reply information', async () => {
  const toolMap = Object.fromEntries(names.map(name => [name, async () => ({})]));
  const commands = [];
  const bridge = createToolBridge({
    toolMap, journalPath: '/tmp/journal.py', journalDirectory: '/tmp/store',
    batchJournalPath: '/tmp/journal-batch.py',
    execCommand: async args => {
      commands.push(args);
      return { exit_code: 0, output: '{"ok":true,"completed":1,"results":[{"record":{"operationId":"op","owner":"worker","generation":1,"state":"accepted"}}]}' };
    },
  });
  const fence = { operationId: 'op', owner: 'worker', generation: 1 };
  const result = await bridge.journalBatch([{ action: 'checkpoint', fields: { state: 'accepted' } }], fence);
  assert.equal(result.completed, 1);
  assert.equal(result.results.length, 1);
  assert.equal(commands.length, 1);
  assert.ok(commands[0].cmd.includes("'/tmp/journal-batch.py'"));
  assert.ok(commands[0].cmd.includes("--directory '/tmp/store'"));

  const partial = createToolBridge({
    toolMap, journalPath: '/tmp/journal.py', journalDirectory: '/tmp/store',
    batchJournalPath: '/tmp/journal-batch.py',
    execCommand: async () => ({ exit_code: 1, output: '{"ok":false,"error":{"code":"identity_changed"},"step":1,"completed":1,"uncertain":true}' }),
  });
  await assert.rejects(() => partial.journalBatch([{ action: 'checkpoint', fields: {} }], fence),
    e => e.code === 'identity_changed' && e.batch.completed === 1 && e.batch.uncertain === true);

  const lost = createToolBridge({
    toolMap, journalPath: '/tmp/journal.py', journalDirectory: '/tmp/store',
    batchJournalPath: '/tmp/journal-batch.py',
    execCommand: async () => ({ exit_code: 0, output: '{"ok":true,"comple' }),
  });
  await assert.rejects(() => lost.journalBatch([{ action: 'checkpoint', fields: {} }], fence),
    e => e.code === 'journal_invalid_response');
});

test('batch bridge rejects malformed success, bad records and an unclosed lease', async () => {
  const toolMap = Object.fromEntries(names.map(name => [name, async () => ({})]));
  const fence = { operationId: 'op', owner: 'worker', generation: 1 };
  const checkpoint = { action: 'checkpoint', fields: { state: 'accepted' } };
  const releaseSteps = [
    { action: 'checkpoint', fields: { state: 'review_pending' } },
    { action: 'release' },
  ];
  const record = { operationId: 'op', owner: 'worker', generation: 1, state: 'accepted', leaseUntil: 1 };
  const malformed = [
    [{ ok: true, completed: 1 }, 'batch_invalid_response'],
    [{ ok: true, completed: 2, results: [{ record }] }, 'batch_invalid_response'],
    [{ ok: true, completed: 1, results: [{}] }, 'batch_invalid_record'],
    [{ ok: true, completed: 1, results: [{ record: { ...record, operationId: 'other' } }] }, 'batch_record_mismatch'],
    [{ ok: true, completed: 1, results: [{ record: { ...record, owner: 'other' } }] }, 'batch_record_mismatch'],
    [{ ok: true, completed: 1, results: [{ record: { ...record, generation: 2 } }] }, 'batch_record_mismatch'],
    [{ ok: true, completed: 1, results: [{ record: { ...record, state: 'watching' } }] }, 'batch_record_mismatch'],
    [{ ok: true, completed: 2, results: [{ record: { ...record, state: 'review_pending' } }, { record: { ...record, state: 'review_pending', leaseUntil: 5 } }] }, 'batch_record_mismatch'],
  ];
  for (const [reply, code] of malformed) {
    const bridge = createToolBridge({
      toolMap, journalPath: '/tmp/journal.py', journalDirectory: '/tmp/store',
      batchJournalPath: '/tmp/journal-batch.py',
      execCommand: async () => ({ exit_code: 0, output: JSON.stringify(reply) }),
    });
    const steps = reply.completed === 2 || reply.results?.length === 2 ? releaseSteps : [checkpoint];
    await assert.rejects(() => bridge.journalBatch(steps, fence), e => e.code === code, code);
  }
  await assert.rejects(() => createToolBridge({
    toolMap, journalPath: '/tmp/journal.py', journalDirectory: '/tmp/store', batchJournalPath: '/tmp/journal-batch.py',
    execCommand: async () => ({ exit_code: 0, output: '{}' }),
  }).journalBatch([checkpoint], { operationId: 'op', owner: '', generation: 1 }), e => e.code === 'batch_invalid_response');
});

test('malformed successful replies never commit either batch phase as ready', async () => {
  const cases = [
    ['missing results', (reply) => ({ ok: true, completed: reply.completed })],
    ['wrong completed count', (reply) => ({ ...reply, completed: reply.completed - 1 })],
    ['missing result record', (reply, phase) => ({ ...reply, results: reply.results.map((entry, i) => i === reply.results.length - 1 ? {} : entry) })],
    ['wrong operation', (reply) => ({ ...reply, results: reply.results.map((entry, i) => i === 0 ? { record: { ...entry.record, operationId: 'other' } } : entry) })],
    ['wrong owner', (reply) => ({ ...reply, results: reply.results.map((entry, i) => i === 0 ? { record: { ...entry.record, owner: 'other' } } : entry) })],
    ['wrong generation', (reply) => ({ ...reply, results: reply.results.map((entry, i) => i === 0 ? { record: { ...entry.record, generation: 99 } } : entry) })],
    ['wrong environment', (reply) => ({ ...reply, results: reply.results.map((entry, i) => i === 1 ? { record: { ...entry.record, environmentId: 'env-2' } } : entry) })],
    ['wrong thread', (reply) => ({ ...reply, results: reply.results.map((entry, i) => i === 1 ? { record: { ...entry.record, threadId: 'thread-2' } } : entry) })],
    ['wrong run', (reply) => ({ ...reply, results: reply.results.map((entry, i) => i === 1 ? { record: { ...entry.record, runId: 'run-2' } } : entry) })],
    ['wrong state', (reply) => ({ ...reply, results: reply.results.map((entry, i) => i === 0 ? { record: { ...entry.record, state: 'dispatch_pending' } } : entry) })],
  ];
  for (const phase of ['accepted', 'terminal']) {
    for (const [name, mutate] of cases) {
      const store = inMemoryJournal(); const tools = happyTools();
      let batches = 0;
      const journalBatch = async (steps, fence) => {
        batches += 1;
        if (phase === 'terminal' && batches === 1) return store.batch(steps, fence);
        const reply = await store.batch(steps, fence);
        return mutate(JSON.parse(JSON.stringify(reply)), phase);
      };
      const result = await runAndMonitor({ ...base, ...bridge(store, tools), journalBatch });
      assert.equal(result.state, phase === 'accepted' ? 'accepted_watch_blocked' : 'watch_blocked', `${phase}: ${name}`);
      assert.equal(result.accepted?.threadId, 'thread-1', `${phase}: ${name} acceptance retained`);
      assert.equal(result.accepted?.runId, 'run-1', `${phase}: ${name} accepted run retained`);
      assert.ok(!['ready_for_review', 'review_pending'].includes(result.state), `${phase}: ${name} no false ready`);
      assert.equal(batches, phase === 'accepted' ? 1 : 2, `${phase}: ${name} no batch replay`);
      assert.deepEqual(tools.calls, phase === 'accepted' ? ['launch'] : ['launch', 'wait', 'read'], `${phase}: ${name} no duplicated native work`);
      assert.equal(store.calls.filter(c => c.action === 'release').length, phase === 'accepted' ? 1 : 0, `${phase}: ${name} release behavior`);
    }
  }
  for (const [name, mutate] of [
    ['missing startedAt', entry => { delete entry.record.startedAt; }],
    ['wrong lastReadPosition', entry => { entry.record.lastReadPosition = 4; }],
    ['wrong waits', entry => { entry.record.waits = 1; }],
  ]) {
    const store = inMemoryJournal(); const tools = happyTools();
    let batches = 0;
    const journalBatch = async (steps, fence) => {
      batches += 1;
      const reply = await store.batch(steps, fence);
      mutate(reply.results[1]);
      return reply;
    };
    const result = await runAndMonitor({ ...base, ...bridge(store, tools), journalBatch });
    assert.equal(result.state, 'accepted_watch_blocked', `watching metadata: ${name}`);
    assert.equal(result.accepted?.threadId, 'thread-1');
    assert.equal(batches, 1, `watching metadata: ${name} no replay`);
    assert.deepEqual(tools.calls, ['launch'], `watching metadata: ${name} no wait/read`);
  }
  const terminalLease = inMemoryJournal(); const terminalTools = happyTools();
  let terminalBatches = 0;
  const terminalJournalBatch = async (steps, fence) => {
    terminalBatches += 1;
    if (terminalBatches === 1) return terminalLease.batch(steps, fence);
    const reply = await terminalLease.batch(steps, fence);
    return { ...reply, results: reply.results.map((entry, i) => i === 1 ? { record: { ...entry.record, leaseUntil: 3 } } : entry) };
  };
  const terminalResult = await runAndMonitor({ ...base, ...bridge(terminalLease, terminalTools), journalBatch: terminalJournalBatch });
  assert.equal(terminalResult.state, 'watch_blocked');
  assert.ok(!['ready_for_review', 'review_pending'].includes(terminalResult.state));
  assert.equal(terminalBatches, 2);
  assert.deepEqual(terminalTools.calls, ['launch', 'wait', 'read']);
  assert.equal(terminalLease.calls.filter(c => c.action === 'release').length, 0, 'nonzero lease reply is not retried');

  for (const [name, mutate] of [
    ['environment', entry => { entry.record.environmentId = 'env-2'; }],
    ['thread', entry => { entry.record.threadId = 'thread-2'; }],
    ['run', entry => { entry.record.runId = 'run-2'; }],
    ['resultRun', entry => { entry.record.resultRunId = 'run-2'; }],
    ['review state', entry => { entry.record.state = 'watching'; }],
    ['lease', entry => { entry.record.leaseUntil = 3; }],
  ]) {
    const store = inMemoryJournal(); const tools = happyTools();
    let batches = 0;
    const journalBatch = async (steps, fence) => {
      batches += 1;
      if (batches === 1) return store.batch(steps, fence);
      const reply = await store.batch(steps, fence);
      mutate(reply.results[1]);
      return reply;
    };
    const result = await runAndMonitor({ ...base, ...bridge(store, tools), journalBatch });
    assert.equal(result.state, 'watch_blocked', `terminal release fence: ${name}`);
    assert.equal(result.errorCode, 'batch_record_mismatch', `terminal release fence: ${name}`);
    assert.equal(result.releaseState, 'batch_uncertain', `terminal release fence: ${name}`);
    assert.equal(result.resultRunId, 'run-1', `terminal release fence: ${name}`);
    assert.equal(result.accepted?.runId, 'run-1', `terminal release fence: ${name}`);
    assert.equal(batches, 2, `terminal release fence: ${name} no replay`);
    assert.deepEqual(tools.calls, ['launch', 'wait', 'read'], `terminal release fence: ${name} no duplicated native work`);
    assert.equal(store.calls.filter(c => c.action === 'release').length, 0, `terminal release fence: ${name} no forced release`);
  }
});

test('batch mode fences every pagination read and never adopts a changed result run', async () => {
  const store = inMemoryJournal(); const tools = happyTools();
  let pages = 0;
  tools.toolMap.t3_thread_read = async () => {
    pages += 1;
    return {
      thread: { threadId: 'thread-1', activeRunId: null, pendingRequestCount: 0, latestRunId: 'run-1' },
      recentRuns: [{ runId: 'run-1', status: 'completed' }],
      items: [{ runId: 'run-1', type: 'assistant_message', status: 'completed', text: 'RESULT', textTruncated: false }],
      hasMore: pages < 2,
      nextPosition: pages < 2 ? 5 : 6,
    };
  };
  const result = await runAndMonitor({ ...base, ...bridge(store, tools), journalBatch: store.batch });
  assert.equal(result.state, 'ready_for_review');
  assert.equal(pages, 2);
  const states = flattened(store.calls);
  assert.equal(states.filter(s => s === 'checkpoint:watching').length, 4, 'initial + pre-wait + one fence per page');
  assert.equal(store.calls.filter(c => c.action === 'batch').length, 2);

  const changedStore = inMemoryJournal(); const changedTools = happyTools();
  changedTools.toolMap.t3_thread_wait = async () => ({ threadId: 'thread-1', runId: 'run-1', status: 'completed', timedOut: false, resultRun: { runId: 'run-2', status: 'completed' } });
  const changed = await runAndMonitor({ ...base, ...bridge(changedStore, changedTools), journalBatch: changedStore.batch });
  assert.equal(changed.state, 'result_run_changed');
  assert.equal(changedStore.calls.filter(c => c.action === 'batch').length, 1, 'no terminal batch for a different result run');
});
