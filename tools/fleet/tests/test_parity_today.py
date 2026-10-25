"""Hermetic coverage for today's parity capture primitives."""
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from tools.fleet.tests.parity_compare import diff_frames
from tools.fleet.tests.parity_today import (_frame_from_reference, _jsonable, _unique_reference_copy_limitations,
                                            classify)


class TodayParityPlumbingTests(unittest.TestCase):
    def test_reference_capture_becomes_comparable_frame(self):
        row = {"cells": [["a", "b"]], "hits": [[0, 0, 1, ["view", "OVERVIEW"]]]}
        frame = _frame_from_reference(row)
        self.assertEqual((frame.cols, frame.rows), (2, 1))
        self.assertEqual(diff_frames(frame, frame), [])

    def test_jsonable_keeps_dataclass_fields_and_drops_unknown_objects(self):
        @dataclass
        class Value:
            count: int
            hidden: object
        value = Value(3, object())
        self.assertEqual(_jsonable(value), {"count": 3, "hidden": None})


def _record(width=140, rows=45, cells=(), hits=None):
    blank = " " * width
    reference, fleet2 = [blank] * rows, [blank] * rows
    differences = [{"kind": "cell", "row": row, "column": column, "expected": [a, ka, None], "actual": [b, kb, None]}
                   for row, column, a, ka, b, kb in cells]
    if hits is not None:
        differences.append({"kind": "hits", "expected": hits[0], "actual": hits[1]})
    for row, column, a, _ka, b, _kb in cells:
        reference[row] = reference[row][:column] + a + reference[row][column + 1:]
        fleet2[row] = fleet2[row][:column] + b + fleet2[row][column + 1:]
    return {"state": "OVERVIEW", "width": width, "rows": rows, "reference_rows": reference, "fleet2_rows": fleet2,
            "reference_edge": [False] * rows, "differences": differences}


class ClassificationTests(unittest.TestCase):
    def test_narrow_reference_defects_precede_content_rules_but_clean_edges_keep_real_defects(self):
        answered = _record(width=80, cells=[(10, 40, "x", "dim", "y", "dim")])
        answered["state"] = "QUESTIONS"
        answered["reference_rows"][10] = " " * 33 + "ANSWERED TODAY 20" + " " * 30
        answered["fleet2_rows"][10] = " " * 33 + "ANSWERED TODAY 20" + " " * 30
        answered["reference_edge"][10] = True

        context = _record(width=100, cells=[(10, 40, "x", "dim", "y", "dim")])
        context["state"] = "session"
        context["reference_rows"][10] = " " * 33 + "context: unknown · last clear ·" + " " * 40
        context["fleet2_rows"][10] = " " * 33 + "context: unknown" + " " * 60
        context["reference_edge"][10] = True

        clean_context = _record(width=100, cells=[(10, 40, "x", "dim", "y", "dim")])
        clean_context["state"] = "session"
        clean_context["reference_rows"][10] = " " * 33 + "context: unknown · last clear ·"
        clean_context["fleet2_rows"][10] = " " * 33 + "context: unknown"

        docs = [classify([record]) for record in (answered, context, clean_context)]
        self.assertEqual(docs[0]["entries"][0]["class"], "excluded")
        self.assertEqual(docs[0]["entries"][0]["rule"], "question-header-overflow")
        self.assertEqual(docs[1]["entries"][0]["class"], "excluded")
        self.assertIn(docs[1]["entries"][0]["rule"], {"narrow-header-overflow", "routes-narrow"})
        self.assertEqual((docs[2]["entries"][0]["class"], docs[2]["entries"][0]["rule"]),
                         ("fleet2-defect", "session-context-line"))

    def test_a_known_style_difference_gets_a_reason_and_an_owner(self):
        document = classify([_record(cells=[(1, 21, "━", "g_blocked", "━", "grp_live")])])
        self.assertEqual(document["summary"]["unexplained"], 0)
        entry = document["entries"][0]
        self.assertEqual((entry["rule"], entry["class"]), ("usage-gauge-80", "fleet2-defect"))
        self.assertTrue(entry["owner"].endswith("chrome.py"))

    def test_a_difference_no_rule_explains_stays_unexplained(self):
        document = classify([_record(cells=[(20, 50, "a", "dim", "b", "dim")])])
        self.assertEqual(document["summary"]["unexplained"], 1)

    def test_a_hit_difference_on_a_row_whose_cells_are_equal_is_not_derived(self):
        hit = [30, 34, 100, ["scope", "x"]]
        document = classify([_record(hits=([hit], []))])
        self.assertEqual([entry["class"] for entry in document["entries"]], ["unexplained"])

    def test_a_hit_difference_on_an_explained_row_follows_its_cells(self):
        hit = [1, 34, 100, ["usage"]]
        document = classify([_record(cells=[(1, 21, "━", "g_blocked", "━", "grp_live")], hits=([hit], []))])
        self.assertEqual(sorted(entry["class"] for entry in document["entries"]), ["derived", "fleet2-defect"])

    def test_reference_hits_beyond_the_screen_are_the_recorded_narrow_width_defect(self):
        hit = [6, 71, 88, ["ui", "sel", "top/codex"]]
        document = classify([_record(width=80, hits=([hit], []))])
        self.assertEqual((document["entries"][0]["class"], document["entries"][0]["rule"]), ("excluded", "hit-outside-screen"))


