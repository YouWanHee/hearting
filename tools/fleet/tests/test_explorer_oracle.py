"""Cell and ordered-hit comparison against the approved, redacted reference captures."""
import json
import os
import unittest
from collections.abc import Mapping
from pathlib import Path

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import load_fixture

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
APPROVED = os.environ.get("FLEET_EXPLORER_REFERENCE_DIR")
EVIDENCE = os.environ.get("FLEET_EXPLORER_EVIDENCE_DIR")


def expand_rows(rows):
    return [[cell for glyph, colour, tint, count in row
             for cell in [[glyph, colour, tint]] * count] for row in rows]


def normalize_new_tree_viewport_row(actual_cells, expected_cells, actual_hits, expected_hits,
                                   tree_scroll):
    """Apply only the new scroll help row and one newly visible r14 tree row."""
    help_y = len(actual_cells) - 2
    help_text = "".join(str(cell[0] or "") for cell in actual_cells[help_y]).rstrip()
    expected_help = (" ↑↓ move · ←→ focus · PgUp/Dn·Home/End·wheel scroll · "
                     "⏎ pane · Tab view · / filter · u usage · F feed · q quit")
    if help_text != expected_help:
        raise AssertionError(f"scroll help row changed outside the approved text: {help_text!r}")
    normalized_cells = [list(row) for row in expected_cells]
    normalized_cells[help_y] = actual_cells[help_y]
    if len(tree_scroll) < 8 or int(tree_scroll[5]) <= 0:
        return normalized_cells, expected_hits
    _x0, _top, width, bottom, offset, _maximum, _total, _visible = tree_scroll[:8]
    boundary = int(bottom) - 1
    if int(offset) != 0 or not 0 <= boundary < len(actual_cells):
        raise AssertionError("the frozen r14 row delta applies only to the initial tree viewport")
    additions = [hit for hit in actual_hits if hit not in expected_hits]
    if (len(additions) != 1 or additions[0][0] != boundary or
            additions[0][3][:1] not in (["scope"], ["route"])):
        raise AssertionError(f"expected exactly one newly visible tree-row hit: {additions!r}")
    if any(hit[0] == boundary and hit[3][:1] in (["scope"], ["route"])
           for hit in expected_hits):
        raise AssertionError("the frozen r14 oracle already contains a hit on the expanded row")
    normalized_cells[boundary][:int(width)] = actual_cells[boundary][:int(width)]
    normalized_hits = list(expected_hits)
    compute_index = next((index for index, hit in enumerate(normalized_hits)
                          if hit[3][:2] == ["root", "COMPUTE"]), None)
    if compute_index is None:
        raise AssertionError("the frozen frame has no COMPUTE summary hit")
    normalized_hits.insert(compute_index, additions[0])
    return normalized_cells, normalized_hits


def _fixture_header_identity(vm):
    source = vm.hearting if isinstance(vm.hearting, Mapping) else {}
    install = vm.system.get("install", {}) if isinstance(vm.system, Mapping) else {}
    version = source.get("version") or install.get("version") or "unknown"
    method = source.get("install_method") or "unmanaged"
    return str(version), str(method)


