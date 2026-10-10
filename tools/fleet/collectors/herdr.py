"""F-100b — herdr attachment probe (stdlib only).

One ``herdr agent list`` per snapshot answers "is this depth-0 session attached to a
herdr pane RIGHT NOW" by exact session-id match: the JSON carries
``agent_session.value`` (the harness's own session id) per agent. That is a stronger
signal than process lineage (``procscan.provenance``), which answers only "who
launched it" and, with a shell between the harness and herdr, could not even see
herdr until F-100 fixed the walk.

Verdict per session (``Session.herdr_attached``):
  · True  — listed by herdr for this harness + session id, OR (F-100c) the session's
            pid is a foreground process of a herdr pane / descends from a herdr pane's
            shell (``herdr pane process-info``), which is exact for every harness —
            herdr reports no session id at all for OpenCode (measured 2026-09-03).
  · False — herdr answered (agent list + pane probe) and neither signal matched.
  · None  — no evidence either way: herdr absent/unreachable/malformed, or a
            worker/companion row. When herdr is absent the lineage walk is the
            fallback and can only ever promote to True.
Misattribution is worse than absence (PRD F-26): every failure path yields None.
"""
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import wait

from ..refresh import background_read

_TIMEOUT_S = 2.0
def verified_id_harnesses():
    """Harnesses whose Fleet session_id is known to equal herdr's ``agent_session.value``.

    Each adapter declares it (`harness-capabilities.json` ``session_identity.herdr_session_id``;
    measured 2026-09-03, herdr 0.8: Claude UUID ↔ ``sessionId``, Codex thread id ↔ the
    rollout session_id). A harness outside this set can be promoted to True by a match but
    never demoted to False.
    """
    import sys
    from pathlib import Path
    utilities = str(Path(__file__).resolve().parents[3] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    from harness_capabilities import herdr_verified_harnesses
    return herdr_verified_harnesses()
_CODEX_PANE_CACHE = {"key": None, "until": 0.0, "identities": {}}
_CODEX_PANE_TTL_S = 10.0


def list_agents(runner=subprocess.run, which=shutil.which):
    """``[agent dict, ...]`` from ``herdr agent list``; ``None`` when herdr is absent,
    the call fails/times out, or the payload is not the documented shape."""
    if which("herdr") is None:
        return None
    try:
        proc = runner(["herdr", "agent", "list"], capture_output=True, text=True,
                      timeout=_TIMEOUT_S)
    except Exception:
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "")
    except ValueError:
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    agents = result.get("agents") if isinstance(result, dict) else None
    if not isinstance(agents, list):
        return None
    return [a for a in agents if isinstance(a, dict)]


def list_panes(runner=subprocess.run, which=shutil.which):
    """``[pane dict, ...]`` from ``herdr pane list``; ``None`` on any failure."""
    if which("herdr") is None:
        return None
    try:
        proc = runner(["herdr", "pane", "list"], capture_output=True, text=True,
                      timeout=_TIMEOUT_S)
        payload = json.loads(proc.stdout or "")
    except Exception:
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    panes = result.get("panes") if isinstance(result, dict) else None
    if not isinstance(panes, list):
        return None
    return [p for p in panes if isinstance(p, dict)]


class PaneEvidence(tuple):
    """Unpackable PID evidence with the completeness needed for a negative fact."""
    def __new__(cls, values, complete=True, errors=()):
        result = super().__new__(cls, values)
        result.complete, result.errors = complete, tuple(errors)
        return result


