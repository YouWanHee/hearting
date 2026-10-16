"""Safe selection behavior for legacy Claude worker credential layouts."""
import json
from pathlib import Path
import tempfile
import unittest

from claude_credential_recovery import SECURE_STORAGE_ENV, resolve_storage_binding


class ClaudeCredentialRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / 'caller'
        self.cwd.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_values_keep_native_empty_and_relative_path_meanings(self):
        config = self.root / 'config'
        self.assertEqual(resolve_storage_binding(
            {SECURE_STORAGE_ENV: ''}, config, cwd=self.cwd), '')
        self.assertEqual(resolve_storage_binding(
            {SECURE_STORAGE_ENV: 'relative-auth'}, config, cwd=self.cwd),
            str((self.cwd / 'relative-auth').resolve()))
        self.assertEqual(resolve_storage_binding(
            {SECURE_STORAGE_ENV: str(self.root / 'custom-auth')}, config, cwd=self.cwd),
            str(self.root / 'custom-auth'))

    def test_settings_override_inherited_value_and_empty_is_not_dropped(self):
        config = self.root / 'config'
        self.assertEqual(resolve_storage_binding(
            {SECURE_STORAGE_ENV: str(self.root / 'inherited')}, config,
            settings_env={SECURE_STORAGE_ENV: ''}, cwd=self.cwd), '')
        self.assertEqual(resolve_storage_binding(
            {}, config, settings_env={SECURE_STORAGE_ENV: 'settings-auth'}, cwd=self.cwd),
            str((self.cwd / 'settings-auth').resolve()))

    def test_default_config_uses_native_empty_binding_and_custom_config_is_pinned(self):
        home = self.root / 'user'
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(home)}, home / '.claude', cwd=self.cwd), '')
        custom = self.root / 'account-b'
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(home)}, custom, cwd=self.cwd), str(custom.resolve()))

    def test_intact_legacy_link_reconnects_to_its_exact_store(self):
        store = self.root / 'store'
        store.mkdir()
        (store / '.credentials.json').write_text('{"test":"synthetic"}')
        worker = self.root / 'legacy-worker'
        worker.mkdir()
        (worker / 'worker-home.json').write_text(json.dumps({'harness': 'claude'}))
        (worker / '.credentials.json').symlink_to(store / '.credentials.json')
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(self.root / 'user')}, worker, cwd=self.cwd), str(store.resolve()))

    def test_legacy_candidate_uses_local_cohort_store_without_promotion(self):
        main = self.root / 'main'
        main.mkdir()
        main_file = main / '.credentials.json'
        main_file.write_bytes(b'{"synthetic":"new-main"}')
        worker = self.root / 'legacy-worker'
        worker.mkdir()
        (worker / 'worker-home.json').write_text(json.dumps({'harness': 'claude'}))
        candidate = worker / '.credentials.json'
        candidate.write_bytes(b'{"synthetic":"opaque-worker-token"}')
        before = candidate.read_bytes()
        main_before = main_file.read_bytes()
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(self.root / 'user')}, worker, cwd=self.cwd),
            str(worker.resolve()))
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(main_file.read_bytes(), main_before)

        empty_worker = self.root / 'legacy-worker-without-credentials'
        empty_worker.mkdir()
        (empty_worker / 'worker-home.json').write_text(
            json.dumps({'harness': 'claude'}))
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(self.root / 'user')}, empty_worker, cwd=self.cwd),
            str(empty_worker.resolve()))
        self.assertFalse((empty_worker / '.credentials.json').exists())
        self.assertEqual(main_file.read_bytes(), main_before)

    def test_new_worker_metadata_propagates_binding_across_nested_launches(self):
        worker = self.root / 'worker'
        worker.mkdir()
        (worker / 'worker-home.json').write_text(json.dumps({
            'harness': 'claude', 'claude_secure_storage_dir': str(self.root / 'store')}))
        self.assertEqual(resolve_storage_binding(
            {'HOME': str(self.root / 'user')}, worker, cwd=self.cwd),
            str(self.root / 'store'))


if __name__ == '__main__':
    unittest.main()
