"""Private, observational state for explicitly marked final-reply questions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import fcntl
import math
import uuid
from contextlib import contextmanager
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request


_MARKER = re.compile(r"^\s*\[(?:답변 필요|결정 필요)\]\s*(.*)$")
_MAX_QUESTION = 160
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:@/+\-=]{1,256}$")
_CACHE_MAX_BYTES = 2 * 1024 * 1024
_DISPLAY_TTL = 180.0
_REQUEST_TTL = 60.0
_SELECTED_DRAFT_MAX = 4096
_SELECTED_RESPONSE_MAX = 256
_SELECTED_STATES = frozenset(("open", "submitted", "received", "applied",
                              "cancelled", "expired", "unknown"))
_SELECTED_ENTRY_KEYS = frozenset(("identity", "draft", "state", "action_id",
                                  "response", "result", "updated_at"))


def state_root() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(xdg) / "hearting" / "human-waits"


def _path(seat_key: str) -> Path:
    digest = hashlib.sha256(str(seat_key).encode("utf-8")).hexdigest()
    return state_root() / (digest + ".json")


def _cache_path() -> Path:
    return state_root() / "projection.json"


def _fleet_cache_path() -> Path:
    return state_root() / "fleet.json"


@contextmanager
def _projection_lock():
    root = state_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = os.lstat(root)
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid():
        raise OSError("answer-wait directory is not private owner state")
    os.chmod(root, 0o700)
    fd = os.open(root / ".projection.lock", os.O_CREAT | os.O_RDWR |
                 getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_cache_file():
    path = _cache_path()
    try:
        parent = os.lstat(path.parent)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) & 0o077):
            return None
        meta = os.lstat(path)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077 or meta.st_size > _CACHE_MAX_BYTES):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get("schema_version") == 3 else None


def _write_cache_value(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _CACHE_MAX_BYTES:
            return False
        _write(_cache_path(), value)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _clean(value: str) -> str:
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    value = " ".join("".join(ch for ch in value if ch >= " " and ch != "\x7f").split())
    return value[:_MAX_QUESTION]


def _safe_native_ref(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    return value if _SAFE.fullmatch(value) else None


def _safe_request_id(value):
    """Validate a JSON-RPC request id without collapsing int and string ids."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _SAFE_REQUEST_ID.fullmatch(value):
        return value
    return None


def _same_request_id(left, right):
    return (type(left) is type(right) and _safe_request_id(left) == left
            and _safe_request_id(right) == right and left == right)


def marked_question(text: str) -> str | None:
    """Return a short marked line outside fenced code, never infer from tone."""
    if not isinstance(text, str):
        return None
    fence = None
    lines = text.splitlines()
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        match = _MARKER.match(line)
        if match:
            return _clean(match.group(1)) or _clean(line.strip())
    return None


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = os.lstat(path.parent)
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid():
        raise OSError("answer-wait directory is not private owner state")
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=".wait-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def signal_value(harness: str, session_id: str, seat_key: str, text: str, *,
                 sequence: int = 0, now: float | None = None) -> dict | None:
    """Build a validated private signal without writing or notifying."""
    question = marked_question(text)
    if (not question or harness not in {"claude", "codex", "opencode"}
            or not _SAFE.fullmatch(str(session_id)) or not seat_key):
        return None
    moment = time.time() if now is None else float(now)
    generation = max(0, int(sequence))
    item_id = hashlib.sha256((harness + "\0" + session_id + "\0" + seat_key +
                              "\0" + question + "\0" + str(generation)).encode()).hexdigest()
    prior = _read(_path(seat_key))
    if prior and prior.get("id") == "S4:" + item_id and prior.get("question") == question:
        moment = float(prior.get("since", moment))
    value = {"schema_version": 1, "id": "S4:" + item_id, "source": "S4",
             "harness": harness, "session_id": session_id, "seat_key": str(seat_key),
             "question": question, "since": moment, "sequence": generation}
    return value


def write_signal(value: dict) -> bool:
    """Write a previously validated signal without running notification work."""
    if not isinstance(value, dict) or not value.get("seat_key") or not value.get("id"):
        return False
    try:
        _write(_path(str(value["seat_key"])), value)
        return True
    except (OSError, ValueError, TypeError):
        return False


def remove_signal_if_current(value: dict) -> bool:
    """Remove this exact observer's signal, preserving any replacement."""
    if not isinstance(value, dict) or not value.get("seat_key") or not value.get("id"):
        return False
    path = _path(str(value["seat_key"]))
    try:
        current = _read(path)
        if current is None or current.get("id") != value.get("id"):
            return False
        path.unlink()
        return True
    except OSError:
        return False


def signal_is_current(value: dict) -> bool:
    """Return whether this exact S4 item is still open for its seat."""
    if not isinstance(value, dict) or not value.get("seat_key") or not value.get("id"):
        return False
    current = _read(_path(str(value["seat_key"])))
    return current is not None and current.get("id") == value.get("id")


def notify_signal(value: dict) -> None:
    """Run registration side effects after the caller has released seat locks."""
    _notify_item(value, boundary="registered")
    _request_producer()


def open_signal(harness: str, session_id: str, seat_key: str, text: str, *,
                sequence: int = 0, now: float | None = None) -> bool:
    """Unconditional convenience writer for callers without a sequence fence."""
    value = signal_value(harness, session_id, seat_key, text, sequence=sequence, now=now)
    if value is None or not write_signal(value):
        return False
    notify_signal(value)
    return True


def close_signal(seat_key: str, *, sequence: int | None = None) -> bool:
    """Close only this exact seat, and never let an older input clear a newer wait."""
    path = _path(seat_key)
    try:
        current = _read(path)
        if current is None:
            return True
        if sequence is not None and int(current.get("sequence", 0)) > int(sequence):
            return False
        path.unlink()
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read(path: Path):
    try:
        meta = os.lstat(path)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("source") != "S4" or not isinstance(value.get("question"), str)
                or not value.get("seat_key") or not value.get("id")):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def read_signals(*, now: float | None = None) -> list[dict]:
    """Read valid private signals. This function never creates or changes state."""
    root = state_root()
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return []
    instant = time.time() if now is None else float(now)
    out = []
    for path in entries:
        if path.suffix != ".json" or path.is_symlink():
            continue
        value = _read(path)
        if value is None or not isinstance(value.get("since"), (int, float)):
            continue
        value["stale"] = instant - float(value["since"]) >= 7 * 86400
        out.append(value)
    return sorted(out, key=lambda item: (item.get("since", 0), item["id"]))


def publish_cache(result: dict) -> bool:
    """Publish private and question-free Fleet views atomically per projection."""
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return False
    by_session: dict[str, list[str]] = {}
    items = []
    for item in result["items"]:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner") or {}
        harness, sid = owner.get("harness"), owner.get("session_id")
        row = {key: item.get(key) for key in ("id", "source", "kind", "since", "route_id",
               "attempt_id", "gate_id", "attribution", "stale")}
        row["question"] = _clean(item.get("question") or "") or None
        row["seat_key"] = owner.get("seat_key")
        row["owner"] = {"harness": harness, "session_id": sid}
        if item.get("source") == "S1":
            for key in ("request_id", "turn_id", "item_id"):
                ref = (_safe_request_id(item.get(key)) if key == "request_id"
                       else _safe_native_ref(item.get(key)))
                if ref is not None:
                    row[key] = ref
        items.append(row)
        if harness and sid and not item.get("stale"):
            key = str(harness) + "\0" + str(sid)
            by_session.setdefault(key, []).append(str(item.get("id") or ""))
    stale_total = sum(bool(item.get("stale")) for item in items)
    current_total = sum(not bool(item.get("stale")) for item in items)
    observed_at = float(result.get("observed_at") or time.time())
    value = {"schema_version": 3, "observed_at": observed_at,
             "complete": bool(result.get("complete")), "total": current_total,
             "older": stale_total, "stale_total": stale_total, "items": items, "by_session": by_session,
             "unattributed": [item.get("id") for item in items
                              if item.get("attribution") == "unattributed" and not item.get("stale")],
             "health": [{"source": row.get("source"), "state": row.get("state")}
                        for row in result.get("health", ()) if isinstance(row, dict)]}
    try:
        fleet_items = []
        for item in items:
            source = item.get("source")
            kind = item.get("kind")
            if source == "S1":
                source = "S1-approval" if kind in {"approval", "permission"} else "S1-question"
                kind = "approval" if source == "S1-approval" else "decision"
            elif source == "S2":
                kind = "human gate"
            elif source == "S3":
                kind = "blocked owner"
            elif source == "S4":
                kind = "marker"
            owner = item.get("owner") or {}
            fleet_item = {
                "id": item.get("id"), "source": source, "kind": kind,
                "session_id": owner.get("session_id"), "harness": owner.get("harness"),
                "pane_id": None, "route_id": item.get("route_id"),
                "node": item.get("gate_id"), "attempt_id": item.get("attempt_id"),
                "since": item.get("since"),
                "stale": "older" if item.get("stale") else "current",
            }
            if item.get("source") == "S1":
                for key in ("request_id", "turn_id", "item_id"):
                    ref = (_safe_request_id(item.get(key)) if key == "request_id"
                           else _safe_native_ref(item.get(key)))
                    if ref is not None:
                        fleet_item[key] = ref
            fleet_items.append(fleet_item)
        fleet_value = {"schema_version": 1, "observed_at": observed_at,
                       "complete": bool(result.get("complete")), "total": current_total,
                       "older": stale_total, "items": fleet_items,
                       "unattributed": [item["id"] for item in fleet_items
                                        if not item["session_id"] or not item["harness"]],
                       "health": [{"source": row.get("source"), "state": row.get("state")}
                                  for row in result.get("health", ()) if isinstance(row, dict)]}
        with _projection_lock():
            previous = _read_cache_file()
            selected = previous.get("selected_responses", {}) if previous else {}
            if not isinstance(selected, dict) or len(selected) > _SELECTED_RESPONSE_MAX:
                return False
            if selected:
                value["selected_responses"] = selected
            if not _write_cache_value(value):
                return False
            _write(_fleet_cache_path(), fleet_value)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _read_cache(*, now=None, max_age=_DISPLAY_TTL):
    value = _read_cache_file()
    current = time.time() if now is None else float(now)
    if (not isinstance(value, dict) or value.get("schema_version") != 3
            or not isinstance(value.get("observed_at"), (int, float))
            or isinstance(value.get("observed_at"), bool)
            or not math.isfinite(float(value["observed_at"]))
            or not isinstance(value.get("items"), list)
            or not isinstance(value.get("by_session"), dict)
            or not isinstance(value.get("total"), int) or isinstance(value.get("total"), bool)
            or not isinstance(value.get("older"), int) or isinstance(value.get("older"), bool)
            or current - float(value["observed_at"]) < 0
            or current - float(value["observed_at"]) > max_age):
        return None
    for item in value["items"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not isinstance(item.get("source"), str)
                or not isinstance(item.get("since"), (int, float))
                or isinstance(item.get("since"), bool) or not math.isfinite(float(item["since"]))
                or not isinstance(item.get("owner"), dict)):
            return None
        if (("request_id" in item and _safe_request_id(item["request_id"]) != item["request_id"])
                or any(_safe_native_ref(item[key]) != item[key]
                       for key in ("turn_id", "item_id") if key in item)):
            return None
    for session, ids in value["by_session"].items():
        if not isinstance(session, str) or "\0" not in session or not isinstance(ids, list) \
                or any(not isinstance(item_id, str) for item_id in ids):
            return None
    return value


