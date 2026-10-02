"""Project existing linked-worktree Git grants into native Codex permissions.

No user config is written. Runtimes without named profiles keep their existing
legacy sandbox projection; discovery never becomes a new launch gate.
"""
from functools import lru_cache
import json
from pathlib import Path
import subprocess

PROFILE_NAME = "hearting_linked_commit"


@lru_cache(maxsize=1)
def named_profiles_available() -> bool:
    try:
        result = subprocess.run(["codex", "sandbox", "--help"], capture_output=True,
                                text=True, timeout=10)
        return result.returncode == 0 and "--permission-profile" in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def commit_profile_config(worktree: str, writable_roots: list[str],
                          sandbox: str, network: bool) -> dict | None:
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
        if git_dir == common:
            return None
        roots = {str(Path(root).resolve()) for root in writable_roots}
        required = {str(git_dir), *(str(common / name) for name in ("objects", "refs", "logs"))}
        if not required.issubset(roots) or not named_profiles_available():
            return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return {
        "default_permissions": PROFILE_NAME,
        "permissions": {PROFILE_NAME: {
            "extends": ":workspace",
            "filesystem": {str(common): "read", **{root: "write" for root in sorted(roots)}},
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
    return ["-c", "default_permissions=" + _toml(config["default_permissions"]),
            "-c", "permissions." + PROFILE_NAME + "=" + _toml(config["permissions"][PROFILE_NAME])]
