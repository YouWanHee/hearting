"""Read-only conversion of already collected Fleet records into the explorer view model.

Every section below turns facts a Fleet reader already decided (liveness, identity, route
projection, usage, compute hosts, memory, peers) into records of exactly the shape the checked
fixture uses. A value the source does not carry is ``None`` (drawn ``·``), a section with no source
is omitted, and nothing is guessed. There is no I/O here: the slow per-selection reads stay in the
background focus reader and arrive through ``accepted_detail``.
"""
from collections.abc import Mapping
import datetime
import hashlib
import math
import os
import re
import time

from .view_model import ExplorerViewModel

HARNESSES = ("claude", "codex", "opencode")
_SEG_DIM = "dim"


def _get(value, *keys, default=None):
    for key in keys:
        candidate = value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
        if candidate is not None:
            return candidate
    return default


def _records(value):
    return tuple(value or ())


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _text(value):
    return value if isinstance(value, str) and value else None


def _dur(minutes):
    """Whole minutes -> the board's duration text; None when unknown."""
    value = _num(minutes)
    if value is None or value < 0:
        return None
    value = int(value)
    if value < 60:
        return f"{value}m"
    hours, minute = divmod(value, 60)
    if hours < 24:
        return f"{hours}h {minute:02d}m"
    days, hour = divmod(hours, 24)
    return f"{days}d {hour}h"


def _since(epoch, now):
    value = _num(epoch)
    if value is None or now is None:
        return None
    return _dur(max(0, (now - value) / 60))


def _iso_age(value, now):
    """``12m ago`` for an ISO timestamp (or epoch seconds); None when it cannot be read."""
    if isinstance(value, str):
        try:
            stamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        value = stamp.timestamp()
    age = _since(value, now)
    return f"{age} ago" if age else None


def _clock(epoch):
    value = _num(epoch)
    if value is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(value).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def _model_pair(model):
    """``[text, colour key]`` for a runtime model name, or None when it is unknown."""
    name = _text(model)
    if name is None:
        return None
    try:
        from .. import render
        text = render._clean_model(render._dispatch_display_model(name)) or name
        key = render._model_key(name)
    except Exception:
        text = name.split(" (", 1)[0]
        key = "fam_other"
    return [text, key]


def _effort(value):
    text = _text(value)
    if text is None or text.strip().lower() in ("runtime-default", "inherit", "unknown", "default"):
        return None
    return text


def _session_model_effort(session, state):
    """``(model pair, effort)`` the way the default board's harness cell resolves them: a stale or
    dead row has no live telemetry (bare harness); the runtime's own default effort is ``default``."""
    if state in ("stale", "dead"):
        return None, None
    model = _model_pair(_get(session, "model", "m"))
    effort = _effort(_get(session, "effort", "e"))
    if effort is None and model is not None and _get(session, "effort_default"):
        effort = "default"
    return model, effort


def _short_capability(name):
    if not isinstance(name, str):
        return None
    return name[len("autopilot-"):] if name.startswith("autopilot-") else name


# -- sessions -----------------------------------------------------------------------------

def _is_main(session):
    if _get(session, "is_child") or _get(session, "mem_worker"):
        return False
    return _get(session, "process_role") != "registered-worker"


def _tag_for(session, used):
    tag = _text(_get(session, "session_tag", "tag"))
    if tag is None:
        sid = _text(_get(session, "session_id", "sid", "id"))
        if sid is None:
            return None
        seed = f"{_get(session, 'harness', default='')}:{sid}"
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
        tag = digest[:2]
        for offset in range(1, 40):
            if tag not in used:
                break
            tag = digest[offset:offset + 2]
    return tag


def _resolver(snapshot):
    resolver = _get(snapshot, "project_of")
    if callable(resolver):
        return resolver
    display = _get(snapshot, "display_snapshot")
    try:
        from ..model import project_of
    except Exception:
        project_of = None
    if display is not None and hasattr(display, "project_for") and project_of is not None:
        return lambda cwd: display.project_for(cwd, project_of)
    return project_of


UNKNOWN_REPO = ""               # a row whose cwd gives no project: grouped, never labelled (the default board hides it too)
NO_PROJECT_LABELS = frozenset({"(unknown)", "(root)"})


def _shown_repo(label):
    """The project label to draw: the default board's hidden labels are not names, so they become no name."""
    text = _text(label)
    return UNKNOWN_REPO if text is None or text in NO_PROJECT_LABELS else text


def _home_forms():
    forms = {os.path.expanduser("~")}
    try:
        forms.add(os.path.realpath(os.path.expanduser("~")))
    except OSError:
        pass
    return tuple(sorted(f.rstrip("/") for f in forms if f and f != "/"))


_HOME_FORMS = _home_forms()
HOME_LABEL = "~"


def _home_folder(label, cwd):
    """Whether ``label`` is the home folder itself: the user's folder name, and ``cwd`` lies inside a
    folder of that name (``$HOME`` or the data root that carries the same name)."""
    names = {os.path.basename(home) for home in _HOME_FORMS}
    parts = [part for part in str(cwd).split("/") if part]
    return label in names and label in parts


def _path_label(cwd):
    try:
        from ..display_snapshot import path_label
        return _shown_repo(path_label(cwd))
    except Exception:
        return _shown_repo(os.path.basename(str(cwd).rstrip("/")))


def _repo_of(resolver, cwd, fallback=None):
    """The project group of a row; a row with no usable cwd is grouped under the nameless group. A folder
    inside the home directory is never grouped under the home folder itself (a git repository at
    ``$HOME`` would make every such session one ``<user>/`` project): it keeps its own folder name."""
    if _text(cwd) is None:
        return _shown_repo(fallback)
    label = None
    if callable(resolver):
        try:
            label = _text(resolver(cwd))
        except Exception:
            label = None
    if label is None:
        label = _text(fallback) or os.path.basename(str(cwd).rstrip("/")) or UNKNOWN_REPO
    label = _shown_repo(label)
    if _home_folder(label, cwd):
        last = [part for part in str(cwd).split("/") if part][-1:]
        return HOME_LABEL if last == [label] else _path_label(cwd)
    return label


