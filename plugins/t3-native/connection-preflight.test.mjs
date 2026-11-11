import test from 'node:test';
import assert from 'node:assert/strict';
import { checkNativeConnection } from './connection-preflight.mjs';

const expectedEnvironmentId = 'verified-environment';
const environment = () => ({ environmentId: expectedEnvironmentId, serverVersion: '0.1.8-nightly.20261107.4340', label: 'private label' });
const projects = () => ({ projects: [{ id: 'project-1', title: 'private title', workspaceRoot: '/private/path' }], nextCursor: 1 });
const capabilities = () => ({ runtimeMode: 'full-access', interactionMode: 'default', providers: [{ private: 'details' }] });
function harness(responses, options = {}) {
  const calls = [];
  return { calls, run: () => checkNativeConnection({ expectedEnvironmentId, callTool: async (name, args) => {
    calls.push({ name, args });
    const value = responses.shift();
    if (value instanceof Error) throw value;
    return value;
  }, ...options }) };
}
test('uses exactly three bounded reads on the verified environment, never a model launch', async () => {
  const h = harness([environment(), projects(), capabilities()]);
  const result = await h.run();
  assert.equal(result.state, 'read_ready');
  assert.equal(result.runtimeCeiling, 'full-access');
  assert.equal(result.writeExecutionVerified, false);
  assert.deepEqual(h.calls, [
    { name: 't3_environment_read', args: { environmentId: expectedEnvironmentId } },
    { name: 't3_project_list', args: { environmentId: expectedEnvironmentId, limit: 1 } },
    { name: 'orchestrator_capabilities', args: { environmentId: expectedEnvironmentId } },
  ]);
  assert.equal(JSON.stringify(result).includes('private'), false);
});
test('accepts MCP structured and JSON-text envelopes', async () => {
  const h = harness([{ structuredContent: environment() }, { content: [{ type: 'text', text: JSON.stringify(projects()) }] }, { structuredContent: capabilities() }]);
  assert.equal((await h.run()).state, 'read_ready');
});
test('a different environment stops before reading any projects', async () => {
  const h = harness([{ ...environment(), environmentId: 'other-environment' }]);
  const result = await h.run();
  assert.equal(result.state, 'environment_mismatch');
  assert.equal(h.calls.length, 1);
});
test('a valid empty project page is readable, not a connectivity failure', async () => {
  const h = harness([environment(), { projects: [], nextCursor: null }, capabilities()]);
  assert.equal((await h.run()).state, 'read_ready');
});
for (const [label, value, at] of [
  ['missing identity', { serverVersion: '1' }, 0],
  ['missing version', { environmentId: expectedEnvironmentId }, 0],
  ['missing projects', {}, 1],
  ['missing project identity', { projects: [{}], nextCursor: null }, 1],
  ['missing pagination', { projects: [] }, 1],
  ['invalid pagination', { projects: [], nextCursor: -1 }, 1],
  ['unknown runtime', { runtimeMode: 'superuser', interactionMode: 'default' }, 2],
  ['missing interaction mode', { runtimeMode: 'full-access' }, 2],
]) {
  test('fails closed on ' + label, async () => {
    const responses = [environment(), projects(), capabilities()];
    responses[at] = value;
    const h = harness(responses);
    assert.equal((await h.run()).state, 'invalid_response');
    assert.equal(h.calls.length, at + 1);
  });
}
for (const failure of [
  { isError: true, structuredContent: { error: { code: 'permission_denied', message: 'secret' } } },
  Object.assign(new Error('secret'), { code: 'ETIMEDOUT' }),
  Object.assign(new Error('secret'), { code: 'secret-value' }),
]) {
  test('does not retry or expose details after a denied or failed read: ' + (failure.code ?? 'tool'), async () => {
    const h = harness([environment(), failure]);
    const result = await h.run();
    assert.equal(result.state, 'blocked');
    assert.equal(h.calls.length, 2);
    assert.equal(JSON.stringify(result).includes('secret'), false);
    assert.equal(result.writeExecutionVerified, false);
  });
}
test('an older server without the capabilities read stops there as blocked, without leaking details', async () => {
  const h = harness([environment(), projects(), { isError: true, structuredContent: { error: { code: 'unknown_tool', message: 'secret' } } }]);
  const result = await h.run();
  assert.equal(result.state, 'blocked');
  assert.equal(result.failedCheck, 'orchestrator_capabilities');
  assert.equal(result.errorCode, 'connector_read_failed');
  assert.equal(h.calls.length, 3);
  assert.equal(JSON.stringify(result).includes('secret'), false);
  assert.equal(result.writeExecutionVerified, false);
});
test('an error text envelope never counts as successful evidence', async () => {
  const h = harness([{ content: [{ type: 'text', text: 'not JSON' }] }]);
  assert.equal((await h.run()).state, 'blocked');
});
test('all supported runtime ceilings remain descriptive, not a write test', async () => {
  for (const runtimeMode of ['approval-required', 'auto-accept-edits', 'auto', 'full-access']) {
    const h = harness([environment(), projects(), { runtimeMode, interactionMode: 'plan' }]);
    const result = await h.run();
    assert.equal(result.state, 'read_ready');
    assert.equal(result.runtimeCeiling, runtimeMode);
    assert.equal(result.writeExecutionVerified, false);
  }
});
test('requires explicit verified target and connector before any reads', async () => {
  await assert.rejects(() => checkNativeConnection({}), /verified environment/);
  await assert.rejects(() => checkNativeConnection({ expectedEnvironmentId: ' ', callTool: async () => {} }), /verified environment/);
});
