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
        self.assertEqual(sample["cpu"], 14)
        self.assertEqual(sample["start"], "987")


if __name__ == "__main__":
    unittest.main()
