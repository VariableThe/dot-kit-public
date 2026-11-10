---
name: start-t3-thread
description: Inspect, start, continue, monitor, or interrupt authorized T3 Code work through an existing connected T3 instance. Use for project and thread coordination, not unrestricted shell access.
---

# Control T3 Code threads

## Prerequisites

Use an existing authenticated T3 connector or controller on the selected host. This package does not install a T3 server, provide credentials, or bundle a standalone controller CLI. Discover the current tool definitions before choosing arguments.

Read-only inspection needs only the service's read permission. Launching, sending, and interruption need the applicable operate permission and the user's authorization for the specific task. A configured credential or installed skill is not permission to act.

## Resolve the task

- Verify the intended host, environment, project, workspace, and thread using the live interface.
- Read existing matching threads before starting another. A title alone is not enough to identify a task.
- Confirm the requested outcome, allowed changes, model or spending limits, and stopping condition.
- Treat thread IDs and account state as host-specific. Do not forward credentials to a different host.

## Launch or continue

- Use the live T3 thread tools. Typical operations include environment/project discovery and thread list, read, launch, send, wait, and interrupt.
- For edits, bind an explicit suitable workspace strategy before launch. Preserve existing work and avoid competing writers.
- Keep the task prompt self-contained. Use only the context needed for the authorized work.
- Preserve runtime and permission ceilings. Do not silently broaden permissions, switch to paid models, create credentials, or change network/security settings.
- Retain the returned thread/run IDs and verified link. Acceptance or a running state does not mean the task completed.
- Choose follow-up mode deliberately. Do not steer or restart unrelated human work.

## Verify outcomes

- Read the resulting messages and inspect required artifacts and test evidence.
- Preserve pagination cursors and retrieve truncated content before relying on it.
- On a timeout or lost write response, check the same target for the outcome before considering a retry. Reuse a request ID when the current API supports it.
- Do not assume a launch is safe to retry because a title search is empty.
- After interruption, verify terminal run state. A stopped turn does not prove detached processes ended.
- Report completed work, remaining limitations, and exact blockers separately.

Do not install services, open ports, mint tokens, or alter authentication as an automatic repair. Request the necessary authorization and use the existing supported route.

## Keep completion ownership

After each authorized launch or follow-up, apply the [native connector completion workflow](../../plugins/t3-native/COMPLETION_MONITOR.md). Keep one native watcher and a durable record for the exact environment/thread/run, including the original read boundary, acceptance criteria, owner and handled-result IDs. Renew bounded event waits after timeout without a native wake contract; do not abandon an unfinished run. Wait for delegated work to settle, read the final reply immediately, verify artifacts, and report useful results or exact blockers. Preserve unfinished ownership after connection errors or execution limits. The helper is caller-side and does not create automatic server-to-dot notifications.


## Required executable monitoring route

Use the active `runAndMonitor` wrapper from [ACTIVE_MONITOR.md](../../plugins/t3-native/ACTIVE_MONITOR.md) for every authorized T3 launch/send. Assign a native worker before dispatch and keep one awaited execution responsible for dispatch, exact-run event waits and immediate validated result retrieval. Do not use raw launch/send then promise to attach a watcher later. If the installed runtime or durable journal is unavailable, stop before dispatch and report the blocker.

On the selected computer the installed runtime is `$HOME/.local/share/dot-t3-monitor/active-runtime.js`, with `completion-journal.py` alongside it and journal metadata under `$HOME/.config/agent-kit/t3-completion`. Resolve HOME and verify files on that computer. Load the generated bundle, use its `createToolBridge` with the existing native T3 callbacks, and follow the executable recipe. No credentials, new server or alternate transport are needed.

The current native launch/send interfaces support `clientRequestId`; preserve it and identical arguments after uncertainty. The wrapper never retries mutations automatically. Resume a known operation with `method:"resume"`, preserving exact IDs and the original read boundary. Do not change an unclear result to success or launch replacement work. For a verified delegated continuation, supply the exact result run IDs and an authoritative evidence reference.

A live same-thread watcher blocks a separate send before dispatch. Serialize an explicit watcher handoff rather than bypassing it. Parent review and user delivery are separate journal states. Installing these files is not a daemon or universal interception; only routed operations are automatically monitored. If all execution dies, the record requires a later authorized resume unless a separately verified scheduler exists. Cloud skill changes and publication must be confirmed separately; local files do not update them.
