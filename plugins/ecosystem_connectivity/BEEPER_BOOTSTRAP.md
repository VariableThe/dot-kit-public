# Read-only Beeper composition

`beeper_bootstrap.create_secure_readonly_beeper_app(config, beeper_token_provider=...)`
is the recommended HTTP composition entrypoint and returns a WSGI application.
See [BEEPER_HTTP.md](BEEPER_HTTP.md) for its routing and transport contract.
The legacy `create_readonly_beeper_app` remains for compatibility and tests.
Neither factory opens a listener, connects to Beeper, reads credentials,
registers OAuth clients, or mints access tokens during construction.

The explicit config must contain exactly:

- `resource`: the approved public HTTPS MCP resource URL.
- `issuer`: the trusted external authorization server's exact HTTPS issuer.
- `jwks`: that issuer's pinned RS256 public verification keys. Private key
  fields and signing/encryption key usage are rejected. No automatic JWKS
  fetching or rotation occurs; rebuild the application after rotation.
- `chat_entitlements`: an explicit subject-to-chat-ID list mapping. The
  startup snapshot is immutable per subject and has no wildcard behavior.
- `beeper_url`: an explicitly selected loopback HTTP(S) Beeper MCP URL,
  without embedded credentials, query strings, or fragments.

Supply a trusted independent `beeper_token_provider(caller)` callback.
It must return the authorized backend credential for that verified caller,
not a caller's OAuth access token. The factory never creates or stores one.
This callback remains deployment-specific and must preserve account binding.

The factory uses the existing RSA JWT verifier, `JwtCallerVerifier`, fresh
`ReadOnlyBeeperMCP` clients, and a bounded WSGI wrapper. T3 package
exports load lazily so this Beeper-only import does not require requests.
RSA verification requires the existing cryptography dependency and public keys
of at least 2048 bits. The shared verifier accepts a string audience or an array
containing only strings, with an exact configured-resource match. These checks
do not establish issuer ownership or public-key provenance.

The Beeper bootstrap currently uses the shared verifier's default 30-second
clock leeway for both expiry and not-before checks. It rejects expiry when the
current time is greater than `exp + 30`; this includes acceptance at the exact
boundary. This patch does not change that policy. The T3 runtime separately
configures zero leeway. Neither path implements token revocation or key refresh;
operators must validate issuer/key provenance and rebuild after rotation.

This is composition, not a production deployment. The host still must supply
TLS, canonical routing metadata, duplicate-header
rejection before WSGI normalization, finite transport timeouts, bounded workers
and process supervision. The secure factory supplies protected-resource discovery,
Bearer challenges, exact Host/path/scheme checks, and Origin/protocol validation;
the legacy factory does not supply that complete HTTP boundary. Neither factory
implements an authorization server or establishes a ChatGPT connection.
Live backend tool compatibility remains unverified. No credentials or live
endpoint values are provided by this patch.

T3 has a separate non-starting ASGI bootstrap,
`t3mcp.runtime.build_runtime(config, credential_provider)`, documented in
[t3mcp_adapter/RUNTIME.md](t3mcp_adapter/RUNTIME.md). It composes policy,
`ToolContext`, and ASGI dispatch, rejects writes/auth opt-out/HS256, and still
requires explicit hosts, project allowlists, independent credentials, an external
issuer and managed hosting. Its project allowlist is shared by all authenticated
readers; it is not per-user project isolation. Separate deployments require
distinct audiences and issuer-side user authorization, or a trusted per-user
gateway; separate processes alone are insufficient. No T3 host or credential
is configured by either Beeper factory.

See [LAUNCH_READINESS.md](LAUNCH_READINESS.md) for the remaining deployment inputs
and live acceptance gates. Both bootstraps are prototypes, not production-ready
services or deployed connectors.

Historical focused validation (legacy bootstrap):
`python -m unittest discover -s tests -v` ran eight bootstrap/transport tests, including synthetic RSA-signed caller verification and a
socket prohibition during startup/tool listing and backend reads. Tests also
exercise alternating caller isolation, startup entitlement snapshots, and the
actual urllib handler chain under ambient proxy settings. Default transports
explicitly disable ambient proxies so loopback bearer tokens stay loopback. This is not a full suite or
live integration test. Full T3 lazy-export regression tests remain unrun. pytest and requests were unavailable in the scratch
environment; no packages were installed.