def cached_fleet_view(*, now=None, request=True):
    """Return the question-free Fleet projection and request its shared producer."""
    path = _fleet_cache_path()
    try:
        parent = os.lstat(path.parent)
        meta = os.lstat(path)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) & 0o077
                or not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077 or meta.st_size > _CACHE_MAX_BYTES):
            raise OSError("invalid Fleet cache permissions or type")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = None
    current = time.time() if now is None else float(now)
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("observed_at"), (int, float))
            or current - float(value["observed_at"]) < 0
            or current - float(value["observed_at"]) > _DISPLAY_TTL):
        value = None
    if request and (value is None or current - float(value["observed_at"]) > _REQUEST_TTL):
        _request_producer()
    return value


def cached_view(*, now=None, request=True):
    value = _read_cache(now=now)
    if request:
        observed = value.get("observed_at") if value else None
        if observed is None or (time.time() if now is None else float(now)) - float(observed) > _REQUEST_TTL:
            _request_producer()
    if value is None:
        return None
    indexed = {tuple(key.split("\0", 1)): ids for key, ids in value.get("by_session", {}).items()
               if isinstance(key, str) and "\0" in key and isinstance(ids, list)}
    return {"items": value["items"], "by_session": indexed, "total": value.get("total", 0),
            "stale_total": value.get("stale_total", 0), "unattributed": value.get("unattributed", []),
            "health": value.get("health", []), "complete": bool(value.get("complete")),
            "observed_at": value["observed_at"]}


def _selected_identity(envelope):
    if not isinstance(envelope, dict):
        return None
    harness, session_id = envelope.get("harness"), envelope.get("session_id")
    request_id = _safe_request_id(envelope.get("request_id"))
    raw_turn_id = envelope.get("turn_id")
    turn_id = _safe_native_ref(raw_turn_id) if raw_turn_id is not None else None
    if (harness not in {"claude", "codex", "opencode"}
            or _safe_native_ref(session_id) != session_id or request_id is None
            or (raw_turn_id is not None and turn_id is None)):
        return None
    identity = {"harness": harness, "session_id": session_id,
                "request_id": request_id, "turn_id": turn_id}
    native = envelope.get("native_ref")
    native_request = native.get("request") if isinstance(native, dict) else None
    item_id = envelope.get("item_id")
    if item_id is None and isinstance(native_request, dict):
        item_id = native_request.get("item_id")
    if item_id is not None:
        item_id = _safe_native_ref(item_id)
        if item_id is None:
            return None
        identity["item_id"] = item_id
    for key in ("route_id", "question_id"):
        value = envelope.get(key)
        if value is not None:
            safe = _safe_native_ref(value)
            if safe is None:
                return None
            identity[key] = safe
    return identity


def _selected_entry_key(identity):
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _valid_selected_overlay(value):
    if not isinstance(value, dict) or len(value) > _SELECTED_RESPONSE_MAX:
        return False
    for key, entry in value.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
                or not isinstance(entry, dict) or not set(entry).issubset(_SELECTED_ENTRY_KEYS)
                or not {"identity", "state", "updated_at"}.issubset(entry)
                or entry.get("state") not in _SELECTED_STATES
                or not isinstance(entry.get("updated_at"), (int, float))
                or isinstance(entry.get("updated_at"), bool)
                or not math.isfinite(float(entry["updated_at"]))):
            return False
        identity = _selected_identity(entry.get("identity"))
        if identity != entry.get("identity") or _selected_entry_key(identity) != key:
            return False
        draft = entry.get("draft", "")
        if not isinstance(draft, str) or len(draft) > _SELECTED_DRAFT_MAX:
            return False
        if entry.get("action_id") is not None and not _SAFE.fullmatch(str(entry["action_id"])):
            return False
        response = entry.get("response")
        if response is not None:
            if not isinstance(response, dict):
                return False
            try:
                if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) > 16_384:
                    return False
            except (TypeError, ValueError):
                return False
        if entry.get("result") is not None and not isinstance(entry["result"], dict):
            return False
    return True


def _selected_entry(envelope):
    identity = _selected_identity(envelope)
    if identity is None:
        return None
    projection = _read_cache_file()
    if projection is None:
        return None
    overlay = projection.get("selected_responses", {})
    if not _valid_selected_overlay(overlay):
        return None
    entry = overlay.get(_selected_entry_key(identity))
    if not entry or entry.get("identity") != identity:
        return None
    return dict(entry)


def _store_selected_entry(envelope, entry):
    identity = _selected_identity(envelope)
    if identity is None:
        return False
    key = _selected_entry_key(identity)
    entry = dict(entry)
    entry["identity"] = identity
    entry["updated_at"] = time.time()
    if not _valid_selected_overlay({key: entry}):
        return False
    try:
        with _projection_lock():
            projection = _read_cache_file()
            if projection is None:
                return False
            overlay = projection.get("selected_responses", {})
            if not _valid_selected_overlay(overlay):
                return False
            previous = overlay.get(key)
            if previous and previous.get("identity") != identity:
                return False
            if previous is None and len(overlay) >= _SELECTED_RESPONSE_MAX:
                return False
            if previous:
                previous_action = previous.get("action_id")
                next_action = entry.get("action_id")
                if previous_action and next_action != previous_action:
                    return False
                if previous_action:
                    if entry.get("response") not in (None, previous.get("response")):
                        return False
                    entry["action_id"] = previous_action
                    entry["response"] = previous.get("response")
                    entry["draft"] = previous.get("draft", "")
                    rank = {"open": 0, "submitted": 1, "unknown": 2,
                            "received": 3, "applied": 4, "cancelled": 4, "expired": 4}
                    old_state = previous.get("state", "unknown")
                    new_state = entry.get("state", "unknown")
                    if (rank.get(new_state, -1) < rank.get(old_state, -1)
                            or (rank.get(new_state) == rank.get(old_state) == 4
                                and new_state != old_state)):
                        entry["state"] = old_state
                        entry["result"] = previous.get("result")
                    if entry.get("state") == "applied":
                        entry["draft"] = ""
                elif previous.get("state") in {"received", "applied", "cancelled", "expired"}:
                    old_state = previous["state"]
                    new_state = entry.get("state", "unknown")
                    if (new_state not in {"received", "applied", "cancelled", "expired"}
                            or (new_state != old_state and old_state in {"applied", "cancelled", "expired"})):
                        entry["state"] = old_state
                        entry["result"] = previous.get("result")
                        entry["draft"] = previous.get("draft", "")
                    elif entry.get("state") == "applied":
                        entry["draft"] = ""
            overlay[key] = entry
            projection["selected_responses"] = overlay
            return _write_cache_value(projection)
    except (OSError, ValueError, TypeError):
        return False


