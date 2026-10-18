#!/usr/bin/env python3

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch_registry_cache as CACHE  # noqa: E402


class RegistryLinesCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        # A distinct path per test keeps the process-wide cache from leaking
        # a stale entry from an earlier test into this one.
        CACHE.clear_cache()
        CACHE.reset_metrics()

    def test_unchanged_file_parses_only_once(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        second = CACHE.registry_lines(self.jobs)
        third = CACHE.registry_lines(self.jobs)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 1)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)
        self.assertEqual(CACHE.metrics_snapshot()["bytes_read"], self.jobs.stat().st_size)

    def test_parsed_views_are_shared_but_compatibility_dicts_are_isolated(self):
        self.jobs.write_text(
            "job\topen\t/repo\ttime\towner\tattempt_id=one,attempt_schema_version=2,parent_sid=owner\n",
            encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        returned = first.handover_latest()
        returned["one"][1]["parent_sid"] = "corrupted"
        second = CACHE.registry_view(self.jobs)
        self.assertIs(first, second)
        self.assertEqual(second.handover_latest()["one"][1]["parent_sid"], "owner")
        row, error = second.completion_attempt("one")
        self.assertIsNone(error)
        self.assertEqual(row.completion_metadata()["parent_sid"], "owner")
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 1)

    def test_completion_and_handover_metadata_keep_distinct_key_rules(self):
        self.jobs.write_text(
            "job\topen\t/repo\ttime\towner\tattempt_id=one,attempt_schema_version=2,parent_sid=exact, parent_sid=trimmed\n",
            encoding="utf-8")
        row = CACHE.registry_view(self.jobs).records[0]
        self.assertEqual(row.completion_metadata()["parent_sid"], "exact")
        self.assertEqual(row.handover_metadata()["parent_sid"], "trimmed")

    def test_same_size_rewrite_and_atomic_replace_rebuild_views(self):
        original = "job\topen\t/repo\ttime\towner\tf=1\n"
        replacement = "job	done\t/repo\ttime\towner\tf=1\n"
        self.assertEqual(len(original), len(replacement))
        self.jobs.write_text(original, encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        frozen_mtime = self.jobs.stat().st_mtime_ns
        self.jobs.write_text(replacement, encoding="utf-8")
        os.utime(self.jobs, ns=(frozen_mtime, frozen_mtime))
        second = CACHE.registry_view(self.jobs)
        self.assertNotEqual(first, second)
        self.assertEqual(second.lines[0].split("\t")[1], "done")

        staged = self.jobs.with_name("jobs.new")
        staged.write_text(original, encoding="utf-8")
        os.replace(staged, self.jobs)
        third = CACHE.registry_view(self.jobs)
        self.assertEqual(third.lines[0].split("\t")[1], "open")
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 3)

    def test_partial_final_row_is_reobserved_after_append(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1", encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        self.assertEqual(first.lines, ("a\tb\tc\td\te\tf=1",))
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write("\na\tb\tc\td\te\tf=2\n")
        second = CACHE.registry_view(self.jobs)
        self.assertEqual(len(second.lines), 2)
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 2)

    def test_trusted_append_reads_only_new_bytes_and_does_not_promote_partial_row(self):
        base = "job\topen\t/repo\ttime\towner\tattempt_id=base,attempt_schema_version=2\n"
        self.jobs.write_bytes(base.encode() +
                              b"job\topen\t/repo\ttime\towner\tattempt_id=n\xc3")
        first = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual(first.completion_attempt("néxt"), (None, None))
        suffix = b"\xa9xt,attempt_schema_version=2\n"
        with self.jobs.open("ab") as stream:
            stream.write(suffix)
        second = CACHE.registry_view(self.jobs, append_only=True)
        row, error = second.completion_attempt("néxt")
        self.assertIsNone(error)
        self.assertIsNotNone(row)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["full_metadata_parses"], 1)
        self.assertEqual(metrics["delta_reads"], 1)
        self.assertEqual(metrics["delta_bytes_read"], len(suffix))
        self.assertEqual(metrics["metadata_rows_parsed"], 2)
        self.assertEqual(second.lines, tuple(base.splitlines() +
                                              ["job\topen\t/repo\ttime\towner\tattempt_id=néxt,attempt_schema_version=2"]))

    def test_atomic_replacement_forces_full_rebuild_even_on_append_path(self):
        self.jobs.write_text("old\topen\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
        first = CACHE.registry_view(self.jobs, append_only=True)
        staged = self.jobs.with_name("jobs.new")
        staged.write_text("old\topen\t/repo\ttime\towner\tf=1\nnew\topen\t/repo\ttime\towner\tf=2\n",
                          encoding="utf-8")
        os.replace(staged, self.jobs)
        second = CACHE.registry_view(self.jobs, append_only=True)
        self.assertIsNot(first, second)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 2)
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)

    def test_handover_candidate_index_drops_attempt_after_parent_binding_changes(self):
        self.jobs.write_text(
            "job\topen\t/repo\ttime\towner\tattempt_id=one,parent_sid=old,"
            "owner_route_id=route,owner_route_hash=hash,route_node=execute\n",
            encoding="utf-8")
        first = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual(len(first.handover_candidates("old")), 1)
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write(
                "job\topen\t/repo\ttime\towner\tattempt_id=one,parent_sid=new,"
                "owner_route_id=route,owner_route_hash=hash,route_node=execute\n")
        second = CACHE.registry_view(self.jobs, append_only=True)
        self.assertEqual(second.handover_candidates("old"), ())
        self.assertEqual(len(second.handover_candidates("new")), 1)

    def test_append_after_observation_gap_rebuilds_instead_of_using_old_prefix(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_view(self.jobs, append_only=True)
        first.last_observed_at -= CACHE._APPEND_OBSERVATION_GAP_SECONDS + 1
        with self.jobs.open("a", encoding="utf-8") as stream:
            stream.write("a\tb\tc\td\te\tf=2\n")
        second = CACHE.registry_view(self.jobs, append_only=True)
        self.assertIsNot(first, second)
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 2)

    def test_path_cache_retains_a_bounded_lru(self):
        for number in range(CACHE._CACHE_MAX_PATHS + 1):
            path = Path(self.tmp.name) / f"jobs-{number}.log"
            path.write_text(f"a\tb\tc\td\te\tf={number}\n", encoding="utf-8")
            CACHE.registry_view(path)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["cache_entries"], CACHE._CACHE_MAX_PATHS)
        self.assertEqual(metrics["cache_evictions"], 1)

    def test_registry_cache_bytes_are_bounded_even_for_one_oversized_path(self):
        with mock.patch.object(CACHE, "_CACHE_MAX_BYTES", 8):
            self.jobs.write_text("large\topen\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
            view = CACHE.registry_view(self.jobs)
            self.assertEqual(view.lines[0].split("\t", 1)[0], "large")
            self.assertEqual(CACHE.metrics_snapshot()["cache_entries"], 0)
            self.assertEqual(CACHE.metrics_snapshot()["cache_bytes"], 0)

    def test_append_segments_compact_indexes_without_reparsing_history(self):
        self.jobs.write_text(
            "job\topen\t/repo\ttime\towner\tattempt_id=att-000,attempt_schema_version=2\n",
            encoding="utf-8")
        view = CACHE.registry_view(self.jobs, append_only=True)
        for number in range(1, 301):
            with self.jobs.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"job\topen\t/repo\ttime\towner\tattempt_id=att-{number:03d},"
                    "attempt_schema_version=2\n")
            view = CACHE.registry_view(self.jobs, append_only=True)
        self.assertLessEqual(len(view._segments), CACHE._MAX_SEGMENTS)
        self.assertEqual(view.record_count, 301)
        self.assertEqual(view.completion_attempt("att-000")[0].order, 0)
        self.assertEqual(view.completion_attempt("att-300")[0].order, 300)
        metrics = CACHE.metrics_snapshot()
        self.assertEqual(metrics["full_metadata_parses"], 1)
        self.assertEqual(metrics["metadata_rows_parsed"], 301)
        self.assertEqual(metrics["delta_reads"], 300)
        self.assertGreater(metrics["segment_compactions"], 0)

    def test_unstable_read_retries_and_returns_only_the_new_generation(self):
        self.jobs.write_text("old\topen\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
        real_read = os.read
        changed = [False]

        def rewrite_after_first_read(fd, size):
            data = real_read(fd, size)
            if not changed[0]:
                changed[0] = True
                self.jobs.write_text("new\tdone\t/repo\ttime\towner\tf=1\n", encoding="utf-8")
            return data

        with mock.patch.object(CACHE.os, "read", side_effect=rewrite_after_first_read):
            view = CACHE.registry_view(self.jobs)
        self.assertEqual(view.lines[0].split("\t", 1)[0], "new")
        self.assertEqual(CACHE.metrics_snapshot()["content_reads"], 1)

    def test_failed_observation_drops_old_view_and_recovers(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_view(self.jobs)
        self.jobs.write_text("x\tb\tc\td\te\tf=2\n", encoding="utf-8")
        with mock.patch.object(CACHE.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(OSError):
                CACHE.registry_view(self.jobs)
        self.jobs.unlink()
        with self.assertRaises(FileNotFoundError):
            CACHE.registry_view(self.jobs)
        self.jobs.write_text("x\tb\tc\td\te\tf=2\n", encoding="utf-8")
        recovered = CACHE.registry_view(self.jobs)
        self.assertIsNot(first, recovered)
        self.assertEqual(recovered.lines[0].split("\t", 1)[0], "x")

    def test_appended_write_invalidates_the_cache(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        self.assertEqual(len(first), 1)
        self.jobs.write_text(
            "a\tb\tc\td\te\tf=1\na\tb\tc\td\te\tf=2\n", encoding="utf-8"
        )
        second = CACHE.registry_lines(self.jobs)
        self.assertEqual(len(second), 2)
        # This fixture rewrites the file directly; size growth alone is not
        # writer evidence for the append-only fast path.
        self.assertEqual(CACHE.metrics_snapshot()["delta_reads"], 0)
        self.assertEqual(CACHE.metrics_snapshot()["full_metadata_parses"], 2)
        self.jobs.write_text("a\tb\tc\td\te\tf=3\n", encoding="utf-8")
        self.assertEqual(len(CACHE.registry_lines(self.jobs)), 1)

    def test_symlink_aliases_have_separate_slots_and_follow_new_generations(self):
        alias = Path(self.tmp.name) / "jobs-alias.log"
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        alias.symlink_to(self.jobs)
        canonical_view = CACHE.registry_view(self.jobs)
        alias_view = CACHE.registry_view(alias)
        self.assertIsNot(canonical_view, alias_view)
        self.assertEqual(canonical_view.lines, alias_view.lines)
        self.jobs.write_text("x\tb\tc\td\te\tf=2\n", encoding="utf-8")
        self.assertEqual(CACHE.registry_view(alias).lines[0].split("\t", 1)[0], "x")
        self.assertEqual(CACHE.registry_view(self.jobs).lines[0].split("\t", 1)[0], "x")

    def test_replaced_file_same_mtime_is_still_distinguished_by_size(self):
        # A same-second rewrite with a different byte count must not be
        # mistaken for the cached content merely because mtime resolution
        # (whole seconds on some filesystems) did not advance.
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        frozen_ns = self.jobs.stat().st_mtime_ns
        self.jobs.write_text(
            "a\tb\tc\td\te\tf=1\na\tb\tc\td\te\tf=2\n", encoding="utf-8"
        )
        os.utime(self.jobs, ns=(frozen_ns, frozen_ns))
        second = CACHE.registry_lines(self.jobs)
        self.assertNotEqual(first, second)
        self.assertEqual(len(second), 2)

    def test_missing_file_raises_like_a_direct_read(self):
        with self.assertRaises(FileNotFoundError):
            CACHE.registry_lines(self.jobs)

    def test_distinct_paths_cache_independently(self):
        other = Path(self.tmp.name) / "other.log"
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        other.write_text("x\tb\tc\td\te\tf=9\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        second = CACHE.registry_lines(other)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
