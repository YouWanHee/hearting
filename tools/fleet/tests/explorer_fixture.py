"""Readers for the checked-in, redacted Fleet B canonical fixture.

``load_source`` / ``load_screens`` return the frozen JSON. ``materialize`` expands the
source into real files under a directory (jobs.log, route and cycle records, completion
markers with their evidence files, peer-steward duties) so the explorer's readers can be
exercised against actual paths instead of in-memory stand-ins.
"""

import json
from pathlib import Path
from types import SimpleNamespace


FIXTURE_DIR = Path(__file__).with_name("fixtures") / "explorer_b"
TOKEN = "$FIXTURE_ROOT"


def _read(name):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def load_source():
    return _read("source.json")


def load_screens():
    return _read("screens.json")


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


def collect(fx, request_id="", stage_id="", epoch=1, sid=None, snapshot=True, **overrides):
    """One ``collect_focus`` over the materialized fixture for ``sid`` (default: the main session)."""
    from tools.fleet.explorer.focus import collect_focus
    from tools.fleet.explorer.types import FocusKey, FocusRequest
    request = FocusRequest(FocusKey("claude", sid or fx.sid, request_id, stage_id), epoch, 1)
    options = dict(jobs_path=fx.jobs_path, artifact_root=fx.artifact_root, route_loader=fx.route_loader,
                   now=fx.now, live_jobs=fx.live_jobs, state_roots=fx.state_roots,
                   snapshot=fx.snapshot if snapshot else None)
    options.update(overrides)
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
        history=(identity(scene.get("tab")),) if scene.get("history") else (),
        keep=tuple(identity(label) for label in scene.get("keep", ())),
        tab_order=tuple(item.identity for item in result.tabs))


def scene_result(fx, scene):
    """The selected-scope result of a scene, with the fixture's events and any injected source failure."""
    import dataclasses
    from tools.fleet.explorer.types import Health

    base = collect(fx)
    labels = {item.label: item.identity for item in base.tabs}
    result = collect(fx, request_id=labels.get(scene.get("request"), ""), stage_id=scene.get("stage", ""))
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
