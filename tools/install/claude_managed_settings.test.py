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


class RetiredHookMigrationTests(unittest.TestCase):
    def test_upgrade_prunes_every_retired_hook_and_preserves_user_settings(self):
        import claude_settings_config as settings
        from drivers import claude, codex
        for runtime, driver, filename in (("claude", claude, "settings.json"), ("codex", codex, "hooks.json")):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                if runtime == "claude":
                    commands = [f'sh "$HOME/.claude/hooks/{name}"' for name in settings._RETIRED_HOOKS]
                    commands.append('"$HOME/.claude/hooks/worker-state-compact.py" guard-write')
                else:
                    commands = ['sh "$root/adapters/codex/hooks/run-hook.sh" pretooluse-write-guard.py']
                user = {"type": "command", "command": "sh /user/hooks/git-state-guard.sh"}
                observer = {"type": "command", "command": 'sh "$HOME/.claude/hooks/spec-read-marker.sh"'}
                data = {"permissions": {"defaultMode": "user-choice"}, "custom": [1, 2], "hooks": {
                    "PreToolUse": [{"matcher": "*", "timeout": 99, "hooks": [
                        *({"type": "command", "command": c} for c in commands), user]}],
                    "PostToolUse": [{"hooks": [observer]}]}}
                path = home / filename
                path.write_text(json.dumps(data))
                entries = [] if runtime == "claude" else [{"action": "symlink", "source": str(home / "source-hooks.json"), "dest": str(path)}]
                with mock.patch.object(driver.paths, "runtime_home", return_value=home), mock.patch.object(driver.projector, "plan", return_value={runtime: entries}):
                    before = path.read_bytes()
                    driver.install(dry_run=True)
                    self.assertEqual(path.read_bytes(), before)
                    result = driver.install()
                    self.assertFalse(result["blocked"], result)
                    updated = json.loads(path.read_text())
                    self.assertEqual(updated["hooks"]["PreToolUse"][0]["hooks"], [user])
                    self.assertEqual(updated["hooks"]["PostToolUse"], data["hooks"]["PostToolUse"])
                    self.assertEqual(updated["permissions"], data["permissions"])
                    self.assertEqual(updated["custom"], data["custom"])
                    once = path.read_bytes()
                    driver.install()
                    self.assertEqual(path.read_bytes(), once)

    def test_activation_without_old_manifest_removes_retired_hook(self):
        helper = ClaudeManagedSettingsTests()
        with tempfile.TemporaryDirectory() as tmp:
            source, home = helper._fixture(tmp)
            user_hook = {"type": "command", "command": "echo user"}
            helper._write_user_settings(home, {"hooks": {"PreToolUse": [{"hooks": [
                {"type": "command", "command": 'sh "$HOME/.claude/hooks/artifact-guard.sh"'}, user_hook]}]}})
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
                runtime_activation._merge_claude_settings(source, None)
            self.assertEqual(helper._read_user_settings(home)["hooks"]["PreToolUse"][0]["hooks"], [user_hook])

OLD_MEM_SYNC = (
    "sh -c 'if [ \"${AGENT_SESSION_ROLE:-}\" = worker ] || [ \"${AGENT_DISPATCH_CHILD:-}\" = 1 ] "
    "|| [ -n \"${AGENT_DISPATCH_DEPTH:-}\" ] || [ -n \"${OPENCODE_DISPATCH_SLUG:-}\" ] "
    "|| [ \"${FLEET_TITLE_REFRESH:-}\" = 1 ] || [ \"${MEM_DISTILL:-}\" = 1 ]; then exit 0; fi; "
    "exec python3 \"$HOME/.claude/tools/memory/mem.py\" sync --json >/dev/null'"
)
FLEET_END = 'python3 "$HOME/.claude/hooks/fleet-interaction-state.py" clear'
HERDR_END = 'bash "$HOME/.claude/hooks/herdr-agent-state.sh" release'


