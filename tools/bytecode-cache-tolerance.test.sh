#!/usr/bin/env bash
# Every checker must tolerate the `__pycache__` a local Python run leaves in
# any source directory: git ignores it and a release is `git archive`, so it
# is never part of a projection or a release (2026-09-10, after five false
# failures in one night). This test seeds a cache into every tracked Python
# directory of a private checkout copy (tools/checkout-copy.sh) and runs the
# checkers that walk those trees there.
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
COPY_PARENT=$(mktemp -d)
trap 'rm -rf "$COPY_PARENT"' EXIT
. "$ROOT/tools/checkout-copy.sh"
checkout_copy "$ROOT" "$COPY_PARENT/repo" || { echo "FAIL - checkout copy"; exit 1; }
cd "$COPY_PARENT/repo"
seeded=()
while IFS= read -r dir; do
  cache="$dir/__pycache__"
  if [ ! -e "$cache" ]; then
    mkdir -p "$cache" && printf 'seed' > "$cache/seed.cpython-312.pyc" && seeded+=("$cache")
  fi
done < <(git ls-files '*.py' | xargs -n1 dirname | sort -u)
echo "seeded ${#seeded[@]} cache directories"
fail=0
run() {
  if "$@" >/tmp/bytecode-cache-tolerance.$$.log 2>&1; then
    echo "ok   - $*"
  else
    echo "FAIL - $*"; grep -v 'SyntaxWarning\|PAGE_TEMPLATE' /tmp/bytecode-cache-tolerance.$$.log | tail -8; fail=1
  fi
}
run python3 tools/generate.py --check
run ./tools/check-adaptation-boundary.sh
run python3 tools/check-model-config.py
run python3 tools/check-unit-config.py
run python3 tools/check-scope-placeholders.py
mkdir -p "$COPY_PARENT/archive"
git archive --format=tar HEAD | tar -xf - -C "$COPY_PARENT/archive"
cd "$COPY_PARENT/archive"
mkdir -p tools/memory/__pycache__ tools/install/__pycache__
printf 'bytecode' > tools/memory/__pycache__/probe.cpython-312.pyc
printf 'bytecode' > tools/install/probe.pyc
run ./tools/check-adaptation-boundary.sh
run python3 tools/sync-missing-projections.py
if [ -e adapters/claude/tools/memory/__pycache__ ] || [ -e adapters/claude/tools/install/probe.pyc ]; then
  echo "FAIL - projection sync copied Python bytecode into the release tree"
  fail=1
else
  echo "ok   - projection sync skips bytecode in a Git archive"
fi
rm -rf tools/memory/__pycache__ tools/install/__pycache__ adapters/claude/tools/memory/__pycache__
unset PYTHONDONTWRITEBYTECODE PYTHONPYCACHEPREFIX
unset AGENT_HOME
HARNESS_STATE_ROOT="$COPY_PARENT/state"
export HARNESS_STATE_ROOT
printf '#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, "utilities")\nimport artifact_producer\n' > hooks/_bytecode_probe.py
chmod +x hooks/_bytecode_probe.py
run hooks/run-hook.sh exec _bytecode_probe.py
rm -f hooks/_bytecode_probe.py
if [ -d utilities/__pycache__ ] || [ -d hooks/__pycache__ ]; then
  echo "FAIL - interactive hook runner wrote bytecode into the release tree"
  fail=1
else
  echo "ok   - interactive hook runner keeps bytecode out of the release tree"
fi
run python3 - <<'PY'
import json, sys
bad = []
def walk(path, node):
    if isinstance(node, dict):
        cmd = node.get("command")
        if isinstance(cmd, str) and "python" in cmd and "run-hook.sh" not in cmd and "tools/memory/mem.py" not in cmd:
            bad.append((path, cmd))
        for v in node.values():
            walk(path, v)
    elif isinstance(node, list):
        for v in node:
            walk(path, v)
for path in ("adapters/claude/settings.json", "adapters/codex/hooks/hooks.json"):
    walk(path, json.load(open(path)))
for path, cmd in bad:
    print("python hook outside run-hook.sh: %s: %s" % (path, cmd))
sys.exit(1 if bad else 0)
PY
mkdir -p "$COPY_PARENT/home" "$COPY_PARENT/claude"
probe_env=(env HOME="$COPY_PARENT/home" CLAUDE_CONFIG_DIR="$COPY_PARENT/claude" AGENT_HOME="$PWD")
printf '{"session_id":"bytecode-probe","cwd":"%s","model":{"id":"probe"}}' "$PWD" | "${probe_env[@]}" bash adapters/claude/statusline.sh >/dev/null 2>&1
"${probe_env[@]}" ./tools/memory/mem.py --help >/dev/null 2>&1
"${probe_env[@]}" ./utilities/compute-hosts --help >/dev/null 2>&1
sleep 2
if [ -n "$(find . -name __pycache__ -print -quit)" ]; then
  echo "FAIL - statusline, mem or compute-hosts wrote bytecode into the release tree"
  find . -name __pycache__ | head -5
  fail=1
elif [ -z "$(find "$HARNESS_STATE_ROOT/pycache" -name '*.pyc' -print -quit 2>/dev/null)" ]; then
  echo "FAIL - statusline, mem and compute-hosts left no bytecode in the state cache"
  fail=1
else
  echo "ok   - statusline, mem and compute-hosts keep bytecode out of the release tree"
fi
rm -f /tmp/bytecode-cache-tolerance.$$.log
[ "$fail" = 0 ] && echo "bytecode-cache-tolerance: PASS" || { echo "bytecode-cache-tolerance: FAIL"; exit 1; }
