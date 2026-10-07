#!/usr/bin/env bash
# PostToolUse hook (Edit|Write|MultiEdit) — spec component ⑥.
# Auto-renders a saved DESIGN HTML file headlessly and alerts the agent if the console errors.
# Fast no-op for non-design / non-HTML edits (the node checker self-filters from the hook JSON).
# Portable CLI: design-postwrite.sh --file <path>
# Opt out per shell with DESIGN_POSTWRITE_HOOK=0.
HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
AGENT_HOME="${AGENT_HOME:-$("$HOOK_DIR/../utilities/agent-home.sh")}"
[ "${DESIGN_POSTWRITE_HOOK:-1}" = "0" ] && exit 0

if [ "${1:-}" = "--file" ]; then
  [ "$#" -ge 2 ] || { echo "design-postwrite: --file requires a path" >&2; exit 64; }
  exec node "$AGENT_HOME/tools/design-mcp/console-check.mjs" "$2"
fi
if [ "$#" -gt 0 ]; then
  case "$1" in
    -h|--help)
      cat <<'EOF'
usage: design-postwrite.sh --file <path>

Without arguments, reads Claude PostToolUse hook JSON from stdin.
EOF
      exit 0
      ;;
    *)
      echo "design-postwrite: unknown argument: $1" >&2
      exit 64
      ;;
  esac
fi

# Hook mode. Almost every Write/Edit is not design HTML, so settle that in the shell:
# node is started only when the payload names an .htm/.html path (a superset of the
# checker's own filter, which still makes the exact call). A missing or unusable node
# is the same infra failure the checker swallows, so only its exit 2 (console errors)
# is surfaced; `--file` stays loud.
payload="$(cat)"
case "$payload" in
  *.[hH][tT][mM]*) ;;
  *) exit 0 ;;
esac
command -v node >/dev/null 2>&1 || exit 0
out="$(printf '%s' "$payload" | node "$AGENT_HOME/tools/design-mcp/console-check.mjs" --hook 2>&1)"
[ "$?" = "2" ] || exit 0
printf '%s\n' "$out" >&2
exit 2
