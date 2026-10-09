#!/usr/bin/env python3
"""Temporary-root checks for durable peer obligations and native readiness."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import peer_obligations as obligations

_HERE = Path(__file__).resolve().parent
_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "peer_steward_under_obligation_test", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)


class ObligationStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = obligations.ObligationStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_is_idempotent_and_keeps_body_out_of_duty_record(self):
        intent = {"ref": "a" * 32, "body_digest": "b" * 64}
        identity = {"server": "fixture-server", "pane": "w1:p7", "session_id": "sid-1"}
        first = self.store.create("message-" + "a" * 32, "message", identity, intent)
        again = self.store.create("message-" + "a" * 32, "message", identity, intent)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["intent"], again["intent"])
        self.assertNotIn("text", first["intent"])

    def test_immutable_identity_conflict_is_refused(self):
        self.store.create("retire-1", "retire", {"pane": "w1:p1"}, {"target": "worker"})
        with self.assertRaisesRegex(obligations.ObligationError, "intent-conflict"):
            self.store.create("retire-1", "retire", {"pane": "w1:p2"}, {"target": "worker"})

    def test_result_is_immutable_across_later_transport_updates(self):
        self.store.create("batch-1", "registered-batch", {"parent": "att-parent"},
                          {"attempt_ids": ["att-child"]})
        self.store.update("batch-1", result="succeeded", delivery="pending")
        updated = self.store.update("batch-1", observation={"reason": "receiver-unavailable"},
                                    delivery="pending")
        self.assertEqual(updated["result"], "succeeded")
        with self.assertRaisesRegex(obligations.ObligationError, "result-conflict"):
            self.store.update("batch-1", result="failed")

    def test_ordinary_pane_without_registry_row_is_valid_native_evidence(self):
        jobs = self.root / "dispatch" / "jobs.log"
        work, state = obligations.bound_work_for_pane(
            "w1:p4", "codex", "thread-1", jobs=jobs)
        self.assertEqual((work, state), ((), "observed"))
        ready = obligations.pane_readiness(
            server="fixture-server", pane="w1:p4", harness="codex",
            session_id="thread-1", pid_birth="pid:9@start:4",
            identity_verified=True, native_turn="idle",
            bound_work=work, bindings_state=state,
        )
        self.assertEqual((ready.state, ready.scope, ready.outcome),
                         ("ready", "native-turn", None))

    def test_observer_error_after_fulfillment_cannot_reopen_duty(self):
        self.store.create("retire-done", "retire", {"pane": "w1:p1"},
                          {"target": "worker"})
        done = self.store.update("retire-done", state="complete", result="normal-exit",
                                 observation={"phase": "complete"}, cleanup="complete")
        after_error = self.store.update("retire-done", observer_error="observer-unavailable")
        self.assertEqual(after_error, done)

    def test_unreadable_binding_source_is_unknown(self):
        jobs = self.root / "not-a-file"
        jobs.mkdir()
        work, state = obligations.bound_work_for_pane(
            "w1:p4", "codex", "thread-1", jobs=jobs)
        self.assertEqual((work, state), ((), "unknown"))
        ready = obligations.pane_readiness(
            server="fixture-server", pane="w1:p4", harness="codex",
            session_id="thread-1", pid_birth="pid:9@start:4",
            identity_verified=True, native_turn="idle",
            bound_work=work, bindings_state=state,
        )
        self.assertEqual((ready.state, ready.reason),
                         ("unknown", "registered-bindings-unavailable"))


_FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
mode = os.environ.get("FAKE_HERDR_MODE", "idle")
target = argv[2] if len(argv) > 2 else "-"
info = {"result": {"agent": {"agent": "claude", "agent_session": {"value": "sid-fake"},
        "agent_status": "idle", "name": target, "pane_id": "w1:p9",
        "window_id": os.environ.get("FAKE_HERDR_WINDOW_ID", "window-fixture")},
        "type": "agent_info"}}
verb = argv[1] if len(argv) > 1 else ""
if verb == "wait" and mode == "held":
    with open(os.environ["FAKE_HERDR_FIFO"], "r") as fh:
        fh.read()
print(json.dumps(info))
sys.exit(0)
'''

_SCRUB_PREFIXES = ("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_",
                   "AGENT_THREAD_", "HERDR_")
_SCRUB_KEYS = ("AGENT_SESSION_ID", "AGENT_SESSION_ROLE", "CLAUDE_CODE_SESSION_ID",
               "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
               "OPENCODE_SESSION_ID", "OPENCODE_DISPATCH_SLUG",
               "AGENT_HERDR_SESSION")


class StewardRecoveryIntegrationTest(unittest.TestCase):
    """Actual steward/controller recovery: fixture transport, death, restart, display."""


def _fixture_env(root: Path, jobs: Path, state: Path, bindir: Path,
                 fifo: Path, session: str, mode: str) -> dict:
    """Fixture transport with inherited runtime/session/dispatch identities removed."""
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith(_SCRUB_PREFIXES):
            env.pop(key, None)
    for key in _SCRUB_KEYS:
        env.pop(key, None)
    env["AGENT_DISPATCH_JOBS"] = str(jobs)
    env["AGENT_PEER_LEDGER_ROOT"] = str(state)
    env["HOME"] = str(root / "home")
    env["CLAUDE_CODE_SESSION_ID"] = session
    env["FAKE_HERDR_MODE"] = mode
    env["FAKE_HERDR_FIFO"] = str(fifo)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return env


class StewardRecoveryIntegrationTest(unittest.TestCase):
    """Actual steward/controller recovery: fixture transport, death, restart, display."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.touch()
        self.state = self.root / "peer-state"
        self.bindir = self.root / "fakebin"
        self.bindir.mkdir()
        fake = self.bindir / "herdr"
        fake.write_text(_FAKE_HERDR, encoding="utf-8")
        fake.chmod(0o755)
        self.fifo = self.root / "release.fifo"
        os.mkfifo(self.fifo)
        self.session = "steward-obligation-1"
        self.saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)
        self._children: list[int] = []
        self.addCleanup(self._reap_children)
        self._old_herdr_session = peer_steward._HERDR_SESSION
        peer_steward._HERDR_SESSION = None
        self.addCleanup(self._restore_herdr_session)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self.saved_environ)

    def _restore_herdr_session(self):
        peer_steward._HERDR_SESSION = self._old_herdr_session

    def _reap_children(self):
        for pid in self._children:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.monotonic() + 10
        remaining = set(self._children)
        while remaining and time.monotonic() < deadline:
            for pid in tuple(remaining):
                try:
                    waited, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    remaining.discard(pid)
                    continue
                except OSError:
                    continue
                if waited:
                    remaining.discard(pid)
            time.sleep(0.02)

    def _apply_fixture_env(self, mode="held"):
        os.environ.clear()
        os.environ.update(_fixture_env(
            self.root, self.jobs, self.state, self.bindir,
            self.fifo, self.session, mode))

    def _arm_watch(self, mode="held"):
        env = _fixture_env(self.root, self.jobs, self.state, self.bindir,
                           self.fifo, self.session, mode)
        proc = subprocess.run(
            [sys.executable, str(_HERE / "peer-steward.py"), "watch", "peer-a"],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields = {}
        for token in proc.stdout.split():
            key, separator, value = token.partition("=")
            if separator:
                fields[key] = value
        return fields["watch_id"], int(fields["pid"]), env

    @staticmethod
    def _wait_for_death(pid: int, seconds: float = 10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.02)
        raise AssertionError(f"fixture observer {pid} never died")

    def test_watch_duty_survives_observer_death_and_resumes_same_id(self):
        watch_id, pid, _env = self._arm_watch(mode="held")
        arm_path = self.state / "peer-watches" / f"{watch_id}.json"
        first = json.loads(arm_path.read_text(encoding="utf-8"))
        self.assertEqual(first["watch_id"], watch_id)
        os.kill(pid, signal.SIGKILL)
        self._wait_for_death(pid)

        self._apply_fixture_env(mode="held")
        peer_steward._ensure_watch_observers()

        resumed = json.loads(arm_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed["watch_id"], watch_id,
                         "restart replaces only the observer, never the duty")
        self.assertGreaterEqual(resumed.get("observer_generation", 1), 2)
        new_pid = int((resumed.get("watcher") or {})["pid"])
        self.assertNotEqual(new_pid, pid)
        self._children.append(new_pid)
        try:
            os.kill(new_pid, 0)
        except OSError:
            self.fail("resumed observer is not alive")
        # Death never fulfills the duty: no terminal receipt was written.
        self.assertFalse((self.state / "peer-watches" / f"{watch_id}.receipt.json").exists())

    def test_pending_duties_reach_the_read_only_collector_body_free(self):
        watch_id, pid, _env = self._arm_watch(mode="held")
        self._children.append(pid)
        store = obligations.ObligationStore(self.state)
        store.create("message-" + "r" * 32, "message",
                     {"server": "default", "pane": "w1:p9",
                      "harness": "claude", "session_id": "sid-fake"},
                     {"ref": "r" * 32, "body_digest": "d" * 64,
                      "target": "peer-a",
                      "to": {"harness": "claude", "session_id": "sid-fake"}})
        tools = str(_HERE.parent / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.collectors import peer_messages
        result = peer_messages.collect(state_roots=[str(self.state)])
        pending = result["by_session"][("claude", "sid-fake")]["pending_obligations"]
        kinds = {item["kind"] for item in pending}
        self.assertIn("watch", kinds)
        self.assertIn("message", kinds)
        for item in pending:
            self.assertLessEqual(set(item), {"kind", "state", "age_min", "ref"})
        os.kill(pid, signal.SIGKILL)
        self._wait_for_death(pid)
        self._children.remove(pid)

    def test_bound_registered_row_overrides_idle_pane_through_real_jobs(self):
        meta = ",".join([
            "attempt_id=att-fixture-1", "parent_sid=sid-1", "parent_pane=w1:p4",
            "parent_harness=codex",
        ])
        with open(self.jobs, "a", encoding="utf-8") as handle:
            handle.write(f"2026-10-09T00:00:00Z\topen\trepo\t-\tslug\t{meta}\n")
        work, state = obligations.bound_work_for_pane(
            "w1:p4", "codex", "sid-1", jobs=self.jobs)
        self.assertEqual(state, "observed")
        self.assertEqual(len(work), 1)
        ready = obligations.pane_readiness(
            server="fixture-server", pane="w1:p4", harness="codex",
            session_id="sid-1", pid_birth="pid:9@start:4",
            identity_verified=True, native_turn="idle",
            bound_work=work, bindings_state=state)
        self.assertNotEqual(ready.state, "ready")
        self.assertIsNone(ready.outcome, "idle never invents a work result")


if __name__ == "__main__":
    unittest.main()
