#!/usr/bin/env python3
"""Route interactive Codex CLI surfaces through the managed App Server entry."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


PASSTHROUGH_COMMANDS = {
    "app",
    "app-server",
    "apply",
    "a",
    "archive",
    "cloud",
    "completion",
    "debug",
    "delete",
    "doctor",
    "e",
    "exec",
    "exec-server",
    "features",
    "help",
    "login",
    "logout",
    "mcp",
    "mcp-server",
    "plugin",
    "remote-control",
    "review",
    "sandbox",
    "unarchive",
    "update",
}
INTERACTIVE_COMMANDS = {"resume", "fork"}
VALUE_OPTIONS = {
    "-a",
    "--add-dir",
    "--ask-for-approval",
    "-c",
    "--cd",
    "--config",
    "-C",
    "--disable",
    "--enable",
    "-i",
    "--image",
    "--local-provider",
    "-m",
    "--model",
    "-p",
    "--profile",
    "--remote",
    "--remote-auth-token-env",
    "-s",
    "--sandbox",
}
PASSTHROUGH_FLAGS = {"-h", "--help", "-V", "--version"}

# Approval/sandbox posture the caller may have selected for itself. Any of these means
# the invocation already carries an explicit stance and the default must not touch it.
# `-p/--profile` is in the set because a profile is a user-authored config layer that may
# pin `approval_policy`/`sandbox_mode`; deferring to it costs a default and never widens
# access, which is the direction to be wrong in.
POSTURE_FLAGS = {
    "-s",
    "--sandbox",
    "-a",
    "--ask-for-approval",
    "--approve-for-me",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "-p",
    "--profile",
}
POSTURE_CONFIG_KEYS = ("approval_policy", "sandbox_mode", "sandbox_permissions")
BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"


class LauncherError(RuntimeError):
    """Installed launcher state is unsafe or incomplete."""


_LOCK_OPEN_RETRIES = 5


def _open_harness_dirfd(home: Path) -> int:
    """Open `.harness` via a stable, no-follow directory descriptor.

    `home` is already checked by the caller with path operations; opening
    `.harness` itself as a descriptor (rather than a path string that a later
    `os.open(lock_path, ...)` re-walks) means every subsequent lock operation
    resolves against the exact directory validated here. `O_NOFOLLOW` alone
    only protects the final pathname component *at that one open call* -- a
    rename of `.harness` followed by a replacement directory or symlink
    between this check and a later path-based open would otherwise let the
    lock open land somewhere other than the directory that was checked.
    """
    harness_dir = home / ".harness"
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        dirfd = os.open(harness_dir, flags)
    except OSError as exc:
        raise LauncherError(f"managed launcher state directory is unsafe: {harness_dir}") from exc
    try:
        info = os.fstat(dirfd)
    except BaseException:
        os.close(dirfd)
        raise
    if not stat.S_ISDIR(info.st_mode):
        os.close(dirfd)
        raise LauncherError(f"managed launcher state directory is unsafe: {harness_dir}")
    return dirfd


def _lock_identity(dirfd: int):
    """`os.stat` the lock name relative to `dirfd`, or `None` if it is gone."""
    try:
        return os.stat("codex-launcher.lock", dir_fd=dirfd, follow_symlinks=False)
    except OSError:
        return None


def _launcher_lock(home: Path, *, dirfd: int | None = None):
    """Acquire a shared read-only lock on the installer-managed lock file.

    Never creates, writes to, chmods, or unlinks the lock; a missing or unsafe
    lock is a hard failure (fail-closed), not something this reader repairs.
    Only a *replaced* pathname (the installer republishes the lock atomically
    during install/repair) is retried, bounded by `_LOCK_OPEN_RETRIES`, so a
    stale descriptor can never be mistaken for the live lock; every other
    unsafe condition raises immediately rather than looping. Blocking on
    `LOCK_SH` (rather than failing fast while the installer holds `LOCK_EX`)
    is acceptable here: the installer's own write window is short and bounded,
    and a launch that cannot read a consistent binding should wait for it
    rather than race an in-progress install/repair.

    The lock itself is opened and identity-checked relative to a single
    `.harness` directory descriptor held for the whole call (see
    `_open_harness_dirfd`), so a directory-level replacement cannot slip a
    foreign lock in after the directory was validated; only the final lock
    name is retried across replacement, never the directory.
    """
    if home.is_symlink() or not home.is_dir():
        raise LauncherError(f"managed CODEX_HOME is unsafe: {home}")
    if fcntl is None:
        raise LauncherError("Codex launcher lock support (fcntl) is unavailable")
    owns_dirfd = dirfd is None
    if owns_dirfd:
        dirfd = _open_harness_dirfd(home)
    try:
        path = home / ".harness" / "codex-launcher.lock"
        # O_NONBLOCK is required at open time: without it, opening a FIFO
        # planted at the lock path for O_RDONLY blocks the launcher
        # indefinitely waiting for a writer, turning a rejection case into a
        # hang. It is cleared again once the descriptor is confirmed to be a
        # regular file (regular-file reads are unaffected by O_NONBLOCK
        # either way, but callers should not observe nonblocking semantics on
        # the returned handle).
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        for _ in range(_LOCK_OPEN_RETRIES):
            try:
                fd = os.open("codex-launcher.lock", flags, dir_fd=dirfd)
            except OSError as exc:
                raise LauncherError(f"Codex launcher lock is unavailable: {path}") from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise LauncherError(f"Codex launcher lock is unsafe: {path}")
                handle = os.fdopen(fd, "rb", buffering=0)
            except BaseException:
                os.close(fd)
                raise
            valid = False
            try:
                opened = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.geteuid()
                    or opened.st_mode & 0o077
                ):
                    raise LauncherError(f"Codex launcher lock is unsafe: {path}")
                current_status_flags = fcntl.fcntl(handle.fileno(), fcntl.F_GETFL)
                fcntl.fcntl(handle.fileno(), fcntl.F_SETFL, current_status_flags & ~os.O_NONBLOCK)
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                current = _lock_identity(dirfd)
                if (
                    current is not None
                    and stat.S_ISREG(current.st_mode)
                    and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)
                ):
                    valid = True
                    return handle
                # Pathname was replaced (or unlinked) between open and flock: this
                # descriptor is stale. Release and retry against the current name,
                # still relative to the same validated `.harness` directory.
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                if not valid:
                    handle.close()
        raise LauncherError(f"Codex launcher lock could not be safely acquired: {path}")
    finally:
        if owns_dirfd:
            os.close(dirfd)


def _unlock(handle) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _read_state_locked(home: Path) -> dict:
    """Acquire the shared lock and read launcher state while holding it.

    `_launcher_lock` proves the lock identity at acquire time, but the
    pathname can still be replaced between that return and the state read
    below -- the reader would then hold a shared lock on a stale inode while
    an installer serializes writers against the successor. Acceptance is
    made to cover the read: identity is re-checked immediately after
    `_state` returns, and any replacement retries the whole
    acquire-and-read sequence (same validated directory, fresh lock) within
    the bounded `_LOCK_OPEN_RETRIES` budget rather than trusting a read
    that raced a live install/repair.
    """
    if home.is_symlink() or not home.is_dir():
        raise LauncherError(f"managed CODEX_HOME is unsafe: {home}")
    dirfd = _open_harness_dirfd(home)
    try:
        for _ in range(_LOCK_OPEN_RETRIES):
            handle = None
            try:
                handle = _launcher_lock(home, dirfd=dirfd)
                pre = os.fstat(handle.fileno())
                value = _state(home, dirfd=dirfd)
                post = _lock_identity(dirfd)
                if (
                    post is not None
                    and stat.S_ISREG(post.st_mode)
                    and (pre.st_dev, pre.st_ino) == (post.st_dev, post.st_ino)
                ):
                    return value
                # Replaced during the read window: the state we just read cannot
                # be trusted as bound to the lock we held. Retry from scratch.
            finally:
                if handle is not None:
                    _unlock(handle)
    finally:
        os.close(dirfd)
    raise LauncherError(f"managed launcher state could not be safely read: {home}")


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    home = Path(raw).expanduser() if raw else Path.home() / ".codex"
    return home.absolute()


def launcher_state_home(runtime_home: Path) -> Path:
    """Resolve the global CLI binding without hijacking a private CODEX_HOME."""

    runtime_state = runtime_home / ".harness" / "codex-launcher.json"
    if runtime_state.is_file() and not runtime_state.is_symlink():
        return runtime_home
    default_home = (Path.home() / ".codex").absolute()
    default_state = default_home / ".harness" / "codex-launcher.json"
    if default_home != runtime_home and default_state.is_file() and not default_state.is_symlink():
        return default_home
    return runtime_home


def _state(home: Path, *, dirfd: int | None = None) -> dict:
    if home.is_symlink() or not home.is_dir():
        raise LauncherError(f"managed CODEX_HOME is unsafe: {home}")
    harness_state = home / ".harness"
    owns_dirfd = dirfd is None
    if owns_dirfd:
        if harness_state.is_symlink() or not harness_state.is_dir():
            raise LauncherError(f"managed launcher state directory is unsafe: {harness_state}")
        dirfd = _open_harness_dirfd(home)
    path = harness_state / "codex-launcher.json"
    fd = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            fd = os.open("codex-launcher.json", flags, dir_fd=dirfd)
        except OSError as exc:
            raise LauncherError(f"managed launcher state is unavailable: {path}") from exc
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise LauncherError(f"managed launcher state is unavailable: {path}") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > 32_768
        ):
            raise LauncherError(f"managed launcher state is unavailable: {path}")
        try:
            handle = os.fdopen(fd, "rb", buffering=0)
        except BaseException:
            os.close(fd)
            fd = None
            raise
        fd = None
        try:
            raw = bytearray()
            while len(raw) <= 32_768:
                chunk = handle.read(32_769 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > 32_768:
                raise LauncherError(f"managed launcher state is unavailable: {path}")
            value = json.loads(bytes(raw).decode("utf-8"))
        except LauncherError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LauncherError(f"managed launcher state is invalid: {path}") from exc
        finally:
            handle.close()
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        if owns_dirfd:
            os.close(dirfd)
    if not isinstance(value, dict) or value.get("schema") not in {1, 2} or value.get("phase") != "installed":
        raise LauncherError(f"managed launcher state is incomplete: {path}")
    real = Path(str(value.get("real_command") or value.get("vendor_binding", {}).get("command_path", "")))
    if not real.is_absolute() or not real.exists() or not os.access(real, os.X_OK):
        raise LauncherError(f"real Codex command is unavailable: {real}")
    if _is_harness_wrapper(real):
        raise LauncherError(
            f"real Codex command resolves to an hearting launcher wrapper: {real}"
        )
    value["real_command"] = str(real)
    ingress = Path(str(value.get("ingress_path") or value.get("wrapper_path", "")))
    if not ingress.is_absolute() or ingress.name != "codex":
        raise LauncherError("managed launcher ingress path is invalid")
    try:
        if ingress.resolve(strict=False).parent == home.resolve(strict=False) / ".harness" / "bin":
            pass
    except (OSError, RuntimeError) as exc:
        raise LauncherError("managed launcher ingress path is invalid") from exc
    return value


def _read_private_file(path: Path, limit: int, unavailable: str, unsafe: str) -> str:
    """Read activation metadata and bytes from one nonblocking descriptor."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise LauncherError(unavailable) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise LauncherError(unavailable)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise LauncherError(unsafe)
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, limit + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit:
            raise LauncherError(unavailable)
        return raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise LauncherError(unavailable) from exc
    finally:
        os.close(fd)


