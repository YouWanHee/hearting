#!/usr/bin/env python3
"""Bounded reuse of small immutable projections over stable file generations.

Callers provide the complete file dependency set and a loader that performs
the real domain verification. Only immutable, bounded result projections are
retained; claims, recipients, process liveness, and transition authority do not
belong in this cache.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import threading
import time
from typing import Callable, Hashable, Iterable, TypeVar


T = TypeVar("T")
SMALL_MUTABLE_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class FileGeneration:
    path_identity: str
    resolved_path: str
    signature: tuple[int, int, int, int, int]
    content_digest: str | None = None
    kind: str = "regular"


@dataclass(frozen=True)
class _Entry:
    generations: tuple[FileGeneration, ...]
    value: object
    weight: int
    expires_at: float | None


def file_generation(path: str | os.PathLike[str]) -> FileGeneration:
    """Return metadata plus a content generation for bounded mutable records."""
    candidate = Path(path).expanduser()
    identity = str(candidate.absolute())
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    for _attempt in range(3):
        fd = None
        try:
            before = candidate.stat()
            resolved = str(candidate.resolve(strict=True))
            if stat.S_ISDIR(before.st_mode):
                signature = (before.st_dev, before.st_ino, before.st_size,
                             before.st_mtime_ns, before.st_ctime_ns)
                after_path = candidate.stat()
                after_resolved = str(candidate.resolve(strict=True))
                if (signature != (after_path.st_dev, after_path.st_ino, after_path.st_size,
                                  after_path.st_mtime_ns, after_path.st_ctime_ns)
                        or resolved != after_resolved):
                    continue
                return FileGeneration(identity, resolved, signature, None, "directory")
            if not stat.S_ISREG(before.st_mode):
                raise OSError("observation-file-not-regular")
            fd = os.open(candidate, flags)
            opened = os.fstat(fd)
            signature = (opened.st_dev, opened.st_ino, opened.st_size,
                         opened.st_mtime_ns, opened.st_ctime_ns)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("observation-file-not-regular")
            if signature != (before.st_dev, before.st_ino, before.st_size,
                             before.st_mtime_ns, before.st_ctime_ns):
                continue
            digest = None
            if opened.st_size <= SMALL_MUTABLE_FILE_BYTES:
                hasher = hashlib.sha256()
                while True:
                    chunk = os.read(fd, min(64 * 1024, SMALL_MUTABLE_FILE_BYTES + 1))
                    if not chunk:
                        break
                    hasher.update(chunk)
                after_fd = os.fstat(fd)
                after_path = candidate.stat()
                final = (after_fd.st_dev, after_fd.st_ino, after_fd.st_size,
                         after_fd.st_mtime_ns, after_fd.st_ctime_ns)
                if (final != signature or final != (after_path.st_dev, after_path.st_ino,
                        after_path.st_size, after_path.st_mtime_ns, after_path.st_ctime_ns)):
                    continue
                digest = hasher.hexdigest()
            else:
                after_path = candidate.stat()
                if signature != (after_path.st_dev, after_path.st_ino, after_path.st_size,
                                 after_path.st_mtime_ns, after_path.st_ctime_ns):
                    continue
            after_resolved = str(candidate.resolve(strict=True))
            if resolved != after_resolved:
                continue
            return FileGeneration(identity, resolved, signature, digest, "regular")
        except FileNotFoundError:
            raise
        except OSError:
            raise
        finally:
            if fd is not None:
                os.close(fd)
    raise OSError("observation-file-changed-during-generation")


def stable_read_bytes(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024,
                      retries: int = 3) -> bytes:
    """Read one exact regular-file generation or raise without returning old bytes."""
    candidate = Path(path).expanduser()
    identity = str(candidate.absolute())
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    last_error = None
    for _attempt in range(max(1, retries)):
        fd = None
        try:
            before = file_generation(candidate)
            fd = os.open(candidate, flags)
            fd_before = os.fstat(fd)
            if (fd_before.st_dev, fd_before.st_ino, fd_before.st_size,
                    fd_before.st_mtime_ns, fd_before.st_ctime_ns) != before.signature:
                raise OSError("observation-file-changed-before-read")
            chunks = []
            size = 0
            while True:
                chunk = os.read(fd, chunk_size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            fd_after = os.fstat(fd)
            after = file_generation(candidate)
            signature = (fd_after.st_dev, fd_after.st_ino, fd_after.st_size,
                         fd_after.st_mtime_ns, fd_after.st_ctime_ns)
            if (before != after or before.signature != signature
                    or size != before.signature[2]):
                raise OSError("observation-file-changed-during-read")
            return b"".join(chunks)
        except OSError as exc:
            last_error = exc
        finally:
            if fd is not None:
                os.close(fd)
    raise OSError(getattr(last_error, "errno", None),
                  f"could not read stable observation file: {identity}") from last_error


class ObservationContext:
    """Per-observer LRU for immutable projections tied to exact file generations."""

    def __init__(self, *, max_entries: int = 128, max_weight: int = 4 * 1024 * 1024):
        if max_entries < 1 or max_weight < 1:
            raise ValueError("observation-cache-bounds-invalid")
        self.max_entries = max_entries
        self.max_weight = max_weight
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._states: OrderedDict[Hashable, tuple[object, float | None]] = OrderedDict()
        self._weight = 0
        self._lock = threading.RLock()
        self._metrics = {"hits": 0, "misses": 0, "invalidations": 0,
                         "loads": 0, "evictions": 0,
                         "state_hits": 0, "state_misses": 0, "state_evictions": 0}

    @staticmethod
    def _generations(paths: Iterable[str | os.PathLike[str]]) -> tuple[FileGeneration, ...]:
        return tuple(file_generation(path) for path in paths)

    def memoize(
        self,
        key: Hashable,
        dependencies: Iterable[str | os.PathLike[str]],
        loader: Callable[[], T],
        *,
        weight: int | Callable[[T], int] = 1,
        ttl: float | None = None,
    ) -> T:
        """Load a small immutable projection once per exact dependency generation.

        A failed stat or loader is never cached. Generations are checked both
        before and after the loader, so a concurrent replacement cannot publish
        a result under the old snapshot. ``weight`` is a caller-estimated bound
        on the retained projection.
        """
        if isinstance(weight, int) and (weight < 1 or weight > self.max_weight):
            raise ValueError("observation-projection-weight-invalid")
        paths = tuple(Path(path) for path in dependencies)
        try:
            before = self._generations(paths)
        except OSError:
            # A missing/unreadable dependency invalidates the old projection;
            # it must never survive a failed observation and become stale
            # success when this key is requested again.
            with self._lock:
                if key in self._entries:
                    self._drop(key)
                    self._metrics["invalidations"] += 1
            raise
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if (entry.generations == before
                        and (entry.expires_at is None or now < entry.expires_at)):
                    self._entries.move_to_end(key)
                    self._metrics["hits"] += 1
                    return entry.value  # type: ignore[return-value]
                self._drop(key)
                self._metrics["invalidations"] += 1
            self._metrics["misses"] += 1
            self._metrics["loads"] += 1

        value = loader()
        after = self._generations(paths)
        if before != after:
            with self._lock:
                self._drop(key)
                self._metrics["invalidations"] += 1
            raise OSError("observation-dependency-changed-during-read")

        retained_weight = weight(value) if callable(weight) else weight
        if retained_weight < 1 or retained_weight > self.max_weight:
            return value

        expires_at = None if ttl is None else time.monotonic() + max(0.0, ttl)
        with self._lock:
            self._drop(key)
            self._entries[key] = _Entry(after, value, retained_weight, expires_at)
            self._weight += retained_weight
            while (len(self._entries) > self.max_entries
                   or self._weight > self.max_weight):
                oldest = next(iter(self._entries))
                self._drop(oldest)
                self._metrics["evictions"] += 1
        return value

    def discard(self, key: Hashable) -> None:
        with self._lock:
            self._drop(key)

    def state(self, key: Hashable, factory: Callable[[], T], *, ttl: float = 3600.0) -> T:
        """Return bounded in-memory observer bookkeeping, never cached authority.

        This is for retry deadlines and diagnostic cadence across short
        checkpoints. Callers must re-observe all file/process/recipient
        authority before acting on the work.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._states.get(key)
            if entry is not None and (entry[1] is None or now < entry[1]):
                self._states.move_to_end(key)
                self._metrics["state_hits"] += 1
                return entry[0]  # type: ignore[return-value]
            if entry is not None:
                self._states.pop(key, None)
            self._metrics["state_misses"] += 1
            value = factory()
            expires_at = None if ttl <= 0 else now + ttl
            self._states[key] = (value, expires_at)
            while len(self._states) > self.max_entries:
                self._states.popitem(last=False)
                self._metrics["state_evictions"] += 1
            return value

    def _drop(self, key: Hashable) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._weight -= entry.weight

    def metrics_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {**self._metrics, "entries": len(self._entries),
                    "weight": self._weight}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._states.clear()
            self._weight = 0
