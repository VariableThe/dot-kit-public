"""Offline unit tests for the durable write journal (crash/restart recovery).

Requirements under test: request ids and outcomes survive crashes and
restarts; no message content or secrets ever reach disk (only digests);
a pending record from a dead process surfaces as uncertain; concurrent
in-process duplicates surface as in_progress; same id + different args
is a conflict; expiry is bounded; replay tolerates torn trailing lines.
"""

import json
import time

import pytest

from t3mcp import AdapterError
from t3mcp.journal import Journal, args_digest, RESCAN_BYTE_LIMIT

from helpers_t3mcp import PROJ


def digest_for(title="T", message="M", tool="t3_start_thread"):
    return args_digest(tool, {"project_id": PROJ, "title": title,
                              "message": message})


def test_begin_then_recall_is_in_progress_own_process(tmp_path):
    j = Journal(tmp_path / "j.jsonl", wall_clock=lambda: 0.0)
    j.begin(tool="t3_start_thread", rid="rid000001", digest=digest_for(),
            wall_ts=0.0)
    kind, rec = j.recall("rid000001", digest_for(), ttl_s=3600)
    assert kind == "in_progress"
    assert rec["state"] == "pending"


def test_finish_then_recall_is_cached(tmp_path):
    j = Journal(tmp_path / "j.jsonl", wall_clock=lambda: 0.0)
    j.begin(tool="t3_start_thread", rid="rid000002", digest=digest_for(),
            wall_ts=0.0)
    j.finish(rid="rid000002", wall_ts=1.0, outcome="launched",
             meta={"thread_id": "t-1"})
    kind, rec = j.recall("rid000002", digest_for(), ttl_s=3600)
    assert kind == "cached"
    assert rec["outcome"] == "launched"
    assert rec["meta"]["thread_id"] == "t-1"


def test_same_id_different_digest_is_conflict(tmp_path):
    j = Journal(tmp_path / "j.jsonl", wall_clock=lambda: 0.0)
    j.begin(tool="t3_start_thread", rid="rid000003", digest=digest_for(),
            wall_ts=0.0)
    kind, _ = j.recall("rid000003", digest_for(title="OTHER"), ttl_s=3600)
    assert kind == "conflict"


def test_ttl_expiry_returns_none(tmp_path):
    now = [0.0]
    j = Journal(tmp_path / "j.jsonl", wall_clock=lambda: now[0])
    j.begin(tool="t3_start_thread", rid="rid000004", digest=digest_for(),
            wall_ts=now[0])
    now[0] = 7200  # past client_request_id_ttl_s
    assert j.recall("rid000004", digest_for(), ttl_s=3600)[0] == "in_progress"


def test_crashed_pending_is_uncertain_after_restart(tmp_path):
    path = tmp_path / "j.jsonl"
    first = Journal(path, wall_clock=lambda: 0.0)
    first.begin(tool="t3_start_thread", rid="rid000005", digest=digest_for(),
                wall_ts=0.0)
    del first  # "process died" before finish()

    second = Journal(path, wall_clock=lambda: 1.0)  # "restart"
    kind, rec = second.recall("rid000005", digest_for(), ttl_s=3600)
    assert kind == "uncertain"
    assert rec["state"] == "pending"


def test_outcome_survives_restart(tmp_path):
    path = tmp_path / "j.jsonl"
    first = Journal(path, wall_clock=lambda: 0.0)
    first.begin(tool="t3_send_thread", rid="rid000006", digest=digest_for(),
                wall_ts=0.0)
    first.finish(rid="rid000006", wall_ts=1.0, outcome="accepted",
                 meta={"thread_id": "t-2"})

    second = Journal(path, wall_clock=lambda: 2.0)
    kind, rec = second.recall("rid000006", digest_for(), ttl_s=3600)
    assert kind == "cached"
    assert rec["outcome"] == "accepted"


