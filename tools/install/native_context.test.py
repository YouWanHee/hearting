from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/install"))
sys.path.insert(0, str(ROOT / "utilities"))
import model_settings
import native_context
import runtime_activation
import safe_fs


class NativeContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native-context-")
        self.home = Path(self.temp.name)
        self.old_fixture = os.environ.get("HEARTING_FIXTURE_ROOT")
        os.environ["HEARTING_FIXTURE_ROOT"] = str(self.home)

    def seed_models(self, adapter="codex", value="760000"):
        config_dir = self.home / "agent-config"
        config_dir.mkdir(exist_ok=True)
        source = ROOT / "adapters" / adapter / "config/models.conf"
        raw = source.read_text(encoding="utf-8")
        import re
        raw, count = re.subn(r"^CFG_MODEL_CONTEXT_WINDOW=.*$",
                             f"CFG_MODEL_CONTEXT_WINDOW={value}", raw, count=1, flags=re.M)
        if not count:
            raw += f"\nCFG_MODEL_CONTEXT_WINDOW={value}\n"
        (config_dir / "models.conf").write_text(raw, encoding="utf-8")

    def tearDown(self):
        if self.old_fixture is None:
            os.environ.pop("HEARTING_FIXTURE_ROOT", None)
        else:
            os.environ["HEARTING_FIXTURE_ROOT"] = self.old_fixture
        self.temp.cleanup()

    def test_codex_root_value_changes_in_place_and_profile_is_reported(self):
        config = self.home / "config.toml"
        original = ("# 한글 사용자 주석\r\nprofile = \"x\"\r\nmodel_context_window = 800000 # managed\r\n"
                    "model = \"vendor/model\"\r\n\r\n[profiles.x]\r\nmodel_context_window = 900000\r\n")
        original = original.encode("utf-8")
        config.write_bytes(original)
        self.seed_models()
        result = native_context.project_context_window(
            "codex", runtime=self.home, source_root=ROOT)
        updated = config.read_bytes()
        self.assertIn(b"model_context_window = 760000 # managed\r\n", updated)
        self.assertIn(b"[profiles.x]\r\nmodel_context_window = 900000\r\n", updated)
        self.assertEqual(updated.replace(b"760000", b"800000"), original)
        self.assertEqual(updated.count(b"model_context_window = 760000"), 1)
        self.assertTrue(any("profile" in line for line in result["report_lines"]))

    def test_codex_insert_precedes_table_and_ignores_multiline_string_header(self):
        config = self.home / "config.toml"
        config.write_text('note = """\n[not-a-table]\nmodel_context_window = 5\n"""\n\n[model]\nname="x"',
                          encoding="utf-8")
        self.seed_models()
        native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
        text = config.read_text(encoding="utf-8")
        self.assertEqual(text.count("model_context_window = 760000"), 1)
        self.assertLess(text.index("model_context_window = 760000"), text.index("[model]"))
        self.assertIn("[not-a-table]\nmodel_context_window = 5", text)

    def test_claude_context_only_overwrites_managed_value_and_deactivate_keeps_it(self):
        settings = self.home / "settings.json"
        settings.write_text(json.dumps({"other": {"keep": 1}, "env": {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000", "USER_KEY": "value"}},
            separators=(",", ":")), encoding="utf-8")
        self.seed_models("claude", "760000")
        result = native_context.project_context_window(
            "claude", runtime=self.home, source_root=ROOT,
            claude_merge=lambda **kwargs: runtime_activation._merge_claude_settings(
                ROOT, None, config_path=settings, **kwargs), context_only=True)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["env"], {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "760000", "USER_KEY": "value"})
        self.assertTrue(result["changed"])
        runtime_activation._unmerge_claude_settings({"managed_config": {
            "claude_values": {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "760000"}}}}, "global",
            config_path=settings)
        # The merge's teardown path deliberately excludes the operating preference.
        self.assertEqual(json.loads(settings.read_text())["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "760000")

    def test_model_set_repairs_native_drift_even_when_scalar_is_unchanged(self):
        self.seed_models()
        result = model_settings.set_model("codex", "context-window", "760000",
                                          runtime=self.home, source_root=ROOT)
        self.assertEqual(result["status"], "unchanged")
        config = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("model_context_window = 760000", config)

    def test_model_set_claude_updates_only_native_context_env(self):
        config_dir = self.home / ".claude/agent-config"
        config_dir.mkdir(parents=True)
        text = (ROOT / "adapters/claude/config/models.conf").read_text(encoding="utf-8")
        (config_dir / "models.conf").write_text(text.replace("CFG_MODEL_CONTEXT_WINDOW=800000",
                                                               "CFG_MODEL_CONTEXT_WINDOW=800000"))
        settings = self.home / ".claude/settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"other": True, "env": {"USER_KEY": "keep",
                            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "800000"}}), encoding="utf-8")
        result = model_settings.set_model("claude", "context-window", "760000",
                                          runtime=self.home / ".claude", source_root=ROOT)
        self.assertEqual(result["status"], "changed")
        self.assertEqual(sum(" -> " in line for line in result["lines"]), 1)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["env"], {"USER_KEY": "keep", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "760000"})

    def test_model_set_projection_failure_reports_saved_scalar_and_retry_repairs_native(self):
        self.seed_models("codex", "800000")
        config = self.home / "config.toml"
        config.write_text("model_context_window = 800000\n[broken\n", encoding="utf-8")
        with self.assertRaisesRegex(model_settings.ModelSettingsError, "models.conf was saved"):
            model_settings.set_model("codex", "context-window", "760000",
                                     runtime=self.home, source_root=ROOT)
        selected = model_settings.model_config.parse_config(self.home / "agent-config/models.conf")
        self.assertEqual(selected["CFG_MODEL_CONTEXT_WINDOW"], "760000")
        config.write_text("model = \"vendor/model\"\n", encoding="utf-8")
        result = model_settings.set_model("codex", "context-window", "760000",
                                          runtime=self.home, source_root=ROOT)
        self.assertEqual(result["status"], "unchanged")
        self.assertIn("model_context_window = 760000", config.read_text(encoding="utf-8"))

    def test_activation_projects_both_native_preferences(self):
        claude_home, codex_home = self.home / ".claude", self.home / ".codex"
        claude_home.mkdir(); codex_home.mkdir()
        claude_config = claude_home / "agent-config"
        codex_config = codex_home / "agent-config"
        claude_config.mkdir(); codex_config.mkdir()
        claude_text = (ROOT / "adapters/claude/config/models.conf").read_text()
        (claude_config / "models.conf").write_text(claude_text.replace(
            "CFG_MODEL_CONTEXT_WINDOW=800000", "CFG_MODEL_CONTEXT_WINDOW=760000"))
        (codex_config / "models.conf").write_text(
            (ROOT / "adapters/codex/config/models.conf").read_text().replace(
                "CFG_MODEL_CONTEXT_WINDOW=800000", "CFG_MODEL_CONTEXT_WINDOW=760000"))
        env = {**os.environ, "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(claude_home),
               "CODEX_HOME": str(codex_home), "HEARTING_FIXTURE_ROOT": str(self.home)}
        env.pop("HEARTING_MODEL_CONFIG_HOME", None)
        with patch.dict(os.environ, env, clear=True):
            with safe_fs.TargetLocks((claude_home / "settings.json", codex_home / "config.toml")):
                claude = runtime_activation._prepare_runtime_config("claude", ROOT, None,
                                                                    native_locked=True)
                codex = runtime_activation._prepare_runtime_config("codex", ROOT, None,
                                                                   native_locked=True)
        cdata = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(cdata["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "760000")
        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("model_context_window = 760000", text)
        self.assertTrue(any("760000" in line for line in claude[0]["report_lines"]))
        self.assertTrue(any("760000" in line for line in codex[0]["report_lines"]))

    def test_claude_worker_home_receives_dynamic_context_env_and_permissions(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_home_fixture", ROOT / "tools/profile/build-home.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        source = self.home / "user/.claude"
        source.mkdir(parents=True)
        (source / "settings.json").write_text(json.dumps({"env": {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "760000"}, "permissions": {"allow": ["Read"]}}))
        destination = self.home / "worker"
        env = {"HOME": str(self.home / "user"), "CLAUDE_CONFIG_DIR": str(source),
               "AGENT_DISPATCH_JOBS": str(self.home / "jobs.log"), "AGENT_HOME": str(ROOT)}
        module.build_worker_home(ROOT, "claude", "stage", "fixture-session",
                                 env=env, destination=destination)
        copied = json.loads((destination / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(copied["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "760000")
        self.assertEqual(copied["permissions"], {"allow": ["Read"]})

    def test_codex_bad_toml_is_left_untouched(self):
        config = self.home / "config.toml"
        config.write_text("model_context_window = 800000\n[broken\n", encoding="utf-8")
        self.seed_models()
        before = config.read_bytes()
        with self.assertRaises(native_context.NativeContextError):
            native_context.project_context_window("codex", runtime=self.home, source_root=ROOT)
        self.assertEqual(config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
