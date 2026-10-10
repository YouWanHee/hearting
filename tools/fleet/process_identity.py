"""Which harness session a process is, in the one identity record shape.

Each harness proves a process's session from its own native source -- the
translation stays in its collector:

- Claude: `~/.claude/sessions/<pid>.json`, else the statusline tap matched by
  pid and start time (`collectors.claude.session_id_of_process`);
- Codex: the rollout the process holds open, the managed registry, or the
  start-time match (`collectors.codex.session_id_of_process`);
- OpenCode: the pane's own TUI selection record
  (`collectors.opencode.session_of_process`). The `--session` a pane was
  started with is reported as `started-on`, not proof: the TUI can switch
  sessions after start, and only the selection record follows that.

The answer is a `session_identity.SessionIdentity` -- the same record the
environment reading gives -- with `confidence="proven"` when the process
proved its session and `"harness-only"` when only the harness is known. The
caller decides what an unproven session id may do; nothing here guesses.

Import contract: like `route_chain`, importable with only `tools` on sys.path;
`utilities/` is added lazily.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

PROVEN = "proven"
STARTED_ON = "started-on"
HARNESS_ONLY = "harness-only"
_UNPROVEN_SOURCES = {"opencode-argv": STARTED_ON}


def process_role(*, tty=None, registered=False, session_backend=False, native_session=False,
                 invocation="unknown"):
    """One role from native invocation and observed ownership, without name exceptions.

    Native command mode is an adapter translation, not an inherited environment hint.
    Unknown reads and not-yet-published headless conversations remain unknown.
    """
    if session_backend:
        return "session-backend"
    if invocation == "command" and not native_session:
        return "service"
    if registered:
        return "registered-worker"
    if native_session:
        return "conversation"
    if tty and tty != "?":
        return "conversation"
    return "unknown"


def resolve_session_claims(claims, successors=None):
    """Resolve exact claims through directed conversation continuity, never recency.

    Adapters validate PID/start before supplying claims. A unique claim reachable
    from every other claim is the current conversation; unrelated claims stay unknown.
    """
    claims = {source: sid for source, sid in claims.items() if sid}
    ids = set(claims.values())
    successors = successors or {}

    def reaches(origin, target):
        pending, seen = [origin], set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(successors.get(current, ()))
        return False

    current = [sid for sid in ids if all(reaches(other, sid) for other in ids)]
    sid = current[0] if len(current) == 1 else None
    return sid, {"verdict": "resolved" if sid else "conflict" if ids else "unobserved",
                 "claims": claims, "session_id": sid}


def pane_process_claims(sessions, panes, bindings, *, complete=True):
    """Consume the existing foreground pane observation; never issue another probe."""
    from .collectors import procscan
    out = {}
    for s in sessions:
        pane_ids = bindings.get(s.pid, set())
        if (not s.proc_start or s.is_child or s.app_server
                or procscan.read_proc_start(s.pid) != s.proc_start):
            continue
        if len(pane_ids) != 1:
            candidates = [p for p in panes or [] if p.get("agent") == s.harness
                          and p.get("cwd") and s.cwd
                          and os.path.realpath(p["cwd"]) == os.path.realpath(s.cwd)]
            if not complete and candidates:
                out[s.pid] = {"verdict": "unobserved", "proc_start": s.proc_start}
            continue
        pane_id = next(iter(pane_ids))
        values = set()
        for p in panes or []:
            native = p.get("agent_session")
            if (p.get("pane_id") == pane_id and p.get("agent") == s.harness
                    and isinstance(native, dict) and native.get("agent") == s.harness
                    and isinstance(native.get("value"), str) and native["value"]):
                values.add(native["value"])
        if len(values) == 1:
            out[s.pid] = {"session_id": values.pop(), "pane": pane_id, "proc_start": s.proc_start}
    return out


def pane_session_successors(harness, pane, cwd):
    """Directed native clear transitions from the existing seat ledger.

    Display aliases are bidirectional and cannot supply these edges. Only a
    recorded clear start follows the previous native start on that same seat.
    """
    _record()  # make the common utilities importable
    import session_tidy
    from .gitinfo import resolve_gitdir
    repository = resolve_gitdir(cwd)[1]
    if not repository:
        return {}
    seat = session_tidy._pane_seat_of(pane)
    summary = session_tidy.session_summary(seat)
    starts = [row for row in session_tidy._read_ledger_lines(seat)
              if row.get("event") == "start" and row.get("harness") == harness and row.get("sid")]
    edges = {}
    for older, newer in zip(starts, starts[1:]):
        same_repo = all(resolve_gitdir((summary.get((harness, row["sid"])) or {}).get("cwd"))[1]
                        == repository for row in (older, newer))
        if same_repo and newer.get("source") == "clear" and older["sid"] != newer["sid"]:
            edges.setdefault(older["sid"], set()).add(newer["sid"])
    return edges


def finalize_process_roles(sessions, jobs):
    """Publish one role after native identities and registered process rows are observed."""
    from .collectors import procscan, herdr
    from . import session_registry
    for s in sessions:
        native = session_registry.read(s.harness, s.pid)
        proven = bool(native and native.get("sessionId") and s.proc_start
                      and str(native.get("procStart") or "") == s.proc_start)
        evidence = s.session_identity_evidence or {}
        if s.harness == "claude":
            proven = evidence.get("verdict") == "resolved"
        elif s.harness == "codex":
            proven = proven or bool(s.session_id and getattr(s, "_fd_owner", False))
        elif s.harness == "opencode" and not proven and getattr(s, "_tty", None) == "?":
            from .collectors import opencode
            _sid, source = opencode.session_of_process(s.pid)
            proven = bool(_sid and source == "opencode-tui-selection")
        registered = False
        if s.attempt_id and getattr(s, "_invocation_mode", "unknown") == "session":
            for j in jobs:
                if (j.attempt_id == s.attempt_id and j.harness == s.harness and j.pid and j.proc_start
                        and procscan.read_proc_start(j.pid) == j.proc_start
                        and procscan.read_proc_start(s.pid) == s.proc_start
                        and herdr.pid_in_panes(s.pid, set(), {j.pid}, max_depth=16)):
                    registered = True
                    break
        s.process_role = process_role(
            tty=getattr(s, "_tty", None), registered=registered,
            session_backend=bool(s.app_server and s.managed_dir), native_session=proven,
            invocation=getattr(s, "_invocation_mode", "unknown"))
    sessions[:] = [s for s in sessions if s.process_role != "service"]


def _record():
    utilities = str(Path(__file__).resolve().parents[2] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    from session_identity import SessionIdentity
    return SessionIdentity


def process_identity(pid, harness, *, live_codex=None):
    """The identity record `harness`'s own source proves for `pid`."""
    record = _record()
    session_id, source = None, ""
    try:
        if harness == "claude":
            from .collectors import claude
            session_id, source = claude.session_id_of_process(pid), "claude-session-registry"
        elif harness == "codex":
            from .collectors import codex
            session_id, source = codex.session_id_of_process(pid, live_codex), "codex-process"
        elif harness == "opencode":
            from .collectors import opencode
            session_id, source = opencode.session_of_process(pid)
    except Exception:
        session_id = None
    if session_id:
        return record(harness, session_id, source, _UNPROVEN_SOURCES.get(source, PROVEN))
    return record(harness, "", "", HARNESS_ONLY)
