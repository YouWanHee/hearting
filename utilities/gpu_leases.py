"""Common, host-local GPU admission for compute-hosts and resource-runner.

The dependency-free implementation can also travel over compute-hosts' existing
SSH boundary. State paths are supplied by the canonical dispatch resolver, not
by an adapter. Only the host running the GPU reads its process identities.
"""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid


class GPUUnavailable(ValueError):
    pass


_TMUX_SESSION_ENV_KEYS = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
    "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID",
)
_TMUX_SERVER_PIN_KEYS = {
    "AGENT_HOME", "CLAUDE_HOME", "AGENT_DISPATCH_ATTEMPT_ID",
    "AGENT_DISPATCH_DEPTH", "AGENT_DISPATCH_JOBS",
    "AGENT_OWNER_ROUTE_FILE", "AGENT_ROUTE_FILE",
    "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
    "TMUX", "TMUX_PANE",
    "HEARTING_COMPUTE_RUN_ID", "HEARTING_COMPUTE_HOST",
    "HEARTING_RESOURCE_RUN_ID", "HEARTING_GPU_LEASE_RELEASE",
} | set(_TMUX_SESSION_ENV_KEYS)


def tmux_server_name(state_file):
    """Stable private tmux namespace for this host-local Hearting state root."""
    state = Path(state_file).expanduser()
    if not state.is_absolute():
        raise GPUUnavailable("GPU state path must be absolute for managed tmux")
    root = state.parent.resolve(strict=False)
    digest = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:20]
    return "hearting-" + digest


def state_path(environ=None):
    from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root
    env = os.environ if environ is None else environ
    jobs = env.get("AGENT_DISPATCH_JOBS")
    return (Path(jobs).expanduser().resolve().parent if jobs else
            resolve_dispatch_state_root(resolve_agent_home(), environ=env)) / "gpu-leases.json"


_OWNER_UNSET = object()


def _valid_session(harness, session_id):
    return (harness in {"claude", "codex", "opencode"}
            and isinstance(session_id, str) and bool(session_id)
            and len(session_id) <= 256 and session_id == session_id.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in session_id))


def _session_owner(harness, session_id):
    if not _valid_session(harness, session_id):
        return None
    owner = {"kind": "session", "harness": harness, "id": session_id}
    tag = None
    try:
        tools = str(Path(__file__).resolve().parents[1] / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.session_handle import resolve_tag
        tag = resolve_tag(harness, session_id)
    except Exception:  # noqa: BLE001 - labels are optional; exact identity is not
        pass
    owner["label"] = ("%s [%s]" % (harness, tag) if tag
                      else "%s:%s" % (harness, session_id[:8]))
    return owner


def _registered_parent(environ, jobs=None):
    """Resolve a registered launcher to its route parent's effective session.

    The boolean distinguishes an unregistered/interactive launcher, which may use its
    own canonical identity, from a registered attempt whose lineage could not be proved.
    """
    attempt = environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if not attempt:
        return False, None
    raw_depth = environ.get("AGENT_DISPATCH_DEPTH")
    if isinstance(raw_depth, int) and not isinstance(raw_depth, bool):
        depth = raw_depth
    elif isinstance(raw_depth, str) and raw_depth in {"0", "1", "2"}:
        depth = int(raw_depth)
    else:
        # An attempt id is evidence that this may be registered. Missing or
        # malformed depth cannot turn it into an interactive launcher.
        return True, None
    if depth == 0:
        return False, None
    if depth not in (1, 2):
        return True, None

    try:
        if jobs is None:
            jobs = environ.get("AGENT_DISPATCH_JOBS")
        if not jobs:
            from dispatch_contract import stable_state_root
            jobs = stable_state_root(environ) / "jobs.log"
        jobs = Path(jobs)

        utilities = str(Path(__file__).resolve().parent)
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import dispatch_seat_handover as handover

        rows = handover.latest_rows(jobs)
        current = rows.get(attempt)
        if not current:
            return True, None
        _status, metadata = current
        if (metadata.get("attempt_id") != attempt
                or str(metadata.get("dispatch_depth") or "") != str(depth)):
            return True, None

        owner_meta = metadata
        if depth == 2:
            parent_attempt = metadata.get("parent_attempt_id")
            parent = rows.get(parent_attempt) if parent_attempt else None
            if not parent:
                return True, None
            _parent_status, owner_meta = parent
            if (owner_meta.get("attempt_id") != parent_attempt
                    or str(owner_meta.get("dispatch_depth") or "") != "1"
                    or not handover.eligible_row(owner_meta)):
                return True, None
            child_route, child_hash, _child_node = handover.route_identity(metadata)
            parent_route, parent_hash, _parent_node = handover.route_identity(owner_meta)
            if (not child_route or not child_hash
                    or (child_route, child_hash) != (parent_route, parent_hash)):
                return True, None
        elif not handover.eligible_row(owner_meta):
            return True, None

        # _effective reads both the confirmed same-seat handover and its harness from
        # the same snapshot, preserving the registered parent when no handover exists.
        session_id, harness = handover._effective(owner_meta, jobs)
        if not _valid_session(harness, session_id):
            if not _valid_session_id(session_id):
                return True, None
            tools = str(Path(__file__).resolve().parents[1] / "tools")
            if tools not in sys.path:
                sys.path.insert(0, tools)
            from fleet import route_chain
            matches = []
            for candidate in route_chain.HARNESSES:
                try:
                    if Path(route_chain.ledger_path(candidate, session_id)).is_file():
                        matches.append(candidate)
                except (OSError, ValueError):
                    continue
            if len(matches) != 1:
                return True, None
            harness = matches[0]
        return True, _session_owner(harness, session_id)
    except Exception:  # noqa: BLE001 - attribution is advisory and must not block launch
        return True, None


def _valid_session_id(session_id):
    return (isinstance(session_id, str) and bool(session_id)
            and len(session_id) <= 256 and session_id == session_id.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in session_id))


