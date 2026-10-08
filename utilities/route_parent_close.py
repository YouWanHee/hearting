"""The existing close's shared process cleanup, for every owner harness.

Intent and replay live in the existing workflow journal and attempt registry,
outside producer artifacts. The journal is not an artifact protection scheme.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import dispatch_contract as DC
import workflow_state as WS

NOTE = "cancelled-by-parent"
OPEN = {"open", "running"}


def jobs_path(jobs=None):
    return Path(jobs) if jobs else DC.resolve_global_registry(
        DC.resolve_agent_home(), None, 0, "close").path


def requested(metadata):
    return metadata.get("parent_close_requested") == "1"


def intent(route, jobs=None):
    if not route.get("route_id") or not route.get("route_hash"):
        return None
    jobs = jobs_path(jobs)
    # Before the first dispatch there is no registry and no cancellation.
    if not jobs.exists():
        return None
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    return ledger_intent(route, ledger)


def ledger_intent(route, ledger):
    return next((e["evidence"]["parent_close"] for e in ledger.journal()
                 if e.get("route_hash") == route["route_hash"]
                 and isinstance(e.get("evidence"), dict)
                 and isinstance(e["evidence"].get("parent_close"), dict)), None)


def settled_result(route, ledger):
    return next((entry["evidence"]["parent_close_result"] for entry in ledger.journal()
                 if entry.get("route_hash") == route["route_hash"]
                 and isinstance(entry.get("evidence"), dict)
                 and isinstance(entry["evidence"].get("parent_close_result"), dict)), None)


def row_requested(metadata, jobs):
    if requested(metadata):
        return True
    route = {"route_id": metadata.get("owner_route_id") or metadata.get("route_id"),
             "route_hash": metadata.get("owner_route_hash") or metadata.get("route_hash")}
    value = intent(route, jobs)
    return bool(value and metadata.get("attempt_id") in value["attempts"])


def _rows(jobs):
    rows = {}
    if not jobs.exists():
        return rows
    for line in jobs.read_text().splitlines():
        fields = line.split("\t")
        if len(fields) == 6:
            meta = DC.parse_registry_metadata(fields[5])
            aid = meta.get("attempt_id")
            if aid:
                if aid in rows:
                    raise ValueError("parent-close-attempt-not-unique")
                rows[aid] = (fields, meta)
    return rows


def _owner(route, path, jobs, rows):
    import owner_route_binding as OWNER
    import route_authority as AUTH
    candidates = []
    for aid, (fields, meta) in rows.items():
        if meta.get("worker_type") != "owner":
            continue
        if route["route_id"] not in {meta.get("owner_route_id"), meta.get("route_id")}:
            continue
        binding, _ = OWNER.resolve_owner_route_lifecycle(jobs, owner_attempt_id=aid)
        if binding and (binding.route_id, binding.route_hash, binding.route_file) == (
                route["route_id"], route["route_hash"], str(path.resolve())):
            candidates.append((aid, fields, meta))
    active = [c for c in candidates if c[1][1] in OPEN]
    if len(active) > 1:
        raise ValueError("parent-close-current-owner-ambiguous")
    if not active:
        # A previously committed PASS wins over a later parent request.
        if any(DC.verdict_pass(c[2]) for c in candidates):
            return None, True
        return None, False
    aid, fields, meta = active[0]
    if aid == os.environ.get("AGENT_DISPATCH_ATTEMPT_ID"):
        raise ValueError("parent-close-requires-parent")
    if not AUTH.owns(meta, AUTH.default_parent_session_id(), jobs):
        raise ValueError("parent-close-owner-not-owned")
    OWNER._owner_row_proof(fields, meta, route=route, environ={})
    return aid, False


def _owned_attempts(route, rows, owner):
    selected = {owner} if owner else set()
    while True:
        children = {aid for aid, (fields, meta) in rows.items()
                    if meta.get("parent_attempt_id") in selected
                    and meta.get("route_id", "") in {"", route["route_id"]}
                    and meta.get("route_hash", "") in {"", route["route_hash"]}
                    and fields[2] == rows[owner][0][2]
                    and (fields[3] == rows[owner][0][3] or DC.is_linked_worktree_slice(meta))}
        if children <= selected:
            return selected
        selected |= children


def request(route, path, *, jobs=None, stop_resources=False, summary=None, commit=None):
    """Serialize the intent with PASS and actual launch claims; no signal here."""
    jobs, path = jobs_path(jobs), Path(path)
    if not jobs.exists():
        return None
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    with ledger.lock():
        existing = intent(route, jobs)
        if existing:
            return existing
        outcome = path.with_suffix(".outcome.json")
        if outcome.exists() and json.loads(outcome.read_text()).get("terminal_gate_proven") is True:
            return None
        jobs.parent.mkdir(parents=True, exist_ok=True)
        with Path(str(jobs) + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            rows = _rows(jobs)
            owner, succeeded = _owner(route, path, jobs, rows)
            if succeeded or not owner:
                return None
            attempts = _owned_attempts(route, rows, owner)
            value = {"route": route, "route_file": str(path.resolve()),
                     "owner_attempt_id": owner, "attempts": sorted(attempts),
                     "stop_resources": bool(stop_resources), "at": WS.now_iso(),
                     "summary": summary, "head_commit": commit}
            value["resources"] = linked_resources(route, path, jobs, attempts)
            # The intent is durable before either the launch fence or any signal.
            ledger._append({"at": value["at"], "route_id": route["route_id"],
                            "route_hash": route["route_hash"], "actor": "parent-close",
                            "evidence": {"parent_close": value}})
            updates = {}
            for aid, (fields, meta) in rows.items():
                if aid in attempts and fields[1] in OPEN:
                    fields[5] = DC._updated_attempt_metadata(fields[5], {
                        "parent_close_requested": "1", "parent_close_route_id": route["route_id"],
                        "parent_close_route_hash": route["route_hash"],
                        "parent_close_stop_resources": str(int(stop_resources)),
                    })
                updates[aid] = "\t".join(fields)
            if rows:
                lines = []
                for line in jobs.read_text().splitlines():
                    fields = line.split("\t")
                    aid = DC.parse_registry_metadata(fields[5]).get("attempt_id") if len(fields) == 6 else None
                    lines.append(updates.get(aid, line))
                DC._atomic_registry_replace(jobs, lines)
            return value


def linked_resources(route, path, jobs, attempts):
    """Discover existing run records, never GPU census or a project-name match."""
    import resource_run_registry as RR
    registries, _ = RR.indexed_paths()
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    armed_dir = ledger.root / "armed"
    for record in armed_dir.glob("*.json"):
        try:
            armed = json.loads(record.read_text())
        except (OSError, ValueError):
            continue
        if (armed.get("route_id") == route["route_id"] and armed.get("route_hash") == route["route_hash"]
                and armed.get("resource_registry")):
            registries.append(Path(armed["resource_registry"]))
    result = []
    for registry in sorted(set(registries)):
        try:
            data = json.loads(registry.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
            continue
        for rid, run in data.get("runs", {}).items():
            if not isinstance(run, dict):
                continue
            owner = run.get("owner_wait") or {}
            if (run.get("route") != str(path.resolve())
                    or run.get("parent_attempt_id") not in attempts
                    or run.get("jobs") not in {None, "", str(jobs)}
                    or owner.get("route_hash", route["route_hash"]) != route["route_hash"]
                    or run.get("node") not in {n["id"] for n in route.get("nodes", [])}):
                continue
            result.append({"kind": "resource", "run_id": rid, "registry": str(registry), "row": run})
    # compute-hosts already records route + starting attempt in each run's meta.
    compute = _compute()
    try:
        config = compute.load_config()
    except compute.ConfigError:
        return result
    for meta_path in config["run_root"].glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        provenance = meta.get("provenance") or {}
        binding = provenance.get("route") or {}
        if (binding.get("route_id") == route["route_id"] and binding.get("route_file") == str(path.resolve())
                and provenance.get("attempt_id") in attempts):
            state = compute._run_state(config, meta_path.parent.name)
            result.append({"kind": "compute", "run_id": meta_path.parent.name,
                           "state": "stopped" if state["stop_reason"] else state["state"],
                           "config": str(compute.config_path())})
    return result


def _compute():
    spec = importlib.util.spec_from_file_location("parent_close_compute", Path(__file__).with_name("compute-hosts.py"))
    compute = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compute)
    return compute


def _protected(resources):
    protected = set()
    for resource in resources:
        run = resource.get("row", {})
        supervision = run.get("supervision") or {}
        if supervision.get("pid") and supervision.get("starttime"):
            observed, actual, _ = DC.process_observation(int(supervision["pid"]))
            if observed == "present" and actual == str(supervision["starttime"]):
                protected.add((int(supervision["pid"]), str(supervision["starttime"])))
        try:
            pid, start = int(run["pid"]), str(run["starttime"])
        except (KeyError, ValueError, TypeError):
            continue
        observed, actual, _ = DC.process_observation(pid)
        if observed == "present" and actual == start:
            protected.add((pid, start))
            if int(run.get("process_group", pid)) == pid:
                group = DC.process_group_observation(pid)
                protected.update((p, s) for p, s, state in group.members if state != "Z")
    # A run can share the owner's group. Preserve its descendants, not its
    # ancestors/controller (the model supervisor may itself be that controller).
    if not protected:
        return protected
    children = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            tail = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            children.append((int(entry.name), tail[19], int(tail[1])))
        except (FileNotFoundError, ProcessLookupError):
            continue
    while True:
        descendants = {(p, s) for p, s, parent in children if parent in {p for p, _ in protected}}
        if descendants <= protected:
            return protected
        protected |= descendants


def _agent_processes(meta, resources):
    """Filter resource branches before selecting exact agent PID/start pairs."""
    if meta.get("launch_claimed") == "0" and not meta.get("pid"):
        return [], True
    identities = DC.authoritative_process_identities(meta)
    identity = identities[0] if identities else None
    if identity is None:
        return [], DC.attempt_process_quiescence(meta).state == "quiescent"
    visibility, actual, leader_state = DC.process_observation(identity.pid)
    if visibility == "inaccessible":
        return [], False
    # Reuse is absence of the old leader, never authority over the new group.
    reused = visibility == "present" and actual != identity.expected_start
    group = (DC.ProcessGroupObservation("empty") if reused else
             DC.process_group_observation(identity.pid))
    tagged = DC.attempt_tagged_descendants(meta)
    members = {(p, s) for p, s, state in (*group.members, *tagged.members) if state != "Z"}
    if visibility == "present" and actual == identity.expected_start and leader_state != "Z":
        members.add((identity.pid, actual))
    members -= _protected(resources)
    # Resource-run environment tags also identify a re-setsid branch.
    run_ids = {r["run_id"] for r in resources}
    agents = []
    for pid, start in sorted(members, reverse=True):
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        except OSError:
            return [], False
        if any(b"HEARTING_COMPUTE_RUN_ID=" + rid.encode() in env for rid in run_ids):
            continue
        agents.append((pid, start))
    return agents, group.state != "unverifiable" and tagged.state != "unverifiable"


def _signal(pid, start, signum):
    # Adjacent exact checks; no killpg that could include a preserved run.
    for _ in range(2):
        visibility, actual, state = DC.process_observation(pid)
        if visibility == "missing" or (visibility == "present" and (actual != start or state == "Z")):
            return
        if visibility != "present":
            return
    try:
        os.kill(pid, signum)
    except OSError:
        pass


def _stop_resources(resources):
    import resource_run_registry as RR
    for resource in resources:
        members = ()
        if resource["kind"] == "resource":
            if resource["row"].get("status") in {"succeeded", "failed"}:
                continue
            if RR.classify_identity(resource["row"])[0] != "working":
                continue
            command = [sys.executable, str(Path(__file__).with_name("resource-runner.py")),
                       "--registry", resource["registry"], "stop", "--run-id", resource["run_id"]]
            run = resource["row"]
            pid = int(run.get("pid", 0))
            start = str(run.get("starttime", ""))
            if DC.exact_process_group_signal_authority(pid, start) == "authoritative":
                members = DC.process_group_observation(pid).members
            else:
                # Legacy runs can share an agent group. The runner's group
                # stop is inapplicable; select only the exact resource branch.
                branch = {**resource, "row": {k: v for k, v in run.items() if k != "supervision"}}
                members = tuple((p, s, "S") for p, s in _protected([branch]))
        else:
            if resource["state"] != "running":
                continue
            command = [sys.executable, str(Path(__file__).with_name("compute-hosts.py")),
                       "stop", resource["run_id"]]
        try:
            stop_env = ({**os.environ, "COMPUTE_HOSTS_CONFIG": resource["config"]}
                        if resource["kind"] == "compute" else None)
            stopped = subprocess.run(command, capture_output=True, text=True, timeout=30, env=stop_env)
        except (OSError, subprocess.TimeoutExpired):
            continue  # Re-observation below retains a live run as pending.
        if resource["kind"] == "resource":
            # The existing stop sends TERM. Complete its exact group cleanup
            # here, escalating only still-identical members after the grace.
            if stopped.returncode != 0:
                if RR.classify_identity(resource["row"])[0] != "working":
                    continue
                for member, birth, _state in members:
                    _signal(member, birth, signal.SIGTERM)
            time.sleep(0.3)
            for member, birth, _state in members:
                _signal(member, birth, signal.SIGKILL)


def continue_close(value, *, jobs=None, grace=0.3, kill_wait=0.5):
    """Same controller for close, join and post-exit recovery; bounded per pass."""
    jobs = jobs_path(jobs)
    route, path = value["route"], Path(value["route_file"])
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    prior = settled_result(route, ledger)
    if prior:
        _finish_existing_cycle(route)
        return prior
    resources = _resources_for_close(value, jobs)
    rows = _rows(jobs)
    pending = []
    # Reconstruct annotations after a crash between durable intent and registry.
    for aid in value["attempts"]:
        if aid not in rows:
            continue
        fields, meta = rows[aid]
        DC.validate_attempt_metadata(meta)
        if fields[1] not in OPEN and meta.get("parent_close_settled") == "1":
            continue
        if fields[1] in OPEN and not requested(meta):
            DC.annotate_attempt_row(jobs, aid, {
                "parent_close_requested": "1", "parent_close_route_id": route["route_id"],
                "parent_close_route_hash": route["route_hash"],
                "parent_close_stop_resources": str(int(value["stop_resources"]))})
            meta = _rows(jobs)[aid][1]
        deadline = time.monotonic() + grace + kill_wait
        sent = set()
        settled = False
        while True:
            try:
                processes, observed = _agent_processes(meta, resources)
            except OSError:
                processes, observed = [], False
            if not observed:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
                continue
            if not processes:
                if fields[1] not in OPEN:
                    # Keep a child's earlier PASS/FAIL while remembering that
                    # its physical cleanup was also observed for this close.
                    DC.annotate_attempt_row(jobs, aid, {"parent_close_settled": "1"})
                    settled = True
                else:
                    never = DC.attempt_row_never_started(fields)
                    settled = DC.close_attempt_row_if(jobs, aid, NOTE,
                        lambda fresh: requested(DC.parse_registry_metadata(fresh[5])) and
                            _agent_processes(DC.parse_registry_metadata(fresh[5]), resources) == ([], True),
                        evidence={"failure_class": "cancelled", "parent_close_settled": "1",
                                  **({"launch_outcome": "never-launched"} if never else {})})
                    if not settled:
                        current = _rows(jobs).get(aid)
                        settled = bool(current and current[0][1] not in OPEN
                                       and current[1].get("parent_close_settled") == "1")
                if settled or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
                continue
            escalated = time.monotonic() >= deadline - kill_wait
            for pid, start in processes:
                key = (pid, start, escalated)
                if key not in sent:
                    _signal(pid, start, signal.SIGKILL if escalated else signal.SIGTERM)
                    sent.add(key)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if not settled:
            pending.append(aid)
    if value["stop_resources"]:
        _stop_resources(resources)
    resources = _resources_for_close(value, jobs)
    resource_states = []
    import resource_run_registry as RR
    for resource in resources:
        if resource["kind"] == "resource":
            live = RR.classify_identity(resource["row"])[0]
            if value["stop_resources"] and live == "stale":
                visibility, actual, state = DC.process_observation(int(resource["row"].get("pid", 0)))
                if visibility != "missing" and not (visibility == "present" and (
                        actual != str(resource["row"].get("starttime")) or state == "Z")):
                    live = "termination-pending"
        else:
            live = resource["state"]
        resource_states.append({"run_id": resource["run_id"], "kind": resource["kind"], "state": live,
                                "preserved": not value["stop_resources"]})
        if value["stop_resources"] and live in {"working", "reaping", "running", "termination-pending"}:
            pending.append("resource:" + resource["run_id"])
    if pending:
        return {"state": "termination-pending", "reason": NOTE, "pending_attempts": pending,
                "resources": resource_states}
    with ledger.lock():
        prior = settled_result(route, ledger)
        if prior:
            return prior
        state = ledger.state()
        for node in route.get("nodes", []):
            previous = (state["nodes"].get(node["id"]) or {}).get("state")
            if previous not in {"STAGE_SUCCEEDED", "CANCELLED", "FAILED_TERMINAL"}:
                ledger.record(node["id"], "CANCELLED", evidence={"reason": NOTE}, actor="parent-close")
        if ledger.state()["workflow_state"] != "CANCELLED":
            ledger.set_workflow_state("CANCELLED", evidence={"reason": NOTE}, actor="parent-close")
        target = path.with_suffix(".outcome.json")
        if target.exists():
            existing = json.loads(target.read_text())
            if existing.get("terminal_gate_proven") is True:
                return existing
            if existing.get("reason") == NOTE:
                outcome = existing
            else:
                outcome = None
        else:
            outcome = None
        outcome = outcome or {"schema_version": 4, "route_id": route["route_id"], "route_hash": route["route_hash"],
                   "route_file": str(path), "cwd": route["cwd"], "capability": route["capability"],
                   "effective_intensity": route["effective_intensity"], "closed_at": WS.now_iso(),
                   "terminal_gate_proven": False, "state": "cancelled", "reason": NOTE,
                   "disposition": "cancelled", "summary": value.get("summary"), "head_commit": value.get("head_commit"),
                   "owner_attempt_id": value["owner_attempt_id"], "resources": resource_states}
        ledger._append({"at": WS.now_iso(), "route_id": route["route_id"],
                        "route_hash": route["route_hash"], "actor": "parent-close",
                        "evidence": {"parent_close_result": outcome}})
        WS._atomic_write(target, json.dumps(outcome, indent=2) + "\n")
    _finish_existing_cycle(route)
    return outcome


def _finish_existing_cycle(route):
    # This is ordinary producer closure, never a dependency on retaining its
    # files. Absence/deletion leaves the cancellation result fully replayable.
    import artifact_producer as AP
    root = Path(route["artifact_root"])
    try:
        cycle = AP.route_cycle_for(root, route)
        if cycle and cycle.get("state") == "open":
            AP.finalize(root, cycle_id=cycle["cycle_id"], state="abandoned",
                        abandon_reason="operator-decision", lock_timeout=0.5)
    except (OSError, ValueError, AP.ProducerError):
        pass


def recover_attempt(jobs, metadata):
    route = {"route_id": metadata.get("parent_close_route_id") or metadata.get("owner_route_id") or metadata.get("route_id"),
             "route_hash": metadata.get("parent_close_route_hash") or metadata.get("owner_route_hash") or metadata.get("route_hash")}
    value = intent(route, jobs)
    return continue_close(value, jobs=jobs) if value else None


def _resources_for_close(value, jobs):
    recorded = {(r["kind"], r["run_id"]): r for r in value.get("resources", [])}
    current = linked_resources(value["route"], Path(value["route_file"]), jobs, set(value["attempts"]))
    recorded.update({(r["kind"], r["run_id"]): r for r in current})
    return list(recorded.values())


def close(route, path, *, jobs=None, stop_resources=False, summary=None, commit=None):
    value = request(route, path, jobs=jobs, stop_resources=stop_resources, summary=summary, commit=commit)
    return continue_close(value, jobs=jobs) if value else None
