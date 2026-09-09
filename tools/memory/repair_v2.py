#!/usr/bin/env python3
"""Pure plan/validate/build support for blocked-history operator recovery.

Implements the record-by-record ``put -> tombstone`` recovery pair described in
``core/MEMORY.md`` §7.2.2: for each affected record of a parentless blocked
``tombstone`` operation ``B``, one new put ``P_r`` reaffirms the transactional
graveyard's exact prior state and one new tombstone ``T_r`` (parented on
``P_r``) reaffirms the original deletion intent. Every new operation covers
exactly one record and carries exactly one mutation — that isolation is what
keeps a late concurrent change to one record from resurrecting another.

This module has no database, filesystem, Git, clock, or network dependency —
only ``hashlib``, ``json``, and ``protocol_v2``. Every check here is advisory
until the caller (``mem.py``) re-verifies inside the same ``BEGIN IMMEDIATE``
transaction that records the result.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

import protocol_v2

PLAN_SCHEMA = 1

_PLAN_TOP_FIELDS = frozenset({
    "schema", "reason", "created_utc", "store", "snapshot", "bind", "units",
    "counts", "plan_digest",
})
_PLAN_UNIT_FIELDS = frozenset({
    "blocked_op_id", "project_key", "record_id", "prior_digest",
    "tombstone_digest", "tombstone_action", "tombstone_pending",
})


class RepairRefusal(ValueError):
    """Typed, machine-readable refusal of a blocked-history repair step."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class Unit:
    """One record's recovery pair inputs (§1.4)."""

    __slots__ = ("blocked_op_id", "project_key", "record_id", "prior_state", "tombstone")

    def __init__(self, blocked_op_id: str, project_key: str, record_id: str,
                 prior_state: Mapping[str, Any], tombstone: Mapping[str, Any]) -> None:
        self.blocked_op_id = blocked_op_id
        self.project_key = project_key
        self.record_id = record_id
        self.prior_state = prior_state
        self.tombstone = tombstone

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"Unit(blocked_op_id={self.blocked_op_id!r}, record_id={self.record_id!r})"


def _digest(value: Any) -> str:
    return hashlib.sha256(protocol_v2.canonical_bytes(value)).hexdigest()


def normalize_observation(raw: Mapping[str, Any]) -> dict:
    """Validate and copy the caller-supplied pure observation mapping.

    ``raw`` is assembled by the caller (``mem.py``) from live/snapshot reads;
    this function only checks shape, never re-reads storage.
    """
    if not isinstance(raw, Mapping):
        raise RepairRefusal("observation-invalid", "observation must be a mapping")
    required = frozenset({
        "result", "operations", "blocked", "by_record", "graveyard", "store",
        "snapshot", "snapshot_targets", "live_targets", "snapshot_graveyard",
        "capture_frontier", "preserved_digest", "pending_ids",
    })
    missing = required - frozenset(raw)
    if missing:
        raise RepairRefusal("observation-invalid", f"missing fields: {sorted(missing)}")
    return dict(raw)


def _by_record_from_operations(operations: Mapping[str, Mapping[str, Any]]) -> dict:
    by_record: dict[str, list[str]] = {}
    for op_id, payload in operations.items():
        for mutation in payload["mutations"]:
            by_record.setdefault(mutation["record_id"], []).append(op_id)
    return by_record


def select_units(obs: Mapping[str, Any], op_ids: Sequence[str]) -> list[Unit]:
    """Pick every record of each requested blocked op (all-or-nothing per op)."""
    operations = obs["operations"]
    blocked = obs["blocked"]
    units: list[Unit] = []
    for bid in op_ids:
        payload = operations.get(bid)
        if payload is None:
            raise RepairRefusal("target-not-found", bid)
        code = blocked.get(bid)
        if code != "blocked-prior-evidence":
            raise RepairRefusal("target-not-blocked-prior-evidence", bid)
        if payload.get("kind") != "tombstone":
            raise RepairRefusal("target-kind-unsupported", bid)
        if payload.get("parents"):
            raise RepairRefusal("target-not-parentless", bid)
        project_key = payload["project_key"]
        for mutation in sorted(payload["mutations"], key=lambda m: m["record_id"]):
            tombstone = mutation.get("tombstone")
            if tombstone is None:
                raise RepairRefusal("target-kind-unsupported", bid)
            if tombstone.get("pending") is not False:
                raise RepairRefusal("target-pending", bid)
            rid = mutation["record_id"]
            graveyard = obs["graveyard"].get((bid, rid))
            if graveyard is None:
                raise RepairRefusal("prior-missing", f"{bid}:{rid}")
            prior_bytes = graveyard["prior_state_bytes"]
            try:
                prior_state = protocol_v2.canonical_loads(prior_bytes)
            except protocol_v2.ProtocolError as exc:
                raise RepairRefusal("prior-schema-incomplete", f"{bid}:{rid}:{exc.code}") from exc
            if not isinstance(prior_state, Mapping):
                raise RepairRefusal("prior-schema-incomplete", f"{bid}:{rid}")
            units.append(Unit(bid, project_key, rid, prior_state, dict(tombstone)))
    return units


