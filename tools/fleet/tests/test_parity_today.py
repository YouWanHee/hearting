"""Hermetic coverage for today's parity capture primitives."""
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from tools.fleet.tests.parity_compare import diff_frames
from tools.fleet.tests.parity_today import _frame_from_reference, _jsonable


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


if __name__ == "__main__":
    unittest.main()
