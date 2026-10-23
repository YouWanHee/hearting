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

    def test_global_count_and_unknown_attribution_are_snapshot_values(self):
        snapshot = SimpleNamespace(jobs=[], sessions=[], answer_waits=self.result(), compute_hosts=None)
        self.assertIn("◑답 대기 1", job_summary(snapshot))
        lines = system_lines(snapshot)
        self.assertIn("답 대기 1", lines)
        value = self.result()
        value["unattributed"] = ["S3:old-owner"]
        value["total"] = 2
        value["items"].append({"id": "S3:old-owner", "question": "오너가 답을 기다림"})
        snapshot.answer_waits = value
        lines = system_lines(snapshot)
        self.assertIn("귀속 못 한 항목 1", lines)
        self.assertTrue(any(line.strip() == "· 오너가 답을 기다림" for line in lines))

    def test_dispatch_owner_row_uses_the_shared_wait_item_chip(self):
        job = SimpleNamespace(interaction_observations=[{"kind": "decision", "item_id": "S3:a"}])
        self.assertEqual(_interaction_badge(job).strip(), "answer needed")

    def test_cached_status_count_is_global_and_exact_session_from_same_items(self):
        self.assertTrue(human_wait_state.publish_cache(self.result()))
        self.assertEqual(human_wait_state.cached_count("claude", "sid-a", now=101), (1, 1))
        self.assertIsNone(human_wait_state.cached_count("claude", "sid-a", now=200))
        partial = self.result()
        partial["complete"] = False
        self.assertTrue(human_wait_state.publish_cache(partial))
        self.assertEqual(human_wait_state.cached_count("claude", "sid-a", now=101), (-1, 1))

    def test_herdr_title_prefix_uses_the_shared_session_projection(self):
        with mock.patch.object(herdr_projection, "session_title", return_value="기존 제목"), \
             mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
             mock.patch.object(herdr_projection, "compose", return_value=("claude", "기존 제목")), \
             mock.patch("human_gate_observation.read_answer_waits", return_value=self.result()):
            command = herdr_projection._metadata_command("herdr", "claude", "sid-a", "pane-a")
        self.assertIn("--title", command)
        title = command[command.index("--title") + 1]
        self.assertIn("◑답변 필요", title)
        self.assertIn("기존 제목", title)


if __name__ == "__main__":
    unittest.main()
