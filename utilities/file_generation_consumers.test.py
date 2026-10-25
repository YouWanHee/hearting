from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import runtime_observation as RO

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dispatch_orphan_watch", ROOT / "utilities" / "dispatch-orphan-watch.py")
WATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WATCH)


class FileGenerationConsumerTest(unittest.TestCase):
    def frozen_stats(self, path):
        info = path.stat()
        return type("FrozenStat", (), {
            "st_dev": info.st_dev, "st_ino": info.st_ino, "st_size": info.st_size,
            "st_mtime_ns": info.st_mtime_ns, "st_ctime_ns": info.st_ctime_ns,
            "st_mode": stat.S_IFREG | 0o600,
        })()

    def test_orphan_watch_reloads_exact_row_for_same_tuple_rewrite(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            jobs = Path(tmp) / "jobs.log"
            jobs.write_text("old row\n", encoding="utf-8")
            frozen = self.frozen_stats(jobs)
            real_fstat = RO.os.fstat
            def fstat(fd):
                actual = real_fstat(fd)
                return frozen if actual.st_ino == frozen.st_ino else actual
            observed = []
            watch = WATCH.RegistryObservation(jobs, "att-fixture")
            with mock.patch.object(RO.Path, "stat", return_value=frozen), \
                 mock.patch.object(RO.os, "fstat", side_effect=fstat), \
                 mock.patch.object(WATCH, "attempt_record",
                                   side_effect=[("open", {"attempt_id": "att-fixture"}),
                                                ("running", {"attempt_id": "att-fixture"})]) as read:
                close = SimpleNamespace(row_requested=lambda metadata, jobs: False)
                observed.append(watch._refresh_scoped(force=False, route_parent_close=close))
                jobs.write_text("new row\n", encoding="utf-8")
                observed.append(watch._refresh_scoped(force=False, route_parent_close=close))
            self.assertEqual(read.call_count, 2)
            self.assertEqual([row[0] for row in observed], ["open", "running"])

    def test_artifact_projection_context_reloads_same_stat_small_record(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            path = Path(tmp) / "record.json"
            path.write_text('{"value":1}', encoding="utf-8")
            frozen = self.frozen_stats(path)
            real_fstat = RO.os.fstat
            def fstat(fd):
                actual = real_fstat(fd)
                return frozen if actual.st_ino == frozen.st_ino else actual
            context = RO.ObservationContext()
            loader = lambda: json.loads(RO.stable_read_bytes(path))
            with mock.patch.object(RO.Path, "stat", return_value=frozen), \
                 mock.patch.object(RO.os, "fstat", side_effect=fstat):
                self.assertEqual(context.memoize("manifest", [path], loader), {"value": 1})
                path.write_text('{"value":2}', encoding="utf-8")
                self.assertEqual(context.memoize("manifest", [path], loader), {"value": 2})
            self.assertEqual(context.metrics_snapshot()["loads"], 2)


if __name__ == "__main__":
    unittest.main()
