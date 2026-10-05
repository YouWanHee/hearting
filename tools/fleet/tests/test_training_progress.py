"""Registered producer -> normal resolver -> model -> JSON/both Fleet views."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))
from fleet import fleet, render
from fleet.collectors import compute_hosts, resource_runs
import resource_progress
import resource_run_registry


def text(lines):
    return "\n".join("".join(part for part, _style in row) for row in lines if row)


class TrainingProgressPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "configs").mkdir()
        config = self.root / "configs" / "model.json"
        self.config = config
        config.write_text(json.dumps({"training": {"attempts": 400000}}))
        # A harmless owned child supplies real PID/start/argv/group evidence.
        ready = self.root / "child-ready"
        self.child = subprocess.Popen([sys.executable, "-B", "-c",
                                       "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(60)",
                                       str(ready),
                                       "--config", str(config)], cwd=self.root)
        self.addCleanup(self.stop_child)
        # Popen may return before exec changes /proc's argv/exe. Observe only
        # after this fixture's Python payload has started; production keeps
        # rejecting unstable identities.
        deadline = time.monotonic() + 5
        while not ready.exists():
            self.assertIsNone(self.child.poll(), "fixture child exited before startup")
            if time.monotonic() >= deadline:
                self.fail("fixture child startup timed out")
            time.sleep(.01)
        identity = resource_progress.observe_process(self.child.pid)
        self.assertIsNotNone(identity)
        wrapper = resource_run_registry.proc_identity(os.getpid())
        directory = self.root / "runs" / "arm"
        directory.mkdir(parents=True)
        self.progress = directory / "progress.json"
        self.progress.write_text(json.dumps({"attempt": 27346, "successful": 27342, "skipped": 4,
                                            "last": {"attempt": 27346, "metrics": {"loss": 0.0048248}}}))
        self.now = time.time()
        os.utime(self.progress, (self.now - 600, self.now - 600))
        (self.root / "run.json").write_text(json.dumps({"arms": [{
            "name": "arm", "state": "training-updates", "directory": str(directory),
            "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "process_identity": {"pid": identity["pid"], "starttime": identity["starttime"],
                                 "cmdline_sha256": identity["command_hash"]}}]}))
        registry = self.root / "resource-runs.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {"run-a": {
            **wrapper, "process_group": os.getpgrp(), "cwd": str(self.root), "status": "running",
            "config_ref": "config:model.json", "config_sha256": "baseline-will-not-supply-total"}}}))
        index = self.root / "index.json"
        self.index = index
        resource_run_registry.register_registry(registry, index)
        self.rows = resource_runs.collect(index)
        self.assertEqual(resource_runs.collect.last_diagnostics, [])
        self.assertEqual(len(self.rows), 1)
        self.training = self.rows[0].training_progress
        self.assertIsNotNone(self.training)
        self.snapshot = {"configured": True, "observed_at": self.now, "hosts": [{
            "host": "test-host", "self": True, "reachable": True, "observed_at": self.now,
            "gpus": [{"index": 0, "name": "GPU", "memory_used_mib": 128, "memory_total_mib": 1024,
                      "processes": [{"pid": self.child.pid, "proc_start": int(identity["starttime"]),
                                     "pgid": os.getpgrp(), "command": "python fixture --config model.json",
                                     "progress": {"line": "raw producer heartbeat", "age_s": 0}}]}]}]}
        self.addCleanup(render.set_compute_hosts, None)
        self.addCleanup(render.set_process_view, False)
        for module, name, value in ((fleet, "_collect_memory", None),
                                    (fleet, "_collect_governor", None),
                                    (fleet, "_collect_route", None)):
            patch = mock.patch.object(module, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def stop_child(self):
        if self.child.poll() is None:
            self.child.terminate()
        self.child.wait(timeout=5)

    def projected(self, snapshot=None, rows=None, now=None):
        return compute_hosts.with_training_progress(
            self.snapshot if snapshot is None else snapshot, self.rows if rows is None else rows,
            now=self.now if now is None else now)

    def training_in(self, snapshot):
        return snapshot["hosts"][0]["gpus"][0]["processes"][0]["progress"].get("training")

    def test_normal_collection_json_and_both_views_have_same_units_loss_and_age(self):
        before = copy.deepcopy(self.snapshot)
        output = json.loads(fleet._snapshot_json([], [], self.rows, compute_host_snapshot=self.snapshot))
        training = self.training_in(output["compute_hosts"])
        self.assertEqual(training["attempt_total"], 400000)
        self.assertEqual(training["successful"], 27342)
        self.assertEqual(training["skipped"], 4)
        self.assertEqual(training["loss"], .0048248)
        self.assertEqual(training["loss_kind"], "last-batch")
        self.assertEqual(training["arm"], "arm")
        self.assertEqual(output["resource_jobs"][0]["training_progress"]["config_ref"], "config:model.json")
        self.assertGreaterEqual(training["progress_age_s"], 600)
        render.set_compute_hosts(self.snapshot)
        for process_view in (False, True):
            render.set_process_view(process_view)
            shown = text(render._build_lines([], [], "both", False, 0, term_width=140,
                                            resources=self.rows, governor=None))
            self.assertIn("TRAIN 7% 27346/400000 · loss=4.82e-03 · stalled 10m", shown)
            self.assertNotIn("LAB RESOURCES", shown)
            self.assertNotIn("raw producer heartbeat", shown)
            self.assertNotIn("Epoch", shown)
            self.assertNotIn("800,000", shown)
        self.assertEqual(self.snapshot, before)

    def test_verified_cadence_projects_schedule_to_json_and_both_human_views(self):
        self.config.write_text(json.dumps({"training": {"attempts": 400000, "epochs": 20,
                                                       "blocks_per_epoch": 1000, "updates_per_block": 20}}))
        metadata = self.root / "run.json"
        value = json.loads(metadata.read_text())
        value["arms"][0]["config_sha256"] = hashlib.sha256(self.config.read_bytes()).hexdigest()
        metadata.write_text(json.dumps(value))
        rows = resource_runs.collect(self.index)
        self.assertEqual(resource_runs.collect.last_diagnostics, [])
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["hosts"][0]["gpus"].append({
            "index": 1, "name": "GPU", "memory_used_mib": 128, "memory_total_mib": 1024,
            "processes": [{"pid": 999999999, "command": "python raw fixture --train",
                           "progress": {"line": "TRAIN: 38%|###| 7603/20000 [00:00<1:38:19, 2batch/s, L_se=9.18e-03, L_se_aux=4.91e-03]",
                                        "epoch": {"n": "23", "of": 200}, "age_s": 0}}]})
        output = json.loads(fleet._snapshot_json([], [], rows, compute_host_snapshot=snapshot))
        training = self.training_in(output["compute_hosts"])
        self.assertEqual(training["schedule_epoch"]["current"], 2)
        self.assertEqual(training["schedule_epoch"]["state"], "partial")
        self.assertEqual(training["loss"], .0048248)
        self.assertEqual(training["successful"], 27342)
        self.assertEqual(training["skipped"], 4)
        self.assertEqual(training["percent"], 6.8365)
        self.assertGreaterEqual(training["progress_age_s"], 600)
        render.set_compute_hosts(snapshot)
        for process_view in (False, True):
            render.set_process_view(process_view)
            shown = text(render._build_lines([], [], "both", False, 0, term_width=180,
                                            resources=rows, governor=None))
            self.assertIn("Epoch 2/20 · TRAIN 37% 7346/20000 · loss=4.82e-03 · stalled 10m", shown)
            self.assertIn("Epoch 23/200 · TRAIN 38% 7603/20000 · 1:38:19 left · L_se=9.18e-03 L_se_aux=4.91e-03", shown)
            self.assertNotIn("raw producer heartbeat", shown)

    def test_human_schedule_boundaries_colors_and_narrow_step_preserve_source_values(self):
        # Literal expectations distinguish interval attempts from optimizer success
        # and overall budget; boundary rows show the interval just completed.
        cases = (
            (0, 0, "↳ Epoch 0/20 · TRAIN 0% 0/20000 · loss=4.82e-03 · stalled 10m"),
            (1, 1, "↳ Epoch 1/20 · TRAIN 0% 1/20000 · loss=4.82e-03 · stalled 10m"),
            (20000, 1, "↳ Epoch 1/20 · TRAIN 100% 20000/20000 · loss=4.82e-03 · stalled 10m"),
            (20001, 2, "↳ Epoch 2/20 · TRAIN 0% 1/20000 · loss=4.82e-03 · stalled 10m"),
            (400000, 20, "↳ Epoch 20/20 · TRAIN 100% 20000/20000 · loss=4.82e-03 · stalled 10m"),
        )
        for attempt, current, expected in cases:
            with self.subTest(attempt=attempt):
                training = {**self.training, "attempt": attempt, "successful": attempt, "skipped": 0,
                            "schedule_epoch": {"current": current, "total": 20, "attempts_per_epoch": 20000}}
                before = copy.deepcopy(training)
                process = {"progress": {"training": training}}
                row = render._gpu_progress_row(process, "", 180)
                self.assertEqual(render._plain(row).strip(), expected)
                self.assertIn(("4.82e-03", "resource_active"), row)
                self.assertEqual(training, before)
        process["progress"]["training"].update(attempt=59415, successful=59397, skipped=18,
                                              schedule_epoch={"current": 3, "total": 20, "attempts_per_epoch": 20000})
        shown = render._plain(render._gpu_progress_row(process, "", 80))
        self.assertIn("Epoch 3/20 · TRAIN 97% 19415/20000", shown)
        self.assertTrue(shown.endswith("stalled 10m"), shown)
        self.assertLessEqual(render._dw(shown), 80)
        process["progress"]["training"]["schedule_epoch"]["total"] = True
        shown = render._plain(render._gpu_progress_row(process, "", 180))
        self.assertNotIn("Epoch", shown)
        self.assertIn("TRAIN 15% 59415/400000", shown)

    def test_fresh_heartbeat_does_not_refresh_counter_age(self):
        projected = self.projected(now=self.now + 5)
        self.assertEqual(projected["hosts"][0]["gpus"][0]["processes"][0]["progress"]["age_s"], 0)
        self.assertGreaterEqual(self.training_in(projected)["progress_age_s"], 605)

    def test_remote_expired_reused_group_or_stale_resource_keep_raw_progress(self):
        for field, value in (("self", False), ("observed_at", self.now - 31)):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot["hosts"][0][field] = value
            self.assertIsNone(self.training_in(self.projected(snapshot)))
        for field, value in (("proc_start", 99), ("pgid", os.getpgrp() + 1), ("pgid", [])):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot["hosts"][0]["gpus"][0]["processes"][0][field] = value
            self.assertIsNone(self.training_in(self.projected(snapshot)))
        self.rows[0].liveness = "stale"
        self.assertIsNone(self.training_in(self.projected()))

    def test_expired_resource_observation_and_ambiguous_registry_do_not_attach(self):
        self.rows[0].training_progress["observed_at"] = self.now - 31
        self.assertIsNone(self.training_in(self.projected()))
        self.rows[0].training_progress["observed_at"] = self.now
        other = copy.deepcopy(self.rows[0])
        other.training_progress["attempt"] += 1
        self.assertIsNone(self.training_in(self.projected(rows=self.rows + [other])))


if __name__ == "__main__":
    unittest.main()
