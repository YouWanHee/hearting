#!/usr/bin/env python3
"""Resolve route-sealed portable execution profiles through adapter config."""

from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path
from typing import Mapping


PORTABLE_PROFILES = ("deep", "balanced-deep", "balanced", "light", "mini")
# The exception profile above `deep` (2026-09-09 사용자 결정): each harness's top
# model -- the one CFG_MAIN_SESSION_ONLY_MODELS reserves for the session the
# user talks to -- reached only by an explicit `top` selection on a dispatch-
# depth-1 owner or review worker with a full demand. It is not portable in the
# five-profile sense: no matrix cell resolves to it, no policy band names it,
# no capacity cascade enters or leaves it, and a runtime config that does not
# declare CFG_MODEL_PROFILE_TOP refuses it typed instead of deriving a model.
TOP_PROFILE = "top"
EXCEPTION_PROFILES = (TOP_PROFILE,)
KNOWN_PROFILES = PORTABLE_PROFILES + EXCEPTION_PROFILES
TOP_WORKER_TYPES = frozenset({"owner", "review"})
RESOLVER_VERSION = "profile-demand/v1"
DEMAND_SCHEMA_VERSION = 1
DEMAND_JUDGMENTS = ("predetermined", "important", "difficult-uncertain")
DEMAND_SCOPES = ("short-local", "extended-multistep")
DEMAND_FIELDS = frozenset({
    "schema_version", "judgment_requirement", "execution_scope",
    "judgment_reason", "execution_reason", "evidence_refs",
})
JUDGMENT_FLOORS = {
    "predetermined": ("light", "balanced"),
    "important": ("balanced-deep", "deep"),
    "difficult-uncertain": ("deep",),
}
MATRIX = {
    ("predetermined", "short-local"): "light",
    ("predetermined", "extended-multistep"): "balanced",
    ("important", "short-local"): "balanced-deep",
    ("important", "extended-multistep"): "balanced-deep",
    ("difficult-uncertain", "short-local"): "deep",
    ("difficult-uncertain", "extended-multistep"): "deep",
}
SUBSTANTIVE_WORKER_TYPES = frozenset({"owner", "stage", "review"})
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:/ |,-]+$")
SAFE_KEY = re.compile(r"^CFG_[A-Z0-9_]+$")


class ModelProfileError(ValueError):
    def __init__(self, message: str, reason: str = "invalid-profile-demand"):
        super().__init__(message)
        self.reason = reason


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_profile_demand(demand: Mapping[str, object]) -> dict[str, object]:
    """Validate and canonicalize the vendor-neutral two-axis demand object."""
    if not isinstance(demand, Mapping):
        raise ModelProfileError("profile_demand must be an object", "profile-demand-invalid")
    if set(demand) != DEMAND_FIELDS:
        raise ModelProfileError("profile_demand must contain exactly the schema v1 fields",
                                "profile-demand-partial")
    if type(demand.get("schema_version")) is not int or demand["schema_version"] != DEMAND_SCHEMA_VERSION:
        raise ModelProfileError("unsupported profile_demand schema_version",
                                "profile-demand-version-unsupported")
    judgment = demand.get("judgment_requirement")
    scope = demand.get("execution_scope")
    if judgment not in DEMAND_JUDGMENTS or scope not in DEMAND_SCOPES:
        raise ModelProfileError("unknown profile demand axis", "profile-demand-unknown-axis")
    reasons = (demand.get("judgment_reason"), demand.get("execution_reason"))
    if any(not isinstance(value, str) or not value.strip() for value in reasons):
        raise ModelProfileError("profile demand reasons must be non-empty strings",
                                "profile-demand-empty-reason")
    evidence = demand.get("evidence_refs")
    if (not isinstance(evidence, list) or not evidence or
            any(not isinstance(value, str) or not value.strip() for value in evidence)):
        raise ModelProfileError("profile demand evidence_refs must be non-empty strings",
                                "profile-demand-empty-evidence")
    return {
        "schema_version": DEMAND_SCHEMA_VERSION,
        "judgment_requirement": judgment,
        "execution_scope": scope,
        "judgment_reason": reasons[0].strip(),
        "execution_reason": reasons[1].strip(),
        "evidence_refs": [value.strip() for value in evidence],
    }


