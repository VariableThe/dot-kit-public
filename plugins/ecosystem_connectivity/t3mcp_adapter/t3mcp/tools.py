"""Tool surface exposed by the t3mcp adapter.

Authorization model (hardened):

* **End-user authentication on every call, reads included.** Each
  ``tools/call`` must present a bearer JWT naming the caller; the token
  must carry the scope the tool requires (``t3:read`` for reads,
  ``t3:write`` for writes). A string literal is *never* treated as proof of
  consent — ``authorize-t3-write`` was removed as an authorization
  mechanism because any prompt can type it.
* **Host/project allowlists enforced inside every handler.** Unknown hosts
  and non-allowlisted projects are refused with ``target_denied``; a thread
  is only readable/writable after a fail-closed membership check binds its
  id to an allowlisted project (arbitrary thread ids included).
* **Durable write journal.** Request ids and outcomes survive crashes and
  restarts (see journal.py). A repeated ``client_request_id`` replays the
  recorded outcome; a *different* payload under the same id is refused
  (``request_id_conflict``). An uncertain launch is never retried just
  because a title search finds nothing; a send may be re-issued under the
  same id because T3 deduplicates sends by ``client_request_id``.

Stable outcome vocabulary: ``launched`` / ``duplicate`` / ``cached`` /
``accepted`` / ``completed`` / ``uncertain`` / ``in_progress`` plus stable
error codes. The adapter deliberately exposes nothing that takes a shell
command, URL, file path, or credential; started threads keep T3's own
approval model (no runtimeMode is ever set).
"""

import time
import threading
import uuid

from t3pipe import interrupt as t3pipe_interrupt
from t3pipe import list_threads as t3pipe_list_threads
from t3pipe import read_thread as t3pipe_read_thread
from t3pipe import send as t3pipe_send
from t3pipe import start_thread as t3pipe_start_thread
from t3pipe import wait_thread as t3pipe_wait_thread
from t3pipe.errors import (
    AuthError,
    ConnectionError,
    ProtocolError,
    ScopeDenied,
    TargetRequired,
    T3Error,
    ToolError,
    Uncertain,
)

from .auth import READ_SCOPE, WRITE_SCOPE, AuthContext, JwtVerifier
from .errors import AdapterError
from .journal import Journal, args_digest

THREAD_STATUSES = (
    "idle", "preparing", "queued", "starting", "running", "waiting",
    "completed", "interrupted", "failed", "cancelled", "rolled_back",
)
SEND_MODES = ("auto", "queue", "steer", "restart")

UNCERTAIN_LAUNCH_GUIDANCE = (
    "launch outcome unknown; a thread may or may not exist. Inspect "
    "t3_list_threads before retrying. Absence of a matching title does NOT "
    "prove the launch failed (titles change and scans are bounded) — do not "
    "relaunch on absence alone; use a NEW client_request_id only after "
    "verifying state."
)
UNCERTAIN_SEND_GUIDANCE = (
    "send outcome unknown; the message may or may not have been delivered. "
    "Re-read the thread, then resend with THIS SAME client_request_id — T3 "
    "deduplicates sends by it, so the resend cannot create a duplicate turn."
)


# ---------------------------------------------------------------------------
# ToolContext
# ---------------------------------------------------------------------------

