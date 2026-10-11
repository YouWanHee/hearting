"""Repository identity, normal CLI rekey, and cross-replica compatibility."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

from helpers import git, load_module
import test_mem_sync_v2 as fixtures


OLD = "git:github.com/example/old-name"
NEW = "git:github.com/example/new-name"


class RepositoryRenameTest(unittest.TestCase):
    setUp = fixtures.MemTwoServerSyncTest.setUp
    tearDown = fixtures.MemTwoServerSyncTest.tearDown
    _mem = fixtures.MemTwoServerSyncTest._mem
    _activate_fresh = fixtures.MemTwoServerSyncTest._activate_fresh
    _sync = fixtures.MemTwoServerSyncTest._sync
    _rows = fixtures.MemTwoServerSyncTest._rows
    _written_id = staticmethod(fixtures.MemTwoServerSyncTest._written_id)
    _digests = fixtures.MemTwoServerSyncTest._digests

    def _environment(self, name, *, remote=False):
        env = fixtures.MemTwoServerSyncTest._environment(self, name, remote=remote)
        if hasattr(self, "bin"):
            env["PATH"] = str(self.bin) + os.pathsep + env["PATH"]
        return env

    def _repository(self, *, same=True):
        self.bin = self.root / "bin"
        self.bin.mkdir()
        values = {"repos/example/old-name": {"id": 101, "full_name": "example/new-name"},
                  "repos/example/new-name": {"id": 101 if same else 202,
                                             "full_name": "example/new-name"}}
        (self.bin / "repos.json").write_text(json.dumps(values))
        program = ("#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
                   "data=json.loads(Path(__file__).with_name('repos.json').read_text())\n"
                   "value=data.get(sys.argv[2])\n"
                   "if value is None: sys.exit(1)\nprint(json.dumps(value))\n")
        (self.bin / "gh").write_text(program)
        (self.bin / "gh").chmod(0o700)
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/old-name.git")

    def _add(self, name="a", *, pending=False, body="Repository memory survives a rename"):
        args = ["add", "durable", "decision", body, "--headline", "Repository rename"]
        if pending:
            args.append("--requires-consume")
        result = self._mem(name, *args)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return self._written_id(result)

    def _states(self, name):
        with sqlite3.connect(self.stores[name] / "memory.db") as con:
            return con.execute("SELECT id,cwd_origin,body,delivery_state FROM records ORDER BY id").fetchall()

    def _operations(self, name):
        protocol = load_module("protocol_v2")
        with sqlite3.connect(self.stores[name] / "memory.db") as con:
            return [{"op_id": rid, "payload": protocol.canonical_loads(bytes(raw))}
                    for rid, raw in con.execute("SELECT op_id,payload_bytes FROM sync_objects")]

    def test_normal_sync_moves_records_and_frontiers_and_preserves_pending_on_two_servers(self):
        self._repository()
        rid = self._add(pending=True)
        self._sync("a")
        self._sync("b")
        before = self._states("a")
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        # A record already written under the new address must also retain its
        # existing lineage when later writes use the unified project identity.
        newer = self._add(body="Already created after changing the repository origin")
        self._sync("a")
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._states("a"), self._states("b"))
        states = {row[0]: row for row in self._states("a")}
        self.assertEqual(states[rid], (rid, NEW, before[0][2], "pending"))
        for name in ("a", "b"):
            with sqlite3.connect(self.stores[name] / "memory.db") as con:
                self.assertEqual({row[0] for row in con.execute("SELECT project_key FROM sync_frontier")}, {NEW})
        self.assertTrue(list(self.stores["a"].glob("memory-rekey-*.bak")))
        # Existing ID remains the normal consume address after the rename.
        result = self._mem("a", "consume", rid)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        result = self._mem("a", "reinforce", newer)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        added_after = self._add(body="New memory authored after the project rename was followed")
        self._sync("a")
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._states("a"), self._states("b"))
        self.assertEqual(self._digests("a"), self._digests("b"))
        self.assertIn(added_after, {row[0] for row in self._states("b")})
        # A down-level reader quarantines the new lineage, preserving its
        # previous records instead of misclassifying it as project corruption.
        protocol = load_module("protocol_v2")
        with mock.patch.object(protocol, "SUPPORTED_SCHEMA_MINORS", frozenset({0})):
            legacy = protocol.fold_operations(self._operations("a"))
        self.assertFalse(legacy.classification.hard_failures)
        self.assertTrue(legacy.classification.quarantined)
        self.assertEqual(legacy.records[rid]["cwd_origin"], OLD)
        self.assertEqual(legacy.records[rid]["delivery_state"], "pending")

    def test_existing_reattribute_follows_verified_repository_rename_for_whole_project(self):
        self._repository()
        first = self._add(pending=True)
        second = self._add(body="Second old repository record remains unchanged")
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        result = self._mem("a", "reattribute", first)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual({row[0] for row in self._states("a")}, {first, second})
        self.assertEqual({row[1] for row in self._states("a")}, {NEW})
        backup = next(self.stores["a"].glob("memory-rekey-*.bak"))
        with sqlite3.connect(backup) as con:
            self.assertEqual({row[0] for row in con.execute("SELECT cwd_origin FROM records")}, {OLD})
        count = len(self._operations("a"))
        self._sync("a")
        self.assertEqual(len(self._operations("a")), count)

    def test_different_repository_or_missing_evidence_is_not_moved(self):
        self._repository(same=False)
        rid = self._add()
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        before = self._states("a")
        self._sync("a")
        self.assertEqual(self._states("a"), before)
        result = self._mem("a", "reattribute", rid)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._states("a"), before)
        self.assertFalse(list(self.stores["a"].glob("memory-rekey-*.bak")))
        (self.bin / "repos.json").write_text("{}")
        self._sync("a")
        self.assertEqual(self._states("a"), before)

    def test_rekey_cannot_change_body_or_consume_an_obligation(self):
        self._repository()
        rid = self._add(pending=True)
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        result = self._mem("a", "reattribute", rid)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        protocol = load_module("protocol_v2")
        operations = self._operations("a")
        rekey = next(op for op in operations if op["payload"]["kind"] == "rekey")
        for field, value in (("body", "Forged new body"), ("delivery_state", "consumed")):
            payload = json.loads(json.dumps(rekey["payload"]))
            payload["mutations"][0]["post_state"][field] = value
            forged = protocol.build_operation(payload)
            folded = protocol.fold_operations([op for op in operations if op != rekey] + [forged])
            self.assertFalse(folded.classification.hard_failures)
            self.assertIn(forged["op_id"], folded.blocked)
            self.assertEqual(folded.records[rid]["cwd_origin"], OLD)

    def test_records_from_both_addresses_can_use_normal_supersede(self):
        self._repository()
        old_record = self._add(body="Old repository decision with a preserved history")
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        new_record = self._add(body="Replacement decision already authored under the new origin")
        self._sync("a")
        result = self._mem("a", "supersede", old_record, "--by", new_record)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self._sync("a")
        self._sync("b")
        self.assertEqual(self._states("a"), self._states("b"))
        protocol = load_module("protocol_v2")
        folded = protocol.fold_operations(self._operations("a"))
        self.assertFalse(folded.blocked)
        self.assertEqual(folded.records[old_record]["superseded_by"], new_record)
        with mock.patch.object(protocol, "SUPPORTED_SCHEMA_MINORS", frozenset({0})):
            legacy = protocol.fold_operations(self._operations("a"))
        self.assertFalse(legacy.classification.hard_failures)

    def test_repeated_rename_and_rename_back_preserve_lineage(self):
        self._repository()
        rid = self._add(pending=True)
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        self._sync("a")
        # GitHub reports the same repository renamed back to its original address.
        values = {"repos/example/old-name": {"id":101, "full_name":"example/old-name"},
                  "repos/example/new-name": {"id":101, "full_name":"example/old-name"}}
        (self.bin / "repos.json").write_text(json.dumps(values))
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/old-name.git")
        self._sync("a")
        result = self._mem("a", "consume", rid)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self._sync("a")
        self._sync("b")
        self.assertEqual(self._states("a"), self._states("b"))
        self.assertEqual(self._states("a")[0][1], OLD)

    def test_new_records_wait_for_successful_repository_identity(self):
        self._repository()
        first = self._add()
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        self.assertEqual(self._mem("a", "reattribute", first).returncode, 0)
        added = self._add(body="New record with a causal repository identity")
        protocol = load_module("protocol_v2")
        operations = self._operations("a")
        rekey = next(op for op in operations if op["payload"]["kind"] == "rekey")
        new = next(op for op in operations if op["payload"]["mutations"][0]["record_id"] == added)
        self.assertIn(rekey["op_id"], new["payload"]["namespace_parents"])
        without = protocol.fold_operations([op for op in operations if op != rekey])
        self.assertIn(new["op_id"], without.deferred)
        self.assertNotIn(added, without.records)
        payload = json.loads(json.dumps(rekey["payload"]))
        payload["mutations"][0]["post_state"]["body"] = "Invalid rename changes content"
        forged = protocol.build_operation(payload)
        payload = json.loads(json.dumps(new["payload"]))
        payload["namespace_parents"] = [forged["op_id"]]
        forged_new = protocol.build_operation(payload)
        folded = protocol.fold_operations([op for op in operations if op not in (rekey, new)]
                                          + [forged, forged_new])
        self.assertIn(forged_new["op_id"], folded.blocked)
        self.assertNotIn(added, folded.records)

    def test_delete_restore_uses_serving_project_after_rename(self):
        self._repository()
        rid = self._add()
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        self._sync("a")
        before = self._states("a")
        for command in ("delete", "restore"):
            result = self._mem("a", command, rid)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self._sync("a")
        self._sync("b")
        self.assertEqual(self._states("a"), before)
        self.assertEqual(self._states("a"), self._states("b"))

    def test_conflicts_after_rename_remain_visible_and_resolvable(self):
        self._repository()
        rid = self._add()
        self._sync("a")
        self._sync("b")
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        self._sync("a")
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._mem("a", "reinforce", rid).returncode, 0)
        for _ in range(2):
            self.assertEqual(self._mem("b", "reinforce", rid).returncode, 0)
        self._sync("a")
        result = self._mem("b", "sync", "--json", remote=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "unresolved-conflicts")
        listed = self._mem("b", "conflicts", "--json")
        self.assertEqual(json.loads(listed.stdout)["count"], 1)
        shown = self._mem("b", "show-conflict", rid, "--json")
        variants = json.loads(shown.stdout)["variants"]
        result = self._mem("b", "resolve", rid, "--parents", *[v["op_id"] for v in variants])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._states("a"), self._states("b"))
        self.assertEqual(self._states("a")[0][1], NEW)

    def test_unresolved_old_conflict_does_not_stop_normal_sync(self):
        self._repository()
        rid = self._add()
        self._sync("a")
        self._sync("b")
        self._mem("a", "reinforce", rid)
        self._mem("b", "reinforce", rid)
        self._mem("b", "reinforce", rid)
        self._sync("a")
        result = self._mem("b", "sync", "--json", remote=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "unresolved-conflicts")
        git(self.project, "remote", "set-url", "origin", "https://github.com/example/new-name.git")
        result = self._mem("b", "sync", "--json", remote=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "unresolved-conflicts")
        self.assertEqual(json.loads(result.stdout)["phases"]["remote-fetch-validate"], "ok")
        listed = self._mem("b", "conflicts", "--json")
        self.assertEqual(json.loads(listed.stdout)["count"], 1)
        shown = self._mem("b", "show-conflict", rid, "--json")
        variants = json.loads(shown.stdout)["variants"]
        result = self._mem("b", "resolve", rid, "--parents", *[v["op_id"] for v in variants])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self._sync("b")
        self._sync("a")
        self.assertEqual(self._states("a"), self._states("b"))


if __name__ == "__main__":
    unittest.main()
