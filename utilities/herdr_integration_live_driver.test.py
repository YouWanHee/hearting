#!/usr/bin/env python3
"""Hermetic contract tests for the bounded native integration evidence driver."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
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
            self.assertNotIn("OPENAI_API_KEY", driver.env)
            self.assertNotIn("HERDR_SOCKET_PATH", driver.env)
            self.assertNotIn("CODEX_THREAD_ID", driver.env)
            self.assertEqual(driver.result["isolation"]["credentials_copied"], False)
            self.assertEqual(Path(driver.env["HOME"]).parent, driver.case_dir / "isolated")

    def test_command_evidence_keeps_exit_and_hash_but_not_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            driver = live.HerdrIntegrationLive(live.build_parser().parse_args([
                "--case", "codex-busy", "--output-dir", str(root / "out"),
                "--project", str(project), "--timeout", "10",
            ]))
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


if __name__ == "__main__":
    unittest.main()
