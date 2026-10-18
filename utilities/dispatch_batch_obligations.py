"""Durable observation ownership for registered completion sidecars."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

_ID_RE = re.compile(r"batch-[0-9a-f]{32}\Z")
_MAX_BYTES = 128 * 1024
_PENDING = {"observing", "delivery-pending", "unknown"}


class BatchObligationError(ValueError):
    pass


def _root(jobs: str | Path) -> Path:
    path = Path(jobs).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BatchObligationError("batch-obligation-jobs-invalid")
    canonical = path.resolve(strict=True)
    root = canonical.parent / "supervisor-state" / "obligations"
    if root.exists() and root.is_symlink():
        raise BatchObligationError("batch-obligation-root-symlink")
    return root


def _record_path(jobs: str | Path, duty_id: str) -> Path:
    if not isinstance(duty_id, str) or not _ID_RE.fullmatch(duty_id):
        raise BatchObligationError("batch-obligation-id-invalid")
    return _root(jobs) / (duty_id + ".json")


def _read(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise BatchObligationError("batch-obligation-record-oversized")
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BatchObligationError("batch-obligation-record-invalid") from exc
    if (not isinstance(record, dict) or record.get("schema_version") != 1
            or not isinstance(record.get("identity"), dict)
            or not isinstance(record.get("arguments"), dict)):
        raise BatchObligationError("batch-obligation-record-invalid")
    return record


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise BatchObligationError("batch-obligation-path-symlink")
    fd, temporary = tempfile.mkstemp(prefix=".batch-obligation-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _plain_arguments(args) -> dict:
    output = {}
    for key, value in vars(args).items():
        if key in {"resume_obligation", "ensure_obligations", "obligation_lock_fd",
                   "obligation_parent_pid", "obligation_parent_start", "service_current"}:
            continue
        output[key] = str(value) if isinstance(value, Path) else value
    return output


def create(args) -> tuple[str, dict]:
    jobs = Path(args.jobs).expanduser().resolve(strict=True)
    attempts = sorted(set(args.attempt_id))
    identity = {
        "jobs": str(jobs),
        "sealed_batch_id": args.sealed_batch_id,
        "parent_attempt_id": args.parent_attempt_id or "",
        "parent_session_id": args.parent_session_id or "",
        "attempt_ids": attempts,
        "delivery_parent_id": (args.parent_session_id or args.parent_attempt_id or ""),
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    duty_id = "batch-" + hashlib.sha256(raw).hexdigest()[:32]
    path = _record_path(jobs, duty_id)
    root = path.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    create_lock = root / (duty_id + ".create.lock")
    fd = os.open(create_lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = _read(path)
        if current is not None:
            if current.get("identity") != identity:
                raise BatchObligationError("batch-obligation-identity-conflict")
            return duty_id, current
        now = time.time()
        record = {
            "schema_version": 1,
            "id": duty_id,
            "identity": identity,
            "arguments": _plain_arguments(args),
            "state": "observing",
            "reason": "accepted",
            "accepted_at": now,
            "updated_at": now,
            "effective_attempts": attempts,
            "replacement_lineage": [],
            "outcome": None,
            "delivery": "pending",
            "observer": None,
        }
        _write(path, record)
        return duty_id, record
    finally:
        os.close(fd)


def read(jobs: str | Path, duty_id: str) -> dict | None:
    return _read(_record_path(jobs, duty_id))


def update(jobs: str | Path, duty_id: str, **changes) -> dict:
    path = _record_path(jobs, duty_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(".update.lock")
    if lock_path.is_symlink():
        raise BatchObligationError("batch-obligation-update-lock-symlink")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        record = _read(path)
        if record is None:
            raise BatchObligationError("batch-obligation-missing")
        if changes.get("outcome") not in (None, record.get("outcome")) and record.get("outcome") is not None:
            raise BatchObligationError("batch-obligation-outcome-conflict")
        record.update(changes)
        record["updated_at"] = time.time()
        _write(path, record)
        return record
    finally:
        os.close(fd)


def acquire(jobs: str | Path, duty_id: str, inherited_fd: int | None = None) -> int | None:
    root = _root(jobs)
    lock_path = root / (duty_id + ".lease")
    if inherited_fd is not None:
        try:
            return inherited_fd if _fd_holds_lease(inherited_fd, lock_path) else None
        except (OSError, ValueError, TypeError):
            return None
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise BatchObligationError("batch-obligation-lease-symlink")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not _fd_holds_lease(fd, lock_path):
            release(fd)
            return None
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def _fd_holds_lease(fd: int, lock_path: str | Path) -> bool:
    """Prove this descriptor names the current lease inode and carries its flock."""
    path = Path(lock_path)
    try:
        if path.is_symlink():
            return False
        expected = path.resolve(strict=True)
        target = Path(os.path.realpath(os.readlink(f"/proc/self/fd/{fd}")))
        descriptor = os.fstat(fd)
        current = os.stat(expected, follow_symlinks=False)
        if (target != expected or not stat.S_ISREG(descriptor.st_mode)
                or (descriptor.st_dev, descriptor.st_ino)
                != (current.st_dev, current.st_ino)):
            return False
        device = f"{os.major(current.st_dev):02x}:{os.minor(current.st_dev):02x}"
        inode = str(current.st_ino)
        fdinfo = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii")
        return any(
            len(fields := line.split()) >= 7
            and fields[0] == "lock:"
            and fields[2] == "FLOCK"
            and fields[3] == "ADVISORY"
            and fields[4] == "WRITE"
            and fields[6] == f"{device}:{inode}"
            for line in fdinfo.splitlines()
        )
    except (OSError, ValueError, TypeError):
        return False


def observer_owns_current(jobs: str | Path, duty_id: str, fd: int | None) -> bool:
    """Check the live lease and exact durable PID/start owner for this process."""
    if fd is None:
        return False
    lock_path = _root(jobs) / (duty_id + ".lease")
    if not _fd_holds_lease(fd, lock_path):
        return False
    try:
        record = read(jobs, duty_id)
        observer = record.get("observer") if record else None
        start = _pid_start(os.getpid())
        return (isinstance(observer, dict)
                and observer.get("pid") == os.getpid()
                and isinstance(start, str) and start.isdecimal()
                and observer.get("pid_start") == start)
    except (OSError, ValueError, TypeError, BatchObligationError):
        return False


def update_for_observer(jobs: str | Path, duty_id: str, fd: int,
                        *, claim: bool = False,
                        expected_observer: dict | None = None, **changes) -> dict | None:
    """Update one duty only while this process still owns its exact lease row."""
    path = _record_path(jobs, duty_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    update_lock = path.with_suffix(".update.lock")
    if update_lock.is_symlink():
        raise BatchObligationError("batch-obligation-update-lock-symlink")
    lock_fd = os.open(update_lock, os.O_RDWR | os.O_CREAT
                      | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if not _fd_holds_lease(fd, _root(jobs) / (duty_id + ".lease")):
            return None
        record = _read(path)
        if record is None or record.get("state") not in _PENDING:
            return None
        owner = {"pid": os.getpid(), "pid_start": _pid_start(os.getpid())}
        if not owner["pid_start"].isdecimal():
            return None
        if not claim and record.get("observer") != owner:
            return None
        if (claim and expected_observer is not None
                and record.get("observer") not in (expected_observer, owner)):
            return None
        record.update(changes)
        if claim:
            record["observer"] = owner
        record["updated_at"] = time.time()
        _write(path, record)
        return record
    finally:
        os.close(lock_fd)


def release(fd: int | None, expected_path: str | Path | None = None) -> None:
    if fd is None:
        return
    if expected_path is not None and not _fd_references_path(fd, expected_path):
        return
    try:
        if expected_path is None or _fd_holds_lease(fd, expected_path):
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _fd_references_path(fd: int, expected_path: str | Path) -> bool:
    try:
        path = Path(expected_path)
        if path.is_symlink():
            return False
        expected = path.resolve(strict=True)
        target = Path(os.path.realpath(os.readlink(f"/proc/self/fd/{fd}")))
        descriptor = os.fstat(fd)
        current = os.stat(expected, follow_symlinks=False)
        return (target == expected and stat.S_ISREG(descriptor.st_mode)
                and (descriptor.st_dev, descriptor.st_ino)
                == (current.st_dev, current.st_ino))
    except (OSError, ValueError, TypeError):
        return False


def _jobs_paths(jobs: str | Path | None, *, strict: bool = True) -> list[Path]:
    if jobs:
        path = Path(jobs).expanduser()
        return [path.resolve(strict=True) if strict else Path(os.path.abspath(path))]
    from dispatch_contract import dispatch_state_roots, resolve_agent_home
    return [Path(root) / "jobs.log" for root in dispatch_state_roots(resolve_agent_home())]


def _observer_root(jobs: str | Path) -> Path | None:
    """Find an obligation root without requiring its compatibility registry.

    Writers continue to use ``_root`` and its strict existing-registry check.
    Read-only observers can still see durable records after jobs.log disappears;
    a missing registry and missing sidecar tree are known-empty, while unsafe or
    unreadable paths remain unknown to callers.
    """
    path = Path(jobs).expanduser()
    if not path.is_absolute():
        raise BatchObligationError("batch-obligation-jobs-invalid")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        try:
            parent = path.parent.resolve(strict=True)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BatchObligationError("batch-obligation-jobs-unavailable") from exc
        root = parent / "supervisor-state" / "obligations"
    except OSError as exc:
        raise BatchObligationError("batch-obligation-jobs-unavailable") from exc
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise BatchObligationError("batch-obligation-jobs-invalid")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            os.close(fd)
            parent = path.resolve(strict=True).parent
        except OSError as exc:
            raise BatchObligationError("batch-obligation-jobs-unavailable") from exc
        root = parent / "supervisor-state" / "obligations"

    for directory in (root.parent, root):
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BatchObligationError("batch-obligation-root-unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise BatchObligationError("batch-obligation-root-invalid")
    return root


def _read_only_records(jobs: str | Path) -> tuple[tuple[Path, dict | None, Exception | None], ...]:
    """Enumerate records with one shared absent-versus-unknown read policy."""
    root = _observer_root(jobs)
    if root is None:
        return ()
    try:
        with os.scandir(root) as entries:
            paths = sorted(root / entry.name for entry in entries
                           if entry.name.startswith("batch-") and entry.name.endswith(".json"))
    except OSError as exc:
        raise BatchObligationError("batch-obligation-root-unavailable") from exc
    records = []
    for path in paths:
        try:
            records.append((path, _read(path), None))
        except (OSError, ValueError, BatchObligationError) as exc:
            records.append((path, None, exc))
    return tuple(records)


def _proc_lifetime(pid):
    """Return (state, start) without treating unreadable /proc as process death."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return "gone", None
    except (OSError, ValueError, TypeError):
        return "unknown", None
    try:
        fields = raw[raw.rfind(")") + 2:].split()
        if fields[0] in {"Z", "X"}:
            return "gone", None
        return "live", fields[19]
    except (IndexError, ValueError):
        return "unknown", None


