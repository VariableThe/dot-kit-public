# Tool OAuth metadata and transport authentication

Both Beeper read tools declare OAuth2 with `beeper:read` in `securitySchemes`
and its `_meta.securitySchemes` compatibility mirror. Neither advertises
`noauth`. Annotations mark the surface read-only, non-destructive, idempotent
and bounded to a private account (`openWorldHint: false`). These are client
hints, not authorization. Existing issuer/audience/expiry/scope checks and
per-subject chat entitlements remain authoritative.

## Preserve the HTTP boundary

This adapter protects the entire MCP HTTP resource. Missing/invalid/expired
caller credentials remain HTTP 401 with protected-resource metadata challenges;
insufficient scope remains HTTP 403 with `insufficient_scope` and `beeper:read`.
Chat entitlement denial is not an invitation to obtain broader OAuth scopes.
No authentication or chat-entitlement denial reaches the backend credential provider.

OpenAI also documents tool-level OAuth: an MCP error tool result with
`isError: true` and `_meta["mcp/www_authenticate"]`, whose challenge includes
`error` and `error_description`. That is a distinct response path from the
resource-server HTTP denial. We deliberately do not convert HTTP auth failures
into HTTP-200 tool results, add misleading result metadata to JSON-RPC errors,
or expose anonymous tools/list. There is currently no separately authenticated
per-tool authorization layer where that result path is needed. Backend token
failure is not evidence that caller OAuth consent is missing and must not
trigger caller reauthorization.

This patch therefore adds descriptors and tests, not a new tool-level OAuth
flow. It makes no claim that ChatGPT's tool-level linking UI is exercised or
that descriptor metadata alone establishes a working connection. Reauthorization
is tested as a new synthetic valid token after expired/insufficient-scope tokens,
not as browser login, refresh-token exchange, or an issued OAuth grant.

## Offline validation

25 focused unittest cases pass: 20 current bootstrap/HTTP/proxy tests plus five
new descriptor/auth regressions. They check matching independent descriptor
metadata, expired/missing/wrong-audience denial, insufficient-scope challenges,
entitlement denial without reauth, and renewed authorization with subject/chat
binding. Socket construction is blocked. Full repository tests and live OAuth
UI/backend tests were not run; no dependencies, credentials or services created.

Sources checked for this change:
- https://developers.openai.com/plugins/reference
- https://developers.openai.com/plugins/build/auth
- https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
