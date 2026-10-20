#!/usr/bin/env python3
"""Small, process-shared readiness projections owned by the existing runner."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time


SCHEMA_VERSION = 1
DUTY_KIND = "readiness-observation"
MAX_AGE_SECONDS = 15.0
MAX_TARGET_PROJECTIONS = 256
RUNNER_WAKE_NAME = "readiness-wake.fifo"
_CHILD_FIELDS = ("attempt_id", "slug", "status", "readiness", "reason",
                 "required_action")
_WATCH_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def attempt_identity(jobs, attempt_id, *, parent="", slug="", consumer_pid,
                     consumer_start, request_token=""):
    """Bind one read request to an exact selection and its live consumer."""
    path = Path(jobs).expanduser().resolve(strict=False)
    return {
        "jobs": str(path),
        "selection": {"attempt_id": str(attempt_id), "parent": str(parent), "slug": str(slug)},
        "consumer": {"pid": int(consumer_pid), "pid_start": str(consumer_start)},
        "request_token": str(request_token),
    }


def duty_id(identity):
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "readiness-" + hashlib.sha256(encoded).hexdigest()


def target_identity(*, server, endpoint, pane, harness, session_id,
                    shell_pid, shell_start, native_state, purpose="completion"):
    try:
        pid = int(shell_pid)
    except (TypeError, ValueError):
        return None
    values = (server, endpoint, pane, harness, shell_start, native_state, purpose)
    if (pid <= 0 or any(not isinstance(value, str) or not value for value in values)
            or not isinstance(session_id, str) or native_state not in {"idle", "done"}):
        return None
    if pane == "-" or harness not in {"claude", "codex", "opencode"}:
        return None
    return {"server": server, "endpoint": endpoint, "pane": pane,
            "harness": harness, "session_id": session_id,
            "shell_pid": pid, "shell_start": shell_start,
            "native_state": native_state, "purpose": purpose}


def legacy_target_identity(*, server, endpoint, pane, harness, session_id,
                           native_state, purpose="completion"):
    """Key an accepted pre-birth watch until the runner proves current lineage.

    This identity deliberately contains no process birth. The runner must attach
    a current ``lineage`` to every projection, and consumers validate that
    lineage against the live endpoint before accepting ready.
    """
    values = (server, endpoint, pane, harness, native_state, purpose)
    if (any(not isinstance(value, str) or not value for value in values)
            or not isinstance(session_id, str)
            or pane == "-" or harness not in {"claude", "codex", "opencode"}
            or native_state not in {"idle", "done"}):
        return None
    return {"server": server, "endpoint": endpoint, "pane": pane,
            "harness": harness, "session_id": session_id,
            "shell_pid": None, "shell_start": "",
            "native_state": native_state, "purpose": purpose}


def _target_path(root, identity):
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    name = hashlib.sha256(encoded).hexdigest() + ".json"
    return Path(root) / "readiness-v1" / name


def _read_json(path):
    try:
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
        if len(raw) > 65536:
            return None
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise OSError("readiness-projection-path-symlink")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > 65536:
        raise ValueError("readiness-projection-too-large")
    fd, temporary = tempfile.mkstemp(prefix=".readiness-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def publish_runner_incarnation(root, pid, pid_start, incarnation):
    path = Path(root) / "readiness-v1" / "runner.json"
    _atomic_json(path, {"pid": int(pid), "pid_start": str(pid_start),
                        "incarnation": str(incarnation)})


def _runner_incarnation_current(root, producer):
    marker = _read_json(Path(root) / "readiness-v1" / "runner.json")
    return bool(marker and marker.get("pid") == producer.get("pid")
                and marker.get("pid_start") == producer.get("pid_start")
                and marker.get("incarnation") == producer.get("incarnation"))


def _runner_wake_path(root):
    directory = Path(root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise OSError("readiness-runner-root-symlink")
    path = directory / RUNNER_WAKE_NAME
    try:
        path.lstat()
    except FileNotFoundError:
        try:
            os.mkfifo(path, 0o600)
        except FileExistsError:
            pass
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISFIFO(info.st_mode):
        raise OSError("readiness-runner-wake-invalid")
    return path


def open_runner_wake(root):
    """Open the existing runner's nonblocking event input under its state root."""
    path = _runner_wake_path(root)
    return os.open(path, os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
                   | getattr(os, "O_NOFOLLOW", 0))


