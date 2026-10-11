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
import math
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


def resolve_session_claims(claims, successors=None, *, current_source=None):
    """Resolve exact claims through directed conversation continuity, never recency.

    Adapters validate PID/start before supplying claims. A native current record
    distinguishes its current selection from historical taps and pane labels.
    It also follows a resume back to an earlier conversation. Without it,
    claims must agree through directed continuity.
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

    historical = {}
    origin = claims.get(current_source) if current_source else None
    if origin:
        historical = {source: sid for source, sid in claims.items() if sid != origin}
        ids = {origin}
    current = [sid for sid in ids if all(reaches(other, sid) for other in ids)]
    sid = current[0] if len(current) == 1 else None
    evidence = {"verdict": "resolved" if sid else "conflict" if ids else "unobserved",
                "claims": claims, "session_id": sid}
    if origin:
        evidence.update(current_source=current_source, historical_claims=historical)
    return sid, evidence


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
            if not complete and (candidates or panes is None and s.herdr_attached is True):
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


def pane_session_successors(harness, pane, cwd, *, claims=None):
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
    rows = [row for row in session_tidy._read_ledger_lines(seat)
            if row.get("harness") == harness and row.get("sid")]
    def timestamp(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and math.isfinite(value) and value > 0 else None

    starts = [dict(row, source=row.get("source") if row.get("event") == "start"
                   else row["start_source"], start_at=row.get("ts") if row.get("event") == "start"
                   else row.get("start_at")) for row in rows
              if row.get("event") == "start" or row.get("event") == "summary" and row.get("start_source")]
    starts = sorted((row for row in starts if timestamp(row.get("start_at")) is not None),
                    key=lambda row: row["start_at"])
    edges = {}
    def same_repository(row):
        return resolve_gitdir((summary.get((harness, row["sid"])) or {}).get("cwd"))[1] == repository

    for older, newer in zip(starts, starts[1:]):
        same_repo = all(same_repository(row) for row in (older, newer))
        if (same_repo and newer.get("source") == "clear" and older["sid"] != newer["sid"]
                and older["start_at"] < newer["start_at"]):
            edges.setdefault(older["sid"], set()).add(newer["sid"])
    # Older folds kept the seat's ordered session history but dropped START
    # sources. A later explicit clear closes those historical claims on this
    # same seat/repository; neither a startup/fork nor a display alias does so.
    folded = [row for row in rows if row.get("event") == "summary" and not row.get("start_source")
              and claims is not None and row["sid"] in claims]
    for row in starts:
        if row.get("source") == "clear" and same_repository(row):
            for older in folded:
                last_seen = timestamp((summary.get((harness, older["sid"])) or {}).get("last_seen"))
                if (older["sid"] != row["sid"] and same_repository(older)
                        and last_seen is not None and last_seen < row["start_at"]):
                    edges.setdefault(older["sid"], set()).add(row["sid"])
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
