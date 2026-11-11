# Connection choices compared: transport, discovery, and consent

Status: source and documentation review on **2026-11-10**. This is a comparison for planning, not a deployment record. The hosted consent screen was not exercised during this review, and the current client's original approval ceremony was not revalidated. For first-install steps, use the [quickstart](QUICKSTART.md).

## Bottom line

- There is **one native transport**: T3 Code's MCP server at `/mcp`, using OAuth, dynamic client registration, PKCE, and a bearer MCP token. This repository does not add another transport.
- **Transport, URL discovery, and consent page are separate axes.** Discovery is manual **Copy MCP URL** or the official wizard. Consent is the environment-local `/connect-agent` page or the hosted `/approve-agent` page. The wizard does not choose between those consent pages; the server's routing conditions do.
- The **current established client is a native OAuth client**, not a `/connect-agent` path. Its original approval ceremony was not revalidated here. A current working grant does not need to be reclassified or migrated.
- On a currently linked tunnel, a **new sign-in using a manually copied identical URL** can also redirect to hosted `/approve-agent`. Conversely, a wizard-started sign-in can land on the environment page when the server's conditions are not met.
- **Rerunning the official wizard at the same endpoint is reauthorization, not switching transports.** Manual URL setup was already an official path. Neither path changes the MCP endpoint or token model.
- A healthy, already-paired client does **not** need migration. The reviewed source does not rewrite an existing grant when the new UI appears. Re-approval is an optional, separately authorized action when it is needed or deliberately requested.
- The current hosted cloud connector **already uses T3 Connect for transport**. Hosted consent adds a T3 Connect account/hosted-approval dependence at sign-in; it does not add a new tunnel for ongoing MCP calls. Local/tailnet reachability is a separate comparison.
- The canceled experimental adapter is a different thing entirely. It is not a third supported connection path.

## Three axes, not two bundled paths

| Axis | Options | What it changes |
| --- | --- | --- |
| Transport | Native T3 Code MCP server at `/mcp`, with OAuth and a bearer MCP token. | Nothing in this comparison. There is no supported second transport here. |
| URL discovery | Manual **Settings → Environments → Copy MCP URL**, or the command-palette/sharing wizard. | How a person finds the endpoint and app-specific instructions. Both can point at the same endpoint. |
| Consent page | Environment-local `/connect-agent`, or hosted `/approve-agent`. | Where and how a **new** sign-in is approved. The server's link, issuer-host, and peer-link conditions choose this; the wizard does not. |

The axes are independent as choices, but a given sign-in still lands on one consent page according to server routing. A manually copied URL is not permanently tied to the environment page, and a wizard URL is not permanently tied to the hosted page.

## What stays the same

- **Endpoint:** the native MCP route is `/mcp`. Manual discovery and the wizard can produce the same URL; the wizard may prefer the T3 Connect address when one is available.
- **OAuth shape:** public client with dynamic registration, no client secret; authorization-code flow with PKCE `S256`; `state`; resource/audience checks.
- **Redirect validation:** HTTPS redirect URIs match exactly. HTTP loopback redirects ignore the port under RFC 8252 §7.3; the remaining components still must match. This is narrower and more precise than saying every redirect is exact.
- **Code and token lifetimes:** the OAuth authorization code lives **60 seconds** and is spent on presentation, even if a later exchange check fails. The host-issued setup/approval code lives **5 minutes** and works once. The resulting MCP token is a **30-day bearer token**.
- **Session mode storage:** `issueMcpClientSession` persists both the issued scopes and the selected `runtimeModeCeiling` (absent for read-only). `authenticateMcpClient` reports read-only when `orchestration:operate` is absent; otherwise it reports the stored ceiling, falling back to `approval-required` if no ceiling was stored. Intermediate levels share the same read/operate scopes and are distinguished by the stored ceiling, not by scopes alone. A managed policy can impose a lower ceiling.
- **No refresh token:** the token endpoint supports `authorization_code` only. After 30 days, or after revocation, the client signs in again.
- **Revocation:** approved clients appear under **Settings → Sharing & access** and can be revoked there. Revocation is the control for an already-issued token, regardless of which approval page created it.

## Manual discovery vs wizard

