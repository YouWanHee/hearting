#!/usr/bin/env python3
"""Cycle seal abolition (artifact-path-contract §45) end-to-end regressions.

One file for the whole §45 loop, one slice prefix per implementation step:
`test_a1_*` covers D-123 (writes, parents, one inclusion rule, binding).  Every
fixture runs on an isolated temporary artifact root; the real canonical root,
registry, and routes directory are never touched.
"""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_index  # noqa: E402
import artifact_producer as P  # noqa: E402
import dispatch_terminal_commit as T  # noqa: E402

_FX_SPEC = importlib.util.spec_from_file_location(
    "producer_fixtures_for_seal_abolition", Path(__file__).with_name("artifact_producer.test.py"))
FX = importlib.util.module_from_spec(_FX_SPEC)
_FX_SPEC.loader.exec_module(FX)
R = FX.R


class SealAbolitionBase(FX.ProducerTestBase):
    def setUp(self):
        # A worker's own AGENT_* variables must not leak into the fixture runtime.
        scrubbed = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("AGENT_")}
        self.addCleanup(os.environ.update, scrubbed)
        # Same switch tools/run-tests.py sets: route-hash and identity checks refuse, not warn.
        patcher = mock.patch.dict(os.environ, {"HEARTING_GATES": "on"})
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def finish(self, route, route_file, result, *, rel="plans/cycle/plan.md", data=b"plan body\n"):
        """Write one output, close the route, and finalize the cycle."""
        self.write_output(result, rel, data)
        self.close(route, route_file)
        return P.finalize(self.root, cycle_id=result["cycle_id"])

    def manifest(self, result):
        return json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))


