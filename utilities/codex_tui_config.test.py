#!/usr/bin/env python3
"""Behavioral regressions for Codex status-line merge and feasibility."""
from pathlib import Path
import tempfile
import tomllib
import unittest

import codex_tui_config as config


class CodexTuiConfigTest(unittest.TestCase):
    def test_existing_items_and_comments_survive_with_thread_id_last(self):
        source = ('# keep this comment\n[tui] # preserve header\n'
                  'status_line = [\n  "project-name", # item comment\n'
                  '  "thread-id", "custom-item",\n]\n'
                  'status_line_use_colors = false # explicit color\n\n[projects."/tmp/example"]\n'
                  'trust_level = "trusted"\n')
        merged = config.merge_status_line_text(source)
        parsed = tomllib.loads(merged)
        self.assertEqual(parsed["tui"]["status_line"],
                         ["project-name", "custom-item", "thread-id"])
        self.assertIs(parsed["tui"]["status_line_use_colors"], False)
        self.assertIn("# keep this comment", merged)
        self.assertIn("# preserve header", merged)
        self.assertIn("# explicit color", merged)
        self.assertIn('[projects."/tmp/example"]', merged)
        self.assertEqual(config.merge_status_line_text(merged), merged)

    def test_absent_table_key_empty_and_duplicates_converge(self):
        self.assertEqual(tomllib.loads(config.merge_status_line_text(""))["tui"]["status_line"][-1],
                         "thread-id")
        self.assertEqual(tomllib.loads(config.merge_status_line_text(
            '[tui]\nstatus_line_use_colors = false\n'))["tui"]["status_line"][-1], "thread-id")
        for source in ('[tui]\nstatus_line = []\n',
                       '[tui]\nstatus_line = ["thread-id", "x", "thread-id"]\n'):
            with self.subTest(source=source):
                merged = config.merge_status_line_text(source)
                items = tomllib.loads(merged)["tui"]["status_line"]
                self.assertEqual(items, (["x", "thread-id"] if '"x"' in source else ["thread-id"]))

    def test_invalid_input_is_not_written_or_backed_up(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragment = Path(__file__).resolve().parents[1] / "adapters/codex/config/tui-statusline.toml"
            path = root / "config.toml"
            for invalid in ('[tui\nstatus_line = ["x"]\n', '[tui]\nstatus_line = 7\n'):
                path.write_text(invalid, encoding="utf-8")
                with self.assertRaises(config.CodexTuiConfigError):
                    config.apply_status_line(path, fragment)
                self.assertEqual(path.read_text(encoding="utf-8"), invalid)
                self.assertFalse(path.with_name(config.BACKUP_NAME).exists())

    def test_dry_run_and_backup_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragment = Path(__file__).resolve().parents[1] / "adapters/codex/config/tui-statusline.toml"
            path = root / "config.toml"
            path.write_text('[tui]\nstatus_line = ["x"]\n', encoding="utf-8")
            original = path.read_bytes()
            result = config.apply_status_line(path, fragment, dry_run=True)
            self.assertTrue(result["changed"])
            self.assertEqual(path.read_bytes(), original)
            first = config.apply_status_line(path, fragment)
            self.assertTrue(first["changed"])
            self.assertEqual(path.with_name(config.BACKUP_NAME).read_bytes(), original)
            after = path.read_bytes()
            second = config.apply_status_line(path, fragment)
            self.assertFalse(second["changed"])
            self.assertEqual(path.read_bytes(), after)

    def test_feasibility_is_exact_and_overrides_stay_unknown(self):
        self.assertEqual(config.status_line_capability(''), "incapable")
        self.assertEqual(config.status_line_capability('[tui]\nstatus_line = []\n'), "incapable")
        self.assertEqual(config.status_line_capability(
            '[tui]\nstatus_line = ["x", "thread-id"]\n'), "capable")
        self.assertEqual(config.status_line_capability(
            '[tui]\nstatus_line = ["thread-id", "x"]\n'), "incapable")
        self.assertEqual(config.status_line_capability('[tui]\nstatus_line = 7\n'), "unknown")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(config.status_line_capability_for_home(root), "incapable")
            (root / "config.toml").write_text('[tui]\nstatus_line = ["thread-id"]\n')
            self.assertEqual(config.status_line_capability_for_home(
                root, argv=["codex", "-c", 'tui.status_line=["x"]']), "unknown")
            self.assertEqual(config.status_line_capability_for_home(
                root, argv=["codex", "--profile", "custom"]), "unknown")


if __name__ == "__main__":
    unittest.main()