def _claim_selected_action(envelope, action_id, response):
    """Atomically persist the one allowed action before its native transport call."""
    identity = _selected_identity(envelope)
    if identity is None:
        return False, None
    key = _selected_entry_key(identity)
    try:
        with _projection_lock():
            projection = _read_cache_file()
            if projection is None:
                return False, None
            overlay = projection.get("selected_responses", {})
            if not _valid_selected_overlay(overlay):
                return False, None
            previous = overlay.get(key)
            if previous and previous.get("identity") != identity:
                return False, None
            if previous and (previous.get("action_id")
                             or previous.get("state") in {
                                 "submitted", "received", "applied", "cancelled", "expired"}):
                return False, dict(previous)
            if previous is None and len(overlay) >= _SELECTED_RESPONSE_MAX:
                return False, None
            claimed = dict(previous or {})
            claimed.update({"identity": identity, "state": "submitted",
                            "action_id": action_id, "response": response,
                            "result": {"status": "submitted"},
                            "updated_at": time.time()})
            claimed.setdefault("draft", envelope.get("draft", ""))
            if not _valid_selected_overlay({key: claimed}):
                return False, None
            overlay[key] = claimed
            projection["selected_responses"] = overlay
            if not _write_cache_value(projection):
                return False, None
            return True, dict(claimed)
    except (OSError, ValueError, TypeError):
        return False, None


def _proc_start_ticks(pid):
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
        fields = raw[raw.rindex(") ") + 2:].split()
        return fields[19] if fields[19].isdigit() else None
    except (OSError, ValueError, IndexError):
        return None


def _proc_uid(pid):
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _proc_argv(pid):
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        if len(raw) > 32768:
            return []
        return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    except OSError:
        return []


def _explicit_opencode_server(argv):
    found = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--server":
            if index + 1 >= len(argv) or found is not None:
                return None
            found = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--server="):
            if found is not None:
                return None
            found = arg.split("=", 1)[1]
        index += 1
    if not found:
        return None
    try:
        parsed = urllib.parse.urlsplit(found)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        if parsed.scheme == "http":
            try:
                local = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                local = parsed.hostname.lower() == "localhost"
            if not local:
                return None
        host = parsed.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        authority = host + ((":" + str(port)) if port else "")
        return parsed.scheme + "://" + authority
    except ValueError:
        return None


def _proc_listening_sockets(pid):
    """Return the exact loopback listener (port, socket inodes) owned by this PID."""
    fd_root = Path("/proc") / str(pid) / "fd"
    try:
        inodes = set()
        for fd in fd_root.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            match = re.fullmatch(r"socket:\[(\d+)\]", target)
            if match:
                inodes.add(match.group(1))
    except OSError:
        return None
    listeners = {}
    for table, ipv6 in ((Path("/proc/net/tcp"), False), (Path("/proc/net/tcp6"), True)):
        try:
            lines = table.read_text(encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A" or fields[9] not in inodes:
                continue
            try:
                address_hex, port_hex = fields[1].split(":", 1)
                raw = bytes.fromhex(address_hex)
                if ipv6:
                    raw = b"".join(raw[pos:pos + 4][::-1] for pos in range(0, 16, 4))
                else:
                    raw = raw[::-1]
                address = ipaddress.ip_address(raw)
                port = int(port_hex, 16)
            except (ValueError, OverflowError):
                continue
            if not (address.is_loopback or address.is_unspecified) or not port:
                continue
            target = listeners.setdefault(port, {"hosts": set(), "inodes": set()})
            target["hosts"].add("127.0.0.1" if address.version == 4 else "::1")
            target["inodes"].add(fields[9])
    if len(listeners) != 1:
        return None
    port, listener = next(iter(listeners.items()))
    host = "127.0.0.1" if "127.0.0.1" in listener["hosts"] else "::1"
    return port, host, tuple(sorted(listener["inodes"]))


def _opencode_endpoint(session_id):
    """Resolve one live OpenCode session to its own server process and listener."""
    if not sys.platform.startswith("linux") or not _safe_native_ref(session_id):
        return None
    try:
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import opencode as opencode_collector
        pids = sorted(int(path.name) for path in Path("/proc").iterdir()
                      if path.name.isdigit())
    except (OSError, ImportError, ValueError):
        return None
    found = []
    for pid in pids:
        if _proc_uid(pid) != os.getuid():
            continue
        start = _proc_start_ticks(pid)
        argv = _proc_argv(pid)
        if not start or not argv or os.path.basename(argv[0]) != "opencode":
            continue
        try:
            observed, source = opencode_collector.session_of_process(pid)
        except Exception:
            continue
        if observed != session_id or source not in {"opencode-tui-selection", "opencode-argv"}:
            continue
        if _proc_start_ticks(pid) != start:
            continue
        explicit = _explicit_opencode_server(argv)
        if explicit:
            base_url, socket_ids = explicit, ()
        else:
            listener = _proc_listening_sockets(pid)
            if listener is None:
                continue
            port, host, socket_ids = listener
            formatted_host = "[" + host + "]" if ":" in host else host
            base_url = "http://" + formatted_host + ":" + str(port)
        generation = hashlib.sha256(
            (str(pid) + "\0" + str(start) + "\0" + base_url + "\0" +
             ",".join(socket_ids)).encode("utf-8")).hexdigest()
        found.append({"url": base_url, "generation": generation,
                      "pid": pid, "proc_start": str(start),
                      "socket_inodes": list(socket_ids)})
    return found[0] if len(found) == 1 else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _opencode_http(endpoint, method, path, payload=None):
    """Call a checked OpenCode endpoint directly; never follow proxies or redirects."""
    if (not isinstance(endpoint, dict) or not isinstance(endpoint.get("url"), str)
            or (not path.startswith("/api/") and path != "/global/health")):
        return {"status": 0, "reason": "endpoint-invalid"}
    url = endpoint["url"].rstrip("/") + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=3.0) as response:
            status = int(response.status)
            raw = response.read(_CACHE_MAX_BYTES + 1)
            if len(raw) > _CACHE_MAX_BYTES:
                return {"status": status, "reason": "response-oversized"}
            if status == 204 or not raw:
                return {"status": status, "body": None}
            try:
                return {"status": status, "body": json.loads(raw.decode("utf-8"))}
            except (ValueError, UnicodeDecodeError):
                return {"status": status, "reason": "response-invalid"}
    except urllib.error.HTTPError as exc:
        return {"status": int(exc.code), "reason": "http-error"}
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return {"status": 0, "reason": "transport-unavailable"}


def _opencode_data(result):
    body = result.get("body") if isinstance(result, dict) else None
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return None


def _opencode_identity_matches(endpoint, session_id):
    current = _opencode_endpoint(session_id)
    return (current if current and endpoint
            and current.get("generation") == endpoint.get("generation") else None)


