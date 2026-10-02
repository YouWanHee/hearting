#!/usr/bin/env python3
"""Public CLI and forward-recovery tests for direct inline finish."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import concurrent.futures
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import artifact_lifecycle
import artifact_producer

CAP_SPEC = importlib.util.spec_from_file_location(
    "inline_finish_capability_route", ROOT / "utilities/capability-route.py")
CAP = importlib.util.module_from_spec(CAP_SPEC)
CAP_SPEC.loader.exec_module(CAP)

GATES = ["atomic-outcome", "known-scope", "no-shared-contract", "no-resource-run",
         "no-artifact-handoff", "no-independent-verifier", "focused-verification"]


class PublicInlineFinishTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="inline-finish-test-")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.root = self.base / "artifacts"
        self.root.mkdir()
        # The public compiler seals the harness source root as its runtime
        # identity. Keep that real source root while isolating mutable session
        # data with unique ids.
        self.home = ROOT
        self.jobs = self.base / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Inline Finish Test"], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.old_env = {key: os.environ.get(key) for key in (
            "AGENT_HOME", "AGENT_DISPATCH_JOBS", "AGENT_DISPATCH_DEPTH",
            "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_ROUTE_FILE", "AGENT_ROUTE_ID",
            "AGENT_ROUTE_NODE", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID",
            "OPENCODE_SESSION_ID", "AGENT_DISPATCH_CALLER_HARNESS", "XDG_STATE_HOME",
            "HEARTING_INLINE_FINISH_CRASH_AT")}
        os.environ.update({
            "AGENT_HOME": str(self.home), "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_DISPATCH_DEPTH": "0", "CODEX_THREAD_ID": "inline-finish-test-session",
            "XDG_STATE_HOME": str(self.base / "state"),
        })
        for key in ("AGENT_DISPATCH_ATTEMPT_ID", "AGENT_ROUTE_FILE", "AGENT_ROUTE_ID",
                    "AGENT_ROUTE_NODE", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID",
                    "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                    "HEARTING_INLINE_FINISH_CRASH_AT"):
            os.environ.pop(key, None)
        artifact_producer.activate(
            self.root, repository_id="repo_" + "a" * 32,
            artifact_root_id="root_" + "b" * 32,
            w7={"campaign_id": "camp_" + "c" * 32},
        )
        gate = {
            "spec_read": {"satisfied": True, "source": "fixture"},
            "drift_verdict": "within-spec", "workflow_mode": "tracked",
            "artifact_guard": {"satisfied": True, "source": "fixture"},
        }
        self.prompt = self.base / "task.md"
        self.prompt.write_text("Exercise the direct inline finish transaction.\n", encoding="utf-8")
        compose = [sys.executable, str(ROOT / "utilities/capability-route.py"), "compose",
                   "--slug", "inline-finish-test", "--campaign-key", "inline-finish-test",
                   "--shape", "direct", "--capability", "autopilot-code", "--capability-mode", "dev",
                   "--intensity", "direct", "--cwd", str(self.repo),
                   "--artifact-root", str(self.root), "--tracking", "tracked",
                   "--prompt-file", str(self.prompt),
                   "--spec-read", "fixture", "--drift-verdict", "within-spec", "--artifact-guard", "fixture",
                   "--owner", "codex", "--parent-harness", "codex", *self.extra_compose_arguments()]
        composed = subprocess.run(compose, cwd=self.repo, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(composed.returncode, 0, composed.stderr)
        route_receipt = json.loads(composed.stdout)
        self.route_file = Path(route_receipt["route_file"])
        self.route = json.loads(self.route_file.read_text(encoding="utf-8"))
        started = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "start",
                                  "--route", str(self.route_file), "--jobs", str(self.jobs)],
                                 cwd=self.repo, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(started.returncode, 0, started.stderr)
        self.cycle = artifact_producer.route_cycle_for(self.root, self.route)
        self.assertIsNotNone(self.cycle, started.stdout + started.stderr + repr(artifact_producer.status(self.root)))
        self.cycle_dir = artifact_producer.cycle_dir(
            self.root, self.cycle["campaign_id"], self.cycle["cycle_id"], self.cycle)
        self.evidence = self.cycle_dir / "artifacts/documents/evidence.md"
        self.evidence.parent.mkdir(parents=True)
        self.evidence.write_bytes(b"exact terminal evidence\n")
        self.summary = self.base / "summary.md"
        self.summary.write_text("Finished the inline route.\n", encoding="utf-8")
        self.command = [
            sys.executable, str(ROOT / "utilities/capability-route.py"), "finish",
            "--route", str(self.route_file), "--evidence", str(self.evidence),
            "--summary-file", str(self.summary),
        ]
        self.env = os.environ.copy()
        # `complete` would start a detached checkpoint that outlives the fixture root.
        self.env["AGENT_ARTIFACT_CHECKPOINT"] = "off"

    def extra_compose_arguments(self):
        return []

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def finish(self, fault=None):
        env = self.env.copy()
        if fault:
            env["HEARTING_INLINE_FINISH_CRASH_AT"] = fault
        else:
            env.pop("HEARTING_INLINE_FINISH_CRASH_AT", None)
        return subprocess.run(self.command, cwd=self.repo, env=env, capture_output=True, text=True)

    def finish_as(self, harness, session_id):
        env = self.env.copy()
        for key in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID"):
            env.pop(key, None)
        env[{"codex":"CODEX_THREAD_ID", "claude":"CLAUDE_CODE_SESSION_ID",
             "opencode":"OPENCODE_SESSION_ID"}[harness]] = session_id
        env["AGENT_DISPATCH_CALLER_HARNESS"] = harness
        return subprocess.run(self.command, cwd=self.repo, env=env, capture_output=True, text=True)

    def test_finish_without_registry_environment_or_existing_default(self):
        self.env.pop("AGENT_DISPATCH_JOBS", None)
        self.env["XDG_STATE_HOME"] = str(self.base / "empty-state")
        receipt = self.finish()
        self.assertEqual(receipt.returncode, 0, receipt.stderr + receipt.stdout)
        self.assertFalse((self.base / "empty-state/hearting/dispatch/jobs.log").exists())

    def test_finish_discovers_an_existing_default_registry(self):
        self.env.pop("AGENT_DISPATCH_JOBS", None)
        self.env["XDG_STATE_HOME"] = str(self.base / "default-state")
        registry = self.base / "default-state/hearting/dispatch/jobs.log"
        registry.parent.mkdir(parents=True)
        registry.write_text("field\tfield\tfield\tfield\tfield\troute_id=" + self.route["route_id"] + "\n")
        receipt = self.finish()
        self.assertNotEqual(receipt.returncode, 0)
        self.assertIn("finish-registered-route-ineligible", receipt.stderr)

    def test_loose_evidence_stays_in_place_and_replays(self):
        loose = self.cycle_dir / "artifacts/code_change.md"
        self.evidence.rename(loose)
        self.command[self.command.index("--evidence") + 1] = str(loose)
        receipt = self.finish()
        self.assertEqual(receipt.returncode, 0, receipt.stderr + receipt.stdout)
        self.assertTrue(loose.is_file())
        self.assertFalse((self.cycle_dir / "artifacts/plans/code_change.md").exists())
        replay = self.finish()
        self.assertEqual(replay.returncode, 0, replay.stderr + replay.stdout)

    def test_public_compose_start_finish_cli_flow_and_campaign_seal(self):
        receipt = self.finish()
        self.assertEqual(receipt.returncode, 0, receipt.stderr)
        self.assertEqual(json.loads(receipt.stdout)["schema"], "finish_receipt_v1")
        self.assertEqual(self.route.get("campaign_key"), "inline-finish-test")
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_depth_zero_public_finish_resolves_all_supported_harness_sessions(self):
        # Independent fixtures preserve the one-shot finish state for each
        # caller identity across the supported runtimes.
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                fixture = PublicInlineFinishTest("test_public_finish_seals_and_exact_replay_returns_one_receipt")
                fixture.setUp()
                try:
                    result = fixture.finish_as(harness, "inline-finish-" + harness)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)["schema"], "finish_receipt_v1")
                finally:
                    fixture.tearDown()

    def test_public_finish_seals_and_exact_replay_returns_one_receipt(self):
        first = self.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        receipt = json.loads(first.stdout)
        self.assertEqual(receipt["schema"], "finish_receipt_v1")
        self.assertFalse(receipt["replay"])
        record = artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])
        self.assertEqual(record["state"], "sealed")
        document = json.loads((self.cycle_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], 2)
        replay = self.finish()
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_receipt = json.loads(replay.stdout)
        self.assertTrue(replay_receipt["replay"])
        self.assertEqual(replay_receipt["inline_finish_id"], receipt["inline_finish_id"])

    def test_claimed_evidence_drift_refuses_then_exact_bytes_resume(self):
        crashed = self.finish("after-claim")
        self.assertNotEqual(crashed.returncode, 0)
        self.evidence.write_bytes(b"changed evidence\n")
        drift = self.finish()
        self.assertNotEqual(drift.returncode, 0)
        self.assertIn("finish-evidence-drift", drift.stderr)
        self.evidence.write_bytes(b"exact terminal evidence\n")
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_manifest_commit_crash_recovers_same_exact_finish(self):
        crashed = self.finish("after-manifest")
        self.assertNotEqual(crashed.returncode, 0)
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_marker_write_crash_recovers_the_same_exact_finish(self):
        crashed = self.finish("after-marker-write")
        self.assertNotEqual(crashed.returncode, 0)
        marker = CAP.completion_dir(self.route["route_id"]) / "inline.json"
        self.assertTrue(marker.is_file())
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])

    def test_concurrent_same_intent_finishes_one_cycle_and_reuses_receipt(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _n: self.finish(), (1, 2)))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        receipts = [json.loads(result.stdout) for result in (first, second)]
        self.assertEqual(receipts[0]["inline_finish_id"], receipts[1]["inline_finish_id"])
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_outcome_write_crash_recovers_exact_close(self):
        crashed = self.finish("after-close-write")
        self.assertNotEqual(crashed.returncode, 0)
        self.assertTrue(self.route_file.with_name(self.route_file.stem + ".outcome.json").is_file())
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])

    def test_index_applied_crash_recovers_exact_seal(self):
        crashed = self.finish("after-index")
        self.assertNotEqual(crashed.returncode, 0)
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(json.loads(resumed.stdout)["replay"])

    def test_finished_receipt_write_crash_replays_without_second_seal(self):
        crashed = self.finish("after-receipt")
        self.assertNotEqual(crashed.returncode, 0)
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        receipt = json.loads(resumed.stdout)
        self.assertTrue(receipt["replay"])
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "sealed")

    def test_summary_or_evidence_path_change_conflicts_with_claim(self):
        self.assertNotEqual(self.finish("after-claim").returncode, 0)
        self.summary.write_text("different summary\n", encoding="utf-8")
        changed_summary = self.finish()
        self.assertNotEqual(changed_summary.returncode, 0)
        self.assertIn("finish-intent-conflict", changed_summary.stderr)
        alternate = self.cycle_dir / "artifacts/documents/alternate.md"
        alternate.write_bytes(self.evidence.read_bytes())
        index = self.command.index("--evidence") + 1
        old = self.command[index]
        self.command[index] = str(alternate)
        changed_path = self.finish()
        self.command[index] = old
        self.assertNotEqual(changed_path.returncode, 0)
        self.assertIn("finish-intent-conflict", changed_path.stderr)

    def test_ambiguous_cross_harness_session_is_refused_before_intent(self):
        env = self.env.copy()
        env.pop("AGENT_DISPATCH_CALLER_HARNESS", None)
        env.pop("AGENT_DISPATCH_CURRENT_HARNESS", None)
        env["CLAUDE_CODE_SESSION_ID"] = "competing-session"
        refused = subprocess.run(self.command, cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-caller-harness-ambiguous", refused.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_finished_replay_rechecks_marker_evidence_binding(self):
        first = self.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        marker_file = CAP.completion_dir(self.route["route_id"]) / "inline.json"
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
        marker["evidence"]["sha256"] = "0" * 64
        marker_file.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
        replay = self.finish()
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("finish-marker-evidence-mismatch", replay.stderr)

    def test_finished_replay_refuses_missing_outcome_or_manifest(self):
        first = self.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        outcome = self.route_file.with_name(self.route_file.stem + ".outcome.json")
        original = outcome.read_bytes()
        outcome.unlink()
        missing_outcome = self.finish()
        self.assertNotEqual(missing_outcome.returncode, 0)
        self.assertIn("finish-outcome-missing-or-corrupt", missing_outcome.stderr)
        outcome.write_bytes(original)
        manifest = self.cycle_dir / "manifest.json"
        manifest.unlink()
        missing_manifest = self.finish()
        self.assertNotEqual(missing_manifest.returncode, 0)
        self.assertIn("already-sealed-mismatch", missing_manifest.stderr)

    def test_closed_and_sealed_replay_refuse_changed_prior_step(self):
        self.assertNotEqual(self.finish("after-close").returncode, 0)
        outcome = self.route_file.with_name(self.route_file.stem + ".outcome.json")
        row = json.loads(outcome.read_text(encoding="utf-8"))
        row["summary_digest"] = "0" * 64
        outcome.write_text(json.dumps(row), encoding="utf-8")
        closed_drift = self.finish()
        self.assertNotEqual(closed_drift.returncode, 0)
        self.assertIn("finish-outcome-drift", closed_drift.stderr)
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "open")

    def test_finished_replay_refuses_missing_exact_index(self):
        first = self.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        index = self.root / ".runtime/artifact-admission/v1/index.json"
        self.assertTrue(index.is_file())
        index.unlink()
        replay = self.finish()
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("already-sealed-mismatch", replay.stderr)

    def test_registered_route_is_refused_before_intent(self):
        metadata = "route_id=" + self.route["route_id"]
        self.jobs.write_text("field\tfield\tfield\tfield\tfield\t" + metadata + "\n", encoding="utf-8")
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-registered-route-ineligible", refused.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_registered_caller_is_refused_before_intent(self):
        env = self.env.copy()
        env["AGENT_DISPATCH_ATTEMPT_ID"] = "att-" + "a"*48
        refused = subprocess.run(self.command, cwd=self.repo, env=env,
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-registered-caller-ineligible", refused.stderr)

    def test_closed_route_and_scoped_tracked_dirt_are_refused_before_intent(self):
        outcome = self.route_file.with_name(self.route_file.stem + ".outcome.json")
        outcome.write_text("{}\n", encoding="utf-8")
        closed = self.finish()
        self.assertNotEqual(closed.returncode, 0)
        self.assertIn("finish-route-already-closed", closed.stderr)
        outcome.unlink()
        (self.repo / "README.md").write_text("dirty source\n", encoding="utf-8")
        dirty = self.finish()
        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("finish-scoped-tracked-dirt", dirty.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_changed_result_commit_conflicts_with_existing_claim(self):
        self.assertNotEqual(self.finish("after-claim").returncode, 0)
        (self.repo / "README.md").write_text("later source result\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "later source result"], check=True)
        changed = self.finish()
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("finish-intent-conflict", changed.stderr)

    def test_complete_and_close_are_fenced_while_finish_is_pending(self):
        self.assertNotEqual(self.finish("after-claim").returncode, 0)
        complete = subprocess.run([
            sys.executable, str(ROOT / "utilities/capability-route.py"), "complete",
            "--route", str(self.route_file), "--node", "inline", "--evidence", str(self.evidence),
        ], cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(complete.returncode, 0)
        self.assertIn("finish-in-progress", complete.stderr)
        close = subprocess.run([
            sys.executable, str(ROOT / "utilities/capability-route.py"), "close", "--route", str(self.route_file),
        ], cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(close.returncode, 0)
        self.assertIn("finish-in-progress", close.stderr)

    def test_public_foreign_finalize_cannot_seal_pending_finish(self):
        self.assertNotEqual(self.finish("after-close").returncode, 0)
        command = [sys.executable, str(ROOT / "utilities/artifact_producer.py"),
                   "finalize", "--artifact-root", str(self.root), "--cycle", self.cycle["cycle_id"]]
        foreign = subprocess.run(command, cwd=self.repo, env=self.env,
                                 capture_output=True, text=True)
        self.assertNotEqual(foreign.returncode, 0)
        self.assertIn("finish-in-progress", foreign.stdout + foreign.stderr, repr(foreign))
        self.assertEqual(artifact_producer.read_cycle_record(self.root, self.cycle["cycle_id"])["state"], "open")
        self.assertFalse((self.cycle_dir / "manifest.json").exists())

    def test_foreign_cycle_record_refuses_before_finish_intent(self):
        record_path = artifact_producer.cycle_record_path(self.root, self.cycle["cycle_id"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["route_id"] = "rt-" + "f" * 16
        record_path.write_text(json.dumps(record), encoding="utf-8")
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-route-cycle-missing", refused.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_non_direct_route_and_foreign_owner_context_refuse_before_slot(self):
        import inline_finish
        from types import SimpleNamespace

        args = SimpleNamespace(evidence=str(self.evidence), summary_file=str(self.summary), commit=None)
        for intensity in ("quick", "standard"):
            with self.subTest(intensity=intensity):
                candidate = dict(self.route, effective_intensity=intensity)
                with self.assertRaisesRegex(inline_finish.InlineFinishError, "finish-route-not-direct"):
                    inline_finish.finish(args, candidate, self.route_file, CAP)
        env = self.env.copy()
        env["AGENT_DISPATCH_ATTEMPT_ID"] = "att-" + "f" * 48
        env["AGENT_ROUTE_ID"] = "rt-" + "e" * 16
        foreign = subprocess.run(self.command, cwd=self.repo, env=env,
                                 capture_output=True, text=True)
        self.assertNotEqual(foreign.returncode, 0)
        self.assertIn("finish-registered-caller-ineligible", foreign.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_explicit_commit_not_source_descendant_is_refused_before_intent(self):
        original_branch = subprocess.run(
            ["git", "-C", str(self.repo), "symbolic-ref", "--short", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        (self.repo / "README.md").write_text("orphan branch\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "--orphan", "unrelated"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "unrelated history"], check=True)
        unrelated = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                   check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", original_branch], check=True)
        self.command.extend(["--commit", unrelated])
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-commit-not-source-descendant", refused.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())

    def test_active_review_lease_is_refused_before_intent(self):
        lease_path = artifact_producer._review_lease_path(self.root, self.cycle["cycle_id"], "att-review")
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text("{}\n", encoding="utf-8")
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-active-review-lease", refused.stderr)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())
        lease_path.unlink()
        resumed = self.finish()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    def test_symlink_evidence_is_refused_before_intent(self):
        outside = self.base / "outside.md"
        outside.write_text("outside evidence\n", encoding="utf-8")
        original = self.cycle_dir / "artifacts/documents/evidence-original.md"
        self.evidence.rename(original)
        self.evidence.symlink_to(outside)
        refused = self.finish()
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").exists())


class NextLegFinishTest(PublicInlineFinishTest):
    """A direct route that is leg 0 of an approved route plan reports `next_leg` on finish and replay."""

    PLAN_INDEX = 0

    def extra_compose_arguments(self):
        import route_plan
        self.task_file = self.base / "leg-task.md"
        self.task_file.write_text("the reusable work request\n", encoding="utf-8")
        leg = {"capability": "autopilot-code", "mode": "dev", "shape": "direct", "graph": None, "intensity": None,
               "why": "x"}
        second = {**leg, "shape": "staged", "graph": ["execute", "test"]}
        frame = {"route_id": "rt-" + "1" * 16, "route_hash": "sha256:" + "2" * 64, "cycle_id": "cyc_" + "3" * 32}
        decision = route_plan.build_decision(
            frame_route=frame, selected="Go", reason="", briefs=[], intent={"path": "x", "sha256": "0" * 64},
            proposal={"summary": "two", "legs": [leg, second], "entry_approvals": []},
            first_leg_compose={"leg": 0, "context": {
                "cwd": str(self.repo), "artifact_root": str(self.root), "slug": "inline-finish-test",
                "campaign_key": "inline-finish-test", "parent_cycle": frame["cycle_id"],
                "prompt_file": str(self.task_file), "prompt_sha256": "0" * 64, "spec_read": "fixture", "owner": None}})
        self.record_path = self.root / "decisions" / "route-decision.json"
        self.record_path.parent.mkdir(parents=True)
        self.record_path.write_bytes(route_plan.render(route_plan.build_record(decision)))
        return ["--route-plan", f"{self.record_path}#{self.PLAN_INDEX}"]

    def test_the_first_finish_and_every_replay_carry_the_same_next_leg(self):
        first = json.loads(self.finish().stdout)
        self.assertEqual(self.route["route_plan"]["index"], 0)
        next_leg = first["next_leg"]
        self.assertEqual((next_leg["index"], next_leg["leg"]["shape"]), (1, "staged"))
        import shlex
        argv = shlex.split(next_leg["compose_command"])
        self.assertEqual(argv[argv.index("--route-plan") + 1], f"{self.record_path}#1")
        self.assertEqual(argv[argv.index("--parent-cycle") + 1], self.cycle["cycle_id"])
        self.assertEqual(argv[argv.index("--campaign-key") + 1], "inline-finish-test")
        self.assertEqual(argv[argv.index("--graph") + 1], "execute,test")
        self.assertNotIn("--start", argv)
        replay = json.loads(self.finish().stdout)
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["next_leg"], next_leg)
        self.assertEqual({k: v for k, v in replay.items() if k not in ("replay",)},
                         {k: v for k, v in first.items() if k not in ("replay",)})

    def test_the_stored_receipt_and_its_identity_are_untouched(self):
        self.finish()
        stored = artifact_producer and json.loads(
            (self.root / ".runtime/inline-finish/v1" / self.route["route_id"] / "finish.json").read_text())["receipt"]
        self.assertEqual(set(stored), {"schema", "inline_finish_id", "route_id", "route_hash", "cycle_id",
                                       "terminal_marker_digest", "manifest_digest", "commit", "replay"})

    def test_the_start_receipt_of_the_finished_route_and_route_status_carry_the_same_next_leg(self):
        next_leg = json.loads(self.finish().stdout)["next_leg"]
        started = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "start",
                                  "--route", str(self.route_file), "--jobs", str(self.jobs)],
                                 cwd=self.repo, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(started.returncode, 0, started.stderr)
        receipt = json.loads(started.stdout)
        self.assertEqual((receipt["state"], receipt["required_action"]), ("completed", "advance-completed"))
        self.assertEqual(receipt["next_leg"], next_leg)
        for control in ("parent_next", "parent_next_command"):
            self.assertNotIn(control, receipt)
        self.assertNotIn("next_leg", receipt["required_action"])
        status = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "status",
                                 "--artifact-root", str(self.root)], cwd=self.repo, env=os.environ.copy(),
                                capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        rows = [row for row in json.loads(status.stdout) if row["route_id"] == self.route["route_id"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["next_leg"], next_leg)
        self.assertTrue(rows[0]["closed"])

    def test_a_route_that_has_not_finished_has_no_next_leg_anywhere(self):
        status = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "status",
                                 "--artifact-root", str(self.root)], cwd=self.repo, env=os.environ.copy(),
                                capture_output=True, text=True)
        rows = [row for row in json.loads(status.stdout) if row["route_id"] == self.route["route_id"]]
        self.assertFalse(rows[0]["closed"])
        self.assertNotIn("next_leg", rows[0])

    def test_a_vanished_record_leaves_the_receipt_as_it_was_without_the_key(self):
        self.finish()
        self.record_path.unlink()
        replay = json.loads(self.finish().stdout)
        self.assertNotIn("next_leg", replay)
        self.assertTrue(replay["replay"])


class LastLegFinishTest(NextLegFinishTest):
    PLAN_INDEX = 1

    def test_the_first_finish_and_every_replay_carry_the_same_next_leg(self):
        first = json.loads(self.finish().stdout)
        self.assertNotIn("next_leg", first)
        self.assertNotIn("next_leg", json.loads(self.finish().stdout))
        started = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "start",
                                  "--route", str(self.route_file), "--jobs", str(self.jobs)],
                                 cwd=self.repo, env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(json.loads(started.stdout)["state"], "completed")
        self.assertNotIn("next_leg", started.stdout)
        status = subprocess.run([sys.executable, str(ROOT / "utilities/capability-route.py"), "status",
                                 "--artifact-root", str(self.root)], cwd=self.repo, env=os.environ.copy(),
                                capture_output=True, text=True)
        self.assertNotIn("next_leg", status.stdout)


# The plan subclasses only add their own tests: do not run the inherited ones a second time.
for _klass in (NextLegFinishTest, LastLegFinishTest):
    for _name in dir(PublicInlineFinishTest):
        if _name.startswith("test_") and _name not in _klass.__dict__:
            setattr(_klass, _name, None)
LastLegFinishTest.test_the_stored_receipt_and_its_identity_are_untouched = None
LastLegFinishTest.test_a_vanished_record_leaves_the_receipt_as_it_was_without_the_key = None
LastLegFinishTest.test_the_start_receipt_of_the_finished_route_and_route_status_carry_the_same_next_leg = None
LastLegFinishTest.test_a_route_that_has_not_finished_has_no_next_leg_anywhere = None


if __name__ == "__main__":
    unittest.main()
