"""Pure attempt ledger projection; decision evidence stays structured."""
from .types import LedgerRow


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def build_ledger(route_views=(), attempts=(), decisions=(), gates=()):
    decision_by_id = {str(_get(d, "attempt_id", "")): _get(d, "decision", d) for d in decisions}
    gate_by_node = {str(_get(g, "route_node", _get(g, "node", ""))): _get(g, "state", _get(g, "mark")) for g in gates}
    rows = []
    for attempt in attempts:
        attempt_id = str(_get(attempt, "attempt_id", _get(attempt, "id", "")))
        route_node = str(_get(attempt, "route_node", _get(attempt, "node", "")))
        decision = _get(attempt, "attempt_decision") or decision_by_id.get(attempt_id)
        outcome = _get(decision, "outcome")
        action = _get(decision, "action")
        reason = _get(decision, "reason", "") or ""
        rows.append(LedgerRow(route_node, str(_get(attempt, "harness", "unknown")),
                              str(_get(attempt, "liveness", "unknown")), outcome,
                              gate_by_node.get(route_node), attempt_id,
                              " · ".join(str(x) for x in (action, reason) if x)))
    order = {str(_get(v, "node_id", _get(v, "node", ""))): i for i, v in enumerate(route_views)}
    rows.sort(key=lambda row: (order.get(row.route_node, len(order)), row.route_node, row.attempt_id))
    # Only latest attempt per route node is presented as current; keep history separately available.
    latest = {}
    for row in rows:
        latest[row.route_node] = row
    return tuple(sorted(latest.values(), key=lambda row: (order.get(row.route_node, len(order)), row.route_node)))