def pinned_runtime(home: Path) -> dict:
    """Resolve one activation root once for the lifetime of a new session."""

    path = home / ".harness" / "activation.json"
    raw = _read_private_file(
        path,
        2_000_000,
        f"runtime activation state is unavailable: {path}",
        f"runtime activation state permissions are unsafe: {path}",
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LauncherError(f"runtime activation state is invalid: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != 2
        or value.get("runtime") != "codex"
        or value.get("mode") not in {"packaged", "linked"}
    ):
        raise LauncherError(f"runtime activation state is incomplete: {path}")
    declared = Path(str(value.get("active_root", "")))
    if not declared.is_absolute():
        raise LauncherError("runtime activation root is unsafe")
    try:
        active = declared.resolve(strict=True)
        projected = (home / "hearting").resolve(strict=True)
    except OSError as exc:
        raise LauncherError("runtime activation projection is unavailable") from exc
    if projected != active or not (active / "core" / "CORE.md").is_file():
        raise LauncherError("runtime activation projection is inconsistent")
    revision = value.get("active_revision")
    if not isinstance(revision, str) or not revision:
        raise LauncherError("runtime activation revision is missing")
    checksum = value.get("bundle_checksum")
    if value["mode"] == "packaged":
        # A packaged bundle addresses the release by symlink when the activation
        # source was an immutable managed release, so containment and metadata are
        # asserted on the DECLARED bundle path -- what activation wrote -- and the
        # resolved path is only used to prove the tree is really there. Resolving
        # first made a linked bundle read as "escapes bundle storage" and put its
        # `bundle.json` beside the release, which refused every managed codex
        # launch with exit 69.
        bundle_root = (home / ".harness" / "bundles").resolve(strict=False)
        declared_parent = declared.parent
        try:
            declared.parent.parent.resolve(strict=False).relative_to(bundle_root)
        except ValueError as exc:
            raise LauncherError("packaged runtime root escapes bundle storage") from exc
        metadata = declared_parent / "bundle.json"
        try:
            bundle = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LauncherError("packaged runtime metadata is unavailable") from exc
        if (
            not isinstance(checksum, str)
            or not checksum
            or not isinstance(bundle, dict)
            or bundle.get("checksum") != checksum
            or bundle.get("source_revision") != revision
        ):
            raise LauncherError("packaged runtime identity is inconsistent")
    return {
        "active_root": active,
        "mode": value["mode"],
        "revision": revision,
        "identity": f"{value['mode']}:{revision}:{checksum or '-'}",
    }


def export_runtime_binding(binding: dict) -> None:
    os.environ.update(
        {
            "AGENT_HOME": str(binding["active_root"]),
            "AGENT_RUNTIME_ROOT": str(binding["active_root"]),
            "AGENT_RUNTIME_IDENTITY": str(binding["identity"]),
            "AGENT_RUNTIME_ACTIVATION_MODE": str(binding["mode"]),
        }
    )


def _is_harness_wrapper(command: Path) -> bool:
    """A recorded binding must never point at any install's launcher ingress."""
    try:
        if command.is_symlink() or not command.is_file() or command.stat().st_size > 4096:
            return False
        payload = command.read_bytes()
    except OSError:
        return False
    return b"hearting" in payload and b"codex-launcher.py" in payload


def _first_positional(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if value in VALUE_OPTIONS:
            index += 2
            continue
        if any(value.startswith(option + "=") for option in VALUE_OPTIONS if option.startswith("--")):
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        return value
    return None


def should_manage(args: list[str]) -> bool:
    if os.environ.get("AGENT_CODEX_LAUNCHER_BYPASS") == "1":
        return False
    if any(value == "--remote" or value.startswith("--remote=") for value in args):
        return False
    if any(value in {"-h", "--help", "-V", "--version"} for value in args):
        return False
    command = _first_positional(args)
    if command in INTERACTIVE_COMMANDS:
        return True
    if command in PASSTHROUGH_COMMANDS:
        return False
    if command is None and any(value in PASSTHROUGH_FLAGS for value in args):
        return False
    return True


def interactive_permission_mode() -> str:
    """`bypass` (default) or `inherit`, from AGENT_CODEX_INTERACTIVE_PERMISSION_MODE.

    User decision 2026-09-03: a Codex session this harness launches should come up ready
    to work, the same way `peer-steward.py start` already starts a child Codex root with
    the bypass flag and a registered Claude worker starts in `bypassPermissions`. An
    unrecognized value is not a silent third mode — it falls back to the default.
    """
    raw = os.environ.get("AGENT_CODEX_INTERACTIVE_PERMISSION_MODE", "").strip().lower()
    return "inherit" if raw == "inherit" else "bypass"


def selects_own_posture(args: list[str]) -> bool:
    """True when the invocation already states an approval/sandbox stance of its own."""
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            return False
        if value in POSTURE_FLAGS:
            return True
        if any(value.startswith(flag + "=") for flag in POSTURE_FLAGS if flag.startswith("--")):
            return True
        if value in {"-c", "--config"}:
            if index + 1 < len(args) and _is_posture_override(args[index + 1]):
                return True
            index += 2
            continue
        if value.startswith("--config=") and _is_posture_override(value.partition("=")[2]):
            return True
        if value.startswith("-c") and len(value) > 2 and _is_posture_override(value[2:]):
            return True
        if value in VALUE_OPTIONS:
            index += 2
            continue
        index += 1
    return False


def _is_posture_override(override: str) -> bool:
    key = str(override).partition("=")[0].strip()
    return key in POSTURE_CONFIG_KEYS


def apply_interactive_permission_mode(args: list[str]) -> list[str]:
    """Prepend the bypass flag to a managed interactive invocation that wants the default.

    The flag goes in front so it stays a root-level option even when the invocation is
    `resume`/`fork`; `codex-managed-entry.py` forwards these verbatim to the TUI client.
    Only this managed interactive path is affected — `codex exec` and every other
    passthrough subcommand never reach here, so the registered dispatch wrapper's
    `approval_policy=never` plus a real sandbox (stage-dispatch SD-125 (5)) is unchanged.
    """
    if interactive_permission_mode() != "bypass" or selects_own_posture(args):
        return list(args)
    return [BYPASS_FLAG, *args]


def managed_auth_ready(home: Path) -> bool:
    """Let the real CLI own first-login and unsafe-auth remediation."""

    auth = home / "auth.json"
    if auth.is_symlink() or not auth.is_file():
        return False
    info = auth.stat()
    return info.st_uid == os.geteuid() and not info.st_mode & 0o077


def private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise LauncherError(f"managed state directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise LauncherError(f"managed state directory is not owner-controlled: {path}")
    os.chmod(path, 0o700)
    return path


def workspace(args: list[str]) -> Path:
    current = Path.cwd()
    index = 0
    selected: str | None = None
    while index < len(args):
        value = args[index]
        if value in {"-C", "--cd"} and index + 1 < len(args):
            selected = args[index + 1]
            index += 2
            continue
        if value.startswith("--cd="):
            selected = value.partition("=")[2]
        index += 1
    if selected is None:
        return current
    candidate = Path(selected).expanduser()
    return (candidate if candidate.is_absolute() else current / candidate).resolve(strict=False)


def managed_command(
    args: list[str], home: Path, real: Path, binding: dict | None = None
) -> list[str]:
    binding = binding or pinned_runtime(home)
    agent_home = Path(binding["active_root"])
    entry = agent_home / "utilities" / "codex-managed-entry.py"
    if not entry.is_file():
        raise LauncherError(f"managed-entry projection is unavailable: {entry}")
    harness_state = private_directory(home / ".harness")
    state_root = private_directory(harness_state / "managed-sessions")
    session = Path(tempfile.mkdtemp(prefix="session-", dir=str(state_root)))
    os.chmod(session, 0o700)
    dispatch_root = private_directory(harness_state / "dispatch")
    jobs = dispatch_root / "jobs.log"
    return [
        sys.executable,
        str(entry),
        "--codex",
        str(real),
        "--codex-home",
        str(home),
        "--state-dir",
        str(session),
        "--workspace",
        str(workspace(args)),
        "--jobs",
        str(jobs),
        "--",
        *args,
    ]


def main() -> int:
    args = sys.argv[1:]
    runtime_home = _codex_home()
    try:
        # execv keeps the PID, so a circular binding (wrapper -> launcher ->
        # wrapper ...) re-enters this process. Spawned children get new PIDs
        # and are unaffected. Fail fast instead of looping forever.
        guard_pid = os.environ.get("AGENT_CODEX_LAUNCHER_GUARD_PID")
        if guard_pid == str(os.getpid()):
            raise LauncherError(
                "launcher re-entered itself; the recorded real Codex command is circular"
            )
        os.environ["AGENT_CODEX_LAUNCHER_GUARD_PID"] = str(os.getpid())
        state_home = launcher_state_home(runtime_home)
        value = _read_state_locked(state_home)
        real = Path(value["real_command"])
        # A global launcher may be used with a one-off CODEX_HOME for tests,
        # repair, or an administrative command. Its global binding remains
        # usable, but only a home with its own launcher state may become a
        # managed interactive parent.
        if state_home == runtime_home and should_manage(args) and managed_auth_ready(runtime_home):
            binding = pinned_runtime(runtime_home)
            export_runtime_binding(binding)
            command = managed_command(
                apply_interactive_permission_mode(args), runtime_home, real, binding
            )
        else:
            command = [str(real), *args]
        os.execv(command[0], command)
    except (LauncherError, OSError) as exc:
        print(f"hearting: Codex launcher failed: {exc}", file=sys.stderr)
        return 69


if __name__ == "__main__":
    raise SystemExit(main())