def _parent_duty_has_recipient(record: dict, jobs: Path) -> bool:
    """Keep a stopped duty dormant until its exact parent or bound successor returns."""
    identity = record.get("identity") or {}
    parent = identity.get("parent_session_id") or ""
    if not parent:
        return True
    binding = record.get("parent_lifetime")
    if not isinstance(binding, dict):
        binding = (record.get("arguments") or {}).get("parent_lifetime")
    if not isinstance(binding, dict):
        return True
    try:
        from dispatch_contract import process_namespace_identity
        namespace = binding.get("pid_namespace")
        if not namespace or process_namespace_identity() != namespace:
            return True
        pid = int(binding.get("pid"))
        start = str(binding.get("pid_start") or "")
    except (ImportError, OSError, TypeError, ValueError):
        return True
    state, observed_start = _proc_lifetime(pid)
    if state == "unknown":
        return True
    if state == "live" and observed_start == start:
        try:
            from pane_ownership import _native_session
            current = _native_session(pid, "codex")
        except Exception:
            return True
        if not current:
            return True
        if current == parent:
            return True

    # A replacement TUI may carry the original native thread under a new PID,
    # so confirm that case using the same pane ownership contract.
    try:
        from pane_ownership import codex_tui_processes_for_session
        bindings = codex_tui_processes_for_session(parent)
    except Exception:
        return True
    if bindings:
        return True

    # The original parent may have handed these exact attempts to a new seat.
    # Keep the observer available for that durable recipient even if it has not
    # started a TUI process yet.
    try:
        import dispatch_seat_handover as handover
        rows = handover.latest_rows(jobs)
        attempts = record.get("effective_attempts") or identity.get("attempt_ids") or []
        for attempt in attempts:
            row = rows.get(attempt)
            if row is None:
                continue
            recipient = handover.effective_parent(row[1], jobs)
            if recipient and recipient != parent:
                return True
    except Exception:
        return True
    return False


