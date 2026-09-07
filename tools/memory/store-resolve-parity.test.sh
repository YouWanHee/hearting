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

# Preserve the main owner's additional exact-expectation matrix and API probes.
python3 - "$ROOT" "$T" <<'PY'
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

repo, outer = map(Path, sys.argv[1:])
commands = [[sys.executable, str(repo / 'tools/memory/store_resolve.py')],
            [shutil.which('sh'), str(repo / 'utilities/memory-store.sh')]]
passed = 0

def populated(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'memory.db').touch()
    return path

def check(label, setup, *, error=False):
    global passed
    with tempfile.TemporaryDirectory(prefix='case-', dir=outer) as temporary:
        root = Path(temporary)
        home, data = root / 'home', root / 'data'
        home.mkdir(); data.mkdir()
        env = {'PATH': os.environ['PATH'], 'HOME': str(home),
               'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_DATA_HOME': str(data),
               'XDG_STATE_HOME': str(root / 'state'),
               'MEM_WRITE_EVENTS': str(root / 'state/write.jsonl'),
               'MEM_RECALL_EVENTS': str(root / 'state/recall.jsonl'),
               'MEM_RECALL_RECEIPTS': str(root / 'state/receipts')}
        expected = setup(root, home, data, env)
        expected_rc = 3 if error else 0
        expected_out = '' if error else str(expected) + '\n'
        expected_err = str(expected) + '\n' if error else ''
        try:
            for command in commands:
                result = subprocess.run(command, env=env, cwd=root, text=True,
                                        capture_output=True, timeout=10)
                assert (result.returncode, result.stdout, result.stderr) == (
                    expected_rc, expected_out, expected_err), (
                    label, command[0], result.returncode, result.stdout, result.stderr,
                    expected_rc, expected_out, expected_err)
            passed += 1
        finally:
            # Restore only this test's known permission fixture for cleanup.
            locked = root / 'locked'
            if locked.is_dir():
                locked.chmod(0o700)

for value in ('./store', './store//leaf/', 'relative space', 'pipe|colon:quote\'"', 'line\nend\n'):
    def explicit(r, h, d, e, value=value):
        e['MEM_STORE'] = value
        loop = r / 'loop'; loop.symlink_to(loop)
        e['AGENT_HOME'] = str(loop)  # proves the override never probes candidates
        return value
    check('explicit verbatim ' + repr(value), explicit)

def empty_override(r, h, d, e):
    e.update(MEM_STORE='', AGENT_HOME='', CLAUDE_HOME='')
    return d / 'hearting/memory'
check('empty overrides / no DB', empty_override)
check('XDG unset default', lambda r,h,d,e: (e.pop('XDG_DATA_HOME'), h/'.local/share/hearting/memory')[1])
check('XDG empty default', lambda r,h,d,e: (e.update(XDG_DATA_HOME=''), h/'.local/share/hearting/memory')[1])

def bundle(r, h, d, e):
    e['AGENT_HOME'] = str(r / 'bundle')
    (r / 'bundle/memory').mkdir(parents=True)
    (d / 'hearting/memory').mkdir(parents=True)
    return populated(h / '.claude/memory')
check('empty bundle and XDG cannot hide legacy', bundle)
for variable in ('AGENT_HOME', 'CLAUDE_HOME'):
    check(variable, lambda r,h,d,e,v=variable: (e.update({v: str(r/v)}), populated(r/v/'memory'))[1])
for relative in ('hearting/memory', 'agent_setting/memory', '.claude/memory'):
    check(relative, lambda r,h,d,e,p=relative: populated(h/p))
check('canonical populated', lambda r,h,d,e: populated(d/'hearting/memory'))
check('managed populated, overrides unset', lambda r,h,d,e: populated(d/'hearting/current/memory'))
check('managed empty excluded', lambda r,h,d,e: ((d/'hearting/current/memory').mkdir(parents=True), d/'hearting/memory')[1])
check('ordinary empty directory retained', lambda r,h,d,e: ((h/'.claude/memory').mkdir(parents=True), h/'.claude/memory')[1])

def conflict(r, h, d, e):
    a = populated(h / '.claude/memory'); b = populated(d / 'hearting/current/memory')
    return f'memory store resolution error: multiple memory databases found: {a}, {b}; set MEM_STORE to one of them'
check('managed conflict, overrides unset', conflict, error=True)

def alias(r, h, d, e, file_alias=False):
    first = populated(h / '.claude/memory')
    other = d / 'hearting/current/memory'
    other.parent.mkdir(parents=True)
    if file_alias:
        other.mkdir(); (other/'memory.db').symlink_to(first/'memory.db')
    else:
        other.symlink_to(first)
    return first
check('directory alias', alias)
check('file symlink alias', lambda *args: alias(*args, file_alias=True))

def unusual(r, h, d, e, component):
    agent = r / component
    e['AGENT_HOME'] = str(agent)
    return populated(agent / 'memory')
for component in ('has|pipe', 'has:colon', 'has space', 'has\nnewline', '-leading', '한글', 'has*glob'):
    check('candidate ' + repr(component), lambda *args,c=component: unusual(*args, c))

def shape(r, h, d, e, kind):
    early = h / 'hearting/memory'
    early.mkdir(parents=True)
    db = early / 'memory.db'
    if kind == 'directory': db.mkdir()
    elif kind == 'dangling': db.symlink_to(r / 'absent')
    elif kind == 'fifo': os.mkfifo(db)
    return populated(h / '.claude/memory')
