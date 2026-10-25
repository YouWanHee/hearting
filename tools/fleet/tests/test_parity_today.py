"""Hermetic coverage for today's parity capture primitives."""
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from tools.fleet.tests.parity_compare import diff_frames
from tools.fleet.tests.parity_today import (_frame_from_reference, _jsonable, _unique_reference_copy_limitations,
                                            build_live_vm, classify)


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

    def test_current_prd_spinner_order_is_intentional_and_not_a_system_pending_defect(self):
        record = _record(width=80, cells=[(0, 2, "0", "dim", "⠏", "spin")])
        record["state"] = "SYSTEM"
        record["reference_rows"][0] = " 0 working  ● 0 idle  ● 0 answer needed   hearting v3.50.3-20-gad247e2b6  02:38⠏"
        record["fleet2_rows"][0] = "  ⠏ 0 working  ● 0 idle  ● 0 answer needed   hearting v3.50.3-20-gad247e2b6  02"

        entry = classify([record])["entries"][0]

        self.assertEqual((entry["class"], entry["rule"]), ("intended", "prd-census-spinner-order"))

    def test_empty_answered_repo_placeholder_does_not_claim_an_aggregate_count_defect(self):
        record = _record(width=140, cells=[(7, 40, "0", "dim", "·", "dim")])
        record["state"] = "QUESTIONS"
        record["reference_rows"][7] = " " * 33 + " ── ANSWERED TODAY  0 · 0 decision records in all projects ───────"
        record["fleet2_rows"][7] = " " * 33 + " ── ANSWERED TODAY  · 0 · 0 decision records in all projects ─────"

        entry = classify([record])["entries"][0]

        self.assertEqual(entry["class"], "unexplained")

    def test_answered_repo_count_difference_requires_the_same_explicit_repo(self):
        record = _record(width=140, cells=[(7, 40, "6", "dim", "20", "dim")])
        record["state"] = "QUESTIONS"
        record["reference_rows"][7] = " " * 33 + " ── ANSWERED TODAY  SR_CorrNet 6 · 32 decision records in all projects"
        record["fleet2_rows"][7] = " " * 33 + " ── ANSWERED TODAY  SR_CorrNet 20 · 32 decision records in all projects"

        entry = classify([record])["entries"][0]

        self.assertEqual((entry["class"], entry["rule"]), ("fleet2-defect", "answered-head"))

    def test_rightmost_feed_cell_is_the_recorded_terminal_guard(self):
        record = _record(width=80, cells=[(42, 78, "수", "soft", " ", None),
                                         (42, 79, "", "soft", " ", None)])
        record["reference_rows"][42] = " ≋ event text ends with 수"
        record["fleet2_rows"][42] = " ≋ event text ends with"

        entry = classify([record])["entries"][0]

        self.assertEqual((entry["class"], entry["rule"]), ("intended", "right-edge-safe-clip"))

    def test_unknown_compute_track_is_a_narrow_reference_copy_limit(self):
        record = _record(width=140, cells=[(5, 55, "·", "dim", "─", "dim"),
                                          (6, 55, "·", "dim", "─", "dim")])
        record["state"] = "COMPUTE"
        record["reference_copy_limitations"] = [
            {"reference_file": "mock13.py", "host": "moving4", "gpu": gpu,
             "unknown_cells": ["UTIL", "VRAM gauge"]} for gpu in (0, 1)]
        record["reference_rows"][5] = " " * 33 + "  moving4   ● 0  RTX 6000 Ada     ·    ·            ·"
        record["fleet2_rows"][5] = " " * 33 + "  moving4   ● 0  RTX 6000 Ada    ·     ··········   ·"
        record["reference_rows"][6] = " " * 33 + "            ● 1  RTX 6000 Ada     ·    ·            ·"
        record["fleet2_rows"][6] = " " * 33 + "            ● 1  RTX 6000 Ada    ·     ··········   ·"

        entry = classify([record])["entries"][0]

        self.assertEqual((entry["class"], entry["rule"]), ("copy-limitation", "unknown-gpu-track"))

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
    def test_saved_model_config_is_loaded_when_detail_reads_are_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            capture = Path(temp)
            (capture / "fleet-json.json").write_text("{}", encoding="utf-8")
            (capture / "snapshot-bundle.json").write_text('{"captured_at": 123.0}', encoding="utf-8")
            (capture / "detail-reads.json").write_text("{}", encoding="utf-8")
            (capture / "seat-reads.json").write_text("{}", encoding="utf-8")
            config = {"profiles": []}
            (capture / "model-config.json").write_text('{"profiles": []}', encoding="utf-8")

            with mock.patch("tools.fleet.tests.snapshot_fixture.replay_source_bundle",
                            return_value=("snapshot", None)), \
                    mock.patch("tools.fleet.tests.parity_today._merged_detail",
                               return_value="accepted-detail") as merge_detail, \
                    mock.patch("tools.fleet.explorer.live.build_view_model", return_value="view-model") as build_vm:
                vm, now = build_live_vm(capture)

        self.assertEqual((vm, now), ("view-model", 123.0))
        merge_detail.assert_called_once_with({}, {}, config)
        build_vm.assert_called_once_with("snapshot", accepted_detail="accepted-detail", now=123.0)

    def test_reference_tree_drops_repo_rows_without_a_current_session(self):
        from tools.fleet.tests.reference_injection import _project_rows

        vm = {"repos": [{"id": "ended", "name": "ended"}, {"id": "active", "name": "active"}],
              "sessions": [{"tag": "a3", "repo_id": "active", "repo": "active"}]}
        rows = _project_rows(vm, [{"tag": "a3"}])

        self.assertEqual([(name, len(sessions)) for name, _done, _age, sessions in rows], [("active", 1)])

    def test_absent_model_config_clears_reference_seed_values(self):
        from types import SimpleNamespace
        from tools.fleet.tests.reference_injection import _inject_models

        renderer = SimpleNamespace(
            mock2=SimpleNamespace(LIVE={"allocation_fields": ["seed"], "allocation_note": "seed"}),
            PROFILES=["seed"], PROFILE_MAP={"seed": {}}, MODEL_CHOICES={"seed": []}, MAIN_ONLY={"seed": []},
            EFFORTS=["seed"], POOLS={"seed": None}, EFF_KEY={"seed": "seed"}, MODEL_SHOWN={"seed": "seed"},
            CTX={"seed": 1}, CTX_HOW={"seed": "seed"}, TIDY=(1, 2), KTOK={"seed": 1})

        _inject_models(renderer, None)

        self.assertEqual(renderer.PROFILES, [])
        self.assertEqual(renderer.MODEL_CHOICES, {})
        self.assertEqual(renderer.MAIN_ONLY, {})
        self.assertEqual(renderer.CTX, {})
        self.assertEqual(renderer.TIDY, ())
        self.assertNotIn("allocation_fields", renderer.mock2.LIVE)
        self.assertNotIn("allocation_note", renderer.mock2.LIVE)

    def test_compute_footer_uses_captured_host_gpu_states(self):
        from tools.fleet.tests.reference_injection import _compute_foot

        vm = {"compute": [["a", False, [], [], [], [[0, "working"], [1, "idle"]]],
                           ["b", False, [], [], [], [[0, "idle"]]]]}

        self.assertEqual(_compute_foot(vm), "2/2 hosts · GPU 1/3 working · 1 host idle")

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
