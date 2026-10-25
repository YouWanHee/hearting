"""Read-only Git worktree classification shared by runtime consumers."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess


def git_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key != "LANGUAGE"}
    env.update({"LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return env


def _negative_ancestry_snapshot(path: Path) -> tuple | None:
    """Snapshot a marker-free ancestry, including exact empty placeholders."""
    try:
        snapshot = []
        for directory in (path, *path.parents):
            directory_info = directory.lstat()
            if not stat.S_ISDIR(directory_info.st_mode):
                return None
            snapshot.append((
                str(directory), directory_info.st_dev, directory_info.st_ino,
                directory_info.st_mode,
            ))
            marker = directory / ".git"
            try:
                info = marker.lstat()
            except FileNotFoundError:
                snapshot.append((str(marker), "absent"))
                continue
            except OSError:
                return None
            if stat.S_ISDIR(info.st_mode):
                # Check the normal metadata names first; a valid repository
                # never needs its .git tree enumerated for this classification.
                for name in ("HEAD", "config", "objects", "refs", "commondir", "index"):
                    try:
                        (marker / name).lstat()
                        return None
                    except FileNotFoundError:
                        continue
                    except OSError:
                        return None
                with os.scandir(marker) as entries:
                    names = tuple(sorted(entry.name for entry in entries))
                if names:
                    return None
                snapshot.append((
                    str(marker), "empty-directory", info.st_dev, info.st_ino,
                    info.st_mode, names,
                ))
            elif stat.S_ISREG(info.st_mode) and info.st_size == 0:
                snapshot.append((
                    str(marker), "empty-file", info.st_dev, info.st_ino,
                    info.st_mode, info.st_size,
                ))
            else:
                return None
        return tuple(snapshot)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _stable_negative_ancestry(path: Path) -> bool:
    """Skip Git only after two matching complete, marker-free ancestry walks."""
    first = _negative_ancestry_snapshot(path)
    if first is None:
        return False
    return _negative_ancestry_snapshot(path) == first


def inspect(path: str | Path, *, expected_root: str | Path | None = None,
            timeout: float = 5.0) -> dict[str, str]:
    """Return git/non-git/error and canonical root, without ambient GIT_* state."""
    original = Path(path).expanduser()
    try:
        candidate = original.resolve(strict=False)
        probe = candidate
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if probe.is_file():
            probe = probe.parent
        probe = probe.resolve(strict=True)
    except OSError as exc:
        return {"state": "error", "detail": f"{type(exc).__name__}: {exc}"}
    if _stable_negative_ancestry(probe):
        return {"state": "non-git", "detail": "no Git worktree marker in path ancestry"}
    try:
        result = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel",
             "--is-bare-repository"],
            text=True, capture_output=True, timeout=timeout, check=False,
            env=git_environment())
    except (OSError, subprocess.SubprocessError) as exc:
        return {"state": "error", "detail": f"{type(exc).__name__}: {exc}"}
    if result.returncode == 0:
        values = result.stdout.splitlines()
        if len(values) != 2 or values[1] not in {"true", "false"}:
            return {"state": "error", "detail": "invalid Git worktree observation"}
        if values[1] == "true":
            return {"state": "error", "detail": "bare Git repository"}
        try:
            root = Path(values[0].strip()).resolve(strict=True)
        except OSError as exc:
            return {"state": "error", "detail": f"invalid Git worktree root: {exc}"}
        if expected_root is not None:
            try:
                expected = Path(expected_root).resolve(strict=False)
            except OSError as exc:
                return {"state": "error", "detail": f"invalid expected root: {exc}"}
            if root != expected:
                return {"state": "non-git", "detail": f"containing worktree is {root}"}
        return {"state": "git", "root": str(root)}
    # A failed Git observation is non-Git only if a fresh complete ancestry
    # scan still proves that every marker is absent or exactly empty.
    if _stable_negative_ancestry(probe):
        return {"state": "non-git", "detail": "no Git worktree marker in path ancestry"}
    stderr = result.stderr or ""
    return {"state": "error", "detail": stderr.strip() or f"git exited {result.returncode}"}
