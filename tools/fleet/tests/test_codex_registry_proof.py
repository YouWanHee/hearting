#!/usr/bin/env python3
"""A launcher registration alone does not prove a daemon-attached Codex TUI thread."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet import session_registry  # noqa: E402
from fleet.collectors import codex, procscan  # noqa: E402

THREAD = "01a0fa57-7f0e-7eb0-bffe-1d56df94e90c"


class RegistryProofTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = os.environ.get("FLEET_SESSION_REGISTRY_DIR")
        os.environ["FLEET_SESSION_REGISTRY_DIR"] = self._tmp.name
        self.addCleanup(self._restore)
        self.proc = subprocess.Popen(["sleep", "60"], cwd=self._tmp.name)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.kill)

    def _restore(self):
        if self._old is None:
            os.environ.pop("FLEET_SESSION_REGISTRY_DIR", None)
        else:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = self._old

    def _write(self, **fields):
        record = {"sessionId": THREAD, "procStart": procscan.read_proc_start(self.proc.pid),
                  "harness": "codex", "kind": "codex-tui"}
        record.update(fields)
        session_registry.write("codex", self.proc.pid, record)

    def test_a_matching_launcher_record_does_not_prove_the_thread(self):
        self._write()
        self.assertEqual(codex._registered_thread(self.proc.pid), THREAD)
        self.assertIsNone(codex.session_id_of_process(self.proc.pid, lambda: []))

    def test_a_recycled_pid_is_not_proof(self):
        self._write(procStart="1")
        self.assertIsNone(codex.session_id_of_process(self.proc.pid, lambda: []))

    def test_a_record_without_a_start_time_or_a_thread_is_not_proof(self):
        self._write(procStart=None)
        self.assertIsNone(codex.session_id_of_process(self.proc.pid, lambda: []))
        self._write(sessionId="not-a-thread")
        self.assertIsNone(codex.session_id_of_process(self.proc.pid, lambda: []))

    def test_no_record_keeps_the_existing_behavior(self):
        self.assertIsNone(codex.session_id_of_process(self.proc.pid, lambda: []))


if __name__ == "__main__":
    unittest.main()
