"""GPU arbitration tests: fake devices, real concurrent CPU-only payloads."""
import concurrent.futures
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpu_leases as G

SOURCE = Path(G.__file__).read_text()
OBS = {"reachable": True, "gpus": [
    {"index": 0, "uuid": "GPU-zero", "free_mib": 24000, "utilization_gpu_pct": 0, "processes": []},
    {"index": 1, "uuid": "GPU-one", "free_mib": 23000, "utilization_gpu_pct": 0, "processes": []}]}


class LeasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "dispatch" / "gpu-leases.json"

    def acquire(self, **kwargs):
        return G.acquire(self.state, OBS, **kwargs)

    def test_selection_and_explicit_conflict_gives_owner_task_time_and_free_gpu(self):
        lease = self.acquire(owner={"label": "SR_CorrNet [cd]"}, task="train", run_id="one")
        self.assertEqual(lease["gpus"], ["0"])
        with self.assertRaisesRegex(G.GPUUnavailable, r"SR_CorrNet \[cd\].*train.*free GPUs: gpu1"):
            self.acquire(requested="0")
        second = self.acquire()
        self.assertEqual(second["gpus"], ["1"])
        with self.assertRaisesRegex(G.GPUUnavailable, "free GPUs: none"):
            self.acquire()

    def test_idle_memory_holding_process_is_occupied_with_or_without_owner(self):
        for owner in ({"label": "TF [33]"}, None):
            observation = json.loads(json.dumps(OBS))
            observation["gpus"][0]["processes"] = [{"pid": 9, "used_memory_mib": 7700,
                                                      "owner": owner, "command": "idle train"}]
            lease = G.acquire(self.state, observation)
            self.assertEqual(lease["gpus"], ["1"])
            G.release(self.state, lease)
            with self.assertRaisesRegex(G.GPUUnavailable, "idle train"):
                G.acquire(self.state, observation, requested="0")

    def test_share_still_records_a_lease_and_default_remains_exclusive(self):
        first = self.acquire(requested="0")
        second = self.acquire(requested="0", share=True)
        self.assertEqual(len(G.snapshot(self.state)), 2)
        G.release(self.state, first)
        with self.assertRaises(G.GPUUnavailable):
            self.acquire(requested="0")
        G.release(self.state, second)
        self.assertEqual(self.acquire(requested="0")["gpus"], ["0"])

    def test_dead_or_reused_pid_is_pruned_immediately_but_unknown_is_retained(self):
        lease = self.acquire(requested="0")
        with G.locked(self.state) as data:
            data["leases"][lease["token"]]["starttime"] = "0"
        self.assertEqual(self.acquire(requested="0")["gpus"], ["0"])
        with G.locked(self.state) as data:
            row = next(iter(data["leases"].values()))
            row["pid_namespace"] = "unknown namespace"
        with self.assertRaises(G.GPUUnavailable):
            self.acquire(requested="0")

    def test_multi_gpu_admission_is_all_or_nothing_and_uuid_is_canonicalized(self):
        self.acquire(requested="1")
        with self.assertRaises(G.GPUUnavailable):
            self.acquire(requested="0,1")
        self.assertEqual(len(G.snapshot(self.state)), 1)
        self.assertEqual(self.acquire(requested="GPU-zero")["gpus"], ["0"])

    def test_unknown_gpu_measurement_refuses_gpu_but_explicit_cpu_still_runs(self):
        for row in ({"reachable": False}, {**OBS, "detail": "smi error"},
                    {**OBS, "process_detail": "process query failed"}):
            with self.assertRaises(G.GPUUnavailable):
                G.acquire(self.state, row)
            self.assertIsNone(G.acquire(self.state, row, requested=""))

    def options(self, name, requested=None):
        run_dir = self.root / name
        inner = ('__HEARTING_GPU_SETUP__ && python3 -c '
                 + "'import os,time; print(os.environ[\"CUDA_VISIBLE_DEVICES\"], flush=True); time.sleep(.6)'"
                 + ' > ' + str(run_dir / "log") + '; printf "%s" "$?" > ' + str(run_dir / "exit_code"))
        return {"state_path": str(self.state), "observation": OBS, "requested": requested,
                "owner": {"label": name}, "task": "CPU fixture", "run_id": "gpu-lease-test-" + name,
                "run_dir": str(run_dir), "inner": inner}

    def launch(self, options):
        code = SOURCE + '\nprint(json.dumps(launch_compute(%r, %r)))' % (options, SOURCE)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_two_concurrent_sessions_choose_distinct_gpus_and_release_on_exit(self):
        options = [self.options("one"), self.options("two")]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(self.launch, options))
        self.assertEqual({row["gpus"] for row in receipts}, {"0", "1"})
        deadline = time.monotonic() + 5
        while G.snapshot(self.state) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(G.snapshot(self.state), [])
        for opts, receipt in zip(options, receipts):
            self.assertEqual((Path(opts["run_dir"]) / "log").read_text().strip(), receipt["gpus"])
            self.assertEqual((Path(opts["run_dir"]) / "exit_code").read_text(), "0")

    def test_resource_and_compute_share_the_same_admission(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.state.parent / "jobs.log")}), \
             mock.patch.object(G, "local_observation", return_value=OBS), \
             mock.patch.object(G, "launcher_owner", return_value={"label": "resource [ab]"}):
            path, lease, env = G.resource_admission({"resource_class": "gpu"}, ["true"], run_id="resource")
        self.assertEqual(path, self.state)
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "0")
        receipt = self.launch(self.options("compute"))
        self.assertEqual(receipt["gpus"], "1")
        self.assertEqual(subprocess.run(["sh", "-c", env["HEARTING_GPU_LEASE_RELEASE"]]).returncode, 0)
        self.assertNotIn(lease["token"], {r["token"] for r in G.snapshot(self.state)})

    def test_unbound_wrapper_never_runs_after_its_pending_reservation_was_reaped(self):
        lease = self.acquire()
        G.release(self.state, lease)
        marker = self.root / "forbidden"
        with self.assertRaises(G.GPUUnavailable):
            G.payload(str(self.state), lease, "touch " + str(marker))
        self.assertFalse(marker.exists())

    def test_resource_runner_fences_records_and_releases_actual_gpu_payload(self):
        spec = importlib.util.spec_from_file_location("_gpu_test_runner", Path(G.__file__).with_name("resource-runner.py"))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        route = self.root / "route.json"
        route.write_text(json.dumps({"capability": "autopilot-lab", "artifact_root": str(self.root),
            "nodes": [{"id": "gpu", "kind": "resource-runner", "resource_class": "gpu",
                       "resource_transport": "detached-process"}]}))
        registry, log = self.root / "runs.json", self.root / "resource.log"
        output, children = io.StringIO(), []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.state.parent / "jobs.log")}), \
             mock.patch.object(G, "local_observation", return_value=OBS), \
             mock.patch.object(runner.subprocess, "run"), mock.patch.object(runner, "register_registry"), \
             mock.patch.object(runner.subprocess, "Popen", side_effect=spawn), mock.patch("sys.stdout", output):
            runner.main(["--registry", str(registry), "start", "--run-id", "local", "--cwd", str(self.root),
                         "--log", str(log), "--route", str(route), "--node", "gpu", "--smoke-attestation", "unused",
                         "--", sys.executable, "-c", "import os; print(os.environ['CUDA_VISIBLE_DEVICES'])"])
            children[0].wait(timeout=5)
        row = json.loads(output.getvalue())
        self.assertEqual(row["gpus"], "0")
        self.assertEqual(log.read_text().strip(), "0")
        self.assertEqual(Path(row["sentinel"]).read_text(), "0")
        self.assertEqual(G.snapshot(self.state), [])

    def test_composed_probe_script_compiles_without_changing_early_exit_indentation(self):
        spec = importlib.util.spec_from_file_location("_gpu_test_compute", Path(G.__file__).with_name("compute-hosts.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        def check_script(_host, script, **kwargs):
            body = script.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            compile(body, "composed-probe", "exec")
            return subprocess.CompletedProcess([], 0, json.dumps({"gpus": [], "gpu_leases": []}), "")
        with mock.patch.object(tool, "remote", side_effect=check_script), \
             mock.patch.object(G, "state_path", return_value=self.state):
            self.assertTrue(tool.probe_host("fixture", {"ssh_host": "local"}, [], [])["reachable"])


if __name__ == "__main__":
    unittest.main()
