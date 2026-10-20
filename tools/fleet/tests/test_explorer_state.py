import unittest

from tools.fleet.explorer.state import ExplorerState, reconcile, reduce_key, restore, serialize


class ExplorerStateTests(unittest.TestCase):
    def test_schema_two_round_trip_and_frame_layout_version(self):
        state = ExplorerState(selection="session:cl:s1", tab="req-b", stage="execute",
                              active_region="output", nav_fold=("project:/repo",),
                              scrolls=(("req-b", 4),), nav_visible=False,
                              events_visible=False, search="build", tab_order=("req-a", "req-b"))
        payload = serialize(state)
        self.assertEqual(payload["frame_layout"]["schema_version"], 2)
        self.assertEqual(restore(payload), state)

    def test_schema_one_selection_folds_and_offset_are_translated(self):
        state = restore({"process_view": "group", "offset": 17,
                         "folds": {"/repo": True, "/other": False},
                         "cursor": {"_entry_id": "claude:s1"},
                         "frame_layout": {"schema_version": 1}})
        self.assertEqual(state.selection, "claude:s1")
        self.assertEqual(state.nav_fold, ("/repo",))
        self.assertEqual(state.scroll_map()["nav"], 17)

    def test_reconcile_uses_nearest_surviving_neighbor(self):
        state = ExplorerState(tab="gone", tab_order=("first", "gone", "third"))
        updated = reconcile(state, [{"identity": "third"}, {"identity": "first"}])
        self.assertEqual(updated.tab, "third")

    def test_reducer_keeps_identity_and_independent_scrolls(self):
        state = ExplorerState(selection="session:x", tab="req-x", active_region="output")
        moved = reduce_key(state, {"key": "down"})
        moved = reduce_key(moved, {"key": "right"})
        expanded = reduce_key(moved, {"key": "e"})
        resized = reduce_key(expanded, {"key": "resize", "cols": 168})
        self.assertEqual(moved.scroll_map()["req-x"], 1)
        self.assertEqual(moved.active_region, "events")
        self.assertEqual((resized.selection, resized.tab), ("session:x", "req-x"))

    def test_narrow_resize_starts_collapsed_and_enter_toggles_fold(self):
        state = reduce_key(ExplorerState(selection="project:/repo"),
                           {"key": "resize", "cols": 80})
        self.assertFalse(state.nav_visible)
        folded = reduce_key(state, "enter")
        self.assertIn("project:/repo", folded.nav_fold)


if __name__ == "__main__":
    unittest.main()
