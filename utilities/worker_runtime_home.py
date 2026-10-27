"""Runtime-home projection shared by registered workers and synchronous callers."""
import importlib.util
import json
import os
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / 'tools/profile/build-home.py'
_spec = importlib.util.spec_from_file_location('hearting_worker_home_builder', _path)
_builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_builder)

prepare_worker_home = _builder.build_worker_home


def recorded_codex_worker_environment(agent_home, worker_home, identity):
    """Read the exact Codex worker environment from an existing home.

    Recovery must not call ``build_worker_home``: that builder seeds links and
    files. This projection only reads the manifest, its recorded source and the
    config-home link provenance, then reuses the builder's pure value renderer.
    """
    home = Path(worker_home)
    source_release = Path(agent_home)
    if (not home.is_absolute() or home.is_symlink() or not home.is_dir()
            or not source_release.is_absolute() or not source_release.is_dir()):
        raise ValueError("worker-home-recovery-path-unavailable")
    manifest_path = home / "worker-home.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("worker-home-manifest-unavailable")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("worker-home-manifest-invalid") from exc
    if (not isinstance(manifest, dict) or manifest.get("schema") != 2
            or manifest.get("harness") != "codex"
            or manifest.get("worker_type") != "owner"
            or manifest.get("identity") != identity
            or Path(str(manifest.get("source") or "")).resolve(strict=False)
               != source_release.resolve(strict=True)
            or manifest.get("key") != _builder.worker_home_key(
                source_release, "codex", "owner", identity)):
        raise ValueError("worker-home-manifest-identity-mismatch")

    config_value = manifest.get("codex_config_home")
    if config_value:
        config_home = Path(config_value)
        if not config_home.is_absolute():
            raise ValueError("worker-home-config-path-invalid")
    else:
        # Older homes did not persist this path. Recover it only from the
        # original absolute symlink that the normal builder placed in the home.
        config_link = home / "config.toml"
        if not config_link.is_symlink():
            raise ValueError("worker-home-config-source-unavailable")
        target = Path(os.readlink(config_link))
        if not target.is_absolute():
            raise ValueError("worker-home-config-source-unavailable")
        config_home = target.parent

    home_value = manifest.get("home_environment")
    if home_value:
        home_environment = Path(home_value)
        if not home_environment.is_absolute():
            raise ValueError("worker-home-home-path-invalid")
    elif config_home.name == ".codex":
        # This is the only legacy derivation supported: the exact linked
        # ``<home>/.codex/config.toml`` path pins its parent without consulting
        # the recovery process's current HOME default.
        home_environment = config_home.parent
    else:
        raise ValueError("worker-home-home-path-unavailable")

    config_file = home / "config.toml"
    source_config = config_home / "config.toml"
    if source_config.is_file():
        if not config_file.is_symlink():
            raise ValueError("worker-home-config-link-changed")
        target = Path(os.readlink(config_file))
        if not target.is_absolute():
            target = config_file.parent / target
        if target.resolve(strict=False) != source_config.resolve(strict=False):
            raise ValueError("worker-home-config-link-changed")
    elif config_file.exists() or config_file.is_symlink():
        raise ValueError("worker-home-config-link-changed")

    values = _builder.codex_worker_launch_values(
        source_release.resolve(strict=True), config_home, home, "owner", home_environment,
    )
    values["HOME"] = str(home_environment)
    return values


def codex_worker_arguments(env=None):
    env = os.environ if env is None else env
    overrides = json.loads(env.get('HEARTING_CODEX_WORKER_OVERRIDES', '[]'))
    if env.get('HEARTING_WORKER_HOME'):
        state = _builder.worker_hook_state_override(env['CODEX_HOME'],
                         json.loads(env.get('HEARTING_CODEX_HOOK_SOURCES', '[]')))
        if state:
            overrides.append(state)
    return [item for value in overrides
            for item in ('-c', value)]


def claude_worker_arguments(env=None):
    env = os.environ if env is None else env
    if not env.get('HEARTING_WORKER_HOME'):
        return []
    return ['--disable-slash-commands', '--strict-mcp-config',
            '--mcp-config', '{"mcpServers":{}}']
