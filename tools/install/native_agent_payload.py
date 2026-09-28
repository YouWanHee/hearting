#!/usr/bin/env python3
"""Effective Codex native-agent payload: plan, materialize, check.

``adapters/codex/agents/*.toml`` are the shipped profiles: rendered from the
shipped ``models.conf`` by ``sync-native-agents.py`` and hardcoding its models.
Codex loads native agents from ``<runtime-home>/agents/``, so linking those
files there meant the user-owned ``agent-config/models.conf`` never reached the
native agents. This module renders the same agent set from the runtime home's
effective model config (``utilities/model_config.resolve_config``: the complete
user file as one unit, otherwise the complete shipped file, never merged) and,
when the result differs from the shipped profiles, keeps it as an immutable
payload under ``<runtime-home>/.harness/native-agents/<digest>/``. Install,
activation and refresh then link ``agents/*.toml`` to that payload instead of
to the shipped files.

* Planning never writes. ``materialize_payload`` is the only writer, and only
  install/activate/refresh call it.
* The payload is inactive, and callers keep linking the shipped files, when
  the shipped config is selected, when the user config renders exactly the
  shipped profiles, or when it cannot render them (reported, whole-file
  fallback).
* A model listed in the selected config's ``CFG_MAIN_SESSION_ONLY_MODELS``
  never enters a payload. That agent keeps its shipped profile, or is withheld
  when the shipped model is main-only too; the plan reports every such agent.
* An installed payload file is recognized as harness-owned only by canonical
  containment under this runtime home plus metadata digest and per-file
  checksum agreement, never by a path substring.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_ROOT / "tools", _ROOT / "utilities"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))
_RENDERER_DIR = _ROOT / "adapters" / "codex" / "bin"
if str(_RENDERER_DIR) not in sys.path:
    sys.path.append(str(_RENDERER_DIR))

import harness_manifest  # noqa: E402
import model_config  # noqa: E402
import native_agent_renderer as renderer  # noqa: E402


SCHEMA = "hearting.native-agent-payload/v1"
RUNTIME = "codex"
MAIN_ONLY_KEY = "CFG_MAIN_SESSION_ONLY_MODELS"
METADATA_NAME = "metadata.json"
_MODEL_LINE = re.compile(r'^model = "((?:[^"\\]|\\.)*)"$', re.MULTILINE)


class PayloadError(RuntimeError):
    """A payload could not be planned or materialized safely."""


def payload_root(runtime_home: str | Path) -> Path:
    return Path(runtime_home) / ".harness" / "native-agents"


@dataclass(frozen=True)
class PayloadPlan:
    runtime_home: Path
    active: bool
    reason: str
    config_source: str
    config_reason: str
    main_session_only_policy: str
    main_only: tuple = ()
    files: Mapping[str, str] = field(default_factory=dict)
    digest: str = ""

    @property
    def target_dir(self) -> Optional[Path]:
        return payload_root(self.runtime_home) / self.digest if self.active else None

    def links(self) -> dict[str, Path]:
        """Agent filename -> payload file; empty while the shipped files stay linked."""
        if not self.active:
            return {}
        return {name: self.target_dir / name for name in sorted(self.files)}

    def identity(self) -> dict:
        return {
            "schema": SCHEMA,
            "runtime": RUNTIME,
            "renderer_version": renderer.RENDERER_VERSION,
            "config_source": self.config_source,
            "config_reason": self.config_reason,
            "main_session_only_policy": self.main_session_only_policy,
            "main_only": [dict(item) for item in self.main_only],
            "files": {name: _sha256(body.encode("utf-8")) for name, body in sorted(self.files.items())},
        }

    def metadata(self) -> dict:
        return {**self.identity(), "digest": self.digest}

    def report(self) -> dict:
        return {
            "active": self.active,
            "reason": self.reason,
            "config_source": self.config_source,
            "config_reason": self.config_reason,
            "main_session_only_policy": self.main_session_only_policy,
            "main_only": [dict(item) for item in self.main_only],
            "digest": self.digest or None,
            "target_dir": str(self.target_dir) if self.active else None,
            "agents": sorted(self.files) if self.active else [],
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _declared_model(body: Optional[str]) -> Optional[str]:
    if body is None:
        return None
    match = _MODEL_LINE.search(body)
    return match.group(1).replace('\\"', '"').replace("\\\\", "\\") if match else None


def _shipped_profiles(source_root: Path) -> dict[str, str]:
    directory = source_root / "adapters" / "codex" / "agents"
    if not directory.is_dir():
        return {}
    profiles = {}
    for path in sorted(directory.glob("*.toml")):
        try:
            profiles[path.name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
    return profiles


def _kernel_names(source_root: Path) -> list[str]:
    """Kernel helpers of this source tree (same fallback as runtime activation)."""
    manifest_path = source_root / harness_manifest.MANIFEST_NAME
    if not manifest_path.is_file():
        return ["memory-scout"]
    try:
        return sorted(harness_manifest.load(manifest_path)["kernel"]["agents"])
    except (harness_manifest.ManifestError, KeyError, TypeError) as exc:
        raise renderer.RenderError(f"invalid canonical manifest: {exc}") from exc


def plan_payload(
    runtime_home: str | Path,
    *,
    source_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> PayloadPlan:
    """Decide which native-agent files this runtime home should link. Never writes."""
    home = Path(runtime_home)
    if not home.is_absolute():
        raise PayloadError(f"runtime home must be absolute: {home}")
    root = Path(source_root) if source_root is not None else _ROOT

    def inactive(reason, source, config_reason, policy, main_only=()):
        return PayloadPlan(home, False, reason, source, config_reason, policy, tuple(main_only))

    try:
        cfg, receipt = model_config.resolve_config(
            RUNTIME, runtime=home, environ=environ, source_root=root
        )
    except model_config.ModelConfigError as exc:
        return inactive(f"model-config-unavailable: {exc}", "unavailable", "shipped-unusable", "absent")
    policy = "declared" if MAIN_ONLY_KEY in cfg else "absent"
    if receipt.source != "user":
        return inactive("shipped-config-selected", receipt.source, receipt.reason, policy)

    shipped = _shipped_profiles(root)
    try:
        rendered = renderer.render_agents(cfg, _kernel_names(root))
    except renderer.RenderError as exc:
        return inactive(f"user-config-unrenderable: {exc}", receipt.source, receipt.reason, policy)

    restricted = cfg.get(MAIN_ONLY_KEY, "")
    files: dict[str, str] = {}
    main_only: list[dict] = []
    for name, (model, body) in rendered.items():
        if not model_config.restricted_model(model, restricted):
            files[name] = body
            continue
        fallback = shipped.get(name)
        fallback_model = _declared_model(fallback)
        if fallback_model and not model_config.restricted_model(fallback_model, restricted):
            files[name] = fallback
            main_only.append({"agent": name, "model": model, "resolution": "shipped-profile",
                              "shipped_model": fallback_model})
        else:
            main_only.append({"agent": name, "model": model, "resolution": "withheld"})

    if files == shipped:
        return inactive("user-config-matches-shipped", receipt.source, receipt.reason, policy, main_only)
    plan = PayloadPlan(home, True, "user-config-differs", receipt.source, receipt.reason,
                       policy, tuple(main_only), dict(sorted(files.items())))
    digest = _sha256(json.dumps(plan.identity(), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return PayloadPlan(home, True, plan.reason, plan.config_source, plan.config_reason,
                       plan.main_session_only_policy, plan.main_only, plan.files, digest)


def agent_links(
    runtime_home: str | Path,
    *,
    source_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Path]:
    """Payload link targets, or ``{}`` when the shipped files stay linked. Never writes."""
    return plan_payload(runtime_home, source_root=source_root, environ=environ).links()


def _read_metadata(directory: Path) -> Optional[dict]:
    path = directory / METADATA_NAME
    if path.is_symlink() or not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _matches(plan: PayloadPlan) -> bool:
    directory = plan.target_dir
    if directory is None or directory.is_symlink() or not directory.is_dir():
        return False
    if _read_metadata(directory) != plan.metadata():
        return False
    for name, body in plan.files.items():
        path = directory / name
        if path.is_symlink() or not path.is_file():
            return False
        try:
            if path.read_bytes() != body.encode("utf-8"):
                return False
        except OSError:
            return False
    return True


def _assert_safe_parents(plan: PayloadPlan) -> None:
    """Refuse a symlinked or non-directory component below the runtime home."""
    current = plan.runtime_home
    for part in (".harness", "native-agents", plan.digest):
        current = current / part
        if current.is_symlink():
            raise PayloadError(f"unsafe symlink in native-agent payload path: {current}")
        if current.exists() and not current.is_dir():
            raise PayloadError(f"non-directory blocks native-agent payload path: {current}")


def _action(plan: PayloadPlan, status: str, detail: str) -> dict:
    return {
        "action": "native-agent-payload",
        "dest": str(plan.target_dir or plan.runtime_home / "agents"),
        "status": status,
        "detail": detail,
        "payload": plan.report(),
    }


def materialize_payload(plan: PayloadPlan, *, dry_run: bool = False) -> dict:
    """Write an active plan's files once under its digest directory.

    Idempotent: a digest directory that already holds exactly the planned
    files and metadata is reported ``unchanged``. An existing directory with
    other content is refused, never overwritten or removed.
    """
    if not plan.active:
        return _action(plan, "inactive", f"shipped native agents stay linked ({plan.reason})")
    _assert_safe_parents(plan)
    if _matches(plan):
        return _action(plan, "unchanged", "native-agent payload already installed")
    if plan.target_dir.exists():
        raise PayloadError(
            f"native-agent payload directory exists with other content; refusing to overwrite: {plan.target_dir}"
        )
    if dry_run:
        return _action(plan, "planned", "dry-run")
    parent = plan.target_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_parents(plan)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(parent)))
    try:
        for name, body in plan.files.items():
            _write_new(staging / name, body.encode("utf-8"))
        _write_new(
            staging / METADATA_NAME,
            json.dumps(plan.metadata(), indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        os.chmod(staging, 0o755)
        try:
            # destructive-ok: reason=publish a fully written staging payload under its content digest; boundary=mkdtemp staging sibling created above renamed onto a digest path verified absent
            os.rename(staging, plan.target_dir)
        except OSError as exc:
            if not _matches(plan):
                raise PayloadError(f"failed to install native-agent payload: {exc}") from exc
            return _action(plan, "unchanged", "native-agent payload installed concurrently")
    finally:
        if staging.exists():
            # destructive-ok: reason=discard an unpublished staging payload; boundary=mkdtemp staging directory created by this call
            shutil.rmtree(staging, ignore_errors=True)
    return _action(plan, "created", "native-agent payload installed")


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def check_payload(
    runtime_home: str | Path,
    *,
    source_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Read-only: is the planned payload installed (or not needed)?"""
    plan = plan_payload(runtime_home, source_root=source_root, environ=environ)
    if not plan.active:
        ok, detail = True, f"shipped native agents apply ({plan.reason})"
    elif _matches(plan):
        ok, detail = True, f"native-agent payload installed at {plan.target_dir}"
    else:
        ok, detail = False, f"native-agent payload missing or stale at {plan.target_dir}; rerun install or refresh"
    return {"ok": ok, "detail": detail, "payload": plan.report()}