def wake_runner(root):
    """Wake a live existing runner after publishing a read demand or watch result."""
    try:
        path = _runner_wake_path(root)
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, TypeError, ValueError):
        return False
    try:
        return os.write(fd, b"\0") == 1
    except OSError:
        return False
    finally:
        os.close(fd)


def publish_target_projection(root, identity, result, *, producer_pid,
                              producer_start, incarnation, generation=None,
                              lineage=None, now=None):
    """Atomically publish readiness once for one exact pane/session/birth."""
    state = result.get("state", "unknown") if isinstance(result, dict) else "unknown"
    reason = result.get("reason", "") if isinstance(result, dict) else "invalid-readiness"
    if state not in {"ready", "pending", "unknown"}:
        state = "unknown"
    value = {"schema_version": SCHEMA_VERSION, "identity": identity,
             "producer": {"pid": int(producer_pid), "pid_start": str(producer_start),
                          "incarnation": str(incarnation)},
             "observed_at": time.time() if now is None else float(now),
             "generation": generation,
             "result": {"state": state, "reason": reason if isinstance(reason, str) else ""}}
    if isinstance(lineage, dict):
        try:
            pid = int(lineage.get("shell_pid"))
            start = str(lineage.get("shell_start") or "")
            if pid > 0 and start:
                value["lineage"] = {"shell_pid": pid, "shell_start": start}
        except (TypeError, ValueError):
            pass
    path = _target_path(root, identity)
    previous = _read_json(path)
    changed = not (previous and previous.get("identity") == identity
                   and previous.get("producer") == value["producer"]
                   and previous.get("generation") == generation
                   and previous.get("lineage") == value.get("lineage")
                   and previous.get("result") == value["result"])
    _atomic_json(path, value)
    projections = [candidate for candidate in path.parent.glob("[0-9a-f]*.json")
                   if len(candidate.name) == 69 and not candidate.is_symlink()]
    if len(projections) > MAX_TARGET_PROJECTIONS:
        projections.sort(key=lambda candidate: candidate.stat().st_mtime_ns)
        for stale in projections[:-MAX_TARGET_PROJECTIONS]:
            try:
                stale.unlink()
            except OSError:
                pass
    return changed


def _generation_current(generation):
    if not isinstance(generation, dict):
        return True
    path = generation.get("path")
    signature = generation.get("signature")
    if not isinstance(path, str) or not isinstance(signature, list) or len(signature) != 5:
        return False
    try:
        candidate = Path(path)
        resolved = str(candidate.resolve(strict=True))
        info = candidate.stat()
        current = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        return resolved == generation.get("resolved") and current == signature
    except (OSError, RuntimeError, ValueError):
        return False


def target_result(root, identity, *, now=None, max_age=MAX_AGE_SECONDS):
    """Read a current target projection; missing or stale data stays unknown."""
    if not isinstance(identity, dict):
        return None
    value = _read_json(_target_path(root, identity))
    if (not value or value.get("schema_version") != SCHEMA_VERSION
            or value.get("identity") != identity):
        return None
    producer = value.get("producer")
    if not isinstance(producer, dict) or not _runner_incarnation_current(root, producer):
        return None
    try:
        from dispatch_contract import process_start_ticks
        if str(process_start_ticks(int(producer.get("pid"))) or "") != str(producer.get("pid_start")):
            return None
        age = (time.time() if now is None else float(now)) - float(value.get("observed_at"))
    except (ImportError, OSError, TypeError, ValueError):
        return None
    if age < -60 or age > max_age or not _generation_current(value.get("generation")):
        return None
    result = value.get("result")
    if not isinstance(result, dict) or result.get("state") not in {"ready", "pending", "unknown"}:
        return None
    lineage = value.get("lineage")
    pid = identity.get("shell_pid")
    start = identity.get("shell_start")
    if pid is None:
        if not isinstance(lineage, dict):
            return None
        pid, start = lineage.get("shell_pid"), lineage.get("shell_start")
    try:
        from dispatch_contract import process_start_ticks
        if (int(pid) <= 0 or not start
                or str(process_start_ticks(int(pid)) or "") != str(start)):
            return None
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return {**result, **({"lineage": lineage} if isinstance(lineage, dict) else {})}


