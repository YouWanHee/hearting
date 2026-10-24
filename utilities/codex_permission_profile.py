"""Project existing Git commit grants into native Codex permissions.

Linked worktrees project their named-profile common Git grant; a primary checkout gets
a commit-only `.git` grant for the same callers (`primary_commit`). No user
config is written. Runtimes without named profiles keep their existing
legacy sandbox projection; discovery never becomes a new launch gate.
"""
from functools import lru_cache
import json
from pathlib import Path
import re
import subprocess

PROFILE_NAME = "hearting_linked_commit"
OWNER_OBSERVE_PROFILE_NAME = "hearting_owner_observe"
OWNER_COMMIT_NO_NETWORK_PROFILE_NAME = "hearting_linked_commit_no_network"
RECORDISH_PROFILE_NAME = "hearting_non_git_record"
TURN_PERMISSIONS_MIN_VERSION = (0, 160, 0)


@lru_cache(maxsize=1)
def named_profiles_available() -> bool:
    try:
        result = subprocess.run(["codex", "sandbox", "--help"], capture_output=True,
                                text=True, stdin=subprocess.DEVNULL, timeout=10)
        return result.returncode == 0 and "--permission-profile" in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def turn_permissions_available(command: list[str] | None = None) -> bool:
    """Whether this app-server CLI supports the negotiated turn profile selector.

    Codex 0.160.0 added ``turn/start.permissions``. Unknown or unparseable
    runtimes keep the existing sandbox-policy projection instead of receiving
    a field their protocol may silently ignore.
    """
    executable = command[0] if command else "codex"
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, stdin=subprocess.DEVNULL, timeout=10)
        if result.returncode != 0:
            return False
        match = re.search(
            r"\bcodex(?:[-_ ]cli(?:_rs)?)?[ /]+v?(0\.\d+\.\d+)\b",
            result.stdout + "\n" + result.stderr,
        )
        if match is None:
            return False
        version = tuple(int(part) for part in match.group(1).split("."))
        return version >= TURN_PERMISSIONS_MIN_VERSION
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


# Entries inside a primary `.git` that stay read-only so a commit-capable
# worker cannot plant config or hooks a later unsandboxed session would run.
# `config`/`hooks` are always protected; the optional ones only when present,
# because Codex materializes an empty placeholder for a missing protected path
# and a placeholder `config.worktree` or `info` breaks git.
_PRIMARY_GIT_PROTECTED = ("config", "hooks")
_PRIMARY_GIT_PROTECTED_IF_PRESENT = ("config.worktree", "info")
_LINKED_GIT_PROTECTED_IF_PRESENT = (
    "info", "config.worktree", "modules", "HEAD", "index", "worktrees",
)