def pane_evidence(panes, runner=subprocess.run, bindings=None):
    """Return shell PIDs, foreground PIDs, and exact Codex PID → thread IDs.

    A pane's session metadata alone can outlive its foreground process. Only a live
    foreground ``codex`` process reported by that same pane may supply an identity;
    conflicting pane claims for one PID are discarded.
    """
    shells, fg = set(), set()
    identities = {}
    errors = []
    selected = [pane for pane in panes or [] if pane.get("agent")]

    def read(pane):
        pane_id = pane.get("pane_id")
        if not pane_id:
            raise ValueError("pane id missing")
        proc = runner(["herdr", "pane", "process-info", "--pane", str(pane_id)],
                      capture_output=True, text=True, timeout=_TIMEOUT_S)
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError("process-info exit %s" % proc.returncode)
        info = (json.loads(proc.stdout or "").get("result") or {}).get("process_info")
        if not isinstance(info, dict):
            raise ValueError("process-info missing")
        return info

    # One shared deadline replaces N serial pane deadlines. Daemon reads never
    # hold the TUI open; subprocess.run still owns each child's timeout/cleanup.
    reads = [(pane, background_read(lambda pane=pane: read(pane))) for pane in selected]
    if reads:
        wait([future for _, future in reads], timeout=_TIMEOUT_S)
    for pane, future in reads:
        pane_id = pane.get("pane_id")
        try:
            if not future.done():
                raise TimeoutError("process-info deadline")
            info = future.result()
            if info.get("shell_pid"):
                shells.add(int(info["shell_pid"]))
            for proc_rec in info.get("foreground_processes") or []:
                if isinstance(proc_rec, dict) and proc_rec.get("pid"):
                    pid = int(proc_rec["pid"])
                    fg.add(pid)
                    if bindings is not None and proc_rec.get("name") == pane.get("agent"):
                        bindings.setdefault(pid, set()).add(str(pane_id))
                    agent_session = pane.get("agent_session") or {}
                    if (pane.get("agent") == "codex"
                            and isinstance(agent_session, dict)
                            and agent_session.get("agent") == "codex"
                            and isinstance(agent_session.get("value"), str)
                            and proc_rec.get("name") == "codex"):
                        identities.setdefault(pid, set()).add(agent_session["value"])
        except Exception as exc:
            errors.append("%s: %s: %s" % (pane_id, type(exc).__name__, str(exc)[:120]))
    return PaneEvidence((shells, fg, {pid: next(iter(sids)) for pid, sids in identities.items()
                                    if len(sids) == 1}),
                        complete=panes is not None and not errors, errors=errors)


def pane_pids(panes, runner=subprocess.run, bindings=None):
    """F-100c — ``(shell_pids, foreground_pids)`` for agent panes."""
    evidence = pane_evidence(panes, runner=runner, bindings=bindings)
    shells, fg, _identities = evidence
    return PaneEvidence((shells, fg), complete=getattr(evidence, "complete", True),
                        errors=getattr(evidence, "errors", ()))


def codex_pane_sessions(sessions, panes=None, runner=subprocess.run):
    """Exact thread IDs for unresolved Codex TUIs, bounded by cwd and cached by PID.

    Herdr pane metadata can be stale (a closed pane may retain a thread ID), so a
    matching live foreground Codex PID and its original process start are required.
    This is called only for TUIs still missing a rollout after process attribution.
    """
    from . import procscan

    targets = {
        int(s.pid): s for s in sessions
        if getattr(s, "pid", None) is not None and getattr(s, "proc_start", None)
    }
    if not targets:
        return {}
    key = tuple(sorted((pid, str(s.proc_start), os.path.realpath(s.cwd or ""))
                       for pid, s in targets.items()))
    now = time.monotonic()
    if panes is None and runner is subprocess.run and key == _CODEX_PANE_CACHE["key"] \
            and now < _CODEX_PANE_CACHE["until"]:
        found = _CODEX_PANE_CACHE["identities"]
    else:
        if panes is None:
            panes = list_panes()
        cwds = {os.path.realpath(s.cwd or "") for s in targets.values()}
        selected = [p for p in panes or []
                    if p.get("agent") == "codex"
                    and os.path.realpath(p.get("cwd") or "") in cwds]
        _shells, _fg, found = pane_evidence(selected, runner=runner)
        if runner is subprocess.run:
            _CODEX_PANE_CACHE.update(key=key, until=now + _CODEX_PANE_TTL_S,
                                     identities=found)
    return {pid: sid for pid, sid in found.items()
            if pid in targets and procscan.read_proc_start(pid) == targets[pid].proc_start}


