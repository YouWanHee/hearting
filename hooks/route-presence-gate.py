#!/usr/bin/env python3
"""Claude PreToolUse bridge for the route presence gate (utilities/route_presence_gate.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))


def main() -> int:
    try:
        import route_presence_gate
    except Exception:  # noqa: BLE001 -- a missing gate never blocks a tool call
        return 0
    return route_presence_gate.main(["--claude"])


if __name__ == "__main__":
    raise SystemExit(main())