| Area | Manual URL discovery | Official wizard |
| --- | --- | --- |
| What it is | Copy the MCP URL from Settings, add it to the client, and approve the new sign-in. | Command palette or **Settings → Sharing & access**; app-specific setup and route checks. The ChatGPT tile copies the URL; it is not a marketplace install. |
| Endpoint | The environment's `/mcp` URL. | The same `/mcp` endpoint, often the T3 Connect URL when available. |
| Consent choice | Not selected by the discovery method. Server routing decides for a new sign-in. | Not selected by the wizard. The same server routing decides. |
| Existing grant | No automatic rewrite or re-approval. | No automatic rewrite or re-approval. Running the wizard again at the same endpoint is reauthorization. |
| Support status | Official. | Official. |

## Environment-local vs hosted consent

| Area | Environment-local `/connect-agent` | Hosted `/approve-agent` |
| --- | --- | --- |
| When it is used | For peer links and sign-ins that do not meet the hosted routing conditions. If a hosted page loads and offers **Use a sign-in code instead**, use that supported link to reach the environment page; re-copying the same URL is not a bypass. | Only for a linked, not-revoked environment whose issuer hostname equals the tunnel hostname and whose client is not a peer link. These conditions come from `mcpOAuthHttp.ts`. |
| Proof used to approve | A host-issued one-time code, or an eligible browser session on the environment. | A T3 Connect account session, sent through the hosted app/relay. The server authenticates the actual HTTP request rather than relying on browser cookies alone. |
| Account requirement | No T3 Connect account is required for a local/tailnet environment-local approval. | T3 Connect account, a linked environment, and the hosted app are required for the no-code path. |
| Code round-trip | A code may be required; the requesting app does not provide it. | No code in the hosted no-code path. When the hosted page offers **Use a sign-in code instead**, that is the supported route to the environment page. |
| Transport | The same native `/mcp` OAuth transport. | The same native `/mcp` OAuth transport. The current hosted connector already uses T3 Connect for that transport. |

## Shared protections

Both consent pages use the same OAuth implementation and token issuance. In particular:

- Dynamic registration issues a signed public client identity; redirect URIs are checked with the HTTPS-exact / HTTP-loopback-port exception above.
- PKCE `S256` is mandatory. A code is bound to the client, redirect URI, resource, and PKCE challenge.
- The authorization code is short-lived and one-use. The setup/pairing code is one-use and short-lived.
- MCP tokens are restricted to `/mcp`; a client token cannot call the general HTTP API or WebSocket RPC.
- OAuth responses are marked `no-store`. The consent UI refuses to render when framed, and the environment error page uses a restrictive content-security policy with `frame-ancestors 'none'`. This is not a page that another site can silently embed and clickjack.

## Preserved approval guards and what actually changed

The approval endpoint authenticates the actual HTTP request and requires a session holding both `access:write` and `orchestration:read`; a session cannot hand out scopes it does not hold. The hosted no-code path can grant only levels whose scopes the approving session holds, and a code path checks the code's scopes separately. These guards were preserved, not newly introduced by #33632.

What #33632 broadens is the set of approving-session credentials the hosted app can use: bearer/DPoP-authenticated requests, including fresh per-call credentials, can authenticate the approval request. #33645 then binds the hosted CSRF token to the DPoP key thumbprint so successive calls for the same browser key can approve without letting a different proof key reuse that approval. Treat those as hosted-authentication changes, not as a new scope-guard design.

Hosted routing remains deliberately narrow: linked, not revoked, matching tunnel hostname, and not a peer link. Peer links stay on the environment page; a level-bound app approval code is refused, so use a plain pairing code.

## Security comparison

### What does not change

- **The MCP token is still a bearer token.** The hosted browser's DPoP session authenticates the approval request. It does **not** bind the client's later MCP bearer token to that browser key. Anyone who obtains the bearer token can use it until expiry or revocation. Do not describe the hosted flow as making the resulting MCP token DPoP-bound.
- **The trust boundary moves; it does not disappear.** The hosted consent path trusts a T3 Connect account session and the hosted app/relay at sign-in. The environment-local path trusts a browser session on the environment or a host-issued code. Neither is universally safer; they fail differently.
- **A newer change is not automatically a safer change.** Hosted consent adds account and hosted-approval dependencies at sign-in. The current hosted connector already depends on T3 Connect for transport, so this is not a newly added tunnel for ongoing calls.

