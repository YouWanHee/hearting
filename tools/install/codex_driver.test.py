#!/usr/bin/env python3
"""The Codex driver's plugin path must reuse one launcher transaction.

`drivers.codex.install(plugin=True)` appends `codex_launcher.install()`'s own
result to its action list; it must never re-implement launcher install logic
or reshape the result. These tests pin that the driver's `managed-launcher`
action carries the launcher's protected/unchanged shape verbatim, and that
`CodexUnavailableError`/`CodexLauncherError` map to the documented
`skipped-unavailable`/`blocked` action shapes without any other transaction.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import codex_launcher  # noqa: E402
import native_agent_payload  # noqa: E402
from drivers import codex as codex_driver  # noqa: E402


def _no_plan_entries(_runtimes, scope="global"):
    return {"codex": []}


class _IsolatedRuntimeHomeTestCase(unittest.TestCase):
    """Base for every test in this file that reaches ``codex_driver.install()``
    or ``codex_driver.checks()``.

    ``paths.runtime_home()`` reads ``CODEX_HOME`` *before* falling back to
    ``$HOME/.codex``, so redirecting ``HOME`` alone is not isolation; this
    cycle already caused two accidental writes into the real ``~/.codex`` by
    getting that wrong. Centralizing the full redirect here (rather than
    class-local ``setUp``) means a test added later to a new class in this
    file inherits the same protection instead of silently regressing it.
    """

    def setUp(self) -> None:
        super().setUp()
        self._isolated_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._isolated_tmp.cleanup)
        root = Path(self._isolated_tmp.name)
        self.agent_home = root / "agent-home"
        self.codex_home = root / "codex-home"
        self.agent_home.mkdir(parents=True)
        self.codex_home.mkdir(parents=True)
        env_patch = mock.patch.dict(
            os.environ,
            {
                "HOME": str(root / "home"),
                "AGENT_HOME": str(self.agent_home),
                "CODEX_HOME": str(self.codex_home),
                "CLAUDE_CONFIG_DIR": str(root / "claude-config"),
                "XDG_CONFIG_HOME": str(root / "xdg-config"),
                "XDG_STATE_HOME": str(root / "xdg-state"),
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)


class CodexDriverLauncherReuseTest(_IsolatedRuntimeHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(codex_driver.projector, "plan", side_effect=_no_plan_entries).start()
        mock.patch.object(codex_driver, "_plugin_action", return_value={"action": "plugin", "status": "skipped", "detail": "SKIP(codex): fixture"}).start()
        mock.patch.object(codex_driver.manifest, "record", return_value={"runtime": "codex"}).start()
        # This test isolates the launcher-passthrough behavior only; it must
        # never touch a real runtime home's `.harness/native-agents/`.
        mock.patch.object(
            codex_driver.native_agent_payload,
            "plan_payload",
            return_value=mock.sentinel.payload_plan,
        ).start()
        mock.patch.object(
            codex_driver.native_agent_payload,
            "materialize_payload",
            return_value={"action": "materialize_native_agent_payload", "status": "unchanged"},
        ).start()

    def _managed_action(self, actions):
        matches = [a for a in actions if a.get("action") == "managed-launcher"]
        self.assertEqual(len(matches), 1, actions)
        return matches[0]

    def test_protected_created_result_passes_through_verbatim(self) -> None:
        expected = {
            "action": "managed-launcher",
            "status": "created",
            "target": "/fixture/.harness/bin/codex",
            "real_command": "/fixture/.local/bin/codex",
            "protected": True,
            "mode": "protected-path-v1",
        }
        with mock.patch.object(codex_launcher, "install", return_value=dict(expected)) as install_mock:
            result = codex_driver.install(plugin=True, dry_run=False)
        install_mock.assert_called_once_with(dry_run=False)
        self.assertEqual(self._managed_action(result["actions"]), expected)
        self.assertFalse(result["blocked"])

    def test_unchanged_result_passes_through_verbatim(self) -> None:
        expected = {
            "action": "managed-launcher",
            "status": "unchanged",
            "target": "/fixture/.harness/bin/codex",
            "real_command": "/fixture/.local/bin/codex",
            "protected": True,
            "mode": "protected-path-v1",
        }
        with mock.patch.object(codex_launcher, "install", return_value=dict(expected)):
            result = codex_driver.install(plugin=True, dry_run=False)
        self.assertEqual(self._managed_action(result["actions"]), expected)
        self.assertFalse(result["blocked"])

    def test_unavailable_command_maps_to_skipped_unavailable(self) -> None:
        with mock.patch.object(
            codex_launcher, "install", side_effect=codex_launcher.CodexUnavailableError("no codex on PATH")
        ):
            result = codex_driver.install(plugin=True, dry_run=False)
        action = self._managed_action(result["actions"])
        self.assertEqual(action["status"], "skipped-unavailable")
        self.assertIn("no codex on PATH", action["detail"])
        self.assertFalse(result["blocked"])

    def test_launcher_error_maps_to_blocked_without_stopping_other_actions(self) -> None:
        with mock.patch.object(
            codex_launcher, "install", side_effect=codex_launcher.CodexLauncherError("foreign file collision")
        ):
            result = codex_driver.install(plugin=True, dry_run=False)
        action = self._managed_action(result["actions"])
        self.assertEqual(action["status"], "blocked")
        self.assertIn("foreign file collision", action["detail"])
        self.assertTrue(result["blocked"])

    def test_dry_run_forwards_dry_run_to_the_launcher(self) -> None:
        with mock.patch.object(
            codex_launcher, "install", return_value={"action": "managed-launcher", "status": "planned"}
        ) as install_mock:
            codex_driver.install(plugin=True, dry_run=True)
        install_mock.assert_called_once_with(dry_run=True)

    def test_plugin_false_never_calls_the_launcher(self) -> None:
        with mock.patch.object(codex_launcher, "install") as install_mock:
            result = codex_driver.install(plugin=False, dry_run=False)
        install_mock.assert_not_called()
        self.assertFalse(any(a.get("action") == "managed-launcher" for a in result["actions"]))


class CodexDriverNativeAgentPayloadTest(_IsolatedRuntimeHomeTestCase):
    """O1 (Astra guide alignment): install/dry-run/checks against the payload.

    Every path here is an isolated fixture -- ``AGENT_HOME`` and ``CODEX_HOME``
    are both temp dirs (see ``_IsolatedRuntimeHomeTestCase``) -- so this never
    touches a real runtime home.
    """

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(codex_driver.projector, "plan", side_effect=_no_plan_entries).start()
        mock.patch.object(codex_driver, "_plugin_action").start()
        mock.patch.object(codex_driver.manifest, "record", return_value={"runtime": "codex"}).start()

        config = self.agent_home / "adapters" / "codex" / "config" / "models.conf"
        config.parent.mkdir(parents=True)
        config.write_text(
            "CFG_PROFILE_DEFAULT=deep:high:workspace-write\nCFG_TIER_DEEP_MODEL=fixture-model\n",
            encoding="utf-8",
        )

    def test_install_materializes_payload_before_reporting(self) -> None:
        result = codex_driver.install(plugin=False, dry_run=False)
        payload_actions = [
            a for a in result["actions"] if a.get("action") == "materialize_native_agent_payload"
        ]
        self.assertEqual(len(payload_actions), 1, result["actions"])
        self.assertEqual(payload_actions[0]["status"], "created")
        self.assertIs(result["actions"][0], payload_actions[0])

        rendered = self.codex_home / ".harness" / "native-agents"
        digests = list(rendered.iterdir())
        self.assertEqual(len(digests), 1)
        self.assertTrue((digests[0] / "memory-scout.toml").is_file())
        self.assertIn("fixture-model", (digests[0] / "memory-scout.toml").read_text())

    def test_dry_run_never_writes(self) -> None:
        result = codex_driver.install(plugin=False, dry_run=True)
        payload_actions = [
            a for a in result["actions"] if a.get("action") == "materialize_native_agent_payload"
        ]
        self.assertEqual(payload_actions[0]["status"], "planned")
        self.assertFalse((self.codex_home / ".harness").exists())

    def test_checks_reports_unmaterialized_then_materialized(self) -> None:
        checks = codex_driver.checks()
        payload_check = next(c for c in checks if getattr(c, "__name__", "") == "_native_agent_payload_check")
        self.assertFalse(payload_check()["ok"])

        codex_driver.install(plugin=False, dry_run=False)
        self.assertTrue(payload_check()["ok"])
        # checks() never creates or repairs anything itself.
        self.assertFalse((self.codex_home / "agents").exists())


if __name__ == "__main__":
    unittest.main()
