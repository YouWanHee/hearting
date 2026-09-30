"""One-time removal of the memory files the retired session distiller left behind.

The distiller, its turn counters and its session-end debounce stamps are gone,
but their per-session state files still sit at the top level of a memory store
(hundreds of them on a long-lived install). Nothing reads them any more.

Removal is by allowlist, never by sweep: only *regular files* directly inside the
store whose whole name matches a retired pattern go. ``memory.db*``, ``dump.jsonl``,
``backups*``, ``.git``, journals, exchange and history files, directories,
symlinks and every other name are left alone. Every failure is silent.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import paths

_ID = r"[A-Za-z0-9._-]+"
RETIRED_NAMES = tuple(re.compile(pattern + "$") for pattern in (
    rf"\.distill-state-{_ID}",
    rf"\.turn-state-{_ID}",
    rf"\.codex-turn-state-{_ID}",
    rf"\.opencode-turn-state-{_ID}",
    rf"\.distill-err(?:-{_ID})?",
    rf"\.distill-budget-{_ID}",
    r"\.distill-failures\.log",
    rf"\.(?:codex|opencode)-distill-(?:state|out|prompt|stamp)-{_ID}",
))


def default_store() -> Path:
    """The store ``mem.py`` uses when nothing points elsewhere."""
    explicit = os.environ.get("MEM_STORE")
    if explicit:
        return Path(explicit)
    legacy = paths.agent_home() / "memory"
    if legacy.exists() or legacy.is_symlink():
        return legacy
    raw = os.environ.get("XDG_DATA_HOME")
    data_home = Path(raw) if raw and Path(raw).is_absolute() else Path.home() / ".local" / "share"
    return data_home / "hearting" / "memory"


def retire(store=None) -> int:
    """Delete retired state files from the top level of ``store``; returns how many went."""
    try:
        root = Path(store) if store is not None else default_store()
        names = os.listdir(root)
    except (OSError, ValueError):
        return 0
    removed = 0
    for name in names:
        if not any(pattern.match(name) for pattern in RETIRED_NAMES):
            continue
        path = root / name
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
