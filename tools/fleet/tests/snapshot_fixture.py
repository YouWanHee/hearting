"""Replay scrubbed source bundles into typed Fleet snapshots and render fixtures."""
from dataclasses import fields, is_dataclass
from collections.abc import Mapping
import json
from pathlib import Path
from typing import get_args, get_origin, get_type_hints
from types import UnionType
from typing import Union
import time

try:
    from fleet import projection, render
    from fleet.collectors import dispatch, resolve_parent_edges
    from fleet.refresh import LiveSnapshot
except ModuleNotFoundError:
    from tools.fleet import projection, render
    from tools.fleet.collectors import dispatch, resolve_parent_edges
    from tools.fleet.refresh import LiveSnapshot


def _typed(value, annotation):
    """Rebuild only declared dataclass fields; unknown values remain provenance."""
    if value is None:
        return None
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        choices = [item for item in args if item is not type(None)]
        for choice in choices:
            if isinstance(choice, type) and is_dataclass(choice) and isinstance(value, dict):
                return _typed(value, choice)
            if get_origin(choice) is not None:
                try:
                    return _typed(value, choice)
                except (TypeError, ValueError):
                    continue
        return value
    if origin in (list, tuple, set):
        if origin is tuple and len(args) > 1 and args[-1] is not Ellipsis:
            return tuple(_typed(item, args[index]) if index < len(args) else item
                         for index, item in enumerate(value))
        item_type = args[0] if args else object
        values = [_typed(item, item_type) for item in value]
        return origin(values) if origin is not tuple else tuple(values)
    if origin in (dict, Mapping):
        kt, vt = args if len(args) == 2 else (object, object)
        return {_typed(k, kt): _typed(v, vt) for k, v in value.items()}
    if isinstance(annotation, type) and is_dataclass(annotation) and isinstance(value, dict):
        if annotation.__name__ == "WorkProjection":
            from tools.fleet.model import ActiveNodeProjection, ProgressProjection
            value = dict(value)
            value["active_nodes"] = tuple(_typed(item, ActiveNodeProjection)
                                          for item in value.get("active_nodes", ()))
            if value.get("progress") is not None:
                value["progress"] = _typed(value["progress"], ProgressProjection)
        allowed = get_type_hints(annotation)
        return annotation(**{key: _typed(item, allowed[key]) for key, item in value.items()
                             if key in allowed})
    return value


def replay_snapshot(payload, *, detail_bundle=None, now=None):
    """Return a LiveSnapshot and ExplorerDetail from the allow-listed bundle schema.

    A bundle may be a mapping or a path to JSON. Unsupported top-level keys are retained in
    ``source_records`` so replay never silently erases source provenance.
    """
    if isinstance(payload, (str, Path)):
        payload = json.loads(Path(payload).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("snapshot bundle must be a mapping or JSON path")
    try:
        from fleet.model import Session, DispatchJob, ResourceJob
    except ModuleNotFoundError:
        from tools.fleet.model import Session, DispatchJob, ResourceJob
    snapshot_fields = get_type_hints(LiveSnapshot)
    rows = {
        "sessions": Session, "jobs": DispatchJob, "resources": ResourceJob,
    }
    snapshot = LiveSnapshot()
    unknown = {key: value for key, value in payload.items() if key not in snapshot_fields}
    for key, value in payload.items():
        if key not in snapshot_fields:
            continue
        if key in rows:
            setattr(snapshot, key, [_typed(item, rows[key]) for item in value or ()])
        else:
            setattr(snapshot, key, _typed(value, snapshot_fields[key]))
    if unknown:
        snapshot.observations = dict(snapshot.observations or {}, replay_unknown=unknown)

    try:
        from fleet.explorer.types import ExplorerDetail, FocusResult, FocusKey
    except ModuleNotFoundError:
        from tools.fleet.explorer.types import ExplorerDetail, FocusResult, FocusKey
    detail_data = detail_bundle or payload.get("detail") or {}
    focus_data = detail_data.get("focus") if isinstance(detail_data, dict) else None
    focus = None
    if isinstance(focus_data, dict):
        key_data = focus_data.get("key", {})
        key = FocusKey(**{name: key_data[name] for name in ("harness", "sid", "generation")
                          if name in key_data})
        allowed = {field.name for field in fields(FocusResult)} - {"key"}
        focus_values = {name: value for name, value in focus_data.items() if name in allowed}
        focus_values["key"] = key
        focus = _typed(focus_values, FocusResult)
    detail = ExplorerDetail(focus=focus,
                            seats=detail_data.get("seats", {}),
                            config=detail_data.get("config"),
                            panes=detail_data.get("panes", {}))
    if now is not None:
        snapshot.observations = dict(snapshot.observations or {}, replay_now=now)
    return snapshot, detail


def observed_fixture(sessions, jobs, resources=None, node_evidence=None):
    evidence = dispatch.collect.last_route_nodes if node_evidence is None else node_evidence
    snapshot = LiveSnapshot(sessions=list(sessions), jobs=list(jobs),
                            resources=list(resources or []), node_evidence=evidence or {})
    resolve_parent_edges(snapshot.sessions, [job for job in snapshot.jobs
                         if (job.parent_managed_dir or getattr(job, "_registry_metadata", None))
                         and not hasattr(job, "_parent_edge_promoted_orphan")])
    if jobs and all(row.work_projection is None for row in snapshot.sessions + snapshot.jobs):
        projection.attach_projections(snapshot.sessions, snapshot.jobs,
                                      node_evidence=snapshot.node_evidence,
                                      degradations=dispatch.collect.last_degradations,
                                      resources=snapshot.resources, now=time.time())
    if not sessions and not jobs:
        snapshot.route_entities = projection.terminal_route_entities(snapshot.node_evidence)
    return snapshot


def build_observed_lines(sessions, jobs, *args, **kwargs):
    snapshot = observed_fixture(sessions, jobs, kwargs.get("resources"), kwargs.get("node_evidence"))
    kwargs.update(node_evidence=snapshot.node_evidence, route_entities=snapshot.route_entities)
    return render._build_lines(sessions, jobs, *args, **kwargs)


def draw_observed(stdscr, sessions, jobs, *args, **kwargs):
    kwargs["snapshot"] = observed_fixture(sessions, jobs, kwargs.get("resources"))
    return render._draw(stdscr, sessions, jobs, *args, **kwargs)
