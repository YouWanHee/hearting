#!/usr/bin/env python3
import importlib.util
import math
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

P = Path(__file__).with_name("foreground_activity.py")
S = importlib.util.spec_from_file_location("foreground_activity", P)
A = importlib.util.module_from_spec(S)
sys.modules[S.name] = A
S.loader.exec_module(A)


class ForegroundActivityTests(unittest.TestCase):
    def test_owner_is_exempt_before_defaults_are_read(self):
        with mock.patch.object(A.importlib.util, "spec_from_file_location", side_effect=AssertionError):
            policy = A.resolve_activity_policy("owner")
        self.assertIsNone(policy.window_seconds)
        self.assertEqual(policy.source, "owner-exempt")

    def test_policy_reads_shipped_defaults_and_context_is_optional_at_zero(self):
        policy = A.resolve_activity_policy("stage")
        self.assertEqual(policy.window_seconds, 1800)
        disabled = A.ActivityPolicy(None, "dispatch-defaults")
        self.assertIsNone(A.make_activity_context(
            disabled, worker_type="stage", harness="codex", log_path=Path("/tmp/a"),
            jobs=Path("/tmp/jobs.log"), metadata={}, runtime_executable="codex",
        ))

    def test_proc_stat_parser_uses_last_closing_parenthesis(self):
        from foreground_activity import _stat
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "stat"
            tail = ["S", "12", "10"] + ["0"] * 8 + ["2", "3", "4", "5"] + ["0"] * 4 + ["987"]
            p.write_text("42 (name ) with spaces) " + " ".join(tail) + "\n", encoding="utf-8")
            sample = _stat(p)
        self.assertEqual(sample["ppid"], 12)
        self.assertEqual(sample["pgid"], 10)
        self.assertEqual(sample["self_cpu"], 5)
        self.assertEqual(sample["children_cpu"], 9)
        self.assertEqual(sample["start"], "987")

    def test_context_helper_skips_owner_and_disabled_policy_inputs(self):
        arguments = dict(
            worker_type="owner", harness="codex", log_path=Path("/no/log"),
            jobs=Path("/no/jobs"), launch_metadata={}, attempt_id="a",
            route_id=None, route_node=None, environment={}, worktree=Path("/work"),
        )
        with mock.patch.object(A, "resolve_activity_policy", side_effect=AssertionError):
            self.assertIsNone(A.build_activity_context(**arguments))

        arguments["worker_type"] = "stage"
        with mock.patch.object(A, "resolve_activity_policy", return_value=A.ActivityPolicy(None, "disabled")):
            self.assertIsNone(A.build_activity_context(**arguments))

    def test_context_helper_builds_for_each_non_owner_worker_type(self):
        for worker_type in ("stage", "review", "support", "frame"):
            context = A.build_activity_context(
                worker_type=worker_type, harness="codex", log_path=Path("/tmp/log"),
                jobs=Path("/tmp/jobs"), launch_metadata={}, attempt_id="a",
                route_id="route", route_node="execute", environment={"CODEX_HOME": "/tmp/codex"},
                worktree=Path("/tmp/work"),
            )
            self.assertIsNotNone(context, worker_type)
            self.assertEqual(context.harness, "codex")
            self.assertEqual(context.transcript_roots, (Path("/tmp/codex/sessions"),))

    def test_context_failure_warns_once_and_returns_none(self):
        from contextlib import redirect_stderr
        from io import StringIO
        error = StringIO()
        with redirect_stderr(error):
            context = A.build_activity_context(
                worker_type="stage", harness="codex", log_path=Path("/tmp/log"),
                jobs=Path("/tmp/jobs"), launch_metadata={}, attempt_id="a",
                route_id=None, route_node=None, environment={}, worktree=Path("/tmp/work"),
            )
        self.assertIsNone(context)
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        self.assertTrue(error.getvalue().startswith("warning="))


if __name__ == "__main__":
    unittest.main()
