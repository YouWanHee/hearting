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
            {"harness": "claude", "sid": "old", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "older"}},
            {"harness": "claude", "sid": "older", "relation": "unknown"},
            {"harness": "claude", "sid": "neighbor", "relation": "chronological"},
            {"harness": "claude", "sid": "unknown", "relation": "unknown"},
        ]
        entries, reason = viewer._entries_for_generation(rows, 1)
        self.assertFalse(reason)
        self.assertEqual([row["sid"] for row in entries], ["old", "older", "neighbor", "unknown"])
        self.assertEqual([row["_label"] for row in entries], [
            "확정된 이전 대화", "확정된 이전 대화", "관계 미확인", "시간순 이웃 · 인계 미확인"])

    def test_local_fallback_uses_raw_lineage_and_defaults_to_confirmed_predecessor(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "old"}},
            {"harness": "claude", "sid": "old", "relation": "unknown"},
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
            {"harness": "claude", "sid": "old", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "older-confirmed"}},
            {"harness": "claude", "sid": "older-confirmed", "relation": "unknown"},
            {"harness": "claude", "sid": "unknown", "relation": "unknown"},
        ]
        expected = [
            ("old", "확정된 이전 대화"),
            ("older-confirmed", "확정된 이전 대화"),
            ("unknown", "관계 미확인"),
        ]
        for generation, (sid, label) in enumerate(expected, 1):
            entries, reason = viewer._entries_for_generation(rows, generation)
            self.assertFalse(reason)
            self.assertEqual((entries[generation - 1]["sid"], entries[generation - 1]["_label"]),
                             (sid, label))
        self.assertIn("범위 밖", viewer._entries_for_generation(rows, len(expected) + 1)[1])

    def test_chronological_fallback_uses_current_rows_relation(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "chronological"},
            {"harness": "claude", "sid": "old", "relation": "unknown"},
        ]
        entries, reason = viewer._entries_for_generation(rows, 1)
        self.assertFalse(reason)
        self.assertEqual((entries[0]["sid"], entries[0]["_label"]),
                         ("old", "시간순 이웃 · 인계 미확인"))

    def test_projected_succession_defaults_to_its_real_predecessor(self):
        rows = [
            {"event": "start", "harness": "claude", "sid": "A", "ts": 1,
             "first_seen": 1, "source": "startup"},
            {"event": "start", "harness": "claude", "sid": "B", "ts": 2,
             "first_seen": 2, "source": "clear"},
            {"event": "succession", "harness": "claude", "sid": "B", "from": "A",
             "source": "tidy-clear", "ts": 2.1},
        ]
        lineage = viewer.session_tidy._project_seat_lineage(rows, {}, {}, sid="B")
        self.assertEqual(lineage[0]["relation"], "confirmed")
        self.assertEqual(lineage[0]["predecessor"], {"harness": "claude", "sid": "A"})
        entries, reason = viewer._entries_for_generation(lineage, 1)
        self.assertFalse(reason)
        self.assertEqual((entries[0]["sid"], entries[0]["_label"]),
                         ("A", "확정된 이전 대화"))

    def test_distinct_handover_and_succession_predecessors_remain_unknown(self):
        rows = [
            {"event": "start", "harness": "claude", "sid": "A", "ts": 1, "first_seen": 1},
            {"event": "start", "harness": "claude", "sid": "C", "ts": 2, "first_seen": 2},
            {"event": "start", "harness": "claude", "sid": "B", "ts": 3, "first_seen": 3},
            {"event": "handover", "harness": "claude", "sid": "B", "from": "A"},
            {"event": "succession", "harness": "claude", "sid": "B", "from": "C"},
        ]
        lineage = viewer.session_tidy._project_seat_lineage(rows, {}, {}, sid="B")
        self.assertEqual(lineage[0]["relation"], "unknown")
        self.assertIsNone(lineage[0]["predecessor"])

    def test_in_pane_status_and_range_messages_use_the_pager(self):
        with patch.object(viewer, "_show", return_value=False) as show:
            viewer._display([], 1)
            self.assertIn("연결 미확인", show.call_args.args[0])
            viewer._display([{"harness": "claude", "sid": "new", "relation": "chronological"},
                             {"harness": "claude", "sid": "old", "relation": "unknown"}], 3)
            self.assertIn("범위 밖", show.call_args.args[0])
        self.assertEqual(show.call_count, 2)

    def test_herdr_viewer_replaces_the_shell_with_exec(self):
        seat = viewer.session_tidy.Seat("pane", "key", "pane-id", "claude", "",
                                        endpoint="endpoint", socket_path="/tmp/herdr.sock")
        result = type("PaneResult", (), {"pane": "viewer-pane", "placement": "tab"})()
        with patch.object(viewer, "_identity", return_value=(seat, "pane-id", "/tmp/herdr.sock")), \
             patch.object(viewer, "open_pane", return_value=result) as open_pane:
            self.assertEqual(viewer.main([]), 0)
        argv = open_pane.call_args.args[0]
        self.assertEqual(argv[0], "exec")
        self.assertIn("--in-pane", argv)

    def test_split_viewer_focuses_the_new_pane_through_the_bound_runner(self):
        seat = viewer.session_tidy.Seat("pane", "key", "origin", "claude", "",
                                        endpoint="endpoint", socket_path="/tmp/herdr.sock")
        calls = []

        result = type("PaneResult", (), {"pane": "viewer", "placement": "split"})()

        def run(command, **kwargs):
            calls.append((command, kwargs["env"].get("HERDR_SOCKET_PATH")))
            return viewer.subprocess.CompletedProcess(command, 0, "{}", "")

        with patch.object(viewer, "_identity", return_value=(seat, "origin", "/tmp/herdr.sock")), \
             patch.object(viewer.HerdrSurface, "layout", return_value={"panes": [
                 {"pane_id": "origin", "rect": {"x": 0, "y": 0, "width": 50, "height": 40}},
                 {"pane_id": "viewer", "rect": {"x": 50, "y": 0, "width": 50, "height": 40}},
             ]}), \
             patch.object(viewer, "open_pane", return_value=result), \
             patch.object(viewer.subprocess, "run", side_effect=run):
            self.assertEqual(viewer.main([]), 0)
        self.assertEqual(calls, [(["herdr", "pane", "focus", "--pane", "origin",
                                   "--direction", "right"], "/tmp/herdr.sock")])

    def test_tab_viewer_does_not_attempt_to_focus_a_split(self):
        seat = viewer.session_tidy.Seat("pane", "key", "origin", "claude", "",
                                        endpoint="endpoint", socket_path="/tmp/herdr.sock")
        calls = []
        result = type("PaneResult", (), {"pane": "viewer", "placement": "tab"})()

        def runner(command):
            calls.append(command)
            return type("Process", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

        with patch.object(viewer, "_identity", return_value=(seat, "origin", "/tmp/herdr.sock")), \
             patch.object(viewer, "HerdrSurface", side_effect=lambda runner: object()), \
             patch.object(viewer, "open_pane", return_value=result):
            self.assertEqual(viewer.main([]), 0)
        self.assertEqual(calls, [])

    def test_pager_menu_reports_generation_boundaries_and_clears_screen(self):
        rows = [
            {"harness": "claude", "sid": "new", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "old"}},
            {"harness": "claude", "sid": "old", "relation": "confirmed",
             "predecessor": {"harness": "claude", "sid": "older"}},
            {"harness": "claude", "sid": "older", "relation": "unknown"},
        ]
        with patch.object(viewer, "_show", return_value=True), \
             patch("builtins.input", side_effect=["p", "q"]) as prompt, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            viewer._display(rows, 2, clear_before_prompt=True)
        self.assertIn("더 앞 세대가 없습니다", prompt.call_args_list[1].args[0])
        self.assertEqual(output.getvalue(), "\x1b[2J\x1b[H\x1b[2J\x1b[H")
        with patch.object(viewer, "_show", return_value=True), \
             patch("builtins.input", side_effect=["n", "q"]) as prompt:
            viewer._display(rows, 1, clear_before_prompt=True)
        self.assertIn("더 최근 세대가 없습니다", prompt.call_args_list[1].args[0])

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
