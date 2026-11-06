"""Offline unit tests for the hardened t3mcp tool layer.

Handler-level tests (host/project/membership enforcement, dedupe, journal
recovery, timeouts). End-user authentication is enforced in ``dispatch``
and covered by test_t3mcp_auth.py; here handlers are exercised directly to
test their own logic. All backend interaction goes through FakeT3.
"""

import os
import tempfile
import threading
import time

import pytest

from t3mcp import AdapterError, Policy, PolicyError, ToolContext
from t3mcp.journal import Journal, args_digest
from t3mcp.tools import (
    HANDLERS,
    dispatch,
    h_health,
    h_interrupt_thread,
    h_list_projects,
    h_list_threads,
    h_read_thread,
    h_send_thread,
    h_start_thread,
    h_wait_thread,
    tool_specs,
)
from t3pipe.errors import AuthError, ConnectionError, ToolError

from helpers_t3mcp import (
    DEADLINE, OTHER, PROJ, PROJECTS, THREADS, args, make_cfg, make_ctx,
)
from fake_t3 import BlockingT3, FakeResult, FakeT3


# ---------------------------------------------------------------------------
# policy validation (fail closed)
# ---------------------------------------------------------------------------

def test_policy_requires_hosts():
    with pytest.raises(PolicyError):
        Policy({"allowed_projects": [PROJ], "auth": {"required": False}})


def test_policy_requires_projects():
    with pytest.raises(PolicyError):
        Policy({"hosts": {"w": {"url": "http://x/mcp"}},
                "auth": {"required": False}})


def test_policy_rejects_writes_non_bool():
    with pytest.raises(PolicyError):
        Policy({"hosts": {"w": {"url": "http://x/mcp"}},
                "allowed_projects": [PROJ], "allow_writes": "yes",
                "auth": {"required": False}})


def test_policy_rejects_unknown_limit():
    with pytest.raises(PolicyError):
        Policy({"hosts": {"w": {"url": "http://x/mcp"}},
                "allowed_projects": [PROJ],
                "limits": {"nonsense": 5}, "auth": {"required": False}})


def test_policy_rejects_bad_limit_value():
    with pytest.raises(PolicyError):
        Policy({"hosts": {"w": {"url": "http://x/mcp"}},
                "allowed_projects": [PROJ],
                "limits": {"list_page_size": -1}, "auth": {"required": False}})


def test_policy_without_auth_section_fails_closed():
    # auth is REQUIRED by default; nothing runs silently unauthenticated
    with pytest.raises(PolicyError):
        Policy({"hosts": {"w": {"url": "http://x/mcp"}},
                "allowed_projects": [PROJ]})


def test_policy_rejects_unknown_host():
    ctx, _ = make_ctx()
    with pytest.raises(AdapterError) as exc:  # runtime denial, stable code
        ctx.resolve_host("not-a-host")
    assert exc.value.code == "target_denied"


def test_writes_enabled_requires_journal():
    with pytest.raises(PolicyError):
        Policy(make_cfg(allow_writes=True, journal_path=None,
                        auth_override={"required": False}))


def test_missing_token_fails_closed(monkeypatch):
    monkeypatch.delenv("T3_TOKEN_TEST", raising=False)
    ctx, _ = make_ctx(default_factory=True)  # real factory: reads the (unset) env
    host = ctx.resolve_host("workstation")
    with pytest.raises(AuthError):  # never a silent fallback credential
        ctx.client(host)


# ---------------------------------------------------------------------------
# read tools
# ---------------------------------------------------------------------------

def test_health_ok():
    ctx, fake = make_ctx()
    r = h_health(ctx, {}, DEADLINE)
    assert r["status"] == "ok"
    assert r["host"] == "workstation"
    assert r["write_enabled"] is False
    assert r["end_user_auth_required"] is True


def test_health_maps_auth_failure():
    ctx, fake = make_ctx()
    fake.fail["t3_environment_read"] = AuthError("401: bad credential")
    with pytest.raises(AuthError):
        h_health(ctx, {}, DEADLINE)


def test_list_projects_filters_to_allowlist():
    ctx, fake = make_ctx()
    r = h_list_projects(ctx, {}, DEADLINE)
    assert [p["id"] for p in r["projects"]] == [PROJ]


