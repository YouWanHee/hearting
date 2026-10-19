"""Focused safety regressions for isolated native Codex replay setup."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from unittest import mock

import codex_parent_completion_replay as replay


class HookTrustSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="parent-replay-hook-test-")
        self.root = Path(self.temporary.name)
        self.source_home = self.root / "source-home"
        self.codex_home = self.root / "private-home"
        self.worktree = self.root / "workspace"
        self.source_home.mkdir(mode=0o700)
        self.codex_home.mkdir(mode=0o700)
        self.worktree.mkdir(mode=0o700)
        (self.source_home / "auth.json").write_text("{}\n", encoding="utf-8")
        (self.source_home / "hooks.json").write_text(
            '{"hooks":{"SessionStart":[{"hooks":[{"command":"user-hook"}]}]}}\n',
            encoding="utf-8")
        (self.source_home / "config.toml").write_text(
            '[hooks.state."outside:user-hook"]\ntrusted_hash="sha256:personal"\n',
            encoding="utf-8")
        copied, _auth_source = replay._copy_runtime_config(
            self.source_home, self.codex_home, {})
        self.assertTrue(copied["hooks.json"])
        self.hooks_path = self.codex_home / "hooks.json"
        self.config_path = self.codex_home / "config.toml"

    def tearDown(self):
        self.temporary.cleanup()

    def test_generated_hooks_use_exact_native_hashes_and_private_config(self):
        client = FakeHookTrustClient(self.hooks_path, self.worktree, self.config_path)
        source_bytes = self.hooks_path.read_bytes()
        result = replay._trust_isolated_hooks(
            client, self.codex_home, self.worktree, self.hooks_path)

        generated = json.loads(source_bytes)
        self.assertEqual(set(generated["hooks"]), {"SessionStart", "UserPromptSubmit"})
        self.assertNotIn("user-hook", source_bytes.decode("utf-8"))
        self.assertEqual(result["hooks_json_sha256"], hashlib.sha256(source_bytes).hexdigest())
        self.assertEqual(result["trusted_hook_count"], 2)
        state = tomllib.loads(self.config_path.read_text(encoding="utf-8"))["hooks"]["state"]
        self.assertEqual(set(state), set(client.current_hashes))
        self.assertEqual({key: value["trusted_hash"] for key, value in state.items()},
                         client.current_hashes)
        self.assertTrue(all(key.startswith(str(self.hooks_path) + ":") for key in state))
        self.assertEqual(self.config_path.stat().st_uid, os.geteuid())
        self.assertEqual(self.hooks_path.stat().st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.hooks_path.stat().st_mode), 0o600)
        self.assertEqual(client.list_calls, 2)
        self.assertEqual(client.trust_writes, 1)

    def test_hook_source_change_during_trust_is_rejected(self):
        client = FakeHookTrustClient(self.hooks_path, self.worktree, self.config_path)
        client.mutate_source_on_first_list = True
        with self.assertRaisesRegex(replay.ReplayBlocked, "source-changed-during-trust"):
            replay._trust_isolated_hooks(
                client, self.codex_home, self.worktree, self.hooks_path)

    def test_trusted_thread_starts_after_hash_write_and_probe_unsubscribe(self):
        client = FakeHookTrustClient(self.hooks_path, self.worktree, self.config_path)
        client.thread_ids = iter(("probe-thread", "measured-thread"))

        thread_id, probe_id, trust = replay._start_trusted_replay_thread(
            client, self.codex_home, self.worktree, self.hooks_path)

        self.assertEqual(thread_id, "measured-thread")
        self.assertEqual(probe_id, "probe-thread")
        self.assertEqual(trust["trusted_hook_count"], 2)
        self.assertEqual(client.calls, [
            "thread/start", "hooks/list", "config/batchWrite", "hooks/list",
            "thread/unsubscribe", "thread/start",
        ])
        self.assertTrue(client.trusted)


class FakeHookTrustClient:
    def __init__(self, hooks_path: Path, worktree: Path, config_path: Path):
        self.hooks_path = hooks_path
        self.worktree = worktree
        self.config_path = config_path
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
        source_digest = hashlib.sha256(hooks_path.read_bytes()).hexdigest()
        self.current_hashes = {}
        self.events = {}
        for event, groups in document["hooks"].items():
            for group_index, group in enumerate(groups):
                for hook_index, _hook in enumerate(group["hooks"]):
                    key = f"{hooks_path}:{event.lower()}:{group_index}:{hook_index}"
                    digest = "sha256:" + hashlib.sha256(
                        f"{source_digest}:{event}:{group_index}:{hook_index}".encode()).hexdigest()
                    self.current_hashes[key] = digest
                    self.events[key] = event
        self.list_calls = 0
        self.trust_writes = 0
        self.trusted = False
        self.mutate_source_on_first_list = False
        self.calls = []

    def request(self, method: str, params: dict) -> dict:
        self.calls.append(method)
        if method == "thread/start":
            return {"thread": {"id": next(self.thread_ids)}}
        if method == "thread/unsubscribe":
            return {"status": "ok"}
        if method == "hooks/list":
            self.list_calls += 1
            hooks = [{
                "key": key,
                "currentHash": current_hash,
                "eventName": self.events[key],
                "enabled": True,
                "sourcePath": str(self.hooks_path),
                "trustStatus": "trusted" if self.trusted else "untrusted",
            } for key, current_hash in self.current_hashes.items()]
            response = {"data": [{"cwd": str(self.worktree), "hooks": hooks}]}
            if self.mutate_source_on_first_list and self.list_calls == 1:
                self.hooks_path.write_bytes(self.hooks_path.read_bytes() + b"\n")
            return response
        if method == "config/batchWrite":
            self.trust_writes += 1
            self.assert_private_write(params)
            if any(edit.get("keyPath") != "hooks.state"
                   or edit.get("mergeStrategy") != "upsert"
                   for edit in params["edits"]):
                raise AssertionError("trust write did not use hooks.state upsert")
            with self.config_path.open("a", encoding="utf-8") as stream:
                for edit in params["edits"]:
                    values = edit["value"]
                    for key, value in values.items():
                        stream.write("\n[hooks.state." + json.dumps(key) + "]\n")
                        stream.write("trusted_hash = " + json.dumps(value["trusted_hash"]) + "\n")
            self.trusted = True
            return {"status": "ok"}
        raise AssertionError(f"unexpected App Server method: {method}")

    def assert_private_write(self, params: dict) -> None:
        if (params.get("filePath") != str(self.config_path)
                or params.get("reloadUserConfig") is not True):
            raise AssertionError("trust write did not target the isolated Codex config")
        keys = {key for edit in params.get("edits", [])
                for key in edit.get("value", {})}
        if keys != set(self.current_hashes):
            raise AssertionError("trust write did not match the exact isolated hook source")


class EndpointSymlinkTests(unittest.TestCase):
    def test_start_server_accepts_owned_codex_short_path_socket_symlink(self):
        with tempfile.TemporaryDirectory(prefix="parent-replay-endpoint-test-") as temporary:
            root = Path(temporary)
            codex_home = root / "home" / ".codex"
            endpoint = codex_home / "app-server-control" / "app-server-control.sock"
            worktree = root / "workspace"
            worktree.mkdir(mode=0o700)
            target_dir = root / "daemon"
            target_dir.mkdir(mode=0o700)
            target = target_dir / "server.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(target))

            class Process:
                def poll(self):
                    return None

            process = Process()

            def spawn(*_args, **_kwargs):
                endpoint.symlink_to(target)
                return process

            stderr_path = root / "server.stderr"
            with mock.patch.object(replay.subprocess, "Popen", side_effect=spawn), \
                    mock.patch.object(replay.codex_queue_delivery, "resolve_endpoint",
                                      wraps=replay.codex_queue_delivery.resolve_endpoint) as resolve:
                started = replay._start_server(
                    ["codex", "app-server"], endpoint, worktree, {}, stderr_path)
            try:
                self.assertIs(started, process)
                self.assertTrue(endpoint.is_symlink())
                self.assertEqual(replay.codex_queue_delivery.resolve_endpoint(endpoint), target)
                resolve.assert_called_once_with(endpoint)
            finally:
                process._replay_stderr_file.close()
                listener.close()


class NativeAcceptanceFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="parent-replay-acceptance-test-")
        self.root = Path(self.temporary.name)
        self.worktree = self.root / "workspace"
        self.worktree.mkdir(mode=0o700)
        self.sentinel = "HEARTING_REPLAY_FIXTURE_ONCE"
        self.fixture = replay._write_native_fixture(self.worktree, self.sentinel)

    def tearDown(self):
        self.temporary.cleanup()

    def _valid_tool_trace(self):
        fixture_rel = Path(".agent_reports/native-parent-fixture")
        read_command = f"cat {fixture_rel}/*.md {fixture_rel}/fixture-facts.json"
        read_output = subprocess.run(["/bin/sh", "-lc", read_command],
                                     cwd=self.worktree, capture_output=True, text=True, check=False)
        self.assertEqual(read_output.returncode, 0, read_output.stderr)
        action_command = f"python3 -B {fixture_rel}/apply_fixture_followthrough.py {self.sentinel}"
        applied = subprocess.run(
            ["/bin/sh", "-lc", action_command], cwd=self.worktree,
            capture_output=True, text=True, check=False)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        verifier_command = (f"python3 -B {fixture_rel}/verify_fixture.py; "
                            f"cat {fixture_rel}/follow-through.json")
        verified = subprocess.run(["/bin/sh", "-lc", verifier_command],
                                  cwd=self.worktree, capture_output=True, text=True, check=False)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        def tool(tool_id, command, output, returncode=0):
            return {
                "id": tool_id,
                "command": "/usr/bin/zsh -lc " + json.dumps(command),
                "commandActions": [{"command": command, "type": "unknown"}],
                "cwd": str(self.worktree),
                "aggregatedOutput": output,
                "status": "completed" if returncode == 0 else "failed",
                "exitCode": returncode,
            }
        read = tool("read-1", read_command, read_output.stdout)
        verifier = tool("verify-1", verifier_command, verified.stdout)
        action = tool("action-1", action_command, applied.stdout)
        return [read, action, verifier]

    def test_turn_items_keep_native_cwd_and_command_action_metadata(self):
        command = "cat .agent_reports/native-parent-fixture/*.md"
        messages, tools = replay._turn_items({"items": [
            {"type": "agentMessage", "text": "done"},
            {"type": "commandExecution", "id": "native-1", "command": "/usr/bin/zsh -lc 'pwd'",
             "commandActions": [{"type": "unknown", "command": command}],
             "cwd": str(self.worktree), "aggregatedOutput": "", "exitCode": 0,
             "status": "completed"},
        ]})
        self.assertEqual(messages, ["done"])
        self.assertEqual(tools[0]["cwd"], str(self.worktree))
        self.assertEqual(tools[0]["commandActions"][0]["command"], command)

    def test_relative_glob_and_recorded_cwd_prove_each_exact_artifact_read(self):
        tools = self._valid_tool_trace()
        facts = self.fixture["facts"]["report"]
        prose = ("The reason was the approved fixture follow-through. Before it, the file was absent "
                 "with action count zero; after it, the file was present with action count one. "
                 "Verification passed all six checks. Commit, deployment, and serving details are "
                 "synthetic; production checks were skipped and were not measured or changed.")
        checks = replay._first_report_checks(
            prose + "\nFACTUAL_REPORT_JSON:\n" + json.dumps(facts), tools, self.fixture)
        self.assertTrue(checks["every_exact_artifact_was_read"])
        self.assertTrue(all(checks["artifact_reads"].values()))
        self.assertTrue(all(evidence[0]["cwd_source"] == "commandExecution"
                            for evidence in checks["artifact_read_evidence"].values()))

        wrong_cwd = [{**item, "cwd": str(self.root)} for item in tools]
        checks = replay._first_report_checks(
            prose + "\nFACTUAL_REPORT_JSON:\n" + json.dumps(facts), wrong_cwd, self.fixture)
        self.assertFalse(checks["every_exact_artifact_was_read"])
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])
        self.assertFalse(checks["one_approved_followthrough_tool_invocation"])

    def test_marker_only_and_failed_verifier_do_not_satisfy_execution_evidence(self):
        fake = {
            "id": "fake-1", "command": "/usr/bin/zsh -lc 'echo FIXTURE_VERIFICATION=PASS checks=6 failed=0'",
            "commandActions": [{"type": "unknown",
                                "command": "echo FIXTURE_VERIFICATION=PASS checks=6 failed=0"}],
            "cwd": str(self.worktree),
            "aggregatedOutput": "FIXTURE_ID=" + self.fixture["facts"]["fixture_id"]
                              + "\nFIXTURE_VERIFICATION=PASS checks=6 failed=0",
            "status": "completed", "exitCode": 0,
        }
        checks = replay._first_report_checks("", [fake], self.fixture)
        self.assertFalse(checks["every_exact_artifact_was_read"])
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])

        code_argument = {
            **fake,
            "commandActions": [{
                "type": "unknown",
                "command": ("python3 -c 'print(1)' "
                            ".agent_reports/native-parent-fixture/verify_fixture.py"),
            }],
            "aggregatedOutput": "FIXTURE_VERIFICATION=PASS checks=6 failed=0",
        }
        checks = replay._first_report_checks("", [code_argument], self.fixture)
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])

        tools = self._valid_tool_trace()
        verifier = next(item for item in tools if item["id"] == "verify-1")
        verifier["status"] = "failed"
        verifier["exitCode"] = 1
        checks = replay._first_report_checks("", tools, self.fixture)
        self.assertFalse(checks["fixture_verifier_passed_in_native_tool_trace"])

    def test_first_report_failure_is_durable_and_later_cases_still_run(self):
        output = self.root / "evidence"
        output.mkdir(mode=0o700)
        report = {"schema_version": 1, "replay_cases": {}}
        d1 = replay._record_case(report, output, "d1_first_report",
                                 {"exact_facts": False, "native_action": True},
                                 evidence=["first-report-checks.json"])
        self.assertEqual(d1["status"], "FAIL")
        self.assertTrue(replay._case_can_continue(d1))
        self.assertEqual(json.loads((output / "replay.json").read_text())[
            "replay_cases"]["d1_first_report"]["checks"]["exact_facts"], False)

        evaluated = []
        prior = d1
        for case_id, checks in (
                ("d2_duplicate_cleanup", {"cleanup_suppressed": False}),
                ("d3_new_conflict", {"evaluated": True}),
                ("direct_question", {"evaluated": True})):
            if not replay._case_can_continue(prior):
                break
            evaluated.append(case_id)
            prior = replay._record_case(report, output, case_id, checks)
        stored = json.loads((output / "replay.json").read_text())
        self.assertEqual(evaluated, ["d2_duplicate_cleanup", "d3_new_conflict", "direct_question"])
        self.assertEqual(stored["replay_cases"]["d1_first_report"]["status"], "FAIL")
        self.assertEqual(stored["replay_cases"]["d2_duplicate_cleanup"]["status"], "FAIL")
        self.assertEqual(stored["replay_cases"]["d3_new_conflict"]["status"], "PASS")
        self.assertEqual(stored["replay_cases"]["direct_question"]["status"], "PASS")

    def test_later_runtime_block_preserves_observed_failure_and_names_unrun_cases(self):
        output = self.root / "failure-evidence"
        output.mkdir(mode=0o700)
        report = {"schema_version": 1, "replay_cases": {}}
        replay._record_case(report, output, "d1_first_report", {"exact_facts": False})
        finished = replay._finish_after_runtime_error(report, output, "BLOCKED", "queue unavailable")
        self.assertEqual(finished["verdict"], "FAIL")
        self.assertEqual(finished["replay_cases"]["d1_first_report"]["status"], "FAIL")
        self.assertEqual(finished["replay_cases"]["d2_duplicate_cleanup"]["status"], "not-run")
        self.assertIn("queue unavailable",
                      finished["replay_cases"]["d2_duplicate_cleanup"]["reason"])

    def test_ordered_synthetic_facts_and_exact_native_report_are_required(self):
        facts = self.fixture["facts"]
        self.assertEqual([item["result"] for item in facts["events"]],
                         ["FAIL", "FIXED", "PASS", "SUCCESS", "PASS", "COMPLETE"])
        self.assertTrue(facts["synthetic_values"]["commit"].startswith("synthetic-only:"))
        self.assertTrue(facts["synthetic_values"]["deployment"].startswith("synthetic-only:"))
        self.assertIn("not probed", facts["synthetic_values"]["serving"])
        self.assertEqual(facts["report"]["before"]["approved_action_count"], 0)
        self.assertEqual(facts["report"]["after"]["approved_action_count"], 1)
        self.assertTrue(facts["report"]["skipped"])
        self.assertTrue(facts["report"]["limits"])

        tools = self._valid_tool_trace()
        text = ("The reason was the approved fixture follow-through. Before it, the file was absent "
                "with action count zero; after it, the file was present with action count one. "
                "Verification passed all six checks. Commit, deployment, and serving details are "
                "synthetic; production checks were skipped and were not measured or changed.\n"
                "FACTUAL_REPORT_JSON:\n"
                + json.dumps(facts["report"], ensure_ascii=False))
        checks = replay._first_report_checks(text, tools, self.fixture)
        for key in ("report_matches_fixture_facts", "report_has_substantive_closing_prose",
                    "every_exact_artifact_was_read", "fixture_verifier_passed_in_native_tool_trace",
                    "one_approved_followthrough_tool_invocation",
                    "followthrough_state_matches_one_time_fixture_action"):
            self.assertTrue(checks[key], key)
        self.assertTrue(all(checks["artifact_reads"].values()))
        self.assertTrue(all(checks["prose_content_checks"].values()))
        self.assertEqual(len(checks["artifact_read_evidence"]), 7)
        self.assertTrue(checks["verifier_script_unchanged"])
        self.assertTrue(checks["followthrough_script_unchanged"])

    def test_keyword_presence_cannot_substitute_for_factual_values_or_action_trace(self):
        tools = self._valid_tool_trace()
        facts = self.fixture["facts"]
        wrong = dict(facts["report"])
        wrong["synthetic_values"] = dict(wrong["synthetic_values"], deployment="production deployment succeeded")
        text = ("The fixture deployment passed verification and the synthetic service is serving.\n"
                "FACTUAL_REPORT_JSON:\n" + json.dumps(wrong))
        checks = replay._first_report_checks(text, tools, self.fixture)
        self.assertFalse(checks["report_matches_fixture_facts"])

        action = next(item for item in tools if item["id"] == "action-1")
        duplicate = {**action, "id": "action-duplicate"}
        checks = replay._first_report_checks(
            "Substantive fixture completion report.\nFACTUAL_REPORT_JSON:\n"
            + json.dumps(facts["report"]), [*tools, duplicate], self.fixture)
        self.assertFalse(checks["one_approved_followthrough_tool_invocation"])

        missing_read = list(tools)
        read_item = dict(next(item for item in tools if item["id"] == "read-1"))
        missing_path = self.fixture["artifact_paths"][0]
        remaining = [path for path in [self.fixture["facts_path"], *self.fixture["artifact_paths"]]
                     if path != missing_path]
        relative = [path.relative_to(self.worktree) for path in remaining]
        read_command = "cat " + " ".join(path.as_posix() for path in relative)
        read_item["commandActions"] = [{"command": read_command, "type": "unknown"}]
        missing_read[missing_read.index(next(item for item in missing_read if item["id"] == "read-1"))] = read_item
        checks = replay._first_report_checks(
            "Substantive fixture completion report.\nFACTUAL_REPORT_JSON:\n"
            + json.dumps(facts["report"]), missing_read, self.fixture)
        self.assertFalse(checks["every_exact_artifact_was_read"])

    def test_fixture_followthrough_script_rejects_a_second_action(self):
        first = subprocess.run([sys.executable, "-B", str(self.fixture["action"]), self.sentinel],
                               capture_output=True, text=True, check=False)
        second = subprocess.run([sys.executable, "-B", str(self.fixture["action"]), self.sentinel],
                                capture_output=True, text=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(second.returncode, 0)
        state = json.loads(self.fixture["followthrough"].read_text(encoding="utf-8"))
        self.assertEqual(state["approved_action_count"], 1)
        self.assertEqual(state["sentinel"], self.sentinel)

    def test_old_ack_cleanup_repeat_and_new_conflict_revision_remain_distinct(self):
        import dispatch_pending_delivery as pending
        import dispatch_supervision as supervision

        jobs = self.root / "jobs.log"
        attempt, route_id, receipt, completion, metadata = replay._fixture_registry(
            jobs, "idle-parent", self.worktree, self.sentinel)
        pending.claim(self.root, "idle-parent", completion["delivery_id"],
                      claim_owner="fixture-consumer", lease_seconds=30,
                      require_generation_proof=False, jobs=jobs)
        acked = pending.ack(self.root, "idle-parent", completion["delivery_id"],
                            acked_by="codex-native-prompt:idle-parent")
        old_ack = pending.received_ack_identity(acked, "idle-parent", completion["delivery_id"])

        cleanup = supervision.materialize(jobs, {attempt}, reason="join-deadline")[0]
        self.assertFalse(supervision.notice_is_current(cleanup))
        self.assertIn("retains cleanup observation only",
                      supervision.render_text(cleanup["receipt"]))
        spec = importlib.util.spec_from_file_location(
            "codex_parent_replay_courier_test", replay.UTILITIES / "codex-managed-completion.py")
        courier = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = courier
        spec.loader.exec_module(courier)
        watcher_args = SimpleNamespace(
            jobs=jobs.resolve(), parent_session_id="idle-parent",
            sealed_batch_id=metadata["managed_sealed_batch_id"],
            interval=0.05, queue_socket=Path("/isolated-native-queue.sock"),
        )
        watcher = courier.NoticeWatcher(watcher_args, {attempt}, capability=None)
        cleanup_path = pending.record_path(self.root, "idle-parent", cleanup["delivery_id"])
        with mock.patch.object(courier.codex_queue_delivery, "send_at_least_once") as cleanup_send, \
             mock.patch.object(courier.codex_queue_delivery, "list_queue") as cleanup_list, \
             mock.patch.object(courier.codex_queue_delivery, "find_turn_by_client_message_id") as cleanup_find:
            watcher._one(cleanup_path)
        cleanup_send.assert_not_called()
        cleanup_list.assert_not_called()
        cleanup_find.assert_not_called()
        rejected_cleanup = pending.read(self.root, "idle-parent", cleanup["delivery_id"])
        self.assertEqual(rejected_cleanup["state"], "rejected")
        self.assertEqual(rejected_cleanup["expiry_reason"], "supervision-resolved")
        old_after_cleanup = pending.read(self.root, "idle-parent", completion["delivery_id"])
        self.assertEqual(pending.received_ack_identity(
            old_after_cleanup, "idle-parent", completion["delivery_id"]), old_ack)
        duplicate = pending.create(
            self.root, recipient_kind="codex-native-queue", recipient_key="idle-parent",
            delivery_id=completion["delivery_id"], session_generation="",
            session_generation_supported="0", attempt_ids=[attempt], parent_attempt_id="-",
            route_id=route_id, route_node="_owner", receipt=receipt,
            receipt_digest=completion["receipt_digest"],
            row_revisions={attempt: metadata["delivery_row_revision"]})
        self.assertEqual(duplicate["state"], "acked")
        self.assertEqual(pending.received_ack_identity(
            pending.read(self.root, "idle-parent", completion["delivery_id"]),
            "idle-parent", completion["delivery_id"]), old_ack)

        conflict_metadata = {
            **metadata, "cleanup_state": "unknown", "terminal_conflict": "1",
            "conflicting_terminal_note": "synthetic-late-failure",
            "conflicting_failure_class": "fail",
            "conflicting_classifier_source": "native-replay-late-fixture",
        }
        jobs.write_text(
            "2026-10-19T00:00:00Z\tdone\t" + str(self.worktree) + "\t" + str(self.worktree)
            + "\tisolated-parent-replay\t"
            + ",".join(f"{key}={value}" for key, value in conflict_metadata.items()) + "\n",
            encoding="utf-8")
        late = supervision.materialize(jobs, {attempt}, reason="terminal-evidence-conflict")[0]
        self.assertNotEqual(late["delivery_id"], cleanup["delivery_id"])
        self.assertNotEqual(late["delivery_id"], completion["delivery_id"])
        self.assertTrue(late["receipt"]["obligation_revision"])
        self.assertTrue(supervision.notice_is_current(late))
        self.assertNotEqual(late["delivery_id"], cleanup["delivery_id"])
        self.assertNotEqual(late["delivery_id"], completion["delivery_id"])
        self.assertEqual(pending.read(self.root, "idle-parent", cleanup["delivery_id"])["expiry_reason"],
                         "supervision-resolved")
        native_turn = {"id": "isolated-native-turn-1", "status": "completed"}
        late_path = pending.record_path(self.root, "idle-parent", late["delivery_id"])
        with mock.patch.object(courier.codex_queue_delivery, "send_at_least_once", return_value={
                 "status": "queued", "already_pending": False,
                 "queued_submission_id": "isolated-native-turn-1"}) as late_send, \
             mock.patch.object(courier.codex_queue_delivery, "list_queue", return_value=[]), \
             mock.patch.object(courier.codex_queue_delivery, "find_turn_by_client_message_id",
                 return_value=native_turn) as late_find:
            watcher._one(late_path)
        late_send.assert_called_once()
        late_find.assert_called_once()
        self.assertEqual(late_find.call_args.args[0:2],
                         (watcher_args.queue_socket, "idle-parent"))
        self.assertEqual(late_find.call_args.args[2], late["delivery_id"])
        self.assertEqual(late_find.call_args.kwargs["timeout"], 5.0)
        late_record = pending.read(self.root, "idle-parent", late["delivery_id"])
        self.assertEqual(late_record["state"], "acked")
        self.assertEqual(late_record["acked_by"], watcher.owner)
        self.assertEqual(native_turn["status"], "completed")
        old_after_conflict = pending.read(self.root, "idle-parent", completion["delivery_id"])
        self.assertEqual(pending.received_ack_identity(
            old_after_conflict, "idle-parent", completion["delivery_id"]), old_ack)

    def test_resolver_rejects_symlink_into_group_or_world_writable_directory(self):
        with tempfile.TemporaryDirectory(prefix="parent-replay-unsafe-endpoint-") as temporary:
            root = Path(temporary)
            target_dir = root / "unsafe"
            target_dir.mkdir(mode=0o700)
            target_dir.chmod(0o777)
            target = target_dir / "server.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(target))
            endpoint = root / "endpoint.sock"
            endpoint.symlink_to(target)
            try:
                with self.assertRaisesRegex(
                        replay.codex_queue_delivery.QueueDeliveryError,
                        "queue-endpoint-path-unsafe"):
                    replay.codex_queue_delivery.resolve_endpoint(endpoint)
            finally:
                listener.close()


if __name__ == "__main__":
    unittest.main()