for kind in ('directory', 'dangling', 'fifo'):
    check('wrong database type ' + kind, lambda *args,k=kind: shape(*args, k))

def fallback_shape(r, h, d, e, kind):
    early = h / 'hearting/memory'; early.parent.mkdir()
    if kind == 'file': early.touch()
    else: early.symlink_to(r / 'absent')
    return d / 'hearting/memory'
for kind in ('file', 'dangling'):
    check('R5 skips ' + kind, lambda *args,k=kind: fallback_shape(*args, k))

def loop(r, h, d, e, parent=False):
    candidate = r / 'agent/memory'
    if parent:
        (r / 'agent').symlink_to(r / 'agent')
    else:
        candidate.mkdir(parents=True); (candidate/'memory.db').symlink_to(candidate/'memory.db')
    e['AGENT_HOME'] = str(r / 'agent')
    populated(h / '.claude/memory')
    return f'memory store resolution error: {candidate}'
check('file symlink loop aborts', loop, error=True)
check('parent symlink loop aborts', lambda *args: loop(*args, parent=True), error=True)

def inaccessible(r, h, d, e):
    locked = r / 'locked'; locked.mkdir(); locked.chmod(0)
    e['AGENT_HOME'] = str(locked)
    populated(h / '.claude/memory')
    return f'memory store resolution error: {locked}/memory'
assert os.geteuid() != 0, 'permission fixture requires an unprivileged test user'
check('unreadable first parent aborts', inaccessible, error=True)

# Import API must honor the supplied environment, rather than ambient HOME.
spec = importlib.util.spec_from_file_location('resolver', repo/'tools/memory/store_resolve.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
with tempfile.TemporaryDirectory(dir=outer) as temporary:
    assert module.resolve_store({'HOME': temporary}) == Path(temporary)/'.local/share/hearting/memory'
    for spelling in ('./store//leaf/', 'relative space', 'line\nend\n'):
        selected = module.resolve_store({'HOME': temporary, 'MEM_STORE': spelling})
        assert isinstance(selected, Path)
        assert str(selected) == os.fspath(selected) == spelling
        assert selected / 'memory.db' == Path(spelling) / 'memory.db'
    candidate = populated(Path(temporary) / 'agent/memory')
    with mock.patch.object(Path, 'resolve', side_effect=OSError('fixture failure')):
        try:
            module.resolve_store({'HOME': temporary, 'AGENT_HOME': str(candidate.parent)})
        except module.StoreResolutionError as error:
            assert str(error) == f'memory store resolution error: {candidate}'
        else:
            raise AssertionError('canonicalization failure was swallowed')
    # The portable shell walks file symlinks using readlink; it must not call
    # Python or GNU stat/realpath, even when those commands are on PATH.
    target = candidate / 'database-target'
    (candidate / 'memory.db').rename(target)
    (candidate / 'memory.db').symlink_to(target.name)
    bin_dir = Path(temporary) / 'bin'; bin_dir.mkdir()
    calls = Path(temporary) / 'forbidden-tool-calls'
    for name in ('stat', 'realpath', 'python', 'python3'):
        stub = bin_dir / name
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$STORE_TOOL_CALLS"\nexit 97\n')
        stub.chmod(0o755)
    shell_env = {
        'HOME': temporary, 'AGENT_HOME': str(candidate.parent),
        'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
        'XDG_DATA_HOME': str(Path(temporary)/'data'),
        'STORE_TOOL_CALLS': str(calls),
    }
    result = subprocess.run(commands[1], env=shell_env, capture_output=True,
                            text=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (0, str(candidate) + '\n', '')
    assert not calls.exists(), 'portable shell called Python or GNU path tools'
    # Preserve the main matrix's injected canonicalization-failure assertion,
    # targeting the dependency the final POSIX implementation actually uses.
    stub = bin_dir / 'readlink'
    stub.write_text('#!/bin/sh\nexit 1\n'); stub.chmod(0o755)
    result = subprocess.run(commands[1], env=shell_env, capture_output=True,
                            text=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (
        3, '', f'memory store resolution error: {candidate}\n')
with tempfile.TemporaryDirectory(dir=outer) as temporary:
    fixture = Path(temporary)
    env = {'PATH': os.environ['PATH'], 'HOME': str(fixture/'home'),
           'XDG_CONFIG_HOME': str(fixture/'config'),
           'XDG_DATA_HOME': str(fixture/'data'),
           'XDG_STATE_HOME': str(fixture/'state'), 'MEM_STORE': '',
           'MEM_WRITE_EVENTS': str(fixture/'state/write.jsonl'),
           'MEM_RECALL_EVENTS': str(fixture/'state/recall.jsonl'),
           'MEM_RECALL_RECEIPTS': str(fixture/'state/receipts')}
    result = subprocess.run([sys.executable, str(repo/'tools/memory/mem.py'),
                             'profile', 'isolated-empty-override'],
                            cwd=fixture, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and 'refusing to create' in result.stderr
    assert not list(fixture.rglob('memory.db'))
print(f'store-resolve-parity: PASS={passed} FAIL=0; supplied-env/verbatim Path/canonicalization/no-Python-or-GNU-hotpath/empty-init APIs PASS')
PY
