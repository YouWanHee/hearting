"""opencode enrichment — passive, read-only SQLite (01_tap_mechanics.md §3).

State lives in ~/.local/share/opencode/opencode.db (WAL; opened mode=ro). The `session` table
carries per-session model/cwd/cost/tokens live. argv has no session id, so pid↔session is
matched by /proc/cwd == session.directory, narrowed to the session THIS process created: the
earliest top-level (parent_id IS NULL) session in that directory whose time_created is at/after
the process start (/proc/<pid>/stat field 22 against /proc/stat btime — the same derivation
Codex already trusts). That narrowing matters because several opencode sessions can share one
directory: without it every process in a shared directory collapsed onto whichever session was
most recently updated, so the panes rendered as one. A process that attached to a pre-existing
session has no such candidate and takes the older binding (most recently updated top-level in
that directory) unchanged.

Structurally missing (render '—', PRD §2/§4): rate-limit (no column). effort = model.variant.
Active context is the latest request's input + cache.read + cache.write. Its display
denominator requires the same process/session's observed native compaction policy; the
session-table `tokens_input` is cumulative cost-side usage and a models.dev catalogue window
does not prove the running policy.
"""
import datetime
import math
import json
import os
import re
import sqlite3
import stat
import sys
import time
from collections import OrderedDict
from pathlib import Path

from ..model import ContextEvidence, SubAgent
from . import shell_jobs as _shell_jobs
from .. import session_registry
from .. import titles

_COLS = ("id, slug, agent, model, cost, tokens_input, tokens_output, tokens_reasoning, "
         "time_updated, parent_id")

# Variant tokens that name the runtime's own choice rather than a user-set dial. These are
# the same words render drops in `_EMPTY_EFFORT` (render.py) — mirrored here, not imported:
# collectors must not import render at module scope (see session_registry's import
# contract), so the two lists are kept in step by name and test rather than by dependency.
_DEFAULT_VARIANTS = frozenset({"default", "runtime-default", "inherit"})
_V2_SHELL_TASKS = OrderedDict()
_V2_SHELL_TASK_LIMIT = 256


def _v2_shell_key(db, session_id, attempt_id=None):
    return (os.path.realpath(db), session_id, attempt_id, "v2")


def _v2_shell_cursor(db, session_id, attempt_id=None):
    state = _V2_SHELL_TASKS.get(_v2_shell_key(db, session_id, attempt_id))
    if not isinstance(state, dict):
        return 0
    try:
        st = os.stat(db)
        identity = {"device": st.st_dev, "inode": st.st_ino}
    except OSError:
        identity = None
    return state.get("cursor", 0) if state.get("database_identity") == identity else 0


def _attach_v2_shell_tasks(sess, observed, db, session_id):
    attempt_id = getattr(sess, "attempt_id", None)
    key = _v2_shell_key(db, session_id, attempt_id)
    source = observed.get("database_identity")
    source_label = ("opencode-v2:%s:%s" % (source.get("device"), source.get("inode"))
                    if isinstance(source, dict) else "opencode-v2:identity-unavailable")
    state = _V2_SHELL_TASKS.get(key)
    if (not isinstance(state, dict) or state.get("database_identity") != source):
        retired = list(state.get("retired_jobs", ())) if isinstance(state, dict) else []
        overflow = bool(state.get("overflow")) if isinstance(state, dict) else False
        if isinstance(state, dict):
            for old in state.get("jobs", {}).values():
                item = dict(old)
                item.update(observation_state="unknown",
                            observation_reason="database-generation-changed",
                            process_state="unknown",
                            process_reason="database-generation-changed")
                retired.append(item)
        state = {"database_identity": source, "cursor": 0,
                 "jobs": OrderedDict(), "retired_jobs": retired[-256:],
                 "overflow": overflow or len(retired) > 256}
    cursor = observed.get("shell_cursor") or {}
    if isinstance(cursor.get("next_seq"), int) and not isinstance(cursor.get("next_seq"), bool):
        state["cursor"] = max(state.get("cursor", 0), cursor["next_seq"])
    for native in observed.get("shell_tasks") or ():
        if not isinstance(native, dict) or not native.get("native_id"):
            continue
        native_id = str(native["native_id"])
        previous = state["jobs"].get(native_id, {})
        started_ms = native.get("started_at_ms")
        started = _shell_jobs.valid_epoch(
            started_ms, milliseconds=isinstance(started_ms, (int, float)) and not isinstance(started_ms, bool))
        updated_ms = native.get("time_updated")
        observed_at = _shell_jobs.valid_epoch(
            updated_ms, milliseconds=isinstance(updated_ms, (int, float)) and not isinstance(updated_ms, bool))
        item = dict(previous)
        item.update({
            "harness": "opencode", "session_id": session_id,
            "attempt_id": attempt_id,
            "source_generation": source_label,
            "source_locator": os.path.realpath(db), "native_id": native_id,
            "native_call_id": native.get("call_id"), "command": (native.get("input") or {}).get("command"),
            "command_source": "session_message.shell.input.command",
            "started_at": previous.get("started_at", started),
            "start_source": "part.time.ran|created", "observed_at": observed_at or time.time(),
            "native_status": "acknowledged", "invocation_status": native.get("invocation_status"),
            "invocation_completed_at": _shell_jobs.valid_epoch(
                native.get("invocation_completed_at_ms"), milliseconds=True),
            "process_state": "unknown", "process_reason": "opencode-background-process-id-unobserved",
            "output_coverage": "unavailable", "output_reason": "managed-output-locator-unobserved",
            "observation_state": cursor.get("state"), "observation_reason": cursor.get("reason"),
            "source_revision": (native.get("seq"), native.get("time_updated")),
        })
        state["jobs"][native_id] = item
        state["jobs"].move_to_end(native_id)
        while len(state["jobs"]) > _shell_jobs.MAX_RETAINED_TASKS:
            state["jobs"].popitem(last=False)
            state["overflow"] = True
    _V2_SHELL_TASKS[key] = state
    _V2_SHELL_TASKS.move_to_end(key)
    while len(_V2_SHELL_TASKS) > _V2_SHELL_TASK_LIMIT:
        _V2_SHELL_TASKS.popitem(last=False)
    sess.shell_jobs = _shell_jobs.observe_jobs(
        list(state.get("retired_jobs", ())) + list(state["jobs"].values()), now=time.time(),
        parent_pid=getattr(sess, "pid", None), parent_start=getattr(sess, "proc_start", None),
        attempt_id=attempt_id)
    sess.shell_job_observation = {
        "state": "partial" if state.get("overflow") else cursor.get("state", "unknown"),
        "reason": "task-retention-limit" if state.get("overflow") else cursor.get("reason"),
        "coverage": "partial" if (state.get("overflow") or not cursor.get("caught_up")
                                     or cursor.get("state") != "observed") else "complete",
        "cursor": state.get("cursor"), "source_generation": source,
    }

