#!/usr/bin/env python3
"""The framed flow from the frame briefs to the first leg: proposals, the decision record, the
first-leg transaction, every none ending, the failure table and restart at each boundary.

Real compose, route files, producer cycles, completion/close/finalize code and a real record on
disk. Controlled here: the two frame markers (their gate), the registered launch (a fake `run`
that registers one row like the selector does), and the readiness probe (fixed evidence).
"""
import contextlib
import copy
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


F = _load("framed_route_for_framed_start", "framed_route.test.py")
T, R, P, L = F.T, F.R, F.P, F.L
import route_plan as RP  # noqa: E402
import work_start as W  # noqa: E402
import frame_interview as FI  # noqa: E402

# `StartBase` replaces `R.proposal_readiness` with a fixture; the real function stays reachable here.
REAL_PROPOSAL_READINESS = R.proposal_readiness
DIRECT = {"capability": "autopilot-code", "shape": "direct", "why": "one small edit"}
CODE_STAGED = {"capability": "autopilot-code", "mode": "dev", "shape": "staged",
               "graph": ["plan", "execute", "test", "report"], "why": "a bug with a test"}
LAB_SETUP = {"capability": "autopilot-lab", "mode": "setup", "shape": "staged",
             "graph": ["scaffold", "smoke", "full-run", "run-verify", "handoff"]}
ROUTE_LABELS = ("예, 이 경로로 진행", "아니요, 다르게 할게요")


def brief(legs, *, summary="Do the work", approvals=None, section=True, tail=""):
    """A direction brief whose section 8 carries one route_proposal_v1 block."""
    body = {"summary": summary, "legs": legs}
    if approvals:
        body["entry_approvals"] = approvals
    import yaml
    text = yaml.safe_dump({"route_proposal_v1": body}, sort_keys=False, allow_unicode=True)
    head = "---\nstatus: framed\n---\n\n## 1. Problem\n\nx\n\n## 7. Questions\n\nnone\n\n"
    if not section:
        return head + "## 8. Something else\n\nnothing\n"
    return head + "## 8. 경로 조립 제안\n\n```yaml\n" + text + "```\n" + tail


def question(qid, *, kind="yes-no", topic=None, labels=("네", "아니요"), approves=None):
    """`approves` is the index of the option that approves (an approval question carries exactly one)."""
    return {"id": qid, "topic": topic or qid, "question": f"{qid}?", "kind": kind,
            "options": [{"label": label, "means": label, **({"approves": True} if at == approves else {})}
                        for at, label in enumerate(labels)],
            "recommended": 0, "why": "only you can say"}


class StartBase(F.EndingBase):
    """A framed route whose two frames are done, with hooks for briefs, interview and first leg."""

    def setUp(self):
        super().setUp()
        self.leg_calls = []
        self.fault = None
        W._ROUTE_MODULE = R
        self.addCleanup(setattr, W, "_ROUTE_MODULE", None)
        self.addCleanup(setattr, W, "FAULT_HOOK", None)
        patch = mock.patch.object(R, "proposal_readiness", create=False, side_effect=self.readiness)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(W, "_recorded_interview", side_effect=lambda route, jobs: (self.interview, self.answers))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(W, "default_parent_session_id", return_value="parent")
        patch.start()
        self.addCleanup(patch.stop)
        self.interview = self.answers = None
        self.set_briefs(DIRECT, DIRECT)
        self.set_interview({"legs": [DIRECT]})

    def readiness(self, frame_route, jobs):
        return {"tuples": [T.nested("claude", "codex")], "candidates": T.registered_headless()["candidates"]}

    # -- fixtures --------------------------------------------------------------------------
    def set_briefs(self, first, second, **kw):
        for node, leg in zip(F.FRAME_IDS, (first, second)):
            text = leg if isinstance(leg, str) else brief(leg if isinstance(leg, list) else [leg], **kw)
            (self.output / "shards" / node / "direction-brief.md").write_text(text, encoding="utf-8")

    def set_interview(self, proposal, *, choice=0, route_choice=None, extra_questions=(), summary="Do the work",
                      approval_choice=None):
        """An interview carrying `route_proposals` for `proposal` and the answers to it."""
        proposal = {"summary": summary, "entry_approvals": [], **proposal}
        questions = [question("route", labels=ROUTE_LABELS), *extra_questions]
        self.interview = {
            "schema": FI.SCHEMA, "route_id": self.route["route_id"], "round": 1, "questions": questions,
            "route_proposals": {"question": "route", "by_option": {ROUTE_LABELS[0]: proposal}}}
        given = {"route": {"choice": choice if route_choice is None else route_choice, "note": ""}}
        for extra in extra_questions:
            given[extra["id"]] = {"choice": 0 if approval_choice is None else approval_choice, "note": ""}
        self.answers = {"schema": FI.ANSWERS_SCHEMA, "route_id": self.route["route_id"], "round": 1,
                        "understanding_confirmed": True, "correction": "", "answers": given}

    def fake_run(self, command, **kwargs):
        """The selector admits the first leg's owner: one registered row."""
        self.leg_calls.append(command)
        value = lambda flag: command[command.index(flag) + 1]
        route = json.loads(Path(value("--route-evidence")).read_text(encoding="utf-8"))
        meta = {"attempt_id": value("--attempt-id"), "parent_sid": "parent", "launch_started": "1",
                "worker_type": "owner", "dispatch_depth": "1", "route_id": route["route_id"],
                "route_hash": route["route_hash"], "owner_route_id": route["route_id"],
                "owner_route_hash": route["route_hash"],
                "parent_completion_delivery": "codex-managed-gateway"}
        with self.jobs.open("a") as stream:
            stream.write("now\topen\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    def settle(self, **kw):
        kw.setdefault("run", self.fake_run)
        with mock.patch.object(W, "join_selected_attempts", return_value={"state": "timeout", "children": []}), \
                mock.patch.object(W, "parent_next", return_value=("end-turn", "fixture", "")):
            return super().settle(**kw)

    # -- reads -----------------------------------------------------------------------------
    def record(self):
        return RP.read_record(self.record_path())

    def leg_routes(self):
        return sorted(p for p in (self.root / ".runtime" / "routes").glob("rt-*.json")
                      if re.fullmatch(r"rt-[0-9a-f]{16}\.json", p.name) and p.stem != self.route["route_id"])

    def registry_rows(self):
        return [line for line in self.jobs.read_text().splitlines() if line.strip()]

    def finish_leg(self, route, route_file):
        """What a finished leg leaves behind: its proven-closed route and its sealed producer cycle."""
        if not any(r.get("route_id") == route["route_id"] for r in P.list_cycle_records(self.root)):
            self.begin_cycle(route_file)           # a registered owner opens its own cycle when it starts
        self.close(route, route_file)
        record = next(r for r in P.list_cycle_records(self.root) if r.get("route_id") == route["route_id"])
        output = Path(P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record)) / "artifacts/documents/done.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("done\n", encoding="utf-8")
        P.finalize(self.root, cycle_id=record["cycle_id"], state="completed")
        return record["cycle_id"]