def _context_gauge(session):
    """``(used %, window in millions of tokens)`` exactly as the default board's context row resolves
    them: the resolved projection when the row has one, else the runtime's own gauge. A stale or dead
    row draws no context."""
    if _get(session, "liveness", "status", "state", "st") in ("stale", "dead"):
        return None, None
    context = _get(session, "context")
    if context is not None and not isinstance(context, (int, float)):
        pct = _num(_get(context, "used_pct"))
    else:
        pct = _num(context)
        if pct is None:
            for key in ("context_pct", "ctx_pct", "ctx"):
                pct = _num(_get(session, key))
                if pct is not None:
                    break
    window = _num(_get(session, "context_display_window_tokens"))
    raw_window = _num(_get(session, "context_window_tokens"))
    if pct is None and window is None:
        try:
            from .. import render
            native_pct, native_window = render._native_context_gauge(session)
        except Exception:
            native_pct, native_window = None, raw_window
        pct = _num(native_pct)
        raw_window = _num(native_window) or raw_window
    tokens = window or raw_window
    if tokens:
        scaled = round(tokens / 1_000_000, 3)
    else:
        scaled = _num(_get(session, "window", "win"))
    return (None if pct is None else int(round(pct))), scaled


def _running(session):
    parts = []
    tool = _get(session, "exec_tool")
    if isinstance(tool, Mapping) and _text(tool.get("name")):
        parts.append([tool["name"], "soft"])
    child = _get(session, "exec_child")
    if isinstance(child, Mapping) and _text(child.get("comm")):
        parts.append([child["comm"], _SEG_DIM])
    subagents = _get(session, "subagents")
    if subagents:
        parts.append([f"{len(subagents)} subagent", _SEG_DIM])
    return parts or None


def _ask_kind(session):
    state = _get(session, "interaction_state")
    kind = _text(_get(state, "kind")) if state is not None else None
    return kind


def _session_records(snapshot, now, resolver):
    used, rows, by_key = set(), [], {}
    for session in _records(_get(snapshot, "sessions", default=())):
        if not _is_main(session):
            continue
        tag = _tag_for(session, used)
        if tag is None or tag in used:
            continue
        used.add(tag)
        harness = _text(_get(session, "harness", "adapter", "h"))
        cwd = _get(session, "cwd", "project")
        repo = _repo_of(resolver, cwd, _get(session, "repo", "slug"))
        state = _text(_get(session, "liveness", "status", "state", "st"))
        interaction = _get(session, "interaction_state")
        elapsed = _get(session, "elapsed_min")
        age = _dur(elapsed) if _num(elapsed) is not None else _text(_get(session, "age"))
        model, effort = _session_model_effort(session, state)
        context, window = _context_gauge(session)
        record = {
            "tag": tag, "harness": harness, "model": model, "effort": effort,
            "state": state, "steward": bool(_get(session, "steward", default=False)),
            "title": _text(_get(session, "title", "name")),
            "context": context, "window": window,
            "now": _text(_get(session, "summary", "now")),
            "ask": _ask_kind(session) if state == "blocked" else None,
            "age": age, "repo": repo, "repo_id": repo,
            "branch": _text(_get(session, "branch")),
            "pane": _text(_get(session, "herdr_pane", "pane", "pane_id")),
            "generation": None, "last_clear": None,
            "running": _running(session), "gpu": None, "pane_meta": None, "pane_lines": [],
        }
        rows.append(record)
        sid = _text(_get(session, "session_id", "sid", "id"))
        chain = _get(session, "route_chain")
        chain_routes = {_text(node.get("route_id")) for node in (chain.get("nodes") or ())
                        if isinstance(node, Mapping)} if isinstance(chain, Mapping) else set()
        by_key[tag] = {"session": session, "sid": sid, "interaction": interaction,
                       "ids": tuple(i for i in (sid, *(_get(session, "session_aliases") or ())) if _text(i)),
                       "chain": chain_routes - {None}}
    return rows, by_key


# -- routes -------------------------------------------------------------------------------

_NODE_STAGE = {"done": "done", "active": "active", "reconciling": "active", "recovering": "active",
               "failed": "failed"}


def _route_entities(snapshot):
    entities = []
    for key in ("sessions", "jobs", "route_entities"):
        entities.extend(_records(_get(snapshot, key, default=())))
    return entities


def _work(entity):
    return _get(entity, "work_projection")


def _collect_routes(snapshot):
    """route id -> {"view", "record", "jobs", "sessions"} in first-seen order."""
    found = {}
    for entity in _route_entities(snapshot):
        work = _work(entity)
        rid = _text(_get(work, "route_id")) if work is not None else None
        rid = rid or _text(_get(entity, "route_id")) or _text(_get(entity, "owner_route_id"))
        if rid is None:
            continue
        slot = found.setdefault(rid, {"view": None, "record": None, "jobs": [], "sessions": []})
        route_view = _get(work, "_route_view") if work is not None else None
        if isinstance(route_view, Mapping):
            if slot["view"] is None and isinstance(route_view.get("view"), Mapping):
                slot["view"] = route_view["view"]
            if slot["record"] is None and isinstance(route_view.get("record"), Mapping):
                slot["record"] = route_view["record"]
        if _get(entity, "key") is not None or hasattr(entity, "parent_sid"):
            slot["jobs"].append(entity)
        else:
            slot["sessions"].append(entity)
    return found


def _stage_state(node):
    return _NODE_STAGE.get(node.get("state"), "todo")


def _stage_record(node):
    harness = _text(node.get("harness"))
    stage = {
        "name": _text(node.get("id")) or "·", "st": _stage_state(node),
        "el": _dur(node.get("elapsed_min")), "h": harness,
        "m": _model_pair(node.get("model")), "e": _effort(node.get("effort")),
        "gate": bool(node.get("gate_passed")),
    }
    rounds = _num(node.get("route_attempt_round")) or _num(node.get("attempt_round"))
    if rounds:
        stage["r"] = int(rounds)
    if stage["st"] == "active" and node.get("unit") == "_kernel/resource":
        stage["host"] = None
    return stage


