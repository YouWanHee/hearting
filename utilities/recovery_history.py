"""Read recovery inputs as historical facts, independently of launch selection.

Stored manifests are checked against stored digests and original attempt rows.
When an older launch has no input payload, its exact source route supplies only
the fields the registry did not store.  A successor's owner, profiles or capacity
never supply missing historical facts.  Current launch authority belongs to
route_authority and the reservation consumer, not this reader.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from replica_batch_contract import DIGEST, ReplicaBatchContractError, build_manifest, verify_manifest

INPUT_SCHEMA = "automatic-parallel-input-v1"


class RecoveryHistoryError(RuntimeError):
    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def batch_input_path(jobs, digest):
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise RecoveryHistoryError("replacement-batch-manifest-invalid")
    return Path(jobs).resolve().parent / "automatic-replacements" / "batch-inputs" / (digest.split(":")[1] + ".json")


def _read(path):
    if path.is_symlink():
        raise RecoveryHistoryError("replacement-record-symlink", str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RecoveryHistoryError("replacement-record-unreadable", str(path)) from exc
    if not isinstance(value, dict):
        raise RecoveryHistoryError("replacement-record-invalid", str(path))
    return value


def source_manifest_for_digest(kwargs, expected_digest):
    """Read the historical schema/encoding selected by its existing digest."""
    for independence, reason in (
        ("persona", ""), ("cross-harness", ""),
        ("degraded-same-harness", "cross-harness-unavailable-user-allowed"),
    ):
        raw_members = kwargs.get("members", [])
        if len(raw_members) == 2:
            try:
                fields = {"assignment_sha256", "attempt_id", "route_node", "harness", "fallback_hop", "fallback_ordinal"}
                legacy = {"schema_version": 1, "kind": "replica-batch", "declared_size": 2,
                    "replica_group": kwargs.get("parallel_group") or kwargs.get("replica_group"),
                    "route_id": kwargs["route_id"], "parent_attempt_id": kwargs["parent_attempt_id"],
                    "independence": independence,
                    "members": sorted(({key: value for key, value in member.items() if key in fields}
                                       for member in raw_members),
                                      key=lambda member: (member["route_node"], member["attempt_id"]))}
                checked = verify_manifest(legacy)
                if checked[1] == expected_digest:
                    return checked
            except (ReplicaBatchContractError, KeyError, TypeError):
                pass
        try:
            result = build_manifest(**kwargs, independence=independence, degradation_reason=reason)
        except ReplicaBatchContractError:
            continue
        candidates = [result[0]]
        # Schema 2 had no leg class or model demand.  Schema 1 sealed just the
        # two execution tuples.  Their bytes remain verify-only evidence.
        v2 = dict(result[0], schema_version=2, members=[
            {key: value for key, value in member.items()
             if key not in {"leg_class", "auxiliary_check", "profile_selection", "profile_demand"}}
            for member in result[0]["members"]])
        candidates.append(v2)
        for candidate in candidates:
            checked = verify_manifest(candidate)
            if checked[1] == expected_digest:
                return checked
    raise ReplicaBatchContractError("sealed source manifest digest mismatch")


def _assignment(prompt):
    return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _verify_rows(manifest, digest, legs, rows, *, complete=False, route_hash=None, prompt=None):
    """Compare recorded values to recorded values, never to a current tuple."""
    from dispatch_contract import validate_attempt_metadata

    members = {member["attempt_id"]: member for member in manifest["members"]}
    attempt_ids = [row.get("attempt_id") for row in rows]
    if (not rows or len(set(attempt_ids)) != len(rows)
            or (complete and set(attempt_ids) != set(members))):
        raise RecoveryHistoryError("batch-prior-manifest-proof-missing")
    group = manifest.get("parallel_group") or manifest.get("replica_group")
    version = int(manifest.get("schema_version", 1))
    if prompt is not None and any(member["assignment_sha256"] != _assignment(prompt) for member in members.values()):
        raise RecoveryHistoryError("batch-prior-binding-drift", "assignment")
    for row in rows:
        # Historical rows do not gain new execution authority by being read.
        # Current rows keep their existing structural validator; older rows
        # remain readable through their canonical manifest and leg digests.
        if row.get("attempt_schema_version") == "2":
            validate_attempt_metadata(row)
        aid = row.get("attempt_id")
        member = members.get(aid)
        if member is None:
            raise RecoveryHistoryError("batch-prior-binding-drift", str(aid))
        expected = {
            "batch_manifest_sha256": digest, "batch_leg_sha256": legs[aid],
            "batch_group": group, "batch_declared_size": str(manifest["declared_size"]),
            "batch_independence": manifest["independence"],
            "route_id": manifest["route_id"], "batch_route_id": manifest["route_id"],
            "parent_attempt_id": manifest["parent_attempt_id"],
            "batch_parent_attempt_id": manifest["parent_attempt_id"],
            "batch_attempt_id": aid,
            "route_node": member["route_node"], "batch_route_node": member["route_node"],
            "harness": member["harness"], "batch_harness": member["harness"],
            "fallback_hop": member["fallback_hop"], "batch_fallback_hop": member["fallback_hop"],
            "fallback_ordinal": str(member["fallback_ordinal"]),
            "batch_fallback_ordinal": str(member["fallback_ordinal"]),
            "batch_assignment_sha256": member["assignment_sha256"],
        }
        if version >= 2:
            expected.update({"batch_" + key: str(member[key]) for key in
                             ("model_profile", "perspective", "parallel_leg_index")})
        if version >= 3:
            if row.get("batch_leg_class", "peer") != member["leg_class"]:
                raise RecoveryHistoryError("batch-prior-binding-drift", "leg_class")
            if member["leg_class"] == "auxiliary":
                expected["batch_auxiliary_check"] = member["auxiliary_check"]
        if route_hash and row.get("route_hash"):
            expected["route_hash"] = route_hash
        # The original digest/leg and source identity are the history anchor.
        # Later mirror columns are useful consistency evidence when recorded,
        # but reading an older payload must not require adding those columns.
        anchors = {"batch_manifest_sha256", "batch_leg_sha256", "route_id", "route_node", "parent_attempt_id"}
        if any(row.get(key) != value for key, value in expected.items() if key in anchors or key in row):
            raise RecoveryHistoryError("batch-prior-binding-drift", str(member["route_node"]))


def _source_route(rows, provided):
    """Prefer the original row's route file to a caller's in-force view."""
    route_ids = {row.get("batch_route_id") for row in rows}
    hashes = {row["route_hash"] for row in rows if row.get("route_hash")}
    if len(route_ids) != 1 or len(hashes) > 1:
        raise RecoveryHistoryError("batch-prior-binding-drift", "source route")
    route_id = next(iter(route_ids))
    sealed_hash = next(iter(hashes), None)
    paths = {row["route_file"] for row in rows if row.get("route_file")}
    if len(paths) > 1:
        raise RecoveryHistoryError("batch-prior-binding-drift", "source route file")
    route = _read(Path(next(iter(paths)))) if paths else None
    if route is None:
        route = provided
    if (not isinstance(route, dict) or route.get("route_id") != route_id
            or (sealed_hash and route.get("route_hash") != sealed_hash)):
        raise RecoveryHistoryError("batch-prior-manifest-proof-missing", "original source route unavailable")
    expected_hash = sealed_hash or route.get("route_hash")
    if isinstance(expected_hash, str) and DIGEST.fullmatch(expected_hash):
        from route_identity import route_hash
        if route_hash(route) != expected_hash:
            raise RecoveryHistoryError("batch-prior-binding-drift", "source route hash")
    return route


def reconstruct_batch_input(rows, source_route, group, prompt=None):
    """Recover pre-payload history from its complete exact row census."""
    route = _source_route(rows, source_route)
    nodes = {node["id"]: node for node in route.get("nodes", [])
             if (node.get("parallel_group") or node.get("replica_group")) == group}
    if (len(rows) != len(nodes) or {row.get("batch_route_node") for row in rows} != set(nodes)
            or len({row.get("attempt_id") for row in rows}) != len(nodes)):
        raise RecoveryHistoryError("batch-prior-manifest-proof-missing", group)
    members = []
    try:
        for row in rows:
            node = nodes[row["batch_route_node"]]
            member = {"assignment_sha256": row["batch_assignment_sha256"], "attempt_id": row["attempt_id"],
                "route_node": row["batch_route_node"], "harness": row["batch_harness"],
                "fallback_hop": row["batch_fallback_hop"], "fallback_ordinal": int(row["batch_fallback_ordinal"])}
            if all("batch_" + key in row for key in ("model_profile", "perspective", "parallel_leg_index")):
                member.update({"model_profile": row["batch_model_profile"], "perspective": row["batch_perspective"],
                    "parallel_leg_index": int(row["batch_parallel_leg_index"]), "leg_class": row.get("batch_leg_class", "peer")})
            if member.get("leg_class") == "auxiliary":
                member["auxiliary_check"] = row["batch_auxiliary_check"]
            if "profile_selection" in node:
                member.update({key: node[key] for key in ("profile_selection", "profile_demand")})
            members.append(member)
        declared = list(next(iter(nodes.values())).get("parallel_independence_axes", ["cross-harness"]))
        realized = []
        if len({member["harness"] for member in members}) >= 2:
            realized.append("cross-harness")
        if all("model_profile" in member for member in members) and len({member["model_profile"] for member in members}) >= 2:
            realized.append("model-profile")
        if all("perspective" in member for member in members) and len({member["perspective"] for member in members}) == len(members):
            realized.append("perspective")
        manifest, digest, legs = source_manifest_for_digest(dict(parallel_group=group,
            route_id=route["route_id"], parent_attempt_id=rows[0]["batch_parent_attempt_id"],
            required_independence_axes=declared, realized_independence_axes=realized, members=members),
            rows[0]["batch_manifest_sha256"])
        _verify_rows(manifest, digest, legs, rows, complete=True, route_hash=route.get("route_hash"), prompt=prompt)
        return {"manifest": manifest, "manifest_digest": digest, "route_hash": route["route_hash"],
                "options": {"prompt_text": prompt}, "source": "registry-manifest-proof"}
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryHistoryError("batch-prior-binding-invalid", str(exc)) from exc


def read_batch_input(jobs, rows, *, source_route=None, group=None, prompt=None, complete=False, node_ids=None):
    """Read payload first, falling back only to its original historical record."""
    if not rows or len({row.get("batch_manifest_sha256") for row in rows}) != 1:
        raise RecoveryHistoryError("batch-prior-binding-ambiguous", group or "")
    digest = rows[0].get("batch_manifest_sha256")
    payload = _read(batch_input_path(jobs, digest))
    if payload is None:
        if source_route is None or group is None:
            raise RecoveryHistoryError("replacement-batch-input-unproven")
        payload = reconstruct_batch_input(rows, source_route, group, prompt)
        if node_ids is not None and {member["route_node"] for member in payload["manifest"]["members"]} != set(node_ids):
            raise RecoveryHistoryError("batch-prior-binding-drift", "node census")
        return payload
    if payload.get("schema") != INPUT_SCHEMA or payload.get("jobs") != str(Path(jobs).resolve()):
        raise RecoveryHistoryError("replacement-batch-input-unproven")
    try:
        manifest, actual, legs = verify_manifest(payload.get("manifest"))
        options = payload.get("options")
        if (digest != actual or payload.get("manifest_digest") != actual
                or payload.get("route_id") != manifest["route_id"]
                or not isinstance(options, dict) or not isinstance(options.get("prompt_text"), str)):
            raise RecoveryHistoryError("replacement-batch-source-drift")
        manifest_group = manifest.get("parallel_group") or manifest.get("replica_group")
        if (options.get("parallel_group") != manifest_group
                or (group is not None and group != manifest_group)
                or (source_route is not None and (payload.get("route_id"), payload.get("route_hash"))
                    != (source_route.get("route_id"), source_route.get("route_hash")))
                or (node_ids is not None and {member["route_node"] for member in manifest["members"]} != set(node_ids))
                or (prompt is not None and prompt != options["prompt_text"])):
            raise RecoveryHistoryError("replacement-batch-source-drift")
        _verify_rows(manifest, actual, legs, rows, complete=complete,
                     route_hash=payload.get("route_hash"), prompt=options["prompt_text"])
        if any(options.get("parent") != row.get("parent") for row in rows):
            raise RecoveryHistoryError("replacement-batch-source-drift", "original parent")
        if any(row.get("route_file") and str(Path(row["route_file"]).resolve()) != options.get("route") for row in rows):
            raise RecoveryHistoryError("replacement-batch-source-drift", "original route file")
        return payload
    except (ReplicaBatchContractError, KeyError, TypeError, ValueError) as exc:
        raise RecoveryHistoryError("replacement-batch-source-drift", str(exc)) from exc
