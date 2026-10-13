#!/usr/bin/env python3
"""Codex bridge for the portable worker state hook."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(os.environ.get("AGENT_HOME") or Path(__file__).resolve().parents[3])
active = os.environ.get("AGENT_DISPATCH_STAGE_AUTHORITY") == "0" or bool(os.environ.get("AGENT_DISPATCH_SUBSESSION_ID"))
action = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    result = subprocess.run(
        [sys.executable, str(ROOT / "utilities/worker-state-hook.py"), *sys.argv[1:]],
        input=None if active else sys.stdin.read(), text=True, capture_output=True, check=False,
        timeout=None if active else 5,
    )
except (OSError, subprocess.SubprocessError):
    raise SystemExit(0 if not active else 2)
if active:
    sys.stdout.write(result.stdout or "")
    sys.stderr.write(result.stderr or "")
    raise SystemExit(result.returncode)
harness = next((sys.argv[i + 1] for i in range(len(sys.argv) - 1) if sys.argv[i] == "--harness"), "")
if result.returncode == 0 and action == "compact-before" and harness in ("claude", "opencode"):
    sys.stdout.write(result.stdout or "")
raise SystemExit(0)
