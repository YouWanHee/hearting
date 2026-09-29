"""Runtime closure of routes nobody works on any more.

A compiled route is a lease the runtime holds for the session that composed it,
not homework that session must remember.  When it is plain that no one works on
a route any more, the runtime closes it and its open producer cycle:

* superseded -- the same interactive session composes a new route; its earlier
  open routes without a live owner are done with.  A non-direct route counts
  only once an owner attempt ran and ended (a route waiting at a human gate has
  no ended owner and is left alone).
* session-ended -- the composing session is provably gone (Claude's own session
  registry; the other runtimes expose no such proof yet) and nothing wrote to
  the route or its cycle for `QUIET_SECONDS`.
* idle -- nothing wrote to the route, its cycle, or its composing session's
  route-chain ledger for `IDLE_SECONDS`.  Used only when the session's liveness
  cannot be decided.
* campaign-close -- closing a campaign closes its members' open routes: the
  closing session's own, and others' once quiet for `QUIET_SECONDS` with no
  provably live session.  Fresh work by someone else still refuses the close.

A route with a live, unverifiable or lease-held dispatch attempt, a pending
terminal commit, a finish another caller claimed, or a cycle directory a live
process still holds open (a training run logging into it) is never touched.

How it closes: a direct route with a cycle-local artifact goes through the same
`finish` transaction the session would have run (the newest artifact as
evidence, the work request as summary, HEAD as commit, scoped tracked edits
recorded rather than refused).  Everything else is closed with its honest
terminal proof (`close_route(allow_unproven=True)`) and its open cycle is sealed
completed when that proof holds, abandoned otherwise.  A cycle whose route was
closed over `QUIET_SECONDS` ago but never sealed is sealed the same way.  Every
closure carries an `autoclose` record naming why.  Only records under the
artifact root are written; the source checkout is never touched.  One sweep
spends at most `BUDGET_SECONDS`; what it did not reach waits for the next one.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

IDLE_SECONDS = 24 * 3600
# An ended session's route still waits this long after its last write.
QUIET_SECONDS = 3600
BUDGET_SECONDS = 20.0
# A terminal registry row may still have its wrapper process publishing; rows
# older than this are not re-probed on every sweep.
_RECENT_TERMINAL_SECONDS = 3600
_TERMINAL = {"done", "killed", "cancelled"}
_OUTPUT_PREFERENCE = (".md", ".txt", ".json", ".html", ".csv", ".yaml", ".yml")


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _newest_mtime(directory: Path, limit: int = 2000) -> float:
    newest, seen = _mtime(directory), 0
    for base, dirs, files in os.walk(directory):
        for name in files + dirs:
            newest = max(newest, _mtime(Path(base) / name))
            seen += 1
            if seen >= limit:
                return newest
    return newest


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


def _open_paths(root: Path) -> set[str]:
    """Paths under `root` a live process holds open or runs in."""
    prefix = str(root) + os.sep
    found: set[str] = set()
    try:
        pids = [pid for pid in os.listdir("/proc") if pid.isdigit()]
    except OSError:
        return found
    for pid in pids:
        links = []
        try:
            links.append(os.readlink(f"/proc/{pid}/cwd"))
            links += [os.readlink(f"/proc/{pid}/fd/{fd}") for fd in os.listdir(f"/proc/{pid}/fd")]
        except OSError:
            pass
        found.update(link for link in links if link.startswith(prefix))
    return found


def _route_chain(api):
    try:
        return api._route_chain_module()
    except Exception:
        return None


def _composers(api) -> tuple[dict, dict]:
    """route_id -> latest ledger holder, and (harness, sid) -> ledger mtime."""
    rc = _route_chain(api)
    holders: dict[str, dict] = {}
    activity: dict[tuple[str, str], float] = {}
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
                    holders[line["route_id"]] = {"harness": harness, "session_id": sid,
                                                 "ts": ts, "event": line.get("event")}
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


def _attempts(api) -> tuple[set, set]:
    """(route ids a dispatch attempt still holds, route ids whose owner ran and ended)."""
    import dispatch_contract as dispatch
    paths = []
    try:
        paths.append(dispatch.resolve_global_registry(api.ROOT, None, 0, "read").path)
    except Exception:
        pass
    if os.environ.get("AGENT_DISPATCH_JOBS"):
        paths.append(Path(os.environ["AGENT_DISPATCH_JOBS"]))
    if not any(Path(path).is_file() for path in paths):
        return set(), set()
    import artifact_cutover
    import codex_dispatch_terminal as terminal
    held: set[str] = set()
    ended: set[str] = set()
    now = time.time()
    for attempt_id, row in artifact_cutover._registry_attempts(paths).items():
        metadata, status = row["metadata"], row["status"]
        ids = {metadata.get(key, "") for key in ("route_id", "owner_route_id", "batch_route_id")} - {""}
        if not ids:
            continue
        is_owner = metadata.get("worker_type") == "owner" or bool(metadata.get("owner_route_id"))
        if status in _TERMINAL:
            try:
                recent = now - datetime.fromisoformat(
                    row["timestamp"].replace("Z", "+00:00")).timestamp() < _RECENT_TERMINAL_SECONDS
            except (TypeError, ValueError):
                recent = True
            if not recent:
                if is_owner:
                    ended |= ids
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
        elif is_owner:
            ended |= ids
    return held, ended


def _claude_state(sid: str) -> str:
    """alive | gone | unknown from Claude Code's own session registry."""
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        from fleet import session_handle, session_registry
        from dispatch_contract import process_identity_disposition
    except Exception:
        return "unknown"
    directory = session_handle._claude_sessions_dir()
    if not directory:
        return "unknown"
    try:
        names = os.listdir(directory)
    except OSError:
        return "unknown"
    unresolved = False
    for name in names:
        if not name.endswith(".json") or not name[:-5].isdigit():
            continue
        pid = int(name[:-5])
        record = session_registry.read("claude", pid)
        if not record:
            continue
        verdict = process_identity_disposition(pid, str(record.get("procStart") or ""))
        if verdict == "dead":
            continue
        if record.get("sessionId") == sid:
            if verdict == "live":
                return "alive"
            unresolved = True
            continue
        if verdict == "live":
            # A resumed session runs under a new id with the old one on argv.
            try:
                if sid in Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace"):
                    return "alive"
            except OSError:
                pass
    return "unknown" if unresolved else "gone"


