#!/usr/bin/env python3
"""Codex PostToolUse(request_user_input) bridge: keep the person's reply to a registered frame
question (utilities/frame_native_answer.py)."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RECORDER = ROOT / "utilities" / "frame_native_answer.py"

if __name__ == "__main__":
    if not RECORDER.is_file():
        raise SystemExit(0)
    os.execv(sys.executable, [sys.executable, str(RECORDER), "--codex"])