class SelectedLegTest(StartBase):
    def test_a164_2_a_direct_proposal_starts_one_first_leg_and_closes_the_frame_route(self):
        result = self.settle()
        self.assertEqual((result["state"], result["required_action"]), ("inline", "execute-inline"), result)
        self.assertEqual(result["route_decision"]["selected"], ROUTE_LABELS[0])
        record = self.record()
        decision, first = record["decision"], record["first_leg"]
        self.assertEqual(decision["reason"], "")
        self.assertEqual(decision["proposal"]["legs"][0]["shape"], "direct")
        self.assertEqual(len(self.leg_routes()), 1)
        leg = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
        self.assertEqual(first["route"]["route_id"], leg["route_id"])
        self.assertEqual(leg["selection"]["shape"], "direct")
        self.assertEqual(leg["parent_cycle_id"], decision["frame_route"]["cycle_id"])
        self.assertEqual(leg["campaign_key"], "framed-key")
        self.assertEqual(leg["route_plan"], {"decision": self.record_path().relative_to(self.root).as_posix(),
                                              "digest": record["digest"], "index": 0})
        self.assertEqual(self.cli_calls, ["complete", "close"])
        self.assertTrue(R.outcome_path(self.path).exists())
        self.assertEqual(P.read_cycle_record(self.root, decision["frame_route"]["cycle_id"])["state"], "sealed")
        self.assertNotIn("next_leg", result)
        self.assertEqual(first["receipt_digest"][:7], "sha256:")
        rows = {row["node"]: row for row in decision["proposals"]}
        self.assertEqual({row["reason"] for row in rows.values()}, {"valid"})
        for node, row in rows.items():                       # the proposal's own text is kept, with its brief digest
            self.assertTrue(row["source"].startswith("route_proposal_v1:"), row)
            self.assertEqual(row["sha256"], RP.file_digest(self.output / "shards" / node / "direction-brief.md"))

    def test_a_staged_proposal_starts_one_registered_owner_and_returns_its_receipt(self):
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        result = self.settle()
        self.assertEqual(result["state"], "running", result)
        self.assertEqual(result["parent_next"], "end-turn")          # the owner's own directive, unchanged
        self.assertTrue(result["owner_started"])
        rows = self.registry_rows()
        self.assertEqual(len(rows), 1)
        (leg_path,) = self.leg_routes()
        leg = json.loads(leg_path.read_text(encoding="utf-8"))
        ids = [n["id"] for n in leg["nodes"]]
        self.assertEqual(ids, ["plan", "execute", "test", "report"])
        self.assertNotIn("frame-review", leg["human_gates"])
        self.assertFalse([n for n in leg["nodes"] if R._frame_node(n)])
        self.assertEqual(leg["composed"], True)
        self.assertEqual(leg["route_plan"]["index"], 0)
        self.assertEqual(len(self.leg_calls), 1)                     # one launch, of the owner
        self.assertIn("--prompt-text", self.leg_calls[0])
        task = self.leg_calls[0][self.leg_calls[0].index("--prompt-text") + 1]
        self.assertIn("## Agreed intent", task)
        self.assertIn("direction-brief.md", task)
        record = self.record()
        self.assertEqual(record["first_leg"]["route"]["route_id"], leg["route_id"])
        self.assertEqual(record["first_leg"]["start_receipt"]["owner_attempt_id"], result["owner_attempt_id"])
        self.assertEqual(self.cli_calls, ["complete", "close"])
        self.assertTrue(R.outcome_path(self.path).exists())

    def test_a_solo_proposal_starts_one_owner_that_runs_without_a_frame_pair(self):
        solo = {"capability": "autopilot-code", "shape": "solo", "why": "one registered worker"}
        self.set_briefs(solo, solo)
        self.set_interview({"legs": [solo]})
        result = self.settle()
        self.assertEqual(result["state"], "running", result)
        (leg_path,) = self.leg_routes()
        leg = json.loads(leg_path.read_text(encoding="utf-8"))
        self.assertEqual([n["id"] for n in leg["nodes"]], ["one-shot"])
        self.assertEqual(leg["selection"]["shape"], "solo")
        self.assertEqual((len(self.leg_calls), len(self.registry_rows())), (1, 1))
        self.assertNotIn("--route-node", self.leg_calls[0])           # no frame launch, only the owner
        self.assertEqual(self.cli_calls, ["complete", "close"])

    def test_a_repeated_start_after_the_ending_returns_the_legs_current_state_and_starts_nothing(self):
        # The owner still runs, so its current state is the receipt the first start stored.
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        first = self.settle()
        from dispatch_notice_state import closed_outcome
        closed = closed_outcome(Path(self.path), self.route)
        again = self.settle(closed=closed)
        self.assertEqual({k: v for k, v in again.items() if k != "route_decision"},
                         {k: v for k, v in first.items() if k != "route_decision"})
        self.assertEqual(len(self.leg_calls), 1)
        self.assertEqual(len(self.leg_routes()), 1)
        self.assertEqual(len(self.registry_rows()), 1)
        self.assertEqual(self.cli_calls, ["complete", "close"])

    def test_a164_3_two_different_proposals_each_select_their_own_route(self):
        for label_index, leg in ((0, DIRECT), (1, CODE_STAGED)):
            with self.subTest(route=leg["shape"]):
                self.tearDown()
                self.setUp()
                self.set_briefs(DIRECT, CODE_STAGED)
                self.interview = {
                    "schema": FI.SCHEMA, "route_id": self.route["route_id"], "round": 1,
                    "questions": [question("route", kind="choice", labels=("먼저 빠르게", "단계대로", "다르게 할게요"))],
                    "route_proposals": {"question": "route", "by_option": {
                        "먼저 빠르게": {"summary": "a", "legs": [DIRECT], "entry_approvals": []},
                        "단계대로": {"summary": "b", "legs": [CODE_STAGED], "entry_approvals": []}}}}
                self.answers["answers"] = {"route": {"choice": label_index, "note": ""}}
                self.settle()
                record = self.record()
                self.assertEqual(record["decision"]["selected"], ("먼저 빠르게", "단계대로")[label_index])
                leg = json.loads(self.leg_routes()[0].read_text(encoding="utf-8"))
                self.assertEqual(leg["selection"]["shape"], "direct" if label_index == 0 else "staged")
                self.assertEqual(len(self.leg_routes()), 1)


