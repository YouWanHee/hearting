#!/usr/bin/env python3
"""Regression tests for the native Codex queue courier."""

from __future__ import annotations

import importlib.util
import json
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


class PeerQueuePolicy(unittest.TestCase):
    def test_peer_ambiguous_add_is_not_retried(self):
        with mock.patch.object(QUEUE, "_known_consumed", return_value=None), \
             mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_rpc", side_effect=QUEUE.QueueDeliveryError("ambiguous", ambiguous=True)) as rpc:
            with self.assertRaises(QUEUE.QueueDeliveryError):
                QUEUE.send_at_least_once(SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
                    message="peer", allow_restart=False, retry_ambiguous=False)
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ["thread/queue/add"])

    def test_peer_never_forces_interrupted_turn_restart(self):
        with mock.patch.object(QUEUE, "_known_consumed", return_value=None), \
             mock.patch.object(QUEUE, "list_queue", return_value=[ITEM]), \
             mock.patch.object(QUEUE, "_rpc") as rpc:
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="peer", allow_restart=False)
        self.assertFalse(result["started_after_interrupt"])
        rpc.assert_not_called()

    def test_observation_only_cannot_add_after_lost_ack(self):
        with mock.patch.object(QUEUE, "_known_consumed", return_value=None), \
             mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_rpc") as rpc:
            with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "previously-unconfirmed"):
                QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                    client_message_id=CLIENT_ID, message="peer", allow_restart=False, allow_add=False)
        rpc.assert_not_called()


class StrictPeerHistory(unittest.TestCase):
    def test_history_failure_and_page_budget_never_add(self):
        for rpc_result in (QUEUE.QueueDeliveryError("history-unavailable"),
                           {"data": [], "nextCursor": "next"}):
            with self.subTest(rpc_result=rpc_result):
                kwargs = {"side_effect": rpc_result} if isinstance(rpc_result, Exception) else {"return_value": rpc_result}
                with mock.patch.object(QUEUE, "_rpc", **kwargs) as rpc:
                    with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "history-unavailable"):
                        QUEUE.send_at_least_once(SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
                            message="peer", allow_restart=False, retry_ambiguous=False, require_message_match=True)
                self.assertTrue(all(call.args[1] == "thread/turns/list" for call in rpc.call_args_list))

    def test_actual_schema_empty_text_elements_and_body_mismatch(self):
        for message, count in (("peer", 1), ("other", 0)):
            turn = {"id": "fixture-turn", "items": [{"type": "userMessage", "clientId": CLIENT_ID,
                              "content": [{"type": "text", "text": message, "text_elements": []}]}]}
            with mock.patch.object(QUEUE, "_rpc", return_value={"data": [turn]}) as rpc:
                if count:
                    result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                        client_message_id=CLIENT_ID, message="peer", require_message_match=True)
                    self.assertEqual(result["status"], "consumed")
                else:
                    with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "history-mismatch"):
                        QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                            client_message_id=CLIENT_ID, message="peer", require_message_match=True)
            self.assertEqual([c.args[1] for c in rpc.call_args_list], ["thread/turns/list"])

    def test_duplicate_client_or_changed_pending_content_is_unverified(self):
        for items in ([dict(ITEM, input=[{"type": "text", "text": "other"}])],
                      [dict(ITEM, input=[{"type": "text", "text": "peer"}])] * 2):
            with mock.patch.object(QUEUE, "_known_consumed", return_value=None), \
                 mock.patch.object(QUEUE, "list_queue", return_value=items), \
                 mock.patch.object(QUEUE, "_rpc") as rpc:
                with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "pending-mismatch"):
                    QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                        client_message_id=CLIENT_ID, message="peer", require_message_match=True)
                rpc.assert_not_called()


