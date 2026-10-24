"""Run the committed reference-state matrix against Fleet's cell renderer."""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
WIDTHS = (80, 100, 140, 168)


def _args(state):
    nav = state["state"]
    folds = dict(nav.folds)
    folds.update(dict(nav.scroll))
    scope = nav.scope if nav.scope != "-" else None
    if scope and scope.startswith("session:"):
        scope = scope.split(":", 1)[1]
    return {"view": nav.view, "scope": scope,
            "uopen": nav.usage_open, "ui": folds}


def _frame(row):
    from types import SimpleNamespace
    return SimpleNamespace(cells=row["cells"], hits=row["hits"], cols=row["cols"], rows=row["rows"])


def _reference_values(vm):
    values = vm.to_mapping()
    ignored = {key: values.pop(key) for key in ("ordering", "readiness") if key in values}
    return values, ignored


def detect_reference_defects(frame, width, *, pane_x=34):
    """Return row/area coordinates for clipped text and hits outside their pane."""
    defects = []
    for y, row in enumerate(frame.cells):
        if len(row) > width:
            defects.append({"kind": "text-beyond-width", "row": y,
                           "area": [width, len(row)], "evidence": "cell row exceeds requested width"})
    hits = _hit_rows(frame)
    outside = [hit for hit in hits if len(hit) >= 3 and
               not (hit[0] == -1 and hit[1] == hit[2] == 0) and
               (hit[0] < 0 or hit[0] >= frame.rows or hit[1] < 0 or hit[2] > width or hit[1] >= hit[2])]
    if outside:
        defects.append({"kind": "hit-outside-screen", "row": None,
                        "area": [0, width], "evidence": outside})
    # Navigation hits are valid in the left rail. Pane actions must remain
    # inside the content pane and inside the frame.
    pane_hits = [hit for hit in hits if len(hit) >= 4 and
                 isinstance(hit[3], (tuple, list)) and hit[3] and hit[3][0] == "pane" and
                 (hit[1] < pane_x or hit[2] > width)]
    if pane_hits:
        defects.append({"kind": "hit-outside-pane", "row": None,
                        "area": [pane_x, width], "evidence": pane_hits})
    # A wide lead cell at the final column has no display space for its trail.
    for y, row in enumerate(frame.cells):
        if len(row) == width and row[-1][0] and _display_width(row[-1][0]) == 2:
            defects.append({"kind": "wide-glyph-overlap", "row": y, "area": [width - 1, width],
                            "evidence": row[-1][0]})
    return defects


def _hit_rows(frame):
    return [[hit.y, hit.x0, hit.x1, list(hit.action)] if hasattr(hit, "y") else list(hit)
            for hit in frame.hits]


def _display_width(value):
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in str(value))


def _exclusions(defects):
    return tuple((row["row"], *row["area"]) for row in defects if row.get("row") is not None)


def _diff_with_reference_exclusions(expected, actual, defects):
    from types import SimpleNamespace
    from tools.fleet.tests.parity_compare import diff_frames
    action_key = lambda action: json.dumps(action, ensure_ascii=False, sort_keys=True, default=str)
    excluded = {(hit[0], action_key(hit[3])) for defect in defects
                for hit in defect.get("evidence", ()) if len(hit) >= 4}
    def filtered(frame):
        hits = [hit for hit in _hit_rows(frame)
                if (hit[0], action_key(hit[3])) not in excluded]
        return SimpleNamespace(cells=frame.cells, hits=hits, cols=frame.cols, rows=frame.rows)
    return diff_frames(filtered(expected), filtered(actual), _exclusions(defects))


def _owner(view):
    return {"OVERVIEW": "tables.py", "ROUTES": "tables.py", "QUESTIONS": "tables.py",
            "HISTORY": "history.py", "PEERS": "peers.py", "COMPUTE": "resources.py",
            "MEMORY": "resources.py", "SYSTEM": "resources.py"}.get(view, "pages.py")


