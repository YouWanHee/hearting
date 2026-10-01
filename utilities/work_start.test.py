#!/usr/bin/env python3
"""Public start orchestration; outcome authority remains the production join.

Admission and observation are controlled here to exercise crash/replay and
refusal boundaries. Actual claim/process/closure tests live in their owners.
"""
import json
import contextlib
import io
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

import work_start as W
from dispatch_completion_join import CurrentDeliveryState


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class WorkStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        self.route = {"route_id": "rt-probe", "route_hash": "sha256:probe", "slug": "task",
            "effective_intensity": "standard", "work_request": {"text": "Run the two commands", "owner_harness": "codex"},
            "nodes": [{"id": n, "worker_type": "frame", "dispatch_depth": 1} for n in ("frame", "frame-alternative")]
                     + [{"id": "test", "depends_on": ["frame", "frame-alternative"]}],
            "dispatch_evidence": {"tuples": [{"child_harness": h, "status": "supported"} for h in ("codex", "opencode")]}}
        self.path = Path(self.tmp.name) / "route.json"
        self.path.write_text(json.dumps(self.route))
        self.calls = []
        self.ready = False
        self.released = False
        self.statuses = {}
        self.parent = mock.patch.object(W, "default_parent_session_id", return_value="parent")
        self.parent.start(); self.addCleanup(self.parent.stop)
        self.environment = mock.patch.dict(os.environ, {"AGENT_CODEX_MANAGED_GATEWAY": "0"})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.join = mock.patch.object(W, "join_selected_attempts", side_effect=self.observe)
        self.join.start(); self.addCleanup(self.join.stop)
        self.gate = mock.patch.object(W, "owner_frame_launch_gate", side_effect=self.owner_gate)
        self.gate.start(); self.addCleanup(self.gate.stop)
        self.pair = mock.patch.object(W, "completion_marker_gate")
        self.pair.start(); self.addCleanup(self.pair.stop)
        self.current = mock.patch.object(W, "current_delivery_state", side_effect=self.delivery)
        self.current.start(); self.addCleanup(self.current.stop)

    def observe(self, **kw):
        return {"state": "ready" if self.ready else "timeout", "children": []}

    def delivery(self, jobs, aid, **kw):
        fields = dict(marker={"artifact": "/exact/report.md"}, marker_digest="sha256:marker",
            row_revision="1", row_digest="sha256:row", status="done", verdict="PASS", quiescent=True,
            owned_children=0, advanced=False, completion_proven=True)
        fields.update(self.statuses.get(aid, {}))
        return CurrentDeliveryState(**fields)

    def owner_gate(self, *args, **kw):
        if not self.released:
            raise W.DispatchContractError("human-gate-unreleased", "frame-review")

    def admit(self, command, **kwargs):
        self.calls.append(command)
        def value(flag, fallback=""):
            return command[command.index(flag) + 1] if flag in command else fallback
        aid = value("--attempt-id")
        node = value("--route-node", "owner")
        meta = {"attempt_id": aid, "parent_sid": "parent", "launch_started": "1",
            "worker_type": "owner" if node == "owner" else "frame", "route_node": node,
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "owner_route_hash": self.route["route_hash"] if node == "owner" else "",
            "parent_completion_delivery": "codex-managed-gateway"}
        with self.jobs.open("a") as stream:
            stream.write("now\topen\t12\tparent\ttask\t" + ",".join(k+"="+v for k,v in meta.items()) + "\n")
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    def start(self, **kwargs):
        return W.start_work(self.route, self.path, self.jobs, run=self.admit, **kwargs)

    def refusal_receipt(self, *, worker_class="dispatch", retry_after_seconds=1,
                         frees_at=None, retryable="1", reason="model-worker-governor-denied",
                         child_spawned="0"):
        if frees_at is None:
            frees_at = int(time.time()) + (retry_after_seconds or 60)
        lines = [
            "check=failed", f"reason={reason}",
            f"detail=rolling model-worker start budget reached: class={worker_class} "
            f"used=20 limit=20 retry_after_seconds={retry_after_seconds}",
            f"child_spawned={child_spawned}",
        ]
        if retryable is not None:
            lines += [
                f"retryable={retryable}", "refusal=start-budget", f"worker_class={worker_class}",
                f"retry_after_seconds={retry_after_seconds}", f"frees_at={frees_at}",
            ]
        return "\n".join(lines) + "\n"

    def make_run(self, *, refuse_node, refuse_times=1, receipt=None):
        """A `run` fixture that refuses one node's launch a bounded number of
        times (typed refusal receipt, no registered row), then falls through
        to the normal admitting fixture."""
        counts = {"n": 0}

        def run(command, **kwargs):
            node = command[command.index("--route-node") + 1] if "--route-node" in command else "owner"
            if node == refuse_node and counts["n"] < refuse_times:
                counts["n"] += 1
                return subprocess.CompletedProcess(command, 75, receipt or self.refusal_receipt(), "")
            return self.admit(command, **kwargs)

        run.counts = counts
        return run

    def test_repeated_start_and_restart_keep_both_frames_then_one_owner(self):
        for _ in range(3):
            result = self.start()
            self.assertEqual(result["state"], "preparing", result)
            self.assertEqual(result["parent_next"], "end-turn")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all("--adapter" not in c for c in self.calls))
        self.ready = True
        result = self.start()
        self.assertEqual(result["state"], "needs-interview", result)
        self.assertEqual(len(self.calls), 2)
        self.released = True
        result = self.start()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result["result"]["recovery_command"], "")
        self.assertEqual(result["result"]["marker"]["artifact"], "/exact/report.md")
        self.start(); self.assertEqual(len(self.calls), 3)

    def test_partial_admission_retains_the_started_child_and_exact_resume(self):
        def partial(command, **kwargs):
            if "frame-alternative" in command:
                return subprocess.CompletedProcess(command, 75, "started=0", "capacity temporarily full")
            return self.admit(command, **kwargs)
        result = W.start_work(self.route, self.path, self.jobs, run=partial)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(len(result["frame_attempts"]), 1)
        self.assertEqual(result["parent_next"], "end-turn")
        self.assertIn("capacity temporarily full", result["launches"][1]["diagnostic"])
        self.assertEqual(self.start()["state"], "preparing")
        self.assertEqual(len(self.calls), 2)

    def test_typed_budget_refusal_is_waiting_capacity_not_failure(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=99,
                             receipt=self.refusal_receipt(retry_after_seconds=1))
        result = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(result["refused_attempt_id"], W.attempt_id(self.route, "frame-alternative"))
        self.assertEqual(result["refused_node"], "frame-alternative")
        self.assertFalse(result["spawned"])
        self.assertIn("retry_at", result)
        self.assertEqual(len(result["frame_attempts"]), 1)
        self.assertEqual(result["parent_next"], "bounded-wait")
        self.assertTrue(result["parent_next_command"].endswith("--wait"))
        self.assertNotIn("failed attempt", result["next_step"])

    def test_resume_after_capacity_relaunches_refused_attempt_exactly_once(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=1,
                             receipt=self.refusal_receipt(retry_after_seconds=1))
        first = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(first["state"], "waiting-capacity", first)
        self.assertEqual(len(self.calls), 1)  # only "frame" admitted so far

        second = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(second["state"], "preparing", second)
        self.assertEqual(len(second["frame_attempts"]), 2)
        self.assertEqual(len(self.calls), 2)  # frame-alternative admitted exactly once

        W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(len(self.calls), 2)  # both rows exist; no further launch call

    def test_resume_wait_sleeps_until_retry_then_launches_once(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=1,
                             receipt=self.refusal_receipt(retry_after_seconds=7))
        sleeps = []
        result = W.start_work(self.route, self.path, self.jobs, run=run, wait=True,
                               sleep=sleeps.append, clock=lambda: 1_800_000_000.0)
        self.assertEqual(sleeps, [7])
        self.assertNotEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(len(result["frame_attempts"]), 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["capacity_waited_seconds"], 7)
        self.assertEqual(W.join_selected_attempts.call_args.kwargs["timeout"], 600 - 7)

    def test_resume_wait_refused_again_hands_back_without_another_wait(self):
        run = self.make_run(refuse_node="frame-alternative", refuse_times=99,
                             receipt=self.refusal_receipt(retry_after_seconds=3))
        sleeps = []
        result = W.start_work(self.route, self.path, self.jobs, run=run, wait=True,
                               sleep=sleeps.append, clock=lambda: 1_800_000_000.0)
        self.assertEqual(sleeps, [3])  # exactly one wait, never a retry loop
        self.assertEqual(result["state"], "waiting-capacity", result)
        self.assertNotIn("parent_next", result)
        self.assertNotIn("parent_next_command", result)
        self.assertEqual(result["required_action"], "report-capacity-wait")
        self.assertEqual(result["capacity_waited_seconds"], 3)

    def test_owner_budget_refusal_is_waiting_capacity(self):
        run = self.make_run(refuse_node="owner", refuse_times=99,
                             receipt=self.refusal_receipt(retry_after_seconds=5))
        self.ready = True
        self.released = True
        result = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(result["refused_attempt_id"], W.attempt_id(self.route, "owner"))
        self.assertEqual(result["refused_node"], "owner")
        self.assertFalse(result["spawned"])

    def test_spawned_then_exit_75_is_never_relaunched(self):
        def crashed_after_claim(command, **kwargs):
            registered = self.admit(command, **kwargs)
            receipt = registered.stdout + self.refusal_receipt(retry_after_seconds=5, child_spawned="1")
            return subprocess.CompletedProcess(command, 75, receipt, "worker crashed after claim")
        result = W.start_work(self.route, self.path, self.jobs, run=crashed_after_claim)
        self.assertNotEqual(result["state"], "waiting-capacity", result)
        calls_after_first = len(self.calls)
        W.start_work(self.route, self.path, self.jobs, run=crashed_after_claim)
        self.assertEqual(len(self.calls), calls_after_first)  # rows exist; no relaunch

    def test_receipt_refusal_with_existing_row_trusts_registry(self):
        def raced(command, **kwargs):
            node = command[command.index("--route-node") + 1] if "--route-node" in command else "owner"
            if node == "frame-alternative":
                self.admit(command, **kwargs)  # a concurrent resume already admitted this exact aid
                return subprocess.CompletedProcess(command, 75, self.refusal_receipt(), "")
            return self.admit(command, **kwargs)
        result = W.start_work(self.route, self.path, self.jobs, run=raced)
        self.assertNotEqual(result["state"], "waiting-capacity", result)
        self.assertEqual(len(result["frame_attempts"]), 2)

    def test_untyped_or_kill_switch_refusal_stays_needs_attention(self):
        def kill_switch(command, **kwargs):
            node = command[command.index("--route-node") + 1] if "--route-node" in command else "owner"
            if node == "frame-alternative":
                receipt = self.refusal_receipt(retryable=None, reason="model-worker-governor-denied")
                return subprocess.CompletedProcess(command, 75, receipt, "kill switch")
            return self.admit(command, **kwargs)
        result = W.start_work(self.route, self.path, self.jobs, run=kill_switch)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "frame-launch-not-admitted")

    def test_no_harness_declaring_top_is_frame_harness_unavailable_with_no_attempt(self):
        def undeclared(command, **kwargs):
            receipt = ("status=unavailable\ntop_undeclared=claude,codex,opencode\n"
                       "check=failed\nreason=frame-harness-unavailable\nchild_spawned=0\n")
            return subprocess.CompletedProcess(command, 65, receipt, "")
        result = W.start_work(self.route, self.path, self.jobs, run=undeclared)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "frame-harness-unavailable")
        self.assertEqual(result["frame_attempts"], [])
        self.assertEqual(len(result["launches"]), 1)  # the anchor leg stopped the start; nothing else launched

    def test_automatic_frames_use_real_selector_usage_gate_before_wrapper_launch(self):
        owner = load("work_start_capacity_owner", W.ROOT / "utilities/dispatch-owner.py")
        self.route.update(cwd=self.tmp.name, capability="autopilot-code", capability_mode="debug",
                          owner_model_profile="deep")
        for node in self.route["nodes"][:2]:
            node.update(model_profile="deep", role="deep maker", unit="plan/frame")
        self.route["dispatch_evidence"]["tuples"] = [
            {"child_harness": h, "status": "supported"} for h in ("claude", "codex")]
        self.path.write_text(json.dumps(self.route))
        context = {"harnesses": ["claude", "codex"],
            "policy": {"primary": ["claude", "codex"], "relief": [], "last_resort": [],
                       "promote_relief_below": 0},
            "allocation": {"strategy": "balanced", "window": 30,
                "harness_order": ["claude", "codex", "opencode"], "usage_gate_used_percent": 85}}
        binding = SimpleNamespace(route_file=str(self.path), route_id=self.route["route_id"],
            route_hash=self.route["route_hash"], route_node="frame", registry_digest="sha256:fixture",
            write_scope="shards/frame/**", completion_gate="code-frame")
        launched = []
        stamp_harness = [True]
        def wrapper(command, **kwargs):
            launched.append(Path(command[0]).parents[1].name)
            return subprocess.CompletedProcess(command, 0)
        def select(command, **kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = owner.main(command[2:])
            if rc == 0:
                self.admit(command)
                if stamp_harness[0]:
                    self.jobs.write_text(self.jobs.read_text().rstrip() + ",harness=" + launched[-1] + "\n")
            return subprocess.CompletedProcess(command, rc, output.getvalue(), "")
        import artifact_producer
        for headroom in (30, 60):
            with self.subTest(headroom=headroom), contextlib.ExitStack() as stack:
                self.jobs.unlink(missing_ok=True); self.calls.clear(); launched.clear()
                stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
                stack.enter_context(mock.patch.object(owner, "_authoritative_jobs", return_value=str(self.jobs)))
                stack.enter_context(mock.patch.object(owner, "_sealed_owner_context", return_value=context))
                stack.enter_context(mock.patch.object(owner, "_usage", return_value=dict.fromkeys(("claude", "codex", "opencode"), "ok")))
                stack.enter_context(mock.patch.object(owner._capacity, "capacity_report", return_value={
                    "scores": {"claude": headroom, "codex": 50, "opencode": 0}, "sources": {}}))
                stack.enter_context(mock.patch.object(owner, "derive_frame_route_binding", return_value=binding))
                stack.enter_context(mock.patch.object(artifact_producer, "prepare_route_artifact_env", return_value={}))
                stack.enter_context(mock.patch.object(owner.subprocess, "run", side_effect=wrapper))
                if headroom == 30:
                    stamp_harness[0] = False
                    pending = W.start_work(self.route, self.path, self.jobs, run=select)
                    self.assertEqual(pending["state"], "preparing", pending)
                    self.assertEqual(pending["reason"], "frame-first-attempt-pending")
                    self.assertEqual(launched, ["codex"])
                    self.assertEqual(len(self.calls), 1)
                    self.jobs.write_text(self.jobs.read_text().rstrip() + ",harness=codex\n")
                    stamp_harness[0] = True
                result = W.start_work(self.route, self.path, self.jobs, run=select)
                self.assertEqual(result["state"], "preparing", result)
                self.assertEqual(launched, ["codex", "codex"] if headroom < 50 else ["claude", "claude"])
                first_launch = pending["launches"][0] if headroom == 30 else result["launches"][0]
                self.assertIn("selection_source=configured-balanced", first_launch["receipt"])
                self.assertIn("selection_source=configured-balanced", result["launches"][-1]["receipt"])
                # No authorized capacity is a refusal before any model wrapper.
                self.jobs.unlink(); self.calls.clear(); launched.clear()
                with mock.patch.object(owner, "_usage", return_value=dict.fromkeys(("claude", "codex", "opencode"), "limited(reset)")):
                    refused = W.start_work(self.route, self.path, self.jobs, run=select)
                self.assertEqual(refused["state"], "needs-attention", refused)
                self.assertEqual(launched, [])
                self.assertFalse(self.jobs.exists())

    def test_public_answer_submission_releases_then_starts_only_one_owner(self):
        self.start(); self.ready = True
        def release(*args, **kwargs):
            self.assertEqual(kwargs["answers"], "actual-answers.json")
            self.released = True
            return {"state": "released", "decision": "proceed"}
        with mock.patch.object(W, "frame_interview_step", side_effect=release):
            result = self.start(interview="question.json", answers="actual-answers.json")
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(len(self.calls), 3)

    def test_question_failure_after_frame_completion_does_not_promise_another_wake(self):
        self.start(); self.ready = True
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t"))
        with mock.patch.object(W, "frame_interview_step", side_effect=ValueError("bad-question")):
            result = self.start(interview="question.json")
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertNotIn("parent_next", result)
        self.assertEqual(len(self.calls), 2)

    def test_exited_owner_with_missing_work_reports_without_waiting_or_replacement(self):
        import dispatch_terminal_commit as T
        self.start(); self.ready = self.released = True; self.start()
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").replace(
            "worker_type=owner", "workflow_completion=runtime-v1,failure_class=pass,worker_type=owner"))
        with mock.patch.object(T, "owner_workflow_gaps", return_value={"report":"completion-marker-absent"}), \
             mock.patch.object(W, "join_selected_attempts", side_effect=AssertionError("cannot wait for an absent executor")):
            result = self.start(wait=True)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "workflow-executor-exited")
        self.assertNotIn("parent_next", result)
        self.assertEqual(len(self.calls), 3)

    def test_completed_deferred_owner_still_reports_workflow_gaps(self):
        """C15 (S3a): `verdict_pass` recognizes a marker-bound deferred owner
        row (failure_class stays infrastructure) the same way a plain
        failure_class=pass row already does -- the gap check must still run,
        not be skipped because the literal failure_class isn't "pass"."""
        import dispatch_terminal_commit as T
        self.start(); self.ready = self.released = True; self.start()
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").replace(
            "worker_type=owner",
            "workflow_completion=runtime-v1,failure_class=infrastructure,"
            "classifier_source=registered-wrapper-completion-transient-v1,"
            "completion_marker=/artifacts/.runtime/completions/one-shot.json,"
            "note=completed-marker,worker_type=owner"))
        with mock.patch.object(T, "owner_workflow_gaps", return_value={"report": "completion-marker-absent"}), \
             mock.patch.object(W, "join_selected_attempts", side_effect=AssertionError("cannot wait for an absent executor")):
            result = self.start(wait=True)
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "workflow-executor-exited")

    def test_failure_conflict_or_unsealed_workflow_never_authorizes_success(self):
        self.start(); self.ready = True
        for fields in ({"verdict":"FAIL"}, {"terminal_conflict":True}, {"workflow_complete":False}):
            with self.subTest(fields=fields):
                self.statuses[W.attempt_id(self.route,"frame")] = fields
                result = self.start()
                self.assertEqual(result["state"], "needs-attention", result)
                self.assertEqual(len(self.calls), 2)
                outcome = next(r for r in result["frame_results"] if r["classification"] == "attention")
                command = outcome["recovery_command"]
                self.assertIn(W.attempt_id(self.route,"frame"), command)
                self.assertNotIn("retry", command)

    def test_exited_owner_with_pending_closure_never_promises_running_or_a_new_turn(self):
        import dispatch_terminal_commit as T
        self.start(); self.ready = self.released = True; self.start()
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").replace(
            "worker_type=owner", "workflow_completion=runtime-v1,failure_class=pass,worker_type=owner"))
        self.ready = False
        self.statuses[W.attempt_id(self.route,"owner")] = {"workflow_complete":False}
        with mock.patch.object(T,"owner_workflow_gaps",return_value={}):
            for wait in (False,True):
                result=self.start(wait=wait)
                self.assertEqual(result["state"],"needs-attention",result)
                self.assertEqual(result["reason"],"owner-settlement-pending")
                self.assertIn(" finish ",result["result"]["recovery_command"])
                self.assertNotIn("parent_next",result)
                self.assertEqual(len(self.calls),3)

    def test_running_owner_receipt_states_the_parent_role(self):
        self.start(); self.ready = self.released = True; self.start()
        with mock.patch.object(W, "join_selected_attempts", return_value={"state": "timeout", "children": []}):
            result = self.start()
        self.assertEqual(result["state"], "running", result)
        self.assertIn("parent_next", result)
        self.assertIn("You are the parent session", result["next_step"])
        self.assertIn("Do not kill", result["next_step"])

    def test_owner_exits_during_join_before_the_public_receipt(self):
        self.start(); self.ready = self.released = True; self.start()
        def close_during_join(**kwargs):
            self.jobs.write_text(self.jobs.read_text().replace("\topen\t","\tdone\t"))
            return {"state":"timeout","children":[]}
        self.statuses[W.attempt_id(self.route,"owner")] = {"workflow_complete":False}
        with mock.patch.object(W,"join_selected_attempts",side_effect=close_during_join):
            result=self.start()
            self.assertEqual(result["state"],"needs-attention",result)
            self.assertNotIn("parent_next",result)
            self.statuses[W.attempt_id(self.route,"owner")] = {}
            self.assertEqual(self.start()["state"],"completed")
        self.assertEqual(len(self.calls),3)

    def test_frame_replacement_keeps_successful_sibling_and_gate_release(self):
        self.start(); self.ready=True; self.released=True
        source=W.attempt_id(self.route,'frame');success=W.attempt_id(self.route,'frame-alternative')
        self.jobs.write_text(self.jobs.read_text().replace('\topen\t','\tdone\t'))
        calls=[]
        def advance(jobs,attempts,**kwargs):
            calls.append(set(attempts))
            if source not in attempts:return set(attempts),[],[]
            row=next(line for line in jobs.read_text().splitlines() if 'attempt_id='+source+',' in line)
            with jobs.open('a') as f:f.write(row.replace(source,'att-frame-replacement')+'\n')
            return {success,'att-frame-replacement'},[{'original_attempt_id':source,'replacement_attempt_id':'att-frame-replacement'}],[]
        with mock.patch('dispatch_replacement.advance_batch',side_effect=advance):
            result=self.start()
        self.assertIn('att-frame-replacement',result['frame_attempts'])
        self.assertIn(success,result['frame_attempts'])
        self.assertEqual(len(self.calls),3)  # initial two frames, then the owner
        self.assertEqual(len(calls),2)
        self.assertNotIn('frame_interview',result)

    def test_owner_registered_replacement_is_resumed_without_new_owner_launch(self):
        self.start();self.ready=True;self.released=True;self.start()
        owner=W.attempt_id(self.route,'owner')
        row=next(line for line in self.jobs.read_text().splitlines() if 'attempt_id='+owner+',' in line)
        self.jobs.write_text(self.jobs.read_text().replace(row,row.replace('\topen\t','\tdone\t')+',note=dead-exact-pid'))
        with self.jobs.open('a') as f:f.write(row.replace(owner,'att-owner-replacement')+',replacement_original_attempt_id='+owner+',launch_claimed=0\n')
        self.ready=False;observed=[]
        def resume(jobs,aid,**kwargs):
            observed.append(aid);return {'state':'not-applicable'}
        with mock.patch('dispatch_replacement.advance',side_effect=resume):result=self.start()
        self.assertEqual(observed,['att-owner-replacement'])
        self.assertEqual(len(self.calls),3)
        self.assertEqual(result['owner_attempt_id'],'att-owner-replacement')

    def _wait_attention(self,aid):
        return {'state':'needs-attention','reason':'replacement-capacity-wait','source_attempt_id':aid,
                'node':'__owner__','harness':'claude','retry_at':'2099-01-01T00:00:00Z','usage_state':'limited(x)'}

    def test_owner_at_a_usage_limit_is_waiting_capacity_and_only_start_may_resume_it(self):
        self.start();self.ready=self.released=True;self.start()
        owner=W.attempt_id(self.route,'owner')
        row=next(line for line in self.jobs.read_text().splitlines() if 'attempt_id='+owner+',' in line)
        self.jobs.write_text(self.jobs.read_text().replace(
            row,row.replace('\topen\t','\tdone\t')+',note=dead-capacity,failure_class=capacity'))
        launches=len(self.calls);seen={}
        def wait(jobs,aid,**kwargs):
            seen.update(kwargs);return self._wait_attention(aid)
        with mock.patch('dispatch_replacement.advance',side_effect=wait):
            result=self.start()
        self.assertTrue(seen['resume_capacity'])          # start is the one explicit resume
        self.assertEqual((result['state'],result['reason'],result['required_action']),
                         ('waiting-capacity','owner-capacity-wait','resume-after-capacity'))
        self.assertEqual((result['retry_at'],result['harness'],result['source_attempt_id']),
                         ('2099-01-01T00:00:00Z','claude',owner))
        self.assertNotIn('parent_next',result);self.assertNotIn('parent_next_command',result)
        self.assertIn('from the session that owns the route',result['next_step'])
        self.assertEqual(len(self.calls),launches)

    def test_frame_replacement_held_by_a_usage_limit_waits_instead_of_failing(self):
        self.start();self.ready=True
        frame=W.attempt_id(self.route,'frame')
        seen={}
        def held(jobs,attempts,**kwargs):
            seen.update(kwargs);return set(attempts),[],[self._wait_attention(frame)]
        with mock.patch('dispatch_replacement.advance_batch',side_effect=held):
            result=self.start()
        self.assertNotIn('resume_capacity',seen)          # a supervisor-shaped call never resumes
        self.assertEqual((result['state'],result['reason']),('waiting-capacity','owner-capacity-wait'))
        self.assertEqual(result['retry_at'],'2099-01-01T00:00:00Z')

    # -- an owner its launcher closed before spawning: nothing ran, so say "start again later" ----
    def _never_started_meta(self, aid):
        return {"attempt_id": aid, "parent_sid": "parent", "worker_type": "owner", "dispatch_depth": "1",
                "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                "owner_route_hash": self.route["route_hash"], "owner_route_file": str(self.path),
                "launch_claimed": "0", "launch_outcome": "never-launched",
                "note": "dead-producer-binding-failed", "failure_class": "contract"}

    def _unstarted_run(self, reason="admission-busy"):
        """A launcher that registers the owner, then closes it before spawning (exit 73)."""
        def run(command, **kwargs):
            self.calls.append(command)
            meta = self._never_started_meta(command[command.index("--attempt-id") + 1])
            with self.jobs.open("a") as stream:
                stream.write("now\tdone\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
            return subprocess.CompletedProcess(
                command, 73, f"check=failed\nreason={reason}\ndetail=admission lock busy\nchild_spawned=0\n", "")
        return run

    def test_owner_that_did_not_start_says_run_start_later_not_harvest(self):
        self.route["nodes"] = []
        result = W.start_work(self.route, self.path, self.jobs, run=self._unstarted_run())
        self.assertEqual((result["state"], result["reason"], result["required_action"]),
                         ("needs-attention", "owner-launch-not-started", "resume-later"), result)
        self.assertEqual(result["launch_reason"], "admission-busy")
        self.assertEqual(result["owner_attempt_id"], W.attempt_id(self.route, "owner"))
        self.assertIn("capability-route.py", result["recovery_command"])
        self.assertIn("start", result["recovery_command"])
        self.assertNotIn("harvest", result["recovery_command"])
        self.assertIn("again later", result["next_step"])
        self.assertEqual(len(self.calls), 1)   # one launch per start, however it ended

    def test_owner_preparation_busy_with_no_row_says_run_start_later(self):
        """dispatch-owner failed before any row existed: the typed admission-busy error."""
        self.route["nodes"] = []
        calls = self.calls

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command, 65, "check=failed\nreason=admission-busy:admission lock held past 120s; "
                "nothing started; run start again later\nchild_spawned=0\n", "")
        result = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual((result["state"], result["reason"], result["required_action"]),
                         ("needs-attention", "owner-launch-not-admitted", "resume-later"), result)
        self.assertEqual(result["launch_reason"], "admission-busy")
        self.assertEqual(result["recovery_command"], result["resume_command"])
        self.assertIn("capability-route.py", result["recovery_command"])
        self.assertNotIn("harvest", result["recovery_command"])
        self.assertIn("Nothing ran", result["next_step"])
        self.assertIn("again later", result["next_step"])
        self.assertEqual(len(calls), 1)
        # no row exists, so the next start launches the same owner again
        again = W.start_work(self.route, self.path, self.jobs, run=self._unstarted_run())
        self.assertEqual(again["reason"], "owner-launch-not-started", again)
        self.assertEqual(len(calls), 2)

    def test_owner_launch_refused_for_another_reason_keeps_needs_inspection(self):
        self.route["nodes"] = []
        result = W.start_work(self.route, self.path, self.jobs, run=lambda command, **kwargs:
                              subprocess.CompletedProcess(command, 65, "check=failed\nreason=x\n", ""))
        self.assertEqual((result["state"], result["reason"]), ("needs-attention", "owner-launch-not-admitted"))
        self.assertNotEqual(result.get("required_action"), "resume-later")

    def test_outcome_of_a_never_started_owner_points_at_start(self):
        aid = W.attempt_id(self.route, "owner")
        meta = self._never_started_meta(aid)
        self.jobs.write_text("now\tdone\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        command = W._outcome(self.jobs, aid)["recovery_command"]
        self.assertIn("capability-route.py", command)
        self.assertIn(str(self.path), command)
        self.assertNotIn("harvest", command)

    def test_next_start_relaunches_an_owner_that_never_started(self):
        """The row shape of the stuck BC route: closed by its launcher, no log, sealed launch input."""
        import dispatch_replacement as R
        from dispatch_contract import parse_registry_metadata
        self.route["nodes"] = []
        self.route.update(artifact_root=self.tmp.name, cwd=self.tmp.name, capability="autopilot-code")
        self.path.write_text(json.dumps(self.route))
        aid = W.attempt_id(self.route, "owner")
        args = SimpleNamespace(attempt_id=aid, jobs_path=self.jobs, worktree=self.tmp.name,
                               route_id=self.route["route_id"], route_node="",
                               replacement_input_argv=["--start", "--attempt-id", aid, "--prompt-text", "the raw task"])
        meta = {**self._never_started_meta(aid), "attempt_schema_version": "2", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "fallback_hop": "same-harness-headless", "harness": "codex",
                "log_file": str(Path(self.tmp.name) / "never-written.jsonl")}
        meta.update(parse_registry_metadata(R.seal_launch_input(args, "codex", "the raw task")))
        self.jobs.write_text("now\tdone\t" + self.tmp.name + "\t" + self.tmp.name + "\ttask\t"
                             + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        launched = []

        def run(command, **kwargs):
            launched.append(command)
            replacement = command[command.index("--attempt-id") + 1]
            record = R.claim(self.jobs, aid)   # the one claim `advance` already made, replayed
            source = R._rows(self.jobs.read_text().splitlines())[aid][1]
            replay = R.launch_input(self.jobs, aid, source)
            sealed = SimpleNamespace(**vars(args))
            sealed.attempt_id = replacement
            sealed.replacement_input_argv = R._replacement_argv(record, source, replay)
            row = {k: v for k, v in meta.items()
                   if k not in ("note", "failure_class", "launch_outcome", "replacement_input_digest")}
            row.update(attempt_id=replacement, automatic_retry_of=aid, launch_claimed="1", launch_started="1",
                       replacement_family_id=record["family_id"], replacement_original_attempt_id=aid,
                       replacement_ordinal="1", replacement_claim_digest=R._digest(record))
            row.update(parse_registry_metadata(R.seal_launch_input(sealed, "codex", "the raw task")))
            with self.jobs.open("a") as stream:
                stream.write("now\topen\t" + self.tmp.name + "\t" + self.tmp.name + "\ttask\t"
                             + ",".join(k + "=" + v for k, v in row.items()) + "\n")
            return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

        with mock.patch("dispatch_replacement._authorized"), \
             mock.patch("dispatch_replacement._reuse_snapshot", return_value={
                 "completed": [], "cycle_id": "cyc-test", "producer_id": "prod-test", "gate_releases": []}), \
             mock.patch("dispatch_replacement._logical_key",
                        side_effect=lambda r, m: {"root_route_id": "rt-root", "node": "__owner__"}), \
             mock.patch("dispatch_replacement._route", return_value=(self.path, self.route)), \
             mock.patch("dispatch_replacement._runtime_drift"), \
             mock.patch("dispatch_replacement._terminal_absent", side_effect=AssertionError("no log to read")), \
             mock.patch("dispatch_contract.attempt_process_quiescence",
                        return_value=SimpleNamespace(state="quiescent", reason="process-absent")), \
             mock.patch("dispatch_capacity_evidence.harness_hold", return_value=None), \
             mock.patch("dispatch_replacement_batch.command", return_value=None):
            result = W.start_work(self.route, self.path, self.jobs, run=run)
        self.assertEqual(len(launched), 1, result)
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(self.calls, [])   # the fresh-owner launcher was not used: a replacement was
        self.assertEqual([edge["original_attempt_id"] for edge in result["replacement_lineage"]], [aid])

    def _parked_owner_row(self):
        self.start();self.ready=self.released=True;self.start()
        owner=W.attempt_id(self.route,'owner')
        row=next(line for line in self.jobs.read_text().splitlines() if 'attempt_id='+owner+',' in line)
        self.jobs.write_text(self.jobs.read_text().replace(
            row,row.replace('\topen\t','\tdone\t')+',note=dead-worker-blocked,failure_class=blocked'))
        self.ready=False
        return owner

    def _parked(self,status):
        return {'gate':'full-run-authorization','status':status,'epoch':1,'raised_at':'2026-09-29T01:00:00Z',
                'artifact':'/tmp/gate.md','route_file':str(self.path),'route_id':'rt-probe',
                'route_hash':'sha256:probe','gated_nodes':['full-run']}

    def test_parked_owner_waits_for_the_person_without_failure_or_wait(self):
        self._parked_owner_row()
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('blocked')), \
             mock.patch('dispatch_replacement.advance',side_effect=AssertionError('a blocked gate never continues')):
            result=self.start()
        self.assertEqual((result['state'],result['reason'],result['required_action']),
                         ('waiting-human-gate','owner-parked-at-human-gate','answer-human-gate'))
        self.assertEqual((result['gate'],result['gate_artifact']),('full-run-authorization','/tmp/gate.md'))
        for token in ('--gate full-run-authorization','--decision proceed','--jobs '+str(self.jobs),'workflow-supervisor.py'):
            self.assertIn(token,result['release_command'])
        self.assertNotIn('parent_next',result)
        self.assertIn('resume_command',result)
        self.assertEqual(len(self.calls),3)

    def test_a_parked_receipt_points_at_the_owners_own_report_when_it_has_one(self):
        import base64
        self._parked_owner_row()
        report=self.path.parent/'owner-report.md'
        report.write_text('what the owner says it changed\n')
        encoded=base64.urlsafe_b64encode(str(report).encode()).decode().rstrip('=')
        readable={'state':'valid','verdict':'PASS','artifact_state':'readable','artifact_path_b64':encoded}
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('blocked')), \
             mock.patch('codex_dispatch_terminal.inspect_terminal_attempt',return_value=readable):
            result=self.start()
        self.assertEqual(result['state'],'waiting-human-gate')
        self.assertEqual(result['owner_report'],str(report))
        self.assertIn('owner_report',result['next_step'])
        unreadable={'state':'invalid','verdict':'-','artifact_state':'missing','artifact_path_b64':''}
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('blocked')), \
             mock.patch('codex_dispatch_terminal.inspect_terminal_attempt',return_value=unreadable):
            result=self.start()
        self.assertEqual(result['state'],'waiting-human-gate')
        self.assertNotIn('owner_report',result)
        self.assertNotIn('owner_report',result['next_step'])

    def test_stopped_gate_reports_without_replacement(self):
        self._parked_owner_row()
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('stop')), \
             mock.patch('dispatch_replacement.advance',side_effect=AssertionError('a stop never continues')):
            result=self.start()
        self.assertEqual((result['state'],result['reason']),('stopped','human-gate-stop'))
        self.assertEqual(len(self.calls),3)

    def test_revised_gate_with_parked_owner_needs_attention_without_continuation(self):
        self._parked_owner_row()
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('revise')), \
             mock.patch('dispatch_replacement.advance',side_effect=AssertionError('a revise never continues')):
            result=self.start()
        self.assertEqual((result['state'],result['reason'],result['required_action']),
                         ('needs-attention','human-gate-revise-owner-parked','report-gate-revision'))
        self.assertEqual(len(self.calls),3)

    def test_released_parked_owner_continues_through_replacement(self):
        owner=self._parked_owner_row()
        row=next(line for line in self.jobs.read_text().splitlines() if 'attempt_id='+owner+',' in line)
        def replace(jobs,aid,**kwargs):
            if aid!=owner:return {'state':'not-applicable'}
            with self.jobs.open('a') as f:
                f.write(row.replace(owner,'att-owner-continuation').replace('\tdone\t','\topen\t')
                        .replace(',note=dead-worker-blocked,failure_class=blocked','')
                        +',replacement_original_attempt_id='+owner+',launch_claimed=1\n')
            return {'state':'running','record':{'route_file':str(self.path)}}
        with mock.patch('dispatch_replacement.owner_parked_gate',return_value=self._parked('proceed')), \
             mock.patch('dispatch_replacement.advance',side_effect=replace), \
             mock.patch('dispatch_replacement.effective_attempts',return_value=({'att-owner-continuation'},[])):
            result=self.start()
        self.assertEqual(result['state'],'running',result)
        self.assertEqual(result['owner_attempt_id'],'att-owner-continuation')

    def test_unknown_process_retains_runtime_wait_without_replacement(self):
        self.start()
        for _ in range(3):
            result = self.start()
            self.assertEqual(result["state"], "preparing")
        self.assertEqual(len(self.calls), 2)

    def test_bounded_wait_deadline_hands_back_without_another_wait_or_retry(self):
        for owner in (False, True):
            with self.subTest(owner=owner):
                self.jobs.unlink(missing_ok=True); self.calls.clear()
                if owner:
                    self.route["nodes"] = []
                result = self.start(wait=True)
                self.assertEqual(result["state"],"needs-attention",result)
                self.assertEqual(result["reason"],"parent-wait-deadline")
                self.assertEqual(result["required_action"],"report-pending-work")
                self.assertNotIn("parent_next_command",result)
                self.assertIn("runtime watchers retain",result["next_step"])
                before = len(self.calls)
                self.start()
                self.assertEqual(len(self.calls),before)

    def test_foreign_parent_is_checked_before_any_new_sibling(self):
        self.admit(["fixture", "--route-node", "frame-alternative", "--attempt-id", W.attempt_id(self.route,"frame-alternative")])
        self.jobs.write_text(self.jobs.read_text().replace("parent_sid=parent", "parent_sid=other"))
        self.calls.clear()
        result = self.start()
        self.assertEqual(result["reason"], "work-parent-recovery-required", result)
        self.assertEqual(self.calls, [])

    def test_legacy_closure_cannot_hide_runtime_owner_pending_settlement(self):
        import dispatch_terminal_commit as terminal
        self.path.with_suffix(".outcome.json").write_text(json.dumps({
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"], "terminal_gate_proven": True}))
        with mock.patch.object(W, "_rows", return_value={"att-owner": ("done", {
            "workflow_completion": "runtime-v1", "owner_route_id": self.route["route_id"]})}), \
                mock.patch.object(terminal, "owner_completion_state", return_value=terminal.CompletionState("pending")):
            self.assertEqual(self.start()["reason"], "workflow-completion-pending")
        self.assertEqual(self.calls, [])

    def test_successor_session_harvests_a_finished_route_but_not_a_live_one(self):
        # A supervisor hands the route to another session: finished attempts
        # carry only a result, so the successor reads it instead of being told
        # to recover a parent it can never become.
        self.start(); self.ready = self.released = True; self.start()
        self.assertEqual(len(self.calls), 3)
        W.default_parent_session_id.return_value = "successor"
        live = self.start()
        self.assertEqual(live["reason"], "work-parent-recovery-required", live)
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t"))
        result = self.start()
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(len(self.calls), 3)

    def test_resume_keeps_native_parent_when_gateway_transport_has_advanced(self):
        self.start()
        with mock.patch.dict(os.environ, {"AGENT_CODEX_MANAGED_GATEWAY": "1", "AGENT_DISPATCH_CHILD": "0"}), \
             mock.patch.object(W, "interactive_parent_identity", return_value=("codex", "parent")), \
             mock.patch.object(W, "probe_managed_codex_parent", return_value=SimpleNamespace(thread_id="gateway-successor")) as probe:
            self.assertEqual(self.start()["state"], "preparing")
            self.assertEqual(len(self.calls), 2)
            probe.assert_not_called()
            probe.return_value = SimpleNamespace(thread_id="sibling")
            self.assertEqual(self.start()["state"], "preparing")
            self.assertEqual(len(self.calls), 2)
            probe.side_effect = W.ManagedDispatchError("managed-gateway-not-ready")
            self.assertEqual(self.start()["state"], "preparing")
            probe.assert_not_called()
            self.assertEqual(len(self.calls), 2)

    def test_route_hash_collision_is_never_adopted(self):
        self.start()
        self.jobs.write_text(self.jobs.read_text().replace("route_hash=sha256:probe", "route_hash=sha256:other"))
        self.assertEqual(self.start()["reason"], "work-attempt-identity-conflict")
        self.assertEqual(len(self.calls), 2)

    def adapters(self):
        return [c[c.index("--adapter") + 1] if "--adapter" in c else None for c in self.calls]

    def test_a_pinned_tool_is_passed_to_both_frame_legs_and_then_the_owner(self):
        pins = {"contract_version": 1,
                "owner": {"harness": "opencode", "model": None, "effort": None}}
        self.route["selection_pins"] = pins
        self.start()
        # Both legs run on the one tool the caller named (SD-160 allows a shared
        # harness); with no pin they stay on automatic selection (no --adapter).
        self.assertEqual(self.adapters(), ["opencode", "opencode"])
        self.ready = True; self.released = True
        self.start()
        self.assertEqual(self.adapters(), ["opencode", "opencode", "opencode"])

    def test_a_frame_pin_wins_over_the_owner_pin_for_the_frame_legs_only(self):
        self.route["selection_pins"] = {"contract_version": 1,
            "owner": {"harness": "codex", "model": None, "effort": None},
            "frame": {"harness": "opencode", "model": "provider/model", "effort": "max"}}
        self.start()
        self.assertEqual(self.adapters(), ["opencode", "opencode"])
        self.ready = True; self.released = True
        self.start()
        self.assertEqual(self.adapters()[2], "codex")

    def test_a_pinned_tool_the_route_never_probed_is_not_forced(self):
        self.route["selection_pins"] = {"contract_version": 1,
            "frame": {"harness": "claude", "model": None, "effort": None}}
        result = self.start()
        self.assertEqual(self.adapters(), [None, None])
        self.assertEqual(result["frame_explicit_harness"], "unavailable:claude")

    def test_a_route_without_pins_keeps_automatic_frame_selection(self):
        self.assertNotIn("selection_pins", self.route)
        result = self.start()
        self.assertEqual(self.adapters(), [None, None])
        self.assertNotIn("frame_explicit_harness", result)

    def test_quick_uses_the_sealed_candidate_pool(self):
        self.route["effective_intensity"] = "quick"
        self.route["registered_headless_candidates"] = [{"harness":"opencode","status":"supported"}]
        self.start()
        self.assertTrue(all("--adapter" not in c for c in self.calls))
        self.assertTrue(all(c[c.index("--route-evidence") + 1] == str(self.path) for c in self.calls))

    def test_owner_completion_waits_for_runtime_closure_and_reuses_attempt(self):
        self.route["nodes"] = []
        self.ready = True
        aid = W.attempt_id(self.route, "owner")
        self.statuses[aid] = {"workflow_complete":False}
        result = self.start()
        self.assertEqual(result["result"]["required_action"], "finish-workflow", result)
        self.statuses.clear()
        self.assertEqual(self.start()["state"], "completed")
        self.assertEqual(len(self.calls), 1)

    def test_claim_interruption_leaves_admitted_attempt_recoverable(self):
        def interrupted(command, **kwargs):
            result = self.admit(command, **kwargs)
            raise OSError("caller disconnected after admission")
        result = W.start_work(self.route, self.path, self.jobs, run=interrupted)
        self.assertEqual(result["state"], "needs-attention")
        self.assertEqual(result["registered_attempts"], [W.attempt_id(self.route,"frame")])
        self.assertEqual(result["parent_next"], "end-turn")
        self.assertEqual(self.start()["state"], "preparing")
        self.assertEqual(len(self.calls), 2)

    def test_direct_never_spawns(self):
        self.route["effective_intensity"] = "direct"
        import artifact_producer
        with mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                               return_value={"AGENT_ARTIFACT_OUTPUT_DIR": "/exact/artifacts"}) as prepare:
            result = self.start()
        self.assertEqual(result["state"], "inline")
        self.assertEqual(result["artifact_env"]["AGENT_ARTIFACT_OUTPUT_DIR"], "/exact/artifacts")
        prepare.assert_called_once_with(self.path, start=True, jobs=self.jobs)
        self.assertEqual(self.calls, [])

    def test_closed_request_replay_never_launches_or_prepares_artifacts(self):
        import artifact_producer
        outcome = {"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                   "terminal_gate_proven": True}
        self.path.with_suffix(".outcome.json").write_text(json.dumps(outcome))
        with mock.patch.object(artifact_producer, "prepare_route_artifact_env", side_effect=AssertionError("reopen")):
            for intensity in ("direct", "standard"):
                self.route["effective_intensity"] = intensity
                self.assertEqual(self.start()["state"], "completed")
        self.assertEqual(self.calls, [])
        outcome["terminal_gate_proven"] = False
        self.path.with_suffix(".outcome.json").write_text(json.dumps(outcome))
        self.assertEqual(self.start()["reason"], "route-closed-unproven")

    def test_direct_start_replay_is_fenced_while_inline_finish_is_pending(self):
        import artifact_producer
        import inline_finish
        pending = {"schema": "inline_finish_v1", "inline_finish_id": "deadbeef",
                   "state": "node-completed", "intent": {"route_id": self.route["route_id"]}}
        with mock.patch.object(inline_finish, "pending_state", return_value=pending), \
             mock.patch.object(artifact_producer, "prepare_route_artifact_env",
                               side_effect=AssertionError("must not resume preparation")):
            for intensity in ("direct", "standard"):
                self.route["effective_intensity"] = intensity
                result = self.start()
                self.assertEqual(result["state"], "needs-attention", result)
                self.assertEqual(result["reason"], "finish-pending")
                self.assertEqual(result["required_action"], "resume-inline-finish")
                self.assertEqual(result["finish_state"], "node-completed")
        self.assertEqual(self.calls, [])

    def test_closed_runtime_owner_requires_complete_settlement(self):
        import dispatch_terminal_commit as terminal
        outcome = {"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                   "terminal_gate_proven": True, "terminal_owner_attempt_id": "att-owner"}
        self.path.with_suffix(".outcome.json").write_text(json.dumps(outcome))
        with mock.patch.object(W, "_rows", return_value={"att-owner": ("done", {"workflow_completion": "runtime-v1"})}):
            for state in ("pending", "blocked", "unknown", "complete"):
                with mock.patch.object(terminal, "owner_completion_state", return_value=terminal.CompletionState(state)):
                    self.assertEqual(self.start()["state"], "completed" if state == "complete" else "needs-attention")
        self.assertEqual(self.calls, [])

    def test_production_selector_and_three_adapter_parsers_accept_the_public_request(self):
        router = load("work_start_router_test", W.ROOT / "utilities/capability-route.py")
        selector = load("work_start_selector_test", W.ROOT / "utilities/dispatch-owner.py")
        evidence = {"tuples": [{"parent_harness":"codex", "child_harness":h,
            "parent_transport":"headless", "parent_sandbox":"workspace-write", "launch_authority":"conductor",
            "status":"supported", "probe_source":"fixture-check", "probe_time":"2026-09-12T00:00:00Z",
            "failure_class":"", "checked_worktree":str(W.ROOT), "failure_scope":"none",
            "codex_command":"ok", "retry_on_isolated_worktree":0} for h in ("codex","opencode")]}
        env = {k:v for k,v in os.environ.items() if not k.startswith(("AGENT_DISPATCH_","AGENT_ROUTE_","AGENT_OWNER_ROUTE_","AGENT_ARTIFACT_"))}
        env["AGENT_HOME"] = str(W.ROOT)
        with mock.patch.dict(os.environ, env, clear=True):
            route = router.compose_route(capability="autopilot-code", capability_mode="dev", shape="staged",
                graph="frame,frame-alternative,test,report", slug="work-start", cwd=W.ROOT,
                artifact_root=self.tmp.name, dispatch_evidence=evidence, parent_harness="codex", profile="light",
                work_request={"text":"Run both commands and record exit 7 and exit 0.","owner_harness":"codex"},
                unassigned=True)
            router.verify_route(route, W.ROOT)
            self.path.write_text(json.dumps(route))
            for node in ("frame", "frame-alternative", "owner"):
                for harness in ("claude", "codex", "opencode"):
                    with self.subTest(node=node,harness=harness):
                        def run(command, **kwargs):
                            _, values, forwarded, _, _ = selector._parse(command[2:])
                            adapter = load("work_start_adapter_"+harness, W.ROOT / "adapters" / harness / "bin/dispatch-headless.py")
                            args = adapter.parser().parse_args(forwarded)
                            self.assertEqual(args.worker_type,"owner" if node=="owner" else "frame")
                            self.assertEqual(args.dispatch_depth,1)
                            self.assertEqual(adapter.resolve_model_settings(args)["profile"],"light")
                            if node == "owner":
                                self.assertEqual(args.prompt_text,route["work_request"]["text"])
                            else:
                                # A frame leg reads a prompt file: the request first, then the part catalogue.
                                self.assertIsNone(args.prompt_text)
                                text = Path(args.prompt_file).read_text(encoding="utf-8")
                                self.assertTrue(text.startswith(route["work_request"]["text"] + "\n"))
                                self.assertIn("## Part catalogue", text)
                            self.assertEqual(args.attempt_id,W.attempt_id(route,node))
                            return subprocess.CompletedProcess(command,0,"validated","")
                        W._start(route,self.path,self.jobs,node,harness,run)


WF = load("work_start_workflow_fixture", W.ROOT / "utilities/workflow_supervisor.test.py")


class FrameInterviewStepTest(WF.WorkflowFixture):
    """Real ledger/gate/answer/intent code; transport and producer location are isolated."""
    def setUp(self):
        super().setUp()
        self.route, self.path = self.two_stage_route(human_gate="frame-review",
            continuation={"kind": "human-gate", "gate": "frame-review"})
        self.jobs = self.base / "jobs.log"
        self.jobs.write_text("")
        self.output = self.base / "artifacts"
        self.calls = []
        self.question = {"understanding": "Run the two commands and preserve their actual results.",
            "brief": {"problem": "We need the measured results.", "outcome": "One report with both results.",
                "affected": "The report only.", "constraints": "Preserve the expected exit code 7.", "open": ""},
            "questions": []}
        self.question_file = self.base / "question.json"
        self.question_file.write_text(json.dumps(self.question))
        import artifact_producer
        for patch in (
            mock.patch.object(artifact_producer, "prepare_route_artifact_env", return_value={"AGENT_ARTIFACT_OUTPUT_DIR": str(self.output)}),
            mock.patch.object(WF.SUP, "create_gate_delivery", return_value=(self.base / "gate-record.json", True)),
            mock.patch.object(WF.SUP, "retire_gate_delivery", return_value="acked"),
        ):
            patch.start(); self.addCleanup(patch.stop)

    def run_command(self, argv, **kwargs):
        self.calls.append(argv[2])
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = WF.SUP.main(argv[2:])
        except (WF.SUP.SupervisorError, WF.WS.WorkflowStateError) as exc:
            rc = 64; stderr.write(str(exc))
        return subprocess.CompletedProcess(argv, rc, stdout.getvalue(), stderr.getvalue())

    def step(self, **kwargs):
        return W.frame_interview_step(self.route, self.path, self.jobs, run=self.run_command, **kwargs)

    def answers(self):
        import frame_interview as FI
        a = FI.answers_template({**self.question, "route_id": self.route["route_id"]})
        a["understanding_confirmed"] = True
        path = self.base / "answers.json"
        path.write_text(json.dumps(a))
        return path

    def resolution(self):
        ledger = WF.WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        return WF.WS.human_gate_resolution(ledger.journal(), "frame-review")

    def test_register_before_question_then_actual_answers_release_once(self):
        self.assertEqual(self.step()["state"], "needs-interview")
        self.assertEqual(self.step(interview=self.question_file)["state"], "needs-question")
        self.assertEqual(self.resolution()["status"], "blocked")
        self.assertEqual(self.step()["state"], "needs-question")
        self.assertEqual(self.resolution()["epoch"], 1)
        result = self.step(answers=self.answers())
        self.assertEqual(result["state"], "released")
        intent = Path(result["intent_file"]).read_bytes()
        self.assertIn(b"status: agreed", intent)
        self.assertEqual(self.resolution()["answers"]["understanding_confirmed"], True)
        self.assertEqual(self.step(answers=self.answers())["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertEqual(Path(result["intent_file"]).read_bytes(), intent)

    def routed(self, *, approve=0, route_choice=0):
        """An interview that maps its one route question to a proposal with a start approval."""
        import frame_interview as FI
        leg = {"capability": "autopilot-lab", "mode": "setup", "shape": "staged",
               "graph": ["scaffold", "smoke", "full-run", "run-verify", "handoff"]}
        proposal = {"summary": "Set the experiment up and run it", "legs": [leg],
                    "entry_approvals": [{"key": "full-run", "leg": 0, "question": "run-ok"}]}
        ask = lambda qid, text, labels: {"id": qid, "topic": qid, "question": text, "kind": "yes-no", "recommended": 0,
                                         "options": [{"label": label, "means": label, **({"approves": True} if at == 0 else {})}
                                                     for at, label in enumerate(labels)],
                                         "why": "Only you can decide this."}
        question = {**self.question, "questions": [
            ask("go", "이 순서로 진행할까요?", ["예, 이 순서로", "아니요, 다르게"]),
            ask("run-ok", "긴 실험을 지금 시작해도 될까요?", ["예, 시작", "아니요, 나중에"])],
            "route_proposals": {"question": "go", "by_option": {"예, 이 순서로": proposal}}}
        self.question_file.write_text(json.dumps(question))
        response = FI.answers_template({**question, "route_id": self.route["route_id"]})
        response["understanding_confirmed"] = True
        response["answers"] = {"go": {"choice": route_choice, "note": ""}, "run-ok": {"choice": approve, "note": ""}}
        answer = self.base / "routed-answers.json"
        answer.write_text(json.dumps(response))
        return question, answer

    def test_a_route_proposals_interview_renders_the_route_and_the_approval_into_the_intent(self):
        question, answer = self.routed()
        result = self.step(interview=self.question_file, answers=answer)
        self.assertEqual(result["state"], "released")
        intent = Path(result["intent_file"]).read_text()
        self.assertIn("## Route", intent)
        self.assertIn("Selected route: Set the experiment up and run it", intent)
        self.assertIn("- full-run for leg 0 (autopilot-lab:full-run) — question `run-ok`: approved", intent)
        self.assertEqual(self.resolution()["answers"]["answers"]["run-ok"]["choice"], 0)
        self.assertEqual(self.step(answers=answer)["state"], "released")         # replay: same bytes, no rewrite
        self.assertEqual(Path(result["intent_file"]).read_text(), intent)
        self.assertEqual(self.calls, ["gate", "release"])

    def test_declining_the_approval_or_the_route_is_a_valid_answer_and_is_written_as_such(self):
        _, declined = self.routed(approve=1)
        intent = Path(self.step(interview=self.question_file, answers=declined)["intent_file"]).read_text()
        self.assertIn("question `run-ok`: declined", intent)
        self.setUp()
        _, off = self.routed(route_choice="none")
        self.question_file.write_text(json.dumps({**json.loads(self.question_file.read_text())}))
        with self.assertRaisesRegex(ValueError, "frame-input-invalid"):          # off-menu needs its note
            self.step(interview=self.question_file, answers=off)
        answers = json.loads(off.read_text()); answers["answers"]["go"]["note"] = "Do something else."
        off.write_text(json.dumps(answers))
        intent = Path(self.step(interview=self.question_file, answers=off)["intent_file"]).read_text()
        self.assertIn("No route was selected (a different direction was chosen)", intent)

    def test_an_approval_question_without_exactly_one_approving_option_is_refused_before_a_gate_is_raised(self):
        for label, marks in (("none marked", (False, False)), ("both marked", (True, True))):
            with self.subTest(label):
                self.setUp()
                question, answer = self.routed()
                for option, mark in zip(question["questions"][1]["options"], marks):
                    option.pop("approves", None)
                    if mark:
                        option["approves"] = True
                self.question_file.write_text(json.dumps(question))
                with self.assertRaisesRegex(ValueError, "frame-input-invalid: approval question 'run-ok'"):
                    self.step(interview=self.question_file, answers=answer)
                self.assertEqual(self.calls, [])

    def test_a_route_proposals_reference_that_points_nowhere_is_refused_before_a_gate_is_raised(self):
        question, answer = self.routed()
        question["route_proposals"]["question"] = "nope"
        self.question_file.write_text(json.dumps(question))
        with self.assertRaisesRegex(ValueError, "frame-input-invalid: route_proposals.question: no such question"):
            self.step(interview=self.question_file, answers=answer)
        self.assertEqual((self.calls, self.resolution()["status"]), ([], "not-raised"))

    def test_an_interview_without_the_field_renders_the_same_intent_as_before(self):
        import frame_interview as FI
        result = self.step(interview=self.question_file, answers=self.answers())
        intent = Path(result["intent_file"]).read_text()
        interview = json.loads(Path(result["interview_file"]).read_text())
        self.assertNotIn("route_proposals", interview)
        self.assertEqual(intent, FI.render_intent(interview, self.resolution()["answers"], now=interview["created"]))
        self.assertNotIn("## Route", intent)

    def test_a_crash_after_the_answers_or_the_intent_are_saved_resumes_to_one_release(self):
        for point in ("after-answer-save", "after-intent-render"):
            with self.subTest(point):
                self.setUp()
                question, answer = self.routed()
                def stop(name, point=point):
                    if name == point:
                        raise RuntimeError("crash at " + name)
                W.FAULT_HOOK = stop
                self.addCleanup(setattr, W, "FAULT_HOOK", None)
                with self.assertRaisesRegex(RuntimeError, "crash at " + point):
                    self.step(interview=self.question_file, answers=answer)
                W.FAULT_HOOK = None
                self.assertNotEqual(self.resolution()["status"], "proceed")      # not released yet
                result = self.step(interview=self.question_file, answers=answer)
                self.assertEqual(result["state"], "released")
                self.assertEqual(self.calls.count("release"), 1)
                self.assertIn("## Route", Path(result["intent_file"]).read_text())
                self.assertEqual(self.step(answers=answer)["state"], "released")
                self.assertEqual(self.calls.count("release"), 1)

    def test_answers_are_recorded_as_decisions_only_by_the_release_not_by_the_start_path(self):
        """D-87: `release` is the one place an answer is accepted; the start path calls it
        and must not record a second time, however often the same answers are replayed."""
        import frame_interview as FI
        import tidy_decisions
        question = {**self.question, "questions": [{
            "id": "q-scope", "topic": "How much to change",
            "question": "Fix only the approval step, or the questions too?", "kind": "choice",
            "options": [{"label": "Both (recommended)", "means": "Fix both."},
                        {"label": "Approval only", "means": "Leave the questions."}],
            "recommended": 0, "why": "Only you can weigh the wording against the schedule."}]}
        self.question_file.write_text(json.dumps(question))
        template = FI.answers_template({**question, "route_id": self.route["route_id"]})
        template["understanding_confirmed"] = True
        template["answers"]["q-scope"].update(choice=0, note="go on")
        answers = self.base / "answers.json"
        answers.write_text(json.dumps(template))
        with mock.patch.object(tidy_decisions, "record_interview_answers", return_value="recorded") as record:
            self.assertEqual(self.step(interview=self.question_file)["state"], "needs-question")
            self.assertEqual(record.call_count, 0)
            self.assertEqual(self.step(answers=answers)["state"], "released")
            self.assertEqual(self.step(answers=answers)["state"], "released")
            self.assertEqual(self.step(interview=self.question_file, answers=answers)["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])
        self.assertEqual(record.call_count, 1)
        interview, given = record.call_args.args
        self.assertEqual(interview["questions"][0]["id"], "q-scope")
        self.assertEqual(given["answers"]["q-scope"]["note"], "go on")

    def test_already_received_answers_register_and_release_without_reasking(self):
        result = self.step(interview=self.question_file, answers=self.answers())
        self.assertEqual(result["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_committed_answer_replay_does_not_reopen_or_write_a_sealed_cycle(self):
        import artifact_producer
        answer = self.answers()
        result = self.step(interview=self.question_file, answers=answer)
        with mock.patch.object(artifact_producer, "prepare_route_artifact_env", side_effect=AssertionError("sealed cycle")), \
             mock.patch.object(W, "_store_once", side_effect=AssertionError("sealed write")):
            self.assertEqual(self.step(answers=answer), result)
            self.assertEqual(self.step(interview=self.question_file), result)
        self.assertEqual(self.calls, ["gate", "release"])

    def test_lost_release_response_replays_exact_committed_answer(self):
        original = self.run_command
        def lose(argv, **kwargs):
            r = original(argv, **kwargs)
            if argv[2] == "release":
                return subprocess.CompletedProcess(argv, 70, "", "lost reply")
            return r
        with self.assertRaisesRegex(ValueError, "frame-release-pending"):
            W.frame_interview_step(self.route,self.path,self.jobs,interview=self.question_file,
                                  answers=self.answers(),run=lose)
        self.assertEqual(self.resolution()["status"], "proceed")
        self.assertEqual(self.step(answers=self.answers())["state"], "released")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_changed_answer_cannot_replace_a_committed_decision(self):
        answer = self.answers()
        self.step(interview=self.question_file, answers=answer)
        before = self.resolution()
        changed = json.loads(answer.read_text()); changed["understanding_confirmed"] = False
        changed["correction"] = "Change the scope."
        answer.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "frame-input-conflict"):
            self.step(answers=answer)
        self.assertEqual(self.resolution(), before)

    def test_invalid_or_foreign_answers_do_not_raise_a_gate(self):
        answer = self.answers()
        invalid = json.loads(answer.read_text()); invalid["route_id"] = "rt-foreign"
        answer.write_text(json.dumps(invalid))
        with self.assertRaisesRegex(ValueError, "frame-input-invalid"):
            self.step(interview=self.question_file, answers=answer)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.resolution()["status"], "not-raised")

    def test_stop_does_not_render_an_agreed_intent_or_start_anything(self):
        result = self.step(interview=self.question_file,answers=self.answers(),decision="stop")
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse((self.output / "shards/frame/intent.md").exists())
        self.assertEqual(self.step(answers=self.answers(),decision="stop")["state"], "cancelled")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_stop_without_answer_and_plain_resume_preserve_cancellation(self):
        self.step(interview=self.question_file)
        self.assertEqual(self.step(decision="stop")["state"], "cancelled")
        self.assertEqual(self.step()["state"], "cancelled")
        self.assertEqual(self.calls, ["gate", "release"])

    def test_revision_registers_next_round_without_replacing_the_first(self):
        answer=self.answers()
        result=self.step(interview=self.question_file,answers=answer,decision="revise")
        before=Path(result["interview_file"]).read_bytes()
        self.assertEqual(self.step()["state"], "needs-revision")
        question=result["interview_template"]
        self.assertEqual(question["round"],2)
        question["understanding"]="Run the two commands and keep both outputs in the report."
        self.question_file.write_text(json.dumps(question))
        second=self.step(interview=self.question_file)
        self.assertEqual(second["state"], "needs-question")
        self.assertEqual(self.resolution()["epoch"],2)
        response=second["answers_template"];response["understanding_confirmed"]=True
        answer.write_text(json.dumps(response))
        self.assertEqual(self.step(answers=answer)["state"],"released")
        self.assertEqual(Path(result["interview_file"]).read_bytes(),before)
        self.assertEqual(self.calls,["gate","release","gate","release"])

    def test_revision_limit_hands_back_without_an_unusable_next_command(self):
        import frame_interview as FI
        question=self.question
        for number in range(1,FI.MAX_ROUNDS+1):
            self.question_file.write_text(json.dumps(question))
            response=FI.answers_template({**question,"route_id":self.route["route_id"]})
            response["understanding_confirmed"]=True
            answer=self.base/"received-answer.json";answer.write_text(json.dumps(response))
            result=self.step(interview=self.question_file,answers=answer,decision="revise")
            question=result.get("interview_template")
        self.assertEqual(result["reason"],"frame-revision-round-limit")
        self.assertIsNone(question)
        self.assertEqual(self.resolution()["status"],"revise")
        self.assertEqual(self.step()["state"],"needs-attention")

    def test_interrupted_input_publication_leaves_no_partial_question_and_replays(self):
        import artifact_receipt
        with mock.patch.object(artifact_receipt.os, "link", side_effect=OSError("publication interrupted")):
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                self.step(interview=self.question_file)
        self.assertFalse((self.output / "shards/frame/round-1/interview.json").exists())
        self.assertEqual(self.resolution()["status"], "not-raised")
        self.assertEqual(self.step(interview=self.question_file)["state"], "needs-question")


GF = load("work_start_group_fixture", W.ROOT / "utilities/artifact_workflow_groups.test.py")


class GroupContextStartTest(GF.fixture.ProducerTestBase):
    """Actual compose/admit/start/producer seams with no live roots or workers."""
    _cycles = GF.WorkflowGroupsTest._cycles
    _proposal = staticmethod(GF.WorkflowGroupsTest._proposal)

    def setUp(self):
        super().setUp()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_ARTIFACT_")}
        patch = mock.patch.dict(os.environ, clean, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.members = self._cycles(2)
        self.campaign = self.members[0]["campaign"]
        plan = GF.W.prepare(self.root, self.campaign, self._proposal(self.members))
        GF.W.apply(self.root, plan)
        self.group = plan["document"]["groups"][0]["group_id"]
        self.context = {"campaign_id": self.campaign, "group_id": self.group}
        self.env = {"AGENT_ARTIFACT_CAMPAIGN_ID": self.campaign,
                    "AGENT_ARTIFACT_WORKFLOW_GROUP_ID": self.group}

    def compose(self, slug="group-followup", **kwargs):
        args = dict(capability="autopilot-code", capability_mode="dev", shape="direct", graph=None,
                    slug=slug, cwd=W.ROOT, artifact_root=self.root, campaign_key="workflow-test",
                    parent_cycle_id=self.members[0]["id"], parent_harness="codex", spec_read="fixture",
                    drift_verdict="within-spec", work_request={"text": "Continue the named goal.",
                                                               "owner_harness": "codex"})
        args.update(kwargs)
        route = GF.fixture.R.compose_route(**args)
        bound = GF.fixture.L.admit_runtime_route(self.root, route)
        return route, Path(bound.route_file)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and p.name != ".admission.lock"}

    def test_fresh_start_and_resume_preserve_explicit_context(self):
        with mock.patch.dict(os.environ, self.env):
            route, path = self.compose()
        # Read serialized route as a new caller does, after the source env is gone.
        route = json.loads(path.read_text())
        self.assertEqual(route["work_request"]["workflow_group_context"], self.context)
        GF.fixture.R.verify_route(route, W.ROOT)
        before_env = dict(os.environ)
        first = W.start_work(route, path, self.jobs)
        self.assertEqual(first["state"], "inline")
        self.assertEqual(first["artifact_env"]["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"], self.group)
        again = W.start_work(json.loads(path.read_text()), path, self.jobs)
        self.assertEqual(again["artifact_env"], first["artifact_env"])
        self.assertEqual(dict(os.environ), before_env)
        document = GF.W.verify(self.root, self.campaign)
        self.assertEqual(document["groups"], 1)
        declaration = json.loads(GF.W.declaration_path(self.root, self.campaign).read_text())
        self.assertEqual(len(declaration["groups"][0]["members"]), 3)

    def test_fresh_cli_process_starts_and_resumes_without_group_environment(self):
        with mock.patch.dict(os.environ, self.env):
            route, path = self.compose()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("AGENT_ARTIFACT_", "AGENT_DISPATCH_", "AGENT_ROUTE_", "AGENT_OWNER_ROUTE_"))}
        env.update(AGENT_HOME=str(W.ROOT), AGENT_DISPATCH_JOBS=str(self.jobs),
                   AGENT_DISPATCH_CALLER_HARNESS="codex")
        command = [sys.executable, str(W.ROOT / "utilities/capability-route.py"),
                   "start", "--route", str(path), "--jobs", str(self.jobs)]
        results = []
        for _ in range(2):
            run = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            result = json.loads(run.stdout)
            self.assertEqual(result["artifact_env"]["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"], self.group)
            results.append(result["artifact_env"])
        self.assertEqual(results[0], results[1])

    def test_parent_only_old_request_stays_ungrouped(self):
        route, path = self.compose()
        self.assertNotIn("workflow_group_context", route["work_request"])
        result = W.start_work(route, path, self.jobs)
        self.assertNotIn("AGENT_ARTIFACT_WORKFLOW_GROUP_ID", result["artifact_env"])

    def test_other_campaign_ambient_context_is_not_captured(self):
        with mock.patch.dict(os.environ, self.env):
            route, path = self.compose(campaign_key="independent-goal", parent_cycle_id=None)
        self.assertNotIn("workflow_group_context", route["work_request"])
        result = W.start_work(route, path, self.jobs)
        self.assertNotIn("AGENT_ARTIFACT_WORKFLOW_GROUP_ID", result["artifact_env"])
        self.assertNotEqual(result["artifact_env"]["AGENT_ARTIFACT_CAMPAIGN_ID"], self.campaign)

    def test_saved_context_cannot_override_different_campaign(self):
        route, path = self.compose(campaign_key="independent-goal", parent_cycle_id=None,
            work_request={"text": "task", "owner_harness": "codex", "workflow_group_context": self.context})
        before = self.snapshot()
        with self.assertRaisesRegex(GF.P.ProducerError, "workflow-group-campaign-mismatch"):
            W.start_work(route, path, self.jobs)
        self.assertEqual(self.snapshot(), before)

    def test_current_explicit_conflict_is_rejected_without_writes(self):
        with mock.patch.dict(os.environ, self.env):
            route, path = self.compose()
        before = self.snapshot()
        with mock.patch.dict(os.environ, {**self.env, "AGENT_ARTIFACT_WORKFLOW_GROUP_ID": "wgrp_" + "f" * 32}):
            with self.assertRaisesRegex(GF.P.ProducerError, "workflow-group-context-conflict"):
                W.start_work(route, path, self.jobs)
        self.assertEqual(self.snapshot(), before)

    def test_saved_context_is_hash_bound_and_closed_shape(self):
        with mock.patch.dict(os.environ, self.env):
            route, _ = self.compose()
        route["work_request"]["workflow_group_context"]["group_id"] = "wgrp_" + "f" * 32
        with mock.patch.dict(os.environ, {"HEARTING_GATES": "on"}):
            with self.assertRaisesRegex(ValueError, "modified route hash"):
                GF.fixture.R.verify_route(route, W.ROOT)
        for context in (None, {}, {**self.context, "guess": True},
                        {**self.context, "group_id": "title-derived"},
                        {**self.context, "campaign_id": "../campaign"}):
            with self.subTest(context=context), self.assertRaisesRegex(ValueError, "work-request-group-context-invalid"):
                W.validate_request({"text": "task", "owner_harness": None, "workflow_group_context": context})

    def test_registered_launch_preparation_uses_same_saved_context(self):
        with mock.patch.dict(os.environ, self.env):
            route, path = self.compose(shape="solo", registered_headless_evidence=GF.fixture.registered_headless())
        # The selector invokes this same producer seam before adapter launch.
        result = GF.P.prepare_route_artifact_env(path, start=True, jobs=self.jobs)
        self.assertEqual(result["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"], self.group)
        self.assertEqual(GF.P.prepare_route_artifact_env(path, start=False, jobs=self.jobs), result)


if __name__ == "__main__":
    unittest.main()
