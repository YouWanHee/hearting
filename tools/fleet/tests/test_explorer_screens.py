"""Content of the composed B screen against the approved screens (not only their geometry).

Every scene of ``fixtures/explorer_b/screens.json`` is composed from the fixture snapshot and the
selected-scope result at 100 and 168 columns, 45 rows. Every row outside the raw-output rectangle
must equal the approved text; the few cells where the product text is allowed to differ are listed
in the scene's ``allowed`` and each of them is asserted explicitly below.
"""
import os
import re
import tempfile
import unittest
from unittest import mock

from tools.fleet.explorer.focus import output_plans
from tools.fleet.explorer.frame import compose, output_pane
from tools.fleet.explorer.ledger import replacement_lines
from tools.fleet.explorer.output import cell_width
from tools.fleet.explorer.state import ExplorerState
from tools.fleet.explorer.types import AttemptRow, FocusKey, FocusResult, Rect, RequestTab
from tools.fleet.tests import explorer_fixture as ef
from tools.fleet.tests.explorer_fixture import (cells, collect, load_screens, materialize, scene_frame,
                                                scene_result, scene_state, tab)

BORDER = "┃"
COMPARED = ("first", "history", "overview", "system", "overflow", "tab-broken", "source-failed")
BANDS = ("inflight", "output-oc")


def blank(text, x0, x1):
    out, col = [], 0
    for char in text:
        width = cell_width(char)
        out.append(" " * width if x0 <= col < x1 else char)
        col += width
    return "".join(out)


def pad(text, cols):
    return text + " " * (cols - cell_width(text))


def split_row(row):
    left, _border, right = row.partition(BORDER)
    return left, right


class SceneContent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.screens = load_screens()
        cls.frames = {(name, cols): scene_frame(cls.fx, scene, cols)
                      for name, scene in cls.screens["scenes"].items() for cols in (100, 168)}

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def scene(self, name):
        return self.screens["scenes"][name]

    def rows_of(self, name, cols):
        data = self.scene(name)["widths"][str(cols)]
        ranges = self.scene(name).get("compare_rows") or [[0, 45]]
        return data, [i for first, last in ranges for i in range(first, last)]

    def allowed_rows(self, name, cols):
        rows = {}
        for entry in self.scene(name).get("allowed", ()):
            if "rows" in entry:
                first, last = entry["rows"][str(cols)]
                rows[entry["id"]] = range(first, last)
        return rows

    def test_there_are_nine_scenes_and_all_are_composed_at_both_widths(self):
        self.assertEqual(len(self.screens["scenes"]), 9)
        self.assertEqual(set(self.screens["scenes"]), set(COMPARED) | set(BANDS))
        self.assertEqual(len(self.frames), 18)

    def test_every_scene_is_45_rows_and_every_row_is_exactly_the_terminal_width(self):
        for (name, cols), frame in self.frames.items():
            with self.subTest(scene=name, cols=cols):
                self.assertEqual(frame.geometry, (cols, 45))
                self.assertEqual(len(frame.lines), 45)
                self.assertEqual([i for i, line in enumerate(frame.lines) if cell_width(line) != cols], [])

    def test_regions_are_the_approved_rectangles(self):
        for name in COMPARED + BANDS:
            for cols in (100, 168):
                frame = self.frames[(name, cols)]
                for region, rect in self.scene(name)["widths"][str(cols)]["regions"].items():
                    with self.subTest(scene=name, cols=cols, region=region):
                        self.assertEqual(frame.region(region), Rect(*rect))

    def test_every_region_outside_the_output_rectangle_has_the_approved_text(self):
        for name in COMPARED:
            for cols in (100, 168):
                data, rows = self.rows_of(name, cols)
                frame = self.frames[(name, cols)]
                special = {i for span in self.allowed_rows(name, cols).values() for i in span}
                wording = any(a["id"] == "successor-wording" for a in self.scene(name).get("allowed", ()))
                out = data["output"]
                for i in rows:
                    actual, expected = frame.lines[i], data["rows"][i]
                    if out and out["y"] <= i < out["y"] + out["h"]:
                        actual = blank(actual, out["x"], out["x"] + out["w"])
                    if i in special:
                        actual, expected = split_row(actual)[0], split_row(expected)[0]
                    elif wording:
                        expected = pad(expected.replace("fixture에 ", ""), cols)
                    with self.subTest(scene=name, cols=cols, row=i + 1):
                        self.assertEqual(actual, expected)

    def test_the_fixed_bands_of_the_two_output_scenes_match(self):
        for name in BANDS:
            for cols in (100, 168):
                data, rows = self.rows_of(name, cols)
                frame = self.frames[(name, cols)]
                for i in rows:
                    with self.subTest(scene=name, cols=cols, row=i + 1):
                        self.assertEqual(frame.lines[i], data["rows"][i])

    # -- the explicitly allowed differences, each asserted ---------------------------------------

    def test_successor_wording_has_no_fixture_word(self):
        for name in ("overview", "source-failed"):
            for cols in (100, 168):
                text = "\n".join(self.frames[(name, cols)].lines)
                self.assertIn("승계 ? · 확인 근거 없음", text)
                self.assertNotIn("fixture", text)

    def test_the_five_duties_are_the_approved_ones_and_listed_oldest_first(self):
        for name in ("overview", "source-failed"):
            for cols in (100, 168):
                data, _rows = self.rows_of(name, cols)
                first, last = next(e for e in self.scene(name)["allowed"] if e["id"] == "p4-duty-order")["rows"][str(cols)]
                approved = [split_row(row)[1].strip() for row in data["rows"][first:last]]
                actual = [split_row(row)[1].strip() for row in self.frames[(name, cols)].lines[first:last]]
                pairs = lambda rows: {(re.sub(r"^\d+\. ", "", rows[i]), rows[i + 1]) for i in range(0, len(rows), 2)}
                self.assertEqual(pairs(actual), pairs(approved))
                ages = [int(re.search(r"(\d+)분 전", detail).group(1)) for detail in actual[1::2]]
                self.assertEqual(ages, sorted(ages, reverse=True))
                self.assertEqual(ages, [214, 70, 56, 23, 14])

    def test_history_replacement_section_belongs_to_the_replaced_request_only(self):
        openrig = tab(collect(self.fx), "openrig 조사")
        view = collect(self.fx, request_id=openrig.identity).request_ledger
        approved_lines = replacement_lines(view)
        self.assertEqual(len(approved_lines), 4)
        for cols in (100, 168):
            data, _rows = self.rows_of("history", cols)
            first, last = self.allowed_rows("history", cols)["history-replacement-section"][0], \
                self.allowed_rows("history", cols)["history-replacement-section"][-1] + 1
            approved = [split_row(row)[1].strip() for row in data["rows"][first:last]]
            # The approved screen spells the request "OpenRig 조사"; its cycle title is "openrig 조사".
            self.assertEqual(approved[0].casefold(), approved_lines[0].casefold())
            self.assertEqual(approved[1:], list(approved_lines[1:]))
            self.assertEqual([split_row(row)[1].strip()
                              for row in self.frames[("history", cols)].lines[first:last]], [""] * 4)


