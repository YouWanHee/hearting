#!/usr/bin/env python3
"""Deterministic harness checks for production PTY measurement contracts."""
import importlib.util
import ast
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).with_name("benchmark_navigation.py")
SPEC = importlib.util.spec_from_file_location("benchmark_navigation", MODULE_PATH)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class BenchmarkNavigationReadinessTest(unittest.TestCase):

    def test_real_detail_observation_is_recognized_by_both_measurement_paths(self):
        from tools.fleet.refresh import LiveSnapshot, set_required_details, required_details_complete

        snapshot = LiveSnapshot(observations={
            "basic": {"state": "ok", "generation_identity": "current-basic"}})
        set_required_details(snapshot, "ok", all_new=True)
        self.assertTrue(required_details_complete(snapshot.observations))
        readiness = {"ready": True, "fully_fresh": True, "all_new": True,
                     "sources": snapshot.observations}
        metrics = benchmark._frame_metrics(
            readiness, generation=1, replay_active=False,
            frame_mode="overview", frame_present=True)
        self.assertTrue(metrics["fresh_all_new"])

        # Exercise the code shipped to the real launcher without installing its
        # process-wide observation wrappers in this test process.
        functions = [node for node in ast.parse(benchmark._observer_source()).body
                     if isinstance(node, ast.FunctionDef)
                     and node.name in ("_states", "_frame_event")]
        namespace = {}
        exec(compile(ast.Module(body=functions, type_ignores=[]),
                     "production-observer", "exec"), namespace)
        render = SimpleNamespace(_EXPLORER_FRAME=SimpleNamespace(mode="overview"),
                                 _EXPLORER_READINESS=readiness,
                                 _EXPLORER_REPLAY_ACTIVE=False,
                                 _EXPLORER_GENERATION=1)
        self.assertTrue(namespace["_frame_event"](render)["fresh_all_new"])
        self.assertEqual(namespace["_states"](snapshot.observations),
                         namespace["_states"](readiness))
        from tools.fleet.explorer.runtime import ExplorerRuntime
        render._EXPLORER_FRAME = ExplorerRuntime().loading_frame(100, 45)
        loading = namespace["_frame_event"](render)
        self.assertTrue(loading["loading"])
        self.assertFalse(loading["fresh_complete"])

    def _readiness(self, *, detail="complete", all_new=True):
        ready = detail == "complete" or detail == "continued"
        return {"ready": ready, "fully_fresh": ready,
                "all_new": all_new, "pending": [] if detail == "complete" else ["required-details"],
                "sources": {
                    "basic": {"state": "complete", "all_new": True},
                    "required-details": {"state": detail, "all_new": all_new,
                                         "continuation_validated": detail == "continued"},
                    "resource-runs": {"state": "absent", "attempted": True, "all_new": True},
                }}

    def test_generation_with_pending_required_detail_is_not_fresh(self):
        metrics = benchmark._frame_metrics(
            self._readiness(detail="pending"), generation=8,
            replay_active=False, frame_mode="overview", frame_present=True)
        self.assertFalse(metrics["ready"])
        self.assertFalse(metrics["fresh_complete"])
        self.assertFalse(metrics["fresh_all_new"])

    def test_loading_and_replay_frames_are_not_fresh(self):
        readiness = self._readiness()
        loading = benchmark._frame_metrics(
            readiness, generation=4, replay_active=False,
            frame_mode="loading", frame_present=True)
        replay = benchmark._frame_metrics(
            readiness, generation=4, replay_active=True,
            frame_mode="overview", frame_present=True)
        self.assertFalse(loading["fresh_complete"])
        self.assertFalse(replay["fresh_complete"])
        self.assertTrue(replay["replay"])

    def test_fresh_and_all_new_are_separate_predicates(self):
        metrics = benchmark._frame_metrics(
            self._readiness(all_new=False), generation=2,
            replay_active=False, frame_mode="overview", frame_present=True)
        self.assertTrue(metrics["fresh_complete"])
        self.assertFalse(metrics["fresh_all_new"])

    def test_validated_continuation_is_fresh_but_not_all_new(self):
        metrics = benchmark._frame_metrics(
            self._readiness(detail="continued", all_new=False), generation=2,
            replay_active=False, frame_mode="overview", frame_present=True)
        self.assertTrue(metrics["fresh_complete"])
        self.assertFalse(metrics["fresh_all_new"])
        pending = self._readiness(detail="continued", all_new=False)
        pending["sources"]["required-details"]["continuation_validated"] = False
        self.assertFalse(benchmark._frame_metrics(
            pending, generation=2, replay_active=False,
            frame_mode="overview", frame_present=True)["fresh_complete"])

    def test_production_observer_wraps_completed_window_refresh(self):
        nodes = ast.parse(benchmark._observer_source()).body
        cls = next(node for node in nodes if isinstance(node, ast.ClassDef)
                   and node.name == "WindowRefreshObserver")
        namespace = {}
        exec(compile(ast.Module(body=[cls], type_ignores=[]),
                     "production-refresh-observer", "exec"), namespace)
        calls = []

        class Window:
            def refresh(self):
                calls.append("refresh-completed")
                return 17

        observer = namespace["WindowRefreshObserver"](
            Window(), lambda: calls.append("before"),
            lambda succeeded: calls.append(("after", succeeded)))
        self.assertEqual(observer.refresh(), 17)
        self.assertEqual(calls, ["before", "refresh-completed", ("after", True)])


