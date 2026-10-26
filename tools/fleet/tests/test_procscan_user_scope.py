#!/usr/bin/env python3
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet.collectors import dispatch, procscan  # noqa: E402
from fleet import projection  # noqa: E402


class CurrentUserProcessScopeTest(unittest.TestCase):
    def _scan_owner(self, **overrides):
        env = {"AGENT_DISPATCH_WORKER_TYPE": "owner", "AGENT_DISPATCH_DEPTH": "1",
               "AGENT_DISPATCH_ATTEMPT_ID": "exact-owner-attempt",
               "AGENT_ROUTE_FILE": "/route.json", "AGENT_ROUTE_ID": "rt-owner",
               "AGENT_OWNER_ROUTE_FILE": "/route.json",
               "AGENT_OWNER_ROUTE_ID": "rt-owner",
               "AGENT_OWNER_ROUTE_HASH": "sha256:owner"}
        env.update(overrides)
        with mock.patch.object(procscan, "_ps_lines", return_value=["101 codex 00:01 codex"]), \
             mock.patch.object(procscan, "_pid_ttys", return_value={}), \
             mock.patch.object(procscan, "_detached_ttys", return_value=set()), \
             mock.patch.object(procscan, "proc_tree", return_value={}), \
             mock.patch.object(procscan, "is_terminal_state", return_value=False), \
             mock.patch.object(procscan, "_read_cwd", return_value=("/work", False)), \
             mock.patch.object(procscan, "_read_argv", return_value=["codex"]), \
             mock.patch.object(procscan, "read_environ", return_value=env), \
             mock.patch.object(procscan, "read_proc_start", return_value="start-101"), \
             mock.patch.object(procscan, "exec_child", return_value=None):
            return procscan.scan()[0]

    def test_node_less_owner_preserves_explicit_binding_without_guessing_session_id(self):
        row = self._scan_owner()
        self.assertIsNone(row.session_id)
        self.assertIsNone(row.route_node)
        binding, error = projection._owner_route_binding(row)
        self.assertIsNone(error)
        self.assertEqual(binding, {"route_file": "/route.json", "route_id": "rt-owner",
                                   "route_hash": "sha256:owner"})
        sentinel = object()
        with mock.patch.object(projection, "_owner_route_advance_for_pass", return_value=(None, "none")), \
             mock.patch.object(projection, "_projection_from_record", return_value=sentinel) as resolve:
            result = projection.resolve_work_projection(row, route_records={
                "rt-owner": {"route_id": "rt-owner", "route_hash": "sha256:owner"}})
        self.assertIs(result, sentinel)
        self.assertTrue(resolve.call_args.kwargs["owner"])

    def test_owner_environment_does_not_relax_depth_role_or_partial_binding(self):
        for changes in ({"AGENT_DISPATCH_DEPTH": "2"},
                        {"AGENT_DISPATCH_WORKER_TYPE": "stage"},
                        {"AGENT_OWNER_ROUTE_HASH": ""}):
            with self.subTest(changes=changes):
                binding, error = projection._owner_route_binding(self._scan_owner(**changes))
                self.assertIsNone(binding)
                self.assertEqual(error, "owner-route-binding-invalid")

    def test_all_ps_surfaces_select_current_effective_uid(self):
        outputs = [
            "101 codex 00:01 codex --yolo\n",
            "101 1 61 codex\n",
            "101 pts/2\n",
            "",
        ]

        def run(argv, **_kwargs):
            return SimpleNamespace(stdout=outputs.pop(0))

        with mock.patch.object(procscan.os, "geteuid", return_value=4242), \
             mock.patch.object(procscan.subprocess, "run", side_effect=run) as runner:
            self.assertEqual(len(procscan._ps_lines()), 1)
            self.assertIn(101, procscan.proc_tree())
            self.assertEqual(procscan._pid_ttys(), {101: "pts/2"})
            # The dispatch scanner shares the same central process-table boundary.
            self.assertEqual(dispatch._scan_processes(), [])

        expected_columns = (
            "pid=,comm=,etime=,args=",
            "pid=,ppid=,etimes=,comm=",
            "pid=,tty=",
            "pid=,comm=,etime=,args=",
        )
        self.assertEqual(len(runner.call_args_list), len(expected_columns))
        for call, columns in zip(runner.call_args_list, expected_columns):
            argv = call.args[0]
            self.assertEqual(argv, ["ps", "-u", "4242", "-o", columns])
            self.assertNotIn("-e", argv)
            self.assertNotIn("-eo", argv)

    def test_ps_failures_return_empty_without_global_retry(self):
        with mock.patch.object(procscan.os, "geteuid", return_value=os.geteuid()), \
             mock.patch.object(procscan.subprocess, "run", side_effect=OSError("ps failed")) as runner:
            self.assertEqual(procscan._ps_lines(), [])
            self.assertEqual(procscan.proc_tree(), {})
            self.assertEqual(procscan._pid_ttys(), {})

        self.assertEqual(runner.call_count, 3)
        self.assertTrue(all("-u" in call.args[0] for call in runner.call_args_list))

    def test_missing_effective_uid_fails_closed_before_ps(self):
        with mock.patch.object(procscan.os, "geteuid", side_effect=OSError("no uid")), \
             mock.patch.object(procscan.subprocess, "run") as runner:
            self.assertEqual(procscan._ps_lines(), [])
            self.assertEqual(procscan.proc_tree(), {})
            self.assertEqual(procscan._pid_ttys(), {})
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