def test_list_threads_requires_allowed_project():
    ctx, fake = make_ctx()
    with pytest.raises(AdapterError) as exc:
        h_list_threads(ctx, {"project_id": OTHER}, DEADLINE)
    assert exc.value.code == "target_denied"
    # the error must not echo allowlist contents
    assert OTHER not in str(exc.value)


def test_list_threads_pagination_passthrough():
    ctx, fake = make_ctx()
    # more threads than the page size to force a cursor
    for i in range(3, 8):
        fake.threads.append({"threadId": f"thread-{i}", "projectId": PROJ,
                             "title": f"Job {i}", "status": "idle"})
    r1 = h_list_threads(ctx, {"project_id": PROJ, "limit": 2}, DEADLINE)
    assert len(r1["threads"]) == 2
    assert r1["has_more"] is True
    r2 = h_list_threads(ctx, {"project_id": PROJ, "limit": 2,
                              "cursor": r1["next_cursor"]}, DEADLINE)
    assert r2["threads"][0]["threadId"] != r1["threads"][0]["threadId"]
    # limit is clamped to the policy page size
    r3 = h_list_threads(ctx, {"project_id": PROJ, "limit": 10_000}, DEADLINE)
    sent = [a for n, a in fake.calls if n == "t3_thread_list"][-1]
    assert sent["limit"] == ctx.policy.limits["list_page_size"]


def test_list_threads_rejects_backend_project_escape():
    ctx, fake = make_ctx()
    real = fake.call_tool

    def poisoned(name, args=None, timeout=None, **kwargs):
        if name == "t3_thread_list":
            return FakeResult({
                "threads": [{"threadId": "secret-thread", "projectId": OTHER}],
                "nextCursor": None,
                "total": 1,
            })
        return real(name, args, timeout, **kwargs)

    fake.call_tool = poisoned
    with pytest.raises(AdapterError) as exc:
        h_list_threads(ctx, {"project_id": PROJ}, DEADLINE)
    assert exc.value.code == "target_denied"


def test_start_rejects_conflicting_backend_project_before_launch():
    ctx, fake = make_ctx(allow_writes=True)
    real = fake.call_tool

    def conflicting(name, args=None, timeout=None, **kwargs):
        if name == "t3_project_list":
            return FakeResult({"projects": [{"id": OTHER, "title": "project"}]})
        return real(name, args, timeout, **kwargs)

    fake.call_tool = conflicting
    with pytest.raises(AdapterError) as exc:
        h_start_thread(ctx, args(title="Conflicting title", message="safe"), DEADLINE)
    assert exc.value.code == "target_denied"
    assert fake.launches == []


def test_start_does_not_reresolve_authorized_project():
    ctx, fake = make_ctx(allow_writes=True)
    real = fake.call_tool
    project_lists = [
        {"projects": [{"id": PROJ, "title": "project"}]},
        {"projects": [{"id": OTHER, "title": PROJ}]},
    ]

    def changing(name, args=None, timeout=None, **kwargs):
        if name == "t3_project_list":
            return FakeResult(project_lists.pop(0))
        return real(name, args, timeout, **kwargs)

    fake.call_tool = changing
    result = h_start_thread(ctx, args(title="No redirect", message="safe"), DEADLINE)
    assert result["outcome"] == "launched"
    assert fake.launches[0]["projectId"] == PROJ


def test_read_thread_requires_membership():
    ctx, fake = make_ctx()
    fake.reads["thread-9"] = {"thread": {"projectId": OTHER},
                              "items": [{"position": 1, "text": "secret"}]}
    # thread-9 exists on the host but belongs to a different project
    with pytest.raises(AdapterError) as exc:
        h_read_thread(ctx, {"project_id": PROJ, "thread_id": "thread-9"}, DEADLINE)
    assert exc.value.code == "thread_not_in_project"
    # membership check runs BEFORE the read: no content call ever happened
    assert not [n for n, a in fake.calls if n == "t3_thread_read"]


def test_send_membership_denial_does_not_poison_journal():
    journal_path = os.path.join(tempfile.mkdtemp(), "journal.jsonl")
    ctx, fake = make_ctx(allow_writes=True, journal_path=journal_path)
    with pytest.raises(AdapterError) as exc:
        h_send_thread(ctx, args(project_id=PROJ, thread_id="thread-9",
                                message="nope", client_request_id="deny000001"),
                      DEADLINE)
    assert exc.value.code == "thread_not_in_project"
    assert not os.path.exists(journal_path)