class NoneEndingTest(StartBase):
    def assert_none(self, reason_prefix, result=None):
        result = result or self.settle()
        self.assertEqual((result["state"], result["required_action"], result["selected"]),
                         ("completed", "compose-route", "none"), result)
        self.assertTrue(result["decision_reason"].startswith(reason_prefix), result["decision_reason"])
        self.assertEqual(self.leg_routes(), [])
        self.assertEqual(self.registry_rows(), [])
        self.assertEqual(self.leg_calls, [])
        self.assertEqual(self.cli_calls, ["complete", "close"])
        self.assertTrue(R.outcome_path(self.path).exists())
        self.assertEqual(P.read_cycle_record(self.root, self.record()["decision"]["frame_route"]["cycle_id"])["state"], "sealed")
        for forbidden in ("next_leg", "parent_next", "parent_next_command"):
            self.assertNotIn(forbidden, result)
        self.assertNotIn("first_leg", self.record())
        return result

    def test_a164_4_an_interview_without_route_proposals_ends_as_before(self):
        del self.interview["route_proposals"]
        self.assert_none("proposal-not-read")

    def test_a164_4_a_declined_route_question_selects_nothing(self):
        self.set_interview({"legs": [DIRECT]}, route_choice=1)
        self.assert_none("route-declined")

    def test_a164_4_an_off_menu_answer_selects_nothing(self):
        self.set_interview({"legs": [DIRECT]}, route_choice="none")
        self.assert_none("route-off-menu")

    def test_a164_4_a_proposal_no_brief_made_is_not_started(self):
        self.set_interview({"legs": [CODE_STAGED]})            # the briefs proposed a direct step
        self.assert_none("proposal-not-verified")

    def test_a164_4_briefs_with_no_valid_proposal_end_with_each_reason_recorded(self):
        self.set_briefs(brief([DIRECT], section=False), "## 8. 경로 조립 제안\n\nnothing fenced\n")
        result = self.assert_none("proposal-not-verified")
        rows = {row["node"]: row for row in self.record()["decision"]["proposals"]}
        self.assertEqual(rows["frame"]["reason"], "section-missing")
        self.assertEqual(rows["frame-alternative"]["reason"], "block-missing")

    def test_a164_4_one_invalid_leg_makes_the_whole_brief_none(self):
        bad = {"capability": "autopilot-code", "shape": "staged", "graph": ["execute", "no-such-stage"]}
        self.set_briefs([DIRECT, bad], DIRECT)
        self.set_interview({"legs": [DIRECT, bad]})
        self.assert_none("proposal-not-verified")
        rows = {row["node"]: row for row in self.record()["decision"]["proposals"]}
        self.assertRegex(rows["frame"]["reason"], r"^leg-invalid:1:compose-graph-unknown-node")
        self.assertEqual(rows["frame-alternative"]["reason"], "valid")


class ApprovalTest(StartBase):
    """Start approvals: taken in the one interview, valid only for the leg and parts the user saw."""

    def lab(self, *, approval=True, approval_choice=None, labels=("예, 지금 시작", "아니요, 나중에"), approves=0):
        approvals = [{"key": "full-run", "leg": 0, "question": "run-ok"}] if approval else []
        self.set_briefs(LAB_SETUP, LAB_SETUP, approvals=approvals)
        self.set_interview({"legs": [LAB_SETUP], "entry_approvals": approvals},
                           extra_questions=[question("run-ok", labels=labels, approves=approves)] if approval else (),
                           approval_choice=approval_choice)

    def test_a164_10_a_yes_to_the_approval_starts_the_leg_and_records_what_was_approved(self):
        self.lab()
        result = self.settle()
        self.assertEqual(result["state"], "running", result)
        given = self.record()["decision"]["approvals"]["given"]
        self.assertEqual(given, [{"key": "full-run", "leg": 0, "question": "run-ok", "label": "예, 지금 시작",
                                  "accepted": True, "parts": ["autopilot-lab:full-run"]}])
        task = self.leg_calls[0][self.leg_calls[0].index("--prompt-text") + 1]
        self.assertIn("- full-run for leg 0 (autopilot-lab:full-run): approved", task)
        self.assertEqual(len(self.leg_routes()), 1)

    def test_a164_10_declining_the_approval_is_a_valid_answer_that_starts_nothing(self):
        self.lab(approval_choice=1)
        NoneEndingTest.assert_none(self, "approval-missing:full-run")
        given = self.record()["decision"]["approvals"] if False else None
        self.assertEqual(self.record()["decision"]["reason"], "approval-missing:full-run")

    def test_an_approving_option_that_is_second_starts_the_leg_when_chosen(self):
        self.lab(labels=("아니요, 나중에", "예, 지금 시작"), approves=1, approval_choice=1)
        result = self.settle()
        self.assertEqual(result["state"], "running", result)
        given = self.record()["decision"]["approvals"]["given"]
        self.assertEqual([(row["label"], row["accepted"]) for row in given], [("예, 지금 시작", True)])
        self.assertEqual(len(self.leg_routes()), 1)

    def test_a_declining_option_that_is_first_does_not_approve_when_chosen(self):
        self.lab(labels=("아니요, 나중에", "예, 지금 시작"), approves=1, approval_choice=0)
        NoneEndingTest.assert_none(self, "approval-missing:full-run")
        self.assertEqual(self.record()["decision"]["reason"], "approval-missing:full-run")
        self.assertEqual(self.leg_calls, [])

    def test_a_question_with_no_marked_option_or_two_marked_options_starts_nothing(self):
        for label, marks in (("none marked", None), ("both marked", "both")):
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.lab(approves=marks)
                if marks == "both":
                    for option in self.interview["questions"][1]["options"]:
                        option["approves"] = True
                self.assertTrue(any("approves" in e for e in FI.validate(self.interview)))
                NoneEndingTest.assert_none(self, "approval-missing:full-run")
                self.assertEqual(self.leg_calls, [])

    def test_a_leg_with_an_approval_part_and_no_approval_asked_ends_as_none(self):
        self.lab(approval=False)
        NoneEndingTest.assert_none(self, "approval-missing:full-run")

    def test_an_approval_for_a_later_leg_does_not_unlock_the_first_leg(self):
        approvals = [{"key": "full-run", "leg": 1, "question": "run-ok"}]
        self.set_briefs([DIRECT, LAB_SETUP], [DIRECT, LAB_SETUP], approvals=approvals)
        self.set_interview({"legs": [DIRECT, LAB_SETUP], "entry_approvals": approvals},
                           extra_questions=[question("run-ok", labels=("예", "아니요"), approves=0)])
        result = self.settle()                     # leg 0 is direct: no approval part, so it starts
        self.assertEqual(result["state"], "inline", result)
        given = self.record()["decision"]["approvals"]["given"]
        self.assertEqual([(row["leg"], row["accepted"]) for row in given], [(1, True)])

    def test_the_intent_shows_the_approval_with_the_parts_the_user_was_shown(self):
        self.lab()
        interview, answers = self.interview, self.answers
        scope = W._approval_scope(interview, answers)
        self.assertEqual(scope, {("full-run", 0): ["autopilot-lab:full-run"]})
        text = FI.render_intent(interview, answers, now="2026-10-01", approval_scope=scope)
        self.assertIn("## Route", text)
        self.assertIn("- full-run for leg 0 (autopilot-lab:full-run) — question `run-ok`: approved", text)
        self.assertEqual(text, FI.render_intent(interview, answers, now="2026-10-01", approval_scope=scope))
        self.assertIn("(starts now)", text)
        answers["answers"]["run-ok"]["choice"] = 1
        self.assertIn("question `run-ok`: declined", FI.render_intent(interview, answers, now="2026-10-01", approval_scope=scope))


