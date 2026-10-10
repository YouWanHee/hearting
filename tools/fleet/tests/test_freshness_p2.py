"""Fleet audit T2/R1/R4: event time and generated text never mint current facts."""
import sys
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import render, route, route_chain, titles, refresh_title
from fleet.model import DispatchJob, ResourceJob, Session, WorkProjection


def text(lines):
    return "\n".join("".join(t for t, _ in row) for row in lines if row)


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        render.set_show_all(True)
        self.addCleanup(render.set_show_all, False)
        self.now = time.time()

    def session(self, **kw):
        values = dict(harness="claude", pid=123, cwd="/work/project", session_id="sid",
                      liveness="idle", title="학습 구조 설명", mtime=self.now - 360)
        values.update(kw)
        return Session(**values)

    def lines(self, sessions=(), jobs=(), resources=(), width=160):
        with mock.patch.object(render, "_COMPUTE_HOSTS", None):
            return render._build_lines(list(sessions), list(jobs), "both", False, 0,
                                       term_width=width, resources=list(resources))

    def header(self, lines):
        return next(row for row in lines if row and any(t == "project" for t, _ in row))

    def test_transcript_mtime_is_file_update_never_completion(self):
        header = text([self.header(self.lines([self.session()]))])
        self.assertNotIn("✓", header)
        self.assertIn("파일 갱신", header)

    def test_idle_owner_running_resource_makes_project_active(self):
        owner = DispatchJob(key="autopilot-lab", slug="train", cwd="/work/project",
                            harness="codex", liveness="idle", attempt_id="at-one",
                            parent_sid="sid", is_child=True,
                            work_projection=WorkProjection(source="none"))
        child = ResourceJob(run_id="run", cwd=owner.cwd, project="project",
                            liveness="working", parent_attempt_id="at-one")
        owner.resource_children = [child]
        header = self.header(self.lines([self.session()], [owner], [child]))
        self.assertTrue(any(t == "●" and k.startswith("g_work") for t, k in header))
        self.assertNotIn("✓", text([header]))

    def test_recorded_result_names_task_and_event_age(self):
        outcome = {"present": True, "match": True, "terminal_gate_proven": True,
                   "closed_at": "2026-10-10T00:00:00Z"}
        result = route.result_projection({"slug": "모델 설명 자료", "route_id": "rt-one"}, outcome)
        self.assertEqual(result["result"], "success")
        self.assertEqual(result["name"], "모델 설명 자료")
        session = self.session()
        session.route_chain = {"nodes": [{"result": result}]}
        header = text([self.header(self.lines([session]))])
        self.assertIn("최근 결과", header)
        self.assertIn("성공", header)
        self.assertIn("모델 설명 자료", header)

    def test_unproven_mismatched_or_pending_outcome_is_not_a_result(self):
        base = {"present": True, "match": True, "terminal_gate_proven": True,
                "closed_at": "2026-10-10T00:00:00Z"}
        for update in ({"terminal_gate_proven": False}, {"match": False},
                       {"finish_pending": True}, {"closed_at": "bad"},
                       {"terminal_gate_proven": 1}):
            with self.subTest(update=update):
                self.assertIsNone(route.result_projection({"slug": "task"}, dict(base, **update)))

    def test_long_result_name_preserves_result_and_event_age(self):
        s = self.session(liveness="working", work_projection=WorkProjection(
            result={"name": "매우 긴 작업 이름 " * 20, "result": "success", "at": self.now - 3600}))
        with mock.patch("time.time", return_value=self.now):
            for width in (80, 100, 160):
                with self.subTest(width=width):
                    header = text([self.header(self.lines([s], width=width))])
                    self.assertIn("최근 결과 · 성공", header)
                    self.assertIn("1시간 전", header)
                    self.assertLessEqual(render._dw(header), width)

    def test_old_summary_is_labelled_after_observed_idle(self):
        s = self.session(summary="보완 작업이 진행 중", summary_ts=self.now - 12 * 3600)
        row = text(render._context_detail_row(s, term_width=180))
        self.assertIn("대기", row)
        self.assertIn("마지막 요약", row)
        self.assertIn("12시간 전", row)
        self.assertLess(row.index("대기"), row.index("마지막 요약"))

    def test_unknown_summary_time_stays_historical_and_exec_comes_first(self):
        s = self.session(liveness="working", summary="작업 완료",
                         exec_tool={"name": "exec_command", "command": "python train.py"})
        row = text(render._context_detail_row(s, term_width=180))
        self.assertIn("마지막 요약 · 시각 미확인", row)
        self.assertLess(row.index("⚙"), row.index("마지막 요약"))

    def test_old_completion_title_is_previous_for_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            s = self.session(harness=harness, title="하팅 작업 완료")
            s.title_ts = self.now - 48 * 3600
            with self.subTest(harness=harness):
                self.assertIn("이전 제목", render._display_session_subject(s))
                s.runtime_name = "사용자 지정 이름"
                self.assertEqual(render._session_name(s), s.runtime_name)

    def test_sidecar_timestamp_only_attaches_to_matching_exact_text(self):
        s = self.session(title="하팅 작업 완료")
        with mock.patch.object(titles, "read", return_value={"title": s.title, "title_ts": self.now - 7200}):
            titles.annotate([s])
        self.assertEqual(s.title_ts, self.now - 7200)
        with mock.patch.object(titles, "read", return_value={"title": "다른 작업", "title_ts": self.now}):
            titles.annotate([s])
        self.assertIsNone(s.title_ts)

    def test_fresh_summary_still_needs_observed_working(self):
        for state in ("working", "idle", "unknown"):
            s = self.session(liveness=state, summary="새 상태 설명", summary_ts=self.now)
            row = text(render._context_detail_row(s, term_width=180))
            with self.subTest(state=state):
                self.assertIn("마지막 요약", row)
                if state == "working":
                    self.assertLess(row.index("작업 중"), row.index("마지막 요약"))

    def test_new_route_boundary_makes_previous_title_historical(self):
        s = self.session(liveness="working")
        s.title_ts = self.now - 30
        s.route_chain = {"current": {"ts": self.now - 10}}
        self.assertIn("이전 제목", render._display_session_subject(s))

    def test_fresh_completion_title_is_still_labelled_generated_text(self):
        s = self.session(liveness="working", title="하팅 작업 완료", title_ts=self.now - 60)
        self.assertEqual(render._display_session_subject(s), "제목 · 하팅 작업 완료")

    def test_recorded_resource_failure_names_its_scope_not_workflow_success(self):
        child = ResourceJob(run_id="run", node="full-run", liveness="exited",
                            exit_code=2, ended_at=self.now - 360)
        result = render._group_last_result([], [], [child])
        self.assertEqual((result["name"], result["result"]), ("자원 full-run", "failure"))
        child.ended_at = None
        self.assertIsNone(render._group_last_result([], [], [child]))

    def test_route_chain_keeps_result_event_separate_from_route_start(self):
        record = {"route_id": "rt-one", "slug": "문서 수정", "capability": "autopilot-code"}
        outcome = {"present": True, "match": True, "terminal_gate_proven": True,
                   "closed_at": "2026-10-10T00:00:00Z"}
        chain = route_chain.assemble([
            {"route_id": "rt-one", "route_file": "/route.json", "ts": 123}],
            load_record=lambda *args: record, load_outcome=lambda *args: outcome)
        node = chain["current"]
        self.assertEqual(node["ts"], 123)
        self.assertGreater(node["result"]["at"], node["ts"])

    def test_failed_refresh_keeps_successful_title_time(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                "FLEET_TITLE_STATE_DIR": tmp}), mock.patch.object(refresh_title, "run_worker", return_value=""):
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text('{"message":"please keep working"}\n')
            titles.write("sid", "Previous work complete", now=self.now - 48 * 3600)
            refresh_title.main(["--sid", "sid", "--transcript", str(transcript)])
            data = titles.read("sid")
            self.assertEqual(data["title_ts"], self.now - 48 * 3600)
            s = self.session(title=data["title"], liveness="working")
            titles.annotate([s])
            self.assertIn("이전 제목", render._display_session_subject(s))

    def test_noop_refresh_keeps_successful_title_time(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FLEET_TITLE_STATE_DIR": tmp}):
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text('{"message":"please keep working"}\n')
            titles.write("sid", "Previous work complete", offset=transcript.stat().st_size,
                         now=self.now - 48 * 3600, cursor_kind="byte-offset-v1")
            refresh_title.main(["--sid", "sid", "--transcript", str(transcript)])
            self.assertEqual(titles.read("sid")["title_ts"], self.now - 48 * 3600)

    def test_legacy_sidecar_write_time_is_not_title_generation_time(self):
        s = self.session(liveness="working")
        with mock.patch.object(titles, "read", return_value={"title": s.title, "ts": self.now}):
            titles.annotate([s])
        self.assertIsNone(s.title_ts)
        self.assertIn("이전 제목", render._display_session_subject(s))


if __name__ == "__main__":
    unittest.main()
