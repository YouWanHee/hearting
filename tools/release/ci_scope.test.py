#!/usr/bin/env python3
"""Exercise change selection against real Git histories and release baselines."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ci_scope as SCOPE


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.git("init", "-q")
        self.commit("core/CORE.md")
        self.git("tag", "v1.0.0")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.context = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True)

    def commit(self, path):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(p.read_text() + "change\n" if p.exists() else "change\n")
        self.git("add", "--", path)
        self.git("commit", "-qm", "fixture")

    def select(self, event=None, released_tag="v1.0.0", **context):
        return SCOPE.select(self.repo, event or {}, dict(self.context, **context),
                            released_tag=released_tag)[0]

    def test_public_documents_and_project_bootstrap_skip_expensive_suites(self):
        for path in ("README.md", "docs/example.md", "AGENTS.md"):
            self.commit(path)
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")
        self.git("add", "CLAUDE.md")
        self.git("commit", "-qm", "project bootstrap link")
        self.assertFalse(self.select())
        self.assertTrue(all(not SCOPE.PLAN.is_release_relevant(path)
                            for path in ("README.md", "docs/example.md", "AGENTS.md", "CLAUDE.md")))

    def test_docs_cannot_hide_unreleased_code_from_previous_push(self):
        self.commit("utilities/runtime.py")
        self.commit("README.md")
        self.assertTrue(self.select())
        self.git("tag", "v1.0.1")
        self.commit("README.md")
        self.assertTrue(self.select())  # Tag exists, but is not published yet.
        self.assertFalse(self.select(released_tag="v1.0.1"))

    def test_runtime_instructions_tests_and_unknown_paths_still_run_full(self):
        for path in ("core/CORE.md", "capabilities/test.md", "roles/README.md",
                     "adapters/codex/AGENTS.md", ".github/workflows/checks.yml",
                     "tools/example.test.py", "unknown.md", "docs/example.py", ".gitignore"):
            with self.subTest(path=path):
                self.assertFalse(SCOPE.documentation_path(path))

    def test_runtime_file_renamed_into_docs_does_not_skip_checks(self):
        (self.repo / "docs").mkdir()
        self.git("mv", "core/CORE.md", "docs/core.md")
        self.git("commit", "-qm", "move")
        self.assertTrue(self.select())

    def test_missing_tag_or_invalid_pr_base_runs_full(self):
        self.commit("README.md")
        self.git("tag", "-d", "v1.0.0")
        self.assertTrue(self.select())
        for event in ({}, {"pull_request": {"base": {"sha": "missing"}}}):
            self.assertTrue(self.select(event, GITHUB_EVENT_NAME="pull_request"))

    def test_pr_uses_merge_base_and_manual_tag_validation_is_full(self):
        self.commit("README.md")
        event = {"pull_request": {"base": {"sha": self.base}}}
        self.assertFalse(self.select(event, GITHUB_EVENT_NAME="pull_request"))
        self.assertTrue(self.select(GITHUB_EVENT_NAME="workflow_dispatch"))
        self.assertTrue(self.select(GITHUB_EVENT_NAME="workflow_call"))
        self.assertTrue(self.select(GITHUB_REF="refs/tags/v1.0.1"))
        self.commit("tools/runtime.py")
        self.assertTrue(self.select(event, GITHUB_EVENT_NAME="pull_request"))

    def test_cli_unknown_event_prints_full_validation(self):
        event = self.repo / "event.json"
        event.write_text(json.dumps({}))
        result = subprocess.run(
            ["python3", str(SCOPE.ROOT / "tools/release/ci_scope.py")], capture_output=True, text=True,
            env=dict(os.environ, GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_EVENT_PATH=str(event)),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("full_tests=true\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