def test_read_thread_denies_cross_project_thread_object():
    ctx, fake = make_ctx()
    # in the list, but the read says the project differs: belt-and-suspenders
    fake.threads.append({"threadId": "thread-10", "projectId": PROJ,
                         "title": "Sneaky", "status": "idle"})
    fake.reads["thread-10"] = {"thread": {"projectId": OTHER},
                               "items": []}
    with pytest.raises(AdapterError) as exc:
        h_read_thread(ctx, {"project_id": PROJ, "thread_id": "thread-10"}, DEADLINE)
    assert exc.value.code == "target_denied"


def test_read_thread_ok_and_paginated():
    ctx, fake = make_ctx()
    fake.reads["thread-1"] = {
        "thread": {"projectId": PROJ, "title": "Build widget"},
        "recentRuns": [],
        "items": [
            {"position": 1, "text": "first"},
            {"position": 2, "text": "second"},
        ],
        "nextPosition": 2,
        "hasMore": True,
    }
    r = h_read_thread(ctx, {"project_id": PROJ, "thread_id": "thread-1"}, DEADLINE)
    assert r["thread_id"] == "thread-1"
    assert r["thread_link"] == "t3-thread://v1/thread-1"
    assert r["has_more"] is True
    assert r["continuation"] == {"after_position": 2}

    # continue the page; continuation args reach the backend
    h_read_thread(ctx, {"project_id": PROJ, "thread_id": "thread-1",
                        "after_position": 2, "item_id": "i1",
                        "text_offset": 100}, DEADLINE)
    sent = [a for n, a in fake.calls if n == "t3_thread_read"][-1]
    assert sent["afterPosition"] == 2
    assert sent["itemId"] == "i1"
    assert sent["textOffset"] == 100


def test_read_thread_reconnects_once_on_connection_error():
    ctx, fake = make_ctx()
    fake.reads["thread-1"] = {"thread": {"projectId": PROJ}, "items": []}
    real = fake.call_tool
    state = {"dropped": False}

    def flaky(name, a=None, timeout=None, **kw):
        if name == "t3_thread_read" and not state["dropped"]:
            state["dropped"] = True
            raise ConnectionError("connection refused")
        return real(name, a, timeout, **kw)

    fake.call_tool = flaky
    r = h_read_thread(ctx, {"project_id": PROJ, "thread_id": "thread-1"}, DEADLINE)
    assert r["thread_id"] == "thread-1"


def test_wait_thread_status_check():
    ctx, fake = make_ctx()
    fake.threads.append({"threadId": "thread-1b", "projectId": PROJ,
                         "title": "Busy", "status": "running"})
    fake.waits["thread-1b"] = {"status": "running", "timedOut": True}
    r = h_wait_thread(ctx, {"project_id": PROJ, "thread_id": "thread-1b",
                            "timeout_ms": 5000}, DEADLINE)
    assert r["timed_out"] is True
    assert "not failure" in r["guidance"]
    sent = [a for n, a in fake.calls if n == "t3_thread_wait"][-1]
    assert sent["timeoutMs"] == 5000


def test_wait_thread_completed():
    ctx, fake = make_ctx()
    fake.waits["thread-1"] = {"status": "completed", "timedOut": False}
    r = h_wait_thread(ctx, {"project_id": PROJ, "thread_id": "thread-1"}, DEADLINE)
    assert r["status"] == "completed"
    assert r["guidance"] is None


def test_wait_thread_requires_membership():
    ctx, fake = make_ctx()
    with pytest.raises(AdapterError) as exc:
        h_wait_thread(ctx, {"project_id": PROJ, "thread_id": "thread-9"}, DEADLINE)
    assert exc.value.code == "thread_not_in_project"


def test_wait_thread_timeout_clamped_to_policy():
    ctx, fake = make_ctx()
    fake.waits["thread-1"] = {"status": "completed", "timedOut": False}
    h_wait_thread(ctx, {"project_id": PROJ, "thread_id": "thread-1",
                        "timeout_ms": 10_000_000}, DEADLINE)
    sent = [a for n, a in fake.calls if n == "t3_thread_wait"][-1]
    assert sent["timeoutMs"] == ctx.policy.limits["wait_max_timeout_ms"]


# ---------------------------------------------------------------------------
# write tools: policy and journal-backed idempotency
# ---------------------------------------------------------------------------

def test_writes_disabled_refuses_by_name():
    ctx, fake = make_ctx(allow_writes=False)
    with pytest.raises(AdapterError) as exc:
        h_start_thread(ctx, args(title="T", message="M"), DEADLINE)
    assert exc.value.code == "writes_disabled"
    assert fake.launches == []


