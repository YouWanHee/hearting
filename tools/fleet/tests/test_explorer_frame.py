import unittest

from tools.fleet.explorer.frame import compose
from tools.fleet.explorer.output import OutputLine, cell_width, render_output
from tools.fleet.explorer.types import Rect


class ExplorerFrameTests(unittest.TestCase):
    def test_rect_uses_half_open_geometry(self):
        rect = Rect(2, 3, 4, 5)
        self.assertTrue(rect.contains(2, 3))
        self.assertFalse(rect.contains(6, 3))
        self.assertFalse(rect.contains(2, 8))

    def test_b_frame_geometry_and_cell_width_at_canonical_sizes(self):
        for cols in (100, 168):
            frame = compose(cols=cols, rows=45, providers={"header": ["작업 장부"]})
            self.assertEqual(frame.geometry, (cols, 45))
            self.assertEqual(len(frame.lines), 45)
            self.assertTrue(all(cell_width(line) == cols for line in frame.lines))
            self.assertIsNotNone(frame.region("nav"))
            self.assertGreater(frame.region("nav").w, 0)
            if cols == 100:
                self.assertGreater(frame.region("output").y, frame.region("ledger").y)
            else:
                self.assertEqual(frame.region("ledger").y, frame.region("output").y)
                self.assertGreater(frame.region("output").x, frame.region("ledger").x)

    def test_each_region_keeps_its_own_last_good_after_provider_error(self):
        first = compose(cols=168, rows=45, providers={"ledger": ["기존 장부"], "nav": ["탐색"]})

        def broken(_rect):
            raise RuntimeError("fixture failure")

        second = compose(cols=168, rows=45, providers={"ledger": broken, "nav": ["새 탐색"]},
                         last_good=first)
        self.assertEqual(second.errors[0][0], "ledger")
        self.assertIn("기존 장부", second.lines[second.region("ledger").y])
        self.assertIn("새 탐색", second.lines[second.region("nav").y])

    def test_output_is_one_row_per_record_and_clips_by_display_cells(self):
        rows = render_output([OutputLine("한글 record with long body", 3), "second\nphysical line"], 14, 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(cell_width(row) <= 14 for row in rows))
        self.assertIn("…", rows[0])
        self.assertNotIn("physical line", rows[1])


if __name__ == "__main__":
    unittest.main()
