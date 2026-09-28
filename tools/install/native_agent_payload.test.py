#!/usr/bin/env python3
"""Codex native agents follow the effective user model config.

Every test runs inside ``fixture_env.patched_environment``: HOME, CODEX_HOME,
CLAUDE_CONFIG_DIR, XDG_* and AGENT_HOME point into a temporary fixture root, so
no real runtime home, installed release, or running process is read or touched.
Model ids here are synthetic; shipped values are read from this checkout.
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import fixture_env  # noqa: E402
import native_agent_payload as payload  # noqa: E402
import runtime_activation  # noqa: E402
from drivers import codex as codex_driver  # noqa: E402

SHIPPED_CONF = ROOT / "adapters" / "codex" / "config" / "models.conf"
SHIPPED_AGENTS = ROOT / "adapters" / "codex" / "agents"


def shipped_model(agent: str) -> str:
    text = (SHIPPED_AGENTS / agent).read_text(encoding="utf-8")
    return re.search(r'^model = "([^"]+)"$', text, re.MULTILINE).group(1)


def agent_model(path: Path) -> str:
    return re.search(r'^model = "([^"]+)"$', path.read_text(encoding="utf-8"), re.MULTILINE).group(1)


class _FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.fixture = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.env = stack.enter_context(fixture_env.patched_environment(self.fixture, ROOT))
        self.home = Path(self.env["CODEX_HOME"])
        self.home.mkdir(parents=True, exist_ok=True)
        self.user_conf = self.home / "agent-config" / "models.conf"

    def write_user_config(self, overrides=None, drop=(), extra=""):
        overrides = dict(overrides or {})
        lines = []
        for raw in SHIPPED_CONF.read_text(encoding="utf-8").splitlines():
            key = raw.split("=", 1)[0].strip()
            if key in drop:
                continue
            if key in overrides:
                raw = f"{key}={overrides.pop(key)}"
            lines.append(raw)
        lines.extend(f"{key}={value}" for key, value in overrides.items())
        self.user_conf.parent.mkdir(parents=True, exist_ok=True)
        self.user_conf.write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")

    def plan(self):
        return payload.plan_payload(self.home, source_root=ROOT)


class PlanTest(_FixtureCase):
    def test_user_models_reach_the_rendered_agents(self) -> None:
        self.write_user_config({
            "CFG_TIER_DEEP_MODEL": "fixture-deep",
            "CFG_TIER_LIGHT_MODEL": "fixture-light",
            "CFG_TIER_MINI_MODEL": "fixture-light",
        })
        plan = self.plan()
        self.assertTrue(plan.active, plan.report())
        self.assertEqual(plan.config_source, "user")
        self.assertIn('model = "fixture-deep"', plan.files["deep.toml"])
        self.assertIn('model = "fixture-deep"', plan.files["general-purpose.toml"])
        self.assertIn('model = "fixture-light"', plan.files["light.toml"])
        self.assertIn('model = "fixture-light"', plan.files["balanced.toml"])
        self.assertIn('model = "fixture-light"', plan.files["memory-scout.toml"])
        self.assertEqual(sorted(plan.files), sorted(p.name for p in SHIPPED_AGENTS.glob("*.toml")))
        self.assertFalse((self.home / ".harness").exists(), "planning must never write")

        created = payload.materialize_payload(plan)
        self.assertEqual(created["status"], "created")
        links = plan.links()
        for name, target in links.items():
            self.assertEqual(target.read_text(encoding="utf-8"), plan.files[name])
            self.assertTrue(payload.owned_payload_file(self.home, target))
        self.assertEqual(agent_model(links["deep.toml"]), "fixture-deep")
        self.assertEqual(payload.materialize_payload(plan)["status"], "unchanged")
        self.assertTrue(payload.check_payload(self.home, source_root=ROOT)["ok"])

        links["deep.toml"].write_text("tampered\n", encoding="utf-8")
        self.assertFalse(payload.owned_payload_file(self.home, links["deep.toml"]))
        self.assertFalse(payload.check_payload(self.home, source_root=ROOT)["ok"])
        with self.assertRaises(payload.PayloadError):
            payload.materialize_payload(plan)

    def test_absent_user_file_keeps_shipped_profiles(self) -> None:
        plan = self.plan()
        self.assertFalse(plan.active)
        self.assertEqual((plan.config_source, plan.config_reason), ("shipped", "user-missing"))
        self.assertEqual(plan.links(), {})
        self.assertEqual(payload.materialize_payload(plan)["status"], "inactive")
        self.assertFalse((self.home / ".harness").exists())
        self.assertTrue(payload.check_payload(self.home, source_root=ROOT)["ok"])

    def test_invalid_user_file_keeps_shipped_profiles(self) -> None:
        cases = {
            "user-malformed": lambda: self.user_conf.write_text("not a cfg line\n", encoding="utf-8"),
            "user-incomplete": lambda: self.write_user_config(
                {"CFG_TIER_DEEP_MODEL": "fixture-deep"}, drop={"CFG_TIER_LIGHT_MODEL"}),
        }
        self.user_conf.parent.mkdir(parents=True, exist_ok=True)
        for reason, write in cases.items():
            with self.subTest(reason=reason):
                write()
                plan = self.plan()
                self.assertFalse(plan.active)
                self.assertEqual((plan.config_source, plan.config_reason), ("shipped", reason))
                self.assertEqual(plan.links(), {})

    def test_seeded_copy_of_shipped_links_shipped_profiles(self) -> None:
        self.write_user_config()
        plan = self.plan()
        self.assertFalse(plan.active)
        self.assertEqual(plan.reason, "user-config-matches-shipped")
        self.assertEqual(plan.config_source, "user")

    def test_unrenderable_user_config_falls_back_whole(self) -> None:
        self.write_user_config({
            "CFG_TIER_DEEP_MODEL": "fixture-deep",
            "CFG_NATIVE_AGENT_CATALOG": '"deep:deep fixture-unknown:light"',
        })
        plan = self.plan()
        self.assertFalse(plan.active)
        self.assertTrue(plan.reason.startswith("user-config-unrenderable"), plan.reason)


class MainSessionOnlyFilterTest(_FixtureCase):
    def test_main_only_model_never_enters_a_payload(self) -> None:
        self.write_user_config({
            "CFG_MAIN_SESSION_ONLY_MODELS": '"fixture-top"',
            "CFG_TIER_DEEP_MODEL": "fixture-top",
            "CFG_TIER_LIGHT_MODEL": "fixture-light",
            "CFG_TIER_MINI_MODEL": "fixture-light",
        })
        plan = self.plan()
        self.assertTrue(plan.active)
        self.assertEqual(plan.main_session_only_policy, "declared")
        for body in plan.files.values():
            self.assertNotIn("fixture-top", body)
        self.assertEqual(plan.files["deep.toml"], (SHIPPED_AGENTS / "deep.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            {item["agent"]: item["resolution"] for item in plan.main_only},
            {"deep.toml": "shipped-profile", "general-purpose.toml": "shipped-profile"},
        )
        self.assertIn('model = "fixture-light"', plan.files["light.toml"])
        self.assertEqual(plan.metadata()["main_only"], [dict(item) for item in plan.main_only])

        actions = codex_driver._native_agent_payload_actions("global", dry_run=True)
        reported = {a["dest"].rsplit("/", 1)[-1]: a for a in actions if a["action"] == "native-agent-main-only"}
        self.assertEqual(set(reported), {"deep.toml", "general-purpose.toml"})
        self.assertIn("fixture-top is main-session-only", reported["deep.toml"]["detail"])
        self.assertEqual(actions[0]["status"], "planned")

    def test_filter_alone_keeps_the_shipped_files_and_reports(self) -> None:
        self.write_user_config({
            "CFG_MAIN_SESSION_ONLY_MODELS": '"fixture-top"',
            "CFG_TIER_DEEP_MODEL": "fixture-top",
        })
        plan = self.plan()
        self.assertFalse(plan.active)
        self.assertEqual(plan.reason, "user-config-matches-shipped")
        self.assertEqual({item["agent"] for item in plan.main_only}, {"deep.toml", "general-purpose.toml"})

    def test_agent_withheld_when_its_shipped_model_is_main_only_too(self) -> None:
        shipped_deep = shipped_model("deep.toml")
        self.write_user_config({
            "CFG_MAIN_SESSION_ONLY_MODELS": f'"fixture-top {shipped_deep}"',
            "CFG_TIER_DEEP_MODEL": "fixture-top",
        })
        plan = self.plan()
        self.assertTrue(plan.active)
        self.assertNotIn("deep.toml", plan.files)
        self.assertNotIn("general-purpose.toml", plan.files)
        self.assertEqual({item["resolution"] for item in plan.main_only}, {"withheld"})
        for body in plan.files.values():
            self.assertNotIn(shipped_deep, body)

    def test_absent_policy_key_leaves_user_models_unrestricted(self) -> None:
        self.write_user_config({"CFG_TIER_DEEP_MODEL": "fixture-top"}, drop={"CFG_MAIN_SESSION_ONLY_MODELS"})
        plan = self.plan()
        self.assertTrue(plan.active)
        self.assertEqual(plan.main_session_only_policy, "absent")
        self.assertEqual(plan.main_only, ())
        self.assertIn('model = "fixture-top"', plan.files["deep.toml"])


class InstallSurfaceTest(_FixtureCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(codex_driver.codex_launcher, "uninstall",
                                    return_value={"action": "managed-launcher", "status": "unchanged"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_driver_links_agents_to_the_payload(self) -> None:
        self.write_user_config({"CFG_TIER_LIGHT_MODEL": "fixture-light", "CFG_TIER_MINI_MODEL": "fixture-light"})
        result = codex_driver.install(plugin=False, dry_run=False)
        self.assertFalse(result["blocked"], [a for a in result["actions"] if a.get("status") == "blocked"])
        link = self.home / "agents" / "light.toml"
        self.assertTrue(link.is_symlink())
        self.assertTrue(payload.owned_payload_file(self.home, link.resolve()))
        self.assertEqual(agent_model(link), "fixture-light")
        self.assertEqual(agent_model(self.home / "agents" / "deep.toml"), shipped_model("deep.toml"))
        checks = {c()["id"]: c() for c in codex_driver.checks() if callable(c) and getattr(c, "__name__", "") != "_bootstrap_smoke"}
        self.assertTrue(checks["codex.native-agent-payload"]["ok"], checks["codex.native-agent-payload"])
        self.assertTrue(checks["codex.symlink.agents.light.toml"]["ok"], checks["codex.symlink.agents.light.toml"])

        # A later config edit leaves the installed link owned (uninstall stays
        # unblocked) while the check asks for a reinstall.
        self.write_user_config({"CFG_TIER_LIGHT_MODEL": "fixture-other", "CFG_TIER_MINI_MODEL": "fixture-other"})
        self.assertFalse(payload.check_payload(self.home, source_root=ROOT)["ok"])
        self.assertTrue(payload.owned_agent_target(self.home, ROOT, "light.toml", link.resolve()))
        self.assertTrue(payload.owned_agent_target(self.home, ROOT, "deep.toml", SHIPPED_AGENTS / "deep.toml"))
        self.assertFalse(payload.owned_agent_target(self.home, ROOT, "deep.toml", link.resolve()))

    def test_default_install_keeps_shipped_links(self) -> None:
        result = codex_driver.install(plugin=False, dry_run=False)
        self.assertFalse(result["blocked"])
        self.assertEqual((self.home / "agents" / "deep.toml").resolve(), (SHIPPED_AGENTS / "deep.toml").resolve())
        self.assertFalse((self.home / ".harness" / "native-agents").exists())
        status = next(a for a in result["actions"] if a["action"] == "native-agent-payload")["status"]
        self.assertEqual(status, "inactive")

    def test_activation_and_refresh_follow_the_user_config(self) -> None:
        self.write_user_config({"CFG_TIER_DEEP_MODEL": "fixture-deep"})
        report = runtime_activation.activate("codex", "linked", source=str(ROOT))
        link = self.home / "agents" / "deep.toml"
        self.assertEqual(agent_model(link), "fixture-deep")
        self.assertTrue(report["native_agent_payload"]["active"], report["native_agent_payload"])
        self.assertEqual(report["freshness"], "fresh", report)

        self.write_user_config({"CFG_TIER_DEEP_MODEL": "fixture-deep-2"})
        self.assertEqual(runtime_activation.status("codex")["freshness"], "cache-stale")
        runtime_activation.refresh("codex")
        self.assertEqual(agent_model(link), "fixture-deep-2")
        self.assertEqual(runtime_activation.status("codex")["freshness"], "fresh")

        self.user_conf.write_text("not a cfg line\n", encoding="utf-8")
        refreshed = runtime_activation.refresh("codex")
        self.assertEqual(link.resolve(), (SHIPPED_AGENTS / "deep.toml").resolve())
        self.assertFalse(refreshed["native_agent_payload"]["active"])
        self.assertEqual(refreshed["freshness"], "fresh", json.dumps(refreshed, indent=1))

        runtime_activation.deactivate("codex")
        self.assertFalse(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
