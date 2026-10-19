"""Event-revision reuse for Fleet's optional Herdr process observations."""
import json
import os
from collections import Counter
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

_TOOLS_DIR = Path(__file__).resolve().parents[2]
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from fleet.collectors import herdr  # noqa: E402


@dataclass(frozen=True)
class TestView:
    endpoint: str
    socket_path: str
    generation: int
    revision: int
    panes: tuple = ()
    pane_revisions: dict = None
    terminal: dict = None
    agents: tuple = ()
    observed_at: float = 1.0
    complete: bool = True


def _module_for(socket_path="/isolated/herdr.sock", endpoint="fleet-named"):
    class Module:
        @staticmethod
        def endpoint_name(value=None):
            return value or endpoint

        @staticmethod
        def socket_path_for_endpoint(_session=None):
            return socket_path

        class ObservationTransport:
            def __init__(self, selected_endpoint, selected_socket, **_kwargs):
                self.endpoint = selected_endpoint
                self.socket_path = selected_socket
                self.close_count = 0

            def observe(self):
                raise AssertionError("observation runs through background_read")

            def close(self):
                self.close_count += 1

    return Module


class PaneProcessReuseTest(unittest.TestCase):
    def test_unchanged_refresh_reuses_process_info_until_pane_event_or_resnapshot(self):
        old_cache = herdr._PANE_INFO_CACHE.copy()
        herdr._PANE_INFO_CACHE.clear()
        self.addCleanup(lambda: (herdr._PANE_INFO_CACHE.clear(),
                                herdr._PANE_INFO_CACHE.update(old_cache)))
        observation = SimpleNamespace(
            endpoint="fleet-named", socket_path="/isolated/herdr.sock", generation=1,
            pane_revisions={"w1:p1": 0, "w1:p2": 0})
        panes = [{"pane_id": "w1:p1", "agent": "codex"},
                 {"pane_id": "w1:p2", "agent": "opencode"}]
        calls = Counter()

        def run(argv, **_kwargs):
            pane_id = argv[-1]
            calls[pane_id] += 1
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {
                "process_info": {"shell_pid": 100,
                                 "foreground_processes": [{"pid": 200, "name": "codex"}]}}}))

        with mock.patch.object(herdr.subprocess, "run", side_effect=run) as runner:
            herdr.pane_evidence(panes, runner=runner, observation=observation)
            herdr.pane_evidence(panes, runner=runner, observation=observation)
            self.assertEqual(calls, {"w1:p1": 1, "w1:p2": 1})

            # One changed pane invalidates its own cached sample; its sibling
            # remains reusable during the same collector lifetime.
            observation.pane_revisions["w1:p1"] += 1
            herdr.pane_evidence(panes, runner=runner, observation=observation)
            self.assertEqual(calls, {"w1:p1": 2, "w1:p2": 1})

            # Reconnect/resnapshot invalidates both panes. Four refreshes over
            # two panes make five process-info calls (baseline without reuse: 8).
            observation.generation += 1
            herdr.pane_evidence(panes, runner=runner, observation=observation)
            self.assertEqual(calls, {"w1:p1": 3, "w1:p2": 2})
            self.assertEqual(runner.call_count, 5)
            self.assertTrue(all(call.args[0][1:4] == ["--session", "fleet-named", "pane"]
                                for call in runner.call_args_list))

    def test_named_endpoint_is_preserved_in_cli_agent_and_process_observations(self):
        with mock.patch.dict("os.environ", {
                "AGENT_HERDR_SESSION": "fleet-named", "HERDR_SOCKET_PATH": ""}, clear=False), \
             mock.patch.object(herdr.shutil, "which", return_value="/bin/herdr"), \
             mock.patch.object(herdr.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout=json.dumps({"result": {"agents": []}}))) as run:
            self.assertEqual(herdr.list_agents(runner=run, which=herdr.shutil.which), [])
            self.assertEqual(run.call_args.args[0],
                             ["herdr", "--session", "fleet-named", "agent", "list"])

        process_calls = []

        def process_info(argv, **_kwargs):
            process_calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": {
                "process_info": {"shell_pid": 100, "foreground_processes": []}}}))

        with mock.patch.dict("os.environ", {
                "AGENT_HERDR_SESSION": "fleet-named", "HERDR_SOCKET_PATH": ""}, clear=False), \
             mock.patch.object(herdr.subprocess, "run", side_effect=process_info) as run:
            herdr.pane_evidence([{"pane_id": "w1:p1", "agent": "codex"}], runner=run)
        self.assertEqual(process_calls, [["herdr", "--session", "fleet-named",
                                          "pane", "process-info", "--pane", "w1:p1"]])

    def test_pending_socket_observation_never_blocks_or_spawns_duplicate_readers(self):
        old = {name: dict(getattr(herdr, name)) for name in (
            "_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
            "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING")}
        for name in old:
            getattr(herdr, name).clear()
        self.addCleanup(lambda: (herdr.close_observations(),
                                [getattr(herdr, name).update(rows) for name, rows in old.items()]))
        view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1)
        future = Future()
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", return_value=future) as start_read:
            self.assertIsNone(herdr.observation_view())
            self.assertIsNone(herdr.observation_view())
            start_read.assert_called_once()
            future.set_result(view)
            self.assertIs(herdr.observation_view(), view)
            self.assertIs(herdr.observation_view(), view)
            start_read.assert_called_once()

    def test_last_complete_view_is_shared_while_one_next_read_runs(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, second = Future(), Future()
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", side_effect=[first, second]) as start_read:
            self.assertIsNone(herdr.observation_view())
            first_view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1)
            first.set_result(first_view)
            self.assertIs(herdr.observation_view(), first_view)

            key = ("fleet-named", "/isolated/herdr.sock")
            herdr._OBSERVATION_LAST_STARTED[key] -= 1.0
            stale = herdr.observation_view()
            self.assertEqual(stale.panes, first_view.panes)
            self.assertFalse(stale.complete)
            self.assertEqual(start_read.call_count, 2)
            self.assertEqual(herdr._observation_error(), "herdr refresh pending")

            same_stale = herdr.observation_view()
            self.assertFalse(same_stale.complete)
            self.assertEqual(start_read.call_count, 2)
            second_view = TestView("fleet-named", "/isolated/herdr.sock", 2, 1)
            second.set_result(second_view)
            self.assertIs(herdr.observation_view(), second_view)

    def test_failed_refresh_keeps_old_rows_but_marks_view_incomplete(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, failed = Future(), Future()
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", side_effect=[first, failed]):
            self.assertIsNone(herdr.observation_view())
            view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1,
                            panes=({"pane_id": "w1:p1"},))
            first.set_result(view)
            self.assertIs(herdr.observation_view(), view)
            key = ("fleet-named", "/isolated/herdr.sock")
            herdr._OBSERVATION_LAST_STARTED[key] -= 1.0
            pending = herdr.observation_view()
            self.assertFalse(pending.complete)
            failed.set_exception(OSError("socket lost"))
            unhealthy = herdr.observation_view()
            self.assertEqual(unhealthy.panes, view.panes)
            self.assertFalse(unhealthy.complete)
            self.assertIn("OSError: socket lost", herdr._observation_error())

    def test_refresh_discards_old_generation_and_close_clears_cached_views(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, refreshed = Future(), Future()
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", side_effect=[first, refreshed]) as start_read:
            self.assertIsNone(herdr.observation_view())
            old_view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1)
            first.set_result(old_view)
            self.assertIs(herdr.observation_view(), old_view)
            self.assertIsNone(herdr.observation_view(refresh=True))
            self.assertEqual(start_read.call_count, 2)
            self.assertIsNone(herdr.observation_view())
            new_view = TestView("fleet-named", "/isolated/herdr.sock", 2, 1)
            refreshed.set_result(new_view)
            self.assertIs(herdr.observation_view(), new_view)
            herdr.close_observations()
            self.assertFalse(herdr._OBSERVATION_VIEWS)
            self.assertFalse(herdr._OBSERVATION_FUTURES)

    def test_refresh_requested_during_read_reconnects_after_that_read_finishes(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, forced = Future(), Future()
        with mock.patch.object(herdr, "_observation_module", return_value=_module_for()), \
             mock.patch.object(herdr, "background_read", side_effect=[first, forced]) as start_read:
            self.assertIsNone(herdr.observation_view())
            self.assertIsNone(herdr.observation_view(refresh=True))
            old_view = TestView("fleet-named", "/isolated/herdr.sock", 1, 1)
            first.set_result(old_view)
            # A later ordinary consumer completes the pending refresh request;
            # it must not publish the just-finished pre-refresh generation.
            self.assertIsNone(herdr.observation_view())
            self.assertFalse(herdr._OBSERVATION_VIEWS)
            self.assertEqual(start_read.call_count, 2)
            self.assertIsNone(herdr.observation_view())
            new_view = TestView("fleet-named", "/isolated/herdr.sock", 2, 1)
            forced.set_result(new_view)
            self.assertIs(herdr.observation_view(), new_view)

    def test_same_endpoint_on_different_socket_paths_keeps_separate_views(self):
        for name in ("_OBSERVATION_TRANSPORTS", "_OBSERVATION_FUTURES", "_OBSERVATION_VIEWS",
                     "_OBSERVATION_ERRORS", "_OBSERVATION_LAST_STARTED", "_OBSERVATION_REFRESH_PENDING"):
            getattr(herdr, name).clear()
        self.addCleanup(herdr.close_observations)
        first, second = Future(), Future()
        module_a, module_b = _module_for("/sockets/a.sock"), _module_for("/sockets/b.sock")
        with mock.patch.object(herdr, "_observation_module", side_effect=[
                module_a, module_a, module_b, module_b]), \
             mock.patch.object(herdr, "background_read", side_effect=[first, second]) as start_read:
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/sockets/a.sock"}):
                self.assertIsNone(herdr.observation_view())
                view_a = TestView("fleet-named", "/sockets/a.sock", 1, 1)
                first.set_result(view_a)
                self.assertIs(herdr.observation_view(), view_a)
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/sockets/b.sock"}):
                self.assertIsNone(herdr.observation_view())
                view_b = TestView("fleet-named", "/sockets/b.sock", 1, 1)
                second.set_result(view_b)
                self.assertIs(herdr.observation_view(), view_b)
        self.assertEqual(start_read.call_count, 2)
        self.assertEqual(set(herdr._OBSERVATION_VIEWS.values()), {view_a, view_b})


if __name__ == "__main__":
    unittest.main()
