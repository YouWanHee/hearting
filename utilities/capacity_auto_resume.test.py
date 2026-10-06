#!/usr/bin/env python3
"""A usage-limit pause with a known reset time resumes itself once and tells its session."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import capacity_auto_resume as C  # noqa: E402

PAUSE = {"state": "waiting-capacity", "reason": "owner-capacity-wait", "required_action": "resume-after-capacity",
         "route_id": "rt-0123456789abcdef", "retry_at": "2026-10-07T12:00:00Z"}
RETRY_EPOCH = C._epoch(PAUSE["retry_at"])


class ArmTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.jobs = self.root / "jobs.log"
        self.route = self.root / "route.json"
        self.spawned = []

    def arm(self, result=PAUSE, env=None):
        return C.arm(result, self.route, self.jobs, environ=env or {}, now=lambda: RETRY_EPOCH - 3600,
                     spawn=lambda argv, **kw: self.spawned.append((argv, kw["env"])))

    def test_one_resume_per_route_and_reset_time(self):
        armed = self.arm()
        self.assertEqual((armed["state"], armed["resume_at"]), ("armed", PAUSE["retry_at"]))
        self.assertEqual(self.arm()["state"], "already-armed")
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][1][C.CHAIN_ENV], "1")
        record = json.loads(Path(armed["record"]).read_text())
        self.assertEqual((record["state"], record["route_file"]), ("armed", str(self.route.resolve())))

    def test_what_arms_nothing(self):
        for result in ({**PAUSE, "retry_at": None}, {**PAUSE, "reason": "launch-capacity-wait"},
                       {**PAUSE, "state": "running"}):
            with self.subTest(result=result):
                self.assertIsNone(self.arm(result))
        self.assertIsNone(self.arm(env={C.CHAIN_ENV: str(C.MAX_CHAIN)}))  # bounded chain
        self.assertEqual(self.spawned, [])


class RunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "r.json"
        self.path.write_text(json.dumps({"state": "armed", "route_id": PAUSE["route_id"], "retry_at": PAUSE["retry_at"],
                                         "route_file": "/r/route.json", "jobs": str(Path(self._tmp.name) / "jobs.log")}))
        self.clock = [RETRY_EPOCH - 120]

    def sleep(self, seconds):
        self.clock[0] += seconds

    def test_it_waits_past_the_reset_runs_start_once_and_records_it(self):
        calls = []
        receipt = {"state": "running", "owner_attempt_id": "att-1"}
        done = SimpleNamespace(returncode=0, stdout=json.dumps(receipt) + "\n")
        with mock.patch.object(C, "_notify") as notify:
            C.run(self.path, sleep=self.sleep, now=lambda: self.clock[0],
                  call=lambda argv, **kw: calls.append(argv) or done)
        self.assertGreaterEqual(self.clock[0], RETRY_EPOCH + C.SLACK_SECONDS)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2:5], ["start", "--route", "/r/route.json"])
        record = json.loads(self.path.read_text())
        self.assertEqual((record["state"], record["owner_attempt_id"]), ("resumed", "att-1"))
        notify.assert_called_once()
        self.assertEqual(C.run(self.path, sleep=self.sleep, now=lambda: self.clock[0],
                               call=lambda *a, **k: self.fail("resumed twice")), 0)

    def test_a_removed_record_ends_the_wait(self):
        def sleep(seconds):
            self.clock[0] += seconds
            self.path.unlink(missing_ok=True)
        self.assertEqual(C.run(self.path, sleep=sleep, now=lambda: self.clock[0],
                               call=lambda *a, **k: self.fail("ran after removal")), 0)


class ReceiptAndNoticeTest(unittest.TestCase):
    def test_the_start_receipt_says_the_runtime_resumes_it(self):
        import work_start
        armed = {"record": "/x.json", "resume_at": PAUSE["retry_at"], "state": "armed"}
        with mock.patch.object(C, "arm", return_value=armed):
            result = work_start._arm_capacity_resume(dict(PAUSE), "/r/route.json", "/j/jobs.log")
        self.assertEqual((result["parent_next"], result["required_action"], result["auto_resume"]),
                         ("end-turn", "wait-for-auto-resume", armed))
        with mock.patch.object(C, "arm", return_value=None):
            self.assertEqual(work_start._arm_capacity_resume(dict(PAUSE), "/r", "/j"), PAUSE)

    def test_the_owning_session_gets_one_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "jobs.log"
            jobs.write_text("")
            record = {"route_id": PAUSE["route_id"], "retry_at": PAUSE["retry_at"], "jobs": str(jobs)}
            with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "sid-9"}, clear=True):
                C._notify(record, {"state": "waiting-capacity", "retry_at": "2026-10-08T00:00:00Z"})
                C._notify(record, {"state": "waiting-capacity", "retry_at": "2026-10-08T00:00:00Z"})
            import dispatch_session_sweep as sweep
            claimed, _ = sweep.sweep_deliver(jobs.parent, "claude-parent-runtime", "sid-9")
            self.assertEqual(len(claimed), 1)
            text = sweep.delivery_context([(jobs.parent, claimed)])
            self.assertIn("paused again until 2026-10-08T00:00:00Z", text)


if __name__ == "__main__":
    unittest.main()
