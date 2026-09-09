#!/usr/bin/env python3
"""The canonical lock-order table required by PRD §13.53.4(3).

`§13.53.4(3)` is explicit that "we pin it in one place" is not enough -- two
implementations with *different* orders could both claim to satisfy that. So
the implementation cycle must register a canonical total order plus an
explicit acquire/release API, and until that table exists the fence contract
may not be claimed at all.

This module is that table. It owns four obligations from the PRD:

1. A single total order over the locks a terminal commit can touch. A writer
   that declares against this table cannot acquire out of order or re-enter:
   both are refused, typed, at the moment of the bad acquisition.

   The scope of that guarantee is exactly the set of declaring writers, and
   `ENFORCED_COMPLETE` records which ranks have *no* undeclared writer left --
   a rank outside it constrains only the code that opted in. Saying "every
   writer" without that distinction would be a false guarantee: the registry
   `jobs.log.lock` is taken by dozens of ordinary dispatch writers that have no
   reason to know about the terminal transaction, and only the terminal-claim
   fence declares it.
2. Named `acquired()` / `enter()` / `leave()` boundaries, so lock ownership is
   an explicit state rather than an implicit consequence of holding an fd.
3. The lock that must be *released* before `finalize()` is re-entered, and a
   typed refusal (never a 30-second flock timeout) when a caller re-enters it
   while holding it.
4. The re-verify point: `assert_held()` lets a mutation site prove it still
   owns the fence it checked under.

The bookkeeping is process-local and advisory-of-advisory: it does not replace
the OS `flock` calls, it *describes* them, so a violation is reported as a
typed error at the moment of the bad acquisition instead of surfacing later as
a deadlock, a timeout, or a silently interleaved write.
"""

from __future__ import annotations

import threading
from typing import Iterator, Optional

# --- the canonical total order ------------------------------------------
#
# Rank ascending == acquisition order. The established order, preserved from
# the implementation that landed with `2e0fc2b0`, is:
#
#     node completion  ->  jobs  ->  producer admission
#
# `terminal-commit-state` sits below producer admission because the terminal
# transaction's state CAS is deliberately taken *after* the producer lock has
# been released (see `dispatch_terminal_commit._advance_state`): it is a small
# publication, not part of the producer critical section.
LOCK_ORDER: tuple[tuple[int, str, str], ...] = (
    (1, "node-completion", "node/attempt completion admission -- a CROSS-PROCESS precheck stage"),
    (2, "jobs", "the registry `jobs.log.lock` -- the one terminal-claim fence"),
    (3, "producer-admission", "artifact_admission admission mutex for one artifact root"),
    (4, "terminal-commit-state", "terminal transaction `state.lock` CAS, taken after producer release"),
)

RANK: dict[str, int] = {name: rank for rank, name, _ in LOCK_ORDER}
DESCRIPTION: dict[str, str] = {name: text for _, name, text in LOCK_ORDER}

# Which ranks this module can actually enforce.
#
# `node-completion` is in the total order because the contract's acquisition
# order is `node completion -> jobs -> producer admission`, but it is *not* an
# in-process mutex any caller here holds: node/batch/chain admission happens
# across a subprocess boundary, where the check is a precheck and the real
# fence is the adapter's registration critical section under the jobs lock.
# Declaring it enforced would be a false guarantee, so it is listed as ordered
# and named as unenforceable in-process. If a future change ever takes a real
# in-process node-completion lock, move it into `DECLARED_IN_PROCESS` -- do not
# quietly rely on rank 1 constraining anything today.
CROSS_PROCESS_PRECHECK: frozenset[str] = frozenset({"node-completion"})
DECLARED_IN_PROCESS: tuple[str, ...] = tuple(
    name for _, name, _ in LOCK_ORDER if name not in CROSS_PROCESS_PRECHECK)

