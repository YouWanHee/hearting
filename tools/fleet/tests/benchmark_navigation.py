#!/usr/bin/env python3
"""Fresh-process Fleet peer preview, PTY latency, scale and retention probe.

Synthetic modes isolate renderer and navigation cost. The
``explorer-collecting`` mode opts into real read-only Fleet collection and records
the first complete Explorer frame plus key-to-paint latency while collection
runs; its private terminal capture belongs only in the supplied evidence root.
"""
from __future__ import annotations

import argparse
import curses
import datetime
import fcntl
import hashlib
import json
import os
import platform
import pty
import resource
import selectors
import signal
import shutil
import socket
import stat
import statistics
import struct
import subprocess
import sys
import termios
import threading
import time
import tracemalloc
from pathlib import Path


SCRIPT = Path(__file__).resolve()


def _frame_metrics(readiness, generation, replay_active, frame_mode,
                   frame_present=True):
    readiness = readiness if isinstance(readiness, dict) else {}
    sources = readiness.get("sources") if isinstance(readiness.get("sources"), dict) else {}
    basic = sources.get("basic") if isinstance(sources.get("basic"), dict) else {}
    required = (sources.get("required-details")
                if isinstance(sources.get("required-details"), dict) else {})
    complete_states = {"complete", "completed", "success", "ok", "observed"}
    basic_complete = str(basic.get("state") or "").lower() in complete_states
    detail_state = str(required.get("state") or "").lower()
    details_complete = (detail_state in complete_states or
                        (detail_state == "continued" and
                         required.get("continuation_validated") is True))
    loading = str(frame_mode or "").lower() == "loading"
    ready = (readiness.get("ready") is True and readiness.get("fully_fresh") is True
             and basic_complete and details_complete)
    fresh = bool(frame_present and not replay_active and not loading
                 and generation > 0 and ready)
    all_new = fresh and readiness.get("all_new") is True
    return {"frame_present": bool(frame_present), "frame_mode": frame_mode,
            "loading": loading, "replay": bool(replay_active),
            "basic_complete": basic_complete,
            "required_details_complete": details_complete,
            "ready": ready, "fresh_complete": fresh,
            "fresh_all_new": all_new,
            "generation": generation,
            "pending": list(readiness.get("pending") or ())}


def _production_schedule(keys, duration):
    if keys < 0 or duration < 0:
        raise ValueError("keys and duration must be non-negative")
    if keys == 0:
        return []
    return [duration * index / keys for index in range(keys)]


def _production_order(replicates=3):
    arms = ("base", "candidate", "legacy")
    orders = (arms, ("candidate", "base", "legacy"), arms)
    return [list(orders[index % len(orders)]) for index in range(replicates)]


def _production_argv(role, trees):
    tree = Path(trees[role]).resolve()
    launcher = tree / "tools" / "fleet" / (
        "fleet.sh" if role == "legacy" else "fleet2.sh")
    return [str(launcher)]


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * percentile))]


def _match_production_keys(sent, events):
    decoded = {item.get("input_id"): item for item in events
               if item.get("kind") == "input-decoded"}
    painted = {}
    for item in events:
        if item.get("kind") == "flush" and item.get("input_id") is not None:
            painted.setdefault(item["input_id"], item)
    matched = []
    for row in sent:
        input_id = row["sample"] + 1
        decode, paint = decoded.get(input_id), painted.get(input_id)
        if row.get("write_error"):
            status = "write-error"
        elif decode is None:
            status = "missing-decode"
        elif paint is None:
            status = "decoded-no-flush"
        else:
            status = "painted"
        latency = (max(0, paint["time_ns"] - row["parent_write_ns"]) / 1e6
                   if paint else None)
        matched.append({**row, "input_id": input_id,
                        "decoded_ns": decode.get("time_ns") if decode else None,
                        "painted_ns": paint.get("time_ns") if paint else None,
                        "end_to_flush_ms": latency, "status": status})
    return matched


def _pump_phase_samples(events):
    previous, completed, samples = {}, {}, []
    for item in events:
        name = item.get("name")
        if item.get("kind") == "pump-start":
            prior = previous.get(name)
            samples.append({"name": name, "phase": "start",
                            "time_ns": item.get("time_ns"),
                            "interval_seconds": item.get("interval_seconds"),
                            "observed_cadence_seconds": (
                                (item["time_ns"] - prior) / 1e9
                                if prior is not None and item.get("time_ns") is not None else None)})
            previous[name] = item.get("time_ns")
        elif item.get("kind") in ("pump-done", "pump-error"):
            index = completed.get(name, 0)
            completed[name] = index + 1
            if "install" in str(name):
                phase = "install"
            elif "selection" in str(name):
                phase = "selection"
            elif "gpu" in str(name):
                phase = "compute-host"
            elif "detail" in str(name):
                phase = "required-detail" if index == 0 else "optional-detail"
            else:
                phase = "first-basic" if index == 0 else "steady-basic"
            samples.append({"name": name, "phase": phase,
                            "duration_ns": item.get("duration_ns"),
                            "cpu_ns": item.get("cpu_ns"),
                            "status": "error" if item.get("kind") == "pump-error" else "complete",
                            "sessions": item.get("sessions"), "jobs": item.get("jobs"),
                            "resources": item.get("resources")})
    return samples


