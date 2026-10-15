"""Conservative launcher-local activity sampling for foreground workers."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import time
from typing import Mapping


@dataclass(frozen=True)
class ActivityPolicy:
    window_seconds: float | None
    source: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ForegroundActivityContext:
    window_seconds: float
    harness: str
    log_path: Path
    metadata: Mapping[str, str]
    transcript_roots: tuple[Path, ...] = ()
    jobs: Path | None = None
    runtime_executable: str = ""


@dataclass(frozen=True)
class ActivityObservation:
    active: bool
    uncertain: bool
    reasons: tuple[str, ...]


def resolve_activity_policy(worker_type: str | None) -> ActivityPolicy:
    """Resolve the one defaults-backed window; owners never read configuration."""
    if worker_type == "owner":
        return ActivityPolicy(None, "owner-exempt")
    here = Path(__file__).resolve().parent
    try:
        spec = importlib.util.spec_from_file_location("hearting_dispatch_defaults", here / "dispatch-defaults.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("defaults-loader-unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config, warnings = module.load_validated(module.default_config_path(), module.default_topology_path())
        minutes = module.query_no_progress_minutes(config)
        normal_warnings = tuple(w for w in warnings if "headless.no_progress_minutes" in w)
        window = minutes * 60.0
        return ActivityPolicy(None if window == 0 else window, "dispatch-defaults", normal_warnings)
    except Exception as exc:
        return ActivityPolicy(1800.0, "fallback", (f"dispatch-defaults activity policy unavailable: {type(exc).__name__}: {exc}; using 30 minutes",))


def make_activity_context(policy: ActivityPolicy, *, worker_type: str, harness: str,
                          log_path: Path, jobs: Path, metadata: Mapping[str, str],
                          runtime_executable: str,
                          transcript_roots: tuple[Path, ...] = ()) -> ForegroundActivityContext | None:
    if worker_type == "owner" or policy.window_seconds is None:
        return None
    return ForegroundActivityContext(
        window_seconds=policy.window_seconds, harness=harness,
        log_path=Path(log_path), metadata=dict(metadata), jobs=Path(jobs),
        transcript_roots=transcript_roots,
        runtime_executable=runtime_executable,
    )


def _stat(path: Path):
    raw = path.read_text(encoding="utf-8")
    end = raw.rfind(")")
    if end < 0:
        raise ValueError("malformed proc stat")
    tail = raw[end + 2 :].split()
    if len(tail) < 20:
        raise ValueError("short proc stat")
    return {
        "state": tail[0], "ppid": int(tail[1]), "pgid": int(tail[2]),
        "cpu": sum(int(value) for value in tail[11:15]), "start": tail[19],
    }


def _file_state(path: Path):
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


class ForegroundActivityMonitor:
    """Sample the worker's exact process set and bounded attempt-owned files."""

    def __init__(self, proc_pid: int, leader_start: str, context: ForegroundActivityContext):
        self.proc_pid = proc_pid
        self.leader_start = str(leader_start)
        self.context = context
        self._process_baseline = None
        self._file_baseline = None
        self._lease_baseline = False

    def _files(self):
        states = {}
        paths = [self.context.log_path]
        for root in self.context.transcript_roots:
            try:
                if root.is_file():
                    paths.append(root)
                elif root.is_dir():
                    paths.extend(p for p in root.rglob("*") if p.is_file())
            except OSError:
                raise
        for path in paths:
            try:
                states[str(path)] = _file_state(path)
            except FileNotFoundError:
                continue
        return states

    def _lease_active(self):
        if self.context.jobs is None:
            return False
        attempt = self.context.metadata.get("attempt_id", "")
        if not attempt:
            return False
        lease = self.context.jobs.parent / "verification-leases" / f"{attempt}.json"
        try:
            data = json.loads(lease.read_text(encoding="utf-8"))
            pid = int(data["pid"])
            start = str(data["pid_start"])
            pgid = int(data["pgid"])
            deadline = float(data["deadline"])
            argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            digest = hashlib.sha256(b"\0".join(argv)).hexdigest()
            return (
                data.get("attempt_id") == attempt
                and data.get("schema_version") == 1
                and data.get("route_id") == self.context.metadata.get("route_id")
                and data.get("route_node") == self.context.metadata.get("route_node")
                and math.isfinite(deadline) and deadline > time.time()
                and _stat(Path("/proc") / str(pid) / "stat")["start"] == start
                and pgid == pid and _stat(Path("/proc") / str(pid) / "stat")["pgid"] == pgid
                and data.get("command_digest") == digest
            )
        except FileNotFoundError:
            return False
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise RuntimeError("verification lease observation incomplete")

    def _processes(self):
        table = {}
        try:
            entries = tuple(Path("/proc").iterdir())
        except OSError as exc:
            raise RuntimeError(f"proc enumeration incomplete: {exc}") from exc
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                info = _stat(entry / "stat")
            except FileNotFoundError:
                continue
            except PermissionError as exc:
                raise RuntimeError(f"proc stat unreadable: {entry.name}") from exc
            except (OSError, ValueError, IndexError) as exc:
                if isinstance(exc, OSError) and exc.errno == 3:
                    continue
                raise RuntimeError(f"proc stat uncertain: {entry.name}: {exc}") from exc
            if info["state"] != "Z":
                table[(int(entry.name), info["start"])] = info
        return table

    def _members(self, table):
        leader = next((info for (pid, start), info in table.items()
                       if pid == self.proc_pid and start == self.leader_start), None)
        if leader is None:
            raise RuntimeError("leader identity changed or disappeared")
        members = {key for key, info in table.items() if info["pgid"] == self.proc_pid}
        members.add((self.proc_pid, self.leader_start))
        by_pid = {pid: (pid, start) for pid, start in table}
        changed = True
        while changed:
            changed = False
            member_pids = {pid for pid, _ in members}
            for key, info in table.items():
                if info["ppid"] in member_pids and key not in members:
                    members.add(key)
                    changed = True
        metadata = dict(self.context.metadata)
        if metadata.get("attempt_id"):
            from dispatch_contract import attempt_tagged_descendants
            tagged = attempt_tagged_descendants(metadata)
            if tagged.state == "unverifiable":
                raise RuntimeError(f"attempt-tag scan uncertain: {tagged.reason}")
            for pid, start, _state in tagged.members:
                key = (int(pid), str(start))
                if key not in table:
                    raise RuntimeError("tagged identity absent from local proc table")
                members.add(key)
        return members

    def sample(self) -> ActivityObservation:
        reasons = []
        try:
            files = self._files()
            table = self._processes()
            members = self._members(table)
            lease = self._lease_active()
            cpu = {key: table[key]["cpu"] for key in members}
            if self._file_baseline is None:
                self._file_baseline = files
                self._process_baseline = cpu
                self._lease_baseline = lease
                return ActivityObservation(False, False, ("baseline",))
            if files != self._file_baseline:
                reasons.append("attempt-file-change")
            old = self._process_baseline
            for key, ticks in cpu.items():
                argv = (Path("/proc") / str(key[0]) / "cmdline").read_bytes().split(b"\0")
                executable = os.fsdecode(argv[0]) if argv and argv[0] else ""
                runtime = self.context.runtime_executable
                is_runtime = bool(runtime and (executable == runtime or Path(executable).name == Path(runtime).name))
                command = b"\0".join(argv)
                is_helper = any(marker in command for marker in (
                    b"dispatch-headless.py", b"launch-fence.py", b"model-governor.py",
                    b"model-governor", b"governor.py",
                ))
                if is_runtime or is_helper:
                    continue
                previous = old.get(key)
                if previous is None:
                    reasons.append("new-process-identity")
                elif ticks > previous:
                    reasons.append("process-cpu")
            if lease:
                reasons.append("exact-verification-lease")
            self._file_baseline = files
            self._process_baseline = cpu
            self._lease_baseline = lease
            return ActivityObservation(bool(reasons), False, tuple(dict.fromkeys(reasons)))
        except Exception as exc:
            return ActivityObservation(True, True, (f"observation-uncertain: {type(exc).__name__}: {exc}",))
