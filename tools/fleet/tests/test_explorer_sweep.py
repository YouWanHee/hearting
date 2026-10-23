"""Bounds checks for the P2 list views at supported terminal widths."""
import unittest

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.grid import cell_width
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import load_fixture
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures" / "explorer"


class ListSizes(unittest.TestCase):
    def test_list_views_fit_and_hits_stay_inside_the_body(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (80, 100, 140, 168):
            for view in ("ROUTES", "QUESTIONS", "HISTORY", "PEERS"):
                variants = [NavState(view=view, title=view)]
                fold_key, ident = {"ROUTES": ("ropen:ROUTES", "f5"),
                                   "QUESTIONS": ("qshut:QUESTIONS", "bb"),
                                   "HISTORY": ("hopen:HISTORY", "0c"),
                                   "PEERS": ("pfocus:PEERS", "f5")}[view]
                variants.append(NavState(view=view, title=view, folds=((fold_key, ident),)))
                for state in variants:
                    with self.subTest(cols=cols, view=view, folds=state.folds):
                        frame = draw(vm, state, cols, 45, beat=1)
                        self.assertEqual(len(frame.cells), 45)
                        self.assertTrue(all(len(row) == cols for row in frame.cells))
                        self.assertTrue(all(hit.y < 41 for hit in frame.hits if hit.y >= 0))
                        self.assertTrue(all((hit.y == -1 and hit.action[0] == "parent") or
                                            (0 <= hit.x0 < hit.x1 <= cols)
                                            for hit in frame.hits))
                        self.assertTrue(all(sum(cell_width(cell[0]) for cell in row) == cols
                                            for row in frame.cells))
                        self.assertEqual(len(frame.text_rows()[42]), cols)
                        self.assertEqual(len(frame.text_rows()[43]), cols)
                        self.assertEqual(len(frame.text_rows()[44]), cols)
                        if cols <= 100 and view == "ROUTES":
                            body = frame.text_rows()
                            x = 34
                            use = cols - x - 2
                            route_header = body[3]
                            route_spans = ((x + 3, x + 5), (x + 11, x + 16),
                                           (x + use - 8, x + use - 5))
                            self.assertEqual(route_header[route_spans[0][0]:route_spans[0][1]], "ID")
                            self.assertEqual(route_header[route_spans[1][0]:route_spans[1][1]], "ROUTE")
                            self.assertEqual(route_header[route_spans[2][0]:route_spans[2][1]], "AGE")
                            for left, right in zip(route_spans, route_spans[1:]):
                                self.assertLessEqual(left[1], right[0])
                        if cols <= 100 and view == "QUESTIONS":
                            header = frame.text_rows()[4]
                            q_spans = ((37, 39), (44, 51), (58, 62), (69, 73))
                            for (lo, hi), label in zip(q_spans, ("ID", "HARNESS", "KIND", "WAIT")):
                                self.assertEqual(header[lo:hi], label)
                            for left, right in zip(q_spans, q_spans[1:]):
                                self.assertLessEqual(left[1], right[0])
                        if cols <= 100 and view == "HISTORY":
                            header = frame.text_rows()[3]
                            h_spans = ((37, 39), (46, 53), (58, 61), (66, 76))
                            for (lo, hi), label in zip(h_spans, ("ID", "HARNESS", "GEN", "LAST CLEAR")):
                                self.assertEqual(header[lo:hi], label)
                            for left, right in zip(h_spans, h_spans[1:]):
                                self.assertLessEqual(left[1], right[0])


class QuietSizes(unittest.TestCase):
    def test_quiet_views_fit_and_hits_stay_in_bounds_at_all_widths(self):
        vm = load_fixture(FIXTURES / "view_model.json")
        for cols in (80, 100, 140, 168):
            for view in ("COMPUTE", "MEMORY", "MODELS", "SYSTEM"):
                state = NavState(view=view, title=view)
                if view == "MODELS":
                    state = NavState(view=view, title=view,
                                     folds=(("ui:sel", "light/codex"),
                                            ("ui:model", "gpt-6-luna"),
                                            ("ui:effort", "high")))
                with self.subTest(cols=cols, view=view):
                    frame = draw(vm, state, cols, 45, beat=1)
                    self.assertTrue(all(len(row) == cols for row in frame.cells))
                    self.assertTrue(all((hit.y == -1 and hit.action[0] == "parent") or
                                        (0 <= hit.y < 41 and 0 <= hit.x0 < hit.x1 <= cols)
                                        for hit in frame.hits))
                    self.assertEqual(len(frame.text_rows()[42]), cols)
                    self.assertEqual(len(frame.text_rows()[43]), cols)
                    self.assertEqual(len(frame.text_rows()[44]), cols)
