#!/usr/bin/env python3
"""Read-only lifecycle projection fixtures for Fleet human-gate waits."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import human_gate_observation as H  # noqa: E402
from route_identity import route_hash, route_id_from_hash  # noqa: E402


class PendingWaitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.interview = self.artifacts / "interview.json"
        self.interview.write_text("{}\n", encoding="utf-8")
        self.route = {"artifact_root": str(self.artifacts), "nodes": []}
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        ledger = self.root / "workflow" / self.route["route_id"] / "journal.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({
            "at": "2026-10-14T01:02:03Z", "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"], "workflow_state": "BLOCKED_HUMAN_GATE",
            "evidence": {"gate": "frame-review", "artifact": str(self.interview)},
        }) + "\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_actual_raise_is_projected_without_interview_content(self):
        waits = H.pending_human_waits(self.route, jobs=self.jobs)
        self.assertEqual(len(waits), 1)
        self.assertEqual((waits[0]["kind"], waits[0]["source"]), ("decision", "hearting-ledger"))
        self.assertEqual(set(waits[0]), {"gate_id", "kind", "source", "waiting_since", "route_id"})

    def test_mismatched_route_identity_has_no_observation(self):
        route = dict(self.route, route_id="rt-unrelated")
        self.assertEqual(H.pending_human_waits(route, jobs=self.jobs), [])

    def test_journal_parse_cache_invalidates_on_same_size_rewrite(self):
        import workflow_state as WS
        ledger = WS.WorkflowLedger(self.route["route_id"], self.route["route_hash"], jobs=self.jobs)
        first = H._journal(ledger)
        with mock.patch.object(H.json, "loads", wraps=H.json.loads) as loads:
            self.assertEqual(H._journal(ledger), first)
            self.assertEqual(loads.call_count, 0)
            raw = ledger.journal_path.read_text(encoding="utf-8")
            changed = raw.replace("BLOCKED_HUMAN_GATE", "CANCELLED_________")
            self.assertEqual(len(changed), len(raw))
            ledger.journal_path.write_text(changed, encoding="utf-8")
            parsed = H._journal(ledger)
            self.assertEqual(loads.call_count, 1)
            self.assertEqual(parsed[0]["workflow_state"], "CANCELLED_________")


if __name__ == "__main__":
    unittest.main()