def owned_payload_file(runtime_home: str | Path, target: str | Path) -> bool:
    """Whether ``target`` is an intact file of a payload installed under this home."""
    try:
        root = payload_root(Path(runtime_home).resolve())
        candidate = Path(target).resolve()
        relative = candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    if len(relative.parts) != 2 or relative.parts[1] == METADATA_NAME:
        return False
    digest, name = relative.parts
    metadata = _read_metadata(root / digest)
    if (
        metadata is None
        or metadata.get("schema") != SCHEMA
        or metadata.get("runtime") != RUNTIME
        or metadata.get("digest") != digest
        or not isinstance(metadata.get("files"), dict)
        or name not in metadata["files"]
        or candidate.is_symlink()
        or not candidate.is_file()
    ):
        return False
    try:
        return _sha256(candidate.read_bytes()) == metadata["files"][name]
    except OSError:
        return False


def owned_agent_target(
    runtime_home: str | Path, source_root: str | Path, name: str, target: str | Path
) -> bool:
    """Whether an ``agents/<name>`` link target is a harness-owned native agent file:
    the shipped profile of this source or an intact payload file of the same name."""
    try:
        resolved = Path(target).resolve()
        shipped = (Path(source_root) / "adapters" / "codex" / "agents" / name).resolve()
    except (OSError, RuntimeError):
        return False
    if resolved == shipped:
        return True
    return resolved.name == name and owned_payload_file(runtime_home, resolved)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("plan", "links", "materialize", "check"))
    parser.add_argument("--runtime-home", required=True)
    parser.add_argument("--source-root")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = plan_payload(args.runtime_home, source_root=args.source_root)
        if args.action == "plan":
            print(json.dumps(plan.report(), indent=2, sort_keys=True))
            return 0
        if args.action == "links":
            # One "<name>\t<path>" line per payload agent; no output means the
            # shipped files stay linked. Shell installers read this as-is.
            for name, path in plan.links().items():
                print(f"{name}\t{path}")
            return 0
        if args.action == "check":
            result = check_payload(args.runtime_home, source_root=args.source_root)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ok"] else 1
        print(json.dumps(materialize_payload(plan, dry_run=args.dry_run), sort_keys=True))
        return 0
    except PayloadError as exc:
        print(str(exc), file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
