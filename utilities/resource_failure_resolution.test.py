#!/usr/bin/env python3
"""Preserved nonzero exits can finish only through current same-byte verification."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("resolution_fixture", HERE / "workflow_supervisor.test.py")
FIX = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = FIX
spec.loader.exec_module(FIX)
SUP, WS = FIX.SUP, FIX.WS
import resource_failure_resolution as R
import dispatch_resource_wait as WAIT


class FailedOutputResolutionTest(FIX.WorkflowFixture):
    _resume_fixture = FIX.TestSupervisorAdvance._resume_fixture
    _settle_resource_owner = FIX.TestSupervisorAdvance._settle_resource_owner

    def fixture(self):
        supervisor_patch = mock.patch.object(WAIT, 'supervisor', return_value=SUP)
        supervisor_patch.start()
        self.addCleanup(supervisor_patch.stop)
        route, path, jobs, registry, output = self._resume_fixture(ordinary=True)
        resource = copy.deepcopy(WS.route_node(route, "full-run"))
        resource["depends_on"] = []
        route["nodes"] = [resource,
            {"id": "metrics", "kind": "pipeline-stage", "depends_on": ["full-run"],
             "outputs": ["summary.json", "metrics.jsonl"], "completion_gate": "metrics"},
            {"id": "verify", "kind": "review-worker", "depends_on": ["metrics"],
             "completion_gate": "independent-verify", "terminal_gate": "independent-verify", "terminal": True}]
        path.write_text(json.dumps(route))
        data = json.loads(registry.read_text())
        row = data["runs"]["fixture-run"]
        row.update(status="failed", exit_code=1, workflow_state="FAILED_RETRYABLE",
                   config_sha256="a" * 64, parent_attempt_id="att-parent")
        Path(row["sentinel"]).write_text("1")
        registry.write_text(json.dumps(data))
        ledger = SUP.ledger_for(route, jobs)
        arm_path = ledger.root / "armed/full-run.json"
        armed = json.loads(arm_path.read_text())
        armed["resource_binding"] = WAIT.resource_body_digest(row)
        arm_path.write_text(json.dumps(armed))
        raw = output / "raw.json"
        raw.write_text('{"scientific_verdict":"FAIL","data":[1,2,3]}')
        receipt = output / "run.json"
        receipt.write_text(json.dumps({"route_id": route["route_id"], "exit": 1,
            "config_sha256": row["config_sha256"], "outputs": [{"path": "raw.json", "sha256": R.digest(raw)}]}))
        correction = output / "correction.json"
        correction.write_text(json.dumps({"route_id": route["route_id"], "node": "full-run",
            "gate_evidence_verdict": "PASS", "artifact_sha256": {"raw.json": R.digest(raw)}}))
        sidecar = output / "metrics.jsonl"
        sidecar.write_text(json.dumps({"evidence_path": str(raw), "input_sha256": R.digest(raw)}) + "\n")
        summary = output / "summary.json"
        summary.write_text(json.dumps({"run_sha256": R.digest(receipt), "metrics_sha256": R.digest(sidecar)}))
        verdict = output / "verdict.json"
        verdict.write_text(json.dumps({"verdict": "PASS", "evidence_hashes": {"summary.json": R.digest(summary)}}))
        directory = SUP.route_module().completion_dir(route["route_id"], jobs=jobs)
        directory.mkdir(parents=True, exist_ok=True)
        for node, evidence in ((resource, correction), (route["nodes"][1], summary), (route["nodes"][2], verdict)):
            marker = {"schema_version": 2, "sequence": 1, "registry_digest": route["registry_digest"],
                      "route_id": route["route_id"], "route_hash": route["route_hash"],
                      "node_id": node["id"], "completion_gate": node["completion_gate"],
                      "registered_worker": False, "evidence": {"path": str(evidence), "sha256": R.digest(evidence)}}
            if node["id"] == "verify":
                marker["review_independence"] = "independent"
            (directory / (node["id"] + ".json")).write_text(json.dumps(marker))
            (directory / (node["id"] + ".1.json")).write_text(json.dumps(marker))
        evidence = SUP.resource_evidence(armed)
        ledger.record("full-run", "FAILED_RETRYABLE", evidence=evidence)
        ledger.set_workflow_state("FAILED_RETRYABLE", evidence={"node": "full-run"})
        gates = {"verify": {"passed": True}}
        # Dispatch authority is tested by the shared marker suites. Here the
        # real identity/evidence-currency reader still detects modified markers
        # and bytes, while fixture-only native dispatch receipts are omitted.
        module = SUP.route_module()
        reader = module._marker_identity_row
        patch = mock.patch.object(module, "_marker_identity_row", side_effect=lambda *a, **kw:
            reader(*a, **{**kw, "exact_terminal": False}))
        patch.start()
        self.addCleanup(patch.stop)
        return route, path, jobs, registry, output, ledger, gates

    def test_original_failure_blocks_then_same_bytes_close_without_rewriting_exit_or_artifacts(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        before = ledger.journal_path.read_bytes()
        protected = {p: p.read_bytes() for p in [registry, *output.glob("*"),
                     *SUP.route_module().completion_dir(route["route_id"], jobs=jobs).glob("*.json")]}
        with self.assertRaises(WS.WorkflowStateError):
            ledger.complete(["verify"], gates)
        result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual((result.result, commits), ("completed", 1), result)
        self.assertEqual(ledger.state()["workflow_state"], "COMPLETE")
        self.assertTrue(ledger.journal_path.read_bytes().startswith(before))
        self.assertEqual(ledger.claims(), {})
        for p, raw in protected.items():
            self.assertEqual(p.read_bytes(), raw)
        proof = ledger.state()["nodes"]["full-run"]["evidence"]["resolved_resource_failure"]
        self.assertEqual(proof["original_failure"]["exit_code"], 1)
        self.assertEqual(proof["independent_nodes"], ["verify"])
        self.assertEqual(len(proof["consumed_artifacts"]), 1)
        journal = ledger.journal_path.read_bytes()
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, "completed")
        self.assertEqual(ledger.journal_path.read_bytes(), journal)

    def test_changed_output_keeps_failure_and_returns_an_existing_executable_next_leg(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        (output / "raw.json").write_text('{"data":[9]}')
        before = ledger.journal_path.read_bytes(), registry.read_bytes()
        result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual(result.reason, "resource-failure-unresolved")
        self.assertEqual(commits, 0)
        self.assertEqual((ledger.journal_path.read_bytes(), registry.read_bytes()), before)
        self.assertIn("capability-route compose --start --shape staged", result.next_step["command"])
        self.assertEqual(result.next_step["run_id"], "fixture-run__a1")
        self.assertEqual(result.next_step["revalidate_nodes"], ["metrics", "verify"])
        self.assertIn("new cycle", result.next_step["command"])

    def test_independent_pass_and_current_marker_without_consumer_hash_are_insufficient(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        summary = output / "summary.json"
        summary.write_text('{"owner_says":"PASS"}')
        marker = SUP.route_module().completion_dir(route["route_id"], jobs=jobs) / "metrics.json"
        data = json.loads(marker.read_text())
        data["evidence"]["sha256"] = R.digest(summary)
        marker.write_text(json.dumps(data))
        marker.with_name('metrics.1.json').write_text(json.dumps(data))
        verdict = output / 'verdict.json'
        verdict.write_text(json.dumps({'verdict': 'PASS', 'summary_sha256': R.digest(summary)}))
        self.update_marker(route, jobs, 'verify', verdict)
        result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual((result.reason, commits), ("resource-failure-unresolved", 0))
        self.assertIn("same-byte-consumer-binding-absent", result.detail)

    def test_owner_override_and_stale_consumer_cannot_resolve(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        directory = SUP.route_module().completion_dir(route["route_id"], jobs=jobs)
        marker = directory / "verify.json"
        data = json.loads(marker.read_text())
        data["review_independence"] = "owner-overridden"
        marker.write_text(json.dumps(data))
        marker.with_name('verify.1.json').write_text(json.dumps(data))
        self.assertIn("independent-pass-absent", self._settle_resource_owner(route, path, jobs)[0].detail)
        data["review_independence"] = "independent"
        marker.write_text(json.dumps(data))
        marker.with_name('verify.1.json').write_text(json.dumps(data))
        (output / "summary.json").write_text('{}')
        self.assertIn("artifact-changed", self._settle_resource_owner(route, path, jobs)[0].detail)

    def update_marker(self, route, jobs, node, evidence):
        directory = SUP.route_module().completion_dir(route['route_id'], jobs=jobs)
        path = directory / (node + '.json')
        data = json.loads(path.read_text())
        data['evidence']['sha256'] = R.digest(evidence)
        for name in (node + '.json', node + '.1.json'):
            (directory / name).write_text(json.dumps(data))

    def test_independent_pass_without_current_consumer_admission_cannot_resolve(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        verdict = output / 'verdict.json'
        verdict.write_text('{"verdict":"PASS"}')
        self.update_marker(route, jobs, 'verify', verdict)
        result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual((result.reason, commits), ('resource-failure-unresolved', 0))
        self.assertIn('same-byte-consumer-binding-absent', result.detail)

    def test_consumer_change_after_marker_validation_is_rejected_without_appending(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        before = ledger.journal_path.read_bytes()
        module = SUP.route_module()
        reader = module._marker_identity_row
        def swap(*args, **kwargs):
            result = reader(*args, **kwargs)
            if args[2] == 'metrics':
                (output / 'summary.json').write_text('{"owner_says":"PASS"}')
            return result
        with mock.patch.object(module, '_marker_identity_row', side_effect=swap):
            result, commits = self._settle_resource_owner(route, path, jobs)
        self.assertEqual((result.reason, commits), ('resource-failure-unresolved', 0))
        self.assertIn('bound-artifact-changed', result.detail)
        self.assertEqual(ledger.journal_path.read_bytes(), before)

    def test_unequal_expected_actual_is_not_an_admitted_hash(self):
        self.assertNotIn('a' * 64, R.hashes({'expected': 'a' * 64, 'actual': 'b' * 64, 'match': False}))

    def test_missing_or_damaged_watch_keeps_failure_and_names_the_original_resource(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        armed = ledger.root / 'armed/full-run.json'
        original = armed.read_bytes()
        before = ledger.journal_path.read_bytes()
        for value in ({}, [], None, {'node': 'other', 'predecessor_kind': 'resource'}):
            with self.subTest(value=value):
                armed.write_text(json.dumps(value))
                result, commits = self._settle_resource_owner(route, path, jobs)
                self.assertEqual((result.reason, commits), ('resource-failure-unresolved', 0))
                self.assertEqual(result.next_step['run_id'], 'fixture-run__a1')
                self.assertIn('--graph full-run,metrics,verify', result.next_step['command'])
                self.assertEqual(ledger.journal_path.read_bytes(), before)
        armed.unlink()
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].reason, 'resource-failure-unresolved')
        armed.write_bytes(original)
        for value in (None, [], 1):
            registry.write_text(json.dumps({'runs': value}))
            result, commits = self._settle_resource_owner(route, path, jobs)
            self.assertEqual((result.reason, commits), ('resource-failure-unresolved', 0))
            self.assertEqual(result.next_step['run_id'], 'fixture-run__a1')
            self.assertEqual(ledger.journal_path.read_bytes(), before)

    def test_interrupted_resolution_resumes_each_append_and_preserves_failure_prefix(self):
        route, path, jobs, registry, output, ledger, gates = self.fixture()
        before = ledger.journal_path.read_bytes()
        append = WS.WorkflowLedger._append
        for target in ("READY", "RUNNING", "STAGE_SUCCEEDED"):
            def interrupt(instance, entry):
                append(instance, entry)
                if entry.get("node") == "full-run" and entry.get("state") == target:
                    raise OSError("interrupted after append")
            with mock.patch.object(WS.WorkflowLedger, "_append", interrupt):
                self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, "recoverable")
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, "completed")
        self.assertTrue(ledger.journal_path.read_bytes().startswith(before))
        self.assertEqual([e["state"] for e in ledger.journal() if e.get("node") == "full-run"][-4:],
                         ["FAILED_RETRYABLE", "READY", "RUNNING", "STAGE_SUCCEEDED"])


if __name__ == "__main__":
    unittest.main()
