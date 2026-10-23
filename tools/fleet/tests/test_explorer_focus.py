"""Selection pump and bounded source-cache contracts retained by the explorer reader."""

import json
import os
import tempfile
import threading
import time
import unittest

from tools.fleet.explorer.focus import SelectionPump, SourceCache
from tools.fleet.explorer.types import FocusKey, FocusResult, Health


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


class GatedProvider:
    def __init__(self):
        self.requests = []
        self.entered = threading.Semaphore(0)
        self.gate = threading.Semaphore(0)

    def __call__(self, request):
        self.requests.append(request)
        self.entered.release()
        self.gate.acquire(timeout=3)
        return FocusResult(request.key, request.epoch, request.snapshot_generation,
                           output=(request.key.sid,), health=Health("success", time.time()))


class SelectionPumpContracts(unittest.TestCase):
    def test_late_result_for_previous_selection_is_dropped(self):
        provider = GatedProvider()
        pump = SelectionPump(provider, interval=0.1)
        a, b = FocusKey("claude", "A"), FocusKey("claude", "B")
        try:
            pump.select(a, 1)
            pump.request()
            self.assertTrue(provider.entered.acquire(timeout=2))
            pump.select(b, 2)
            pump.select(a, 3)
            pump.request()
            provider.gate.release()
            self.assertTrue(provider.entered.acquire(timeout=2))
            self.assertIsNone(pump.poll())
            self.assertEqual([item.epoch for item in provider.requests], [1, 3])
            provider.gate.release()
            result = wait_for(pump.poll)
            self.assertEqual((result.key, result.epoch), (a, 3))
        finally:
            provider.gate.release()
            pump.stop()

    def test_selections_while_reading_coalesce_to_latest(self):
        provider = GatedProvider()
        pump = SelectionPump(provider, interval=0.1)
        try:
            pump.select(FocusKey("claude", "A"), 1)
            pump.request()
            self.assertTrue(provider.entered.acquire(timeout=2))
            for sid in ("B", "C", "D"):
                pump.select(FocusKey("claude", sid), 1)
            pump.request()
            provider.gate.release()
            self.assertTrue(provider.entered.acquire(timeout=2))
            self.assertEqual([item.key.sid for item in provider.requests], ["A", "D"])
            provider.gate.release()
            self.assertEqual(wait_for(pump.poll).key.sid, "D")
        finally:
            provider.gate.release()
            pump.stop()


class SourceCacheContracts(unittest.TestCase):
    def test_unchanged_file_is_read_once_and_cache_is_bounded(self):
        cache = SourceCache(limit=2)
        with tempfile.TemporaryDirectory() as root:
            paths = [os.path.join(root, f"{index}.json") for index in range(3)]
            for index, path in enumerate(paths):
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump({"value": index}, stream)
            self.assertEqual(cache.read_json(paths[0]), {"value": 0})
            reads = cache.reads
            self.assertEqual(cache.read_json(paths[0]), {"value": 0})
            self.assertEqual(cache.reads, reads)
            cache.read_json(paths[1])
            cache.read_json(paths[2])
            self.assertLessEqual(len(cache._items), 2)
            self.assertIsNone(cache.read_json(os.path.join(root, "missing.json")))

    def test_changed_file_is_read_again(self):
        cache = SourceCache()
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "record.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"value": 1}, stream)
            self.assertEqual(cache.read_json(path), {"value": 1})
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"value": 2}, stream)
            self.assertEqual(cache.read_json(path), {"value": 2})
            self.assertEqual(cache.reads, 2)


if __name__ == "__main__":
    unittest.main()
