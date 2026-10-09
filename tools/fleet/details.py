"""Passive live detail fills joined to exact basic observations.

The ordinary snapshot pump owns existence/liveness/resources. This module runs
on one coalesced RefreshPump worker and can neither create rows nor change those
observations. A slow filesystem read here leaves basic refresh independent.
"""
from dataclasses import dataclass, replace
import copy
import json
import time

from .model import Session


def identity(row):
    # Include process start even for a stable session/attempt: PID reuse must
    # never receive an old title, native NOW, or route projection.
    return ("session" if isinstance(row, Session) else "job",
            getattr(row, "harness", None), getattr(row, "session_id", None),
            getattr(row, "attempt_id", None), getattr(row, "pid", None),
            getattr(row, "proc_start", None), getattr(row, "cwd", None),
            getattr(row, "source", None))


def binding(row):
    return tuple(getattr(row, field, None) for field in (
        "route_id", "route_hash", "route_file", "route_node", "owner_route_id",
        "owner_route_hash", "owner_route_file", "parent_attempt_id", "parent_sid",
        "parent_harness", "parent_slug", "_runtime_session_id"))


def state_key(snapshot):
    """Projection authority can transfer only while the observed roster agrees.

    Activity timestamps/progress text do not gate titles or route details. Live
    job/resource changes and terminal registry evidence do gate route projection.
    """
    rows = tuple((identity(row), binding(row), getattr(row, "liveness", None))
                 for row in snapshot.sessions + snapshot.jobs)
    resources = tuple((r.run_id, r.parent_attempt_id, r.liveness,
                       r.route_id, r.route_hash, r.route_node, r.node,
                       r.pid, r.starttime, r.command_hash, r.exit_code) for r in snapshot.resources)
    evidence = {rid: json.dumps(value, sort_keys=True, default=str)
                for rid, value in snapshot.node_evidence.items()}
    return {"rows": rows, "resources": resources, "evidence": evidence}


def projection_current(row, previous, current, source):
    """Judge only this row's route/ownership neighbourhood, not unrelated work."""
    if not isinstance(source, dict):
        return False
    routes = {getattr(previous.work_projection, "route_id", None), row.route_id,
              getattr(row, "owner_route_id", None)}
    routes.update(node.get("route_id") for node in
                  (getattr(previous, "route_chain", None) or {}).get("nodes", ()))
    routes.discard(None)
    sid, attempt, slug = (getattr(row, "session_id", None),
                          getattr(row, "attempt_id", None), getattr(row, "slug", None))

    def scoped(state):
        observed = [item for item in state["rows"] if item[0] == identity(row)
                    or item[1][0] in routes or item[1][4] in routes
                    or (sid and item[1][8] == sid)
                    or (attempt and item[1][7] == attempt)
                    or (slug and item[1][10] == slug)]
        attempts = {item[0][3] for item in observed if item[0][3]}
        resources = [item for item in state["resources"]
                     if item[1] in attempts or item[3] in routes]
        evidence = {rid: state["evidence"].get(rid) for rid in routes}
        return sorted(observed, key=repr), sorted(resources, key=repr), evidence

    return scoped(current) == scoped(source)


@dataclass
class DetailSnapshot:
    source_key: object
    snapshot: object


def _enrich(snapshot):
    """Fill copied rows with display detail; never rescan or reclassify work."""
    from .collectors import dispatch, _adopt_child_titles, apply_peer_rows
    from . import projection, route_chain
    source_key = state_key(snapshot)
    value = copy.deepcopy(snapshot)
    sessions, jobs = value.sessions, value.jobs
    dispatch._fill_locations(jobs)
    dispatch._campaign_labels(jobs)
    for job in jobs:
        try:
            dispatch._enrich_claude_stream_session(job)
            dispatch._enrich_codex_attempt_session(job)
            dispatch._enrich_opencode_attempt_session(job)
            dispatch._enrich_attempt_summary(job)
            if job.qa_source in (None, "default") and (job.liveness != "dead" or job.afterglow
                    or getattr(job, "_dead_terminal_owner", False)):
                job.qa, job.qa_source = dispatch.effective_qa(
                    None, None, job.cwd, job.slug, job.key,
                    getattr(job, "capability_owner", None), job.worker_role,
                    artifact_root=job.artifact_root)
        except Exception:
            pass
    _adopt_child_titles(sessions, jobs)
    route_chain.enrich(sessions, jobs=jobs, node_evidence=value.node_evidence, now=time.time())
    degradations = dispatch._scan_degradations(
        set(value.node_evidence) | {j.route_id for j in jobs if j.route_id}, jobs=jobs)
    projection.attach_projections(sessions, jobs, node_evidence=value.node_evidence,
                                  degradations=degradations, resources=value.resources, now=time.time())
    # Optional header details retain their last observed value across cheap
    # ticks. Initial absence stays unknown; these observers never classify work.
    dispatch.collect.last_degradations = degradations
    try:
        dispatch.collect.last_pending_delivery = dispatch._pending_delivery_counts(
            dispatch._candidate_jobs_paths())
    except Exception:
        dispatch.collect.last_pending_delivery = None
    try:
        from .collectors import peer_messages
        peer = peer_messages.collect()
        apply_peer_rows(sessions, (peer or {}).get("by_session") or {})
    except Exception:
        pass
    return DetailSnapshot(source_key, value)


