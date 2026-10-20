import dataclasses
import tempfile
import unittest

from tools.fleet.explorer.frame import compose
from tools.fleet.explorer.nav import build_nav, visible_rows
from tools.fleet.explorer.state import (ExplorerState, coerce, initial_state, reconcile, reduce_key,
                                        restore, serialize)
from tools.fleet.tests.explorer_fixture import collect, materialize, scene_result, tab, load_screens


class Persistence(unittest.TestCase):
    def test_schema_two_round_trip_and_frame_layout_version(self):
        state = ExplorerState(selection="session:cl:s1", tab="req-b", stage="execute",
                              active_region="output", nav_fold=("project:/repo",),
                              scrolls=(("output:req-b", 4),), nav_visible=False,
                              events_visible=False, search="build", tab_order=("req-a", "req-b"),
                              nav_open=("project:cairn",), keep=("req-a",), history=("req-b",))
        payload = serialize(state)
        self.assertEqual(payload["frame_layout"]["schema_version"], 2)
        self.assertEqual(restore(payload), state)
        self.assertEqual(coerce(payload), state)

    def test_schema_one_selection_folds_and_offset_are_translated(self):
        state = restore({"process_view": "group", "offset": 17,
                         "folds": {"/repo": True, "/other": False},
                         "cursor": {"_entry_id": "claude:s1"},
                         "frame_layout": {"schema_version": 1}})
        self.assertEqual(state.selection, "claude:s1")
        self.assertEqual(state.nav_fold, ("/repo",))
        self.assertEqual(state.scroll_map()["nav"], 17)
        self.assertEqual(state.active_region, "nav")
        self.assertTrue(state.nav_visible)

    def test_older_hand_over_without_cursor_or_folds_still_restores(self):
        for payload in (None, {}, {"process_view": "process"}, {"repo_folds": ["/repo"]}, "garbage"):
            state = restore(payload)
            self.assertEqual(state.tab, "")
            self.assertEqual(state.active_region, "nav")
        self.assertEqual(restore({"repo_folds": ["/repo"]}).nav_fold, ("/repo",))

    def test_a_vanished_tab_moves_to_the_nearest_surviving_neighbor_then_overview(self):
        state = ExplorerState(tab="gone", tab_order=("first", "gone", "third"))
        self.assertEqual(reconcile(state, [{"identity": "third"}, {"identity": "first"}]).tab, "third")
        self.assertEqual(reconcile(state, [{"identity": "first"}]).tab, "first")
        self.assertEqual(reconcile(state, []).tab, "gone")
        self.assertEqual(reconcile(ExplorerState(tab=""), [{"identity": "first"}]).tab, "")

    def test_below_100_columns_the_navigation_starts_folded(self):
        self.assertFalse(initial_state(99).nav_visible)
        self.assertFalse(initial_state(80).nav_visible)
        self.assertTrue(initial_state(100).nav_visible)
        narrow = reduce_key(ExplorerState(selection="project:/repo"), {"key": "resize", "cols": 80})
        self.assertFalse(narrow.nav_visible)
        self.assertEqual(narrow.selection, "project:/repo")
        self.assertTrue(reduce_key(ExplorerState(), {"key": "resize", "cols": 168}).nav_visible)