def _ppid_of(pid):
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            data = f.read()
        return int(data[data.rindex(")") + 1:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def pid_in_panes(pid, shells, fg, max_depth=6, ppid_of=None):
    """True when ``pid`` is a pane's foreground process or descends from a pane shell."""
    if ppid_of is None:
        ppid_of = _ppid_of          # resolved at call time so tests can patch the module
    try:
        cur = int(pid)
    except (TypeError, ValueError):
        return False
    if cur in fg:
        return True
    for _ in range(max_depth):
        cur = ppid_of(cur)
        if not cur or cur <= 1:
            return False
        if cur in shells or cur in fg:
            return True
    return False


def attached_index(agents):
    """``{(harness, session_id): agent}`` over the listed agents."""
    out = {}
    for agent in agents or []:
        if not isinstance(agent, dict):
            continue
        sess = agent.get("agent_session")
        if not isinstance(sess, dict):
            continue
        harness = str(sess.get("agent") or agent.get("agent") or "").lower()
        value = sess.get("value")
        if harness and isinstance(value, str) and value:
            out[(harness, value)] = agent
    return out


def _eligible(session):
    if getattr(session, "is_child", False) or getattr(session, "app_server", False):
        return False
    if getattr(session, "mem_worker", False):
        return False
    return True


def _clear_gpu_session_aliases(harness, sid, pane):
    """Display-only ids linked by this pane's confirmed native /clear."""
    if harness not in {"claude", "codex", "opencode"} or not sid or not pane:
        return []
    try:
        import sys
        from pathlib import Path
        utilities = str(Path(__file__).resolve().parents[3] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import session_tidy
        seat = session_tidy.Seat("pane", session_tidy._digest("pane", pane), pane, harness)
        record = session_tidy.read_json(session_tidy.state_root() / "clear" / (seat.key + ".json"))
        if (not isinstance(record, dict) or record.get("schema") != 1
                or record.get("status") != "cleared" or record.get("harness") != harness
                or record.get("new_session") != sid
                or (record.get("seat") or {}).get("kind") != "pane"
                or (record.get("seat") or {}).get("pane") != pane
                or (record.get("observed") or {}).get("harness") != harness
                or (record.get("observed") or {}).get("sid") != sid):
            return []
        older = record.get("sid")
        if not isinstance(older, str) or not older or older == sid:
            return []
        aliases = [older]
        starts = [row for row in session_tidy._read_ledger_lines(seat)
                  if row.get("harness") == harness and row.get("event") == "start"]
        if len(starts) >= 2 and starts[-1].get("sid") == sid and starts[-2].get("sid") == older:
            for index in range(len(starts) - 2, 0, -1):
                if starts[index].get("source") != "clear":
                    break
                aliases.append(starts[index - 1]["sid"])
        return list(dict.fromkeys(aliases))[:8]
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        return []


def pane_session_metadata(harness, pane, cwd):
    """Existing native seat metadata when the pane omits its session ID.

    Read-only display context, not a process identity or a new probe. A foreign
    repository record cannot identify the pane's current displayed project.
    """
    if not pane or harness not in {"claude", "codex", "opencode"}:
        return None
    try:
        import sys
        from pathlib import Path
        utilities = str(Path(__file__).resolve().parents[3] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import session_tidy
        from ..gitinfo import resolve_gitdir
        seat = session_tidy.Seat("pane", session_tidy._digest("pane", pane), pane, harness)
        row = session_tidy.latest_session(seat, harness)
        repository = resolve_gitdir(cwd)[1]
        if row and repository and resolve_gitdir(row.get("cwd"))[1] == repository:
            return row
    except Exception:
        pass
    return None


def pane_session_aliases(harness, sid, pane, cwd):
    """Reuse the common seat-history display join, without another pane probe."""
    try:
        import sys
        from pathlib import Path
        utilities = str(Path(__file__).resolve().parents[3] / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import session_tidy
        return session_tidy.pane_session_aliases(harness, sid, pane, cwd)
    except Exception:
        return []


def enrich(sessions, agents=None, lineage=None, panes=None, pids=None, pane_bindings=None):
    """Set ``herdr_attached`` on every eligible depth-0 session. ``agents`` = a
    pre-fetched ``list_agents()`` result (``None`` → probe once here); ``panes``/``pids``
    = pre-fetched pane list / ``pane_pids()`` result (``None`` → probe once here);
    ``lineage`` = ``pid -> provenance`` callable used only when herdr is absent."""
    for session in sessions:
        session._gpu_session_aliases = []
        session._herdr_name = None
    if agents is None:
        agents = list_agents()
    if agents is None:
        if lineage is None:
            try:
                from . import procscan
                lineage = procscan.provenance
            except Exception:
                lineage = None
        for s in sessions:
            if not _eligible(s) or lineage is None:
                continue
            try:
                s.herdr_attached = True if lineage(s.pid) == "herdr" else None
            except Exception:
                s.herdr_attached = None
        return {"complete": False, "last_error": "herdr 조회 불가"}
    index = attached_index(agents)
    bindings = {} if pane_bindings is None else pane_bindings
    if pids is None:
        if panes is None:
            panes = list_panes()
        pids = pane_pids(panes, bindings=bindings) if panes is not None else None
    shells, fg = pids if pids else (set(), set())
    probe_ok = pids is not None and getattr(pids, "complete", True)
    # A native resume can run inside the still-live foreground runtime. Keep
    # one pane row instead of letting that detached companion make the same
    # display alias ambiguous. Independent live processes remain separate.
    from . import procscan
    companions = set()
    for owner in sessions:
        owner_panes = bindings.get(owner.pid, set())
        if not _eligible(owner) or len(owner_panes) != 1 or not owner.proc_start:
            continue
        pane = next(iter(owner_panes))
        aliases = pane_session_aliases(owner.harness, owner.session_id, pane, owner.cwd)
        for other in sessions:
            agent = index.get((other.harness, other.session_id), {})
            if (other is owner or not _eligible(other) or other.harness != owner.harness
                    or not other.detached or other.session_id not in aliases
                    or agent.get("pane_id") != pane or not other.proc_start
                    or procscan.read_proc_start(owner.pid) != owner.proc_start
                    or procscan.read_proc_start(other.pid) != other.proc_start
                    or not pid_in_panes(other.pid, set(), {owner.pid})):
                continue
            companions.add(id(other))
            owner.session_aliases = list(dict.fromkeys(
                list(getattr(owner, "session_aliases", None) or []) + [other.session_id]))
            for field in ("liveness", "mtime", "title", "summary", "summary_ts", "model", "effort",
                          "ctx_pct", "context", "context_window_tokens", "exec_tool", "work_projection"):
                value = getattr(other, field, None)
                if value is not None:
                    setattr(owner, field, value)
    if companions:
        sessions[:] = [s for s in sessions if id(s) not in companions]
    for s in sessions:
        if not _eligible(s):
            continue
        sid = getattr(s, "session_id", None)
        harness = str(getattr(s, "harness", "") or "").lower()
        if sid and (harness, sid) in index:
            s.herdr_attached = True
        elif pid_in_panes(getattr(s, "pid", None), shells, fg):
            s.herdr_attached = True
        elif probe_ok:
            # herdr answered on both surfaces (or on the id surface for a verified-id
            # harness) and nothing matched → a plain terminal, not a guess.
            s.herdr_attached = False
        else:
            s.herdr_attached = None
        # Herdr can lag a native resume/clear. Its exact foreground PID (or an
        # existing session match) identifies the pane; seat history joins its IDs.
        matching_panes = bindings.get(s.pid, set())
        if not matching_panes and sid and (harness, sid) in index:
            pane = index[(harness, sid)].get("pane_id")
            matching_panes = {pane} if pane else set()
        if len(matching_panes) == 1:
            pane = next(iter(matching_panes))
            names = {agent.get("name") for agent in agents
                     if isinstance(agent, dict) and agent.get("pane_id") == pane
                     and agent.get("agent") == harness
                     and agent.get("name")}
            if len(names) == 1:
                s._herdr_name = names.pop()
            aliases = _clear_gpu_session_aliases(harness, sid, next(iter(matching_panes)))
            pane_aliases = pane_session_aliases(harness, sid, pane, getattr(s, "cwd", None))
            if aliases or pane_aliases:
                from . import procscan
                if getattr(s, "proc_start", None) and procscan.read_proc_start(s.pid) == s.proc_start:
                    live = {(other.harness, other.session_id) for other in sessions}
                    s._gpu_session_aliases = [old for old in aliases if (harness, old) not in live]
                    s.session_aliases = list(dict.fromkeys(
                        list(getattr(s, "session_aliases", None) or [])
                        + [other for other in pane_aliases if (harness, other) not in live]))
    errors = list(getattr(pids, "errors", ()))
    return {"complete": probe_ok,
            "last_error": "; ".join(errors) if errors else None if probe_ok else "pane 조회 불가"}
