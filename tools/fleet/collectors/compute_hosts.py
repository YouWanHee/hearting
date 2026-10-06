"""Fail-soft bridge from Fleet to the user-owned compute-host inventory.

The inventory/probe utility remains the single source of SSH and hostname
semantics. Fleet invokes its JSON surface with a bounded timeout and exposes the
result unchanged enough for diagnostics; it never edits config or chooses a host.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from ..model import project_of


COLLECT_TIMEOUT = 5.0


def _tool_argv():
    override = os.environ.get("FLEET_COMPUTE_HOSTS_TOOL")
    if override:
        path = Path(override).expanduser()
        return [sys.executable, str(path)] if path.suffix == ".py" else [str(path)]

    here = Path(__file__).resolve()
    for parent in here.parents:
        path = parent / "utilities" / "compute-hosts.py"
        if path.is_file():
            return [sys.executable, str(path)]

    agent_home = os.environ.get("AGENT_HOME")
    if agent_home:
        path = Path(agent_home).expanduser() / "utilities" / "compute-hosts.py"
        if path.is_file():
            return [sys.executable, str(path)]
    return None


def _config_path():
    override = os.environ.get("COMPUTE_HOSTS_CONFIG")
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "hearting" / "compute-hosts.yaml"


def _unconfigured(status, path):
    """Guidance-only snapshot: nothing to probe, but the panel says what to do."""
    hint = ("edit the seeded template" if status == "template"
            else "run `harness install` to seed a template")
    return {"configured": False, "status": status, "hosts": [],
            "path": str(path), "hint": hint, "observed_at": time.time()}


def _diagnostic(message, observed_at=None):
    return {
        "configured": True,
        "hosts": [],
        "error": str(message or "compute-host probe failed")[:300],
        "observed_at": observed_at if observed_at is not None else time.time(),
    }


def collect(timeout=COLLECT_TIMEOUT):
    """Return a host snapshot, a probe diagnostic, or an unconfigured guidance block."""
    path = _config_path()
    if not path.is_file():
        return _unconfigured("missing", path)
    argv = _tool_argv()
    if not argv:
        return _diagnostic("compute-hosts utility unavailable")
    observed_at = time.time()
    try:
        result = subprocess.run(
            argv + ["list", "--json"], text=True, capture_output=True,
            timeout=max(0.1, float(timeout)),
        )
    except subprocess.TimeoutExpired:
        return _diagnostic("compute-host probe timed out", observed_at)
    except OSError as exc:
        return _diagnostic(exc, observed_at)
    if result.returncode:
        detail = (result.stderr or result.stdout or "compute-host probe failed").strip()
        # A config can disappear between the pre-check and subprocess startup;
        # treat that race exactly like an initially absent config. A seeded but
        # still-commented template is guidance, not a probe failure.
        if "not initialized" in detail:
            return _unconfigured("missing", path)
        if "has no hosts yet" in detail:
            return _unconfigured("template", path)
        return _diagnostic(detail, observed_at)
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        return _diagnostic("invalid compute-host JSON", observed_at)
    if not isinstance(payload, dict) or not isinstance(payload.get("hosts"), list):
        return _diagnostic("invalid compute-host payload", observed_at)
    snapshot = dict(payload)
    snapshot["configured"] = True
    snapshot["observed_at"] = max(
        [row.get("observed_at") for row in snapshot["hosts"]
         if isinstance(row, dict) and isinstance(row.get("observed_at"), (int, float))]
        or [observed_at]
    )
    return snapshot


def _pos_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonneg_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _session_key(owner):
    """`(harness, id)` of a valid session owner, else None (same rule as F-88)."""
    if (isinstance(owner, dict) and owner.get("kind") == "session"
            and owner.get("harness") in {"claude", "codex", "opencode"}
            and isinstance(owner.get("id"), str) and owner.get("id")):
        return (owner["harness"], owner["id"])
    return None


def _registered_run_marks(resource_jobs):
    """Identity of every working registered run: (pid, starttime) and process groups."""
    exact, groups = set(), set()
    for job in resource_jobs or ():
        if getattr(job, "liveness", None) != "working":
            continue
        pid = getattr(job, "pid", None)
        if _pos_int(pid):
            exact.add((pid, str(getattr(job, "starttime", None))))
        group = getattr(job, "process_group", None)
        if _pos_int(group):
            groups.add(group)
    return exact, groups


def with_training_progress(snapshot, resource_jobs=(), now=None):
    """Pure exact-process join shared by JSON and both Fleet views.

    The resource collector has already bound config/progress bytes to a stable
    wrapper and child. A fresh self-host GPU sample must still name that exact
    child. Heartbeat freshness never resets the progress file's age.

    Remote production arms ride along as display-only candidates (no
    live/done claim) and join only their own non-self host, keyed by exact
    host-qualified pid/start plus command hash when both sides carry it. Same
    pid on another host, euid-gated absence, or any mismatch fails soft back
    to the raw probe line. Verified local training always wins its own host.
    """
    if not isinstance(snapshot, dict) or snapshot.get("error"):
        return snapshot
    now = time.time() if now is None else now
    matches = {}
    remote_matches = {}
    for job in resource_jobs or ():
        training = getattr(job, "training_progress", None)
        if getattr(job, "liveness", None) != "working" or not isinstance(training, dict):
            continue
        observed = training.get("observed_at")
        updated = training.get("progress_updated_at")
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) for value in (observed, updated)) \
                or not 0 <= now - observed <= 30:
            continue
        key = (training.get("pid"), str(training.get("starttime")), training.get("process_group"))
        if not _pos_int(key[0]) or not _pos_int(key[2]) \
                or key[2] != getattr(job, "process_group", None):
            continue
        # Multiple registrations with different observations are ambiguous.
        if key in matches and matches[key] != training:
            matches[key] = None
        else:
            matches.setdefault(key, training)
        for candidate in getattr(job, "remote_training", None) or ():
            if not isinstance(candidate, dict) or candidate.get("remote") is not True:
                continue
            host = candidate.get("host")
            if not isinstance(host, str) or not host:
                continue
            seen = candidate.get("observed_at")
            changed = candidate.get("progress_updated_at")
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                       and math.isfinite(value) for value in (seen, changed)) \
                    or not 0 <= now - seen <= 30:
                continue
            remote_key = (host, candidate.get("pid"), str(candidate.get("starttime")))
            if not _pos_int(remote_key[1]):
                continue
            if remote_key in remote_matches and remote_matches[remote_key] != candidate:
                remote_matches[remote_key] = None
            else:
                remote_matches.setdefault(remote_key, candidate)
    hosts = []
    for host in snapshot.get("hosts") or ():
        if not isinstance(host, dict):
            hosts.append(host)
            continue
        observed = host.get("observed_at", snapshot.get("observed_at"))
        if host.get("reachable") is not True \
                or not isinstance(observed, (int, float)) or isinstance(observed, bool) \
                or not math.isfinite(observed) or not 0 <= now - observed <= 30:
            hosts.append(host)
            continue
        is_self = host.get("self") is True
        names = {value for value in (host.get("host"), host.get("hostname"))
                 if isinstance(value, str) and value}
        gpus = []
        for gpu in host.get("gpus") or ():
            if not isinstance(gpu, dict):
                gpus.append(gpu)
                continue
            processes = []
            for process in gpu.get("processes") or ():
                training = None
                if isinstance(process, dict) and _pos_int(process.get("pid")) \
                        and _pos_int(process.get("pgid")):
                    if is_self:
                        training = matches.get((process["pid"], str(process.get("proc_start")), process["pgid"]))
                    else:
                        training = _remote_hit(remote_matches, names, process)
                if training is not None:
                    training = dict(training)
                    training["progress_age_s"] = max(0.0, now - training["progress_updated_at"])
                    raw_progress = process.get("progress")
                    progress = dict(raw_progress) if isinstance(raw_progress, dict) else {}
                    progress["training"] = training
                    process = {**process, "progress": progress}
                processes.append(process)
            gpus.append({**gpu, "processes": processes})
        hosts.append({**host, "gpus": gpus})
    return {**snapshot, "hosts": hosts}


def _remote_hit(remote_matches, names, process):
    """One display candidate for this exact remote process, else None.

    Host-qualified pid/start, plus command-hash equality when both sides
    carry one. Distinct observations for one process stay ambiguous (None);
    a process the probe could not read as its own user (`command` absent
    under the host-local euid gate) never joins.
    """
    if process.get("command") is None:
        return None
    found = []
    for name in names:
        hit = remote_matches.get((name, process["pid"], str(process.get("proc_start"))))
        if hit is not None and all(hit != prior for prior in found):
            found.append(hit)
    if len(found) != 1:
        return None
    candidate = found[0]
    probe_hash = process.get("command_hash")
    candidate_hash = candidate.get("command_hash")
    if isinstance(probe_hash, str) and probe_hash \
            and isinstance(candidate_hash, str) and candidate_hash \
            and probe_hash != candidate_hash:
        return None
    return candidate


def unregistered_gpu(snapshot, resource_jobs=(), shown_sessions=frozenset(), age_s=0.0):
    """One entry per live GPU process not already shown elsewhere (F-104). No I/O.

    A process is skipped when a working registered resource run owns it (same
    pid+start, or same process group; only on the host Fleet runs on) or when
    the F-88 session GPU line for its `session_owner` is on screen. `cwd` picks
    the project card only; it is never ownership evidence.
    """
    if (not isinstance(snapshot, dict) or not snapshot.get("configured")
            or snapshot.get("error")):
        return []
    hosts = snapshot.get("hosts")
    if not isinstance(hosts, (list, tuple)):
        return []
    run_exact, run_groups = _registered_run_marks(resource_jobs)
    entries = {}
    for host in hosts:
        if not isinstance(host, dict) or host.get("reachable") is not True:
            continue
        host_name = host.get("host") if isinstance(host.get("host"), str) else "?"
        is_self = host.get("self") is True
        gpus = host.get("gpus")
        if not isinstance(gpus, (list, tuple)):
            continue
        for gpu in gpus:
            if not isinstance(gpu, dict):
                continue
            gpu_index = gpu.get("index")
            processes = gpu.get("processes")
            if not _nonneg_int(gpu_index) or not isinstance(processes, (list, tuple)):
                continue
            for process in processes:
                if not isinstance(process, dict) or not _pos_int(process.get("pid")):
                    continue
                pid, proc_start = process["pid"], process.get("proc_start")
                pgid = process.get("pgid")
                if is_self and ((pid, str(proc_start)) in run_exact
                                or (_pos_int(pgid) and pgid in run_groups)):
                    continue
                session_owner = process.get("session_owner")
                if _session_key(session_owner) in shown_sessions:
                    continue
                key = (host_name, pid, proc_start)
                entry = entries.get(key)
                used = process.get("used_memory_mib")
                used = used if _nonneg_int(used) else None
                if entry is None:
                    cwd = process.get("cwd")
                    cwd = cwd if isinstance(cwd, str) and cwd.startswith("/") else None
                    elapsed = process.get("elapsed_s")
                    owner = process.get("owner")
                    entry = {
                        "host": host_name, "self": is_self, "gpu_indexes": [],
                        "gpu_name": gpu.get("name") if isinstance(gpu.get("name"), str) else None,
                        "pid": pid, "proc_start": proc_start,
                        "pgid": pgid if _pos_int(pgid) else None,
                        "used_memory_mib": None,
                        "elapsed_s": (elapsed + max(0, int(age_s or 0)))
                        if _nonneg_int(elapsed) else None,
                        "command": process.get("command")
                        if isinstance(process.get("command"), str) else None,
                        "process_name": process.get("process_name")
                        if isinstance(process.get("process_name"), str) else None,
                        "cwd": cwd,
                        "project": project_of(cwd) if cwd else "(unknown)",
                    }
                    if (isinstance(owner, dict) and owner.get("kind") in {"job", "run"}
                            and isinstance(owner.get("label"), str) and owner.get("label")):
                        entry["owner_kind"] = owner["kind"]
                        entry["owner_label"] = owner["label"]
                    if isinstance(session_owner, dict):
                        entry["session_owner"] = session_owner
                    entries[key] = entry
                if gpu_index not in entry["gpu_indexes"]:
                    entry["gpu_indexes"].append(gpu_index)
                if used is not None:
                    entry["used_memory_mib"] = (entry["used_memory_mib"] or 0) + used
    out = list(entries.values())
    for entry in out:
        entry["gpu_indexes"].sort()
    out.sort(key=lambda e: (e["project"], e["host"], e["gpu_indexes"][0],
                            -(e["used_memory_mib"] or 0), e["pid"]))
    return out
