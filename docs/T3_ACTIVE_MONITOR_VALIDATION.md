# Active completion route validation, 2026-11-10

The executable launch/send wrapper, fenced journal, pure core and generated tools-isolate bundle were added to the native-connector integration. This is not a replacement T3 server or an independent credentialed service.

## Local checks

- 74 JavaScript tests passed, including generated bundle execution and native callback bridge.
- 25 Python journal tests passed, including multiprocess contention and read-only snapshots.
- The generated runtime matched its source.
- Review corrections: validate wait/result identity before any potentially acknowledging result read; omit a negative initial cursor from native reads; allow escaped native IDs; fence/dedupe request keys; require evidence for continuation IDs; keep review/delivery separate.

## Live evidence

One new scratch run used the existing native T3 OAuth connector, Pi, and a P8 model. The wrapper durably recorded dispatch, launched once, and attached an exact-run wait. The run paused at a command approval for its bounded sleep. The first monitor found a native cursor mismatch; the original record stayed unfinished.

After the user approved that exact command and the cursor fix was applied, `method:resume` used the original operation/thread/run. Its only native calls were wait and read. The final exact marker `T3_ACTIVE_WRAPPER_20261110_OK` was retrieved, the run was completed with no active run or pending request, and the journal was left at `review_pending` for parent delivery. No replacement launch, permission widening, credential creation, or personal ChatGPT-through-T3 call occurred.

This verifies real dispatch, durable metadata, safe interruption/blocker handling, and same-run completion recovery through the executable route. The resumed read occurred after completion. It is not evidence of sub-second latency, continuous unattended execution, server callbacks, or successful user delivery.

## Remaining boundaries

- Only operations routed through this wrapper automatically attach monitoring.
- A live same-thread watcher intentionally blocks a separate send until an explicit ownership handoff.
- No independent scheduler/daemon was created; a terminated caller needs an authorized resume.
- Cloud skill editing tools were unavailable. Local/repository skill updates do not change the cloud copy.
- Publication is tracked in the associated pull request. These checks do not imply merge or deployed server changes.