def test_start_thread_launched_and_stable_ids():
    ctx, fake = make_ctx(allow_writes=True)
    r = h_start_thread(ctx, args(title="New task", message="Do the thing"), DEADLINE)
    assert r["outcome"] == "launched"
    assert r["thread_id"].startswith("thread-")
    assert r["thread_link"] == f"t3-thread://v1/{r['thread_id']}"
    assert r["project_id"] == PROJ
    assert r["client_request_id"]
    assert len(fake.launches) == 1


def test_start_thread_dedupes_on_active_title():
    ctx, fake = make_ctx(allow_writes=True)
    r = h_start_thread(ctx, args(title="Build widget", message="again"), DEADLINE)
    assert r["outcome"] == "duplicate"
    assert r["thread_id"] == "thread-1"
    assert fake.launches == []


def test_start_thread_client_request_id_dedupes_repeats():
    ctx, fake = make_ctx(allow_writes=True)
    r1 = h_start_thread(ctx, args(title="A", message="M",
                                  client_request_id="key12345678"), DEADLINE)
    assert r1["outcome"] == "launched"
    r2 = h_start_thread(ctx, args(title="A", message="M",
                                  client_request_id="key12345678"), DEADLINE)
    assert r2["outcome"] == "cached"
    assert r2["previous"]["thread_id"] == r1["thread_id"]
    assert len(fake.launches) == 1  # no second launch


def test_start_thread_same_id_different_args_is_conflict():
    ctx, fake = make_ctx(allow_writes=True)
    h_start_thread(ctx, args(title="A", message="M",
                             client_request_id="key12345678"), DEADLINE)
    with pytest.raises(AdapterError) as exc:
        h_start_thread(ctx, args(title="A", message="DIFFERENT",
                                  client_request_id="key12345678"), DEADLINE)
    assert exc.value.code == "request_id_conflict"
    assert len(fake.launches) == 1


def test_start_thread_client_request_id_ttl_expiry():
    now = [0.0]
    ctx, fake = make_ctx(allow_writes=True, wall=lambda: now[0])
    r1 = h_start_thread(ctx, args(title="A", message="M",
                                  client_request_id="key12345678"), DEADLINE)
    assert r1["outcome"] == "launched"
    now[0] = 7200  # past the TTL: the journal no longer knows the id
    with pytest.raises(AdapterError) as exc:
        h_start_thread(ctx, args(title="A", message="M",
                                 client_request_id="key12345678"), DEADLINE)
    assert exc.value.code == "journal_failed"


def test_start_thread_uncertain_on_connection_drop():
    ctx, fake = make_ctx(allow_writes=True)
    fake.fail["t3_thread_launch"] = ConnectionError("timeout reaching T3 MCP server")
    r = h_start_thread(ctx, args(title="Unclear", message="M",
                                  client_request_id="unc000001"), DEADLINE)
    assert r["outcome"] == "uncertain"
    assert r["client_request_id"] == "unc000001"
    assert "does NOT prove" in r["guidance"]


def test_start_thread_uncertain_repeat_never_relaunches():
    ctx, fake = make_ctx(allow_writes=True)
    fake.fail["t3_thread_launch"] = ConnectionError("drop")
    r1 = h_start_thread(ctx, args(title="Unclear", message="M",
                                   client_request_id="unc000002"), DEADLINE)
    assert r1["outcome"] == "uncertain"
    fake.fail.pop("t3_thread_launch")  # backend works again now
    # the SAME id repeats: replay the uncertain record, never relaunch
    r2 = h_start_thread(ctx, args(title="Unclear", message="M",
                                   client_request_id="unc000002"), DEADLINE)
    assert r2["outcome"] == "uncertain"
    assert r2["recovered_from_journal"] is True
    assert fake.launches == []  # even though no matching title exists


def test_start_thread_scope_denied_from_backend():
    ctx, fake = make_ctx(allow_writes=True)
    fake.fail["t3_thread_launch"] = ToolError(
        "credential lacks orchestration:operate", t3_code="scope_denied_operate")
    first = h_start_thread(ctx, args(title="S", message="M",
                                     client_request_id="reject0001"), DEADLINE)
    assert first["outcome"] == "uncertain"
    fake.fail.pop("t3_thread_launch")
    retry = h_start_thread(ctx, args(title="S", message="M",
                                     client_request_id="reject0001"), DEADLINE)
    assert retry["outcome"] == "uncertain"


