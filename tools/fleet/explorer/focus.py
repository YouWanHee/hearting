"""Single selection-scoped asynchronous collection boundary.

``collect_focus`` reads the selected main session's own records from disk: its registry
rows, the owner routes those rows name, the cycle records those routes' ``parent_cycle_id``
point at, completion-gate markers for the selected request, and the session's unfinished
peer-steward duties. Nothing outside the selected session is read, and no raw attempt
output is opened here (that belongs to the output reader). It never runs on the curses
thread: ``SelectionPump`` runs it through one ``RefreshPump`` (single flight, coalesced
follow-up) and publishes only a result whose focus key and epoch are still current.
"""
from collections import OrderedDict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
import time

from .output import OutputReader, read_generation, utility
from .types import AttemptRow, FocusRequest, FocusResult, Health
from ..refresh import RefreshPump

_CYCLE_ID = re.compile(r"^cyc_[0-9a-f]{32}$")
_ROUTE_ID = re.compile(r"^rt-[0-9a-f]{16}$")
_MAX_RECORD_BYTES = 256 * 1024
_MAX_OBLIGATION_FILES = 256
_MAX_CYCLE_FILES = 2048
_DONE_DUTY_STATES = frozenset(("complete", "cancelled"))


def harness_abbr(value):
    value = str(value or "").lower()
    return {"claude": "CL", "codex": "CX", "opencode": "OC"}.get(
        value, "?" if not value else value[:2].upper())


def _get(value, key, default=None):
    if value is None:
        return default
    found = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
    return default if found is None else found


class SourceCache:
    """Bounded (path, mtime_ns, size)-keyed JSON reads; an unchanged file is not read twice."""

    def __init__(self, limit=512):
        self._limit = limit
        self._lock = threading.Lock()
        self._items = OrderedDict()
        self.reads = 0

    def read_json(self, path):
        path = str(path)
        try:
            info = os.stat(path)
        except OSError:
            return None
        if info.st_size > _MAX_RECORD_BYTES:
            return None
        key = (info.st_mtime_ns, info.st_size)
        with self._lock:
            hit = self._items.get(path)
            if hit is not None and hit[0] == key:
                self._items.move_to_end(path)
                return hit[1]
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError, UnicodeError):
            record = None
        with self._lock:
            self.reads += 1
            self._items[path] = (key, record)
            self._items.move_to_end(path)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        return record


def _dispatch():
    from ..collectors import dispatch
    return dispatch


def _registry_paths(jobs_path):
    """One registry path, several, or None for the registries Fleet itself observes."""
    if jobs_path is None:
        return tuple(path for path in _dispatch()._candidate_jobs_paths() if path)
    if isinstance(jobs_path, (str, bytes, os.PathLike)):
        return (jobs_path,)
    return tuple(jobs_path)


def session_entries(jobs_path, parent_sid):
    """Every registry line (number, fields, meta) naming ``parent_sid`` as its parent, in file order."""
    dispatch = _dispatch()
    entries, number = [], 0
    for path in _registry_paths(jobs_path):
        try:
            lines = dispatch._dispatch_registry_cache.registry_view(Path(path)).lines
        except FileNotFoundError:
            continue
        for line in lines:
            number += 1
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            meta = dispatch._parse_pipe_meta(fields[5] or "")
            if meta.get("parent_sid") == parent_sid:
                entries.append((number, fields, meta))
    return entries


def read_attempts(jobs_path, parent_sid, live_jobs=None, *, entries=None):
    """Latest registry row per attempt for one main session, with Fleet's own decision/liveness."""
    dispatch = _dispatch()
    if entries is None:
        entries = session_entries(jobs_path, parent_sid)
    live = live_jobs or {}
    latest = {}
    for number, fields, meta in entries:
        latest[meta.get("attempt_id") or fields[4]] = (number, fields, meta)
    rows = []
    for key, (number, fields, meta) in latest.items():
        attempt_id = meta.get("attempt_id") or key
        job = live.get(attempt_id)
        decision = _get(job, "attempt_decision") if job is not None else None
        if decision is None:
            decision = dispatch.attempt_decision_payload(
                fields[1], meta, _get(job, "state_evidence") if job is not None else None)
        try:
            depth = int(meta.get("dispatch_depth") or 0)
        except ValueError:
            depth = 0
        rows.append(AttemptRow(
            attempt_id=attempt_id, line=number, status=fields[1], slug=fields[4],
            harness=meta.get("harness") or "", worker_type=meta.get("worker_type") or "",
            depth=depth, route_id=meta.get("route_id") or None,
            owner_route_id=meta.get("owner_route_id") or None,
            route_node=meta.get("route_node") or None, note=meta.get("note") or "",
            timestamp=fields[0], parent_sid=meta.get("parent_sid") or "",
            liveness=(_get(job, "liveness") if job is not None else None) or "unknown",
            decision=decision, meta=meta))
    return sorted(rows, key=lambda row: row.line)