_REG = {"ts": 0.0, "map": None, "by_provider": None}   # → context window (from models.json)
_REG_TTL = 300.0
# `part` first — the `message` table carries only per-message metadata (role/tokens/cost/
# modelID), never conversational text, so a refresh cursor anchored there advances over
# rows that can never produce a title or summary. Fixed order, no schema guessing.
_MESSAGE_TABLES = ("part", "message", "session_message")


def _attempt_sidecar_fallback(sess):
    """Exact-attempt sidecar fallback (SD-95): a registered dispatch session has
    no statusline producer for its runtime sid; the dispatch summary owner
    writes under the attempt sid. attempt_id is exact env/registry identity."""
    attempt_sid = titles.attempt_sid(getattr(sess, "attempt_id", None))
    if not attempt_sid:
        return
    if not getattr(sess, "title", None):
        sess.title = titles.last_title(attempt_sid, harness="opencode")
    if not getattr(sess, "summary", None):
        sess.summary, sess.summary_ts = titles.fresh_summary_with_ts(
            attempt_sid, harness="opencode")


def _opencode_v2_runtime():
    utilities = Path(__file__).resolve().parents[3] / "utilities"
    if str(utilities) not in sys.path:
        sys.path.insert(0, str(utilities))
    import opencode_runtime
    return opencode_runtime


