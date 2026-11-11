import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { assessSetupReadiness } from './setup-readiness.mjs';

const setup = input => assessSetupReadiness(input);
const codes = result => result.issues.map(issue => issue.code);
const cli = () => fileURLToPath(new URL('./setup-readiness.mjs', import.meta.url));

test('hosted wizard path with a public https URL is ready without claiming reachability', () => {
  const result = setup({ mcpUrl: 'https://env.t3.codes/mcp', agent: 'hosted', discovery: 'wizard', approval: 'hosted' });
  assert.equal(result.state, 'ready');
  assert.equal(result.path, 'wizard');
  assert.equal(result.consent, 'hosted_no_code');
  assert.equal(result.agentLocation, 'hosted');
  assert.deepEqual(result.url, { scheme: 'https', hostKind: 'public', pathEndsInMcp: true });
  assert.deepEqual(result.issues, []);
  assert.equal(result.liveChecksPerformed, false);
  assert.equal(result.publicReachabilityVerified, false);
  assert.equal(result.writeExecutionVerified, false);
  assert.match(result.nextAction, /T3 web app/);
});

test('local agent on the host can use localhost with a one-time code fallback', () => {
  const result = setup({ mcpUrl: 'http://127.0.0.1:3773/mcp', agent: 'local', discovery: 'wizard', approval: 'code' });
  assert.equal(result.state, 'ready');
  assert.equal(result.consent, 'one_time_code');
  assert.deepEqual(result.url, { scheme: 'http', hostKind: 'local', pathEndsInMcp: true });
  assert.match(result.nextAction, /5 minutes/);
});

test('a local agent may use https over a tailnet address', () => {
  const result = setup({ mcpUrl: 'https://machine.tailnet.ts.net/mcp', agent: 'local' });
  assert.equal(result.state, 'ready');
  assert.equal(result.url.hostKind, 'tailnet');
  assert.equal(result.path, 'unknown');
});

test('cold start: no URL fails with the copy step and performs no live check', () => {
  const result = setup({ agent: 'hosted' });
  assert.equal(result.state, 'needs_fix');
  assert.deepEqual(codes(result), ['missing_mcp_url']);
  assert.equal(result.url, null);
  assert.match(result.issues[0].message, /Copy the MCP URL/);
  assert.equal(result.liveChecksPerformed, false);
});

test('cold start: non-string and malformed URLs fail closed', () => {
  assert.deepEqual(codes(setup({ mcpUrl: 42, agent: 'hosted' })), ['missing_mcp_url']);
  assert.deepEqual(codes(setup({ mcpUrl: 'not a url', agent: 'hosted' })), ['invalid_mcp_url']);
});

test('unsupported URL schemes are rejected before any host reasoning', () => {
  const result = setup({ mcpUrl: 'ftp://env.t3.codes/mcp', agent: 'hosted' });
  assert.equal(result.state, 'needs_fix');
  assert.deepEqual(codes(result), ['unsupported_scheme']);
  assert.equal(result.url, null);
});

test('a hosted assistant cannot reach localhost', () => {
  const result = setup({ mcpUrl: 'http://localhost:3773/mcp', agent: 'hosted' });
  assert.deepEqual(codes(result), ['hosted_unreachable_local']);
  assert.match(result.issues[0].message, /T3 Connect/);
});

test('a hosted assistant cannot reach tailnet or private listeners', () => {
  assert.deepEqual(codes(setup({ mcpUrl: 'https://100.101.102.103/mcp', agent: 'hosted' })), ['hosted_unreachable_tailnet']);
  assert.deepEqual(codes(setup({ mcpUrl: 'https://192.168.1.5/mcp', agent: 'hosted' })), ['hosted_unreachable_private']);
  assert.deepEqual(codes(setup({ mcpUrl: 'https://[fd00::1]/mcp', agent: 'hosted' })), ['hosted_unreachable_private']);
});

test('public domain names that start with fc or fd are not private addresses', () => {
  for (const host of ['fda.gov', 'fc.example.com', 'fd.example.com']) {
    const result = setup({ mcpUrl: `https://${host}/mcp`, agent: 'hosted' });
    assert.equal(result.url.hostKind, 'public', host);
    assert.deepEqual(codes(result), [], host);
  }
});

test('IPv6 ranges and IPv4-mapped addresses are parsed as addresses', () => {
  for (const [mcpUrl, kind] of [
    ['https://[fc00::1]/mcp', 'private'],
    ['https://[fd12:3456::1]/mcp', 'private'],
    ['https://[fe80::1]/mcp', 'private'],
    ['https://[fd7a:115c:a1e0::1]/mcp', 'tailnet'],
    ['https://[::ffff:127.0.0.1]/mcp', 'local'],
    ['https://[::ffff:192.168.1.5]/mcp', 'private'],
    ['https://[::ffff:8.8.8.8]/mcp', 'public'],
    ['https://[ff02::1]/mcp', 'nonpublic'],
  ]) assert.equal(setup({ mcpUrl, agent: 'local' }).url.hostKind, kind, mcpUrl);
});

