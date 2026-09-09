#!/usr/bin/env python3
"""CLI/DB end-to-end tests for `mem repair-blocked` (core/MEMORY.md §7.2.2).

Every store here is a disposable synthetic SQLite database under a private,
isolated HOME/XDG environment (never a live store); the blocked-history
fixture is built directly against the local ledgers the same way a real
historical migration gap would leave one, then verified through the public
CLI and its logical-state invariants.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MEM = ROOT / "tools/memory/mem.py"
sys.path.insert(0, str(ROOT / "tools/memory"))
import protocol_v2  # noqa: E402
import sync_v2  # noqa: E402

EPOCH = "0123456789abcdef0123456789abcdef"

SEED_FIXTURE = r"""
import sys, hashlib, json
sys.path.insert(0, sys.argv[1])
import mem, sync_v2, protocol_v2

project_key, out_path = sys.argv[2], sys.argv[3]
record_ids = sorted(sys.argv[4:])
con = mem.get_con()
try:
    con.execute('BEGIN IMMEDIATE')
    fp = mem._installation_fingerprint()
    replica = sync_v2.ensure_replica_identity(con, installation_fingerprint=fp)
    counter = sync_v2.allocate_counter(con, replica, installation_fingerprint=fp)
    priors, mutations = {}, []
    for i, rid in enumerate(record_ids):
        prior = {
            "id": rid, "tier": "durable", "scope": "global", "type": "note",
            "cwd_origin": None, "created": "2026-01-01", "updated": "2026-01-01",
            "expires": "2026-02-01", "source": "historical", "tags": [], "links": [],
            "body": "prior body for " + rid, "strength": 1, "last_accessed": "2026-01-01",
            "injection_flag": 0, "delivery_state": "ordinary", "headline": "fixture",
            "aliases": [], "entities": [], "topics": [], "artifact_refs": [],
            "status": "active", "canonical_id": rid, "superseded_by": None,
            "capsule_version": 1,
        }
        priors[rid] = prior
        prior_bytes = protocol_v2.canonical_bytes(prior)
        mutations.append({
            "record_id": rid, "mutation_ordinal": i,
            "tombstone": {
                "action": "prune", "pending": False,
                "prior_digest": hashlib.sha256(prior_bytes).hexdigest(),
                "record_id": rid,
            },
        })
    frontiers = [{"record_id": rid, "heads": []} for rid in record_ids]
    op = protocol_v2.build_operation({
        "protocol_major": 2, "schema_minor": 0, "replica_id": replica, "counter": counter,
        "parents": [], "project_key": project_key, "kind": "tombstone",
        "frontiers": frontiers, "mutations": mutations,
        "provenance": {"actor": "test", "reason": "historical-fixture", "source": "test"},
    })
    sync_v2.record_local_operation(con, op, installation_fingerprint=fp)
    for rid in record_ids:
        sync_v2.record_graveyard_evidence(
            con, op["op_id"], rid, "prune", protocol_v2.canonical_bytes(priors[rid]))
    result = protocol_v2.fold_operations(mem._sync_envelopes(con))
    mem._apply_fold(con, result, None, "", record_peer=False)
    con.commit()
finally:
    con.close()