def commit_profile_config(worktree: str, writable_roots: list[str],
                          sandbox: str, network: bool, *,
                          primary_commit: bool = False) -> dict | None:
    if sandbox != "workspace-write":
        return None
    try:
        wt = Path(worktree).resolve()
        result = subprocess.run(["git", "-C", str(wt), "rev-parse",
                                 "--absolute-git-dir", "--git-common-dir"],
                                capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        git_dir, common = result.stdout.strip().splitlines()
        git_dir = Path(git_dir).resolve()
        common = (wt / common).resolve()
        roots = {str(Path(root).resolve()) for root in writable_roots}
        if git_dir == common:
            # Primary checkout: only a commit-grant caller on a runtime with
            # named profiles gets a grant, and never as a plain writable root
            # (`--add-dir .git` would also open config and hooks).
            if not primary_commit or not named_profiles_available():
                return None
            return _profile({
                **{root: "write" for root in sorted(roots)},
                str(git_dir): "write",
                **{str(git_dir / name): "read" for name in _PRIMARY_GIT_PROTECTED},
                **{str(git_dir / name): "read" for name in _PRIMARY_GIT_PROTECTED_IF_PRESENT
                   if (git_dir / name).exists()},
            }, network)
        required = {str(git_dir), *(str(common / name) for name in ("objects", "refs", "logs"))}
        if not required.issubset(roots) or not named_profiles_available():
            return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return _profile({
        str(common): "write",
        **{str(common / name): "read" for name in _PRIMARY_GIT_PROTECTED},
        **{str(common / name): "read" for name in _LINKED_GIT_PROTECTED_IF_PRESENT
           if (common / name).exists()},
        **{root: "write" for root in sorted(roots)},
    }, network)


def owner_turn_profile_config(worktree: str, writable_roots: list[str],
                              sandbox: str, *, primary_commit: bool = False) -> dict | None:
    """Build safe default and owner commit profiles for same-thread turn selection."""
    commit = commit_profile_config(
        worktree, writable_roots, sandbox, True, primary_commit=primary_commit,
    )
    if commit is None:
        return None
    commit_profile = commit["permissions"][PROFILE_NAME]
    observe_profile = {
        "extends": ":read-only",
        "filesystem": {},
        "network": {"enabled": False},
    }
    no_network_profile = {
        **commit_profile,
        "network": {"enabled": False},
    }
    return {
        "default_permissions": OWNER_OBSERVE_PROFILE_NAME,
        "permissions": {
            OWNER_OBSERVE_PROFILE_NAME: observe_profile,
            PROFILE_NAME: commit_profile,
            OWNER_COMMIT_NO_NETWORK_PROFILE_NAME: no_network_profile,
        },
    }


def recordish_profile_config(worktree: str, writable_roots: list[str],
                             route_file: str | None, *, network: bool = False) -> dict | None:
    """Build a read-only Codex profile with only sealed artifact/runtime write roots."""
    if not route_file or not named_profiles_available():
        return None
    try:
        from dispatch_contract import launch_repository_identity
        _identity, kind = launch_repository_identity(worktree, route_file)
        if kind != "non-git":
            return None
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        cwd = Path(worktree).resolve()
        artifact = Path(route["artifact_root"]).resolve()
        if artifact == cwd:
            return None
        artifact_ancestor = artifact in cwd.parents
        allowed: set[str] = set()
        for raw in writable_roots:
            root = Path(raw).expanduser().resolve(strict=False)
            # Never grant the cwd or an ancestor of it. An in-cwd grant must be
            # contained by the route's canonical artifact root.
            if root == cwd or root in cwd.parents:
                continue
            if root == cwd or cwd in root.parents:
                if artifact_ancestor:
                    continue
                if not (root == artifact or artifact in root.parents):
                    continue
            allowed.add(str(root))
        if str(artifact) not in allowed:
            allowed.add(str(artifact))
        filesystem = {root: "write" for root in sorted(allowed)}
        if artifact_ancestor:
            filesystem[str(cwd)] = "read"
        return {
            "default_permissions": RECORDISH_PROFILE_NAME,
            "permissions": {RECORDISH_PROFILE_NAME: {
                "extends": ":read-only",
                "filesystem": filesystem,
                "network": {"enabled": bool(network)},
            }},
        }
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return None
def _profile(filesystem: dict, network: bool) -> dict:
    return {
        "default_permissions": PROFILE_NAME,
        "permissions": {PROFILE_NAME: {
            "extends": ":workspace",
            "filesystem": filesystem,
            "network": {"enabled": bool(network)},
        }},
    }


def _toml(value) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + "=" + _toml(item)
                               for key, item in value.items()) + "}"
    return json.dumps(value)


def config_arguments(config: dict) -> list[str]:
    # Keep absolute paths in one inline TOML table: CLI dotted keys split dots
    # in path names (including .git), even when those segments are quoted.
    arguments = ["-c", "default_permissions=" + _toml(config["default_permissions"])]
    for name, profile in config["permissions"].items():
        arguments.extend(("-c", "permissions." + name + "=" + _toml(profile)))
    return arguments
