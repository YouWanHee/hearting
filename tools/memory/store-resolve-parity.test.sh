#!/usr/bin/env sh
# Expected-value tests for both read-only resolvers, entirely under one fixture.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
. "$ROOT/tools/memory/test-isolation.sh"
hearting_test_isolate
trap 'chmod -R u+rwx "$HEARTING_TEST_ROOT"; rm -rf "$HEARTING_TEST_ROOT"' EXIT HUP INT TERM
python3 - "$ROOT" "$HEARTING_TEST_ROOT" <<'PY'
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
    bin_dir = Path(temporary) / 'bin'; bin_dir.mkdir()
    stub = bin_dir / 'realpath'
    stub.write_text('#!/bin/sh\nexit 1\n'); stub.chmod(0o755)
    result = subprocess.run(commands[1], env={
        'HOME': temporary, 'AGENT_HOME': str(candidate.parent),
        'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
        'XDG_DATA_HOME': str(Path(temporary)/'data'),
    }, capture_output=True, text=True, timeout=10)
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
print(f'store-resolve-parity: PASS={passed} FAIL=0; supplied-env/verbatim Path/canonicalization/empty-init APIs PASS')
PY
