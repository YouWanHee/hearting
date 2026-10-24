"""File-generation reuse contract for parsed registry rows."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet.collectors import dispatch


class ParsedRegistryRowsTest(unittest.TestCase):
    def test_reuses_immutable_rows_until_file_generation_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("open\tslug\t/repo\t/work\t2026-01-01\tcapability=code\n")
            first = dispatch.parsed_registry_rows(path)
            second = dispatch.parsed_registry_rows(path)
            self.assertIs(first, second)
            self.assertEqual(first[0][0], 1)
            self.assertEqual(len(first[0][1]), 6)
            with self.assertRaises(TypeError):
                first[0][2]["mode"] = "changed"

            with path.open("a") as stream:
                stream.write("bad\trow\n")
            third = dispatch.parsed_registry_rows(path)
            self.assertIsNot(first, third)
            self.assertEqual(len(third), 1)

    def test_missing_registry_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(dispatch.parsed_registry_rows(Path(directory) / "missing"), ())


if __name__ == "__main__":
    unittest.main()
