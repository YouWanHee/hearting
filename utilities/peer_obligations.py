#!/usr/bin/env python3
"""Durable task-scoped peer follow-through and shared pane readiness inputs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Iterable

from dispatch_attempt_policy import (
    PaneObservation,
    RegisteredWorkObservation,
    completion_readiness,
    decide_attempt,
)

SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,160}\Z")
_MAX_RECORD_BYTES = 128 * 1024
_PENDING_STATES = frozenset({"pending", "unknown", "delivery-pending", "cleanup-pending"})
_DUTY_KINDS = frozenset({"watch", "message", "retire", "registered-batch"})


class ObligationError(ValueError):
    pass


def peer_state_root() -> Path:
    # Lazy import keeps the shared store independent of peer-message's CLI load.
    import importlib.util

    path = Path(__file__).with_name("peer-message.py")
    spec = importlib.util.spec_from_file_location("_peer_obligations_message", path)
    if spec is None or spec.loader is None:
        raise ObligationError("peer-state-root-unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return Path(module.peer_state_root()).resolve()


def obligations_root(root: str | Path | None = None) -> Path:
    base = Path(root).expanduser().resolve() if root is not None else peer_state_root()
    if base.is_symlink():
        raise ObligationError("peer-state-root-symlink")
    directory = base / "peer-steward" / "obligations"
    if directory.exists() and directory.is_symlink():
        raise ObligationError("peer-obligations-root-symlink")
    return directory


def _safe_id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ObligationError("peer-obligation-id-invalid")
    return value


def _read_record(path: Path) -> dict | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(_MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            raise ObligationError("peer-obligation-record-oversized")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, ObligationError):
            raise
        raise ObligationError("peer-obligation-record-invalid") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or not isinstance(value.get("id"), str) or not isinstance(value.get("intent"), dict)):
        raise ObligationError("peer-obligation-record-invalid")
    return value


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ObligationError("peer-obligation-path-symlink")
    fd, temporary_name = tempfile.mkstemp(prefix=".obligation-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class ObligationStore:
    """Atomic intent records; immutable intent is separate from mutable progress."""

    def __init__(self, root: str | Path | None = None):
        self.root = obligations_root(root)

    def _record_path(self, duty_id: str) -> Path:
        return self.root / (_safe_id(duty_id) + ".json")

    def _lock(self, duty_id: str):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / (_safe_id(duty_id) + ".lock")
        if path.is_symlink():
            raise ObligationError("peer-obligation-lock-symlink")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def get(self, duty_id: str) -> dict | None:
        return _read_record(self._record_path(duty_id))

    def list(self, *, states: Iterable[str] = _PENDING_STATES) -> list[dict]:
        if not self.root.is_dir():
            return []
        wanted = set(states)
        output = []
        for path in sorted(self.root.glob("*.json")):
            record = _read_record(path)
            if record is not None and record.get("state") in wanted:
                output.append(record)
        return output

    def create(self, duty_id: str, kind: str, identity: dict, intent: dict) -> dict:
        duty_id = _safe_id(duty_id)
        if kind not in _DUTY_KINDS or not isinstance(identity, dict) or not isinstance(intent, dict):
            raise ObligationError("peer-obligation-intent-invalid")
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            current = _read_record(path)
            immutable = {"kind": kind, "identity": identity, **intent}
            if current is not None:
                if current.get("intent") != immutable:
                    raise ObligationError("peer-obligation-intent-conflict")
                return current
            record = {"schema_version": SCHEMA_VERSION, "id": duty_id,
                      "intent": immutable, "state": "pending", "revision": 1,
                      "accepted_at": time.time(), "updated_at": time.time(),
                      "observation": None, "result": None, "delivery": "pending",
                      "cleanup": "pending"}
            _write_atomic(path, record)
            return record
        finally:
            os.close(fd)

    def update(self, duty_id: str, *, state: str | None = None,
               observation: dict | None = None, result: str | None = None,
               delivery: str | None = None, cleanup: str | None = None) -> dict:
        if state is not None and state not in _PENDING_STATES | {"complete", "cancelled"}:
            raise ObligationError("peer-obligation-state-invalid")
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            record = _read_record(path)
            if record is None:
                raise ObligationError("peer-obligation-missing")
            if result is not None and record.get("result") not in {None, result}:
                raise ObligationError("peer-obligation-result-conflict")
            if state is not None:
                record["state"] = state
            if observation is not None:
                record["observation"] = observation
            if result is not None:
                record["result"] = result
            if delivery is not None:
                record["delivery"] = delivery
            if cleanup is not None:
                record["cleanup"] = cleanup
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = time.time()
            _write_atomic(path, record)
            return record
        finally:
            os.close(fd)

    def claim_phase(self, duty_id: str, expected: set[str], new_phase: str,
                    *, state: str = "pending", extra: dict | None = None) -> dict | None:
        """Publish one side-effect phase exactly once under the duty lock."""
        if state not in _PENDING_STATES | {"complete", "cancelled"}:
            raise ObligationError("peer-obligation-state-invalid")
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            record = _read_record(path)
            if record is None:
                return None
            observation = record.get("observation") or {}
            if observation.get("phase", "waiting") not in expected:
                return None
            observation = {**observation, **(extra or {}), "phase": new_phase}
            record["observation"] = observation
            record["state"] = state
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = time.time()
            _write_atomic(path, record)
            return record
        finally:
            os.close(fd)


def stable_duty_id(kind: str, identity: dict, discriminator: str = "") -> str:
    if kind not in _DUTY_KINDS:
        raise ObligationError("peer-obligation-kind-invalid")
    raw = json.dumps([kind, identity, discriminator], sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return kind + "-" + hashlib.sha256(raw).hexdigest()[:32]


def ensure_runner(root: str | Path | None = None) -> bool:
    """Start the existing peer-steward task runner once for actual open duties."""
    store = ObligationStore(root)
    if not store.list():
        return False
    lock_path = store.root / "runner.lock"
    store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise ObligationError("peer-obligation-runner-lock-symlink")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        os.set_inheritable(fd, True)
        runner = Path(__file__).with_name("peer-steward.py")
        subprocess.Popen(
            [sys.executable, str(runner), "__obligation-runner", "--lock-fd", str(fd),
             "--state-root", str(Path(root).resolve()) if root else str(peer_state_root())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, pass_fds=(fd,), start_new_session=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        os.close(fd)


def pane_readiness(
    *,
    server: str,
    pane: str,
    harness: str,
    session_id: str,
    pid_birth: str,
    identity_verified: bool,
    native_turn: str,
    form_state: str = "unknown",
    draft_state: str = "unknown",
    bound_work: tuple[RegisteredWorkObservation, ...] = (),
    bindings_state: str = "observed",
    provenance: tuple[tuple[str, str], ...] = (),
):
    observation = PaneObservation(
        server=server, pane=pane, harness=harness, session_id=session_id,
        pid_birth=pid_birth, identity_verified=identity_verified,
        native_turn=native_turn, form_state=form_state, draft_state=draft_state,
        bound_work=bound_work, bindings_state=bindings_state, provenance=provenance,
    )
    return completion_readiness(observation)


def bound_work_for_pane(pane: str, harness: str, session_id: str, *,
                        jobs: str | Path | None = None) -> tuple[tuple[RegisteredWorkObservation, ...], str]:
    """Read exact session/pane-owned attempts; empty is valid only after a successful scan."""
    from dispatch_contract import (
        observed_attempt_liveness,
        parse_registry_metadata,
        resolve_agent_home,
        resolve_dispatch_state_root,
    )
    from dispatch_terminal_commit import owner_completion_state

    if jobs is None:
        state_root = resolve_dispatch_state_root(resolve_agent_home())
        jobs_path = state_root / "jobs.log"
    else:
        jobs_path = Path(jobs).expanduser().resolve()
        state_root = jobs_path.parent
    try:
        lines = jobs_path.read_text(encoding="utf-8").splitlines() if jobs_path.exists() else []
    except OSError:
        return (), "unknown"
    result: list[RegisteredWorkObservation] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            return (), "unknown"
        status = fields[1]
        metadata = parse_registry_metadata(fields[5])
        if (metadata.get("parent_sid") != session_id
                or metadata.get("parent_pane") != pane
                or metadata.get("parent_harness") not in {None, "", harness}):
            continue
        attempt_id = metadata.get("attempt_id", "")
        if not attempt_id:
            return (), "unknown"
        live = observed_attempt_liveness(status, metadata)
        decision = decide_attempt(
            status, metadata, process_state=live.process_state,
            process_reason=live.process_reason,
            terminal_observed=live.state == "reconcile-needed",
        )
        try:
            closure = owner_completion_state(jobs_path, status, metadata).state
        except Exception:
            closure = "unknown"
        closure_state = ("settled" if closure in {"not-applicable", "complete"}
                         else "unknown" if closure == "unknown" else "pending")
        result.append(RegisteredWorkObservation(
            attempt_id=attempt_id, decision=decision,
            process_state=live.process_state,
            evidence_state=metadata.get("receipt_state", metadata.get("note", "unclassified")),
            owner_completion_state=closure_state,
            cleanup_state=("pending" if metadata.get("cleanup_pending") == "1" else "settled"),
            provenance=(("jobs", str(jobs_path)), ("registry_status", status),
                        ("registry_state_root", str(state_root))),
        ))
    return tuple(result), "observed"
