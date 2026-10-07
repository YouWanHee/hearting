"""Installer bridge to Fleet's single preference reader and create-once template."""
import importlib.util
import paths


def _module():
    spec = importlib.util.spec_from_file_location(
        "_hearting_fleet_config", paths.agent_home() / "tools/fleet/config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure(*, dry_run=False):
    return _module().ensure(dry_run=dry_run)


def validate():
    return _module().validate()
