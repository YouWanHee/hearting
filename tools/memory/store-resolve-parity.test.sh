#!/usr/bin/env sh
# Parity between tools/memory/store_resolve.py and utilities/memory-store.sh.
# Both must agree on stdout, exit status, and (for failures) a path-only
# stderr diagnostic for every R0-R5 case. Never touches a live store.
set -u
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
# Every R0-R5 case below runs its own `env -i HOME=... XDG_DATA_HOME=...`
# per-case environment, which is already a stricter isolation than the shared
# fixture root; test-isolation.sh is sourced to satisfy the repo-wide "every
# tools/memory/*.test.sh sources it" contract (test-isolation-contract.test.sh).
. "$ROOT/tools/memory/test-isolation.sh"
PY="$ROOT/tools/memory/store_resolve.py"
SH="$ROOT/utilities/memory-store.sh"
PYTHON3=$(command -v python3)
SH_BIN=$(command -v sh)

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); }
bad() { FAIL=$((FAIL + 1)); printf 'not ok - %s\n' "$1" >&2; }

# run_case <label> <env-args...>  -- env-args passed to `env`
run_case() {
  label=$1; shift
  py_out=$(env -i "$@" "$PYTHON3" "$PY" 2>/tmp/parity_py_err.$$); py_rc=$?
  sh_out=$(env -i "$@" "$SH_BIN" "$SH" 2>/tmp/parity_sh_err.$$); sh_rc=$?
  py_err=$(cat /tmp/parity_py_err.$$); rm -f /tmp/parity_py_err.$$
  sh_err=$(cat /tmp/parity_sh_err.$$); rm -f /tmp/parity_sh_err.$$

  if [ "$py_rc" != "$sh_rc" ]; then
    bad "$label: exit mismatch py=$py_rc sh=$sh_rc"; return
  fi
  if [ "$py_rc" = "0" ]; then
    if [ "$py_out" != "$sh_out" ]; then
      bad "$label: stdout mismatch py=[$py_out] sh=[$sh_out]"; return
    fi
    if [ -n "$py_err" ] || [ -n "$sh_err" ]; then
      bad "$label: unexpected stderr on success py=[$py_err] sh=[$sh_err]"; return
    fi
  else
    if [ -n "$py_out" ] || [ -n "$sh_out" ]; then
      bad "$label: stdout on failure py=[$py_out] sh=[$sh_out]"; return
    fi
    if [ -z "$py_err" ] || [ -z "$sh_err" ]; then
      bad "$label: missing diagnostic py=[$py_err] sh=[$sh_err]"; return
    fi
  fi
  ok
}

T=$(mktemp -d "${TMPDIR:-/tmp}/mem-store-parity.XXXXXX")
trap 'rm -rf "$T"' EXIT HUP INT TERM
mkdir -p "$T/home" "$T/data"

populate() { mkdir -p "$(dirname "$1")"; : > "$1"; }

# 1. Explicit override, nonexistent path.
run_case "explicit-nonexistent" HOME="$T/home" MEM_STORE="$T/does-not-exist"

# 2. Empty override treated as unset (falls through to no-database R5).
run_case "explicit-empty" HOME="$T/home" XDG_DATA_HOME="$T/data" MEM_STORE=

