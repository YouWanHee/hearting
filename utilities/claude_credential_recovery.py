"""Resolve a Claude worker's native auth store without guessing token lineage.

Legacy worker credentials may be the only surviving copy of a rotated token,
or may be stale relative to a later main login. The file format does not carry
trusted generation ordering, so this module follows explicit configuration,
builder metadata, and intact credential symlinks. A marked legacy worker with
no known origin keeps its selected config directory as a local cohort store;
that does not promote its credential into main authentication. This module
never promotes or deletes a credential file.
"""
import json
from pathlib import Path


SECURE_STORAGE_ENV = "CLAUDE_SECURESTORAGE_CONFIG_DIR"


def _normalize(value, cwd):
    """Keep the native empty-value meaning and anchor relative paths at caller cwd."""
    if value == "":
        return ""
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((Path(cwd) / path).resolve(strict=False))


def _worker_metadata(config_home):
    path = Path(config_home) / "worker-home.json"
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(metadata, dict) and metadata.get("harness") == "claude":
        return metadata
    return None


def resolve_storage_binding(env, config_home, *, settings_env=None, cwd=None):
    """Return the selected native store without guessing legacy token lineage.

    Claude settings' `env` is applied over the inherited launch environment, so
    its explicit value is considered first. Empty strings remain meaningful and
    are not replaced by a default path. If a marked legacy worker has no stored
    origin or intact credential link, its selected config directory remains a
    local cohort store; it is never treated as the main account's canonical
    store.
    """
    cwd = Path.cwd() if cwd is None else Path(cwd)
    settings_env = settings_env if isinstance(settings_env, dict) else {}
    if SECURE_STORAGE_ENV in settings_env:
        return _normalize(str(settings_env[SECURE_STORAGE_ENV]), cwd)
    if SECURE_STORAGE_ENV in env:
        return _normalize(str(env[SECURE_STORAGE_ENV]), cwd)

    config_home = Path(config_home)
    metadata = _worker_metadata(config_home)
    if metadata is not None:
        inherited = metadata.get("claude_secure_storage_dir")
        if isinstance(inherited, str):
            return _normalize(inherited, cwd)

    legacy_credentials = config_home / ".credentials.json"
    if legacy_credentials.is_symlink():
        target = legacy_credentials.resolve(strict=False)
        return _normalize(str(target.parent), cwd)

    if metadata is not None:
        # Keep auth continuity within this selected legacy cohort. The CLI
        # owns refresh and locking in this directory; no credential is copied,
        # promoted to main, or fabricated when the directory is empty.
        return _normalize(str(config_home.resolve(strict=False)), cwd)

    default_home = Path(env.get("HOME") or str(Path.home())) / ".claude"
    resolved_config = config_home.resolve(strict=False)
    if resolved_config == default_home.resolve(strict=False):
        # Preserve Claude's native default service/path semantics (notably on
        # macOS), instead of spelling the default as a custom absolute path.
        return ""
    return str(resolved_config)
