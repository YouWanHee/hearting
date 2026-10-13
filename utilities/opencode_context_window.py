"""Resolve and cap OpenCode's installed native model limits before launch."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

try:
    from model_config import parse_context_window
except ImportError:  # package import in focused unit tests
    from utilities.model_config import parse_context_window


class OpenCodeContextError(ValueError):
    """The selected OpenCode model's effective native limit cannot be verified."""


def _integer_limit(limits: Mapping[str, object], key: str, *, required: bool) -> int | None:
    value = limits.get(key)
    if value is None and not required:
        return None
    if type(value) is not int or value < 0:
        raise OpenCodeContextError(f"OpenCode native model limit.{key} is unavailable or invalid")
    return value


def _json_objects(output: str):
    decoder = json.JSONDecoder()
    cursor = 0
    while True:
        start = output.find("{", cursor)
        if start < 0:
            return
        try:
            value, end = decoder.raw_decode(output, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        cursor = end
        if isinstance(value, dict):
            yield value


def _run(command: list[str], *, cwd: str, env: Mapping[str, str]) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, env=dict(env), text=True,
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenCodeContextError(f"cannot inspect installed OpenCode model limits: {exc}") from exc
    if result.returncode != 0:
        raise OpenCodeContextError(
            f"installed OpenCode limit inspection exited {result.returncode}"
        )
    return result.stdout


def _installed_command(command: str | None = None) -> str:
    if command:
        return command
    return shutil.which("opencode") or "opencode"


def _resolved_config(*, cwd: str, env: Mapping[str, str], command: str) -> dict[str, object]:
    output = _run([command, "debug", "config"], cwd=cwd, env=env)
    try:
        config = json.loads(output)
    except json.JSONDecodeError as exc:
        raise OpenCodeContextError("installed OpenCode did not return resolved JSON config") from exc
    if not isinstance(config, dict):
        raise OpenCodeContextError("installed OpenCode did not return a resolved JSON object")
    return config


def _model_limits(config: Mapping[str, object], model: str) -> dict[str, int | None]:
    provider, model_id = model.split("/", 1)
    providers = config.get("provider")
    provider_config = providers.get(provider) if isinstance(providers, dict) else None
    models = provider_config.get("models") if isinstance(provider_config, dict) else None
    model_config = models.get(model_id) if isinstance(models, dict) else None
    limits = model_config.get("limit") if isinstance(model_config, dict) else None
    if limits is None:
        limits = {}
    if not isinstance(limits, dict):
        raise OpenCodeContextError(f"resolved OpenCode model {model} has an invalid limit object")
    return {
        "context": _integer_limit(limits, "context", required=False),
        "input": _integer_limit(limits, "input", required=False),
        "output": _integer_limit(limits, "output", required=False),
    }


def _minimum_limits(*sources: Mapping[str, int | None]) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for key in ("context", "input", "output"):
        values = [source[key] for source in sources if source.get(key) is not None]
        result[key] = min(values) if values else None
    if result["context"] is None:
        raise OpenCodeContextError("resolved OpenCode model has no native context limit")
    return result


def _verify_final_limits(model: str, applied: Mapping[str, int | None],
                         final: Mapping[str, int | None]) -> None:
    """Reject a higher-priority managed layer that defeats the inline cap."""
    for key in ("context", "input", "output"):
        cap = applied[key]
        if cap is None:
            continue
        actual = final[key]
        if actual is None:
            raise OpenCodeContextError(
                f"final OpenCode model {model} limit.{key} is missing after context override"
            )
        if actual > cap:
            raise OpenCodeContextError(
                f"final OpenCode model {model} limit.{key}={actual} exceeds applied cap {cap}; "
                "a higher-priority managed setting overrides OPENCODE_CONFIG_CONTENT"
            )


def _models_dev_cache_path(env: Mapping[str, str]) -> Path:
    explicit = env.get("OPENCODE_MODELS_PATH")
    if explicit:
        return Path(explicit).expanduser()
    cache_dir = env.get("HEARTING_OPENCODE_MODELS_CACHE")
    if not cache_dir:
        home = Path(env.get("HOME") or Path.home()).expanduser()
        cache_dir = str(Path(env.get("XDG_CACHE_HOME") or home / ".cache") / "opencode")
    path = Path(cache_dir).expanduser()
    return path if path.name == "models.json" else path / "models.json"


def _native_limits(model: str, *, env: Mapping[str, str]) -> dict[str, int | None]:
    """Read OpenCode's installed models.dev snapshot before user overrides."""
    if not isinstance(model, str) or model.count("/") < 1:
        raise OpenCodeContextError("selected OpenCode model must use provider/model syntax")
    provider, model_id = model.split("/", 1)
    if not provider or not model_id:
        raise OpenCodeContextError("selected OpenCode model must use provider/model syntax")
    cache = _models_dev_cache_path(env)
    try:
        if cache.is_symlink() or not cache.is_file() or cache.stat().st_size > 64 * 1024 * 1024:
            raise OpenCodeContextError("installed models.dev cache is missing or unsafe")
        catalog = json.loads(cache.read_text(encoding="utf-8"))
    except OpenCodeContextError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OpenCodeContextError(f"cannot read installed models.dev cache: {exc}") from exc
    provider_record = catalog.get(provider) if isinstance(catalog, dict) else None
    models = provider_record.get("models") if isinstance(provider_record, dict) else None
    model_record = models.get(model_id) if isinstance(models, dict) else None
    if not isinstance(model_record, dict):
        raise OpenCodeContextError(f"installed models.dev cache has no standard model record for {model}")
    limits = model_record.get("limit")
    if not isinstance(limits, dict):
        raise OpenCodeContextError(f"models.dev record for {model} has no native limit object")
    return {
        "context": _integer_limit(limits, "context", required=True),
        "input": _integer_limit(limits, "input", required=False),
        "output": _integer_limit(limits, "output", required=False),
    }


def apply_context_override(config_content: str, model: str, context_window: str | int,
                           native_limits: Mapping[str, int | None]) -> tuple[str, dict[str, int | None]]:
    """Add only selected-model limit fields, preserving every other config value."""
    raw = config_content.strip()
    try:
        config = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise OpenCodeContextError(f"OPENCODE_CONFIG_CONTENT is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise OpenCodeContextError("OPENCODE_CONFIG_CONTENT must contain a JSON object")
    try:
        requested = int(parse_context_window(str(context_window)), 10)
    except ValueError as exc:
        raise OpenCodeContextError(f"resolved context window is invalid: {exc}") from exc
    native_context = _integer_limit(native_limits, "context", required=True)
    native_input = _integer_limit(native_limits, "input", required=False)
    native_output = _integer_limit(native_limits, "output", required=False)
    provider, model_id = model.split("/", 1)

    providers = config.setdefault("provider", {})
    if not isinstance(providers, dict):
        raise OpenCodeContextError("OPENCODE_CONFIG_CONTENT provider must be an object")
    provider_config = providers.setdefault(provider, {})
    if not isinstance(provider_config, dict):
        raise OpenCodeContextError(f"OpenCode provider {provider} must be an object")
    models = provider_config.setdefault("models", {})
    if not isinstance(models, dict):
        raise OpenCodeContextError(f"OpenCode provider {provider} models must be an object")
    model_config = models.setdefault(model_id, {})
    if not isinstance(model_config, dict):
        raise OpenCodeContextError(f"OpenCode model {model} config must be an object")
    limits = model_config.setdefault("limit", {})
    if not isinstance(limits, dict):
        raise OpenCodeContextError(f"OpenCode model {model} limit must be an object")
    existing_context = _integer_limit(limits, "context", required=False)
    existing_input = _integer_limit(limits, "input", required=False)
    # The installed 1.19.0 consumer prefers limit.input to limit.context.
    # Preserve a smaller configured context cap. The installed consumer uses
    # `input` independently and prefers it for compaction, so that ceiling is
    # capped below without rewriting context to the native input ceiling.
    context_caps = [requested, native_context]
    if existing_context is not None:
        context_caps.append(existing_context)
    context = min(context_caps)
    limits["context"] = context
    input_cap = None
    if native_input is not None or existing_input is not None:
        input_caps = [context]
        if native_input is not None:
            input_caps.append(native_input)
        if existing_input is not None:
            input_caps.append(existing_input)
        input_cap = min(input_caps)
        limits["input"] = input_cap
    existing_output = _integer_limit(limits, "output", required=False)
    output_caps = [value for value in (native_output, existing_output) if value is not None]
    output_cap = min(output_caps) if output_caps else None
    if output_cap is not None:
        limits["output"] = output_cap
    result = {"context": context, "input": input_cap, "output": output_cap}
    return json.dumps(config, ensure_ascii=False, separators=(",", ":")), result


def prepare_launch_context(*, model: str, context_window: str | int,
                           config_content: str, cwd: str,
                           environ: Mapping[str, str] | None = None,
                           command: str | None = None) -> dict[str, object]:
    """Inspect the installed runtime's effective model limit and prepare its cap."""
    native_command = _installed_command(command)
    env = dict(os.environ if environ is None else environ)
    env["OPENCODE_CONFIG_CONTENT"] = config_content
    before_config = _resolved_config(cwd=cwd, env=env, command=native_command)
    selected_model = before_config.get("model") if model == "inherit" else model
    if not isinstance(selected_model, str) or selected_model.count("/") < 1:
        raise OpenCodeContextError("selected OpenCode model is not declared by resolved native config")
    catalog_limits = _native_limits(selected_model, env=env)
    configured_limits = _model_limits(before_config, selected_model)
    limits = _minimum_limits(catalog_limits, configured_limits)
    updated, applied = apply_context_override(config_content, selected_model, context_window, limits)
    final_env = {**env, "OPENCODE_CONFIG_CONTENT": updated}
    after_config = _resolved_config(cwd=cwd, env=final_env, command=native_command)
    final_model = after_config.get("model")
    if model == "inherit" and final_model != selected_model:
        raise OpenCodeContextError(
            f"final OpenCode model {final_model!r} differs from capped inherited model {selected_model!r}"
        )
    final_limits = _model_limits(after_config, selected_model)
    _verify_final_limits(selected_model, applied, final_limits)
    return {
        "config_content": updated,
        "model": selected_model,
        "catalog_limits": catalog_limits,
        "configured_limits": configured_limits,
        "native_limits": limits,
        "applied_limits": applied,
        "final_limits": final_limits,
    }