### Limits

- **No plan or distribution guarantee.** A successful OAuth sign-in does not prove that every ChatGPT plan, workspace policy, or Enterprise app-distribution policy supports the connection. Do not broaden workspace sharing to work around policy.
- **No local exercise of the hosted UI.** This review did not approve a live agent, create a grant, or test the hosted page; its behavior is source-derived until a disposable-install test is authorized.
- **The local/tailnet alternative is separate.** A local/tailnet client may avoid T3 Connect for transport, but that is a reachability choice, not a property of the consent-page comparison.

## Reliability, support, and operations

- **Current established client:** no automatic update, no forced re-approval, and no new security property for the token already held. The reviewed source contains no migration that rewrites an existing grant. The server retaining an old session does not prove the client still retains or can select that credential.
- **Manual discovery with environment-local consent:** fewer hosted-approval dependencies for a local/tailnet client, but it depends on the environment being reachable over an allowed `https` route and on obtaining/entering a code at the right time.
- **Hosted consent:** removes the code round-trip for the common hosted case and gives better app-specific setup. It adds T3 Connect account state, the hosted app, and an approval-time relay dependency. If the hosted page loads and offers **Use a sign-in code instead**, use that supported link to the environment page. Re-copying the same linked tunnel URL is not a bypass. If the hosted page cannot load and no supported environment-page fallback is reachable, stop and diagnose or wait; do not rewrite the OAuth URL, switch accounts/endpoints, or change service/tunnel state automatically. An authorized local/other route is a separate choice that must be verified, not guaranteed rollback for the hosted client.
- **Support status:** the native connector, manual **Copy MCP URL** flow, environment-local approval page, pairing codes, and wizard are official. The experimental adapter is not a supported alternative.
- **Audit and revocation:** both consent paths end at the same client list. Revoke the exact client there; do not rely on deleting a local note or forgetting a code. Revocation is a separate action, not cleanup that should happen automatically after a failed attempt.

## Gains for an already-paired client

There is no immediate transport or token-security upgrade. The practical gains appear only when a new sign-in is needed:

- The wizard is easier to find than a manually copied URL.
- On a linked environment, the next approval can avoid the code round-trip.
- The #33645 fix makes hosted re-approval work across per-call DPoP credentials.
- The consent page is clearer about app, environment, and access level.

If the current client reads and controls threads as intended, keep it. Reconnecting solely to try the new page creates a new credential and a new failure window without improving the existing token.

## Optional confirmed re-approval (not migration)

> **Do not execute any step in this section now.** It is an optional path for a later, explicit owner request. Every new credential creation, approval, and revocation needs its own per-action confirmation. No grant, reconnect, revocation, install, restart, or migration is authorized by this document.

There is no demonstrated security need to redo a healthy current grant. If the owner wants to revalidate the setup through a new ceremony, this is the requested path; it should preserve the old connection where the client supports that and stop if it does not.

### Preconditions

- Explicit owner confirmation naming the app, the independently verified environment ID, the intended access level/mode, and the purpose. Do not accept a similar hostname or a device selector as the environment identity.
- A maintenance window with no active thread runs that matter.
- The intended environment and current grant verified separately, without copying tokens or one-time codes into notes, chat, or repository files.
- A check, with the owner's approval, of whether the client can keep and select two separately named connections/credentials at the same time. Do not assume it can. Server retention of an old session is not proof that the client still holds or can select its token.
- For hosted consent: T3 Connect linked to the intended environment, not revoked, and the issuer host matching the tunnel hostname. The consent route is still determined by server conditions, not by whether the wizard was used.
- A rollback path only while a usable old client connection remains. Once the old credential is replaced or revoked, a failed new approval may require new authentication; there is no guaranteed old fallback.
- **New credential creation needs separate per-action approval. Revocation is a separate scoped confirmation, not automatic cleanup after a failure.**

### Approval checklist: record the grant data

Before approving, write down and verify:

