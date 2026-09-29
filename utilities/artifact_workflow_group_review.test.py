#!/usr/bin/env python3
"""Tests for `artifact_workflow_group_review.py`: target selection, response validation,
merge apply, judgement record, dry-run, seal trigger, and isolation.

Real activate/begin/close/finalize fixtures come from `artifact_producer.test.py`. The
model is never called: every test injects `invoke` or mocks the provider cascade.
"""
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_workflow_group_review as R  # noqa: E402
import artifact_workflow_groups as W  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "workflow_group_review_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
fixture = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixture)

PAST = 1_700_000_000.0
START = "=== CAMPAIGN DATA ===\n"
END = "\n=== END DATA ==="


def data_of(prompt):
    return json.loads(prompt.split(START, 1)[1].rsplit(END, 1)[0])


def target_ids(prompt):
    return [row["cycle_id"] for row in data_of(prompt)["targets"]]


def answer(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return lambda prompt: (text, "claude")


def none_for(prompt, reason="No shared subgoal is visible in the bodies."):
    return json.dumps({"decisions": [{"cycle_id": cid, "verdict": "none", "reason": reason}
                                     for cid in target_ids(prompt)], "new_groups": [], "relations": []})


class Recorder:
    """An `invoke` that records each prompt and answers `none` for its targets."""

    def __init__(self, reply=None):
        self.prompts = []
        self.reply = reply or none_for

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.reply(prompt), "claude"

    @property
    def targets(self):
        return [target_ids(prompt) for prompt in self.prompts]


def tree_snapshot(root):
    rows = []
    for path in sorted(Path(root).rglob("*")):
        meta = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else ""
        rows.append((str(path.relative_to(root)), meta.st_size, meta.st_mtime_ns, digest))
    return rows


class ReviewBase(fixture.ProducerTestBase):
    def setUp(self):
        self._attempt = os.environ.pop("AGENT_DISPATCH_ATTEMPT_ID", None)
        self.addCleanup(self._restore_attempt)
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(R.DISABLE_ENV, None)
        self.activate()

    def _restore_attempt(self):
        if self._attempt is not None:
            os.environ["AGENT_DISPATCH_ATTEMPT_ID"] = self._attempt

    # -- fixtures -------------------------------------------------------
    def start(self, key="camp", slug="cycle", now=None):
        route, route_file = self.route(slug=slug, campaign_key=key)
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                        intensity="direct", campaign_key=key, now=now)
        return route, route_file, begun

    def finish(self, route, route_file, begun, body=b"# Body\n\nwork\n", now=None):
        self.write_output(begun, "reports/final_report.md", body)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=begun["cycle_id"], primary="reports/final_report.md", now=now)

    def seal(self, key="camp", slug="cycle", body=b"# Body\n\nwork\n", now=None):
        route, route_file, begun = self.start(key, slug, now)
        self.finish(route, route_file, begun, body, now)
        return begun

    def ready(self, key="camp", slug="cycle"):
        """A closed route with output written; `P.finalize` is left to the caller."""
        route, route_file, begun = self.start(key, slug)
        self.write_output(begun, "reports/final_report.md", b"# Body\n\nwork\n")
        self.close(route, route_file)
        return begun

    def finalize(self, begun):
        P.finalize(self.root, cycle_id=begun["cycle_id"], primary="reports/final_report.md")
        return begun

    def path_of(self, begun):
        directory = P.cycle_dir(self.root, begun["campaign_id"], begun["cycle_id"])
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        rel = manifest["artifact_revisions"][0]["locator"]["path"]
        return (directory / rel).relative_to(self.root).as_posix()

    def declare(self, campaign_id, groups):
        plan = W.prepare(self.root, campaign_id, {"groups": groups})
        W.apply(self.root, plan)
        return plan["document"]["groups"]

    def declaration(self, campaign_id):
        return W._load_existing(W.declaration_path(self.root, campaign_id))

    def record(self):
        status, doc = R.read_record(self.root)
        self.assertEqual(status, "ok")
        return doc

    @staticmethod
    def report(result, index=0):
        return result["campaigns"][index]