def _watch_transition_path(root, watch_id):
    if not isinstance(watch_id, str) or not _WATCH_ID.fullmatch(watch_id):
        raise ValueError("readiness-watch-id-invalid")
    return Path(root) / "readiness-v1" / "watch-transitions" / f"{watch_id}.json"


def publish_watch_transition(root, watch_id, identity, request_token, result, *,
                             producer_pid, producer_start, incarnation,
                             generation=None, lineage=None, now=None):
    """Publish the runner's fresh, request-bound authority check for one watch."""
    if (not isinstance(identity, dict) or not isinstance(request_token, str)
            or not request_token or len(request_token) > 128):
        raise ValueError("readiness-watch-transition-invalid")
    state = result.get("state", "unknown") if isinstance(result, dict) else "unknown"
    reason = result.get("reason", "") if isinstance(result, dict) else "invalid-readiness"
    if state not in {"ready", "pending", "unknown"}:
        state = "unknown"
    observed_at = time.time() if now is None else float(now)
    value = {
        "schema_version": SCHEMA_VERSION,
        "watch_id": watch_id,
        "request_token": request_token,
        "identity": identity,
        "producer": {"pid": int(producer_pid), "pid_start": str(producer_start),
                     "incarnation": str(incarnation)},
        "observed_at": observed_at,
        "generation": generation,
        "result": {"state": state, "reason": reason if isinstance(reason, str) else ""},
    }
    if isinstance(lineage, dict):
        try:
            pid = int(lineage.get("shell_pid"))
            start = str(lineage.get("shell_start") or "")
            if pid > 0 and start:
                value["lineage"] = {"shell_pid": pid, "shell_start": start}
        except (TypeError, ValueError):
            pass
    path = _watch_transition_path(root, watch_id)
    previous = _read_json(path)
    previous_age = None
    if previous:
        try:
            previous_age = observed_at - float(previous.get("observed_at"))
        except (TypeError, ValueError):
            pass
    changed = not (previous and previous_age is not None
                   and -60 <= previous_age <= MAX_AGE_SECONDS
                   and previous.get("watch_id") == watch_id
                   and previous.get("request_token") == request_token
                   and previous.get("identity") == identity
                   and previous.get("producer") == value["producer"]
                   and previous.get("generation") == generation
                   and previous.get("lineage") == value.get("lineage")
                   and previous.get("result") == value["result"])
    _atomic_json(path, value)
    return changed


def watch_transition_result(root, watch_id, identity, request_token, *,
                            now=None, max_age=MAX_AGE_SECONDS):
    """Read a current per-watch refresh response; mismatches remain unknown."""
    try:
        value = _read_json(_watch_transition_path(root, watch_id))
    except ValueError:
        return None
    if (not value or value.get("schema_version") != SCHEMA_VERSION
            or value.get("watch_id") != watch_id
            or value.get("request_token") != request_token
            or value.get("identity") != identity):
        return None
    producer = value.get("producer")
    if not isinstance(producer, dict) or not _runner_incarnation_current(root, producer):
        return None
    try:
        from dispatch_contract import process_start_ticks
        if str(process_start_ticks(int(producer.get("pid"))) or "") != str(producer.get("pid_start")):
            return None
        age = (time.time() if now is None else float(now)) - float(value.get("observed_at"))
    except (ImportError, OSError, TypeError, ValueError):
        return None
    if age < -60 or age > max_age or not _generation_current(value.get("generation")):
        return None
    result = value.get("result")
    lineage = value.get("lineage")
    if not isinstance(result, dict) or result.get("state") not in {"ready", "pending", "unknown"}:
        return None
    pid = identity.get("shell_pid")
    start = identity.get("shell_start")
    if pid is None:
        if not isinstance(lineage, dict):
            return None
        pid, start = lineage.get("shell_pid"), lineage.get("shell_start")
    try:
        from dispatch_contract import process_start_ticks
        if (int(pid) <= 0 or not start
                or str(process_start_ticks(int(pid)) or "") != str(start)):
            return None
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return {**result, **({"lineage": lineage} if isinstance(lineage, dict) else {})}