def _worker_segments(node, resources):
    """``[label, harness segments, state segments]`` of the route's worker, or None."""
    if node is None:
        return None
    harness = _text(node.get("harness"))
    unit = _text(node.get("unit")) or ""
    elapsed = _dur(node.get("elapsed_min"))
    running = node.get("state") == "active"
    if unit == "_kernel/resource" and resources:
        run = resources[0]
        name = _text(_get(run, "display_title")) or _text(_get(run, "route_node", "node", "run_id"))
        host = [[name, "now_sub"]] if name else [["resource", _SEG_DIM]]
        state = [(f"running {elapsed}" if running and elapsed else "resource", _SEG_DIM)]
        return ["resource", host, [list(s) for s in state]]
    if harness is None:
        return None
    segs = [[harness, "hb_" + harness]]
    pair = _model_pair(node.get("model"))
    effort = _effort(node.get("effort"))
    if pair:
        segs += [[" (", _SEG_DIM], [pair[0], pair[1]]]
        if effort:
            segs += [["·", _SEG_DIM], [effort, "eff_" + effort if effort in ("medium", "xhigh") else _SEG_DIM]]
        segs.append([")", _SEG_DIM])
    state = []
    if elapsed:
        state.append([(f"running {elapsed}" if running else f"{elapsed} · worker ended"), _SEG_DIM])
    return ["worker", segs, state]


def _owner_job(jobs):
    for job in jobs:
        if (_get(job, "depth") == 1 or _get(job, "dispatch_depth") == 1) and not _get(job, "route_node"):
            return job
    return jobs[0] if jobs else None


def _route_records(snapshot, session_rows, session_index, resolver, now):
    collected = _collect_routes(snapshot)
    resources = _records(_get(snapshot, "resources", default=()))
    tag_by_sid = {}
    for tag, info in session_index.items():
        for known in info["ids"]:               # the live id and the ids it answers to after a resume
            tag_by_sid.setdefault(known, tag)
    by_tag = {row["tag"]: row for row in session_rows}
    routes, nodes = [], {}
    for rid, slot in collected.items():
        view = slot["view"] or {}
        record = slot["record"] or {}
        jobs = slot["jobs"]
        owner = _owner_job(jobs)
        owner_tag = None
        for session in slot["sessions"]:
            tag = _text(_get(session, "session_tag"))
            if tag in by_tag:
                owner_tag = tag
                break
        if owner_tag is None:
            owner_tag = next((tag for tag, info in session_index.items() if rid in info["chain"]), None)
        if owner_tag is None and owner is not None:
            for key in ("parent_sid", "caller_sid"):
                tag = tag_by_sid.get(_text(_get(owner, key)))
                if tag:
                    owner_tag = tag
                    break
        session_row = by_tag.get(owner_tag) if owner_tag else None
        view_nodes = [n for n in (view.get("nodes") or ()) if isinstance(n, Mapping)]
        stages = [_stage_record(node) for node in view_nodes]
        done = sum(1 for node in view_nodes if node.get("state") == "done")
        failed = any(node.get("state") == "failed" for node in view_nodes)
        active = next((n for n in view_nodes if n.get("state") in ("active", "reconciling", "recovering")), None)
        total = len(view_nodes)
        capability = _text(view.get("capability")) or _text(record.get("capability"))
        if capability is None:
            capability = next((_text(_get(job, "capability_owner", "assigned_contract")) for job in jobs
                               if _text(_get(job, "capability_owner", "assigned_contract"))), None)
        short = _short_capability(capability)
        cwd = _text(view.get("cwd")) or _text(record.get("cwd")) or _text(_get(owner, "cwd"))
        repo = (session_row or {}).get("repo") or _repo_of(resolver, cwd)
        owner_state = (session_row or {}).get("state")
        if active is not None:
            state = "working"
        elif total and done == total:
            state = "done"
        elif failed:
            state = "dead"
        elif owner_state == "blocked":
            state = "blocked"
        else:
            state = "idle"
        shown_node = active or next((n for n in view_nodes if n.get("state") != "done"), None) \
            or (view_nodes[-1] if view_nodes else None)
        run_children = [r for r in resources if _text(_get(r, "route_id")) == rid
                        and _get(r, "liveness") == "working"]
        resource_node = shown_node is not None and shown_node.get("unit") == "_kernel/resource"
        elapsed_values = [v for v in (_num(_get(job, "elapsed_min")) for job in jobs) if v is not None]
        age = _dur(max(elapsed_values)) if elapsed_values else None
        topology = _text(view.get("execution_topology")) or _text(record.get("execution_topology"))
        shape = topology
        if topology == "staged" and resource_node:
            shape = "staged+resource"
        leg_status = "failed" if failed and active is None else "done" if state == "done" else "active"
        progress = f"{done}/{total}" if total else None
        worker = _worker_segments(shown_node, run_children)
        owner_model = _model_pair(_get(owner, "model") if owner is not None else None) or \
            (session_row or {}).get("model")
        owner_harness = _text(_get(owner, "harness")) if owner is not None else None
        owner_harness = (session_row or {}).get("harness") or owner_harness
        worktree = None
        if cwd and ("-wt" in cwd or "/.worktrees/" in cwd):
            worktree = os.path.basename(cwd.rstrip("/")) or None
        result = _get(_get(owner, "work_projection"), "result") if owner is not None else None
        if result is None:
            result = _get(record, "result")
        if result is None:
            for session in slot["sessions"]:
                chain = _get(session, "route_chain")
                chain_nodes = _get(chain, "nodes", default=()) if chain is not None else ()
                match = next((node for node in chain_nodes if _get(node, "route_id") == rid), None)
                if match is not None and _get(match, "result") is not None:
                    result = _get(match, "result")
                    break
        title = _text(_get(result, "name")) or _text(view.get("slug")) or _text(record.get("slug")) or \
            (_text(_get(owner, "slug")) if owner is not None else None)
        if (owner is None or not jobs) and (session_row or {}).get("title"):
            title = title or session_row["title"]
        worker_stage = _text(shown_node.get("id")) if shown_node else None
        worker_unit = _text(shown_node.get("unit")) if shown_node else None
        context = None
        if owner is not None:
            tokens = _num(_get(owner, "active_context_tokens"))
            window = _num(_get(owner, "context_window_tokens"))
            if tokens is not None and window:
                context = [int(tokens), int(window)]
        routes.append({
            "id": rid, "repo": repo, "session_tag": owner_tag, "title": title, "state": state,
            "worker": worker, "age": age,
            "legs": [[short or "·", leg_status, progress]] if short else [],
            "stages": stages, "resume": None, "activity": "running" if active is not None else "ended",
            "loops": None, "worktree": worktree, "tree_label": short,
            "harness": owner_harness, "model": owner_model,
            "effort": (session_row or {}).get("effort") or (_effort(_get(owner, "effort")) if owner is not None else None),
            "mode": _text(view.get("capability_mode")) or _text(record.get("capability_mode")),
            "intensity": _text(view.get("effective_intensity")) or _text(record.get("effective_intensity")),
            "profile": _text(_get(owner, "profile")) if owner is not None else None,
            "capability": short, "shape": shape, "past_from": None,
            "worker_stage": worker_stage or "·", "worker_unit": worker_unit,
            "worker_stage_key": None, "worker_step_key": None,
            "owner_context": context, "subworker": None,
        })
        nodes[rid] = _node_groups(view_nodes, short, leg_status, progress)
    return routes, nodes