def _opencode_current_selected(base, *, requested_id=None):
    session_id = base["session_id"]
    endpoint = _opencode_endpoint(session_id)
    if endpoint is None:
        return {"state": "unknown", "reason": "opencode-server-identity-unavailable"}
    health = _opencode_http(endpoint, "GET", "/global/health")
    health_body = health.get("body") if isinstance(health, dict) else None
    if (health.get("status") != 200 or not isinstance(health_body, dict)
            or health_body.get("healthy") is not True
            or not isinstance(health_body.get("version"), str)
            or not health_body["version"].startswith("2.")):
        return {"state": "unknown", "reason": "opencode-v2-server-unavailable"}
    kind = "approval" if base["kind"] in {"approval", "permission"} else "question"
    if kind == "approval":
        collection = "permission"
    else:
        collection = "form"
    sid_path = urllib.parse.quote(session_id, safe="")
    list_result = _opencode_http(endpoint, "GET", f"/api/session/{sid_path}/{collection}")
    requests = _opencode_data(list_result)
    if list_result.get("status") != 200 or not isinstance(requests, list):
        return {"state": "unknown", "reason": "opencode-request-list-unavailable"}
    candidates = [row for row in requests if isinstance(row, dict)
                  and row.get("sessionID") == session_id
                  and _safe_native_ref(row.get("id")) is not None]
    wanted = requested_id or base.get("request_id") or base.get("item_id")
    if wanted is not None:
        matches = [row for row in candidates if row.get("id") == wanted]
    else:
        matches = candidates
    if len(matches) != 1:
        return {"state": "unknown", "reason": "opencode-request-missing-or-ambiguous"}
    request = matches[0]
    request_id = request["id"]
    id_path = urllib.parse.quote(request_id, safe="")
    detail_result = _opencode_http(
        endpoint, "GET", f"/api/session/{sid_path}/{collection}/{id_path}")
    detail = _opencode_data(detail_result)
    if (detail_result.get("status") != 200 or not isinstance(detail, dict)
            or detail.get("sessionID") != session_id or detail.get("id") != request_id):
        return {"state": "unknown", "reason": "opencode-request-detail-unavailable"}
    if not _opencode_identity_matches(endpoint, session_id):
        return {"state": "unknown", "reason": "opencode-server-replaced"}
    native_ref = {"transport": "opencode-http-v2", "generation": endpoint["generation"],
                  "request_id": request_id, "server_pid": endpoint["pid"],
                  "proc_start": endpoint["proc_start"],
                  "socket_inodes": endpoint["socket_inodes"]}
    if kind == "approval":
        turn_id = _safe_native_ref(detail.get("messageID"))
        if detail.get("state") not in (None, "pending"):
            return {"state": "unknown", "reason": "opencode-permission-not-pending"}
        request_detail = {
            "title": detail.get("title") if isinstance(detail.get("title"), str) else "",
            "operation": detail.get("type") if isinstance(detail.get("type"), str) else "",
            "metadata": detail.get("metadata") if isinstance(detail.get("metadata"), dict) else {},
            "pattern": detail.get("pattern"),
            "message_id": turn_id,
        }
        supported = bool(turn_id and (request_detail["title"] or request_detail["operation"]
                                      or request_detail["metadata"] or request_detail["pattern"]))
        return {"state": "open" if supported else "unknown",
                "reason": None if supported else "opencode-permission-binding-or-detail-incomplete",
                "kind": kind, "request_id": request_id, "turn_id": turn_id,
                "question_id": None, "detail": request_detail, "choices": None,
                "support": supported, "native_ref": native_ref}

    form_state = detail.get("state")
    if not isinstance(form_state, dict) or form_state.get("status") != "pending":
        status = form_state.get("status") if isinstance(form_state, dict) else None
        return {"state": "cancelled" if status == "cancelled" else
                "applied" if status == "answered" else "unknown",
                "reason": "opencode-form-not-pending", "kind": kind,
                "request_id": request_id, "native_ref": native_ref}
    fields = detail.get("fields")
    if not isinstance(fields, list) or len(fields) != 1 or not isinstance(fields[0], dict):
        return {"state": "unknown", "reason": "opencode-form-needs-native-ui",
                "kind": kind, "request_id": request_id, "native_ref": native_ref}
    field = fields[0]
    question_id = _safe_native_ref(field.get("key"))
    metadata = detail.get("metadata") if isinstance(detail.get("metadata"), dict) else {}
    # Use an owner message/turn ref only when the native request metadata supplies one.
    turn_id = (_safe_native_ref(metadata.get("turnID"))
               or _safe_native_ref(metadata.get("messageID")))
    field_type = field.get("type")
    choices = None
    option_values = []
    supported = field_type == "string"
    minimum, maximum = field.get("minItems"), field.get("maxItems")
    bounds_valid = ((minimum is None or type(minimum) is int and minimum >= 0)
                    and (maximum is None or type(maximum) is int and maximum >= 0)
                    and (minimum is None or maximum is None or minimum <= maximum))
    if field_type == "multiselect" and isinstance(field.get("options"), list):
        choices = []
        for option in field["options"]:
            if not isinstance(option, dict):
                return {"state": "unknown", "reason": "opencode-form-options-invalid",
                        "kind": kind, "request_id": request_id, "native_ref": native_ref}
            value, label = option.get("value"), option.get("label")
            if not isinstance(value, str) or not value or not isinstance(label, str):
                return {"state": "unknown", "reason": "opencode-form-options-invalid",
                        "kind": kind, "request_id": request_id, "native_ref": native_ref}
            option_values.append(value)
            choices.append({"value": value, "label": label,
                            "description": option.get("description")
                            if isinstance(option.get("description"), str) else ""})
        supported = bool(choices) or bool(field.get("custom"))
    if field_type == "multiselect" and not bounds_valid:
        supported = False
    if not question_id or not turn_id:
        supported = False
    native_ref.update({"field_type": field_type, "option_values": option_values,
                       "custom": bool(field.get("custom")),
                       "min_items": minimum, "max_items": maximum})
    detail_value = {
        "title": detail.get("title") if isinstance(detail.get("title"), str) else "",
        "question": field.get("title") if isinstance(field.get("title"), str) else
                    (detail.get("title") if isinstance(detail.get("title"), str) else ""),
        "description": field.get("description") if isinstance(field.get("description"), str) else "",
        "field_type": field_type,
    }
    return {"state": "open" if supported else "unknown",
            "reason": None if supported else "opencode-form-schema-or-turn-binding-unsupported",
            "kind": kind, "request_id": request_id, "turn_id": turn_id,
            "question_id": question_id, "detail": detail_value,
            "choices": choices, "support": supported, "native_ref": native_ref}


def _codex_control_path(argv):
    result = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--control":
            if result is not None or index + 1 >= len(argv):
                return None
            result = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--control="):
            if result is not None:
                return None
            result = arg.split("=", 1)[1]
        index += 1
    return result


def _codex_control_request(path, payload):
    try:
        path = Path(path)
        meta = os.lstat(path)
        if (not stat.S_ISSOCK(meta.st_mode) or meta.st_uid != os.getuid()
                or stat.S_IMODE(meta.st_mode) & 0o077):
            return None
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > 256 * 1024:
            return None
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2.5)
        try:
            connection.connect(str(path))
            connection.sendall(encoded)
            response = bytearray()
            while b"\n" not in response:
                chunk = connection.recv(8192)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > _CACHE_MAX_BYTES:
                    return None
        finally:
            connection.close()
        line, separator, remainder = bytes(response).partition(b"\n")
        if not separator or remainder.strip():
            return None
        value = json.loads(line.decode("utf-8"))
        return value if isinstance(value, dict) and value.get("schema_version") == 1 else None
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return None


def _codex_control(session_id):
    """Discover the one same-user managed gateway bound to this exact thread."""
    if not sys.platform.startswith("linux") or not _safe_native_ref(session_id):
        return None
    matches = []
    try:
        pids = sorted(int(path.name) for path in Path("/proc").iterdir()
                      if path.name.isdigit())
    except (OSError, ValueError):
        return None
    for pid in pids:
        if _proc_uid(pid) != os.getuid():
            continue
        start = _proc_start_ticks(pid)
        argv = _proc_argv(pid)
        if not start or not any(os.path.basename(arg) == "codex-managed-gateway.py" for arg in argv):
            continue
        control_path = _codex_control_path(argv)
        if not control_path or _proc_start_ticks(pid) != start:
            continue
        status = _codex_control_request(control_path, {"schema_version": 1, "op": "status"})
        if (not status or status.get("status") != "ready"
                or status.get("binding_blocked") is not False
                or status.get("thread_id") != session_id
                or status.get("upstream_clients") != 1
                or not isinstance(status.get("epoch"), int)
                or isinstance(status.get("epoch"), bool)):
            continue
        if _proc_start_ticks(pid) != start:
            continue
        matches.append({"path": control_path, "pid": pid, "proc_start": str(start),
                        "epoch": status["epoch"], "status": status})
    return matches[0] if len(matches) == 1 else None


def _codex_safe_ref(ref):
    if not isinstance(ref, dict):
        return False
    forbidden = {"detail", "question", "choices", "prompt", "draft", "response",
                 "command", "arguments", "output", "text", "payload"}
    if forbidden.intersection(ref):
        return False
    no_turn_elicitation = (ref.get("kind") == "elicitation"
                           and ref.get("method") == "mcpServer/elicitation/request"
                           and ref.get("turn_id") is None)
    return (ref.get("harness") == "codex"
            and isinstance(ref.get("session_id"), str)
            and _safe_request_id(ref.get("request_id")) is not None
            and (_safe_native_ref(ref.get("turn_id")) is not None or no_turn_elicitation))


def _codex_exact_binding(value):
    no_turn_elicitation = (isinstance(value, dict) and value.get("kind") == "elicitation"
                           and value.get("turn_id") is None)
    return (isinstance(value, dict) and value.get("harness") == "codex"
            and isinstance(value.get("session_id"), str)
            and _safe_request_id(value.get("request_id")) is not None
            and (_safe_native_ref(value.get("turn_id")) is not None or no_turn_elicitation))


def _codex_operation_available(value):
    status = value.get("status") if isinstance(value, dict) else None
    return (isinstance(status, str) and bool(status)
            and status.lower() not in {"unsupported", "unknown", "error", "rejected",
                                       "disconnected", "stale", "unavailable"})


def _codex_questions(detail):
    questions = detail.get("questions") if isinstance(detail, dict) else None
    if not isinstance(questions, list) or not questions:
        return None, "codex-question-list-unavailable"
    normalized = []
    seen = set()
    for question in questions:
        if not isinstance(question, dict):
            return None, "codex-question-schema-invalid"
        question_id = _safe_native_ref(question.get("id"))
        prompt = question.get("question")
        options = question.get("options")
        if (not question_id or question_id in seen or not isinstance(prompt, str) or not prompt
                or (options is not None and not isinstance(options, list))):
            return None, "codex-question-schema-invalid"
        seen.add(question_id)
        choices = None
        if isinstance(options, list):
            choices = []
            for option in options:
                if not isinstance(option, dict) or not isinstance(option.get("label"), str) \
                        or not option["label"]:
                    return None, "codex-question-options-invalid"
                choices.append({"value": option["label"], "label": option["label"],
                                "description": option.get("description")
                                if isinstance(option.get("description"), str) else ""})
        normalized.append({"id": question_id,
                           "header": question.get("header")
                           if isinstance(question.get("header"), str) else "",
                           "question": prompt, "choices": choices,
                           "is_other": bool(question.get("isOther")),
                           "is_secret": bool(question.get("isSecret"))})
    result = {"questions": normalized}
    if len(normalized) == 1:
        result["question"] = normalized[0]["question"]
        result["header"] = normalized[0]["header"]
    return {"detail": result,
            "question_id": normalized[0]["id"] if len(normalized) == 1 else None,
            "choices": normalized[0]["choices"] if len(normalized) == 1 else None}, None


def _codex_request_ref_matches(base, ref):
    if (not _codex_safe_ref(ref) or ref.get("session_id") != base.get("session_id")):
        return False
    wanted_id = base.get("request_id")
    if wanted_id is not None and not _same_request_id(wanted_id, ref.get("request_id")):
        return False
    for key in ("turn_id", "item_id"):
        wanted = base.get(key)
        if wanted is not None and ref.get(key) != wanted:
            return False
    return True