with open(out_path, "w") as handle:
    json.dump({"op_id": op["op_id"], "priors": priors, "replica": replica}, handle)
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class RepairBlockedCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = self.root / "store"
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "MEM_WRITE_EVENTS": str(self.root / "state/write-events.jsonl"),
            "MEM_PROFILE": str(self.root / "profile"),
            "AGENT_HOME": str(ROOT),
            "MEM_SYNC_REMOTE": "0", "MEM_DUMP_PUSH": "0",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "MEM_STORE": str(self.store),
            "MEM_INIT": "1",
            "MEM_PROJECTS": str(self.root / "projects"),
            "CODEX_SESSIONS": str(self.root / "codex-sessions"),
        }
        for key, suffix in {"TMPDIR":"tmp", "CODEX_HOME":"codex", "CLAUDE_CONFIG_DIR":"claude",
                            "HARNESS_STATE_ROOT":"runtime-state", "MEM_RECALL_EVENTS":"state/recall.jsonl",
                            "MEM_RECALL_RECEIPTS":"state/recall", "GIT_CONFIG_GLOBAL":"gitconfig",
                            "AGENT_MODEL_WORKER_STATE_ROOT":"governor"}.items():
            self.env[key] = str(self.root / suffix)
        self.env.update(AGENT_MODEL_WORKERS_DISABLED="1", AGENT_SESSION_ROLE="worker", MEM_DUMP_COMMIT="0")
        for key in ("HOME", "TMPDIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "HARNESS_STATE_ROOT"):
            Path(self.env[key]).mkdir(parents=True, exist_ok=True)
        initialized = self.run_mem("index", json_output=False)
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.db = self.store / "memory.db"

    def tearDown(self):
        self.tmp.cleanup()

    def run_mem(self, *args, json_output=True):
        argv = [sys.executable, str(MEM), *args]
        if json_output and "--json" not in argv:
            argv.append("--json")
        return subprocess.run(argv, env=self.env, text=True, capture_output=True)

    def payload(self, result):
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def next_expect(value):
        return value.get("state_digest") or value["extra"]["state_digest"]

    def join_v2(self):
        """Bootstrap this fresh, single-replica store onto a real v2 epoch.

        ``repair-blocked`` requires an authoritatively active, fenced v2 epoch
        (§2.2 A4); ``migration join`` is the supported one-command bootstrap for
        a provably-fresh store (no legacy content, no prior operations), backed
        by a private bare Git remote so it never touches a real exchange
        location. Every ``RepairBlockedCliTest`` test that expects
        ``repair-blocked`` to actually apply must call this before seeding its
        blocked-tombstone fixture.
        """
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       text=True, capture_output=True)
        checkout = self.root / "exchange"
        self.env.update({"MEM_SYNC_REMOTE_URL": str(remote),
                         "MEM_SYNC_REF": "refs/heads/hearting-memory-v2",
                         "MEM_SYNC_DIR": str(checkout)})
        result = self.run_mem("migration", "join", "--epoch", EPOCH, "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.payload(result)

    def seed_blocked_tombstone(self, record_ids, *, project_key="global"):
        out_path = self.root / f"seed-{'-'.join(record_ids)}.json"
        result = subprocess.run(
            [sys.executable, "-c", SEED_FIXTURE, str(MEM.parent), project_key,
             str(out_path), *record_ids],
            env=self.env, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(out_path.read_text())

    def prepare_snapshot(self, *, prefix):
        # Supported snapshot leaf avoids starting a new, incomplete cutover
        # over an already joined v2 epoch merely to obtain a backup.
        script = r"""
import sys,json
sys.path.insert(0,sys.argv[1])
import mem,sync_v2,migration_v2
con=mem.get_con()
replica,counter=con.execute("SELECT replica_id,counter FROM sync_replica WHERE active=1").fetchone()
frontier=sync_v2.capture_frontier(con)
con.close()
member={"replica_id":replica,"logical_project_keys":["global"],
 "protected_ref":"refs/heads/hearting-memory-v2",
 "writer_capability_hash":json.loads(sys.argv[4])["writer_capability_hash"]}
roster=migration_v2.seal_membership(epoch_id=sys.argv[2],member_manifests=[member])
migration_v2.create_snapshot(db_path=mem.DB,epoch_id=sys.argv[2],membership=roster,
 replica_id=replica,out=sys.argv[3],apply=True,capture_enabled=True,
 snapshot_capture_seq=frontier,db_high_watermark=frontier,outbox_counter=int(counter))
"""
        out = self.root / (prefix + "-snapshot")
        result = subprocess.run([sys.executable, "-c", script, str(MEM.parent), EPOCH, str(out), self.run_mem("migration", "capabilities", "--epoch", EPOCH).stdout],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return out / "snapshot.json"

    def prepare_incomplete_snapshot(self, *, prefix):
        capability = self.payload(self.run_mem("migration", "capabilities", "--epoch", EPOCH))
        con = sqlite3.connect(self.db)
        try:
            replica = con.execute(
                "SELECT replica_id FROM sync_replica WHERE active=1").fetchone()[0]
        finally:
            con.close()
        member_path = self.root / f"{prefix}-member.json"
        member_path.write_text(canonical({
            "replica_id": replica, "logical_project_keys": ["global"],
            "protected_ref": "refs/heads/hearting-memory-v2",
            "writer_capability_hash": capability["writer_capability_hash"],
        }), encoding="utf-8")
        expect = self.payload(self.run_mem("migration", "status", "--epoch", EPOCH))["state_digest"]
        membership_out = self.root / f"{prefix}-membership"
        receipt = self.payload(self.run_mem(
            "migration", "roster", "membership-seal", "--epoch", EPOCH,
            "--expect", expect, "--member", str(member_path),
            "--out", str(membership_out), "--apply"))
        expect = self.next_expect(receipt)
        snapshot_out = self.root / f"{prefix}-snapshot"
        for _ in range(2):
            result = self.run_mem(
                "migration", "snapshot", "--epoch", EPOCH, "--expect", expect,
                "--membership", str(membership_out / "membership.json"),
                "--replica", replica, "--store", str(self.store),
                "--out", str(snapshot_out), "--apply")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            receipt = self.payload(result)
            expect = self.next_expect(receipt)
        return snapshot_out / "snapshot.json"

    def db_state(self):
        con = sqlite3.connect(self.db)
        try:
            tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            counter = con.execute("SELECT counter FROM sync_replica WHERE active=1").fetchone()
            return {
                "schema": con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
                "all_tables": {name: sorted(con.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr) for name in tables},
                "objects": sorted(row[0] for row in con.execute("SELECT op_id FROM sync_objects")),
                "records": con.execute("SELECT * FROM records ORDER BY id").fetchall(),
                "frontier": sorted(con.execute(
                    "SELECT record_id,op_id FROM sync_frontier").fetchall()),
                "outbox": sorted(con.execute(
                    "SELECT op_id,state FROM sync_outbox").fetchall()),
                "graveyard": sorted(con.execute(
                    "SELECT destructive_op_id,record_id,evidence_digest "
                    "FROM sync_transactional_graveyard").fetchall()),
                "counter": counter[0] if counter else None,
                "user_version": con.execute("PRAGMA user_version").fetchone()[0],
            }
        finally:
            con.close()

    # -- #11/#17: happy path ------------------------------------------------
    @staticmethod
    def snapshot_tree(snapshot):
        return {p.relative_to(snapshot.parent).as_posix(): p.read_bytes()
                for p in snapshot.parent.rglob("*") if p.is_file()}

    def snapshot_refusal(self, snapshot, *args, **kwargs):
        before_tree, before_db = self.snapshot_tree(snapshot), self.db_state()
        result = self.run_mem(*args, **kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before_tree, self.snapshot_tree(snapshot))
        self.assertEqual(before_db, self.db_state())
        return result

    def test_same_inode_changed_output_staging_is_preserved(self):
        import stat
        from unittest.mock import patch
        import mem
        target = self.root / "output.json"
        fsync, seen = os.fsync, []
        foreign = b"FOREIGN SAME INODE TEMP"
        def syncing(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode) and not seen:
                path = Path(os.readlink(f"/proc/self/fd/{fd}"))
                inode = path.stat().st_ino
                path.write_bytes(foreign)
                seen.append((path, inode, path.stat().st_ino))
                raise OSError("injected after foreign staging mutation")
            return fsync(fd)
        with patch.object(os, "fsync", side_effect=syncing):
            with self.assertRaisesRegex(mem.repair_v2.RepairRefusal, "staging-ownership-conflict"):
                mem._repair_atomic_write(target, {"safe": "payload"})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], seen[0][2])
        self.assertEqual(seen[0][0].read_bytes(), foreign)
        self.assertFalse(target.exists())

    def test_invalid_preplan_fence_proof_and_postplan_changes_are_refused(self):
        seed, snapshot, path, plan = self.make_plan("proof")
        for proof in (None, "changed-unverified-proof", "f" * 64, "sha256:" + "f" * 64):
            with self.subTest(proof=proof):
                with sqlite3.connect(self.db) as con:
                    con.execute("UPDATE sync_migration_epoch SET fence_proof=? WHERE current=1", (proof,))
                planned = self.snapshot_refusal(snapshot, "repair-blocked", "plan",
                    "--snapshot", str(snapshot), "--op-id", seed["op_id"],
                    "--reason", "invalid proof", "--out", str(self.root / "refused.json"))
                self.assertIn("fence-evidence-invalid", planned.stderr)
                self.assertFalse((self.root / "refused.json").exists())
                applied = self.snapshot_refusal(snapshot, "repair-blocked", "apply",
                    "--plan", str(path), "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
                self.assertIn("fence-evidence-invalid", applied.stderr)

    def test_valid_fence_evidence_remains_bound_by_postplan_cas(self):
        seed, snapshot, path, plan = self.make_plan("authority-cas")
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE sync_migration_epoch SET fence_activated_at='changed metadata' WHERE current=1")
        result = self.snapshot_refusal(snapshot, "repair-blocked", "apply",
            "--plan", str(path), "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertIn("store-binding-mismatch", result.stderr)

    def rotated_fresh_plan(self, rotations):
        joined = self.join_v2()
        lineage = [joined["replica_id"]]
        for _ in range(rotations):
            rotated = self.run_mem("replica", "rotate", "--reason", "isolated repair regression", json_output=False)
            self.assertEqual(rotated.returncode, 0, rotated.stdout + rotated.stderr)
            status = self.payload(self.run_mem("replica", "status"))
            self.assertEqual(status["predecessor_replica_id"], lineage[-1])
            self.assertNotIn(status["replica_id"], lineage)
            lineage.append(status["replica_id"])
        seed = self.seed_blocked_tombstone(["rotation-r1", "rotation-r2"])
        snapshot = self.prepare_snapshot(prefix="rotated-fresh")
        path = self.root / "rotation-plan.json"
        planned = self.run_mem("repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "supported rotation", "--out", str(path))
        self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
        plan = json.loads(path.read_text())
        self.assertEqual(plan["store"]["replica_id"], lineage[-1])
        return lineage, seed, snapshot, path, plan

    def assert_rotated_fresh_repair(self, rotations):
        lineage, seed, snapshot, path, plan = self.rotated_fresh_plan(rotations)
        tree, before = self.snapshot_tree(snapshot), self.db_state()
        applied = self.run_mem("repair-blocked", "apply", "--snapshot", str(snapshot),
            "--plan", str(path), "--expect", plan["plan_digest"])
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        receipt = self.payload(applied)
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual(len(receipt["new_op_ids"]), 4)
        self.assertEqual(tree, self.snapshot_tree(snapshot))
        after = self.db_state()
        for name, rows in before["all_tables"].items():
            if name.startswith("sync_migration_"):
                self.assertEqual(rows, after["all_tables"][name])
        with sqlite3.connect(self.db) as con:
            raw = {row[0]: bytes(row[1]) for row in con.execute(
                "SELECT op_id,payload_bytes FROM sync_objects")}
        folded = protocol_v2.fold_operations([
            {"op_id": oid, "payload": protocol_v2.canonical_loads(value)}
            for oid, value in raw.items()])
        self.assertIn(seed["op_id"], protocol_v2.resolved_blocked_by(folded))
        self.assertEqual(after["records"], before["records"])
        for rid in ("rotation-r1", "rotation-r2"):
            self.assertNotIn(rid, folded.records)
        for oid in receipt["new_op_ids"]:
            self.assertEqual(protocol_v2.canonical_loads(raw[oid])["replica_id"], lineage[-1])
        before_objects = {row[0]: row for row in before["all_tables"]["sync_objects"]}
        after_objects = {row[0]: row for row in after["all_tables"]["sync_objects"]}
        self.assertTrue(before_objects.items() <= after_objects.items())

    def test_fresh_repair_after_one_supported_rotation(self):
        self.assert_rotated_fresh_repair(1)

    def test_fresh_repair_after_two_supported_rotations(self):
        self.assert_rotated_fresh_repair(2)

    def test_fresh_rotation_invalid_ancestry_and_split_seals_refuse(self):
        lineage, seed, snapshot, path, plan = self.rotated_fresh_plan(2)
        origin, middle, current = lineage
        foreign = "e" * 32
        with sqlite3.connect(self.db) as con:
            replicas = con.execute("SELECT * FROM sync_replica").fetchall()
            proofs = con.execute("SELECT no_tail_digest,fence_proof FROM sync_migration_epoch WHERE current=1").fetchone()

        def proof(kind, replica):
            # Canonical producer shape, independently constructed so a valid
            # digest for the wrong member cannot pass as a malformed fixture.
            value = {"kind": kind, "epoch_id": EPOCH, "replica_id": replica,
                "protected_ref": self.env["MEM_SYNC_REF"], "object_count": 0,
                "legacy_nonempty": False}
            return hashlib.sha256(canonical(value).encode()).hexdigest()

        self.assertEqual(proofs, (proof("fresh-store-join", origin),
                                  proof("fresh-store-fence", origin)))
        cases = {
            "missing-predecessor": ("UPDATE sync_replica SET predecessor_replica_id=? WHERE replica_id=?", ("d" * 32, middle)),
            "malformed-predecessor": ("UPDATE sync_replica SET predecessor_replica_id=? WHERE replica_id=?", ("not-a-replica", middle)),
            "self-cycle": ("UPDATE sync_replica SET predecessor_replica_id=? WHERE replica_id=?", (current, current)),
            "cycle-below-matching-producer": ("UPDATE sync_replica SET predecessor_replica_id=? WHERE replica_id=?", (middle, origin)),
            "unretired-ancestor": ("UPDATE sync_replica SET retired_at=NULL WHERE replica_id=?", (origin,)),
            "retired-current": ("UPDATE sync_replica SET retired_at='fixture' WHERE replica_id=?", (current,)),
            "missing-active": ("UPDATE sync_replica SET active=0 WHERE replica_id=?", (current,)),
            "split-seals": ("UPDATE sync_migration_epoch SET fence_proof=? WHERE current=1", (proof("fresh-store-fence", middle),)),
            "forged-seal": ("UPDATE sync_migration_epoch SET fence_proof=? WHERE current=1", ("f" * 64,)),
            "foreign-seals": ("UPDATE sync_migration_epoch SET no_tail_digest=?,fence_proof=? WHERE current=1",
                (proof("fresh-store-join", foreign), proof("fresh-store-fence", foreign))),
        }
        for label, (sql, values) in cases.items():
            with self.subTest(case=label):
                with sqlite3.connect(self.db) as con:
                    con.execute("INSERT INTO sync_replica(replica_id,active,retired_at) VALUES (?,0,'fixture')", (foreign,))
                    con.execute(sql, values)
                try:
                    for args in (("plan", "--op-id", seed["op_id"], "--reason", label, "--out", "-"),
                                 ("apply", "--plan", str(path), "--expect", plan["plan_digest"])):
                        refused = self.snapshot_refusal(snapshot, "repair-blocked", *args, "--snapshot", str(snapshot))
                        self.assertIn("fence-evidence-invalid", refused.stderr)
                finally:
                    # Restore only deliberately corrupted synthetic metadata.
                    with sqlite3.connect(self.db) as con:
                        con.execute("DELETE FROM sync_replica WHERE replica_id=?", (foreign,))
                        con.executemany("INSERT OR REPLACE INTO sync_replica VALUES (?,?,?,?,?,?,?)", replicas)
                        con.execute("UPDATE sync_migration_epoch SET no_tail_digest=?,fence_proof=? WHERE current=1", proofs)

    def test_fresh_rotation_preserves_postplan_lineage_and_current_replica_cas(self):
        lineage, seed, snapshot, path, plan = self.rotated_fresh_plan(1)
        with sqlite3.connect(self.db) as con:
            retired = con.execute("SELECT retired_at FROM sync_replica WHERE replica_id=?", (lineage[0],)).fetchone()[0]
            con.execute("UPDATE sync_replica SET retired_at='changed but still retired' WHERE replica_id=?", (lineage[0],))
        refused = self.snapshot_refusal(snapshot, "repair-blocked", "apply", "--snapshot", str(snapshot),
            "--plan", str(path), "--expect", plan["plan_digest"])
        self.assertIn("plan-stale", refused.stderr)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE sync_replica SET retired_at=? WHERE replica_id=?", (retired, lineage[0]))
        rotated = self.run_mem("replica", "rotate", "--reason", "post-plan rotation", json_output=False)
        self.assertEqual(rotated.returncode, 0, rotated.stdout + rotated.stderr)
        refused = self.snapshot_refusal(snapshot, "repair-blocked", "apply", "--snapshot", str(snapshot),
            "--plan", str(path), "--expect", plan["plan_digest"])
        self.assertIn("snapshot-identity-mismatch", refused.stderr)
        fresh = self.prepare_snapshot(prefix="postplan-rotation")
        planned = self.run_mem("repair-blocked", "plan", "--snapshot", str(fresh),
            "--op-id", seed["op_id"], "--reason", "new current replica", "--out", "-")
        self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)

    def test_repair_after_supported_completed_snapshot_cutover(self):
        # Reuse the supported public-CLI cutover fixture through activation.
        # The local sentinel stops its later sync/rollback scenario, after the
        # real activation command has durably completed; no result is mocked.
        import test_mem_migration_v2 as cutover_tests
        fixture = cutover_tests.MigrationCliTest()
        fixture.root, fixture.store, fixture.db, fixture.env = self.root, self.store, self.db, self.env
        run_mem = fixture.run_mem
        class Activated(Exception):
            pass
        def until_activation(*args, **kwargs):
            result = run_mem(*args, **kwargs)
            if (args[:2] == ("migration", "activate") and result.returncode == 0
                    and json.loads(result.stdout).get("migration_state") == "v2-only-enabled"):
                raise Activated()
            return result
        fixture.run_mem = until_activation
        with self.assertRaises(Activated):
            fixture.test_public_cutover_e2e_through_v2_activation()
        with sqlite3.connect(self.db) as con:
            epoch = con.execute("SELECT seed_mode,state,fence_proof FROM sync_migration_epoch WHERE current=1").fetchone()
            self.assertEqual(epoch[:2], ("snapshot", "active"))
            for table in ("sync_migration_members", "sync_migration_seed_map", "sync_migration_equality"):
                self.assertGreater(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], 0)
            evidence_before = {name: con.execute(f'SELECT * FROM "{name}"').fetchall()
                for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sync_migration_%'")}
        # Repair consumes the full-roster DB seal, without demanding local
        # copies of every remote fence receipt. Preserve this fixture's raw
        # local receipt beside its original path to exercise that boundary.
        with sqlite3.connect(self.db) as con:
            fence_path = Path(con.execute("SELECT local_path FROM sync_migration_artifacts "
                "WHERE artifact_kind='fence'").fetchone()[0])
        held_fence = fence_path.with_suffix(".held")
        fence_path.rename(held_fence)
        seed = self.seed_blocked_tombstone(["cutover-r1", "cutover-r2"])
        snapshot = self.prepare_snapshot(prefix="after-cutover")
        tree = self.snapshot_tree(snapshot)
        planned = self.run_mem("repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "completed cutover repair", "--out", str(self.root / "cutover-plan.json"))
        self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
        plan = json.loads((self.root / "cutover-plan.json").read_text())
        applied = self.run_mem("repair-blocked", "apply", "--snapshot", str(snapshot),
            "--plan", str(self.root / "cutover-plan.json"), "--expect", plan["plan_digest"])
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(self.payload(applied)["status"], "applied")
        self.assertEqual(len(self.payload(applied)["new_op_ids"]), 4)
        self.assertEqual(tree, self.snapshot_tree(snapshot))
        with sqlite3.connect(self.db) as con:
            for name, rows in evidence_before.items():
                self.assertEqual(rows, con.execute(f'SELECT * FROM "{name}"').fetchall())
        # Ready flags plus a forged digest are also insufficient in snapshot mode.
        for proof in (None, "f" * 64):
            with sqlite3.connect(self.db) as con:
                con.execute("UPDATE sync_migration_epoch SET fence_proof=? WHERE current=1", (proof,))
            result = self.snapshot_refusal(snapshot, "repair-blocked", "plan", "--snapshot", str(snapshot),
                "--op-id", seed["op_id"], "--reason", "invalid snapshot fence", "--out", "-")
            self.assertIn("fence-evidence-invalid", result.stderr)

        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE sync_migration_epoch SET fence_proof=? WHERE current=1", (epoch[2],))
            raw = con.execute("SELECT manifest_bytes FROM sync_migration_seals "
                "WHERE seal_kind='evidence'").fetchone()[0]
            sealed = json.loads(bytes(raw))
            sealed["replicas"][0]["fence_digest"] = "f" * 64
            import migration_v2
            sealed["manifest_digest"] = migration_v2.digest_json(
                {key: value for key, value in sealed.items() if key != "manifest_digest"})
            con.execute("UPDATE sync_migration_seals SET manifest_bytes=? WHERE seal_kind='evidence'",
                        (migration_v2.canonical_bytes(sealed),))
        result = self.snapshot_refusal(snapshot, "repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "forged sealed fence", "--out", "-")
        self.assertIn("fence-evidence-invalid", result.stderr)
        held_fence.rename(fence_path)

    def test_11_plan_then_apply_resolves_and_preserves_raw_blocked(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1", "r2"])
        snapshot = self.prepare_snapshot(prefix="happy")
        plan_out = self.root / "plan.json"
        planned = self.run_mem(
            "repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "test-recovery", "--out", str(plan_out))
        self.assertEqual(planned.returncode, 0, planned.stderr + planned.stdout)
        plan = json.loads(plan_out.read_text())
        self.assertEqual(plan["counts"], {"targets": 1, "records": 2, "new_operations": 4})
        # plan must never leak record bodies.
        self.assertNotIn("prior body for r1", plan_out.read_text())

        before = self.db_state()
        applied = self.run_mem(
            "repair-blocked", "apply", "--plan", str(plan_out),
            "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertEqual(applied.returncode, 0, applied.stderr + applied.stdout)
        result_payload = self.payload(applied)
        self.assertEqual(result_payload["status"], "applied")
        self.assertEqual(len(result_payload["new_op_ids"]), 4)

        con = sqlite3.connect(self.db)
        try:
            envelopes = [{"op_id": row[0], "payload": protocol_v2.canonical_loads(bytes(row[1]))}
                        for row in con.execute("SELECT op_id,payload_bytes FROM sync_objects")]
        finally:
            con.close()
        folded = protocol_v2.fold_operations(envelopes)
        self.assertEqual(folded.blocked[seed["op_id"]].code, "blocked-prior-evidence")
        self.assertIn(seed["op_id"], protocol_v2.resolved_blocked_by(folded))
        self.assertNotIn("r1", folded.records)
        self.assertNotIn("r2", folded.records)
        after = self.db_state()
        self.assertEqual(len(after["objects"]), len(before["objects"]) + 4)
        self.assertEqual(after["records"], before["records"])  # no live-row resurrection

    # -- #12/#12b: retry and forged receipts --------------------------------
    def test_12_repeated_apply_is_already_applied_not_stale(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="retry")
        plan_out = self.root / "plan.json"
        self.run_mem("repair-blocked", "plan", "--snapshot", str(snapshot),
                     "--op-id", seed["op_id"], "--reason", "r", "--out", str(plan_out))
        plan = json.loads(plan_out.read_text())
        first = self.run_mem("repair-blocked", "apply", "--plan", str(plan_out),
                             "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertEqual(first.returncode, 0, first.stderr)
        state_after_first = self.db_state()

        # An unrelated write happens in between -- this must not turn a
        # legitimate retry into `plan-stale` (§2.4's (2)-before-(3) contract).
        unrelated = self.run_mem("add", "durable", "note", "unrelated record",
                                 "--scope", "global", json_output=False)
        self.assertEqual(unrelated.returncode, 0, unrelated.stderr)
        immediate_pre_retry = self.db_state()

        second = self.run_mem("repair-blocked", "apply", "--plan", str(plan_out),
                              "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.payload(second)["status"], "already-applied")
        state_after_second = self.db_state()
        self.assertEqual(immediate_pre_retry, state_after_second)
        for key in ("objects", "frontier", "outbox", "graveyard", "counter"):
            if key == "objects":
                # the unrelated add legitimately grew sync_objects by exactly one.
                self.assertEqual(len(state_after_second[key]), len(state_after_first[key]) + 1)
            elif key == "counter":
                self.assertGreater(state_after_second[key], state_after_first[key])
            else:
                self.assertNotEqual(state_after_second[key], None)

    def test_12b_receipt_missing_new_op_is_not_treated_as_applied(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="forged")
        plan_out = self.root / "plan.json"
        self.run_mem("repair-blocked", "plan", "--snapshot", str(snapshot),
                     "--op-id", seed["op_id"], "--reason", "r", "--out", str(plan_out))
        plan = json.loads(plan_out.read_text())
        applied = self.run_mem("repair-blocked", "apply", "--plan", str(plan_out),
                               "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertEqual(applied.returncode, 0, applied.stderr)

        con = sqlite3.connect(self.db)
        try:
            con.execute("UPDATE sync_repair_receipts SET new_op_ids=? WHERE plan_digest=?",
                       (json.dumps(["0" * 64, "1" * 64]), plan["plan_digest"]))
            con.commit()
        finally:
            con.close()
        retried = self.run_mem("repair-blocked", "apply", "--plan", str(plan_out),
                               "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertEqual(retried.returncode, 2, retried.stdout)
        self.assertIn("receipt-evidence-incomplete", retried.stderr)

    # -- #12c: no init/migration side effects -------------------------------
    def test_12c_missing_store_and_wrong_schema_never_create_or_migrate(self):
        missing_store = self.root / "does-not-exist" / "memory.db"
        result = subprocess.run(
            [sys.executable, str(MEM), "repair-blocked", "plan",
             "--snapshot", "irrelevant.json", "--op-id", "a" * 64,
             "--reason", "r", "--out", str(self.root / "p.json")],
            env={**self.env, "MEM_STORE": str(missing_store.parent)},
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("store-absent", result.stderr)
        self.assertFalse(missing_store.parent.exists())

        con = sqlite3.connect(self.db)
        try:
            con.execute("PRAGMA user_version=999999")
            con.commit()
        finally:
            con.close()
        result = self.run_mem("repair-blocked", "plan", "--snapshot", "irrelevant.json",
                              "--op-id", "a" * 64, "--reason", "r",
                              "--out", str(self.root / "p2.json"), json_output=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("store-schema-unsupported", result.stderr)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 999999)
        finally:
            con.close()

    # -- #13: CAS mismatch leaves logical state untouched -------------------
    def test_13_expect_mismatch_reserves_no_counter_and_changes_nothing(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="cas")
        plan_out = self.root / "plan.json"
        self.run_mem("repair-blocked", "plan", "--snapshot", str(snapshot),
                     "--op-id", seed["op_id"], "--reason", "r", "--out", str(plan_out))
        plan_bytes_before = plan_out.read_bytes()
        before = self.db_state()
        before_tree = self.snapshot_tree(snapshot)
        rejected = self.run_mem("repair-blocked", "apply", "--plan", str(plan_out),
                                "--expect", "0" * 64, "--snapshot", str(snapshot))
        self.assertEqual(rejected.returncode, 2, rejected.stdout)
        self.assertIn("plan-digest-mismatch", rejected.stderr)
        after = self.db_state()
        self.assertEqual(before, after)
        self.assertEqual(plan_out.read_bytes(), plan_bytes_before)
        self.assertEqual(before_tree, self.snapshot_tree(snapshot))

    # -- #14: crash injection at the first durable write --------------------
    def test_14_post_put_later_pair_and_pre_receipt_roll_back_every_table(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1", "r2"])
        snapshot = self.prepare_snapshot(prefix="crash")
        plan_out = self.root / "plan.json"
        self.run_mem("repair-blocked", "plan", "--snapshot", str(snapshot),
                     "--op-id", seed["op_id"], "--reason", "r", "--out", str(plan_out))
        plan = json.loads(plan_out.read_text())
        before = self.db_state()
        snapshot_bytes = {p.name: p.read_bytes() for p in snapshot.parent.iterdir() if p.is_file()}

        crash_fixture = r"""
import sys
sys.path.insert(0, sys.argv[1])
import mem, sync_v2

original = sync_v2.record_local_operation
count = 0
def boom(*a, **k):
    global count
    result = original(*a, **k)
    count += 1
    if count == int(sys.argv[5]):
        assert a[0].execute('SELECT COUNT(*) FROM sync_objects').fetchone()[0] > 1
        raise RuntimeError("injected crash after real write")
    return result
sync_v2.record_local_operation = boom
if sys.argv[5] == '0':
    def before_receipt(*a, **k):
        assert count == 4
        raise RuntimeError('injected crash before receipt')
    mem._repair_receipt = before_receipt

class Args:
    plan = sys.argv[2]; expect = sys.argv[3]; snapshot = sys.argv[4]; json_output = True

try:
    mem.repair_blocked_apply(Args)
    raise SystemExit("expected a crash")
except RuntimeError as exc:
    assert "injected crash" in str(exc)
"""
        for point in (1, 3, 0):
            with self.subTest(point=point):
                result = subprocess.run(
                    [sys.executable, "-c", crash_fixture, str(MEM.parent),
                     str(plan_out), plan["plan_digest"], str(snapshot), str(point)],
                    env=self.env, text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(before, self.db_state())
                self.assertEqual(snapshot_bytes, {p.name: p.read_bytes() for p in snapshot.parent.iterdir() if p.is_file()})

    # -- #17/#18/#19/#19b/#19c: snapshot verification ------------------------
    def test_17_valid_snapshot_manifest_is_accepted(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="valid")
        manifest = json.loads(snapshot.read_text())
        planned = self.run_mem(
            "repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "r", "--out", str(self.root / "p.json"),
            "--snapshot-sha256", manifest["backup"]["sha256"])
        self.assertEqual(planned.returncode, 0, planned.stderr)

    def test_18_tampered_manifest_is_rejected(self):
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="tampered")
        manifest = json.loads(snapshot.read_text())
        manifest["schema_user_version"] += 1  # self-digest now stale
        snapshot.write_text(canonical(manifest), encoding="utf-8")
        result = self.snapshot_refusal(snapshot, "repair-blocked", "plan", "--snapshot", str(snapshot),
                              "--op-id", seed["op_id"], "--reason", "r",
                              "--out", str(self.root / "p.json"), json_output=False)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("snapshot-manifest-invalid", result.stderr)

    def test_19_corrupted_backup_bytes_are_rejected(self):
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="corrupt")
        backup = snapshot.parent / "snapshot.db"
        raw = bytearray(backup.read_bytes())
        raw[-1] ^= 0xFF
        backup.write_bytes(bytes(raw))
        result = self.snapshot_refusal(snapshot, "repair-blocked", "plan", "--snapshot", str(snapshot),
                              "--op-id", seed["op_id"], "--reason", "r",
                              "--out", str(self.root / "p.json"), json_output=False)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("snapshot-manifest-invalid", result.stderr)

    def test_19c_snapshot_locator_missing_is_rejected_by_argparse(self):
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="missing-locator")
        before_tree, before_db = self.snapshot_tree(snapshot), self.db_state()
        argv = [sys.executable, str(MEM), "repair-blocked", "plan",
               "--op-id", seed["op_id"], "--reason", "r",
               "--out", str(self.root / "p.json")]
        result = subprocess.run(argv, env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--snapshot", result.stderr)

        self.assertEqual(before_tree, self.snapshot_tree(snapshot))
        self.assertEqual(before_db, self.db_state())

    # -- #20: A1 output-path protection ---------------------------------------
    def test_20a_plan_output_cannot_replace_the_live_db_wal_or_snapshot(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="a1")
        for target, label in (
            (self.db, "memory.db"),
            (self.db.with_name(self.db.name + "-wal"), "memory.db-wal"),
            (snapshot, "snapshot manifest"),
        ):
            with self.subTest(target=label):
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(b"pre-existing sentinel bytes")
                before = target.read_bytes()
                result = self.run_mem(
                    "repair-blocked", "plan", "--snapshot", str(snapshot),
                    "--op-id", seed["op_id"], "--reason", "a1-collision",
                    "--out", str(target), json_output=False)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("output-path-", result.stderr)
                self.assertEqual(target.read_bytes(), before)

    def test_20b_plan_output_to_a_fresh_path_still_works(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="a1-ok")
        plan_out = self.root / "fresh-plan.json"
        result = self.run_mem(
            "repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "a1-ok", "--out", str(plan_out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(plan_out.exists())
        # A second plan to the same now-existing path is refused, not replaced.
        retried = self.run_mem(
            "repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "a1-ok-again",
            "--out", str(plan_out), json_output=False)
        self.assertEqual(retried.returncode, 2, retried.stdout)
        self.assertIn("output-path-exists", retried.stderr)

    # -- #21: A2 serving-state CAS ---------------------------------------------
    def test_21a_serving_state_drift_before_plan_is_refused(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1", "r2"])
        snapshot = self.prepare_snapshot(prefix="a2-before")
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "INSERT INTO records(id,tier,scope,type,body,delivery_state,"
                "canonical_id,status) VALUES('r1','durable','global','note',"
                "'synthetic pending state before plan','pending','r1','active')")
            con.commit()
        finally:
            con.close()
        result = self.run_mem(
            "repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "a2-before",
            "--out", str(self.root / "a2-before-plan.json"), json_output=False)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("serving-state-drift", result.stderr)
        con = sqlite3.connect(self.db)
        try:
            row = con.execute(
                "SELECT delivery_state FROM records WHERE id='r1'").fetchone()
        finally:
            con.close()
        self.assertEqual(row, ("pending",))

    def test_21b_serving_state_drift_between_plan_and_apply_is_refused(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1", "r2"])
        snapshot = self.prepare_snapshot(prefix="a2-after")
        plan_out = self.root / "a2-after-plan.json"
        planned = self.run_mem(
            "repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "a2-after", "--out", str(plan_out))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(plan_out.read_text())

        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "INSERT INTO records(id,tier,scope,type,body,delivery_state,"
                "canonical_id,status) VALUES('r1','durable','global','note',"
                "'synthetic pending state after plan','pending','r1','active')")
            con.commit()
            before_count = con.execute(
                "SELECT COUNT(*) FROM records WHERE id='r1'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(before_count, 1)

        applied = self.run_mem(
            "repair-blocked", "apply", "--plan", str(plan_out),
            "--expect", plan["plan_digest"], "--snapshot", str(snapshot),
            json_output=False)
        self.assertEqual(applied.returncode, 2, applied.stdout)
        self.assertIn("serving-state-drift", applied.stderr)
        con = sqlite3.connect(self.db)
        try:
            after_count = con.execute(
                "SELECT COUNT(*) FROM records WHERE id='r1'").fetchone()[0]
            # The refused attempt's own BEGIN IMMEDIATE rolled back in full,
            # so this receipt table -- created lazily inside that same
            # transaction -- may never have durably existed at all.
            has_table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='sync_repair_receipts'").fetchone()
            receipts = (con.execute(
                "SELECT COUNT(*) FROM sync_repair_receipts").fetchone()[0]
                if has_table else 0)
        finally:
            con.close()
        # The drifted row survives untouched -- no silent delete -- and no
        # receipt was ever recorded for this refused attempt.
        self.assertEqual(after_count, 1)
        self.assertEqual(receipts, 0)

    # -- #22: A3 tampered embedded plan_digest at the CLI boundary -------------
    def test_22_tampered_embedded_plan_digest_with_correct_expect_is_refused(self):
        self.join_v2()
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="a3")
        plan_out = self.root / "a3-plan.json"
        planned = self.run_mem(
            "repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "a3", "--out", str(plan_out))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(plan_out.read_text())
        original_digest = plan["plan_digest"]

        tampered = dict(plan)
        tampered["plan_digest"] = "f" * 64
        plan_out.write_text(canonical(tampered), encoding="utf-8")

        before = self.db_state()
        applied = self.run_mem(
            "repair-blocked", "apply", "--plan", str(plan_out),
            "--expect", original_digest, "--snapshot", str(snapshot),
            json_output=False)
        self.assertEqual(applied.returncode, 2, applied.stdout)
        self.assertIn("plan-digest-mismatch", applied.stderr)
        after = self.db_state()
        self.assertEqual(before, after)

    # -- #23: A4 authoritative active-epoch/fence binding -----------------------
    def test_23a_repair_is_refused_on_a_never_joined_store(self):
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="a4-nojoin")
        result = self.run_mem(
            "repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "a4-nojoin",
            "--out", str(self.root / "a4-nojoin-plan.json"), json_output=False)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("epoch-not-active", result.stderr)

    def test_23b_repair_succeeds_once_the_store_has_actually_joined_v2(self):
        joined = self.join_v2()
        self.assertTrue(joined["v2_only"])
        self.assertTrue(joined["old_writer_fence_active"])
        seed = self.seed_blocked_tombstone(["r1"])
        snapshot = self.prepare_snapshot(prefix="a4-joined")
        plan_out = self.root / "a4-joined-plan.json"
        planned = self.run_mem(
            "repair-blocked", "plan", "--snapshot", str(snapshot),
            "--op-id", seed["op_id"], "--reason", "a4-joined", "--out", str(plan_out))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(plan_out.read_text())
        applied = self.run_mem(
            "repair-blocked", "apply", "--plan", str(plan_out),
            "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(self.payload(applied)["status"], "applied")

    def make_plan(self, prefix="extra", records=("r1", "r2")):
        self.join_v2()
        seed = self.seed_blocked_tombstone(records)
        snapshot = self.prepare_snapshot(prefix=prefix)
        path = self.root / (prefix + "-plan.json")
        result = self.run_mem("repair-blocked", "plan", "--snapshot", str(snapshot),
                              "--op-id", seed["op_id"], "--reason", "regression", "--out", str(path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return seed, snapshot, path, json.loads(path.read_text())

    def test_24_incomplete_cutover_before_plan_and_after_plan_is_refused(self):
        seed, snapshot, path, plan = self.make_plan("incomplete")
        self.prepare_incomplete_snapshot(prefix="cutover")
        before = self.db_state()
        for args in (("plan", "--op-id", seed["op_id"], "--reason", "negative", "--out", "-"),
                     ("apply", "--plan", str(path), "--expect", plan["plan_digest"])):
            result = self.run_mem("repair-blocked", *args, "--snapshot", str(snapshot))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("epoch-not-active", result.stderr)
            self.assertEqual(before, self.db_state())

    def test_25_readonly_repetition_no_bodies_and_blocked_exit_one(self):
        seed, snapshot, path, plan = self.make_plan("readonly")
        before = self.db_state()
        snapshot_bytes = {p.name: p.read_bytes() for p in snapshot.parent.iterdir() if p.is_file()}
        for _ in range(2):
            result = self.run_mem("repair-blocked", "plan", "--op-id", seed["op_id"],
                                 "--reason", "readonly", "--snapshot", str(snapshot), "--out", "-")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("prior body", result.stdout)
            for command in (("sync", "status"), ("doctor",)):
                result = self.run_mem(*command)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(before, self.db_state())
            self.assertEqual(snapshot_bytes, {p.name: p.read_bytes() for p in snapshot.parent.iterdir() if p.is_file()})

    def test_26_output_aliases_existing_files_and_new_protected_paths(self):
        seed, snapshot, path, plan = self.make_plan("aliases")
        other = self.root / "unrelated.txt"; other.write_bytes(b"owned by somebody else")
        symlink = self.root / "alias"; symlink.symlink_to(other)
        hardlink = self.root / "hardlink"; os.link(other, hardlink)
        parentlink = self.root / "parent-link"; parentlink.symlink_to(self.root, target_is_directory=True)
        before = self.db_state()
        for out in (other, symlink, hardlink, parentlink / "new-plan", self.store / "memory.db-shm",
                    self.store / "dump.jsonl", self.store / "new-sync-meta", snapshot.parent / "new-output"):
            result = self.run_mem("repair-blocked", "plan", "--op-id", seed["op_id"],
                                  "--reason", "negative", "--snapshot", str(snapshot), "--out", str(out))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(other.read_bytes(), b"owned by somebody else")
            self.assertEqual(before, self.db_state())

    def test_27_forged_resigned_epoch_backup_and_different_locator(self):
        seed, snapshot, path, plan = self.make_plan("forgery")
        import migration_v2
        original = snapshot.read_bytes()
        before = self.db_state()
        forged = json.loads(original)
        forged["epoch_id"] = "f" * 32
        forged["manifest_digest"] = migration_v2.digest_json({k:v for k,v in forged.items() if k != "manifest_digest"})
        snapshot.write_text(canonical(forged))
        result = self.snapshot_refusal(snapshot, "repair-blocked", "apply", "--plan", str(path), "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(before, self.db_state())
        snapshot.write_bytes(original)
        result = self.run_mem("add", "durable", "note", "An unrelated accepted record changes the valid snapshot contents.", "--scope", "global", json_output=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        other = self.prepare_snapshot(prefix="different-valid")
        before = self.db_state()
        original_tree = self.snapshot_tree(snapshot)
        result = self.snapshot_refusal(other, "repair-blocked", "apply", "--plan", str(path), "--expect", plan["plan_digest"], "--snapshot", str(other))
        self.assertEqual(original_tree, self.snapshot_tree(snapshot))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(before, self.db_state())

    def test_28_repaired_publication_failure_retry_independent_reader_and_restore(self):
        seed, snapshot, path, plan = self.make_plan("publication", ("r1", "r2", "r3"))
        # Add a supported pending obligation before making the final plan.
        pending_script = "import sys;sys.path.insert(0,sys.argv[1]);import mem;print(mem.write_record('durable','global','note','Pending obligation survives recovery and exchange.',quiet=True,requires_consume=True))"
        pending = subprocess.run([sys.executable, "-c", pending_script, str(MEM.parent)],
                                 env=self.env, text=True, capture_output=True)
        self.assertEqual(pending.returncode, 0, pending.stderr)
        pending_id = pending.stdout.strip()
        planned = self.run_mem("repair-blocked", "plan", "--snapshot", str(snapshot),
                              "--op-id", seed["op_id"], "--reason", "with pending", "--out", "-")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout); path.write_text(canonical(plan))
        before = self.db_state()
        snapshot_bytes = snapshot.read_bytes(), (snapshot.parent / "snapshot.db").read_bytes()
        applied = self.run_mem("repair-blocked", "apply", "--plan", str(path), "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertEqual(applied.returncode, 0, applied.stderr)
        new_ids = self.payload(applied)["new_op_ids"]
        self.assertEqual(len(new_ids), 2 * len(seed["priors"]))
        state = self.db_state()
        self.assertEqual(before["records"], state["records"])
        self.assertEqual(snapshot_bytes, (snapshot.read_bytes(), (snapshot.parent / "snapshot.db").read_bytes()))
        receipt = state["all_tables"]["sync_repair_receipts"]
        self.env["MEM_SYNC_REMOTE"] = "1"
        failure_script = r"""
import sys
sys.path.insert(0,sys.argv[1])
import mem,git_exchange_v2
method=sys.argv[2]
def fail(*args,**kwargs):
    raise git_exchange_v2.ExchangeUnavailable('injected publication/confirmation failure')
setattr(git_exchange_v2.GitExchange,method,fail)
raise SystemExit(mem.sync(json_output=True,exchange_only=True))
"""
        for method in ("publish_operations", "confirm_validated_snapshot"):
            failed = subprocess.run([sys.executable, "-c", failure_script, str(MEM.parent), method],
                                    env=self.env, text=True, capture_output=True)
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            now = self.db_state()
            self.assertEqual(receipt, now["all_tables"]["sync_repair_receipts"])
            self.assertEqual(before["records"], now["records"])
            self.assertTrue(any(row[1] != "confirmed" for row in now["outbox"] if row[0] in new_ids))
        success = self.run_mem("sync", "--exchange-only")
        self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
        self.assertEqual(receipt, self.db_state()["all_tables"]["sync_repair_receipts"])
        remote = self.root / "remote.git"
        tip = subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/hearting-memory-v2"], env=self.env, text=True).strip()
        tree = subprocess.check_output(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", tip], env=self.env, text=True)
        for oid in new_ids:
            self.assertIn(oid + ".json", tree)
        # Every published revision containing one repair operation contains all.
        revisions = subprocess.check_output(["git", "--git-dir", str(remote), "rev-list", tip], env=self.env, text=True).splitlines()
        for revision in revisions:
            names = subprocess.check_output(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", revision], env=self.env, text=True)
            present = [oid for oid in new_ids if oid + ".json" in names]
            self.assertIn(len(present), (0, len(new_ids)))
        reader = RepairBlockedCliTest()
        reader.setUp()
        try:
            reader.env.update(MEM_SYNC_REMOTE_URL=str(remote), MEM_SYNC_REF="refs/heads/hearting-memory-v2",
                              MEM_SYNC_DIR=str(reader.root / "exchange"), MEM_SYNC_REMOTE="1")
            joined = reader.run_mem("migration", "join", "--apply")
            self.assertEqual(joined.returncode, 0, joined.stdout + joined.stderr)
            readback = reader.run_mem("sync", "--exchange-only")
            self.assertEqual(readback.returncode, 0, readback.stdout + readback.stderr)
            con = sqlite3.connect(reader.db)
            try:
                envelopes = [{"op_id":row[0], "payload":protocol_v2.canonical_loads(bytes(row[1]))}
                             for row in con.execute("SELECT op_id,payload_bytes FROM sync_objects")]
                folded = protocol_v2.fold_operations(envelopes)
                self.assertEqual(set(folded.blocked) - set(protocol_v2.resolved_blocked_by(folded)), set())
                self.assertIn(seed["op_id"], folded.blocked)
                for rid in seed["priors"]:
                    self.assertNotIn(rid, folded.records)
                self.assertEqual(con.execute("SELECT delivery_state FROM records WHERE id=?", (pending_id,)).fetchone(), ("pending",))
            finally:
                con.close()
            restored = reader.run_mem("restore", "r1", json_output=False)
            self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
            con = sqlite3.connect(reader.db)
            try:
                self.assertEqual(con.execute("SELECT body FROM records WHERE id='r1'").fetchone(), (seed["priors"]["r1"]["body"],))
                self.assertEqual(con.execute("SELECT COUNT(*) FROM records WHERE id IN ('r2','r3')").fetchone()[0], 0)
            finally:
                con.close()
        finally:
            reader.tearDown()

    def test_29_truncated_backup_and_resigned_payload_forgery_are_refused(self):
        import migration_v2
        seed, snapshot, path, plan = self.make_plan("payload-forgery")
        backup = snapshot.parent / "snapshot.db"
        original_manifest, original_backup = snapshot.read_bytes(), backup.read_bytes()
        before = self.db_state()
        backup.write_bytes(original_backup[:len(original_backup)//2])
        result = self.snapshot_refusal(snapshot, "repair-blocked", "apply", "--plan", str(path), "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(before, self.db_state())
        backup.write_bytes(original_backup)
        con = sqlite3.connect(backup)
        try:
            con.execute("UPDATE sync_objects SET payload_bytes=? WHERE op_id=?", (b'{}', seed["op_id"]))
            con.commit()
            dumped = migration_v2._dump(con)
        finally:
            con.close()
        forged = json.loads(original_manifest)
        raw = backup.read_bytes()
        forged["backup"].update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        forged["deterministic_dump"] = {"sha256":hashlib.sha256(dumped).hexdigest(), "bytes":len(dumped)}
        forged["manifest_digest"] = migration_v2.digest_json({k:v for k,v in forged.items() if k != "manifest_digest"})
        snapshot.write_text(canonical(forged))
        plan["snapshot"].update(manifest_digest=forged["manifest_digest"], backup_sha256=forged["backup"]["sha256"], backup_bytes=len(raw))
        plan["plan_digest"] = hashlib.sha256(protocol_v2.canonical_bytes({k:v for k,v in plan.items() if k != "plan_digest"})).hexdigest()
        path.write_text(canonical(plan))
        result = self.snapshot_refusal(snapshot, "repair-blocked", "apply", "--plan", str(path), "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("snapshot-target-payload-mismatch", result.stderr)
        self.assertEqual(before, self.db_state())

    def test_30_actual_missing_changed_capsule_and_sync_state_are_bound(self):
        seed, snapshot, path, plan = self.make_plan("actual-cas")
        added = self.run_mem("add", "durable", "note", "Unrelated record for actual serving CAS checks.", "--scope", "global", json_output=False)
        self.assertEqual(added.returncode, 0, added.stderr)
        con = sqlite3.connect(self.db)
        rid = con.execute("SELECT id FROM records").fetchone()[0]
        con.close()
        planned = self.run_mem("repair-blocked", "plan", "--op-id", seed["op_id"], "--reason", "CAS", "--snapshot", str(snapshot), "--out", "-")
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout); path.write_text(canonical(plan))
        for statement in ("UPDATE records SET headline='injected capsule drift' WHERE id=?",
                          "DELETE FROM records WHERE id=?"):
            con = sqlite3.connect(self.db)
            prior = con.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
            con.execute(statement, (rid,)); con.commit(); con.close()
            before = self.db_state()
            result = self.run_mem("repair-blocked", "apply", "--plan", str(path), "--expect", plan["plan_digest"], "--snapshot", str(snapshot))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("serving-state-drift", result.stderr)
            self.assertEqual(before, self.db_state())
            con = sqlite3.connect(self.db)
            con.execute("DELETE FROM records WHERE id=?", (rid,))
            con.execute("INSERT INTO records VALUES(" + ",".join("?" for _ in prior) + ")", prior)
            con.commit(); con.close()


if __name__ == "__main__":
    unittest.main()
