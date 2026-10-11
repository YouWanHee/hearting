#!/usr/bin/env python3
"""Parent deploy uses only temporary fixtures; never any unit, service or DB."""
import json
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import signal
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ship_parent_deploy as S


class ParentDeployTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "dispatch/jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.write_text("")
        self.route = {"route_id": "rt-test", "route_hash": "sha256:sealed", "capability": "autopilot-ship",
                      "entry_scope_contract_version": 1, "entry_execution_scope": "complete",
                      "nodes": [{"id": "deploy", "completion_gate": "ship-deploy", "depends_on": ["security-review", "release-review"]},
                                {"id": "security-review", "kind": "review-worker", "completion_gate": "security-pass"},
                                {"id": "release-review", "kind": "review-worker", "completion_gate": "release-pass"}]}
        self.meta = {"parent_sid": "parent", "parent_harness": "codex", "worker_type": "owner",
                     "dispatch_depth": "1", "registered_worker": "1", "note": "dead-worker-blocked"}
        self.record = self.root / "cycle/artifacts/release-config/deploy-record.json"
        self.record.parent.mkdir(parents=True)
        self.record.write_text(json.dumps({"rollback": "retained", "private_preimage": "existing"}))
        self.script = self.root / "deploy.py"
        self.counter = self.root / "effects"
        self.script.write_text("from pathlib import Path\np=Path('effects')\np.write_text((p.read_text() if p.exists() else '')+'x')\n")
        self.command = self.root / "command.json"
        self.command.write_text(json.dumps({"argv": [sys.executable, str(self.script)], "cwd": str(self.root), "timeout_seconds": 3}))
        self.proof = {"security-review": {"passed": True, "attempt_id": "security", "marker_digest": "s"},
                      "release-review": {"passed": True, "attempt_id": "release", "marker_digest": "r"}}
        self.markers = self.root / "markers"
        self.markers.mkdir()
        for node in self.proof:
            S.write(self.markers / (node + ".json"), {"review_independence": "independent"})
        self.cr = SimpleNamespace(_current_owner_attempt=lambda jobs, attempt: "owner",
                                  valid_entry_execution_scope_marker=lambda v, s: (v, s) == (None, None) or (v == 1 and s in ("complete", "report")),
                                  owner_terminal_prerequisites=lambda *args: {},
                                  _exclusive_lock=lambda path: S.locked(path),
                                  completion_dir=lambda *args, **kwargs: self.markers,
                                  _marker_identity_row=lambda r, n, nid, gate, **kw: dict(self.proof[nid]),
                                  terminal_gate_observation=lambda *args, **kwargs: {})
        for obj, name, value in [
            (S.DC, "_route_module", lambda: self.cr),
            (S, "exact_attempt_row", lambda jobs, aid: SimpleNamespace(status="done", raw=datetime.now(timezone.utc).isoformat()+"\t", metadata={"worker_type":"review"} if aid in {p["attempt_id"] for p in self.proof.values()} else self.meta)),
            (S, "_route", lambda *args: (self.root / "route.json", self.route)),
            (S.DC, "owner_operation_fence", lambda *args, **kwargs: None),
            (S.DC, "human_gate_entry_fence", lambda *args, **kwargs: None),
            (S, "record_path", lambda route: self.record),
        ]:
            mock = patch.object(obj, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        # Native identity resolution itself is real, and used for all three harnesses.
        env = patch.dict(os.environ, {"CODEX_THREAD_ID": "parent"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        host = patch.object(S, "pid_namespace_evidence", return_value={"lifecycle_selector_source": "host-like"})
        host.start()
        self.addCleanup(host.stop)

    def prepared(self):
        return Path(S.prepare(self.jobs, "owner", self.command)["request"])

    def test_success_preserves_record_and_hands_back_exact_route(self):
        request = self.prepared()
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(S.run(request, check=True)["execution_state"], "not-run")
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        result = S.run(request)
        self.assertEqual(result["state"], "success")
        self.assertEqual(self.counter.read_text(), "x")
        record = S.read(self.record)
        self.assertEqual(record["rollback"], "retained")
        self.assertEqual(record["parent_deploy"]["executor"]["session_id"], "parent")
        S.validate_completion(self.route, self.jobs, self.record)
        self.assertIn("correct", result["correction_command"])
        message = S.paths(self.jobs, "rt-test")[1].with_suffix(".message.md").read_text()
        self.assertIn("complete --inline --route", message)
        self.assertIn(str(self.root / "route.json"), message)
        # Simulate a lost reply: a repeated run must only republish its result.
        S.run(request)
        self.assertEqual(self.counter.read_text(), "x")

    def test_all_native_harnesses_apply_same_parent_boundary(self):
        for harness, variable in [("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"), ("opencode", "OPENCODE_SESSION_ID")]:
            with self.subTest(harness=harness), patch.dict(os.environ, {variable: "parent"}, clear=True):
                self.meta["parent_harness"] = harness
                self.assertEqual(S.parent(self.meta, self.jobs)["harness"], harness)
                with patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}):
                    with self.assertRaisesRegex(ValueError, "dispatch-child-forbidden"):
                        S.parent(self.meta, self.jobs)

    def test_wrong_parent_or_unknown_host_cannot_prepare_or_run(self):
        request = self.prepared()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "foreign"}, clear=True):
            with self.assertRaisesRegex(ValueError, "actual-parent-required"):
                S.run(request)
        with patch.object(S, "pid_namespace_evidence", return_value={"lifecycle_selector_source": "pid1-class"}):
            with self.assertRaisesRegex(ValueError, "host-unverified"):
                S.run(request)
        self.assertFalse(self.counter.exists())

    def test_registered_current_owner_can_prepare_but_cannot_execute(self):
        with patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "owner", "AGENT_DISPATCH_CHILD": "1"}), patch.object(S.authority, "review_owner_authority") as auth:
            request = self.prepared()
            auth.assert_called_once()
            self.assertIsNone(S.read(request)["parent_availability"])
            with self.assertRaisesRegex(ValueError, "dispatch-child-forbidden"):
                S.run(request)

    def test_pending_or_overridden_review_stops_before_spawn(self):
        request = self.prepared()
        self.cr.owner_terminal_prerequisites = lambda *args: {"release-review": "blocked"}
        with self.assertRaisesRegex(ValueError, "reviews-pending"):
            S.run(request)
        self.cr.owner_terminal_prerequisites = lambda *args: {}
        self.proof["release-review"]["review_independence"] = "owner-overridden"
        with self.assertRaisesRegex(ValueError, "independent-review-required"):
            S.run(request)
        self.assertFalse(S.paths(self.jobs, "rt-test")[1].exists())

    def review_row(self, attempt="security", node="security-review"):
        return datetime.now(timezone.utc).isoformat()+"\tdone\trepo\tworktree\treview\t" + ",".join(
            ["attempt_id="+attempt,"route_id="+self.route["route_id"],"route_node="+node,"worker_type=review"]) + "\n"

    def test_changed_request_rejects_existing_and_inflight_reviews_within_same_second(self):
        request = self.prepared()
        old = S.read(request)
        self.jobs.write_text(self.review_row()+self.review_row("release", "release-review"))
        # Same normalized request preserves its binding, even after review began.
        self.prepared()
        self.assertEqual(S.read(request)["request_digest"], old["request_digest"])
        self.assertEqual(S.read(request)["prepared_at"], old["prepared_at"])
        self.script.write_text("from pathlib import Path\nPath('effects').write_text('unreviewed')\n")
        self.prepared()
        changed = S.read(request)
        self.assertNotEqual(changed["request_digest"], old["request_digest"])
        self.assertIn("security", changed["prior_review_attempts"])
        with self.assertRaisesRegex(ValueError, "review-precedes-request"):
            S.run(request)
        self.assertFalse(self.counter.exists())
        self.proof["security-review"]["attempt_id"] = "new-security"
        with self.assertRaisesRegex(ValueError, "review-precedes-request:release-review"):
            S.run(request)
        self.proof["release-review"]["attempt_id"] = "new-release"
        with patch.object(S, "exact_attempt_row", side_effect=lambda jobs, aid: SimpleNamespace(
                status="done", metadata={"worker_type":"review"} if aid in ("new-security","new-release") else self.meta,
                raw=datetime.now(timezone.utc).isoformat()+"\t")):
            self.assertEqual(S.run(request)["state"], "success")

    def test_raw_registered_review_override_and_parallel_leg_are_consumed(self):
        request = self.prepared()
        S.write(self.markers / "release-review.json", {"review_independence":"owner-overridden"})
        with self.assertRaisesRegex(ValueError, "independent-review-required"):
            S.run(request)
        S.write(self.markers / "release-review.json", {"review_independence":"independent"})
        self.route["nodes"].append({"id":"security-alternative","kind":"review-worker","completion_gate":"security-pass"})
        self.route["parallel_groups"] = [{"id":"security","members":["security-review","security-alternative"]}]
        self.proof["security-alternative"] = {"passed":True,"attempt_id":"alternative","marker_digest":"a"}
        S.write(self.markers / "security-alternative.json", {"review_independence":"owner-overridden"})
        with self.assertRaisesRegex(ValueError, "independent-review-required:security-alternative"):
            S.run(request)

    def test_older_registered_review_start_is_refused(self):
        request = self.prepared()
        with patch.object(S, "exact_attempt_row", side_effect=lambda jobs, aid: SimpleNamespace(
                status="done", metadata=self.meta if aid == "owner" else {"worker_type":"review"},
                raw="2000-01-01T00:00:00Z\t")):
            with self.assertRaisesRegex(ValueError, "review-precedes-request"):
                S.run(request)

    def test_command_source_drift_and_route_report_scope_refuse(self):
        request = self.prepared()
        self.script.write_text("raise SystemExit(0)")
        with self.assertRaisesRegex(ValueError, "command-input-drift"):
            S.run(request)
        self.route["entry_execution_scope"] = "report"
        with self.assertRaisesRegex(ValueError, "approved-ship-scope-required"):
            S.run(request)

    def test_stop_and_closed_route_do_not_execute(self):
        request = self.prepared()
        with patch.object(S.DC, "owner_operation_fence", side_effect=S.DC.DispatchContractError("cancelled-by-parent")):
            with self.assertRaises(S.DC.DispatchContractError):
                S.run(request)
        with patch.object(S, "_route", side_effect=S.DC.DispatchContractError("replacement-route-closed")):
            with self.assertRaises(S.DC.DispatchContractError):
                S.run(request)
        self.assertFalse(self.counter.exists())

    def test_owner_replacement_retains_exact_request_and_reviews(self):
        request = self.prepared()
        self.cr._current_owner_attempt = lambda jobs, attempt: "replacement-owner"
        S.run(request)
        result = S.read(S.paths(self.jobs, "rt-test")[1])
        self.assertEqual(result["owner_attempt_id"], "replacement-owner")
        self.assertEqual(result["reviews"], self.proof)
        self.assertEqual(S.read(request)["owner_attempt_id"], "owner")

    def test_failed_and_timeout_outcomes_never_repeat(self):
        self.script.write_text("from pathlib import Path\nPath('effects').write_text('once')\nraise SystemExit(7)\n")
        request = self.prepared()
        self.assertEqual(S.run(request)["state"], "failed")
        with self.assertRaisesRegex(ValueError, "successful-result-required"):
            S.validate_completion(self.route, self.jobs, self.record)
        S.run(request)
        self.assertEqual(self.counter.read_text(), "once")
        with self.assertRaisesRegex(ValueError, "execution-already-recorded"):
            self.prepared()

    def test_timeout_cleans_owned_process_group(self):
        self.script.write_text("import time\ntime.sleep(60)\n")
        self.command.write_text(json.dumps({"argv": [sys.executable, str(self.script)], "cwd": str(self.root), "timeout_seconds": 0.15}))
        request = self.prepared()
        self.assertEqual(S.run(request)["state"], "failed")
        result = S.read(S.paths(self.jobs, "rt-test")[1])
        self.assertEqual(result["detail"], "TimeoutExpired")
        self.assertFalse(S.DC.process_identity_is_live(result["pid"], result["pid_start"]))
        with patch.object(S.subprocess, "Popen", side_effect=AssertionError("must not retry")):
            S.run(request)

    def test_parent_interruption_retains_result_and_cleans_its_command(self):
        self.script.write_text("import time\ntime.sleep(60)\n")
        request = self.prepared()
        timer = threading.Timer(0.15, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        try:
            self.assertEqual(S.run(request)["state"], "interrupted")
        finally:
            timer.cancel()
            timer.join()
        result = S.read(S.paths(self.jobs, "rt-test")[1])
        self.assertFalse(S.DC.process_identity_is_live(result["pid"], result["pid_start"]))
        with patch.object(S.subprocess, "Popen", side_effect=AssertionError("must not retry")):
            S.run(request)

    def test_running_intent_and_result_record_loss_do_not_repeat(self):
        request = self.prepared()
        value, result_path = S.load_request(request)
        S.write(result_path, {"state": "running", "request_digest": value["request_digest"]})
        self.assertEqual(S.run(request)["state"], "running-or-interrupted")
        self.assertFalse(self.counter.exists())

    def test_completion_refuses_changed_result_or_review_and_normal_deploy_stays_unchanged(self):
        S.validate_completion(self.route, self.jobs, self.root / "missing-normal-record")
        request = self.prepared()
        S.run(request)
        self.proof["release-review"]["marker_digest"] = "changed"
        with self.assertRaisesRegex(ValueError, "review-evidence-drift"):
            S.validate_completion(self.route, self.jobs, self.record)
        self.proof["release-review"]["marker_digest"] = "r"
        record = S.read(self.record)
        record["parent_deploy"]["exit_code"] = 9
        S.write(self.record, record)
        with self.assertRaisesRegex(ValueError, "successful-result-required"):
            S.validate_completion(self.route, self.jobs, self.record)

    def test_exact_command_schema_and_private_log(self):
        for value in [{"argv": ["python3"], "cwd": str(self.root)},
                      {"argv": [sys.executable], "cwd": str(self.root), "timeout_seconds": float("nan")},
                      {"argv": [sys.executable], "cwd": str(self.root), "env": {"UNSAFE": "1"}}]:
            with self.assertRaises(ValueError):
                S.command(value)
        result = S.run(self.prepared())
        log = Path(S.read(result["result"])["output_log"])
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_printed_complete_command_uses_real_route_writer_and_keeps_owner_open(self):
        # Reuse the existing compile fixture, while exercising the production
        # CLI/parser/marker writer. No registered owner is closed by an inline
        # deploy completion: that owner still owns post-deploy verification.
        spec = importlib.util.spec_from_file_location("ship_route_fixture", Path(__file__).with_name("capability_route.test.py"))
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        t = fixture.TestRoute()
        cr = fixture.R
        self.route = cr.compile_route(**t.args(capability="autopilot-ship", capability_mode="default",
            artifact_root=self.root, requested_intensity="standard", predicates=[], signals=[],
            transport="headless", inline_reason=None, dispatch_evidence=t.dispatch(t.nested()),
            registered_headless_evidence=t.registered_headless()))
        route_file = self.root / "route.json"
        route_file.write_text(json.dumps(self.route))
        self.jobs.write_text("time\topen\trepo\tworktree\towner\tattempt_schema_version=2,attempt_id=owner,worker_type=owner\n")
        original = self.jobs.read_bytes()
        result = S.run(self.prepared())
        message = Path(result["result"]).with_suffix(".message.md").read_text()
        command_line = next(line.split(": ", 1)[1] for line in message.splitlines() if line.startswith("If success,"))
        import shlex
        args = shlex.split(command_line)[2:]
        with patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs), "AGENT_DISPATCH_ATTEMPT_ID": "owner",
                                     "AGENT_ARTIFACT_CHECKPOINT": "off", "HEARTING_GATES": "on"}), patch.object(sys, "argv", [str(cr.__file__), *args]), contextlib.redirect_stdout(io.StringIO()) as output:
            cr.main()
        marker = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(marker["node_id"], "deploy")
        self.assertFalse(marker["registered_worker"])
        self.assertEqual(self.jobs.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