def ensure_observers(jobs: str | Path | None = None, *, service_current=None,
                     observer_snapshot=None, settlement_context=None) -> int:
    """Resume unfinished exact batches through the existing completion sidecar."""
    launched = 0
    import peer_obligations
    script, child_env, binding = peer_obligations.service_script(
        "utilities/codex-managed-completion.py", service_current)
    jobs_paths = _jobs_paths(jobs)
    settlement_signatures = []
    # Discovery returns the canonical writer first, followed by compatibility
    # roots that are read-only under dispatch_state_roots(). Owner settlement
    # mutates the registry, so only the explicitly selected path (or the
    # canonical discovered path at index 0) may reach that writer. Sidecar
    # discovery below still visits every root independently.
    for jobs_path in jobs_paths[:1]:
        try:
            settlement_signatures.extend(
                _resume_owner_settlements(jobs_path, context=settlement_context))
        except (OSError, UnicodeError):
            # Keep this root's sidecar scan and later independent roots reachable.
            settlement_signatures.append((str(jobs_path), "owner-settlement-unavailable"))
            continue
    if observer_snapshot is not None and settlement_signatures:
        observer_snapshot["signature"] = tuple(sorted(
            (*observer_snapshot.get("signature", ()), *settlement_signatures)))
    for jobs_path in jobs_paths:
        try:
            root = _root(jobs_path)
            snapshot_paths = None
            if observer_snapshot is not None:
                key = str(jobs_path.resolve(strict=False))
                snapshot_paths = observer_snapshot.get("paths_by_jobs", {}).get(key)
            records = (sorted(root.glob("batch-*.json")) if snapshot_paths is None
                       else list(snapshot_paths)) if root.is_dir() else []
        except (OSError, BatchObligationError):
            continue
        for path in records:
            try:
                if path.parent.resolve(strict=True) != root.resolve(strict=True):
                    continue
                # A per-pass snapshot selects candidates only. Re-read the
                # exact durable row before acquiring or replacing its owner.
                record = _read(path)
                if not record or record.get("state") not in _PENDING:
                    continue
                if (record.get("state") == "unknown"
                        and record.get("reason") == "parent-session-ended"
                        and not _parent_duty_has_recipient(record, jobs_path)):
                    continue
                duty_id = record["id"]
                fd = acquire(jobs_path, duty_id)
                if fd is None:
                    observer = record.get("observer") or {}
                    if (not isinstance(observer, dict) or not binding
                            or not observer.get("pid")):
                        continue
                    if "pid_start" in observer:
                        observer_start = observer["pid_start"]
                        if (not isinstance(observer_start, str)
                                or not observer_start.isdecimal()):
                            continue
                    else:
                        observer_start = None
                    replaced = peer_obligations.replace_managed_observer(
                        observer["pid"], observer_start,
                        root / (duty_id + ".lease"),
                        "utilities/codex-managed-completion.py",
                        ("--jobs", str(jobs_path), "--resume-obligation", duty_id),
                        binding, lock_fd_option="--obligation-lock-fd",
                        exact_arguments=True,
                    )
                    if replaced != "replaced":
                        continue
                    fd = acquire(jobs_path, duty_id)
                    if fd is None:
                        continue
                os.set_inheritable(fd, True)
                command = [
                    sys.executable, str(script), "--jobs", str(jobs_path),
                    "--resume-obligation", duty_id,
                    "--obligation-lock-fd", str(fd),
                ]
                if binding:
                    command.extend(("--service-current", binding))
                lease_path = root / (duty_id + ".lease")
                process = None
                try:
                    process = subprocess.Popen(
                        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True, pass_fds=(fd,),
                        start_new_session=True, cwd=str(jobs_path.parent), env=child_env,
                    )
                    update(jobs_path, duty_id, observer={
                        "pid": process.pid,
                        "pid_start": _pid_start(process.pid),
                    })
                    launched += 1
                except (OSError, subprocess.SubprocessError, BatchObligationError):
                    if process is None:
                        release(fd, lease_path)
                    else:
                        # The child inherited this open file description. Closing
                        # the parent's copy must not unlock the child's lease.
                        os.close(fd)
                else:
                    # flock state belongs to the shared open file description;
                    # closing this copy preserves the child's lock.
                    os.close(fd)
            except (OSError, ValueError, BatchObligationError):
                continue
    return launched


