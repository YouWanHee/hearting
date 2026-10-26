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
        raw["items"].append({"id": "S3:old-owner", "source": "S3", "kind": "blocked owner",
                             "question": "PRIVATE-BLOCKER-SENTINEL", "choices": ["SECRET"],
                             "route_id": "route-123456789", "attempt_id": "attempt-abcdefgh",
                             "since": 91, "owner": {"harness": "claude", "session_id": None},
                             "attribution": "unattributed", "stale": "current"})
        raw["items"][0]["stale"] = "current"
        view = fleet_answer_wait_view(raw)
        snapshot = SimpleNamespace(jobs=[], sessions=[], answer_waits=view, compute_hosts=None)
        self.assertIn("답변 필요 2", job_summary(snapshot))
        self.assertNotIn("◑답 대기", job_summary(snapshot))
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

    def test_fleet_projection_has_no_private_fields_and_summary_reserves_half_for_s2(self):
        result = {"items": [
            {"id": "s1", "source": "S1", "kind": "approval", "since": 100,
             "owner": {"harness": "codex", "session_id": "a", "seat_key": "PRIVATE-SEAT"},
             "question": "PRIVATE-QUESTION", "choices": ["PRIVATE-CHOICE"],
             "recommendation": "PRIVATE-REC", "stale": False},
            {"id": "s1q", "source": "S1", "kind": "elicitation", "since": 100,
             "owner": {"harness": "opencode", "session_id": "q"},
             "question": "PRIVATE-QUESTION", "stale": False},
            {"id": "s2", "source": "S2", "kind": "decision", "since": 100,
             "route_id": "route", "gate_id": "gate", "owner": {"harness": None,
             "session_id": None}, "question": "PRIVATE-QUESTION", "stale": False},
            {"id": "s3", "source": "S3", "kind": "decision", "since": 100,
             "attempt_id": "att", "owner": {"harness": "claude", "session_id": "b"},
             "question": "PRIVATE-BLOCKER", "stale": True},
            {"id": "s3-old", "source": "S3", "kind": "decision", "since": 90,
             "attempt_id": "older", "owner": {"harness": None, "session_id": None},
             "question": "PRIVATE-OLD-S3", "stale": True},
            {"id": "s4", "source": "S4", "kind": "decision", "since": 100,
             "owner": {"harness": "claude", "session_id": "c"},
             "question": "PRIVATE-QUESTION", "stale": False},
        ], "complete": True, "observed_at": 101}
        self.assertTrue(human_wait_state.publish_cache(result))
        payload = human_wait_state._fleet_cache_path().read_bytes()
        self.assertNotIn(b'"question"', payload)
        self.assertNotIn(b'"choices"', payload)
        self.assertNotIn(b'"recommendation"', payload)
        self.assertNotIn(b'"seat_key"', payload)
        for sentinel in (b"PRIVATE-QUESTION", b"PRIVATE-CHOICE", b"PRIVATE-REC",
                         b"PRIVATE-SEAT", b"PRIVATE-BLOCKER", b"PRIVATE-OLD-S3"):
            self.assertNotIn(sentinel, payload)
        view = human_wait_state.cached_fleet_view(now=101, request=False)
        self.assertEqual([item["source"] for item in view["items"]],
                         ["S1-approval", "S1-question", "S2", "S3", "S3", "S4"])
        self.assertEqual([item["kind"] for item in view["items"]],
                         ["approval", "decision", "human gate", "blocked owner",
                          "blocked owner", "marker"])
        self.assertEqual([item["stale"] for item in view["items"]],
                         ["current", "current", "current", "older", "older", "current"])
        self.assertEqual([item.get("pane_id") for item in view["items"]],
                         [None, None, None, None, None, None])
        self.assertEqual(view["total"], 4)
        self.assertEqual(view["older"], 2)
        self.assertEqual(view["unattributed"], ["s2", "s3-old"])
        ids_before = [item["id"] for item in view["items"]]
        self.assertTrue(human_wait_state.publish_cache(result))
        second = human_wait_state.cached_fleet_view(now=101, request=False)
        self.assertEqual([item["id"] for item in second["items"]], ids_before)
        snapshot = SimpleNamespace(jobs=[], sessions=[], answer_waits=fleet_answer_wait_view(view),
                                   compute_hosts=None)
        summary = job_summary(snapshot)
        self.assertIn("◑답 대기 1", summary)
        self.assertIn("답변 필요 3", summary)

    def test_fleet_reader_does_not_fall_back_to_private_projection_cache(self):
        private_path = human_wait_state._cache_path()
        private_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_path.parent.chmod(0o700)
        private_path.write_text(
            '{"schema_version":1,"observed_at":101,"complete":true,'
            '"items":[{"id":"private","question":"PRIVATE-QUESTION-SENTINEL"}]}',
            encoding="utf-8")
        private_path.chmod(0o600)

        self.assertEqual(human_wait_state._fleet_cache_path().name, "fleet.json")
        with mock.patch.object(
                human_wait_state, "_read_cache",
                side_effect=AssertionError("Fleet must not read projection.json")) as reader:
            self.assertIsNone(human_wait_state.cached_fleet_view(now=101, request=False))
        reader.assert_not_called()

    def test_expired_and_malformed_fleet_cache_are_unknown(self):
        path = human_wait_state._fleet_cache_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(
            '{"schema_version":1,"observed_at":101,"complete":true,'
            '"total":1,"items":[],"unattributed":[]}', encoding="utf-8")
        path.chmod(0o600)

        self.assertIsNone(human_wait_state.cached_fleet_view(
            now=101 + human_wait_state._DISPLAY_TTL + 1, request=False))
        path.write_text("{malformed", encoding="utf-8")
        self.assertIsNone(human_wait_state.cached_fleet_view(now=101, request=False))

    def test_incomplete_fleet_cache_keeps_global_count_unknown_on_every_surface(self):
        partial = self.result()
        partial["complete"] = False
        self.assertTrue(human_wait_state.publish_cache(partial))

        cached = human_wait_state.cached_fleet_view(now=101, request=False)
        view = fleet_answer_wait_view(cached)
        snapshot = SimpleNamespace(jobs=[], sessions=[], answer_waits=view,
                                   compute_hosts=None)

        self.assertEqual(human_wait_state.cached_count("claude", "sid-a", now=101),
                         (-1, 1))
        self.assertIn("◑답 대기 ?", job_summary(snapshot))
        self.assertNotIn("◑답 대기 0", job_summary(snapshot))
        lines = system_lines(snapshot)
        self.assertIn("답 대기 ? · 관측 미완료", lines)
        self.assertNotIn("답 대기 0", lines)

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

    def test_explorer_projects_only_current_safe_waits_and_counts_unattributed(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.live import build_view_model
        from fleet.explorer.state import NavState

        private = "PRIVATE-WAIT-BODY-SENTINEL"
        snapshot = {
            "sessions": [{"harness": "claude", "session_id": "sid-a", "session_tag": "a1",
                          "cwd": "/synthetic/repo", "liveness": "idle", "herdr_pane": "pane-a",
                          "summary": private,
                          "interaction_state": {"kind": "decision"}},
                         {"harness": "codex", "session_id": "sid-b", "session_tag": "b1",
                          "cwd": "/synthetic/repo", "liveness": "blocked",
                          "interaction_state": {"kind": "decision"}}],
            "project_of": lambda _cwd: "repo",
            "answer_waits": {
                "items": [
                    {"id": "S1:approval", "source": "S1-approval", "kind": "approval",
                     "since": 100, "stale": "current", "harness": "claude", "session_id": "sid-a",
                     "question": private, "choices": ["PRIVATE-CHOICE-SENTINEL"]},
                    {"id": "S2:unattributed", "source": "S2", "kind": "human gate",
                     "since": 101, "stale": "current", "harness": None, "session_id": None,
                     "question": private},
                    {"id": "S4:older", "source": "S4", "kind": "decision",
                     "since": 90, "stale": "older", "harness": "claude", "session_id": "sid-a"},
                ],
                "by_session": {}, "unattributed": ["S2:unattributed"],
                "total": 2, "older": 1, "complete": True, "health": [],
            },
            "resources": [], "jobs": [],
        }
        vm = build_view_model(snapshot, now=1000)
        self.assertEqual(len(vm.questions), 2)
        self.assertEqual(vm.system["answer_waits"], {"state": "complete", "total": 2, "older": 1})
        by_tag = {row["tag"]: row for row in vm.sessions}
        self.assertEqual(by_tag["a1"]["ask"], "answer needed")
        self.assertEqual(by_tag["a1"]["answer_wait_marker"], "●")
        self.assertIsNone(by_tag["b1"]["ask"])
        records = [dict(item) for item in vm.questions]
        self.assertEqual({item["tag"] for item in records}, {"S1:approval", "S2:unattributed"})
        self.assertEqual(next(item for item in records if item["tag"] == "S2:unattributed")["marker"], "◑")
        self.assertTrue(all(not ({"question", "choices", "title", "now"} & set(item))
                            for item in records))
        frame = draw(vm, NavState(view="QUESTIONS"), 140, 45)
        text = "\n".join(frame.text_rows())
        self.assertIn("2 questions", text)
        self.assertIn("older waits 1", text)
        self.assertNotIn(private, text)
        self.assertNotIn("PRIVATE-CHOICE-SENTINEL", text)

    def test_incomplete_explorer_wait_cache_never_presents_zero(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.live import build_view_model
        from fleet.explorer.state import NavState

        vm = build_view_model({"sessions": [], "resources": [],
                               "answer_waits": {"items": [], "total": 0, "older": 0,
                                                "complete": False,
                                                "health": [{"source": "cache", "state": "failed"}]}})
        self.assertEqual(vm.system["answer_waits"]["state"], "failed")
        self.assertIsNone(vm.system["answer_waits"]["total"])
        text = "\n".join(draw(vm, NavState(view="QUESTIONS"), 140, 45).text_rows())
        self.assertIn("? question", text)
        self.assertNotIn("0 questions", text)
        self.assertIn("? waiting", text)


if __name__ == "__main__":
    unittest.main()
