from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import fcntl
import json
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock
from types import SimpleNamespace

import human_wait_state as state
import human_gate_observation as observation


class HumanWaitStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = mock.patch.dict(os.environ, {"PATH": str(Path(self.tmp.name) / "empty-bin")})
        self.path.start()
        self.addCleanup(self.path.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_only_explicit_first_line_markers_open_private_signal(self):
        self.assertIsNone(state.marked_question("Would you like me to proceed?"))
        self.assertIsNone(state.marked_question("문장 [답변 필요] 중간 표지"))
        self.assertEqual(state.marked_question("[답변 필요] 진행할까요?\n부연"), "진행할까요?")
        self.assertEqual(state.marked_question("[결정 필요]\x1b[31m배포 범위\x1b[0m"), "배포 범위")
        self.assertEqual(state.marked_question("긴 작업 보고\n변경 사항과 결과\n[답변 필요] 마지막 선택"), "마지막 선택")
        self.assertIsNone(state.marked_question("예시:\n```text\n[답변 필요] 코드 예시\n```"))
        self.assertEqual(state.marked_question("~~~md\n[결정 필요] 코드 예시\n~~~\n[결정 필요] 실제 선택"),
                         "실제 선택")
        self.assertTrue(state.open_signal("claude", "sid-1", "seat-1",
                                          "[답변 필요] 진행할까요?", sequence=3, now=100))
        path = next(Path(self.tmp.name).rglob("*.json"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertTrue(state.open_signal("claude", "sid-1", "seat-1",
                                          "[답변 필요] 진행할까요?", sequence=3, now=200))
        self.assertEqual(state.read_signals(now=200)[0]["since"], 100)

    def test_reader_is_read_only_and_user_prompt_closes_exact_seat(self):
        self.assertTrue(state.open_signal("codex", "sid-2", "seat-2",
                                          "[결정 필요] A 또는 B", sequence=2, now=100))
        path = next(Path(self.tmp.name).rglob("*.json"))
        before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        result = observation.read_answer_waits(jobs_paths=(), now=101)
        after = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        self.assertEqual(before, after)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["question"], "A 또는 B")
        self.assertTrue(state.close_signal("seat-2", sequence=3))
        self.assertEqual(observation.read_answer_waits(jobs_paths=(), now=102)["total"], 0)

    def test_stale_signal_remains_source_state_and_is_not_deleted_by_reader(self):
        self.assertTrue(state.open_signal("opencode", "sid-3", "seat-3",
                                          "[답변 필요] 확인", sequence=0, now=10))
        path = next(Path(self.tmp.name).rglob("*.json"))
        before = path.read_bytes()
        result = observation.read_answer_waits(jobs_paths=(), now=10 + 8 * 86400)
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["items"][0]["stale"])
        self.assertEqual((result["total"], result["stale_total"]), (0, 1))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(state.close_signal("seat-3"))
        self.assertEqual(observation.read_answer_waits(jobs_paths=(), now=10 + 8 * 86400)["items"], [])

    def test_s4_current_older_and_expired_windows_match_shared_policy(self):
        now = 10 * 86400
        for sid, seat, since in (("s-current", "seat-current", now - 72 * 3600 + 1),
                                 ("s-older", "seat-older", now - 72 * 3600),
                                 ("s-expired", "seat-expired", now - 7 * 86400 - 1)):
            self.assertTrue(state.open_signal("claude", sid, seat,
                                              "[답변 필요] fixture", now=since))
        result = observation.read_answer_waits(jobs_paths=(), now=now)
        by_id = {item["owner"]["session_id"]: item for item in result["items"]}
        # F104 supersedes the former seven-day disappearance. The historical
        # "expired" fixture name remains to track the original CI failure.
        self.assertEqual(set(by_id), {"s-current", "s-older", "s-expired"})
        self.assertFalse(by_id["s-current"]["stale"])
        self.assertTrue(by_id["s-older"]["stale"])
        self.assertTrue(by_id["s-expired"]["stale"])
        self.assertEqual((result["total"], result["stale_total"]), (1, 2))

    def test_s1_windows_kind_mapping_and_stale_activity_fixture(self):
        from tools.fleet import interaction
        now = 20 * 86400
        interactions = Path(self.tmp.name) / "interactions"
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(interactions)}):
            for sid, kind, since in (("s1-current", "approval", now - 1),
                                     ("s1-older", "permission", now - 72 * 3600),
                                     ("s1-expired", "decision", now - 7 * 86400 - 1)):
                self.assertTrue(interaction.set_wait(sid, "codex", kind,
                                                     "codex-rollout", now=since))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
            by_session = {item["owner"]["session_id"]: item for item in result["items"]}
            self.assertEqual(set(by_session), {"s1-current", "s1-older", "s1-expired"})
            self.assertEqual((result["total"], result["stale_total"]), (1, 2))
            self.assertEqual(by_session["s1-current"]["kind"], "approval")
            self.assertEqual(by_session["s1-older"]["kind"], "permission")
            self.assertTrue(state.publish_cache(result))
            fleet = state.cached_fleet_view(now=now, request=False)
            mapped = {item["session_id"]: item for item in fleet["items"]}
            self.assertEqual(mapped["s1-current"]["source"], "S1-approval")
            self.assertEqual(mapped["s1-current"]["kind"], "approval")
            self.assertEqual(mapped["s1-older"]["source"], "S1-approval")
            self.assertEqual(mapped["s1-older"]["stale"], "older")
            self.assertEqual(mapped["s1-expired"]["source"], "S1-question")
            self.assertEqual(mapped["s1-expired"]["stale"], "older")
            # A real later session activity closes a stale rollout sidecar; an empty
            # producer session set cannot establish that closure (investigated below).
            active = SimpleNamespace(harness="codex", session_id="s1-current",
                                     started_at=1, _interaction_activity=now)
            closed = observation.read_answer_waits(jobs_paths=(), sessions=(active,), now=now)
            self.assertNotIn("s1-current", {item["owner"]["session_id"]
                                            for item in closed["items"]})

    def test_producer_keeps_codex_sidecar_pending_after_later_rollout_write_without_sessions(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.fleet import interaction
        now = 2_000_000
        sid = "12345678-1234-1234-1234-123456789abc"
        state_dir = Path(self.tmp.name) / "interactions"
        codex_home = Path(self.tmp.name) / "codex-home"
        rollout = codex_home / "sessions" / "2026" / "10" / "23" / ("rollout-2026-10-23T00-00-00-" + sid + ".jsonl")
        rollout.parent.mkdir(parents=True)
        rollout.write_text('{"type":"event_msg"}\n', encoding="utf-8")
        os.utime(rollout, (now - 10, now - 10))
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CODEX_HOME": str(codex_home)}):
            self.assertTrue(interaction.set_wait(sid, "codex", "decision",
                                                 "codex-rollout", now=now - 100))
            # This is the detached producer path: no Fleet session objects supplied.
            pending = observation.read_answer_waits(jobs_paths=(), now=now)
            self.assertIn(sid, {item["owner"]["session_id"] for item in pending["items"]})

            missing_sid = "abcdefab-1234-1234-1234-123456789abc"
            self.assertTrue(interaction.set_wait(missing_sid, "codex", "decision",
                                                 "codex-rollout", now=now - 100))
            still_pending = observation.read_answer_waits(jobs_paths=(), now=now)
            self.assertIn(missing_sid, {item["owner"]["session_id"]
                                        for item in still_pending["items"]})

    def test_producer_closes_codex_sidecar_after_later_task_started(self):
        from tools.fleet import interaction
        now = 2_000_100
        sid = "12345678-1234-1234-1234-123456789abc"
        state_dir = Path(self.tmp.name) / "interactions"
        codex_home = Path(self.tmp.name) / "codex-home"
        rollout = codex_home / "sessions" / "2026" / "10" / "23" / ("rollout-session-" + sid + ".jsonl")
        rollout.parent.mkdir(parents=True)
        timestamp = datetime.fromtimestamp(now - 50, timezone.utc).isoformat().replace("+00:00", "Z")
        rollout.write_text(json.dumps({"type": "event_msg", "timestamp": timestamp,
                                       "payload": {"type": "task_started", "turn_id": "new-turn"}}) + "\n",
                           encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CODEX_HOME": str(codex_home)}):
            self.assertTrue(interaction.set_wait(sid, "codex", "decision", "codex-rollout", now=now - 100))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
        self.assertNotIn(sid, {item["owner"]["session_id"] for item in result["items"]})

    def test_producer_keeps_codex_permission_wait_after_same_turn_records(self):
        from tools.fleet import interaction
        now = 2_000_100
        sid = "22345678-1234-1234-1234-123456789abc"
        state_dir = Path(self.tmp.name) / "interactions"
        codex_home = Path(self.tmp.name) / "codex-home"
        rollout = codex_home / "sessions" / "2026" / "10" / "23" / ("rollout-session-" + sid + ".jsonl")
        rollout.parent.mkdir(parents=True)
        timestamp = datetime.fromtimestamp(now - 50, timezone.utc).isoformat().replace("+00:00", "Z")
        rows = [
            {"type": "event_msg", "timestamp": timestamp,
             "payload": {"type": "token_count", "info": {"total_tokens": 12}}},
            {"type": "response_item", "timestamp": timestamp,
             "payload": {"type": "function_call", "name": "request_permission"}},
        ]
        rollout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CODEX_HOME": str(codex_home)}):
            self.assertTrue(interaction.set_wait(sid, "codex", "permission",
                                                 "codex-permissionrequest", now=now - 100))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
        self.assertIn(sid, {item["owner"]["session_id"] for item in result["items"]})

    def test_producer_keeps_codex_wait_when_turn_proof_is_beyond_tail_cap(self):
        from tools.fleet import interaction
        now = 2_000_100
        sid = "32345678-1234-1234-1234-123456789abc"
        state_dir = Path(self.tmp.name) / "interactions"
        codex_home = Path(self.tmp.name) / "codex-home"
        rollout = codex_home / "sessions" / "2026" / "10" / "23" / ("rollout-session-" + sid + ".jsonl")
        rollout.parent.mkdir(parents=True)
        timestamp = datetime.fromtimestamp(now - 50, timezone.utc).isoformat().replace("+00:00", "Z")
        proof = json.dumps({"type": "event_msg", "timestamp": timestamp,
                            "payload": {"type": "task_started", "turn_id": "old"}})
        rollout.write_text(proof + "\n" + ("{}\n" * 100), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CODEX_HOME": str(codex_home)}), \
             mock.patch.object(observation, "_S1_LOG_TAIL_MAX_BYTES", 64):
            self.assertTrue(interaction.set_wait(sid, "codex", "decision", "codex-rollout", now=now - 100))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
        self.assertIn(sid, {item["owner"]["session_id"] for item in result["items"]})

    def test_producer_keeps_claude_sidecar_pending_after_transcript_write_without_sessions(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.fleet import interaction
        now = 2_000_000
        sid = "fedcba98-7654-4321-8765-abcdefabcdef"
        state_dir = Path(self.tmp.name) / "interactions"
        claude_home = Path(self.tmp.name) / "claude-home"
        transcript = claude_home / "projects" / "project" / (sid + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"attachment"}\n', encoding="utf-8")
        os.utime(transcript, (now - 10, now - 10))
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CLAUDE_CONFIG_DIR": str(claude_home)}):
            self.assertTrue(interaction.set_wait(sid, "claude", "decision",
                                                 "claude-asktool", now=now - 100))
            # Transcript metadata can be appended while the user is still deciding.
            pending = observation.read_answer_waits(jobs_paths=(), now=now)
            self.assertIn(sid, {item["owner"]["session_id"] for item in pending["items"]})

    def test_producer_closes_claude_sidecar_after_later_user_turn(self):
        from tools.fleet import interaction
        now = 2_000_100
        sid = "fedcba98-7654-4321-8765-abcdefabcdef"
        state_dir = Path(self.tmp.name) / "interactions"
        claude_home = Path(self.tmp.name) / "claude-home"
        transcript = claude_home / "projects" / "project" / (sid + ".jsonl")
        transcript.parent.mkdir(parents=True)
        timestamp = datetime.fromtimestamp(now - 50, timezone.utc).isoformat().replace("+00:00", "Z")
        transcript.write_text(json.dumps({"type": "user", "timestamp": timestamp,
                                          "message": {"content": "A later user reply"}}) + "\n",
                              encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CLAUDE_CONFIG_DIR": str(claude_home)}):
            self.assertTrue(interaction.set_wait(sid, "claude", "decision", "claude-asktool", now=now - 100))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
        self.assertNotIn(sid, {item["owner"]["session_id"] for item in result["items"]})

    def test_producer_keeps_claude_wait_after_metadata_attachment_and_tool_result(self):
        from tools.fleet import interaction
        now = 2_000_100
        sid = "cedcba98-7654-4321-8765-abcdefabcdef"
        state_dir = Path(self.tmp.name) / "interactions"
        claude_home = Path(self.tmp.name) / "claude-home"
        transcript = claude_home / "projects" / "project" / (sid + ".jsonl")
        transcript.parent.mkdir(parents=True)
        timestamp = datetime.fromtimestamp(now - 50, timezone.utc).isoformat().replace("+00:00", "Z")
        rows = [
            {"type": "user", "timestamp": timestamp, "isMeta": True},
            {"type": "attachment", "timestamp": timestamp, "content": "image"},
            {"type": "user", "timestamp": timestamp,
             "message": {"content": [{"type": "tool_result", "content": "tool output"}]}},
            {"type": "user", "timestamp": timestamp,
             "message": {"content": [{"type": "image", "source": {"type": "base64"}}]}},
        ]
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CLAUDE_CONFIG_DIR": str(claude_home)}):
            self.assertTrue(interaction.set_wait(sid, "claude", "decision", "claude-asktool", now=now - 100))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
        self.assertIn(sid, {item["owner"]["session_id"] for item in result["items"]})

    def test_fake_herdr_gets_quiet_registration_and_only_six_hour_sound(self):
        import time
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        calls = Path(self.tmp.name) / "herdr.calls"
        fake = bin_dir / "herdr"
        fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HERDR_TEST_LOG\"\n", encoding="utf-8")
        fake.chmod(0o755)
        now = time.time()
        result = {"observed_at": now, "items": [{"id": "S3:attempt", "since": now - 7 * 3600,
                  "question": "사용자 답을 기다림"}]}
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                                          "HERDR_TEST_LOG": str(calls)}), \
             mock.patch.object(state, "_request_producer"):
            state._notify_result(result)
            state._notify_result(result)
        lines = calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("notification show", lines[0])
        self.assertNotIn("--sound", lines[0])
        self.assertIn("--sound request", lines[-1])
        self.assertTrue(all("사용자 답을 기다림" in line for line in lines))

    def test_absent_herdr_does_not_leave_a_dedupe_marker(self):
        with mock.patch("human_wait_state.shutil.which", return_value=None):
            self.assertFalse(state._notify_item({"id": "S4:missing", "question": "확인"},
                                                boundary="registered"))
        self.assertFalse((state.state_root() / "notifications").exists())

    def test_producer_stays_only_for_future_current_notification_boundaries(self):
        now = 10_000.0
        item = {"source": "S3", "since": now - 1800, "stale": False}
        self.assertEqual(state._notification_delay([item], now, enabled=True), 1800)
        self.assertIsNone(state._notification_delay([item], now, enabled=False))
        self.assertIsNone(state._notification_delay([dict(item, stale=True)], now, enabled=True))
        self.assertIsNone(state._notification_delay(
            [{"source": "S3", "since": now - 73 * 3600, "stale": False}], now, enabled=True))

    def test_idle_producer_sleeps_until_notification_boundary(self):
        demand = state.state_root() / ".demand"
        demand.parent.mkdir(parents=True, exist_ok=True)
        demand.touch()
        os.utime(demand, (0, 0))
        now = 10_000.0
        cached = {"items": [{"source": "S3", "since": now, "stale": False}]}
        class Slept(Exception):
            pass
        with mock.patch.object(state, "_refresh_cache"), \
             mock.patch.object(state, "_read_cache", return_value=cached), \
             mock.patch.object(state.shutil, "which", return_value="/bin/herdr"), \
             mock.patch.object(state.time, "time", return_value=now), \
             mock.patch.object(state.time, "sleep", side_effect=Slept) as sleep, \
             mock.patch.object(state.fcntl, "flock"), mock.patch.object(state.os, "close"):
            state._producer_loop(123)
        sleep.assert_called_once_with(3600.0)

    def test_notification_claim_is_single_under_concurrent_refresh(self):
        bin_dir = Path(self.tmp.name) / "race-bin"
        bin_dir.mkdir()
        calls = Path(self.tmp.name) / "race.calls"
        fake = bin_dir / "herdr"
        fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HERDR_TEST_LOG\"\n", encoding="utf-8")
        fake.chmod(0o755)
        item = {"id": "S4:race", "question": "같은 대기", "since": 100}
        errors = []
        def notify():
            try:
                state._notify_item(item, boundary="registered")
            except Exception as exc:
                errors.append(exc)
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir), "HERDR_TEST_LOG": str(calls)}):
            threads = [threading.Thread(target=notify) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(calls.read_text(encoding="utf-8").splitlines()), 1)

    def test_producer_is_single_flight_before_spawn(self):
        fd_holder = []
        spawner = mock.Mock(side_effect=lambda _cmd, **kw: (
            fd_holder.append(os.dup(kw["pass_fds"][0])) or SimpleNamespace()))
        with mock.patch.object(state.subprocess, "Popen", spawner):
            self.assertTrue(state._ensure_producer())
            self.assertFalse(state._ensure_producer())
            self.assertEqual(len(fd_holder), 1)
        for held_fd in fd_holder:
            os.close(held_fd)

    def test_spawn_failure_releases_projection_lock_for_next_demand(self):
        with mock.patch.object(state.subprocess, "Popen", side_effect=OSError("fixture spawn failure")):
            self.assertFalse(state._ensure_producer())
        with mock.patch.object(state.subprocess, "Popen", return_value=SimpleNamespace()):
            self.assertTrue(state._ensure_producer())

    def test_projection_failure_publishes_fresh_incomplete_unknown(self):
        import human_gate_observation
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from fleet.collectors import dispatch
        with mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[]), \
             mock.patch.object(human_gate_observation, "read_answer_waits",
                               side_effect=RuntimeError("fixture failure")):
            self.assertFalse(state._refresh_cache())
        value = state._read_cache()
        self.assertIsNotNone(value)
        self.assertFalse(value["complete"])
        self.assertEqual(value["items"], [])
        self.assertGreater(value["observed_at"], 0)


