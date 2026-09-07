#!/usr/bin/env python3
"""Finite SessionEnd command completion; command exit never proves memory apply.

Linux/POSIX only. Receipts contain lifecycle metadata, never command arguments
or output. A terminal receipt deduplicates the same session/input generation.
A new generation can run after the existing lease ends; an active different
generation is deferred without changing its receipt or scheduling a retry.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = 1
DEFAULT_INPUT_GENERATION = hashlib.sha256(b"hearting-session-completion:unversioned").hexdigest()
MAX_ID = 160
MAX_ARG = 4096
MAX_RECEIPT = 64 * 1024
WORKER_KEYS = (
    "AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
    "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL",
)
FIELDS = {
    "schema", "key", "nonce", "harness", "session_id_hash", "state", "reason",
    "launcher", "runner", "command", "started_at", "timeout", "exit_code",
    "runner_exit", "duration_ms", "memory_apply", "input_generation",
}
FAILURES = {"timeout", "command-failed", "spawn-failed", "identity-unavailable",
            "receipt-write-failed", "receipt-publish-failed", "runner-failed"}


class CompletionError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def excluded(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return (
        env.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or env.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(env.get("AGENT_DISPATCH_DEPTH"))
        or bool(env.get("OPENCODE_DISPATCH_SLUG"))
        or env.get("FLEET_TITLE_REFRESH") == "1"
        or env.get("MEM_DISTILL") == "1"
        or env.get("MEM_SESSION_COMPLETION") == "1"
    )


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _valid_identity(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {"pid", "start_ticks", "boot_id"}
            and _integer(value["pid"], 1) and _integer(value["start_ticks"], 1)
            and isinstance(value["boot_id"], str)
            and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value["boot_id"]) is not None)


def _identity(pid: int | None) -> dict[str, Any] | None:
    if not _integer(pid, 1):
        return None
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = data[data.rfind(")") + 2:].split()
        value = {"pid": pid, "start_ticks": int(fields[19]),
                 "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()}
        return value if _valid_identity(value) else None
    except (OSError, ValueError, IndexError):
        return None


def identity_state(identity: dict[str, Any] | None) -> str:
    """Unknown /proc inspection must never be mistaken for a dead process."""
    if not _valid_identity(identity):
        return "unknown"
    current = _identity(identity["pid"])
    if current is not None:
        return "alive" if current == identity else "dead"
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if boot != identity["boot_id"] and re.fullmatch(r"[0-9a-f-]{36}", boot):
            return "dead"
        os.kill(identity["pid"], 0)
    except ProcessLookupError:
        return "dead"
    except OSError:
        pass
    return "unknown"


def identity_alive(identity: dict[str, Any] | None) -> bool:
    return identity_state(identity) == "alive"


def _safe_abs(value: str, label: str) -> Path:
    if (not isinstance(value, str) or not value.startswith("/") or "\x00" in value
            or ".." in value.split("/")):
        raise CompletionError(f"invalid-{label}")
    return Path(value)


def _leaf(root: Path, *, create: bool = True) -> int:
    """Walk from / through opened no-follow dirfds; never re-open a pathname."""
    root = _safe_abs(str(root), "receipt-root")
    if not hasattr(os, "O_NOFOLLOW") or not sys.platform.startswith("linux"):
        raise CompletionError("unsupported-platform")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for component in root.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise CompletionError("receipt-root-insecure")
        result, fd = fd, -1
        return result
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise CompletionError("receipt-root-unavailable") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def receipt_root(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("MEM_SESSION_COMPLETION_RECEIPTS"):
        return _safe_abs(env["MEM_SESSION_COMPLETION_RECEIPTS"], "receipt-root")
    # Do not consult the process HOME when an explicit environment was supplied.
    base = env.get("XDG_STATE_HOME")
    if not base:
        home = env.get("HOME")
        if not home:
            raise CompletionError("missing-state-home")
        base = str(_safe_abs(home, "home") / ".local" / "state")
    return _safe_abs(base, "state-home") / "agent-memory" / "session-completion"


def _key(harness: str, session_id: str) -> str:
    if (not isinstance(harness, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", harness) is None
            or not isinstance(session_id, str) or not 0 < len(session_id) <= MAX_ID
            or any(ord(c) < 32 or ord(c) == 127 for c in session_id)):
        raise CompletionError("invalid-session-id")
    try:
        return hashlib.sha256((harness + "\0" + session_id).encode()).hexdigest()
    except UnicodeError as exc:
        raise CompletionError("invalid-session-id") from exc


def stat_is_regular_private(st: os.stat_result) -> bool:
    return (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
            and stat.S_IMODE(st.st_mode) == 0o600 and st.st_nlink == 1)


def _valid_receipt(value: Any, name: str) -> bool:
    if not isinstance(value, dict) or set(value) != FIELDS:
        return False
    if (type(value["schema"]) is not int or value["schema"] != SCHEMA
            or re.fullmatch(r"[0-9a-f]{64}\.json", name) is None
            or value["key"] != name[:-5]
            or not isinstance(value["nonce"], str) or re.fullmatch(r"[0-9a-f]{32}", value["nonce"]) is None
            or not isinstance(value["harness"], str) or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value["harness"]) is None
            or not isinstance(value["session_id_hash"], str) or re.fullmatch(r"[0-9a-f]{64}", value["session_id_hash"]) is None
            or not isinstance(value["input_generation"], str) or re.fullmatch(r"[0-9a-f]{64}", value["input_generation"]) is None
            or not _valid_identity(value["launcher"])
            or any(value[k] is not None and not _valid_identity(value[k]) for k in ("runner", "command"))
            or not _finite(value["started_at"]) or value["started_at"] <= 0
            or not _finite(value["timeout"]) or not 0.1 <= value["timeout"] <= 3600
            or not _integer(value["duration_ms"])
            or value["memory_apply"] != "not-asserted"):
        return False
    code = value["exit_code"]
    if code is not None and (type(code) is not int or not -255 <= code <= 255):
        return False
    state = value["state"]
    if state == "started":
        return value["reason"] == "launching" and code is None and value["runner_exit"] is None
    if state == "completed":
        return (value["reason"] == "completed" and type(code) is int and code == 0
                and type(value["runner_exit"]) is int and value["runner_exit"] == 0
                and value["runner"] is not None and value["command"] is not None)
    return (state == "failed" and isinstance(value["reason"], str) and value["reason"] in FAILURES
            and type(value["runner_exit"]) is int and value["runner_exit"] == 1)


def _read_at(fd: int, name: str) -> dict[str, Any] | None:
    if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
        raise CompletionError("invalid-receipt")
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CompletionError("invalid-receipt") from exc
    try:
        st = os.fstat(source)
        if not stat_is_regular_private(st) or st.st_size > MAX_RECEIPT:
            raise CompletionError("invalid-receipt")
        with os.fdopen(source, "rb", closefd=False) as stream:
            data = stream.read(MAX_RECEIPT + 1)
        if len(data) > MAX_RECEIPT:
            raise CompletionError("receipt-too-large")
        # Duplicate JSON keys are malformed, even if their final value is valid.
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, val in items:
                if key in result:
                    raise CompletionError("malformed-receipt")
                result[key] = val
            return result
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
        if not _valid_receipt(value, name):
            raise CompletionError("invalid-receipt")
        return value
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise CompletionError("malformed-receipt") from exc
    finally:
        os.close(source)


def _publish(fd: int, name: str, value: dict[str, Any], *, expected_nonce: str | None) -> None:
    if not _valid_receipt(value, name):
        raise CompletionError("invalid-receipt")
    prior = _read_at(fd, name)
    if (prior is None) != (expected_nonce is None) or (prior and prior["nonce"] != expected_nonce):
        raise CompletionError("receipt-ownership-changed")
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(blob) > MAX_RECEIPT:
        raise CompletionError("receipt-too-large")
    tmp = f".{name}.{secrets.token_hex(16)}.tmp"
    out = -1
    replacing = False
    try:
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        offset = 0
        while offset < len(blob):
            count = os.write(out, blob[offset:])
            if count <= 0:
                raise OSError(errno.EIO, "write-failed")
            offset += count
        os.fsync(out)
        os.close(out)
        out = -1
        # Recheck after the temporary write too: a replaced receipt must not
        # be overwritten merely because it matched before a slow fsync.
        current = _read_at(fd, name)
        if ((current is None) != (expected_nonce is None)
                or (current and current["nonce"] != expected_nonce)):
            raise CompletionError("receipt-ownership-changed")
        replacing = True
        os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except OSError as exc:
        raise CompletionError("receipt-publish-failed" if replacing else "receipt-write-failed") from exc
    finally:
        try:
            if out >= 0:
                os.close(out)
        finally:
            try:
                os.unlink(tmp, dir_fd=fd)
            except FileNotFoundError:
                pass


def _timeout(env: dict[str, str], explicit: float | None) -> float:
    try:
        value = explicit if explicit is not None else float(env.get("MEM_SESSION_COMPLETION_TIMEOUT", "900"))
        if not _finite(value) or (explicit is not None and not 0.1 <= value <= 3600):
            raise ValueError
        return float(value) if explicit is not None else min(3600.0, max(30.0, value))
    except (ValueError, TypeError, OverflowError) as exc:
        raise CompletionError("invalid-timeout") from exc


def _terminate(proc: subprocess.Popen[bytes], grace: float = 0.2) -> None:
    # Always escalate the group, including when the leader already exited.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + min(grace, 2.0)
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            break
        proc.poll()
        time.sleep(0.01)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _check_lock(fd: int, lock: int, name: str) -> None:
    opened = os.fstat(lock)
    current = os.stat(name[:-5] + ".lock", dir_fd=fd, follow_symlinks=False)
    if (not stat_is_regular_private(opened) or not stat_is_regular_private(current)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
        raise CompletionError("invalid-lock")


def _terminal(receipt: dict[str, Any], reason: str, code: int | None, started: float) -> None:
    receipt.update(state="completed" if reason == "completed" else "failed", reason=reason,
                   exit_code=code, runner_exit=0 if reason == "completed" else 1,
                   duration_ms=max(0, int((time.monotonic() - started) * 1000)))


def _runner(fd: int, lock: int, name: str, nonce: str, executable: str, argv: list[str], cwd: str, timeout: float) -> int:
    receipt = None
    command: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    reason, code = "runner-failed", None
    try:
        _check_lock(fd, lock, name)
        receipt = _read_at(fd, name)
        if receipt is None or receipt["nonce"] != nonce or receipt["state"] != "started":
            raise CompletionError("receipt-ownership-changed")
        if _timeout({}, timeout) != receipt["timeout"]:
            raise CompletionError("invalid-timeout")
        receipt["runner"] = _identity(os.getpid())
        if receipt["runner"] is None:
            raise CompletionError("identity-unavailable")
        _publish(fd, name, receipt, expected_nonce=nonce)
        try:
            command = subprocess.Popen([executable, *argv], cwd=cwd, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=True, pass_fds=(lock,), env=os.environ.copy())
        except OSError:
            reason = "spawn-failed"
        else:
            receipt["command"] = _identity(command.pid)
            if receipt["command"] is None:
                raise CompletionError("identity-unavailable")
            _publish(fd, name, receipt, expected_nonce=nonce)
            try:
                code = command.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
                reason = "completed" if code == 0 else "command-failed"
            except subprocess.TimeoutExpired:
                reason = "timeout"
    except CompletionError as exc:
        reason = exc.reason if exc.reason in FAILURES else "runner-failed"
    except Exception:
        reason = "runner-failed"
    finally:
        try:
            if command is not None:
                _terminate(command)
                code = command.returncode
            if receipt is not None:
                _terminal(receipt, reason, code, started)
                try:
                    _check_lock(fd, lock, name)
                    _publish(fd, name, receipt, expected_nonce=nonce)
                except (CompletionError, OSError):
                    # In particular, a post-rename fsync failure must not leave
                    # a newly published successful receipt as the final claim.
                    _terminal(receipt, "receipt-publish-failed", code, started)
                    try:
                        _check_lock(fd, lock, name)
                        _publish(fd, name, receipt, expected_nonce=nonce)
                    except (CompletionError, OSError):
                        pass
                    reason = "receipt-publish-failed"
        finally:
            os.close(lock)
            os.close(fd)
    return 0 if reason == "completed" else 1


def launch(harness: str, session_id: str, cwd: str, executable: str, argv: list[str], *, timeout: float | None = None,
           env: dict[str, str] | None = None, input_generation: str = DEFAULT_INPUT_GENERATION) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    if excluded(env):
        return {"state": "excluded", "reason": "worker-context"}
    if not isinstance(input_generation, str) or re.fullmatch(r"[0-9a-f]{64}", input_generation) is None:
        raise CompletionError("invalid-input-generation")
    if not isinstance(argv, list) or len(argv) > 64 or any(not isinstance(a, str) or len(a) > MAX_ARG or "\x00" in a for a in argv):
        raise CompletionError("invalid-argv")
    cwd_path, exe = _safe_abs(cwd, "cwd"), _safe_abs(executable, "executable")
    if not cwd_path.is_dir() or not exe.is_file() or not os.access(exe, os.X_OK):
        raise CompletionError("invalid-command")
    limit, key = _timeout(env, timeout), _key(harness, session_id)
    session_hash = hashlib.sha256(session_id.encode()).hexdigest()
    launcher = _identity(os.getpid())
    if launcher is None:
        raise CompletionError("identity-unavailable")
    fd = _leaf(receipt_root(env))
    name, lock = f"{key}.json", -1
    try:
        try:
            lock = os.open(f"{key}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=fd)
            _check_lock(fd, lock, name)
        except OSError as exc:
            raise CompletionError("invalid-lock") from exc
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                active = _read_at(fd, name)
                if active:
                    if active["harness"] != harness or active["session_id_hash"] != session_hash:
                        raise CompletionError("invalid-receipt")
                    if active["input_generation"] != input_generation:
                        return {"state": "deferred", "reason": "active-input-generation"}
                return {"state": "duplicate", "reason": "active"}
            raise CompletionError("lock-failed") from exc
        _check_lock(fd, lock, name)
        prior = _read_at(fd, name)
        if prior:
            if prior["harness"] != harness or prior["session_id_hash"] != session_hash:
                raise CompletionError("invalid-receipt")
            if prior["state"] in ("completed", "failed"):
                if prior["input_generation"] == input_generation:
                    return {"state": "duplicate", "reason": "terminal"}
            else:
                identities = [prior[k] for k in ("runner", "command") if prior[k] is not None] or [prior["launcher"]]
                states = [identity_state(identity) for identity in identities]
                if "unknown" in states:
                    raise CompletionError("identity-unavailable")
                if "alive" in states:
                    if prior["input_generation"] != input_generation:
                        return {"state": "deferred", "reason": "active-input-generation"}
                    return {"state": "duplicate", "reason": "active"}
        started = time.monotonic()
        receipt = {"schema": SCHEMA, "key": key, "nonce": secrets.token_hex(16), "harness": harness,
                   "session_id_hash": session_hash, "state": "started", "reason": "launching",
                   "launcher": launcher, "runner": None, "command": None, "started_at": time.time(),
                   "timeout": limit, "exit_code": None, "runner_exit": None, "duration_ms": 0,
                   "memory_apply": "not-asserted", "input_generation": input_generation}
        try:
            _publish(fd, name, receipt, expected_nonce=prior["nonce"] if prior else None)
        except CompletionError as exc:
            # A directory fsync can fail after rename. If our claim reached
            # disk, make that claim terminal without ever starting a child.
            try:
                current = _read_at(fd, name)
                if current and current["nonce"] == receipt["nonce"]:
                    _terminal(receipt, exc.reason if exc.reason in FAILURES else "runner-failed", None, started)
                    _publish(fd, name, receipt, expected_nonce=receipt["nonce"])
            except (CompletionError, OSError):
                pass
            return {"state": "failed", "reason": exc.reason, "key": key}
        child_env = dict(env, MEM_SESSION_COMPLETION="1")
        cmd = [sys.executable, str(Path(__file__).absolute()), "--run", str(fd), str(lock), name,
               receipt["nonce"], str(exe), str(cwd_path), str(limit), "--", *argv]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True, pass_fds=(fd, lock), env=child_env)
        except OSError:
            _terminal(receipt, "spawn-failed", None, started)
            try:
                _publish(fd, name, receipt, expected_nonce=receipt["nonce"])
            except CompletionError:
                pass
            return {"state": "failed", "reason": "spawn-failed", "key": key}
        # No launcher publication after spawn: the child may already be terminal.
        # A finite daemon reaper owns Popen's wait status in long-lived callers;
        # short-lived hooks simply exit and the runner is adopted by the OS.
        threading.Thread(target=proc.wait, daemon=True, name="memory-completion-reap").start()
        return {"state": "started", "key": key, "runner": _identity(proc.pid)}
    finally:
        if lock >= 0:
            os.close(lock)
        os.close(fd)


def read_receipt(harness: str, session_id: str, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    key = _key(harness, session_id)
    try:
        fd = _leaf(receipt_root(env), create=False)
    except FileNotFoundError:
        return None
    try:
        value = _read_at(fd, f"{key}.json")
        if value and (value["harness"] != harness or value["session_id_hash"] != hashlib.sha256(session_id.encode()).hexdigest()):
            raise CompletionError("invalid-receipt")
        return value
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run", nargs=7)
    parser.add_argument("runner_args", nargs=argparse.REMAINDER)
    ns = parser.parse_args()
    if not ns.run:
        return 2
    fd_s, lock_s, name, nonce, executable, cwd, timeout_s = ns.run
    argv = ns.runner_args[1:] if ns.runner_args[:1] == ["--"] else ns.runner_args
    try:
        return _runner(int(fd_s), int(lock_s), name, nonce, executable, argv, cwd, _timeout({}, float(timeout_s)))
    except (CompletionError, OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
