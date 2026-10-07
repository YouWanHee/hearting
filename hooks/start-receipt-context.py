#!/usr/bin/env python3
"""Native post-tool context projection of a session's saved start receipt."""
import json
import os
from pathlib import Path
import sys

def main():
    try:
        harness = "codex" if "--codex" in sys.argv else "claude"
        roots = (Path(os.environ.get("AGENT_HOME") or "/nonexistent"),
                 Path(__file__).resolve().parents[1], Path.home() / ("." + harness) / "hearting")
        root = next((r for r in roots if (r / "core/CORE.md").is_file()), None)
        if root is None:
            return
        sys.path.insert(0, str(root / "utilities"))
        from start_receipt import context
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("hook_event_name", "PostToolUse") != "PostToolUse":
            return
        sid = payload.get("session_id") or payload.get("thread_id") or payload.get("sessionID") or payload.get("threadID")
        if not isinstance(sid, str) or not sid:
            return
        text = context(harness, sid)
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}, ensure_ascii=False))
    except (ImportError, OSError, ValueError, TypeError):
        pass


if __name__ == "__main__":
    main()
