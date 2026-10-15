#!/usr/bin/env sh
# Apply the shared Codex TUI preference merge to runtime-owned config.toml.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME=${AGENT_HOME:-}
if [ -z "$AGENT_HOME" ] || [ ! -f "$AGENT_HOME/core/CORE.md" ]; then
  AGENT_HOME=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
fi
CODEX_HOME=${CODEX_HOME:-$HOME/.codex}
CONFIG="$CODEX_HOME/config.toml"
FRAGMENT="$AGENT_HOME/adapters/codex/config/tui-statusline.toml"

case "${1:-}" in
  -h|--help)
    echo "usage: apply-tui-config.sh   # preserves the current-thread footer item in \$CODEX_HOME/config.toml"
    exit 0 ;;
  "") ;;
  *) echo "apply-tui-config: unknown option: $1" >&2; exit 64 ;;
esac

[ -f "$FRAGMENT" ] || { echo "apply-tui-config: fragment missing: $FRAGMENT" >&2; exit 69; }
mkdir -p "$CODEX_HOME"

python3 - "$CONFIG" "$FRAGMENT" "$AGENT_HOME/utilities" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[3])
import codex_tui_config

config, fragment = Path(sys.argv[1]), Path(sys.argv[2])
try:
    result = codex_tui_config.apply_status_line(config, fragment)
except (OSError, UnicodeError, codex_tui_config.CodexTuiConfigError) as exc:
    raise SystemExit(f"apply-tui-config: {exc}") from exc
print("adapter=codex")
print("runtime_surface=codex-runtime-tui-config")
print(f"config={result['path']}")
print(f"fragment={result['fragment']}")
print("managed_keys=status_line,status_line_use_colors")
print(f"changed={'yes' if result['changed'] else 'no'}")
print("status=ok")
PY
