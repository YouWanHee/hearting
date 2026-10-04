#!/usr/bin/env python3
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

import resource_progress as progress


class ResourceProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "configs").mkdir(parents=True)
        self.config = self.repo / "configs" / "a.json"
        self.config.write_text(json.dumps({"training": {"attempts": 400000}}))
        self.digest = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.directory = self.root / "runs" / "first"
        self.directory.mkdir(parents=True)
        self.path = self.directory / "progress.json"
        self.counter = {"attempt": 27346, "successful": 27342, "skipped": 4,
                        "last": {"attempt": 27346, "metrics": {"loss": 0.0048248}}}
        self.path.write_text(json.dumps(self.counter))
        self.now = time.time()
        os.utime(self.path, (self.now - 600, self.now - 600))
        self.wrapper = {"pid": 4, "starttime": "10", "command_hash": "a" * 64,
                        "process_group": 4, "cwd": str(self.repo), "argv": ["wrapper"]}
        self.child = {"pid": 42, "starttime": "11", "command_hash": "b" * 64,
                      "process_group": 4, "cwd": str(self.repo),
                      "argv": ["python", "run.py", "--config", str(self.config)]}
        self.run = dict(self.wrapper)
        self.arm = {"name": "first", "state": "training-updates", "directory": str(self.directory),
                    "config_sha256": self.digest, "process_identity": {
                        "pid": 42, "starttime": 11, "cmdline_sha256": "b" * 64}}
        self.metadata = {"arms": [self.arm]}
        self.write_metadata()

    def write_metadata(self):
        (self.root / "run.json").write_text(json.dumps(self.metadata))

    def reader(self, pid):
        return copy.deepcopy({4: self.wrapper, 42: self.child}.get(pid))

    def collect(self, reader=None):
        return progress.collect(self.run, self.root / "resource-runs.json", self.now,
                                process_reader=reader or self.reader)

    def test_attempt_denominator_last_batch_and_stale_progress_are_independent(self):
        value = self.collect()
        self.assertEqual((value["attempt"], value["attempt_total"], value["successful"], value["skipped"]),
                         (27346, 400000, 27342, 4))
        self.assertAlmostEqual(value["percent"], 6.8365)
        self.assertEqual((value["loss"], value["loss_kind"]), (0.0048248, "last-batch"))
        self.assertAlmostEqual(value["progress_age_s"], 600, places=4)
        self.assertLess(self.now - value["metadata_updated_at"], 2)
        self.assertEqual(value["config_sha256"], self.digest)
        self.assertEqual(value["config_ref"], "config:a.json")
        self.assertNotIn("eta", value)
        self.assertNotIn("epoch", value)

    def test_next_arm_uses_its_actual_config_instead_of_registered_baseline(self):
        other = self.repo / "configs" / "next.json"
        other.write_text(json.dumps({"training": {"attempts": 90000}}))
        self.child["argv"] = ["python", "run.py", "--config=" + str(other)]
        self.arm["config_sha256"] = hashlib.sha256(other.read_bytes()).hexdigest()
        self.arm["name"] = "next"
        self.write_metadata()
        value = self.collect()
        self.assertEqual((value["arm"], value["attempt_total"], value["config_ref"]),
                         ("next", 90000, "config:next.json"))

    def test_hash_mismatch_and_unbound_config_fail_soft(self):
        self.config.write_text(json.dumps({"training": {"attempts": 800000}}))
        self.assertIsNone(self.collect())
        self.child["argv"] = ["python", "run.py"]
        self.assertIsNone(self.collect())
        self.child["argv"] = ["python", "run.py", "--config", str(self.config), "--config=a.json"]
        self.assertIsNone(self.collect())

    def test_config_outside_normal_resolver_root_is_not_read(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps({"training": {"attempts": 123}}))
        self.child["argv"][-1] = str(outside)
        self.arm["config_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
        self.write_metadata()
        self.assertIsNone(self.collect())

    def test_successful_target_is_not_an_attempt_target(self):
        for training in ({"successful_updates": 400000}, {"epochs": 20, "updates_per_block": 20},
                         {"attempts": True}, {"attempts": 2}):
            with self.subTest(training=training):
                self.config.write_text(json.dumps({"training": training}))
                self.arm["config_sha256"] = hashlib.sha256(self.config.read_bytes()).hexdigest()
                self.write_metadata()
                self.assertIsNone(self.collect())

    def test_loss_from_a_different_attempt_is_omitted(self):
        self.counter["last"]["attempt"] -= 1
        self.path.write_text(json.dumps(self.counter))
        value = self.collect()
        self.assertIsNotNone(value)
        self.assertIsNone(value["loss"])
        self.assertIsNone(value["loss_kind"])

    def test_contradictory_counter_and_nonfinite_json_are_unsupported(self):
        self.counter["successful"] += 1
        self.path.write_text(json.dumps(self.counter))
        self.assertIsNone(self.collect())
        self.counter["successful"] -= 1
        self.counter["last"]["metrics"]["loss"] = float("nan")
        self.path.write_text(json.dumps(self.counter))
        self.assertIsNone(self.collect())

    def test_reused_child_changed_group_and_wrapper_change_do_not_attach(self):
        for key, value in (("starttime", "12"), ("command_hash", "c" * 64), ("process_group", 5)):
            old = self.child[key]
            self.child[key] = value
            self.assertIsNone(self.collect())
            self.child[key] = old
        calls = 0
        def raced(pid):
            nonlocal calls
            if pid == 4:
                calls += 1
                if calls > 1:
                    return {**self.wrapper, "starttime": "20"}
            return self.reader(pid)
        self.assertIsNone(self.collect(raced))

    def test_symlink_oversize_and_duplicate_metadata_do_not_attach(self):
        raw = self.path.read_bytes()
        self.path.unlink()
        other = self.root / "other.json"
        other.write_bytes(raw)
        self.path.symlink_to(other)
        self.assertIsNone(self.collect())
        self.path.unlink()
        self.path.write_bytes(b" " * (progress.MAX_BYTES + 1))
        self.assertIsNone(self.collect())
        self.path.write_bytes(raw)
        (self.root / "run.json").write_text('{"arms":[],"arms":[]}')
        self.assertIsNone(self.collect())

    def test_foreign_progress_directory_and_ambiguous_arms_fail_soft(self):
        self.arm["directory"] = str(self.repo.parent / ".." / "other")
        self.write_metadata()
        self.assertIsNone(self.collect())
        self.arm["directory"] = str(self.directory)
        self.metadata["arms"].append(copy.deepcopy(self.arm))
        self.write_metadata()
        self.assertIsNone(self.collect())


if __name__ == "__main__":
    unittest.main()
