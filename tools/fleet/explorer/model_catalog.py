"""Read model choices from already-authenticated runtime catalog caches only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from .types import ModelCatalog

_MAX_BYTES = 16 * 1024 * 1024


def _json(path):
    try:
        if Path(path).stat().st_size > _MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, UnicodeError, TypeError):
        return None


def _ids(value):
    """Extract model identifiers from the bounded public catalog record shapes."""
    found = []
    seen = set()

    def add(item, fallback=""):
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = next((item.get(key) for key in ("id", "slug", "model")
                         if isinstance(item.get(key), str) and item.get(key).strip()), fallback)
            if not name and isinstance(item.get("name"), str):
                name = item["name"].strip()
        else:
            name = ""
        if name and name not in seen:
            seen.add(name)
            found.append(name)

    if isinstance(value, list):
        for item in value:
            add(item)
    elif isinstance(value, dict):
        direct = value.get("models")
        if isinstance(direct, list):
            for item in direct:
                add(item)
        elif isinstance(direct, dict):
            for key, item in direct.items():
                add(item if isinstance(item, dict) else key)
        providers = value.get("providers")
        if isinstance(providers, dict):
            for provider in providers.values():
                models = provider.get("models") if isinstance(provider, dict) else None
                if isinstance(models, dict):
                    for key, item in models.items():
                        add(item, key)
                elif isinstance(models, list):
                    for item in models:
                        add(item)
    return tuple(found)


def _candidates(harness):
    home = Path.home()
    if harness == "claude":
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        return (config / "cache" / "model-catalog.json",
                config / "cache" / "models.json")
    if harness == "codex":
        root = Path(os.environ.get("CODEX_HOME") or home / ".codex")
        return (root / "models_cache.json",)
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return (data_home / "opencode" / "models.json",)


def read_catalog(harness):
    """Return one source-qualified catalog; absence and parse failure stay unknown."""
    for candidate in _candidates(harness):
        path = Path(candidate)
        document = _json(path)
        if document is None:
            continue
        models = _ids(document)
        if models:
            return ModelCatalog(harness=harness, state="available", source=str(path),
                                models=models, observed_at=path.stat().st_mtime)
        return ModelCatalog(harness=harness, state="empty", source=str(path),
                            observed_at=path.stat().st_mtime)
    return ModelCatalog(harness=harness)


def read_catalogs():
    """Read the three runtime cache files without invoking harnesses or the network."""
    return {harness: read_catalog(harness) for harness in ("claude", "codex", "opencode")}