class SelectionTest(ReviewBase):
    def test_enrollment_bounds_the_automatic_backlog(self):  # T1a
        old = self.seal(slug="old", now=PAST)
        trigger = self.seal(slug="trigger")
        first = Recorder()
        result = R.sweep(self.root, auto=True, cycles=[trigger["cycle_id"]], invoke=first)
        self.assertEqual(first.targets, [[trigger["cycle_id"]]])
        self.assertEqual(result["status"], "ok")
        self.assertTrue(self.record()["enrolled_at"])
        fresh = self.seal(slug="fresh")
        second = Recorder()
        R.sweep(self.root, auto=True, invoke=second)
        self.assertEqual(second.targets, [[fresh["cycle_id"]]])
        explicit = Recorder()
        R.sweep(self.root, cycles=[old["cycle_id"]], invoke=explicit)
        self.assertEqual(explicit.targets, [[old["cycle_id"]]])
        since = Recorder()
        third = self.seal(slug="third", now=PAST + 5)
        R.sweep(self.root, since=P._rfc3339(PAST + 1), invoke=since,
                dry_run=True)
        self.assertIn(third["cycle_id"], sum(since.targets, []))

    def test_no_enrollment_means_only_trigger_cycles(self):  # T1b
        for setup in ("no-file", "no-enrolled-at"):
            with self.subTest(setup):
                old = [self.seal(key=f"c-{setup}", slug=f"old{i}", now=PAST + i) for i in range(3)]
                trigger = self.seal(key=f"c-{setup}", slug="trigger")
                R.record_path(self.root).unlink(missing_ok=True)
                if setup == "no-enrolled-at":
                    self.assertTrue(R._update_record(self.root, lambda doc: None))
                    self.assertIsNone(self.record()["enrolled_at"])
                invoke = Recorder()
                with mock.patch.object(R, "ensure_enrolled", return_value=False):
                    R.sweep(self.root, auto=True, cycles=[trigger["cycle_id"]], invoke=invoke)
                self.assertEqual(invoke.targets, [[trigger["cycle_id"]]])
                self.assertFalse({o["cycle_id"] for o in old} & set(sum(invoke.targets, [])))
                R.record_path(self.root).unlink(missing_ok=True)

    def test_enrollment_survives_a_failed_review(self):  # T1c
        for reply in ("", "not json"):
            with self.subTest(reply=reply):
                R.record_path(self.root).unlink(missing_ok=True)
                cycle = self.seal(slug=f"c{len(reply)}")
                R.sweep(self.root, auto=True, cycles=[cycle["cycle_id"]], invoke=lambda prompt, r=reply: (r, None))
                self.assertTrue(self.record()["enrolled_at"])

    def test_failed_enrollment_still_limits_to_trigger(self):  # T1d
        for index in range(2):
            self.seal(slug=f"old{index}", now=PAST + index)
        trigger = self.seal(slug="trigger")
        invoke = Recorder()
        with mock.patch.object(adm, "_acquire_lock", side_effect=adm.AdmissionBusy("busy")):
            result = R.sweep(self.root, auto=True, cycles=[trigger["cycle_id"]], invoke=invoke)
        self.assertEqual(invoke.targets, [[trigger["cycle_id"]]])
        self.assertEqual(self.report(result)["record"], "unwritable")
        self.assertEqual(R.read_record(self.root)[0], "missing")

    def test_unreadable_or_foreign_record_stops_auto_and_keeps_pending(self):  # T1e
        trigger = self.seal(slug="trigger")
        valid = R._new_record(self.root)
        foreign = dict(valid, artifact_root_id="root_" + "e" * 32)
        for label, content in (("corrupt", b"{not json"), ("foreign", json.dumps(foreign).encode())):
            with self.subTest(label):
                path = R.record_path(self.root)
                path.write_bytes(content)
                invoke = Recorder()
                with redirect_stderr(io.StringIO()):
                    result = R.sweep(self.root, auto=True, cycles=[trigger["cycle_id"]], invoke=invoke)
                self.assertEqual(invoke.prompts, [])
                self.assertEqual(result["record"], "unwritable")
                self.assertEqual(path.read_bytes(), content)
                self.assertTrue((R.pending_dir(self.root) / trigger["cycle_id"]).exists())
                # An explicit run still judges, but leaves the bad file alone.
                explicit = Recorder()
                result = R.sweep(self.root, cycles=[trigger["cycle_id"]], invoke=explicit)
                self.assertEqual(explicit.targets, [[trigger["cycle_id"]]])
                self.assertEqual(self.report(result)["record"], "unwritable")
                self.assertEqual(path.read_bytes(), content)
                path.unlink()
                (R.pending_dir(self.root) / trigger["cycle_id"]).unlink()

    def test_existing_members_are_never_sent(self):  # T2
        a, b, c = (self.seal(slug=name) for name in ("a", "b", "c"))
        self.declare(a["campaign_id"], [{"title": "Existing", "members": [
            {"cycle_id": a["cycle_id"], "stage_label": "One"},
            {"cycle_id": b["cycle_id"], "stage_label": "Two"}], "relations": []}])
        invoke = Recorder()
        result = R.sweep(self.root, cycles=[a["cycle_id"], c["cycle_id"]], invoke=invoke)
        self.assertEqual(invoke.targets, [[c["cycle_id"]]])
        self.assertEqual(result["already_member"], [a["cycle_id"]])
        self.assertNotIn(a["cycle_id"], self.record()["cycles"])
        auto = Recorder()
        result = R.sweep(self.root, auto=True, cycles=[b["cycle_id"]], invoke=auto)
        self.assertEqual(auto.prompts, [])
        self.assertEqual(result["already_member"], [b["cycle_id"]])

    def test_retry_limit_and_reassessment_after_seal(self):  # T3
        cycle = self.seal(slug="retry")
        cid = cycle["cycle_id"]
        R.sweep(self.root, auto=True, cycles=[cid], invoke=answer(""))
        entry = self.record()["cycles"][cid]
        self.assertEqual((entry["verdict"], entry["failure_class"], entry["failures"], entry["hard_failures"]),
                         ("failed", "unavailable", 1, 0))
        R.sweep(self.root, auto=True, invoke=answer(""))
        self.assertEqual(self.record()["cycles"][cid]["hard_failures"], 0)
        self.assertEqual(self.record()["cycles"][cid]["failures"], 2)
        for expected in (1, 2, 3):
            invoke = Recorder(lambda prompt: "garbage")
            R.sweep(self.root, auto=True, invoke=invoke)
            self.assertEqual(len(invoke.prompts), 1)
            self.assertEqual(self.record()["cycles"][cid]["hard_failures"], expected)
        limited = Recorder()
        R.sweep(self.root, auto=True, invoke=limited)
        self.assertEqual(limited.prompts, [])
        explicit = Recorder()
        R.sweep(self.root, cycles=[cid], invoke=explicit)
        self.assertEqual(explicit.targets, [[cid]])
        self.assertEqual(self.record()["cycles"][cid]["verdict"], "unassigned")

    def test_open_unassigned_is_reassessed_once_after_sealing(self):  # T3
        seed = self.seal(slug="seed")
        R.sweep(self.root, auto=True, cycles=[seed["cycle_id"]], invoke=Recorder())
        route, route_file, begun = self.start(slug="opened")
        cid = begun["cycle_id"]
        R.sweep(self.root, cycles=[cid], include_open=True, invoke=Recorder())
        entry = self.record()["cycles"][cid]
        self.assertEqual((entry["verdict"], entry["cycle_state"]), ("unassigned", "open"))
        self.finish(route, route_file, begun)
        again = Recorder()
        R.sweep(self.root, auto=True, invoke=again)
        self.assertIn(cid, sum(again.targets, []))
        entry = self.record()["cycles"][cid]
        self.assertEqual((entry["verdict"], entry["cycle_state"]), ("unassigned", "sealed"))
        for _ in range(2):
            later = Recorder()
            R.sweep(self.root, auto=True, invoke=later)
            self.assertNotIn(cid, sum(later.targets, []))

    def test_limit_applies_before_the_campaign_split(self):
        ids = [self.seal(key=key, slug=f"{key}{i}", now=PAST + n)["cycle_id"]
               for n, (key, i) in enumerate([("one", 0), ("two", 0), ("one", 1)])]
        invoke = Recorder()
        R.sweep(self.root, cycles=ids, limit=2, invoke=invoke)
        self.assertEqual(sorted(sum(invoke.targets, [])), sorted(ids[:2]))
        self.assertEqual(len(invoke.prompts), 2)