def _codex_retired_selected(base, control, saved_ref):
    saved_gateway = base.get("native_ref")
    if (not isinstance(saved_gateway, dict)
            or saved_gateway.get("transport") != "codex-managed-control"
            or saved_gateway.get("pid") != control.get("pid")
            or saved_gateway.get("proc_start") != control.get("proc_start")
            or saved_gateway.get("control_path") != control.get("path")
            or not _codex_request_ref_matches(base, saved_ref)):
        return None
    result = _codex_control_request(control["path"], {
        "schema_version": 1, "op": "interaction-detail", "ref": saved_ref})
    request = result.get("request") if isinstance(result, dict) else None
    if (not isinstance(request, dict) or not _codex_exact_binding(request)
            or request.get("session_id") != base.get("session_id")
            or not _same_request_id(request.get("request_id"), saved_ref.get("request_id"))
            or request.get("turn_id") != saved_ref.get("turn_id")):
        return None
    state = request.get("state")
    status = result.get("status")
    if state not in _SELECTED_STATES or status != state:
        return None
    return {"state": state, "kind": request.get("kind"),
            "request_id": request["request_id"], "turn_id": request.get("turn_id"),
            "detail": request.get("detail"), "support": False,
            "reason": "codex-native-request-" + state,
            "native_ref": saved_gateway, "gateway": control}


def _codex_current_selected(base, *, requested_id=None):
    session_id = base["session_id"]
    control = _codex_control(session_id)
    if control is None:
        return {"state": "unknown", "reason": "codex-managed-control-unavailable"}
    listing = _codex_control_request(control["path"], {
        "schema_version": 1, "op": "interaction-list", "session_id": session_id})
    requests = listing.get("requests") if isinstance(listing, dict) else None
    if not _codex_operation_available(listing) or not isinstance(requests, list):
        return {"state": "unknown", "reason": "codex-native-request-list-unavailable"}
    candidates = [ref for ref in requests if _codex_safe_ref(ref)
                  and ref.get("session_id") == session_id]
    wanted_id = requested_id if requested_id is not None else base.get("request_id")
    wanted_item = base.get("item_id")
    if wanted_id is not None:
        candidates = [ref for ref in candidates
                      if _same_request_id(ref.get("request_id"), wanted_id)]
    if wanted_item is not None:
        candidates = [ref for ref in candidates if ref.get("item_id") == wanted_item]
    saved = base.get("native_ref")
    saved_ref = saved.get("request") if isinstance(saved, dict) else None
    if isinstance(saved_ref, dict):
        exact = [ref for ref in candidates if ref == saved_ref]
        if exact:
            candidates = exact
        else:
            retired = _codex_retired_selected(base, control, saved_ref)
            if retired is not None:
                return retired
            if candidates:
                return {"state": "unknown", "reason": "codex-native-request-binding-changed"}
    if not candidates:
        return {"state": "unknown", "reason": "codex-native-request-missing-or-ambiguous"}
    if len(candidates) != 1:
        return {"state": "unknown", "reason": "codex-native-request-missing-or-ambiguous"}
    safe_ref = candidates[0]
    ref_epoch = safe_ref.get("connection_epoch", safe_ref.get("gateway_epoch", safe_ref.get("epoch")))
    if ref_epoch != control["epoch"]:
        retired = _codex_retired_selected(base, control, safe_ref)
        if retired is not None:
            return retired
        return {"state": "unknown", "reason": "codex-native-connection-replaced"}
    detail_result = _codex_control_request(control["path"], {
        "schema_version": 1, "op": "interaction-detail", "ref": safe_ref})
    request = detail_result.get("request") if isinstance(detail_result, dict) else None
    if (not _codex_operation_available(detail_result)
            or not _codex_exact_binding(request)
            or request.get("session_id") != session_id
            or not _same_request_id(request.get("request_id"), safe_ref.get("request_id"))
            or request.get("turn_id") != safe_ref.get("turn_id")):
        return {"state": "unknown", "reason": "codex-native-request-detail-unavailable"}
    request_epoch = request.get("connection_epoch", request.get("gateway_epoch", request.get("epoch")))
    if request_epoch is not None and request_epoch != control["epoch"]:
        return {"state": "unknown", "reason": "codex-native-request-not-current"}
    kind = request.get("kind")
    if request.get("state") != "open":
        return _codex_retired_selected(base, control, safe_ref) or {
            "state": "unknown", "reason": "codex-native-request-not-current"}
    if kind not in {"question", "approval", "elicitation"}:
        return {"state": "unknown", "kind": kind,
                "request_id": request.get("request_id"), "turn_id": request.get("turn_id"),
                "detail": request.get("detail"), "support": False,
                "reason": "codex-native-request-kind-unsupported",
                "native_ref": {"transport": "codex-managed-control",
                               "pid": control["pid"], "proc_start": control["proc_start"],
                               "control_path": control["path"], "epoch": control["epoch"],
                               "request": safe_ref}, "gateway": control}
    if not isinstance(request.get("detail"), (str, dict)):
        return {"state": "unknown", "reason": "codex-native-request-detail-invalid"}
    detail = request["detail"]
    question_id = request.get("question_id")
    choices = request.get("choices")
    reason = None
    if kind == "question":
        normalized, reason = _codex_questions(detail)
        if normalized is None:
            return {"state": "open", "kind": kind, "request_id": request["request_id"],
                    "turn_id": request["turn_id"], "detail": detail, "support": False,
                    "reason": reason, "native_ref": {"transport": "codex-managed-control",
                    "pid": control["pid"], "proc_start": control["proc_start"],
                    "control_path": control["path"], "epoch": control["epoch"],
                    "request": safe_ref}, "gateway": control}
        detail, question_id, choices = (normalized["detail"], normalized["question_id"],
                                        normalized["choices"])
    native_ref = {"transport": "codex-managed-control",
                  "pid": control["pid"], "proc_start": control["proc_start"],
                  "control_path": control["path"], "epoch": control["epoch"],
                  "request": safe_ref}
    support = kind in {"question", "approval"}
    reason = None
    native_support = request.get("support")
    if isinstance(native_support, dict) and native_support.get("approve_supported") is False:
        reason = native_support.get("reason")
    if kind == "elicitation":
        support = False
        reason = ("codex-native-turn-binding-unavailable" if request.get("turn_id") is None
                  else "codex-native-elicitation-requires-native-ui")
    return {"state": "open", "kind": kind, "request_id": request["request_id"],
            "turn_id": request["turn_id"], "route_id": request.get("route_id"),
            "question_id": question_id, "item_id": request.get("item_id"),
            "detail": detail, "choices": choices, "support": support, "reason": reason,
            "native_ref": native_ref,
            "gateway": control}


def _selected_base(ref):
    cache = _read_cache()
    if cache is None:
        _request_producer()
        return None, "selected-wait-cache-unknown"
    if isinstance(ref, str):
        values = {"id": ref}
    elif isinstance(ref, dict):
        values = ref.get("selected_ref") if isinstance(ref.get("selected_ref"), dict) else ref
        if isinstance(ref.get("ref"), dict):
            values = ref["ref"]
    else:
        return None, "selected-wait-reference-invalid"
    item_id = values.get("id") or values.get("selected_id")
    if not isinstance(item_id, str):
        return None, "selected-wait-reference-invalid"
    item = next((row for row in cache["items"] if row.get("id") == item_id), None)
    if not isinstance(item, dict) or item.get("source") != "S1":
        return None, "selected-item-not-current"
    owner = item.get("owner") or {}
    harness, session_id = owner.get("harness"), owner.get("session_id")
    if harness not in {"claude", "codex", "opencode"} or not _safe_native_ref(session_id):
        return None, "selected-item-identity-unknown"
    for key, actual in (("harness", harness), ("session_id", session_id)):
        supplied = values.get(key)
        if supplied is not None and supplied != actual:
            return None, "selected-item-identity-changed"
    route_id = item.get("route_id")
    supplied_route = values.get("route_id")
    if route_id is not None and supplied_route not in (None, route_id):
        return None, "selected-route-changed"
    hints = {}
    for key in ("request_id", "turn_id", "item_id", "question_id"):
        safe_ref = _safe_request_id if key == "request_id" else _safe_native_ref
        actual = safe_ref(item.get(key))
        supplied = safe_ref(values.get(key))
        if item.get(key) is not None and actual is None:
            return None, "selected-native-reference-invalid"
        if (actual is not None and supplied is not None
                and (not _same_request_id(actual, supplied) if key == "request_id"
                     else actual != supplied)):
            return None, "selected-native-reference-changed"
        hints[key] = actual if actual is not None else supplied
    raw_kind = item.get("kind")
    if raw_kind not in {"decision", "approval", "permission", "elicitation"}:
        return None, "selected-kind-unsupported"
    return {"id": item_id, "harness": harness, "session_id": session_id,
            "route_id": route_id, "kind": raw_kind, **hints,
            "native_ref": ref.get("native_ref") if isinstance(ref, dict) else None}, None


def _selected_fallback(base, reason, *, state="unknown"):
    if base is None:
        base = {"harness": "unknown", "session_id": "", "kind": "question"}
    kind = "approval" if base.get("kind") in {"approval", "permission"} else "question"
    value = {"v": 1, "harness": base.get("harness"),
             "session_id": base.get("session_id"),
             "request_id": base.get("request_id"), "turn_id": base.get("turn_id"),
             "kind": kind, "detail": None, "state": state,
             "support": {"supported": False, "reason": reason},
             "actions": [], "result": {"reason": reason}}
    for key in ("route_id", "question_id"):
        if base.get(key) is not None:
            value[key] = base[key]
    if base.get("id"):
        value["selected_id"] = base["id"]
    entry = _selected_entry(value)
    if entry:
        value["draft"] = entry.get("draft", "")
        if entry.get("action_id"):
            value["result"] = entry.get("result") or value["result"]
        if (value["state"] == "unknown"
                and entry.get("state") in {"submitted", "received", "applied", "cancelled", "expired"}):
                value["state"] = entry["state"]
    return value


