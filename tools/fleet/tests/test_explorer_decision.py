import unittest

from tools.fleet.collectors.dispatch import NO_PROCESS_PROOF_REASON, attempt_decision_payload
from tools.fleet.explorer.ledger import build_ledger_rows
from tools.fleet.tests.explorer_fixture import load_source
from utilities.dispatch_attempt_policy import decide_attempt

SID = "b68ba0fa-8c95-4464-9774-998729d8765c"
FIELDS = ("outcome", "action", "responsible", "retry_kind")


def evidence(process_state=None, terminal=False, result_state=None, reason="live-proof"):
    inputs = {}
    if process_state is not None:
        inputs["observed_liveness"] = {"process_state": process_state, "process_reason": reason}
    if terminal:
        inputs["terminal_observation"] = {"kind": "turn.completed"}
    return {"inputs": inputs, "result_state": result_state}


class SharedInputMapping(unittest.TestCase):
    """attempt_decision_payload is the one mapping both the collector and the explorer use."""

    OPEN = {"worker_type": "stage", "dispatch_depth": "2", "attempt_id": "att-x"}
    DONE = dict(OPEN, note="completed-marker", failure_class="pass")

    def expect(self, status, metadata, *, process_state, reason, terminal=False, result_state=None):
        decision = decide_attempt(status, metadata, process_state=process_state, process_reason=reason,
                                  terminal_observed=terminal, result_state=result_state)
        return {"outcome": decision.outcome, "action": decision.action, "responsible": decision.responsible,
                "reason": decision.reason, "retry_kind": decision.retry_kind, "retry_allowed": decision.retry_allowed}

    def test_table_observed_liveness_by_terminal_observation(self):
        for status, metadata in (("open", self.OPEN), ("done", self.DONE)):
            for observed in ("live", None):
                for terminal in (True, False):
                    with self.subTest(status=status, observed=observed, terminal=terminal):
                        got = attempt_decision_payload(status, metadata, evidence(observed, terminal))
                        want = self.expect(
                            status, metadata, process_state=observed or "unknown",
                            reason="live-proof" if observed else NO_PROCESS_PROOF_REASON, terminal=terminal)
                        self.assertEqual(got, want)

    def test_no_process_proof_is_unknown_never_assumed_alive_or_dead(self):
        none_state = attempt_decision_payload("open", self.OPEN, None)
        empty_state = attempt_decision_payload("open", self.OPEN, {"inputs": {}})
        self.assertEqual(none_state, empty_state)
        self.assertEqual(none_state["reason"], NO_PROCESS_PROOF_REASON)
        self.assertEqual((none_state["outcome"], none_state["action"]), ("pending", "recover"))
        live = attempt_decision_payload("open", self.OPEN, evidence("live"))
        self.assertEqual((live["outcome"], live["action"]), ("pending", "wait"))

    def test_terminal_observation_and_result_state_are_passed_through(self):
        with_result = attempt_decision_payload("done", self.DONE, evidence(None, True, result_state="succeeded"))
        self.assertEqual(with_result, self.expect("done", self.DONE, process_state="unknown",
                                                  reason=NO_PROCESS_PROOF_REASON, terminal=True,
                                                  result_state="succeeded"))

    def test_incomplete_metadata_gives_none_not_a_guess(self):
        self.assertIsNone(attempt_decision_payload("done", None, None))
        self.assertIsNone(attempt_decision_payload("done", "not-a-dict", None))


class FrozenLiveDecisions(unittest.TestCase):
    """Fleet's mapping reproduces the canonical decisions-live.json (manual=false) rows."""

    @classmethod
    def setUpClass(cls):
        cls.source = load_source()
        cls.live = {job["attempt_id"]: job for job in cls.source["snapshot"]["jobs"]}
        cls.frozen = {row["attempt_id"]: row for row in cls.source["decisions"]}
        cls.rows = [r for r in cls.source["registry"] if r["metadata"].get("parent_sid") == SID]

    def payload(self, registry_row):
        job = self.live.get(registry_row["metadata"]["attempt_id"])
        return attempt_decision_payload(registry_row["status"], registry_row["metadata"],
                                        job["state_evidence"] if job else None)

    def test_every_session_row_matches_the_frozen_outcome_and_action(self):
        self.assertEqual(len(self.rows), 40)
        for registry_row in self.rows:
            attempt_id = registry_row["metadata"]["attempt_id"]
            with self.subTest(line=registry_row["line"]):
                want = self.frozen[attempt_id]["decision"]
                got = self.payload(registry_row)
                self.assertEqual({k: got[k] for k in FIELDS}, {k: want[k] for k in FIELDS})

    def test_rows_with_exact_process_proof_are_the_five_live_ones(self):
        proofs = [r for r in self.rows if r["metadata"]["attempt_id"] in self.live]
        self.assertEqual(len(proofs), 5)
        for registry_row in proofs:
            got = self.payload(registry_row)
            self.assertEqual((got["outcome"], got["action"], got["reason"]), ("pending", "wait", self.frozen[registry_row["metadata"]["attempt_id"]]["decision"]["reason"]))

    def test_named_rows(self):
        by_line = {r["line"]: self.payload(r) for r in self.rows}
        self.assertEqual(by_line[5653]["outcome"], "review-blocked")        # impl-review, blocking note
        self.assertEqual(by_line[5659]["outcome"], "failed")                # test, dead-worker-blocked
        self.assertEqual(by_line[5670]["outcome"], "succeeded")             # test, completed-marker
        self.assertEqual(by_line[5614]["outcome"], "cancelled")             # replaced owner, cancelled by parent
        self.assertEqual(by_line[5697]["outcome"], "failed")                # rt-caad execute

    def test_ledger_rows_carry_the_decision_outcome_not_the_note(self):
        from tools.fleet.explorer.types import AttemptRow
        from tools.fleet.explorer.requests import build_tabs
        row = AttemptRow("a", 1, "done", "s", "codex", worker_type="stage", depth=2, route_id="r1", route_node="plan",
                         note="failed but actually succeeded", decision={"outcome": "succeeded", "action": "continue"})
        owner = AttemptRow("o", 0, "open", "s", "claude", worker_type="owner", depth=1, owner_route_id="r1")
        tabs = build_tabs([owner, row], {"r1": {"route_id": "r1", "parent_cycle_id": "c"}},
                          {"c": {"cycle_id": "c", "route_id": "f", "title": "t"}})
        plan, = build_ledger_rows(tabs[0], [owner, row], {"r1": {"nodes": [{"id": "plan"}]}})
        self.assertEqual(plan.outcome, "succeeded")


if __name__ == "__main__":
    unittest.main()