def _enrich_v2(sess, tick, db):
    """Exact V2 sid reader; a cwd/time fallback is accepted only when unique."""
    runtime = _opencode_v2_runtime()
    if not runtime.credential_store_present(db_path=db):
        return False
    sid, binding_source = session_of_process(sess.pid) if getattr(sess, "pid", None) else (None, "")
    shell_cursor = (None if getattr(sess, "_fast_first", False)
                    else _v2_shell_cursor(db, sid, getattr(sess, "attempt_id", None))
                    if sid else 0)
    generation = None
    if sid:
        observed = runtime.read_session_observation(
            sid, db_path=db, shell_after_seq=shell_cursor)
        if observed.get("state") == "observed":
            generation = observed.get("generation")
    else:
        started = _process_started_ms(sess)
        if started is not None and sess.cwd:
            candidates = runtime.list_session_candidates(
                sess.cwd, db_path=db, after_created=started,
                before_created=(tick or {}).get(sess.pid), limit=2)
            if len(candidates) > 1:
                _attempt_sidecar_fallback(sess)
                return True
            if len(candidates) == 1:
                sid = candidates[0].get("id")
                generation = candidates[0].get("generation")
                binding_source = "unique-process-window"
                observed = runtime.read_session_observation(
                    sid, db_path=db, generation=generation,
                    shell_after_seq=shell_cursor)
    if not sid:
        sess.shell_jobs = []
        sess.shell_job_observation = {"state": "unavailable", "reason": "exact-session-id-unresolved"}
        _attempt_sidecar_fallback(sess)
        return True
    if generation != "v2":
        sess.shell_jobs = []
        if generation == "v1":
            sess.shell_job_observation = {
                "state": "unobserved", "reason": "v1-background-native-record-not-observed"}
        else:
            sess.shell_job_observation = {
                "state": observed.get("state", "unknown"),
                "reason": observed.get("reason", "runtime-generation-unresolved")}
        return True
    if "observed" not in locals():
        observed = runtime.read_session_observation(
            sid, db_path=db, generation="v2", shell_after_seq=shell_cursor)
    if observed.get("state") != "observed" or observed.get("generation") != "v2":
        sess.shell_jobs = []
        sess.shell_job_observation = {"state": observed.get("state", "unknown"),
                                     "reason": observed.get("reason", "v2-source-unavailable")}
        _attempt_sidecar_fallback(sess)
        return True
    _attach_v2_shell_tasks(sess, observed, db, sid)
    native = observed.get("session") or {}
    if native.get("directory") and sess.cwd and os.path.realpath(native["directory"]) != os.path.realpath(sess.cwd):
        # The process/session binding is exact, but a mismatched directory is
        # not allowed to reattribute session activity to this pane.
        return True
    sess._opencode_bind_kind = "process" if binding_source == "opencode-tui-selection" else "window"
    sess._refresh_source = {"kind": "opencode-db-v2", "harness": "opencode",
                            "db_path": db, "session_id": sid,
                            "table": "session_message",
                            "cursor_kind": observed.get("cursor_kind"),
                            "observed_cursor": observed.get("cursor")}
    if native.get("directory"):
        from .. import session_cwd
        session_cwd.observe(sess, native["directory"], sid)
    sess.session_id = sid
    sess.title = titles.last_title(sid, harness="opencode")
    sess.summary, sess.summary_ts = titles.fresh_summary_with_ts(sid, harness="opencode")
    _attempt_sidecar_fallback(sess)
    if native.get("slug"):
        sess.slug = native["slug"]
    sess.is_child = bool(native.get("parent_id"))
    model = native.get("model") if isinstance(native.get("model"), dict) else {}
    if model:
        sess.model = model.get("id")
        if model.get("providerID"):
            sess._opencode_provider_id = model["providerID"]
        variant = str(model.get("variant") or "").strip()
        if variant.lower() in _DEFAULT_VARIANTS:
            sess.effort_default = True
        elif variant:
            sess.effort = variant
    for attr, key in (("cost", "cost"), ("session_input_tokens", "tokens_input"),
                      ("session_output_tokens", "tokens_output"),
                      ("session_reasoning_output_tokens", "tokens_reasoning")):
        value = native.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(sess, attr, value)
    values = [native.get(key) for key in ("tokens_input", "tokens_output", "tokens_reasoning")]
    sess.tokens = sum(value for value in values if isinstance(value, (int, float))) or None
    sess.session_total_tokens = sess.tokens
    messages = observed.get("messages") or []
    latest = next((row for row in reversed(messages)
                   if row.get("type") == "assistant" and row.get("tokens")), None)
    if latest:
        last_ctx = _context_tokens_from_payload({"tokens": latest["tokens"]})
        if isinstance(last_ctx, (int, float)) and last_ctx >= 0:
            sess.active_context_tokens = int(last_ctx)
    from ..token_budget import context_policy_snapshot, context_policy_used_pct
    from ..context_policy import read_policy
    model_ref = f"{model.get('providerID') or ''}/{sess.model or ''}"
    policy_record = read_policy(harness="opencode", session_id=sid, pid=sess.pid,
                                proc_start=sess.proc_start, model=model_ref)
    policy_data = policy_record.get("policy") if isinstance(policy_record, dict) else None
    if isinstance(policy_data, dict):
        sess.context_policy = context_policy_snapshot(
            source=policy_data.get("source") or "opencode-native-chat-params",
            runtime_version=policy_data.get("runtime_version"),
            configured_request_tokens=policy_data.get("configured_request_tokens"),
            configured_request=policy_data.get("configured_request"),
            effective_window_tokens=policy_data.get("effective_window_tokens"),
            applied_trigger_tokens=policy_data.get("applied_trigger_tokens"),
            output_allowance_tokens=policy_data.get("output_allowance_tokens"),
            compaction_reserve_tokens=policy_data.get("compaction_reserve_tokens"),
            disabled=policy_data.get("disabled"), enabled=policy_data.get("enabled"),
            enforced=policy_data.get("enforced"), session_id=sid,
            raw_percentage=policy_data.get("raw_percentage"), reason=policy_data.get("reason"))
    else:
        sess.context_policy = context_policy_snapshot(
            source="opencode-db-v2", session_id=sid,
            reason="selected-request-policy-not-observed")
    sess.context_window_tokens = sess.context_policy["effective_window_tokens"]
    sess.context_display_window_tokens = (sess.context_policy["applied_trigger_tokens"]
                                          if sess.context_policy["state"] == "applied" else None)
    sess.ctx_pct = context_policy_used_pct(sess.active_context_tokens, sess.context_policy)
    stamp = native.get("time_updated")
    if isinstance(stamp, (int, float)):
        sess.mtime = stamp / 1000.0
    return True


