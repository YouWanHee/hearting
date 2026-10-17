#!/usr/bin/env python3
"""Claude Code's `~/.claude/projects/<name>` directory name for a cwd.

Claude Code replaces every character outside `[A-Za-z0-9]` with `-` (it runs
`/[^a-zA-Z0-9]/g` over UTF-16 code units, so a BMP character such as `연` is one
`-` and an astral one such as an emoji is two). A result longer than 200
characters is cut to 200 and suffixed with `-` plus the base-36 absolute value
of a 32-bit Java-style string hash of the original path. Verified against a
real 206-character project directory on 2026-09-23 (see the test).

This is the one encoder: the earlier `/`, `.`, `_` -> `-` copies missed every
other character (space, `+`, `@`, `~`, non-ASCII), so a transcript or memory
directory of such a cwd was never found. Readers that map a directory name back
to a cwd compare by encoding candidate paths with `encode_project_dir_component`
rather than reversing the name; a truncated (>200) name cannot be walked back
that way and stays unresolved.

Usage from shell: `python3 claude_project_dir.py <path>` prints the name.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

MAX_NAME_LENGTH = 200
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _utf16_units(text: str) -> list[int]:
    raw = text.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def encode_project_dir_component(text: str) -> str:
    """The character rule alone, without truncation (for one path component)."""
    return "".join(
        chr(unit) if unit < 128 and chr(unit).isalnum() else "-"
        for unit in _utf16_units(text)
    )


def _java_hash_base36(text: str) -> str:
    value = 0
    for unit in _utf16_units(text):
        value = (value * 31 + unit) & 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    value = abs(value)
    digits = ""
    while True:
        value, rest = divmod(value, 36)
        digits = _BASE36[rest] + digits
        if not value:
            return digits


def encode_project_dir(path: str) -> str:
    """`~/.claude/projects/` directory name for `path`, exactly as Claude Code names it."""
    path = str(path)
    name = encode_project_dir_component(path)
    if len(name) <= MAX_NAME_LENGTH:
        return name
    return f"{name[:MAX_NAME_LENGTH]}-{_java_hash_base36(path)}"


def resolve_session_transcript(session_id: str, cwd: str, *, expected_cwd: str,
                               runtime_home: str) -> tuple[Path | None, str | None]:
    """Resolve the one Claude transcript belonging to an announced attempt.

    Claude Code may name the project directory from the physical cwd, so a
    symlinked worktree also gets the exact realpath encoding, under the same home.
    """
    if (not isinstance(session_id, str) or session_id in {".", ".."}
            or not session_id or any(ch in session_id for ch in "/\\")
            or any(ord(ch) < 32 for ch in session_id)):
        return None, "invalid-supervisor-session-event"
    if (not isinstance(cwd, str) or not os.path.isabs(cwd)
            or not isinstance(expected_cwd, str) or not os.path.isabs(expected_cwd)):
        return None, "invalid-supervisor-session-event"
    if not isinstance(runtime_home, str) or not runtime_home or not os.path.isabs(runtime_home):
        return None, "invalid-supervisor-session-event"
    try:
        if os.path.realpath(cwd) != os.path.realpath(expected_cwd):
            return None, "supervisor-worktree-mismatch"
        projects = Path(runtime_home) / "projects"
        names = dict.fromkeys((encode_project_dir(cwd), encode_project_dir(os.path.realpath(cwd))))
        for name in names:
            project_dir = (projects / name).resolve()
            transcript = (project_dir / (session_id + ".jsonl")).resolve()
            if os.path.commonpath((str(projects.resolve()), str(project_dir))) != str(projects.resolve()):
                return None, "invalid-supervisor-session-event"
            if os.path.commonpath((str(project_dir), str(transcript))) != str(project_dir):
                return None, "invalid-supervisor-session-event"
            if transcript.is_file():
                return transcript, None
    except (OSError, TypeError, ValueError):
        return None, "invalid-supervisor-session-event"
    return None, None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: claude_project_dir.py <path>", file=sys.stderr)
        raise SystemExit(2)
    print(encode_project_dir(sys.argv[1]))
