# T3 setup and connection preflight

For a first install, start with the [shortest-path quickstart](QUICKSTART.md). This page records the detailed setup behavior, the read-only preflight, and the upgrade boundary. For how transport, URL discovery, and the consent page relate, see [connection choices compared](CONNECTION_MODES.md).

## Direct control, separate worker usage

The working cloud plugin calls T3's native MCP server from the cloud assistant. Reading projects, reading threads and sending authorized instructions do not require a local Codex or GPT session as a middleman. A T3 thread that performs a task still runs its chosen provider/model and uses that provider's allowance. The connector does not make worker inference free, and this guide makes no claim about a hosted assistant plan's usage limits.

This is the existing native connection, not the canceled experimental Python adapter. Keep a working connection rather than recreating it just to use the new wizard.

## Upstream setup improvements reviewed on 2026-11-10

- [#32937](https://github.com/pingdotgg/t3code/pull/32937): **Copy MCP URL** prefers the T3 Connect address instead of a local address that a hosted assistant cannot reach.
- [#33196](https://github.com/pingdotgg/t3code/pull/33196): **Cmd/Ctrl+K → Use T3 Code in another app (MCP)**, also in **Settings → Sharing & access**, checks the public route and provides app-specific setup instructions. The ChatGPT tile copies the URL; it does not install an official marketplace connector.
- [#33632](https://github.com/pingdotgg/t3code/pull/33632): hosted approval can authenticate the approving request with bearer/DPoP credentials and return directly to the requesting app. This broadens hosted-approval authentication; it does not replace the existing scope guards or MCP token model.
- [#33645](https://github.com/pingdotgg/t3code/pull/33645): fixes approval failure when successive DPoP credentials represent the same browser key, and removes a redundant authorization call.

The corrected hosted-approval fix first appears in [nightly .4449](https://github.com/pingdotgg/t3code/releases/tag/v0.1.9-nightly.20261110.4449). [Nightly .4452](https://github.com/pingdotgg/t3code/releases/tag/v0.1.9-nightly.20261110.4452) was the latest server nightly inspected during this review. These are prereleases, not a statement that this repository deployed or tested them.

## Set up a new connection, only when needed

1. Use the command-palette entry above on a server that supports it. For an older server, use **Settings → Environments → Copy MCP URL**.
2. A hosted assistant (ChatGPT) needs the environment's internet-reachable T3 Connect HTTPS URL ending in `/mcp`. A local or private tailnet address does not establish hosted reachability.
3. In ChatGPT, create the custom MCP app using that URL and **OAuth**. T3 uses dynamic registration; no client ID or client secret is needed.
4. Approve the exact app, environment and access level on the supported T3 consent screen. When offered, the hosted page uses the linked T3 Connect account. Otherwise the environment page accepts a host-issued one-time code or an eligible signed-in browser session; the requesting app does not provide the code. Never paste codes or credentials into repository files.
5. Confirm the connection with a simple read in the client. If you already have an authorized connector callback, the preflight below is optional advanced verification. Read success is not proof that a thread launch is permitted or working. Test writes only as part of a separately authorized task.

The new hosted page starts without a selected access level. The environment-local empty-code approval path starts at **Read only**, while older typed plain-code flows may start at Auto, capped by policy. Follow the actual consent screen and choose the least access required; do not silently widen it.

This wizard does not establish that Enterprise app policy or per-user distribution restrictions are solved. Do not broaden workspace sharing to work around them. It also does not require migrating an already working native connection or adding a new tunnel.

## Read-only connection preflight (optional advanced check)

A normal first install does not need this module, a repository clone, submodules, or a connector callback: a successful client read is the ordinary connection check. Use this preflight only when you already have an authorized connector callback and want bounded, sanitized read evidence.

Before connecting, the [quickstart](QUICKSTART.md) offers `setup-readiness.mjs` to classify the copied URL and the setup path. `setup-readiness.mjs` is offline and only checks shape; it is not a substitute for the live reads below.

Import `checkNativeConnection` from `connection-preflight.mjs` and supply:

- `expectedEnvironmentId`: the independently verified T3 environment ID. This is a T3 ID, not a device-selector ID.
- `callTool(name, args)`: the caller's existing authorized native T3 connector.

The module performs these calls in order, stopping on failure:

1. `t3_environment_read`: verifies that the returned environment ID matches the target.
2. `t3_project_list` with `limit: 1`: checks project readability without reading project contents or enumerating all projects.
3. `orchestrator_capabilities`: records the advertised runtime ceiling and interaction mode.

The result includes version and check status, but excludes project names/paths, provider catalogs and raw error messages. `read_ready` means these reads passed. `writeExecutionVerified` is always false: neither a reported Full access ceiling nor this preflight grants permission to act.

Call count and project page size are bounded; elapsed-time limits and cancellation belong to the connector callback. A callback that never settles leaves this preflight pending.

There is no transport implementation, token handling, retry, reconnect, launch, send, service restart or upgrade in the preflight module. Denial and uncertain failures stop the check. It does not run automatically; callers must integrate it with their existing connector.

Run its regression tests (offline fixtures) alongside the completion-monitor suite:

```sh
node --test plugins/t3-native/*.test.mjs
```

## Offline setup-readiness helper

`setup-readiness.mjs` is a pure classifier for first-install inputs. It accepts the copied MCP URL plus what the user observed, and returns the likely path, consent style, URL shape, and actionable issues:

```sh
node plugins/t3-native/setup-readiness.mjs 'https://<environment-address>/mcp' \
  --agent hosted --discovery wizard --approval hosted
```

- `--agent`: `hosted` (cloud assistant, the CLI default) or `local` (agent on your own computer).
- `--discovery`: `wizard` (palette/sharing entry present), `settings` (manual fallback via **Settings → Environments → Copy MCP URL**), or `unknown`.
- `--approval`: `hosted` (approve on the T3 web app without a code), `code` (one-time code), or `unknown`.

It never contacts a server, reads credentials, writes files, or echoes the URL. Exit code `0` means the supplied URL shape is ready (the setup path and consent style can still be `unknown`), `1` means an actionable issue, and `2` means a usage error. It cannot verify DNS, TLS, ingress, consent, access level, or write execution; only the live connection and `checkNativeConnection` can provide read evidence. `hostKind: "public"` is a naming/range heuristic, not proof of DNS or internet routability. It does not infer an older server from a version number.

## Upgrade boundary

The setup improvements are upstream T3 features. The version observations in this repository are dated provenance, not proof that the hosted UI flow was exercised. To change or upgrade the server, first coordinate active runs and review the official update route and rollback plan. Do not pin source to a newer version and call that a deployed upgrade. Do not reconnect, create credentials, change permissions, restart the live server or start a new tunnel as part of this read-only preflight.

Reference: [upstream outside-agent documentation](https://github.com/pingdotgg/t3code/blob/main/docs/user/outside-agents.md).

## Validation for this change

- The first-install quickstart, connection-choices comparison, and offline readiness helper were added on 2026-11-10. After the review fixes, `node --test plugins/t3-native/*.test.mjs` passed 90 tests, including 26 readiness tests and one explicit older-server capability-read test.
- The setup-readiness tests cover missing/malformed URLs, unsupported schemes, `localhost` versus hosted, private/tailnet hosts, public domains that merely start with `fc`/`fd`, IPv6 and IPv4-mapped ranges, trailing-dot hostnames, bind/unspecified addresses, multicast/reserved/benchmark/documentation IPv4 ranges, empty query/fragment delimiters, insecure plain-http remotes, `/mcp` path mistakes, credential-like URL parts (with no echo), older-server discovery/consent combinations, unknown inputs, CLI exit codes, and CLI errors that must not echo a URL or secret.
- Independent review reran the native-connector regression tests successfully.
- The setup-readiness helper was tested with offline fixtures; the preflight regression tests use fake connector responses and do not contact T3. Neither helper is installed as an automatic runtime hook. The new hosted consent UI was not exercised and the live server was not upgraded. No live T3 connection, credential, or access change was performed as part of this port.