class _StrictOpenCodeTransport:
    def __init__(self, session_id):
        self.session_id = session_id
        self.forms = {}
        self.form_ids = []
        self.permissions = {}
        self.permission_ids = []
        self.calls = []
        self.unexpected = []
        self.post_status = 204

    def question(self, request_id="frm_1", *, state="pending", message_id="msg_1",
                 field_type="multiselect", min_items=1, max_items=1, custom=False):
        field = {"type": field_type, "key": "choice", "title": "Pick a color",
                 "description": "Choose the color for the task", "required": True}
        if field_type == "multiselect":
            field.update({"options": [{"value": "red", "label": "Red"},
                                       {"value": "blue", "label": "Blue"}],
                          "minItems": min_items, "maxItems": max_items, "custom": custom})
        value = {
            "id": request_id, "sessionID": self.session_id, "title": "Question title",
            "metadata": {"messageID": message_id},
            "fields": [field],
            "state": {"status": state},
        }
        self.forms[request_id] = value
        if request_id not in self.form_ids:
            self.form_ids.append(request_id)
        return value

    def permission(self, request_id="per_1", *, message_id="msg_permission"):
        value = {"id": request_id, "sessionID": self.session_id,
                 "title": "Run a shell command", "type": "shell",
                 "metadata": {"command": "printf fixture"},
                 "pattern": {"action": "shell", "resource": "printf fixture"},
                 "messageID": message_id}
        self.permissions[request_id] = value
        if request_id not in self.permission_ids:
            self.permission_ids.append(request_id)
        return value

    def __call__(self, endpoint, method, path, payload=None):
        self.calls.append((method, path, payload))
        if endpoint.get("url") != "http://127.0.0.1:4096":
            self.unexpected.append((method, path, "endpoint"))
            return {"status": 0, "reason": "fixture-endpoint-mismatch"}
        if method == "GET" and path == "/global/health":
            return {"status": 200, "body": {"healthy": True, "version": "2.0.32"}}
        prefix = "/api/session/" + self.session_id + "/"
        if method == "GET" and path == prefix + "form":
            data = [{key: value for key, value in self.forms[request_id].items()
                     if key != "state"} for request_id in self.form_ids]
            return {"status": 200, "body": {"data": data}}
        if method == "GET" and path.startswith(prefix + "form/"):
            request_id = path[len(prefix + "form/"):]
            value = self.forms.get(request_id)
            return ({"status": 200, "body": {"data": value}} if value else
                    {"status": 404, "reason": "not-found"})
        if method == "POST" and path.startswith(prefix + "form/") and path.endswith("/reply"):
            request_id = path[len(prefix + "form/"):-len("/reply")]
            if request_id not in self.forms or not isinstance(payload, dict):
                self.unexpected.append((method, path, "form-reply-shape"))
                return {"status": 400, "reason": "fixture-reply-shape"}
            if self.post_status == 204:
                self.forms[request_id]["state"] = {"status": "answered",
                                                    "answer": payload.get("answer", {})}
            return {"status": self.post_status, "body": None}
        if method == "GET" and path == prefix + "permission":
            data = [self.permissions[request_id] for request_id in self.permission_ids]
            return {"status": 200, "body": {"data": data}}
        if method == "GET" and path.startswith(prefix + "permission/"):
            request_id = path[len(prefix + "permission/"):]
            value = self.permissions.get(request_id)
            return ({"status": 200, "body": {"data": value}} if value else
                    {"status": 404, "reason": "not-found"})
        if method == "POST" and path.startswith(prefix + "permission/") and path.endswith("/reply"):
            if not isinstance(payload, dict) or set(payload) != {"decision"}:
                self.unexpected.append((method, path, "permission-reply-shape"))
                return {"status": 400, "reason": "fixture-reply-shape"}
            return {"status": self.post_status, "body": None}
        self.unexpected.append((method, path, payload))
        return {"status": 0, "reason": "fixture-endpoint-unsupported"}


