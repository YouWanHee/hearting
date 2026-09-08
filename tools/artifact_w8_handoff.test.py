#!/usr/bin/env python3
"""Gate tests for the `--notes` (cairn-w8-notes/v1) input of artifact-w8-handoff.py."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("w8", ROOT / "tools" / "artifact-w8-handoff.py")
w8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w8)

GOOD = {"id": "note-a", "parent_id": None, "page_no": None, "repo": "hearting", "source_dir": "/x/.agent_reports/plans/a.md",
        "source_capability": None, "trashed_at": None, "revision": 1}


def write(tmp, doc):
    path = Path(tmp) / "notes.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class LoadNotesTest(unittest.TestCase):
    def test_accepts_exact_allowlist_and_sorts_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, meta = w8.load_notes(write(tmp, {"schema": w8.NOTES_SCHEMA, "exported_at": "2026-08-26T00:00:00Z",
                                                   "notes": [{**GOOD, "id": "note-b"}, GOOD]}))
        self.assertEqual([r["id"] for r in rows], ["note-a", "note-b"])
        self.assertEqual(set(rows[0]), w8.NOTE_ALLOWED_KEYS)
        self.assertEqual(meta, {"exported_at": "2026-08-26T00:00:00Z"})

    def test_rejects_body_bearing_keys_anywhere(self):
        for bad in ({**GOOD, "body": ""}, {**GOOD, "title": "t"}):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
                w8.load_notes(write(tmp, {"notes": [bad]}))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
            w8.load_notes(write(tmp, {"source": {"body": "leak"}, "notes": [GOOD]}))

    def test_rejects_extra_missing_keys_duplicates_and_wrong_types(self):
        cases = [
            {**GOOD, "card_id": "c1"},
            {k: v for k, v in GOOD.items() if k != "revision"},
            {**GOOD, "page_no": "1"},
            {**GOOD, "id": ""},
        ]
        for bad in cases:
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
                w8.load_notes(write(tmp, {"notes": [bad]}))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
            w8.load_notes(write(tmp, {"notes": [GOOD, dict(GOOD)]}))

    def test_rejects_foreign_schema(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(w8.NotesInputError):
            w8.load_notes(write(tmp, {"schema": "other/v9", "notes": [GOOD]}))


class BundleNotesRowTest(unittest.TestCase):
    def test_existing_notes_row_and_counts(self):
        b = w8.Bundle.__new__(w8.Bundle)
        b.notes = [GOOD, {**GOOD, "id": "note-t", "trashed_at": "2026-01-01T00:00:00Z"}, {**GOOD, "id": "note-o", "repo": "other"}]
        b.notes_meta = {"exported_at": "x"}
        counts = b.existing_note_counts()
        self.assertEqual(counts, {"total": 3, "active": 2, "trashed": 1, "active_by_repo": {"hearting": 1, "other": 1}})
        row = b.existing_notes()
        self.assertEqual(row["schema"], w8.NOTES_SCHEMA)
        self.assertTrue(row["body_free"])
        self.assertEqual(row["columns"], sorted(w8.NOTE_ALLOWED_KEYS))
        self.assertEqual(w8._forbidden_keys(row), [])
        b.notes = None
        self.assertIsNone(b.existing_note_counts())



class PickPrimaryTest(unittest.TestCase):
    def rows(self, *locators):
        return [{"locator": f"campaigns/c/cycles/y/artifacts/plans/x/{l}"} for l in sorted(locators)]

    def test_name_priority_beats_locator_order(self):
        rows = self.rows("_internal/prompts/plan.md", "evidence/a.json", "final_report.md")
        self.assertTrue(w8.pick_primary(rows)["locator"].endswith("/x/final_report.md"))

    def test_shallowest_path_wins_within_a_name(self):
        rows = self.rows("plan/plan.md", "plan.md", "notes.md")
        self.assertTrue(w8.pick_primary(rows)["locator"].endswith("/x/plan.md"))

    def test_falls_back_to_first_row(self):
        rows = self.rows("b.md", "a.md")
        self.assertTrue(w8.pick_primary(rows)["locator"].endswith("/x/a.md"))


# 발급 CLI는 실제 임시 producer cycle에만 쓴다. 운영 root/DB/원장은 사용하지 않는다.
fixture_spec = importlib.util.spec_from_file_location(
    "w16_producer_fixture", ROOT / "utilities" / "artifact_producer.test.py")
producer_fixture = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(producer_fixture)


class BundleBoundaryCLITest(producer_fixture.ProducerTestBase):
    def setUp(self):
        super().setUp()
        self.activate()
        (self.root / "shared").mkdir()
        route, route_file = self.route(slug="w16-source-fixture")
        source = w8.P.begin(self.root, route_file=route_file,
                            capability="autopilot-code", intensity="direct")
        self.write_output(source)
        self.close(route, route_file)
        w8.P.finalize(self.root, cycle_id=source["cycle_id"])
        self.source_cycle = source["cycle_id"]
        self.output_route, self.output_route_file = self.route(slug="w16-bundle-fixture")
        self.output = w8.P.begin(self.root, route_file=self.output_route_file,
                                 capability="autopilot-code", intensity="direct")
        self.output_dir = Path(self.output["cycle_dir"]) / "artifacts" / "plans" / "fixture"
        self.inputs = Path(self._tmp.name) / "inputs"
        self.inputs.mkdir()
        for name in ("compatibility-map.jsonl", "applied-journal.jsonl", "applied-inverse.jsonl"):
            (self.inputs / name).write_text("")
        (self.inputs / "backup-seal.json").write_text("{}")
        (self.inputs / "apply-receipt.json").write_text(json.dumps({
            "file_bytes_before": 0, "file_bytes_after": 0, "byte_loss": 0, "applied_row_count": 0}))
        self.backup = self.inputs / "fixture-backup.tar"
        self.backup.write_bytes(b"fixture backup; never an operational rollback\n")

    def issue(self, name="default", *options, success=True):
        bundle = self.output_dir / name
        command = [sys.executable, str(ROOT / "tools/artifact-w8-handoff.py"),
                   "--artifact-root", str(self.root), "--bundle-dir", str(bundle),
                   "--cycle", self.source_cycle, "--w7-evidence", str(self.inputs),
                   "--w7c-run", str(self.inputs), "--retirement-run", str(self.inputs),
                   "--backup-tar", str(self.backup), *options]
        result = subprocess.run(command, capture_output=True, text=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return bundle, result

    def test_default_and_explicit_w16_are_sealed_but_unauthorized(self):
        old, _ = self.issue()
        new, _ = self.issue("with-w16", "--include-w16-namespace-delete")
        old_boundary = w8.read_json(old / "approval-boundary.json")
        new_boundary = w8.read_json(new / "approval-boundary.json")
        self.assertEqual([s["stage"] for s in old_boundary["stages"]],
                         ["W9-dry-run", "W10-D20-destructive-apply", "W10-note-link-apply"])
        self.assertEqual(new_boundary["stages"][:3], old_boundary["stages"])
        self.assertEqual(len(new_boundary["stages"]), 4)
        self.assertEqual(new_boundary["stages"][-1]["stage"], "W16-namespace-delete")
        self.assertTrue(new_boundary["stages"][-1]["invariant"])
        self.assertEqual(len({s["approval_id"] for s in new_boundary["stages"]}), 4)
        for bundle, boundary in ((old, old_boundary), (new, new_boundary)):
            self.assertTrue(all(s["authorized"] is False for s in boundary["stages"]))
            for stage in boundary["stages"]:
                self.assertRegex(stage["approval_id"], r"^apr_[0-9a-f]{32}$")
            handoff = w8.read_json(bundle / "handoff.json")
            self.assertEqual(handoff["bundle_digest"], w8.sha_text(w8.canonical(handoff["files"])))
            for name, seal in handoff["files"].items():
                self.assertEqual(seal["sha256"], w8.sha_file(bundle / name))
                self.assertEqual(seal["bytes"], (bundle / name).stat().st_size)
        self.assertNotEqual(w8.read_json(old / "handoff.json")["bundle_digest"],
                            w8.read_json(new / "handoff.json")["bundle_digest"])

    def test_invalid_option_refuses_before_output(self):
        bundle, result = self.issue("invalid", "--include-w16-namespace-delete=true", success=False)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(bundle.exists())

    def test_sealed_bundle_cannot_be_reissued_with_w16(self):
        bundle, _ = self.issue()
        before = {p.name: p.read_bytes() for p in bundle.iterdir()}
        self.close(self.output_route, self.output_route_file)
        w8.P.finalize(self.root, cycle_id=self.output["cycle_id"])
        _, result = self.issue("default", "--include-w16-namespace-delete", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bundle dir not writable under the producer contract", result.stderr)
        self.assertEqual(before, {p.name: p.read_bytes() for p in bundle.iterdir()})

    @unittest.skipUnless(os.environ.get("CAIRN_CHECKOUT"), "CAIRN_CHECKOUT으로 실제 reader 검증 선택")
    def test_real_cairn_reader_accepts_bindings_and_rejects_invalid_boundaries(self):
        cairn = Path(os.environ["CAIRN_CHECKOUT"]).resolve()
        old, _ = self.issue()
        new, _ = self.issue("with-w16", "--include-w16-namespace-delete")
        script = Path(self._tmp.name) / "reader.mts"
        script.write_text(r'''
import assert from 'node:assert/strict';
import { readFile, writeFile, cp, rm } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const api = await import(pathToFileURL(process.argv[2]).href);
const [oldDir, newDir, badDir] = process.argv.slice(3);
const oldBoundary = await api.loadSealedApprovalBoundary(oldDir);
const boundary = await api.loadSealedApprovalBoundary(newDir);
const d20 = api.bindingForStage(boundary, 'W10-D20-destructive-apply');
const w16 = api.bindingForStage(boundary, 'W16-namespace-delete');
assert.equal(d20.bundle_digest, w16.bundle_digest);
assert.equal(d20.approval_boundary_digest, w16.approval_boundary_digest);
assert.notEqual(d20.gate_id, w16.gate_id);
for (const binding of [d20, w16]) {
  assert.deepEqual(await api.revalidateApprovalBoundaryBinding(newDir, binding, binding.stage_key), binding);
  assert.equal(boundary.stages[binding.stage_key].authorized, false);
}
const code = (expected) => (e) => e.code === expected;
assert.throws(() => api.bindingForStage(oldBoundary, 'W16-namespace-delete'), code('W10_APPROVAL_BOUNDARY_STAGE_MISSING'));
assert.throws(() => api.assertApprovalBoundaryBinding(boundary, api.bindingForStage(oldBoundary, d20.stage_key), d20.stage_key), code('W10_APPROVAL_BOUNDARY_BINDING_MISMATCH'));
assert.throws(() => api.assertApprovalBoundaryBinding(boundary, { ...w16, gate_id: d20.gate_id }, w16.stage_key), code('W10_APPROVAL_BOUNDARY_BINDING_MISMATCH'));
assert.throws(() => api.assertApprovalBoundaryBinding(boundary, w16, d20.stage_key), code('W10_APPROVAL_STAGE_MISMATCH'));
const original = JSON.parse(await readFile(`${newDir}/approval-boundary.json`, 'utf8'));
for (const [kind, mutate, expected] of [
  ['preauthorized', (d) => d.stages[3].authorized = true, 'W10_APPROVAL_BOUNDARY_PREAUTHORIZED'],
  ['missing-required', (d) => d.stages.splice(1, 1), 'W10_APPROVAL_BOUNDARY_STAGE_MISSING'],
  ['unknown', (d) => d.stages[3].stage = 'W17-delete', 'W10_APPROVAL_BOUNDARY_STAGE_UNKNOWN'],
  ['duplicate', (d) => d.stages[3] = d.stages[0], 'W10_APPROVAL_BOUNDARY_STAGE_DUPLICATE'],
  ['bad-id', (d) => d.stages[3].approval_id = 'not-an-id', 'W10_APPROVAL_ID_INVALID'],
  ['empty-invariant', (d) => d.stages[3].invariant = '', 'W10_APPROVAL_BOUNDARY_STAGE_INVALID'],
]) {
  const changed = structuredClone(original); mutate(changed);
  assert.throws(() => api.decodeApprovalBoundary(changed, w16.bundle_digest, w16.approval_boundary_digest), code(expected), kind);
}
await cp(newDir, badDir, { recursive: true });
await writeFile(`${badDir}/approval-boundary.json`, JSON.stringify({ ...original, note: 'tampered fixture' }));
await assert.rejects(() => api.loadSealedApprovalBoundary(badDir), code('W10_APPROVAL_BOUNDARY_DIGEST_MISMATCH'));
await rm(`${badDir}/approval-boundary.json`);
await assert.rejects(() => api.loadSealedApprovalBoundary(badDir), code('W10_APPROVAL_BOUNDARY_MISSING'));
console.log(JSON.stringify({ status: 'PASS', d20, w16, negative_cases: 12, database_access: false }));
''')
        result = subprocess.run([
            "node", "--import", str(cairn / "node_modules/tsx/dist/loader.mjs"), str(script),
            str(cairn / "lib/artifact-reconciliation/approval-boundary.ts"),
            str(old), str(new), str(self.output_dir / "tampered-fixture"),
        ], capture_output=True, text=True, cwd=cairn)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print("cairn-reader=" + result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
