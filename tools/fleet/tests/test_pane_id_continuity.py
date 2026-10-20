"""Native same-pane resumes keep supervision, peer badges and current ID reporting."""
import os
import json
import importlib.util
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

    def handover(self, harness, older, newer, pane="w:p", source="clear", now=500):
        seat = session_tidy._pane_seat_of(pane)
        row = {"ts": now, "harness": harness, "sid": newer, "event": "handover",
               "from": older, "source": source}
        with session_tidy.seat_lock(seat):
            session_tidy._append_ledger_entry(seat, row)

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

    def test_confirmed_handover_chain_survives_compact_resume_and_recent_alias_limit(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                ids = [f"{harness}-{index}" for index in range(10)]
                self.record(harness, ids[0], source="startup", now=100)
                for index, sid in enumerate(ids[1:], start=1):
                    if harness == "opencode":
                        seat = session_tidy._pane_seat_of("w:p")
                        with session_tidy.seat_lock(seat):
                            session_tidy.record_event(seat, harness, sid, "prompt",
                                                      cwd=str(self.repo), now=100 + index)
                        source = "first-message"
                    else:
                        self.record(harness, sid, source="clear", now=100 + index)
                        source = "clear"
                    self.handover(harness, ids[index - 1], sid, source=source, now=200 + index)
                    seat = session_tidy._pane_seat_of("w:p")
                    with session_tidy.seat_lock(seat):
                        session_tidy.record_event(seat, harness, sid, "compact", now=300 + index)
                        session_tidy.record_event(seat, harness, sid, "start", source="resume",
                                                  cwd=str(self.repo), now=400 + index)

                expected = list(reversed(ids[:-1]))
                self.assertEqual(session_tidy.confirmed_pane_predecessors(
                    harness, ids[-1], "w:p", str(self.repo)), expected)
                self.assertEqual(herdr._clear_gpu_session_aliases(
                    harness, ids[-1], "w:p", str(self.repo)), expected)
                aliases = session_tidy.pane_session_aliases(harness, ids[-1], "w:p", str(self.repo))
                self.assertEqual(set(aliases), set(ids[:-1]))
                self.assertEqual(len(aliases), 9)

    def test_legacy_endpoint_ledger_requires_exact_sid_and_repository_bridge(self):
        with mock.patch.dict(os.environ, {"AGENT_HERDR_SESSION": "fixture-current",
                                         "HERDR_SOCKET_PATH": ""}):
            current = session_tidy._pane_seat_of("w:p")
            legacy = session_tidy.Seat("pane", session_tidy._digest("pane", "w:p"),
                                       "w:p", "codex")
            with session_tidy.seat_lock(current):
                session_tidy.record_event(current, "codex", "shared", "start",
                                          source="startup", cwd=str(self.repo), now=300)
            with session_tidy.seat_lock(legacy):
                session_tidy.record_event(legacy, "codex", "older", "start",
                                          source="startup", cwd=str(self.repo), now=100)
                session_tidy.record_event(legacy, "codex", "shared", "start",
                                          source="clear", cwd=str(self.repo), now=200)
                session_tidy._append_ledger_entry(legacy, {
                    "ts": 250, "harness": "codex", "sid": "shared", "event": "handover",
                    "from": "older", "source": "clear"})
            paths = (session_tidy._ledger_path(current), session_tidy._ledger_path(legacy))
            before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
            self.assertEqual(session_tidy.confirmed_pane_predecessors(
                "codex", "shared", "w:p", str(self.repo)), ["older"])
            self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_other_endpoint_and_unbridged_legacy_rows_are_not_read_as_lineage(self):
        with mock.patch.dict(os.environ, {"AGENT_HERDR_SESSION": "fixture-current",
                                         "HERDR_SOCKET_PATH": ""}):
            current = session_tidy._pane_seat_of("w:p")
            other_endpoint_id = session_tidy._digest("herdr-endpoint", "session", "fixture-other")
            other_endpoint = session_tidy.Seat(
                "pane", session_tidy._digest("pane", other_endpoint_id, "w:p"),
                "w:p", "codex", endpoint=other_endpoint_id)
            legacy = session_tidy.Seat("pane", session_tidy._digest("pane", "w:p"),
                                       "w:p", "codex")
            with session_tidy.seat_lock(current):
                session_tidy.record_event(current, "codex", "current", "start",
                                          source="startup", cwd=str(self.repo), now=300)
            for seat, successor in ((other_endpoint, "current"), (legacy, "legacy-current")):
                with session_tidy.seat_lock(seat):
                    session_tidy.record_event(seat, "codex", "foreign-old", "start",
                                              source="startup", cwd=str(self.repo), now=100)
                    session_tidy.record_event(seat, "codex", successor, "start",
                                              source="clear", cwd=str(self.repo), now=200)
                    session_tidy._append_ledger_entry(seat, {
                        "ts": 250, "harness": "codex", "sid": successor, "event": "handover",
                        "from": "foreign-old", "source": "clear"})
            self.assertEqual(session_tidy.confirmed_pane_predecessors(
                "codex", "current", "w:p", str(self.repo)), [])

    def test_conflicting_foreign_pane_metadata_invalidates_same_sid_evidence(self):
        seat = session_tidy._pane_seat_of("w:p")
        self.record("codex", "old", source="startup", now=100)
        self.record("codex", "current", source="clear", now=200)
        with session_tidy.seat_lock(seat):
            session_tidy._append_ledger_entry(seat, {
                "ts": 250, "harness": "codex", "sid": "current", "event": "handover",
                "from": "old", "source": "clear", "pane": "w:foreign"})
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), [])

    def test_clear_start_uses_the_exact_booking_and_immediate_same_harness_predecessor(self):
        import session_tidy_clear
        seat = session_tidy._pane_seat_of("w:p")
        self.record("codex", "first", source="startup", now=100)
        self.record("codex", "middle", source="resume", now=200)
        self.record("codex", "current", source="clear", now=300)
        booking = {"schema": 1, "nonce": "fixture", "status": "cleared", "seat": seat.as_dict(),
                   "harness": "codex", "sid": "middle", "new_session": "current",
                   "observed": {"harness": "codex", "sid": "current"}, "cwd": str(self.repo)}
        session_tidy_clear._write_reservation(booking)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), ["middle"])

        booking["observed"] = {"harness": "codex", "sid": "other"}
        session_tidy_clear._write_reservation(booking)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), [])

        booking.update(observed={"harness": "codex", "sid": "current"}, sid="first")
        session_tidy_clear._write_reservation(booking)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), [])

    def test_folded_start_provenance_confirms_clear_when_raw_predecessor_start_is_gone(self):
        import session_tidy_clear
        seat = session_tidy._pane_seat_of("w:p")
        with session_tidy.seat_lock(seat):
            session_tidy._append_ledger_entry(seat, {
                "ts": 150, "harness": "codex", "sid": "folded-old", "event": "summary",
                "cwd": str(self.repo), "latest_start_source": "startup", "latest_start_at": 100})
            session_tidy.record_event(seat, "codex", "current", "start", source="clear",
                                      cwd=str(self.repo), now=300)
        session_tidy_clear._write_reservation({
            "schema": 1, "nonce": "folded-fixture", "status": "cleared", "seat": seat.as_dict(),
            "harness": "codex", "sid": "folded-old", "new_session": "current",
            "observed": {"harness": "codex", "sid": "current"}, "cwd": str(self.repo)})
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), ["folded-old"])

    def test_foreign_repository_and_harness_handover_evidence_is_ignored(self):
        self.record("codex", "foreign-repo-old", cwd=self.foreign, source="startup", now=100)
        self.record("codex", "repo-current", source="resume", now=200)
        self.handover("codex", "foreign-repo-old", "repo-current", now=300)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "repo-current", "w:p", str(self.repo)), [])

        self.record("codex", "harness-old", source="startup", now=400)
        self.record("codex", "harness-current", source="resume", now=500)
        self.handover("claude", "harness-old", "harness-current", now=600)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "harness-current", "w:p", str(self.repo)), [])

    def test_ambiguous_predecessors_and_cycles_do_not_repeat_ids(self):
        for sid, now in (("a", 100), ("b", 200), ("c", 300), ("current", 400)):
            self.record("codex", sid, source="startup", now=now)
        self.handover("codex", "a", "current", now=500)
        self.handover("codex", "b", "current", now=501)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "current", "w:p", str(self.repo)), [])

        self.handover("codex", "a", "b", now=600)
        self.handover("codex", "b", "a", now=601)
        self.assertEqual(session_tidy.confirmed_pane_predecessors(
            "codex", "b", "w:p", str(self.repo)), ["a"])

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
            self.assertIn("← [4d]", shown)
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

    def test_codex_projection_reports_start_lifecycle_for_start_resume_and_fork(self):
        lifecycle_path = ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py"
        lifecycle_spec = importlib.util.spec_from_file_location("codex_sessionstart_lifecycle_test",
                                                                 lifecycle_path)
        lifecycle = importlib.util.module_from_spec(lifecycle_spec)
        lifecycle_spec.loader.exec_module(lifecycle)
        projection_path = ROOT / "adapters/codex/hooks/herdr_session_projection.py"
        projection_spec = importlib.util.spec_from_file_location("codex_herdr_projection_test",
                                                                  projection_path)
        projection = importlib.util.module_from_spec(projection_spec)
        projection_spec.loader.exec_module(projection)
        for source, current_thread in (("startup", "new-thread"),
                                       ("resume", "resumed-thread"),
                                       ("fork", "fork-current-thread")):
            with self.subTest(source=source):
                payload = {"hook_event_name": "SessionStart", "source": source,
                           "session_id": current_thread}
                self.assertEqual(lifecycle.session_id(payload), current_thread)
                with mock.patch.object(projection, "_project", return_value=True) as publish, \
                        mock.patch("peer_obligations.request_obligation_recovery"):
                    self.assertTrue(projection.project(payload, current_thread))
                args, kwargs = publish.call_args
                self.assertEqual(args, ("codex", current_thread))
                self.assertIs(kwargs.get("report_session", True), True)
                self.assertGreater(kwargs["session_seq"], 0)
                self.assertEqual(kwargs["session_start_source"], source)
                self.assertNotIn("resume_argv", kwargs)

    def test_proven_embedded_sessionstart_keeps_reporting_with_the_v10_state_hook(self):
        path = ROOT / "adapters/codex/hooks/herdr_session_projection.py"
        spec = importlib.util.spec_from_file_location("codex_embedded_projection_test", path)
        projection = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(projection)
        payload = {"hook_event_name": "SessionStart", "session_id": "thread-A",
                   "transcript_path": "/fixture/rollout.jsonl", "source": "resume"}
        with tempfile.TemporaryDirectory(prefix="codex-home-") as temporary:
            (Path(temporary) / "herdr-agent-state.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            env = {"HERDR_SOCKET_PATH": "/fixture/herdr.sock", "HERDR_PANE_ID": "w:p",
               "CODEX_HOME": temporary,
               "CODEX_THREAD_ID": "thread-A"}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(projection, "_may_report", return_value=True), \
                 mock.patch.object(projection, "_project", return_value=True) as publish, \
                 mock.patch("peer_obligations.request_obligation_recovery"):
                projection.project(payload, "thread-A")
        kwargs = publish.call_args.kwargs
        self.assertIs(kwargs["report_session"], False)
        self.assertEqual(kwargs["session_start_source"], "resume")
        self.assertGreater(kwargs["session_seq"], 0)

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

    def test_recipient_tag_uses_recipient_pane_and_cwd_across_resume(self):
        self.record("claude", "sender-old", pane="w:sender", now=100)
        self.record("claude", "current", pane="w:sender", now=200)
        self.record("claude", "recipient-old", pane="w:recipient", now=100)
        self.record("claude", "current", pane="w:recipient", now=200)
        home = self.root / "claude-recipient"
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        for pid, sid, name in (("11", "sender-old", "sender-4c"),
                               ("12", "recipient-old", "recipient-3a")):
            (sessions / f"{pid}.json").write_text(json.dumps(
                {"sessionId": sid, "name": name, "nameSource": "derived"}),
                encoding="utf-8")
        with mock.patch.dict(os.environ, {
                "HERDR_PANE_ID": "w:sender", "CLAUDE_CONFIG_DIR": str(home),
                "FLEET_TITLE_STATE_DIR": str(self.root / "titles")}), \
                mock.patch.object(session_handle.os, "getcwd", return_value=str(self.repo)):
            # The compatibility API retains its caller-seat behavior.
            self.assertEqual(session_handle.resolve_tag("claude", "current"), "4c")
            # A recipient lookup follows only that recipient's resumed seat.
            self.assertEqual(session_handle.resolve_recipient_tag(
                "claude", "current", pane="w:recipient", cwd=str(self.repo)), "3a")

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
