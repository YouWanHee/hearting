"""Content of the composed B screen against the approved screens (not only their geometry).

Every scene of ``fixtures/explorer_b/screens.json`` is composed from the fixture snapshot and the
selected-scope result at 100 and 168 columns, 45 rows. Every row outside the raw-output rectangle
must equal the approved text; the few cells where the product text is allowed to differ are listed
in the scene's ``allowed`` and each of them is asserted explicitly below.
"""
import re
import tempfile
import unittest

from tools.fleet.explorer.frame import compose
from tools.fleet.explorer.ledger import replacement_lines
from tools.fleet.explorer.output import cell_width
from tools.fleet.explorer.types import Rect
from tools.fleet.tests.explorer_fixture import (cells, collect, load_screens, materialize, scene_frame,
                                                scene_result, tab)

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

    def test_output_pane_shows_the_ledger_summary_not_every_job(self):
        for cols, out in ((100, (27, 22, 73, 18)), (168, (104, 4, 64, 36))):
            frame = scene_frame(self.fx, self.screens["first"], cols)
            rect = frame.region("output")
            self.assertEqual((rect.x, rect.y, rect.w, rect.h), out)
            pane = [cells(line, rect.x, rect.x + rect.w).rstrip() for line in frame.lines[rect.y:rect.y + rect.h]]
            self.assertEqual(pane[0], "test · CX · 요약")
            self.assertIn("원문 읽는 중", pane)
            self.assertEqual(sum(1 for line in pane if line), 4)

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


if __name__ == "__main__":
    unittest.main()