def test_launch_error_after_backend_side_effect_stays_uncertain():
    ctx, fake = make_ctx(allow_writes=True)
    real = fake.call_tool
    state = {"first": True}

    def after_side_effect(name, args=None, timeout=None, **kwargs):
        result = real(name, args, timeout, **kwargs)
        if name == "t3_thread_launch" and state["first"]:
            state["first"] = False
            raise ToolError("response failed after launch", t3_code="internal_error")
        return result

    fake.call_tool = after_side_effect
    first = h_start_thread(ctx, args(title="Once", message="safe"), DEADLINE)
    assert first["outcome"] == "uncertain"
    rid = first["client_request_id"]
    retry = h_start_thread(ctx, args(title="Once", message="safe",
                                     client_request_id=rid), DEADLINE)
    assert retry["outcome"] == "uncertain"
    assert len(fake.launches) == 1


def test_send_scope_denial_preserves_uncertain_request_id():
    ctx, fake = make_ctx(allow_writes=True)
    fake.fail["t3_thread_send"] = ToolError(
        "credential lacks orchestration:operate", t3_code="scope_denied_operate")
    first = h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                                    client_request_id="reject0002"), DEADLINE)
    assert first["outcome"] == "uncertain"
    fake.fail.pop("t3_thread_send")
    retry = h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                                    client_request_id="reject0002"), DEADLINE)
    assert retry["outcome"] == "accepted"


def test_send_deadline_after_claim_is_uncertain_and_recoverable():
    now = [0.0]
    ctx, fake = make_ctx(allow_writes=True, clock=lambda: now[0])
    original = ctx.begin_write_checked

    def claim_then_expire(tool, rid, digest):
        result = original(tool, rid, digest)
        now[0] = 999999.0
        return result

    ctx.begin_write_checked = claim_then_expire
    first = h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                                    client_request_id="deadline1"), 1.0)
    assert first["outcome"] == "uncertain"
    now[0] = 0.0
    retry = h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                                    client_request_id="deadline1"), DEADLINE)
    assert retry["outcome"] == "accepted"


def test_send_generated_id_is_returned_for_generic_error_recovery():
    ctx, fake = make_ctx(allow_writes=True)
    real = fake.call_tool
    state = {"first": True}

    def after_side_effect(name, args=None, timeout=None, **kwargs):
        result = real(name, args, timeout, **kwargs)
        if name == "t3_thread_send" and state["first"]:
            state["first"] = False
            raise ToolError("response failed after send", t3_code="internal_error")
        return result

    fake.call_tool = after_side_effect
    first = h_send_thread(ctx, args(thread_id="thread-1", message="hi"), DEADLINE)
    assert first["outcome"] == "uncertain"
    rid = first["client_request_id"]
    assert isinstance(rid, str) and rid
    retry = h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                                    client_request_id=rid), DEADLINE)
    assert retry["outcome"] == "accepted"


def test_start_thread_rejects_oversized_inputs():
    ctx, fake = make_ctx(allow_writes=True)
    with pytest.raises(AdapterError) as exc:
        h_start_thread(ctx, args(title="T" * 500, message="M"), DEADLINE)
    assert exc.value.code == "invalid_params"
    with pytest.raises(AdapterError):
        h_start_thread(ctx, args(title="T", message="M", project_id="bad id"), DEADLINE)


def test_concurrent_same_id_launch_is_in_progress_not_double_launch():
    # Two dispatches race with the same client_request_id: the write-ahead
    # journal + check lock make the second one observe the first's pending
    # record (in_progress) instead of launching twice.
    fake = BlockingT3(PROJECTS, THREADS, block_on="t3_thread_launch")
    ctx, fake = make_ctx(allow_writes=True, fake=fake)

    results = {}

    def first():
        results["r1"] = h_start_thread(
            ctx, args(title="Race", message="M", client_request_id="racekey01"), DEADLINE)

    t1 = threading.Thread(target=first)
    t1.start()
    assert fake.reached.wait(timeout=5)  # launch #1 is now in flight
    # duplicate arrives while #1 executes
    results["r2"] = h_start_thread(
        ctx, args(title="Race", message="M", client_request_id="racekey01"), DEADLINE)
    assert results["r2"]["outcome"] == "in_progress"
    fake.release.set()                   # let #1 finish
    t1.join(timeout=5)
    assert results["r1"]["outcome"] == "launched"
    assert len(fake.launches) == 1


