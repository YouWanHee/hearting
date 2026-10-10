#!/usr/bin/env python3
"""S3a — the one place a deferred completion row is judged as success or not."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch_attempt_policy as POLICY  # noqa: E402


def _deferred_pending_metadata() -> dict[str, str]:
    return {
        "note": "completion-deferred",
        "failure_class": "infrastructure",
        "classifier_source": POLICY.DEFERRED_COMPLETION_SOURCE,
        "reconcile_reason": "completion-transient:TimeoutExpired",
    }


def _deferred_completed_metadata() -> dict[str, str]:
    metadata = _deferred_pending_metadata()
    metadata.update({
        "note": "completed-marker",
        "completion_marker": "/artifacts/.runtime/completions/execute.json",
        "completion_marker_history": "/artifacts/.runtime/completions/execute.1.json",
    })
    return metadata


class DeferredCompletionStatesTest(unittest.TestCase):
    def test_non_deferred_row_is_untouched(self):
        ordinary_pass = {"note": "completed-supervisor", "failure_class": "pass"}
        ordinary_fail = {"note": "dead-worker-fail", "failure_class": "runtime"}
        self.assertEqual(POLICY.deferred_completion(ordinary_pass), "")
        self.assertTrue(POLICY.verdict_pass(ordinary_pass))
        self.assertTrue(POLICY.success_note(ordinary_pass))
        self.assertEqual(POLICY.deferred_completion(ordinary_fail), "")
        self.assertFalse(POLICY.verdict_pass(ordinary_fail))
        self.assertFalse(POLICY.success_note(ordinary_fail))

    def test_deferred_pending_row_is_not_success(self):
        pending = _deferred_pending_metadata()
        self.assertEqual(POLICY.deferred_completion(pending), "pending")
        self.assertFalse(POLICY.verdict_pass(pending))
        self.assertFalse(POLICY.success_note(pending))
        self.assertNotIn("completion-deferred", POLICY.SUCCESS_NOTES)

    def test_deferred_row_completed_by_marker_is_success(self):
        completed = _deferred_completed_metadata()
        self.assertEqual(POLICY.deferred_completion(completed), "completed")
        self.assertTrue(POLICY.verdict_pass(completed))
        self.assertTrue(POLICY.success_note(completed))

    def test_committed_outcome_deferred_states(self):
        self.assertEqual(
            POLICY.committed_outcome("done", _deferred_pending_metadata()),
            "completion-deferred",
        )
        self.assertEqual(
            POLICY.committed_outcome("done", _deferred_completed_metadata()),
            "succeeded",
        )
        self.assertEqual(
            POLICY.committed_outcome("done", {"note": "completed-marker", "failure_class": "pass"}),
            "succeeded",
        )


class PolicyCompletionDeferredIsNotFallbackTest(unittest.TestCase):
    def test_pending_deferred_row_asks_for_completion_not_retry(self):
        decision = POLICY.decide_attempt(
            "done", _deferred_pending_metadata(), process_state="quiescent",
        )
        self.assertEqual(decision.outcome, "completion-deferred")
        self.assertEqual(decision.action, "complete")
        self.assertEqual(decision.responsible, "completion-controller")
        self.assertFalse(decision.retry_allowed)
        self.assertEqual(POLICY.required_action("done", _deferred_pending_metadata()), "complete-deferred")

    def test_marker_bound_deferred_row_advances_as_succeeded(self):
        decision = POLICY.decide_attempt(
            "done", _deferred_completed_metadata(), process_state="quiescent",
        )
        self.assertEqual(decision.outcome, "succeeded")
        self.assertEqual(decision.action, "advance")
        self.assertEqual(
            POLICY.required_action("done", _deferred_completed_metadata()), "advance-completed",
        )



class ExactDeathAndInterruptionTest(unittest.TestCase):
    """5th Codex run outcomes, read by the one committed-outcome policy."""

    def test_an_extinct_namespace_row_reconciles_open_and_falls_back_closed(self):
        self.assertEqual(POLICY.decide_attempt(
            "open", {}, process_state="quiescent", process_reason="namespace-extinct",
        ).action, "reconcile")
        closed = {"note": "dead-namespace-absent", "failure_class": "runtime"}
        decision = POLICY.decide_attempt("done", closed, process_state="quiescent",
                                         process_reason="namespace-extinct")
        self.assertEqual((decision.outcome, decision.action, decision.retry_kind),
                         ("failed", "inspect-failure", "fallback"))

    def test_an_interrupted_foreground_worker_is_an_ordinary_failure(self):
        # B2: the wrapper closes `dead-interrupted` with failure_class=runtime.
        closed = {"note": "dead-interrupted", "failure_class": "runtime",
                  "reconcile_reason": "interrupted"}
        self.assertEqual(POLICY.committed_outcome("done", closed), "failed")
        decision = POLICY.decide_attempt("done", closed, process_state="quiescent")
        self.assertEqual((decision.action, decision.reason, decision.retry_kind),
                         ("inspect-failure", "dead-interrupted", "fallback"))


class CompletionReadinessProjectionTest(unittest.TestCase):
    def pane(self, **changes):
        values = dict(server="server-a", pane="pane-7", harness="claude",
                      session_id="sid-a", pid_birth="pid:44@start:99",
                      identity_verified=True, native_turn="idle")
        values.update(changes)
        return POLICY.PaneObservation(**values)

    def test_ordinary_native_pane_needs_no_attempt_or_work_result(self):
        busy = POLICY.completion_readiness(self.pane(native_turn="busy"))
        self.assertEqual((busy.state, busy.scope, busy.outcome),
                         ("pending", "native-turn", None))
        ready = POLICY.completion_readiness(self.pane())
        self.assertEqual((ready.state, ready.scope, ready.outcome),
                         ("ready", "native-turn", None))
        self.assertEqual(ready.next_owed, "native-turn")
        self.assertIn(("session_id", "sid-a"), ready.identity)

    def test_finalizing_form_and_unknown_turn_are_not_ready(self):
        self.assertEqual(POLICY.completion_readiness(
            self.pane(native_turn="finalizing")).state, "pending")
        self.assertEqual(POLICY.completion_readiness(
            self.pane(native_turn="blocked", form_state="open")).state, "pending")
        unknown = POLICY.completion_readiness(self.pane(native_turn="unknown"))
        self.assertEqual((unknown.state, unknown.outcome), ("unknown", None))

    def test_harness_working_label_projects_to_busy_pending(self):
        readiness = POLICY.completion_readiness(self.pane(native_turn="working"))
        self.assertEqual((readiness.state, readiness.reason), ("pending", "native-turn-busy"))

    def test_unverified_pane_identity_stays_unknown(self):
        result = POLICY.completion_readiness(self.pane(identity_verified=False))
        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.reason, "pane-identity-unverified")

    def test_pane_before_its_first_session_is_judged_by_its_native_turn(self):
        fresh = POLICY.completion_readiness(self.pane(harness="codex", session_id=""))
        self.assertEqual((fresh.state, fresh.reason), ("ready", "native-turn-ready"))
        busy = POLICY.completion_readiness(self.pane(session_id="", native_turn="working"))
        self.assertEqual((busy.state, busy.reason), ("pending", "native-turn-busy"))
        for changes in ({"identity_verified": False}, {"pid_birth": ""}, {"pane": ""}):
            with self.subTest(changes=changes):
                result = POLICY.completion_readiness(self.pane(session_id="", **changes))
                self.assertEqual((result.state, result.reason), ("unknown", "pane-identity-unverified"))
        bound = POLICY.RegisteredWorkObservation(
            "att-open",
            POLICY.AttemptDecision("pending", "wait", "execution-boundary", "process-alive"),
            "live", "unsettled",
        )
        result = POLICY.completion_readiness(self.pane(session_id="", bound_work=(bound,)))
        self.assertEqual((result.state, result.reason), ("unknown", "pane-identity-unverified"))

    def test_registered_work_overrides_optimistic_idle(self):
        unfinished = POLICY.RegisteredWorkObservation(
            "att-open",
            POLICY.AttemptDecision("pending", "wait", "execution-boundary", "process-alive"),
            "live", "unsettled",
        )
        result = POLICY.completion_readiness(self.pane(bound_work=(unfinished,)))
        self.assertEqual((result.state, result.outcome),
                         ("pending", None))
        self.assertEqual(result.reason, "bound-registered-work-pending")
        self.assertIn(("attempt_id", "att-open"), result.provenance)

    def test_unobservable_registered_work_overrides_idle_as_unknown(self):
        unresolved = POLICY.RegisteredWorkObservation(
            "att-unknown",
            POLICY.AttemptDecision("unknown", "recover", "supervision-controller",
                                   "process-unverifiable"),
            "unverifiable", "missing", owner_completion_state="unknown",
        )
        result = POLICY.completion_readiness(self.pane(bound_work=(unresolved,)))
        self.assertEqual((result.state, result.reason),
                         ("unknown", "bound-registered-work-unknown"))

    def test_settled_pass_and_fail_preserve_outcome_provenance(self):
        for outcome, action, expected in (
            ("succeeded", "advance", "succeeded"),
            ("failed", "inspect-failure", "failed"),
        ):
            work = POLICY.RegisteredWorkObservation(
                "att-settled",
                POLICY.AttemptDecision(outcome, action, "completion-controller", "registry-closed"),
                "quiescent", "terminal-settled",
            )
            result = POLICY.completion_readiness(self.pane(bound_work=(work,)))
            self.assertEqual((result.state, result.outcome), ("ready", expected))

    def test_registered_cleanup_pending_does_not_erase_settled_pass(self):
        work = POLICY.RegisteredWorkObservation(
            "att-pass",
            POLICY.AttemptDecision("succeeded", "advance", "completion-controller",
                                   "registry-closed"),
            "quiescent", "terminal-settled", cleanup_state="pending",
        )
        result = POLICY.completion_readiness(work)
        self.assertEqual((result.state, result.outcome, result.reason),
                         ("pending", "succeeded", "registered-follow-through-pending"))

    def test_unavailable_binding_scan_is_not_an_empty_binding(self):
        result = POLICY.completion_readiness(self.pane(bindings_state="unknown"))
        self.assertEqual((result.state, result.reason),
                         ("unknown", "registered-bindings-unavailable"))

    def test_input_ignores_parent_work_but_completion_and_executor_keep_it(self):
        from dataclasses import replace
        for harness in ("claude", "codex", "opencode"):
            for state, action in (("pending", "wait"), ("unknown", "recover")):
                with self.subTest(harness=harness, state=state):
                    work = POLICY.RegisteredWorkObservation(
                        "att-child", POLICY.AttemptDecision(state, action, "owner", "fixture"),
                        "live" if state == "pending" else "unverifiable", "fixture",
                        pane_relation="parent")
                    pane = self.pane(harness=harness, bound_work=(work,))
                    self.assertEqual(POLICY.completion_readiness(pane).state, state)
                    self.assertEqual(POLICY.completion_readiness(pane, purpose="input").state, "ready")
                    executor = replace(pane, bound_work=(replace(work, pane_relation="executor"),))
                    self.assertEqual(POLICY.completion_readiness(executor, purpose="input").state, state)
                    busy = replace(pane, native_turn="working")
                    self.assertEqual(POLICY.completion_readiness(busy, purpose="input").reason,
                                     "native-turn-busy")


if __name__ == "__main__":
    unittest.main()
