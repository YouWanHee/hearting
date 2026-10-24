"""Stable reference-state inventory for parity captures."""
from __future__ import annotations

import hashlib
import json

from tools.fleet.explorer.state import NavState


def enumerate_states(reference=None, vm=None):
    """Enumerate stable view/page/selection states from the current source identities.

    ``reference`` may be a frozen manifest or a mapping with a ``states`` list. Stable ids
    include the semantic selection key; ordering is deterministic and independent of hash order.
    """
    values = vm.to_mapping() if hasattr(vm, "to_mapping") else (vm or {})
    rows = []
    def add(state_id, state):
        rows.append((state_id, state))

    views = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE",
             "MEMORY", "MODELS", "SYSTEM")
    for view in views:
        add(view, NavState(view=view, title=view))
    repos = values.get("repos") or ()
    for repo in repos:
        rid = repo.get("id") or repo.get("name")
        if rid:
            add("repo:" + str(rid), NavState(scope="repo:" + str(rid), title=str(rid)))
    sessions = values.get("sessions") or ()
    for session in sessions:
        tag = session.get("tag")
        if tag:
            add("session:" + str(tag), NavState(scope="session:" + str(tag), title=str(tag)))
    routes = values.get("routes") or ()
    for route in routes:
        rid = route.get("id")
        if rid:
            add("route:" + str(rid), NavState(scope="route:" + str(rid), title=str(rid)))
            add("ROUTES-open:" + str(rid), NavState(view="ROUTES", title="ROUTES",
                folds=(("ropen:ROUTES", str(rid)),)))
    questions = values.get("questions") or ()
    for question in questions:
        tag = question.get("tag") if isinstance(question, dict) else None
        if tag:
            add("QUESTIONS-fold:" + str(tag), NavState(view="QUESTIONS", title="QUESTIONS",
                folds=(("qshut:QUESTIONS", str(tag)),)))
    for seat in values.get("seats") or ():
        key = seat[0] if isinstance(seat, (list, tuple)) and seat else None
        if key:
            add("HISTORY-open:" + str(key), NavState(view="HISTORY", title="HISTORY",
                folds=(("hopen:HISTORY", str(key)),)))
    peers = values.get("peer_nodes") or ()
    for _repo, columns in peers:
        for column in columns:
            for tag in column:
                add("PEERS-pick:" + str(tag), NavState(view="PEERS", title="PEERS",
                    folds=(("pfocus:PEERS", str(tag)),)))
    # A supplied manifest contributes state labels as coverage hints, not authority to remove
    # states derived from current live entities.
    manifest_states = reference.get("states", ()) if isinstance(reference, dict) else ()
    known = {key for key, _ in rows}
    for label in manifest_states:
        if label not in known:
            add("manifest:" + str(label), NavState(view="OVERVIEW", title=str(label)))
    unique = {key: state for key, state in rows}
    result = [(key, unique[key]) for key in sorted(unique)]
    fingerprint = hashlib.sha256(json.dumps([key for key, _ in result], separators=(",", ":")).encode()).hexdigest()
    return [{"id": key, "state": state, "fingerprint": fingerprint} for key, state in result]
