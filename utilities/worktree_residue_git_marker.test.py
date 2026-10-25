from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import git_repository as G


SCRIPT = Path(__file__).with_name("worktree-residue.py")


class WorktreeResidueGitMarkerTest(unittest.TestCase):
    def test_empty_marker_is_refused_even_with_ambient_git_selection(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True,
                           env=G.git_environment())
            candidate = root / "candidate"
            (candidate / ".git").mkdir(parents=True)
            env = {**os.environ, "GIT_DIR": str(repository / ".git"),
                   "GIT_WORK_TREE": str(repository), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--worktree", str(candidate)],
                text=True, capture_output=True, env=env, timeout=10, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("reason=not-a-git-worktree", result.stdout)

    def test_valid_worktree_remains_eligible_for_read_only_scan(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True,
                           env=G.git_environment())
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--worktree", str(root)],
                text=True, capture_output=True, env={**os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1"}, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("status=check", result.stdout)


if __name__ == "__main__":
    unittest.main()
