# Beeper secure HTTP composition

Use `create_secure_readonly_beeper_app` from `beeper_bootstrap`, with the same
explicit config and independent backend credential provider as the original
bootstrap. Supply an explicit `allowed_origins` collection for browser clients;
native clients may omit Origin. Construction opens no listener or connection.
The legacy `create_readonly_beeper_app` remains available for existing tests but
is not the recommended HTTP deployment boundary.

The new WSGI callable provides:

- Path-qualified RFC 9728 metadata at `/.well-known/oauth-protected-resource`
  followed by the resource path, with exact resource, issuer and read scope.
- 401 Bearer challenges pointing to that metadata; invalid-token and
  insufficient-scope challenges use OAuth error fields. Entitlement denial
  stays separate from missing OAuth scope.
- Exact HTTPS scheme, Host and resource path checks; explicit Origin allowlist;
  no query-string credentials, proxy-header trust, CORS or browser preflight.
- Strict content length, no transfer encoding, bounded request/header/response
  bytes, JSON object/duplicate-key/nonfinite-value rejection, and finite
  concurrent admission with no waiting queue. Saturation returns 503.
- JSON-only stateless March 2025 Streamable HTTP. Initialization can negotiate
  down to `2025-03-26`; unsupported explicit protocol headers are rejected.
  MCP GET returns 405 because this server offers no SSE stream.
- Fresh per-call readers, verified subject chat entitlements, separate backend
  credentials, and explicit rejection if a provider returns the caller token.
  No sending or mutation tools are added.

## Mandatory hosting work, still not supplied

A production WSGI gateway must reject duplicate headers before normalization,
terminate TLS, enforce finite header/body/socket read and write timeouts and
connection limits, and supply the original public Host and HTTPS scheme without
trusting client Forwarded headers. WSGI cannot enforce those transport properties.
The admission slot remains held during a blocked body read/backend operation;
the gateway must bound these operations and terminate stuck workers. Response
size enforcement is after serialization; host memory limits are still required.
Use bounded workers/process supervision and avoid debug/error logging of tokens
or chat contents. No public server, certificate, ingress or gateway is installed.

The authorization server must already exist and implement supported OAuth flows,
metadata and client registration. Configure its exact issuer and trusted public
JWKS snapshot, approved HTTPS resource audience, subject chat entitlement mapping,
and account-bound backend token provider. No credential or OAuth grant is created.
Public keys and entitlement changes require rebuilding the application. Selecting
hosting, registration, permission grants and live validation remain separate steps.
Installed Beeper tool compatibility is still unverified.

## Offline evidence

`python -m unittest discover -s tests -v` passes 20 tests in the isolated snapshot:
8 prior bootstrap/proxy tests plus 12 new HTTP tests. These cover synthetic RS256
caller authentication, metadata/challenges, issuer/audience/expiry/scope failures,
initialize/list/notification, real read-only reader against a fake transport,
per-user backend credentials/chat access, caller-token nonforwarding, routing,
Origin/framing/JSON/size rejection (including overflowing float literals),
notification HTTP rejection without JSON-RPC response, and overload/recovery. Socket construction is
prohibited in the new tests. No full repository suite or live integration ran;
pytest and requests remain unavailable and no dependency was installed.

Reference contracts:
- https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- https://modelcontextprotocol.io/specification/2025-06-18/basic/transports

For shared deployment prerequisites and live acceptance/rollback gates, see
[Read-only launch gates](LAUNCH_READINESS.md).
