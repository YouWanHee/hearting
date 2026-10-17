"""Shared self-exclusion marker semantics for cycle payload candidates."""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any, Tuple

CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
_MARKERS = ("CACHEDIR.TAG", "pyvenv.cfg", ".gitignore")
_GITIGNORE_ALL = frozenset((b"*", b"*\n", b"*\r\n"))
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_LEAF_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _identity(info: os.stat_result) -> Tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _open_directory_chain(path: Path) -> int:
    """Open an absolute directory path component by component without following links."""
    absolute = Path(os.path.abspath(path))
    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise OSError(errno.ENOTSUP, "no-follow directory opens are unavailable", str(path))
    fd = os.open(absolute.anchor or "/", _DIR_FLAGS)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.ENOTDIR, "not a directory", str(path))
        return fd
    except BaseException:
        os.close(fd)
        raise


def _stat_marker(directory_fd: int, name: str) -> Any:
    try:
        return _identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        return ("error", exc.errno)


def directory_snapshot(directory: Path) -> Any:
    """Return no-follow directory/marker identities without reading marker bytes."""
    fd = None
    try:
        fd = _open_directory_chain(Path(directory))
        directory_id = _identity(os.fstat(fd))
        markers = tuple((name, _stat_marker(fd, name)) for name in _MARKERS)
        return directory_id, markers
    except OSError as exc:
        return ("error", exc.errno)
    finally:
        if fd is not None:
            os.close(fd)


def snapshot_matches(directory: Path, snapshot: Any) -> bool:
    """Check an earlier observation using stat only; safe to use while holding a lock."""
    return directory_snapshot(directory) == snapshot


def snapshot_differences(directory: Path, snapshot: Any) -> Tuple[bool, bool]:
    """Return `(directory_changed, marker_changed)` using stat only."""
    current = directory_snapshot(directory)
    if current == snapshot:
        return False, False
    if (isinstance(current, tuple) and len(current) == 2
            and isinstance(snapshot, tuple) and len(snapshot) == 2):
        return current[0] != snapshot[0], current[1] != snapshot[1]
    return True, True


def observe_directory(directory: Path) -> Tuple[bool, Any]:
    """Return `(self_excluded, snapshot)` using no-follow, regular-file-only reads.

    Unreadable, replaced, linked, and non-regular markers are not exclusion evidence.
    At most 43 bytes are read from CACHEDIR.TAG and 4 bytes from .gitignore.
    """
    fd = None
    excluded = False
    try:
        fd = _open_directory_chain(Path(directory))
        directory_id = _identity(os.fstat(fd))
        observations = []
        for name in _MARKERS:
            try:
                before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                observations.append((name, None if exc.errno == errno.ENOENT else ("error", exc.errno)))
                continue
            marker_id = _identity(before)
            valid = False
            if (stat.S_ISREG(before.st_mode) and getattr(os, "O_NOFOLLOW", 0)
                    and getattr(os, "O_NONBLOCK", 0)):
                marker_fd = None
                try:
                    marker_fd = os.open(name, _LEAF_FLAGS, dir_fd=fd)
                    opened = os.fstat(marker_fd)
                    if stat.S_ISREG(opened.st_mode) and _identity(opened) == marker_id:
                        if name == "pyvenv.cfg":
                            valid = True
                        else:
                            limit = len(CACHEDIR_SIGNATURE) if name == "CACHEDIR.TAG" else 4
                            with os.fdopen(os.dup(marker_fd), "rb") as stream:
                                data = stream.read(limit)
                            after = os.fstat(marker_fd)
                            if _identity(after) == marker_id:
                                valid = (data == CACHEDIR_SIGNATURE if name == "CACHEDIR.TAG"
                                         else data in _GITIGNORE_ALL)
                            else:
                                marker_id = ("unstable", _identity(after))
                except OSError:
                    pass
                finally:
                    if marker_fd is not None:
                        os.close(marker_fd)
            observations.append((name, marker_id))
            excluded = excluded or valid
        return excluded, (directory_id, tuple(observations))
    except OSError as exc:
        return False, ("error", exc.errno)
    finally:
        if fd is not None:
            os.close(fd)


def self_excluded_directory(directory: Path) -> bool:
    """Whether this exact payload directory carries one of the three markers."""
    return observe_directory(directory)[0]
