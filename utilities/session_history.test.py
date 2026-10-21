#!/usr/bin/env python3
import sys
import io
import contextlib
import tempfile
import unittest
from pathlib import Path
from argparse import Namespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_history as viewer


class HistoryTests(unittest.TestCase):
    def test_confirmed_predecessor_beats_chronological_neighbor(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "old"}},
            {"harness": "claude", "sid": "neighbor", "relation": "chronological"},
            {"harness": "claude", "sid": "old", "relation": "chronological"},
            {"harness": "claude", "sid": "unknown", "relation": "unknown"},
        ]
        entries, reason = viewer._entries_for_generation(rows, 1)
        self.assertFalse(reason)
        self.assertEqual([row["sid"] for row in entries], ["old", "neighbor", "unknown"])
        self.assertEqual([row["_label"] for row in entries], [
            "확정된 이전 대화", "시간순 이웃 · 인계 미확인", "관계 미확인"])

    def test_local_fallback_uses_raw_lineage_and_defaults_to_confirmed_predecessor(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "old"}},
            {"harness": "claude", "sid": "neighbor", "relation": "chronological"},
            {"harness": "claude", "sid": "old", "relation": "chronological"},
        ]
        seat = viewer.session_tidy.Seat("pane", "key", "pane", "claude")
        with patch.object(viewer, "_identity", return_value=(seat, "", "")), \
             patch.object(viewer.session_tidy, "seat_lineage", return_value=rows) as lineage, \
             patch.object(viewer, "_render", side_effect=lambda row: (row["sid"], None)), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(viewer.main(["--stdout"]), 0)
        lineage.assert_called_once_with(seat)
        self.assertEqual(output.getvalue().strip(), "old")

    def test_generation_walks_confirmed_then_lineage_order_and_labels(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "old"}},
            {"harness": "claude", "sid": "near-chronological", "relation": "chronological"},
            {"harness": "claude", "sid": "old", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "older-confirmed"}},
            {"harness": "claude", "sid": "older-confirmed", "relation": "chronological"},
            {"harness": "claude", "sid": "unknown", "relation": "unknown"},
        ]
        expected = [
            ("old", "확정된 이전 대화"),
            ("near-chronological", "시간순 이웃 · 인계 미확인"),
            ("older-confirmed", "시간순 이웃 · 인계 미확인"),
            ("unknown", "관계 미확인"),
        ]
        for generation, (sid, label) in enumerate(expected, 1):
            entries, reason = viewer._entries_for_generation(rows, generation)
            self.assertFalse(reason)
            self.assertEqual((entries[generation - 1]["sid"], entries[generation - 1]["_label"]),
                             (sid, label))
        self.assertIn("범위 밖", viewer._entries_for_generation(rows, len(expected) + 1)[1])

    def test_viewer_renders_missing_and_unreadable_status(self):
        missing = {"harness": "claude", "sid": "missing",
                   "transcript": {"path": "/missing/transcript"}}
        self.assertIn("원문 missing", viewer._render(missing)[0])
        with tempfile.TemporaryDirectory() as directory:
            unreadable = {"harness": "claude", "sid": "unreadable",
                          "transcript": {"path": directory}}
            self.assertIn("원문 unreadable", viewer._render(unreadable)[0])

    def test_detached_active_pane_uses_the_hook_seat_digest(self):
        from unittest.mock import patch
        env = {"HERDR_ACTIVE_PANE_ID": "workspace:active", "HERDR_PANE_ID": "workspace:other",
               "HERDR_SOCKET_PATH": "/tmp/herdr.sock"}
        endpoint = viewer.session_tidy._endpoint_scope({"HERDR_SOCKET_PATH": env["HERDR_SOCKET_PATH"]})
        expected = viewer.session_tidy._pane_seat_of(
            env["HERDR_ACTIVE_PANE_ID"], endpoint=endpoint, socket_path=env["HERDR_SOCKET_PATH"])
        with patch.dict("os.environ", env, clear=True):
            actual, pane, socket = viewer._identity(Namespace(harness=None, session_id=None))
        self.assertEqual((actual.key, pane, socket), (expected.key, "workspace:active", "/tmp/herdr.sock"))


if __name__ == "__main__":
    unittest.main()
