"""s/x and the mouse on the B surface: the target set is the navigation's session and worker rows,
never larger than the old group path's, and nothing else on the screen can start a kill.

Hermetic like test_f27_*: no signal leaves the process, ``control.is_excluded`` is stubbed where a
fake pid has to be judged, and every action log lands in a temp FLEET_ACTION_STATE_DIR.
"""
import curses
import os
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from tools.fleet import control, render
from tools.fleet.explorer.state import ExplorerState
from tools.fleet.model import DispatchJob, Session, WorkProjection
from tools.fleet.tests.explorer_fixture import collect, materialize, model_snapshot, tab


class FakeScreen:
    def __init__(self, h=45, w=168):
        self.h, self.w = h, w
        self.rows = {}
        self.reversed = []

    def getmaxyx(self):
        return self.h, self.w

    def erase(self):
        self.rows = {}

    def addstr(self, row, col, text, attr=0):
        self.rows[row] = text

    def chgat(self, y, x, n, attr):
        self.reversed.append((y, x, n))

    def refresh(self):
        pass


STATE_NAMES = ("_EXPLORER_ENABLED", "_EXPLORER_STATE", "_EXPLORER_FRAME", "_EXPLORER_NAV_ROWS",
               "_EXPLORER_ALL_ROWS", "_EXPLORER_RESULT", "_EXPLORER_SIZE", "_EXPLORER_ORDER",
               "_SELECTABLE", "_CLICK_ROWS", "_CLICK_SPANS", "_PROMPT_HITS", "_HIT_VALID", "_HIT_SIZE",
               "_HIT_SNAPSHOT_ID", "_PROCESS_VIEW", "_SHOW_ALL", "_LAYOUT", "_OFFSET")


class ControlEnv(unittest.TestCase):
    def setUp(self):
        self.log = tempfile.mkdtemp()
        self.saved = {name: getattr(render, name) for name in STATE_NAMES}
        self.prev_log = os.environ.get("FLEET_ACTION_STATE_DIR")
        os.environ["FLEET_ACTION_STATE_DIR"] = self.log
        render.reset_selection()
        render.reset_scroll()
        render.set_explorer_mode(True)
        self.excluded = set()
        patcher = mock.patch.object(control, "is_excluded", side_effect=lambda pid: pid in self.excluded)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(render, name, value)
        if self.prev_log is None:
            os.environ.pop("FLEET_ACTION_STATE_DIR", None)
        else:
            os.environ["FLEET_ACTION_STATE_DIR"] = self.prev_log
        render.reset_selection()

    @staticmethod
    def pairs(entries):
        return {(entry["kind"], entry["pid"]) for entry in entries}

    def draw(self, snapshot, size=(45, 168)):
        screen = FakeScreen(*size)
        render._draw_explorer(screen, snapshot)
        return screen

    def press(self, x, y, snapshot, size=(45, 168)):
        render._explorer_handle(curses.KEY_MOUSE, (x, y, curses.BUTTON1_PRESSED), size, snapshot)


def session(pid, sid, liveness, cwd="/home/u/p", **extra):
    row = Session(harness="claude", pid=pid, cwd=cwd, session_id=sid, slug=sid, liveness=liveness,
                  title="work " + sid, process_role="conversation", **extra)
    row.proc_start = str(pid + 1000)
    return row


def job(pid, slug, parent_sid, depth=1, liveness="working", parent_slug=None):
    row = DispatchJob(key="code", pid=pid, slug=slug, depth=depth, parent_slug=parent_slug,
                      attempt_id="att-%d" % pid, liveness=liveness, cwd="/home/u/p", parent_sid=parent_sid,
                      harness="claude", status="open", work_projection=WorkProjection())
    row.proc_start = str(pid + 2000)
    return row


def small_snapshot():
    child = Session(harness="claude", pid=15, cwd="/home/u/p", session_id="c1", slug="c1", liveness="working",
                    is_child=True, process_role="registered-worker")
    child.proc_start = "1015"
    sessions = [session(11, "s1", "working"), session(12, "s2", "idle"), session(13, "s3", "unused"),
                session(14, "s4", "stale", cwd="/home/u/q"), child]
    jobs = [job(21, "own1", "s1"), job(22, "stg1", "s1", 2, parent_slug="own1"),
            job(23, "own2", "s2", liveness="idle"), job(24, "gone", "s1", liveness="done"),
            job(25, "nopar", "zzz"), job(26, "nopid", "s3")]
    jobs[-1].pid = None
    return SimpleNamespace(sessions=sessions, jobs=jobs, resources=[], usage_snapshots={},
                           display_snapshot=None, observations={}, malformed=0)


