#!/usr/bin/env python3
"""WP1 (Astra guide alignment O1) tests for the Codex native-agent payload primitive."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC = importlib.util.spec_from_file_location(
    "native_agent_payload", HERE / "native_agent_payload.py"
)
assert SPEC and SPEC.loader
NAP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = NAP
SPEC.loader.exec_module(NAP)


# The shipped fixture deliberately carries only the two keys this repo's real
# kernel catalog (`memory-scout`) actually needs to render: a fallback profile
# and its tier's model. A "complete" user file must carry both; anything less
# is incomplete and falls back to this whole shipped mapping.
SHIPPED = "CFG_PROFILE_DEFAULT=deep:high:workspace-write\nCFG_TIER_DEEP_MODEL=shipped-model\n"
USER_COMPLETE = "CFG_PROFILE_DEFAULT=deep:high:workspace-write\nCFG_TIER_DEEP_MODEL=user-model\n"
USER_INCOMPLETE = "CFG_TIER_DEEP_MODEL=user-model\n"


class NativeAgentPayloadTest(unittest.TestCase):
    def make_root(self, shipped: str = SHIPPED) -> Path:
        root = Path(tempfile.mkdtemp())
        shipped_path = root / "adapters" / "codex" / "config" / "models.conf"
        shipped_path.parent.mkdir(parents=True)
        shipped_path.write_text(shipped, encoding="utf-8")
        return root

    def test_plan_payload_is_deterministic_and_files_are_sorted(self):
        root = self.make_root()
        home = root / "home"
        plan_a = NAP.plan_payload(home, source_root=root)
        plan_b = NAP.plan_payload(home, source_root=root)
        self.assertEqual(plan_a.digest, plan_b.digest)
        self.assertEqual(list(plan_a.files), sorted(plan_a.files))
        self.assertEqual(list(plan_a.metadata["files"]), sorted(plan_a.metadata["files"]))
        # plan_payload must never create anything, even repeatedly.
        self.assertFalse((home / ".harness").exists())

    def test_plan_payload_never_writes_on_a_read_only_home(self):
        root = self.make_root()
        home = root / "readonly-home"
        home.mkdir()
        home.chmod(0o555)
        try:
            plan = NAP.plan_payload(home, source_root=root)
            self.assertFalse(plan.target_dir.exists())
        finally:
            home.chmod(0o755)

    def test_user_complete_file_wins_as_one_unit_and_changes_digest(self):
        root = self.make_root()
        shipped_only = NAP.plan_payload(root / "no-user-home", source_root=root)

        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(USER_COMPLETE, encoding="utf-8")
        user_plan = NAP.plan_payload(home, source_root=root)

        self.assertEqual(user_plan.receipt["source"], "user")
        self.assertNotEqual(shipped_only.digest, user_plan.digest)
        self.assertIn("user-model", user_plan.files["memory-scout.toml"])
        self.assertIn("shipped-model", shipped_only.files["memory-scout.toml"])

    def test_incomplete_user_file_falls_back_to_whole_shipped_without_merge(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(USER_INCOMPLETE, encoding="utf-8")
        plan = NAP.plan_payload(home, source_root=root)
        self.assertEqual(plan.receipt["source"], "shipped")
        # Never merges: an incomplete user CFG_TIER_DEEP_MODEL never leaks in.
        self.assertIn("shipped-model", plan.files["memory-scout.toml"])
        self.assertNotIn("user-model", plan.files["memory-scout.toml"])

    def test_materialize_is_idempotent_and_check_confirms_it(self):
        root = self.make_root()
        home = root / "home"
        plan = NAP.plan_payload(home, source_root=root)

        created = NAP.materialize_payload(plan)
        self.assertEqual(created["status"], "created")
        again = NAP.materialize_payload(plan)
        self.assertEqual(again["status"], "unchanged")

        result = NAP.check_payload(home, source_root=root)
        self.assertTrue(result["ok"])
        metadata_path = plan.target_dir / "metadata.json"
        self.assertTrue(metadata_path.is_file())
        self.assertFalse(metadata_path.is_symlink())

    def test_repaired_user_config_does_not_collide_with_identical_fallback(self):
        root = self.make_root()
        home = root / "home"
        user = home / "agent-config" / "models.conf"
        user.parent.mkdir(parents=True)
        user.write_text(USER_INCOMPLETE, encoding="utf-8")
        fallback = NAP.plan_payload(home, source_root=root)
        NAP.materialize_payload(fallback)
        original = {path.name: path.read_bytes() for path in fallback.target_dir.iterdir()}

        user.write_text(SHIPPED, encoding="utf-8")
        repaired = NAP.plan_payload(home, source_root=root)
        self.assertEqual(fallback.files, repaired.files)
        self.assertNotEqual(fallback.digest, repaired.digest)
        self.assertEqual(repaired.receipt["source"], "user")
        self.assertEqual(NAP.materialize_payload(repaired)["status"], "created")
        self.assertTrue(NAP.check_payload(home, source_root=root)["ok"])
        self.assertEqual(NAP.materialize_payload(repaired)["status"], "unchanged")
        self.assertEqual(
            original, {path.name: path.read_bytes() for path in fallback.target_dir.iterdir()}
        )

        user.write_text(USER_INCOMPLETE, encoding="utf-8")
        restored_fallback = NAP.plan_payload(home, source_root=root)
        self.assertEqual(restored_fallback.digest, fallback.digest)
        self.assertEqual(NAP.materialize_payload(restored_fallback)["status"], "unchanged")
        self.assertTrue(NAP.check_payload(home, source_root=root)["ok"])

    def test_dry_run_materialize_never_writes(self):
        root = self.make_root()
        home = root / "home"
        plan = NAP.plan_payload(home, source_root=root)
        result = NAP.materialize_payload(plan, dry_run=True)
        self.assertEqual(result["status"], "planned")
        self.assertFalse(plan.target_dir.exists())

    def test_tampered_file_is_rejected_by_check_and_verify(self):
        root = self.make_root()
        home = root / "home"
        plan = NAP.plan_payload(home, source_root=root)
        NAP.materialize_payload(plan)

        name = next(iter(plan.files))
        target_file = plan.target_dir / name
        self.assertTrue(NAP.verify_owned_target(home, target_file))
        target_file.write_text("tampered", encoding="utf-8")

        self.assertFalse(NAP.verify_owned_target(home, target_file))
        result = NAP.check_payload(home, source_root=root)
        self.assertFalse(result["ok"])

    def test_tampered_metadata_is_rejected(self):
        root = self.make_root()
        home = root / "home"
        plan = NAP.plan_payload(home, source_root=root)
        NAP.materialize_payload(plan)

        metadata_path = plan.target_dir / "metadata.json"
        metadata_path.write_text('{"schema": "forged", "runtime": "codex"}', encoding="utf-8")

        name = next(iter(plan.files))
        self.assertFalse(NAP.verify_owned_target(home, plan.target_dir / name))
        result = NAP.check_payload(home, source_root=root)
        self.assertFalse(result["ok"])

    def test_foreign_dot_harness_substring_is_not_adopted(self):
        # A lookalike path that merely contains ".harness" but is not the
        # canonical native-agents root must never verify as owned.
        root = self.make_root()
        home = root / "home"
        home.mkdir(parents=True)
        foreign = home / ".harness-other" / "native-agents" / "deadbeef" / "memory-scout.toml"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("not ours", encoding="utf-8")
        self.assertFalse(NAP.verify_owned_target(home, foreign))

    def test_symlinked_harness_directory_is_refused(self):
        root = self.make_root()
        home = root / "home"
        home.mkdir(parents=True)
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        (home / ".harness").symlink_to(elsewhere, target_is_directory=True)

        plan = NAP.plan_payload(home, source_root=root)
        with self.assertRaises(NAP.PayloadMaterializeError):
            NAP.materialize_payload(plan)

    def test_symlinked_digest_directory_is_refused(self):
        root = self.make_root()
        home = root / "home"
        plan = NAP.plan_payload(home, source_root=root)
        plan.target_dir.parent.mkdir(parents=True)
        elsewhere = root / "elsewhere-digest"
        elsewhere.mkdir()
        plan.target_dir.symlink_to(elsewhere, target_is_directory=True)

        with self.assertRaises(NAP.PayloadMaterializeError):
            NAP.materialize_payload(plan)

    def test_symlinked_runtime_home_is_refused(self):
        root = self.make_root()
        real_home = root / "real-home"
        real_home.mkdir()
        link_home = root / "link-home"
        link_home.symlink_to(real_home, target_is_directory=True)

        plan = NAP.plan_payload(link_home, source_root=root)
        with self.assertRaises(NAP.PayloadMaterializeError):
            NAP.materialize_payload(plan)
        # Nothing was created through the link's target.
        self.assertEqual(list(real_home.iterdir()), [])

    def test_symlinked_ancestor_of_runtime_home_is_refused(self):
        root = self.make_root()
        real_parent = root / "real-parent"
        real_parent.mkdir()
        link_parent = root / "link-parent"
        link_parent.symlink_to(real_parent, target_is_directory=True)
        home = link_parent / "home"

        plan = NAP.plan_payload(home, source_root=root)
        with self.assertRaises(NAP.PayloadMaterializeError):
            NAP.materialize_payload(plan)
        # Nothing was created through the link's target, including the "home"
        # component itself.
        self.assertEqual(list(real_parent.iterdir()), [])

    def test_shipped_generator_remains_byte_stable(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "adapters" / "codex" / "bin" / "sync-native-agents.py"), "--check"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