def _message_table(con):
    """Choose one compatible table in fixed order; no schema guessing by recency."""
    for table in _MESSAGE_TABLES:
        try:
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row:
                con.execute("SELECT rowid FROM %s LIMIT 1" % table).fetchone()
                return table
        except Exception:
            continue
    return None


def _observed_cursor(con, table, sid):
    if not table:
        return None
    try:
        row = con.execute("SELECT MAX(rowid) FROM %s WHERE session_id=?" % table, (sid,)).fetchone()
        return int(row[0]) if row and isinstance(row[0], int) else 0
    except Exception:
        return None


def _load_model_registry():
    """Build (provider-scoped, provider-agnostic) context-window maps. Cached 5 min."""
    now = time.time()
    if _REG["map"] is not None and now - _REG["ts"] <= _REG_TTL:
        return _REG["by_provider"] or {}, _REG["map"] or {}
    scoped, flat = {}, {}
    path = os.environ.get("OPENCODE_MODELS") or os.path.expanduser("~/.cache/opencode/models.json")
    try:
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
        for pkey, prov in (reg.items() if isinstance(reg, dict) else []):
            models = prov.get("models") if isinstance(prov, dict) else None
            if not isinstance(models, dict):
                continue
            pid = (prov.get("id") if isinstance(prov, dict) else None) or pkey
            for mkey, mdef in models.items():
                lim = mdef.get("limit") if isinstance(mdef, dict) else None
                ctx = lim.get("context") if isinstance(lim, dict) else None
                if not isinstance(ctx, (int, float)) or ctx <= 0:
                    continue
                for k in (mkey, mkey.split("/")[-1]):   # bare id or provider/org-prefixed
                    scoped.setdefault((pid, k), int(ctx))
                    if flat.get(k, 0) < ctx:
                        flat[k] = int(ctx)
    except Exception:
        scoped, flat = {}, {}
    _REG.update(ts=now, map=flat, by_provider=scoped)
    return scoped, flat


def _model_ctx_limit(model_id, provider=None):
    """Context window for a model id, from opencode's models.dev registry cache
    (~/.cache/opencode/models.json — the same source opencode's own TUI uses for context%).
    None when unavailable → ctx% stays '—'. Cached 5 min.

    The same model id is published by many providers at different window sizes (one id
    in this registry spans a 48k spread across two providers), so the session's own
    providerID decides. The provider-agnostic max is only a last resort for an unknown
    provider — an over-large window understates ctx%, the safer direction to be wrong.
    """
    if not model_id:
        return None
    scoped, flat = _load_model_registry()
    leaf = model_id.split("/")[-1]
    if provider:
        for key in ((provider, model_id), (provider, leaf)):
            if key in scoped:
                return scoped[key]
    return flat.get(model_id) or flat.get(leaf)


def _db():
    return str(_opencode_v2_runtime().opencode_db_path())


def _process_started_ms(sess):
    """This process's start as epoch MILLIseconds, or None when unknown.

    ``Session.proc_start`` is ``/proc/<pid>/stat`` field 22 — clock ticks since boot.
    ``/proc/stat``'s ``btime`` is that same boot epoch in seconds, so the pair converts
    exactly; the same derivation Codex already trusts for its own process-start match
    (``collectors/codex.py`` ``_process_started_at``). Missing/non-Linux/unreadable
    evidence stays None so the caller keeps the previous, weaker binding.
    """
    try:
        ticks = int(sess.proc_start)
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        if ticks < 0 or clock_ticks <= 0:
            return None
        with open("/proc/stat", encoding="ascii", errors="replace") as handle:
            boot = next(
                int(line.split()[1]) for line in handle
                if line.startswith("btime ")
            )
    except (AttributeError, OSError, StopIteration, TypeError, ValueError):
        return None
    return int((boot + ticks / clock_ticks) * 1000)


SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]{1,252}$")
TUI_SELECTION_SCHEMA = "hearting-tui-selection-v1"
TUI_SELECTION_MAX_BYTES = 1024
# Words that make an `opencode` command line something other than an interactive pane.
_NON_PANE_COMMANDS = frozenset(("run", "serve", "attach", "web", "auth", "agent", "models", "stats",
                                "export", "import", "session", "upgrade", "uninstall", "mcp", "acp",
                                "debug", "completion"))


def _proc_start_ticks(pid):
    try:
        with open("/proc/%d/stat" % int(pid), encoding="ascii", errors="replace") as handle:
            stat = handle.read()
        fields = stat[stat.rindex(") ") + 2:].split()
        return fields[19] if fields[19].isdigit() else None
    except (OSError, ValueError, IndexError):
        return None


