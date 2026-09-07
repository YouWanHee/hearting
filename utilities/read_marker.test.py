#!/usr/bin/env python3
"""Portable read-marker writers: CLI failures versus non-blocking read hooks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReadMarkerStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.art = self.repo / '.agent_reports'
        (self.art / 'spec').mkdir(parents=True)
        (self.repo / 'core').mkdir()
        self.spec = self.art / 'spec/prd.md'
        self.core = self.repo / 'core/CORE.md'
        self.spec.write_text('spec fixture\n')
        self.core.write_text('core fixture\n')
        self.home = Path(self.tmp.name) / 'marker-home'
        self.home.mkdir()
        self.env = {'PATH': os.environ['PATH'], 'HOME': self.tmp.name,
                    'AGENT_HOME': str(self.home),
                    'AGENT_ARTIFACT_ROOT': str(self.art),
                    'PYTHONDONTWRITEBYTECODE': '1'}

    def readonly(self, cmd):
        if not shutil.which('bwrap'):
            self.skipTest('bwrap required for a real read-only mount')
        return ['bwrap', '--ro-bind', '/', '/', '--dev', '/dev',
                '--die-with-parent', '--', *cmd]

    def run_marker(self, kind, *, hook=False, readonly=False, wrapper=False):
        directory = 'adapters/claude/hooks' if wrapper else 'hooks'
        script = ROOT / directory / f'{kind}-read-marker.sh'
        env = dict(self.env)
        if wrapper:
            env['AGENT_HOME'] = str(ROOT)
        file = self.spec if kind == 'spec' else self.core
        cmd = [str(script)]
        data = None
        if hook:
            data = json.dumps({'tool_input': {'file_path': str(file)},
                               'session_id': 'marker-fixture'})
        else:
            cmd += ['--file', str(file), '--session', 'marker-fixture']
        if readonly:
            cmd = self.readonly(cmd)
        return subprocess.run(cmd, input=data, env=env, text=True, capture_output=True)

    def test_cli_readonly_failure_is_nonzero(self):
        for kind in ('spec', 'core'):
            with self.subTest(kind=kind):
                result = self.run_marker(kind, readonly=True)
                self.assertIn('Read-only file system', result.stderr)
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_hook_readonly_failure_is_nonblocking_and_diagnosed(self):
        for kind in ('spec', 'core'):
            with self.subTest(kind=kind):
                result = self.run_marker(kind, hook=True, readonly=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'{kind}-read-marker: marker-write-failed', result.stderr)
                self.assertEqual(result.stdout, '')

    def test_existing_directory_does_not_hide_marker_file_failure(self):
        for kind in ('spec', 'core'):
            (self.home / f'.{kind}-grounding').mkdir()
            for hook in (False, True):
                with self.subTest(kind=kind, hook=hook):
                    result = self.run_marker(kind, hook=hook, readonly=True)
                    self.assertIn('Read-only file system', result.stderr)
                    self.assertIn('marker-write-failed (read not recorded)', result.stderr)
                    self.assertEqual(result.returncode == 0, hook, result.stderr)

    def test_exact_spec_directory_grant_allows_marker_but_not_home_write(self):
        marker_dir = self.home / '.spec-grounding'
        marker_dir.mkdir()
        cmd = self.readonly(['sh', '-c',
            '"$1" --file "$2" --session marker-fixture || exit $?; '
            'if touch "$3/unrelated"; then exit 99; fi',
            'marker-check', str(ROOT/'hooks/spec-read-marker.sh'),
            str(self.spec), str(self.home)])
        separator = cmd.index('--')
        cmd[separator:separator] = ['--bind', str(marker_dir), str(marker_dir)]
        result = subprocess.run(cmd, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Read-only file system', result.stderr)
        markers = list(marker_dir.iterdir())
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].read_text().strip(), str(int(self.spec.stat().st_mtime)))
        self.assertFalse((self.home/'unrelated').exists())

    def test_success_records_real_read_and_unrelated_read_is_noop(self):
        for kind, source in (('spec', self.spec), ('core', self.core)):
            result = self.run_marker(kind)
            self.assertEqual(result.returncode, 0, result.stderr)
            markers = list((self.home / f'.{kind}-grounding').iterdir())
            self.assertEqual(len(markers), 1)
            self.assertIn(str(int(source.stat().st_mtime)), markers[0].read_text())
        result = subprocess.run([str(ROOT/'hooks/spec-read-marker.sh'), '--file', str(self.core)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list((self.home/'.spec-grounding').iterdir())), 1)

    def test_preflights_preserve_core_and_spec_cli_failures(self):
        env = dict(self.env, AGENT_HOME=str(ROOT))
        for adapter in ('codex', 'opencode'):
            for file in (self.core, self.spec):
                with self.subTest(adapter=adapter, file=file.name):
                    cmd = self.readonly([str(ROOT/f'adapters/{adapter}/bin/preflight.sh'),
                                         'read', str(file), 'marker-fixture'])
                    result = subprocess.run(cmd, env=env, text=True, capture_output=True)
                    self.assertIn('Read-only file system', result.stderr)
                    self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_claude_delegation_keeps_cli_and_hook_status_distinct(self):
        for hook in (False, True):
            with self.subTest(hook=hook):
                result = self.run_marker('core', readonly=True, wrapper=True, hook=hook)
                self.assertIn('Read-only file system', result.stderr)
                if hook:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('core-read-marker: marker-write-failed', result.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
