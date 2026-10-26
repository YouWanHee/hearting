#!/usr/bin/env bash
# fleet.sh — public launcher for the Fleet Explorer.
# The shared terminal/window launcher remains in fleet-monitor.sh.
set -euo pipefail

# Resolve the real script location when invoked through a symlink.
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR=$(cd -P "$(dirname "$SOURCE")" && pwd)
  SOURCE=$(readlink "$SOURCE")
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR=$(cd -P "$(dirname "$SOURCE")" && pwd)
exec "$SCRIPT_DIR/fleet-monitor.sh" --explorer "$@"
