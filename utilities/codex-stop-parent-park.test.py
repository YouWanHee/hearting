#!/usr/bin/env python3
"""Regression tests for the silent, exact-cleanup-only Codex Stop bridge."""

from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STOP_PATH = ROOT / "adapters" / "codex" / "hooks" / "stop-lifecycle.py"
SESSIONEND_PATH = ROOT / "adapters" / "codex" / "hooks" / "sessionend-lifecycle.py"
HOOKS_PATH = ROOT / "adapters" / "codex" / "hooks" / "hooks.json"


def load_stop():
    spec = importlib.util.spec_from_file_location("codex_stop_lifecycle", STOP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StopLifecycleTest(unittest.TestCase):
    def test_manifest_uses_short_dedicated_stop_bridge(self):
        config = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
        definition = config["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(definition["timeout"], 30)
        self.assertIn("stop-lifecycle.py", definition["command"])
        self.assertNotIn("sessionend-lifecycle.py", definition["command"])

    def test_stop_has_no_completion_or_continuation_authority(self):
        module = load_stop()
        self.assertEqual(module.main(), 0)
        source = STOP_PATH.read_text(encoding="utf-8")
        for retired in (
            "subprocess",
            "session-end",
            "join_session_batch",
            "decision",
            "parent_session_state",
        ):
            self.assertNotIn(retired, source)

    def test_stop_source_is_limited_to_exact_interaction_cleanup(self):
        source = STOP_PATH.read_text(encoding="utf-8")
        self.assertIn("sys.stdin", source)
        self.assertIn("os.environ", source)
        self.assertIn("interaction.clear_wait", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("dispatch-wait", source)
        self.assertNotIn("join_session_batch", source)

    def test_sessionend_has_no_stop_branch(self):
        source = SESSIONEND_PATH.read_text(encoding="utf-8")
        self.assertNotIn("join_session_batch", source)
        self.assertNotIn('event == "stop"', source)
        spec = importlib.util.spec_from_file_location("codex_sessionend_lifecycle", SESSIONEND_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        launch = mock.Mock(return_value={"state": "started"})
        completion = SimpleNamespace(CompletionError=RuntimeError, launch=launch)
        payload = {"cwd": "/fixture/project", "session_id": "fixture-session"}
        # Exercise main's handoff without spawning any preflight, model, or
        # memory process. Stop remains cleanup-only; SessionEnd owns completion.
        with mock.patch.dict(sys.modules, {
            "memory_session_completion": completion,
            "fleet": SimpleNamespace(interaction=SimpleNamespace(clear_wait=mock.Mock())),
            "session_summary_trigger": SimpleNamespace(launch_trigger=mock.Mock()),
        }), mock.patch.object(module, "is_worker_session", return_value=False), \
                mock.patch.object(module, "load_payload", return_value=payload), \
                mock.patch.object(module, "input_generation", return_value="fixture-generation"), \
                mock.patch.object(module, "run_preflight") as preflight:
            self.assertEqual(module.main(), 0)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args, (
            "codex", "fixture-session", "/fixture/project", str(module.PREFLIGHT),
            ["session-end", "/fixture/project", "fixture-session"],
        ))
        self.assertEqual(launch.call_args.kwargs["input_generation"], "fixture-generation")
        preflight.assert_called_once_with(
            "material-route", "clear", "--session", "fixture-session",
            quiet=True, timeout=mock.ANY,
        )


if __name__ == "__main__":
    unittest.main()