def _session_state(holder: Mapping[str, Any] | None) -> str:
    if not holder:
        return "unknown"
    if holder["harness"] == "claude":
        return _claude_state(holder["session_id"])
    return "unknown"


def _open_cycle(root: Path, route: Mapping[str, Any]):
    import artifact_producer
    try:
        return artifact_producer.route_cycle_for(root, route)
    except Exception:
        return None


def _cycle_home(root: Path, record) -> Path | None:
    import artifact_producer
    try:
        return artifact_producer.cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
    except Exception:
        return None


def _last_activity(root: Path, path: Path, raw: Mapping[str, Any], holder, api, record, *,
                   deep: bool = False) -> float:
    """Newest write to the route, its ledger line and its cycle.  `deep` walks
    the cycle tree and completion markers; the cheap form stats the top only."""
    newest = _mtime(path)
    if holder:
        newest = max(newest, float(holder.get("ts") or 0))
    finish_home = root / ".runtime" / "inline-finish" / "v1" / str(raw.get("route_id"))
    newest = max(newest, _newest_mtime(finish_home, limit=20) if deep else _mtime(finish_home))
    if deep:
        try:
            newest = max(newest, _newest_mtime(api.completion_dir(raw["route_id"]), limit=200))
        except Exception:
            pass
    home = _cycle_home(root, record) if record else None
    if record:
        newest = max(newest, _mtime(root / ".runtime/artifact-producer/v1/cycles" / (record["cycle_id"] + ".json")))
    if home is not None:
        newest = max(newest, _newest_mtime(home) if deep else max(_mtime(home), _mtime(home / "artifacts")))
    return newest


