"""Initial explorer collection publishes a complete frame or explicit loading state."""
import unittest

from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.view_model import ExplorerViewModel


class InitialReadinessTests(unittest.TestCase):
    def test_first_generation_disables_fast_first_and_later_generations_keep_it(self):
        runtime = ExplorerRuntime()
        self.assertEqual(runtime.collection_options(first_complete=True), {"fast_first": False})
        self.assertEqual(runtime.collection_options(first_complete=False), {"fast_first": True})

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
        render.set_explorer_mode(True)
        self.addCleanup(render.set_explorer_mode, False)
        self.addCleanup(setattr, render, "_EXPLORER_SHOWN", False)
        render._EXPLORER_SHOWN = False

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