def _observer_source():
    return r'''import atexit
import curses
import hashlib
import json
import os
from importlib.machinery import PathFinder
from pathlib import Path
import sys
import time

FD = int(os.environ.get("FLEET_BENCHMARK_TELEMETRY_FD", "-1"))
_emit_ns = 0
_input_id = 0
_pending_input = None
_flush_id = 0

def emit(record):
    global _emit_ns
    if FD < 0:
        return
    started = time.monotonic_ns()
    try:
        data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        view = memoryview(data)
        while view:
            written = os.write(FD, view)
            view = view[written:]
    except (OSError, TypeError, ValueError):
        return
    finally:
        _emit_ns += time.monotonic_ns() - started

def chain_sitecustomize():
    observer_file = Path(__file__).resolve()
    observer_dir = Path(os.environ.get("FLEET_BENCHMARK_OBSERVER_DIR", "")).resolve()
    for entry in sys.path:
        try:
            if Path(entry or os.getcwd()).resolve() == observer_dir:
                continue
        except OSError:
            continue
        spec = PathFinder.find_spec("sitecustomize", [entry])
        if spec is None or not spec.origin or Path(spec.origin).resolve() == observer_file:
            continue
        try:
            source = Path(spec.origin).read_bytes()
            globals()["__file__"] = spec.origin
            exec(compile(source, spec.origin, "exec"), globals(), globals())
        except Exception as error:
            emit({"kind": "observer-unsupported", "component": "sitecustomize-chain",
                  "error_type": type(error).__name__})
        finally:
            globals()["__file__"] = str(observer_file)
        break

chain_sitecustomize()

def _times():
    value = os.times()
    return {"user_seconds": value.user, "system_seconds": value.system,
            "children_user_seconds": value.children_user,
            "children_system_seconds": value.children_system}

def _states(readiness):
    sources = readiness.get("sources", readiness) if isinstance(readiness, dict) else {}
    if not isinstance(sources, dict):
        return {}
    return {str(name): {key: item[key] for key in ("state", "all_new", "attempted",
                                                    "continuation_validated")
                        if isinstance(item, dict) and key in item}
            for name, item in sources.items()}

def _frame_event(render):
    frame = getattr(render, "_EXPLORER_FRAME", None)
    readiness = getattr(render, "_EXPLORER_READINESS", None)
    readiness = readiness if isinstance(readiness, dict) else {}
    sources = _states(readiness)
    basic_state = str((sources.get("basic") or {}).get("state") or "").lower()
    detail_state = str((sources.get("required-details") or {}).get("state") or "").lower()
    complete = {"complete", "completed", "success", "ok", "observed"}
    detail_complete = (detail_state in complete or
                       (detail_state == "continued" and
                        (sources.get("required-details") or {}).get(
                            "continuation_validated") is True))
    frame_mode = (getattr(frame, "mode", None) or getattr(frame, "kind", None)
                  or getattr(frame, "view", None) or getattr(frame, "status", None)
                  or getattr(frame, "title", None)) if frame is not None else None
    loading = str(frame_mode or "").lower() == "loading"
    replay = bool(getattr(render, "_EXPLORER_REPLAY_ACTIVE", False))
    generation = getattr(render, "_EXPLORER_GENERATION", 0)
    ready = (readiness.get("ready") is True and readiness.get("fully_fresh") is True
             and basic_state in complete and detail_complete)
    fresh = bool(frame is not None and not loading and not replay
                 and isinstance(generation, int) and generation > 0 and ready)
    return {"screen_flush": True, "frame_present": frame is not None,
            "frame_mode": frame_mode, "loading": loading, "replay": replay,
            "generation": generation, "ready": ready,
            "basic_complete": basic_state in complete,
            "required_details_complete": detail_complete,
            "fresh_complete": fresh,
            "fresh_all_new": fresh and readiness.get("all_new") is True,
            "pending": list(readiness.get("pending") or ()),
            "source_states": sources}

def _required_detail_diagnostics(snapshot):
    groups = (("session", list(getattr(snapshot, "sessions", ()) or ())),
              ("job", list(getattr(snapshot, "jobs", ()) or ())),
              ("route-entity", list(getattr(snapshot, "route_entities", ()) or ())))
    entities = [entity for _kind, rows in groups for entity in rows]
    result = {"entity_count": len(entities), "route_bound_count": 0,
              "details_pending": 0, "required_detail_error": 0,
              "projection_missing": 0, "projection_ambiguous": 0,
              "projection_route_missing": 0, "route_record_missing": 0,
              "route_record_identity_mismatch": 0, "route_record_hash_mismatch": 0,
              "route_bound_by_kind": {}, "route_bound_issues": []}
    for entity in entities:
        result["details_pending"] += bool(getattr(entity, "_details_pending", False))
        result["required_detail_error"] += bool(getattr(entity, "_required_detail_error", False))
    for kind, rows in groups:
        bound = [entity for entity in rows
                 if getattr(entity, "route_id", None) or getattr(entity, "owner_route_id", None)]
        result["route_bound_count"] += len(bound)
        result["route_bound_by_kind"][kind] = len(bound)
        for entity in bound:
            work = getattr(entity, "work_projection", None)
            problems = {}
            if work is None:
                result["projection_missing"] += 1
                problems["projection_missing"] = True
            else:
                ambiguous = bool(getattr(work, "ambiguity", None))
                result["projection_ambiguous"] += ambiguous
                if ambiguous:
                    problems["projection_ambiguous"] = True
                    code = str(getattr(work, "ambiguity", ""))[:80].lower()
                    problems["ambiguity_code"] = (code if code and all(
                        char.isascii() and (char.isalnum() or char in "-_")
                        for char in code) else "unclassified")
                route_id = getattr(work, "route_id", None)
                if not route_id:
                    result["projection_route_missing"] += 1
                    problems["projection_route_missing"] = True
                route_view = getattr(work, "_route_view", None)
                record = route_view.get("record") if isinstance(route_view, dict) else None
                if not isinstance(record, dict):
                    result["route_record_missing"] += 1
                    problems["route_record_missing"] = True
                else:
                    identity_mismatch = record.get("route_id") != route_id
                    hash_mismatch = record.get("route_hash") != getattr(work, "route_hash", None)
                    result["route_record_identity_mismatch"] += identity_mismatch
                    result["route_record_hash_mismatch"] += hash_mismatch
                    if identity_mismatch:
                        problems["route_record_identity_mismatch"] = True
                    if hash_mismatch:
                        problems["route_record_hash_mismatch"] = True
            if problems and len(result["route_bound_issues"]) < 32:
                exact = (kind, getattr(entity, "harness", None), getattr(entity, "session_id", None),
                         getattr(entity, "attempt_id", None), getattr(entity, "pid", None),
                         getattr(entity, "proc_start", None), getattr(entity, "cwd", None),
                         getattr(entity, "source", None))
                fingerprint = hashlib.sha256(repr(exact).encode("utf-8", "replace")).hexdigest()
                result["route_bound_issues"].append({"kind": kind,
                                                     "identity_sha256": fingerprint,
                                                     **problems})
    return result

class WindowRefreshObserver:
    """Observe the completed refresh used by production renderers."""
    def __init__(self, window, before_refresh, after_refresh):
        self._window = window
        self._before_refresh = before_refresh
        self._after_refresh = after_refresh

    def refresh(self, *args, **kwargs):
        self._before_refresh()
        try:
            result = self._window.refresh(*args, **kwargs)
        except BaseException:
            self._after_refresh(False)
            raise
        self._after_refresh(True)
        return result

    def __getattr__(self, name):
        return getattr(self._window, name)

def install():
    tools = os.environ.get("FLEET_BENCHMARK_SOURCE_TOOLS")
    if tools and tools not in sys.path:
        sys.path.insert(0, tools)
    cache_home = os.environ.get("FLEET_BENCHMARK_CACHE_HOME")
    try:
        from fleet.explorer.runtime import ExplorerRuntime
        original_configure = ExplorerRuntime.configure_persistence
        def configure(runtime, *args, **kwargs):
            if cache_home:
                runtime._state_home = Path(cache_home)
            result = original_configure(runtime, *args, **kwargs)
            emit({"kind": "cache-configured", "cache_path": str(result)})
            return result
        ExplorerRuntime.configure_persistence = configure
    except Exception as error:
        emit({"kind": "observer-unsupported", "component": "explorer-runtime",
              "error_type": type(error).__name__})
    try:
        from fleet import render
        from fleet.refresh import RefreshPump
        from fleet.collectors import dispatch
        original_init = RefreshPump.__init__
        def init(pump, producer, interval, *args, **kwargs):
            name = kwargs.get("name", args[2] if len(args) > 2 else "fleet-refresh")
            adaptive = kwargs.get("adaptive_cadence", False)
            def observed(*call_args, **call_kwargs):
                started = time.monotonic_ns()
                cpu_started = time.process_time_ns()
                emit({"kind": "pump-start", "name": name, "interval_seconds": interval,
                      "adaptive_cadence": bool(adaptive), "time_ns": started})
                try:
                    result = producer(*call_args, **call_kwargs)
                except BaseException as error:
                    emit({"kind": "pump-error", "name": name,
                          "duration_ns": time.monotonic_ns() - started,
                          "cpu_ns": time.process_time_ns() - cpu_started,
                          "error_type": type(error).__name__})
                    raise
                value = result[0] if isinstance(result, tuple) and result else result
                snapshot = getattr(value, "snapshot", value)
                emit({"kind": "pump-done", "name": name,
                      "duration_ns": time.monotonic_ns() - started,
                      "cpu_ns": time.process_time_ns() - cpu_started,
                      "sessions": len(getattr(snapshot, "sessions", ()) or ()),
                      "jobs": len(getattr(snapshot, "jobs", ()) or ()),
                      "resources": len(getattr(snapshot, "resources", ()) or ()),
                      "observations": _states(getattr(snapshot, "observations", {})),
                      "required_detail_diagnostics": _required_detail_diagnostics(snapshot)})
                return result
            observed.__name__ = getattr(producer, "__name__", "observed_producer")
            return original_init(pump, observed, interval, *args, **kwargs)
        RefreshPump.__init__ = init

        original_registry = dispatch.parsed_registry_rows
        def parsed_registry(path):
            started = time.monotonic_ns()
            rows = original_registry(path)
            try:
                info = Path(path).stat()
                generation = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
            except OSError:
                generation = None
            emit({"kind": "registry", "path": str(path), "generation": generation,
                  "rows": len(rows), "duration_ns": time.monotonic_ns() - started})
            return rows
        dispatch.parsed_registry_rows = parsed_registry

        state = {"input_id": 0, "pending_input": None, "flush_id": 0,
                 "refresh_depth": 0}
        original_read = render._CursesInput.read
        def read(reader, *args, **kwargs):
            value = original_read(reader, *args, **kwargs)
            ch = value[0] if isinstance(value, tuple) else value
            if ch not in (curses.ERR, getattr(curses, "KEY_RESIZE", -999999)):
                state["input_id"] += 1
                state["pending_input"] = state["input_id"]
                emit({"kind": "input-decoded", "input_id": state["input_id"],
                      "time_ns": time.monotonic_ns()})
            return value
        render._CursesInput.read = read

        def record_flush(path):
            state["flush_id"] += 1
            event = _frame_event(render)
            event.update({"kind": "flush", "flush_id": state["flush_id"],
                          "flush_path": path, "input_id": state["pending_input"],
                          "time_ns": time.monotonic_ns(), "process_times": _times(),
                          "observer_emit_ns": _emit_ns})
            state["pending_input"] = None
            emit(event)

        original_flush = curses.doupdate
        def flush():
            value = original_flush()
            if state["refresh_depth"] == 0:
                record_flush("curses.doupdate")
            return value
        curses.doupdate = flush

        original_wrapper = curses.wrapper
        def observed_wrapper(func, *args, **kwargs):
            def observed_main(stdscr, *call_args, **call_kwargs):
                def before_refresh():
                    state["refresh_depth"] += 1
                def after_refresh(succeeded):
                    state["refresh_depth"] -= 1
                    if succeeded:
                        record_flush("stdscr.refresh")
                return func(WindowRefreshObserver(stdscr, before_refresh, after_refresh),
                            *call_args, **call_kwargs)
            return original_wrapper(observed_main, *args, **kwargs)
        curses.wrapper = observed_wrapper
        emit({"kind": "observer-ready", "pid": os.getpid(), "argv": sys.argv,
              "python": sys.executable, "term": os.environ.get("TERM"),
              "agent_home": os.environ.get("AGENT_HOME"),
              "refresh_interval": os.environ.get("FLEET_REFRESH_INTERVAL"),
              "compute_interval": os.environ.get("FLEET_COMPUTE_HOST_INTERVAL"),
              "process_times": _times()})
        atexit.register(lambda: emit({"kind": "process-exit", "process_times": _times(),
                                     "observer_emit_ns": _emit_ns}))
    except Exception as error:
        emit({"kind": "observer-error", "component": "fleet-hooks",
              "error_type": type(error).__name__})

try:
    install()
except Exception as error:
    emit({"kind": "observer-error", "component": "install",
          "error_type": type(error).__name__})
'''


def _hash_file(path):
    target = Path(path)
    try:
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": str(target.resolve()), "sha256": digest.hexdigest(),
                "bytes": target.stat().st_size}
    except OSError as error:
        return {"path": str(target), "error": type(error).__name__}