class BoundedPeerHistory(unittest.TestCase):
    def send(self):
        return QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
            client_message_id=CLIENT_ID, message="peer", timeout=3,
            allow_restart=False, retry_ambiguous=False, require_message_match=True)

    def test_large_aggregate_pages_complete_before_one_enqueue(self):
        # A real serialized-size fixture reproduces full-page overflow without
        # changing the socket cap; each individual turn is well below it.
        turns = [{"id": f"turn-{i}", "items": [{"type": "commandExecution",
                  "aggregatedOutput": "x" * (1024 * 1024)}]} for i in range(5)]
        reads, adds = [], []

        def rpc(path, method, params, *, timeout):
            if method == "thread/queue/add":
                adds.append(params)
                return {"queuedSubmission": ITEM}
            self.assertEqual(method, "thread/turns/list")
            self.assertEqual(params["threadId"], THREAD)
            self.assertEqual((params["itemsView"], params["sortDirection"]), ("full", "desc"))
            start = int(params.get("cursor", 0))
            end = min(len(turns), start + params["limit"])
            response = {"data": turns[start:end], "nextCursor": str(end) if end < len(turns) else None}
            size = len(json.dumps(response).encode())
            reads.append((start, params["limit"], size))
            if size > 4 * 1024 * 1024:
                raise QUEUE.QueueDeliveryError("queue-websocket-message-oversized")
            return response

        with mock.patch.object(QUEUE, "_rpc", side_effect=rpc), \
             mock.patch.object(QUEUE, "list_queue", return_value=[]):
            result = self.send()
        self.assertEqual(result["status"], "queued")
        self.assertEqual(len(adds), 1)
        self.assertEqual([limit for start, limit, _ in reads[:4]], [16, 8, 4, 2])
        self.assertEqual(sum(start == 2 for start, _, size in reads if size < 4 * 1024 * 1024), 2)
        self.assertEqual(QUEUE.MAX_FRAME_BYTES, 4 * 1024 * 1024)

    def item_rpc(self, tail, *, error=None):
        # The matching user item occurs after tools and the first user; summary
        # would miss it. A second matching item can occupy a later item page.
        items = [{"type": "userMessage", "clientId": "other", "content": []},
                 {"type": "commandExecution"},
                 {"type": "userMessage", "clientId": CLIENT_ID,
                  "content": [{"type": "text", "text": "peer"}]}] + tail
        calls = []

        def rpc(path, method, params, *, timeout):
            calls.append((method, params))
            self.assertEqual(params["threadId"], THREAD)
            if method == "thread/turns/list":
                self.assertEqual(params["sortDirection"], "desc")
                if params["itemsView"] == "full":
                    raise QUEUE.QueueDeliveryError("queue-websocket-message-oversized")
                self.assertEqual((params["itemsView"], params["limit"]), ("notLoaded", 1))
                return {"data": [{"id": "large-turn", "items": []}], "nextCursor": None}
            self.assertEqual(method, "thread/items/list")
            self.assertEqual((params["turnId"], params["sortDirection"]), ("large-turn", "asc"))
            if error:
                raise error
            start = int(params.get("cursor", 0))
            end = min(len(items), start + 2)
            return {"data": [{"turnId": "large-turn", "item": item} for item in items[start:end]],
                    "nextCursor": str(end) if end < len(items) else None}
        return rpc, calls

    def test_singleton_item_pagination_finds_middle_user_with_exact_body(self):
        rpc, calls = self.item_rpc([{"type": "agentMessage"}])
        with mock.patch.object(QUEUE, "_rpc", side_effect=rpc):
            result = self.send()
        self.assertEqual(result["status"], "consumed")
        self.assertEqual([params.get("cursor") for method, params in calls if method == "thread/items/list"], [None, "2"])

    def test_matching_item_requires_rest_of_turn_duplicate_check(self):
        duplicate = {"type": "userMessage", "clientId": CLIENT_ID,
                     "content": [{"type": "text", "text": "changed"}]}
        rpc, calls = self.item_rpc([{"type": "agentMessage"}, duplicate])
        with mock.patch.object(QUEUE, "_rpc", side_effect=rpc):
            with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "history-mismatch"):
                self.send()
        self.assertEqual([params.get("cursor") for method, params in calls if method == "thread/items/list"], [None, "2", "4"])

    def test_unsupported_item_api_and_single_item_overflow_never_enqueue(self):
        for reason in ("queue-rpc-refused:thread/items/list:-32601:unsupported",
                       "queue-websocket-message-oversized"):
            rpc, calls = self.item_rpc([], error=QUEUE.QueueDeliveryError(reason))
            with self.subTest(reason=reason), mock.patch.object(QUEUE, "_rpc", side_effect=rpc):
                with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "history-unavailable"):
                    self.send()
            self.assertNotIn("thread/queue/add", [method for method, _ in calls])

    def test_foreign_items_and_repeated_cursors_are_unavailable(self):
        for entries, cursor in (([{"turnId": "foreign", "item": {"type": "agentMessage"}}], None),
                                ([{"turnId": "large-turn", "item": {"type": "agentMessage"}}], "same"),
                                ([{"turnId": "large-turn", "item": None}], None)):
            rpc, calls = self.item_rpc([])
            def wrong(path, method, params, *, timeout):
                if method == "thread/items/list":
                    return {"data": entries, "nextCursor": cursor}
                return rpc(path, method, params, timeout=timeout)
            with self.subTest(entries=entries), mock.patch.object(QUEUE, "_rpc", side_effect=wrong):
                with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "history-unavailable"):
                    self.send()

    def test_turn_request_and_time_budgets_never_become_absence(self):
        def rpc(path, method, params, *, timeout):
            index = int(params.get("cursor", 0))
            return {"data": [{"id": f"turn-{index}", "items": []}], "nextCursor": str(index + 1)}
        for bounds in ({"MAX_QUEUE_PAGES": 1, "MAX_PAGE_SIZE": 2},
                       {"MAX_PEER_HISTORY_REQUESTS": 2}):
            with self.subTest(bounds=bounds), mock.patch.multiple(QUEUE, **bounds), \
                 mock.patch.object(QUEUE, "_rpc", side_effect=rpc) as calls:
                with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "history-budget-exhausted"):
                    self.send()
                self.assertTrue(all(call.args[1] == "thread/turns/list" for call in calls.call_args_list))
        with mock.patch.object(QUEUE.time, "monotonic", side_effect=[0, 100]), mock.patch.object(QUEUE, "_rpc") as calls:
            with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "history-budget-exhausted"):
                self.send()
            calls.assert_not_called()

    def test_item_budget_exhaustion_never_accepts_a_partial_match(self):
        rpc, _ = self.item_rpc([{"type": "agentMessage"}])
        with mock.patch.object(QUEUE, "MAX_PEER_HISTORY_ITEMS", 2), \
             mock.patch.object(QUEUE, "_rpc", side_effect=rpc):
            with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "item-history-budget-exhausted"):
                self.send()


if __name__ == "__main__":
    unittest.main()