def _selected_actions(kind, detail, native_ref):
    if kind != "approval":
        return ["reply"]
    request = native_ref.get("request") if isinstance(native_ref, dict) else None
    if (isinstance(request, dict)
            and request.get("method") == "item/fileChange/requestApproval"):
        changes = detail.get("changes") if isinstance(detail, dict) else None
        if (not isinstance(detail, dict) or detail.get("grantRoot") is not None
                or not isinstance(changes, list) or not changes
                or any(not isinstance(change, dict) or not isinstance(change.get("path"), str)
                       or not change["path"] for change in changes)):
            return ["reject", "defer"]
    if (isinstance(request, dict)
            and request.get("method") == "item/commandExecution/requestApproval"):
        available = detail.get("availableDecisions") if isinstance(detail, dict) else None
        if not isinstance(available, list):
            return ["defer"]
        actions = []
        if "accept" in available:
            actions.append("approve")
        if "decline" in available:
            actions.append("reject")
        actions.append("defer")
        return actions
    return ["approve", "reject", "defer"]


def _selected_envelope(base, resolved):
    if not isinstance(resolved, dict):
        return _selected_fallback(base, "native-request-unavailable")
    request_id = _safe_request_id(resolved.get("request_id"))
    if request_id is None:
        request_id = base.get("request_id")
    turn_id = _safe_native_ref(resolved.get("turn_id")) or base.get("turn_id")
    route_id = base.get("route_id")
    native_route = _safe_native_ref(resolved.get("route_id"))
    if route_id is not None and native_route is not None and route_id != native_route:
        return _selected_fallback(base, "native-route-binding-changed")
    kind = resolved.get("kind") if resolved.get("kind") in {"question", "approval"} else (
        "approval" if base.get("kind") in {"approval", "permission"} else "question")
    envelope = {"v": 1, "harness": base["harness"], "session_id": base["session_id"],
                "request_id": request_id, "turn_id": turn_id, "kind": kind,
                "detail": resolved.get("detail"),
                "state": resolved.get("state") if resolved.get("state") in _SELECTED_STATES else "unknown"}
    if route_id is not None:
        envelope["route_id"] = route_id
    question_id = _safe_native_ref(resolved.get("question_id")) or base.get("question_id")
    if question_id is not None:
        envelope["question_id"] = question_id
    choices = resolved.get("choices")
    if isinstance(choices, list):
        envelope["choices"] = choices
    if base.get("id"):
        envelope["selected_id"] = base["id"]
    native_ref = resolved.get("native_ref")
    if isinstance(native_ref, dict):
        envelope["native_ref"] = native_ref
    support = bool(resolved.get("support"))
    reason = resolved.get("reason")
    envelope["support"] = {"supported": support}
    if isinstance(reason, str):
        envelope["support"]["reason"] = reason
        envelope["reason"] = reason
    entry = _selected_entry(envelope)
    if entry:
        envelope["draft"] = entry.get("draft", "")
        if entry.get("result") is not None:
            envelope["result"] = entry["result"]
        if entry.get("state") in {"submitted", "received", "applied", "cancelled", "expired", "unknown"}:
            if envelope["state"] == "open":
                envelope["state"] = entry["state"]
            if entry.get("action_id"):
                envelope["action_id"] = entry["action_id"]
    if envelope["state"] == "open" and support:
        envelope["actions"] = _selected_actions(kind, envelope.get("detail"), native_ref)
    else:
        envelope["actions"] = []
    return envelope


def _selected_transport(base, requested_id=None):
    try:
        if base["harness"] == "codex":
            return _codex_current_selected(base, requested_id=requested_id)
        if base["harness"] == "opencode":
            return _opencode_current_selected(base, requested_id=requested_id)
        return {"state": "unknown", "reason": "claude-native-response-surface-unavailable"}
    except Exception:
        return {"state": "unknown", "reason": "native-request-observation-failed"}


def read_selected_request(ref):
    """Read one selected current native request; details never enter shared projections."""
    base, reason = _selected_base(ref)
    if base is None:
        return _selected_fallback(None, reason)
    requested_id = base.get("request_id")
    resolved = _selected_transport(base, requested_id=requested_id)
    if not isinstance(resolved, dict):
        return _selected_fallback(base, "native-request-unavailable")
    resolved_id = _safe_request_id(resolved.get("request_id"))
    resolved_turn = _safe_native_ref(resolved.get("turn_id"))
    resolved_base = dict(base)
    if resolved_id is not None:
        resolved_base["request_id"] = resolved_id
    if resolved_turn:
        resolved_base["turn_id"] = resolved_turn
    if (base.get("request_id") is not None and resolved_id is not None
            and base["request_id"] != resolved_id):
        return _selected_fallback(resolved_base, "native-request-binding-changed")
    if (base.get("turn_id") is not None and resolved_turn is not None
            and base["turn_id"] != resolved_turn):
        return _selected_fallback(resolved_base, "native-turn-binding-changed")
    old_native_ref = base.get("native_ref")
    new_native_ref = resolved.get("native_ref")
    if old_native_ref is not None and old_native_ref != new_native_ref:
        return _selected_fallback(resolved_base, "native-transport-incarnation-changed")
    if (base.get("route_id") is not None and resolved.get("route_id") is not None
            and base["route_id"] != resolved.get("route_id")):
        return _selected_fallback(resolved_base, "native-route-binding-changed")
    envelope = _selected_envelope(resolved_base, resolved)
    if envelope["state"] in {"submitted", "received", "applied", "cancelled", "expired"}:
        identity = _selected_identity(envelope)
        if identity is not None:
            previous = _selected_entry(envelope) or {"draft": "", "state": "open"}
            if previous.get("state") != envelope["state"]:
                previous["state"] = envelope["state"]
                if envelope["state"] == "applied":
                    previous["draft"] = ""
                previous["result"] = {"source": "codex-managed-gateway",
                                       "status": envelope["state"]}
                if _store_selected_entry(envelope, previous):
                    stored = _selected_entry(envelope)
                    if stored:
                        envelope["state"] = stored.get("state", envelope["state"])
                        envelope["result"] = stored.get("result", envelope.get("result"))
                        envelope["draft"] = stored.get("draft", envelope.get("draft", ""))
    return envelope


def write_selected_draft(ref, draft):
    """Persist only a selected request's private draft in the owner-only cache overlay."""
    base, reason = _selected_base(ref)
    if base is None:
        return _selected_fallback(None, reason)
    if isinstance(ref, dict) and ref.get("state") in {"applied", "cancelled", "expired"}:
        return dict(ref)
    identity = _selected_identity(ref) if isinstance(ref, dict) else None
    if (identity is None or identity.get("harness") != base["harness"]
            or identity.get("session_id") != base["session_id"]
            or identity.get("route_id") != base.get("route_id")):
        return _selected_fallback(base, "exact-selected-request-binding-required")
    if not isinstance(draft, str) or len(draft.encode("utf-8")) > _SELECTED_DRAFT_MAX:
        return _selected_fallback(base, "selected-draft-invalid")
    envelope = dict(ref)
    envelope["selected_id"] = base["id"]
    previous = _selected_entry(envelope)
    if previous and (previous.get("state") in {"submitted", "received", "applied",
                                                "cancelled", "expired"}
                     or (previous.get("state") == "unknown" and previous.get("action_id"))):
        return _selected_fallback(base, "selected-response-already-settled-or-ambiguous",
                                  state=previous["state"])
    entry = previous or {"draft": "", "state": "open"}
    draft_state = "unknown" if ref.get("state") == "unknown" else "open"
    entry["draft"] = draft
    entry["state"] = draft_state
    if not _store_selected_entry(envelope, entry):
        return _selected_fallback(base, "selected-draft-storage-unavailable")
    updated = dict(ref)
    updated.update({"selected_id": base["id"], "draft": draft, "state": draft_state})
    return updated


def _portable_response(envelope, response):
    if not isinstance(response, dict):
        return None, "response-shape-invalid"
    if envelope.get("kind") == "approval":
        if set(response) != {"decision"} or response.get("decision") not in {"approve", "reject", "defer"}:
            return None, "approval-decision-required"
        return {"decision": response["decision"]}, None
    if envelope.get("kind") != "question" or set(response) != {"answers"}:
        return None, "question-answer-required"
    answers = response.get("answers")
    detail = envelope.get("detail")
    questions = detail.get("questions") if isinstance(detail, dict) else None
    if isinstance(questions, list) and questions:
        question_ids = [_safe_native_ref(question.get("id"))
                        if isinstance(question, dict) else None for question in questions]
        if any(question_id is None for question_id in question_ids) \
                or len(set(question_ids)) != len(question_ids):
            return None, "native-question-identities-invalid"
    else:
        question_id = _safe_native_ref(envelope.get("question_id"))
        question_ids = [question_id] if question_id is not None else []
    if not isinstance(answers, dict) or not question_ids or set(answers) != set(question_ids):
        return None, "nonempty-exact-question-answer-required"
    normalized = {}
    for question_id in question_ids:
        values = answers.get(question_id)
        if (not isinstance(values, list) or not values
                or any(not isinstance(value, str) or not value.strip()
                       or len(value.encode("utf-8")) > _SELECTED_DRAFT_MAX for value in values)):
            return None, "nonempty-exact-question-answer-required"
        normalized[question_id] = list(values)
    return {"answers": normalized}, None


