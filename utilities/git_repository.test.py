from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import git_repository as G


class GitRepositoryTest(unittest.TestCase):
    def git(self, *args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                       env=G.git_environment())

    def test_classifies_empty_marker_as_non_git_and_broken_marker_as_error(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            plain = root / "plain"
            plain.mkdir()
            self.assertEqual(G.inspect(plain)["state"], "non-git")
            empty = root / "empty"
            empty.mkdir()
            (empty / ".git").mkdir()
            self.assertEqual(G.inspect(empty)["state"], "non-git")
            broken = root / "broken"
            broken.mkdir()
            (broken / ".git").write_text("gitdir: missing\n", encoding="utf-8")
            self.assertEqual(G.inspect(broken)["state"], "error")

    def test_no_possible_worktree_marker_skips_git_process(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "ordinary"
            root.mkdir()
            child = root / "child"
            child.mkdir()
            original_run = G.subprocess.run
            git_processes = []

            def count_git_processes(args, **kwargs):
                if args and Path(args[0]).name == "git":
                    git_processes.append(args)
                return original_run(args, **kwargs)

            with mock.patch.object(G.subprocess, "run", side_effect=count_git_processes):
                self.assertEqual(G.inspect(child)["state"], "non-git")
            self.assertEqual(git_processes, [])

    def test_empty_inner_marker_does_not_hide_outer_repository(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            self.git("init", str(repository), cwd=root)
            inner = repository / "inner"
            (inner / ".git").mkdir(parents=True)
            self.assertEqual(
                G.inspect(inner), {"state": "git", "root": str(repository.resolve())}
            )

    def test_marker_created_during_negative_scan_forces_git_probe(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            inner = repository / "inner"
            (inner / ".git").mkdir(parents=True)
            original_snapshot = G._negative_ancestry_snapshot
            snapshots = []

            def create_outer_repo_after_snapshot(path):
                snapshot = original_snapshot(path)
                snapshots.append(snapshot)
                if len(snapshots) == 1:
                    self.git("init", str(repository), cwd=root)
                return snapshot

            with mock.patch.object(
                G, "_negative_ancestry_snapshot", side_effect=create_outer_repo_after_snapshot
            ):
                observed = G.inspect(inner)
            self.assertEqual(observed, {"state": "git", "root": str(repository.resolve())})
            self.assertEqual(len(snapshots), 2)

    def test_follows_real_gitfile_and_rejects_ambient_repository_selection(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            main = root / "main"
            main.mkdir()
            self.git("init", str(main), cwd=root)
            self.git("-C", str(main), "-c", "user.name=Test", "-c",
                     "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "initial",
                     cwd=root)
            linked = root / "linked"
            self.git("worktree", "add", "--detach", str(linked), cwd=main)
            result = G.inspect(linked / "child")
            self.assertEqual(result, {"state": "git", "root": str(linked.resolve())})
            contamination = {**G.git_environment(), "GIT_DIR": str(main / ".git"),
                             "GIT_WORK_TREE": str(main)}
            with mock.patch.dict(os.environ, contamination, clear=True):
                plain = root / "plain"
                plain.mkdir()
                self.assertEqual(G.inspect(plain)["state"], "non-git")

    def test_expected_root_rejects_outer_repository_as_marker_evidence(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            self.git("init", str(root), cwd=root)
            child = root / "ordinary"
            child.mkdir()
            (child / ".git").mkdir()
            self.assertEqual(G.inspect(child, expected_root=child)["state"], "non-git")

    def test_bare_repository_is_not_a_worktree_for_generic_classification(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            bare = Path(tmp) / "bare.git"
            self.git("init", "--bare", str(bare), cwd=Path(tmp))
            observed = G.inspect(bare)
            self.assertEqual(observed["state"], "non-git")


if __name__ == "__main__":
    unittest.main()
