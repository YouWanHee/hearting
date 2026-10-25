"""Capture one private live snapshot and compare both renderers at the same moment."""
from __future__ import annotations

import argparse
import hashlib
import re
import dataclasses
import importlib
import json
import sys
import subprocess
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
WIDTHS = (80, 100, 140, 168)
VIEWS = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE",
         "MEMORY", "MODELS", "SYSTEM")

# The narrow-width exceptions belong to this observed R14/v87 renderer. Runtime
# put-tracing below emits the exact changed-cell mask; these source hashes prevent
# a different reference tree from inheriting that mask accidentally.
REFERENCE_MASK_VERSION = "r14-v87-explicit-cell-mask-v1"
R14_REFERENCE_PROFILE = "mock-r14-v87"
R14_REFERENCE_MODULE_SHA256 = {
    "mock2.py": "79f93bb6c0ceb7e1d17849e994bccde01e268178f123cf445eac50c51c7b6a4e",
    "mock3.py": "ed6cbfce9f6a3fe44df9297c6963b7c662846be94e10b8096ab7d2437786d70b",
    "mock4.py": "6fa3b4aa36e93708b3d5e59ea82e16f5422a936551fa3bbfc79f2c0766e58aa1",
    "mock5.py": "9e5cde92861179368aa3e2c465cd59b2ff1163bc45240acac7107dcddd369055",
    "mock6.py": "17ce91195a69665bcbc531df3bc84076038244e18ad8d47b0da739192786a19e",
    "mock7.py": "042671dfdd570147e78da3d7e5b18e0b0ce9ae8af8dd90eb0d70f51d2250b428",
    "mock8.py": "079f75e994e36034153b771666476be054fc6e84c704e31c5b17290340f5f3fe",
    "mock9.py": "5cecf395c0530c6f20d72d0bb5b63dce3c6931ce1a4240d1f31ea59b13c188c6",
    "mock10.py": "84bb17169f969fdaef3bb55428e056666fef71be98fb5fadabc9a2e5a6992cbb",
    "mock11.py": "821af2919bc650f6a789a1f7810d8d066bb824246880c70567e3561e75917deb",
    "mock12.py": "ce8fffd59f720f5e42369c0488a9d06ba1c2dab810c7088ca7a2b90de9036eae",
    "mock13.py": "f59e426cd0cb613760d935f7487c0ef8cbe0aae0b183abfeeb832d7cbb501234",
}
_MODEL_TIERS = ("top", "deep", "balanced-deep", "balanced", "light", "mini")
R14_MODELS_HIT_MASKS = {
    80: {
        "missing": tuple((row, 71, 88, ("ui", "sel", f"{tier}/codex")) for row, tier in enumerate(_MODEL_TIERS, 6)) +
                   tuple((row, 90, 107, ("ui", "sel", f"{tier}/opencode")) for row, tier in enumerate(_MODEL_TIERS, 6)),
        "extra": tuple((row, 71, 78, ("ui", "sel", f"{tier}/codex")) for row, tier in enumerate(_MODEL_TIERS, 6)),
    },
    100: {
        "missing": tuple((row, 90, 107, ("ui", "sel", f"{tier}/opencode")) for row, tier in enumerate(_MODEL_TIERS, 6)),
        "extra": tuple((row, 90, 98, ("ui", "sel", f"{tier}/opencode")) for row, tier in enumerate(_MODEL_TIERS, 6)),
    },
}
R14_UNKNOWN_GPU_TRACK_LAST_COLUMN = {80: 78, 100: 81, 140: 81, 168: 81}


def _reference_profile(reference):
    """Return the pinned R14 profile only when every renderer module matches the cycle trace."""
    root = Path(reference)
    try:
        for name, expected in R14_REFERENCE_MODULE_SHA256.items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
                return None
    except OSError:
        return None
    return R14_REFERENCE_PROFILE


def _reference_trace_mask(reference, renderer, width, state, profile=None):
    """Trace reference puts and return a versioned mask of exact narrow reference-defect cells."""
    from tools.fleet.tests.parity_matrix import _overwrites

    if profile is None:
        profile = _reference_profile(reference)
    grid_class = renderer.mock2.Grid
    original_put = grid_class.put
    owners, mask, serial = {}, {}, [0]
    top = 4 if state.usage_open else 2

    def add(row, column, kind):
        if width not in (80, 100) or column < 33 or column >= width:
            return
        final_row = row + 1 if row >= top else row
        mask.setdefault((final_row, column), set()).add(kind)

    def traced(self, x, y, text, key=None, limit=None, style=None):
        serial[0] += 1
        call = serial[0]
        x, y = int(x), int(y)
        start_x, start_y = x, y
        right = self.W if limit is None else min(self.W, int(limit))
        rendered = str(text)
        if 0 <= y < len(self.c):
            for char in rendered:
                cells = renderer.mock2.cw(char)
                if cells <= 0:
                    continue
                if x + cells > right:
                    if right >= self.W and x < self.W and 2 <= y <= 39:
                        for column in range(max(33, x), self.W):
                            add(y, column, "text-beyond-width")
                    break
                overlaps = False
                for offset in range(cells):
                    column = x + offset
                    previous = owners.get((y, column))
                    glyph = char if offset == 0 else ""
                    if previous and previous[0] != call and _overwrites(previous[1], glyph):
                        overlaps = True
                if overlaps and 2 <= y <= 39 and x + cells - 1 >= 33:
                    for column in range(max(33, x), min(self.W, x + cells)):
                        add(y, column, "text-overlap")
                if 2 <= y <= 39 and x + cells > self.W - 1:
                    for column in range(max(33, x), min(self.W, x + cells)):
                        add(y, column, "text-beyond-pane")
                for offset in range(cells):
                    owners[(y, x + offset)] = (call, char if offset == 0 else "")
                x += cells
        return original_put(self, start_x, start_y, text, key=key, limit=limit, style=style)

    grid_class.put = traced
    try:
        from tools.fleet.tests.freeze_explorer import capture
        ref_scope = state.scope if state.scope != "-" else None
        if ref_scope and ref_scope.startswith("session:"):
            ref_scope = ref_scope.split(":", 1)[1]
        row = capture(reference, width, renderer=renderer, scrub=False, view=state.view,
                      scope=ref_scope, uopen=state.usage_open)
    finally:
        grid_class.put = original_put
    cells = [{"row": row, "column": column, "kinds": sorted(kinds)}
             for (row, column), kinds in sorted(mask.items())]
    return row, {"version": REFERENCE_MASK_VERSION, "profile": profile, "cells": cells}


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
    hits = [[hit[0], hit[1], hit[2], list(hit[3])] for hit in row["hits"]]        # actions as lists, like fleet2's
    return SimpleNamespace(cells=row["cells"], hits=hits, cols=len(row["cells"][0]), rows=len(row["cells"]))


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


