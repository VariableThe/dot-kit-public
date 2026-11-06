# Offline Beeper OAuth resource contract

The `tests/test_beeper_oauth_contract.py` regressions join previously separate
checks into a client-visible sequence: missing-token HTTP 401 challenge,
advertised protected-resource metadata discovery, insufficient-scope HTTP 403,
expired-token HTTP 401, renewed signed fixture verification, tool OAuth
security descriptors, and a successful entitled read through the real facade
and read-only client with a fake backend transport.

Path-bearing issuer and audience identifiers are compared exactly. Origin-only
claims fail. A valid renewed token cannot bypass chat entitlements. No
authentication or chat-entitlement denial reaches the backend credential
provider. Successful upstream calls use only the independent synthetic backend
credential, never the caller bearer token. Socket construction is prohibited
by the test fixture; no listener or authorization server is started.

These are resource-server contract tests, not an OAuth authorization-code/PKCE
flow, registration, consent, refresh-token, key-rotation, TLS, browser, or
ChatGPT account-linking test. Signed tokens are generated locally solely as
fixtures. Public JWKS, issuer, hosting and entitlement configuration remain
operator-supplied production requirements. There is no production issuer or
credential setup in this patch. HTTP authentication failures intentionally stay
HTTP challenges rather than becoming tool-result reauthentication messages.

At base `5b21f468c76aa5345fc504e196b81d13c39068ab`, the two new contract tests
and 19 existing HTTP/header/tool-auth tests pass (21 total), with Python
compilation passing. This does not claim the full repository suite or live
Beeper interoperability was tested.
