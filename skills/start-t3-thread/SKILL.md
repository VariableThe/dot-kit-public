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
