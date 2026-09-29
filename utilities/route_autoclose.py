"""Runtime closure of routes nobody works on any more.

A compiled route is a lease the runtime holds for the session that composed
it, not homework that session must remember.  `compose` and `campaign-status`
run one bounded background sweep that closes an open route only when time says
nobody works on it:

* session-ended -- this host holds positive evidence that the composing Claude
  session is gone (its own session record, whose process is dead) and nothing
  wrote to the route or its cycle for `QUIET_SECONDS`;
* idle -- otherwise (no such evidence, or Codex/OpenCode, which expose none),
  nothing wrote to the route, its cycle, its composer's route-chain ledger or
  a bound resource run's log for `IDLE_SECONDS`.  An activity scan that hits
  its size limit counts as activity.

`campaign-close` is an explicit end: besides that sweep it closes the
campaign's own open member routes that are the closing session's, or quiet
for `QUIET_SECONDS` with no provably live session.

Never closed, whatever the time or trigger: a route of the session running
the sweep (outside `campaign-close`), a route a dispatch attempt still holds
(live, unverifiable or lease-held), one whose owner's runtime settlement the
runtime itself still classifies as pending, one whose workflow ledger waits on
a human gate, one a resource run without an end record is bound to or logs
into (on any host), one with an unfinished `finish`, and one whose cycle
directory a live process holds open.  When any of that evidence cannot be
read, the sweep closes nothing.

The closure claims no proof.  Every route closes the same way,
`close_route(allow_unproven=True)` with an `autoclose` record, so its terminal
proof is exactly what completion markers already show.  Its open cycle is then
sealed with the existing states: completed when that proof holds, abandoned
otherwise.  Both steps are idempotent, so the next sweep finishes whatever an
interrupted one left: a cycle behind a runtime-written closure is sealed at
once, any other closed-but-unsealed cycle after `QUIET_SECONDS`.  A session
that comes back to an automatically closed route is not refused: `finish`
reports it closed and succeeds, `start` hands back a compose command.

The sweep writes under the artifact root only and reads the checkout with
`git rev-parse` alone.  It takes no lock it would wait for and spends at most
`BUDGET_SECONDS`; whatever it did not reach waits for the next one.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

IDLE_SECONDS = 7 * 24 * 3600
QUIET_SECONDS = 3600
BUDGET_SECONDS = 20.0
WALK_LIMIT = 2000
# A terminal registry row may still have its wrapper process publishing; rows
# older than this are not re-probed on every sweep.
_RECENT_TERMINAL_SECONDS = 3600
_TERMINAL = {"done", "killed", "cancelled"}
_RESOURCE_OPEN = {"open", "running", "pending", "working"}   # artifact-quiescence RESOURCE_OPEN


class Unreadable(Exception):
    """Evidence a protection needs could not be read: the sweep closes nothing."""


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _newest_mtime(directory: Path, limit: int = WALK_LIMIT) -> float | None:
    """Newest mtime in a tree, or None when the tree is larger than `limit`."""
    newest, seen = _mtime(directory), 0
    for base, dirs, files in os.walk(directory):
        for name in files + dirs:
            newest = max(newest, _mtime(Path(base) / name))
            seen += 1
            if seen > limit:
                return None
    return newest


def _under(path: str, home: Path | None) -> bool:
    return home is not None and (path == str(home) or path.startswith(str(home) + os.sep))


def _open_paths(root: Path) -> set[str]:
    """Paths under `root` a live process holds open or runs in.  Non-dumpable
    daemons of this user (sd-pam, key agents) refuse inspection to everyone and
    are skipped; a scan that can read none of this user's other processes is
    blind, and a blind scan is unreadable evidence."""
    prefix = str(root) + os.sep
    found: set[str] = set()
    try:
        pids = [pid for pid in os.listdir("/proc") if pid.isdigit() and int(pid) != os.getpid()]
    except OSError as exc:
        raise Unreadable("process-table") from exc
    read = denied = 0
    for pid in pids:
        try:
            mine = os.stat(f"/proc/{pid}").st_uid == os.getuid()
            links = [os.readlink(f"/proc/{pid}/cwd")]
            links += [os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd")]
        except PermissionError:
            denied += mine
            continue
        except OSError:
            continue   # the process or one descriptor went away meanwhile
        read += mine
        found.update(link for link in links if link.startswith(prefix))
    if denied and not read:
        raise Unreadable("process-descriptors")
    return found


def _composers(api) -> tuple[dict, dict]:
    """route_id -> latest route-chain ledger holder, and (harness, sid) -> ledger mtime."""
    holders: dict[str, dict] = {}
    activity: dict[tuple[str, str], float] = {}
    try:
        rc = api._route_chain_module()
    except Exception:
        rc = None
    if rc is None:
        return holders, activity
    for harness in rc.HARNESSES:
        directory = Path(rc.state_root()) / harness
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            sid = name[:-len(".jsonl")]
            activity[(harness, sid)] = _mtime(directory / name)
            for line in rc.read_tail(harness, sid, max_bytes=1024 * 1024):
                ts = line.get("ts") or 0
                previous = holders.get(line["route_id"])
                if previous is None or ts >= previous["ts"]:
                    holders[line["route_id"]] = {"harness": harness, "session_id": sid, "ts": ts}
    return holders, activity


def _current_identity():
    """The interactive depth-0 session running this command, else None."""
    if os.environ.get("AGENT_DISPATCH_ATTEMPT_ID") or os.environ.get("AGENT_DISPATCH_DEPTH", "0") not in ("", "0"):
        return None
    try:
        from dispatch_parent_completion import interactive_parent_identity
        harness, sid = interactive_parent_identity(os.environ)
    except Exception:
        return None
    return (harness, sid) if harness and sid else None


def _registries(api) -> list[Path]:
    import dispatch_contract as dispatch
    paths = []
    try:
        paths.append(Path(dispatch.resolve_global_registry(api.ROOT, None, 0, "read").path))
    except Exception:
        pass
    if os.environ.get("AGENT_DISPATCH_JOBS"):
        paths.append(Path(os.environ["AGENT_DISPATCH_JOBS"]))
    return list(dict.fromkeys(paths))


def _attempts(registries: list[Path]) -> tuple[set[str], dict]:
    """(route ids a registered attempt still holds -- live, unverifiable or
    lease-held --, route id -> its settled owner rows for the runtime's own
    settlement check)."""
    if not any(path.is_file() for path in registries):
        return set(), {}
    import artifact_cutover
    import codex_dispatch_terminal as terminal
    import dispatch_contract as dispatch
    held: set[str] = set()
    owners: dict[str, list] = {}
    now = time.time()
    for attempt_id, row in artifact_cutover._registry_attempts(registries).items():
        metadata, status = row["metadata"], row["status"]
        ids = {metadata.get(key, "") for key in ("route_id", "owner_route_id", "batch_route_id")} - {""}
        if not ids:
            continue
        if status == "done" and metadata.get("workflow_completion") == "runtime-v1":
            for route_id in ids:
                owners.setdefault(route_id, []).append(row)
        if status in _TERMINAL:
            try:
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                stamp = now
            if now - stamp >= _RECENT_TERMINAL_SECONDS:
                continue
        try:
            observed = dispatch.observed_attempt_liveness(
                status, metadata, terminal_envelope=terminal.terminal_envelope_observed(metadata.get("log_file")))
            state = observed.state
            lease = bool(metadata.get("supervisor_lease_file")) and dispatch.supervisor_lease_is_held(row["jobs"], metadata)
        except Exception:
            state, lease = "unverifiable", False
        if state in {"alive", "unverifiable"} or lease:
            try:
                current, _conflict = artifact_cutover._current_owner_route_id(attempt_id, metadata, row["jobs"])
            except Exception:
                current = None
            held |= ids | ({current} if current else set())
    return held, owners


def _settlement_pending(route_id: str, owners: dict) -> bool:
    """The runtime's own classifier: a PASS owner whose settlement is pending
    or unknown still owns the route's closure."""
    from dispatch_terminal_commit import owner_completion_pending
    for row in owners.get(route_id, ()):
        try:
            if owner_completion_pending(Path(row["jobs"]), row["status"], row["metadata"]):
                return True
        except Exception:
            return True
    return False