def handoff_observer(jobs: str | Path, duty_id: str, fd: int, *, service_current=None) -> bool:
    """Pass a pending duty to one successor while this process still holds its lease."""
    if not observer_owns_current(jobs, duty_id, fd):
        return False
    record = read(jobs, duty_id)
    if not record or record.get("state") not in _PENDING:
        return False
    parent = {"pid": os.getpid(), "pid_start": _pid_start(os.getpid())}
    if not parent["pid_start"].isdecimal():
        return False
    try:
        import peer_obligations
        script, child_env, binding = peer_obligations.service_script(
            "utilities/codex-managed-completion.py", service_current)
        jobs_path = Path(jobs).resolve(strict=True)
        command = [
            sys.executable, str(script), "--jobs", str(jobs_path),
            "--resume-obligation", duty_id,
            "--obligation-lock-fd", str(fd),
            "--obligation-parent-pid", str(parent["pid"]),
            "--obligation-parent-start", parent["pid_start"],
        ]
        if binding:
            command.extend(("--service-current", binding))
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, pass_fds=(fd,),
            start_new_session=True, cwd=str(jobs_path.parent), env=child_env,
        )
    except (OSError, subprocess.SubprocessError, BatchObligationError):
        return False

    replacement = {"pid": process.pid, "pid_start": _pid_start(process.pid)}
    try:
        updated = update_for_observer(
            jobs, duty_id, fd, observer=replacement)
        if updated is None:
            current = read(jobs, duty_id) or {}
            if current.get("observer") != replacement:
                return False
    except (OSError, BatchObligationError):
        # The child can claim the row only if it still names this parent (or
        # the child itself) and the inherited lease remains live.
        pass
    return True


