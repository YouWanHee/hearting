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
import sys
from pathlib import Path

# The managed-current compatibility spelling. It reconnects an already
# populated release-adjacent store, but an *empty* directory there is never a
# creation/import target: the release tree is immutable, not runtime state.
_MANAGED_CURRENT_SEGMENTS = ("hearting", "current", "memory")
_CANONICAL_SEGMENTS = ("hearting", "memory")


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
        return Path(value)
    return home / ".local" / "share"


def _candidate_list(env, home):
    """Return [(original_path, is_managed_current), ...] in R1 order.

    A candidate sourced from an environment variable is skipped entirely when
    that variable is empty or unset; HOME-derived candidates are always
    present because HOME is always set.
    """
    candidates = []
    agent_home = env.get("AGENT_HOME")
    if agent_home:
        candidates.append((Path(agent_home) / "memory", False))
    claude_home = env.get("CLAUDE_HOME")
    if claude_home:
        candidates.append((Path(claude_home) / "memory", False))
    candidates.append((home / "hearting" / "memory", False))
    candidates.append((home / "agent_setting" / "memory", False))
    candidates.append((home / ".claude" / "memory", False))
    xdg_data = _xdg_data_home(env, home)
    candidates.append((xdg_data.joinpath(*_MANAGED_CURRENT_SEGMENTS), True))
    candidates.append((xdg_data.joinpath(*_CANONICAL_SEGMENTS), False))
    return candidates


def _probe_populated(candidate):
    """Return True/False, or raise StoreResolutionError on a real probe error.

    Ordinary absence and the "memory.db is a directory" wrong-type case are
    not errors -- they simply mean "not populated". A permission failure or
    symlink loop while checking is a real error and must abort the scan
    rather than silently choosing a different candidate.
    """
    db = candidate / "memory.db"
    try:
        if db.is_symlink():
            if not db.exists():
                return False  # dangling symlink: not populated, not an error
            resolved = db.resolve(strict=True)
            return resolved.is_file()
        if not db.exists():
            return False
        return db.is_file()
    except OSError as exc:
        raise _diagnostic(candidate) from exc


def _resolved_identity(candidate):
    db = candidate / "memory.db"
    try:
        return str(db.resolve(strict=True))
    except OSError as exc:
        raise _diagnostic(candidate) from exc


def resolve_store(env=None, home=None):
    """Resolve the one memory store directory per core/MEMORY.md Section 7.0.

    Raises StoreConflict when two or more distinct databases are populated,
    or StoreResolutionError on the first real probe/canonicalization failure.
    Never creates, moves, copies, or imports anything.
    """
    if env is None:
        env = os.environ
    if home is None:
        home = Path(os.path.expanduser("~"))
    else:
        home = Path(home)

    override = env.get("MEM_STORE")
    if override:
        return Path(override)

    candidates = _candidate_list(env, home)

    populated_identity = {}  # resolved memory.db path -> original candidate path
    for candidate, _is_managed_current in candidates:
        if not _probe_populated(candidate):
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
        try:
            exists = candidate.exists() or candidate.is_symlink()
        except OSError as exc:
            raise _diagnostic(candidate) from exc
        if exists:
            return candidate
    return candidates[-1][0]


def main(argv):
    del argv
    try:
        store = resolve_store()
    except StoreResolutionError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    print(str(store))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
