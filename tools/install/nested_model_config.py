#!/usr/bin/env python3
"""Snapshot a parent Codex home's effective model configuration into a nested,
harness-created execution home before installer/native-agent rendering.

A derived (nested) Codex home is normally seeded by the regular installer,
which writes the *shipped* ``models.conf``. That shipped file is complete and
looks like a valid user file, so a later ``resolve_config`` call on the nested
home happily accepts it — the parent's actually-configured model tiers never
reach the nested home or the native agents rendered from it. This module
closes that gap with one nested-only transaction:

1. Resolve the parent's whole effective configuration with the existing
   ``model_config.resolve_config`` (the only selection authority — this
   module never invents a second one).
2. Snapshot that exact selection into the nested home's own
   ``agent-config/models.conf``, tracked by an explicit ownership receipt
   (``.harness/nested-model-config/receipt.json``) so a later run can tell an
   owned snapshot apart from a foreign/user-edited file.
3. Run the existing installer against the nested home (still user-runtime
   seed-once for its own concerns) and materialize the native-agent payload
   from what is now the nested home's *own* effective config.

All public mutating operations (``seed``, ``prepare``, ``recover --apply``)
share one ``safe_fs.TargetLock`` keyed at
``<nested>/.harness/nested-model-config/preparation``, held by the private
lock-scoped methods only -- no operation re-acquires it as a child call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_ROOT / "utilities", _ROOT / "tools" / "install"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import model_config  # noqa: E402
import native_agent_payload  # noqa: E402
import safe_fs  # noqa: E402
from dispatch_contract import observed_attempt_liveness, parse_registry_metadata, process_observation  # noqa: E402


RECEIPT_SCHEMA = "hearting.nested-model-config/v1"
HARNESS_DIRNAME = ".harness"
NAMESPACE_DIRNAME = "nested-model-config"
RECEIPT_NAME = "receipt.json"
LOCK_NAME = "preparation"
BACKUP_DIRNAME = "backup"
RECOVER_AUTHORIZATION = "hearting-nested-model-config-recover"
MAX_SNAPSHOT_RETRIES = 3
OWNER = "nested-model-config"


class NestedModelConfigError(RuntimeError):
    """A typed, actionable refusal. ``code`` is machine-readable."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