def resolve_profile_demand(
    demand: Mapping[str, object] | None,
    *,
    explicit_profile: str | None = None,
    legacy: bool = False,
    existing_versioned_stage: bool = False,
) -> dict[str, object]:
    """Resolve one demand into a sealed selection; no adapter/model knowledge."""
    if demand is None:
        if legacy and existing_versioned_stage and explicit_profile in PORTABLE_PROFILES:
            return {
                "schema_version": 1, "source": "legacy", "resolver_version": RESOLVER_VERSION,
                "demand_digest": None, "resolved_profile": explicit_profile, "judgment_floor": "unknown",
                "reason": "unannotated-existing-stage",
            }
        raise ModelProfileError("new or ad-hoc stages require full profile_demand",
                                "profile-demand-required")
    normalized = normalize_profile_demand(demand)
    judgment = normalized["judgment_requirement"]
    scope = normalized["execution_scope"]
    matrix_profile = MATRIX[(judgment, scope)]
    if explicit_profile is None:
        resolved = matrix_profile
        source = "matrix"
        reason = "matrix-cell"
    else:
        if explicit_profile not in KNOWN_PROFILES:
            raise ModelProfileError("unknown explicit profile", "profile-explicit-unknown")
        if explicit_profile == TOP_PROFILE:
            # Above every floor, but never for predetermined work: the top
            # model is an exception spent on judgment, not on execution length.
            if judgment == "predetermined":
                raise ModelProfileError("the top exception profile needs important or "
                                        "difficult-uncertain judgment", "profile-top-predetermined")
            allowed = (TOP_PROFILE,)
        else:
            allowed = JUDGMENT_FLOORS[judgment]
        if explicit_profile not in allowed:
            raise ModelProfileError("explicit profile is below the judgment floor",
                                    "profile-floor-violation")
        if judgment == "predetermined" and explicit_profile != matrix_profile:
            raise ModelProfileError("predetermined demand only permits its exact matrix cell",
                                    "profile-explicit-cell-mismatch")
        resolved = explicit_profile
        source = "explicit"
        reason = ("explicit-top-exception" if explicit_profile == TOP_PROFILE
                  else "important-explicit-deep-additional-judgment-headroom"
                  if judgment == "important" and explicit_profile == "deep"
                  else "explicit-within-floor")
    return {
        "schema_version": 1,
        "source": source,
        "resolver_version": RESOLVER_VERSION,
        "demand_digest": _digest(normalized),
        "resolved_profile": resolved,
        "judgment_floor": {"predetermined": "none", "important": "balanced-deep",
                           "difficult-uncertain": "deep"}[judgment],
        "reason": reason,

    }


def validate_profile_selection(selection, demand=None, *, profile=None,
                               existing_versioned_stage=False):
    """Recompute every semantic field, rather than trusting a supplied digest."""
    fields = {"schema_version", "source", "resolver_version", "demand_digest",
              "resolved_profile", "judgment_floor", "reason"}
    if (not isinstance(selection, Mapping) or set(selection) != fields
            or type(selection.get("schema_version")) is not int
            or selection["schema_version"] != 1
            or selection.get("resolver_version") != RESOLVER_VERSION):
        raise ModelProfileError("unsupported or invalid profile selection contract",
                                "profile-selection-version-unsupported")
    source = selection.get("source")
    if source not in {"matrix", "explicit", "legacy"}:
        raise ModelProfileError("invalid profile selection source", "profile-selection-invalid")
    resolved = selection.get("resolved_profile")
    if profile is not None and resolved != profile:
        raise ModelProfileError("sealed profile differs from selection", "profile-selection-mismatch")
    expected = resolve_profile_demand(
        demand, explicit_profile=resolved if source in {"explicit", "legacy"} else None,
        legacy=source == "legacy", existing_versioned_stage=existing_versioned_stage,
    )
    if dict(selection) != expected:
        raise ModelProfileError("sealed profile selection differs from resolver",
                                "profile-selection-mismatch")