def _gate_ledger_roots(registries: list[Path]) -> list[Path]:
    import workflow_state
    try:
        roots = [workflow_state.ledger_root_for(jobs=jobs)[0] for jobs in registries]
        roots.append(workflow_state.ledger_root_for()[0])
    except Exception as exc:
        raise Unreadable("workflow-ledger-root") from exc
    return list(dict.fromkeys(roots))


def _waits_on_human(route_id: str, roots: list[Path]) -> bool:
    """A gate this route's workflow raised and nobody has resolved yet."""
    import workflow_state
    for root in roots:
        try:
            entries = workflow_state.WorkflowLedger(route_id, root=root).journal()
        except Exception:
            return True   # unreadable: treat as waiting
        gates = {(entry.get("evidence") or {}).get("gate") for entry in entries
                 if isinstance(entry, dict) and entry.get("workflow_state") == "BLOCKED_HUMAN_GATE"
                 and isinstance(entry.get("evidence"), dict)} - {None}
        if any(workflow_state.human_gate_resolution(entries, gate)["status"] == "blocked" for gate in gates):
            return True
    return False


def _resource_runs() -> tuple[set[str], list[str], dict]:
    """(route ids a resource run without an end record is bound to, the paths
    such runs log into, route id -> newest bound log write).  A run counts as
    live until its registry row or exit sentinel records an end: a run started
    on another host has no process here to ask."""
    try:
        import resource_run_registry
        rows, diagnostics = resource_run_registry.scan()
    except Exception as exc:
        raise Unreadable("resource-run-index") from exc
    if any(str(row.get("kind", "")).startswith("malformed") for row in diagnostics):
        raise Unreadable("resource-run-index")
    routes, paths, activity = set(), [], {}
    for row in rows:
        bound = set()
        for value in (row.get("route_id"), row.get("route_file"), row.get("route")):
            if isinstance(value, str) and value:
                bound.add(Path(value).stem if value.endswith(".json") else value)
        log = row.get("log_path") if isinstance(row.get("log_path"), str) else ""
        written = _mtime(Path(log)) if log else 0.0
        for route_id in bound:
            activity[route_id] = max(activity.get(route_id, 0.0), written)
        status = str(row.get("registry_status") or "").lower()
        sentinel = row.get("sentinel")
        ended = (row.get("ended_at") is not None or row.get("exit_code") is not None
                 or (status and status not in _RESOURCE_OPEN)
                 or (isinstance(sentinel, str) and sentinel and os.path.exists(sentinel)))
        if ended:
            continue
        routes |= bound
        if log:
            paths.append(os.path.realpath(log))
    return routes, paths, activity


