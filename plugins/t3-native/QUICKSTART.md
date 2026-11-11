# First-install quickstart: T3 Code MCP connector

This is the shortest safe path for a **new** installation of the T3 MCP connector. If your T3 connection already reads projects and controls threads, keep it: this page is not a reason to reconnect a healthy installation.

The connector is T3 Code's native MCP server. It is not a local GPT/Codex relay, and this repository does not ship the T3 engine, an OAuth transport, or credentials. A T3 thread still runs its selected provider/model and uses that provider's allowance. For how transport, URL discovery, and the consent page relate, see [connection choices compared](CONNECTION_MODES.md).

## Before you start

- A T3 Code environment you control, running on a host that stays on while you work.
- For a hosted assistant (ChatGPT or another cloud assistant): T3 Connect signed in and **enabled for that environment**. In the desktop app that is **Settings → Sharing & access**; on a CLI host, `t3 up` and choose T3 Connect. Saving the sign-in alone does not start a server or make the machine reachable.
- An MCP client that supports remote MCP over HTTP with OAuth. ChatGPT custom MCP apps, Claude Code, and Codex qualify. An app that requires its own client ID cannot use T3's dynamic registration.
- Permission to approve access on the environment.

No client ID, client secret, or copied token is required.

## Shortest happy path (hosted assistant)

1. **Open the wizard on the T3 host.** Press `Cmd/Ctrl+K` and choose **Use T3 Code in another app (MCP)**, or use **Settings → Sharing & access**. Pick your app.
2. **Let it prepare the public route.** If prompted, sign in to T3 Connect and turn it on for the environment. The wizard checks the route and shows app-specific setup; the ChatGPT tile copies the T3 Connect MCP URL. If there is no wizard entry, use **Settings → Environments → Copy MCP URL**.
3. **Confirm the URL shape.** For a hosted assistant it must be the environment's internet-reachable T3 Connect address ending in `/mcp`:

   ```text
   https://<environment-address>/mcp
   ```

   A `localhost`, LAN, or tailnet address will not work from a hosted assistant.

4. **Add the server in the assistant.** Create a custom MCP app/server, paste the URL, and choose **OAuth**. For ChatGPT, the tile copies the URL; it does not install an official marketplace connector. For an unlisted app, choose **Something else…** and add a remote MCP server with OAuth.
5. **Approve the sign-in you just started.** When the environment is linked to T3 Connect, the page opens on the T3 Code web app: sign in with the linked account, choose the exact access level this client needs, and approve. On the environment's own page, enter a one-time code from the host or approve with a signed-in browser that can manage access. Hosted approval needs no code when your signed-in session holds the scopes for the chosen level; otherwise use the offered **Use a sign-in code instead** link. The wizard does not choose that consent page; the server's link, issuer-host, and peer-link conditions do. If you use a manually copied URL on a linked tunnel, a new sign-in can still open the hosted page. When that page offers **Use a sign-in code instead**, use that supported link to reach the environment page; re-copying the same URL is not a bypass.

   Choose least permission for the intended use. **Read only** is for inspection and cannot start, message, or stop threads. Choose **Supervised** through **Full access** only when you are authorized to let this client control threads; the thread still cannot exceed that mode. Picking Read only for a client that must control threads just forces a second consent flow. The initial verification should still be read-only, independent of the ceiling you grant.

6. **Confirm the connection in the client.** The app's connected state, `claude mcp list`, or `codex mcp list` should show the server, and a simple request to read the environment or list projects should succeed. That is the normal success check; it does not require cloning this repository, initializing submodules, or writing a connector callback.

### Optional developer check

If you already have an authorized connector callback, you can run the bounded read-only preflight in [`connection-preflight.mjs`](connection-preflight.mjs) through it and expect `state: 'read_ready'`. This is advanced verification, not a first-install requirement. The `setup-readiness.mjs` helper can check the copied URL shape offline; it does not contact T3 and proves nothing about reachability, consent, or writes. In contrast, the preflight above is live: it performs three read-only reads through your existing connector.

## Older-server fallback

Older installed UIs may not have the command-palette wizard or hosted consent. The setup still works; this older-server path is not evidence that re-copying the same linked tunnel URL will bypass hosted consent:

1. On the T3 host, open **Settings → Environments**, open the intended environment's menu, and choose **Copy MCP URL**.
2. Connect the app with that URL and OAuth as above.
3. The sign-in approval page **asks for** a one-time code; it does not give you one. Get a code on the T3 host from **Use T3 Code in another app (MCP)** (in an older wizard, choose the level first and it shows a level-bound code), from the access-management controls under **Settings → Sharing & access**, or from `t3 auth pairing create`. Enter that code on the approval page.
4. A level-bound code carries the level chosen in the host wizard. A plain code, such as one created by access management or the CLI, is not level-bound, so choose the access on the approval page; **Auto** is selected by default, capped by managed policy. Approving without a code on the environment's own page starts at **Read only** when that browser session is allowed to approve. Codes are valid for **5 minutes and one use**. Never put a code or credential in a repository file or chat message. If the hosted page appears instead and offers **Use a sign-in code instead**, use that supported link; do not re-copy the URL to try to change the consent page.

Do not guess the fallback from a version number. Use what the installed UI actually shows, and re-check after an upgrade.

## Local agent path (Claude Code or Codex on your machine)

- Use `localhost` if the agent runs on the T3 host, or the T3 Connect / Tailscale HTTPS URL from another device. A plain-`http` LAN or tailnet URL can be discovered but cannot finish sign-in. `0.0.0.0` and `::` are bind addresses, not addresses to connect to.
- Upstream commands (see the outside-agent docs below for the current syntax):

  ```sh
  claude mcp add --transport http t3 https://<environment-address>/mcp
  claude mcp login t3
  ```

  ```sh
  codex mcp add t3 --url https://<environment-address>/mcp
  codex mcp login t3
  ```

