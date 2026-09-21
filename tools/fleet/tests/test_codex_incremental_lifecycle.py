"""Incremental Codex lifecycle regression tests for long append-only rollouts."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet.collectors import codex, liveness
from fleet.model import Session
from fleet.tests import benchmark_tick


FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "codex_lifecycle", "open_task_1_4m.json"
)


def _event(event_type, turn_id):
    return json.dumps({
        "type": "event_msg",
        "payload": {"type": event_type, "turn_id": turn_id},
    }) + "\n"


class IncrementalLifecycleTest(unittest.TestCase):
    def setUp(self):
        codex._LIFECYCLE_CACHE.clear()
        codex._LIFECYCLE_CACHE_EVICTIONS = 0

    def _write(self, path, *records):
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(records)

    def test_open_start_1_4m_before_eof_enriches_as_working(self):
        with open(FIXTURE, encoding="utf-8") as handle:
            fixture = json.load(handle)
        with tempfile.TemporaryDirectory() as tmp:
            sid = "01234567-89ab-cdef-0123-456789abcdef"
            path = os.path.join(tmp, "rollout-2026-09-21T00-00-00-%s.jsonl" % sid)
            note = json.dumps({
                "type": "event_msg",
                "payload": {"type": "note", "text": "x" * fixture["trailing_note_bytes"]},
            }) + "\n"
            self._write(path, _event("task_started", fixture["turn_id"]), note)
            sess = Session(harness="codex", pid=43210, cwd=tmp)
            codex._PROC_PATHS[sess.pid] = path
            with mock.patch.object(codex, "_config_model_effort", return_value=(None, None)), \
                 mock.patch.object(codex, "_thread_titles", return_value={}), \
                 mock.patch.object(codex, "_thread_subagents", return_value={}), \
                 mock.patch.object(codex, "_tail_token_count", return_value=None), \
                 mock.patch.object(codex, "_tail_open_tool_call", return_value=None), \
                 mock.patch.object(codex, "_tail_pending_request_user_input", return_value=None):
                codex.enrich(sess)
            with mock.patch.object(liveness, "_alive", return_value=True):
                state = liveness.classify(sess, now=os.path.getmtime(path) + 1)
            self.assertEqual(sess.task_lifecycle, fixture["expected_lifecycle"])
            self.assertEqual(state, fixture["expected_liveness"])
            self.assertEqual(sess.state_evidence["source"], fixture["expected_source"])
            self.assertEqual(
                codex._latest_task_lifecycle(path),
                ("task_started", fixture["turn_id"]),
            )

    def test_unchanged_reads_nothing_and_append_starts_at_saved_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "append.jsonl")
            self._write(path, _event("task_started", "turn"))
            first_size = os.path.getsize(path)
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            raw = codex._read_lifecycle_range
            with mock.patch.object(codex, "_read_lifecycle_range", wraps=raw) as read_range:
                self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
                read_range.assert_not_called()
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(_event("task_complete", "turn"))
                self.assertEqual(
                    codex._latest_task_lifecycle(path), ("task_complete", "turn")
                )
            self.assertEqual(read_range.call_count, 1)
            self.assertEqual(read_range.call_args.args[1], first_size)
            self.assertEqual(read_range.call_args.args[2], os.path.getsize(path))

    def test_partial_record_is_completed_by_only_the_next_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "partial.jsonl")
            self._write(path, _event("task_started", "turn"))
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            terminal = _event("task_complete", "turn")
            split = len(terminal) // 2
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(terminal[:split])
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(terminal[split:])
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_complete", "turn"))

    def test_default_and_full_modes_share_one_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "modes.jsonl")
            self._write(path, _event("task_started", "turn"))
            with mock.patch.object(
                codex, "_initialize_lifecycle_cursor",
                wraps=codex._initialize_lifecycle_cursor,
            ) as initialize:
                self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
                self.assertEqual(
                    codex._latest_task_lifecycle(path, max_scan=None),
                    ("task_started", "turn"),
                )
            initialize.assert_called_once()
            self.assertEqual(list(codex._LIFECYCLE_CACHE), [os.path.realpath(path)])

    def test_unrelated_malformed_json_is_ignored_but_bad_lifecycle_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "malformed.jsonl")
            self._write(path, _event("task_started", "turn"))
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("{not-json}\n")
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "turn"))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": ""},
                }) + "\n")
            self.assertIsNone(codex._latest_task_lifecycle(path))

    def test_rotation_and_truncation_fail_closed_before_reinitializing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "changing.jsonl")
            self._write(path, _event("task_started", "old"))
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "old"))
            replacement = os.path.join(tmp, "replacement.jsonl")
            self._write(replacement, _event("task_started", "new"))
            os.replace(replacement, path)
            self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertEqual(codex._latest_task_lifecycle(path), ("task_started", "new"))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}\n")
            self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertIsNone(codex._latest_task_lifecycle(path))

    def test_deletion_permission_read_and_stat_errors_drop_stale_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "errors.jsonl")
            canonical = os.path.realpath(path)

            self._write(path, _event("task_started", "delete"))
            codex._latest_task_lifecycle(path)
            os.unlink(path)
            self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(canonical, codex._LIFECYCLE_CACHE)

            self._write(path, _event("task_started", "permission"))
            codex._latest_task_lifecycle(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("{}\n")
            with mock.patch("builtins.open", side_effect=PermissionError):
                self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(canonical, codex._LIFECYCLE_CACHE)

            self.assertEqual(
                codex._latest_task_lifecycle(path), ("task_started", "permission")
            )
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("{}\n")
            with mock.patch.object(codex, "_read_lifecycle_range", side_effect=OSError):
                self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(canonical, codex._LIFECYCLE_CACHE)

            self.assertEqual(
                codex._latest_task_lifecycle(path), ("task_started", "permission")
            )
            with mock.patch.object(codex.os, "stat", side_effect=OSError):
                self.assertIsNone(codex._latest_task_lifecycle(path))
            self.assertNotIn(canonical, codex._LIFECYCLE_CACHE)

    def test_lru_is_bounded_by_file_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(codex._LIFECYCLE_CACHE_MAX + 1):
                path = os.path.join(tmp, "%04d.jsonl" % index)
                self._write(path, _event("task_started", str(index)))
                codex._latest_task_lifecycle(path)
            self.assertEqual(len(codex._LIFECYCLE_CACHE), codex._LIFECYCLE_CACHE_MAX)
            self.assertEqual(codex._LIFECYCLE_CACHE_EVICTIONS, 1)

    def test_live_budget_pins_exact_boundary_and_violation(self):
        result = {
            "benchmark_schema": benchmark_tick.BENCHMARK_SCHEMA,
            "mode": "live-descriptive-only",
            "warm_samples": [{"wall_ns": 11_000_000_000}],
        }
        self.assertTrue(benchmark_tick._live_budget(result, 11.0)["pass"])
        result["warm_samples"][0]["wall_ns"] += 1
        self.assertFalse(benchmark_tick._live_budget(result, 11.0)["pass"])


if __name__ == "__main__":
    unittest.main()
