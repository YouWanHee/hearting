#!/usr/bin/env python3
"""Hermetic OpenCode dispatch CLI and scoped worker-home projection fixture."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "opencode_dispatch_scope_fixture",
    ROOT / "adapters/opencode/bin/dispatch-headless.py",
)
DISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DISPATCH)


class OpenCodeDispatchScopeFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-opencode-dispatch-scope-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.worktree = self.base / "worktree"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "config", "user.email",
                        "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "config", "user.name", "Fixture"],
                       check=True)
        (self.worktree / "fixture.txt").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.worktree), "add", "fixture.txt"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "commit", "-qm", "fixture"], check=True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.artifact_root = self.base / ".agent_reports"
        self.artifact_root.mkdir()
        self.state_root = self.base / "state"
        self.state_root.mkdir()
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "AGENT_HOME": str(ROOT),
            "AGENT_ARTIFACT_ROOT": str(self.artifact_root),
            "AGENT_DISPATCH_JOBS": str(self.state_root / "jobs.log"),
            "HARNESS_STATE_ROOT": str(self.state_root),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_STATE_HOME": str(self.state_root),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "OPENCODE_RUNTIME_VERSION": "2.0.32",
            "OPENCODE_CONFIG_CONTENT": "{}",
            "FLEET_USAGE_STATE_DIR": str(self.state_root / "usage-state"),
        }

    def test_real_dispatch_surface_builds_worker_scope_and_native_identity(self):
        argv = [
            "dispatch-headless.py", "--dry-run", "--worktree", str(self.worktree),
            "--slug", "native-hook-fixture", "--capability", "autopilot-code",
            "--capability-mode", "dev", "--worker-mode", "dev/backend",
            "--worker-type", "stage", "--assigned-contract", "code-execute",
            "--unit", "dev/backend", "--dispatch-depth", "1", "--intensity", "standard",
            "--registered-worker", "1", "--launch-lifecycle", "foreground-scoped",
            "--foreground-timeout", "240", "--model", "openai/gpt-6.1-sol",
            "--variant", "runtime-default", "--prompt-text",
            "Run only the isolated guard fixture.",
        ]
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(DISPATCH.main(argv), 0)
        receipt = output.getvalue()
        self.assertIn("status=dry-run", receipt)
        self.assertIn(f"worktree={self.worktree}", receipt)
        self.assertIn(f"artifact_root={self.artifact_root}", receipt)
        self.assertIn("worker_mode=dev/backend", receipt)
        self.assertIn("dispatch_depth=1", receipt)

        with mock.patch.dict(os.environ, self.env, clear=True):
            nested = DISPATCH.prepare_nested_runtime(
                self.worktree, "att-native-fixture", state_root=self.state_root,
            )
            scoped = DISPATCH.scoped_external_directory_config(
                str(self.artifact_root), agent_home=str(ROOT), worktree=str(self.worktree),
                selected_agent="build",
            )
            launch_env = {
                **self.env,
                **{key: value for key, value in nested.items() if value is not None},
                "AGENT_SESSION_ROLE": "worker",
                "AGENT_DISPATCH_CHILD": "1",
                "AGENT_DISPATCH_DEPTH": "1",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-native-fixture",
                "AGENT_DISPATCH_WORKTREE": str(self.worktree),
                "OPENCODE_CONFIG_CONTENT": scoped,
            }
            projected = DISPATCH.prepare_worker_home(
                ROOT, "opencode", "stage", "att-native-fixture", env=launch_env,
            )
        effective = {**launch_env, **projected}
        self.assertTrue(Path(effective["HEARTING_WORKER_HOME"]).is_relative_to(self.state_root))
        self.assertIsNone(projected.get("OPENCODE_DB"))
        config = json.loads(effective["OPENCODE_CONFIG_CONTENT"])
        permission = config.get("permission", config.get("permissions", {}))
        if isinstance(permission, list):
            external = [row for row in permission if row.get("action") == "external_directory"]
            self.assertTrue(external)
            self.assertIn({"action": "external_directory", "resource": "*", "effect": "deny"},
                          external)
            self.assertIn({"action": "external_directory", "resource": str(self.artifact_root),
                           "effect": "allow"}, external)
        else:
            external = permission.get("external_directory", {})
            self.assertEqual(external.get("*"), "deny")
            self.assertEqual(external.get(str(self.artifact_root)), "allow")

        prompt = self.state_root / "probe.prompt"
        log = self.state_root / "probe.jsonl"
        command_args = type("DispatchArgs", (), {
            "worktree": str(self.worktree), "agent": "build", "attempt_id": "att-native-fixture",
            "resolved_model_settings": {"source": "inherit"},
        })()
        with mock.patch.dict(os.environ, self.env, clear=True):
            command = DISPATCH.shell_command(command_args, prompt, log)
        self.assertIn("hearting-worker-att-native-fixture", command)
        self.assertIn("--format json", command)


if __name__ == "__main__":
    unittest.main()
