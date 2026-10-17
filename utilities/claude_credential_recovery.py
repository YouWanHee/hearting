"""Resolve a Claude worker's native auth store without guessing token lineage.

Legacy worker credentials may be the only surviving copy of a rotated token,
or may be stale relative to a later main login. The file format does not carry
trusted generation ordering, so this module follows explicit configuration,
builder metadata, intact credential symlinks, and the source config home the
legacy builder recorded in the worker's own launch environment. A credential
file left in a worker home is never selected as an auth source, copied,
promoted, or deleted.
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


def _store(env, directory):
    """Spell HOME/.claude as the native default so macOS keeps its service name."""
    default_home = Path(env.get("HOME") or str(Path.home())) / ".claude"
    resolved = Path(directory).resolve(strict=False)
    if resolved == default_home.resolve(strict=False):
        return ""
    return str(resolved)


def _legacy_source_store(env, config_home, cwd):
    """Return the main store of a legacy worker whose generated link was replaced.

    The legacy builder emitted HEARTING_WORKER_HOME and HEARTING_MODEL_CONFIG_HOME
    together with that worker's CLAUDE_CONFIG_DIR, and linked credentials from
    the same source config home. Without that paired environment, use the
    builder's own HOME/.claude fallback.
    """
    worker_home = env.get("HEARTING_WORKER_HOME")
    source = env.get("HEARTING_MODEL_CONFIG_HOME")
    resolved_config = config_home.resolve(strict=False)
    if worker_home and source and \
            Path(worker_home).resolve(strict=False) == resolved_config and \
            Path(source).resolve(strict=False) != resolved_config:
        unpaired = {key: value for key, value in env.items()
                    if key not in ("HEARTING_WORKER_HOME", "HEARTING_MODEL_CONFIG_HOME")}
        return resolve_storage_binding(unpaired, Path(source), cwd=cwd)
    return ""


def resolve_storage_binding(env, config_home, *, settings_env=None, cwd=None):
    """Return the selected native store without guessing legacy token lineage.

    Claude settings' `env` is applied over the inherited launch environment, so
    its explicit value is considered first. Empty strings remain meaningful and
    are not replaced by a default path. If a marked legacy worker has no stored
    origin or intact credential link, its builder's source store is used; a
    credential file left in that worker home is preserved but never selected.
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
        return _store(env, legacy_credentials.resolve(strict=False).parent)

    if metadata is not None:
        # Native refresh replaced this worker's generated link, or it never had
        # one. Its file may be stale or the only copy of a newer token, so it
        # stays in place and new launches use the source main store instead.
        return _legacy_source_store(env, config_home, cwd)

    # Preserve Claude's native default service/path semantics (notably on
    # macOS), instead of spelling the default as a custom absolute path.
    return _store(env, config_home)
