"""Background calls honor their profile without inheriting agent bootstraps."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import refresh_title as rt


class TextProviderContextTest(unittest.TestCase):
    def test_profile_budget_reaches_every_provider_in_one_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            for adapter, flag in (("claude", "--effort"), ("codex", "-c"), ("opencode", "--variant")):
                with self.subTest(adapter=adapter), mock.patch.object(
                    rt, "provider_settings", return_value={"model": "test-model", "budget": "low"}
                ) as resolve:
                    command = rt.provider_command(adapter, "prompt", workdir=Path(tmp), profile="light")
                    resolve.assert_called_once_with(adapter, mock.ANY, profile="light")
                    argv = command[0]
                    if adapter == "codex":
                        self.assertIn('model_reasoning_effort="low"', argv)
                    else:
                        self.assertEqual(argv[argv.index(flag) + 1], "low")
            self.assertEqual(rt.WORKING_DEBOUNCE_SEC, 300)

    def test_codex_uses_auth_only_home_and_neutral_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "user-codex"
            source.mkdir()
            (source / "auth.json").write_text("{}")
            (source / "AGENTS.md").write_text("GLOBAL BOOTSTRAP")
            (source / "config.toml").write_text('model_reasoning_effort="xhigh"')
            state = Path(tmp) / "state"
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(source)}), mock.patch.object(
                rt, "provider_settings", return_value={"model": "test-model", "budget": "low"}
            ):
                command = rt.provider_command("codex", "prompt", workdir=state)
            runtime = Path(command.env["CODEX_HOME"])
            self.assertEqual((runtime / "auth.json").resolve(), source / "auth.json")
            self.assertFalse((runtime / "AGENTS.md").exists())
            self.assertFalse((runtime / "config.toml").exists())
            self.assertEqual(command.cwd, str(state))
            self.assertEqual((source / "config.toml").read_text(), 'model_reasoning_effort="xhigh"')
            for feature in ("shell_tool", "unified_exec", "multi_agent", "apps", "plugins", "hooks"):
                self.assertIn("features.%s=false" % feature, command[0])

    def test_opencode_keeps_transport_without_inheriting_mcp_and_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "user-config" / "opencode"
            source.mkdir(parents=True)
            original = ('{ // transport comment\n'
                        '"provider":{"custom":{"options":{"baseURL":"https://host/path/*ok*/"}}},\n'
                        '"mcp":{"foreign":{"command":["foreign"]}},'
                        '"instructions":["GLOBAL.md"],"plugin":["foreign"],}')
            (source / "opencode.jsonc").write_text(original)
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(source.parent)}), mock.patch.object(
                rt, "provider_settings", return_value={"model": "custom/model", "budget": "runtime-default"}
            ):
                command = rt.provider_command("opencode", "prompt", workdir=Path(tmp) / "state")
            config = json.loads((Path(command.env["XDG_CONFIG_HOME"]) / "opencode/opencode.json").read_text())
            self.assertEqual(config["provider"]["custom"]["options"]["baseURL"], "https://host/path/*ok*/")
            self.assertEqual(config["mcp"], {})
            self.assertEqual(config["instructions"], [])
            self.assertNotIn("plugin", config)
            self.assertIn("runtime-default", command[0])
            self.assertEqual((source / "opencode.jsonc").read_text(), original)

    def test_cascade_passes_each_child_context_without_mutating_caller(self):
        env = {"CODEX_HOME": "caller", "OPENCODE_CONFIG_CONTENT": "foreign", "KEEP": "value"}
        command = rt._ProviderCommand(["provider"], "prompt", None,
                                      env={"CODEX_HOME": "isolated", "OPENCODE_CONFIG_CONTENT": None},
                                      cwd="neutral")
        with mock.patch.object(rt.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="reply")) as run:
            self.assertEqual(rt.run_provider_cascade([command], timeout=1, env=env, cwd="caller"), ("reply", 0))
        self.assertEqual(run.call_args.kwargs["cwd"], "neutral")
        self.assertEqual(run.call_args.kwargs["env"], {"CODEX_HOME": "isolated", "KEEP": "value"})
        self.assertEqual(env["CODEX_HOME"], "caller")
        self.assertIn("OPENCODE_CONFIG_CONTENT", env)

    def test_opencode_refreshes_existing_generated_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".opencode/agent/fleet-titler.md"
            path.parent.mkdir(parents=True)
            path.write_text("old per-tool deny list")
            rt._opencode_workdir("unused", workdir=tmp)
            self.assertEqual(path.read_text(), rt._OPENCODE_AGENT)


if __name__ == "__main__":
    unittest.main()
