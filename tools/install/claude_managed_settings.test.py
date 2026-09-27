"""D-79 retirement of MEM_DISTILL_ENABLE and the autoMemoryEnabled managed field.

Exercises `_merge_claude_settings`/`_claude_managed_values`/`_claude_settings_health`
in isolation: a prior release's managed env key must be dropped only when the
user never changed it away, and `autoMemoryEnabled` merges like `statusLine`
(apply when absent, keep-and-report when the user set a conflicting value).
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import runtime_activation  # noqa: E402

HOOKS = {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}
STATUS_LINE = {
    "type": "command",
    "command": runtime_activation.CLAUDE_STATUSLINE_COMMAND,
    "refreshInterval": 60,
}


class ClaudeManagedSettingsTests(unittest.TestCase):
    def _fixture(self, tmp, *, auto_memory=False):
        active_root = Path(tmp) / "source"
        source_settings = active_root / "adapters" / "claude" / "settings.json"
        source_settings.parent.mkdir(parents=True)
        source_settings.write_text(
            json.dumps(
                {
                    "autoMemoryEnabled": auto_memory,
                    "hooks": HOOKS,
                    "statusLine": STATUS_LINE,
                }
            ),
            encoding="utf-8",
        )
        claude_home = Path(tmp) / "claude-home"
        claude_home.mkdir()
        return active_root, claude_home

    def _write_user_settings(self, claude_home, data):
        (claude_home / "settings.json").write_text(json.dumps(data), encoding="utf-8")

    def _read_user_settings(self, claude_home):
        return json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))

    def test_retired_env_key_is_dropped_when_user_never_changed_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp)
            self._write_user_settings(
                claude_home,
                {
                    "hooks": {},
                    "statusLine": STATUS_LINE,
                    "env": {"MEM_DISTILL_ENABLE": "1", "USER_FLAG": "keep"},
                },
            )
            previous = {
                "managed_config": {
                    "claude_hooks": {},
                    "claude_values": {
                        "statusLine": STATUS_LINE,
                        "env": {"MEM_DISTILL_ENABLE": "1"},
                    },
                }
            }
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, previous)
            self.assertEqual(result["conflicts"], [])
            settings = self._read_user_settings(claude_home)
            self.assertNotIn("MEM_DISTILL_ENABLE", settings["env"])
            self.assertEqual(settings["env"]["USER_FLAG"], "keep")

    def test_retired_env_key_is_kept_and_reported_when_user_changed_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp)
            self._write_user_settings(
                claude_home,
                {
                    "hooks": {},
                    "statusLine": STATUS_LINE,
                    "env": {"MEM_DISTILL_ENABLE": "0", "USER_FLAG": "keep"},
                },
            )
            previous = {
                "managed_config": {
                    "claude_hooks": {},
                    "claude_values": {
                        "statusLine": STATUS_LINE,
                        "env": {"MEM_DISTILL_ENABLE": "1"},
                    },
                }
            }
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, previous)
            self.assertIn("env.MEM_DISTILL_ENABLE", result["conflicts"])
            settings = self._read_user_settings(claude_home)
            self.assertEqual(settings["env"]["MEM_DISTILL_ENABLE"], "0")
            self.assertEqual(settings["env"]["USER_FLAG"], "keep")

    def test_no_env_object_is_forced_when_nothing_is_managed_or_retired(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp)
            self._write_user_settings(
                claude_home, {"hooks": {}, "statusLine": STATUS_LINE}
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, None)
            self.assertEqual(result["conflicts"], [])
            settings = self._read_user_settings(claude_home)
            self.assertNotIn("env", settings)

    def test_auto_memory_enabled_applies_default_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp, auto_memory=False)
            self._write_user_settings(
                claude_home, {"hooks": {}, "statusLine": STATUS_LINE}
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, None)
            self.assertEqual(result["conflicts"], [])
            settings = self._read_user_settings(claude_home)
            self.assertIs(settings["autoMemoryEnabled"], False)

    def test_auto_memory_enabled_conflict_keeps_user_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp, auto_memory=False)
            self._write_user_settings(
                claude_home,
                {"hooks": {}, "statusLine": STATUS_LINE, "autoMemoryEnabled": True},
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, None)
            self.assertIn("autoMemoryEnabled", result["conflicts"])
            settings = self._read_user_settings(claude_home)
            self.assertIs(settings["autoMemoryEnabled"], True)

    def test_auto_memory_enabled_reapplies_after_user_reverts_to_previous_managed_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp, auto_memory=False)
            self._write_user_settings(
                claude_home,
                {"hooks": {}, "statusLine": STATUS_LINE, "autoMemoryEnabled": False},
            )
            previous = {
                "managed_config": {
                    "claude_hooks": {},
                    "claude_values": {"statusLine": STATUS_LINE, "autoMemoryEnabled": False},
                }
            }
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                result = runtime_activation._merge_claude_settings(active_root, previous)
            self.assertEqual(result["conflicts"], [])
            settings = self._read_user_settings(claude_home)
            self.assertIs(settings["autoMemoryEnabled"], False)

    def test_health_reports_missing_and_conflicting_auto_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp, auto_memory=False)
            self._write_user_settings(
                claude_home,
                {"hooks": HOOKS, "statusLine": STATUS_LINE},
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                missing, conflicts = runtime_activation._claude_settings_health(active_root)
            self.assertTrue(missing)
            self.assertEqual(conflicts, [])

            self._write_user_settings(
                claude_home,
                {"hooks": HOOKS, "statusLine": STATUS_LINE, "autoMemoryEnabled": True},
            )
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                missing, conflicts = runtime_activation._claude_settings_health(active_root)
            self.assertIn("autoMemoryEnabled", conflicts)

    def test_health_ignores_env_when_nothing_is_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_root, claude_home = self._fixture(tmp, auto_memory=False)
            self._write_user_settings(
                claude_home,
                {
                    "hooks": HOOKS,
                    "statusLine": STATUS_LINE,
                    "autoMemoryEnabled": False,
                    "env": {"USER_FLAG": "keep"},
                },
            )
            statusline = claude_home / "statusline.sh"
            statusline.write_text("#!/bin/sh\n", encoding="utf-8")
            statusline.chmod(0o755)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_home)}):
                missing, conflicts = runtime_activation._claude_settings_health(active_root)
            self.assertFalse(missing)
            self.assertEqual(conflicts, [])


if __name__ == "__main__":
    unittest.main()