class ToolContext:
    """Per-process state: policy, auth verifier, cached T3 clients,
    durable write journal."""

    def __init__(self, policy, client_factory=None, clock=time.monotonic,
                 wall_clock=time.time, journal=None):
        self.policy = policy
        self.clock = clock
        self.wall_clock = wall_clock
        if client_factory is None:
            from t3pipe import T3Client
            def client_factory(host_cfg):  # noqa: E306 — local default
                # HostConfig.token raises AuthError (fail closed) when the
                # env var is missing; the credential never leaves this process.
                return T3Client(host_cfg.url, host_cfg.token,
                                timeout=policy.limits["call_timeout_s"])
        self.client_factory = client_factory
        self._clients = {}
        self._verifier = None
        self._wlock = threading.RLock()   # recall+begin must be one atomic step
        self._request_state = threading.local()

        # Durable journal (policy requires it whenever writes are enabled).
        self.journal = journal
        if self.journal is None and policy.journal_path:
            self.journal = Journal(policy.journal_path, wall_clock=wall_clock)
        if self.journal is None and policy.allow_writes:
            # Defensive: policy validation should have caught this.
            raise AdapterError(
                "journal_failed",
                "allow_writes=true requires a journal; refusing writes")

    @property
    def auth_subject(self):
        """Authenticated subject for this request's execution context."""
        return getattr(self._request_state, "subject", None)

    # --- hosts / projects --------------------------------------------------

    def resolve_host(self, name=None):
        return self.policy.host(name)

    def client(self, host_cfg):
        if host_cfg.name not in self._clients:
            self._clients[host_cfg.name] = self.client_factory(host_cfg)
        return self._clients[host_cfg.name]

    def reconnect(self, host_cfg):
        client = self.client(host_cfg)
        if hasattr(client, "reconnect"):
            client.reconnect()
        else:
            self._clients.pop(host_cfg.name, None)

    def check_project(self, project_id):
        return self.policy.check_project(project_id)

    # --- end-user authentication --------------------------------------------

    def _get_verifier(self):
        # Built lazily so a missing HS256 secret fails the call (fail closed),
        # not process startup. Cached after first success.
        if self._verifier is None:
            cfg = self.policy.auth
            self._verifier = JwtVerifier(
                issuer=cfg["issuer"],
                audience=cfg["audience"],
                algorithm=cfg["algorithm"],
                jwks=cfg.get("jwks"),
                secret_env=cfg.get("secret_env"),
                leeway_s=cfg.get("leeway_s", 30),
                wall_clock=self.wall_clock,
            )
        return self._verifier

    def authenticate(self, token):
        """Verify the end-user token for one call. Fail closed."""
        if not self.policy.auth_required:
            # Explicit development-only opt-out (policy auth.required=false).
            return AuthContext(subject="unauthenticated",
                               scopes={READ_SCOPE, WRITE_SCOPE})
        if not isinstance(token, str) or not token.strip():
            raise AdapterError(
                "auth_required",
                "every tools/call must carry an end-user bearer token in "
                "params._meta.authorization",
            )
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return self._get_verifier().verify(token)

    # --- budget / journal ----------------------------------------------------

    def check_deadline(self, deadline):
        if deadline is not None and self.clock() > deadline:
            raise AdapterError(
                "deadline_exceeded",
                "request budget exhausted; the outcome of any T3 call already "
                "issued is unknown — re-read state before retrying",
            )

    def check_write(self, rid, digest):
        """Recall the durable view of a request id (or None if unknown)."""
        if self.journal is None:
            raise AdapterError(
                "journal_failed", "writes require the durable journal")
        return self.journal.recall(
            rid, digest,
            ttl_s=self.policy.limits["client_request_id_ttl_s"],
        )

    def begin_write_checked(self, tool, rid, digest):
        """Atomic recall + write-ahead begin. Returns the prior durable view
        (conflict/cached/in_progress/uncertain) or None when this call may
        proceed. The lock makes concurrent same-id duplicates in one process
        deterministic: the second caller sees the first's pending record."""
        if self.journal is None:
            raise AdapterError(
                "journal_failed", "writes require the durable journal")
        with self._wlock:
            prev = self.journal.recall(
                rid, digest,
                ttl_s=self.policy.limits["client_request_id_ttl_s"],
            )
            if prev is not None:
                return prev
            self.journal.begin(tool=tool, rid=rid, digest=digest,
                               wall_ts=self.wall_clock())
            return None

    def begin_write(self, tool, rid, digest):
        """Write-ahead record before a write executes. Fails closed."""
        if self.journal is None:
            raise AdapterError(
                "journal_failed", "writes require the durable journal")
        self.journal.begin(tool=tool, rid=rid, digest=digest,
                           wall_ts=self.wall_clock())
    def finish_write(self, rid, outcome, meta):
        if self.journal is None:
            raise AdapterError(
                "journal_failed", "writes require the durable journal")
        self.journal.finish(rid=rid, wall_ts=self.wall_clock(),
                            outcome=outcome, meta=meta)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read_with_reconnect(ctx, host_cfg, fn):
    """Reads are idempotent: one reconnect + retry is safe. Writes never use this."""
    try:
        return fn()
    except ConnectionError:
        ctx.reconnect(host_cfg)
        return fn()


def _require_write_enabled(ctx):
    if not ctx.policy.allow_writes:
        raise AdapterError(
            "writes_disabled",
            "this adapter has allow_writes=false; start/send/interrupt are disabled",
        )


def _str_arg(args, name, *, required=False, max_chars=None, default=None):
    value = args.get(name, default)
    if value is None or (isinstance(value, str) and value == ""):
        if required:
            raise AdapterError("invalid_params", f"{name} is required")
        return default
    if not isinstance(value, str):
        raise AdapterError("invalid_params", f"{name} must be a string")
    if max_chars and len(value) > max_chars:
        raise AdapterError(
            "invalid_params", f"{name} exceeds the {max_chars}-character limit"
        )
    return value


def _int_arg(args, name, *, low, high, default=None):
    value = args.get(name, default)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise AdapterError("invalid_params", f"{name} must be an integer")
    return max(low, min(high, value))


def _request_id_arg(args):
    rid = args.get("client_request_id")
    if rid is None:
        rid = uuid.uuid4().hex
    if not isinstance(rid, str) or not (8 <= len(rid) <= 128):
        raise AdapterError(
            "invalid_params", "client_request_id must be a string of 8-128 chars"
        )
    if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in rid):
        raise AdapterError("invalid_params", "client_request_id may only contain [A-Za-z0-9_-]")
    return rid


