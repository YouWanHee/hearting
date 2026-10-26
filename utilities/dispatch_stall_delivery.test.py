#!/usr/bin/env python3
"""Closed-loop fixtures for advisory attempt stalls and exact parent receipts."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location("peer_steward_stall_fixture",
                                              ROOT / "utilities/peer-steward.py")
peer_steward = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(peer_steward)

import dispatch_contract
import dispatch_pending_delivery as pending
import dispatch_supervision as supervision
import peer_obligations
from harness_capabilities import parent_completion
from fleet.collectors.dispatch import _attempt_stall_attention
from fleet.model import DispatchJob


class StallDeliveryLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stall-delivery-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs.log"
        self.attempt = "att-stall-loop"
        self.parent = "idle-parent-fixture"

    def _row(self, harness):
        carrier = parent_completion(harness)["carrier"]
        metadata = {
            "attempt_schema_version": "2", "dispatch_depth": "2",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "worker_type": "stage", "harness": harness,
            "route_id": "rt-stall", "route_hash": "sha256:" + "a" * 64,
            "route_node": "execute", "attempt_id": self.attempt,
            "parent_sid": self.parent, "parent_harness": harness,
            "parent_completion_delivery": carrier, "managed_sealed_batch_id": "",
            "pid_ns": dispatch_contract.process_namespace_identity() or "",
            "pid_observer_ns": dispatch_contract.process_namespace_identity() or "",
            "pid_scope": "host-visible",
        }
        self.jobs.write_text("2026-10-26T00:00:00Z\topen\t/repo\t" + str(self.root)
            + "\texecute\t" + ",".join(f"{key}={value}" for key, value in metadata.items()) + "\n",
            encoding="utf-8")
        return metadata, carrier

    def _materialize_stall(self, harness):
        metadata, carrier = self._row(harness)
        with mock.patch.dict(os.environ, {
                "XDG_STATE_HOME": str(self.root / "xdg-state"),
                "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state")}), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            observation = supervision.update_attempt_stall_observation(
                self.jobs, self.attempt, now=100.0, row=("open", metadata),
                worktree=str(self.root))
        self.assertEqual(observation["state"], "stalled")
        before_registry = self.jobs.read_bytes()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg-state"),
                                          "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state")}), \
             mock.patch.object(dispatch_contract, "attempt_execution_observation",
                return_value=dispatch_contract.AttemptExecutionObservation(
                    "absent", "carrier-only")), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
             mock.patch("peer_obligations.ensure_normal_runner"):
            records = supervision.materialize(self.jobs, {self.attempt}, reason="attempt-stalled")
        self.assertEqual(self.jobs.read_bytes(), before_registry)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["recipient_kind"], carrier)
        self.assertEqual(record["recipient_digest"], pending.recipient_digest(self.parent))
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["receipt"]["monitored_attempt_ids"], [self.attempt])
        with mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            self.assertTrue(supervision.notice_is_current(record))
        duty_id = peer_obligations.stable_duty_id("registered-batch", {
            "jobs": str(self.jobs.resolve()), "attempt_id": self.attempt,
            "session_id": self.parent, "harness": harness, "server": "default",
        }, record["delivery_id"])
        duty = peer_obligations.ObligationStore(self.jobs.parent).get(duty_id)
        self.assertEqual(duty["intent"]["notice_delivery_id"], record["delivery_id"])
        self.assertEqual(duty["intent"]["notice_digest"], record["receipt_digest"])
        return metadata, carrier, record, duty

    def test_observation_retention_courier_exact_receive_and_recovery_for_each_adapter(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory(
                    prefix=f"stall-{harness}-") as root:
                self.root = Path(root)
                self.jobs = self.root / "jobs.log"
                self.attempt = "att-stall-loop-" + harness
                self.parent = "idle-parent-" + harness
                metadata, carrier, record, duty = self._materialize_stall(harness)
                store = peer_obligations.ObligationStore(self.jobs.parent)
                stall_state_path = self.root / "watchdog" / f"{self.attempt}.json"
                initial_episode = json.loads(stall_state_path.read_text(encoding="utf-8"))[
                    "stall_observation"]["episode_id"]
                expected = (harness, self.parent)
                fleet_job = DispatchJob(key="code", route_id=metadata["route_id"],
                    route_hash=metadata["route_hash"], route_node=metadata["route_node"],
                    attempt_id=self.attempt, parent_sid=self.parent,
                    worker_type="stage", status="open", liveness="working")
                fleet_job._registry_path = str(self.jobs)
                fleet_job._launch_home = str(self.root)
                census = {"result": {"agents": [{"agent": harness, "pane_id": "w1:p5",
                    "agent_session": {"value": self.parent}}]}}
                peer_root = self.root / "peer-state"

                def receive(args, *, expected_recipient, on_prepared, producer_binding):
                    self.assertEqual(expected_recipient, expected)
                    body = Path(args.body_file).read_text(encoding="utf-8")
                    sender = {"harness": "runtime", "session_id": None,
                              "name": "Hearting runtime"}
                    recipient = {"harness": harness, "session_id": self.parent}
                    text, ref = peer_steward.peer_message.prepare_peer_message(
                        body, sender, recipient, defer=True, refs=list(args.ref),
                        producer_binding=producer_binding)
                    on_prepared(ref)
                    self.assertEqual(peer_steward.peer_message.receive_peer_message(
                        text, recipient), 0)
                    return 0

                with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(peer_root),
                                                  "XDG_STATE_HOME": str(self.root / "xdg-state")}), \
                     mock.patch.object(dispatch_contract, "attempt_execution_observation",
                        return_value=dispatch_contract.AttemptExecutionObservation(
                            "absent", "carrier-only")), \
                     mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
                     mock.patch.object(peer_steward.subprocess, "run",
                        return_value=SimpleNamespace(stdout=json.dumps(census), returncode=0)), \
                     mock.patch.object(peer_steward, "_resolve_target", return_value=expected), \
                     mock.patch.object(peer_steward, "cmd_prompt", side_effect=receive), \
                     mock.patch.object(peer_steward, "_settle_claude_native_dispatch_receipt",
                        return_value=False):
                    self.assertTrue(supervision.notice_is_current(record))
                    state_path = self.root / "watchdog" / f"{self.attempt}.json"
                    before_fleet = state_path.read_bytes()
                    self.assertEqual(_attempt_stall_attention(fleet_job)["delivery_id"],
                                     record["delivery_id"])
                    self.assertEqual(state_path.read_bytes(), before_fleet)
                    peer_steward._resume_registered_obligation(duty, store)
                    transferred = (store.get(duty["id"]).get("observation") or {}).get("transfer_ref")
                    self.assertTrue(transferred)
                    exact = pending.read(self.jobs.parent, self.parent, record["delivery_id"])
                    self.assertEqual(exact["state"], "acked")
                    self.assertEqual(exact["receipt_digest"], record["receipt_digest"])
                    self.assertEqual((exact.get("receipt") or {}).get("reason"), "attempt-stalled")
                    body = supervision.render_text(record["receipt"])
                    self.assertIn("no exact runtime/command process", body)
                    self.assertIn("dispatch-registry.py reconcile --jobs", body)
                    peer_steward._resume_registered_obligation(store.get(duty["id"]), store)
                    self.assertEqual(store.get(duty["id"])["state"], "complete")

                    # ACK proves the receiver consumed this advisory, not that the
                    # attempt recovered. A fresh observation of progress clears it.
                    self.assertTrue(supervision.notice_is_current(record))
                    with mock.patch.object(dispatch_contract, "attempt_execution_observation",
                            return_value=dispatch_contract.AttemptExecutionObservation(
                                "active", "runtime-command-live")), \
                         mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
                        current = supervision.update_attempt_stall_observation(
                            self.jobs, self.attempt, now=200.0, row=("open", metadata),
                            worktree=str(self.root))
                        self.assertEqual(current["state"], "active")
                        self.assertFalse(supervision.notice_is_current(record))
                        self.assertIsNone(_attempt_stall_attention(fleet_job))
                    with mock.patch.object(dispatch_contract, "attempt_execution_observation",
                            return_value=dispatch_contract.AttemptExecutionObservation(
                                "absent", "carrier-only")), \
                         mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
                         mock.patch("peer_obligations.ensure_normal_runner"):
                        new_episode = supervision.update_attempt_stall_observation(
                            self.jobs, self.attempt, now=300.0, row=("open", metadata),
                            worktree=str(self.root))
                        next_notice = supervision.materialize(
                            self.jobs, {self.attempt}, reason="attempt-stalled")[0]
                    self.assertEqual(new_episode["state"], "stalled")
                    self.assertNotEqual(new_episode["episode_id"], initial_episode)
                    self.assertNotEqual(next_notice["delivery_id"], record["delivery_id"])
                    self.assertEqual(self.jobs.read_text(encoding="utf-8").split("\t")[1], "open")

    def test_normal_observer_reentry_discovers_sidecarless_open_attempt_once(self):
        metadata, carrier = self._row("codex")
        before = self.jobs.read_bytes()
        execution = dispatch_contract.AttemptExecutionObservation("absent", "carrier-only")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "xdg-state"),
                                          "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state")}), \
             mock.patch.object(dispatch_contract, "attempt_execution_observation",
                return_value=execution), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
             mock.patch("peer_obligations.ensure_normal_runner"):
            first = supervision.update_open_attempt_stall_observations(self.jobs)
            second = supervision.update_open_attempt_stall_observations(self.jobs)
            signature = supervision.stall_observer_signature(self.jobs)
        self.assertEqual(len(first), 1)
        self.assertEqual([item["delivery_id"] for item in first],
                         [item["delivery_id"] for item in second])
        self.assertEqual(len(signature), 1)
        self.assertEqual(signature[0][0], self.attempt)
        self.assertEqual(self.jobs.read_bytes(), before)
        duties = peer_obligations.ObligationStore(self.jobs.parent).list(
            states={"pending"}, kinds={"registered-batch"})
        self.assertEqual(len(duties), 1)
        self.assertEqual(duties[0]["intent"]["carrier"], carrier)
        with mock.patch.object(supervision, "_attempt_human_wait", return_value=False):
            self.assertTrue(supervision.has_unowned_stall_observation(self.jobs))

    def test_recovery_after_claim_is_rechecked_before_any_parent_send(self):
        import dispatch_pending_delivery as queue
        metadata, _carrier, record, duty = self._materialize_stall("codex")
        store = peer_obligations.ObligationStore(self.jobs.parent)
        current = {"execution": dispatch_contract.AttemptExecutionObservation(
            "absent", "carrier-only")}
        census = {"result": {"agents": [{"agent": "codex", "pane_id": "w1:p5",
            "agent_session": {"value": self.parent}}]}}
        original_claim = queue.claim_ready_batch
        sends = []

        def claim_then_recover(*args, **kwargs):
            claimed = original_claim(*args, **kwargs)
            current["execution"] = dispatch_contract.AttemptExecutionObservation(
                "active", "runtime-command-live")
            supervision.update_attempt_stall_observation(
                self.jobs, self.attempt, now=200.0, row=("open", metadata),
                worktree=str(self.root))
            return claimed

        with mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-state"),
                                          "XDG_STATE_HOME": str(self.root / "xdg-state")}), \
             mock.patch.object(dispatch_contract, "attempt_execution_observation",
                               side_effect=lambda _meta: current["execution"]), \
             mock.patch.object(supervision, "_attempt_human_wait", return_value=False), \
             mock.patch.object(peer_steward.subprocess, "run",
                return_value=SimpleNamespace(stdout=json.dumps(census), returncode=0)), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("codex", self.parent)), \
             mock.patch.object(queue, "claim_ready_batch", side_effect=claim_then_recover), \
             mock.patch.object(peer_steward, "cmd_prompt", side_effect=lambda *a, **k:
                sends.append((a, k)) or 0), \
             mock.patch.object(peer_steward.peer_message, "bind_dispatch_notice"), \
             mock.patch.object(peer_steward.peer_message, "prepare_peer_message",
                side_effect=AssertionError("stale notice must not prepare a transfer")):
            peer_steward._resume_registered_obligation(duty, store)
            peer_steward._resume_registered_obligation(store.get(duty["id"]), store)

        self.assertEqual(sends, [])
        latest = pending.read(self.jobs.parent, self.parent, record["delivery_id"])
        self.assertEqual(latest["state"], "rejected")
        self.assertEqual(store.get(duty["id"])["state"], "complete")


if __name__ == "__main__":
    unittest.main()
