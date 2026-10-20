import unittest
from tools.fleet.explorer.ledger import build_ledger

class LedgerTests(unittest.TestCase):
    def test_policy_outcome_and_gate_are_separate_and_latest_attempt_wins(self):
        rows = build_ledger([{"node_id":"test"}], [
            {"attempt_id":"a1", "route_node":"test", "harness":"codex", "liveness":"dead", "attempt_decision":{"outcome":"failed", "action":"inspect-failure", "reason":"failed"}},
            {"attempt_id":"a2", "route_node":"test", "harness":"codex", "liveness":"working", "attempt_decision":{"outcome":"succeeded", "action":"continue"}}], gates=[{"route_node":"test", "state":"passed"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].attempt_id, "a2")
        self.assertEqual(rows[0].outcome, "succeeded")
        self.assertEqual(rows[0].gate, "passed")

    def test_unknown_liveness_does_not_change_success(self):
        row, = build_ledger(attempts=[{"attempt_id":"x", "liveness":"unknown", "attempt_decision":{"outcome":"succeeded"}}])
        self.assertEqual(row.liveness, "unknown")
        self.assertEqual(row.outcome, "succeeded")