class ValidationTest(ReviewBase):
    def setUp(self):
        super().setUp()
        self.a1 = self.seal(slug="a1")
        self.a2 = self.seal(slug="a2")
        self.context = self.seal(slug="context")
        self.target = self.seal(slug="target")
        self.other = self.seal(key="other", slug="elsewhere")
        self.camp = self.a1["campaign_id"]
        self.groups = self.declare(self.camp, [{"title": "Existing", "members": [
            {"cycle_id": self.a1["cycle_id"], "stage_label": "One"},
            {"cycle_id": self.a2["cycle_id"], "stage_label": "Two"}], "relations": []}])
        self.gid = self.groups[0]["group_id"]
        self.declared = W.declaration_path(self.root, self.camp).read_bytes()
        self.tid = self.target["cycle_id"]

    def run_reply(self, reply):
        return R.sweep(self.root, cycles=[self.tid], invoke=answer(reply))

    def decision(self, **extra):
        return {"cycle_id": self.tid, "verdict": "none", "reason": "No shared subgoal.", **extra}

    def new_group(self, members=None, title="Fresh workflow"):
        return {"key": "g1", "title": title, "members": members or [
            {"cycle_id": self.tid, "stage_label": "Start"},
            {"cycle_id": self.context["cycle_id"], "stage_label": "Earlier"}]}

    def new_reply(self, **group):
        return {"decisions": [{"cycle_id": self.tid, "verdict": "new", "new_group": "g1",
                               "stage_label": "Start", "reason": "Same subgoal."}],
                "new_groups": [self.new_group(**group)], "relations": []}

    def test_invalid_responses_reject_the_campaign_and_keep_the_declaration(self):  # T4
        bad_member = [{"cycle_id": self.tid, "stage_label": "Start"},
                      {"cycle_id": self.other["cycle_id"], "stage_label": "Elsewhere"}]
        single = [{"cycle_id": self.tid, "stage_label": "Start"}]
        duplicate = ('{"decisions":[{"cycle_id":"%s","verdict":"none","verdict":"none","reason":"r"}],'
                     '"new_groups":[],"relations":[]}' % self.tid)
        cases = {
            "unknown-group": {"decisions": [{"cycle_id": self.tid, "verdict": "join", "group_id": "wgrp_" + "0" * 32,
                                             "stage_label": "S", "reason": "r"}], "new_groups": [], "relations": []},
            "non-target": {"decisions": [self.decision(), self.decision(cycle_id=self.a1["cycle_id"])],
                           "new_groups": [], "relations": []},
            "missing": {"decisions": [], "new_groups": [], "relations": []},
            "other-campaign": self.new_reply(members=bad_member),
            "one-member": self.new_reply(members=single),
            "long-title": self.new_reply(title="t" * 121),
            "control-char": {"decisions": [self.decision(reason="bad\x01reason")], "new_groups": [], "relations": []},
            "duplicate-key": duplicate,
            "not-json": "here you go: {}",
            "unreferenced": {"decisions": [self.decision()], "new_groups": [self.new_group()], "relations": []},
            "unknown-kind": dict(self.new_reply(), relations=[{
                "from_cycle_id": self.tid, "to_cycle_id": self.context["cycle_id"], "kind": "sequel",
                "rationale": "r", "evidence_paths": [self.path_of(self.target)]}]),
        }
        for label, reply in cases.items():
            with self.subTest(label):
                report = self.report(self.run_reply(reply))
                self.assertEqual((report["status"], report["failure_class"]), ("failed", "invalid-response"))
                self.assertEqual(W.declaration_path(self.root, self.camp).read_bytes(), self.declared)
                self.assertEqual(self.record()["cycles"][self.tid]["failure_class"], "invalid-response")

    def test_long_stage_label_falls_back_and_newlines_are_normalized(self):
        reply = {"decisions": [{"cycle_id": self.tid, "verdict": "join", "group_id": self.gid,
                                "stage_label": "s" * 60, "reason": "First line\nsecond   line"}],
                 "new_groups": [], "relations": []}
        report = self.report(self.run_reply(reply))
        self.assertEqual(report["status"], "applied")
        entry = self.record()["cycles"][self.tid]
        self.assertEqual(entry["stage_label"],
                         W.stage_label_from_title(P.read_cycle_record(self.root, self.tid)["title"]))
        self.assertEqual(entry["reason"], "First line second line")

    def test_bad_evidence_drops_only_the_relation(self):  # T5
        trio = [self.seal(key="rel", slug=name) for name in ("x", "y", "z")]
        x, y, z = (item["cycle_id"] for item in trio)
        px, py, pz = (self.path_of(item) for item in trio)
        good = lambda a, b, pa, pb, kind="precedes": {
            "from_cycle_id": a, "to_cycle_id": b, "kind": kind, "rationale": "Used as the input.",
            "evidence_paths": [pa, pb]}
        reply = {
            "decisions": [{"cycle_id": cid, "verdict": "new", "new_group": "g1", "stage_label": name,
                           "reason": "Same subgoal."} for cid, name in zip((x, y, z), "xyz")],
            "new_groups": [{"key": "g1", "title": "Trio", "members": [
                {"cycle_id": cid, "stage_label": name} for cid, name in zip((x, y, z), "xyz")]}],
            "relations": [good(x, y, px, py), good(y, z, py, pz), good(z, x, pz, px),
                          good(x, z, px, "campaigns/nope/artifacts/missing.md"),
                          good(x, z, px, self.path_of(self.a1))],
        }
        report = self.report(R.sweep(self.root, cycles=[x, y, z], invoke=answer(reply)))
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["accepted_relations"], 2)
        codes = {row["index"]: row["code"] for row in report["dropped_relations"]}
        self.assertEqual(codes, {2: "relation-cycle", 3: "evidence-not-candidate", 4: "evidence-not-candidate"})
        group = self.declaration(trio[0]["campaign_id"])["groups"][0]
        self.assertEqual(len(group["members"]), 3)
        self.assertEqual([(r["from_cycle_id"], r["to_cycle_id"]) for r in group["relations"]], [(x, y), (y, z)])
        self.assertEqual([ref["path"] for ref in group["relations"][0]["evidence_refs"]], [px, py])
        self.assertEqual(W.verify(self.root, trio[0]["campaign_id"])["stale_evidence"], [])

    def test_merge_keeps_existing_declaration_and_bytes(self):  # T6
        existing = self.groups[0]
        first, second = existing["members"][0]["cycle_id"], existing["members"][1]["cycle_id"]
        plan = W.prepare(self.root, self.camp, {"groups": [{"group_id": self.gid, "title": "Existing", "members": [
            {"cycle_id": first, "stage_label": "One"}, {"cycle_id": second, "stage_label": "Two"}],
            "relations": [{"from_cycle_id": first, "to_cycle_id": second, "kind": "precedes",
                           "rationale": "Two uses one.", "evidence_refs": [
                               {"path": self.path_of(self.a1)}, {"path": self.path_of(self.a2)}]}]}]})
        W.apply(self.root, plan)
        before = self.declaration(self.camp)["groups"][0]
        extra = self.seal(slug="extra")
        protected = [P.campaign_dir(self.root, self.camp) / "campaign.json",
                     R.producer.producer_dir(self.root) / "cycles" / f"{self.tid}.json"]
        protected += list((P.campaign_dir(self.root, self.camp)).rglob("manifest.json"))
        protected += list((P.campaign_dir(self.root, self.camp)).rglob(".cycle.json"))
        protected = [path for path in protected if path.exists()]
        untouched = {path: path.read_bytes() for path in protected}
        reply = {
            "decisions": [
                {"cycle_id": self.tid, "verdict": "join", "group_id": self.gid, "stage_label": "Three",
                 "reason": "Continues the same work."},
                {"cycle_id": extra["cycle_id"], "verdict": "new", "new_group": "g1", "stage_label": "Other",
                 "reason": "Separate subgoal."}],
            "new_groups": [{"key": "g1", "title": "Second workflow", "members": [
                {"cycle_id": extra["cycle_id"], "stage_label": "Other"},
                {"cycle_id": self.context["cycle_id"], "stage_label": "Prep"}]}],
            "relations": [{"from_cycle_id": second, "to_cycle_id": self.tid, "kind": "followup",
                           "rationale": "Its result was continued.",
                           "evidence_paths": [self.path_of(self.a2), self.path_of(self.target)]}]}
        result = R.sweep(self.root, cycles=[self.tid, extra["cycle_id"]], invoke=answer(reply))
        self.assertEqual(self.report(result)["status"], "applied")
        groups = self.declaration(self.camp)["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["title"], before["title"])
        self.assertEqual(groups[0]["members"][:2], before["members"])
        self.assertEqual(groups[0]["members"][2], {"cycle_id": self.tid, "stage_label": "Three"})
        self.assertEqual(groups[0]["relations"][0], before["relations"][0])
        self.assertEqual(len(groups[0]["relations"]), 2)
        self.assertRegex(groups[1]["group_id"], r"wgrp_[0-9a-f]{32}\Z")
        self.assertEqual({m["cycle_id"] for m in groups[1]["members"]}, {extra["cycle_id"], self.context["cycle_id"]})
        self.assertEqual(W.verify(self.root, self.camp)["stale_evidence"], [])
        self.assertEqual({path: path.read_bytes() for path in protected if path.name != f"{self.tid}.json"},
                         {path: untouched[path] for path in protected if path.name != f"{self.tid}.json"})
        cycles = self.record()["cycles"]
        self.assertEqual((cycles[self.tid]["verdict"], cycles[self.tid]["group_id"]), ("joined", self.gid))
        self.assertEqual(cycles[extra["cycle_id"]]["group_id"], groups[1]["group_id"])
        self.assertEqual(cycles[self.context["cycle_id"]]["verdict"], "new-group")

    def test_none_records_the_reason_and_writes_no_declaration(self):  # T7
        other = self.seal(key="lonely", slug="lonely")
        reason = "Only the title looks alike;   the bodies share nothing."
        result = R.sweep(self.root, cycles=[other["cycle_id"]], invoke=answer(
            {"decisions": [{"cycle_id": other["cycle_id"], "verdict": "none", "reason": reason}],
             "new_groups": [], "relations": []}))
        self.assertEqual(self.report(result)["status"], "no-change")
        self.assertFalse(W.declaration_path(self.root, other["campaign_id"]).exists())
        entry = self.record()["cycles"][other["cycle_id"]]
        self.assertEqual((entry["verdict"], entry["profile"], entry["harness"], entry["mode"]),
                         ("unassigned", "light", "claude", "explicit"))
        self.assertEqual(entry["reason"], "Only the title looks alike; the bodies share nothing.")
        self.assertIsNone(re.search(r"opus|sonnet|haiku|gpt|fable", R.record_path(self.root).read_text(), re.I))

    def test_dry_run_writes_nothing_under_the_artifact_root(self):  # T8
        before = tree_snapshot(self.root)
        reply = self.new_reply()
        result = R.sweep(self.root, cycles=[self.tid], dry_run=True, invoke=answer(reply))
        self.assertEqual(tree_snapshot(self.root), before)
        report = self.report(result)
        self.assertEqual(report["status"], "dry-run")
        self.assertTrue(report["after_sha256"].startswith("sha256:"))
        self.assertEqual(report["decisions"][self.tid]["verdict"], "new")
        self.assertFalse(R.record_path(self.root).exists())
        self.assertFalse(R.lock_path(self.root).exists())
        self.assertFalse(R.pending_dir(self.root).exists())

    def test_apply_conflict_is_retried_once(self):
        real = W.apply
        calls = []

        def racing(root, plan):
            calls.append(1)
            if len(calls) == 1:
                raise W.WorkflowGroupError("declaration-preimage-conflict")
            return real(root, plan)

        with mock.patch.object(W, "apply", side_effect=racing):
            report = self.report(self.run_reply(self.new_reply()))
        self.assertEqual((report["status"], len(calls)), ("applied", 2))

    def test_apply_failure_is_a_hard_failure(self):
        with mock.patch.object(W, "apply", side_effect=W.WorkflowGroupError("evidence-total-size-limit")):
            report = self.report(self.run_reply(self.new_reply()))
        self.assertEqual((report["status"], report["failure_class"]), ("failed", "apply-failed"))
        self.assertEqual(self.record()["cycles"][self.tid]["hard_failures"], 1)


class TriggerTest(ReviewBase):
    def spawned(self):
        return mock.patch.object(R.subprocess, "Popen")

    def test_seal_succeeds_when_the_hook_or_spawn_fails(self):  # T9
        begun = self.ready(slug="hook-raises")
        with mock.patch.object(R, "launch_after_seal", side_effect=RuntimeError("boom")):
            self.finalize(begun)
        self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"])["state"], "sealed")
        begun = self.ready(slug="spawn-fails")
        with mock.patch.object(R, "in_test_process", return_value=False), \
                mock.patch.object(R.subprocess, "Popen", side_effect=OSError("no fork")) as popen:
            self.finalize(begun)
        self.assertTrue(popen.called)
        self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"])["state"], "sealed")
        directory = P.cycle_dir(self.root, begun["campaign_id"], begun["cycle_id"])
        self.assertTrue((directory / "manifest.json").is_file())

    def test_switch_off_disables_trigger_and_auto_sweep(self):  # T10
        cycle_env = {R.DISABLE_ENV: "off"}
        begun = self.ready(slug="switch-off")
        with mock.patch.dict(os.environ, cycle_env), mock.patch.object(R, "in_test_process", return_value=False), \
                self.spawned() as popen:
            self.finalize(begun)
            invoke = Recorder()
            self.assertEqual(R.sweep(self.root, auto=True, cycles=[begun["cycle_id"]], invoke=invoke),
                             {"status": "disabled"})
        popen.assert_not_called()
        self.assertEqual(invoke.prompts, [])
        explicit = Recorder()
        with mock.patch.dict(os.environ, cycle_env):
            R.sweep(self.root, cycles=[begun["cycle_id"]], invoke=explicit)
        self.assertEqual(explicit.targets, [[begun["cycle_id"]]])

    def test_test_process_never_spawns(self):  # T12
        begun = self.ready(slug="in-test")
        with self.spawned() as popen:
            self.finalize(begun)
        popen.assert_not_called()

    def test_busy_lock_leaves_a_pending_marker_and_the_next_sweep_takes_it(self):  # T11
        held = R._try_flock(self.root)
        self.assertIsNotNone(held)
        begun = self.ready(slug="busy")
        try:
            with mock.patch.object(R, "in_test_process", return_value=False), self.spawned() as popen, \
                    mock.patch.object(R, "select_targets") as select, mock.patch.object(R, "build_input") as build:
                self.finalize(begun)
            popen.assert_not_called()
            select.assert_not_called()
            build.assert_not_called()
            self.assertEqual(P.read_cycle_record(self.root, begun["cycle_id"])["state"], "sealed")
            self.assertTrue((R.pending_dir(self.root) / begun["cycle_id"]).exists())
            self.assertEqual(R.sweep(self.root, auto=True, cycles=["cyc_" + "1" * 32], invoke=Recorder())["status"],
                             "busy")
        finally:
            R._unlock(held)
        invoke = Recorder()
        R.sweep(self.root, auto=True, invoke=invoke)
        self.assertEqual(invoke.targets, [[begun["cycle_id"]]])
        self.assertEqual(R._pending_ids(self.root), [])

    def test_free_lock_spawns_one_detached_auto_sweep(self):  # T11
        begun = self.ready(slug="free")
        with mock.patch.object(R, "in_test_process", return_value=False), self.spawned() as popen:
            self.finalize(begun)
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1], str(Path(R.__file__).resolve()))
        self.assertEqual(argv[2:4], ["sweep", "--artifact-root"])
        self.assertEqual(Path(argv[4]).resolve(), self.root.resolve())
        self.assertEqual(argv[5:], ["--auto", "--cycle", begun["cycle_id"]])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIsNone(popen.call_args.kwargs.get("shell"))
        self.assertFalse((R.pending_dir(self.root) / begun["cycle_id"]).exists())

    def test_pending_is_only_touched_for_a_cutover_root(self):
        with mock.patch.object(R, "in_test_process", return_value=False), self.spawned() as popen:
            self.assertFalse(R.launch_after_seal(self.root / "nowhere", {"cycle_id": "cyc_" + "2" * 32}))
            self.assertFalse(R.launch_after_seal(self.root, {"cycle_id": "not-a-cycle"}))
        popen.assert_not_called()


