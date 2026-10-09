"""Cheap, read-only placement of one exact local resource and its descendants."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import socket
import time

MAX_PROCESSES = 32
MAX_BYTES = 65536
MAX_SECONDS = 0.01
_DEVICE = re.compile(r"(?:[0-9]{1,4}|GPU-[a-fA-F0-9-]{8,48}|MIG-[a-fA-F0-9/-]{8,64})\Z")


def _read(path, limit=MAX_BYTES):
    with path.open("rb") as handle:
        return handle.read(limit)


def _stat(root, pid):
    path = root / str(pid)
    if path.stat().st_uid != os.geteuid():
        raise ValueError("foreign process")
    raw = _read(path / "stat", 4096)
    fields = raw.rsplit(b") ", 1)[1].split()
    if int(raw.split(b" ", 1)[0]) != pid or fields[0] in (b"Z", b"X"):
        raise ValueError("terminal or changed process")
    return {"pid": pid, "starttime": fields[19].decode("ascii"),
            "ppid": int(fields[1]), "state": fields[0].decode("ascii")}


def _environment(root, pid):
    raw = _read(root / str(pid) / "environ")
    values = {}
    for part in raw.split(b"\0")[:-1]:
        key, sep, value = part.partition(b"=")
        if sep and key in (b"CUDA_VISIBLE_DEVICES", b"HEARTING_RESOURCE_RUN_ID"):
            values[key.decode("ascii")] = value.decode("ascii", errors="replace")[:256]
    return values


def observe(run, proc_root=Path("/proc")):
    """Display evidence only; at most 32 targeted processes and 10ms per run.

    Every edge and start time is rechecked. No directory walk, subprocess,
    workload file, GPU query or runtime-state mutation occurs here.
    """
    try:
        pid = run.get("pid")
        if type(pid) is not int or pid <= 0 or not str(run.get("starttime", "")).isdigit():
            return None
        root = Path(proc_root)
        deadline = time.monotonic() + MAX_SECONDS
        before = _stat(root, pid)
        command = _read(root / str(pid) / "cmdline")
        if (before["starttime"] != str(run["starttime"]) or not command
                or hashlib.sha256(command).hexdigest() != run.get("command_hash")):
            return None
        pending = [(pid, None)]
        seen = {}
        devices = set()
        selected_states = []
        complete = True
        for current, parent in pending:
            if len(seen) >= MAX_PROCESSES or time.monotonic() >= deadline:
                break
            if current in seen:
                continue
            try:
                process = _stat(root, current)
                if parent is not None and process["ppid"] != parent:
                    continue
                env = _environment(root, current)
                if env.get("HEARTING_RESOURCE_RUN_ID") not in (None, run.get("run_id")):
                    continue  # a nested resource owns its own descendants
                selected = env.get("CUDA_VISIBLE_DEVICES", "").split(",")
                selected = [value.strip() for value in selected]
                if selected and all(_DEVICE.fullmatch(value) for value in selected):
                    devices.update(selected)
                    selected_states.append(process["state"])
                seen[current] = process
                children = _read(root / str(current) / "task" / str(current) / "children", 4096)
                if len(children) == 4096:
                    complete = False
                for child in children.split():
                    if not child.isdigit():
                        complete = False
                    elif len(pending) < MAX_PROCESSES:
                        pending.append((int(child), current))
                    else:
                        complete = False
            except (OSError, ValueError, IndexError, UnicodeError):
                complete = False
                continue
        # Reuse, reparenting or root exec discards the batch.
        if pid not in seen:
            return None
        for key, row in seen.items():
            if time.monotonic() >= deadline:
                return None
            after = _stat(root, key)
            if after["starttime"] != row["starttime"] or after["ppid"] != row["ppid"]:
                return None
        if (_read(root / str(pid) / "cmdline") != command
                or time.monotonic() >= deadline):
            return None
        return {"hostname": socket.gethostname(),
                "processes": [{"pid": row["pid"], "starttime": row["starttime"]}
                              for row in seen.values()],
                "requested_devices": sorted(devices),
                "complete": complete and len(seen) == len(pending),
                "io_wait": bool(selected_states) and all(state == "D" for state in selected_states)}
    except (OSError, ValueError, IndexError, UnicodeError, TypeError):
        return None