def _node_groups(view_nodes, short, leg_status, progress):
    """One leg group holding the route's stages: the detail the route page prints per stage."""
    if not view_nodes:
        return []
    detail = []
    for node in view_nodes:
        state = _stage_state(node)
        harness = _text(node.get("harness"))
        model = _model_pair(node.get("model"))
        effort = _effort(node.get("effort"))
        who = None
        if harness:
            who = [harness, (model[0] if model else "") + (("·" + effort) if effort and model else "")]
        rounds = []
        if state != "todo":
            rounds.append(["✓" if state == "done" else "✕" if state == "failed" else "●",
                           _dur(node.get("elapsed_min")) or "", ""])
        detail.append({
            "n": _text(node.get("id")) or "·", "st": state,
            "verdict": "PASS" if node.get("gate_passed") else state if state != "todo" else "pending",
            "unit": _text(node.get("unit")) or "·",
            "who": who, "when": _dur(node.get("elapsed_min")) or "·",
            "rounds": rounds, "gate": _text(node.get("gate")), "out": "·", "says": "",
        })
    label = f"leg 1 · {short}" if short else "leg 1"
    return [{
        "kind": "leg", "n": label, "st": leg_status, "verdict": progress or leg_status,
        "lines": [[[label, "dim"]]], "detail": detail,
        "title": [[label, "head"]], "loops": [],
    }]


# -- repos / questions / peers --------------------------------------------------------------

