#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


TOOL = Path(__file__).with_name("artifact-relocation-live.py")
SPEC = importlib.util.spec_from_file_location("artifact_relocation_live", TOOL)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def write_json(path: Path, value: dict) -> None:
    path.write_bytes(M.canonical(value))


class LiveRelocationTest(unittest.TestCase):
    def quiescence_fixture(self, base):
        spec = importlib.util.spec_from_file_location("quiescence_fixture_helpers",
            Path(__file__).with_name("artifact-quiescence.test.py"))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        helper = module.QuiescenceTest(); config = helper.fixture(base)
        root = Path(config["artifact_root"])
        other = helper.artifact_root(base, "external")
        registry = root / "runtime-resource.json"
        registry.write_text(json.dumps({"schema_version": 1, "runs": {
            "external": {"status": "running", "artifact_root": str(other)}}}))
        helper.indexed(config, registry)
        Path(config["dispatch_jobs"]).write_text("".join(
            helper.dispatch_row(slug=str(i), attempt=f"att-{i}",
                                metadata={"artifact_root": other}) for i in range(2)))
        source = base / "raw.json"
        value = module.Q.publish(str(source), config)
        self.assertTrue(value["proven"], value)
        return module.Q, root, source, value

    def test_scoped_quiescence_consumes_sealed_attribution_not_registry_location(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); _, root, source, _ = self.quiescence_fixture(base)
            output = base / "scoped.json"
            args = argparse.Namespace(input=str(source), artifact_root=str(root), output=str(output), allow_fixture=True)
            # Fixture opt-in is an internal test parameter, never inferred from the input.
            status = M.scoped_quiescence(args)
            result = json.loads(output.read_text())
            self.assertEqual(status, M.OK)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["scoped_open_jobs"], 0)
            self.assertEqual(result["external_open_jobs"], 1)
            self.assertEqual(result["external_open_dispatch_attempts"], 2)

    def test_review_valid_open_route_requires_transactional_override(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); validator, root, source, initial = self.quiescence_fixture(base)
            spec = importlib.util.spec_from_file_location("quiescence_open_route_fixture",
                Path(__file__).with_name("artifact-quiescence.test.py"))
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            module.QuiescenceTest().sealed_route(root)
            value = validator.publish(str(source), initial["config"])
            self.assertTrue(value["observation_valid"])
            self.assertEqual(value["open_routes"], 1)
            self.assertFalse(value["proven"])
            output = base / "open-route-scoped.json"
            args = argparse.Namespace(input=str(source), artifact_root=str(root), output=str(output), allow_fixture=True)
            self.assertEqual(M.scoped_quiescence(args), M.OK)
            scoped = json.loads(output.read_text())
            self.assertFalse(scoped["strict_a13_quiescence"])
            self.assertTrue(scoped["transactional_override_required"])
            self.assertFalse(validator.validate(str(source), allow_fixture=True)["proven"])

    def test_review_consumer_rejects_invalid_evidence(self):
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); _, root, source, original = self.quiescence_fixture(base)
            now = datetime.now(timezone.utc)
            mutations = [
                {"pending": 1}, {"proven": False}, {"observation_valid": False},
                {"observed_at": (now - timedelta(seconds=400)).isoformat()},
                {"observed_at": (now + timedelta(seconds=100)).isoformat()},
                {"schema_version": 3}, {"sources": {}},
            ]
            for ordinal, mutation in enumerate(mutations):
                with self.subTest(mutation=mutation):
                    value = json.loads(json.dumps(original)); value.update(mutation)
                    write_json(source, value)
                    args = argparse.Namespace(input=str(source), artifact_root=str(root),
                                              output=str(base / f"bad-scoped-{ordinal}.json"), allow_fixture=True)
                    with self.assertRaises(ValueError):
                        M.scoped_quiescence(args)

    def fixture(self, base: Path):
        root = base / "artifacts"
        root.mkdir()
        (root / ".pipeline-lock").write_bytes(b"")
        (root / "plans" / "cycle").mkdir(parents=True)
        source = root / "plans" / "cycle" / "report.md"
        source.write_bytes(b"payload\n")
        source.chmod(0o640)
        ledger = base / "ledger.json"
        ledger.write_bytes(b"ledger\n")
        rows = [
            {
                "row_ordinal": 0,
                "source_locator": "plans/cycle",
                "target_locator": "campaigns/camp/cycles/cycle/artifacts/plans/cycle",
                "kind": "directory",
                "mode": (root / "plans" / "cycle").stat().st_mode & 0o7777,
                "size": None,
                "sha256": None,
                "source_row_key": "dir",
                "identity_refs": [{"stable_id": "camp_1"}],
            },
            {
                "row_ordinal": 1,
                "source_locator": "plans/cycle/report.md",
                "target_locator": "campaigns/camp/cycles/cycle/artifacts/plans/cycle/report.md",
                "kind": "file",
                "mode": 0o640,
                "size": 8,
                "sha256": hashlib.sha256(b"payload\n").hexdigest(),
                "source_row_key": "file",
                "identity_refs": [{"stable_id": "art_1"}],
            },
        ]
        plan = {
            "schema_version": M.PLAN_SCHEMA,
            "status": "ready",
            "source_preservation_required": True,
            "source_retirement_authorized": False,
            "cairn_access_authorized": False,
            "d20_authorized": False,
            "artifact_root_identity": str(root),
            "baseline_sha256": "a" * 64,
            "manifest_sha256": "b" * 64,
            "target_set_sha256": "c" * 64,
            "identity_ledger_sha256": M.digest_file(ledger),
            "mapping_sha256": "d" * 64,
            "row_count": 2,
            "kind_counts": {"directory": 1, "file": 1},
            "file_bytes": 8,
            "collision_counts": {"byte": 0},
            "rows": rows,
        }
        plan["plan_sha256"] = M.plan_digest(plan)
        plan_path = base / "plan.json"
        write_json(plan_path, plan)
        return root, ledger, plan_path

    def test_rehearsal_and_rollback_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, _, plan_path = self.fixture(base)
            receipts = []
            journals = []
            inverses = []
            for index in range(2):
                workspace = base / f"work-{index}"
                workspace.mkdir()
                journal = base / f"journal-{index}.jsonl"
                inverse = base / f"inverse-{index}.jsonl"
                receipt = base / f"receipt-{index}.json"
                args = argparse.Namespace(plan=str(plan_path), artifact_root=str(root),
                                          workspace=str(workspace), journal=str(journal),
                                          inverse_journal=str(inverse), output=str(receipt))
                self.assertEqual(M.rehearse(args), 0)
                rollback = base / f"rollback-{index}.json"
                self.assertEqual(M.rollback_rehearsal(argparse.Namespace(
                    workspace=str(workspace), journal=str(journal),
                    inverse_journal=str(inverse), output=str(rollback))), 0)
                self.assertFalse(any(workspace.iterdir()))
                receipts.append(receipt.read_bytes())
                journals.append(journal.read_bytes())
                inverses.append(inverse.read_bytes())
            self.assertEqual(receipts[0], receipts[1])
            self.assertEqual(journals[0], journals[1])
            self.assertEqual(inverses[0], inverses[1])
            self.assertEqual((root / "plans/cycle/report.md").read_bytes(), b"payload\n")

    def test_apply_is_additive_no_replace_and_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, ledger, plan_path = self.fixture(base)
            package = base / "package.json"
            package_body = {
                "schema_version": M.PACKAGE_SCHEMA,
                "status": "pass",
                "strict_a13_quiescence": True,
                "exact_approval_values": {"baseline_sha256": "a" * 64},
            }
            write_json(package, package_body)
            approval = base / "approval.json"
            write_json(approval, {
                "schema_version": M.APPROVAL_SCHEMA,
                "status": "approved",
                "package_sha256": M.digest_file(package),
                "exact_approval_values": package_body["exact_approval_values"],
                "registry_quiescence_override": False,
            })
            journal = base / "applied.jsonl"
            inverse = base / "inverse.jsonl"
            receipt = base / "apply.json"
            args = argparse.Namespace(
                plan=str(plan_path), package=str(package), approval=str(approval),
                artifact_root=str(root), identity_ledger=str(ledger),
                journal=str(journal), inverse_journal=str(inverse), output=str(receipt))
            self.assertEqual(M.apply(args), 0)
            source = root / "plans/cycle/report.md"
            target = root / "campaigns/camp/cycles/cycle/artifacts/plans/cycle/report.md"
            self.assertEqual(source.read_bytes(), target.read_bytes())
            verify_path = base / "verify.json"
            self.assertEqual(M.verify(argparse.Namespace(
                plan=str(plan_path), artifact_root=str(root),
                identity_ledger=str(ledger), output=str(verify_path))), 0)
            self.assertEqual(json.loads(verify_path.read_text())["byte_loss"], 0)
            with self.assertRaises(ValueError):
                M.apply(argparse.Namespace(
                    plan=str(plan_path), package=str(package), approval=str(approval),
                    artifact_root=str(root), identity_ledger=str(ledger),
                    journal=str(base / "second.jsonl"), inverse_journal=str(base / "second-inverse.jsonl"),
                    output=str(base / "second.json")))
            self.assertEqual(source.read_bytes(), b"payload\n")

    def test_source_drift_fails_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, _, plan_path = self.fixture(base)
            (root / "plans/cycle/report.md").write_bytes(b"changed\n")
            with self.assertRaises(ValueError):
                M.dry_run(argparse.Namespace(plan=str(plan_path), artifact_root=str(root),
                                             output=str(base / "out.json")))
            self.assertFalse((root / "campaigns").exists())

    def test_top_level_census_classifies_compatibility_and_canonical_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, _, plan_path = self.fixture(base)
            (root / "campaigns").mkdir()
            (root / "shared").mkdir()
            baseline = base / "baseline.jsonl"
            baseline.write_bytes(b"".join(M.canonical(row) for row in [
                {"path": ".pipeline-lock", "record_type": "file"},
                {"path": "plans", "record_type": "dir"},
            ]))
            delta = base / "delta.json"
            write_json(delta, {
                "schema_version": "artifact-relocation-deterministic-delta/v1",
                "baseline_sha256": "a" * 64,
                "self_write_scope": "plans/cycle",
                "rows": [],
            })
            output = base / "census.json"
            self.assertEqual(M.top_level_census(argparse.Namespace(
                plan=str(plan_path), artifact_root=str(root), baseline=str(baseline),
                delta=str(delta), output=str(output))), 0)
            body = json.loads(output.read_text())
            self.assertEqual(body["unclassified_top_level_count"], 0)
            self.assertEqual(body["c_unk"], 0)


if __name__ == "__main__":
    unittest.main()