def _cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as handle:
            raw = handle.read(32769)
    except (OSError, ValueError):
        return None
    if len(raw) > 32768:
        return None
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _argv_session(argv):
    """The `--session`/`-s` id an `opencode` pane was started on, or None."""
    if not argv or os.path.basename(argv[0]) != "opencode":
        return None
    found = None
    for index, arg in enumerate(argv[1:], 1):
        if arg in _NON_PANE_COMMANDS:
            return None
        key, equal, value = arg.partition("=")
        if key not in ("--session", "-s"):
            continue
        if not equal:
            value = argv[index + 1] if index + 1 < len(argv) else ""
        if found is not None or not SESSION_ID_RE.match(value or ""):
            return None
        found = value
    return found


def _tui_selection_record(pid, start, environ=None):
    """Read the producer's exact lifetime record, including a retained dead one.

    No process, argv, cwd or expected-session lookup supplies this identity.
    Source timestamps are observations; the retire consumer checks acceptance.
    Live readers do not require a retirement timestamp or writtenAt."""
    env = os.environ if environ is None else environ
    home = env.get("HOME") or ""
    base = env.get("XDG_STATE_HOME") or (os.path.join(home, ".local", "state") if home else "")
    if (not base or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or isinstance(start, bool) or not str(start).isdigit() or int(start) <= 0):
        return None
    path = os.path.join(base, "hearting", "tui-identity", "%d-%s.json" % (pid, start))
    def generation(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid,
                info.st_mtime_ns, info.st_ctime_ns)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_size > TUI_SELECTION_MAX_BYTES):
                return None
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(TUI_SELECTION_MAX_BYTES + 1)
            if (len(raw) > TUI_SELECTION_MAX_BYTES or len(raw) != before.st_size
                    or generation(before) != generation(os.fstat(fd))
                    or generation(before) != generation(os.lstat(path))):
                return None
        finally:
            os.close(fd)
        row = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(row, dict) or row.get("schema") != TUI_SELECTION_SCHEMA
            or type(row.get("pid")) is not int or row["pid"] != pid
            or isinstance(row.get("start"), bool) or str(row.get("start")) != str(start)
            or not isinstance(row.get("sessionID"), str)
            or not SESSION_ID_RE.fullmatch(row["sessionID"])):
        return None
    written = None
    try:
        timestamp = datetime.datetime.fromisoformat(row["writtenAt"].replace("Z", "+00:00"))
        if timestamp.tzinfo is not None:
            written = timestamp.timestamp()
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        pass
    return {"value": row, "source": {"generation": generation(before),
            "mtime": before.st_mtime, "ctime": before.st_ctime, "written_at": written}}


def _tui_selection(pid, start, environ=None):
    """The exact TUI selection SID or None; live callers check /proc separately."""
    record = _tui_selection_record(pid, start, environ)
    return record["value"]["sessionID"] if record is not None else None


def session_of_process(pid, environ=None):
    """`(session_id, source)` an OpenCode process proves about itself, or `(None, "")`.

    Its TUI's own selection record first (it follows `/new` and session switches),
    then the `--session` it was started with. Never the database's newest session,
    which is a guess shared by every pane in the directory."""
    start = _proc_start_ticks(pid)
    if start is None:
        return None, ""
    selected = _tui_selection(pid, start, environ)
    if selected:
        return selected, "opencode-tui-selection"
    started_on = _argv_session(_cmdline(pid))
    if started_on and _proc_start_ticks(pid) == start:
        return started_on, "opencode-argv"
    return None, ""


def session_id_of_process(pid, environ=None):
    return session_of_process(pid, environ)[0]


def prepare_tick(sessions):
    """`{pid: next same-directory opencode start (ms) | None}` for this tick.

    A process owns the top-level sessions created between its own start and the next
    same-directory opencode process's start; that window is what separates panes that
    share a checkout without pinning a pane to the first session it ever opened.
    """
    starts = {}
    for sess in sessions:
        if getattr(sess, "harness", None) != "opencode" or not getattr(sess, "cwd", None):
            continue
        started = _process_started_ms(sess)
        if started is not None:
            starts.setdefault(sess.cwd, []).append((started, sess.pid))
    until = {}
    for rows in starts.values():
        rows.sort()
        for index, (_started, pid) in enumerate(rows):
            until[pid] = rows[index + 1][0] if index + 1 < len(rows) else None
    return until


def _keeper_key(sess):
    """Sort key for one member of a shared session id: a row its own process proved
    (its TUI selection record) first, then window-bound rows that created the
    session (exact ownership), then earliest process start (the creator
    started before any attacher or helper), then lowest pid. Missing evidence
    sorts last — never used as a fact."""
    kind = getattr(sess, "_opencode_bind_kind", None)
    try:
        started = int(getattr(sess, "proc_start", None))
    except (TypeError, ValueError):
        started = None
    try:
        pid = int(getattr(sess, "pid", None))
    except (TypeError, ValueError):
        pid = None
    return (
        {"process": 0, "window": 1}.get(kind, 2),
        started if started is not None and started >= 0 else float("inf"),
        pid if pid is not None else float("inf"),
    )


