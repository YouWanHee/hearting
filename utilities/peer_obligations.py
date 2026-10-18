#!/usr/bin/env python3
"""Durable task-scoped peer follow-through and shared pane readiness inputs."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Iterable

from dispatch_attempt_policy import (
    PaneObservation,
    RegisteredWorkObservation,
    completion_readiness,
    decide_attempt,
)

SCHEMA_VERSION = 1
RUNNER_LOCK_NAME = "runner-current.lock"
_RUNNER_CONTROL_SCHEMA = 1
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,160}\Z")
_MAX_RECORD_BYTES = 128 * 1024
_PENDING_STATES = frozenset({"pending", "unknown", "delivery-pending", "cleanup-pending"})
_DUTY_KINDS = frozenset({"watch", "message", "retire", "registered-batch"})
_RUNNERS: list[subprocess.Popen] = []
_DISTRIBUTION_MODULES = {}


class ObligationError(ValueError):
    pass


def _code_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _distribution_module(root: Path):
    path = root / "tools" / "install" / "distribution.py"
    key = str(path.resolve(strict=True))
    module = _DISTRIBUTION_MODULES.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location("_peer_runner_distribution", key)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _DISTRIBUTION_MODULES[key] = module
    return module


def _release_root(root: Path, releases: Path) -> Path | None:
    try:
        resolved = root.resolve(strict=True)
        releases = releases.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if root.is_symlink() or resolved.parent != releases or not resolved.is_dir():
        return None
    required = (
        "RELEASE_VERSION",
        "harness-manifest.json",
        "core/CORE.md",
        "utilities/peer_obligations.py",
        "utilities/peer-steward.py",
    )
    return resolved if all((resolved / relative).is_file() for relative in required) else None


def _service_layout(root: Path):
    """Resolve the exact installer current pointer for a managed release root."""
    try:
        root = root.resolve(strict=True)
        distribution = _distribution_module(root)
        if distribution is None:
            return None
        data_root = Path(distribution.data_root()).expanduser()
        pointer = Path(distribution.current_path()).expanduser()
        if not data_root.is_absolute() or not pointer.is_absolute():
            return None
        data_root = Path(os.path.abspath(os.fspath(data_root)))
        pointer = Path(os.path.abspath(os.fspath(pointer)))
        releases = data_root / "releases"
        if pointer != data_root / "current" or releases.is_symlink():
            return None
        releases_real = releases.resolve(strict=True)
        if root.parent != releases_real or _release_root(root, releases_real) is None:
            return None
        return root, data_root, releases_real, pointer
    except (OSError, RuntimeError, ValueError, AttributeError):
        return None


def _service_environment_matches(root: Path, environ=None) -> bool:
    environ = os.environ if environ is None else environ
    for name in ("AGENT_HOME", "CLAUDE_HOME"):
        raw = environ.get(name)
        if not raw:
            continue
        try:
            if Path(raw).expanduser().resolve(strict=True) != root:
                return False
        except (OSError, RuntimeError, ValueError):
            return False
    return True


def validate_service_binding(service_current: str | Path | None, *, root=None) -> str | None:
    """Keep only this code root's lexical managed-current binding; never infer it."""
    if not service_current:
        return None
    try:
        code_root = Path(root).resolve(strict=True) if root is not None else _code_root()
        layout = _service_layout(code_root)
        if layout is None or not _service_environment_matches(code_root):
            return None
        raw = Path(service_current).expanduser()
        if not raw.is_absolute() or Path(os.path.abspath(os.fspath(raw))) != layout[3]:
            return None
        return str(layout[3])
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def managed_service_binding(source_root: str | Path | None) -> str | None:
    """Issue an internal lifecycle binding only for this packaged managed root."""
    if source_root is None:
        return None
    try:
        source = Path(source_root).expanduser().resolve(strict=True)
        if source != _code_root() or not _service_environment_matches(source):
            return None
        layout = _service_layout(source)
        return str(layout[3]) if layout is not None else None
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def active_service_root(service_current: str | Path | None, *, root=None) -> Path | None:
    """Resolve a validated binding's active release without comparing versions."""
    try:
        code_root = Path(root).resolve(strict=True) if root is not None else _code_root()
        binding = validate_service_binding(service_current, root=code_root)
        if binding is None:
            return None
        layout = _service_layout(code_root)
        if layout is None or layout[3] != Path(binding) or not layout[3].is_symlink():
            return None
        target = layout[3].resolve(strict=True)
        return _release_root(target, layout[2])
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _service_exec_environment(source_root: Path, target_root: Path, environ=None) -> dict:
    child_env = dict(os.environ if environ is None else environ)
    source_root = source_root.resolve(strict=True)
    target_root = target_root.resolve(strict=True)
    layout = _service_layout(source_root)
    releases_root = layout[2] if layout is not None else None
    child_env["AGENT_HOME"] = str(target_root)
    if child_env.get("CLAUDE_HOME"):
        try:
            if Path(child_env["CLAUDE_HOME"]).expanduser().resolve(strict=True) == source_root:
                child_env["CLAUDE_HOME"] = str(target_root)
        except (OSError, RuntimeError, ValueError):
            pass
    pythonpath = child_env.get("PYTHONPATH")
    if pythonpath is not None:
        rewritten = []
        for entry in pythonpath.split(os.pathsep):
            if not entry:
                rewritten.append(entry)
                continue
            try:
                resolved = Path(entry).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                rewritten.append(entry)
                continue
            try:
                relative = resolved.relative_to(source_root)
            except ValueError:
                if releases_root is None:
                    rewritten.append(entry)
                    continue
                try:
                    relative_release = resolved.relative_to(releases_root)
                    if not relative_release.parts:
                        raise ValueError("not a release code path")
                    relative = Path(*relative_release.parts[1:]) if len(relative_release.parts) > 1 else Path()
                except (OSError, RuntimeError, ValueError):
                    rewritten.append(entry)
                else:
                    rewritten.append(str(target_root / relative))
            else:
                rewritten.append(str(target_root / relative))
        child_env["PYTHONPATH"] = os.pathsep.join(rewritten)
    return child_env


