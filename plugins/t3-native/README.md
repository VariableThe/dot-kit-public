# Native T3 Code connector

This directory documents the native T3 Code MCP connector, pins its upstream source, and provides a caller-side completion monitor. It does not contain or modify the T3 engine.

## Status

- Working connector: native T3 Code feature, not this repository's Python adapter.
- Latest completion-test server version: `0.1.8-nightly.20261107.4340` (2026-11-09). The source pin below remains the historical 2026-11-06 dependency, not a reproduced build of this runtime.
- Native MCP route: `/mcp`
- Public hostname/ingress mapping was not fully verified from non-secret metadata.

## Upstream source dependency

- Submodule path: `vendor/t3code`
- Repository: `https://github.com/pingdotgg/t3code`
- Tag: `v0.1.8-nightly.20261104.4179`
- Commit: `9e85538535c8553901351112ab02dd1dd62db5ee`
- License: MIT, Copyright (c) 2026 T3 Tools Inc.
- License SHA-256: `935d8f2af0c703f9c39517ee57cc4930b19d02d533be930b63f0e82f93614b43`
- Build requirements: Node `26.12.1`, pnpm `12.14.0`
- Build command: `vp run --filter './apps/*' build`
- Test command: `vp run -r test`
- Relevant upstream paths:
  - `apps/server/src/mcp`
  - `apps/server/src/auth`
  - `apps/server/src/cloud`
  - `apps/server/src/environment`
  - `docs/user/outside-agents.md`
  - `docs/user/faq.md`

The submodule is pinned to the exact commit. It is not a vendored copy and is not modified by this repository. Initialize it with `git submodule update --init --recursive`. This repository does not claim that the upstream build was reproduced.

## Native connector setup

1. In T3, open **Settings -> Environments**.
2. Open a saved environment and choose **Copy MCP URL**.
3. Use `https://<environment-address>/mcp`.
4. Choose OAuth. No client ID or secret is required.
5. Approve the sign-in with a pairing code from **Settings -> Sharing & access** or `t3 auth pairing create`.
6. Choose **Read only**, **Supervised**, or **Full access**.

For ChatGPT, create a custom MCP app, set the server URL to the T3 Connect HTTPS URL ending in `/mcp`, choose OAuth, and approve the sign-in.

Agents on the same device may use `localhost`, T3 Connect, or another HTTPS address. Plain HTTP LAN or tailnet addresses are not suitable for sign-in.

## Observed operations

The observed native tool identifiers are: t3_environment_read, t3_project_list, t3_thread_read, t3_thread_launch, t3_thread_send, t3_thread_wait. This is not a complete MCP tool schema.

## Experimental Python adapter

The Python adapter under plugins/ecosystem_connectivity is included separately. It is experimental, offline-tested, and not represented as deployed.

## Limitations

- The public hostname/ingress chain was not fully verified from non-secret local metadata.
- The upstream source is referenced, not copied, except through the pinned submodule.
- No service, credential, tunnel, or runtime configuration was changed by this repository.

## Completion monitoring

Use the [caller-side completion monitor](COMPLETION_MONITOR.md) after each authorized launch or follow-up. It pairs the native event wait with immediate result retrieval, durable caller-owned checkpoints and a conservative completion guard. It does not add a server callback or run automatically when installed.
