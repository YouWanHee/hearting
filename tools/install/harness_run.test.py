#!/usr/bin/env python3
"""`hearting run <utility>`: the one PATH entry that needs no session environment."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "tools/install/harness.sh"


def run(*argv, env=None):
    base = {k: v for k, v in os.environ.items() if k != "AGENT_HOME"}
    return subprocess.run(["sh", str(LAUNCHER), "run", *argv], text=True, capture_output=True,
                          env={**base, **(env or {})}, check=False)


class HarnessRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "core").mkdir()
        (self.home / "core/CORE.md").write_text("x\n")
        (self.home / "utilities").mkdir()
        (self.home / "utilities/echo-args.py").write_text(
            "import os, sys\nprint(os.environ['AGENT_HOME'], *sys.argv[1:])\n")

    def test_a_session_root_runs_its_own_utility_by_name(self):
        for name in ("echo-args", "echo-args.py"):
            with self.subTest(name=name):
                done = run(name, "--route", "r.json", env={"AGENT_HOME": str(self.home)})
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(done.stdout.split(), [str(self.home), "--route", "r.json"])

    def test_without_a_session_root_the_launcher_root_answers(self):
        done = run("agent-home")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(done.stdout.strip()).resolve(), ROOT)

    def test_a_path_or_an_unknown_name_is_refused(self):
        for name in ("../core/CORE.md", ".hidden", "no-such-utility"):
            with self.subTest(name=name):
                done = run(name, env={"AGENT_HOME": str(self.home)})
                self.assertEqual(done.returncode, 2)
                self.assertIn("harness:", done.stderr)


if __name__ == "__main__":
    unittest.main()
