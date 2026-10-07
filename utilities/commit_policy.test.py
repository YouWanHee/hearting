#!/usr/bin/env python3
"""Same launch, same commit decision, on every adapter wrapper (audit §4 #8, A6)."""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import commit_policy  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")


def calls(harness: str) -> set[str]:
    tree = ast.parse((ROOT / "adapters" / harness / "bin" / "dispatch-headless.py").read_text(encoding="utf-8"))
    return {node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "commit_policy"}


def common_calls() -> set[str]:
    tree = ast.parse((ROOT / "utilities" / "dispatch_wrapper_common.py").read_text(encoding="utf-8"))
    return {node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "commit_policy"}


class CommitPolicyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.primary = base / "primary"
        self.primary.mkdir()
        git = ["git", "-C", str(self.primary)]
        subprocess.run([*git[:1], "init", "-q", str(self.primary)], check=True)
        subprocess.run([*git, "-c", "user.email=f@example.com", "-c", "user.name=F",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        self.linked = base / "linked"
        subprocess.run([*git, "worktree", "add", "-q", "-b", "fx", str(self.linked)], check=True)

    def args(self, worker_type, worktree=None, **extra):
        return argparse.Namespace(worker_type=worker_type, write_scope="source/**",
                                  worktree=str(worktree or self.linked), agent_home=self.primary, **extra)

    def test_who_may_commit(self):
        owner, stage = self.args("owner"), self.args("stage")
        sealed = self.args("stage", commit_expected=True)
        slice_ = self.args("stage", commit_expected=True, subsession_id="slice-1")
        self.assertEqual([commit_policy.may_commit(a) for a in (owner, stage, sealed, slice_)],
                         [True, False, True, False])
        self.assertEqual([commit_policy.no_commit_stage(a) for a in (owner, stage, sealed, slice_)],
                         [False, True, False, True])
        # the primary checkout is not a linked worktree: nothing to hold back there
        self.assertFalse(commit_policy.no_commit_stage(self.args("stage", worktree=self.primary)))
        # a stage that edits no source is not a no-commit worker
        self.assertFalse(commit_policy.no_commit_stage(
            argparse.Namespace(worker_type="stage", write_scope="reports/**", worktree=str(self.linked))))

    def test_prompt_row_and_git_metadata_follow_the_decision(self):
        stage, owner = self.args("stage"), self.args("owner")
        self.assertEqual(commit_policy.prompt_clause(stage), commit_policy.PROMPT_CLAUSE)
        self.assertEqual(commit_policy.registry_fragment(stage), ",no_commit=1")
        self.assertEqual((commit_policy.prompt_clause(owner), commit_policy.registry_fragment(owner)), ("", ""))
        self.assertNotIn("Claude", commit_policy.PROMPT_CLAUSE)  # no harness is named as the committer
        common = (self.primary / ".git").resolve()
        self.assertEqual(commit_policy.commit_git_metadata_dirs(owner),
                         ((common / "worktrees" / "linked").resolve(), common / "objects", common / "refs", common / "logs"))
        self.assertEqual(commit_policy.commit_git_metadata_dirs(stage), ())
        self.assertEqual(commit_policy.commit_git_metadata_dirs(self.args("owner", worktree=self.primary)), ())

    def test_every_wrapper_asks_the_same_module(self):
        # append_job is shared in dispatch_wrapper_common: the registry
        # fragment lives there now, reached through each wrapper's append_job.
        shared = common_calls()
        self.assertIn("registry_fragment", shared)
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                used = calls(harness) | shared
                self.assertLessEqual({"prompt_clause", "registry_fragment"}, used)
        # the allowlist realization asks the shared answer instead of re-deriving it
        self.assertIn("may_commit", calls("claude"))



class OpenCodeRealizationTest(unittest.TestCase):
    """OpenCode refuses `git commit` to a no-commit worker through its own permission rules."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "opencode_dispatch_headless_commit", ROOT / "adapters/opencode/bin/dispatch-headless.py")
        cls.wrapper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.wrapper)

    def deny(self, config, agent=None):
        import json
        return json.loads(self.wrapper.deny_commands(json.dumps(config), commit_policy.COMMIT_COMMANDS, agent))

    def test_the_global_and_agent_bash_rules_end_with_the_denies(self):
        denies = {"git commit": "deny", "git commit *": "deny"}
        self.assertEqual(self.deny({})["permission"], {"bash": denies})
        out = self.deny({"permission": {"bash": "allow", "edit": "allow"}})["permission"]
        self.assertEqual(list(out), ["edit", "bash"])                       # last match wins
        self.assertEqual(out["bash"], {"*": "allow", **denies})
        out = self.deny({"permission": {"bash": {"git *": "allow"}}})["permission"]["bash"]
        self.assertEqual(out, {"git *": "allow", **denies})
        out = self.deny({"permission": "allow", "agent": {"build": {"permission": {"*": "allow"}}}}, "build")
        self.assertEqual(out["permission"], {"*": "allow", "bash": denies})
        self.assertEqual(out["agent"]["build"]["permission"], {"*": "allow", "bash": denies})

    def test_the_launch_applies_it_only_to_a_no_commit_stage(self):
        source = (ROOT / "adapters/opencode/bin/dispatch-headless.py").read_text(encoding="utf-8")
        self.assertIn("if commit_policy.no_commit_stage(args):", source)
        self.assertIn("deny_commands(", source)

if __name__ == "__main__":
    unittest.main()