def _unbind_shadow(sess):
    """Return a duplicate-bound row to anonymous-process state.

    The process row stays (existence is the backbone's decision, PRD §1) but
    carries no session identity, so the exact herdr/steward/peer joins and the
    tag mint — all keyed on session_id — answer on the keeper only. mtime is
    kept: the transcript really is updating, and liveness must stay truthful
    about the live process. slug is restored to the scan-time cwd basename.
    """
    sess.session_id = None
    sess.session_tag = None
    cwd = getattr(sess, "cwd", None)
    sess.slug = os.path.basename(cwd.rstrip("/")) if cwd else None
    sess.title = None
    sess.summary = None
    sess.summary_ts = None
    sess.subagents = None
    sess.model = None
    sess.effort = None
    sess.effort_default = False
    sess.cost = None
    sess.tokens = None
    sess.session_input_tokens = None
    sess.session_output_tokens = None
    sess.session_reasoning_output_tokens = None
    sess.session_total_tokens = None
    sess.active_context_tokens = None
    sess.context_window_tokens = None
    sess.context_display_window_tokens = None
    sess.context_policy = None
    sess.ctx_pct = None
    sess._context_evidence = None
    sess._refresh_source = None
    sess._opencode_bind_kind = None


def collapse_duplicate_sids(sessions):
    """One session id → one row.

    Measured 2026-10-05: a session leader plus its arg-less helper child bound
    the same sid through the directory fallback and rendered as two identical
    working rows (issue #158). The 2026-09-29 window binding only separates
    processes that each created a session; every other process in the directory
    shares the one fallback guess, so the losers must be unbound here, after
    all bindings are known — a per-row claim inside enrich cannot do this,
    because a lower-pid fallback row enriches before the higher-pid process
    that actually owns the session through its window.
    """
    groups = {}
    for sess in sessions:
        if getattr(sess, "harness", None) != "opencode":
            continue
        sid = getattr(sess, "session_id", None)
        if not sid:
            continue
        groups.setdefault(sid, []).append(sess)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=_keeper_key)
        for loser in members[1:]:
            _unbind_shadow(loser)


def _query_window(cur, cwd, proc_start_ms, until_ms=None):
    """The exact branch: the top-level session this process created.

    Most recently updated among the sessions created inside this process's
    window [own start, next same-directory start). Returns None when this
    process created nothing (attached to a pre-existing session, or its own
    session row does not exist yet) — the caller then falls back.
    """
    bound = "AND time_created<? " if until_ms is not None else ""
    args = (cwd, proc_start_ms) + ((until_ms,) if until_ms is not None else ())
    return cur.execute(
        "SELECT %s FROM session WHERE directory=? AND parent_id IS NULL "
        "AND time_created>=? %sORDER BY time_updated DESC LIMIT 1" % (_COLS, bound),
        args,
    ).fetchone()


def _query_fallback(cur, cwd):
    """The guess branch: most recently updated top-level session in the
    directory, else any session there. Shared by every process that created
    nothing itself — which is exactly why two such processes can land on one
    session id (see collapse_duplicate_sids)."""
    for extra in ("AND parent_id IS NULL ", ""):
        row = cur.execute(
            "SELECT %s FROM session WHERE directory=? %s"
            "ORDER BY time_updated DESC LIMIT 1" % (_COLS, extra),
            (cwd,),
        ).fetchone()
        if row:
            return row
    return None


def _query(cur, cwd, proc_start_ms=None, until_ms=None):
    # N opencode processes can share one directory, and one process can open several
    # sessions over its life (/new). Bind to the most recently updated top-level session
    # created inside this process's window [own start, next same-directory start): the
    # window keeps panes apart, and "most recently updated" follows the session the pane
    # is on now instead of the first one it ever created. A process that attached to a
    # pre-existing session has no candidate here and keeps the older binding below.
    if proc_start_ms is not None:
        row = _query_window(cur, cwd, proc_start_ms, until_ms)
        if row:
            return row
    # prefer a top-level session; fall back to any session in the directory
    return _query_fallback(cur, cwd)


def _context_tokens_from_payload(payload):
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    total_tokens = tokens.get("total")
    if (isinstance(total_tokens, (int, float)) and not isinstance(total_tokens, bool)
            and math.isfinite(total_tokens) and total_tokens > 0):
        return int(total_tokens)
    total = 0
    observed = False
    for value in (tokens.get("input"), tokens.get("output"), cache.get("read"), cache.get("write")):
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            total += value
            observed = True
    return total if observed else None


