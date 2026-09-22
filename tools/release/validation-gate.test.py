#!/usr/bin/env python3
"""Exercise release admission and the workflow's prerequisite wiring."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("validation_gate", HERE / "validation-gate.py")
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
HEAD = "a" * 40
TIP = "b" * 40
REPO = {"full_name": "owner/repo", "id": 42}


class ReleaseGateTest(unittest.TestCase):
    def setUp(self):
        self.context = {
            "GITHUB_REPOSITORY": "owner/repo", "GITHUB_REPOSITORY_ID": "42",
            "GITHUB_EVENT_NAME": "workflow_run", "GITHUB_SHA": TIP,
            "GITHUB_REF": "refs/heads/main",
        }
        self.event = {
            "action": "completed", "repository": copy.deepcopy(REPO),
            "workflow_run": {
                "name": "Checks", "path": ".github/workflows/checks.yml",
                "event": "push", "status": "completed", "conclusion": "success",
                "head_branch": "main", "head_sha": HEAD, "head_commit": {"id": HEAD},
                "repository": copy.deepcopy(REPO), "head_repository": copy.deepcopy(REPO),
            },
        }

    def test_uses_tested_sha_even_when_default_branch_has_advanced(self):
        self.assertEqual(GATE.admit(self.event, self.context),
                         {"head": HEAD, "validation_required": "false"})

    def test_unsuccessful_or_incomplete_checks_never_admit(self):
        for conclusion in ("failure", "cancelled", "skipped", "neutral", "timed_out", None):
            with self.subTest(conclusion=conclusion):
                self.event["workflow_run"]["conclusion"] = conclusion
                with self.assertRaises(GATE.Rejected):
                    GATE.admit(self.event, self.context)
        self.event["workflow_run"]["conclusion"] = "success"
        self.event["workflow_run"]["status"] = "in_progress"
        with self.assertRaises(GATE.Rejected):
            GATE.admit(self.event, self.context)

    def test_pr_branch_workflow_and_sha_substitutions_are_rejected(self):
        for field, value in (("event", "pull_request"), ("event", "workflow_dispatch"),
                             ("head_branch", "feature"), ("name", "Other"),
                             ("path", ".github/workflows/other.yml"), ("head_sha", TIP),
                             ("head_sha", "main"), ("head_commit", {"id": TIP})):
            with self.subTest(field=field, value=value):
                event = copy.deepcopy(self.event)
                event["workflow_run"][field] = value
                with self.assertRaises(GATE.Rejected):
                    GATE.admit(event, self.context)

    def test_foreign_or_missing_repository_identity_is_rejected(self):
        for location in ("event", "repository", "head_repository"):
            for replacement in ({"id": 99, "full_name": "owner/repo"},
                                {"id": 42, "full_name": "fork/repo"}, None):
                with self.subTest(location=location, replacement=replacement):
                    event = copy.deepcopy(self.event)
                    if location == "event":
                        event["repository"] = replacement
                    else:
                        event["workflow_run"][location] = replacement
                    with self.assertRaises(GATE.Rejected):
                        GATE.admit(event, self.context)

    def test_tag_and_manual_paths_require_fresh_validation_at_caller_sha(self):
        for kind, ref in (("push", "refs/tags/v1.2.3"),
                          ("workflow_dispatch", "refs/heads/main"),
                          ("workflow_dispatch", "refs/tags/v1.2.3-rc.1")):
            with self.subTest(kind=kind, ref=ref):
                context = dict(self.context, GITHUB_EVENT_NAME=kind, GITHUB_REF=ref)
                event = {"repository": REPO, "ref": ref, "after": TIP, "deleted": False}
                self.assertEqual(GATE.admit(event, context),
                                 {"head": TIP, "validation_required": "true"})

    def test_annotated_tag_object_does_not_replace_runner_commit(self):
        context = dict(self.context, GITHUB_EVENT_NAME="push", GITHUB_REF="refs/tags/v1.2.3")
        event = {"repository": REPO, "ref": "refs/tags/v1.2.3", "after": HEAD, "deleted": False}
        self.assertEqual(GATE.admit(event, context)["head"], TIP)

    def test_unsupported_events_refs_and_deleted_tags_fail(self):
        for kind, ref, after, deleted in (
                ("push", "refs/heads/main", TIP, False),
                ("pull_request", "refs/heads/main", TIP, False),
                ("workflow_dispatch", "refs/heads/feature", TIP, False),
                ("push", "refs/tags/v1.2.3", TIP, True)):
            with self.subTest(kind=kind, ref=ref, after=after, deleted=deleted):
                context = dict(self.context, GITHUB_EVENT_NAME=kind, GITHUB_REF=ref)
                event = {"repository": REPO, "ref": ref, "after": after, "deleted": deleted}
                with self.assertRaises(GATE.Rejected):
                    GATE.admit(event, context)

    def test_cli_emits_no_outputs_on_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            env = dict(os.environ, **self.context, GITHUB_EVENT_PATH=str(path))
            for success in (True, False):
                self.event["workflow_run"]["conclusion"] = "success" if success else "failure"
                path.write_text(json.dumps(self.event))
                result = subprocess.run([sys.executable, str(HERE / "validation-gate.py")],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if success else 1)
                self.assertEqual(result.stdout, f"head={HEAD}\nvalidation_required=false\n" if success else "")

    def test_actual_publish_condition_requires_admission_and_validation(self):
        workflow = (HERE.parents[1] / ".github/workflows/release.yml").read_text()
        release = workflow.split("\n  release:\n", 1)[1]
        condition = re.search(r"    if: >-\n((?:      .+\n)+)", release).group(1).strip()
        for admitted in ("success", "failure", "skipped", "cancelled"):
            for validation in ("success", "failure", "skipped", "cancelled"):
                for required in ("true", "false", ""):
                    for cancelled in (False, True):
                        with self.subTest(admitted=admitted, validation=validation,
                                          required=required, cancelled=cancelled):
                            expression = condition.replace("always()", "True")
                            expression = expression.replace("!cancelled()", str(not cancelled))
                            expression = expression.replace("needs.admission.result", repr(admitted))
                            expression = expression.replace("needs.validation.result", repr(validation))
                            expression = expression.replace("needs.admission.outputs.validation_required", repr(required))
                            expression = expression.replace("&&", " and ").replace("||", " or ")
                            actual = eval(" ".join(expression.split()), {"__builtins__": {}})
                            expected = (not cancelled and admitted == "success"
                                        and (required == "false" or
                                             (required == "true" and validation == "success")))
                            self.assertEqual(actual, expected)
        self.assertIn("needs: [admission, validation]", release)
        self.assertNotIn("${{ github.sha }}", release)
        self.assertIn("uses: ./.github/workflows/checks.yml", workflow)
        checks = (HERE.parents[1] / ".github/workflows/checks.yml").read_text()
        self.assertIn("  workflow_call:", checks)
        self.assertEqual(checks.count("ref: ${{ github.sha }}"), 4)

    def test_denied_checks_never_enter_publish_serialization(self):
        workflow = (HERE.parents[1] / ".github/workflows/release.yml").read_text()
        before_publish, publish = workflow.split("\n  release:\n", 1)
        self.assertNotRegex(before_publish, r"(?m)^\s*concurrency:")
        self.assertRegex(publish, r"    concurrency:\n      group: release\n      cancel-in-progress: false")
        # GitHub's concurrency queue replaces an older pending job even with
        # cancel-in-progress=false. Failed admission must therefore stop before
        # the only job owning that queue; the expression matrix above exercises
        # failure/cancellation against that actual job's condition.
        for conclusion in ("failure", "cancelled"):
            self.event["workflow_run"]["conclusion"] = conclusion
            with self.assertRaises(GATE.Rejected):
                GATE.admit(self.event, self.context)

    def test_actual_checkout_guard_rejects_foreign_commit(self):
        workflow = (HERE.parents[1] / ".github/workflows/release.yml").read_text()
        step = workflow.split("- name: Verify the checked out commit\n", 1)[1].split("\n      - name:", 1)[0]
        script = re.search(r"        run: (.+)", step).group(1)
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
            subprocess.run(["git", "init", "-q", directory], env=env, check=True)
            subprocess.run(["git", "-C", directory, "commit", "--allow-empty", "-qm", "fixture"], env=env, check=True)
            actual = subprocess.check_output(["git", "-C", directory, "rev-parse", "HEAD"], env=env, text=True).strip()
            for expected in (actual, HEAD):
                result = subprocess.run(["bash", "-e", "-c", script], cwd=directory,
                                        env=dict(env, RELEASE_HEAD=expected))
                self.assertEqual(result.returncode, 0 if expected == actual else 1)


if __name__ == "__main__":
    unittest.main()
