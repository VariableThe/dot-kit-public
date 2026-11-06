# Read-only launch gates

These are deployment prerequisites, not evidence of a deployed or production-ready
plugin. Constructing either application starts no listener and creates no grant.
Keep credentials, private data, and machine-specific configuration out of Git.

## Existing direct T3 connector

These gates apply to the custom adapters in this repository, not to any already working direct connector. No evidence links an installed connector's implementation to this repository's custom T3 adapter.

## Compose the intended entrypoint

- Beeper: `beeper_bootstrap.create_secure_readonly_beeper_app(config,
  beeper_token_provider=...)`; see [BEEPER_HTTP.md](BEEPER_HTTP.md). Supply the
  approved HTTPS resource, exact external issuer, pinned public RS256 JWKS,
  subject-to-chat-ID entitlements, selected loopback Beeper MCP URL, and an
  independent account-bound backend credential provider. Configure permitted
  browser Origins explicitly when needed. Do not deploy the legacy factory as
  though it supplied the secure HTTP boundary.
- T3: `t3mcp.runtime.build_runtime(config, credential_provider)`; see
  [RUNTIME.md](t3mcp_adapter/RUNTIME.md). Supply the HTTPS `/mcp` resource, exact
  external issuer, public RS256 JWKS, stable host names with explicit upstream
  URLs and credential references, and canonical project UUID allowlist. Drive
  ASGI lifespan shutdown or await `runtime.aclose()`.
- T3's project allowlist applies to **every authenticated reader**. Use separate
  scoped runtimes for users with different project permissions, with distinct
  resource audiences and issuer policy restricting which users can obtain tokens
  for each audience, or an explicitly trusted per-user authorization gateway.
  Separate processes alone do not isolate users sharing an accepted audience.
  The bootstrap rejects `allow_writes=true`; enabling writes requires separate reviewed work.
- Supply runtime dependencies and a managed WSGI/ASGI server through the selected
  host's approved packaging workflow. Neither factory supplies the server,
  credential store/provider implementation, OAuth issuer, or process supervisor.

## Select and approve the deployment inputs

1. Verify the intended machine, backend account, installed version and endpoint.
   Beeper Desktop must be running; its documented default is
   `http://localhost:23373/v0/mcp`. Verify the actual installed endpoint rather
   than assuming the default. T3 needs a credential accepted by its `/mcp`
   audience with read access; a relay token or generic session is not evidence
   of that access.
2. Supply an external authorization server with public discovery, authorization
   code + PKCE S256, supported client registration or a predefined client, exact
   callback allowlisting, and resource-bound RS256 tokens with subject, expiry
   and the appropriate `beeper:read` or `t3:read` scope. Plan refresh/revocation.
   The adapters' discovery documents advertise this issuer; they do not create it.
3. Select managed HTTPS ingress or evaluate a private Secure MCP Tunnel. Public
   ingress must terminate TLS, enforce canonical Host/scheme/path, reject
   duplicate headers before normalization, and bound connections, buffering,
   memory and I/O time. Do not trust client-supplied forwarding headers.
   T3's worker threads cannot be killed on timeout; a supervisor and finite
   upstream deadlines remain necessary.
4. A Secure MCP Tunnel needs an approved tunnel identity, runtime API key,
   Platform permissions, target workspace association, a running official
   tunnel-client, outbound HTTPS and local MCP reachability. Validate forwarding
   against the adapters' exact routing/resource requirements; do not relax auth
   or routing checks to make it connect. Private tunnels are not a public plugin
   distribution endpoint. The tunnel does not automatically carry the browser-facing
   OAuth flow; the authorization server must be reachable by the required browser
   and token-exchange participants.
5. Obtain specific approval before creating/configuring persistent credentials
   or OAuth grants, expanding access, changing network security, installing or
   starting services, or exposing endpoints. Agree the host, provider, audience,
   allowed data and test scope first. Source publication does not authorize
   deployment or agent/message writes. Never paste secrets into a runbook.

## Evidence required before claiming usable

First run the complete dependency-backed offline suites against the selected
commit. Historical focused passes and stubbed imports are not a full-suite pass.
Socket-free tests can prove synthetic JWT validation, dispatch, fake upstream
protocol/shape handling, scope and allowlist denials, credential separation,
limits, overload recovery and lifecycle behavior. They cannot prove installed
backend compatibility, real OAuth, gateway behavior or ChatGPT linking.

Then record the version/commit, checks and sanitized outcomes for an authorized
live test:

- Check each upstream's initialize, initialized notification and tools/list.
  Beeper must advertise compatible `search_chats` and `list_messages` schemas;
  verify actual result shapes and pagination. For T3 verify the capabilities,
  project-list, thread-list/read/wait tools the adapter invokes. Test read-scoped
  credentials, not broad credentials merely assumed to work.
- Through the actual gateway/tunnel, verify protected-resource discovery and
  Bearer challenges, complete OAuth in the intended ChatGPT workspace, inspect
  the discovered read-only tool list, and perform one approved chat read and one
  approved project/thread read, including pagination and bounded wait.
- At the adapter boundary, confirm denial of disallowed chats/projects and
  mutation tool names without invoking upstream mutations; reject
  wrong issuer/audience, expired tokens and missing scope. Check overload and
  restart recovery without exposing credentials or private content in logs.
- Verify credential/key rotation and reconnection. Public keys and T3 upstream
  credentials are captured at construction, so rebuild the runtime when changed.
  Refresh ChatGPT tool metadata after relevant changes and retest in a new chat.

Keep rollback explicit: disconnect the test plugin, stop its new ingress/tunnel
and service, revoke newly granted access through the authorized flow, restore
the prior configuration/version, and verify the old token is denied while
pre-existing local integrations still work. Revoking OAuth consent alone may not
invalidate an already-issued JWT: disable ingress immediately and verify expiry,
an enforced revocation mechanism, or approved key replacement/runtime rebuild
before asserting that old tokens are unusable. Drain T3 workers; a shutdown timeout
is not successful cleanup. Do not revoke unrelated existing credentials.

## Primary references

- [OpenAI authentication](https://developers.openai.com/plugins/build/auth)
- [Connect and test](https://developers.openai.com/plugins/deploy/connect-chatgpt)
- [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [Beeper Desktop MCP](https://developers.beeper.com/desktop-api/mcp/)
- [T3 environment authentication](https://github.com/pingdotgg/t3code/blob/main/docs/internals/environment-auth.md)

Upstream documentation evolves. Confirm the installed versions and current
account/workspace capabilities at deployment time.
