#!/usr/bin/env python3
import importlib.util
import json
import math
import sqlite3
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

    def opencode_context(self, root, *, launch_metadata=None, environment=None):
        return A.build_activity_context(
            worker_type="stage", harness="opencode", log_path=root / "attempt.jsonl",
            jobs=root / "jobs.log", launch_metadata=launch_metadata or {}, attempt_id="a",
            route_id="route", route_node="test", environment=environment or {"XDG_DATA_HOME": str(root)},
            worktree=root,
        )

    def test_optional_opencode_runtime_keeps_context_without_warning(self):
        from contextlib import redirect_stderr
        from io import StringIO
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            error = StringIO()
            with redirect_stderr(error):
                shared = self.opencode_context(root)
            self.assertEqual(error.getvalue(), "")
            self.assertEqual(shared.opencode_db, root / "opencode/opencode.db")
            self.assertEqual(shared.transcript_roots, ())
            self.assertEqual(shared.metadata["attempt_id"], "a")
            self.assertIsNone(A.ForegroundActivityMonitor(10, "1", shared)._opencode_session_state())
            private = self.opencode_context(root, launch_metadata={"opencode_runtime_dir": str(root / "private")})
            self.assertEqual(private.transcript_roots, (
                root / "private/data/opencode/opencode.db", root / "private/data/opencode/opencode.db-wal"))
            self.assertIsNone(private.opencode_db)
            overridden = self.opencode_context(root, environment={
                "OPENCODE_DATA_DIR": str(root / "shared"), "OPENCODE_DB": "custom.db"})
            self.assertEqual(overridden.opencode_db, root / "shared/custom.db")

    def test_shared_database_activity_is_exact_session_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context = self.opencode_context(root)
            context.log_path.write_text(json.dumps({"type": "step_start", "sessionID": "ses_own"}) + "\n")
            context.opencode_db.parent.mkdir()
            db = sqlite3.connect(context.opencode_db)
            self.addCleanup(db.close)
            db.executescript("""
                CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, time_updated INTEGER);
                CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER,
                    time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            for sid in ("ses_own", "ses_other"):
                db.execute("INSERT INTO session_v2 VALUES (?, ?, 1)", (sid, str(root)))
                db.execute("INSERT INTO session_message VALUES (?, 'assistant', 1, 1, 1, ?)",
                           (sid, json.dumps({"content": [{"type": "text", "text": "initial"}]})))
            db.commit()
            monitor = A.ForegroundActivityMonitor(10, "1", context)
            table = {(10, "1"): {"self_cpu": 0, "children_cpu": 0}}
            with mock.patch.object(monitor, "_processes", return_value=table), \
                 mock.patch.object(monitor, "_members", return_value=set(table)), \
                 mock.patch.object(monitor, "_runtime_lineage", return_value=set(table)), \
                 mock.patch.object(monitor, "_lease_active", return_value=False):
                self.assertFalse(monitor.sample().active)
                db.execute("UPDATE session_message SET time_updated=2 WHERE session_id='ses_other'")
                db.commit()
                self.assertFalse(monitor.sample().active, "another session reset the attempt clock")
                db.execute("UPDATE session_v2 SET time_updated=2 WHERE id='ses_own'")
                db.commit()
                self.assertFalse(monitor.sample().active, "session metadata alone counted as model work")
                db.execute("UPDATE session_message SET time_updated=2 WHERE session_id='ses_own'")
                db.commit()
                own = monitor.sample()
                self.assertTrue(own.active)
                self.assertIn("exact-opencode-session-change", own.reasons)
                db.close()
                context.opencode_db.unlink()
                self.assertFalse(monitor.sample().active, "lost access was treated as progress")
                context.log_path.write_text(context.log_path.read_text() + "attempt-owned progress\n")
                self.assertIn("attempt-file-change", monitor.sample().reasons)


if __name__ == "__main__":
    unittest.main()
