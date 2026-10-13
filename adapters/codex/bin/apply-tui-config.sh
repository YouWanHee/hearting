#!/usr/bin/env sh
# apply-tui-config.sh — apply harness-recommended Codex TUI preferences to the
# runtime-owned $CODEX_HOME/config.toml without taking ownership of that file.
#
# The [tui] status_line and status_line_use_colors keys are the only keys this
# command manages. Existing status-line entries and an explicit color choice
# are preserved; the current thread id is added once at the front.
# Project trust, hook trust hashes, plugin state, model defaults, credentials
# pointers, sessions, logs, caches, and other runtime state remain untouched.
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
    echo "usage: apply-tui-config.sh   # adds the current-thread footer item in \$CODEX_HOME/config.toml"
    exit 0 ;;
  "")
    ;;
  *)
    echo "apply-tui-config: unknown option: $1" >&2
    exit 64 ;;
esac

[ -f "$FRAGMENT" ] || { echo "apply-tui-config: fragment missing: $FRAGMENT" >&2; exit 69; }
mkdir -p "$CODEX_HOME"

python3 - "$CONFIG" "$FRAGMENT" <<'PY'
import json
import pathlib
import re
import sys
import tomllib

config_path = pathlib.Path(sys.argv[1])
fragment_path = pathlib.Path(sys.argv[2])

fragment_data = tomllib.loads(fragment_path.read_text(encoding="utf-8"))
fragment_tui = fragment_data.get("tui")
default_items = fragment_tui.get("status_line") if isinstance(fragment_tui, dict) else None
default_colors = fragment_tui.get("status_line_use_colors") if isinstance(fragment_tui, dict) else None
if (not isinstance(default_items, list) or "thread-id" not in default_items
        or any(not isinstance(item, str) for item in default_items)
        or not isinstance(default_colors, bool)):
    raise SystemExit(f"apply-tui-config: invalid tui defaults in {fragment_path}")

text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
try:
    current = tomllib.loads(text) if text.strip() else {}
except tomllib.TOMLDecodeError as exc:
    raise SystemExit(f"apply-tui-config: invalid TOML in {config_path}: {exc}")
tui = current.get("tui", {})
if not isinstance(tui, dict):
    raise SystemExit(f"apply-tui-config: [tui] must be a table in {config_path}")

items = tui.get("status_line")
if items is None:
    items = list(default_items)
elif not isinstance(items, list) or any(not isinstance(item, str) for item in items):
    raise SystemExit(f"apply-tui-config: tui.status_line must be an array of strings in {config_path}")
elif "thread-id" not in items:
    items = ["thread-id", *items]

lines = text.splitlines()
section_re = re.compile(r"^\s*\[\[?[^\]]+\]\]?\s*(?:#.*)?$")
key_re = re.compile(r"^\s*status_line\s*=")
tui_start = next((i for i, line in enumerate(lines)
                  if re.match(r"^\s*\[tui\]\s*(?:#.*)?$", line)), None)
if tui_start is None:
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(["[tui]", "status_line = " + json.dumps(items),
                  "status_line_use_colors = " + str(tui.get("status_line_use_colors", default_colors)).lower()])
else:
    tui_end = next((i for i in range(tui_start + 1, len(lines))
                    if section_re.match(lines[i])), len(lines))
    status_idx = next((i for i in range(tui_start + 1, tui_end)
                       if key_re.match(lines[i])), None)
    if status_idx is None:
        lines.insert(tui_end, "status_line = " + json.dumps(items))
        tui_end += 1
    else:
        end_idx = status_idx + 1
        assignment = None
        while end_idx <= tui_end:
            chunk = "[tui]\n" + "\n".join(lines[status_idx:end_idx]) + "\n"
            try:
                assignment = tomllib.loads(chunk)["tui"]["status_line"]
                break
            except (tomllib.TOMLDecodeError, KeyError):
                end_idx += 1
        if not isinstance(assignment, list):
            raise SystemExit(f"apply-tui-config: could not read tui.status_line in {config_path}")
        if "thread-id" not in assignment:
            indent = lines[status_idx][:len(lines[status_idx]) - len(lines[status_idx].lstrip())]
            lines[status_idx:end_idx] = [indent + "status_line = " + json.dumps(items)]
            tui_end -= end_idx - status_idx - 1

    color_re = re.compile(r"^\s*status_line_use_colors\s*=")
    has_colors = any(color_re.match(lines[i]) for i in range(tui_start + 1, tui_end))
    if not has_colors:
        color_value = tui.get("status_line_use_colors", default_colors)
        lines.insert(tui_end, "status_line_use_colors = " + str(color_value).lower())

new_text = "\n".join(lines) + ("\n" if lines else "")
if config_path.exists() and config_path.read_text(encoding="utf-8") == new_text:
    changed = False
else:
    if config_path.exists():
        backup = config_path.with_name(config_path.name + ".pre-harness-tui")
        if not backup.exists():
            backup.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")
    config_path.write_text(new_text, encoding="utf-8")
    changed = True

print("adapter=codex")
print("runtime_surface=codex-runtime-tui-config")
print(f"config={config_path}")
print(f"fragment={fragment_path}")
print("managed_keys=status_line,status_line_use_colors")
print(f"changed={'yes' if changed else 'no'}")
print("status=ok")
PY
