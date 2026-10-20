import contextlib
import curses
import io
import os
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from tools.fleet import fleet as fleet_mod
from tools.fleet import render
from tools.fleet.explorer import frame as explorer_frame
from tools.fleet.explorer.focus import FocusReader, SelectionPump
from tools.fleet.explorer.output import cell_width
from tools.fleet.explorer.state import ExplorerState
from tools.fleet.explorer.types import FocusKey, FocusResult, Health
from tools.fleet.model import Session
from tools.fleet.refresh import LiveSnapshot
from tools.fleet.tests.explorer_fixture import collect, materialize, model_snapshot
from tools.fleet.tests.test_explorer_control import FakeScreen, small_snapshot

STATE_NAMES = ("_EXPLORER_ENABLED", "_EXPLORER_STATE", "_EXPLORER_FRAME", "_EXPLORER_NAV_ROWS",
               "_EXPLORER_ALL_ROWS", "_EXPLORER_RESULT", "_EXPLORER_SIZE", "_EXPLORER_ORDER",
               "_RELOAD_FRAME", "_RELOAD_LAYOUT", "_SELECTABLE", "_COMPUTE_HOSTS", "_COMPUTE_HOSTS_SET_AT",
               "_BLINK_ON", "_REFRESH_HEALTH", "_CLICK_ROWS", "_CLICK_SPANS", "_HIT_VALID")


class IntegrationEnv(unittest.TestCase):
    def setUp(self):
        self.saved = {name: getattr(render, name) for name in STATE_NAMES}
        render.reset_selection()
        render.reset_scroll()

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(render, name, value)
        render.reset_selection()


class ComposeAdapter(IntegrationEnv):
    def test_compose_path_does_not_call_legacy_line_builder(self):
        snapshot = SimpleNamespace(sessions=(), jobs=(), resources=(), usage_snapshots={},
                                   display_snapshot=None)
        with mock.patch.object(render, "_build_lines", side_effect=AssertionError("legacy renderer")), \
                mock.patch.object(render, "_usage_header_rows", return_value=[]), \
                mock.patch.object(render, "_compute_host_rows", return_value=[]):
            frame = render._explorer_compose(snapshot, 100, 45)
        self.assertEqual(frame.geometry, (100, 45))
        self.assertEqual(len(frame.lines), 45)
        self.assertTrue(all(cell_width(line) == 100 for line in frame.lines))
        self.assertIn("세션", "\n".join(frame.lines))

    def test_the_adapter_hands_the_snapshot_the_result_and_the_state_to_compose(self):
        with mock.patch.object(explorer_frame, "compose", wraps=explorer_frame.compose) as compose:
            render._explorer_compose(small_snapshot(), 100, 45)
        args = compose.call_args
        self.assertIsNone(args.args[1])                                        # no result read yet
        self.assertEqual(args.args[3:5], (100, 45))
        self.assertIsInstance(args.args[2], ExplorerState)
        self.assertIsNone(args.kwargs.get("providers"))                        # no provider overrides: defaults only

    def test_no_job_summary_dump_system_rows_or_node_status_ledger_are_in_the_output(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root)
            snapshot = model_snapshot(fx)
            result = collect(fx, request_id=next(t.identity for t in collect(fx).tabs if t.label == "b6-report"))
            render._EXPLORER_STATE = ExplorerState(
                selection="request:" + result.key.request_id, tab=result.key.request_id,
                focus="claude:" + fx.sid, tab_order=tuple(t.identity for t in collect(fx).tabs))
            frame = render._explorer_compose(snapshot, 168, 45, render._EXPLORER_STATE, result)
        text = "\n".join(frame.lines)
        self.assertNotIn("작업: ", text)                    # the all-jobs summary lines
        self.assertNotIn("요약 시각", text)
        self.assertNotIn("작업 출력 없음", text)
        self.assertNotIn("요청 장부를 선택하세요", text)
        self.assertNotIn("작업 장부 + 출력", text)
        self.assertNotRegex(text, r"(?m)^\w+ · (open|running|done)$")     # old 'node · status' ledger rows
        self.assertIn("b6-report", text)
        self.assertIn("통과", text)


