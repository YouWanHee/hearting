#!/usr/bin/env python3
"""Compute and materialize the effective Codex native-agent payload.

WP1 (Astra guide alignment O1). ``adapters/codex/bin/sync-native-agents.py`` is
deterministic from the checked-in ``models.conf`` only. This module renders the
*same* TOML shapes (via ``native_agent_renderer``) from a runtime's *effective*
config (``utilities/model_config.resolve_config`` — whole-file selection, never
merged) and, only through the separately named mutating step, materializes them
under that runtime home's own ``.harness/native-agents/<digest>/`` directory
together with an immutable metadata JSON.

``plan_payload`` never writes, even when the target directory does not exist —
callers (status/doctor/check) may call it freely. Only ``materialize_payload``
may create a directory or file, and only the install/activate/refresh callers
invoke it.

Ownership of a materialized payload is proved by canonical runtime-home
containment plus exact metadata/runtime/digest/per-file-checksum agreement —
never by a ``.harness`` substring or directory-name match. See
``verify_owned_target``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_ROOT / "utilities", _ROOT / "tools", _ROOT / "adapters" / "codex" / "bin"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import harness_manifest  # noqa: E402
import model_config  # noqa: E402
import native_agent_renderer as renderer  # noqa: E402


PAYLOAD_SCHEMA = "hearting.native-agent-payload/v1"
HARNESS_DIRNAME = ".harness"
NATIVE_AGENTS_DIRNAME = "native-agents"


class PayloadError(RuntimeError):
    """A payload could not be planned."""


class PayloadMaterializeError(RuntimeError):
    """A payload could not be safely materialized."""


@dataclass(frozen=True)
class PayloadPlan:
    runtime: str
    runtime_home: Path
    digest: str
    target_dir: Path
    files: dict[str, str]
    checksums: dict[str, str]
    metadata: dict[str, object]
    receipt: dict[str, str]


def _kernel_names(source_root: Path) -> list[str]:
    """Read kernel agent names from *this* source tree's own manifest.

    Mirrors ``runtime_activation.py``'s ``_kernel_agents()`` fallback so a
    packaged/bundled or fixture source tree with no manifest still renders the
    one always-shipped kernel helper, instead of silently reading whichever
    manifest happens to be on disk at the running installer's own location.
    """
    manifest_path = source_root / harness_manifest.MANIFEST_NAME
    if not manifest_path.is_file():
        return ["memory-scout"]
    try:
        canonical = harness_manifest.load(manifest_path)
    except harness_manifest.ManifestError as exc:
        raise PayloadError(f"invalid canonical manifest: {exc}") from exc
    return sorted(canonical["kernel"]["agents"])


def _digest(
    cfg: Mapping[str, str], rendered: Mapping[str, str], provenance: Mapping[str, str]
) -> str:
    hasher = hashlib.sha256()
    hasher.update(renderer.RENDERER_VERSION.encode("utf-8"))
    hasher.update(b"\0\0")
    # Metadata is immutable too: an identical mapping selected from a repaired
    # user file must not collide with the earlier fallback's source/reason.
    hasher.update(json.dumps(dict(provenance), sort_keys=True).encode("utf-8"))
    hasher.update(b"\0")
    for key, value in sorted(cfg.items()):
        hasher.update(key.encode("utf-8"))
        hasher.update(b"=")
        hasher.update(value.encode("utf-8"))
        hasher.update(b"\0")
    hasher.update(b"\0")
    for name, body in sorted(rendered.items()):
        hasher.update(name.encode("utf-8"))
        hasher.update(b":")
        hasher.update(body.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def plan_payload(
    runtime_home: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    source_root: str | Path | None = None,
) -> PayloadPlan:
    """Compute the effective Codex native-agent payload. Never writes."""
    home = Path(runtime_home)
    if not home.is_absolute():
        raise PayloadError(f"runtime home must be absolute: {home}")
    effective_source_root = (
        Path(source_root) if source_root is not None else model_config.repository_root()
    )
    try:
        cfg, receipt = model_config.resolve_config(
            "codex", runtime=home, environ=environ, source_root=source_root
        )
        rendered = renderer.render_agents(cfg, _kernel_names(effective_source_root))
    except (model_config.ModelConfigError, ValueError) as exc:
        raise PayloadError(str(exc)) from exc

    provenance = {"config_source": receipt.source, "config_reason": receipt.reason}
    digest = _digest(cfg, rendered, provenance)
    checksums = {
        name: hashlib.sha256(body.encode("utf-8")).hexdigest() for name, body in rendered.items()
    }
    target_dir = home / HARNESS_DIRNAME / NATIVE_AGENTS_DIRNAME / digest
    metadata = {
        "schema": PAYLOAD_SCHEMA,
        "runtime": "codex",
        "digest": digest,
        "renderer_version": renderer.RENDERER_VERSION,
        **provenance,
        "files": dict(sorted(checksums.items())),
    }
    return PayloadPlan(
        runtime="codex",
        runtime_home=home,
        digest=digest,
        target_dir=target_dir,
        files=dict(rendered),
        checksums=checksums,
        metadata=metadata,
        receipt=receipt.as_dict(),
    )


def expected_links(
    runtime_home: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    source_root: str | Path | None = None,
) -> dict[str, Path]:
    """Map each agent filename to its computed payload path. Never writes."""
    plan = plan_payload(runtime_home, environ=environ, source_root=source_root)
    return {name: plan.target_dir / name for name in plan.files}


def _assert_safe_containment(home: Path, target_dir: Path) -> None:
    root = home / HARNESS_DIRNAME / NATIVE_AGENTS_DIRNAME
    try:
        rel = target_dir.relative_to(root)
    except ValueError as exc:
        raise PayloadMaterializeError(f"payload target escapes its computed root: {target_dir}") from exc
    # Walk the *complete* path from the filesystem root through ``home`` itself
    # and on to ``target_dir``, so a symlinked runtime home or a symlinked
    # existing ancestor of it is refused just like a symlinked ``.harness`` or
    # digest directory. The same walked path is what ``materialize_payload``
    # then creates, so the path validated here is the path actually written to.
    current = Path(home.anchor)
    for part in (*home.parts[1:], HARNESS_DIRNAME, NATIVE_AGENTS_DIRNAME, *rel.parts):
        current = current / part
        if current.is_symlink():
            raise PayloadMaterializeError(f"unsafe symlink in native-agent payload path: {current}")
        if current.exists() and not current.is_dir() and current != target_dir:
            raise PayloadMaterializeError(f"non-directory blocks native-agent payload path: {current}")


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_metadata(target_dir: Path) -> dict | None:
    meta_path = target_dir / "metadata.json"
    if meta_path.is_symlink() or not meta_path.is_file():
        return None
    try:
        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _matches(existing: Mapping[str, object], plan: PayloadPlan) -> bool:
    if existing != plan.metadata:
        return False
    for name, checksum in plan.checksums.items():
        file_path = plan.target_dir / name
        if file_path.is_symlink() or not file_path.is_file():
            return False
        if hashlib.sha256(file_path.read_bytes()).hexdigest() != checksum:
            return False
    return True


def materialize_payload(plan: PayloadPlan, *, dry_run: bool = False) -> dict[str, object]:
    """Write ``plan``'s files and metadata under its digest directory.

    Idempotent: a digest directory that already holds byte-identical files and
    metadata is left untouched and reported ``unchanged``. This is the only
    function in this module that creates a directory or writes a file.
    """
    _assert_safe_containment(plan.runtime_home, plan.target_dir)

    if dry_run:
        return {
            "action": "materialize_native_agent_payload",
            "status": "planned",
            "digest": plan.digest,
            "target_dir": str(plan.target_dir),
        }

    existing = _read_metadata(plan.target_dir)
    if existing is not None and _matches(existing, plan):
        return {
            "action": "materialize_native_agent_payload",
            "status": "unchanged",
            "digest": plan.digest,
            "target_dir": str(plan.target_dir),
        }

    parent = plan.target_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    if plan.target_dir.is_symlink():
        raise PayloadMaterializeError(f"refusing symlinked payload directory: {plan.target_dir}")

    work_dir = Path(tempfile.mkdtemp(prefix=".native-agents.", dir=str(parent)))
    try:
        for name, body in plan.files.items():
            _write_atomic(work_dir / name, body.encode("utf-8"))
        _write_atomic(
            work_dir / "metadata.json",
            json.dumps(plan.metadata, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        os.chmod(work_dir, 0o755)
        try:
            work_dir.rename(plan.target_dir)
        except OSError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            refreshed = _read_metadata(plan.target_dir)
            if refreshed is None or not _matches(refreshed, plan):
                raise PayloadMaterializeError(f"failed to materialize payload: {exc}") from exc
            return {
                "action": "materialize_native_agent_payload",
                "status": "unchanged",
                "digest": plan.digest,
                "target_dir": str(plan.target_dir),
            }
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise

    return {
        "action": "materialize_native_agent_payload",
        "status": "created",
        "digest": plan.digest,
        "target_dir": str(plan.target_dir),
    }


def check_payload(
    runtime_home: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    source_root: str | Path | None = None,
) -> dict[str, object]:
    """Read-only: is the effective payload already materialized and intact?"""
    plan = plan_payload(runtime_home, environ=environ, source_root=source_root)
    existing = _read_metadata(plan.target_dir)
    ok = existing is not None and _matches(existing, plan)
    return {
        "ok": ok,
        "digest": plan.digest,
        "target_dir": str(plan.target_dir),
        "files": sorted(plan.files),
        "detail": (
            "native-agent payload materialized and verified"
            if ok
            else f"native-agent payload not materialized or stale at {plan.target_dir}"
        ),
    }


def verify_owned_target(runtime_home: str | Path, target: str | Path) -> bool:
    """Prove ``target`` is a file inside a self-consistent materialized payload
    directory under ``runtime_home``'s ``.harness/native-agents/<digest>/``.

    Ownership requires canonical containment *plus* a regular non-symlink
    metadata file whose schema/runtime/digest and per-file checksum all agree
    — never a ``.harness`` substring or directory-name match, which would let
    a foreign link be adopted (or a real one be removed as foreign).
    """
    try:
        home = Path(runtime_home).resolve()
        candidate = Path(target).resolve()
    except OSError:
        return False
    root = home / HARNESS_DIRNAME / NATIVE_AGENTS_DIRNAME
    try:
        root_resolved = root.resolve()
    except OSError:
        return False
    try:
        rel = candidate.relative_to(root_resolved)
    except ValueError:
        return False
    if len(rel.parts) != 2:
        return False
    digest_name, filename = rel.parts
    digest_dir = root_resolved / digest_name
    metadata = _read_metadata(digest_dir)
    if (
        metadata is None
        or metadata.get("schema") != PAYLOAD_SCHEMA
        or metadata.get("runtime") != "codex"
        or metadata.get("digest") != digest_name
    ):
        return False
    files = metadata.get("files")
    if not isinstance(files, dict) or filename not in files:
        return False
    file_path = digest_dir / filename
    if file_path.is_symlink() or not file_path.is_file():
        return False
    try:
        actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError:
        return False
    return actual == files[filename]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "links", "materialize", "check"))
    parser.add_argument("--runtime-home", required=True)
    parser.add_argument("--source-root")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.action == "plan":
            plan = plan_payload(args.runtime_home, source_root=args.source_root)
            print(json.dumps(
                {
                    "digest": plan.digest,
                    "target_dir": str(plan.target_dir),
                    "files": sorted(plan.files),
                    "receipt": plan.receipt,
                },
                indent=2,
                sort_keys=True,
            ))
            return 0
        if args.action == "links":
            # Read-only, shell-friendly: one "<name>\t<absolute path>" line per
            # agent, so install/uninstall shell scripts can loop without a JSON
            # parser. Never requires the payload to already be materialized.
            for name, path in sorted(expected_links(args.runtime_home, source_root=args.source_root).items()):
                print(f"{name}\t{path}")
            return 0
        if args.action == "check":
            result = check_payload(args.runtime_home, source_root=args.source_root)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ok"] else 1
        plan = plan_payload(args.runtime_home, source_root=args.source_root)
        result = materialize_payload(plan, dry_run=args.dry_run)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (PayloadError, PayloadMaterializeError) as exc:
        print(str(exc), file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
