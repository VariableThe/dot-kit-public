"""A fake T3 MCP backend for offline adapter tests.

Models just enough of the T3 thread tool surface for the adapter's safety
rules: project listing, cursor-paginated thread lists, thread reads with a
project-scoped thread object, launches (title-dedupe is the adapter's job),
sends that deduplicate by clientRequestId, and interrupts. Any tool can be
programmed to raise (connection drops, scope denials) via ``fail``.
"""

import itertools
import threading

from t3pipe.errors import ConnectionError, ToolError


class FakeResult:
    def __init__(self, data):
        self.data = data
        self.text = data
        self.structured = None
        self.is_error = False


class FakeT3:
    def __init__(self, projects=None, threads=None, reads=None):
        self.projects = projects or []
        # each thread: {"threadId", "projectId", "title", "status"}
        self.threads = list(threads or [])
        # threadId -> {"thread": {..., "projectId": ...}, "items": [...], ...}
        self.reads = reads or {}
        self.launches = []
        self.sends = []
        self.interrupts = []
        self.waits = {}         # threadId -> scripted t3_thread_wait answer
        self.fail = {}          # tool name -> Exception to raise every call
        self.calls = []
        self._ids = itertools.count(1)
        self._sent = {}         # clientRequestId -> send response (dedupe)

    # -- helpers ------------------------------------------------------------

    def _next_id(self, prefix):
        return f"{prefix}-{next(self._ids)}"

    def threads_in(self, project_id):
        return [t for t in self.threads if t.get("projectId") == project_id]

    # -- FakeClient-style interface ------------------------------------------

    def call_tool(self, name, args=None, timeout=None, **kwargs):
        args = args or {}
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail[name]
        return FakeResult(self._handle(name, args))

    def _handle(self, name, args):
        if name == "t3_environment_read":
            return {"environmentId": "env-1", "label": "Host",
                    "serverVersion": "1.2.3"}
        if name == "t3_project_list":
            return {"projects": self.projects}
        if name == "t3_thread_list":
            rows = self.threads_in(args.get("projectId"))
            title = args.get("titleContains")
            if title:
                rows = [t for t in rows if title.lower() in t.get("title", "").lower()]
            if args.get("settled") is not None:
                rows = [t for t in rows if bool(t.get("settled")) is bool(args["settled"])]
            cursor = int(args.get("cursor") or 0)
            limit = int(args.get("limit") or 50)
            page = rows[cursor:cursor + limit]
            nxt = cursor + len(page)
            return {
                "threads": page,
                "nextCursor": str(nxt) if nxt < len(rows) else None,
                "total": len(rows),
            }
        if name == "t3_thread_search":
            return {"matches": []}
        if name == "t3_thread_read":
            tid = args["threadId"]
            if tid not in self.reads:
                raise ToolError(f"no such thread {tid}", t3_code="not_found")
            data = dict(self.reads[tid])
            after = args.get("afterPosition")
            if after is not None:
                data["items"] = [i for i in data.get("items", [])
                                 if i.get("position", 0) > after]
            return data
        if name == "t3_thread_wait":
            tid = args["threadId"]
            if tid in self.waits:                 # explicit scripted answer
                out = dict(self.waits[tid])
            else:
                thread = next((t for t in self.threads
                               if t.get("threadId") == tid), None)
                if thread is None:
                    raise ToolError(f"no such thread {tid}", t3_code="not_found")
                status = thread.get("status", "running")
                # terminal or idle -> answered; otherwise still working
                out = {"status": status,
                       "timedOut": status not in (
                           "idle", "completed", "interrupted", "failed",
                           "cancelled", "rolled_back")}
            out.setdefault("runId", args.get("runId") or self._next_id("run"))
            return out
        if name == "t3_thread_launch":
            self.launches.append(args)
            thread = {
                "threadId": self._next_id("thread"),
                "projectId": args.get("projectId"),
                "title": args.get("title"),
                "status": "starting",
            }
            self.threads.append(thread)
            return {"threadId": thread["threadId"], "status": "starting",
                    "runId": self._next_id("run")}
        if name == "t3_thread_send":
            self.sends.append(args)
            rid = args.get("clientRequestId")
            if rid in self._sent:
                return self._sent[rid]     # T3 dedupes by clientRequestId
            result = {
                "delivery": args.get("mode", "queue"),
                "runId": self._next_id("run"),
                "taskId": self._next_id("task"),
                "messageId": self._next_id("msg"),
                "status": "queued",
            }
            self._sent[rid] = result
            return result
        if name == "t3_thread_interrupt":
            self.interrupts.append(args)
            return {"status": "interrupting"}
        raise ToolError(f"unexpected tool {name}", t3_code="unexpected")


class BlockingT3(FakeT3):
    """A fake that blocks inside one tool until an event is set — used to
    exercise cancellation and shutdown-drain races deterministically."""

    def __init__(self, *args, block_on="t3_environment_read", **kwargs):
        super().__init__(*args, **kwargs)
        self.block_on = block_on
        self.release = threading.Event()
        self.reached = threading.Event()

    def call_tool(self, name, args=None, timeout=None, **kwargs):
        if name == self.block_on:
            self.reached.set()
            self.release.wait(timeout=10)
        return super().call_tool(name, args, timeout, **kwargs)