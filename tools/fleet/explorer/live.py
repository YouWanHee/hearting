"""Read-only conversion of already collected Fleet records into the explorer view model."""
from collections.abc import Mapping
from .view_model import ExplorerViewModel


def _get(value, *keys, default=None):
    for key in keys:
        candidate = value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
        if candidate is not None:
            return candidate
    return default


def _records(value):
    return tuple(value or ())


def build_view_model(snapshot, accepted_detail=None, cached_config=None, now=None):
    """Project facts only; identity and liveness decisions stay with their source builders."""
    fixture_values = _get(snapshot, "values")
    if isinstance(fixture_values, Mapping):
        return ExplorerViewModel.from_mapping(fixture_values)
    if isinstance(snapshot, Mapping) and "schema" in snapshot and "sessions" in snapshot:
        return ExplorerViewModel.from_mapping(snapshot)
    sessions = []
    repos = {}
    resolver = _get(snapshot, "project_of")
    if not callable(resolver):
        try:
            from ..model import project_of as resolver
        except Exception:
            resolver = None
    for record in _records(_get(snapshot, "sessions", default=())):
        harness = _get(record, "harness", "adapter", "h", default=None)
        sid = _get(record, "session_tag", "tag", "session_id", "sid", "id", default=None)
        if sid is None:
            continue
        cwd = _get(record, "cwd", "project", default=None)
        repo = resolver(cwd) if callable(resolver) and cwd is not None else _get(record, "repo", "project", "slug", default=cwd)
        repo_id = str(_get(record, "repo_id", default=repo or ""))
        context = _get(record, "context_pct", "ctx_pct", "context", "ctx", default=None)
        if not isinstance(context, (int, float)) or isinstance(context, bool):
            context = _get(context, "used_pct", default=None)
        if repo_id:
            repos.setdefault(repo_id, {"id": repo_id, "name": repo or repo_id})
        sessions.append({
            "tag": str(sid), "harness": harness, "repo": repo_id,
            "repo_id": repo_id, "title": _get(record, "title", "name", default=None),
            "state": _get(record, "status", "state", "st", "liveness", default=None),
            "status_mark": _get(record, "status_mark", default=None),
            "age": _get(record, "age", "elapsed_min", default=None), "context": context,
            "now": _get(record, "now", "summary", default=None),
            "model": _get(record, "model", "m", default=None),
            "effort": _get(record, "effort", "e", default=None),
            "window": _get(record, "window", "win", default=None),
            "steward": _get(record, "steward", default=False),
            "pane": _get(record, "herdr_pane", "pane", "pane_id", default=None),
            "generation": _get(record, "generation", "generation_count", default=None),
            "last_clear": _get(record, "last_clear", "last_tidy", default=None),
            "branch": _get(record, "branch", default=None),
            "running": _get(record, "running", default=None),
            "gpu": _get(record, "gpu", default=None),
            "pane_meta": _get(record, "pane_meta", default=None),
            "pane_lines": _get(record, "pane_lines", default=None),
        })
    routes = []
    for record in _records(_get(snapshot, "routes", "route_records", default=())):
        routes.append({"id": _get(record, "route_id", "id", "pick", default=None),
                       "session_tag": _get(record, "session_tag", "owner_tag", "from", default=None),
                       "title": _get(record, "title", "slug", default=None),
                       "state": _get(record, "status", "state", "st", default=None),
                       "repo": _get(record, "repo", default=None),
                       "worker": _get(record, "worker", default=None),
                       "worktree": _get(record, "worktree", "wt", default=None)})
    peer_edges = _get(snapshot, "peer_edges", default=None)
    if not isinstance(peer_edges, Mapping):
        peer_edges = {}
    data = {
        "identity": _get(snapshot, "generation", "identity", default=None),
        "clock": now,
        "hosts": _records(_get(snapshot, "compute_hosts", "hosts", default=())),
        "usage": _records(_get(snapshot, "usage", default=())),
        "census": _get(snapshot, "census", default=None),
        "repos": tuple(repos.values()), "sessions": tuple(sessions), "routes": tuple(routes),
        "nodes": _records(_get(snapshot, "nodes", "route_nodes", default=())),
        "attempts": _records(_get(snapshot, "attempts", "route_attempts", default=())),
        "questions": _records(_get(accepted_detail, "questions", default=())),
        "answered": _records(_get(accepted_detail, "answered", default=())),
        "seats": _records(_get(accepted_detail, "seats", default=())),
        "generations": _records(_get(accepted_detail, "generations", default=())),
        "cards": _records(_get(accepted_detail, "cards", default=())),
        "dialogue": _records(_get(accepted_detail, "dialogue", default=())),
        "peer_nodes": _records(_get(snapshot, "peer_nodes", default=())),
        "peer_edges": peer_edges,
        "events": _records(_get(accepted_detail, "events", default=())),
        "compute": _records(_get(snapshot, "resources", default=())),
        "memory": _get(snapshot, "memory", default=_get(accepted_detail, "memory", default=None)),
        "models": _get(cached_config, "models", "model_config", default=None), "config": cached_config,
        "system": _get(snapshot, "system", default=None),
        "pane_graph": _records(_get(snapshot, "pane_graph", "pane_edges", default=())),
        "source_records": _get(accepted_detail, "source_records", default=None),
    }
    return ExplorerViewModel.from_mapping(data)


def from_mapping(value):
    return ExplorerViewModel.from_mapping(value)
