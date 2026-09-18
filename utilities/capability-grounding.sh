#!/bin/sh
# capability-grounding.sh — record the acting session's inline entry capability so Fleet can
# show `capability(mode·intensity)` for work that never dispatches (and thus leaves no jobs.log
# row). Sibling of the spec-read grounding marker: dispatched work is grounded by jobs.log/route,
# inline work by this marker. POSIX sh, no jq.
#
#   capability-grounding.sh record --sid <id> --capability <name> \
#       [--mode <m>] [--intensity <i>] [--agent-home <dir>] [--cwd <dir>]
#
# Writes `${FLEET_CAPABILITY_GROUNDING_DIR:-${XDG_STATE_HOME:-~/.local/state}/agent-fleet
# /capability-grounding}/<sid>` with KV lines. The release tree is immutable — this writer moved
# out of it (F-<next> fleet-route-chain-r2); it no longer depends on `AGENT_HOME`, and does not lean
# on the runtime's build/ignore lists (`runtime_activation._IGNORE_NAMES`, build-release exclude)
# to keep a release clean. `--agent-home` is accepted for caller compatibility and discarded. A
# release built before this change still holds `<agent-home>/.capability-grounding/`; Fleet reads
# that old location as a fallback only (tools/fleet/projection.py). Overwrites on each call, so the
# freshest entry-skill invocation wins (the session's CURRENT capability). Fleet reads the file's
# mtime for freshness (same sid-reuse rule as the spec marker) and the KV body for the tag.

set -eu

# The fixed, capability-agnostic intensity vocabulary (CONVENTIONS §1.1). A mode is
# capability-specific, so the caller passes it explicitly; only the value is validated as
# non-empty. An unrecognized intensity is dropped rather than stored (honest omission).
valid_intensity() {
  case "$1" in
    direct|quick|standard|strong|thorough|adversarial) return 0 ;;
    *) return 1 ;;
  esac
}

record() {
  sid="" cap="" mode="" intensity="" cwd=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --sid) sid=${2:-}; shift 2 ;;
      --capability) cap=${2:-}; shift 2 ;;
      --mode) mode=${2:-}; shift 2 ;;
      --intensity) intensity=${2:-}; shift 2 ;;
      --cwd) cwd=${2:-}; shift 2 ;;
      --agent-home) shift 2 ;;   # accepted for compat, discarded — write location is fixed below
      *) echo "capability-grounding: unknown argument: $1" >&2; return 64 ;;
    esac
  done
  [ -n "$sid" ] || { echo "capability-grounding: --sid is required" >&2; return 64; }
  [ -n "$cap" ] || { echo "capability-grounding: --capability is required" >&2; return 64; }
  # Only the entry-capability set is grounded; a sub-skill or tool call is not a session identity.
  case "$cap" in
    autopilot-apply|autopilot-code|autopilot-design|autopilot-draft|autopilot-lab|autopilot-refine|autopilot-research|autopilot-ship|autopilot-spec) ;;
    *) return 0 ;;
  esac
  valid_intensity "$intensity" || intensity=""

  dir="${FLEET_CAPABILITY_GROUNDING_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-fleet/capability-grounding}"
  (umask 077 && mkdir -p "$dir") 2>/dev/null || return 0
  tmp="$dir/.$sid.tmp.$$"
  {
    printf 'capability=%s\n' "$cap"
    [ -n "$mode" ] && printf 'mode=%s\n' "$mode"
    [ -n "$intensity" ] && printf 'intensity=%s\n' "$intensity"
    [ -n "$cwd" ] && printf 'cwd=%s\n' "$cwd"
    :   # keep the block's exit status 0 even when the last optional field is empty
  } > "$tmp" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 0; }
  mv -f "$tmp" "$dir/$sid" 2>/dev/null || rm -f "$tmp" 2>/dev/null
}

case "${1:-}" in
  record) shift; record "$@" ;;
  -h|--help|"") echo "usage: capability-grounding.sh record --sid <id> --capability <name> [--mode <m>] [--intensity <i>] [--agent-home <dir>] [--cwd <dir>]" ;;
  *) echo "capability-grounding: unknown command: $1" >&2; exit 64 ;;
esac
