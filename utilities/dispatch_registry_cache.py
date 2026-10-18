#!/usr/bin/env python3
"""Shared, generation-checked views of the dispatch registry.

The immutable view may be reused while the registry generation is unchanged.
Consumers must still observe their other inputs on each pass and recheck
authority under the existing lock before a state transition.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading


_RETRIES = 3
_CHUNK_SIZE = 1024 * 1024
_LOCK = threading.RLock()
_CACHE: dict[str, "RegistryView"] = {}
_METRICS = {
    "cache_hits": 0,
    "content_reads": 0,
    "bytes_read": 0,
    "full_metadata_parses": 0,
    "metadata_rows_parsed": 0,
    "invalidations": 0,
}


def _stat_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)


def _completion_metadata(raw: str) -> tuple[tuple[str, str], ...]:
    values = {}
    for part in raw.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return tuple(values.items())


def _handover_metadata(raw: str) -> tuple[tuple[str, str], ...]:
    values = {}
    for part in raw.split(","):
        key, separator, value = part.partition("=")
        if separator and key.strip():
            values[key.strip()] = value
    return tuple(values.items())


@dataclass(frozen=True)
class RegistryRecord:
    order: int
    raw: str
    fields: tuple[str, ...]
    completion_items: tuple[tuple[str, str], ...]
    handover_items: tuple[tuple[str, str], ...]

    @property
    def status(self) -> str:
        return self.fields[1]

    @property
    def slug(self) -> str:
        return self.fields[4]

    def completion_metadata(self) -> dict[str, str]:
        return dict(self.completion_items)

    def handover_metadata(self) -> dict[str, str]:
        return dict(self.handover_items)


class RegistryView:
    """One immutable lexical/metadata parse and its read-only lookup indexes."""

    __slots__ = (
        "path_identity", "resolved_path", "signature", "lines", "records",
        "_completion_latest", "_completion_errors", "_completion_all", "_completion_children",
        "_handover_latest", "_handover_by_parent", "_handover_by_binding",
        "_headless_owner_rows",
    )

    def __init__(self, path_identity: str, resolved_path: str, signature: tuple,
                 raw: bytes):
        self.path_identity = path_identity
        self.resolved_path = resolved_path
        self.signature = signature
        self.lines = tuple(raw.decode("utf-8", errors="replace").splitlines())
        records = []
        completion_latest = {}
        completion_errors = {}
        completion_all = {}
        completion_children = {}
        handover_latest = {}
        handover_by_parent = {}
        handover_by_binding = {}
        headless_owner_rows = []

        for order, line in enumerate(self.lines):
            fields = tuple(line.split("\t"))
            if len(fields) != 6:
                continue
            completion_items = _completion_metadata(fields[5])
            handover_items = _handover_metadata(fields[5])
            record = RegistryRecord(order, line, fields, completion_items, handover_items)
            records.append(record)

            completion_meta = dict(completion_items)
            attempt = completion_meta.get("attempt_id")
            if attempt:
                completion_all.setdefault(attempt, []).append(record)
            parent_attempt = completion_meta.get("parent_attempt_id")
            if parent_attempt:
                completion_children.setdefault(parent_attempt, []).append(record)
            if attempt:
                if completion_meta.get("attempt_schema_version") != "2":
                    completion_errors.setdefault(attempt, "attempt-row-schema-invalid")
                else:
                    completion_latest[attempt] = record

            handover_meta = dict(handover_items)
            handover_attempt = handover_meta.get("attempt_id")
            if handover_attempt:
                handover_latest[handover_attempt] = (fields[1], record)

        self.records = tuple(records)
        self._completion_latest = completion_latest
        self._completion_errors = completion_errors
        self._completion_all = {key: tuple(value) for key, value in completion_all.items()}
        self._completion_children = {
            key: tuple(value) for key, value in completion_children.items()
        }
        self._handover_latest = handover_latest

        for attempt, (status, record) in handover_latest.items():
            metadata = record.handover_metadata()
            parent = metadata.get("parent_sid")
            if parent:
                handover_by_parent.setdefault(parent, {})[attempt] = (status, record)
            route = metadata.get("owner_route_id") or metadata.get("route_id") or ""
            digest = metadata.get("owner_route_hash") or metadata.get("route_hash") or ""
            node = metadata.get("route_node") or ""
            if route and digest and node:
                handover_by_binding.setdefault((route, digest, node), {})[attempt] = (status, record)
            if (metadata.get("dispatch_depth") == "1" and metadata.get("worker_type") == "owner"
                    and metadata.get("harness") == "opencode"
                    and metadata.get("transport") == "headless"
                    and metadata.get("registered_worker") == "1"
                    and metadata.get("execution_surface") == "registered-headless"):
                headless_owner_rows.append((status, record))
        self._handover_by_parent = handover_by_parent
        self._handover_by_binding = handover_by_binding
        self._headless_owner_rows = tuple(headless_owner_rows)

    def completion_attempt(self, attempt_id: str):
        """Return (current valid row, error reason); malformed history fails closed."""
        return self._completion_latest.get(attempt_id), self._completion_errors.get(attempt_id)

    def completion_attempt_records(self, attempt_id: str) -> tuple[RegistryRecord, ...]:
        return self._completion_all.get(attempt_id, ())

    def completion_children(self, parent_attempt_id: str) -> tuple[RegistryRecord, ...]:
        return self._completion_children.get(parent_attempt_id, ())

    def handover_latest(self) -> dict[str, tuple[str, dict[str, str]]]:
        """Return an isolated compatibility copy of latest_rows()."""
        return {attempt: (status, record.handover_metadata())
                for attempt, (status, record) in self._handover_latest.items()}

    def handover_attempt(self, attempt_id: str):
        """Return one isolated current handover row, if present."""
        current = self._handover_latest.get(attempt_id)
        if current is None:
            return None
        status, record = current
        return status, record.handover_metadata()

    def handover_candidates(self, parent_sid: str, binding_keys=()):
        """Return latest rows registered to this session or its current bound lineage."""
        selected = dict(self._handover_by_parent.get(parent_sid, {}))
        for key in binding_keys:
            if not isinstance(key, tuple) or len(key) != 3:
                continue
            selected.update(self._handover_by_binding.get(key, {}))
        return tuple((status, record) for _attempt, (status, record) in selected.items())

    def headless_owner_candidates(self):
        """Narrow OpenCode rows whose exact native sidecar must still be observed."""
        return self._headless_owner_rows


def _stable_read(path: Path, path_identity: str):
    last_error = None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(_RETRIES):
        fd = None
        try:
            resolved_before = str(path.resolve(strict=True))
            path_before = path.stat()
            fd = os.open(path, flags)
            fd_before = os.fstat(fd)
            before_signature = _stat_signature(path_before)
            if _stat_signature(fd_before) != before_signature:
                raise OSError("registry changed before read")

            chunks = []
            size = 0
            while True:
                chunk = os.read(fd, _CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)

            fd_after = os.fstat(fd)
            path_after = path.stat()
            resolved_after = str(path.resolve(strict=True))
            after_signature = _stat_signature(path_after)
            if (resolved_before != resolved_after
                    or before_signature != _stat_signature(fd_after)
                    or before_signature != after_signature
                    or size != before_signature[-1]):
                raise OSError("registry changed during read")
            return resolved_after, after_signature, b"".join(chunks)
        except OSError as exc:
            last_error = exc
        finally:
            if fd is not None:
                os.close(fd)
    raise OSError(getattr(last_error, "errno", None),
                  f"could not read a stable registry generation: {path_identity}") from last_error


def registry_view(jobs: Path) -> RegistryView:
    """Return the current stable view, rebuilding once when the source generation changes.

    A failed or unstable observation invalidates the cached generation; an older
    successful view is never returned as authority for a missing/unreadable path.
    """
    path = Path(jobs)
    identity = str(path.absolute())
    with _LOCK:
        try:
            resolved = str(path.resolve(strict=True))
            signature = _stat_signature(path.stat())
        except OSError:
            if _CACHE.pop(identity, None) is not None:
                _METRICS["invalidations"] += 1
            raise

        cached = _CACHE.get(identity)
        if (cached is not None and cached.resolved_path == resolved
                and cached.signature == signature):
            _METRICS["cache_hits"] += 1
            return cached

        _CACHE.pop(identity, None)
        _METRICS["invalidations"] += 1
        resolved, signature, raw = _stable_read(path, identity)
        _METRICS["content_reads"] += 1
        _METRICS["bytes_read"] += len(raw)
        view = RegistryView(identity, resolved, signature, raw)
        _METRICS["full_metadata_parses"] += 1
        _METRICS["metadata_rows_parsed"] += len(view.records)
        _CACHE[identity] = view
        return view


def registry_lines(jobs: Path) -> tuple[str, ...]:
    """Compatibility facade retaining the existing tuple[str, ...]/OSError contract."""
    return registry_view(jobs).lines


def metrics_snapshot() -> dict[str, int]:
    """Return counters used by deterministic regression and workload fixtures."""
    with _LOCK:
        return dict(_METRICS)


def reset_metrics() -> None:
    """Reset diagnostic counters without changing any cached generation."""
    with _LOCK:
        for key in _METRICS:
            _METRICS[key] = 0


def clear_cache() -> None:
    """Test helper: discard process-local views."""
    with _LOCK:
        _CACHE.clear()
