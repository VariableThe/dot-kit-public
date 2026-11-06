# Read-only composition prototype

`from t3mcp.runtime import build_runtime` provides a non-starting ASGI callable.
It is an offline-tested bootstrap, **not a production-ready service or a deployed
connector**. Construction performs no network request, starts no listener,
creates no credential, reads no environment secret, and writes no journal.

**This is not per-user project isolation.** Every valid authorized reader shares
one configured project allowlist. Do not use one instance for users with different
project permissions; provision separately scoped runtimes with distinct resource
audiences and issuer policy limiting which users can obtain each audience, or
an explicitly trusted per-user authorization gateway. Separate processes alone
do not isolate users whose tokens are accepted by both runtimes.

The host supplies a dictionary with exactly these required fields:

- `resource`: the externally visible HTTPS resource URL, with path `/mcp`.
- `issuer`: the exact externally managed HTTPS authorization-server issuer.
- `jwks`: a pinned public RSA JWKS, including unique nonempty `kid`, `n`, `e`,
  and `kty: RSA`; private key components are rejected. Keys must be at least
  2048 bits. Only RS256 signature-verification keys are accepted.
- `hosts`: a nonempty mapping of stable names to `{url, credential_ref}`.
  Upstream URLs must use HTTPS, or literal `127.0.0.1`/`::1` over HTTP. URLs
  cannot contain credentials, queries, or fragments. These are explicit trusted
  operator destinations, never model-provided URLs.
- `allowed_projects`: a nonempty list of canonical project UUIDs.

Optional `limits` may only tighten the existing policy defaults. Integer caps
must stay integers; booleans, nonfinite values, and unknown keys are rejected.
`allow_writes` may be omitted or exactly `false`; this bootstrap deliberately
rejects enabling writes. It accepts no auth opt-out, HMAC fallback, journal path,
inline token, wildcard host, dynamic endpoint, or ambient credential fallback.
All authenticated users with read scope share this configured project allowlist;
use separately authorized runtimes as described above if users need different
project entitlements.

Pass a trusted callable as the second argument: `credential_provider(ref)`
returns that reference's upstream bearer credential from an already-authorized
runtime store. The provider receives only explicit configuration references,
never incoming bearer tokens or model arguments. Do not paste credentials into
configuration or source. Bootstrap validates availability eagerly and uses a
separate credential for each upstream client. It never forwards an end-user JWT
as an upstream credential. HTTP sessions disable environment proxy and `.netrc`
inheritance; TLS verification remains enabled and redirects are refused by the
existing client. Credentials and public keys are captured at construction;
rotation/revocation changes require closing and rebuilding the runtime. The
provider must already be authorized and must not create access as a side effect.

The returned object is an ASGI callable and has `await runtime.aclose()` for
embedded hosts. Drive lifespan shutdown or call `aclose`; sessions close only
when the underlying worker is drained. A shutdown timeout reports failure and
leaves active sessions intact. Cleanup attempts every session even when one
fails; `aclose` raises a sanitized `RuntimeCloseError` and retains failed sessions
for retry. Lifespan reports `shutdown.failed` on incomplete cleanup. Python threads cannot be killed; configure finite
upstream timeouts and process supervision. No workers or transport requests are
started by construction. One runtime belongs to one event loop.

The bootstrap's transport wrapper requests streaming responses and caps decoded
body bytes at 1,000,000 before the existing T3 client sees text. It rejects oversized
Content-Length, counts streamed chunks, rejects invalid UTF-8, skips error bodies,
and closes responses on all paths. It checks the call budget between chunks.
This wrapper applies only to this bootstrap; direct T3Client users are unchanged.
**Deployment blocker:** requests read timeouts and between-chunk checks are not a
hard wall-clock bound on DNS, response headers, decompression, or an individual
socket read. Header limits and hard I/O deadlines still require the host/gateway
or a separately validated transport. No cancellation can terminate a running
Python thread. Live compression/streaming behavior has not been validated.

## Required external inputs before any live use

1. An actual authorized T3 host and read-scoped upstream credential provider.
   Host reachability, installed tool schemas, versions, and permissions still
   need explicit live validation. This code does not configure the host.
2. A real authorization server issuing RS256 access tokens with exact issuer,
   resource audience, subject, expiry, and `t3:read` scope. Client registration,
   OAuth consent/redirect policy, revocation, authorization-server metadata and
   JWKS discovery/refresh are not implemented here. Pin and review public keys
   out of band; this module never follows a URL found in a token.
3. Gateway routing for `/mcp` and the existing protected-resource metadata path
   `/.well-known/oauth-protected-resource`. This metadata advertises the issuer;
   it does not implement an authorization/token endpoint or establish linking.
4. A trusted HTTPS gateway with finite connections, header/body buffers, timeouts,
   duplicate-header fidelity, correct origin handling, and process supervision.
   Use the existing ASGI lifecycle integration. Merely constructing this callable
   supplies none of TLS, DNS, access routing, firewall policy, or public exposure.
5. Deployment review, dependency installation through the host's approved
   packaging workflow, and the full offline suite before live integration.

## Verification limits

Seventeen new offline tests exercise actual synthetic RS256 JWT verification,
policy/context/ASGI/dispatch composition, real T3Client over fake streaming HTTP,
read-only and unknown-host denial, expired/missing-scope tokens, duplicate JWT
members and unsupported JOSE critical headers, weak/duplicate key rejection,
provider separation/failure, close-all retries and lifespan cleanup failure,
streamed byte limits, invalid UTF-8, response closure, and stream-budget failure.
The auth parser also rejects nonstandard JSON constants and unsupported `b64`.
No test contacts a network. The local runner stubs the missing `requests` import;
most tests replace the upstream client, while one composes the actual client with
a fake HTTP session. The dependency-backed pytest suite and live OAuth/HTTP/T3
compatibility have not been run here. No endpoint, credential, installation, or
deployment was created.

For shared deployment prerequisites and live acceptance/rollback gates, see
[Read-only launch gates](../LAUNCH_READINESS.md).
