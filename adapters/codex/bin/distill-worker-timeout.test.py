#!/usr/bin/env python3
"""Actual worker/GNU timeout plumbing with a synthetic transcript and model."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
WORKER = ROOT / "adapters/codex/bin/distill-worker.sh"


class CompletionTimeout(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="distill-completion-timeout-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        home = self.base / "home"; home.mkdir()
        self.project = self.base / "project"; self.project.mkdir()
        self.store = self.base / "store"
        agent = self.base / "agent"; (agent / "core").mkdir(parents=True)
        (agent / "core/CORE.md").write_text("# Isolated source-home fixture\n")
        self.bin = self.base / "bin"; self.bin.mkdir()
        self.sessions = self.base / "sessions"; self.sessions.mkdir()
        self.sid = "completion-timeout-fixture"
        (self.sessions / (self.sid + ".jsonl")).write_text(json.dumps({
            "type": "event_msg", "timestamp": "2026-09-07T00:00:00Z",
            "payload": {"type": "user_message", "id": "synthetic-u1",
                        "message": "Synthetic memory worker timeout fixture."},
        }) + "\n")
        self.model_receipt = self.base / "model.json"
        model = self.bin / "codex"
        model.write_text("#!" + sys.executable + "\n"
            "import json, os, signal, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path(" + repr(str(self.model_receipt)) + ").write_text(json.dumps({"
            "'pid': os.getpid(), 'pgid': os.getpgrp()}))\n"
            "time.sleep(30)\n")
        model.chmod(0o755)
        self.env = {
            "HOME": str(home), "PATH": str(self.bin) + os.pathsep + os.defpath,
            "AGENT_HOME": str(agent), "MEM_STORE": str(self.store),
            "XDG_CONFIG_HOME": str(self.base / "config"),
            "XDG_DATA_HOME": str(self.base / "data"),
            "XDG_STATE_HOME": str(self.base / "state"),
            "MEM_PROJECTS": str(self.base / "projects"),
            "MEM_WRITE_EVENTS": str(self.base / "state/write.jsonl"),
            "MEM_RECALL_EVENTS": str(self.base / "state/recall.jsonl"),
            "MEM_RECALL_RECEIPTS": str(self.base / "state/receipts"),
            "AGENT_MODEL_GOVERNOR_ROOT": str(self.base / "governor"),
            "AGENT_ARTIFACT_ROOT": str(self.project / ".agent_reports"),
            "CODEX_SESSIONS": str(self.sessions),
            "CODEX_DISTILL_ENABLE": "1", "CODEX_DISTILL_APPLY": "1",
            "CODEX_DISTILL_CONTRACT_ACCEPTED": "1",
            "CODEX_DISTILL_TIMEOUT": "0.2", "MEM_SESSION_COMPLETION": "1",
        }
        init = subprocess.run([sys.executable, str(ROOT / "tools/memory/mem.py"), "index"],
                              env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(init.returncode, 0, init.stderr)

    def run_worker(self):
        started = time.monotonic()
        process = subprocess.Popen(["sh", str(WORKER), self.sid, str(self.project)],
            env=self.env, cwd=self.project, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=10)
            return process.pid, process.returncode, stdout, stderr, time.monotonic() - started
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)

    def assert_delta_preserved(self):
        self.assertFalse((self.store / (".distill-state-" + self.sid)).exists())
        result = subprocess.run([sys.executable, str(ROOT / "tools/memory/mem.py"),
                                 "distill", self.sid, "--source", "codex"],
                                env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Synthetic memory worker timeout fixture", result.stdout)

    def test_model_keeps_runner_group_and_is_killed_after_ignored_term(self):
        pgid, rc, stdout, stderr, elapsed = self.run_worker()
        self.assertNotEqual(rc, 0, (stdout, stderr))
        evidence = json.loads(self.model_receipt.read_text())
        self.assertEqual(evidence["pgid"], pgid)
        self.assertLess(elapsed, 8)
        # GNU timeout must finish independently; run_worker's final group kill
        # is only a fixture cleanup and cannot make communicate() return early.
        self.assert_delta_preserved()

    def test_unsupported_foreground_mode_fails_before_model(self):
        timeout = self.bin / "timeout"
        timeout.write_text("#!/bin/sh\nexit 125\n"); timeout.chmod(0o755)
        _pgid, rc, stdout, stderr, _elapsed = self.run_worker()
        self.assertEqual(rc, 69, (stdout, stderr))
        self.assertIn("completion-timeout-unavailable", stderr)
        self.assertFalse(self.model_receipt.exists())
        self.assert_delta_preserved()

    def test_zero_timeout_is_not_an_unbounded_escape(self):
        self.env["CODEX_DISTILL_TIMEOUT"] = "0.00"
        _pgid, rc, stdout, stderr, _elapsed = self.run_worker()
        self.assertEqual(rc, 69, (stdout, stderr))
        self.assertIn("completion-timeout-unavailable", stderr)
        self.assertFalse(self.model_receipt.exists())
        self.assert_delta_preserved()


if __name__ == "__main__":
    unittest.main()
