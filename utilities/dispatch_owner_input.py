"""Durable owner input, consumed only by the existing execution supervisor.

This module owns admission and delivery receipts, never execution, retry or
completion policy. Files are private and serialized independently of jobs.lock.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from dispatch_completion_join import exact_attempt_row
from dispatch_contract import supervisor_lease_is_held, supervisor_lease_path


class InputError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _target(jobs, attempt):
    row = exact_attempt_row(Path(jobs), attempt)
    meta = row.metadata
    if meta.get("worker_type") != "owner" or meta.get("dispatch_depth") != "1":
        raise InputError("correction-target-not-owner")
    keys = ("attempt_id", "harness", "owner_route_id", "owner_route_hash",
            "route_id", "route_hash", "parent_sid", "supervisor_lease_nonce")
    return row, _digest({key: meta.get(key, "") for key in keys})


def _path(jobs, attempt):
    return supervisor_lease_path(jobs, attempt).with_suffix(".input.json")


@contextmanager
def _locked(jobs, attempt, *, create=False):
    path = _path(jobs, attempt)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.parent.exists():
        raise InputError("owner-input-unsupported")
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.is_symlink():
            raise InputError("owner-input-state-unsafe")
        value = json.loads(path.read_text()) if path.exists() else None
        if value is not None and (value.get("schema_version") != 1 or
                                  value.get("attempt_id") != attempt):
            raise InputError("owner-input-state-invalid")
        yield path, value
    finally:
        os.close(fd)


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix=".owner-input-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _transition(item, state, **evidence):
    item.update(state=state, **evidence)
    item["events"].append({"state": state, "time": time.time(), **evidence})


def _public(value, *, live=False):
    return {"attempt_id": value["attempt_id"], "thread_id": value["thread_id"],
            "transport": value["transport"], "accepting": value["accepting"] and live,
            "supervisor_live": live,
            "requests": [{key: item[key] for key in item if key != "text"}
                         for item in value["requests"]],
            "applied": "not-verified",
            "next_step": "Inspect delivery receipts; do not replay an unknown send or restart the owner."}


def inspect(jobs, attempt):
    row, target = _target(jobs, attempt)
    with _locked(jobs, attempt) as (_, value):
        if value is None:
            raise InputError("owner-input-unsupported")
        if value["target"] != target:
            raise InputError("owner-input-target-changed")
        live = supervisor_lease_is_held(jobs, row.metadata)
        result = _public(value, live=live)
        for item in result["requests"]:
            item["delivery_observation"] = (
                "delivery-unknown" if not live and item["state"] == "sending" else
                "undelivered" if not live and item["state"] == "queued" else item["state"])
        if not live:
            # Observation does not rewrite uncertain transport history.
            result["next_step"] = "Supervisor unavailable; retain the correction and inspect its exact receipt before recovery."
        return result


def submit(jobs, attempt, text, request_id=None):
    if not text.strip() or len(text.encode()) > 65536:
        raise InputError("correction-text-empty-or-oversized")
    row, target = _target(jobs, attempt)
    digest = _digest(text)
    request_id = request_id or "input-" + _digest([attempt, digest])[:32]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise InputError("correction-id-invalid")
    with _locked(jobs, attempt) as (path, value):
        if value is None:
            raise InputError("owner-input-unsupported")
        if value["target"] != target:
            raise InputError("owner-input-target-changed")
        for item in value["requests"]:
            if item["id"] == request_id:
                if item["digest"] != digest:
                    raise InputError("correction-id-content-conflict")
                return {**_public(value, live=supervisor_lease_is_held(jobs, row.metadata)),
                        "request_id": request_id, "duplicate": True}
        if (not value["accepting"] or row.status not in {"open", "running"}
                or not supervisor_lease_is_held(jobs, row.metadata)):
            raise InputError("owner-input-unavailable-retain-correction")
        item = {"id": request_id, "digest": digest, "text": text, "events": [],
                "source_session": os.environ.get("CODEX_THREAD_ID") or
                os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("OPENCODE_SESSION_ID") or "operator"}
        _transition(item, "queued")
        value["requests"].append(item)
        _write(path, value)
        return {**_public(value, live=True), "request_id": request_id, "duplicate": False}


class OwnerInput:
    def __init__(self, jobs, attempt, thread_id, transport, emit):
        self.jobs, self.attempt, self.emit = Path(jobs), attempt, emit
        self.generation = uuid.uuid4().hex
        _, target = _target(jobs, attempt)
        with _locked(jobs, attempt, create=True) as (path, value):
            if value is not None and value["target"] != target:
                raise InputError("owner-input-target-changed")
            value = value or {"schema_version": 1, "attempt_id": attempt,
                              "target": target, "requests": []}
            for item in value["requests"]:
                if item["state"] == "sending":
                    _transition(item, "delivery-unknown", reason="supervisor-restarted")
                elif item["state"] == "queued" and value.get("thread_id") != thread_id:
                    _transition(item, "undelivered", reason="owner-thread-changed")
            value.update(thread_id=thread_id, transport=transport, accepting=True,
                         generation=self.generation)
            _write(path, value)
        self.thread_id = thread_id
        self.active_turn = ""
        self.prepared = []
        self.inflight = {}
        self.next_poll = 0.0
        self.notify_unresolved()

    @contextmanager
    def state(self):
        with _locked(self.jobs, self.attempt) as (path, value):
            if value is None or value["generation"] != self.generation:
                raise InputError("owner-input-consumer-changed")
            yield path, value

    def reopen(self):
        with self.state() as (path, value):
            value["accepting"] = True
            _write(path, value)

    def bind_initial_thread(self, thread):
        if not thread or thread == self.thread_id:
            return
        if self.thread_id != "pending-native-session":
            raise InputError("owner-input-thread-changed")
        with self.state() as (path, value):
            value["thread_id"] = thread
            _write(path, value)
        self.thread_id = thread

    def pending(self):
        with self.state() as (_, value):
            return any(item["state"] == "queued" for item in value["requests"])

    def take(self, mode):
        with self.state() as (path, value):
            items = [item for item in value["requests"] if item["state"] == "queued"]
            for item in items:
                _transition(item, "sending", mode=mode, thread_id=self.thread_id)
            if items:
                _write(path, value)
            return items

    @staticmethod
    def text(items):
        if not items:
            return ""
        return ("\n<owner-corrections>\nUser corrections relayed by the parent/operator. "
                "Acknowledge what changes in the current work and preserve valid prior work; "
                "do not duplicate active children, forge completed gates, or widen permissions.\n" +
                "\n".join(json.dumps({"id": i["id"], "text": i["text"]}, ensure_ascii=False)
                          for i in items) + "\n</owner-corrections>")

    def prepare(self, prompt):
        items = self.take("next-turn")
        self.prepared = [i["id"] for i in items]
        return prompt + self.text(items)

    def record(self, ids, state, **evidence):
        if not ids:
            return
        with self.state() as (path, value):
            for item in value["requests"]:
                if item["id"] in ids:
                    _transition(item, state, **evidence)
            _write(path, value)
        self.emit({"type": "dispatch.supervisor.input", "attempt_id": self.attempt,
                   "request_ids": ids, "state": state, **evidence})

    def started(self, turn):
        self.active_turn = turn
        self.record(self.prepared, "accepted", thread_id=self.thread_id, turn_id=turn)
        self.prepared = []

    def completed(self, turn):
        with self.state() as (_, value):
            ids = [i["id"] for i in value["requests"]
                   if i["state"] == "accepted" and i.get("turn_id") == turn]
        self.record(ids, "turn-completed", turn_id=turn)
        self.active_turn = ""

    def tick(self, server):
        if not self.active_turn or self.inflight or time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + 0.2
        items = self.take("active-turn")
        if not items:
            return
        request = server.next_id
        server.next_id += 1
        self.inflight[request] = ([i["id"] for i in items], self.active_turn)
        server.send({"jsonrpc": "2.0", "id": request, "method": "turn/steer",
                     "params": {"threadId": self.thread_id, "expectedTurnId": self.active_turn,
                                "input": [{"type": "text", "text": self.text(items)}]}})

    def response(self, value):
        if value.get("id") not in self.inflight:
            return False
        ids, turn = self.inflight.pop(value["id"])
        if "error" in value:
            # Explicit rejection proves no delivery; retain for the next turn.
            self.record(ids, "queued", reason="native-steer-rejected")
            self.active_turn = ""
        elif value.get("result", {}).get("turnId") == turn:
            self.record(ids, "accepted", thread_id=self.thread_id, turn_id=turn)
        else:
            self.record(ids, "delivery-unknown", reason="native-steer-receipt-mismatch")
        return True

    def terminal_boundary(self):
        """Serialize last-input admission with closure, without rewriting results."""
        with self.state() as (path, value):
            if any(i["state"] == "queued" for i in value["requests"]):
                return False
            value["accepting"] = False
            _write(path, value)
            return True

    def close(self):
        try:
            with self.state() as (path, value):
                value["accepting"] = False
                for item in value["requests"]:
                    if item["state"] in {"queued", "sending"}:
                        _transition(item, "undelivered" if item["state"] == "queued" else "delivery-unknown",
                                    reason="supervisor-stopped")
                _write(path, value)
        except Exception as exc:
            # A receipt persistence fault cannot reverse committed execution.
            self.emit({"type": "dispatch.supervisor.input-notice-pending", "attempt_id": self.attempt,
                       "reason": type(exc).__name__})
        self.notify_unresolved()

    def notify_unresolved(self):
        materialize_unresolved(self.jobs, self.attempt)


def unresolved_revision(jobs, attempt):
    path = _path(jobs, attempt)
    if not path.exists() or path.is_symlink():
        return ""
    row, target = _target(jobs, attempt)
    live = supervisor_lease_is_held(jobs, row.metadata)
    with _locked(jobs, attempt) as (_, value):
        if value is None or value["target"] != target:
            return ""
        items = [(i["id"], i["state"]) for i in value["requests"]
                 if i["state"] in {"delivery-unknown", "undelivered"}
                 or (not live and i["state"] in {"queued", "sending"})]
        return _digest(items) if items else ""


def unresolved(jobs, attempt):
    return bool(unresolved_revision(jobs, attempt))


def materialize_unresolved(jobs, attempt):
    """Reuse terminal-close and reconcile drivers after a supervisor crash."""
    try:
        if unresolved(jobs, attempt):
            from dispatch_supervision import materialize
            materialize(Path(jobs), {attempt}, reason="owner-input-undelivered")
    except Exception as exc:
        import sys
        sys.stderr.write(f"owner-input-notice-pending attempt_id={attempt} reason={type(exc).__name__}\n")
