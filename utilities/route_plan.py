"""The framed route's decision record (`route_decision_v1`) and the framed-shape predicate.

A framed route (capability `route-frame`, shape `framed`) ends in one runtime terminal,
`route-decision`, that no model runs. The runtime fixes one record as that terminal's
evidence: an immutable `decision` part with its canonical digest, and a separate,
monotonic `first_leg` part that may be added once and never changed. The digest covers
the `decision` part only, so binding a first leg never invalidates the record.

This module has no CLI, no gate and no recovery command; it only builds, renders,
reads and checks records.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

SCHEMA = "route_decision_v1"
CAPABILITY = "route-frame"
SHAPE = "framed"
NODE_IDS = ("frame", "frame-alternative", "route-decision")
TERMINAL_NODE = "route-decision"
RECORD_RELATIVE = "shards/frame/route-decision.json"
MAX_RECORD_BYTES = 262144
NONE = "none"
NO_PROPOSAL_READ = "proposal-not-read"
_DECISION_KEYS = frozenset((
    "frame_route", "selected", "reason", "proposal", "proposals", "briefs", "intent",
    "approvals", "first_leg_compose"))
_FRAME_NODES = ("frame", "frame-alternative")


def is_framed_route(route) -> bool:
    """The exact framed route shape: nothing else may take the model-less terminal path."""
    if not isinstance(route, dict) or route.get("capability") != CAPABILITY:
        return False
    if (route.get("selection") or {}).get("shape") != SHAPE or route.get("effective_intensity") != "standard":
        return False
    nodes = route.get("nodes")
    return (isinstance(nodes, list) and tuple(n.get("id") for n in nodes) == NODE_IDS
            and nodes[-1].get("kind") == "runtime-terminal" and nodes[-1].get("terminal") is True)


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decision_digest(decision) -> str:
    """`sha256:<hex>` of the decision part's canonical bytes; `first_leg` is never part of it."""
    return "sha256:" + hashlib.sha256(_canonical(decision)).hexdigest()


def file_digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_decision(*, frame_route, selected, reason, briefs, intent, proposal=None, proposals=None,
                   approvals=None, first_leg_compose=None) -> dict:
    """The immutable `decision` part. `selected` is an option label or `none`."""
    if not isinstance(selected, str) or not selected:
        raise ValueError("route-decision-invalid:selected")
    if not isinstance(reason, str) or (selected == NONE and not reason):
        raise ValueError("route-decision-invalid:reason")
    return {
        "frame_route": dict(frame_route),
        "selected": selected,
        "reason": reason,
        "proposal": proposal,
        "proposals": proposals if proposals is not None else [],
        "briefs": [dict(item) for item in briefs],
        "intent": dict(intent),
        "approvals": approvals if approvals is not None else {},
        "first_leg_compose": first_leg_compose,
    }


def none_decision(*, frame_route, briefs, intent, reason=NO_PROPOSAL_READ) -> dict:
    """The minimal ending: no proposal was selected, so no leg starts and the main session composes next."""
    return build_decision(
        frame_route=frame_route, selected=NONE, reason=reason, briefs=briefs, intent=intent,
        proposals=[{"node": node, "proposal": None, "reason": reason} for node in _FRAME_NODES])


def build_record(decision, first_leg=None) -> dict:
    record = {"schema": SCHEMA, "decision": decision, "digest": decision_digest(decision)}
    if first_leg is not None:
        record["first_leg"] = first_leg
    return record


def render(record) -> bytes:
    """The record's exact file bytes; the same record always renders the same bytes."""
    return (json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def validate_record(record) -> dict:
    """Return `record` when it is a well-formed `route_decision_v1`; else raise ValueError."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise ValueError("route-decision-invalid:schema")
    if set(record) - {"schema", "decision", "digest", "first_leg"} or {"decision", "digest"} - set(record):
        raise ValueError("route-decision-invalid:fields")
    decision = record["decision"]
    if not isinstance(decision, dict) or set(decision) != _DECISION_KEYS:
        raise ValueError("route-decision-invalid:decision")
    if record["digest"] != decision_digest(decision):
        raise ValueError("route-decision-invalid:digest")
    frame_route = decision["frame_route"]
    if not isinstance(frame_route, dict) or set(frame_route) != {"route_id", "route_hash", "cycle_id"}:
        raise ValueError("route-decision-invalid:frame_route")
    if not isinstance(decision["selected"], str) or not decision["selected"]:
        raise ValueError("route-decision-invalid:selected")
    if decision["selected"] == NONE and (not decision["reason"] or decision["proposal"] is not None):
        raise ValueError("route-decision-invalid:none")
    if "first_leg" in record and (decision["selected"] == NONE or not isinstance(record["first_leg"], dict)):
        raise ValueError("route-decision-invalid:first_leg")
    return record


def read_record(path) -> dict:
    """Read and validate a record file; it must be a small regular, non-symlink file."""
    path = Path(path)
    try:
        meta = os.lstat(path)
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_RECORD_BYTES:
            raise ValueError("route-decision-invalid:file")
        return validate_record(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("route-decision-invalid:unreadable") from exc


def bind_first_leg(record, first_leg) -> dict:
    """Add the `first_leg` part once, or confirm the same value again; any other value conflicts.

    The decision part and its digest are never touched. S2 fills `first_leg`; a `none`
    decision has no first leg.
    """
    validate_record(record)
    if record["decision"]["selected"] == NONE:
        raise ValueError("route-decision-invalid:first_leg")
    current = record.get("first_leg")
    if current is None:
        return {**record, "first_leg": first_leg}
    merged = {**current, **first_leg}
    if any(current.get(key) != value for key, value in first_leg.items() if key in current):
        raise ValueError("route-decision-conflict:first_leg")
    return {**record, "first_leg": merged}
