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
            with mock.patch.object(HANDOVER, "_seats_for_binding", return_value=[object()]), \
                 mock.patch.object(HANDOVER, "handover_rows", return_value=[handover]):
                return RA.owns(self.OWNER, successor_sid, jobs)

    def test_the_registered_session_owns_its_route(self):
        self.assertTrue(RA.owns(self.OWNER, "codex-sid", None))
        self.assertFalse(RA.owns(self.OWNER, "", None))

    def test_a_recorded_successor_inherits_on_any_harness(self):
        # RA-2 (stage 2): a recorded handover row moves the parent role across harnesses.
        self.assertTrue(self._owns_after("codex-sid-after-clear", "codex"))
        self.assertTrue(self._owns_after("claude-sid", "claude"))

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
        # Every harness binds the same way: an OpenCode caller is its own runtime parent too,
        # and an ambiguous caller binds nothing.
        for env, expected in (({"OPENCODE_SESSION_ID": "ses_1"}, ("ses_1", "opencode", None)),
                              ({"CLAUDE_SESSION_ID": "claude-old"}, ("claude-old", "claude", None)),
                              ({"CODEX_THREAD_ID": "x", "CLAUDE_CODE_SESSION_ID": "c"},
                               ("synthetic", "claude", "owner"))):
            args = SimpleNamespace(dispatch_depth=1, parent_session_id="synthetic",
                                   parent_slug="owner", parent_harness="claude")
            RA.bind_runtime_parent(args, environ=env)
            self.assertEqual((args.parent_session_id, args.parent_harness, args.parent_slug), expected)
        self.assertEqual(RA.caller_identity({"CLAUDE_CODE_SESSION_ID": "claude-sid"}), ("claude", "claude-sid"))
        # One reading now: the correction sender is the same session caller_identity names
        # (the old copy did not read CLAUDE_CODE_SESSION_ID and labelled it "operator").
        self.assertEqual(RA.correction_source_session({"CLAUDE_CODE_SESSION_ID": "claude-sid"}), "claude-sid")


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
        self.assertTrue(RA.gate_unmet(rows, "test"))
        # RA-5 (stage 2): an unfinished round ends BLOCKED; it spends no round and leaves
        # the gate unmet, so the phases after it are a gap retry. Two BLOCKED in a row still bind.
        blocked = [("done", {"note": "dead-worker-blocked", "failure_class": "blocked", "worker_type": "stage"})]
        self.assertEqual(RA.round_budget(self.ROUTE, self.NODE, blocked).verdict_rounds, 0)
        self.assertTrue(RA.gate_unmet(blocked, "test"))
        self.assertFalse(RA.gate_unmet(rows + [("done", {"note": "completed-marker", "worker_type": "stage"})], "test"))
        self.assertEqual(RA.round_budget(self.ROUTE, self.NODE, blocked * 2).state, "verdictless-bound")

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
    """Case 4: a readable FAIL that inherited a transport retry link exhausted the replacement.

    RA-4 (stage 2): a readable FAIL or BLOCKED never becomes a retry
    predecessor, whatever its round admission; only a transport failure does.
    """

    def test_a_readable_result_is_never_a_retry_predecessor(self):
        latest = _fail_row("att-0c43", automatic_retry_of="att-4cf2")
        self.assertEqual(RA.retry_predecessor([latest]), "")
        blocked = {**latest, "note": "dead-worker-blocked", "failure_class": "blocked"}
        self.assertEqual(RA.retry_predecessor([blocked]), "")
        death = {**latest, "note": "dead-route-completion-rejected", "failure_class": "contract"}
        self.assertEqual(RA.retry_predecessor([death]), "att-0c43")

    def test_the_attempt_policy_retries_only_transport_failures(self):
        from dispatch_attempt_policy import decide_attempt
        def retry(note, failure_class):
            return decide_attempt("done", {"note": note, "failure_class": failure_class},
                                  process_state="quiescent").retry_kind
        self.assertEqual(retry("dead-worker-fail", "fail"), "")
        self.assertEqual(retry("dead-worker-blocked", "blocked"), "")
        self.assertEqual(retry("dead-exact-pid", "contract"), "fallback")
        self.assertEqual(retry("dead-invalid-envelope", "invalid-envelope"), "fallback")
        self.assertEqual(retry("dead-capacity", "capacity"), "capacity")

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
    """Case 6: an uncapped execute FAIL; envelopes are matched exactly.

    RA-4 (stage 2): the execute FAIL no longer becomes a retry predecessor, so
    the owner's next execute is new work instead of an exhausted replacement.
    """

    def test_an_uncapped_fail_is_not_a_retry_predecessor(self):
        execute = {"id": "execute", "kind": "pipeline-stage"}
        self.assertFalse(RA.is_round_capped_node(execute))
        self.assertEqual(RA.retry_predecessor([_fail_row("att-2358")]), "")

    def test_a_pass_with_an_explained_none_blocker_is_a_pass(self):
        # RA-8 (stage 2): the note is kept as a remark; other blocker text still breaks PASS.
        text = "artifact: /cycle/test_logs/envelope.md\nverdict: PASS\nblocker: none (범위 한정 PASS)"
        handoff = RA.HANDOFF_RE.search(text).groupdict()
        self.assertEqual(handoff["blocker"], "none (범위 한정 PASS)")
        self.assertIsNone(RA.pass_blocker_violation(handoff["verdict"], handoff["blocker"]))
        self.assertEqual(RA.pass_blocker_note(handoff["blocker"]), "범위 한정 PASS")
        self.assertEqual(RA.pass_blocker_note("none"), "")
        self.assertEqual(RA.pass_blocker_violation("PASS", "G1 skipped"), "pass-blocker-not-none")
        self.assertIsNone(RA.pass_blocker_violation("PASS", "none"))
        self.assertIsNone(RA.pass_blocker_violation("FAIL", "G1 incomplete"))
        self.assertIsNone(RA.HANDOFF_RE.search("verdict: PASS\nblocker: none"))