class A1PolicyWritesParentsTest(SealAbolitionBase):
    # -- A28-1 -----------------------------------------------------------
    def test_a1_open_parent_child_first_and_cross_campaign(self):
        self.activate()
        parent_route, parent_file, parent = self.begin(campaign_key="causal-stream")
        child_route, child_file = self.route(slug="followup", parent_cycle_id=parent["cycle_id"])
        child = P.begin(self.root, route_file=child_file, capability="autopilot-code", intensity="direct")
        self.write_output(parent)
        self.write_output(child)
        self.close(child_route, child_file)
        # The child seals first: the parent is still open and not in the index.
        sealed_child = P.finalize(self.root, cycle_id=child["cycle_id"])
        self.assertEqual(sealed_child["status"], "sealed")
        index = adm.load_index(self.root)
        self.assertIn(child["cycle_id"], index.cycles)
        self.assertNotIn(parent["cycle_id"], index.cycles)
        self.assertEqual(P.read_cycle_record(self.root, child["cycle_id"])["parent_cycle_id"], parent["cycle_id"])
        # The parent seals afterwards, in either order.
        self.close(parent_route, parent_file)
        self.assertEqual(P.finalize(self.root, cycle_id=parent["cycle_id"])["status"], "sealed")
        index = adm.load_index(self.root)
        self.assertIn(parent["cycle_id"], index.cycles)
        self.assertIn(child["cycle_id"], index.cycles)

        # A parent in another campaign is just a parent: the child keeps its own campaign.
        other_file = self.route(slug="other-first", campaign_key="other-stream")[1]
        other = P.begin(self.root, route_file=other_file, capability="autopilot-code", intensity="direct")
        self.assertNotEqual(other["campaign_id"], parent["campaign_id"])
        cross_route, cross_file = self.route(
            slug="cross-campaign", campaign_key="other-stream", parent_cycle_id=parent["cycle_id"])
        cross = P.begin(self.root, route_file=cross_file, capability="autopilot-code", intensity="direct")
        self.assertEqual(cross["campaign_id"], other["campaign_id"])
        self.assertEqual(P.read_cycle_record(self.root, cross["cycle_id"])["parent_cycle_id"], parent["cycle_id"])
        self.write_output(cross)
        self.close(cross_route, cross_file)
        self.assertEqual(P.finalize(self.root, cycle_id=cross["cycle_id"])["status"], "sealed")
        self.assertIn(cross["cycle_id"], adm.load_index(self.root).cycles)

    def test_a1_parent_state_does_not_gate_a_child(self):
        self.activate()
        _, _, first = self.begin(campaign_key="stream-a")
        parent = P.read_cycle_record(self.root, first["cycle_id"])
        route_file = self.route(slug="lifecycle-child")[1]
        parent["state"] = "superseded"
        P._write_cycle_record(self.root, parent, exclusive=False)
        child = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                        parent_cycle_id=first["cycle_id"])
        self.assertEqual(P.read_cycle_record(self.root, child["cycle_id"])["parent_cycle_id"], first["cycle_id"])
        # What stays: the parent must be a producer record of this root, and a
        # superseded campaign still takes no new cycle from `begin`.
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=self.route(slug="orphan-child")[1], capability="autopilot-code",
                    intensity="direct", parent_cycle_id="cyc_" + "9" * 32)
        self.assertEqual(ctx.exception.code, "parent-cycle-not-joinable")
        campaign = P.read_campaign(self.root, first["campaign_id"])
        campaign["state"] = "superseded"
        P._write_campaign(self.root, campaign, exclusive=False)
        with self.assertRaises(P.ProducerError) as ctx:
            P.begin(self.root, route_file=self.route(slug="late-child")[1], capability="autopilot-code",
                    intensity="direct", parent_cycle_id=first["cycle_id"])
        self.assertEqual(ctx.exception.code, "campaign-not-active")

    def test_a1_index_parent_context_keeps_self_parent_and_orphan_judgments(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="index-parent")
        self.finish(route, route_file, result)
        document = self.manifest(result)
        child = json.loads(json.dumps(document))
        child["cycle"]["cycle_id"] = "cyc_" + "7" * 32
        child["cycle"]["parent_cycle_id"] = "cyc_" + "8" * 32
        empty = artifact_index.empty(document["artifact_root_id"])
        report = artifact_index.check(empty, child, idempotency_key="k", manifest_digest="d")
        self.assertIn("index-orphan-parent-cycle", {v.code for v in report.violations})
        known = artifact_index.check(
            empty, child, idempotency_key="k", manifest_digest="d",
            known_parent_cycle_ids=frozenset({child["cycle"]["parent_cycle_id"]}))
        self.assertNotIn("index-orphan-parent-cycle", {v.code for v in known.violations})
        self.assertNotIn("index-parent-cycle-campaign-mismatch", {v.code for v in known.violations})
        child["cycle"]["parent_cycle_id"] = child["cycle"]["cycle_id"]
        selfish = artifact_index.check(
            empty, child, idempotency_key="k", manifest_digest="d",
            known_parent_cycle_ids=frozenset({child["cycle"]["cycle_id"]}))
        self.assertIn("index-self-parent-cycle", {v.code for v in selfish.violations})

    # -- A28-2 -----------------------------------------------------------
    def test_a1_closed_cycle_write_and_output_allow(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="closed-writes")
        self.assertEqual(self.finish(route, route_file, result)["status"], "sealed")
        cycle_dir = Path(result["cycle_dir"])
        target = cycle_dir / "artifacts" / "plans" / "cycle" / "late-edit.md"
        verdict = P.check_write(self.root, target)
        self.assertEqual(verdict["verdict"], "allow", verdict)
        self.assertEqual(verdict["cycle_id"], result["cycle_id"])
        self.assertEqual(P.cycle_bucket(self.root, target), ("plans", result["cycle_id"]))
        output, layout = P.resolve_output_dir(self.root, "plans", cycle_dir_hint=str(cycle_dir))
        self.assertEqual((output, layout), (cycle_dir / "artifacts" / "plans", "cycle"))
        # The route's own lineage finds the closed cycle and may write into it again.
        record = P.read_cycle_record(self.root, result["cycle_id"])
        self.assertEqual(P.route_cycle_for(self.root, route)["cycle_id"], result["cycle_id"])
        self.assertTrue(P.cycle_route_admission(self.root, record, route).allow)
        self.assertEqual(
            P.require_cycle_output(self.root, target, cycle_id=result["cycle_id"], route_id=route["route_id"]),
            cycle_dir / "artifacts")
        # A review report is prepared against the closed cycle as well.
        review_output = cycle_dir / "artifacts" / "plans" / "review.md"
        binding = P.prepare_review_output_binding(
            self.root, cycle_id=result["cycle_id"], producer_id=record["producer_id"], attempt_id="att-late-review",
            review_output=review_output, capability="autopilot-code", unit="qa/code-review",
            worktree=str(Path(route["cwd"]).resolve()))
        self.assertEqual(binding["output_path"], str(review_output))
        # An abandoned cycle takes writes too: state never decides.
        abandoned_route, abandoned_file = self.route(slug="abandoned-writes", campaign_key="closed-writes")
        abandoned = P.begin(self.root, route_file=abandoned_file, capability="autopilot-code", intensity="direct")
        self.write_output(abandoned)
        P.finalize(self.root, cycle_id=abandoned["cycle_id"], state="abandoned",
                   abandon_reason="operator-decision", allow_open_route=True)
        late = Path(abandoned["cycle_dir"]) / "artifacts" / "plans" / "cycle" / "after-abandon.md"
        self.assertEqual(P.check_write(self.root, late)["verdict"], "allow")

    def test_a1_hidden_symlink_and_temporary_exclusion(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="inclusion-rule")
        outside = Path(self._tmp.name) / "outside-secret.txt"
        outside.write_text("must never be read\n", encoding="utf-8")
        artifacts = Path(result["cycle_dir"]) / "artifacts"
        kept = {
            "plans/cycle/plan.md": b"plan body\n",
            "plans/_internal/notes.md": b"support path stays\n",
            "plans/cycle/data.parquet": bytes(range(256)),
        }
        for rel, data in kept.items():
            self.write_output(result, rel, data)
        excluded = {
            ".pytest_cache/v/cache/lastfailed": b"{}",
            "plans/cycle/.hidden.md": b"hidden file",
            "plans/.git/config": b"[core]",
            "plans/cycle/__pycache__/mod.cpython-312.pyc": b"\x00pyc",
            "plans/cycle/stray.pyc": b"\x00pyc",
            "plans/cycle/draft.md.swp": b"swap",
            "plans/cycle/draft.md~": b"backup",
            "plans/cycle/.#draft.md": b"emacs lock",
            "plans/cycle/scratch.tmp": b"tmp",
            "plans/cycle/download.part": b"part",
        }
        for rel, data in excluded.items():
            self.write_output(result, rel, data)
        link = artifacts / "plans" / "cycle" / "outside-link.md"
        os.symlink(outside, link)
        dir_link = artifacts / "plans" / "cycle" / "linked-dir"
        os.symlink(outside.parent, dir_link)
        self.close(route, route_file)
        reads = []
        original_read_bytes = Path.read_bytes

        def tracking_read_bytes(path):
            reads.append(str(path))
            return original_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", tracking_read_bytes):
            sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed")
        manifest_text = (Path(result["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8")
        for rel in kept:
            self.assertIn("artifacts/" + rel, manifest_text, rel)
        for rel in excluded:
            self.assertNotIn(rel, manifest_text, rel)
        self.assertNotIn("outside-link.md", manifest_text)
        self.assertNotIn("linked-dir", manifest_text)
        self.assertEqual(
            sorted(sealed["excluded_hidden"]),
            sorted("artifacts/" + rel for rel in excluded))
        self.assertEqual(
            sorted(sealed["excluded_symlinks"]),
            ["artifacts/plans/cycle/linked-dir", "artifacts/plans/cycle/outside-link.md"])
        # A link is only lstat-ed: neither the link nor its target was read.
        self.assertEqual([p for p in reads if "outside" in p or "linked-dir" in p], [])
        self.assertEqual(outside.read_text(encoding="utf-8"), "must never be read\n")

    def test_a1_enumerate_applies_one_rule_without_flags(self):
        self.activate()
        _, _, result = self.begin(campaign_key="one-rule")
        self.write_output(result, "plans/cycle/plan.md")
        self.write_output(result, "plans/cycle/.cache/blob", b"x")
        os.symlink(Path(self._tmp.name), Path(result["cycle_dir"]) / "artifacts" / "plans" / "cycle" / "link")
        excluded, links = [], []
        rows, violations = P._enumerate_output(Path(result["cycle_dir"]), excluded=excluded,
                                                excluded_symlinks=links)
        self.assertEqual(violations, [])
        self.assertEqual([rel for rel, _data in rows], ["artifacts/plans/cycle/plan.md"])
        self.assertEqual(excluded, ["artifacts/plans/cycle/.cache/blob"])
        self.assertEqual(links, ["artifacts/plans/cycle/link"])

    def test_a1_location_and_runtime_guards_unchanged(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="guards")
        self.finish(route, route_file, result)
        cycle_dir = Path(result["cycle_dir"])
        # location contract: legacy top level, shared, campaign record, control files, unknown cycle.
        legacy = P.check_write(self.root, self.root / "plans" / "2026-10-01_x" / "plan.md")
        self.assertEqual((legacy["verdict"], legacy["reason"]), ("deny", "legacy-top-level-write-denied"))
        shared = self.root / "shared" / "spec" / ("ref_" + "1" * 32) / "revisions" / ("rrev_" + "2" * 32) / "prd.md"
        self.assertEqual(P.check_write(self.root, shared)["reason"], "shared-revision-immutable")
        self.assertEqual(P.check_write(self.root, cycle_dir.parent / "campaign.json")["reason"],
                         "campaign-record-machine-managed")
        self.assertEqual(P.check_write(self.root, cycle_dir / "manifest.json")["reason"], "outside-cycle-artifacts")
        self.assertEqual(P.check_write(self.root, cycle_dir / ".cycle.json")["verdict"], "deny")
        unknown = cycle_dir.parent / "2026-10-01_unknown" / "artifacts" / "plans" / "x.md"
        self.assertEqual(P.check_write(self.root, unknown)["verdict"], "deny")
        # containment: a path that climbs out of the cycle never classifies as a cycle write.
        escape = cycle_dir / "artifacts" / ".." / ".." / "escape.md"
        self.assertEqual(P.check_write(self.root, escape)["verdict"], "deny")
        # a symlinked component is followed to where it really points, never into the cycle.
        elsewhere = Path(self._tmp.name) / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, cycle_dir / "artifacts" / "plans" / "linked")
        through = P.check_write(self.root, cycle_dir / "artifacts" / "plans" / "linked" / "x.md")
        self.assertEqual((through["verdict"], through["reason"]), ("allow", "outside-artifact-root"), through)
        self.assertNotEqual(through.get("layout"), "cycle")
        # route hash seal: a tampered route file stays unreadable as a route.
        tampered, _tampered_file = self.route(slug="tamper-probe", campaign_key="guards")
        tampered["campaign_key"] = "tampered"
        with self.assertRaisesRegex(ValueError, "modified route hash"):
            R.verify_route(tampered)

    # -- binding ---------------------------------------------------------
    def test_a1_binding_identity_ignores_campaign_move_only(self):
        record = {"campaign_id": "camp_" + "1" * 32, "cycle_id": "cyc_" + "2" * 32,
                  "producer_id": "prod_" + "3" * 32, "route_id": "rt-fixture", "route_hash": "h" * 64,
                  "route_file": "/routes/rt-fixture.json"}
        stored = T.cycle_identity_digest(record)
        moved = {**record, "campaign_id": "camp_" + "9" * 32}
        self.assertNotEqual(T.cycle_identity_digest(moved), stored)
        self.assertTrue(T.cycle_identity_matches(moved, stored, campaign_id=record["campaign_id"]))
        self.assertTrue(T.cycle_identity_matches(record, stored, campaign_id=record["campaign_id"]))
        for field, value in (("cycle_id", "cyc_" + "8" * 32), ("producer_id", "prod_" + "8" * 32),
                             ("route_id", "rt-other"), ("route_hash", "e" * 64)):
            with self.subTest(field=field):
                changed = {**moved, field: value}
                self.assertFalse(T.cycle_identity_matches(changed, stored, campaign_id=record["campaign_id"]))


if __name__ == "__main__":
    unittest.main()
