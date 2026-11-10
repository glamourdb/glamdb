"""Bounded, allowlisted operational logging for the Python control plane.

Use generated request IDs. This best-effort stream is not a durable audit journal.
"""
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import secrets
import stat
import threading
import time

EVENTS = frozenset({"runtime_started", "runtime_stopped", "request_completed", "storage_failed",
                    "recovery_required", "logger_health", "access_denied", "org_created", "org_updated",
                    "org_deleted", "database_created", "database_deleted", "session_started", "session_ended"})
OPERATIONS = frozenset({"other", "create_store", "inspect_store", "health", "collections", "snapshots",
                        "append", "scan", "operation", "auth", "organization", "database", "session"})
OUTCOMES = frozenset({"ok", "error", "unknown", "denied", "cancelled"})
NUMBERS = frozenset({"duration_us", "row_count", "status_code", "revision", "submitted", "accepted", "sampled",
                     "rate_limited", "dropped_full", "dropped_contended", "dropped_stopped", "dropped_sink",
                     "written", "sink_errors", "backlog", "queue_capacity"})
UUID_PATTERN = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
SLUG_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
REQUEST_PATTERN = re.compile(r"[a-f0-9]{32}\Z")


def _enum(value, allowed, default):
    return value if isinstance(value, str) and len(value) <= 64 and value in allowed else default


def sanitize_record(event, **fields):
    """Project known fields; never stringify an arbitrary value or exception."""
    event = _enum(event, EVENTS, "request_completed")
    outcome = _enum(fields.get("outcome", "ok"), OUTCOMES, "error")
    request_id = fields.get("request_id")
    record = {
        "schema": 1, "timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "service": "glamdb", "event": event,
        "level": "error" if outcome in {"error", "unknown"} or event in {"storage_failed", "recovery_required"}
        else "info" if outcome == "ok" else "warn",
        "operation": _enum(fields.get("operation", "other"), OPERATIONS, "other"), "outcome": outcome,
        "request_id": request_id if isinstance(request_id, str) and len(request_id) == 32 and
        REQUEST_PATTERN.fullmatch(request_id) else secrets.token_hex(16),
    }
    for key in NUMBERS:
        value = fields.get(key)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            record[key] = value
    for key in ("org_id", "database_id"):
        value = fields.get(key)
        if isinstance(value, str) and len(value) <= 64 and (UUID_PATTERN.fullmatch(value) or SLUG_PATTERN.fullmatch(value)):
            record[key] = value
    return record


