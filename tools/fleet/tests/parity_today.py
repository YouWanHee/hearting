"""Capture one private live snapshot and compare both renderers at the same moment."""
from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import sys
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
WIDTHS = (80, 100, 140, 168)
VIEWS = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE",
         "MEMORY", "MODELS", "SYSTEM")


def _jsonable(value):
    if dataclasses.is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _frame_from_reference(row):
    from types import SimpleNamespace
    return SimpleNamespace(cells=row["cells"], hits=row["hits"], cols=len(row["cells"][0]), rows=len(row["cells"]))


def _state_jobs(vm):
    data = vm.to_mapping() if hasattr(vm, "to_mapping") else vm
    repos = data.get("repos") or ()
    sessions = data.get("sessions") or ()
    routes = data.get("routes") or ()
    return (("repo", "repo:" + str((repos[0].get("id") or repos[0].get("name"))), "OVERVIEW"),
            ("session", "session:" + str(sessions[0].get("tag")), "OVERVIEW"),
            ("route", "route:" + str(routes[0].get("id")), "OVERVIEW"))


def _nav(scope, view, usage=False):
    from tools.fleet.explorer.state import NavState
    return NavState(view=view, scope=scope, title=scope.split(":", 1)[-1] if scope != "-" else view,
                    usage_open=usage)


def _capture_pair(reference, vm, state, width, rows, outdir, label):
    from tools.fleet.explorer.frame import draw
    from tools.fleet.tests.explorer_capture import draw_png
    from tools.fleet.tests.freeze_explorer import capture, load_reference
    from tools.fleet.tests.parity_compare import diff_frames
    fleet_render = importlib.import_module("tools.fleet.render")
    ref_scope = state.scope if state.scope != "-" else None
    if ref_scope and ref_scope.startswith("session:"):
        ref_scope = ref_scope.split(":", 1)[1]
    state_args = {"view": state.view, "scope": ref_scope,
                  "uopen": state.usage_open}
    ref_module = load_reference(reference)
    from tools.fleet.tests.reference_injection import inject_view_model
    values = vm.to_mapping()
    values.pop("ordering", None)
    values.pop("readiness", None)
    inject_view_model(ref_module, values)
    ref_row = capture(reference, width, renderer=ref_module, scrub=False, **state_args)
    fleet_frame = draw(vm, state, width, rows, beat=1)
    reference_frame = _frame_from_reference(ref_row)
    diffs = diff_frames(reference_frame, fleet_frame)
    label = label.replace("/", "_").replace(":", "-")
    target = outdir / f"{label}-{width}x{rows}.png"
    # Draw identical cell arrays with the Fleet palette; both images use one code path.
    draw_png(fleet_render, reference_frame, target.with_name(target.stem + "-reference.png"))
    draw_png(fleet_render, fleet_frame, target.with_name(target.stem + "-fleet2.png"))
    return {"state": label, "width": width, "rows": rows, "equal": not diffs,
            "difference_count": len(diffs), "differences": diffs[:100],
            "reference_png": str(target.with_name(target.stem + "-reference.png")),
            "fleet2_png": str(target.with_name(target.stem + "-fleet2.png"))}


