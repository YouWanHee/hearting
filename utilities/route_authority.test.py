#!/usr/bin/env python3
"""Decision table for route_authority: the six cases of 2026-10-06, as they are decided today.

Stage 1 moved each judgment into `route_authority` without changing it. These
rows pin the current answers; a later stage that changes a meaning changes the
matching row here on purpose (ROUTE-AUTHORITY-DESIGN-REVIEW-1006.md §2).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import route_authority as RA  # noqa: E402
import dispatch_contract as DC  # noqa: E402
import dispatch_seat_handover as HANDOVER  # noqa: E402
from execution_access import ExecutionAccessRequest  # noqa: E402
from route_identity import route_hash  # noqa: E402


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fail_row(attempt_id, **extra):
    """A done stage worker whose envelope said FAIL, as the registry records it."""
    return {"_status": "done", "attempt_id": attempt_id, "note": "dead-worker-fail",
            "failure_class": "fail", "worker_type": "stage", **extra}


class Case1ParentSessionTest(unittest.TestCase):
    """Case 1: a Codex parent handed its seat to a Claude successor (beside -> retire)."""

    OWNER = {"dispatch_depth": "1", "worker_type": "owner", "attempt_id": "att-owner",
             "parent_sid": "codex-sid", "parent_harness": "codex",
             "owner_route_id": "rt-96bab699717dba8f", "owner_route_hash": "sha256:96ba"}

    def _owns_after(self, successor_sid, successor_harness):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            handover = {"from": "codex-sid", "sid": successor_sid, "harness": successor_harness,
                        "bindings": [HANDOVER.binding_of(jobs, self.OWNER)], "ts": 1}
            with mock.patch.object(HANDOVER, "_seat_for_binding", return_value=object()), \
                 mock.patch.object(HANDOVER, "handover_rows", return_value=[handover]):
                return RA.owns(self.OWNER, successor_sid, jobs)

    def test_the_registered_session_owns_its_route(self):
        self.assertTrue(RA.owns(self.OWNER, "codex-sid", None))
        self.assertFalse(RA.owns(self.OWNER, "", None))

    def test_a_same_harness_successor_inherits_but_another_harness_does_not(self):
        self.assertTrue(self._owns_after("codex-sid-after-clear", "codex"))
        # Today: the handover row is ignored when the harness differs (RA-2 changes this).
        self.assertFalse(self._owns_after("claude-sid", "claude"))

    def test_a_non_owner_cannot_start_the_replacement_owner(self):
        with self.assertRaises(DC.DispatchContractError) as refused:
            RA.require_replacement_parent(None, {}, self.OWNER, current_session=lambda: "claude-sid")
        self.assertEqual(refused.exception.reason, "replacement-parent-identity-unproven")
        RA.require_replacement_parent(None, {}, self.OWNER, current_session=lambda: "codex-sid")

    def test_replacement_lineage_compares_the_registered_parent_exactly(self):
        self.assertTrue(RA.lineage_parent_matches(self.OWNER, thread_id="codex-sid", parent_attempt_id=None))
        self.assertFalse(RA.lineage_parent_matches(self.OWNER, thread_id="claude-sid", parent_attempt_id=None))
        stage = {"dispatch_depth": "2", "parent_attempt_id": "att-owner"}
        self.assertTrue(RA.lineage_parent_matches(stage, thread_id="x", parent_attempt_id="att-owner"))
        self.assertFalse(RA.lineage_parent_matches(stage, thread_id="x", parent_attempt_id="att-other"))

    def test_the_runtime_parent_is_the_calling_session(self):
        env = {"CODEX_THREAD_ID": "codex-sid", "CODEX_DISPATCH_PARENT_CURRENT_FORCE": "1"}
        for honor_force, expected in ((True, "codex-sid"), (False, "synthetic")):
            args = SimpleNamespace(dispatch_depth=2, parent_session_id="synthetic",
                                   parent_slug="owner", parent_harness="claude")
            RA.bind_runtime_parent(args, honor_force=honor_force, environ=env)
            self.assertEqual(args.parent_session_id, expected)
        args = SimpleNamespace(dispatch_depth=1, parent_session_id="synthetic",
                               parent_slug="owner", parent_harness="claude")
        RA.bind_runtime_parent(args, environ={"CLAUDE_CODE_SESSION_ID": "claude-sid"})
        self.assertEqual((args.parent_session_id, args.parent_harness, args.parent_slug),
                         ("claude-sid", "claude", None))
        self.assertEqual(RA.caller_identity({"CLAUDE_CODE_SESSION_ID": "claude-sid"}), ("claude", "claude-sid"))
        self.assertEqual(RA.correction_source_session({"CLAUDE_CODE_SESSION_ID": "claude-sid"}), "operator")


class Case2SealedPinTest(unittest.TestCase):
    """Case 2: `--pin owner=codex` stays on the route; a later owner cannot switch harness."""

    ROUTE = {"selection_pins": {"owner": {"harness": "codex"}, "worker": {"harness": "codex"}}}

    def test_the_sealed_pin_beats_the_request_while_it_is_available(self):
        self.assertEqual(RA.pinned_launch_harness(self.ROUTE, worker_type="owner", requested="claude",
                                                  available=lambda harness: True), ("codex", "claude"))
        self.assertEqual(RA.pinned_launch_harness(self.ROUTE, worker_type="owner", requested="claude",
                                                  available=lambda harness: False), ("claude", None))
        self.assertEqual(RA.sealed_pin_harness(self.ROUTE, worker_type="stage"), "codex")
        self.assertEqual(RA.PIN_TARGETS, ("owner", "frame", "worker"))

    def test_the_pin_is_part_of_the_route_identity_and_the_replacement_harness_is_fixed(self):
        repinned = {"selection_pins": {"owner": {"harness": "claude"}, "worker": {"harness": "codex"}}}
        self.assertNotEqual(route_hash(self.ROUTE), route_hash(repinned))
        self.assertIn("harness", RA.REPLACEMENT_FIXED_KEYS)


class Case3TestRoundTest(unittest.TestCase):
    """Case 3: two test FAILs, one of them only incomplete, spent the standard cap."""

    ROUTE = {"effective_intensity": "standard"}
    NODE = {"id": "test", "kind": "pipeline-stage"}

    def test_every_fail_spends_a_round_whatever_its_reason(self):
        rows = [("done", _fail_row("att-r1")), ("done", _fail_row("att-r2"))]
        budget = RA.round_budget(self.ROUTE, self.NODE, rows)
        self.assertEqual((budget.state, budget.verdict_rounds, budget.cap), ("exhausted", 2, 2))
        self.assertTrue(RA.last_verdict_blocking(rows, "test"))

    def test_a_declared_phase_is_outside_the_full_stage_rounds(self):
        phase = SimpleNamespace(subsession_id="ss-gap-1", subsession_index=1, subsession_count=5,
                                subsession_mode="serial", session_chain_id="ssc-gap-1",
                                phase_brief="brief.md", narrow_verify="true", expected_round_trips=12,
                                stage_authority=0, dispatch_depth=2, route_id="rt-96bab699717dba8f",
                                route_node="test")
        self.assertTrue(RA.declared_subsession(phase))
        self.assertTrue(RA.no_stage_authority({"subsession_id": "ss-gap-1", "stage_authority": "0"}))
        self.assertFalse(RA.no_stage_authority({"note": "dead-worker-fail"}))
        with self.assertRaises(DC.DispatchContractError) as refused:
            RA.declared_subsession(SimpleNamespace(subsession_id="ss-gap-1", stage_authority=0))
        self.assertEqual(refused.exception.reason, "subsession-arguments-incomplete")

    def test_the_three_subsession_marks_stay_distinct(self):
        only_id = {"subsession_id": "ss-1"}
        only_authority = {"stage_authority": "false"}
        both = {"subsession_id": "ss-1", "stage_authority": "0"}
        self.assertEqual([RA.no_stage_authority(m) for m in (only_id, only_authority, both)], [False, True, True])
        self.assertEqual([RA.subsession_row(m) for m in (only_id, only_authority, both)], [True, True, True])
        self.assertEqual([RA.linked_worktree_slice(m) for m in (only_id, only_authority, both)], [False, False, True])
        self.assertEqual([RA.subsession_launch(i, a) for i, a in ((None, 1), ("ss-1", 1), (None, 0))],
                         [False, True, True])


class Case4RetryLinkTest(unittest.TestCase):
    """Case 4: a readable FAIL that inherited a transport retry link exhausted the replacement."""

    CAPPED = {"id": "test", "kind": "pipeline-stage"}

    def test_an_admitted_verdict_round_is_not_a_retry_of_the_last_fail(self):
        admit = SimpleNamespace(budget=SimpleNamespace(state="admit"))
        latest = _fail_row("att-0c43", automatic_retry_of="att-4cf2")
        self.assertEqual(RA.retry_predecessor([latest], self.CAPPED, admit), "")
        self.assertEqual(RA.retry_predecessor([latest], self.CAPPED, None), "att-0c43")

    def test_a_second_link_on_the_same_source_is_exhausted(self):
        source = ("2026-10-06T08:00:00Z\tdone\t/repo\t/wt\ttest-r2\t"
                  "attempt_id=att-0c43,automatic_retry_of=att-4cf2,route_id=rt-f22b0e181b82b4a3,route_node=test")
        with self.assertRaises(DC.DispatchContractError) as refused:
            DC._automatic_retry_admission([source], {"attempt_id": "att-next", "automatic_retry_of": "att-0c43"})
        self.assertEqual(refused.exception.reason, "automatic-replacement-exhausted")


class Case5ReadRootsTest(unittest.TestCase):
    """Case 5: read roots are not projected into the grant (Claude and Codex rows)."""

    def test_read_roots_are_dropped_and_reported_unmet(self):
        request = ExecutionAccessRequest(
            writable_roots=(), read_roots=(Path("/nas/records"),), network_required=False,
            network_reason="", network_hosts=(), enforcement_required="any", justification=(),
            request_sha256="0" * 64, source_path=Path("/tmp/request.json"))
        for runtime in ("claude-cli", "codex-exec"):
            with self.subTest(runtime=runtime):
                grant = RA.access_grant(request, runtime=runtime)
                self.assertEqual(grant.read_roots, ())
                self.assertIn("read-roots-unprojected", grant.unmet)


class Case6UncappedAndEnvelopeTest(unittest.TestCase):
    """Case 6: an uncapped execute FAIL keeps its retry link; envelopes are matched exactly."""

    def test_an_uncapped_fail_is_linked_as_the_retry_predecessor(self):
        # stage-dispatch-fallback admits a round only for a capped node, so an
        # `execute` FAIL reaches this judgment with no round admission at all.
        execute = {"id": "execute", "kind": "pipeline-stage"}
        self.assertFalse(RA.is_round_capped_node(execute))
        self.assertEqual(RA.retry_predecessor([_fail_row("att-2358")], execute, None), "att-2358")

    def test_a_pass_with_an_explained_none_blocker_breaks_the_contract(self):
        text = "artifact: /cycle/test_logs/envelope.md\nverdict: PASS\nblocker: none (범위 한정 PASS)"
        handoff = RA.HANDOFF_RE.search(text).groupdict()
        self.assertEqual(handoff["blocker"], "none (범위 한정 PASS)")
        self.assertEqual(RA.pass_blocker_violation(handoff["verdict"], handoff["blocker"]), "pass-blocker-not-none")
        self.assertIsNone(RA.pass_blocker_violation("PASS", "none"))
        self.assertIsNone(RA.pass_blocker_violation("FAIL", "G1 incomplete"))
        self.assertIsNone(RA.HANDOFF_RE.search("verdict: PASS\nblocker: none"))


class OneHomeTest(unittest.TestCase):
    """The old names are this module's judgments, not copies."""

    def test_old_names_resolve_to_route_authority(self):
        import dispatch_parent_completion as P
        import model_profile as MP
        import stage_session_runtime as SSR
        import codex_dispatch_terminal as CDT
        import dispatch_supervisor_terminal as DST
        import route_plan as RP
        F = _load("route_authority_fallback", "utilities/stage-dispatch-fallback.py")
        CR = _load("route_authority_capability_route", "utilities/capability-route.py")
        pairs = [
            (P.interactive_parent_identity, RA.caller_identity),
            (P.default_parent_session_id, RA.default_parent_session_id),
            (P.default_parent_harness, RA.default_parent_harness),
            (DC.is_linked_worktree_slice, RA.linked_worktree_slice),
            (MP.pin_target, RA.pin_target),
            (MP.sealed_pin_harness, RA.sealed_pin_harness),
            (MP.pinned_launch_harness, RA.pinned_launch_harness),
            (SSR.declared, RA.declared_subsession),
            (F.retry_predecessor, RA.retry_predecessor),
            (CDT._HANDOFF_RE, RA.HANDOFF_RE),
            (DST._HANDOFF_RE, RA.HANDOFF_RE),
            (RP._PIN_TARGETS, RA.PIN_TARGETS),
            (CR.SELECTION_PIN_TARGETS, RA.PIN_TARGETS),
            (CR._LIVE_ROW_STATUSES, RA.LIVE_ROW_STATUSES),
        ]
        for old, new in pairs:
            self.assertIs(old, new)


if __name__ == "__main__":
    unittest.main()