def test_cross_process_crashed_pending_is_uncertain_and_never_relaunched(tmp_path):
    # Simulate a previous process: pending record exists, no outcome written.
    journal = Journal(tmp_path / "journal.jsonl", wall_clock=lambda: 0.0)
    digest = args_digest("t3_start_thread",
                         {"project_id": PROJ, "title": "Ghost", "message": "M"},
                         host="workstation", subject=None)
    journal.begin(tool="t3_start_thread", rid="ghost00001", digest=digest,
                  wall_ts=0.0)
    del journal  # "crash": the process died without an outcome

    ctx, fake = make_ctx(allow_writes=True,
                         journal_path=tmp_path / "journal.jsonl")
    r = h_start_thread(ctx, args(title="Ghost", message="M",
                                 client_request_id="ghost00001"), DEADLINE)
    assert r["outcome"] == "uncertain"
    assert r["recovered_from_journal"] is True
    # no matching title exists on the backend — still no auto-relaunch
    assert fake.launches == []


def test_cross_process_recorded_outcome_replays_as_cached(tmp_path):
    journal = Journal(tmp_path / "journal.jsonl", wall_clock=lambda: 0.0)
    digest = args_digest("t3_start_thread",
                         {"project_id": PROJ, "title": "Done", "message": "M"},
                         host="workstation", subject=None)
    journal.begin(tool="t3_start_thread", rid="done000001", digest=digest,
                  wall_ts=0.0)
    journal.finish(rid="done000001", wall_ts=1.0, outcome="launched",
                   meta={"thread_id": "thread-77"})
    del journal

    ctx, fake = make_ctx(allow_writes=True,
                         journal_path=tmp_path / "journal.jsonl")
    r = h_start_thread(ctx, args(title="Done", message="M",
                                 client_request_id="done000001"), DEADLINE)
    assert r["outcome"] == "cached"
    assert r["previous"]["thread_id"] == "thread-77"
    assert fake.launches == []


def test_journal_stores_no_raw_content(tmp_path):
    path = tmp_path / "journal.jsonl"
    ctx, fake = make_ctx(allow_writes=True, journal_path=path)
    h_start_thread(ctx, args(title="SecretTitle", message="secret body text",
                             client_request_id="priv000001"), DEADLINE)
    raw = path.read_text()
    assert "SecretTitle" not in raw
    assert "secret body text" not in raw
    assert args_digest("t3_start_thread",
                       {"project_id": PROJ, "title": "SecretTitle",
                        "message": "secret body text"},
                       host="workstation", subject=None) in raw


def test_unreadable_journal_fails_closed(tmp_path):
    with pytest.raises(AdapterError) as exc:
        make_ctx(allow_writes=True, journal_path=tmp_path)  # a directory
    assert exc.value.code == "journal_failed"


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

def test_send_accepted():
    ctx, fake = make_ctx(allow_writes=True)
    r = h_send_thread(ctx, args(thread_id="thread-1", message="hi"), DEADLINE)
    assert r["outcome"] == "accepted"
    assert r["delivery"] == "queue"   # default mode is the least intrusive
    assert r["thread_id"] == "thread-1"
    assert r["client_request_id"]


def test_send_requires_thread_in_allowed_project():
    ctx, fake = make_ctx(allow_writes=True)
    with pytest.raises(AdapterError) as exc:
        h_send_thread(ctx, args(thread_id="thread-9", message="hi"), DEADLINE)
    assert exc.value.code == "thread_not_in_project"
    assert fake.sends == []


def test_send_uncertain_carries_client_request_id():
    ctx, fake = make_ctx(allow_writes=True)
    fake.fail["t3_thread_send"] = ConnectionError("connection refused")
    r = h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                                client_request_id="retrykey123"), DEADLINE)
    assert r["outcome"] == "uncertain"
    assert r["client_request_id"] == "retrykey123"
    assert "same client_request_id" in r["guidance"].lower()