EVENT_LIMIT = 24


def read_events(entries, tag=None, limit=EVENT_LIMIT):
    """The newest registry lines of the focused session as event records (what each line says, nothing more)."""
    records = []
    for number, fields, meta in entries[-limit:]:
        records.append({"time_kst": fields[0], "kind": "registry", "tag": tag,
                        "attempt_id": meta.get("attempt_id") or fields[4], "status": fields[1],
                        "route_node": meta.get("route_node") or None, "harness": meta.get("harness") or None,
                        "worker_type": meta.get("worker_type") or None, "note": meta.get("note") or None,
                        "line": number})
    return tuple(records)


def derive_artifact_root(rows):
    """The artifact root the selected session's routes live under: the root its route files name, else the env's."""
    counts = {}
    for row in rows:
        for key in ("owner_route_file", "route_file"):
            path = row.meta.get(key)
            if not path:
                continue
            parts = Path(path).parts
            if len(parts) >= 4 and parts[-2] == "routes" and parts[-3] == ".runtime":
                root = str(Path(*parts[:-3]))
                counts[root] = counts.get(root, 0) + 1
        explicit = row.meta.get("artifact_root")
        if explicit and Path(explicit, ".runtime").is_dir():
            counts[explicit] = counts.get(explicit, 0) + 1
    if counts:
        return max(counts, key=lambda root: counts[root])
    return os.environ.get("AGENT_ARTIFACT_ROOT") or ""


