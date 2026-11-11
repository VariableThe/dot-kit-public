#!/usr/bin/env node
/** Pure first-install setup classifier. No network, writes, tokens, or server calls. */
import { isIP } from 'node:net';
import { pathToFileURL } from 'node:url';

const AGENTS = new Set(['hosted', 'local']);
const DISCOVERY = new Set(['wizard', 'settings', 'unknown']);
const APPROVALS = new Set(['hosted', 'code', 'unknown']);

const ISSUE_MESSAGES = {
  missing_mcp_url: 'Copy the MCP URL from T3: Cmd/Ctrl+K → Use T3 Code in another app (MCP), or Settings → Environments → Copy MCP URL.',
  invalid_mcp_url: 'Use the exact URL copied from T3; do not hand-build or truncate it.',
  unsupported_scheme: 'Use an http(s) MCP URL. T3 remote MCP uses HTTP with OAuth.',
  embedded_credentials: 'Remove the username or password from the URL. T3 uses OAuth; do not put credentials in repository files or URLs.',
  wrong_mcp_path: 'Copied T3 MCP URLs end in /mcp. Re-copy the URL instead of editing the path.',
  unexpected_url_parts: 'Remove the query string and fragment. Re-copy the plain MCP URL from T3.',
  hosted_unreachable_local: 'A hosted assistant cannot reach localhost or a device-local address. Turn on T3 Connect for the environment, then copy its https MCP URL again.',
  hosted_unreachable_private: 'A hosted assistant cannot reach a private or LAN address. Turn on T3 Connect for the environment, then copy its https MCP URL again.',
  hosted_unreachable_tailnet: 'A hosted assistant cannot reach a tailnet address. Use the environment’s https T3 Connect URL; keep tailnet URLs for agents on your own devices.',
  nonpublic_address: 'This is a multicast, reserved, benchmarking, or documentation address, not a normal client destination. Re-copy the environment’s /mcp URL; do not hand-edit the host.',
  insecure_remote_url: 'Sign-in only completes over https or localhost. Use the https T3 Connect URL, a Tailscale HTTPS URL, or a localhost URL for an agent on this host.',
  unspecified_bind_address: '0.0.0.0 and :: are bind addresses, not addresses to connect to. Use localhost for an agent on this host, or the environment’s https URL.',
};

function ipv4Kind(address) {
  const [a, b, c] = address.split('.').map(Number);
  if (a === 0) return address === '0.0.0.0' ? 'unspecified' : 'nonpublic';
  if (a === 127) return 'local';
  if (a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) ||
      (a === 169 && b === 254)) return 'private';
  if (a === 100 && b >= 64 && b <= 127) return 'tailnet';
  // Multicast, reserved, benchmark, and documentation ranges are not normal client destinations.
  if (a >= 224) return 'nonpublic';
  if (a === 192 && b === 0 && (c === 0 || c === 2)) return 'nonpublic';
  if (a === 192 && b === 88 && c === 99) return 'nonpublic';
  if (a === 198 && (b === 18 || b === 19)) return 'nonpublic';
  if (a === 198 && b === 51 && c === 100) return 'nonpublic';
  if (a === 203 && b === 0 && c === 113) return 'nonpublic';
  return 'public';
}

/** The IPv4 address inside an IPv4-mapped IPv6 literal, or null. */
function mappedIpv4(host) {
  const match = /^::ffff:(?:(\d{1,3}(?:\.\d{1,3}){3})|([0-9a-f]{1,4}):([0-9a-f]{1,4}))$/i.exec(host);
  if (match === null) return null;
  if (match[1] !== undefined) return match[1];
  const high = Number.parseInt(match[2], 16);
  const low = Number.parseInt(match[3], 16);
  return `${high >> 8}.${high & 255}.${low >> 8}.${low & 255}`;
}

function ipv6Kind(address) {
  if (address === '::') return 'unspecified';
  if (address === '::1') return 'local';
  if (address.startsWith('fd7a:115c:a1e0:')) return 'tailnet';
  const first = Number.parseInt(address.split(':')[0] || '0', 16);
  if ((first & 0xffc0) === 0xfe80) return 'private';
  if ((first & 0xfe00) === 0xfc00) return 'private';
  if ((first & 0xff00) === 0xff00) return 'nonpublic';
  return 'public';
}

/** Host classification is a naming/range heuristic, not DNS resolution or proof of routability. */
function hostKind(host) {
  const normalized = host.replace(/\.+$/, '');
  if (normalized === 'localhost' || normalized.endsWith('.localhost')) return 'local';
  if (normalized.endsWith('.ts.net')) return 'tailnet';
  if (normalized.endsWith('.local')) return 'private';
  const address = mappedIpv4(normalized) ?? normalized;
  const version = isIP(address);
  if (version === 4) return ipv4Kind(address);
  if (version === 6) return ipv6Kind(address);
  return 'public';
}