def _thread_in_project(ctx, client, thread_id, project_id, deadline):
    """Fail-closed membership check: the thread must appear in the project's
    thread list (bounded scan) before its content is read or written.
    ``threadId`` is host-scoped; this check binds it to an allowlisted project."""
    cursor = None
    for _ in range(ctx.policy.limits["membership_scan_pages"]):
        ctx.check_deadline(deadline)
        data = t3pipe_list_threads(
            client, project_id,
            limit=ctx.policy.limits["membership_scan_page_size"],
            cursor=cursor,
        )
        for t in data.get("threads", []):
            # Do not trust a project-scoped backend response blindly. A
            # compromised or buggy backend must not turn the requested
            # allowlisted project into a handle for another project.
            if not isinstance(t, dict) or t.get("projectId") != project_id:
                raise AdapterError(
                    "target_denied",
                    "backend returned a thread outside the requested project",
                )
            if t.get("threadId") == thread_id:
                return True
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return False


def _assert_thread_in_project(ctx, client, thread_id, project_id, deadline):
    if not _thread_in_project(ctx, client, thread_id, project_id, deadline):
        raise AdapterError(
            "thread_not_in_project",
            "thread id was not found in this project (or lies beyond the "
            "adapter's bounded membership scan); refuse rather than touch it",
        )


def _authorize_backend_project(ctx, host_cfg, client, project_id, deadline):
    """Bind the requested allowlisted id to the backend's exact project id.

    In particular, do this before a launch: a title or other backend-side
    resolution must never be allowed to redirect a write to another project.
    """
    ctx.check_deadline(deadline)
    data = _read_with_reconnect(
        ctx, host_cfg,
        lambda: client.call_tool("t3_project_list", {}).data or {},
    )
    projects = data.get("projects", [])
    if not isinstance(projects, list) or not any(
            isinstance(p, dict) and p.get("id") == project_id for p in projects):
        raise AdapterError(
            "target_denied",
            "backend did not resolve the requested project id exactly",
        )
    return project_id


def _t3_code_for(exc):
    """Map T3Error subclasses to a stable adapter error dict (sanitized)."""
    if isinstance(exc, Uncertain):  # shouldn't escape handlers, but stay safe
        return {"code": "uncertain", "message": str(exc), "details": exc.details}
    if isinstance(exc, AuthError):
        return {"code": "auth_error", "message": str(exc), "details": exc.details}
    if isinstance(exc, ScopeDenied):
        return {"code": "scope_denied", "message": str(exc), "details": exc.details}
    if isinstance(exc, TargetRequired):
        return {"code": "target_required", "message": str(exc), "details": exc.details}
    if isinstance(exc, ConnectionError):
        return {"code": "connection_error", "message": str(exc), "details": exc.details}
    if isinstance(exc, ProtocolError):
        return {"code": "protocol_error", "message": str(exc), "details": exc.details}
    if isinstance(exc, ToolError):
        code = "tool_error"
        if exc.t3_code and "scope" in str(exc.t3_code).lower():
            code = "scope_denied"
        return {"code": code, "message": str(exc), "details": exc.details}
    if isinstance(exc, T3Error):
        return {"code": "t3_error", "message": str(exc), "details": exc.details}
    return {"code": "internal_error", "message": str(exc), "details": {}}


def _raise_conflict(rid):
    raise AdapterError(
        "request_id_conflict",
        "this client_request_id was already used with different arguments; "
        "refusing to replay a different request under it. Use a new "
        "client_request_id.",
        details={"client_request_id": rid},
    )


def _replay_summary(record):
    """Public view of a journaled record (ids and outcome only)."""
    return {
        "outcome": record.get("outcome"),
        "recorded_at": record.get("ts"),
        **{k: v for k, v in (record.get("meta") or {}).items()
           if k in ("host", "project_id", "thread_id", "run_id")},
    }


def _uncertain_replay_response(rid, record, guidance):
    return {
        "outcome": "uncertain",
        "client_request_id": rid,
        "recovered_from_journal": True,
        "guidance": guidance,
        "previous": _replay_summary(record),
    }


# ---------------------------------------------------------------------------
# read-only handlers
# ---------------------------------------------------------------------------

def h_health(ctx, args, deadline):
    host_cfg = ctx.resolve_host(args.get("host"))
    client = ctx.client(host_cfg)

    def fetch():
        return client.call_tool("t3_environment_read", {}).data or {}

    env = _read_with_reconnect(ctx, host_cfg, fetch)
    return {
        "status": "ok",
        "host": host_cfg.name,
        "environment_id": env.get("environmentId"),
        "environment_label": env.get("label"),
        "server_version": env.get("serverVersion"),
        "end_user_auth_required": ctx.policy.auth_required,
        "write_enabled": ctx.policy.allow_writes,
        "projects_exposed": len(ctx.policy.allowed_projects),
    }


def h_list_projects(ctx, args, deadline):
    host_cfg = ctx.resolve_host(args.get("host"))
    client = ctx.client(host_cfg)
    ctx.check_deadline(deadline)
    data = _read_with_reconnect(
        ctx, host_cfg, lambda: client.call_tool("t3_project_list", {}).data or {}
    )
    allowed = ctx.policy.allowed_projects
    projects = [p for p in data.get("projects", []) if p.get("id") in allowed]
    return {"host": host_cfg.name, "projects": projects}