test('empty query and fragment delimiters are rejected', () => {
  for (const mcpUrl of ['https://env.t3.codes/mcp?', 'https://env.t3.codes/mcp#']) {
    const result = setup({ mcpUrl, agent: 'hosted' });
    assert.equal(result.state, 'needs_fix', mcpUrl);
    assert.deepEqual(codes(result), ['unexpected_url_parts'], mcpUrl);
  }
});

test('trailing-dot local, private, and tailnet hostnames keep their classification', () => {
  for (const [mcpUrl, kind] of [
    ['http://localhost.:3773/mcp', 'local'],
    ['http://127.0.0.1.:3773/mcp', 'local'],
    ['https://machine.tailnet.ts.net./mcp', 'tailnet'],
    ['https://192.168.1.5./mcp', 'private'],
    ['https://printer.local./mcp', 'private'],
  ]) {
    const result = setup({ mcpUrl, agent: 'local' });
    assert.equal(result.url.hostKind, kind, mcpUrl);
    assert.equal(result.state, 'ready', mcpUrl);
  }
  assert.deepEqual(codes(setup({ mcpUrl: 'https://192.168.1.5./mcp', agent: 'hosted' })), ['hosted_unreachable_private']);
});

test('multicast, reserved, benchmark, and documentation IPv4 ranges are not called public', () => {
  for (const mcpUrl of [
    'https://224.0.0.1/mcp',
    'https://239.255.255.250/mcp',
    'https://240.0.0.1/mcp',
    'https://255.255.255.255/mcp',
    'https://192.0.0.1/mcp',
    'https://192.0.2.1/mcp',
    'https://192.88.99.1/mcp',
    'https://198.18.0.1/mcp',
    'https://198.19.255.254/mcp',
    'https://198.51.100.1/mcp',
    'https://203.0.113.1/mcp',
  ]) {
    const result = setup({ mcpUrl, agent: 'hosted' });
    assert.equal(result.state, 'needs_fix', mcpUrl);
    assert.equal(result.url.hostKind, 'nonpublic', mcpUrl);
    assert.deepEqual(codes(result), ['nonpublic_address'], mcpUrl);
  }
});

test('bind/unspecified addresses are not treated as safe loopback endpoints', () => {
  for (const mcpUrl of ['http://0.0.0.0:3773/mcp', 'https://0.0.0.0/mcp', 'http://[::]:3773/mcp', 'https://[::]/mcp']) {
    const result = setup({ mcpUrl, agent: 'local' });
    assert.equal(result.state, 'needs_fix', mcpUrl);
    assert.equal(result.url.hostKind, 'unspecified', mcpUrl);
    assert.ok(codes(result).includes('unspecified_bind_address'), mcpUrl);
    assert.match(result.issues[0].message, /bind addresses/);
  }
});

test('plain http outside localhost cannot finish sign-in even for a local agent', () => {
  const result = setup({ mcpUrl: 'http://192.168.1.5:3773/mcp', agent: 'local' });
  assert.deepEqual(codes(result), ['insecure_remote_url']);
  assert.match(result.issues[0].message, /https or localhost/);
});

test('a local agent can reach localhost over IPv6 loopback', () => {
  const result = setup({ mcpUrl: 'http://[::1]:3773/mcp', agent: 'local' });
  assert.equal(result.state, 'ready');
  assert.equal(result.url.hostKind, 'local');
});

test('copied URLs must end in /mcp, including no trailing slash', () => {
  assert.deepEqual(codes(setup({ mcpUrl: 'https://env.t3.codes', agent: 'hosted' })), ['wrong_mcp_path']);
  assert.deepEqual(codes(setup({ mcpUrl: 'https://env.t3.codes/mcp/', agent: 'hosted' })), ['wrong_mcp_path']);
  assert.deepEqual(codes(setup({ mcpUrl: 'https://env.t3.codes/MCP', agent: 'hosted' })), ['wrong_mcp_path']);
});

test('credential-like URL parts are rejected and never echoed', () => {
  const result = setup({ mcpUrl: 'https://private-user:private-pass@env.t3.codes/mcp?token=private-token', agent: 'hosted' });
  assert.deepEqual(codes(result), ['embedded_credentials', 'unexpected_url_parts']);
  const rendered = JSON.stringify(result);
  for (const secret of ['private-user', 'private-pass', 'private-token', 'env.t3.codes']) {
    assert.equal(rendered.includes(secret), false);
  }
});

test('older server fallback is explicit and ready when the URL is valid', () => {
  const result = setup({ mcpUrl: 'https://env.t3.codes/mcp', agent: 'hosted', discovery: 'settings', approval: 'code' });
  assert.equal(result.state, 'ready');
  assert.equal(result.path, 'settings_fallback');
  assert.equal(result.consent, 'one_time_code');
  assert.match(result.nextAction, /Settings → Environments → Copy MCP URL/);
});

