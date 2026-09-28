#!/usr/bin/env python3
"""The Codex driver no longer installs the interactive managed launcher."""
from __future__ import annotations

import sys
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
        self.assertIn("modified successor", result["actions"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
