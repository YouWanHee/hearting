#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "claude_dispatch_headless",
    ROOT / "adapters" / "claude" / "bin" / "dispatch-headless.py",
)
WRAPPER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WRAPPER)
sys.path.insert(0, str(ROOT / "utilities"))
from model_config import parse_config  # noqa: E402


def selection(**values):
    return SimpleNamespace(
        inherit_model_settings=values.get("inherit", False),
        model_profile=values.get("profile"),
        model_role=values.get("role"),
        model=values.get("model"),
        effort=values.get("effort"),
        registered_worker=values.get("registered_worker", 1),
        dispatch_depth=values.get("dispatch_depth", 1),
        worker_type=values.get("worker_type", "owner"),
        capacity_retry=values.get("capacity_retry", 0),
    )


SHIPPED_CONF = ROOT / "adapters" / "claude" / "config" / "models.conf"


def shipped_policy() -> dict[str, str]:
    """The shipped file itself (not the user's runtime copy): expectations derive
    from it so the tests follow the config instead of a literal model name."""
    return parse_config(SHIPPED_CONF)


def restricted_policy(*aliases: str) -> dict[str, str]:
    """Shipped policy with an explicit interactive-main-only list — exercises the
    rejection path regardless of what the shipped default declares."""
    return {**shipped_policy(), "CFG_MAIN_SESSION_ONLY_MODELS": " ".join(aliases)}


class ClaudeDispatchModelEligibilityTest(unittest.TestCase):
    def setUp(self):
        # Pin the policy to the shipped file: the user's runtime copy
        # ($CLAUDE_CONFIG_DIR/agent-config/models.conf) must not steer these tests.
        patcher = mock.patch.object(WRAPPER, "_model_policy", side_effect=shipped_policy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shipped_default_has_no_main_session_only_model(self):
        # 2026-09-08 user rule: shipped default == user runtime mapping. Fable is
        # headless-eligible for the deep tier; nothing is main-session-only.
        policy = shipped_policy()
        self.assertEqual(policy["CFG_MAIN_SESSION_ONLY_MODELS"].split(), [])
        self.assertEqual(policy["CFG_TIER_DEEP_MODEL"], "fable")
        self.assertFalse(WRAPPER._main_session_only_model("claude-fable-5"))

    def test_deep_role_resolves_to_config_deep_tier_and_is_dispatch_eligible(self):
        policy = shipped_policy()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(ROOT / "adapters" / "claude" / "no-such-runtime-home")}):
            result = WRAPPER.resolve_model_settings(selection(role="deep orchestrator"))
        # Model and effort are the shipped deep-tier defaults (user-tunable), not literals.
        self.assertEqual(result["model"], policy["CFG_TIER_DEEP_MODEL"])
        self.assertEqual(result["effort"], policy["CFG_TIER_DEEP_EFFORT"])
        self.assertFalse(WRAPPER._main_session_only_model(result["model"]))

    def test_shipped_default_admits_explicit_fable_headless(self):
        result = WRAPPER.resolve_model_settings(selection(model="claude-fable-5", effort="high"))
        self.assertEqual((result["source"], result["model"], result["effort"]), ("explicit", "claude-fable-5", "high"))

    def test_explicit_and_role_override_of_a_main_only_model_are_rejected(self):
        # The rejection path stays live for any config that declares a main-only
        # model, independent of the shipped default's (empty) list.
        with mock.patch.object(WRAPPER, "_model_policy", side_effect=lambda: restricted_policy("fable")):
            with self.assertRaises(WRAPPER.ModelSelectionError) as explicit:
                WRAPPER.resolve_model_settings(selection(model="claude-fable-5", effort="xhigh"))
            self.assertEqual(explicit.exception.reason, "headless-main-session-only-model")
            with mock.patch.dict(os.environ, {"CLAUDE_MODEL_DEEP": "fable"}):
                with self.assertRaises(WRAPPER.ModelSelectionError) as mapped:
                    WRAPPER.resolve_model_settings(selection(role="deep maker"))
            self.assertEqual(mapped.exception.reason, "headless-main-session-only-model")

    def test_inherited_headless_model_is_rejected_before_launch(self):
        with self.assertRaises(WRAPPER.ModelSelectionError) as inherited:
            WRAPPER.resolve_model_settings(selection(inherit=True))
        self.assertEqual(
            inherited.exception.reason,
            "headless-model-inheritance-ineligible",
        )

    def test_missing_main_only_policy_fails_closed(self):
        with mock.patch.object(WRAPPER, "_model_policy", return_value={}):
            with self.assertRaises(WRAPPER.ModelSelectionError) as unavailable:
                WRAPPER.resolve_model_settings(
                    selection(model="sonnet", effort="high")
                )
        self.assertEqual(
            unavailable.exception.reason,
            "dispatch-model-policy-unavailable",
        )

    def test_explicit_eligible_model_remains_explicit(self):
        result = WRAPPER.resolve_model_settings(
            selection(model="sonnet", effort="high")
        )
        self.assertEqual(
            result,
            {
                "source": "explicit",
                "role": "-",
                "profile": "unsealed",
                "tier": "explicit",
                "granularity": "legacy",
                "model": "sonnet",
                "effort": "high",
            },
        )

    def test_cli_rejects_a_main_only_model_before_registry_prompt_log_or_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "repo"
            worktree.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(worktree)], check=True
            )
            # A user runtime copy that declares fable main-only (selected whole-file
            # over the shipped default): the CLI must still reject before any side effect.
            runtime_home = root / "claude-home"
            (runtime_home / "agent-config").mkdir(parents=True)
            shipped_text = SHIPPED_CONF.read_text(encoding="utf-8")
            restricted_text = re.sub(
                r'^CFG_MAIN_SESSION_ONLY_MODELS=.*$', 'CFG_MAIN_SESSION_ONLY_MODELS="fable"',
                shipped_text, count=1, flags=re.MULTILINE,
            )
            self.assertNotEqual(restricted_text, shipped_text)
            (runtime_home / "agent-config" / "models.conf").write_text(restricted_text, encoding="utf-8")
            jobs = root / "jobs.log"
            logs = root / "logs"
            env = {key: value for key, value in os.environ.items() if key != "CLAUDE_MODEL_DEEP"}
            env["CLAUDE_CONFIG_DIR"] = str(runtime_home)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "adapters" / "claude" / "bin" / "dispatch-headless.py"),
                    "--register",
                    "--worktree", str(worktree),
                    "--jobs", str(jobs),
                    "--log-dir", str(logs),
                    "--slug", "main-only-rejected",
                    "--capability", "autopilot-code",
                    "--capability-mode", "dev",
                    "--qa", "standard",
                    "--model", "claude-fable-5",
                    "--effort", "xhigh",
                    "--prompt-text", "must not launch",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
            self.assertIn("reason=headless-main-session-only-model", result.stdout)
            self.assertIn("child_spawned=0", result.stdout)
            self.assertFalse(jobs.exists())
            self.assertFalse(logs.exists())


if __name__ == "__main__":
    unittest.main()