def _claude_state(sid: str) -> str:
    """alive | gone | unknown.  `gone` only on positive evidence on this host:
    the session's own registry record, whose process is dead."""
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        from fleet import session_handle, session_registry
        from dispatch_contract import process_identity_disposition
        directory = session_handle._claude_sessions_dir()
        names = os.listdir(directory) if directory else []
    except Exception:
        return "unknown"
    dead = unresolved = False
    for name in names:
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        pid = int(name[:-5])
        record = session_registry.read("claude", pid)
        if not record:
            continue
        verdict = process_identity_disposition(pid, str(record.get("procStart") or ""))
        if record.get("sessionId") == sid:
            if verdict == "live":
                return "alive"
            dead |= verdict == "dead"
            unresolved |= verdict != "dead"
        elif verdict == "live":
            # A resumed session runs under a new id with the old one on argv.
            try:
                if sid in Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace"):
                    return "alive"
            except OSError:
                pass
    return "gone" if dead and not unresolved else "unknown"


def _session_state(holder: Mapping[str, Any] | None) -> str:
    if holder and holder["harness"] == "claude":
        return _claude_state(holder["session_id"])
    return "unknown"


def _open_route_files(root: Path, api) -> list[Path]:
    """Canonical route records with no closure beside them (names only)."""
    directory = api.canonical_routes_dir(root)
    try:
        names = set(os.listdir(directory))
    except OSError:
        return []
    return [directory / name for name in sorted(names)
            if api.ROUTE_RECORD_BASENAME.fullmatch(name) and name[:-5] + ".outcome.json" not in names]


