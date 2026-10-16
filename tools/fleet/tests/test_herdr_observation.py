"""Event-revision reuse for Fleet's optional Herdr process observations."""
import json
from collections import Counter
from concurrent.futures import Future
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

_TOOLS_DIR = Path(__file__).resolve().parents[2]
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from fleet.collectors import herdr  # noqa: E402


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
        old_transports = dict(herdr._OBSERVATION_TRANSPORTS)
        old_futures = dict(herdr._OBSERVATION_FUTURES)
        herdr._OBSERVATION_TRANSPORTS.clear()
        herdr._OBSERVATION_FUTURES.clear()
        self.addCleanup(lambda: (herdr.close_observations(),
                                herdr._OBSERVATION_TRANSPORTS.update(old_transports),
                                herdr._OBSERVATION_FUTURES.update(old_futures)))
        view = SimpleNamespace(endpoint="fleet-named", complete=True)
        class Module:
            @staticmethod
            def endpoint_name(value=None):
                return value or "fleet-named"

            @staticmethod
            def socket_path_for_endpoint(_session=None):
                return "/isolated/herdr.sock"

            class ObservationTransport:
                def __init__(self, *_args, **_kwargs):
                    pass

                def observe(self):
                    raise AssertionError("the transport must run through background_read")

                def close(self):
                    pass

        future = Future()
        with mock.patch.object(herdr, "_observation_module", return_value=Module), \
             mock.patch.object(herdr, "background_read", return_value=future) as start_read:
            self.assertIsNone(herdr.observation_view())
            self.assertIsNone(herdr.observation_view())
            start_read.assert_called_once()
            future.set_result(view)
            self.assertIs(herdr.observation_view(), view)


if __name__ == "__main__":
    unittest.main()
