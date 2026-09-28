#!/usr/bin/env python3
"""Runtime-neutral hook bridge for worker compaction records."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "utilities" / "worker-state-ledger.py"


def _active() -> bool:
    return (
        os.environ.get("AGENT_DISPATCH_STAGE_AUTHORITY") == "0"
        or bool(os.environ.get("AGENT_DISPATCH_SUBSESSION_ID"))
    )


def _binding() -> tuple[str, str]:
    path = os.environ.get("AGENT_WORKER_STATE_LEDGER", "")
    attempt = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if not path or not attempt:
        raise ValueError("worker sub-session ledger binding missing")
    return path, attempt


def _run(action: str) -> subprocess.CompletedProcess[str]:
    ledger, attempt = _binding()
    command = [sys.executable, str(LEDGER), action, "--path", ledger, "--attempt-id", attempt]
    return subprocess.run(command, text=True, capture_output=True, check=False)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("compact-before", "compact-after"))
    args = p.parse_args()
    if not _active():
        return 0
    try:
        result = _run(args.action)
        if result.returncode:
            raise ValueError((result.stderr or result.stdout).strip())
        if args.action == "compact-after" and result.stdout:
            print(result.stdout, end="")
        return 0
    except ValueError as exc:
        print(f"worker-state-hook: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
