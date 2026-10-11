"""Native same-pane resumes keep supervision, peer badges and current ID reporting."""
import os
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))
import session_tidy
from fleet import herdr_projection, render, session_handle, session_registry
from fleet.collectors import herdr, steward, procscan
from fleet.model import Session


class PaneContinuityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.foreign = self.root / "foreign"
        (self.repo / ".git").mkdir(parents=True)
        (self.foreign / ".git").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)

    def record(self, harness, sid, pane="w:p", cwd=None, source="resume", now=100):
        seat = session_tidy._pane_seat_of(pane)
        with session_tidy.seat_lock(seat):
            session_tidy.record_event(seat, harness, sid, "start", source=source,
                                      cwd=str(cwd or self.repo), now=now)

    def test_aliases_use_exact_pane_harness_repository_and_both_id_directions(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.record(harness, "old", now=100)
                self.record(harness, "current", now=200)
                self.record(harness, "other-pane", pane="w:other", now=300)
                self.record(harness, "foreign", cwd=self.foreign, now=400)
                self.assertEqual(session_tidy.pane_session_aliases(harness, "old", "w:p", str(self.repo)), ["current"])
                self.assertEqual(session_tidy.pane_session_aliases(harness, "current", "w:p", str(self.repo)), ["old"])
                self.assertEqual(session_tidy.pane_session_aliases(harness, "unknown", "w:p", str(self.repo)), [])
                self.assertEqual(session_tidy.session_start_source(harness, "current", "w:p"), "resume")

    def test_stale_native_id_joins_new_marker_peer_badge_and_herdr_role_on_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            self.record(harness, "old", now=100)
            self.record(harness, "current", now=200)
            parent = Session(harness=harness, pid=10, proc_start="start", session_id="old",
                             session_tag="4d", cwd=str(self.repo), liveness="idle")
            child = Session(harness="codex", pid=20, session_id="child", session_tag="cb",
                            cwd=str(self.repo), liveness="idle")
            child.peer_last_recv = {"from_harness": harness, "from_session_id": "current", "kind": "steer", "age_min": 0}
            markers = {(harness, "current"): {"targets": {"child": {
                "harness": "codex", "session_id": "child", "source": "start"}}}}
            agents = [{"agent": harness, "pane_id": "w:p", "name": "supervisor",
                       "cwd": str(self.repo), "agent_session": {"agent": harness, "value": "current"}}]
            with mock.patch.object(procscan, "read_proc_start", return_value="start"), \
                 mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=[]):
                herdr.enrich([parent, child], agents=agents, pids=({1}, {10}), pane_bindings={10: {"w:p"}})
            self.assertIn((harness, "current"), session_registry.session_join_keys(parent))
            with mock.patch.object(steward, "_registry_sessions", return_value=[]), \
                 mock.patch.object(steward, "_native_role_sessions", return_value=[]), \
                 mock.patch.object(steward, "read_markers", return_value=markers), \
                 mock.patch.object(steward, "_projection_sessions", return_value=[parent, child]), \
                 mock.patch.object(herdr_projection.os, "getcwd", return_value=str(self.repo)):
                steward.enrich([parent, child])
                self.assertTrue(parent.steward)
                self.assertTrue(herdr_projection.is_steward(harness, "current"))
                self.assertEqual(child.steward_parents[0]["session_id"], "old")
            rows = render._build_lines([child, parent], [], "fleet", False, 0, term_width=100, layout="narrow")
            shown = "\n".join(render._plain(row) for row in rows if row)
            self.assertIn("← [4d] " + harness, shown)
            self.assertLess(shown.index("[4d]"), shown.index("[cb]"))

    def test_another_live_session_keeps_its_identity_and_pid_reuse_adds_no_alias(self):
        self.record("codex", "old", now=100)
        self.record("codex", "current", now=200)
        old = Session(harness="codex", pid=10, proc_start="start", session_id="old", cwd=str(self.repo))
        current = Session(harness="codex", pid=20, proc_start="start", session_id="current", cwd=str(self.repo))
        from fleet.collectors import procscan
        for start in ("start", "reused"):
            with mock.patch.object(procscan, "read_proc_start", return_value=start):
                herdr.enrich([old, current], agents=[], pids=({1}, {10}), pane_bindings={10: {"w:p"}})
            self.assertNotIn(("codex", "current"), session_registry.session_join_keys(old))

    def test_same_pane_native_resume_companion_keeps_one_foreground_row_and_current_work(self):
        for harness in ("claude", "codex", "opencode"):
            self.record(harness, "old", now=100)
            self.record(harness, "current", now=200)
            old = Session(harness=harness, pid=10, proc_start="start", session_id="old",
                          session_tag="4d", cwd=str(self.repo), liveness="idle")
            resumed = Session(harness=harness, pid=20, proc_start="start", session_id="current",
                              cwd=str(self.root), detached=True, liveness="working", summary="current work")
            agents = [{"agent": harness, "pane_id": "w:p", "cwd": str(self.repo),
                       "agent_session": {"agent": harness, "value": "current"}}]
            rows = [old, resumed]
            with mock.patch.object(procscan, "read_proc_start", return_value="start"), \
                 mock.patch.object(herdr, "_ppid_of", side_effect=lambda pid: 10 if pid == 20 else 1):
                herdr.enrich(rows, agents=agents, pids=({1}, {10}), pane_bindings={10: {"w:p"}})
            self.assertEqual(rows, [old])
            self.assertEqual(old.liveness, "working")
            self.assertEqual(old.summary, "current work")
            self.assertIn((harness, "current"), session_registry.session_join_keys(old))

    def test_recorded_start_source_reaches_shared_publisher_without_weakening_guard(self):
        for harness in ("claude", "codex", "opencode"):
            self.record(harness, "current")
            with mock.patch.object(herdr_projection.shutil, "which", return_value="/fixture/herdr"), \
                 mock.patch.object(herdr_projection, "may_report", return_value=True), \
                 mock.patch.object(herdr_projection, "_report") as report:
                herdr_projection.project(harness, "current", pane_id="w:p")
                self.assertEqual(report.call_args.kwargs["session_start_source"], "resume")
                self.assertGreater(report.call_args.kwargs["session_seq"], 0)
            with mock.patch.object(herdr_projection.shutil, "which", return_value="/fixture/herdr"), \
                 mock.patch.object(herdr_projection, "may_report", return_value=False), \
                 mock.patch.object(herdr_projection, "_report") as report:
                herdr_projection.project(harness, "current", pane_id="w:p")
                report.assert_not_called()

    def test_claude_pane_badge_survives_native_id_change(self):
        self.record("claude", "old", now=100)
        self.record("claude", "current", now=200)
        home = self.root / "claude"
        (home / "sessions").mkdir(parents=True)
        (home / "sessions" / "10.json").write_text(json.dumps(
            {"sessionId": "old", "name": "hearting-4d", "nameSource": "derived"}))
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w:p", "CLAUDE_CONFIG_DIR": str(home),
                                          "FLEET_TITLE_STATE_DIR": str(self.root / "titles")}), \
             mock.patch.object(session_handle.os, "getcwd", return_value=str(self.repo)):
            self.assertEqual(session_handle.resolve_tag("claude", "current"), "4d")

    def test_resume_source_is_in_the_actual_herdr_session_command_for_all_harnesses(self):
        for harness in ("claude", "codex", "opencode"):
            with mock.patch.object(herdr_projection.shutil, "which", return_value="/fixture/herdr"), \
                 mock.patch.object(herdr_projection, "session_title", return_value=""), \
                 mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
                 mock.patch.object(herdr_projection, "compose", return_value=(harness, "")), \
                 mock.patch.object(herdr_projection.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                herdr_projection._report(harness, "current", "w:p", True, session_seq=100, session_start_source="resume")
                self.assertEqual(run.call_args_list[0].args[0][-4:], ["--seq", "100", "--session-start-source", "resume"])


if __name__ == "__main__":
    unittest.main()
