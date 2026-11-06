import json
import multiprocessing

import pytest

from plugins.ecosystem_connectivity.t3mcp_journal import (
    DurableJournal, JournalError, request_digest,
)


def digest(message="safe"):
    return request_digest("t3_send_thread", {"project_id": "p", "message": message})


def test_restart_pending_is_uncertain(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = DurableJournal(path, clock=lambda: 0)
    first.begin(tool="t3_send_thread", rid="r1", digest=digest(), timestamp=0)
    second = DurableJournal(path, clock=lambda: 1)
    assert second.recall("r1", digest(), ttl=3600)[0] == "uncertain"


def test_final_torn_line_is_the_only_tolerated_corruption(tmp_path):
    path = tmp_path / "journal.jsonl"
    valid = {"v": 1, "state": "outcome", "rid": "r1", "digest": digest(),
             "ts": 0, "outcome": "accepted", "meta": {}}
    path.write_text(json.dumps(valid) + "\n{" )
    journal = DurableJournal(path, clock=lambda: 1)
    assert journal.recall("r1", digest(), ttl=3600)[0] == "cached"


def test_torn_tail_is_repaired_before_append_and_restart(tmp_path):
    path = tmp_path / "journal.jsonl"
    valid = {"v": 1, "state": "outcome", "rid": "r1", "digest": digest(),
             "ts": 0, "outcome": "accepted", "meta": {}}
    path.write_text(json.dumps(valid) + "\n{")
    journal = DurableJournal(path, clock=lambda: 1)
    journal.begin(tool="t3_send_thread", rid="r2", digest=digest("next"), timestamp=1)
    restarted = DurableJournal(path, clock=lambda: 2)
    assert restarted.recall("r1", digest(), ttl=3600)[0] == "cached"
    assert restarted.recall("r2", digest("next"), ttl=3600)[0] == "uncertain"


def test_complete_final_record_gets_separator_before_append(tmp_path):
    path = tmp_path / "journal.jsonl"
    valid = {"v": 1, "state": "outcome", "rid": "r1", "digest": digest(),
             "ts": 0, "outcome": "accepted", "meta": {}}
    path.write_text(json.dumps(valid))
    journal = DurableJournal(path, clock=lambda: 1)
    journal.begin(tool="t3_send_thread", rid="r2", digest=digest("next"), timestamp=1)
    restarted = DurableJournal(path, clock=lambda: 2)
    assert restarted.recall("r1", digest(), ttl=3600)[0] == "cached"
    assert restarted.recall("r2", digest("next"), ttl=3600)[0] == "uncertain"


def test_two_instances_do_not_reuse_stale_torn_offset(tmp_path):
    path = tmp_path / "journal.jsonl"
    valid = {"v": 1, "state": "outcome", "rid": "r1", "digest": digest(),
             "ts": 0, "outcome": "accepted", "meta": {}}
    path.write_text(json.dumps(valid) + "\n{")
    first = DurableJournal(path, clock=lambda: 1)
    second = DurableJournal(path, clock=lambda: 1)
    first.begin(tool="t3_send_thread", rid="r2", digest=digest("first"), timestamp=1)
    second.begin(tool="t3_send_thread", rid="r3", digest=digest("second"), timestamp=1)
    restarted = DurableJournal(path, clock=lambda: 2)
    assert restarted.recall("r2", digest("first"), ttl=3600)[0] == "uncertain"
    assert restarted.recall("r3", digest("second"), ttl=3600)[0] == "uncertain"


def test_offsets_are_bytes_and_cover_blank_unicode_crlf(tmp_path):
    path = tmp_path / "journal.jsonl"
    valid = {"v": 1, "state": "outcome", "rid": "é", "digest": digest(),
             "ts": 0, "outcome": "accepted", "meta": {}}
    path.write_bytes(("\r\n" + json.dumps(valid, ensure_ascii=False) + "\r\n{").encode())
    journal = DurableJournal(path, clock=lambda: 1)
    journal.begin(tool="t3_send_thread", rid="r2", digest=digest("next"), timestamp=1)
    restarted = DurableJournal(path, clock=lambda: 2)
    assert restarted.recall("é", digest(), ttl=3600)[0] == "cached"
    assert restarted.recall("r2", digest("next"), ttl=3600)[0] == "uncertain"


def test_missing_id_rescan_tolerates_existing_torn_tail(tmp_path):
    path = tmp_path / "journal.jsonl"
    valid = {"v": 1, "state": "outcome", "rid": "r1", "digest": digest(),
             "ts": 0, "outcome": "accepted", "meta": {}}
    path.write_text(json.dumps(valid) + "\n{")
    journal = DurableJournal(path, clock=lambda: 1)
    assert journal.recall("missing", digest("other"), ttl=3600) is None


def _claim(path, queue, start):
    try:
        start.wait()
        DurableJournal(path).begin(tool="t3_send_thread", rid="same",
                                   digest=digest(), timestamp=0)
        queue.put("claimed")
    except JournalError:
        queue.put("rejected")


def test_simultaneous_process_claims_are_serialized(tmp_path):
    path = str(tmp_path / "journal.jsonl")
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    start = ctx.Event()
    a = ctx.Process(target=_claim, args=(path, queue, start))
    b = ctx.Process(target=_claim, args=(path, queue, start))
    a.start(); b.start(); start.set()
    a.join(5); b.join(5)
    results = sorted([queue.get(timeout=2), queue.get(timeout=2)])
    assert results == ["claimed", "rejected"]


def test_uncertain_pending_does_not_expire(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = DurableJournal(path, clock=lambda: 0)
    first.begin(tool="t3_send_thread", rid="r1", digest=digest(), timestamp=0)
    restarted = DurableJournal(path, clock=lambda: 999999)
    assert restarted.recall("r1", digest(), ttl=1)[0] == "uncertain"


def test_request_id_cannot_be_reclaimed(tmp_path):
    journal = DurableJournal(tmp_path / "journal.jsonl")
    journal.begin(tool="t3_send_thread", rid="r1", digest=digest(), timestamp=0)
    with pytest.raises(JournalError):
        journal.begin(tool="t3_send_thread", rid="r1", digest=digest("other"), timestamp=0)


def test_second_process_view_cannot_claim_existing_id(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = DurableJournal(path, clock=lambda: 0)
    first.begin(tool="t3_send_thread", rid="r1", digest=digest(), timestamp=0)
    second = DurableJournal(path, clock=lambda: 0)
    with pytest.raises(JournalError):
        second.begin(tool="t3_send_thread", rid="r1", digest=digest(), timestamp=0)


def test_invalid_begin_is_not_persisted(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = DurableJournal(path)
    with pytest.raises(JournalError):
        journal.begin(tool="t3_send_thread", rid="r1", digest=digest(),
                      timestamp="not-a-number")
    assert not path.exists()
    DurableJournal(path)


def test_invalid_finish_is_not_persisted(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = DurableJournal(path, clock=lambda: 0)
    journal.begin(tool="t3_send_thread", rid="r1", digest=digest(), timestamp=0)
    before = path.read_text()
    with pytest.raises(JournalError):
        journal.finish(rid="r1", outcome=None, timestamp=1)
    assert path.read_text() == before
    restarted = DurableJournal(path)
    assert restarted.recall("r1", digest(), ttl=3600)[0] == "uncertain"


def test_corruption_in_middle_fails_closed(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text("{\"v\":1,\"state\":\"broken\"}\n")
    with pytest.raises(JournalError):
        DurableJournal(path)


def test_oversized_rescan_fails_closed(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_bytes(b"x" * 32)
    with pytest.raises(JournalError):
        DurableJournal(path, rescan_limit=16)


def test_finish_requires_existing_pending_request(tmp_path):
    journal = DurableJournal(tmp_path / "journal.jsonl")
    with pytest.raises(JournalError):
        journal.finish(rid="missing", outcome="accepted")
