#!/bin/sh
# PreToolUse(Edit|Write|MultiEdit|NotebookEdit): deny file edits while a git
# repository is merging, rebasing, or cherry-picking (OPERATIONS §5.9).
# This also covers direct edit paths that bypass workflow ceremony.
# Conflict-resolution authority and marker lifecycle are defined only in
# core/OPERATIONS.md §5.9; this guard enforces that existing marker mechanism.
# POSIX sh, no jq. Also supports portable CLI mode:
#   git-state-guard.sh --file <path>
case "${HEARTING_GATES:-off}" in on) ;; *) cat >/dev/null 2>&1; exit 0 ;; esac  # gates are off unless HEARTING_GATES=on

fp=""
hook_mode=1

if [ "$#" -gt 0 ]; then
  hook_mode=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --file)
        [ "$#" -ge 2 ] || { echo "git-state-guard: --file requires a path" >&2; exit 64; }
        fp="$2"; shift 2 ;;
      --help|-h)
        echo "usage: git-state-guard.sh --file <path>"
        exit 0 ;;
      *)
        echo "git-state-guard: unknown argument: $1" >&2
        exit 64 ;;
    esac
  done
else
  input=$(cat 2>/dev/null)
  [ -z "$input" ] && exit 0
  fp=$(printf '%s' "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"file_path"[[:space:]]*:[[:space:]]*"//; s/"$//')
fi
[ -z "$fp" ] && exit 0

dir=$(dirname "$fp")
# If dirname does not exist, walk to the nearest existing ancestor so a write
# to a new subdirectory cannot bypass merge/rebase detection.
while [ ! -d "$dir" ] && [ "$dir" != "/" ] && [ "$dir" != "." ]; do dir=$(dirname "$dir"); done
[ -d "$dir" ] || exit 0
gd=$(git -C "$dir" rev-parse --git-dir 2>/dev/null) || exit 0
case "$gd" in /*) ;; *) gd="$dir/$gd" ;; esac

op=""
[ -f "$gd/MERGE_HEAD" ] && op="merge"
[ -d "$gd/rebase-merge" ] || [ -d "$gd/rebase-apply" ] && op="rebase"
[ -f "$gd/CHERRY_PICK_HEAD" ] && op="cherry-pick"
# Detached HEAD means the worktree points directly at a commit without a branch.
[ -z "$op" ] && ! git -C "$dir" symbolic-ref --quiet HEAD >/dev/null 2>&1 && op="detached-HEAD"
[ -z "$op" ] && exit 0

# Authority marker governed by OPERATIONS §5.9.
[ -f "$gd/CLAUDE_MERGE_EDIT_OK" ] && exit 0

reason="$op is in progress in this repository. Apply core/OPERATIONS.md §5.9 for conflict-resolution authority and its marker lifecycle before editing. Ordinary edits and commits remain stopped; do not auto-abort or force-checkout."
if [ "$hook_mode" -eq 1 ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$reason"
  exit 0
fi

printf '⛔ %s\n' "$reason" >&2
exit 2
