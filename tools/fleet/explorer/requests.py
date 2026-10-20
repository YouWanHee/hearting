"""Pure request-tab reconstruction from registry, route, and cycle evidence."""
from .types import RequestTab


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def build_tabs(registry_rows=(), routes=(), cycles=()):
    """Join owner routes to a cycle only with exact cycle and frame-route proof."""
    route_by_id = {_get(r, "route_id", _get(r, "id")): r for r in routes}
    cycles_by_id = {_get(c, "cycle_id"): c for c in cycles}
    tabs = {}
    for row in registry_rows:
        sid = _get(row, "parent_sid") or _get(row, "sid") or "unknown"
        attempt = _get(row, "attempt_id") or _get(row, "id") or _get(row, "slug") or "unknown"
        route_id = _get(row, "route_id") or _get(row, "owner_route_id")
        route = route_by_id.get(route_id)
        parent_cycle = _get(route, "parent_cycle_id") if route else None
        cycle = cycles_by_id.get(parent_cycle)
        verified = bool(cycle and _get(cycle, "cycle_id") == parent_cycle
                        and _get(cycle, "route_id") == (_get(route, "frame_route_id") or _get(route, "parent_route_id") or _get(row, "frame_route_id") or route_id))
        identity = f"{sid}:{parent_cycle if verified else 'unverified:' + str(attempt)}"
        if verified:
            label = _get(cycle, "title") or _get(cycle, "label") or parent_cycle
            cycle_id = parent_cycle
        else:
            label, cycle_id = "연결 미확인", None
        old = tabs.get(identity)
        complete = _get(row, "status") in {"done", "killed", "cancelled", "complete", "completed"}
        tab = RequestTab(identity, str(label), route_id, cycle_id, complete, verified)
        if old is None or (complete and not old.complete):
            tabs[identity] = tab
    return tuple(tabs.values())


def select_fallback(tabs, selected=""):
    tabs = tuple(tabs)
    if not tabs:
        return ""
    if any(tab.identity == selected for tab in tabs):
        return selected
    return tabs[0].identity
