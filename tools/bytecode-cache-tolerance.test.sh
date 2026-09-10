#!/usr/bin/env bash
# Every checker must tolerate the `__pycache__` a local Python run leaves in
# any source directory: git ignores it and a release is `git archive`, so it
# is never part of a projection or a release (2026-09-10, after five false
# failures in one night). This test seeds a cache into every tracked Python
# directory, runs the checkers that walk those trees, and removes only what
# it seeded.
set -u
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
seeded=()
cleanup() {
  for d in "${seeded[@]:-}"; do
    [ -n "$d" ] && rm -rf "$d"
  done
}
trap cleanup EXIT
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
rm -f /tmp/bytecode-cache-tolerance.$$.log
[ "$fail" = 0 ] && echo "bytecode-cache-tolerance: PASS" || { echo "bytecode-cache-tolerance: FAIL"; exit 1; }