def service_script(relative_script: str | Path, service_current: str | Path | None = None,
                   *, root=None, environ=None):
    """Resolve a service script only through an explicitly validated binding.

    Fixed callers receive a script beneath their current code root. Lifecycle
    callers may pass the verified managed-current binding to select the active
    release and rebuild its service environment.
    """
    code_root = Path(root).resolve(strict=True) if root is not None else _code_root().resolve(strict=True)
    relative = Path(relative_script)
    if relative.is_absolute():
        relative = relative.resolve(strict=True).relative_to(code_root)
    if not relative.parts or ".." in relative.parts:
        raise ValueError("service-script-path-invalid")
    binding = validate_service_binding(service_current, root=code_root)
    target_root = active_service_root(binding, root=code_root) if binding else None
    target_root = target_root or code_root
    script = target_root / relative
    if not script.is_file():
        raise FileNotFoundError(script)
    environment = (_service_exec_environment(code_root, target_root, environ=environ)
                   if binding and target_root != code_root else None)
    return script, environment, binding


def _proc_start_identity(pid: int):
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        if not fields or fields[0] in {"Z", "X"}:
            return None
        return fields[19]
    except (OSError, ValueError, TypeError, IndexError):
        return None


def _process_holds_flock(pid: int, lock_path: Path, fd_number: str) -> bool:
    """Require the exact duty lock to be attached to this process's open fd."""
    try:
        info = lock_path.stat()
        device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}"
        inode = str(info.st_ino)
        fdinfo = Path(f"/proc/{pid}/fdinfo/{fd_number}").read_text(encoding="ascii")
        for line in fdinfo.splitlines():
            fields = line.split()
            if (len(fields) >= 7 and fields[0] == "lock:" and fields[2] == "FLOCK"
                    and fields[6] == device + ":" + inode):
                return True
    except (OSError, ValueError, TypeError):
        return False
    return False


