"""Optional, bounded training-progress reads for exact registered processes.

This observes existing producer metadata. It never imports a producer, scans a
run tree, writes a registry, or changes the registered process lifecycle.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys


MAX_BYTES = 65536
MAX_ARMS = 32


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path: Path, root: Path):
    """One regular, root-contained file; reject links, swaps and growing reads."""
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("path-traversal")
    relative = path.relative_to(root)
    parents = []
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("progress-link")
        parents.append((current, info.st_dev, info.st_ino))
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        first = os.fstat(stream.fileno())
        if not stat.S_ISREG(first.st_mode) or first.st_size > MAX_BYTES:
            raise ValueError("progress-not-bounded-file")
        data = stream.read(MAX_BYTES + 1)
        last = os.fstat(stream.fileno())
    if len(data) > MAX_BYTES or _signature(first) != _signature(last) \
            or _signature(last) != _signature(path.stat()):
        raise ValueError("progress-changed-during-read")
    for parent, device, inode in parents:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != (device, inode):
            raise ValueError("progress-path-changed")
    return data, last.st_mtime


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate-progress-key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("nonfinite-progress")

    value = json.loads(data, object_pairs_hook=unique, parse_constant=nonfinite)
    if not isinstance(value, dict):
        raise ValueError("progress-not-object")
    return value


def observe_process(pid):
    """Stable same-EUID procfs observation, including the actual argv and cwd."""
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        root = Path("/proc") / str(pid)
        if root.stat().st_uid != os.geteuid():
            return None
        before = (root / "stat").read_bytes()
        fields = before.rsplit(b") ", 1)[1].split()
        raw = (root / "cmdline").read_bytes()
        cwd = os.readlink(root / "cwd")
        after = (root / "stat").read_bytes().rsplit(b") ", 1)[1].split()
        if not raw or len(raw) > MAX_BYTES or fields[19] != after[19] or fields[2] != after[2]:
            return None
        if root.stat().st_uid != os.geteuid() or (root / "cmdline").read_bytes() != raw \
                or os.readlink(root / "cwd") != cwd:
            return None
        return {"pid": pid, "starttime": fields[19].decode("ascii"),
                "command_hash": hashlib.sha256(raw).hexdigest(),
                "process_group": int(fields[2]), "cwd": cwd,
                "argv": [part.decode("utf-8", "strict") for part in raw.rstrip(b"\0").split(b"\0")]}
    except (OSError, ValueError, TypeError, IndexError, UnicodeError):
        return None


def _matches(actual, expected):
    return isinstance(actual, dict) and isinstance(expected, dict) and all(
        expected.get(key) not in (None, "")
        and str(actual.get(key)) == str(expected[key])
        for key in ("pid", "starttime", "command_hash"))


def _config_argument(argv):
    refs = []
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            refs.append(argv[index + 1])
        elif arg.startswith("--config="):
            refs.append(arg[len("--config="):])
    return refs[0] if len(refs) == 1 and refs[0] else None


def resolve_config(cwd, reference):
    """Reuse the normal resolver's CLI, keeping its failures off the Fleet TUI."""
    tool = Path(__file__).resolve().parents[1] / "tools" / "lab-config-provenance.py"
    result = subprocess.run(
        [sys.executable, "-B", str(tool), "resolve", "--repo", str(cwd), "--ref", reference],
        capture_output=True, timeout=2, check=False,
    )
    if result.returncode or len(result.stdout) > MAX_BYTES:
        raise ValueError("config-reference-unresolved")
    return _json(result.stdout)


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**63


def _hash(value):
    return value.removeprefix("sha256:") if isinstance(value, str) else None


def _schedule_epoch(training, attempt, total):
    """Optional schedule intervals, derived only from an explicit exact cadence."""
    epochs, blocks, updates = (training.get(key) for key in
                               ("epochs", "blocks_per_epoch", "updates_per_block"))
    if not all(_count(value) and value > 0 for value in (epochs, blocks, updates)):
        return None
    width = blocks * updates
    if epochs * width != total:
        return None
    completed, position = divmod(attempt, width)
    state = ("start" if attempt == 0 else "complete" if attempt == total
             else "boundary" if position == 0 else "partial")
    return {"current": completed + int(position > 0), "total": epochs,
            "attempts_per_epoch": width, "completed": completed,
            "attempt_in_epoch": position, "state": state}