def clear_watch_transition(root, watch_id):
    """Drop the bounded response when its exact watch has settled."""
    try:
        _watch_transition_path(root, watch_id).unlink()
    except FileNotFoundError:
        pass


def consumer_is_current(identity):
    consumer = identity.get("consumer") if isinstance(identity, dict) else None
    if not isinstance(consumer, dict):
        return False
    try:
        pid = int(consumer.get("pid"))
        expected = str(consumer.get("pid_start") or "")
        if pid <= 0 or not expected:
            return False
        from dispatch_contract import process_start_ticks
        return str(process_start_ticks(pid) or "") == expected
    except (ImportError, OSError, TypeError, ValueError):
        return False


def make_projection(identity, result, *, producer_pid, producer_start, incarnation,
                    generation=None, now=None):
    """Keep only the small result needed by a caller; never persist full rows."""
    state = str(result.get("state", "unknown")) if isinstance(result, dict) else "unknown"
    reason = result.get("reason", "") if isinstance(result, dict) else "invalid-classification"
    children = result.get("children", ()) if isinstance(result, dict) else ()
    projected_children = []
    if isinstance(children, (list, tuple)):
        for child in children[:16]:
            if isinstance(child, dict):
                projected_children.append({key: child[key] for key in _CHILD_FIELDS
                                           if key in child and isinstance(child[key], (str, int, bool))})
    value = {
        "schema_version": SCHEMA_VERSION,
        "identity": identity,
        "producer": {"pid": int(producer_pid), "pid_start": str(producer_start),
                     "incarnation": str(incarnation)},
        "observed_at": time.time() if now is None else float(now),
        "generation": generation,
        "result": {"state": state, "reason": reason if isinstance(reason, str) else "",
                   "children": projected_children},
    }
    if len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")) > 65536:
        value["result"] = {"state": "unknown", "reason": "readiness-projection-too-large",
                           "children": []}
    return value


def projection_is_current(value, identity, *, now=None, max_age=MAX_AGE_SECONDS):
    """Reject stale or unverifiable producer results as unknown, never ready."""
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return False
    if value.get("identity") != identity or not consumer_is_current(identity):
        return False
    producer = value.get("producer")
    if not isinstance(producer, dict):
        return False
    jobs_root = Path(identity.get("jobs", "")).parent
    obligations = jobs_root / "peer-steward" / "obligations"
    if not _runner_incarnation_current(obligations, producer):
        return False
    try:
        pid = int(producer.get("pid"))
        expected = str(producer.get("pid_start") or "")
        if pid <= 0 or not expected or not producer.get("incarnation"):
            return False
        from dispatch_contract import process_start_ticks
        if str(process_start_ticks(pid) or "") != expected:
            return False
        stamp = float(value.get("observed_at"))
    except (ImportError, OSError, TypeError, ValueError):
        return False
    current = time.time() if now is None else float(now)
    age = current - stamp
    if age < -60 or age > max_age:
        return False
    result = value.get("result")
    return isinstance(result, dict) and result.get("state") in {
        "ready", "pending", "terminal", "contract-error", "unknown"
    }


def current_result(duty, identity, *, now=None, max_age=MAX_AGE_SECONDS):
    """Return a validated tiny result or ``None`` when the runner must reobserve."""
    if not isinstance(duty, dict):
        return None
    intent = duty.get("intent")
    if not isinstance(intent, dict) or intent.get("kind") != DUTY_KIND:
        return None
    if duty.get("id") != duty_id(identity) or intent.get("identity") != identity:
        return None
    observation = duty.get("observation")
    value = observation.get("shared_readiness") if isinstance(observation, dict) else None
    if not projection_is_current(value, identity, now=now, max_age=max_age):
        return None
    return value["result"]