- App/client name and redirect host shown by the consent page.
- The independently verified environment ID and environment host.
- Expected consent page: environment-local or hosted, and why the server conditions should choose it.
- Requested access level, resulting scopes, and the stored `runtimeModeCeiling` (read-only has no ceiling; intermediate levels share scopes and are distinguished by the ceiling).
- Intended purpose and why that is the least access needed.
- Whether an old grant will remain; its exact client label, app, environment, access, and expiry.
- Whether the client can show a second, separately named connection and let the owner select it.
- Who will revoke what, and whether the owner has separately authorized that revocation.

### Steps, only after confirmation

1. **Preserve the current connection if the client supports coexistence.** Inventory the current client label, app, environment, access, and expiry without recording secrets. Identify the exact old grant; do not revoke a grant merely because it looks old or unrelated.
2. **Verify client semantics before creating anything.** If the client can add and select a separately named connection, proceed. If it can only replace the current one, stop before the destructive step and obtain a separate confirmation; do not assume two credentials can coexist.
3. **Start the new sign-in, using the same endpoint.** The official wizard is fine, but at the same endpoint it is reauthorization, not a transport switch. A manually copied identical URL can also route to hosted consent on a linked tunnel.
4. **Confirm the consent page.** It must name the intended app, environment, and access level. If the account, environment, or access is wrong, stop. Do not approve to “see what happens.”
5. **Approve the least required level.** Do not select a higher mode to make the test pass. The mode is a ceiling for threads the client starts or steers.
6. **Verify reads first on the separately named new connection.** Confirm the client lists it and can read the environment identity and a project page. Do not claim write execution from reads.
7. **Test writes only under separate authorization.** If needed, use a disposable or explicitly authorized task. Do not launch a live test thread as part of a documentation change.
8. **Decide about the old grant only after verification, and only with separate confirmation.** If coexistence is supported and the new grant is verified, the owner may keep both or authorize revoking the exact old grant. If the old grant's identity is unclear, stop rather than revoke a possibly unrelated client.

### Rollback and failure handling

- **Guaranteed rollback exists only while a usable old client connection remains.** Keep it until the new path is verified. The server may retain an old session after the client has replaced its local credential; that is not a usable rollback for the client.
- **If the new approval fails or names the wrong account/environment:** cancel, keep the old connection, and do not treat the partial new grant as automatically cleaned up. A partial new grant may need revocation, but that is a separate scoped confirmation.
- **If the client replaced the old credential and the new one later fails:** reauthenticate through the wizard. If the hosted page loads, use the offered **Use a sign-in code instead** link when you need the environment page. Re-copying the same linked tunnel URL is not a bypass. If the hosted page cannot load and no supported environment-page fallback is reachable, stop and diagnose or wait.
- **If the hosted page is unavailable:** do not re-copy the same URL expecting a different consent page, and do not rewrite the OAuth URL, switch accounts/endpoints, or change service/tunnel state automatically. Use a supported environment-page fallback only if one is reachable; otherwise stop and diagnose or wait. An authorized local/other route is a separate choice that must be verified, not guaranteed rollback for the hosted client.
- **If policy denies the requested level:** stop. Do not reapprove, broaden sharing, or change policy to bypass it.

### Risks to accept explicitly

- The client may not support two credentials or two separately named connections; verifying that is a prerequisite, not an assumption.
- Wrong account, wrong environment, or over-granted access at the consent page.
- Hosted approval depends on the T3 Connect account, hosted app, and relay being available at sign-in.
- The MCP token is a 30-day bearer credential with no refresh token; client-side storage matters.
- A managed-policy ceiling lower than the selected level.
- Revocation interrupts access immediately and may interrupt active work.
- A partial or unused new grant remains until separately revoked; revocation is not automatic cleanup.

### Stop conditions

Stop and ask the owner if: the consent page names the wrong app/environment/account; an explicit policy or permission denial appears; the required level is not offered; the environment is not the verified target; the client cannot preserve/select a separate connection; the old grant's identity is unclear; active runs cannot be drained; or no usable rollback connection can be kept. Do not revoke a possibly unrelated grant and do not broaden sharing to force the flow through.

## Recommendation