class Keys(unittest.TestCase):
    def test_e_folds_and_unfolds_the_navigation_without_touching_selection_tab_or_scroll(self):
        state = ExplorerState(selection="request:r1", tab="r1", stage="test", scrolls=(("nav", 3), ("ledger:r1", 2)))
        folded = reduce_key(state, "e")
        self.assertFalse(folded.nav_visible)
        self.assertEqual((folded.selection, folded.tab, folded.stage, folded.scrolls),
                         (state.selection, state.tab, state.stage, state.scrolls))
        self.assertEqual(folded.active_region, "ledger")                 # focus leaves the hidden region
        self.assertEqual(reduce_key(folded, "e").nav_visible, True)
        self.assertEqual(reduce_key(folded, "e").selection, "request:r1")

    def test_F_hides_and_shows_the_event_row_only(self):
        state = ExplorerState(selection="x", tab="t")
        hidden = reduce_key(state, "F")
        self.assertFalse(hidden.events_visible)
        self.assertEqual((hidden.selection, hidden.tab, hidden.nav_visible), ("x", "t", True))
        self.assertTrue(reduce_key(hidden, "F").events_visible)

    def test_tab_walks_overview_then_each_tab_then_back_and_remembers_the_one_it_left(self):
        state = ExplorerState(tab_order=("a", "b", "c"))
        seen = []
        for _ in range(5):
            state = reduce_key(state, "tab")
            seen.append(state.tab)
        self.assertEqual(seen, ["a", "b", "c", "", "a"])
        self.assertEqual(reduce_key(ExplorerState(tab="b", tab_order=("a", "b")), "tab").keep, ("b",))
        self.assertEqual(reduce_key(ExplorerState(tab="a", tab_order=("a", "b")), "btab").tab, "")

    def test_left_right_move_focus_between_regions_and_skip_a_hidden_navigation(self):
        state = ExplorerState()
        order = []
        for _ in range(7):
            state = reduce_key(state, "right")
            order.append(state.active_region)
        self.assertEqual(order, ["ledger", "output", "tabs", "relations", "system", "nav", "ledger"])
        self.assertEqual(reduce_key(ExplorerState(), "left").active_region, "system")
        hidden = ExplorerState(nav_visible=False, active_region="system")
        self.assertEqual(reduce_key(hidden, "right").active_region, "ledger")

    def test_up_down_move_the_navigation_selection_over_the_rows_it_is_given(self):
        class Row:
            def __init__(self, identity):
                self.identity = identity
        rows = [Row("a"), Row("b"), Row("c")]
        state = ExplorerState(selection="b")
        self.assertEqual(reduce_key(state, {"key": "down", "nav_rows": rows}).selection, "c")
        self.assertEqual(reduce_key(state, {"key": "up", "nav_rows": rows}).selection, "a")
        self.assertEqual(reduce_key(ExplorerState(selection="c"), {"key": "down", "nav_rows": rows}).selection, "c")
        self.assertEqual(reduce_key(ExplorerState(selection="a"), {"key": "up", "nav_rows": rows}).selection, "a")
        self.assertEqual(reduce_key(ExplorerState(), {"key": "down", "nav_rows": rows}).selection, "a")

    def test_navigation_and_body_scroll_independently(self):
        state = ExplorerState(selection="request:r1", tab="r1", active_region="ledger")
        for _ in range(3):
            state = reduce_key(state, {"key": "down"})
        state = reduce_key(state, {"key": "pagedown", "viewport": 10})
        self.assertEqual(state.scroll_map(), {"ledger:r1": 13})
        state = reduce_key(state, "left")                                 # focus the navigation
        state = reduce_key(state, {"key": "down", "nav_rows": []})        # no rows: scrolls the viewport
        self.assertEqual(state.scroll_map(), {"ledger:r1": 13, "nav": 1})
        state = reduce_key(state, "right")
        state = reduce_key(state, {"key": "up"})
        self.assertEqual(state.scroll_map(), {"ledger:r1": 12, "nav": 1})
        self.assertEqual(reduce_key(state, {"key": "home"}).scroll_map()["ledger:r1"], 0)
        self.assertEqual(reduce_key(state, {"key": "end", "max_scroll": 30}).scroll_map()["ledger:r1"], 30)
        self.assertEqual(reduce_key(ExplorerState(active_region="output", tab="r1"), {"key": "up"})
                         .scroll_map()["output:r1"], 0)

    def test_scroll_never_goes_below_zero_or_past_the_end(self):
        state = reduce_key(ExplorerState(tab="t", active_region="ledger"), {"key": "up"})
        self.assertEqual(state.scroll_map()["ledger:t"], 0)
        state = reduce_key(state, {"key": "pagedown", "viewport": 50, "max_scroll": 7})
        self.assertEqual(state.scroll_map()["ledger:t"], 7)

    def test_search_filters_by_typing_and_escape_clears_it(self):
        state = reduce_key(ExplorerState(), "/")
        self.assertTrue(state.searching)
        for char in "ab":
            state = reduce_key(state, "text:" + char)
        self.assertEqual(state.search, "ab")
        self.assertEqual(reduce_key(state, "backspace").search, "a")
        kept = reduce_key(state, "enter")
        self.assertEqual((kept.searching, kept.search), (False, "ab"))
        self.assertEqual(reduce_key(kept, "escape").search, "")
        self.assertEqual(reduce_key(state, "escape"), ExplorerState())
        # while typing, letters that are commands elsewhere are just text
        self.assertEqual(reduce_key(reduce_key(ExplorerState(), "/"), "text:e").search, "e")


class EnterOnRealRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.base = collect(cls.fx)
        cls.b6 = tab(cls.base, "b6-report").identity
        cls.fleet = tab(cls.base, "fleet-explorer").identity
        cls.result = collect(cls.fx, request_id=cls.b6, stage_id="test")

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def enter(self, state, identity):
        state = dataclasses.replace(state, selection=identity)
        rows = visible_rows(build_nav(self.fx.snapshot, self.result, state, width=40), state)
        return reduce_key(state, {"key": "enter", "nav_rows": rows})

    def test_enter_on_a_request_selects_its_tab_remembers_the_old_one_and_focuses_the_ledger(self):
        start = ExplorerState(tab=self.b6, tab_order=(self.b6, self.fleet))
        after = self.enter(start, "request:" + self.fleet)
        self.assertEqual((after.tab, after.keep, after.active_region), (self.fleet, (self.b6,), "ledger"))
        self.assertEqual(after.stage, "")

    def test_enter_on_a_stage_row_selects_the_request_and_the_stage(self):
        start = ExplorerState(nav_open=("stages:" + self.b6,))
        after = self.enter(start, f"stage:{self.b6}:test")
        self.assertEqual((after.tab, after.stage), (self.b6, "test"))

    def test_enter_on_a_fold_toggles_it_and_a_second_enter_toggles_it_back(self):
        closed = self.enter(ExplorerState(), "project:cairn")
        self.assertIn("project:cairn", closed.nav_open)
        again = self.enter(closed, "project:cairn")
        self.assertNotIn("project:cairn", again.nav_open)
        self.assertIn("project:cairn", again.nav_fold)
        folded = self.enter(ExplorerState(), "project:hearting")        # open by default: Enter folds it
        self.assertIn("project:hearting", folded.nav_fold)
        reopened = self.enter(folded, "project:hearting")
        self.assertNotIn("project:hearting", reopened.nav_fold)

    def test_enter_on_the_information_rows_opens_their_page_without_a_new_mode(self):
        self.assertEqual(self.enter(ExplorerState(tab=self.b6), "section:todo").tab, "")
        self.assertEqual(self.enter(ExplorerState(), "section:relations").active_region, "relations")
        self.assertEqual(self.enter(ExplorerState(), "system:moving4").active_region, "system")

    def test_enter_in_the_ledger_toggles_the_attempt_history_of_the_tab(self):
        on = reduce_key(ExplorerState(tab=self.b6, active_region="ledger"), "enter")
        self.assertEqual(on.history, (self.b6,))
        self.assertEqual(reduce_key(on, "enter").history, ())
        self.assertEqual(reduce_key(ExplorerState(active_region="ledger"), "enter").history, ())

    def test_the_reducer_can_take_the_composed_frame_instead_of_a_row_list(self):
        state = ExplorerState(selection="section:sessions")
        frame = compose(self.fx.snapshot, self.result, state, 168, 45, now=self.fx.now)
        self.assertEqual(frame.nav_rows[0].identity, "section:sessions")
        self.assertEqual(reduce_key(state, "down", frame).selection, frame.nav_rows[1].identity)
        paged = reduce_key(dataclasses.replace(state, active_region="ledger", tab=self.b6), "pagedown", frame)
        self.assertEqual(paged.scroll_map(), {f"ledger:{self.b6}": frame.region("ledger").h})

    def test_keys_change_which_page_composes_and_nothing_else(self):
        scenes = load_screens()["scenes"]["first"]
        result = scene_result(self.fx, scenes)
        state = ExplorerState(selection="request:" + self.b6, tab=self.b6, stage="test",
                              tab_order=tuple(t.identity for t in result.tabs))
        def page(view):
            return compose(self.fx.snapshot, result, view, 168, 45, now=self.fx.now).page

        history = reduce_key(reduce_key(state, "right"), "enter")           # focus the ledger, Enter
        overview = dataclasses.replace(state, tab="")
        system = state
        for _ in range(5):
            system = reduce_key(system, "right")
        pages = [page(state), page(history), page(overview), page(system)]
        self.assertEqual(pages, ["ledger", "history", "overview", "system"])
        self.assertEqual((system.selection, system.tab), (state.selection, state.tab))


if __name__ == "__main__":
    unittest.main()