class TargetSet(ControlEnv):
    """s/x targets: navigation session and worker rows only, never beyond the old group path."""

    def old_targets(self, snapshot):
        render._build_lines(list(snapshot.sessions), list(snapshot.jobs), section="both", narrow=False,
                            malformed=0, term_width=168)
        return self.pairs(render._SELECTABLE)

    def test_nav_targets_are_a_subset_of_the_old_group_path_targets(self):
        snapshot = small_snapshot()
        old = self.old_targets(snapshot)
        self.assertTrue({("session", 11), ("job", 21)} <= old)           # the comparison is not vacuous
        render._explorer_compose(snapshot, 168, 45)
        new = self.pairs(render._SELECTABLE)
        self.assertTrue(new <= old, sorted(new - old))
        self.assertEqual(new, {("session", 11), ("session", 13), ("job", 21), ("job", 22), ("job", 23)})

    def test_rows_the_old_path_hid_or_never_listed_are_not_targets(self):
        snapshot = small_snapshot()
        render._explorer_compose(snapshot, 168, 45)
        new = self.pairs(render._SELECTABLE)
        self.assertNotIn(("session", 12), new)        # a plain idle session is nobody's cleanup target
        self.assertNotIn(("session", 14), new)        # stale: hidden by the old group path unless --all
        self.assertNotIn(("session", 15), new)        # a worker session is counted, not listed
        self.assertNotIn(("job", 24), new)            # ended
        self.assertNotIn(("job", 25), new)            # no parent session
        self.assertNotIn(("job", 26), new)            # no pid to verify

    def test_fixture_snapshot_targets_do_not_grow(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = model_snapshot(materialize(root))
            old = self.old_targets(snapshot)
            render._explorer_compose(snapshot, 168, 45)
            new = self.pairs(render._SELECTABLE)
        self.assertTrue(new, "the fixture has live sessions and workers")
        self.assertTrue(new <= old, sorted(new - old))

    def test_the_target_set_does_not_depend_on_folds_search_or_request_data(self):
        snapshot = small_snapshot()
        render._explorer_compose(snapshot, 168, 45)
        plain = self.pairs(render._SELECTABLE)
        render._explorer_compose(snapshot, 168, 45, ExplorerState(nav_fold=("project:p", "section:sessions"),
                                                                  search="no-such-name"))
        self.assertEqual(self.pairs(render._SELECTABLE), plain)

    def test_every_target_row_is_a_session_or_worker_navigation_row(self):
        snapshot = small_snapshot()
        render._explorer_compose(snapshot, 168, 45)
        kinds = {row.identity: row.kind for row in render._EXPLORER_ALL_ROWS}
        self.assertTrue(render._SELECTABLE)
        for entry in render._SELECTABLE:
            self.assertIn(kinds[entry["nav_id"]], ("session", "worker"))

    def test_an_unreadable_row_is_not_a_target(self):
        snapshot = small_snapshot()
        snapshot.sessions[0].pid = None                    # no pid to verify -> not a target
        render._explorer_compose(snapshot, 168, 45)
        self.assertNotIn(("session", None), self.pairs(render._SELECTABLE))


class Regions(ControlEnv):
    """Clicks anywhere but a target row of the navigation never start a kill."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.fx = materialize(self.dir.name)
        self.snapshot = model_snapshot(self.fx)
        base = collect(self.fx)
        self.b6 = tab(base, "b6-report")
        render._EXPLORER_RESULT = collect(self.fx, request_id=self.b6.identity)
        render._EXPLORER_STATE = ExplorerState(
            selection="request:" + self.b6.identity, tab=self.b6.identity,
            focus="claude:" + self.fx.sid, tab_order=tuple(item.identity for item in base.tabs))
        self.screen = self.draw(self.snapshot)
        self.frame = render._EXPLORER_FRAME

    def assertNoControl(self):
        self.assertFalse(render._SELECT_MODE)
        self.assertIsNone(render._PROMPT)
        self.assertIsNone(render._CURSOR_ID)

    def test_the_screen_has_all_the_regions_the_clicks_below_cover(self):
        regions = dict(self.frame.regions)
        for name in ("nav", "ledger", "output", "tabs", "request", "header", "legend", "events", "keys"):
            self.assertTrue(regions[name].w and regions[name].h, name)
        self.assertEqual(self.frame.page, "ledger")
        self.assertTrue(render._CLICK_ROWS, "session and worker rows are clickable")

    def test_a_click_in_ledger_output_tabs_request_legend_events_or_keys_is_not_a_kill_request(self):
        regions = dict(self.frame.regions)
        for name in ("ledger", "output", "tabs", "request", "header", "separator", "legend", "events", "keys"):
            rect = regions[name]
            if not rect.w or not rect.h:
                continue
            for y in range(rect.y, rect.y + rect.h):
                for x in (rect.x, rect.x + rect.w // 2, rect.x + rect.w - 1):
                    with self.subTest(region=name, x=x, y=y):
                        self.press(x, y, self.snapshot)
                        self.assertNoControl()

    def test_relations_remaining_work_and_pages_are_not_kill_requests_either(self):
        for region in ("relations", "tabs", "system", "ledger"):
            render._EXPLORER_STATE = replace(render._EXPLORER_STATE, active_region=region)
            screen = self.draw(self.snapshot)
            frame = render._EXPLORER_FRAME
            body = dict(frame.regions)["ledger"]
            for y in range(body.y, body.y + body.h):
                self.press(body.x + 1, y, self.snapshot)
            self.assertNoControl()

    def test_navigation_rows_that_are_not_sessions_or_workers_are_not_kill_requests(self):
        nav_rect = dict(self.frame.regions)["nav"]
        clickable = set(render._CLICK_ROWS)
        informational = [y for y in range(nav_rect.y, nav_rect.y + nav_rect.h) if y not in clickable]
        self.assertTrue(informational)
        for y in informational:
            self.press(nav_rect.x + 1, y, self.snapshot)
            self.assertNoControl()
        kinds = {row.kind for row in self.frame.nav_rows}
        self.assertTrue({"section", "request", "stage-group"} & kinds)

    def test_a_navigation_target_row_selects_first_and_asks_to_kill_on_the_second_click(self):
        y, entry = sorted(render._CLICK_ROWS.items())[0]
        self.press(1, y, self.snapshot)
        self.assertTrue(render._SELECT_MODE)
        self.assertEqual(render._CURSOR_ID, (entry["pid"], entry["proc_start"]))
        self.assertIsNone(render._PROMPT)
        self.draw(self.snapshot)
        y2 = next(row for row, found in render._CLICK_ROWS.items() if found["pid"] == entry["pid"])
        self.press(1, y2, self.snapshot)
        self.assertEqual(render._PROMPT["stage"], "confirm")
        self.assertEqual(render._PROMPT["entry"]["pid"], entry["pid"])

    def test_an_excluded_pid_is_never_selected_prompted_or_reachable_by_x(self):
        y, entry = sorted(render._CLICK_ROWS.items())[0]
        self.excluded.add(entry["pid"])
        self.press(1, y, self.snapshot)
        self.assertNoControl()
        self.assertNotIn(entry["pid"], {e["pid"] for e in render._live_targets()})
        for ch in (ord("x"), ord("s")):
            render._explorer_handle(ch, None, (45, 168), self.snapshot)
            if render._SELECT_MODE:
                self.assertNotEqual(render._CURSOR_ID, (entry["pid"], entry["proc_start"]))
            render._explorer_handle(27, None, (45, 168), self.snapshot)
        self.assertIsNone(render._PROMPT)

    def test_a_click_aimed_at_an_older_paint_is_ignored_after_a_resize(self):
        y, entry = sorted(render._CLICK_ROWS.items())[0]
        self.assertTrue(render._explorer_click_current((45, 168), self.snapshot))
        self.assertFalse(render._explorer_click_current((40, 120), self.snapshot))
        self.press(1, y, self.snapshot, size=(40, 120))                    # the terminal is now smaller
        self.assertNoControl()
        self.assertFalse(render._explorer_click_current((45, 168), SimpleNamespace()))   # another snapshot
        self.draw(self.snapshot, size=(40, 120))                          # the repaint publishes new geometry
        self.assertTrue(render._explorer_click_current((40, 120), self.snapshot))
        rows_now = set(render._CLICK_ROWS)
        self.assertTrue(all(row < 40 for row in rows_now))
        for row, found in render._CLICK_ROWS.items():
            self.assertIn(found["nav_id"], {r.identity for r in render._EXPLORER_FRAME.nav_rows})


class Keys(ControlEnv):
    def setUp(self):
        super().setUp()
        self.snapshot = small_snapshot()
        self.screen = self.draw(self.snapshot)

    def test_s_enters_the_kill_selection_over_nav_targets_and_x_asks_for_confirmation(self):
        render._explorer_handle(ord("s"), None, (45, 168), self.snapshot)
        self.assertTrue(render._SELECT_MODE)
        ids = {(e["pid"], e["proc_start"]) for e in render._live_targets()}
        self.assertIn(render._CURSOR_ID, ids)
        render._explorer_handle(ord("x"), None, (45, 168), self.snapshot)
        self.assertEqual(render._PROMPT["stage"], "confirm")
        self.assertIn((render._PROMPT["entry"]["pid"], render._PROMPT["entry"]["proc_start"]), ids)

    def test_the_cursor_walks_target_rows_and_the_navigation_selection_follows_it(self):
        render._explorer_handle(ord("s"), None, (45, 168), self.snapshot)
        start = render._CURSOR_ID
        render._explorer_handle(curses.KEY_DOWN, None, (45, 168), self.snapshot)
        self.assertNotEqual(render._CURSOR_ID, start)
        screen = self.draw(self.snapshot)
        entry = next(e for e in render._SELECTABLE if (e["pid"], e["proc_start"]) == render._CURSOR_ID)
        self.assertEqual(render._EXPLORER_STATE.selection, entry["nav_id"])
        self.assertTrue(screen.reversed, "the cursor row is highlighted")

    def test_a_kill_cursor_on_a_folded_worker_unfolds_the_rows_above_it(self):
        render._explorer_compose(self.snapshot, 168, 45)
        worker = next(e for e in render._SELECTABLE if e["kind"] == "job")
        self.assertNotIn(worker["nav_id"], {r.identity for r in render._EXPLORER_NAV_ROWS})
        render._explorer_handle(ord("s"), None, (45, 168), self.snapshot)
        while render._CURSOR_ID != (worker["pid"], worker["proc_start"]):
            render._explorer_handle(curses.KEY_DOWN, None, (45, 168), self.snapshot)
            self.draw(self.snapshot)
        self.draw(self.snapshot)
        self.assertIn(worker["nav_id"], {r.identity for r in render._EXPLORER_NAV_ROWS})

    def test_p_w_a_c_and_space_change_no_view_or_layout(self):
        before = (render._PROCESS_VIEW, render._LAYOUT, render._SHOW_ALL, render._EXPLORER_STATE,
                  render._OFFSET, dict(render._REPO_FOLD), dict(render._ROUTE_FOLD))
        for ch in (ord("p"), ord("P"), ord("w"), ord("W"), ord("a"), ord("A"), ord("c"), ord("C"), ord(" ")):
            render._explorer_handle(ch, None, (45, 168), self.snapshot)
        after = (render._PROCESS_VIEW, render._LAYOUT, render._SHOW_ALL, render._EXPLORER_STATE,
                 render._OFFSET, dict(render._REPO_FOLD), dict(render._ROUTE_FOLD))
        self.assertEqual(before, after)
        self.assertFalse(render._SELECT_MODE)

    def test_typed_text_in_the_search_is_text_not_a_command(self):
        render._explorer_handle(ord("/"), None, (45, 168), self.snapshot)
        self.assertTrue(render._EXPLORER_STATE.searching)
        for ch in (ord("x"), ord("s"), ord("q"), ord("e")):
            render._explorer_handle(ch, None, (45, 168), self.snapshot)
        self.assertEqual(render._EXPLORER_STATE.search, "xsqe")
        self.assertFalse(render._SELECT_MODE)
        self.assertIsNone(render._PROMPT)
        self.assertTrue(render._explorer_searching())
        render._explorer_handle(27, None, (45, 168), self.snapshot)
        self.assertEqual(render._EXPLORER_STATE.search, "")
        self.assertFalse(render._explorer_searching())

    def test_a_confirmed_kill_still_refuses_a_target_whose_start_time_does_not_match(self):
        render._explorer_handle(ord("s"), None, (45, 168), self.snapshot)
        render._explorer_handle(ord("x"), None, (45, 168), self.snapshot)
        killed = []
        with mock.patch.object(os, "kill", lambda pid, sig: killed.append((pid, sig))):
            render._handle_prompt_key(ord("y"))
            render._handle_prompt_key(ord("Y"))
        self.assertEqual(killed, [], "fake pids fail the pid/start-time verification: no signal is sent")


if __name__ == "__main__":
    unittest.main()