def _git_provenance(tree):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

    def git(*args):
        try:
            value = subprocess.run(["git", *args], cwd=str(tree), env=env,
                                   text=True, capture_output=True, timeout=5, check=False)
            return value.stdout.strip() if value.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    status = git("status", "--porcelain", "--untracked-files=no") or ""
    main = git("rev-parse", "origin/main")
    head = git("rev-parse", "HEAD")
    ancestor = None
    if main and head:
        try:
            check = subprocess.run(["git", "merge-base", "--is-ancestor", main, head],
                                   cwd=str(tree), env=env, timeout=5, check=False)
            ancestor = check.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    return {"tree": str(Path(tree).resolve()), "head": head,
            "branch": git("branch", "--show-current"), "dirty": bool(status),
            "dirty_status_sha256": hashlib.sha256(status.encode()).hexdigest(),
            "origin_main": main, "origin_main_is_ancestor": ancestor}


def _proc_stats(pid):
    result = {"time_ns": time.monotonic_ns()}
    try:
        fields = Path("/proc/%d/stat" % pid).read_text(encoding="ascii").rsplit(")", 1)[1].split()
        result["user_seconds"] = int(fields[11]) / os.sysconf("SC_CLK_TCK")
        result["system_seconds"] = int(fields[12]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        result["user_seconds"] = result["system_seconds"] = None
    try:
        for line in Path("/proc/%d/status" % pid).read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                result["rss_kib"] = int(line.split()[1])
            elif line.startswith("VmHWM:"):
                result["rss_peak_kib"] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        result.setdefault("rss_kib", None)
        result.setdefault("rss_peak_kib", None)
    return result


def _valid_cache(path):
    if not path:
        return False
    target = Path(path)
    try:
        info = target.lstat()
        if (target.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_size > 1024 * 1024
                or stat.S_IMODE(info.st_mode) != 0o600):
            return False
        value = json.loads(target.read_text(encoding="utf-8"))
        return value.get("fresh_complete") is True and value.get("all_new") is True
    except (OSError, ValueError, TypeError):
        return False


def _cache_envelope(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return {key: value.get(key) for key in (
            "schema_version", "fresh_complete", "all_new", "generation",
            "observed_at", "source_freshness")}
    except (OSError, ValueError, TypeError):
        return None


def _production_environment(tree, cache_home, observer_dir, telemetry_fd,
                            launch_start_ns, runtime_dir):
    env = os.environ.copy()
    for name in (
        "AGENT_ROUTE_FILE", "AGENT_ROUTE_ID", "AGENT_ROUTE_NODE",
        "AGENT_OWNER_ROUTE_FILE", "AGENT_OWNER_ROUTE_ID", "AGENT_OWNER_ROUTE_HASH",
        "AGENT_DISPATCH_MODEL", "AGENT_DISPATCH_MODEL_ROLE",
        "AGENT_DISPATCH_MODEL_PROFILE", "AGENT_DISPATCH_REASONING",
        "AGENT_DISPATCH_EFFORT",
    ):
        env.pop(name, None)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(observer_dir) + (os.pathsep + existing if existing else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPYCACHEPREFIX"] = str(Path(runtime_dir) / "pycache")
    env.setdefault("TERM", "xterm-256color")
    env.update({
        "FLEET_BENCHMARK_SOURCE_TOOLS": str(Path(tree).resolve() / "tools"),
        "FLEET_BENCHMARK_CACHE_HOME": str(cache_home),
        "FLEET_BENCHMARK_TELEMETRY_FD": str(telemetry_fd),
        "FLEET_BENCHMARK_START_MONOTONIC_NS": str(launch_start_ns),
        "FLEET_BENCHMARK_OBSERVER_DIR": str(observer_dir),
    })
    return env


def _production_run(role, tree, cache_home, observer_dir, runtime_dir,
                    run_dir, *, keys=100, duration=60, seed=False,
                    event_timeout=2.0):
    tree = Path(tree).resolve()
    cache_home = Path(cache_home).resolve()
    runtime_dir = Path(runtime_dir).resolve()
    run_dir = Path(run_dir).resolve()
    raw_dir = runtime_dir / "production-raw" / run_dir.name
    raw_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(raw_dir, 0o700)
    run_dir.mkdir(parents=True, exist_ok=True)
    launcher = _production_argv(role, {role: tree})[0]
    if not Path(launcher).is_file():
        return {"role": role, "tree": str(tree), "launcher": launcher,
                "error": "launcher-missing", "keys": [], "exit_code": None}

    observer_dir = Path(observer_dir).resolve()
    observer_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(observer_dir, 0o700)
    _write_private_bytes(observer_dir / "sitecustomize.py", _observer_source().encode("utf-8"))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 168, 0, 0))
    telemetry_read, telemetry_write = os.pipe()
    os.set_blocking(master, False)
    os.set_blocking(telemetry_read, False)
    launch_start_ns = time.monotonic_ns()
    launch_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    env = _production_environment(tree, cache_home, observer_dir, telemetry_write,
                                  launch_start_ns, runtime_dir)
    keep_flags = ("TERM", "FLEET_REFRESH_INTERVAL", "FLEET_COMPUTE_HOST_INTERVAL",
                  "FLEET_MOUSE", "FLEET_PYTHON")
    flags = {key: env.get(key) for key in keep_flags}
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    observer_cpu_start = time.process_time()
    parent_wall_start = time.monotonic()
    proc = None
    transcript = bytearray()
    telemetry_buffer = bytearray()
    events = []
    rss_samples = []
    child_start = None
    failure = None
    selector = selectors.DefaultSelector()
    try:
        proc = subprocess.Popen(
            _production_argv(role, {role: tree}), cwd=str(tree), env=env,
            stdin=slave, stdout=slave, stderr=slave, close_fds=True,
            pass_fds=(telemetry_write,), start_new_session=True,
        )
        os.close(slave)
        slave = -1
        os.close(telemetry_write)
        telemetry_write = -1
        child_start = _proc_start(proc.pid)
        selector.register(master, selectors.EVENT_READ, "pty")
        selector.register(telemetry_read, selectors.EVENT_READ, "telemetry")

        def drain(timeout=0.05):
            for selected, _mask in selector.select(timeout):
                try:
                    data = os.read(selected.fd, 262144)
                except (BlockingIOError, OSError):
                    data = b""
                if selected.data == "pty" and data:
                    transcript.extend(data)
                elif selected.data == "telemetry" and data:
                    telemetry_buffer.extend(data)
                    while b"\n" in telemetry_buffer:
                        line, _, rest = telemetry_buffer.partition(b"\n")
                        telemetry_buffer[:] = rest
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if isinstance(record, dict):
                            events.append(record)

        def sample_process():
            if proc is not None and proc.poll() is None:
                row = _proc_stats(proc.pid)
                if (not rss_samples or row["time_ns"] - rss_samples[-1]["time_ns"]
                        >= 1_000_000_000):
                    rss_samples.append(row)

        if seed:
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                drain(0.05)
                sample_process()
                cache_path = next((item.get("cache_path") for item in events
                                   if item.get("kind") == "cache-configured"), None)
                if (any(item.get("kind") == "flush" and item.get("fresh_all_new")
                        for item in events) and _valid_cache(cache_path)):
                    break
                if proc.poll() is not None:
                    failure = "candidate-exited-before-all-new-cache"
                    break
            else:
                failure = "candidate-all-new-cache-timeout"
        else:
            schedule = _production_schedule(keys, duration)
            key_raw = []
            for index, offset in enumerate(schedule):
                deadline_ns = launch_start_ns + int(offset * 1e9)
                while time.monotonic_ns() < deadline_ns:
                    remaining = max(0.0, (deadline_ns - time.monotonic_ns()) / 1e9)
                    drain(min(0.1, remaining))
                    sample_process()
                value = b"\x1b[B" if index % 2 == 0 else b"\x1b[A"
                write_ns = time.monotonic_ns()
                try:
                    os.write(master, value)
                    write_error = None
                except OSError as error:
                    write_error = type(error).__name__
                key_raw.append({"sample": index, "scheduled_offset_seconds": offset,
                                "parent_write_ns": write_ns,
                                "key": "down" if index % 2 == 0 else "up",
                                "input_bytes_sha256": hashlib.sha256(value).hexdigest(),
                                "write_error": write_error})
            end_ns = launch_start_ns + int(duration * 1e9)
            while time.monotonic_ns() < end_ns:
                remaining = max(0.0, (end_ns - time.monotonic_ns()) / 1e9)
                drain(min(0.1, remaining))
                sample_process()
            settle_until = time.monotonic() + event_timeout
            while time.monotonic() < settle_until and proc.poll() is None:
                drain(0.05)
                sample_process()

        if proc is not None and proc.poll() is None:
            try:
                os.write(master, b"q")
            except OSError:
                pass
            exit_deadline = time.monotonic() + 2.0
            while time.monotonic() < exit_deadline and proc.poll() is None:
                drain(0.05)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            exit_code = proc.wait()
        else:
            exit_code = proc.wait() if proc is not None else None
        drain(0.05)
        if not seed:
            key_samples = _match_production_keys(key_raw, events)
        else:
            key_samples = []
        cache_path = next((item.get("cache_path") for item in events
                           if item.get("kind") == "cache-configured"), None)
        raw_pty = raw_dir / "pty.raw"
        raw_telemetry = raw_dir / "telemetry.json"
        _write_private_bytes(raw_pty, transcript)
        _write_private_json(raw_telemetry, events)
        times = [item for item in events if item.get("kind") == "process-exit"]
        process_exit_times = times[-1].get("process_times") if times else None
        flushes = [item for item in events if item.get("kind") == "flush"]
        replay = next((item for item in flushes if item.get("replay")
                       and item.get("frame_present")), None)
        fresh = next((item for item in flushes if item.get("fresh_complete")), None)
        all_new = next((item for item in flushes if item.get("fresh_all_new")), None)
        latencies = [item["end_to_flush_ms"] for item in key_samples
                     if item["end_to_flush_ms"] is not None]
        run = {
            "role": role, "tree": str(tree), "launcher": launcher,
            "launch_utc": launch_utc, "launch_start_ns": launch_start_ns,
            "wall_seconds": time.monotonic() - parent_wall_start,
            "launch_elapsed_to_replay_ms": ((replay["time_ns"] - launch_start_ns) / 1e6
                                             if replay else None),
            "launch_elapsed_to_fresh_ms": ((fresh["time_ns"] - launch_start_ns) / 1e6
                                            if fresh else None),
            "launch_elapsed_to_all_new_ms": ((all_new["time_ns"] - launch_start_ns) / 1e6
                                              if all_new else None),
            "fresh_status": ("observed" if fresh else "unsupported-or-unobserved"),
            "replay_status": ("observed" if replay else "unsupported-or-unobserved"),
            "cache_state_path": str(cache_path) if cache_path else None,
            "cache_envelope": _cache_envelope(cache_path) if cache_path else None,
            "cache_is_all_new": _valid_cache(cache_path),
            "screen_flushes": len(flushes), "key_count": len(key_samples),
            "key_painted_count": sum(item["status"] == "painted" for item in key_samples),
            "key_missing_count": sum(item["status"] != "painted" for item in key_samples),
            "key_latency_ms": {"count": len(latencies), "raw": latencies,
                               "p50": statistics.median(latencies) if latencies else None,
                               "p95": _percentile(latencies, 0.95),
                               "max": max(latencies) if latencies else None},
            "key_samples": key_samples, "rss_unit": "KiB", "rss_samples": rss_samples,
            "rss_sampling_interval_seconds": 1,
            "rss_current_kib": rss_samples[-1].get("rss_kib") if rss_samples else None,
            "rss_steady_median_kib": statistics.median(
                [row["rss_kib"] for row in rss_samples[-10:] if row.get("rss_kib") is not None]
            ) if any(row.get("rss_kib") is not None for row in rss_samples[-10:]) else None,
            "rss_peak_kib": max((row.get("rss_peak_kib") or row.get("rss_kib") or 0
                                 for row in rss_samples), default=None),
            "process_times": process_exit_times,
            "process_user_system_seconds": {
                "user": rss_samples[-1].get("user_seconds") if rss_samples else None,
                "system": rss_samples[-1].get("system_seconds") if rss_samples else None},
            "environment_flags": flags,
            "observer_ready": any(item.get("kind") == "observer-ready" for item in events),
            "agent_home": next((item.get("agent_home") for item in events
                                if item.get("kind") == "observer-ready"), None),
            "agent_home_matches_tree": next((item.get("agent_home") == str(tree)
                                              for item in events
                                              if item.get("kind") == "observer-ready"), None),
            "observer_emit_ns": next((item.get("observer_emit_ns") for item in reversed(events)
                                      if item.get("kind") in ("flush", "process-exit")), None),
            "observer_unsupported": [item for item in events
                                     if item.get("kind") == "observer-unsupported"],
            "pump_events": [item for item in events
                            if item.get("kind") in ("pump-start", "pump-done", "pump-error")],
            "pump_phase_samples": _pump_phase_samples(events),
            "session_job_resource_counts": [
                {key: item.get(key) for key in ("kind", "name", "sessions", "jobs", "resources")}
                for item in events if item.get("kind") == "pump-done"],
            "registry_observations": [
                {"path_sha256": hashlib.sha256(str(item.get("path", "")).encode()).hexdigest(),
                 "generation": item.get("generation"), "rows": item.get("rows"),
                 "duration_ns": item.get("duration_ns")}
                for item in events if item.get("kind") == "registry"],
            "exit_code": exit_code, "error": failure,
            "process_start_ticks": child_start,
            "pty_raw": {"path": str(raw_pty), "sha256": hashlib.sha256(transcript).hexdigest(),
                        "bytes": len(transcript)},
            "telemetry_raw": {"path": str(raw_telemetry),
                              "sha256": hashlib.sha256(raw_telemetry.read_bytes()).hexdigest(),
                              "bytes": raw_telemetry.stat().st_size,
                              "event_count": len(events)},
            "observer_cpu_seconds_parent": max(0.0, time.process_time() - observer_cpu_start),
        }
        cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        run["child_rusage_cpu_seconds"] = {
            "user": max(0.0, cpu_after.ru_utime - cpu_before.ru_utime),
            "system": max(0.0, cpu_after.ru_stime - cpu_before.ru_stime)}
        run["cache_seed_valid"] = _valid_cache(cache_path) if seed else None
        if failure:
            run["status"] = "failed"
        elif run.get("agent_home_matches_tree") is False:
            run["status"] = "failed"
            run["error"] = "AGENT_HOME does not resolve to the launched source tree"
        elif seed:
            run["status"] = "seeded" if run["cache_seed_valid"] else "seed-incomplete"
        else:
            run["status"] = "completed" if exit_code == 0 else "nonzero-exit"
        return run
    except BaseException as error:
        failure = type(error).__name__
        return {"role": role, "tree": str(tree), "launcher": launcher,
                "status": "failed", "error": failure, "keys": [],
                "exit_code": proc.poll() if proc else None}
    finally:
        if proc is not None and proc.poll() is None:
            if _proc_start(proc.pid) == child_start:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                if _proc_start(proc.pid) == child_start:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.wait()
        selector.close()
        for fd in (master, slave, telemetry_read, telemetry_write):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _cache_files(cache_home):
    root = Path(cache_home)
    if not root.is_dir():
        return []
    return sorted(path.relative_to(root).as_posix() for path in root.rglob(
        "fleet2-observation-*.json") if path.is_file() and not path.is_symlink())


def _cache_fingerprints(cache_home):
    root = Path(cache_home)
    result = []
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("fleet2-observation-*.json")):
        if path.is_file() and not path.is_symlink():
            fingerprint = _hash_file(path)
            fingerprint["path"] = path.relative_to(root).as_posix()
            result.append(fingerprint)
    return result


