import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import herdr_observation as observation


class PaneCensusTest(unittest.TestCase):
    def test_successful_empty_census_confirms_absence_only_on_its_endpoint(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"result": {"panes": []}}), stderr=""))
        census = observation.pane_census(session="isolated", runner=runner,
                                         which=lambda _name: "/usr/bin/herdr")
        self.assertTrue(observation.confirms_absence(census, "w1:p9", endpoint="isolated"))
        self.assertFalse(observation.confirms_absence(census, "w1:p9", endpoint="default"))
        self.assertEqual(runner.call_args.args[0][:3], ["herdr", "--session", "isolated"])

    def test_error_malformed_or_duplicate_census_is_unknown(self):
        for code, body in ((1, "{}"), (0, "not-json"),
                           (0, json.dumps({"result": {"panes": [{"pane_id": "p"}, {"pane_id": "p"}]}}))):
            runner = lambda _argv, _code=code, _body=body, **_kw: subprocess.CompletedProcess(
                [], _code, stdout=_body, stderr="")
            self.assertIsNone(observation.pane_census(runner=runner, which=lambda _n: "herdr"))
        self.assertIsNone(observation.pane_census(which=lambda _n: None))


class EventReducerTest(unittest.TestCase):
    def setUp(self):
        self.state = observation.ObservationSnapshot("named")
        self.assertTrue(self.state.replace_snapshot("named", [{"pane_id": "w1:p9"}]))

    def test_closed_and_exited_events_are_idempotent_terminal_facts(self):
        self.assertTrue(self.state.apply_event("named", {
            "event": "pane_closed", "data": {"pane_id": "w1:p9"}}))
        self.assertEqual(self.state.terminal["w1:p9"], "pane-closed")
        self.assertTrue(self.state.confirms_absence("w1:p9"))
        self.assertTrue(self.state.apply_event("named", {
            "event": "pane.exited", "data": {"pane_id": "w1:p9"}}))
        self.assertEqual(self.state.terminal["w1:p9"], "pane-exited")

    def test_reconnect_invalidates_census_and_cross_endpoint_events(self):
        self.state.reconnect()
        self.assertFalse(self.state.confirms_absence("w1:p9"))
        self.assertFalse(self.state.apply_event("named", {
            "event": "pane_closed", "data": {"pane_id": "w1:p9"}}))
        self.assertFalse(self.state.replace_snapshot("other", []))
        self.assertFalse(self.state.complete)

    def test_malformed_update_invalidates_snapshot(self):
        self.assertFalse(self.state.apply_event("named", {
            "event": "pane_updated", "data": {"pane_id": "w1:p9"}}))
        self.assertFalse(self.state.complete)


class NativeStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pid = os.getpid()
        self.start = observation._process_start_ticks(self.pid)
        self.row = {"endpoint": "named", "pane": "w1:p9", "harness": "opencode",
                    "session_id": "sid", "pid": self.pid, "pid_start": self.start,
                    "status": "idle", "observed_at": 100.0}

    def test_native_status_is_exact_bounded_and_owner_only(self):
        with mock.patch.object(observation.time, "time", return_value=100.0):
            self.assertTrue(observation.record_native_status(self.row))
        row = observation.read_native_status("named", "w1:p9", "opencode", "sid",
                                             self.pid, self.start, now=105.0)
        self.assertEqual(row["status"], "idle")
        self.assertEqual(Path(observation._native_status_path(
            "named", "w1:p9", "opencode", "sid")).stat().st_mode & 0o777, 0o600)
        self.assertIsNone(observation.read_native_status(
            "default", "w1:p9", "opencode", "sid", self.pid, self.start, now=105.0))
        self.assertIsNone(observation.read_native_status(
            "named", "w1:p9", "opencode", "sid", self.pid, self.start,
            max_age=1.0, now=105.0))

    def test_stale_process_birth_never_writes_or_reads_native_state(self):
        stale = dict(self.row, pid_start="1")
        self.assertFalse(observation.record_native_status(stale))
        with mock.patch.object(observation, "_process_start_ticks", return_value="2"):
            self.assertIsNone(observation.read_native_status(
                "named", "w1:p9", "opencode", "sid", self.pid, self.start, now=100.0))

if __name__ == "__main__":
    unittest.main()
