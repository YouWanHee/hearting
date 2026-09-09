#!/usr/bin/env python3
"""Select one complete Hearting model configuration for a runtime."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


ADAPTERS = ("claude", "codex", "opencode")
SAFE_KEY = re.compile(r"^CFG_[A-Z0-9_]+$")
SAFE_UNQUOTED = re.compile(r"^[A-Za-z0-9._:/ |,-]+$")
ASSIGNMENT = re.compile(r"^(CFG_[A-Z0-9_]+)=(.*)$")


class ModelConfigError(ValueError):
    """A candidate configuration is not safe or complete."""


class ShippedConfigError(ModelConfigError):
    """The shipped fallback itself cannot provide a safe configuration."""


@dataclass(frozen=True)
class ModelConfigReceipt:
    schema: str
    adapter: str
    source: str
    reason: str
    selected_path: str
    user_path: str
    shipped_path: str
    balanced_provenance: str = "explicit"
    # Shipped tier keys the user file lacks but never references (2026-09-08,
    # SD-145): a complete legacy copy stays selected whole-file instead of being
    # silently replaced by the shipped policy when a release adds a tier.
    unreferenced_tier_keys: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


TIER_KEY = re.compile(r"^CFG_TIER_([A-Z0-9_]+)_(MODEL|EFFORT|VARIANT)$")
# Tiers each adapter's own wrappers read by literal key name, whatever the
# profiles say: the role mappers (`adapters/<adapter>/bin/model-map.sh`,
# `role-map.sh`) and the distill workers match a role in `CFG_ROLES_*` and then
# read `CFG_TIER_DEEP_MODEL` and friends directly, so those keys are required
# even when every profile routes through `model/<id>:effort` (review R2-B1).
# This is a declared contract, not a runtime source scan: selecting the required
# tiers reads no wrapper file (the config files themselves are still read on every
# resolve). `model_config.test.py` guards it by scanning each adapter's `bin/*.sh`
# and `*.py` for fully written tier keys, so a consumer that assembles the key at
# runtime, uses another extension, or lives outside `bin/` must be added here by
# hand (reviews R3-B1/R3-M1/R4-M1/R4-M2).
WRAPPER_REQUIRED_TIERS: dict[str, frozenset[str]] = {
    "claude": frozenset({"DEEP", "LIGHT", "MINI"}),
    "codex": frozenset({"DEEP", "LIGHT", "MINI"}),
    "opencode": frozenset({"BALANCED_DEEP", "LIGHT", "MINI"}),
}


TIER_REFERENCE_KEYS = ("CFG_TIER_DEEP_FAILOVER", "CFG_NATIVE_SUBAGENT", "CFG_LIFECYCLE_NUDGE", "CFG_LIFECYCLE_CURATE")
# Profile keys a complete user copy may omit. `balanced` is derived from light
# in memory (below); `top` is never derived -- a copy without it simply has no
# top exception profile, and its TOP tier keys become unreferenced like any
# other tier a release added (SD-145). Nothing here writes to the user file.
OPTIONAL_PROFILE_KEYS = frozenset({
    "CFG_MODEL_PROFILE_BALANCED", "CFG_MODEL_PROFILE_GRANULARITY_BALANCED",
    "CFG_MODEL_PROFILE_TOP", "CFG_MODEL_PROFILE_GRANULARITY_TOP",
})


def restricted_model(model: str, restricted: list[str] | tuple[str, ...] | str) -> bool:
    """Whether `model` names a CFG_MAIN_SESSION_ONLY_MODELS entry.

    An entry matches as a whole identifier (a hyphenated vendor id) or, when it
    is a bare alphanumeric alias, as one token of the model id (the alias inside
    a versioned full id).
    One definition for every consumer -- the Claude and Codex wrappers, the
    capacity cascade, the native-subagent hook mirror -- so a hyphenated top
    model id cannot pass one gate and fail another."""

    entries = restricted.split() if isinstance(restricted, str) else list(restricted)
    tokens = set(re.split(r"[^a-z0-9]+", model.lower()))
    lowered = model.lower()
    for entry in entries:
        alias = entry.lower()
        if not alias:
            continue
        if alias == lowered or (re.fullmatch(r"[a-z0-9]+", alias) and alias in tokens):
            return True
    return False


def _referenced_tiers(values: Mapping[str, str]) -> set[str]:
    """Tier ids a config file actually points at (profile `tier:budget` values and
    the scalar tier selectors). Tier ids are normalized like model_profile does
    (`balanced-deep` -> `BALANCED_DEEP`)."""
    tiers: set[str] = set()
    for key, value in values.items():
        if key.startswith("CFG_MODEL_PROFILE_") and ":" in value and not value.startswith("model/"):
            tiers.add(value.split(":", 1)[0].strip().upper().replace("-", "_"))
        elif key in TIER_REFERENCE_KEYS:
            tiers.add(value.strip().upper().replace("-", "_"))
    return tiers


def _unreferenced_tier_keys(missing: set[str], user_values: Mapping[str, str], adapter: str) -> set[str]:
    """The subset of `missing` shipped keys that are tier keys of a tier nothing
    in this configuration needs. A release that adds a tier (e.g. `balanced-deep`)
    must not turn an older complete user copy into `user-incomplete` — that would
    silently replace the user's explicit policy (main-only list, model tiers)
    with the shipped one. A tier stays required when the user file references it
    (a profile's `tier:budget`, or a scalar tier selector) or when this adapter's
    wrappers read its keys by name (`WRAPPER_REQUIRED_TIERS`)."""
    required = _referenced_tiers(user_values) | WRAPPER_REQUIRED_TIERS.get(adapter, frozenset())
    optional: set[str] = set()
    for key in missing:
        match = TIER_KEY.fullmatch(key)
        if match and match.group(1) not in required:
            optional.add(key)
    return optional


def _derive_balanced_values(adapter: str, values: dict[str, str]) -> dict[str, str]:
    """Derive only the optional balanced extension in memory.

    The user file remains the selected whole file; this is deliberately not a
    merge with shipped values and never writes back to the runtime home.
    """
    if "CFG_MODEL_PROFILE_BALANCED" in values:
        return values
    light = values.get("CFG_MODEL_PROFILE_LIGHT")
    if not light or ":" not in light:
        raise ModelConfigError("light profile is required to derive balanced")
    light_tier, light_budget = light.split(":", 1)
    from model_profile import resolve_profile_values
    try:
        resolve_profile_values(adapter, values, "light")
    except ValueError as exc:
        raise ModelConfigError("light tier is incomplete for balanced derivation") from exc
    values = dict(values)
    values["CFG_MODEL_PROFILE_BALANCED"] = f"{light_tier}:" + (light_budget if adapter == "opencode" else "high")
    values["CFG_MODEL_PROFILE_GRANULARITY_BALANCED"] = (
        "collapsed-balanced-to-light" if adapter == "opencode" else "full"
    )
    return values


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _check_adapter(adapter: str) -> None:
    if adapter not in ADAPTERS:
        raise ModelConfigError(f"unknown adapter: {adapter!r}")


def _absolute(path: str | Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ModelConfigError(f"{label} must be an absolute path")
    return candidate


def shipped_path(adapter: str, *, source_root: str | Path | None = None) -> Path:
    _check_adapter(adapter)
    root = _absolute(source_root, "source root") if source_root is not None else repository_root()
    return root / "adapters" / adapter / "config" / "models.conf"


def runtime_home(
    adapter: str,
    explicit: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    _check_adapter(adapter)
    env = os.environ if environ is None else environ
    if explicit is not None:
        return _absolute(explicit, "runtime home")
    home = _absolute(env.get("HOME") or str(Path.home()), "HOME")
    if adapter == "claude":
        return _absolute(env.get("CLAUDE_CONFIG_DIR") or home / ".claude", "CLAUDE_CONFIG_DIR")
    if adapter == "codex":
        return _absolute(env.get("CODEX_HOME") or home / ".codex", "CODEX_HOME")
    config_home = _absolute(env.get("XDG_CONFIG_HOME") or home / ".config", "XDG_CONFIG_HOME")
    return config_home / "opencode"


def user_path(
    adapter: str,
    *,
    runtime: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    return runtime_home(adapter, runtime, environ) / "agent-config" / "models.conf"


def _strip_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#":
            return value[:index].rstrip()
    if quote:
        raise ModelConfigError("unterminated quoted value")
    return value.rstrip()


def _parse_value(raw: str, lineno: int) -> str:
    value = _strip_comment(raw).strip()
    if not value:
        raise ModelConfigError(f"line {lineno} has an empty value")
    if value[0] in "'\"":
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise ModelConfigError(f"line {lineno} has an unterminated quoted value")
        value = value[1:-1]
        if quote == '"':
            value = re.sub(r"\\([\\\"#])", r"\1", value)
        if not value:
            raise ModelConfigError(f"line {lineno} has an empty value")
        return value
    if not SAFE_UNQUOTED.fullmatch(value):
        raise ModelConfigError(f"line {lineno} has an unsafe unquoted value")
    return value


def parse_config(path: str | Path, *, allow_symlink: bool = True) -> dict[str, str]:
    """Parse a flat CFG file without evaluating shell syntax."""
    candidate = Path(path)
    try:
        if not allow_symlink and candidate.is_symlink():
            raise OSError("symlinks are not accepted")
        mode = candidate.stat().st_mode
        if not stat.S_ISREG(mode) or not mode & 0o444:
            raise OSError("not a readable regular file")
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ModelConfigError(f"configuration unreadable: {exc}") from exc
    values: dict[str, str] = {}
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match or not SAFE_KEY.fullmatch(match.group(1)):
            raise ModelConfigError(f"line {lineno} is not a safe CFG_ assignment")
        key = match.group(1)
        if key in values:
            raise ModelConfigError(f"line {lineno} duplicates {key}")
        values[key] = _parse_value(match.group(2), lineno)
    if not values:
        raise ModelConfigError("configuration has no CFG_ declarations")
    return values


def resolve_config(
    adapter: str,
    *,
    runtime: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    source_root: str | Path | None = None,
) -> tuple[dict[str, str], ModelConfigReceipt]:
    _check_adapter(adapter)
    shipped = shipped_path(adapter, source_root=source_root)
    selected_user = user_path(adapter, runtime=runtime, environ=environ)
    try:
        shipped_values = parse_config(shipped)
    except ModelConfigError as exc:
        raise ShippedConfigError(f"shipped configuration unusable: {exc}") from exc

    try:
        user_values = parse_config(selected_user, allow_symlink=False)
    except ModelConfigError as exc:
        if not selected_user.exists() and not selected_user.is_symlink():
            reason = "user-missing"
        elif "unreadable" in str(exc):
            reason = "user-unreadable"
        else:
            reason = "user-malformed"
    else:
        optional_balanced = set(OPTIONAL_PROFILE_KEYS)
        missing = set(shipped_values) - set(user_values)
        unreferenced_tier = _unreferenced_tier_keys(missing - optional_balanced, user_values, adapter)
        if missing - optional_balanced - unreferenced_tier:
            reason = "user-incomplete"
        else:
            deriving = ("CFG_MODEL_PROFILE_BALANCED" in shipped_values
                        and "CFG_MODEL_PROFILE_BALANCED" not in user_values)
            try:
                selected = _derive_balanced_values(adapter, user_values) if deriving else user_values
                # Validate the extension when present; old unrelated config validation stays unchanged.
                if "CFG_MODEL_PROFILE_BALANCED" in selected:
                    from model_profile import resolve_profile_values
                    resolve_profile_values(adapter, selected, "balanced")
            except ValueError:
                reason = "user-incomplete"
            else:
                return selected, ModelConfigReceipt(
                    "hearting.model-config/v1", adapter, "user",
                    "user-valid-derived-balanced" if deriving else "user-valid",
                    str(selected_user), str(selected_user), str(shipped),
                    "derived-from-user-light" if deriving else "explicit",
                    ",".join(sorted(unreferenced_tier)),
                )

    return shipped_values, ModelConfigReceipt(
        "hearting.model-config/v1",
        adapter,
        "shipped",
        reason,
        str(shipped),
        str(selected_user),
        str(shipped),
    )


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def assignments(values: Mapping[str, str]) -> str:
    return "".join(f"{key}={_shell_quote(value)}\n" for key, value in sorted(values.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", required=True, choices=ADAPTERS)
    parser.add_argument("--runtime-home")
    parser.add_argument("--source-root")
    parser.add_argument("--receipt-fd", type=int)
    try:
        args = parser.parse_args(argv)
        if args.receipt_fd is not None and args.receipt_fd < 0:
            return 64
        values, receipt = resolve_config(
            args.adapter, runtime=args.runtime_home, source_root=args.source_root
        )
        if args.receipt_fd is not None:
            payload = json.dumps(receipt.as_dict(), separators=(",", ":")) + "\n"
            os.write(args.receipt_fd, payload.encode())
        sys.stdout.write(assignments(values))
        return 0
    except ShippedConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 65
    except (ModelConfigError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
