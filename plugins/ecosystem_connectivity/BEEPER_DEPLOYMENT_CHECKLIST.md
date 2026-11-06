# Beeper deployment inputs and acceptance checklist

Status: preparation only. No issuer, client registration, OAuth linking, host,
credentials or working deployment is supplied or established by this document.
The adapter is an OAuth resource verifier, not an authorization server. This
checklist describes the adapter's source behavior.

## Explicit configuration template

`examples/beeper-config.template.json` is valid JSON with deliberately unusable
placeholders. Do not run it as deployment configuration. All `.invalid` names,
REPLACE fields, port 1 and the backend path must be replaced with verified
operator-selected values. It contains no real key, subject, chat ID or secret.
Passing construction is not proof a placeholder has been replaced correctly.

The template is the five-field input to `create_secure_readonly_beeper_app`,
not the complete `preflight.py` plan envelope. Use the secure factory, not the
legacy minimal WSGI wrapper. Supply its separate `beeper_token_provider`
callable through approved host integration; it is intentionally absent from
JSON. Do not put bearer tokens, client secrets or private signing keys in this
file, source control, URLs, logs or test fixtures.

## Authorization service and identity contract

- Choose and independently verify an existing trusted authorization service.
  Establish its client registration, consent and authorization-code/PKCE
  interoperability with the intended MCP client. This repository does not
  implement these steps or verify any provider supports them.
- Choose one canonical external HTTPS resource URL. The template example is
  `https://mcp.example.invalid/private/beeper`; replace it consistently. This
  exact string is the expected JWT audience, including path and trailing-slash
  choice. Do not substitute the origin, backend URL or a client identifier.
- Set `issuer` to the exact service-issued JWT `iss`, including any tenant path.
  The configured issuer is also advertised as the authorization server.
  Independently verify its public metadata and actual client interoperability.
- Require signed RS256 access-token JWTs with exact issuer/audience, an unexpired
  `exp`, a stable nonempty `sub`, and the `beeper:read` scope. Do not assume an
  opaque token or an ID token is an acceptable access token. No write scope or
  send tool is enabled. Scope is enforced by the adapter, not a configurable
  field in the five-field template. Subject stability is an issuer/operator
  requirement: the verifier checks the subject value on each request, but does
  not prove that the issuer keeps that identity stable over time.
- Populate `jwks.keys` with authenticated, independently verified public RSA
  signing keys from that issuer. Copy the actual key ID, base64url modulus and
  exponent; do not generate a mock issuer/key as a production shortcut. Use
  only verification public parameters, never private RSA fields. Validate the
  complete plan through the existing offline preflight before host work.
- Map each exact verified `sub` to explicit allowed chat IDs. Scope alone grants
  no chat access. Verify the Beeper account and chat identity before granting
  entitlements. Keep actual subject/chat mappings private; the template is not
  permission to share them publicly.

## Snapshot and revocation limitations

JWKS and chat entitlements are startup snapshots. There is no automatic JWKS
fetch/refresh or live entitlement lookup. Reconstruct/restart the application
with an approved fresh snapshot to change either. Plan and test overlapping
old/new verification keys during issuer rotation, then remove retired keys at
the authorized cutover. Unknown keys must fail closed rather than trigger
untrusted retrieval. Token expiry is checked, but this adapter supplies no
revocation/introspection or refresh-token service. Define emergency key and
entitlement removal procedures before deployment; do not promise immediate
issuer-side revocation will be observed by an unchanged process.

## Independent backend and host requirements

- Verify the intended same-host loopback Beeper route and account separately.
  Do not expose Beeper directly or replace the loopback restriction with a
  public URL. The placeholder backend URL is not a discovery mechanism.
- Provide independently authorized Beeper credentials through the separate
  runtime provider. Never reuse or forward the caller OAuth token upstream.
  Authorization or chat-entitlement denial must not reach that provider.
- Use supported HTTPS termination and a bounded WSGI host. Enforce canonical
  Host/path, trusted scheme handling, request framing and duplicate-header
  rejection before WSGI normalization, body/header limits, finite read/write
  timeouts and worker supervision. WSGI cannot recover erased raw headers.
- Set only explicitly approved browser origins; defaults allow none. Validate
  operational limits and logging redaction. The factory does not start a
  listener, provide TLS, or establish reachability.

## Acceptance gates before claiming a usable integration

1. Offline: validate real public configuration with the preflight, then run
   resource-contract/security tests. Preflight declarations do not verify a
   deployed host or guarantee supplied files contain no secrets.
2. In an authorized deployment, confirm unauthenticated HTTP 401 advertises the
   correct `resource_metadata` URL. For the example path it is
   `https://mcp.example.invalid/.well-known/oauth-protected-resource/private/beeper`.
   Confirm GET metadata advertises the exact resource, issuer, `beeper:read`
   and header-only bearer use. Do not invent an authorization endpoint here.
3. Verify actual client login/consent and issuance separately. Confirm expired
   or wrong issuer/audience tokens get 401, missing scope gets 403 with an
   insufficient-scope challenge, and renewed tokens still obey chat grants.
4. Confirm only `search_chats` and `list_messages` are advertised; perform only
   explicitly authorized read-only live checks. Verify independent upstream
   credentials, no cross-user access, and no sends.
5. Record results before calling linking or deployment complete. Existing
   synthetic signed-token tests prove resource-side behavior only, not these
   live gates. This checklist authorizes none of the live steps itself.