def _last_request_context(con, sid):
    """Latest assistant step prompt size, excluding output/cumulative session totals."""
    for table in ("message", "part", "session_message"):
        try:
            rows = con.execute(
                "SELECT data FROM %s WHERE session_id=? ORDER BY time_updated DESC LIMIT 50" % table,
                (sid,),
            )
        except Exception:
            continue
        for (data,) in rows:
            try:
                payload = json.loads(data) or {}
            except Exception:
                continue
            ctx = _context_tokens_from_payload(payload)
            if ctx == 0 and not _request_completed(payload):
                continue         # a just-created assistant row starts at zero; not a reading
            if ctx is not None:
                return ctx
    return None


def _request_completed(payload):
    """True when an assistant row records that its request finished (finish or time.completed)."""
    if not isinstance(payload, dict):
        return False
    if payload.get("finish"):
        return True
    moment = payload.get("time")
    return isinstance(moment, dict) and bool(moment.get("completed"))


def _child_sessions(con, sid):
    """F-29 (v9, prd.md:292 source #1 — already SELECTing agent/parent_id, previously
    discarded at the `_query` filter). None on any read failure (honest gap, not a guess);
    [] when the query succeeds and finds no children.

    No completion signal exists in this schema (unlike claude's tool_use/tool_result
    pairing) — every row found here is reported active=True; that is not a guess, it is
    the absence of evidence to the contrary, and the absence renders as '—'-adjacent
    (nothing hidden) rather than a fabricated 'done'.
    """
    try:
        rows = con.execute(
            "SELECT id, agent, time_updated FROM session WHERE parent_id=? "
            "ORDER BY time_updated DESC", (sid,),
        ).fetchall()
    except Exception:
        return None
    out = []
    for _cid, agent, tupd in rows:
        out.append(SubAgent(agent_type=agent or None, active=True,
                            started_at=(tupd / 1000.0) if isinstance(tupd, (int, float))
                                      else None,
                            source="opencode-db"))
    return out


