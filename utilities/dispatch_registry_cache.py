#!/usr/bin/env python3
"""Shared, generation-checked views of the dispatch registry.

The immutable view may be reused while the registry generation is unchanged.
Consumers must still observe their other inputs on each pass and recheck
authority under the existing lock before a state transition.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
from pathlib import Path
import threading
import time


_RETRIES = 3
_CHUNK_SIZE = 1024 * 1024
_APPEND_OBSERVATION_GAP_SECONDS = 30.0
_CACHE_MAX_PATHS = 128
_CACHE_MAX_BYTES = 64 * 1024 * 1024
_MAX_SEGMENTS = 256
_COMPACT_SEGMENTS = 128
_LOCK = threading.RLock()
_CACHE: OrderedDict[str, "RegistryView"] = OrderedDict()
_CACHE_BYTES = 0
_METRICS = {
    "cache_hits": 0,
    "content_reads": 0,
    "bytes_read": 0,
    "delta_reads": 0,
    "delta_bytes_read": 0,
    "full_metadata_parses": 0,
    "metadata_rows_parsed": 0,
    "invalidations": 0,
    "cache_evictions": 0,
    "segment_compactions": 0,
}


def _stat_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


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


class _RegistrySegment:
    """One append batch and the indexes for rows introduced by that batch."""

    __slots__ = ("lines", "records", "malformed_rows", "completion_latest",
                 "completion_errors", "completion_all", "completion_children",
                 "completion_by_parent_sid",
                 "handover_latest", "handover_by_parent", "handover_by_binding",
                 "headless_owner_rows")

    def __init__(self, lines: tuple[str, ...], order_base: int):
        self.lines = lines
        records = []
        completion_latest = {}
        completion_errors = {}
        completion_all = {}
        completion_children = {}
        completion_by_parent_sid = {}
        handover_latest = {}
        self.malformed_rows = 0
        for relative_order, line in enumerate(lines):
            fields = tuple(line.split("\t"))
            if len(fields) != 6:
                self.malformed_rows += 1
                continue
            completion_items = _completion_metadata(fields[5])
            handover_items = _handover_metadata(fields[5])
            record = RegistryRecord(order_base + relative_order, line, fields,
                                     completion_items, handover_items)
            records.append(record)
            completion_meta = dict(completion_items)
            attempt = completion_meta.get("attempt_id")
            if attempt:
                completion_all.setdefault(attempt, []).append(record)
                if completion_meta.get("attempt_schema_version") != "2":
                    completion_errors.setdefault(attempt, "attempt-row-schema-invalid")
                else:
                    completion_latest[attempt] = record
            parent_attempt = completion_meta.get("parent_attempt_id")
            if parent_attempt:
                completion_children.setdefault(parent_attempt, []).append(record)
            parent_sid = completion_meta.get("parent_sid")
            if parent_sid:
                completion_by_parent_sid.setdefault(parent_sid, []).append(record)
            handover_meta = dict(handover_items)
            handover_attempt = handover_meta.get("attempt_id")
            if handover_attempt:
                handover_latest[handover_attempt] = (fields[1], record)
        self.records = tuple(records)
        self.completion_latest = completion_latest
        self.completion_errors = completion_errors
        self.completion_all = {key: tuple(value) for key, value in completion_all.items()}
        self.completion_children = {key: tuple(value) for key, value in completion_children.items()}
        self.completion_by_parent_sid = {
            key: tuple(value) for key, value in completion_by_parent_sid.items()
        }
        self.handover_latest = handover_latest
        self._index_handover()

    def _index_handover(self):
        self.handover_by_parent = {}
        self.handover_by_binding = {}
        headless = []
        for attempt, (status, record) in self.handover_latest.items():
            metadata = record.handover_metadata()
            parent = metadata.get("parent_sid")
            if parent:
                self.handover_by_parent.setdefault(parent, {})[attempt] = (status, record)
            route = metadata.get("owner_route_id") or metadata.get("route_id") or ""
            digest = metadata.get("owner_route_hash") or metadata.get("route_hash") or ""
            node = metadata.get("route_node") or ""
            if route and digest and node:
                self.handover_by_binding.setdefault((route, digest, node), {})[attempt] = (status, record)
            if (metadata.get("dispatch_depth") == "1" and metadata.get("worker_type") == "owner"
                    and metadata.get("harness") == "opencode"
                    and metadata.get("transport") == "headless"
                    and metadata.get("registered_worker") == "1"
                    and metadata.get("execution_surface") == "registered-headless"):
                headless.append((status, record))
        self.headless_owner_rows = tuple(headless)

    @classmethod
    def compact(cls, segments: tuple["_RegistrySegment", ...]):
        """Merge index deltas without reparsing their registry rows."""
        result = cls.__new__(cls)
        result.lines = tuple(line for segment in segments for line in segment.lines)
        result.records = tuple(record for segment in segments for record in segment.records)
        result.malformed_rows = sum(segment.malformed_rows for segment in segments)
        result.completion_latest = {}
        result.completion_errors = {}
        completion_all = {}
        completion_children = {}
        completion_by_parent_sid = {}
        result.handover_latest = {}
        for segment in segments:
            result.completion_latest.update(segment.completion_latest)
            for attempt, error in segment.completion_errors.items():
                result.completion_errors.setdefault(attempt, error)
            for target, source in ((completion_all, segment.completion_all),
                                   (completion_children, segment.completion_children),
                                   (completion_by_parent_sid, segment.completion_by_parent_sid)):
                for key, rows in source.items():
                    target.setdefault(key, []).extend(rows)
            result.handover_latest.update(segment.handover_latest)
        result.completion_all = {key: tuple(value) for key, value in completion_all.items()}
        result.completion_children = {key: tuple(value) for key, value in completion_children.items()}
        result.completion_by_parent_sid = {
            key: tuple(value) for key, value in completion_by_parent_sid.items()
        }
        result._index_handover()
        return result


class RegistryView:
    """Immutable segmented registry view with incremental append indexes."""

    __slots__ = ("path_identity", "resolved_path", "signature", "_segments", "_tail",
                 "line_count", "record_count", "malformed_rows", "has_partial_tail",
                 "last_observed_at")

    def __init__(self, path_identity: str, resolved_path: str, signature: tuple,
                 raw: bytes, *, previous: "RegistryView | None" = None):
        self.path_identity = path_identity
        self.resolved_path = resolved_path
        self.signature = signature
        self.last_observed_at = time.monotonic()
        if previous is None:
            boundary = raw.rfind(b"\n") + 1
            complete = raw[:boundary].decode("utf-8", errors="replace").splitlines()
            tail = raw[boundary:]
            segment = _RegistrySegment(tuple(complete), 0)
            self._segments = (segment,) if complete else ()
            self._tail = tail
            self.line_count = len(complete) + (1 if tail else 0)
        else:
            # The caller supplied only bytes appended to a continuously observed,
            # trusted append-only registry. Rejoin its pending UTF-8 byte tail.
            combined = previous._tail + raw
            pieces = combined.split(b"\n")
            complete_bytes, tail = pieces[:-1], pieces[-1]
            complete = tuple(piece.decode("utf-8", errors="replace") for piece in complete_bytes)
            old_partial = bool(previous._tail)
            order_base = previous.line_count - 1 if old_partial and complete else previous.line_count
            new_segments = list(previous._segments)
            appended_segment = None
            if complete:
                appended_segment = _RegistrySegment(complete, order_base)
                new_segments.append(appended_segment)
            if len(new_segments) > _MAX_SEGMENTS:
                compacted = _RegistrySegment.compact(tuple(new_segments[-_COMPACT_SEGMENTS:]))
                new_segments = new_segments[:-_COMPACT_SEGMENTS] + [compacted]
                _METRICS["segment_compactions"] += 1
            self._segments = tuple(new_segments)
            self._tail = tail
            self.line_count = (previous.line_count + len(complete)
                               - (1 if old_partial and complete else 0))
        if previous is None:
            self.record_count = sum(len(segment.records) for segment in self._segments)
            self.malformed_rows = sum(segment.malformed_rows for segment in self._segments)
            self.has_partial_tail = bool(self._tail)
        else:
            added_records = len(appended_segment.records) if appended_segment is not None else 0
            added_malformed = appended_segment.malformed_rows if appended_segment is not None else 0
            self.record_count = previous.record_count + added_records
            self.malformed_rows = previous.malformed_rows + added_malformed
            self.has_partial_tail = bool(self._tail)

    @property
    def lines(self) -> tuple[str, ...]:
        result = tuple(line for segment in self._segments for line in segment.lines)
        if self._tail:
            result += (self._tail.decode("utf-8", errors="replace"),)
        return result

    @property
    def records(self) -> tuple[RegistryRecord, ...]:
        return tuple(record for segment in self._segments for record in segment.records)

    def completion_attempt(self, attempt_id: str):
        latest, error = None, None
        for segment in self._segments:
            if attempt_id in segment.completion_latest:
                latest = segment.completion_latest[attempt_id]
            error = error or segment.completion_errors.get(attempt_id)
        return latest, error

    def completion_attempt_records(self, attempt_id: str) -> tuple[RegistryRecord, ...]:
        return tuple(record for segment in self._segments
                     for record in segment.completion_all.get(attempt_id, ()))

    def completion_children(self, parent_attempt_id: str) -> tuple[RegistryRecord, ...]:
        return tuple(record for segment in self._segments
                     for record in segment.completion_children.get(parent_attempt_id, ()))

    def completion_parent_records(self, parent_sid: str) -> tuple[RegistryRecord, ...]:
        return tuple(record for segment in self._segments
                     for record in segment.completion_by_parent_sid.get(parent_sid, ()))

    def handover_latest(self) -> dict[str, tuple[str, dict[str, str]]]:
        latest = {}
        for segment in self._segments:
            latest.update(segment.handover_latest)
        return {attempt: (status, record.handover_metadata())
                for attempt, (status, record) in latest.items()}

    def handover_attempt(self, attempt_id: str):
        latest = None
        for segment in self._segments:
            latest = segment.handover_latest.get(attempt_id, latest)
        if latest is None:
            return None
        status, record = latest
        return status, record.handover_metadata()

    def handover_candidates(self, parent_sid: str, binding_keys=()):
        selected = {}
        for segment in self._segments:
            matches = dict(segment.handover_by_parent.get(parent_sid, {}))
            for key in binding_keys:
                if isinstance(key, tuple) and len(key) == 3:
                    matches.update(segment.handover_by_binding.get(key, {}))
            for attempt, candidate in matches.items():
                prior = selected.get(attempt)
                if prior is None or candidate[1].order > prior[1].order:
                    selected[attempt] = candidate
        current = []
        keys = {key for key in binding_keys if isinstance(key, tuple) and len(key) == 3}
        for attempt in selected:
            latest = self.handover_attempt(attempt)
            if latest is None:
                continue
            status, metadata = latest
            parent = metadata.get("parent_sid") == parent_sid
            route = metadata.get("owner_route_id") or metadata.get("route_id") or ""
            digest = metadata.get("owner_route_hash") or metadata.get("route_hash") or ""
            node = metadata.get("route_node") or ""
            if parent or (route, digest, node) in keys:
                record = next((segment.handover_latest[attempt][1]
                               for segment in reversed(self._segments)
                               if attempt in segment.handover_latest), None)
                if record is not None:
                    current.append((status, record))
        return tuple(current)

    def headless_owner_candidates(self):
        candidate_attempts = set()
        for segment in self._segments:
            candidate_attempts.update(attempt for _status, record in segment.headless_owner_rows
                                      for attempt in (record.handover_metadata().get("attempt_id"),)
                                      if attempt)
        result = []
        for attempt in candidate_attempts:
            latest = self.handover_attempt(attempt)
            if latest is None:
                continue
            status, metadata = latest
            if (metadata.get("dispatch_depth") == "1" and metadata.get("worker_type") == "owner"
                    and metadata.get("harness") == "opencode"
                    and metadata.get("transport") == "headless"
                    and metadata.get("registered_worker") == "1"
                    and metadata.get("execution_surface") == "registered-headless"):
                record = next((segment.handover_latest[attempt][1]
                               for segment in reversed(self._segments)
                               if attempt in segment.handover_latest), None)
                if record is None:
                    continue
                result.append((status, record))
        return tuple(result)


def _stable_read(path: Path, path_identity: str, *, start: int = 0):
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
            if start < 0 or start > before_signature[2]:
                raise OSError("registry append boundary changed")
            if start:
                os.lseek(fd, start, os.SEEK_SET)

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
                    or size != before_signature[2] - start):
                raise OSError("registry changed during read")
            return resolved_after, after_signature, b"".join(chunks)
        except OSError as exc:
            last_error = exc
        finally:
            if fd is not None:
                os.close(fd)
    raise OSError(getattr(last_error, "errno", None),
                  f"could not read a stable registry generation: {path_identity}") from last_error


def registry_view(jobs: Path, *, append_only: bool = False) -> RegistryView:
    """Return the current stable view, rebuilding once when the source generation changes.

    A failed or unstable observation invalidates the cached generation; an older
    successful view is never returned as authority for a missing/unreadable path.
    ``append_only`` is an assertion by a caller executing the supported dispatch
    writer protocol: same-inode growth is used as a suffix only on that path.
    Arbitrary files and direct rewrites must leave it false; stat size alone is
    not evidence that an arbitrary writer preserved the old prefix.
    """
    path = Path(jobs)
    identity = str(path.absolute())
    global _CACHE_BYTES
    with _LOCK:
        try:
            resolved = str(path.resolve(strict=True))
            signature = _stat_signature(path.stat())
        except OSError:
            removed = _CACHE.pop(identity, None)
            if removed is not None:
                _CACHE_BYTES -= removed.signature[2]
                _METRICS["invalidations"] += 1
            raise

        cached = _CACHE.get(identity)
        if (cached is not None and cached.resolved_path == resolved
                and cached.signature == signature):
            _METRICS["cache_hits"] += 1
            _CACHE.move_to_end(identity)
            cached.last_observed_at = time.monotonic()
            return cached

        can_append = bool(
            append_only and cached is not None and cached.resolved_path == resolved
            and cached.signature[:2] == signature[:2]
            and signature[2] > cached.signature[2]
            and time.monotonic() - cached.last_observed_at <= _APPEND_OBSERVATION_GAP_SECONDS
        )
        removed = _CACHE.pop(identity, None)
        if removed is not None:
            _CACHE_BYTES -= removed.signature[2]
        _METRICS["invalidations"] += 1
        previous = None
        if can_append:
            try:
                observed_resolved, observed_signature, raw = _stable_read(
                    path, identity, start=cached.signature[2]
                )
                if (observed_resolved == cached.resolved_path
                        and observed_signature[:2] == cached.signature[:2]
                        and observed_signature[2] > cached.signature[2]):
                    resolved, signature = observed_resolved, observed_signature
                    previous = cached
                    _METRICS["delta_reads"] += 1
                    _METRICS["delta_bytes_read"] += len(raw)
                else:
                    resolved, signature, raw = _stable_read(path, identity)
            except OSError:
                raise
        else:
            resolved, signature, raw = _stable_read(path, identity)
        _METRICS["content_reads"] += 1
        _METRICS["bytes_read"] += len(raw)
        view = RegistryView(identity, resolved, signature, raw, previous=previous)
        if previous is None:
            _METRICS["full_metadata_parses"] += 1
            _METRICS["metadata_rows_parsed"] += view.record_count
        else:
            _METRICS["metadata_rows_parsed"] += view.record_count - previous.record_count
        if signature[2] <= _CACHE_MAX_BYTES:
            _CACHE[identity] = view
            _CACHE_BYTES += signature[2]
        while (len(_CACHE) > _CACHE_MAX_PATHS or _CACHE_BYTES > _CACHE_MAX_BYTES):
            _evicted_identity, evicted = _CACHE.popitem(last=False)
            _CACHE_BYTES -= evicted.signature[2]
            _METRICS["cache_evictions"] += 1
        return view


def registry_lines(jobs: Path) -> tuple[str, ...]:
    """Compatibility facade retaining the existing tuple[str, ...]/OSError contract."""
    return registry_view(jobs).lines


def metrics_snapshot() -> dict[str, int]:
    """Return counters used by deterministic regression and workload fixtures."""
    with _LOCK:
        return {**_METRICS, "cache_entries": len(_CACHE), "cache_bytes": _CACHE_BYTES}


def reset_metrics() -> None:
    """Reset diagnostic counters without changing any cached generation."""
    with _LOCK:
        for key in _METRICS:
            _METRICS[key] = 0


def clear_cache() -> None:
    """Test helper: discard process-local views."""
    global _CACHE_BYTES
    with _LOCK:
        _CACHE.clear()
        _CACHE_BYTES = 0
