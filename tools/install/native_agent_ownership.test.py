#!/usr/bin/env python3
"""Old Codex payload migration and native-agent projection regressions.

The fixture layout and identity fields are literal transcriptions of
de5ae20f2:tools/install/native_agent_payload.py (PayloadPlan.identity) and
d9e66ad84:tools/install/native_agent_payload.py (owned_payload_file).
Expected metadata is deliberately independent of the new production reader.
"""

from argparse import Namespace
from contextlib import chdir
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import fixture_env  # noqa: E402
import installer  # noqa: E402
import manifest  # noqa: E402
import native_agent_ownership as ownership  # noqa: E402
import runtime_activation as activation  # noqa: E402
from drivers import codex  # noqa: E402


class NativeAgentOwnershipTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-owner-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = fixture_env.patched_environment(self.root / "fixture", ROOT)
        self.env = environment.__enter__()
        self.addCleanup(environment.__exit__, None, None, None)
        self.home = Path(self.env["CODEX_HOME"])
        (self.home / "agents").mkdir(parents=True, exist_ok=True)

    def source(self, name="deep.toml"):
        return ROOT / "adapters" / "codex" / "agents" / name

    def dest(self, name="deep.toml"):
        return self.home / "agents" / name

    def release_fixture(self, name="deep.toml", *, catalog=None):
        old_root = self.root / "fixture" / "old-release"
        old_agent = old_root / "adapters/codex/agents" / name
        old_agent.parent.mkdir(parents=True)
        old_agent.write_bytes(self.source("deep.toml").read_bytes())
        shutil.copy2(ROOT / "harness-manifest.json", old_root / "harness-manifest.json")
        (old_root / "core").mkdir()
        shutil.copy2(ROOT / "core/CORE.md", old_root / "core/CORE.md")
        shutil.copytree(ROOT / "capabilities", old_root / "capabilities")
        shutil.copytree(ROOT / "roles/units", old_root / "roles/units")
        config = old_root / "adapters/codex/config/models.conf"
        config.parent.mkdir(parents=True)
        source_config = (ROOT / "adapters/codex/config/models.conf").read_text()
        if catalog is not None:
            source_config = re.sub(r'^CFG_NATIVE_AGENT_CATALOG=.*$',
                                   f'CFG_NATIVE_AGENT_CATALOG="{catalog}"',
                                   source_config, flags=re.M)
        config.write_text(source_config)
        return old_agent

    def old_payload(self, name="deep.toml", body=b'model = "old-user-model"\n', extra=None):
        contents = {name: body, **(extra or {})}
        identity = {
            "schema": "hearting.native-agent-payload/v1",
            "runtime": "codex",
            "renderer_version": "1",
            "config_source": "user",
            "config_reason": "user-config-valid",
            "main_session_only_policy": "absent",
            "main_only": [],
            "files": {key: hashlib.sha256(value).hexdigest()
                      for key, value in contents.items()},
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
        directory = self.home / ".harness" / "native-agents" / digest
        directory.mkdir(parents=True)
        target = directory / name
        for key, value in contents.items():
            (directory / key).write_bytes(value)
        (directory / "metadata.json").write_text(json.dumps({**identity, "digest": digest}))
        return target

    def driver(self, name="deep.toml", *, dry_run=False):
        entry = {"action": "symlink", "native_agent": True,
                 "source": str(self.source(name)), "dest": str(self.dest(name))}
        with (mock.patch.object(codex.projector, "plan", return_value={"codex": [entry]}),
              mock.patch.object(codex, "_plugin_action", return_value={"action": "plugin", "status": "skipped"}),
              mock.patch.object(codex.codex_launcher, "uninstall", return_value={"action": "managed-launcher", "status": "unchanged"}),
              mock.patch.object(codex.manifest, "record", return_value={})):
            result = codex.install(dry_run=dry_run)
        return next(action for action in result["actions"] if action.get("dest") == str(self.dest(name)))

    def test_public_driver_preview_and_actual_preserve_foreign_and_migrate_owned(self):
        target = self.old_payload()
        dest = self.dest()
        dest.symlink_to(target)
        self.assertEqual(self.driver(dry_run=True)["status"], "planned")
        self.assertEqual(os.readlink(dest), str(target))
        self.assertEqual(self.driver()["status"], "updated")
        self.assertEqual(os.readlink(dest), str(self.source()))
        self.assertEqual(target.read_bytes(), b'model = "old-user-model"\n')
        self.assertEqual(self.driver(dry_run=True)["status"], "unchanged")
        self.assertEqual(self.driver()["status"], "unchanged")

        cases = ("foreign", "relative", "dangling", "regular", "payload-bytes",
                 "metadata", "file-and-metadata", "broken", "symlink-metadata")
        for case in cases:
            with self.subTest(case=case):
                name = "light.toml"
                leaf = self.dest(name)
                if leaf.is_symlink() or leaf.exists():
                    leaf.unlink()
                if case in {"payload-bytes", "metadata", "file-and-metadata", "broken", "symlink-metadata"}:
                    old = self.old_payload(name, body=case.encode())
                    leaf.symlink_to(old)
                    metadata = old.parent / "metadata.json"
                    if case in {"payload-bytes", "file-and-metadata"}:
                        old.write_bytes(b"changed")
                    if case in {"metadata", "file-and-metadata"}:
                        data = json.loads(metadata.read_text())
                        data["config_reason"] = "forged"
                        if case == "file-and-metadata":
                            data["files"][name] = hashlib.sha256(old.read_bytes()).hexdigest()
                        metadata.write_text(json.dumps(data))
                    if case == "broken":
                        old.unlink()
                    if case == "symlink-metadata":
                        copy = self.root / "metadata-copy"
                        copy.write_bytes(metadata.read_bytes())
                        metadata.unlink()
                        metadata.symlink_to(copy)
                elif case == "regular":
                    leaf.write_bytes(b"user file")
                else:
                    external = self.root / "external.toml"
                    external.write_bytes(b"external")
                    value = ({"foreign": external, "relative": Path(os.path.relpath(external, leaf.parent)),
                              "dangling": self.root / "missing.toml"})[case]
                    leaf.symlink_to(value)
                prior = os.readlink(leaf) if leaf.is_symlink() else leaf.read_bytes()
                self.assertEqual(self.driver(name, dry_run=True)["status"], "preserved")
                self.assertEqual(self.driver(name)["status"], "preserved")
                self.assertEqual(os.readlink(leaf) if leaf.is_symlink() else leaf.read_bytes(), prior)

    def test_prior_shipped_release_link_updates_to_current_release(self):
        old_agent = self.release_fixture()
        self.dest().symlink_to(old_agent)
        self.assertEqual(self.driver(dry_run=True)["status"], "planned")
        self.assertEqual(os.readlink(self.dest()), str(old_agent))
        self.assertEqual(self.driver()["status"], "updated")
        self.assertEqual(os.readlink(self.dest()), str(self.source()))

    def test_prior_shipped_mirror_and_removed_catalog_agent_remain_owned(self):
        old_agent = self.release_fixture("obsolete.toml", catalog="obsolete:deep")
        mirror_dir = old_agent.parents[3] / "codex_setting/codex-agents"
        mirror_dir.parent.mkdir(parents=True)
        mirror_dir.symlink_to("../adapters/codex/agents", target_is_directory=True)
        mirror = mirror_dir / old_agent.name
        dest = self.dest("obsolete.toml")
        dest.symlink_to(mirror)
        self.assertTrue(ownership.owned_link(self.home, dest))
        registry = manifest._manifest_path("codex", "global")
        manifest._write_manifest(registry, {"schema": 1, "runtime": "codex", "scope": "global",
                                            "version": "old", "timestamp": "fixture", "files": {}})
        args = Namespace(runtimes=["codex"], target="codex", scope="global", dry_run=True)
        with (mock.patch.object(installer.projector, "plan", return_value={"codex": []}),
              mock.patch.object(installer.codex_launcher, "capture_snapshot", return_value=None),
              mock.patch.object(installer.codex_launcher, "uninstall", return_value={"status": "unchanged"})):
            preview = installer.cmd_uninstall(args)
            self.assertTrue(any(f"remove symlink {dest}" in line for line in preview["lines"]))
            args.dry_run = False
            actual = installer.cmd_uninstall(args)
        self.assertEqual(actual["exit"], 0, actual)
        self.assertFalse(dest.is_symlink())
        self.assertEqual(old_agent.read_bytes(), self.source().read_bytes())

    def test_public_driver_preserves_marker_only_external_agent(self):
        external = self.root / "external"
        target = external / "adapters/codex/agents/deep.toml"
        target.parent.mkdir(parents=True)
        target.write_bytes(b'model = "user-owned"\n')
        (external / "harness-manifest.json").write_text("{}")
        self.dest().symlink_to(target)
        self.assertEqual(self.driver(dry_run=True)["status"], "preserved")
        self.assertEqual(self.driver()["status"], "preserved")
        self.assertEqual(os.readlink(self.dest()), str(target))
        self.assertEqual(target.read_bytes(), b'model = "user-owned"\n')

    def test_public_driver_rejects_invalid_release_evidence(self):
        old_agent = self.release_fixture()
        root = old_agent.parents[3]
        marker = root / "harness-manifest.json"
        original = marker.read_bytes()
        config = root / "adapters/codex/config/models.conf"
        original_config = config.read_text()
        core = root / "core/CORE.md"
        original_core = core.read_bytes()
        cases = ("empty-object", "empty-bytes", "malformed", "unrelated",
                 "wrong-runtime", "missing-root", "missing-projection", "wrong-projection")
        for case in cases:
            with self.subTest(case=case):
                marker.write_bytes(original)
                config.write_text(original_config)
                core.write_bytes(original_core)
                if case == "empty-object":
                    marker.write_text("{}")
                elif case == "empty-bytes":
                    marker.write_bytes(b"")
                elif case == "malformed":
                    marker.write_text('{"schema_version":')
                else:
                    data = json.loads(original)
                    if case == "unrelated":
                        data = {"schema_version": 4, "product": {"id": "other"}}
                    elif case == "wrong-runtime":
                        data["product"]["runtimes"] = ["claude", "opencode"]
                    elif case == "missing-root":
                        core.unlink()
                    elif case == "missing-projection":
                        data["ownership"]["generated"].remove("adapters/codex/agents/*.toml")
                    elif case == "wrong-projection":
                        config.write_text(re.sub(r'^CFG_NATIVE_AGENT_CATALOG=.*$',
                                                 'CFG_NATIVE_AGENT_CATALOG="light:light"',
                                                 original_config, flags=re.M))
                    marker.write_text(json.dumps(data))
                dest = self.dest()
                if dest.is_symlink():
                    dest.unlink()
                dest.symlink_to(old_agent)
                before = old_agent.read_bytes()
                self.assertEqual(self.driver(dry_run=True)["status"], "preserved")
                self.assertEqual(self.driver()["status"], "preserved")
                self.assertEqual(os.readlink(dest), str(old_agent))
                self.assertEqual(old_agent.read_bytes(), before)

    def test_public_uninstall_preserves_marker_only_external_agent(self):
        external = self.root / "external"
        target = external / "adapters/codex/agents/obsolete.toml"
        target.parent.mkdir(parents=True)
        target.write_bytes(b'model = "user-owned"\n')
        (external / "harness-manifest.json").write_bytes(b"")
        dest = self.dest("obsolete.toml")
        dest.symlink_to(target)
        known_root = self.root / "known-external"
        known = known_root / "adapters/codex/agents/deep.toml"
        known.parent.mkdir(parents=True)
        known.write_bytes(b'model = "another-user"\n')
        (known_root / "harness-manifest.json").write_text("{}")
        self.dest().symlink_to(known)
        registry = manifest._manifest_path("codex", "global")
        manifest._write_manifest(registry, {"schema": 1, "runtime": "codex", "scope": "global",
                                            "version": "old", "timestamp": "fixture", "files": {}})
        args = Namespace(runtimes=["codex"], target="codex", scope="global", dry_run=True)
        row = {"action": "symlink", "dest": str(self.dest()),
               "source": str(self.source()), "native_agent": True}
        with (mock.patch.object(installer.projector, "plan", return_value={"codex": [row]}),
              mock.patch.object(installer.codex_launcher, "capture_snapshot", return_value=None),
              mock.patch.object(installer.codex_launcher, "uninstall", return_value={"status": "unchanged"})):
            preview = installer.cmd_uninstall(args)
            self.assertEqual(preview["exit"], 0, preview)
            self.assertFalse(any(f"remove symlink {dest}" in line for line in preview["lines"]))
            self.assertFalse(any(f"remove symlink {self.dest()}" in line for line in preview["lines"]))
            args.dry_run = False
            actual = installer.cmd_uninstall(args)
        self.assertEqual(actual["exit"], 0, actual)
        self.assertEqual(os.readlink(dest), str(target))
        self.assertEqual(target.read_bytes(), b'model = "user-owned"\n')
        self.assertEqual(os.readlink(self.dest()), str(known))
        self.assertEqual(known.read_bytes(), b'model = "another-user"\n')

    def test_activation_discovery_and_refresh_preserve_false_release_marker(self):
        external = self.root / "external"
        target = external / "adapters/codex/agents/deep.toml"
        target.parent.mkdir(parents=True)
        target.write_bytes(b'model = "user-owned"\n')
        (external / "harness-manifest.json").write_text("{}")
        dest = self.dest()
        dest.symlink_to(target)
        self.assertNotIn(dest, activation._discovered_harness_links("codex"))
        with self.assertRaisesRegex(activation.ActivationError, "foreign native agent"):
            activation.activate("codex", "linked", str(ROOT))
        self.assertEqual(os.readlink(dest), str(target))

        dest.unlink()
        report = activation.activate("codex", "linked", str(ROOT))
        self.assertEqual(report["freshness"], "fresh", report)
        dest.unlink()
        dest.symlink_to(target)
        self.assertNotIn(dest, activation._discovered_harness_links("codex"))
        activation.refresh("codex")
        self.assertEqual(os.readlink(dest), str(target))

        config = self.home / "agent-config/models.conf"
        config.parent.mkdir(parents=True, exist_ok=True)
        shipped = (ROOT / "adapters/codex/config/models.conf").read_text()
        config.write_text(re.sub(r'^CFG_MAIN_SESSION_ONLY_MODELS=.*$',
                                 f'CFG_MAIN_SESSION_ONLY_MODELS="{" ".join(self._shipped_models())}"',
                                 shipped, flags=re.M))
        activation.refresh("codex")
        self.assertEqual(os.readlink(dest), str(target))

        config.write_text(re.sub(r'^CFG_NATIVE_AGENT_CATALOG=.*$',
                                 'CFG_NATIVE_AGENT_CATALOG="light:light"',
                                 shipped, flags=re.M))
        activation.refresh("codex")
        self.assertEqual(os.readlink(dest), str(target))
        self.assertEqual(target.read_bytes(), b'model = "user-owned"\n')

    def test_project_installer_keeps_shipped_agent_with_user_model_config(self):
        project = self.root / "fixture" / "project"
        project.mkdir()
        config = self.home / "agent-config/models.conf"
        config.parent.mkdir(parents=True)
        config.write_text("CFG_TIER_DEEP_MODEL=custom-user-model\n")
        with (chdir(project),
              mock.patch.object(codex, "_plugin_action", return_value={"action": "plugin", "status": "skipped"}),
              mock.patch.object(codex.codex_launcher, "uninstall", return_value={"action": "managed-launcher", "status": "unchanged"}),
              mock.patch.object(codex.manifest, "record", return_value={})):
            result = codex.install(scope="project")
        local_agent = project / ".codex/agents/deep.toml"
        self.assertFalse(result["blocked"], result)
        self.assertEqual(os.readlink(local_agent),
                         str(ROOT / "codex_setting/codex-agents/deep.toml"))
        self.assertEqual(config.read_text(), "CFG_TIER_DEEP_MODEL=custom-user-model\n")

    def test_checked_writer_rejects_repoint_between_classification_and_write(self):
        old = self.old_payload()
        dest = self.dest()
        dest.symlink_to(old)
        foreign = self.root / "foreign.toml"
        foreign.write_bytes(b"foreign")
        original = ownership.classify
        calls = 0

        def race(*args):
            nonlocal calls
            calls += 1
            result = original(*args)
            if calls == 1:
                dest.unlink()
                dest.symlink_to(foreign)
            return result

        with mock.patch.object(ownership, "classify", side_effect=race):
            with self.assertRaisesRegex(ownership.safe_fs.SafetyError, "expected-state-mismatch"):
                ownership.write_link(self.home, ROOT, dest, self.source())
        self.assertEqual(os.readlink(dest), str(foreign))
        self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_payload_path_escapes_and_false_identity_are_preserved(self):
        variants = ("symlink-root", "symlink-directory", "symlink-file", "wrong-digest",
                    "duplicate-key", "arbitrary-file")
        for variant in variants:
            with self.subTest(variant=variant):
                with tempfile.TemporaryDirectory(prefix="old-variant-", dir=self.root / "fixture") as td:
                    # Each variant gets a separate private home; the helper's
                    # expected ownership decision is never used to make it.
                    home = Path(td) / "home"
                    (home / "agents").mkdir(parents=True)
                    old_home = self.home
                    self.home = home
                    try:
                        target = self.old_payload()
                        dest = self.dest()
                        if variant == "symlink-root":
                            original = home / ".harness/native-agents"
                            moved = home / "payload-moved"
                            original.rename(moved)
                            original.symlink_to(moved, target_is_directory=True)
                        elif variant == "symlink-directory":
                            moved = home / "digest-moved"
                            target.parent.rename(moved)
                            target.parent.symlink_to(moved, target_is_directory=True)
                        elif variant == "symlink-file":
                            moved = home / "agent-moved"
                            target.rename(moved)
                            target.symlink_to(moved)
                        elif variant == "wrong-digest":
                            moved = target.parent.parent / ("0" * 64)
                            target.parent.rename(moved)
                            target = moved / target.name
                        elif variant == "duplicate-key":
                            metadata = target.parent / "metadata.json"
                            metadata.write_text(metadata.read_text()[:-1] + ',"digest":"' +
                                                target.parent.name + '"}')
                        elif variant == "arbitrary-file":
                            target = home / "user.toml"
                            target.write_bytes(b"user")
                        dest.symlink_to(target)
                        pointer = os.readlink(dest)
                        self.assertFalse(ownership.owned_link(home, dest))
                        self.assertEqual(ownership.write_link(home, ROOT, dest, self.source())["status"],
                                         "preserved")
                        self.assertEqual(os.readlink(dest), pointer)
                    finally:
                        self.home = old_home

    def test_whole_old_payload_must_remain_intact(self):
        target = self.old_payload(extra={"light.toml": b"another old agent"})
        self.dest().symlink_to(target)
        self.assertEqual(self.driver(dry_run=True)["status"], "planned")
        (target.parent / "light.toml").write_bytes(b"changed")
        self.assertEqual(self.driver(dry_run=True)["status"], "preserved")
        self.assertEqual(self.driver()["status"], "preserved")
        self.assertEqual(os.readlink(self.dest()), str(target))

    def test_complete_shell_projection_imports_helper_and_preserves_entries(self):
        old = self.old_payload()
        self.dest().symlink_to(old)
        external = self.root / "external"
        foreign = external / "adapters/codex/agents/light.toml"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"foreign")
        (external / "harness-manifest.json").write_text("{}")
        self.dest("light.toml").symlink_to(foreign)
        self.dest("balanced.toml").write_bytes(b"user")
        result = subprocess.run(["sh", str(ROOT / "adapters/codex/bin/install-runtime-projection.sh")],
                                env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(os.readlink(self.dest()), str(ROOT / "codex_setting/codex-agents/deep.toml"))
        self.assertEqual(os.readlink(self.dest("light.toml")), str(foreign))
        self.assertEqual(self.dest("balanced.toml").read_bytes(), b"user")
        self.assertIn("agent_preserved=", result.stdout)
        self.assertEqual(old.read_bytes(), b'model = "old-user-model"\n')

    def test_activation_refresh_withheld_and_receipt_migration(self):
        old = self.old_payload()
        self.dest().symlink_to(old)
        report = activation.activate("codex", "linked", str(ROOT))
        self.assertEqual(report["freshness"], "fresh", report)
        self.assertTrue(self.dest().is_symlink())
        self.assertEqual(self.dest().resolve(), self.source().resolve())
        self.assertEqual(old.read_bytes(), b'model = "old-user-model"\n')

        config = self.home / "agent-config/models.conf"
        config.parent.mkdir(parents=True, exist_ok=True)
        shipped = (ROOT / "adapters/codex/config/models.conf").read_text()
        user_config = shipped.replace("CFG_TIER_DEEP_MODEL=", "CFG_TIER_DEEP_MODEL=fixture-")
        config.write_text(user_config)
        refreshed = activation.refresh("codex")
        self.assertEqual(refreshed["model_config_source"], "user")
        self.assertTrue(self.dest().is_file() and not self.dest().is_symlink())
        selected = re.search(r'^model = "([^"]+)"', self.dest().read_text(), re.M).group(1)
        restricted = " ".join([*self._shipped_models(), selected])
        config.write_text(re.sub(r'^CFG_MAIN_SESSION_ONLY_MODELS=.*$',
                                 f'CFG_MAIN_SESSION_ONLY_MODELS="{restricted}"',
                                 user_config, flags=re.M))
        activation.refresh("codex")
        self.assertFalse(self.dest().exists() or self.dest().is_symlink())

    def test_old_payload_withheld_and_recorded_receipt_integrity(self):
        old = self.old_payload()
        dest = self.dest()
        dest.symlink_to(old)
        shipped = (ROOT / "adapters/codex/config/models.conf").read_text()
        deep_model = next(model for model in self._shipped_models() if model)
        config = self.home / "agent-config/models.conf"
        config.parent.mkdir(parents=True)
        config.write_text(re.sub(r'^CFG_MAIN_SESSION_ONLY_MODELS=.*$',
                                 f'CFG_MAIN_SESSION_ONLY_MODELS="{" ".join(self._shipped_models())}"',
                                 shipped, flags=re.M))
        activation.activate("codex", "linked", str(ROOT))
        self.assertFalse(dest.exists() or dest.is_symlink(), deep_model)

        # A prior activation receipt can legitimately record an old payload
        # target. Refresh may adopt it only while both receipt and bytes agree.
        config.write_text(shipped)
        activation.activate("codex", "linked", str(ROOT))
        state_path = activation._state_path("codex")
        state = json.loads(state_path.read_text())
        row = next(item for item in state["owned_paths"] if item["dest"] == str(dest))
        row["source"] = str(old)
        state_path.write_text(json.dumps(state))
        dest.unlink()
        dest.symlink_to(old)
        activation.refresh("codex")
        self.assertEqual(dest.resolve(), self.source().resolve())

        state = json.loads(state_path.read_text())
        row = next(item for item in state["owned_paths"] if item["dest"] == str(dest))
        row["source"] = str(old)
        state_path.write_text(json.dumps(state))
        dest.unlink()
        dest.symlink_to(old)
        old.write_bytes(b"user changed")
        prior = os.readlink(dest)
        activation.refresh("codex")
        self.assertEqual(os.readlink(dest), prior)
        self.assertEqual(old.read_bytes(), b"user changed")

    def test_old_payload_catalog_removal_only_removes_owned_leaf(self):
        old = self.old_payload("general-purpose.toml")
        removed = self.dest("general-purpose.toml")
        removed.symlink_to(old)
        foreign = self.root / "foreign.toml"
        foreign.write_bytes(b"foreign")
        preserved = self.dest("balanced.toml")
        preserved.symlink_to(foreign)
        shipped = (ROOT / "adapters/codex/config/models.conf").read_text()
        config = self.home / "agent-config/models.conf"
        config.parent.mkdir(parents=True)
        config.write_text(re.sub(r'^CFG_NATIVE_AGENT_CATALOG=.*$',
                                 'CFG_NATIVE_AGENT_CATALOG="deep:deep light:light"',
                                 shipped, flags=re.M))
        report = activation.activate("codex", "linked", str(ROOT))
        self.assertEqual(report["freshness"], "fresh", report)
        self.assertFalse(removed.exists() or removed.is_symlink())
        self.assertEqual(os.readlink(preserved), str(foreign))
        self.assertEqual(old.read_bytes(), b'model = "old-user-model"\n')

    def _shipped_models(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import model_profile
        cfg = model_profile.load_config(ROOT / "adapters/codex/config/models.conf")
        return sorted({v for k, v in cfg.items() if k.startswith("CFG_TIER_") and k.endswith("_MODEL")})

    def test_uninstall_removes_intact_old_catalog_leaf_and_preserves_tamper(self):
        valid = self.old_payload("deep.toml")
        self.dest().symlink_to(valid)
        bad = self.old_payload("light.toml", b"original")
        self.dest("light.toml").symlink_to(bad)
        bad.write_bytes(b"tampered")
        extra = self.old_payload("obsolete.toml")
        self.dest("obsolete.toml").symlink_to(extra)
        registry = manifest._manifest_path("codex", "global")
        manifest._write_manifest(registry, {"schema": 1, "runtime": "codex", "scope": "global",
                                            "version": "old", "timestamp": "fixture", "files": {}})
        row = lambda name: {"action": "symlink", "dest": str(self.dest(name)),
                            "source": str(self.source(name)), "native_agent": True}
        args = Namespace(runtimes=["codex"], target="codex", scope="global", dry_run=True)
        with (mock.patch.object(installer.projector, "plan", return_value={"codex": [row("deep.toml"), row("light.toml")]}),
              mock.patch.object(installer.codex_launcher, "capture_snapshot", return_value=None),
              mock.patch.object(installer.codex_launcher, "uninstall", return_value={"status": "unchanged"})):
            preview = installer.cmd_uninstall(args)
            self.assertEqual(preview["exit"], 0, preview)
            self.assertTrue(self.dest().is_symlink())
            args.dry_run = False
            actual = installer.cmd_uninstall(args)
        self.assertEqual(actual["exit"], 0, actual)
        self.assertFalse(self.dest().is_symlink())
        self.assertFalse(self.dest("obsolete.toml").is_symlink())
        self.assertTrue(self.dest("light.toml").is_symlink())
        self.assertEqual(bad.read_bytes(), b"tampered")


if __name__ == "__main__":
    unittest.main()
