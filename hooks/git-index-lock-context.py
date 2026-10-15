#!/usr/bin/env python3
"""Thin Claude/Codex hook bridge to the shared index-lock diagnosis."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import git_index_lock


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=("codex", "claude"), required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        result = git_index_lock.diagnose_hook_payload(payload, args.harness)
    except (OSError, ValueError, TypeError, UnicodeError):
        return 0
    if not result or result.get("state") == "unbound":
        return 0
    event = payload.get("hook_event_name")
    if event not in {"PostToolUse", "PostToolUseFailure"}:
        return 0
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": result["message"],
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
