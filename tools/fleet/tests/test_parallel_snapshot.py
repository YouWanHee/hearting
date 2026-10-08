"""Independent reads overlap without publishing an incomplete snapshot."""
import contextlib
import io
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import fleet, refresh, render


class ParallelSnapshotTest(unittest.TestCase):
    def setUp(self):
        for name in ("_COMPUTE_HOSTS", "_COMPUTE_HOSTS_SET_AT", "_GIT_TELEMETRY"):
            self.addCleanup(setattr, render, name, getattr(render, name))

    def quiet_renderer(self):
        stack = contextlib.ExitStack()
        for name in ("set_show_all", "set_api_disabled", "set_hearting"):
            stack.enter_context(mock.patch.object(render, name))
        stack.enter_context(mock.patch.object(render.gitinfo, "enrich_entities"))
        stack.enter_context(mock.patch.object(render, "_malformed", return_value=0))
        stack.enter_context(mock.patch.object(render, "_collect_memory", return_value=None))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        return stack

    def test_native_once_starts_both_reads_before_waiting_for_sessions(self):
        hosts_started, governor_started = threading.Event(), threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        hosts = {"hosts": [], "configured": True}
        governor = {"active": 2, "cap": 30}
        main_thread = threading.get_ident()

        def independent(event, result):
            event.set()
            self.assertNotEqual(threading.get_ident(), main_thread)
            self.assertTrue(threading.current_thread().daemon)
            self.assertTrue(release.wait(5.0))
            return result

        def sessions(harness_filter=None):
            try:
                self.assertTrue(hosts_started.wait(2.0))
                self.assertTrue(governor_started.wait(2.0))
            finally:
                release.set()
            return [], []

        sessions.last_resource_jobs = ["resource"]
        sessions.last_usage_snapshots = {"codex": "cached"}

        def build(*args, **kwargs):
            self.assertEqual(threading.get_ident(), main_thread)
            self.assertEqual(render._COMPUTE_HOSTS, hosts)
            self.assertIs(kwargs["governor"], governor)
            self.assertEqual(kwargs["resources"], ["resource"])
            self.assertEqual(kwargs["usage_snapshots"], {"codex": "cached"})
            return [[("snapshot", None)]]

        with self.quiet_renderer(), \
             mock.patch.object(fleet, "_arm_stall_dump"), \
             mock.patch.object(fleet, "_disabled_tokens", return_value={"api_disabled": False}), \
             mock.patch.object(fleet.installinfo, "collect", return_value={}), \
             mock.patch.object(fleet, "collect_all", side_effect=sessions) as collector, \
             mock.patch.object(fleet.compute_hosts, "collect", side_effect=lambda: independent(hosts_started, hosts)), \
             mock.patch.object(render, "_collect_governor", side_effect=lambda: independent(governor_started, governor)), \
             mock.patch.object(render, "_build_lines", side_effect=build):
            collector.last_resource_jobs = sessions.last_resource_jobs
            collector.last_usage_snapshots = sessions.last_usage_snapshots
            self.assertEqual(fleet.main(["--once"]), 0)

    def test_host_exception_keeps_existing_snapshot_and_propagates(self):
        previous = {"hosts": ["old"]}
        render.set_compute_hosts(previous)

        def failed():
            raise ValueError("host failure")

        with self.quiet_renderer(), \
             mock.patch.object(render, "_collect_governor", return_value=None), \
             mock.patch.object(render, "_build_lines") as build:
            with self.assertRaisesRegex(ValueError, "host failure"):
                render.render_once(lambda **kw: ([], []), None, "both", compute_hosts_refresh=failed)
            build.assert_not_called()
        self.assertEqual(render._COMPUTE_HOSTS, previous)

    def test_governor_failure_stays_optional(self):
        from fleet.collectors import governor
        with self.quiet_renderer(), \
             mock.patch.object(governor, "collect", side_effect=OSError("missing")), \
             mock.patch.object(render, "_build_lines", return_value=[]) as build:
            self.assertEqual(render.render_once(lambda **kw: ([], []), None, "both"), 0)
        self.assertIsNone(build.call_args.kwargs["governor"])

    def test_background_read_transfers_exception(self):
        def failed():
            raise LookupError("read failure")
        with self.assertRaisesRegex(LookupError, "read failure"):
            refresh.background_read(failed).result(timeout=2.0)

    def test_thread_exhaustion_uses_synchronous_read(self):
        caller = threading.get_ident()
        with mock.patch.object(refresh.threading.Thread, "start", side_effect=RuntimeError("no threads")):
            self.assertEqual(refresh.background_read(threading.get_ident).result(), caller)


if __name__ == "__main__":
    unittest.main()