class _Sink:
    def __init__(self, directory, max_bytes, file_count):
        self.directory = None if directory is None else Path(directory)
        self.max_bytes, self.file_count = max_bytes, file_count
        self.fd = None
        self.directory_fd = None
        if self.directory is None:
            self.fd = os.dup(2)
            self.size = 0
            return
        try:
            if not self.directory.is_absolute():
                raise ValueError("log directory must be absolute")
            self.directory.mkdir(mode=0o700, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ValueError("log directory must be owned and private to its writer")
            self.directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            fcntl.flock(self.directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for index in range(1, 16):
                path = self.directory / f"glamdb.log.{index}"
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                self._check(info)
                if index >= file_count or info.st_size > max_bytes:
                    path.unlink()
                else:
                    path.chmod(0o600)
            self._open()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(info):
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError("log sink must be an owned, single-link regular file")

    def _open(self):
        self.fd = os.open(self.directory / "glamdb.log",
                          os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(self.fd)
            self._check(info)
            os.fchmod(self.fd, 0o600)
            self.size = info.st_size
            if self.size > self.max_bytes:
                os.ftruncate(self.fd, 0)
                self.size = 0
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def write(self, record):
        line = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
        if self.directory is not None and (self.fd is None or self.size + len(line) > self.max_bytes):
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self.file_count == 1:
                (self.directory / "glamdb.log").unlink(missing_ok=True)
            for index in range(self.file_count - 1, 0, -1):
                source = self.directory / ("glamdb.log" if index == 1 else f"glamdb.log.{index-1}")
                try:
                    source.replace(self.directory / f"glamdb.log.{index}")
                except FileNotFoundError:
                    pass
            self._open()
        offset = 0
        try:
            while offset < len(line):
                written = os.write(self.fd, line[offset:])
                if written == 0:
                    raise OSError("zero-length log write")
                offset += written
        except OSError:
            if offset and self.directory is not None:
                os.ftruncate(self.fd, self.size)
            raise
        self.size += len(line)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None


class StructuredLogger:
    """Small critical sections and bounded memory; all sink IO runs in a worker."""
    def __init__(self, *, directory=None, capacity=4096, reserve=64, sample_every=100,
                 max_success_per_second=20, max_bytes=16*1024*1024, file_count=4):
        if (not 2 <= capacity <= 1048576 or not 0 <= reserve < capacity or sample_every < 1 or
                max_success_per_second < 1 or not 4096 <= max_bytes <= 1024**3 or not 1 <= file_count <= 16):
            raise ValueError("invalid logging limits")
        self._sink = _Sink(directory, max_bytes, file_count)
        self._queue = queue.Queue(maxsize=capacity)
        self._capacity, self._reserve = capacity, reserve
        self._sample_every, self._success_max = sample_every, max_success_per_second
        self._lock = threading.Lock()
        self._admission = threading.Lock()
        self._stats = Counter()
        self._sequence, self._second, self._successes = 0, 0, 0
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="glamdb-log", daemon=True)
        self._worker.start()

    @staticmethod
    def new_request_id():
        return secrets.token_hex(16)

    def _count(self, key):
        with self._lock:
            self._stats[key] += 1

    def snapshot(self):
        with self._lock:
            result = dict(self._stats)
        result["backlog"] = result.get("accepted", 0) - result.get("written", 0) - result.get("dropped_sink", 0)
        result["queue_capacity"] = self._capacity
        return result

    def emit(self, event, **fields):
        self._count("submitted")
        if not self._admission.acquire(blocking=False):
            self._count("dropped_contended")
            return False
        try:
            if self._closed:
                self._count("dropped_stopped")
                return False
            record = sanitize_record(event, **fields)
            success = record["event"] == "request_completed" and record["outcome"] == "ok"
            if success:
                self._sequence += 1
                if (self._sequence - 1) % self._sample_every:
                    self._count("sampled")
                    return False
                second = int(time.monotonic())
                if second != self._second:
                    self._second, self._successes = second, 0
                if self._successes >= self._success_max:
                    self._count("rate_limited")
                    return False
                self._successes += 1
            if success and self._queue.qsize() >= self._capacity - self._reserve:
                self._count("dropped_full")
                return False
            try:
                # Counter precedes publication so a fast worker cannot create negative backlog.
                self._count("accepted")
                self._queue.put_nowait(record)
            except queue.Full:
                with self._lock:
                    self._stats["accepted"] -= 1
                    self._stats["dropped_full"] += 1
                return False
            return True
        finally:
            self._admission.release()

    def _write(self, record, counted=True):
        try:
            self._sink.write(record)
            if counted:
                self._count("written")
        except (OSError, ValueError):
            self._count("sink_errors")
            if counted:
                self._count("dropped_sink")
            time.sleep(0.1)

    def _run(self):
        next_health = time.monotonic() + 5
        try:
            while True:
                try:
                    record = self._queue.get(timeout=max(0.01, next_health - time.monotonic()))
                except queue.Empty:
                    record = False
                if record is None:
                    break
                if record is not False:
                    self._write(record)
                if time.monotonic() >= next_health:
                    self._write(sanitize_record("logger_health", **self.snapshot()), counted=False)
                    next_health = time.monotonic() + 5
            self._write(sanitize_record("logger_health", **self.snapshot()), counted=False)
        finally:
            self._sink.close()

    def close(self):
        with self._admission:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        self._worker.join()


def render_prometheus(snapshot):
    """Render actual logger snapshot values for the supervisor's metrics endpoint."""
    lines = []
    for field in ("submitted", "accepted", "sampled", "rate_limited", "written", "sink_errors"):
        value = snapshot.get(field, 0)
        value = value if type(value) is int and value >= 0 else 0
        lines.append(f"glamdb_log_{field}_total {value}")
    for field, reason in (("dropped_full", "queue_full"), ("dropped_contended", "contended"),
                          ("dropped_stopped", "stopped"), ("dropped_sink", "sink")):
        value = snapshot.get(field, 0)
        value = value if type(value) is int and value >= 0 else 0
        lines.append(f'glamdb_log_dropped_total{{reason="{reason}"}} {value}')
    for field in ("backlog", "queue_capacity"):
        value = snapshot.get(field, 0)
        value = value if type(value) is int and value >= 0 else 0
        lines.append(f"glamdb_log_{field} {value}")
    return "\n".join(lines) + "\n"