class InjectionPieces(unittest.TestCase):
    def test_unknown_compute_metrics_remain_none_and_render_as_unknown_cells(self):
        from types import SimpleNamespace
        from tools.fleet.tests.reference_injection import _adapt_unknown_compute_cells

        class Grid:
            def __init__(self):
                self.cells = {}

            def put(self, x, y, value, _style):
                self.cells[(x, y)] = value

            def segs(self, x, y, value):
                self.cells[(x, y)] = value

        def compute_renderer(g, X, use, y, util, used, total):
            for _host in (None,):
                for _gpu in (0,):
                    st = "working"
                    g.put(X + 31, y, "%3d%%" % util, "soft" if st == "working" else "dim")
                    g.segs(X + 38, y, gauge(round(total / 4), used / total * 100))
                    g.put(X + 51, y, ("%g/%g GB" % (used, total)).rjust(10), "dim")

        def compute_block(g, EW, y1, used, total):
            for _host in (None,):
                for right, lab in [(23, "gpu0")]:
                    for _gpu in (0,):
                        y, gl, gk2 = y1, "●", "dim"
                        p = round(used / total * 100)
                        g.put(right - 5, y, gl, gk2)
                        g.put(right - 3, y, "%3d%%" % p, pct_key(p) if p else "dim")

        renderer = SimpleNamespace(ROOT_FN={"COMPUTE": compute_renderer},
                                   mock12=SimpleNamespace(compute_block=compute_block),
                                   mock6=SimpleNamespace(COMPUTE_BLOCK=compute_block))
        compute = [["moving4", True, [1, 2], [1, 2], [0, 1],
                    [[0, "idle", "GPU", "gpu_test", None, None, None, []]]]]
        with mock.patch.dict(globals(), {"gauge": lambda track, percent: (track, percent),
                                         "pct_key": lambda _percent: "known"}):
            limitations = _adapt_unknown_compute_cells(renderer, compute)
            # Keep the captured values as None; only the isolated renderer's cell formatting adapts.
            self.assertEqual(compute[0][5][0][4:7], [None, None, None])
            self.assertEqual(limitations, [{"reference_file": "mock13.py", "host": "moving4", "gpu": 0,
                                            "unknown_cells": ["UTIL", "COMPUTE block VRAM percent",
                                                              "VRAM gauge", "VRAM used/total"]}])

            unknown = Grid()
            renderer.ROOT_FN["COMPUTE"](unknown, 0, 60, 5, None, None, None)
            self.assertEqual(unknown.cells, {(31, 5): "  ·", (38, 5): "·", (51, 5): "·"})

            unknown_block = Grid()
            renderer.mock12.compute_block(unknown_block, 32, 7, None, None)
            self.assertEqual(unknown_block.cells, {(18, 7): "●", (20, 7): "  ·"})

            known = Grid()
            renderer.ROOT_FN["COMPUTE"](known, 0, 60, 6, 75, 12, 24)
            self.assertEqual(known.cells, {(31, 6): " 75%", (38, 6): (6, 50.0),
                                           (51, 6): "  12/24 GB"})

            known_block = Grid()
            renderer.mock12.compute_block(known_block, 32, 8, 12, 24)
            self.assertEqual(known_block.cells, {(18, 8): "●", (20, 8): " 50%"})

    def test_reference_copy_limitations_are_reported_without_exempting_differences(self):
        limitation = {"reference_file": "mock13.py", "host": "moving4", "gpu": 0,
                      "unknown_cells": ["VRAM gauge", "VRAM used/total"]}
        records = [_record(cells=[(10, 50, "x", "dim", "y", "dim")]) for _ in range(2)]
        for record in records:
            record["reference_copy_limitations"] = [limitation]
        self.assertEqual(_unique_reference_copy_limitations(records), [limitation])
        self.assertEqual(classify(records[:1])["summary"]["unexplained"], 1)

    def test_a_state_outside_the_five_the_copy_knows_draws_as_an_idle_dot(self):
        from tools.fleet.tests.reference_injection import _session_row
        row = _session_row({"tag": "aa", "harness": "claude", "state": "stale", "context": None, "window": None})
        self.assertEqual((row["st"], row["ctx"], row["win"]), ("idle", None, 1.0))

    def test_a_route_row_keeps_the_captures_own_words(self):
        from tools.fleet.tests.reference_injection import _route_record
        record = _route_record({"id": "rt-1", "state": "working", "harness": "codex", "legs": [["code", "active", "2/6"]],
                                "stages": [{"name": "plan", "st": "done"}], "activity": "running", "tree_label": "code"})
        self.assertEqual((record["act"], record["tree_label"], record["legs"]), ("running", "code", [("code", "active", "2/6")]))

    def test_the_status_words_follow_the_capture(self):
        from types import SimpleNamespace
        from tools.fleet.tests.reference_injection import _status
        renderer = SimpleNamespace(mock5=SimpleNamespace(SEATS=[("aa", "r", 3, "·", False)]))
        vm = {"sessions": [{"steward": True}], "routes": [{"state": "dead"}, {"state": "working"}], "questions": [],
              "compute": [], "system": {"peer_delivery": {}, "install": {"version": "v9"}}}
        status = _status(renderer, vm, 0)
        self.assertEqual(status["OVERVIEW"], "1 session · 2 routes · 0 questions")
        self.assertEqual(status["ROUTES"], "2 open · 1 stopped leg")
        self.assertEqual(status["SYSTEM"], "v9 · · deliveries pending")


if __name__ == "__main__":
    unittest.main()