class SelectedQuestionActionsTest(unittest.TestCase):
    """Strict isolated tests for selected private detail and one-shot native replies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sid = "ses_fixture"
        self.now = time.time()
        self.endpoint = {"url": "http://127.0.0.1:4096", "generation": "fixture-gen-1",
                         "pid": 1234, "proc_start": "123", "socket_inodes": []}
        self.api = _StrictOpenCodeTransport(self.sid)
        env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
            "HOME": self.tmp.name,
            "FLEET_INTERACTION_STATE_DIR": str(Path(self.tmp.name) / "interactions"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)
        endpoint = mock.patch.object(
            state, "_opencode_endpoint",
            side_effect=lambda sid: dict(self.endpoint) if sid == self.sid else None)
        endpoint.start()
        self.addCleanup(endpoint.stop)
        transport = mock.patch.object(state, "_opencode_http", side_effect=self.api)
        transport.start()
        self.addCleanup(transport.stop)
        self._publish_wait()

    def _publish_wait(self, *, harness="opencode", kind="decision", request_id=None,
                      turn_id=None, item_id=None, sid=None, stamp=None):
        sid = sid or self.sid
        stamp = self.now if stamp is None else stamp
        self.item_id = f"S1:{harness}:{sid}:{int(stamp * 1000)}"
        item = {"id": self.item_id, "source": "S1", "kind": kind,
                "question": None, "choices": [], "route_id": None,
                "attempt_id": None, "gate_id": None, "since": stamp,
                "stale": False, "owner": {"harness": harness, "session_id": sid},
                "attribution": "exact"}
        for key, value in (("request_id", request_id), ("turn_id", turn_id), ("item_id", item_id)):
            if value is not None:
                item[key] = value
        self.assertTrue(state.publish_cache({"items": [item], "complete": True,
                                             "observed_at": time.time(), "health": []}))
        return {"id": self.item_id, "harness": harness, "session_id": sid}

    def test_selected_detail_and_draft_stay_private_across_refresh(self):
        self.api.question()
        selected = state.read_selected_request({"id": self.item_id,
                                                "harness": "opencode",
                                                "session_id": self.sid})
        self.assertEqual(selected["state"], "open")
        self.assertEqual(selected["detail"]["question"], "Pick a color")
        self.assertEqual(selected["choices"][0]["value"], "red")
        drafted = state.write_selected_draft(selected, "private draft")
        self.assertEqual(drafted["draft"], "private draft")
        self._publish_wait(stamp=self.now + 0.1)
        reopened = state.read_selected_request({**selected, "selected_id": self.item_id})
        self.assertEqual(reopened["draft"], "private draft")
        fleet_bytes = state._fleet_cache_path().read_text(encoding="utf-8")
        projection_bytes = state._cache_path().read_text(encoding="utf-8")
        self.assertNotIn("Pick a color", fleet_bytes)
        self.assertNotIn("private draft", fleet_bytes)
        self.assertNotIn("Pick a color", projection_bytes)
        self.assertIn("private draft", projection_bytes)
        self.assertEqual(self.api.unexpected, [])

    def test_native_sidecar_refs_reach_body_free_s1_projection(self):
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet import interaction
        self.assertTrue(interaction.set_wait(
            "thread_42", "codex", "decision", "codex-appserver", now=self.now,
            request_id="server_req_42", turn_id="turn_8", item_id="item_4"))
        observed = observation.read_answer_waits(jobs_paths=[], sessions=(), now=self.now)
        item = next(row for row in observed["items"] if row["source"] == "S1")
        self.assertEqual((item["request_id"], item["turn_id"], item["item_id"]),
                         ("server_req_42", "turn_8", "item_4"))
        self.assertIsNone(item["question"])
        self.assertEqual(item["choices"], [])
        self.assertTrue(state.publish_cache(observed))
        fleet = state.cached_fleet_view(request=False)
        row = next(row for row in fleet["items"] if row["session_id"] == "thread_42")
        self.assertEqual(row["request_id"], "server_req_42")
        self.assertNotIn("question", row)
        self.assertNotIn("choices", row)

    def test_opencode_v2_form_reply_uses_exact_session_request_and_shape(self):
        self.api.question()
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        result = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="answer-1")
        self.assertEqual(result["state"], "applied")
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(posts, [("POST", f"/api/session/{self.sid}/form/frm_1/reply",
                                  {"answer": {"choice": ["red"]}})])
        self.assertEqual(self.api.unexpected, [])

    def test_opencode_local_cardinality_and_option_errors_leave_request_correctable(self):
        self.api.question(min_items=1, max_items=1)
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        drafted = state.write_selected_draft(selected, "red, then blue")
        invalid_count = state.submit_selected_response(
            drafted, {"answers": {"choice": ["red", "blue"]}}, action_id="too-many")
        self.assertEqual(invalid_count["state"], "open")
        self.assertEqual(invalid_count["reason"], "opencode-answer-count-invalid")
        self.assertEqual(invalid_count["draft"], "red, then blue")
        self.assertEqual(invalid_count["native_ref"], selected["native_ref"])
        self.assertEqual(invalid_count["actions"], ["reply"])
        self.assertNotIn("action_id", invalid_count)
        self.assertIsNone(state._selected_entry(invalid_count).get("action_id"))
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        drafted = state.write_selected_draft(invalid_count, "green")
        invalid_option = state.submit_selected_response(
            drafted, {"answers": {"choice": ["green"]}}, action_id="unknown-option")
        self.assertEqual(invalid_option["state"], "open")
        self.assertEqual(invalid_option["reason"], "opencode-answer-not-in-current-options")
        self.assertEqual(invalid_option["draft"], "green")
        self.assertEqual(invalid_option["native_ref"], selected["native_ref"])
        self.assertEqual(invalid_option["actions"], ["reply"])
        self.assertNotIn("action_id", invalid_option)
        self.assertIsNone(state._selected_entry(invalid_option).get("action_id"))
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        corrected = state.submit_selected_response(
            invalid_option, {"answers": {"choice": ["red"]}}, action_id="corrected-once")
        self.assertEqual(corrected["state"], "applied")
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(posts, [("POST", f"/api/session/{self.sid}/form/frm_1/reply",
                                  {"answer": {"choice": ["red"]}})])
        self.assertEqual(self.api.unexpected, [])

    def test_opencode_minimum_multiselect_bounds_are_enforced_before_claim(self):
        self.api.question(min_items=2, max_items=2)
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        self.assertEqual(selected["native_ref"]["min_items"], 2)
        self.assertEqual(selected["native_ref"]["max_items"], 2)
        drafted = state.write_selected_draft(selected, "red")
        incomplete = state.submit_selected_response(
            drafted, {"answers": {"choice": ["red"]}}, action_id="one-of-two")
        self.assertEqual(incomplete["state"], "open")
        self.assertEqual(incomplete["reason"], "opencode-answer-count-invalid")
        self.assertEqual(incomplete["draft"], "red")
        self.assertIsNone(state._selected_entry(incomplete).get("action_id"))
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        corrected = state.submit_selected_response(
            incomplete, {"answers": {"choice": ["red", "blue"]}},
            action_id="two-of-two")
        self.assertEqual(corrected["state"], "applied")
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(posts, [("POST", f"/api/session/{self.sid}/form/frm_1/reply",
                                  {"answer": {"choice": ["red", "blue"]}})])

    def test_opencode_string_form_keeps_single_free_text_answer(self):
        self.api.question(field_type="string")
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        drafted = state.write_selected_draft(selected, "single answer")
        invalid = state.submit_selected_response(
            drafted, {"answers": {"choice": ["one", "two"]}}, action_id="string-too-many")
        self.assertEqual(invalid["state"], "open")
        self.assertEqual(invalid["reason"], "opencode-string-answer-count-invalid")
        self.assertEqual(invalid["draft"], "single answer")
        self.assertIsNone(state._selected_entry(invalid).get("action_id"))
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        result = state.submit_selected_response(
            invalid, {"answers": {"choice": ["private answer"]}},
            action_id="string-answer")
        self.assertEqual(result["state"], "applied")
        self.assertEqual([call for call in self.api.calls if call[0] == "POST"], [
            ("POST", f"/api/session/{self.sid}/form/frm_1/reply",
             {"answer": {"choice": "private answer"}})])

    def test_opencode_custom_multiselect_accepts_native_custom_value(self):
        self.api.question(min_items=1, max_items=1, custom=True)
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        self.assertTrue(selected["native_ref"]["custom"])
        result = state.submit_selected_response(
            selected, {"answers": {"choice": ["user-supplied"]}}, action_id="custom-answer")
        self.assertEqual(result["state"], "applied")
        self.assertEqual([call for call in self.api.calls if call[0] == "POST"], [
            ("POST", f"/api/session/{self.sid}/form/frm_1/reply",
             {"answer": {"choice": ["user-supplied"]}})])

    def test_approval_approve_is_one_shot_and_defer_sends_nothing(self):
        self._publish_wait(kind="permission")
        self.api.permission()
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        deferred = state.submit_selected_response(selected, {"decision": "defer"})
        self.assertEqual(deferred["state"], "open")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])
        approved = state.submit_selected_response(selected, {"decision": "approve"},
                                                 action_id="approve-once")
        self.assertEqual(approved["state"], "applied")
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(posts, [("POST", f"/api/session/{self.sid}/permission/per_1/reply",
                                  {"decision": "once"})])
        duplicate = state.submit_selected_response(selected, {"decision": "reject"},
                                                   action_id="second-action")
        self.assertEqual(duplicate["action_id"], "approve-once")
        self.assertEqual(len([call for call in self.api.calls if call[0] == "POST"]), 1)
        self.assertEqual(self.api.unexpected, [])

    def test_approval_reject_maps_to_native_reject(self):
        self._publish_wait(kind="approval")
        self.api.permission()
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        result = state.submit_selected_response(selected, {"decision": "reject"},
                                                action_id="reject-once")
        self.assertEqual(result["state"], "applied")
        self.assertEqual([call for call in self.api.calls if call[0] == "POST"], [
            ("POST", f"/api/session/{self.sid}/permission/per_1/reply", {"decision": "reject"})])
        self.assertEqual(self.api.unexpected, [])

    def test_replaced_cancelled_and_restarted_requests_are_refused(self):
        self.api.question()
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        self.api.question("frm_2", message_id="msg_2")
        self.api.form_ids = ["frm_2"]
        replaced = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="late-answer")
        self.assertNotEqual(replaced["state"], "applied")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        self.api.form_ids = ["frm_1"]
        self.api.forms["frm_1"]["state"] = {"status": "cancelled"}
        cancelled = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                 "session_id": self.sid})
        self.assertEqual(cancelled["state"], "cancelled")
        refused = state.submit_selected_response(
            cancelled, {"answers": {"choice": ["red"]}}, action_id="cancelled-answer")
        self.assertEqual(refused["state"], "cancelled")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        self.api.forms["frm_1"]["state"] = {"status": "pending"}
        fresh = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                             "session_id": self.sid})
        self.endpoint["generation"] = "fixture-gen-restarted"
        expired = state.submit_selected_response(
            fresh, {"answers": {"choice": ["red"]}}, action_id="old-server-answer")
        self.assertNotEqual(expired["state"], "applied")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

    def test_ambiguous_and_ambiguous_delivery_never_replay(self):
        self.api.question("frm_1", message_id="msg_1")
        self.api.question("frm_2", message_id="msg_2")
        ambiguous = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                 "session_id": self.sid})
        self.assertFalse(ambiguous["support"]["supported"])
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        self.api.form_ids = ["frm_1"]
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        self.api.post_status = 0
        first = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="ambiguous-send")
        self.assertEqual(first["state"], "unknown")
        self.assertEqual(len([call for call in self.api.calls if call[0] == "POST"]), 1)
        second = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="retry")
        self.assertEqual(second["action_id"], "ambiguous-send")
        self.assertEqual(len([call for call in self.api.calls if call[0] == "POST"]), 1)
        self.assertEqual(self.api.unexpected, [])

    def test_unsupported_runtime_and_schema_keep_native_fallback(self):
        no_endpoint = mock.patch.object(state, "_opencode_endpoint", return_value=None)
        no_endpoint.start()
        self.addCleanup(no_endpoint.stop)
        unsupported = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                   "session_id": self.sid})
        self.assertFalse(unsupported["support"]["supported"])
        self.assertEqual(unsupported["state"], "unknown")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        claude_ref = self._publish_wait(harness="claude", kind="decision", sid="claude_fixture",
                                        stamp=self.now + 1)
        claude = state.read_selected_request(claude_ref)
        self.assertFalse(claude["support"]["supported"])
        self.assertEqual(claude["state"], "unknown")

    def test_opencode_form_without_native_turn_keeps_draft_but_cannot_submit(self):
        self.api.question(message_id=None)
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        self.assertEqual(selected["request_id"], "frm_1")
        self.assertIsNone(selected["turn_id"])
        self.assertFalse(selected["support"]["supported"])
        drafted = state.write_selected_draft(selected, "keep this native fallback draft")
        self.assertEqual(drafted["draft"], "keep this native fallback draft")
        result = state.submit_selected_response(
            drafted, {"answers": {"choice": ["red"]}}, action_id="no-turn")
        self.assertFalse(result["support"]["supported"])
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

    def test_codex_control_shape_uses_native_gateway_questions(self):
        sid = "thread_fixture"
        self._publish_wait(harness="codex", kind="decision", sid=sid,
                           request_id="request_9", turn_id="turn_9", item_id="item_9")
        control = {"path": str(Path(self.tmp.name) / "control.sock"), "pid": 77,
                   "proc_start": "770", "epoch": 5,
                   "status": {"status": "ready", "thread_id": sid}}
        ref = {"harness": "codex", "session_id": sid, "request_id": "request_9",
               "turn_id": "turn_9", "item_id": "item_9", "kind": "question",
               "method": "item/tool/requestUserInput", "epoch": 5,
               "binding_generation": 2}
        detail = {"v": 1, "harness": "codex", "session_id": sid,
                  "request_id": "request_9", "turn_id": "turn_9",
                  "kind": "question", "detail": {
                      "threadId": sid, "turnId": "turn_9", "itemId": "item_9",
                      "questions": [{"id": "choice", "header": "Confirm",
                                     "question": "Exact question",
                                     "options": [{"label": "yes", "description": "Yes"}]}]},
                  "state": "open", "support": {"supported": True}, "actions": ["answer"]}
        operations = []

        def gateway_call(_path, payload):
            operations.append(payload)
            if payload["op"] == "interaction-list":
                return {"schema_version": 1, "status": "ready", "requests": [ref]}
            if payload["op"] == "interaction-detail":
                return {"schema_version": 1, "status": "ready", "request": detail}
            if payload["op"] == "interaction-reply":
                return {"schema_version": 1, "status": "applied",
                        "request": ref, "action_id": payload["action_id"]}
            return {"schema_version": 1, "status": "rejected"}

        with mock.patch.object(state, "_codex_control", return_value=control), \
             mock.patch.object(state, "_codex_control_request", side_effect=gateway_call):
            selected = state.read_selected_request({"id": self.item_id, "harness": "codex",
                                                    "session_id": sid, "request_id": "request_9"})
            self.assertEqual(selected["state"], "open")
            result = state.submit_selected_response(
                selected, {"answers": {"choice": ["yes"]}}, action_id="codex-action")
        self.assertEqual(result["state"], "applied")
        reply = operations[-1]
        self.assertEqual(reply["op"], "interaction-reply")
        self.assertEqual(reply["ref"], ref)
        self.assertEqual(reply["response"], {"answers": {"choice": ["yes"]}})
        self.assertEqual(reply["action_id"], "codex-action")

    def test_v1_and_malformed_projection_are_unknown(self):
        path = state._cache_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text('{"schema_version":1,"observed_at":100,"complete":true,'
                        '"total":9,"by_session":{}}', encoding="utf-8")
        path.chmod(0o600)
        self.assertIsNone(state._read_cache(now=101))
        path.write_text('{"schema_version":2,"observed_at":100,"items":[{}],'
                        '"by_session":{},"total":0,"stale_total":0}', encoding="utf-8")
        self.assertIsNone(state._read_cache(now=101))


class SelectedAnswerIntegrationTest(unittest.TestCase):
    """Exercise shared selected answers through the real managed-gateway socket."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
            "HOME": self.tmp.name,
            "PYTHONDONTWRITEBYTECODE": "1",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)

        path = Path(__file__).with_name("codex_managed_gateway.test.py")
        spec = importlib.util.spec_from_file_location("codex_managed_gateway_integration_fixture", path)
        if spec is None or spec.loader is None:
            raise AssertionError("managed gateway fixture could not be loaded")
        self.gateway_tests = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gateway_tests)
        self.gateway_fixture = self.gateway_tests.ManagedGatewayTest("runTest")
        self.gateway_fixture.setUp()
        self.addCleanup(self.gateway_fixture.tearDown)
        self.control = self._control_binding()
        self._codex_control_patch = mock.patch.object(
            state, "_codex_control",
            side_effect=lambda sid: dict(self.control) if sid == "thread-1" else None)
        self._codex_control_patch.start()
        self.addCleanup(self._codex_control_patch.stop)

    def _control_binding(self):
        status = self.gateway_tests.control(self.gateway_fixture.control, {
            "schema_version": 1, "op": "status"})
        return {"path": str(self.gateway_fixture.control), "pid": os.getpid(),
                "proc_start": state._proc_start_ticks(os.getpid()) or "fixture-process",
                "epoch": status["epoch"], "status": status}

    def _wait(self, request_id, *, kind="decision", turn_id="turn-question",
              item_id="item-question", stale=False, stamp=None, append=False):
        stamp = time.time() if stamp is None else stamp
        row_id = f"S1:codex:thread-1:{request_id!s}:{stamp:.6f}"
        item = {"id": row_id, "source": "S1", "kind": kind,
                "question": None, "choices": [], "route_id": None,
                "attempt_id": None, "gate_id": None, "since": stamp,
                "stale": stale, "owner": {"harness": "codex", "session_id": "thread-1"},
                "attribution": "exact", "request_id": request_id,
                "turn_id": turn_id, "item_id": item_id}
        items = []
        if append:
            previous = state._read_cache()
            if previous is not None:
                items.extend(previous["items"])
        items.append(item)
        self.assertTrue(state.publish_cache({"items": items, "complete": True,
                                             "observed_at": time.time(), "health": []}))
        return {"id": row_id, "harness": "codex", "session_id": "thread-1",
                "request_id": request_id, "turn_id": turn_id, "item_id": item_id}

    def _server_request(self, request):
        current = self.gateway_fixture.server.current
        if current is None:
            raise AssertionError("fake App Server has no owning connection")
        current.write_json(request)
        return self.gateway_fixture.client.wait_for(
            lambda item: item.get("id") == request.get("id")
            and item.get("method") == request.get("method"))

    def _wait_native_response(self, request_id, count=1):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.gateway_fixture.server.lock:
                matches = [item for item in self.gateway_fixture.server.approval_responses
                           if item.get("id") == request_id and "method" not in item
                           and ("result" in item or "error" in item)]
            if len(matches) >= count:
                return matches
            time.sleep(0.01)
        raise AssertionError(f"native response {request_id!r} did not arrive")

    def _selected(self, ref):
        selected = state.read_selected_request(ref)
        self.assertIsNotNone(selected.get("native_ref"))
        return selected

    def test_shared_question_answers_all_native_questions_and_tracks_receipt(self):
        fixture = self.gateway_fixture
        native = {
            "jsonrpc": "2.0", "id": 501, "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-1", "turnId": "turn-question",
                        "itemId": "item-question", "questions": [
                            {"id": "language", "header": "Language",
                             "question": "Which language?", "isOther": False,
                             "options": [{"label": "Rust", "description": "Systems"},
                                         {"label": "Python", "description": "Scripts"}]},
                            {"id": "style", "header": "Style", "question": "How concise?",
                             "isOther": True, "options": None}],
                        "isBlocking": True, "autoResolutionMs": None},
        }
        self._server_request(native)
        listing = self.gateway_tests.control(fixture.control, {
            "schema_version": 1, "op": "interaction-list", "session_id": "thread-1"})
        self.assertNotIn("Which language?", json.dumps(listing))
        ref = self._wait(501)
        selected = self._selected(ref)
        self.assertEqual(selected["state"], "open")
        self.assertEqual(selected["request_id"], 501)
        wrong_turn = dict(selected, turn_id="turn-forged")
        rejected = state.submit_selected_response(
            wrong_turn, {"answers": {"language": ["Rust"], "style": ["Concise"]}},
            action_id="forged-turn")
        self.assertFalse(rejected.get("support", {}).get("supported"))
        self.assertEqual(fixture.gateway._interaction_actions, {})
        self.assertEqual([q["id"] for q in selected["detail"]["questions"]],
                         ["language", "style"])
        self.assertEqual(selected["detail"]["questions"][0]["choices"][0]["value"], "Rust")
        self.assertIsNone(selected.get("question_id"))
        self.assertEqual(state._portable_response(
            selected, {"answers": {"language": ["Rust"]}})[1],
            "nonempty-exact-question-answer-required")

        drafted = state.write_selected_draft(selected, "private two-part answer")
        self.assertEqual(drafted["draft"], "private two-part answer")
        refreshed_ref = self._wait(501, stamp=time.time() + 0.01)
        selected = self._selected(refreshed_ref)
        self.assertEqual(selected["draft"], "private two-part answer")
        projection = state._cache_path().read_text(encoding="utf-8")
        fleet = state._fleet_cache_path().read_text(encoding="utf-8")
        self.assertNotIn("Which language?", projection)
        self.assertIn("private two-part answer", projection)
        self.assertNotIn("Which language?", fleet)
        self.assertNotIn("private two-part answer", fleet)

        result = state.submit_selected_response(
            selected, {"answers": {"language": ["Rust"], "style": ["Concise"]}},
            action_id="integration-question")
        self.assertEqual(result["state"], "submitted")
        native_reply = self._wait_native_response(501)[0]
        self.assertEqual(native_reply["result"], {
            "answers": {"language": {"answers": ["Rust"]},
                        "style": {"answers": ["Concise"]}}})

        fixture.server.emit_resolved("thread-1", 501)
        fixture.client.wait_for(lambda item: item.get("method") == "serverRequest/resolved")
        received = state.read_selected_request(selected)
        self.assertEqual(received["state"], "received")
        self.assertEqual(received["draft"], "private two-part answer")
        self.assertEqual(received["result"]["status"], "received")

    def test_option_only_codex_question_rejects_free_text_then_accepts_native_choice(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": 811, "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-1", "turnId": "turn-option-only",
                        "itemId": "item-option-only", "questions": [
                            {"id": "route", "header": "Route", "question": "Choose a route",
                             "isOther": False,
                             "options": [{"label": "Green", "description": "Allowed"},
                                         {"label": "Blue", "description": "Allowed"}]}]}})
        ref = self._wait(811, turn_id="turn-option-only", item_id="item-option-only")
        selected = self._selected(ref)
        drafted = state.write_selected_draft(selected, "free-form route")
        invalid = state.submit_selected_response(
            drafted, {"answers": {"route": ["free-form route"]}},
            action_id="option-only-invalid")
        self.assertEqual(invalid["state"], "open")
        self.assertEqual(invalid["reason"], "codex-answer-not-in-current-options")
        self.assertEqual(invalid["draft"], "free-form route")
        self.assertEqual(invalid["actions"], ["reply"])
        self.assertEqual(invalid["native_ref"], selected["native_ref"])
        self.assertNotIn("action_id", invalid)
        self.assertIsNone(state._selected_entry(invalid).get("action_id"))
        self.assertNotIn("option-only-invalid", fixture.gateway._interaction_actions)
        with fixture.server.lock:
            native_responses = [item for item in fixture.server.approval_responses
                                if item.get("id") == 811 and "method" not in item
                                and ("result" in item or "error" in item)]
        self.assertEqual(native_responses, [])

        corrected = state.submit_selected_response(
            invalid, {"answers": {"route": ["Green"]}}, action_id="option-only-corrected")
        self.assertEqual(corrected["state"], "submitted")
        self.assertEqual(self._wait_native_response(811)[0]["result"], {
            "answers": {"route": {"answers": ["Green"]}}})
        self.assertIn("option-only-corrected", fixture.gateway._interaction_actions)

    def test_unavailable_command_approval_stays_unclaimed_then_allowed_reject_works(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": 812,
            "method": "item/commandExecution/requestApproval",
            "params": {"kind": "command", "threadId": "thread-1",
                        "turnId": "turn-command-limited", "itemId": "item-command-limited",
                        "startedAtMs": 1, "environmentId": None, "command": "run fixture",
                        "cwd": "/repo", "reason": "Run fixture",
                        "availableDecisions": ["acceptForSession", "decline", "cancel"]}})
        ref = self._wait(812, kind="approval", turn_id="turn-command-limited",
                         item_id="item-command-limited")
        selected = self._selected(ref)
        self.assertEqual(selected["actions"], ["reject", "defer"])
        self.assertNotIn("acceptForSession", selected["actions"])
        self.assertNotIn("applyNetworkPolicyAmendment", selected["actions"])

        invalid = state.submit_selected_response(
            selected, {"decision": "approve"}, action_id="unsupported-command-approve")
        self.assertEqual(invalid["state"], "open")
        self.assertEqual(invalid["reason"], "codex-command-decision-not-available")
        self.assertEqual(invalid["actions"], ["reject", "defer"])
        self.assertEqual(invalid["native_ref"], selected["native_ref"])
        self.assertNotIn("action_id", invalid)
        entry = state._selected_entry(invalid)
        self.assertFalse(entry and entry.get("action_id"))
        self.assertNotIn("unsupported-command-approve", fixture.gateway._interaction_actions)
        with fixture.server.lock:
            native_responses = [item for item in fixture.server.approval_responses
                                if item.get("id") == 812 and "method" not in item
                                and ("result" in item or "error" in item)]
        self.assertEqual(native_responses, [])

        corrected = state.submit_selected_response(
            invalid, {"decision": "reject"}, action_id="command-reject-once")
        self.assertEqual(corrected["state"], "submitted")
        self.assertEqual(self._wait_native_response(812)[0]["result"], {"decision": "decline"})
        self.assertIn("command-reject-once", fixture.gateway._interaction_actions)

    def test_file_approval_uses_actual_native_targets_and_refuses_session_root(self):
        fixture = self.gateway_fixture
        current = fixture.server.current
        self.assertIsNotNone(current)
        current.write_json({"jsonrpc": "2.0", "method": "turn/started", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-file", "status": "inProgress",
                                               "items": []}}})
        fixture.client.wait_for(lambda item: item.get("method") == "turn/started")

        def request(request_id, item_id, grant_root=None):
            self._server_request({"jsonrpc": "2.0", "id": request_id,
                "method": "item/fileChange/requestApproval", "params": {
                    "threadId": "thread-1", "turnId": "turn-file", "itemId": item_id,
                    "startedAtMs": 1, "reason": "Update report", "grantRoot": grant_root}})
            return self._wait(request_id, kind="approval", turn_id="turn-file", item_id=item_id)

        def native_item(item_id):
            current.write_json({"jsonrpc": "2.0", "method": "item/started", "params": {
                "threadId": "thread-1", "turnId": "turn-file", "item": {
                    "id": item_id, "type": "fileChange", "status": "inProgress",
                    "changes": [{"path": "/repo/report.py", "kind": {"type": "update"},
                                 "diff": "PRIVATE-FILE-DIFF-SENTINEL"}]}}})
            fixture.client.wait_for(lambda item: item.get("method") == "item/started"
                                    and item.get("params", {}).get("item", {}).get("id") == item_id)

        ref = request(530, "file-visible")
        missing = self._selected(ref)
        self.assertEqual(missing["actions"], ["reject", "defer"])
        invalid = state.submit_selected_response(missing, {"decision": "approve"},
                                                  action_id="missing-targets")
        self.assertEqual(invalid["state"], "open")
        self.assertIsNone(state._selected_entry(missing))
        native_item("file-visible")
        visible = self._selected(ref)
        self.assertNotEqual(missing["native_ref"]["request"]["binding_generation"],
                            visible["native_ref"]["request"]["binding_generation"])
        stale = state.submit_selected_response(missing, {"decision": "approve"},
                                               action_id="old-target-context")
        self.assertEqual(stale["state"], "unknown")
        self.assertIsNone(state._selected_entry(missing))
        self.assertEqual(visible["detail"]["changes"],
                         [{"path": "/repo/report.py", "kind": {"type": "update"}}])
        self.assertIn("approve", visible["actions"])
        approved = state.submit_selected_response(visible, {"decision": "approve"},
                                                   action_id="visible-file-once")
        self.assertEqual(approved["state"], "submitted")
        self.assertEqual(self._wait_native_response(530)[0]["result"], {"decision": "accept"})
        state.submit_selected_response(visible, {"decision": "approve"},
                                       action_id="visible-file-duplicate")
        self.assertEqual(len(self._wait_native_response(530)), 1)

        native_item("file-session-root")
        root_ref = request(531, "file-session-root", "/repo")
        root_selected = self._selected(root_ref)
        self.assertEqual(root_selected["actions"], ["reject", "defer"])
        invalid = state.submit_selected_response(root_selected, {"decision": "approve"},
                                                 action_id="no-session-grant")
        self.assertEqual(invalid["state"], "open")
        self.assertIsNone(state._selected_entry(root_selected))
        direct = self.gateway_tests.control(fixture.control, {
            "schema_version": 1, "op": "interaction-reply",
            "ref": root_selected["native_ref"]["request"], "response": {"decision": "approve"},
            "action_id": "no-direct-session-grant"})
        self.assertEqual(direct["status"], "rejected")
        with fixture.server.lock:
            self.assertFalse([item for item in fixture.server.approval_responses
                              if item.get("id") == 531])
        rejected = state.submit_selected_response(root_selected, {"decision": "reject"},
                                                   action_id="decline-session-root")
        self.assertEqual(rejected["state"], "submitted")
        self.assertEqual(self._wait_native_response(531)[0]["result"], {"decision": "decline"})
        status = self.gateway_tests.control(fixture.control, {"schema_version": 1, "op": "status"})
        listing = self.gateway_tests.control(fixture.control, {
            "schema_version": 1, "op": "interaction-list", "session_id": "thread-1"})
        exported = json.dumps([status, listing]) + state._fleet_cache_path().read_text()
        self.assertNotIn("/repo/report.py", exported)
        self.assertNotIn("PRIVATE-FILE-DIFF-SENTINEL", exported)

    def test_shared_approval_uses_gateway_reply_and_applied_evidence(self):
        fixture = self.gateway_fixture
        fixture.server.emit_approval()
        ref = self._wait(900, kind="approval", turn_id="turn-approval",
                         item_id="item-approval")
        selected = self._selected(ref)
        self.assertEqual(selected["kind"], "approval")
        result = state.submit_selected_response(
            selected, {"decision": "approve"}, action_id="integration-approval")
        self.assertEqual(result["state"], "submitted")
        native_reply = self._wait_native_response(900)[0]
        self.assertEqual(native_reply["result"], {"decision": "accept"})

        fixture.server.emit_resolved("thread-1", 900)
        fixture.client.wait_for(lambda item: item.get("method") == "serverRequest/resolved")
        received = state.read_selected_request(selected)
        self.assertEqual(received["state"], "received")
        current = fixture.server.current
        self.assertIsNotNone(current)
        current.write_json({"jsonrpc": "2.0", "method": "item/completed",
                            "params": {"threadId": "thread-1", "turnId": "turn-approval",
                                       "item": {"id": "item-approval",
                                                "type": "commandExecution",
                                                "status": "completed"}}})
        fixture.client.wait_for(lambda item: item.get("method") == "item/completed")
        applied = state.read_selected_request(selected)
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["result"]["status"], "applied")
        settled = state._selected_entry(selected)
        stale_result = dict(settled, state="received", result={"status": "received"})
        self.assertTrue(state._store_selected_entry(selected, stale_result))
        preserved = state._selected_entry(selected)
        self.assertEqual(preserved["state"], "applied")
        self.assertEqual(preserved["result"]["status"], "applied")

    def test_typed_request_ids_remain_distinct_on_real_gateway_list_detail(self):
        first = {"jsonrpc": "2.0", "id": 17, "method": "item/tool/requestUserInput",
                 "params": {"threadId": "thread-1", "turnId": "turn-int",
                            "itemId": "item-int", "questions": [
                                {"id": "int-q", "header": "Integer",
                                 "question": "Integer request?", "options": None}]}}
        second = {"jsonrpc": "2.0", "id": "17", "method": "item/tool/requestUserInput",
                  "params": {"threadId": "thread-1", "turnId": "turn-string",
                             "itemId": "item-string", "questions": [
                                 {"id": "str-q", "header": "String",
                                  "question": "String request?", "options": None}]}}
        self._server_request(first)
        self._server_request(second)
        int_ref = self._wait(17, turn_id="turn-int", item_id="item-int")
        string_ref = self._wait("17", turn_id="turn-string", item_id="item-string",
                                append=True)
        selected_int = self._selected(int_ref)
        selected_string = self._selected(string_ref)
        self.assertEqual(selected_int["request_id"], 17)
        self.assertIs(type(selected_int["request_id"]), int)
        self.assertEqual(selected_int["detail"]["question"], "Integer request?")
        self.assertEqual(selected_string["request_id"], "17")
        self.assertEqual(selected_string["detail"]["question"], "String request?")

    def test_concurrent_same_precheck_claims_one_native_reply(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": 601, "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-1", "turnId": "turn-race",
                        "itemId": "item-race", "questions": [
                            {"id": "race", "header": "Race", "question": "Choose one",
                             "options": [{"label": "Yes", "description": ""}]}]}})
        ref = self._wait(601, turn_id="turn-race", item_id="item-race")
        selected = self._selected(ref)
        ready_dir = Path(self.tmp.name) / "claim-ready"
        ready_dir.mkdir(mode=0o700)
        control_json = json.dumps(self.control)
        ref_json = json.dumps(ref)
        worker = r'''
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, os.environ["HEARTING_UTILITIES"])
import human_wait_state as state
binding = json.loads(os.environ["GATEWAY_BINDING"])
state._codex_control = lambda sid: binding if sid == "thread-1" else None
original = state._claim_selected_action
def synchronized_claim(envelope, action_id, response):
    Path(os.environ["CLAIM_READY"]).write_text("ready")
    deadline = time.monotonic() + 20
    while len(list(Path(os.environ["CLAIM_DIR"]).glob("ready-*"))) < 2:
        if time.monotonic() >= deadline:
            raise TimeoutError("both callers did not reach the same-precheck barrier")
        time.sleep(0.01)
    return original(envelope, action_id, response)
state._claim_selected_action = synchronized_claim
selected = state.read_selected_request(json.loads(os.environ["SELECTED_REF"]))
if selected.get("state") != "open":
    raise AssertionError("precheck did not resolve the same open native request")
result = state.submit_selected_response(
    selected, {"answers": {"race": ["Yes"]}}, action_id=os.environ["ACTION_ID"])
print(json.dumps(result))
'''
        env_base = dict(os.environ)
        env_base.update({"HEARTING_UTILITIES": str(Path(__file__).parent),
                         "GATEWAY_BINDING": control_json, "SELECTED_REF": ref_json,
                         "CLAIM_DIR": str(ready_dir),
                         "XDG_STATE_HOME": self.tmp.name,
                         "FLEET_INTERACTION_STATE_DIR": str(fixture.interactions),
                         "PYTHONDONTWRITEBYTECODE": "1"})
        original_reply = fixture.gateway._interaction_reply
        reply_calls = []
        reply_lock = threading.Lock()
        def counted_reply(payload):
            with reply_lock:
                reply_calls.append(payload)
            return original_reply(payload)
        fixture.gateway._interaction_reply = counted_reply
        children = []
        for action_id in ("race-action-a", "race-action-b"):
            child_env = dict(env_base, CLAIM_READY=str(ready_dir / ("ready-" + action_id)),
                             ACTION_ID=action_id)
            children.append(subprocess.Popen(
                [sys.executable, "-B", "-c", worker], env=child_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        outcomes = []
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, stderr)
            outcomes.append(json.loads(stdout.strip().splitlines()[-1]))
        native = self._wait_native_response(601)
        self.assertEqual(len(reply_calls), 1)
        self.assertEqual(len(native), 1)
        self.assertEqual(sum(item.get("state") == "submitted" for item in outcomes), 2)
        state.write_selected_draft(selected, "stale draft after action claim")
        claimed = state._selected_entry(selected)
        self.assertNotEqual(claimed.get("draft"), "stale draft after action claim")
        self.assertTrue(claimed.get("action_id"))

    def test_older_wait_can_reopen_only_while_exact_native_request_is_live(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": 701, "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-1", "turnId": "turn-old",
                        "itemId": "item-old", "questions": [
                            {"id": "old", "header": "Old", "question": "Still open?",
                             "options": None}]}})
        old_since = time.time() - 8 * 86400
        ref = self._wait(701, turn_id="turn-old", item_id="item-old",
                         stale=True, stamp=old_since)
        selected = self._selected(ref)
        self.assertEqual(selected["state"], "open")
        drafted = state.write_selected_draft(selected, "keep while still pending")
        refreshed_ref = self._wait(701, turn_id="turn-old", item_id="item-old",
                                   stale=True, stamp=old_since + 1)
        reopened = state.read_selected_request(refreshed_ref)
        self.assertEqual(reopened["state"], "open")
        self.assertEqual(reopened["draft"], "keep while still pending")

        restart_worker = r'''
import json, os, sys
sys.path.insert(0, os.environ["HEARTING_UTILITIES"])
import human_wait_state as state
state._request_producer = lambda: None
binding = json.loads(os.environ["GATEWAY_BINDING"])
state._codex_control = lambda sid: binding if sid == "thread-1" else None
value = state.read_selected_request(json.loads(os.environ["SELECTED_REF"]))
print(json.dumps({"state": value.get("state"), "draft": value.get("draft")}))
'''
        restart_env = dict(os.environ, HEARTING_UTILITIES=str(Path(__file__).parent),
                           GATEWAY_BINDING=json.dumps(self.control),
                           SELECTED_REF=json.dumps(refreshed_ref),
                           XDG_STATE_HOME=self.tmp.name,
                           FLEET_INTERACTION_STATE_DIR=str(fixture.interactions),
                           PYTHONDONTWRITEBYTECODE="1")
        restarted = subprocess.run([sys.executable, "-B", "-c", restart_worker],
                                   env=restart_env, capture_output=True, text=True, timeout=20)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(json.loads(restarted.stdout.strip()), {
            "state": "open", "draft": "keep while still pending"})
        self.assertEqual(state.cached_count("codex", "thread-1"), (0, 0))

    def test_exact_no_turn_native_detail_stays_read_only_fallback(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": "mcp-no-turn",
            "method": "mcpServer/elicitation/request",
            "params": {"threadId": "thread-1", "turnId": None,
                        "serverName": "fixture", "mode": "url",
                        "message": "Open the sign-in page?",
                        "url": "https://example.invalid/login", "elicitationId": "elicit-1"}})
        ref = self._wait("mcp-no-turn", kind="elicitation", turn_id=None, item_id=None)
        selected = state.read_selected_request(ref)
        self.assertEqual(selected["state"], "open")
        self.assertIsNone(selected["turn_id"])
        self.assertFalse(selected["support"]["supported"])
        self.assertEqual(selected["support"]["reason"],
                         "codex-native-turn-binding-unavailable")
        self.assertEqual(selected["detail"]["message"], "Open the sign-in page?")
        drafted = state.write_selected_draft(selected, "preserve this fallback draft")
        self.assertEqual(drafted["draft"], "preserve this fallback draft")
        refused = state.submit_selected_response(
            drafted, {"decision": "approve"}, action_id="no-turn-approval")
        self.assertFalse(refused["support"]["supported"])
        self.assertEqual(fixture.gateway._interaction_actions, {})

    def test_retired_request_requery_reports_gateway_epoch_expiry(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": 801, "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-1", "turnId": "turn-expire",
                        "itemId": "item-expire", "questions": [
                            {"id": "expire", "header": "Expire", "question": "Still live?",
                             "options": None}]}})
        ref = self._wait(801, turn_id="turn-expire", item_id="item-expire")
        selected = self._selected(ref)
        fixture.client.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = self.gateway_tests.control(fixture.control, {
                "schema_version": 1, "op": "status"})
            if status.get("status") == "disconnected":
                break
            time.sleep(0.01)
        self.assertEqual(status.get("status"), "disconnected")
        fixture.client = fixture._connect_client()
        self.control = self._control_binding()
        expired = state.read_selected_request(selected)
        self.assertEqual(expired["state"], "expired")
        self.assertEqual(expired["support"]["reason"], "codex-native-request-expired")


