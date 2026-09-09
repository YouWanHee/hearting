#!/usr/bin/env sh
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
# Regression: empty-store creation guard (2026-07-22 memory audit P2).
# A DERIVED store path (AGENT_HOME/default) with no memory.db must FAIL LOUD and
# create nothing; explicit MEM_STORE or MEM_INIT=1 may create. Uncovered before:
# a worktree-style AGENT_HOME export silently fabricated an empty DB and reported
# "aspect not found" as if the knowledge did not exist.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
MEM="$ROOT/tools/memory/mem.py"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

fail() { echo "not ok - $*" >&2; exit 1; }

# 1. Derived path (AGENT_HOME → empty checkout): refuse, mention the resolved path,
#    create no store.
mkdir -p "$TMP/fake-home"
if hearting_test_derived_env env AGENT_HOME="$TMP/fake-home" XDG_DATA_HOME="$TMP/fake-data" \
    python3 "$MEM" profile 01_paper_figure_style \
    >"$TMP/out1" 2>"$TMP/err1"; then
  fail "derived empty store was accepted (guard missing)"
fi
grep -q "refusing to create" "$TMP/err1" || fail "refusal message missing"
grep -q "$TMP/fake-home" "$TMP/err1" || fail "resolved path missing from error"
test ! -e "$TMP/fake-data/hearting/memory/memory.db" \
  || fail "empty DB was still created"

# 2. Explicit MEM_STORE: creation allowed (isolated envs and tests depend on this).
if ! MEM_STORE="$TMP/store2" python3 "$MEM" add durable note \
    "empty-store guard regression fixture: explicit MEM_STORE must be allowed to create a brand-new isolated store for tests and isolated environments." \
    --scope global >"$TMP/out2" 2>"$TMP/err2"; then
  cat "$TMP/err2" >&2; fail "explicit MEM_STORE creation was refused"
fi
test -f "$TMP/store2/memory.db" || fail "MEM_STORE store not created"

# 3. MEM_INIT=1 escape hatch on a derived path: creation allowed.
mkdir -p "$TMP/fresh-home"
if ! hearting_test_derived_env env AGENT_HOME="$TMP/fresh-home" XDG_DATA_HOME="$TMP/fresh-data" MEM_INIT=1 \
    python3 "$MEM" add durable note \
    "empty-store guard regression fixture: MEM_INIT=1 is the documented escape hatch for a genuine first install on a derived path." \
    --scope global >"$TMP/out3" 2>"$TMP/err3"; then
  cat "$TMP/err3" >&2; fail "MEM_INIT=1 first install was refused"
fi
test -f "$TMP/fresh-data/hearting/memory/memory.db" \
  || fail "MEM_INIT store not created"

# 4. An isolated primary fixture still opens read paths normally (no regression).
MEM_STORE="$TMP/store2" python3 "$MEM" stats >/dev/null 2>&1 \
  || fail "primary store profile read broke"

# 5. Empty MEM_STORE is unset for both path selection and creation authority.
if hearting_test_derived_env env MEM_STORE= XDG_DATA_HOME="$TMP/empty-data" \
    python3 "$MEM" index >"$TMP/empty-out" 2>"$TMP/empty-err"; then
  fail "empty MEM_STORE granted initialization authority"
fi
grep -q 'refusing to create' "$TMP/empty-err" || fail "empty override refusal missing"
test ! -e "$TMP/empty-data/hearting/memory/memory.db" || fail "empty override created a DB"

# 6. Actual OpenCode SessionEnd plus bounded worker-environment probes. All
# model boundaries are stubs; the workers' child index command uses real mem.py.
python3 - "$ROOT" "$TMP" <<'PYTEST'
import json
import os
from pathlib import Path
import subprocess
import sys

root, fixture = map(Path, sys.argv[1:])
mem = root / 'tools/memory/mem.py'

def environment(name):
    base = fixture / name
    base.mkdir()
    home = base / 'home'; home.mkdir()
    project = base / 'project'; project.mkdir()
    agent = base / 'agent'; (agent / 'core').mkdir(parents=True)
    (agent / 'core/CORE.md').write_text('# Isolated agent-home fixture\n')
    env = {
        'PATH': os.defpath, 'HOME': str(home), 'AGENT_HOME': str(agent),
        'XDG_DATA_HOME': str(base / 'data'),
        'XDG_CONFIG_HOME': str(base / 'config'),
        'XDG_STATE_HOME': str(base / 'state'),
        'MEM_PROJECTS': str(base / 'projects'),
        'MEM_WRITE_EVENTS': str(base / 'state/write.jsonl'),
        'MEM_RECALL_EVENTS': str(base / 'state/recall.jsonl'),
        'MEM_RECALL_RECEIPTS': str(base / 'state/receipts'),
        'AGENT_MODEL_GOVERNOR_ROOT': str(base / 'governor'),
        'AGENT_ARTIFACT_ROOT': str(project / '.agent_reports'),
    }
    return base, project, env

