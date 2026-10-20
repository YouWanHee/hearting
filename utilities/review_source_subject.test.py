#!/usr/bin/env python3
"""Effective source-subject identity, binding, and replacement regressions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import review_source_subject as SUBJECT  # noqa: E402


class SourceSubjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "user.name", "Fixture")
        for relative, content in {
            "engine.py": "value = 'A'\n",
            "tests/test_engine.py": "assert True\n",
            "core/CONVENTIONS.md": "runtime meaning\n",
            "docs/manual.md": "pure documentation\n",
            "README.md": "pure root documentation\n",
            "checklist.md": "artifact tracking\n",
            "evidence/result.json": "{}\n",
            "dev_logs/run.md": "artifact log\n",
            ".agent_reports/report.md": "shadow report\n",
            ".gitignore": "ignored-output.log\n",
        }.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (self.repo / "outside-target").write_text("outside\n", encoding="utf-8")
        (self.repo / "external-link").symlink_to("outside-target")
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture A")
        self.route = {
            "route_id": "rt-subject", "route_hash": "sha256:subject",
            "cwd": str(self.repo), "artifact_root": str(self.repo / ".agent_reports"),
            "nodes": [
                {"id": "execute", "kind": "pipeline-stage", "completion_gate": "code-execute",
                 "write_scope": ["source/**", "checklist.md", "dev_logs/**", "evidence/**"],
                 "depends_on": []},
                {"id": "impl-review", "kind": "review-worker", "unit": "qa/code-review",
                 "completion_gate": "code-impl-review", "write_scope": ["_internal/dev_reviews/**"],
                 "depends_on": ["execute"]},
                {"id": "smoke", "kind": "pipeline-stage", "unit": "qa/test",
                 "completion_gate": "code-test", "write_scope": ["test_logs/**"],
                 "depends_on": ["impl-review"]},
            ],
        }
        self.node = self.route["nodes"][1]
        self.test_node = self.route["nodes"][2]

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def observe(self):
        return SUBJECT.observe(self.route, self.node, worktree=self.repo)

    def test_source_bytes_dirty_index_and_same_content_commit(self):
        before = self.observe()
        source = self.repo / "engine.py"
        source.write_text("value = 'B'\n", encoding="utf-8")
        dirty = self.observe()
        self.assertNotEqual(before["digest"], dirty["digest"])
        self.git("add", "engine.py")
        staged = self.observe()
        self.assertEqual(dirty["digest"], staged["digest"])
        self.git("commit", "-qm", "source B")
        committed = self.observe()
        self.assertEqual(staged["digest"], committed["digest"])
        self.git("commit", "--allow-empty", "-qm", "empty publication")
        empty_commit = self.observe()
        self.assertEqual(committed["digest"], empty_commit["digest"])
        self.assertNotEqual(before["provenance"]["head"], committed["provenance"]["head"])
        source = self.repo / "engine.py"
        source.write_text("value = 'C'\n", encoding="utf-8")
        self.git("add", "engine.py")
        source.write_text("value = 'B'\n", encoding="utf-8")
        index_only = self.observe()
        self.assertEqual(committed["digest"], index_only["digest"])
        self.assertNotEqual(committed["provenance"]["index_tree"],
                            index_only["provenance"]["index_tree"])

    def test_source_modes_symlink_delete_rename_and_untracked_files_are_effective(self):
        original = self.observe()["digest"]
        source = self.repo / "engine.py"
        source.chmod(source.stat().st_mode ^ 0o100)
        mode_changed = self.observe()["digest"]
        self.assertNotEqual(original, mode_changed)
        link = self.repo / "external-link"
        link.unlink()
        link.symlink_to("engine.py")
        link_changed = self.observe()["digest"]
        self.assertNotEqual(mode_changed, link_changed)
        (self.repo / "new_source.py").write_text("new = True\n", encoding="utf-8")
        untracked = self.observe()["digest"]
        self.assertNotEqual(link_changed, untracked)
        (self.repo / "tests" / "test_engine.py").unlink()
        deleted = self.observe()["digest"]
        self.assertNotEqual(untracked, deleted)
        self.git("mv", "engine.py", "renamed_engine.py")
        renamed = self.observe()["digest"]
        self.assertNotEqual(deleted, renamed)

    def test_semantic_markdown_is_source_and_reports_docs_and_artifacts_are_not(self):
        before = self.observe()["digest"]
        (self.repo / "README.md").write_text("changed readme\n", encoding="utf-8")
        (self.repo / "docs" / "manual.md").write_text("changed docs\n", encoding="utf-8")
        (self.repo / "checklist.md").write_text("changed checklist\n", encoding="utf-8")
        (self.repo / "evidence" / "result.json").write_text("changed evidence\n", encoding="utf-8")
        (self.repo / "dev_logs" / "run.md").write_text("changed log\n", encoding="utf-8")
        (self.repo / ".agent_reports" / "report.md").write_text("changed report\n", encoding="utf-8")
        (self.repo / "ignored-output.log").write_text("ignored\n", encoding="utf-8")
        self.assertEqual(before, self.observe()["digest"])
        (self.repo / "core" / "CONVENTIONS.md").write_text("changed runtime meaning\n", encoding="utf-8")
        self.assertNotEqual(before, self.observe()["digest"])

    def test_artifact_only_stage_scopes_do_not_become_source_subjects(self):
        planning_node = {
            "id": "plan-alternative", "kind": "pipeline-stage",
            "write_scope": ["plan.alternative.md", "checklist.alternative.md"],
        }
        self.assertFalse(SUBJECT.has_source_subject(self.route, planning_node))
        self.assertIsNone(SUBJECT.marker_subject(
            self.route, planning_node, completion_dir=self.temp,
        ))
        self.assertTrue(SUBJECT.has_source_subject(self.route, self.route["nodes"][0]))

    def test_custom_code_test_gate_binds_source_and_uses_ancestor_source_scope(self):
        self.assertTrue(SUBJECT.has_review_subject(self.route, self.test_node))
        self.assertTrue(SUBJECT.has_source_subject(self.route, self.test_node))
        self.assertIn("source/**", SUBJECT.source_scopes(self.route, self.test_node))
        jobs = Path(self.temp.name) / "state" / "jobs.log"
        jobs.parent.mkdir()
        route_file = Path(self.temp.name) / "route.json"
        route_file.write_text(json.dumps(self.route), encoding="utf-8")
        values = SUBJECT.seal_attempt(jobs, self.route, self.test_node,
                                      "att-test-source-A", worktree=self.repo)
        metadata = {"route_file": str(route_file), "route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"], "route_node": self.test_node["id"],
                    "completion_gate": "code-test", "unit": "qa/test",
                    "attempt_id": "att-test-source-A", **values}
        fields = ["now", "open", str(self.repo), str(self.repo), "test-source-A", ""]
        self.assertEqual(SUBJECT.validate_attempt(jobs, fields, metadata, require_new=True)["subject_digest"],
                         values["review_subject_digest"])
        marker_subject = SUBJECT.marker_subject(
            self.route, self.test_node, attempt_id=metadata["attempt_id"],
            attempt_metadata=metadata, jobs=jobs, worktree=self.repo,
            completion_dir=Path(self.temp.name) / "completion" / self.route["route_id"],
        )
        self.assertEqual(marker_subject["digest"], values["review_subject_digest"])
        (self.repo / "engine.py").write_text("value = 'B'\n", encoding="utf-8")
        with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
            SUBJECT.validate_attempt(jobs, fields, metadata)
        self.assertEqual(refused.exception.reason, "review-source-subject-changed-before-launch")
        with self.assertRaises(SUBJECT.SourceSubjectError) as completion_refused:
            SUBJECT.marker_subject(
                self.route, self.test_node, attempt_id=metadata["attempt_id"],
                attempt_metadata=metadata, jobs=jobs, worktree=self.repo,
                completion_dir=Path(self.temp.name) / "completion" / self.route["route_id"],
            )
        self.assertEqual(completion_refused.exception.reason,
                         "review-source-subject-changed-before-completion")

    def test_inline_code_test_marker_carries_current_source_without_a_registered_binding(self):
        completion = Path(self.temp.name) / "completion" / self.route["route_id"]
        attempt = "att-inline-code-test"
        metadata = {"attempt_id": attempt, "registered_worker": False,
                    "execution_surface": "inline"}
        subject = SUBJECT.marker_subject(
            self.route, self.test_node, attempt_id=attempt, attempt_metadata=metadata,
            worktree=self.repo, completion_dir=completion,
        )
        self.assertEqual(subject["digest"], self.observe_test()["digest"])
        marker = {"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                  "node_id": self.test_node["id"], "completion_gate": "code-test",
                  "attempt_id": attempt, "registered_worker": False,
                  "execution_surface": "inline", "source_subject": subject}
        self.assertIsNone(SUBJECT.marker_currency(
            self.route, self.test_node, marker, completion,
        ))
        legacy = {key: value for key, value in marker.items() if key != "source_subject"}
        self.assertEqual(SUBJECT.marker_currency(self.route, self.test_node, legacy, completion),
                         ("superseded", "completion-source-subject-unknown"))

    def test_owner_inline_review_provenance_needs_its_exact_attempt(self):
        completion = Path(self.temp.name) / "completion" / self.route["route_id"]
        attempt = "att-owner-inline-review"
        subject = SUBJECT.marker_subject(
            self.route, self.node, attempt_id=attempt, worktree=self.repo,
            completion_dir=completion, owner_inline=True,
        )
        self.assertEqual(subject["inline_provenance"]["attempt_id"], attempt)
        marker = {"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                  "node_id": self.node["id"], "completion_gate": "code-impl-review",
                  "attempt_id": attempt, "registered_worker": False,
                  "execution_surface": "inline", "reviewer_kind": "owner-inline",
                  "review_independence": "degraded", "reviewer_identity": attempt,
                  "source_subject": subject}
        self.assertIsNone(SUBJECT.marker_currency(self.route, self.node, marker, completion))
        # Without an exact attempt the provenance could never validate, so the
        # marker keeps the historical source-unknown shape and is rerun.
        self.assertIsNone(SUBJECT.marker_subject(
            self.route, self.node, attempt_id=None, worktree=self.repo,
            completion_dir=completion, owner_inline=True,
        ))

    def observe_test(self):
        return SUBJECT.observe(self.route, self.test_node, worktree=self.repo)

    def test_binding_is_attempt_exact_and_launch_rechecks_effective_source(self):
        jobs = Path(self.temp.name) / "state" / "jobs.log"
        jobs.parent.mkdir()
        route_file = Path(self.temp.name) / "route.json"
        route_file.write_text(json.dumps(self.route), encoding="utf-8")
        values = SUBJECT.seal_attempt(jobs, self.route, self.node, "att-review-A", worktree=self.repo)
        metadata = {"route_file": str(route_file), "route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"], "route_node": self.node["id"],
                    "completion_gate": self.node["completion_gate"], "unit": "qa/code-review",
                    "attempt_id": "att-review-A", **values}
        fields = ["now", "open", str(self.repo), str(self.repo), "review-A", ""]
        self.assertIsNotNone(SUBJECT.validate_attempt(jobs, fields, metadata, require_new=True))
        predecessor = SUBJECT.read_binding(jobs, metadata)
        replacement = SUBJECT.seal_attempt(jobs, self.route, self.node, "att-review-A-retry",
            worktree=self.repo, predecessor_attempt_id="att-review-A")
        self.assertEqual(replacement["review_subject_digest"], values["review_subject_digest"])
        changed = self.repo / "engine.py"
        changed.write_text("value = 'B'\n", encoding="utf-8")
        with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
            SUBJECT.validate_attempt(jobs, fields, metadata)
        self.assertEqual(refused.exception.reason, "review-source-subject-changed-before-launch")
        self.assertEqual(predecessor["attempt_id"], "att-review-A")

    def test_unknown_legacy_attempt_is_not_inferred_from_launch_head(self):
        jobs = Path(self.temp.name) / "state" / "jobs.log"
        self.assertIsNone(SUBJECT.read_binding(jobs, {"attempt_id": "att-old"}, required=False))
        route_file = Path(self.temp.name) / "route.json"
        route_file.write_text(json.dumps(self.route), encoding="utf-8")
        fields = ["now", "open", str(self.repo), str(self.repo), "old", ""]
        metadata = {"route_file": str(route_file), "route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"], "route_node": self.node["id"],
                    "attempt_id": "att-old", "launch_head": self.git("rev-parse", "HEAD")}
        self.assertIsNone(SUBJECT.validate_attempt(jobs, fields, metadata))

    def test_gitlink_tracks_index_identity_and_dirty_submodule_content(self):
        sub = self.repo / "vendor"
        sub.mkdir()
        subprocess.run(["git", "init", "-q", str(sub)], check=True)
        subprocess.run(["git", "-C", str(sub), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(sub), "config", "user.name", "Fixture"], check=True)
        (sub / "library.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(sub), "add", "library.py"], check=True)
        subprocess.run(["git", "-C", str(sub), "commit", "-qm", "submodule"], check=True)
        oid = subprocess.check_output(["git", "-C", str(sub), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(self.repo), "update-index", "--add", "--cacheinfo",
                        f"160000,{oid},vendor"], check=True)
        before = self.observe()["digest"]
        (sub / "library.py").write_text("x = 2\n", encoding="utf-8")
        self.assertNotEqual(before, self.observe()["digest"])

    def test_snapshot_race_stays_unknown_instead_of_publishing_a_mixed_subject(self):
        original_reader = SUBJECT._read_regular

        def race(path):
            result = original_reader(path)
            if path.name == "engine.py":
                path.write_text(path.read_text(encoding="utf-8") + "# race\n", encoding="utf-8")
            return result

        with mock.patch.object(SUBJECT, "_read_regular", side_effect=race):
            with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
                self.observe()
        self.assertEqual(refused.exception.reason, "review-source-file-changed")


if __name__ == "__main__":
    unittest.main()
