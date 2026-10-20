import unittest
from tools.fleet.explorer.ledger import build_ledger

class DecisionProjectionTests(unittest.TestCase):
    def test_decision_uses_structured_fields_not_note_parsing(self):
        row, = build_ledger(attempts=[{"attempt_id":"a", "note":"failed but actually succeeded", "attempt_decision":{"outcome":"succeeded", "action":"continue", "responsible":"owner", "reason":"verified"}}])
        self.assertEqual(row.outcome, "succeeded")
        self.assertIn("continue", row.note)
        self.assertIn("verified", row.note)
