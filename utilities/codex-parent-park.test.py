#!/usr/bin/env python3
"""Regression tests for the retired Codex all-tool parent park."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "adapters" / "codex" / "hooks" / "hooks.json"


class ParentParkRetirementTest(unittest.TestCase):
    def test_hook_manifest_has_no_wildcard_parent_park(self):
        config = json.loads(HOOKS.read_text(encoding="utf-8"))
        entries = config["hooks"].get("PreToolUse", [])
        self.assertEqual(entries, [])
        rendered = json.dumps(entries)
        self.assertNotIn("AGENT_PARENT_PARK_ONLY", rendered)


if __name__ == "__main__":
    unittest.main()
