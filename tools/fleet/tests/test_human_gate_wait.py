"""Privacy and liveness composition for ledger-backed interaction observations."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet.model import Session, classify_session  # noqa: E402
from fleet.render import _interaction_badge  # noqa: E402


class HumanGateWaitProjectionTest(unittest.TestCase):
    def test_ledger_wait_coexists_with_native_wait_without_public_route_content(self):
        session = Session(harness="claude", pid=1, session_id="parent")
        session.interaction_state = {"kind": "approval", "source": "claude-transcript", "waiting_since": 10}
        session.interaction_observations = [{
            "kind": "decision", "source": "hearting-ledger", "waiting_since": 5,
        }]
        ev = {"pid_alive": True, "proc_start_match": True,
              "interaction_wait": [session.interaction_state, *session.interaction_observations]}
        state, _ = classify_session(ev, 20)
        self.assertEqual(state, "blocked")
        self.assertEqual(_interaction_badge(session), " approval needed + answer needed")
        public = session.to_dict()
        self.assertEqual(public["interaction_observations"], session.interaction_observations)
        projected_waits = json.dumps(public["interaction_observations"] + [public["interaction_state"]])
        self.assertNotIn("route_id", projected_waits)
        self.assertNotIn("question", projected_waits)
        self.assertNotIn("answers", projected_waits)

    def test_identity_guard_still_precedes_wait_classification(self):
        ev = {"pid_alive": False, "proc_start_match": False,
              "interaction_wait": [{"kind": "decision", "source": "hearting-ledger"}]}
        state, _ = classify_session(ev, 20)
        self.assertEqual(state, "dead")


if __name__ == "__main__":
    unittest.main()
