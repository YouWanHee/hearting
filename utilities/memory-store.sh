#!/usr/bin/env sh
# Print the memory store directory, resolved exactly like
# tools/memory/store_resolve.py (R0-R5; see core/MEMORY.md Section 7.0).
#
# The store is runtime state, not harness source, so it must never resolve
# into a managed release tree. This is a read-only probe: it never opens
# memory.db, and never moves, copies, or imports anything. On conflict
# (R4) or a real probe/canonicalization error, it exits 3 with no stdout and
# a path-only diagnostic on stderr. Callers under `set -e` must guard the
# call with an explicit `if STORE=$(...); then ... else ... fi` so this
# script's fail-closed exit cannot be silently swallowed nor bypass a
# fail-open hook contract.
set -u
set -f  # disable globbing: candidate paths are split with '|' below, unquoted

_mem_store_die() {
  printf 'memory store resolution error: %s\n' "$1" >&2
  exit 3
}

# R0 -- explicit override: a non-empty MEM_STORE wins verbatim, unprobed.
if [ -n "${MEM_STORE:-}" ]; then
  printf '%s\n' "$MEM_STORE"
  exit 0
fi

_mem_home=$HOME
_mem_xdg_data=${XDG_DATA_HOME:-$_mem_home/.local/share}

# R1 -- ordered candidate set. Each entry is "<marker><SEP><path>", where
# marker is "1" only for the managed-current compatibility spelling (excluded
# from the R5 no-database fallback) and "0" otherwise. Skip a candidate whose
# source variable is empty/unset; HOME-derived candidates are always present.
_mem_sep=:
_mem_entries=""
if [ -n "${AGENT_HOME:-}" ]; then
  _mem_entries="$_mem_entries|0$_mem_sep$AGENT_HOME/memory"
fi
if [ -n "${CLAUDE_HOME:-}" ]; then
  _mem_entries="$_mem_entries|0$_mem_sep$CLAUDE_HOME/memory"
fi
_mem_entries="$_mem_entries|0$_mem_sep$_mem_home/hearting/memory"
_mem_entries="$_mem_entries|0$_mem_sep$_mem_home/agent_setting/memory"
_mem_entries="$_mem_entries|0$_mem_sep$_mem_home/.claude/memory"
_mem_entries="$_mem_entries|1$_mem_sep$_mem_xdg_data/hearting/current/memory"
_mem_entries="$_mem_entries|0$_mem_sep$_mem_xdg_data/hearting/memory"

# Strip the leading separator and split on '|' using quoted positional
# parameters -- no eval, no word splitting on IFS default whitespace, no
# newline-delimited parsing of environment-influenced paths.
_mem_entries=${_mem_entries#|}

_mem_old_ifs=$IFS
IFS='|'
set -- $_mem_entries
IFS=$_mem_old_ifs

_mem_populated_identities=""
_mem_populated_originals=""
_mem_conflict_list=""
_mem_conflict_count=0

for _mem_entry in "$@"; do
  _mem_cand=${_mem_entry#?:}
  _mem_db="$_mem_cand/memory.db"
  if [ -L "$_mem_db" ]; then
    if [ ! -e "$_mem_db" ]; then
      continue  # dangling symlink: not populated, not an error
    fi
  elif [ ! -e "$_mem_db" ]; then
    continue  # ordinary absence: not populated, not an error
  fi
  if [ ! -f "$_mem_db" ]; then
    continue  # R2 wrong-type (e.g. a directory named memory.db): not populated
  fi
  _mem_identity=$(CDPATH= cd -P -- "$(dirname -- "$_mem_db")" 2>/dev/null && pwd -P) || _mem_store_die "$_mem_cand"
  _mem_identity="$_mem_identity/memory.db"
  case "|$_mem_populated_identities|" in
    *"|$_mem_identity|"*) continue ;;
  esac
  _mem_populated_identities="$_mem_populated_identities|$_mem_identity"
  _mem_populated_originals="$_mem_populated_originals|$_mem_cand"
  _mem_conflict_count=$((_mem_conflict_count + 1))
  _mem_conflict_list="$_mem_conflict_list, $_mem_cand"
done

if [ "$_mem_conflict_count" -ge 2 ]; then
  _mem_conflict_list=${_mem_conflict_list#, }
  printf 'memory store resolution error: multiple memory databases found: %s; set MEM_STORE to one of them\n' \
    "$_mem_conflict_list" >&2
  exit 3
fi

if [ "$_mem_conflict_count" -eq 1 ]; then
  printf '%s\n' "${_mem_populated_originals#|}"
  exit 0
fi

# R5 -- no database anywhere: first existing candidate directory, excluding
# the managed-current compatibility spelling, else the canonical XDG path.
for _mem_entry in "$@"; do
  _mem_marker=${_mem_entry%%:*}
  _mem_cand=${_mem_entry#?:}
  [ "$_mem_marker" = "1" ] && continue
  if [ -e "$_mem_cand" ] || [ -L "$_mem_cand" ]; then
    printf '%s\n' "$_mem_cand"
    exit 0
  fi
done

printf '%s\n' "$_mem_xdg_data/hearting/memory"
exit 0