class BenchmarkProductionContractTest(unittest.TestCase):

    def test_production_observer_source_compiles_with_optional_startup_profiling(self):
        compile(benchmark._observer_source(), "production-observer", "exec")
        profile = benchmark._startup_profile_summary([
            {"kind": "installinfo-call", "fast_local": True, "duration_ns": 500},
            {"kind": "synchronous-draw", "loading": True, "duration_ns": 300},
            {"kind": "readiness-check", "ready": False, "duration_ns": 100},
        ])
        imports = benchmark._importtime_profile(
            b"import time: 10 | 30 | fleet.collectors\n"
            b"import time: 5 | 8 | json\n")
        self.assertEqual(imports["entry_count"], 2)
        self.assertEqual(imports["largest_cumulative"][0]["module"], "fleet.collectors")
        self.assertEqual(len(profile["installinfo_calls"]), 1)
        self.assertEqual(len(profile["synchronous_draw_samples"]), 1)
        self.assertEqual(len(profile["readiness_check_samples"]), 1)

    def test_production_schedule_keeps_exact_key_count_and_duration(self):
        schedule = benchmark._production_schedule(100, 60)
        self.assertEqual(len(schedule), 100)
        self.assertEqual(schedule[0], 0)
        self.assertEqual(schedule[-1], 59.4)
        self.assertEqual(benchmark._production_schedule(0, 60), [])

    def test_production_order_alternates_pinned_arms(self):
        self.assertEqual(benchmark._production_order(), [
            ["base", "candidate", "legacy"],
            ["candidate", "base", "legacy"],
            ["base", "candidate", "legacy"],
        ])

    def test_production_argv_uses_real_shell_entry_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            trees = {}
            for role, launcher in (("base", "fleet2.sh"),
                                   ("candidate", "fleet2.sh"),
                                   ("legacy", "fleet.sh")):
                tree = Path(tmp) / role
                path = tree / "tools/fleet" / launcher
                path.parent.mkdir(parents=True)
                path.write_text("#!/bin/sh\n", encoding="utf-8")
                trees[role] = tree
            for role, launcher in (("base", "fleet2.sh"),
                                   ("candidate", "fleet2.sh"),
                                   ("legacy", "fleet.sh")):
                self.assertEqual(benchmark._production_argv(role, trees), [
                    str(trees[role].resolve() / "tools/fleet" / launcher)
                ])

    def test_environment_preserves_live_registry_and_xdg_but_clears_worker_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observer = root / "observer"
            cache = root / "cache"
            with mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_JOBS": "/private/registry/jobs.log",
                "AGENT_ROUTE_FILE": "/private/worker/route.json",
                "AGENT_DISPATCH_MODEL_ROLE": "fast-implementer",
                "XDG_STATE_HOME": "/private/user/state",
                "PYTHONPATH": "/existing/python/path",
            }, clear=True):
                env = benchmark._production_environment(
                    root, cache, observer, 22, 1234, root / "runtime")
            self.assertEqual(env["AGENT_DISPATCH_JOBS"], "/private/registry/jobs.log")
            self.assertEqual(env["XDG_STATE_HOME"], "/private/user/state")
            self.assertNotIn("AGENT_ROUTE_FILE", env)
            self.assertNotIn("AGENT_DISPATCH_MODEL_ROLE", env)
            self.assertNotIn("PYTHONDONTWRITEBYTECODE", env)
            self.assertEqual(env["PYTHONPYCACHEPREFIX"], str(root / "runtime" / "pycache"))
            self.assertTrue(env["PYTHONPATH"].startswith(str(observer)))

    def test_explicit_no_bytecode_setting_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}, clear=True):
                env = benchmark._production_environment(
                    root, root / "cache", root / "observer", 22, 1234,
                    root / "runtime")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_fresh_paint_does_not_mask_nonzero_process_exit(self):
        # A painted fresh frame is evidence of rendering, not a successful process exit.
        status, error = benchmark._production_status(
            1, agent_home_matches_tree=True, seed=False, cache_seed_valid=False)
        self.assertEqual(status, "nonzero-exit")
        self.assertIsNone(error)

    def test_post_exit_drain_keeps_trailing_telemetry_and_nonzero_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "fake-fleet2"
            launcher.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "fd = int(os.environ['FLEET_BENCHMARK_TELEMETRY_FD'])\n"
                "record = b'{\"kind\":\"diagnostic-padding\",\"padding\":\"' + b'x' * 96 + b'\"}\\n'\n"
                "payload = record * 3200\n"
                "while payload:\n"
                "    written = os.write(fd, payload)\n"
                "    payload = payload[written:]\n"
                "os.write(fd, b'{\"kind\":\"process-exit\",\"process_times\":{}}\\n')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8")
            launcher.chmod(0o700)
            with mock.patch.object(benchmark, "_production_argv",
                                   return_value=[str(launcher)]):
                result = benchmark._production_run(
                    "candidate", root, root / "cache", root / "observer",
                    root / "runtime", root / "run", keys=0, duration=0,
                    event_timeout=1)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["status"], "nonzero-exit")
        self.assertTrue(result["shutdown"]["process_exit_event_seen"])
        self.assertTrue(result["shutdown"]["telemetry_eof_seen"])
        self.assertGreater(result["telemetry_raw"]["bytes"], 262144)

    def test_missing_or_unpainted_keys_are_retained_as_samples(self):
        sent = [
            {"sample": 0, "parent_write_ns": 100},
            {"sample": 1, "parent_write_ns": 200},
            {"sample": 2, "parent_write_ns": 300, "write_error": "OSError"},
            {"sample": 3, "parent_write_ns": 400},
        ]
        events = [
            {"kind": "input-decoded", "input_id": 1, "time_ns": 150},
            {"kind": "flush", "input_id": 1, "time_ns": 250},
            {"kind": "input-decoded", "input_id": 2, "time_ns": 270},
            {"kind": "input-decoded", "input_id": 4, "time_ns": 450},
            {"kind": "flush", "input_id": 4, "time_ns": 500},
        ]
        samples = benchmark._match_production_keys(sent, events)
        self.assertEqual(len(samples), len(sent))
        self.assertEqual([row["status"] for row in samples], [
            "painted", "decoded-no-flush", "write-error", "painted",
        ])
        self.assertEqual(samples[0]["end_to_flush_ms"], 0.00015)
        self.assertIsNone(samples[1]["end_to_flush_ms"])

    def test_cache_validation_requires_private_complete_all_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps({"fresh_complete": True, "all_new": True}),
                            encoding="utf-8")
            path.chmod(0o600)
            self.assertTrue(benchmark._valid_cache(path))
            path.chmod(0o644)
            self.assertFalse(benchmark._valid_cache(path))

    def test_raw_runtime_writer_creates_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "capture.raw"
            benchmark._write_private_bytes(path, b"private payload")
            self.assertEqual(path.read_bytes(), b"private payload")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_pump_records_distinguish_initial_and_steady_phases(self):
        samples = benchmark._pump_phase_samples([
            {"kind": "pump-start", "name": "fleet-refresh", "time_ns": 10,
             "interval_seconds": 2},
            {"kind": "pump-done", "name": "fleet-refresh", "duration_ns": 5,
             "cpu_ns": 3},
            {"kind": "pump-start", "name": "fleet-refresh", "time_ns": 2_000_000_010,
             "interval_seconds": 2},
            {"kind": "pump-done", "name": "fleet-refresh", "duration_ns": 7,
             "cpu_ns": 4},
            {"kind": "pump-done", "name": "fleet-detail-refresh", "duration_ns": 8,
             "cpu_ns": 2},
        ])
        done = [row for row in samples if row.get("duration_ns") is not None]
        self.assertEqual([row["phase"] for row in done], [
            "first-basic", "steady-basic", "required-detail",
        ])
        other = benchmark._pump_phase_samples([
            {"kind": "pump-done", "name": "fleet-install-refresh", "duration_ns": 3},
            {"kind": "pump-done", "name": "fleet-selection", "duration_ns": 4},
        ])
        self.assertEqual([row["phase"] for row in other], ["install", "selection"])
        cadence = [row for row in samples if row["phase"] == "start"][-1]
        self.assertEqual(cadence["observed_cadence_seconds"], 2)


if __name__ == "__main__":
    unittest.main()
