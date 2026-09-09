"""Fail-soft projection of an existing Codex session into an existing Herdr pane.

The shape of that projection — `[<tag>] <harness>[ ⚑]` plus the existing summary — is
shared with Claude and OpenCode in `tools/fleet/herdr_projection.py`, because a pane
header that means one thing in one harness and another thing in the next is exactly the
identity confusion it exists to remove. This module stays as the Codex hook entry point
its two lifecycle hooks already call.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
from fleet.herdr_projection import project as _project  # noqa: E402


def project(payload: dict[str, Any] | None, session_id: str, *, worker: bool = False) -> bool:
    return _project("codex", session_id, worker=worker)
