#!/usr/bin/env sh
# Exact status, path and diagnostic parity over synthetic filesystem layouts.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
. "$ROOT/tools/memory/test-isolation.sh"
T=$(mktemp -d "${TMPDIR:-/tmp}/mem-store-parity.XXXXXX")
trap 'rm -rf "$T"' EXIT HUP INT TERM
hearting_test_isolate "$T"
python3 - "$ROOT" "$T" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT, FIXTURE = map(Path, sys.argv[1:])

class StoreParity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=FIXTURE)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.data = self.base / 'data'
        self.canonical = self.data / 'hearting/memory'
        self.legacy = self.home / '.claude/memory'
        self.agent = self.base / 'agent'
        self.managed = self.data / 'hearting/current/memory'
        self.env = {
            'HOME': str(self.home), 'PATH': os.defpath,
            'XDG_DATA_HOME': str(self.data),
            'XDG_CONFIG_HOME': str(self.base / 'config'),
            'XDG_STATE_HOME': str(self.base / 'state'),
            'MEM_WRITE_EVENTS': str(self.base / 'state/write.jsonl'),
            'MEM_RECALL_EVENTS': str(self.base / 'state/recall.jsonl'),
            'MEM_RECALL_RECEIPTS': str(self.base / 'state/receipts'),
            'MEM_PROJECTS': str(self.base / 'projects'),
        }

    def populate(self, store):
        store.mkdir(parents=True, exist_ok=True)
        (store / 'memory.db').write_bytes(b'synthetic path-probe fixture; never opened')

    def check(self, expected, *, rc=0, error='', **env):
        before = self.snapshot()
        for name, argv in [
            ('python', [sys.executable, str(ROOT / 'tools/memory/store_resolve.py')]),
            ('shell', ['sh', str(ROOT / 'utilities/memory-store.sh')]),
        ]:
            with self.subTest(implementation=name):
                got = subprocess.run(argv, env={**self.env, **env}, text=True,
                                     capture_output=True, timeout=5)
                self.assertEqual(got.returncode, rc, got.stderr)
                self.assertEqual(got.stdout, str(expected) + '\n' if rc == 0 else '')
                self.assertEqual(got.stderr, error + '\n' if error else '')
        self.assertEqual(self.snapshot(), before, 'resolver mutated the fixture')

    def snapshot(self):
        # lstat only; never follows a self-loop fixture or opens a database.
        return sorted((str(p.relative_to(self.base)), p.lstat().st_mode,
                       p.lstat().st_size, p.lstat().st_mtime_ns)
                      for p in self.base.rglob('*'))

    def conflict(self, *stores, **env):
        self.check('', rc=3, error='memory store resolution error: multiple memory databases found: '
                   + ', '.join(map(str, stores)) + '; set MEM_STORE to one of them', **env)

    def error(self, store, **env):
        self.check('', rc=3, error=f'memory store resolution error: {store}', **env)

    def test_explicit_nonexistent(self):
        self.check(self.base / 'nonexistent', MEM_STORE=str(self.base / 'nonexistent'))

    def test_explicit_relative_and_unusual_characters(self):
        for path in ['relative/store path', str(self.base / 'pipe|colon:tab\tline\nstore')]:
            self.check(path, MEM_STORE=path)

    def test_empty_override_is_unset(self):
        self.check(self.canonical, MEM_STORE='')

    def test_bundle_empty_legacy_populated(self):
        (self.agent / 'memory').mkdir(parents=True)
        self.populate(self.legacy)
        self.check(self.legacy, AGENT_HOME=str(self.agent))

    def test_empty_early_populated_late(self):
        (self.agent / 'memory').mkdir(parents=True)
        self.populate(self.canonical)
        self.check(self.canonical, AGENT_HOME=str(self.agent))

    def test_conflict(self):
        first = self.home / 'hearting/memory'
        self.populate(first); self.populate(self.legacy)
        self.conflict(first, self.legacy)

    def test_directory_symlink_alias(self):
        self.populate(self.legacy)
        self.canonical.parent.mkdir(parents=True)
        self.canonical.symlink_to(self.legacy, target_is_directory=True)
        self.check(self.legacy)

    def test_file_symlink_alias(self):
        first = self.agent / 'memory'
        self.populate(first)
        self.legacy.mkdir(parents=True)
        (self.legacy / 'memory.db').symlink_to(first / 'memory.db')
        self.check(first, AGENT_HOME=str(self.agent))

    def test_relative_file_symlink_alias(self):
        first = self.agent / 'memory'
        self.populate(first)
        self.legacy.mkdir(parents=True)
        target = os.path.relpath(first / 'memory.db', self.legacy)
        (self.legacy / 'memory.db').symlink_to(target)
        self.check(first, AGENT_HOME=str(self.agent))

    def test_pipe_and_whitespace_candidate(self):
        unusual = self.base / 'agent|part :\tline\n'
        self.populate(unusual / 'memory')
        self.check(unusual / 'memory', AGENT_HOME=str(unusual))

    def test_pipe_candidates_still_conflict(self):
        unusual = self.base / 'agent|part'
        self.populate(unusual / 'memory'); self.populate(self.legacy)
        self.conflict(unusual / 'memory', self.legacy, AGENT_HOME=str(unusual))

    def test_no_database_no_compat_directory(self):
        self.check(self.canonical)

    def test_no_database_existing_compat_directory(self):
        self.legacy.mkdir(parents=True)
        self.check(self.legacy)

    def test_empty_managed_current_excluded(self):
        self.managed.mkdir(parents=True)
        self.check(self.canonical)

    def test_populated_managed_current(self):
        self.populate(self.managed)
        self.check(self.managed)

    def test_managed_current_conflict(self):
        self.populate(self.legacy); self.populate(self.managed)
        self.conflict(self.legacy, self.managed)

    def test_each_precedence_source(self):
        for key, value in [('AGENT_HOME', self.agent), ('CLAUDE_HOME', self.base / 'claude')]:
            with self.subTest(source=key):
                self.populate(value / 'memory')
                self.check(value / 'memory', **{key: str(value)})
                (value / 'memory/memory.db').unlink()
        for name in ['hearting', 'agent_setting']:
            store = self.home / name / 'memory'
            self.populate(store); self.check(store)
            (store / 'memory.db').unlink()

    def test_directory_named_memory_db(self):
        (self.legacy / 'memory.db').mkdir(parents=True)
        self.check(self.legacy)

    def test_wrong_type_parent_is_absence(self):
        self.agent.mkdir()
        (self.agent / 'memory').write_text('not a directory')
        self.check(self.canonical, AGENT_HOME=str(self.agent))

    def test_dangling_database_symlink(self):
        self.legacy.mkdir(parents=True)
        (self.legacy / 'memory.db').symlink_to('missing')
        self.check(self.legacy)

    def test_dangling_candidate_directory_is_not_fallback(self):
        self.agent.mkdir()
        (self.agent / 'memory').symlink_to('missing')
        self.check(self.canonical, AGENT_HOME=str(self.agent))

    def test_valid_external_database_symlink(self):
        target = self.base / 'external'
        self.populate(target)
        self.legacy.mkdir(parents=True)
        (self.legacy / 'memory.db').symlink_to(target / 'memory.db')
        self.check(self.legacy)

    def test_self_loop_before_later_database(self):
        first = self.agent / 'memory'
        first.mkdir(parents=True)
        (first / 'memory.db').symlink_to('memory.db')
        self.populate(self.legacy)
        self.error(first, AGENT_HOME=str(self.agent))

    def test_parent_loop_before_later_database(self):
        self.agent.mkdir()
        (self.agent / 'memory').symlink_to('memory')
        self.populate(self.legacy)
        self.error(self.agent / 'memory', AGENT_HOME=str(self.agent))

    def test_two_link_cycle(self):
        first = self.agent / 'memory'
        first.mkdir(parents=True)
        (first / 'memory.db').symlink_to('other')
        (first / 'other').symlink_to('memory.db')
        self.error(first, AGENT_HOME=str(self.agent))

    @unittest.skipIf(os.geteuid() == 0, 'root bypasses filesystem permission denial')
    def test_inaccessible_parent_before_later_database(self):
        self.populate(self.agent / 'memory'); self.populate(self.legacy)
        self.agent.chmod(0)
        try:
            self.error(self.agent / 'memory', AGENT_HOME=str(self.agent))
        finally:
            self.agent.chmod(0o700)

    def test_first_error_wins_over_earlier_population(self):
        self.populate(self.agent / 'memory')
        self.legacy.mkdir(parents=True)
        (self.legacy / 'memory.db').symlink_to('memory.db')
        self.error(self.legacy, AGENT_HOME=str(self.agent))

    def test_explicit_override_does_not_probe_loop(self):
        self.agent.mkdir()
        (self.agent / 'memory').symlink_to('memory')
        self.check('explicit path', AGENT_HOME=str(self.agent), MEM_STORE='explicit path')

    def test_symlink_target_trailing_slash_requires_directory(self):
        target = self.base / 'external'
        self.populate(target)
        self.legacy.mkdir(parents=True)
        (self.legacy / 'memory.db').symlink_to(str(target / 'memory.db') + '/')
        self.check(self.legacy)

unittest.main(argv=[sys.argv[0]], verbosity=1)
PY
