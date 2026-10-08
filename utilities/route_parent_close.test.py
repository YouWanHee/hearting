#!/usr/bin/env python3
"""Safe real-process fixtures for parent close; no provider or live project runs."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch_contract as DC
import route_parent_close as CLOSE
import dispatch_completion_join as JOIN
import dispatch_attempt_policy as POLICY
import workflow_state as WS
import resource_run_registry as RR
import work_start
sys.path.insert(0, str(HERE.parent / "tools"))
import fixture_processes


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROUTE = load("parent_close_route_fixture", "capability-route.py")
SUP = load("parent_close_supervisor_fixture", "workflow-supervisor.py")


class ParentCloseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.jobs = self.base / "dispatch" / "fixture-jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.write_text("")
        self.artifacts = self.base / ".agent_reports"
        self.artifacts.mkdir()
        self.processes = []
        self.suffix = self.base.name
        self.addCleanup(self.reap)
        self.addCleanup(fixture_processes.reap, str(self.base))
        env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_")
               and key not in {"CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID"}}
        env.update(AGENT_HOME=str(HERE.parent), AGENT_DISPATCH_JOBS=str(self.jobs),
            AGENT_ARTIFACT_ROOT=str(self.artifacts), AGENT_ARTIFACT_CHECKPOINT="off",
            AGENT_RESOURCE_RUN_INDEX=str(self.base / "resource-index.json"),
            XDG_STATE_HOME=str(self.base / "state"), CODEX_THREAD_ID="fixture-parent",
            HEARTING_GATES="off", COMPUTE_HOSTS_CONFIG=str(self.base / "no-compute.yaml"))
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        tuples = [{"parent_harness": h, "child_harness": h, "parent_transport": "headless",
                   "parent_sandbox": ROUTE.WRAPPER_PARENT_SANDBOXES[h][0], "status": "supported",
                   "launch_authority": "conductor", "probe_source": "fixture",
                   "probe_time": "2026-10-08T00:00:00Z", "failure_class": "",
                   "checked_worktree": str(self.base), "failure_scope": "none",
                   "codex_command": "ok" if h == "codex" else "not-applicable",
                   "retry_on_isolated_worktree": 0} for h in ("claude", "codex", "opencode")]
        self.route = ROUTE.compile_route("autopilot-code", "dev", "standard", self.base,
            self.artifacts, predicates=[], transport="headless", transport_evidence="fixture",
            inline_reason=None, tracking="tracked", dispatch_evidence={"tuples": tuples, "native_subagent": []},
            tracked_gate_evidence={"spec_read": {"satisfied": True, "source": "fixture"},
                "drift_verdict": "within-spec", "workflow_mode": "tracked",
                "artifact_guard": {"satisfied": True, "source": "fixture"}})
        self.path = self.artifacts / ".runtime" / "routes" / (self.route["route_id"] + ".json")
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.route))

    def reap(self):
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()

    def aid(self, name):
        return name + "-" + self.suffix

    def process(self, aid, *, ignore_term=False, script=None):
        script = script or ("import signal,time; " +
            ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "") +
            "print('ready',flush=True); time.sleep(300)")
        process = subprocess.Popen([sys.executable, "-c", script], start_new_session=True,
            stdout=subprocess.PIPE, text=True, env={**os.environ, "AGENT_DISPATCH_ATTEMPT_ID": self.aid(aid)})
        self.processes.append(process)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0])
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    def row(self, aid, *, parent=None, process=None, harness="codex", status="open", worktree=None, **extra):
        aid = self.aid(aid)
        parent = self.aid(parent) if parent else None
        meta = {"attempt_schema_version": "2", "dispatch_depth": "2" if parent else "1",
                "transport": "headless", "execution_surface": "registered-headless",
                "fallback_hop": "same-harness-headless",
                "registered_worker": "1", "attempt_id": aid, "harness": harness,
                "launch_claimed": "1" if process else "0"}
        if parent:
            meta.update(parent_attempt_id=parent, route_id=self.route["route_id"],
                        route_hash=self.route["route_hash"], worker_type="stage")
        else:
            meta.update(worker_type="owner", unit="_kernel/owner", parent_sid="fixture-parent",
                owner_route_file=str(self.path), owner_route_id=self.route["route_id"],
                owner_route_hash=self.route["route_hash"], capability="autopilot-code",
                capability_mode="dev", intensity="standard", owner_harness=harness,
                artifact_root=str(self.artifacts))
        if process:
            meta.update(DC.process_launch_identity(process.pid), launch_started="1")
        meta.update(extra)
        DC.validate_attempt_metadata(meta)
        row = "\t".join(["2026-10-08T00:00:00Z", status, str(self.base), str(worktree or self.base), aid,
                         ",".join(k + "=" + str(v) for k, v in meta.items())]) + "\n"
        with self.jobs.open("a") as handle:
            handle.write(row)
        return meta

    def close(self, **kwargs):
        return CLOSE.close(self.route, self.path, jobs=self.jobs, **kwargs)

    def test_three_harnesses_inline_and_parked_owner_close_real_processes(self):
        for harness in ("claude", "codex", "opencode"):
            for parked in (False, True):
                with self.subTest(harness=harness, parked=parked):
                    # Each independent fixture gets its own route and registry.
                    child = ParentCloseTest()
                    child.setUp()
                    try:
                        owner = child.process("att-owner")
                        child.row("att-owner", process=owner, harness=harness)
                        if parked:
                            worker = child.process("att-worker")
                            child.row("att-worker", parent="att-owner", process=worker, harness=harness)
                        result = child.close()
                        self.assertEqual(result["state"], "cancelled")
                        self.assertEqual(owner.wait(timeout=2), -signal.SIGTERM)
                        if parked:
                            self.assertEqual(worker.wait(timeout=2), -signal.SIGTERM)
                        self.assertEqual({r[1]["note"] for r in CLOSE._rows(child.jobs).values()}, {CLOSE.NOTE})
                    finally:
                        child.doCleanups()

    def test_never_started_chain_and_post_nodes_fold_no_start_advice(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        serial = dict(session_chain_id="ssc-fixture", subsession_mode="serial",
                 route_node=self.route["nodes"][0]["id"], stage_authority="0",
                 subsession_count="3", subsession_purpose="planned", expected_round_trips="1",
                 phase_brief=str(self.base / "brief.md"), state_ledger=str(self.base / "state.json"),
                 phase_brief_sha256="a" * 64, fixed_files_sha256="b" * 64, narrow_verify_sha256="c" * 64)
        linked = self.base / "linked-slice"
        linked.mkdir()
        worker = self.process("att-slice")
        self.row("att-slice", parent="att-owner", process=worker, worktree=linked,
                 subsession_id="ss-current", subsession_index="1", **serial)
        self.row("att-next", parent="att-owner", worktree=linked,
                 subsession_id="ss-next", subsession_index="2", **serial)
        result = self.close()
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(worker.wait(timeout=2), -signal.SIGTERM)
        next_row = CLOSE._rows(self.jobs)[self.aid("att-next")]
        self.assertEqual(next_row[1]["launch_outcome"], "never-launched")
        self.assertEqual(POLICY.required_action(next_row[0][1], next_row[1]), "advance-completed")
        with mock.patch.object(work_start, "_advance", side_effect=AssertionError("started")):
            replay = work_start.start_work(self.route, self.path, self.jobs)
        self.assertEqual(replay["state"], "cancelled")
        self.assertNotIn("resume_command", replay)
        self.assertEqual({n["state"] for n in WS.WorkflowLedger(self.route["route_id"],
            self.route["route_hash"], jobs=self.jobs).state()["nodes"].values()}, {"CANCELLED"})

    def test_committed_pass_wins_then_cancel_intent_wins_over_late_pass(self):
        self.row("att-owner", status="done", note="completed-supervisor", failure_class="pass")
        self.assertIsNone(self.close())
        self.jobs.write_text("")
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        value = CLOSE.request(self.route, self.path, jobs=self.jobs)
        self.assertEqual(DC.reconcile_attempt_terminal(self.jobs, self.aid("att-owner"), "completed-supervisor",
            evidence={"failure_class": "pass"}), "cancellation-pending")
        self.assertEqual(CLOSE.continue_close(value, jobs=self.jobs)["state"], "cancelled")
        self.assertEqual(DC.reconcile_attempt_terminal(self.jobs, self.aid("att-owner"), "dead-worker-fail",
            evidence={"failure_class": "fail"}), "already-terminal")
        self.assertNotIn("terminal_conflict", CLOSE._rows(self.jobs)[self.aid("att-owner")][1])

    def test_term_ignored_escalates_only_fixture_pid(self):
        owner = self.process("att-owner", ignore_term=True)
        self.row("att-owner", process=owner)
        self.assertEqual(self.close()["state"], "cancelled")
        self.assertEqual(owner.wait(timeout=2), -signal.SIGKILL)

    def test_unobservable_stays_pending_then_restart_closes_without_artifacts(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        with mock.patch.object(CLOSE, "_agent_processes", return_value=([], False)), \
                mock.patch.object(CLOSE, "_signal") as signal_mock:
            self.assertEqual(self.close()["state"], "termination-pending")
            signal_mock.assert_not_called()
        # A fresh execution runner replays the existing journal; output can be gone.
        payload = self.artifacts / "campaigns"
        payload.mkdir()
        (payload / "report.md").write_text("temporary output")
        shutil.rmtree(payload)
        result = CLOSE.recover_attempt(self.jobs, CLOSE._rows(self.jobs)[self.aid("att-owner")][1])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(self.close(), result)

    def test_pid_reuse_does_not_signal_new_process(self):
        owner = self.process("att-owner")
        meta = self.row("att-owner", process=owner)
        identity = DC.AuthoritativeProcessIdentity("local", owner.pid, meta["pid_start"])
        with mock.patch.object(DC, "attempt_process_quiescence", return_value=DC.ProcessQuiescence(
                "quiescent", "pid-reused", identity=identity)), \
             mock.patch.object(DC, "process_observation", return_value=("present", "different-birth", "S")), \
             mock.patch.object(DC, "attempt_tagged_descendants", return_value=DC.ProcessGroupObservation("empty")), \
             mock.patch.object(CLOSE, "_signal") as signal_mock:
            self.assertEqual(self.close()["state"], "cancelled")
            signal_mock.assert_not_called()
        self.assertIsNone(owner.poll())

    def resource(self, process, aid="att-owner", rid="fixture-run"):
        registry = self.base / "resources.json"
        row = {**RR.proc_identity(process.pid), "run_id": rid, "process_group": os.getpgid(process.pid),
               "route": str(self.path), "node": self.route["nodes"][0]["id"],
               "parent_attempt_id": self.aid(aid), "jobs": str(self.jobs), "status": "running"}
        registry.write_text(json.dumps({"schema_version": 1, "runs": {rid: row}}))
        RR.register_registry(registry)
        return registry, row

    def test_gpu_default_preserved_and_opt_in_stops_only_linked_run(self):
        for stop in (False, True):
            with self.subTest(stop=stop):
                child = ParentCloseTest()
                child.setUp()
                try:
                    owner = child.process("att-owner")
                    gpu = child.process("att-owner")
                    unrelated = child.process("att-foreign")
                    child.row("att-owner", process=owner)
                    child.resource(gpu)
                    result = child.close(stop_resources=stop)
                    gpu.poll()
                    self.assertEqual(result["state"], "cancelled")
                    self.assertEqual(gpu.poll() is None, not stop)
                    self.assertIsNone(unrelated.poll())
                    self.assertEqual(result["resources"][0]["preserved"], not stop)
                finally:
                    child.doCleanups()

    def test_parent_completion_cancel_is_delivery_success_without_failure_prompt(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        self.close()
        state = JOIN.current_delivery_state(self.jobs, self.aid("att-owner"), parent_attempt_id="fixture-parent")
        self.assertEqual(JOIN.delivery_classification(state), "success")
        self.assertEqual(JOIN.delivery_required_action(state), "advance-completed")
        self.assertTrue(state.cancelled)
        self.assertEqual(state.verdict, "CANCELLED")
        receipt = DC._terminal_delivery_receipt(CLOSE._rows(self.jobs)[self.aid("att-owner")][1])
        self.assertEqual(receipt["delivery_classification"], "success")
        self.assertEqual(receipt["children"][0]["reason"], CLOSE.NOTE)
        import human_gate_receipt as HG
        # A cancelled gate is consumed before looking for removed artifacts.
        with self.assertRaisesRegex(HG.HumanGateReceiptError, "route-already-closed"):
            HG._load_route({"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                            "job_registry": str(self.jobs), "route_file": str(self.path)})

    def test_real_cli_close_and_duplicate_close_need_only_existing_command(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        command = [sys.executable, str(HERE / "capability-route.py"), "close", "--route", str(self.path)]
        first = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["state"], "cancelled")
        self.assertNotIn("terminal-gate-unproven", first.stderr)
        replay = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertEqual(json.loads(replay.stdout), json.loads(first.stdout))

    def test_resource_in_owner_group_survives_and_missing_output_does_not_change_replay(self):
        pidfile = self.base / "child.pid"
        script = ("import os,time; p=os.fork(); "
            "open(" + repr(str(pidfile)) + ",'w').write(str(p)) if p else None; "
            "print('ready',flush=True) if p else None; time.sleep(300)")
        owner = self.process("att-owner", script=script)
        resource_pid = int(pidfile.read_text())
        self.row("att-owner", process=owner)
        from types import SimpleNamespace
        registry, run = self.resource(SimpleNamespace(pid=resource_pid))
        self.assertEqual(os.getpgid(resource_pid), owner.pid)
        self.assertEqual(self.close()["state"], "cancelled")
        self.assertEqual(RR.classify_identity(run)[0], "working")
        registry.unlink()
        self.assertEqual(self.close()["state"], "cancelled")
        self.assertEqual(RR.classify_identity(run)[0], "working")

    def test_cancelled_watcher_records_result_without_missing_artifact_or_successor(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        self.close()
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        armed = {"node": "fixture-resource", "predecessor_kind": "resource"}
        evidence = {"terminal": True, "succeeded": True, "identity": "fixture-run:ended"}
        with mock.patch.object(SUP, "read_armed", return_value={"fixture-resource": armed}), \
             mock.patch.object(SUP, "resource_evidence", return_value=evidence), \
             mock.patch.object(SUP, "_start_successor", side_effect=AssertionError("successor")), \
             mock.patch.object(SUP, "artifact_evidence", side_effect=AssertionError("artifact")):
            self.assertEqual(SUP.poll_once(self.route, ledger)[0]["action"], "cancelled-result")
            SUP.poll_once(self.route, ledger)
        results = [e for e in ledger.journal() if (e.get("evidence") or {}).get("cancelled_resource_result")]
        self.assertEqual(len(results), 1)
        self.assertEqual(ledger.state()["workflow_state"], "CANCELLED")

    def test_other_parent_cannot_close_and_done_child_is_drained_without_changing_pass(self):
        owner = self.process("att-owner")
        worker = self.process("att-worker")
        self.row("att-owner", process=owner)
        self.row("att-worker", parent="att-owner", process=worker, status="done",
                 note="completed-supervisor", failure_class="pass")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "foreign-parent"}):
            with self.assertRaisesRegex(ValueError, "parent-close-owner-not-owned"):
                self.close()
        self.assertIsNone(owner.poll())
        self.assertEqual(self.close()["state"], "cancelled")
        self.assertEqual(worker.wait(timeout=2), -signal.SIGTERM)
        self.assertEqual(CLOSE._rows(self.jobs)[self.aid("att-worker")][1]["failure_class"], "pass")

    def test_crash_after_journal_intent_fences_late_pass_and_launch(self):
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        self.row("att-next", parent="att-owner")
        original = self.jobs.read_text()
        CLOSE.request(self.route, self.path, jobs=self.jobs)
        self.jobs.write_text(original)  # writer died after intent, before annotations
        self.assertEqual(DC.reconcile_attempt_terminal(self.jobs, self.aid("att-owner"),
            "completed-supervisor", evidence={"failure_class": "pass"}), "cancellation-pending")
        with mock.patch.object(CLOSE.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(DC.DispatchContractError, "cancelled-by-parent"):
                DC.spawn_claimed_attempt(self.jobs, self.aid("att-next"), parent_binding=None, spawn=spawn)
            spawn.assert_not_called()
        self.assertEqual(work_start.start_work(self.route, self.path, self.jobs)["state"], "cancelled")

    def test_before_first_dispatch_no_registry_means_no_close_intent(self):
        self.jobs.unlink()
        self.assertIsNone(CLOSE.intent(self.route, self.jobs))
        self.assertIsNone(self.close())
        self.assertFalse((self.jobs.parent / "workflow").exists())

    def test_simultaneous_pass_and_cancel_have_one_committed_outcome(self):
        for _ in range(6):
            child = ParentCloseTest()
            child.setUp()
            try:
                child.row("att-owner")
                barrier = threading.Barrier(2)
                results, errors = {}, []
                def run(name, function):
                    try:
                        barrier.wait(timeout=5)
                        results[name] = function()
                    except BaseException as exc:
                        errors.append(exc)
                threads = [threading.Thread(target=run, args=("pass", lambda:
                    DC.reconcile_attempt_terminal(child.jobs, child.aid("att-owner"),
                        "completed-supervisor", evidence={"failure_class": "pass"}))),
                    threading.Thread(target=run, args=("close", child.close))]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                status, meta = CLOSE._rows(child.jobs)[child.aid("att-owner")]
                outcome = POLICY.committed_outcome(status[1], meta)
                self.assertIn(outcome, {"succeeded", "cancelled"})
                self.assertEqual(results["close"] is None, outcome == "succeeded")
                self.assertNotIn("terminal_conflict", meta)
            finally:
                child.doCleanups()

    def test_stop_resources_selects_shared_group_branch_and_leaves_foreign_run(self):
        pidfile = self.base / "resource.pid"
        script = ("import os,time,signal; p=os.fork(); "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN) if not p else None; "
            "open(" + repr(str(pidfile)) + ",'w').write(str(p)) if p else None; "
            "print('ready',flush=True) if p else None; time.sleep(300)")
        owner = self.process("att-owner", script=script)
        self.row("att-owner", process=owner)
        from types import SimpleNamespace
        resource_pid = int(pidfile.read_text())
        _, run = self.resource(SimpleNamespace(pid=resource_pid))
        foreign = self.process("att-foreign")
        result = self.close(stop_resources=True)
        self.assertEqual(result["state"], "cancelled")
        self.assertNotEqual(RR.classify_identity(run)[0], "working")
        self.assertIsNone(foreign.poll())

    def test_close_finalizes_existing_cycle_and_deleted_output_is_replayable(self):
        import artifact_producer as AP
        owner = self.process("att-owner")
        self.row("att-owner", process=owner)
        begun = AP.begin(self.artifacts, route_file=self.path, capability="autopilot-code", intensity="standard")
        output = Path(begun["cycle_dir"]) / "artifacts/plans/fixture/REPORT.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("fixture output")
        result = self.close()
        self.assertEqual(result["state"], "cancelled")
        record = AP.read_cycle_record(self.artifacts, begun["cycle_id"])
        self.assertEqual(record["state"], "sealed")
        self.assertEqual(AP._published_cycle_state(self.artifacts, record), "abandoned")
        self.assertEqual(output.read_text(), "fixture output")
        shutil.rmtree(Path(begun["cycle_dir"]))
        self.path.with_suffix(".outcome.json").unlink()
        with mock.patch.object(CLOSE, "_agent_processes", side_effect=AssertionError("already settled")):
            self.assertEqual(self.close(), result)

    def test_simultaneous_close_reuses_one_cancelled_result(self):
        self.row("att-owner")
        barrier = threading.Barrier(2)
        results, errors = [], []
        def close():
            try:
                barrier.wait(timeout=5)
                results.append(self.close())
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=close) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        self.assertEqual(sum("parent_close_result" in (e.get("evidence") or {}) for e in ledger.journal()), 1)

    def test_compute_stop_uses_exact_route_attempt_and_original_configuration(self):
        from types import SimpleNamespace
        self.row("att-owner")
        run_root = self.base / "compute-runs"
        for rid, aid, path in (("own-run", self.aid("att-owner"), str(self.path)),
                               ("foreign-run", "foreign-attempt", str(self.path)),
                               ("other-route", self.aid("att-owner"), "/foreign/route.json")):
            directory = run_root / rid
            directory.mkdir(parents=True)
            (directory / "meta.json").write_text(json.dumps({"provenance": {
                "attempt_id": aid, "route": {"route_id": self.route["route_id"], "route_file": path}}}))
        stopped = []
        compute = SimpleNamespace(ConfigError=ValueError, load_config=lambda: {"run_root": run_root},
            config_path=lambda: self.base / "original-config.yaml", _run_state=lambda config, rid:
                {"stop_reason": "parent" if rid in stopped else None,
                 "state": "finished" if rid in stopped else "running"})
        actual_run = subprocess.run
        def stop(command, **kwargs):
            if len(command) < 2 or Path(command[1]).name != "compute-hosts.py":
                return actual_run(command, **kwargs)
            self.assertEqual(command[-2:], ["stop", "own-run"])
            self.assertEqual(kwargs["env"]["COMPUTE_HOSTS_CONFIG"], str(self.base / "original-config.yaml"))
            stopped.append(command[-1])
            return SimpleNamespace(returncode=0)
        with mock.patch.object(CLOSE, "_compute", return_value=compute), \
             mock.patch.object(CLOSE.subprocess, "run", side_effect=stop):
            result = self.close(stop_resources=True)
        self.assertEqual(stopped, ["own-run"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual([r["run_id"] for r in result["resources"]], ["own-run"])

    def test_existing_orphan_observer_finishes_interrupted_close_without_parent_recovery(self):
        owner = self.process("att-owner")
        metadata = self.row("att-owner", process=owner)
        with mock.patch.object(CLOSE, "_agent_processes", return_value=([], False)):
            self.assertEqual(self.close()["state"], "termination-pending")
        observer = subprocess.Popen([sys.executable, str(HERE / "dispatch-orphan-watch.py"),
            "--jobs", str(self.jobs), "--agent-home", str(HERE.parent),
            "--attempt-id", self.aid("att-owner"), "--pid", str(owner.pid),
            "--pid-start", metadata["pid_start"], "--interval", "0.02"],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(observer)
        stdout, stderr = observer.communicate(timeout=15)
        self.assertEqual(observer.returncode, 0, stdout + stderr)
        self.assertEqual(owner.wait(timeout=2), -signal.SIGTERM)
        self.assertEqual(self.close()["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
