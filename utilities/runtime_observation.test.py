#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from runtime_observation import (ObservationContext, file_generation,
                                stable_read_bytes)  # noqa: E402


class ObservationContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "input.json"
        self.path.write_text('{"value": 1}', encoding="utf-8")
        self.context = ObservationContext(max_entries=2, max_weight=8)

    def test_reuses_projection_for_same_dependencies(self):
        loads = []
        loader = lambda: loads.append(1) or ("verified", 7)
        key = (str(self.path), "cycle-a")
        self.assertEqual(self.context.memoize(key, [self.path], loader), ("verified", 7))
        self.assertEqual(self.context.memoize(key, [self.path], loader), ("verified", 7))
        self.assertEqual(len(loads), 1)
        self.assertEqual(self.context.metrics_snapshot()["hits"], 1)

    def test_same_size_rewrite_with_restored_mtime_invalidates(self):
        key = "same-size"
        old = self.path.stat().st_mtime_ns
        first = file_generation(self.path)
        self.context.memoize(key, [self.path], lambda: "old")
        self.path.write_text('{"value": 2}', encoding="utf-8")
        os.utime(self.path, ns=(old, old))
        self.assertNotEqual(first, file_generation(self.path))
        self.assertEqual(self.context.memoize(key, [self.path], lambda: "new"), "new")

    def test_missing_dependency_does_not_reuse_old_projection(self):
        key = "missing"
        self.context.memoize(key, [self.path], lambda: "old")
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.context.memoize(key, [self.path], lambda: "incorrect")
        self.assertEqual(self.context.metrics_snapshot()["entries"], 0)

    def test_lru_and_weight_are_bounded(self):
        for number in range(4):
            self.context.memoize(number, [self.path], lambda number=number: number,
                                 weight=3)
        metrics = self.context.metrics_snapshot()
        self.assertLessEqual(metrics["entries"], 2)
        self.assertLessEqual(metrics["weight"], 8)
        self.assertGreater(metrics["evictions"], 0)

    def test_loader_change_does_not_publish_under_old_generation(self):
        calls = []

        def loader():
            calls.append(1)
            if len(calls) == 1:
                self.path.write_text('{"value": 3}', encoding="utf-8")
            return len(calls)

        with self.assertRaisesRegex(OSError, "dependency-changed"):
            self.context.memoize("racing", [self.path], loader)
        self.assertEqual(self.context.memoize("racing", [self.path], loader), 2)

    def test_stable_reader_returns_exact_bytes_and_observer_state_survives_checkpoint(self):
        self.assertEqual(stable_read_bytes(self.path), b'{"value": 1}')
        progress = self.context.state("join-checkpoint", list)
        progress.append("settlement-backoff")
        self.assertIs(progress, self.context.state("join-checkpoint", list))
        self.assertEqual(self.context.metrics_snapshot()["state_hits"], 1)


if __name__ == "__main__":
    unittest.main()
