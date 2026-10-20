"""Conservative launcher-local activity sampling for foreground workers."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
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
    opencode_db: Path | None = None


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


def build_activity_context(*, worker_type: str, harness: str, log_path: Path,
                           jobs: Path, launch_metadata: Mapping[str, str],
                           attempt_id: str, route_id: str | None,
                           route_node: str | None, environment: Mapping[str, str],
                           worktree: Path) -> ForegroundActivityContext | None:
    """Build adapter-specific observation state without risking a live child."""
    if worker_type == "owner":
        return None
    try:
        policy = resolve_activity_policy(worker_type)
        if policy.window_seconds is None:
            for warning in policy.warnings:
                print(f"warning={warning}", file=sys.stderr)
            return None
        roots = ()
        opencode_db = None
        if harness == "claude":
            from claude_project_dir import encode_project_dir
            project = Path(environment["CLAUDE_CONFIG_DIR"]) / "projects" / encode_project_dir(worktree)
            roots = (project,)
        elif harness == "codex":
            roots = (Path(environment["CODEX_HOME"]) / "sessions",)
        elif harness == "opencode":
            from opencode_runtime import opencode_db_path
            runtime = launch_metadata.get("opencode_runtime_dir")
            if runtime:
                data = Path(runtime) / "data" / "opencode"
                roots = (data / "opencode.db", data / "opencode.db-wal")
            else:
                # Shared storage is not an attempt-owned file. Only changes to
                # the exact session bound by this attempt's log prove activity.
                opencode_db = opencode_db_path(environment)
        else:
            raise ValueError(f"unsupported foreground activity harness: {harness}")
        metadata = {
            **{str(key): str(value) for key, value in launch_metadata.items()},
            "attempt_id": attempt_id,
            "route_id": route_id or "",
            "route_node": route_node or "",
        }
        context = ForegroundActivityContext(
            window_seconds=policy.window_seconds, harness=harness,
            log_path=Path(log_path), jobs=Path(jobs), metadata=metadata,
            runtime_executable=harness, transcript_roots=roots,
            opencode_db=opencode_db,
        )
        for warning in policy.warnings:
            print(f"warning={warning}", file=sys.stderr)
        return context
    except Exception as exc:
        print(f"warning=foreground activity context unavailable: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None


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
        "self_cpu": sum(int(value) for value in tail[11:13]),
        "children_cpu": sum(int(value) for value in tail[13:15]),
        "start": tail[19],
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
        self._session_baseline = None

    def _opencode_session_state(self):
        if self.context.opencode_db is None:
            return None
        from opencode_server_log import bind_session
        from opencode_runtime import read_session_observation
        session = bind_session(self.context.log_path)
        if not session:
            return None
        observation = read_session_observation(
            session, db_path=self.context.opencode_db, shell_after_seq=None)
        if observation.get("state") != "observed":
            return None
        # Do not include the shared DB/WAL stat or session title. Other workers
        # and background title generation must not reset this attempt's clock.
        identity = (session, observation.get("database_identity"))
        progress = json.dumps(observation.get("messages", []), sort_keys=True)
        return identity, hashlib.sha256(progress.encode()).hexdigest()

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

    def _runtime_lineage(self, table, members):
        """Return the shallowest harness CLI identities and their ancestors."""
        by_pid = {pid: (pid, start) for pid, start in members}
        depths = { (self.proc_pid, self.leader_start): 0 }
        pending = [ (self.proc_pid, self.leader_start) ]
        while pending:
            parent = pending.pop(0)
            parent_pid = parent[0]
            for key in members:
                if key not in depths and table[key]["ppid"] == parent_pid:
                    depths[key] = depths[parent] + 1
                    pending.append(key)

        interpreter_names = {"python", "node", "nodejs", "bun"}
        matches = []
        for key in members:
            proc = Path("/proc") / str(key[0])
            try:
                raw = (proc / "cmdline").read_bytes()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RuntimeError(f"process command line unreadable: {key[0]}") from exc
            argv = [os.fsdecode(part) for part in raw.rstrip(b"\0").split(b"\0") if part]
            if not argv:
                continue
            first = Path(argv[0]).name
            identity = first == self.context.harness
            is_interpreter = first in interpreter_names or re.fullmatch(r"python\d+(?:\.\d+)*", first)
            if not identity and is_interpreter and len(argv) > 1:
                identity = Path(argv[1]).name == self.context.harness
            if identity:
                if key not in depths:
                    raise RuntimeError("harness CLI is not a descendant of the foreground leader")
                matches.append(key)
        if not matches:
            raise RuntimeError("harness CLI identity absent from member process set")

        shallowest = min(depths[key] for key in matches)
        lineage = set()
        for cli in matches:
            if depths[cli] != shallowest:
                continue
            current = cli
            while True:
                lineage.add(current)
                if current == (self.proc_pid, self.leader_start):
                    break
                parent_pid = table[current]["ppid"]
                parent = by_pid.get(parent_pid)
                if parent is None or parent not in depths:
                    raise RuntimeError("harness CLI ancestor chain does not reach foreground leader")
                current = parent
        return lineage

    def sample(self) -> ActivityObservation:
        reasons = []
        try:
            files = self._files()
            session_state = self._opencode_session_state()
            session_uncertain = self.context.opencode_db is not None and session_state is None
            table = self._processes()
            members = self._members(table)
            runtime_lineage = self._runtime_lineage(table, members)
            lease = self._lease_active()
            cpu = {key: (table[key]["self_cpu"], table[key]["children_cpu"])
                   for key in members}
            if self._file_baseline is None:
                self._file_baseline = files
                self._process_baseline = cpu
                self._lease_baseline = lease
                self._session_baseline = session_state
                return ActivityObservation(False, session_uncertain, ("baseline",))
            if files != self._file_baseline:
                reasons.append("attempt-file-change")
            if (session_state is not None and self._session_baseline is not None
                    and session_state[0] == self._session_baseline[0]
                    and session_state[1] != self._session_baseline[1]):
                reasons.append("exact-opencode-session-change")
            old = self._process_baseline
            for key, (self_ticks, children_ticks) in cpu.items():
                previous = old.get(key)
                if previous is None:
                    if key not in runtime_lineage:
                        reasons.append("new-process-identity")
                    if children_ticks > 0:
                        reasons.append("children-cpu")
                elif key not in runtime_lineage and self_ticks > previous[0]:
                    reasons.append("process-cpu")
                if previous is not None and children_ticks > previous[1]:
                    reasons.append("children-cpu")
            if lease:
                reasons.append("exact-verification-lease")
            self._file_baseline = files
            self._process_baseline = cpu
            self._lease_baseline = lease
            self._session_baseline = session_state
            if session_uncertain:
                reasons.append("opencode-session-unobserved")
            return ActivityObservation(
                any(reason != "opencode-session-unobserved" for reason in reasons),
                session_uncertain, tuple(dict.fromkeys(reasons)))
        except Exception as exc:
            return ActivityObservation(True, True, (f"observation-uncertain: {type(exc).__name__}: {exc}",))
