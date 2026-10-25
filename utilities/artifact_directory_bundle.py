"""Bounded-memory directory bundle selection, descriptor, and tree verification."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple

import artifact_manifest

SCHEMA = "hearting-directory-bundle/v1"
ALGORITHM = "sha256-canonical-leaf-list/v1"
MEDIA_TYPE = "application/vnd.hearting.directory-bundle+json"
CHUNK_BYTES = 1024 * 1024
MAX_DESCRIPTOR_BYTES = 64 * 1024


class DirectoryBundleError(ValueError):
    def __init__(self, code: str, path: str = "", detail: str = "") -> None:
        super().__init__(f"{code}: {path}: {detail}" if detail else f"{code}: {path}")
        self.code, self.path, self.detail = code, path, detail or code


@dataclass(frozen=True)
class Leaf:
    path: str
    digest: str
    size: int
    fingerprint: Tuple[int, int, int, int, int]


@dataclass(frozen=True)
class Tree:
    directory: str
    leaves: Tuple[Leaf, ...]
    directories: Tuple[Tuple[str, Tuple[int, int, int, int, int]], ...]
    body: Mapping[str, Any]
    descriptor: bytes
    descriptor_digest: str


def _identity(st: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _open_directory_chain(path: Path) -> int:
    """Open every absolute path component without following a symlink."""
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor or "/", flags)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_relative_directory(root_fd: int, relative: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.dup(root_fd)
    try:
        for part in relative.split("/"):
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DirectoryBundleError("descriptor-invalid", detail=str(exc)) from exc


def canonical_descriptor_bytes(body: Mapping[str, Any]) -> bytes:
    validate_body(body)
    return _canonical(body)


def parse_descriptor(data: bytes) -> Dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DirectoryBundleError("descriptor-duplicate-key", key)
            result[key] = value
        return result
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda token: (_ for _ in ()).throw(
                               DirectoryBundleError("descriptor-invalid-number", token)))
    except DirectoryBundleError:
        raise
    except (UnicodeError, ValueError, TypeError) as exc:
        raise DirectoryBundleError("descriptor-invalid", detail=str(exc)) from exc
    validate_body(value)
    if _canonical(value) != data:
        raise DirectoryBundleError("descriptor-noncanonical")
    return value


def validate_body(body: Any) -> None:
    keys = {"schema", "directory", "file_count", "total_bytes", "tree_digest", "digest_algorithm"}
    if not isinstance(body, dict) or set(body) != keys:
        raise DirectoryBundleError("descriptor-unknown-or-missing-field")
    if body["schema"] != SCHEMA or body["digest_algorithm"] != ALGORITHM:
        raise DirectoryBundleError("descriptor-schema-or-algorithm")
    directory = body["directory"]
    if not isinstance(directory, dict) or set(directory) != {"kind", "path"} or directory["kind"] != "cycle-relative":
        raise DirectoryBundleError("descriptor-directory-invalid")
    path = directory["path"]
    if (not isinstance(path, str) or not path.startswith("artifacts/")
            or path.rsplit("/", 1)[-1] != "raw-results"
            or not artifact_manifest.validate_locator_path(path).ok):
        raise DirectoryBundleError("descriptor-directory-invalid", str(path))
    if isinstance(body["file_count"], bool) or not isinstance(body["file_count"], int) or body["file_count"] <= 0:
        raise DirectoryBundleError("descriptor-file-count-invalid")
    if isinstance(body["total_bytes"], bool) or not isinstance(body["total_bytes"], int) or body["total_bytes"] < 0:
        raise DirectoryBundleError("descriptor-total-bytes-invalid")
    digest = body["tree_digest"]
    if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:") or any(c not in "0123456789abcdef" for c in digest[7:]):
        raise DirectoryBundleError("descriptor-tree-digest-invalid")


def select_outermost(artifacts: Path, *, prune_directory: Callable[[Path], bool] | None = None) -> Tuple[str, ...]:
    """Find outermost raw-results directories, optionally pruning candidate directories.

    The default keeps historical selection behavior for sealed descriptor coverage.
    """
    root = Path(artifacts)
    try:
        rst = os.lstat(root)
    except OSError as exc:
        raise DirectoryBundleError("artifacts-dir-missing", str(root), str(exc)) from exc
    if not stat.S_ISDIR(rst.st_mode):
        raise DirectoryBundleError("artifacts-dir-not-directory", str(root))
    if prune_directory is not None and prune_directory(root):
        return ()
    selected: List[str] = []
    stack = [(root, "artifacts")]

    def has_pruned_descendant(start: Path) -> bool:
        """A bundle candidate cannot cover a subtree that candidate collection prunes."""
        pending = [start]
        found = False
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    children = sorted(entries, key=lambda e: e.name)
            except OSError as exc:
                raise DirectoryBundleError("tree-read-failed", str(directory), str(exc)) from exc
            for child in children:
                try:
                    info = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise DirectoryBundleError("tree-read-failed", child.path, str(exc)) from exc
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    continue
                child_path = Path(child.path)
                if prune_directory is not None and prune_directory(child_path):
                    found = True
                    continue
                pending.append(child_path)
        return found

    while stack:
        directory, rel = stack.pop()
        try:
            with os.scandir(directory) as entries:
                rows = sorted(entries, key=lambda e: e.name.encode("utf-8"), reverse=True)
        except OSError as exc:
            raise DirectoryBundleError("tree-read-failed", rel, str(exc)) from exc
        for entry in rows:
            child_rel = f"{rel}/{entry.name}"
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise DirectoryBundleError("tree-read-failed", child_rel, str(exc)) from exc
            if stat.S_ISDIR(info.st_mode) and prune_directory is not None:
                if prune_directory(Path(entry.path)):
                    continue
            if entry.name == "raw-results":
                if stat.S_ISDIR(info.st_mode):
                    if prune_directory is None or not has_pruned_descendant(Path(entry.path)):
                        selected.append(child_rel)
                        continue
                    # Keep searching for nested candidates outside marked directories;
                    # the outer tree will be collected as ordinary files instead.
                    stack.append((Path(entry.path), child_rel))
                    continue
                if stat.S_ISLNK(info.st_mode):
                    raise DirectoryBundleError("raw-results-symlink", child_rel)
            if stat.S_ISDIR(info.st_mode):
                stack.append((Path(entry.path), child_rel))
    return tuple(sorted(selected, key=lambda p: p.encode("utf-8")))


def _hash_fd(fd: int, rel: str) -> Tuple[str, int, Tuple[int, int, int, int, int]]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise DirectoryBundleError("tree-non-regular", rel)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(os.dup(fd), "rb") as stream:
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    after = os.fstat(fd)
    if _identity(before) != _identity(after) or size != after.st_size:
        raise DirectoryBundleError("output-changed-while-reading", rel)
    return "sha256:" + digest.hexdigest(), size, _identity(before)


def scan_tree(cycle_root: Path, directory: str) -> Tree:
    root = Path(os.path.abspath(cycle_root))
    if not artifact_manifest.validate_locator_path(directory).ok:
        raise DirectoryBundleError("tree-locator-invalid", directory)
    root_fd = None
    try:
        root_fd = _open_directory_chain(root)
        base_fd = _open_relative_directory(root_fd, directory)
    except OSError as exc:
        if root_fd is not None:
            os.close(root_fd)
        raise DirectoryBundleError("tree-ancestor-unsafe", directory, str(exc)) from exc
    base_stat = os.fstat(base_fd)
    if not stat.S_ISDIR(base_stat.st_mode):
        os.close(base_fd)
        os.close(root_fd)
        raise DirectoryBundleError("tree-not-directory", directory)
    leaves: List[Leaf] = []
    dirs: List[Tuple[str, Tuple[int, int, int, int, int]]] = []
    listing: List[Tuple[str, str, Tuple[int, int, int, int, int]]] = []
    stack = [(base_fd, "")]
    opened_dirs = [base_fd]
    try:
      while stack:
        current_fd, inner = stack.pop()
        st = os.fstat(current_fd)
        dirs.append((inner, _identity(st)))
        try:
            with os.scandir(current_fd) as it:
                entries = sorted(it, key=lambda e: e.name.encode("utf-8"))
        except OSError as exc:
            raise DirectoryBundleError("tree-read-failed", inner, str(exc)) from exc
        for entry in entries:
            rel_inner = f"{inner}/{entry.name}" if inner else entry.name
            rel = f"{directory}/{rel_inner}"
            try:
                pre = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise DirectoryBundleError("tree-read-failed", rel, str(exc)) from exc
            if stat.S_ISLNK(pre.st_mode):
                raise DirectoryBundleError("tree-symlink-forbidden", rel)
            if stat.S_ISDIR(pre.st_mode):
                if not artifact_manifest.validate_locator_path(rel).ok:
                    raise DirectoryBundleError("tree-locator-invalid", rel)
                listing.append((rel_inner, "directory", _identity(pre)))
                try:
                    child_fd = os.open(entry.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                                       getattr(os, "O_NOFOLLOW", 0), dir_fd=current_fd)
                except OSError as exc:
                    raise DirectoryBundleError("tree-ancestor-unsafe", rel, str(exc)) from exc
                if _identity(os.fstat(child_fd)) != _identity(pre):
                    os.close(child_fd)
                    raise DirectoryBundleError("output-changed-while-reading", rel)
                opened_dirs.append(child_fd)
                stack.append((child_fd, rel_inner))
                continue
            if not stat.S_ISREG(pre.st_mode):
                raise DirectoryBundleError("tree-non-regular", rel)
            if not artifact_manifest.validate_locator_path(rel).ok:
                raise DirectoryBundleError("tree-locator-invalid", rel)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                fd = os.open(entry.name, flags, dir_fd=current_fd)
            except OSError as exc:
                raise DirectoryBundleError("tree-open-failed", rel, str(exc)) from exc
            try:
                fdst = os.fstat(fd)
                if _identity(pre) != _identity(fdst):
                    raise DirectoryBundleError("output-changed-while-reading", rel)
                digest, size, fingerprint = _hash_fd(fd, rel)
            finally:
                os.close(fd)
            try:
                post = os.stat(entry.name, dir_fd=current_fd, follow_symlinks=False)
            except OSError as exc:
                raise DirectoryBundleError("output-changed-while-reading", rel, str(exc)) from exc
            if _identity(post) != fingerprint:
                raise DirectoryBundleError("output-changed-while-reading", rel)
            leaves.append(Leaf(rel_inner, digest, size, fingerprint))
            listing.append((rel_inner, "file", fingerprint, digest))
    except BaseException:
      raise
    finally:
      for open_fd in opened_dirs:
        try: os.close(open_fd)
        except OSError: pass
      os.close(root_fd)
    if not leaves:
        raise DirectoryBundleError("tree-empty", directory)
    leaves.sort(key=lambda leaf: leaf.path.encode("utf-8"))
    tree_digest = "sha256:" + hashlib.sha256(_canonical([[x.path, x.digest, x.size] for x in leaves])).hexdigest()
    body = {"schema": SCHEMA, "directory": {"kind": "cycle-relative", "path": directory},
            "file_count": len(leaves), "total_bytes": sum(x.size for x in leaves),
            "tree_digest": tree_digest, "digest_algorithm": ALGORITHM}
    descriptor = canonical_descriptor_bytes(body)
    # Final inventory detects additions, removals, replacements, and directory races.
    check = scan_inventory(root, directory)
    if check != tuple(sorted(listing, key=lambda x: x[0].encode("utf-8"))):
        raise DirectoryBundleError("output-changed-while-reading", directory, "tree inventory changed")
    return Tree(directory, tuple(leaves), tuple(dirs), body, descriptor,
                "sha256:" + hashlib.sha256(descriptor).hexdigest())


def scan_inventory(cycle_root: Path, directory: str):
    rows = []
    root_fd = _open_directory_chain(cycle_root)
    base_fd = _open_relative_directory(root_fd, directory)
    stack = [(base_fd, "")]
    opened_dirs = [base_fd]
    try:
        while stack:
            current_fd, inner = stack.pop()
            try:
                entries = list(os.scandir(current_fd))
            except OSError as exc:
                raise DirectoryBundleError("tree-read-failed", inner, str(exc)) from exc
            for entry in entries:
                rel = f"{inner}/{entry.name}" if inner else entry.name
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise DirectoryBundleError("tree-read-failed", rel, str(exc)) from exc
                if stat.S_ISDIR(st.st_mode):
                    rows.append((rel, "directory", _identity(st)))
                    child = os.open(entry.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                                    getattr(os, "O_NOFOLLOW", 0), dir_fd=current_fd)
                    if _identity(os.fstat(child)) != _identity(st):
                        os.close(child)
                        raise DirectoryBundleError("output-changed-while-reading", f"{directory}/{rel}")
                    opened_dirs.append(child)
                    stack.append((child, rel))
                elif stat.S_ISREG(st.st_mode):
                    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
                    try:
                        fd = os.open(entry.name, flags, dir_fd=current_fd)
                    except OSError as exc:
                        raise DirectoryBundleError("tree-open-failed", f"{directory}/{rel}", str(exc)) from exc
                    try:
                        opened = os.fstat(fd)
                        if _identity(opened) != _identity(st):
                            raise DirectoryBundleError("output-changed-while-reading", f"{directory}/{rel}")
                        digest, size, fingerprint = _hash_fd(fd, f"{directory}/{rel}")
                        after = os.stat(entry.name, dir_fd=current_fd, follow_symlinks=False)
                        if (_identity(after) != fingerprint or size != after.st_size):
                            raise DirectoryBundleError("output-changed-while-reading", f"{directory}/{rel}")
                    finally:
                        os.close(fd)
                    rows.append((rel, "file", fingerprint, digest))
                else:
                    raise DirectoryBundleError("tree-unsafe-entry", f"{directory}/{rel}")
        return tuple(sorted(rows, key=lambda x: x[0].encode("utf-8")))
    finally:
        for fd in opened_dirs:
            try: os.close(fd)
            except OSError: pass
        os.close(root_fd)


def verify_coverage(cycle_root: Path, descriptor_path: str, row: Mapping[str, Any]) -> Tuple[str, ...]:
    if row.get("media_type") != MEDIA_TYPE:
        raise DirectoryBundleError("bundle-media-type-invalid", descriptor_path)
    root_fd = _open_directory_chain(Path(cycle_root))
    try:
        parent_rel, leaf_name = descriptor_path.rsplit("/", 1)
        parent_fd = _open_relative_directory(root_fd, parent_rel)
        st = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            raise DirectoryBundleError("bundle-descriptor-not-regular", descriptor_path)
        if st.st_size > MAX_DESCRIPTOR_BYTES:
            raise DirectoryBundleError("bundle-descriptor-size-limit", descriptor_path)
        fd = os.open(leaf_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                     getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(MAX_DESCRIPTOR_BYTES + 1)
        if len(data) > MAX_DESCRIPTOR_BYTES:
            raise DirectoryBundleError("bundle-descriptor-size-limit", descriptor_path)
    except OSError as exc:
        raise DirectoryBundleError("bundle-descriptor-unreadable", descriptor_path, str(exc)) from exc
    finally:
        os.close(root_fd)
        if 'parent_fd' in locals(): os.close(parent_fd)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if st.st_size != row.get("byte_size") or digest != row.get("content_digest"):
        raise DirectoryBundleError("bundle-descriptor-revision-mismatch", descriptor_path)
    body = parse_descriptor(data)
    selected = select_outermost(Path(cycle_root) / "artifacts")
    if body["directory"]["path"] not in selected:
        raise DirectoryBundleError("descriptor-directory-not-selected", body["directory"]["path"])
    tree = scan_tree(cycle_root, body["directory"]["path"])
    if tree.body != body:
        raise DirectoryBundleError("bundle-tree-mismatch", body["directory"]["path"])
    return tuple(f"{body['directory']['path']}/{leaf.path}" for leaf in tree.leaves)


def copy_file_streaming(source: Path, destination: Path, *, expected_digest: str | None = None,
                        expected_size: int | None = None) -> Tuple[str, int]:
    """Copy one regular payload with bounded reads, no-follow leaf open, and fsync."""
    src = Path(source)
    dst = Path(destination)
    parent_fd = None
    fd = None
    try:
        parent_fd = _open_directory_chain(src.parent)
        before_path = os.stat(src.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before_path.st_mode):
            raise DirectoryBundleError("tree-non-regular", str(src))
        fd = os.open(src.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                     getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
    except DirectoryBundleError:
        if parent_fd is not None: os.close(parent_fd)
        raise
    except OSError as exc:
        if parent_fd is not None: os.close(parent_fd)
        raise DirectoryBundleError("tree-open-failed", str(src), str(exc)) from exc
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(fd)
        if _identity(before_path) != _identity(before) or not stat.S_ISREG(before.st_mode):
            raise DirectoryBundleError("output-changed-while-reading", str(src))
        dst.parent.mkdir(parents=True, exist_ok=True)
        out_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(os.dup(fd), "rb") as rfh, os.fdopen(os.dup(out_fd), "wb") as wfh:
                while True:
                    chunk = rfh.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    wfh.write(chunk)
                wfh.flush()
                os.fsync(wfh.fileno())
            after = os.fstat(fd)
        finally:
            os.close(out_fd)
    except BaseException:
        try:
            dst.unlink()
        except OSError:
            pass
        raise
    finally:
        if fd is not None: os.close(fd)
        if parent_fd is not None: os.close(parent_fd)
    try:
        check_parent_fd = _open_directory_chain(src.parent)
        after_path = os.stat(src.name, dir_fd=check_parent_fd, follow_symlinks=False)
        os.close(check_parent_fd)
    except OSError as exc:
        dst.unlink(missing_ok=True)
        raise DirectoryBundleError("output-changed-while-reading", str(src), str(exc)) from exc
    actual = "sha256:" + digest.hexdigest()
    if (_identity(before) != _identity(after) or _identity(after_path) != _identity(after)
            or size != after.st_size or expected_size is not None and size != expected_size
            or expected_digest is not None and actual != expected_digest):
        dst.unlink(missing_ok=True)
        raise DirectoryBundleError("output-changed-while-reading", str(src))
    return actual, size
