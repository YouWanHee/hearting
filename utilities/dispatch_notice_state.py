"""Read current obligations immediately before an existing carrier emits them.

False means positively superseded; unreadable/conflicting evidence raises so
the carrier retains its lease for recovery instead of discarding the notice.
This is not a second delivery queue or a workflow outcome writer.
"""
from __future__ import annotations

import json
from pathlib import Path

from route_identity import route_hash


def closed_outcome(path: Path, route: dict) -> dict | None:
    """Only the exact route generation's closure is evidence of resolution."""
    try:
        outcome = json.loads(path.with_name(path.stem + ".outcome.json").read_text())
    except FileNotFoundError:
        return None
    if (not isinstance(outcome, dict)
            or outcome.get("route_id") != route.get("route_id")
            or outcome.get("route_hash") != route.get("route_hash")):
        raise ValueError("notice-route-outcome-mismatch")
    if outcome.get("inline_finish_id"):
        import inline_finish
        pending = inline_finish.pending_state(Path(route.get("artifact_root", "")), route["route_id"])
        if not pending or pending.get("inline_finish_id") != outcome.get("inline_finish_id"):
            raise ValueError("notice-inline-finish-state-mismatch")
        if pending.get("state") != "finished":
            outcome = dict(outcome, finish_pending=True, finish_state=pending.get("state"),
                           terminal_gate_proven=False)
    return outcome


def bound_route(metadata: dict, jobs: Path, expected_id: str = ""):
    """Use the row's exact binding, including a verified owner advance."""
    prefix = "" if metadata.get("route_file") else "owner_"
    path, rid, digest = (metadata.get(prefix + key) for key in ("route_file", "route_id", "route_hash"))
    if prefix == "owner_" and all((path, rid, digest, metadata.get("attempt_id"))):
        from owner_route_binding import resolve_owner_route_lifecycle
        binding, _ = resolve_owner_route_lifecycle(jobs, owner_attempt_id=metadata["attempt_id"])
        if binding:
            path, rid, digest = binding.route_file, binding.route_id, binding.route_hash
    if not path:
        return None  # Historical route-free rows have no closure authority.
    route = json.loads(Path(path).read_text())
    if (not isinstance(route, dict) or route.get("route_id") != rid
            or route.get("route_hash") != digest or route_hash(route) != digest
            or (expected_id and rid != expected_id)):
        raise ValueError("notice-route-binding-mismatch")
    return Path(path), route


def route_obligation_closed(metadata: dict, jobs: Path) -> bool:
    bound = bound_route(metadata, jobs)
    outcome = closed_outcome(*bound) if bound else None
    return bool(outcome and not outcome.get("finish_pending"))


def _gate_resolution(entries: list, gate: str, delivery: str) -> dict:
    """Resolve this delivery's raise, never a previous or future question.

    The producer writes pending before BLOCKED_HUMAN_GATE. Absence of that
    exact raise is still publication in progress, not proof of resolution.
    Keep earlier resolved raises readable after a later question was raised.
    """
    from workflow_state import human_gate_resolution
    found = False
    end = len(entries)
    for index, entry in enumerate(entries):
        evidence = entry.get("evidence") or {}
        if not isinstance(evidence, dict):
            raise ValueError("notice-gate-journal-invalid")
        if entry.get("workflow_state") == "BLOCKED_HUMAN_GATE" and evidence.get("gate") == gate:
            if found:
                end = index
                break
            found = evidence.get("delivery") == delivery
    if not found:
        raise ValueError("notice-gate-publication-pending")
    resolution = human_gate_resolution(entries[:end], gate)
    if end < len(entries) and resolution["status"] == "blocked":
        raise ValueError("notice-gate-journal-conflict")
    return resolution


