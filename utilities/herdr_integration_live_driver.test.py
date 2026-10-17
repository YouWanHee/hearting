#!/usr/bin/env python3
"""Hermetic contract tests for the bounded native integration evidence driver."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "utilities" / "herdr_integration_live.py"
SPEC = importlib.util.spec_from_file_location("herdr_integration_live_driver", DRIVER)
live = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = live
SPEC.loader.exec_module(live)


class NativeDriverContractTest(unittest.TestCase):
    def test_case_set_matches_the_native_measurement_matrix(self):
        self.assertEqual(set(live.CASES), {
            "callbacks", "codex-async-local-skip", "codex-busy",
            "opencode-busy", "booked-seat", "events-lost",
        })

    def test_prompt_payload_is_redacted_from_durable_argv(self):
        rendered = live._redact_argv([
            "herdr", "agent", "prompt", "private-agent",
            "do not store this prompt", "--wait", "--attach-url",
            "https://user:pass@example.test/private?token=secret",
        ])
        text = " ".join(rendered)
        self.assertNotIn("do not store", text)
        self.assertNotIn("user:pass", text)
        self.assertNotIn("token=secret", text)

    def test_isolation_drops_inherited_credentials_and_keeps_output_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            output = root / "output"
            with mock.patch.dict(os.environ, {
                "OPENAI_API_KEY": "must-not-copy",
                "HERDR_SOCKET_PATH": "/run/default-herdr.sock",
                "CODEX_THREAD_ID": "foreign-thread",
            }, clear=True):
                driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                    "--case", "callbacks", "--output-dir", str(output),
                    "--project", str(project), "--timeout", "10",
                ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            self.assertNotIn("OPENAI_API_KEY", driver.env)
            self.assertNotIn("HERDR_SOCKET_PATH", driver.env)
            self.assertNotIn("CODEX_THREAD_ID", driver.env)
            self.assertEqual(driver.result["isolation"]["credentials_copied"], False)
            self.assertEqual(Path(driver.env["HOME"]).parent, driver.runtime_root / "isolated")

    def test_command_evidence_keeps_exit_and_hash_but_not_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "codex-busy", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            rc, stdout, _stderr = driver.command(
                "fixture", ["sh", "-c", "printf '%s' \"$PAYLOAD\""],
                env={**driver.env, "PAYLOAD": "private-output"})
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "private-output")
            recorded = driver.commands_path.read_text(encoding="utf-8")
            self.assertNotIn("private-output", recorded)
            row = json.loads(recorded)
            self.assertEqual(row["step"], "fixture")
            self.assertEqual(row["returncode"], 0)
            self.assertTrue(row["stdout_hmac"])
            self.assertTrue(row["stderr_reason"] == "exit-0")

    def test_seeded_native_callback_redirect_keeps_a_spaced_private_path_as_one_target(self):
        with tempfile.TemporaryDirectory(prefix="hearting callback ") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "callbacks", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            for runtime, event in (("claude", "SessionStart"), ("codex", "SessionStart")):
                with self.subTest(runtime=runtime):
                    log = driver._seed_user_callback(runtime)
                    config = Path(driver.env["CLAUDE_CONFIG_DIR"] if runtime == "claude"
                                  else driver.env["CODEX_HOME"]) / (
                                      "settings.json" if runtime == "claude" else "hooks.json")
                    payload = json.loads(config.read_text(encoding="utf-8"))
                    if runtime == "claude":
                        command = payload["hooks"][event][0]["hooks"][0]["command"]
                    else:
                        command = payload["hooks"][event][0]["hooks"][0]["command"]
                    argv = shlex.split(command)
                    self.assertEqual(argv[:3], ["printf", runtime, ">>"])
                    self.assertEqual(argv[3], str(log))

    def test_long_evidence_path_can_bind_a_private_socket_and_failure_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            output = root / ("long-evidence-" + "a" * 100) / ("b" * 100)
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "events-lost", "--output-dir", str(output),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            runtime = driver.runtime_root
            endpoint = Path(driver.env["HERDR_CONFIG_PATH"]).parent / "sessions" / driver.session / "herdr-client.sock"
            endpoint.parent.mkdir(parents=True)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(endpoint))
            with mock.patch.object(driver, "_case", return_value={"outcome": "unavailable", "reason": "fixture"}), \
                    mock.patch("sys.stdout"):
                self.assertEqual(driver.execute(), 2)
            self.assertFalse(runtime.exists())
            self.assertTrue(driver.result_path.is_file())
            result = json.loads(driver.result_path.read_text())
            self.assertIn({"action": "remove-private-runtime", "result": "removed"}, result["cleanup"])

    def test_startup_error_keeps_classification_without_raw_server_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "events-lost", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            driver.herdr = "/fixture/herdr"
            server = mock.Mock(pid=2147483647)
            server.poll.return_value = 1

            def start(_argv, **kwargs):
                kwargs["stderr"].write(b"private startup text: socket name length exceeds capacity of sun_path")
                kwargs["stderr"].flush()
                return server

            with mock.patch.object(live.subprocess, "Popen", side_effect=start):
                self.assertFalse(driver.start_server())
            evidence = driver.commands_path.read_text()
            self.assertNotIn("private startup text", evidence)
            last = json.loads(evidence.splitlines()[-1])
            self.assertEqual(last["stderr_reason"], "local-socket-path-too-long")
            self.assertEqual(last["returncode"], 1)
            self.assertTrue(last["stderr_hmac"])

    def test_event_tap_uses_private_endpoint_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "events-lost", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
            self.addCleanup(driver.runtime_dir.cleanup)
            driver.env["HERDR_SOCKET_PATH"] = "/private/owned/herdr.sock"
            sys.path.insert(0, str(ROOT / "utilities"))
            self.addCleanup(sys.path.remove, str(ROOT / "utilities"))
            import herdr_observation
            with mock.patch.dict(os.environ, {
                    "HERDR_SOCKET_PATH": "/foreign/herdr.sock",
                    "AGENT_HERDR_SESSION": "foreign-session"}), \
                    mock.patch.object(herdr_observation, "ObservationTransport") as transport:
                transport.return_value.observe.return_value.complete = True
                try:
                    self.assertTrue(driver._start_event_tap())
                    transport.assert_called_once_with(
                        driver.session, "/private/owned/herdr.sock", timeout=0.5)
                finally:
                    driver._stop_event_tap()


if __name__ == "__main__":
    unittest.main()
