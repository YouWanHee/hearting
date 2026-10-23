from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))

import human_wait_state
from fleet.explorer.system import job_summary, system_lines
from fleet.refresh import fleet_answer_wait_view
from fleet import herdr_projection
from fleet.render import _interaction_badge


class AnswerWaitSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def result(self):
        return {"items": [{"id": "S4:fixture", "source": "S4", "kind": "decision",
                           "question": "진행할까요?", "since": 100,
                           "owner": {"harness": "claude", "session_id": "sid-a"},
                           "attribution": "exact"}],
                "by_session": {("claude", "sid-a"): ["S4:fixture"]},
                "unattributed": [], "total": 1, "health": [], "observed_at": 101,
                "complete": True}

    def test_global_count_and_unattributed_description_hide_private_question(self):
        raw = self.result()
        raw["unattributed"] = ["S3:old-owner"]
        raw["total"] = 2
        raw["items"].append({"id": "S3:old-owner", "source": "S3", "kind": "decision",
                             "question": "PRIVATE-BLOCKER-SENTINEL", "choices": ["SECRET"],
                             "route_id": "route-123456789", "attempt_id": "attempt-abcdefgh",
                             "since": 91, "owner": {"harness": "claude", "session_id": None},
                             "attribution": "unattributed"})
        view = fleet_answer_wait_view(raw)
        snapshot = SimpleNamespace(jobs=[], sessions=[], answer_waits=view, compute_hosts=None)
        self.assertIn("◑답 대기 2", job_summary(snapshot))
        lines = system_lines(snapshot)
        self.assertIn("답 대기 2", lines)
        self.assertIn("귀속 못 한 항목 1", lines)
        line = next(line for line in lines if "owner 답 대기" in line)
        self.assertIn("Claude", line)
        self.assertIn("경로 23456789", line)
        self.assertIn("시도 abcdefgh", line)
        self.assertIn("1분 미만 경과", line)
        rendered = "\n".join(lines)
        self.assertNotIn("PRIVATE-BLOCKER-SENTINEL", rendered)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("PRIVATE-BLOCKER-SENTINEL", repr(snapshot.answer_waits))
        self.assertNotIn("question", repr(view["items"][1]))

    def test_dispatch_owner_row_uses_the_shared_wait_item_chip(self):
        job = SimpleNamespace(interaction_observations=[{"kind": "decision", "item_id": "S3:a"}])
        self.assertEqual(_interaction_badge(job).strip(), "answer needed")

    def test_cached_status_count_is_global_and_exact_session_from_same_items(self):
        self.assertTrue(human_wait_state.publish_cache(self.result()))
        self.assertEqual(human_wait_state.cached_count("claude", "sid-a", now=101), (1, 1))
        with mock.patch.object(human_wait_state, "_request_producer") as request:
            self.assertEqual(human_wait_state.cached_count("claude", "sid-a", now=200), (1, 1))
            request.assert_called_once_with()
        with mock.patch.object(human_wait_state, "_request_producer"):
            self.assertIsNone(human_wait_state.cached_count("claude", "sid-a", now=300))
        partial = self.result()
        partial["complete"] = False
        self.assertTrue(human_wait_state.publish_cache(partial))
        self.assertEqual(human_wait_state.cached_count("claude", "sid-a", now=101), (-1, 1))

    def test_herdr_title_prefix_uses_the_shared_session_projection(self):
        with mock.patch.object(herdr_projection, "session_title", return_value="기존 제목"), \
             mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
             mock.patch.object(herdr_projection, "compose", return_value=("claude", "기존 제목")), \
             mock.patch("human_wait_state.cached_view", return_value={
                 "items": self.result()["items"],
                 "by_session": {("claude", "sid-a"): ["S4:fixture"]}}):
            command = herdr_projection._metadata_command("herdr", "claude", "sid-a", "pane-a")
        self.assertIn("--title", command)
        title = command[command.index("--title") + 1]
        self.assertIn("◑답변 필요", title)
        self.assertIn("기존 제목", title)


if __name__ == "__main__":
    unittest.main()
