"""Fail-closed durable request journal for offline T3 plugin use.

This is a small, dependency-free hardening component. It stores request
digests and outcomes, never request arguments. Any non-terminal corruption,
invalid record, unreadable journal, or oversized rescan fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
import fcntl


class JournalError(RuntimeError):
    """The journal cannot safely establish write history."""


VERSION = 1
DEFAULT_RESCAN_LIMIT = 4 * 1024 * 1024


def request_digest(tool: str, args: dict) -> str:
    identity = {
        key: args.get(key)
        for key in ("project_id", "thread_id", "title", "message", "mode")
    }
    payload = json.dumps(
        {"tool": tool, **identity}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class JournalRecord:
    state: str
    rid: str
    digest: str
    timestamp: float
    outcome: str | None = None
    meta: dict | None = None


class DurableJournal:
    """Append-only journal with conservative crash and corruption handling."""

    def __init__(self, path: str | os.PathLike, *, clock=time.time,
                 rescan_limit=DEFAULT_RESCAN_LIMIT):
        self.path = Path(path)
        self.clock = clock
        self.rescan_limit = rescan_limit
        self._lock = threading.RLock()
        self._records: dict[str, JournalRecord] = {}
        self._live_pending: set[str] = set()
        self._torn_tail_offset: int | None = None
        self._needs_separator = False
        self._replay()

    def _fsync_directory(self) -> None:
        try:
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise JournalError("cannot persist journal directory") from exc

    def _require_state_directory(self) -> None:
        """Require operator-provisioned storage before any durable write.

        Creating a missing directory and fsyncing only the leaf directory is
        not enough to make a newly-created parent chain power-loss durable.
        Provisioning the state directory is therefore an explicit setup step.
        """
        if not self.path.parent.is_dir():
            raise JournalError("journal state directory must already exist")

    @contextmanager
    def _file_lock(self, *, exclusive: bool):
        lock_path = self.path.with_name(self.path.name + ".lock")
        try:
            self._require_state_directory()
            with lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise JournalError("cannot lock journal") from exc

    @staticmethod
    def _decode(raw: str) -> JournalRecord:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise JournalError("invalid journal JSON") from exc
        if not isinstance(value, dict) or value.get("v") != VERSION:
            raise JournalError("invalid journal record version")
        state = value.get("state")
        rid = value.get("rid")
        digest = value.get("digest", "")
        timestamp = value.get("ts")
        if state not in {"pending", "outcome"} or not isinstance(rid, str) \
                or not rid or not isinstance(digest, str) \
                or not isinstance(timestamp, (int, float)):
            raise JournalError("invalid journal record")
        outcome = value.get("outcome")
        if state == "outcome" and not isinstance(outcome, str):
            raise JournalError("outcome record is missing outcome")
        meta = value.get("meta", {})
        if not isinstance(meta, dict):
            raise JournalError("journal metadata must be an object")
        return JournalRecord(state, rid, digest, float(timestamp), outcome, meta)

    def _replay(self) -> None:
        # Recompute this marker from the current file on every replay. A
        # second instance may have repaired and appended since the last scan.
        self._torn_tail_offset = None
        self._needs_separator = False
        if not self.path.exists():
            return
        try:
            if self.path.stat().st_size > self.rescan_limit:
                raise JournalError("journal rescan limit exceeded")
            raw_file = self.path.read_bytes()
            lines = raw_file.splitlines(keepends=True)
            offset = 0
            for index, line in enumerate(lines):
                raw = line.decode("utf-8").strip()
                if not raw:
                    offset += len(line)
                    continue
                try:
                    record = self._decode(raw)
                except JournalError:
                    # Only a final unterminated line may be torn by a crash.
                    if index == len(lines) - 1 and not line.endswith(b"\n"):
                        self._torn_tail_offset = offset
                        break
                    raise
                self._records[record.rid] = record
                offset += len(line)
                if index == len(lines) - 1 and not line.endswith(b"\n"):
                    self._needs_separator = True
        except (OSError, JournalError) as exc:
            if isinstance(exc, JournalError):
                raise
            raise JournalError("cannot read journal") from exc

    def _append(self, record: dict) -> None:
        try:
            self._require_state_directory()
            if self._torn_tail_offset is not None:
                expected_size = self.path.stat().st_size
                if expected_size < self._torn_tail_offset:
                    raise JournalError("journal changed after torn-tail recovery")
                with self.path.open("r+b") as repair:
                    repair.truncate(self._torn_tail_offset)
                    repair.flush()
                    os.fsync(repair.fileno())
                self._fsync_directory()
                self._torn_tail_offset = None
            elif self._needs_separator:
                with self.path.open("ab") as repair:
                    repair.write(b"\n")
                    repair.flush()
                    os.fsync(repair.fileno())
                self._fsync_directory()
                self._needs_separator = False
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True,
                                         separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._fsync_directory()
        except OSError as exc:
            raise JournalError("cannot persist journal") from exc

    def begin(self, *, tool: str, rid: str, digest: str, timestamp=None,
              meta=None) -> None:
        record = {"v": VERSION, "state": "pending", "tool": tool,
                  "rid": rid, "digest": digest,
                  "ts": self.clock() if timestamp is None else timestamp,
                  "meta": meta or {}}
        with self._lock:
            with self._file_lock(exclusive=True):
                self._replay()
                if rid in self._records:
                    raise JournalError("request id is already claimed")
                validated = self._decode(json.dumps(record))
                self._append(record)
                self._records[rid] = validated
                self._live_pending.add(rid)

    def finish(self, *, rid: str, outcome: str, timestamp=None, meta=None) -> None:
        with self._lock:
            with self._file_lock(exclusive=True):
                self._replay()
                prior = self._records.get(rid)
                if prior is None or prior.state != "pending":
                    raise JournalError("cannot finish unknown journal request")
                record = {"v": VERSION, "state": "outcome", "rid": rid,
                          "digest": prior.digest,
                          "ts": self.clock() if timestamp is None else timestamp,
                          "outcome": outcome, "meta": {**(prior.meta or {}), **(meta or {})}}
                validated = self._decode(json.dumps(record))
                self._append(record)
                self._records[rid] = validated
                self._live_pending.discard(rid)

    def recall(self, rid: str, digest: str, *, ttl: float):
        with self._lock:
            with self._file_lock(exclusive=False):
                self._replay()
                record = self._records.get(rid)
                if record is None:
                    record = self._rescan(rid)
                if record is None:
                    return None
                if record.digest and record.digest != digest:
                    return ("conflict", record)
                if record.state == "pending":
                    return (("in_progress" if rid in self._live_pending else "uncertain"), record)
                if record.outcome != "uncertain" and self.clock() - record.timestamp > ttl:
                    return None
                return ("cached", record)

    def _rescan(self, rid: str):
        try:
            if not self.path.exists():
                return None
            if self.path.stat().st_size > self.rescan_limit:
                raise JournalError("journal rescan limit exceeded")
            found = None
            raw_file = self.path.read_bytes()
            lines = raw_file.splitlines(keepends=True)
            for index, line in enumerate(lines):
                    raw = line.decode("utf-8").strip()
                    if not raw:
                        continue
                    try:
                        record = self._decode(raw)
                    except JournalError:
                        if index == len(lines) - 1 and not line.endswith(b"\n"):
                            break
                        raise
                    if record.rid == rid:
                        found = record
            if found is not None:
                self._records[rid] = found
            return found
        except (OSError, JournalError) as exc:
            if isinstance(exc, JournalError):
                raise
            raise JournalError("cannot rescan journal") from exc