def load_config(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ModelProfileError(f"model profile config unreadable: {exc}") from exc
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            if line.startswith("CFG_"):
                raise ModelProfileError(
                    f"model profile config line {lineno} is a malformed CFG_ declaration"
                )
            continue
        key, value = line.split("=", 1)
        value = value.split("#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        key = key.strip()
        if not key.startswith("CFG_"):
            continue
        if not SAFE_KEY.fullmatch(key):
            raise ModelProfileError(
                f"model profile config line {lineno} has an invalid CFG_ key: {key!r}"
            )
        if not value or not SAFE_VALUE.fullmatch(value):
            raise ModelProfileError(
                f"model profile config line {lineno} has an invalid value for {key}"
            )
        values[key] = value
    return values


def resolve_profile_values(
    adapter: str, config: Mapping[str, str], profile: str
) -> dict[str, str]:
    if profile not in KNOWN_PROFILES:
        raise ModelProfileError(f"unknown portable model profile: {profile!r}")
    if adapter not in {"claude", "codex", "opencode"}:
        raise ModelProfileError(f"unknown adapter: {adapter!r}")
    profile_key = "CFG_MODEL_PROFILE_" + profile.upper().replace("-", "_")
    spec = config.get(profile_key)
    if profile == TOP_PROFILE and not spec:
        # Opt-in only: the selected runtime file (the user's whole-file copy or
        # the shipped default) must say what `top` is; nothing is derived.
        raise ModelProfileError(
            "the selected runtime model config does not declare CFG_MODEL_PROFILE_TOP; "
            "the top exception profile is opt-in", "profile-top-undeclared")
    if not spec or spec.count(":") != 1:
        raise ModelProfileError(f"{profile_key} must declare tier:effort-or-variant")
    tier, budget = spec.split(":", 1)
    tier_key = tier.upper().replace("-", "_")
    model = tier[len("model/"):] if tier.startswith("model/") else config.get(f"CFG_TIER_{tier_key}_MODEL")
    budget_suffix = "VARIANT" if adapter == "opencode" else "EFFORT"
    declared_default = budget if tier.startswith("model/") else config.get(f"CFG_TIER_{tier_key}_{budget_suffix}")
    granularity_key = "CFG_MODEL_PROFILE_GRANULARITY_" + profile.upper().replace("-", "_")
    granularity = config.get(granularity_key) or config.get(
        "CFG_MODEL_PROFILE_GRANULARITY", "unknown"
    )
    if not model or not declared_default:
        raise ModelProfileError(f"profile tier {tier!r} lacks model/{budget_suffix.lower()}")
    if not budget:
        raise ModelProfileError(f"profile {profile!r} has an empty execution budget")
    return {
        "profile": profile,
        "tier": tier,
        "model": model,
        "budget": budget,
        "budget_kind": budget_suffix.lower(),
        "granularity": granularity,
    }


def resolve_profile(adapter: str, config_path: str | Path, profile: str) -> dict[str, str]:
    return resolve_profile_values(adapter, load_config(config_path), profile)


def resolve_runtime_profile(
    adapter: str,
    profile: str,
    *,
    runtime: str | Path | None = None,
    environ: dict[str, str] | None = None,
    source_root: str | Path | None = None,
) -> tuple[dict[str, str], object]:
    """Resolve a profile from the complete user file or complete shipped fallback."""
    try:
        from model_config import ModelConfigError, resolve_config
    except ImportError:  # package import in focused unit tests
        from utilities.model_config import ModelConfigError, resolve_config

    try:
        values, receipt = resolve_config(
            adapter, runtime=runtime, environ=environ, source_root=source_root
        )
    except ModelConfigError as exc:
        raise ModelProfileError(f"runtime model config unavailable: {exc}") from exc
    return resolve_profile_values(adapter, values, profile), receipt


def validate_registered_profile(
    profile: str | None,
    *,
    registered_worker: bool,
    dispatch_depth: int,
    worker_type: str | None,
) -> None:
    if profile is None:
        return
    if profile not in KNOWN_PROFILES:
        raise ModelProfileError(f"unknown portable model profile: {profile!r}")
    if profile == TOP_PROFILE and not (
        registered_worker and dispatch_depth == 1 and worker_type in TOP_WORKER_TYPES
    ):
        raise ModelProfileError(
            "the top exception profile is limited to a registered dispatch-depth-1 "
            "owner or review worker", "profile-top-depth-forbidden")
    if (
        profile == "mini"
        and registered_worker
        and dispatch_depth in {1, 2}
        and worker_type in SUBSTANTIVE_WORKER_TYPES
    ):
        raise ModelProfileError(
            "mini is reserved for lifecycle or explicitly micro-semantic helpers"
        )


def selection_receipt(args):
    """Bounded diagnostic projection of the route already checked by the wrapper."""
    binding = getattr(args, "owner_route_binding", None)
    path = getattr(args, "route_file", None) or getattr(binding, "route_file", None)
    if not path:
        return {}
    route = json.loads(Path(path).read_text(encoding="utf-8"))
    node_id = getattr(args, "route_node", None)
    if node_id:
        node = next((n for n in route.get("nodes", []) if n.get("id") == node_id), None)
        if node is None:
            raise ModelProfileError("profile route node missing", "profile-selection-mismatch")
        selection, demand = node.get("profile_selection"), node.get("profile_demand")
    else:
        selection, demand = route.get("owner_profile_selection"), route.get("owner_profile_demand")
    if route.get("profile_selection_contract_version") is None:
        return {"profile_selection_source": "legacy", "profile_resolver_version": "-",
                "profile_demand_digest": "-", "profile_judgment_floor": "unknown"}
    if type(route["profile_selection_contract_version"]) is not int or route["profile_selection_contract_version"] != 1:
        raise ModelProfileError("unsupported profile selection contract", "profile-selection-version-unsupported")
    validate_profile_selection(selection, demand,
        profile=args.resolved_model_settings["profile"], existing_versioned_stage=True)
    return {"profile_selection_source": selection["source"],
            "profile_resolver_version": selection["resolver_version"],
            "profile_demand_digest": selection["demand_digest"] or "-",
            "profile_selection_digest": _digest(selection),
            "profile_judgment_floor": selection["judgment_floor"]}
