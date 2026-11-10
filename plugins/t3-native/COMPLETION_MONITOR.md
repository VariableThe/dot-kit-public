# Track T3 completion without losing ownership

This is a caller-side helper for the existing native T3 MCP connector. It does not modify the upstream T3 engine, add a server tool, register a webhook, create credentials, or run a daemon. Node 20 or newer is sufficient for the helper and its tests. Cloning this repository does not start monitoring.

## Use it after authorized launches and follow-ups

1. Save the verified environment, project, thread and exact run IDs, the authorized task and acceptance criteria, the pre-run read boundary, and the parent responsible for the result.
2. Assign exactly one native watcher for that run. The calling agent must keep ownership and a durable checkpoint under its existing task record or store. A second watcher must not be started while the first owns the run. This module does not provide a distributed lock or exactly-once messaging.
3. Use the existing connector for `t3_thread_wait` with `untilDelegatedWorkSettled:true`. The bounded event wait returns early when work finishes. Timeout without `next` renews the wait; it is not a completion or a reason to abandon the task.
4. On a terminal response, read the final result immediately, in the same execution rather than after an unrelated model turn. Recover pagination and truncated text. A completed parent with live children is unfinished. Questions, approvals, failures, disconnection and execution-budget exhaustion retain unfinished ownership.
5. A T3 `t3_thread_read` of a child final result can mark that result delivered on the server. This acknowledgment does not establish delivery to the user and must not automatically mark the local handled-result record. Keep one owner and checkpoint before reading; do not read another coordinator's child solely as a monitoring probe.
6. Verify artifacts and acceptance criteria after `ready_for_review`. Report the useful result promptly. Only after delivery or the next authorized step is known should the parent mark that result run handled. A message-send timeout requires reconciliation before retry, not a duplicate send.

## Module interface

```js
import { monitorCompletion } from './completion-monitor.mjs';
const result = await monitorCompletion({
  target: {
    environmentId: verifiedEnvironmentId,
    threadId: acceptedThreadId,
    runId: acceptedRunId,
    afterPosition: positionBeforeThisRun,
  },
  callTool: existingAuthorizedConnectorCall,
  checkpoint: persistExistingTaskRecord,
  timeoutMs: 60000,
  maxWaits: 60,
  handledResultRunIds: previouslyHandledResults,
});
```

`callTool(name,args)` receives only `t3_thread_wait` or `t3_thread_read` and must use the already-authorized connector for the recorded environment. It returns the MCP envelope or decoded JSON. No token, URL, sign-in or credential provider is accepted by this module. `checkpoint(record)` is required and must persist metadata under the caller's single-owner record. Checkpoint failure prevents successful handoff; it must not be ignored.

Checkpoints preserve both `startPosition` and `lastReadPosition`. On recovery use the original `startPosition` as the target's `afterPosition`, so a previously seen final reply is not accidentally skipped. Keep read cursors separately from handled-result IDs. These records contain no full prompts or replies. The returned final evidence can contain the task's private reply and must stay within its authorized audience.

The helper defaults to at most 60 one-minute waits per invocation. If that budget ends, it returns `watch_incomplete`; the parent must continue ownership by resuming the same watch or reporting a genuine blocker. It does not cancel T3 work. Cancellation is checked before and after tool calls and again during final handoff; a held call can take up to its bounded timeout plus transport overhead to return before cancellation is observed. Retry is limited to recognized transient read errors, at most three attempts with backoff by default. Authentication and permission errors are not repaired or retried automatically.

The helper reads current run metadata and pending-request count. It rejects a different run or unverified continuation rather than adopting a newer human task. If the returned final answer uses another run, verify its continuation lineage before putting it in `target.verifiedResultRunIds`. Only a real T3 parent that owns the app-owned delegated task may set `target.callerOwnsDelegatedTask:true` and rely on `next` wake instructions. An outside caller has no automatic T3-to-parent push subscription.

`completion-guard.mjs` is also usable independently with JSON on stdin. It classifies the `expected`, `wait`, `read`, and optional handled-result IDs described in its tests. Neither helper returns `taskComplete:true`, starts new work, sends follow-ups, approves permissions, merges, deploys, or sends user notifications.

## Verification

```sh
node --test plugins/t3-native/completion-*.test.mjs
node --check plugins/t3-native/completion-monitor.mjs
node --check plugins/t3-native/completion-guard.mjs
```

The offline suites cover terminal outcomes, timeout with unfinished children, blockers, wrong or newer runs, pagination/truncation, duplicate handled results, read retry/backoff, cancellation, execution limits and checkpoint failures. They do not prove background survival or delivery.

On 2026-11-09 an explicitly authorized live test used the native T3 connector on server `0.1.8-nightly.20261107.4340`. A fresh thread ran a bounded delay and returned an exact completion marker. Its wait returned 178 ms after the server's recorded completion. A second run in the same thread exercised this monitor's core with the live tool callbacks: detection took 265 ms, and the immediate final-read completed 15.814 seconds after server completion. Both markers were verified. Times compare server and observer wall clocks and are observations, not a latency guarantee.

The second live harness loaded the same module functions into the tool execution context, supplied existing connector callbacks, and retained checkpoints in the test session. No standalone transport, durable-process restart, unsolicited wakeup, nested-child live run, credential refresh or outage recovery was tested. Repository tests exercise those classifier/control-flow branches offline. Independent review added regressions for newer activity appearing during text-offset recovery, cancellation during in-flight reads or final checkpoints, and an unverified different result run reporting failure. Those later edge-case fixes passed the expanded 45-test suite. The final modified module also fetched and verified the existing completed run through the live connector; its edge cases were not exercised through another live agent run. The upstream source pin remains the older documented dependency; this validation did not rebuild or update T3.


## Required active operation route

Use [ACTIVE_MONITOR.md](ACTIVE_MONITOR.md) and the executable `runAndMonitor` wrapper for every authorized launch/send in this workflow. It attaches the exact-run monitor before returning; the lower-level helper alone is not activation. This is caller-owned, not a daemon or global native-tool hook.
