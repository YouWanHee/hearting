#!/usr/bin/env python3
"""H6/H7: stale obligations are retired, real completion and gates survive."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import dispatch_notice_state as notice
import dispatch_pending_delivery as pending
import dispatch_session_sweep as sweep
import dispatch_supervision as supervision
from route_identity import route_hash
from workflow_state import WorkflowLedger


class NoticeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.artifact = self.root / "review.json"
        self.artifact.write_text("{}")
        self.path = self.root / "route.json"
        self.route = {"route_id": "rt-notice", "nodes": [{"id": "frame",
            "continuation": {"kind": "human-gate", "gate": "review"}}]}
        self.route["route_hash"] = route_hash(self.route)
        self.path.write_text(json.dumps(self.route))
        self.meta = dict(attempt_id="att-owner", route_id="rt-notice", route_node="frame",
                         route_file=str(self.path), route_hash=self.route["route_hash"],
                         parent_sid="parent", parent_completion_delivery="claude-parent-runtime")
        self.write_row()

    def write_row(self):
        self.jobs.write_text("now\tdone\t/r\t/w\ttask\t" + ",".join(k+"="+v for k,v in self.meta.items()) + "\n")

    def close(self, proven=True):
        self.path.with_suffix(".outcome.json").write_text(json.dumps(dict(
            route_id="rt-notice", route_hash=self.route["route_hash"], terminal_gate_proven=proven)))

    def seed(self, action="inspect-done-failure"):
        receipt = {"schema_version": 2, "state": "attention", "parent_attempt_id": "att-owner",
            "job_registry": str(self.jobs), "delivery_classification": "attention", "children": [{
                "attempt_id": "att-owner", "status": "done", "readiness": "ready", "reason": str(self.artifact),
                "required_action": action, "harness": "claude", "delivery_classification": "attention"}]}
        return pending.create(self.root, delivery_id="delivery-notice", recipient_kind="claude-parent-runtime",
            recipient_key="parent", session_generation="", session_generation_supported="0",
            attempt_ids=["att-owner"], parent_attempt_id="att-owner", route_id="rt-notice", route_node="frame",
            receipt=receipt, receipt_digest=pending._canonical_receipt_digest(receipt), row_revisions={"att-owner": "1"})

    def journal(self, record, *, state="BLOCKED_HUMAN_GATE", delivery=None):
        path = WorkflowLedger("rt-notice", self.route["route_hash"], jobs=self.jobs).journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"workflow_state": state, "evidence": {"gate": "review",
            "artifact": str(self.artifact), "delivery": delivery or str(pending.record_path(
                self.root, "parent", record["delivery_id"]))}}) + "\n")
        return path

    def test_closed_blocked_route_stops_repeated_sd111_continue_request(self):
        self.seed()
        self.close(proven=False)
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])
        record = pending.read(self.root, "parent", "delivery-notice")
        self.assertEqual(record["state"], "rejected")

    def test_closed_success_still_delivers_first_completion(self):
        self.seed("advance-completed")
        self.close()
        records, _ = sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")
        self.assertEqual(len(records), 1)
        self.assertEqual(sweep.ack_delivered(self.root, "parent", records, acked_by="test"), 1)
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])

    def test_bad_closure_is_unknown_and_preserves_claim_for_recovery(self):
        self.seed()
        self.path.with_suffix(".outcome.json").write_text('{"route_id":"another"}')
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])
        self.assertEqual(pending.read(self.root, "parent", "delivery-notice")["state"], "claimed")

    def test_closed_route_retires_exited_supervisor_during_pending_settlement(self):
        self.meta["workflow_completion"] = "runtime-v1"
        self.write_row()
        record = supervision.materialize(self.jobs, {"att-owner"}, reason="supervisor-exited")[0]
        self.close()
        with mock.patch("dispatch_terminal_commit.owner_completion_pending", return_value=True):
            self.assertFalse(notice.notice_is_current(record))
            # A real finishing failure is not silenced by an early close.
            blocked = supervision.materialize(self.jobs, {"att-owner"}, reason="closure-blocked")[0]
            self.assertTrue(notice.notice_is_current(blocked))

    def test_gate_needs_latest_exact_raise_but_can_outlive_owner(self):
        record = self.seed("human-gate:review")
        self.journal(record)
        self.assertTrue(notice.notice_is_current(record))  # owner row is already done
        path = self.journal(record, delivery="/another/delivery.json")
        self.assertFalse(notice.notice_is_current(record))
        self.journal(record)
        with path.open("a") as stream:
            stream.write(json.dumps({"workflow_state": "RUNNING", "evidence": {"released_gate": "review"}}) + "\n")
        self.assertFalse(notice.notice_is_current(record))
        self.journal(record)
        self.close()
        self.assertFalse(notice.notice_is_current(record))

    def test_corrupt_gate_journal_is_not_silently_consumed(self):
        record = self.seed("human-gate:review")
        self.journal(record).write_text("corrupt\n")
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])
        self.assertEqual(pending.read(self.root, "parent", "delivery-notice")["state"], "claimed")

    def test_missing_gate_artifact_defers_without_discarding_obligation(self):
        record = self.seed("human-gate:review")
        self.journal(record)
        self.artifact.unlink()
        self.assertEqual(sweep.sweep_deliver(self.root, "claude-parent-runtime", "parent")[0], [])
        self.assertEqual(pending.read(self.root, "parent", "delivery-notice")["state"], "claimed")


if __name__ == "__main__":
    unittest.main()