- Approve the least access the agent needs. Do not grant thread control for a client that only reads. Confirm with a simple read; the optional preflight above is not required.

## What success looks like

- The consent page names the exact app, environment, and access level you intended.
- The client lists the T3 server as connected after approval.
- A simple read succeeds: environment identity and a project page are readable.
- `read_ready` from the optional preflight is read proof only. It is **not** proof that a thread launch, send, or stop is permitted or will execute.

## When it does not work

| Symptom | Likely cause | Do this |
| --- | --- | --- |
| No **Use T3 Code in another app (MCP)** entry | This installed UI does not expose the wizard | Use **Settings → Environments → Copy MCP URL** for discovery only. It does not choose the consent page; on a linked tunnel the same URL can return to hosted approval. If the hosted page offers **Use a sign-in code instead**, use that supported link. If no supported environment-page fallback is reachable, stop and diagnose or wait; do not rewrite the OAuth URL, switch accounts/endpoints, or change service/tunnel state automatically. |
| Hosted assistant cannot connect; copied URL is `localhost`, `127.x`, `100.x`, or `192.168.x` | Local/private route copied | Enable T3 Connect for the environment and copy its public `https` MCP URL again. |
| URL is `http://` and not `localhost` | Sign-in cannot complete over plain http | Use the T3 Connect `https` URL or a Tailscale HTTPS URL. Use `localhost` only for an agent on that host. |
| URL is `http://0.0.0.0` or `http://[::]` | A bind address was copied, not a client destination | Use `localhost` for an agent on the host, or copy the environment's https URL again. |
| URL does not end in `/mcp` | Wrong or edited URL | Re-copy from T3; do not hand-build the path. |
| App asks for its own client ID or registration | That app cannot use T3's dynamic registration | Use an OAuth-capable custom MCP app or a supported app. |
| Consent page names the wrong environment or account | Wrong flow or account | Cancel, return to the intended environment, and start the wizard there with the linked account. |
| Consent page asks for a code, but you did not get one | The code comes from the host, not the requesting app | Use **Use T3 Code in another app (MCP)**, **Settings → Sharing & access**, or `t3 auth pairing create` on the host. |
| Client reports `unauthorized`, `auth_error`, or an expired/revoked sign-in | Authentication is missing, expired, or revoked | Re-authenticate with the same least access. If hosted consent loads and offers **Use a sign-in code instead**, use that supported link; re-copying the same URL is not a bypass. The MCP token lasts 30 days and has no refresh grant; revocation takes effect immediately. |
| Consent or tool call explicitly says `permission_denied`, `forbidden`, or a managed-policy limit | An access or policy denial, not a missing sign-in | Do not reapprove to bypass it. Adjust access only if authorized, or stop. Approval never overrides an explicit denial. |
| Preflight says `environment_mismatch` | `expectedEnvironmentId` is not the environment the connector reached | Re-copy from the intended environment; do not treat it as the same host. |
| Preflight fails or returns `invalid_response` at `orchestrator_capabilities` | The client or server lacks a compatible tool/schema; OAuth already succeeded | Check the client and server tool/update compatibility. Repeating OAuth or reinstalling credentials will not add a missing tool. Treat the read proof as incomplete and make no write claim. |
| Preflight says `read_ready` but a launch or send fails | Read-only approval, missing write support, or write execution unverified | Raise access only if the task needs it and you are authorized to. Reads do not prove writes. |
| Existing plugin still works | No automatic migration is needed; healthy grants survive setup changes | Keep it. Do not reconnect just to try the new wizard. |

## What this page does not prove

- A URL that looks public is not proof of DNS, TLS, tunnel, ingress reachability, or internet routability. `setup-readiness.mjs` is an offline naming/range heuristic; the wizard and the live connection make the real check.
- The hosted consent flow does not make every ChatGPT plan, workspace policy, or Enterprise app-distribution policy support the connection. Do not broaden workspace sharing to work around a policy.
- If hosted consent cannot load and no supported environment-page fallback is reachable, stop and diagnose or wait. An authorized local/other route is a separate choice that must be verified, not guaranteed rollback for the hosted client.
- Approving in the hosted browser does not bind the client's later MCP bearer token to that browser's DPoP key. The MCP token is a bearer credential held by the client; treat it as access.
- The source pin and recorded versions in this repository are historical provenance, not a reproduced build of your installed binary.
- `setup-readiness.mjs` has no side effects and does not contact T3. `connection-preflight.mjs` performs three live read-only reads through an existing authorized connector; it does not write, create credentials, or change access. Creating an app connection or approving OAuth in the real T3 UI **does** create persistent access, controlled by you and revocable under **Settings → Sharing & access**. This repository does none of that.

## Sources checked on 2026-11-10

- Upstream outside-agent setup and consent: <https://github.com/pingdotgg/t3code/blob/main/docs/user/outside-agents.md>
- Upstream T3 Connect and remote routes: <https://github.com/pingdotgg/t3code/blob/main/docs/user/remote-access.md>
- Wizard/sharing entry: [#33196](https://github.com/pingdotgg/t3code/pull/33196)
- Hosted approval request authentication: [#33632](https://github.com/pingdotgg/t3code/pull/33632)
- DPoP key-bound approval fix: [#33645](https://github.com/pingdotgg/t3code/pull/33645)

Next: [connection choices compared](CONNECTION_MODES.md), [setup and preflight details](SETUP_AND_PREFLIGHT.md), and [completion monitoring](COMPLETION_MONITOR.md).
