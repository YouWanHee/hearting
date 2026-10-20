import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.fleet import render
from tools.fleet.explorer.output import cell_width


class ExplorerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.saved = (render._EXPLORER_ENABLED, render._EXPLORER_STATE,
                      render._EXPLORER_FRAME, render._EXPLORER_NAV_ROWS,
                      render._RELOAD_FRAME, render._RELOAD_LAYOUT, render._SELECTABLE)

    def tearDown(self):
        (render._EXPLORER_ENABLED, render._EXPLORER_STATE, render._EXPLORER_FRAME,
         render._EXPLORER_NAV_ROWS, render._RELOAD_FRAME, render._RELOAD_LAYOUT,
         render._SELECTABLE) = self.saved

    def test_compose_path_does_not_call_legacy_line_builder(self):
        snapshot = SimpleNamespace(sessions=(), jobs=(), resources=(), usage_snapshots={},
                                   display_snapshot=None)
        with patch.object(render, "_build_lines", side_effect=AssertionError("legacy renderer")), \
                patch.object(render, "_usage_header_rows", return_value=[]), \
                patch.object(render, "_compute_host_rows", return_value=[]):
            frame = render._explorer_compose(snapshot, 100, 45)
        self.assertEqual(frame.geometry, (100, 45))
        self.assertEqual(len(frame.lines), 45)
        self.assertTrue(all(cell_width(line) == 100 for line in frame.lines))
        self.assertIn("세션", "\n".join(frame.lines))

    def test_kill_targets_are_limited_to_nav_session_and_worker_rows(self):
        session = SimpleNamespace(session_id="s1", harness="claude", cwd="/repo", title="owner",
                                  status="busy", liveness="working", pid=101, proc_start=1,
                                  slug="owner", app_server=False, is_child=False, mem_worker=False)
        child = SimpleNamespace(job_id="j1", parent_sid="s1", title="worker", status="busy",
                                liveness="working", pid=102, proc_start=2, cwd="/repo", slug="job",
                                harness="claude", source="jobs", is_child=True)
        unrelated = SimpleNamespace(job_id="j2", parent_sid="elsewhere", title="other",
                                    status="busy", liveness="working", pid=103, proc_start=3,
                                    cwd="/repo", slug="other", harness="claude", source="jobs",
                                    is_child=True)
        snapshot = SimpleNamespace(sessions=(session,), jobs=(child, unrelated), resources=(),
                                   usage_snapshots={}, display_snapshot=None)
        with patch.object(render, "_usage_header_rows", return_value=[]), \
                patch.object(render, "_compute_host_rows", return_value=[]), \
                patch("tools.fleet.control.is_excluded", return_value=False):
            render._explorer_compose(snapshot, 168, 45)
            self.assertEqual({row["pid"] for row in render._live_targets()}, {101, 102})

    def test_handoff_schema_two_restores_and_invalidates_legacy_frame(self):
        render.set_explorer_mode(True)
        state = {"schema_version": 2, "selection": "session:claude:s1", "tab": "cycle-1",
                 "active_region": "nav", "nav_fold": ["project:/repo"], "scrolls": {"nav": 3},
                 "nav_visible": True, "events_visible": True, "tab_order": ["cycle-1"],
                 "frame_layout": {"schema_version": 2}}
        render.restore_viewer_state({"explorer_state": state,
                                     "frame": [[("old group frame", None)]],
                                     "frame_layout": {"schema_version": 1}})
        saved = render.viewer_state(render._LiveOrderState())
        self.assertEqual(saved["explorer_state"]["selection"], "session:claude:s1")
        self.assertEqual(saved["frame_layout"]["schema_version"], 2)
        self.assertIsNone(saved["frame"])


if __name__ == "__main__":
    unittest.main()
