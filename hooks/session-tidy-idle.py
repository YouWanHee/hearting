#!/usr/bin/env python3
"""Claude Stop bridge for the shared session-tidy idle observer; always silent."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def active_root() -> Path:
    for value in (os.environ.get("AGENT_HOME", ""),):
        candidate = Path(value) if value else None
        if candidate and (candidate / "core" / "CORE.md").is_file():
            return candidate
    resolver = HERE / "utilities" / "agent-home.sh"
    if resolver.is_file():
        try:
            result = subprocess.run(["sh", str(resolver)], capture_output=True, text=True, timeout=2)
            candidate = Path(result.stdout.strip()) if result.returncode == 0 else None
            if candidate and (candidate / "core" / "CORE.md").is_file():
                return candidate
        except (OSError, subprocess.SubprocessError):
            pass
    return HERE


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        root = active_root()
        utilities = str(root / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        from session_tidy_idle import observe_payload
        observe_payload("claude", payload)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