def h_list_threads(ctx, args, deadline):
    host_cfg = ctx.resolve_host(args.get("host"))
    project_id = ctx.check_project(args.get("project_id"))
    client = ctx.client(host_cfg)

    title_contains = _str_arg(args, "title_contains",
                              max_chars=ctx.policy.limits["title_max_chars"])
    statuses = args.get("statuses")
    if statuses is not None:
        if not isinstance(statuses, list) or any(s not in THREAD_STATUSES for s in statuses):
            raise AdapterError("invalid_params", "statuses contains an unknown value")
    settled = args.get("settled")
    if settled is not None and not isinstance(settled, bool):
        raise AdapterError("invalid_params", "settled must be a boolean")
    limit = _int_arg(args, "limit", low=1, high=ctx.policy.limits["list_page_size"],
                     default=ctx.policy.limits["list_page_size"])
    cursor = _str_arg(args, "cursor", max_chars=256)

    def fetch():
        return t3pipe_list_threads(
            client, project_id,
            title_contains=title_contains, statuses=statuses,
            settled=settled, limit=limit, cursor=cursor,
        )

    ctx.check_deadline(deadline)
    data = _read_with_reconnect(ctx, host_cfg, fetch)
    threads = data.get("threads", [])
    if not isinstance(threads, list):
        raise AdapterError("protocol_error", "backend returned an invalid thread list")
    for thread in threads:
        if not isinstance(thread, dict) or thread.get("projectId") != project_id:
            raise AdapterError(
                "target_denied",
                "backend returned a thread outside the requested project",
            )
    next_cursor = data.get("nextCursor")
    return {
        "host": host_cfg.name,
        "project_id": project_id,
        "threads": threads,
        "next_cursor": next_cursor,
        "has_more": bool(next_cursor),
        "total": data.get("total"),
    }


def h_read_thread(ctx, args, deadline):
    host_cfg = ctx.resolve_host(args.get("host"))
    project_id = ctx.check_project(args.get("project_id"))
    thread_id = _str_arg(args, "thread_id", required=True, max_chars=128)
    client = ctx.client(host_cfg)

    view = args.get("view", "messages")
    if view not in ("messages", "activity"):
        raise AdapterError("invalid_params", "view must be 'messages' or 'activity'")
    limit = _int_arg(args, "limit", low=1, high=ctx.policy.limits["read_page_limit"],
                     default=ctx.policy.limits["read_page_limit"])
    after_position = _int_arg(args, "after_position", low=0, high=10**12)
    item_id = _str_arg(args, "item_id", max_chars=128)
    text_offset = _int_arg(args, "text_offset", low=0, high=10**9)

    # Fail closed: verify membership BEFORE fetching any thread content.
    ctx.check_deadline(deadline)
    _assert_thread_in_project(ctx, client, thread_id, project_id, deadline)

    def fetch():
        return t3pipe_read_thread(
            client, thread_id, view=view, limit=limit,
            max_chars=ctx.policy.limits["max_chars_per_item"],
            after_position=after_position, item_id=item_id,
            text_offset=text_offset,
        )

    data = _read_with_reconnect(ctx, host_cfg, fetch)

    # Belt and suspenders: if the server told us the thread's project, verify it.
    thread = data.get("thread")
    if not isinstance(thread, dict) or thread.get("projectId") != project_id:
        raise AdapterError("target_denied", "thread belongs to a different project")

    has_more = bool(data.get("hasMore"))
    return {
        "host": host_cfg.name,
        "project_id": project_id,
        "thread_id": thread_id,
        "thread_link": f"t3-thread://v1/{thread_id}",
        "view": view,
        "thread": thread,
        "recent_runs": data.get("recentRuns"),
        "items": data.get("items", []),
        "next_position": data.get("nextPosition"),
        "has_more": has_more,
        "continuation": (
            {"after_position": data.get("nextPosition")}
            if has_more else None
        ),
    }


def h_wait_thread(ctx, args, deadline):
    """Explicit status check — the short asynchronous pattern: writes return
    quickly (accepted/launched/uncertain), and the caller confirms effects
    with this bounded wait or t3_read_thread instead of holding one call
    open near the tunnel deadline."""
    host_cfg = ctx.resolve_host(args.get("host"))
    project_id = ctx.check_project(args.get("project_id"))
    thread_id = _str_arg(args, "thread_id", required=True, max_chars=128)
    run_id = _str_arg(args, "run_id", max_chars=128)
    client = ctx.client(host_cfg)

    timeout_ms = _int_arg(
        args, "timeout_ms",
        low=100, high=ctx.policy.limits["wait_max_timeout_ms"],
        default=ctx.policy.limits["wait_default_timeout_ms"],
    )

    ctx.check_deadline(deadline)
    _assert_thread_in_project(ctx, client, thread_id, project_id, deadline)

    def fetch():
        return t3pipe_wait_thread(
            client, thread_id, run_id=run_id, timeout_ms=timeout_ms)

    data = _read_with_reconnect(ctx, host_cfg, fetch)
    return {
        "host": host_cfg.name,
        "project_id": project_id,
        "thread_id": thread_id,
        "thread_link": f"t3-thread://v1/{thread_id}",
        "status": data.get("status"),
        "timed_out": bool(data.get("timedOut")),
        "run_id": data.get("runId") or run_id,
        "guidance": (
            "timed_out means still working, not failure; wait again or read "
            "the thread"
        ) if data.get("timedOut") else None,
    }


