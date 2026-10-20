import tempfile
import unittest

from tools.fleet.explorer.ledger import build_request_ledger, format_row, ledger_lines, outcome_text
from tools.fleet.explorer.requests import build_tabs
from tools.fleet.explorer.types import AttemptRow, LedgerRow
from tools.fleet.tests.explorer_fixture import collect, materialize, screen_main, tab


def attempt(attempt_id, line, node, outcome, *, liveness="unknown", note="", harness="codex", route="r1", decision=True):
    return AttemptRow(attempt_id, line, "done", "slug", harness, worker_type="stage", depth=2, route_id=route,
                      route_node=node, note=note, liveness=liveness,
                      decision={"outcome": outcome} if decision else None)


def one_tab(rows, route_nodes=("plan", "execute")):
    owner = AttemptRow("att-owner", 0, "open", "slug-owner", "claude", worker_type="owner", depth=1,
                       owner_route_id="r1", liveness="unknown", decision={"outcome": "pending"})
    allrows = [owner] + list(rows)
    routes = {"r1": {"route_id": "r1", "parent_cycle_id": "c1", "nodes": [{"id": n} for n in route_nodes]}}
    cycles = {"c1": {"cycle_id": "c1", "route_id": "f1", "title": "req"}}
    tabs = build_tabs(allrows, routes, cycles)
    return tabs, allrows, routes


class CanonicalB6Ledger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.b6 = tab(collect(cls.fx), "b6-report")
        cls.result = collect(cls.fx, request_id=cls.b6.identity, stage_id="test")
        cls.view = cls.result.request_ledger

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_rows_carry_the_canonical_values(self):
        self.assertEqual([(r.route_node, r.harness, r.liveness, r.outcome, r.gate, r.attempts) for r in self.result.ledger],
                         [("plan", "CX", "unknown", "succeeded", "passed", 1),
                          ("execute", "CX", "unknown", "succeeded", "hold", 1),
                          ("impl-review", "CX", "unknown", "succeeded", "passed", 2),
                          ("test", "CX", "unknown", "succeeded", "passed", 3),
                          ("report", "CX", "unknown", "succeeded", "passed", 1)])

    def test_test_history_is_the_registry_note_of_each_attempt(self):
        test = next(r for r in self.result.ledger if r.route_node == "test")
        self.assertEqual([(n, note) for n, _attempt, note in test.history],
                         [(1, "dead-worker-blocked"), (2, "dead-worker-fail"), (3, "completed-marker")])
        review = next(r for r in self.result.ledger if r.route_node == "impl-review")
        self.assertEqual([note for _n, _a, note in review.history], ["completed-review-blocking", "completed-marker"])

    def test_text_equals_the_canonical_100_column_screen(self):
        lines = list(ledger_lines(self.view, "test"))
        self.assertEqual(len(lines), 17)
        self.assertEqual(lines, screen_main("first", 100, 4, 17))

    def test_summary_names_the_held_gate_and_the_mismatch(self):
        self.assertEqual((self.view.gate_passed, self.view.gate_total, self.view.gate_hold), (4, 5, ("execute",)))
        lines = ledger_lines(self.view)
        self.assertIn("⊸ 통과 4/5 · execute — 판정 보류", lines)
        self.assertIn("  근거 불일치", lines)

    def test_the_held_gate_does_not_change_the_result(self):
        execute = next(r for r in self.result.ledger if r.route_node == "execute")
        self.assertEqual((execute.outcome, execute.gate), ("succeeded", "hold"))
        self.assertIn("✓ 성공 —", format_row(execute))

    def test_header_shows_the_chain_and_the_supervisor_result(self):
        self.assertEqual(self.view.start_chain, "frame → 경로 결정 → 감독(owner) CL")
        self.assertEqual(self.view.attempt_chain, "frame att-14b38c4… → 감독 att-00aa672…")
        self.assertEqual((self.view.supervisor_liveness, self.view.supervisor_outcome), ("unknown", "succeeded"))

    def test_other_open_requests_and_responsibility(self):
        self.assertEqual(self.view.others, (("owner-fleet", "idle", "pending"),))
        self.assertEqual(self.view.responsible, "감독(owner)")
        lines = ledger_lines(self.view)
        self.assertIn("같은 세션 다른 요청: owner-fleet ·쉼 …아직", lines)
        self.assertEqual(lines[-1], "담당: 감독(owner) · 실행 중 단계는 상세에서")

    def test_unselected_stage_has_no_history_lines(self):
        plain = ledger_lines(self.view)
        self.assertNotIn("  3 completed-marker", plain)
        self.assertIn("▶test        CX  ?  ✓ 성공 ⊸    3", ledger_lines(self.view, "test"))

    def test_open_request_shows_each_step_result_without_reclassifying(self):
        herdr = collect(self.fx, request_id=tab(collect(self.fx), "herdr-tmux").identity)
        rows = {r.route_node: r for r in herdr.ledger}
        self.assertEqual((rows["plan"].outcome, rows["plan"].gate), ("failed", "passed"))
        self.assertEqual((rows["execute"].outcome, rows["execute"].gate), ("failed", None))
        self.assertEqual(rows["report"].attempts, 0)
        self.assertIn(" report             … 아직      0", ledger_lines(herdr.request_ledger))
        self.assertIn("⊸ 통과 1/5", ledger_lines(herdr.request_ledger))


