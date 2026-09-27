"""F-48 structured Codex request_user_input call/output pairing."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet.collectors import codex  # noqa: E402
from fleet.collectors import interaction, liveness  # noqa: E402
from fleet.model import Session  # noqa: E402


def record(payload, timestamp="2026-08-03T00:00:00Z"):
    return {"timestamp": timestamp, "type": "response_item", "payload": payload}


class CodexPendingTest(unittest.TestCase):
    def setUp(self):
        codex._LIFECYCLE_CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "rollout.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *rows):
        with open(self.path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(row if isinstance(row, str) else json.dumps(row))
                handle.write("\n")

    def call(self, call_id="c1", name="request_user_input", timestamp="2026-08-03T00:00:00Z"):
        return record({"type": "function_call", "name": name, "call_id": call_id}, timestamp)

    def output(self, call_id="c1"):
        return record({"type": "function_call_output", "call_id": call_id})

    def append(self, *rows):
        with open(self.path, "a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def event(self, kind, turn="t1"):
        return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn}}

    def test_open_and_closed_call(self):
        self.write(self.call())
        pending = codex._tail_pending_request_user_input(self.path)
        self.assertEqual(pending["call_id"], "c1")
        self.assertIsInstance(pending["waiting_since"], float)
        self.write(self.call(), self.output())
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_prose_and_other_function_are_not_evidence(self):
        prose = record({"type": "message", "content": "request_user_input should be used"})
        self.write(prose, self.call(name="exec_command"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_call_id_reuse_reopens_after_output(self):
        self.write(
            self.call("same", timestamp="2026-08-03T00:00:00Z"),
            self.output("same"),
            self.call("same", timestamp="2026-08-03T00:00:10Z"),
        )
        pending = codex._tail_pending_request_user_input(self.path)
        self.assertEqual(pending["call_id"], "same")
        self.assertGreater(pending["waiting_since"], 0)

    def test_latest_open_call_wins_and_malformed_is_skipped(self):
        self.write("{broken", self.call("old"), self.call("new"), "not json")
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "new")

    def test_absent_file_is_silent(self):
        self.assertIsNone(codex._tail_pending_request_user_input(self.path + ".missing"))

    def test_question_outside_tail_survives_unrelated_activity_without_rescan(self):
        self.write(self.event("task_started"), self.call(),
                   record({"type": "message", "content": "x" * 150000}))
        self.assertEqual(codex._tail_pending_request_user_input(self.path)["call_id"], "c1")
        with mock.patch.object(codex, "_read_lifecycle_range", wraps=codex._read_lifecycle_range) as read:
            self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
            read.assert_not_called()
            offset = os.path.getsize(self.path)
            self.append(self.output("foreign"))
            self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
            self.assertEqual(read.call_args.args[1], offset)
        self.append(self.output())
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_turn_boundaries_clear_abandoned_question(self):
        for kind in ("task_complete", "turn_aborted", "task_started"):
            with self.subTest(kind=kind):
                codex._LIFECYCLE_CACHE.clear()
                self.write(self.event("task_started"), self.call())
                self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
                self.append(self.event(kind))
                self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_foreign_turn_terminal_does_not_answer_question(self):
        self.write(self.event("task_started"), self.call(),
                   self.event("task_complete", "other"), self.event("turn_aborted", "other2"))
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        self.append(self.event("task_complete"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_partial_output_does_not_clear_until_complete_line(self):
        self.write(self.call())
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        output = json.dumps(self.output())
        with open(self.path, "a") as handle:
            handle.write(output)
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        with open(self.path, "a") as handle:
            handle.write("\n")
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_rotation_drops_old_question(self):
        self.write(self.call())
        self.assertIsNotNone(codex._tail_pending_request_user_input(self.path))
        os.rename(self.path, self.path + ".old")
        self.write(self.event("task_started", "new"))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))
        self.assertIsNone(codex._tail_pending_request_user_input(self.path))

    def test_enrichment_blocks_without_gateway_and_unblocks_after_answer(self):
        sid = "01234567-89ab-cdef-0123-456789abcdef"
        self.path = os.path.join(self.tmp.name, "rollout-test-%s.jsonl" % sid)
        self.write(self.event("task_started"), self.call())
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": self.tmp.name}), \
             mock.patch.object(codex, "_config_model_effort", return_value=(None, None)), \
             mock.patch.object(codex, "_thread_titles", return_value={}), \
             mock.patch.object(codex, "_thread_subagents", return_value={}), \
             mock.patch.object(codex, "_tail_token_count", return_value=None), \
             mock.patch.object(liveness, "_alive", return_value=True):
            for answered in (False, True):
                if answered:
                    self.append(self.output())
                sess = Session(harness="codex", pid=43210, cwd=self.tmp.name)
                with mock.patch.dict(codex._PROC_PATHS, {sess.pid: self.path}):
                    codex.enrich(sess)
                interaction.enrich(sess)
                state = liveness.classify(sess, now=os.path.getmtime(self.path) + 1)
                self.assertEqual(state, "working" if answered else "blocked")
                if not answered:
                    self.assertEqual(set(sess.interaction_state), {"kind", "source", "waiting_since"})
                    self.assertEqual(sess.interaction_state["source"], "codex-rollout")


if __name__ == "__main__":
    unittest.main()