test('unknown discovery and consent stay unclaimed instead of guessing a version', () => {
  const result = setup({ mcpUrl: 'https://env.t3.codes/mcp', agent: 'hosted' });
  assert.equal(result.state, 'ready');
  assert.equal(result.path, 'unknown');
  assert.equal(result.consent, 'unknown');
  assert.match(result.nextAction, /Cmd\/Ctrl\+K/);
});

test('the result shape stays stable for ready and needs_fix outcomes', () => {
  const keys = ['agentLocation', 'consent', 'issues', 'liveChecksPerformed', 'nextAction', 'path', 'publicReachabilityVerified', 'state', 'url', 'writeExecutionVerified'];
  for (const result of [
    setup({ mcpUrl: 'https://env.t3.codes/mcp', agent: 'hosted', discovery: 'wizard', approval: 'hosted' }),
    setup({ agent: 'hosted' }),
  ]) assert.deepEqual(Object.keys(result).sort(), keys);
});

test('every reported issue has an actionable message', () => {
  const results = [
    setup({ agent: 'hosted' }),
    setup({ mcpUrl: 'nope', agent: 'hosted' }),
    setup({ mcpUrl: 'ftp://x/mcp', agent: 'hosted' }),
    setup({ mcpUrl: 'https://u:p@env.t3.codes/mcp?x=1', agent: 'hosted' }),
    setup({ mcpUrl: 'https://env.t3.codes', agent: 'hosted' }),
    setup({ mcpUrl: 'http://192.168.1.5/mcp', agent: 'local' }),
    setup({ mcpUrl: 'http://localhost/mcp', agent: 'hosted' }),
  ];
  for (const result of results) {
    assert.ok(result.issues.length > 0);
    for (const issue of result.issues) {
      assert.equal(typeof issue.code, 'string');
      assert.ok(issue.message.length > 20, issue.code);
    }
  }
});

test('invalid agent or enum inputs are rejected before classification', () => {
  const url = 'https://env.t3.codes/mcp';
  assert.throws(() => setup({ mcpUrl: url }), /agent/);
  assert.throws(() => setup({ mcpUrl: url, agent: 'cloud' }), /agent/);
  assert.throws(() => setup({ mcpUrl: url, agent: 'hosted', discovery: 'palette' }), /discovery/);
  assert.throws(() => setup({ mcpUrl: url, agent: 'hosted', approval: 'pin' }), /approval/);
});

test('the helper has no network I/O, filesystem, or process imports', () => {
  const source = readFileSync(new URL('./setup-readiness.mjs', import.meta.url), 'utf8');
  assert.equal(source.includes("from 'node:net'"), true);
  for (const token of ['node:http', 'node:https', 'node:tls', 'node:dns', 'node:dgram', 'node:fs', 'node:child_process', 'fetch(', 'XMLHttpRequest', '.connect(', 'createConnection']) {
    assert.equal(source.includes(token), false, token);
  }
});

test('offline CLI prints sanitized JSON and uses a distinct exit code for each outcome', () => {
  const ready = spawnSync(process.execPath, [cli(), 'https://env.t3.codes/mcp', '--agent', 'hosted', '--discovery', 'wizard', '--approval', 'hosted'], { encoding: 'utf8' });
  assert.equal(ready.status, 0, ready.stderr);
  const parsed = JSON.parse(ready.stdout);
  assert.equal(parsed.state, 'ready');
  assert.equal(ready.stdout.includes('env.t3.codes'), false);

  const blocked = spawnSync(process.execPath, [cli(), 'http://localhost:3773/mcp', '--agent', 'hosted'], { encoding: 'utf8' });
  assert.equal(blocked.status, 1, blocked.stderr);
  assert.equal(JSON.parse(blocked.stdout).state, 'needs_fix');

  const usage = spawnSync(process.execPath, [cli(), 'https://env.t3.codes/mcp', '--agent'], { encoding: 'utf8' });
  assert.equal(usage.status, 2);
  assert.match(usage.stderr, /Usage:/);
});

test('CLI usage errors never echo a URL, host, or secret-like argument', () => {
  const cases = [
    [cli(), 'https://env.t3.codes/mcp', '--agent'],
    [cli(), '--https://private.example/secret'],
    [cli(), 'https://env.t3.codes/mcp', '--agent=https://private.example/secret'],
    [cli(), 'https://env.t3.codes/mcp', '--unknown=https://private.example/secret'],
    [cli(), 'https://env.t3.codes/mcp', 'extra'],
    [cli()],
  ];
  for (const args of cases) {
    const result = spawnSync(process.execPath, args, { encoding: 'utf8' });
    assert.equal(result.status, 2, result.stderr);
    assert.equal(result.stdout, '');
    assert.match(result.stderr, /Usage:/);
    for (const secret of ['private.example', 'env.t3.codes', 'secret']) {
      assert.equal(result.stderr.includes(secret), false, secret);
    }
  }
});