def launcher_owner(environ=None, jobs=None):
    """Return one canonical launcher session, resolving registered attempts to their parent."""
    env = os.environ if environ is None else environ
    registered, owner = _registered_parent(env, jobs)
    if registered:
        return owner
    try:
        from session_identity import identity as session_identity
        found = session_identity(env)
    except Exception:  # noqa: BLE001 - unknown identity leaves the run unattributed
        return None
    if not found.known or not _valid_session(found.harness, found.session_id):
        return None
    return _session_owner(found.harness, found.session_id)


def launcher_session_environment(environ=None, *, owner=_OWNER_UNSET):
    """Copy an environment with every harness session alias replaced by one exact owner."""
    values = dict(os.environ if environ is None else environ)
    from session_identity import session_env
    declared = session_env()
    keys = tuple(key for names in declared.values() for key in names)
    for key in keys:
        values.pop(key, None)
    if owner is _OWNER_UNSET:
        owner = launcher_owner(environ)
    if (not isinstance(owner, dict) or owner.get("kind") != "session"
            or not _valid_session(owner.get("harness"), owner.get("id"))):
        return values
    names = declared.get(owner["harness"]) or ()
    if names:
        values[names[0]] = owner["id"]
    return values


def session_environment_keys():
    from session_identity import session_env
    return tuple(key for names in session_env().values() for key in names)


_REMOTE_LEASE_CORE = '''import contextlib, fcntl, json, os, socket, tempfile
from pathlib import Path

class GPUUnavailable(ValueError):
    pass

def identity(pid):
    try:
        fields = Path("/proc/%s/stat" % pid).read_text().rsplit(") ", 1)[1].split()
        return {"pid": int(pid), "starttime": fields[19],
                "pid_namespace": os.readlink("/proc/self/ns/pid"), "state": fields[0]}
    except FileNotFoundError:
        return None

def living(record):
    if record.get("host") != socket.gethostname().lower().split(".")[0]:
        return None
    if record.get("pid_namespace") != os.readlink("/proc/self/ns/pid"):
        return None
    try:
        current = identity(record["pid"])
    except (OSError, ValueError, IndexError, KeyError):
        return None
    return bool(current and current["state"] not in ("Z", "X")
                and current["starttime"] == str(record.get("starttime")))

def _read(path):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"schema_version": 1, "leases": {}}
    if data.get("schema_version") != 1 or not isinstance(data.get("leases"), dict):
        raise ValueError("invalid GPU reservation state")
    return data

def _write(path, data):
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)

@contextlib.contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(path)
        data["leases"] = {key: row for key, row in data["leases"].items()
                          if living(row) is not False}
        try:
            yield data
        finally:
            _write(path, data)

def release(path, lease):
    if not lease:
        return
    with locked(path) as data:
        data["leases"].pop(lease["token"], None)
'''


