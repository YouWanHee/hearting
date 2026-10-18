#!/usr/bin/env python3
"""Isolated exact-batch regression for settled completion and late cleanup notices."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_completion_join as join
import dispatch_contract
import dispatch_pending_delivery as pending
import dispatch_session_sweep as sweep
import dispatch_supervision as supervision


class ExactCompletionSequenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.recipient = "codex-isolated-parent"
        self.attempts = ["att-code", "att-ship", "att-spec"]
        parent = "parent-session-" + hashlib.sha256(self.recipient.encode()).hexdigest()
        self.receipt = {
            "schema_version": 2, "state": "ready", "parent_attempt_id": parent,
            "job_registry": str(self.jobs), "delivery_classification": "success",
            "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
                "reason": "registry-closed", "required_action": "advance-completed",
                "harness": "codex", "delivery_classification": "success"}
                for attempt in self.attempts],
        }
        digest = pending._canonical_receipt_digest(self.receipt)
        lines = []
        self.delivery_ids = {}
        for index, (attempt, node) in enumerate(zip(self.attempts, ("code", "ship", "spec")), start=1):
            delivery_id = "delivery-sequence-" + str(index)
            self.delivery_ids[attempt] = delivery_id
            revision = "revision-" + str(index)
            metadata = {
                "attempt_schema_version": "2", "dispatch_depth": "1", "transport": "headless",
                "execution_surface": "registered-headless", "registered_worker": "1",
                "launch_claimed": "1", "launch_started": "1", "worker_type": "stage",
                "attempt_id": attempt, "parent_attempt_id": "-", "route_id": "rt-sequence",
                "route_node": node, "route_hash": "sha256:sequence", "harness": "codex",
                "parent_sid": self.recipient, "parent_completion_delivery": "codex-native-queue",
                "delivery_intent": "1", "delivery_id": delivery_id,
                "delivery_receipt_digest": digest, "delivery_row_revision": revision,
                "delivery_recipient_kind": "codex-native-queue", "note": "completed-marker",
                "launch_outcome": "completed",
            }
            lines.append("2026-10-19T00:00:00Z\tdone\t/repo\t/wt\t" + node + "\t"
                         + ",".join(f"{key}={value}" for key, value in metadata.items()))
        self.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.completion_times = {}
        for index, attempt in enumerate(self.attempts, start=1):
            delivery_id = self.delivery_ids[attempt]
            revision = "revision-" + str(index)
            record = pending.create(self.root, recipient_kind="codex-native-queue",
                recipient_key=self.recipient, delivery_id=delivery_id,
                session_generation="", session_generation_supported="0", attempt_ids=[attempt],
                parent_attempt_id="-", route_id="rt-sequence",
                route_node=("code", "ship", "spec")[index - 1], receipt=self.receipt,
                receipt_digest=digest, row_revisions={attempt: revision})
            pending.claim(self.root, self.recipient, delivery_id,
                          claim_owner="isolated-sequence", lease_seconds=30)
            pending.record_send(self.root, self.recipient, delivery_id,
                                source="codex-prompt-context-rendered", event_id="turn-1")
            received = pending.ack(self.root, self.recipient, delivery_id,
                                   acked_by="codex-native-prompt:" + self.recipient)
            self.completion_times[attempt] = received["timestamp_provenance"]

    def test_late_cleanup_and_join_deadline_do_not_reopen_an_exact_received_batch(self):
        original_jobs = self.jobs.read_bytes()
        chronology = [
            {"stage": "code-original", "verdict": "FAIL", "skipped": ["independent review"]},
            {"stage": "code-correction", "verdict": "PASS", "skipped": []},
            {"stage": "deployment-fixture", "verdict": "PASS", "operating_deployment": "unmeasured"},
            {"stage": "independent-review-fixture", "verdict": "PASS"},
            {"stage": "documentation", "verdict": "complete"},
            {"stage": "cleanup", "state": "unknown"},
            {"stage": "late-join-deadline", "state": "observed"},
            {"stage": "same-completion-redelivery", "state": "duplicate"},
        ]
        history = self.root / "pipeline-history.json"
        history.write_text(json.dumps(chronology, sort_keys=True) + "\n", encoding="utf-8")
        original_history = history.read_bytes()
        recovered = join.recover_acknowledged_batch_outcome(
            self.jobs, set(self.attempts), recipient_sid=self.recipient,
            parent_attempt_id=self.receipt["parent_attempt_id"],
            parent_completion_delivery="codex-native-queue")
        self.assertEqual(recovered, self.receipt)
        notice = supervision.materialize(self.jobs, {self.attempts[0]}, reason="join-deadline")[0]
        delivered_text = sweep._bounded_receipt_text(notice)
        self.assertIn("retains cleanup observation only", delivered_text)
        self.assertIn("Do not restart the goal", delivered_text)
        self.assertIn("ask the user whether to wait or cancel", delivered_text)
        self.assertIn("first observed send=", delivered_text)
        self.assertIn("exact receipt=", delivered_text)
        self.assertIn("notice record materialized", delivered_text)
        self.assertNotIn("run the route's start command", delivered_text)
        self.assertEqual(self.jobs.read_bytes(), original_jobs)
        self.assertEqual(json.loads(history.read_text(encoding="utf-8")), chronology)
        self.assertEqual(history.read_bytes(), original_history)

        # The exact receipt is idempotent; a duplicate materialization does not
        # change its first-send/receipt timestamps or create another message.
        original_record = pending.read(self.root, self.recipient,
                                       self.delivery_ids[self.attempts[0]])
        duplicate = pending.create(self.root, recipient_kind="codex-native-queue",
            recipient_key=self.recipient, delivery_id=original_record["delivery_id"],
            session_generation="", session_generation_supported="0", attempt_ids=[self.attempts[0]],
            parent_attempt_id="-", route_id="rt-sequence", route_node="code", receipt=self.receipt,
            receipt_digest=original_record["receipt_digest"],
            row_revisions={self.attempts[0]: "revision-1"})
        self.assertEqual(duplicate["timestamp_provenance"], original_record["timestamp_provenance"])

    def test_unacked_child_or_new_terminal_conflict_keeps_recovery_visible(self):
        record = pending.read(self.root, self.recipient, self.delivery_ids[self.attempts[-1]])
        original_read = pending.read

        def partial_read(root, recipient, delivery_id):
            value = original_read(root, recipient, delivery_id)
            if delivery_id == self.delivery_ids[self.attempts[-1]]:
                return dict(value, state="pending")
            return value

        with mock.patch.object(pending, "read", side_effect=partial_read):
            self.assertIsNone(join.recover_acknowledged_batch_outcome(
                self.jobs, set(self.attempts), recipient_sid=self.recipient,
                parent_attempt_id=self.receipt["parent_attempt_id"],
                parent_completion_delivery="codex-native-queue"))
        with mock.patch("dispatch_contract.terminal_conflict_pending", return_value=True):
            self.assertIsNone(join.recover_acknowledged_batch_outcome(
                self.jobs, set(self.attempts), recipient_sid=self.recipient,
                parent_attempt_id=self.receipt["parent_attempt_id"],
                parent_completion_delivery="codex-native-queue"))


if __name__ == "__main__":
    unittest.main()