def _production_campaign(args):
    candidate = Path(args.source_tree).resolve()
    trees = {"base": Path(args.base_tree).resolve(),
             "candidate": candidate, "legacy": Path(args.legacy_tree).resolve()}
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    evidence_dir = Path(args.evidence_dir).expanduser().resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    campaign_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    campaign_id += "-%d-%d" % (os.getpid(), time.monotonic_ns() % 1_000_000_000)
    campaign_runtime = runtime_dir / ("fleet2-production-" + campaign_id)
    campaign_runtime.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(campaign_runtime, 0o700)
    observer_dir = campaign_runtime / "observer"
    observer_dir.mkdir(mode=0o700)
    os.chmod(observer_dir, 0o700)
    _write_private_bytes(observer_dir / "sitecustomize.py", _observer_source().encode("utf-8"))
    trees_provenance = {}
    for role, tree in trees.items():
        launcher = Path(_production_argv(role, trees)[0])
        trees_provenance[role] = {
            **_git_provenance(tree),
            "launcher": _hash_file(launcher),
            "fleet_python": _hash_file(tree / "tools" / "fleet" / "fleet.py"),
        }
    interpreter = os.environ.get("FLEET_PYTHON") or shutil.which("python3") or sys.executable
    interpreter_path = shutil.which(interpreter) or interpreter
    cache_states = ("absent", "present") if args.cache_state == "both" else (args.cache_state,)
    result = {
        "schema": "fleet-production-navigation-v1",
        "campaign_id": campaign_id,
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "candidate_tree": str(candidate), "trees": trees_provenance,
        "interpreter": _hash_file(interpreter_path),
        "host": socket.gethostname(),
        "euid_sha256": hashlib.sha256(str(os.geteuid()).encode()).hexdigest(),
        "platform": platform.platform(), "cpu_count": os.cpu_count(),
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "runtime_dir": str(campaign_runtime), "evidence_dir": str(evidence_dir),
        "terminal": {"columns": 168, "rows": 45,
                     "term": os.environ.get("TERM") or "xterm-256color"},
        "sample_contract": {"keys": args.keys, "duration_seconds": args.duration,
                            "replicates": args.replicates,
                            "key_schedule_seconds": _production_schedule(args.keys, args.duration),
                            "key_pattern": "down,up repeated",
                            "cache_states": list(cache_states),
                            "page_cache": "not flushed; process-cold and filesystem state recorded separately"},
        "runs": [], "cache_seeds": [], "state_errors": [],
    }

    for cache_state in cache_states:
        cache_root = campaign_runtime / "cache" / cache_state
        if cache_state == "present":
            seed_home = cache_root / "candidate"
            seed_dir = evidence_dir / "seeds" / campaign_id
            seed = _production_run("candidate", candidate, seed_home, observer_dir,
                                   campaign_runtime, seed_dir, keys=0, duration=0,
                                   seed=True, event_timeout=args.event_timeout)
            seed.update({"cache_state": "present-seed", "role": "candidate"})
            _json(seed_dir / "summary.json", seed)
            result["cache_seeds"].append(seed)
            if seed.get("status") != "seeded" or not seed.get("cache_seed_valid"):
                result["state_errors"].append({"cache_state": "present",
                                               "reason": "candidate all-new cache seed did not validate"})
                continue

        for replicate, order in enumerate(_production_order(args.replicates), start=1):
            for role in order:
                if cache_state == "present" and role == "candidate":
                    role_cache = cache_root / role
                else:
                    role_cache = cache_root / ("r%d-%s" % (replicate, role))
                before_files = _cache_files(role_cache)
                before_fingerprints = _cache_fingerprints(role_cache)
                if cache_state == "absent" and before_files:
                    result["state_errors"].append({"cache_state": cache_state,
                                                   "role": role,
                                                   "reason": "isolated absent cache was not empty"})
                    continue
                if cache_state == "present" and role == "candidate" and not before_files:
                    result["state_errors"].append({"cache_state": cache_state,
                                                   "role": role,
                                                   "reason": "candidate cache disappeared before fresh process"})
                    continue
                run_dir = evidence_dir / "runs" / cache_state / ("r%d-%s" % (replicate, role))
                run = _production_run(role, trees[role], role_cache, observer_dir,
                                      campaign_runtime, run_dir, keys=args.keys,
                                      duration=args.duration,
                                      event_timeout=args.event_timeout)
                run.update({"cache_state": cache_state, "replicate": replicate,
                            "cache_files_at_launch": before_fingerprints,
                            "cache_files_after_run": _cache_fingerprints(role_cache),
                            "cache_at_launch": "present" if before_files else "absent",
                            "order": order})
                _json(run_dir / "summary.json", run)
                result["runs"].append(run)

    groups = {}
    for run in result["runs"]:
        key = run["cache_state"] + "/" + run["role"]
        groups.setdefault(key, []).append(run)
    result["groups"] = {}
    for key, runs in groups.items():
        for metric in ("launch_elapsed_to_replay_ms", "launch_elapsed_to_fresh_ms",
                       "launch_elapsed_to_all_new_ms"):
            values = [run[metric] for run in runs if run.get(metric) is not None]
            result["groups"].setdefault(key, {})[metric] = {
                "count": len(values), "raw": values,
                "median": statistics.median(values) if values else None,
                "min": min(values) if values else None, "max": max(values) if values else None}
        latencies = [sample["end_to_flush_ms"] for run in runs
                     for sample in run.get("key_samples", ())
                     if sample.get("end_to_flush_ms") is not None]
        result["groups"][key]["key_latency_ms"] = {
            "count": len(latencies), "raw": latencies,
            "p50": statistics.median(latencies) if latencies else None,
            "p95": _percentile(latencies, 0.95), "max": max(latencies) if latencies else None}
    failures = [run for run in result["runs"]
                if run.get("status") in ("failed", "nonzero-exit")]
    result["status"] = "failed" if failures or result["state_errors"] else "complete"
    result["failure_count"] = len(failures) + len(result["state_errors"])
    result_path = evidence_dir / "production.json"
    result["result_path"] = str(result_path)
    _json(result_path, result)
    return result


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     indent=2) + "\n", encoding="utf-8")