1. **Keep a healthy current connection.** There is no demonstrated security need to redo it, and the server retaining a session does not prove the client retains its credential.
2. **If the owner wants revalidation, use the optional confirmed re-approval path above.** Preserve the old connection where supported, validate a separately named new connection only with approval, and stop before a destructive replace/revoke when coexistence is unavailable.
3. **Manual discovery and the wizard are both official.** Rerunning the wizard at the same endpoint is reauthorization, not a transport change; the consent page is chosen by server conditions.
4. **New install, linked environment, hosted consent available:** use the wizard and hosted approval for the simplest path. Choose the least access the client actually needs.
5. **New install, no wizard/linked account/hosted page:** use **Copy MCP URL** only as URL discovery when the server's conditions actually select the environment page. If the hosted page loads and offers **Use a sign-in code instead**, use that supported link. Re-copying the same linked tunnel URL is not a bypass; if no supported environment-page fallback is reachable, stop and diagnose or wait.
6. **Never reapprove to bypass an explicit denial.** Distinguish missing/expired/revoked authentication from `permission_denied`, `forbidden`, or a policy limit. Approval is not an override.
7. **Do not repeat OAuth to fix a missing tool or schema mismatch.** Check client/server compatibility and update; credential churn will not add a tool.

## Precise limitations

- The comparison is based on upstream source and docs read on 2026-11-10, not a reproduced build of the installed binary. Source pins and version observations are dated provenance, not installation prerequisites.
- The hosted page is offered only under the narrow cloud-link conditions above. It is not a universal replacement for the environment page.
- Apps that require their own client ID cannot use T3's dynamic registration.
- Peer links always stay on the environment page and reject level-bound app approval codes; use a plain pairing code.
- A managed policy can cap the selected runtime mode; the consent page should not be used to route around it.
- Tokens last 30 days, have no refresh grant, and are bearer credentials held by the client.
- This repository adds no OAuth transport, relay, token storage, or automatic consent. `setup-readiness.mjs` is an offline shape classifier; `connection-preflight.mjs` performs three live read-only reads through a caller-provided connector and does not write. The real UI performs approval.
- The hosted flow was not exercised live during this review. Treat its behavior as source-derived until a disposable-install test is authorized.

## Sources

Checked 2026-11-10 against upstream `main` (not the installed binary):

- Outside-agent setup, approval, and code lifetimes: <https://github.com/pingdotgg/t3code/blob/main/docs/user/outside-agents.md>
- OAuth validation, PKCE, code lifetime, redirect matching, token exchange, approving-session checks: <https://github.com/pingdotgg/t3code/blob/main/apps/server/src/auth/McpOAuth.ts>
- Hosted approval routing and link/hostname conditions: <https://github.com/pingdotgg/t3code/blob/main/apps/server/src/auth/mcpOAuthHttp.ts>
- Hosted consent page and no-code approval: <https://github.com/pingdotgg/t3code/blob/main/apps/web/src/components/auth/ApproveAgentSurface.tsx>
- Consent UI, access levels, code entry, redirect checks: <https://github.com/pingdotgg/t3code/blob/main/apps/web/src/components/auth/ConnectAgentSurface.tsx> and <https://github.com/pingdotgg/t3code/blob/main/apps/web/src/components/auth/ConnectAgentSurface.logic.ts>
- Per-call hosted authorization reuse: <https://github.com/pingdotgg/t3code/blob/main/packages/client-runtime/src/state/connectMcpApproval.ts>
- Scopes, access levels, bearer token result, no refresh grant: <https://github.com/pingdotgg/t3code/blob/main/packages/contracts/src/auth.ts>
- Session scopes, stored runtime ceiling, and access-mode mapping: <https://github.com/pingdotgg/t3code/blob/main/apps/server/src/auth/EnvironmentAuth.ts>
- 30-day MCP session TTL, `/mcp` restriction, bearer authentication: <https://github.com/pingdotgg/t3code/blob/main/apps/server/src/auth/EnvironmentAuth.ts>
- Five-minute one-use setup code default: <https://github.com/pingdotgg/t3code/blob/main/apps/server/src/auth/PairingGrantStore.ts>
- Wizard/sharing entry: [#33196](https://github.com/pingdotgg/t3code/pull/33196)
- Hosted approval and request-session checks: [#33632](https://github.com/pingdotgg/t3code/pull/33632)
- DPoP key-bound CSRF fix and authorization reuse: [#33645](https://github.com/pingdotgg/t3code/pull/33645)

Related repository pages: [quickstart](QUICKSTART.md), [setup and preflight](SETUP_AND_PREFLIGHT.md), and [native connector notes](README.md).