def _registry_stamp(path: Path):
    try:
        info = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory_stamp(path: Path):
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


class OwnerSettlementReadContext:
    """Reuse unchanged settlement discovery while rechecking observed leases."""

    def __init__(self):
        self._seen = {}

    def needs_scan(self, jobs: Path, lease_is_held) -> bool:
        key = str(jobs.resolve(strict=False))
        prior = self._seen.get(key)
        stamp = _registry_stamp(jobs)
        if prior is None or stamp is None or stamp != prior[0]:
            return True
        for metadata in prior[1]:
            try:
                if not lease_is_held(jobs, metadata):
                    return True
            except Exception:
                return True
        return False

    def cached_signatures(self, jobs: Path):
        prior = self._seen.get(str(jobs.resolve(strict=False)))
        return () if prior is None else prior[2]

    def remember(self, jobs: Path, before_stamp, live_leases, retry_required=False,
                 signatures=()):
        key = str(jobs.resolve(strict=False))
        after_stamp = _registry_stamp(jobs)
        if retry_required or before_stamp is None or after_stamp != before_stamp:
            self._seen.pop(key, None)
            return
        self._seen[key] = (after_stamp, tuple(live_leases), tuple(signatures))


def _pending_owner_settlements(jobs: Path):
    """Return exact owner rows whose existing terminal path still owes work."""
    from dispatch_contract import (
        SUPERVISOR_LEASE_KIND, parse_registry_metadata, supervisor_lease_is_held,
    )
    from dispatch_terminal_commit import owner_completion_pending

    try:
        from dispatch_registry_cache import registry_view
        candidates = registry_view(jobs).settlement_owner_candidates()
    except FileNotFoundError:
        return ()
    pending = []
    for candidate in candidates:
        order, raw, fields = candidate.order, candidate.raw, candidate.fields
        if fields[1] not in {"open", "running", "done"}:
            continue
        meta = candidate.completion_metadata()
        if (meta.get("worker_type") != "owner" or meta.get("launch_started") != "1"
                or meta.get("supervisor_lease") != SUPERVISOR_LEASE_KIND
                or not meta.get("supervisor_lease_file") or not meta.get("log_file")
                or not meta.get("attempt_id")):
            continue
        if fields[1] == "done":
            if meta.get("workflow_completion") != "runtime-v1":
                continue
            try:
                if not owner_completion_pending(jobs, fields[1], meta):
                    continue
            except Exception:
                # An unavailable observation remains a retryable obligation.
                pass
        try:
            lease_held = supervisor_lease_is_held(jobs, meta)
        except (OSError, ValueError):
            lease_held = True
        pending.append((order, fields[1], raw, meta, lease_held))
    return tuple(pending)


