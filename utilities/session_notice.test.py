#!/usr/bin/env python3
"""A notice about non-attempt work reaches its session through the shared store and renderer."""
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dispatch_session_sweep as sweep  # noqa: E402
import session_notice as N  # noqa: E402


class SessionNoticeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.jobs = Path(self._tmp.name) / "jobs.log"
        self.jobs.write_text("")

    def notify(self, harness="claude", key="run:host-1"):
        return N.notify(harness, "sid-1", key=key, subject="compute run host-1",
                        text="finished with exit 0; read it with compute-hosts tail host-1", jobs=self.jobs)

    def test_one_notice_is_delivered_once_and_rendered_in_its_own_block(self):
        first = self.notify()
        self.assertEqual(first["recipient_kind"], "claude-parent-runtime")
        self.assertEqual(self.notify()["delivery_id"], first["delivery_id"])  # same key, same record
        claimed, _count = sweep.sweep_deliver(self.jobs.parent, "claude-parent-runtime", "sid-1")
        self.assertEqual([record["delivery_id"] for record in claimed], [first["delivery_id"]])
        text = sweep.delivery_context([(self.jobs.parent, claimed)])
        self.assertTrue(text.startswith(sweep.NOTICE_DELIVERY_HEADER))
        self.assertIn("compute run host-1: finished with exit 0", text)
        self.assertNotIn(sweep.COMPLETION_DELIVERY_HEADER, text)
        self.assertEqual(sweep.ack_delivered(self.jobs.parent, "sid-1", claimed, acked_by="test"), 1)
        self.assertEqual(sweep.sweep_deliver(self.jobs.parent, "claude-parent-runtime", "sid-1")[0], [])

    def test_each_harness_uses_its_declared_carrier(self):
        self.assertEqual(self.notify("codex", "k-codex")["recipient_kind"], "codex-native-queue")
        self.assertEqual(self.notify("opencode", "k-oc")["recipient_kind"], "opencode-turn")
        self.assertIsNone(self.notify("other", "k-other"))
        self.assertIsNone(N.notify("claude", "", key="k", subject="s", text="t", jobs=self.jobs))

    def test_writer_retains_idempotent_notice_duty_and_pending_survives_runner_failure(self):
        import peer_obligations
        env = {"HOME": str(self.jobs.parent / "home"),
               "XDG_STATE_HOME": str(self.jobs.parent / "state"),
               "AGENT_DISPATCH_JOBS": str(self.jobs)}
        with mock.patch.dict("os.environ", env), \
             mock.patch.object(peer_obligations, "ensure_normal_runner", return_value=False) as ensure:
            first = self.notify()
            second = self.notify()
            duties = peer_obligations.ObligationStore(self.jobs.parent).list(
                kinds={"session-notice"})
        self.assertEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(len(duties), 1)
        identity = duties[0]["intent"]["identity"]
        self.assertEqual(identity["storage_sid"], "sid-1")
        self.assertEqual(identity["harness"], "claude")
        self.assertEqual(identity["delivery_id"], first["delivery_id"])
        self.assertEqual(identity["receipt_digest"], first["receipt_digest"])
        self.assertNotIn("recipient_sid", identity)
        self.assertEqual(ensure.call_count, 2)

    def test_runner_failure_does_not_remove_the_pending_notice(self):
        import dispatch_pending_delivery as pending
        import peer_obligations
        env = {"HOME": str(self.jobs.parent / "home"),
               "XDG_STATE_HOME": str(self.jobs.parent / "state"),
               "AGENT_DISPATCH_JOBS": str(self.jobs)}
        with mock.patch.dict("os.environ", env), \
             mock.patch.object(peer_obligations, "ensure_normal_runner",
                               side_effect=OSError("runner unavailable")):
            record = self.notify(key="run:durable")
            stored = pending.read(self.jobs.parent, "sid-1", record["delivery_id"])
        self.assertEqual(stored["state"], "pending")
        self.assertEqual(stored["receipt_digest"], record["receipt_digest"])

    def test_a_malformed_notice_is_never_shown(self):
        with self.assertRaises(ValueError):
            N.validate({"kind": "notice"})
        self.assertEqual(N.render_text({"kind": "notice"}), "notice=unreadable")


if __name__ == "__main__":
    unittest.main()
