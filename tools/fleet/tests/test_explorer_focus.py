import threading
import time
import unittest
from tools.fleet.explorer.focus import SelectionPump
from tools.fleet.explorer.types import FocusKey, FocusResult, Health

class FocusTests(unittest.TestCase):
    def test_result_is_published_only_for_current_focus_epoch(self):
        entered, release = threading.Event(), threading.Event()
        def provider(request):
            entered.set(); release.wait(2)
            return FocusResult(request.key, request.epoch, request.snapshot_generation, output=(request.key.sid,))
        pump = SelectionPump(provider, interval=0.1)
        pump.select(FocusKey("claude", "A"), 1); pump.request()
        self.assertTrue(entered.wait(1))
        pump.select(FocusKey("claude", "B"), 2)
        release.set()
        for _ in range(100):
            pump.poll()
            if not pump._pump.running: break
            time.sleep(.01)
        self.assertIsNone(pump.poll())
        pump.stop()

    def test_reader_marks_first_result_health(self):
        pump = SelectionPump(lambda req: {"output":["summary"]})
        pump.select(FocusKey("codex", "s"), 4); pump.request()
        for _ in range(100):
            result = pump.poll()
            if result: break
            time.sleep(.01)
        self.assertEqual(result.output, ("summary",))
        self.assertEqual(result.health.state, "success")
        pump.stop()
