#!/usr/bin/env python3
"""Project this Claude session's identity into its Herdr pane header.

Claude Code's own herdr integration (`herdr-agent-state.sh`, installed and overwritten by
herdr) reports lifecycle STATE and the session id. It reports no title, so a Claude pane
header stayed anonymous while Codex panes already carried one. This hearting-owned hook
adds the display-only half through the shared projector, so all three harnesses put the
same thing in the same order: `[<tag>] <harness>[ ⚑]` and the summary Fleet's title
worker already computed.

Fail-soft by construction: reads stdin, never writes stdout, and exits 0 on every path.
A registered worker projects nothing — the pane belongs to the interactive session.
"""
import json
import os
import sys
from pathlib import Path


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return 0
    if not os.environ.get("HERDR_PANE_ID"):
        return 0
    try:
        home = os.environ.get("AGENT_HOME") or os.environ.get("CLAUDE_HOME")
        roots = [Path(home) / "tools"] if home else []
        roots += [parent / "tools" for parent in Path(__file__).resolve().parents]
        for tools in roots:
            if not (tools / "fleet" / "herdr_projection.py").is_file():
                continue
            if str(tools) not in sys.path:
                sys.path.insert(0, str(tools))
            from fleet.herdr_projection import project
            # `report-agent-session` stays herdr's own integration's job — reporting it
            # from two sources would race two `seq` claims for one pane.
            project("claude", session_id, report_session=False)
            break
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