class ResultGateAndLivenessAreIndependent(unittest.TestCase):
    def test_succeeded_with_no_gate_verdict_stays_succeeded_and_blank(self):
        tabs, rows, routes = one_tab([attempt("a1", 1, "plan", "succeeded")])
        row, _execute = build_request_ledger(tabs[0], tabs, rows, routes, {}).rows
        self.assertEqual((row.outcome, row.gate), ("succeeded", None))
        self.assertEqual(format_row(row), " plan        CX  ?  ✓ 성공      1")

    def test_unknown_liveness_does_not_change_the_result_and_vice_versa(self):
        tabs, rows, routes = one_tab([
            attempt("a1", 1, "plan", "succeeded", liveness="unknown"),
            attempt("a2", 2, "execute", "failed", liveness="working")])
        plan, execute = build_request_ledger(tabs[0], tabs, rows, routes, {}).rows
        self.assertEqual((plan.liveness, plan.outcome), ("unknown", "succeeded"))
        self.assertEqual((execute.liveness, execute.outcome), ("working", "failed"))
        self.assertIn("⠇  ✕ 실패", format_row(execute))

    def test_outcome_comes_from_the_decision_never_from_the_note(self):
        tabs, rows, routes = one_tab([attempt("a1", 1, "plan", "succeeded", note="dead-worker-fail"),
                                      attempt("a2", 2, "execute", "failed", note="completed-marker")])
        plan, execute = build_request_ledger(tabs[0], tabs, rows, routes, {}).rows
        self.assertEqual((plan.outcome, execute.outcome), ("succeeded", "failed"))
        self.assertEqual(plan.history[0][2], "dead-worker-fail")      # the note is only shown as history

    def test_a_missing_decision_is_unknown_not_success_or_failure(self):
        tabs, rows, routes = one_tab([attempt("a1", 1, "plan", None, decision=False)])
        plan, _ = build_request_ledger(tabs[0], tabs, rows, routes, {}).rows
        self.assertIsNone(plan.outcome)
        self.assertEqual(outcome_text(None), "? 미확인")
        self.assertIn("? 미확인", format_row(plan))
        self.assertNotIn("✓", format_row(plan))
        self.assertNotIn("✕", format_row(plan))

    def test_a_step_never_attempted_is_pending_with_zero_attempts(self):
        tabs, rows, routes = one_tab([attempt("a1", 1, "plan", "succeeded")])
        _plan, execute = build_request_ledger(tabs[0], tabs, rows, routes, {}).rows
        self.assertEqual((execute.attempts, execute.outcome, execute.harness), (0, None, ""))
        self.assertTrue(format_row(execute).startswith(" execute"))
        self.assertIn("… 아직", format_row(execute))

    def test_gate_hold_without_summary_when_everything_passed(self):
        tabs, rows, routes = one_tab([attempt("a1", 1, "plan", "succeeded"), attempt("a2", 2, "execute", "succeeded")])
        gates = {("r1", "plan"): "passed", ("r1", "execute"): "passed"}
        view = build_request_ledger(tabs[0], tabs, rows, routes, gates)
        lines = ledger_lines(view)
        self.assertIn("⊸ 통과 2/2", lines)
        self.assertNotIn("  근거 불일치", lines)

    def test_row_text_pads_by_display_cells(self):
        row = LedgerRow("impl-review", "CX", "unknown", "succeeded", "passed", "a", attempts=2)
        self.assertEqual(format_row(row), " impl-review CX  ?  ✓ 성공 ⊸    2")
        self.assertEqual(format_row(row, selected=True)[0], "▶")


if __name__ == "__main__":
    unittest.main()
