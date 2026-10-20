import dataclasses
import json
import tempfile
import unittest
from unittest import mock

from tools.fleet.explorer.frame import compose
from tools.fleet.explorer.nav import build_nav, visible_rows
from tools.fleet.explorer.state import (ExplorerState, coerce, focus_key, initial_state, reconcile,
                                        reduce_key, restore, serialize, settle)
from tools.fleet.explorer.types import RequestTab
from tools.fleet.tests.explorer_fixture import (collect, materialize, model_snapshot, scene_result, tab,
                                                load_screens)


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


class LegacyHandoff(unittest.TestCase):
    """A schema-1 viewer hand-over (process_view / offset / folds / repo_folds / cursor / frame) read by B."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.snapshot = model_snapshot(cls.fx)
        cls.rows = build_nav(cls.snapshot)
        cls.main = next(s for s in cls.snapshot.sessions if s.session_id == cls.fx.sid)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def old_payload(self, **over):
        """What the previous viewer wrote: JSON, cursor = (pid, proc_start), folds as [key, bool] pairs."""
        payload = {"offset": 9, "process_view": True, "show_all": False, "layout": "stack",
                   "folds": [[["route-card", "a", 0], True], [["gpu-commands", "host", 0], False]],
                   "repo_folds": [["%s/personal/hearting" % self.dir.name, True], ["elsewhere", False]],
                   "select_mode": False, "cursor": [self.main.pid, self.main.proc_start],
                   "frame": [[["old group frame", "dim"]]],
                   "frame_layout": {"schema_version": 1, "width": 168, "process_view": True,
                                    "layout": "wide", "fixed_end": 2, "body_start": 2, "body_end": 9,
                                    "legend_start": 9},
                   "frame_at": 12.5, "frame_compute_at": 12.0,
                   "order": {"groups": ["hearting"], "tiers": {"hearting": 0}, "sessions": {}}}
        payload.update(over)
        return json.loads(json.dumps(payload))

    def test_cursor_pid_becomes_the_session_row_it_pointed_at(self):
        state = restore(self.old_payload())
        self.assertTrue(state.selection.startswith("pid:"))
        settled = settle(state, self.rows)
        self.assertEqual(settled.selection, f"session:claude:{self.fx.sid}")
        self.assertEqual(settled.focus, f"claude:{self.fx.sid}")
        self.assertEqual(focus_key(settled).sid, self.fx.sid)

    def test_cursor_on_a_job_row_becomes_that_worker_row(self):
        job = next(j for j in self.snapshot.jobs if j.liveness == "working" and j.parent_sid == self.fx.sid)
        state = restore(self.old_payload(cursor=[job.pid, job.proc_start]))
        settled = settle(state, self.rows)
        self.assertTrue(settled.selection.startswith("worker:"), settled.selection)
        self.assertEqual(settled.focus, f"claude:{self.fx.sid}")

    def test_a_cursor_that_points_at_nothing_falls_back_to_the_first_session(self):
        settled = settle(restore(self.old_payload(cursor=[999999999, "1"])), self.rows)
        first = next(r for r in self.rows if r.kind == "session")
        self.assertEqual(settled.selection, first.identity)

    def test_repo_folds_become_project_folds_and_offset_the_nav_scroll(self):
        state = restore(self.old_payload())
        self.assertEqual(state.scroll_map()["nav"], 9)
        self.assertEqual(state.nav_fold[0].endswith("hearting"), True)
        settled = settle(state, self.rows)
        self.assertEqual(settled.nav_fold, ("project:hearting",))
        self.assertEqual(settled.scroll_map()["nav"], 9)

    def test_older_handoffs_without_a_cursor_or_folds_settle_on_the_first_session(self):
        for payload in ({}, {"process_view": "process"}, {"offset": 3}):
            settled = settle(restore(payload), self.rows)
            self.assertEqual(settled.selection, next(r for r in self.rows if r.kind == "session").identity)

    def test_the_old_frame_is_not_replayed_and_the_new_hand_over_is_schema_two(self):
        from tools.fleet import render
        names = ("_EXPLORER_ENABLED", "_EXPLORER_STATE", "_RELOAD_FRAME", "_RELOAD_LAYOUT", "_RELOAD_FRAME_AT",
                 "_RELOAD_FRAME_COMPUTE_AT", "_OFFSET", "_PROCESS_VIEW", "_LAYOUT", "_RESUME_ORDER",
                 "_ROUTE_FOLD", "_REPO_FOLD", "_CURSOR_ID", "_SELECT_MODE", "_SHOW_ALL", "_SELECTABLE",
                 "_FOLDABLE", "_REPO_FOLDABLE", "_SHELL_TTY_MODE")
        saved = {name: getattr(render, name) for name in names}
        self.addCleanup(lambda: [setattr(render, name, value) for name, value in saved.items()])
        render.set_explorer_mode(True)
        render.restore_viewer_state(self.old_payload())
        self.assertIsNone(render._RELOAD_FRAME)
        self.assertEqual(render._RELOAD_LAYOUT, {"schema_version": 2})
        handoff = render.viewer_state(render._LiveOrderState())
        self.assertIsNone(handoff["frame"])
        self.assertEqual(handoff["frame_layout"], {"schema_version": 2})
        self.assertEqual(handoff["explorer_state"]["frame_layout"]["schema_version"], 2)
        self.assertEqual(handoff["explorer_state"]["scrolls"], {"nav": 9})

    def test_a_schema_one_frame_is_never_painted_by_the_b_surface(self):
        from tools.fleet import render
        from tools.fleet.tests.test_explorer_control import FakeScreen
        names = ("_EXPLORER_ENABLED", "_EXPLORER_STATE", "_RELOAD_FRAME", "_RELOAD_LAYOUT", "_RELOAD_FRAME_AT",
                 "_RELOAD_FRAME_COMPUTE_AT", "_RESUME_ORDER", "_SELECTABLE", "_EXPLORER_FRAME", "_EXPLORER_RESULT",
                 "_EXPLORER_SIZE", "_OFFSET", "_PROCESS_VIEW", "_LAYOUT", "_ROUTE_FOLD", "_REPO_FOLD",
                 "_CURSOR_ID", "_SELECT_MODE", "_SHOW_ALL", "_FOLDABLE", "_REPO_FOLDABLE", "_SHELL_TTY_MODE",
                 "_EXPLORER_ALL_ROWS", "_EXPLORER_NAV_ROWS", "_EXPLORER_ORDER", "_CLICK_ROWS", "_CLICK_SPANS",
                 "_HIT_VALID")
        saved = {name: getattr(render, name) for name in names}
        self.addCleanup(lambda: [setattr(render, name, value) for name, value in saved.items()])
        render.set_explorer_mode(True)
        render.restore_viewer_state(self.old_payload())
        screen = FakeScreen(45, 168)
        render._draw(screen, self.snapshot.sessions, self.snapshot.jobs, "both", 0, loading=True,
                     snapshot=self.snapshot)
        self.assertNotIn("old group frame", "\n".join(screen.rows.values()))
        self.assertIn("세션", "\n".join(screen.rows.values()))

    def test_a_schema_two_hand_over_round_trips_through_the_render_module(self):
        from tools.fleet import render
        names = ("_EXPLORER_ENABLED", "_EXPLORER_STATE", "_RELOAD_FRAME", "_RELOAD_LAYOUT", "_RESUME_ORDER",
                 "_CURSOR_ID", "_SELECT_MODE", "_OFFSET", "_PROCESS_VIEW", "_SHOW_ALL", "_LAYOUT", "_ROUTE_FOLD",
                 "_REPO_FOLD", "_SELECTABLE", "_FOLDABLE", "_REPO_FOLDABLE", "_SHELL_TTY_MODE",
                 "_RELOAD_FRAME_AT", "_RELOAD_FRAME_COMPUTE_AT")
        saved = {name: getattr(render, name) for name in names}
        self.addCleanup(lambda: [setattr(render, name, value) for name, value in saved.items()])
        render.set_explorer_mode(True)
        state = ExplorerState(selection="stage:cyc:test", tab="cyc", stage="test", active_region="ledger",
                              nav_fold=("project:hearting",), scrolls=(("ledger:cyc", 2), ("nav", 4)),
                              tab_order=("cyc", "other"), keep=("other",), history=("cyc",),
                              focus="claude:" + self.fx.sid, events_visible=False)
        render._EXPLORER_STATE = state
        handoff = json.loads(json.dumps(render.viewer_state(render._LiveOrderState())))
        render.reset_scroll()
        render.restore_viewer_state(handoff)
        self.assertEqual(render._EXPLORER_STATE, state)


class Settling(unittest.TestCase):
    """The state is resolved against the rows it is drawn with; a vanished request leaves cleanly."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.result = collect(cls.fx)
        cls.tabs = cls.result.tabs
        cls.rows = build_nav(cls.fx.snapshot, cls.result, ExplorerState(focus="claude:" + cls.fx.sid))

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_a_vanished_request_moves_to_the_nearest_surviving_neighbor_tab(self):
        order = tuple(t.identity for t in self.tabs)
        gone = order[2]
        state = ExplorerState(selection=f"request:{gone}", tab=gone, stage="test", tab_order=order,
                              focus="claude:" + self.fx.sid)
        survivors = [t for t in self.tabs if t.identity != gone]
        rows = build_nav(self.fx.snapshot, dataclasses.replace(self.result, tabs=tuple(survivors)),
                         ExplorerState(focus=state.focus))
        settled = settle(state, rows, survivors)
        self.assertEqual(settled.tab, order[3])                       # the next surviving neighbor
        self.assertEqual(settled.stage, "")
        self.assertEqual(settled.selection, f"request:{order[3]}")

    def test_with_no_request_left_the_view_returns_to_the_overview(self):
        state = ExplorerState(selection="request:gone", tab="gone", tab_order=("gone",),
                              focus="claude:" + self.fx.sid)
        rows = build_nav(self.fx.snapshot, dataclasses.replace(self.result, tabs=()),
                         ExplorerState(focus=state.focus))
        settled = settle(state, rows, ())
        self.assertEqual(settled.tab, "")
        self.assertEqual(settled.selection, f"session:claude:{self.fx.sid}")

    def test_a_request_selection_is_kept_while_the_selected_scope_is_still_being_read(self):
        state = ExplorerState(selection=f"request:{self.tabs[0].identity}", tab=self.tabs[0].identity,
                              focus="claude:" + self.fx.sid)
        plain = build_nav(self.fx.snapshot)                           # no request rows yet
        settled = settle(state, plain, None)
        self.assertEqual((settled.selection, settled.tab), (state.selection, state.tab))

    def test_the_focus_follows_the_selected_row_and_keeps_the_last_session_over_a_section(self):
        other = next(r for r in self.rows if r.kind == "session" and r.identity != f"session:claude:{self.fx.sid}")
        moved = settle(ExplorerState(selection=other.identity, focus="claude:" + self.fx.sid), self.rows)
        self.assertEqual(moved.focus, other.identity[len("session:"):])
        section = settle(ExplorerState(selection="section:system", focus="claude:" + self.fx.sid), self.rows)
        self.assertEqual(section.focus, "claude:" + self.fx.sid)

    def test_focus_key_names_the_session_tab_and_stage_the_pump_reads_for(self):
        state = ExplorerState(focus="codex:abc", tab="cyc", stage="test")
        key = focus_key(state)
        self.assertEqual((key.harness, key.sid, key.request_id, key.stage_id), ("codex", "abc", "cyc", "test"))
        self.assertIsNone(focus_key(ExplorerState()))
        self.assertEqual(focus_key(ExplorerState(focus="codex:abc", stage="test")).stage_id, "")


if __name__ == "__main__":
    unittest.main()