class OnceSurface(IntegrationEnv):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.fx = materialize(self.dir.name)
        self.snapshot = LiveSnapshot(**{k: v for k, v in vars(model_snapshot(self.fx)).items()
                                        if k in LiveSnapshot.__dataclass_fields__ and k != "observations"})
        self.snapshot.observations = dict(self.fx.snapshot.observations)
        self.reader = FocusReader(self.fx.jobs_path, self.fx.artifact_root, route_loader=self.fx.route_loader,
                                  now=self.fx.now, state_roots=self.fx.state_roots,
                                  live_jobs=self.fx.live_jobs, snapshot=lambda: self.fx.snapshot)

    def run_main(self, argv, env=None, cols=100, rows=45):
        buf = io.StringIO()
        collector = lambda harness_filter=None, jobs_path=None, usage="cache-only", fast_first=False: self.snapshot
        with mock.patch.object(fleet_mod, "collect_all", collector), contextlib.redirect_stdout(buf), \
                mock.patch("tools.fleet.explorer.focus.FocusReader", lambda **kwargs: self.reader), \
                mock.patch.object(shutil, "get_terminal_size", lambda *a, **k: os.terminal_size((cols, rows))), \
                mock.patch.object(render, "_collect_governor", return_value=None), \
                mock.patch.object(render, "_collect_memory", return_value=None), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            code = fleet_mod.main(argv)
        return code, buf.getvalue()

    def focus_main_session(self):
        """The viewer is already on the fixture's main session (as after a viewer hand-over)."""
        render._EXPLORER_STATE = ExplorerState(selection=f"session:claude:{self.fx.sid}",
                                               focus=f"claude:{self.fx.sid}")

    def test_default_once_output_is_the_compose_result(self):
        self.focus_main_session()
        seen = []
        original = render._explorer_once_frame

        def spy(*args, **kwargs):
            seen.append(original(*args, **kwargs))
            return seen[-1]

        with mock.patch.object(render, "_explorer_once_frame", spy):
            code, out = self.run_main(["--once"])
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1)
        frame = seen[0]
        self.assertEqual(out, "\n".join(frame.lines).rstrip() + "\n")
        again = explorer_frame.compose(render._explorer_view(self.snapshot), render._EXPLORER_RESULT,
                                       render._EXPLORER_STATE, 100, 45, now=self.fx.now)
        self.assertEqual(again.lines, frame.lines)                      # the output IS compose's, line for line
        self.assertEqual(render._EXPLORER_RESULT.key.sid, self.fx.sid)
        self.assertIn("개요", out)
        self.assertIn("요청 9 · 열림 4", out)                            # the selected scope was read
        self.assertNotIn("작업:", out)

    def test_once_reads_the_selected_scope_once_without_a_pump(self):
        self.focus_main_session()
        calls = []
        real = self.reader.__call__

        def counting(request):
            calls.append(threading.current_thread() is threading.main_thread())
            return real(request)

        frame = render._explorer_once_frame(self.snapshot, 100, 45, reader=counting)
        self.assertEqual(calls, [True])                                # one synchronous read, no worker thread
        text = "\n".join(frame.lines)
        self.assertIn("요청 9 · 열림 4 · 최신 실패 3 · 검토 0", text)      # the selected scope's own header
        self.assertIn("▶개요 │ b-p3 │ herdr │ owner │ fleet │ ‹+5 끝남›", text)
        self.assertIn("남은 일 5 · 담당 CL", text)

    def test_once_frames_have_no_width_mismatch_at_100_and_168_columns(self):
        for cols in (100, 168):
            with self.subTest(cols=cols):
                render.reset_scroll()
                seen = []
                original = render._explorer_once_frame

                def spy(*args, **kwargs):
                    seen.append(original(*args, **kwargs))
                    return seen[-1]

                with mock.patch.object(render, "_explorer_once_frame", spy):
                    code, _out = self.run_main(["--once"], cols=cols)
                self.assertEqual(code, 0)
                lines = seen[0].lines
                self.assertEqual(len(lines), 45)
                self.assertEqual([(i, cell_width(line)) for i, line in enumerate(lines)
                                  if cell_width(line) != cols], [])

    def test_once_with_view_group_or_process_is_the_old_renderer(self):
        for argv, env in ((["--once", "--view", "group"], {}), (["--once", "--view", "process"], {}),
                          (["--once"], {"FLEET_VIEW": "group"})):
            with self.subTest(argv=argv, env=env):
                with mock.patch.object(render, "_explorer_compose",
                                       side_effect=AssertionError("B surface used")), \
                        mock.patch.object(render, "_explorer_once_frame",
                                          side_effect=AssertionError("B surface used")):
                    code, out = self.run_main(argv, env)
                self.assertEqual(code, 0)
                self.assertNotIn("탐색 1–", out)
                self.assertNotIn("요청 ? ·", out)
        self.assertFalse(render._EXPLORER_ENABLED)


