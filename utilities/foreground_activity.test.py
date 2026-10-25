#!/usr/bin/env python3
import importlib.util
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
import subprocess
import tempfile
import sys
import time
import unittest
from types import SimpleNamespace
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
                # The consumer renews the no-progress clock on active or
                # uncertain, so an observed session must be neither.
                baseline = monitor.sample()
                self.assertEqual((baseline.active, baseline.uncertain), (False, False))
                db.execute("UPDATE session_message SET time_updated=2 WHERE session_id='ses_other'")
                db.commit()
                other = monitor.sample()
                self.assertEqual((other.active, other.uncertain), (False, False),
                                 "another session reset the attempt clock")
                db.execute("UPDATE session_v2 SET time_updated=2 WHERE id='ses_own'")
                db.commit()
                metadata = monitor.sample()
                self.assertEqual((metadata.active, metadata.uncertain), (False, False),
                                 "session metadata alone reset the attempt clock")
                db.execute("UPDATE session_message SET time_updated=2 WHERE session_id='ses_own'")
                db.commit()
                own = monitor.sample()
                self.assertEqual((own.active, own.uncertain), (True, False))
                self.assertIn("exact-opencode-session-change", own.reasons)
                db.close()
                context.opencode_db.unlink()
                missing = monitor.sample()
                self.assertFalse(missing.active, "lost access was treated as progress")
                self.assertTrue(missing.uncertain, "lost access could incorrectly expire the no-progress window")
                context.log_path.write_text(context.log_path.read_text() + "attempt-owned progress\n")
                self.assertIn("attempt-file-change", monitor.sample().reasons)

    def test_owner_observation_ignores_supervisor_logs_transcript_roots_and_aggregate_child_cpu(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = root / "attempt.jsonl"
            broad = root / "other-session.jsonl"
            exact = root / "exact-session.jsonl"
            log.write_text("supervisor bookkeeping 1\n")
            broad.write_text("other session 1\n")
            exact.write_text("payload 1\n")
            context = A.ForegroundActivityContext(
                window_seconds=7200, harness="claude", log_path=log,
                metadata={"attempt_id": "owner-1"},
                transcript_roots=(root,), exact_files=(exact,), owner_turn=True,
            )
            monitor = A.ForegroundActivityMonitor(10, "1", context)
            table = {
                (10, "1"): {"self_cpu": 1, "children_cpu": 10},
                (11, "2"): {"self_cpu": 3, "children_cpu": 100},
            }
            members = set(table)
            with mock.patch.object(monitor, "_processes", return_value=table), \
                 mock.patch.object(monitor, "_members", return_value=members), \
                 mock.patch.object(monitor, "_runtime_lineage", return_value={(10, "1")}), \
                 mock.patch.object(monitor, "_lease_active", return_value=False), \
                 mock.patch.object(monitor, "_opencode_session_state", return_value=None):
                baseline = monitor.sample()
                self.assertEqual((baseline.active, baseline.uncertain), (False, False))
                table[(10, "1")] = {"self_cpu": 1, "children_cpu": 99}
                log.write_text("supervisor bookkeeping 2\n")
                broad.write_text("other session 2\n")
                quiet = monitor.sample()
                self.assertEqual((quiet.active, quiet.uncertain), (False, False))
                exact.write_text("payload 2\n")
                changed = monitor.sample()
                self.assertTrue(changed.active)
                self.assertIn("attempt-file-change", changed.reasons)

    def test_owner_process_members_exclude_process_group_siblings_and_attempt_tags(self):
        context = A.ForegroundActivityContext(
            window_seconds=7200, harness="codex", log_path=Path("/attempt.log"),
            metadata={"attempt_id": "owner-1"}, owner_turn=True,
        )
        monitor = A.ForegroundActivityMonitor(10, "1", context)
        table = {
            (10, "1"): {"ppid": 1, "pgid": 10},
            (11, "2"): {"ppid": 1, "pgid": 10},  # same group, not a descendant
            (12, "3"): {"ppid": 10, "pgid": 12},  # exact runtime descendant
        }
        with mock.patch("dispatch_contract.attempt_tagged_descendants",
                        side_effect=AssertionError("owner must not broaden by tag")):
            self.assertEqual(monitor._members(table), {(10, "1"), (12, "3")})

    def test_owner_pid_reuse_is_uncertain_and_never_becomes_activity(self):
        context = A.ForegroundActivityContext(
            window_seconds=7200, harness="codex", log_path=Path("/attempt.log"),
            metadata={"attempt_id": "owner-1"}, owner_turn=True,
        )
        monitor = A.ForegroundActivityMonitor(10, "birth-1", context)
        table = {(10, "birth-2"): {"ppid": 1, "pgid": 10}}
        with self.assertRaisesRegex(RuntimeError, "identity changed or disappeared"):
            monitor._members(table)

    def test_owner_lease_requires_exact_process_route_and_live_deadline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jobs = root / "jobs.log"
            jobs.touch()
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            try:
                proc = Path("/proc") / str(process.pid)
                stat = A._stat(proc / "stat")
                argv = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
                record = {
                    "attempt_id": "owner-1", "schema_version": 1,
                    "route_id": "route-1", "route_node": "owner",
                    "pid": process.pid, "pid_start": stat["start"],
                    "pgid": process.pid, "deadline": time.time() + 30,
                    "command_digest": hashlib.sha256(b"\0".join(argv)).hexdigest(),
                }
                lease = root / "verification-leases" / "owner-1.json"
                lease.parent.mkdir()
                lease.write_text(json.dumps(record), encoding="utf-8")
                context = A.ForegroundActivityContext(
                    window_seconds=7200, harness="codex", log_path=root / "attempt.log",
                    metadata={"attempt_id": "owner-1", "route_id": "route-1",
                              "route_node": "owner"}, jobs=jobs, owner_turn=True,
                )
                monitor = A.ForegroundActivityMonitor(process.pid, stat["start"], context)
                self.assertTrue(monitor._lease_active())
                lease.write_text(json.dumps({**record, "pid_start": "reused"}), encoding="utf-8")
                self.assertFalse(monitor._lease_active())
                lease.write_text(json.dumps({**record, "route_id": "route-other"}), encoding="utf-8")
                self.assertFalse(monitor._lease_active())
                lease.write_text(json.dumps({**record, "deadline": time.time() - 1}), encoding="utf-8")
                self.assertFalse(monitor._lease_active())
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_owner_turn_factory_requires_the_exact_registered_owner_binding(self):
        args = SimpleNamespace(parent_attempt_id="owner-1", jobs="/tmp/jobs.log",
                               route_id="route-1", route_hash="sha256:route-1",
                               worktree="/tmp/work")
        process = SimpleNamespace(pid=4242)
        row = SimpleNamespace(
            attempt_id="owner-1", status="running",
            metadata={"worker_type": "owner", "route_id": "route-1",
                      "route_hash": "sha256:route-1", "route_node": "owner",
                      "log_file": "/tmp/owner.log"},
        )
        monitor = mock.Mock()
        with mock.patch("dispatch_completion_join.exact_attempt_row", return_value=row), \
             mock.patch.object(A, "_stat", return_value={"start": "77"}), \
             mock.patch.object(A, "ForegroundActivityMonitor", return_value=monitor) as create:
            activity = A.owner_turn_activity(args, process, harness="codex", window_seconds=20)
        context = create.call_args.args[2]
        self.assertTrue(context.owner_turn)
        self.assertEqual(context.metadata["runtime_pid"], "4242")
        self.assertEqual(context.metadata["runtime_start"], "77")
        self.assertEqual(context.metadata["route_node"], "owner")
        activity.expired(now=0)
        monitor.sample.assert_called_once()

        row.metadata["worker_type"] = "stage"
        with mock.patch("dispatch_completion_join.exact_attempt_row", return_value=row), \
             mock.patch.object(A, "ForegroundActivityMonitor",
                               side_effect=AssertionError("mismatched attempt must not be sampled")):
            uncertain = A.owner_turn_activity(args, process, harness="codex", window_seconds=20)
        self.assertFalse(uncertain.expired(now=0))


if __name__ == "__main__":
    unittest.main()
