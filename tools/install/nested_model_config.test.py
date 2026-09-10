#!/usr/bin/env python3
"""Focused regressions for tools/install/nested_model_config.py.

Every fixture is a synthetic temporary parent/nested Codex home pair rooted
under the real repository's ``adapters/codex`` shipped config (read-only) so
``model_config``/``native_agent_payload`` behave exactly as they do at
runtime. No production HOME/CODEX_HOME, network call, or subprocess other
than a fake, fixture-local "installer" script is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import shutil
from unittest import mock
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _sub in (_ROOT / "utilities", _ROOT / "tools" / "install"):
    if str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import model_config  # noqa: E402
import nested_model_config as nmc  # noqa: E402
import safe_fs  # noqa: E402


REPO_ROOT = model_config.repository_root()
SHIPPED_VALUES = model_config.parse_config(model_config.shipped_path("codex", source_root=REPO_ROOT))


def _write_parent_config(parent: Path, overrides: dict[str, str] | None = None) -> dict[str, str]:
    values = dict(SHIPPED_VALUES)
    if overrides:
        values.update(overrides)
    (parent / "agent-config").mkdir(parents=True, exist_ok=True)
    (parent / "agent-config" / "models.conf").write_text(model_config.assignments(values), encoding="utf-8")
    return values


def _fake_installer(path: Path, *, exit_code: int = 0) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        "mkdir -p \"$CODEX_HOME/agent-config\"\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class NestedModelConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.parent = self.root / "parent-codex"
        self.nested = self.root / "nested-codex"
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("")
        self.installer = _fake_installer(self.root / "fake_installer.sh")
        self.nested.mkdir(mode=0o700, parents=True)

    def _recover(self, *args, **kwargs):
        with mock.patch.object(nmc, "_scoped_live_use", return_value="quiescent"):
            return nmc.recover(*args, **kwargs)

    def test_legacy_balanced_derivation_preserves_exact_snapshot_bytes(self):
        values = _write_parent_config(self.parent)
        values.pop("CFG_MODEL_PROFILE_BALANCED", None)
        values.pop("CFG_MODEL_PROFILE_GRANULARITY_BALANCED", None)
        path = model_config.user_path("codex", runtime=self.parent)
        raw = model_config.assignments(values).encode()
        path.write_bytes(raw)
        selected, receipt, captured = nmc.capture_snapshot("codex", self.parent, source_root=REPO_ROOT)
        self.assertEqual(receipt.balanced_provenance, "derived-from-user-light")
        self.assertEqual(selected, model_config._derive_balanced_values("codex", values))
        self.assertEqual(captured, raw)
        nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(model_config.user_path("codex", runtime=self.nested).read_bytes(), raw)
        self.assertEqual(path.read_bytes(), raw)

    def test_upgraded_shipped_policy_preserves_legacy_astra_bytes_and_native_output(self):
        import tomllib
        values = _write_parent_config(self.parent, {
            "CFG_TIER_DEEP_MODEL":"gpt-6-astra", "CFG_TIER_DEEP_EFFORT":"ultra",
            "CFG_MODEL_PROFILE_DEEP":"deep:ultra",
            "CFG_MODEL_PROFILE_BALANCED_DEEP":"deep:high",
            "CFG_TIER_DEEP_FAILOVER_CASCADE":"gpt-6-astra:ultra gpt-5.6-luna:medium",
        })
        for key in ("CFG_MAIN_SESSION_ONLY_MODELS", "CFG_TIER_TOP_MODEL", "CFG_TIER_TOP_EFFORT",
                    "CFG_MODEL_PROFILE_TOP", "CFG_MODEL_PROFILE_GRANULARITY_TOP",
                    "CFG_MODEL_PROFILE_BALANCED", "CFG_MODEL_PROFILE_GRANULARITY_BALANCED"):
            values.pop(key, None)
        path = model_config.user_path("codex", runtime=self.parent)
        raw = ("# Legacy user choices; preserve these exact bytes.\n" + model_config.assignments(values)).encode()
        path.write_bytes(raw)
        selected, receipt, captured = nmc.capture_snapshot("codex", self.parent, source_root=REPO_ROOT)
        self.assertEqual((receipt.source,receipt.reason), ("user","user-valid-derived-balanced"))
        self.assertEqual(captured,raw)
        self.assertEqual(receipt.unreferenced_tier_keys,"CFG_TIER_TOP_EFFORT,CFG_TIER_TOP_MODEL")
        self.assertEqual(receipt.balanced_provenance,"derived-from-user-light")
        self.assertNotIn("CFG_MAIN_SESSION_ONLY_MODELS",selected)
        self.assertNotIn("CFG_MODEL_PROFILE_TOP",selected)
        result = nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(result["status"],"prepared")
        self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(model_config.user_path("codex",runtime=self.nested).read_bytes(),raw)
        nested, nested_receipt = model_config.resolve_config("codex",runtime=self.nested,source_root=REPO_ROOT)
        self.assertEqual(nested,selected)
        self.assertEqual(nested_receipt.source,"user")
        payload = nmc.native_agent_payload.check_payload(self.nested,source_root=REPO_ROOT)
        self.assertTrue(payload["ok"],payload)
        target = Path(payload["target_dir"])
        for name, effort in (("deep.toml","ultra"),("general-purpose.toml","high")):
            actual = tomllib.loads((target/name).read_text())
            self.assertEqual((actual["model"],actual["model_reasoning_effort"]),("gpt-6-astra",effort))
        # The wrapper's receipt must also report absent policy, not pretend the
        # upgraded shipped main-only restriction entered this selected user file.
        import importlib.util
        spec = importlib.util.spec_from_file_location("legacy_nested_wrapper",REPO_ROOT/"adapters/codex/bin/dispatch-headless.py")
        wrapper = importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
        with mock.patch.object(wrapper,"_model_policy",return_value=nested):
            policy_receipt = wrapper._main_session_only_policy_state()
        self.assertEqual(policy_receipt,"absent")

    def test_explicit_balanced_override_is_not_derived(self):
        values = _write_parent_config(self.parent, {"CFG_MODEL_PROFILE_BALANCED": "light:low"})
        selected, receipt, raw = nmc.capture_snapshot("codex", self.parent, source_root=REPO_ROOT)
        self.assertEqual(selected, values)
        self.assertEqual(receipt.balanced_provenance, "explicit")
        nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(model_config.user_path("codex", runtime=self.nested).read_bytes(), raw)

    def test_explicit_new_home_row_does_not_block_old_home_attribution(self):
        old = self.root / "worktree/.dispatch/nested-codex-home"
        new = old.with_name("nested-codex-home-v2")
        self.jobs.write_text(f"now\topen\trepo\t{self.root / 'worktree'}\tjob\tattempt_id=fixture,codex_home={new}\n")
        self.assertEqual(nmc._registry_attribution_quiescent(old, self.jobs), "quiescent")
        self.assertEqual(nmc._registry_attribution_quiescent(new, self.jobs), "in-use")

    # -- fresh snapshot ----------------------------------------------------

    def test_fresh_prepare_produces_full_cfg_equality_and_native_payload(self) -> None:
        values = _write_parent_config(self.parent, {"CFG_TIER_LIGHT_MODEL": "astra"})
        result = nmc.prepare(
            "codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer
        )
        self.assertEqual(result["status"], "prepared")

        nested_values, receipt = model_config.resolve_config("codex", runtime=self.nested, source_root=REPO_ROOT)
        self.assertEqual(nested_values, values)
        self.assertEqual(receipt.source, "user")
        self.assertEqual(receipt.reason, "user-valid")

        payload_check = __import__("native_agent_payload").check_payload(self.nested, source_root=REPO_ROOT)
        self.assertTrue(payload_check["ok"])
        for name in ("deep.toml", "general-purpose.toml", "light.toml", "memory-scout.toml"):
            self.assertIn(name, payload_check["files"])

        # Actual rendered TOML content, not just filename presence: each named
        # agent's model/effort must reflect the parent's inherited config.
        materialized = Path(payload_check["target_dir"])
        deep_toml = (materialized / "deep.toml").read_text(encoding="utf-8")
        self.assertIn(f'model = "{values["CFG_TIER_DEEP_MODEL"]}"', deep_toml)
        self.assertIn(f'model_reasoning_effort = "{values["CFG_TIER_DEEP_EFFORT"]}"', deep_toml)
        light_toml = (materialized / "light.toml").read_text(encoding="utf-8")
        self.assertIn('model = "astra"', light_toml)
        self.assertIn(f'model_reasoning_effort = "{values["CFG_TIER_LIGHT_EFFORT"]}"', light_toml)
        general_purpose_toml = (materialized / "general-purpose.toml").read_text(encoding="utf-8")
        self.assertIn(f'model = "{values["CFG_TIER_DEEP_MODEL"]}"', general_purpose_toml)
        memory_scout_toml = (materialized / "memory-scout.toml").read_text(encoding="utf-8")
        self.assertIn('model = "astra"', memory_scout_toml)

    def test_reuse_is_idempotent_and_reverifies_projection(self) -> None:
        _write_parent_config(self.parent)
        first = nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        second = nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(first["content_sha256"], second["content_sha256"])
        self.assertEqual(second["native_payload"]["status"], "unchanged")

    def test_parent_change_updates_owned_snapshot(self) -> None:
        _write_parent_config(self.parent)
        nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        _write_parent_config(self.parent, {"CFG_TIER_MINI_MODEL": "astra", "CFG_TIER_MINI_EFFORT": "medium"})
        updated = nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        nested_values, _ = model_config.resolve_config("codex", runtime=self.nested, source_root=REPO_ROOT)
        self.assertEqual(nested_values["CFG_TIER_MINI_MODEL"], "astra")
        self.assertEqual(updated["native_payload"]["status"], "created")

    # -- conflicts: preserved bytes -----------------------------------------

    def test_unmarked_existing_destination_is_preserved_as_conflict(self) -> None:
        _write_parent_config(self.parent)
        dest = model_config.user_path("codex", runtime=self.nested)
        dest.parent.mkdir(parents=True)
        dest.write_text("CFG_FOO=bar\n", encoding="utf-8")
        before = dest.read_bytes()
        with self.assertRaises(nmc.ConflictError):
            nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(dest.read_bytes(), before)

    def test_foreign_receipt_parent_mismatch_is_preserved(self) -> None:
        _write_parent_config(self.parent)
        nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        other_parent = self.root / "other-parent-codex"
        _write_parent_config(other_parent, {"CFG_TIER_LIGHT_MODEL": "astra"})
        dest = model_config.user_path("codex", runtime=self.nested)
        before = dest.read_bytes()
        with self.assertRaises(nmc.ConflictError):
            nmc.prepare("codex", other_parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(dest.read_bytes(), before)

    def test_edited_owned_destination_is_preserved_as_conflict(self) -> None:
        _write_parent_config(self.parent)
        nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        dest = model_config.user_path("codex", runtime=self.nested)
        dest.write_text("CFG_FOO=edited\n", encoding="utf-8")
        with self.assertRaises(nmc.ConflictError):
            nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)

    def test_receipt_symlink_is_a_typed_conflict_not_a_crash(self) -> None:
        _write_parent_config(self.parent)
        nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        receipt = nmc.receipt_path(self.nested)
        payload = receipt.read_bytes()
        receipt.unlink()
        decoy = self.nested / "decoy-receipt.json"
        decoy.write_bytes(payload)
        receipt.symlink_to(decoy)
        with self.assertRaises(nmc.ConflictError):
            nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)

    # -- parent missing/malformed fallback -----------------------------------

    def test_missing_parent_config_falls_back_to_shipped_and_still_snapshots(self) -> None:
        # No parent/agent-config/models.conf at all: resolve_config's existing
        # whole-file shipped fallback applies; this helper snapshots that
        # fallback rather than inventing a second policy.
        self.parent.mkdir(parents=True)
        result = nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(result["config_source"]["reason"], "user-missing")
        nested_values, _ = model_config.resolve_config("codex", runtime=self.nested, source_root=REPO_ROOT)
        self.assertEqual(nested_values, SHIPPED_VALUES)

    def test_malformed_parent_config_falls_back_to_shipped(self) -> None:
        (self.parent / "agent-config").mkdir(parents=True)
        (self.parent / "agent-config" / "models.conf").write_text("not a valid line\n", encoding="utf-8")
        result = nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(result["config_source"]["reason"], "user-malformed")

    # -- captured-buffer validation / ABA -------------------------------------

    def test_captured_buffer_mismatch_is_retried_then_reported_parent_unstable(self) -> None:
        _write_parent_config(self.parent)
        selected = self.parent / "agent-config" / "models.conf"
        real_read_bytes = Path.read_bytes
        calls = {"n": 0}

        def flaky_read_bytes(self_path):  # noqa: ANN001
            if self_path == selected:
                calls["n"] += 1
                return b"CFG_ONLY_ONE_KEY=x\n"
            return real_read_bytes(self_path)

        Path.read_bytes = flaky_read_bytes
        try:
            with self.assertRaises(nmc.ParentUnstableError):
                nmc.capture_snapshot("codex", self.parent, source_root=REPO_ROOT)
        finally:
            Path.read_bytes = real_read_bytes
        self.assertEqual(calls["n"], nmc.MAX_SNAPSHOT_RETRIES)

    def test_aba_race_where_a_is_invalid_cannot_produce_a_snapshot(self) -> None:
        """A(invalid/incomplete)->B(valid,different)->A(invalid) mid-call race.

        Every accepted snapshot must parse to resolve_config's full resolved
        mapping; an invalid A masquerading as stable must never be accepted.
        """
        _write_parent_config(self.parent, {"CFG_TIER_LIGHT_MODEL": "astra"})
        selected = self.parent / "agent-config" / "models.conf"
        invalid_a = b"CFG_INCOMPLETE_ONLY=1\n"
        real_read_bytes = Path.read_bytes
        calls = {"n": 0}

        def racing_read_bytes(self_path):  # noqa: ANN001
            if self_path == selected:
                calls["n"] += 1
                return invalid_a
            return real_read_bytes(self_path)

        Path.read_bytes = racing_read_bytes
        try:
            with self.assertRaises(nmc.ParentUnstableError):
                nmc.capture_snapshot("codex", self.parent, source_root=REPO_ROOT)
        finally:
            Path.read_bytes = real_read_bytes
        self.assertGreaterEqual(calls["n"], 1)
        # No destination should have been written by the failed capture.
        dest = model_config.user_path("codex", runtime=self.nested)
        self.assertFalse(dest.exists())

    # -- failure handling: no false complete claim ---------------------------

    def test_installer_failure_leaves_pending_not_complete(self) -> None:
        _write_parent_config(self.parent)
        failing_installer = _fake_installer(self.root / "failing_installer.sh", exit_code=7)
        with self.assertRaises(nmc.NestedModelConfigError) as ctx:
            nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=failing_installer)
        self.assertEqual(ctx.exception.code, "installer-failed")
        receipt = json.loads(nmc.receipt_path(self.nested).read_text(encoding="utf-8"))
        self.assertEqual(receipt["projection"]["state"], "pending")
        self.assertEqual(receipt["projection"]["failure_stage"], "installer")

        # Supported retry path: fixing the installer and re-running prepare
        # repairs the pending projection to complete.
        result = nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(result["status"], "prepared")
        receipt = json.loads(nmc.receipt_path(self.nested).read_text(encoding="utf-8"))
        self.assertEqual(receipt["projection"]["state"], "complete")

    # -- check / dry-run: zero writes -----------------------------------------

    def test_check_is_read_only_for_fresh_destination(self) -> None:
        _write_parent_config(self.parent)
        result = nmc.check("codex", self.parent, self.nested, source_root=REPO_ROOT)
        self.assertEqual(result["state"], "fresh")
        self.assertTrue(result["ok"])
        dest = model_config.user_path("codex", runtime=self.nested)
        self.assertFalse(dest.exists())
        self.assertFalse(nmc.receipt_path(self.nested).exists())

    def test_check_reports_pending_update_without_writing(self) -> None:
        _write_parent_config(self.parent)
        nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        _write_parent_config(self.parent, {"CFG_TIER_MINI_MODEL": "astra", "CFG_TIER_MINI_EFFORT": "medium"})
        before = model_config.user_path("codex", runtime=self.nested).read_bytes()
        result = nmc.check("codex", self.parent, self.nested, source_root=REPO_ROOT)
        self.assertEqual(result["state"], "owned-stale")
        after = model_config.user_path("codex", runtime=self.nested).read_bytes()
        self.assertEqual(before, after)

    # -- concurrency: two full preparations with differing parent content ----

    def test_two_full_preparations_cannot_publish_a_mixed_or_obsolete_snapshot(self) -> None:
        """Closure follow-up: two full ``prepare`` calls race with genuinely
        different parent content (not just seed-vs-seed). Serialized rendering
        must land on exactly one of the two full, self-consistent snapshots --
        never a mixture of A's config with B's native payload digest, and
        never a completed receipt whose payload_digest does not match its own
        content_sha256's rendering.
        """
        _write_parent_config(self.parent, {"CFG_TIER_LIGHT_MODEL": "astra"})
        nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)

        parent_b = self.root / "parent-b-codex"
        _write_parent_config(parent_b, {"CFG_TIER_MINI_MODEL": "astra", "CFG_TIER_MINI_EFFORT": "medium"})

        results: list[dict] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def run(parent_home: Path) -> None:
            try:
                barrier.wait(timeout=5)
                results.append(
                    nmc.prepare("codex", parent_home, self.nested, source_root=REPO_ROOT, installer=self.installer)
                )
            except Exception as exc:  # noqa: BLE001 - captured for assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(self.parent,)),
            threading.Thread(target=run, args=(parent_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        # Serialization means both attempts either complete cleanly or report
        # a typed, non-corrupting error; neither ever writes a torn snapshot.
        for exc in errors:
            self.assertIsInstance(exc, (nmc.NestedModelConfigError, safe_fs.SafetyError))

        final_receipt = json.loads(nmc.receipt_path(self.nested).read_text(encoding="utf-8"))
        self.assertEqual(final_receipt["projection"]["state"], "complete")
        dest_bytes = model_config.user_path("codex", runtime=self.nested).read_bytes()
        self.assertEqual(hashlib.sha256(dest_bytes).hexdigest(), final_receipt["content_sha256"])

        # The final config actually resolves (whole-file, not mixed) to
        # exactly one of the two parents' full mappings.
        final_values, _ = model_config.resolve_config("codex", runtime=self.nested, source_root=REPO_ROOT)
        parent_a_values, _ = model_config.resolve_config("codex", runtime=self.parent, source_root=REPO_ROOT)
        parent_b_values, _ = model_config.resolve_config("codex", runtime=parent_b, source_root=REPO_ROOT)
        self.assertIn(final_values, (parent_a_values, parent_b_values))

        # The native payload actually materialized matches the config that
        # won -- not a stale digest from the other racer.
        payload_check = __import__("native_agent_payload").check_payload(self.nested, source_root=REPO_ROOT)
        self.assertTrue(payload_check["ok"])
        self.assertEqual(final_receipt["projection"]["payload_digest"], payload_check["digest"])

    def test_concurrent_prepare_vs_prepare_A_to_B_to_A_capture_is_still_full_and_consistent(self) -> None:
        """A->B->A capture regression, kept distinct from the two-preparations
        race above: the *same* parent's content changes mid-capture (A, then
        B, then back to A) across two threads snapshotting the same target."""
        _write_parent_config(self.parent, {"CFG_TIER_LIGHT_MODEL": "astra"})
        original = (self.parent / "agent-config" / "models.conf").read_bytes()
        changed = model_config.assignments({**SHIPPED_VALUES, "CFG_TIER_LIGHT_MODEL": "sol"}).encode()

        stop = threading.Event()
        target = self.parent / "agent-config" / "models.conf"

        def _atomic_replace(payload: bytes) -> None:
            # Real writers (installer, root's CAS updates) replace atomically;
            # a reader must never observe a truncated/partial file. The race
            # this regression targets is A->B->A *value* flapping across a
            # capture, not a torn write, so the flapper mirrors that discipline.
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(payload)
            tmp.replace(target)

        def flapper() -> None:
            while not stop.is_set():
                _atomic_replace(changed)
                _atomic_replace(original)

        flap_thread = threading.Thread(target=flapper)
        flap_thread.start()
        try:
            successes = 0
            for _ in range(20):
                try:
                    values, _receipt, raw = nmc.capture_snapshot("codex", self.parent, source_root=REPO_ROOT)
                except nmc.ParentUnstableError:
                    # A correctly detected, non-corrupting refusal is an
                    # acceptable outcome of a genuine mid-call race -- the
                    # invariant under test is "no invalid snapshot is ever
                    # produced", not "every racing call must succeed".
                    continue
                reparsed = _parse_config_bytes(raw)
                self.assertEqual(reparsed, values)
                successes += 1
            # Every accepted capture is sound; sustained churn may refuse all attempts.
            # No lower bound on successes during a continuously changing source.
        finally:
            stop.set()
            flap_thread.join(timeout=5)

    # -- different-parent-identity conflict coverage --------------------------

    def test_same_parent_content_change_updates_but_different_parent_identity_conflicts(self) -> None:
        _write_parent_config(self.parent)
        nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)

        # Same parent path, changed content: update is allowed.
        _write_parent_config(self.parent, {"CFG_TIER_LIGHT_MODEL": "astra"})
        updated = nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(updated["status"], "prepared")

        # Different parent path (even with identical resolved content): conflict.
        other_parent = self.root / "identical-other-parent"
        _write_parent_config(other_parent, {"CFG_TIER_LIGHT_MODEL": "astra"})
        with self.assertRaises(nmc.ConflictError):
            nmc.prepare("codex", other_parent, self.nested, source_root=REPO_ROOT, installer=self.installer)

    # -- untouched auth/config/trust ------------------------------------------

    def test_auth_and_config_links_are_untouched_by_prepare(self) -> None:
        _write_parent_config(self.parent)
        auth = self.nested / "auth.json"
        auth.write_text('{"token": "unrelated"}', encoding="utf-8")
        before = auth.read_bytes()
        nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)
        self.assertEqual(auth.read_bytes(), before)

    # -- legacy recovery -------------------------------------------------------

    def test_recover_dry_run_makes_zero_writes(self) -> None:
        _write_parent_config(self.parent)
        dest = model_config.user_path("codex", runtime=self.nested)
        dest.parent.mkdir(parents=True)
        dest.write_bytes(model_config.assignments(SHIPPED_VALUES).encode())
        result = self._recover(
            "codex", self.parent, self.nested, source_root=REPO_ROOT,
            apply=False, authorize=None, expect_current_sha256=None, jobs=str(self.jobs),
        )
        self.assertTrue(result["ok"])
        self.assertFalse(nmc.receipt_path(self.nested).exists())

    def test_recover_apply_requires_authorization_and_hash_match(self) -> None:
        _write_parent_config(self.parent)
        dest = model_config.user_path("codex", runtime=self.nested)
        dest.parent.mkdir(parents=True)
        legacy = model_config.assignments(SHIPPED_VALUES).encode()
        dest.write_bytes(legacy)
        current_sha = hashlib.sha256(legacy).hexdigest()

        with self.assertRaises(nmc.NestedModelConfigError) as ctx:
            nmc.recover(
                "codex", self.parent, self.nested, source_root=REPO_ROOT, apply=True,
                authorize=None, expect_current_sha256=current_sha, jobs=str(self.jobs),
            )
        self.assertEqual(ctx.exception.code, "authorization-missing")

        with self.assertRaises(nmc.NestedModelConfigError) as ctx:
            nmc.recover(
                "codex", self.parent, self.nested, source_root=REPO_ROOT, apply=True,
                authorize=nmc.RECOVER_AUTHORIZATION, expect_current_sha256="0" * 64, jobs=str(self.jobs),
            )
        self.assertEqual(ctx.exception.code, "expected-hash-mismatch")

    def test_recover_apply_backs_up_then_adopts_with_cas(self) -> None:
        _write_parent_config(self.parent, {"CFG_TIER_LIGHT_MODEL": "astra"})
        dest = model_config.user_path("codex", runtime=self.nested)
        dest.parent.mkdir(parents=True)
        legacy = model_config.assignments(SHIPPED_VALUES).encode()
        dest.write_bytes(legacy)
        current_sha = hashlib.sha256(legacy).hexdigest()

        result = self._recover(
            "codex", self.parent, self.nested, source_root=REPO_ROOT, apply=True,
            authorize=nmc.RECOVER_AUTHORIZATION, expect_current_sha256=current_sha,
            jobs=str(self.jobs), installer=self.installer,
        )
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(len(result["backup"]["files"]), 1)
        backed_up = Path(result["backup"]["files"][0]["backup"])
        self.assertEqual(backed_up.read_bytes(), legacy)

        nested_values, _ = model_config.resolve_config("codex", runtime=self.nested, source_root=REPO_ROOT)
        self.assertEqual(nested_values["CFG_TIER_LIGHT_MODEL"], "astra")

    def test_recover_apply_refuses_when_registry_row_is_open(self) -> None:
        _write_parent_config(self.parent)
        dest = model_config.user_path("codex", runtime=self.nested)
        dest.parent.mkdir(parents=True)
        legacy = model_config.assignments(SHIPPED_VALUES).encode()
        dest.write_bytes(legacy)
        current_sha = hashlib.sha256(legacy).hexdigest()
        self.jobs.write_text(
            f"2026-01-01T00:00:00Z\topen\trepo\twt\tslug\tcodex_home={self.nested}\n", encoding="utf-8"
        )
        with self.assertRaises(nmc.QuiescenceUnavailableError):
            nmc.recover(
                "codex", self.parent, self.nested, source_root=REPO_ROOT, apply=True,
                authorize=nmc.RECOVER_AUTHORIZATION, expect_current_sha256=current_sha, jobs=str(self.jobs),
            )
        self.assertEqual(dest.read_bytes(), legacy)

    def test_recover_apply_refuses_on_unknown_registry_evidence(self) -> None:
        _write_parent_config(self.parent)
        dest = model_config.user_path("codex", runtime=self.nested)
        dest.parent.mkdir(parents=True)
        legacy = model_config.assignments(SHIPPED_VALUES).encode()
        dest.write_bytes(legacy)
        current_sha = hashlib.sha256(legacy).hexdigest()
        with self.assertRaises(nmc.QuiescenceUnavailableError):
            nmc.recover(
                "codex", self.parent, self.nested, source_root=REPO_ROOT, apply=True,
                authorize=nmc.RECOVER_AUTHORIZATION, expect_current_sha256=current_sha, jobs=None,
            )

    def test_recover_terminal_status_without_process_proof_is_unknown(self):
        self.jobs.write_text(f"now\tdone\trepo\twt\tslug\tcodex_home={self.nested}\n")
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "unknown")

    def test_scoped_live_use_refuses_while_a_same_user_process_holds_the_home_open(self) -> None:
        self.nested.mkdir(parents=True, exist_ok=True)
        target = self.nested / "agent-config"
        target.mkdir(parents=True, exist_ok=True)
        held_path = target / "models.conf"
        held_path.write_text("CFG_X=1\n", encoding="utf-8")
        handle = open(held_path, "rb")
        try:
            state = nmc._scoped_live_use(self.nested)
        finally:
            pass
        handle.close()
        # A held fd from *this* process is intentionally excluded from the
        # scan (own_pid is skipped) -- exercise the real cross-process guard
        # via a genuinely separate helper process instead.
        script = self.root / "holder.py"
        script.write_text(
            "import sys, time\n"
            f"f = open({str(held_path)!r}, 'rb')\n"
            "sys.stdout.write('ready\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(5)\n",
            encoding="utf-8",
        )
        import subprocess

        proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
        try:
            proc.stdout.readline()
            state = nmc._scoped_live_use(self.nested)
            self.assertEqual(state, "in-use")
        finally:
            proc.kill()
            proc.wait(timeout=5)
            proc.stdout.close()

    def test_symlinked_parent_config_is_rejected_as_unsafe(self) -> None:
        real = self.root / "real-parent"
        _write_parent_config(real)
        self.parent.symlink_to(real, target_is_directory=True)
        with self.assertRaises(nmc.NestedModelConfigError):
            nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT, installer=self.installer)


    def _prepare(self, **kwargs):
        return nmc.prepare("codex", self.parent, self.nested, source_root=REPO_ROOT,
                           installer=self.installer, **kwargs)

    def _legacy(self, *, native=False):
        _write_parent_config(self.parent, {"CFG_TIER_DEEP_MODEL": "gpt-6-astra"})
        dest = model_config.user_path("codex", runtime=self.nested)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(model_config.assignments(SHIPPED_VALUES))
        if native:
            plan = nmc.native_agent_payload.plan_payload(self.nested, source_root=REPO_ROOT)
            nmc.native_agent_payload.materialize_payload(plan)
            (self.nested / "agents").mkdir(exist_ok=True)
            for name in plan.files:
                (self.nested / "agents" / name).symlink_to(plan.target_dir / name)
        return dest

    def _recover_legacy(self, dest):
        return self._recover("codex", self.parent, self.nested, source_root=REPO_ROOT, apply=True,
            authorize=nmc.RECOVER_AUTHORIZATION, expect_current_sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),
            jobs=self.jobs, installer=self.installer)

    def test_seed_is_pending_and_serializes_with_prepare_under_same_lock(self):
        _write_parent_config(self.parent)
        self._prepare()
        entered, release, seeded = threading.Event(), threading.Event(), threading.Event()
        original = nmc._run_installer
        errors = []
        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)
        def run_prepare():
            try: self._prepare()
            except Exception as exc: errors.append(exc)
        def run_seed():
            try:
                nmc.seed("codex", self.parent, self.nested, source_root=REPO_ROOT)
                seeded.set()
            except Exception as exc: errors.append(exc)
        with mock.patch.object(nmc, "_run_installer", side_effect=blocked):
            first = threading.Thread(target=run_prepare)
            second = threading.Thread(target=run_seed)
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            self.assertFalse(seeded.wait(0.1))
            release.set()
            first.join(5); second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(seeded.is_set())
        self.assertEqual(json.loads(nmc.receipt_path(self.nested).read_text())["projection"]["state"], "pending")
        self.assertFalse(nmc.check("codex", self.parent, self.nested, source_root=REPO_ROOT)["ready"])
        self._prepare()
        self.assertTrue(nmc.check("codex", self.parent, self.nested, source_root=REPO_ROOT)["ready"])

    def test_all_ancestor_and_inner_directory_collisions_refuse_before_writes(self):
        _write_parent_config(self.parent)
        real = self.root / "outside"
        real.mkdir()
        aliases = self.root / "alias"
        aliases.symlink_to(real, target_is_directory=True)
        for action in (nmc.seed, nmc.prepare, nmc.check):
            with self.subTest(action=action.__name__):
                with self.assertRaises(nmc.NestedModelConfigError):
                    action("codex", self.parent, aliases / "absent", source_root=REPO_ROOT)
                self.assertEqual(list(real.iterdir()), [])
        for relative in (".harness", "agent-config", "agents"):
            collision = self.nested / relative
            collision.symlink_to(real, target_is_directory=True)
            with self.assertRaises(nmc.NestedModelConfigError): self._prepare()
            self.assertEqual(list(real.iterdir()), [])
            collision.unlink()
            collision.write_text("user-owned")
            with self.assertRaises(nmc.NestedModelConfigError): self._prepare()
            self.assertEqual(collision.read_text(), "user-owned")
            collision.unlink()

    def test_config_change_during_installer_never_completes_or_overwrites(self):
        _write_parent_config(self.parent)
        def edit(*args):
            dest = model_config.user_path("codex", runtime=self.nested)
            dest.write_text(model_config.assignments({**SHIPPED_VALUES, "CFG_TIER_LIGHT_MODEL": "user-successor"}))
            return 0, ""
        with mock.patch.object(nmc, "_run_installer", side_effect=edit):
            with self.assertRaises(nmc.ConflictError): self._prepare()
        self.assertIn("user-successor", model_config.user_path("codex", runtime=self.nested).read_text())
        self.assertEqual(json.loads(nmc.receipt_path(self.nested).read_text())["projection"]["state"], "pending")

    def test_receipt_change_during_failed_installer_is_preserved(self):
        _write_parent_config(self.parent)
        def edit(*args):
            nmc.receipt_path(self.nested).write_text('{"foreign":"successor"}\n')
            return 7, "fixture failure"
        with mock.patch.object(nmc, "_run_installer", side_effect=edit):
            with self.assertRaises(nmc.NestedModelConfigError): self._prepare()
        self.assertEqual(nmc.receipt_path(self.nested).read_text(), '{"foreign":"successor"}\n')

    def test_receipt_change_before_successful_completion_is_preserved(self):
        _write_parent_config(self.parent)
        original = nmc._verify_projection
        def edit(*args, **kwargs):
            original(*args, **kwargs)
            if kwargs.get("links", True):
                nmc.receipt_path(self.nested).write_text('{"foreign":"late-successor"}\n')
        with mock.patch.object(nmc, "_verify_projection", side_effect=edit):
            with self.assertRaises(nmc.ConflictError): self._prepare()
        self.assertIn("late-successor", nmc.receipt_path(self.nested).read_text())

    def test_native_link_and_target_edits_during_installer_are_preserved(self):
        _write_parent_config(self.parent)
        self._prepare()
        link = next((self.nested / "agents").glob("*.toml"))
        target = link.resolve()
        original = target.read_bytes()
        def edit_target(*args):
            target.write_bytes(original + b"# user successor\n")
            return 0, ""
        with mock.patch.object(nmc, "_run_installer", side_effect=edit_target):
            with self.assertRaises(nmc.ConflictError): self._prepare()
        self.assertEqual(target.read_bytes(), original + b"# user successor\n")
        target.write_bytes(original)
        user_file = self.root / "custom.toml"
        user_file.write_text("user-owned")
        def edit_link(*args):
            link.unlink(); link.symlink_to(user_file)
            return 0, ""
        with mock.patch.object(nmc, "_run_installer", side_effect=edit_link):
            with self.assertRaises(nmc.ConflictError): self._prepare()
        self.assertEqual(link.resolve(), user_file)

    def test_custom_native_definition_collision_is_never_overwritten(self):
        _write_parent_config(self.parent)
        (self.nested / "agents").mkdir()
        native = self.nested / "agents/deep.toml"
        native.write_text("user-owned")
        with self.assertRaises(nmc.ConflictError): self._prepare()
        self.assertEqual(native.read_text(), "user-owned")

    def test_source_and_renderer_changes_invalidate_completed_receipt(self):
        _write_parent_config(self.parent)
        source = self.root / "source"
        for relative in ("adapters/codex/config/models.conf",
                         "adapters/codex/bin/native_agent_renderer.py",
                         "tools/install/native_agent_payload.py", "tools/install/nested_model_config.py",
                         "adapters/codex/bin/install-runtime-projection.sh"):
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / relative, path)
        nmc.prepare("codex", self.parent, self.nested, source_root=source, installer=self.installer)
        self.assertTrue(nmc.check("codex", self.parent, self.nested, source_root=source)["ready"])
        renderer = source / "adapters/codex/bin/native_agent_renderer.py"
        renderer.write_bytes(renderer.read_bytes() + b"\n# renderer source changed\n")
        self.assertFalse(nmc.check("codex", self.parent, self.nested, source_root=source)["ready"])
        nmc.prepare("codex", self.parent, self.nested, source_root=source, installer=self.installer)
        with mock.patch.object(nmc.native_agent_payload.renderer, "RENDERER_VERSION", "test-new-renderer"):
            self.assertFalse(nmc.check("codex", self.parent, self.nested, source_root=source)["ready"])

    def test_post_backup_config_race_with_preserved_mtime_refuses(self):
        dest = self._legacy()
        original = nmc._backup_file
        before = dest.stat()
        successor = dest.read_bytes() + b"# user's edit\n"
        def raced(*args, **kwargs):
            record = original(*args, **kwargs)
            if args[0] == dest:
                dest.write_bytes(successor)
                os.utime(dest, ns=(before.st_atime_ns, before.st_mtime_ns))
            return record
        with mock.patch.object(nmc, "_backup_file", side_effect=raced):
            with self.assertRaises(nmc.ConflictError): self._recover_legacy(dest)
        self.assertEqual(dest.read_bytes(), successor)
        self.assertFalse(nmc.receipt_path(self.nested).exists())

    def test_post_backup_same_bytes_rewrite_is_detected_by_identity(self):
        dest = self._legacy()
        original = nmc._backup_file
        before = dest.stat()
        def raced(*args, **kwargs):
            record = original(*args, **kwargs)
            if args[0] == dest:
                dest.write_bytes(dest.read_bytes())
                os.utime(dest, ns=(before.st_atime_ns, before.st_mtime_ns))
            return record
        with mock.patch.object(nmc, "_backup_file", side_effect=raced):
            with self.assertRaises(nmc.ConflictError): self._recover_legacy(dest)
        self.assertFalse(nmc.receipt_path(self.nested).exists())

    def test_post_backup_payload_metadata_and_link_races_refuse(self):
        for kind in ("payload", "metadata", "link"):
            with self.subTest(kind=kind):
                # Each case owns an independent recovery home.
                self.nested = self.root / ("nested-" + kind)
                self.nested.mkdir(mode=0o700)
                dest = self._legacy(native=True)
                link = next((self.nested / "agents").glob("*.toml"))
                target = link.resolve()
                changed = target if kind == "payload" else target.parent / "metadata.json"
                original = nmc._backup_file
                did_edit = []
                def raced(*args, **kwargs):
                    record = original(*args, **kwargs)
                    if args[0] == dest and not did_edit:
                        did_edit.append(True)
                        if kind == "link":
                            link.unlink(); link.symlink_to(self.root / "foreign.toml")
                        else:
                            changed.write_bytes(changed.read_bytes() + b"\nuser-successor\n")
                    return record
                with mock.patch.object(nmc, "_backup_file", side_effect=raced):
                    with self.assertRaises(nmc.ConflictError): self._recover_legacy(dest)
                self.assertFalse(nmc.receipt_path(self.nested).exists())
                if kind != "link": self.assertIn(b"user-successor", changed.read_bytes())
                else: self.assertEqual(os.readlink(link), str(self.root / "foreign.toml"))

    def test_recovery_backs_up_native_links_bytes_and_metadata(self):
        dest = self._legacy(native=True)
        result = self._recover_legacy(dest)
        records = result["backup"]["files"]
        self.assertTrue(any(r["kind"] == "symlink" for r in records))
        self.assertTrue(any(r["source"].endswith("metadata.json") for r in records))
        for record in records:
            self.assertEqual(hashlib.sha256(Path(record["backup"]).read_bytes()).hexdigest(), record["sha256"])
        self.assertEqual(result["quiescence_scope"], "private-home-owner-runtime-processes-and-all-attributed-attempts")

    def test_backup_failure_cannot_adopt_legacy(self):
        dest = self._legacy()
        before = dest.read_bytes()
        with mock.patch.object(nmc, "_backup_file", side_effect=OSError("fixture backup failure")):
            with self.assertRaises(OSError): self._recover_legacy(dest)
        self.assertEqual(dest.read_bytes(), before)
        self.assertFalse(nmc.receipt_path(self.nested).exists())

    def _write_row(self, status, metadata):
        pipe = ",".join(f"{key}={value}" for key, value in metadata.items())
        self.jobs.write_text(f"now\t{status}\trepo\t{self.root / 'wt'}\tslug\t{pipe}\n")

    def _holder(self, *, home_use=True):
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.nested if home_use else self.root / "unrelated")
        child = subprocess.Popen([sys.executable, "-B", "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
            env=env, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            start_new_session=True)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        def cleanup():
            if child.poll() is None: child.kill()
            child.wait(timeout=5)
            child.stdin.close(); child.stdout.close()
        self.addCleanup(cleanup)
        return child

    def test_live_idle_closed_live_unregistered_and_reaped_process_cases(self):
        child = self._holder()
        visibility, start, _ = nmc.process_observation(child.pid)
        self.assertEqual(visibility, "present")
        ns = os.readlink("/proc/self/ns/pid")
        metadata = {"codex_home": str(self.nested), "attempt_id": "att-nested-private-fixture",
                    "registered_worker": "1", "pid": str(child.pid), "pid_start": start,
                    "pgid": str(child.pid), "pid_scope": "host-visible", "pid_ns": ns, "pid_observer_ns": ns}
        self.assertEqual(nmc._scoped_live_use(self.nested), "in-use")  # unregistered idle process
        self._write_row("open", metadata)
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "in-use")
        self._write_row("done", metadata)
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "in-use")
        child.stdin.close(); child.wait(timeout=5)
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "quiescent")
        self._write_row("open", metadata)
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "in-use")

    def test_namespace_reaped_receipt_and_incomplete_receipt_are_distinct(self):
        metadata = {"codex_home": str(self.nested), "attempt_id": "att-nested-ns-fixture",
            "registered_worker": "1", "pid": "437", "pid_start": "42", "pgid": "437",
            "pid_scope": "namespace-local", "pid_ns": "pid:[former-observer]", "pid_observer_ns": "pid:[former-observer]",
            "launch_lifecycle": "detached", "launch_outcome": "governed-process-group-drained",
            "group_reap_proof": "pgid-empty-v1", "group_reap_pgid": "437",
            "attempt_descendant_proof": "attempt-tagged-empty-v1", "attempt_descendant_observer_ns": "pid:[former-observer]"}
        self._write_row("done", metadata)
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "quiescent")
        del metadata["attempt_descendant_proof"]
        self._write_row("done", metadata)
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "unknown")

    def test_missing_duplicate_and_malformed_registry_are_unknown(self):
        self.jobs.unlink()
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "unknown")
        self.jobs.write_text("malformed\n")
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "unknown")
        self.jobs.write_text(f"now\tdone\trepo\twt\tslug\tcodex_home={self.nested},codex_home=/elsewhere\n")
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "unknown")

    def test_scoped_observer_requires_private_owner_home_and_relevant_visibility(self):
        self.nested.chmod(0o755)
        self.assertEqual(nmc._scoped_live_use(self.nested), "unknown")
        self.nested.chmod(0o700)
        child = self._holder(home_use=False)
        original = nmc.process_observation
        def unknown(pid):
            return ("unknown", "", "") if pid == child.pid else original(pid)
        with mock.patch.object(nmc, "process_observation", side_effect=unknown):
            self.assertEqual(nmc._scoped_live_use(self.nested), "unknown")

    def test_real_installer_nested_option_is_forwarded(self):
        _write_parent_config(self.parent)
        self.installer.write_text('#!/bin/sh\n[ "$1" = "--defer-native-agent-links" ] || exit 19\n')
        self._prepare()
        self.assertTrue(all(p.is_symlink() for p in (self.nested / "agents").glob("*.toml")))

    def test_actual_installer_defer_and_default_native_link_behavior(self):
        _write_parent_config(self.nested)
        (self.nested / "agents").mkdir()
        custom = self.root / "custom-native.toml"
        custom.write_text("user-owned")
        link = self.nested / "agents/deep.toml"
        link.symlink_to(custom)
        installer = _ROOT / "adapters/codex/bin/install-runtime-projection.sh"
        env = {**os.environ, "AGENT_HOME": str(REPO_ROOT), "CODEX_HOME": str(self.nested)}
        for defer in (True, False):
            result = subprocess.run([str(installer), *(["--defer-native-agent-links"] if defer else [])],
                env=env, cwd=self.root, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            if defer:
                self.assertEqual(link.resolve(), custom)
                self.assertIn("agents_linked=0", result.stdout)
            else:
                self.assertNotEqual(link.resolve(), custom)
                self.assertTrue(nmc.native_agent_payload.verify_owned_target(self.nested, link.resolve()))

    def test_actual_atomic_aba_between_resolver_and_capture_is_refused(self):
        _write_parent_config(self.parent)
        selected = self.parent / "agent-config/models.conf"
        valid = selected.read_bytes()
        original = model_config.resolve_config
        def atomic_replace(raw):
            temporary = selected.with_suffix(".incoming")
            temporary.write_bytes(raw)
            temporary.replace(selected)
        def swap_after_resolution(*args, **kwargs):
            atomic_replace(valid)
            resolved = original(*args, **kwargs)
            atomic_replace(b"CFG_INCOMPLETE_ONLY=1\n")
            return resolved
        with mock.patch.object(model_config, "resolve_config", side_effect=swap_after_resolution):
            with self.assertRaises(nmc.ParentUnstableError):
                nmc.seed("codex", self.parent, self.nested, source_root=REPO_ROOT)
        self.assertFalse(model_config.user_path("codex", runtime=self.nested).exists())

    def test_config_change_while_complete_receipt_is_published_returns_pending(self):
        _write_parent_config(self.parent)
        original = nmc._write_receipt
        def race(nested, payload, **kwargs):
            result = original(nested, payload, **kwargs)
            if payload["projection"]["state"] == "complete":
                model_config.user_path("codex", runtime=nested).write_text("CFG_USER_SUCCESSOR=1\n")
            return result
        with mock.patch.object(nmc, "_write_receipt", side_effect=race):
            with self.assertRaises(nmc.ConflictError): self._prepare()
        self.assertEqual(model_config.user_path("codex", runtime=self.nested).read_text(), "CFG_USER_SUCCESSOR=1\n")
        self.assertEqual(json.loads(nmc.receipt_path(self.nested).read_text())["projection"]["state"], "pending")

    def test_post_backup_receipt_race_is_preserved(self):
        dest = self._legacy()
        original = nmc._backup_file
        def raced(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == dest:
                nmc.receipt_path(self.nested).write_text('{"foreign":"successor"}\n')
            return result
        with mock.patch.object(nmc, "_backup_file", side_effect=raced):
            with self.assertRaises(nmc.ConflictError): self._recover_legacy(dest)
        self.assertEqual(nmc.receipt_path(self.nested).read_text(), '{"foreign":"successor"}\n')

    def _closed_namespace_row(self):
        return {"codex_home": str(self.nested), "attempt_id": "att-repeat-note-private-fixture",
            "registered_worker": "1", "pid": "437", "pid_start": "42", "pgid": "437",
            "pid_scope": "namespace-local", "pid_ns": "pid:[former-observer]", "pid_observer_ns": "pid:[former-observer]",
            "launch_lifecycle": "detached", "launch_outcome": "governed-process-group-drained",
            "group_reap_proof": "pgid-empty-v1", "group_reap_pgid": "437",
            "attempt_descendant_proof": "attempt-tagged-empty-v1", "attempt_descendant_observer_ns": "pid:[former-observer]"}

    def test_repeatable_registry_notes_preserve_closed_process_proof(self):
        metadata = self._closed_namespace_row()
        self._write_row("done", metadata)
        ordinary = self.jobs.read_text()
        self.jobs.write_text(ordinary.rstrip("\n") + ",note=completed,note=cleanup-merged\n")
        before = self.jobs.read_bytes()
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "quiescent")
        self.assertEqual(self.jobs.read_bytes(), before)
        # An unrelated historical annotation must not obstruct this home's proof.
        unrelated = before.decode().replace(str(self.nested), str(self.root / "unrelated-home"))
        self.jobs.write_text(unrelated + ordinary)
        before = self.jobs.read_bytes()
        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "quiescent")
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_repeat_notes_do_not_permit_duplicate_identity_or_home_keys(self):
        metadata = self._closed_namespace_row()
        for key in ("attempt_id", "codex_home", "pid", "pid_start", "pid_ns"):
            for duplicate in (metadata[key], "foreign-successor"):
                with self.subTest(key=key, duplicate=duplicate):
                    self._write_row("done", metadata)
                    original = self.jobs.read_text().rstrip("\n")
                    self.jobs.write_text(original + f",note=old,note=new,{key}={duplicate}\n")
                    before = self.jobs.read_bytes()
                    with mock.patch.object(nmc, "observed_attempt_liveness", wraps=nmc.observed_attempt_liveness) as observe:
                        self.assertEqual(nmc._registry_attribution_quiescent(self.nested, self.jobs), "unknown")
                        observe.assert_not_called()
                    self.assertEqual(self.jobs.read_bytes(), before)


def _parse_config_bytes(raw: bytes) -> dict[str, str]:
    return nmc._parse_captured_buffer(raw)


if __name__ == "__main__":
    unittest.main()
