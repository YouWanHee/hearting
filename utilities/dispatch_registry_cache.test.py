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
