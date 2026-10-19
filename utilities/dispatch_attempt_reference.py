#!/usr/bin/env python3
"""Attempt-scoped admission for pending-delivery publication and HOME detach.

Delivery records expose their attempt references through ``attempt_ids`` and
``parent_attempt_id``.  Retention and the shared delivery writer use the same
attempt grammar here, so they can order a reference publication against one
candidate without serializing unrelated recipients or deliveries.

Lock files live beside (not inside) the recipient groups and are pruned by the
ordinary pending-delivery lifecycle.  A holder always verifies that its fd is
still the inode named by the path after flock; waiters on an unlinked inode
release it and reopen, matching the delivery-record lock's prune behavior.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Iterable, Iterator

import dispatch_lock_order


ATTEMPT_RE = re.compile(r"att-[A-Za-z0-9._-]{1,240}")
LOCK_DIRECTORY = "pending-delivery-reference-locks"
LOCK_SUFFIX = ".attempt-reference.lock"
REOPEN_LIMIT = 16
FILE_MODE = 0o600
DIR_MODE = 0o700
_HELD = threading.local()


class AttemptReferenceError(RuntimeError):
    """The reference-admission file could not be safely used."""


def referenced_attempts(attempt_ids: Iterable[object]) -> frozenset[str]:
    """Apply retention's exact relation rule: only ``att-*`` identities count."""
    return frozenset(value for value in attempt_ids
                     if isinstance(value, str) and ATTEMPT_RE.fullmatch(value))


def record_attempts(record: object) -> frozenset[str]:
    """Return valid references from a delivery record, including its parent."""
    if not isinstance(record, dict):
        return frozenset()
    values = record.get("attempt_ids")
    if not isinstance(values, (list, tuple, set, frozenset)):
        values = ()
    parent = record.get("parent_attempt_id")
    return referenced_attempts((*values, parent))


def held_attempts() -> frozenset[str]:
    """References held by this thread's active admission scopes."""
    return frozenset(getattr(_HELD, "attempts", ()))


def lock_path(root: Path, attempt_id: str) -> Path:
    if not isinstance(attempt_id, str) or not ATTEMPT_RE.fullmatch(attempt_id):
        raise AttemptReferenceError("attempt-reference-identity-invalid")
    digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
    return Path(root) / LOCK_DIRECTORY / f"{digest}{LOCK_SUFFIX}"


def lock_paths(root: Path, attempt_ids: Iterable[object]) -> tuple[Path, ...]:
    """Sorted lock paths give every multi-reference writer one acquisition order."""
    return tuple(sorted(lock_path(root, attempt) for attempt in referenced_attempts(attempt_ids)))


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise AttemptReferenceError("attempt-reference-directory-unavailable") from exc
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise AttemptReferenceError("attempt-reference-directory-untrusted")
    try:
        try:
            os.chmod(path, DIR_MODE, follow_symlinks=False)
        except NotImplementedError:
            # glibc < 2.32 has no no-follow chmod. The lstat above proved a
            # real directory, so change it through a no-follow descriptor.
            fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                         getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fchmod(fd, DIR_MODE)
            finally:
                os.close(fd)
    except OSError as exc:
        raise AttemptReferenceError("attempt-reference-directory-unavailable") from exc


def _open_locked(path: Path, *, blocking: bool) -> int | None:
    for _attempt in range(REOPEN_LIMIT):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC |
                         getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
        except OSError as exc:
            raise AttemptReferenceError("attempt-reference-lock-unavailable") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
                raise AttemptReferenceError("attempt-reference-lock-untrusted")
            os.fchmod(fd, FILE_MODE)
            while True:
                try:
                    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                    fcntl.flock(fd, flags)
                    break
                except InterruptedError:
                    continue
                except BlockingIOError:
                    os.close(fd)
                    return None
                except OSError as exc:
                    raise AttemptReferenceError("attempt-reference-lock-unavailable") from exc
            current = None
            try:
                current = os.stat(path, follow_symlinks=False)
                current_identity = (current.st_dev, current.st_ino)
                opened_identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            except OSError:
                current_identity = None
                opened_identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            if (current_identity == opened_identity and current is not None
                    and stat.S_ISREG(current.st_mode) and current.st_uid == os.geteuid()):
                now_ns = time.time_ns()
                os.utime(fd, ns=(now_ns, now_ns))
                return fd
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BaseException:
            # The nonblocking miss already closed the descriptor.
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        os.close(fd)
    raise AttemptReferenceError("attempt-reference-lock-replaced")


@contextmanager
def admission(root: Path, attempt_ids: Iterable[object], *, blocking: bool = True
              ) -> Iterator[bool]:
    """Acquire all related attempt locks, or yield False for nonblocking GC.

    Normal writers wait on only the references they publish.  Retention uses
    ``blocking=False`` after acquiring ``jobs.log.lock``; contention defers
    that candidate to its ordinary next sweep.  The lock-order declaration is
    entered only after the complete sorted OS lock set is held.
    """
    references = referenced_attempts(attempt_ids)
    paths = lock_paths(Path(root), references)
    handles: list[int] = []
    acquired_all = True
    try:
        if paths:
            _ensure_directory(paths[0].parent)
        for path in paths:
            fd = _open_locked(path, blocking=blocking)
            if fd is None:
                acquired_all = False
                break
            handles.append(fd)
        if not acquired_all:
            yield False
            return
        if handles:
            with dispatch_lock_order.acquired("delivery-reference"):
                previous = held_attempts()
                _HELD.attempts = previous | references
                try:
                    yield True
                finally:
                    _HELD.attempts = previous
        else:
            yield True
    finally:
        for fd in reversed(handles):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def prune_stale_lock(path: Path, *, cutoff_ns: int) -> str:
    """Remove one old reference mutex under its flock; waiters reopen safely."""
    path = Path(path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                         getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unavailable"
    try:
        parent = os.fstat(dir_fd)
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
            return "unavailable"
        try:
            fd = os.open(path.name, os.O_RDWR | os.O_CLOEXEC |
                         getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unavailable"
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
                return "unavailable"
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    break
                except InterruptedError:
                    continue
                except OSError:
                    return "unavailable"
            with dispatch_lock_order.acquired("delivery-reference"):
                try:
                    current_parent = os.stat(path.parent, follow_symlinks=False)
                except OSError:
                    return "replaced"
                if ((parent.st_dev, parent.st_ino) !=
                        (current_parent.st_dev, current_parent.st_ino)
                        or not stat.S_ISDIR(current_parent.st_mode)):
                    return "replaced"
                try:
                    current = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return "missing"
                if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                        or not stat.S_ISREG(current.st_mode)
                        or current.st_uid != os.geteuid()):
                    return "replaced"
                if current.st_mtime_ns >= cutoff_ns:
                    return "recent"
                try:
                    os.unlink(path.name, dir_fd=dir_fd)
                except FileNotFoundError:
                    return "missing"
                except OSError:
                    return "unavailable"
                return "removed"
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
    finally:
        os.close(dir_fd)