def _resume_owner_settlements(jobs: Path, *, context=None) -> None:
    """The open registry row retains a failed supervisor settlement itself.

    Resume through the same writer on ordinary reconnection, including older
    owners with no batch sidecar record. Storage failure keeps it open for
    the next existing callback; this never launches model work.
    """
    from dispatch_contract import supervisor_lease_is_held
    from dispatch_completion_join import (
        ChildRow, materialize_after_terminal_close, settle_finished_attempt,
    )
    lease_is_held = supervisor_lease_is_held
    if context is not None and not context.needs_scan(jobs, lease_is_held):
        return context.cached_signatures(jobs)
    before_stamp = _registry_stamp(jobs)
    try:
        pending = _pending_owner_settlements(jobs)
    except (OSError, UnicodeError):
        if context is not None:
            context.remember(jobs, before_stamp, (), retry_required=True)
        return ((str(jobs), "owner-settlement-unavailable"),)
    live_leases = []
    retry_required = False
    signatures = []
    for order, status, raw, meta, lease_held in pending:
        if lease_held:
            live_leases.append(meta)
            if status == "done":
                signatures.append((str(jobs), "owner-settlement",
                                   meta.get("attempt_id", ""), status,
                                   meta.get("workflow_completion", ""),
                                   meta.get("note", "")))
            continue
        try:
            settlement_stamp = _registry_stamp(jobs)
            if status == "done":
                materialize_after_terminal_close(jobs, meta["attempt_id"])
                from dispatch_terminal_commit import owner_completion_pending
                try:
                    if owner_completion_pending(jobs, status, meta):
                        retry_required = True
                        signatures.append((str(jobs), "owner-settlement",
                                           meta.get("attempt_id", ""), status,
                                           meta.get("workflow_completion", ""),
                                           meta.get("note", "")))
                except Exception:
                    retry_required = True
                    signatures.append((str(jobs), "owner-settlement",
                                       meta.get("attempt_id", ""), status,
                                       meta.get("workflow_completion", ""),
                                       meta.get("note", "")))
            else:
                result = settle_finished_attempt(
                    jobs, ChildRow(order, status, raw.split("\t")[4],
                                   meta["attempt_id"], raw, meta))
                if not isinstance(result, dict) or not result.get("closed"):
                    signatures.append((str(jobs), "owner-settlement",
                                       meta.get("attempt_id", ""), status,
                                       meta.get("workflow_completion", ""),
                                       meta.get("note", "")))
            if _registry_stamp(jobs) == settlement_stamp:
                retry_required = True
        except (OSError, UnicodeError, ValueError, RuntimeError):
            retry_required = True
            signatures.append((str(jobs), "owner-settlement",
                               meta.get("attempt_id", ""), status,
                               meta.get("workflow_completion", ""),
                               meta.get("note", "")))
            continue
    if context is not None:
        context.remember(jobs, before_stamp, live_leases, retry_required, signatures)
    return tuple(signatures)