class Resilience(IntegrationEnv):
    """A blocked or broken selected-scope reader / region never stops drawing, keys or the status band."""

    def setUp(self):
        super().setUp()
        self.snapshot = small_snapshot()
        self.snapshot.usage_snapshots = {"claude": {"payload": {"rl_5h": 29, "rl_7d": 8}}}
        render.set_explorer_mode(True)

    def host(self, busy):
        return {"configured": True, "hosts": [{"host": "gpu1", "reachable": True, "cpu_utilization_pct": 4,
                                               "memory_used_mib": 1, "memory_total_mib": 2, "self": False,
                                               "gpus": [{"index": 0, "memory_used_mib": 1,
                                                         "memory_total_mib": 2,
                                                         "processes": [{"pid": 5}] if busy else []}]}]}

    def test_a_blocked_focus_provider_does_not_stop_draw_keys_or_usage_and_compute_updates(self):
        entered, release = threading.Event(), threading.Event()

        def blocking(request):
            entered.set()
            release.wait(10)
            return FocusResult(request.key, request.epoch, request.snapshot_generation,
                               health=Health("success", time.time()))

        feed = render._SelectionFeed(SelectionPump(blocking, 0.2))
        self.addCleanup(release.set)
        self.addCleanup(feed.stop)
        screen = FakeScreen(45, 168)
        render._explorer_compose(self.snapshot, 168, 45)                  # settles the first selection
        started = time.perf_counter()
        result, changed = feed.update(render._EXPLORER_STATE, 1, time.monotonic())
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertTrue(entered.wait(2.0))                                 # the read is blocked right now
        render._EXPLORER_RESULT = result
        self.assertEqual(result.health.state, "loading")
        draws = []
        for _ in range(5):
            started = time.perf_counter()
            render._draw_explorer(screen, self.snapshot)
            draws.append(time.perf_counter() - started)
        self.assertLess(max(draws), 0.5)
        self.assertIn("요청 ?", "\n".join(screen.rows.values()))           # honest: counts not read yet
        before = render._EXPLORER_STATE.selection
        render._explorer_handle(curses.KEY_DOWN, None, (45, 168), self.snapshot)
        self.assertNotEqual(render._EXPLORER_STATE.selection, before)      # key input still works
        render._draw_explorer(screen, self.snapshot)
        self.assertIn("CL 5h29%", screen.rows[1])
        self.snapshot.usage_snapshots = {"claude": {"payload": {"rl_5h": 41, "rl_7d": 8}}}
        render.set_compute_hosts(self.host(busy=False))
        render._draw_explorer(screen, self.snapshot)
        self.assertIn("CL 5h41%", screen.rows[1])
        self.assertIn("GPU 0/1", screen.rows[1])
        render.set_compute_hosts(self.host(busy=True))
        render._draw_explorer(screen, self.snapshot)
        self.assertIn("GPU 1/1", screen.rows[1])
        self.assertFalse(release.is_set())                                  # all of it while the read hung

    def test_an_erroring_region_renderer_leaves_the_other_regions_and_the_next_key_working(self):
        screen = FakeScreen(45, 168)
        render._draw_explorer(screen, self.snapshot)
        good = dict(screen.rows)
        with mock.patch("tools.fleet.explorer.system.status_lines", side_effect=RuntimeError("band broke")), \
                mock.patch("tools.fleet.explorer.nav.nav_lines", side_effect=RuntimeError("nav broke")):
            render._draw_explorer(screen, self.snapshot)
            frame = render._EXPLORER_FRAME
            failed = {name for name, _message in frame.errors}
            self.assertEqual(failed, {"header", "nav"})
            self.assertIn("그리지 못함", screen.rows[0])
            self.assertEqual(screen.rows[40], good[40])                      # legend / separator / keys unaffected
            self.assertEqual(screen.rows[44], good[44])
            before = render._EXPLORER_STATE.selection
            render._explorer_handle(curses.KEY_DOWN, None, (45, 168), self.snapshot)   # keys keep working
            self.assertNotEqual(render._EXPLORER_STATE.selection, before)
            render._draw_explorer(screen, self.snapshot)
            self.assertIn("그리지 못함", screen.rows[0])
        render._draw_explorer(screen, self.snapshot)
        self.assertNotIn("그리지 못함", "\n".join(screen.rows.values()))     # and it recovers


