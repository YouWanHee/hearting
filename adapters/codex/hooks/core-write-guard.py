#!/usr/bin/env python3
"""Codex PreToolUse bridge for the two kept write gates in hooks/core-write-guard.py."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GUARD = ROOT / "hooks" / "core-write-guard.py"

if __name__ == "__main__":
    os.execv(sys.executable, [sys.executable, str(GUARD), "--codex"])
