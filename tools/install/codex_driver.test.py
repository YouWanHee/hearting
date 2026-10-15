#!/usr/bin/env python3
"""The Codex driver no longer installs the interactive managed launcher."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from drivers import codex as codex_driver  # noqa: E402


def _no_plan_entries(_runtimes, scope="global"):
    return {"codex": []}


class CodexDriverLauncherRetirementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(mock.patch.stopall)
        self.temp = tempfile.TemporaryDirectory(prefix="codex-driver-config-")
        self.addCleanup(self.temp.cleanup)
        self.runtime_home = Path(self.temp.name) / "codex"
        self.runtime_home.mkdir()
        mock.patch.object(codex_driver.paths, "runtime_home",
                          return_value=self.runtime_home).start()
        mock.patch.object(codex_driver.projector, "plan", side_effect=_no_plan_entries).start()
        mock.patch.object(codex_driver, "_plugin_action", return_value={"action": "plugin", "status": "skipped", "detail": "fixture"}).start()
        mock.patch.object(codex_driver.manifest, "record", return_value={"runtime": "codex"}).start()

    def test_every_install_retires_owned_launcher_even_without_plugin(self):
        for plugin in (False, True):
            for dry_run in (False, True):
                with self.subTest(plugin=plugin, dry_run=dry_run), \
                     mock.patch.object(codex_driver.codex_launcher, "uninstall",
                         return_value={"action": "managed-launcher", "status": "restored"}) as retire:
                    result = codex_driver.install(plugin=plugin, dry_run=dry_run)
                    retire.assert_called_once_with(
                        codex_home=codex_driver.paths.runtime_home("codex", "global"), dry_run=dry_run)
                    self.assertFalse(result["blocked"])

    def test_foreign_launcher_conflict_is_reported(self):
        with mock.patch.object(codex_driver.codex_launcher, "uninstall",
                side_effect=codex_driver.codex_launcher.CodexLauncherError("modified successor")):
            result = codex_driver.install(plugin=False, dry_run=False)
        self.assertTrue(result["blocked"])
        launcher = next(action for action in result["actions"] if action.get("action") == "managed-launcher")
        self.assertIn("modified successor", launcher["detail"])

    def test_install_and_dry_run_share_the_statusline_merge(self):
        config = self.runtime_home / "config.toml"
        original = '[tui]\nstatus_line = ["thread-id", "custom", "thread-id"]\nstatus_line_use_colors = false\n'
        config.write_text(original, encoding="utf-8")
        planned = codex_driver.install(dry_run=True)
        self.assertEqual(config.read_text(encoding="utf-8"), original)
        self.assertFalse((self.runtime_home / "config.toml.pre-harness-tui").exists())
        action = next(item for item in planned["actions"] if item["action"] == "codex-tui-statusline")
        self.assertEqual(action["status"], "planned")

        applied = codex_driver.install(dry_run=False)
        import tomllib
        merged = tomllib.loads(config.read_text(encoding="utf-8"))["tui"]
        self.assertEqual(merged["status_line"], ["custom", "thread-id"])
        self.assertIs(merged["status_line_use_colors"], False)
        self.assertEqual((self.runtime_home / "config.toml.pre-harness-tui").read_text(), original)
        action = next(item for item in applied["actions"] if item["action"] == "codex-tui-statusline")
        self.assertEqual(action["status"], "updated")

    def test_invalid_statusline_is_skipped_without_blocking_install(self):
        config = self.runtime_home / "config.toml"
        original = '[tui]\nstatus_line = 7\n'
        config.write_text(original, encoding="utf-8")
        result = codex_driver.install(dry_run=False)
        self.assertFalse(result["blocked"])
        self.assertEqual(config.read_text(encoding="utf-8"), original)
        action = next(item for item in result["actions"] if item["action"] == "codex-tui-statusline")
        self.assertEqual(action["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