def validate_units(obs: Mapping[str, Any], units: Sequence[Unit]) -> None:
    """Reject any unit that fails a §2.2 precondition against ``obs``."""
    result = obs["result"]
    by_record = obs["by_record"]
    for unit in units:
        bid, rid = unit.blocked_op_id, unit.record_id
        graveyard = obs["graveyard"].get((bid, rid))
        if graveyard is None:
            raise RepairRefusal("prior-missing", f"{bid}:{rid}")
        recomputed = _digest(unit.prior_state)
        if recomputed != graveyard["evidence_digest"] or recomputed != unit.tombstone["prior_digest"]:
            raise RepairRefusal("prior-digest-mismatch", f"{bid}:{rid}")
        if unit.prior_state.get("id") != rid:
            raise RepairRefusal("prior-record-id-mismatch", f"{bid}:{rid}")
        if unit.prior_state.get("delivery_state") == "pending":
            raise RepairRefusal("prior-pending", f"{bid}:{rid}")
        namespace = ("global" if unit.prior_state.get("scope") == "global"
                     else unit.prior_state.get("cwd_origin"))
        if namespace != unit.project_key:
            raise RepairRefusal("namespace-mismatch", f"{bid}:{rid}")
        if rid in result.conflicts:
            raise RepairRefusal("record-conflicted", f"{bid}:{rid}")
        if rid in result.records:
            raise RepairRefusal("record-present", f"{bid}:{rid}")
        heads = result.frontiers.get(rid, ())
        if len(heads) != 1:
            raise RepairRefusal("record-multi-head", f"{bid}:{rid}")
        if heads[0] != bid:
            raise RepairRefusal("record-head-not-target", f"{bid}:{rid}")
        others = [oid for oid in by_record.get(rid, ()) if oid != bid]
        if others:
            raise RepairRefusal("record-extra-operation", f"{bid}:{rid}")
        snapshot_evidence = obs["snapshot_graveyard"].get((bid, rid))
        if snapshot_evidence is None or bytes(snapshot_evidence) != bytes(graveyard["prior_state_bytes"]):
            raise RepairRefusal("evidence-store-mismatch", f"{bid}:{rid}")
    total_new = 2 * len(units)
    if total_new > protocol_v2.MAX_FOLD_OPERATIONS - len(result.accepted):
        raise RepairRefusal("budget-overflow", "new operation count exceeds fold budget")