# ---------------------------------------------------------------------------
# write handlers
# ---------------------------------------------------------------------------

def h_start_thread(ctx, args, deadline):
    _require_write_enabled(ctx)
    host_cfg = ctx.resolve_host(args.get("host"))
    project_id = ctx.check_project(args.get("project_id"))
    title = _str_arg(args, "title", required=True,
                     max_chars=ctx.policy.limits["title_max_chars"])
    message = _str_arg(args, "message", required=True,
                       max_chars=ctx.policy.limits["message_max_chars"])
    rid = _request_id_arg(args)
    digest = args_digest("t3_start_thread", args, host=host_cfg.name,
                         subject=ctx.auth_subject)
    client = ctx.client(host_cfg)
    _authorize_backend_project(ctx, host_cfg, client, project_id, deadline)

    # Atomic recall + write-ahead begin: durable across crashes/restarts, and
    # deterministic against concurrent same-id duplicates in this process.
    prev = ctx.begin_write_checked("t3_start_thread", rid, digest)
    if prev is not None:
        kind, record = prev
        if kind == "conflict":
            _raise_conflict(rid)
        if kind == "in_progress":
            return {"outcome": "in_progress", "client_request_id": rid,
                    "guidance": "an identical write with this "
                                "client_request_id is executing now"}
        if kind == "uncertain" or record.get("outcome") == "uncertain":
            # Crashed mid-write, or a lost response was already recorded.
            # NEVER auto-relaunch: a title search finding nothing proves nothing.
            return _uncertain_replay_response(rid, record, UNCERTAIN_LAUNCH_GUIDANCE)
        return {"outcome": "cached", "client_request_id": rid,
                "previous": _replay_summary(record)}

    try:
        result = t3pipe_start_thread(
            client=client, host_name=host_cfg.name,
            project_id=project_id, title=title, message=message,
            project_id_is_authorized=True,
        )
    except Uncertain as exc:
        ctx.finish_write(rid, "uncertain",
                         {"host": host_cfg.name, "project_id": project_id})
        return {
            "outcome": "uncertain",
            "client_request_id": rid,
            "host": host_cfg.name,
            "project_id": project_id,
            "guidance": UNCERTAIN_LAUNCH_GUIDANCE,
            "details": exc.details,
        }
    except T3Error as exc:
        # A generic backend/MCP error is not proof that the write had no side
        # effect. Preserve the id as uncertain; only pre-submission checks
        # happen before the journal record is created.
        ctx.finish_write(rid, "uncertain",
                         {"host": host_cfg.name, "project_id": project_id})
        return {
            "outcome": "uncertain",
            "client_request_id": rid,
            "host": host_cfg.name,
            "project_id": project_id,
            "guidance": UNCERTAIN_LAUNCH_GUIDANCE,
            "details": _t3_code_for(exc),
        }

    outcome = "launched" if result.get("launched") else "duplicate"
    ctx.finish_write(rid, outcome, {
        "host": host_cfg.name,
        "project_id": project_id,
        "thread_id": result.get("thread_id"),
        "run_id": result.get("run_id"),
    })
    return {
        "outcome": outcome,
        "client_request_id": rid,
        "host": host_cfg.name,
        "project_id": project_id,
        "thread_id": result.get("thread_id"),
        "thread_link": result.get("thread_link"),
        "status": result.get("status"),
        "run_id": result.get("run_id"),
        "reason": result.get("reason"),
    }