def _ago(epoch, now):
    """``41m ago`` / ``6h ago`` / ``3d ago`` (the default board's age words); None when unknown."""
    value = _num(epoch)
    if value is None or now is None or not 0 <= value <= now:
        return None
    minutes = int((now - value) // 60)
    if minutes >= 1440:
        return f"{minutes // 1440}d ago"
    if minutes >= 60:
        return f"{minutes // 60}h ago"
    return f"{minutes}m ago"


def _repo_results(snapshot, resolver, now):
    """repo -> its newest recorded result ``{"name", "result", "at"}``, the way the default board's
    project header finds it (work projection result, route chain node results, resource exits)."""
    try:
        from .. import render
        group_last_result = render._group_last_result
    except Exception:
        return {}
    groups = {}
    sessions = _records(_get(snapshot, "sessions", default=()))
    jobs = _records(_get(snapshot, "jobs", default=()))
    resources = _records(_get(snapshot, "resources", default=()))
    repo_of_sid = {}
    for session in sessions:
        repo = _repo_of(resolver, _get(session, "cwd", "project"), _get(session, "repo", "slug"))
        for known in (_get(session, "session_id", "sid", "id"), *(_get(session, "session_aliases") or ())):
            if _text(known):
                repo_of_sid[known] = repo
        groups.setdefault(repo, ([], [], []))[0].append(session)
    for job in jobs:
        repo = repo_of_sid.get(_text(_get(job, "parent_sid"))) or _repo_of(
            resolver, _get(job, "parent_cwd", "cwd"), None)
        groups.setdefault(repo, ([], [], []))[1].append(job)
    for resource in resources:
        repo = _repo_of(resolver, _get(resource, "cwd"), _get(resource, "project"))
        groups.setdefault(repo, ([], [], []))[2].append(resource)
    found = {}
    for repo, (g_sessions, g_jobs, g_resources) in groups.items():
        try:
            result = group_last_result(g_sessions, g_jobs, g_resources)
        except Exception:
            result = None
        if result:
            found[repo] = result
    return found


def _repo_records(session_rows, routes, results=None, now=None):
    order = []
    for row in session_rows:
        if row["repo_id"] not in order:
            order.append(row["repo_id"])
    for route in routes:
        if route["repo"] is not None and route["repo"] not in order:
            order.append(route["repo"])
    order.sort(key=lambda repo: repo == UNKNOWN_REPO)      # the nameless group goes last
    repos = []
    for repo in order:
        result = (results or {}).get(repo)
        if result and result.get("result") != "success":
            result = None
        if result:
            done, age = _text(result.get("name")), _ago(result.get("at"), now)
        else:
            finished = next((r for r in routes if r["repo"] == repo and r["state"] == "done" and r["title"]), None)
            done, age = (finished["title"] if finished else None), None
        repos.append({"id": repo, "name": repo, "done": done, "age": age})
    return repos


def _question_records(session_rows, session_index, now):
    questions = []
    for row in session_rows:
        if row["state"] != "blocked":
            continue
        interaction = session_index[row["tag"]]["interaction"]
        kind = _text(_get(interaction, "kind")) if interaction is not None else None
        waiting = _get(interaction, "waiting_since") if interaction is not None else None
        questions.append({
            "tag": row["tag"], "h": row["harness"], "m": row["model"], "e": row["effort"],
            "kind": kind, "wait": _since(waiting, now), "project": row["repo"],
            "title": row["title"], "pane": row["pane"], "now": row["now"],
        })
    return questions


def _peer_records(session_rows, session_index, now):
    tag_by_sid = {known: tag for tag, info in session_index.items() for known in info["ids"]}
    harness_of = {row["tag"]: row["harness"] for row in session_rows}
    links, linked = [], set()

    def endpoint(sid, harness):
        tag = tag_by_sid.get(sid)
        return [tag, harness_of.get(tag) or harness] if tag else None

    for row in session_rows:
        session = session_index[row["tag"]]["session"]
        me = [row["tag"], row["harness"]]
        received = _get(session, "peer_last_recv")
        if isinstance(received, Mapping):
            other = endpoint(received.get("from_session_id"), received.get("from_harness"))
            if other and _text(received.get("kind")):
                age = _dur(received.get("age_min"))
                links.append([other, me, received["kind"], f"{age} ago" if age else None, ""])
        sent = _get(session, "peer_last_sent")
        if isinstance(sent, Mapping):
            other = endpoint(sent.get("to_session_id"), sent.get("to_harness"))
            if other and _text(sent.get("kind")):
                age = _dur(sent.get("age_min"))
                links.append([me, other, sent["kind"], f"{age} ago" if age else None, ""])
        for target in _get(session, "steward_targets") or ():
            other = endpoint(_get(target, "session_id"), _get(target, "harness"))
            links.append([me, other if other else (_text(_get(target, "name")) or "·"), "watch ⚑",
                          _iso_age(_get(target, "ts"), now), ""])
    unique, seen = [], set()
    for link in links:
        key = repr(link[:3])
        if key in seen:
            continue
        seen.add(key)
        unique.append(link)
        for end in link[:2]:
            if isinstance(end, list) and end:
                linked.add(end[0])
    nodes = []
    by_repo = {}
    for row in session_rows:
        if row["tag"] in linked:
            by_repo.setdefault(row["repo"], []).append([row["tag"]])
    for repo, columns in by_repo.items():
        nodes.append([repo, columns])
    # The collector exposes one newest send and receive per session. Put those
    # observations in temporal order while retaining stable row order for ties.
    def newest(link):
        value = link[3]
        if isinstance(value, str):
            match = re.match(r"(\d+)(m|h|d) ago$", value)
            if match:
                scale = {"m": 1, "h": 60, "d": 1440}[match.group(2)]
                return int(match.group(1)) * scale
        return float("inf")
    unique.sort(key=newest)
    if not unique:
        return [], None                       # no peer record: the section is omitted
    return nodes, {"links": unique, "cross": [], "side_lanes": []}


# -- hosts / compute / usage / memory / system / events ------------------------------------

def _host_blocks(snapshot):
    block = _get(snapshot, "compute_hosts")
    hosts = _get(block, "hosts") if block is not None else None
    return list(hosts) if isinstance(hosts, (list, tuple)) else None


def _gib(mib):
    value = _num(mib)
    return None if value is None else round(value / 1024, 1)


def _host_records(snapshot, session_index):
    hosts = _host_blocks(snapshot)
    if hosts is None:
        return [], []
    owner_tag = {}
    for tag, info in session_index.items():
        if info["sid"]:
            owner_tag[(_get(info["session"], "harness"), info["sid"])] = tag
    labels, compute = [], []
    for host in hosts:
        if not isinstance(host, Mapping):
            continue
        name = _text(host.get("host")) or "·"
        base = host.get("self") is True
        labels.append({"mark": "⌂" if base else "◇", "name": name, "key": "base_host" if base else "host"})
        if host.get("reachable") is False:
            compute.append([name, base, None, None, None, []])
            continue
        cpu_pct, cpu_n = _num(host.get("cpu_utilization_pct")), _num(host.get("cpu_count"))
        cpu = [round(cpu_n * cpu_pct / 100, 1), cpu_n] if cpu_pct is not None and cpu_n else None
        mem_total, mem_used = _gib(host.get("memory_total_mib")), _gib(host.get("memory_used_mib"))
        mem = [mem_used, mem_total] if mem_total and mem_used is not None else None
        gpus = []
        for gpu in host.get("gpus") or ():
            if not isinstance(gpu, Mapping):
                continue
            processes = [p for p in (gpu.get("processes") or ()) if isinstance(p, Mapping)]
            users = []
            for process in processes:
                owner = process.get("session_owner")
                tag = owner_tag.get((_get(owner, "harness"), _get(owner, "session_id"))) if owner else None
                if tag and tag not in users:
                    users.append(tag)
            used, total = _gib(gpu.get("memory_used_mib")), _gib(gpu.get("memory_total_mib"))
            util = _num(gpu.get("utilization_gpu_pct"))
            gpus.append([_num(gpu.get("index")), "working" if processes else "idle",
                         _text(gpu.get("name")), None,
                         None if util is None else int(util), used, total, users])
        compute.append([name, base, cpu, mem, None, gpus])
    return labels, compute


_UNIT = {"5h": "5h", "7d": "week", "wk": "week", "mo": "month"}


def _usage_records(snapshot, session_rows, now):
    best = {}
    for session in _records(_get(snapshot, "sessions", default=())):
        harness = _text(_get(session, "harness"))
        if harness not in HARNESSES:
            continue
        windows = _get(session, "rl_windows")
        r5, r7, rms = _get(session, "rl_5h"), _get(session, "rl_7d"), _get(session, "rl_ms")
        if windows or r5 is not None or r7 is not None or rms:
            mtime = _num(_get(session, "mtime")) or 0
            if harness not in best or mtime > best[harness][0]:
                resets = _get(session, "rl_rs") or (None, None)
                best[harness] = (mtime, r5, r7, rms, resets, windows)
    for harness, snap in (_get(snapshot, "usage_snapshots", default={}) or {}).items():
        payload = snap.get("payload") if isinstance(snap, Mapping) else None
        if isinstance(payload, Mapping) and harness in HARNESSES and (
                payload.get("rl_windows") or payload.get("windows") or payload.get("rl_5h") is not None
                or payload.get("rl_7d") is not None or payload.get("rl_ms")):
            best[harness] = (_num(snap.get("observed_at")) or 0, payload.get("rl_5h"), payload.get("rl_7d"),
                             payload.get("rl_ms"), (payload.get("rs_5h"), payload.get("rs_7d")),
                             payload.get("rl_windows") or payload.get("windows"))
    usage = []
    for harness in HARNESSES:
        if harness not in best:
            continue
        _mt, r5, r7, rms, resets, windows = best[harness]
        raw = list(windows) if windows else [("5h", r5, resets[0]), ("week", r7, resets[1])]
        raw += [(label, value, None) for label, value in (rms or ())]
        shown = []
        for item in raw:
            label, pct, reset = (tuple(item) + (None, None))[:3]
            if _num(pct) is None:
                continue
            reset_text = None
            if _num(reset) is not None and now is not None:
                reset_text = _dur(max(0, (reset - now) / 60))
            shown.append([_UNIT.get(str(label), str(label)), int(pct), reset_text])
        if shown:
            usage.append({"text": harness, "key": "hb_" + harness, "windows": shown})
    return usage


def _parse_clock(ts):
    if not isinstance(ts, str):
        return None
    try:
        stamp = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp.strftime("%H:%M")


def _memory_records(snapshot, accepted_detail):
    detail = _get(accepted_detail, "memory")
    if detail is not None and not isinstance(detail, Mapping):
        return detail
    memory = _get(snapshot, "memory")
    if not isinstance(memory, Mapping):
        return None
    by_repo = memory.get("by_repo")
    if not isinstance(by_repo, Mapping):
        return None
    records = []
    for repo, events in by_repo.items():
        rows = []
        for event in events or ():
            if not isinstance(event, Mapping):
                continue
            action = "saved" if event.get("action") in ("add", "note") else "changed"
            rows.append([_parse_clock(event.get("ts")), action, _text(event.get("type")),
                         _text(event.get("snippet"))])
        if rows:
            records.append([_shown_repo(repo), rows])
    return records


def _system_record(snapshot, session_rows, compute, accepted_detail, usage, now=None):
    system = {}
    governor = _get(snapshot, "governor")
    jobs = _records(_get(snapshot, "jobs", default=()))
    resources = _records(_get(snapshot, "resources", default=()))
    dispatch = {"jobs": len(jobs),
                "resource_running": sum(1 for r in resources if _get(r, "liveness") == "working"),
                "resource_exited": sum(1 for r in resources if _get(r, "liveness") == "exited")}
    if isinstance(governor, Mapping):
        active, cap = _num(governor.get("active")), _num(governor.get("cap"))
        if active is not None:
            dispatch["governor_active"] = int(active)
        if cap is not None:
            dispatch["governor_cap"] = int(cap)
    system["dispatch"] = dispatch
    system["dispatch_count"] = sum(1 for job in jobs if _get(job, "depth") == 1)
    delivery = _get(snapshot, "pending_delivery")
    if isinstance(delivery, Mapping):
        pending = _num(delivery.get("pending", delivery.get("total")))
        if pending is not None:
            system["peer_delivery"] = {"pending": int(pending)}
    memory = _get(snapshot, "memory")
    if isinstance(memory, Mapping) and isinstance(memory.get("today"), Mapping):
        today = memory["today"]
        summary = {"added_today": _num(today.get("added")), "expired": _num(today.get("expired")),
                   "latest": len(memory.get("recent") or ())}
        if isinstance(memory.get("by_repo"), Mapping):
            summary["projects"] = len(memory["by_repo"])
        system["memory_summary"] = {k: v for k, v in summary.items() if v is not None}
    install = _install_record(snapshot)
    if install is not None:
        system["install"] = install
    if usage:
        observed = [_num(snap.get("observed_at")) for snap in (_get(snapshot, "usage_snapshots", default={}) or {}).values()
                    if isinstance(snap, Mapping)]
        stamp = _clock(max((v for v in observed if v is not None), default=now))
        if stamp:
            system["usage_api"] = {"state": "on", "freshness": stamp}
    return system


def _epoch_of(value):
    """Epoch seconds of an ISO text or number, else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            stamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return stamp.timestamp()
    return None


def _memory_events(snapshot, tag_of_sid, tag_of_cwd, limit=24):
    """The journal's newest events as feed records ``[time, tag, source, kind, key, text]``."""
    memory = _get(snapshot, "memory")
    recent = memory.get("recent") if isinstance(memory, Mapping) else None
    events = []
    for event in recent or ():
        if not isinstance(event, Mapping):
            continue
        epoch = _epoch_of(event.get("ts"))
        text = _text(event.get("snippet"))
        if epoch is None or text is None:
            continue
        action = "saved" if event.get("action") in ("add", "note", "tidy-add") else "changed"
        tag = tag_of_sid.get(_text(event.get("sid"))) or tag_of_cwd.get(_text(event.get("cwd"))) or "·"
        events.append((epoch, [_clock(epoch), tag, "hearting", f"mem {action}", "dim", text]))
    events.sort(key=lambda item: -item[0])
    return events[:limit]


def _registry_events(focus, tag):
    """The focused session's newest registry lines as feed records."""
    events = []
    for event in _records(_get(focus, "events", default=())):
        if isinstance(event, Mapping) and event.get("kind") == "registry":
            epoch = _epoch_of(event.get("time_kst"))
            if epoch is None:
                continue
            what = " ".join(part for part in (event.get("worker_type"), event.get("route_node"),
                                              event.get("note")) if part)
            events.append((epoch, [_clock(epoch), event.get("tag") or tag or "·", "registry",
                                   str(event.get("status") or "·"), "dim", what or str(event.get("harness") or "·")]))
        elif isinstance(event, Mapping):
            events.append((0.0, [event.get("time"), event.get("tag"), event.get("source"),
                                 event.get("kind"), event.get("key"), event.get("text")]))
        else:
            events.append((0.0, list(event)))
    return events


def _answered_records(snapshot):
    """Decision records the user answered, per repo, newest first: ``[repo, [[HH:MM, text], ...]]``."""
    memory = _get(snapshot, "memory")
    by_repo = memory.get("by_repo") if isinstance(memory, Mapping) else None
    if not isinstance(by_repo, Mapping):
        return []
    answered = []
    for repo, events in by_repo.items():
        rows = []
        for event in events or ():
            if isinstance(event, Mapping) and event.get("type") == "decision" and _text(event.get("snippet")):
                rows.append((_epoch_of(event.get("ts")) or 0.0,
                             [_parse_clock(event.get("ts")), event["snippet"]]))
        rows.sort(key=lambda item: -item[0])
        if rows:
            answered.append([_shown_repo(repo), [row for _epoch, row in rows]])
    return answered


# -- detail (the selection pump's reads) ----------------------------------------------------------

def _detail_parts(detail):
    """``(focus result, seats by harness:sid, config, panes)`` of whatever the feed handed over."""
    if detail is None:
        return None, {}, None, {}
    if hasattr(detail, "seats") and hasattr(detail, "focus"):
        return detail.focus, dict(detail.seats or {}), detail.config, dict(getattr(detail, "panes", {}) or {})
    return detail, {}, None, {}


def _stamp(epoch, now):
    value = _num(epoch)
    if value is None:
        return None
    try:
        when = datetime.datetime.fromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return None
    return when.strftime("%m-%d %H:%M")


def _clear_text(entries, now):
    """When the seat was last cleared: ``HH:MM`` today (``· Nm ago`` within the hour), else ``MM-DD HH:MM``."""
    cleared = [entry for entry in entries if entry.get("source") == "clear" and _num(entry.get("started_at"))]
    if not cleared:
        return None
    epoch = max(float(entry["started_at"]) for entry in cleared)
    try:
        when = datetime.datetime.fromtimestamp(epoch)
        today = datetime.datetime.fromtimestamp(now).date()
    except (OverflowError, OSError, ValueError):
        return None
    if when.date() != today:
        return when.strftime("%m-%d %H:%M")
    minutes = int(max(0, now - epoch) // 60)
    return when.strftime("%H:%M") + (f" · {minutes}m ago" if minutes < 60 else "")


def _seat_view(tag, repo, harness, sid, entries, now):
    """``(seat record, generation records, cards or None)`` of one session's seat history."""
    from .nav import generation_chain
    chain = generation_chain(entries, harness, sid)
    full = any("lines" in card for entry in entries for card in (entry.get("cards") or ()))
    generations, marks, card_rows = [], [], []
    latest_title = ""
    for item in chain:
        card = item["card"] or {}
        title = _text(card.get("title")) or ""
        if title:
            latest_title = title
        generations.append({
            "harness": _text(item["harness"]), "sid": str(item["sid"] or "")[:8],
            "at": "now" if item["now"] else (_stamp(card.get("at"), now) or ""),
            "kind": item["kind"], "connection": item["connection"], "note": None,
            "card": bool(item["card"] and item["card"].get("status") == "available"),
            "detail": full, "title": title})
        marks.append([_text(item["harness"]) or "·", item["kind"]])
        if full and item["card"] and "lines" in card:
            card_rows.append({"gen": len(generations), "harness": _text(item["harness"]),
                              "sid": str(item["sid"] or "")[:8], "at": _stamp(card.get("at"), now) or "",
                              "chars": int(card.get("chars") or 0), "lines": list(card["lines"])})
    has_card = any(g["card"] for g in generations)
    seat = [tag, repo, len(chain), _clear_text(entries, now) or "·", has_card, latest_title, marks]
    cards = {"seat": tag, "cards": card_rows, "prior_cards": []} if full else None
    return seat, generations, cards


def _detail_sections(rows, session_index, accepted_detail, now):
    """Seat, generation, card, dialogue, pane and raw-output sections from the pump's results."""
    focus, seats, _config, _panes = _detail_parts(accepted_detail)
    by_ref = {}
    for row in rows:
        info = session_index[row["tag"]]
        by_ref[f"{row['harness']}:{info['sid']}"] = row
    seat_rows, generations, cards, dialogue = [], {}, {}, {}
    sources = _get(focus, "source_records") if focus is not None else None
    sources = sources if isinstance(sources, Mapping) else {}
    focus_key = _get(focus, "key")
    focus_ref = f"{_get(focus_key, 'harness')}:{_get(focus_key, 'sid')}" if focus_key is not None else None
    merged = dict(seats)
    if focus_ref and sources.get("seat") is not None:
        merged[focus_ref] = sources["seat"]
    for row in rows:                       # list order is the view's order
        ref = f"{row['harness']}:{session_index[row['tag']]['sid']}"
        entries = merged.get(ref)
        if entries is None:
            continue
        seat, gens, card_block = _seat_view(row["tag"], row["repo"], row["harness"],
                                            session_index[row["tag"]]["sid"], entries, now)
        seat_rows.append(seat)
        generations[row["tag"]] = gens
        if card_block is not None:
            cards[row["tag"]] = card_block
    pane_read, raw, spoken = None, None, None
    if focus_ref in by_ref:
        tag = by_ref[focus_ref]["tag"]
        if isinstance(sources.get("pane_read"), Mapping):
            pane_read = (tag, sources["pane_read"])
        if isinstance(sources.get("dialogue"), Mapping):
            spoken = (tag, sources["dialogue"])
        if isinstance(sources.get("raw"), Mapping):
            raw = sources["raw"]
    if spoken:
        tag, log = spoken
        dialogue = {"logs": {f"{tag}:{int(log['index'])}": {
            "h": _text(log.get("harness")), "sid": str(log.get("sid") or "")[:8],
            "lines": list(log.get("lines") or ())}}, "generation_text": {}}
    return seat_rows, generations, cards, dialogue, pane_read, raw


def session_index(snapshot, now=None):
    """``{tag: {"harness", "sid", "ids", "session", "repo"}}`` for the main sessions, with the tags the
    explorer draws; the feed uses it to turn what the person opened back into the session to read."""
    clock = time.time() if now is None else now
    resolver = _resolver(snapshot)
    rows, index = _session_records(snapshot, clock, resolver)
    by_tag = {row["tag"]: row for row in rows}
    return {tag: {"harness": by_tag[tag]["harness"], "sid": info["sid"], "ids": info["ids"],
                  "session": info["session"], "repo": by_tag[tag]["repo"], "title": by_tag[tag]["title"]}
            for tag, info in index.items()}


# -- entry --------------------------------------------------------------------------------

def _install_record(snapshot):
    install = _get(snapshot, "hearting")
    if not isinstance(install, Mapping) or not _text(install.get("version")):
        return None
    version = str(install["version"])
    try:
        from .. import render
        segments = render._hearting_version_segments(version)
        version = "".join(text for text, _key in segments)
    except Exception:
        pass
    record = {"version": version if version[:1] == "v" or not version[:1].isdigit() else "v" + version}
    for key in ("managed", "pinned", "distribution"):
        if key in install:
            record[key] = install[key]
    return record


def build_view_model(snapshot, accepted_detail=None, cached_config=None, now=None):
    """Project facts only; identity and liveness decisions stay with their source builders."""
    fixture_values = _get(snapshot, "values")
    if isinstance(fixture_values, Mapping):
        return ExplorerViewModel.from_mapping(fixture_values)
    if isinstance(snapshot, Mapping) and "schema" in snapshot and "sessions" in snapshot:
        return ExplorerViewModel.from_mapping(snapshot)
    clock = time.time() if now is None else now
    resolver = _resolver(snapshot)
    session_rows, session_index = _session_records(snapshot, clock, resolver)
    order = _get(snapshot, "session_order")
    if order:
        position = {str(sid): i for i, sid in enumerate(order)}
        sid_of = {row["tag"]: session_index[row["tag"]]["sid"] for row in session_rows}
        session_rows.sort(key=lambda row: position.get(str(sid_of.get(row["tag"])), len(position)))
    routes, nodes = _route_records(snapshot, session_rows, session_index, resolver, clock)
    repos = _repo_records(session_rows, routes, _repo_results(snapshot, resolver, clock), clock)
    questions = _question_records(session_rows, session_index, clock)
    peer_nodes, peer_edges = _peer_records(session_rows, session_index, clock)
    hosts, compute = _host_records(snapshot, session_index)
    usage = _usage_records(snapshot, session_rows, clock)
    by_repo = {}
    for row in session_rows:
        by_repo.setdefault(row["repo_id"], []).append(row)
    session_rows = [row for repo in repos for row in by_repo.get(repo["id"], ())]
    focus, _seats, detail_config, panes = _detail_parts(accepted_detail)
    for row in session_rows:                          # a pane id the herdr read found for this session
        if not row.get("pane"):
            info = session_index[row["tag"]]
            row["pane"] = next((panes[i] for i in info["ids"] if i in panes), None)
    seat_rows, generations, cards, dialogue, pane_read, raw = _detail_sections(
        session_rows, session_index, accepted_detail, clock)
    by_tag = {row["tag"]: row for row in session_rows}
    for seat in seat_rows:
        row = by_tag[seat[0]]
        row["generation"] = seat[2]
        row["last_clear"] = None if seat[3] == "·" else seat[3].split(" · ")[0]
    if pane_read:
        tag, read = pane_read
        by_tag[tag]["pane"] = by_tag[tag]["pane"] or _text(read.get("pane"))
        by_tag[tag]["pane_meta"] = "visible screen" + (f" · {_clock(read['at'])}" if _clock(read.get("at")) else "")
        by_tag[tag]["pane_lines"] = [[str(line), "soft"] for line in read.get("lines") or ()]
    for question in questions:
        if not question.get("pane"):
            question["pane"] = by_tag.get(question["tag"], {}).get("pane")
    tag_of_sid = {info["sid"]: tag for tag, info in session_index.items() if info["sid"]}
    tag_of_cwd = {}
    for tag, info in session_index.items():
        cwd = _text(_get(info["session"], "cwd"))
        if cwd:
            tag_of_cwd[cwd] = None if cwd in tag_of_cwd else tag
    tag_of_cwd = {cwd: tag for cwd, tag in tag_of_cwd.items() if tag}
    focus_tag = None
    focus_key = _get(focus, "key")
    if focus_key is not None:
        focus_tag = tag_of_sid.get(_get(focus_key, "sid"))
    feed = _memory_events(snapshot, tag_of_sid, tag_of_cwd) + _registry_events(focus, focus_tag)
    feed.sort(key=lambda item: -item[0])
    explicit = _get(focus, "source_records")
    answered = _answered_records(snapshot)
    system = _system_record(snapshot, session_rows, compute, accepted_detail, usage, clock)
    if answered:
        system["answered_total"] = sum(len(rows) for _repo, rows in answered)
    data = {
        "identity": _get(snapshot, "generation", "identity"),
        "clock": clock,
        "hosts": hosts, "usage": usage, "census": None,
        "repos": repos, "sessions": session_rows, "routes": routes, "nodes": nodes,
        "attempts": raw,
        "questions": questions, "answered": answered,
        "seats": seat_rows, "generations": generations, "cards": cards or None,
        "dialogue": dialogue or None,
        "peer_nodes": peer_nodes, "peer_edges": peer_edges,
        "events": [event for _epoch, event in feed],
        "compute": compute,
        "memory": _memory_records(snapshot, accepted_detail),
        "models": detail_config if detail_config is not None else _get(cached_config, "models", "model_config"),
        "config": cached_config,
        "system": system,
        "pane_graph": _records(_get(snapshot, "pane_graph", "pane_edges", default=())),
        "source_records": explicit if isinstance(explicit, Mapping) else None,
    }
    return ExplorerViewModel.from_mapping(data)


def from_mapping(value):
    return ExplorerViewModel.from_mapping(value)
