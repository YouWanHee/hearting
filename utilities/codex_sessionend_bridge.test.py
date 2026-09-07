#!/usr/bin/env python3
"""Isolated native-deadline tests; the model executable is synthetic."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
BRIDGE = ROOT / "adapters/codex/hooks/sessionend-lifecycle.py"
MEM = ROOT / "tools/memory/mem.py"
sys.path.insert(0, str(ROOT / "utilities"))
import memory_session_completion as completion


def load_bridge():
    spec = importlib.util.spec_from_file_location("sessionend_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SessionEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="memory-sessionend-e2e-")
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.cleanup_children)
        self.sid = "synthetic-end-" + self.base.name
        # tempfile suffixes can contain underscores, which split recall tokens
        # and make an absent new nonce match the prior record's shared prefix.
        self.nonce = "HRTEND" + hashlib.sha256(self.base.name.encode()).hexdigest()[:24]
        self.project = self.base / "project"
        self.project.mkdir()
        # An allowlist prevents inherited route, model, session, and remote-sync
        # configuration from touching the user's live state.
        self.env = {"PATH": str(self.base / "bin") + ":/usr/local/bin:/usr/bin:/bin",
                    "HOME": str(self.base / "home"), "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1", "AGENT_HOME": str(ROOT),
                    "CODEX_DISTILL_ENABLE": "1", "CODEX_DISTILL_APPLY": "1",
                    "CODEX_DISTILL_CONTRACT_ACCEPTED": "1",
                    "MEM_SYNC_REMOTE": "0", "MEM_DUMP_PUSH": "0",
                    "FLEET_TITLE_DISABLE": "1"}
        for key, leaf in {
            "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data", "XDG_STATE_HOME": "state",
            "CODEX_HOME": "codex-home", "CLAUDE_CONFIG_DIR": "claude-home",
            "MEM_STORE": "store", "MEM_PROJECTS": "transcripts-claude",
            "CODEX_SESSIONS": "transcripts-codex", "MEM_SESSION_COMPLETION_RECEIPTS": "receipts",
            "AGENT_MODEL_GOVERNOR_ROOT": "governor", "AGENT_ARTIFACT_ROOT": "artifacts",
            "MEM_RECALL_RECEIPTS": "recall-receipts",
        }.items():
            self.env[key] = str(self.base / leaf)
            Path(self.env[key]).mkdir(mode=0o700)
        Path(self.env["HOME"]).mkdir()
        (self.base / "bin").mkdir()
        self.env["MEM_WRITE_EVENTS"] = str(self.base / "write-events.jsonl")
        self.env["MEM_RECALL_EVENTS"] = str(self.base / "recall-events.jsonl")
        self.env["SYNTHETIC_INVOCATIONS"] = str(self.base / "invocations.jsonl")
        self.env["SYNTHETIC_NONCE"] = self.nonce
        self.env["SYNTHETIC_DELAY"] = "4.5"
        self.env["SYNTHETIC_EXIT"] = "0"
        fake = self.base / "bin/codex"
        fake.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
nonce = os.environ["SYNTHETIC_NONCE"]
proc_fields = pathlib.Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
identity = {"pid": os.getpid(), "start_ticks": int(proc_fields[19]),
    "boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
with open(os.environ["SYNTHETIC_INVOCATIONS"], "a") as out:
    out.write(json.dumps({"args": args, "pid":os.getpid(), "identity":identity, "nonce":nonce,
        "role": os.environ.get("AGENT_SESSION_ROLE"),
        "distill": os.environ.get("MEM_DISTILL"), "completion": os.environ.get("MEM_SESSION_COMPLETION"),
        "has_curate_contract": '"action":"add"' in prompt}) + "\\n")
time.sleep(float(os.environ["SYNTHETIC_DELAY"]))
if int(os.environ["SYNTHETIC_EXIT"]):
    sys.stderr.write("synthetic private error must not enter receipt\\n")
    sys.exit(int(os.environ["SYNTHETIC_EXIT"]))
action = {"action":"add", "tier":"durable", "type":"user-correction",
    "body":nonce + " approved deployment region is ap-northeast-2.",
    "headline":nonce + " deployment correction", "aliases":[nonce, "deployment correction"],
    "entities":[nonce], "topics":["memory-test"], "artifact_refs":[]}
pathlib.Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(action) + "\\n")
''')
        fake.chmod(0o700)
        transcript = {"type": "event_msg", "timestamp": "2026-09-07T11:00:00Z",
                      "payload": {"type": "user_message", "id": "synthetic-u1",
                                  "message": self.nonce + " correction: approved deployment region is ap-northeast-2."}}
        self.transcript = Path(self.env["CODEX_SESSIONS"]) / ("rollout-" + self.sid + ".jsonl")
        self.transcript.write_text(json.dumps(transcript) + "\n")
        self.mem("index")

    def cleanup_children(self):
        try:
            receipt = completion.read_receipt("codex", self.sid, self.env) or {}
        except completion.CompletionError:
            receipt = {}
        for key in ("command", "runner"):
            identity = receipt.get(key)
            if identity and completion.identity_alive(identity):
                try:
                    os.killpg(identity["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # A broken timeout wrapper may have moved the synthetic model out of
        # the command group. Kill only the exact fixture PID/start/boot tuple;
        # neither a reused PID nor any other runtime/model process is eligible.
        for invocation in self.invocations():
            identity = invocation.get("identity")
            if identity and completion.identity_alive(identity):
                try:
                    os.kill(identity["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def invocations(self):
        try:
            lines = Path(self.env["SYNTHETIC_INVOCATIONS"]).read_text().splitlines()
        except FileNotFoundError:
            return []
        result = []
        for line in lines:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # The fixture may still be appending its last line.
        return result

    def wait_invocations(self, count, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.invocations()
            if len(result) >= count:
                return result
            time.sleep(0.025)
        self.fail("synthetic model invocation did not start")

    def assert_model_dead(self, identity):
        self.assertTrue(completion._valid_identity(identity))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if completion.identity_state(identity) == "dead":
                return
            try:
                state = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                return
            if state == "Z":
                return  # Host init may not yet have reaped the killed orphan.
            time.sleep(0.01)
        self.fail("synthetic model survived completion process-group timeout")

    def wait_lease_release(self):
        key = completion._key("codex", self.sid)
        lock = os.open(Path(self.env["MEM_SESSION_COMPLETION_RECEIPTS"]) / (key + ".lock"), os.O_RDWR)
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    completion.fcntl.flock(lock, completion.fcntl.LOCK_EX | completion.fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    time.sleep(0.01)
            self.fail("synthetic completion did not release its lease")
        finally:
            os.close(lock)

    def mem(self, *args):
        result = subprocess.run([sys.executable, str(MEM), *args], cwd=self.project,
                                env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def assert_stored(self, nonce):
        found = self.mem("recall", nonce, "--full")
        self.assertNotIn("(no store matches)", found)
        self.assertIn(nonce + " approved deployment region is ap-northeast-2.", found)
        self.assertIn("user-correction_", found)

    def invoke(self, env=None):
        start = time.monotonic()
        result = subprocess.run([sys.executable, str(BRIDGE)], cwd=self.project,
                                input=json.dumps({"cwd": str(self.project), "session_id": self.sid,
                                                  "transcript_path": str(self.transcript)}),
                                env=env or self.env, text=True, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertLess(time.monotonic() - start, 2.9)
        return result

    def terminal(self, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = completion.read_receipt("codex", self.sid, self.env)
            if result and result["state"] in ("completed", "failed"):
                return result
            time.sleep(0.05)
        self.fail("detached session completion did not terminate")

    def test_real_bridge_slow_curate_applies_and_advances_once(self):
        self.invoke()
        started = completion.read_receipt("codex", self.sid, self.env)
        self.assertIsNotNone(started)
        self.assertEqual(started["state"], "started")
        self.invoke()  # Duplicate while model is running.
        receipt = self.terminal()
        self.assertEqual(receipt["state"], "completed", receipt)
        self.assertEqual(receipt["memory_apply"], "not-asserted")
        self.assert_stored(self.nonce)
        marker = Path(self.env["MEM_STORE"]) / (".distill-state-" + self.sid)
        self.assertEqual(marker.read_text().strip(), "synthetic-u1")
        self.assertEqual(self.mem("distill", self.sid, "--source", "codex").strip(), "")
        self.invoke()  # Duplicate after terminal must also remain a no-op.
        calls = [json.loads(line) for line in Path(self.env["SYNTHETIC_INVOCATIONS"]).read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["role"], calls[0]["distill"], calls[0]["completion"]), ("worker", "1", "1"))
        self.assertTrue(calls[0]["has_curate_contract"])

    def test_same_session_resumed_with_new_transcript_runs_again(self):
        self.env["SYNTHETIC_DELAY"] = "0.1"
        self.invoke()
        before = self.terminal()
        next_nonce = self.nonce + "RESUMED"
        self.env["SYNTHETIC_NONCE"] = next_nonce
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type":"event_msg", "timestamp":"2026-09-07T12:00:00Z",
                "payload":{"type":"user_message", "id":"synthetic-u2",
                           "message":next_nonce + " correction: approved deployment region is ap-northeast-2."}}) + "\n")
        self.invoke()
        after = self.terminal()
        self.assertNotEqual(before["input_generation"], after["input_generation"])
        self.assertEqual(after["state"], "completed", after)
        self.assertEqual((Path(self.env["MEM_STORE"]) / (".distill-state-" + self.sid)).read_text().strip(), "synthetic-u2")
        self.assert_stored(next_nonce)
        self.assertEqual(len(Path(self.env["SYNTHETIC_INVOCATIONS"]).read_text().splitlines()), 2)

    def test_real_timeout_kills_model_and_preserves_unapplied_delta(self):
        self.env.update(SYNTHETIC_DELAY="60", MEM_SESSION_COMPLETION_TIMEOUT="30",
                        CODEX_DISTILL_TIMEOUT_CURATE="60")
        started = time.monotonic()
        self.invoke()  # Real bridge/preflight/GNU timeout, native 3s deadline.
        call = self.wait_invocations(1)[0]
        receipt = self.terminal(timeout=35)
        self.assertLess(time.monotonic() - started, 35)
        self.assertEqual((receipt["state"], receipt["reason"]), ("failed", "timeout"), receipt)
        self.assertEqual(receipt["memory_apply"], "not-asserted")
        self.assert_model_dead(call["identity"])
        marker = Path(self.env["MEM_STORE"]) / (".distill-state-" + self.sid)
        self.assertFalse(marker.exists())
        self.assertIn(self.nonce, self.mem("distill", self.sid, "--source", "codex"))
        self.assertIn("(no store matches)", self.mem("recall", self.nonce, "--full"))
        self.assertEqual(len(self.invocations()), 1)

    def test_active_new_generation_preserves_frontier_and_retries_delta(self):
        self.invoke()
        first_call = self.wait_invocations(1)[0]
        self.assertEqual(first_call["nonce"], self.nonce)
        next_nonce = self.nonce + "PENDING"
        with self.transcript.open("a") as out:
            out.write(json.dumps({"type":"event_msg", "timestamp":"2026-09-07T12:00:00Z",
                "payload":{"type":"user_message", "id":"synthetic-u2",
                           "message":next_nonce + " correction: approved deployment region is ap-northeast-2."}}) + "\n")
        # The first child captured the old environment at spawn. Only the
        # deferred/retried invocation sees the next synthetic memory action.
        self.env.update(SYNTHETIC_NONCE=next_nonce, SYNTHETIC_DELAY="0.1")
        deferred = self.invoke()
        self.assertEqual(deferred.stderr, "codex memory completion: active-input-generation\n")
        first_terminal = self.terminal()
        self.assertEqual(first_terminal["state"], "completed", first_terminal)
        marker = Path(self.env["MEM_STORE"]) / (".distill-state-" + self.sid)
        self.assertEqual(marker.read_text().strip(), "synthetic-u1")
        self.assert_stored(self.nonce)
        self.assertIn(next_nonce, self.mem("distill", self.sid, "--source", "codex"))
        self.assertIn("(no store matches)", self.mem("recall", next_nonce, "--full"))
        self.assertEqual(len(self.invocations()), 1)
        self.wait_lease_release()
        self.invoke()  # Same SID and unchanged pending generation are retried.
        second_terminal = self.terminal()
        self.assertEqual(second_terminal["state"], "completed", second_terminal)
        self.assertNotEqual(first_terminal["input_generation"], second_terminal["input_generation"])
        self.assert_stored(next_nonce)
        self.assertEqual(marker.read_text().strip(), "synthetic-u2")
        self.assertEqual(self.mem("distill", self.sid, "--source", "codex").strip(), "")
        calls = self.invocations()
        self.assertEqual([call["nonce"] for call in calls], [self.nonce, next_nonce])

    def test_missing_generation_is_explicit_and_does_not_read_content(self):
        bridge = load_bridge()
        self.assertIsNone(bridge.input_generation({"transcript_path": None}))
        link = self.base / "transcript-link"
        link.symlink_to(self.transcript)
        self.assertIsNone(bridge.input_generation({"transcript_path": str(link)}))
        self.assertEqual(len(bridge.input_generation({"transcript_path": str(self.transcript)})), 64)

    def test_nullable_payload_uses_default_but_unsafe_path_cannot_launch(self):
        bridge = load_bridge()
        payload = {"cwd":str(self.project), "session_id":self.sid, "transcript_path":None}
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(bridge, "load_payload", return_value=payload), \
             mock.patch.object(bridge, "run_preflight"), \
             mock.patch.object(completion, "launch", return_value={"state":"started"}) as launch, \
             mock.patch.dict(sys.modules, {"fleet":types.SimpleNamespace(interaction=types.SimpleNamespace(clear_wait=lambda *_: None)),
                                           "session_summary_trigger":types.SimpleNamespace(launch_trigger=lambda *_: False)}), \
             mock.patch.object(sys, "stderr", new_callable=io.StringIO) as err:
            self.assertEqual(bridge.main(), 0)
            self.assertEqual(launch.call_count, 1)
            self.assertNotIn("input_generation", launch.call_args.kwargs)
            self.assertEqual(err.getvalue(), "codex memory completion: input-generation-unavailable\n")
            launch.reset_mock()
            payload["transcript_path"] = "relative-unsafe"
            self.assertEqual(bridge.main(), 0)
            launch.assert_not_called()
            self.assertTrue(err.getvalue().endswith("codex memory completion: input-generation-invalid\n"))

    def test_failed_model_keeps_actual_delta_and_marker(self):
        self.env.update(SYNTHETIC_EXIT="17", SYNTHETIC_DELAY="0.1")
        self.invoke()
        receipt = self.terminal()
        # The existing session-end command can return success after model
        # failure. Its receipt deliberately does not claim an applied memory.
        self.assertEqual(receipt["memory_apply"], "not-asserted")
        self.assertFalse((Path(self.env["MEM_STORE"]) / (".distill-state-" + self.sid)).exists())
        self.assertIn(self.nonce, self.mem("distill", self.sid, "--source", "codex"))
        self.assertIn("(no store matches)", self.mem("recall", self.nonce, "--full"))
        self.assertNotIn("synthetic private error", json.dumps(receipt))

    def test_actual_bridge_worker_excluded_without_state(self):
        for key, value in {"AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_CHILD": "1",
                           "AGENT_DISPATCH_DEPTH": "1", "OPENCODE_DISPATCH_SLUG": "fixture",
                           "FLEET_TITLE_REFRESH": "1", "MEM_DISTILL": "1", "MEM_SESSION_COMPLETION": "1"}.items():
            with self.subTest(key=key):
                self.invoke({**self.env, key: value})
        self.assertEqual(list(Path(self.env["MEM_SESSION_COMPLETION_RECEIPTS"]).iterdir()), [])
        self.assertFalse(Path(self.env["SYNTHETIC_INVOCATIONS"]).exists())

    def test_optional_helper_hang_occurs_after_handoff_and_is_bounded(self):
        bridge = load_bridge()
        calls = []
        interaction = types.SimpleNamespace(clear_wait=lambda *_: time.sleep(10))
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(bridge, "load_payload", return_value={"cwd": str(self.project), "session_id": self.sid, "transcript_path": str(self.transcript)}), \
             mock.patch.object(bridge, "run_preflight"), \
             mock.patch.object(completion, "launch", side_effect=lambda *_a, **_k: calls.append("handoff") or {"state":"started"}), \
             mock.patch.dict(sys.modules, {"fleet": types.SimpleNamespace(interaction=interaction)}):
            start = time.monotonic()
            self.assertEqual(bridge.main(), 0)
            self.assertLess(time.monotonic() - start, 2.8)
        self.assertEqual(calls, ["handoff"])

    def test_route_clear_cleans_descendant_after_leader_exit(self):
        bridge = load_bridge()
        pidfile = self.base / "route-child.pid"
        fake = self.base / "route-clear"
        fake.write_text("#!" + sys.executable + "\n" +
                        "import os, signal, time\n" +
                        "pid=os.fork()\n" +
                        "if pid == 0:\n    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n    time.sleep(30)\n" +
                        "else:\n    open(" + repr(str(pidfile)) + ", 'w').write(str(pid))\n    time.sleep(0.1)\n")
        fake.chmod(0o700)
        with mock.patch.object(bridge, "PREFLIGHT", fake), mock.patch.dict(os.environ, self.env, clear=True):
            bridge.run_preflight("clear", timeout=0.5)
        pid = int(pidfile.read_text())
        # A killed orphan may remain a zombie until the host init reaps it.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("route-clear descendant survived group cleanup")


if __name__ == "__main__":
    unittest.main()