def replace_managed_observer(pid: int, pid_start: str | None, lock_path: str | Path,
                             relative_script: str | Path, required_arguments: Iterable[str],
                             service_current: str | Path, *, lock_fd_option: str,
                             root=None, timeout: float = 2.0,
                             exact_arguments: bool = False,
                             argument_variants: Iterable[Iterable[str]] = (),
                             service_current_option: str = "--service-current"):
    """Stop one exact old observer after proving its same-install duty lock.

    Returns ``current`` for an already-current observer, ``replaced`` after
    that exact process exits, and ``False`` when any part of the proof is
    unavailable. No duty data is changed here.
    """
    if pid_start is not None and (
            not isinstance(pid_start, str) or not pid_start.isdecimal()):
        return False
    if (isinstance(pid, bool) or not isinstance(pid, (int, str))
            or (isinstance(pid, str) and not pid.isdecimal())):
        return False
    try:
        code_root = Path(root).resolve(strict=True) if root is not None else _code_root().resolve(strict=True)
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            return False
        pid = int(pid)
        if pid <= 0:
            return False
        expected_start = pid_start
        lock = Path(lock_path).resolve(strict=True)
        relative = Path(relative_script)
        expected_args = list(map(str, required_arguments))
        expected_variants = [expected_args]
        if exact_arguments:
            expected_variants.extend(list(map(str, variant)) for variant in argument_variants)
        pidfd = os.pidfd_open(pid)
        try:
            def inspect():
                nonlocal expected_start
                # Resolve the managed target for every inspection. Activation
                # may change the current release between the initial check and
                # the pidfd signal boundary.
                binding = validate_service_binding(service_current, root=code_root)
                target_root = active_service_root(binding, root=code_root) if binding else None
                if target_root is None:
                    return None
                observed_start = _proc_start_identity(pid)
                if not observed_start:
                    return None
                if expected_start is None:
                    # PID-only historical rows may bind only to the process
                    # behind this newly opened pidfd. Subsequent inspections
                    # must retain that exact birth identity.
                    expected_start = observed_start
                elif observed_start != expected_start:
                    return None
                namespace = os.stat(f"/proc/{pid}/ns/pid").st_ino
                if namespace != os.stat("/proc/self/ns/pid").st_ino:
                    return None
                proc = Path("/proc") / str(pid)
                argv = proc.joinpath("cmdline").read_bytes().decode().rstrip("\0").split("\0")
                if len(argv) < 3:
                    return None
                script = Path(argv[1]).resolve(strict=True)
                source_root = script.parent.parent
                if (script != source_root / relative or _release_root(
                        source_root, _service_layout(code_root)[2]) is None):
                    return None
                source_layout = _service_layout(source_root)
                target_layout = _service_layout(code_root)
                if (source_layout is None or target_layout is None
                        or source_layout[1] != target_layout[1]
                        or source_layout[2] != target_layout[2]
                        or source_layout[3] != target_layout[3]):
                    return None
                lock_fd_number = None
                if exact_arguments:
                    duty_arguments = []
                    control_values = {}
                    control_options = {lock_fd_option, service_current_option}
                    index = 2
                    while index < len(argv):
                        option = argv[index]
                        if option in control_options:
                            if option in control_values or index + 1 >= len(argv):
                                return None
                            value = argv[index + 1]
                            if not value or value.startswith("--"):
                                return None
                            control_values[option] = value
                            index += 2
                            continue
                        duty_arguments.append(option)
                        index += 1
                    if duty_arguments not in expected_variants:
                        return None
                    lock_fd_number = control_values.get(lock_fd_option)
                    if lock_fd_number is None or not lock_fd_number.isdecimal():
                        return None
                    observed_binding = control_values.get(service_current_option)
                    if observed_binding is not None and (
                            validate_service_binding(observed_binding, root=code_root) != binding):
                        return None
                elif not any(argv[i:i + len(expected_args)] == expected_args
                             for i in range(2, len(argv) - len(expected_args) + 1)):
                    return None
                try:
                    env = {}
                    for item in proc.joinpath("environ").read_bytes().split(b"\0"):
                        if b"=" in item:
                            key, value = item.split(b"=", 1)
                            env[key.decode()] = value.decode(errors="replace")
                    # The launcher can inherit a different release path from
                    # the script's source root. Accept that provenance only
                    # when it resolves to this same managed installation.
                    for name in ("AGENT_HOME", "CLAUDE_HOME"):
                        if name not in env:
                            continue
                        raw = env.get(name)
                        if not raw:
                            return None
                        env_root = Path(raw).expanduser().resolve(strict=True)
                        env_layout = _service_layout(env_root)
                        if env_layout is None or env_layout[1:] != source_layout[1:]:
                            return None
                    if "HARNESS_DATA_ROOT" in env:
                        raw = env["HARNESS_DATA_ROOT"]
                        if not raw:
                            return None
                        observed_data_root = Path(raw).expanduser()
                    elif "XDG_DATA_HOME" in env:
                        raw = env["XDG_DATA_HOME"]
                        if not raw:
                            return None
                        observed_data_root = Path(raw).expanduser() / "hearting"
                    else:
                        home = env.get("HOME")
                        if not home:
                            return None
                        observed_data_root = (
                            Path(home).expanduser() / ".local" / "share" / "hearting")
                    if (not observed_data_root.is_absolute()
                            or Path(os.path.abspath(os.fspath(observed_data_root)))
                            != source_layout[1]):
                        return None
                except (OSError, RuntimeError, ValueError):
                    return None
                try:
                    if lock_fd_number is None:
                        fd_index = argv.index(lock_fd_option)
                        lock_fd_number = argv[fd_index + 1]
                    held_path = Path(os.path.realpath(proc / "fd" / lock_fd_number))
                except (ValueError, IndexError, OSError):
                    return None
                if held_path != lock or not _process_holds_flock(pid, lock, lock_fd_number):
                    return None
                if source_root == target_root:
                    return ("current", None)
                return ("legacy", None)

            status = inspect()
            if status is None:
                return False
            if status[0] == "current":
                return "current"
            # Recheck all evidence immediately before signaling the pidfd-bound
            # process. A stale PID or changed duty lock cannot authorize this.
            status = inspect()
            if status is None:
                return False
            if status[0] == "current":
                return "current"
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            deadline = time.monotonic() + max(0.0, timeout)
            while time.monotonic() < deadline:
                if _proc_start_identity(pid) != expected_start:
                    return "replaced"
                time.sleep(0.02)
            return False
        finally:
            os.close(pidfd)
    except (OSError, RuntimeError, ValueError, TypeError, IndexError):
        return False


def exec_service_runner(lock_fd: int, state_root: str | Path, service_current: str | Path,
                        *, herdr_session: str | None = None) -> bool:
    """Replace this process with the same-install runner while retaining its flock."""
    was_inheritable = None
    try:
        source_root = _code_root().resolve(strict=True)
        binding = validate_service_binding(service_current, root=source_root)
        if binding is None:
            return False
        target_root = active_service_root(binding, root=source_root)
        if target_root is None or target_root == source_root:
            return False
        runner = target_root / "utilities" / "peer-steward.py"
        argv = [sys.executable, str(runner)]
        if herdr_session:
            argv.extend(("--herdr-session", herdr_session))
        argv.extend(("__obligation-runner", "--lock-fd", str(lock_fd),
                     "--state-root", str(Path(state_root).resolve(strict=True)),
                     "--service-current", binding))
        # Selection, strict root resolution and environment preparation are all
        # pre-exec work. Any OSError here is the same unsuccessful handoff as an
        # execve error: this process keeps its lock and retries on its next pass.
        child_env = _service_exec_environment(source_root, target_root)
        was_inheritable = os.get_inheritable(lock_fd)
        os.set_inheritable(lock_fd, True)
        os.execve(sys.executable, argv, child_env)
    except OSError:
        if was_inheritable is not None:
            try:
                os.set_inheritable(lock_fd, was_inheritable)
            except OSError:
                pass
        return False
    return True