def build_plan(obs: Mapping[str, Any], units: Sequence[Unit], *,
               reason: str, created_utc: str) -> dict:
    """Build the self-digested ``plan.json`` document (§2.3)."""
    if not reason:
        raise RepairRefusal("observation-invalid", "reason is required")
    store = obs["store"]
    snapshot = obs["snapshot"]
    result = obs["result"]
    plan_units = [
        {
            "blocked_op_id": unit.blocked_op_id,
            "project_key": unit.project_key,
            "record_id": unit.record_id,
            "prior_digest": _digest(unit.prior_state),
            "tombstone_digest": _digest(unit.tombstone),
            "tombstone_action": str(unit.tombstone["action"]),
            "tombstone_pending": bool(unit.tombstone["pending"]),
        }
        for unit in sorted(units, key=lambda u: (u.blocked_op_id, u.record_id))
    ]
    plan = {
        "schema": PLAN_SCHEMA,
        "reason": str(reason),
        "created_utc": str(created_utc),
        "store": {
            "replica_id": store["replica_id"],
            "epoch_id": store["epoch_id"],
            "store_path_digest": store["store_path_digest"],
            "fence_active": bool(store["fence_active"]),
            **({"authority_digest": store["authority_digest"]} if "authority_digest" in store else {}),
        },
        "snapshot": {
            "manifest_digest": snapshot["manifest_digest"],
            "epoch_id": snapshot["epoch_id"],
            "replica_id": snapshot["replica_id"],
            "membership_digest": snapshot["membership_digest"],
            "backup_sha256": snapshot["backup_sha256"],
            "backup_bytes": int(snapshot["backup_bytes"]),
            "schema_user_version": int(snapshot["schema_user_version"]),
        },
        "bind": {
            "operation_set_digest": result.accepted_set_digest,
            "capture_frontier": int(obs["capture_frontier"]),
            "preserved_digest": obs["preserved_digest"],
            "records": len(result.records),
            "pending": len(obs["pending_ids"]),
        },
        "units": plan_units,
        "counts": {
            "targets": len({unit.blocked_op_id for unit in units}),
            "records": len(units),
            "new_operations": 2 * len(units),
        },
    }
    plan["plan_digest"] = plan_digest(plan)
    return plan