def _native_response_error(envelope, portable):
    """Check a portable response against the exact selected native request shape."""
    native_ref = envelope.get("native_ref")
    detail = envelope.get("detail")
    if envelope.get("kind") == "approval":
        decision = portable.get("decision")
        if decision == "defer":
            return None
        request_ref = native_ref.get("request") if isinstance(native_ref, dict) else None
        if (isinstance(request_ref, dict)
                and request_ref.get("method") == "item/commandExecution/requestApproval"):
            available = detail.get("availableDecisions") if isinstance(detail, dict) else None
            native_decision = "accept" if decision == "approve" else "decline"
            if not isinstance(available, list) or native_decision not in available:
                return "codex-command-decision-not-available"
        if decision not in _selected_actions("approval", detail, native_ref):
            return "native-approval-decision-not-available"
        return None

    answers = portable["answers"]
    harness = envelope.get("harness")
    if harness == "opencode":
        field_type = native_ref.get("field_type") if isinstance(native_ref, dict) else None
        values = answers.get(envelope.get("question_id"), [])
        if field_type == "string":
            return None if len(values) == 1 else "opencode-string-answer-count-invalid"
        if field_type != "multiselect":
            return "opencode-form-type-unsupported"
        allowed = native_ref.get("option_values")
        if not isinstance(allowed, list):
            return "opencode-options-unavailable"
        if len(set(values)) != len(values):
            return "opencode-answer-duplicate-option"
        if not native_ref.get("custom") and any(value not in allowed for value in values):
            return "opencode-answer-not-in-current-options"
        minimum, maximum = native_ref.get("min_items"), native_ref.get("max_items")
        if ((minimum is not None and (type(minimum) is not int or minimum < 0))
                or (maximum is not None and (type(maximum) is not int or maximum < 0))
                or (minimum is not None and maximum is not None and minimum > maximum)):
            return "opencode-answer-constraints-invalid"
        if ((minimum is not None and len(values) < minimum)
                or (maximum is not None and len(values) > maximum)):
            return "opencode-answer-count-invalid"
        return None

    if harness == "codex" and isinstance(detail, dict):
        questions = detail.get("questions")
        if not isinstance(questions, list):
            question_id = envelope.get("question_id")
            questions = [{"id": question_id, "choices": envelope.get("choices")
                          if isinstance(envelope.get("choices"), list) else None,
                          "is_other": False}]
        for question in questions:
            if not isinstance(question, dict):
                return "codex-question-schema-invalid"
            question_id = question.get("id")
            values = answers.get(question_id, [])
            choices = question.get("choices", question.get("options"))
            if (isinstance(choices, list) and not question.get("is_other")
                    and any(value not in {
                        option.get("value", option.get("label"))
                        for option in choices if isinstance(option, dict)
                    } for value in values)):
                return "codex-answer-not-in-current-options"
    return None


def _selected_invalid_response(envelope, reason):
    """Keep a known local validation failure editable without claiming the action."""
    value = dict(envelope)
    value.update({"state": "open", "reason": reason,
                  "result": {"status": "invalid-local-response", "reason": reason}})
    value.setdefault("draft", "")
    value["actions"] = list(envelope.get("actions", ()))
    return value


def _opencode_submit(envelope, portable):
    base, error = _selected_base(envelope)
    if base is None:
        return "unknown", {"reason": error}
    native_ref = envelope.get("native_ref")
    if not isinstance(native_ref, dict) or native_ref.get("transport") != "opencode-http-v2":
        return "unknown", {"reason": "opencode-native-reference-required"}
    current = _opencode_current_selected(base, requested_id=envelope.get("request_id"))
    if (current.get("state") != "open" or not current.get("support")
            or current.get("native_ref") != native_ref
            or current.get("request_id") != envelope.get("request_id")
            or current.get("turn_id") != envelope.get("turn_id")):
        return "unknown", {"reason": current.get("reason") or "opencode-request-binding-changed"}
    endpoint = _opencode_endpoint(base["session_id"])
    if endpoint is None or endpoint.get("generation") != native_ref.get("generation"):
        return "unknown", {"reason": "opencode-server-replaced"}
    session_path = urllib.parse.quote(base["session_id"], safe="")
    request_path = urllib.parse.quote(envelope["request_id"], safe="")
    if envelope["kind"] == "approval":
        decision = "once" if portable["decision"] == "approve" else "reject"
        reply_path = f"/api/session/{session_path}/permission/{request_path}/reply"
        result = _opencode_http(endpoint, "POST", reply_path, {"decision": decision})
        if result.get("status") != 204:
            return "unknown", {"reason": "opencode-permission-response-ambiguous"}
        if not _opencode_identity_matches(endpoint, base["session_id"]):
            return "unknown", {"reason": "opencode-server-replaced-after-response"}
        return "applied", {"transport": "received", "runtime": "applied", "http_status": 204}
    field_type = native_ref.get("field_type")
    values = portable["answers"][envelope["question_id"]]
    if field_type == "string":
        if len(values) != 1:
            return "unknown", {"reason": "opencode-string-answer-shape-invalid"}
        answer_value = values[0]
    elif field_type == "multiselect":
        allowed = native_ref.get("option_values")
        if not isinstance(allowed, list):
            return "unknown", {"reason": "opencode-options-unavailable"}
        if (not native_ref.get("custom") and any(value not in allowed for value in values)):
            return "unknown", {"reason": "opencode-answer-not-in-current-options"}
        minimum, maximum = native_ref.get("min_items"), native_ref.get("max_items")
        if (isinstance(minimum, int) and len(values) < minimum
                or isinstance(maximum, int) and len(values) > maximum):
            return "unknown", {"reason": "opencode-answer-count-invalid"}
        answer_value = values
    else:
        return "unknown", {"reason": "opencode-form-type-unsupported"}
    reply_path = f"/api/session/{session_path}/form/{request_path}/reply"
    result = _opencode_http(endpoint, "POST", reply_path,
                            {"answer": {envelope["question_id"]: answer_value}})
    if result.get("status") != 204:
        return "unknown", {"reason": "opencode-form-response-ambiguous"}
    confirmation = _opencode_http(endpoint, "GET",
                                  f"/api/session/{session_path}/form/{request_path}")
    detail = _opencode_data(confirmation)
    if not _opencode_identity_matches(endpoint, base["session_id"]):
        return "unknown", {"reason": "opencode-server-replaced-after-response"}
    if (confirmation.get("status") == 200 and isinstance(detail, dict)
            and detail.get("id") == envelope["request_id"]
            and detail.get("sessionID") == base["session_id"]
            and isinstance(detail.get("state"), dict)
            and detail["state"].get("status") == "answered"):
        return "applied", {"transport": "received", "runtime": "applied", "http_status": 204}
    if (confirmation.get("status") == 200 and isinstance(detail, dict)
            and isinstance(detail.get("state"), dict)
            and detail["state"].get("status") == "cancelled"):
        return "cancelled", {"reason": "opencode-form-cancelled"}
    return "received", {"transport": "received", "http_status": 204}


def _codex_submit(envelope, portable, action_id):
    base, error = _selected_base(envelope)
    if base is None:
        return "unknown", {"reason": error}
    native_ref = envelope.get("native_ref")
    if (not isinstance(native_ref, dict) or native_ref.get("transport") != "codex-managed-control"
            or not isinstance(native_ref.get("request"), dict)):
        return "unknown", {"reason": "codex-native-reference-required"}
    current = _codex_current_selected(base, requested_id=envelope.get("request_id"))
    if (current.get("state") != "open" or not current.get("support")
            or current.get("native_ref") != native_ref
            or current.get("request_id") != envelope.get("request_id")
            or current.get("turn_id") != envelope.get("turn_id")):
        return "unknown", {"reason": current.get("reason") or "codex-request-binding-changed"}
    gateway = current.get("gateway")
    if (not isinstance(gateway, dict) or gateway.get("pid") != native_ref.get("pid")
            or gateway.get("proc_start") != native_ref.get("proc_start")
            or gateway.get("path") != native_ref.get("control_path")
            or gateway.get("epoch") != native_ref.get("epoch")):
        return "unknown", {"reason": "codex-gateway-incarnation-changed"}
    result = _codex_control_request(gateway["path"], {
        "schema_version": 1, "op": "interaction-reply",
        "ref": native_ref["request"], "response": portable, "action_id": action_id})
    if (not result or result.get("action_id") != action_id
            or result.get("request") != native_ref["request"]):
        return "unknown", {"reason": "codex-response-result-ambiguous"}
    state = result.get("status")
    if state not in _SELECTED_STATES:
        return "unknown", {"reason": "codex-response-status-unknown"}
    response_result = {"action_id": action_id, "transport": state}
    if isinstance(result.get("reason"), str):
        response_result["reason"] = result["reason"]
    return state, response_result


