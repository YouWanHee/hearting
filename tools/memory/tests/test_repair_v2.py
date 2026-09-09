#!/usr/bin/env python3
"""Pure tests for ``repair_v2`` (core/MEMORY.md §7.2.2 blocked-history repair).

Every fixture goes through real ``protocol_v2.build_operation``/``fold_operations``
so a shape ``_validate_kind_mutations`` would reject can never appear here.
``repair_v2`` itself has no DB/filesystem dependency; these tests build the
observation mapping by hand instead of through ``mem.py``.
"""

from __future__ import annotations

import hashlib
import itertools
import unittest

from helpers import record_post_state, tombstone_evidence

import protocol_v2
import repair_v2

REPLICA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PROJECT = "project-alpha"


def op(counter, kind, parents, mutations_by_record, *, replica=REPLICA,
       project_key=PROJECT, provenance_extra=None):
    parents = tuple(sorted(parents))
    rids = sorted(mutations_by_record)
    frontiers = [{"record_id": rid, "heads": list(parents)} for rid in rids]
    mutations = [
        {"record_id": rid, "mutation_ordinal": i, **mutations_by_record[rid]}
        for i, rid in enumerate(rids)
    ]
    provenance = {"actor": "test", "reason": "fixture"}
    if provenance_extra:
        provenance.update(provenance_extra)
    return protocol_v2.build_operation({
        "protocol_major": 2, "schema_minor": 0, "replica_id": replica,
        "counter": counter, "parents": list(parents), "project_key": project_key,
        "kind": kind, "frontiers": frontiers, "mutations": mutations,
        "provenance": provenance,
    })


def blocked_tombstone(counter, record_ids, *, priors=None):
    """A parentless multi-record tombstone plus its transactional-graveyard priors."""
    priors = priors or {rid: record_post_state(rid, f"{rid} prior") for rid in record_ids}
    b = op(counter, "tombstone", (), {
        rid: {"tombstone": tombstone_evidence(rid, action="prune", prior_state=priors[rid])}
        for rid in record_ids
    })
    return b, priors


def make_obs(all_ops, target_ids, graveyard_priors, *, snapshot_targets=None,
            snapshot_graveyard=None, capture_frontier=0, snapshot=None,
            store=None):
    result = protocol_v2.fold_operations(all_ops)
    operations = {oid: result.classification.operations[oid].payload
                 for oid in result.accepted}
    blocked = {oid: diag.code for oid, diag in result.blocked.items()}
    by_record = {}
    for oid, payload in operations.items():
        for mutation in payload["mutations"]:
            by_record.setdefault(mutation["record_id"], []).append(oid)
    graveyard = {}
    for (bid, rid), prior in graveyard_priors.items():
        prior_bytes = protocol_v2.canonical_bytes(prior)
        graveyard[(bid, rid)] = {
            "action": "prune",
            "prior_state_bytes": prior_bytes,
            "evidence_digest": hashlib.sha256(prior_bytes).hexdigest(),
        }
    if snapshot_graveyard is None:
        snapshot_graveyard = {key: value["prior_state_bytes"] for key, value in graveyard.items()}
    live_targets = {}
    for bid in target_ids:
        if bid in operations:
            live_targets[bid] = {"op_id": bid,
                                 "payload_bytes": protocol_v2.canonical_bytes(operations[bid])}
    if snapshot_targets is None:
        snapshot_targets = dict(live_targets)
    pending_ids = {rid for rid, state in result.records.items()
                  if state.get("delivery_state") == "pending"}
    preserved = {
        "records": sorted(result.records),
        "pending": sorted(pending_ids),
        "graveyard": sorted(f"{k[0]}:{k[1]}:{v['evidence_digest']}" for k, v in graveyard.items()),
    }
    preserved_digest = hashlib.sha256(protocol_v2.canonical_bytes(preserved)).hexdigest()
    return repair_v2.normalize_observation({
        "result": result,
        "operations": operations,
        "blocked": blocked,
        "by_record": by_record,
        "graveyard": graveyard,
        "snapshot_graveyard": snapshot_graveyard,
        "snapshot_targets": snapshot_targets,
        "live_targets": live_targets,
        "store": store or {"replica_id": "r" * 32, "epoch_id": "epoch-1",
                           "store_path_digest": "sha256:store", "fence_active": True},
        "snapshot": snapshot or {
            "manifest_digest": "sha256:manifest", "epoch_id": "epoch-1",
            "replica_id": "r" * 32, "membership_digest": "sha256:membership",
            "backup_sha256": "sha256:backup", "backup_bytes": 4096,
            "schema_user_version": 10,
        },
        "capture_frontier": capture_frontier,
        "preserved_digest": preserved_digest,
        "pending_ids": pending_ids,
    })