# 3. Bundle AGENT_HOME (empty) plus populated ~/.claude.
rm -rf "$T"/*; mkdir -p "$T/home" "$T/data" "$T/bundle/memory"
populate "$T/home/.claude/memory/memory.db"
run_case "bundle-empty-legacy-populated" HOME="$T/home" XDG_DATA_HOME="$T/data" AGENT_HOME="$T/bundle"

# 4. Empty early directory (AGENT_HOME/memory exists empty) + populated later store.
rm -rf "$T"/*; mkdir -p "$T/home" "$T/data" "$T/bundle/memory"
populate "$T/data/hearting/memory/memory.db"
run_case "empty-early-populated-late" HOME="$T/home" XDG_DATA_HOME="$T/data" AGENT_HOME="$T/bundle"

# 5. Two distinct populated databases -> conflict.
rm -rf "$T"/*; mkdir -p "$T/home"
populate "$T/home/.claude/memory/memory.db"
populate "$T/home/hearting/memory/memory.db"
run_case "conflict-distinct" HOME="$T/home" XDG_DATA_HOME="$T/data"

# 6. Same database through a symlink alias -- not a conflict.
rm -rf "$T"/*; mkdir -p "$T/home/.claude/memory" "$T/data/hearting"
populate "$T/home/.claude/memory/memory.db"
ln -s "$T/home/.claude/memory" "$T/data/hearting/memory"
run_case "symlink-alias-no-conflict" HOME="$T/home" XDG_DATA_HOME="$T/data"

# 7. No database, no existing compatibility directory -> canonical XDG default.
rm -rf "$T"/*; mkdir -p "$T/home" "$T/data"
run_case "no-database-no-compat-dir" HOME="$T/home" XDG_DATA_HOME="$T/data"

# 8. No database, existing compatibility directory present (non-managed-current).
rm -rf "$T"/*; mkdir -p "$T/home/.claude/memory" "$T/data"
run_case "no-database-existing-compat-dir" HOME="$T/home" XDG_DATA_HOME="$T/data"

# 9. No database, only an *empty* managed-current directory exists -> excluded,
#    falls through to canonical XDG rather than the empty release-adjacent dir.
rm -rf "$T"/*; mkdir -p "$T/home" "$T/data/hearting/current/memory"
run_case "managed-current-empty-excluded" HOME="$T/home" XDG_DATA_HOME="$T/data"

# 10. Managed-current populated: reconnects, participates in conflict checks.
rm -rf "$T"/*; mkdir -p "$T/home" "$T/data/hearting/current/memory"
populate "$T/data/hearting/current/memory/memory.db"
run_case "managed-current-populated" HOME="$T/home" XDG_DATA_HOME="$T/data"

# 11. Managed-current populated + another populated candidate -> conflict.
rm -rf "$T"/*; mkdir -p "$T/home/.claude/memory" "$T/data/hearting/current/memory"
populate "$T/home/.claude/memory/memory.db"
populate "$T/data/hearting/current/memory/memory.db"
run_case "managed-current-conflict" HOME="$T/home" XDG_DATA_HOME="$T/data"

# 12. Each home-precedence source individually populated.
rm -rf "$T"/*; mkdir -p "$T/home"
populate "$T/agent-home/memory/memory.db"
run_case "precedence-AGENT_HOME" HOME="$T/home" XDG_DATA_HOME="$T/data" AGENT_HOME="$T/agent-home"

rm -rf "$T"/*; mkdir -p "$T/home"
populate "$T/claude-home/memory/memory.db"
run_case "precedence-CLAUDE_HOME" HOME="$T/home" XDG_DATA_HOME="$T/data" CLAUDE_HOME="$T/claude-home"

rm -rf "$T"/*; mkdir -p "$T/home"
populate "$T/home/hearting/memory/memory.db"
run_case "precedence-HOME-hearting" HOME="$T/home" XDG_DATA_HOME="$T/data"

rm -rf "$T"/*; mkdir -p "$T/home"
populate "$T/home/agent_setting/memory/memory.db"
run_case "precedence-HOME-agent_setting" HOME="$T/home" XDG_DATA_HOME="$T/data"

# 13. Relative and space-containing explicit paths (R0 preserves them verbatim).
rm -rf "$T"/*; mkdir -p "$T/home"
run_case "explicit-relative" HOME="$T/home" MEM_STORE="relative/store path"
run_case "explicit-space" HOME="$T/home" MEM_STORE="$T/space store/dir"

# 14. memory.db as a directory (R2 wrong type) -- not populated, no error.
rm -rf "$T"/*; mkdir -p "$T/home/.claude/memory/memory.db" "$T/data"
run_case "memory-db-is-directory" HOME="$T/home" XDG_DATA_HOME="$T/data"

# 15. Dangling symlink -- not populated, no error.
rm -rf "$T"/*; mkdir -p "$T/home/.claude/memory" "$T/data"
ln -s "$T/home/.claude/memory/nonexistent-target" "$T/home/.claude/memory/memory.db"
run_case "dangling-symlink" HOME="$T/home" XDG_DATA_HOME="$T/data"

# 16. Valid database symlink (not an alias of another candidate).
rm -rf "$T"/*; mkdir -p "$T/home/.claude/memory" "$T/real-target"
populate "$T/real-target/memory.db"
ln -s "$T/real-target/memory.db" "$T/home/.claude/memory/memory.db"
run_case "valid-database-symlink" HOME="$T/home" XDG_DATA_HOME="$T/data"

rm -rf "$T"/*

printf 'store-resolve-parity: PASS=%s FAIL=%s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