def _runner_control_payload(service_current: str | Path, *, root=None) -> bytes | None:
    """Serialize only provenance freshly verified for this installed code root."""
    try:
        code_root = Path(root).resolve(strict=True) if root is not None else _code_root().resolve(strict=True)
        binding = validate_service_binding(service_current, root=code_root)
        layout = _service_layout(code_root)
        if binding is None or layout is None or binding != str(layout[3]):
            return None
        return json.dumps({"schema": _RUNNER_CONTROL_SCHEMA,
                           "install_data_root": str(layout[1]),
                           "service_current": binding},
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _remembered_service_binding(lock_fd: int, *, root=None) -> str | None:
    """Read a lock payload only while holding its lock; malformed data is fixed."""
    try:
        code_root = Path(root).resolve(strict=True) if root is not None else _code_root().resolve(strict=True)
        raw = os.pread(lock_fd, 4097, 0)
        if not raw or len(raw) > 4096:
            return None
        value = json.loads(raw.decode("utf-8"))
        if (not isinstance(value, dict) or set(value) !=
                {"schema", "install_data_root", "service_current"}
                or type(value.get("schema")) is not int
                or value["schema"] != _RUNNER_CONTROL_SCHEMA
                or not isinstance(value.get("install_data_root"), str)
                or not isinstance(value.get("service_current"), str)):
            return None
        layout = _service_layout(code_root)
        binding = validate_service_binding(value["service_current"], root=code_root)
        if (layout is None or binding is None
                or value["install_data_root"] != str(layout[1])
                or binding != str(layout[3])):
            return None
        return binding
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError):
        return None


def _write_runner_control(lock_fd: int, payload: bytes) -> None:
    """Update the existing locked inode; never replace the runner lock file."""
    os.ftruncate(lock_fd, 0)
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        offset += os.pwrite(lock_fd, view[offset:], offset)
    os.fsync(lock_fd)


def peer_state_root() -> Path:
    # Lazy import keeps the shared store independent of peer-message's CLI load.
    import importlib.util

    path = Path(__file__).with_name("peer-message.py")
    spec = importlib.util.spec_from_file_location("_peer_obligations_message", path)
    if spec is None or spec.loader is None:
        raise ObligationError("peer-state-root-unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return Path(module.peer_state_root()).resolve()


def obligations_root(root: str | Path | None = None) -> Path:
    base = Path(root).expanduser().resolve() if root is not None else peer_state_root()
    if base.is_symlink():
        raise ObligationError("peer-state-root-symlink")
    directory = base / "peer-steward" / "obligations"
    if directory.exists() and directory.is_symlink():
        raise ObligationError("peer-obligations-root-symlink")
    return directory


def _safe_id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ObligationError("peer-obligation-id-invalid")
    return value


def _read_record(path: Path) -> dict | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(_MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            raise ObligationError("peer-obligation-record-oversized")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, ObligationError):
            raise
        raise ObligationError("peer-obligation-record-invalid") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or not isinstance(value.get("id"), str) or not isinstance(value.get("intent"), dict)):
        raise ObligationError("peer-obligation-record-invalid")
    return value


