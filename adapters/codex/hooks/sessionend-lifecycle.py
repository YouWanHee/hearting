#!/usr/bin/env python3
"""Bounded Codex SessionEnd bridge with detached memory completion."""

from __future__ import annotations

import json
import hashlib
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = ROOT / "adapters" / "codex" / "bin" / "preflight.sh"
UTILITIES = ROOT / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))


def first_string(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def nested_string(payload: dict[str, Any], *keys: str) -> str:
    direct = first_string(payload, *keys)
    if direct:
        return direct
    for key in ("context", "workspace", "session", "payload", "event", "input", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            found = nested_string(value, *keys)
            if found:
                return found
    return ""


def load_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def input_generation(payload: dict[str, Any]) -> str | None:
    """Fingerprint native transcript metadata without reading conversation text.

    Codex documents transcript_path as nullable. Missing evidence uses a stable
    fallback; a supplied unsafe path cannot authorize another generation.
    """
    source = nested_string(payload, "transcript_path", "transcriptPath")
    if not source or not os.path.isabs(source):
        return None
    fd = -1
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        fields = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def is_worker_session() -> bool:
    return (
        os.environ.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(os.environ.get("AGENT_DISPATCH_DEPTH"))
        or bool(os.environ.get("OPENCODE_DISPATCH_SLUG"))
        or os.environ.get("FLEET_TITLE_REFRESH") == "1"
        or os.environ.get("MEM_DISTILL") == "1"
        or os.environ.get("MEM_SESSION_COMPLETION") == "1"
    )


def run_preflight(*args: str, quiet: bool = False, timeout: float = 0.7) -> None:
    env = os.environ.copy()
    env.setdefault("AGENT_HOME", str(ROOT))
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [str(PREFLIGHT), *args], cwd=str(ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            proc.wait(timeout=max(0.05, timeout))
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        return
    finally:
        # Also clean descendants when their leader exits first, or the outer
        # bridge deadline interrupts wait(). This command owns no detached work.
        if proc is not None:
            _terminate_group(proc, 0.05)


def _terminate_group(proc: subprocess.Popen[bytes], grace: float) -> None:
    """Bounded best-effort termination, including descendants."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    end = time.monotonic() + grace
    while time.monotonic() < end:
        if proc.poll() is not None:
            break
        time.sleep(0.01)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=max(0.05, grace))
    except (OSError, subprocess.TimeoutExpired):
        pass


class _Deadline(BaseException):
    pass


def _deadline_handler(signum: int, frame: Any) -> None:
    raise _Deadline()


def main() -> int:
    # Native Codex SessionEnd is synchronous and bounded to three seconds.
    # Exclusion is intentionally the first action, before imports or writes.
    if is_worker_session():
        return 0
    deadline = time.monotonic() + 2.3
    previous = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, _deadline_handler)
    signal.setitimer(signal.ITIMER_REAL, 2.25)
    try:
        payload = load_payload()
        event_cwd = nested_string(payload, "cwd", "working_directory", "workingDirectory") or os.getcwd()
        event_session_id = nested_string(payload, "session_id", "sessionID", "thread_id", "threadID")
        session = payload.get("session")
        if not event_session_id and isinstance(session, dict):
            event_session_id = first_string(session, "id")
        event_session_id = event_session_id or "codex-hook"
        remaining = deadline - time.monotonic()
        if remaining > 0.15:
            run_preflight("material-route", "clear", "--session", event_session_id,
                          quiet=True, timeout=min(0.8, remaining - 0.05))
        remaining = deadline - time.monotonic()
        if remaining > 0.15:
            try:
                from memory_session_completion import CompletionError, launch
                generation = input_generation(payload)
                if generation is None:
                    if nested_string(payload, "transcript_path", "transcriptPath"):
                        raise CompletionError("input-generation-invalid")
                    sys.stderr.write("codex memory completion: input-generation-unavailable\n")
                generation_args = {} if generation is None else {"input_generation": generation}
                result = launch("codex", event_session_id, event_cwd, str(PREFLIGHT),
                                ["session-end", event_cwd, event_session_id],
                                env=os.environ.copy(), **generation_args)
                if result["state"] in ("failed", "deferred"):
                    sys.stderr.write(f"codex memory completion: {result['reason']}\n")
            except _Deadline:
                raise
            except CompletionError as exc:
                sys.stderr.write(f"codex memory completion: {exc.reason}\n")
            except Exception:
                sys.stderr.write("codex memory completion: launch-failed\n")
        # Optional UI bookkeeping follows the memory handoff so a slow helper
        # cannot prevent the session's final memory processing from starting.
        if event_session_id and time.monotonic() < deadline - 0.25:
            try:
                tools = ROOT / "tools"
                if str(tools) not in sys.path:
                    sys.path.insert(0, str(tools))
                from fleet import interaction
                interaction.clear_wait(event_session_id, "codex")
            except _Deadline:
                raise
            except Exception:
                pass
        if event_session_id and time.monotonic() < deadline - 0.05:
            try:
                from session_summary_trigger import launch_trigger
                launch_trigger("codex", event_session_id, "final")
            except _Deadline:
                raise
            except Exception:
                pass
    except _Deadline:
        return 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if previous_timer[0] > 0:
            elapsed = max(0.0, time.monotonic() - (deadline - 2.3))
            signal.setitimer(signal.ITIMER_REAL, max(0.001, previous_timer[0] - elapsed), previous_timer[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
