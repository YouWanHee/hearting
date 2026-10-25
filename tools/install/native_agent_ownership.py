"""Read-only ownership proof and checked projection for Codex agent leaves.

The legacy payload format is accepted only as migration input.  Nothing here
renders agents or writes payloads or metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import safe_fs

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))
import harness_manifest

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[^/\\]+\.toml\Z")
_IDENTITY = {"schema", "runtime", "renderer_version", "config_source",
             "config_reason", "main_session_only_policy", "main_only", "files"}


def agent_leaf(home: Path, dest: Path) -> bool:
    return (dest.parent == home / "agents" and bool(_NAME.fullmatch(dest.name))
            and dest.name not in {".", ".."})


def _target(dest: Path) -> Path:
    raw = Path(os.readlink(dest))
    return Path(os.path.abspath(raw if raw.is_absolute() else dest.parent / raw))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metadata key")
        result[key] = value
    return result


def legacy_payload(home: Path, name: str, target: Path) -> bool:
    """Verify the old PayloadPlan.identity() digest and the actual file bytes."""
    if not target.is_absolute() or not _NAME.fullmatch(name):
        return False
    root = home / ".harness" / "native-agents"
    try:
        relative = target.relative_to(root)
        if len(relative.parts) != 2 or relative.parts[1] != name:
            return False
        digest = relative.parts[0]
        if not _HEX.fullmatch(digest):
            return False
        directory = root / digest
        for part in (home / ".harness", root, directory, target,
                     directory / "metadata.json"):
            if part.is_symlink():
                return False
        if not directory.is_dir() or not target.is_file():
            return False
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"),
                              object_pairs_hook=_unique)
        if not isinstance(metadata, dict) or set(metadata) != _IDENTITY | {"digest"}:
            return False
        if (metadata["schema"] != "hearting.native-agent-payload/v1"
                or metadata["runtime"] != "codex" or metadata["digest"] != digest
                or not all(isinstance(metadata[key], str) for key in (
                    "renderer_version", "config_source", "config_reason",
                    "main_session_only_policy"))):
            return False
        main_only = metadata["main_only"]
        files = metadata["files"]
        if (not isinstance(main_only, list) or not all(
                isinstance(item, dict) and all(isinstance(k, str) and isinstance(v, str)
                                               for k, v in item.items()) for item in main_only)
                or not isinstance(files, dict) or name not in files
                or not all(isinstance(k, str) and _NAME.fullmatch(k)
                           and isinstance(v, str) and _HEX.fullmatch(v)
                           for k, v in files.items())):
            return False
        identity = {key: metadata[key] for key in _IDENTITY}
        calculated = hashlib.sha256(json.dumps(identity, sort_keys=True,
                                                separators=(",", ":")).encode()).hexdigest()
        if calculated != digest or {part.name for part in directory.iterdir()} != (
                set(files) | {"metadata.json"}):
            return False
        for file_name, checksum in files.items():
            payload = directory / file_name
            if (payload.is_symlink() or not payload.is_file()
                    or hashlib.sha256(payload.read_bytes()).hexdigest() != checksum):
                return False
        return True
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError):
        return False


def _declared_codex_agent(root: Path, name: str, target: Path) -> bool:
    """Check the release identity and the agent it declares for Codex."""
    manifest = root / harness_manifest.MANIFEST_NAME
    if manifest.is_symlink() or not manifest.is_file():
        return False
    try:
        core = root / "core/CORE.md"
        if core.is_symlink() or not core.is_file():
            return False
        release = harness_manifest.load(manifest)
        if "adapters/codex/agents/*.toml" not in release["ownership"]["generated"]:
            return False
        canonical = root / "adapters/codex/agents" / name
        mirror = root / "codex_setting/codex-agents" / name
        if (target not in (canonical, mirror) or canonical.is_symlink()
                or not canonical.is_file()):
            return False
        if target == mirror and mirror.read_bytes() != canonical.read_bytes():
            return False
        if name[:-5] in release["kernel"]["agents"]:
            return True
        config_path = root / "adapters/codex/config/models.conf"
        if config_path.is_symlink():
            return False
        config = config_path.read_text(encoding="utf-8")
        for line in config.splitlines():
            if not line.strip().startswith("CFG_NATIVE_AGENT_CATALOG="):
                continue
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            for token in raw.split():
                agent, separator, profile = token.partition(":")
                if agent == name[:-5] and separator and profile:
                    return True
            return False
    except (harness_manifest.ManifestError, OSError, UnicodeError, ValueError,
            KeyError, TypeError, RuntimeError):
        return False
    return False


def shipped_target(name: str, target: Path) -> bool:
    """Recognize an agent declared by an intact source release."""
    if not target.is_absolute() or not _NAME.fullmatch(name):
        return False
    try:
        resolved = target.resolve(strict=True)
        if not resolved.is_file() or resolved.name != name:
            return False
        for root in resolved.parents:
            if _declared_codex_agent(root, name, resolved):
                return True
    except (OSError, RuntimeError):
        pass
    return False


def owned_link(home: Path, dest: Path) -> bool:
    if not agent_leaf(home, dest) or not dest.is_symlink():
        return False
    try:
        target = _target(dest)
        # Preserve a relative link to a foreign or dangling path.  Absolute
        # lexical containment is required for old payload identity.
        return legacy_payload(home, dest.name, target) or shipped_target(dest.name, target)
    except (OSError, RuntimeError):
        return False


def _activation_receipt_changed(home: Path, dest: Path,
                                state: safe_fs.PathState) -> bool:
    """Do not let a later installer adopt an activation-preserved successor."""
    receipt = home / ".harness" / "activation.json"
    if receipt.is_symlink() or not receipt.is_file():
        return False
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("runtime") != "codex":
            return False
        for item in data.get("owned_paths", []):
            if not isinstance(item, dict) or item.get("dest") != str(dest):
                continue
            if state.kind == "symlink":
                return item.get("kind") != "symlink" or item.get("source") != state.link_target
            if state.kind == "file":
                return item.get("kind") != "render" or item.get("content_sha256") != state.digest
            return False
    except (OSError, UnicodeError, ValueError, TypeError):
        return True
    return False


def classify(home: Path, source_root: Path, dest: Path, source: Path) -> str:
    """Return absent, unchanged-owned, replaceable-owned, or preserved."""
    if (not agent_leaf(home, dest) or source.name != dest.name
            or not source.is_absolute() or not source_root.is_absolute()
            or not source.is_relative_to(source_root)
            or not shipped_target(dest.name, source)):
        return "preserved"
    state = safe_fs.capture_state(dest)
    if state.kind == "missing":
        return "absent"
    if _activation_receipt_changed(home, dest, state):
        return "preserved"
    if state.kind != "symlink" or not owned_link(home, dest):
        return "preserved"
    return "unchanged-owned" if state.link_target == str(source) else "replaceable-owned"


def write_link(home: Path, source_root: Path, dest: Path, source: Path,
               *, dry_run: bool = False) -> dict:
    if not agent_leaf(home, dest):
        raise safe_fs.SafetyError("ownership-unproved", dest, "not a native agent leaf")
    def decide():
        status = classify(home, source_root, dest, source)
        return {"action": "symlink", "source": str(source), "dest": str(dest),
                "status": ("planned" if dry_run and status in {"absent", "replaceable-owned"}
                           else {"absent": "created", "unchanged-owned": "unchanged",
                                 "replaceable-owned": "updated", "preserved": "preserved"}[status]),
                "detail": status}
    if dry_run:
        return decide()
    with safe_fs.TargetLock(dest):
        result = decide()
        if result["status"] not in {"created", "updated"}:
            return result
        prior = safe_fs.capture_state(dest)
        # Recheck payload bytes and metadata immediately before the CAS write.
        if classify(home, source_root, dest, source) not in {
                "absent" if result["status"] == "created" else "replaceable-owned"}:
            raise safe_fs.SafetyError("expected-state-mismatch", dest, "ownership changed")
        auth = safe_fs.authority(dest, owner="native-agent-projection",
                                 allowed_paths=(dest,), expected=prior)
        safe_fs.atomic_write_symlink(auth, str(source), create_parents=True)
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = write_link(args.home, args.source_root,
                            args.home / "agents" / args.source.name, args.source)
    except (OSError, safe_fs.SafetyError) as exc:
        parser.exit(1, f"native-agent-projection: {exc}\n")
    print(result["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