class SelectAndValidateTest(unittest.TestCase):
    def test_5_multi_record_group_pure_replay_and_reorder_equivalence(self):
        b1, priors1 = blocked_tombstone(1, ("r1", "r2"))
        b2, priors2 = blocked_tombstone(10, ("r3",))
        all_ops = [b1, b2]
        graveyard_priors = {(b1["op_id"], rid): prior for rid, prior in priors1.items()}
        graveyard_priors.update({(b2["op_id"], rid): prior for rid, prior in priors2.items()})
        obs = make_obs(all_ops, [b1["op_id"], b2["op_id"]], graveyard_priors)
        units = repair_v2.select_units(obs, [b1["op_id"], b2["op_id"]])
        self.assertEqual(len(units), 3)
        repair_v2.validate_units(obs, units)
        plan = repair_v2.build_plan(obs, units, reason="fixture", created_utc="2026-09-09T00:00:00Z")
        self.assertEqual(plan["counts"], {"targets": 2, "records": 3, "new_operations": 6})
        pairs, candidate_ops = [], []
        replica = REPLICA
        counter = 100
        for unit in units:
            put_op, tomb_op = repair_v2.build_operation_pair(
                unit, replica_id=replica, put_counter=counter,
                tombstone_counter=counter + 1, plan_digest=plan["plan_digest"], actor="operator")
            counter += 2
            candidate_ops += [put_op, tomb_op]
            pairs.append((put_op, tomb_op))
        before = obs["result"]
        new_ids = [o["op_id"] for o in candidate_ops]
        # Bound the reorder sweep (fold_operations is not free); a sample plus
        # the fully-reversed order already exercises arrival-order independence.
        for order in itertools.islice(itertools.permutations(candidate_ops), 30):
            after = protocol_v2.fold_operations(all_ops + list(order))
            repair_v2.verify_fold(before, after, units, new_ids)
        after_reversed = protocol_v2.fold_operations(all_ops + list(reversed(candidate_ops)))
        repair_v2.verify_fold(before, after_reversed, units, new_ids)
        # Duplicate replay (idempotent re-fold of the same set) is a no-op.
        after_once = protocol_v2.fold_operations(all_ops + candidate_ops)
        after_twice = protocol_v2.fold_operations(all_ops + candidate_ops + candidate_ops)
        self.assertEqual(after_once.accepted_set_digest, after_twice.accepted_set_digest)

    def test_6_p0_regression_late_concurrent_write_stays_isolated_per_record(self):
        """The per-record design survives the rejected 16-op design's exact counterexample."""
        b, priors = blocked_tombstone(1, ("r1", "r2"))
        graveyard_priors = {(b["op_id"], rid): prior for rid, prior in priors.items()}
        obs = make_obs([b], [b["op_id"]], graveyard_priors)
        units = repair_v2.select_units(obs, [b["op_id"]])
        repair_v2.validate_units(obs, units)
        plan = repair_v2.build_plan(obs, units, reason="fixture", created_utc="2026-09-09T00:00:00Z")
        candidate_ops = []
        counter = 100
        for unit in units:
            put_op, tomb_op = repair_v2.build_operation_pair(
                unit, replica_id=REPLICA, put_counter=counter, tombstone_counter=counter + 1,
                plan_digest=plan["plan_digest"], actor="operator")
            counter += 2
            candidate_ops.append((unit.record_id, put_op, tomb_op))
        # A late-arriving concurrent write on r1 only, from another replica,
        # descending the same blocked op B (simulating a replica that only
        # ever saw B, never the repair).
        other_replica = "b" * 32
        late = op(1, "put", (b["op_id"],), {
            "r1": {"post_state": record_post_state("r1", "late write", project_key=PROJECT)},
        }, replica=other_replica)
        per_record_ops = [pair for _, put_op, tomb_op in candidate_ops for pair in (put_op, tomb_op)]
        result = protocol_v2.fold_operations([b, late] + per_record_ops)
        self.assertIn("r1", result.conflicts)
        self.assertNotIn("r2", result.conflicts)
        self.assertNotIn("r2", result.records)
        self.assertEqual(result.tombstones.get("r2"),
                         next(t["op_id"] for rid, _, t in candidate_ops if rid == "r2"))
        self.assertNotIn(b["op_id"], protocol_v2.resolved_blocked_by(result))

        # Contrast: the rejected shared-T (16-op-style, one shared T(r1,r2))
        # design resurrects r2's body under the identical late arrival.
        shared_put = op(200, "put", (b["op_id"],), {
            "r1": {"post_state": priors["r1"]}, "r2": {"post_state": priors["r2"]},
        })
        shared_tombstone = op(201, "tombstone", (shared_put["op_id"],), {
            "r1": {"tombstone": tombstone_evidence("r1", prior_state=priors["r1"])},
            "r2": {"tombstone": tombstone_evidence("r2", prior_state=priors["r2"])},
        })
        shared_result = protocol_v2.fold_operations([b, late, shared_put, shared_tombstone])
        self.assertEqual(shared_result.blocked[shared_tombstone["op_id"]].code, "blocked-concurrency")
        self.assertIn("r2", shared_result.records)
        self.assertEqual(shared_result.records["r2"]["body"], priors["r2"]["body"])

    def test_7_late_arrival_permutation_sweep_never_resurrects_an_unrelated_record(self):
        b, priors = blocked_tombstone(1, ("r1", "r2", "r3"))
        graveyard_priors = {(b["op_id"], rid): prior for rid, prior in priors.items()}
        obs = make_obs([b], [b["op_id"]], graveyard_priors)
        units = repair_v2.select_units(obs, [b["op_id"]])
        plan = repair_v2.build_plan(obs, units, reason="fixture", created_utc="2026-09-09T00:00:00Z")
        pairs = {}
        counter = 100
        for unit in units:
            put_op, tomb_op = repair_v2.build_operation_pair(
                unit, replica_id=REPLICA, put_counter=counter, tombstone_counter=counter + 1,
                plan_digest=plan["plan_digest"], actor="operator")
            counter += 2
            pairs[unit.record_id] = (put_op, tomb_op)
        late = op(1, "put", (b["op_id"],), {
            "r1": {"post_state": record_post_state("r1", "late write", project_key=PROJECT)},
        }, replica="b" * 32)
        base_ops = [b] + [pair for pr in pairs.values() for pair in pr]
        # Sweep every arrival position of the late write against a fixed
        # causal-valid base order (fold_operations is order-independent for the
        # base set already, per PureFoldTest; this isolates the one axis that
        # matters here -- when the unrelated late write shows up).
        for position in range(len(base_ops) + 1):
            order = base_ops[:position] + [late] + base_ops[position:]
            result = protocol_v2.fold_operations(order)
            self.assertNotIn("r2", result.records)
            self.assertNotIn("r3", result.records)
            self.assertEqual(result.tombstones.get("r2"), pairs["r2"][1]["op_id"])
            self.assertEqual(result.tombstones.get("r3"), pairs["r3"][1]["op_id"])
        # And a handful of full shuffles of the base set (order-independence
        # is already covered generally; this just checks it still holds here).
        for order in itertools.islice(itertools.permutations(base_ops), 20):
            result = protocol_v2.fold_operations(list(order) + [late])
            self.assertNotIn("r2", result.records)
            self.assertNotIn("r3", result.records)

    def test_7b_late_restore_never_invalidates_an_unrelated_records_tombstone(self):
        b, priors = blocked_tombstone(1, ("r1", "r2"))
        graveyard_priors = {(b["op_id"], rid): prior for rid, prior in priors.items()}
        obs = make_obs([b], [b["op_id"]], graveyard_priors)
        units = repair_v2.select_units(obs, [b["op_id"]])
        plan = repair_v2.build_plan(obs, units, reason="fixture", created_utc="2026-09-09T00:00:00Z")
        pairs = {}
        counter = 100
        for unit in units:
            put_op, tomb_op = repair_v2.build_operation_pair(
                unit, replica_id=REPLICA, put_counter=counter, tombstone_counter=counter + 1,
                plan_digest=plan["plan_digest"], actor="operator")
            counter += 2
            pairs[unit.record_id] = (put_op, tomb_op)
        base_ops = [pair for pr in pairs.values() for pair in pr]

        for target, label in ((pairs["r1"][1]["op_id"], "T_r1"), (b["op_id"], "B")):
            with self.subTest(restore_target=label):
                restore = op(300, "restore", (target,), {
                    "r1": {"post_state": priors["r1"], "target_op_id": target},
                }, replica="c" * 32)
                result = protocol_v2.fold_operations([b] + base_ops + [restore])
                self.assertNotIn("r2", result.records)
                self.assertEqual(result.tombstones.get("r2"), pairs["r2"][1]["op_id"])
                if "r1" in result.records:
                    # r1 lost its safe final head; the whole op stays active.
                    self.assertNotIn(b["op_id"], protocol_v2.resolved_blocked_by(result))

    def test_8_prior_mismatch_missing_and_bad_schema_are_typed_refusals(self):
        b, priors = blocked_tombstone(1, ("r1",))
        graveyard_priors = {(b["op_id"], "r1"): priors["r1"]}
        obs = make_obs([b], [b["op_id"]], graveyard_priors)
        units = repair_v2.select_units(obs, [b["op_id"]])
        repair_v2.validate_units(obs, units)  # sanity: the good fixture passes

        with self.assertRaises(repair_v2.RepairRefusal) as ctx:
            repair_v2.select_units(obs, ["0" * 64])
        self.assertEqual(ctx.exception.code, "target-not-found")

        tampered = dict(obs)
        tampered_graveyard = dict(obs["graveyard"])
        row = dict(tampered_graveyard[(b["op_id"], "r1")])
        row["evidence_digest"] = "0" * 64
        tampered_graveyard[(b["op_id"], "r1")] = row
        tampered["graveyard"] = tampered_graveyard
        with self.assertRaises(repair_v2.RepairRefusal) as ctx:
            repair_v2.validate_units(tampered, units)
        self.assertEqual(ctx.exception.code, "prior-digest-mismatch")

        missing = dict(obs)
        missing["graveyard"] = {}
        with self.assertRaises(repair_v2.RepairRefusal) as ctx:
            repair_v2.select_units(missing, [b["op_id"]])
        self.assertEqual(ctx.exception.code, "prior-missing")

    def test_9_unsafe_record_states_are_each_rejected_with_pending_preserved(self):
        b, priors = blocked_tombstone(1, ("r1", "r2"))
        graveyard_priors = {(b["op_id"], rid): prior for rid, prior in priors.items()}

        # r2 is present (deleted body since resurrected via a later put).
        resurrect = op(2, "put", (b["op_id"],), {
            "r2": {"post_state": record_post_state("r2", "resurrected", project_key=PROJECT)},
        })
        obs = make_obs([b, resurrect], [b["op_id"]], graveyard_priors)
        units = repair_v2.select_units(obs, [b["op_id"]])
        with self.assertRaises(repair_v2.RepairRefusal) as ctx:
            repair_v2.validate_units(obs, units)
        self.assertEqual(ctx.exception.code, "record-present")

        # An unrelated pending record must stay untouched and irrelevant.
        pending_rid = "r-pending"
        pending_prior = record_post_state(pending_rid, "pending", pending=True, project_key=PROJECT)
        pending_put = op(3, "put", (), {pending_rid: {"post_state": pending_prior}})
        obs2 = make_obs([b, pending_put], [b["op_id"]], graveyard_priors)
        self.assertIn(pending_rid, obs2["pending_ids"])
        units2 = repair_v2.select_units(obs2, [b["op_id"]])
        repair_v2.validate_units(obs2, units2)  # unaffected by the unrelated pending record