def _write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ObligationError("peer-obligation-path-symlink")
    fd, temporary_name = tempfile.mkstemp(prefix=".obligation-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def retain_registered_completion(jobs, attempt_id, session_id, carrier, *, holder=None):
    """Reconnect the existing exact-attempt courier; never start the work again."""
    from dispatch_completion_join import JoinContractError, current_attempt_row
    from dispatch_seat_handover import effective_parent
    from harness_capabilities import HARNESSES, parent_completion
    try:
        row = current_attempt_row(Path(jobs), attempt_id)
    except JoinContractError:
        return
    harness = next((h for h in HARNESSES if parent_completion(h)["carrier"] == carrier), None)
    if (not harness or row is None or row.status not in {"open", "running", "done"}
            or row.metadata.get("parent_completion_delivery") != carrier
            or effective_parent(row.metadata, Path(jobs)) != session_id):
        return
    identity = {"jobs": str(Path(jobs).resolve()), "attempt_id": attempt_id,
                "session_id": session_id, "harness": harness,
                "server": os.environ.get("AGENT_HERDR_SESSION") or "default"}
    store = ObligationStore(Path(jobs).parent)
    duty = store.create(stable_duty_id("registered-batch", identity),
                        "registered-batch", identity, {"carrier": carrier})
    if duty.get("state") not in {"complete", "cancelled"}:
        if holder:
            store.update(duty["id"], observation={"holder": list(holder)})
        ensure_normal_runner(Path(jobs).parent)


def _registered_delivery_bindings(message, roots=None, *, received_only=False, read_context=None):
    if (received_only and (message.get("state") != "received"
            or message.get("receipt") not in {"exact-peer-ref", "exact-native-history"})):
        return
    if message.get("state") not in {"pending", "queued", "unverified", "received"}:
        return
    binding = message.get("dispatch_notice") or {}
    expected_binding = {"duty_id", "jobs", "attempt_id", "carrier", "recipient_sid",
                        "storage_recipient", "delivery_id", "claim_owner"}
    if (not isinstance(binding, dict) or set(binding) != expected_binding
            or binding.get("duty_id") not in (message.get("refs") or [])
            or not isinstance(message.get("ref"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", message["ref"])):
        return
    from dispatch_completion_join import current_attempt_row
    import dispatch_pending_delivery as pending
    recipient = message.get("to") or {}
    jobs = Path(binding["jobs"])
    if not jobs.is_absolute() or jobs.is_symlink() or not jobs.is_file():
        return
    try:
        root = jobs.resolve(strict=True).parent
    except (OSError, RuntimeError, ValueError):
        return
    if roots is not None and root not in {Path(r).resolve() for r in roots}:
        return
    store = ObligationStore(root)
    duty = store.get(binding["duty_id"])
    intent = (duty or {}).get("intent") or {}
    identity = intent.get("identity") or {}
    if (intent.get("kind") != "registered-batch" or identity.get("jobs") != str(jobs)
            or identity.get("attempt_id") != binding.get("attempt_id")
            or identity.get("harness") != recipient.get("harness")
            or binding.get("recipient_sid") != recipient.get("session_id")
            or intent.get("carrier") != binding.get("carrier")
            or binding.get("claim_owner") != "retained-rewake:" + binding["duty_id"]):
        return
    try:
        row = (current_attempt_row(jobs, binding["attempt_id"]) if read_context is None else
               current_attempt_row(jobs, binding["attempt_id"], read_context=read_context))
        storage, delivery_id = binding["storage_recipient"], binding["delivery_id"]
        record = pending.read(root, storage, delivery_id)
        if (row is None or not record or record.get("delivery_id") != delivery_id
                or row.attempt_id not in record.get("attempt_ids", [])
                or record.get("recipient_kind") != binding.get("carrier")
                or record.get("recipient_digest") != pending.recipient_digest(storage)
                or row.metadata.get("parent_sid") != storage
                or row.metadata.get("parent_completion_delivery") != binding.get("carrier")):
            return
        from dispatch_seat_handover import effective_parent
        parent = (effective_parent(row.metadata, jobs) if read_context is None else
                  effective_parent(row.metadata, jobs, read_context=read_context))
        if parent != binding["recipient_sid"]:
            return
        if (row.metadata.get("delivery_intent") == "1"
                and row.metadata.get("delivery_id") == delivery_id and (
                not row.metadata.get("delivery_receipt_digest")
                or record.get("receipt_digest") != row.metadata.get("delivery_receipt_digest")
                or not row.metadata.get("delivery_row_revision")
                or (record.get("row_revisions") or {}).get(row.attempt_id)
                    != row.metadata.get("delivery_row_revision"))):
            return
        identity = pending.received_ack_identity(record, storage, delivery_id)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return
    yield store, duty, row, record, root, storage, delivery_id


def registered_delivery_settled(message, *, roots=None, read_context=None):
    """A consumed exact notice makes its deferred peer payload unnecessary."""
    return any(record["state"] in {"acked", "rejected", "expired"}
               for _store, _duty, _row, record, _root, _storage, _delivery_id
               in _registered_delivery_bindings(message, roots, read_context=read_context))


def acknowledge_registered_delivery(message, *, roots=None):
    """Settle only a delivery proven by this exact recipient-side peer receipt.

    Return true when the receipt is bound to the current delivery, even if the
    queue write needs a later retry. Callers use that fact to avoid preparing a
    duplicate transfer while preserving the open delivery for recovery.
    """
    if message.get("state") != "received":
        return False
    import dispatch_pending_delivery as pending
    matched = False
    for store, duty, row, record, root, storage, delivery_id in _registered_delivery_bindings(
            message, roots, received_only=True):
        matched = True
        settled = record
        if record["state"] in pending.OPEN_STATES:
            try:
                identity = pending.received_ack_identity(record, storage, delivery_id)
                settled = pending.ack_received(root, storage, delivery_id,
                    expected_identity=identity, acked_by="peer-received:" + message["ref"])
            except (OSError, ValueError, RuntimeError):
                continue
        if settled["state"] != "acked":
            continue
        terminal = row.status == "done" and row.metadata.get("delivery_id") == delivery_id
        if duty.get("state") not in {"complete", "cancelled"}:
            try:
                store.update(duty["id"], state="complete" if terminal else "pending",
                             delivery="acknowledged", cleanup="complete" if terminal else "pending")
            except (OSError, ValueError, RuntimeError):
                pass
    return matched


class ObligationStore:
    """Atomic intent records; immutable intent is separate from mutable progress."""

    def __init__(self, root: str | Path | None = None):
        self.root = obligations_root(root)
        self._list_cache: dict[str, tuple[tuple[int, int, int, int, int], dict | None]] = {}
        self._discovery_metrics = {"stat_calls": 0, "json_reads": 0, "json_parses": 0}
        self._active_list_context = None

    def begin_read_pass(self) -> None:
        """Share one discovery snapshot among lists made during a runner pass."""
        self._active_list_context = {"paths": None, "records": {}}

    def end_read_pass(self) -> None:
        """Drop the pass snapshot so the next list observes external changes."""
        self._active_list_context = None

    def _record_path(self, duty_id: str) -> Path:
        return self.root / (_safe_id(duty_id) + ".json")

    def _lock(self, duty_id: str):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / (_safe_id(duty_id) + ".lock")
        if path.is_symlink():
            raise ObligationError("peer-obligation-lock-symlink")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def get(self, duty_id: str) -> dict | None:
        return _read_record(self._record_path(duty_id))

    @staticmethod
    def _list_signature(path: Path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ObligationError("peer-obligation-record-invalid")
        if not stat.S_ISREG(info.st_mode):
            return None
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)

    def _read_list_record(self, path: Path):
        key = str(path)
        for _attempt in range(3):
            try:
                before = self._list_signature(path)
            except FileNotFoundError:
                self._list_cache.pop(key, None)
                return None
            self._discovery_metrics["stat_calls"] += 1
            if before is None:
                self._list_cache.pop(key, None)
                return None
            cached = self._list_cache.get(key)
            if cached is not None and cached[0] == before:
                return cached[1]

            self._list_cache.pop(key, None)
            self._discovery_metrics["json_reads"] += 1
            self._discovery_metrics["json_parses"] += 1
            record = _read_record(path)
            try:
                after = self._list_signature(path)
            except FileNotFoundError:
                self._list_cache.pop(key, None)
                return None
            self._discovery_metrics["stat_calls"] += 1
            if before == after:
                self._list_cache[key] = (after, copy.deepcopy(record))
                return record
        self._list_cache.pop(key, None)
        raise ObligationError("peer-obligation-list-unstable")

    def discovery_metrics(self) -> dict[str, int]:
        return dict(self._discovery_metrics)

    def reset_discovery_metrics(self) -> None:
        for key in self._discovery_metrics:
            self._discovery_metrics[key] = 0

    def list(self, *, states: Iterable[str] = _PENDING_STATES,
             kinds: Iterable[str] | None = None) -> list[dict]:
        if not self.root.is_dir():
            self._list_cache.clear()
            return []
        wanted = set(states)
        wanted_kinds = None if kinds is None else set(kinds)
        output = []
        context = self._active_list_context
        if context is None:
            context = {"paths": None, "records": {}}
        if context["paths"] is None:
            paths = sorted(self.root.glob("*.json"))
            context["paths"] = paths
            present = {str(path) for path in paths}
            for stale in set(self._list_cache).difference(present):
                self._list_cache.pop(stale, None)
        paths = context["paths"]
        for path in paths:
            key = str(path)
            if key not in context["records"]:
                context["records"][key] = self._read_list_record(path)
            record = context["records"][key]
            if (record is not None and record.get("state") in wanted
                    and (wanted_kinds is None
                         or (record.get("intent") or {}).get("kind") in wanted_kinds)):
                output.append(copy.deepcopy(record))
        return output

    def _invalidate_list_record(self, duty_id: str) -> None:
        path = self._record_path(duty_id)
        key = str(path)
        self._list_cache.pop(key, None)
        context = self._active_list_context
        if context is None:
            return
        context["records"].pop(key, None)
        if context["paths"] is not None and path.exists() and key not in {
                str(candidate) for candidate in context["paths"]}:
            context["paths"] = sorted([*context["paths"], path])

    def create(self, duty_id: str, kind: str, identity: dict, intent: dict) -> dict:
        duty_id = _safe_id(duty_id)
        if kind not in _DUTY_KINDS or not isinstance(identity, dict) or not isinstance(intent, dict):
            raise ObligationError("peer-obligation-intent-invalid")
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            current = _read_record(path)
            immutable = {"kind": kind, "identity": identity, **intent}
            if current is not None:
                if current.get("intent") != immutable:
                    raise ObligationError("peer-obligation-intent-conflict")
                return current
            record = {"schema_version": SCHEMA_VERSION, "id": duty_id,
                      "intent": immutable, "state": "pending", "revision": 1,
                      "accepted_at": time.time(), "updated_at": time.time(),
                      "observation": None, "result": None, "delivery": "pending",
                      "cleanup": "pending"}
            _write_atomic(path, record)
            return record
        finally:
            os.close(fd)
            self._invalidate_list_record(duty_id)

    def update(self, duty_id: str, *, state: str | None = None,
               observation: dict | None = None, result: str | None = None,
               delivery: str | None = None, cleanup: str | None = None,
               observer_error: str | None = None,
               expected_phases: set[str] | None = None,
               skip_unchanged: bool = False) -> dict:
        if state is not None and state not in _PENDING_STATES | {"complete", "cancelled"}:
            raise ObligationError("peer-obligation-state-invalid")
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            record = _read_record(path)
            if record is None:
                raise ObligationError("peer-obligation-missing")
            if expected_phases is not None and (
                    record.get("state") in {"complete", "cancelled"}
                    or (record.get("observation") or {}).get("phase", "waiting")
                    not in expected_phases):
                return record
            if observer_error is not None:
                # Error observation cannot undo a claimed side effect or reopen
                # a fulfilled duty. Merge the checkpoint under the same lock.
                if record.get("state") in {"complete", "cancelled"}:
                    return record
                state = "unknown"
                observation = {**(record.get("observation") or {}),
                               "reason": observer_error}
            if result is not None and record.get("result") not in {None, result}:
                raise ObligationError("peer-obligation-result-conflict")
            changed = False
            if state is not None and record.get("state") != state:
                record["state"] = state
                changed = True
            if observation is not None and record.get("observation") != observation:
                record["observation"] = observation
                changed = True
            if result is not None and record.get("result") != result:
                record["result"] = result
                changed = True
            if delivery is not None and record.get("delivery") != delivery:
                record["delivery"] = delivery
                changed = True
            if cleanup is not None and record.get("cleanup") != cleanup:
                record["cleanup"] = cleanup
                changed = True
            if skip_unchanged and not changed:
                return record
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = time.time()
            _write_atomic(path, record)
            return record
        finally:
            os.close(fd)
            self._invalidate_list_record(duty_id)

    def claim_phase(self, duty_id: str, expected: set[str], new_phase: str,
                    *, state: str = "pending", extra: dict | None = None) -> dict | None:
        """Publish one side-effect phase exactly once under the duty lock."""
        if state not in _PENDING_STATES | {"complete", "cancelled"}:
            raise ObligationError("peer-obligation-state-invalid")
        path = self._record_path(duty_id)
        fd = self._lock(duty_id)
        try:
            record = _read_record(path)
            if record is None:
                return None
            if record.get("state") in {"complete", "cancelled"}:
                return None
            observation = record.get("observation") or {}
            if observation.get("phase", "waiting") not in expected:
                return None
            observation = {**observation, **(extra or {}), "phase": new_phase}
            record["observation"] = observation
            record["state"] = state
            record["revision"] = int(record.get("revision", 0)) + 1
            record["updated_at"] = time.time()
            _write_atomic(path, record)
            return record
        finally:
            os.close(fd)
            self._invalidate_list_record(duty_id)


def stable_duty_id(kind: str, identity: dict, discriminator: str = "") -> str:
    if kind not in _DUTY_KINDS:
        raise ObligationError("peer-obligation-kind-invalid")
    raw = json.dumps([kind, identity, discriminator], sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return kind + "-" + hashlib.sha256(raw).hexdigest()[:32]


def _handoff_legacy_runner(store: ObligationStore, lock_path: Path, *,
                          reconciliation_required=False, service_current=None):
    """Replace only an exact observer whose old code cannot process this duty.

    Accepted work stays in the existing store; the same flock fences its next
    observer. A pidfd prevents a recycled PID from ever receiving the signal.
    """
    has_batch = any(d.get("intent", {}).get("kind") == "registered-batch" for d in store.list())
    if not has_batch and not reconciliation_required:
        return False
    if not service_current:
        return False
    state_root = str(store.root.parents[1])
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        pidfd = None
        try:
            pidfd = os.pidfd_open(int(proc.name))
            argv = (proc / "cmdline").read_bytes().decode().rstrip("\0").split("\0")
            if (len(argv) < 3 or Path(argv[1]).name != "peer-steward.py"
                    or argv[2] != "__obligation-runner"
                    or argv[argv.index("--state-root") + 1] != state_root):
                continue
            lock_fd = argv[argv.index("--lock-fd") + 1]
            if (proc / "fd" / lock_fd).resolve() != lock_path.resolve():
                continue
            result = replace_managed_observer(
                int(proc.name), _proc_start_identity(int(proc.name)) or "", lock_path,
                "utilities/peer-steward.py",
                ("__obligation-runner", "--lock-fd", lock_fd,
                 "--state-root", state_root),
                service_current, lock_fd_option="--lock-fd",
            )
            if result in {"current", "replaced"}:
                return result
        except (OSError, ValueError, IndexError, UnicodeError):
            continue
        finally:
            if pidfd is not None:
                os.close(pidfd)
    return False


def _ensure_runner(root, *, service_current=None, use_remembered=False,
                   remember_binding=False, reconciliation_required=False):
    store = ObligationStore(root)
    duties = store.list()
    lock_path = store.root / RUNNER_LOCK_NAME
    if not duties and not remember_binding and not reconciliation_required:
        if not use_remembered or not lock_path.exists():
            return False
    store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise ObligationError("peer-obligation-runner-lock-symlink")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handoff = _handoff_legacy_runner(
                store, lock_path, reconciliation_required=reconciliation_required,
                service_current=service_current)
            if handoff == "current":
                return True
            if handoff != "replaced":
                return not reconciliation_required
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.02)
        code_root = _code_root()
        binding = validate_service_binding(service_current, root=code_root)
        if use_remembered:
            binding = _remembered_service_binding(fd, root=code_root)
        elif remember_binding and binding:
            payload = _runner_control_payload(binding, root=code_root)
            if payload is None:
                binding = None
            else:
                _write_runner_control(fd, payload)
        if not duties and not reconciliation_required:
            return False
        os.set_inheritable(fd, True)
        active_root = active_service_root(binding, root=code_root) if binding else None
        runner_root = active_root or code_root
        runner = runner_root / "utilities" / "peer-steward.py"
        state_root = Path(root).resolve() if root else peer_state_root()
        child_env = (_service_exec_environment(code_root, runner_root) if binding else None)
        argv = [sys.executable, str(runner), "__obligation-runner", "--lock-fd", str(fd),
                "--state-root", str(state_root)]
        if binding:
            argv.extend(("--service-current", binding))
        _RUNNERS[:] = [process for process in _RUNNERS if process.poll() is None]
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, pass_fds=(fd,), start_new_session=True, env=child_env,
        )
        _RUNNERS.append(process)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        os.close(fd)


def ensure_runner(root: str | Path | None = None, *,
                  service_current: str | Path | None = None,
                  reconciliation_required=False) -> bool:
    """Start a fixed-root runner unless an explicit internal binding is supplied."""
    return _ensure_runner(root, service_current=service_current,
                          reconciliation_required=reconciliation_required)


def ensure_normal_runner(root: str | Path | None = None, *, lifecycle_root=None,
                         reconciliation_required=False) -> bool:
    """Ensure a producer runner using only verified lifecycle provenance.

    Lifecycle callbacks may establish the binding while holding the existing
    runner lock, including when no duty currently needs a process. Ordinary
    producers reuse only a validated binding already in that lock's payload.
    """
    if lifecycle_root is not None:
        binding = managed_service_binding(lifecycle_root)
        return _ensure_runner(root, service_current=binding,
                              remember_binding=binding is not None,
                              reconciliation_required=reconciliation_required)
    return _ensure_runner(root, use_remembered=True,
                          reconciliation_required=reconciliation_required)


@contextmanager
def legacy_runner_lock(store: ObligationStore):
    """Serialize retire side effects with a runner from before input separation."""
    path = store.root / "runner.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def pane_readiness(
    *,
    server: str,
    pane: str,
    harness: str,
    session_id: str,
    pid_birth: str,
    identity_verified: bool,
    native_turn: str,
    form_state: str = "unknown",
    draft_state: str = "unknown",
    bound_work: tuple[RegisteredWorkObservation, ...] = (),
    bindings_state: str = "observed",
    provenance: tuple[tuple[str, str], ...] = (),
    purpose: str = "completion",
):
    observation = PaneObservation(
        server=server, pane=pane, harness=harness, session_id=session_id,
        pid_birth=pid_birth, identity_verified=identity_verified,
        native_turn=native_turn, form_state=form_state, draft_state=draft_state,
        bound_work=bound_work, bindings_state=bindings_state, provenance=provenance,
    )
    return completion_readiness(observation, purpose=purpose)


def execution_attempts_for_processes(processes: Iterable[dict]) -> tuple[str, ...]:
    """Read only the exact foreground processes' existing dispatch tags."""
    from dispatch_contract import ATTEMPT_DESCENDANT_ENV, _runtime_ancestry_proc_stat

    attempts = set()
    key = ATTEMPT_DESCENDANT_ENV.encode() + b"="
    for process in processes:
        try:
            pid = int(process["pid"])
            before = _runtime_ancestry_proc_stat(pid)
            if before is None:
                continue
            try:
                raw = (Path("/proc") / str(pid) / "environ").read_bytes()
            except OSError as exc:
                if before == _runtime_ancestry_proc_stat(pid):
                    raise ObligationError("pane-execution-unavailable") from exc
                continue
            if before != _runtime_ancestry_proc_stat(pid):
                continue
            for item in raw.split(b"\0"):
                if item.startswith(key):
                    attempt = item[len(key):].decode("utf-8")
                    if _SAFE_ID.fullmatch(attempt):
                        attempts.add(attempt)
        except ObligationError:
            raise
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            continue
    return tuple(sorted(attempts))


def bound_work_for_pane(pane: str, harness: str, session_id: str, *,
                        jobs: str | Path | None = None,
                        execution_attempt_ids: tuple[str, ...] = (),
                        include_parent: bool = True,
                        ) -> tuple[tuple[RegisteredWorkObservation, ...], str]:
    """Read exact executor, registered-parent or handed-over attempt bindings.

    Input readiness considers executor bindings only, so it passes
    ``include_parent=False`` and skips observing every parented attempt.
    """
    from dispatch_contract import (
        observed_attempt_liveness,
        parse_registry_metadata,
        resolve_agent_home,
        resolve_dispatch_state_root,
    )
    from dispatch_terminal_commit import owner_completion_state
    from route_authority import owns
    from dispatch_seat_handover import effective_parent_harness

    if jobs is None:
        state_root = resolve_dispatch_state_root(resolve_agent_home())
        jobs_path = state_root / "jobs.log"
    else:
        jobs_path = Path(jobs).expanduser().resolve()
        state_root = jobs_path.parent
    try:
        lines = jobs_path.read_text(encoding="utf-8").splitlines() if jobs_path.exists() else []
    except OSError:
        return (), "unknown"
    result: list[RegisteredWorkObservation] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            return (), "unknown"
        status = fields[1]
        metadata = parse_registry_metadata(fields[5])
        executor = (metadata.get("attempt_id") in execution_attempt_ids
                    and metadata.get("harness") == harness)
        parent = (include_parent and owns(metadata, session_id, jobs_path)
                  and (metadata.get("parent_harness") if metadata.get("parent_sid") == session_id
                       else effective_parent_harness(metadata, jobs_path)) in {None, "", harness})
        if not executor and not parent:
            continue
        attempt_id = metadata.get("attempt_id", "")
        if not attempt_id:
            return (), "unknown"
        live = observed_attempt_liveness(status, metadata)
        decision = decide_attempt(
            status, metadata, process_state=live.process_state,
            process_reason=live.process_reason,
            terminal_observed=live.state == "reconcile-needed",
        )
        try:
            closure = owner_completion_state(jobs_path, status, metadata).state
        except Exception:
            closure = "unknown"
        closure_state = ("settled" if closure in {"not-applicable", "complete"}
                         else "unknown" if closure == "unknown" else "pending")
        result.append(RegisteredWorkObservation(
            attempt_id=attempt_id, decision=decision,
            process_state=live.process_state,
            evidence_state=metadata.get("receipt_state", metadata.get("note", "unclassified")),
            owner_completion_state=closure_state,
            cleanup_state=("pending" if metadata.get("cleanup_pending") == "1" else "settled"),
            pane_relation="executor" if executor else "parent",
            provenance=(("jobs", str(jobs_path)), ("registry_status", status),
                        ("registry_state_root", str(state_root))),
        ))
    return tuple(result), "observed"