def _legacy_gate_current(record: dict, jobs: Path, child: dict, metadata: dict) -> bool:
    import dispatch_pending_delivery as pending
    from workflow_state import WorkflowLedger, node_raises_human_gate
    bound = bound_route(metadata, jobs, record.get("route_id", ""))
    if bound is None:
        raise ValueError("notice-gate-route-unbound")
    path, route = bound
    if closed_outcome(path, route):
        return False
    gate = child["required_action"].removeprefix("human-gate:")
    node = next((n for n in route.get("nodes", []) if n.get("id") == record.get("route_node")), {})
    if not node_raises_human_gate(node, gate):
        return False
    # Strict read: a corrupt journal is unknown, not an unraised gate.
    journal = WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs).journal_path
    entries = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("notice-gate-journal-invalid")
    expected = pending.record_directory(jobs.parent, metadata["parent_sid"]) / (record["delivery_id"] + ".json")
    resolution = _gate_resolution(entries, gate, str(expected))
    current = resolution["status"] == "blocked"
    if current:
        if resolution.get("artifact") != child.get("reason"):
            raise ValueError("notice-gate-artifact-mismatch")
        from human_gate_receipt import _absolute_regular
        _absolute_regular(child["reason"], "artifact")
        if resolution.get("artifact_sha256"):
            from workflow_state import require_gate_artifact_current
            require_gate_artifact_current(resolution)
    return current


def notice_is_current(record: dict, *, jobs: Path | None = None) -> bool:
    receipt = record.get("receipt") or {}
    if receipt.get("kind") == "supervision":
        from dispatch_supervision import notice_is_current as current
        return current(record)
    if receipt.get("kind") == "human-gate":
        import human_gate_receipt as gate
        try:
            gate._load_route(receipt)
        except gate.HumanGateReceiptError as exc:
            if str(exc) == "route-already-closed":
                return False
            raise
        gate._validate_shape(receipt)
        resolution = _gate_resolution(gate._load_journal(receipt, Path(receipt["job_registry"])),
            receipt["gate"], str(gate.pending_delivery.record_path(Path(receipt["job_registry"]).parent,
                receipt["recipient_thread_id"], record["delivery_id"])))
        if resolution["epoch"] != receipt["gate_epoch"]:
            raise ValueError("notice-gate-epoch-unproved")
        return resolution["status"] == "blocked"
    from dispatch_supervision import _rows
    # Terminal-intent receipts deliberately omit job_registry. The carrier
    # already owns the exact queue root; never resolve an empty string to cwd.
    jobs = Path(receipt.get("job_registry") or jobs or "")
    if not jobs.is_absolute():
        raise ValueError("notice-jobs-unbound")
    rows = _rows(jobs)
    current = not receipt.get("children")
    for child in receipt.get("children", []):
        action = str(child.get("required_action", ""))
        row = rows.get(child.get("attempt_id"))
        if row is None:
            if action.startswith("human-gate:"):
                raise ValueError("notice-gate-attempt-missing")
            current = True
            continue  # Old route-free completion records remain deliverable.
        status, meta = row
        if action.startswith("human-gate:"):
            if not _legacy_gate_current(record, jobs, child, meta):
                return False
        elif action != "advance-completed" and route_obligation_closed(meta, jobs):
            if meta.get("workflow_completion") == "runtime-v1":
                from dispatch_terminal_commit import owner_completion_pending
                if owner_completion_pending(jobs, status, meta):
                    current = True
                    continue  # Closure alone does not prove cycle settlement.
            continue
        current = True
    return current


def keep_claim(root, recipient, delivery_id, record, claim_owner, *, jobs=None) -> bool:
    """Retire only proven obsolete notices; unknown evidence stays recoverable."""
    import dispatch_pending_delivery as pending
    try:
        if notice_is_current(record, jobs=jobs or Path(root) / "jobs.log"):
            return True
        pending.reject_claimed(root, recipient, delivery_id, claim_owner=claim_owner,
                               reason=("supervision-resolved" if record.get("receipt", {}).get("kind") == "supervision"
                                       else "notice-obligation-resolved"))
    except (OSError, ValueError, KeyError, pending.PendingDeliveryError):
        pass
    return False
