"""Resource leg of the existing owner controller, outside any model turn.

The workflow watch records evidence; it does not launch model successors for
this leg. Delivery lives in the existing private supervisor phase/outbox file.
"""
from __future__ import annotations
import importlib.util
from functools import lru_cache
import json
from pathlib import Path
import time

import dispatch_completion_join as JOIN
import resource_resume as RESUME


@lru_cache(maxsize=1)
def supervisor():
    spec = importlib.util.spec_from_file_location("owner_resource_supervisor", Path(__file__).with_name("workflow-supervisor.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start_binding(route, route_file, args, environ):
    """Use the normal registered owner identity, never labels or a guessed SID."""
    import owner_route_binding as OWNER
    from dispatch_contract import resolve_live_parent_attempt
    from dispatch_owner_input import inspect, PLACEHOLDER_THREAD
    if environ.get("AGENT_DISPATCH_COMPLETION_MODE") != "supervised":
        return None
    attempt = OWNER._registered_owner_attempt(environ)
    if not attempt:
        return None
    if args.parent_attempt_id and args.parent_attempt_id != attempt:
        raise ValueError("resource-owner-attempt-conflict")
    jobs = Path(args.jobs or environ.get("AGENT_DISPATCH_JOBS", ""))
    if not jobs.is_absolute() or not jobs.is_file():
        raise ValueError("resource-owner-jobs-unavailable")
    jobs = jobs.resolve()
    if environ.get("AGENT_DISPATCH_JOBS") and Path(environ["AGENT_DISPATCH_JOBS"]).resolve() != jobs:
        raise ValueError("resource-owner-jobs-conflict")
    binding, _ = OWNER.resolve_owner_route_lifecycle(jobs, owner_attempt_id=attempt)
    if (binding is None or binding.route_file != str(route_file)
            or binding.route_id != route["route_id"] or binding.route_hash != route["route_hash"]):
        raise ValueError("resource-owner-route-conflict")
    row = JOIN.exact_attempt_row(jobs, attempt)
    fields = row.raw.split("\t")
    parent = resolve_live_parent_attempt(jobs, parent_slug=row.slug, repo=fields[2],
        worktree=fields[3], expected_attempt_id=attempt)
    current = inspect(jobs, attempt)
    session = current["thread_id"]
    if not current["supervisor_live"] or not session or session == PLACEHOLDER_THREAD:
        raise ValueError("resource-owner-session-unavailable")
    from dispatch_contract import dispatch_state_root
    state_path = dispatch_state_root(jobs) / "supervisor-state" / (attempt + ".json")
    if Path(environ.get("AGENT_DISPATCH_COMPLETION_STATE_FILE", "")).resolve() != state_path.resolve():
        raise ValueError("resource-owner-state-conflict")
    with JOIN._supervisor_state_lock(state_path):
        state = JOIN.read_supervisor_phase_state(state_path, attempt)
        if state is None:
            raise ValueError("resource-owner-state-unavailable")
        resource = state.resource or {"session_id": session, "delivered": [], "outbox": None}
        if resource["session_id"] != session:
            raise ValueError("resource-owner-session-conflict")
        JOIN._write_supervisor_state_unlocked(state_path, attempt, set(state.delivered_attempt_ids),
            phase=state.phase, outbox=state.outbox, resource=resource)
    args.jobs, args.parent_attempt_id = str(jobs), attempt
    return {"parent_attempt_id": attempt, "session_id": session, "route_id": route["route_id"],
        "route_hash": route["route_hash"], "jobs": str(jobs),
        "owner_pid": parent.pid, "owner_start": parent.pid_start}


def resource_body_digest(row):
    keys = ("run_id", "cwd", "log", "command", "route", "node", "parent_attempt_id", "jobs",
            "config_ref", "config_sha256", "source_commit", "source_dirty", "source_git_state", "config_layout",
            "resource_policy", "owner_wait")
    return RESUME.row_digest({key: row.get(key) for key in keys})


def resource_key(row):
    keys = ("run_id", "pid", "starttime", "command", "route", "node", "jobs", "owner_wait", "command_hash", "launch_argv", "process_group")
    return RESUME.row_digest({key: row.get(key) for key in keys})


def context(args, control):
    """Read only exact armed records under the current owner route and registry."""
    import owner_route_binding as OWNER
    route_file = getattr(args, "route_file", "")
    if not route_file or not Path(route_file).is_file():
        return None
    jobs = Path(args.jobs).resolve(strict=True)
    sup = supervisor()
    route = sup.load_route(route_file)
    ledger = sup.ledger_for(route, str(jobs))
    armed_rows = sup.read_armed(ledger)
    if not any(a.get("predecessor_kind") == "resource" for a in armed_rows.values()):
        return None
    binding = None
    result = []
    for node, armed in armed_rows.items():
        if armed.get("predecessor_kind") != "resource":
            continue
        data = json.loads(Path(armed["resource_registry"]).read_text())
        row = data.get("runs", {}).get(armed["predecessor_id"])
        if not isinstance(row, dict) or row.get("resource_policy") != "supervised-owner":
            continue
        owner = row.get("owner_wait") or {}
        if owner.get("parent_attempt_id") != args.parent_attempt_id:
            continue
        if binding is None:
            binding, _ = OWNER.resolve_owner_route_lifecycle(jobs, owner_attempt_id=args.parent_attempt_id)
            if (binding is None or binding.route_file != str(Path(route_file).resolve())
                    or binding.route_id != args.route_id or binding.route_hash != args.route_hash):
                raise JOIN.JoinContractError("resource-owner-route-changed")
        expected = {"parent_attempt_id": args.parent_attempt_id, "session_id": control.thread_id,
            "route_id": args.route_id, "route_hash": args.route_hash, "jobs": str(jobs)}
        parent = JOIN.exact_attempt_row(jobs, args.parent_attempt_id)
        if parent.status not in {"open", "running"}:
            raise JOIN.JoinContractError("resource-owner-not-open")
        OWNER._owner_row_proof(parent.raw.split("\t"), parent.metadata, route=route, environ={})
        if (any(owner.get(key) != value for key, value in expected.items())
                or str(owner.get("owner_pid")) != parent.metadata.get("pid")
                or owner.get("owner_start") != parent.metadata.get("pid_start")
                or row.get("parent_attempt_id") != args.parent_attempt_id
                or row.get("route") != binding.route_file or row.get("node") != node
                or row.get("jobs") != str(jobs) or not row.get("command")
                or type(row.get("pid")) is not int or row["pid"] <= 0 or not row.get("starttime")
                or armed.get("route_id") != args.route_id or armed.get("route_hash") != args.route_hash
                or armed.get("route_file") != binding.route_file or armed.get("jobs") != str(jobs)
                or armed.get("resource_binding") != resource_body_digest(row)
                or armed.get("successor_external") is not True or armed.get("successor_command") is not None):
            raise JOIN.JoinContractError("resource-owner-binding-invalid")
        result.append((armed, row))
    return sup, route, ledger, result


def _write(path, parent, delivered, resource, phase):
    if path is None:
        raise JOIN.JoinContractError("resource-supervisor-state-required")
    with JOIN._supervisor_state_lock(path):
        state = JOIN.read_supervisor_phase_state(path, parent)
        if state is not None and state.outbox is not None:
            raise JOIN.JoinContractError("resource-model-outbox-pending")
        JOIN._write_supervisor_state_unlocked(path, parent, delivered, phase=phase, resource=resource)


def pending_prompt(path, parent, args=None, control=None):
    state = JOIN.read_supervisor_phase_state(path, parent)
    box = (state.resource or {}).get("outbox") if state else None
    if not box:
        return None
    if args is not None and control is not None:
        receipt = box["receipt"]
        expected = {"parent_attempt_id": parent, "session_id": control.thread_id,
                    "route_id": args.route_id, "route_hash": args.route_hash,
                    "jobs": str(Path(args.jobs).resolve())}
        found = context(args, control)
        row = next((r for _, r in found[3] if resource_key(r) == box["key"]), None) if found else None
        if (any(receipt.get(k) != v for k, v in expected.items()) or row is None
                or RESUME.row_digest(row) != receipt.get("resource_sha256")):
            raise JOIN.JoinContractError("resource-outbox-binding-changed")
    return ("Runtime resource receipt (not a model child or verification PASS): "
        + json.dumps(box["receipt"], sort_keys=True, separators=(",", ":"))
        + "\nUse pending user corrections first. Continue only the already authorized next work; "
        "do not restart the resource. Exit is not workflow completion.")


def acknowledge(path, parent, receipt_id):
    """Only the returning receiving turn acknowledges this exact resource receipt."""
    if not receipt_id:
        return False
    with JOIN._supervisor_state_lock(path):
        state = JOIN.read_supervisor_phase_state(path, parent)
        resource = dict(state.resource or {}) if state else {}
        box = resource.get("outbox")
        if not box or box["receipt_id"] != receipt_id:
            return False
        resource["delivered"] = resource["delivered"] + [box["key"]]
        resource["outbox"] = None
        JOIN._write_supervisor_state_unlocked(path, parent, set(state.delivered_attempt_ids),
            phase="running-turn", outbox=state.outbox, resource=resource)
        return True


def wait(args, path, control, delivered, emit, *, sleep=time.sleep):
    """No model calls, continuation spend, replacement or payload launch in this loop."""
    pending = pending_prompt(path, args.parent_attempt_id, args, control)
    if pending:
        return pending
    found = context(args, control)
    if found is None:
        return None
    sup, route, ledger, candidates = found
    state = JOIN.read_supervisor_phase_state(path, args.parent_attempt_id)
    resource = dict(state.resource or {}) if state else {}
    if resource and resource["session_id"] != control.thread_id:
        raise JOIN.JoinContractError("resource-native-session-changed")
    resource = resource or {"session_id": control.thread_id, "delivered": [], "outbox": None}
    candidates = [(a, r) for a, r in candidates if resource_key(r) not in resource["delivered"]]
    if not candidates:
        return None
    # A model stage admits at most one next resource per receiving turn. Every
    # additional exact resource stays armed and will be collected subsequently.
    armed, original = candidates[0]
    key = resource_key(original)
    _write(path, args.parent_attempt_id, delivered, resource, "parked")
    emit({"type": "dispatch.supervisor.resource-parked", "parent_attempt_id": args.parent_attempt_id,
          "node": armed["node"], "run_id": original["run_id"]})
    while True:
        if control.pending():
            return "Continue the same work using the pending user correction; preserve the active resource."
        current = context(args, control)
        if current is None:
            raise JOIN.JoinContractError("resource-owner-context-lost")
        exact = next(((a, r) for a, r in current[3] if resource_key(r) == key), None)
        if exact is None:
            raise JOIN.JoinContractError("resource-owner-row-changed")
        armed, row = exact
        # Owner watches use the existing external successor surface. Polling can
        # record/claim evidence, but cannot spawn the owner's next model leg.
        if control.pending():
            continue
        sup.poll_once(route, ledger)
        evidence = sup.resource_evidence(armed)
        stage = ledger.state().get("nodes", {}).get(armed["node"], {})
        lost_watch = not RESUME.supervisor_alive(row.get("supervision"))
        if stage.get("state") == "STAGE_SUCCEEDED" or stage.get("state") in {"FAILED_RETRYABLE", "FAILED_TERMINAL", "CANCELLED"} or lost_watch:
            current_rows = context(args, control)[3]
            receipt_row = next((r for _, r in current_rows if resource_key(r) == key), None)
            if receipt_row is None:
                raise JOIN.JoinContractError("resource-owner-row-changed")
            artifact = sup.artifact_evidence(armed)
            proven = (stage.get("state") == "STAGE_SUCCEEDED" and evidence.get("succeeded")
                and evidence.get("liveness") == "exited" and evidence.get("exit_code") == 0
                and sup.runner().read_sentinel(receipt_row.get("sentinel")) == 0
                and (stage.get("evidence") or {}).get("resource_sha256") == RESUME.row_digest(receipt_row)
                and not artifact.get("missing"))
            outcome = ("cancelled" if receipt_row.get("cancel_requested") else
                       "succeeded" if proven else "needs-attention")
            receipt = {"type": "resource-completion", "parent_attempt_id": args.parent_attempt_id,
                "session_id": control.thread_id, "route_id": args.route_id, "route_hash": args.route_hash,
                "jobs": str(Path(args.jobs).resolve()), "node": armed["node"], "run_id": row["run_id"],
                "resource_key": key, "resource_sha256": RESUME.row_digest(receipt_row), "state": outcome, "exit_code": evidence.get("exit_code"),
                "reason": "resource-watch-lost" if lost_watch and not evidence.get("terminal") else stage.get("state"),
                "verification_pass": False, "workflow_complete": False,
                "successors": list(armed["successors"]) if outcome == "succeeded" else []}
            digest = RESUME.row_digest(receipt)
            resource["outbox"] = {"receipt_id": "resource-" + digest[:32], "digest": digest,
                                  "key": key, "receipt": receipt}
            _write(path, args.parent_attempt_id, delivered, resource, "deliverable")
            return pending_prompt(path, args.parent_attempt_id, args, control)
        sleep(1)


def needs_recovery(path, parent, failed=False):
    state = JOIN.read_supervisor_phase_state(path, parent)
    return bool(state and state.resource and
                (failed or state.phase == "parked" or state.resource.get("outbox")))


def durable_native_session(args):
    """The already sealed owner route, before starting its native session."""
    path = getattr(args, "route_file", "")
    if not path:
        return False
    route = supervisor().load_route(path)
    return (not RESUME.route_selected(route) and any(
        n.get("kind") == "resource-runner" and
        (n.get("continuation") or {}).get("kind") == "supervised"
        for n in route.get("nodes", [])))