def _write_private_bytes(path, data):
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _write_private_json(path, value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n"
    _write_private_bytes(path, data.encode("utf-8"))


def _load_fixture(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("fixture_schema") != "peer-navigation-v1" or not raw.get("rows"):
        raise ValueError("unsupported or empty peer navigation fixture")
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode()).hexdigest()
    return raw, digest


def _isolate_runtime(evidence_dir, fixture, registry_count):
    """Keep every runtime lookup in this probe inside its evidence sandbox."""
    root = Path(evidence_dir).resolve()
    for name, child in (("HOME", "sandbox-home"),
                        ("XDG_CONFIG_HOME", "sandbox-config"),
                        ("XDG_DATA_HOME", "sandbox-data"),
                        ("XDG_STATE_HOME", "sandbox-state")):
        path = root / child
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    os.environ["HERDR_SOCKET_PATH"] = str(root / "sandbox-herdr.sock")
    os.environ["TERM"] = "xterm-256color"
    os.environ["LC_ALL"] = "C.UTF-8"

    assignments = {}
    for row in fixture["rows"]:
        for direction, harness_key, sid_key in (
                ("sent", "to_harness", "to_session_id"),
                ("recv", "from_harness", "from_session_id")):
            peer = row.get(direction) or {}
            harness, sid = peer.get(harness_key), peer.get(sid_key)
            if harness in {"claude", "codex", "opencode"} and isinstance(sid, str) and sid:
                assignments.setdefault((harness, sid), None)
    used_tags = set()
    next_tag = 0
    rows = []
    for index, (harness, sid) in enumerate(sorted(assignments)):
        if harness == "claude":
            tag = "aa"  # Claude may retain colliding derived tags.
        else:
            while "%02x" % next_tag in used_tags:
                next_tag += 1
            tag = "%02x" % next_tag
            used_tags.add(tag)
            next_tag += 1
        rows.append({"harness": harness, "session_id": sid, "tag": tag,
                     "first_seen": index + 1, "started_at": index + 1})
    for index in range(max(0, registry_count - len(rows))):
        rows.append({"harness": "claude", "session_id": "benchmark-registry-%05d" % index,
                     "tag": "aa", "first_seen": len(rows) + 1,
                     "started_at": len(rows) + 1})
    target = Path(os.environ["XDG_STATE_HOME"]) / "agent-fleet" / "tag-assignments.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _json(target, {"version": 1, "assignments": rows})


def _rss_pss():
    result = {}
    try:
        for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in {"Rss", "Pss", "Pss_Anon", "Private_Dirty", "Anonymous"}:
                result[key + "_KiB"] = int(value.strip().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return result


def _proc_start(pid):
    try:
        tail = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
        return tail[19]
    except (OSError, IndexError):
        return None


def _sessions(fixture, count, *, protected=False):
    from fleet.model import Session
    values = []
    rows = fixture["rows"]
    for i in range(count):
        row = rows[i] if i < len(rows) else rows[i % len(rows)]
        sid = row["session_id"] if i < len(rows) else "%s-%04d" % (row["session_id"], i)
        pid = 9500000 + i
        proc_start = "fixture-start-%04d" % i
        if protected and i == 0:
            pid, proc_start = os.getpid(), _proc_start(os.getpid())
        elif protected and i == 1:
            parent = os.getppid()
            pid, proc_start = parent, _proc_start(parent)
        session = Session(
            harness=row["harness"], pid=pid, proc_start=proc_start,
            cwd=fixture["cwd"], slug="peer-fixture-%04d" % i,
            title=row.get("title", "peer fixture %04d" % i), session_id=sid,
            liveness="working", elapsed_min=i + 1, session_tag=row.get("session_tag"),
            process_role="conversation")
        session.peer_last_sent = dict(row["sent"]) if row.get("sent") else None
        session.peer_last_recv = dict(row["recv"]) if row.get("recv") else None
        values.append(session)
    return values


def _forbidden(name):
    def fail(*_args, **_kwargs):
        raise AssertionError("benchmark reached forbidden operation: " + name)
    return fail


def _setup(source_tree, fixture_path):
    tools_dir = str(Path(source_tree) / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    fixture, fixture_hash = _load_fixture(fixture_path)
    from fleet import control, gitinfo, render, session_tags
    render._API_DISABLED = True
    render._REPO_FOLD = {}
    render._ROUTE_FOLD = {}
    render._OFFSET = 0
    render._SELECT_MODE = False
    render._CURSOR_ID = None
    render._collect_memory = lambda: None
    render._collect_governor = lambda: None
    control.kill_target = _forbidden("control.kill_target")
    control.close_registry_row = _forbidden("control.close_registry_row")
    # A missing fixture assignment must fail inside this isolated probe rather
    # than seed tags by scanning a real Herdr or harness registry.
    session_tags.assign = _forbidden("session_tags.assign")
    # The detail pump still follows its normal copy/merge publication path, but
    # it cannot start a second inventory, provider, GPU, or repository observer.
    gitinfo.enrich_entities = lambda *_args, **_kwargs: None
    return fixture, fixture_hash, render


def _display_snapshot(rows, memory=None):
    """Use the candidate's producer projection when present; sealed baseline has none."""
    try:
        from fleet.display_snapshot import build, extend
    except ImportError:
        return None
    return extend(build(rows), memory=memory)


def _display_kwargs(display):
    return {"display_snapshot": display} if display is not None else {}


def _live_snapshot(rows):
    from fleet.refresh import LiveSnapshot
    value = LiveSnapshot(sessions=rows)
    display = _display_snapshot(rows)
    if display is not None and hasattr(value, "display_snapshot"):
        value.display_snapshot = display
    return value


def _preview(source_tree, fixture_path):
    fixture, fixture_hash, render = _setup(source_tree, fixture_path)
    cases = []
    for harness in ("claude", "codex", "opencode"):
        rows = _sessions(fixture, len(fixture["rows"]))
        for session in rows:
            session.harness = harness
            session.session_id = "%s-%s" % (harness, session.session_id)
        for width in (168, 139, 138, 137, 120, 100, 71, 70, 69, 60, 46):
            render.reset_selection()
            render._REPO_FOLD = {}
            render._OFFSET = 0
            layout = render._layout_mode(width)
            display = _display_snapshot(rows)
            lines = render._build_lines(rows, [], "both", width < render._NARROW_CUTOFF,
                                        0, layout=layout, term_width=width,
                                        usage_snapshots={}, **_display_kwargs(display))
            text_lines = ["".join(text for text, _key in line or []) for line in lines]
            cases.append({"harness": harness, "width": width, "layout": layout,
                          "logical_rows": len(lines), "lines": text_lines,
                          "has_forward": any("→" in line for line in text_lines),
                          "has_reverse": any("←" in line for line in text_lines),
                          "has_both": any("↔" in line for line in text_lines),
                          "overflow": any(sum(render._dw(text) for text, _ in line or []) > width
                                          for line in lines)})
    return {"fixture_sha256": fixture_hash, "source_tree": str(source_tree),
            "cases": cases}


def _counted_build(render, sessions, width, iterations):
    from fleet import gitinfo, model
    counters = {"project_of": 0, "render_project_of": 0,
                "display_project_of": 0, "resolve_gitdir": 0, "peer_tag": 0}
    orig_project, orig_model_project = render.project_of, model.project_of
    orig_gitdir = gitinfo.resolve_gitdir
    orig_tag = render._resolve_session_tag

    def project(*args, **kwargs):
        counters["project_of"] += 1
        counters["render_project_of"] += 1
        return orig_project(*args, **kwargs)

    def model_project(*args, **kwargs):
        counters["project_of"] += 1
        counters["display_project_of"] += 1
        return orig_model_project(*args, **kwargs)

    def gitdir(*args, **kwargs):
        counters["resolve_gitdir"] += 1
        return orig_gitdir(*args, **kwargs)

    def tag(*args, **kwargs):
        counters["peer_tag"] += 1
        return orig_tag(*args, **kwargs)

    render.project_of, model.project_of = project, model_project
    gitinfo.resolve_gitdir, render._resolve_session_tag = gitdir, tag
    memory = {"periodic_curate": {"total": 1, "current_cwd": "/tmp/navigation-curator"}}
    display = _display_snapshot(sessions, memory=memory)
    display_project_calls = counters["display_project_of"]
    before_builds = dict(counters)
    samples, cpu = [], []
    before_mem = _rss_pss()
    tracemalloc.start()
    try:
        for _ in range(iterations):
            wall0, cpu0 = time.perf_counter_ns(), time.process_time_ns()
            render._build_lines(sessions, [], "both", width < render._NARROW_CUTOFF,
                                0, layout=render._layout_mode(width), memory=memory,
                                term_width=width, usage_snapshots={},
                                **_display_kwargs(display))
            samples.append(time.perf_counter_ns() - wall0)
            cpu.append(time.process_time_ns() - cpu0)
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        render.project_of, model.project_of = orig_project, orig_model_project
        gitinfo.resolve_gitdir, render._resolve_session_tag = orig_gitdir, orig_tag
    return {"sessions": len(sessions), "width": width, "iterations": iterations,
            "wall_median_ms": statistics.median(samples) / 1e6,
            "wall_max_ms": max(samples) / 1e6,
            "cpu_median_ms": statistics.median(cpu) / 1e6,
            "calls_per_build": {key: (value - before_builds[key]) / iterations
                                for key, value in counters.items()},
            "display_snapshot_project_calls": display_project_calls,
            "rss_pss_before": before_mem, "rss_pss_after": _rss_pss(),
            "tracemalloc_current_bytes": current, "tracemalloc_peak_bytes": peak,
            "ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def _scale_one(source_tree, fixture_path, count, width, registry_count):
    fixture, fixture_hash, render = _setup(source_tree, fixture_path)
    rows = _sessions(fixture, count)
    result = _counted_build(render, rows, width, 20)
    result.update(fixture_sha256=fixture_hash, source_tree=str(source_tree),
                  registry_assignments=registry_count,
                  python=sys.version.split()[0], pid=os.getpid())
    return result


def _retention(source_tree, fixture_path):
    fixture, fixture_hash, render = _setup(source_tree, fixture_path)
    import copy
    import gc
    from fleet.refresh import LiveSnapshot
    rows = _sessions(fixture, 100)
    base = _live_snapshot(rows)
    tracemalloc.start()
    before = _rss_pss()
    cpu0 = time.process_time_ns()
    fixed = None
    churn = None
    fixed_retained = None
    fixed_rss = None
    for i in range(60):
        source = base if i < 30 else _live_snapshot(_sessions(fixture, 100))
        copied = copy.deepcopy(source)
        render._build_lines(copied.sessions, [], "both", False, 0, term_width=168,
                            usage_snapshots={}, **_display_kwargs(
                                getattr(copied, "display_snapshot", None)))
        if i < 30:
            fixed = copied
        else:
            churn = copied
        if i in (0, 9, 29, 30, 39, 59):
            gc.collect()
        if i == 29:
            fixed_retained = tracemalloc.get_traced_memory()[0]
            fixed_rss = _rss_pss()
    current, peak = tracemalloc.get_traced_memory()
    gc.collect()
    after_gc = tracemalloc.get_traced_memory()[0]
    after = _rss_pss()
    cpu_ms = (time.process_time_ns() - cpu0) / 1e6
    tracemalloc.stop()
    return {"fixture_sha256": fixture_hash, "source_tree": str(source_tree),
            "profile": "30 fixed + 30 roster-churn snapshot copies, 100 sessions, 1000 synthetic registry rows",
            "iterations": 60, "registry_assignments": 1000, "rss_pss_before": before,
            "fixed_profile_retained_bytes": fixed_retained, "fixed_profile_rss_pss": fixed_rss,
            "rss_pss_after_gc": after, "tracemalloc_current_before_gc_bytes": current,
            "tracemalloc_retained_after_gc_bytes": after_gc,
            "tracemalloc_peak_bytes": peak, "process_cpu_ms": cpu_ms,
            "ru_maxrss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "live_snapshot_refs": int(fixed is not None) + int(churn is not None)}


def _emit(fd, record):
    data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _pty_child(source_tree, fixture_path, telemetry_fd, width, mode):
    live_collecting = mode == "explorer-collecting"
    live_collect_all = None
    if live_collecting:
        tools_dir = str(Path(source_tree) / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        fixture, fixture_hash = _load_fixture(fixture_path)
        from fleet import render
        from fleet.collectors import collect_all as live_collect_all
        render._API_DISABLED = True
        render._EXPLORER_ENABLED = True
        if not hasattr(render, "_EXPLORER_RUNTIME"):
            class RuntimeProbe:
                def collection_options(self, first_complete):
                    return {"fast_first": bool(first_complete)}

                def prepare(self, _snapshot, _detail, *, vm, generation, now):
                    return vm, {"ready": True, "generation": generation}

                def loading_frame(self, _cols, _rows):
                    return None

                def apply_effects(self, _effects, _vm):
                    return None

            render._explorer_runtime = lambda: RuntimeProbe()
        rows = []
    else:
        fixture, fixture_hash, render = _setup(source_tree, fixture_path)
        rows = _sessions(fixture, 100, protected=True)
    current = {"event_id": 0, "record": None, "last_build_ns": 0,
               "merge": None, "input_cpu_ns": 0}
    counts = {"build": 0, "project_of": 0, "tag_resolver": 0}
    orig_build, orig_draw = render._build_lines, render._draw
    orig_read = render._CursesInput.read
    orig_flush = curses.doupdate
    orig_select, orig_base = render._handle_select_key, render._handle_base_key
    from fleet import details, gitinfo, model
    orig_merge = details.merge
    orig_project, orig_model_project = render.project_of, model.project_of
    orig_tag = render._resolve_session_tag
    first_frame = {"start_ns": time.perf_counter_ns(), "reported": False}

    def project(*args, **kwargs):
        counts["project_of"] += 1
        return orig_project(*args, **kwargs)

    def model_project(*args, **kwargs):
        counts["project_of"] += 1
        return orig_model_project(*args, **kwargs)

    def tag(*args, **kwargs):
        counts["tag_resolver"] += 1
        return orig_tag(*args, **kwargs)

    def build(*args, **kwargs):
        start = time.perf_counter_ns()
        result = orig_build(*args, **kwargs)
        current["last_build_ns"] = time.perf_counter_ns() - start
        counts["build"] += 1
        return result

    def merge(*args, **kwargs):
        start = time.perf_counter_ns()
        result = orig_merge(*args, **kwargs)
        current["merge"] = {"start_ns": start, "end_ns": time.perf_counter_ns()}
        return result

    def read(reader, stdscr, **kwargs):
        wall0, thread0 = time.perf_counter_ns(), time.thread_time_ns()
        ch, mouse = orig_read(reader, stdscr, **kwargs)
        if ch != curses.ERR and ch != getattr(curses, "KEY_RESIZE", -9999):
            current["event_id"] += 1
            current["record"] = {"event_id": current["event_id"], "key": ch,
                                 "read_start_ns": wall0,
                                 "decoded_ns": time.perf_counter_ns(),
                                 "decode_thread_ns": time.thread_time_ns() - thread0,
                                 "before_cursor": render._CURSOR_ID,
                                 "before_offset": render._OFFSET,
                                 "merge": current.get("merge"),
                                 "process_cpu_ns": time.process_time_ns(),
                                 "thread_cpu_ns": time.thread_time_ns()}
            _emit(telemetry_fd, {"kind": "decoded", **current["record"]})
        return ch, mouse

    def select_key(ch):
        record = current.get("record")
        start = time.perf_counter_ns()
        result = orig_select(ch)
        if record is not None:
            record["selection_ns"] = time.perf_counter_ns() - start
            record["after_cursor"] = render._CURSOR_ID
        return result

    def base_key(ch, body_h):
        record = current.get("record")
        start = time.perf_counter_ns()
        result = orig_base(ch, body_h)
        if record is not None:
            record["selection_ns"] = time.perf_counter_ns() - start
            record["after_offset"] = render._OFFSET
        return result

    def draw(stdscr, *args, **kwargs):
        result = orig_draw(stdscr, *args, **kwargs)
        if (live_collecting and not first_frame["reported"]
                and getattr(render, "_EXPLORER_GENERATION", 0) > 0
                and getattr(render, "_EXPLORER_FRAME", None) is not None):
            first_frame["reported"] = True
            _emit(telemetry_fd, {
                "kind": "first-complete-frame",
                "elapsed_ms": (time.perf_counter_ns() - first_frame["start_ns"]) / 1e6,
                "generation": render._EXPLORER_GENERATION,
                "readiness": getattr(render, "_EXPLORER_READINESS", None),
            })
        record = current.get("record")
        if record is not None and record.get("reported") is not True:
            record["draw_end_ns"] = time.perf_counter_ns()
            record["draw_thread_ns"] = time.thread_time_ns()
            record["draw_process_cpu_ns"] = time.process_time_ns()
            record["draw_ns"] = record["draw_end_ns"] - record["decoded_ns"]
            record["build_ns"] = current["last_build_ns"]
            record["flush_ns"] = current.get("last_flush_ns", 0)
            record["selection_mode"] = bool(render._SELECT_MODE)
            record["cursor"] = render._CURSOR_ID
            record["offset"] = render._OFFSET
            record["size"] = list(stdscr.getmaxyx())
            record["counts"] = dict(counts)
            record["reported"] = True
            _emit(telemetry_fd, {"kind": "painted", **record})
        return result

    def flush():
        start = time.perf_counter_ns()
        try:
            return orig_flush()
        finally:
            current["last_flush_ns"] = time.perf_counter_ns() - start

    curses.doupdate = flush
    render.project_of, model.project_of = project, model_project
    render._resolve_session_tag = tag
    render._build_lines, render._draw = build, draw
    render._CursesInput.read = read
    render._handle_select_key, render._handle_base_key = select_key, base_key
    details.merge = merge
    render._REPO_FOLD = {}
    render._ROUTE_FOLD = {}
    render._PROCESS_VIEW = False
    render._SELECT_MODE = False
    render._CURSOR_ID = None
    render._OFFSET = 0

    collector_state = {"snapshot": None, "ready": threading.Event(),
                       "stop": threading.Event(), "thread": None}

    def live_collection_loop():
        index = 0
        while not collector_state["stop"].is_set():
            started = time.perf_counter_ns()
            _emit(telemetry_fd, {"kind": "collection-start", "index": index,
                                 "time_ns": started})
            try:
                value = live_collect_all(harness_filter=None, fast_first=(index == 0))
                collector_state["snapshot"] = value
                collector_state["ready"].set()
                _emit(telemetry_fd, {"kind": "collection-done", "index": index,
                                     "duration_ms": (time.perf_counter_ns() - started) / 1e6,
                                     "sessions": len(getattr(value, "sessions", ())),
                                     "jobs": len(getattr(value, "jobs", ()))})
            except BaseException as exc:
                _emit(telemetry_fd, {"kind": "collection-error", "index": index,
                                     "error": repr(exc)})
                if index == 0:
                    collector_state["ready"].set()
                return
            index += 1
            if collector_state["stop"].wait(0.01):
                return

    if live_collecting:
        collector_state["thread"] = threading.Thread(
            target=live_collection_loop, name="benchmark-live-collection", daemon=True)
        collector_state["thread"].start()

    def collect_all(**_kwargs):
        start = time.perf_counter_ns()
        if live_collecting:
            if not collector_state["ready"].wait(240):
                raise TimeoutError("live collector did not publish its first snapshot")
            value = collector_state["snapshot"]
            if value is None:
                raise RuntimeError("live collector failed before first snapshot")
        else:
            value = _live_snapshot(rows)
        current["last_collection_ns"] = time.perf_counter_ns() - start
        _emit(telemetry_fd, {"kind": "snapshot-produced", "duration_ns": current["last_collection_ns"],
                             "project_lookups": counts["project_of"], "pid": os.getpid()})
        return value

    collect_all.__name__ = "synthetic_collect_all"
    if live_collecting:
        def close():
            collector_state["stop"].set()
            thread = collector_state["thread"]
            if thread is not None:
                thread.join(2.0)
        collect_all.close = close
    _emit(telemetry_fd, {"kind": "child-start", "pid": os.getpid(),
                         "proc_start": _proc_start(os.getpid()),
                         "source_tree": str(source_tree), "fixture_sha256": fixture_hash,
                         "width": width, "mode": mode,
                         "safety_fixture_pids": [row.pid for row in rows[:2]],
                         "counts": counts})
    curses.wrapper(render._loop, collect_all, None, "both", 20.0)


def _pty_run(source_tree, fixture_path, evidence_dir, width, mode, replicate, keys=100,
             event_timeout=15):
    run_dir = Path(evidence_dir) / ("pty-%s-%03d-r%d" % (mode, width, replicate))
    run_dir.mkdir(parents=True, exist_ok=True)
    read_fd, write_fd = os.pipe()
    pid, master = pty.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            _pty_child(source_tree, fixture_path, write_fd, width, mode)
            os._exit(0)
        except BaseException as exc:
            try:
                _emit(write_fd, {"kind": "child-error", "error": repr(exc)})
            finally:
                os._exit(2)
    os.close(write_fd)
    start = _proc_start(pid)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 36, width, 0, 0))
    os.set_blocking(master, False)
    os.set_blocking(read_fd, False)
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ, "pty")
    selector.register(read_fd, selectors.EVENT_READ, "telemetry")
    transcript = bytearray()
    pipe_buffer = bytearray()
    telemetry, painted, sent = [], {}, {}
    counter = 0
    child_error = None

    def drain(timeout=0.1):
        nonlocal child_error
        for key, _ in selector.select(timeout):
            try:
                data = os.read(key.fd, 262144)
            except BlockingIOError:
                data = b""
            except OSError:
                data = b""
            if key.data == "pty" and data:
                transcript.extend(data)
            elif key.data == "telemetry" and data:
                pipe_buffer.extend(data)
                while b"\n" in pipe_buffer:
                    line, _, rest = pipe_buffer.partition(b"\n")
                    pipe_buffer[:] = rest
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    telemetry.append(record)
                    if record.get("kind") == "painted":
                        painted[record["event_id"]] = record
                    elif record.get("kind") == "child-error":
                        child_error = record.get("error")

    def wait_event(event_id, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            drain(0.02)
            if event_id in painted:
                return painted[event_id]
            if child_error or os.waitpid(pid, os.WNOHANG)[0]:
                raise RuntimeError("PTY child exited before event %d: %r" % (event_id, child_error))
        raise TimeoutError("timed out waiting for PTY paint event %d" % event_id)

    def wait_first_complete(timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            drain(0.02)
            frame = next((item for item in telemetry
                          if item.get("kind") == "first-complete-frame"), None)
            if frame is not None:
                return frame
            if child_error or os.waitpid(pid, os.WNOHANG)[0]:
                raise RuntimeError("PTY child exited before first complete frame: %r" % child_error)
        raise TimeoutError("timed out waiting for first complete Explorer frame")

    def send(data, warmup=False):
        nonlocal counter
        counter += 1
        write_ns = time.perf_counter_ns()
        os.write(master, data)
        sent[counter] = {"write_ns": write_ns, "warmup": warmup, "bytes": data.hex()}
        return wait_event(counter, timeout=event_timeout)

    try:
        deadline = time.monotonic() + 5
        while not any(r.get("kind") == "child-start" for r in telemetry):
            drain(0.02)
            if time.monotonic() > deadline:
                raise TimeoutError("PTY child did not start")
        if mode == "selection":
            send(b"s", warmup=True)
        # Exercise the arrow decoder in selection mode too, so its latency
        # includes the same escape-sequence path as the user's navigation.
        key_bytes = b"\x1b[B"
        other = b"\x1b[A"
        for i in range(10):
            send(key_bytes if i % 2 == 0 else other, warmup=True)
        if mode == "explorer-collecting":
            wait_first_complete()
        measured = []
        for i in range(keys):
            data = key_bytes if i % 2 == 0 else other
            paint = send(data)
            event_id = counter
            row = dict(paint)
            row["parent_write_ns"] = sent[event_id]["write_ns"]
            row["parent_painted_receipt_ns"] = time.perf_counter_ns()
            row["end_to_paint_ms"] = (row["parent_painted_receipt_ns"] - row["parent_write_ns"]) / 1e6
            measured.append(row)
        os.write(master, b"q")
        end = time.monotonic() + 2
        while time.monotonic() < end:
            drain(0.02)
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
        else:
            # Cleanup is scoped to this exact fixture child and its validated start time.
            if _proc_start(pid) == start:
                os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
        if child_error:
            raise RuntimeError(child_error)
        (run_dir / "pty.raw").write_bytes(transcript)
        _json(run_dir / "telemetry.json", telemetry)
        values = [row["end_to_paint_ms"] for row in measured]
        collection_started = {item["index"]: item["time_ns"] for item in telemetry
                              if item.get("kind") == "collection-start"}
        collection_done = {item["index"]: item["duration_ms"] for item in telemetry
                           if item.get("kind") == "collection-done"}
        last_paint_ns = max((row.get("draw_end_ns", 0) for row in measured), default=0)
        active_samples = sum(1 for row in measured if any(
            started <= row.get("decoded_ns", 0)
            and (index not in collection_done
                 or row.get("decoded_ns", 0) < started + collection_done[index] * 1e6)
            for index, started in collection_started.items()))
        return {"source_tree": str(source_tree), "mode": mode, "width": width,
                "replicate": replicate, "keys": len(measured),
                "median_ms": statistics.median(values),
                "max_ms": max(values),
                "mean_child_decode_thread_ms": statistics.mean(
                    row.get("decode_thread_ns", 0) for row in measured) / 1e6,
                "mean_child_selection_ms": statistics.mean(
                    row.get("selection_ns", 0) for row in measured) / 1e6,
                "mean_child_build_ms": statistics.mean(
                    row.get("build_ns", 0) for row in measured) / 1e6,
                "mean_child_flush_ms": statistics.mean(
                    row.get("flush_ns", 0) for row in measured) / 1e6,
                "mean_child_draw_after_decode_ms": statistics.mean(
                    row.get("draw_ns", 0) for row in measured) / 1e6,
                "first_complete_frame_ms": next((item["elapsed_ms"] for item in telemetry
                                                  if item.get("kind") == "first-complete-frame"), None),
                "key_samples_during_collection": active_samples,
                "collection_runs_started": len(collection_started),
                "collection_runs_completed": len(collection_done),
                "last_paint_ns": last_paint_ns,
                "collection_durations_ms": [item["duration_ms"] for item in telemetry
                                             if item.get("kind") == "collection-done"],
                "parent_raw_pty_bytes": len(transcript),
                "child_start_ticks": start,
                "sample_events": measured,
                "raw_paths": [str(run_dir / "pty.raw"), str(run_dir / "telemetry.json")]}
    except BaseException as exc:
        # A timed-out sample still owns this exact forked PTY child. Save its partial
        # transcript and receipts, then stop only the PID whose start tick we captured.
        (run_dir / "pty.partial.raw").write_bytes(transcript)
        _json(run_dir / "telemetry.partial.json", telemetry)
        if _proc_start(pid) == start:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        _json(run_dir / "failure.json", {"error": repr(exc), "pid": pid,
                                         "proc_start": start,
                                         "last_painted_event": max(painted, default=None),
                                         "fixture_sha256": _load_fixture(fixture_path)[1]})
        raise
    finally:
        selector.close()
        os.close(master)
        os.close(read_fd)


def _run(args):
    mode = args.mode
    output = Path(args.evidence_dir)
    output.mkdir(parents=True, exist_ok=True)
    result = {"phase": args.phase, "source_tree": str(args.source_tree),
              "fixture": str(args.fixture), "fixture_sha256": _load_fixture(args.fixture)[1],
              "python": sys.version.split()[0], "platform": sys.platform,
              "term": os.environ.get("TERM"), "locale": os.environ.get("LC_ALL")}
    if mode in ("preview", "all"):
        value = _preview(args.source_tree, args.fixture)
        _json(output / "preview.json", value)
        result["preview"] = str(output / "preview.json")
    if mode in ("scale", "all"):
        values = []
        for count in (3, 30, 100, 300):
            for width in (168, 100, 60):
                for replicate in range(1, 4):
                    cmd = [sys.executable, str(SCRIPT), "--source-tree", str(args.source_tree),
                           "--fixture", str(args.fixture), "--phase", args.phase,
                           "--mode", "_scale-one", "--scale-count", str(count),
                           "--scale-width", str(width), "--registry-count", "100",
                           "--evidence-dir", str(output / "workers")]
                    completed = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                    values.append(json.loads(completed.stdout))
        session_values = values
        registry_values = []
        for registry_count in (10, 100, 1000, 5000):
            for replicate in range(1, 4):
                cmd = [sys.executable, str(SCRIPT), "--source-tree", str(args.source_tree),
                       "--fixture", str(args.fixture), "--phase", args.phase,
                       "--mode", "_scale-one", "--scale-count", "100",
                       "--scale-width", "100", "--registry-count", str(registry_count),
                       "--evidence-dir", str(output / "workers")]
                completed = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                registry_values.append(json.loads(completed.stdout))
        _json(output / "scale.json", {"session_scale": session_values,
                                      "registry_scale": registry_values})
        result["scale"] = str(output / "scale.json")
    if mode in ("retention", "all"):
        cmd = [sys.executable, str(SCRIPT), "--source-tree", str(args.source_tree),
               "--fixture", str(args.fixture), "--phase", args.phase, "--mode", "_retention",
               "--registry-count", "1000", "--evidence-dir", str(output / "worker")]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        value = json.loads(completed.stdout)
        _json(output / "retention.json", value)
        result["retention"] = str(output / "retention.json")
    if mode in ("latency", "all"):
        values = []
        for test_mode, width in (("selection", 168), ("scroll", 60)):
            for replicate in range(1, 4):
                values.append(_pty_run(args.source_tree, args.fixture, output,
                                       width, test_mode, replicate, keys=100,
                                       event_timeout=args.event_timeout))
        _json(output / "latency.json", values)
        result["latency"] = str(output / "latency.json")
    if mode == "explorer-collecting":
        values = [_pty_run(args.source_tree, args.fixture, output, 168,
                           "explorer-collecting", replicate, keys=100,
                           event_timeout=args.event_timeout)
                  for replicate in range(1, 4)]
        _json(output / "explorer-collecting.json", values)
        result["explorer_collecting"] = str(output / "explorer-collecting.json")
        result["first_complete_frame_ms"] = [item["first_complete_frame_ms"] for item in values]
        result["latency_p50_ms"] = [statistics.median(
            sample["end_to_paint_ms"] for sample in item["sample_events"]) for item in values]
        result["latency_p95_ms"] = [sorted(
            sample["end_to_paint_ms"] for sample in item["sample_events"])[94]
            for item in values]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-tree", default=".")
    parser.add_argument("--base-tree")
    parser.add_argument("--legacy-tree")
    parser.add_argument("--fixture")
    parser.add_argument("--phase", choices=("baseline", "after"))
    parser.add_argument("--evidence-dir", default=".")
    parser.add_argument("--runtime-dir")
    parser.add_argument("--mode", choices=("production", "latency", "explorer-collecting", "retention", "preview", "scale", "all",
                                             "_scale-one", "_retention"), required=True)
    parser.add_argument("--replicates", type=int, default=3)
    parser.add_argument("--keys", type=int, default=100)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--cache-state", choices=("absent", "present", "both"), default="both")
    parser.add_argument("--scale-count", type=int, default=3)
    parser.add_argument("--scale-width", type=int, default=168)
    parser.add_argument("--registry-count", type=int, default=100)
    parser.add_argument("--event-timeout", type=float, default=15)
    args = parser.parse_args()
    if args.mode == "production":
        if not args.base_tree or not args.legacy_tree or not args.runtime_dir:
            parser.error("production mode requires --base-tree, --legacy-tree, and --runtime-dir")
        if not args.evidence_dir:
            parser.error("production mode requires --evidence-dir")
        if args.replicates != 3:
            parser.error("production mode requires exactly three alternating replicates")
        if args.keys != 100 or args.duration != 60:
            parser.error("production mode requires exactly 100 keys over 60 seconds")
        if args.event_timeout <= 0:
            parser.error("--event-timeout must be positive")
        for label, path in (("source", args.source_tree), ("base", args.base_tree),
                            ("legacy", args.legacy_tree)):
            if not (Path(path).expanduser() / "tools/fleet" /
                    ("fleet.sh" if label == "legacy" else "fleet2.sh")).is_file():
                parser.error("%s tree is missing its production launcher" % label)
        value = _production_campaign(args)
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if value["status"] == "complete" else 1
    if not args.fixture:
        parser.error("--fixture is required outside production mode")
    if not args.phase:
        parser.error("--phase is required outside production mode")
    fixture, _fixture_hash = _load_fixture(args.fixture)
    if args.mode != "explorer-collecting":
        _isolate_runtime(args.evidence_dir, fixture, args.registry_count)
    if args.mode == "_scale-one":
        value = _scale_one(args.source_tree, args.fixture, args.scale_count,
                           args.scale_width, args.registry_count)
        print(json.dumps(value, ensure_ascii=False))
    elif args.mode == "_retention":
        print(json.dumps(_retention(args.source_tree, args.fixture), ensure_ascii=False))
    else:
        print(json.dumps(_run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
