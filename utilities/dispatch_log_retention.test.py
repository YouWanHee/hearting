"""Isolated safety and cadence checks for dispatch log retention."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_log_retention as R  # noqa: E402
import dispatch_observation_log as OBS  # noqa: E402
import dispatch_contract as DC  # noqa: E402
import dispatch_pending_delivery as PD  # noqa: E402

DAY = 86400 * 1_000_000_000


def stamp(value):
    return datetime.fromtimestamp(value / 1_000_000_000, timezone.utc).isoformat().replace("+00:00", "Z")


class DispatchLogRetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dispatch-log-retention-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.jobs = self.root / "jobs.log"
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.now = time.time_ns()
        self.rows = []

    def add_row(self, attempt, log_file, *, status="done", ended=None, harness="claude",
                worker_type="stage", **extra):
        meta = {"attempt_id": attempt, "harness": harness, "worker_type": worker_type,
                "launch_outcome": "never-launched", "log_file": str(log_file)}
        meta.update(extra)
        self.rows.append(f"{stamp(ended if ended is not None else self.now)}\t{status}\t/repo\t/wt\tjob\t"
                         + ",".join(f"{key}={value}" for key, value in meta.items()))
        self.jobs.write_text("\n".join(self.rows) + "\n", encoding="utf-8")

    def log(self, name, body=b"transcript\n"):
        path = self.logs / name
        path.write_bytes(body)
        return path

    def sweep(self, *, at=None):
        return R.sweep(self.jobs, now_ns=self.now if at is None else at, force=True)

    def test_only_verified_logs_older_than_fourteen_days_are_removed(self):
        old = self.log("old.jsonl")
        recent = self.log("recent.jsonl")
        self.add_row("att-old-log", old, ended=self.now - 15 * DAY)
        self.add_row("att-recent-log", recent, ended=self.now - 13 * DAY)

        result = self.sweep()

        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual((result["removed"], result["eligible"]), (1, 2))
        self.assertEqual(result["over_limit_bytes"], 0)

    def test_live_unsettled_review_and_unattributable_logs_are_kept(self):
        live = self.log("live.jsonl")
        review = self.log("review.jsonl")
        delivery = self.log("delivery.jsonl")
        unbound = self.log("unbound.jsonl")
        outside = self.root / "outside.jsonl"
        outside.write_text("outside\n", encoding="utf-8")
        self.add_row("att-live-log", live, status="open", ended=self.now - 30 * DAY)
        self.add_row("att-review-log", review, ended=self.now - 30 * DAY,
                     worker_type="review", failure_class="fail")
        self.add_row("att-delivery-log", delivery, ended=self.now - 30 * DAY,
                     delivery_intent="1", delivery_id="delivery-" + "a" * 32,
                     delivery_recipient_digest=PD.recipient_digest("sess-log-fixture"),
                     parent_sid="sess-log-fixture", parent_attempt_id="att-parent",
                     dispatch_depth="2", route_id="rt-" + "a" * 16, route_node="execute")
        self.add_row("att-outside-log", outside, ended=self.now - 30 * DAY)

        with mock.patch.object(R, "MAX_LOG_BYTES", 1):
            result = self.sweep()

        self.assertEqual(result["removed"], 0)
        self.assertTrue(all(path.exists() for path in (live, review, delivery, unbound, outside)))
        self.assertGreater(result["protected_bytes"], 0)
        self.assertGreater(result["unknown_bytes"], 0)
        self.assertGreater(result["over_limit_bytes"], 0)
        self.assertEqual(result["kept_reasons"].get("delivery-pending"), 1)
        self.assertEqual(result["kept_reasons"].get("review-consumer-open"), 1)

    def test_observation_logs_are_not_attempt_log_candidates(self):
        for index, name in enumerate(("dispatch-session-sweep.log", "dispatch-pending-delivery.log")):
            path = self.log(name)
            self.add_row(f"att-observation-{index}", path, ended=self.now - 30 * DAY)

        result = self.sweep()

        self.assertEqual(result["removed"], 0)
        self.assertTrue((self.logs / "dispatch-session-sweep.log").exists())
        self.assertTrue((self.logs / "dispatch-pending-delivery.log").exists())

    def test_live_and_unverifiable_processes_keep_attempt_logs(self):
        live = self.log("live-process.jsonl")
        unknown = self.log("unknown-process.jsonl")
        old = self.now - 30 * DAY
        self.add_row("att-live-process", live, ended=old)
        self.add_row("att-unknown-process", unknown, ended=old)

        def quiescence(meta, *, terminal_receipt=False):
            if meta["attempt_id"] == "att-live-process":
                return DC.ProcessQuiescence("live", "attempt-descendant-live")
            return DC.ProcessQuiescence("unverifiable", "process-namespace-unverifiable")

        with mock.patch.object(DC, "attempt_process_quiescence", side_effect=quiescence):
            result = self.sweep()
        self.assertEqual(result["removed"], 0)
        self.assertTrue(live.exists() and unknown.exists())
        self.assertEqual(result["kept_reasons"].get("not-quiescent"), 1)
        self.assertEqual(result["kept_reasons"].get("quiescence-unverifiable"), 1)

    def test_shared_log_waits_for_every_bound_attempt(self):
        shared = self.log("shared.jsonl")
        old = self.now - 30 * DAY
        self.add_row("att-shared-done", shared, ended=old)
        self.add_row("att-shared-live", shared, status="running", ended=old)

        result = self.sweep()

        self.assertEqual(result["removed"], 0)
        self.assertTrue(shared.exists())

    def test_symlinks_and_hardlinks_are_never_unlinked(self):
        target = self.root / "target.jsonl"
        target.write_text("target\n", encoding="utf-8")
        symlink = self.logs / "linked.jsonl"
        symlink.symlink_to(target)
        hard_source = self.log("hard-source.jsonl")
        hard = self.logs / "hard.jsonl"
        os.link(hard_source, hard)
        old = self.now - 30 * DAY
        self.add_row("att-symlink", symlink, ended=old)
        self.add_row("att-hardlink", hard, ended=old)

        result = self.sweep()

        self.assertEqual(result["removed"], 0)
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(hard.exists())
        self.assertTrue(hard_source.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "target\n")

    def test_size_pressure_removes_oldest_eligible_log_first(self):
        old = self.log("oldest.jsonl", b"o" * 8)
        recent = self.log("recent.jsonl", b"r" * 8)
        self.add_row("att-oldest", old, ended=self.now - 2 * DAY)
        self.add_row("att-recent", recent, ended=self.now - DAY)

        with mock.patch.object(R, "MAX_LOG_BYTES", 10):
            result = self.sweep()

        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(result["removed_bytes"], 8)
        self.assertEqual(result["over_limit_bytes"], 0)

    def test_interrupted_unlink_retries_on_the_next_normal_pass(self):
        old = self.log("retry.jsonl")
        self.add_row("att-retry-log", old, ended=self.now - 30 * DAY)
        with mock.patch.object(R, "_unlink_if_same", return_value=False):
            first = self.sweep()
        self.assertEqual(first["removed"], 0)
        self.assertTrue(old.exists())

        second = self.sweep()
        self.assertEqual(second["removed"], 1)
        self.assertFalse(old.exists())

    def test_fair_cursor_visits_more_than_one_bounded_batch(self):
        attempts = []
        for index in range(70):
            attempt = f"att-cursor-{index:03d}"
            path = self.log(f"cursor-{index:03d}.jsonl")
            self.add_row(attempt, path, ended=self.now - DAY)
            attempts.append(attempt)
        seen = []
        actual = R._log_protection

        def record(ctx, attempt, *, probe_processes):
            seen.append(attempt)
            return actual(ctx, attempt, probe_processes=probe_processes)

        with mock.patch.object(R, "_log_protection", side_effect=record):
            first = self.sweep()
            second = self.sweep()
        self.assertEqual((first["examined"], second["examined"]), (64, 64))
        self.assertEqual(set(seen), set(attempts))

    def test_inode_replacement_between_scan_and_unlink_is_preserved(self):
        path = self.log("swap.jsonl")
        info = os.stat(path, follow_symlinks=False)
        expected = R._stamp(info)
        replacement = self.logs / "replacement.jsonl"
        replacement.write_text("replacement\n", encoding="utf-8")
        os.replace(replacement, path)

        with R._LogLayout(self.root) as layout:
            self.assertFalse(R._unlink_if_same(layout, path.name, expected))
        self.assertEqual(path.read_text(encoding="utf-8"), "replacement\n")


class ObservationLogRotationTest(unittest.TestCase):
    def test_concurrent_appends_rotate_only_complete_json_records(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-observation-") as td:
            root = Path(td)
            with mock.patch.object(OBS, "MAX_ACTIVE_BYTES", 160), \
                    mock.patch.object(OBS, "MAX_RETAINED_BYTES", 100_000):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(
                        lambda index: OBS.append_record(root, "dispatch-session-sweep.log",
                                                        {"record": index, "payload": "x" * 18}),
                        range(120)))
            self.assertTrue(all(results))
            files = [root / "logs" / name for name in os.listdir(root / "logs")
                     if name.startswith("dispatch-session-sweep.log")
                     and not name.endswith(".lock")]
            records = []
            for path in files:
                for line in path.read_text(encoding="utf-8").splitlines():
                    records.append(json.loads(line))
            self.assertEqual(sorted(row["record"] for row in records), list(range(120)))

    def test_first_append_safely_rotates_existing_backlog_and_prunes_old_segments(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-observation-") as td:
            root = Path(td)
            logs = root / "logs"
            logs.mkdir()
            active = logs / "dispatch-pending-delivery.log"
            active.write_bytes(b"old backlog\n" * 20)
            old_segment = logs / "dispatch-pending-delivery.log.segment.old"
            old_segment.write_text("expired segment\n", encoding="utf-8")
            expired = time.time_ns() - 20 * DAY
            os.utime(old_segment, ns=(expired, expired))

            with mock.patch.object(OBS, "MAX_ACTIVE_BYTES", 64):
                self.assertTrue(OBS.append_record(root, "dispatch-pending-delivery.log",
                                                  {"event": "new"}))

            self.assertFalse(old_segment.exists())
            segments = list(logs.glob("dispatch-pending-delivery.log.segment.*"))
            self.assertTrue(any(b"old backlog" in path.read_bytes() for path in segments))
            self.assertEqual(json.loads(active.read_text(encoding="utf-8")), {"event": "new"})


if __name__ == "__main__":
    unittest.main()