def submit_selected_response(ref, response, *, action_id=None):
    """Submit one explicit response to the exact live request; ambiguous delivery never retries."""
    base, reason = _selected_base(ref)
    if base is None:
        return _selected_fallback(None, reason)
    if isinstance(ref, dict) and ref.get("state") in {"applied", "cancelled", "expired"}:
        return dict(ref)
    identity = _selected_identity(ref) if isinstance(ref, dict) else None
    if (identity is None or identity.get("harness") != base["harness"]
            or identity.get("session_id") != base["session_id"]
            or identity.get("route_id") != base.get("route_id")
            or identity.get("turn_id") is None):
        return _selected_fallback(base, "exact-request-turn-binding-required")
    current = read_selected_request(ref)
    current_identity = _selected_identity(current)
    if (current.get("state") != "open" or current.get("support", {}).get("supported") is not True
            or current_identity != identity
            or current.get("native_ref") != ref.get("native_ref")):
        return current
    portable, response_error = _portable_response(current, response)
    if portable is None:
        return _selected_invalid_response(current, response_error)
    if current["kind"] == "approval" and portable["decision"] == "defer":
        prior = _selected_entry(current)
        entry = prior or {"draft": current.get("draft", ""), "state": "open"}
        entry["response"] = {"decision": "defer"}
        entry["result"] = {"decision": "defer", "status": "no-response-sent"}
        entry["state"] = "open"
        if not _store_selected_entry(current, entry):
            return _selected_fallback(base, "selected-response-storage-unavailable")
        returned = dict(current)
        returned["state"] = "open"
        returned["result"] = entry["result"]
        return returned
    if action_id is None:
        action_id = uuid.uuid4().hex
    if not isinstance(action_id, str) or not _SAFE.fullmatch(action_id):
        return _selected_invalid_response(current, "action-id-invalid")
    response_error = _native_response_error(current, portable)
    if response_error:
        return _selected_invalid_response(current, response_error)
    claimed, entry = _claim_selected_action(current, action_id, portable)
    if not claimed:
        return read_selected_request(current)
    if current["harness"] == "opencode":
        state, result = _opencode_submit(current, portable)
    elif current["harness"] == "codex":
        state, result = _codex_submit(current, portable, action_id)
    else:
        state, result = "unknown", {"reason": "claude-native-response-surface-unavailable"}
    entry["state"] = state
    entry["result"] = result
    if state == "applied":
        entry["draft"] = ""
    if not _store_selected_entry(current, entry):
        state, result = "unknown", {"reason": "response-result-storage-unavailable"}
    returned = dict(current)
    returned.update({"state": state, "action_id": action_id, "result": result,
                     "actions": [] if state != "open" else current.get("actions", [])})
    if state == "applied":
        returned["draft"] = ""
    return returned


def cached_count(harness: str, session_id: str, *, now: float | None = None,
                 max_age: float = _DISPLAY_TTL) -> tuple[int, int] | None:
    """Return (global current total, exact session count) from a display-valid cache."""
    value = _read_cache(now=now, max_age=max_age)
    if value is None:
        if max_age == _DISPLAY_TTL:
            _request_producer()
        return None
    current = time.time() if now is None else float(now)
    if max_age == _DISPLAY_TTL and current - float(value["observed_at"]) > _REQUEST_TTL:
        _request_producer()
    key = str(harness) + "\0" + str(session_id)
    counts = value.get("by_session") if isinstance(value.get("by_session"), dict) else {}
    key = str(harness) + "\0" + str(session_id)
    total = int(value.get("total", 0)) if value.get("complete") else -1
    return total, len(counts.get(key, ()))


def _request_producer():
    root = state_root()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_meta = os.lstat(root)
        if not stat.S_ISDIR(root_meta.st_mode) or root_meta.st_uid != os.getuid():
            return
        os.chmod(root, 0o700)
        marker = root / ".demand"
        demand_lock = os.open(root / ".demand.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(demand_lock, 0o600)
            try:
                fcntl.flock(demand_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                age = time.time() - marker.stat().st_mtime
            except OSError:
                age = _REQUEST_TTL
            if age >= _REQUEST_TTL:
                marker.touch(exist_ok=True)
        finally:
            os.close(demand_lock)
        now = time.time()
        cache = _read_cache(now=now)
        fresh = cache is not None and now - float(cache["observed_at"]) <= _REQUEST_TTL
        if not fresh:
            _ensure_producer()
    except OSError:
        return


def _ensure_producer():
    root = state_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_meta = os.lstat(root)
    if not stat.S_ISDIR(root_meta.st_mode) or root_meta.st_uid != os.getuid():
        return False
    os.chmod(root, 0o700)
    fd = os.open(root / ".projection.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--refresh-cache", "--lock-fd", str(fd)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, pass_fds=(fd,),
                         start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        os.close(fd)
        return False
    os.close(fd)
    return True


def _refresh_cache() -> bool:
    """Run one observational refresh. Caller owns the single-flight lock."""
    try:
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import dispatch
        import human_gate_observation
        result = human_gate_observation.read_answer_waits(
            jobs_paths=dispatch.human_gate_registry_paths())
        if not publish_cache(result):
            failure = {"items": [], "complete": False, "observed_at": time.time(),
                       "health": [{"source": "producer", "state": "unknown"}]}
            publish_cache(failure)
            return False
        _notify_result(result)
        return True
    except Exception:
        publish_cache({"items": [], "complete": False, "observed_at": time.time(),
                       "health": [{"source": "producer", "state": "unknown"}]})
        return False


def _claim_notification(item_id: str, boundary: str):
    digest = hashlib.sha256((item_id + "\0" + boundary).encode()).hexdigest()
    root = state_root() / "notifications"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path = root / (digest + ".sent")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        return None
    return path, fd


def _notify_item(item: dict, *, boundary: str, sound: bool = False) -> bool:
    executable = shutil.which("herdr")
    if not executable:
        return False
    claim = None
    try:
        claim = _claim_notification(str(item.get("id") or ""), boundary)
        if claim is None:
            return True
        marker, fd = claim
        os.write(fd, (boundary + "\n").encode())
        os.fchmod(fd, 0o600)
        os.close(fd)
        claim = (marker, None)
        question = _clean(item.get("question") or "사용자 답 대기")
        age = max(0, int((time.time() - float(item.get("since") or time.time())) // 3600))
        title = "◑답변 필요" + (" · %dh" % age if boundary in {"1h", "6h"} else "")
        command = [executable, "notification", "show", title, "--body", question or "사용자 답 대기"]
        if sound:
            command.extend(("--sound", "request"))
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
        if result.returncode == 0:
            return True
        marker.unlink(missing_ok=True)
        return False
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        if claim is not None:
            try:
                if claim[1] is not None:
                    os.close(claim[1])
            except OSError:
                pass
            claim[0].unlink(missing_ok=True)
        return False


def _notify_result(result: dict) -> None:
    now = float(result.get("observed_at") or time.time())
    for item in result.get("items", ()):
        if (not isinstance(item, dict) or item.get("stale")
                or now - float(item.get("since") or now) >= 72 * 3600
                   and item.get("source") in {"S2", "S3"}
                or now - float(item.get("since") or now) >= 7 * 86400):
            continue
        _notify_item(item, boundary="registered")
        age = now - float(item.get("since") or now)
        if age >= 6 * 3600:
            _notify_item(item, boundary="1h")  # skipped boundary prevents a delayed replay
            _notify_item(item, boundary="6h", sound=True)
        elif age >= 3600:
            _notify_item(item, boundary="1h", sound=True)


def _notification_delay(items, now, *, enabled):
    """Seconds to the next 1h/6h boundary, or None when no producer is needed."""
    if not enabled:
        return None
    deadlines = []
    for item in items:
        if not isinstance(item, dict) or item.get("stale"):
            continue
        try:
            since = float(item["since"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        maximum = 72 * 3600 if item.get("source") in {"S2", "S3"} else 7 * 86400
        age = now - since
        if age < 0 or age >= maximum:
            continue
        for boundary in (3600, 6 * 3600):
            remaining = since + boundary - now
            if remaining > 0:
                deadlines.append(remaining)
    return min(deadlines) if deadlines else None


def _producer_loop(fd: int) -> None:
    root = state_root()
    try:
        _refresh_cache()
        while True:
            now = time.time()
            try:
                demand = (root / ".demand").stat().st_mtime
            except OSError:
                demand = 0
            cached = _read_cache(now=now, max_age=float("inf")) or {}
            boundary = _notification_delay(cached.get("items", ()), now,
                                           enabled=bool(shutil.which("herdr")))
            idle = now - demand
            if idle >= 600 and boundary is None:
                try:
                    latest_demand = (root / ".demand").stat().st_mtime
                except OSError:
                    latest_demand = 0
                if latest_demand <= demand and time.time() - latest_demand >= 600:
                    return
                continue
            if idle >= 600 and boundary is not None:
                # With no live display demand, a notification is the only
                # reason to retain the producer. Sleep until that boundary;
                # the 30 second refresh cadence is for live demand only.
                delay = max(1.0, boundary)
            else:
                delay = min(30.0, max(1.0, 600.0 - idle))
                if boundary is not None:
                    delay = min(delay, max(1.0, boundary))
            time.sleep(delay)
            _refresh_cache()
    except Exception:
        return
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--lock-fd", type=int)
    parser.add_argument("--count", nargs=2, metavar=("HARNESS", "SESSION"))
    args = parser.parse_args(argv)
    if args.refresh_cache:
        if args.lock_fd is not None:
            _producer_loop(args.lock_fd)
        else:
            # Direct internal invocation participates in the same single flight.
            try:
                root = state_root()
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(root / ".projection.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _producer_loop(fd)
            except (OSError, BlockingIOError):
                pass
        return 0
    if args.count:
        value = cached_count(*args.count)
        if value is not None:
            print(("?" if value[0] < 0 else str(value[0])) + " " + str(value[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