class PlanTest(unittest.TestCase):
    def test_10_plan_digest_stability_no_body_leakage_and_duplicate_key_rejection(self):
        b, priors = blocked_tombstone(1, ("r1",))
        graveyard_priors = {(b["op_id"], "r1"): priors["r1"]}
        obs = make_obs([b], [b["op_id"]], graveyard_priors)
        units = repair_v2.select_units(obs, [b["op_id"]])
        plan = repair_v2.build_plan(obs, units, reason="fixture", created_utc="2026-09-09T00:00:00Z")
        self.assertEqual(plan["plan_digest"], repair_v2.plan_digest(plan))
        again = repair_v2.build_plan(obs, units, reason="fixture", created_utc="2026-09-09T00:00:00Z")
        self.assertEqual(plan, again)

        blob = protocol_v2.canonical_bytes(plan).decode("utf-8")
        self.assertNotIn(priors["r1"]["body"], blob)

        normalized = repair_v2.normalize_plan(plan)
        self.assertEqual(normalized, plan)

        duplicate_key_json = (
            '{"schema":1,"schema":1,"reason":"x","created_utc":"x","store":{},'
            '"snapshot":{},"bind":{},"units":[],"counts":{},"plan_digest":"x"}'
        )
        with self.assertRaises(repair_v2.RepairRefusal) as ctx:
            repair_v2.normalize_plan(duplicate_key_json)
        self.assertEqual(ctx.exception.code, "plan-malformed")

    def test_10c_tampered_embedded_plan_digest_is_never_trusted(self):
        """A wrong ``plan_digest`` field must be caught by shape alone (Repair A3):
        an attacker who supplies the correct original digest as the caller's
        ``--expect`` must not be able to smuggle a different embedded identity
        through for receipt/provenance storage."""
        b, priors = blocked_tombstone(1, ("r1",))
        graveyard_priors = {(b["op_id"], "r1"): priors["r1"]}
        obs = make_obs([b], [b["op_id"]], graveyard_priors)
        units = repair_v2.select_units(obs, [b["op_id"]])
        plan = repair_v2.build_plan(obs, units, reason="fixture", created_utc="2026-09-09T00:00:00Z")

        tampered = dict(plan)
        tampered["plan_digest"] = "f" * 64
        with self.assertRaises(repair_v2.RepairRefusal) as ctx:
            repair_v2.normalize_plan(tampered)
        self.assertEqual(ctx.exception.code, "plan-digest-mismatch")

        wrong_type = dict(plan)
        wrong_type["plan_digest"] = 12345
        with self.assertRaises(repair_v2.RepairRefusal) as ctx:
            repair_v2.normalize_plan(wrong_type)
        self.assertEqual(ctx.exception.code, "plan-malformed")

        # The untampered plan continues to normalize cleanly and unchanged.
        self.assertEqual(repair_v2.normalize_plan(plan), plan)

    def test_10b_verify_fold_preserves_a_preexisting_resolution(self):
        # An already-resolved blocked op (via a pre-existing safe final put)
        # must remain resolved, with the same decision map, after an unrelated
        # blocked op's repair pair set is folded in.
        preexisting_blocked, preexisting_priors = blocked_tombstone(1, ("rx",))
        preexisting_final = op(2, "put", (preexisting_blocked["op_id"],), {
            "rx": {"post_state": record_post_state("rx", "safe final put", project_key=PROJECT)},
        })
        b, priors = blocked_tombstone(10, ("r1",))
        graveyard_priors = {(b["op_id"], "r1"): priors["r1"]}
        all_ops = [preexisting_blocked, preexisting_final, b]
        obs = make_obs(all_ops, [b["op_id"]], graveyard_priors)
        before = obs["result"]
        before_resolved = protocol_v2.resolved_blocked_by(before)
        self.assertIn(preexisting_blocked["op_id"], before_resolved)

        units = repair_v2.select_units(obs, [b["op_id"]])
        plan = repair_v2.build_plan(obs, units, reason="fixture", created_utc="2026-09-09T00:00:00Z")
        candidate_ops = []
        counter = 100
        for unit in units:
            put_op, tomb_op = repair_v2.build_operation_pair(
                unit, replica_id=REPLICA, put_counter=counter, tombstone_counter=counter + 1,
                plan_digest=plan["plan_digest"], actor="operator")
            counter += 2
            candidate_ops += [put_op, tomb_op]
        after = protocol_v2.fold_operations(all_ops + candidate_ops)
        new_ids = [o["op_id"] for o in candidate_ops]
        repair_v2.verify_fold(before, after, units, new_ids)  # must not raise
        after_resolved = protocol_v2.resolved_blocked_by(after)
        self.assertEqual(after_resolved[preexisting_blocked["op_id"]],
                         before_resolved[preexisting_blocked["op_id"]])

        # If the preexisting resolution were lost, verify_fold must refuse
        # rather than silently accept a tightened fold. Prove it by folding
        # only the candidate ops without the preexisting resolution's own
        # final put in `after` -- `rx` reverts to unresolved, which the
        # invariant check must catch as a lost preexisting resolution.
        after_without_preexisting_final = protocol_v2.fold_operations(
            [preexisting_blocked, b] + candidate_ops
        )
        with self.assertRaises(repair_v2.RepairRefusal) as ctx:
            repair_v2.verify_fold(before, after_without_preexisting_final, units, new_ids)
        self.assertEqual(ctx.exception.code, "fold-invariant-failed")


if __name__ == "__main__":
    unittest.main()