class FailureTableTest(StartBase):
    def test_a164_11_a_first_leg_compose_refusal_keeps_the_record_and_the_same_start_resumes(self):
        with mock.patch.object(R, "compile_first_leg", side_effect=ValueError("compose-graph-order:test-before-execute")):
            blocked = self.settle()
        self.assertEqual((blocked["state"], blocked["reason"]), ("needs-attention", "first-leg-compose-refused"), blocked)
        self.assertIn("compose-graph-order", blocked["detail"])
        self.assertNotIn("first_leg", self.record())
        self.assertEqual(self.record()["decision"]["selected"], ROUTE_LABELS[0])
        self.assertFalse(R.outcome_path(self.path).exists())          # the frame route stays open
        self.assertEqual((self.leg_routes(), self.registry_rows(), self.cli_calls), ([], [], []))
        resumed = self.settle()
        self.assertEqual(resumed["state"], "inline", resumed)
        self.assertEqual(len(self.leg_routes()), 1)
        self.assertEqual(self.cli_calls, ["complete", "close"])

    def test_a164_11_a_first_leg_that_waits_for_capacity_returns_its_own_receipt_and_the_frame_stays_open(self):
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})

        def refused(command, **kwargs):
            self.leg_calls.append(command)
            return subprocess.CompletedProcess(command, 75, "check=failed\nreason=model-worker-governor-denied\n"
                                               "child_spawned=0\nretryable=1\nrefusal=class-cap\n"
                                               "worker_class=owner\nretry_after_seconds=30\n", "")
        waiting = self.settle(run=refused)
        self.assertEqual((waiting["state"], waiting["reason"]), ("waiting-capacity", "launch-capacity-wait"), waiting)
        self.assertEqual(waiting["required_action"], "resume-after-capacity")
        self.assertEqual(waiting["route_id"], self.record()["first_leg"]["route"]["route_id"])   # the leg's own receipt
        self.assertNotIn("start_receipt", self.record()["first_leg"])
        self.assertFalse(R.outcome_path(self.path).exists())
        self.assertEqual((self.cli_calls, self.registry_rows()), ([], []))
        done = self.settle()
        self.assertEqual(done["state"], "running", done)
        self.assertEqual(len(self.leg_routes()), 1)
        self.assertEqual(len(self.registry_rows()), 1)
        self.assertEqual(self.cli_calls, ["complete", "close"])

    def test_a_launch_that_is_not_admitted_is_the_first_legs_own_needs_attention(self):
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        result = self.settle(run=lambda command, **kw: subprocess.CompletedProcess(command, 65, "check=failed\nreason=x\n", ""))
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertEqual(result["reason"], "owner-launch-not-admitted")
        self.assertFalse(R.outcome_path(self.path).exists())

    def test_two_starts_at_once_fix_one_decision_one_route_and_one_attempt(self):
        import threading
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        results, errors = [], []

        def run():
            try:
                results.append(self.settle())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual((len(self.leg_routes()), len(self.registry_rows()), len(self.leg_calls)), (1, 1, 1))
        self.assertEqual(self.cli_calls.count("complete"), 1)