def serialized_snapshot_source():
    return _REMOTE_LEASE_CORE + '''
def snapshot(path):
    data = _read(path)
    host = socket.gethostname().lower().split(".")[0]
    return [row for row in data["leases"].values()
            if row.get("host") == host and living(row) is not False]
'''


def serialized_payload_source():
    return _REMOTE_LEASE_CORE + '''
import subprocess

def bind(path, lease, process):
    if not lease:
        return
    with locked(path) as data:
        row = data["leases"].get(lease["token"])
        if row is None:
            raise GPUUnavailable("GPU reservation ended before payload launch")
        row.update(process)

def payload(path, lease, inner):
    bind(path, lease, identity(os.getpid()))
    try:
        return subprocess.call(["bash", "-lc", inner])
    finally:
        release(path, lease)
'''


def identity(pid):
    try:
        text = Path("/proc/%s/stat" % pid).read_text()
        fields = text.rsplit(") ", 1)[1].split()
        return {"pid": int(pid), "starttime": fields[19],
                "pid_namespace": os.readlink("/proc/self/ns/pid"), "state": fields[0]}
    except FileNotFoundError:
        return None


def living(record):
    """False is proven death; unreadable/foreign namespace stays occupied."""
    if record.get("host") != socket.gethostname().lower().split(".")[0]:
        return None
    if record.get("pid_namespace") != os.readlink("/proc/self/ns/pid"):
        return None
    try:
        current = identity(record["pid"])
    except (OSError, ValueError, IndexError, KeyError):
        return None
    return bool(current and current["state"] not in ("Z", "X")
                and current["starttime"] == str(record.get("starttime")))


def _read(path):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {"schema_version": 1, "leases": {}}
    if data.get("schema_version") != 1 or not isinstance(data.get("leases"), dict):
        raise ValueError("invalid GPU reservation state")
    return data


def _write(path, data):
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read(path)
        data["leases"] = {key: row for key, row in data["leases"].items()
                          if living(row) is not False}
        try:
            yield data
        finally:
            _write(path, data)


def snapshot(path):
    # Probes are read-only, including on a remote host. Atomic writers already
    # publish complete JSON; prune the view here and persist cleanup at admission.
    data = _read(path)
    host = socket.gethostname().lower().split(".")[0]
    return [row for row in data["leases"].values()
            if row.get("host") == host and living(row) is not False]


def owner_label(owner):
    owner = owner or {}
    return owner.get("label") or ("%s:%s" % (owner.get("harness", "?"),
                                           str(owner.get("id", "unknown"))[:8]))


def description(row):
    stamp = row.get("started_at")
    when = datetime.datetime.fromtimestamp(stamp).isoformat(timespec="seconds") \
        if isinstance(stamp, (int, float)) else "unknown"
    return "%s · %s · %s" % (owner_label(row.get("owner")), row.get("task", "?"), when)