def _read_route(path: Path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("route_id") == path.stem else None


def _open_cycles_by_route(root: Path) -> dict:
    """route_id -> its open cycle record, read once per sweep."""
    import artifact_producer
    table = {}
    try:
        records = artifact_producer.list_cycle_records(root)
    except Exception:
        return table
    for record in records:
        if record.get("state") != "open":
            continue
        table.setdefault(record.get("route_id"), record)
        for binding in record.get("route_bindings") or []:
            route_id = binding.get("route_id") if isinstance(binding, dict) else binding
            if isinstance(route_id, str):
                table.setdefault(route_id, record)
    return table


def _cycle_home(root: Path, record) -> Path | None:
    import artifact_producer
    if not record:
        return None
    try:
        return artifact_producer.cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    except Exception:
        return None


def _last_activity(root: Path, path: Path, raw: Mapping[str, Any], holder, api, record, now: float, *,
                   deep: bool = False, resource_activity: Mapping[str, float] | None = None) -> float:
    """Newest write to the route, its ledger line and its cycle.  The cheap form
    stats the top of the cycle; `deep` walks it and the completion markers, and
    a tree past `WALK_LIMIT` counts as written now."""
    newest = max(_mtime(path), float(holder.get("ts") or 0) if holder else 0.0,
                 (resource_activity or {}).get(str(raw.get("route_id")), 0.0))
    homes = [root / ".runtime" / "inline-finish" / "v1" / str(raw.get("route_id"))]
    if deep:
        try:
            homes.append(Path(api.completion_dir(raw["route_id"])))
        except Exception:
            pass
    if record:
        newest = max(newest, _mtime(root / ".runtime/artifact-producer/v1/cycles" / (record["cycle_id"] + ".json")))
        home = _cycle_home(root, record)
        if home is not None:
            homes.append(home)
            newest = max(newest, _mtime(home / "artifacts"))
    for home in homes:
        seen = _newest_mtime(home) if deep else _mtime(home)
        if seen is None:
            return now
        newest = max(newest, seen)
    return newest


def _kept(route_id: str, raw, *, root, runtime, facts, home) -> str | None:
    """Why this route must stay open whatever the time, else None."""
    import inline_finish
    if route_id in facts["held"]:
        return "owner-live"
    if _settlement_pending(route_id, facts["owners"]):
        return "owner-settlement-pending"
    try:
        pending = inline_finish.pending_state(root, route_id)
    except Exception:
        return "finish-pending"
    if pending and pending.get("state") != "finished":
        return "finish-pending"
    if _waits_on_human(route_id, facts["gate_roots"]):
        return "human-gate"
    if route_id in facts["resource_routes"] or any(_under(item, home) for item in facts["resource_paths"]):
        return "resource-run"
    if any(_under(item, home) for item in facts["open_paths"]):
        return "cycle-in-use"
    return None


def _decide(holder, *, activity, current, idle_since, now, member=False) -> tuple[str | None, str | None]:
    """(reason to close | None, reason kept).  `member`: a member route of the
    campaign `campaign-close` is closing."""
    identity = (holder["harness"], holder["session_id"]) if holder else None
    if member:
        if current and identity == current:
            return "campaign-close", None
        if _session_state(holder) == "alive":
            return None, "session-alive"
        if now - idle_since >= QUIET_SECONDS:
            return "campaign-close", None
        return None, "recent-activity"
    if current and identity == current:
        return None, "current-session"
    state = _session_state(holder)
    if state == "alive":
        return None, "session-alive"
    if state == "gone" and now - idle_since >= QUIET_SECONDS:
        return "session-ended", None
    recent = max(idle_since, activity.get(identity, 0.0) if identity else 0.0)
    if now - recent >= IDLE_SECONDS:
        return "idle", None
    return None, "recent-activity"


def _seal_cycle(root: Path, route: Mapping[str, Any], proven) -> str:
    """Seal the route's open cycle with an existing state; never waits on a lock."""
    import artifact_producer
    try:
        record = artifact_producer.route_cycle_for(root, route)
    except Exception:
        record = None
    if not record:
        return "no-open-cycle"
    try:
        leaf = artifact_producer._finalize_route(root, record)
    except Exception as exc:
        return "cycle-left-open:" + str(exc)[:80]
    if leaf.get("route_id") != route.get("route_id"):
        return "cycle-held-by-continuation"
    if proven is True:
        try:
            artifact_producer.finalize(root, cycle_id=record["cycle_id"], state="completed", lock_timeout=0)
            return "cycle-completed"
        except Exception:
            pass
    try:
        result = artifact_producer.finalize(root, cycle_id=record["cycle_id"], state="abandoned",
                                            abandon_reason="route-unrecoverable", lock_timeout=0)
    except Exception as exc:
        return "cycle-left-open:" + str(exc)[:80]
    return "cycle-abandoned-empty" if result.get("status") == "no-lineage" else "cycle-abandoned"


def _summary_text(raw: Mapping[str, Any]) -> str:
    request = raw.get("work_request") if isinstance(raw.get("work_request"), dict) else {}
    text = str(request.get("text") or "").strip()
    return text or f"{raw.get('capability')} {raw.get('slug') or raw.get('route_id')}"


def close_one(root: Path, path: Path, raw: Mapping[str, Any], reason: str, api, *,
              trigger: str, now: float) -> dict:
    """Close one route nobody works on, claiming no proof.  Idempotent: a rerun
    replays the existing closure and seals whatever is still open."""
    route = api.verify_route(dict(raw), None, allow_stale_registry=True)
    outcome, _created = api.close_route(
        route, path, None, _summary_text(raw), allow_unproven=True,
        autoclose={"reason": reason, "trigger": trigger, "closed_by": "runtime",
                   "proof": "not-claimed", "at": _iso(now)})
    proven = outcome.get("terminal_gate_proven")
    return {"route_id": route["route_id"], "reason": reason,
            "closed": "proven" if proven else "unproven", "cycle": _seal_cycle(root, route, proven)}


def _seal_unsealed_cycles(root: Path, api, cycles: dict, facts, *, deadline, now) -> list[dict]:
    """Open cycles behind an already closed route: the runtime's own closure at
    once (an interrupted sweep), anyone else's after `QUIET_SECONDS`."""
    import artifact_producer
    sealed, seen = [], set()
    runtime = root / ".runtime"
    for record in list(cycles.values()):
        if time.monotonic() >= deadline:
            break
        if record["cycle_id"] in seen:
            continue
        seen.add(record["cycle_id"])
        if not api.outcome_path(api.canonical_route_path(root, record["route_id"])).is_file():
            continue   # the begin route is open: the route pass owns it
        try:
            leaf = artifact_producer._finalize_route(root, record)
            outcome_file = api.outcome_path(api.canonical_route_path(root, leaf["route_id"]))
            outcome = json.loads(outcome_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not outcome.get("autoclose") and now - _mtime(outcome_file) < QUIET_SECONDS:
            continue
        if _kept(leaf["route_id"], leaf, root=root, runtime=runtime, facts=facts,
                 home=_cycle_home(root, record)):
            continue
        sealed.append({"cycle_id": record["cycle_id"], "route_id": leaf["route_id"],
                       "cycle": _seal_cycle(root, leaf, outcome.get("terminal_gate_proven"))})
    return sealed


def sweep(artifact_root, *, api, trigger: str, campaign_id: str | None = None,
          now: float | None = None, budget: float = BUDGET_SECONDS) -> dict:
    """Close what nobody works on under one artifact root.  Never raises.
    `campaign_id` names the campaign `campaign-close` is closing."""
    summary: dict[str, Any] = {"closed": [], "cycles": [], "kept": {}, "errors": [], "deferred": 0}
    # Reading old routes prints lineage/registry advisories meant for their
    # owners; this bookkeeping pass reports one line of its own instead.
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            _sweep(summary, Path(artifact_root).resolve(), api=api, trigger=trigger,
                   campaign_id=campaign_id, now=time.time() if now is None else now, budget=budget)
        except Unreadable as exc:
            summary["kept"]["evidence-unreadable:" + str(exc)] = 1
        except Exception as exc:
            summary["errors"].append({"error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    return summary


def _campaign_members(root: Path, cycles: dict, campaign_id: str | None) -> set[str]:
    """Route ids that seal the named campaign's open cycles."""
    import artifact_producer
    members = set()
    for record in {id(r): r for r in cycles.values()}.values():
        if campaign_id and record.get("campaign_id") == campaign_id:
            try:
                members.add(artifact_producer._finalize_route(root, record)["route_id"])
            except Exception:
                continue
    return members


def _sweep(summary, root: Path, *, api, trigger, campaign_id, now, budget) -> None:
    runtime = root / ".runtime"
    if not (runtime / "routes").is_dir():
        return
    deadline = time.monotonic() + budget
    with (runtime / "route-autoclose.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            summary["busy"] = True
            return
        registries = _registries(api)
        resource_routes, resource_paths, resource_activity = _resource_runs()
        held, owners = _attempts(registries)
        facts = {"held": held, "owners": owners, "gate_roots": _gate_ledger_roots(registries),
                 "resource_routes": resource_routes, "resource_paths": resource_paths,
                 "open_paths": _open_paths(root)}
        holders, activity = _composers(api)
        current = _current_identity()
        cycles = _open_cycles_by_route(root)
        members = _campaign_members(root, cycles, campaign_id)
        files = sorted(_open_route_files(root, api), key=_mtime)
        for index, path in enumerate(files):
            if time.monotonic() >= deadline:
                summary["deferred"] += len(files) - index
                break
            raw = _read_route(path)
            if raw is None:
                continue
            route_id = raw["route_id"]
            holder, record = holders.get(route_id), cycles.get(route_id)
            home = _cycle_home(root, record)
            kept = _kept(route_id, raw, root=root, runtime=runtime, facts=facts, home=home)
            reason = None
            rules = dict(activity=activity, current=current, now=now, member=route_id in members)
            if kept is None:
                reason, kept = _decide(holder, idle_since=_last_activity(
                    root, path, raw, holder, api, record, now, resource_activity=resource_activity), **rules)
            if reason is not None:
                # Confirm against the whole cycle tree before acting.
                reason, kept = _decide(holder, idle_since=_last_activity(
                    root, path, raw, holder, api, record, now, deep=True,
                    resource_activity=resource_activity), **rules)
            if reason is None:
                summary["kept"][kept] = summary["kept"].get(kept, 0) + 1
                continue
            try:
                summary["closed"].append(close_one(root, path, raw, reason, api, trigger=trigger, now=now))
            except Exception as exc:
                summary["errors"].append({"route_id": route_id, "error": str(exc)[:160]})
        summary["cycles"] = _seal_unsealed_cycles(root, api, _open_cycles_by_route(root), facts,
                                                  deadline=deadline, now=now)


def report(summary: Mapping[str, Any], stream=None) -> None:
    """One stderr line; silent when nothing happened."""
    stream = stream or sys.stderr
    closed, cycles, errors = summary.get("closed") or [], summary.get("cycles") or [], summary.get("errors") or []
    if not (closed or cycles or errors or summary.get("deferred")):
        return
    reasons: dict[str, int] = {}
    for row in closed:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    print("route_autoclose closed=%d cycles_sealed=%d deferred=%d errors=%d reasons=%s" % (
        len(closed), len(cycles), summary.get("deferred", 0), len(errors),
        ",".join(f"{k}:{v}" for k, v in sorted(reasons.items())) or "-"), file=stream)
