#!/usr/bin/env python3
from pathlib import Path
import json
import os
import select
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import shared_readiness as READINESS  # noqa: E402
from dispatch_contract import process_start_ticks  # noqa: E402


class SharedReadinessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.start = str(process_start_ticks(os.getpid()))
        self.identity = READINESS.attempt_identity(
            self.root / "jobs.log", "att-one", parent="parent", slug="worker",
            consumer_pid=os.getpid(), consumer_start=self.start,
            request_token="test-request")
        READINESS.publish_runner_incarnation(
            self.root / "peer-steward" / "obligations", os.getpid(), self.start,
            "runner-test")

    def projection(self, *, result=None, now=None, producer_pid=None, producer_start=None):
        pid = os.getpid() if producer_pid is None else producer_pid
        start = self.start if producer_start is None else producer_start
        return READINESS.make_projection(
            self.identity, result or {"state": "ready", "children": []},
            producer_pid=pid, producer_start=start, incarnation="runner-test",
            generation=[1, 2, 3], now=now)

    def generated_normal_authority(self, row_count=72):
        rows = []
        extensions = {f"extension_{index:03d}": f"generated-value-{index:03d}"
                      for index in range(127)}
        for index in range(row_count):
            metadata = {
                "attempt_id": f"att-generated-related-{index:03d}",
                "attempt_schema_version": "2",
                "dispatch_depth": "1",
                "worker_type": "review",
                "registered_worker": "1",
                "execution_surface": "registered-headless",
                "harness": "claude",
                "parent_sid": "generated-parent-session",
                "parent_harness": "claude",
                "route_id": "rt-generated-normal-shape",
                "route_hash": "sha256:" + "a" * 64,
                "route_node": "review",
                "note": "completed-marker",
                "failure_class": "pass",
                "receipt_state": "settled",
                "cleanup_pending": "0",
                "generated_extension_detail": "x" * 2400,
                **extensions,
            }
            raw_metadata = ",".join(f"{key}={value}" for key, value in metadata.items())
            fields = ["2026-10-20T00:00:00Z", "done", "/generated/repo",
                      "/generated/worktree", f"review-{index:03d}", raw_metadata]
            rows.append({"status": "done", "metadata": metadata,
                         "completion_metadata": dict(metadata), "fields": fields})
        return {"rows": rows, "handover_keys": []}

    def test_request_identity_separates_consumer_and_attempt_selection(self):
        other_consumer = READINESS.attempt_identity(
            self.root / "jobs.log", "att-one", parent="parent", slug="worker",
            consumer_pid=os.getpid(), consumer_start=self.start,
            request_token="other-request")
        other_attempt = READINESS.attempt_identity(
            self.root / "jobs.log", "att-two", consumer_pid=os.getpid(),
            consumer_start=self.start, request_token="test-request")
        self.assertNotEqual(READINESS.duty_id(self.identity), READINESS.duty_id(other_consumer))
        self.assertNotEqual(READINESS.duty_id(self.identity), READINESS.duty_id(other_attempt))

    def test_projection_is_small_and_requires_exact_fresh_live_bindings(self):
        value = self.projection(result={
            "state": "pending", "children": [{"attempt_id": "att-one", "readiness": "pending",
                "status": "running", "metadata": {"large": "private row"}}]})
        self.assertTrue(READINESS.projection_is_current(value, self.identity))
        self.assertNotIn("metadata", value["result"]["children"][0])
        self.assertFalse(READINESS.projection_is_current(value, {**self.identity, "request_token": "other"}))
        self.assertFalse(READINESS.projection_is_current(
            value, self.identity, now=value["observed_at"] + READINESS.MAX_AGE_SECONDS + 1))

    def test_target_projection_is_shared_only_for_exact_shell_and_generation(self):
        jobs = self.root / "jobs.log"
        jobs.write_text("registry\n", encoding="utf-8")
        info = jobs.stat()
        generation = {"path": str(jobs), "resolved": str(jobs.resolve()),
                      "signature": [info.st_dev, info.st_ino, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns]}
        identity = READINESS.target_identity(
            server="server-a", endpoint="/tmp/herdr.sock", pane="w1:p1",
            harness="claude", session_id="sid-a", shell_pid=os.getpid(),
            shell_start=self.start, native_state="idle")
        changed = READINESS.publish_target_projection(
            self.root / "peer-steward" / "obligations", identity,
            {"state": "pending", "reason": "bound-registered-work-pending"},
            producer_pid=os.getpid(), producer_start=self.start,
            incarnation="runner-test", generation=generation)
        self.assertTrue(changed)
        result = READINESS.target_result(
            self.root / "peer-steward" / "obligations", identity)
        self.assertEqual(result["state"], "pending")
        jobs.write_text("registry changed\n", encoding="utf-8")
        self.assertIsNone(READINESS.target_result(
            self.root / "peer-steward" / "obligations", identity))

    def test_runner_wake_is_an_event_on_the_existing_state_root(self):
        root = self.root / "peer-steward" / "obligations"
        self.assertFalse(READINESS.wake_runner(root))
        wake_fd = READINESS.open_runner_wake(root)
        self.addCleanup(os.close, wake_fd)
        self.assertTrue(READINESS.wake_runner(root))
        readable, _, _ = select.select([wake_fd], [], [], 0.1)
        self.assertEqual(readable, [wake_fd])
        self.assertEqual(os.read(wake_fd, 4096), b"\0")

    def test_unverifiable_producer_and_oversized_result_never_publish_ready(self):
        bad = self.projection(producer_start="1")
        self.assertFalse(READINESS.projection_is_current(bad, self.identity))
        large = self.projection(result={"state": "ready", "children": [
            {"attempt_id": str(index), "reason": "x" * 100_000} for index in range(20)
        ]})
        self.assertEqual(large["result"]["state"], "unknown")
        self.assertEqual(large["result"]["reason"], "readiness-projection-too-large")

    def test_realistic_selected_authority_round_trips_over_small_result_limit(self):
        authority = self.generated_normal_authority()
        sample = authority["rows"][0]
        encoded = json.dumps(authority, sort_keys=True, separators=(",", ":")).encode("ascii")
        self.assertGreater(len(sample["metadata"]), 64)
        self.assertGreater(len(sample["fields"][5]), 4096)
        self.assertGreater(max(map(len, sample["metadata"].values())), 2048)
        self.assertEqual(sample["metadata"], sample["completion_metadata"])
        self.assertGreater(len(encoded), 65536)
        self.assertLess(len(encoded), READINESS.MAX_WATCH_AUTHORITY_BYTES)
        self.assertGreater(len(authority["rows"]), 64)
        self.assertTrue(READINESS.valid_watch_authority(authority))

        jobs = self.root / "jobs.log"
        jobs.write_text("generated registry fixture\n", encoding="utf-8")
        info = jobs.stat()
        generation = {"path": str(jobs), "resolved": str(jobs.resolve()),
                      "signature": [info.st_dev, info.st_ino, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns]}
        identity = READINESS.target_identity(
            server="fixture", endpoint="/tmp/fixture.sock", pane="w1:p1",
            harness="claude", session_id="sid-generated", shell_pid=os.getpid(),
            shell_start=self.start, native_state="idle")
        root = self.root / "peer-steward" / "obligations"
        changed = READINESS.publish_watch_transition(
            root, "watch-generated-large-authority", identity, "generated-token",
            {"state": "ready", "reason": "native-turn-ready"},
            producer_pid=os.getpid(), producer_start=self.start,
            incarnation="runner-test", generation=generation,
            lineage={"shell_pid": os.getpid(), "shell_start": self.start},
            authority=authority)
        self.assertTrue(changed)
        path = READINESS._watch_transition_path(root, "watch-generated-large-authority")
        self.assertGreater(path.stat().st_size, 65536)
        result = READINESS.watch_transition_result(
            root, "watch-generated-large-authority", identity, "generated-token")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["authority"], authority)

    def test_authority_over_total_transport_bound_stays_unknown(self):
        oversized = {"rows": [{
            "status": "done",
            "metadata": {"generated_extension_detail": "x" * READINESS.MAX_WATCH_AUTHORITY_BYTES},
            "completion_metadata": {"generated_extension_detail": "x" * READINESS.MAX_WATCH_AUTHORITY_BYTES},
            "fields": ["now", "done", "/repo", "/worktree", "review",
                       "generated_extension_detail=" + "x" * READINESS.MAX_WATCH_AUTHORITY_BYTES],
        }], "handover_keys": []}
        self.assertFalse(READINESS.valid_watch_authority(oversized))
        jobs = self.root / "jobs.log"
        jobs.write_text("generated registry fixture\n", encoding="utf-8")
        info = jobs.stat()
        generation = {"path": str(jobs), "resolved": str(jobs.resolve()),
                      "signature": [info.st_dev, info.st_ino, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns]}
        identity = READINESS.target_identity(
            server="fixture", endpoint="/tmp/fixture.sock", pane="w1:p1",
            harness="claude", session_id="sid-generated", shell_pid=os.getpid(),
            shell_start=self.start, native_state="idle")
        root = self.root / "peer-steward" / "obligations"
        READINESS.publish_watch_transition(
            root, "watch-overlarge-authority", identity, "oversized-token",
            {"state": "ready", "reason": "native-turn-ready"},
            producer_pid=os.getpid(), producer_start=self.start,
            incarnation="runner-test", generation=generation,
            lineage={"shell_pid": os.getpid(), "shell_start": self.start},
            authority=oversized)
        result = READINESS.watch_transition_result(
            root, "watch-overlarge-authority", identity, "oversized-token")
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["reason"], "shared-readiness-authority-unbounded")
        self.assertNotIn("authority", result)


if __name__ == "__main__":
    unittest.main()