class SelectionFeed(IntegrationEnv):
    """Viewer state -> the selected-scope pump: select + force on a change, poll every tick."""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.lock = threading.Lock()

        def reader(request):
            with self.lock:
                self.calls.append(request.key)
            return FocusResult(request.key, request.epoch, request.snapshot_generation,
                               health=Health("success", time.time()))

        self.feed = render._SelectionFeed(SelectionPump(reader, 0.1))
        self.addCleanup(self.feed.stop)

    def wait(self, predicate, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def tick(self, state, generation=1):
        return self.feed.update(state, generation, time.monotonic())

    def test_a_changed_session_tab_or_stage_selects_and_forces_a_read(self):
        base = ExplorerState(focus="claude:s1")
        self.tick(base)
        self.assertTrue(self.wait(lambda: len(self.calls) == 1))
        for state in (replace(base, tab="cyc"), replace(base, tab="cyc", stage="test"), ExplorerState(focus="claude:s2")):
            count = len(self.calls)
            self.tick(state)
            self.assertTrue(self.wait(lambda: len(self.calls) > count), state)
        self.assertEqual([(k.sid, k.request_id, k.stage_id) for k in self.calls],
                         [("s1", "", ""), ("s1", "cyc", ""), ("s1", "cyc", "test"), ("s2", "", "")])

    def test_an_unchanged_selection_is_only_polled_and_a_hidden_tab_is_never_read(self):
        state = ExplorerState(focus="claude:s1", tab="cyc")
        self.tick(state)
        self.assertTrue(self.wait(lambda: len(self.calls) == 1))
        for _ in range(30):
            self.tick(state)
        self.assertTrue(all(key.request_id == "cyc" for key in self.calls))
        self.assertEqual({key.sid for key in self.calls}, {"s1"})

    def test_a_result_for_another_scope_is_never_returned_and_the_same_session_keeps_its_previous_result(self):
        result, _ = self.tick(ExplorerState(focus="claude:s1"))
        self.assertTrue(self.wait(lambda: self.tick(ExplorerState(focus="claude:s1"))[0].health.state == "success"))
        first, _ = self.tick(ExplorerState(focus="claude:s1"))
        self.assertEqual(first.key.sid, "s1")
        gate = threading.Event()
        slow = SelectionPump(lambda request: (gate.wait(5), FocusResult(request.key, request.epoch,
                                                                      request.snapshot_generation,
                                                                      health=Health("success", 1.0)))[1], 0.1)
        feed = render._SelectionFeed(slow)
        self.addCleanup(feed.stop)
        self.addCleanup(gate.set)
        feed.last = first
        moved, changed = feed.update(ExplorerState(focus="claude:s1", tab="cyc"), 1, time.monotonic())
        self.assertEqual(moved, first)                 # same session: drawn from its previous result
        other, _ = feed.update(ExplorerState(focus="claude:s9"), 1, time.monotonic())
        self.assertEqual(other.health.state, "loading")  # another session: a placeholder, never s1's data
        self.assertEqual(other.key.sid, "s9")

    def test_no_selected_session_means_no_read(self):
        result, _ = self.tick(ExplorerState())
        self.assertIsNone(result)
        self.assertEqual(self.calls, [])


class LineageAndEarlierRequests(IntegrationEnv):
    def test_the_feed_passes_the_open_focused_session_and_a_change_of_it_forces_a_read(self):
        seen, lock = [], threading.Lock()

        def reader(request):
            with lock:
                seen.append(request.expanded_sessions)
            return FocusResult(request.key, request.epoch, request.snapshot_generation,
                               health=Health("success", time.time()))

        feed = render._SelectionFeed(SelectionPump(reader, 0.1))
        self.addCleanup(feed.stop)
        state = ExplorerState(focus="claude:s1")

        def wait(count):
            end = time.monotonic() + 3
            while time.monotonic() < end and len(seen) < count:
                time.sleep(0.01)

        feed.update(state, 1, time.monotonic(), expanded=("claude:s1",))
        wait(1)
        feed.update(state, 1, time.monotonic(), expanded=())            # the session was folded
        wait(2)
        self.assertEqual(seen, [("claude:s1",), ()])

    def test_only_an_open_focused_session_counts_as_expanded(self):
        from tools.fleet.explorer.types import NavRow
        old = render._EXPLORER_NAV_ROWS
        self.addCleanup(setattr, render, "_EXPLORER_NAV_ROWS", old)
        state = ExplorerState(focus="claude:s1")
        render._EXPLORER_NAV_ROWS = (NavRow("session:claude:s1", "session", 2, "x", open=True),)
        self.assertEqual(render._explorer_expanded(state), ("claude:s1",))
        render._EXPLORER_NAV_ROWS = (NavRow("session:claude:s1", "session", 2, "x", open=False),)
        self.assertEqual(render._explorer_expanded(state), ())
        render._EXPLORER_NAV_ROWS = ()                                    # its project is folded: no row at all
        self.assertEqual(render._explorer_expanded(state), ())
        self.assertEqual(render._explorer_expanded(ExplorerState()), ())

    def test_scrolling_up_from_the_first_output_row_asks_for_older_records_once_and_only_then(self):
        from tools.fleet.explorer.types import Frame
        olds = (render._EXPLORER_FRAME, render._EXPLORER_RESULT, render._EXPLORER_EARLIER)
        self.addCleanup(lambda: (setattr(render, "_EXPLORER_FRAME", olds[0]),
                                 setattr(render, "_EXPLORER_RESULT", olds[1]),
                                 setattr(render, "_EXPLORER_EARLIER", olds[2])))
        render._EXPLORER_FRAME = Frame(lines=(), regions=(), scroll_max=(("output:t", 5),))
        more = FocusResult(FocusKey("claude", "s1", "t"), 1, 1, output_more=True)
        render._EXPLORER_RESULT = more

        def asked(state, key):
            render._EXPLORER_EARLIER = False
            render._explorer_note_earlier(state, key)
            return render._EXPLORER_EARLIER

        region = dict(active_region="output", tab="t")
        self.assertFalse(asked(ExplorerState(**region), "up"))                          # following the newest rows
        self.assertFalse(asked(ExplorerState(scrolls=(("output:t", 3),), **region), "up"))
        self.assertTrue(asked(ExplorerState(scrolls=(("output:t", 0),), **region), "up"))   # already at the top
        self.assertTrue(asked(ExplorerState(scrolls=(("output:t", 0),), **region), "pageup"))
        self.assertFalse(asked(ExplorerState(scrolls=(("output:t", 0),), **region), "down"))
        self.assertFalse(asked(ExplorerState(scrolls=(("output:t", 0),), active_region="ledger", tab="t"), "up"))
        render._EXPLORER_RESULT = FocusResult(FocusKey("claude", "s1", "t"), 1, 1, output_more=False)
        self.assertFalse(asked(ExplorerState(scrolls=(("output:t", 0),), **region), "up"))   # nothing older exists


class LiveLoop(IntegrationEnv):
    """The real ``_loop`` on the B surface, with a fake terminal: keys, status updates and an orderly
    stop while the selected-scope reader is still blocked."""

    def test_loop_draws_reads_keys_and_updates_usage_and_gpu_while_the_focus_read_hangs(self):
        self.addCleanup(setattr, render, "_REFRESH_HEALTH", render._REFRESH_HEALTH)
        render.set_explorer_mode(True)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        gpu_calls, snap_calls = [], []

        def blocking(request):
            entered.set()
            release.wait(10)
            return FocusResult(request.key, request.epoch, request.snapshot_generation,
                               health=Health("success", time.time()))

        def collector(harness_filter=None, fast_first=False):
            snap_calls.append(1)
            percent = 29 if len(snap_calls) < 4 else 41
            sessions = [s for s in small_snapshot().sessions if s.session_id in ("s1", "s2", "s3")]
            return LiveSnapshot(sessions=sessions, jobs=[],
                                usage_snapshots={"claude": {"payload": {"rl_5h": percent, "rl_7d": 8}}})

        collector.last_resource_jobs = []
        collector.last_usage_snapshots = {}

        def hosts():
            gpu_calls.append(1)
            busy = len(gpu_calls) > 1
            return {"configured": True, "hosts": [{"host": "gpu1", "reachable": True, "self": False,
                                                   "cpu_utilization_pct": 1, "memory_used_mib": 1,
                                                   "memory_total_mib": 2,
                                                   "gpus": [{"index": 0, "memory_used_mib": 1, "memory_total_mib": 2,
                                                             "processes": [{"pid": 5}] if busy else []}]}]}

        collector.compute_hosts_refresh = hosts
        frames, selections = [], []

        class Screen(FakeScreen):
            step = 0

            def refresh(self_inner):
                frames.append("\n".join(self_inner.rows[k] for k in sorted(self_inner.rows)))

            def timeout(self_inner, _ms):
                pass

            def keypad(self_inner, _flag):
                pass

            def getch(self_inner):
                time.sleep(0.01)
                text = frames[-1] if frames else ""
                seen = "\n".join(frames)
                if "세션 3" not in text:
                    return -1
                if not selections:
                    selections.append(render._EXPLORER_STATE.selection)
                    return curses.KEY_DOWN
                if render._EXPLORER_STATE.selection == selections[0]:
                    return -1
                if "CL 5h41%" in seen and "GPU 1/1" in seen and "GPU 0/1" in seen:
                    return ord("q")
                return -1

        result = []
        with mock.patch.object(render, "_init_colors"), mock.patch.object(render.curses, "curs_set"), \
                mock.patch.object(render, "_malformed", return_value=0), \
                mock.patch.object(render, "_collect_memory", return_value=None), \
                mock.patch.object(render, "_collect_governor", return_value=None), \
                mock.patch.object(render.gitinfo, "enrich_entities"), \
                mock.patch("tools.fleet.explorer.focus.FocusReader", lambda **kwargs: blocking), \
                mock.patch.dict(os.environ, {"FLEET_COMPUTE_HOST_INTERVAL": "1"}):
            worker = threading.Thread(target=lambda: result.append(render._loop(Screen(45, 168), collector,
                                                                               None, "both", 0.1)))
            worker.start()
            worker.join(20.0)
        self.assertFalse(worker.is_alive(), "the loop must reach 'q' while the focus read is blocked")
        self.assertEqual(result, [0])
        self.assertTrue(entered.is_set())
        self.assertFalse(release.is_set())
        joined = "\n".join(frames)
        self.assertIn("CL 5h29%", joined)
        self.assertIn("CL 5h41%", joined)
        self.assertIn("GPU 0/1", joined)
        self.assertIn("GPU 1/1", joined)
        self.assertNotEqual(render._EXPLORER_STATE.selection, selections[0])


if __name__ == "__main__":
    unittest.main()