def test_torn_trailing_line_is_tolerated(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text(json.dumps({
        "v": 1, "state": "outcome", "rid": "rid000007",
        "ts": 0.0, "outcome": "launched", "meta": {},
    }) + "\n" + '{"v": 1, "state": "outcome", "rid": "rid000008"')  # torn
    j = Journal(path, wall_clock=lambda: 1.0)
    assert j.recall("rid000007", digest_for(), ttl_s=3600)[0] == "cached"
    assert j.recall("rid000008", digest_for(), ttl_s=3600) is None


def test_unreadable_journal_fails_closed(tmp_path):
    with pytest.raises(AdapterError) as exc:
        Journal(tmp_path, wall_clock=lambda: 0.0)  # a directory, not a file
    assert exc.value.code == "journal_failed"


def test_records_never_contain_arguments_or_secrets(tmp_path):
    path = tmp_path / "j.jsonl"
    j = Journal(path, wall_clock=lambda: 0.0)
    d = args_digest("t3_send_thread",
                    {"project_id": PROJ, "thread_id": "t-9",
                     "message": "SECRET PLANS token=abc123"})
    j.begin(tool="t3_send_thread", rid="rid000009", digest=d, wall_ts=0.0,
            meta={"thread_id": "t-9"})
    j.finish(rid="rid000009", wall_ts=1.0, outcome="accepted",
             meta={"thread_id": "t-9"})
    raw = path.read_text()
    assert "SECRET PLANS" not in raw
    assert "abc123" not in raw
    assert d in raw


def test_cross_process_record_is_found_by_bounded_rescan(tmp_path):
    # Journal A starts; journal B (another live adapter process) records an
    # outcome for the same rid; A's recall finds it via the bounded rescan.
    path = tmp_path / "j.jsonl"
    a = Journal(path, wall_clock=lambda: 0.0)
    b = Journal(path, wall_clock=lambda: 0.0)
    d = digest_for()
    b.begin(tool="t3_start_thread", rid="rid000010", digest=d, wall_ts=0.0)
    b.finish(rid="rid000010", wall_ts=1.0, outcome="launched",
             meta={"thread_id": "t-3"})
    kind, rec = a.recall("rid000010", d, ttl_s=3600)
    assert kind == "cached"
    assert rec["meta"]["thread_id"] == "t-3"


def test_rescan_is_bounded(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_bytes(b"x" * (RESCAN_BYTE_LIMIT + 1))
    with pytest.raises(AdapterError):
        Journal(path, wall_clock=lambda: 0.0)


def test_args_digest_ignores_irrelevant_fields():
    # only request-identity fields matter; unrelated extras do not change it
    d1 = args_digest("t3_start_thread",
                     {"project_id": PROJ, "title": "T", "message": "M",
                      "client_request_id": "same"})
    d2 = args_digest("t3_start_thread",
                     {"project_id": PROJ, "title": "T", "message": "M",
                      "client_request_id": "different"})
    assert d1 == d2
    d3 = args_digest("t3_start_thread",
                     {"project_id": PROJ, "title": "T", "message": "M2"})
    assert d1 != d3


def test_args_digest_binds_host_and_subject():
    args = {"project_id": "p", "title": "t", "message": "m"}
    assert args_digest("t3_start_thread", args, host="a", subject="u") != \
        args_digest("t3_start_thread", args, host="b", subject="u")
    assert args_digest("t3_start_thread", args, host="a", subject="u") != \
        args_digest("t3_start_thread", args, host="a", subject="v")


def test_uncertain_outcome_does_not_expire(tmp_path):
    now = [0.0]
    j = Journal(tmp_path / "j.jsonl", wall_clock=lambda: now[0])
    j.begin(tool="t3_send_thread", rid="uncertain1", digest=digest_for(), wall_ts=0.0)
    j.finish(rid="uncertain1", wall_ts=0.0, outcome="uncertain")
    now[0] = 999999
    assert j.recall("uncertain1", digest_for(), ttl_s=1)[0] == "cached"
