#!/usr/bin/env bash
# Session card / tidy-notice bridge for Claude SessionStart and UserPromptSubmit.
# Independent of the memory candidate probe: it needs no prompt text, no memory
# tool and no candidate, so a short or empty prompt still receives its card.
# Any missing file, tool or failure is silent and exits 0.
set -u

HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
AGENT_HOME="${AGENT_HOME:-$("$HOOK_DIR/../utilities/agent-home.sh" 2>/dev/null || true)}"

if [ "${AGENT_SESSION_ROLE:-}" = worker ] \
  || [ "${AGENT_DISPATCH_CHILD:-}" = 1 ] \
  || [ -n "${AGENT_DISPATCH_DEPTH:-}" ] \
  || [ -n "${OPENCODE_DISPATCH_SLUG:-}" ] \
  || [ "${FLEET_TITLE_REFRESH:-}" = 1 ] \
  || [ "${MEM_DISTILL:-}" = 1 ]; then
  cat >/dev/null 2>&1 || true
  exit 0
fi

HOOK_DIR="$HOOK_DIR" AGENT_HOME="$AGENT_HOME" python3 -c '
import json, os, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}
if not isinstance(payload, dict):
    payload = {}
name = payload.get("hook_event_name")
event = {"SessionStart": "start", "UserPromptSubmit": "prompt"}.get(name)
sid = payload.get("session_id")
if not event or not isinstance(sid, str) or not sid:
    sys.exit(0)
for base in (os.environ.get("AGENT_HOME", ""),
             os.path.join(os.environ.get("HOOK_DIR", ""), "..")):
    tool = os.path.join(base, "utilities", "session_tidy.py") if base else ""
    if tool and os.path.isfile(tool):
        sys.path.insert(0, os.path.dirname(tool))
        break
else:
    sys.exit(0)
def text(key):
    value = payload.get(key)
    return value if isinstance(value, str) else ""
def emit(context):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": name,
          "additionalContext": context}}, ensure_ascii=False), flush=True)
try:
    import session_tidy
    session_tidy.run_hook(
        "claude", event, sid, source=text("source"),
        transcript=text("transcript_path"), cwd=text("cwd") or os.getcwd(), emit=emit)
except Exception:
    pass
' 2>/dev/null
exit 0
