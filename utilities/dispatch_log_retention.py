"""Bounded retention for exact Hearting dispatch logs under the jobs state root.

This pass shares terminal and consumer protection with worker-home retention.
Its ownership boundary is narrower: only a regular, single-link file in the
canonical state-root ``logs`` directory with an exact registry binding is ever
eligible for unlink.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import time

from dispatch_contract import (
    DispatchContractError,
    dispatch_state_root,
    validate_dispatch_log_dir,
)

RETENTION_NS = 14 * 86400 * 1_000_000_000
MAX_LOG_BYTES = 2 * 1024 * 1024 * 1024
DUE_SECONDS = 300
EXAMINE_LIMIT = 64
MAX_DIRECTORY_ENTRIES = 50000
MAX_PASS_SECONDS = 10
REMOVE_LIMIT = 16
STATE_NAME = "dispatch-log-retention.json"
LOCK_NAME = "dispatch-log-retention.lock"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_uid)


def _terminal_ns(value):
    from datetime import datetime, timezone

    try:
        ended = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        delta = ended.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return ((delta.days * 86400 + delta.seconds) * 1_000_000_000
                + delta.microseconds * 1000)
    except (AttributeError, OverflowError, TypeError, ValueError):
        return None


class _LogLayout:
    def __init__(self, root: Path):
        self.fds = []
        self.root_fd = os.open(root, _DIR_FLAGS)
        self.fds.append(self.root_fd)
        if os.fstat(self.root_fd).st_uid != os.geteuid():
            self.close()
            raise PermissionError("dispatch-state-root-owner-mismatch")
        self.logs_fd = os.open("logs", _DIR_FLAGS, dir_fd=self.root_fd)
        self.fds.append(self.logs_fd)
        if os.fstat(self.logs_fd).st_uid != os.geteuid():
            self.close()
            raise PermissionError("dispatch-log-dir-owner-mismatch")

    def close(self):
        while self.fds:
            with contextlib.suppress(OSError):
                os.close(self.fds.pop())

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


@contextlib.contextmanager
def _advisory_lock(layout):
    fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=layout.root_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.geteuid():
            yield False
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_state(layout):
    try:
        fd = os.open(STATE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=layout.root_fd)
    except FileNotFoundError:
        return {"last_pass_ns": 0, "cursor": ""}
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(16385)
    if len(raw) > 16384:
        return {"last_pass_ns": 0, "cursor": ""}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {"last_pass_ns": 0, "cursor": ""}
    if (not isinstance(value, dict) or not isinstance(value.get("last_pass_ns"), int)
            or not isinstance(value.get("cursor", ""), str)):
        return {"last_pass_ns": 0, "cursor": ""}
    return value


def _write_state(layout, state):
    temp = ".dispatch-log-retention.%s.tmp" % secrets.token_hex(8)
    data = json.dumps(state, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=layout.root_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, STATE_NAME, src_dir_fd=layout.root_fd, dst_dir_fd=layout.root_fd)
        os.fsync(layout.root_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp, dir_fd=layout.root_fd)
        raise


def _bound_name(jobs: Path, root: Path, log_file: str):
    if not isinstance(log_file, str) or not os.path.isabs(log_file):
        return None
    if os.path.normpath(log_file) != log_file:
        return None
    path = Path(log_file)
    logs = root / "logs"
    try:
        validated = validate_dispatch_log_dir(jobs, logs)
    except (DispatchContractError, OSError, ValueError):
        return None
    if (validated != logs or path.parent != logs or path.name in ("", ".", "..",
            "dispatch-session-sweep.log", "dispatch-pending-delivery.log")
            or ".segment." in path.name or path.name.endswith(".lock")):
        return None
    return path.name


def _log_protection(ctx, attempt, *, probe_processes):
    """Shared attempt decision plus the log-only failing-review consumer."""
    from dispatch_retention import Protection
    from worker_home_retention import _protections, _route_state

    verdict = _protections(ctx, "", attempt, probe_processes=probe_processes)
    row = ctx.snapshot.rows.get(attempt)
    if (verdict.kind == "eligible" and row is not None
            and row.meta.get("worker_type") == "review"
            and row.meta.get("failure_class") == "fail"):
        state, _activity = _route_state(ctx.jobs, row.meta)
        if state != "closed":
            return Protection("keep", "review-consumer-open")
    return verdict


def _unlink_if_same(layout, name, expected):
    """Unlink only a pinned regular inode that still occupies the exact name."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=layout.logs_fd)
    except OSError:
        return False
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_uid != os.geteuid() or _stamp(opened) != expected):
            return False
        current = os.stat(name, dir_fd=layout.logs_fd, follow_symlinks=False)
        if _stamp(current) != expected:
            return False
        os.unlink(name, dir_fd=layout.logs_fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _fresh_eligible(jobs, root, layout, log_file, expected_rows, expected_stamp, now_ns):
    from dispatch_contract import process_table_scan_scope
    from worker_home_retention import _Context, _Snapshot, _declared_index

    try:
        snapshot = _Snapshot(jobs)
        if log_file in snapshot.unknown_log_refs:
            return False
        current_refs = snapshot.log_refs.get(log_file, {})
        if set(current_refs) != set(expected_rows):
            return False
        for attempt, old in expected_rows.items():
            current = snapshot.rows.get(attempt)
            if (current is None or current.raw != old.raw or current_refs[attempt].raw != old.raw
                    or current.meta.get("log_file") != log_file):
                return False
        ctx = _Context(jobs, root, layout, snapshot, now_ns)
        ctx.by_key = _declared_index(snapshot, root, ctx.workers_real)
        with process_table_scan_scope():
            for attempt in expected_rows:
                verdict = _log_protection(ctx, attempt, probe_processes=True)
                if verdict.kind != "eligible":
                    return False
                row = snapshot.rows[attempt]
                terminal = _terminal_ns(row.fields[0])
                if terminal is None or max(terminal, verdict.activity_ns) > now_ns:
                    return False
        current_stat = os.stat(Path(log_file).name, dir_fd=layout.logs_fd, follow_symlinks=False)
        return (_stamp(current_stat) == expected_stamp and stat.S_ISREG(current_stat.st_mode)
                and current_stat.st_nlink == 1 and current_stat.st_uid == os.geteuid())
    except Exception:  # noqa: BLE001 -- unreadable evidence keeps the bytes
        return False


def sweep(jobs, *, now_ns=None, force=False):
    """Run one best-effort pass; unknown evidence and failures always keep files."""
    started = time.monotonic()
    now_ns = time.time_ns() if now_ns is None else int(now_ns)
    try:
        jobs = Path(jobs).resolve(strict=False)
        root = dispatch_state_root(jobs)
        log_dir = validate_dispatch_log_dir(jobs, root / "logs")
        if log_dir != root / "logs":
            return {"skipped": "log-dir-noncanonical"}
        log_info = os.stat(log_dir, follow_symlinks=False)
        if not stat.S_ISDIR(log_info.st_mode):
            return {"skipped": "log-dir-not-directory"}
        with _LogLayout(root) as layout:
            with _advisory_lock(layout) as locked:
                if not locked:
                    return {"skipped": "busy"}
                state = _read_state(layout)
                if (not force and state["last_pass_ns"] <= now_ns
                        and now_ns - state["last_pass_ns"] < DUE_SECONDS * 1_000_000_000):
                    return {"skipped": "not-due"}
                return _pass(jobs, root, layout, state, now_ns, started)
    except FileNotFoundError:
        return {"skipped": "logs-missing"}
    except (OSError, DispatchContractError) as exc:
        return {"skipped": "io-error", "error": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001
        return {"skipped": "error", "error": type(exc).__name__}


def _pass(jobs, root, layout, state, now_ns, started):
    from dispatch_contract import process_table_scan_scope
    from worker_home_retention import _Context, _Snapshot

    names = []
    with os.scandir(layout.logs_fd) as listing:
        for entry in listing:
            names.append(entry.name)
            if len(names) > MAX_DIRECTORY_ENTRIES:
                result = {"skipped": "directory-limit", "observed_entries": len(names)}
                state.update(last_pass_ns=now_ns, last_result=result)
                _write_state(layout, state)
                return result
    names.sort()
    total_bytes = 0
    stamps = {}
    for name in names:
        try:
            info = os.stat(name, dir_fd=layout.logs_fd, follow_symlinks=False)
        except OSError:
            continue
        stamps[name] = _stamp(info)
        total_bytes += max(0, info.st_size)

    try:
        snapshot = _Snapshot(jobs)
    except Exception as exc:  # noqa: BLE001
        result = {"skipped": "registry-unreadable", "error": type(exc).__name__,
                  "total_bytes": total_bytes, "removed": 0}
        state.update(last_pass_ns=now_ns, last_result=result)
        _write_state(layout, state)
        return result

    # A path with any malformed or unidentifiable registry reference remains
    # protected even if another row happens to bind it correctly.
    rows_by_name = {}
    for log_file, rows in snapshot.log_refs.items():
        name = _bound_name(jobs, root, log_file)
        if name is None:
            continue
        rows_by_name.setdefault(name, {}).update(rows)
    unknown_names = {
        name for path in snapshot.unknown_log_refs
        if (name := _bound_name(jobs, root, path)) is not None
    }

    referenced_names = set(rows_by_name)
    ordered = sorted(referenced_names)
    cursor = state.get("cursor", "")
    after = [name for name in ordered if name > cursor]
    selected_names = (after + [name for name in ordered if name <= cursor])[:EXAMINE_LIMIT]
    if selected_names:
        state["cursor"] = selected_names[-1]

    protected_names = set()
    eligible = []
    unknown_bytes = 0
    reasons = {}
    ctx = _Context(jobs, root, layout, snapshot, now_ns)
    with process_table_scan_scope():
        for name in selected_names:
            if time.monotonic() - started > MAX_PASS_SECONDS:
                break
            expected_stamp = stamps.get(name)
            if expected_stamp is None:
                protected_names.add(name)
                unknown_names.add(name)
                continue
            mode, links = expected_stamp[2], expected_stamp[3]
            if (not stat.S_ISREG(mode) or links != 1 or expected_stamp[7] != os.geteuid()
                    or name in unknown_names):
                protected_names.add(name)
                unknown_names.add(name)
                continue
            rows = rows_by_name[name]
            if len(rows) == 0:
                protected_names.add(name)
                continue
            terminal_values = []
            all_eligible = True
            for attempt, row in rows.items():
                if row.meta.get("attempt_id") != attempt or row.meta.get("harness") not in {"claude", "codex", "opencode"}:
                    all_eligible = False
                    unknown_names.add(name)
                    break
                verdict = _log_protection(ctx, attempt, probe_processes=True)
                if verdict.kind != "eligible":
                    reasons[verdict.reason or verdict.kind] = reasons.get(verdict.reason or verdict.kind, 0) + 1
                    all_eligible = False
                    if verdict.kind == "unknown":
                        unknown_names.add(name)
                    break
                terminal = _terminal_ns(row.fields[0])
                if terminal is None:
                    all_eligible = False
                    unknown_names.add(name)
                    break
                settled = max(terminal, verdict.activity_ns)
                if settled > now_ns:
                    all_eligible = False
                    unknown_names.add(name)
                    break
                terminal_values.append(settled)
            if all_eligible and terminal_values:
                eligible.append((name, max(terminal_values), expected_stamp, rows))
            else:
                protected_names.add(name)

    selected_set = set(selected_names)
    known_names = set(rows_by_name)
    unknown_names.update(set(stamps) - known_names)
    unknown_names.update(known_names - selected_set)
    unknown_bytes = sum(max(0, stamps[name][4]) for name in unknown_names if name in stamps)
    protected_bytes = sum(max(0, stamps[name][4]) for name in protected_names - unknown_names if name in stamps)
    eligible_bytes = sum(max(0, item[2][4]) for item in eligible)
    expired = [item for item in eligible if now_ns - item[1] >= RETENTION_NS]
    expired.sort(key=lambda item: (item[1], item[0]))
    selected = list(expired)
    projected = total_bytes - sum(max(0, item[2][4]) for item in selected)
    if projected > MAX_LOG_BYTES:
        for item in sorted((item for item in eligible if item not in selected), key=lambda x: (x[1], x[0])):
            if projected <= MAX_LOG_BYTES:
                break
            selected.append(item)
            projected -= max(0, item[2][4])
    removed = 0
    removed_bytes = 0
    deferred = 0
    for name, settled, expected_stamp, rows in selected:
        if removed >= REMOVE_LIMIT or time.monotonic() - started > MAX_PASS_SECONDS:
            deferred += 1
            continue
        log_file = str(root / "logs" / name)
        if not _fresh_eligible(jobs, root, layout, log_file, rows, expected_stamp, now_ns):
            protected_names.add(name)
            unknown_names.add(name)
            deferred += 1
            continue
        if _unlink_if_same(layout, name, expected_stamp):
            removed += 1
            removed_bytes += expected_stamp[4]
        else:
            protected_names.add(name)
            unknown_names.add(name)
            deferred += 1
    result = {
        "examined": len(selected_names), "eligible": len(eligible), "eligible_bytes": eligible_bytes,
        "removed": removed, "removed_bytes": removed_bytes, "deferred": deferred,
        "total_bytes": total_bytes, "protected_bytes": protected_bytes,
        "unknown_bytes": unknown_bytes,
        "over_limit_bytes": max(0, total_bytes - removed_bytes - MAX_LOG_BYTES),
        "kept_reasons": reasons,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    state.update(last_pass_ns=now_ns, last_result=result)
    _write_state(layout, state)
    return result


def sweep_best_effort(jobs):
    try:
        return sweep(jobs)
    except Exception:  # noqa: BLE001 -- never change the caller's result
        return None
