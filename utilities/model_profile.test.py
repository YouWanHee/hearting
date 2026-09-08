#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PROFILE = load("portable_model_profile", ROOT / "utilities" / "model_profile.py")
WRAPPERS = {
    adapter: load(
        f"{adapter}_profile_wrapper",
        ROOT / "adapters" / adapter / "bin" / "dispatch-headless.py",
    )
    for adapter in ("claude", "codex", "opencode")
}


def args(adapter: str, profile: str, **overrides):
    budget_key = {"claude": "effort", "codex": "reasoning", "opencode": "variant"}[adapter]
    values = {
        "model_profile": profile,
        "registered_worker": 1,
        "dispatch_depth": 2,
        "worker_type": "stage",
        "inherit_model_settings": False,
        "model_role": "fast implementer",
        "model": None,
        budget_key: None,
        "capacity_retry": 0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class ModelProfileTest(unittest.TestCase):
    def demand(self, judgment="predetermined", scope="short-local", **extra):
        value = {
            "schema_version": 1,
            "judgment_requirement": judgment,
            "execution_scope": scope,
            "judgment_reason": "approved judgment boundary",
            "execution_reason": "bounded execution scope",
            "evidence_refs": ["fixture:sd-88"],
        }
        value.update(extra)
        return value

    def test_two_axis_matrix_and_independent_floors(self):
        expected = {
            ("predetermined", "short-local"): "light",
            ("predetermined", "extended-multistep"): "balanced",
            ("important", "short-local"): "balanced-deep",
            ("important", "extended-multistep"): "balanced-deep",
            ("difficult-uncertain", "short-local"): "deep",
            ("difficult-uncertain", "extended-multistep"): "deep",
        }
        for axes, profile in expected.items():
            with self.subTest(axes=axes):
                result = PROFILE.resolve_profile_demand(self.demand(*axes))
                self.assertEqual(result["resolved_profile"], profile)
                self.assertTrue(result["demand_digest"])
        with self.assertRaises(PROFILE.ModelProfileError) as caught:
            PROFILE.resolve_profile_demand(
                self.demand("predetermined", "short-local"), explicit_profile="deep"
            )
        self.assertEqual(caught.exception.reason, "profile-floor-violation")
        result = PROFILE.resolve_profile_demand(
            self.demand("important"), explicit_profile="deep"
        )
        self.assertIn("additional", result["reason"])

    def test_demand_reasons_and_axes_are_sealed(self):
        base = PROFILE.resolve_profile_demand(self.demand("important"))
        changed = PROFILE.resolve_profile_demand(
            self.demand("important", judgment_reason="new approved reason")
        )
        self.assertNotEqual(base["demand_digest"], changed["demand_digest"])
        for field, value in (("execution_scope", "unknown"), ("evidence_refs", [])):
            with self.subTest(field=field), self.assertRaises(PROFILE.ModelProfileError):
                PROFILE.resolve_profile_demand(self.demand("important", **{field: value}))

    def test_malformed_cfg_declarations_fail_loudly(self):
        cases = {
            "missing equals": "CFG_MODEL_PROFILE_GRANULARITY\n",
            "invalid key": "CFG_model_profile=full\n",
            "empty value": "CFG_MODEL_PROFILE_GRANULARITY=\n",
            "unsafe value": "CFG_MODEL_PROFILE_GRANULARITY=full+collapsed\n",
        }
        for label, config_text in cases.items():
            with tempfile.NamedTemporaryFile(
                "w", suffix=".conf", delete=False
            ) as handle:
                handle.write(config_text)
                path = handle.name
            try:
                with self.subTest(label=label), self.assertRaises(
                    PROFILE.ModelProfileError
                ) as caught:
                    PROFILE.load_config(path)
                self.assertIn("line 1", str(caught.exception))
            finally:
                Path(path).unlink()

    def test_unrelated_non_cfg_lines_remain_ignored(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as handle:
            handle.write("not a declaration\nOTHER=value\nCFG_MODEL_PROFILE_GRANULARITY=full\n")
            path = handle.name
        try:
            self.assertEqual(
                PROFILE.load_config(path),
                {"CFG_MODEL_PROFILE_GRANULARITY": "full"},
            )
        finally:
            Path(path).unlink()

    def test_portable_profiles_resolve_to_declared_adapter_budgets(self):
        expected = {
            # Five profiles use the configured judgment and execution budgets;
            # Claude keeps five operating points across three concrete models.
            "claude": {
                "deep": ("fable", "high"),
                "balanced-deep": ("opus", "high"),
                "balanced": ("sonnet", "high"),
                "light": ("sonnet", "medium"),
                "mini": ("sonnet", "low"),
            },
            "codex": {
                "deep": ("gpt-6-astra", "high"),
                "balanced-deep": ("gpt-6-astra", "medium"),
                "balanced": ("gpt-5.6-luna", "high"),
                "light": ("gpt-5.6-luna", "medium"),
                "mini": ("gpt-5.6-luna", "low"),
            },
            # OpenCode has five profiles and three shipped operating points:
            # balanced/light/mini share one model and runtime-default budget,
            # while deep and balanced-deep use distinct configured models.
            "opencode": {
                "deep": ("opencode-go/qwen3.8-max", "runtime-default"),
                "balanced-deep": ("opencode-go/glm-5.3", "runtime-default"),
                "balanced": ("opencode-go/glm-5.3-flash", "runtime-default"),
                "light": ("opencode-go/glm-5.3-flash", "runtime-default"),
                "mini": ("opencode-go/glm-5.3-flash", "runtime-default"),
            },
        }
        for adapter, profiles in expected.items():
            for profile, pair in profiles.items():
                with self.subTest(adapter=adapter, profile=profile):
                    resolved = PROFILE.resolve_profile(
                        adapter,
                        ROOT / "adapters" / adapter / "config" / "models.conf",
                        profile,
                    )
                    self.assertEqual((resolved["model"], resolved["budget"]), pair)

    def test_route_profile_is_primary_and_preserves_semantic_role(self):
        for adapter, wrapper in WRAPPERS.items():
            with self.subTest(adapter=adapter):
                resolved = wrapper.resolve_model_settings(args(adapter, "balanced-deep"))
                self.assertEqual(resolved["source"], "profile")
                self.assertEqual(resolved["role"], "fast implementer")
                self.assertEqual(resolved["profile"], "balanced-deep")
                self.assertNotEqual(resolved["model"], "inherit")

    def test_runtime_profile_prefers_complete_user_config(self):
        adapter = "codex"
        shipped = (ROOT / "adapters" / adapter / "config" / "models.conf").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            user = home / "agent-config" / "models.conf"
            user.parent.mkdir()
            user.write_text(
                shipped.replace(
                    "CFG_TIER_DEEP_MODEL=gpt-6-astra",
                    "CFG_TIER_DEEP_MODEL=user/deep",
                )
            )
            resolved, receipt = PROFILE.resolve_runtime_profile(
                adapter, "deep", runtime=home, source_root=ROOT
            )
            self.assertEqual(resolved["model"], "user/deep")
            self.assertEqual(receipt.source, "user")

    def test_owner_profile_does_not_need_a_stage_role(self):
        for adapter, wrapper in WRAPPERS.items():
            with self.subTest(adapter=adapter):
                resolved = wrapper.resolve_model_settings(args(
                    adapter, "balanced-deep", dispatch_depth=1, worker_type="owner", model_role=None
                ))
                self.assertEqual(resolved["role"], "_kernel/owner")
                self.assertEqual(resolved["profile"], "balanced-deep")

    def test_mini_is_denied_for_registered_substantive_topology(self):
        for adapter, wrapper in WRAPPERS.items():
            with self.subTest(adapter=adapter), self.assertRaises(wrapper.ModelSelectionError) as caught:
                wrapper.resolve_model_settings(args(adapter, "mini"))
            self.assertEqual(caught.exception.reason, "invalid-dispatch-model-profile")

    def test_concrete_override_requires_checked_capacity_retry(self):
        cases = {
            "claude": {"model": "sonnet", "effort": "medium"},
            "codex": {"model": "gpt-5.6-luna", "reasoning": "medium"},
            "opencode": {"model": "opencode-go/deepseek-v4-pro", "variant": "runtime-default"},
        }
        for adapter, concrete in cases.items():
            wrapper = WRAPPERS[adapter]
            with self.subTest(adapter=adapter), self.assertRaises(wrapper.ModelSelectionError) as caught:
                wrapper.resolve_model_settings(args(adapter, "deep", **concrete))
            self.assertEqual(caught.exception.reason, "model-profile-override-forbidden")
            resolved = wrapper.resolve_model_settings(
                args(adapter, "deep", capacity_retry=1, **concrete)
            )
            self.assertEqual(resolved["source"], "profile+capacity")
            self.assertEqual(resolved["model"], concrete["model"])

    def test_opencode_live_conf_resolves_deep_and_balanced_deep_distinctly(self):
        # 66e38467 (2026-08-07 사용자 결정): deep=qwen3.8-max. 2026-09-03 tier
        # refresh moved balanced-deep to glm-5.3 and light/mini to glm-5.3-flash
        # (same-or-cheaper registry rows); only `mini` still collapses (into
        # light), named by CFG_MODEL_PROFILE_GRANULARITY.
        conf = ROOT / "adapters" / "opencode" / "config" / "models.conf"
        balanced = PROFILE.resolve_profile("opencode", conf, "balanced-deep")
        self.assertEqual(balanced["tier"], "balanced-deep")
        self.assertEqual(balanced["model"], "opencode-go/glm-5.3")

        deep = PROFILE.resolve_profile("opencode", conf, "deep")
        self.assertEqual(deep["tier"], "deep")
        self.assertEqual(deep["model"], "opencode-go/qwen3.8-max")
        self.assertEqual(deep["granularity"], "collapsed-mini")

    def test_per_profile_granularity_key_supports_typed_demotion(self):
        # Mechanism guard for CFG_MODEL_PROFILE_GRANULARITY_<PROFILE>: an adapter
        # with a vacant tier may demote a profile and record it per-profile
        # without touching the file-wide granularity value.
        import os
        import tempfile
        conf_text = (
            "CFG_TIER_BALANCED_DEEP_MODEL=opencode-go/glm-5.2\n"
            "CFG_TIER_BALANCED_DEEP_VARIANT=runtime-default\n"
            "CFG_TIER_LIGHT_MODEL=opencode-go/deepseek-v4-flash\n"
            "CFG_TIER_LIGHT_VARIANT=runtime-default\n"
            "CFG_TIER_MINI_MODEL=opencode-go/deepseek-v4-flash\n"
            "CFG_TIER_MINI_VARIANT=runtime-default\n"
            "CFG_MODEL_PROFILE_DEEP=balanced-deep:runtime-default\n"
            "CFG_MODEL_PROFILE_BALANCED_DEEP=balanced-deep:runtime-default\n"
            "CFG_MODEL_PROFILE_LIGHT=light:runtime-default\n"
            "CFG_MODEL_PROFILE_MINI=mini:runtime-default\n"
            "CFG_MODEL_PROFILE_GRANULARITY=exact\n"
            "CFG_MODEL_PROFILE_GRANULARITY_DEEP=deep-vacant-demoted-to-balanced-deep\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as handle:
            handle.write(conf_text)
            path = handle.name
        try:
            deep = PROFILE.resolve_profile("opencode", path, "deep")
            self.assertEqual(deep["tier"], "balanced-deep")
            self.assertEqual(deep["model"], "opencode-go/glm-5.2")
            self.assertEqual(deep["granularity"], "deep-vacant-demoted-to-balanced-deep")
            balanced = PROFILE.resolve_profile("opencode", path, "balanced-deep")
            self.assertEqual(balanced["granularity"], "exact")
        finally:
            os.unlink(path)

    def test_claude_codex_granularity_unaffected_by_per_profile_key(self):
        for adapter, expected in {"claude": "full", "codex": "full"}.items():
            conf = ROOT / "adapters" / adapter / "config" / "models.conf"
            for profile in ("deep", "balanced-deep"):
                with self.subTest(adapter=adapter, profile=profile):
                    resolved = PROFILE.resolve_profile(adapter, conf, profile)
                    self.assertEqual(resolved["granularity"], expected)

    def test_opencode_runtime_default_omits_unverified_variant_flag(self):
        wrapper = WRAPPERS["opencode"]
        resolved = wrapper.resolve_model_settings(args("opencode", "balanced-deep"))
        with tempfile.TemporaryDirectory() as temp_dir:
            command = wrapper.shell_command(
                argparse.Namespace(
                    resolved_model_settings=resolved,
                    worktree=temp_dir,
                    agent="build",
                ),
                Path(temp_dir) / "prompt.txt",
                Path(temp_dir) / "worker.log",
            )
        self.assertIn("--model", command)
        self.assertNotIn("--variant", command)

    def test_existing_key_astra_ultra_fixture_resolves_deep_profile(self):
        # O2: the opt-in changes only existing values (deep tier model/effort,
        # deep profile tier:budget, first failover-cascade step) -- no new
        # CFG_* key is introduced, and no empty-budget inheritance occurs.
        import tempfile
        conf_text = (
            "CFG_TIER_DEEP_MODEL=gpt-6-astra\n"
            "CFG_TIER_DEEP_EFFORT=ultra\n"
            "CFG_TIER_LIGHT_MODEL=gpt-5.6-luna\n"
            "CFG_TIER_LIGHT_EFFORT=medium\n"
            "CFG_TIER_MINI_MODEL=gpt-5.6-luna\n"
            "CFG_TIER_MINI_EFFORT=low\n"
            "CFG_MODEL_PROFILE_DEEP=deep:ultra\n"
            "CFG_MODEL_PROFILE_BALANCED_DEEP=deep:medium\n"
            "CFG_MODEL_PROFILE_LIGHT=light:medium\n"
            "CFG_MODEL_PROFILE_MINI=mini:low\n"
            "CFG_MODEL_PROFILE_GRANULARITY=full\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as handle:
            handle.write(conf_text)
            path = handle.name
        deep = PROFILE.resolve_profile("codex", path, "deep")
        self.assertEqual(deep["tier"], "deep")
        self.assertEqual(deep["model"], "gpt-6-astra")
        self.assertEqual(deep["budget"], "ultra")
        self.assertEqual(deep["budget_kind"], "effort")
        balanced_deep = PROFILE.resolve_profile("codex", path, "balanced-deep")
        self.assertEqual(balanced_deep["model"], "gpt-6-astra")
        self.assertEqual(balanced_deep["budget"], "medium")
        self.assertNotEqual(deep["budget"], "")
        self.assertNotEqual(balanced_deep["budget"], "")


if __name__ == "__main__":
    unittest.main()
