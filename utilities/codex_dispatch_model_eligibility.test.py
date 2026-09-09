#!/usr/bin/env python3
"""Codex parity for the main-session-only gate and the `top` exception profile (2026-09-10)."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "codex_dispatch_headless", ROOT / "adapters" / "codex" / "bin" / "dispatch-headless.py")
WRAPPER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(WRAPPER)
sys.path.insert(0, str(ROOT / "utilities"))
from model_config import parse_config  # noqa: E402

SHIPPED_CONF = ROOT / "adapters" / "codex" / "config" / "models.conf"


def selection(**values):
    return SimpleNamespace(
        inherit_model_settings=values.get("inherit", False),
        model_profile=values.get("profile"),
        model_role=values.get("role"),
        model=values.get("model"),
        reasoning=values.get("reasoning"),
        registered_worker=values.get("registered_worker", 1),
        dispatch_depth=values.get("dispatch_depth", 1),
        worker_type=values.get("worker_type", "owner"),
        capacity_retry=values.get("capacity_retry", 0),
    )


def shipped_policy() -> dict[str, str]:
    return parse_config(SHIPPED_CONF)


class CodexDispatchModelEligibilityTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(WRAPPER, "_model_policy", side_effect=shipped_policy)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {
            "AGENT_HOME": str(ROOT),
            "CODEX_HOME": str(ROOT / "adapters" / "codex" / "no-such-runtime-home"),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_shipped_default_declares_astra_main_session_only(self):
        policy = shipped_policy()
        self.assertEqual(policy["CFG_MAIN_SESSION_ONLY_MODELS"].split(), ["gpt-6-astra"])
        self.assertTrue(WRAPPER._main_session_only_model("gpt-6-astra"))
        self.assertFalse(WRAPPER._main_session_only_model(policy["CFG_TIER_DEEP_MODEL"]))

    def test_explicit_astra_is_refused_and_an_eligible_explicit_model_passes(self):
        with self.assertRaises(WRAPPER.ModelSelectionError) as refused:
            WRAPPER.resolve_model_settings(selection(model="gpt-6-astra", reasoning="xhigh"))
        self.assertEqual(refused.exception.reason, "headless-main-session-only-model")
        result = WRAPPER.resolve_model_settings(selection(model="gpt-5.6-sol", reasoning="high"))
        self.assertEqual((result["source"], result["model"]), ("explicit", "gpt-5.6-sol"))

    def test_a_role_mapped_to_astra_is_refused(self):
        with mock.patch.object(WRAPPER, "role_map", return_value={"exact_model_id": "gpt-6-astra", "reasoning": "high"}):
            with self.assertRaises(WRAPPER.ModelSelectionError) as refused:
                WRAPPER.resolve_model_settings(selection(role="deep maker"))
        self.assertEqual(refused.exception.reason, "headless-main-session-only-model")

    def test_the_sealed_top_profile_is_the_one_door_to_astra(self):
        policy = shipped_policy()
        result = WRAPPER.resolve_model_settings(selection(profile="top"))
        self.assertEqual((result["model"], result["reasoning"], result["source"], result["tier"]),
                         (policy["CFG_TIER_TOP_MODEL"], policy["CFG_TIER_TOP_EFFORT"], "profile-top", "top"))
        with self.assertRaises(WRAPPER.ModelSelectionError) as override:
            WRAPPER.resolve_model_settings(selection(profile="top", model="gpt-6-astra", reasoning="xhigh", capacity_retry=1))
        self.assertEqual(override.exception.reason, "headless-main-session-only-model")
        with self.assertRaises(WRAPPER.ModelSelectionError) as depth:
            WRAPPER.resolve_model_settings(selection(profile="top", dispatch_depth=2, worker_type="stage"))
        self.assertEqual(depth.exception.reason, "invalid-dispatch-model-profile")

    def test_a_user_copy_without_the_key_carries_no_restriction(self):
        without = {k: v for k, v in shipped_policy().items() if k != "CFG_MAIN_SESSION_ONLY_MODELS"}
        with mock.patch.object(WRAPPER, "_model_policy", return_value=without):
            self.assertFalse(WRAPPER._main_session_only_model("gpt-6-astra"))

    def test_the_receipt_reports_whether_the_key_is_declared(self):
        self.assertEqual(WRAPPER._main_session_only_policy_state(), "declared")
        without = {k: v for k, v in shipped_policy().items() if k != "CFG_MAIN_SESSION_ONLY_MODELS"}
        with mock.patch.object(WRAPPER, "_model_policy", return_value=without):
            self.assertEqual(WRAPPER._main_session_only_policy_state(), "absent")
        with mock.patch.object(WRAPPER, "_model_policy",
                               side_effect=WRAPPER.ModelSelectionError("dispatch-model-policy-unavailable", "x")):
            self.assertEqual(WRAPPER._main_session_only_policy_state(), "unavailable")

    def test_deep_profile_and_ordinary_roles_are_unaffected(self):
        result = WRAPPER.resolve_model_settings(selection(profile="deep"))
        self.assertEqual((result["source"], result["model"]), ("profile", shipped_policy()["CFG_TIER_DEEP_MODEL"]))


if __name__ == "__main__":
    unittest.main()
