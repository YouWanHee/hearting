"""Overview relation and obligation projections without inferred edges."""


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def build_overview(snapshot=None, focus_result=None):
    snapshot = snapshot or object()
    rows = []
    evidence = getattr(snapshot, "route_entities", ()) or ()
    for edge in evidence:
        kind = _get(edge, "kind", _get(edge, "relation", "relation"))
        source, target = _get(edge, "source"), _get(edge, "target")
        if source is not None and target is not None:
            rows.append(f"{kind}: {source} → {target}")
    delivery = getattr(snapshot, "pending_delivery", None)
    if delivery:
        rows.append(f"전달 대기: {_get(delivery, 'state', '미확인')}")
    for item in getattr(snapshot, "observations", {}).get("obligations", ()) if isinstance(getattr(snapshot, "observations", {}), dict) else ():
        rows.append(f"의무: {_get(item, 'kind', '미분류')} · 담당 {(_get(item, 'owner') or '미귀속')}")
    return tuple(rows)