def enrich(sess, tick=None):
    # B-5: no hearting-managed writer exists yet for OpenCode (`writer_support ==
    # "not-implemented"`), so this always reads None today — the call is here so the
    # harness-parity contract (D) and a future writer both have one call site, matching
    # claude/codex's tier-1 read at the top of their own `enrich`.
    try:
        rec = session_registry.read("opencode", sess.pid)
        if rec:
            session_registry.apply_to_session(sess, rec, "opencode")
    except Exception:
        pass
    db = _db()
    if not sess.cwd or not os.path.exists(db):
        return
    if _enrich_v2(sess, tick, db):
        return
    con = None
    last_ctx = None
    subagents = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1.0)
        cur = con.cursor()
        start_ms = _process_started_ms(sess)
        row, sess._opencode_bind_kind = None, None
        proven, source = session_of_process(sess.pid) if getattr(sess, "pid", None) else (None, "")
        if proven and source == "opencode-tui-selection":
            row = cur.execute("SELECT %s FROM session WHERE id=? LIMIT 1" % _COLS, (proven,)).fetchone()
            if row:
                sess._opencode_bind_kind = "process"
                directory = cur.execute("SELECT directory FROM session WHERE id=? LIMIT 1", (proven,)).fetchone()
                if directory:
                    from .. import session_cwd
                    session_cwd.observe(sess, directory[0], proven)
        if row is None and start_ms is not None:
            row = _query_window(cur, sess.cwd, start_ms, (tick or {}).get(sess.pid))
            if row:
                sess._opencode_bind_kind = "window"
        if row is None:
            row = _query_fallback(cur, sess.cwd)
            if row:
                sess._opencode_bind_kind = "fallback"
        if row and row[0]:
            table = _message_table(con)
            cursor = _observed_cursor(con, table, row[0])
            sess._refresh_source = {
                "kind": "opencode-db", "harness": "opencode", "db_path": db,
                "session_id": row[0], "table": table,
                "cursor_kind": "opencode-rowid-v1:%s" % table if table else None,
                "observed_cursor": cursor,
            }
            last_ctx = _last_request_context(con, row[0])
            # R3-2: only query children when `row` is genuinely top-level (parent_id IS NULL,
            # row[-1] here) — the `_query` fallback clause can hand back a CHILD session, and
            # querying ITS children would surface grandchildren under the wrong parent.
            if row[-1] is None:
                subagents = _child_sessions(con, row[0])
    except Exception:
        return
    finally:
        if con is not None:
            con.close()
    if not row:
        _attempt_sidecar_fallback(sess)
        return
    sid, slug, agent, model_j, cost, ti, to, tr, tupd, _parent = row
    sidecar_title = titles.last_title(sid, harness="opencode")
    sidecar_summary, sidecar_summary_ts = titles.fresh_summary_with_ts(
        sid, harness="opencode")
    sess.title = sidecar_title
    if sidecar_summary:
        sess.summary = sidecar_summary
        sess.summary_ts = sidecar_summary_ts
    _attempt_sidecar_fallback(sess)
    sess.subagents = subagents
    if sid:
        sess.session_id = sid
        # F-100b — OpenCode has no derived session name either (Q-3: sqlite title/slug
        # only), so the `[xx]` badge tag is minted from the session id the same way.
        from fleet.session_handle import resolve_tag
        sess.session_tag = resolve_tag("opencode", sid)
    if slug:
        sess.slug = slug
    provider = None
    if model_j:
        try:
            mj = json.loads(model_j) or {}
            sess.model = mj.get("id") or model_j
            provider = mj.get("providerID") or None
            # opencode reasoning effort = model JSON 'variant' (e.g. high/low) — user 2026-07-01
            if mj.get("variant"):
                # A real `default` is the runtime naming its own choice, not an unset
                # dial, so it is kept OUT of `effort` (render's `_EMPTY_EFFORT` treats
                # those words as carrying no information) and recorded separately —
                # otherwise "the runtime defaulted" and "we observed nothing" render
                # as the same blank.
                if str(mj["variant"]).strip().lower() in _DEFAULT_VARIANTS:
                    sess.effort_default = True
                else:
                    sess.effort = mj.get("variant")
        except Exception:
            sess.model = model_j
    if isinstance(cost, (int, float)):
        sess.cost = cost
    toks = sum(x for x in (ti, to, tr) if isinstance(x, (int, float)))
    sess.tokens = toks or None
    sess.session_input_tokens = int(ti) if isinstance(ti, (int, float)) else None
    sess.session_output_tokens = int(to) if isinstance(to, (int, float)) else None
    sess.session_reasoning_output_tokens = int(tr) if isinstance(tr, (int, float)) else None
    sess.session_total_tokens = toks or None
    # The latest assistant request alone describes current usage. Cumulative
    # session input is cost telemetry; a catalogue window is not the applied
    # compaction boundary. Preserve an observed zero without manufacturing a %.
    if isinstance(last_ctx, (int, float)) and not isinstance(last_ctx, bool) and last_ctx >= 0:
        sess.active_context_tokens = int(last_ctx)
    from ..token_budget import context_policy_snapshot, context_policy_used_pct
    from ..context_policy import read_policy
    native_model = f"{provider or ''}/{sess.model or ''}"
    policy_record = read_policy(
        harness="opencode", session_id=sid, pid=sess.pid,
        proc_start=sess.proc_start, model=native_model)
    policy_data = policy_record.get("policy") if isinstance(policy_record, dict) else None
    policy_fresh_for_request = bool(
        policy_record and isinstance(tupd, (int, float))
        and isinstance(policy_record.get("observed_at"), (int, float))
        and policy_record["observed_at"] <= tupd / 1000.0 + 1.0)
    if isinstance(policy_data, dict) and policy_fresh_for_request:
        sess.context_policy = context_policy_snapshot(
            source=policy_data.get("source") or "opencode-native-chat-params",
            runtime_version=policy_data.get("runtime_version"),
            configured_request_tokens=policy_data.get("configured_request_tokens"),
            configured_request=policy_data.get("configured_request"),
            effective_window_tokens=policy_data.get("effective_window_tokens"),
            applied_trigger_tokens=policy_data.get("applied_trigger_tokens"),
            output_allowance_tokens=policy_data.get("output_allowance_tokens"),
            compaction_reserve_tokens=policy_data.get("compaction_reserve_tokens"),
            disabled=policy_data.get("disabled"), enabled=policy_data.get("enabled"),
            enforced=policy_data.get("enforced"), session_id=sid,
            raw_percentage=policy_data.get("raw_percentage"),
            reason=policy_data.get("reason"))
        sess.context_window_tokens = sess.context_policy["effective_window_tokens"]
    else:
        sess.context_policy = context_policy_snapshot(
            source="opencode-db", session_id=sid,
            reason="selected-request-policy-not-observed-or-mismatched")
    sess.context_display_window_tokens = (
        sess.context_policy["applied_trigger_tokens"]
        if sess.context_policy["state"] == "applied" else None)
    sess.ctx_pct = context_policy_used_pct(sess.active_context_tokens, sess.context_policy)
    if sess.ctx_pct is not None:
        cursor = (sess._refresh_source or {}).get("observed_cursor", 0)
        sess._context_evidence = ContextEvidence(
            used_pct=sess.ctx_pct, source="opencode-db-native-policy",
            sequence=(tupd or 0, cursor), source_head_sequence=(tupd or 0, cursor),
            observed_at=(tupd / 1000.0 if isinstance(tupd, (int, float)) else None),
            fresh_until=(tupd / 1000.0 + 86400 if isinstance(tupd, (int, float)) else None))
    if isinstance(tupd, (int, float)):
        sess.mtime = tupd / 1000.0                  # ms → s
    # rl_5h / rl_7d: the Go plan account quota rides the usage cache (collectors
    # __init__ account-usage loop) — nothing per-session here stays None.