def test_send_uncertain_then_recover_with_same_id():
    ctx, fake = make_ctx(allow_writes=True)
    fake.fail["t3_thread_send"] = ConnectionError("connection refused")
    r1 = h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                                 client_request_id="retrykey124"), DEADLINE)
    assert r1["outcome"] == "uncertain"
    fake.fail.pop("t3_thread_send")
    # recovery path: re-issue under the SAME id; T3 deduplicates sends
    r2 = h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                                 client_request_id="retrykey124"), DEADLINE)
    assert r2["outcome"] == "accepted"
    assert r2["recovered_from_uncertain"] is True
    # both sends reached the backend with the same clientRequestId
    sent = [a for n, a in fake.calls if n == "t3_thread_send"]
    assert {a["clientRequestId"] for a in sent} == {"retrykey124"}


def test_send_reuse_of_client_request_id_is_cached_replay():
    ctx, fake = make_ctx(allow_writes=True)
    r1 = h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                                 client_request_id="rid12345"), DEADLINE)
    r2 = h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                                 client_request_id="rid12345"), DEADLINE)
    # the durable journal short-circuits the repeat: the recorded outcome is
    # replayed and the backend is not touched again
    assert r1["outcome"] == "accepted"
    assert r2["outcome"] == "cached"
    assert len(fake.sends) == 1


def test_send_same_id_different_args_is_conflict():
    ctx, fake = make_ctx(allow_writes=True)
    h_send_thread(ctx, args(thread_id="thread-1", message="hi",
                            client_request_id="rid999999"), DEADLINE)
    with pytest.raises(AdapterError) as exc:
        h_send_thread(ctx, args(thread_id="thread-1", message="different",
                                client_request_id="rid999999"), DEADLINE)
    assert exc.value.code == "request_id_conflict"


def test_send_invalid_mode():
    ctx, fake = make_ctx(allow_writes=True)
    with pytest.raises(AdapterError) as exc:
        h_send_thread(ctx, args(thread_id="thread-1", message="hi", mode="destroy"), DEADLINE)
    assert exc.value.code == "invalid_params"


# ---------------------------------------------------------------------------
# interrupt
# ---------------------------------------------------------------------------

def test_interrupt_completed():
    ctx, fake = make_ctx(allow_writes=True)
    r = h_interrupt_thread(ctx, args(thread_id="thread-1", reason="stop"), DEADLINE)
    assert r["outcome"] == "completed"
    assert r["status"] == "interrupting"
    assert fake.interrupts[0]["reason"] == "stop"


def test_interrupt_uncertain_on_connection_drop():
    ctx, fake = make_ctx(allow_writes=True)
    fake.fail["t3_thread_interrupt"] = ConnectionError("connection refused")
    r = h_interrupt_thread(ctx, args(thread_id="thread-1"), DEADLINE)
    assert r["outcome"] == "uncertain"
    assert "idempotent" in r["guidance"]


def test_interrupt_requires_membership():
    ctx, fake = make_ctx(allow_writes=True)
    with pytest.raises(AdapterError) as exc:
        h_interrupt_thread(ctx, args(thread_id="thread-9"), DEADLINE)
    assert exc.value.code == "thread_not_in_project"
    assert fake.interrupts == []


# ---------------------------------------------------------------------------
# deadline
# ---------------------------------------------------------------------------

def test_deadline_exceeded_is_stable_code():
    ctx, fake = make_ctx()
    with pytest.raises(AdapterError) as exc:
        h_list_threads(ctx, {"project_id": PROJ}, deadline=-1)  # already past
    assert exc.value.code == "deadline_exceeded"


# ---------------------------------------------------------------------------
# specs / annotations
# ---------------------------------------------------------------------------

def test_tools_hidden_when_writes_disabled():
    names = [s["name"] for s in tool_specs(False)]
    assert names == ["t3_health", "t3_list_projects", "t3_list_threads",
                     "t3_read_thread", "t3_wait_thread"]


def test_write_tool_annotations():
    specs = {s["name"]: s for s in tool_specs(True)}
    assert specs["t3_start_thread"]["annotations"] == {
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": False,
    }
    assert specs["t3_interrupt_thread"]["annotations"]["destructiveHint"] is True
    assert specs["t3_interrupt_thread"]["annotations"]["idempotentHint"] is True
    assert specs["t3_read_thread"]["annotations"]["readOnlyHint"] is True
    for name in HANDLERS:
        assert name in specs


def test_no_authorization_literal_in_any_schema():
    # the authorize-t3-write literal is gone; consent comes from the
    # end-user JWT, never from a string the model can type
    for spec in tool_specs(True):
        assert "authorization" not in spec["inputSchema"].get("required", [])
        assert "authorization" not in spec["inputSchema"].get("properties", {})