# Ranks where the census finds no undeclared in-process writer of the physical
# lock, so ordering is guaranteed for the whole rank rather than for opt-ins.
#
# `producer-admission` qualifies: `.runtime/artifact-admission/v1/lock.flock`
# is opened by exactly two modules (`artifact_admission`, and the sealed-restore
# path that re-implements the same acquisition), and both declare.
#
# `jobs` does not, and deliberately so: the registry lock is the ordinary
# serialization point for dozens of dispatch writers, and only the terminal
# claim fence needs to be ordered against the producer mutex. `terminal-commit-
# state` is private to the terminal transaction, so its single writer declares.
ENFORCED_COMPLETE: frozenset[str] = frozenset({"producer-admission", "terminal-commit-state"})

# Physical lock files a rank in `ENFORCED_COMPLETE` corresponds to, for the
# acceptance census. Counting from the *acquisition* side is the only way to
# notice a module that re-implements the same lock without declaring.
ENFORCED_LOCK_FILES: dict[str, str] = {
    "producer-admission": "lock.flock",
    "terminal-commit-state": "state.lock",
}

# §13.53.4(3): "the table names the lock that must be released before entering
# `finalize()`". Re-entering the producer admission mutex is the concrete
# deadlock this forbids.
RELEASE_BEFORE_FINALIZE: tuple[str, ...] = ("producer-admission",)


class LockOrderError(RuntimeError):
    """A typed refusal carrying a closed reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


# Held locks are per-thread: two threads in one process genuinely may hold
# different locks, and flock's own semantics are per-open-file-description.
_STATE = threading.local()


def _held() -> list[str]:
    stack = getattr(_STATE, "stack", None)
    if stack is None:
        stack = []
        _STATE.stack = stack
    return stack


def held() -> tuple[str, ...]:
    """The locks this thread currently declares, in acquisition order."""
    return tuple(_held())


def enter(name: str) -> None:
    """Declare `name` acquired. Refuses re-entry and reverse-order acquisition.

    Call this immediately *after* the underlying flock succeeds, so a lock this
    module reports as held is one the OS also considers held.
    """
    if name not in RANK:
        raise LockOrderError("lock-unregistered", name)
    stack = _held()
    if name in stack:
        raise LockOrderError("lock-reentry-forbidden", name)
    if stack and RANK[name] <= RANK[stack[-1]]:
        raise LockOrderError("lock-order-violation", f"{stack[-1]}->{name}")
    stack.append(name)


def leave(name: str) -> None:
    """Declare `name` released.

    Releasing out of order is itself a violation: the stack discipline is what
    makes "no lock is dropped between the re-verify point and the mutation"
    checkable.
    """
    if name not in RANK:
        raise LockOrderError("lock-unregistered", name)
    stack = _held()
    if not stack or stack[-1] != name:
        raise LockOrderError("lock-release-out-of-order", name)
    stack.pop()


def assert_not_held(name: str, operation: str) -> None:
    """§13.53.4(3): refuse a re-entrant `finalize()` immediately and typed.

    Without this the second acquisition blocks on `flock` until the admission
    timeout expires and surfaces as a generic timeout, which reads as load
    rather than as the contract violation it is.
    """
    if name in _held():
        raise LockOrderError("lock-reentry-forbidden", f"{operation}:{name}")


def assert_held(name: str, operation: str) -> None:
    """The re-verify point: prove the fence checked earlier is still owned."""
    if name not in _held():
        raise LockOrderError("lock-not-held", f"{operation}:{name}")


class acquired:
    """Context manager form of `enter`/`leave` for a lock taken elsewhere.

    Wrap the region in which the underlying flock is genuinely held::

        fd = _acquire_lock(root, timeout)
        with acquired("producer-admission"):
            ...
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> "acquired":
        enter(self.name)
        return self

    def __exit__(self, *_exception: object) -> bool:
        leave(self.name)
        return False


def reset_for_test() -> None:
    """Drop this thread's declared stack. Test-only recovery hook."""
    _STATE.stack = []


def table() -> list[dict[str, object]]:
    """The registered table, for evidence and for the acceptance fixture."""
    return [{"rank": rank, "lock": name, "description": text}
            for rank, name, text in LOCK_ORDER]