def plan_digest(plan: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical plan bytes, excluding ``plan_digest`` itself."""
    stripped = {key: value for key, value in plan.items() if key != "plan_digest"}
    return _digest(stripped)


def normalize_plan(raw: bytes | bytearray | str | Mapping[str, Any]) -> dict:
    """Validate plan shape. Bytes/str input is parsed with duplicate-key rejection."""
    if isinstance(raw, Mapping):
        plan = dict(raw)
    else:
        try:
            plan = protocol_v2.canonical_loads(raw, require_canonical=False)
        except protocol_v2.ProtocolError as exc:
            raise RepairRefusal("plan-malformed", exc.code) from exc
    if not isinstance(plan, Mapping):
        raise RepairRefusal("plan-malformed", "plan must be an object")
    if frozenset(plan) != _PLAN_TOP_FIELDS:
        raise RepairRefusal("plan-malformed", "plan has unknown or missing top-level fields")
    if plan.get("schema") != PLAN_SCHEMA:
        raise RepairRefusal("plan-malformed", "unsupported plan schema")
    embedded_digest = plan.get("plan_digest")
    if not isinstance(embedded_digest, str) or len(embedded_digest) != 64:
        raise RepairRefusal("plan-malformed", "plan_digest must be a 64-hex-char string")
    if plan_digest(plan) != embedded_digest:
        # The embedded self-digest is untrusted input until it is proven to
        # match the plan body it claims to describe; only after this check
        # may callers treat ``plan["plan_digest"]`` as the plan's identity
        # (for ``--expect``, the repair receipt, and reported provenance).
        raise RepairRefusal("plan-digest-mismatch",
                            "embedded plan_digest does not match the recomputed digest")
    units = plan.get("units")
    if not isinstance(units, list) or not units:
        raise RepairRefusal("plan-malformed", "plan.units must be a non-empty list")
    for unit in units:
        if not isinstance(unit, Mapping) or frozenset(unit) != _PLAN_UNIT_FIELDS:
            raise RepairRefusal("plan-malformed", "plan unit has unknown or missing fields")
    return plan


def verify_plan_binding(obs: Mapping[str, Any], plan: Mapping[str, Any],
                        manifest: Mapping[str, Any]) -> None:
    """Bind plan <-> live store identity <-> verified snapshot manifest <-> selected B."""
    store = obs["store"]
    if plan["store"].get("authority_digest") != store.get("authority_digest"):
        raise RepairRefusal("store-binding-mismatch", "authority")
    if manifest.get("epoch_id") != store["epoch_id"] or manifest.get("replica_id") != store["replica_id"]:
        raise RepairRefusal("snapshot-identity-mismatch", "live epoch/replica")
    if (plan["store"]["replica_id"] != store["replica_id"]
            or plan["store"]["epoch_id"] != store["epoch_id"]
            or plan["store"]["store_path_digest"] != store["store_path_digest"]):
        raise RepairRefusal("store-binding-mismatch")
    for field in ("manifest_digest", "epoch_id", "replica_id", "membership_digest"):
        if plan["snapshot"][field] != manifest.get(field):
            raise RepairRefusal("snapshot-identity-mismatch", field)
    backup = manifest.get("backup", {})
    if (plan["snapshot"]["backup_sha256"] != backup.get("sha256")
            or plan["snapshot"]["backup_bytes"] != backup.get("bytes")
            or plan["snapshot"]["schema_user_version"] != manifest.get("schema_user_version")):
        raise RepairRefusal("snapshot-identity-mismatch", "backup")
    targets = {unit["blocked_op_id"] for unit in plan["units"]}
    for bid in targets:
        snap_target = obs["snapshot_targets"].get(bid)
        live_target = obs["live_targets"].get(bid)
        if (snap_target is None or live_target is None
                or bytes(snap_target["payload_bytes"]) != bytes(live_target["payload_bytes"])
                or snap_target["op_id"] != live_target["op_id"]):
            raise RepairRefusal("snapshot-target-payload-mismatch", bid)


def verify_plan_against(obs: Mapping[str, Any], plan: Mapping[str, Any]) -> list[Unit]:
    """Recompute every pre-apply staleness/record predicate; return fresh Units."""
    result = obs["result"]
    if (plan["bind"]["operation_set_digest"] != result.accepted_set_digest
            or plan["bind"]["capture_frontier"] != int(obs["capture_frontier"])
            or plan["bind"]["preserved_digest"] != obs["preserved_digest"]
            or plan["bind"]["records"] != len(result.records)
            or plan["bind"]["pending"] != len(obs["pending_ids"])):
        raise RepairRefusal("plan-stale")
    units: list[Unit] = []
    for entry in plan["units"]:
        bid, rid = entry["blocked_op_id"], entry["record_id"]
        graveyard = obs["graveyard"].get((bid, rid))
        if graveyard is None:
            raise RepairRefusal("prior-missing", f"{bid}:{rid}")
        recomputed = hashlib.sha256(bytes(graveyard["prior_state_bytes"])).hexdigest()
        if recomputed != graveyard["evidence_digest"] or recomputed != entry["prior_digest"]:
            raise RepairRefusal("prior-digest-mismatch", f"{bid}:{rid}")
        try:
            prior_state = protocol_v2.canonical_loads(bytes(graveyard["prior_state_bytes"]))
        except protocol_v2.ProtocolError as exc:
            raise RepairRefusal("prior-schema-incomplete", f"{bid}:{rid}:{exc.code}") from exc
        payload = obs["operations"].get(bid)
        if payload is None or obs["blocked"].get(bid) != "blocked-prior-evidence":
            raise RepairRefusal("target-not-blocked-prior-evidence", bid)
        mutation = next(
            (m for m in payload["mutations"] if m["record_id"] == rid), None
        )
        if mutation is None or "tombstone" not in mutation:
            raise RepairRefusal("record-set-incomplete", f"{bid}:{rid}")
        tombstone = dict(mutation["tombstone"])
        if tombstone.get("pending") is not False:
            raise RepairRefusal("target-pending", bid)
        unit = Unit(bid, payload["project_key"], rid, prior_state, tombstone)
        validate_units(obs, [unit])
        units.append(unit)
    return units


def build_operation_pair(unit: Unit, *, replica_id: str, put_counter: int,
                         tombstone_counter: int, plan_digest: str,
                         actor: str) -> tuple[dict, dict]:
    """Build ``(P_r, T_r)`` per §1.4: one mutation, one record, each."""
    reason = f"repair-blocked:{unit.blocked_op_id[:12]}:{unit.record_id[:12]}:{plan_digest[:12]}"
    provenance = {"actor": str(actor), "reason": reason, "source": "repair_v2.py"}
    put_op = protocol_v2.build_operation({
        "protocol_major": 2,
        "schema_minor": 0,
        "replica_id": replica_id,
        "counter": put_counter,
        "parents": [unit.blocked_op_id],
        "project_key": unit.project_key,
        "kind": "put",
        "frontiers": [{"record_id": unit.record_id, "heads": [unit.blocked_op_id]}],
        "mutations": [{
            "record_id": unit.record_id,
            "mutation_ordinal": 0,
            "post_state": dict(unit.prior_state),
        }],
        "provenance": provenance,
    })
    tombstone_op = protocol_v2.build_operation({
        "protocol_major": 2,
        "schema_minor": 0,
        "replica_id": replica_id,
        "counter": tombstone_counter,
        "parents": [put_op["op_id"]],
        "project_key": unit.project_key,
        "kind": "tombstone",
        "frontiers": [{"record_id": unit.record_id, "heads": [put_op["op_id"]]}],
        "mutations": [{
            "record_id": unit.record_id,
            "mutation_ordinal": 0,
            "tombstone": dict(unit.tombstone),
        }],
        "provenance": provenance,
    })
    return put_op, tombstone_op


def verify_fold(before: Any, after: Any, units: Sequence[Unit],
                new_ids: Sequence[str]) -> None:
    """Pure post-fold invariant check (§2.5). Raises on any violation."""
    new_id_set = set(new_ids)
    target_records = {unit.record_id for unit in units}
    target_ops = {unit.blocked_op_id for unit in units}

    def fail(reason: str) -> None:
        raise RepairRefusal("fold-invariant-failed", reason)

    if after.classification.hard_failures:
        fail("hard-failures-present")
    for op_id in new_id_set:
        op = after.classification.operations.get(op_id)
        if op is None:
            fail(f"new operation missing from fold: {op_id}")
            continue
        if len(op.payload["mutations"]) != 1 or len(op.payload["frontiers"]) != 1:
            fail(f"new operation is not single-record/single-mutation: {op_id}")
    if set(after.quarantined) != set(before.quarantined):
        fail("quarantined set changed")
    if set(after.deferred) != set(before.deferred):
        fail("deferred set changed")
    before_other_records = {rid: state for rid, state in before.records.items()
                            if rid not in target_records}
    after_other_records = {rid: state for rid, state in after.records.items()
                           if rid not in target_records}
    if set(before_other_records) != set(after_other_records):
        fail("unrelated record set changed")
    for rid in before_other_records:
        if protocol_v2.canonical_bytes(before_other_records[rid]) != \
                protocol_v2.canonical_bytes(after_other_records[rid]):
            fail(f"unrelated record body changed: {rid}")

    def pending_ids(result: Any) -> set:
        return {rid for rid, state in result.records.items()
                if state.get("delivery_state") == "pending"}

    if pending_ids(before) - target_records != pending_ids(after) - target_records:
        fail("unrelated pending set changed")
    for rid in target_records:
        if rid in after.records:
            fail(f"target record still present: {rid}")
    for unit in units:
        expected_tombstone = None
        for op_id in new_id_set:
            op = after.classification.operations.get(op_id)
            if (op is not None and op.payload["kind"] == "tombstone"
                    and op.payload["mutations"][0]["record_id"] == unit.record_id):
                expected_tombstone = op_id
        if after.tombstones.get(unit.record_id) != expected_tombstone:
            fail(f"target tombstone mismatch: {unit.record_id}")
        if after.frontiers.get(unit.record_id) != (expected_tombstone,):
            fail(f"target frontier mismatch: {unit.record_id}")
    for rid, heads in before.frontiers.items():
        if rid in target_records:
            continue
        if after.frontiers.get(rid) != heads:
            fail(f"unrelated frontier changed: {rid}")
    if set(before.conflicts) != set(after.conflicts):
        fail("conflict set changed")
    if new_id_set & set(after.blocked):
        fail("a new operation is blocked")
    if set(before.blocked) != set(after.blocked):
        fail("raw blocked set changed")
    resolved_after = protocol_v2.resolved_blocked_by(after)
    for bid in target_ops:
        decisions = resolved_after.get(bid)
        expected_records = {unit.record_id for unit in units if unit.blocked_op_id == bid}
        if decisions is None or set(decisions) != expected_records:
            fail(f"target op not fully resolved: {bid}")
    resolved_before = protocol_v2.resolved_blocked_by(before)
    for bid, decisions in resolved_before.items():
        if resolved_after.get(bid) != decisions:
            fail(f"preexisting resolution not preserved: {bid}")
    if len(after.accepted) != len(before.accepted) + len(new_id_set):
        fail("accepted count did not grow by exactly the new operation count")


__all__ = [
    "PLAN_SCHEMA", "RepairRefusal", "Unit", "build_operation_pair", "build_plan",
    "normalize_observation", "normalize_plan", "plan_digest", "select_units",
    "validate_units", "verify_fold", "verify_plan_against", "verify_plan_binding",
]
