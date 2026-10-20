"""Readers for the checked-in, redacted Fleet B canonical fixture.

``load_source`` / ``load_screens`` return the frozen JSON. ``materialize`` expands the
source into real files under a directory (jobs.log, route and cycle records, completion
markers with their evidence files, peer-steward duties) so the explorer's readers can be
exercised against actual paths instead of in-memory stand-ins.
"""

import contextlib
import json
import os
import sqlite3
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


FIXTURE_DIR = Path(__file__).with_name("fixtures") / "explorer_b"
TOKEN = "$FIXTURE_ROOT"


def _read(name):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def load_source():
    return _read("source.json")


def load_screens():
    return _read("screens.json")


def load_output():
    """The reduced raw-output records behind the canonical screens (see README)."""
    return _read("output_synthetic.json")


KST = timezone(timedelta(hours=9))


def expand(value, root):
    """Replace the ``$FIXTURE_ROOT`` token with a real directory, recursively."""
    if isinstance(value, str):
        return value.replace(TOKEN, str(root))
    if isinstance(value, list):
        return [expand(item, root) for item in value]
    if isinstance(value, dict):
        return {key: expand(item, root) for key, item in value.items()}
    return value


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def jobs_log_text(registry):
    """jobs.log lines (six tab fields, pipe metadata) for the given registry rows."""
    lines = []
    for row in registry:
        pipe = ",".join(f"{key}={value}" for key, value in row["metadata"].items())
        lines.append("\t".join((row["timestamp"], row["status"], row["location"],
                                row["worktree"], row["slug"], pipe)))
    return "\n".join(lines) + "\n"


