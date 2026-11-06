"""Durable request-outcome journal for t3mcp writes.

The tunnel transports requests; a request ID and its outcome must survive
adapter crashes and restarts, or "uncertain" can never be resolved. This
journal is an append-only, fsync'd JSONL file holding exactly what recovery
needs — request ids, argument digests (not the arguments), targets, and
outcomes — and nothing that is (or derives to) a secret: no messages, no
tokens, no credentials.

Record lifecycle per write:

1. ``pending``  — appended *before* the write is attempted (write-ahead).
2. ``outcome``  — appended once the backend answered (launched/duplicate/
                 accepted/...  or ``uncertain`` after a lost response).

On startup the journal is replayed:

* ``outcome`` records rebuild the idempotency map (same ``client_request_id``
  + same argument digest -> the recorded outcome, reported as ``cached``).
* ``pending`` records with no later ``outcome`` are from a crashed run:
  they are surfaced as ``uncertain`` — the write may or may not have taken
  effect. A launch is never retried just because a title search finds
  nothing; only a human/state verification (or a NEW request id) proceeds.
* A repeated ``client_request_id`` with a *different* argument digest is a
  conflict and refused (``request_id_conflict``), never silently replayed.

During one process's life, a ``pending`` record it wrote itself (and has not
finished) means a concurrent duplicate of an in-flight write: reported as
``in_progress`` rather than executing twice.

The file is opened for append per record (O_APPEND keeps concurrent writers
from clobbering each other), flushed and fsync'd before the write executes,
and a torn trailing line from a crash is tolerated on replay.
"""

import hashlib
import json
import os
import threading
import time
import fcntl
from contextlib import contextmanager

from .errors import AdapterError

JOURNAL_VERSION = 1
RESCAN_BYTE_LIMIT = 4 * 1024 * 1024  # bounded cross-process rescan


