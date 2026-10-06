#!/bin/sh
# PreToolUse(Skill): SD-11 stage-dispatch reminder (Claude hook; portable CLI elsewhere).
#   A dispatch-depth-1 conductor at standard+ intensity normally dispatches each durable stage
#   (code-plan/execute/test/report) as its own dispatch-depth-2 headless session
#   (dev-pipeline Step 1~7) instead of invoking code-<stage> in-session.
#
#   The hook only reminds; it never denies. It cannot tell a legitimate inline run (a checked
#   runtime fallback, a full worker seat, a nonseparable edit) from a mistake: its one real deny
#   in 30 days was a false positive that sent an owner into 24 retries against the same limit.
#   Codex and OpenCode never had a deny here, so a reminder keeps the three harnesses equal.
#
#   Decision (else clean exit 0 / silent):
#     conductor_code_stage = AGENT_DISPATCH_DEPTH=1 (harness-planted dispatch marker)
#                            AND skill ∈ {code-plan,code-execute,code-test,code-report}
#     · not conductor_code_stage        → silent (main, dispatch-depth-2 stage session, non-code)
#     · intensity ∈ {direct,quick}      → silent (direct inline; quick is a dispatch-depth-1 one-shot worker)
#     · otherwise (standard+ or unknown) → reminder (additionalContext JSON on stdout), exit 0
#
#   Portable CLI (conformance): stage-dispatch-reminder.sh --skill <name>
#     [--cwd <dir>] [--session <id>] [--dispatch-depth <n>] [--intensity <i>]
#   Without args, reads Claude PreToolUse hook JSON from stdin.

CODE_STAGES="code-plan code-execute code-test code-report"

is_code_stage() {
  for s in $CODE_STAGES; do [ "$1" = "$s" ] && return 0; done
  return 1
}

# conductor_code_stage — whether a dispatch-depth-1 conductor invokes code-<stage> in-session.
# Depth alone is the evidence: AGENT_DISPATCH_DEPTH is planted by the dispatch wrapper, while a
# runtime child-session marker also appears in interactive teammate sessions (core/OPERATIONS.md §5.10).
conductor_code_stage() { # $1=skill $2=depth ; env: AGENT_DISPATCH_DEPTH (via caller)
  [ "$2" = "1" ] || return 1
  is_code_stage "$1" || return 1
  return 0
}

_json_wrap() { # $1=message ; emit PreToolUse hookSpecificOutput additionalContext
  printf '%s' "$1" | python3 -c 'import sys,json
out={"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":sys.stdin.read()}}
print(json.dumps(out, ensure_ascii=False))'
}

emit_reminder() { # $1=skill
  node=${1#code-}
  msg="📌 stage-dispatch: this session is a dispatch-depth-1 conductor (intensity=${AGENT_DISPATCH_INTENSITY:-?}). Dispatch ${1} route-bound with python3 \"\$AGENT_HOME/utilities/stage-dispatch-fallback.py\" --node ${node} --start (route, slug, parent and harness come from this owner); capture the emitted attempt_id, then yield for the runtime-owned exact-batch join and harvest the typed receipt before completing that route/node/attempt (dev-pipeline steps 1-7). Use dispatch-wait only when the wrapper reports poll-fallback. Invoke the Skill in-session only for direct inline work, a quick one-shot worker, or a checked runtime fallback."
  _json_wrap "$msg"
}

# decide — single decision point; unmet conditions exit silently with status 0.
decide() { # $1=skill $2=depth $3=intensity
  conductor_code_stage "$1" "$2" || return 0
  case "$3" in
    direct|quick) return 0 ;;  # Direct inline / quick one-shot worker: stay silent.
    *) emit_reminder "$1" ;;   # standard+ or unknown intensity: remind, never deny.
  esac
}

# --- CLI mode ---
if [ "$#" -gt 0 ]; then
  skill=""; depth="${AGENT_DISPATCH_DEPTH:-}"; intensity="${AGENT_DISPATCH_INTENSITY:-}"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --skill) skill=$2; shift 2 ;;
      --cwd) shift 2 ;;
      --session) shift 2 ;;
      --dispatch-depth) depth=$2; shift 2 ;;
      --intensity) intensity=$2; shift 2 ;;
      -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) echo "stage-dispatch-reminder: unknown arg '$1'" >&2; exit 64 ;;
    esac
  done
  [ -n "$skill" ] || { echo "stage-dispatch-reminder: --skill required" >&2; exit 64; }
  AGENT_DISPATCH_INTENSITY="$intensity"
  decide "$skill" "$depth" "$intensity"
  exit 0
fi

# --- stdin (Claude hook JSON) mode ---
input=$(cat 2>/dev/null)
[ -z "$input" ] && exit 0
skill=$(printf '%s' "$input" | grep -o '"skill"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"skill"[[:space:]]*:[[:space:]]*"//; s/"$//')
decide "$skill" "${AGENT_DISPATCH_DEPTH:-}" "${AGENT_DISPATCH_INTENSITY:-}"
exit 0