class ParentUnstableError(NestedModelConfigError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("parent-unstable", detail)


class ConflictError(NestedModelConfigError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("conflict", detail)


class QuiescenceUnavailableError(NestedModelConfigError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("quiescence-unavailable", detail)


def _namespace_dir(nested_home: Path) -> Path:
    return nested_home / HARNESS_DIRNAME / NAMESPACE_DIRNAME


def receipt_path(nested_home: Path) -> Path:
    return _namespace_dir(nested_home) / RECEIPT_NAME


def lock_target(nested_home: Path) -> Path:
    return _namespace_dir(nested_home) / LOCK_NAME


def _absolute(path: str | Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise NestedModelConfigError("unsafe-ambient-path", f"{label} must be absolute: {candidate}")
    return Path(os.path.abspath(os.fspath(candidate)))


def _directory_chain(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            raise NestedModelConfigError("unsafe-ambient-path", f"not a real directory: {current}")


def validate_homes(parent_home: Path, nested_home: Path) -> tuple[Path, Path]:
    """Validate every existing ancestor before locks, mkdir, chmod or backups."""
    parent = _absolute(parent_home, "parent home")
    nested = _absolute(nested_home, "nested home")
    if parent == nested or parent in nested.parents or nested in parent.parents:
        raise NestedModelConfigError("unsafe-ambient-path", "parent and nested home must be disjoint")
    for directory in (parent, nested, nested / "agent-config", nested / "agents",
                      _namespace_dir(nested), nested / ".harness/native-agents",
                      _namespace_dir(nested) / BACKUP_DIRNAME):
        _directory_chain(directory)
    # Authority construction is read-only and checks unsafe leaf types/ancestors.
    for path in (nested / "agent-config/models.conf", receipt_path(nested), lock_target(nested)):
        if path.is_symlink():
            raise ConflictError(f"symlink leaf collision: {path}")
        safe_fs.authority(path, owner=OWNER, allowed_roots=[nested], allow_leaf_symlink=False)
        if path.exists() and not path.is_file():
            raise ConflictError(f"non-file collision: {path}")
    return parent, nested


def _identity(path: Path) -> tuple | None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@dataclass(frozen=True)
class Snapshot:
    state: "safe_fs.PathState"
    identity: tuple | None


def _capture(path: Path) -> Snapshot:
    before = _identity(path)
    state = safe_fs.capture_state(path, include_payload=True)
    after = _identity(path)
    if before != after:
        raise ConflictError(f"file changed during capture: {path}")
    return Snapshot(state, after)


def _assert_snapshot(path: Path, expected: Snapshot) -> None:
    if _capture(path) != expected:
        raise ConflictError(f"concurrent change preserved: {path}")


# ---------------------------------------------------------------------------
# Whole-effective-snapshot capture, validated against resolve_config's grammar
# ---------------------------------------------------------------------------


def _parse_captured_buffer(raw: bytes) -> dict[str, str]:
    """Parse an in-memory buffer with ``model_config``'s own line grammar.

    ``model_config.parse_config`` only accepts a path and re-reads the file
    from disk, which is exactly the gap an A->B->A race exploits: reading the
    path a second time can observe a third, different value. Validating the
    *exact bytes already captured* instead requires reusing its assignment/
    key/value helpers directly, not re-deriving a second parsing policy.
    """

    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise model_config.ModelConfigError(f"configuration unreadable: {exc}") from exc
    values: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = model_config.ASSIGNMENT.fullmatch(stripped)
        if not match or not model_config.SAFE_KEY.fullmatch(match.group(1)):
            raise model_config.ModelConfigError(f"line {lineno} is not a safe CFG_ assignment")
        key = match.group(1)
        if key in values:
            raise model_config.ModelConfigError(f"line {lineno} duplicates {key}")
        values[key] = model_config._parse_value(match.group(2), lineno)
    if not values:
        raise model_config.ModelConfigError("configuration has no CFG_ declarations")
    return values


def capture_snapshot(
    adapter: str,
    parent_home: Path,
    *,
    source_root: str | Path | None = None,
) -> tuple[dict[str, str], "model_config.ModelConfigReceipt", bytes]:
    """Resolve the parent's whole effective config and its exact selected bytes.

    ``resolve_config`` is the only selection authority: this reuses it, then
    independently re-parses the *exact bytes captured from the selected file*
    with the same grammar helpers and requires the two full mappings to be
    equal before accepting the snapshot. A bare before/after byte compare is
    not enough (an A->B->A mid-call change would hide behind it); re-parsing
    the captured buffer itself closes that gap. Retries a bounded number of
    times on mismatch, then reports a typed parent-unstable conflict without
    ever writing a destination.
    """

    last_detail = "no attempt made"
    for _ in range(MAX_SNAPSHOT_RETRIES):
        values, receipt = model_config.resolve_config(
            adapter, runtime=parent_home, source_root=source_root
        )
        selected = Path(receipt.selected_path)
        try:
            _directory_chain(selected.parent)
            before = selected.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise OSError("selected configuration is not a regular file")
            raw = selected.read_bytes()
            after = selected.lstat()
            identity = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            if identity(before) != identity(after):
                raise OSError("selected configuration changed during capture")
        except (OSError, NestedModelConfigError) as exc:
            last_detail = f"selected file unreadable during capture: {exc}"
            continue
        try:
            reparsed = _parse_captured_buffer(raw)
        except model_config.ModelConfigError as exc:
            last_detail = f"captured bytes failed grammar validation: {exc}"
            continue
        if reparsed != values:
            last_detail = "captured bytes parsed to a different mapping than resolve_config selected"
            continue
        return values, receipt, raw
    raise ParentUnstableError(last_detail)


def _content_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Receipt handling
# ---------------------------------------------------------------------------


def _load_receipt(nested_home: Path, snapshot: Snapshot | None = None) -> dict | None:
    observed = snapshot or _capture(receipt_path(nested_home))
    if observed.state.kind == "missing":
        return None
    if observed.state.kind != "file":
        raise ConflictError("receipt is not a regular file")
    try:
        loaded = json.loads(observed.state.payload)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ConflictError(f"receipt unreadable/invalid: {exc}") from exc
    if not isinstance(loaded, dict) or loaded.get("schema") != RECEIPT_SCHEMA:
        raise ConflictError("receipt has an unexpected shape")
    return loaded


def _authority(path: Path, nested: Path, snapshot: Snapshot, *, link: bool = False):
    return safe_fs.authority(path, owner=OWNER, allowed_roots=[nested],
                             allow_leaf_symlink=link, expected=snapshot.state)


def _write_receipt(nested_home: Path, payload: dict, *, expected: Snapshot) -> Snapshot:
    path = receipt_path(nested_home)
    _assert_snapshot(path, expected)
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    safe_fs.atomic_write_bytes(_authority(path, nested_home, expected), body, 0o600,
                               expected=expected.state, create_parents=True)
    written = _capture(path)
    if written.state.payload != body:
        raise ConflictError("receipt changed immediately after publication")
    return written


@dataclass
class Classification:
    state: str
    receipt: dict | None
    dest_state: "safe_fs.PathState"
    reason: str
    destination_snapshot: Snapshot
    receipt_snapshot: Snapshot


def classify(nested_home: Path, dest: Path, parent_home: Path, content_sha256: str) -> Classification:
    destination = _capture(dest)
    receipt_state = _capture(receipt_path(nested_home))
    receipt = _load_receipt(nested_home, receipt_state)
    state, reason = "fresh", ""
    if receipt is None:
        if destination.state.kind != "missing":
            state, reason = "conflict-unmarked", "existing destination has no ownership receipt"
    elif (receipt.get("runtime") != "codex" or receipt.get("parent_home") != str(parent_home)
          or receipt.get("destination") != str(dest)):
        state, reason = "conflict-foreign", "receipt names a different runtime/parent/destination"
    elif destination.state.kind != "file" or destination.state.digest != receipt.get("content_sha256"):
        state, reason = "conflict-foreign", "destination changed outside this helper"
    elif destination.state.digest == content_sha256:
        state = "owned-fresh"
    else:
        state, reason = "owned-stale", "parent configuration changed"
    return Classification(state, receipt, destination.state, reason, destination, receipt_state)


def _source_fingerprint(source_root: Path) -> str:
    # Bind code inputs too: unchanged rendered bytes do not prove a new renderer
    # or projection source has been checked. No git/mtime-only identity.
    digest = hashlib.sha256()
    for relative in ("harness-manifest.json", "adapters/codex/bin/native_agent_renderer.py",
                     "tools/install/native_agent_payload.py", "tools/install/nested_model_config.py",
                     "adapters/codex/bin/install-runtime-projection.sh"):
        path = source_root / relative
        digest.update(relative.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        digest.update(b"\0")
    digest.update(native_agent_payload.renderer.RENDERER_VERSION.encode())
    return digest.hexdigest()


def _run_installer(installer: Path, nested_home: Path, agent_home: Path, extra_args: list[str]) -> tuple[int, str]:
    env = {**os.environ, "AGENT_HOME": str(agent_home), "CODEX_HOME": str(nested_home)}
    # Nested transaction owns these CAS writes. The global installer keeps its
    # existing behavior unless this narrowly scoped option is explicitly given.
    result = subprocess.run([str(installer), "--defer-native-agent-links", *extra_args],
                            cwd=str(agent_home), env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return result.returncode, (result.stderr or result.stdout or "").strip()


def _pending(adapter, parent, dest, cfg_receipt, raw) -> dict:
    return {"schema": RECEIPT_SCHEMA, "runtime": adapter, "parent_home": str(parent),
            "selected_source": cfg_receipt.as_dict(), "destination": str(dest),
            "content_sha256": _content_sha256(raw),
            "projection": {"state": "pending", "payload_digest": None,
                           "failure_stage": None, "failure_detail": None}}


def _seed_locked(*, adapter, parent_home, nested_home, source_root, predecessors=None):
    values, cfg_receipt, raw = capture_snapshot(adapter, parent_home, source_root=source_root)
    dest = model_config.user_path(adapter, runtime=nested_home)
    current = classify(nested_home, dest, parent_home, _content_sha256(raw))
    if current.state.startswith("conflict-"):
        raise ConflictError(current.reason)
    if predecessors is not None:
        if (current.destination_snapshot != predecessors[dest]
                or current.receipt_snapshot != predecessors[receipt_path(nested_home)]):
            raise ConflictError("recovery predecessor changed before seed")
    pending = _pending(adapter, parent_home, dest, cfg_receipt, raw)
    receipt_state = current.receipt_snapshot
    # Invalidate complete BEFORE replacing an owned config. During the narrow
    # update window the receipt still binds the old bytes and says pending.
    if current.receipt is not None:
        invalidated = dict(current.receipt, projection=pending["projection"])
        receipt_state = _write_receipt(nested_home, invalidated, expected=receipt_state)
    destination = current.destination_snapshot
    _assert_snapshot(dest, destination)
    if current.state in {"fresh", "owned-stale"}:
        safe_fs.atomic_write_bytes(_authority(dest, nested_home, destination), raw, 0o600,
                                   expected=destination.state, create_parents=True)
        destination = _capture(dest)
    if destination.state.payload != raw:
        raise ConflictError("destination no longer equals captured parent bytes")
    receipt_state = _write_receipt(nested_home, pending, expected=receipt_state)
    return values, cfg_receipt, raw, destination, pending, receipt_state


def _native_inventory(nested: Path, names) -> dict[Path, Snapshot]:
    """Owned links AND their payload bytes/metadata; foreign collisions refuse."""
    result = {}
    for name in names:
        link = nested / "agents" / name
        snapshot = _capture(link)
        if snapshot.state.kind == "symlink":
            target = Path(snapshot.state.link_target)
            if not target.is_absolute():
                target = link.parent / target
            if not native_agent_payload.verify_owned_target(nested, target):
                raise ConflictError(f"foreign or modified native definition: {link}")
        elif snapshot.state.kind != "missing":
            raise ConflictError(f"custom native definition collision: {link}")
        result[link] = snapshot
    payload_root = nested / ".harness/native-agents"
    if payload_root.exists():
        for directory in sorted(payload_root.iterdir()):
            _directory_chain(directory)
            for path in sorted(directory.iterdir()):
                snapshot = _capture(path)
                if snapshot.state.kind != "file":
                    raise ConflictError(f"unsafe generated payload entry: {path}")
                result[path] = snapshot
    return result


def _assert_inventory(inventory):
    for path, snapshot in inventory.items():
        _assert_snapshot(path, snapshot)


def _verify_config(dest, destination, raw, values):
    _assert_snapshot(dest, destination)
    current = _capture(dest)
    if current.state.payload != raw or _parse_captured_buffer(current.state.payload) != values:
        raise ConflictError("destination no longer matches the full transaction snapshot")


def _verify_projection(nested, source_root, plan, *, links=True):
    refreshed = native_agent_payload.plan_payload(nested, source_root=source_root)
    if refreshed.digest != plan.digest or refreshed.files != plan.files:
        raise ConflictError("native renderer/configuration changed during preparation")
    if not native_agent_payload.check_payload(nested, source_root=source_root)["ok"]:
        raise ConflictError("native payload or metadata changed")
    if links:
        for name in plan.files:
            link = nested / "agents" / name
            expected_target = plan.target_dir / name
            if (not link.is_symlink() or os.readlink(link) != str(expected_target)
                    or not native_agent_payload.verify_owned_target(nested, expected_target)):
                raise ConflictError(f"native definition changed: {link}")


def _mark_projection_failure(nested, pending, expected, stage, detail):
    failed = dict(pending, projection=dict(pending["projection"], failure_stage=stage,
                                          failure_detail=str(detail)[:2000]))
    try:
        _write_receipt(nested, failed, expected=expected)
    except (safe_fs.SafetyError, NestedModelConfigError):
        pass  # A concurrent successor is never re-read and blessed or overwritten.


def _prepare_locked(*, adapter, parent_home, nested_home, source_root, installer, installer_args, predecessors=None):
    values, cfg_receipt, raw, destination, pending, receipt_state = _seed_locked(
        adapter=adapter, parent_home=parent_home, nested_home=nested_home, source_root=source_root,
        predecessors=predecessors)
    dest = model_config.user_path(adapter, runtime=nested_home)
    stage = "native-payload"
    try:
        source_identity = _source_fingerprint(source_root)
        plan = native_agent_payload.plan_payload(nested_home, source_root=source_root)
        expected_files = native_agent_payload.renderer.render_agents(
            values, native_agent_payload._kernel_names(source_root))
        if plan.files != expected_files:
            raise ConflictError("native plan did not render the captured full mapping")
        _verify_config(dest, destination, raw, values)
        inventory = _native_inventory(nested_home, plan.files)
        if predecessors is not None:
            prior_native = {p: state for p, state in predecessors.items()
                            if p not in {dest, receipt_path(nested_home)}}
            if inventory != prior_native:
                raise ConflictError("recovery native predecessor changed before projection")
        if installer is not None:
            stage = "installer"
            code, detail = _run_installer(installer, nested_home, source_root, installer_args)
            if code:
                raise NestedModelConfigError("installer-failed", detail)
        stage = "native-payload"
        _assert_inventory(inventory)
        _verify_config(dest, destination, raw, values)
        _assert_snapshot(receipt_path(nested_home), receipt_state)
        materialized = native_agent_payload.materialize_payload(plan)
        _verify_projection(nested_home, source_root, plan, links=False)
        # Verify previous targets as well as the newly generated payload. An
        # installer or concurrent writer must not edit a definition behind a
        # stable symlink and have it silently adopted.
        _assert_inventory(inventory)
        for name in plan.files:
            link = nested_home / "agents" / name
            expected = inventory[link]
            target = str(plan.target_dir / name)
            _assert_snapshot(link, expected)
            if expected.state.kind == "symlink" and expected.state.link_target == target:
                continue
            safe_fs.atomic_write_symlink(_authority(link, nested_home, expected, link=True),
                                         target, expected=expected.state, create_parents=True)
        _verify_config(dest, destination, raw, values)
        _verify_projection(nested_home, source_root, plan)
        if _source_fingerprint(source_root) != source_identity:
            raise ConflictError("projection source changed during preparation")
        _assert_snapshot(receipt_path(nested_home), receipt_state)
        _verify_config(dest, destination, raw, values)
        complete = dict(pending, projection={"state": "complete", "payload_digest": plan.digest,
                        "source_fingerprint": source_identity, "failure_stage": None, "failure_detail": None})
        receipt_state = _write_receipt(nested_home, complete, expected=receipt_state)
        _verify_config(dest, destination, raw, values)
        _verify_projection(nested_home, source_root, plan)
        if _source_fingerprint(source_root) != source_identity:
            raise ConflictError("projection source changed during completion publication")
        _assert_snapshot(receipt_path(nested_home), receipt_state)
        return {"status": "prepared", "destination": str(dest), "content_sha256": _content_sha256(raw),
                "native_payload": materialized, "config_source": cfg_receipt.as_dict()}
    except (NestedModelConfigError, safe_fs.SafetyError, native_agent_payload.PayloadError,
            native_agent_payload.PayloadMaterializeError) as exc:
        _mark_projection_failure(nested_home, pending, receipt_state, stage, str(exc))
        if isinstance(exc, (native_agent_payload.PayloadError, native_agent_payload.PayloadMaterializeError)):
            raise NestedModelConfigError("native-payload-failed", str(exc)) from exc
        raise


def seed(adapter, parent_home, nested_home, *, source_root):
    parent, nested = validate_homes(parent_home, nested_home)
    root = _absolute(source_root, "source root")
    with safe_fs.TargetLock(lock_target(nested)):
        validate_homes(parent, nested)
        nested.mkdir(mode=0o700, parents=True, exist_ok=True)
        _, cfg_receipt, raw, _, _, _ = _seed_locked(
            adapter=adapter, parent_home=parent, nested_home=nested, source_root=root)
        return {"status": "seeded", "projection": "pending", "content_sha256": _content_sha256(raw),
                "destination": str(model_config.user_path(adapter, runtime=nested)),
                "config_source": cfg_receipt.as_dict()}


def prepare(adapter, parent_home, nested_home, *, source_root, installer=None, installer_args=None):
    parent, nested = validate_homes(parent_home, nested_home)
    root = _absolute(source_root, "source root")
    installer_path = _absolute(installer, "installer") if installer is not None else None
    with safe_fs.TargetLock(lock_target(nested)):
        validate_homes(parent, nested)
        nested.mkdir(mode=0o700, parents=True, exist_ok=True)
        return _prepare_locked(adapter=adapter, parent_home=parent, nested_home=nested, source_root=root,
                               installer=installer_path, installer_args=list(installer_args or []))


def check(adapter, parent_home, nested_home, *, source_root):
    """Read-only readiness and preparation eligibility; never acquires a lock."""
    parent, nested = validate_homes(parent_home, nested_home)
    root = _absolute(source_root, "source root")
    dest = model_config.user_path(adapter, runtime=nested)
    values, cfg_receipt, raw = capture_snapshot(adapter, parent, source_root=root)
    current = classify(nested, dest, parent, _content_sha256(raw))
    ready = False
    if current.state == "owned-fresh":
        try:
            plan = native_agent_payload.plan_payload(nested, source_root=root)
            _verify_projection(nested, root, plan)
            projection = current.receipt.get("projection", {})
            ready = (projection.get("state") == "complete" and projection.get("payload_digest") == plan.digest
                     and projection.get("source_fingerprint") == _source_fingerprint(root))
            _assert_snapshot(dest, current.destination_snapshot)
            _assert_snapshot(receipt_path(nested), current.receipt_snapshot)
        except (NestedModelConfigError, native_agent_payload.PayloadError, OSError):
            ready = False
    return {"ok": not current.state.startswith("conflict-"), "ready": ready, "state": current.state,
            "reason": current.reason, "destination": str(dest), "content_sha256": _content_sha256(raw),
            "config_source": cfg_receipt.as_dict(), "native_payload_verified": ready}


# ---------------------------------------------------------------------------
# Legacy recovery: explicit, backed-up, hash-pinned, quiescence-gated
# ---------------------------------------------------------------------------


def _proc_root() -> Path | None:
    root = Path("/proc")
    return root if root.is_dir() else None


def _scoped_live_use(nested_home: Path) -> str:
    """Observe this user's runtime processes, not privileged OS administrators.

    The target must be caller-owned and private (0700). Every process of that
    UID is scanned, except the checker itself; registry checks independently
    cover *all* attributed attempts, including cross-UID/namespace attempts.
    Unknown relevant observations refuse. This does not claim to exclude a
    privileged administrator or cross-UID processes holding earlier-opened FDs.
    """
    proc = _proc_root()
    if proc is None:
        return "unknown"
    try:
        home_stat = nested_home.lstat()
        if home_stat.st_uid != os.geteuid() or stat.S_IMODE(home_stat.st_mode) != 0o700:
            return "unknown"
    except OSError:
        return "unknown"
    unknown = False
    try:
        entries = list(proc.iterdir())
    except OSError:
        return "unknown"
    def uses_home(target):
        if target.endswith(" (deleted)"):
            target = target[:-10]
        path = Path(target)
        if not path.is_absolute():
            return False
        path = Path(os.path.normpath(target))
        return path == nested_home or nested_home in path.parents
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        pid = int(entry.name)
        try:
            if entry.stat().st_uid != home_stat.st_uid:
                continue
        except FileNotFoundError:
            continue
        except OSError:
            unknown = True
            continue
        before = process_observation(pid)
        if before[0] == "missing" or (before[0] == "present" and before[2] == "Z"):
            continue
        if before[0] != "present":
            unknown = True
            continue
        incomplete = False
        try:
            if uses_home(os.readlink(entry / "cwd")):
                return "in-use"
            for chunk in (entry / "environ").read_bytes().split(b"\0"):
                if chunk.startswith(b"CODEX_HOME=") and uses_home(chunk[11:].decode("utf-8", "surrogateescape")):
                    return "in-use"
            for fd in (entry / "fd").iterdir():
                try:
                    if uses_home(os.readlink(fd)):
                        return "in-use"
                except FileNotFoundError:
                    continue  # fd closed during this live process's scan
        except OSError:
            incomplete = True
        after = process_observation(pid)
        if after[0] == "missing" or (after[0] == "present" and after[2] == "Z"):
            continue
        if before[:2] != after[:2] or after[0] != "present" or incomplete:
            unknown = True
    return "unknown" if unknown else "quiescent"


def _registry_attribution_quiescent(nested_home: Path, jobs: Path | None) -> str:
    if jobs is None or jobs.is_symlink() or not jobs.is_file():
        return "unknown"
    try:
        snapshot = _capture(jobs)
        lines = snapshot.state.payload.decode("utf-8").splitlines()
        for line in lines:
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 6:
                return "unknown"
            status, pipe = parts[1], parts[5]
            # The portable parser is permissive; refuse ambiguous input before
            # using its supported exact-PID/namespace-aware liveness reader.
            fields = [field for field in pipe.split(",") if "=" in field]
            # Registry writers append repeatable note annotations; the portable
            # parser retains the latest note. It is not process/attribution
            # proof. Every other key must remain unambiguous.
            keys = [field.split("=", 1)[0] for field in fields
                    if field.split("=", 1)[0] != "note"]
            if len(set(keys)) != len(keys):
                return "unknown"
            metadata = parse_registry_metadata(pipe)
            attributed = metadata.get("codex_home")
            derived = str(Path(parts[3]) / ".dispatch/nested-codex-home")
            if attributed != str(nested_home) and derived != str(nested_home):
                # A mention in an unsupported field cannot prove non-use.
                if str(nested_home) in pipe:
                    return "unknown"
                continue
            if status in {"open", "running"}:
                return "in-use"
            observed = observed_attempt_liveness(status, metadata)
            if observed.state == "alive":
                return "in-use"
            if observed.state != "terminal":
                return "unknown"
        _assert_snapshot(jobs, snapshot)
    except (OSError, UnicodeError, NestedModelConfigError, safe_fs.SafetyError):
        return "unknown"
    return "quiescent"


def assert_quiescent(nested_home: Path, jobs: Path | None) -> None:
    registry = _registry_attribution_quiescent(nested_home, jobs)
    if registry == "in-use":
        raise QuiescenceUnavailableError("a matching registry attempt is open or still live")
    processes = _scoped_live_use(nested_home)
    if processes == "in-use":
        raise QuiescenceUnavailableError("a live process still uses this home")
    if registry != "quiescent" or processes != "quiescent":
        raise QuiescenceUnavailableError("complete exact process quiescence could not be proved")


def _backup_dir(nested_home: Path) -> Path:
    return _namespace_dir(nested_home) / BACKUP_DIRNAME / uuid.uuid4().hex


def _backup_file(src: Path, backup_root: Path, *, nested_home: Path, snapshot: Snapshot) -> dict | None:
    if snapshot.state.kind == "missing":
        return None
    _assert_snapshot(src, snapshot)
    relative = src.relative_to(nested_home)
    dest = backup_root / "files" / relative
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Link targets are backed up as data, never followed by the recovery writer.
    data = snapshot.state.payload if snapshot.state.kind == "file" else snapshot.state.link_target.encode()
    with dest.open("xb") as handle:
        os.chmod(dest, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return {"source": str(src), "backup": str(dest), "sha256": _content_sha256(data),
            "kind": snapshot.state.kind, "link_target": snapshot.state.link_target}


def recover(adapter, parent_home, nested_home, *, source_root, apply, authorize,
            expect_current_sha256, jobs, installer=None, installer_args=None):
    parent, nested = validate_homes(parent_home, nested_home)
    root = _absolute(source_root, "source root")
    dest = model_config.user_path(adapter, runtime=nested)
    if not apply:
        state = _capture(dest).state
        return {"ok": state.kind in {"missing", "file"}, "action": "recover-dry-run",
                "destination": str(dest), "current_digest": state.digest}
    if authorize != RECOVER_AUTHORIZATION:
        raise NestedModelConfigError("authorization-missing", "recover --apply requires --authorize " + RECOVER_AUTHORIZATION)
    jobs_path = _absolute(jobs, "registry") if jobs else None
    installer_path = _absolute(installer, "installer") if installer is not None else None
    with safe_fs.TargetLock(lock_target(nested)):
        validate_homes(parent, nested)
        destination = _capture(dest)
        if destination.state.kind not in {"file", "missing"} or destination.state.digest != expect_current_sha256:
            raise NestedModelConfigError("expected-hash-mismatch", "current file no longer matches authorized hash")
        receipt_state = _capture(receipt_path(nested))
        receipt = _load_receipt(nested, receipt_state)
        if receipt is not None:
            current = classify(nested, dest, parent, destination.state.digest)
            if current.state.startswith("conflict-"):
                raise ConflictError(current.reason)
        # Render names from the selected parent without adopting any nested
        # data yet. Legacy definitions must already be demonstrably owned.
        values, _, _ = capture_snapshot(adapter, parent, source_root=root)
        names = native_agent_payload.renderer.render_agents(values, native_agent_payload._kernel_names(root))
        inventory = _native_inventory(nested, names)
        inventory.update({dest: destination, receipt_path(nested): receipt_state})
        assert_quiescent(nested, jobs_path)
        backup_root = _backup_dir(nested)
        backup_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        backups = []
        for path, snapshot in inventory.items():
            record = _backup_file(path, backup_root, nested_home=nested, snapshot=snapshot)
            if record is not None:
                backups.append(record)
        manifest = backup_root / "manifest.json"
        with manifest.open("x", encoding="utf-8") as handle:
            os.chmod(manifest, 0o600)
            json.dump({"files": backups}, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Never re-capture a changed file as the newly authorized predecessor.
        # Validate membership too, so new definitions/metadata are not adopted.
        _assert_inventory(inventory)
        after_native = _native_inventory(nested, names)
        if after_native != {p: s for p, s in inventory.items() if p not in {dest, receipt_path(nested)}}:
            raise ConflictError("native definition inventory changed after backup")
        assert_quiescent(nested, jobs_path)
        _assert_inventory(inventory)
        if destination.state.kind == "file" and receipt is None:
            adopted = {"schema": RECEIPT_SCHEMA, "runtime": adapter, "parent_home": str(parent),
                       "selected_source": {"reason": "legacy-adopted"}, "destination": str(dest),
                       "content_sha256": destination.state.digest,
                       "projection": {"state": "pending", "payload_digest": None,
                                      "failure_stage": "legacy-adopt", "failure_detail": None}}
            inventory[receipt_path(nested)] = _write_receipt(nested, adopted, expected=receipt_state)
        result = _prepare_locked(adapter=adapter, parent_home=parent, nested_home=nested, source_root=root,
                                 installer=installer_path, installer_args=list(installer_args or []),
                                 predecessors=inventory)
        result["backup"] = {"dir": str(backup_root), "files": backups}
        result["quiescence_scope"] = "private-home-owner-runtime-processes-and-all-attributed-attempts"
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_result(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "seed", "prepare", "recover"])
    parser.add_argument("--adapter", default="codex", choices=["codex"])
    parser.add_argument("--parent-home", required=True)
    parser.add_argument("--nested-home", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--installer")
    parser.add_argument("--installer-arg", action="append", default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--authorize")
    parser.add_argument("--expect-current-sha256")
    parser.add_argument("--jobs")
    args = parser.parse_args(argv)
    if (args.apply or args.dry_run) and args.action != "recover":
        parser.error("--apply and --dry-run are recovery-only; use check for read-only inspection")

    try:
        if args.action == "check":
            payload = check(args.adapter, args.parent_home, args.nested_home, source_root=args.source_root)
            _print_result(payload)
            return 0 if payload.get("ok") else 3
        if args.action == "seed":
            _print_result(seed(args.adapter, args.parent_home, args.nested_home, source_root=args.source_root))
            return 0
        if args.action == "prepare":
            payload = prepare(
                args.adapter,
                args.parent_home,
                args.nested_home,
                source_root=args.source_root,
                installer=args.installer,
                installer_args=args.installer_arg,
            )
            _print_result(payload)
            return 0
        payload = recover(
            args.adapter,
            args.parent_home,
            args.nested_home,
            source_root=args.source_root,
            apply=args.apply,
            authorize=args.authorize,
            expect_current_sha256=args.expect_current_sha256,
            jobs=args.jobs,
            installer=args.installer,
            installer_args=args.installer_arg,
        )
        _print_result(payload)
        return 0
    except (NestedModelConfigError, safe_fs.SafetyError, model_config.ModelConfigError) as exc:
        code = getattr(exc, "code", exc.__class__.__name__)
        detail = getattr(exc, "detail", str(exc))
        _print_result({"ok": False, "error": code, "detail": detail})
        return 3
    except Exception as exc:  # pragma: no cover - unexpected error path
        _print_result({"ok": False, "error": "unexpected-error", "detail": str(exc)})
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
