#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
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
        ]
        with patch.object(viewer.session_tidy, "seat_lineage", return_value=rows):
            entries, reason = viewer._entries_for_generation(rows, 1)
        self.assertFalse(reason)
        self.assertEqual(entries[0]["sid"], "old")
        self.assertIn("범위 밖", viewer._entries_for_generation(rows, 2)[1])

    def test_lineage_generation_range_is_checked(self):
        rows = [{"harness": "claude", "sid": "current", "relation": "confirmed",
                 "predecessor": {"harness": "claude", "sid": "prior"}},
                {"harness": "claude", "sid": "prior", "relation": "chronological"},
                {"harness": "claude", "sid": "grandparent", "relation": "chronological"}]
        rows[1]["relation"] = "confirmed"
        rows[1]["predecessor"] = {"harness": "claude", "sid": "grandparent"}
        entries, reason = viewer._entries_for_generation(rows, 2)
        self.assertFalse(reason)
        self.assertEqual(entries[1]["sid"], "grandparent")
        self.assertIn("범위 밖", viewer._entries_for_generation(rows, 4)[1])

    def test_detached_active_pane_uses_the_hook_seat_digest(self):
        from argparse import Namespace
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