class OutputPaneAndBroken(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.screens = load_screens()["scenes"]

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_output_pane_shows_the_selected_stages_raw_output_not_every_job(self):
        for cols, out in ((100, (27, 22, 73, 18)), (168, (104, 4, 64, 36))):
            frame = scene_frame(self.fx, self.screens["first"], cols)
            rect = frame.region("output")
            self.assertEqual((rect.x, rect.y, rect.w, rect.h), out)
            pane = [cells(line, rect.x, rect.x + rect.w).rstrip() for line in frame.lines[rect.y:rect.y + rect.h]]
            self.assertEqual(pane[0], "test · CX · 원래 출력")
            self.assertTrue(all(line.startswith("[test] CX ") for line in pane[1:] if line), pane)
            self.assertEqual(sum(1 for line in pane if line), min(len(pane), 21))

    def test_without_a_reader_the_pane_keeps_the_ledger_summary(self):
        scene = self.screens["first"]
        base = collect(self.fx)
        labels = {item.label: item.identity for item in base.tabs}
        result = collect(self.fx, request_id=labels["b6-report"], stage_id="test")
        frame = compose(self.fx.snapshot, result, scene_state(self.fx, scene, result), 168, 45, now=self.fx.now)
        rect = frame.region("output")
        pane = [cells(line, rect.x, rect.x + rect.w).rstrip() for line in frame.lines[rect.y:rect.y + rect.h]]
        self.assertEqual(pane[0], "test · CX · 요약")
        self.assertIn("원문 읽는 중", pane)

    def test_broken_tab_keeps_its_own_previous_screen_and_age_and_drops_the_output_pane(self):
        for cols in (100, 168):
            frame = scene_frame(self.fx, self.screens["tab-broken"], cols)
            ledger = frame.region("ledger")
            self.assertEqual(frame.errors, (("ledger", "RuntimeError: tab renderer failed"),))
            self.assertEqual(cells(frame.lines[ledger.y], ledger.x).rstrip(), "그리지 못함 · 직전 화면 2분 전")
            self.assertEqual(cells(frame.lines[ledger.y + 1], ledger.x).rstrip(), "직전 화면")
            self.assertEqual(cells(frame.lines[ledger.y + 2], ledger.x).rstrip(), "b6-report · 완료 근거")
            self.assertEqual((frame.region("output").w, frame.region("output").h), (0, 0))
            self.assertEqual(frame.page, "ledger")

    def test_source_failure_adds_the_age_to_the_status_row_and_the_relations_page(self):
        frame = scene_frame(self.fx, self.screens["source-failed"], 100)
        self.assertIn("compute 0s · 관계 읽지 못함 2분 │", frame.lines[1])
        self.assertTrue(frame.lines[4].split(BORDER)[1].startswith("관계 읽지 못함 2분 · 직전 값"))
        self.assertEqual(frame.page, "relations")

    def test_empty_snapshot_still_fills_both_widths(self):
        for cols in (100, 168):
            frame = compose(cols=cols, rows=45)
            self.assertEqual(len(frame.lines), 45)
            self.assertEqual([i for i, line in enumerate(frame.lines) if cell_width(line) != cols], [])

    def test_result_helper_injects_events_only_from_the_fixture_source(self):
        result = scene_result(self.fx, self.screens["first"])
        self.assertEqual(len(result.events), len(self.fx.source["events"]))


class CanonicalOutputRows(unittest.TestCase):
    """The approved B-first / B-inflight / B-output-oc output rectangles, row for row, from real sources.

    The text, the glyphs, the labels, the times, the ``(+k줄)`` counts and the row order are the approved
    ones; the only change is the stand-in for the capture machine's home path (same width), applied to the
    fixture and to the expected rows alike. B-inflight and B-output-oc are drawn without a ledger column
    in the approved screens, so their rows are compared at the approved output width."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.fx = ef.materialize(cls.temp.name)
        cls.scenes = ef.load_screens()["scenes"]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def compare(self, name, result, state, now):
        for width in ("100", "168"):
            geometry = self.scenes[name]["output_rows"][width]
            rect = Rect(geometry["x"], geometry["y"], geometry["w"], geometry["h"])
            rows, _ = output_pane(result, state, rect, now)
            actual = [row.rstrip() for row in rows] + [""] * (rect.h - len(rows))
            for index, expected in enumerate(geometry["rows"]):
                with self.subTest(scene=name, width=width, row=index):
                    self.assertEqual(actual[index], expected)

    def test_b_first_the_selected_stage_of_a_codex_attempt(self):
        scene = self.scenes["first"]
        result = ef.scene_result(self.fx, scene)
        self.assertEqual(result.output_title, "test · CX · 원래 출력")
        self.compare("first", result, ef.scene_state(self.fx, scene, result), self.fx.now)

    def test_b_inflight_supervisor_claude_and_stage_codex_mixed_by_native_time(self):
        scene = self.scenes["inflight"]
        result = ef.scene_result(self.fx, scene)
        self.assertEqual(result.output_title, "fleet-explorer · 감독 CL + 단계 CX")
        self.compare("inflight", result, ef.scene_state(self.fx, scene, result), self.fx.now)
        times = [row.text.split(" ")[0] for row in result.output]
        self.assertEqual(times, sorted(times))
        self.assertEqual({row.text.split(" ")[1] for row in result.output}, {"[owner]", "[material-strategy]"})

    def test_b_output_oc_an_opencode_owner_from_its_database_in_both_generations(self):
        for generation in ("v1", "v2"):
            with self.subTest(generation=generation), tempfile.TemporaryDirectory() as root:
                jobs, attempt, _sid, _db = ef.materialize_opencode(root, generation)
                owner = AttemptRow(attempt_id=attempt, line=1, status="done", slug="oc-owner",
                                   harness="opencode", worker_type="owner")
                tab = RequestTab(identity="t", label="ac-cmd-vad-sap-leg2", attempt_ids=(attempt,))
                key = FocusKey("opencode", "ses_main", "t")
                title, plans, stamped = output_plans(key, tab, [owner])
                self.assertEqual((title, stamped), ("ac-cmd-vad-sap-leg2 · 감독 OC", False))
                env = {"AGENT_DISPATCH_JOBS": jobs, "AGENT_HOME": root + "/h", "XDG_STATE_HOME": root + "/x"}
                with mock.patch.dict(os.environ, env):
                    view = ef.output_reader().read(key, title, plans)
                result = FocusResult(key, 1, 1, output=view.rows, output_title=view.title,
                                     output_notes=view.notes, output_health=view.health)
                self.assertEqual(view.health.state, "success")
                self.compare("output-oc", result, ExplorerState(tab="t"), 0)


if __name__ == "__main__":
    unittest.main()