_DETAIL_READ_CACHE = None


def enrich(snapshot):
    """One artifact inventory/cache scope for the entire passive detail pass."""
    global _DETAIL_READ_CACHE
    from .collectors import dispatch
    reader = dispatch.artifact_reader
    if reader is None:
        return _enrich(snapshot)
    if _DETAIL_READ_CACHE is None:
        _DETAIL_READ_CACHE = reader.ReadCache()
    with reader.read_scope(_DETAIL_READ_CACHE):
        return _enrich(snapshot)


_METADATA = ("title", "campaign_label", "location_kind", "location_wt", "location_repo",
             "branch", "worktree_count", "branch_ahead", "branch_behind", "qa", "qa_source",
             "peer_last_recv", "peer_last_sent")


def merge(basic, detail):
    """Join description only; the newest basic observation always owns state."""
    if detail is None:
        return basic
    value = detail.snapshot
    current_state = state_key(basic)
    old = {}
    for row in value.sessions + value.jobs:
        old.setdefault(identity(row), []).append(row)
    sessions, jobs = [], []
    for target, rows in ((sessions, basic.sessions), (jobs, basic.jobs)):
        for row in rows:
            candidates = old.get(identity(row), ())
            if len(candidates) != 1 or binding(row) != binding(candidates[0]):
                target.append(row)
                continue
            previous = candidates[0]
            current = copy.copy(row)
            for field in _METADATA:
                observed = getattr(previous, field, None)
                if observed is not None:
                    setattr(current, field, observed)
            same_activity = (getattr(row, "liveness", None) == getattr(previous, "liveness", None)
                             and getattr(row, "_detail_activity_key", None) ==
                                 getattr(previous, "_detail_activity_key", None))
            if same_activity:
                if getattr(previous, "subagents", None) is not None:
                    current.subagents = previous.subagents
                context = getattr(current, "context", None)
                if getattr(current, "_dispatch_context_owned", False) and (
                        context is None or getattr(context, "used_pct", None) is None):
                    for field in ("context", "_context_evidence", "ctx_pct", "active_context_tokens",
                                  "context_window_tokens", "session_input_tokens", "session_cached_input_tokens",
                                  "session_output_tokens", "session_reasoning_output_tokens", "session_total_tokens"):
                        if hasattr(previous, field):
                            setattr(current, field, getattr(previous, field))
                summary = getattr(previous, "summary", None)
                old_ts, current_ts = getattr(previous, "summary_ts", None), getattr(row, "summary_ts", None)
                if summary and (not getattr(row, "summary", None) or
                                (old_ts is not None and old_ts > (current_ts or 0))):
                    current.summary, current.summary_ts = summary, old_ts
                # Only OpenCode's bounded native read supplies extra command
                # evidence. Never transfer a command to an advanced attempt log.
                if (getattr(row, "harness", None) == "opencode"
                        and not isinstance(row, Session)
                        and getattr(row, "_detail_activity_key", None) is not None):
                    current.exec_tool = getattr(previous, "exec_tool", None)
            if projection_current(row, previous, current_state, detail.source_key):
                has_now = any(getattr(current, field, None) for field in (
                    "summary", "exec_child", "exec_tool", "resource_children"))
                current._details_pending = (not same_activity or
                    (current.liveness == "working" and not has_now))
                current.work_projection = previous.work_projection
                current.route_chain = getattr(previous, "route_chain", None)
                current.cap_grounding = getattr(previous, "cap_grounding", None)
                if not isinstance(current, Session):
                    current.stage = previous.stage
                    if previous.resource_wait is not None:
                        current.resource_wait = previous.resource_wait
            target.append(current)
    return replace(basic, sessions=sessions, jobs=jobs,
                   memory=value.memory, governor=value.governor,
                   hearting=value.hearting or basic.hearting)
