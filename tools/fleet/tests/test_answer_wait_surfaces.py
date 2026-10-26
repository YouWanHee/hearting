from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace
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


class FleetQuestionActionTests(unittest.TestCase):
    def setUp(self):
        self.ref = {"id": "cache-q1", "harness": "codex", "session_id": "sid-q1",
                    "request_id": "req-q1", "turn_id": "turn-q1", "question_id": "question-q1"}

    def envelope(self, *, kind="question", state="open", detail="Which route should run?",
                 choices=None, draft=None):
        return {"v": 1, "harness": self.ref["harness"],
                "session_id": self.ref["session_id"], "request_id": self.ref["request_id"],
                "turn_id": self.ref["turn_id"], "question_id": self.ref["question_id"],
                "kind": kind, "detail": detail, "choices": choices,
                "draft": draft, "state": state, "support": {"supported": True},
                "native_ref": {"transport": "fixture", "epoch": 1}}

    def state(self, detail=None, *, draft=None, editing=False, pending=False):
        from fleet.explorer.state import NavState

        detail = detail or self.envelope(choices=["Continue", "Stop"])
        return NavState(view="QUESTIONS", folds=(("qsel:QUESTIONS", self.ref["id"]),),
                        question_overlay={"ref": dict(self.ref), "detail": detail,
                                          "draft": draft, "state": detail.get("state", "open"),
                                          "editing": editing, "pending": pending})

    def test_selected_detail_is_visible_only_in_private_overlay(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import serialize
        from fleet.explorer.view_model import ExplorerViewModel, persistable_view_model

        secret = "PRIVATE-QUESTION-DETAIL-SENTINEL"
        detail = self.envelope(detail=secret, choices=["Continue"])
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        text = "\n".join(draw(vm, self.state(detail), 140, 45).text_rows())

        self.assertIn(secret, text)
        self.assertNotIn(secret, repr(persistable_view_model(vm)))
        self.assertNotIn(secret, repr(serialize(self.state(detail))))
        self.assertNotIn("question_overlay", serialize(self.state(detail)))
        self.assertNotIn(secret, repr(vm))

    def test_choice_and_free_text_build_the_exact_structured_response(self):
        from fleet.explorer.state import reduce

        state, effects = reduce(self.state(), {"type": "click",
                                               "action": ("question.choice", "question-q1", "Continue")})
        expected = {"answers": {"question-q1": ["Continue"]}}
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][1]["native_ref"], self.envelope()["native_ref"])
        self.assertEqual(effects[0][1]["state"], "open")
        self.assertEqual(effects[0][2], "Continue")
        submitted, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][1]["native_ref"], self.envelope()["native_ref"])
        self.assertEqual(effects[0][2], expected)
        self.assertTrue(submitted.question_overlay["pending"])

        free_text_detail = self.envelope(detail={"questions": [{
            "id": "question-q1", "question": "Choose or enter another route",
            "choices": [{"value": "Continue", "label": "Continue"}], "is_other": True}]})
        typed, effects = reduce(self.state(free_text_detail, editing=True),
                                {"type": "key", "key": "text:take route B"})
        free_text = {"answers": {"question-q1": ["take route B"]}}
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][2], "take route B")
        typed, effects = reduce(typed, {"type": "key", "key": "enter"})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][2], free_text)

    def test_option_only_native_question_disables_free_text_but_other_allows_it(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import reduce
        from fleet.explorer.view_model import ExplorerViewModel

        option_only = self.envelope(detail={"questions": [{
            "id": "question-q1", "question": "Choose a listed route",
            "choices": [{"value": "route-a", "label": "Route A"}],
            "is_other": False}]})
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        text = "\n".join(draw(vm, self.state(option_only), 140, 45).text_rows())
        self.assertIn("Route A", text)
        self.assertNotIn("press e or click to type", text)
        blocked, effects = reduce(self.state(option_only), {"type": "click",
                                                            "action": ("question.edit", "question-q1")})
        self.assertEqual(effects, ())
        self.assertFalse(blocked.question_overlay.get("editing"))

        other_allowed = self.envelope(detail={"questions": [{
            "id": "question-q1", "question": "Choose or enter another route",
            "choices": [{"value": "route-a", "label": "Route A"}],
            "is_other": True}]})
        text = "\n".join(draw(vm, self.state(other_allowed), 140, 45).text_rows())
        self.assertIn("press e or click to type", text)
        editing, effects = reduce(self.state(other_allowed), {"type": "click",
                                                               "action": ("question.edit", "question-q1")})
        self.assertEqual(effects, ())
        self.assertTrue(editing.question_overlay["editing"])
        typed, effects = reduce(editing, {"type": "key", "key": "text:custom route"})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][2], "custom route")

    def test_constrained_opencode_multiselect_toggles_and_requires_native_bounds(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import (question_values_valid, reduce)
        from fleet.explorer.view_model import ExplorerViewModel

        multiselect = self.envelope(choices=[{"value": "a", "label": "Alpha"},
                                             {"value": "b", "label": "Beta"}])
        multiselect.update(harness="opencode", question_id="choice",
                           native_ref={"transport": "opencode-http-v2",
                                       "field_type": "multiselect",
                                       "option_values": ["a", "b"], "custom": False,
                                       "min_items": 2, "max_items": 2},
                           detail={"field_type": "multiselect", "question": "Pick two"})
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        start = self.state(multiselect)
        text = "\n".join(draw(vm, start, 140, 45).text_rows())
        self.assertIn("[ ] Alpha", text)
        self.assertIn("[ ] Beta", text)
        first, effects = reduce(start, {"type": "click", "action":
                                        ("question.choice", "choice", "a")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(first.question_overlay["draft"]["answers"]["choice"], ["a"])
        self.assertFalse(question_values_valid(multiselect, "choice", ["a"]))
        text = "\n".join(draw(vm, first, 140, 45).text_rows())
        self.assertIn("[x] Alpha", text)
        self.assertNotIn("[ submit answer ]", text)
        incomplete, effects = reduce(first, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects, ())

        complete, effects = reduce(incomplete, {"type": "click", "action":
                                                ("question.choice", "choice", "b")})
        self.assertEqual(effects[0][0], "questions.draft")
        values = complete.question_overlay["draft"]["answers"]["choice"]
        self.assertEqual(values, ["a", "b"])
        self.assertTrue(question_values_valid(multiselect, "choice", values))
        adjusted, effects = reduce(complete, {"type": "click", "action":
                                              ("question.choice", "choice", "a")})
        self.assertEqual(effects[0][0], "questions.draft")
        values = adjusted.question_overlay["draft"]["answers"]["choice"]
        self.assertEqual(values, ["b"])
        self.assertFalse(question_values_valid(multiselect, "choice", values))
        _incomplete, effects = reduce(adjusted, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects, ())
        complete, effects = reduce(adjusted, {"type": "click", "action":
                                              ("question.choice", "choice", "a")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(complete.question_overlay["draft"]["answers"]["choice"], ["b", "a"])
        text = "\n".join(draw(vm, complete, 140, 45).text_rows())
        self.assertIn("[ submit answer ]", text)
        submitted, effects = reduce(complete, {"type": "click", "action":
                                               ("question.submit",)})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][2], {"answers": {"choice": ["b", "a"]}})
        self.assertTrue(submitted.question_overlay["pending"])

        single = dict(multiselect, native_ref=dict(multiselect["native_ref"],
                                                   min_items=1, max_items=1))
        radio, _effects = reduce(self.state(single), {"type": "click", "action":
                                                      ("question.choice", "choice", "a")})
        radio, effects = reduce(radio, {"type": "click", "action":
                                        ("question.choice", "choice", "b")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(radio.question_overlay["draft"]["answers"]["choice"], ["b"])

    def test_choice_displays_label_but_submits_native_value(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import reduce
        from fleet.explorer.view_model import ExplorerViewModel

        detail = self.envelope(choices=[{"label": "Visible safe choice",
                                         "value": "native_choice_id"}])
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        text = "\n".join(draw(vm, self.state(detail), 140, 45).text_rows())
        self.assertIn("Visible safe choice", text)
        state, effects = reduce(self.state(detail), {"type": "click", "action":
                                                     ("question.choice", self.ref["question_id"],
                                                      "native_choice_id")})
        self.assertEqual(effects[0][2], "native_choice_id")

    def test_approval_requires_a_choice_and_has_no_persistent_escalation(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import reduce
        from fleet.explorer.view_model import ExplorerViewModel

        approval = self.envelope(kind="commandExecution/requestApproval",
                                 detail={"operation": "run command", "target": "repo A",
                                         "permissions": ["execute once"]})
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "approval", "source": "S1-approval", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        text = "\n".join(draw(vm, self.state(approval), 140, 45).text_rows())
        self.assertIn("run command", text)
        self.assertIn("approve", text)
        self.assertIn("reject", text)
        self.assertIn("defer", text)
        self.assertNotIn("acceptForSession", text)

        _state, effects = reduce(self.state(approval), {"type": "click",
                                                         "action": ("question.decision", "approve")})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][1]["native_ref"], approval["native_ref"])
        self.assertEqual(effects[0][2], {"decision": "approve"})
        _state, effects = reduce(self.state(approval), {"type": "click",
                                                         "action": ("question.decision", "reject")})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][1]["native_ref"], approval["native_ref"])
        self.assertEqual(effects[0][2], {"decision": "reject"})
        deferred, effects = reduce(self.state(approval), {"type": "click",
                                                           "action": ("question.decision", "defer")})
        self.assertEqual(effects, ())
        self.assertEqual(deferred.question_overlay["detail"]["state"], "open")
        self.assertIn("remains unresolved", deferred.question_overlay["status"])

        limited = self.envelope(
            kind="commandExecution/requestApproval",
            detail={"operation": "run command", "availableDecisions":
                    ["acceptForSession", "decline", "cancel"]})
        limited["native_ref"] = {"transport": "codex-managed-control",
                                  "request": {"method":
                                              "item/commandExecution/requestApproval"}}
        limited["actions"] = ["reject", "defer"]
        text = "\n".join(draw(vm, self.state(limited), 140, 45).text_rows())
        self.assertNotIn("[ approve ]", text)
        self.assertIn("[ reject ]", text)
        self.assertIn("[ defer ]", text)
        self.assertNotIn("acceptForSession", text)
        self.assertNotIn("applyNetworkPolicyAmendment", text)
        _state, effects = reduce(self.state(limited), {"type": "click",
                                                        "action": ("question.decision", "approve")})
        self.assertEqual(effects, ())
        _state, effects = reduce(self.state(limited), {"type": "click",
                                                        "action": ("question.decision", "reject")})
        self.assertEqual(effects[0][2], {"decision": "reject"})

    def test_duplicate_replaced_and_expired_requests_do_not_replay(self):
        from fleet.explorer.state import reduce
        from fleet.explorer.view_model import ExplorerViewModel
        from fleet import render

        draft = {"answers": {"question-q1": ["Continue"]}}
        state, effects = reduce(self.state(draft=draft), {"type": "click",
                                                          "action": ("question.submit",)})
        self.assertEqual(len(effects), 1)
        _state, duplicate = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(duplicate, ())

        replacement = dict(self.ref, request_id="req-new", turn_id="turn-new")
        replaced, effects = reduce(self.state(), {"type": "click",
                                                   "action": ("question.select", self.ref["id"], replacement)})
        self.assertEqual(effects, (("questions.read", replacement),))
        current_vm = ExplorerViewModel(questions=({"id": self.ref["id"], "ref": replacement},))
        with mock.patch.object(render, "_EXPLORER_STATE", replaced), \
             mock.patch.object(render, "_EXPLORER_MODEL", current_vm):
            render._explorer_accept_question_result(
                {"effect": ("questions.read", self.ref), "result": self.envelope()})
            self.assertEqual(render._EXPLORER_STATE.question_overlay["ref"], replacement)
            self.assertEqual(render._EXPLORER_STATE.question_overlay["state"], "loading")

        expired = self.state(self.envelope(state="expired"), draft=draft)
        _state, effects = reduce(expired, {"type": "click",
                                          "action": ("question.submit",)})
        self.assertEqual(effects, ())
        approval = self.state(self.envelope(kind="approval", state="expired"))
        _state, effects = reduce(approval, {"type": "click",
                                           "action": ("question.decision", "approve")})
        self.assertEqual(effects, ())

    def test_invalid_local_response_keeps_private_overlay_open_and_actionable(self):
        from fleet import render
        from fleet.explorer.view_model import ExplorerViewModel

        draft_text = "red, then blue"
        current = self.envelope(draft=draft_text)
        current.update(reason="opencode-answer-count-invalid",
                       result={"status": "invalid-local-response",
                               "reason": "opencode-answer-count-invalid"},
                       actions=["reply"])
        overlay_state = self.state(current, draft={"answers": {"question-q1": [draft_text]}},
                                   pending=True)
        vm = ExplorerViewModel(
            questions=({"id": self.ref["id"], "tag": self.ref["id"], "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        with mock.patch.object(render, "_EXPLORER_STATE", overlay_state), \
             mock.patch.object(render, "_EXPLORER_MODEL", vm):
            render._explorer_accept_question_result({
                "effect": ("questions.submit", self.ref, {"answers": {"question-q1": [draft_text]}}),
                "result": current})
            adopted = render._EXPLORER_STATE.question_overlay
        self.assertEqual(adopted["state"], "open")
        self.assertFalse(adopted["pending"])
        self.assertEqual(adopted["status"], "opencode-answer-count-invalid")
        self.assertEqual(adopted["draft"], {"answers": {"question-q1": [draft_text]}})
        self.assertEqual(adopted["detail"]["actions"], ["reply"])
        self.assertEqual(adopted["detail"]["native_ref"], current["native_ref"])

    def test_shared_draft_api_and_demo_mode_do_not_send_answers(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet import render

        draft = {"answers": {"question-q1": ["retained text"]}}
        envelope = self.envelope(draft=draft)
        with mock.patch.object(human_wait_state, "write_selected_draft", create=True,
                               return_value=envelope) as write:
            result = ExplorerRuntime.execute_question_action(
                ("questions.draft", dict(self.ref, **{"native_ref": self.envelope()["native_ref"],
                                                        "state": "open"}),
                 "retained text"))
        write.assert_called_once_with(
            dict(self.ref, **{"native_ref": self.envelope()["native_ref"], "state": "open"}),
            "retained text")
        self.assertEqual(result["draft"], draft)

        with mock.patch.object(human_wait_state, "read_selected_request", None, create=True):
            unavailable = ExplorerRuntime.execute_question_action(
                ("questions.read", self.ref))
        self.assertEqual(unavailable["state"], "unknown")
        self.assertIn("native prompt", unavailable["reason"])

        approval_response = {"decision": "approve"}
        with mock.patch.object(human_wait_state, "submit_selected_response", create=True,
                               return_value=self.envelope(kind="approval", state="submitted")) as submit:
            ExplorerRuntime.execute_question_action(
                ("questions.submit", self.ref, approval_response))
        submit.assert_called_once_with(self.ref, approval_response, action_id=None)

        state = self.state()
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", None), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": "1"}), \
             mock.patch.object(human_wait_state, "submit_selected_response", create=True) as submit:
            render._explorer_queue_question_effect(
                ("questions.submit", self.ref, approval_response))
            submit.assert_not_called()
            self.assertIn("disabled", render._EXPLORER_STATE.question_overlay["status"])


    def test_nested_permission_targets_are_visible_only_in_selected_private_detail(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import serialize
        from fleet.explorer.view_model import ExplorerViewModel

        detail = self.envelope(kind="approval", detail={
            "threadId": "internal-thread-only", "turnId": "internal-turn-only",
            "itemId": "internal-item-only", "operation": "Request turn permissions",
            "permissions": {"fileSystem": {"write": ["/workspace/output"]},
                            "network": {"enabled": True}}})
        state = self.state(detail)
        vm = ExplorerViewModel(questions=({"id": self.ref["id"], "tag": self.ref["id"],
                                          "ref": self.ref, "kind": "approval",
                                          "source": "S1-approval", "wait": "now"},),
                               system={"answer_waits": {"state": "complete", "total": 1,
                                                        "older": 0}})
        text = "\n".join(draw(vm, state, 140, 55).text_rows())
        self.assertIn("/workspace/output", text)
        details = " ".join(row.split("detail:", 1)[1].strip()
                           for row in text.splitlines() if "detail:" in row)
        self.assertIn('"enabled": true', details)
        self.assertIn("Request turn permissions", text)
        self.assertNotIn("internal-thread-only", text)
        self.assertNotIn("/workspace/output", json.dumps(serialize(state)))
        self.assertNotIn("/workspace/output", json.dumps(vm.to_mapping()))


    def test_secret_native_answer_is_masked_without_changing_explicit_response(self):
        from fleet.explorer.frame import draw
        from fleet.explorer.state import reduce
        from fleet.explorer.view_model import ExplorerViewModel

        secret = "fixture-private-answer"
        detail = self.envelope(detail={"questions": [{"id": "question-q1",
            "question": "Enter the requested secret", "choices": None,
            "is_other": True, "is_secret": True}]})
        state = self.state(detail, draft={"answers": {"question-q1": [secret]}})
        vm = ExplorerViewModel(questions=({"id": self.ref["id"], "tag": self.ref["id"],
                                          "ref": self.ref, "kind": "question",
                                          "source": "S1-question", "wait": "now"},),
                               system={"answer_waits": {"state": "complete", "total": 1,
                                                        "older": 0}})
        text = "\n".join(draw(vm, state, 140, 45).text_rows())
        self.assertNotIn(secret, text)
        self.assertIn("•", text)
        _state, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects[0][2], {"answers": {"question-q1": [secret]}})


class FleetQuestionBackendIntegrationTests(unittest.TestCase):
    """Exercise the real selected-wait API through its strict control transport seam."""

    def setUp(self):
        from time import time
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.now = time()
        self._install_wait()
        self.discovery = mock.patch.object(human_wait_state, "_codex_control",
                                           side_effect=self._discover_control)
        self.transport = mock.patch.object(human_wait_state, "_codex_control_request",
                                           side_effect=self._control_request)
        self.discovery.start()
        self.transport.start()
        self.addCleanup(self.discovery.stop)
        self.addCleanup(self.transport.stop)

    def _install_wait(self, kind="question", *, stale=False, questions=None,
                      request_id="native-request-q1"):
        self.kind = kind
        self.item_id = "S1:integration-q1"
        self.session_id = "thread-integration-q1"
        self.request_id = request_id
        self.turn_id = "native-turn-q1"
        self.question_id = "native-question-q1" if kind == "question" else None
        self.epoch = 17
        self.ref = {"id": self.item_id, "harness": "codex",
                    "session_id": self.session_id, "request_id": self.request_id,
                    "turn_id": self.turn_id}
        if self.question_id:
            self.ref["question_id"] = self.question_id
        self.native_ref = {"harness": "codex", "session_id": self.session_id,
                           "request_id": self.request_id, "turn_id": self.turn_id,
                           "item_id": "native-item-q1", "connection_epoch": self.epoch}
        self.gateway = {"path": str(Path(self.tmp.name) / "control.sock"), "pid": 73,
                        "proc_start": "730", "epoch": self.epoch,
                        "status": {"status": "ready", "thread_id": self.session_id}}
        if kind == "approval":
            native_detail = {"operation": "run command", "target": "workspace",
                             "permissions": ["execute once"]}
            native_questions = None
        else:
            native_detail = {"questions": list(questions or ({
                "id": self.question_id, "question": "Which route should run?",
                "options": [{"label": "Green route", "description": "Preferred route"},
                            {"label": "Blue route", "description": "Alternative route"}],
            },))}
            native_questions = native_detail["questions"]
        self.detail_request = {"harness": "codex", "session_id": self.session_id,
                               "request_id": self.request_id, "turn_id": self.turn_id,
                               "item_id": "native-item-q1", "kind": kind,
                               "detail": native_detail, "state": "open",
                               "connection_epoch": self.epoch}
        if self.question_id:
            self.detail_request["question_id"] = self.question_id
        if native_questions and len(native_questions) == 1:
            self.detail_request["choices"] = native_questions[0].get("options", [])
        self.transport_calls = []
        self.replies = []
        item = {"id": self.item_id, "source": "S1",
                "kind": "approval" if kind == "approval" else "decision",
                "since": self.now, "stale": stale, "attribution": "exact",
                "question": "PRIVATE-QUESTION-SENTINEL",
                "owner": {"harness": "codex", "session_id": self.session_id,
                          "seat_key": "integration-seat"},
                "request_id": self.request_id, "turn_id": self.turn_id,
                "item_id": "native-item-q1"}
        if self.question_id:
            item["question_id"] = self.question_id
        self.assertTrue(human_wait_state.publish_cache({
            "items": [item], "complete": True, "observed_at": self.now,
            "total": 0 if stale else 1, "health": [],
        }))

    def _discover_control(self, session_id):
        return self.gateway if session_id == self.session_id else None

    def _control_request(self, path, payload):
        self.assertEqual(path, self.gateway["path"])
        self.transport_calls.append(dict(payload))
        operation = payload.get("op")
        if operation == "interaction-list":
            self.assertEqual(set(payload), {"schema_version", "op", "session_id"})
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["session_id"], self.session_id)
            return {"schema_version": 1, "status": "ready", "requests": [self.native_ref]}
        if operation == "interaction-detail":
            self.assertEqual(set(payload), {"schema_version", "op", "ref"})
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["ref"], self.native_ref)
            return {"schema_version": 1, "status": "ready",
                    "request": dict(self.detail_request)}
        if operation == "interaction-reply":
            self.assertEqual(set(payload), {"schema_version", "op", "ref", "response", "action_id"})
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["ref"], self.native_ref)
            self.replies.append(dict(payload))
            return {"schema_version": 1, "status": "applied",
                    "request": self.native_ref, "action_id": payload["action_id"]}
        self.fail(f"unexpected Codex control operation: {operation}")

    def _read_into_ui(self, state=None):
        from fleet.explorer.state import NavState, question_draft_from_backend, reduce
        from fleet.explorer.runtime import ExplorerRuntime

        state = state or NavState(view="QUESTIONS")
        selected, effects = reduce(state, {"type": "click", "action":
                                          ("question.select", self.ref["id"], self.ref)})
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.read")
        envelope = ExplorerRuntime.execute_question_action(effects[0])
        overlay = dict(selected.question_overlay)
        overlay.update(detail=envelope,
                       draft=question_draft_from_backend(envelope, envelope.get("draft")),
                       state=envelope.get("state"), pending=False)
        return replace(selected, question_overlay=overlay), envelope

    def _adopt_draft(self, state, result):
        from fleet.explorer.state import question_draft_from_backend

        overlay = dict(state.question_overlay)
        overlay.update(detail=result,
                       draft=question_draft_from_backend(result, result.get("draft")),
                       state=result.get("state"), pending=False)
        return replace(state, question_overlay=overlay)

    def test_native_detail_choice_draft_restart_submit_and_deduplicate(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import NavState, reduce, restore, serialize

        state, envelope = self._read_into_ui()
        self.assertEqual(envelope["state"], "open")
        self.assertEqual(envelope["detail"]["questions"][0]["id"], self.question_id)
        state, effects = reduce(state, {"type": "click", "action":
                                        ("question.choice", self.question_id, "Green route")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][1]["native_ref"], envelope["native_ref"])
        self.assertEqual(effects[0][1]["state"], "open")
        self.assertEqual(effects[0][2], "Green route")
        saved = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(saved["draft"], "Green route")
        state = self._adopt_draft(state, saved)

        state_snapshot = json.dumps(serialize(state), ensure_ascii=False)
        self.assertNotIn("PRIVATE-QUESTION-SENTINEL", state_snapshot)
        fleet_snapshot = human_wait_state._fleet_cache_path().read_bytes()
        self.assertNotIn(b"PRIVATE-QUESTION-SENTINEL", fleet_snapshot)
        self.assertNotIn(b'"question"', fleet_snapshot)

        restarted = restore(serialize(state))
        restarted, reopened = self._read_into_ui(restarted)
        self.assertEqual(reopened["draft"], "Green route")
        self.assertEqual(restarted.question_overlay["draft"],
                         {"answers": {self.question_id: ["Green route"]}})
        submitted, effects = reduce(restarted, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][1]["native_ref"], reopened["native_ref"])
        result = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(self.replies[0]["response"],
                         {"answers": {self.question_id: ["Green route"]}})
        duplicate = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(duplicate["state"], "applied")
        self.assertEqual(len(self.replies), 1)

    def test_free_text_draft_uses_exact_native_question_id(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce

        state, envelope = self._read_into_ui()
        state, _ = reduce(state, {"type": "click", "action": ("question.edit", self.question_id)})
        state, effects = reduce(state, {"type": "key", "key": "text:free text answer"})
        self.assertEqual(effects[0][2], "free text answer")
        self.assertEqual(effects[0][1]["native_ref"], envelope["native_ref"])
        saved = ExplorerRuntime.execute_question_action(effects[0])
        state = self._adopt_draft(state, saved)
        state, effects = reduce(state, {"type": "key", "key": "enter"})
        self.assertEqual(effects[0][0], "questions.submit")
        result = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(self.replies[0]["response"],
                         {"answers": {self.question_id: ["free text answer"]}})

    def test_approval_defer_sends_nothing_and_approve_is_one_shot(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import reduce

        self._install_wait(kind="approval")
        state, envelope = self._read_into_ui()
        deferred, effects = reduce(state, {"type": "click", "action":
                                          ("question.decision", "defer")})
        self.assertEqual(effects, ())
        self.assertIn("remains unresolved", deferred.question_overlay["status"])
        result = ExplorerRuntime.execute_question_action(
            ("questions.submit", dict(envelope, id=self.item_id), {"decision": "defer"}))
        self.assertEqual(result["state"], "open")
        self.assertFalse(self.replies)

        approved, effects = reduce(deferred, {"type": "click", "action":
                                             ("question.decision", "approve")})
        self.assertEqual(effects[0][2], {"decision": "approve"})
        result = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(self.replies[0]["response"], {"decision": "approve"})
        ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(len(self.replies), 1)

    def test_older_wait_is_folded_and_selected_only_when_expanded(self):
        from fleet.explorer.live import _question_records
        from fleet.explorer.state import NavState, reduce, question_draft_from_backend
        from fleet import render
        from fleet.explorer.runtime import ExplorerRuntime

        self._install_wait(stale=True)
        current = {"id": "S1:current-integration", "source": "S1", "kind": "decision",
                   "since": self.now, "stale": False, "attribution": "exact",
                   "owner": {"harness": "codex", "session_id": self.session_id},
                   "request_id": "native-request-current", "turn_id": "native-turn-current"}
        older = {"id": self.item_id, "source": "S1", "kind": "decision",
                 "since": self.now, "stale": True, "attribution": "exact",
                 "owner": {"harness": "codex", "session_id": self.session_id},
                 "request_id": self.request_id, "turn_id": self.turn_id,
                 "item_id": "native-item-q1"}
        self.assertTrue(human_wait_state.publish_cache({
            "items": [current, older], "complete": True, "observed_at": self.now,
            "total": 1, "health": [],
        }))
        waits = human_wait_state.cached_fleet_view(now=self.now, request=False)
        questions, summary = _question_records({"answer_waits": waits}, [], {}, self.now)
        self.assertEqual((summary["total"], summary["older"]), (1, 1))
        older_row = next(item for item in questions if item["id"] == self.item_id)
        self.assertTrue(older_row["older"])
        with mock.patch.object(render, "_EXPLORER_MODEL", SimpleNamespace(questions=questions)), \
             mock.patch.object(render, "_EXPLORER_STATE", NavState(view="QUESTIONS")):
            self.assertEqual([item["id"] for item in render._explorer_question_refs()],
                             ["S1:current-integration"])
            state, _ = reduce(NavState(view="QUESTIONS"), {"type": "click", "action":
                                                ("question.older.toggle",)})
            with mock.patch.object(render, "_EXPLORER_STATE", state):
                self.assertEqual([item["id"] for item in render._explorer_question_refs()],
                                 ["S1:current-integration", self.item_id])
            selected, effects = reduce(state, {"type": "click", "action":
                                               ("question.select", self.item_id,
                                                older_row["ref"])})
        self.assertEqual(effects[0][0], "questions.read")
        self.assertEqual(effects[0][1]["request_id"], self.request_id)
        envelope = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(envelope["state"], "open")
        overlay = dict(selected.question_overlay)
        overlay.update(detail=envelope, draft=question_draft_from_backend(envelope,
                                                                          envelope.get("draft")),
                       state="open")
        selected = replace(selected, question_overlay=overlay)
        selected, effects = reduce(selected, {"type": "click", "action":
                                             ("question.choice", self.question_id,
                                              "Green route")})
        saved = ExplorerRuntime.execute_question_action(effects[0])
        selected = self._adopt_draft(selected, saved)
        selected, effects = reduce(selected, {"type": "click", "action": ("question.submit",)})
        result = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(len(self.replies), 1)

    def test_multiple_native_questions_show_no_false_single_answer_support(self):
        from fleet.explorer.runtime import ExplorerRuntime
        from fleet.explorer.state import (question_action_supported,
                                          question_answer_supported,
                                          question_draft_from_backend, reduce, restore,
                                          serialize)
        from fleet.explorer.frame import draw
        from fleet.explorer.view_model import ExplorerViewModel

        self._install_wait(questions=[
            {"id": "native-question-one", "question": "First?",
             "options": [{"label": "A", "value": "first-a"}]},
            {"id": "native-question-two", "question": "Second?",
             "options": [{"label": "B", "value": "second-b"}]},
        ])
        state, envelope = self._read_into_ui()
        self.assertEqual(len(envelope["detail"]["questions"]), 2)
        self.assertTrue(envelope["support"]["supported"])
        self.assertIsInstance(envelope["native_ref"], dict)
        self.assertTrue(question_answer_supported(envelope))
        self.assertEqual([choice["value"] for choice in
                          envelope["detail"]["questions"][0]["choices"]], ["A"])
        self.assertEqual([choice["value"] for choice in
                          envelope["detail"]["questions"][1]["choices"]], ["B"])

        state, effects = reduce(state, {"type": "click", "action":
                                        ("question.choice", "native-question-one", "A")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][1]["native_ref"], envelope["native_ref"])
        saved = ExplorerRuntime.execute_question_action(effects[0])
        self.assertIsInstance(saved["draft"], str)
        state = self._adopt_draft(state, saved)

        partial, effects = reduce(state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects, ())
        self.assertFalse(self.replies)

        state, effects = reduce(partial, {"type": "click", "action":
                                          ("question.choice", "native-question-two", "B")})
        self.assertEqual(effects[0][0], "questions.draft")
        self.assertEqual(effects[0][1]["native_ref"], envelope["native_ref"])
        saved = ExplorerRuntime.execute_question_action(effects[0])
        expected = {"answers": {"native-question-one": ["A"],
                                "native-question-two": ["B"]}}
        self.assertIsInstance(saved["draft"], str)
        self.assertEqual(question_draft_from_backend(saved, saved["draft"]), expected)
        state = self._adopt_draft(state, saved)

        restarted = restore(serialize(state))
        restarted, reopened = self._read_into_ui(restarted)
        self.assertIsInstance(reopened["draft"], str)
        self.assertEqual(question_draft_from_backend(reopened, reopened["draft"]), expected)
        self.assertEqual(restarted.question_overlay["draft"], expected)
        submitted, effects = reduce(restarted, {"type": "click", "action":
                                                ("question.submit",)})
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.submit")
        self.assertEqual(effects[0][1]["native_ref"], reopened["native_ref"])
        self.assertEqual(effects[0][2], expected)
        self.assertTrue(submitted.question_overlay["pending"])
        result = ExplorerRuntime.execute_question_action(effects[0])
        self.assertEqual(result["state"], "applied")
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(self.replies[0]["response"], expected)

        vm = ExplorerViewModel(
            questions=({"id": self.item_id, "tag": self.item_id, "ref": self.ref,
                        "kind": "question", "source": "S1-question", "wait": "now"},),
            system={"answer_waits": {"state": "complete", "total": 1, "older": 0}})
        text = "\n".join(draw(vm, restarted, 140, 45).text_rows())
        self.assertNotIn("Multiple question response is unavailable", text)

        self._install_wait(kind="elicitation")
        self.ref["turn_id"] = None
        self.native_ref.update(kind="elicitation", method="mcpServer/elicitation/request",
                               turn_id=None)
        self.native_ref.pop("item_id", None)
        self.detail_request.update(kind="elicitation", turn_id=None,
                                   detail={"message": "Continue in the native prompt."})
        self.detail_request.pop("item_id", None)
        cached = human_wait_state._read_cache()
        unsupported_item = dict(cached["items"][0], kind="elicitation", turn_id=None)
        unsupported_item.pop("item_id", None)
        self.assertTrue(human_wait_state.publish_cache({
            "items": [unsupported_item], "complete": True,
            "observed_at": self.now, "total": 1, "health": [],
        }))
        no_turn_state, no_turn = self._read_into_ui()
        self.assertIsNone(no_turn["turn_id"])
        self.assertFalse(no_turn["support"]["supported"])
        self.assertEqual(no_turn["actions"], [])
        self.assertFalse(question_answer_supported(no_turn))
        self.assertFalse(question_action_supported(no_turn))
        _, effects = reduce(no_turn_state, {"type": "click", "action":
                                            ("question.choice", "native-question-one", "A")})
        self.assertEqual(effects, ())
        _, effects = reduce(no_turn_state, {"type": "click", "action": ("question.submit",)})
        self.assertEqual(effects, ())
        self.assertFalse(self.replies)

    def test_terminal_submit_states_refresh_exact_ref_and_changed_selection_cannot_send(self):
        from collections import deque
        from fleet import render
        from fleet.explorer.state import NavState

        state, envelope = self._read_into_ui()
        selected_ref = dict(envelope, id=self.item_id, _fleet_row_ref=dict(self.ref))
        effect = ("questions.submit", selected_ref,
                  {"answers": {self.question_id: ["Green route"]}})
        model = SimpleNamespace(questions=({"id": self.item_id, "ref": self.ref},))
        for result_state in ("submitted", "received", "applied"):
            queue = deque()
            pump = mock.Mock()
            with mock.patch.object(render, "_EXPLORER_STATE", state), \
                 mock.patch.object(render, "_EXPLORER_MODEL", model), \
                 mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue), \
                 mock.patch.object(render, "_EXPLORER_QUESTION_PUMP", pump):
                render._explorer_accept_question_result({
                    "effect": effect, "result": dict(envelope, state=result_state)})
                self.assertEqual(queue[0][0], "questions.read")
                self.assertEqual(queue[0][1]["native_ref"], envelope["native_ref"])
                pump.request.assert_called_once_with(force=True)

        changed = NavState(view="QUESTIONS", folds=(("qsel:QUESTIONS", "S1:other"),),
                           question_overlay={"ref": {"id": "S1:other", "harness": "codex",
                                                      "session_id": self.session_id},
                                             "state": "open"})
        queue = deque([effect])
        before = len(self.transport_calls)
        with mock.patch.object(render, "_EXPLORER_STATE", changed), \
             mock.patch.object(render, "_EXPLORER_MODEL", model), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue):
            result = render._explorer_question_worker()
            self.assertEqual(result["result"]["state"], "unknown")
            self.assertIn("changed", result["result"]["reason"])
            render._explorer_accept_question_result({"effect": effect,
                                                     "result": dict(envelope, state="applied")})
            self.assertEqual(render._EXPLORER_STATE.question_overlay,
                             changed.question_overlay)
        self.assertEqual(len(self.transport_calls), before)


    def test_item_only_marker_resolves_rpc_id_from_the_actual_native_request(self):
        cache = human_wait_state._read_cache()
        cache["items"][0].pop("request_id")
        self.assertTrue(human_wait_state.publish_cache(cache))
        selected = human_wait_state.read_selected_request({
            "id": self.item_id, "harness": "codex", "session_id": self.session_id})
        self.assertEqual(selected["state"], "open")
        self.assertEqual(selected["request_id"], self.request_id)
        self.assertEqual(selected["native_ref"]["request"]["item_id"], "native-item-q1")
        self.assertFalse(self.replies)

    def test_reused_rpc_id_on_a_new_native_item_does_not_inherit_applied(self):
        from fleet.explorer.runtime import ExplorerRuntime

        selected_state, first = self._read_into_ui()
        old_item = human_wait_state._selected_identity(first)["item_id"]
        answered = ExplorerRuntime.execute_question_action(("questions.submit",
            dict(first, id=self.item_id), {"answers": {self.question_id: ["Green route"]}}))
        self.assertEqual(answered["state"], "applied")
        self.epoch += 1
        self.gateway["epoch"] = self.epoch
        self.native_ref = dict(self.native_ref, item_id="native-item-q2",
                               connection_epoch=self.epoch)
        self.detail_request = dict(self.detail_request, item_id="native-item-q2",
                                   connection_epoch=self.epoch)
        cache = human_wait_state._read_cache()
        cache["items"][0]["item_id"] = "native-item-q2"
        self.assertTrue(human_wait_state.publish_cache(cache))
        _state, second = self._read_into_ui()
        self.assertEqual(second["state"], "open")
        self.assertNotEqual(human_wait_state._selected_identity(second)["item_id"], old_item)
        second_answer = ExplorerRuntime.execute_question_action(("questions.submit",
            dict(second, id=self.item_id), {"answers": {self.question_id: ["Blue route"]}}))
        self.assertEqual(second_answer["state"], "applied")
        ExplorerRuntime.execute_question_action(("questions.submit", dict(second, id=self.item_id),
            {"answers": {self.question_id: ["Blue route"]}}))
        self.assertEqual(len(self.replies), 2)  # two actual native items, once each
        cache = human_wait_state._read_cache()
        cache["items"][0]["item_id"] = "different-item-from-stale-observation"
        self.assertTrue(human_wait_state.publish_cache(cache))
        mismatched = human_wait_state.read_selected_request(self.ref)
        self.assertEqual(mismatched["state"], "unknown")
        self.assertIsNone(mismatched["detail"])

    def test_selected_open_request_can_refresh_late_native_target_details(self):
        from fleet import render
        from fleet.explorer.state import reduce
        from fleet.explorer.runtime import ExplorerRuntime

        self._install_wait(kind="approval")
        self.native_ref["method"] = "item/fileChange/requestApproval"
        self.detail_request["detail"] = {"operation": "Apply file changes", "grantRoot": None,
                                         "changes": []}
        state, missing = self._read_into_ui()
        self.assertNotIn("approve", missing["actions"])
        self.detail_request["detail"]["changes"] = [{"path": "/repo/report.py",
                                                      "kind": {"type": "update"}}]
        loading, effects = reduce(state, {"type": "click", "action":
                                         ("question.select", self.item_id, self.ref)})
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][0], "questions.read")
        latest = ExplorerRuntime.execute_question_action(effects[0])
        with mock.patch.object(render, "_EXPLORER_STATE", loading), \
             mock.patch.object(render, "_EXPLORER_MODEL", SimpleNamespace(
                 questions=({"id": self.item_id, "ref": self.ref},))):
            render._explorer_accept_question_result({"effect": effects[0], "result": latest})
            self.assertIn("approve", render._EXPLORER_STATE.question_overlay["detail"]["actions"])
        self.assertFalse(self.replies)

    def test_numeric_request_ids_reach_actual_render_without_string_coercion(self):
        from fleet import render
        from fleet.explorer.live import _question_records

        for request_id in (17, 0, "17"):
            with self.subTest(request_id=request_id):
                self._install_wait(request_id=request_id)
                public = json.loads(human_wait_state._fleet_cache_path().read_text())
                rows, _ = _question_records(SimpleNamespace(answer_waits=public), [], {}, self.now)
                self.assertEqual(type(rows[0]["ref"]["request_id"]), type(request_id))
                self.ref = rows[0]["ref"]
                state, envelope = self._read_into_ui()
                with mock.patch.object(render, "_EXPLORER_STATE", state), \
                     mock.patch.object(render, "_EXPLORER_MODEL", SimpleNamespace(questions=rows)):
                    render._explorer_accept_question_result({
                        "effect": ("questions.read", self.ref), "result": envelope})
                    adopted = render._EXPLORER_STATE.question_overlay
                    self.assertEqual(adopted["state"], "open")
                    self.assertEqual(type(adopted["detail"]["request_id"]), type(request_id))
                    self.assertIsNone(render._explorer_question_envelope(
                        self.ref, dict(envelope, request_id=True)))
                    if type(request_id) is int:
                        self.assertIsNone(render._explorer_question_envelope(
                            self.ref, dict(envelope, request_id=str(request_id))))
        for invalid in (True, 1.5, {"question": "NOT-A-NATIVE-ID-SENTINEL"}):
            public["items"][0]["request_id"] = invalid
            rows, _ = _question_records(SimpleNamespace(answer_waits=public), [], {}, self.now)
            self.assertNotIn("request_id", rows[0]["ref"])
            self.assertNotIn("NOT-A-NATIVE-ID-SENTINEL", json.dumps(rows))
        self.assertFalse(self.replies)

    def test_delayed_native_status_refreshes_until_terminal_without_resubmitting(self):
        from collections import deque
        from fleet import render

        state, envelope = self._read_into_ui()
        ref = dict(envelope, id=self.item_id, _fleet_row_ref=dict(self.ref))
        queue = deque()
        pump = mock.Mock(running=False)
        pump.poll.return_value = None
        clock = [10.0]
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_MODEL", SimpleNamespace(
                 questions=({"id": self.item_id, "ref": self.ref},))), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue), \
             mock.patch.object(render, "_EXPLORER_QUESTION_PUMP", pump), \
             mock.patch.object(render, "_EXPLORER_QUESTION_REFRESH_AT", 0.0), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": ""}), \
             mock.patch.object(render.time, "monotonic", side_effect=lambda: clock[0]):
            for status in ("submitted", "received"):
                render._explorer_accept_question_result({
                    "effect": ("questions.read", ref), "result": dict(envelope, state=status)})
                render._explorer_question_pump_tick()
                self.assertFalse(queue)
                clock[0] += 1.1
                render._explorer_question_pump_tick()
                self.assertEqual(len(queue), 1)
                refresh = queue.popleft()
                self.assertEqual(refresh[0], "questions.read")
                self.assertEqual(refresh[1]["native_ref"], envelope["native_ref"])
            for status in ("applied", "unknown", "cancelled", "expired"):
                render._explorer_accept_question_result({
                    "effect": ("questions.read", ref), "result": dict(envelope, state=status)})
                clock[0] += 2.0
                render._explorer_question_pump_tick()
                self.assertFalse(queue, status)
        self.assertFalse(self.replies)

    def test_navigation_and_shutdown_save_local_drafts_without_running_queued_submit(self):
        from collections import deque
        from fleet import render
        from fleet.explorer.state import NavState

        state, envelope = self._read_into_ui()
        ref = dict(envelope, id=self.item_id, _fleet_row_ref=dict(self.ref))
        draft = ("questions.draft", ref, "Green route")
        submit = ("questions.submit", ref, {"answers": {self.question_id: ["Green route"]}})
        queue = deque([draft])
        with mock.patch.object(render, "_EXPLORER_STATE", NavState(view="PEERS")), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": ""}):
            saved = render._explorer_question_worker()["result"]
        self.assertEqual(saved["draft"], "Green route")

        overlay = dict(state.question_overlay,
                       draft={"answers": {self.question_id: ["Blue route"]}})
        state = replace(state, question_overlay=overlay)
        queue = deque([submit, ("questions.read", ref), draft,
                       ("questions.draft", dict(ref, draft="older"), "Green route")])
        pump = mock.Mock()
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", queue), \
             mock.patch.object(render, "_EXPLORER_QUESTION_PUMP", pump), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": ""}):
            results = render._explorer_stop_question_pump()
            self.assertIsNone(render._EXPLORER_QUESTION_QUEUE)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["draft"], "Blue route")
        pump.stop.assert_called_once_with(join_timeout=1.0)
        self.assertEqual(human_wait_state.read_selected_request(ref)["draft"], "Blue route")
        self.assertFalse(self.replies)

    def test_private_draft_coalesces_by_binding_and_replay_shutdown_does_not_write(self):
        from collections import deque
        from fleet import render

        state, envelope = self._read_into_ui()
        ref = dict(envelope, id=self.item_id, _fleet_row_ref=dict(self.ref))
        pump = mock.Mock()
        with mock.patch.object(render, "_EXPLORER_STATE", state), \
             mock.patch.object(render, "_EXPLORER_QUESTION_QUEUE", deque()), \
             mock.patch.object(render, "_EXPLORER_QUESTION_PUMP", pump), \
             mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False), \
             mock.patch.dict(os.environ, {"FLEET_DEMO": ""}):
            render._explorer_queue_question_effect(("questions.draft", ref, "G"))
            render._explorer_queue_question_effect(("questions.draft", dict(ref, draft="G"),
                                                    "Green route"))
            self.assertEqual(len(render._EXPLORER_QUESTION_QUEUE), 1)
            self.assertEqual(render._EXPLORER_QUESTION_QUEUE[0][2], "Green route")
            before = human_wait_state._cache_path().read_bytes()
            with mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", True):
                self.assertEqual(render._explorer_stop_question_pump(), [])
            self.assertEqual(human_wait_state._cache_path().read_bytes(), before)
        self.assertFalse(self.replies)


if __name__ == "__main__":
    unittest.main()
