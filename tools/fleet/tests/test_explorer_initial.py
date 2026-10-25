"""Initial explorer collection publishes a complete frame or explicit loading state."""
import unittest

from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.view_model import ExplorerViewModel


class InitialReadinessTests(unittest.TestCase):
    def test_basic_observation_stays_fast_after_and_before_first_complete(self):
        runtime = ExplorerRuntime()
        self.assertEqual(runtime.collection_options(first_complete=True),
                         {"fast_first": True, "include_report_verification": False})
        self.assertEqual(runtime.collection_options(first_complete=False),
                         {"fast_first": True, "include_report_verification": False})

    def test_only_explicit_pending_sources_delay_readiness_and_model_is_not_rebuilt(self):
        runtime = ExplorerRuntime()
        vm = ExplorerViewModel.from_mapping({"identity": "same"})
        pending = {"observations": {"usage": {"state": "pending"},
                                     "models": {"state": "unavailable"}}}
        prepared, readiness = runtime.prepare(pending, None, vm=vm, generation=0, now=1.0)
        # The model is the one already built (never rebuilt); prepare may only add its ordering record.
        self.assertEqual({key: value for key, value in prepared.to_mapping().items() if key != "ordering"},
                         {key: value for key, value in vm.to_mapping().items() if key != "ordering"})
        self.assertEqual(readiness["pending"], ("usage",))
        self.assertEqual(readiness["sources"]["models"]["state"], "unavailable")
        self.assertFalse(readiness["ready"])
        frame = runtime.loading_frame(40, 10)
        self.assertEqual((frame.cols, frame.rows), (40, 10))
        self.assertIn("불러오는 중", frame.text_rows()[5])
        _, completed = runtime.prepare({"observations": {"usage": {"state": "unavailable"}}}, None,
                                       vm=vm, generation=1, now=2.0)
        self.assertTrue(completed["ready"])

    def test_production_requires_basic_detail_usage_and_attempted_remote_observations(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from tools.fleet.explorer import order

        with tempfile.TemporaryDirectory() as temp:
            runtime = ExplorerRuntime()
            runtime.configure_persistence({"codex"}, strict_readiness=True)
            observations = {
                "basic": {"state": "complete", "observed_at": 10,
                          "generation_identity": "g-1"},
                "required-details": {"state": "pending"},
                "usage:codex": {"state": "complete", "observed_at": 10},
                "remote-host": {"state": "pending", "attempted": False},
            }
            snapshot = {"observations": observations}
            vm = ExplorerViewModel.from_mapping({})
            with patch.object(order, "order_path", return_value=Path(temp) / "order.json"):
                _model, pending = runtime.prepare(snapshot, None, vm=vm, generation=1, now=10)
                self.assertFalse(pending["ready"])
                self.assertEqual(pending["pending"], ("remote-host", "required-details"))
                observations["required-details"] = {
                    "state": "complete", "generation_identity": "g-1", "all_new": False}
                observations["remote-host"] = {"state": "failed", "attempted": True}
                _model, continued = runtime.prepare(snapshot, None, vm=vm, generation=1, now=10)
                observations["required-details"] = {
                    "state": "complete", "generation_identity": "other", "all_new": True}
                _model, mismatched = runtime.prepare(snapshot, None, vm=vm, generation=1, now=10)
            self.assertTrue(continued["ready"])
            self.assertFalse(continued["all_new"])
            self.assertFalse(mismatched["ready"])
            self.assertIn("required-details", mismatched["pending"])

    def test_strict_readiness_does_not_treat_missing_required_sources_as_complete(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from tools.fleet.explorer import order

        runtime = ExplorerRuntime(strict_readiness=True)
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(order, "order_path", return_value=Path(temp) / "order.json"):
            _model, readiness = runtime.prepare({"observations": {}}, None,
                                                vm=ExplorerViewModel.from_mapping({}),
                                                generation=0, now=1)
        self.assertFalse(readiness["ready"])
        self.assertEqual(set(readiness["pending"]),
                         {"basic", "required-details", "usage:claude", "usage:codex",
                          "usage:opencode", "remote-host"})


class DetailCallbackSelection(unittest.TestCase):
    def test_required_callback_precedes_legacy_callback_and_optional_uses_legacy(self):
        from types import SimpleNamespace
        from tools.fleet import render

        calls = []
        collector = SimpleNamespace(
            required_detail_refresh=lambda source: calls.append(("required", source)),
            detail_refresh=lambda source: calls.append(("full", source)),
        )
        required, is_required = render._explorer_detail_callback(collector)
        optional, optional_is_required = render._explorer_detail_callback(
            collector, optional=True)

        self.assertTrue(is_required)
        self.assertFalse(optional_is_required)
        required("basic")
        optional("complete")
        self.assertEqual(calls, [("required", "basic"), ("full", "complete")])


class _Screen:
    def __init__(self, rows=45, cols=140):
        self.size, self.cells = (rows, cols), {}

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.cells = {}

    def addstr(self, y, x, text, *_attr):
        self.cells[(y, x)] = text

    def refresh(self):
        pass

    def text(self):
        return "".join(self.cells[key] for key in sorted(self.cells))


class LoadingGate(unittest.TestCase):
    """The full-screen loading frame stands only before the first complete frame."""

    def setUp(self):
        from tools.fleet import render
        from tools.fleet.tests import explorer_fixture as ef
        self.render = render
        self.snapshot, hosts = ef.demo_snapshot()
        self.snapshot.compute_hosts = hosts
        self.addCleanup(setattr, render, "_EXPLORER_MODEL", render._EXPLORER_MODEL)
        self.addCleanup(setattr, render, "_EXPLORER_READINESS", render._EXPLORER_READINESS)
        render.set_explorer_mode(True)
        self.addCleanup(render.set_explorer_mode, False)
        self.addCleanup(setattr, render, "_EXPLORER_SHOWN", False)
        self.addCleanup(setattr, render, "_EXPLORER_PUBLISHED_MODEL", None)
        self.addCleanup(setattr, render, "_EXPLORER_REPLAY_ACTIVE", False)
        self.addCleanup(setattr, render, "_EXPLORER_MODEL", None)
        render._EXPLORER_SHOWN = False
        render._EXPLORER_PUBLISHED_MODEL = None
        render._EXPLORER_MODEL = None
        render._EXPLORER_REPLAY_ACTIVE = False

    def draw(self, *, loading, pending=False):
        self.snapshot.observations = {"usage": {"state": "pending"}} if pending else {}
        screen = _Screen()
        self.render._draw(screen, self.snapshot.sessions, self.snapshot.jobs, "all", 0,
                          loading=loading, snapshot=self.snapshot)
        return screen.text()

    def test_no_collection_yet_is_loading_not_an_empty_fleet(self):
        self.assertIn("불러오는 중", self.draw(loading=True))

    def test_a_pending_source_after_the_first_frame_keeps_the_frame(self):
        self.assertIn("불러오는 중", self.draw(loading=False, pending=True))
        self.assertNotIn("불러오는 중", self.draw(loading=False))
        self.assertNotIn("불러오는 중", self.draw(loading=False, pending=True))

    def test_cached_frame_skips_live_composition_before_first_collection(self):
        from unittest.mock import patch
        from tools.fleet.explorer.view_model import ExplorerViewModel

        cached = ExplorerViewModel.from_mapping({"repos": [{"id": "cached", "name": "cached"}]})
        self.render._EXPLORER_PUBLISHED_MODEL = cached
        self.render._EXPLORER_MODEL = cached
        self.render._EXPLORER_REPLAY_ACTIVE = True
        self.render._EXPLORER_READINESS = {"ready": False, "pending": ("basic",)}
        self.render._EXPLORER_STATE = None
        screen = _Screen()
        with patch.object(self.render, "_explorer_compose",
                          side_effect=AssertionError("replay must not compose live data")):
            self.render._draw(screen, self.snapshot.sessions, self.snapshot.jobs, "all", 0,
                              loading=True, snapshot=self.snapshot)
        self.assertNotIn("불러오는 중", screen.text())
        self.assertIsNotNone(self.render._EXPLORER_STATE)
        self.assertEqual(self.render._EXPLORER_STATE.view, "OVERVIEW")

    def test_working_marks_turn_at_draw_time_and_the_frame_keeps_the_still_glyph(self):
        from unittest import mock
        from tools.fleet.explorer.runtime import ExplorerRuntime
        frame = ExplorerRuntime().loading_frame(40, 10)
        self.assertIn("⠏", frame.text_rows()[5])
        glyphs = set()
        for tick in range(10):
            screen = _Screen(10, 40)
            with mock.patch.object(self.render.time, "time", return_value=tick / 10):
                self.render._draw_explorer_frame(screen, frame, self.snapshot)
            glyphs.update(ch for ch in screen.text() if "⠀" <= ch <= "⣿")
        self.assertEqual(len(glyphs), 10)


class RemoteHostObservation(unittest.TestCase):
    def test_compute_host_pump_result_records_success_failure_or_confirmed_absence(self):
        from tools.fleet.render import _explorer_remote_host_observation

        success = _explorer_remote_host_observation({
            "configured": True, "observed_at": 10,
            "hosts": [{"self": True, "reachable": True},
                      {"self": False, "reachable": True}],
        })
        failed = _explorer_remote_host_observation({
            "configured": True, "observed_at": 11,
            "hosts": [{"self": False, "reachable": False}],
        })
        absent = _explorer_remote_host_observation({
            "configured": False, "status": "missing", "observed_at": 12, "hosts": [],
        })
        self.assertEqual((success["state"], success["attempted"]), ("success", True))
        self.assertEqual((failed["state"], failed["attempted"]), ("failed", True))
        self.assertEqual((absent["state"], absent["attempted"]), ("absent", True))


class StartupOrdering(unittest.TestCase):
    def test_cached_draw_happens_before_collection_and_metadata_pumps_start(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.fleet import render

        events = []

        class Runtime:
            has_complete = False
            cached_continuation = None
            def configure_persistence(self, *_args, **_kwargs):
                pass
            def load_last_complete(self, *_args, **_kwargs):
                return object()

        class Pump:
            def __init__(self, _producer, _interval, **kwargs):
                self.name = kwargs.get("name", "fleet-refresh")
                self.generation = 0
                self.running = False
            def start(self):
                events.append("start:" + self.name)
                self.running = True
            def health(self, *_args, **_kwargs):
                return {}
            def request_due(self, **_kwargs):
                return False
            def request(self, **_kwargs):
                return False
            def poll(self, *_args, **_kwargs):
                return None
            def stop(self, **_kwargs):
                pass

        class Screen:
            def getmaxyx(self):
                return 45, 168
            def timeout(self, _value):
                pass
            def keypad(self, _value):
                pass

        class Reader:
            def __init__(self, curses_mod, *_args, **_kwargs):
                self.error = curses_mod.ERR
                self.calls = 0
            def read(self, *_args, **_kwargs):
                self.calls += 1
                return (self.error if self.calls == 1 else ord("q")), None
            def has_buffered_input(self):
                return False

        collector = lambda **_kwargs: None
        collector.install_follower = SimpleNamespace(target=lambda: None)
        collector.compute_hosts_refresh = lambda: None
        prior_mode = render._EXPLORER_ENABLED
        prior_published = render._EXPLORER_PUBLISHED_MODEL
        prior_model = render._EXPLORER_MODEL
        prior_readiness = render._EXPLORER_READINESS
        prior_replay = render._EXPLORER_REPLAY_ACTIVE
        prior_shown = render._EXPLORER_SHOWN
        self.addCleanup(render.set_explorer_mode, prior_mode)
        self.addCleanup(setattr, render, "_EXPLORER_PUBLISHED_MODEL", prior_published)
        self.addCleanup(setattr, render, "_EXPLORER_MODEL", prior_model)
        self.addCleanup(setattr, render, "_EXPLORER_READINESS", prior_readiness)
        self.addCleanup(setattr, render, "_EXPLORER_REPLAY_ACTIVE", prior_replay)
        self.addCleanup(setattr, render, "_EXPLORER_SHOWN", prior_shown)
        render.set_explorer_mode(True)

        def record_draw(*_args, **_kwargs):
            self.assertTrue(render._EXPLORER_REPLAY_ACTIVE)
            events.append("draw")

        with patch.object(render, "_explorer_runtime", return_value=Runtime()), \
                patch.object(render, "_init_colors"), \
                patch.object(render, "_configure_input"), \
                patch.object(render, "_CursesInput", Reader), \
                patch.object(render, "RefreshPump", Pump), \
                patch.object(render, "_ExplorerFeed", return_value=None), \
                patch("tools.fleet.explorer.focus.FocusReader", return_value=object()), \
                patch.object(render, "set_refresh_health"), \
                patch.object(render.curses, "curs_set"), \
                patch.object(render, "_draw", side_effect=record_draw), \
                patch.object(render.os, "write", return_value=0):
            render._loop(Screen(), collector, None, "both", 2.0)

        draw_index = events.index("draw")
        starts = [index for index, event in enumerate(events) if event.startswith("start:")]
        self.assertTrue(starts)
        self.assertTrue(all(draw_index < index for index in starts), events)
