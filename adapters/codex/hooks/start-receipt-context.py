#!/usr/bin/env python3
"""Codex-native projection of the shared start receipt context reader."""
import runpy
import sys
from pathlib import Path

if "--codex" not in sys.argv:
    sys.argv.append("--codex")
try:
    runpy.run_path(str(Path(__file__).resolve().parents[3] / "hooks/start-receipt-context.py"), run_name="__main__")
except (ImportError, OSError, ValueError):
    pass
