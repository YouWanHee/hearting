#!/usr/bin/env python3
"""Focused tests for native receipt identity and exact settlement."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dispatch_pending_delivery as pending
import dispatch_recipient_receipt as receipt


class RecipientReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="recipient-receipt-")
        self.root = Path(self.tmp.name) / "dispatch"
        self.storage = "session-original"
        self.session = "session-recipient"
        self.delivery_id = "delivery-" + "a" * 32
        body = {"kind": "supervision", "owner_attempt_id": "att-" + "b" * 28,
                "reason": "waiting", "target_attempts": ["att-" + "c" * 28]}
        self.record = pending.create(
            self.root, recipient_kind="claude-parent-runtime", recipient_key=self.storage,
            delivery_id=self.delivery_id, session_generation="g1",
            session_generation_supported="1", attempt_ids=["att-" + "c" * 28],
            parent_attempt_id="att-" + "b" * 28, route_id="route-test", route_node="execute",
            receipt=body, receipt_digest=pending._canonical_receipt_digest(body),
            row_revisions={"att-" + "c" * 28: "rev-1"})

    def tearDown(self):
        self.tmp.cleanup()

    def _mark_sent_ambiguous(self):
        pending.claim(self.root, self.storage, self.delivery_id,
                      claim_owner="test-carrier", lease_seconds=30,
                      require_generation_proof=False)
        pending.mark_sent_ambiguous(self.root, self.storage, self.delivery_id,
                                    claim_owner="test-carrier")
        self.record = pending.read(self.root, self.storage, self.delivery_id)

    def test_binding_is_exact_to_immutable_identity(self):
        marker = receipt.receipt_marker(self.record, self.storage)
        self.assertEqual(marker, receipt.receipt_marker(self.record, self.storage))
        changed = dict(self.record, route_node="other")
        self.assertNotEqual(marker, receipt.receipt_marker(changed, self.storage))

    def test_native_translators_require_exact_session_and_native_user_events(self):
        marker = receipt.receipt_marker(self.record, self.storage)
        messages = [
            {"info": {"sessionID": self.session, "role": "assistant"},
             "parts": [{"type": "text", "text": marker}]},
            {"info": {"sessionID": "foreign", "role": "user"},
             "parts": [{"type": "text", "text": marker}]},
            {"info": {"sessionID": self.session, "role": "user"},
             "parts": [{"type": "text", "text": marker}]},
        ]
        evidence = receipt.opencode_messages_evidence(messages, self.session)
        self.assertEqual(evidence["bindings"], [(self.delivery_id, marker.split()[-1])])
        self.assertEqual(evidence["source"], "opencode-session-messages")
        claude = receipt.claude_feedback_evidence([
            {"type": "assistant", "sessionId": self.session,
             "summary": "Stop hook feedback", "message": {"content": marker}},
            {"type": "user", "sessionId": "foreign",
             "summary": "Stop hook feedback", "message": {"content": marker}},
            {"type": "user", "sessionId": self.session, "summary": "user prompt",
             "message": {"content": marker}},
            {"type": "user", "sessionId": self.session,
             "summary": "Stop hook feedback", "message": {"content": marker}},
        ], self.session)
        self.assertEqual(claude["bindings"], [(self.delivery_id, marker.split()[-1])])

    def test_exact_receipt_settles_delivery_only(self):
        self._mark_sent_ambiguous()
        marker = receipt.receipt_marker(self.record, self.storage)
        evidence = receipt.opencode_messages_evidence([
            {"info": {"sessionID": self.session, "role": "user"},
             "parts": [{"type": "text", "text": marker}]},
        ], self.session)
        result = receipt.settle_native_receipt(
            self.root, self.storage, "opencode", self.session, self.record,
            evidence, acked_by="opencode-session:" + self.session)
        self.assertEqual(result, {"received": True, "settled": True,
                                  "reason": "native-receipt-settled"})
        self.assertEqual(pending.read(self.root, self.storage, self.delivery_id)["state"], "acked")
        # Settlement is only a pending-delivery transition. The receipt schema
        # and its unresolved owner state are untouched.
        current = pending.read(self.root, self.storage, self.delivery_id)
        self.assertEqual(current["receipt"]["reason"], "waiting")
        self.assertEqual(current["receipt"]["owner_attempt_id"], self.record["receipt"]["owner_attempt_id"])

    def test_ack_write_failure_still_reports_received_and_does_not_resend(self):
        self._mark_sent_ambiguous()
        marker = receipt.receipt_marker(self.record, self.storage)
        evidence = receipt.opencode_messages_evidence([
            {"info": {"sessionID": self.session, "role": "user"},
             "parts": [{"type": "text", "text": marker}]},
        ], self.session)
        with mock.patch.object(pending, "ack_received", side_effect=OSError("simulated write failure")):
            first = receipt.settle_native_receipt(
                self.root, self.storage, "opencode", self.session, self.record,
                evidence, acked_by="opencode-session:" + self.session)
        self.assertTrue(first["received"])
        self.assertFalse(first["settled"])
        self.assertEqual(first["reason"], "native-receipt-ack-write-failed")
        self.assertEqual(pending.read(self.root, self.storage, self.delivery_id)["state"],
                         "sent-ambiguous")
        second = receipt.settle_native_receipt(
            self.root, self.storage, "opencode", self.session, self.record,
            evidence, acked_by="opencode-session:" + self.session)
        self.assertTrue(second["received"])
        self.assertTrue(second["settled"])

    def test_foreign_or_stale_identity_cannot_suppress_delivery(self):
        marker = receipt.receipt_marker(self.record, self.storage)
        foreign = receipt.opencode_messages_evidence([
            {"info": {"sessionID": "other", "role": "user"},
             "parts": [{"type": "text", "text": marker}]},
        ], self.session)
        result = receipt.settle_native_receipt(
            self.root, self.storage, "opencode", self.session, self.record,
            foreign, acked_by="test")
        self.assertFalse(result["received"])
        mismatched = {**self.record, "attempt_ids": ["att-" + "d" * 28]}
        stale = receipt.settle_native_receipt(
            self.root, self.storage, "opencode", self.session, mismatched,
            receipt.opencode_messages_evidence([
                {"info": {"sessionID": self.session, "role": "user"},
                 "parts": [{"type": "text", "text": marker}]},
            ], self.session), acked_by="test")
        self.assertFalse(stale["received"])
        self.assertEqual(pending.read(self.root, self.storage, self.delivery_id)["state"], "pending")


if __name__ == "__main__":
    unittest.main()
