#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_model_config", ROOT / "utilities" / "model_config.py"
)
assert SPEC and SPEC.loader
config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = config
SPEC.loader.exec_module(config)


BASE = (
    'CFG_MODEL_PROFILE_DEEP=deep:xhigh\n'
    'CFG_TIER_DEEP_MODEL=shipped-model\n'
    'CFG_TIER_DEEP_EFFORT=xhigh\n'
)

# O2 (Astra guide alignment): a coherent existing-key deep-tier opt-in fixture.
# Every CFG_ key here already exists in BASE's shape -- the opt-in changes only
# values, never adds a key -- with CFG_TIER_DEEP_FAILOVER_CASCADE added so the
# diagnostic has a first step to compare.
ASTRA_ULTRA_COHERENT = (
    'CFG_MODEL_PROFILE_DEEP=deep:ultra\n'
    'CFG_TIER_DEEP_MODEL=gpt-6-astra\n'
    'CFG_TIER_DEEP_EFFORT=ultra\n'
    'CFG_TIER_DEEP_FAILOVER_CASCADE="gpt-6-astra:ultra gpt-5.6-luna:medium"\n'
)


class ModelConfigTest(unittest.TestCase):
    def make_root(self, adapter="claude", shipped=BASE):
        root = Path(tempfile.mkdtemp())
        shipped_path = root / "adapters" / adapter / "config" / "models.conf"
        shipped_path.parent.mkdir(parents=True)
        shipped_path.write_text(shipped, encoding="utf-8")
        return root

    def test_each_runtime_home_is_independent(self):
        for adapter, variable, suffix in (
            ("claude", "CLAUDE_CONFIG_DIR", "claude"),
            ("codex", "CODEX_HOME", "codex"),
            ("opencode", "XDG_CONFIG_HOME", "opencode"),
        ):
            with self.subTest(adapter=adapter):
                environ = {"HOME": "/tmp/isolated-home", variable: f"/tmp/{suffix}"}
                expected = Path(f"/tmp/{suffix}")
                if adapter == "opencode":
                    expected /= "opencode"
                self.assertEqual(config.user_path(adapter, environ=environ), expected / "agent-config/models.conf")

    def test_valid_user_file_wins_as_a_complete_file(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(BASE + 'CFG_USER_EXTRA="literal"\n', encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "user")
        self.assertEqual(receipt.reason, "user-valid")
        self.assertEqual(values["CFG_USER_EXTRA"], "literal")

    def test_resolve_config_accepts_the_exact_call_shape_native_agent_payload_uses(self):
        # WP1 (O1): tools/install/native_agent_payload.py's plan_payload() calls
        # resolve_config("codex", runtime=<absolute Path>, source_root=...) to
        # compute the effective mapping it renders and digests. Pin that exact
        # call shape here so a signature drift breaks this test, not silently
        # the payload primitive.
        root = self.make_root(adapter="codex")
        home = root / "runtime-home"
        values, receipt = config.resolve_config("codex", runtime=home, source_root=root)
        self.assertEqual(receipt.adapter, "codex")
        self.assertEqual(receipt.source, "shipped")
        self.assertEqual(receipt.reason, "user-missing")
        self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "shipped-model")

    def test_missing_incomplete_malformed_and_unsafe_user_files_fallback_whole_file(self):
        for text, reason in ((None, "user-missing"), ("CFG_TIER_DEEP_MODEL=user-only\n", "user-incomplete"),
                             ("CFG_MODEL_PROFILE_DEEP=bad+syntax\n", "user-malformed"),
                             ("$(touch sentinel)\n" + BASE, "user-malformed")):
            with self.subTest(reason=reason):
                root = self.make_root()
                home = root / "home"
                user = home / "agent-config" / "models.conf"
                if text is not None:
                    user.parent.mkdir(parents=True)
                    user.write_text(text, encoding="utf-8")
                values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
                self.assertEqual(receipt.reason, reason)
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "shipped-model")

    def test_quoted_comments_extra_keys_and_no_merge(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text('CFG_MODEL_PROFILE_DEEP="deep:xhigh" # comment\nCFG_TIER_DEEP_MODEL="user-model ; $(touch sentinel) `echo nope` O\'Brien"\nCFG_TIER_DEEP_EFFORT=xhigh\n', encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "user")
        self.assertIn("$(touch sentinel)", values["CFG_TIER_DEEP_MODEL"])
        self.assertNotIn("CFG_UNDECLARED", values)

    def test_shipped_failure_is_fatal(self):
        root = self.make_root(shipped="$(bad)\n")
        with self.assertRaises(config.ShippedConfigError):
            config.resolve_config("claude", runtime=root / "home", source_root=root)

    def test_user_symlink_is_rejected_and_falls_back(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.symlink_to(root / "adapters" / "claude" / "config" / "models.conf")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "shipped")
        self.assertEqual(receipt.reason, "user-unreadable")
        self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "shipped-model")

    def test_diagnose_reports_only_receipt_for_non_codex_adapters(self):
        root = self.make_root(adapter="claude")
        report = config.diagnose("claude", runtime=root / "home", source_root=root)
        self.assertEqual(report["receipt"]["adapter"], "claude")
        self.assertNotIn("deep", report)

    def test_diagnose_coherent_astra_ultra_fixture_reports_no_mismatch(self):
        root = self.make_root(adapter="codex")
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(ASTRA_ULTRA_COHERENT, encoding="utf-8")
        report = config.diagnose("codex", runtime=home, source_root=root)
        self.assertEqual(report["receipt"]["source"], "user")
        deep = report["deep"]
        self.assertEqual(deep["tier_model"], "gpt-6-astra")
        self.assertEqual(deep["tier_effort"], "ultra")
        self.assertEqual(deep["profile_tier"], "deep")
        self.assertEqual(deep["profile_budget"], "ultra")
        self.assertEqual(
            deep["failover_cascade"],
            [{"model": "gpt-6-astra", "effort": "ultra"}, {"model": "gpt-5.6-luna", "effort": "medium"}],
        )
        self.assertEqual(deep["mismatches"], [])

    def test_diagnose_one_field_perturbation_reports_exact_mismatch(self):
        root = self.make_root(adapter="codex")
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        # Only CFG_MODEL_PROFILE_DEEP's effort is perturbed to `high` -- tier
        # effort and the failover cascade's first step still say `ultra`.
        user.write_text(
            ASTRA_ULTRA_COHERENT.replace("CFG_MODEL_PROFILE_DEEP=deep:ultra", "CFG_MODEL_PROFILE_DEEP=deep:high"),
            encoding="utf-8",
        )
        report = config.diagnose("codex", runtime=home, source_root=root)
        deep = report["deep"]
        self.assertEqual(deep["profile_budget"], "high")
        self.assertEqual(len(deep["mismatches"]), 1)
        self.assertIn("CFG_TIER_DEEP_EFFORT", deep["mismatches"][0])
        self.assertIn("CFG_MODEL_PROFILE_DEEP", deep["mismatches"][0])

    def test_diagnose_shipped_fallback_uses_same_receipt_as_resolve_config(self):
        root = self.make_root(adapter="codex")
        home = root / "home"  # no user file at all -> shipped fallback
        report = config.diagnose("codex", runtime=home, source_root=root)
        _, receipt = config.resolve_config("codex", runtime=home, source_root=root)
        self.assertEqual(report["receipt"], receipt.as_dict())
        self.assertEqual(report["deep"]["mismatches"], [])

    def test_diagnose_cli_prints_json(self):
        root = self.make_root(adapter="codex")
        home = root / "home"
        result = subprocess.run(
            [sys.executable, str(ROOT / "utilities" / "model_config.py"), "--adapter", "codex",
             "--runtime-home", str(home), "--source-root", str(root), "--diagnose"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("receipt", payload)
        self.assertIn("deep", payload)

    def test_bridge_quotes_metacharacters_and_receipt(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(BASE.replace("shipped-model", '"$(touch sentinel); `echo bad` O\'Brien"'), encoding="utf-8")
        receipt = root / "receipt.json"
        command = [str(ROOT / "utilities" / "model-config.sh"), "--adapter", "claude", "--runtime-home", str(home), "--source-root", str(root), "--receipt-fd", "3"]
        result = subprocess.run(shlex.join(command) + f" 3>{shlex.quote(str(receipt))}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CFG_TIER_DEEP_MODEL='$(touch sentinel); `echo bad` O'\"'\"'Brien'", result.stdout)
        self.assertEqual(json.loads(receipt.read_text())["source"], "user")
        self.assertFalse((root / "sentinel").exists())


if __name__ == "__main__":
    unittest.main()
