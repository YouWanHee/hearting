#!/usr/bin/env python3
"""The two kept write gates: installed release copies and the shared checkout."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().with_name("core-write-guard.py")
_spec = importlib.util.spec_from_file_location("core_write_guard", HOOK)
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)


class CoreWriteGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.home = self.tmp / "home"
        (self.home / ".local/share/hearting/releases/v1/hooks").mkdir(parents=True)
        (self.home / ".claude/.harness/bundles/b1").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        self.env.start()
        os.environ.pop("XDG_DATA_HOME", None)
        self.primary = self.tmp / "repo"
        (self.primary / ".git/worktrees/feat").mkdir(parents=True)
        self.worktree = self.tmp / "repo-wt/feat"
        self.worktree.mkdir(parents=True)
        (self.worktree / ".git").write_text(
            f"gitdir: {self.primary}/.git/worktrees/feat\n", encoding="utf-8")

    def tearDown(self):
        self.env.stop()

    def test_installed_release_copies_are_refused(self):
        for target in (self.home / ".local/share/hearting/releases/v1/hooks/x.py",
                       self.home / ".claude/.harness/bundles/b1/y.md"):
            self.assertIn("installed release copy", G.violation(str(target), str(self.tmp)))

    def test_worktree_session_is_pointed_at_its_own_copy(self):
        reason = G.violation(str(self.primary / "utilities/a.py"), str(self.worktree))
        self.assertIn(str(self.worktree / "utilities/a.py"), reason)

    def test_allowed_writes(self):
        self.assertEqual(G.violation(str(self.worktree / "utilities/a.py"), str(self.worktree)), "")
        self.assertEqual(G.violation(str(self.primary / ".agent_reports/r.md"), str(self.worktree)), "")
        self.assertEqual(G.violation(str(self.primary / "utilities/a.py"), str(self.primary)), "")
        self.assertEqual(G.violation("relative.py", str(self.worktree)), "")

    def test_hook_modes(self):
        payload = {"tool_name": "Edit", "cwd": str(self.worktree),
                   "tool_input": {"file_path": str(self.primary / "core/CORE.md")}}
        env = {**os.environ}
        claude = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                                text=True, capture_output=True, env=env)
        self.assertEqual(claude.returncode, 2)
        self.assertIn("shared checkout", claude.stderr)
        patch = {"tool_name": "apply_patch", "cwd": str(self.worktree),
                 "tool_input": {"input": "*** Begin Patch\n*** Update File: "
                                         f"{self.primary}/core/CORE.md\n*** End Patch\n"}}
        codex = subprocess.run([sys.executable, str(HOOK), "--codex"], input=json.dumps(patch),
                               text=True, capture_output=True, env=env)
        self.assertEqual(codex.returncode, 0)
        self.assertEqual(json.loads(codex.stdout)["decision"], "block")
        check = subprocess.run([sys.executable, str(HOOK), "--check", "notes.md",
                                "--cwd", str(self.worktree)], text=True, capture_output=True, env=env)
        self.assertEqual(check.returncode, 0)
        garbage = subprocess.run([sys.executable, str(HOOK)], input="not json",
                                 text=True, capture_output=True, env=env)
        self.assertEqual(garbage.returncode, 0)


if __name__ == "__main__":
    unittest.main()
