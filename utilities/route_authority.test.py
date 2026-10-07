#!/usr/bin/env python3
"""Decision table for route_authority: the six cases of 2026-10-06, as they are decided today.

Stage 1 moved each judgment into `route_authority` without changing it. These
rows pin the current answers; a later stage that changes a meaning changes the
matching row here on purpose (ROUTE-AUTHORITY-DESIGN-REVIEW-1006.md §2).
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
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
    """Case 2: `--pin owner=codex` stays sealed in the route; only the route's parent moves the
    owner pin later, by a recorded change (RA-3, `OwnerPinChangeTest`), never by a new hash."""

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


def _tuple(parent, child, sandbox="workspace-write", status="supported"):
    return {"parent_harness": parent, "parent_transport": "headless", "parent_sandbox": sandbox,
            "child_harness": child, "launch_authority": "conductor", "status": status,
            "failure_class": "", "probe_source": "fixture", "checked_worktree": "/w",
            "failure_scope": "none", "retry_on_isolated_worktree": 0}


def _chain(tuples):
    same = [row for row in tuples if row["child_harness"] == row["parent_harness"]]
    cross = [row for row in tuples if row["child_harness"] != row["parent_harness"]]
    return [{"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": same},
            {"ordinal": 2, "fallback_hop": "cross-harness-headless", "candidates": cross},
            {"ordinal": 3, "fallback_hop": "native-subagent", "candidates": [], "fleet_visibility": "degraded"},
            {"ordinal": 4, "fallback_hop": "inline", "status": "eligible-after-prior-hop-exhaustion",
             "reason_enum": "runtime-unavailable", "fleet_visibility": "none"}]


class OwnerPinChangeTest(unittest.TestCase):
    """RA-3 (stage 2): the BC route rt-96bab699 shape -- a Codex owner and Codex worker pin
    with only Codex-parent tuples -- moves its next owner to Claude; the sealed route stays."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        tuples = [_tuple("codex", "codex"), _tuple("codex", "claude"), _tuple("codex", "opencode", status="unsupported")]
        self.route = {"route_id": "rt-96bab699717dba8f", "route_hash": "sha256:sealed",
                      "artifact_root": self.temp.name, "cwd": "/w", "effective_intensity": "standard",
                      "dispatch_contract_version": 3,
                      "selection_pins": {"contract_version": 1, "owner": {"harness": "codex", "model": None, "effort": None},
                                         "worker": {"harness": "codex", "model": None, "effort": None}},
                      "dispatch_evidence": {"tuples": tuples, "native_subagent": []},
                      "nodes": [{"id": "test", "dispatch_depth": 2, "fallback_hops": _chain(tuples)}]}
        self.sealed = json.loads(json.dumps(self.route))
        self.probed = [_tuple("claude", "claude", sandbox="bypass"), _tuple("claude", "codex", sandbox="bypass")]

    def change(self, harness="claude", **extra):
        return RA.record_pin_change(self.route, target="owner",
                                    pin={"harness": harness, "model": None, "effort": None},
                                    by={"harness": "codex", "session_id": "codex-sid"}, source="unattributed",
                                    tuples=self.probed if harness == "claude" else [], candidates=[], **extra)

    def test_nothing_recorded_is_the_sealed_route_itself(self):
        self.assertIs(RA.route_in_force(self.route), self.route)
        self.assertEqual(RA.sealed_pin_harness(self.route, worker_type="owner"), "codex")

    def test_the_parent_moves_the_owner_and_the_worker_pin_stays(self):
        row = self.change()
        self.assertEqual((row["previous"]["harness"], row["pin"]["harness"], row["source"]), ("codex", "claude", "unattributed"))
        self.assertEqual(self.route, self.sealed)                 # the sealed dict and its hash are untouched
        view = RA.route_in_force(self.route)
        self.assertEqual(view["route_hash"], "sha256:sealed")
        self.assertEqual(RA.sealed_pin_harness(self.route, worker_type="owner"), "claude")
        self.assertEqual(RA.sealed_pin_harness(self.route, worker_type="stage"), "codex")
        self.assertEqual(RA.pinned_launch_harness(self.route, worker_type="owner", requested="codex",
                                                  available=lambda h: True), ("claude", "codex"))
        hops = view["nodes"][0]["fallback_hops"]
        self.assertIn(("claude", "claude"), {(r["parent_harness"], r["child_harness"]) for r in hops[0]["candidates"]})
        self.assertIn(("claude", "codex"), {(r["parent_harness"], r["child_harness"]) for r in hops[1]["candidates"]})
        self.assertEqual(len(view["dispatch_evidence"]["tuples"]), 5)
        self.assertIsNone(self.change())                         # the pin in force already is claude
        self.assertEqual(len(RA.pin_changes(self.route)), 1)
        self.assertEqual(RA.route_in_force(view), view)          # applying twice changes nothing

    def test_a_claude_owner_of_the_changed_route_launches_its_codex_worker(self):
        self.change()
        route_file = Path(self.temp.name) / "route.json"
        route_file.write_text(json.dumps({**self.route, "schema_version": 2}))
        policy = DC.headless_attempt_policy(
            route_file=str(route_file), route_node="test", intensity="standard", harness="codex",
            dispatch_depth=2, parent_slug="owner", execution_surface="registered-headless",
            registered_worker=True, fallback_hop="cross-harness-headless", fallback_ordinal=2,
            parent_harness="claude", parent_transport="headless", parent_sandbox="bypass",
            launch_authority="conductor")
        self.assertEqual(policy["fallback_hop"], "cross-harness-headless")
        owner = _load("route_authority_dispatch_owner", "utilities/dispatch-owner.py")
        self.assertIn("claude", owner._sealed_owner_context(route_file)["harnesses"])

    def isolated_env(self):
        base = Path(self.temp.name)
        return {"HOME": str(base / "home"), "XDG_STATE_HOME": str(base / "xdg"),
                "HARNESS_STATE_ROOT": str(base / "state"), "AGENT_PEER_LEDGER_ROOT": str(base / "peer"),
                "AGENT_DISPATCH_JOBS": str(base / "state" / "jobs.log"), "HERDR_PANE_ID": "wB:p99",
                "PATH": os.environ.get("PATH", "")}

    def start_pin(self, values, *, caller=("codex", "codex-sid"), owner_parent="codex-sid", probed=None):
        router = _load("route_authority_capability_route", "utilities/capability-route.py")
        import work_start
        rows = {"att-owner": ("done", {"worker_type": "owner", "owner_route_id": self.route["route_id"],
                                       "parent_sid": owner_parent, "attempt_id": "att-owner"})}
        probe = {"tuples": [_tuple("codex", "codex")] + (self.probed if probed is None else probed), "candidates": []}
        with mock.patch.dict(os.environ, self.isolated_env(), clear=True), \
             mock.patch.object(RA, "caller_identity", return_value=caller), \
             mock.patch.object(work_start, "_rows", return_value=rows), \
             mock.patch.object(router, "_compose_readiness", return_value=probe) as readiness:
            result = router._change_pins(self.route, Path(self.temp.name) / "jobs.log", values)
        return result, readiness

    def test_start_pin_probes_the_new_owner_and_records_the_change(self):
        result, readiness = self.start_pin(["owner=claude"])
        self.assertTrue(result["changed"])
        self.assertEqual((result["owner"]["harness"], result["previous"]["harness"], result["source"]),
                         ("claude", "codex", "unattributed"))
        self.assertEqual(readiness.call_args.args[2], "claude")          # probed by the runtime, now
        self.assertEqual(sorted(readiness.call_args.args[3]), ["claude", "codex", "opencode"])
        (row,) = RA.pin_changes(self.route)
        self.assertEqual({(t["parent_harness"], t["child_harness"]) for t in row["tuples"]},
                         {("claude", "claude"), ("claude", "codex")})   # only the new owner's rows
        self.assertEqual(row["by"], {"harness": "codex", "session_id": "codex-sid"})
        again, _ = self.start_pin(["owner=claude"])
        self.assertFalse(again["changed"])

    def test_start_pin_refuses_what_it_cannot_do_and_records_nothing(self):
        for values, kwargs, reason in (
                (["owner=claude"], {"caller": ("claude", "another-sid")}, "start-pin-parent-only"),
                (["worker=opencode"], {"probed": [_tuple("codex", "opencode", status="unsupported")]},
                 "start-pin-worker-unready:opencode"),
                (["owner=claude"], {"probed": [_tuple("claude", "codex", status="unsupported")]}, "start-pin-owner-unready:claude")):
            with self.subTest(reason=reason), self.assertRaises(ValueError) as refused:
                self.start_pin(values, **kwargs)
            self.assertIn(reason, str(refused.exception))
        self.assertEqual(RA.pin_changes(self.route), [])

    def test_a_peer_message_at_this_turn_is_the_recorded_source(self):
        router = _load("route_authority_capability_route_source", "utilities/capability-route.py")
        peer = _load("route_authority_peer_message", "utilities/peer-message.py")
        import argparse, session_tidy, time
        with mock.patch.dict(os.environ, self.isolated_env(), clear=True):
            self.assertEqual(router._turn_peer_source("codex", "codex-sid", "/w"), "unattributed")
            seat = session_tidy.resolve_seat("codex", "/w", os.environ, "codex-sid", "")
            session_tidy.bump_prompt_seq(seat, "codex", "codex-sid", time.time())
            self.assertEqual(router._turn_peer_source("codex", "codex-sid", "/w"), "unattributed")
            peer.cmd_record(argparse.Namespace(
                from_harness="claude", from_session_id="sup-sid", from_name="hearting-4d", from_project="p",
                to_harness="codex", to_session_id="codex-sid", to_name=None, kind="notice", surface="herdr",
                status="received", receipt="exact-peer-ref", ref=["0123456789abcdef0123456789abcdef"], transfer_ref="0123456789abcdef0123456789abcdef",
                body_file=None, body_stdin=False, body_text="herdr steer received"))
            self.assertEqual(router._turn_peer_source("codex", "codex-sid", "/w"), "peer:hearting-4d·0123456789abcdef0123456789abcdef")
            self.assertEqual(router._turn_peer_source("codex", "other-sid", "/w"), "unattributed")

    def test_only_pin_targets_change_and_other_routes_rows_are_ignored(self):
        with self.assertRaises(ValueError):
            RA.record_pin_change(self.route, target="unit", pin={"harness": "claude"}, by={}, source="x",
                                 tuples=[], candidates=[])
        path = RA.pin_changes_path(self.route)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "route_id": self.route["route_id"], "route_hash": "sha256:other",
                                    "target": "owner", "pin": {"harness": "claude"}}) + "\nnot json\n")
        self.assertIs(RA.route_in_force(self.route), self.route)


