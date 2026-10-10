#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import resource_run_registry as registry


class ResourceRegistryTest(unittest.TestCase):
    def test_empty_log_has_no_output_timestamp_but_nonempty_log_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "resource.log"
            log.touch()
            raw = {"status": "succeeded", "log": str(log)}
            row = registry.normalize_run("run", raw, Path(tmp) / "runs.json")
            self.assertIsNone(row["log_updated_at"])
            self.assertEqual(row["log_size"], 0)
            log.write_text("observed output\n")
            row = registry.normalize_run("run", raw, Path(tmp) / "runs.json")
            self.assertEqual(row["log_updated_at"], log.stat().st_mtime)
            self.assertGreater(row["log_size"], 0)

    def test_normalize_preserves_additive_artifact_attribution_fields(self):
        raw = {
            "status": "succeeded",
            "artifact_root": "/artifacts",
            "route": "/artifacts/.runtime/routes/rt-a.json",
            "route_file": "/artifacts/.runtime/routes/rt-a.json",
            "route_id": "rt-a",
            "route_hash": "sha256:" + "a" * 64,
            "node": "test",
            "route_node": "test",
        }
        row = registry.normalize_run(
            "run-a", raw, Path("/registry.json"), identity_reader=lambda _pid: None,
        )
        for key in ("artifact_root", "route", "route_file", "route_id", "route_hash",
                    "node", "route_node"):
            self.assertEqual(row[key], raw[key])

    def test_missing_registry_is_typed_skip_with_valid_neighbor(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); index = base / "index.json"
            good = base / "good.json"; missing = base / "gone" / "registry.json"
            good.write_text(json.dumps({"schema_version": 1, "runs": {"good": {"pid": 7}}}))
            index.write_text(json.dumps({"schema_version": 1, "registries": {
                "good": {"path": str(good)}, "gone": {"path": str(missing)}}}))
            rows, diagnostics = registry.scan(index)
            self.assertEqual([r["run_id"] for r in rows], ["good"])
            self.assertEqual(diagnostics[0]["kind"], "missing-registry")
            self.assertEqual(diagnostics[0]["path"], str(missing))
            self.assertEqual(registry.counts(index)["malformed"], 0)
            self.assertEqual(registry.counts(index)["missing"], 1)

    def test_dangling_link_retains_registered_path_and_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td); link = base / "link"; link.symlink_to(base / "absent")
            index = base / "index.json"
            for source in (link, link / "registry.json"):
                index.write_text(json.dumps({"schema_version": 1,
                    "registries": {"bad": {"path": str(source)}}}))
                paths, _ = registry.indexed_paths(index)
                self.assertEqual(paths, [source])
                _, diagnostics = registry.scan(index)
                self.assertNotEqual(diagnostics[0]["kind"], "missing-registry")
                self.assertEqual(diagnostics[0]["path"], str(source))

    def test_live_exited_and_pid_reuse(self):
        row = {"pid": 2147483647, "starttime": "11", "command_hash": "abc"}
        exact = lambda pid: {"pid": pid, "starttime": "11", "command_hash": "abc"}
        reused = lambda pid: {"pid": pid, "starttime": "12", "command_hash": "def"}
        self.assertEqual(registry.classify_identity(row, exact)[0], "working")
        self.assertEqual(registry.classify_identity(row, lambda _pid: None)[0], "exited")
        self.assertEqual(registry.classify_identity(row, reused)[0], "stale")
        unreadable = {**row, "pid": os.getpid()}
        self.assertEqual(registry.classify_identity(
            unreadable, lambda _pid: None)[0], "stale")

    def test_owned_zombie_requires_exact_kernel_and_controller_identity(self):
        pid = 999999999
        parent = registry.proc_identity(os.getpid())
        namespace = os.readlink("/proc/self/ns/pid")
        row = {"pid": pid, "starttime": "42", "command_hash": "a" * 64,
               "process_group": pid, "pid_namespace": namespace,
               "resource_policy": "supervised-owner", "launch_state": "started",
               "owner_wait": {"launch_scope": "codex-owner-controller"},
               "launch_controller": {**parent, "pid_namespace": namespace}}
        # Linux fields 3/4/5/22: state, parent PID, group, kernel start tick.
        fields = ["Z", str(os.getpid()), str(pid)] + ["0"] * 16 + ["42"]
        raw = f"{pid} (wrapper) " + " ".join(fields)
        reader = lambda candidate: parent if candidate == os.getpid() else None
        with mock.patch.object(Path, "read_text", return_value=raw), \
             mock.patch.object(Path, "read_bytes", return_value=b""), \
             mock.patch.object(Path, "exists", return_value=True):
            self.assertEqual(registry.classify_identity(row, reader),
                             ("reaping", None, "owned-wrapper-awaiting-reap"))
            self.assertFalse(registry.is_alive(row, reader))
            for change in ({"starttime": "43"}, {"process_group": pid + 1},
                           {"pid_namespace": "pid:[foreign]"}, {"launch_state": "claimed"},
                           {"resource_policy": "verified-resume"}, {"owner_wait": {}},
                           {"launch_controller": {**parent, "starttime": "0", "pid_namespace": namespace}},
                           {"launch_controller": {**parent, "command_hash": "foreign", "pid_namespace": namespace}}):
                with self.subTest(change=change):
                    self.assertEqual(registry.classify_identity({**row, **change}, reader)[0], "stale")
            for index, value in ((0, "S"), (1, str(os.getpid() + 1)), (2, str(pid + 1)), (19, "43")):
                altered = list(fields); altered[index] = value
                with self.subTest(field=index), mock.patch.object(Path, "read_text",
                        return_value=f"{pid} (wrapper) " + " ".join(altered)):
                    self.assertEqual(registry.classify_identity(row, reader)[0], "stale")
            for method in ("read_text", "read_bytes"):
                with self.subTest(error=method), mock.patch.object(Path, method, side_effect=PermissionError()):
                    self.assertEqual(registry.classify_identity(row, reader)[0], "stale")
            with mock.patch.object(Path, "read_text", side_effect=[raw, raw.replace("42", "43")]):
                self.assertEqual(registry.classify_identity(row, reader)[0], "stale")
            with mock.patch.object(Path, "read_text", return_value="malformed"):
                self.assertEqual(registry.classify_identity(row, reader)[0], "stale")
            with mock.patch.object(Path, "read_bytes", return_value=b"live-command\0"):
                self.assertEqual(registry.classify_identity(row, reader)[0], "stale")

    def test_multi_project_index_and_malformed_registry_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index = root / "index.json"
            good_a = root / "a.json"
            good_b = root / "b.json"
            bad = root / "bad.json"
            identity = {"pid": 7, "starttime": "11", "command_hash": "abc"}
            for path, cwd, run_id in (
                (good_a, "/projects/a", "a1"),
                (good_b, "/projects/b", "b1"),
            ):
                path.write_text(json.dumps({
                    "schema_version": 1,
                    "runs": {run_id: {**identity, "cwd": cwd, "status": "running"}},
                }))
                registry.register_registry(path, index)
            bad.write_text("{")
            # An indexed registry can later become malformed; collection must
            # preserve every other project.
            payload = json.loads(index.read_text())
            payload["registries"]["bad"] = {"path": str(bad)}
            index.write_text(json.dumps(payload))
            rows, diagnostics = registry.scan(index, identity_reader=lambda pid: identity)
            self.assertEqual({row["run_id"] for row in rows}, {"a1", "b1"})
            self.assertEqual({row["cwd"] for row in rows}, {"/projects/a", "/projects/b"})
            self.assertTrue(any(d["kind"] == "malformed-registry" for d in diagnostics))


if __name__ == "__main__":
    unittest.main()