def collect(run, registry, now, process_reader=observe_process, config_resolver=resolve_config):
    """Return one optional active-arm observation; unknown or ambiguous shapes fail soft."""
    try:
        wrapper = process_reader(run.get("pid"))
        if not _matches(wrapper, run) or wrapper["process_group"] != run.get("process_group"):
            return None
        root = Path(registry).parent.resolve(strict=True)
        metadata_bytes, metadata_mtime = _read(root / "run.json", root)
        metadata = _json(metadata_bytes)
        arms = metadata.get("arms", [metadata])
        if not isinstance(arms, list) or not 0 < len(arms) <= MAX_ARMS:
            return None
        candidates = []
        for arm in arms:
            if not isinstance(arm, dict):
                continue
            identity = arm.get("process_identity")
            if not isinstance(identity, dict):
                continue
            expected = {**identity, "command_hash": identity.get("command_hash")
                        or identity.get("cmdline_sha256")}
            child = process_reader(identity.get("pid"))
            if _matches(child, expected) and child["process_group"] == wrapper["process_group"]:
                candidates.append((arm, child))
        if len(candidates) != 1:
            return None
        arm, child = candidates[0]
        if Path(child["cwd"]).resolve(strict=True) != Path(run["cwd"]).resolve(strict=True):
            return None
        directory = arm.get("directory")
        if not isinstance(directory, str) or not directory:
            return None
        directory = Path(directory)
        if not directory.is_absolute():
            directory = root / directory
        progress_path = directory / "progress.json"
        data, progress_mtime = _read(progress_path, root)
        progress = _json(data)
        attempt, successful, skipped = (progress.get(key) for key in ("attempt", "successful", "skipped"))
        if not all(_count(value) for value in (attempt, successful, skipped)) \
                or attempt != successful + skipped:
            return None
        reference = _config_argument(child["argv"])
        if reference is None:
            return None
        resolved = config_resolver(child["cwd"], reference)
        config_path = Path(resolved["path"])
        config_bytes, _ = _read(config_path, Path(child["cwd"]).resolve(strict=True))
        digest = hashlib.sha256(config_bytes).hexdigest()
        if digest != _hash(arm.get("config_sha256")):
            return None
        config = _json(config_bytes)
        training = config.get("training")
        total = training.get("attempts") if isinstance(training, dict) else None
        if not _count(total) or total <= 0 or attempt > total:
            return None
        epoch = _schedule_epoch(training, attempt, total)
        last = progress.get("last")
        loss = None
        if isinstance(last, dict) and _count(last.get("attempt")) and last["attempt"] == attempt:
            metrics = last.get("metrics")
            value = metrics.get("loss") if isinstance(metrics, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                loss = value
        # No read may outlive the exact wrapper/child tuple it was attributed to.
        if process_reader(run["pid"]) != wrapper or process_reader(child["pid"]) != child:
            return None
        return {
            "pid": child["pid"], "starttime": child["starttime"],
            "command_hash": child["command_hash"], "process_group": child["process_group"],
            "phase": arm.get("state"), "arm": arm.get("name"),
            "attempt": attempt, "attempt_total": total,
            "percent": attempt * 100.0 / total, "successful": successful, "skipped": skipped,
            "loss": loss, "loss_kind": "last-batch" if loss is not None else None,
            "observed_at": float(now), "progress_updated_at": progress_mtime,
            "progress_age_s": max(0.0, float(now) - progress_mtime),
            "metadata_path": str(root / "run.json"),
            "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
            "metadata_updated_at": metadata_mtime,
            "progress_path": str(progress_path), "progress_sha256": hashlib.sha256(data).hexdigest(),
            "config_path": str(config_path), "config_ref": resolved["config_ref"],
            "config_sha256": digest,
            **({"schedule_epoch": epoch} if epoch is not None else {}),
        }
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError,
            OverflowError, subprocess.SubprocessError):
        return None
