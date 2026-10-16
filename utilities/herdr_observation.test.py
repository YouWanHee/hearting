import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import herdr_observation as observation


class _MemorySocket:
    def __init__(self, server):
        self.server = server
        self.buffer = bytearray()
        self.closed = False

    def connect(self, _path):
        return None

    def settimeout(self, _timeout):
        return None

    def sendall(self, data):
        self.server.request(self, data)

    def recv(self, size):
        if not self.buffer:
            return b""
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def feed(self, data):
        self.buffer.extend(data)

    def readable(self):
        return bool(self.buffer) or self.closed

    def fileno(self):
        return id(self) & 0x7fffffff

    def close(self):
        self.closed = True


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

    def test_events_lost_error_invalidates_without_treating_it_as_an_event_kind(self):
        self.assertFalse(self.state.apply_event("named", {
            "id": "", "error": {"code": "events_lost", "message": "resnapshot"}}))
        self.assertFalse(self.state.complete)

    def test_agent_status_event_updates_only_its_pane_and_revision(self):
        before = self.state.revision
        self.assertTrue(self.state.apply_event("named", {
            "event": "pane.agent_status_changed",
            "data": {"type": "pane_agent_status_changed", "pane_id": "w1:p9",
                     "agent_status": "idle"}}))
        self.assertEqual(self.state.panes["w1:p9"]["agent_status"], "idle")
        self.assertEqual(self.state.revision, before + 1)
        self.assertTrue(self.state.complete)


class _HerdrApiFixture:
    def __init__(self, path, *, panes=None, protocol=22, interleave=None):
        self.path = str(path)
        self.panes = list(panes or [{"pane_id": "w1:p9", "agent": "codex"}])
        self.protocol = protocol
        self.interleave = interleave
        self.methods = []
        self.errors = []
        self.subscribers = []
        self.lock = threading.Lock()
        self.stopping = False

    def connect(self, _family, _kind):
        return _MemorySocket(self)

    def request(self, client, data):
        try:
            request = json.loads(data.decode("utf-8").splitlines()[0])
            method = request.get("method")
            with self.lock:
                self.methods.append(method)
            if method == "events.subscribe":
                with self.lock:
                    self.subscribers.append(client)
                response = {"id": request["id"], "result": {"type": "ok"}}
                client.feed((json.dumps(response) + "\n").encode())
                return
            if method == "session.snapshot":
                if self.interleave is not None:
                    with self.lock:
                        subscribers = list(self.subscribers)
                    for subscriber in subscribers:
                        subscriber.feed((json.dumps(self.interleave) + "\n").encode())
                snapshot = {"protocol": self.protocol, "panes": self.panes,
                            "agents": [{"agent": "codex", "pane_id": "w1:p9"}]}
                response = {"id": request["id"], "result": {
                    "type": "session_snapshot", "snapshot": snapshot}}
                client.feed((json.dumps(response) + "\n").encode())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def publish(self, envelope):
        data = envelope if isinstance(envelope, bytes) else (json.dumps(envelope) + "\n").encode()
        with self.lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            subscriber.feed(data)

    def disconnect_subscribers(self):
        with self.lock:
            subscribers = list(self.subscribers)
            self.subscribers.clear()
        for subscriber in subscribers:
            subscriber.close()

    def close(self):
        self.stopping = True
        with self.lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            subscriber.close()


class ObservationTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.socket_path = os.path.join(self.tmp.name, "herdr.sock")

    def start(self, **kwargs):
        server = _HerdrApiFixture(self.socket_path, **kwargs)
        self.addCleanup(server.close)
        readiness = mock.patch.object(
            observation.select, "select",
            side_effect=lambda reads, _writes, _errors, _timeout: (
                [sock for sock in reads
                 if isinstance(sock, _MemorySocket) and sock.readable()], [], []))
        readiness.start()
        self.addCleanup(readiness.stop)
        transport = observation.ObservationTransport(
            "isolated", self.socket_path, timeout=0.2, connector=server.connect)
        self.addCleanup(transport.close)
        return server, transport

    def test_subscribes_before_snapshot_and_applies_interleaved_event_in_wire_order(self):
        event = {"event": "pane.closed", "data": {"type": "pane_closed",
                                                       "pane_id": "w1:p9"}}
        server, transport = self.start(interleave=event)
        view = transport.observe()
        self.assertIsNotNone(view, server.errors)
        self.assertEqual(view.endpoint, "isolated")
        self.assertEqual(view.panes, ())
        self.assertEqual(server.methods[:2], ["events.subscribe", "session.snapshot"])
        self.assertEqual(transport._state.terminal["w1:p9"], "pane-closed")

    def test_events_lost_error_invalidates_then_one_snapshot_resynchronizes(self):
        server, transport = self.start()
        first = transport.observe()
        self.assertIsNotNone(first, server.errors)
        self.assertEqual([pane["pane_id"] for pane in first.panes], ["w1:p9"])
        server.publish({"id": "", "error": {"code": "events_lost",
                                                "message": "resubscribe and snapshot"}})
        second = transport.observe()
        self.assertIsNotNone(second)
        self.assertTrue(second.complete)
        self.assertEqual(second.panes[0]["pane_id"], "w1:p9")
        self.assertEqual(server.methods.count("events.subscribe"), 2)
        self.assertEqual(server.methods.count("session.snapshot"), 2)

    def test_malformed_event_invalidates_and_reconnects_without_confirming_absence(self):
        server, transport = self.start()
        self.assertIsNotNone(transport.observe(), server.errors)
        server.publish(b"not-json\n")
        view = transport.observe()
        self.assertIsNotNone(view)
        self.assertTrue(view.complete)
        self.assertEqual(view.panes[0]["pane_id"], "w1:p9")
        self.assertEqual(server.methods.count("events.subscribe"), 2)

    def test_disconnect_invalidates_and_reconnects_to_a_complete_snapshot(self):
        server, transport = self.start()
        self.assertIsNotNone(transport.observe(), server.errors)
        server.disconnect_subscribers()
        view = transport.observe()
        self.assertIsNotNone(view)
        self.assertTrue(view.complete)
        self.assertEqual(view.panes[0]["pane_id"], "w1:p9")
        self.assertEqual(server.methods.count("events.subscribe"), 2)
        self.assertEqual(server.methods.count("session.snapshot"), 2)

    def test_protocol_mismatch_is_unknown_and_never_publishes_partial_snapshot(self):
        _server, transport = self.start(protocol=21)
        self.assertIsNone(transport.observe())
        self.assertFalse(transport._state.complete)

    def test_missing_socket_is_a_bounded_unknown_observation(self):
        transport = observation.ObservationTransport("isolated", None, timeout=0.05)
        started = time.monotonic()
        self.assertIsNone(transport.observe())
        self.assertLess(time.monotonic() - started, 0.2)

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