def _side_by_side(render, left, right, label, path):
    """One PNG: the reference copy left, fleet2 right, both in the Fleet palette, labelled on top."""
    import tempfile
    from PIL import Image, ImageDraw
    from tools.fleet.tests.explorer_capture import draw_png
    with tempfile.TemporaryDirectory() as scratch:
        draw_png(render, left, Path(scratch) / "l.png")
        draw_png(render, right, Path(scratch) / "r.png")
        a, b = Image.open(Path(scratch) / "l.png"), Image.open(Path(scratch) / "r.png")
        gap, bar = 12, 22
        sheet = Image.new("RGB", (a.width + gap + b.width, max(a.height, b.height) + bar), (40, 44, 58))
        sheet.paste(a, (0, bar))
        sheet.paste(b, (a.width + gap, bar))
        pen = ImageDraw.Draw(sheet)
        pen.text((6, 5), "reference copy  |  " + label, fill=(230, 230, 230))
        pen.text((a.width + gap + 6, 5), "fleet2", fill=(230, 230, 230))
        sheet.save(path)


def _slug(label):
    return label.replace("/", "_").replace(":", "-")


def _capture_pair(reference, vm, state, width, rows, outdir, label, reference_profile=None):
    """Draw one state at one width from the copy (fed this view model) and from fleet2."""
    from tools.fleet.explorer.frame import draw
    from tools.fleet.tests.parity_compare import diff_frames
    from tools.fleet.tests.reference_injection import inject_live, load_isolated
    fleet_render = importlib.import_module("tools.fleet.render")
    ref_module = load_isolated(reference)
    injection = inject_live(ref_module, vm.to_mapping())
    ref_row, overflow_mask = _reference_trace_mask(reference, ref_module, width, state, reference_profile)
    fleet_frame = draw(vm, state, width, rows, beat=1)
    reference_frame = _frame_from_reference(ref_row)
    diffs = diff_frames(reference_frame, fleet_frame)
    target = outdir / f"{_slug(label)}-{width}x{rows}.png"
    _side_by_side(fleet_render, reference_frame, fleet_frame,
                  f"{label} {width}x{rows} - {len(diffs)} differences", target)
    return {"state": _slug(label), "width": width, "rows": rows, "equal": not diffs,
            "difference_count": len(diffs), "png": str(target),
            "reference_copy_limitations": injection.get("reference_copy_limitations", []),
            "reference_profile": reference_profile,
            "reference_overflow_mask": overflow_mask,
            "reference_cells": [[list(cell) for cell in row] for row in reference_frame.cells],
            "fleet2_cells": [[list(cell) for cell in row] for row in fleet_frame.cells],
            "reference_rows": ["".join(cell[0] for cell in row) for row in reference_frame.cells],
            "reference_edge": [bool(row and (row[-1][0].strip() or (row[-1][0] == "" and len(row) > 1 and row[-2][0].strip())))
                               for row in reference_frame.cells],
            "fleet2_rows": ["".join(cell[0] for cell in row) for row in fleet_frame.cells],
            "differences": diffs}


def _merged_detail(details, seats=None, config=None):
    """One ExplorerDetail from every per-session read: the focused session's reads, all sessions' seats."""
    from tools.fleet.tests.snapshot_fixture import replay_snapshot
    readable = {tag: row for tag, row in details.items() if row.get("identity_available")}
    primary = readable.get("f5") or next(iter(readable.values()), {})
    bundle = dict(primary.get("detail") or {})
    merged = {}
    for row in readable.values():
        merged.update((row.get("detail") or {}).get("seats") or {})
    merged.update(seats or {})
    bundle["seats"] = merged
    if config is not None:
        bundle["config"] = config
    return replay_snapshot({}, detail_bundle=bundle)[1]