def h_send_thread(ctx, args, deadline):
    _require_write_enabled(ctx)
    host_cfg = ctx.resolve_host(args.get("host"))
    project_id = ctx.check_project(args.get("project_id"))
    thread_id = _str_arg(args, "thread_id", required=True, max_chars=128)
    message = _str_arg(args, "message", required=True,
                       max_chars=ctx.policy.limits["message_max_chars"])
    mode = args.get("mode", "queue")
    if mode not in SEND_MODES:
        raise AdapterError("invalid_params",
                           "mode must be one of " + ", ".join(SEND_MODES))
    rid = _request_id_arg(args)
    digest = args_digest("t3_send_thread", args, host=host_cfg.name,
                         subject=ctx.auth_subject)

    client = ctx.client(host_cfg)
    # Membership is a definitive pre-submission check. Do it before writing a
    # new pending record so a rejected target does not poison its request id.
    ctx.check_deadline(deadline)
    _assert_thread_in_project(ctx, client, thread_id, project_id, deadline)

    recovered = False
    prev = ctx.begin_write_checked("t3_send_thread", rid, digest)
    if prev is not None:
        kind, record = prev
        if kind == "conflict":
            _raise_conflict(rid)
        if kind == "in_progress":
            return {"outcome": "in_progress", "client_request_id": rid,
                    "guidance": "an identical write with this "
                                "client_request_id is executing now"}
        if kind == "uncertain" or record.get("outcome") == "uncertain":
            # Safe to proceed, unlike a launch: T3 deduplicates sends by
            # client_request_id, so re-issuing under the SAME id cannot
            # create a duplicate turn. This is the recovery path.
            recovered = True
        else:  # cached
            return {"outcome": "cached", "client_request_id": rid,
                    "previous": _replay_summary(record)}

    try:
        ctx.check_deadline(deadline)
    except AdapterError as exc:
        ctx.finish_write(rid, "uncertain",
                         {"host": host_cfg.name, "project_id": project_id,
                          "thread_id": thread_id})
        return {
            "outcome": "uncertain",
            "client_request_id": rid,
            "host": host_cfg.name,
            "project_id": project_id,
            "thread_id": thread_id,
            "thread_link": f"t3-thread://v1/{thread_id}",
            "guidance": UNCERTAIN_SEND_GUIDANCE,
            "details": {"code": exc.code, "message": str(exc)},
        }
    try:
        result = t3pipe_send(
            client, host_cfg.name, thread_id=thread_id, message=message,
            mode=mode, client_request_id=rid,
        )
    except Uncertain as exc:
        ctx.finish_write(rid, "uncertain",
                         {"host": host_cfg.name, "project_id": project_id,
                          "thread_id": thread_id})
        return {
            "outcome": "uncertain",
            "client_request_id": rid,
            "host": host_cfg.name,
            "project_id": project_id,
            "thread_id": thread_id,
            "thread_link": f"t3-thread://v1/{thread_id}",
            "guidance": UNCERTAIN_SEND_GUIDANCE,
            "details": exc.details,
        }
    except T3Error as exc:
        ctx.finish_write(rid, "uncertain",
                         {"host": host_cfg.name, "project_id": project_id,
                          "thread_id": thread_id})
        return {
            "outcome": "uncertain",
            "client_request_id": rid,
            "host": host_cfg.name,
            "project_id": project_id,
            "thread_id": thread_id,
            "thread_link": f"t3-thread://v1/{thread_id}",
            "guidance": UNCERTAIN_SEND_GUIDANCE,
            "details": _t3_code_for(exc),
        }

    ctx.finish_write(rid, "accepted", {
        "host": host_cfg.name,
        "project_id": project_id,
        "thread_id": thread_id,
        "run_id": result.get("run_id"),
    })
    return {
        "outcome": "accepted",
        "recovered_from_uncertain": recovered,
        "client_request_id": rid,
        "host": host_cfg.name,
        "project_id": project_id,
        "thread_id": thread_id,
        "thread_link": f"t3-thread://v1/{thread_id}",
        "mode": mode,
        "delivery": result.get("delivery"),
        "run_id": result.get("run_id"),
        "task_id": result.get("task_id"),
        "message_id": result.get("message_id"),
        "status": result.get("status"),
    }


def h_interrupt_thread(ctx, args, deadline):
    _require_write_enabled(ctx)
    host_cfg = ctx.resolve_host(args.get("host"))
    project_id = ctx.check_project(args.get("project_id"))
    thread_id = _str_arg(args, "thread_id", required=True, max_chars=128)
    run_id = _str_arg(args, "run_id", max_chars=128)
    reason = _str_arg(args, "reason", max_chars=ctx.policy.limits["reason_max_chars"])
    client = ctx.client(host_cfg)

    ctx.check_deadline(deadline)
    _assert_thread_in_project(ctx, client, thread_id, project_id, deadline)

    try:
        result = t3pipe_interrupt(
            client, host_cfg.name, thread_id=thread_id, run_id=run_id,
            reason=reason,
        )
    except Uncertain as exc:  # future-proof: interrupt currently has no dedupe path
        return {
            "outcome": "uncertain",
            "client_request_id": None,
            "host": host_cfg.name,
            "thread_id": thread_id,
            "guidance": (
                "interrupt outcome unknown; interrupt is idempotent, so after "
                "re-reading the thread's status it is safe to re-issue."
            ),
            "details": exc.details,
        }
    except ConnectionError as exc:
        return {
            "outcome": "uncertain",
            "host": host_cfg.name,
            "project_id": project_id,
            "thread_id": thread_id,
            "guidance": (
                "interrupt request was sent but not confirmed; interrupt is "
                "idempotent, so after re-reading the thread's status it is "
                "safe to re-issue."
            ),
            "details": exc.details,
        }
    except T3Error as exc:
        raise AdapterError(**_t3_code_for(exc)) from exc

    return {
        "outcome": "completed",
        "host": host_cfg.name,
        "project_id": project_id,
        "thread_id": thread_id,
        "thread_link": f"t3-thread://v1/{thread_id}",
        "run_id": run_id,
        "status": result.get("status"),
        "note": "terminal or idle threads return without side effect",
    }


# ---------------------------------------------------------------------------
# dispatch (scope enforcement for every tool, reads included)
# ---------------------------------------------------------------------------

HANDLERS = {
    "t3_health": h_health,
    "t3_list_projects": h_list_projects,
    "t3_list_threads": h_list_threads,
    "t3_read_thread": h_read_thread,
    "t3_wait_thread": h_wait_thread,
    "t3_start_thread": h_start_thread,
    "t3_send_thread": h_send_thread,
    "t3_interrupt_thread": h_interrupt_thread,
}

