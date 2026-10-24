#!/usr/bin/env python3
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet.collectors import dispatch, procscan  # noqa: E402


class CurrentUserProcessScopeTest(unittest.TestCase):
    def test_all_ps_surfaces_select_current_effective_uid(self):
        outputs = [
            "101 codex 00:01 codex --yolo\n",
            "101 1 61 codex\n",
            "101 pts/2\n",
            "",
        ]

        def run(argv, **_kwargs):
            return SimpleNamespace(stdout=outputs.pop(0), returncode=0)

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
            lines = procscan._ps_lines()
            self.assertEqual(lines, [])
            self.assertFalse(lines.census_complete)
            self.assertIn("ps-error-OSError", lines.census_reasons)
            self.assertEqual(procscan.proc_tree(), {})
            self.assertEqual(procscan._pid_ttys(), {})

        self.assertEqual(runner.call_count, 3)
        self.assertTrue(all("-u" in call.args[0] for call in runner.call_args_list))

    def test_missing_effective_uid_fails_closed_before_ps(self):
        with mock.patch.object(procscan.os, "geteuid", side_effect=OSError("no uid")), \
             mock.patch.object(procscan.subprocess, "run") as runner:
            lines = procscan._ps_lines()
            self.assertEqual(lines, [])
            self.assertFalse(lines.census_complete)
            self.assertIn("effective-uid-unavailable", lines.census_reasons)
            self.assertEqual(procscan.proc_tree(), {})
            self.assertEqual(procscan._pid_ttys(), {})
        runner.assert_not_called()

    def test_nonzero_ps_preserves_rows_but_marks_census_incomplete(self):
        with mock.patch.object(procscan.os, "geteuid", return_value=4242), \
             mock.patch.object(procscan.subprocess, "run", return_value=SimpleNamespace(
                 stdout="101 codex 00:01 codex --yolo\n", returncode=2)):
            lines = procscan._ps_lines()

        self.assertEqual(lines, ["101 codex 00:01 codex --yolo"])
        self.assertFalse(lines.census_complete)
        self.assertIn("ps-nonzero-exit", lines.census_reasons)

    def test_ps_timeout_preserves_partial_rows_but_marks_census_incomplete(self):
        timeout = subprocess.TimeoutExpired(
            ["ps"], 5, output=b"101 codex 00:01 codex --yolo\n")
        with mock.patch.object(procscan.os, "geteuid", return_value=4242), \
             mock.patch.object(procscan.subprocess, "run", side_effect=timeout):
            lines = procscan._ps_lines()

        self.assertEqual(lines, ["101 codex 00:01 codex --yolo"])
        self.assertFalse(lines.census_complete)
        self.assertIn("ps-timeout", lines.census_reasons)

    def test_missing_ps_exit_status_is_not_a_complete_empty_census(self):
        with mock.patch.object(procscan.os, "geteuid", return_value=4242), \
             mock.patch.object(procscan.subprocess, "run",
                               return_value=SimpleNamespace(stdout="")):
            lines = procscan._ps_lines()

        self.assertEqual(lines, [])
        self.assertFalse(lines.census_complete)
        self.assertIn("ps-returncode-unavailable", lines.census_reasons)

    def test_scan_distinguishes_complete_empty_from_malformed_process_output(self):
        with mock.patch.object(procscan, "_ps_lines",
                               return_value=procscan._CensusRows(complete=True)), \
             mock.patch.object(procscan, "_pid_ttys", return_value={}), \
             mock.patch.object(procscan, "_detached_ttys", return_value={}), \
             mock.patch.object(procscan, "proc_tree", return_value={}), \
             mock.patch.object(procscan, "children_index", return_value={}):
            empty = procscan.scan()
        self.assertEqual(empty, [])
        self.assertTrue(empty.census_complete)

        with mock.patch.object(procscan, "_ps_lines", return_value=procscan._CensusRows(
                ["partial malformed row"], complete=True)), \
             mock.patch.object(procscan, "_pid_ttys", return_value={}), \
             mock.patch.object(procscan, "_detached_ttys", return_value={}), \
             mock.patch.object(procscan, "proc_tree", return_value={}), \
             mock.patch.object(procscan, "children_index", return_value={}):
            partial = procscan.scan()
        self.assertEqual(partial, [])
        self.assertFalse(partial.census_complete)
        self.assertIn("process-row-pid-invalid", partial.census_reasons)

        with mock.patch.object(procscan, "_ps_lines", return_value=procscan._CensusRows(
                ["partial malformed"], complete=True)), \
             mock.patch.object(procscan, "_pid_ttys", return_value={}), \
             mock.patch.object(procscan, "_detached_ttys", return_value={}), \
             mock.patch.object(procscan, "proc_tree", return_value={}), \
             mock.patch.object(procscan, "children_index", return_value={}):
            malformed = procscan.scan()
        self.assertFalse(malformed.census_complete)
        self.assertIn("process-row-malformed", malformed.census_reasons)

        with mock.patch.object(procscan, "_ps_lines", return_value=procscan._CensusRows(
                ["101 00:01 claude"], complete=True)), \
             mock.patch.object(procscan, "_pid_ttys", return_value={}), \
             mock.patch.object(procscan, "_detached_ttys", return_value={}), \
             mock.patch.object(procscan, "proc_tree", return_value={}), \
             mock.patch.object(procscan, "children_index", return_value={}):
            shifted = procscan.scan()
        self.assertFalse(shifted.census_complete)
        self.assertIn("process-row-malformed", shifted.census_reasons)

    def test_nonzero_ps_rows_remain_visible_while_their_census_stays_incomplete(self):
        raw = procscan._CensusRows(
            ["101 claude 00:01 claude"], complete=False,
            reasons=("ps-nonzero-exit",))
        with mock.patch.object(procscan, "_ps_lines", return_value=raw), \
             mock.patch.object(procscan, "_pid_ttys", return_value={}), \
             mock.patch.object(procscan, "_detached_ttys", return_value={}), \
             mock.patch.object(procscan, "proc_tree", return_value={}), \
             mock.patch.object(procscan, "children_index", return_value={}), \
             mock.patch.object(procscan, "is_terminal_state", return_value=False), \
             mock.patch.object(procscan, "_read_cwd", return_value=("/fixture", False)), \
             mock.patch.object(procscan, "read_environ", return_value={}), \
             mock.patch.object(procscan, "read_proc_start", return_value=None), \
             mock.patch.object(procscan, "_read_argv", return_value=["claude"]), \
             mock.patch.object(procscan, "_orca_dead_socks", return_value=set()):
            sessions = procscan.scan()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].harness, "claude")
        self.assertFalse(sessions.census_complete)
        self.assertIn("ps-nonzero-exit", sessions.census_reasons)


if __name__ == "__main__":
    unittest.main()
