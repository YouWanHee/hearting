#!/usr/bin/env python3
"""Codex PreToolUse bridge for the route presence gate in utilities/route_presence_gate.py."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "utilities" / "route_presence_gate.py"

if __name__ == "__main__":
    if not GATE.is_file():
        raise SystemExit(0)
    os.execv(sys.executable, [sys.executable, str(GATE), "--codex"])
