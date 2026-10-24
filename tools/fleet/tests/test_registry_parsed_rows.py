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
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("open\tslug\t/repo\t/work\t2026-01-01\tcapability=code\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                first = dispatch.parsed_registry_rows(path)
                first_misses = dispatch._parse_pipe_meta_cached.cache_info().misses
                second = dispatch.parsed_registry_rows(path)
            self.assertIs(first, second)
            self.assertEqual(dispatch._parse_pipe_meta_cached.cache_info().misses, first_misses)
            self.assertEqual(first[0][0], 0)
            self.assertEqual(len(first[0][1]), 6)
            with self.assertRaises(TypeError):
                first[0][2]["mode"] = "changed"

            with path.open("a") as stream:
                stream.write("\n")
                stream.write("bad\trow\n")
            with path.open("a") as stream:
                stream.write("open\tslug2\t/repo\t/work\t2026-01-02\tmode=dev\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                third = dispatch.parsed_registry_rows(path)
            self.assertIsNot(first, third)
            self.assertEqual(
                dispatch._parse_pipe_meta_cached.cache_info().misses, first_misses + 1)
            self.assertEqual(len(third), 2)
            self.assertEqual(third[-1][0], 3)
            self.assertEqual(third[0], first[0])

    def test_registry_order_keeps_physical_zero_based_line_positions(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            valid = "open\tslug\t/repo\t/work\t2026-01-01\tcapability=code\n"
            path.write_text("\nmalformed\n" + valid)
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[0] for row in rows], [2])

            with path.open("a") as stream:
                stream.write("\n")
                stream.write("also\tmalformed\n")
                stream.write("open\tslug2\t/repo\t/work\t2026-01-02\tmode=dev\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                appended = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[0] for row in appended], [2, 5])

    def test_missing_registry_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(dispatch.parsed_registry_rows(Path(directory) / "missing"), ())


if __name__ == "__main__":
    unittest.main()
