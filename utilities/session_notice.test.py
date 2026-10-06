#!/usr/bin/env python3
"""A notice about non-attempt work reaches its session through the shared store and renderer."""
import sys
import tempfile
import unittest
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

    def test_a_malformed_notice_is_never_shown(self):
        with self.assertRaises(ValueError):
            N.validate({"kind": "notice"})
        self.assertEqual(N.render_text({"kind": "notice"}), "notice=unreadable")


if __name__ == "__main__":
    unittest.main()
