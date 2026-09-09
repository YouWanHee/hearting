#!/usr/bin/env python3
"""Strict list representation equivalence; every DB here is synthetic/private."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_mem_repair_blocked as fixtures

SET_FIELDS = ("tags", "links", "aliases", "entities", "topics", "artifact_refs")

class ServingListOrderTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RepairBlockedCliTest()
        f = self.fixture
        def bounded_mem(*args, json_output=True):
            argv = [sys.executable, str(fixtures.MEM), *args]
            if json_output and "--json" not in argv: argv.append("--json")
            workspace = f.root / "workspace"
            workspace.mkdir(exist_ok=True)
            return subprocess.run(argv, env=f.env, cwd=workspace, text=True,
                                  capture_output=True, timeout=45)
        f.run_mem = bounded_mem
        f.setUp()
        self.addCleanup(f.tearDown)
        # The existing fixture's git-init child also receives the private env.
        with patch.dict(os.environ, f.env, clear=True):
            f.join_v2()
        self.seed = f.seed_blocked_tombstone(["history-a", "history-b"])
        self.record_id = self.add_supported("ordinary", pending=False)
        self.pending_id = self.add_supported("pending", pending=True)
        self.snapshot = f.prepare_snapshot(prefix="unordered")
        self.snapshot_bytes = f.snapshot_tree(self.snapshot)
        # Import only after private HOME/MEM overrides exist; no live store probe.
        with patch.dict(os.environ, f.env, clear=True):
            spec = importlib.util.spec_from_file_location("order_memory", fixtures.MEM)
            self.memory = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.memory)

    def add_supported(self, suffix, *, pending):
        f = self.fixture
        args = ["add", "working" if pending else "durable",
                "handoff" if pending else "decision",
                "Synthetic preserved full body for " + suffix,
                "--scope", "global", "--source", "order-" + suffix,
                "--headline", "Synthetic order " + suffix,
                "--tags", "zeta,alpha", "--links", "zulu,able",
                "--alias", "zeta", "--alias", "alpha",
                "--entity", "zulu", "--entity", "able",
                "--topic", "zeta", "--topic", "alpha",
                "--artifact-ref", "zeta-ref", "--artifact-ref", "alpha-ref"]
        if pending: args.append("--requires-consume")
        result = f.run_mem(*args, json_output=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with sqlite3.connect(f.db) as con:
            return con.execute("SELECT id FROM records WHERE source=?",
                               ("order-" + suffix,)).fetchone()[0]

    def plan(self, name="plan.json"):
        f = self.fixture
        path = f.root/name
        result = f.run_mem("repair-blocked", "plan", "--snapshot", str(self.snapshot),
                          "--op-id", self.seed["op_id"], "--reason", "synthetic list-order regression",
                          "--out", str(path))
        return result, path

    def apply(self, path, plan):
        return self.fixture.run_mem("repair-blocked", "apply", "--snapshot", str(self.snapshot),
                                    "--plan", str(path), "--expect", plan["plan_digest"])

    def replace_cell_negative_fixture(self, field, value):
        self.assertIn(field, SET_FIELDS)
        with sqlite3.connect(self.fixture.db) as con:
            fixtures.sync_v2.register_writer_functions(con, protocol_major=2)
            con.execute('UPDATE records SET "' + field + '"=? WHERE id=?',
                        (value, self.record_id))

    def test_supported_unordered_puts_apply_preserves_raw_rows_indexes_pending_history_and_retry(self):
        f = self.fixture
        before = f.db_state()
        with sqlite3.connect(f.db) as con:
            row = con.execute("SELECT aliases,entities,topics FROM records WHERE id=?",
                              (self.record_id,)).fetchone()
        self.assertEqual(json.loads(row[0]), ["zeta", "alpha"])
        self.assertEqual(json.loads(row[1]), ["zulu", "able"])
        self.assertEqual(json.loads(row[2]), ["zeta", "alpha"])
        result, path = self.plan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, f.db_state())  # plan is read-only
        plan = json.loads(path.read_text())
        applied = self.apply(path, plan)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(json.loads(applied.stdout)["status"], "applied")
        after = f.db_state()
        self.assertEqual(before["records"], after["records"])
        for name, rows in before["all_tables"].items():
            if name.startswith(("records_fts", "records_cjk", "records_capsule_fts")) or name == "record_topics":
                self.assertEqual(rows, after["all_tables"][name], name)
        self.assertEqual(self.snapshot_bytes, f.snapshot_tree(self.snapshot))
        self.assertTrue(set(before["objects"]) < set(after["objects"]))
        self.assertEqual(len(after["objects"])-len(before["objects"]), 4)
        self.assertTrue(set(before["graveyard"]) <= set(after["graveyard"]))
        with sqlite3.connect(f.db) as con:
            self.assertEqual(con.execute("SELECT delivery_state,body FROM records WHERE id=?",
                (self.pending_id,)).fetchone(), ("pending", "Synthetic preserved full body for pending"))
        retry_before = f.db_state()
        retry = self.apply(path, plan)
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(json.loads(retry.stdout)["status"], "already-applied")
        self.assertEqual(retry_before, f.db_state())

    def test_strict_comparator_all_six_sets_and_nonlist_columns(self):
        compare = self.memory._record_params_equivalent
        for field in SET_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(compare(('["z", "a"]',), ('["a", "z"]',), (field,)))
                self.assertFalse(compare(('["z", "a", "extra"]',), ('["a", "z"]',), (field,)))
                self.assertFalse(compare(('["z"]',), ('["a", "z"]',), (field,)))
                for bad in ('oops', '{}', 'null', '1', '"text"', '[1]', '["z", null]', '["\\ud800"]', None, b'[]'):
                    self.assertFalse(compare((bad,), ('[]',), (field,)), (field, bad))
                    self.assertFalse(compare((bad,), (bad,), (field,)), (field, bad))
        for field in ("body", "delivery_state", "headline", "source", "expires"):
            self.assertFalse(compare(("left",), ("right",), (field,)))
        self.assertFalse(compare((), (), ("body",)))

    def test_extra_or_missing_list_item_refuses_without_changes(self):
        f = self.fixture
        for field in SET_FIELDS:
            with sqlite3.connect(f.db) as con:
                original = con.execute('SELECT "'+field+'" FROM records WHERE id=?',
                                       (self.record_id,)).fetchone()[0]
            items = json.loads(original)
            for kind, changed in (("extra", items+["different-item"]), ("missing", items[1:])):
                with self.subTest(field=field, kind=kind):
                    self.replace_cell_negative_fixture(field, json.dumps(changed))
                    before = f.db_state()
                    result, path = self.plan(field+"-"+kind+".json")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("serving-state-drift", result.stderr)
                    self.assertFalse(path.exists())
                    self.assertEqual(before, f.db_state())
            self.replace_cell_negative_fixture(field, original)

    def test_malformed_or_wrong_json_type_refuses_without_changes(self):
        f = self.fixture
        with sqlite3.connect(f.db) as con:
            original = con.execute("SELECT aliases FROM records WHERE id=?",(self.record_id,)).fetchone()[0]
        for index, bad in enumerate(('oops', '{}', 'null', '1', '"text"', '[1]', '["zeta", false]')):
            with self.subTest(bad=bad):
                self.replace_cell_negative_fixture("aliases", bad)
                before = f.db_state()
                result, path = self.plan("bad-"+str(index)+".json")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("serving-state-drift", result.stderr)
                self.assertFalse(path.exists())
                self.assertEqual(before, f.db_state())
        self.replace_cell_negative_fixture("aliases", original)

    def test_post_plan_order_only_change_still_fails_raw_cas(self):
        f = self.fixture
        result, path = self.plan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = json.loads(path.read_text())
        self.replace_cell_negative_fixture("aliases", json.dumps(["alpha", "zeta"]))
        changed = f.db_state()
        applied = self.apply(path, plan)
        self.assertNotEqual(applied.returncode, 0)
        self.assertIn("plan-stale", applied.stderr)
        self.assertEqual(changed, f.db_state())
        self.assertEqual(self.snapshot_bytes, f.snapshot_tree(self.snapshot))

    def test_materialize_rewrites_real_content_difference(self):
        f = self.fixture
        # A separate synthetic fault proves the new fixpoint retains the
        # existing rewrite path for actual data changes, not just set order.
        with sqlite3.connect(f.db) as con:
            fixtures.sync_v2.register_writer_functions(con, protocol_major=2)
            source = self.memory._record_state(con, self.record_id)
            source["body"] = "A genuinely changed synthetic body for materialization."
            self.memory._materialize_fold_state(con, self.record_id, source)
            actual = con.execute("SELECT body FROM records WHERE id=?",(self.record_id,)).fetchone()[0]
            self.assertEqual(actual, source["body"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
