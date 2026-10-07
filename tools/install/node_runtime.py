#!/usr/bin/env python3
"""Ensure a usable Node.js runtime at install time (user-space, verified).

Claude plugin lifecycle hooks (openai-codex SessionStart/SessionEnd/Stop,
agent-note SessionEnd) execute ``node``; a host without it raises a hook
error at every session boundary. Policy (2026-08-21 user decision, matching
the Cairn Node dependency policy): reuse a compatible Node >= 20.9.0 from
PATH; otherwise install the latest verified LTS into user space and expose
it on the launcher bin dir. Every failure degrades to a warning — ensure
never raises and never blocks the install.

Standalone and Python-stdlib-only, like ``host_probes.py``. Ownership
boundary: only symlinks this module itself created (targets inside our
node root) are ever replaced; a foreign ``node`` on PATH or a foreign file
at the expose path is reported, never touched.

One more shape is ours: a *dangling* launcher link whose target follows the
managed layout (``<data>/hearting/node/current/bin/<name>``) but whose node
root is gone -- an isolated fixture's temp data root, an uninstalled data
dir. Only ``_expose`` writes that spelling, the link executes nothing, and
``_owned_link`` can never claim it again because it resolves into no live
root. ``sweep_dangling_links`` removes exactly those on install, update, and
runtime activation; ``launcher_status`` reports them for ``harness verify``.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import safe_fs

MIN_NODE = (20, 9, 0)
DIST_INDEX_URL = "https://nodejs.org/dist/index.json"
DIST_BASE_URL = "https://nodejs.org/dist"
INDEX_LIMIT = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
EXPOSED = ("node", "npm", "npx")

_ARCHES = {"x86_64": "x64", "aarch64": "arm64"}


def _result(status: str, detail: str, probe_id: str = "host.node-runtime") -> dict:
    return {"id": probe_id, "status": status, "detail": detail}


def _node_root() -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data / "hearting" / "node"


def _bin_dir() -> Path:
    return Path(os.environ.get("HARNESS_BIN_DIR") or Path.home() / ".local" / "bin")


def _parse_version(text: str) -> tuple | None:
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _current_node_version() -> tuple | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        probe = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if probe.returncode != 0:
        return None
    return _parse_version(probe.stdout)


def _fetch_bytes(url: str, limit: int) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "hearting-installer/1"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise OSError(f"response exceeds size limit: {url}")
    return payload


def _latest_lts() -> str:
    rows = json.loads(_fetch_bytes(DIST_INDEX_URL, INDEX_LIMIT))
    for row in rows:
        if isinstance(row, dict) and row.get("lts") and row.get("version"):
            return row["version"]
    raise OSError("no LTS entry in the Node.js dist index")


def _expected_checksum(version: str, archive_name: str) -> str:
    text = _fetch_bytes(f"{DIST_BASE_URL}/{version}/SHASUMS256.txt", INDEX_LIMIT).decode(
        "utf-8", errors="replace"
    )
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive_name:
            return parts[0]
    raise OSError(f"no checksum for {archive_name}")


def _download_verified(url: str, destination: Path, expected: str) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "hearting-installer/1"}
    )
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("xb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise OSError("node archive exceeds size limit")
                digest.update(chunk)
                handle.write(chunk)
    if digest.hexdigest() != expected:
        raise OSError(f"checksum mismatch for {url}")


def _extract(archive: Path, staging: Path, top_level: str) -> Path:
    subprocess.run(
        ["tar", "-xJf", str(archive), "-C", str(staging)],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    extracted = staging / top_level
    if not (extracted / "bin" / "node").is_file():
        raise OSError(f"archive did not contain {top_level}/bin/node")
    return extracted


def _owned_link(path: Path, root: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        target = Path(os.readlink(path))
    except OSError:
        return False
    if not target.is_absolute():
        target = path.parent / target
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _managed_layout(target: Path, name: str) -> bool:
    """Whether ``target`` is where a managed node root exposes ``name``.

    ``_expose`` always links ``<bin>/<name>`` to
    ``<data>/hearting/node/current/bin/<name>``. The data root varies
    (``XDG_DATA_HOME``, an isolated fixture's temp root); the five trailing
    components never do.
    """
    parts = target.parts
    return (
        len(parts) >= 5
        and parts[-1] == name
        and parts[-2] == "bin"
        and parts[-3] == "current"
        and parts[-4] == "node"
        and parts[-5] == "hearting"
    )


def _dangling_managed_link(link: Path, name: str) -> bool:
    """A symlink at ``link`` in the managed layout whose target no longer exists.

    Only ``_expose`` writes that spelling, so the link is ours even when its
    node root was a different data root than the current one. A dangling
    entry executes nothing and shadows nothing (``shutil.which`` and the
    shell both skip it), so removing it changes no behaviour; keeping it
    leaves a broken ``node`` in the launcher dir forever, because
    ``_owned_link`` resolves it into no live root and ``_expose`` then
    treats it as foreign.
    """
    if not link.is_symlink():
        return False
    try:
        target = Path(os.readlink(link))
    except OSError:
        return False
    if not target.is_absolute():
        target = link.parent / target
    if not _managed_layout(target, name):
        return False
    try:
        return not link.exists()
    except OSError:
        return False


def dangling_links() -> list:
    """Exposed names whose launcher link is a dangling managed link (read-only)."""
    bin_dir = _bin_dir()
    return [name for name in EXPOSED if _dangling_managed_link(bin_dir / name, name)]


def sweep_dangling_links() -> list:
    """Remove every dangling managed link from the launcher dir; never raises.

    Returns the names removed. A live link (target present), a foreign link
    (target outside the managed layout), and a regular file are never
    touched; a link that changes under us is left for the next pass.
    """
    removed = []
    bin_dir = _bin_dir()
    for name in EXPOSED:
        link = bin_dir / name
        try:
            if not _dangling_managed_link(link, name):
                continue
            state = safe_fs.capture_state(link)
            if state.kind != "symlink":
                continue
            auth = safe_fs.authority(
                link,
                owner=f"node-runtime:dangling-{name}",
                allowed_paths=(link,),
                expected=state,
            )
            safe_fs.remove_exact(auth)
        except (OSError, safe_fs.SafetyError):
            continue
        removed.append(name)
    return removed


def _swept_detail(removed: list) -> str:
    if not removed:
        return ""
    return f"; removed dangling managed {'/'.join(removed)} from {_bin_dir()}"


def launcher_status() -> dict:
    """Read-only row for ``harness verify``: are the managed launcher links sound?"""
    dangling = dangling_links()
    if dangling:
        return _result(
            "dangling",
            f"dangling managed {'/'.join(dangling)} in {_bin_dir()}; "
            "harness update removes them",
            probe_id="host.node-launchers",
        )
    return _result(
        "ok",
        f"no dangling managed node links in {_bin_dir()}",
        probe_id="host.node-launchers",
    )


def reconcile_launchers() -> dict:
    """Mutating row for ``harness update`` and ``runtime activate``: sweep, then report."""
    removed = sweep_dangling_links()
    left = dangling_links()
    if left:
        return _result(
            "warning",
            f"could not remove dangling managed {'/'.join(left)} from {_bin_dir()}",
            probe_id="host.node-launchers",
        )
    if removed:
        return _result(
            "repaired",
            f"removed dangling managed {'/'.join(removed)} from {_bin_dir()}",
            probe_id="host.node-launchers",
        )
    return _result(
        "ok",
        f"no dangling managed node links in {_bin_dir()}",
        probe_id="host.node-launchers",
    )


def _expose(install_dir: Path, root: Path) -> list:
    """Symlink node/npm/npx into the bin dir; never replace a foreign entry."""
    bin_dir = _bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    skipped = []
    current = root / "current"
    current_state = safe_fs.capture_state(current)
    if current_state.kind != "missing" and not _owned_link(current, root):
        raise OSError(f"managed Node current pointer is foreign: {current}")
    current_auth = safe_fs.authority(
        current,
        owner="node-runtime:current-pointer",
        allowed_roots=(root,),
        expected=current_state,
    )
    safe_fs.atomic_write_symlink(
        current_auth, str(install_dir), create_parents=True, target_is_directory=True
    )
    for name in EXPOSED:
        source = current / "bin" / name
        if not source.exists():
            continue
        link = bin_dir / name
        if link.exists() or link.is_symlink():
            if not _owned_link(link, root):
                skipped.append(name)
                continue
        link_state = safe_fs.capture_state(link)
        link_auth = safe_fs.authority(
            link,
            owner=f"node-runtime:exposed-{name}",
            allowed_paths=(link,),
            expected=link_state,
        )
        safe_fs.atomic_write_symlink(link_auth, str(source), create_parents=True)
    return skipped


def ensure_node(dry_run: bool = False) -> dict:
    """Reuse a compatible node, else install a verified LTS. Never raises.

    A dry run stops before the network and the filesystem: it only reports
    whether an install would happen and where it would put the launchers.
    """
    try:
        # Our own leftovers go first, before any policy decision: a dangling
        # managed link is never a usable node, so neither the opt-out nor a
        # reusable node on PATH is a reason to keep it.
        swept = sweep_dangling_links()
        if os.environ.get("HARNESS_NO_NODE_INSTALL") == "1":
            return _result(
                "ok",
                "node ensure skipped (HARNESS_NO_NODE_INSTALL=1)" + _swept_detail(swept),
            )
        found = _current_node_version()
        if found is not None and found >= MIN_NODE:
            return _result(
                "ok", ("reusing node v%d.%d.%d from PATH" % found) + _swept_detail(swept)
            )
        arch = _ARCHES.get(platform.machine())
        if platform.system() != "Linux" or arch is None:
            return _result(
                "warning",
                f"no compatible node and no managed build for "
                f"{platform.system()}/{platform.machine()}; install Node >= "
                "%d.%d.%d manually" % MIN_NODE,
            )
        if dry_run:
            return _result(
                "would-install",
                "no node >= %d.%d.%d on PATH; " % MIN_NODE
                + f"would install the latest LTS into {_node_root()} and "
                f"expose {'/'.join(EXPOSED)} in {_bin_dir()}",
            )
        version = _latest_lts()
        root = _node_root()
        install_dir = root / version
        top_level = f"node-{version}-linux-{arch}"
        if not (install_dir / "bin" / "node").is_file():
            archive_name = f"{top_level}.tar.xz"
            expected = _expected_checksum(version, archive_name)
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=root) as staging:
                staging_path = Path(staging)
                archive = staging_path / archive_name
                _download_verified(
                    f"{DIST_BASE_URL}/{version}/{archive_name}", archive, expected
                )
                extracted = _extract(archive, staging_path, top_level)
                if install_dir.exists():
                    install_state = safe_fs.capture_state(install_dir)
                    install_auth = safe_fs.authority(
                        install_dir,
                        owner="node-runtime:incomplete-version",
                        allowed_roots=(root,),
                        expected=install_state,
                    )
                    safe_fs.remove_exact(install_auth, recursive=True)
                extracted.rename(install_dir)
        smoke = subprocess.run(
            [str(install_dir / "bin" / "node"), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if smoke.returncode != 0 or _parse_version(smoke.stdout) is None:
            return _result(
                "warning", f"installed node failed its version check: {smoke.stderr.strip()}"
            )
        skipped = _expose(install_dir, root)
        detail = f"installed node {version} at {install_dir}" + _swept_detail(swept)
        if found is not None:
            detail += "; existing node v%d.%d.%d on PATH is below %d.%d.%d and was left untouched" % (
                found + MIN_NODE
            )
        if skipped:
            detail += (
                f"; kept foreign {'/'.join(skipped)} in {_bin_dir()} — "
                f"use {_node_root() / 'current' / 'bin'} directly"
            )
        elif str(_bin_dir()) not in os.environ.get("PATH", "").split(os.pathsep):
            detail += f"; add {_bin_dir()} to PATH"
        return _result("installed", detail)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip().splitlines()
        return _result(
            "warning", f"node install failed: {stderr[0] if stderr else exc}"
        )
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return _result("warning", f"node install failed: {exc}")