class WorkerPinChangeTest(unittest.TestCase):
    """The BC route rt-96bab699 after its owner moved to Claude: the parent now moves the worker
    pin to a stronger Codex model for the remaining checks; the owner pin and the sealed route stay."""

    change = OwnerPinChangeTest.change
    isolated_env = OwnerPinChangeTest.isolated_env
    start_pin = OwnerPinChangeTest.start_pin

    def setUp(self):
        OwnerPinChangeTest.setUp(self)
        self.change()                                            # the owner already moved to claude
        self.route_file = Path(self.temp.name) / "route.json"
        self.route_file.write_text(json.dumps(self.route))
        self.probed = [_tuple("claude", "codex", sandbox="bypass")]

    def test_the_parent_moves_the_worker_model_and_the_next_stage_launch_uses_it(self):
        import model_profile
        before = model_profile.route_selection_pin(self.route_file, worker_type="stage", adapter="codex")
        self.assertEqual(before, {"status": "harness-only"})
        result, readiness = self.start_pin(["worker=codex:gpt-6.1-sol@high"], caller=("claude", "claude-sid"),
                                           owner_parent="claude-sid")
        self.assertTrue(result["changed"])
        self.assertEqual(result["worker"], {"harness": "codex", "model": "gpt-6.1-sol", "effort": "high"})
        self.assertEqual((readiness.call_args.args[2], readiness.call_args.args[3]), ("claude", ["codex"]))
        rows = RA.pin_changes(self.route)
        self.assertEqual([row["target"] for row in rows], ["owner", "worker"])
        self.assertEqual(rows[-1]["previous"], {"harness": "codex", "model": None, "effort": None})
        self.assertEqual(model_profile.route_selection_pin(self.route_file, worker_type="stage", adapter="codex"),
                         {"status": "applied", "model": "gpt-6.1-sol", "effort": "high"})
        self.assertEqual(model_profile.route_selection_pin(self.route_file, worker_type="review", adapter="codex"),
                         {"status": "applied", "model": "gpt-6.1-sol", "effort": "high"})
        self.assertEqual(RA.sealed_pin_harness(self.route, worker_type="owner"), "claude")   # owner unchanged
        self.assertEqual(self.route, json.loads(self.route_file.read_text()))               # sealed route unchanged
        again, _ = self.start_pin(["worker=codex:gpt-6.1-sol@high"], caller=("claude", "claude-sid"),
                                  owner_parent="claude-sid")
        self.assertFalse(again["changed"])

    def test_composing_the_work_again_carries_the_pins_in_force(self):
        import shlex
        import work_start
        self.start_pin(["worker=codex:gpt-6.1-sol@high"], caller=("claude", "claude-sid"), owner_parent="claude-sid")
        argv = shlex.split(work_start._compose_again({**self.route, "capability": "autopilot-code", "slug": "s"}))
        pins = [argv[i + 1] for i, word in enumerate(argv) if word == "--pin"]
        self.assertIn("owner=claude", pins)
        self.assertIn("worker=codex:gpt-6.1-sol@high", pins)

    def test_a_frame_pin_is_probed_from_this_session(self):
        result, readiness = self.start_pin(["frame=claude"], caller=("claude", "claude-sid"),
                                           owner_parent="claude-sid",
                                           probed=[_tuple("claude", "claude", sandbox="bypass")])
        self.assertEqual((readiness.call_args.args[2], readiness.call_args.args[3]), ("claude", ["claude"]))
        self.assertEqual(result["frame"]["harness"], "claude")
        self.assertEqual(RA.changed_pin_harness(self.route, "frame"), "claude")

    def test_a_derived_continuation_launches_on_the_moved_worker_harness(self):
        import stage_session_contract as SSC
        manifest = {"route_file": str(self.route_file)}
        self.assertIsNone(SSC._worker_adapter(manifest))         # never moved: the source adapter stays
        self.start_pin(["worker=claude"], caller=("claude", "claude-sid"), owner_parent="claude-sid",
                       probed=[_tuple("claude", "claude", sandbox="bypass")])
        self.assertEqual(SSC._worker_adapter(manifest), "claude")


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
    """Case 5: one read-only request is readable and unwritten on every harness (RA-7, stage 3);
    the grant records each runtime's guarantee."""

    def test_read_roots_are_projected_on_every_runtime(self):
        request = ExecutionAccessRequest(
            writable_roots=(), read_roots=(Path("/nas/records"),), network_required=False,
            network_reason="", network_hosts=(), enforcement_required="any", justification=(),
            request_sha256="0" * 64, source_path=Path("/tmp/request.json"))
        for runtime, grade in (("claude-cli", "tool-permission"), ("codex-exec", "os-sandbox"),
                               ("opencode", "tool-permission")):
            with self.subTest(runtime=runtime):
                grant = RA.access_grant(request, runtime=runtime)
                self.assertEqual(grant.read_roots, (Path("/nas/records"),))
                self.assertEqual(grant.unwritable_read_roots, (Path("/nas/records"),))
                self.assertEqual((grant.read_enforcement, grant.unmet), (grade, ()))


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

    def test_a_fence_or_two_trailing_sentences_leave_the_verdict(self):
        # G1: the envelope plus a code fence and/or up to two sentences is
        # still that envelope. The captured fields never include the tail.
        base = "artifact: -\nverdict: {verdict}\nblocker: {blocker}"
        tails = [
            "",
            "\n",
            "\nAll done.",
            "\nAll done.\nClosing out.",
            "\n```log\nsome output\n```",
            "\n```log\nsome output\n```\nAll done.",
            "\n```log\nsome output\n```\nAll done.\nClosing out.",
            "\n\nAll done.",
        ]
        for verdict, blocker in (("PASS", "none"), ("FAIL", "real"), ("BLOCKED", "waiting")):
            for tail in tails:
                with self.subTest(verdict=verdict, tail=tail[:20]):
                    handoff = RA.HANDOFF_RE.search(base.format(verdict=verdict, blocker=blocker) + tail)
                    self.assertIsNotNone(handoff)
                    self.assertEqual(
                        handoff.groupdict(),
                        {"artifact": "-", "verdict": verdict, "blocker": blocker},
                    )
        # Three sentences, a verdict-mentioning tail, or no envelope still fail closed.
        for text in (
            "artifact: -\nverdict: PASS\nblocker: none\nOne.\nTwo.\nThree.",
            "artifact: -\nverdict: PASS\nblocker: none\nverdict: FAIL",
            "artifact: -\nverdict: PASS\nblocker: none\nVerdict: FAIL",
            "artifact: -\nverdict: PASS\nblocker: none\n## 평결: FAIL",
            "artifact: -\nverdict: PASS\nblocker: none\nThe verdict: FAIL.",
            "artifact: -\nverdict: PASS\nblocker: none\nFinal verdict: FAIL",
            "artifact: -\nverdict: PASS\nblocker: none\n`verdict: FAIL`",
            "artifact: -\nverdict: PASS\nblocker: none\n> verdict: FAIL",
            "artifact: -\nverdict: PASS\nblocker: none\nThe verdict: all good",
            "artifact: -\nverdict: PASS\nblocker: none\nAll tests PASS.",
            "artifact: -\nverdict: PASS\nblocker: none\nartifact: -",
            "artifact: -\nverdict: PASS\nblocker: none\n```\nverdict: FAIL\n```",
            "see artifact: -\nverdict: PASS\nblocker: none",
        ):
            with self.subTest(text=text[:40]):
                self.assertIsNone(RA.HANDOFF_RE.search(text))
        # A sentence that names no verdict stays a sentence.
        for tail in ("\nAll tests pass.", "\nDONE.", "\n검증을 마쳤습니다."):
            with self.subTest(tail=tail):
                mentioned = RA.HANDOFF_RE.search(
                    "artifact: -\nverdict: PASS\nblocker: none" + tail
                )
                self.assertEqual(mentioned.group("verdict"), "PASS")
        # Two envelopes still resolve to the last one.
        last = RA.HANDOFF_RE.search(
            "artifact: -\nverdict: PASS\nblocker: none\n\nartifact: -\nverdict: FAIL\nblocker: real"
        )
        self.assertEqual(last.group("verdict"), "FAIL")

    def test_an_envelope_inside_a_fence_never_hides_the_later_one(self):
        # PR #281 review round 1: a fenced later envelope must not be swallowed
        # by an earlier decoy; the later block is the one read (or the tail
        # fails closed when only a field line sits in the fence).
        decoy = ("Contract example:\nartifact: <path>\nverdict: PASS\nblocker: none\n"
                 "```\nartifact: /r.md\nverdict: FAIL\nblocker: 3 tests fail\n```")
        handoff = RA.HANDOFF_RE.search(decoy)
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.group("verdict"), "FAIL")
        self.assertEqual(handoff.group("artifact"), "/r.md")

    def test_a_long_failing_tail_stays_linear(self):
        # PR #281 review rounds 1-3: optional newlines between tail elements
        # made the failing search cubic (4k chars did not finish in 250s), and
        # a long indented line re-scanned the space run per giveback
        # (80k spaces: 2.7s). Newline-only gaps keep both cases linear.
        text = "artifact: -\nverdict: BLOCKED\nblocker: " + "a" * 100_000 + "\nx\ny\nz"
        start = time.monotonic()
        self.assertIsNone(RA.HANDOFF_RE.search(text))
        self.assertLess(time.monotonic() - start, 5.0)
        spaces = "artifact: -\nverdict: BLOCKED\nblocker: b\n" + " " * 200_000 + "verdict: FAIL"
        start = time.monotonic()
        self.assertIsNone(RA.HANDOFF_RE.search(spaces))
        self.assertLess(time.monotonic() - start, 5.0)


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



