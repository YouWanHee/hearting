#!/usr/bin/env python3
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
