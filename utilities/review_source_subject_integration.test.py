#!/usr/bin/env python3
"""Isolated Git/jobs/completion consumers for source-bound implementation review."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_contract as DC  # noqa: E402
import review_round_cap as CAP  # noqa: E402
import review_source_subject as SUBJECT  # noqa: E402
from route_identity import route_hash, route_id_from_hash  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROUTE_WRITER = _load("capability_route_source_subject_fixture", "utilities/capability-route.py")
DISPATCH_NODE = _load("dispatch_node_source_subject_fixture", "utilities/dispatch-node.py")


class SourceSubjectIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "user.name", "Fixture")
        (self.repo / "engine.py").write_text("value = 'A'\n", encoding="utf-8")
        self.git("add", "engine.py")
        self.git("commit", "-qm", "source A")
        self.artifact_root = self.base / ".agent_reports"
        self.artifact_root.mkdir()
        self.state = self.base / "dispatch"
        self.state.mkdir()
        self.jobs = self.state / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.agent_home = self.base / "agent-home"
        (self.agent_home / "core").mkdir(parents=True)
        (self.agent_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self.stable_home = self.base / "home"
        self.stable_home.mkdir()
        self.route = {
            "schema_version": 2, "dispatch_contract_version": 3,
            "route_id": "", "route_hash": "", "registry_digest": "fixture-registry",
            "capability": "autopilot-code", "capability_mode": "debug",
            "effective_intensity": "standard",
            "continuation_budget": {"review_round_cap": 2},
            "cwd": str(self.repo), "artifact_root": str(self.artifact_root),
            "nodes": [
                {"id": "execute", "kind": "pipeline-stage", "unit": "dev/backend",
                 "completion_gate": "code-execute", "dispatch_depth": 2,
                 "write_scope": ["source/**", "checklist.md", "dev_logs/**", "evidence/**"],
                 "depends_on": []},
                {"id": "smoke", "kind": "pipeline-stage", "unit": "qa/test",
                 "completion_gate": "code-test", "dispatch_depth": 2,
                 "write_scope": ["test_logs/**"], "depends_on": ["execute"]},
                {"id": "impl-review", "kind": "review-worker", "unit": "qa/code-review",
                 "completion_gate": "code-impl-review", "dispatch_depth": 2,
                 "write_scope": ["_internal/dev_reviews/**"], "depends_on": ["smoke"]},
                {"id": "test", "kind": "pipeline-stage", "unit": "qa/test",
                 "completion_gate": "code-test", "dispatch_depth": 2,
                 "write_scope": ["test_logs/**"], "depends_on": ["impl-review"]},
            ],
        }
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.review_node = self.route["nodes"][2]
        self.route_file = self.base / "route.json"
        self.route_file.write_text(json.dumps(self.route, sort_keys=True), encoding="utf-8")
        env = {
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_HOME": str(self.agent_home),
            "AGENT_ARTIFACT_ROOT": str(self.artifact_root),
            "AGENT_ARTIFACT_CHECKPOINT": "off",
            "AGENT_DISPATCH_ATTEMPT_ID": "",
            "HOME": str(self.stable_home),
        }
        self.env = mock.patch.dict(os.environ, env, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def select_custom_registry(self):
        custom = self.state / "route-registry.log"
        self.jobs.rename(custom)
        self.jobs = custom
        os.environ["AGENT_DISPATCH_JOBS"] = str(custom)

        # Build the ordinary sealed tuple, then seal the exact custom filename
        # accepted by root-explicit and inherited registry selection.
        launch = ROUTE_WRITER.launch_compatibility_tuple(
            artifact_root=self.artifact_root, jobs=custom, cwd=self.repo,
        )
        jobs_identity = launch["jobs_path"]
        jobs_identity["path"] = str(custom.resolve())
        jobs_identity["binding_digest"] = ROUTE_WRITER._sha256_record({
            "kind": "jobs_path", "path": str(custom.resolve()),
            "release_id": jobs_identity["release_id"],
            "content_digest": jobs_identity["content_digest"],
        })
        self.route["launch_compatibility_tuple"] = {
            "contract_version": 1, **launch,
        }
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.review_node = self.route["nodes"][2]
        self.route_file.write_text(json.dumps(self.route, sort_keys=True), encoding="utf-8")

        missing_ambient = self.base / "ambient-missing" / "jobs.log"
        explicit = DC.resolve_global_registry(
            self.agent_home, str(custom), 1, "start",
            environ={"AGENT_DISPATCH_JOBS": str(missing_ambient), "HOME": str(self.stable_home)},
        )
        inherited = DC.resolve_global_registry(
            self.agent_home, None, 2, "start",
            environ={"AGENT_DISPATCH_JOBS": str(custom), "HOME": str(self.stable_home)},
        )
        self.assertEqual((explicit.path, explicit.source), (custom.resolve(), "root-explicit"))
        self.assertEqual((inherited.path, inherited.source), (custom.resolve(), "inherited-env"))

    def get_node(self, node_id):
        return next(item for item in self.route["nodes"] if item["id"] == node_id)

    def _metadata(self, node, attempt_id, worker_type):
        return {
            "attempt_id": attempt_id, "attempt_schema_version": 2,
            "dispatch_depth": 2, "transport": "headless",
            "execution_surface": "registered-headless", "registered_worker": "1",
            "fallback_hop": "same-harness-headless", "worker_type": worker_type,
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "route_node": node["id"], "route_file": str(self.route_file),
            "registry_digest": self.route["registry_digest"],
        }

    def complete_node(self, node_id, attempt_id, evidence_text):
        node = self.get_node(node_id)
        evidence = self.artifact_root / f"{attempt_id}.md"
        evidence.write_text(evidence_text, encoding="utf-8")
        metadata = self._metadata(node, attempt_id, "stage")
        return ROUTE_WRITER._publish_completion_locked(
            self.route, node, node_id, evidence, jobs=self.jobs,
            attempt_id=attempt_id, attempt_metadata=metadata,
        )

    def revise_execute(self, attempt_id, evidence_text):
        evidence = self.artifact_root / f"{attempt_id}.md"
        evidence.write_text(evidence_text, encoding="utf-8")
        return ROUTE_WRITER.publish_revision_locked(
            self.route, "execute", evidence, basis="user-direction",
            direction="fixture-direction", author_attempt_id=attempt_id, jobs=self.jobs,
        )

    def _row(self, metadata, *, status="done", note="completed-marker", extra=None):
        record = {**metadata, "note": note, "failure_class": "pass", **(extra or {})}
        row = "\t".join(("fixture", status, str(self.repo), str(self.repo),
                         record["attempt_id"], ",".join(f"{k}={v}" for k, v in sorted(record.items()))))
        lines = self.jobs.read_text(encoding="utf-8").splitlines()
        matches = [i for i, line in enumerate(lines)
                   if len(line.split("\t")) == 6
                   and DC.parse_registry_metadata(line.split("\t")[5]).get("attempt_id") == record["attempt_id"]]
        if matches:
            lines[matches[0]] = row
            self.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            with self.jobs.open("a", encoding="utf-8") as stream:
                stream.write(row + "\n")
        return record

    def prepare_review(self, attempt_id, *, status="done", note="completed-marker", extra=None):
        metadata = self._metadata(self.review_node, attempt_id, "review")
        binding = SUBJECT.seal_attempt(self.jobs, self.route, self.review_node, attempt_id,
                                       worktree=self.repo)
        metadata.update(binding)
        return self._row(metadata, status=status, note=note, extra=extra)

    def complete_review(self, attempt_id, *, evidence_text="independent PASS"):
        metadata = self.prepare_review(attempt_id)
        evidence = self.artifact_root / f"{attempt_id}.md"
        evidence.write_text(evidence_text, encoding="utf-8")
        marker = ROUTE_WRITER._publish_completion_locked(
            self.route, self.review_node, self.review_node["id"], evidence, jobs=self.jobs,
            attempt_id=attempt_id, attempt_metadata=metadata,
        )
        return metadata, marker

    def currency(self, marker):
        path = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs) / "impl-review.json"
        return DC.gate_currency(self.route, self.review_node, path, marker, observe=True)

    def admission(self):
        return DISPATCH_NODE.admit_round(self.route, self.review_node, self.jobs, record_auto_revisions=False)

    def current_smoke_markers(self, prefix):
        self.complete_node("execute", f"att-{prefix}-execute", f"execute {prefix}")
        self.complete_node("smoke", f"att-{prefix}-smoke", f"smoke {prefix}")

    def test_A_passes_do_not_consume_B_or_C_and_A_does_not_revive(self):
        self.current_smoke_markers("A1")
        _m1, a1 = self.complete_review("att-review-A1")
        _m2, a2 = self.complete_review("att-review-A2")
        self.assertEqual(self.currency(a2).state, "current")
        same_a = self.admission()
        self.assertEqual((same_a.budget.state, same_a.budget.verdict_rounds), ("exhausted", 2))
        prior_rows = DISPATCH_NODE.prior_round_attempts(
            self.jobs, self.route["route_id"], self.review_node["id"], route=self.route)
        same_source_prompt = DISPATCH_NODE.round_protocol_block(
            same_a.budget, prior_rows, "review", self.review_node["id"])
        self.assertIn("This is a correction round", same_source_prompt)

        # A pre-change PASS attempt finishes after B. Its exact launch binding
        # is retained and must not overwrite the canonical B completion.
        late_a_values = SUBJECT.seal_attempt(self.jobs, self.route, self.review_node,
                                             "att-review-A-late", worktree=self.repo)
        late_a_meta = {**self._metadata(self.review_node, "att-review-A-late", "review"), **late_a_values}
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        self.current_smoke_markers("B1")
        self.assertEqual(self.currency(a2).state, "superseded")
        b_admission = self.admission()
        self.assertEqual((b_admission.budget.state, b_admission.budget.verdict_rounds,
                          b_admission.budget.round_kind), ("admit", 0, "first"))
        new_source_prompt = DISPATCH_NODE.round_protocol_block(
            b_admission.budget, prior_rows, "review", self.review_node["id"])
        self.assertIn("full independent review", new_source_prompt)
        self.assertIn("Earlier findings are context only", new_source_prompt)
        _bmeta, b1 = self.complete_review("att-review-B1")
        self.assertEqual(self.currency(b1).state, "current")
        canonical_before_late_a = (ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
                                   / "impl-review.json").read_bytes()
        late_path = self.artifact_root / "att-review-A-late.md"
        late_path.write_text("late old source PASS", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "review-source-subject-changed-before-completion"):
            ROUTE_WRITER._publish_completion_locked(
                self.route, self.review_node, self.review_node["id"], late_path, jobs=self.jobs,
                attempt_id="att-review-A-late", attempt_metadata=late_a_meta,
            )
        self.assertEqual(canonical_before_late_a,
                         (ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
                          / "impl-review.json").read_bytes())

        self.complete_review("att-review-B2")
        self.assertEqual(self.admission().budget.state, "exhausted")
        # Republishing the same source through ordinary completion preserves
        # the content epoch and cannot replenish B's two verdicts.
        self.complete_node("execute", "att-B-republish-execute", "execute B replay")
        self.complete_node("smoke", "att-B-republish-smoke", "smoke B replay")
        self.assertEqual(self.admission().budget.state, "exhausted")

        # A->B->A changes the source epoch, so historical A cannot revive;
        # its content budget remains exhausted. C then gets its own ordinary
        # first review after normal execute/smoke completion.
        (self.repo / "engine.py").write_text("value = 'A'\n", encoding="utf-8")
        self.current_smoke_markers("A2")
        self.assertEqual(self.currency(a2).state, "superseded")
        self.assertEqual(self.admission().budget.state, "exhausted")
        (self.repo / "engine.py").write_text("value = 'C'\n", encoding="utf-8")
        self.current_smoke_markers("C1")
        c_admission = self.admission()
        self.assertEqual((c_admission.budget.state, c_admission.budget.verdict_rounds,
                          c_admission.budget.round_kind), ("admit", 0, "first"))

        fields = CAP.recovery_fields("review-worker", "exhausted", route=self.route,
            node=self.review_node, jobs=self.jobs, route_file=self.route_file)
        self.assertEqual(fields["recovery_surface"], "owner-report")
        self.assertNotIn("recovery_check_command", fields)
        self.assertNotIn("recovery_complete_command", fields)
        # A PASS attempt remains immutable; the existing writer still rejects
        # new closure evidence linked to that exact PASS.
        different = self.artifact_root / "att-review-B2.owner-closure.md"
        different.write_text("owner disposition", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "immutable attempt completion differs from existing link"):
            ROUTE_WRITER._publish_completion_locked(
                self.route, self.review_node, self.review_node["id"], different, jobs=self.jobs,
                attempt_id="att-review-B2", attempt_metadata={
                    **self._metadata(self.review_node, "att-review-B2", "review"),
                    **SUBJECT.read_binding(self.jobs, self._row_metadata("att-review-B2")),
                }, owner_override=True,
            )

    def _row_metadata(self, attempt_id):
        for line in self.jobs.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) == 6:
                metadata = DC.parse_registry_metadata(fields[5])
                if metadata.get("attempt_id") == attempt_id:
                    return metadata
        raise AssertionError(attempt_id)

    def test_review_can_finish_before_its_source_upstream_marker(self):
        self.current_smoke_markers("A")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        _meta, b_pass = self.complete_review("att-review-B-before-smoke")
        self.assertEqual(b_pass["source_subject"]["epoch"], 1)
        self.assertEqual(self.currency(b_pass).state, "current")
        self.current_smoke_markers("B")
        self.assertEqual(self.currency(b_pass).state, "current")

    def test_simultaneous_B_upstream_and_review_completions_converge(self):
        self.current_smoke_markers("A")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        execute = self.get_node("execute")
        execute_id = "att-execute-B-barrier"
        execute_meta = self._metadata(execute, execute_id, "stage")
        self._row(execute_meta, note="completed-supervisor")
        execute_evidence = self.artifact_root / f"{execute_id}.md"
        execute_evidence.write_text("execute B barrier", encoding="utf-8")

        review_id = "att-review-B-barrier"
        review_meta = self._metadata(self.review_node, review_id, "review")
        review_meta.update(SUBJECT.seal_attempt(self.jobs, self.route, self.review_node,
            review_id, worktree=self.repo))
        self._row(review_meta, note="completed-supervisor")
        review_evidence = self.artifact_root / f"{review_id}.md"
        review_evidence.write_text("independent B barrier review", encoding="utf-8")

        barrier = threading.Barrier(2)

        def complete(node, attempt, evidence):
            barrier.wait(timeout=5)
            return ROUTE_WRITER.complete_node(
                self.route, node, node["id"], evidence, self.jobs, attempt)

        with ThreadPoolExecutor(max_workers=2) as pool:
            execute_future = pool.submit(complete, execute, execute_id, execute_evidence)
            review_future = pool.submit(complete, self.review_node, review_id, review_evidence)
            execute_marker, _ = execute_future.result(timeout=15)
            review_marker, _ = review_future.result(timeout=15)
        self.assertEqual(execute_marker["source_subject"]["digest"],
                         review_marker["source_subject"]["digest"])
        self.assertEqual(execute_marker["source_subject"]["epoch"],
                         review_marker["source_subject"]["epoch"])
        self.assertEqual(self.currency(review_marker).state, "current")

    def test_revision_preserves_matching_B_pass_and_same_source_revision_keeps_budget(self):
        self.current_smoke_markers("A")
        self.complete_review("att-review-A")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        _b_meta, b_pass = self.complete_review("att-review-B-before-revision")
        self.assertEqual(self.currency(b_pass).state, "current")

        first_revision = self.revise_execute("att-execute-revision-B", "execute evidence for B")
        self.assertNotIn("impl-review", first_revision["tombstoned"])
        self.assertEqual(self.currency(b_pass).state, "current")
        self.complete_node("smoke", "att-smoke-B-after-revision", "smoke B")

        # A second revision republishes the same source content. Existing
        # revision semantics reopen the dependent review, while its prior B
        # verdict still spends one unit of B's two verdict budget.
        second_revision = self.revise_execute("att-execute-revision-B-same", "second execute evidence for B")
        self.assertIn("impl-review", second_revision["tombstoned"])
        admission = self.admission()
        self.assertEqual(admission.budget.verdict_rounds, 1)
        self.assertEqual(admission.budget.round_kind, "closure-check")

    def test_same_content_revision_after_two_passes_keeps_public_admission_exhausted(self):
        self.current_smoke_markers("A-cap")
        _first_meta, first = self.complete_review("att-review-A-cap-1")
        _second_meta, second = self.complete_review("att-review-A-cap-2")
        digest_before = SUBJECT.observe(self.route, self.review_node, worktree=self.repo)["digest"]

        revision = self.revise_execute(
            "att-execute-A-same-content-after-cap",
            "revised upstream evidence without source changes",
        )
        self.complete_node("smoke", "att-smoke-A-after-same-content-revision", "smoke A after revision")
        digest_after = SUBJECT.observe(self.route, self.review_node, worktree=self.repo)["digest"]

        self.assertEqual(digest_after, digest_before)
        self.assertIn("impl-review", revision["tombstoned"])
        self.assertEqual(self.currency(first).state, "current")
        self.assertEqual(self.currency(second).state, "current")
        budget = self.admission().budget
        self.assertEqual((budget.state, budget.verdict_rounds, budget.cap), ("exhausted", 2, 2))
        self.assertNotEqual(budget.round_kind, "closure-check")

    def test_marker_currency_rejects_registry_and_manifest_binding_tamper(self):
        self.current_smoke_markers("A")
        metadata, marker = self.complete_review("att-review-bound")
        lines = self.jobs.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) == 6 and fields[4] == metadata["attempt_id"]:
                entries = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
                entries["review_subject_binding_digest"] = "0" * 64
                fields[5] = ",".join(f"{key}={value}" for key, value in entries.items())
                lines[index] = "\t".join(fields)
        self.jobs.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(self.currency(marker).reason,
                         "completion-review-source-row-binding-mismatch")

        self._row(metadata)
        subject_path = SUBJECT.binding_path(self.jobs, metadata["attempt_id"])
        record = json.loads(subject_path.read_text(encoding="utf-8"))
        record["manifest"][0]["content_sha256"] = "0" * 64
        subject_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        self.assertEqual(self.currency(marker).reason,
                         "completion-review-source-binding-missing")

    def test_marker_currency_uses_sealed_registry_under_ambient_mismatch(self):
        self.current_smoke_markers("sealed-registry")
        metadata, marker = self.complete_review("att-review-sealed-registry")
        sealed_before = self.jobs.read_bytes()

        missing_ambient = self.base / "missing-ambient" / "jobs.log"
        os.environ["AGENT_DISPATCH_JOBS"] = str(missing_ambient)
        missing_result = self.currency(marker)

        conflicting_ambient = self.base / "conflicting-ambient" / "jobs.log"
        conflicting_ambient.parent.mkdir(parents=True)
        row = next(line for line in self.jobs.read_text(encoding="utf-8").splitlines()
                   if DC.parse_registry_metadata(line.split("\t")[5]).get("attempt_id")
                   == metadata["attempt_id"])
        fields = row.split("\t")
        row_metadata = DC.parse_registry_metadata(fields[5])
        row_metadata["review_subject_binding_digest"] = "0" * 64
        fields[5] = ",".join(f"{key}={value}" for key, value in sorted(row_metadata.items()))
        conflicting_bytes = ("\t".join(fields) + "\n").encode("utf-8")
        conflicting_ambient.write_bytes(conflicting_bytes)
        os.environ["AGENT_DISPATCH_JOBS"] = str(conflicting_ambient)
        conflicting_result = self.currency(marker)

        self.assertFalse(missing_ambient.exists(), "currency must not create ambient registry")
        self.assertEqual(conflicting_ambient.read_bytes(), conflicting_bytes,
                         "currency must not mutate the conflicting ambient registry")
        self.assertEqual(self.jobs.read_bytes(), sealed_before,
                         "currency must only read the route-bound registry")
        self.assertEqual((missing_result.state, conflicting_result.state), ("current", "current"),
                         (missing_result, conflicting_result))

    def test_custom_selected_registry_currency_and_continuation_ignore_ambient_jobs(self):
        self.select_custom_registry()
        self.current_smoke_markers("custom-registry")
        metadata, marker = self.complete_review("att-review-custom-registry")
        marker_path = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs) / "impl-review.json"
        sealed_before = self.jobs.read_bytes()

        missing_ambient = self.base / "ambient-custom-missing" / "jobs.log"
        os.environ["AGENT_DISPATCH_JOBS"] = str(missing_ambient)
        direct_missing = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        continuation_missing = ROUTE_WRITER._continuation_reused_evidence(
            self.route, self.review_node,
        )

        conflicting_ambient = self.base / "ambient-custom-conflicting" / "jobs.log"
        conflicting_ambient.parent.mkdir(parents=True)
        fields = next(
            line.split("\t") for line in sealed_before.decode().splitlines()
            if DC.parse_registry_metadata(line.split("\t")[5]).get("attempt_id") == metadata["attempt_id"]
        )
        conflicting_row = DC.parse_registry_metadata(fields[5])
        conflicting_row["review_subject_binding_digest"] = "0" * 64
        fields[5] = ",".join(f"{key}={value}" for key, value in sorted(conflicting_row.items()))
        conflicting_bytes = ("\t".join(fields) + "\n").encode("utf-8")
        conflicting_ambient.write_bytes(conflicting_bytes)
        os.environ["AGENT_DISPATCH_JOBS"] = str(conflicting_ambient)
        direct_conflict = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        continuation_conflict = ROUTE_WRITER._continuation_reused_evidence(
            self.route, self.review_node,
        )

        self.assertEqual(direct_missing.state, "current", direct_missing)
        self.assertEqual(direct_conflict.state, "current", direct_conflict)
        self.assertEqual(continuation_missing[0]["node_id"], "impl-review")
        self.assertEqual(continuation_conflict[0]["node_id"], "impl-review")
        self.assertFalse(missing_ambient.exists())
        self.assertEqual(conflicting_ambient.read_bytes(), conflicting_bytes)
        self.assertEqual(self.jobs.read_bytes(), sealed_before)

        # The selected registry remains authoritative when it is damaged.
        self.jobs.unlink()
        missing_selected = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        self.jobs.write_bytes(sealed_before)
        self.assertEqual(missing_selected.reason, "completion-review-source-registry-missing")

        self.jobs.write_text("", encoding="utf-8")
        absent_row = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        self.jobs.write_bytes(sealed_before)
        self.assertEqual(absent_row.reason, "completion-review-source-row-not-exact")
        self.jobs.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(
            ValueError, "completion-review-source-row-not-exact",
        ):
            ROUTE_WRITER._continuation_reused_evidence(self.route, self.review_node)
        self.jobs.write_bytes(sealed_before)

        lines = self.jobs.read_text(encoding="utf-8").splitlines()
        changed = []
        for line in lines:
            row = line.split("\t")
            values = DC.parse_registry_metadata(row[5]) if len(row) == 6 else {}
            if values.get("attempt_id") == metadata["attempt_id"]:
                values["route_hash"] = "sha256:" + "0" * 64
                row[5] = ",".join(f"{key}={value}" for key, value in sorted(values.items()))
                line = "\t".join(row)
            changed.append(line)
        self.jobs.write_text("\n".join(changed) + "\n", encoding="utf-8")
        wrong_record = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        self.jobs.write_bytes(sealed_before)
        self.assertEqual(wrong_record.reason, "completion-review-source-row-binding-mismatch")

        binding_path = SUBJECT.binding_path(self.jobs, metadata["attempt_id"])
        binding_bytes = binding_path.read_bytes()
        binding_path.write_text("{}\n", encoding="utf-8")
        corrupt_binding = DC.gate_currency(
            self.route, self.review_node, marker_path, marker, observe=True,
        )
        binding_path.write_bytes(binding_bytes)
        self.assertEqual(corrupt_binding.reason, "completion-review-source-binding-missing")

    def test_public_complete_node_binds_registered_review_source(self):
        self.current_smoke_markers("public-completion")
        attempt = "att-review-public-complete-node"
        metadata = self.prepare_review(attempt, status="running")
        evidence = self.artifact_root / f"{attempt}.md"
        evidence.write_text("public completion PASS", encoding="utf-8")

        marker, row = ROUTE_WRITER.complete_node(
            self.route, self.review_node, self.review_node["id"], evidence,
            jobs=self.jobs, attempt_id=attempt,
        )

        self.assertEqual(row["status"], "closed")
        self.assertEqual(marker["source_subject"]["digest"], metadata["review_subject_digest"])
        self.assertEqual(marker["source_subject"]["binding_digest"],
                         metadata["review_subject_binding_digest"])
        self.assertEqual(self.currency(marker).state, "current")
        with mock.patch.object(
            DC, "attempt_process_quiescence",
            return_value=DC.ProcessQuiescence("quiescent", "fixture-terminal-observation"),
        ):
            terminal = ROUTE_WRITER._marker_identity_row(
                self.route, self.review_node, self.review_node["id"],
                self.review_node["completion_gate"], jobs=self.jobs, exact_terminal=True,
            )
        self.assertTrue(terminal["passed"], terminal)

    def test_gates_off_does_not_reuse_a_stale_review_pass(self):
        self.current_smoke_markers("A")
        _meta, a_pass = self.complete_review("att-review-gate-off-A")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        self.current_smoke_markers("B")
        self.assertEqual(self.currency(a_pass).state, "superseded")
        with mock.patch.dict(os.environ, {**os.environ, "HEARTING_GATES": "off"}, clear=True), \
             mock.patch.object(DC, "completion_attempt_readiness",
                               side_effect=AssertionError("stale PASS must not be consumed")):
            DC.completion_marker_gate(str(self.route_file), "test", "start",
                                      self.agent_home, self.jobs)
        with mock.patch.dict(os.environ, {**os.environ, "HEARTING_GATES": "on"}, clear=True):
            with self.assertRaises(DC.DispatchContractError) as refused:
                DC.completion_marker_gate(str(self.route_file), "test", "start",
                                          self.agent_home, self.jobs)
        self.assertEqual(refused.exception.reason, "completion-review-source-stale")

    def test_registration_and_launch_fences_reject_source_races_without_spawning(self):
        admitted_digest = SUBJECT.observe(self.route, self.review_node,
                                          worktree=self.repo)["digest"]
        attempt_id = "att-review-launch-race"
        metadata = self._metadata(self.review_node, attempt_id, "review")
        metadata.update({
            "capability": "autopilot-code", "capability_mode": "debug",
            "qa": "standard", "intensity": "standard", "unit": "qa/code-review",
            "route_node": self.review_node["id"], "write_scope": "_internal/dev_reviews/**",
            "completion_gate": "code-impl-review", "artifact_root": str(self.artifact_root),
            "log_file": str(self.base / "worker.log"), "launch_home": str(self.agent_home),
            "launch_authority": "conductor", "launch_fence": "registry-v1",
        })
        row = "\t".join(("fixture", "open", str(self.repo), str(self.repo), attempt_id,
                         ",".join(f"{key}={value}" for key, value in metadata.items())))
        with mock.patch.dict(os.environ, {"AGENT_REVIEW_SOURCE_SUBJECT_DIGEST": admitted_digest}):
            self.assertTrue(DC.claim_attempt_row(self.jobs, attempt_id, row, launch=False))

        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        spawned = []

        def never_spawn(_fd):
            spawned.append(True)
            raise AssertionError("the launch fence must run before spawn")

        with self.assertRaises(DC.DispatchContractError) as launch_refusal:
            DC.spawn_claimed_attempt(self.jobs, attempt_id, parent_binding=None, spawn=never_spawn)
        self.assertEqual(launch_refusal.exception.reason, "review-source-subject-changed-before-launch")
        self.assertEqual(spawned, [])

        second_id = "att-review-register-race"
        second_metadata = {**metadata, "attempt_id": second_id}
        second_row = "\t".join(("fixture", "open", str(self.repo), str(self.repo), second_id,
                                ",".join(f"{key}={value}" for key, value in second_metadata.items())))
        with mock.patch.dict(os.environ, {"AGENT_REVIEW_SOURCE_SUBJECT_DIGEST": admitted_digest}):
            with self.assertRaises(DC.DispatchContractError) as registration_refusal:
                DC.claim_attempt_row(self.jobs, second_id, second_row, launch=False)
        self.assertEqual(registration_refusal.exception.reason,
                         "review-source-admission-subject-changed")

    def test_live_and_unsettled_other_subjects_still_block_B_admission(self):
        self.current_smoke_markers("A")
        live = self.prepare_review("att-review-A-live", status="open", note="")
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        self.current_smoke_markers("B")
        self.assertEqual(self.admission().budget.state, "blocked-live")
        self._row(live, status="done", note="dead-worker-blocked")
        self.prepare_review("att-review-A-unsettled", status="done", note="completed-marker",
            extra={"terminal_conflict": "1", "conflicting_terminal_note": "completed-marker",
                    "conflicting_failure_class": "pass", "conflicting_classifier_source": "fixture"})
        self.assertEqual(self.admission().budget.state, "blocked-unsettled")

    def test_continuation_inherits_the_source_epoch_and_cannot_revive_A(self):
        self.current_smoke_markers("A")
        parent_route = json.loads(json.dumps(self.route))
        routes_dir = self.artifact_root / ".runtime" / "routes"
        routes_dir.mkdir(parents=True, exist_ok=True)
        (routes_dir / f"{parent_route['route_id']}.json").write_text(
            json.dumps(parent_route), encoding="utf-8")

        child = json.loads(json.dumps(parent_route))
        child["source_route_id"] = parent_route["route_id"]
        child["source_route_hash"] = parent_route["route_hash"]
        child["continuation_contract_version"] = 1
        child["route_hash"] = route_hash(child)
        child["route_id"] = route_id_from_hash(child["route_hash"])
        child_exec = next(item for item in child["nodes"] if item["id"] == "execute")
        child_dir = self.state / "completion" / child["route_id"]
        source_a = SUBJECT.observe(child, child_exec, worktree=self.repo)
        self.assertEqual(SUBJECT.source_epoch(child, child_exec, source_a["digest"], child_dir), 0)

        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        evidence = self.artifact_root / "continuation-execute-B.md"
        evidence.write_text("continued execute B", encoding="utf-8")
        attempt = "att-continuation-execute-B"
        metadata = self._metadata(child_exec, attempt, "stage")
        metadata.update(route_id=child["route_id"], route_hash=child["route_hash"],
                        route_file=str(self.route_file))
        marker = ROUTE_WRITER._publish_completion_locked(
            child, child_exec, "execute", evidence, jobs=self.jobs,
            attempt_id=attempt, attempt_metadata=metadata,
        )
        self.assertEqual(marker["source_subject"]["epoch"], 1)
        (self.repo / "engine.py").write_text("value = 'A'\n", encoding="utf-8")
        source_a_again = SUBJECT.observe(child, child_exec, worktree=self.repo)
        self.assertEqual(SUBJECT.source_epoch(child, child_exec, source_a_again["digest"], child_dir), 2)

    def test_publication_retry_converges_without_duplicate_verdict_and_reads_do_not_write(self):
        node = self.get_node("execute")
        evidence = self.artifact_root / "retry.md"
        evidence.write_text("execute", encoding="utf-8")
        metadata = self._metadata(node, "att-execute-retry", "stage")
        original_atomic = ROUTE_WRITER.atomic_write
        failed = {"once": False}

        def interrupt(path, payload):
            if Path(path).name == "execute.json" and not failed["once"]:
                failed["once"] = True
                raise OSError("injected publication interruption")
            return original_atomic(path, payload)

        with mock.patch.object(ROUTE_WRITER, "atomic_write", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "injected publication interruption"):
                ROUTE_WRITER._publish_completion_locked(
                    self.route, node, "execute", evidence, jobs=self.jobs,
                    attempt_id="att-execute-retry", attempt_metadata=metadata,
                )
        result = ROUTE_WRITER._publish_completion_locked(
            self.route, node, "execute", evidence, jobs=self.jobs,
            attempt_id="att-execute-retry", attempt_metadata=metadata,
        )
        directory = ROUTE_WRITER.completion_dir(self.route["route_id"], jobs=self.jobs)
        self.assertEqual(result["sequence"], 1)
        self.assertEqual(len(list(directory.glob("execute.[0-9]*.json"))), 1)
        marker = directory / "execute.json"
        before_mtime = marker.stat().st_mtime_ns
        for _ in range(2):
            source_state = SUBJECT.observe(self.route, node, worktree=self.repo)
            self.assertEqual(source_state["digest"], result["source_subject"]["digest"])
        self.assertEqual(DC.gate_currency(self.route, node, marker, result).state, "current")
        self.assertEqual(marker.stat().st_mtime_ns, before_mtime)


if __name__ == "__main__":
    unittest.main()
