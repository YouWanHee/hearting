"""File-generation reuse contract for parsed registry rows."""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet.collectors import dispatch


class ParsedRegistryRowsTest(unittest.TestCase):
    def test_fast_spacing_guard_matches_the_legacy_regex(self):
        legacy_pattern = re.compile(r"(?:^|,)[^=,]*\s[^=,]*=|=\s|\s+[^\s,]*=")
        examples = (
            "capability=code,mode=dev",
            "model_role=deep  maker,mode=dev",
            "model_role=deep maker=fast",
            "model_role=deep\t maker",
            "capability=code, model_role=deep maker",
            "capability=code,note=foo,bar",
            "capability:code(x)",
            "a=wide\u2003b=value",
            "a=wide\u2003value",
            "a=\u2003wide",
        )
        for raw in examples:
            eq_pos, colon_pos = raw.find("="), raw.find(":")
            expected = ("(" in raw or eq_pos < 0
                        or colon_pos >= 0 and colon_pos < eq_pos
                        or any("=" not in part for part in raw.split(","))
                        or bool(legacy_pattern.search(raw)))
            self.assertEqual(dispatch._registry_metadata_needs_legacy_parser(raw),
                             expected, raw)

    def test_comma_delimited_role_values_reuse_the_existing_generation_parse(self):
        raw = "capability=autopilot-code,model_role=deep  maker,mode=dev"
        expected = dispatch._parse_pipe_meta_uncached(raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("t\topen\t/repo\t/work\towner\t" + raw + "\n")
            with dispatch._dispatch_registry_cache.registry_read_scope(), \
                 mock.patch.object(dispatch, "_parse_pipe_meta", side_effect=AssertionError("reparsed")):
                rows = dispatch.parsed_registry_rows(path)
        self.assertEqual(dict(rows[0][2]), expected)

    def test_noncanonical_metadata_keeps_the_legacy_parser_semantics(self):
        examples = ["capability=autopilot-code model_role=deep maker",
                    "capability=autopilot-code,model_role= deep maker",
                    "capability=autopilot-code,note=foo =bar",
                    "capability=autopilot-code,note=foo,bar",
                    "capability=autopilot-code, model_role=deep maker"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("".join("t\topen\t/repo\t/work\towner\t" + raw + "\n"
                                    for raw in examples))
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
        self.assertEqual([dict(row[2]) for row in rows],
                         [dispatch._parse_pipe_meta_uncached(raw) for raw in examples])

    def test_collection_adapts_registry_records_without_reparsing_common_metadata(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text(
                "open\tslug\t/repo\t/work\tstamp\tcapability=autopilot-code,mode=dev\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual(rows[0][2]["capability"], "autopilot-code")
            self.assertEqual(rows[0][2]["_name"], "code")
            self.assertEqual(rows[0][2]["mode"], "dev")
            self.assertEqual(dispatch._parse_pipe_meta_cached.cache_info().misses, 0)

    def test_canonical_metadata_skips_legacy_whitespace_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            raw = "capability=autopilot-code,mode=dev,attempt_id=att-1"
            path.write_text("t\topen\t/repo\t/work\towner\t" + raw + "\n")
            with (dispatch._dispatch_registry_cache.registry_read_scope(),
                  mock.patch.object(dispatch, "_parse_pipe_meta",
                                    side_effect=AssertionError("canonical metadata reparsed"))):
                rows = dispatch.parsed_registry_rows(path)
        self.assertEqual(dict(rows[0][2]), dispatch._parse_pipe_meta_uncached(raw))

    def test_attempt_index_preserves_first_duplicate_registry_row(self):
        first = {"order": 1, "meta": {"attempt_id": "att-1", "value": "first"}}
        duplicate = {"order": 2, "meta": {"attempt_id": "att-1", "value": "later"}}
        missing = {"order": 3, "meta": {"value": "missing-attempt"}}
        index = dispatch._index_registry_rows_by_attempt([first, duplicate, missing])
        self.assertIs(index["att-1"], first)
        self.assertIs(index[None], missing)

    def test_orphan_row_adapter_matches_dispatch_registry_contract(self):
        registry = dispatch._orphan_registry_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text(
                "t\topen\t/repo\t/work\towner\tcapability=code,attempt_id=att-1\n"
                "malformed\trow\n"
                "t\tdone\t/repo\t/work\tlegacy\tcapability:code(x)\n"
                "t\topen\t/repo\t/work\tworker\tmodel_role=deep  maker,attempt_id=att-2\n",
                encoding="utf-8")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                parsed = dispatch.parsed_registry_rows(path)
                expected_lines = ["\t".join(fields) for _order, fields, _meta in parsed]
                expected = registry.read_rows(path, lines=expected_lines)
                for row, (physical_order, fields, _meta) in zip(expected, parsed):
                    row["order"] = physical_order
                    row["raw"] = "\t".join(fields)
                actual = dispatch._orphan_registry_rows(path, registry)
        self.assertEqual(actual, expected)

    def test_orphan_rows_are_shared_only_through_explicit_pass_cache(self):
        registry = object()
        rows = [{"order": 0, "meta": {"attempt_id": "att-1"}}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            cache = {}
            with mock.patch.object(dispatch, "_orphan_registry_rows",
                                   return_value=rows) as reader:
                first = dispatch._orphan_registry_rows_for_pass(path, registry, cache)
                second = dispatch._orphan_registry_rows_for_pass(
                    os.path.abspath(path), registry, cache)
                self.assertIs(first, second)
                self.assertEqual(reader.call_count, 1)
                dispatch._orphan_registry_rows_for_pass(path, registry, None)
                dispatch._orphan_registry_rows_for_pass(path, registry, None)
                self.assertEqual(reader.call_count, 3)

    def test_collection_falls_back_for_legacy_and_whitespace_continuation_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            legacy = "open\tlegacy\t/repo\t/work\tstamp\tautopilot-code:dev(x)\n"
            continuation = (
                "open\tworker\t/repo\t/work\tstamp\t"
                "capability=autopilot-code model_role=deep maker\n")
            path.write_text(legacy + continuation)
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual(rows[0][2]["_name"], "code")
            self.assertEqual(rows[0][2]["mode"], "dev")
            self.assertEqual(rows[1][2]["model_role"], "deep maker")

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

    def test_multiple_appends_reuse_each_prefix_without_dropping_or_repeating_rows(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("\nmalformed\ndate\topen\t/repo\t/work\tslug0\tcapability=cap0\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[0] for row in rows], [2])
            misses = dispatch._parse_pipe_meta_cached.cache_info().misses

            expected_orders = [2]
            for index in range(1, 4):
                with path.open("a") as stream:
                    stream.write(f"date\topen\t/repo\t/work\tslug{index}\tcapability=cap{index}\n")
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    rows = dispatch.parsed_registry_rows(path)
                expected_orders.append(index + 2)
                self.assertEqual([row[0] for row in rows], expected_orders)
                self.assertEqual(
                    [row[1][4] for row in rows], [f"slug{i}" for i in range(index + 1)])
                self.assertEqual(
                    dispatch._parse_pipe_meta_cached.cache_info().misses, misses + index)

    def test_cache_is_isolated_by_path_and_file_generation(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.log"
            second_path = Path(directory) / "second.log"
            first_path.write_text("date\topen\t/repo\t/work\tfirst\tcapability=first\n")
            second_path.write_text("date\topen\t/repo\t/work\tsecond\tcapability=second\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                first = dispatch.parsed_registry_rows(first_path)
                second = dispatch.parsed_registry_rows(second_path)
            self.assertEqual(first[0][1][4], "first")
            self.assertEqual(second[0][1][4], "second")

            first_path.write_text(
                "date\topen\t/repo\t/work\treplaced\tcapability=replaced\n"
                "date\topen\t/repo\t/work\tnew\tcapability=new\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                replaced = dispatch.parsed_registry_rows(first_path)
                unchanged_second = dispatch.parsed_registry_rows(second_path)
            self.assertEqual([row[1][4] for row in replaced], ["replaced", "new"])
            self.assertIs(second, unchanged_second)

    def test_missing_registry_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(dispatch.parsed_registry_rows(Path(directory) / "missing"), ())

    def test_large_generation_keeps_every_unique_row_after_parser_lru_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            count = 8205
            path.write_text("".join(
                f"open\tslug-{index}\t/repo\t/work\tstamp\tkey=value-{index}\n"
                for index in range(count)))
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual(len(rows), count)
            self.assertEqual(rows[0][0], 0)
            self.assertEqual(rows[-1][0], count - 1)
            self.assertEqual(rows[0][2]["key"], "value-0")
            self.assertEqual(rows[-1][2]["key"], f"value-{count - 1}")

    def test_incomplete_tail_is_omitted_until_its_newline_arrives(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            first = "stamp\topen\t/repo\t/work\tfirst\tkey=first\n"
            tail = "stamp\topen\t/repo\t/work\tsecond\tkey=second"
            path.write_text(first + tail)
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[1][4] for row in rows], ["first"])

            with path.open("a") as stream:
                stream.write("\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                completed = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[1][4] for row in completed], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