def json_route_loader(path, expect_id=None):
    """Reads a fixture route record without the hash check (the fixture keeps the captured
    ``route_hash`` while its paths are tokenized, so the recomputed hash cannot match)."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if expect_id is not None and record.get("route_id") != expect_id:
        return None
    return record


def materialize(root, source=None, drop_cycles=(), drop_routes=()):
    """Write the fixture under ``root`` and describe where everything is.

    ``drop_cycles`` / ``drop_routes`` omit those ids, to model an unreadable join record.
    """
    root = Path(root)
    source = expand(source if source is not None else load_source(), root)
    artifacts, state = root / "artifacts", root / "state"
    jobs_path = state / "jobs.log"
    state.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(jobs_log_text(source["registry"]), encoding="utf-8")
    for record in source["routes"]:
        if record["route_id"] not in drop_routes:
            _write_json(artifacts / ".runtime" / "routes" / (record["route_id"] + ".json"), record)
    for record in source["cycles"]:
        if record["cycle_id"] not in drop_cycles:
            _write_json(artifacts / ".runtime" / "artifact-producer" / "v1" / "cycles"
                        / (record["cycle_id"] + ".json"), record)
    for route_id, markers in source["markers"].items():
        for name, record in markers["files"].items():
            _write_json(state / "completion" / route_id / name, record)
        for node, text in markers["evidence"].items():
            for record in markers["files"].values():
                evidence = record.get("evidence")
                if record.get("node_id") == node and isinstance(evidence, dict) and evidence.get("path"):
                    path = Path(evidence["path"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
    for name, record in source["obligations"].items():
        _write_json(state / "peer-steward" / "obligations" / name, record)
    live_jobs = {job.get("attempt_id") or job["key"]: job for job in source["snapshot"]["jobs"]}
    raw = source["snapshot"]
    snapshot = SimpleNamespace(
        sessions=raw["sessions"], jobs=list(live_jobs.values()), pending_delivery=None,
        observations=raw.get("observations", {}), session_order=tuple(raw.get("session_order", ())),
        usage_snapshots=raw.get("usage_snapshots", {}), governor=raw.get("governor"),
        compute_hosts=raw.get("compute_hosts"), unregistered_gpu=raw.get("unregistered_gpu", ()))
    return SimpleNamespace(
        root=root, source=source, jobs_path=str(jobs_path), artifact_root=str(artifacts),
        state_roots=[str(state)], live_jobs=live_jobs, snapshot=snapshot,
        sid=source["main_session"]["sid"], now=source["now"], route_loader=json_route_loader)


def _registry_meta(fx, attempt_id):
    return next(row["metadata"] for row in fx.source["registry"] if row["metadata"].get("attempt_id") == attempt_id)


def _write_lines(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def supervisor_rows(attempt_id, session_id, cwd):
    return [{"type": "dispatch.supervisor.session", "parent_attempt_id": attempt_id,
             "session_id": session_id, "cwd": cwd},
            {"type": "dispatch.supervisor.turn-started", "parent_attempt_id": attempt_id, "turn_ordinal": 1},
            {"type": "dispatch.supervisor.turn-completed", "parent_attempt_id": attempt_id, "turn_ordinal": 1}]


def materialize_output(fx):
    """Write the raw-output sources of the fixture: the Codex attempt logs, the supervised Claude owner
    (supervisor log + native transcript under its worker home) and the cleaned b6 Claude owner (supervisor
    log only: its worker home is gone)."""
    if getattr(fx, "output_written", False):
        return fx
    from claude_project_dir import encode_project_dir
    data = expand(load_output(), fx.root)
    for attempt_id, item in data["logs"].items():
        _write_lines(_registry_meta(fx, attempt_id)["log_file"], item["records"])
    owner = data["claude_owner"]
    meta = _registry_meta(fx, owner["attempt_id"])
    row = next(r for r in fx.source["registry"] if r["metadata"]["attempt_id"] == owner["attempt_id"])
    cwd = row["worktree"]
    _write_lines(meta["log_file"], supervisor_rows(owner["attempt_id"], owner["session_id"], cwd))
    _write_lines(Path(owner["runtime_home"]) / "projects" / encode_project_dir(cwd) / (owner["session_id"] + ".jsonl"),
                 owner["records"])
    cleaned = data["cleaned_owner"]
    cmeta = _registry_meta(fx, cleaned["attempt_id"])
    crow = next(r for r in fx.source["registry"] if r["metadata"]["attempt_id"] == cleaned["attempt_id"])
    _write_lines(cmeta["log_file"], supervisor_rows(cleaned["attempt_id"], cleaned["session_id"], crow["worktree"]))
    fx.output_written = True
    return fx


@contextlib.contextmanager
def fixture_env(fx, jobs_path=None):
    """Point the shared viewer's registry lookup at the fixture (never at this machine's registry)."""
    env = {"AGENT_DISPATCH_JOBS": str(jobs_path or fx.jobs_path), "AGENT_HOME": str(Path(fx.root) / "agent-home"),
           "XDG_STATE_HOME": str(Path(fx.root) / "xdg-state"), "HOME": str(Path(fx.root) / "home")}
    with mock.patch.dict(os.environ, env):
        yield


def output_reader(**options):
    from tools.fleet.explorer.output import OutputReader
    options.setdefault("tz", KST)
    return OutputReader(**options)


def collect(fx, request_id="", stage_id="", epoch=1, sid=None, snapshot=True, output=None, expanded=(),
            generation="", **overrides):
    """One ``collect_focus`` over the materialized fixture for ``sid`` (default: the main session).

    ``output`` is an ``OutputReader`` (or True for a fresh one): without it no raw output is opened.
    """
    from tools.fleet.explorer.focus import collect_focus
    from tools.fleet.explorer.types import FocusKey, FocusRequest
    request = FocusRequest(FocusKey("claude", sid or fx.sid, request_id, stage_id, generation=generation),
                           epoch, 1, tuple(expanded))
    options = dict(jobs_path=fx.jobs_path, artifact_root=fx.artifact_root, route_loader=fx.route_loader,
                   now=fx.now, live_jobs=fx.live_jobs, state_roots=fx.state_roots,
                   snapshot=fx.snapshot if snapshot else None)
    if output:
        materialize_output(fx)
        options["output_reader"] = output_reader() if output is True else output
    options.update(overrides)
    with fixture_env(fx):
        return collect_focus(request, **options)


def tab(result, label):
    return next(item for item in result.tabs if item.label == label)


def screen_main(scene, width, first=3, count=None):
    """Main-column text (right of the nav border) of a canonical scene at ``width`` columns.

    The 100-column scenes hold the whole ledger/overview text in that column, so they are
    the exact comparison target; ``first`` is the zero-based screen row to start from.
    """
    rows = load_screens()["scenes"][scene]["widths"][str(width)]["rows"]
    lines = [row.split("\u2503", 1)[1].rstrip() for row in rows[first:] if "\u2503" in row]
    return lines if count is None else lines[:count]


def cells(text, x0, x1=None):
    """The part of ``text`` that covers terminal cells [x0, x1) (W/F characters take two)."""
    from tools.fleet.explorer.output import cell_width
    out, col = [], 0
    for char in text:
        if col >= x0 and (x1 is None or col < x1):
            out.append(char)
        col += cell_width(char)
    return "".join(out)


def scene_state(fx, scene, result):
    """The viewer state a screens.json scene stands for (labels resolved to request identities)."""
    from tools.fleet.explorer.state import ExplorerState

    def identity(label):
        return next((item.identity for item in result.tabs if item.label == label), "") if label else ""

    request = identity(scene.get("request"))
    return ExplorerState(
        selection="request:" + request if request else "", tab=identity(scene.get("tab")),
        stage=scene.get("stage", ""), active_region=scene.get("region", "nav"),
        scrolls=((f"output:{identity(scene.get('tab'))}", scene["output_scroll"]),) if "output_scroll" in scene else (),
        history=(identity(scene.get("tab")),) if scene.get("history") else (),
        keep=tuple(identity(label) for label in scene.get("keep", ())),
        tab_order=tuple(item.identity for item in result.tabs))


def scene_result(fx, scene):
    """The selected-scope result of a scene, with the fixture's events and any injected source failure."""
    import dataclasses
    from tools.fleet.explorer.types import Health

    base = collect(fx)
    labels = {item.label: item.identity for item in base.tabs}
    result = collect(fx, request_id=labels.get(scene.get("request"), ""), stage_id=scene.get("stage", ""),
                     output=bool(scene.get("output_rows")))
    result = dataclasses.replace(result, events=tuple(fx.source["events"]))
    failed = scene.get("failed_source")
    if failed:
        result = dataclasses.replace(result, sources=(
            (failed["name"], Health(state="failed", last_success_at=fx.now - failed["age_s"], error="timeout")),))
    return result