for value in [None, '', 'explicit', 'init']:
    base, project, env = environment('session-end-' + str(value))
    env['OPENCODE_DISTILL_ENABLE'] = '0'
    if value in ['', 'explicit']:
        env['MEM_STORE'] = str(base / 'explicit') if value else ''
    if value == 'init':
        env['MEM_INIT'] = '1'
    command = ['sh', str(root / 'adapters/opencode/bin/preflight.sh'),
               'session-end', str(project), 'empty-store-fixture']
    result = subprocess.run(command, env=env, cwd=project, text=True,
                            capture_output=True, timeout=15)
    # SessionEnd preserves the initial sync failure after the bounded
    # curator/post-sync attempt. A refused derived store is exit 2, not success.
    expected_status = 0 if value in ['explicit', 'init'] else 2
    assert result.returncode == expected_status, (command, result.stderr)
    assert not result.stdout, result.stdout
    db = (base / 'explicit' if value == 'explicit' else base / 'data/hearting/memory') / 'memory.db'
    if value in ['explicit', 'init']:
        assert db.is_file(), ('authorized initialization lost', value, result.stderr)
    else:
        assert not db.exists(), ('derived initialization bypass', value)
        assert 'refusing to create' in result.stderr, result.stderr
        assert 'session-end memory sync status=2' in result.stderr, result.stderr

for runtime in ['codex', 'opencode']:
    for value in [None, '', 'explicit']:
        base, project, env = environment(runtime + '-' + str(value))
        bin_dir = base / 'bin'; bin_dir.mkdir()
        probe = base / 'child-probe.json'
        # Intercept only the transcript boundary. Probe actual mem.py index
        # under exactly the worker-provided environment, then report no delta.
        wrapper = bin_dir / 'python3'
        wrapper.write_text('#!' + sys.executable + '\n' +
            'import json, os, subprocess, sys\n'
            'from pathlib import Path\n'
            'if len(sys.argv) > 2 and sys.argv[1].endswith("/tools/memory/mem.py") and sys.argv[2] == "distill":\n'
            ' r = subprocess.run([' + repr(sys.executable) + ', sys.argv[1], "index"], capture_output=True, text=True)\n'
            ' Path(' + repr(str(probe)) + ').write_text(json.dumps({"override": os.environ.get("MEM_STORE"), "rc": r.returncode, "err": r.stderr}))\n'
            ' print(json.dumps({"delta": "", "frontier": "fixture-empty"}))\n'
            ' sys.exit(0)\n'
            'os.execv(' + repr(sys.executable) + ', [' + repr(sys.executable) + '] + sys.argv[1:])\n')
        wrapper.chmod(0o755)
        model = bin_dir / runtime
        model.write_text('#!/bin/sh\nexit 88\n'); model.chmod(0o755)
        env['PATH'] = str(bin_dir) + os.pathsep + os.defpath
        env[runtime.upper() + '_DISTILL_ENABLE'] = '1'
        if value is not None:
            env['MEM_STORE'] = str(base / 'explicit') if value else ''
        command = ['sh', str(root / 'adapters' / runtime / 'bin/distill-worker.sh'),
                   'empty-store-fixture', str(project)]
        result = subprocess.run(command, env=env, cwd=project, text=True,
                                capture_output=True, timeout=15)
        assert result.returncode == 0, (command, result.stderr)
        evidence = json.loads(probe.read_text())
        assert evidence['override'] == env.get('MEM_STORE'), evidence
        expected = 0 if value == 'explicit' else 2
        assert evidence['rc'] == expected, evidence
        db = (base / 'explicit' if value == 'explicit' else base / 'data/hearting/memory') / 'memory.db'
        assert db.exists() == (value == 'explicit'), (value, evidence)
print('OpenCode SessionEnd: 4 actual isolated guard cases passed')
print('Codex/OpenCode workers: 6 mocked transcript-boundary / real mem.py guard cases passed')
PYTEST

echo "empty-store-guard: PASS"