def _decide(route_id, raw, *, holder, session_activity, current, new_route_id, held, ended,
            idle_since, now, idle, campaign_close, in_use=False):
    """(reason to close | None, reason kept)."""
    if route_id in held:
        return None, "owner-live"
    if in_use:
        return None, "cycle-in-use"
    identity = (holder["harness"], holder["session_id"]) if holder else None
    if campaign_close and route_id in campaign_close:
        # The closer's own route ends with its campaign; anyone else's only
        # once it is quiet and its session is not provably still there.
        if current and identity == current:
            return "campaign-close", None
        if _session_state(holder) == "alive":
            return None, "session-alive"
        if now - idle_since >= QUIET_SECONDS:
            return "campaign-close", None
        return None, "recent-activity"
    if current and identity == current:
        if new_route_id and route_id != new_route_id and (
                raw.get("effective_intensity") == "direct" or route_id in ended):
            return "superseded", None
        return None, "current-session"
    state = _session_state(holder)
    if state == "alive":
        return None, "session-alive"
    if state == "gone":
        if now - idle_since >= QUIET_SECONDS:
            return "session-ended", None
        return None, "recent-activity"
    recent = max(idle_since, session_activity.get(identity, 0.0) if identity else 0.0)
    if now - recent >= idle:
        return "idle", None
    return None, "recent-activity"


