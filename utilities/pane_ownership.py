"""Live pane ownership; inherited pane labels are lookup hints, never proof.

No state is written. Native background attachment through a shared daemon is
unproven unless the foreground runtime itself proves the exact current session.
In particular Claude's parkedJobId names the job originally backgrounded, not
necessarily the conversation currently attached to that frontend.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def _process(pid):
    try:
        pid = int(pid)
        path = Path(f"/proc/{pid}")
        fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in {"Z", "X"} or path.stat().st_uid != os.getuid():
            return None
        return (pid, int(fields[1]), int(fields[2]), int(fields[4]),
                int(fields[5]), fields[19])  # pid, parent, pgid, tty, tpgid, start
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _runtime(pid):
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        words = [part.decode("utf-8", "replace") for part in argv if part]
        name = Path(words[0]).name
        # Native daemon children rewrite argv[0] to a process title. They
        # remain service-side processes, never physical pane owners.
        if name in {"claude bg-spare", "claude bg-pty-host"}:
            return "claude", True
        if name == "claude" or "/claude/versions/" in words[0]:
            return "claude", any(w in {"daemon", "bg-pty-host", "bg-spare"} for w in words[1:3])
        if name == "codex" or name.startswith("codex-"):
            return "codex", "app-server" in words[1:3] or name != "codex"
        if name == "opencode":
            return "opencode", any(w in {"serve", "web"} for w in words[1:3])
    except (OSError, IndexError):
        pass
    return "", False


def _caller_runtime(pid, harness):
    for _ in range(32):
        record = _process(pid)
        if not record or record[0] <= 1:
            break
        kind, service = _runtime(pid)
        if kind:
            return (record, service) if kind == harness else (None, False)
        pid = record[1]
    return None, False


def _native_session(pid, harness):
    """Exact native evidence only; no same-directory/start-time rollout guesses."""
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        if harness == "claude":
            from fleet.collectors import claude
            record = claude.read_registry(pid) or {}
            live = _process(pid)
            if live and str(record.get("procStart") or "") == live[5]:
                # A parked frontend still names its original conversation.
                if record.get("parkedJobId"):
                    return ""
                return record.get("sessionId") or ""
        elif harness == "codex":
            from fleet.collectors import codex
            cwd = os.readlink(f"/proc/{pid}/cwd")
            path = codex._proc_rollout(pid, cwd, codex._home())
            return codex._sid(path) if path else codex._registered_thread(pid) or ""
        elif harness == "opencode":
            from fleet.process_identity import process_identity, PROVEN
            found = process_identity(pid, harness)
            return found.session_id if found.confidence == PROVEN else ""
    except Exception:
        pass
    return ""


def verified_pane(pane, harness, sid=None, *, pid=None, executable=None, server=None, socket_path=None):
    """Return the hinted pane only with a live physical or exact session binding.

    Tools and hooks may have pipes for stdio; their nearest runtime must be the
    foreground process on the pane's actual controlling tty. Shared services
    instead need the requested session proven by that foreground runtime.
    Inspecting another PID cannot borrow its physical ownership to certify a
    guessed session id: an explicit PID/session query needs native session proof.
    PID/start and foreground tty/group are re-read before accepting either path.
    """
    if not pane or harness not in {"claude", "codex", "opencode"}:
        return ""
    executable = executable or shutil.which("herdr")
    if not executable:
        return ""
    try:
        command = [executable, *(["--session", server] if server else []),
                   "pane", "process-info", "--pane", str(pane)]
        done = subprocess.run(command,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=0.5,
                              env={**os.environ, "HERDR_SOCKET_PATH": str(socket_path)} if socket_path else None)
        if done.returncode:
            return ""
        info = (json.loads(done.stdout).get("result") or {}).get("process_info") or {}
        if info.get("pane_id") != pane:
            return ""
        group = int(info.get("foreground_process_group_id") or 0)
        own, service = _caller_runtime(os.getpid() if pid is None else pid, harness)
        if not own:
            return ""
        for item in info.get("foreground_processes") or []:
            foreground = _process(item.get("pid")) if isinstance(item, dict) else None
            if not foreground or not foreground[3] or foreground[2] != group or foreground[4] != group:
                continue
            kind, foreground_service = _runtime(foreground[0])
            if kind != harness or foreground_service:
                continue
            physical = (not service and own[0] == foreground[0]
                        and (pid is None or not sid or _native_session(foreground[0], harness) == sid))
            exact = bool(sid and _native_session(foreground[0], harness) == sid
                         and ((service and harness in {"codex", "opencode"})
                              or _native_session(own[0], harness) == sid))
            if (physical or exact) and _process(foreground[0]) == foreground and _process(own[0]) == own:
                return str(pane)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        pass
    return ""
