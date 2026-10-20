import unittest

from tools.fleet.explorer.frame import compose
from tools.fleet.explorer.output import cell_width
from tools.fleet.tests.explorer_fixture import load_screens


class ExplorerScreenFixtureTests(unittest.TestCase):
    def test_all_canonical_scenes_match_region_geometry_and_width(self):
        fixture = load_screens()
        self.assertEqual(len(fixture["scenes"]), 9)
        for scene_name, scene in fixture["scenes"].items():
            for width, expected in scene["widths"].items():
                with self.subTest(scene=scene_name, width=width):
                    cols = int(width)
                    state = {"active_region": "output" if scene_name == "output-oc" else "nav"}
                    frame = compose(cols=cols, rows=45, state=state)
                    self.assertEqual(len(frame.lines), 45)
                    self.assertTrue(all(cell_width(line) == cols for line in frame.lines))
                    regions = {name: rect for name, rect in frame.regions}
                    for name, expected_rect in expected["regions"].items():
                        actual = regions[name]
                        self.assertEqual(
                            (actual.x, actual.y, actual.w, actual.h),
                            (expected_rect["x"], expected_rect["y"],
                             expected_rect["w"], expected_rect["h"]),
                        )


if __name__ == "__main__":
    unittest.main()