def has_unowned_work(jobs: str | Path | None = None) -> bool:
    """Read-only check for batch or owner settlement work without a live owner."""
    for jobs_path in _jobs_paths(jobs, strict=False):
        try:
            owner_settlements = _pending_owner_settlements(jobs_path)
        except (OSError, UnicodeError):
            return True
        if any(not lease_held for _order, _status, _raw, _meta, lease_held in owner_settlements):
            return True

        try:
            records = _read_only_records(jobs_path)
        except (OSError, UnicodeError, BatchObligationError):
            return True
        for _path, record, error in records:
            if error is not None:
                return True
            if not record or record.get("state") not in _PENDING:
                continue
            observer = record.get("observer") or {}
            pid = observer.get("pid")
            start = observer.get("pid_start")
            if (not isinstance(pid, int) or not isinstance(start, str) or not start
                    or _pid_start(pid) != start):
                return True
    return False


def pending_observer_snapshot(jobs: str | Path | None = None, *,
                              include_owner_settlements: bool = False):
    """Read one pass of pending records for reconciliation and idle signature.

    This is read-only liveness input. It carries no delivery or execution
    authority and deliberately excludes sealed arguments and route identity.
    ``paths_by_jobs`` only selects candidates; ``ensure_observers`` rereads each
    selected record before acquiring its lease or replacing an observer.
    """
    signature = []
    paths_by_jobs = {}
    roots_by_jobs = {}
    root_stamps = {}
    cacheable = True
    for jobs_path in _jobs_paths(jobs, strict=False):
        key = str(jobs_path.resolve(strict=False))
        root_path = jobs_path.resolve(strict=False).parent / "supervisor-state" / "obligations"
        roots_by_jobs[key] = root_path
        before_stamp = _directory_stamp(root_path)
        unavailable = False
        owner_settlements = ()
        if include_owner_settlements:
            try:
                owner_settlements = _pending_owner_settlements(jobs_path)
            except (OSError, UnicodeError):
                signature.append((str(jobs_path), "unavailable"))
                unavailable = True
                cacheable = False
        try:
            records = _read_only_records(jobs_path)
        except (OSError, UnicodeError, BatchObligationError):
            if not unavailable:
                signature.append((str(jobs_path), "unavailable"))
            paths_by_jobs[key] = None
            root_stamps[key] = _directory_stamp(root_path)
            cacheable = False
            continue
        after_stamp = _directory_stamp(root_path)
        root_stamps[key] = after_stamp
        if before_stamp is None or before_stamp != after_stamp:
            cacheable = False
        for _order, status, _raw, meta, lease_held in owner_settlements:
            if not lease_held:
                signature.append((str(jobs_path), "owner-settlement",
                                  meta.get("attempt_id", ""), status,
                                  meta.get("workflow_completion", ""),
                                  meta.get("note", "")))
        pending_paths = []
        for path, record, error in records:
            if error is not None:
                signature.append((str(jobs_path), path.name, "unreadable"))
                pending_paths.append(path)
                cacheable = False
                continue
            if not record or record.get("state") not in _PENDING:
                continue
            pending_paths.append(path)
            observer = record.get("observer") or {}
            signature.append((
                str(jobs_path), record.get("id", path.stem), record.get("state"),
                record.get("delivery"), observer.get("pid"),
                observer.get("pid_start"),
            ))
        paths_by_jobs[key] = tuple(pending_paths)
    return {
        "signature": tuple(sorted(signature)), "paths_by_jobs": paths_by_jobs,
        "roots_by_jobs": roots_by_jobs, "root_stamps": root_stamps,
        "cacheable": cacheable,
    }


def pending_observer_signature(jobs: str | Path | None = None) -> tuple[tuple, ...]:
    """Describe pending completion duties without granting mutation authority."""
    return pending_observer_snapshot(jobs, include_owner_settlements=True)["signature"]


def _pid_start(pid: int) -> str:
    try:
        from dispatch_contract import process_start_ticks
        return str(process_start_ticks(pid) or "")
    except Exception:
        return ""
