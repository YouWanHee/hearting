#!/usr/bin/env python3
from __future__ import annotations

import json
import importlib.util
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_model_config", ROOT / "utilities" / "model_config.py"
)
assert SPEC and SPEC.loader
config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = config
SPEC.loader.exec_module(config)


BASE = (
    'CFG_MODEL_PROFILE_DEEP=deep:xhigh\n'
    'CFG_TIER_DEEP_MODEL=shipped-model\n'
    'CFG_TIER_DEEP_EFFORT=xhigh\n'
)


class ModelConfigTest(unittest.TestCase):
    def make_root(self, adapter="claude", shipped=BASE):
        root = Path(tempfile.mkdtemp())
        shipped_path = root / "adapters" / adapter / "config" / "models.conf"
        shipped_path.parent.mkdir(parents=True)
        shipped_path.write_text(shipped, encoding="utf-8")
        return root

    def test_each_runtime_home_is_independent(self):
        for adapter, variable, suffix in (
            ("claude", "CLAUDE_CONFIG_DIR", "claude"),
            ("codex", "CODEX_HOME", "codex"),
            ("opencode", "XDG_CONFIG_HOME", "opencode"),
        ):
            with self.subTest(adapter=adapter):
                environ = {"HOME": "/tmp/isolated-home", variable: f"/tmp/{suffix}"}
                expected = Path(f"/tmp/{suffix}")
                if adapter == "opencode":
                    expected /= "opencode"
                self.assertEqual(config.user_path(adapter, environ=environ), expected / "agent-config/models.conf")

    def test_valid_user_file_wins_as_a_complete_file(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(BASE + 'CFG_USER_EXTRA="literal"\n', encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "user")
        self.assertEqual(receipt.reason, "user-valid")
        self.assertEqual(values["CFG_USER_EXTRA"], "literal")

    def test_a_tier_the_adapter_wrappers_read_by_name_stays_required(self):
        # Review R2-B1: the role mappers read CFG_TIER_DEEP_MODEL/EFFORT (and
        # light/mini) directly after matching CFG_ROLES_*, so a copy that routes
        # every profile through model/<id>:effort still needs them.
        shipped = (
            'CFG_MODEL_PROFILE_DEEP=deep:high\n'
            'CFG_TIER_DEEP_MODEL=shipped-deep\n'
            'CFG_TIER_DEEP_EFFORT=high\n'
        )
        root = self.make_root(shipped=shipped)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        # Explicit-model profile: nothing in the file references the deep tier…
        user.write_text('CFG_MODEL_PROFILE_DEEP=model/user-model:high\nCFG_TIER_DEEP_EFFORT=high\n', encoding="utf-8")
        _values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("shipped", "user-incomplete"))
        # …and the same copy is complete once it declares the key the mapper reads.
        user.write_text('CFG_MODEL_PROFILE_DEEP=model/user-model:high\nCFG_TIER_DEEP_EFFORT=high\nCFG_TIER_DEEP_MODEL=user-deep\n', encoding="utf-8")
        _values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))

    def test_declared_wrapper_tiers_cover_every_literal_key_the_wrappers_read(self):
        """Drift guard for required tiers and declared optional fallbacks.

        Scope (deliberately narrow, review R4-M1): it reads `.sh` and `.py` files
        directly under each adapter's `bin/` and matches fully written key names.
        A wrapper that assembles the key at runtime (`CFG_TIER_${tier}_MODEL`),
        lives under another extension, or sits outside `bin/` is NOT covered —
        such a consumer must be added to the table by hand. It also does not tell
        a real read from a mention in a comment, which is the safe direction.
        """
        literal = re.compile(r"CFG_TIER_([A-Z0-9_]+)_(?:MODEL|EFFORT|VARIANT)\b")
        for adapter, declared in config.WRAPPER_REQUIRED_TIERS.items():
            with self.subTest(adapter=adapter):
                found = set()
                fallbacks = config.WRAPPER_FALLBACK_TIERS.get(adapter, {})
                self.assertFalse(declared & fallbacks.keys())
                bin_dir = ROOT / "adapters" / adapter / "bin"
                self.assertTrue(bin_dir.is_dir(), bin_dir)
                for path in sorted(bin_dir.iterdir()):
                    if path.is_file() and path.suffix in (".sh", ".py"):
                        contents = path.read_text(encoding="utf-8", errors="replace")
                        found.update(literal.findall(contents))
                        for tier, fallback in fallbacks.items():
                            self.assertIn(fallback, declared)
                            for suffix in ("MODEL", "EFFORT", "VARIANT"):
                                key = f"CFG_TIER_{tier}_{suffix}"
                                guarded = "${" + key + ":-$CFG_TIER_" + fallback + "_" + suffix + "}"
                                self.assertNotRegex(contents.replace(guarded, ""), rf"\b{key}\b",
                                                    f"{path.name} reads optional {key} without its required fallback")
                self.assertEqual(found - declared - fallbacks.keys(), set(),
                                 f"{adapter} wrappers read an undeclared tier")
                # Each declared tier must ship the exact keys a wrapper reads —
                # a failover/cascade-only mention is not enough.
                shipped = config.parse_config(config.shipped_path(adapter, source_root=ROOT))
                budget = "VARIANT" if adapter == "opencode" else "EFFORT"
                for tier in declared:
                    required = {f"CFG_TIER_{tier}_MODEL", f"CFG_TIER_{tier}_{budget}"}
                    self.assertLessEqual(required, shipped.keys(), f"{adapter} tier {tier} is incomplete")

    def test_legacy_complete_user_copy_survives_a_shipped_tier_extension(self):
        # A release adds a tier (balanced-deep) with two new CFG_TIER_* keys. An
        # older user copy that never references that tier is still a complete
        # policy and stays selected whole-file (its main-only list included).
        shipped = (
            BASE
            + 'CFG_TIER_BALANCED_DEEP_MODEL=shipped-bd\n'
            + 'CFG_TIER_BALANCED_DEEP_EFFORT=high\n'
            + 'CFG_MODEL_PROFILE_BALANCED_DEEP=balanced-deep:high\n'
            + 'CFG_MAIN_SESSION_ONLY_MODELS=" "\n'
        )
        legacy_user = BASE + 'CFG_MODEL_PROFILE_BALANCED_DEEP=deep:high\nCFG_MAIN_SESSION_ONLY_MODELS="fable"\n'
        root = self.make_root(shipped=shipped)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(legacy_user, encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
        self.assertEqual(receipt.unreferenced_tier_keys, "CFG_TIER_BALANCED_DEEP_EFFORT,CFG_TIER_BALANCED_DEEP_MODEL")
        self.assertEqual(values["CFG_MAIN_SESSION_ONLY_MODELS"], "fable")
        self.assertNotIn("CFG_TIER_BALANCED_DEEP_MODEL", values)  # whole-file, never merged
        # The same copy that *references* the new tier without declaring it is incomplete.
        user.write_text(legacy_user.replace("balanced-deep:high", "balanced-deep:high").replace(
            "CFG_MODEL_PROFILE_BALANCED_DEEP=deep:high", "CFG_MODEL_PROFILE_BALANCED_DEEP=balanced-deep:high"), encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("shipped", "user-incomplete"))
        # A scalar tier selector counts as a reference too.
        user.write_text(legacy_user + "CFG_TIER_DEEP_FAILOVER=balanced-deep\n", encoding="utf-8")
        _values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.reason, "user-incomplete")
        # Missing non-tier keys are still incomplete (the exception is tier-shaped only).
        user.write_text(legacy_user.replace("CFG_MODEL_PROFILE_DEEP=deep:xhigh\n", ""), encoding="utf-8")
        _values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.reason, "user-incomplete")

    def test_missing_incomplete_malformed_and_unsafe_user_files_fallback_whole_file(self):
        for text, reason in ((None, "user-missing"), ("CFG_TIER_DEEP_MODEL=user-only\n", "user-incomplete"),
                             ("CFG_MODEL_PROFILE_DEEP=bad+syntax\n", "user-malformed"),
                             ("$(touch sentinel)\n" + BASE, "user-malformed")):
            with self.subTest(reason=reason):
                root = self.make_root()
                home = root / "home"
                user = home / "agent-config" / "models.conf"
                if text is not None:
                    user.parent.mkdir(parents=True)
                    user.write_text(text, encoding="utf-8")
                values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
                self.assertEqual(receipt.reason, reason)
                self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "shipped-model")

    def test_quoted_comments_extra_keys_and_no_merge(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text('CFG_MODEL_PROFILE_DEEP="deep:xhigh" # comment\nCFG_TIER_DEEP_MODEL="user-model ; $(touch sentinel) `echo nope` O\'Brien"\nCFG_TIER_DEEP_EFFORT=xhigh\n', encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "user")
        self.assertIn("$(touch sentinel)", values["CFG_TIER_DEEP_MODEL"])
        self.assertNotIn("CFG_UNDECLARED", values)

    def test_shipped_failure_is_fatal(self):
        root = self.make_root(shipped="$(bad)\n")
        with self.assertRaises(config.ShippedConfigError):
            config.resolve_config("claude", runtime=root / "home", source_root=root)

    def test_user_symlink_is_rejected_and_falls_back(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.symlink_to(root / "adapters" / "claude" / "config" / "models.conf")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual(receipt.source, "shipped")
        self.assertEqual(receipt.reason, "user-unreadable")
        self.assertEqual(values["CFG_TIER_DEEP_MODEL"], "shipped-model")

    def test_bridge_quotes_metacharacters_and_receipt(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(BASE.replace("shipped-model", '"$(touch sentinel); `echo bad` O\'Brien"'), encoding="utf-8")
        receipt = root / "receipt.json"
        command = [str(ROOT / "utilities" / "model-config.sh"), "--adapter", "claude", "--runtime-home", str(home), "--source-root", str(root), "--receipt-fd", "3"]
        result = subprocess.run(shlex.join(command) + f" 3>{shlex.quote(str(receipt))}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CFG_TIER_DEEP_MODEL='$(touch sentinel); `echo bad` O'\"'\"'Brien'", result.stdout)
        self.assertEqual(json.loads(receipt.read_text())["source"], "user")
        self.assertFalse((root / "sentinel").exists())


class TopProfileOptionalityTest(unittest.TestCase):
    def make_root(self, adapter="claude", shipped=BASE):
        root = Path(tempfile.mkdtemp())
        shipped_path = root / "adapters" / adapter / "config" / "models.conf"
        shipped_path.parent.mkdir(parents=True)
        shipped_path.write_text(shipped, encoding="utf-8")
        return root

    SHIPPED_WITH_TOP = (BASE + 'CFG_TIER_TOP_MODEL=shipped-top\nCFG_TIER_TOP_EFFORT=max\n'
                        'CFG_MODEL_PROFILE_TOP=top:max\nCFG_MODEL_PROFILE_GRANULARITY_TOP=full\n'
                        'CFG_MAIN_SESSION_ONLY_MODELS="shipped-top"\n')

    def test_a_user_copy_without_the_top_profile_stays_valid_and_collapses_top_onto_its_own_deep_tier(self):
        # 2026-10-06: the framed default shape seals its frame legs at `top`, so a
        # legacy copy that simply "had no top" refused every new piece of work
        # (`frame-harness-unavailable`, `top_undeclared=claude,codex,opencode`).
        # The copy stays the selected whole file and `top` is derived in memory as
        # a collapse onto the copy's OWN deep tier -- the shipped top model (which
        # the user never opted into) is not pulled in, and nothing is written back.
        legacy_user = BASE + 'CFG_MAIN_SESSION_ONLY_MODELS="fable"\n'
        root = self.make_root(shipped=self.SHIPPED_WITH_TOP)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(legacy_user, encoding="utf-8")
        from model_profile import ModelProfileError, resolve_profile_values
        # An ordinary resolution keeps the contract: no `top`, refused typed.
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason, receipt.top_provenance), ("user", "user-valid", "absent"))
        self.assertNotIn("CFG_MODEL_PROFILE_TOP", values)
        with self.assertRaises(ModelProfileError) as refused:
            resolve_profile_values("claude", values, "top")
        self.assertEqual(refused.exception.reason, "profile-top-undeclared")
        # The automatic frame path opts in, and only it sees the collapse.
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root, collapse_top=True)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
        self.assertEqual(receipt.unreferenced_tier_keys, "CFG_TIER_TOP_EFFORT,CFG_TIER_TOP_MODEL")
        self.assertEqual(receipt.top_provenance, "derived-from-user-deep")
        self.assertEqual(values["CFG_MODEL_PROFILE_TOP"], "deep:xhigh")
        self.assertEqual(values["CFG_MODEL_PROFILE_GRANULARITY_TOP"], "collapsed-top-to-deep")
        self.assertNotIn("CFG_TIER_TOP_MODEL", values)  # whole-file: the shipped top tier never merges in
        resolved = resolve_profile_values("claude", values, "top")
        self.assertEqual((resolved["tier"], resolved["model"], resolved["budget"], resolved["granularity"]),
                         ("deep", "shipped-model", "xhigh", "collapsed-top-to-deep"))
        self.assertEqual(user.read_text(encoding="utf-8"), legacy_user)  # never written back
        self.assertEqual(json.loads(json.dumps(receipt.as_dict()))["top_provenance"], "derived-from-user-deep")
        # a copy that opts in without declaring the tier stays selected whole-file,
        # and resolving `top` on it refuses typed instead (top review B2 (iii)):
        # an explicit row is the user's declaration, never second-guessed by a collapse.
        user.write_text(legacy_user + "CFG_MODEL_PROFILE_TOP=top:max\n", encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root, collapse_top=True)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
        self.assertEqual(receipt.top_provenance, "explicit")
        with self.assertRaises(ModelProfileError) as refused:
            resolve_profile_values("claude", values, "top")
        self.assertEqual(refused.exception.reason, "profile-top-undeclared")

    def test_the_shipped_file_and_an_explicit_user_top_report_explicit_provenance(self):
        root = self.make_root(shipped=self.SHIPPED_WITH_TOP)
        home = root / "home"
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason, receipt.top_provenance), ("shipped", "user-missing", "explicit"))
        self.assertEqual(values["CFG_MODEL_PROFILE_TOP"], "top:max")
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(self.SHIPPED_WITH_TOP, encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.top_provenance), ("user", "explicit"))
        self.assertEqual(values["CFG_MODEL_PROFILE_GRANULARITY_TOP"], "full")

    def test_a_legacy_opencode_copy_collapses_top_onto_its_own_deep_and_the_fallback_is_balanced_deep(self):
        # A complete OpenCode copy seeded before the shipped file declared `top` keeps its own
        # deep profile, so the collapse lands there. The balanced-deep rung is the fallback for
        # values with no deep profile at all -- unreachable for a *complete* copy (the deep profile
        # row is required), so it is exercised on the pure function.
        shipped_text = (ROOT / "adapters" / "opencode" / "config" / "models.conf").read_text(encoding="utf-8")
        legacy = "\n".join(
            line for line in shipped_text.splitlines()
            if not line.startswith(("CFG_MODEL_PROFILE_TOP=", "CFG_MODEL_PROFILE_GRANULARITY_TOP="))
        ) + "\n"
        root = self.make_root("opencode", shipped_text)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(legacy, encoding="utf-8")
        values, receipt = config.resolve_config("opencode", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.top_provenance), ("user", "absent"), receipt)
        values, receipt = config.resolve_config("opencode", runtime=home, source_root=root, collapse_top=True)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"), receipt)
        self.assertEqual(receipt.top_provenance, "derived-from-user-deep")
        self.assertEqual(values["CFG_MODEL_PROFILE_TOP"], values["CFG_MODEL_PROFILE_DEEP"])
        # the record names the tier the collapse lands on; the shipped OpenCode deep profile
        # is itself collapsed onto balanced-deep, so that is where `top` lands too
        deep_tier = values["CFG_MODEL_PROFILE_DEEP"].split(":", 1)[0]
        self.assertEqual(values["CFG_MODEL_PROFILE_GRANULARITY_TOP"], f"collapsed-top-to-{deep_tier}")
        from model_profile import resolve_profile_values
        self.assertEqual(resolve_profile_values("opencode", values, "top")["tier"], deep_tier)
        without_deep = {k: v for k, v in values.items()
                        if k not in {"CFG_MODEL_PROFILE_TOP", "CFG_MODEL_PROFILE_GRANULARITY_TOP",
                                     "CFG_MODEL_PROFILE_DEEP", "CFG_TIER_DEEP_MODEL", "CFG_TIER_DEEP_VARIANT"}}
        derived, provenance = config._derive_top_values("opencode", without_deep)
        self.assertEqual(provenance, "derived-from-user-balanced-deep")
        self.assertEqual(derived["CFG_MODEL_PROFILE_TOP"], values["CFG_MODEL_PROFILE_BALANCED_DEEP"])
        self.assertEqual(derived["CFG_MODEL_PROFILE_GRANULARITY_TOP"], "collapsed-top-to-balanced-deep")
        self.assertEqual(resolve_profile_values("opencode", derived, "top")["tier"], "balanced-deep")

    def test_the_collapse_prefers_the_copies_own_deep_profile_and_a_copy_with_neither_stays_absent(self):
        shipped = self.SHIPPED_WITH_TOP
        root = self.make_root(shipped=shipped)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(BASE + 'CFG_MODEL_PROFILE_BALANCED_DEEP=deep:medium\nCFG_MAIN_SESSION_ONLY_MODELS=" "\n', encoding="utf-8")
        values, receipt = config.resolve_config("claude", runtime=home, source_root=root, collapse_top=True)
        self.assertEqual((receipt.top_provenance, values["CFG_MODEL_PROFILE_TOP"]), ("derived-from-user-deep", "deep:xhigh"))
        # the derivation is a pure function of the values: nothing to collapse onto -> absent, unchanged
        bare = {"CFG_MODEL_PROFILE_LIGHT": "light:medium", "CFG_TIER_LIGHT_MODEL": "m", "CFG_TIER_LIGHT_EFFORT": "medium"}
        derived, provenance = config._derive_top_values("claude", bare)
        self.assertEqual((derived, provenance), (bare, "absent"))

    def test_an_opencode_copy_without_the_deep_granularity_key_stays_selected(self):
        # 2026-09-30: the shipped OpenCode file started naming its deep collapse
        # (`CFG_MODEL_PROFILE_GRANULARITY_DEEP`). That key is receipt metadata, so a
        # complete copy seeded before it must keep being the user's selected file
        # instead of being replaced whole by the shipped one.
        shipped_text = (ROOT / "adapters" / "opencode" / "config" / "models.conf").read_text(encoding="utf-8")
        self.assertIn("CFG_MODEL_PROFILE_GRANULARITY_DEEP=", shipped_text)
        legacy = "\n".join(
            line for line in shipped_text.splitlines()
            if not line.startswith("CFG_MODEL_PROFILE_GRANULARITY_DEEP=")
        ) + "\n"
        root = self.make_root("opencode", shipped_text)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(legacy, encoding="utf-8")
        values, receipt = config.resolve_config("opencode", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
        from model_profile import resolve_profile_values
        self.assertEqual(resolve_profile_values("opencode", values, "deep")["granularity"], "full")
        # Any other missing shipped key still means an incomplete copy.
        user.write_text(legacy.replace("CFG_MODEL_PROFILE_MINI=", "#CFG_MODEL_PROFILE_MINI="), encoding="utf-8")
        _values, receipt = config.resolve_config("opencode", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("shipped", "user-incomplete"))

    def test_a_codex_copy_without_the_main_only_key_stays_selected_whole_file(self):
        # top review B2: a policy key a release added must not turn an older
        # complete codex copy into `user-incomplete` (a silent whole-file
        # replacement of the user's policy).
        shipped = BASE + 'CFG_MAIN_SESSION_ONLY_MODELS="shipped-top"\n'
        legacy_user = BASE.replace("CFG_TIER_DEEP_MODEL=", "CFG_TIER_DEEP_MODEL=user-custom-") if "CFG_TIER_DEEP_MODEL=" in BASE else BASE
        root = self.make_root(adapter="codex", shipped=shipped)
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(legacy_user, encoding="utf-8")
        values, receipt = config.resolve_config("codex", runtime=home, source_root=root)
        self.assertEqual((receipt.source, receipt.reason), ("user", "user-valid"))
        self.assertNotIn("CFG_MAIN_SESSION_ONLY_MODELS", values)   # absent, never merged
        # the same omission on claude is still incomplete (the key is required there)
        root2 = self.make_root(adapter="claude", shipped=shipped)
        home2 = root2 / "home"
        (home2 / "agent-config").mkdir(parents=True)
        (home2 / "agent-config" / "models.conf").write_text(legacy_user, encoding="utf-8")
        _v, receipt2 = config.resolve_config("claude", runtime=home2, source_root=root2)
        self.assertEqual(receipt2.reason, "user-incomplete")

    def test_every_optional_policy_key_is_shipped_and_registered_by_hand(self):
        # combined review m1: the optional-key registration is hand-written,
        # so pin what it must satisfy -- the key exists in that adapter's
        # shipped file (otherwise the entry is dead), and a user copy without
        # it stays selected whole-file with the key absent (the consumer must
        # tolerate that, which is why the Claude key is deliberately absent
        # from this map: its wrapper raises when the key is missing).
        # Absence *tolerance* itself is asserted at the consumer, not here:
        # `codex_dispatch_model_eligibility.test.py`
        # ::test_a_user_copy_without_the_key_carries_no_restriction
        # (guard review m5).
        root = config.repository_root()
        for adapter, keys in config.OPTIONAL_POLICY_KEYS.items():
            shipped = config.parse_config(config.shipped_path(adapter, source_root=root))
            for key in keys:
                with self.subTest(adapter=adapter, key=key):
                    self.assertIn(key, shipped)
        self.assertNotIn("claude", config.OPTIONAL_POLICY_KEYS)
        # and a shipped key that is registered nowhere still makes a copy
        # incomplete -- the deliberate default this map is an exception to
        shipped_text = BASE + 'CFG_SOME_NEW_POLICY_KEY=value\n'
        root2 = self.make_root(shipped=shipped_text)
        home = root2 / "home"
        (home / "agent-config").mkdir(parents=True)
        (home / "agent-config" / "models.conf").write_text(BASE, encoding="utf-8")
        _values, receipt = config.resolve_config("claude", runtime=home, source_root=root2)
        self.assertEqual(receipt.reason, "user-incomplete")

    def test_inheritance_is_refused_exactly_when_a_main_only_model_is_declared(self):
        # guard review m1 pinned that opencode declares no main-session-only
        # list, because its wrapper allowed inheritance on that ground alone.
        # The rule now reads the list itself on every adapter, so declaring
        # the key closes inheritance with no wrapper edit.
        for adapter in config.ADAPTERS:
            shipped = config.parse_config(config.shipped_path(adapter, source_root=config.repository_root()))
            with self.subTest(adapter=adapter):
                refusal = config.inheritance_refusal(shipped, adapter)
                self.assertEqual(refusal is None, not config.main_session_only_models(shipped))

    def test_restricted_model_matches_whole_ids_and_alias_tokens(self):
        self.assertTrue(config.restricted_model("claude-fable-5-1", "fable"))
        self.assertTrue(config.restricted_model("fable", ["fable"]))
        self.assertTrue(config.restricted_model("gpt-6-astra", "gpt-6-astra"))
        self.assertTrue(config.restricted_model("GPT-6-Astra", ["gpt-6-astra"]))
        self.assertFalse(config.restricted_model("gpt-6-astra-mini", "gpt-6-astra"))  # whole id only
        self.assertFalse(config.restricted_model("gpt-5.6-sol", "gpt-6-astra"))
        self.assertFalse(config.restricted_model("opus", " "))
        self.assertFalse(config.restricted_model("claude-opus-5", ["fable"]))


if __name__ == "__main__":
    unittest.main()
