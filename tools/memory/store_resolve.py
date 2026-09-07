#!/usr/bin/env python3
"""Shared memory-store resolution contract (R0-R5).

Both `mem.py` (this module) and `utilities/memory-store.sh` (its POSIX-shell
realization) must agree on which directory holds the one live `memory.db`.
This module is stdlib-only, read-only, and creates nothing: it inspects
candidate directories for an existing `memory.db` and returns a decision, it
never opens the database, moves/copies/imports state, or mutates the
filesystem. See core/MEMORY.md Section 7.0 for the governing contract.

`resolve_store()` is imported by `mem.py`. The `__main__` CLI below exists
only so `store-resolve-parity.test.sh` can compare this implementation
against the shell realization by invoking each as a subprocess and diffing
stdout/exit status/stderr.
"""
import os
import stat
import sys
from pathlib import Path

class _StorePath(type(Path())):
    """Keep the selected spelling at the API boundary, with normal Path children."""

    def __str__(self):
        return getattr(self, "_store_spelling", super().__str__())


class StoreResolutionError(Exception):
    """Resolution could not produce one store (probe/canonicalization error)."""


class StoreConflict(StoreResolutionError):
    """Two or more distinct populated memory.db paths were found."""

    def __init__(self, paths):
        self.paths = list(paths)
        joined = ", ".join(str(p) for p in self.paths)
        super().__init__(
            "memory store resolution error: multiple memory databases found: "
            f"{joined}; set MEM_STORE to one of them"
        )


def _diagnostic(candidate):
    return StoreResolutionError(f"memory store resolution error: {candidate}")


def _xdg_data_home(env, home):
    value = env.get("XDG_DATA_HOME")
    if value:
        return value
    return f"{home}/.local/share"


def _candidate_list(env, home):
    """Return [(original_path, is_managed_current), ...] in R1 order.

    A candidate sourced from an environment variable is skipped entirely when
    that variable is empty or unset; HOME-derived candidates are always
    present because HOME is always set.
    """
    candidates = []
    agent_home = env.get("AGENT_HOME")
    if agent_home:
        candidates.append((f"{agent_home}/memory", False))
    claude_home = env.get("CLAUDE_HOME")
    if claude_home:
        candidates.append((f"{claude_home}/memory", False))
    candidates.append((f"{home}/hearting/memory", False))
    candidates.append((f"{home}/agent_setting/memory", False))
    candidates.append((f"{home}/.claude/memory", False))
    xdg_data = _xdg_data_home(env, home)
    candidates.append((f"{xdg_data}/hearting/current/memory", True))
    candidates.append((f"{xdg_data}/hearting/memory", False))
    return candidates


def _probe_mode(path, candidate):
    """Path predicates suppress some OS errors; stat preserves their cause."""
    try:
        return os.stat(path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return 0
    except OSError as exc:
        raise _diagnostic(candidate) from exc


def _resolved_identity(candidate):
    db = Path(candidate) / "memory.db"
    try:
        return str(db.resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise _diagnostic(candidate) from exc


def _resolve_store_text(env=None, home=None):
    """Resolve the one memory store directory per core/MEMORY.md Section 7.0.

    Raises StoreConflict when two or more distinct databases are populated,
    or StoreResolutionError on the first real probe/canonicalization failure.
    Never creates, moves, copies, or imports anything.
    """
    if env is None:
        env = os.environ
    if home is None:
        home = env.get("HOME") or os.path.expanduser("~")
    home = str(home)

    override = env.get("MEM_STORE")
    if override:
        return override

    candidates = _candidate_list(env, home)

    populated_identity = {}  # resolved memory.db path -> original candidate path
    for candidate, _is_managed_current in candidates:
        if not stat.S_ISREG(_probe_mode(f"{candidate}/memory.db", candidate)):
            continue
        identity = _resolved_identity(candidate)
        if identity not in populated_identity:
            populated_identity[identity] = candidate

    if len(populated_identity) >= 2:
        raise StoreConflict(populated_identity.values())
    if len(populated_identity) == 1:
        return next(iter(populated_identity.values()))

    # R5 -- no database anywhere: first existing candidate directory,
    # excluding the managed-current compatibility spelling, else canonical XDG.
    for candidate, is_managed_current in candidates:
        if is_managed_current:
            continue
        if stat.S_ISDIR(_probe_mode(candidate, candidate)):
            return candidate
    return candidates[-1][0]


def resolve_store(env=None, home=None) -> Path:
    """Return the selected store as a Path without resolving its spelling."""
    spelling = _resolve_store_text(env, home)
    selected = _StorePath(spelling)
    selected._store_spelling = spelling
    return selected


def main(argv):
    del argv
    try:
        # Preserve even lexical spellings (./, repeated /) at the text boundary.
        store = _resolve_store_text()
    except StoreResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(str(store))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
