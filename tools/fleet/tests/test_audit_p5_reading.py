"""Fleet 감사 P5: 이미 관측한 이름·호출자와 좁은 화면의 소비자 경계."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))
from fleet import collectors, demo, model, render
from fleet.model import DispatchJob, Session
import session_tidy_runner as runner


class ReadingTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(render.set_show_all, False)
        self.addCleanup(render.set_process_view, False)
        self.addCleanup(render.set_compute_hosts, None)
        model.reset_parent_edge_tracker()
        self.addCleanup(model.reset_parent_edge_tracker)

    def test_r2_folded_gpu_keeps_config_name(self):
        resource = dict(host="moving4", index=0, model="RTX A6000", processes=[
            dict(pid=101, proc_start=11, command="python run.py --config AMI_8ch_varying_0_3spk_v3.yaml")])
        with mock.patch.object(render, "_ROUTE_FOLD", {}):
            text = "\n".join(render._plain(x) for x in render._gpu_resource_strip([resource], 80))
        self.assertIn("AMI_8ch_varying_0_3spk_v3", text)
        self.assertNotIn("--config", text)

    def test_r5_process_header_names_work_project_and_fleet(self):
        owner = DispatchJob(key="lab", cwd="/work/TF-Rehancer", slug="resume-training", pid=2,
                            proc_start="22", harness="codex", worker_type="owner", parent_sid="sid")
        parent = Session(harness="claude", pid=1, cwd=owner.cwd, session_id="sid", session_tag="4d")
        view = dict(key="route", route_id="rt-53b700ec81f04dc7", capability="autopilot-lab",
                    capability_mode="setup", effective_intensity="standard", nodes=[
                        dict(id="resume-run", state="active", level=0, job=owner, unit="_kernel/resource")])
        lines, _ = render._route_card(view, {(1, None): parent}, 100, 0, resource_owners=[owner])
        header = render._plain(lines[0])
        for token in ("TF-Rehancer", "resume-training", "[4d]"):
            self.assertIn(token, header)
        self.assertNotIn("rt-53b", header)

    def test_r5_human_input_uses_korean(self):
        self.assertEqual(render._INTERACTION_LABEL["decision"], "답변 필요")
        self.assertEqual(render._INTERACTION_LABEL["permission"], "승인 필요")

    def test_r7_footer_preserves_keys_at_80_and_100(self):
        for width in (80, 100):
            segs = render._footer_segs(False, [], width)
            text = render._plain(segs)
            self.assertLessEqual(render._dw(text), width - 1)
            for key in ("q", "r", "a", "c", "w", "p", "jk", "s", "g/G"):
                self.assertIn(key, text)

    def test_r7_all_rows_fit_shared_width(self):
        sessions, jobs = demo.collect()
        memory = dict(recent=[dict(ts="2026-10-10T09:00:00", action="decision-record",
                                  tier="working", type="decision", actor="tidy-applier",
                                  snippet="긴 기억 내용 " * 20)], by_repo={}, totals={})
        render.set_show_all(True)
        for process in (False, True):
            render.set_process_view(process)
            for width in (80, 100):
                lines = render._build_lines(sessions, jobs, "both", False, 0,
                                            memory=memory, term_width=width)
                overflow = [render._plain(x) for x in lines if x and render._dw(render._plain(x)) > width]
                self.assertEqual(overflow, [], (process, width, overflow))

    def test_r8_context_named_and_process_legend_present(self):
        session = Session(harness="codex", pid=1, cwd="/work/a", session_id="s", ctx_pct=74,
                          liveness="working", summary="현재 작업")
        self.assertIn("문맥", "\n".join(render._plain(x) for x in render._context_detail_row(session, term_width=80)))
        render.set_process_view(True)
        text = "\n".join(render._plain(x) for x in render._build_lines([], [], "both", False, 0, term_width=80))
        for word in ("세션", "학습·장치", "단계"):
            self.assertIn(word, text)

    def test_h1_observation_links_only_confirmed_caller_continuity(self):
        parent = Session(harness="claude", pid=1, cwd="/work/a", session_id="new", liveness="working",
                         session_aliases=["old"])
        other = Session(harness="claude", pid=2, cwd=parent.cwd, session_id="other", liveness="working")
        job = DispatchJob(key="support", slug="helper", worker_type="support", cwd=parent.cwd)
        job.caller_sid, job.caller_harness, job.caller_pane = "old", "claude", "wB:p3N"
        collectors.resolve_parent_edges([parent, other], [job])
        self.assertEqual(getattr(job, "_parent_edge_sid", None), "new")
        self.assertIsNone(job.parent_sid)  # observation grants no dispatch/delivery authority
        job.caller_sid = "unconfirmed"
        collectors.resolve_parent_edges([parent, other], [job])
        self.assertIsNone(getattr(job, "_parent_edge_sid", None))

    def test_h1_unmatched_support_explicitly_project_level(self):
        support = DispatchJob(key="session-tidy", slug="helper", worker_type="support",
                              registered_worker=True, cwd="/work/a", liveness="working")
        parent = Session(harness="claude", pid=1, cwd=support.cwd, session_id="s", liveness="working")
        text = "\n".join(render._plain(x) for x in render._build_lines([parent], [support], "both", False, 0, term_width=100))
        self.assertIn("프로젝트 지원 작업", text)

    def test_h1_runner_publishes_caller_as_observation(self):
        item = dict(harness="codex", sid="caller-sid", cwd="/work/a", seat=dict(kind="pane", pane="wB:p3N"))
        receipt = dict(job_registry="/test/jobs.log", attempt_id="att-helper")
        with mock.patch("dispatch_contract.annotate_attempt_row") as annotate:
            runner.record_caller_observation(item, receipt)
        self.assertEqual(annotate.call_args.args[2], dict(caller_harness="codex", caller_sid="caller-sid",
                                                        caller_pane="wB:p3N", caller_cwd="/work/a"))


if __name__ == "__main__":
    unittest.main()