class SessionEndMemorySyncRetirementTests(unittest.TestCase):
    """D-82: the managed SessionEnd `mem.py sync` leaves installed settings, nothing else does."""

    @staticmethod
    def _group(command, timeout=10):
        return {"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": timeout}]}

    def _installed(self, extra=()):
        return {"hooks": {"SessionEnd": [
            self._group(FLEET_END), self._group(HERDR_END), self._group(OLD_MEM_SYNC, 120), *extra]}}

    def _commands(self, data):
        return [h["command"] for g in data["hooks"]["SessionEnd"] for h in g["hooks"]]

    def _source(self, tmp):
        helper = ClaudeManagedSettingsTests()
        active_root, claude_home = helper._fixture(tmp)
        settings_path = active_root / "adapters" / "claude" / "settings.json"
        source = json.loads(settings_path.read_text(encoding="utf-8"))
        source["hooks"] = {"SessionEnd": [self._group(FLEET_END), self._group(HERDR_END)]}
        settings_path.write_text(json.dumps(source), encoding="utf-8")
        return helper, active_root, claude_home

    def test_install_with_an_old_manifest_retires_only_the_managed_sync(self):
        user_end = {"matcher": "*", "hooks": [{"type": "command", "command": "echo user-end"}]}
        user_sync = self._group('python3 "$HOME/bin/my-own-mem.py" sync')
        with tempfile.TemporaryDirectory() as tmp:
            helper, source, home = self._source(tmp)
            helper._write_user_settings(home, self._installed([user_end, user_sync]))
            previous = {"managed_config": {"claude_hooks": {"SessionEnd": [
                self._group(FLEET_END), self._group(HERDR_END), self._group(OLD_MEM_SYNC, 120)]}}}
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
                runtime_activation._merge_claude_settings(source, previous)
            self.assertCountEqual(
                self._commands(helper._read_user_settings(home)),
                [FLEET_END, HERDR_END, "echo user-end", 'python3 "$HOME/bin/my-own-mem.py" sync'],
            )

    def test_install_without_a_manifest_retires_only_the_managed_sync(self):
        user_end = {"matcher": "*", "hooks": [{"type": "command", "command": "echo user-end"}]}
        # A user's own hook that merely looks alike: other path, no worker guard, other verb.
        lookalikes = [
            self._group('python3 "$HOME/.claude/tools/memory/mem.py" sync --json'),
            self._group("sh -c 'exec python3 \"$HOME/.claude/tools/memory/mem.py\" sync --json'"),
            self._group(OLD_MEM_SYNC.replace("sync --json", "inject --hook")),
            self._group(OLD_MEM_SYNC.replace("$HOME/.claude/tools", "$HOME/mine")),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            helper, source, home = self._source(tmp)
            helper._write_user_settings(home, self._installed([user_end, *lookalikes]))
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
                runtime_activation._merge_claude_settings(source, None)
            got = self._commands(helper._read_user_settings(home))
            self.assertNotIn(OLD_MEM_SYNC, got)
            self.assertEqual(got.count(FLEET_END), 1)
            self.assertEqual(got.count(HERDR_END), 1)
            self.assertEqual(len(got), 2 + 1 + len(lookalikes), got)
            self.assertIn("echo user-end", got)
            for group in lookalikes:
                self.assertIn(group["hooks"][0]["command"], got)

    def test_older_spellings_of_the_managed_sync_are_retired_too(self):
        older = OLD_MEM_SYNC.replace(" >/dev/null", "").replace("exec python3", "exec env MEM_DUMP_PUSH=1 python3")
        data = {"hooks": {"SessionEnd": [self._group(older)]}}
        import claude_settings_config as settings
        self.assertTrue(settings.remove_retired_hooks(data))
        self.assertNotIn("SessionEnd", data["hooks"])

    def test_the_sync_command_is_only_retired_under_session_end(self):
        data = {"hooks": {"Stop": [self._group(OLD_MEM_SYNC)]}}
        import claude_settings_config as settings
        self.assertFalse(settings.remove_retired_hooks(data))
        self.assertEqual(self._commands({"hooks": {"SessionEnd": data["hooks"]["Stop"]}}), [OLD_MEM_SYNC])

    def test_the_shipped_template_has_no_session_end_memory_hook_but_keeps_fleet_and_herdr(self):
        template = json.loads((HERE.parents[1] / "adapters/claude/settings.json").read_text(encoding="utf-8"))
        commands = self._commands(template)
        self.assertEqual(commands, [FLEET_END, HERDR_END])
        self.assertFalse([c for c in commands if "mem.py" in c])


if __name__ == "__main__":
    unittest.main()
