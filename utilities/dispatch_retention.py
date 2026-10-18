"""Shared terminal and outstanding-consumer protection for dispatch artifacts.

Home and transcript retention have different ownership and file-removal rules,
but they must agree on whether an attempt is still observable or consumable.
The adapters below supply their evidence readers; this module owns the ordered
decision so one artifact cannot be retired under weaker lifecycle semantics.
"""

from __future__ import annotations


class Protection:
    __slots__ = ("kind", "reason", "activity_ns", "attempt", "raw")

    def __init__(self, kind, reason="", activity_ns=0, attempt="", raw=""):
        self.kind, self.reason, self.activity_ns = kind, reason, activity_ns
        self.attempt, self.raw = attempt, raw


TERMINAL_STATUSES = frozenset({"done", "killed", "cancelled"})
OPEN_STATUSES = frozenset({"open", "running"})
MAX_INTENT_CHILDREN = 256


def terminal_protection(ctx, attempt, *, probe_processes, delivery_state,
                        replacement_state, route_state, owner_state, lease_state,
                        delivery_index, supervisor_activity, process_quiescence):
    """Return ``eligible`` only when the exact attempt has no live consumer.

    Callback arguments are read-only evidence resolvers. ``unknown`` always
    keeps data, and activity timestamps extend retention instead of expiring an
    outstanding obligation.
    """
    snapshot = ctx.snapshot
    row = snapshot.rows.get(attempt)
    if row is None:
        return Protection("unknown", "no-row")
    if row.status not in TERMINAL_STATUSES:
        return Protection("keep", "not-terminal")
    if attempt in snapshot.open_refs:
        return Protection("keep", "open-reference")

    activity = 0
    state, mtime = delivery_state(row.meta)
    if state in ("open", "pending"):
        return Protection("keep", "delivery-" + state)
    if state == "unknown":
        return Protection("unknown", "delivery-unknown")
    activity = max(activity, mtime)

    children = snapshot.intents.get(attempt, ())
    if len(children) > MAX_INTENT_CHILDREN:
        return Protection("unknown", "delivery-unknown")
    for child in children:
        child_row = snapshot.rows.get(child)
        if child_row is None:
            return Protection("unknown", "delivery-unknown")
        state, mtime = delivery_state(child_row.meta)
        if state in ("open", "pending"):
            return Protection("keep", "delivery-" + state)
        if state == "unknown":
            return Protection("unknown", "delivery-unknown")
        activity = max(activity, mtime)

    state, mtime = replacement_state(attempt)
    if state == "unresolved":
        return Protection("keep", "replacement-unresolved")
    if state == "unknown":
        return Protection("unknown", "replacement-unknown")
    activity = max(activity, mtime)

    state, mtime = route_state(row.meta)
    if state == "open":
        return Protection("keep", "route-open")
    if state == "unknown":
        return Protection("unknown", "route-unknown")
    activity = max(activity, mtime)

    owner = owner_state(row.meta)
    if owner == "live":
        return Protection("keep", "summary-live")
    if owner == "unknown":
        return Protection("unknown", "summary-owner-unknown")
    lease = lease_state(row.meta)
    if lease == "held":
        return Protection("keep", "lease-held")
    if lease == "unknown":
        return Protection("unknown", "lease-unknown")

    refs, index_unknown = delivery_index()
    if index_unknown:
        return Protection("unknown", "delivery-index-unknown")
    if attempt in refs:
        return Protection("keep", "delivery-open")
    activity = max(activity, ctx.delivery_activity.get(attempt, 0))
    activity = max(activity, supervisor_activity(attempt))

    if probe_processes:
        try:
            quiescence = process_quiescence(row.meta)
        except Exception:  # noqa: BLE001 -- an unobservable process is not quiet
            return Protection("unknown", "quiescence-unverifiable")
        if quiescence.state == "live":
            return Protection("keep", "not-quiescent")
        if quiescence.state != "quiescent":
            return Protection("unknown", "quiescence-unverifiable")

    return Protection("eligible", activity_ns=activity, attempt=attempt, raw=row.raw)
