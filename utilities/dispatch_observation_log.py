"""Append and rotate bounded dispatch observation logs at record boundaries."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import time

MAX_ACTIVE_BYTES = 16 * 1024 * 1024
MAX_RETAINED_BYTES = 2 * 1024 * 1024 * 1024
RETENTION_NS = 14 * 86400 * 1_000_000_000
MAX_RECORD_BYTES = 64 * 1024
LOG_NAMES = frozenset({"dispatch-session-sweep.log", "dispatch-pending-delivery.log"})
WRITER_LOCK = ".dispatch-observation-log.lock"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_uid)


def _unlink_segment(dir_fd, name, expected):
    try:
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (_stamp(current) != expected or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1 or current.st_uid != os.geteuid()):
            return False
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=dir_fd)
        try:
            if _stamp(os.fstat(fd)) != expected:
                return False
            if _stamp(os.stat(name, dir_fd=dir_fd, follow_symlinks=False)) != expected:
                return False
            os.unlink(name, dir_fd=dir_fd)
            return True
        finally:
            os.close(fd)
    except OSError:
        return False


def _rotate_and_prune(dir_fd, name, now_ns):
    try:
        active = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        active = None
    except OSError:
        return
    if (active is not None and (not stat.S_ISREG(active.st_mode) or active.st_nlink != 1
                                or active.st_uid != os.geteuid())):
        return
    if active is not None and active.st_size >= MAX_ACTIVE_BYTES:
        segment = f"{name}.segment.{now_ns}.{secrets.token_hex(4)}"
        try:
            if _stamp(os.stat(name, dir_fd=dir_fd, follow_symlinks=False)) != _stamp(active):
                return
            os.rename(name, segment, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError:
            return

    segments = []
    try:
        for entry in os.scandir(dir_fd):
            if not any(entry.name.startswith(log_name + ".segment.") for log_name in LOG_NAMES):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and info.st_uid == os.geteuid()):
                segments.append((entry.name, _stamp(info)))
    except OSError:
        return
    total = sum(item[1][4] for item in segments)
    for segment, expected in sorted(segments, key=lambda item: (item[1][5], item[0])):
        expired = now_ns - expected[5] >= RETENTION_NS
        if not expired and total <= MAX_RETAINED_BYTES:
            continue
        if _unlink_segment(dir_fd, segment, expected):
            total -= expected[4]


def append_record(root, name, record):
    """Append one JSON record; all failures are advisory and return ``False``."""
    if name not in LOG_NAMES:
        return False
    try:
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False) + "\n").encode("utf-8")
        if len(payload) > MAX_RECORD_BYTES:
            return False
        root = Path(root).resolve(strict=False)
        root_fd = os.open(root, _DIR_FLAGS)
        try:
            if os.fstat(root_fd).st_uid != os.geteuid():
                return False
            try:
                os.mkdir("logs", mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            logs_fd = os.open("logs", _DIR_FLAGS, dir_fd=root_fd)
            try:
                if os.fstat(logs_fd).st_uid != os.geteuid():
                    return False
                lock_fd = os.open(WRITER_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                                  0o600, dir_fd=logs_fd)
                try:
                    lock_info = os.fstat(lock_fd)
                    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid():
                        return False
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    now_ns = time.time_ns()
                    _rotate_and_prune(logs_fd, name, now_ns)
                    fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=logs_fd)
                    try:
                        info = os.fstat(fd)
                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                or info.st_uid != os.geteuid()):
                            return False
                        offset = 0
                        while offset < len(payload):
                            written = os.write(fd, payload[offset:])
                            if written <= 0:
                                return False
                            offset += written
                    finally:
                        os.close(fd)
                    _rotate_and_prune(logs_fd, name, now_ns)
                    return True
                finally:
                    with contextlib.suppress(OSError):
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
            finally:
                os.close(logs_fd)
        finally:
            os.close(root_fd)
    except (OSError, TypeError, ValueError):
        return False
