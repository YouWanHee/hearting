#!/usr/bin/env python3
"""git < 2.31 has no `rev-parse --path-format=absolute`: it echoes the unknown
option as an extra output line and exits 0. A PATH shim emulates that, so the
route worktree preparation and the index-lock binding must ask git plainly and
absolutize a relative answer themselves."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROUTE = load("route_path_format", HERE / "capability-route.py")
LOCK = load("git_index_lock_path_format", Path(__file__).with_name("git_index_lock.py"))

SHIM = """#!{python}
import os, sys
args = sys.argv[1:]
if "--path-format=absolute" in args:
    print("--path-format=absolute", flush=True)
    args = [a for a in args if a != "--path-format=absolute"]
os.execv(os.environ["H1_REAL_GIT"], [os.environ["H1_REAL_GIT"], *args])
"""


class OldGitPathFormatTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-h1-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        real_git = shutil.which("git")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "git"
        shim.write_text(SHIM.format(python=sys.executable), encoding="utf-8")
        shim.chmod(0o755)
        patched = mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "H1_REAL_GIT": real_git,
        })
        patched.start()
        self.addCleanup(patched.stop)
        self.real_git = real_git
        origin = self.root / "origin.git"
        self.primary = self.root / "primary"
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.git(self.root, "clone", "-q", str(origin), str(self.primary))
        self.git(self.primary, "config", "user.name", "Fixture")
        self.git(self.primary, "config", "user.email", "fixture@example.test")
        (self.primary / "a.txt").write_text("base\n", encoding="utf-8")
        self.git(self.primary, "add", "a.txt")
        self.git(self.primary, "commit", "-qm", "base")
        self.git(self.primary, "push", "-q", "-u", "origin", "main")
        (self.primary / "sub").mkdir()
        self.linked = self.root / "linked"
        self.git(self.primary, "worktree", "add", "-q", "-b", "topic", str(self.linked))
        self.reports = self.root / "primary" / ".agent_reports"
        self.reports.mkdir()

    def git(self, cwd, *args):
        return subprocess.run([self.real_git, "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_shim_emulates_old_git(self):
        out = subprocess.run(["git", "-C", str(self.primary), "rev-parse",
                              "--path-format=absolute", "--git-common-dir"],
                             check=True, capture_output=True, text=True).stdout
        self.assertEqual(out.splitlines()[0], "--path-format=absolute")
        self.assertEqual(len(out.splitlines()), 2)

    def test_worktree_preparation_from_primary_and_linked(self):
        made = ROUTE.prepare_isolated_worktree(self.linked, "demo", artifact_root=self.reports)
        self.assertEqual(made["state"], "created", made)
        made = ROUTE.prepare_isolated_worktree(self.primary, "demo2", artifact_root=self.reports)
        self.assertEqual(made["state"], "created", made)

    def test_index_lock_binds_relative_and_linked_paths(self):
        bound = LOCK._run_git_index(["git", "-C", "sub", "commit"], str(self.primary),
                                    dict(os.environ), 5.0)
        self.assertIsNotNone(bound)
        self.assertEqual(bound["index"], os.path.realpath(self.primary / ".git" / "index"))
        name = self.linked.name
        bound = LOCK._run_git_index(["git", "commit"], str(self.linked), dict(os.environ), 5.0)
        self.assertIsNotNone(bound)
        self.assertEqual(
            bound["index"],
            os.path.realpath(self.primary / ".git" / "worktrees" / name / "index"))


if __name__ == "__main__":
    unittest.main()
