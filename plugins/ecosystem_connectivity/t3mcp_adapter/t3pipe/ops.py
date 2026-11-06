"""High-level operations over the T3 thread interface.

These encode the safety rules the caller depends on:

* **Explicit targets.** ``start`` needs a project; ``send``/``read``/``wait``/
  ``interrupt`` need a thread; every call names its host. Nothing is resolved
  implicitly from "the current thread".
* **Dedupe before launch.** ``start`` inspects existing threads first and
  returns an active title match. This is best-effort, non-atomic dedupe;
  concurrent callers can still launch duplicates.
* **No auto-approval.** Broadening ``runtimeMode`` (``auto``/``auto-accept-edits``/
  ``full-access``) requires ``allow_auto_approve=True``.
* **Verify before retry.** A lost launch/send response is resolved by reading
  state, never by blind retry (there is no idempotency key on launch).
* **Re-read on reconnect.** State is never cached across calls; every operation
  reads fresh from the server.
"""

import uuid

from .client import T3Client
from .errors import (
    ConnectionError,
    ProtocolError,
    ScopeDenied,
    TargetRequired,
    Uncertain,
)

# Thread statuses that represent active work (not finished/archived).
ACTIVE_STATUSES = ("running", "queued", "starting", "preparing", "waiting")

# runtimeMode values that imply the agent may approve its own actions.
AUTO_APPROVE_MODES = ("auto", "auto-accept-edits", "full-access")


def _thread_link(thread_id):
    return f"t3-thread://v1/{thread_id}"


def discover(client, host_name):
    """Server info + projects + environment id for one host. Read-only."""
    caps = client.call_tool("orchestrator_capabilities", {}).data or {}
    proj = client.call_tool("t3_project_list", {}).data or {}
    env = {}
    try:
        env = client.call_tool("t3_environment_read", {}).data or {}
    except Exception:  # noqa: BLE001 — environment read is optional
        pass
    return {
        "host": host_name,
        "environment_id": env.get("environmentId"),
        "environment_label": env.get("label"),
        "server_version": env.get("serverVersion"),
        "server": _server_info(caps),
        "projects": proj.get("projects", []),
    }


def _server_info(caps):
    return {
        "providers": caps.get("providers"),
        "parent_thread_id": caps.get("parentThreadId"),
        "inherited_model": caps.get("inheritedModel"),
    }


def projects(client):
    """List projects on a host. Read-only."""
    return client.call_tool("t3_project_list", {}).data or {}


def resolve_project_id(client, project):
    """Resolve a project id (or title) to a stable UUID. Raises if ambiguous."""
    data = projects(client)
    all_projects = data.get("projects", [])
    if not project:
        raise TargetRequired("a project id or title is required", details={"project": None})

    exact = [p for p in all_projects if p.get("id") == project]
    if exact:
        return project

    by_title = [p for p in all_projects if p.get("title") == project]
    if len(by_title) == 1:
        return by_title[0]["id"]
    if len(by_title) > 1:
        raise TargetRequired(
            f"ambiguous project title {project!r}; use the project id",
            details={"project": project, "matches": [p["id"] for p in by_title]},
        )

    fuzzy = [p for p in all_projects if project.lower() in (p.get("title") or "").lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]["id"]
    raise TargetRequired(
        f"project {project!r} not found", details={"project": project}
    )


def list_threads(
    client,
    project_id=None,
    *,
    title_contains=None,
    statuses=None,
    settled=None,
    limit=50,
    cursor=None,
):
    """List threads. A caller outside a thread must pass ``project_id``.
    ``cursor`` is the opaque ``nextCursor`` from a previous page."""
    args = {"limit": limit}
    if project_id:
        args["projectId"] = project_id
    if title_contains:
        args["titleContains"] = title_contains
    if statuses:
        args["statuses"] = list(statuses)
    if settled is not None:
        args["settled"] = bool(settled)
    if cursor:
        args["cursor"] = cursor
    return client.call_tool("t3_thread_list", args).data or {}


def search_threads(client, query, project_id=None, limit=10):
    args = {"query": query, "limit": limit}
    if project_id:
        args["projectId"] = project_id
    return client.call_tool("t3_thread_search", args).data or {}