READ_TOOLS = frozenset((
    "t3_health", "t3_list_projects", "t3_list_threads",
    "t3_read_thread", "t3_wait_thread",
))
WRITE_TOOLS = frozenset((
    "t3_start_thread", "t3_send_thread", "t3_interrupt_thread",
))
TOOL_SCOPES = {
    "t3_health": READ_SCOPE,
    "t3_list_projects": READ_SCOPE,
    "t3_list_threads": READ_SCOPE,
    "t3_read_thread": READ_SCOPE,
    "t3_wait_thread": READ_SCOPE,
    "t3_start_thread": WRITE_SCOPE,
    "t3_send_thread": WRITE_SCOPE,
    "t3_interrupt_thread": WRITE_SCOPE,
}


def dispatch(ctx, name, args, deadline, token):
    """Single entry point for every tools/call. Enforces, in order:

    1. the tool exists,
    2. writes are enabled at all (hidden + refused by name),
    3. the caller is an authenticated end user (fail closed),
    4. the token carries the scope the tool requires,
    5. the handler's own host/project/membership checks.
    """
    if name not in HANDLERS:
        raise AdapterError("unknown_tool", f"unknown tool {name!r}")
    if name in WRITE_TOOLS and not ctx.policy.allow_writes:
        # Also enforced in specs (hidden from tools/list); double-check here.
        raise AdapterError(
            "writes_disabled",
            "this adapter has allow_writes=false; start/send/interrupt are disabled")
    if not isinstance(args, dict):
        raise AdapterError("invalid_params", "arguments must be an object")

    auth = ctx.authenticate(token)
    required_scope = TOOL_SCOPES[name]
    if not auth.has_scope(required_scope):
        raise AdapterError(
            "auth_insufficient_scope",
            f"token lacks the {required_scope} scope required by {name}",
            details={"required_scope": required_scope, "subject": auth.subject},
        )
    had_subject = hasattr(ctx._request_state, "subject")
    prior_subject = getattr(ctx._request_state, "subject", None)
    ctx._request_state.subject = auth.subject
    try:
        return HANDLERS[name](ctx, args, deadline)
    finally:
        if had_subject:
            ctx._request_state.subject = prior_subject
        else:
            del ctx._request_state.subject


# ---------------------------------------------------------------------------
# tool specs (JSON Schema subset + MCP annotations)
# ---------------------------------------------------------------------------

_READ_ANNOTATIONS = {"readOnlyHint": True, "openWorldHint": False}

_PROJECT_ID_SCHEMA = {
    "type": "string",
    "description": (
        "Project UUID from t3_list_projects. Only allowlisted projects are "
        "accepted; anything else is refused."
    ),
}


def _str_schema(required=False, description=None, max_items=None, enum=None):
    schema = {"type": "string"}
    if description:
        schema["description"] = description
    if enum:
        schema["enum"] = list(enum)
    return schema


def _base_write_schema(description, extra_required, props):
    return {
        "type": "object",
        "description": description,
        "properties": {
            "project_id": dict(_PROJECT_ID_SCHEMA),
            **props,
        },
        "required": ["project_id", *extra_required],
        "additionalProperties": False,
    }


