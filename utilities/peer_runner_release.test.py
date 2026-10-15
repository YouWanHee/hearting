#!/usr/bin/env python3
"""Isolated A→B service exec keeps one PID, lock and acknowledged duty."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import peer_obligations as obligations  # noqa: E402

_MESSAGE_SPEC = importlib.util.spec_from_file_location(
    "runner_release_peer_message", ROOT / "utilities/peer-message.py")
peer_message = importlib.util.module_from_spec(_MESSAGE_SPEC)
_MESSAGE_SPEC.loader.exec_module(peer_message)


class ReleaseTransitionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.data = self.base / "data"
        self.releases = self.data / "releases"
        self.state = self.base / "state"
        self.ready = self.base / "duty-in-flight"
        self.release_boundary = self.base / "release-duty-boundary"
        self.b_ready = self.base / "b-duty-in-flight"
        self.b_release_boundary = self.base / "release-b-duty-boundary"
        self.markers = self.base / "runner-starts.jsonl"
        self.ack_marker = self.base / "b-consumed-ack.json"
        self.b_observation = self.base / "b-before-duty.json"
        for path in (self.home, self.releases, self.state):
            path.mkdir(parents=True, exist_ok=True)
        self.a = self._release("A")
        self.b = self._release("B")
        self.current = self.data / "current"
        self._point_current(self.a)
        self.body = "[handoff] release fixture message"
        self.sender = {"harness": "codex", "session_id": "sid-release-sender", "name": "sender"}
        self.recipient = {"harness": "codex", "session_id": "sid-release-recipient", "name": "recipient"}
        self.env = self._environment(self.a)
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.ref, self.body_digest, self.duty_id = self._seed_acknowledged_duty()

    def _release(self, version):
        root = self.releases / version
        shutil.copytree(ROOT / "utilities", root / "utilities",
                        ignore=shutil.ignore_patterns("*.test.py", "*.test.sh", "__pycache__", "*.pyc"))
        for relative in ("tools/install", "core"):
            shutil.copytree(ROOT / relative, root / relative,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
        shutil.copy2(ROOT / "harness-manifest.json", root / "harness-manifest.json")
        runner = root / "utilities/peer-steward.py"
        source = runner.read_text(encoding="utf-8")
        prelude = '''import json as _runner_fixture_json
import os as _runner_fixture_os
import sys as _runner_fixture_sys
from pathlib import Path as _RunnerFixturePath

def _runner_fixture_start_record():
    marker = _runner_fixture_os.environ.get("RUNNER_FIXTURE_MARKERS")
    if not marker:
        return
    argv = _runner_fixture_sys.argv
    fd = int(argv[argv.index("--lock-fd") + 1])
    state_root = argv[argv.index("--state-root") + 1]
    info = _runner_fixture_os.fstat(fd)
    record = {"release": _RunnerFixturePath(__file__).resolve().parents[1].name,
              "pid": _runner_fixture_os.getpid(), "fd": fd,
              "device": info.st_dev, "inode": info.st_ino,
              "state_root": state_root}
    with open(marker, "a", encoding="utf-8") as stream:
        stream.write(_runner_fixture_json.dumps(record, sort_keys=True) + "\\n")
        stream.flush()
        _runner_fixture_os.fsync(stream.fileno())
    if (record["release"] == "B"
            and _runner_fixture_os.environ.get("RUNNER_FAIL_B_STARTUP") == "1"):
        _runner_fixture_os._exit(41)

_runner_fixture_start_record()
'''
        fixture_wrapper = '''
_production_resume_message_obligation = _resume_message_obligation

def _fixture_resume_message_obligation(duty, store):
    release = Path(__file__).resolve().parents[1].name
    if release == "A" and os.environ.get("RUNNER_FIXTURE_READY"):
        ready = Path(os.environ["RUNNER_FIXTURE_READY"])
        if not ready.exists():
            ready.write_text(duty["id"], encoding="utf-8")
            boundary = Path(os.environ["RUNNER_FIXTURE_RELEASE"])
            deadline = time.monotonic() + 15
            while not boundary.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not boundary.exists():
                raise RuntimeError("fixture-boundary-timeout")
            return
    if release == "B" and os.environ.get("RUNNER_FIXTURE_B_READY"):
        ready = Path(os.environ["RUNNER_FIXTURE_B_READY"])
        if not ready.exists():
            ready.write_text(duty["id"], encoding="utf-8")
            boundary = Path(os.environ["RUNNER_FIXTURE_B_RELEASE"])
            deadline = time.monotonic() + 15
            while not boundary.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not boundary.exists():
                raise RuntimeError("fixture-b-boundary-timeout")
            return
    if release == "B":
        ref = (duty.get("intent") or {}).get("ref")
        observed = peer_message._read_pending(ref)
        Path(os.environ["RUNNER_FIXTURE_OBSERVATION"]).write_text(
            json.dumps({"state": (observed or {}).get("state"),
                        "ref": ref,
                        "ledger_root": str(peer_message.peer_state_root()),
                        "env_ledger_root": os.environ.get("AGENT_PEER_LEDGER_ROOT")},
                       sort_keys=True), encoding="utf-8")
    result = _production_resume_message_obligation(duty, store)
    if release in {"A", "B"} and store.get(duty["id"]).get("state") == "complete":
        received = peer_message._read_pending((duty.get("intent") or {}).get("ref"))
        record = {"release": release, "duty_id": duty["id"],
                  "ref": (duty.get("intent") or {}).get("ref"),
                  "body_sha256": (received or {}).get("body_sha256"),
                  "transfer_state": (received or {}).get("state")}
        Path(os.environ["RUNNER_FIXTURE_ACK_MARKER"]).write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8")
    return result

_resume_message_obligation = _fixture_resume_message_obligation
'''
        source = prelude + source.replace(
            '\nif __name__ == "__main__":\n', fixture_wrapper + '\nif __name__ == "__main__":\n')
        runner.write_text(source, encoding="utf-8")
        return root

    def _environment(self, code_root):
        return {
            "HOME": str(self.home),
            "HARNESS_DATA_ROOT": str(self.data),
            "XDG_DATA_HOME": str(self.base / "xdg-data"),
            "XDG_STATE_HOME": str(self.base / "xdg-state"),
            "AGENT_HOME": str(code_root),
            "AGENT_PEER_LEDGER_ROOT": str(self.state),
            "AGENT_DISPATCH_JOBS": str(self.base / "dispatch" / "jobs.log"),
            "PYTHONPATH": str(code_root / "utilities"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": os.environ.get("PATH", ""),
            "RUNNER_FIXTURE_READY": str(self.ready),
            "RUNNER_FIXTURE_RELEASE": str(self.release_boundary),
            "RUNNER_FIXTURE_MARKERS": str(self.markers),
            "RUNNER_FIXTURE_ACK_MARKER": str(self.ack_marker),
            "RUNNER_FIXTURE_OBSERVATION": str(self.b_observation),
        }

    def _point_current(self, target):
        replacement = self.data / "current.next"
        replacement.unlink(missing_ok=True)
        replacement.symlink_to(target, target_is_directory=True)
        os.replace(replacement, self.current)

    def _seed_acknowledged_duty(self):
        text, ref = peer_message.prepare_peer_message(
            self.body, self.sender, self.recipient, defer=True,
            refs=["release-fixture-duty"], receipt="fixture-awaiting-recipient")
        body_digest = peer_message._read_pending(ref)["body_sha256"]
        self.assertEqual(peer_message.receive_peer_message(text, self.recipient), 0)
        received = peer_message._read_pending(ref)
        self.assertEqual(received["state"], "received")
        store = obligations.ObligationStore(self.state)
        duty_id = "message-" + ref
        store.create(duty_id, "message",
                     {"server": "default", "pane": "fixture-pane",
                      "session_id": self.recipient["session_id"]},
                     {"ref": ref, "target": "fixture-target",
                      "identity": {"server": "default", "pane": "fixture-pane"}})
        return ref, body_digest, duty_id

    def _wait_for(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for fixture state: {path.name}")

    def _pending(self, root=None):
        environ = self._environment(root or self.b)
        with mock.patch.dict(os.environ, environ, clear=True):
            return peer_message._read_pending(self.ref)

    def _start_managed_runner(self, root, environ):
        obligations._RUNNERS.clear()
        with mock.patch.object(obligations, "__file__",
                               str(root / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, environ, clear=True):
            binding = obligations.managed_service_binding(root)
            self.assertEqual(binding, str(self.current))
            self.assertTrue(obligations.ensure_runner(self.state, service_current=binding))
        self.assertTrue(obligations._RUNNERS)
        return obligations._RUNNERS[-1]

    def _assert_same_lock_handoff(self, records):
        self.assertEqual([row["release"] for row in records], ["A", "B"])
        for key in ("pid", "fd", "device", "inode", "state_root"):
            self.assertEqual(records[0][key], records[1][key], key)
        self.assertEqual(records[0]["state_root"], str(self.state.resolve()))

    def test_current_switch_execs_b_at_safe_boundary_and_consumes_exact_ack(self):
        proc = self._start_managed_runner(self.a, self.env)
        try:
            self._wait_for(self.ready)
            self.assertEqual(self.ready.read_text(encoding="utf-8"), self.duty_id)
            self.assertEqual([json.loads(line)["release"] for line in self.markers.read_text().splitlines()], ["A"])

            self._point_current(self.b)
            lock_path = self.state / "peer-steward" / "obligations" / obligations.RUNNER_LOCK_NAME
            probe = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    import fcntl
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)

            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self.env, clear=True), \
                 mock.patch.object(obligations.subprocess, "Popen") as concurrent:
                self.assertTrue(obligations.ensure_runner(self.state,
                                                          service_current=str(self.current)))
                concurrent.assert_not_called()

            self.assertFalse(self.ack_marker.exists(), "B cannot consume before A leaves its duty")
            self.release_boundary.touch()
            self.assertEqual(proc.wait(timeout=15), 0)
            records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self._assert_same_lock_handoff(records)
            ack = json.loads(self.ack_marker.read_text(encoding="utf-8"))
            self.assertEqual(ack, {"release": "B", "duty_id": self.duty_id, "ref": self.ref,
                                   "body_sha256": self.body_digest,
                                   "transfer_state": "received"})
            duty = obligations.ObligationStore(self.state).get(self.duty_id)
            self.assertEqual((duty["state"], duty["result"], duty["delivery"]),
                             ("complete", "received", "acknowledged"))
            self.assertEqual(self._pending()["state"], "received")
            observed = json.loads(self.b_observation.read_text(encoding="utf-8"))
            self.assertEqual(observed["state"], "received", observed)
        finally:
            self.release_boundary.touch()
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            obligations._RUNNERS.clear()

    def test_post_exec_startup_failure_keeps_acknowledged_duty_for_next_lifecycle(self):
        failing_env = dict(self.env, RUNNER_FAIL_B_STARTUP="1")
        proc = self._start_managed_runner(self.a, failing_env)
        try:
            self._wait_for(self.ready)
            self._point_current(self.b)
            self.release_boundary.touch()
            self.assertEqual(proc.wait(timeout=15), 41)
            duty = obligations.ObligationStore(self.state).get(self.duty_id)
            self.assertEqual(duty["state"], "pending")
            self.assertEqual(self._pending()["state"], "received")
            first_records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self.assertEqual([row["release"] for row in first_records], ["A", "B"])
            self.assertEqual(first_records[0]["pid"], first_records[1]["pid"])

            recovery_env = self._environment(self.b)
            recovered = self._start_managed_runner(self.b, recovery_env)
            self.assertEqual(recovered.wait(timeout=15), 0)
            duty = obligations.ObligationStore(self.state).get(self.duty_id)
            self.assertEqual((duty["state"], duty["result"], duty["delivery"]),
                             ("complete", "received", "acknowledged"))
            ack = json.loads(self.ack_marker.read_text(encoding="utf-8"))
            self.assertEqual((ack["ref"], ack["body_sha256"], ack["transfer_state"]),
                             (self.ref, self.body_digest, "received"))
        finally:
            self.release_boundary.touch()
            for process in obligations._RUNNERS:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            obligations._RUNNERS.clear()

    def test_current_rollback_reexecs_again_without_dropping_the_acknowledged_duty(self):
        rollback_env = dict(self.env,
                            RUNNER_FIXTURE_B_READY=str(self.b_ready),
                            RUNNER_FIXTURE_B_RELEASE=str(self.b_release_boundary))
        proc = self._start_managed_runner(self.a, rollback_env)
        try:
            self._wait_for(self.ready)
            self._point_current(self.b)
            self.release_boundary.touch()
            self._wait_for(self.b_ready)
            self.assertFalse(self.ack_marker.exists(), "B remains inside its duty until the rollback boundary")

            self._point_current(self.a)
            self.b_release_boundary.touch()
            self.assertEqual(proc.wait(timeout=15), 0)
            records = [json.loads(line) for line in self.markers.read_text().splitlines()]
            self.assertEqual([row["release"] for row in records], ["A", "B", "A"])
            for key in ("pid", "fd", "device", "inode", "state_root"):
                self.assertEqual(len({row[key] for row in records}), 1, key)
            ack = json.loads(self.ack_marker.read_text(encoding="utf-8"))
            self.assertEqual(ack, {"release": "A", "duty_id": self.duty_id,
                                   "ref": self.ref, "body_sha256": self.body_digest,
                                   "transfer_state": "received"})
            duty = obligations.ObligationStore(self.state).get(self.duty_id)
            self.assertEqual((duty["state"], duty["result"], duty["delivery"]),
                             ("complete", "received", "acknowledged"))
        finally:
            self.release_boundary.touch()
            self.b_release_boundary.touch()
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
            obligations._RUNNERS.clear()


if __name__ == "__main__":
    unittest.main()
