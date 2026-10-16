#!/usr/bin/env python3
"""Focused regressions for exact route context and typed output selection."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import artifact_context as C
import artifact_producer as P


class ArtifactContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "artifacts"
        self.root.mkdir()
        self.source = self.base / "source"
        self.source.mkdir()
        self.route = {
            "route_id": "rt-context", "route_hash": "sha256:route",
            "artifact_root": str(self.root), "cwd": str(self.source),
            "capability": "autopilot-code", "effective_intensity": "standard",
            "nodes": [{"id": "test", "outputs": ["test_logs/**", "source-diff"],
                       "write_scope": ["test_logs/**", "source-scoped"]}],
        }
        self.cycle = {"campaign_id": "camp_fixture", "cycle_id": "cyc_fixture",
                      "producer_id": "prod_fixture", "route_id": "rt-context"}

    def _context(self, env=None, **kwargs):
        values = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ROUTE_NODE": "test",
                  "AGENT_DISPATCH_WORKTREE": str(self.source)}
        values.update(env or {})
        with mock.patch.object(P, "load_route", return_value=self.route), \
             mock.patch.object(P, "route_cycle_for", return_value=self.cycle), \
             mock.patch.object(P, "cycle_dir", return_value=self.root / "fixture-cycle"):
            return C.resolve_context(route=self.route, environ=values, **kwargs)

    def test_role_selection_uses_exact_declared_output_and_scope(self):
        context = self._context()
        report = context.resolve_output("test-report")
        diagnostics = context.resolve_output("test-diagnostics")
        self.assertEqual(report.path, self.root / "fixture-cycle/artifacts/test_logs/run-tests-report.tsv")
        self.assertEqual(diagnostics.path, self.root / "fixture-cycle/artifacts/test_logs/run-tests-diagnostics")
        self.assertNotEqual(report.path, diagnostics.path)
        with self.assertRaisesRegex(C.ArtifactContextError, "artifact-output-role-missing"):
            context.resolve_output("ship-deploy")

    def test_logical_outputs_are_not_reconstructed_as_paths(self):
        self.assertEqual(C.render_outputs(self.root / "cycle/artifacts", ["source-diff"]), ["source-diff"])
        self.assertEqual(C.render_outputs(self.root / "cycle/artifacts", ["test_logs/**"]),
                         [str(self.root / "cycle/artifacts/test_logs/**")])

    def test_report_roles_select_subtree_and_exact_manifest_from_multiple_outputs(self):
        base = self._context()
        node = {"id": "report", "outputs": ["report/REPORT.md", "report/index.html",
                "report/report_manifest.json", "experiment-artifact"],
                "write_scope": ["report/REPORT.md", "report/index.html", "report/report_manifest.json"]}
        context = C.ArtifactContext(base.artifact_root, base.route, node, "report",
            base.cycle_record, base.cycle_dir, base.output_dir, base.source_worktree,
            base.binding_source)
        self.assertEqual(context.resolve_output("report-source").path,
                         base.output_dir / "report")
        self.assertEqual(context.resolve_output("report-manifest").path,
                         base.output_dir / "report/report_manifest.json")

    def test_component_scope_matching_rejects_prefix_and_traversal(self):
        self.assertTrue(C.scope_matches("test_logs/run.tsv", ["test_logs/**"]))
        self.assertFalse(C.scope_matches("test_logs-foreign/run.tsv", ["test_logs/**"]))
        self.assertFalse(C.scope_matches("test_logs/../outside", ["test_logs/**"]))
        self.assertFalse(C.scope_matches("test_logs/./run.tsv", ["test_logs/**"]))
        self.assertEqual(C.classify_scope("source-alternative/**"), "source")
        self.assertEqual(C.classify_scope("target-artifact"), "target")
        self.assertEqual(C.scope_values("tests/**;source-scoped;;dev_logs/**"),
                         ["dev_logs/**", "source-scoped", "tests/**"])

    def test_verified_route_lineage_overrides_stale_cycle_environment(self):
        context = self._context({"AGENT_ARTIFACT_CYCLE_ID": "cyc_stale",
                                 "AGENT_ARTIFACT_CAMPAIGN_ID": "camp_stale"})
        self.assertEqual(context.cycle_record["cycle_id"], "cyc_fixture")
        self.assertTrue(context.inherited_cycle_mismatch)
        self.assertEqual(context.binding_source, "route-lineage")

    def test_unbound_stale_environment_is_refused(self):
        values = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ROUTE_NODE": "test",
                  "AGENT_DISPATCH_WORKTREE": str(self.source),
                  "AGENT_ARTIFACT_CYCLE_ID": "cyc_stale"}
        with mock.patch.object(P, "load_route", return_value=self.route), \
             mock.patch.object(P, "route_cycle_for", return_value=None):
            with self.assertRaisesRegex(C.ArtifactContextError, "artifact-cycle-unbound"):
                C.resolve_context(route=self.route, environ=values)

    def test_explicit_node_cannot_reselect_a_different_bound_node(self):
        values = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ROUTE_NODE": "test",
                  "AGENT_DISPATCH_WORKTREE": str(self.source)}
        with mock.patch.object(P, "load_route", return_value=self.route), \
             mock.patch.object(P, "route_cycle_for", return_value=self.cycle), \
             mock.patch.object(P, "cycle_dir", return_value=self.root / "fixture-cycle"):
            with self.assertRaisesRegex(C.ArtifactContextError, "route-node-mismatch"):
                C.resolve_context(route=self.route, environ=values, node_id="other")

    def test_owner_producer_binding_precedes_stale_environment(self):
        bound = {**self.cycle, "cycle_id": "cyc_owner"}
        loaded = mock.Mock(binding=bound)
        admission = mock.Mock(allow=True)
        values = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ROUTE_NODE": "test",
                  "AGENT_DISPATCH_WORKTREE": str(self.source), "AGENT_ARTIFACT_CYCLE_ID": "cyc_old"}
        with mock.patch.object(P, "load_route", return_value=self.route), \
             mock.patch.object(P, "route_cycle_for") as route_cycle, \
             mock.patch.object(P, "read_cycle_record", return_value=bound), \
             mock.patch.object(P, "cycle_route_admission", return_value=admission), \
             mock.patch.object(P, "cycle_dir", return_value=self.root / "owner-cycle"), \
             mock.patch("dispatch_terminal_commit.load_producer_binding", return_value=loaded):
            context = C.resolve_context(route=self.route, environ=values, parent_attempt_id="att_owner")
        self.assertEqual(context.cycle_record["cycle_id"], "cyc_owner")
        self.assertEqual(context.binding_source, "producer-binding")
        self.assertTrue(context.inherited_cycle_mismatch)
        route_cycle.assert_not_called()

    def test_review_binding_selects_only_its_exact_report_target(self):
        review = {"cycle_id": "cyc_review", "producer_id": "prod_review",
                  "campaign_id": "camp_review"}
        output = self.root / "fixture-cycle/artifacts/plans/review.md"
        values = {"AGENT_ARTIFACT_ROOT": str(self.root), "AGENT_ROUTE_NODE": "test",
            "AGENT_DISPATCH_WORKTREE": str(self.source), "AGENT_DISPATCH_JOBS": str(self.base / "jobs.log"),
            "AGENT_DISPATCH_ATTEMPT_ID": "att_review", "AGENT_DISPATCH_UNIT": "qa/code-review",
            "AGENT_REVIEW_OUTPUT": str(output), "AGENT_REVIEW_CYCLE_ID": review["cycle_id"],
            "AGENT_REVIEW_PRODUCER_ID": review["producer_id"]}
        with mock.patch.object(P, "load_route", return_value=self.route), \
             mock.patch.object(P, "read_cycle_record", return_value=review), \
             mock.patch.object(P, "cycle_dir", return_value=self.root / "fixture-cycle"), \
             mock.patch("dispatch_contract.validate_review_output_binding", return_value={
                 "cycle_id": review["cycle_id"], "producer_id": review["producer_id"],
                 "output_path": str(output)}):
            context = C.resolve_context(route=self.route, environ=values)
        self.assertEqual(context.binding_source, "review-binding")
        self.assertEqual(context.review_output, output)
        self.assertEqual(context.resolve_output("review-output").path, output)

    def test_source_provenance_uses_source_worktree_not_artifact_repository(self):
        def init_repo(path: Path, filename: str) -> str:
            path.mkdir()
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.email", "fixture@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.name", "Fixture"], check=True)
            (path / filename).write_text(filename)
            subprocess.run(["git", "-C", str(path), "add", filename], check=True)
            subprocess.run(["git", "-C", str(path), "commit", "-qm", filename], check=True)
            return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()

        source_head = init_repo(self.base / "git-source", "source.txt")
        artifact_head = init_repo(self.base / "git-artifacts", "artifact.txt")
        state, observed = C.source_git_state(self.base / "git-source")
        self.assertEqual((state, observed), ("clean", source_head))
        self.assertNotEqual(source_head, artifact_head)
        (self.base / "git-source/new.txt").write_text("dirty")
        dirty, observed = C.source_git_state(self.base / "git-source")
        self.assertEqual((dirty, observed), ("untracked", source_head))
        (self.base / "git-source/new.txt").unlink()
        (self.base / "git-source/.gitignore").write_text("ignored.txt\n")
        subprocess.run(["git", "-C", str(self.base / "git-source"), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(self.base / "git-source"), "commit", "-qm", "ignore"], check=True)
        (self.base / "git-source/ignored.txt").write_text("ignored")
        ignored, _ = C.source_git_state(self.base / "git-source")
        self.assertEqual(ignored, "ignored")

    def test_source_provenance_without_git_uses_existing_unknown_state(self):
        state, commit = C.source_git_state(self.base)
        self.assertEqual((state, commit), ("unknown-no-git", None))


if __name__ == "__main__":
    unittest.main()