class FirstLegStateReplayTest(StartBase):
    """Re-reading the frame route answers with the first leg's state now, never its old launch receipt (defect 4)."""

    SECOND = {"capability": "autopilot-code", "shape": "direct", "why": "then the second"}

    def replay(self):
        from dispatch_notice_state import closed_outcome
        return self.settle(closed=closed_outcome(Path(self.path), self.route))

    def leg(self):
        (leg_path,) = self.leg_routes()
        return json.loads(leg_path.read_text(encoding="utf-8")), leg_path

    def staged_leg(self, legs=(CODE_STAGED,)):
        self.set_briefs(list(legs), list(legs))
        self.set_interview({"legs": list(legs)})
        return self.settle()

    def end_owner_row(self):
        """The owner's registry row, as a finished attempt's row reads (schema 2, status done)."""
        row = self.jobs.read_text(encoding="utf-8").replace("\topen\t", "\tdone\t", 1)
        self.jobs.write_text(row.rstrip("\n") + ",attempt_schema_version=2\n", encoding="utf-8")

    def untouched(self, before):
        self.assertEqual((len(self.leg_routes()), len(self.registry_rows()), len(self.leg_calls), self.cli_calls),
                         before)
        self.assertEqual(self.record_path().read_bytes(), self.record_bytes)

    def snapshot(self):
        self.record_bytes = self.record_path().read_bytes()
        return (len(self.leg_routes()), len(self.registry_rows()), len(self.leg_calls), list(self.cli_calls))

    def test_a_finished_direct_leg_answers_completed_not_its_old_execute_inline_receipt(self):
        first = self.settle()
        self.assertEqual((first["state"], first["required_action"]), ("inline", "execute-inline"))
        route, leg_path = self.leg()
        self.finish_leg(route, leg_path)
        before = self.snapshot()
        for _ in range(2):
            again = self.replay()
            self.assertEqual((again["state"], again["required_action"]), ("completed", "advance-completed"), again)
            self.assertEqual(again["route_id"], route["route_id"])
            self.assertEqual(again["route_decision"]["frame_route_id"], self.route["route_id"])
            self.assertNotIn("task", again)
            self.assertNotIn("artifact_env", again)
            self.assertNotIn("next_leg", again)               # the only leg of its plan
        self.untouched(before)

    def test_a_finished_leg_that_is_not_the_last_names_the_next_leg_and_starts_nothing(self):
        self.set_briefs([DIRECT, self.SECOND], [DIRECT, self.SECOND])
        self.set_interview({"legs": [DIRECT, self.SECOND]})
        self.settle()
        route, leg_path = self.leg()
        cycle = self.finish_leg(route, leg_path)
        before = self.snapshot()
        again = self.replay()
        self.assertEqual(again["state"], "completed", again)
        self.assertEqual(again["next_leg"]["index"], 1)
        self.assertEqual(shlex.split(again["next_leg"]["compose_command"])[shlex.split(again["next_leg"]["compose_command"]).index("--parent-cycle") + 1], cycle)
        self.untouched(before)

    def test_a_finished_staged_owner_leg_answers_completed(self):
        first = self.staged_leg()
        self.assertEqual(first["state"], "running")
        route, leg_path = self.leg()
        self.finish_leg(route, leg_path)
        before = self.snapshot()
        again = self.replay()
        self.assertEqual(again["state"], "completed", again)
        self.assertEqual(again["required_action"], "advance-completed")
        self.assertNotEqual(again.get("parent_next"), "end-turn")
        self.untouched(before)

    def test_a_staged_leg_whose_owner_still_runs_answers_running_without_a_second_launch(self):
        first = self.staged_leg()
        before = self.snapshot()
        again = self.replay()
        self.assertEqual(again["state"], "running", again)
        self.assertEqual(again["owner_attempt_id"], first["owner_attempt_id"])
        self.assertEqual(again["resume_command"], first["resume_command"])      # the leg's, not the frame's
        self.untouched(before)

    def test_a_staged_leg_whose_owner_died_is_reported_and_not_relaunched_by_the_replay(self):
        first = self.staged_leg()
        self.end_owner_row()
        before = self.snapshot()
        with mock.patch("dispatch_replacement.advance", side_effect=AssertionError("a replay never launches")), \
                mock.patch.object(W, "_launch_admitted", side_effect=AssertionError("a replay never launches")):
            again = self.replay()
        self.assertEqual(again["state"], "needs-attention", again)
        self.assertEqual(again["owner_attempt_id"], first["owner_attempt_id"])
        self.assertEqual(again["resume_command"], first["resume_command"])
        self.untouched(before)

    def test_a_staged_leg_parked_at_a_human_gate_answers_the_gate_not_running(self):
        first = self.staged_leg()
        parked = {"gate": "preview-disposition", "status": "blocked", "artifact": "/tmp/preview.md",
                  "route_file": str(self.leg()[1])}
        self.end_owner_row()
        before = self.snapshot()
        with mock.patch("dispatch_replacement.owner_parked_gate", return_value=parked):
            again = self.replay()
        self.assertEqual((again["state"], again["required_action"], again["gate"]),
                         ("waiting-human-gate", "answer-human-gate", "preview-disposition"), again)
        self.assertIn("release_command", again)
        self.untouched(before)

    def test_a_replay_of_an_exited_leg_owner_asks_the_approval_question_it_never_raised(self):
        # The frame route's own resume is a parent `start` too: an exited owner that never raised the gate
        # sealed on its own operation is asked about here as well, and still nothing launches or settles.
        first = self.staged_leg()
        self.end_owner_row()
        before = self.snapshot()
        parked = {"gate": "preview-disposition", "status": "blocked", "artifact": "/tmp/preview.md",
                  "route_file": str(self.leg()[1])}
        with mock.patch.object(W, "_raise_owner_entry_gate") as raised, \
                mock.patch("dispatch_replacement.owner_parked_gate",
                           side_effect=lambda *a, **k: parked if raised.called else None), \
                mock.patch("dispatch_replacement.advance", side_effect=AssertionError("a replay never launches")), \
                mock.patch.object(W, "_launch_admitted", side_effect=AssertionError("a replay never launches")):
            again = self.replay()
        raised.assert_called_once()
        self.assertEqual(raised.call_args.args[3], first["owner_attempt_id"])
        self.assertEqual((again["state"], again["required_action"], again["gate"]),
                         ("waiting-human-gate", "answer-human-gate", "preview-disposition"), again)
        self.assertEqual(again["resume_command"], first["resume_command"])
        self.untouched(before)

    def test_a_replay_of_a_leg_whose_owner_still_runs_asks_nothing(self):
        self.staged_leg()
        with mock.patch.object(W, "_raise_owner_entry_gate", side_effect=AssertionError("owner is live")):
            self.assertEqual(self.replay()["state"], "running")

    def test_an_inline_finish_still_pending_on_the_leg_is_reported_as_such(self):
        self.settle()
        route, _ = self.leg()
        state = self.root / ".runtime" / "inline-finish" / "v1" / route["route_id"] / "finish.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"schema": "inline_finish_v1", "state": "node-completed"}), encoding="utf-8")
        before = self.snapshot()
        again = self.replay()
        self.assertEqual((again["state"], again["reason"]), ("needs-attention", "finish-pending"), again)
        self.untouched(before)

    def test_a_direct_leg_that_is_still_to_be_done_keeps_answering_execute_inline(self):
        first = self.settle()
        before = self.snapshot()
        again = self.replay()
        self.assertEqual({k: v for k, v in again.items() if k != "route_decision"},
                         {k: v for k, v in first.items() if k != "route_decision"})
        self.untouched(before)

    def test_a_first_leg_that_is_not_the_one_the_record_binds_is_a_conflict_not_a_state(self):
        self.settle()
        route, leg_path = self.leg()
        forged = {**route, "cwd": str(self.root)}
        leg_path.write_text(json.dumps(forged, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.replay()


class PinnedStartBase(StartBase):
    """A framed route composed with the pins the CLI would seal (`--pin` after `_filter_top_pins`)."""

    PINS = ["owner=claude", "worker=claude:sonnet@high", "frame=claude"]
    OWNER_HARNESS = "claude"

    def compose(self, **kw):
        pins, _ = R._filter_top_pins(R._parse_selection_pins(self.PINS))
        kw.setdefault("selection_pins", pins)
        kw.setdefault("work_request", {"text": "Decide how to do this", "owner_harness": self.OWNER_HARNESS})
        return super().compose(**kw)

    def pins_sealed_on_frame(self):
        return {k: v for k, v in self.route["selection_pins"].items() if k != "contract_version"}


class SelectionPinInheritanceTest(PinnedStartBase):
    """The compose-time pins reach the leg the frame proposes and starts (defect 1)."""

    def first_leg(self):
        (leg_path,) = self.leg_routes()
        return json.loads(leg_path.read_text(encoding="utf-8"))

    def test_the_first_staged_leg_seals_the_frames_pins_and_every_worker_follows_the_worker_pin(self):
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        self.settle()
        leg = self.first_leg()
        self.assertEqual(leg["selection_pins"], self.route["selection_pins"])
        workers = [n for n in leg["nodes"] if n.get("dispatch_depth") == 2]
        self.assertTrue(workers)
        self.assertEqual({n["harness_affinity"] for n in workers}, {"claude"})
        for node in workers:
            self.assertEqual(node["harness_policy"]["primary"][0], "claude", node["id"])
        self.assertEqual(leg["work_request"]["owner_harness"], "claude")
        R.verify_route(json.loads(json.dumps(leg)), R.ROOT)

    def test_a_direct_first_leg_and_a_solo_first_leg_carry_the_pins_too(self):
        for leg_arguments in (DIRECT, {"capability": "autopilot-code", "shape": "solo", "why": "one worker"}):
            with self.subTest(shape=leg_arguments["shape"]):
                self.tearDown()
                self.setUp()
                self.set_briefs(leg_arguments, leg_arguments)
                self.set_interview({"legs": [leg_arguments]})
                self.settle()
                self.assertEqual(self.first_leg()["selection_pins"], self.route["selection_pins"])

    def test_a_pin_the_user_never_gave_adds_no_field_to_the_leg(self):
        self.tearDown()
        self.PINS, self.OWNER_HARNESS = [], None
        self.setUp()
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        self.settle()
        self.assertNotIn("selection_pins", self.route)
        leg = self.first_leg()
        self.assertNotIn("selection_pins", leg)
        self.assertEqual({n.get("harness_affinity") for n in leg["nodes"] if n.get("dispatch_depth") == 2}, {"diverse"})

    def test_a_frame_only_pin_is_sealed_on_the_leg_without_touching_worker_affinity(self):
        self.tearDown()
        self.PINS, self.OWNER_HARNESS = ["frame=claude"], None
        self.setUp()
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        self.settle()
        leg = self.first_leg()
        self.assertEqual(leg["selection_pins"], self.route["selection_pins"])
        self.assertEqual({n["harness_affinity"] for n in leg["nodes"] if n.get("dispatch_depth") == 2}, {"diverse"})

    def test_the_owner_pin_reaches_the_first_legs_work_request_even_when_the_frames_request_did_not_name_it(self):
        self.tearDown()
        self.OWNER_HARNESS = None
        self.setUp()
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        self.settle()
        self.assertEqual(self.first_leg()["work_request"]["owner_harness"], "claude")

    def test_the_leg_gets_its_own_copy_of_the_pins_and_the_frame_route_is_not_changed(self):
        before = json.dumps(self.route["selection_pins"], sort_keys=True)
        compiled = R._leg_compose_kwargs(RP.leg_arguments(CODE_STAGED), frame_route=self.route,
                                         frame_cycle_id="cyc_" + "a" * 32, slug="x")
        pins = compiled["selection_pins"]
        self.assertEqual(pins, self.pins_sealed_on_frame())
        self.assertNotIn("contract_version", pins)
        pins["worker"]["harness"] = "codex"
        self.assertEqual(json.dumps(self.route["selection_pins"], sort_keys=True), before)

    def test_the_readiness_probe_asks_about_a_pinned_harness_even_if_the_user_policy_left_it_out(self):
        seen = []
        with mock.patch.object(R, "_compose_default_children", return_value=("codex",)), \
                mock.patch.object(R, "_compose_readiness",
                                  side_effect=lambda *a: seen.append(a[3]) or {"tuples": [], "candidates": []}):
            REAL_PROPOSAL_READINESS(self.route, self.jobs)
        self.assertEqual(len(seen), 1)
        with mock.patch.object(R, "_compose_default_children", wraps=R._compose_default_children) as children, \
                mock.patch.object(R, "_compose_readiness", return_value={"tuples": [], "candidates": []}):
            REAL_PROPOSAL_READINESS(self.route, self.jobs)
        self.assertEqual(children.call_args.args[0], self.pins_sealed_on_frame())


class CatalogueInputTest(F.FramedBase):
    def test_a_frame_leg_gets_the_request_the_hints_and_the_full_catalogue_from_the_same_source_as_stages(self):
        route = self.compose(capability="autopilot-spec", graph="plan,execute",
                             work_request={"text": "Make the thing", "owner_harness": None})
        with mock.patch("subprocess.run", side_effect=AssertionError("the catalogue is never read back from CLI text")):
            text = W.frame_task_text(route)
        self.assertTrue(text.startswith("Make the thing\n"))
        self.assertIn("- capability: autopilot-spec", text)
        self.assertIn("- graph: plan,execute", text)
        block = text.split("```json\n", 1)[1].split("\n```", 1)[0]
        registry = R.TOPO.load_registry()
        expected = [R.stages_block(registry, r) for r in registry["recipes"] if r["capability"] != "route-frame"]
        self.assertEqual(json.loads(block), json.loads(json.dumps(expected)))
        self.assertIn("autopilot-lab", block)
        self.assertIn('"start_approval":"full-run"', block)
        self.assertNotIn("route-frame", block)
        self.assertIn("do not read another leg's brief", text)
        self.assertEqual(text, W.frame_task_text(route))

    def test_recipe_internal_and_quick_frames_get_the_same_unit_prompt_and_catalogue(self):
        for shape, graph in (("staged", None), ("solo", None)):
            with self.subTest(shape):
                route = R.compose_route(
                    capability="autopilot-code", capability_mode=None, shape=shape, graph=graph, slug="frames",
                    cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="k",
                    dispatch_evidence={"tuples": [T.nested("claude", "codex")]},
                    registered_headless_evidence=T.registered_headless(),
                    work_request={"text": "Fix it", "owner_harness": None})
                path = Path(L.admit_runtime_route(self.root, route).route_file)
                seen = []

                def run(command, **kw):
                    seen.append(command)
                    return subprocess.CompletedProcess(command, 1, "", "")
                W._start(route, path, self.jobs, "frame", None, run)
                command = seen[0]
                self.assertNotIn("--prompt-text", command)
                prompt = Path(command[command.index("--prompt-file") + 1]).read_text(encoding="utf-8")
                self.assertTrue(prompt.startswith("Fix it\n"))
                self.assertIn("## Part catalogue", prompt)
                self.assertEqual(route["nodes"][0]["unit"], "plan/frame")
        owner = []
        W._start(route, path, self.jobs, "owner", None, lambda command, **kw: owner.append(command) or subprocess.CompletedProcess(command, 1, "", ""))
        self.assertIn("--prompt-text", owner[0])                      # an owner still gets the request itself

    def test_both_legs_read_one_file_so_neither_sees_the_other(self):
        route, path = self.admitted()
        commands = []
        for node in F.FRAME_IDS:
            W._start(route, path, self.jobs, node, None, lambda command, **kw: commands.append(command) or subprocess.CompletedProcess(command, 1, "", ""))
        files = {c[c.index("--prompt-file") + 1] for c in commands}
        self.assertEqual(len(files), 1)
        self.assertIn("do not read another leg's brief", Path(files.pop()).read_text())


class ReviewStartTest(F.FramedStartTest):
    """`needs-interview` for a framed route carries the two validated proposals as information."""

    def test_the_interview_step_gets_both_proposals_their_equality_and_the_downgrade_place(self):
        self.start()
        self.ready = True
        self.steps = [{"state": "needs-interview", "required_action": "prepare-frame-question", "next_step": "go"}]
        output = Path(P.list_cycle_records(self.root)[0]["cycle_id"]) and None
        record = P.list_cycle_records(self.root)[0]
        cycle = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
        (cycle / "shards/frame-alternative").mkdir(parents=True, exist_ok=True)
        (cycle / "shards/frame").mkdir(parents=True, exist_ok=True)
        (cycle / "shards/frame/direction-brief.md").write_text(brief([DIRECT]), encoding="utf-8")
        (cycle / "shards/frame-alternative/direction-brief.md").write_text("## 8. 경로 조립 제안\n\nnone\n", encoding="utf-8")
        with mock.patch.object(R, "proposal_readiness", side_effect=AssertionError("a direct leg needs no probe")):
            W._ROUTE_MODULE = R
            self.addCleanup(setattr, W, "_ROUTE_MODULE", None)
            result = self.start()
        self.assertEqual(result["state"], "needs-interview")
        review = result["route_proposal_review"]
        self.assertEqual([row["node"] for row in review["proposals"]], F.FRAME_IDS)
        first, second = review["proposals"]
        self.assertEqual((first["reason"], first["display"]), ("valid", "proposal"))
        self.assertEqual(first["legs"][0]["shape"], "direct")
        self.assertEqual((second["proposal"], second["reason"], second["display"]),
                         (None, "block-missing", "proposal:none(block-missing)"))
        self.assertEqual((review["equal"], review["wording_differs"]), (False, False))
        self.assertIn("frame_downgrade", review)
        self.assertIsNone(review["frame_downgrade"])                  # nothing ran lower
        self.assertIn("route_proposals", result["next_step"])
        self.assertEqual(result["frame_interview"]["route_proposal_review"], review)
        self.assertEqual(len(self.calls), 2)                          # nothing launched to learn this

    def test_a_frame_leg_retried_one_profile_lower_is_named_in_the_result_and_the_review(self):
        self.start()
        self.ready = True
        self.steps = [{"state": "needs-interview", "required_action": "prepare-frame-question", "next_step": "go"}]
        record = P.list_cycle_records(self.root)[0]
        cycle = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record) / "artifacts"
        for node in F.FRAME_IDS:
            (cycle / "shards" / node).mkdir(parents=True, exist_ok=True)
            (cycle / "shards" / node / "direction-brief.md").write_text(brief([DIRECT]), encoding="utf-8")
        summary = [{"node": "frame", "original_profile": "top", "actual_profile": "deep", "cause": "capacity",
                    "attempt_id": "att-replacement", "original_attempt_id": "att-source"}]
        W._ROUTE_MODULE = R
        self.addCleanup(setattr, W, "_ROUTE_MODULE", None)
        with mock.patch.object(W, "_frame_downgrade_summary", return_value=summary) as summarize:
            result = self.start()
        self.assertEqual(result["state"], "needs-interview")
        self.assertEqual(result["frame_downgrade"], summary)                       # the frame summary
        self.assertEqual(result["route_proposal_review"]["frame_downgrade"], summary)   # the interview result
        self.assertTrue(all(call.args[0]["route_id"] == result["route_id"] for call in summarize.call_args_list))
        self.ready = True
        with mock.patch.object(W, "_frame_downgrade_summary", return_value=None):
            quiet = self.start()
        self.assertNotIn("frame_downgrade", quiet)                                  # no downgrade, no key
        self.assertIsNone(quiet["route_proposal_review"]["frame_downgrade"])

    def test_a_failed_frame_never_lets_the_surviving_brief_decide(self):
        self.start()
        self.ready = True

        def one_failed(jobs, aid, **kw):
            from dispatch_completion_join import CurrentDeliveryState
            failed = aid.endswith(self.failed_suffix)
            return CurrentDeliveryState(marker=None if failed else {"artifact": "/b.md"}, marker_digest="" if failed else "sha256:m",
                                        row_revision="1", row_digest="sha256:r", status="done",
                                        verdict="FAIL" if failed else "PASS", quiescent=True, owned_children=0,
                                        advanced=False, completion_proven=not failed)
        self.failed_suffix = self.calls[1][self.calls[1].index("--attempt-id") + 1][-4:]
        with mock.patch.object(W, "current_delivery_state", side_effect=one_failed):
            result = self.start()
        self.assertEqual((result["state"], result["reason"]), ("needs-attention", "frame-outcome-needs-inspection"))
        self.assertFalse(R.outcome_path(self.path).exists())
        self.assertFalse(list(self.root.rglob("route-decision.json")))
        self.assertNotIn("route_proposal_review", result)


# Inherited tests belong to the framed-route suite; only this suite's own run here.
for _name in dir(F.FramedStartTest):
    if _name.startswith("test_") and _name not in ReviewStartTest.__dict__:
        setattr(ReviewStartTest, _name, None)


class NonFramedClosedRouteTest(T.ProducerTestBase):
    """A closed route that is not the framed shape takes the path it always took: same keys, same values."""

    def test_a_closed_direct_route_replays_completed_with_its_old_fields_and_no_framed_or_plan_fields(self):
        with mock.patch.dict(os.environ, F.clean_environment(), clear=True):
            os.environ.update(AGENT_HOME=str(R.ROOT), AGENT_DISPATCH_JOBS=str(self.jobs))
            self.activate()
            route = R.compose_route(
                capability="autopilot-code", capability_mode=None, shape="direct", graph=None, slug="plain-direct",
                cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="plain-key",
                work_request={"text": "Do the small thing", "owner_harness": None})
            route_file = Path(L.admit_runtime_route(self.root, route).route_file)
            self.close(route, route_file)
            first = W.start_work(route, route_file, self.jobs)
            again = W.start_work(route, route_file, self.jobs)
        self.assertEqual(first, again)
        self.assertEqual(set(first), {"route_file", "route_id", "launches", "owner_started", "advisories",
                                      "resume_command", "state", "required_action", "outcome"})
        self.assertEqual((first["state"], first["required_action"]), ("completed", "advance-completed"))
        self.assertIs(first["outcome"]["terminal_gate_proven"], True)
        for new in ("next_leg", "route_decision", "record_file", "selected"):
            self.assertNotIn(new, first)


# --- Interruption: a real process is killed at a boundary, then the same settle runs in a new one ---

CRASH_EXIT = 77
BOUNDARIES = ("after-intent-save", "after-decision-write", "after-compiled-snapshot", "after-route-publish",
              "after-route-bind", "run-before-row", "run-after-row", "after-first-start",
              "after-first-start-receipt", "after-complete", "after-close", "after-manifest",
              "after-finalize")


def driver(context_path, crash_at):
    """One `_framed_settle` in this process with the fixture's controls; `crash_at` kills it there."""
    context = json.loads(Path(context_path).read_text(encoding="utf-8"))
    os.environ.clear()
    os.environ.update(context["env"])
    jobs, path = Path(context["jobs"]), Path(context["route_file"])
    route = json.loads(path.read_text(encoding="utf-8"))
    W._ROUTE_MODULE = R

    def hook(name):
        if crash_at == "after-manifest" and name == "finalize-crash-after-manifest":
            return True
        if name == crash_at:
            os._exit(CRASH_EXIT)
    W.FAULT_HOOK = hook

    def in_process_cli(_jobs, *argv):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if argv[0] == "complete":
            verified = R.verify_route(raw)
            node = next(n for n in verified["nodes"] if n["id"] == argv[argv.index("--node") + 1])
            R.complete_node(verified, node, node["id"], Path(argv[argv.index("--evidence") + 1]))
        else:
            R.close_route(R.verify_route(raw, allow_stale_registry=True), path,
                          summary=argv[argv.index("--summary") + 1], allow_unproven=False)
        return ""

    def run(command, **kwargs):
        if crash_at == "run-before-row":
            os._exit(CRASH_EXIT)
        value = lambda flag: command[command.index(flag) + 1]
        leg = json.loads(Path(value("--route-evidence")).read_text(encoding="utf-8"))
        meta = {"attempt_id": value("--attempt-id"), "parent_sid": "parent", "launch_started": "1",
                "worker_type": "owner", "dispatch_depth": "1", "route_id": leg["route_id"],
                "route_hash": leg["route_hash"], "owner_route_id": leg["route_id"],
                "owner_route_hash": leg["route_hash"], "parent_completion_delivery": "codex-managed-gateway"}
        with jobs.open("a") as stream:
            stream.write("now\topen\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        if crash_at == "run-after-row":
            os._exit(CRASH_EXIT)
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    interview = json.loads(Path(context["interview"]).read_text(encoding="utf-8"))
    answers = json.loads(Path(context["answers"]).read_text(encoding="utf-8"))
    evidence = {"tuples": [T.nested("claude", "codex")], "candidates": T.registered_headless()["candidates"]}
    with mock.patch("dispatch_contract.completion_marker_gate"), \
            mock.patch.object(W, "_route_cli", side_effect=in_process_cli), \
            mock.patch.object(R, "proposal_readiness", return_value=evidence), \
            mock.patch.object(W, "_recorded_interview", return_value=(interview, answers)), \
            mock.patch.object(W, "default_parent_session_id", return_value="parent"), \
            mock.patch.object(W, "join_selected_attempts", return_value={"state": "timeout", "children": []}), \
            mock.patch.object(W, "parent_next", return_value=("end-turn", "fixture", "")):
        result = W._framed_settle(route, path, jobs, {"route_id": route["route_id"]}, run=run)
    print("RESULT " + json.dumps(result, sort_keys=True))
    return 0


class InterruptionTest(StartBase):
    """A164-6: stop at each boundary of the transaction, run the same start again in a fresh process,
    and read the registry, route files, record and manifest back from disk."""

    def setUp(self):
        super().setUp()
        self.set_briefs(CODE_STAGED, CODE_STAGED)
        self.set_interview({"legs": [CODE_STAGED]})
        base = Path(self._tmp.name)
        (base / "interview.json").write_text(json.dumps(self.interview), encoding="utf-8")
        (base / "answers.json").write_text(json.dumps(self.answers), encoding="utf-8")
        self.context = base / "context.json"
        self.context.write_text(json.dumps({
            "env": dict(os.environ), "jobs": str(self.jobs), "route_file": str(self.path),
            "interview": str(base / "interview.json"), "answers": str(base / "answers.json")}), encoding="utf-8")

    def run_driver(self, crash_at=""):
        return subprocess.run([sys.executable, str(Path(__file__).resolve()), "--driver", str(self.context), crash_at],
                              text=True, capture_output=True, env=dict(os.environ))

    def result_of(self, done):
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        return json.loads(done.stdout.split("RESULT ", 1)[1])

    def assert_settled_once(self, result):
        self.assertEqual((result["state"], result["owner_started"]), ("running", True), result)
        self.assertEqual((len(self.leg_routes()), len(self.registry_rows())), (1, 1))
        record = self.record()
        self.assertEqual(record["first_leg"]["route"]["route_id"], self.leg_routes()[0].stem)
        self.assertEqual(record["first_leg"]["start_receipt"]["owner_attempt_id"], result["owner_attempt_id"])
        self.assertEqual(record["digest"], RP.decision_digest(record["decision"]))
        self.assertTrue(R.outcome_path(self.path).exists())
        outcome = json.loads(R.outcome_path(self.path).read_text(encoding="utf-8"))
        self.assertIs(outcome["terminal_gate_proven"], True)
        cycle = record["decision"]["frame_route"]["cycle_id"]
        self.assertEqual(P.read_cycle_record(self.root, cycle)["state"], "sealed")
        directory = self.jobs.parent / "completion" / self.route["route_id"]
        self.assertEqual([p.name for p in sorted(directory.glob("route-decision.[0-9]*.json"))],
                         ["route-decision.1.json"])             # the one marker of the terminal, never a second

    def test_an_uninterrupted_run_in_a_fresh_process_settles_once_and_a_repeat_changes_nothing(self):
        first = self.result_of(self.run_driver())
        self.assert_settled_once(first)
        before = (self.record_path().read_bytes(), self.registry_rows(), [p.name for p in self.leg_routes()])
        second = self.result_of(self.run_driver())
        self.assertEqual({k: v for k, v in second.items() if k != "route_decision"},
                         {k: v for k, v in first.items() if k != "route_decision"})
        self.assertEqual((self.record_path().read_bytes(), self.registry_rows(), [p.name for p in self.leg_routes()]), before)

    def test_a164_6_every_boundary_resumes_to_one_record_one_route_and_one_attempt(self):
        for boundary in BOUNDARIES:
            with self.subTest(boundary):
                self.tearDown()
                self.setUp()
                crashed = self.run_driver(boundary)
                self.assertNotEqual(crashed.returncode, 0, (boundary, crashed.stdout[-500:]))
                if boundary != "after-manifest":
                    self.assertEqual(crashed.returncode, CRASH_EXIT, (boundary, crashed.stderr[-2000:]))
                saved = self.record_path().read_bytes() if self.record_path().exists() else None
                rows_then = len(self.registry_rows())
                self.assertLessEqual(rows_then, 1)
                result = self.result_of(self.run_driver())
                self.assert_settled_once(result)
                if saved is not None:
                    self.assertEqual(json.loads(saved)["decision"], self.record()["decision"])
                    self.assertEqual(json.loads(saved)["digest"], self.record()["digest"])
                again = self.result_of(self.run_driver())
                self.assertEqual({k: v for k, v in again.items() if k != "route_decision"},
                                 {k: v for k, v in result.items() if k != "route_decision"})
                self.assertEqual((len(self.leg_routes()), len(self.registry_rows())), (1, 1))

    def test_the_decision_is_written_before_anything_else_and_a_crash_before_it_writes_none(self):
        crashed = self.run_driver("after-intent-save")
        self.assertEqual(crashed.returncode, CRASH_EXIT)
        self.assertFalse(self.record_path().exists())
        self.assertEqual((self.leg_routes(), self.registry_rows()), ([], []))
        crashed = self.run_driver("after-decision-write")
        self.assertEqual(crashed.returncode, CRASH_EXIT)
        record = self.record()
        self.assertNotIn("first_leg", record)
        self.assertEqual((self.leg_routes(), self.registry_rows()), ([], []))
        self.assertFalse(R.outcome_path(self.path).exists())

    def test_a_crash_between_publishing_the_route_and_binding_its_id_does_not_make_a_second_route(self):
        self.assertEqual(self.run_driver("after-route-publish").returncode, CRASH_EXIT)
        self.assertEqual(len(self.leg_routes()), 1)
        self.assertNotIn("route", self.record()["first_leg"])
        published = self.leg_routes()[0].read_bytes()
        self.assert_settled_once(self.result_of(self.run_driver()))
        self.assertEqual(self.leg_routes()[0].read_bytes(), published)           # never rewritten or recompiled

    def test_a_crash_after_the_row_was_written_but_before_the_receipt_reuses_that_attempt(self):
        self.assertEqual(self.run_driver("run-after-row").returncode, CRASH_EXIT)
        self.assertEqual(len(self.registry_rows()), 1)
        self.assertNotIn("start_receipt", self.record()["first_leg"])
        before = self.registry_rows()
        self.assert_settled_once(self.result_of(self.run_driver()))
        self.assertEqual(self.registry_rows(), before)



if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--driver":
        raise SystemExit(driver(sys.argv[2], sys.argv[3]))
    unittest.main()
