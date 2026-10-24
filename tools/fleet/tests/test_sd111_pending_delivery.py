#!/usr/bin/env python3
"""SD-111 P6 -- fleet `pending`/`expired` visibility. Read-only observer:
these tests never call anything that transitions a pending-delivery record or
a registry row. Hermetic (temp trees only), stdlib unittest.

Runnable directly or via `python3 -m unittest discover -s tools/fleet/tests
-t . -p 'test_sd111*'`.
"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
_UTILITIES_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "utilities")
if _UTILITIES_DIR not in sys.path:
    sys.path.insert(0, _UTILITIES_DIR)

from fleet.collectors import dispatch  # noqa: E402
import dispatch_pending_delivery as pending_delivery  # noqa: E402


def _record(**overrides):
    receipt = {
        "schema_version": 2,
        "state": "delivered",
        "parent_attempt_id": "att-0000000000000000000000000000aaaa",
        "job_registry": "/tmp/sd111p6/jobs.log",
        "children": [
            {
                "attempt_id": "att-0000000000000000000000000000bbbb",
                "status": "done",
                "readiness": "ready",
                "reason": "terminal-failure-or-unclosed",
                "required_action": "inspect-done-failure",
                "harness": "claude",
                "delivery_classification": "attention",
            }
        ],
        "delivery_classification": "attention",
    }
    base = dict(
        root=None,
        recipient_kind="claude-parent-runtime",
        recipient_key="sess-p6",
        delivery_id="delivery-" + "a" * 32,
        session_generation="",
        session_generation_supported="0",
        attempt_ids=["att-0000000000000000000000000000bbbb"],
        parent_attempt_id="att-0000000000000000000000000000aaaa",
        route_id="rt-example",
        route_node="execute",
        receipt=receipt,
        receipt_digest=pending_delivery._canonical_receipt_digest(receipt),
        row_revisions={"att-0000000000000000000000000000bbbb": "deadbeef"},
    )
    base.update(overrides)
    return base


class PendingDeliveryCountsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sd111-p6-")
        self.state_root = Path(self._tmp.name) / "state"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.jobs_path = self.state_root / "jobs.log"
        self.jobs_path.write_text("", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_jobs_row(self, meta: dict[str, str]) -> None:
        pipe = ",".join(f"{k}={v}" for k, v in meta.items())
        line = f"2026-08-28T00:00:00Z\tdone\trepo\t-\tslug\t{pipe}\n"
        with open(self.jobs_path, "a", encoding="utf-8") as handle:
            handle.write(line)

    def test_no_records_no_intent_rows_returns_zero(self):
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 0, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_materialized_pending_record_counted_once(self):
        pending_delivery.create(**_record(root=self.state_root))
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 1, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_notice_remains_visible_without_any_parent_session(self):
        from fleet import render
        record = pending_delivery.create(**_record(root=self.state_root))
        before = pending_delivery.read(self.state_root, "sess-p6", record["delivery_id"])
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        for process_view in (False, True):
            with mock.patch.object(render, "_PROCESS_VIEW", process_view):
                lines = render._build_lines([], [], "all", False, 0, term_width=80,
                                            pending_delivery=counts)
                text = "\n".join("".join(segment[0] for segment in line) for line in lines if line)
                self.assertIn("delivery pending: 1", text)
                self.assertIn("awaiting recipient", text)
        self.assertEqual(pending_delivery.read(self.state_root, "sess-p6", record["delivery_id"]), before)
        pending_delivery.claim(self.state_root, "sess-p6", record["delivery_id"],
                               claim_owner="test", lease_seconds=30, require_generation_proof=False)
        pending_delivery.ack(self.state_root, "sess-p6", record["delivery_id"], acked_by="prompt")
        lines = render._build_lines([], [], "all", False, 0, term_width=80,
            pending_delivery=dispatch._pending_delivery_counts([str(self.jobs_path)]))
        self.assertNotIn("delivery pending", str(lines))

    def test_expired_record_counted_separately_and_never_deleted(self):
        record = pending_delivery.create(**_record(root=self.state_root))
        pending_delivery.expire_if_due(
            self.state_root, "sess-p6", record["delivery_id"],
            actor="dispatch-reconcile", reason="recipient-session-gone",
            liveness="known",
        )
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 0, "expired": 1, "retained_batch_duties": 0,
                                  "no_recipient": None})
        # A-9 visibility clause: the file still exists on disk.
        record_path = pending_delivery.record_path(
            self.state_root, "sess-p6", record["delivery_id"]
        )
        self.assertTrue(record_path.is_file())

    def test_intent_stamped_row_without_a_materialized_record_is_counted(self):
        # The crash window between the terminal commit and trigger 1/2: the
        # row carries delivery_intent but no pending-delivery/ record file
        # exists yet anywhere on disk. Omitting this term would make the
        # completion silently invisible (plan §1.6-1, §9 R-9).
        self._write_jobs_row({
            "attempt_id": "att-intent-only",
            "delivery_intent": "1",
            "parent_sid": "sess-p6",
            "delivery_id": "delivery-" + "b" * 32,
        })
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 1, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_intent_row_and_its_own_materialized_record_counted_once_not_twice(self):
        record = pending_delivery.create(**_record(root=self.state_root))
        self._write_jobs_row({
            "attempt_id": "att-0000000000000000000000000000bbbb",
            "delivery_intent": "1",
            "parent_sid": "sess-p6",
            "delivery_id": record["delivery_id"],
        })
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 1, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_enumeration_failure_fails_open_missing_directory(self):
        missing_jobs = self.state_root / "does-not-exist" / "jobs.log"
        counts = dispatch._pending_delivery_counts([str(missing_jobs)])
        self.assertEqual(counts, {"pending": 0, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_corrupt_record_file_skipped_not_raised(self):
        pending_root = self.state_root / "pending-delivery" / ("f" * 64)
        pending_root.mkdir(parents=True, exist_ok=True)
        (pending_root / "delivery-broken.json").write_text("not json", encoding="utf-8")
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 0, "expired": 0, "retained_batch_duties": 0,
                                  "no_recipient": None})

    def test_retained_batch_duties_counted_while_complete_ones_are_not(self):
        obligations = self.state_root / "supervisor-state" / "obligations"
        obligations.mkdir(parents=True, exist_ok=True)
        for duty_id, state in (("batch-" + "a" * 32, "observing"),
                               ("batch-" + "b" * 32, "delivery-pending"),
                               ("batch-" + "c" * 32, "unknown"),
                               ("batch-" + "d" * 32, "complete")):
            (obligations / f"{duty_id}.json").write_text(
                json.dumps({"schema_version": 1, "id": duty_id, "state": state}),
                encoding="utf-8")
        (obligations / "batch-broken.json").write_text("not json", encoding="utf-8")
        counts = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertEqual(counts, {"pending": 0, "expired": 0, "retained_batch_duties": 3,
                                  "no_recipient": None})

    def test_collect_stashes_counts_on_the_module_like_last_malformed(self):        # A-9/A-21 visibility clause: `collect()` (fleet's one read-only
        # entry point, F-27's control module is the only fleet surface with
        # write authority) stashes the pending-delivery counts on itself the
        # same additive way it already stashes last_malformed/
        # last_route_nodes -- no return-signature change, never a gate.
        pending_delivery.create(**_record(root=self.state_root))
        try:
            with mock.patch.object(dispatch, "_scan_processes", return_value=[]):
                dispatch.collect(jobs_path=str(self.jobs_path))
            self.assertEqual(
                dispatch.collect.last_pending_delivery,
                {"pending": 1, "expired": 0, "retained_batch_duties": 0,
                 "no_recipient": None}
            )
        finally:
            dispatch.collect.last_pending_delivery = None

    def _count_with_graph(self, storage_sid, recipient_sid, live=(), duplicate_paths=False):
        record = pending_delivery.create(**_record(root=self.state_root,
                                                    recipient_key=storage_sid))
        path = pending_delivery.record_path(self.state_root, storage_sid, record["delivery_id"])
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        graph = SimpleNamespace(ledger_keys={("claude", storage_sid): ()})
        paths = [str(self.jobs_path), str(self.jobs_path)] if duplicate_paths else [str(self.jobs_path)]
        with mock.patch("dispatch_seat_handover.lineage_graph", return_value=graph), \
                mock.patch("dispatch_seat_handover.record_recipient",
                           return_value=recipient_sid):
            counts = dispatch._pending_delivery_counts(
                paths, session_observation={"complete": True, "live": set(live)})
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        return counts

    def test_dead_storage_and_confirmed_dead_chain_end_count_once(self):
        counts = self._count_with_graph("dead-A", "dead-C", duplicate_paths=True)
        self.assertEqual(counts["no_recipient"], 1)
        self.assertEqual(counts["pending"], 1)

    def test_live_successor_and_live_untidied_storage_are_excluded(self):
        successor = self._count_with_graph("dead-A", "live-C", live={("claude", "live-C")})
        self.assertEqual(successor["no_recipient"], 0)

    def test_live_untidied_storage_is_excluded(self):
        untidied = self._count_with_graph("live-A", "live-A", live={("claude", "live-A")})
        self.assertEqual(untidied["no_recipient"], 0)

    def test_unresolved_recipient_and_missing_census_remain_unknown(self):
        unresolved = self._count_with_graph("dead-A", None)
        self.assertIsNone(unresolved["no_recipient"])
        pending_delivery.create(**_record(root=self.state_root))
        missing = dispatch._pending_delivery_counts([str(self.jobs_path)])
        self.assertIsNone(missing["no_recipient"])

    def test_render_and_overview_show_known_count_but_never_unknown_as_zero(self):
        from fleet import render
        from fleet.explorer.overview import build_overview
        known = {"pending": 2, "expired": 0, "no_recipient": 1}
        with mock.patch.object(render, "_PROCESS_VIEW", False):
            lines = render._delivery_pending_rows(known)
            text = "\n".join(segment[0] for line in lines for segment in line)
        self.assertIn("받을 세션 없음 1", text)
        overview = "\n".join(build_overview({"pending_delivery": known}))
        self.assertIn("받을 세션 없음 1", overview)
        unknown = {"pending": 2, "expired": 0, "no_recipient": None}
        self.assertNotIn("no recipient", str(render._delivery_pending_rows(unknown)))
        self.assertNotIn("받을 세션 없음", "\n".join(build_overview({"pending_delivery": unknown})))


if __name__ == "__main__":
    unittest.main()
