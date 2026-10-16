import json
from pathlib import Path
import tempfile
import unittest

import resource_run_evidence as evidence


class ResourceRunEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root / "artifacts"
        self.route_file = self.root / "route.json"

    def tearDown(self):
        self.temp.cleanup()

    def route(self, *, mapped=False):
        output = "parts/autopilot-lab/full-run/run.json" if mapped else "run.json"
        node = {"id": "resource", "outputs": [output], "write_scope": [output]}
        if mapped:
            node.update(part="autopilot-lab:full-run", part_io={"run.json": output})
        route = {"route_id": "rt-test", "route_hash": "sha256:" + "a" * 64,
                 "artifact_root": str(self.root), "nodes": [node]}
        self.route_file.write_text(json.dumps(route))
        return {"artifact_base": str(self.base), "declared_outputs": [output],
                "route_file": str(self.route_file), "route_id": route["route_id"],
                "route_hash": route["route_hash"], "node": "resource"}

    def test_native_run_record_stays_at_cycle_root_without_writes(self):
        receipt = evidence.paths(self.route(), prepare=False)
        self.assertEqual(receipt["expected_outputs"], [str(self.base / "run.json")])
        self.assertEqual(receipt["runtime_output"], str(self.base / "run.json"))
        self.assertFalse(self.base.exists())

    def test_borrowed_run_record_uses_its_mapped_declaration_without_root_copy(self):
        receipt = evidence.paths(self.route(mapped=True), prepare=False)
        mapped = self.base / "parts/autopilot-lab/full-run/run.json"
        self.assertEqual(receipt["expected_outputs"], [str(mapped)])
        self.assertEqual(receipt["runtime_output"], str(mapped))
        self.assertFalse((self.base / "run.json").exists())
        self.assertFalse(self.base.exists())


if __name__ == "__main__":
    unittest.main()
