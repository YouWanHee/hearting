"""Bridge reference drawing data and Fleet's typed explorer view model."""
from __future__ import annotations

import copy


def extract_view_model(renderer, *, scrub=False):
    """Extract the complete reference data table, including non-VM drawing literals."""
    from tools.fleet.tests.freeze_explorer import _fixture_values
    values = copy.deepcopy(_fixture_values(renderer))
    if scrub:
        from tools.fleet.tests.freeze_explorer import sanitize_reference
        sanitize_reference(renderer)
        values = copy.deepcopy(_fixture_values(renderer))
    # Keep the reference's independent data tables available for an exact in-memory round trip.
    # This private token is never serialized into public fixtures.
    modules = {}
    for name in ("mock2", "mock3", "mock4", "mock5", "mock6", "mock9", "mock12"):
        module = getattr(renderer, name, None)
        if module is not None:
            modules[name] = {key: copy.deepcopy(value) for key, value in vars(module).items()
                             if key.isupper() and _jsonish(value)}
    globals_copy = {key: copy.deepcopy(value) for key, value in vars(renderer).items()
                    if key.isupper() and _jsonish(value)}
    values["_reference_snapshot"] = {"globals": globals_copy, "modules": modules}
    return values


def _jsonish(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, dict):
        return all(_jsonish(k) and _jsonish(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return all(_jsonish(item) for item in value)
    return False


def inject_model_catalog(renderer, catalog):
    """Replace mock choices from an authenticated runtime catalog observation."""
    if not isinstance(catalog, dict) or not isinstance(catalog.get("providers"), dict):
        raise ValueError("catalog requires provider-keyed model entries")
    choices, shown = {}, {}
    for harness, entries in catalog["providers"].items():
        if harness not in ("claude", "codex", "opencode") or not isinstance(entries, list):
            continue
        choices[harness] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            model_id = str(entry["id"])
            choices[harness].append(model_id)
            if entry.get("version"):
                shown[model_id] = str(entry["version"])
    renderer.MODEL_CHOICES = choices
    renderer.MODEL_SHOWN = shown
    renderer.MAIN_ONLY = tuple(str(item) for item in catalog.get("main_only", ()))
    return {"providers": sorted(choices), "model_counts": {key: len(value) for key, value in choices.items()},
            "main_only_count": len(renderer.MAIN_ONLY)}


def inject_view_model(renderer, vm, *, catalog=None):
    """Inject an extracted reference VM into its isolated renderer namespace.

    Exact extraction round trips restore the independently loaded module tables. For a
    serialized Fleet VM, fields without a safe reference representation fail explicitly.
    """
    values = dict(vm)
    snapshot = values.pop("_reference_snapshot", None)
    if snapshot is not None:
        for key, value in snapshot["globals"].items():
            setattr(renderer, key, copy.deepcopy(value))
        for name, fields in snapshot["modules"].items():
            module = getattr(renderer, name, None)
            if module is not None:
                for key, value in fields.items():
                    setattr(module, key, copy.deepcopy(value))
        injected = sorted(snapshot["globals"])
        if catalog is not None:
            inject_model_catalog(renderer, catalog)
        return {"injected": injected, "unresolved": []}
    catalog = catalog or (values.get("models") or {}).get("catalog")
    unsupported = sorted(set(values) - {"identity", "clock", "hosts", "usage", "census", "repos",
                                        "sessions", "routes", "nodes", "attempts", "questions",
                                        "answered", "seats", "generations", "cards", "dialogue",
                                        "peer_nodes", "peer_edges", "compute", "memory", "models",
                                        "config", "system", "events", "pane_graph", "source_records"})
    if unsupported:
        raise ValueError("unsupported view-model fields: " + ", ".join(unsupported))
    if catalog is not None:
        inject_model_catalog(renderer, catalog)
    unresolved = ["PROJECTS/session source rows", "route rows and lineage", "peer graph layout",
                  "mock-specific drawing literals"]
    return {"injected": [], "unresolved": unresolved}
