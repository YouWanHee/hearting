"""Installer bridge to Fleet's single preference reader and create-once template."""
import importlib.util
from pathlib import Path


def _module():
    spec = importlib.util.spec_from_file_location(
        # The running installer owns this reader. AGENT_HOME can instead name
        # an activation target (including a minimal/older source without Fleet).
        "_hearting_fleet_config", Path(__file__).resolve().parents[1] / "fleet/config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure(*, dry_run=False):
    return _module().ensure(dry_run=dry_run)


def validate():
    return _module().validate()
