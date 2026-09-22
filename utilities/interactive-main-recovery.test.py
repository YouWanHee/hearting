#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import socket
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("interactive_main_recovery", HERE / "interactive-main-recovery.py")
RECOVERY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RECOVERY)


def completed(stdout: str, code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["herdr"], code, stdout=stdout, stderr="")


class InteractiveMainRecoveryTest(unittest.TestCase):
    def test_explicit_socket_is_used_for_every_host_request(self):
        calls = []
        responses = [self.payload, self.split_payload, self.get_payload, "{}"]
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return completed(responses[len(calls) - 1])
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/wrong/server.sock"}, clear=True):
            RECOVERY.start(self.args(), runner=run, which=lambda _: "herdr")
        self.assertEqual(len(calls), 4)
        for command, kwargs in calls:
            self.assertEqual(kwargs.get("env", {}).get("HERDR_SOCKET_PATH"), str(self.socket_path))
        self.assertEqual(calls[-1][0][1:3], ["pane", "run"])
        self.assertIn(str(self.launcher), calls[-1][0][-1])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.socket.close)
        self.socket_path = self.root / "herdr.sock"
        self.socket.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.launcher = self.root / "codex"
        self.launcher.write_text("#!/bin/sh\nexec python3 codex-launcher.py \"$@\"\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.launcher_state = self.root / "codex-launcher.json"
        self.launcher_state.write_text(json.dumps({
            "schema": 2, "phase": "installed", "ingress_path": str(self.launcher),
            "ingress_sha256": hashlib.sha256(self.launcher.read_bytes()).hexdigest(),
        }), encoding="utf-8")
        self.launcher_state.chmod(0o600)
        self.payload = json.dumps({"result": {"panes": [{
            "pane_id": "p-1", "workspace_id": "w-1", "tab_id": "t-1", "cwd": "/tmp/project"
        }]}})
        self.created_pane = {
            "pane_id": "p-2", "workspace_id": "w-1", "tab_id": "t-1", "cwd": "/tmp/project"
        }
        self.split_payload = json.dumps({"result": {"pane": {"pane_id": "p-2"}}})
        self.get_payload = json.dumps({"result": {"pane": self.created_pane}})

    def args(self, **changes):
        values = {
            "pane": "p-1", "name": "recovery", "workspace": "/tmp/project",
            "socket": str(self.socket_path), "launcher_state": str(self.launcher_state),
            "agent_args": [],
        }
        values.update(changes)
        return type("Args", (), values)()

    def test_check_reads_pane_inventory_only(self) -> None:
        calls: list[list[str]] = []

        def run(command, **kwargs):
            calls.append(command)
            return completed(self.payload)

        with mock.patch.dict("os.environ", {"HERDR_WORKSPACE_ID": "w-1", "HERDR_TAB_ID": "t-1"}, clear=True):
            result = RECOVERY.checked_pane(
                "p-1", workspace="/tmp/project", socket_path=str(self.socket_path),
                runner=run, which=lambda _: "herdr",
            )
        self.assertEqual(result["pane_id"], "p-1")
        self.assertEqual(calls, [["herdr", "pane", "list"]])

    def test_start_creates_visible_pane_with_native_host_api(self) -> None:
        calls: list[list[str]] = []

        def run(command, **kwargs):
            calls.append(command)
            if command[1:3] == ["pane", "list"]:
                return completed(self.payload)
            if command[1:3] == ["pane", "split"]:
                return completed(self.split_payload)
            if command[1:3] == ["pane", "get"]:
                return completed(self.get_payload)
            return completed("{}")

        with mock.patch.dict("os.environ", {}, clear=True):
            result = RECOVERY.start(
                self.args(),
                runner=run,
                which=lambda _: "/usr/bin/herdr",
            )
        self.assertEqual(result["status"], "launch-requested")
        self.assertEqual(calls[0], ["herdr", "pane", "list"])
        self.assertEqual(calls[1], [
            "herdr", "pane", "split", "--pane", "p-1", "--direction", "right",
            "--cwd", "/tmp/project", "--focus",
        ])
        self.assertEqual(calls[2], ["herdr", "pane", "get", "p-2"])
        self.assertEqual(calls[3][3], "p-2")
        self.assertIn(str(self.launcher), calls[3][-1])
        self.assertFalse(any("tmux" in call for call in calls))

    def test_foreign_tab_is_rejected(self) -> None:
        with self.assertRaises(RECOVERY.RecoveryError) as raised:
            with mock.patch.dict("os.environ", {"HERDR_WORKSPACE_ID": "w-1", "HERDR_TAB_ID": "other"}, clear=True):
                RECOVERY.checked_pane(
                    "p-1", workspace="/tmp/project", socket_path=str(self.socket_path),
                    runner=lambda _, **kwargs: completed(self.payload), which=lambda _: "herdr",
                )
        self.assertEqual(raised.exception.reason, "tab-mismatch")

    def test_missing_or_mismatched_cwd_is_rejected(self) -> None:
        for cwd, reason in ((None, "pane-cwd-missing"), ("/tmp/other", "pane-cwd-mismatch")):
            pane = {"pane_id": "p-1", "workspace_id": "w-1", "tab_id": "t-1"}
            if cwd is not None:
                pane["cwd"] = cwd
            payload = json.dumps({"result": {"panes": [pane]}})
            with self.subTest(cwd=cwd), mock.patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(RECOVERY.RecoveryError) as raised:
                    RECOVERY.checked_pane(
                        "p-1", workspace="/tmp/project", socket_path=str(self.socket_path),
                        runner=lambda _, **kwargs: completed(payload), which=lambda _: "herdr",
                    )
            self.assertEqual(raised.exception.reason, reason)

    def test_path_shadow_cannot_replace_protected_launcher(self) -> None:
        calls: list[list[str]] = []

        def run(command, **kwargs):
            calls.append(command)
            if command[1:3] == ["pane", "list"]:
                return completed(self.payload)
            if command[1:3] == ["pane", "split"]:
                return completed(self.split_payload)
            if command[1:3] == ["pane", "get"]:
                return completed(self.get_payload)
            return completed("{}")

        with mock.patch.dict("os.environ", {"PATH": str(self.root)}, clear=True):
            RECOVERY.start(self.args(), runner=run, which=lambda _: str(self.root / "fake-herdr"))
        self.assertEqual(shlex.split(calls[3][-1])[0], str(self.launcher))

    def test_split_failure_starts_no_agent(self) -> None:
        calls: list[list[str]] = []

        def run(command, **kwargs):
            calls.append(command)
            if command[1:3] == ["pane", "list"]:
                return completed(self.payload)
            return completed("split failed", code=7)

        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RECOVERY.RecoveryError) as raised:
                RECOVERY.start(self.args(), runner=run, which=lambda _: "/usr/bin/herdr")
        self.assertEqual(raised.exception.reason, "herdr-pane-split-failed")
        self.assertFalse(any(command[1:3] == ["pane", "run"] for command in calls))

    def test_post_split_validation_reports_created_pane(self) -> None:
        calls: list[list[str]] = []
        foreign = dict(self.created_pane, tab_id="w-1:t-other")

        def run(command, **kwargs):
            calls.append(command)
            if command[1:3] == ["pane", "list"]:
                return completed(self.payload)
            if command[1:3] == ["pane", "split"]:
                return completed(self.split_payload)
            if command[1:3] == ["pane", "get"]:
                return completed(json.dumps({"result": {"pane": foreign}}))
            return completed("{}")

        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RECOVERY.RecoveryError) as raised:
                RECOVERY.start(self.args(), runner=run, which=lambda _: "/usr/bin/herdr")
        self.assertEqual(raised.exception.reason, "tab-mismatch")
        self.assertEqual(raised.exception.created_pane, "p-2")
        self.assertFalse(any(command[1:3] == ["pane", "run"] for command in calls))

    def test_launch_transport_error_keeps_created_pane(self):
        def run(command, **kwargs):
            responses = {"list": self.payload, "split": self.split_payload, "get": self.get_payload}
            if command[2] in responses:
                return completed(responses[command[2]])
            raise RECOVERY.RecoveryError("herdr-invocation-failed", "timed out")
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(RECOVERY.RecoveryError) as raised:
            RECOVERY.start(self.args(), runner=run, which=lambda _: "herdr")
        self.assertEqual(raised.exception.created_pane, "p-2")

    def test_missing_host_location_is_rejected_before_mutation(self):
        payload = json.dumps({"result": {"panes": [{"pane_id": "p-1", "cwd": "/tmp/project"}]}})
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(RECOVERY.RecoveryError) as raised:
            RECOVERY.checked_pane("p-1", socket_path=str(self.socket_path),
                                  runner=lambda _, **kwargs: completed(payload), which=lambda _: "herdr")
        self.assertEqual(raised.exception.reason, "pane-location-missing")

    def test_agent_start_failure_is_typed(self) -> None:
        def run(command, **kwargs):
            if command[1:3] == ["pane", "list"]:
                return completed(self.payload)
            if command[1:3] == ["pane", "split"]:
                return completed(self.split_payload)
            if command[1:3] == ["pane", "get"]:
                return completed(self.get_payload)
            return completed("", code=7)

        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RECOVERY.RecoveryError) as raised:
                RECOVERY.start(self.args(), runner=run, which=lambda _: "/usr/bin/herdr")
        self.assertEqual(raised.exception.reason, "herdr-agent-start-failed")
        self.assertEqual(raised.exception.created_pane, "p-2")


if __name__ == "__main__":
    unittest.main()
