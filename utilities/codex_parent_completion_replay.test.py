"""Focused safety regressions for isolated native Codex replay setup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import tomllib
import unittest
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