def _epoch(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _runtime(artifact_root, *parts):
    return Path(artifact_root, ".runtime", *parts)


def _load_routes(rows, artifact_root, route_loader, selected_rows=(), have=()):
    """Owner routes (needed for the cycle join) plus the selected request's other routes."""
    wanted = {}
    for row in rows:
        if row.worker_type == "owner":
            route_id = row.owner_route_id or row.route_id
            if route_id:
                wanted.setdefault(route_id, row)
    for row in selected_rows:
        if row.route_id:
            wanted.setdefault(row.route_id, row)
    records = {}
    for route_id, row in wanted.items():
        if route_id in have or not _ROUTE_ID.match(route_id):
            continue
        candidates = [_runtime(artifact_root, "routes", route_id + ".json")]
        for key in ("owner_route_file", "route_file"):
            if row.meta.get(key):
                candidates.append(Path(row.meta[key]))
        for path in dict.fromkeys(candidates):
            record = route_loader(str(path), expect_id=route_id)
            if record is not None:
                records[route_id] = record
                break
    return records


def _load_cycles(rows, routes, artifact_root, cache):
    """Cycle records named by owner routes; frame-only requests are found by their frame route."""
    cycles = {}
    cycle_dir = _runtime(artifact_root, "artifact-producer", "v1", "cycles")
    for route in routes.values():
        cycle_id = _get(route, "parent_cycle_id")
        if isinstance(cycle_id, str) and _CYCLE_ID.match(cycle_id) and cycle_id not in cycles:
            record = cache.read_json(cycle_dir / (cycle_id + ".json"))
            if isinstance(record, dict):
                cycles[cycle_id] = record
    proven = {_get(c, "route_id") for c in cycles.values()}
    pending = {row.route_id for row in rows
               if row.worker_type == "frame" and row.route_id and row.route_id not in proven}
    if pending:
        stamps = [e for e in (_epoch(row.timestamp) for row in rows) if e is not None]
        since = (min(stamps) - 86400) if stamps else 0
        try:
            entries = [entry for entry in os.scandir(cycle_dir) if entry.name.endswith(".json")]
        except OSError:
            entries = []
        dated = []
        for entry in entries:
            try:
                dated.append((entry.stat().st_mtime, entry.path))
            except OSError:
                continue
        for count, (mtime, path) in enumerate(sorted(dated, reverse=True)):
            if mtime < since or count >= _MAX_CYCLE_FILES or not pending:
                break
            record = cache.read_json(path)
            if isinstance(record, dict) and _get(record, "route_id") in pending:
                cycles[_get(record, "cycle_id")] = record
                pending.discard(_get(record, "route_id"))
    return cycles


def _gate(record, route_id, node, state_roots, route_module):
    if route_module.gate_mark(record, node, state_roots=state_roots):
        return "passed"
    if isinstance(record, dict) and record.get("schema_version") == 2:
        for root in state_roots:
            if os.path.exists(os.path.join(root, "completion", route_id, node + ".json")):
                return "hold"                  # a marker exists but does not prove the gate
    return None


def _normalize_duty(record, session_id, now):
    if not isinstance(record, dict) or record.get("state") in _DONE_DUTY_STATES:
        return None
    intent = record.get("intent") or {}
    identity = intent.get("identity") or record.get("identity") or {}
    if intent.get("kind") != "registered-batch" or identity.get("session_id") != session_id:
        return None
    stamp = record.get("updated_at") or record.get("accepted_at")
    try:
        age = max(0, int((now - float(stamp)) // 60))
    except (TypeError, ValueError):
        age = None
    return {"id": record.get("id"), "kind": "registered-batch",
            "attempt_id": identity.get("attempt_id"), "harness": identity.get("harness"),
            "state": record.get("state"), "cleanup": record.get("cleanup"),
            "delivery": record.get("delivery"), "age_min": age,
            "_order": float(stamp) if isinstance(stamp, (int, float)) else 0.0}


def read_obligations(state_roots, session_id, now, cache):
    """Unfinished registered-batch duties of one main session, oldest first (bounded scan)."""
    items, seen = [], set()
    for root in state_roots:
        folder = Path(root, "peer-steward", "obligations")
        try:
            entries = [e for e in os.scandir(folder)
                       if e.name.startswith("registered-batch-") and e.name.endswith(".json")
                       and not e.is_symlink()]
        except OSError:
            continue
        dated = []
        for entry in entries:
            try:
                dated.append((entry.stat().st_mtime, entry.path))
            except OSError:
                continue
        for _mtime, path in sorted(dated, reverse=True)[:_MAX_OBLIGATION_FILES]:
            duty = _normalize_duty(cache.read_json(path), session_id, now)
            if duty is not None and duty["id"] not in seen:
                seen.add(duty["id"])
                items.append(duty)
    items.sort(key=lambda item: item["_order"])
    for item in items:
        del item["_order"]
    return tuple(items)


LINEAGE_LIMIT = 12


def _card_title(module, card):
    """First line of the earlier generation's card, or ''."""
    path = (card or {}).get("path")
    if not path or (card or {}).get("status") != "available":
        return ""
    data = module.read_json(Path(path))
    body = data.get("body") if isinstance(data, dict) else None
    for line in str(body or "").splitlines():
        if line.strip():
            return line.strip()[:48]
    return ""


def read_lineage(session, harness, sid):
    """``(entries, Health)``: the earlier generations of a main session's seat (read only).

    The seat is made the way ``session_tidy`` makes it (harness, sid, cwd and, when the session
    reports one, its pane). ``entries`` is None when the lineage could not be read, never a guess.
    """
    from . import nav
    try:
        module = utility("session_tidy")
        env = {}
        pane = nav._get(session, "herdr_pane", "pane", "pane_id")
        if pane:
            env["HERDR_PANE_ID"] = str(pane)
        seat = module.resolve_seat(harness, cwd=nav._get(session, "cwd", "project"), env=env, sid=sid)
    except Exception as exc:
        return None, Health("failed", None, f"seat 없음 · {type(exc).__name__}")
    try:
        rows = module.seat_lineage(seat, sid=sid)
    except Exception as exc:
        return None, Health("failed", None, type(exc).__name__)
    entries = []
    for row in rows:
        if row.get("harness") == harness and row.get("sid") == sid:
            continue
        entries.append({"harness": row.get("harness"), "sid": row.get("sid"),
                        "source": row.get("source"), "relation": row.get("relation"),
                        "title": _card_title(module, row.get("card")),
                        "card_status": (row.get("card") or {}).get("status"),
                        "transcript": dict(row.get("transcript") or {})})
        if len(entries) >= LINEAGE_LIMIT:
            break
    return tuple(entries), Health("success", time.time())


def nav_tag(session):
    tag = _get(session, "session_tag")
    return str(tag) if tag else None


def _as_mapping(value):
    value = value() if callable(value) else value
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return {_get(job, "attempt_id"): job for job in value if _get(job, "attempt_id")}


def collect_focus(request, *, jobs_path=None, artifact_root=None, route_loader=None, now=None,
                  live_jobs=None, snapshot=None, state_roots=None, cache=None, output_reader=None,
                  earlier=False):
    """Read only the focused session's bounded source facts and optional earlier transcript."""
    from .. import route as route_module
    route_loader = route_loader or route_module.load
    cache = cache or SourceCache()
    wall = time.time() if now is None else float(now)
    if state_roots is None:
        try:
            state_roots = list(route_module._dispatch_state_roots())
        except Exception:
            state_roots = []
    live = _as_mapping(live_jobs)
    key = request.key
    entries = session_entries(jobs_path, key.sid)
    rows = read_attempts(jobs_path, key.sid, live, entries=entries)
    if not artifact_root:
        artifact_root = derive_artifact_root(rows)
    errors = []

    owner_routes = _load_routes(rows, artifact_root, route_loader)
    cycles = _load_cycles(rows, owner_routes, artifact_root, cache)
    records = dict(owner_routes)
    gates = {}

    sources = []
    output, lineage = {}, {}
    session = None
    if snapshot is not None:
        from . import nav
        session = nav.find_session(snapshot, key.sid)
    focus_ref = f"{key.harness}:{key.sid}"
    if focus_ref in tuple(getattr(request, "expanded_sessions", ()) or ()) and session is not None:
        try:
            earlier_sessions, health = read_lineage(session, key.harness, key.sid)
        except Exception as exc:                 # a lineage fault is the entry row's ``?``, nothing else
            earlier_sessions, health = None, Health("failed", None, type(exc).__name__)
        lineage = {"lineage": earlier_sessions, "lineage_health": health}
    generation = None
    if key.generation and lineage.get("lineage"):
        generation = next((item for item in lineage["lineage"]
                           if f"{item['harness']}:{item['sid']}" == key.generation), None)
    if output_reader is not None and generation is not None:
        title = ""
        try:
            view = read_generation(output_reader, generation)
            output = {"output": view.rows, "output_events": view.events,
                      "output_title": view.title, "output_notes": view.notes,
                      "output_health": view.health, "output_more": view.more}
            sources.append(("output", view.health))
        except Exception as exc:
            errors.append(f"output: {type(exc).__name__}")
            sources.append(("output", Health("failed", None, type(exc).__name__)))
            output = {"output_title": title, "output_health": Health("failed", None, type(exc).__name__)}
    try:
        obligations = read_obligations(state_roots, key.sid, wall, cache)
        sources.append(("obligations", Health("success", wall)))
    except Exception as exc:
        obligations = None
        errors.append(f"obligations: {type(exc).__name__}")
        sources.append(("obligations", Health("failed", None, type(exc).__name__)))
    events = read_events(entries, nav_tag(session))

    pane = None
    if session is not None:
        for field_name in ("herdr_pane", "pane", "pane_id"):
            pane = _get(session, field_name)
            if pane is not None:
                break
    source_records = {
        "registry_entries": tuple(entries),
        "attempts": tuple(rows),
        "routes": tuple(sorted(records.items())),
        "cycles": tuple(sorted(cycles.items())),
        "gates": tuple((route_id, node_id, value)
                       for (route_id, node_id), value in sorted(gates.items())),
        "lineage": lineage.get("lineage"),
        "pane": pane,
        "obligations": obligations,
        "output_events": tuple(output.get("output_events", ())),
        "warnings": tuple(errors),
    }
    result = FocusResult(
        key=key, epoch=request.epoch, snapshot_generation=request.snapshot_generation,
        obligations=obligations, events=events, sources=tuple(sources),
        health=Health("partial" if errors else "success", wall, "; ".join(errors) or None),
        **output, **lineage, source_records=source_records)
    return result


class FocusReader:
    """``SelectionReader`` provider with one source cache for the process lifetime."""

    def __init__(self, jobs_path=None, artifact_root=None, *, route_loader=None, live_jobs=None,
                 snapshot=None, state_roots=None, now=None, output_reader=None):
        self.jobs_path = jobs_path
        self.artifact_root = artifact_root
        self.route_loader = route_loader
        self.live_jobs = live_jobs
        self.snapshot = snapshot
        self.state_roots = state_roots
        self.now = now
        self.cache = SourceCache()
        self.output_reader = OutputReader() if output_reader is None else (output_reader or None)
        self._lock = threading.Lock()
        self._earlier = set()

    def load_earlier(self, key):
        """Ask for the records before the oldest one shown for ``key`` (read on the next pass)."""
        with self._lock:
            self._earlier.add(key)

    def __call__(self, request):
        with self._lock:
            earlier = request.key in self._earlier
            self._earlier.discard(request.key)
        return collect_focus(
            request, jobs_path=self.jobs_path, artifact_root=self.artifact_root,
            route_loader=self.route_loader, now=self.now() if callable(self.now) else self.now,
            live_jobs=self.live_jobs,
            snapshot=self.snapshot() if callable(self.snapshot) else self.snapshot,
            state_roots=self.state_roots, cache=self.cache, output_reader=self.output_reader,
            earlier=earlier)


class SelectionReader:
    def __init__(self, providers):
        self.providers = providers

    def read(self, request):
        result = self.providers(request)
        if isinstance(result, FocusResult):
            return result
        return FocusResult(request.key, request.epoch, request.snapshot_generation,
                           output=tuple(result.get("output", ())),
                           events=tuple(result.get("events", ())),
                           source_records=result.get("source_records", {}),
                           health=Health("success", time.time()))


def status_text(health, now=None):
    """The one line shown for a region fed by the pump: reading, no success yet, or last-good age."""
    now = time.time() if now is None else now
    if health.state == "loading":
        return "읽는 중"
    if health.state == "failed":
        return "성공 기록 없음" + (f" · {health.error}" if health.error else "")
    if health.state == "stale":
        age = int(max(0, now - health.last_success_at)) if health.last_success_at else 0
        return f"직전 값 {age}초 전" + (f" · {health.error}" if health.error else "")
    return ""


class SelectionPump:
    """RefreshPump-backed single flight; publish only the current focus identity."""

    _GOOD_LIMIT = 8

    def __init__(self, reader, interval=1.0, clock=time.monotonic):
        self.reader = reader if isinstance(reader, SelectionReader) else SelectionReader(reader)
        self._lock = threading.Lock()
        self._request = None
        self._epoch = 0
        self._generation = 0
        self._good = OrderedDict()                 # key -> last result published for it
        self._pump = RefreshPump(self._produce, interval, clock=clock, name="fleet-selection")

    def select(self, key, snapshot_generation=0, expanded_sessions=()):
        with self._lock:
            self._epoch += 1
            self._request = FocusRequest(key, self._epoch, snapshot_generation, tuple(expanded_sessions))
            return self._request

    def _produce(self):
        with self._lock:
            request = self._request
        if request is None:
            return None
        return self.reader.read(request)

    def request(self, force=True):
        return self._pump.request(force=force)

    def load_earlier(self):
        """Read further back in the raw output of the current selection (only when the person asks)."""
        with self._lock:
            current = self._request
        loader = getattr(self.reader.providers, "load_earlier", None)
        if current is None or loader is None:
            return False
        loader(current.key)
        self._pump.request(force=True)
        return True

    def request_due(self, now=None):
        return self._pump.request_due(now)

    def poll(self):
        """The newest result for the current focus, or None; a late result for any older
        selection (different key or epoch) is dropped, never shown."""
        update = self._pump.poll(self._generation)
        with self._lock:
            current = self._request
            if update is not None:
                self._generation, value = update
                if (value is not None and current is not None and
                        (value.key, value.epoch, value.snapshot_generation) ==
                        (current.key, current.epoch, current.snapshot_generation)):
                    self._good[current.key] = value
                    self._good.move_to_end(current.key)
                    while len(self._good) > self._GOOD_LIMIT:
                        self._good.popitem(last=False)
            return self._good.get(current.key) if current is not None else None

    def health(self, now=None):
        status = self._pump.health(now)
        return Health(status["state"], status.get("last_success_at"), status.get("last_error"))

    def status(self, now=None):
        """Per-focus state: loading (no result yet), failed (no success ever), stale, or success."""
        with self._lock:
            current = self._request
            good = self._good.get(current.key) if current is not None else None
        if current is None:
            return Health("idle")
        pump = self._pump.health(now)
        if good is not None and good.epoch == current.epoch and pump["state"] != "failed":
            return Health("success", good.health.last_success_at)
        if good is not None:
            return Health("stale", good.health.last_success_at, pump.get("last_error"))
        if pump["state"] == "failed":
            return Health("failed", None, pump.get("last_error"))
        return Health("loading")

    def stop(self, join_timeout=1.0):
        self._pump.stop(join_timeout=join_timeout)
