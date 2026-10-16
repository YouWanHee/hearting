"""Conservative normalization for Herdr 0.9.x pane observations.

Herdr remains optional. A failed command is unknown; only a successful,
well-shaped pane census can confirm absence. Event envelopes are applied only
to a snapshot from the same endpoint and connection generation.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Callable


def endpoint_name(session: str | None = None) -> str:
    value = session if session is not None else os.environ.get("AGENT_HERDR_SESSION")
    return value.strip() if isinstance(value, str) and value.strip() else "default"


@dataclass(frozen=True)
class PaneCensus:
    endpoint: str
    panes: tuple[dict, ...]
    observed_at: float
    complete: bool = True


def pane_census(*, session: str | None = None,
                runner: Callable = subprocess.run,
                which: Callable = shutil.which) -> PaneCensus | None:
    """Read one complete ``herdr pane list`` result; return None on uncertainty."""
    if which("herdr") is None:
        return None
    server = endpoint_name(session)
    argv = ["herdr"]
    if server != "default":
        argv.extend(("--session", server))
    argv.extend(("pane", "list"))
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=2.0)
        if getattr(proc, "returncode", 1) != 0:
            return None
        payload = json.loads(proc.stdout or "")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = result.get("panes") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return None
    panes = []
    seen = set()
    for row in rows:
        pane_id = row.get("pane_id") if isinstance(row, dict) else None
        if not isinstance(pane_id, str) or not pane_id or pane_id in seen:
            return None
        seen.add(pane_id)
        panes.append(dict(row))
    return PaneCensus(server, tuple(panes), time.monotonic())


def confirms_absence(census: PaneCensus | None, pane_id: str,
                     *, endpoint: str | None = None) -> bool:
    """A missing pane is terminal only in a complete census of its endpoint."""
    if (census is None or census.complete is not True
            or not isinstance(pane_id, str) or not pane_id
            or (endpoint is not None and census.endpoint != endpoint_name(endpoint))):
        return False
    return all(row.get("pane_id") != pane_id for row in census.panes)


@dataclass
class ObservationSnapshot:
    """A small event reducer; reconnect starts a new, untrusted generation."""

    endpoint: str
    generation: int = 0
    complete: bool = False
    panes: dict[str, dict] = field(default_factory=dict)
    terminal: dict[str, str] = field(default_factory=dict)

    def reconnect(self) -> None:
        self.generation += 1
        self.complete = False
        self.panes.clear()
        self.terminal.clear()

    def replace_snapshot(self, endpoint: str, panes) -> bool:
        if endpoint_name(endpoint) != self.endpoint or not isinstance(panes, list):
            self.reconnect()
            return False
        replacement = {}
        for pane in panes:
            pane_id = pane.get("pane_id") if isinstance(pane, dict) else None
            if not isinstance(pane_id, str) or not pane_id or pane_id in replacement:
                self.reconnect()
                return False
            replacement[pane_id] = dict(pane)
        self.generation += 1
        self.panes = replacement
        self.terminal = {}
        self.complete = True
        return True

    def apply_event(self, endpoint: str, envelope: dict) -> bool:
        if endpoint_name(endpoint) != self.endpoint or not self.complete:
            return False
        if not isinstance(envelope, dict):
            return False
        data = envelope.get("data")
        event = envelope.get("event")
        if not isinstance(data, dict):
            return False
        event = event or data.get("type")
        pane_id = data.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            return False
        if event in {"pane_closed", "pane.closed"}:
            self.panes.pop(pane_id, None)
            self.terminal[pane_id] = "pane-closed"
            return True
        if event in {"pane_exited", "pane.exited"}:
            self.panes.pop(pane_id, None)
            self.terminal[pane_id] = "pane-exited"
            return True
        pane = data.get("pane")
        if event in {"pane_created", "pane.updated", "pane_updated", "pane.created"}:
            if not isinstance(pane, dict) or pane.get("pane_id") != pane_id:
                self.complete = False
                return False
            self.panes[pane_id] = dict(pane)
            self.terminal.pop(pane_id, None)
        return True

    def confirms_absence(self, pane_id: str) -> bool:
        return self.complete and pane_id not in self.panes


def _native_status_path(endpoint: str, pane_id: str, harness: str, session_id: str) -> Path:
    from dispatch_contract import stable_state_root
    key = "\0".join((endpoint_name(endpoint), pane_id, harness, session_id)).encode()
    return stable_state_root(os.environ) / "herdr-observation" / "native-turn" / (sha256(key).hexdigest() + ".json")


def _process_start_ticks(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rindex(")") + 1:].split()
        if fields[0] in {"Z", "X"} or not fields[19].isdigit():
            return None
        return fields[19]
    except (OSError, ValueError, IndexError):
        return None


def record_native_status(payload: dict, *, now: float | None = None) -> bool:
    """Persist one exact native OpenCode turn event for bounded retire reads."""
    if not isinstance(payload, dict):
        return False
    endpoint = endpoint_name(payload.get("endpoint"))
    pane = payload.get("pane")
    harness = payload.get("harness")
    session_id = payload.get("session_id")
    pid = payload.get("pid")
    pid_start = payload.get("pid_start")
    status = payload.get("status")
    if (harness != "opencode" or not all(isinstance(value, str) and value and value != "-"
                                         for value in (pane, session_id))
            or type(pid) is not int or pid <= 1
            or not isinstance(pid_start, str) or not pid_start.isdigit()
            or status not in {"idle", "working", "blocked", "unknown"}
            or _process_start_ticks(pid) != pid_start):
        return False
    try:
        namespace = os.readlink(f"/proc/{pid}/ns/pid")
        if namespace != os.readlink("/proc/self/ns/pid"):
            return False
    except OSError:
        return False
    observed_at = payload.get("observed_at", time.time() if now is None else now)
    if type(observed_at) not in (float, int) or not (0 < observed_at < float("inf")):
        return False
    row = {"schema_version": 1, "endpoint": endpoint, "pane": pane,
           "harness": harness, "session_id": session_id, "pid": pid,
           "pid_start": pid_start, "pid_namespace": namespace,
           "status": status, "observed_at": float(observed_at)}
    path = _native_status_path(endpoint, pane, harness, session_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".native-turn-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(row, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return True
    except OSError:
        return False
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def read_native_status(endpoint: str, pane: str, harness: str, session_id: str,
                       pid: int, pid_start: str, *, max_age: float = 15.0,
                       now: float | None = None) -> dict | None:
    """Return a fresh native event only for its exact pane, SID and PID birth."""
    try:
        path = _native_status_path(endpoint, pane, harness, session_id)
        row = json.loads(path.read_text(encoding="utf-8"))
        current = time.time() if now is None else now
        if (not isinstance(row, dict) or row.get("schema_version") != 1
                or any(row.get(key) != value for key, value in (
                    ("endpoint", endpoint_name(endpoint)), ("pane", pane),
                    ("harness", harness), ("session_id", session_id),
                    ("pid", pid), ("pid_start", pid_start)))
                or row.get("status") not in {"idle", "working", "blocked", "unknown"}
                or type(row.get("observed_at")) not in (int, float)
                or current - row["observed_at"] < 0
                or current - row["observed_at"] > max_age
                or _process_start_ticks(pid) != pid_start
                or row.get("pid_namespace") != os.readlink(f"/proc/{pid}/ns/pid")
                or row.get("pid_namespace") != os.readlink("/proc/self/ns/pid")):
            return None
        return row
    except (OSError, ValueError, TypeError):
        return None


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["record-native-turn"]:
        return 2
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 1
    return 0 if record_native_status(payload) else 1


if __name__ == "__main__":
    raise SystemExit(main())