def select(observation, leases, requested=None, share=False):
    if requested in {"", "-1"}:
        return []  # explicit CPU-only
    if (not observation.get("reachable") or observation.get("detail")
            or observation.get("reservation_detail") or observation.get("gpu_status")
            or any(g.get("observation_source") for g in observation.get("gpus", []))):
        status = observation.get("gpu_status") or {}
        if status.get("summary"):
            occupied = sorted({str(g["index"]) for g in observation.get("gpus", [])
                               if g.get("processes")}, key=int)
            suffix = " · 장치 점유 " + ", ".join("GPU " + d for d in occupied) if occupied else ""
            raise GPUUnavailable(status["summary"] + suffix + " · 새 GPU 실행 예약 불가")
        raise GPUUnavailable("GPU availability unknown: %s" % (observation.get("detail") or "probe unavailable"))
    gpus = {str(g["index"]): g for g in observation.get("gpus", [])}
    if not gpus and requested is None:
        return []  # genuinely GPU-less host
    busy = {}
    for row in leases:
        for device in row["gpus"]:
            busy.setdefault(device, []).append(description(row))
    for device, gpu in gpus.items():
        for process in gpu.get("processes") or []:
            owner = process.get("session_owner") or process.get("owner")
            busy.setdefault(device, []).append("%s · %s · pid %s" % (
                owner_label(owner), process.get("command") or process.get("process_name") or "GPU process",
                process.get("pid", "?")))
        if gpu.get("utilization_gpu_pct") != 0 and device not in busy:
            busy[device] = ["GPU active or utilization unknown"]
        if observation.get("process_detail") and device not in busy:
            busy[device] = ["GPU processes unknown"]
    free = sorted((device for device in gpus if device not in busy),
                  key=lambda d: (-(gpus[d].get("free_mib") or 0), int(d)))
    if requested is None:
        devices = free[:1]
        if share and not devices:
            devices = sorted(gpus, key=lambda d: (-(gpus[d].get("free_mib") or 0), int(d)))[:1]
    else:
        devices = []
        for value in str(requested).split(","):
            value = value.strip()
            matches = [d for d, gpu in gpus.items() if value in (d, gpu.get("uuid"))]
            if len(matches) != 1:
                raise GPUUnavailable("unknown GPU %s; available indexes: %s" % (value, ",".join(gpus) or "none"))
            if matches[0] not in devices:
                devices.append(matches[0])
    conflicts = [d for d in devices if d in busy]
    if not devices or (conflicts and not share):
        details = "; ".join("gpu%s: %s" % (d, " / ".join(busy[d]))
                            for d in (conflicts or sorted(busy)))
        raise GPUUnavailable("GPU in use — %s; free GPUs: %s%s" % (
            details, ", ".join("gpu" + d for d in free) or "none",
            "; --share allows intentional sharing"))
    return devices


def acquire(path, observation, *, requested=None, share=False, owner=None, task="", run_id=""):
    with locked(path) as data:
        host = socket.gethostname().lower().split(".")[0]
        leases = [row for row in data["leases"].values() if row.get("host") == host]
        devices = select(observation, leases, requested, share)
        if not devices:
            return None
        token = uuid.uuid4().hex
        row = {"token": token, "host": socket.gethostname().lower().split(".")[0],
               "gpus": devices, "owner": owner, "task": task, "run_id": run_id,
               "started_at": time.time(), "share": bool(share), **identity(os.getpid())}
        data["leases"][token] = row
        return row


def bind(path, lease, process):
    if not lease:
        return
    with locked(path) as data:
        row = data["leases"].get(lease["token"])
        if row is None:
            raise GPUUnavailable("GPU reservation ended before payload launch")
        row.update(process)


def release(path, lease):
    if not lease:
        return
    with locked(path) as data:
        data["leases"].pop(lease["token"], None)


def payload(path, lease, inner):
    bind(path, lease, identity(os.getpid()))
    try:
        return subprocess.call(["bash", "-lc", inner])
    finally:
        release(path, lease)