def tool_specs(allow_writes):
    specs = [
        {
            "name": "t3_health",
            "description": (
                "Read-only health check of the private T3 controller: confirms "
                "the T3 credential works and reports environment/server info. "
                "Safe, cheap, and the first call any new consumer should make."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": dict(_READ_ANNOTATIONS, idempotentHint=True),
        },
        {
            "name": "t3_list_projects",
            "description": (
                "Read-only: list the T3 projects this adapter is allowed to "
                "expose (policy-filtered). Returns stable project UUIDs."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": dict(_READ_ANNOTATIONS, idempotentHint=True),
        },
        {
            "name": "t3_list_threads",
            "description": (
                "Read-only: list threads of an allowlisted project, newest "
                "first. Paginated: pass next_cursor as cursor to continue. "
                "Each entry carries the stable threadId; pair it with this "
                "host+project in later calls."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": dict(_PROJECT_ID_SCHEMA),
                    "title_contains": _str_schema(description="Optional title filter"),
                    "statuses": {
                        "type": "array", "items": {"type": "string", "enum": list(THREAD_STATUSES)},
                        "description": "Optional status filter",
                    },
                    "settled": {"type": "boolean", "description": "Optional settled filter"},
                    "limit": {"type": "integer", "minimum": 1},
                    "cursor": _str_schema(description="next_cursor from a previous page"),
                },
                "required": ["project_id"],
                "additionalProperties": False,
            },
            "annotations": dict(_READ_ANNOTATIONS, idempotentHint=True),
        },
        {
            "name": "t3_read_thread",
            "description": (
                "Read-only: read a thread's messages or activity timeline "
                "(paginated). Requires the project so the adapter can verify "
                "the thread belongs to it before returning any content. "
                "Continue with next_position (after_position), or item_id + "
                "text_offset for a truncated item."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": dict(_PROJECT_ID_SCHEMA),
                    "thread_id": _str_schema(description="Stable threadId from t3_list_threads"),
                    "view": {"type": "string", "enum": ["messages", "activity"]},
                    "limit": {"type": "integer", "minimum": 1},
                    "after_position": {"type": "integer", "minimum": 0, "description": "Continuation cursor from a previous page"},
                    "item_id": _str_schema(description="Long-item continuation: item id"),
                    "text_offset": {"type": "integer", "minimum": 0, "description": "Long-item continuation: UTF-16 offset"},
                },
                "required": ["project_id", "thread_id"],
                "additionalProperties": False,
            },
            "annotations": dict(_READ_ANNOTATIONS, idempotentHint=True),
        },
        {
            "name": "t3_wait_thread",
            "description": (
                "Read-only status check: bounded wait for a thread's current "
                "run to reach a terminal state. timed_out=true means still "
                "working, not failure — wait again or read the thread. This "
                "is the explicit status check to use after a write returns "
                "accepted/launched/uncertain, instead of holding calls open."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project_id": dict(_PROJECT_ID_SCHEMA),
                    "thread_id": _str_schema(description="Stable threadId"),
                    "run_id": _str_schema(description="Optional run id; defaults to the newest run"),
                    "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 60000,
                                   "description": "Bounded wait (default 5000, max 60000)"},
                },
                "required": ["project_id", "thread_id"],
                "additionalProperties": False,
            },
            "annotations": dict(_READ_ANNOTATIONS, idempotentHint=True),
        },
    ]
    if allow_writes:
        specs += [
            {
                "name": "t3_start_thread",
                "description": (
                    "WRITE (requires an end-user token with the t3:write "
                    "scope): start a new T3 thread for an allowlisted project "
                    "with a clear task message. Deduplicates against active "
                    "threads with the same title and against a repeated "
                    "client_request_id (durably journaled across restarts). "
                    "Returns outcome launched/duplicate/cached/in_progress/"
                    "uncertain with stable ids (thread_id, thread_link). If "
                    "uncertain, verify state before retrying — absence of a "
                    "matching title does not prove failure. Threads always "
                    "start under T3's own approval model."
                ),
                "inputSchema": _base_write_schema(
                    "Start a T3 thread (write).",
                    ["title", "message"],
                    {
                        "title": _str_schema(description="Short, unique title for the thread"),
                        "message": _str_schema(description="Full task message for the thread"),
                        "client_request_id": _str_schema(
                            description="Optional idempotency key (8-128 chars, [A-Za-z0-9_-]); "
                                        "reusing a previous key returns the recorded outcome "
                                        "instead of launching again"
                        ),
                    },
                ),
                "annotations": {
                    "readOnlyHint": False, "destructiveHint": False,
                    "idempotentHint": False, "openWorldHint": False,
                },
            },
            {
                "name": "t3_send_thread",
                "description": (
                    "WRITE (requires an end-user token with the t3:write "
                    "scope): send a message to an existing T3 thread. Modes: "
                    "queue (default; separate follow-up turn), auto "
                    "(start/steer/queue as fits), steer (join the running "
                    "turn), restart (interrupt and restart the turn). Returns "
                    "outcome accepted/uncertain; on uncertain, re-read and "
                    "resend with the SAME client_request_id (T3 deduplicates "
                    "sends by it)."
                ),
                "inputSchema": _base_write_schema(
                    "Send a message to a T3 thread (write).",
                    ["thread_id", "message"],
                    {
                        "thread_id": _str_schema(description="Stable threadId"),
                        "message": _str_schema(description="Message text for the thread"),
                        "mode": {"type": "string", "enum": list(SEND_MODES)},
                        "client_request_id": _str_schema(
                            description="Optional idempotency key; generated when omitted. "
                                        "Reuse it verbatim when retrying an uncertain send"
                        ),
                    },
                ),
                "annotations": {
                    "readOnlyHint": False, "destructiveHint": False,
                    "idempotentHint": False, "openWorldHint": False,
                },
            },
            {
                "name": "t3_interrupt_thread",
                "description": (
                    "WRITE (requires an end-user token with the t3:write "
                    "scope): request interruption of a running turn in a T3 "
                    "thread. Idempotent: terminal or idle threads return "
                    "without side effect. Returns outcome completed/uncertain; "
                    "on uncertain it is safe to re-issue after re-reading status."
                ),
                "inputSchema": _base_write_schema(
                    "Interrupt a running turn in a T3 thread (write).",
                    ["thread_id"],
                    {
                        "thread_id": _str_schema(description="Stable threadId"),
                        "run_id": _str_schema(description="Optional run id; defaults to the newest interruptible run"),
                        "reason": _str_schema(description="Optional human-readable reason"),
                    },
                ),
                "annotations": {
                    "readOnlyHint": False, "destructiveHint": True,
                    "idempotentHint": True, "openWorldHint": False,
                },
            },
        ]
    return specs


def spec_names(allow_writes):
    return [s["name"] for s in tool_specs(allow_writes)]
