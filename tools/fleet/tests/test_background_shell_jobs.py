"""Focused regressions for exact native background shell task projections."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fleet import render
from fleet.collectors import opencode, procscan, shell_jobs
from fleet.model import DispatchJob, Session


class BackgroundShellJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_jsonl(self, path, rows, mode="w"):
        with open(path, mode, encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, separators=(",", ":")) + "\n")

    @staticmethod
    def claude_call(call_id="call-1", session_id="sid-a"):
        return {
            "type": "assistant", "session_id": session_id,
            "timestamp": "2026-10-20T00:00:00Z",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "id": call_id,
                                        "input": {"command": "echo ok; TOKEN=hidden",
                                                  "run_in_background": True}}]},
        }

    def claude_result(self, call_id="call-1", session_id="sid-a", task_id="task-123"):
        output = self.root / "tasks" / (task_id + ".output")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.touch(exist_ok=True)
        return ({
            "type": "user", "session_id": session_id,
            "timestamp": "2026-10-20T00:00:01Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": call_id,
                                        "content": [{"type": "text", "text":
                                            "Command running in background. Output: " + str(output)}]}]},
        }, output)

    def test_claude_exact_ack_command_redaction_and_bad_attribution(self):
        path = self.root / "claude.jsonl"
        result, output = self.claude_result()
        self.write_jsonl(path, [self.claude_call(), result])

        jobs = shell_jobs.read_claude_jobs(path, session_id="sid-a", now=1792454405)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["native_task_id"], "task-123")
        self.assertEqual(job["native_status"], "acknowledged")
        self.assertEqual(job["process_state"], "unknown")
        self.assertEqual(job["output_path"], str(output))
        self.assertIn("[redacted]", job["command"])
        self.assertNotIn("hidden", job["command"])

        other = self.root / "other.jsonl"
        wrong_call = dict(self.claude_call(session_id="sid-other"))
        wrong_result, _ = self.claude_result(session_id="sid-other", task_id="task-other")
        self.write_jsonl(other, [wrong_call, wrong_result])
        self.assertEqual(shell_jobs.read_claude_jobs(other, session_id="sid-a"), [])

    def test_codex_nested_handles_multiple_jobs_exit_and_json_render(self):
        path = self.root / "rollout.jsonl"
        self.write_jsonl(path, [
            {"timestamp": "2026-10-20T00:00:00Z", "payload": {
                "type": "custom_tool_call", "call_id": "outer", "name": "functions.exec",
                "input": "await tools.exec_command({cmd: 'private source is not parsed'})"}},
            {"timestamp": "2026-10-20T00:00:05Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "outer", "output": [
                    {"type": "input_text", "text": json.dumps({"session_id": 101, "output": ""})},
                    {"type": "input_text", "text": json.dumps({"session_id": 202, "output": ""})},
                ]}},
        ])
        jobs = shell_jobs.read_codex_jobs(path, session_id="thread-a", now=1792454405)
        self.assertEqual({item["continuation_handle"] for item in jobs}, {"101", "202"})
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(item["command"] is None for item in jobs))
        self.assertTrue(all(item["process_state"] == "unknown" for item in jobs))
        self.assertTrue(all(item["lifecycle_state"] == "running" for item in jobs))
        self.assertTrue(all(item["elapsed_s"] == 5 for item in jobs))

        self.write_jsonl(path, [
            {"timestamp": "2026-10-20T00:00:06Z", "payload": {
                "type": "custom_tool_call", "call_id": "status-poll", "name": "functions.exec",
                "input": "await tools.write_stdin({session_id: 101})"}},
            {"timestamp": "2026-10-20T00:00:07Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "status-poll", "output": [
                    {"type": "input_text", "text": json.dumps(
                        {"session_id": 101, "output": "", "status": "completed"})}]}},
        ], mode="a")
        status_only = shell_jobs.read_codex_jobs(path, session_id="thread-a", now=1792454407)
        self.assertEqual({item["continuation_handle"]: item["lifecycle_state"]
                          for item in status_only}["101"], "running")

        self.write_jsonl(path, [
            {"timestamp": "2026-10-20T00:00:06Z", "payload": {
                "type": "custom_tool_call", "call_id": "poll", "name": "functions.exec",
                "input": "await tools.write_stdin({session_id: 101})"}},
            {"timestamp": "2026-10-20T00:00:07Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "poll", "output": [
                    {"type": "input_text", "text": json.dumps(
                        {"session_id": 101, "output": "", "exit_code": 2})}]}},
        ], mode="a")
        jobs = shell_jobs.read_codex_jobs(path, session_id="thread-a", now=1792454407)
        by_handle = {item["continuation_handle"]: item for item in jobs}
        self.assertEqual(by_handle["101"]["lifecycle_state"], "completed")
        self.assertEqual(by_handle["101"]["exit_code"], 2)
        self.assertEqual(by_handle["202"]["lifecycle_state"], "running")

        session = Session(harness="codex", pid=10, session_id="thread-a",
                          liveness="idle", shell_jobs=jobs)
        public = session.to_dict()
        self.assertEqual(public["liveness"], "idle")
        self.assertEqual(len(public["shell_jobs"]), 2)
        with mock.patch("os.stat", side_effect=AssertionError("render attempted observation I/O")):
            by_width = {width: render._shell_job_rows(session, term_width=width)
                        for width in (60, 100, 120, 168)}
        for width, rows in by_width.items():
            for line in rows:
                self.assertLessEqual(render._dw("".join(text for text, _key in line)), width)
        rendered = "".join(text for line in by_width[120] for text, _key in line)
        self.assertIn("native exit 2", rendered)
        self.assertIn("proc ?", rendered)

        worker = DispatchJob(key="autopilot-code", slug="worker-shell-jobs",
                             harness="codex", liveness="working", shell_jobs=jobs)
        group_lines = render._build_lines([], [worker], "dispatch", False, 0,
                                          term_width=120)
        group_screen = "\n".join(render._plain(line) for line in group_lines if line)
        self.assertIn("worker-shell-jobs", group_screen)
        self.assertIn("native exit 2", group_screen)
        process_lines, _meta = render._degrade_card(worker, {}, term_width=120)
        process_screen = "\n".join(render._plain(line) for line in process_lines if line)
        self.assertIn("native exit 2", process_screen)

    def test_bounded_catchup_and_same_size_source_rewrite_invalidation(self):
        path = self.root / "large.jsonl"
        with open(path, "wb") as stream:
            stream.write((b'{"type":"other"}\n' * 18000))
            stream.write((json.dumps(self.claude_call()).encode() + b"\n"))
            result, _output = self.claude_result()
            stream.write((json.dumps(result).encode() + b"\n"))
        first = shell_jobs.read_claude_jobs(path, session_id="sid-a")
        self.assertEqual(first, [])
        source_key = ("claude", os.path.abspath(path), "sid-a", None)
        self.assertEqual(shell_jobs.source_status(path, source_key)["state"], "catching-up")
        second = []
        for _ in range(8):
            second = shell_jobs.read_claude_jobs(path, session_id="sid-a")
            if shell_jobs.source_status(path, source_key)["state"] == "observed":
                break
        self.assertEqual(len(second), 1)
        self.assertEqual(shell_jobs.source_status(path, source_key)["state"], "observed")

        small = self.root / "rewrite.jsonl"
        raw_a = b'{"type":"other","key":"a"}\n'
        raw_b = b'{"type":"other","key":"b"}\n'
        self.assertEqual(len(raw_a), len(raw_b))
        small.write_bytes(raw_a)
        before = shell_jobs.jsonl_delta(small, "rewrite-source")
        small.write_bytes(raw_b)
        after = shell_jobs.jsonl_delta(small, "rewrite-source")
        self.assertTrue(after["reset"])
        self.assertNotEqual(before["generation"], after["generation"])

    def test_quiet_boundaries_blank_output_rotation_and_completion(self):
        path = self.root / "output.log"
        path.write_text("first\n", encoding="utf-8")
        base = path.stat().st_mtime + 1
        job = {"harness": "claude", "session_id": "sid-q", "native_id": "task-q",
               "native_status": "acknowledged", "process_state": "alive",
               "started_at": base - 5,
               "output_path": str(path)}
        first = shell_jobs.normalize_job(job, now=base)
        self.assertFalse(first["quiet_suspected"])
        self.assertEqual(first["output_coverage"], "sampled")
        before = shell_jobs.normalize_job(job, now=base + 599)
        self.assertFalse(before["quiet_suspected"])
        at_boundary = shell_jobs.normalize_job(job, now=base + 600)
        self.assertTrue(at_boundary["quiet_suspected"])
        self.assertTrue(shell_jobs.normalize_job(job, now=base + 601)["quiet_suspected"])

        with path.open("a", encoding="utf-8") as stream:
            stream.write("second\n")
        os.utime(path, (base + 629, base + 629))
        growth = shell_jobs.normalize_job(job, now=base + 630)
        self.assertEqual(growth["output_coverage"], "continuous-file")
        self.assertEqual(growth["last_output_at"], base + 629)
        self.assertFalse(growth["quiet_suspected"])

        path.write_text("", encoding="utf-8")
        truncated = shell_jobs.normalize_job(job, now=base + 601)
        self.assertIsNone(truncated["last_output_at"])
        self.assertFalse(truncated["quiet_suspected"])
        os.replace(path, self.root / "rotated.log")
        job["output_path"] = str(self.root / "rotated.log")
        rotated = shell_jobs.normalize_job(job, now=base + 602)
        self.assertEqual(rotated["output_coverage"], "sampled")

        done = dict(job, native_id="task-done", native_status="completed", exit_code=0,
                    completed_at=base + 1)
        shell_jobs.normalize_job(done, now=base + 2)
        completed_later = shell_jobs.normalize_job(done, now=base + 900)
        self.assertFalse(completed_later["quiet_suspected"])

        future = shell_jobs.normalize_job({"harness": "codex", "session_id": "s",
                                           "native_id": "future", "started_at": base + 100},
                                          now=base)
        self.assertIsNone(future["elapsed_s"])
        self.assertEqual(future["time_reason"], "future-start")
        future_output = self.root / "future-output"
        future_output.write_text("x", encoding="utf-8")
        os.utime(future_output, (base + 10, base + 10))
        invalid_output = shell_jobs.normalize_job(
            {"harness": "claude", "session_id": "sid-q", "native_id": "future-output",
             "output_path": str(future_output)}, now=base)
        self.assertEqual(invalid_output["output_time_state"], "future")
        self.assertIsNone(invalid_output["last_output_at"])
        missing_output = shell_jobs.normalize_job(
            {"harness": "claude", "session_id": "sid-q", "native_id": "missing-output",
             "output_path": str(self.root / "missing")}, now=base)
        self.assertEqual(missing_output["output_coverage"], "unavailable")
        self.assertEqual(missing_output["output_reason"], "FileNotFoundError")

    def test_pid_reuse_namespace_and_exact_output_writer(self):
        path = self.root / "writer.log"
        with open(path, "wb") as writer:
            writer.write(b"running")
            writer.flush()
            pid = os.getpid()
            start = shell_jobs._proc_start(pid)
            namespace = shell_jobs._pid_namespace(pid)
            candidate = {"pid": pid, "root_pid": pid, "proc_start": start,
                         "ownership_verified": True,
                         "ancestry": [{"pid": pid, "ppid": os.getppid(), "start": start}]}
            exact = {"harness": "claude", "session_id": "sid-p", "native_id": "task-p",
                     "source_generation": "1", "output_path": str(path)}
            live = shell_jobs.correlate_output_writers(
                exact, [candidate], namespace=namespace, root_pid=pid)
            self.assertEqual(live["process_state"], "alive")

            reuse = dict(exact, native_id="task-reuse")
            wrong_generation = dict(candidate, proc_start="0")
            self.assertEqual(shell_jobs.correlate_output_writers(
                reuse, [wrong_generation], namespace=namespace, root_pid=pid)
                ["process_state"], "unknown")
            self.assertEqual(shell_jobs.correlate_output_writers(
                dict(exact, native_id="task-ns"), [candidate], namespace="pid:[different]",
                root_pid=pid)
                ["process_state"], "unknown")

            key = shell_jobs._job_key(reuse)
            shell_jobs._VERIFIED_PROCESSES[key] = {
                "pid": pid, "start": "0", "namespace": namespace,
                "ancestry": candidate["ancestry"],
            }
            dead_generation = shell_jobs.correlate_output_writers(
                reuse, [], namespace=namespace)
            self.assertEqual(dead_generation["process_state"], "dead")
        static_file = shell_jobs.correlate_output_writers(
            exact, [], namespace=namespace, root_pid=pid)
        self.assertEqual(static_file["process_state"], "unknown")

    def test_procscan_enumerates_multiple_verified_branches(self):
        tree = {1: (0, 100, "claude"), 2: (1, 5, "bash"),
                3: (2, 3, "python"), 4: (1, 2, "node")}
        kids = {1: [2, 4], 2: [3], 3: [], 4: []}
        identities = {1: (0, "1"), 2: (1, "2"), 3: (2, "3"), 4: (1, "4")}
        with mock.patch.object(procscan, "_exec_identity", side_effect=identities.get), \
                mock.patch.object(procscan, "_exec_is_helper", return_value=False), \
                mock.patch.object(procscan, "_exec_is_wrapper", side_effect=lambda _pid, comm: comm == "bash"):
            found = procscan.exec_children(1, tree=tree, kids=kids, min_age=0,
                                           expected_start="1")
        self.assertEqual({item["pid"] for item in found}, {3, 4})
        self.assertTrue(all(item["ownership_verified"] for item in found))

    def test_opencode_v2_documented_background_input_and_mutable_row(self):
        path = self.root / "opencode.db"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE session_v2 (
                id TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER,
                title TEXT, slug TEXT, agent TEXT, model TEXT, cost REAL,
                tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
                parent_id TEXT
            );
            CREATE TABLE session_message (
                session_id TEXT, type TEXT, seq INTEGER, data TEXT,
                time_created INTEGER, time_updated INTEGER
            );
        """)
        sid = "ses_backgroundfixture123"
        connection.execute(
            "INSERT INTO session_v2(id,directory,time_created,time_updated,model) VALUES(?,?,?,?,?)",
            (sid, str(self.root), 1000, 1000, "{}"))

        def message(status, updated):
            return {"content": [{"type": "tool", "id": "part-shell-1", "name": "shell",
                                 "state": {"status": status, "input": {
                                     "command": "sleep 20", "background": True}},
                                 "time": {"created": 1000, "ran": 1000,
                                          "completed": 1001 if status == "completed" else None}}]}

        connection.execute(
            "INSERT INTO session_message VALUES(?,?,?,?,?,?)",
            (sid, "assistant", 1, json.dumps(message("completed", 1001)), 1000, 1001))
        connection.commit()
        connection.close()

        observed = opencode._opencode_v2_runtime().read_session_observation(
            sid, db_path=path, generation="v2", shell_after_seq=0)
        self.assertEqual(observed["generation"], "v2")
        self.assertEqual(len(observed["shell_tasks"]), 1)
        session = SimpleNamespace(pid=None, proc_start=None, attempt_id=None)
        opencode._attach_v2_shell_tasks(session, observed, path, sid)
        self.assertEqual(session.shell_jobs[0]["native_status"], "acknowledged")
        self.assertEqual(session.shell_jobs[0]["process_state"], "unknown")

        connection = sqlite3.connect(path)
        connection.execute("UPDATE session_message SET data=?,time_updated=? WHERE session_id=? AND seq=1",
                           (json.dumps(message("running", 2000)), 2000, sid))
        connection.commit()
        connection.close()
        updated = opencode._opencode_v2_runtime().read_session_observation(
            sid, db_path=path, generation="v2", shell_after_seq=1)
        opencode._attach_v2_shell_tasks(session, updated, path, sid)
        self.assertEqual(len(session.shell_jobs), 1)
        self.assertEqual(session.shell_jobs[0]["invocation_status"], "running")
        self.assertEqual(session.shell_jobs[0]["native_status"], "acknowledged")

        connection = sqlite3.connect(path)
        connection.execute("INSERT INTO session_message VALUES(?,?,?,?,?,?)",
                           (sid, "assistant", 2, "{invalid-json", 2000, 2001))
        connection.commit()
        connection.close()
        failed = opencode._opencode_v2_runtime().read_session_observation(
            sid, db_path=path, generation="v2", shell_after_seq=1)
        self.assertEqual(failed["shell_cursor"]["state"], "partial")
        opencode._attach_v2_shell_tasks(session, failed, path, sid)
        self.assertEqual(session.shell_job_observation["state"], "partial")
        self.assertEqual(session.shell_job_observation["coverage"], "partial")

    def test_opencode_v1_does_not_inherit_v2_background_semantics(self):
        path = self.root / "opencode-v1.db"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE session (
                id TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER,
                title TEXT, slug TEXT, agent TEXT, model TEXT, cost REAL,
                tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
                parent_id TEXT
            );
            CREATE TABLE message (
                id TEXT, session_id TEXT, role TEXT, time_created INTEGER,
                time_updated INTEGER, data TEXT
            );
            CREATE TABLE part (session_id TEXT, message_id TEXT, data TEXT);
        """)
        sid = "ses_v1fixture123"
        connection.execute("INSERT INTO session(id,directory,time_created,time_updated) VALUES(?,?,?,?)",
                           (sid, str(self.root), 1000, 1000))
        connection.execute("INSERT INTO message VALUES(?,?,?,?,?,?)",
                           ("m1", sid, "assistant", 1000, 1000, "{}"))
        connection.execute("INSERT INTO part VALUES(?,?,?)",
                           (sid, "m1", json.dumps({"type": "tool", "tool": "bash",
                                                   "state": {"status": "running"}})))
        connection.commit()
        connection.close()
        observed = opencode._opencode_v2_runtime().read_session_observation(
            sid, db_path=path, generation="v1")
        self.assertEqual(observed["generation"], "v1")
        self.assertEqual(observed["shell_tasks"], [])
        self.assertEqual(observed["shell_cursor"]["state"], "unobserved")


if __name__ == "__main__":
    unittest.main()
