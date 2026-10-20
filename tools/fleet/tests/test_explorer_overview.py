import copy
import tempfile
import unittest
from types import SimpleNamespace

from tools.fleet.explorer.overview import build_overview, obligation_lines
from tools.fleet.tests.explorer_fixture import collect, materialize, screen_main

OBLIGATION_PAIRS = {
    "att-38fd853b2dcc390251ddbb1b0c070b32": "   담당 CL · 정리 대기 · 전달 대기 · 70분 전",
    "att-ad605131fead711661cb52a21c9da2bd": "   담당 CL · 정리 대기 · 전달 대기 · 23분 전",
    "att-126c433a3efd01c07b6b3f704f3778b6": "   담당 CL · 정리 대기 · 전달 대기 · 214분 전",
    "att-69814a640b08e076be18a3d648b63a4e": "   담당 CL · 정리 대기 · 전달 대기 · 14분 전",
    "att-da1054dece491ff5dd64b2132b8b68ad": "   담당 CL · 정리 대기 · 전달 대기 · 56분 전",
}


def pairs(lines):
    out = {}
    for first, second in zip(lines, lines[1:]):
        if first[:3] in {f"{n}. " for n in range(1, 10)}:
            out[first[3:]] = second
    return out


class CanonicalOverview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.result = collect(cls.fx)
        cls.lines = list(cls.result.overview)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    @staticmethod
    def canonical():
        # the 168-column scene shows the whole overview in the ledger column (62 cells)
        return [line.split("│")[0].rstrip() for line in screen_main("overview", 168, 4)]

    def test_team_composition_rows_equal_the_canonical_screen(self):
        canonical = self.canonical()
        head = 15                       # title, lane, relations, message row and the succession row
        # the canonical row says "fixture에 확인 근거 없음"; the product has no fixture to name
        self.assertEqual(self.lines[:head], [l.replace("fixture에 ", "") for l in canonical[:head]])
        self.assertEqual(self.lines[0], "개요 · 팀 구성(P2) / 남은 일(P4)")

    def test_execution_lane_observation_and_message_markers_are_distinct(self):
        self.assertIn("[3e] CL ─▸ 감독 CL ─▸ 실행 중 단계 CX", self.lines)
        self.assertIn("hearting ┄▷ [7a]", self.lines)
        self.assertIn("[b6] ⚑ 관찰 5 대상", self.lines)
        self.assertIn("  ┄▷ hearting-friction-cl CL", self.lines)
        self.assertIn("[34] ⚑ 관찰 1 대상", self.lines)
        self.assertIn("hearting-b6 ··→ [3e] 26분 전", self.lines)
        self.assertEqual(len([l for l in self.lines if l.startswith("  ┄▷ ")]), 6)

    def test_succession_without_evidence_is_unknown_with_a_reason(self):
        self.assertIn("승계 ? · 확인 근거 없음", self.lines)
        self.assertFalse([l for l in self.lines if l.startswith("승계") and "?" not in l])

    def test_remaining_work_header_and_each_duty(self):
        self.assertIn("남은 일 5 · 담당 CL", self.lines)
        self.assertEqual(pairs(self.lines), OBLIGATION_PAIRS)
        self.assertEqual(pairs(self.canonical()), OBLIGATION_PAIRS)

    def test_duties_come_from_unfinished_registered_batches_of_this_session_only(self):
        self.assertEqual(len(self.result.obligations), 5)
        self.assertTrue(all(o["kind"] == "registered-batch" and o["state"] == "pending" for o in self.result.obligations))
        self.assertEqual([o["age_min"] for o in self.result.obligations], [214, 70, 56, 23, 14])   # oldest first
        self.assertEqual({o["harness"] for o in self.result.obligations}, {"claude"})


class PartialEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.result = collect(cls.fx)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def snapshot_without_steward(self):
        sessions = copy.deepcopy(self.fx.snapshot.sessions)
        for session in sessions:
            session["steward"] = False
            session["steward_targets"] = None
            session["steward_parents"] = None
        return SimpleNamespace(sessions=sessions, jobs=self.fx.snapshot.jobs, pending_delivery=None)

    def test_steward_failure_keeps_the_parent_edge_and_peer_rows(self):
        lines = build_overview(self.snapshot_without_steward(), self.result, steward_error="steward 원천 읽기 실패")
        self.assertIn("[3e] CL ─▸ 감독 CL ─▸ 실행 중 단계 CX", lines)           # parent edge from the jobs
        self.assertIn("hearting-b6 ··→ [3e] 26분 전", lines)                      # peer message
        self.assertIn("관계 읽지 못함 · steward 원천 읽기 실패", lines)
        self.assertFalse([l for l in lines if "┄▷" in l or "관찰" in l])         # nothing is invented
        self.assertIn("남은 일 5 · 담당 CL", lines)

    def test_missing_relations_are_not_drawn(self):
        sessions = [{"session_id": "s", "session_tag": "zz", "harness": "codex", "is_child": False,
                     "process_role": "conversation", "peer_last_recv": {"from_name": None, "age_min": 19}}]
        lines = build_overview(SimpleNamespace(sessions=sessions, jobs=[]), None, sid="s")
        self.assertEqual(lines[:4], ("개요 · 팀 구성(P2) / 남은 일(P4)", "[zz] CX", "? ··→ [zz] 19분 전",
                                     "승계 ? · 확인 근거 없음"))
        self.assertEqual(lines[4], "남은 일 ? · 읽지 못함")                     # obligations were never read

    def test_a_session_without_messages_gets_no_message_row(self):
        sessions = [{"session_id": "s", "session_tag": "zz", "harness": "claude", "is_child": False}]
        lines = build_overview(SimpleNamespace(sessions=sessions, jobs=[]), None, sid="s")
        self.assertFalse([l for l in lines if "··→" in l])

    def test_unknown_obligations_are_not_zero_and_empty_is_zero(self):
        self.assertEqual(obligation_lines(None), ["남은 일 ? · 읽지 못함"])
        self.assertEqual(obligation_lines(()), ["남은 일 0"])

    def test_unattributed_duty_is_shown_as_unassigned(self):
        lines = obligation_lines([{"attempt_id": "att-x", "harness": None, "cleanup": "pending",
                                   "delivery": "unknown", "age_min": None}])
        self.assertEqual(lines, ["남은 일 1 · 담당 미귀속", "1. att-x", "   담당 미귀속 · 정리 대기 · 전달 ? · 시각 ?"])


if __name__ == "__main__":
    unittest.main()
