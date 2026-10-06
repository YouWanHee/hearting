#!/usr/bin/env python3
"""Who is this session: the one reading of the harness identity variables.

Each harness exports its own native session id to the commands it runs:
Claude `CLAUDE_CODE_SESSION_ID` (older builds `CLAUDE_SESSION_ID`), Codex
`CODEX_THREAD_ID` (older builds `CODEX_SESSION_ID`), OpenCode
`OPENCODE_SESSION_ID`. A command may also be told which harness it runs under
(`AGENT_DISPATCH_CALLER_HARNESS`, then `AGENT_DISPATCH_CURRENT_HARNESS`).

The same question used to be answered by about a dozen local copies, each with
its own variable list, order, and answer when two harnesses' ids were both
inherited (refuse, take the first, or say nothing). This module answers it once
and reports how sure the answer is; callers keep only their own policy on top
(a launch refuses an ambiguous caller, a label falls back to "operator").

`identity()` never raises. Its `confidence` is:

- `named`: a harness was named and its id (possibly empty) is read from that
  harness's own variables.
- `sole`: no harness was named and exactly one harness's id is set.
- `ambiguous`: no harness was named and two or more harnesses' ids are set.
- `invalid`: the named harness is not one of the three.
- `none`: nothing is set.
"""
from __future__ import annotations

from dataclasses import dataclass
import os

# Per harness, the variables that carry its native session id, current name first.
SESSION_ENV: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"),
    "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID"),
    "opencode": ("OPENCODE_SESSION_ID",),
}
# Variables that name the harness a command runs under, strongest first.
HARNESS_ENV = ("AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS")
KNOWN = frozenset(("named", "sole"))


@dataclass(frozen=True)
class SessionIdentity:
    harness: str = ""
    session_id: str = ""
    source: str = ""  # the variable the answer came from; several, comma-joined, when ambiguous
    confidence: str = "none"

    @property
    def known(self) -> bool:
        return self.confidence in KNOWN


def session_ids(environ=None) -> dict[str, tuple[str, str]]:
    """`{harness: (session_id, variable)}` for every harness whose id is set."""
    env = os.environ if environ is None else environ
    found = {}
    for harness, names in SESSION_ENV.items():
        for name in names:
            value = env.get(name)
            if value:
                found[harness] = (value, name)
                break
    return found


def identity(environ=None) -> SessionIdentity:
    env = os.environ if environ is None else environ
    sessions = session_ids(env)
    for name in HARNESS_ENV:
        named = env.get(name)
        if not named:
            continue
        if named not in SESSION_ENV:
            return SessionIdentity(source=name, confidence="invalid")
        session_id, variable = sessions.get(named, ("", ""))
        return SessionIdentity(named, session_id, variable or name, "named")
    if len(sessions) > 1:
        return SessionIdentity(source=",".join(variable for _sid, variable in sessions.values()),
                               confidence="ambiguous")
    if sessions:
        harness, (session_id, variable) = next(iter(sessions.items()))
        return SessionIdentity(harness, session_id, variable, "sole")
    return SessionIdentity()


def session_label(environ=None, default: str = "operator") -> str:
    """A session id to record as who did something; never a check.

    The known identity's id when there is one, else the first id set in the
    fixed claude, codex, opencode order, else `default`.
    """
    found = identity(environ)
    if found.known and found.session_id:
        return found.session_id
    sessions = session_ids(environ)
    return next((session_id for session_id, _variable in sessions.values()), default)