class WrapperAdmissionTest(unittest.TestCase):
    """RA-11 (stage 3): the three wrappers' completion/preview gate and access binding are
    route_authority's, and they read the same registry before the claim."""

    WRAPPERS = ("claude", "codex", "opencode")

    def test_the_three_wrappers_call_the_one_admission(self):
        for harness in self.WRAPPERS:
            with self.subTest(harness=harness):
                wrapper = _load(f"route_authority_{harness}_wrapper", f"adapters/{harness}/bin/dispatch-headless.py")
                self.assertIs(wrapper.completion_gate_fail_fields, RA.completion_gate_fail_fields)
                source = (ROOT / f"adapters/{harness}/bin/dispatch-headless.py").read_text(encoding="utf-8")
                self.assertEqual(source.count("route_authority.completion_gate("), 2)
                self.assertEqual(source.count("route_authority.bind_launch_access("), 1)
                self.assertNotIn("load_parent_effective_grant(", source)
                self.assertNotIn("recover_preview_gate_after_refusal(", source)

    def test_a_refused_gate_comes_back_with_its_preview_recovery_and_fields(self):
        args = SimpleNamespace(route_file="/r/route.json", route_node="test", attempt_id="att-x",
                               jobs=None, agent_home=ROOT)
        seen = []
        def refusing(*a, **k):
            seen.append(k["planned_revision_nodes"])
            raise DC.DispatchContractError("human-gate-not-raised", "preview-disposition")
        with mock.patch("review_input.preview_request_nodes", return_value=("plan",)), \
             mock.patch.object(DC, "recover_preview_gate_after_refusal", return_value="preview_gate_recovery=unavailable"):
            reason, code, fields = RA.completion_gate(args, "start", ROOT, Path("/j/jobs.log"), gate=refusing,
                                                      before=(lambda: seen.append("before"),))
        self.assertEqual(seen, ["before", ("plan",)])
        self.assertEqual((reason, code, fields["detail"], fields["child_spawned"]),
                         ("human-gate-not-raised", 65, "preview_gate_recovery=unavailable", "0"))
        self.assertIsNone(RA.completion_gate(args, "start", ROOT, Path("/j/jobs.log"), gate=lambda *a, **k: None))

    def test_the_registry_before_the_claim_is_one_rule(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": "/inherited/jobs.log"}):
            self.assertEqual(RA.prelaunch_registry(SimpleNamespace(jobs="/explicit/jobs.log", agent_home=ROOT)),
                             Path("/explicit/jobs.log"))
            self.assertEqual(RA.prelaunch_registry(SimpleNamespace(jobs=None, agent_home=ROOT)),
                             Path("/inherited/jobs.log"))

    def test_a_child_access_request_needs_its_exact_parent(self):
        from execution_access import ExecutionAccessError
        with tempfile.TemporaryDirectory() as td:
            request = Path(td) / "request.json"
            request.write_text("{}")
            args = SimpleNamespace(worktree=td, artifact_root=td, jobs_path=Path(td) / "jobs.log", agent_home=ROOT,
                                   dispatch_depth=2, execution_access_file=str(request), parent_binding=None)
            with self.assertRaises(ExecutionAccessError) as refused:
                RA.bind_launch_access(args, runtime="opencode", default_roots=())
            self.assertEqual(refused.exception.reason, "execution-access-exceeds-parent:parent-grant-unknown")
            args.execution_access_file = None
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AGENT_DISPATCH_EXECUTION_ACCESS_FILE", None)
                self.assertIsNone(RA.bind_launch_access(args, runtime="opencode", default_roots=()))

class ReleaseAxisTest(unittest.TestCase):
    """Launch roots that differ only because the installed release moved, from one managed
    release to another verified one, are where the launch runs, not the work."""

    OLD, NEW = "/opt/hearting/releases/v1", "/opt/hearting/releases/v2"

    @staticmethod
    def identity(kind, path, release):
        return {"kind": kind, "path": path, "release_id": release,
                "content_digest": "sha256:c" + release, "binding_digest": "sha256:b" + release}

    def roots(self, top, release, *, registry=None, jobs="/state/dispatch/jobs.log"):
        return {"runtime_root": self.identity("runtime_root", top, release),
                "launch_home": self.identity("launch_home", top, release),
                "wrapper_root": self.identity("wrapper_root", top + "/adapters", release),
                "registry_root": self.identity("registry_root", registry or top, release),
                "jobs_path": self.identity("jobs_path", jobs, release),
                "grounding_roots.cwd": self.identity("grounding_roots.cwd", "/work", "abc")}

    def moved(self, sealed, current, verified=None):
        return RA.release_moved(sealed, current, managed_release=lambda path: path == (verified or self.NEW))

    def test_a_managed_release_move_relieves_the_release_roots_and_the_registry_s_release(self):
        sealed = self.roots(self.OLD, "release:v1:" + "a" * 12)
        current = self.roots(self.NEW, "release:v2:" + "b" * 12)
        self.assertEqual(self.moved(sealed, current), RA.RELEASE_LAUNCH_ROOTS | {"jobs_path"})

    def test_a_registry_or_work_root_that_moved_is_never_relieved(self):
        sealed = self.roots(self.OLD, "release:v1:" + "a" * 12)
        current = self.roots(self.NEW, "release:v2:" + "b" * 12, jobs="/other/jobs.log",
                             registry="/home/me/checkout")
        self.assertEqual(self.moved(sealed, current), RA.RELEASE_LAUNCH_ROOTS - {"registry_root"})

    def test_a_projection_tree_a_checkout_or_a_malformed_root_is_never_a_release_move(self):
        sealed = self.roots(self.OLD, "release:v1:" + "a" * 12)
        projection = self.roots("/home/me/.claude", "release:v2:" + "b" * 12)
        self.assertEqual(self.moved(sealed, projection), frozenset())        # not a verified release copy
        checkout = self.roots(self.OLD, "0" * 40)
        self.assertEqual(self.moved(checkout, self.roots(self.NEW, "release:v2:" + "b" * 12)), frozenset())
        for broken in (None, 7, {"kind": "runtime_root", "path": "relative/root"},
                       {"kind": "wrapper_root", "path": self.NEW}, {"kind": "runtime_root", "path": None}):
            with self.subTest(broken=broken):
                current = dict(self.roots(self.NEW, "release:v2:" + "b" * 12), runtime_root=broken)
                self.assertEqual(self.moved(sealed, current), frozenset())

    def test_the_replacement_keeps_the_runtime_derived_values_of_the_release(self):
        import dispatch_replacement as R
        self.assertIs(R.RUNTIME_DERIVED_KEYS, RA.RELEASE_DERIVED_VALUES)


class AccessChangeTest(unittest.TestCase):
    """BC rt-839dbd48: the route's parent hands its next owner another access request; the
    request in force is the latest `access` row beside the route, and the sealed route stays."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        (base / "wt").mkdir()
        self.data = base / "DB" / "WWD_LG"
        self.data.mkdir(parents=True)
        self.route = {"route_id": "rt-839dbd48af21216d", "route_hash": "sha256:" + "e" * 64,
                      "artifact_root": str(base / "artifacts"), "cwd": str(base / "wt"),
                      "selection_pins": {"contract_version": 1, "owner": {"harness": "opencode", "model": None,
                                                                         "effort": None}}}
        self.request_file = base / "access.json"
        self.request_file.write_text(json.dumps({"schema_version": 1, "writable_roots": [],
                                                 "read_roots": [str(self.data)], "network": {"required": False}}))
        self.jobs = base / "state" / "jobs.log"

    def row(self, root="/data/a", source="unattributed", route=None, digest=None):
        request = {"read_roots": [root]}
        return RA.record_access_change(route or self.route, request=request,
                                       request_sha256=digest or RA.request_digest(request),
                                       by={"harness": "opencode", "session_id": "oc-sid"}, source=source)

    def test_the_latest_access_row_is_in_force_and_the_pins_are_untouched(self):
        self.assertIsNone(RA.access_in_force(self.route))
        first = self.row()
        self.assertIsNone(first["previous_sha256"])
        self.assertIsNone(self.row())                          # the same request again records nothing
        second = self.row("/data/b", source="peer:hearting-4d·" + "f" * 32)
        self.assertEqual(second["previous_sha256"], first["request_sha256"])
        self.assertEqual(RA.access_in_force(self.route)["request_sha256"], second["request_sha256"])
        self.row("/data/c", digest="0" * 64)                     # a row that does not digest to its request
        self.assertEqual(RA.access_in_force(self.route)["request_sha256"], second["request_sha256"])
        self.assertEqual(RA.pin_changes(self.route), [])       # the pin readers skip access rows
        self.assertIs(RA.route_in_force(self.route), self.route)
        other = dict(self.route, route_hash="sha256:" + "0" * 64)
        self.assertIsNone(RA.access_in_force(other))

    def given(self, *, caller=("opencode", "oc-sid"), owner_parent="oc-sid", env=True):
        router = _load("route_authority_capability_route_access", "utilities/capability-route.py")
        import work_start
        rows = {"att-owner": ("done", {"worker_type": "owner", "owner_route_id": self.route["route_id"],
                                       "parent_sid": owner_parent, "attempt_id": "att-owner"})}
        extra = {"AGENT_DISPATCH_EXECUTION_ACCESS_FILE": str(self.request_file)} if env else {}
        with mock.patch.dict(os.environ, extra), \
             mock.patch.object(RA, "caller_identity", return_value=caller), \
             mock.patch.object(work_start, "_rows", return_value=rows), \
             mock.patch.object(router, "_turn_peer_source", return_value="unattributed"):
            if not env:
                os.environ.pop("AGENT_DISPATCH_EXECUTION_ACCESS_FILE", None)
            return router._record_access_change(self.route, self.jobs)

    def test_the_parent_s_request_at_start_or_correct_is_recorded_once(self):
        self.assertIsNone(self.given(env=False))
        result = self.given()
        self.assertEqual((result["changed"], result["source"]), (True, "unattributed"))
        (row,) = RA.access_changes(self.route)
        self.assertEqual(row["request"]["read_roots"], [str(self.data)])
        self.assertEqual(row["by"], {"harness": "opencode", "session_id": "oc-sid"})
        self.assertFalse(self.given()["changed"])

    def test_another_session_or_an_invalid_request_records_nothing(self):
        refused = self.given(caller=("claude", "other-sid"))
        self.assertEqual(refused, {"changed": False, "reason": "access-change-parent-only"})
        self.request_file.write_text(json.dumps({"schema_version": 1, "writable_roots": ["/"],
                                                 "read_roots": [], "network": {"required": False}}))
        invalid = self.given()
        self.assertFalse(invalid["changed"])
        self.assertTrue(invalid["reason"].startswith("execution-access-root-too-broad"))
        self.assertEqual(RA.access_changes(self.route), [])


if __name__ == "__main__":
    unittest.main()
