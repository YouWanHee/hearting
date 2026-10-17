"""Launch isolation must preserve user guards/login and the assigned input."""
import json
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import worker_runtime_home
from worker_runtime_home import prepare_worker_home, codex_worker_arguments, claude_worker_arguments
from worker_bootstrap import render_worker_bootstrap
from claude_credential_recovery import SECURE_STORAGE_ENV

ROOT = Path(__file__).resolve().parents[1]


class WorkerHomes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / 'user'
        self.source.mkdir()
        self.env = {'AGENT_DISPATCH_JOBS': str(self.base / 'dispatch/jobs.log')}

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_every_type_isolates_global_input_keeps_auth_config_and_hooks(self):
        (self.source / 'agent-config').mkdir()
        (self.source / 'agent-config/models.conf').write_text('user-model-policy')
        for name, content in [('auth.json', '{}'), ('config.toml', '[features]\nhooks=true\n[mcp_servers.external]\ncommand="external"\n'), ('hooks.json', '{"user_guard":true}')]:
            (self.source / name).write_text(content)
        (self.source / 'skills/.system/test-skill').mkdir(parents=True)
        (self.source / 'skills/.system/test-skill/SKILL.md').write_text('test')
        self.env['CODEX_HOME'] = str(self.source)
        before = {p.name: p.read_bytes() for p in self.source.iterdir() if p.is_file()}
        for typ in ('owner', 'stage', 'review', 'support', 'frame'):
            env = prepare_worker_home(ROOT, 'codex', typ, typ, env=self.env)
            self.assertNotIn(SECURE_STORAGE_ENV, env)
            self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
            self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
            home = Path(env['CODEX_HOME'])
            self.assertNotIn('AGENTS.md — Codex Adapter Bootstrap', (home / 'AGENTS.md').read_text())
            for name in before:
                self.assertEqual((home / name).resolve(), (self.source / name).resolve())
            self.assertFalse((home / 'agents').exists())
            self.assertFalse((home / 'plugins').exists())
            self.assertEqual((home / 'agent-config/models.conf').read_text(), 'user-model-policy')
            args = codex_worker_arguments(env)
            self.assertIn('features.multi_agent=false', args)
            self.assertIn('features.default_mode_request_user_input=false', args)
            self.assertIn('mcp_servers={"external"={"command"="true","enabled"=false}}', args)
            self.assertTrue(any('test-skill' in arg for arg in args))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.iterdir() if p.is_file()})

    def test_claude_guards_permissions_and_credentials_survive_catalog_removal(self):
        (self.source / 'hooks').mkdir()
        (self.source / 'hooks/user-guard.py').write_text('guard')
        settings = {'hooks': {'PreToolUse': [{'hooks': [{'command': 'user-guard'}]}]},
                    'permissions': {'deny': ['Read(secret)']}, 'env': {'USER_GUARD': '1'},
                    'enabledPlugins': {'plugin@market': True}, 'autoMemoryEnabled': True}
        (self.source / 'settings.json').write_text(json.dumps(settings))
        (self.source / '.credentials.json').write_text('{}')
        credential_bytes = (self.source / '.credentials.json').read_bytes()
        account = {'accountUuid': 'user-account', 'organizationUuid': 'user-org'}
        (self.source / '.claude.json').write_text(json.dumps({'oauthAccount': account, 'mcpServers': {'main': {}}}))
        self.env['CLAUDE_CONFIG_DIR'] = str(self.source)
        env = prepare_worker_home(ROOT, 'claude', 'support', 'tidy', env=self.env)
        self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
        self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
        home = Path(env['CLAUDE_CONFIG_DIR'])
        actual = json.loads((home / 'settings.json').read_text())
        for key in ('hooks', 'permissions', 'env'):
            self.assertEqual(actual[key], settings[key])
        self.assertEqual(actual['enabledPlugins'], {'plugin@market': False})
        self.assertFalse(actual['autoMemoryEnabled'])
        self.assertEqual(env[SECURE_STORAGE_ENV], str(self.source.resolve()))
        self.assertFalse((home / '.credentials.json').exists())
        self.assertEqual((self.source / '.credentials.json').read_bytes(), credential_bytes)
        self.assertEqual(json.loads((home / 'worker-home.json').read_text())[
                         'claude_secure_storage_dir'], str(self.source.resolve()))
        self.assertEqual((home / 'hooks/user-guard.py').resolve(), (self.source / 'hooks/user-guard.py').resolve())
        self.assertEqual(json.loads((home / '.claude.json').read_text()), {'oauthAccount': account})
        self.assertIn('--strict-mcp-config', claude_worker_arguments(env))
        self.assertNotIn('--bare', claude_worker_arguments(env))

    def test_claude_oauth_account_projection_uses_native_global_config_path(self):
        cli_home = self.base / 'cli-home'
        user_config = cli_home / '.claude'
        user_config.mkdir(parents=True)
        root_account = {'accountUuid': 'synthetic-home-account'}
        wrong_nested_account = {'accountUuid': 'synthetic-nested-account'}
        (cli_home / '.claude.json').write_text(json.dumps({
            'oauthAccount': root_account, 'mcpServers': {'private': {}}}))
        (user_config / '.claude.json').write_text(json.dumps({
            'oauthAccount': wrong_nested_account}))

        cases = (
            ('unset', {}, root_account),
            ('empty', {'CLAUDE_CONFIG_DIR': ''}, root_account),
        )
        for label, override, expected in cases:
            with self.subTest(config_dir=label):
                destination = self.base / f'account-projection-{label}'
                self.assertFalse(destination.exists())
                values = prepare_worker_home(
                    ROOT, 'claude', 'stage', 'account-projection-' + label,
                    env={**self.env, 'HOME': str(cli_home), **override},
                    destination=destination)
                self.assertEqual(json.loads(
                    (Path(values['CLAUDE_CONFIG_DIR']) / '.claude.json').read_text()),
                    {'oauthAccount': expected})

        empty_cwd = self.base / 'empty-config-cwd'
        empty_cwd.mkdir()
        empty_legacy_account = {'accountUuid': 'synthetic-empty-legacy-account'}
        (empty_cwd / '.config.json').write_text(json.dumps({
            'oauthAccount': empty_legacy_account}))
        empty_legacy_destination = self.base / 'account-projection-empty-legacy'
        with patch('pathlib.Path.cwd', return_value=empty_cwd):
            values = prepare_worker_home(
                ROOT, 'claude', 'stage', 'account-projection-empty-legacy',
                env={**self.env, 'HOME': str(cli_home), 'CLAUDE_CONFIG_DIR': ''},
                destination=empty_legacy_destination)
        self.assertEqual(json.loads(
            (Path(values['CLAUDE_CONFIG_DIR']) / '.claude.json').read_text()),
            {'oauthAccount': empty_legacy_account})

        custom = self.base / 'custom-config'
        custom.mkdir()
        custom_account = {'accountUuid': 'synthetic-custom-account'}
        (custom / '.claude.json').write_text(json.dumps({
            'oauthAccount': custom_account, 'mcpServers': {'private': {}}}))
        destination = self.base / 'account-projection-custom'
        self.assertFalse(destination.exists())
        values = prepare_worker_home(
            ROOT, 'claude', 'stage', 'account-projection-custom',
            env={**self.env, 'HOME': str(cli_home), 'CLAUDE_CONFIG_DIR': str(custom)},
            destination=destination)
        self.assertEqual(json.loads(
            (Path(values['CLAUDE_CONFIG_DIR']) / '.claude.json').read_text()),
            {'oauthAccount': custom_account})

        legacy_account = {'accountUuid': 'synthetic-custom-legacy-account'}
        (custom / '.config.json').write_text(json.dumps({
            'oauthAccount': legacy_account}))
        destination = self.base / 'account-projection-custom-legacy'
        values = prepare_worker_home(
            ROOT, 'claude', 'stage', 'account-projection-custom-legacy',
            env={**self.env, 'HOME': str(cli_home), 'CLAUDE_CONFIG_DIR': str(custom)},
            destination=destination)
        self.assertEqual(json.loads(
            (Path(values['CLAUDE_CONFIG_DIR']) / '.claude.json').read_text()),
            {'oauthAccount': legacy_account})

    def test_claude_unreadable_global_config_leaves_the_launch_unscoped(self):
        cli_home = self.base / 'rewriting-home'
        (cli_home / '.claude').mkdir(parents=True)
        for label, text in (('torn', '{"oauthAccount": '), ('non-object', '[]')):
            with self.subTest(config=label):
                (cli_home / '.claude.json').write_text(text)
                values = prepare_worker_home(
                    ROOT, 'claude', 'owner', 'unreadable-config-' + label,
                    env={**self.env, 'HOME': str(cli_home)})
                self.assertFalse((Path(values['CLAUDE_CONFIG_DIR']) / '.claude.json').exists())

    def test_claude_binding_covers_worker_types_and_cross_harness_descendants(self):
        source = self.base / 'main-claude-config'
        source.mkdir()
        (source / '.credentials.json').write_text('{"synthetic":"main"}')
        (source / 'settings.json').write_text(json.dumps({
            'permissions': {'deny': ['Read(secret)']}, 'env': {'USER_GUARD': '1'}}))
        env = {**self.env, 'HOME': str(self.base / 'user-home'),
               'CLAUDE_CONFIG_DIR': str(source)}
        expected = str(source.resolve())
        first = None
        for worker_type in ('owner', 'stage', 'review', 'support', 'frame'):
            with self.subTest(worker_type=worker_type):
                values = prepare_worker_home(
                    ROOT, 'claude', worker_type, 'all-types-' + worker_type, env=env)
                self.assertEqual(values[SECURE_STORAGE_ENV], expected)
                worker_config = Path(values['CLAUDE_CONFIG_DIR'])
                self.assertFalse((worker_config / '.credentials.json').exists())
                self.assertEqual(json.loads((worker_config / 'settings.json').read_text())[
                                 'permissions'], {'deny': ['Read(secret)']})
                if worker_type == 'owner':
                    first = values

        codex_source = self.base / 'cross-codex-config'
        codex_source.mkdir()
        for name, content in (('auth.json', '{}'), ('config.toml', '')):
            (codex_source / name).write_text(content)
        codex_before = {name: (codex_source / name).read_bytes()
                        for name in ('auth.json', 'config.toml')}
        codex_parent = {**env, **first, 'CODEX_HOME': str(codex_source)}
        codex = prepare_worker_home(ROOT, 'codex', 'review', 'claude-through-codex',
                                    env=codex_parent)
        self.assertNotIn(SECURE_STORAGE_ENV, codex)
        through_codex = {**codex_parent, **codex}
        nested_codex = prepare_worker_home(
            ROOT, 'claude', 'stage', 'nested-after-codex', env=through_codex)
        self.assertEqual(nested_codex[SECURE_STORAGE_ENV], expected)
        self.assertEqual({name: (codex_source / name).read_bytes()
                          for name in ('auth.json', 'config.toml')}, codex_before)

        opencode_source = self.base / 'cross-opencode-config'
        opencode_source.mkdir()
        opencode_config = opencode_source / 'opencode.json'
        opencode_config.write_text(json.dumps({'provider': {'fixture': {'name': 'test'}}}))
        opencode_before = opencode_config.read_bytes()
        opencode_parent = {**env, **first,
                           'OPENCODE_CONFIG_DIR': str(opencode_source),
                           'XDG_CONFIG_HOME': str(self.base / 'xdg-config')}
        opencode = prepare_worker_home(
            ROOT, 'opencode', 'review', 'claude-through-opencode', env=opencode_parent)
        self.assertNotIn(SECURE_STORAGE_ENV, opencode)
        through_opencode = {**opencode_parent, **opencode}
        nested_opencode = prepare_worker_home(
            ROOT, 'claude', 'stage', 'nested-after-opencode', env=through_opencode)
        self.assertEqual(nested_opencode[SECURE_STORAGE_ENV], expected)
        self.assertEqual(opencode_config.read_bytes(), opencode_before)

    def test_claude_secure_storage_override_values_and_config_fallback_are_preserved(self):
        home = self.base / 'worker-home'
        config = self.base / 'main-config'
        env = {**self.env, 'HOME': str(self.base / 'user-home'),
               'CLAUDE_CONFIG_DIR': str(config)}
        for value, expected in (
                ('', ''),
                (str(self.base / 'custom-auth'), str(self.base / 'custom-auth')),
                ('relative-auth', str((Path.cwd() / 'relative-auth').resolve()))):
            with self.subTest(value=value):
                result = prepare_worker_home(
                    ROOT, 'claude', 'stage', 'override-' + repr(value),
                    env={**env, SECURE_STORAGE_ENV: value},
                    destination=home / ('empty' if value == '' else str(abs(hash(value)))))
                self.assertEqual(result[SECURE_STORAGE_ENV], expected)

        default_env = {**self.env, 'HOME': str(self.base / 'alternate-home')}
        default_env.pop('CLAUDE_CONFIG_DIR', None)
        default = prepare_worker_home(ROOT, 'claude', 'review', 'default-auth',
                                      env=default_env, destination=home / 'default')
        self.assertEqual(default[SECURE_STORAGE_ENV], '')

        missing_custom = self.base / 'missing-account-config'
        custom = prepare_worker_home(
            ROOT, 'claude', 'support', 'missing-custom-auth',
            env={**self.env, 'HOME': str(self.base / 'alternate-home'),
                 'CLAUDE_CONFIG_DIR': str(missing_custom)},
            destination=home / 'missing-custom')
        self.assertEqual(custom[SECURE_STORAGE_ENV], str(missing_custom.resolve()))
        self.assertFalse((Path(custom['CLAUDE_CONFIG_DIR']) / '.credentials.json').exists())

    def test_claude_settings_env_precedence_is_normalized_and_nested_store_is_stable(self):
        config = self.base / 'main-config'
        config.mkdir()
        (config / 'settings.json').write_text(json.dumps({
            'env': {SECURE_STORAGE_ENV: 'settings-auth', 'USER_GUARD': '1'},
            'permissions': {'deny': ['Read(secret)']}}))
        env = {**self.env, 'CLAUDE_CONFIG_DIR': str(config),
               SECURE_STORAGE_ENV: str(self.base / 'inherited-auth')}
        first = prepare_worker_home(ROOT, 'claude', 'stage', 'settings-precedence', env=env)
        binding = str((Path.cwd() / 'settings-auth').resolve())
        self.assertEqual(first[SECURE_STORAGE_ENV], binding)
        first_home = Path(first['CLAUDE_CONFIG_DIR'])
        generated = json.loads((first_home / 'settings.json').read_text())
        self.assertEqual(generated['env'][SECURE_STORAGE_ENV], binding)
        self.assertEqual(generated['env']['USER_GUARD'], '1')
        self.assertEqual(generated['permissions'], {'deny': ['Read(secret)']})

        child = prepare_worker_home(
            ROOT, 'claude', 'review', 'nested-settings-store', env={**env, **first})
        self.assertNotEqual(child['CLAUDE_CONFIG_DIR'], first['CLAUDE_CONFIG_DIR'])
        self.assertEqual(child[SECURE_STORAGE_ENV], binding)

    def test_claude_legacy_links_and_split_candidates_use_main_store(self):
        canonical = self.base / 'legacy-canonical'
        canonical.mkdir()
        (canonical / '.credentials.json').write_text('{"synthetic":"canonical"}')
        legacy_link_home = self.base / 'legacy-link-home'
        legacy_link_home.mkdir()
        (legacy_link_home / 'worker-home.json').write_text('{"harness":"claude"}')
        (legacy_link_home / '.credentials.json').symlink_to(canonical / '.credentials.json')
        linked_destination = self.base / 'new-linked-home'
        linked_destination.mkdir()
        (linked_destination / '.credentials.json').symlink_to(
            canonical / '.credentials.json')
        linked = prepare_worker_home(
            ROOT, 'claude', 'stage', 'legacy-intact-link',
            env={**self.env, 'CLAUDE_CONFIG_DIR': str(legacy_link_home)},
            destination=linked_destination)
        self.assertEqual(linked[SECURE_STORAGE_ENV], str(canonical.resolve()))
        self.assertTrue((legacy_link_home / '.credentials.json').is_symlink())
        self.assertFalse((linked_destination / '.credentials.json').exists())

        legacy_regular_home = self.base / 'legacy-regular-home'
        legacy_regular_home.mkdir()
        candidate = legacy_regular_home / '.credentials.json'
        candidate.write_bytes(b'{"synthetic":"opaque-candidate"}')
        before = candidate.read_bytes()
        (legacy_regular_home / 'worker-home.json').write_text('{"harness":"claude"}')
        (legacy_regular_home / 'settings.json').write_text(json.dumps({
            'permissions': {'deny': ['Read(secret)']},
            'env': {'USER_GUARD': '1'}}))
        legacy_main_home = self.base / 'legacy-main-home'
        (legacy_main_home / '.claude').mkdir(parents=True)
        main_candidate = legacy_main_home / '.claude' / '.credentials.json'
        main_candidate.write_bytes(b'{"synthetic":"main-current"}')
        main_before = main_candidate.read_bytes()
        regular_destination = self.base / 'new-regular-home'
        self.assertFalse(regular_destination.exists())
        env = {**self.env, 'HOME': str(legacy_main_home),
               'CLAUDE_CONFIG_DIR': str(legacy_regular_home)}
        regular = prepare_worker_home(
            ROOT, 'claude', 'stage', 'legacy-opaque-candidate', env=env,
            destination=regular_destination)
        # The split file is not this child's auth source: without a paired
        # legacy launch environment, the builder's HOME/.claude main is used.
        self.assertEqual(regular[SECURE_STORAGE_ENV], '')
        self.assertNotEqual(regular['CLAUDE_CONFIG_DIR'], str(legacy_regular_home))
        self.assertFalse((regular_destination / '.credentials.json').exists())
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(main_candidate.read_bytes(), main_before)
        self.assertEqual(json.loads((regular_destination / 'settings.json').read_text())[
                         'permissions'], {'deny': ['Read(secret)']})
        self.assertEqual(json.loads((regular_destination / 'worker-home.json').read_text())[
                         'claude_secure_storage_dir'], '')

        # A legacy parent's own launch environment names its source main.
        custom_main = self.base / 'legacy-custom-main'
        custom_main.mkdir()
        for label, source, expected in (
                ('default', legacy_main_home / '.claude', ''),
                ('custom', custom_main, str(custom_main.resolve()))):
            with self.subTest(paired_source=label):
                paired = prepare_worker_home(
                    ROOT, 'claude', 'stage', 'legacy-paired-' + label,
                    env={**env, 'HEARTING_WORKER_HOME': str(legacy_regular_home),
                         'HEARTING_MODEL_CONFIG_HOME': str(source)},
                    destination=self.base / ('new-paired-home-' + label))
                self.assertEqual(paired[SECURE_STORAGE_ENV], expected)
                self.assertEqual(candidate.read_bytes(), before)
                self.assertEqual(main_candidate.read_bytes(), main_before)
                self.assertFalse((custom_main / '.credentials.json').exists())

        occupied_destination = self.base / 'occupied-regular-home'
        occupied_destination.mkdir()
        destination_candidate = occupied_destination / '.credentials.json'
        destination_candidate.write_bytes(b'{"synthetic":"destination-candidate"}')
        destination_before = destination_candidate.read_bytes()
        occupied = prepare_worker_home(
            ROOT, 'claude', 'stage', 'legacy-opaque-candidate-occupied', env=env,
            destination=occupied_destination)
        self.assertEqual(occupied[SECURE_STORAGE_ENV], '')
        self.assertEqual(destination_candidate.read_bytes(), destination_before)
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(main_candidate.read_bytes(), main_before)

        nested_destination = self.base / 'nested-regular-home'
        nested = prepare_worker_home(
            ROOT, 'claude', 'review', 'nested-legacy-opaque-candidate',
            env={**env, **regular}, destination=nested_destination)
        self.assertEqual(nested[SECURE_STORAGE_ENV], '')
        self.assertNotEqual(nested['CLAUDE_CONFIG_DIR'], regular['CLAUDE_CONFIG_DIR'])
        self.assertFalse((nested_destination / '.credentials.json').exists())
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(main_candidate.read_bytes(), main_before)

        empty_legacy_home = self.base / 'legacy-without-credentials'
        empty_legacy_home.mkdir()
        (empty_legacy_home / 'worker-home.json').write_text('{"harness":"claude"}')
        empty_destination = self.base / 'new-home-without-credentials'
        empty = prepare_worker_home(
            ROOT, 'claude', 'stage', 'legacy-no-credential',
            env={**env, 'CLAUDE_CONFIG_DIR': str(empty_legacy_home)},
            destination=empty_destination)
        self.assertEqual(empty[SECURE_STORAGE_ENV], '')
        self.assertFalse((empty_legacy_home / '.credentials.json').exists())
        self.assertFalse((empty_destination / '.credentials.json').exists())
        self.assertEqual(main_candidate.read_bytes(), main_before)

    def test_manifest_records_the_identity_retention_needs_to_bind_a_home(self):
        (self.source / 'settings.json').write_text('{}')
        env = {**self.env, 'CLAUDE_CONFIG_DIR': str(self.source)}
        built = prepare_worker_home(ROOT, 'claude', 'stage', 'att-manifest', env=env)
        home = Path(built['CLAUDE_CONFIG_DIR'])
        manifest = json.loads((home / 'worker-home.json').read_text())
        key = worker_runtime_home._builder.worker_home_key(str(ROOT.resolve()), 'claude', 'stage', 'att-manifest')
        self.assertEqual(home.name, key)
        self.assertEqual((manifest['schema'], manifest['identity'], manifest['key'], manifest['destination']),
                         (2, 'att-manifest', key, False))
        self.assertEqual({'harness', 'worker_type', 'source', 'profile'} - set(manifest), set())
        shared = self.base / 'shared-home'
        built = prepare_worker_home(ROOT, 'claude', 'stage', 'att-shared', env=env, destination=shared)
        manifest = json.loads((shared / 'worker-home.json').read_text())
        self.assertTrue(manifest['destination'])
        self.assertEqual(manifest['identity'], 'att-shared')

    def test_codex_hook_alias_preserves_user_decisions_and_rereads_them_on_resume(self):
        hooks = self.source / 'hooks.json'
        hooks.write_text('{"hooks":{}}')
        config = self.source / 'config.toml'
        key = str(hooks) + ':pre_tool_use:0:0'
        config.write_text('[hooks.state.' + json.dumps(key) + ']\ntrusted_hash="sha256:user-approved"\nenabled=false\n')
        self.env['CODEX_HOME'] = str(self.source)
        env = prepare_worker_home(ROOT, 'codex', 'owner', 'trusted-guards', env=self.env)
        before = config.read_bytes()
        state = next(s for s in codex_worker_arguments(env) if s.startswith('hooks.state='))
        self.assertIn(str(Path(env['CODEX_HOME']) / 'hooks.json') + ':pre_tool_use:0:0', state)
        self.assertIn('sha256:user-approved', state)
        self.assertIn('"enabled"=false', state)
        self.assertEqual(config.read_bytes(), before)
        # A resumed supervisor constructs its command again: revocation must
        # not be restored from a previously computed launch environment.
        config.write_text('[hooks.state.' + json.dumps(key) + ']\nenabled=false\n')
        state = next(s for s in codex_worker_arguments(env) if s.startswith('hooks.state='))
        self.assertNotIn('trusted_hash', state)
        self.assertIn('"enabled"=false', state)

    def test_opencode_keeps_deny_and_guard_plugins_excludes_main_and_skills(self):
        config = {'permission': {'bash': {'danger *': 'deny'}},
                  'instructions': [str(ROOT / 'adapters/opencode/AGENTS.md')],
                  'skills': {'paths': ['full-catalog']}, 'provider': {'internal': {'name': 'test'}}}
        (self.source / 'opencode.json').write_text(json.dumps(config))
        (self.source / 'plugins').mkdir()
        (self.source / 'plugins/guard.js').write_text('guard')
        self.env['OPENCODE_CONFIG_DIR'] = str(self.source)
        self.env['XDG_CACHE_HOME'] = str(self.source / 'cache')
        env = prepare_worker_home(ROOT, 'opencode', 'review', 'review', env=self.env)
        self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
        self.assertEqual(env['HEARTING_OPENCODE_MODELS_CACHE'], str(self.source / 'cache' / 'opencode'))
        self.assertEqual(env['HEARTING_MODEL_CONFIG_HOME'], str(self.source))
        home = Path(env['OPENCODE_CONFIG_DIR'])
        actual = json.loads((home / 'opencode.json').read_text())
        self.assertEqual(actual['permission']['bash'], config['permission']['bash'])
        self.assertEqual(actual['provider'], config['provider'])
        self.assertEqual(actual['instructions'], [])
        self.assertEqual(actual['permission']['skill'], {'*': 'deny'})
        self.assertEqual((home / 'plugins').resolve(), (self.source / 'plugins').resolve())
        self.assertEqual(env['OPENCODE_DISABLE_CLAUDE_CODE_PROMPT'], '1')

    def test_opencode_all_worker_types_override_snapshot_without_changing_user_config(self):
        config = {'snapshot': True, 'permission': {'bash': {'secret *': 'deny'}}}
        source = self.source / 'opencode.json'
        source.write_text(json.dumps(config))
        self.env.update(OPENCODE_CONFIG_DIR=str(self.source),
                        OPENCODE_CONFIG_CONTENT=json.dumps({'snapshot': True, 'model': 'test/model'}))
        before = source.read_bytes()
        for typ in ('owner', 'stage', 'review', 'support', 'frame'):
            env = prepare_worker_home(ROOT, 'opencode', typ, 'snapshot-' + typ, env=self.env)
            self.assertNotIn(SECURE_STORAGE_ENV, env)
            actual = json.loads((Path(env['OPENCODE_CONFIG_DIR']) / 'opencode.json').read_text())
            self.assertFalse(actual['snapshot'])
            inline = json.loads(env['OPENCODE_CONFIG_CONTENT'])
            self.assertFalse(inline['snapshot'])
            self.assertEqual(inline['model'], 'test/model')
            self.assertEqual(actual['permission']['bash'], config['permission']['bash'])
        self.assertEqual(source.read_bytes(), before)

    def test_profile_specialization_uses_same_guard_preserving_home(self):
        (self.source / 'settings.json').write_text('{"permissions":{"deny":["Read(secret)"]}}')
        self.env['CLAUDE_CONFIG_DIR'] = str(self.source)
        env = prepare_worker_home(ROOT, 'claude', 'stage', 'specialized', env=self.env, profile='code-report')
        home = Path(env['CLAUDE_CONFIG_DIR'])
        self.assertIn('profiles/code-report.yaml', (home / 'CLAUDE.md').read_text())
        self.assertEqual(json.loads((home / 'settings.json').read_text())['permissions']['deny'], ['Read(secret)'])

    def test_opencode_isolation_keeps_the_user_inventory_through_descendants(self):
        config = self.base / 'user-config'
        inventory = config / 'hearting/compute-hosts.yaml'
        inventory.parent.mkdir(parents=True)
        inventory.write_text(f'schema_version: 1\nrun_root: {self.base}/runs\nhosts:\n  fixture:\n    ssh_host: local\n')
        before = inventory.read_bytes()
        env = {**self.env, 'HOME': str(self.source), 'XDG_CONFIG_HOME': str(config)}
        owner = prepare_worker_home(ROOT, 'opencode', 'owner', 'lab-owner', env=env)
        self.assertNotEqual(owner['XDG_CONFIG_HOME'], str(config))
        self.assertEqual(owner['COMPUTE_HOSTS_CONFIG'], str(inventory))
        child = prepare_worker_home(ROOT, 'opencode', 'review', 'lab-child', env={**env, **owner})
        for values in (owner, child):
            # Run the real inventory loader in the actual masked environment,
            # rather than only checking a projected environment string.
            result = subprocess.run([sys.executable, '-c',
                                     'import importlib.util; '
                                     f's=importlib.util.spec_from_file_location("compute", {str(ROOT / "utilities/compute-hosts.py")!r}); '
                                     'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                                     'print(m.config_path()); print(m.load_config()["run_root"])'],
                                    env={**os.environ, **env, **values},
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(inventory), result.stdout)
            self.assertFalse((Path(values['XDG_CONFIG_HOME']) / 'hearting').exists())
        self.assertEqual(inventory.read_bytes(), before)

    def test_opencode_preserves_an_explicit_inventory_location(self):
        inventory = self.base / 'operator/inventory.yaml'
        values = prepare_worker_home(ROOT, 'opencode', 'owner', 'explicit-inventory',
                                     env={**self.env, 'COMPUTE_HOSTS_CONFIG': str(inventory)})
        self.assertEqual(values['COMPUTE_HOSTS_CONFIG'], str(inventory))

    def test_codex_without_optional_user_hook_file_still_builds_minimal_home(self):
        minimal = self.base / 'minimal-source'
        (minimal / 'profiles/templates').mkdir(parents=True)
        (minimal / 'profiles/templates/bootstrap-codex.md').write_text('minimal worker attach')
        self.env['CODEX_HOME'] = str(self.source)
        env = prepare_worker_home(minimal, 'codex', 'review', 'no-user-hooks', env=self.env)
        self.assertEqual((Path(env['CODEX_HOME']) / 'AGENTS.md').read_text(), 'minimal worker attach')
        self.assertFalse((Path(env['CODEX_HOME']) / 'hooks.json').exists())

    def test_liveness_uses_exact_worker_store_before_legacy_profile_or_default(self):
        path = ROOT / 'adapters/codex/bin/dispatch-liveness.py'
        spec = importlib.util.spec_from_file_location('worker_home_liveness', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        actual = self.base / 'actual-worker'
        pipe = 'profile=legacy-profile,runtime_home=' + str(actual)
        default = self.base / 'other-session/sessions'
        self.assertEqual(mod.sessions_dir_for(pipe, 'job', ROOT, default), actual / 'sessions')
        self.assertEqual(mod.sessions_dirs_for(pipe, 'job', ROOT, default, str(ROOT)), [actual / 'sessions'])

    def test_concurrent_home_builders_reuse_the_same_link_in_every_harness(self):
        # Force both creators past the missing-path observation before either
        # symlink is published, rather than relying on a lucky thread schedule.
        original = Path.symlink_to
        for harness, filename, variable in (
                ('codex', 'AGENTS.md', 'CODEX_HOME'),
                ('claude', 'CLAUDE.md', 'CLAUDE_CONFIG_DIR'),
                ('opencode', 'AGENTS.md', 'OPENCODE_CONFIG_DIR')):
            with self.subTest(harness=harness):
                barrier = threading.Barrier(2)
                env = {**self.env, variable: str(self.source)}
                def synchronized(path, *args, **kwargs):
                    if path.name == filename:
                        barrier.wait(timeout=5)
                    return original(path, *args, **kwargs)
                def build():
                    return prepare_worker_home(ROOT, harness, 'stage', harness, env=env)
                with patch.object(Path, 'symlink_to', synchronized), ThreadPoolExecutor(2) as pool:
                    futures = [pool.submit(build) for _ in range(2)]
                    results = [future.result(timeout=10) for future in futures]
                self.assertEqual(results[0], results[1])
                home = Path(results[0][variable])
                self.assertEqual((home / filename).read_bytes(),
                                 (ROOT / f'profiles/templates/bootstrap-{harness}.md').read_bytes())
                self.assertEqual(json.loads((home / 'worker-home.json').read_text())['harness'], harness)

    def test_generated_files_and_profile_refresh_publish_complete_bytes(self):
        settings = {'permissions': {'deny': ['Read(secret)']}}
        (self.source / 'settings.json').write_text(json.dumps(settings))
        env = {**self.env, 'CLAUDE_CONFIG_DIR': str(self.source)}
        first = prepare_worker_home(ROOT, 'claude', 'stage', 'profile-refresh',
                                    env=env, profile='code-report')
        home = Path(first['CLAUDE_CONFIG_DIR'])
        before = {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()}
        bootstrap_inode = (home / 'CLAUDE.md').stat().st_ino
        original = os.replace
        publications = []
        def checked_replace(source, destination):
            destination = Path(destination)
            self.assertEqual(destination.read_bytes(), before[destination.name])
            if destination.suffix == '.json':
                json.loads(Path(source).read_text())
            self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
            publications.append(destination.name)
            return original(source, destination)
        with patch.object(worker_runtime_home._builder.os, 'replace', checked_replace), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(prepare_worker_home, ROOT, 'claude', 'stage',
                                   'profile-refresh', env=env, profile='code-report') for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], [first, first])
        self.assertEqual(publications.count('CLAUDE.md'), 0)
        self.assertEqual((home / 'CLAUDE.md').stat().st_ino, bootstrap_inode)
        self.assertEqual(publications.count('settings.json'), 2)
        self.assertEqual(publications.count('worker-home.json'), 2)

    def test_concurrent_profile_bootstrap_publication_is_idempotent(self):
        builder = worker_runtime_home._builder
        env = {**self.env, 'CLAUDE_CONFIG_DIR': str(self.source)}
        original = os.link
        barrier = threading.Barrier(2)
        def synchronized(source, destination, *args, **kwargs):
            if Path(destination).name == 'CLAUDE.md':
                barrier.wait(timeout=5)
            return original(source, destination, *args, **kwargs)
        with patch.object(builder.os, 'link', synchronized), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(prepare_worker_home, ROOT, 'claude', 'stage',
                                   'new-profile', env=env, profile='code-report') for _ in range(2)]
            results = [f.result(timeout=10) for f in futures]
        self.assertEqual(results[0], results[1])
        self.assertIn('profiles/code-report.yaml',
                      (Path(results[0]['CLAUDE_CONFIG_DIR']) / 'CLAUDE.md').read_text())

    def test_profile_preserves_a_foreign_regular_bootstrap(self):
        home = self.base / 'foreign-home'
        home.mkdir()
        bootstrap = home / 'CLAUDE.md'
        bootstrap.write_text('foreign instructions')
        with self.assertRaisesRegex(ValueError, 'worker home collision'):
            prepare_worker_home(ROOT, 'claude', 'stage', 'foreign-profile',
                                env={**self.env, 'CLAUDE_CONFIG_DIR': str(self.source)},
                                destination=home, profile='code-report')
        self.assertEqual(bootstrap.read_text(), 'foreign instructions')

    def test_link_refresh_is_atomic_and_foreign_file_is_preserved(self):
        builder = worker_runtime_home._builder
        old, new, target = self.base / 'old', self.base / 'new', self.base / 'link'
        old.write_text('old'); new.write_text('new')
        builder.publish_symlink(old, target)
        original = os.replace
        def checked_replace(source, destination):
            self.assertEqual(target.read_text(), 'old')
            return original(source, destination)
        with patch.object(builder.os, 'replace', checked_replace):
            builder.publish_symlink(new, target)
        self.assertEqual(target.read_text(), 'new')
        with patch.object(Path, 'unlink', side_effect=AssertionError('matching link removed')):
            builder.publish_symlink(new, target)
        target.unlink(); target.write_text('foreign')
        with self.assertRaisesRegex(ValueError, 'worker home collision'):
            builder.publish_symlink(new, target)
        self.assertEqual(target.read_text(), 'foreign')


if __name__ == '__main__':
    unittest.main()