def scene_frame(fx, scene, cols, rows=45):
    """Compose one scene at ``cols`` columns; a broken-tab scene is composed after its healthy screen."""
    from tools.fleet.explorer.frame import compose

    result = scene_result(fx, scene)
    state = scene_state(fx, scene, result)
    broken = scene.get("broken_tab")
    if not broken:
        return compose(fx.snapshot, result, state, cols, rows, now=fx.now)
    healthy = compose(fx.snapshot, result, state, cols, rows, now=fx.now - broken["previous_age_s"])

    def fails(_rect):
        raise RuntimeError("tab renderer failed")

    return compose(fx.snapshot, result, state, cols, rows, providers={"ledger": fails},
                   last_good=healthy, now=fx.now)


def model_snapshot(fx):
    """The fixture snapshot with real ``Session`` / ``DispatchJob`` rows and a pid + start time on every
    process, so the s/x entry builders and the old group renderer can read it like a live one."""
    from tools.fleet.model import DispatchJob, Session
    raw = fx.snapshot
    sessions, jobs = [], []
    for index, row in enumerate(raw.sessions):
        row = dict(row)
        row["proc_start"] = row.get("proc_start") or str(1000 + index)
        sessions.append(Session(**row))
    for index, row in enumerate(raw.jobs):
        row = dict(row)
        row["key"] = row.get("key") or row.get("attempt_id") or row.get("slug")
        row["pid"] = row.get("pid") or 700000 + index
        row["proc_start"] = row.get("proc_start") or str(5000 + index)
        jobs.append(DispatchJob(**row))
    return SimpleNamespace(**{**vars(raw), "sessions": sessions, "jobs": jobs})


# -- an OpenCode owner with its database (the B-output-oc request) ------------------------------------------

def materialize_opencode(root, generation="v1", rows=None, attempt_suffix=""):
    """A one-attempt registry whose owner is OpenCode: supervisor log announcing the session and a read-only
    database of the requested generation built from ``output_synthetic.json`` (or ``rows``).

    Returns ``(jobs_path, attempt_id, session_id, database_path)``.
    """
    data = load_output()["opencode"]
    root = Path(root)
    state = root / "oc-state"
    attempt, sid = data["attempt_id"], data["session_id"]
    runtime = root / "oc-runtime"
    database = runtime / "data" / "opencode" / "opencode.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    cwd = str(root / "worktree")
    log = state / "logs" / f"oc-owner.{attempt}.opencode.jsonl"
    _write_lines(log, [{"type": "dispatch.supervisor.session", "runtime": "opencode", "parent_attempt_id": attempt,
                        "session_id": sid, "cwd": cwd}])
    meta = {"attempt_id": attempt, "harness": "opencode", "worker_type": "owner", "log_file": str(log),
            "opencode_runtime_dir": str(runtime), "parent_sid": "ses_parentfixture0", "dispatch_depth": "1"}
    pipe = ",".join(f"{key}={value}" for key, value in meta.items())
    jobs = state / "jobs.log"
    jobs.write_text("\t".join(("2026-10-13T00:32:29.740698Z", "done", cwd, cwd, "oc-owner", pipe)) + "\n", encoding="utf-8")
    write_opencode_db(database, sid, generation, rows if rows is not None else data[generation])
    return str(jobs), attempt, sid, database


def write_opencode_db(path, sid, generation, rows):
    with contextlib.closing(sqlite3.connect(path)) as con:
        if generation == "v1":
            con.execute("CREATE TABLE IF NOT EXISTS message (id TEXT, session_id TEXT, role TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS part (session_id TEXT, message_id TEXT, data TEXT)")
            for row in rows:
                con.execute("INSERT INTO message VALUES (?, ?, ?)", (row["message"], sid, row["role"]))
                con.execute("INSERT INTO part (rowid, session_id, message_id, data) VALUES (?, ?, ?, ?)",
                            (row["rowid"], sid, row["message"], json.dumps(row["data"], ensure_ascii=False)))
        else:
            con.execute("CREATE TABLE IF NOT EXISTS session_message (session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
            for row in rows:
                con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?)",
                            (sid, row["type"], row["seq"], json.dumps(row["data"], ensure_ascii=False)))
        con.commit()