class ModelCallTest(unittest.TestCase):
    def test_invoke_model_uses_the_light_profile_stdin_and_neutral_workdir(self):  # T12
        rt = R._refresh_title()
        prompt = "P" * 5000
        cascade = mock.Mock(return_value=("reply", 1))
        governor = mock.Mock()
        governor.acquire.return_value = "token"
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": state, "AGENT_DISPATCH_ATTEMPT_ID": "att-x",
                                             "AGENT_ROUTE_ID": "rt-x", "FLEET_TITLE_PROVIDER": "codex",
                                             "AGENT_ARTIFACT_WORKFLOW_GROUP_ID": "wg-x"}), \
                mock.patch.object(rt, "selected_providers", return_value=("claude", "opencode")) as select, \
                mock.patch.object(rt, "provider_model", return_value="model-x") as model, \
                mock.patch.object(rt, "_executable_available", return_value=True), \
                mock.patch.object(rt, "run_provider_cascade", cascade), \
                mock.patch.object(R, "_load_governor", return_value=governor):
            text, harness = R._invoke_model(prompt)
            workdir = R.neutral_workdir()
            self.assertTrue((workdir / ".opencode" / "agent" / "workflow-group-reviewer.md").is_file())
        self.assertEqual((text, harness), ("reply", "opencode"))
        select.assert_called_once_with(profile="light", pin_env=None)
        self.assertTrue(all(call.kwargs["profile"] == "light" for call in model.call_args_list))
        commands = cascade.call_args.args[0]
        self.assertEqual(len(commands), 2)
        claude_argv, claude_stdin, _out = commands[0]
        self.assertNotIn(prompt, claude_argv)
        self.assertEqual((claude_argv[:2], claude_stdin), (["claude", "-p"], prompt))
        opencode_argv = commands[1][0]
        self.assertEqual(opencode_argv[opencode_argv.index("--agent") + 1], "workflow-group-reviewer")
        self.assertEqual(cascade.call_args.kwargs["cwd"], str(workdir))
        env = cascade.call_args.kwargs["env"]
        self.assertEqual(env["AGENT_SESSION_ROLE"], "worker")
        self.assertNotIn("AGENT_DISPATCH_ATTEMPT_ID", env)
        self.assertNotIn("AGENT_ROUTE_ID", env)
        self.assertNotIn("AGENT_ARTIFACT_WORKFLOW_GROUP_ID", env)
        governor.acquire.assert_called_once()
        self.assertEqual(governor.acquire.call_args.args[1], "title")
        governor.release.assert_called_once()

    def test_no_provider_or_governor_failure_is_unavailable(self):
        rt = R._refresh_title()
        with mock.patch.object(rt, "selected_providers", return_value=()):
            self.assertEqual(R._invoke_model("p"), ("", None))
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}), \
                mock.patch.object(rt, "selected_providers", return_value=("claude",)), \
                mock.patch.object(rt, "provider_model", return_value="m"), \
                mock.patch.object(rt, "_executable_available", return_value=True), \
                mock.patch.object(R, "_load_governor", side_effect=ImportError("nope")):
            self.assertEqual(R._invoke_model("p"), ("", None))


class CliTest(unittest.TestCase):
    def test_help_and_argument_errors(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            R.main(["sweep", "--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--dry-run", out.getvalue())
        for argv in (["sweep"], ["sweep", "--artifact-root", "/nonexistent-root"],
                     ["sweep", "--artifact-root", "/tmp", "--since", "yesterday"]):
            with redirect_stderr(io.StringIO()):
                try:
                    code = R.main(argv)
                except SystemExit as exc:
                    code = exc.code
            self.assertEqual(code, 65, argv)


if __name__ == "__main__":
    unittest.main()
