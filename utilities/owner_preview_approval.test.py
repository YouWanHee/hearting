#!/usr/bin/env python3
"""A refine owner's own `transaction` honours `preview-disposition` (D2).

The refine `review,transaction` route is composed, admitted and begun through the production
paths; only the owner's native result and its registry rows are fixtures. `gate` and `release`
are the real `workflow-supervisor.py`, so the gate journal, epochs and who may release are real.
Kept in its own file: every case builds a real route and cycle, and the cost belongs here rather
than in `artifact_producer.test.py`.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SPEC = importlib.util.spec_from_file_location(
    "producer_fixture_for_owner_preview", Path(__file__).with_name("artifact_producer.test.py"))
PF = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(PF)
R, P, L, D, nested = PF.R, PF.P, PF.L, PF.D, PF.nested


NATIVE_OWNER_RESULT = {
    "claude": lambda text: [{"type": "result", "subtype": "success", "is_error": False, "result": text}],
    "codex": lambda text: [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                           {"type": "turn.completed"}],
    "opencode": lambda text: [{"type": "text", "sessionID": "ses_test", "part": {"type": "text", "text": text}},
                              {"type": "step_finish", "sessionID": "ses_test",
                               "part": {"type": "step-finish", "reason": "stop"}}],
}


class OwnerRefineBase(PF.ProducerTestBase):
    """A real refine `review,transaction` route whose depth-1 owner executes the transaction.

    Compose, admission, producer begin, review completion and the gate journal are the
    production paths; only the owner's native result and its registry rows are fixtures.
    `gate`/`release` run the real `workflow-supervisor.py` (a person's release is the
    default actor, a registered worker's is refused).
    """

    PREVIEW = "preview-disposition"

    def setUp(self):
        super().setUp()
        # A worker running these tests carries its own route/attempt binding; the fixture owner has none.
        scrub = mock.patch.dict(os.environ)
        scrub.start()
        self.addCleanup(scrub.stop)
        for key in [k for k in os.environ if k.startswith((
                "AGENT_ROUTE", "AGENT_OWNER", "AGENT_DISPATCH_ATTEMPT", "AGENT_DISPATCH_REGISTERED",
                "AGENT_ARTIFACT_CYCLE", "AGENT_ARTIFACT_OUTPUT", "AGENT_PARENT", "AGENT_WORKFLOW"))]:
            os.environ.pop(key)

    def row(self, status, slug, meta):
        return (f"2026-10-01T00:00:00Z\t{status}\t{R.ROOT}\t{R.ROOT}\t{slug}\t"
                + ",".join(f"{k}={v}" for k, v in meta.items()) + "\n")

    def build(self, harness="claude", *, review=True, close=True, parent="claude-parent-runtime",
              report_rel="owner-report.md"):
        os.environ["AGENT_HOME"] = str(R.ROOT)
        self.activate()
        self.harness = harness
        self.route = R.compose_route(
            capability="autopilot-refine", capability_mode=None, shape="staged", graph="review,transaction",
            slug="owner-preview", cwd=R.ROOT, artifact_root=self.root, intensity="standard",
            dispatch_evidence={"tuples": [nested(harness, "codex")]}, unassigned=True,
            work_request={"text": "Refine the README", "owner_harness": harness})
        self.path = Path(L.admit_runtime_route(self.root, self.route).route_file)
        self.owner, self.child, self.log = "att-preview-owner", "att-preview-review", self.jobs.parent / "owner.jsonl"
        self.owner_meta = dict(
            attempt_id=self.owner, worker_type="owner", unit="_kernel/owner", dispatch_depth=1,
            registered_worker=1, harness=harness, owner_route_file=self.path,
            owner_route_id=self.route["route_id"], owner_route_hash=self.route["route_hash"],
            parent_sid="fixture-parent", parent_completion_delivery=parent,
            attempt_schema_version=2, log_file=self.log, workflow_completion="runtime-v1")
        self.jobs.write_text(self.row("open", "owner", self.owner_meta))
        self.cycle = P.begin(self.root, route_file=self.path, capability="autopilot-refine",
                             intensity="standard", jobs=self.jobs, owner_attempt_id=self.owner)
        self.preview = self.write_output(self.cycle, rel="reviews/refine/preview.md", data=b"preview of the edit\n")
        self.report = self.write_output(self.cycle, rel=report_rel, data=b"owner report\n")
        if review:
            node = self.route["nodes"][0]
            child_meta = dict(
                attempt_schema_version=2, attempt_id=self.child, parent_attempt_id=self.owner, dispatch_depth=2,
                transport="headless", execution_surface="registered-headless", registered_worker=1,
                fallback_hop="same-harness-headless", harness="codex", route_id=self.route["route_id"],
                route_hash=self.route["route_hash"], route_node=node["id"], failure_class="pass",
                launch_outcome="reaped-before-publish")
            self.jobs.write_text(self.row("open", "owner", self.owner_meta) + self.row("open", "review", child_meta))
            R.complete_node(self.route, node, node["id"], self.preview, jobs=self.jobs, attempt_id=self.child)
        text = f"artifact: {self.report}\nverdict: PASS\nblocker: none"
        self.log.write_text("\n".join(json.dumps(x) for x in NATIVE_OWNER_RESULT[harness](text)) + "\n")
        if close:
            self.close_owner()
        return self

    def close_owner(self):
        lines = self.jobs.read_text().splitlines()
        for i, line in enumerate(lines):
            fields = line.split("\t")
            meta = D.parse_registry_metadata(fields[5])
            if meta.get("attempt_id") != self.owner:
                continue
            meta.update(execution_surface="registered-headless", transport="headless",
                        fallback_hop="same-harness-headless", failure_class="pass", note="completed-supervisor",
                        launch_outcome="reaped-before-publish")
            fields[1] = "done"
            fields[5] = ",".join(f"{k}={v}" for k, v in meta.items())
            lines[i] = "\t".join(fields)
        self.jobs.write_text("\n".join(lines) + "\n")

    def supervisor(self, *argv, worker=False):
        env = {**os.environ, "AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_DISPATCH_JOBS": str(self.jobs)}
        env.pop("AGENT_DISPATCH_REGISTERED_WORKER", None)
        if worker:
            env["AGENT_DISPATCH_REGISTERED_WORKER"] = "1"
        return subprocess.run([sys.executable, str(R.ROOT / "utilities" / "workflow-supervisor.py"), *argv],
                              text=True, capture_output=True, env=env)

    def raise_gate(self, artifact=None):
        done = self.supervisor("gate", "--route", str(self.path), "--gate", self.PREVIEW, "--block",
                               "--jobs", str(self.jobs), "--artifact", str(artifact or self.preview))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def release(self, decision="proceed", worker=False):
        return self.supervisor("release", "--route", str(self.path), "--gate", self.PREVIEW,
                               "--decision", decision, "--jobs", str(self.jobs), "--actor", "user", worker=worker)

    def resolution(self):
        import workflow_state as WS
        return WS.human_gate_resolution(
            WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs).journal(), self.PREVIEW)

    def workflow_state(self):
        import workflow_state as WS
        return WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs).state()["workflow_state"]

    def env(self):
        return mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_DISPATCH_JOBS": str(self.jobs)})

    def meta(self):
        from dispatch_completion_join import exact_attempt_row
        return exact_attempt_row(self.jobs, self.owner).metadata

    def settle(self):
        import dispatch_terminal_commit as T
        with self.env():
            return T.settle_owner_completion(self.jobs, "done", self.meta())

    def start(self):
        import work_start as W
        with self.env():
            return W.start_work(self.route, self.path, self.jobs)

    def gate_records(self):
        return sorted((self.jobs.parent / "pending-delivery").rglob("*.json")) if (self.jobs.parent / "pending-delivery").exists() else []


class OwnerPreviewApprovalTest(OwnerRefineBase):
    """The owner-executed `transaction` honours `preview-disposition`, whatever the harness says.

    A PASS terminal result is a proposal: without a person's release of the current preview it
    must neither settle nor reach `next_leg`. When the owner never raised the gate the main
    session's `start` turns that into the existing question; when the preview cannot be proved
    the receipt says `human-gate-not-raised` instead.
    """

    def test_a_pass_with_no_raise_does_not_settle_and_start_asks_the_existing_question_for_any_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                fixture = OwnerPreviewApprovalTest(
                    "test_a_pass_with_no_raise_does_not_settle_and_start_asks_the_existing_question_for_any_harness")
                fixture.setUp()
                try:
                    fixture.build(harness)
                    result = fixture.settle()
                    self.assertNotEqual(result.result, "completed", result)
                    self.assertNotEqual(fixture.workflow_state(), "COMPLETE")
                    self.assertEqual(fixture.resolution()["status"], "not-raised")
                    gates = R.terminal_gate_observation(fixture.route, jobs=fixture.jobs, exact_terminal=True)
                    self.assertFalse(gates["transaction"]["passed"], gates)
                    self.assertIn("human-gate-not-raised", gates["transaction"]["reason"])
                    self.assertEqual(P.read_cycle_record(fixture.root, fixture.cycle["cycle_id"])["state"], "open")
                    receipt = fixture.start()
                    self.assertEqual((receipt["state"], receipt["reason"], receipt["gate"]),
                                     ("waiting-human-gate", "owner-parked-at-human-gate", fixture.PREVIEW), receipt)
                    self.assertEqual(receipt["gate_artifact"], str(fixture.preview))
                    self.assertIn("workflow-supervisor.py", receipt["release_command"])
                    self.assertIn("--decision proceed", receipt["release_command"])
                    self.assertEqual(Path(receipt["owner_report"]).name, "owner-report.md")
                    self.assertTrue(Path(receipt["owner_report"]).is_file())
                    self.assertIn("owner_report", receipt["next_step"])
                    self.assertNotIn("next_leg", receipt)
                    self.assertEqual(fixture.resolution()["status"], "blocked")
                    self.assertEqual(fixture.resolution()["artifact"], str(fixture.preview))
                    self.assertEqual(fixture.resolution()["artifact_sha256"],
                                     hashlib.sha256(fixture.preview.read_bytes()).hexdigest())
                    self.assertNotEqual(fixture.workflow_state(), "COMPLETE")
                finally:
                    fixture.doCleanups()

    def test_the_question_is_raised_once_only_a_person_can_answer_it_and_proceed_settles(self):
        self.build("claude")
        first = self.start()
        records = self.gate_records()
        self.assertEqual(first["state"], "waiting-human-gate", first)
        self.assertEqual(len(records), 1)
        before = records[0].read_bytes()
        second = self.start()                                   # a repeated start does not raise it twice
        self.assertEqual((second["state"], second["gate"]), ("waiting-human-gate", self.PREVIEW))
        self.assertEqual(self.resolution()["epoch"], 1)
        self.assertEqual([p.read_bytes() for p in self.gate_records()], [before])
        refused = self.release("proceed", worker=True)          # the owner cannot answer for the person
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(self.resolution()["status"], "blocked")
        self.assertNotEqual(self.settle().result, "completed")
        released = self.release("proceed")
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        receipt = self.start()
        self.assertEqual(receipt["state"], "completed", receipt)
        self.assertEqual(self.workflow_state(), "COMPLETE")
        self.assertEqual(P.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_stop_and_revise_never_settle(self):
        for decision, state, reason in (("stop", "stopped", "human-gate-stop"),
                                        ("revise", "needs-attention", "human-gate-revise-owner-parked")):
            with self.subTest(decision=decision):
                fixture = OwnerPreviewApprovalTest("test_stop_and_revise_never_settle")
                fixture.setUp()
                try:
                    fixture.build("claude")
                    fixture.start()
                    self.assertEqual(fixture.release(decision).returncode, 0)
                    receipt = fixture.start()
                    self.assertEqual((receipt["state"], receipt["reason"]), (state, reason), receipt)
                    self.assertNotEqual(fixture.settle().result, "completed")
                    self.assertNotEqual(fixture.workflow_state(), "COMPLETE")
                finally:
                    fixture.doCleanups()

    def test_a_preview_changed_after_it_was_shown_does_not_authorize_the_apply(self):
        self.build("claude")
        self.assertEqual(self.start()["state"], "waiting-human-gate")
        self.preview.write_text("a different preview")        # not the bytes the person was asked about
        with mock.patch.dict(os.environ, {"HEARTING_GATES": "on"}):   # the opt-in same-work identity check
            self.assertEqual(self.release("proceed").returncode, 0)
            self.assertNotEqual(self.settle().result, "completed")
            receipt = self.start()
        self.assertNotEqual(receipt["state"], "completed", receipt)
        self.assertNotIn("next_leg", receipt)
        self.assertNotEqual(self.workflow_state(), "COMPLETE")

    def test_an_owner_that_raised_and_was_released_settles_as_before(self):
        self.build("claude", close=False)
        self.raise_gate()
        self.assertEqual(self.release("proceed").returncode, 0)
        self.close_owner()
        self.assertEqual(self.settle().result, "completed")
        self.assertEqual(self.workflow_state(), "COMPLETE")

    def test_an_owner_that_raised_and_exited_without_a_release_is_waiting(self):
        self.build("claude", close=False)
        self.raise_gate()
        self.close_owner()
        self.assertNotEqual(self.settle().result, "completed")
        receipt = self.start()
        self.assertEqual((receipt["state"], receipt["gate"]), ("waiting-human-gate", self.PREVIEW), receipt)
        self.assertEqual(self.resolution()["epoch"], 1)

    def test_a_pass_with_no_review_marker_keeps_the_existing_unfinished_stage_receipt_and_raises_nothing(self):
        self.build("claude", review=False)
        receipt = self.start()
        self.assertEqual((receipt["state"], receipt["reason"]), ("needs-attention", "workflow-executor-exited"), receipt)
        self.assertIn("review", receipt["missing_terminal_gates"])
        self.assertNotIn("next_leg", receipt)
        self.assertEqual(self.resolution()["status"], "not-raised")
        self.assertNotEqual(self.settle().result, "completed")
        self.assertNotEqual(self.workflow_state(), "COMPLETE")

    def test_a_parent_whose_carrier_cannot_hold_the_gate_gets_the_same_attention_receipt(self):
        # The runtime can raise the question only where a carrier can hold it for an owner that has
        # already exited; elsewhere the receipt says so instead of settling or inventing an approval.
        for parent in ("opencode-turn", "codex-native-queue", "codex-managed-gateway"):
            with self.subTest(parent=parent):
                fixture = OwnerPreviewApprovalTest(
                    "test_a_parent_whose_carrier_cannot_hold_the_gate_gets_the_same_attention_receipt")
                fixture.setUp()
                try:
                    fixture.build("claude", parent=parent)
                    receipt = fixture.start()
                    self.assertEqual((receipt["state"], receipt["reason"], receipt["gate"]),
                                     ("needs-attention", "human-gate-not-raised", fixture.PREVIEW), receipt)
                    self.assertEqual(Path(receipt["owner_report"]).name, "owner-report.md")
                    self.assertNotIn("next_leg", receipt)
                    self.assertEqual(fixture.resolution()["status"], "not-raised")
                    self.assertNotEqual(fixture.workflow_state(), "COMPLETE")
                finally:
                    fixture.doCleanups()

    def test_completing_the_owner_node_by_hand_does_not_bypass_the_gate(self):
        self.build("claude", close=False)
        node = next(n for n in self.route["nodes"] if n["id"] == "transaction")
        with self.env(), self.assertRaises(ValueError) as caught:
            R.complete_node(self.route, node, "transaction", self.report, jobs=self.jobs)
        self.assertIn("human-gate-not-raised", str(caught.exception))
        self.assertFalse((R.completion_dir(self.route["route_id"], jobs=self.jobs) / "transaction.json").exists())


if __name__ == "__main__":
    unittest.main()