def normalize_f70_method_delta(actual_cells, expected_cells, *, version, method):
    """Normalize only the added F70 method, then require every frozen tuple to match."""
    width = len(expected_cells[0]) if expected_cells else 0
    if width not in (140, 168) or len(actual_cells) != len(expected_cells) or not actual_cells:
        return actual_cells
    row = actual_cells[0]
    if len(row) != width or any(len(cell) < 3 or len(str(cell[0] or "")) != 1 for cell in row):
        raise AssertionError("F70 oracle row must contain only single-cell header tuples")
    text = "".join(str(cell[0] or "") for cell in row)
    identity = f"hearting {version} · {method}"
    if text.count(identity) != 1:
        raise AssertionError("candidate header must contain its exact F70 product/version/method identity")
    start = text.index("hearting")
    version_start = start + len("hearting ")
    method_start = version_start + len(version)
    method_text = " · " + method
    method_end = method_start + len(method_text)
    if text[version_start:method_start] != version or text[method_start:method_end] != method_text:
        raise AssertionError("candidate F70 field boundaries do not match the validated identity")
    expected_styles = (
        (start, len("hearting"), "hearting_name"),
        (version_start, len(version), "version_release"),
        (method_start, len(" · "), "dim"),
        (method_start + len(" · "), len(method), "version_method"),
    )
    for begin, count, style in expected_styles:
        if any(tuple(row[index][1:3]) != (style, None) for index in range(begin, begin + count)):
            raise AssertionError(f"candidate F70 field has an unexpected style: {style}")
    if row[start + len("hearting")][1] != "version_release":
        raise AssertionError("brand/version separator must keep its existing version style")
    spinners = [index for index, cell in enumerate(row)
                if str(cell[0] or "") in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠯⠷⠻"]
    if len(spinners) != 1 or not 0 < spinners[0] < method_start:
        raise AssertionError("header must retain its single existing census spinner")

    # Right-alignment moves only the pre-method census group left by the inserted method width.
    inserted = len(method_text)
    normalized_row = (row[:spinners[0]] + [[" ", None, None]] * inserted +
                      row[spinners[0]:method_start] + row[method_end:])
    if len(normalized_row) != width:
        raise AssertionError("F70 normalization changed the physical header width")
    normalized = [list(cells) for cells in actual_cells]
    normalized[0] = normalized_row
    return normalized


class CoreReference(unittest.TestCase):
    def test_freezer_matches_unscrubbed_raw_reference(self):
        if not APPROVED or not Path(APPROVED).is_dir():
            self.skipTest("set FLEET_EXPLORER_REFERENCE_DIR to run the raw reference comparison")
        if not EVIDENCE or not Path(EVIDENCE).is_dir():
            self.skipTest("set FLEET_EXPLORER_EVIDENCE_DIR to run the evidence comparison")
        from tools.fleet.tests.freeze_explorer import check_raw_reference
        report = check_raw_reference(Path(APPROVED), Path(EVIDENCE) / "w2-frame" /
                                     "freezer-vs-reference.json")
        self.assertEqual(report["comparisons"], 36)
        self.assertEqual(report["difference_count"], 0, report["differences"])

    def test_overview_exact_reference_140_168(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            with self.subTest(cols=cols):
                expected = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
                expected["cells"] = expand_rows(expected["cells"])
                actual = draw(vm, NavState(), cols, 45, beat=1)
                cells = [[list(cell) for cell in row] for row in actual.cells]
                hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
                version, method = _fixture_header_identity(vm)
                compared = normalize_f70_method_delta(cells, expected["cells"], version=version, method=method)
                expected_cells, expected_hits = normalize_new_tree_viewport_row(
                    compared, expected["cells"], hits, expected["hits"], actual.tree_scroll)
                if compared != expected_cells:
                    first = next((f"row={y} col={x} expected={expected_cells[y][x]!r} actual={compared[y][x]!r}"
                                  for y in range(min(len(compared), len(expected_cells)))
                                  for x in range(min(len(compared[y]), len(expected_cells[y])))
                                  if compared[y][x] != expected_cells[y][x]), "dimensions differ")
                    self.fail(f"first cell mismatch at {cols} columns: {first}")
                self.assertEqual(hits, expected_hits, f"ordered hit mismatch at {cols} columns")


class ListReference(unittest.TestCase):
    def test_routes_questions_and_history_exact_reference(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            expected_file = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            for view in ("ROUTES", "QUESTIONS", "HISTORY"):
                with self.subTest(cols=cols, view=view):
                    expected = expected_file["list_states"][view]
                    actual = draw(vm, NavState(view=view, title=view), cols, 45, beat=1)
                    cells = [[list(cell) for cell in row] for row in actual.cells]
                    hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
                    expected_cells = expand_rows(expected["cells"])
                    version, method = _fixture_header_identity(vm)
                    cells = normalize_f70_method_delta(cells, expected_cells, version=version, method=method)
                    expected_cells, expected_hits = normalize_new_tree_viewport_row(
                        cells, expected_cells, hits, expected["hits"], actual.tree_scroll)
                    self.assertEqual(cells, expected_cells, f"{view} cells at {cols}")
                    self.assertEqual(hits, expected_hits, f"{view} ordered hits at {cols}")

    def test_repo_session_route_pages_and_unfolded_route_match_reference(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            expected_file = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            states = expected_file["list_states"]
            for case, base in (
                ("repo-page", NavState(scope="repo:xxxxxxxx")),
                ("session-page-f5", NavState(scope="session:f5")),
                ("route-page-f5", NavState(scope="route:f5")),
                ("ROUTES-open-f5", NavState(view="ROUTES",
                                             folds=(("ropen:ROUTES", "f5"),))),
            ):
                with self.subTest(cols=cols, case=case):
                    expected = states[case]
                    from dataclasses import replace
                    state = replace(base, title=expected["title"])
                    actual = draw(vm, state, cols, 45, beat=1)
                    cells = [[list(cell) for cell in row] for row in actual.cells]
                    hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
                    expected_cells = expand_rows(expected["cells"])
                    version, method = _fixture_header_identity(vm)
                    cells = normalize_f70_method_delta(cells, expected_cells, version=version, method=method)
                    expected_cells, expected_hits = normalize_new_tree_viewport_row(
                        cells, expected_cells, hits, expected["hits"], actual.tree_scroll)
                    self.assertEqual(cells, expected_cells, f"{case} cells at {cols}")
                    self.assertEqual(hits, expected_hits, f"{case} ordered hits at {cols}")

    def test_peers_and_filtered_links_match_exact_reference(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            expected_file = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            cases = (
                ("PEERS", NavState(view="PEERS", title="PEERS")),
                ("PEERS-pick-f5", NavState(view="PEERS", title="PEERS",
                                            folds=(("pfocus:PEERS", "f5"),))),
            )
            for name, state in cases:
                with self.subTest(cols=cols, state=name):
                    expected = expected_file["list_states"][name]
                    actual = draw(vm, state, cols, 45, beat=1)
                    cells = [[list(cell) for cell in row] for row in actual.cells]
                    hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
                    expected_cells = expand_rows(expected["cells"])
                    version, method = _fixture_header_identity(vm)
                    cells = normalize_f70_method_delta(cells, expected_cells, version=version, method=method)
                    expected_cells, expected_hits = normalize_new_tree_viewport_row(
                        cells, expected_cells, hits, expected["hits"], actual.tree_scroll)
                    self.assertEqual(cells, expected_cells, f"{name} cells at {cols}")
                    self.assertEqual(hits, expected_hits, f"{name} ordered hits at {cols}")


class QuietReference(unittest.TestCase):
    def test_compute_memory_models_system_exact_reference_140_168(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            golden = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            for view in ("COMPUTE", "MEMORY", "MODELS", "SYSTEM"):
                with self.subTest(cols=cols, view=view):
                    expected = golden["list_states"][view]
                    actual = draw(vm, NavState(view=view, title=view), cols, 45, beat=1)
                    cells = [[list(cell) for cell in row] for row in actual.cells]
                    hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
                    expected_cells = expand_rows(expected["cells"])
                    version, method = _fixture_header_identity(vm)
                    cells = normalize_f70_method_delta(cells, expected_cells, version=version, method=method)
                    expected_cells, expected_hits = normalize_new_tree_viewport_row(
                        cells, expected_cells, hits, expected["hits"], actual.tree_scroll)
                    self.assertEqual(cells, expected_cells, f"{view} cells at {cols}")
                    self.assertEqual(hits, expected_hits, f"{view} ordered hits at {cols}")

    def test_folded_question_and_history_generation_timeline_match_reference(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (140, 168):
            expected_file = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            question_state = NavState(view="QUESTIONS", title="QUESTIONS",
                                      folds=(("qshut:QUESTIONS", "bb"),))
            actual = draw(vm, question_state, cols, 45, beat=1)
            expected = expected_file["list_states"]["QUESTIONS-fold-bb"]
            actual_cells = [[list(cell) for cell in row] for row in actual.cells]
            expected_cells = expand_rows(expected["cells"])
            version, method = _fixture_header_identity(vm)
            actual_cells = normalize_f70_method_delta(actual_cells, expected_cells, version=version, method=method)
            actual_hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
            expected_cells, expected_hits = normalize_new_tree_viewport_row(
                actual_cells, expected_cells, actual_hits, expected["hits"], actual.tree_scroll)
            self.assertEqual(actual_cells, expected_cells)
            self.assertEqual(actual_hits, expected_hits)

            history_state = NavState(view="HISTORY", title="HISTORY",
                                     folds=(("hopen:HISTORY", "0c"),))
            actual = draw(vm, history_state, cols, 45, beat=1)
            expected = expected_file["list_states"]["HISTORY-open-0c"]
            # P2 owns the seat and generation timeline through generation rows;
            # card and conversation content below it belongs to P4.
            expected_cells = expand_rows(expected["cells"])
            actual_cells = [[list(cell) for cell in row] for row in actual.cells]
            version, method = _fixture_header_identity(vm)
            actual_cells = normalize_f70_method_delta(actual_cells, expected_cells,
                                                      version=version, method=method)
            self.assertEqual(tuple(tuple(tuple(cell) for cell in row) for row in actual_cells[:11]),
                             tuple(tuple(tuple(cell) for cell in row) for row in expected_cells[:11]))
            actual_hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits if 0 <= hit.y <= 10]
            expected_hits = [hit for hit in expected["hits"] if 0 <= hit[0] <= 10]
            self.assertEqual(actual_hits, expected_hits)

            card_state = NavState(view="HISTORY", title="HISTORY",
                                  folds=(("hopen:HISTORY", "0c"),))
            actual = draw(vm, card_state, cols, 45, beat=1)
            expected = expected_file["list_states"]["HISTORY-open-0c-card"]
            actual_cells = [[list(cell) for cell in row] for row in actual.cells]
            expected_cells = expand_rows(expected["cells"])
            version, method = _fixture_header_identity(vm)
            actual_cells = normalize_f70_method_delta(actual_cells, expected_cells, version=version, method=method)
            actual_hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
            expected_cells, expected_hits = normalize_new_tree_viewport_row(
                actual_cells, expected_cells, actual_hits, expected["hits"], actual.tree_scroll)
            self.assertEqual(actual_cells, expected_cells, f"HISTORY card/dialogue cells at {cols}")
            self.assertEqual(actual_hits, expected_hits,
                             f"HISTORY card/dialogue ordered hits at {cols}")

            picked_state = NavState(view="HISTORY", title="HISTORY",
                                    folds=(("hopen:HISTORY", "0c"),
                                           ("ui:gen:f5", "0")))
            actual = draw(vm, picked_state, cols, 45, beat=1)
            expected = expected_file["list_states"]["HISTORY-open-0c-picked-generation"]
            actual_cells = [[list(cell) for cell in row] for row in actual.cells]
            expected_cells = expand_rows(expected["cells"])
            version, method = _fixture_header_identity(vm)
            actual_cells = normalize_f70_method_delta(actual_cells, expected_cells, version=version, method=method)
            actual_hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in actual.hits]
            expected_cells, expected_hits = normalize_new_tree_viewport_row(
                actual_cells, expected_cells, actual_hits, expected["hits"], actual.tree_scroll)
            self.assertEqual(actual_cells, expected_cells, f"HISTORY picked-generation cells at {cols}")
            self.assertEqual(actual_hits, expected_hits,
                             f"HISTORY picked-generation ordered hits at {cols}")
