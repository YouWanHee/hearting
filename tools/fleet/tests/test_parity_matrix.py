"""Synthetic tests for the matrix state inventory, diff and defect bookkeeping."""
import unittest
from types import SimpleNamespace

from tools.fleet.tests.parity_compare import diff_frames
from tools.fleet.tests.parity_matrix import detect_reference_defects
from tools.fleet.tests.parity_states import enumerate_states


class MatrixParityTests(unittest.TestCase):
    def test_enumeration_includes_interaction_dimensions(self):
        rows = enumerate_states(vm={"repos": [{"id": "r"}],
                                    "sessions": [{"tag": "s"}],
                                    "routes": [{"id": "rt"}]})
        ids = {row["id"] for row in rows}
        self.assertTrue({"usage-open", "OVERVIEW-scroll:1", "repo:r", "session:s",
                         "route:rt", "HISTORY-generation:0"}.issubset(ids))

    def test_reference_defect_coordinates_can_be_excluded_from_cell_diff(self):
        cells = [[("x", "d", None), ("界", "d", None)]]
        reference = SimpleNamespace(cells=cells, hits=[[0, 1, 3, ["x"]]], cols=2, rows=1)
        fleet = SimpleNamespace(cells=[[('x', 'd', None), ('y', 'd', None)]],
                                hits=reference.hits, cols=2, rows=1)
        defects = detect_reference_defects(reference, 2, pane_x=1)
        self.assertTrue(defects)
        # The explicit region excludes only the reported edge cell.
        self.assertEqual(diff_frames(reference, fleet, ((0, 1, 2),)), [])


if __name__ == "__main__":
    unittest.main()
