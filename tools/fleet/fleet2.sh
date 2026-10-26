#!/usr/bin/env bash
# fleet2.sh — compatibility alias for the Fleet Explorer.
#   Keep this path for existing scripts; fleet.sh is the public command name.
set -euo pipefail

# Resolve the real script location when invoked through a symlink.
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR=$(cd -P "$(dirname "$SOURCE")" && pwd)
  SOURCE=$(readlink "$SOURCE")
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR=$(cd -P "$(dirname "$SOURCE")" && pwd)
exec "$SCRIPT_DIR/fleet.sh" "$@"
