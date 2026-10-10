"""Fleet audit P3: failed observations never establish negative facts."""
import json
import os
from pathlib import Path
import sys
import threading
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import collectors, details, herdr_projection, projection, render, session_tags
from fleet.collectors import dispatch, herdr, procscan, resource_runs, usage_cache
from fleet.model import DispatchJob, Session
from fleet.refresh import LiveSnapshot


class PaneFailures(unittest.TestCase):
    def test_partial_probe_preserves_positive_match_and_unknown_nonmatch(self):
        panes = [{"pane_id": "good", "agent": "codex"},
                 {"pane_id": "failed", "agent": "claude"}]

        def runner(argv, **kwargs):
            if argv[-1] == "failed":
                raise TimeoutError("pane unavailable")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {
                "process_info": {"foreground_processes": [{"pid": 4321, "name": "codex"}]}}}))

        pids = herdr.pane_pids(panes, runner=runner)
        sessions = [Session(harness="codex", pid=4321),
                    Session(harness="claude", pid=9876),
                    Session(harness="claude", pid=9877, session_id="unmatched")]
        with mock.patch.object(herdr, "_ppid_of", return_value=1):
            herdr.enrich(sessions, agents=[], pids=pids)
        self.assertEqual([s.herdr_attached for s in sessions], [True, None, None])

    def test_pane_waits_overlap_in_one_bounded_observation(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def runner(argv, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.025)
            with lock:
                active -= 1
            raise TimeoutError("pane unavailable")

        herdr.pane_pids([{"pane_id": str(i), "agent": "codex"} for i in range(12)], runner=runner)
        self.assertGreater(maximum, 1)


class JobFailures(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for patch in (
            mock.patch.dict(os.environ, {"XDG_STATE_HOME": state.name}),
            mock.patch.object(session_tags, "refresh"),
            mock.patch.object(session_tags, "assign", return_value=[], create=True),
            mock.patch.object(herdr_projection, "_report"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_registry_candidates_propagate_inaccessible_paths(self):
        with mock.patch.object(dispatch.os, "stat", side_effect=PermissionError("NAS denied")):
            with self.assertRaises(PermissionError):
                dispatch._installed_registry_paths()
            with mock.patch.dict(dispatch.os.environ, {}, clear=True), \
                 mock.patch.object(dispatch, "_jobs_path", return_value="/audit/jobs.log"):
                with self.assertRaises(PermissionError):
                    dispatch._candidate_jobs_paths()
            with self.assertRaises(PermissionError):
                dispatch._validated_split_registry_paths(
                    [], session_rows=[Session(harness="codex", pid=1, session_id="parent")],
                    observed={"/audit/split.log": {"attempt"}})

    def test_first_failed_census_never_claims_no_work(self):
        with mock.patch.object(procscan, "scan", return_value=[]), \
             mock.patch.object(herdr, "enrich"), \
             mock.patch.object(usage_cache, "account_usage", return_value={}), \
             mock.patch.object(resource_runs, "collect", return_value=[]), \
             mock.patch.object(projection, "attach_projections"), \
             mock.patch.object(dispatch, "collect", side_effect=PermissionError("NAS denied")):
            failed = collectors.collect_all(jobs_path="/audit/p3/first-failure", fast_first=True)
        for process_view in (False, True):
            with self.subTest(process_view=process_view), \
                 mock.patch.object(render, "_PROCESS_VIEW", process_view), \
                 mock.patch.object(render, "_compute_host_rows", return_value=[]), \
                 mock.patch.object(render, "_fresh_compute_hosts", return_value=(None, None)):
                text = "\n".join(render._plain(line) for line in render._build_lines(
                    failed.sessions, failed.jobs, "both", False, 0,
                    observations=failed.observations, governor=None))
                self.assertIn("jobs.log 미확인", text)
                self.assertNotIn("no active", text)
                self.assertNotIn("0 jobs", text)

    def test_registry_permission_error_is_not_an_empty_census(self):
        with mock.patch("builtins.open", side_effect=PermissionError("NAS denied")):
            with self.assertRaises(PermissionError):
                dispatch._scan_jobs_log("/audit/jobs.log", set())
            with self.assertRaises(PermissionError):
                dispatch._jobs_log_fields(["/audit/jobs.log"])

    def test_last_jobs_and_failure_are_one_publication_then_recover(self):
        owner = DispatchJob(key="owner", slug="audit-owner", attempt_id="audit-attempt",
                            harness="codex", liveness="working")
        with mock.patch.object(procscan, "scan", return_value=[]), \
             mock.patch.object(herdr, "enrich"), \
             mock.patch.object(usage_cache, "account_usage", return_value={}), \
             mock.patch.object(resource_runs, "collect", return_value=[]), \
             mock.patch.object(projection, "attach_projections"), \
             mock.patch.object(dispatch, "collect", side_effect=[[owner], RuntimeError("NAS denied"), []]):
            first = collectors.collect_all(jobs_path="/audit/p3/isolated", fast_first=True)
            failed = collectors.collect_all(jobs_path="/audit/p3/isolated", fast_first=True)
            recovered = collectors.collect_all(jobs_path="/audit/p3/isolated", fast_first=True)
        self.assertEqual([j.attempt_id for j in failed.jobs], ["audit-attempt"])
        self.assertIsNot(failed.jobs[0], first.jobs[0])
        self.assertIn("NAS denied", failed.observations["jobs"]["last_error"])
        self.assertEqual(failed.observations["jobs"]["state"], "failed")
        self.assertEqual(failed.jobs[0].liveness, "unknown")
        with mock.patch.object(render, "_PROCESS_VIEW", False):
            lines = render._build_lines(failed.sessions, failed.jobs, "both", False, 0,
                                        observations=failed.observations, governor=None)
        text = "\n".join(render._plain(line) for line in lines)
        self.assertIn("jobs.log 미확인", text)
        self.assertIn("audit-owner", text)
        self.assertEqual(recovered.jobs, [])
        self.assertEqual(recovered.observations["jobs"]["state"], "idle")


class PaneMetadata(unittest.TestCase):
    def test_pane_metadata_wait_cannot_delay_basic_publication(self):
        with tempfile.TemporaryDirectory() as state:
            agents = [{"agent": "codex", "pane_id": str(i),
                       "agent_session": {"kind": "id", "value": "audit-pane-%d" % i}}
                      for i in range(6)]
            live = {("codex", "audit-pane-%d" % i):
                    {"harness": "codex", "session_id": "audit-pane-%d" % i,
                     "started_at": 10, "pid": 1, "proc_start": "audit"}
                    for i in range(6)}
            published, reporting, release = threading.Event(), threading.Event(), threading.Event()
            values = []

            def report(*args, **kwargs):
                reporting.set()
                release.wait(3)

            def collect():
                values.append(collectors.collect_all(jobs_path=state + "/jobs", fast_first=True))
                published.set()

            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}), \
                 mock.patch.object(session_tags, "_inventory", side_effect=lambda rows: (live.copy(), agents)), \
                 mock.patch.object(herdr_projection, "_send_projection", autospec=True, side_effect=report) as writer, \
                 mock.patch.multiple(herdr_projection, session_title=lambda *a: "",
                                     _formatter_overrides=lambda *a: (None, None),
                                     compose=lambda *a, **k: ("[ab] codex", ""),
                                     resolve_tag=lambda *a: "ab"), \
                 mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"), \
                 mock.patch.object(herdr, "list_agents", return_value=agents), \
                 mock.patch.object(procscan, "scan", return_value=[]), \
                 mock.patch.object(herdr, "enrich"), \
                 mock.patch.object(usage_cache, "account_usage", return_value={}), \
                 mock.patch.object(resource_runs, "collect", return_value=[]), \
                 mock.patch.object(projection, "attach_projections"), \
                 mock.patch.object(dispatch, "collect", return_value=[]), \
                 mock.patch.object(dispatch, "_fill_locations"), \
                 mock.patch.object(dispatch, "_campaign_labels"), \
                 mock.patch.object(dispatch, "_scan_degradations", return_value={}), \
                 mock.patch.object(dispatch, "_pending_delivery_counts", return_value={}), \
                 mock.patch("fleet.route_chain.enrich"), \
                 mock.patch("fleet.collectors.peer_messages.collect", return_value=None):
                basic = threading.Thread(target=collect, daemon=True)
                detail = None
                basic.start()
                try:
                    self.assertTrue(published.wait(1), "pane metadata blocked basic publication")
                    writer.assert_not_called()
                    self.assertEqual(values[0].tag_metadata, agents)
                    detail = threading.Thread(target=details._enrich, args=(values[0],), daemon=True)
                    detail.start()
                    self.assertTrue(reporting.wait(1), "detail did not update pane metadata")
                    self.assertFalse(release.is_set())
                    collectors.collect_all(jobs_path=state + "/jobs", fast_first=True)
                finally:
                    release.set()
                    basic.join(3)
                    if detail is not None:
                        detail.join(3)
                self.assertFalse(basic.is_alive())
                self.assertFalse(detail.is_alive())
                self.assertEqual(writer.call_count, len(agents))

    def test_delayed_metadata_never_writes_to_a_changed_or_unconfirmed_pane(self):
        observed = {"agent": "codex", "pane_id": "pane-a",
                    "agent_session": {"kind": "id", "value": "observed-sid"}}
        changed = {**observed, "agent_session": {"kind": "id", "value": "new-sid"}}
        for current, count in (([observed], 1), ([changed], 0), (None, 0),
                               ([observed, changed], 0)):
            with self.subTest(current=current), \
                 mock.patch.object(herdr, "list_agents", return_value=current), \
                 mock.patch.object(herdr_projection, "resolve_tag", return_value="ab"), \
                 mock.patch.object(herdr_projection, "_send_projection", autospec=True) as writer, \
                 mock.patch.object(herdr_projection, "session_title", return_value=""), \
                 mock.patch.object(herdr_projection, "_formatter_overrides", return_value=(None, None)), \
                 mock.patch.object(herdr_projection, "compose", return_value=("[ab] codex", "")), \
                 mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"), \
                 mock.patch.object(dispatch, "_fill_locations"), \
                 mock.patch.object(dispatch, "_campaign_labels"), \
                 mock.patch.object(dispatch, "_scan_degradations", return_value={}), \
                 mock.patch.object(dispatch, "_pending_delivery_counts", return_value={}), \
                 mock.patch.object(projection, "attach_projections"), \
                 mock.patch("fleet.route_chain.enrich"), \
                 mock.patch("fleet.collectors.peer_messages.collect", return_value=None):
                details._enrich(LiveSnapshot(tag_metadata=[observed]))
                self.assertEqual(writer.call_count, count)

    def test_formatter_delay_cannot_write_old_metadata_after_pane_switch(self):
        observed = {"agent": "codex", "pane_id": "pane-a",
                    "agent_session": {"kind": "id", "value": "observed-sid"}}
        current = [observed]

        def formatter(*args):
            current[:] = [{**observed, "agent_session": {"kind": "id", "value": "new-sid"}}]
            return None, None

        with mock.patch.object(herdr, "list_agents", side_effect=lambda: current.copy()), \
             mock.patch.object(herdr_projection, "resolve_tag", return_value="ab"), \
             mock.patch.object(herdr_projection, "_send_projection", autospec=True) as writer, \
             mock.patch.object(herdr_projection, "session_title", return_value=""), \
             mock.patch.object(herdr_projection, "_formatter_overrides", side_effect=formatter), \
             mock.patch.object(herdr_projection, "compose", return_value=("[ab] codex", "")), \
             mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"):
            herdr_projection.refresh_observed_tag_metadata([observed])
        writer.assert_not_called()

    def test_identity_lookup_delay_cannot_write_a_reassigned_number(self):
        observed = {"agent": "codex", "pane_id": "pane-a",
                    "agent_session": {"kind": "id", "value": "observed-sid"}}
        tag = ["ab"]

        def current_agents():
            tag[:] = ["cd"]
            return [observed]

        with mock.patch.object(herdr, "list_agents", side_effect=current_agents), \
             mock.patch.object(herdr_projection, "resolve_tag", side_effect=lambda *a: tag[0]), \
             mock.patch.object(herdr_projection, "_send_projection", autospec=True) as writer, \
             mock.patch.object(herdr_projection, "_metadata_command",
                               return_value=["herdr", "pane", "report-metadata", "pane-a",
                                             "--display-agent", "[ab] codex"]), \
             mock.patch.object(herdr_projection.shutil, "which", return_value="herdr"):
            herdr_projection.refresh_observed_tag_metadata([observed])
        writer.assert_not_called()


class PublishedSnapshots(unittest.TestCase):
    def tearDown(self):
        render.set_refresh_health()

    def test_once_thread_start_failure_keeps_basic_screen(self):
        from fleet import refresh
        import io
        with mock.patch.object(refresh.threading.Thread, "start", side_effect=RuntimeError("no threads")), \
             mock.patch.object(render, "_collect_memory", return_value=None), \
             mock.patch.object(render.gitinfo, "enrich_entities"), \
             mock.patch.object(render, "_build_lines", return_value=[]) as build, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            render.render_once(lambda **kwargs: LiveSnapshot(), None, "both",
                               compute_hosts_refresh=lambda: None)
        self.assertEqual(build.call_args.kwargs["observations"]["governor"]["state"], "failed")
        self.assertEqual(build.call_args.kwargs["observations"]["compute hosts"]["state"], "failed")

    def test_detail_failure_reaches_existing_health_display(self):
        render.set_refresh_health(snapshot={"state": "idle", "age": 0},
                                  details={"state": "failed", "age": 55,
                                           "last_error": "OSError: NAS denied"})
        text = "".join(t for t, _ in render._refresh_health_segments())
        self.assertIn("상세 미확인", text)
        self.assertIn("NAS denied", text)
        self.assertIn("55s", text)

    def test_actual_loop_publishes_detail_failure_while_basic_refresh_succeeds(self):
        self.addCleanup(setattr, render, "_BLINK_ON", render._BLINK_ON)
        observed = threading.Event()
        values = []

        def collector(**kwargs):
            return LiveSnapshot()

        def failed(source):
            raise OSError("detail NAS denied")

        collector.detail_refresh = failed

        def draw(*args, **kwargs):
            detail = render._REFRESH_HEALTH.get("details") or {}
            if detail.get("state") == "failed":
                values.append(render._REFRESH_HEALTH)
                observed.set()

        class Screen:
            def timeout(self, value):
                pass
            def getmaxyx(self):
                return 50, 168
            def getch(self):
                return ord("q") if observed.wait(0.01) else -1

        with mock.patch.object(render, "_init_colors"), \
             mock.patch.object(render, "_draw", side_effect=draw), \
             mock.patch.object(render.curses, "curs_set"), \
             mock.patch.object(render, "_collect_memory", return_value=None), \
             mock.patch.object(render, "_collect_governor", return_value=None), \
             mock.patch.object(render.gitinfo, "enrich_entities"), \
             mock.patch.object(render, "_poll_pending_kill"), \
             mock.patch.object(render, "_handle_base_key", return_value=False), \
             mock.patch.object(render, "_PROMPT", None), \
             mock.patch.object(render, "_SELECT_MODE", False):
            worker = threading.Thread(target=render._loop,
                                      args=(Screen(), collector, None, "both", 60.0), daemon=True)
            worker.start()
            self.assertTrue(observed.wait(3.0))
            worker.join(3.0)
            self.assertFalse(worker.is_alive())
        self.assertIn("detail NAS denied", values[-1]["details"]["last_error"])
        self.assertIsNone(values[-1]["details"]["last_success_at"])
        self.assertNotEqual(values[-1]["snapshot"]["state"], "failed")

    def test_render_never_reprojects_bare_rows_or_reads_collector_globals(self):
        evidence = {"rt-audit": {"report": {"route_file": "/audit/route.json"}}}
        with mock.patch.object(dispatch.collect, "last_route_nodes", evidence), \
             mock.patch.object(projection, "attach_projections") as attach:
            render._build_lines([], [], "both", False, 0)
            render._build_lines([], [DispatchJob(key="audit", slug="audit")], "both", False, 0)
        attach.assert_not_called()

    def test_detail_publication_does_not_write_collector_side_channels(self):
        previous = {"audit": "old"}
        with mock.patch.object(dispatch.collect, "last_degradations", previous), \
             mock.patch.object(dispatch.collect, "last_pending_delivery", previous), \
             mock.patch.object(dispatch, "_fill_locations"), \
             mock.patch.object(dispatch, "_campaign_labels"), \
             mock.patch.object(dispatch, "_scan_degradations", return_value={"audit": "new"}), \
             mock.patch.object(dispatch, "_pending_delivery_counts", return_value={}), \
             mock.patch.object(projection, "attach_projections"), \
             mock.patch("fleet.route_chain.enrich"), \
             mock.patch("fleet.collectors.peer_messages.collect", return_value=None):
            basic = LiveSnapshot()
            detail = details._enrich(basic)
            self.assertEqual(details.merge(basic, detail).degradations, {"audit": "new"})
            changed = LiveSnapshot(jobs=[DispatchJob(key="new", slug="new")])
            self.assertEqual(details.merge(changed, detail).degradations, {})
            self.assertEqual(dispatch.collect.last_degradations, previous)
            self.assertEqual(dispatch.collect.last_pending_delivery, previous)


if __name__ == "__main__":
    unittest.main()
