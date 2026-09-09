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

    @staticmethod
    def _declared_point(config, profile):
        """Independent derivation of a profile's operating point from the raw
        CFG_ keys (tier -> CFG_TIER_<TIER>_MODEL / _EFFORT|_VARIANT), so the
        expectation follows the shipped file instead of a literal table."""
        tier, budget = config[f"CFG_MODEL_PROFILE_{profile.upper().replace('-', '_')}"].split(":", 1)
        key = tier.upper().replace("-", "_")
        return (config[f"CFG_TIER_{key}_MODEL"], budget)

    @staticmethod
    def _reachable_models(adapter, config, path):
        """Every concrete model this config can put in front of a worker: the declared
        tier models plus each of the five profiles' resolved model. A profile may name a
        model directly (`model/<id>:budget`), so scanning `CFG_TIER_*_MODEL` alone leaves
        a hole an edit can walk through (review MA-2a)."""
        models = {value for key, value in config.items() if key.endswith("_MODEL")}
        for profile in ("deep", "balanced-deep", "balanced", "light", "mini"):
            models.add(PROFILE.resolve_profile(adapter, path, profile)["model"])
        return models

    def test_claude_shipped_default_is_the_user_profile_mapping(self):
        # 2026-09-09 user rule: the shipped default equals the user's runtime
        # mapping — the top model (Fable) is reserved for the main session, so
        # both deep-side profiles ride opus and separate by effort only.
        path = ROOT / "adapters" / "claude" / "config" / "models.conf"
        config = PROFILE.load_config(path)
        main_only = config["CFG_MAIN_SESSION_ONLY_MODELS"].split()
        self.assertEqual(main_only, ["fable"])
        self.assertEqual(self._declared_point(config, "deep"), ("opus", "xhigh"))
        self.assertEqual(self._declared_point(config, "balanced-deep"), ("opus", "medium"))
        cascade = [entry.split(":", 1)[0] for entry in config["CFG_TIER_DEEP_FAILOVER_CASCADE"].split()]
        self.assertEqual(cascade, ["opus", "sonnet"])
        self.assertEqual(cascade[0], config["CFG_TIER_DEEP_MODEL"])
        # The role mappers read CFG_TIER_DEEP_EFFORT directly while route-bound work
        # reads the profile's budget; a config where the two disagree silently makes
        # the README's `deep reviewer` row false, so pin them equal (review MI-4).
        self.assertEqual(config["CFG_TIER_DEEP_EFFORT"], "xhigh")
        self.assertEqual(config["CFG_TIER_DEEP_EFFORT"], self._declared_point(config, "deep")[1])
        # A main-session-only model may never appear in a dispatch-eligible tier, in the
        # cascade the fallback walks, or as ANY profile's resolved model — the last of
        # which is the `model/<id>:budget` profile form a tier-key scan cannot see
        # (review MA-2a).
        self.assertNotIn(config["CFG_TIER_DEEP_MODEL"], main_only)
        self.assertEqual([model for model in cascade if model in main_only], [])
        self.assertEqual(
            [m for m in self._reachable_models("claude", config, path) if m in main_only], []
        )
        self.assertEqual(config["CFG_TIER_DEEP_FAILOVER"], "light")
        points = {profile: self._declared_point(config, profile) for profile in ("deep", "balanced-deep", "balanced", "light", "mini")}
        self.assertNotEqual(points["deep"], points["balanced-deep"])  # two distinct operating points
        self.assertEqual(len(set(points.values())), 5)  # five distinct operating points

    def test_codex_shipped_default_is_the_user_profile_mapping(self):
        # Same 2026-09-09 rule on the Codex adapter: Astra is the top model and
        # stays with the main session, so the deep tier is Sol at its maximum
        # effort and balanced-deep is the same model at medium. This adapter has
        # no main-session-only KEY — the restriction is carried by never naming
        # Astra in a tier or cascade, which is what this test pins.
        path = ROOT / "adapters" / "codex" / "config" / "models.conf"
        config = PROFILE.load_config(path)
        self.assertEqual(self._declared_point(config, "deep"), ("gpt-5.6-sol", "xhigh"))
        self.assertEqual(self._declared_point(config, "balanced-deep"), ("gpt-5.6-sol", "medium"))
        cascade = [entry.split(":", 1)[0] for entry in config["CFG_TIER_DEEP_FAILOVER_CASCADE"].split()]
        self.assertEqual(cascade[0], config["CFG_TIER_DEEP_MODEL"])
        self.assertEqual(config["CFG_TIER_DEEP_EFFORT"], "xhigh")  # review MI-4, as above
        self.assertEqual(config["CFG_TIER_DEEP_EFFORT"], self._declared_point(config, "deep")[1])
        # Every way a model can be reached — tier keys, the cascade, and each profile's
        # RESOLVED model (which covers the `model/<id>:budget` form a key scan misses,
        # review MA-2a). `tools/check-model-config.py` separately refuses the literal
        # anywhere outside this file.
        self.assertNotIn("gpt-6-astra", set(cascade) | self._reachable_models("codex", config, path))

    def test_portable_profiles_resolve_to_declared_adapter_budgets(self):
        claude_config = PROFILE.load_config(ROOT / "adapters" / "claude" / "config" / "models.conf")
        codex_config = PROFILE.load_config(ROOT / "adapters" / "codex" / "config" / "models.conf")
        expected = {
            # Five profiles use the configured judgment and execution budgets.
            # Claude expectations derive from the shipped config (the user's
            # runtime mapping is the shipped default, 2026-09-09); the concrete
            # contract itself is asserted in
            # test_claude_shipped_default_is_the_user_profile_mapping.
            "claude": {
                profile: self._declared_point(claude_config, profile)
                for profile in ("deep", "balanced-deep", "balanced", "light", "mini")
            },
            # Codex expectations derive from its shipped config for the same
            # reason as Claude's; the concrete contract is asserted in
            # test_codex_shipped_default_is_the_user_profile_mapping.
            "codex": {
                profile: self._declared_point(codex_config, profile)
                for profile in ("deep", "balanced-deep", "balanced", "light", "mini")
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
                    "CFG_TIER_DEEP_MODEL=gpt-5.6-sol",
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
                    adapter, "deep", dispatch_depth=1, worker_type="owner", model_role=None
                ))
                self.assertEqual(resolved["role"], "_kernel/owner")
                self.assertEqual(resolved["profile"], "deep")

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


if __name__ == "__main__":
    unittest.main()