def args_digest(tool, args, *, host=None, subject=None):
    """Stable SHA-256 over the *identity* of a request. The digest is stored
    instead of the arguments so message content never lands on disk."""
    identity = {
        "tool": tool,
        "project_id": args.get("project_id"),
        "thread_id": args.get("thread_id"),
        "title": args.get("title"),
        "message": args.get("message"),
        "mode": args.get("mode"),
        "host": host,
        "subject": subject,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Journal:
    """Append-only JSONL store; replayed into memory at startup."""

    def __init__(self, path, *, wall_clock=time.time, rescan_byte_limit=RESCAN_BYTE_LIMIT):
        self.path = str(path)
        self.wall_clock = wall_clock
        self.rescan_byte_limit = rescan_byte_limit
        self._lock = threading.RLock()
        # rid -> record dict (latest wins); entries expire by wall clock.
        self._records = {}
        self._live_pending = set()   # rids this process has started (in flight)
        self._torn_tail_offset = None
        self._needs_separator = False
        self._replay()

    def _require_state_directory(self):
        if not os.path.isdir(os.path.dirname(os.path.abspath(self.path))):
            raise AdapterError("journal_failed", "journal state directory must already exist")

    def _fsync_directory(self):
        try:
            directory = os.open(os.path.dirname(os.path.abspath(self.path)), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise AdapterError("journal_failed", "cannot persist journal directory") from exc

    @contextmanager
    def _file_lock(self, exclusive):
        lock_path = self.path + ".lock"
        try:
            self._require_state_directory()
            with open(lock_path, "a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise AdapterError("journal_failed", "cannot lock journal") from exc

    @staticmethod
    def _validate_record(record):
        if not isinstance(record, dict) or record.get("v") != JOURNAL_VERSION:
            raise AdapterError("journal_failed", "invalid journal record")
        if record.get("state") not in ("pending", "outcome", "released") \
                or not isinstance(record.get("rid"), str) \
                or not record.get("rid") \
                or not isinstance(record.get("digest", ""), str) \
                or not isinstance(record.get("ts"), (int, float)):
            raise AdapterError("journal_failed", "invalid journal record")
        if record.get("state") == "outcome" \
                and not isinstance(record.get("outcome"), str):
            raise AdapterError("journal_failed", "invalid journal outcome")

    # --- replay -------------------------------------------------------------

    def _replay(self):
        self._torn_tail_offset = None
        self._needs_separator = False
        if not os.path.exists(self.path):
            return
        try:
            size = os.path.getsize(self.path)
            if size > self.rescan_byte_limit:
                raise AdapterError("journal_failed", "journal rescan limit exceeded")
            with open(self.path, "rb") as fh:
                raw_file = fh.read()
            lines = raw_file.splitlines(keepends=True)
            offset = 0
            for index, line in enumerate(lines):
                raw_bytes = line.strip()
                if not raw_bytes:
                    offset += len(line)
                    continue
                try:
                    rec = json.loads(raw_bytes.decode("utf-8"))
                    if not isinstance(rec, dict) or rec.get("v") != JOURNAL_VERSION \
                            or not isinstance(rec.get("rid"), str) \
                            or not isinstance(rec.get("digest", ""), str) \
                            or not isinstance(rec.get("ts"), (int, float)) \
                            or rec.get("state") not in ("pending", "outcome", "released"):
                        raise ValueError("invalid journal record")
                    if rec.get("state") in ("outcome", "released") and not isinstance(rec.get("outcome"), str):
                        raise ValueError("invalid outcome")
                except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                    if index == len(lines) - 1 and not line.endswith(b"\n"):
                        self._torn_tail_offset = offset
                        break
                    raise AdapterError("journal_failed", "invalid journal record") from exc
                self._records[rec["rid"]] = rec
                offset += len(line)
                if index == len(lines) - 1 and not line.endswith(b"\n"):
                    self._needs_separator = True
        except AdapterError:
            raise
        except (OSError, UnicodeError) as exc:
            raise AdapterError("journal_failed", f"cannot read journal: {exc}") from exc

    # --- writes ---------------------------------------------------------------

    def _append(self, record):
        self._require_state_directory()
        line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            if self._torn_tail_offset is not None:
                with open(self.path, "r+b") as repair:
                    repair.truncate(self._torn_tail_offset)
                    repair.flush()
                    os.fsync(repair.fileno())
                self._fsync_directory()
                self._torn_tail_offset = None
            elif self._needs_separator:
                with open(self.path, "ab") as repair:
                    repair.write(b"\n")
                    repair.flush()
                    os.fsync(repair.fileno())
                self._fsync_directory()
                self._needs_separator = False
            with open(self.path, "ab") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._fsync_directory()
        except OSError as exc:
            raise AdapterError(
                "journal_failed",
                f"cannot persist request record: {exc}",
            ) from exc

    def begin(self, *, tool, rid, digest, wall_ts, meta=None):
        """Append the write-ahead ``pending`` record before a write executes.
        Failing to persist refuses the write (fail closed)."""
        record = {
            "v": JOURNAL_VERSION,
            "state": "pending",
            "tool": tool,
            "rid": rid,
            "digest": digest,
            "ts": wall_ts,
            "meta": meta or {},
        }
        with self._lock:
            self._validate_record(record)
            with self._file_lock(True):
                self._replay()
                if rid in self._records and self._records[rid].get("state") != "released":
                    raise AdapterError("journal_failed", "request id is already claimed")
                self._append(record)
            self._records[rid] = record
            self._live_pending.add(rid)

    def finish(self, *, rid, wall_ts, outcome, meta=None):
        """Append the terminal record for a write this process executed."""
        record = {
            "v": JOURNAL_VERSION,
            "state": "outcome",
            "rid": rid,
            "ts": wall_ts,
            "outcome": outcome,
            "meta": meta or {},
        }
        with self._lock:
            with self._file_lock(True):
                self._replay()
                prior = self._records.get(rid, {})
                if prior.get("state") != "pending" and not (
                        prior.get("state") == "outcome" and
                        prior.get("outcome") == "uncertain"):
                    raise AdapterError("journal_failed", "cannot finish unknown journal request")
                record["digest"] = prior.get("digest", "")
                self._validate_record(record)
                self._append(record)
            merged = dict(prior)
            merged.update({"state": "outcome", "outcome": outcome,
                           "ts": wall_ts, "meta": {**(prior.get("meta") or {}), **(meta or {})}})
            self._records[rid] = merged
            self._live_pending.discard(rid)

    def release(self, *, rid, wall_ts, meta=None):
        """Record a definitive pre-submission rejection and permit retry."""
        record = {"v": JOURNAL_VERSION, "state": "released", "rid": rid,
                  "ts": wall_ts, "outcome": "rejected", "meta": meta or {}}
        with self._lock:
            with self._file_lock(True):
                self._replay()
                prior = self._records.get(rid, {})
                if prior.get("state") != "pending":
                    raise AdapterError("journal_failed", "cannot release unknown journal request")
                record["digest"] = prior.get("digest", "")
                self._validate_record(record)
                self._append(record)
            self._records[rid] = {**prior, **record}
            self._live_pending.discard(rid)

    # --- reads ----------------------------------------------------------------

    def recall(self, rid, digest, *, ttl_s):
        """Return the durable view of a request id, or None.

        * ``("conflict", record)``      — same rid, different arguments.
        * ``("cached", record)``       — a recorded outcome (replay/echo).
        * ``("in_progress", record)``  — this process is executing it now.
        * ``("uncertain", record)``    — crashed mid-write (replayed pending).
        * ``None``                     — unknown; the caller may proceed.
        Expired records (wall clock) are ignored and pruned.
        """
        with self._lock:
            with self._file_lock(False):
                self._replay()
                record = self._records.get(rid)
                if record is None:
                    record = self._rescan_file_for(rid)
            if record is None:
                return None
            # Unresolved writes never expire: their outcome may still be
            # uncertain after a long outage and must be reconciled.
            if record.get("state") != "pending" and record.get("outcome") != "uncertain" \
                    and self.wall_clock() - float(record.get("ts") or 0) > ttl_s:
                self._records.pop(rid, None)
                return None
            if record.get("digest") and digest and record["digest"] != digest:
                return ("conflict", record)
            if record.get("state") == "released":
                return None
            if record.get("state") == "pending":
                if rid in self._live_pending:
                    return ("in_progress", record)
                return ("uncertain", record)   # crashed before outcome was written
            return ("cached", record)

    def _rescan_file_for(self, rid):
        """Bounded cross-process check: another adapter instance may have
        recorded this rid after our startup replay."""
        try:
            if not os.path.exists(self.path) \
                    or os.path.getsize(self.path) > self.rescan_byte_limit:
                return None
            found = None
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if rid not in line:
                        continue
                    line = line.strip()
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("rid") == rid:
                        found = rec
            if found is not None:
                self._records[rid] = found
            return found
        except OSError:
            return None  # rescan is best-effort; the append path still fails closed
