"""Small, shared live-session number assignments; runtime identity stays untouched."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

_HEX = re.compile(r"^[0-9a-f]{2}$")


def _path():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "agent-fleet/tag-assignments.json"


def _read(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("assignments"), list):
        raise ValueError("invalid tag assignments")
    rows = {}
    for row in data["assignments"]:
        if not isinstance(row, dict):
            raise ValueError("invalid tag assignment")
        key = (row.get("harness"), row.get("session_id"))
        seen = row.get("first_seen")
        started = row.get("started_at")
        if (key[0] not in {"claude", "codex", "opencode"}
                or not isinstance(key[1], str) or not key[1]
                or not isinstance(row.get("tag"), str) or not _HEX.fullmatch(row["tag"])
                or not isinstance(seen, (int, float)) or isinstance(seen, bool) or not math.isfinite(seen)
                or not isinstance(started, (int, float)) or isinstance(started, bool) or not math.isfinite(started)
                or key in rows):
            raise ValueError("invalid tag assignment")
        rows[key] = row
    return rows


def assigned_tag(harness, sid):
    """Atomic snapshot read. Missing/corrupt state uses the resolver's hash fallback."""
    try:
        row = _read(_path()).get((harness, sid))
        return row["tag"] if row else None
    except (OSError, ValueError, TypeError):
        return None


def _started(harness, sid, started=None):
    if isinstance(started, (int, float)) and not isinstance(started, bool) and math.isfinite(started):
        return started
    # Codex thread UUIDv7 encodes creation milliseconds. This also orders the
    # pre-install same-hash pair when Herdr exposes no creation timestamp.
    if harness == "codex" and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f-]{18}", sid):
        return int(sid[:8] + sid[9:13], 16) / 1000
    return time.time()


def _inventory(sessions):
    from .session_handle import resolve_tag
    from .collectors.herdr import list_agents
    live = {}
    for sess in sessions:
        harness, sid = sess.harness, getattr(sess, "session_id", None)
        if harness not in {"claude", "codex", "opencode"} or not isinstance(sid, str) or not sid:
            continue
        row = {"harness": harness, "session_id": sid,
               "started_at": _started(harness, sid, getattr(sess, "started_at", None)),
               "pid": getattr(sess, "pid", None), "proc_start": getattr(sess, "proc_start", None)}
        if harness == "claude":
            row["tag"] = getattr(sess, "session_tag", None) or resolve_tag(harness, sid)
        live[(harness, sid)] = row
    agents = list_agents()
    for agent in agents or []:
        identity = agent.get("agent_session") or {}
        if not isinstance(identity, dict) or identity.get("kind") != "id":
            continue
        harness, sid = agent.get("agent"), identity.get("value")
        if harness not in {"claude", "codex", "opencode"} or not isinstance(sid, str) or not sid:
            continue
        row = live.setdefault((harness, sid), {"harness": harness, "session_id": sid,
                              "started_at": _started(harness, sid)})
        row["pane_id"] = agent.get("pane_id")
        if harness == "claude" and not row.get("tag"):
            row["tag"] = resolve_tag(harness, sid)
    return live, agents is not None


def _keep(row, panes_known):
    """Release only proven exited reservations, never uncertain live processes."""
    pid, start = row.get("pid"), row.get("proc_start")
    if pid and start:
        from .collectors.procscan import read_proc_start
        actual = read_proc_start(pid)
        if actual is not None:
            return str(actual) == str(start)
        return Path("/proc/%s" % pid).exists()
    return not (panes_known and row.get("pane_id"))


def refresh(sessions):
    """Fleet's ordinary collection seeds/refreshes all assignments under one lock.

    Unknown consumers only read the resulting snapshot. A corrupt file is left
    untouched; a later normal collection can seed a missing file automatically.
    """
    from .session_handle import minted_tag
    path = _path()
    try:
        import fcntl
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / "tag-assignments.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            live, panes_known = _inventory(sessions)
            try:
                old = _read(path)
            except FileNotFoundError:
                old = {}
            for key, row in old.items():
                if key not in live and _keep(row, panes_known):
                    live[key] = row.copy()
            now = time.time()
            for key, row in live.items():
                previous = old.get(key, {})
                if previous:
                    row["started_at"] = previous["started_at"]
                row["first_seen"] = previous.get("first_seen", now)
                if previous and row["harness"] != "claude":
                    row["tag"] = previous["tag"]
                elif row["harness"] != "claude":
                    row["tag"] = minted_tag(row["session_id"])
            for row in live.values():
                if row["harness"] == "claude" and not _HEX.fullmatch(str(row.get("tag") or "")):
                    row.pop("tag", None)
            fixed = {row["tag"] for row in live.values() if row["harness"] == "claude" and row.get("tag")}
            taken = set(fixed)
            movable = [(key, row) for key, row in live.items() if row["harness"] != "claude"]
            movable.sort(key=lambda item: (0 if item[0] in old else 1,
                                           item[1]["first_seen"], item[1]["started_at"], item[0]))
            # Reserve every surviving old number before placing newcomers or a
            # displaced number. A new hash must never evict an existing session.
            displaced = []
            for key, row in movable:
                tag = row.get("tag")
                if tag and tag not in taken:
                    taken.add(tag)
                else:
                    displaced.append(row)
            for row in displaced:
                base = int(minted_tag(row["session_id"]), 16)
                tag = next((f"{(base + step) % 256:02x}" for step in range(256)
                            if f"{(base + step) % 256:02x}" not in taken), None)
                if tag is None:
                    row.pop("tag", None)  # namespace exhausted: honest hash fallback
                else:
                    row["tag"] = tag
                    taken.add(tag)
            rows = [row for row in live.values() if row.get("tag")]
            if old != {(row["harness"], row["session_id"]): row for row in rows}:
                fd, temp = tempfile.mkstemp(prefix=".tag-assignments-", dir=path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as output:
                        json.dump({"version": 1, "assignments": rows}, output, sort_keys=True)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temp, path)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
    except (OSError, ValueError, TypeError, ImportError):
        pass  # observation must not block any session or consumer

