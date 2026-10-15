#!/usr/bin/env python3
"""A usage-limit pause with a known reset time, or an owner launch that did not start,
resumes itself once and tells its session."""
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

    def test_the_record_names_the_paused_harness_attempt_and_usage_state(self):
        armed = self.arm({**PAUSE, "harness": "claude", "source_attempt_id": "att-dead",
                          "usage_state": "allocation-usage-gate"})
        record = json.loads(Path(armed["record"]).read_text())
        self.assertEqual((record["harness"], record["source_attempt_id"], record["usage_state"]),
                         ("claude", "att-dead", "allocation-usage-gate"))

    def test_what_arms_nothing(self):
        for result in ({**PAUSE, "retry_at": None}, {**PAUSE, "reason": "launch-capacity-wait"},
                       {**PAUSE, "state": "running"}):
            with self.subTest(result=result):
                self.assertIsNone(self.arm(result))
        self.assertIsNone(self.arm(env={C.CHAIN_ENV: str(C.MAX_CHAIN)}))  # bounded chain
        self.assertEqual(self.spawned, [])


    def test_a_launch_that_did_not_start_is_run_again_a_minute_later(self):
        for index, reason in enumerate(C.LATER_REASONS):
            with self.subTest(reason=reason):
                result = {"state": "needs-attention", "reason": reason, "required_action": "resume-later",
                          "route_id": f"rt-{index}"}
                armed = self.arm(result)
                self.assertEqual((armed["state"], armed["cause"]), ("armed", "launch-not-started"))
                self.assertEqual(C._epoch(armed["resume_at"]), RETRY_EPOCH - 3600 + C.LATER_SECONDS)
                self.assertEqual(json.loads(Path(armed["record"]).read_text())["cause"], "launch-not-started")
        # The same reason without the resume-later remedy is a diagnostic, not a pause.
        self.assertIsNone(self.arm({"state": "needs-attention", "reason": "owner-launch-not-admitted",
                                    "route_id": "rt-x"}))
        self.assertIsNone(self.arm({"state": "needs-attention", "reason": "owner-launch-not-started",
                                    "required_action": "resume-later", "route_id": "rt-y"},
                                   env={C.CHAIN_ENV: str(C.MAX_CHAIN)}))

    def test_an_answer_awaiting_the_parent_is_started_at_once(self):
        armed = self.arm({"reason": C.ANSWER_REASON, "route_id": "rt-answer"})
        self.assertEqual((armed["state"], armed["cause"]), ("armed", "answer"))
        self.assertEqual(C._epoch(armed["resume_at"]), RETRY_EPOCH - 3600)
        self.assertEqual(len(self.spawned), 1)


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
        self.assertTrue(calls[0][1].endswith("/utilities/capability-route.py"))
        record = json.loads(self.path.read_text())
        self.assertEqual((record["state"], record["owner_attempt_id"]), ("resumed", "att-1"))
        notify.assert_called_once()
        self.assertEqual(C.run(self.path, sleep=self.sleep, now=lambda: self.clock[0],
                               call=lambda *a, **k: self.fail("resumed twice")), 0)

    def test_the_resume_runs_the_installed_release_not_the_arming_one(self):
        done = SimpleNamespace(returncode=0, stdout="{}\n")
        calls = []
        with mock.patch("parent_next_directive.command_home", return_value="/data/hearting/current"), \
             mock.patch.object(C, "_notify"):
            C.run(self.path, sleep=self.sleep, now=lambda: self.clock[0],
                  call=lambda argv, **kw: calls.append(argv) or done)
        self.assertEqual(calls[0][1], "/data/hearting/current/utilities/capability-route.py")

    def test_an_answer_record_runs_start_without_the_reset_slack(self):
        record = json.loads(self.path.read_text())
        self.path.write_text(json.dumps({**record, "cause": "answer", "retry_at": PAUSE["retry_at"]}))
        self.clock[0] = RETRY_EPOCH
        calls = []
        done = SimpleNamespace(returncode=0, stdout=json.dumps({"state": "running", "owner_attempt_id": "att-2"}) + "\n")
        with mock.patch.object(C, "_notify") as notify:
            C.run(self.path, sleep=self.sleep, now=lambda: self.clock[0],
                  call=lambda argv, **kw: calls.append(argv) or done)
        self.assertEqual(self.clock[0], RETRY_EPOCH)  # no wait
        self.assertEqual(calls[0][2:5], ["start", "--route", "/r/route.json"])
        self.assertEqual(json.loads(self.path.read_text())["owner_attempt_id"], "att-2")
        notify.assert_called_once()

    def claude_pause(self, **extra):
        record = json.loads(self.path.read_text())
        self.path.write_text(json.dumps({**record, "cause": "capacity", "harness": "claude",
                                         "source_attempt_id": "att-dead", "usage_state": "2026-10-07T12:00:00Z",
                                         **extra}))

    def test_a_claude_pause_resumes_once_the_recheck_clears_it_and_can_pause_again(self):
        self.claude_pause()
        self.clock[0] = RETRY_EPOCH - 7200
        checks, calls, nested = [], [], []
        def cleared(rec):
            checks.append(self.clock[0])
            return len(checks) == 2
        def call(argv, **kw):
            calls.append(argv)
            # The resumed start pauses again with the same reset: it must arm a fresh record.
            nested.append(C.arm({**PAUSE, "harness": "claude"}, "/r/route.json", Path(self._tmp.name) / "jobs.log",
                                environ={}, now=lambda: self.clock[0], spawn=lambda argv, **kw: None))
            return SimpleNamespace(returncode=0, stdout=json.dumps({"state": "waiting-capacity",
                                                                     "retry_at": PAUSE["retry_at"]}) + "\n")
        record_path = C.record_path(Path(self._tmp.name) / "jobs.log", PAUSE["route_id"], PAUSE["retry_at"])
        record_path.parent.mkdir(parents=True, exist_ok=True)
        self.path.replace(record_path)
        self.path = record_path
        with mock.patch.object(C, "_notify") as notify:
            C.run(self.path, sleep=self.sleep, now=lambda: self.clock[0], call=call, cleared=cleared)
        self.assertEqual(checks[1] - checks[0], C.RECHECK_SECONDS)
        self.assertLess(self.clock[0], RETRY_EPOCH)
        self.assertEqual(calls[0][2:5], ["start", "--route", "/r/route.json"])
        self.assertEqual(nested[0]["state"], "armed")
        [moved] = self.path.parent.glob(self.path.stem + ".early-*.json")
        early = json.loads(moved.read_text())
        self.assertEqual((early["state"], early["early"]), ("resumed", True))
        self.assertEqual(json.loads(self.path.read_text())["state"], "armed")  # the new pause's own record
        notify.assert_called_once()

    def test_a_pause_the_recheck_never_clears_waits_for_its_reset(self):
        self.claude_pause()
        self.clock[0] = RETRY_EPOCH - 3000
        checks = []
        done = SimpleNamespace(returncode=0, stdout="{}\n")
        with mock.patch.object(C, "_notify"):
            C.run(self.path, sleep=self.sleep, now=lambda: self.clock[0], call=lambda argv, **kw: done,
                  cleared=lambda rec: checks.append(self.clock[0]) and False)
        self.assertEqual(len(checks), 3000 // C.RECHECK_SECONDS)
        self.assertGreaterEqual(self.clock[0], RETRY_EPOCH + C.SLACK_SECONDS)
        self.assertNotIn("early", json.loads(self.path.read_text()))

    def test_only_a_claude_usage_limit_is_rechecked_and_only_ok_clears_it(self):
        import dispatch_capacity_evidence as Q
        record = {"cause": "capacity", "harness": "claude", "jobs": "/j/jobs.log", "source_attempt_id": "att-dead",
                  "usage_state": "2026-10-07T12:00:00Z"}
        with mock.patch.object(Q, "attempt_model", return_value="claude-opus-4-6") as model, \
             mock.patch.object(Q, "usage_states", return_value={"claude": "ok"}) as states:
            self.assertTrue(C._cleared(record))
        model.assert_called_once_with("/j/jobs.log", "att-dead")
        states.assert_called_once_with("/j/jobs.log", models={"claude": "claude-opus-4-6"}, recheck=True)
        for state in ("limited(2026-10-07T12:00:00Z)", "unknown"):
            with mock.patch.object(Q, "attempt_model", return_value="claude-opus-4-6"), \
                 mock.patch.object(Q, "usage_states", return_value={"claude": state}):
                self.assertFalse(C._cleared(record))
        with mock.patch.object(Q, "usage_states", side_effect=AssertionError("asked")):
            for other in ({**record, "cause": "answer"}, {**record, "harness": "codex"},
                          {**record, "usage_state": "allocation-usage-gate"}):
                self.assertFalse(C._cleared(other))

    def test_an_unresolved_source_model_waits_for_the_original_reset(self):
        import dispatch_capacity_evidence as Q
        self.claude_pause(source_attempt_id="")
        self.clock[0] = RETRY_EPOCH - 1800
        starts = []
        with mock.patch.object(Q, "attempt_model", return_value=None), \
             mock.patch.object(Q, "usage_states", return_value={"claude": "ok"}) as states, \
             mock.patch.object(C, "_notify"):
            C.run(self.path, sleep=self.sleep, now=lambda: self.clock[0], cleared=C._cleared,
                  call=lambda *a, **k: starts.append(self.clock[0]) or SimpleNamespace(returncode=0, stdout="{}"))
        states.assert_not_called()
        self.assertEqual(starts, [RETRY_EPOCH + C.SLACK_SECONDS])
        self.assertNotIn("early", json.loads(self.path.read_text()))

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

    def test_a_launch_resume_receipt_ends_the_turn_and_says_nothing_ran(self):
        import work_start
        pause = {"state": "needs-attention", "reason": "owner-launch-not-started",
                 "required_action": "resume-later", "route_id": PAUSE["route_id"]}
        armed = {"record": "/x.json", "resume_at": "2026-10-07T12:01:00Z", "state": "armed",
                 "cause": "launch-not-started"}
        with mock.patch.object(C, "arm", return_value=armed):
            result = work_start._arm_capacity_resume(dict(pause), "/r/route.json", "/j/jobs.log")
        self.assertEqual((result["parent_next"], result["required_action"], result["parent_next_reason"]),
                         ("end-turn", "wait-for-auto-resume", "launch-auto-resume"))
        self.assertIn("did not start and nothing ran", result["next_step"])

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
            self.assertIn("resumed after the usage limit reset", text)
            with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "sid-9"}, clear=True):
                C._notify({**record, "retry_at": "2026-10-07T12:01:00Z", "cause": "launch-not-started"},
                          {"state": "running", "owner_attempt_id": "att-2"})
            claimed, _ = sweep.sweep_deliver(jobs.parent, "claude-parent-runtime", "sid-9")
            self.assertIn("started again after a launch that did not start",
                          sweep.delivery_context([(jobs.parent, claimed)]))
            # A start that failed (here: refused, no receipt) hands the parent the command again.
            route = Path(tmp) / f"{PAUSE['route_id']}.json"
            with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "sid-9"}, clear=True):
                C._notify({**record, "retry_at": "2026-10-07T12:02:00Z", "cause": "answer", "exit_code": 2,
                           "route_file": str(route)}, {})
            claimed, _ = sweep.sweep_deliver(jobs.parent, "claude-parent-runtime", "sid-9")
            text = sweep.delivery_context([(jobs.parent, claimed)])
            self.assertIn("continued with the answer another session sent: state unknown", text)
            self.assertIn("start exited 2, run it again: ", text)
            self.assertIn(f"capability-route.py start --route {route}", text)
            self.assertIn("required_action=run-resume-command", text)


if __name__ == "__main__":
    unittest.main()
