#!/usr/bin/env python3
"""Regression tests for the native Codex queue courier."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "codex_queue_delivery", HERE / "codex_queue_delivery.py"
)
QUEUE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = QUEUE
SPEC.loader.exec_module(QUEUE)


SOCKET = Path("/tmp/app-server.sock")
THREAD = "01a0e1c0-bd78-7a30-aea5-a7cf4f3356e3"
CLIENT_ID = "delivery-test-0001"
ITEM_ID = "01a0e1c1-fdb8-7190-8524-6812c28b3f9a"
ITEM = {"id": ITEM_ID, "clientUserMessageId": CLIENT_ID,
        "input": [{"type": "text", "text": "fixture"}]}


class QueueDeliveryTest(unittest.TestCase):
    def test_idle_queue_add_does_not_race_with_tui_wakeup(self):
        with mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(QUEUE, "_rpc", return_value={"queuedSubmission": ITEM}) as rpc:
            result = QUEUE.send_at_least_once(
                SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
                message="fixture",
            )
        self.assertEqual(result["queued_submission_id"], ITEM_ID)
        self.assertFalse(result["started_after_interrupt"])
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ["thread/queue/add"])

    def setUp(self):
        self.history = mock.patch.object(QUEUE, "find_turn_by_client_message_id", return_value=None).start()
        self.addCleanup(mock.patch.stopall)

    def test_pending_exact_item_restarts_without_resuming_or_taking_subscription(self):
        with mock.patch.object(QUEUE, "list_queue", return_value=[ITEM]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="interrupted"), \
             mock.patch.object(QUEUE, "_rpc", return_value={"turn": {"id": "turn-1"}}) as rpc:
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="fixture")
        self.assertTrue(result["already_pending"])
        self.assertTrue(result["started_after_interrupt"])
        self.assertEqual([c.args[1] for c in rpc.call_args_list], ["thread/queue/start"])
        self.assertEqual(rpc.call_args.args[2]["queuedSubmissionId"], ITEM_ID)

    def test_start_refusal_is_preserved_without_hidden_resume(self):
        with mock.patch.object(QUEUE, "list_queue", return_value=[ITEM]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="interrupted"), \
             mock.patch.object(QUEUE, "_rpc", side_effect=QUEUE.QueueDeliveryError("resume-required")) as rpc:
            with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "resume-required"):
                QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                    client_message_id=CLIENT_ID, message="fixture")
        self.assertEqual([c.args[1] for c in rpc.call_args_list], ["thread/queue/start"])

    def test_consumed_item_is_not_resent(self):
        self.history.return_value = {"id": "turn-consumed"}
        with mock.patch.object(QUEUE, "_rpc") as rpc:
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="fixture")
        self.assertEqual(result["status"], "consumed")
        rpc.assert_not_called()

    def test_consume_between_history_and_queue_list_is_not_resent(self):
        self.history.side_effect = [None, {"id": "turn-consumed"}]
        with mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_rpc") as rpc:
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="fixture")
        self.assertEqual(result["status"], "consumed")
        rpc.assert_not_called()

    def test_missing_or_duplicate_pending_identity_suppresses_send_and_start(self):
        for items in ([{"clientUserMessageId": CLIENT_ID}], [ITEM, ITEM]):
            with self.subTest(items=items), mock.patch.object(QUEUE, "list_queue", return_value=items), \
                 mock.patch.object(QUEUE, "_rpc") as rpc:
                result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                    client_message_id=CLIENT_ID, message="fixture")
                self.assertTrue(result["already_pending"])
                self.assertIsNone(result["queued_submission_id"])
                rpc.assert_not_called()

    def test_unavailable_history_does_not_prevent_queueing_or_claim_consumption(self):
        self.history.side_effect = QUEUE.QueueDeliveryError("queue-websocket-message-oversized")
        with mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(QUEUE, "_rpc", return_value={"queuedSubmission": ITEM}):
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="fixture")
        self.assertEqual(result["status"], "queued")

    def test_add_success_without_item_identity_is_accepted(self):
        with mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(QUEUE, "_rpc", return_value={}) as rpc:
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="fixture")
        self.assertEqual(result["status"], "queued")
        self.assertIsNone(result["queued_submission_id"])
        rpc.assert_called_once()

    def test_unrelated_user_queue_entry_is_never_started(self):
        user_item = {"id": "human-item", "clientUserMessageId": "human-id"}
        with mock.patch.object(QUEUE, "list_queue", return_value=[user_item]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="interrupted"), \
             mock.patch.object(QUEUE, "_rpc") as rpc:
            result = QUEUE.poll_owned_item(
                SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
            )
        self.assertEqual(result, {"pending": False, "started": False})
        rpc.assert_not_called()

    def test_ambiguous_add_lists_before_retry_and_skips_retry_when_pending(self):
        ambiguous = QUEUE.QueueDeliveryError("socket-dropped-after-send", ambiguous=True)
        with mock.patch.object(QUEUE, "list_queue", side_effect=[[], [ITEM], [ITEM]]) as listing, \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(QUEUE, "_rpc", side_effect=[ambiguous]) as rpc:
            result = QUEUE.send_at_least_once(
                SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
                message="fixture",
            )
        self.assertTrue(result["already_pending"])
        self.assertEqual(listing.call_count, 3)
        rpc.assert_called_once()

    def test_ambiguous_missing_item_is_retried_once(self):
        ambiguous = QUEUE.QueueDeliveryError("socket-dropped-after-send", ambiguous=True)
        with mock.patch.object(QUEUE, "list_queue", side_effect=[[], []]) as listing, \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(QUEUE, "_rpc", side_effect=[ambiguous, {"queuedSubmission": ITEM}]) as rpc:
            result = QUEUE.send_at_least_once(
                SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
                message="fixture",
            )
        self.assertEqual(result["queued_submission_id"], ITEM_ID)
        self.assertEqual(listing.call_count, 2)
        self.assertEqual([call.args[1] for call in rpc.call_args_list],
                         ["thread/queue/add", "thread/queue/add"])


class QueueHistoryBoundTest(unittest.TestCase):
    def test_long_history_does_not_block_a_new_receipt(self):
        with mock.patch.object(QUEUE, "MAX_QUEUE_PAGES", 2), \
             mock.patch.object(QUEUE, "_rpc", return_value={"data": [], "nextCursor": "older"}) as rpc:
            self.assertIsNone(QUEUE.find_turn_by_client_message_id(SOCKET, THREAD, CLIENT_ID))
        self.assertEqual(rpc.call_count, 2)
        self.assertTrue(all(c.args[2]["itemsView"] == "full" for c in rpc.call_args_list))


if __name__ == "__main__":
    unittest.main()
