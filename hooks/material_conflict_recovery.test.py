#!/usr/bin/env python3
"""Approved exact-file recovery must work before importing conflicted code."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "hooks/material-route-guard.py"


class ConflictRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("AGENT_", "GIT_"))}
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.target = self.repo / "app.py"
        self.target.write_text("# start\n# end\n")
        (self.repo / "other.py").write_text("# unrelated\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.branch = self.git("symbolic-ref", "--short", "HEAD").stdout.strip()
        self.git("checkout", "-qb", "theirs")
        self.target.write_text("# start\nfrom theirs import Added\n# end\n")
        self.git("commit", "-qam", "theirs")
        self.git("checkout", "-q", self.branch)
        self.target.write_text("# start\nfrom ours import Independent\n# end\n")
        self.git("commit", "-qam", "ours")
        result = self.git("merge", "--no-edit", "theirs", check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.git_dir = Path(self.git("rev-parse", "--absolute-git-dir").stdout.strip())
        self.marker = self.git_dir / "CLAUDE_MERGE_EDIT_OK"
        self.runtime = self.base / "runtime"
        (self.runtime / "hooks").mkdir(parents=True)
        (self.runtime / "utilities").mkdir()
        self.guard = self.runtime / "hooks/material-route-guard.py"
        shutil.copy2(GUARD, self.guard)
        # Normal import must fail; only the exact approved edit can return
        # before it. This reproduces the self-hosted dispatch_contract conflict.
        (self.runtime / "utilities/dispatch_contract.py").write_text("<<<<<<< unresolved\n")

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              text=True, capture_output=True, check=check)

    def cli(self, path=None, tool="Edit", *, cwd=None, env=None):
        return subprocess.run(
            [sys.executable, str(self.guard), "check", "--tool", tool,
             "--file", str(path or self.target), "--cwd", str(cwd or self.repo),
             "--session", "conflict-fixture"],
            env=env or self.env, text=True, capture_output=True,
        )

    def test_approved_exact_file_passes_cli_before_broken_import_without_writes(self):
        self.marker.touch()
        before = {str(path): path.read_bytes() for path in
                  (self.target, self.marker, self.git_dir / "index", self.git_dir / "MERGE_HEAD")}
        for tool in ("Edit", "Write", "edit", "write"):
            with self.subTest(tool=tool):
                result = self.cli(tool=tool)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(before, {name: Path(name).read_bytes() for name in before})

    def test_pretooluse_hook_passes_but_other_events_do_not(self):
        self.marker.touch()
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Write",
                   "cwd": str(self.repo), "tool_input": {"file_path": str(self.target)}}
        result = subprocess.run([sys.executable, str(self.guard)], input=json.dumps(payload),
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        payload["hook_event_name"] = "PostToolUse"
        result = subprocess.run([sys.executable, str(self.guard)], input=json.dumps(payload),
                                env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_no_approval_or_unrelated_file_has_no_recovery_exception(self):
        self.assertNotEqual(self.cli().returncode, 0)
        self.assertFalse(self.marker.exists())
        self.marker.touch()
        self.assertNotEqual(self.cli(self.repo / "other.py").returncode, 0)
        self.assertNotEqual(self.cli(self.repo / "new.py").returncode, 0)

    def test_staged_resolution_and_finished_operation_do_not_reuse_marker(self):
        self.marker.touch()
        self.target.write_text("# start\nfrom ours import Independent\nfrom theirs import Added\n# end\n")
        self.git("add", "app.py")
        self.assertNotEqual(self.cli().returncode, 0)
        self.git("commit", "-qm", "union")
        self.assertNotEqual(self.cli().returncode, 0)

    def test_marker_without_operation_and_detached_head_do_not_pass(self):
        self.marker.touch()
        (self.git_dir / "MERGE_HEAD").unlink()
        self.assertNotEqual(self.cli().returncode, 0)
        # Detached HEAD alone is never an approved conflict operation.
        head = self.git("rev-parse", "HEAD").stdout.strip()
        (self.git_dir / "HEAD").write_text(head + "\n")
        self.assertNotEqual(self.cli().returncode, 0)

    def test_symlink_target_ancestor_and_approval_marker_do_not_pass(self):
        self.marker.touch()
        contents = self.target.read_bytes()
        elsewhere = self.base / "outside.py"
        elsewhere.write_bytes(contents)
        self.target.unlink()
        self.target.symlink_to(elsewhere)
        self.assertNotEqual(self.cli().returncode, 0)
        self.target.unlink()
        self.target.write_bytes(contents)
        alias = self.base / "repo-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assertNotEqual(self.cli(alias / "app.py").returncode, 0)
        self.marker.unlink()
        self.marker.symlink_to(elsewhere)
        self.assertNotEqual(self.cli().returncode, 0)

    def test_regular_worktree_file_cannot_hide_a_symlink_index_stage(self):
        self.marker.touch()
        # Replace one conflicted stage with a symlink mode while leaving the
        # working file regular. It is no longer a regular-file-only conflict.
        stage = self.git("ls-files", "--unmerged", "--", "app.py").stdout.splitlines()[0]
        metadata, path = stage.split("\t", 1)
        _mode, oid, number = metadata.split()
        result = subprocess.run(["git", "-C", str(self.repo), "update-index", "--index-info"],
                                input=f"120000 {oid} {number}\t{path}\n", env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.cli().returncode, 0)

    def test_shell_artifact_and_worker_paths_keep_existing_guards(self):
        self.marker.touch()
        for tool in ("Bash", "ArtifactWrite", "MultiEdit", "NotebookEdit"):
            with self.subTest(tool=tool):
                self.assertNotEqual(self.cli(tool=tool).returncode, 0)
        env = {**self.env, "AGENT_DISPATCH_ATTEMPT_ID": "att-worker"}
        self.assertNotEqual(self.cli(env=env).returncode, 0)

    def test_foreign_cwd_and_literal_pathspec_cannot_borrow_unmerged_entry(self):
        self.marker.touch()
        self.assertNotEqual(self.cli(cwd=self.base).returncode, 0)
        wildcard = self.repo / "*.py"
        wildcard.write_text("# not an index conflict\n")
        self.assertNotEqual(self.cli(wildcard).returncode, 0)

    def test_linked_worktree_needs_its_own_marker_and_ignores_foreign_git_environment(self):
        self.marker.touch()
        linked = self.base / "linked"
        self.git("worktree", "add", "-qb", "linked", str(linked), "HEAD")
        result = subprocess.run(["git", "-C", str(linked), "merge", "--no-edit", "theirs"],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        foreign_env = {**self.env, "GIT_DIR": str(self.git_dir),
                       "GIT_INDEX_FILE": str(self.git_dir / "index"),
                       "GIT_WORK_TREE": str(self.repo)}
        self.assertNotEqual(self.cli(linked / "app.py", cwd=linked, env=foreign_env).returncode, 0)
        result = subprocess.run(["git", "-C", str(linked), "rev-parse", "--absolute-git-dir"],
                                env=self.env, text=True, capture_output=True, check=True)
        (Path(result.stdout.strip()) / "CLAUDE_MERGE_EDIT_OK").touch()
        self.assertEqual(self.cli(linked / "app.py", cwd=linked, env=foreign_env).returncode, 0)

    def test_cherry_pick_and_rebase_operation_states_remain_narrow(self):
        self.marker.touch()
        merge_head = self.git_dir / "MERGE_HEAD"
        head = merge_head.read_bytes()
        merge_head.unlink()
        cherry = self.git_dir / "CHERRY_PICK_HEAD"
        cherry.write_bytes(head)
        self.assertEqual(self.cli().returncode, 0)
        cherry.unlink()
        for name in ("rebase-merge", "rebase-apply"):
            operation = self.git_dir / name
            operation.mkdir()
            self.assertEqual(self.cli().returncode, 0)
            self.assertNotEqual(self.cli(self.repo / "other.py").returncode, 0)
            operation.rmdir()


if __name__ == "__main__":
    unittest.main()