def launch_compute(options, payload_source):
    """Runs on the target host; reserve before detached launch and bind before CUDA."""
    path = options["state_path"]
    lease = acquire(path, options["observation"], requested=options.get("requested"),
                    share=options.get("share", False), owner=options.get("owner"),
                    task=options["task"], run_id=options["run_id"])
    devices = ",".join(lease["gpus"]) if lease else options.get("requested")
    inner = options["inner"].replace("__HEARTING_GPU_SETUP__",
        "export CUDA_VISIBLE_DEVICES=%s" % shlex.quote(devices) if devices is not None else ":")
    script = payload_source + "\nraise SystemExit(payload(%r, %r, %r))\n" % (path, lease, inner)
    command = "python3 -c " + shlex.quote(script)
    run_dir = Path(options["run_dir"])
    launch_cwd = options.get("cwd") or os.getcwd()
    if not isinstance(launch_cwd, str) or not os.path.isabs(launch_cwd):
        release(path, lease)
        raise GPUUnavailable("compute launch directory must be absolute")
    tmux_environment = options.get("tmux_environment") or {}
    if not isinstance(tmux_environment, dict):
        release(path, lease)
        raise GPUUnavailable("invalid tmux launch environment")
    tmux_environment = dict(tmux_environment)
    for key in (
        *_TMUX_SESSION_ENV_KEYS,
        "AGENT_HOME", "AGENT_DISPATCH_ATTEMPT_ID",
        "HEARTING_COMPUTE_RUN_ID", "HEARTING_COMPUTE_HOST",
        "HEARTING_RESOURCE_RUN_ID", "HEARTING_GPU_LEASE_RELEASE",
    ):
        tmux_environment.setdefault(key, "")
    for key, value in tmux_environment.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(value, str) or "\0" in value):
            release(path, lease)
            raise GPUUnavailable("invalid tmux launch environment")
    server_env = dict(os.environ)
    for key in _TMUX_SERVER_PIN_KEYS | set(tmux_environment):
        server_env.pop(key, None)
    server_name = tmux_server_name(path)
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        if subprocess.call(
            ["sh", "-c", "command -v tmux >/dev/null 2>&1"],
            env=server_env,
            cwd="/",
        ) == 0:
            # The first client owns server creation. Keep that long-lived
            # process outside the invoking release/session, then provide only
            # this launch's validated identity and cwd to its new session.
            subprocess.check_call(
                ["tmux", "-L", server_name, "start-server"],
                env=server_env,
                cwd="/",
            )
            tmux_command = ["tmux", "-L", server_name, "new-session", "-d",
                            "-s", options["run_id"]]
            for key, value in sorted(tmux_environment.items()):
                tmux_command.extend(["-e", f"{key}={value}"])
            tmux_command.extend(["-c", launch_cwd, command])
            subprocess.check_call(tmux_command, env=server_env, cwd="/")
            tmux_transport = {"kind": "hearting-tmux-v1", "server": server_name}
        else:
            with open(os.devnull, "rb") as stdin, open(os.devnull, "ab") as output:
                subprocess.Popen(["python3", "-c", script], stdin=stdin, stdout=output,
                                 stderr=output, start_new_session=True, cwd=launch_cwd)
            tmux_transport = {"kind": "nohup-v1"}
        # The launcher remains the pending holder until its wrapper is bound.
        # A dead launcher can never let an unbound wrapper start a payload.
        deadline = time.monotonic() + 5
        while lease and time.monotonic() < deadline:
            with locked(path) as data:
                current = data["leases"].get(lease["token"])
                if current is None or current["pid"] != lease["pid"]:
                    break
            time.sleep(.02)
        else:
            if lease:
                raise GPUUnavailable("GPU launch did not establish its wrapper")
    except Exception:
        release(path, lease)
        raise
    return {"gpus": devices, "gpu_lease": lease,
            "tmux_transport": tmux_transport}


def requested_devices(command, default=None):
    requested = default
    for word in command:
        if word.startswith("CUDA_VISIBLE_DEVICES="):
            requested = word.split("=", 1)[1]
    return requested


def local_observation():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_lease_compute_hosts", Path(__file__).with_name("compute-hosts.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool._run_gpu_observation(socket.gethostname(), {"ssh_host": "local"})


def resource_admission(node, command, *, gpu_scoped=False, share=False, jobs=None, run_id=""):
    """Use the route's GPU declaration and the existing CUDA environment choice."""
    requested = requested_devices(command, os.environ.get("CUDA_VISIBLE_DEVICES"))
    if requested in {"", "-1"} or (requested is None and node.get("resource_class") != "gpu" and not gpu_scoped):
        return None, None, {}
    observation = local_observation()
    path = state_path({**os.environ, **({"AGENT_DISPATCH_JOBS": str(jobs)} if jobs else {})})
    try:
        lease = acquire(path, observation, requested=requested, share=share,
                        owner=launcher_owner(jobs=jobs),
                        task=run_id or shlex.join(command), run_id=run_id)
    except GPUUnavailable as error:
        raise GPUUnavailable("Local GPU admission on %s: %s" % (socket.gethostname(), error)) from error
    if not lease:
        return path, None, {}
    cleanup = _REMOTE_LEASE_CORE + "\nrelease(%r, %r)\n" % (str(path), lease)
    return path, lease, {"CUDA_VISIBLE_DEVICES": ",".join(lease["gpus"]),
                         "HEARTING_GPU_LEASE_RELEASE": "python3 -c " + shlex.quote(cleanup)}