class DeathPatternTest(unittest.TestCase):
    """Audit #9: one death/limit table for every harness, at launch and in liveness."""

    def test_every_reader_uses_the_one_table(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                wrapper = _load(f"route_authority_wrapper_{harness}", f"adapters/{harness}/bin/dispatch-headless.py")
                self.assertIs(wrapper.DEATH_PATTERNS, RA.DEATH_PATTERNS)
                self.assertIs(wrapper.scan_anchored_death, RA.scan_anchored_death)
        self.assertIs(DC.anchored_capacity_failure, RA.anchored_capacity_failure)
        for path in ("adapters/codex/bin/dispatch-liveness.py", "adapters/opencode/bin/dispatch-liveness.py",
                     "utilities/dispatch-liveness.sh"):
            text = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn("scan_anchored_death", text, path)
            self.assertNotIn("LIMIT_RE", text, path)

    def test_lines_that_split_by_harness_now_classify_alike(self):
        # Claude's wrapper missed the first two; OpenCode's liveness missed the third.
        cases = {"usage_limit_reached": "usage-limit",
                 "error: exceeded retry limit, last status: 429 Too Many Requests": "usage-limit",
                 "network is unreachable": "network-operation-not-permitted",
                 "permission requested: bash; auto-rejecting": "permission-reject",
                 "Selected model is at capacity": "capacity"}
        for line, label in cases.items():
            with self.subTest(line=line):
                self.assertEqual(RA.scan_anchored_death(line), (label, ""))
        self.assertEqual(RA.scan_death("You've hit your session limit · resets 3pm"), ("session-limit", "3pm"))
        for prose in ("This report discusses rate limits at length " + "x" * 200,
                      "the model is at capacity per an earlier note, continuing",
                      "req_429abc finished"):
            with self.subTest(prose=prose[:30]):
                self.assertIsNone(RA.scan_anchored_death(prose))


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