class WorkflowSelectedResponseTest(unittest.TestCase):
    """Exercise selected S2/S3/S4 records against isolated workflow sources."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        agent_home = self.root / "agent-home"
        (agent_home / "core").mkdir(parents=True)
        (agent_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "xdg"),
            "HOME": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AGENT_HOME": str(agent_home),
            "AGENT_WORKFLOW_ROOT": str(self.root / "workflow"),
            "AGENT_ARTIFACT_CHECKPOINT": "off",
            "AGENT_ARTIFACT_CAMPAIGN_ID": "",
            "AGENT_ARTIFACT_CYCLE_ID": "",
            "AGENT_ARTIFACT_PRODUCER_ID": "",
            "AGENT_ARTIFACT_CYCLE_DIR": "",
            "AGENT_ARTIFACT_OUTPUT_DIR": "",
            "AGENT_DISPATCH_CALLER_HARNESS": "codex",
            "AGENT_DISPATCH_REGISTERED_WORKER": "",
            "AGENT_DISPATCH_CHILD": "",
            "AGENT_DISPATCH_DEPTH": "0",
            "AGENT_DISPATCH_ATTEMPT_ID": "",
            "CODEX_THREAD_ID": "parent-session",
            "CODEX_SESSION_ID": "",
            "CLAUDE_CODE_SESSION_ID": "",
            "CLAUDE_SESSION_ID": "",
            "OPENCODE_SESSION_ID": "",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)

        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import dispatch
        self.dispatch = dispatch
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        registries = mock.patch.object(dispatch, "human_gate_registry_paths",
                                       return_value=(self.jobs,))
        registries.start()
        self.addCleanup(registries.stop)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.route_file = self.root / "owner.route.json"
        self.route = {"artifact_root": str(self.artifacts), "nodes": []}
        self._seal_route()

    def _seal_route(self):
        from route_identity import route_hash, route_id_from_hash
        self.route.pop("route_hash", None)
        self.route.pop("route_id", None)
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")

    def _metadata(self, attempt="att-workflow-owner"):
        from dispatch_contract import supervisor_lease_path
        lease = supervisor_lease_path(self.jobs, attempt)
        return ",".join((
            "attempt_schema_version=2", "dispatch_depth=1", "worker_type=owner",
            "transport=headless", "execution_surface=registered-headless",
            "registered_worker=1", "completion_delivery=app-server-supervised",
            "harness=codex", "attempt_id=" + attempt,
            "supervisor_lease=flock-v1", "supervisor_lease_nonce=" + "d" * 64,
            "supervisor_lease_file=" + str(lease), "parent_sid=parent-session",
            "parent_harness=codex", "route_id=" + self.route["route_id"],
            "route_hash=" + self.route["route_hash"], "route_file=" + str(self.route_file),
            "owner_route_id=" + self.route["route_id"],
            "owner_route_hash=" + self.route["route_hash"],
            "owner_route_file=" + str(self.route_file),
        ))

    def _write_owner(self, status, *, attempt="att-workflow-owner", suffix=""):
        row = (f"{time.time():.6f}\t{status}\t/repo\t/wt\towner\t"
               + self._metadata(attempt) + suffix + "\n")
        self.jobs.write_text(row, encoding="utf-8")

    def _observe(self):
        import human_gate_observation
        result = human_gate_observation.read_answer_waits(jobs_paths=(self.jobs,))
        self.assertTrue(result["complete"], result["health"])
        self.assertTrue(state.publish_cache(result))
        return result

    def _blocked_owner_wait(self):
        import dispatch_owner_input
        attempt = "att-workflow-owner"
        self._write_owner("open", attempt=attempt)
        dispatch_owner_input.initialize_owner_input(self.jobs, attempt, "codex-next-turn")
        suffix = (",note=dead-worker-blocked,failure_class=blocked")
        self._write_owner("done", attempt=attempt, suffix=suffix)
        result = self._observe()
        item = next(row for row in result["items"] if row["source"] == "S3")
        return item

    def _human_gate_wait(self):
        self.route["human_gate_bindings"] = [{"gate": "frame-review", "node": "owner"}]
        self._seal_route()
        self._write_owner("open", suffix="")
        import workflow_state
        ledger = workflow_state.WorkflowLedger(self.route["route_id"],
                                                self.route["route_hash"], jobs=self.jobs)
        ledger.root.mkdir(parents=True, exist_ok=True)
        ledger.journal_path.write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"],
            "workflow_state": "BLOCKED_HUMAN_GATE",
            "evidence": {"gate": "frame-review"},
        }) + "\n", encoding="utf-8")
        result = self._observe()
        item = next(row for row in result["items"] if row["source"] == "S2")
        return item, ledger.journal_path

    def test_s3_explicit_correction_uses_public_owner_input_once(self):
        import dispatch_owner_input
        item = self._blocked_owner_wait()
        selected = state.read_selected_request({"id": item["id"]})
        self.assertEqual(selected["state"], "open")
        self.assertTrue(selected["support"]["supported"])
        self.assertEqual(selected["native_ref"]["transport"], "hearting-owner-input")
        self.assertIsNone(selected["turn_id"])
        self.assertEqual(selected["item_id"], "att-workflow-owner")
        self.assertEqual(selected["route_id"], self.route["route_id"])
        self.assertEqual(state._read_cache()["items"][0]["route_hash"],
                         self.route["route_hash"])

        drafted = state.write_selected_draft(selected, "preserve the approved route and retry")
        self.assertEqual(drafted["draft"], "preserve the approved route and retry")
        empty = state.submit_selected_response(
            drafted, {"answers": {"owner-correction": [""]}}, action_id="empty-correction")
        self.assertEqual(empty["state"], "open")
        self.assertEqual(dispatch_owner_input.inspect(
            self.jobs, "att-workflow-owner")["requests"], [])

        original = state._claim_selected_action
        barrier = threading.Barrier(2)

        def simultaneous_claim(*args):
            barrier.wait(timeout=5)
            return original(*args)

        with mock.patch.object(state, "_claim_selected_action", side_effect=simultaneous_claim), \
             mock.patch.object(state.subprocess, "run",
                               wraps=state.subprocess.run) as public_correct:
            results = []
            failures = []

            def send(action_id):
                try:
                    results.append(state.submit_selected_response(
                        drafted, {"answers": {"owner-correction": [drafted["draft"]]}},
                        action_id=action_id))
                except Exception as exc:
                    failures.append(exc)

            callers = [threading.Thread(target=send, args=(action,))
                       for action in ("owner-action-a", "owner-action-b")]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(timeout=20)
            self.assertTrue(all(not caller.is_alive() for caller in callers))
            self.assertEqual(failures, [])
            self.assertEqual(public_correct.call_count, 1)

        receipt = dispatch_owner_input.inspect(self.jobs, "att-workflow-owner")
        self.assertEqual(len(receipt["requests"]), 1)
        request = receipt["requests"][0]
        self.assertEqual(request["state"], "retained")
        self.assertNotIn(drafted["draft"], json.dumps(receipt))
        self.assertEqual(dispatch_owner_input.retained(self.jobs, "att-workflow-owner")[0]["text"],
                         drafted["draft"])
        current = state.read_selected_request(selected)
        self.assertEqual(current["state"], "received")
        self.assertEqual(current["result"]["status"], "retained")
        self.assertEqual(self._observe()["total"], 0)
        self.assertNotIn(drafted["draft"], state._fleet_cache_path().read_text(encoding="utf-8"))

    def test_s3_consumed_receipt_preserves_its_actual_source_and_request(self):
        import dispatch_owner_input

        item = self._blocked_owner_wait()
        selected = state.read_selected_request({"id": item["id"]})
        received = state.submit_selected_response(
            selected, {"answers": {"owner-correction": ["continue the approved work"]}},
            action_id="owner-consumed")
        self.assertEqual(received["state"], "received")
        with mock.patch.object(dispatch_owner_input.OwnerInput, "notify_unresolved"):
            consumer = dispatch_owner_input.OwnerInput(
                self.jobs, item["attempt_id"], "actual-owner-thread", "codex-next-turn", lambda _: None)
        consumer.record(["owner-consumed"], "accepted", turn_id="actual-owner-turn")
        consumer.completed("actual-owner-turn")
        applied = state.read_selected_request(selected)
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["result"], {
            "source": "hearting-owner-input", "status": "turn-completed",
            "request_id": "owner-consumed", "delivery_observation": "turn-completed",
            "target_attempt_id": item["attempt_id"],
            "continuation": received["result"]["continuation"]})
        self.assertEqual(applied["draft"], "")

    def test_s3_replaced_owner_route_or_parent_cannot_receive_old_answer(self):
        import dispatch_owner_input
        item = self._blocked_owner_wait()
        selected = state.read_selected_request({"id": item["id"]})
        drafted = state.write_selected_draft(selected, "do not retarget this correction")
        changed = self.jobs.read_text(encoding="utf-8").replace(
            "parent_sid=parent-session", "parent_sid=replacement-session")
        self.jobs.write_text(changed, encoding="utf-8")
        refused = state.submit_selected_response(
            drafted, {"answers": {"owner-correction": [drafted["draft"]]}},
            action_id="stale-owner-action")
        self.assertFalse(refused["support"]["supported"])
        self.assertEqual(refused["state"], "unknown")
        with self.assertRaisesRegex(dispatch_owner_input.InputError, "target-changed"):
            dispatch_owner_input.inspect(self.jobs, "att-workflow-owner")
        value = json.loads(dispatch_owner_input._path(
            self.jobs, "att-workflow-owner").read_text(encoding="utf-8"))
        self.assertEqual(value["requests"], [])

    def test_s2_detail_is_exact_and_release_stays_disabled_without_raise_binding(self):
        item, journal = self._human_gate_wait()
        before = journal.read_bytes()
        selected = state.read_selected_request({"id": item["id"]})
        self.assertEqual(selected["route_id"], self.route["route_id"])
        self.assertEqual(selected["native_ref"]["transport"], "hearting-human-gate")
        self.assertIsNone(selected["turn_id"])
        self.assertFalse(selected["support"]["supported"])
        self.assertEqual(selected["support"]["reason"],
                         "workflow-gate-artifact-unavailable")
        refused = state.submit_selected_response(
            selected, {"answers": {"frame-review": ["proceed"]}},
            action_id="must-not-release")
        self.assertFalse(refused["support"]["supported"])
        self.assertEqual(journal.read_bytes(), before)

    def test_s4_marker_detail_and_draft_never_fabricate_a_reply(self):
        import human_gate_observation
        text = "[답변 필요] select the next step"
        self.assertTrue(state.open_signal("codex", "marker-session", "marker-seat", text,
                                           sequence=7))
        result = human_gate_observation.read_answer_waits(jobs_paths=(self.jobs,))
        self.assertTrue(state.publish_cache(result))
        item = next(row for row in result["items"] if row["source"] == "S4")
        selected = state.read_selected_request({"id": item["id"]})
        self.assertEqual(selected["detail"]["question"], "select the next step")
        self.assertIsNone(selected["turn_id"])
        self.assertFalse(selected["support"]["supported"])
        self.assertEqual(selected["support"]["reason"],
                         "marker-has-no-bound-native-response-surface")
        drafted = state.write_selected_draft(selected, "keep this private for a native reply")
        refused = state.submit_selected_response(
            drafted, {"answers": {"marker-response": [drafted["draft"]]}},
            action_id="marker-reply")
        self.assertFalse(refused["support"]["supported"])
        self.assertTrue(state.signal_is_current({"id": item["id"], "seat_key": "marker-seat"}))
        self.assertNotIn("select the next step",
                         state._cache_path().read_text(encoding="utf-8"))
        self.assertNotIn("select the next step",
                         state._fleet_cache_path().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