def _pick_evidence(root: Path, record) -> Path | None:
    """The newest nonempty regular file among the cycle's outputs."""
    import artifact_producer
    try:
        directory = artifact_producer.cycle_dir(root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
    except Exception:
        return None
    best = None
    for base, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            candidate = Path(base) / name
            try:
                meta = candidate.lstat()
            except OSError:
                continue
            if candidate.is_symlink() or not meta.st_size or not os.path.isfile(candidate):
                continue
            key = (name.lower().endswith(_OUTPUT_PREFERENCE), meta.st_mtime)
            if best is None or key > best[0]:
                best = (key, candidate)
    return best[1] if best else None


def _seal_cycle(root: Path, route: Mapping[str, Any], proven) -> str:
    import artifact_producer
    record = _open_cycle(root, route)
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
            artifact_producer.finalize(root, cycle_id=record["cycle_id"], state="completed")
            return "cycle-completed"
        except Exception:
            pass
    try:
        result = artifact_producer.finalize(root, cycle_id=record["cycle_id"], state="abandoned",
                                            abandon_reason="route-unrecoverable")
    except Exception as exc:
        return "cycle-left-open:" + str(exc)[:80]
    return "cycle-abandoned-empty" if result.get("status") == "no-lineage" else "cycle-abandoned"


def _summary_text(raw: Mapping[str, Any]) -> str:
    request = raw.get("work_request") if isinstance(raw.get("work_request"), dict) else {}
    text = str(request.get("text") or "").strip()
    return text or f"{raw.get('capability')} {raw.get('slug') or raw.get('route_id')}"


def close_one(root: Path, path: Path, raw: Mapping[str, Any], reason: str, api, *,
              trigger: str, now: float) -> dict:
    """Close one route the runtime decided nobody works on.  Raises on failure."""
    import inline_finish
    record_of = {"reason": reason, "trigger": trigger, "closed_by": "runtime", "at": _iso(now)}
    route = api.verify_route(dict(raw), None, allow_stale_registry=True)
    route_id = route["route_id"]
    pending = inline_finish.pending_state(root, route_id)
    if pending and pending.get("state") != "finished" and not pending.get("autoclose"):
        raise ValueError("finish-claimed-by-session")
    if route.get("effective_intensity") == "direct":
        import artifact_producer
        record = _open_cycle(root, route)
        if pending and pending.get("autoclose"):
            # Resume our own interrupted finish with exactly its recorded intent.
            intent = pending.get("intent") or {}
            record = artifact_producer.read_cycle_record(root, intent.get("cycle_id", ""))
            evidence = (artifact_producer.cycle_dir(root, record["campaign_id"], record["cycle_id"], record)
                        / "artifacts" / intent.get("evidence_path", "")) if record else None
            summary = Path(pending["autoclose"].get("summary_file", ""))
        else:
            evidence = _pick_evidence(root, record) if record else None
            summary = root / ".runtime" / "inline-finish" / "v1" / route_id / "autoclose-summary.md"
        strict_ok = True
        try:
            api.verify_route(dict(raw), raw.get("cwd"))
        except Exception:
            strict_ok = False
        if evidence is not None and strict_ok and Path(route["cwd"]).is_dir():
            if not summary.is_file():
                summary.parent.mkdir(parents=True, exist_ok=True)
                summary.write_text(_summary_text(raw) + "\n", encoding="utf-8")
            args = types.SimpleNamespace(evidence=str(evidence), summary_file=str(summary), commit=None)
            try:
                inline_finish.finish(args, route, path, api,
                                     autoclose={**record_of, "summary_file": str(summary),
                                                "evidence": "auto-selected"})
                return {"route_id": route_id, "reason": reason, "closed": "finished",
                        "cycle": "cycle-completed", "evidence": str(evidence)}
            except Exception:
                state = inline_finish.pending_state(root, route_id)
                if state and state.get("autoclose") and state.get("state") != "claimed":
                    raise   # past the claim: the next sweep resumes the same intent
                if state and state.get("autoclose"):
                    # Our own claim with nothing recorded under it yet.
                    (root / ".runtime" / "inline-finish" / "v1" / route_id / "finish.json").unlink(missing_ok=True)
        record_of["evidence"] = "none"
    outcome, _created = api.close_route(route, path, None, _summary_text(raw),
                                        allow_unproven=True, autoclose=record_of)
    proven = outcome.get("terminal_gate_proven")
    return {"route_id": route_id, "reason": reason, "closed": "proven" if proven else "unproven",
            "cycle": _seal_cycle(root, route, proven)}


def _seal_orphan_cycles(root: Path, api, held: set, cycles: dict, *, campaign_id, deadline, now,
                        open_paths=()) -> list[dict]:
    """Open cycles whose sealing route closed over `QUIET_SECONDS` ago and
    whose finalize never ran."""
    import artifact_producer
    import inline_finish
    sealed, seen = [], set()
    for record in cycles.values():
        if time.monotonic() >= deadline:
            break
        if record["cycle_id"] in seen or (campaign_id and record.get("campaign_id") != campaign_id):
            continue
        seen.add(record["cycle_id"])
        if not api.outcome_path(api.canonical_route_path(root, record["route_id"])).is_file():
            continue   # the begin route is open: the route pass owns it
        try:
            leaf = artifact_producer._finalize_route(root, record)
        except Exception:
            continue
        outcome_file = api.outcome_path(api.canonical_route_path(root, leaf["route_id"]))
        if (leaf["route_id"] in held or not outcome_file.is_file()
                or now - _mtime(outcome_file) < QUIET_SECONDS
                or (root / ".runtime" / "terminal-commits" / "v1" / leaf["route_id"]).exists()):
            continue
        try:
            if inline_finish.pending_for_cycle(root, record["cycle_id"]):
                continue
        except Exception:
            continue
        home = _cycle_home(root, record)
        if home is not None and any(item == str(home) or item.startswith(str(home) + os.sep) for item in open_paths):
            continue
        try:
            proven = json.loads(outcome_file.read_text(encoding="utf-8")).get("terminal_gate_proven")
        except (OSError, ValueError):
            continue
        sealed.append({"cycle_id": record["cycle_id"], "route_id": leaf["route_id"],
                       "cycle": _seal_cycle(root, leaf, proven)})
    return sealed


def sweep(artifact_root, *, api, trigger: str, new_route_id: str | None = None,
          campaign_id: str | None = None, now: float | None = None,
          budget: float = BUDGET_SECONDS, idle: float = IDLE_SECONDS) -> dict:
    """Close what nobody works on under one artifact root.  Never raises."""
    summary: dict[str, Any] = {"closed": [], "cycles": [], "kept": {}, "errors": [], "deferred": 0}
    # Reading old routes prints lineage/registry advisories meant for their
    # owners; this bookkeeping pass reports one line of its own instead.
    with contextlib.redirect_stderr(io.StringIO()):
        return _sweep(summary, artifact_root, api=api, trigger=trigger, new_route_id=new_route_id,
                      campaign_id=campaign_id, now=now, budget=budget, idle=idle)


def _sweep(summary, artifact_root, *, api, trigger, new_route_id, campaign_id, now, budget, idle):
    try:
        root = Path(artifact_root).resolve()
        runtime = root / ".runtime"
        if not (runtime / "routes").is_dir():
            return summary
        now = time.time() if now is None else now
        deadline = time.monotonic() + budget
        lock_path = runtime / "route-autoclose.lock"
        with lock_path.open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                summary["busy"] = True
                return summary
            holders, activity = _composers(api)
            held, ended = _attempts(api)
            current = _current_identity()
            open_paths = _open_paths(root)
            members = None
            if campaign_id:
                import artifact_producer
                members = set()
                for record in artifact_producer.list_cycle_records(root):
                    if record.get("state") == "open" and record.get("campaign_id") == campaign_id:
                        try:
                            members.add(artifact_producer._finalize_route(root, record)["route_id"])
                        except Exception:
                            continue
            cycles = _open_cycles_by_route(root)
            mine = {route_id for route_id, holder in holders.items()
                    if current and (holder["harness"], holder["session_id"]) == current}
            files = sorted(_open_route_files(root, api),
                           key=lambda item: (item.stem not in mine, _mtime(item)))
            for index, path in enumerate(files):
                if time.monotonic() >= deadline:
                    summary["deferred"] += len(files) - index
                    break
                raw = _read_route(path)
                if raw is None:
                    continue
                route_id = raw["route_id"]
                if (runtime / "terminal-commits" / "v1" / route_id).exists():
                    kept = "terminal-commit-pending"
                    summary["kept"][kept] = summary["kept"].get(kept, 0) + 1
                    continue
                holder = holders.get(route_id)
                record = cycles.get(route_id)
                home = _cycle_home(root, record) if record else None
                in_use = home is not None and any(
                    item == str(home) or item.startswith(str(home) + os.sep) for item in open_paths)
                facts = dict(holder=holder, session_activity=activity, current=current,
                             new_route_id=new_route_id, held=held, ended=ended, now=now, idle=idle,
                             campaign_close=members, in_use=in_use)
                reason, kept = _decide(route_id, raw, idle_since=_last_activity(
                    root, path, raw, holder, api, record), **facts)
                if reason not in (None, "superseded"):
                    # Confirm quiet against the whole cycle tree before acting.
                    reason, kept = _decide(route_id, raw, idle_since=_last_activity(
                        root, path, raw, holder, api, record, deep=True), **facts)
                if reason is None:
                    summary["kept"][kept] = summary["kept"].get(kept, 0) + 1
                    continue
                try:
                    summary["closed"].append(close_one(root, path, raw, reason, api, trigger=trigger, now=now))
                except Exception as exc:
                    summary["errors"].append({"route_id": route_id, "error": str(exc)[:160]})
            summary["cycles"] = _seal_orphan_cycles(root, api, held, cycles, campaign_id=campaign_id,
                                                    deadline=deadline, now=now, open_paths=open_paths)
    except Exception as exc:
        summary["errors"].append({"error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    return summary


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
