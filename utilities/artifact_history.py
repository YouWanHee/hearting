#!/usr/bin/env python3
"""The one history recorder: one append-only JSON line per change.

Every change to producer-owned metadata (campaign/cycle metadata, the project
vocabulary, workflow groups, cycle/campaign lifecycle, and later flow records and
artifact files) records its meaning through `make_event` + `publish_events_locked`
(or `publish_events` outside the admission lock); there is no second recorder.  The same lines are the change signal Cairn watches, so no other
notification device exists.

One event is one file `.runtime/artifact-producer/v1/history/YYYY-MM/<event_id>.jsonl`
holding exactly one LF-terminated JSON line.  A published file is never
modified, truncated, renamed, or deleted; a new UTC month starts a new
directory.  File names and directory order do not define a global sequence.
After a publish created new files, `history/LATEST.json` names the last one and the
published count; it is a convenience signal, the event files stay canonical.
Nothing here is a gate, an input, or an obligation for an agent or a user.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_admission as admission  # noqa: E402
import artifact_producer as producer  # noqa: E402

CONTRACT = "artifact-history/v1"
HISTORY_REL = ".runtime/artifact-producer/v1/history"
LATEST_REL = HISTORY_REL + "/LATEST.json"
STAGING_REL = ".runtime/artifact-producer/v1/artifact-meta-transactions"
VALUE_LIMIT = 512  # canonical JSON bytes; longer values are recorded as digest + size

KEYS = ("schema_version", "contract", "event_id", "transaction_id", "at", "actor", "kind", "target",
        "operation", "field", "before", "after", "reason")
KINDS = frozenset(("meta", "group", "flow", "artifact", "lifecycle"))
TARGET_TYPES = frozenset(("campaign", "cycle", "project", "group", "flow", "artifact"))
OPERATIONS = frozenset(("add", "update", "move", "delete"))
ACTORS = frozenset(("rule", "model", "human", "agent"))
ACTOR_KEYS = ("by", "session", "harness", "route", "attempt")
# Environment markers of an agent session: a command run from one records `by=agent`.
AGENT_ENV_MARKERS = ("AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_CURRENT_HARNESS", "AGENT_ROUTE_ID",
                     "AGENT_ROUTE_FILE", "AGENT_DISPATCH_PARENT_SESSION_ID")
LIFECYCLE_TARGETS = frozenset(("cycle", "campaign"))
LIFECYCLE_FIELDS = frozenset(("state", "campaign", "parent", "disposition", "path"))
CLOSE_STATES = frozenset(("completed", "abandoned"))
CLOSE_KEYS = ("state", "manifest_digest", "revision_id", "files", "excluded")
EVENT_ID = re.compile(r"hevt_[0-9a-f]{32}\Z")
TXN_ID = re.compile(r"htxn_[0-9a-f]{32}\Z")
RFC3339 = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class HistoryError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


class HistoryPublishError(HistoryError):
    """A publish did not complete (lock busy, conflict, unreadable or unwritable history, invalid event).

    A caller that must not lose the line keeps it and publishes it again later."""


def new_event_id() -> str:
    return "hevt_" + secrets.token_hex(16)


def new_transaction_id() -> str:
    return "htxn_" + secrets.token_hex(16)


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_of(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def value_ref(value: Any) -> Dict[str, Any]:
    """A JSON value as it is recorded: the value itself when small, else its digest and size."""
    raw = canonical(value)
    if len(raw) <= VALUE_LIMIT:
        return {"value": value}
    return {"digest": digest_of(raw), "bytes": len(raw)}


def file_ref(raw: Optional[bytes]) -> Dict[str, Any]:
    """File bytes as recorded: always a digest and size, whatever the length; absent is null."""
    if raw is None:
        return {"value": None}
    return {"digest": digest_of(raw), "bytes": len(raw)}


def _token(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128 and all(32 < ord(c) < 127 for c in value)


def make_actor(by: str, *, session: Optional[str] = None, harness: Optional[str] = None,
               route: Optional[str] = None, attempt: Optional[str] = None) -> Dict[str, Optional[str]]:
    """The closed actor object; everything but `by` is optional and an absent one is null."""
    actor = {"by": by, "session": session, "harness": harness, "route": route, "attempt": attempt}
    _check_actor(actor)
    return actor


def actor_from_env(default_by: str = "human", env: Optional[Mapping[str, str]] = None) -> Dict[str, Optional[str]]:
    """Who is acting, from the process environment.

    An agent session (any `AGENT_ENV_MARKERS` variable set) is `by=agent` with the markers it
    carries; otherwise `default_by` (a person at a shell: `human`).  A runtime that observed a change
    without knowing who made it passes `by=rule` itself (`make_actor("rule", session=…, route=…)`).
    A marker that is not a plain token is dropped, never an error."""
    if default_by not in ACTORS:
        raise HistoryError("event-invalid", "actor")
    env = os.environ if env is None else env

    def marker(name: str) -> Optional[str]:
        value = env.get(name)
        return value if _token(value) else None

    if not any(env.get(name) for name in AGENT_ENV_MARKERS):
        return make_actor(default_by)
    attempt = marker("AGENT_DISPATCH_ATTEMPT_ID")
    return make_actor("agent", session=attempt, harness=marker("AGENT_DISPATCH_CURRENT_HARNESS"),
                      route=marker("AGENT_ROUTE_ID"), attempt=attempt)


def _check_actor(actor: Any) -> None:
    if (not isinstance(actor, dict) or list(actor) != list(ACTOR_KEYS) or actor["by"] not in ACTORS
            or not all(actor[key] is None or _token(actor[key]) for key in ACTOR_KEYS[1:])):
        raise HistoryError("event-invalid", "actor")


def _check_ref(ref: Any, name: str) -> None:
    if not isinstance(ref, dict):
        raise HistoryError("event-invalid", name)
    if set(ref) == {"value"}:
        if len(canonical(ref["value"])) > VALUE_LIMIT:
            raise HistoryError("event-invalid", f"{name}-value-too-long")
        return
    if (set(ref) == {"digest", "bytes"} and isinstance(ref["digest"], str) and DIGEST.fullmatch(ref["digest"])
            and isinstance(ref["bytes"], int) and not isinstance(ref["bytes"], bool) and ref["bytes"] >= 0):
        return
    raise HistoryError("event-invalid", name)


def _safe_path(value: Any) -> str:
    if (not isinstance(value, str) or not value or value.startswith("/") or "\\" in value
            or any(part in ("", ".", "..") for part in value.split("/"))
            or unicodedata.normalize("NFC", value) != value
            or any(ord(char) <= 31 or ord(char) == 127 for char in value)):
        raise HistoryError("event-invalid", "path")
    return value


def validate_event(event: Any) -> Dict[str, Any]:
    """The closed event shape; raises HistoryError('event-invalid', field) otherwise."""
    if not isinstance(event, dict) or list(event) != list(KEYS):
        raise HistoryError("event-invalid", "keys")
    if event["schema_version"] != 1 or isinstance(event["schema_version"], bool) or event["contract"] != CONTRACT:
        raise HistoryError("event-invalid", "contract")
    if not isinstance(event["event_id"], str) or not EVENT_ID.fullmatch(event["event_id"]):
        raise HistoryError("event-invalid", "event_id")
    if not isinstance(event["transaction_id"], str) or not TXN_ID.fullmatch(event["transaction_id"]):
        raise HistoryError("event-invalid", "transaction_id")
    if not isinstance(event["at"], str) or not RFC3339.fullmatch(event["at"]):
        raise HistoryError("event-invalid", "at")
    _check_actor(event["actor"])
    if event["kind"] not in KINDS:
        raise HistoryError("event-invalid", "kind")
    target = event["target"]
    if (not isinstance(target, dict) or list(target) != ["type", "id", "path"]
            or target["type"] not in TARGET_TYPES or not isinstance(target["id"], str) or not target["id"]):
        raise HistoryError("event-invalid", "target")
    _safe_path(target["path"])
    if event["operation"] not in OPERATIONS:
        raise HistoryError("event-invalid", "operation")
    if not isinstance(event["field"], str) or not event["field"] or len(event["field"]) > 512:
        raise HistoryError("event-invalid", "field")
    _check_ref(event["before"], "before")
    _check_ref(event["after"], "after")
    if not isinstance(event["reason"], str) or not event["reason"] or len(event["reason"]) > 400:
        raise HistoryError("event-invalid", "reason")
    if event["kind"] == "lifecycle":
        _check_lifecycle(event)
    return event


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_lifecycle(event: Mapping[str, Any]) -> None:
    """`lifecycle`: a cycle or campaign changing state, membership, parent, mark, or location.

    A cycle close is `update` of `state` with the closed-manifest summary as the after value; a delete is
    `delete` of `state` with the removed manifest digest and path as the before value.  The other fields
    only need the common shape."""
    target, field, operation = event["target"], event["field"], event["operation"]
    if target["type"] not in LIFECYCLE_TARGETS:
        raise HistoryError("event-invalid", "lifecycle-target")
    if field not in LIFECYCLE_FIELDS:
        raise HistoryError("event-invalid", "lifecycle-field")
    if operation == "delete" and field != "state":
        raise HistoryError("event-invalid", "lifecycle-delete")
    if field != "state":
        return
    if operation == "delete":
        before = event["before"].get("value")
        if (set(event["before"]) != {"value"} or not isinstance(before, dict)
                or set(before) != {"manifest_digest", "path"}
                or not (before["manifest_digest"] is None or (isinstance(before["manifest_digest"], str)
                                                              and DIGEST.fullmatch(before["manifest_digest"])))):
            raise HistoryError("event-invalid", "lifecycle-delete-before")
        _safe_path(before["path"])
        if event["after"] != {"value": None}:
            raise HistoryError("event-invalid", "lifecycle-delete-after")
    elif operation != "update":
        raise HistoryError("event-invalid", "lifecycle-state-operation")
    elif target["type"] == "cycle":
        after = event["after"].get("value")
        if (set(event["after"]) != {"value"} or not isinstance(after, dict) or set(after) != set(CLOSE_KEYS)
                or after["state"] not in CLOSE_STATES
                or not (isinstance(after["manifest_digest"], str) and DIGEST.fullmatch(after["manifest_digest"]))
                or not _token(after["revision_id"]) or not _count(after["files"]) or not _count(after["excluded"])):
            raise HistoryError("event-invalid", "lifecycle-close")
    else:
        for side in ("before", "after"):
            value = event[side].get("value")
            if set(event[side]) != {"value"} or not _token(value):
                raise HistoryError("event-invalid", f"lifecycle-campaign-state-{side}")


def make_event(*, kind: str, target_type: str, target_id: str, target_path: str, operation: str, field: str,
               before: Mapping[str, Any], after: Mapping[str, Any], reason: str, transaction_id: str,
               actor: Optional[Mapping[str, Any]] = None, actor_by: Optional[str] = None,
               session: Optional[str] = None, harness: Optional[str] = None, route: Optional[str] = None,
               attempt: Optional[str] = None, now: Optional[float] = None,
               event_id: Optional[str] = None) -> Dict[str, Any]:
    """One validated event.  `before` / `after` come from `value_ref` or `file_ref`.

    The actor is either the `actor` object (`actor_from_env()` / `make_actor(...)`) or `actor_by` with the
    optional `session`/`harness`/`route`/`attempt`; never both."""
    if actor is not None:
        if actor_by is not None or any(v is not None for v in (session, harness, route, attempt)):
            raise HistoryError("event-invalid", "actor")
        if not isinstance(actor, Mapping) or not set(actor) <= set(ACTOR_KEYS) or "by" not in actor:
            raise HistoryError("event-invalid", "actor")
        who = make_actor(actor["by"], **{key: actor.get(key) for key in ACTOR_KEYS[1:]})
    else:
        who = make_actor(actor_by, session=session, harness=harness, route=route, attempt=attempt)
    event = {
        "schema_version": 1, "contract": CONTRACT, "event_id": event_id or new_event_id(),
        "transaction_id": transaction_id, "at": producer._rfc3339(now),
        "actor": who, "kind": kind,
        "target": {"type": target_type, "id": target_id, "path": target_path},
        "operation": operation, "field": field, "before": dict(before), "after": dict(after),
        "reason": reason,
    }
    return validate_event(event)


def event_bytes(event: Mapping[str, Any]) -> bytes:
    validate_event(dict(event))
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def event_path(root: Path, event: Mapping[str, Any]) -> Path:
    return Path(root) / HISTORY_REL / event["at"][:7] / f"{event['event_id']}.jsonl"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def publish_events_locked(root: Path, events: Sequence[Mapping[str, Any]]) -> List[str]:
    """Publish every event once; the CALLER HOLDS the producer admission lock.

    A file is prepared and fsynced outside the history directory, then made visible with
    one no-replace link, so a reader never sees a partial line.  Publishing an event again
    with identical bytes is a no-op; the same `event_id` with other bytes is a conflict and
    nothing is overwritten.  Any failure raises `HistoryPublishError`; lines already made visible
    stay.  After new files were created, `LATEST.json` is updated inside the lock; that update
    failing never fails the publish and the next publish re-syncs it.  Returns the root-relative
    paths of the events now present.
    """
    root = Path(root).resolve()
    created: List[Mapping[str, Any]] = []
    try:
        published = _publish_locked(root, events, created)
    except HistoryPublishError:
        raise
    except HistoryError as exc:
        raise HistoryPublishError(exc.code, exc.detail) from exc
    finally:
        if created:  # also after a failure: the lines already made visible are published
            _write_latest(root, created[-1])
    return published


def publish_events(root: Path, events: Sequence[Mapping[str, Any]], *,
                   lock_timeout: Optional[float] = None) -> List[str]:
    """`publish_events_locked` for a caller that does not hold the admission lock: takes the same lock.

    A caller already inside the lock gets the plain locked publish (no second acquisition)."""
    root = Path(root).resolve()
    if admission.holds_lock(root):
        return publish_events_locked(root, events)
    try:
        fd = admission._acquire_lock(root, admission.LOCK_TIMEOUT_DEFAULT if lock_timeout is None else lock_timeout)
    except (admission.AdmissionBusy, admission.dispatch_lock_order.LockOrderError) as exc:
        raise HistoryPublishError("admission-busy", str(exc)) from exc
    try:
        return publish_events_locked(root, events)
    finally:
        admission._release_lock(root, fd)


def _publish_locked(root: Path, events: Sequence[Mapping[str, Any]], created: List[Mapping[str, Any]]) -> List[str]:
    if not admission.holds_lock(root):
        raise HistoryError("admission-lock-required")
    prepared = [(event, event_bytes(event)) for event in events]
    if len({event["event_id"] for event, _raw in prepared}) != len(prepared):
        raise HistoryError("event-duplicate")
    staging = root / STAGING_REL
    published: List[str] = []
    for event, raw in prepared:
        final = event_path(root, event)
        try:
            existing = final.read_bytes()
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise HistoryError("history-unreadable", str(exc)) from exc
        if existing is not None:
            if existing != raw:
                raise HistoryError("event-conflict", event["event_id"])
            published.append(final.relative_to(root).as_posix())
            continue
        try:
            staging.mkdir(parents=True, exist_ok=True)
            final.parent.mkdir(parents=True, exist_ok=True)
            tmp = staging / f".event-{os.getpid()}-{secrets.token_hex(4)}"
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(str(tmp), str(final))
                except FileExistsError:
                    if final.read_bytes() != raw:
                        raise HistoryError("event-conflict", event["event_id"])
                else:
                    created.append(event)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            _fsync_dir(final.parent)
        except OSError as exc:
            raise HistoryError("history-write-failed", str(exc)) from exc
        published.append(final.relative_to(root).as_posix())
    return published


def _write_latest(root: Path, last: Mapping[str, Any]) -> None:
    """`history/LATEST.json` = the last published event, its time, and the number of event files.

    The count comes from the files themselves, so one missed update is healed by the next.  Failure is
    swallowed: the event files are canonical and Cairn also rescans on a timer."""
    base = root / HISTORY_REL
    try:
        count = sum(1 for month in base.iterdir() if month.is_dir() for _ in month.glob("hevt_*.jsonl"))
        payload = {"event_id": last["event_id"], "at": last["at"], "count": count}
        producer._write_atomic(root / LATEST_REL, json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
    except OSError:
        return


def iter_events(root: Path) -> Iterator[Dict[str, Any]]:
    """Every readable event, oldest month first; a malformed file is skipped, never repaired."""
    base = Path(root) / HISTORY_REL
    if not base.is_dir():
        return
    for month in sorted(entry for entry in base.iterdir() if entry.is_dir()):
        for path in sorted(month.glob("hevt_*.jsonl")):
            try:
                lines = path.read_bytes().decode("utf-8").splitlines()
                if len(lines) != 1:
                    continue
                yield validate_event(json.loads(lines[0]))
            except (OSError, ValueError, UnicodeError, HistoryError):
                continue