def augment_capture(capture_dir):
    """Add what the capture's per-session reads left out: every seat's history and the model config.

    Both are read from this host (the capture's host) and written beside the capture, so a replay
    stays a replay. Existing files are kept.
    """
    from tools.fleet.explorer import nav
    from tools.fleet.explorer.focus import read_model_config, read_seat_history
    from tools.fleet.tests.snapshot_fixture import replay_source_bundle
    capture_dir = Path(capture_dir)
    seats_path, config_path = capture_dir / "seat-reads.json", capture_dir / "model-config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(_jsonable(read_model_config()), ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    if not seats_path.exists():
        cli = json.loads((capture_dir / "fleet-json.json").read_text(encoding="utf-8"))
        now = json.loads((capture_dir / "snapshot-bundle.json").read_text(encoding="utf-8")).get("captured_at")
        snapshot, _ = replay_source_bundle(cli, {}, {}, {}, now=now)
        details = json.loads((capture_dir / "detail-reads.json").read_text(encoding="utf-8"))
        panes = next((row["detail"].get("panes") for row in details.values() if row.get("detail", {}).get("panes")), {})
        seats = {}
        for session in snapshot.sessions:
            sid, harness = nav._get(session, "session_id"), nav._get(session, "harness")
            if not sid or not harness:
                continue
            pane = next((panes[i] for i in (sid, *(nav._get(session, "session_aliases") or ())) if i in panes), None)
            entries, _pane, _health = read_seat_history(session, harness, sid, cards=False, pane=pane)
            if entries is not None:
                seats[f"{harness}:{sid}"] = _jsonable(entries)
        seats_path.write_text(json.dumps(seats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_live_vm(capture_dir):
    """The one live view model both renderers draw: ``fleet --json`` replayed with every seat read."""
    from tools.fleet.explorer.live import build_view_model
    from tools.fleet.tests.snapshot_fixture import replay_source_bundle
    capture_dir = Path(capture_dir)
    cli = json.loads((capture_dir / "fleet-json.json").read_text(encoding="utf-8"))
    now = json.loads((capture_dir / "snapshot-bundle.json").read_text(encoding="utf-8")).get("captured_at")
    details_path = capture_dir / "detail-reads.json"
    details = json.loads(details_path.read_text(encoding="utf-8")) if details_path.exists() else {}
    snapshot, _ = replay_source_bundle(cli, {}, {}, {}, now=now)
    seats_path, config_path = capture_dir / "seat-reads.json", capture_dir / "model-config.json"
    seats = json.loads(seats_path.read_text(encoding="utf-8")) if seats_path.exists() else None
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else None
    # A capture can have no per-session detail reads and still contain an independent
    # model-config observation. Keep that source in the VM instead of letting the reference
    # renderer's hand-written model tables stand in for missing data.
    detail = _merged_detail(details, seats, config) if details or seats is not None or config is not None else None
    return build_view_model(snapshot, accepted_detail=detail, now=now), now


def compare_vm(vm, reference, out_dir, widths=WIDTHS, rows=45, only=None):
    """Every root view plus one repo, session and route page at every width."""
    out_dir.mkdir(parents=True, exist_ok=True)
    states = [(view, "-", view) for view in VIEWS]
    reference_profile = _reference_profile(reference)
    try:
        repo, session, route = _state_jobs(vm)
        states.extend((view, scope, name) for name, scope, view in (repo, session, route))
    except (IndexError, TypeError, AttributeError):
        pass
    records = []
    for view, scope, label in states:
        if only and label not in only:
            continue
        for width in widths:
            records.append(_capture_pair(Path(reference), vm, _nav(scope, view), width, rows, out_dir, label,
                                         reference_profile=reference_profile))
    return records


def _unique_reference_copy_limitations(records):
    """Keep narrow reference-cell limits visible without classifying them as parity exemptions."""
    unique = {}
    for record in records:
        for item in record.get("reference_copy_limitations", ()):
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            unique[key] = item
    return [unique[key] for key in sorted(unique)]


def _bands(record):
    """Group a pair's differing cells into row bands per pane, with both sides' text."""
    width, ref_rows, fleet_rows = record["width"], record["reference_rows"], record["fleet2_rows"]
    split = 33 if width >= 100 else 33
    cells, samples = {}, {}
    for diff in record["differences"]:
        if diff["kind"] != "cell":
            continue
        pane = "frame" if diff["row"] in (0, 1) or diff["row"] >= len(ref_rows) - 3 else \
            ("left" if diff["column"] < split else "right")
        cells.setdefault((pane, diff["row"]), []).append(diff["column"])
        samples.setdefault((pane, diff["row"]), []).append(diff)
    bands = []
    for (pane, row) in sorted(cells, key=lambda key: (key[0], key[1])):
        bands.append({"pane": pane, "rows": [row]})
    for band in bands:
        lo, hi = (0, split) if band["pane"] == "left" else (split, width) if band["pane"] == "right" else (0, width)
        band["cells"] = sum(len(cells[(band["pane"], row)]) for row in band["rows"])
        band["reference"] = [ref_rows[row][lo:hi].rstrip() for row in band["rows"]]
        band["fleet2"] = [fleet_rows[row][lo:hi].rstrip() for row in band["rows"]]
        found = [diff for row in band["rows"] for diff in samples[(band["pane"], row)]]
        band["text_equal"] = all((diff["expected"] or [""])[0] == (diff["actual"] or [""])[0] for diff in found)
        band["style_pairs"] = sorted({(json.dumps((d["expected"] or [None, None])[1]), json.dumps((d["actual"] or [None, None])[1]))
                                      for d in found})
        band["blank_only"] = band["text_equal"] and all((diff["expected"] or [" "])[0] in (" ", "") for diff in found)
        band["ref_edge"] = any(record["reference_edge"][row] for row in band["rows"])
        band["diffs"] = found
        band["samples"] = [{"row": diff["row"], "column": diff["column"], "reference": diff["expected"],
                            "fleet2": diff["actual"]} for diff in found[:3]]
    hits = [diff for diff in record["differences"] if diff["kind"] == "hits"]
    return bands, hits



# -- classification --------------------------------------------------------------------------------
# Every differing band of every state/width pair gets one reason. ``class`` is one of
#   fleet2-defect     fleet2 draws something the approved reference's rule or design does not (owner module named)
#   copy-limitation   the reference copy cannot draw this live value: its hand tables never held such data
#   intended          a deliberate, recorded difference
#   excluded          a 80/100-column reference self-defect (the reference itself overflows or overlaps)
# A band no rule explains stays ``unexplained`` and fails the run.

def _band_text(band):
    return "\n".join(band["reference"]), "\n".join(band["fleet2"])


def _style_is(band, reference, fleet2):
    """The band differs only in style, and only by this one reference-key to fleet2-key change."""
    return band["text_equal"] and band["style_pairs"] == [(json.dumps(reference), json.dumps(fleet2))]


def _style_in(band, pairs):
    """The band differs only in style, every change being one of ``pairs``."""
    allowed = {(json.dumps(a), json.dumps(b)) for a, b in pairs}
    return band["text_equal"] and bool(band["style_pairs"]) and set(band["style_pairs"]) <= allowed


def _collapse(text):
    return " ".join(text.split())


def _has(pattern, text):
    return re.search(pattern, text) is not None


def _answered_repo_count(text):
    """Return an answered header's explicit repo/count pair, when both are present."""
    if "ANSWERED TODAY" not in text:
        return None
    tail = _collapse(text.split("ANSWERED TODAY", 1)[1])
    match = re.match(r"([A-Za-z0-9_.-]+)\s+(\d+)\s+·\s+\d+\s+decision records", tail)
    return (match.group(1), int(match.group(2))) if match else None


def _answered_count_diff(record, band):
    if record["state"] != "QUESTIONS" or band["pane"] != "right":
        return False
    reference, fleet2 = _band_text(band)
    ref_value, fleet_value = _answered_repo_count(reference), _answered_repo_count(fleet2)
    return bool(ref_value and fleet_value and ref_value[0] == fleet_value[0]
                and ref_value[1] != fleet_value[1])


def _system_pending_diff(record, band):
    if record["state"] != "SYSTEM" or band["pane"] != "frame":
        return False
    reference, fleet2 = _band_text(band)
    pattern = r"(\d+|·) deliveries pending"
    ref_value, fleet_value = re.search(pattern, reference), re.search(pattern, fleet2)
    return bool(ref_value and fleet_value and ref_value.group(1) != fleet_value.group(1))


def _reference_overflow_mask(record, band):
    """A narrow reference exception applies only when every changed cell is traced in the R14 mask."""
    trace = record.get("reference_overflow_mask") or {}
    if (record.get("width") not in (80, 100) or record.get("reference_profile") != R14_REFERENCE_PROFILE or
            trace.get("profile") != R14_REFERENCE_PROFILE or trace.get("version") != REFERENCE_MASK_VERSION):
        return []
    masks = {(item.get("row"), item.get("column")): item.get("kinds", ())
             for item in trace.get("cells", ())}
    reference_cells, fleet2_cells = record.get("reference_cells", ()), record.get("fleet2_cells", ())
    diffs, accepted = band.get("diffs") or (), []
    if not diffs:
        return []
    for diff in diffs:
        row, column = diff.get("row"), diff.get("column")
        kinds = masks.get((row, column))
        if not kinds or row is None or column is None:
            return []
        if row >= len(reference_cells) or row >= len(fleet2_cells):
            return []
        if column >= len(reference_cells[row]) or column >= len(fleet2_cells[row]):
            return []
        if list(diff.get("expected") or ()) != list(reference_cells[row][column]):
            return []
        if list(diff.get("actual") or ()) != list(fleet2_cells[row][column]):
            return []
        accepted.append({"row": row, "column": column, "kinds": list(kinds)})
    return accepted


def _reserved_terminal_column(record, band):
    """Accept only an exact visible suffix clipped from an approved edge row."""
    width = record["width"]
    diffs = band.get("diffs") or ()
    if not diffs or any(d.get("column", -1) < width - 2 for d in diffs):
        return False
    by_row = defaultdict(list)
    for diff in diffs:
        by_row[diff["row"]].append(diff)
    if len(by_row) != 1:
        return False
    row, row_diffs = next(iter(by_row.items()))
    if band["pane"] == "frame" and record["state"] in {
            "OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE", "MEMORY", "MODELS", "SYSTEM"}:
        if row != 42 or not record["reference_rows"][row].lstrip().startswith("≋ "):
            return False
    elif band["pane"] == "right" and record["state"] == "SYSTEM":
        if row != 8 or "collectors:" not in record["reference_rows"][row]:
            return False
    else:
        return False
    reference_cells, fleet2_cells = record.get("reference_cells") or (), record.get("fleet2_cells") or ()
    if row >= len(reference_cells) or row >= len(fleet2_cells):
        return False
    columns = sorted(diff["column"] for diff in row_diffs)
    if columns != list(range(columns[0], width)):
        return False
    visible_suffix = False
    for diff in row_diffs:
        column = diff["column"]
        if column >= len(reference_cells[row]) or column >= len(fleet2_cells[row]):
            return False
        expected, actual = diff.get("expected") or (), diff.get("actual") or ()
        if list(expected) != list(reference_cells[row][column]) or list(actual) != list(fleet2_cells[row][column]):
            return False
        glyph = expected[0] if expected else None
        clipped = actual[0] if actual else None
        if clipped not in (None, "", " ") or (actual[2] if len(actual) > 2 else None) is not None:
            return False
        if isinstance(glyph, str) and glyph not in ("", " "):
            visible_suffix = True
            continue
        if glyph != "" or column == 0 or column - 1 not in columns:
            return False
        lead = reference_cells[row][column - 1][0]
        if not lead or unicodedata.east_asian_width(lead[0]) not in ("W", "F"):
            return False
    return visible_suffix


_SPINNER_GLYPHS = frozenset("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠯⠷⠻")


def _spinner_row_order(record, band):
    """Normalize only the spinner coordinate; every remaining glyph, style and row order must match."""
    if band["pane"] != "frame" or band["rows"] != [0]:
        return False
    reference = (record.get("reference_cells") or [[]])[0]
    fleet2 = (record.get("fleet2_cells") or [[]])[0]
    ref_positions = [i for i, cell in enumerate(reference) if cell and cell[0] in _SPINNER_GLYPHS]
    live_positions = [i for i, cell in enumerate(fleet2) if cell and cell[0] in _SPINNER_GLYPHS]
    if len(ref_positions) != 1 or len(live_positions) != 1:
        return False
    if reference[ref_positions[0]][1:] != fleet2[live_positions[0]][1:]:
        return False
    for row in (record.get("reference_rows") or (), record.get("fleet2_rows") or ()):
        if not row or not re.search(r"\d+ working", row[0]) or not re.search(r"hearting\s+v", row[0]) or not re.search(r"\d{2}:\d{2}", row[0]):
            return False
    return ([cell for i, cell in enumerate(reference) if i != ref_positions[0]] ==
            [cell for i, cell in enumerate(fleet2) if i != live_positions[0]])


def _unknown_compute_track(record, band):
    unknown_cells = frozenset({"UTIL", "COMPUTE block VRAM percent", "VRAM gauge", "VRAM used/total"})
    limitations = {(item.get("gpu"), frozenset(item.get("unknown_cells") or ()))
                   for item in record.get("reference_copy_limitations", ())
                   if item.get("reference_file") == "mock13.py" and item.get("host") == "moving4"}
    trace = record.get("reference_overflow_mask") or {}
    if not (record["state"] == "COMPUTE" and record["width"] in R14_UNKNOWN_GPU_TRACK_LAST_COLUMN
            and band["pane"] == "right" and len(band["rows"]) == 1 and band["rows"][0] in (5, 6)
            and record.get("reference_profile") == R14_REFERENCE_PROFILE
            and trace.get("profile") == R14_REFERENCE_PROFILE and trace.get("version") == REFERENCE_MASK_VERSION
            and limitations == {(0, unknown_cells), (1, unknown_cells)}
            and len(band["reference"]) == len(band["fleet2"]) == 1):
        return False
    reference_tail = {80: "     ·    ·", 100: "     ·    ·            ·",
                      140: "     ·    ·            ·", 168: "     ·    ·            ·"}[record["width"]]
    fleet2_tail = {80: "    ·     ·······", 100: "    ·     ··········   ·",
                   140: "    ·     ··········   ·", 168: "    ·     ··········   ·"}[record["width"]]
    row = band["rows"][0]
    gpu = row - 5
    reference, fleet2 = band["reference"][0], band["fleet2"][0]
    model = "RTX 6000 Ada"
    if f"● {gpu}" not in reference or f"● {gpu}" not in fleet2 or model not in reference or model not in fleet2:
        return False
    reference_prefix, reference_suffix = reference.split(model, 1)
    fleet2_prefix, fleet2_suffix = fleet2.split(model, 1)
    if reference_prefix != fleet2_prefix or reference_suffix != reference_tail or fleet2_suffix != fleet2_tail:
        return False
    known = {(row, 66, (" ", "dim", None), ("·", "dim", None)),
             (row, 67, ("·", "dim", None), (" ", "dim", None))}
    for column in range(73, R14_UNKNOWN_GPU_TRACK_LAST_COLUMN[record["width"]] + 1):
        known.add((row, column, (" ", None, None), ("·", "dim", None)))
    actual = {(diff["row"], diff["column"], tuple(diff.get("expected") or ()), tuple(diff.get("actual") or ()))
              for diff in band.get("diffs", ())}
    return actual == known


def _same_backgrounds(band):
    return all((diff.get("expected") or [None, None, None])[2:] ==
               (diff.get("actual") or [None, None, None])[2:] for diff in band.get("diffs", ()))


def _blank_cell_key(record, band):
    return (band["blank_only"] and _same_backgrounds(band) and
            all((diff.get("expected") or [None])[0] in (" ", "") and
                (diff.get("actual") or [None])[0] in (" ", "") for diff in band.get("diffs", ())))


def _unknown_state_glyph(record, band):
    if record["state"] != "PEERS" or not band.get("diffs") or not _same_backgrounds(band):
        return False
    for reference, fleet2 in zip(band["reference"], band["fleet2"]):
        if not re.search(r"● \[[0-9a-f]{2}\]", reference) or fleet2 != reference.replace("●", "·", 1):
            return False
    return all((diff.get("expected") or [None])[0] == "●" and
               (diff.get("actual") or [None])[0] == "·" for diff in band["diffs"])


def _peers_glyph_style(record, band):
    return (record["state"] == "PEERS" and band["pane"] == "right" and band["text_equal"] and
            bool(band.get("diffs")) and _same_backgrounds(band) and
            all((diff.get("expected") or [None])[0] in "├┤" and
                (diff.get("actual") or [None])[0] == (diff.get("expected") or [None])[0]
                for diff in band["diffs"]))


def _worker_no_effort(record, band):
    if band["pane"] != "right" or not band.get("diffs") or not _same_backgrounds(band):
        return False
    spans = {}
    for index, row in enumerate(band["rows"]):
        reference = band["reference"][index]
        fleet2 = band["fleet2"][index]
        marker = "opencode (de…"
        start = reference.find(marker)
        if start < 0 or fleet2 != reference[:start] + "opencode":
            return False
        spans[row] = (33 + start, 33 + start + len(marker))
    return all(diff["row"] in spans and spans[diff["row"]][0] <= diff["column"] < spans[diff["row"]][1]
               for diff in band["diffs"])


def _worker_row_empty(record, band):
    if band["pane"] != "right" or not band.get("diffs") or not _same_backgrounds(band):
        return False
    spans = {}
    for index, row in enumerate(band["rows"]):
        reference = band["reference"][index].rstrip()
        fleet2 = band["fleet2"][index].rstrip()
        match = re.search(r"report(\s+·(?:\s+·)?)$", reference)
        if not match or fleet2 != reference[:match.start()] + "report":
            return False
        spans[row] = (33 + match.start(1), 33 + len(reference))
    return all(diff["row"] in spans and spans[diff["row"]][0] <= diff["column"] < spans[diff["row"]][1] and
               (diff.get("expected") or [None])[0] == "·" and
               (diff.get("actual") or [None])[0] in (" ", "") for diff in band["diffs"])


def _question_empty_now(record, band):
    if record["state"] != "QUESTIONS" or not band.get("diffs") or not _same_backgrounds(band):
        return False
    if len(band["reference"]) != 1 or len(band["fleet2"]) != 1:
        return False
    reference, fleet2 = band["reference"][0].rstrip(), band["fleet2"][0].rstrip()
    return (reference.endswith("now:") and fleet2.endswith("now: ·") and
            fleet2[:-1].rstrip() == reference and
            all((diff.get("expected") or [None])[0] in (" ", "") and
                (diff.get("actual") or [None])[0] == "·" for diff in band["diffs"]))


def _hard_cut_vs_ellipsis(record, band):
    if record["width"] not in (80, 100) or band["pane"] != "right" or len(band["rows"]) != 1:
        return False
    reference, fleet2 = band["reference"][0].rstrip(), band["fleet2"][0].rstrip()
    if not (fleet2.endswith("…") and not reference.endswith("…") and reference[:20] == fleet2[:20]):
        return False
    diffs = band.get("diffs") or ()
    return (len(diffs) == 1 and (diffs[0].get("expected") or [None])[0] != "…" and
            (diffs[0].get("actual") or [None])[0] == "…")


CHROME = "tools/fleet/explorer/chrome.py"
TABLES = "tools/fleet/explorer/tables.py"
PAGES = "tools/fleet/explorer/pages.py"
PEERS = "tools/fleet/explorer/peers.py"
RES = "tools/fleet/explorer/resources.py"
MODELS_PY = "tools/fleet/explorer/models.py"

# (id, class, owner, reason, predicate(record, band))
RULES = [
    ("usage-gauge-80", "fleet2-defect", CHROME,
     "the week-usage gauge at 80% is drawn green (grp_live); the reference's pct_key() paints >= 80 red (lvl_r_flat)",
     lambda r, b: b["pane"] == "frame" and _style_is(b, "g_blocked", "grp_live")),
    ("tree-route-branch", "fleet2-defect", CHROME,
     "tree route rows: a route that is not the session's last draws the last-branch glyph (┗━ twice) and its name is "
     "not cut with an ellipsis (`route-frame`); the reference draws ┣━ and `route-fr…` inside the pane",
     lambda r, b: b["pane"] == "left" and _has("route-fr…", b["reference"][0]) and _has("route-frame", b["fleet2"][0])),
    ("sidebar-compute-gauge", "fleet2-defect", CHROME,
     "sidebar compute block: a GPU at 82% uses the plain red key (g_blocked); the reference's gauge rule uses its bold level red",
     lambda r, b: b["pane"] == "left" and _style_is(b, "g_dead", "g_blocked")),
    ("blank-cell-key", "intended", None,
     "only blank cells differ, and only in their foreground key: a space has no glyph and no background, so nothing visible differs",
     _blank_cell_key),
    ("prd-census-spinner-order", "intended", None,
     "the current PRD moves only the spinner; census, version, clock and every other styled cell match after its position is normalized",
     _spinner_row_order),
    ("right-edge-safe-clip", "intended", None,
     "fleet2 reserves the final terminal column on the shared feed and SYSTEM collector row; the frozen reference writes into that cell",
     _reserved_terminal_column),
    ("unknown-gpu-track", "copy-limitation", None,
     "moving4 GPU 0/1 UTIL and VRAM values are None: the reference copy can show · but cannot draw fleet2's empty 10-cell unknown track; no zero is synthesized",
     _unknown_compute_track),
    ("hosts-fold-100", "fleet2-defect", CHROME,
     "100 columns: when the host list does not fit before the census the reference folds it to `⌂ moving4 +4 · 5/5`; "
     "fleet2 clips the list mid-name (chrome.py draw_chrome host loop)",
     lambda r, b: r["width"] == 100 and b["pane"] == "frame" and _has(r"\+4 · 5/5", b["reference"][0])),
    ("more-below-marker", "fleet2-defect", "tools/fleet/explorer/frame.py",
     "the pane's content runs past the bottom rule: the reference writes ` ↓ more below ` on the rule (its `spilled` test); "
     "fleet2 writes it only for its own overflow test, so the marker is missing here",
     lambda r, b: b["pane"] == "right" and _has("more below", b["reference"][-1]) and not _has("more below", b["fleet2"][-1])),
    ("working-codex-model", "copy-limitation", None,
     "a working codex session's model name: the reference colours every fam_gpt model yellow (no 22:53 working codex row exists); "
     "fleet2 colours it with the harness hue like the original board (tables.py)",
     lambda r, b: _style_is(b, "g_idle", "hb_codex")),
    ("harness-mix", "fleet2-defect", TABLES,
     "`owner·worker` counts: fleet2 adds each route's worker row (`route.worker`) although the same view model's `subworker` is null "
     "and its own tree shows no depth-2 row; the reference counts the depth-2 workers it draws (tables.py _harness_mix)",
     lambda r, b: b["pane"] == "right" and _has("owner·worker", b["reference"][0]) and _has("owner·worker", b["fleet2"][0])),
    ("stage-hue", "fleet2-defect", TABLES,
     "route worker row: the step name (and, while it runs, `running …` in bold) is coloured with its stage hue in the reference (index in "
     "the route's stages); the live view model carries no worker_step_key, so fleet2 falls back to g_work_off / dim instead of deriving "
     "the hue (tables.py worker row)",
     lambda r, b: b["pane"] == "right" and _style_in(
         b, [(k, "g_work_off") for k in ("grp_live", "version_release", "hb_claude", "h_claude")] + [("g_work", "dim")])),
    ("unknown-bold-title", "fleet2-defect", TABLES,
     "progress rail: the current stage's name takes its colour from a table that has no entry for stage index 0 and 1 "
     "(tables.py:501 falls back to the unknown key); the reference colours every stage by its hue",
     lambda r, b: b["text_equal"] and _style_is(b, "hb_claude", "unknown")),
    ("worker-effort-max", "fleet2-defect", TABLES,
     "worker harness cell folded to `harness·effort`: fleet2 drops the effort when it is `max`; the reference keeps every effort in its EFFORTS list",
     lambda r, b: b["pane"] == "right" and any(_has(r"claude·max", x) for x in b["reference"]) and
     not any(_has(r"claude·max", x) for x in b["fleet2"][-1:]) and any(_has("worker", x) for x in b["reference"])),
    ("worker-no-effort", "copy-limitation", None,
     "a worker harness cell with a model but no effort (opencode): the reference's narrow fold only handles the six-part cell, so it clips "
     "`opencode (de…`; fleet2 shows the harness name alone",
     _worker_no_effort),
    ("worker-row-empty", "copy-limitation", None,
     "a route with no worker record: the reference always draws a worker line and fills it with `·`; fleet2 leaves the cells empty",
     _worker_row_empty),
    ("unknown-context-gauge-peers", "fleet2-defect", PEERS,
     "PEERS card for a session whose context is unknown: fleet2 draws an empty gauge (`────────`); its OVERVIEW draws nothing "
     "for the same unknown context (tables.py), as the reference does",
     lambda r, b: r["state"] == "PEERS" and any(_has(r"│ ────────", x) and not _has(r"│ ────────", y)
                                                for x, y in zip(b["fleet2"], b["reference"]))),
    ("unknown-state-glyph", "copy-limitation", None,
     "a session whose state is `unknown`/`stale`: the reference has five session states and draws these as an idle dot; fleet2's PEERS card "
     "draws `·` for them (peers.py STATE_MARKS fallback) while its tree and OVERVIEW draw the idle dot",
     _unknown_state_glyph),
    ("peers-glyph-style", "copy-limitation", None,
     "PEERS lane/link junction colour: the reference colours a lane by the upper/lower session's harness hue; the live pair's link "
     "colour in fleet2 follows the other session",
     _peers_glyph_style),
    ("answered-head", "fleet2-defect", TABLES,
     "ANSWERED TODAY shows a different count for the same explicit repo in the reference and Fleet2",
     _answered_count_diff),
    ("question-empty-now", "copy-limitation", None,
     "a waiting question whose `now` text is empty: the reference draws nothing after `now:` (all 22:53 questions carried text); fleet2 draws `·`",
     _question_empty_now),
    ("compute-command-args", "fleet2-defect", RES,
     "COMPUTE process line: fleet2 cuts the command at ` --` (`script.py`); the reference keeps the script and its short options "
     "(`script.py --request A2.json`, its short_cmd())",
     lambda r, b: r["state"] == "COMPUTE" and b["pane"] == "right" and all(
         _collapse(re.sub(r" --.*?(?=\s+\d+(\.\d)? GB)", "", x)) == _collapse(y) for x, y in zip(b["reference"], b["fleet2"]))),
    ("session-context-line", "fleet2-defect", PAGES,
     "session page context line: with an unknown last-clear time fleet2 drops the `· last clear ·` clause that its own seat line "
     "(and the reference's context line) print",
     lambda r, b: r["state"] == "session" and any(_has("context:", x) for x in b["reference"])),
    ("hard-cut-vs-ellipsis", "fleet2-defect", PAGES,
     "a seat/field line longer than the pane: the reference cuts it hard at the pane edge, fleet2 ends it with `…`",
     _hard_cut_vs_ellipsis),
    ("route-page-stage-line", "fleet2-defect", PAGES,
     "route page leg box: fleet2 prints `stage <name> n/m` above the owner line; the reference's leg box lists the owner line first and puts "
     "`stage` second",
     lambda r, b: r["state"] == "route" and any(_has(r"owner +codex", x) for x in b["reference"]) and any(_has("stage", x) for x in b["fleet2"])),
    ("models-long-name-clip", "fleet2-defect", MODELS_PY,
     "MODELS cell: a model name longer than the column is cut with an ellipsis in the reference (`muse-spark-1.3-cont…`); fleet2 cuts it without one",
     lambda r, b: r["state"] == "MODELS" and any(_has("cont…", x) for x in b["reference"])),
    ("models-main-only-tuple", "fleet2-defect", MODELS_PY,
     "MODELS `main session only` line prints the Python tuple (`claude ('fable',)`) where the reference prints the model name",
     lambda r, b: r["state"] == "MODELS" and any(_has(r"\('", x) for x in b["fleet2"])),
    ("models-tidy-bar", "fleet2-defect", MODELS_PY,
     "CONTEXT & TIDY bar: the live model config carries no tidy points (`tidy` is null; the reference keeps 500K/700K, a code constant in "
     "context_alert.py), so fleet2 paints the whole bar red (g_blocked) while its own chips still show 500K/700K as current "
     "(models.py:80-84)",
     lambda r, b: r["state"] == "MODELS" and _style_in(b, [("g_work_off", "g_blocked"), ("g_idle", "g_blocked")])),
    ("system-status", "fleet2-defect", CHROME,
     "SYSTEM status line shows an observed pending-delivery count differently between renderers",
     _system_pending_diff),
    ("gauge-min-one-cell", "fleet2-defect", TABLES,
     "a small percentage (6%) on a 6-cell gauge: the reference's gauge() always fills at least one cell for a value above 0; "
     "fleet2 rounds it to an empty gauge",
     lambda r, b: b["pane"] == "right" and any(_has(r"━─{5}\s+\d%", x) and _has(r"─{6}\s+\d%", y)
                                              for x, y in zip(b["reference"], b["fleet2"]))),
]


def _hit_delta(expected, actual):
    expected_counts = Counter(json.dumps(hit, ensure_ascii=False, separators=(",", ":")) for hit in expected)
    actual_counts = Counter(json.dumps(hit, ensure_ascii=False, separators=(",", ":")) for hit in actual)
    missing = list((expected_counts - actual_counts).elements())
    extra = list((actual_counts - expected_counts).elements())
    return ([json.loads(hit) for hit in missing], [json.loads(hit) for hit in extra])


def _r14_hit_mask_matches(record, expected_hits, actual_hits, missing, extra):
    trace = record.get("reference_overflow_mask") or {}
    if (record.get("state") != "MODELS" or record.get("reference_profile") != R14_REFERENCE_PROFILE or
            trace.get("profile") != R14_REFERENCE_PROFILE or trace.get("version") != REFERENCE_MASK_VERSION):
        return False
    expected = R14_MODELS_HIT_MASKS.get(record.get("width"))
    if expected is None:
        return False
    key = lambda hit: json.dumps(hit, ensure_ascii=False, separators=(",", ":"))
    missing_mask, extra_mask = expected["missing"], expected["extra"]
    if (Counter(map(key, missing)) != Counter(map(key, missing_mask)) or
            Counter(map(key, extra)) != Counter(map(key, extra_mask))):
        return False
    paired_actions = {key(hit[3]) for hit in missing_mask} & {key(hit[3]) for hit in extra_mask}

    def normalize(sequence, masks):
        counts = Counter(map(key, masks))
        normalized = []
        for hit in sequence:
            ident = key(hit)
            if counts[ident]:
                counts[ident] -= 1
                action = key(hit[3])
                if action in paired_actions:
                    normalized.append(("masked-hit", action))
            else:
                normalized.append(("hit", ident))
        return normalized

    return normalize(expected_hits, missing_mask) == normalize(actual_hits, extra_mask)


def classify(records):
    """Classify only exact rules, source-traced reference cells and the versioned R14 hit mask."""
    entries, counts = [], {}
    for record in records:
        bands, hits = _bands(record)
        for band in bands:
            matched = []
            selected = None
            masked_cells = _reference_overflow_mask(record, band)
            if masked_cells:
                rule_id, cls, owner = "r14-reference-overflow-mask", "excluded", None
                reason = ("every differing cell is inside the versioned R14 reference put-trace mask; "
                          "cells and styles outside those exact coordinates remain compared")
            else:
                for rule in RULES:
                    try:
                        if rule[4](record, band):
                            matched.append(rule)
                    except (KeyError, IndexError, TypeError):
                        continue
                if matched:
                    selected = matched[0]
                    rule_id, cls, owner, reason = selected[:4]
                else:
                    selected = None
                    rule_id, cls, owner, reason = "unexplained", "unexplained", None, "no rule explains this band"
            entry = {"state": record["state"], "width": record["width"], "kind": "cells", "pane": band["pane"],
                     "rows": [band["rows"][0], band["rows"][-1]], "cells": band["cells"],
                     "difference_cells": [[diff["row"], diff["column"]] for diff in band.get("diffs", ())],
                     "text_equal": band["text_equal"], "rule": rule_id, "class": cls, "owner": owner,
                     "also": [rule[0] for rule in matched if rule is not selected] if matched else [],
                     "reason": reason, "reference": band["reference"][:3], "fleet2": band["fleet2"][:3]}
            if masked_cells:
                entry["reference_mask_cells"] = masked_cells
            entries.append(entry)
            counts[cls] = counts.get(cls, 0) + 1
        if hits:
            expected, actual = hits[0]["expected"], hits[0]["actual"]
            missing, extra = _hit_delta(expected, actual)
            if _r14_hit_mask_matches(record, expected, actual, missing, extra):
                cls, rule_id = "excluded", "r14-models-overflow-hit-mask"
                reason = "the ordered hit changes exactly match the versioned R14 MODELS overflow ledger"
            else:
                cls, rule_id = "unexplained", "unexplained"
                reason = "the ordered click map differs outside the exact R14 reference-hit mask"
            entries.append({"state": record["state"], "width": record["width"], "kind": "hits", "rule": rule_id,
                            "class": cls, "owner": None, "reason": reason,
                            "differing_hit_areas": (missing + extra)[:20]})
            counts[cls] = counts.get(cls, 0) + 1
    return {"schema": "fleet-today-classified-v1", "reference_mask_version": REFERENCE_MASK_VERSION,
            "summary": {"bands": len(entries), "by_class": counts,
                        "unexplained": counts.get("unexplained", 0)}, "entries": entries}


def write_classified(document, out_dir, capture_name):
    """classified.json plus a readable classified.md: fleet2 defects first (the owner's list), then the rest."""
    out_dir = Path(out_dir)
    (out_dir / "classified.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    by_rule = {}
    for entry in document["entries"]:
        row = by_rule.setdefault(entry["rule"], {"class": entry["class"], "owner": entry.get("owner"),
                                                 "reason": entry["reason"], "where": []})
        row["where"].append(f"{entry['state']}@{entry['width']}" + (f" rows {entry['rows'][0]}-{entry['rows'][1]}"
                                                                    if entry.get("rows") else " hits"))
    lines = [f"# Classified differences - {capture_name}", "",
             f"{document['summary']['bands']} differing bands/hit sets; unexplained: {document['summary']['unexplained']}.",
             f"By class: {json.dumps(document['summary']['by_class'], ensure_ascii=False)}", ""]
    for cls, title in (("fleet2-defect", "fleet2 renderer defects (for the owner)"), ("copy-limitation", "Reference-copy limitations"),
                       ("intended", "Intended"), ("excluded", "Excluded 80/100 reference self-defects"),
                       ("derived", "Derived"), ("unexplained", "Unexplained")):
        rules = {key: value for key, value in by_rule.items() if value["class"] == cls}
        if not rules:
            continue
        lines += [f"## {title}", ""]
        for key, value in sorted(rules.items()):
            where = ", ".join(sorted(set(value["where"]))[:12]) + (" ..." if len(set(value["where"])) > 12 else "")
            owner = f" - owner `{value['owner']}`" if value.get("owner") else ""
            lines += [f"### {key}{owner}", "", value["reason"], "", f"Seen in {len(value['where'])} place(s): {where}", ""]
    (out_dir / "classified.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_capture(out, reference, approved=None, source_bundle=None, from_capture=None):
    """Collect once (or replay ``from_capture``), then draw one live view model through both renderers."""
    from tools.fleet.tests.explorer_capture import host_snapshot
    from tools.fleet.tests.snapshot_fixture import replay_snapshot, replay_source_bundle
    render = importlib.import_module("tools.fleet.render")
    now = time.time()
    if from_capture:
        capture_dir = Path(from_capture)
        augment_capture(capture_dir)
        vm, now = build_live_vm(capture_dir)
    else:
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        capture_dir = Path(out) / "evidence/parity/today" / stamp
        capture_dir.mkdir(parents=True, exist_ok=True)
        _collect(capture_dir, render, now, source_bundle)
        augment_capture(capture_dir)
        vm, now = build_live_vm(capture_dir)
    side_dir = Path(out) / "evidence/parity/side-by-side/today"
    records = compare_vm(vm, reference, side_dir)
    copy_limitations = _unique_reference_copy_limitations(records)
    summary = [{key: row[key] for key in ("state", "width", "rows", "equal", "difference_count", "png")}
               for row in records]
    (side_dir / "manifest.json").write_text(json.dumps({"schema": "fleet-side-by-side-v1",
        "source": f"today host capture {capture_dir.name}: one live view model injected into the copy and drawn by fleet2",
        "reference_copy_limitations": copy_limitations,
        "captures": summary}, ensure_ascii=False, indent=2) + "\n")
    (capture_dir / "diff.json").write_text(json.dumps({"schema": "fleet-today-diff-v1", "captured_at": now,
        "reference_copy_limitations": copy_limitations, "captures": summary}, ensure_ascii=False, indent=2) + "\n")
    document = classify(records)
    document["reference_copy_limitations"] = copy_limitations
    write_classified(document, capture_dir, capture_dir.name)
    if approved:
        _capture_2253(Path(out), Path(approved), Path(reference), capture_dir, now)
    return capture_dir, records


def _collect(capture_dir, render, now, source_bundle):
    """Collect this host once (``fleet --json`` plus every session's detail reads) into ``capture_dir``."""
    from tools.fleet.explorer.state import NavState
    from tools.fleet.tests.snapshot_fixture import replay_snapshot
    completed = subprocess.run(["fleet", "--json"], check=True, capture_output=True, text=True, timeout=180)
    cli_data = json.loads(completed.stdout)
    (capture_dir / "fleet-json.json").write_text(completed.stdout, encoding="utf-8")
    snapshot, _ = replay_snapshot(cli_data, now=now)
    details = {}
    for session in snapshot.sessions:
        tag = getattr(session, "tag", None) or getattr(session, "session_tag", None)
        if not tag:
            continue
        state = NavState(view="OVERVIEW", scope="session:" + str(tag), title=str(tag))
        render._explorer_compose(snapshot, 140, 45, state, now=now)
        details[str(tag)] = {"identity_available": True,
                             "detail": _jsonable(render.explorer_read_detail(snapshot, state))}
    payload = _jsonable(snapshot)
    payload["captured_at"] = now
    (capture_dir / "detail-reads.json").write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (capture_dir / "snapshot-bundle.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _capture_2253(out, approved, reference, today_dir, now):
    """The 22:53 pair: the approved data drawn by the reference (left, width-scrubbed as the goldens are) and by
    fleet2 from the production adapter's view model of the same four source files (right)."""
    from tools.fleet.explorer.frame import draw
    from tools.fleet.explorer.view_model import ExplorerViewModel
    from tools.fleet.tests.freeze_explorer import capture
    from tools.fleet.tests.parity_compare import (_nav_state, _state_args, diff_frames, live_ledger,
                                                  overlay_view_model)
    from tools.fleet.tests.reference_injection import extract_view_model, load_isolated
    _ledger, _private, live = live_ledger(reference)
    hand_vm = extract_view_model(load_isolated(approved), scrub=True)
    # fleet2 draws the adapter's view model with the hand values the ledger classifies (class 2/3, no real source)
    # kept from the reference, so a remaining difference is the renderer's, never a missing source
    merged = {key: value for key, value in overlay_view_model(hand_vm, live).items() if not key.startswith("_")}
    overlay_vm = ExplorerViewModel.from_mapping(merged)
    live_vm = ExplorerViewModel.from_mapping(live)
    render = importlib.import_module("tools.fleet.render")
    directory = Path(out) / "evidence/parity/side-by-side/2253"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((Path(__file__).parent / "fixtures/explorer/manifest.json").read_text())
    records = []
    for label in manifest["states"]:
        for width in WIDTHS:
            args = _state_args(label)
            if label == "repo-page":
                args["scope"] = "repo:xxxxxxxx"            # the scrubbed repo id, as the goldens use
            row = capture(approved, width, renderer=load_isolated(approved), scrub=True, **args)
            left = _frame_from_reference(row)
            right = draw(overlay_vm, _nav_state(label), width, 45, beat=1)
            count = len(diff_frames(left, right))
            plain = len(diff_frames(left, draw(live_vm, _nav_state(label), width, 45, beat=1)))
            target = directory / f"{_slug(label)}-{width}x45.png"
            _side_by_side(render, left, right, f"{label} {width}x45 - {count} differences", target)
            records.append({"state": _slug(label), "width": width, "difference_count": count,
                            "difference_count_without_overlay": plain, "png": str(target)})
    (directory / "manifest.json").write_text(json.dumps({"schema": "fleet-side-by-side-v1",
        "source": "22:53 approved data: the reference (left, width-scrubbed) beside fleet2 drawing the production adapter's "
                  "view model with the hand-only values kept from the reference (right)",
        "captures": records}, ensure_ascii=False, indent=2) + "\n")
    return records


def write_index(out):
    """One index of every side-by-side PNG, grouped by data set."""
    base = Path(out) / "evidence/parity/side-by-side"
    index, lines = {}, ["# Side-by-side PNGs", "",
                        "Reference copy on the left, fleet2 on the right, both drawn in the Fleet palette.", ""]
    for group in ("today", "2253"):
        manifest = base / group / "manifest.json"
        if not manifest.exists():
            continue
        data = json.loads(manifest.read_text(encoding="utf-8"))
        rows = [{"state": row["state"], "width": row["width"], "difference_count": row.get("difference_count"),
                 "png": str(Path(row["png"]).relative_to(base))} for row in data["captures"]]
        index[group] = {"source": data["source"], "captures": rows}
        lines += [f"## {group}", "", data["source"], "", "| state | width | differences | image |", "|---|---|---|---|"]
        lines += [f"| {row['state']} | {row['width']} | {row['difference_count']} | {row['png']} |" for row in rows]
        lines.append("")
    (base / "index.json").write_text(json.dumps({"schema": "fleet-side-by-side-index-v1", "groups": index},
                                                ensure_ascii=False, indent=2) + "\n")
    (base / "index.md").write_text("\n".join(lines) + "\n")
    return index


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--approved", type=Path)
    parser.add_argument("--source-bundle", type=Path,
                        help="optional four-file source directory; otherwise collect this host")
    parser.add_argument("--from-capture", type=Path,
                        help="replay an existing capture directory (fleet-json.json, detail-reads.json, "
                             "snapshot-bundle.json) instead of collecting this host")
    parser.add_argument("--round-trip", action="store_true",
                        help="only check that the copy fed the approved data still draws the approved frames")
    args = parser.parse_args(argv)
    if args.round_trip:
        from tools.fleet.tests.reference_injection import round_trip
        result = round_trip(args.approved, args.reference)
        print(json.dumps({"states": result["state_count"], "differences": result["diff_count"]}))
        return 0 if result["diff_count"] == 0 else 1
    capture_dir, records = run_capture(args.out, args.reference, args.approved, args.source_bundle,
                                       args.from_capture)
    write_index(args.out)
    strict = classify(records)
    strict_pass = (strict["summary"]["unexplained"] == 0 and
                   strict["summary"]["by_class"].get("fleet2-defect", 0) == 0)
    print(json.dumps({"capture_dir": str(capture_dir), "capture_count": len(records),
                      "differences": sum(row["difference_count"] for row in records),
                      "strict_parity": strict["summary"], "strict_pass": strict_pass,
                      "reference_copy_limitations": _unique_reference_copy_limitations(records)}))
    return 0 if strict_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