def read_thread(client, thread_id, *, view="messages", limit=50, max_chars=20000,
                 after_position=None, item_id=None, text_offset=None):
    """Read a thread's messages or activity. Always reads fresh (no cache).
    ``after_position`` continues from a previous page (``nextPosition``);
    ``item_id`` + ``text_offset`` continue a truncated item's text."""
    if not thread_id:
        raise TargetRequired("a thread id is required", details={"thread_id": None})
    args = {"threadId": thread_id, "view": view, "limit": limit}
    if after_position is not None:
        args["afterPosition"] = after_position
    if item_id is not None:
        args["itemId"] = item_id
        args["textOffset"] = text_offset or 0
    if max_chars:
        args["maxCharsPerItem"] = max_chars
    data = client.call_tool("t3_thread_read", args).data or {}
    data["thread_link"] = _thread_link(thread_id)
    return data


def wait_thread(client, thread_id, *, run_id=None, timeout_ms=60000):
    """Bounded wait. Returns status; ``timed_out`` means still working."""
    if not thread_id:
        raise TargetRequired("a thread id is required", details={"thread_id": None})
    args = {"threadId": thread_id}
    if run_id:
        args["runId"] = run_id
    if timeout_ms:
        args["timeoutMs"] = timeout_ms
    # The wait may legitimately run for ``timeout_ms``; the HTTP transport
    # timeout must exceed that (default 30s < 60s previously dropped the call).
    http_timeout = (max(1, (timeout_ms or 0) // 1000) + 10) if timeout_ms else None
    data = client.call_tool("t3_thread_wait", args, timeout=http_timeout).data or {}
    data["thread_link"] = _thread_link(thread_id)
    return data


def _find_active_match(client, project_id, title):
    """Look for an active thread that already owns this work (by title).
    Returns a thread summary dict or None."""
    active = None

    by_title = list_threads(client, project_id, title_contains=title, limit=50)
    for t in by_title.get("threads", []):
        if t.get("title") == title and t.get("status") in ACTIVE_STATUSES:
            active = t

    if active is None:
        # Search may surface a thread whose title differs but whose task matches.
        search = search_threads(client, title, project_id=project_id, limit=10)
        for m in search.get("matches", []):
            tid = m.get("threadId")
            # Confirm live status with a list (search results carry no status).
            for t in by_title.get("threads", []):
                if t.get("threadId") == tid and t.get("status") in ACTIVE_STATUSES:
                    active = t

    return active


def start_thread(
    client,
    host_name,
    *,
    project_id,
    title,
    message,
    workspace_strategy=None,
    model_selection=None,
    runtime_mode=None,
    allow_auto_approve=False,
    dry_run=False,
    project_id_is_authorized=False,
):
    """Start a thread for a project with a clear task. Dedupes first."""
    if not host_name:
        raise TargetRequired("a host name is required", details={"host": None})
    if not title or not message:
        raise TargetRequired("title and message are required to start a thread")

    if runtime_mode in AUTO_APPROVE_MODES and not allow_auto_approve:
        raise ScopeDenied(
            f"runtimeMode {runtime_mode!r} lets the agent approve its own actions; "
            "pass allow_auto_approve=True explicitly to opt in",
            details={"runtime_mode": runtime_mode},
        )

    # Adapter callers may already have bound this exact UUID to the backend
    # and policy. Do not resolve it again: a changed project list could turn a
    # previously authorized target into a title-based redirect.
    resolved_project = project_id if project_id_is_authorized \
        else resolve_project_id(client, project_id)

    existing = _find_active_match(client, resolved_project, title)
    if existing:
        return {
            "launched": False,
            "reason": "duplicate",
            "host": host_name,
            "project_id": resolved_project,
            "thread_id": existing["threadId"],
            "thread_link": _thread_link(existing["threadId"]),
            "status": existing["status"],
            "title": existing["title"],
        }

    plan = {
        "host": host_name,
        "project_id": resolved_project,
        "title": title,
        "workspace_strategy": workspace_strategy,
        "runtime_mode": runtime_mode,
        "model_selection": model_selection,
    }
    if dry_run:
        return {"launched": False, "reason": "dry_run", **plan}

    args = {
        "title": title,
        "message": message,
        "projectId": resolved_project,
    }
    if workspace_strategy is not None:
        args["workspaceStrategy"] = workspace_strategy
    if model_selection is not None:
        args["modelSelection"] = model_selection
    if runtime_mode is not None:
        args["runtimeMode"] = runtime_mode

    try:
        result = client.call_tool("t3_thread_launch", args).data
        if not isinstance(result, dict) or not isinstance(result.get("threadId"), str) or not result["threadId"]:
            raise ProtocolError("launch response missing valid threadId")
    except (ConnectionError, ProtocolError) as exc:
        # Launch response lost. There is no server-side retry key for launches,
        # so a same-title match could belong to another client. Preserve an
        # uncertain outcome rather than claiming it.
        return _recover_launch(client, host_name, resolved_project, title, exc)

    thread_id = result["threadId"]

    return {
        "launched": True,
        "host": host_name,
        "project_id": resolved_project,
        "thread_id": thread_id,
        "thread_link": _thread_link(thread_id),
        "status": result.get("status"),
        "run_id": result.get("runId"),
    }


def _recover_launch(client, host_name, project_id, title, cause,
                    *, correlation_supported=False):
    """Resolve an uncertain launch.

    T3's ``t3_thread_launch`` has no idempotency/correlation key, so we cannot
    reliably tell whether a new same-title thread is ours or another client's.
    Any candidate is reported as diagnostic context only; the outcome stays
    uncertain and the caller must inspect state before retrying.
    """
    candidates = []
    try:
        found = _find_active_match(client, project_id, title)
        if found is not None:
            candidates.append(found.get("threadId"))
    except Exception:  # noqa: BLE001 — recovery reads must not crash/claim
        candidates = []
    raise Uncertain(
        "launch outcome unknown; inspect t3_thread_list before re-running "
        "(no reliable correlation marker is available for launches)",
        details={
            "cause": getattr(cause, "code", None) if cause is not None else None,
            "title": title,
            "host": host_name,
            "project_id": project_id,
            "candidates": candidates,
        },
    )


def send(
    client,
    host_name,
    *,
    thread_id,
    message,
    mode="auto",
    client_request_id=None,
):
    """Send a follow-up / steer to an existing thread. Requires an explicit
    thread id and host."""
    if not host_name:
        raise TargetRequired("a host name is required", details={"host": None})
    if not thread_id:
        raise TargetRequired("a thread id is required", details={"thread_id": None})
    if mode not in ("auto", "queue", "steer", "restart"):
        raise TargetRequired(f"invalid mode {mode!r}", details={"mode": mode})

    # Default to a fresh idempotency key so retries (after a lost response)
    # can reuse it to avoid creating a duplicate turn. Never send without one.
    if not client_request_id:
        client_request_id = uuid.uuid4().hex

    args = {"threadId": thread_id, "message": message, "mode": mode,
            "clientRequestId": client_request_id}

    try:
        result = client.call_tool("t3_thread_send", args).data
        if not isinstance(result, dict) or not isinstance(result.get("delivery"), str) or not result["delivery"]:
            raise ProtocolError("send response missing valid delivery")
    except (ConnectionError, ProtocolError) as exc:
        # Response lost: preserve uncertainty (incl. the key for retry) instead
        # of silently dropping the classification. Never blind-retry.
        raise Uncertain(
            "send outcome unknown; re-read the thread and resend with the same "
            "client_request_id to avoid a duplicate turn",
            details={
                "cause": getattr(exc, "code", None),
                "thread_id": thread_id,
                "client_request_id": client_request_id,
            },
        ) from exc

    return {
        "host": host_name,
        "thread_id": thread_id,
        "thread_link": _thread_link(thread_id),
        "delivery": result.get("delivery"),
        "run_id": result.get("runId"),
        "task_id": result.get("taskId"),
        "message_id": result.get("messageId"),
        "status": result.get("status"),
        "client_request_id": client_request_id,
    }


def interrupt(client, host_name, *, thread_id, run_id=None, reason=None):
    if not host_name:
        raise TargetRequired("a host name is required", details={"host": None})
    if not thread_id:
        raise TargetRequired("a thread id is required", details={"thread_id": None})
    args = {"threadId": thread_id}
    if run_id:
        args["runId"] = run_id
    if reason:
        args["reason"] = reason
    result = client.call_tool("t3_thread_interrupt", args).data or {}
    return {
        "host": host_name,
        "thread_id": thread_id,
        "thread_link": _thread_link(thread_id),
        "status": result.get("status"),
    }
