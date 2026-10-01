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
# Each pattern is the exact name an old writer created, as of 60fea1f7^ (the commit
# before the distiller was retired). Regular files only: the old `.distill-budget-N`
# slots were mkdir'd directories, so they are not listed and are never removed here.
RETIRED_NAMES = tuple(re.compile(pattern + "$") for pattern in (
    rf"\.distill-state-{_ID}",             # mem.py _distill_state_path(sid)
    rf"\.turn-state-{_ID}",                # hooks/mem-turn-nudge.sh STATE
    rf"\.codex-turn-state-{_ID}",          # adapters/codex/bin/preflight.sh turn counter
    rf"\.distill-err-{_ID}",               # hooks/mem-distill-dispatch.sh ERRLOG (always -<sid>)
    r"\.distill-failures\.log",            # hooks/mem-distill-dispatch.sh FAILLOG
    rf"\.(?:codex|opencode)-distill-(?:out|prompt)-{_ID}",  # adapters/*/bin/distill-worker.sh
    rf"\.opencode-distill-stamp-{_ID}",    # adapters/opencode/bin/preflight.sh debounce stamp
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
                # destructive-ok: reason=remove one retired distiller state file; boundary=one allowlisted regular file directly inside the memory store
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