def _expand_reference_scroll_states(reference, values, states):
    """Discover scroll controls from each reference frame and add first/end positions."""
    from dataclasses import replace
    from tools.fleet.tests.freeze_explorer import capture, load_reference
    expanded = list(states)
    known = {row["id"] for row in expanded}
    for state_record in states:
        ref_module = load_reference(reference)
        from tools.fleet.tests.reference_injection import inject_view_model
        inject_view_model(ref_module, values)
        frame = capture(reference, 168, renderer=ref_module, scrub=False, **_args(state_record))
        for hit in frame["hits"]:
            action = hit[3] if len(hit) > 3 else ()
            if not action or action[0] != "scroll" or len(action) < 3:
                continue
            key, maximum = str(action[1]), max(0, int(action[2]))
            for offset in sorted({1, maximum}):
                if offset <= 0:
                    continue
                state_id = f"{state_record['id']}:scroll:{key}:{offset}"
                if state_id in known:
                    continue
                nav = replace(state_record["state"],
                              scroll=tuple(sorted(dict(state_record["state"].scroll,
                                                       **{key: str(offset)}).items())))
                expanded.append({"id": state_id, "state": nav,
                                 "fingerprint": state_record["fingerprint"]})
                known.add(state_id)
    return expanded


def run_matrix(reference, fixture, out):
    from tools.fleet.explorer.frame import draw
    from tools.fleet.explorer.view_model import load_fixture
    from tools.fleet.tests.freeze_explorer import capture, load_reference
    from tools.fleet.tests.parity_compare import diff_frames
    from tools.fleet.tests.parity_states import enumerate_states
    from tools.fleet.tests.reference_injection import inject_view_model
    reference, fixture, out = Path(reference), Path(fixture), Path(out)
    vm = load_fixture(fixture)
    reference_values, ignored_metadata = _reference_values(vm)
    inventory = load_reference(reference)
    injection = inject_view_model(inventory, reference_values)
    states = enumerate_states(reference=inventory, vm=vm)
    states = _expand_reference_scroll_states(reference, reference_values, states)
    results, reference_defects, fleet2_defects = [], [], []
    residue = defaultdict(list)
    for state_record in states:
        args = _args(state_record)
        for width in WIDTHS:
            ref_module = load_reference(reference)
            inject_view_model(ref_module, reference_values)
            ref_row = capture(reference, width, renderer=ref_module, scrub=False, **args)
            left = _frame(ref_row)
            right = draw(vm, state_record["state"], width, 45, beat=1)
            defects = detect_reference_defects(left, width) if width in (80, 100) else []
            own_defects = detect_reference_defects(right, width) if width in (80, 100) else []
            for defect in defects:
                reference_defects.append({"state": state_record["id"], "width": width, **defect})
            for defect in own_defects:
                fleet2_defects.append({"state": state_record["id"], "width": width, **defect})
            diffs = _diff_with_reference_exclusions(left, right, defects)
            result = {"state": state_record["id"], "width": width, "equal": not diffs,
                      "difference_count": len(diffs), "differences": diffs}
            results.append(result)
            if diffs:
                residue[_owner(state_record["state"].view)].append({"state": state_record["id"],
                    "width": width, "difference_count": len(diffs), "rows": sorted({
                        row["row"] for row in diffs if row.get("kind") == "cell"})})
    out.mkdir(parents=True, exist_ok=True)
    (out / "reference-defects.json").write_text(json.dumps({"schema": "fleet-reference-defects-v1",
        "widths": [80, 100], "defect_count": len(reference_defects), "defects": reference_defects},
        ensure_ascii=False, indent=2) + "\n")
    (out / "fleet2-defects.json").write_text(json.dumps({"schema": "fleet-renderer-defects-v1",
        "widths": [80, 100], "defect_count": len(fleet2_defects), "defects": fleet2_defects},
        ensure_ascii=False, indent=2) + "\n")
    equal = sum(row["equal"] for row in results)
    summary = {"schema": "fleet-parity-matrix-v1", "state_count": len(states),
        "reference_readme_state_count": 18, "widths": list(WIDTHS),
        "capture_count": len(results), "equal_count": equal,
        "different_count": len(results) - equal,
        "reference_defect_count": len(reference_defects),
        "fleet2_defect_count": len(fleet2_defects), "results": results,
        "reference_injection_unresolved": injection.get("unresolved", []),
        "reference_ignored_metadata_fields": sorted(ignored_metadata),
        "residue_by_renderer": dict(residue)}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (out / "residue-by-renderer.json").write_text(json.dumps(dict(residue), ensure_ascii=False, indent=2) + "\n")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tools/fleet/tests/fixtures/explorer/view_model.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_matrix(args.reference, args.fixture, args.out)
    print(json.dumps({key: result[key] for key in ("state_count", "capture_count", "equal_count",
                                                    "different_count", "reference_defect_count")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
