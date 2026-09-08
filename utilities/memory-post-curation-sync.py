#!/usr/bin/env python3
"""One bounded sync owned by the main session completion controller."""
from __future__ import annotations
import ctypes
import json
import hashlib
import secrets
import stat
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
STATUSES = {"not-configured", "local-only", "queued-offline", "fetched", "folded",
            "conflict", "quarantined", "push-retry-exhausted", "remote-confirmed", "hard-failure"}


def is_worker(env):
    # MEM_SESSION_COMPLETION identifies the main-owned Codex controller.
    return (env.get("AGENT_SESSION_ROLE", "").lower() == "worker"
            or env.get("AGENT_DISPATCH_CHILD") == "1"
            or bool(env.get("AGENT_DISPATCH_DEPTH"))
            or bool(env.get("OPENCODE_DISPATCH_SLUG"))
            or env.get("FLEET_TITLE_REFRESH") == "1"
            or env.get("MEM_DISTILL") == "1")


def process(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[19]), fields[0]
    except (OSError, ValueError, IndexError):
        return None


def track(known):
    """Keep exact PID birth identities for this controller's descendants."""
    rows = {}
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            row = process(int(path.name))
            if row:
                rows[int(path.name)] = row
    parents = {os.getpid()}
    parents.update(pid for pid, birth in known.items()
                   if pid in rows and rows[pid][1] == birth)
    for _ in range(len(rows) + 1):
        added = {pid for pid, row in rows.items() if row[0] in parents and pid not in parents}
        if not added:
            break
        for pid in added:
            known[pid] = rows[pid][1]
        parents.update(added)


def cleanup(proc, known):
    # Inherit the completion group so an outer deadline owns this whole tree.
    # Linux subreaping also exposes orphaned git children after mem.py exits.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        track(known)
        for pid, birth in reversed(list(known.items())):
            row = process(pid)
            if row and row[1] == birth and row[2] != "Z":
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        try:
            proc.wait(timeout=0.15)
        except subprocess.TimeoutExpired:
            pass
    for pid in known:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def run_sync(cwd, env=None, command=None):
    env = dict(os.environ if env is None else env)
    if is_worker(env):
        return None
    result = {"status_schema": 1, "phase": "post-curation-sync",
              "status": "hard-failure", "exit_code": 2}
    try:
        limit = float(env.get("MEM_POST_CURATION_SYNC_TIMEOUT", "60"))
        if not math.isfinite(limit) or not 1 <= limit <= 300:
            raise ValueError
    except (ValueError, TypeError):
        return dict(result, reason="invalid-timeout")
    try:
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError
    except (OSError, AttributeError):
        return dict(result, reason="process-supervision-unavailable")
    proc = None
    known = {}
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(command or [sys.executable, str(ROOT / "tools/memory/mem.py"),
                                               "sync", "--json"], cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            row = process(proc.pid)
            if row:
                known[proc.pid] = row[1]
            deadline = time.monotonic() + limit
            while proc.poll() is None:
                track(known)
                if time.monotonic() >= deadline:
                    return dict(result, reason="timeout", exit_code=124)
                time.sleep(0.05)
            out.seek(0)
            raw = out.read(65537)
            try:
                value = json.loads(raw) if len(raw) <= 65536 else None
            except (ValueError, UnicodeError):
                value = None
            if (not isinstance(value, dict) or value.get("status") not in STATUSES
                    or type(value.get("exit_code")) is not int
                    or value["exit_code"] not in (0, 1, 2)
                    or value["exit_code"] != proc.returncode
                    or (value["status"] == "hard-failure" and proc.returncode != 2)
                    or (value["status"] in {"queued-offline", "conflict", "quarantined", "push-retry-exhausted"}
                        and proc.returncode == 0)):
                return dict(result, reason="invalid-sync-result",
                            exit_code=proc.returncode if proc.returncode > 0 else 2)
            # No paths, remote URLs, record bodies, or raw stderr in diagnostics.
            result.update(status=value["status"], exit_code=proc.returncode if proc.returncode >= 0 else 2)
            if result["exit_code"]:
                result["reason"] = "sync-incomplete"
            return result
    except InterruptedError:
        raise
    except (OSError, ValueError):
        return dict(result, reason="sync-launch-failed")
    finally:
        if proc is not None:
            cleanup(proc, known)


def record_failure(cwd, runtime, sid, result, env):
    """One private latest-failure receipt per runtime, bounded to three files."""
    from memory_session_completion import CompletionError, _leaf
    if runtime not in ("codex", "claude", "opencode"):
        raise ValueError("invalid-runtime")
    base = env.get("XDG_STATE_HOME") or str(Path(env["HOME"]) / ".local/state")
    root = Path(base) / "agent-memory/post-curation-failures"
    fd = _leaf(root)
    name = runtime + ".json"
    tmp = "." + runtime + "." + secrets.token_hex(12) + ".tmp"
    schema = "hearting.memory-post-curation-failure/v1"
    def current():
        try:
            item = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(item)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError("invalid-failure-receipt")
            raw = os.read(item, 4097)
            value = json.loads(raw) if len(raw) <= 4096 else None
            if (not isinstance(value, dict) or value.get("schema") != schema
                    or value.get("runtime") != runtime):
                raise ValueError("foreign-failure-receipt")
            return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size
        finally:
            os.close(item)
    try:
        before = current()
        payload = dict(result, schema=schema, runtime=runtime, timestamp=int(time.time()),
                       session_id_hash=hashlib.sha256(sid.encode()).hexdigest(),
                       cwd_hash=hashlib.sha256(os.fsencode(cwd)).hexdigest())
        raw = json.dumps(payload, sort_keys=True).encode() + b"\n"
        if len(raw) > 4096:
            raise ValueError("failure-receipt-too-large")
        item = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(item, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if current() != before:
                raise ValueError("failure-receipt-changed")
            os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(tmp, dir_fd=fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)


def main():
    if is_worker(os.environ):
        return 0
    if len(sys.argv) != 4 or sys.argv[2] not in ("codex", "claude", "opencode"):
        return 64
    def interrupted(signum, frame):
        raise InterruptedError
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        result = run_sync(sys.argv[1])
    except InterruptedError:
        result = {"status_schema": 1, "phase": "post-curation-sync",
                  "status": "hard-failure", "exit_code": 124, "reason": "interrupted"}
    if result is None:
        return 0
    if result["exit_code"]:
        try:
            record_failure(sys.argv[1], sys.argv[2], sys.argv[3], result, os.environ)
        except Exception:
            result["failure_receipt"] = "unavailable"
    payload = json.dumps(result, sort_keys=True)
    print(payload)
    if result["exit_code"]:
        print(payload, file=sys.stderr)
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