def run_capture(out, reference, approved=None, source_bundle=None):
    """Collect once, persist the unredacted bundle under evidence, then replay it."""
    from tools.fleet.tests.explorer_capture import host_snapshot
    from tools.fleet.tests.reference_injection import extract_view_model, inject_view_model
    from tools.fleet.tests.freeze_explorer import load_reference
    from tools.fleet.tests.parity_compare import diff_view_models
    from tools.fleet.tests.snapshot_fixture import replay_snapshot, replay_source_bundle
    render = importlib.import_module("tools.fleet.render")
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    capture_dir = Path(out) / "evidence/parity/today" / stamp
    capture_dir.mkdir(parents=True, exist_ok=True)
    if source_bundle:
        source = Path(source_bundle)
        state_data = json.loads((source / "state.json").read_text())
        bundle, detail = replay_source_bundle(state_data, source / "history_data.json",
            source / "logs_hist.json", source / "raw_f5.json", now=now)
        payload = _jsonable(bundle)
        detail_data = _jsonable(detail)
    else:
        completed = subprocess.run(["fleet", "--json"], check=True, capture_output=True,
                                   text=True, timeout=180)
        cli_text = completed.stdout
        cli_data = json.loads(cli_text)
        (capture_dir / "fleet-json.json").write_text(cli_text, encoding="utf-8")
        snapshot, _ = replay_snapshot(cli_data, now=now)
        details = {}
        from tools.fleet.explorer.state import NavState
        for session in snapshot.sessions:
            tag = getattr(session, "tag", None) or getattr(session, "session_tag", None)
            if not tag:
                continue
            state = NavState(view="OVERVIEW", scope="session:" + str(tag), title=str(tag))
            render._explorer_compose(snapshot, 140, 45, state, now=now)
            details[str(tag)] = {"identity_available": True,
                                 "detail": _jsonable(render.explorer_read_detail(snapshot, state))}
        if not details and snapshot.sessions:
            state = NavState()
            render._explorer_compose(snapshot, 140, 45, state, now=now)
            details["unidentified-session-0"] = {
                "identity_available": False,
                "source_gap": "fleet --json omitted session_id and session_tag; history/log/raw focus reads cannot be joined",
                "detail": _jsonable(render.explorer_read_detail(snapshot, state)),
            }
        state = _nav("session:f5", "OVERVIEW")
        detail_data = (details.get("f5") or next(iter(details.values()), {}).get("detail") or {})
        snapshot, detail = replay_snapshot(cli_data, detail_bundle=detail_data, now=now)
        payload = _jsonable(snapshot)
        payload["detail"] = detail_data
        (capture_dir / "detail-reads.json").write_text(json.dumps(details, ensure_ascii=False,
            indent=2) + "\n", encoding="utf-8")
    payload["captured_at"] = now
    bundle_path = capture_dir / "snapshot-bundle.json"
    bundle_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    snapshot, detail = replay_snapshot(bundle_path, now=now)
    from tools.fleet.explorer.live import build_view_model
    vm = build_view_model(snapshot, accepted_detail=detail, now=now)
    ref = load_reference(Path(reference))
    ref_values = vm.to_mapping()
    ignored_fields = [key for key in ("ordering", "readiness") if key in ref_values]
    for key in ignored_fields:
        ref_values.pop(key)
    injection = inject_view_model(ref, ref_values)
    expected_vm = extract_view_model(ref)
    vm_diff = diff_view_models(expected_vm, vm.to_mapping())
    outputs, today_rows = [], []
    states = [(view, "-", view, False) for view in VIEWS]
    try:
        repo, session, route = _state_jobs(vm)
        states.extend((view, scope, name, False) for name, scope, view in (repo, session, route))
    except (IndexError, TypeError, AttributeError):
        states.extend((("OVERVIEW", "repo:hearting", "repo-page", False),
                       ("OVERVIEW", "session:f5", "session-page", False),
                       ("OVERVIEW", "route:f5", "route-page", False)))
    side_dir = Path(out) / "evidence/parity/side-by-side/today"
    side_dir.mkdir(parents=True, exist_ok=True)
    for view, scope, label, usage in states:
        for width in WIDTHS:
            record = _capture_pair(Path(reference), vm, _nav(scope, view, usage), width, 45,
                                   side_dir, label + ("-usage" if usage else ""))
            outputs.append(record)
            today_rows.append(record)
    (side_dir / "manifest.json").write_text(json.dumps({"schema": "fleet-side-by-side-v1",
        "source": "today host capture replayed to one live VM injected into both renderers",
        "captures": outputs}, ensure_ascii=False, indent=2) + "\n")
    (capture_dir / "diff.json").write_text(json.dumps({"schema": "fleet-today-diff-v1",
        "captured_at": now, "view_model_difference_count": len(vm_diff),
        "view_model_differences": vm_diff, "reference_injection_unresolved": injection.get("unresolved", []),
        "reference_ignored_metadata_fields": ignored_fields, "captures": outputs},
        ensure_ascii=False, indent=2) + "\n")
    if approved:
        _capture_2253(Path(out), Path(approved), Path(reference), capture_dir, now)
    return capture_dir, outputs


def _capture_2253(out, approved, reference, today_dir, now):
    from tools.fleet.tests.freeze_explorer import load_reference
    from tools.fleet.tests.snapshot_fixture import replay_source_bundle
    from tools.fleet.tests.explorer_capture import draw_png
    from tools.fleet.tests.freeze_explorer import capture
    from tools.fleet.tests.parity_compare import diff_frames
    from tools.fleet.explorer.live import build_view_model
    from tools.fleet.explorer.frame import draw
    import importlib
    source = Path(approved)
    # The cycle reference already contains the 22:53 hand VM. Keep it intact;
    # injecting an extracted copy back into its source module aliases mutable
    # palette and helper globals rather than producing an independent renderer.
    hand_vm_renderer = load_reference(reference)
    from tools.fleet.tests.reference_injection import extract_view_model
    hand_vm = extract_view_model(hand_vm_renderer)
    bundle, detail = replay_source_bundle(source / "state.json", source / "history_data.json",
        source / "logs_hist.json", source / "raw_f5.json", now=now)
    live_vm = build_view_model(bundle, accepted_detail=detail, now=now)
    render = importlib.import_module("tools.fleet.render")
    directory = Path(out) / "evidence/parity/side-by-side/2253"
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    states = [(view, "-", view) for view in VIEWS]
    try:
        states.extend((view, scope, label) for label, scope, view in _state_jobs(hand_vm))
    except (IndexError, TypeError, AttributeError):
        pass
    for view, scope, label in states:
        for width in WIDTHS:
            ref = load_reference(reference)
            ref_scope = scope.split(":", 1)[1] if scope.startswith("session:") else scope
            row = capture(reference, width, view=view, scope=ref_scope if ref_scope != "-" else None,
                          renderer=ref, scrub=False)
            from types import SimpleNamespace
            left = SimpleNamespace(cells=row["cells"], hits=row["hits"], cols=width, rows=45)
            right = draw(live_vm, _nav(scope, view), width, 45, beat=1)
            stem = directory / f"{label.replace(':', '-')}-{width}x45"
            draw_png(render, left, stem.with_name(stem.name + "-reference.png"))
            draw_png(render, right, stem.with_name(stem.name + "-fleet2.png"))
            records.append({"state": label, "width": width,
                "difference_count": len(diff_frames(left, right)),
                "reference_png": str(stem.with_name(stem.name + "-reference.png")),
                "fleet2_png": str(stem.with_name(stem.name + "-fleet2.png"))})
    (directory / "manifest.json").write_text(json.dumps({"schema": "fleet-side-by-side-v1",
        "source": "22:53 replay live VM compared with reference hand VM", "captures": records},
        ensure_ascii=False, indent=2) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--approved", type=Path)
    parser.add_argument("--source-bundle", type=Path,
                        help="optional four-file source directory; otherwise collect this host")
    args = parser.parse_args(argv)
    capture_dir, records = run_capture(args.out, args.reference, args.approved, args.source_bundle)
    print(json.dumps({"capture_dir": str(capture_dir), "capture_count": len(records),
                      "differences": sum(row["difference_count"] for row in records)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
