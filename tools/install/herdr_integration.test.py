"""Pinned Herdr asset, user-config migration, and native registration fixtures."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import herdr_integration as herdr  # noqa: E402
import fixture_env  # noqa: E402
import projector  # noqa: E402


class PinnedSourceTest(unittest.TestCase):
    def test_three_runtime_sources_have_the_expected_marker_and_entrypoint(self):
        for runtime in herdr.VERSIONS:
            with self.subTest(runtime=runtime):
                checks = herdr.source_checks(runtime, ROOT)
                self.assertTrue(checks)
                self.assertFalse([item for item in checks if not item["ok"]], checks)

    def test_claude_reporter_has_one_copy_managed_destination(self):
        with tempfile.TemporaryDirectory(prefix="herdr-claude-plan-") as temporary:
            home = Path(temporary) / "claude-home"
            with mock.patch.object(projector.paths, "runtime_home", return_value=home):
                entries = projector.plan(["claude"], scope="global")["claude"]
        dest = home / "hooks/herdr-agent-state.sh"
        matching = [entry for entry in entries if Path(entry.get("dest", "")) == dest]
        self.assertEqual(len(matching), 1, matching)
        self.assertEqual(matching[0]["action"], "copy_managed")
        self.assertEqual(matching[0]["integration_id"], "claude")

    def test_codex_uses_the_herdr_home_asset_for_callback_and_manifest(self):
        self.assertEqual(
            herdr.CODEX_SESSION_COMMAND,
            'bash "${CODEX_HOME:-$HOME/.codex}/herdr-agent-state.sh" session',
        )
        codex_asset = [item for item in projector._CODEX_TABLE
                       if item.get("action") == "copy_managed"
                       and item.get("integration_id") == "codex"]
        self.assertEqual(len(codex_asset), 1)
        self.assertEqual(codex_asset[0]["dest_name"], "herdr-agent-state.sh")
        self.assertNotIn(("herdr-agent-state.sh", "adapters/codex/hooks/herdr-agent-state.sh"),
                         projector._CODEX_FIXED_SYMLINKS)
        with tempfile.TemporaryDirectory(prefix="herdr-codex-manifest-") as temporary:
            root = Path(temporary)
            source = root / "source/adapters/codex/hooks/herdr-agent-state.sh"
            dest = root / "codex-home/herdr-agent-state.sh"
            source.parent.mkdir(parents=True)
            dest.parent.mkdir(parents=True)
            source.write_text("source\n", encoding="utf-8")
            dest.write_text("installed\n", encoding="utf-8")
            receipt = herdr.manifest_assets("codex", root / "source", root / "codex-home")
        self.assertEqual([item["path"] for item in receipt["assets"]],
                         ["herdr-agent-state.sh"])

    def test_configured_codex_session_command_parses_and_runs_in_fake_codex_home(self):
        hooks = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text(encoding="utf-8"))
        commands = [item["command"] for group in hooks["hooks"]["SessionStart"]
                    for item in group.get("hooks", [])]
        self.assertIn(herdr.CODEX_SESSION_COMMAND, commands)
        with tempfile.TemporaryDirectory(prefix="codex session command ") as temporary:
            home = Path(temporary) / "codex home"
            home.mkdir()
            marker = Path(temporary) / "invoked.txt"
            helper = home / "herdr-agent-state.sh"
            helper.write_text('#!/bin/sh\nprintf "%s\\n" "$1" > "$CODEX_HOOK_MARKER"\n',
                              encoding="utf-8")
            helper.chmod(0o700)
            env = {**os.environ, "HOME": str(Path(temporary) / "home"),
                   "CODEX_HOME": str(home), "CODEX_HOOK_MARKER": str(marker)}
            env.pop("HERDR_SOCKET_PATH", None)
            env.pop("HERDR_PANE_ID", None)
            syntax = subprocess.run(["bash", "-n", "-c", herdr.CODEX_SESSION_COMMAND],
                                    text=True, capture_output=True, env=env, check=False)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            result = subprocess.run(["bash", "-c", herdr.CODEX_SESSION_COMMAND],
                                    text=True, capture_output=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "session\n")

    def test_hearting_update_adopts_only_the_matching_herdr_owned_file(self):
        with tempfile.TemporaryDirectory(prefix="herdr-asset-adoption-") as temporary:
            root = Path(temporary)
            source = root / "release/adapters/codex/hooks/herdr-agent-state.sh"
            home = root / "codex-home"
            dest = home / "herdr-agent-state.sh"
            source.parent.mkdir(parents=True)
            home.mkdir()
            source.write_text("# installed by herdr\n# HERDR_INTEGRATION_ID=codex\n"
                              "# HERDR_INTEGRATION_VERSION=10\nnew\n", encoding="utf-8")
            dest.write_text("# installed by herdr\n# HERDR_INTEGRATION_ID=codex\n"
                            "# HERDR_INTEGRATION_VERSION=7\nold\n", encoding="utf-8")
            result = herdr.project_asset(source, dest, "codex", allowed_root=home)
            self.assertEqual(result["status"], "updated")
            self.assertFalse(dest.is_symlink())
            self.assertEqual(dest.read_bytes(), source.read_bytes())

            dest.write_text("user callback\n", encoding="utf-8")
            result = herdr.project_asset(source, dest, "codex", allowed_root=home)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(dest.read_text(encoding="utf-8"), "user callback\n")

    def test_user_hook_file_migrates_in_place_and_activation_receipt_unmerges_only_ours(self):
        with tempfile.TemporaryDirectory(prefix="herdr-claude-hooks-") as tmp:
            path = Path(tmp) / ".claude" / "settings.json"
            path.parent.mkdir()
            path.write_text(json.dumps({
                "env": {"USER_VALUE": "keep"},
                "statusLine": {"command": "user-status"},
                "hooks": {"UserPromptSubmit": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": herdr.CLAUDE_SESSION_COMMAND.replace(
                        " session", " working")},
                    {"type": "command", "command": "user-prompt"},
                ]}]},
            }), encoding="utf-8")
            with fixture_env.patched_environment(tmp, ROOT):
                result = herdr.migrate_hook_file(path, "claude", source_root=ROOT)
            self.assertEqual(result["status"], "updated")
            self.assertEqual(result["managed_config"], {
                "event": "SessionStart", "command": herdr.CLAUDE_SESSION_COMMAND})
            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["env"], {"USER_VALUE": "keep"})
            self.assertEqual(migrated["statusLine"], {"command": "user-status"})
            self.assertEqual(migrated["hooks"]["UserPromptSubmit"][0]["hooks"], [
                {"type": "command", "command": "user-prompt"}])
            with fixture_env.patched_environment(tmp, ROOT):
                removed = herdr.remove_managed_hook_file(path, result["managed_config"])
            self.assertTrue(removed)
            restored = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("SessionStart", restored["hooks"])
            self.assertEqual(restored["hooks"]["UserPromptSubmit"][0]["hooks"], [
                {"type": "command", "command": "user-prompt"}])

    def test_missing_optional_asset_removes_old_writer_without_registering_a_new_one(self):
        with tempfile.TemporaryDirectory(prefix="herdr-old-source-") as tmp:
            root = Path(tmp) / "old-source"
            path = Path(tmp) / ".claude" / "settings.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
                {"type": "command", "command":
                 'sh "$HOME/.claude/hooks/run-hook.sh" bash herdr-agent-state.sh working'},
            ]}]}}), encoding="utf-8")
            with fixture_env.patched_environment(tmp, ROOT):
                result = herdr.migrate_hook_file(path, "claude", source_root=root)
            self.assertEqual(result["status"], "updated")
            self.assertIsNone(result["managed_config"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"hooks": {}})

    def test_claude_and_codex_keep_one_session_reporter_and_user_hooks(self):
        cases = (
            ("claude", herdr.CLAUDE_SESSION_COMMAND),
            ("codex", herdr.CODEX_SESSION_COMMAND),
        )
        for runtime, expected in cases:
            if runtime == "claude":
                old_idle = 'sh "$HOME/.claude/hooks/run-hook.sh" bash herdr-agent-state.sh idle'
                old_working = 'sh "$HOME/.claude/hooks/run-hook.sh" bash herdr-agent-state.sh working'
                old_blocked = 'sh "$HOME/.claude/hooks/run-hook.sh" bash herdr-agent-state.sh blocked'
            else:
                old_idle = '"$root/adapters/codex/hooks/run-hook.sh" herdr-agent-state.sh idle'
                old_working = '"$root/adapters/codex/hooks/run-hook.sh" herdr-agent-state.sh working'
                old_blocked = '"$root/adapters/codex/hooks/run-hook.sh" herdr-agent-state.sh blocked'
            data = {
                "env": {"USER_KEY": "keep"},
                "hooks": {
                    "SessionStart": [{"matcher": "*", "hooks": [
                        {"type": "command", "command": "user-start"},
                        {"type": "command", "command": old_idle},
                    ]}],
                    "UserPromptSubmit": [{"matcher": "*", "hooks": [
                        {"type": "command", "command": old_working},
                        {"type": "command", "command": "/home/me/hooks/herdr-agent-state.sh working"},
                        {"type": "command", "command": "herdr-session-projection.py"},
                    ]}],
                    "PermissionRequest": [{"matcher": "*", "hooks": [
                        {"type": "command", "command": old_blocked},
                        {"type": "command", "command": "user-permission"},
                    ]}],
                },
            }
            with self.subTest(runtime=runtime):
                self.assertTrue(herdr.migrate_hook_data(data, runtime))
                self.assertFalse(herdr.migrate_hook_data(data, runtime))
                self.assertEqual(data["env"], {"USER_KEY": "keep"})
                commands = [(event, hook["command"])
                            for event, groups in data["hooks"].items()
                            for group in groups
                            for hook in group.get("hooks", [])]
                self.assertEqual([(event, command) for event, command in commands
                                  if herdr._owned_state_hook(command)],
                                 [("SessionStart", expected)])
                self.assertIn(("SessionStart", "user-start"), commands)
                self.assertIn(("UserPromptSubmit", "herdr-session-projection.py"), commands)
                self.assertIn(("UserPromptSubmit", "/home/me/hooks/herdr-agent-state.sh working"), commands)
                self.assertIn(("PermissionRequest", "user-permission"), commands)


class JsoncRegistrationTest(unittest.TestCase):
    def test_plugin_edit_preserves_comments_and_other_entries(self):
        text = '''{
  // keep this user comment
  "enabled": true,
  "plugin": [
    "./user-plugin.js", // keep the user plugin
  ],
}
'''
        merged = herdr.edit_plugin_list(text, add=(herdr.OPENCODE_TUI_V1,))
        data = herdr.parse_jsonc(merged)
        self.assertIn("keep this user comment", merged)
        self.assertIn("keep the user plugin", merged)
        self.assertEqual(data["plugin"], ["./user-plugin.js", herdr.OPENCODE_TUI_V1])
        restored = herdr.edit_plugin_list(merged, remove=(herdr.OPENCODE_TUI_V1,))
        self.assertEqual(herdr.parse_jsonc(restored)["plugin"], ["./user-plugin.js"])
        self.assertIn("keep this user comment", restored)

    def test_migration_merges_both_versions_and_deactivation_keeps_user_config(self):
        with tempfile.TemporaryDirectory(prefix="herdr-config-") as tmp:
            home = Path(tmp) / "opencode"
            home.mkdir()
            tui = home / "tui.jsonc"
            tui.write_text('''{
  // user's existing TUI choices
  "plugin": ["./user-tui.js",],
  "theme": "user-theme",
}
''', encoding="utf-8")
            with fixture_env.patched_environment(tmp, ROOT):
                result = herdr.migrate_opencode_configs(home, ROOT)
            self.assertTrue(all(item["status"] in {"updated", "unchanged"}
                                for item in result["actions"]), result["actions"])
            merged_tui = herdr.parse_jsonc(tui.read_text(encoding="utf-8"))
            self.assertEqual(merged_tui["theme"], "user-theme")
            self.assertIn("./user-tui.js", merged_tui["plugin"])
            self.assertIn(herdr.OPENCODE_TUI_V1, merged_tui["plugin"])
            self.assertIn(herdr.OPENCODE_IDENTITY, merged_tui["plugin"])
            self.assertIn("user's existing TUI choices", tui.read_text(encoding="utf-8"))
            cli = home / "cli.json"
            self.assertTrue(cli.is_file())
            cli_data = json.loads(cli.read_text(encoding="utf-8"))
            self.assertIn(herdr.OPENCODE_TUI_V2, cli_data["plugins"])
            self.assertIn(herdr.OPENCODE_IDENTITY, cli_data["plugins"])
            refreshed = herdr.migrate_opencode_configs(
                home, ROOT, previous=result["managed_config"])
            self.assertFalse([item for item in refreshed["actions"] if item["changed"]],
                             refreshed["actions"])
            self.assertEqual(refreshed["managed_config"], result["managed_config"])
            with fixture_env.patched_environment(tmp, ROOT):
                removed = herdr.remove_opencode_configs(home, refreshed["managed_config"])
            self.assertIn(str(tui), removed)
            self.assertIn(str(cli), removed)
            restored = herdr.parse_jsonc(tui.read_text(encoding="utf-8"))
            self.assertEqual(restored["plugin"], ["./user-tui.js"])
            self.assertEqual(restored["theme"], "user-theme")
            self.assertFalse(cli.exists())

    def test_migration_repairs_both_json_formats_without_removing_user_or_identity_plugins(self):
        with tempfile.TemporaryDirectory(prefix="herdr config spaces ") as tmp:
            root = Path(tmp)
            home = root / ".config" / "opencode"
            home.mkdir(parents=True)
            configs = {
                "tui.jsonc": ("plugin", ["./user-tui.js", "./tui/herdr-tui-session.js",
                                           "../../old release/adapters/opencode/tui/herdr-tui-session.js",
                                           "./tui/hearting-tui-identity.ts"]),
                "tui.json": ("plugin", [str(root / "old release/adapters/opencode/tui/herdr-tui-session.js"),
                                          "./user-json.js", "./tui/hearting-tui-identity.ts"]),
                "cli.jsonc": ("plugins", ["./user-cli.js", "./tui/herdr-opencode",
                                             "../../old release/adapters/opencode/tui/herdr-opencode/tui.js",
                                             "./tui/hearting-tui-identity.ts"]),
                "cli.json": ("plugins", [str(home / "tui/herdr-opencode/tui.js"),
                                           "./user-cli-json.js", "./tui/hearting-tui-identity.ts"]),
            }
            for name, (key, values) in configs.items():
                encoded = json.dumps(values)
                (home / name).write_text(
                    f'{{\n  // preserve {name} user options\n  "{key}": {encoded},\n  "theme": "keep",\n}}\n',
                    encoding="utf-8")

            result = herdr.migrate_opencode_configs(home, ROOT)
            self.assertTrue(all(item["status"] in {"updated", "unchanged"}
                                for item in result["actions"]), result["actions"])
            for name, (key, original) in configs.items():
                text = (home / name).read_text(encoding="utf-8")
                data = herdr.parse_jsonc(text)
                expected = herdr.OPENCODE_TUI_V1 if key == "plugin" else herdr.OPENCODE_TUI_V2
                self.assertEqual(data[key].count(expected), 1, (name, data[key]))
                self.assertIn(herdr.OPENCODE_IDENTITY, data[key], name)
                self.assertIn(next(value for value in original if value.startswith("./user")),
                              data[key], name)
                self.assertEqual(data["theme"], "keep", name)
                self.assertIn(f"preserve {name} user options", text)
                self.assertFalse(any(herdr._legacy_opencode_tui_ref(
                    value, "tui" if key == "plugin" else "cli", home, ROOT)
                    for value in data[key]), (name, data[key]))

            rerun = herdr.migrate_opencode_configs(home, ROOT)
            self.assertFalse([item for item in rerun["actions"] if item["changed"]], rerun["actions"])

    def test_symlinked_runtime_config_is_never_rewritten(self):
        with tempfile.TemporaryDirectory(prefix="herdr-symlink-") as tmp:
            home = Path(tmp) / "opencode"
            home.mkdir()
            target = Path(tmp) / "external-tui.json"
            original = '{"plugin": ["./user.js"]}\n'
            target.write_text(original, encoding="utf-8")
            (home / "tui.json").symlink_to(target)
            result = herdr.migrate_opencode_configs(home, ROOT)
            self.assertEqual((home / "tui.json").read_text(encoding="utf-8"), original)
            self.assertEqual(result["actions"][0]["status"], "source-managed")


class NativeSessionReporterFixtureTest(unittest.TestCase):
    def _run_hook(self, harness, payload, *, allowed, launch_argv, extra_env=None,
                  expect_report=True, source_asset=False):
        with tempfile.TemporaryDirectory(prefix="herdr session hook ") as tmp:
            root = Path(tmp)
            tools = root / "tools" / "fleet"
            tools.mkdir(parents=True)
            (tools / "__init__.py").write_text("", encoding="utf-8")
            (tools / "herdr_projection.py").write_text(
                "import os\n"
                "def may_report(harness, session_id, pane_id=None):\n"
                "    return (os.environ.get('REPORTER_FIXTURE_ALLOW') == '1' and "
                "os.environ.get('REPORTER_FIXTURE_SESSION') == session_id and "
                "pane_id == 'pane-fixture' and "
                "os.environ.get('AGENT_SESSION_ROLE') != 'worker' and "
                "os.environ.get('AGENT_DISPATCH_CHILD') != '1' and "
                "not os.environ.get('AGENT_DISPATCH_DEPTH'))\n",
                encoding="utf-8",
            )
            (tools / "process_identity.py").write_text(
                "import json, os\n"
                "def current_launch_context(harness, session_id):\n"
                "    if (os.environ.get('REPORTER_LAUNCH_HARNESS') != harness or "
                "os.environ.get('REPORTER_FIXTURE_SESSION') != session_id): return None\n"
                "    return {'argv': json.loads(os.environ['REPORTER_LAUNCH_ARGV'])}\n"
                "def is_native_resume_argv(argv, harness, session_id=None):\n"
                "    if (harness not in {'claude','codex','opencode'} or not isinstance(argv,list) "
                "or not argv or len(argv) > 64): return False\n"
                "    if any(not isinstance(arg,str) or not arg or '\\0' in arg for arg in argv): return False\n"
                "    if sum(len(arg.encode('utf-8','surrogatepass')) for arg in argv) > 8192: return False\n"
                "    executable = argv[0]\n"
                "    if not (executable == harness or os.path.isabs(executable) and "
                "os.path.basename(executable) == harness): return False\n"
                "    return session_id is None or isinstance(session_id,str) and session_id in argv\n"
                "def native_resume_wire_argv(argv, harness, session_id=None):\n"
                "    if not is_native_resume_argv(argv,harness,session_id): return None\n"
                "    return [harness, *argv[1:]]\n",
                encoding="utf-8",
            )
            if harness == "claude":
                source = ROOT / "hooks" / "herdr-agent-state.sh"
                script = source if source_asset else root / ".claude" / "hooks" / "herdr-agent-state.sh"
            else:
                source = ROOT / "adapters" / "codex" / "hooks" / "herdr-agent-state.sh"
                script = source if source_asset else root / "codex home" / "herdr-agent-state.sh"
            if not source_asset:
                script.parent.mkdir(parents=True)
                shutil.copyfile(source, script)

            socket_path = str(root / "herdr.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(socket_path)
            listener.listen(1)
            listener.settimeout(1)
            received = []

            def serve_once():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    return
                with connection:
                    with connection.makefile("rb") as stream:
                        line = stream.readline()
                    received.append(json.loads(line))
                    connection.sendall(b'{"result":{}}\n')

            server = None
            if allowed:
                server = threading.Thread(target=serve_once, daemon=True)
                server.start()
            env = {
                **fixture_env.build_environment(root, ROOT),
                "AGENT_HOME": str(root),
                "CODEX_HOME": str(root / "codex home"),
                "HERDR_ENV": "1",
                "HERDR_SOCKET_PATH": socket_path,
                "HERDR_PANE_ID": "pane-fixture",
                "REPORTER_FIXTURE_ALLOW": "1" if allowed else "0",
                "REPORTER_FIXTURE_SESSION": payload["session_id"],
                "REPORTER_LAUNCH_HARNESS": harness,
                "REPORTER_LAUNCH_ARGV": json.dumps(launch_argv),
            }
            for marker in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                           "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL"):
                env.pop(marker, None)
            if harness == "codex":
                env["CODEX_THREAD_ID"] = payload["session_id"]
            env.update(extra_env or {})
            result = subprocess.run(["sh", str(script), "session"], input=json.dumps(payload),
                                    text=True, capture_output=True, env=env, timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            if server is not None:
                server.join(2)
                self.assertFalse(server.is_alive(), "native hook did not connect to Herdr")
                self.assertEqual(bool(received), expect_report)
            else:
                with self.assertRaises(socket.timeout):
                    listener.accept()
            listener.close()
            return received[0] if received else None

    def test_native_hooks_forward_transcript_source_resume_argv_and_nanosecond_sequence(self):
        for harness, launch in (("claude", ["claude", "--resume", "ses-A"]),
                                ("codex", ["codex", "--resume", "ses-A"])):
            for source in ("startup", "resume", "clear", "compact", "fork"):
                with self.subTest(harness=harness, source=source):
                    request = self._run_hook(harness, {
                        "hook_event_name": "SessionStart", "session_id": "ses-A",
                        "transcript_path": "/fixture/transcript.jsonl", "source": source,
                    }, allowed=True, launch_argv=launch)
                    self.assertEqual(request["method"], "pane.report_agent_session")
                    params = request["params"]
                    self.assertEqual(params["agent"], harness)
                    self.assertEqual(params["agent_session_id"], "ses-A")
                    self.assertEqual(params["agent_session_path"], "/fixture/transcript.jsonl")
                    self.assertEqual(params["session_start_source"], source)
                    if source == "resume":
                        self.assertEqual(params["resume_argv"], launch)
                    else:
                        self.assertNotIn("resume_argv", params)
                    self.assertGreater(params["seq"], 10**18)

    def test_codex_source_and_runtime_copy_report_through_the_pinned_root_with_spaces(self):
        payload = {
            "hook_event_name": "SessionStart", "session_id": "ses-A",
            "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
        }
        launch = ["codex", "--resume", "ses-A"]
        for source_asset in (True, False):
            with self.subTest(source_asset=source_asset):
                request = self._run_hook("codex", payload, allowed=True, launch_argv=launch,
                                         source_asset=source_asset)
                self.assertEqual(request["method"], "pane.report_agent_session")
                self.assertEqual(request["params"]["agent_session_id"], "ses-A")

    def test_native_hooks_translate_proven_absolute_launcher_argv_for_wire(self):
        for harness in ("claude", "codex"):
            launch = [f"/home/user/.local/bin/{harness}", "--color", "always",
                      "--resume", "ses-A", "--model", "fixture", "--setting-sources", "user"]
            request = self._run_hook(harness, {
                "hook_event_name": "SessionStart", "session_id": "ses-A",
                "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
            }, allowed=True, launch_argv=launch)
            params = request["params"]
            self.assertEqual(params["resume_argv"], [harness, *launch[1:]])
            self.assertEqual(params["agent_session_id"], "ses-A")
            self.assertEqual(params["agent_session_path"], "/fixture/transcript.jsonl")
            self.assertEqual(params["session_start_source"], "resume")
            self.assertGreater(params["seq"], 10**18)

            for bad_argv in (["/usr/bin/env", harness, "--resume", "ses-A"],
                             ["/usr/bin/python3", f"/opt/{harness}", "--resume", "ses-A"],
                             [f"bin/{harness}", "--resume", "ses-A"],
                             [f"/opt/{harness}", "--resume", "other-session"],
                             [f"/opt/{harness}", "--resume", "ses-A", "\0bad"],
                             [f"/opt/{harness}", "--resume", "ses-A", "x" * 8192]):
                with self.subTest(harness=harness, argv=bad_argv[:3]):
                    request = self._run_hook(harness, {
                        "hook_event_name": "SessionStart", "session_id": "ses-A",
                        "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
                    }, allowed=True, launch_argv=bad_argv)
                    self.assertNotIn("resume_argv", request["params"])
                    self.assertEqual(request["params"]["agent_session_id"], "ses-A")
                    self.assertEqual(request["params"]["agent_session_path"], "/fixture/transcript.jsonl")
                    self.assertEqual(request["params"]["session_start_source"], "resume")

    def test_native_session_reporters_refuse_worker_and_mismatched_codex_identity(self):
        for harness, launch in (("claude", ["claude", "--resume", "ses-A"]),
                                ("codex", ["codex", "--resume", "ses-A"])):
            with self.subTest(harness=harness, case="worker"):
                self.assertIsNone(self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
                }, allowed=True, launch_argv=launch,
                    extra_env={"AGENT_SESSION_ROLE": "worker"}, expect_report=False))
        self.assertIsNone(self._run_hook("codex", {
            "hook_event_name": "SessionStart", "session_id": "ses-A",
            "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
        }, allowed=True, launch_argv=["codex", "--resume", "ses-A"],
            extra_env={"CODEX_THREAD_ID": "other-session"}, expect_report=False))

    def test_native_hooks_respect_report_guard_and_omit_unproven_resume_argv(self):
        for harness, launch in (("claude", ["claude", "--resume", "ses-A"]),
                                ("codex", ["codex", "--resume", "ses-A"])):
            with self.subTest(harness=harness, case="non-resume"):
                request = self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": "startup",
                }, allowed=True, launch_argv=launch)
                self.assertEqual(request["params"]["session_start_source"], "startup")
                self.assertNotIn("resume_argv", request["params"])
            with self.subTest(harness=harness, case="unsupported-source-shape"):
                request = self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": {"unknown": True},
                }, allowed=True, launch_argv=launch)
                self.assertNotIn("session_start_source", request["params"])
                self.assertNotIn("resume_argv", request["params"])
            with self.subTest(harness=harness, case="guard-refused"):
                request = self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
                }, allowed=False, launch_argv=launch)
                self.assertIsNone(request)

    def test_codex_display_projection_carries_native_lifecycle_to_reporter(self):
        path = ROOT / "adapters" / "codex" / "hooks" / "herdr_session_projection.py"
        module_spec = importlib.util.spec_from_file_location("herdr_session_projection_fixture", path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        with mock.patch.object(module, "_project", return_value=True) as project, \
                mock.patch.object(module.subprocess, "Popen"), \
                mock.patch.object(module.time, "time_ns", return_value=123456):
            module.project({"hook_event_name": "SessionStart", "source": "resume"}, "ses-A")
        project.assert_called_once_with("codex", "ses-A", worker=False, report_session=True,
                                        session_seq=123456, session_start_source="resume")


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the asset registration fixture")
class PythonToNodeRegistrationTest(unittest.TestCase):
    def test_native_assets_and_v1_v2_registrations_load_together(self):
        script = r'''import fs from "node:fs";
import { pathToFileURL } from "node:url";
const root = process.env.HEARTING_TEST_ROOT;
const assets = process.env.HEARTING_TEST_ASSET_ROOT;
const read = (name) => JSON.parse(fs.readFileSync(`${root}/${name}`, "utf8"));
const claude = read("adapters/claude/settings.json");
const codex = read("adapters/codex/hooks/hooks.json");
const v1 = read("adapters/opencode/tui/tui.json");
const v2 = read("adapters/opencode/tui/cli.json");
const heartingPublisher = fs.readFileSync(`${root}/adapters/opencode/plugins/hearting-guards.js`, "utf8");
const commands = (hooks) => Object.values(hooks).flat().flatMap((group) => group.hooks || [])
  .map((hook) => hook.command || "");
if (commands(claude.hooks).filter((value) => value.includes("herdr-agent-state.sh")).length !== 1) throw Error("Claude reporter registration");
if (commands(codex.hooks).filter((value) => value.includes("herdr-agent-state.sh")).length !== 1) throw Error("Codex reporter registration");
if (!v1.plugin.includes("./herdr-tui-session.js")) throw Error("V1 TUI registration");
if (!v2.plugins.includes("./herdr-opencode")) throw Error("V2 CLI registration");
if (!heartingPublisher.includes("--no-report-session") || heartingPublisher.includes("paneReportSequence")) throw Error("duplicate OpenCode session reporter");
const backend = await import(pathToFileURL(`${assets}/plugins/herdr-agent-state.js`));
const tuiV1 = await import(pathToFileURL(`${assets}/herdr-tui-session.js`));
const tuiV2 = await import(pathToFileURL(`${assets}/herdr-opencode/tui.js`));
if (backend.default.id !== "herdr.opencode") throw Error("OpenCode backend export");
if (tuiV1.default.id !== "herdr.opencode.session-selection") throw Error("V1 TUI export");
if (tuiV2.default.id !== tuiV1.default.id) throw Error("V2 TUI export");
console.log("Claude/Codex session hooks and OpenCode v1/v2 assets registered");
'''
        with tempfile.TemporaryDirectory(prefix="herdr-node-assets-") as temporary:
            asset_root = Path(temporary)
            (asset_root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
            for relative in (
                "adapters/opencode/plugins/herdr-agent-state.js",
                "adapters/opencode/tui/herdr-tui-session.js",
                "adapters/opencode/tui/herdr-opencode/tui.js",
            ):
                source_relative = relative.removeprefix("adapters/opencode/")
                target_relative = {
                    "plugins/herdr-agent-state.js": "plugins/herdr-agent-state.js",
                    "tui/herdr-tui-session.js": "herdr-tui-session.js",
                    "tui/herdr-opencode/tui.js": "herdr-opencode/tui.js",
                }[source_relative]
                target = asset_root / target_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                cwd=ROOT,
                env={**fixture_env.build_environment(asset_root, ROOT),
                     "HEARTING_TEST_ROOT": str(ROOT),
                     "HEARTING_TEST_ASSET_ROOT": str(asset_root)},
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("registered", result.stdout)

    def test_opencode_state_plugin_canonicalizes_only_proven_native_resume_argv(self):
        script = r'''import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { pathToFileURL } from "node:url";
const base = process.env.HEARTING_TEST_ASSET_ROOT;
const backend = await import(pathToFileURL(`${base}/plugins/herdr-agent-state.js`));
const sid = "ses-A";
const stat = fs.readFileSync("/proc/self/stat", "utf8");
const close = stat.lastIndexOf(") ");
const start = stat.slice(close + 2).trim().split(/\s+/)[19];
async function capture(index, identitySid, argv, identityStart = start) {
  const state = `${base}/state-${index}`;
  const identityDir = `${state}/hearting/tui-identity`;
  fs.mkdirSync(identityDir, {recursive: true});
  fs.writeFileSync(`${identityDir}/${process.pid}-${start}.json`, JSON.stringify({
    schema: "hearting-tui-selection-v1", sessionID: identitySid, pid: process.pid,
    start: identityStart, sessionSeq: 1700000000123, sessionStartSource: "resume",
  }));
  const socketPath = `${base}/plugin-${index}.sock`;
  process.env.HERDR_ENV = "1";
  process.env.HERDR_SOCKET_PATH = socketPath;
  process.env.HERDR_PANE_ID = "pane-fixture";
  process.env.XDG_STATE_HOME = state;
  process.argv = argv;
  let settle;
  const received = new Promise((resolve) => { settle = resolve; });
  const server = net.createServer((client) => {
    let buffer = "";
    client.on("data", (chunk) => {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(buffer.slice(0, newline));
      client.write("{\"result\":{}}\n");
      if (request.method === "pane.report_agent_session") settle(request);
    });
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
  const plugin = await backend.HerdrAgentStatePlugin();
  await plugin.event({event: {type: "session.updated", properties: {
    sessionID: sid, info: {id: sid, parentID: null},
  }}});
  let timeout;
  const request = await Promise.race([received, new Promise((_, reject) =>
    { timeout = setTimeout(() => reject(new Error("session report timeout")), 2500); })]);
  clearTimeout(timeout);
  await new Promise((resolve) => server.close(resolve));
  return {params: request.params, processArgv: process.argv.slice()};
}
const validArgv = ["/home/user/.local/bin/opencode", "/opt/opencode-cli.js", "run",
  "--color", "always", "--resume", sid, "--model", "fixture", "--setting-sources", "user"];
const valid = await capture(1, sid, validArgv);
const expectedWireArgv = ["opencode", ...validArgv.slice(1)];
if (JSON.stringify(valid.params.resume_argv) !== JSON.stringify(expectedWireArgv)) throw Error("native wire argv was not canonicalized");
if (JSON.stringify(valid.processArgv) !== JSON.stringify(validArgv)) throw Error("raw process argv was mutated");
if (valid.params.agent_session_id !== sid || valid.params.session_start_source !== "resume" || valid.params.seq !== 1700000000123) {
  throw Error("native session metadata changed");
}
const rejected = [
  [sid, ["/usr/bin/env", "opencode", "run", "--resume", sid]],
  [sid, ["/usr/bin/python3", "/opt/opencode", "run", "--resume", sid]],
  [sid, ["bin/opencode", "/opt/opencode-cli.js", "run", "--resume", sid]],
  [sid, ["/opt/opencode", "/opt/opencode-cli.js", "run", "--resume", "other-session"]],
  [sid, ["/opt/opencode", "/opt/opencode-cli.js", "run", "--resume", sid, "\0bad"]],
  [sid, ["/opt/opencode", "/opt/opencode-cli.js", "run", "--resume", sid, "x".repeat(8192)]],
  [sid, ["/opt/opencode", "/opt/opencode-cli.js", "run", ...Array(62).fill("x"), sid]],
];
for (let index = 0; index < rejected.length; index += 1) {
  const [identitySid, argv] = rejected[index];
  const params = await capture(index + 2, identitySid, argv);
  if ("resume_argv" in params.params) throw Error(`unproven launcher argv accepted at ${index}`);
  if (params.params.agent_session_id !== sid || params.params.session_start_source !== "resume"
      || params.params.seq !== 1700000000123) throw Error(`optional argv erased session identity at ${index}`);
}
const mismatched = await capture(20, "ses-other", validArgv);
if ("resume_argv" in mismatched.params || "seq" in mismatched.params || "session_start_source" in mismatched.params) {
  throw Error("current selection mismatch was accepted");
}
const changedBirth = await capture(21, sid, validArgv, "0");
if ("resume_argv" in changedBirth.params || "seq" in changedBirth.params || "session_start_source" in changedBirth.params) {
  throw Error("changed PID birth was accepted");
}
console.log("OpenCode state plugin canonicalized bounded proven native argv");
'''
        with tempfile.TemporaryDirectory(prefix="herdr-opencode-plugin-") as temporary:
            asset_root = Path(temporary)
            (asset_root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
            plugins = asset_root / "plugins"
            plugins.mkdir()
            shutil.copyfile(ROOT / "adapters/opencode/plugins/herdr-agent-state.js",
                            plugins / "herdr-agent-state.js")
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script], cwd=ROOT,
                env={**fixture_env.build_environment(asset_root, ROOT),
                     "HEARTING_TEST_ASSET_ROOT": str(asset_root)},
                text=True, capture_output=True, timeout=30, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("canonicalized bounded proven native argv", result.stdout)

    def test_opencode_tui_report_uses_pid_birth_bound_lifecycle_and_omits_unproven_argv(self):
        script = r'''import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { pathToFileURL } from "node:url";
const base = process.env.HEARTING_TEST_ASSET_ROOT;
const tuiV1 = await import(pathToFileURL(`${base}/herdr-tui-session.js`));
const tuiV2 = await import(pathToFileURL(`${base}/herdr-opencode/tui.js`));
const sid = "ses-A";
const stat = fs.readFileSync("/proc/self/stat", "utf8");
const close = stat.lastIndexOf(") ");
const start = stat.slice(close + 2).trim().split(/\s+/)[19];
const api = {
  ui: { router: { current: () => ({type: "session", sessionID: sid}) } },
  data: {
    session: {
      get: (id) => ({id, parentID: null}), family: () => [], status: () => "idle",
      permission: {list: () => []}, form: {list: () => []},
    },
    listen: () => () => {},
  },
};
async function capture(index, identitySid, argv, identityStart = start, entry = tuiV1) {
  const state = `${base}/state-${index}`;
  const identityDir = `${state}/hearting/tui-identity`;
  fs.mkdirSync(identityDir, {recursive: true});
  fs.writeFileSync(`${identityDir}/${process.pid}-${start}.json`, JSON.stringify({
    schema: "hearting-tui-selection-v1", sessionID: identitySid, pid: process.pid,
    start: identityStart, sessionSeq: 1700000000123000, sessionStartSource: "resume",
  }));
  const socketPath = `${base}/herdr-${index}.sock`;
  process.env.HERDR_ENV = "1";
  process.env.HERDR_SOCKET_PATH = socketPath;
  process.env.HERDR_PANE_ID = "pane-fixture";
  process.env.XDG_STATE_HOME = state;
  process.argv = argv;
  let settle;
  const received = new Promise((resolve) => { settle = resolve; });
  let reportCount = 0;
  const server = net.createServer((client) => {
    let buffer = "";
    client.on("data", (chunk) => {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      const request = JSON.parse(buffer.slice(0, newline));
      if (request.method === "pane.report_agent_session") reportCount += 1;
      client.write("{\"result\":{}}\n");
      if (request.method === "pane.report_agent_session") settle(request);
    });
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
  const dispose = entry.default.setup(api);
  let timeout;
  const request = await Promise.race([received, new Promise((_, reject) =>
    { timeout = setTimeout(() => reject(new Error("session report timeout")), 2500); })]);
  clearTimeout(timeout);
  await new Promise((resolve) => setTimeout(resolve, 25));
  dispose();
  await new Promise((resolve) => server.close(resolve));
  return {params: request.params, processArgv: process.argv.slice(), reportCount};
}
const rawArgv = ["/home/user/.local/bin/opencode", "--resume", sid, "--model", "fixture"];
const valid = await capture(1, sid, rawArgv);
const validV2 = await capture(22, sid, rawArgv, start, tuiV2);
if (valid.reportCount !== 1 || validV2.reportCount !== 1) throw Error("copied V1/V2 callback count");
if (validV2.params.agent_session_id !== sid || validV2.params.session_start_source !== "resume") {
  throw Error("copied V2 TUI callback missing");
}
const mismatched = await capture(2, "ses-other", rawArgv);
const changedBirth = await capture(3, sid, rawArgv, "0");
const invalidArgvs = [
  ["bin/opencode", "--resume", sid],
  ["/usr/bin/env", "opencode", "--resume", sid],
  ["/home/user/.local/bin/opencode", "--resume", "other-session"],
  ["/home/user/.local/bin/opencode", "--resume", sid, "\0bad"],
  ["/home/user/.local/bin/opencode", "--resume", sid, "x".repeat(8192)],
  ["/home/user/.local/bin/opencode", ...Array(64).fill("x"), sid],
];
const invalidResults = [];
for (let index = 0; index < invalidArgvs.length; index += 1) {
  invalidResults.push(await capture(index + 4, sid, invalidArgvs[index]));
}
if (valid.params.seq !== 1700000000123000 || valid.params.seq > Number.MAX_SAFE_INTEGER) throw Error("OpenCode seq compatibility");
if (valid.params.session_start_source !== "resume") throw Error("native OpenCode source missing");
if (JSON.stringify(valid.params.resume_argv) !== JSON.stringify(["opencode", ...rawArgv.slice(1)])) throw Error("native OpenCode wire argv missing");
if (JSON.stringify(valid.processArgv) !== JSON.stringify(rawArgv)) throw Error("TUI mutated its raw argv");
if (mismatched.params.session_start_source !== "select" || "seq" in mismatched.params || "resume_argv" in mismatched.params
    || "resume_argv" in changedBirth.params) {
  throw Error("unproven OpenCode identity fields were emitted");
}
for (const [index, item] of invalidResults.entries()) {
  if ("resume_argv" in item.params) throw Error(`invalid TUI argv accepted at ${index}`);
  if (item.params.agent_session_id !== sid || item.params.session_start_source !== "resume"
      || item.params.seq !== 1700000000123000) throw Error(`optional TUI argv erased session identity at ${index}`);
}
console.log(JSON.stringify({valid: valid.params, mismatched: mismatched.params,
  changedBirth: changedBirth.params, invalid: invalidResults.map(item => item.params)}));
'''
        with tempfile.TemporaryDirectory(prefix="Herdr OpenCode session ") as temporary:
            asset_root = Path(temporary)
            (asset_root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
            shutil.copyfile(ROOT / "adapters/opencode/tui/herdr-tui-session.js",
                            asset_root / "herdr-tui-session.js")
            v2_entry = asset_root / "herdr-opencode/tui.js"
            v2_entry.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "adapters/opencode/tui/herdr-opencode/tui.js", v2_entry)
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script], cwd=ROOT,
                env={**fixture_env.build_environment(asset_root, ROOT),
                     "HEARTING_TEST_ASSET_ROOT": str(asset_root)},
                text=True, capture_output=True, timeout=15, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn('"session_start_source":"resume"', result.stdout)


if __name__ == "__main__":
    unittest.main()
