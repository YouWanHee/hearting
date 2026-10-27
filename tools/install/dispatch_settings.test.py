#!/usr/bin/env python3
"""In-place, validated edits of the user-owned dispatch allocation keys."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import dispatch_settings as settings
import fixture_env

USER = """# my notes stay
schema_version: 3
harnesses:
  enabled: [claude, codex, opencode]
profiles:
""" + "".join(f"""  {name}:
    primary: [codex, claude]
    relief: []
    last_resort: []
    promote_relief_below: 0
""" for name in ("deep", "balanced-deep", "balanced", "light", "mini")) + """allocation:
  strategy: balanced
  window: 30
  usage_gate_used_percent: 85   # keep this comment
  depth_affinity:
    worker: codex
  depth_affinity_weight: 0.85
  usage_headroom_exponent: 2
  # lower opencode
  harness_weights:
    opencode: 0.4
capabilities:
"""


class DispatchSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dispatch-settings-test-")
        self.addCleanup(self.tmp.cleanup)
        env = fixture_env.build_environment(Path(self.tmp.name), ROOT)
        env.pop("DISPATCH_DEFAULTS_CONFIG", None)
        fixture_env.prepare_environment(env)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.path = Path(env["XDG_CONFIG_HOME"]) / "hearting" / "dispatch-defaults.yaml"

    def write(self, text=USER):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def test_one_key_changes_one_line_and_keeps_comments_with_a_backup(self):
        self.write()
        planned = settings.set_allocation({"usage_gate_used_percent": 80}, dry_run=True)
        self.assertEqual(planned["status"], "planned")
        self.assertEqual(self.path.read_text(), USER)
        changed = settings.set_allocation({"usage_gate_used_percent": 80})
        self.assertEqual(changed["status"], "changed")
        self.assertEqual(changed["changes"], ["usage_gate_used_percent: 85 -> 80"])
        after = self.path.read_text().splitlines()
        before = USER.splitlines()
        gate = next(index for index, line in enumerate(before) if "usage_gate_used_percent" in line)
        self.assertEqual([index for index, (a, b) in enumerate(zip(before, after)) if a != b], [gate])
        self.assertEqual(len(before), len(after))
        self.assertEqual(after[gate], "  usage_gate_used_percent: 80   # keep this comment")
        self.assertEqual(Path(changed["backup"]).read_text(), USER)
        self.assertEqual(settings.set_allocation({"usage_gate_used_percent": 80})["status"], "unchanged")

    def test_nested_keys_insert_update_and_remove(self):
        self.write()
        settings.set_allocation({"harness_weights.codex": 0.7, "harness_weights.opencode": 0.3,
                                 "depth_affinity.owner": "claude", "strategy": "capacity-aware"})
        text = self.path.read_text()
        self.assertIn("  harness_weights:\n    opencode: 0.3\n    codex: 0.7\n", text)
        self.assertIn("  depth_affinity:\n    worker: codex\n    owner: claude\n", text)
        self.assertIn("  strategy: capacity-aware\n", text)
        settings.set_allocation({"depth_affinity.owner": None, "depth_affinity.worker": None})
        self.assertNotIn("depth_affinity:", self.path.read_text())

    def test_invalid_values_and_unknown_keys_leave_the_file_unchanged(self):
        self.write()
        for updates in ({"usage_gate_used_percent": 101}, {"harness_weights.codex": 0.0},
                        {"usage_headroom_exponent": 5}, {"strategy": "fastest"}):
            with self.assertRaises(settings.DispatchSettingsUsageError):
                settings.set_allocation(updates)
        with self.assertRaises(settings.DispatchSettingsUsageError):
            settings.set_allocation({"owner_order": "claude"})
        self.assertEqual(self.path.read_text(), USER)
        self.assertFalse(self.path.with_name(self.path.name + ".bak").exists())

    def test_missing_user_file_is_seeded_from_the_shipped_policy(self):
        result = settings.set_allocation({"window": 60})
        self.assertEqual((result["status"], result["source_reason"]), ("changed", "user-missing; shipped seed"))
        self.assertIsNone(result["backup"])
        self.assertIn("  window: 60\n", self.path.read_text())
        shipped = (ROOT / "profiles" / "dispatch-defaults.yaml").read_text()
        self.assertNotIn("  window: 60\n", shipped)

    def test_override_path_is_the_file_dispatch_reads(self):
        other = Path(self.tmp.name) / "other.yaml"
        other.write_text(USER, encoding="utf-8")
        with mock.patch.dict(os.environ, {"DISPATCH_DEFAULTS_CONFIG": str(other)}):
            settings.set_allocation({"window": 20})
        self.assertIn("  window: 20\n", other.read_text())
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
