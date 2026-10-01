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
              report_rel="owner-report.md", verdict="PASS"):
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
        self.verdict = verdict
        blocker = "none" if verdict == "PASS" else "the gate command was refused, so I did not apply"
        text = f"artifact: {self.report}\nverdict: {verdict}\nblocker: {blocker}"
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
            blocked = getattr(self, "verdict", "PASS") == "BLOCKED"
            meta.update(execution_surface="registered-headless", transport="headless",
                        fallback_hop="same-harness-headless", failure_class="blocked" if blocked else "pass",
                        note="dead-worker-blocked" if blocked else "completed-supervisor",
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

    def release_in_process(self, decision="proceed"):
        """`release` run in this process so the continuation (a `start`) can be observed, not spawned."""
        import contextlib
        import io
        spec = importlib.util.spec_from_file_location("sup_for_release", R.ROOT / "utilities" / "workflow-supervisor.py")
        sup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sup)
        self.continuation_calls = []

        def continuation(route_path, jobs):
            import dispatch_replacement as DR
            self.continuation_calls.append({"attempt": self.owner})
            with mock.patch.object(DR, "advance", return_value={"state": "not-applicable"}) as advance:
                self.start(run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no launch")))
            advance.assert_called()
            return 0, {"state": "running"}

        out = io.StringIO()
        with self.env(), mock.patch.object(sup, "start_owner_continuation", continuation), contextlib.redirect_stdout(out):
            sup.main(["release", "--route", str(self.path), "--gate", self.PREVIEW, "--decision", decision,
                      "--actor", "user", "--jobs", str(self.jobs)])
        return json.loads(out.getvalue())

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

    def start(self, run=None):
        import work_start as W
        with self.env():
            return W.start_work(self.route, self.path, self.jobs, **({"run": run} if run else {}))

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

    def test_completing_the_owner_node_by_hand_does_not_bypass_the_gate(self):
        self.build("claude", close=False)
        node = next(n for n in self.route["nodes"] if n["id"] == "transaction")
        with self.env(), self.assertRaises(ValueError) as caught:
            R.complete_node(self.route, node, "transaction", self.report, jobs=self.jobs)
        self.assertIn("human-gate-not-raised", str(caught.exception))
        self.assertFalse((R.completion_dir(self.route["route_id"], jobs=self.jobs) / "transaction.json").exists())


PARENTS = (("claude-parent-runtime", "claude"), ("codex-native-queue", "codex"),
           ("codex-managed-gateway", "codex"), ("opencode-turn", "opencode"))


class OwnerGateReachesEveryParentTest(OwnerRefineBase):
    """The existing question reaches the person on every harness, with no dead end (D2, delivery).

    The runtime raises `preview-disposition` inside the parent's own `start`; the receipt that call
    returns is the delivery, so a parent kind with no push carrier still asks. The owner's own
    `gate --block` is untouched (SD-OPEN-33), see `workflow_supervisor.test.py`.
    """

    def fresh(self, name, *args, **kwargs):
        fixture = type(self)(name)
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture.build(*args, **kwargs)

    def assert_not_settled(self, fixture):
        settled = fixture.settle()                      # a BLOCKED owner has nothing to settle at all
        self.assertTrue(settled is None or settled.result != "completed", settled)
        self.assertNotEqual(fixture.workflow_state(), "COMPLETE")

    def no_launch(self, *a, **k):
        raise AssertionError("asking the question never launches or replaces an owner")

    def assert_asks(self, fixture, receipt, *, records):
        self.assertEqual((receipt["state"], receipt["reason"], receipt["required_action"], receipt["gate"]),
                         ("waiting-human-gate", "owner-parked-at-human-gate", "answer-human-gate", fixture.PREVIEW),
                         receipt)
        self.assertEqual(receipt["gate_artifact"], str(fixture.preview))
        for token in ("workflow-supervisor.py", "--gate " + fixture.PREVIEW, "--decision proceed", str(fixture.jobs)):
            self.assertIn(token, receipt["release_command"])
        self.assertEqual(Path(receipt["owner_report"]).name, "owner-report.md")
        self.assertNotIn("next_leg", receipt)
        self.assertNotIn("parent_next", receipt)
        resolution = fixture.resolution()
        self.assertEqual((resolution["status"], resolution["epoch"], resolution["artifact"]),
                         ("blocked", 1, str(fixture.preview)))
        self.assertEqual(len(fixture.gate_records()), records)
        self.assertNotEqual(fixture.workflow_state(), "COMPLETE")

    def test_a_pass_that_never_raised_asks_in_the_start_receipt_for_every_parent_kind(self):
        for kind, harness in PARENTS:
            with self.subTest(parent=kind):
                fixture = self.fresh("test_a_pass_that_never_raised_asks_in_the_start_receipt_for_every_parent_kind",
                                     harness, parent=kind)
                first = fixture.start(run=self.no_launch)
                self.assert_asks(fixture, first, records=1 if kind == "claude-parent-runtime" else 0)
                again = fixture.start(run=self.no_launch)        # a repeated start asks, never raises twice
                self.assert_asks(fixture, again, records=1 if kind == "claude-parent-runtime" else 0)
                self.assert_not_settled(fixture)

    def test_a_blocked_owner_that_obeyed_the_refusal_line_is_asked_about_never_replaced(self):
        # Owner row `done`, verdict BLOCKED, gate never raised: the same runtime raise, recognised as the
        # existing owner park. No replacement, relaunch or settlement happens before the person answers.
        import dispatch_replacement as DR
        for kind, harness in PARENTS:
            with self.subTest(parent=kind):
                fixture = self.fresh("test_a_blocked_owner_that_obeyed_the_refusal_line_is_asked_about_never_replaced",
                                     harness, parent=kind, verdict="BLOCKED")
                rows = fixture.jobs.read_text()
                with mock.patch.object(DR, "advance", side_effect=AssertionError("a blocked gate never continues")):
                    first = fixture.start(run=self.no_launch)
                    again = fixture.start(run=self.no_launch)
                for receipt in (first, again):
                    self.assert_asks(fixture, receipt, records=1 if kind == "claude-parent-runtime" else 0)
                self.assertEqual(fixture.jobs.read_text(), rows, "no row was added or changed")
                self.assert_not_settled(fixture)
                self.assertEqual(DR.owner_parked_gate(fixture.jobs, fixture.owner)["status"], "blocked")

    def test_the_receipt_says_what_a_proceed_will_really_do_for_each_owner_result(self):
        for verdict, expected, forbidden in (
                ("PASS", "settles the owner's finished work", "starts the continuation"),
                ("BLOCKED", "starts the continuation", "settles the owner's finished work")):
            with self.subTest(verdict=verdict):
                fixture = self.fresh("test_the_receipt_says_what_a_proceed_will_really_do_for_each_owner_result",
                                     "opencode", parent="opencode-turn", verdict=verdict)
                text = fixture.start(run=self.no_launch)["next_step"]
                self.assertIn(expected, text)
                self.assertNotIn(forbidden, text)

    def test_a_proceed_settles_a_passed_owner_for_every_parent_kind(self):
        for kind, harness in PARENTS:
            with self.subTest(parent=kind):
                fixture = self.fresh("test_a_proceed_settles_a_passed_owner_for_every_parent_kind", harness, parent=kind)
                fixture.start(run=self.no_launch)
                released = fixture.release("proceed")
                self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
                receipt = fixture.start(run=self.no_launch)
                self.assertEqual(receipt["state"], "completed", receipt)
                self.assertEqual(fixture.workflow_state(), "COMPLETE")
                self.assertEqual(P.read_cycle_record(fixture.root, fixture.cycle["cycle_id"])["state"], "sealed")

    def test_revise_and_stop_are_handled_for_every_parent_kind_and_owner_result(self):
        import dispatch_replacement as DR
        for kind, harness in PARENTS:
            for verdict in ("PASS", "BLOCKED"):
                for decision, state, reason in (("stop", "stopped", "human-gate-stop"),
                                                ("revise", "needs-attention", "human-gate-revise-owner-parked")):
                    with self.subTest(parent=kind, verdict=verdict, decision=decision):
                        fixture = self.fresh("test_revise_and_stop_are_handled_for_every_parent_kind_and_owner_result",
                                             harness, parent=kind, verdict=verdict)
                        fixture.start(run=self.no_launch)
                        self.assertEqual(fixture.release(decision).returncode, 0)
                        with mock.patch.object(DR, "advance", side_effect=AssertionError("never continues")):
                            receipt = fixture.start(run=self.no_launch)
                        self.assertEqual((receipt["state"], receipt["reason"]), (state, reason), receipt)
                        self.assertEqual(Path(receipt["owner_report"]).name, "owner-report.md")
                        self.assertNotIn("next_leg", receipt)
                        self.assert_not_settled(fixture)

    def test_a_proceed_after_a_blocked_owner_enters_the_existing_replacement_path(self):
        import dispatch_replacement as DR
        for kind, harness in PARENTS:
            with self.subTest(parent=kind):
                fixture = self.fresh("test_a_proceed_after_a_blocked_owner_enters_the_existing_replacement_path",
                                     harness, parent=kind, verdict="BLOCKED")
                fixture.start(run=self.no_launch)
                payload = fixture.release_in_process("proceed")
                self.assertEqual(payload["decision"], "proceed")
                self.assertEqual(payload["owner_continuation"]["state"], "running")
                self.assertEqual(DR.owner_parked_gate(fixture.jobs, fixture.owner)["status"], "proceed")
                fields, meta = DR._rows(fixture.jobs.read_text().splitlines())[fixture.owner]
                self.assertEqual(DR.death_kind(fields, meta, jobs=fixture.jobs), "parked")
                self.assertEqual(fixture.continuation_calls[0]["attempt"], fixture.owner)

    # Every `required_action` token `utilities/work_start.py` emitted at base 237a3e96 (the literals in it,
    # plus `delivery_required_action`'s own). A receipt may only use one of these: a new token is a new
    # thing every consumer would have to learn.
    BASE_REQUIRED_ACTIONS = frozenset({
        "advance-completed", "answer-human-gate", "ask-registered-question", "compose-again", "compose-route",
        "execute-inline", "inspect-closed-route", "inspect-preparation", "inspect-recovery",
        "prepare-frame-question", "report-capacity-wait", "report-gate-revision", "report-pending-work",
        "report-unfinished-work", "report-unresolved-frame", "resume-after-capacity", "resume-inline-finish",
        "revise-frame-question", "wait-for-first-frame-attempt", "wait-for-frame-results"})

    def base_required_actions(self):
        return self.BASE_REQUIRED_ACTIONS

    def test_a_raise_the_runtime_cannot_make_keeps_the_attention_receipt_and_asks_nothing(self):
        # The one case that still ends at `human-gate-not-raised`: the runtime tried to ask and the raise itself
        # was refused (a preview that stopped being provable at that moment, a ledger that cannot enter the
        # gate, a timeout). There is nothing the person could be shown, so nothing is settled, raised or
        # released. A missing review stage is not this case: it keeps `workflow-executor-exited`.
        import dispatch_contract as DC
        for kind, harness in PARENTS:
            with self.subTest(parent=kind):
                fixture = self.fresh("test_a_raise_the_runtime_cannot_make_keeps_the_attention_receipt_and_asks_nothing",
                                     harness, parent=kind)
                with mock.patch.object(DC, "raise_preview_gate_for_node",
                                       side_effect=ValueError("gate-carrier-refused: ledger cannot enter the gate")):
                    receipt = fixture.start(run=self.no_launch)
                self.assertEqual((receipt["state"], receipt["reason"], receipt["required_action"], receipt["gate"]),
                                 ("needs-attention", "human-gate-not-raised", "report-unfinished-work",
                                  fixture.PREVIEW), receipt)
                # no action token the base did not already emit (a new one is a new thing for consumers to learn)
                self.assertIn(receipt["required_action"], self.base_required_actions())
                self.assertEqual(Path(receipt["owner_report"]).name, "owner-report.md")
                self.assertIn("ledger cannot enter the gate", receipt["next_step"])
                self.assertNotIn("next_leg", receipt)
                self.assertEqual(fixture.resolution()["status"], "not-raised")
                self.assertEqual(fixture.gate_records(), [])
                self.assertNotEqual(fixture.workflow_state(), "COMPLETE")


if __name__ == "__main__":
    unittest.main()
