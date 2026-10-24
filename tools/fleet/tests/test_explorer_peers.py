"""Peer pane parity and layout checks from scrubbed fixture evidence."""
import json
import unittest
from pathlib import Path

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import load_fixture

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
SEPARATOR = 34


def _expand(rows):
    return [[cell for glyph, colour, tint, count in row
             for cell in [[glyph, colour, tint]] * count] for row in rows]


def _with_reference_additions(vm):
    """Restore PEERS-only data represented by the scrubbed reference literals."""
    values = vm.to_mapping()
    literal = values.get("source_records", {}).get("reference_literals", {})
    mock13 = literal.get("mock13", {})
    mock5 = literal.get("mock5", {})
    mock6 = literal.get("mock6", {})
    edges = dict(values.get("peer_edges") or {})
    cards = {}
    owners = mock6.get("OWN", {})
    extras = mock5.get("EXTRA", {})
    routes_by_tag = {}
    for route in values.get("routes", ()):
        routes_by_tag.setdefault(str(route.get("session_tag") or ""), []).append(route)
    for tag in set(owners) | set(extras):
        card = {}
        owner_rows = []
        route_candidates = sorted(routes_by_tag.get(str(tag), ()),
                                  key=lambda route: {"dead": 0, "working": 1, "idle": 2,
                                                     "blocked": 3, "done": 4}.get(route.get("state"), 5))
        for owner_index, record in enumerate(owners.get(tag, ())):
            if isinstance(record, dict):
                state, label, stages = record.get("state"), record.get("label"), record.get("stages", ())
            else:
                state, label, stages = record
            if not state or set(str(state)) <= {"x"}:
                state = (route_candidates[owner_index].get("state") if owner_index < len(route_candidates)
                         else "done")
            owner_rows.append({"state": state, "label": label,
                               "stages": [{"name": stage.get("name"), "state": stage.get("st")}
                                          if isinstance(stage, dict) else {"name": stage[0], "state": stage[1]}
                                          for stage in stages]})
        if owner_rows:
            card["owners"] = owner_rows
        extra = extras.get(tag, ())
        if extra:
            card["extra"] = [(text, style if style in ("dim", "blocked") else
                               ("blocked" if len(str(style)) >= 7 else "dim")) for text, style in extra]
        cards[tag] = card
    edges["cards"] = cards
    links = edges.get("links", ())
    edges["lane_labels"] = [
        {"from": str(link[0][0]), "to": str(link[1][0]), "kind": link[2],
         "when": str(link[3]).removesuffix(" ago")}
        for link in links if isinstance(link[0], (list, tuple)) and isinstance(link[1], (list, tuple))
        and str(link[2]) == "steer"
    ]
    edges["generations"] = [
        {"tag": str(link[1][0]), "harness": str(link[1][1]),
         "text": f"◂ {link[0]} · {str(link[3]).removesuffix(' ago')}"}
        for link in links if isinstance(link[1], (list, tuple)) and str(link[2]) == "handover"
    ]
    watch_sources = [str(link[0][0]) for link in links
                     if isinstance(link[0], (list, tuple)) and str(link[2]).startswith("watch")]
    if watch_sources:
        repo_by_tag = {str(tag): str(repo) for repo, columns in mock13.get("PEER_PROJECTS", ())
                       for col in columns for tag in col}
        edges["watches"] = [{"repo": repo_by_tag.get(tag),
                             "text": f"watch {len(watch_sources)} · ended"}
                            for tag in dict.fromkeys(watch_sources) if repo_by_tag.get(tag)]
    values["peer_edges"] = edges
    from tools.fleet.explorer.view_model import ExplorerViewModel
    return ExplorerViewModel.from_mapping(values)


def _pane(frame):
    cells = [[list(cell) for cell in row[SEPARATOR:]] for row in frame.cells[5:]]
    hits = [[hit.y - 5, hit.x0 - SEPARATOR, hit.x1 - SEPARATOR, list(hit.action)]
            for hit in frame.hits if hit.y >= 5 and hit.x0 >= SEPARATOR]
    return cells, hits


class PeerPaneReference(unittest.TestCase):
    def test_peers_and_selected_links_match_reference_pane(self):
        vm = _with_reference_additions(load_fixture(FIXTURES / "view_model.json"))
        for cols in (140, 168):
            capture = json.loads((FIXTURES / f"cells-{cols}.json").read_text())
            for name, folds in (("PEERS", ()), ("PEERS-pick-f5", (("pfocus:PEERS", "f5"),))):
                with self.subTest(cols=cols, state=name):
                    state = NavState(view="PEERS", title="PEERS", folds=folds)
                    actual_cells, actual_hits = _pane(draw(vm, state, cols, 45, beat=1))
                    expected = capture["list_states"][name]
                    expected_cells = _expand(expected["cells"])
                    expected_cells = [row[SEPARATOR:] for row in expected_cells[5:]]
                    expected_hits = [[hit[0] - 5, hit[1] - SEPARATOR, hit[2] - SEPARATOR, hit[3]]
                                     for hit in expected["hits"]
                                     if hit[0] >= 5 and hit[1] >= SEPARATOR]
                    if actual_cells != expected_cells:
                        mismatch = next(((row, col, expected_cells[row][col], actual_cells[row][col])
                                         for row in range(min(len(actual_cells), len(expected_cells)))
                                         for col in range(min(len(actual_cells[row]), len(expected_cells[row])))
                                         if actual_cells[row][col] != expected_cells[row][col]), None)
                        self.fail(f"{name} pane first cell difference at {cols}: {mismatch}")
                    self.assertEqual(actual_hits, expected_hits, f"{name} pane ordered hits at {cols}")

    def test_narrow_pane_has_bounded_hits_and_no_horizontal_overflow(self):
        vm = _with_reference_additions(load_fixture(FIXTURES / "view_model.json"))
        frame = draw(vm, NavState(view="PEERS", title="PEERS"), 80, 100, beat=1)
        self.assertEqual(len(frame.cells), 100)
        self.assertTrue(all(len(row) == 80 for row in frame.cells))
        for hit in frame.hits:
            if hit.y == -1:  # chrome's keyboard target is a deliberate off-screen sentinel
                continue
            self.assertGreaterEqual(hit.y, 0)
            self.assertLess(hit.y, 96)
            self.assertGreaterEqual(hit.x0, 0)
            self.assertLessEqual(hit.x1, 80)
            self.assertLess(hit.x0, hit.x1)

    def test_cards_use_each_harness_palette(self):
        base = load_fixture(FIXTURES / "view_model.json").to_mapping()
        base["sessions"] = [
            {"tag": harness[:2], "harness": harness, "title": harness, "state": "working",
             "context": 40, "window": 1.0, "steward": False}
            for harness in ("claude", "codex", "opencode")]
        base["peer_nodes"] = [("repo", [[item["tag"] for item in base["sessions"]]])]
        base["peer_edges"] = {"links": [], "cross": [], "side_lanes": []}
        from tools.fleet.explorer.view_model import ExplorerViewModel
        frame = draw(ExplorerViewModel.from_mapping(base), NavState(view="PEERS", title="PEERS"), 140, 30)
        used = {cell[1] for row in frame.cells for cell in row if cell[1]}
        for harness in ("claude", "codex", "opencode"):
            self.assertIn("hb_" + harness, used)


if __name__ == "__main__":
    unittest.main()
