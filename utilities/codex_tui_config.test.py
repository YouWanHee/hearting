#!/usr/bin/env python3
"""Behavioral regressions for Codex status-line merge and feasibility."""
from pathlib import Path
import os
import stat
import tempfile
import tomllib
import unittest

import codex_tui_config as config


class CodexTuiConfigTest(unittest.TestCase):
    def _tracked_merge(self, text, previous=None):
        merged = config.merge_status_line_text(text)
        return merged, config.status_line_record(text, merged, previous)

    def test_restore_clean_and_existing_config_after_refresh(self):
        for source in ('', 'model = "mine"\n',
                       '# mine\n[tui]\nstatus_line = ["x"] # footer\nstatus_line_use_colors = false\n',
                       '[tui]\nstatus_line = ["thread-id", "x"]\n',
                       '[tui]\nstatus_line = []\n',
                       '[tui]\nnotification_method = "osc9"\n'):
            with self.subTest(source=source):
                merged, record = self._tracked_merge(source)
                refreshed, record = self._tracked_merge(merged, record)
                self.assertEqual(config.restore_status_line_text(refreshed, record), source)

    def test_restore_keeps_later_items_colors_and_other_tables(self):
        merged, record = self._tracked_merge('[tui]\nstatus_line = ["x"]\n')
        edited = merged.replace('["x", "thread-id"]', '["x", "new", "thread-id"]')
        edited = edited.replace('status_line_use_colors = true', 'status_line_use_colors = false')
        edited += '\n[projects."/work"]\ntrust_level = "trusted"\n'
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                current, receipt = self._tracked_merge(edited, record) if refresh else (edited, record)
                restored = config.restore_status_line_text(current, receipt)
                tui = tomllib.loads(restored)["tui"]
                self.assertEqual(tui["status_line"], ["x", "new"])
                self.assertIs(tui["status_line_use_colors"], False)
                self.assertIn('[projects."/work"]\ntrust_level = "trusted"\n', restored)

    def test_restore_keeps_user_owned_thread_id_and_new_table_settings(self):
        merged, record = self._tracked_merge('[tui]\nstatus_line = ["thread-id", "x"]\n')
        edited = merged.replace('["x", "thread-id"]', '["new", "thread-id"]')
        self.assertEqual(tomllib.loads(config.restore_status_line_text(edited, record))["tui"]["status_line"],
                         ["new", "thread-id"])
        merged, record = self._tracked_merge('')
        edited = merged + 'notification_method = "osc9"\n'
        self.assertEqual(tomllib.loads(config.restore_status_line_text(edited, record)),
                         {"tui": {"notification_method": "osc9"}})

    def test_missing_receipt_does_not_claim_legacy_settings(self):
        source = config.merge_status_line_text('')
        self.assertEqual(config.restore_status_line_text(source, None), source)
        for invalid in ({"keys": {}}, {"keys": {"status_line": {"baseline": {"present": False}}}}):
            self.assertEqual(config.restore_status_line_text(source, invalid), source)

    def test_new_keys_stay_outside_the_next_tables_managed_comment(self):
        for settings in ('', 'status_line = ["custom"]\n', 'status_line_use_colors = false\n'):
            source = '[tui]\n' + settings + '\n# >>> managed block\n[other]\nvalue = true\n'
            merged, record = self._tracked_merge(source)
            self.assertIn('\n# >>> managed block\n[other]\nvalue = true\n', merged)
            self.assertEqual(config.restore_status_line_text(merged, record), source)

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

    def test_symlink_target_is_updated_with_one_time_backup_and_idempotently(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragment = Path(__file__).resolve().parents[1] / "adapters/codex/config/tui-statusline.toml"
            target = root / "shared" / "codex.toml"
            target.parent.mkdir()
            original = (b'model = "keep"\n[tui]\n'
                        b'status_line = ["project-name", "thread-id", "custom-item"]\n'
                        b'status_line_use_colors = false\n')
            target.write_bytes(original)
            target.chmod(0o640)
            path = root / "config.toml"
            link_target = os.path.relpath(target, path.parent)
            path.symlink_to(link_target)

            first = config.apply_status_line(path, fragment)
            self.assertTrue(first["changed"])
            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), link_target)
            self.assertEqual(path.with_name(config.BACKUP_NAME).read_bytes(), original)
            merged_bytes = target.read_bytes()
            merged = tomllib.loads(merged_bytes.decode("utf-8"))
            self.assertEqual(merged["model"], "keep")
            self.assertEqual(merged["tui"]["status_line"],
                             ["project-name", "custom-item", "thread-id"])
            self.assertIs(merged["tui"]["status_line_use_colors"], False)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

            second = config.apply_status_line(path, fragment)
            self.assertFalse(second["changed"])
            self.assertEqual(target.read_bytes(), merged_bytes)
            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), link_target)

    def test_invalid_symlink_target_is_unchanged_and_not_backed_up(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fragment = Path(__file__).resolve().parents[1] / "adapters/codex/config/tui-statusline.toml"
            target = root / "shared.toml"
            original = b'[tui]\nstatus_line = 7\n'
            target.write_bytes(original)
            path = root / "config.toml"
            path.symlink_to(target)

            with self.assertRaises(config.CodexTuiConfigError):
                config.apply_status_line(path, fragment)
            self.assertTrue(path.is_symlink())
            self.assertEqual(os.readlink(path), str(target))
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse(path.with_name(config.BACKUP_NAME).exists())

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
