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


class PinnedSourceTest(unittest.TestCase):
    def test_three_runtime_sources_have_the_expected_marker_and_entrypoint(self):
        for runtime in herdr.VERSIONS:
            with self.subTest(runtime=runtime):
                checks = herdr.source_checks(runtime, ROOT)
                self.assertTrue(checks)
                self.assertFalse([item for item in checks if not item["ok"]], checks)

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
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                result = herdr.migrate_hook_file(path, "claude", source_root=ROOT)
            self.assertEqual(result["status"], "updated")
            self.assertEqual(result["managed_config"], {
                "event": "SessionStart", "command": herdr.CLAUDE_SESSION_COMMAND})
            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["env"], {"USER_VALUE": "keep"})
            self.assertEqual(migrated["statusLine"], {"command": "user-status"})
            self.assertEqual(migrated["hooks"]["UserPromptSubmit"][0]["hooks"], [
                {"type": "command", "command": "user-prompt"}])
            with mock.patch.dict(os.environ, {"HOME": tmp}):
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
            with mock.patch.dict(os.environ, {"HOME": tmp}):
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
            with mock.patch.dict(os.environ, {"HOME": tmp}):
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
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                removed = herdr.remove_opencode_configs(home, result["managed_config"])
            self.assertIn(str(tui), removed)
            self.assertIn(str(cli), removed)
            restored = herdr.parse_jsonc(tui.read_text(encoding="utf-8"))
            self.assertEqual(restored["plugin"], ["./user-tui.js"])
            self.assertEqual(restored["theme"], "user-theme")
            self.assertFalse(cli.exists())

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
    def _run_hook(self, harness, payload, *, allowed, launch_argv):
        with tempfile.TemporaryDirectory(prefix="herdr-session-hook-") as tmp:
            root = Path(tmp)
            tools = root / "tools" / "fleet"
            tools.mkdir(parents=True)
            (tools / "__init__.py").write_text("", encoding="utf-8")
            (tools / "herdr_projection.py").write_text(
                "import os\n"
                "def may_report(harness, session_id, pane_id=None):\n"
                "    return (os.environ.get('REPORTER_FIXTURE_ALLOW') == '1' and "
                "os.environ.get('REPORTER_FIXTURE_SESSION') == session_id and "
                "pane_id == 'pane-fixture')\n",
                encoding="utf-8",
            )
            (tools / "process_identity.py").write_text(
                "import json, os\n"
                "def current_launch_context(harness, session_id):\n"
                "    if (os.environ.get('REPORTER_LAUNCH_HARNESS') != harness or "
                "os.environ.get('REPORTER_FIXTURE_SESSION') != session_id): return None\n"
                "    return {'argv': json.loads(os.environ['REPORTER_LAUNCH_ARGV'])}\n",
                encoding="utf-8",
            )
            if harness == "claude":
                source = ROOT / "hooks" / "herdr-agent-state.sh"
                script = root / "hooks" / "herdr-agent-state.sh"
            else:
                source = ROOT / "adapters" / "codex" / "hooks" / "herdr-agent-state.sh"
                script = root / "adapters" / "codex" / "hooks" / "herdr-agent-state.sh"
            script.parent.mkdir(parents=True)
            shutil.copyfile(source, script)

            socket_path = str(root / "herdr.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(socket_path)
            listener.listen(1)
            listener.settimeout(2)
            received = []

            def serve_once():
                connection, _ = listener.accept()
                with connection:
                    line = connection.makefile("rb").readline()
                    received.append(json.loads(line))
                    connection.sendall(b'{"result":{}}\n')

            server = None
            if allowed:
                server = threading.Thread(target=serve_once, daemon=True)
                server.start()
            env = {
                **os.environ,
                "AGENT_HOME": str(root),
                "HERDR_ENV": "1",
                "HERDR_SOCKET_PATH": socket_path,
                "HERDR_PANE_ID": "pane-fixture",
                "REPORTER_FIXTURE_ALLOW": "1" if allowed else "0",
                "REPORTER_FIXTURE_SESSION": payload["session_id"],
                "REPORTER_LAUNCH_HARNESS": harness,
                "REPORTER_LAUNCH_ARGV": json.dumps(launch_argv),
            }
            if harness == "codex":
                env["CODEX_THREAD_ID"] = payload["session_id"]
            result = subprocess.run(["sh", str(script), "session"], input=json.dumps(payload),
                                    text=True, capture_output=True, env=env, timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            if server is not None:
                server.join(2)
                self.assertFalse(server.is_alive(), "native hook did not connect to Herdr")
            else:
                with self.assertRaises(socket.timeout):
                    listener.accept()
            listener.close()
            return received[0] if received else None

    def test_native_hooks_forward_transcript_source_resume_argv_and_nanosecond_sequence(self):
        for harness, launch in (("claude", ["claude", "--resume", "ses-A"]),
                                ("codex", ["codex", "--resume", "ses-A"])):
            with self.subTest(harness=harness):
                request = self._run_hook(harness, {
                    "hook_event_name": "SessionStart", "session_id": "ses-A",
                    "transcript_path": "/fixture/transcript.jsonl", "source": "resume",
                }, allowed=True, launch_argv=launch)
                self.assertEqual(request["method"], "pane.report_agent_session")
                params = request["params"]
                self.assertEqual(params["agent"], harness)
                self.assertEqual(params["agent_session_id"], "ses-A")
                self.assertEqual(params["agent_session_path"], "/fixture/transcript.jsonl")
                self.assertEqual(params["session_start_source"], "resume")
                self.assertEqual(params["resume_argv"], launch)
                self.assertGreater(params["seq"], 10**18)

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

    def test_codex_display_projection_does_not_compute_inactive_session_lifecycle(self):
        path = ROOT / "adapters" / "codex" / "hooks" / "herdr_session_projection.py"
        module_spec = importlib.util.spec_from_file_location("herdr_session_projection_fixture", path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        with mock.patch.object(module, "_project", return_value=True) as project, \
                mock.patch.object(module.subprocess, "Popen"):
            module.project({"hook_event_name": "SessionStart", "source": "resume"}, "ses-A")
        project.assert_called_once_with("codex", "ses-A", worker=False, report_session=False)


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
if (!v1.plugin.includes("./tui/herdr-tui-session.js")) throw Error("V1 TUI registration");
if (!v2.plugins.includes("./tui/herdr-opencode")) throw Error("V2 CLI registration");
if (!heartingPublisher.includes("--no-report-session") || heartingPublisher.includes("paneReportSequence")) throw Error("duplicate OpenCode session reporter");
const backend = await import(pathToFileURL(`${assets}/plugins/herdr-agent-state.js`));
const tuiV1 = await import(pathToFileURL(`${assets}/tui/herdr-tui-session.js`));
const tuiV2 = await import(pathToFileURL(`${assets}/tui/herdr-opencode/tui.js`));
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
                target = asset_root / relative.removeprefix("adapters/opencode/")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                cwd=ROOT,
                env={**os.environ, "HEARTING_TEST_ROOT": str(ROOT),
                     "HEARTING_TEST_ASSET_ROOT": str(asset_root)},
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("registered", result.stdout)

    def test_opencode_tui_report_uses_pid_birth_bound_lifecycle_and_omits_unproven_argv(self):
        script = r'''import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { pathToFileURL } from "node:url";
const base = process.env.HEARTING_TEST_ASSET_ROOT;
const tui = await import(pathToFileURL(`${base}/tui/herdr-tui-session.js`));
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
async function capture(index, identitySid, argv) {
  const state = `${base}/state-${index}`;
  const identityDir = `${state}/hearting/tui-identity`;
  fs.mkdirSync(identityDir, {recursive: true});
  fs.writeFileSync(`${identityDir}/${process.pid}-${start}.json`, JSON.stringify({
    schema: "hearting-tui-selection-v1", sessionID: identitySid, pid: process.pid,
    start, sessionSeq: 1700000000123000, sessionStartSource: "resume",
  }));
  const socketPath = `${base}/herdr-${index}.sock`;
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
  const dispose = tui.default.setup(api);
  let timeout;
  const request = await Promise.race([received, new Promise((_, reject) =>
    { timeout = setTimeout(() => reject(new Error("session report timeout")), 2500); })]);
  clearTimeout(timeout);
  dispose();
  await new Promise((resolve) => server.close(resolve));
  return request.params;
}
const valid = await capture(1, sid, ["opencode", "--resume", sid]);
const mismatched = await capture(2, "ses-other", ["opencode", "--resume", sid]);
if (valid.seq !== 1700000000123000 || valid.seq > Number.MAX_SAFE_INTEGER) throw Error("OpenCode seq compatibility");
if (valid.session_start_source !== "resume") throw Error("native OpenCode source missing");
if (JSON.stringify(valid.resume_argv) !== JSON.stringify(["opencode", "--resume", sid])) throw Error("actual OpenCode argv missing");
if (mismatched.session_start_source !== "select" || "seq" in mismatched || "resume_argv" in mismatched) {
  throw Error("unproven OpenCode identity fields were emitted");
}
console.log(JSON.stringify({valid, mismatched}));
'''
        with tempfile.TemporaryDirectory(prefix="herdr-opencode-session-") as temporary:
            asset_root = Path(temporary)
            (asset_root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
            (asset_root / "tui").mkdir()
            shutil.copyfile(ROOT / "adapters/opencode/tui/herdr-tui-session.js",
                            asset_root / "tui/herdr-tui-session.js")
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script], cwd=ROOT,
                env={**os.environ, "HEARTING_TEST_ASSET_ROOT": str(asset_root)},
                text=True, capture_output=True, timeout=15, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn('"session_start_source":"resume"', result.stdout)


if __name__ == "__main__":
    unittest.main()