function inspectUrl(value) {
  const issues = [];
  if (typeof value !== 'string' || value.trim().length === 0)
    return { issues: [{ code: 'missing_mcp_url' }], url: null };
  const raw = value.trim();
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    return { issues: [{ code: 'invalid_mcp_url' }], url: null };
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:')
    return { issues: [{ code: 'unsupported_scheme' }], url: null };
  const scheme = parsed.protocol.slice(0, -1);
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '');
  const kind = hostKind(host);
  const url = { scheme, hostKind: kind, pathEndsInMcp: parsed.pathname.endsWith('/mcp') };
  if (parsed.username.length > 0 || parsed.password.length > 0) issues.push({ code: 'embedded_credentials' });
  if (!url.pathEndsInMcp) issues.push({ code: 'wrong_mcp_path' });
  // URL.search/hash collapse empty delimiters, so inspect the copied text as well.
  if (raw.includes('?') || raw.includes('#') || parsed.search.length > 0 || parsed.hash.length > 0) issues.push({ code: 'unexpected_url_parts' });
  if (kind === 'nonpublic') issues.push({ code: 'nonpublic_address' });
  if (kind === 'unspecified') issues.push({ code: 'unspecified_bind_address' });
  if (scheme === 'http' && kind !== 'local') issues.push({ code: 'insecure_remote_url' });
  return { issues, url };
}

/**
 * Classify setup inputs copied by a person. The result never echoes the URL,
 * and URL shape is not proof of reachability, consent, or write permission.
 */
export function assessSetupReadiness({ mcpUrl, agent, discovery = 'unknown', approval = 'unknown' } = {}) {
  if (!AGENTS.has(agent)) throw new TypeError("agent must be 'hosted' or 'local'");
  if (!DISCOVERY.has(discovery)) throw new TypeError("discovery must be 'wizard', 'settings' or 'unknown'");
  if (!APPROVALS.has(approval)) throw new TypeError("approval must be 'hosted', 'code' or 'unknown'");

  const inspected = inspectUrl(mcpUrl);
  const issueCodes = [...inspected.issues.map(issue => issue.code)];
  // A hosted agent cannot reach a local, private, or tailnet listener.
  if (inspected.url && agent === 'hosted' && ['local', 'private', 'tailnet'].includes(inspected.url.hostKind))
    issueCodes.push(`hosted_unreachable_${inspected.url.hostKind}`);
  const issues = issueCodes.map(code => ({ code, message: ISSUE_MESSAGES[code] }));

  const path = discovery === 'wizard' ? 'wizard'
    : discovery === 'settings' ? 'settings_fallback' : 'unknown';
  const consent = approval === 'hosted' ? 'hosted_no_code'
    : approval === 'code' ? 'one_time_code' : 'unknown';

  const nextAction = issues[0]?.message
    ?? (path === 'unknown'
      ? 'Open Cmd/Ctrl+K and look for “Use T3 Code in another app (MCP)”. If it is absent, use Settings → Environments → Copy MCP URL (manual fallback).'
      : path === 'settings_fallback'
        ? 'Open Settings → Environments → Copy MCP URL on the intended environment. The sign-in page may ask for an access level and a one-time code created on the host.'
        : consent === 'hosted_no_code'
          ? 'Approve the sign-in on the T3 web app with the linked T3 Connect account, choose the least access you need, then confirm with a simple read in the client. If that session cannot approve the level, use the offered sign-in-code link.'
          : consent === 'one_time_code'
            ? 'Enter the one-time code from the host wizard, Settings → Sharing & access, or `t3 auth pairing create`. It is valid 5 minutes and one use, then confirm with a simple read in the client.'
            : 'Follow the sign-in page you get; it names the app, environment and access level. Then confirm with a simple read in the client.');

  const { url } = inspected;
  return {
    state: issues.length === 0 ? 'ready' : 'needs_fix',
    path,
    consent,
    agentLocation: agent,
    url: url ? { scheme: url.scheme, hostKind: url.hostKind, pathEndsInMcp: url.pathEndsInMcp } : null,
    issues,
    nextAction,
    liveChecksPerformed: false,
    publicReachabilityVerified: false,
    writeExecutionVerified: false,
  };
}

function parseCli(argv) {
  const positional = [];
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg.startsWith('--')) {
      const [name, inline] = arg.slice(2).split('=', 2);
      const value = inline ?? argv[++index];
      if (!['agent', 'discovery', 'approval'].includes(name) || value === undefined)
        throw new TypeError('Unknown or incomplete option. Use --agent, --discovery or --approval with a value.');
      options[name] = value;
    } else positional.push(arg);
  }
  if (positional.length !== 1) throw new TypeError('Pass exactly one MCP URL.');
  return { mcpUrl: positional[0], agent: options.agent ?? 'hosted', discovery: options.discovery ?? 'unknown', approval: options.approval ?? 'unknown' };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const result = assessSetupReadiness(parseCli(process.argv.slice(2)));
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
    process.exitCode = result.state === 'ready' ? 0 : 1;
  } catch (error) {
    process.stderr.write(`${error.message}\nUsage: node setup-readiness.mjs '<mcp-url>' [--agent hosted|local] [--discovery wizard|settings|unknown] [--approval hosted|code|unknown]\n`);
    process.exitCode = 2;
  }
}
