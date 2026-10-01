#!/usr/bin/env python3
"""Cycle seal abolition (artifact-path-contract §45) end-to-end regressions.

One file for the whole §45 loop, one slice prefix per implementation step:
`test_a1_*` covers D-123 (writes, parents, one inclusion rule, binding);
`test_a2_*` covers the preserved copies, the next-document publisher and the
D-127 proofs (a finished cycle stays finished after its files change);
`test_b1_*` covers the bounded, lock-free scan of D-124 and the history calls of
D-125 (a refresh is one short publication, never a long lock).  Every
fixture runs on an isolated temporary artifact root; the real canonical root,
registry, and routes directory are never touched.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time
import tracemalloc
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission as adm  # noqa: E402
import artifact_campaign as CAMP  # noqa: E402
import artifact_index  # noqa: E402
import artifact_manifest as M  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_receipt as RCPT  # noqa: E402
import dispatch_lock_order  # noqa: E402
import dispatch_terminal_commit as T  # noqa: E402

_FX_SPEC = importlib.util.spec_from_file_location(
    "producer_fixtures_for_seal_abolition", Path(__file__).with_name("artifact_producer.test.py"))
FX = importlib.util.module_from_spec(_FX_SPEC)
_FX_SPEC.loader.exec_module(FX)
R = FX.R


def _load_sibling(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TERM = _load_sibling("terminal_fixtures_for_seal_abolition", "dispatch_terminal_commit.test.py")
INLINE = _load_sibling("inline_fixtures_for_seal_abolition", "inline_finish.test.py")


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


L = FX.L


def _row_bytes(rows, key, value):
    return [json.dumps(row, sort_keys=True) for row in rows if row.get(key) == value]


class A2SnapshotsAndRefreshTest(SealAbolitionBase):
    """§45 D-124: copies come first, a closed cycle publishes its next document."""

    def closed(self, campaign_key="a2-stream", files=None):
        self.activate()
        route, route_file = self.route(slug=campaign_key, campaign_key=campaign_key)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for rel, data in (files or {"plans/cycle/plan.md": b"plan body\n"}).items():
            self.write_output(result, rel, data)
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed", sealed)
        return route, route_file, result

    def closed_with_route(self, campaign_key, files=None):
        self.activate()
        route, route_file = self.route(slug=campaign_key, campaign_key=campaign_key)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for rel, data in (files or {"plans/cycle/plan.md": b"plan body\n"}).items():
            self.write_output(result, rel, data)
        self.close(route, route_file)
        self.assertEqual(P.finalize(self.root, cycle_id=result["cycle_id"])["status"], "sealed")
        return route, route, route_file, result

    def edit(self, result, rel, data):
        (Path(result["cycle_dir"]) / "artifacts" / rel).write_bytes(data)

    def snapshot_names(self, result):
        directory = L.manifest_snapshot_dir(self.root, result["cycle_id"])
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def test_a2_first_and_refinalize_snapshot_before_publish(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="a2-snapshots")
        self.write_output(result, "plans/cycle/plan.md", b"plan body\n")
        self.close(route, route_file)
        cycle_id = result["cycle_id"]
        seen = []
        exclusive, atomic = P._write_exclusive, P._write_atomic

        def spy_exclusive(path, data, mode=0o644):
            if Path(path).name == "manifest.json":
                copy = L.manifest_snapshot_path(self.root, cycle_id, json.loads(data)["manifest_revision_id"])
                seen.append(("first", copy.is_file() and copy.read_bytes() == data))
            return exclusive(path, data, mode)

        def spy_atomic(path, data, mode=0o644):
            if Path(path).name == "manifest.json":
                copy = L.manifest_snapshot_path(self.root, cycle_id, json.loads(data)["manifest_revision_id"])
                seen.append(("refinalize", copy.is_file() and copy.read_bytes() == data))
            return atomic(path, data, mode)

        with mock.patch.object(P, "_write_exclusive", spy_exclusive), mock.patch.object(P, "_write_atomic", spy_atomic):
            P.finalize(self.root, cycle_id=cycle_id)
            first_raw = (Path(result["cycle_dir"]) / "manifest.json").read_bytes()
            self.edit(result, "plans/cycle/plan.md", b"plan body, edited\n")
            again = P.finalize(self.root, cycle_id=cycle_id)
        self.assertTrue(again["refreshed"], again)
        self.assertEqual(seen, [("first", True), ("refinalize", True)])
        manifest = Path(result["cycle_dir"]) / "manifest.json"
        copies = {json.loads(p.read_bytes())["manifest_revision_id"]: p.read_bytes()
                  for p in L.manifest_snapshot_dir(self.root, cycle_id).iterdir()}
        self.assertEqual(len(copies), 2)
        self.assertIn(first_raw, copies.values())
        self.assertIn(manifest.read_bytes(), copies.values())


    def test_a2_refinalize_continuation_keeps_previous_terminal(self):
        _, route, route_file, result = self.closed_with_route("a2-continuation")
        cycle_id = result["cycle_id"]
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        first = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = P.read_cycle_record(self.root, cycle_id)
        binding_first = {key: record[key] for key in ("campaign_id", "cycle_id", "producer_id", "route_id", "route_hash")}
        binding_first["cycle_record_digest"] = T.cycle_identity_digest(record)
        # A plain refresh never touches the close: state, routes and the terminal record stay as written.
        self.edit(result, "plans/cycle/plan.md", b"plan body, edited\n")
        plain = P.finalize(self.root, cycle_id=cycle_id)
        self.assertTrue(plain["refreshed"])
        self.assertNotIn("terminal_added", plain)
        second = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("routes", "cycle"):
            self.assertEqual(first[key], second[key], key)
        # A route that continues the closed cycle writes into it and, while open, adds no terminal record.
        continuation = R.build_continuation_route(route, resume_from_node="inline", requested_boundary="inline",
                                                  reason="a2-continuation", artifact_root=self.root)
        continuation_file = R.canonical_route_path(self.root, continuation["route_id"])
        R.publish_continuation_route(continuation, route, continuation_file)
        self.assertTrue(R.bind_continuation_cycle(self.root, route, continuation)["bound"])
        self.write_output(result, "plans/cycle/after.md", b"written by the continuation\n")
        during = P.finalize(self.root, cycle_id=cycle_id)
        self.assertTrue(during["refreshed"])
        self.assertNotIn("terminal_added", during)
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["routes"], first["routes"])
        # Closed, it adds exactly its own terminal record -- even with no file changed -- and keeps the earlier one.
        self.close(continuation, continuation_file)
        closed = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(closed.get("terminal_added"), continuation["route_id"], closed)
        self.assertTrue(closed["refreshed"])
        third = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(third["routes"][:1], first["routes"])
        self.assertEqual([row["route_id"] for row in third["routes"]], [route["route_id"], continuation["route_id"]])
        self.assertNotEqual(third["routes"][1]["terminal_marker"], "pending")
        added = [e for e in third["events"][len(second["events"]):] if e["event_type"] == "route.terminal.recorded"]
        self.assertEqual([e["event_id"] for e in added], [third["routes"][1]["terminal_evidence_id"]])
        self.assertEqual(third["events"][:len(first["events"])], first["events"])
        self.assertEqual(third["cycle"], first["cycle"])
        self.assertTrue(M.validate_update(third, preserved=[first, second], previous=second).ok)
        # Each route's own proof holds, and the same leaf closing again adds nothing more.
        leaf_binding = dict(binding_first, route_id=continuation["route_id"], route_hash=continuation["route_hash"])
        for binding in (binding_first, leaf_binding):
            verified = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
            self.assertEqual(verified["status"], "already-sealed")
        again = P.finalize(self.root, cycle_id=cycle_id)
        self.assertFalse(again["refreshed"])
        self.assertNotIn("terminal_added", again)
        self.assertTrue(adm.verify_index(self.root).ok)

    def test_a2_exact_finalize_of_a_continuation_records_its_terminal(self):
        _, route, route_file, result = self.closed_with_route("a2-exact-continuation")
        cycle_id = result["cycle_id"]
        record = P.read_cycle_record(self.root, cycle_id)
        continuation = R.build_continuation_route(route, resume_from_node="inline", requested_boundary="inline",
                                                  reason="a2-exact", artifact_root=self.root)
        continuation_file = R.canonical_route_path(self.root, continuation["route_id"])
        R.publish_continuation_route(continuation, route, continuation_file)
        self.assertTrue(R.bind_continuation_cycle(self.root, route, continuation)["bound"])
        self.write_output(result, "plans/cycle/continued.md", b"continued\n")
        self.close(continuation, continuation_file)
        binding = {key: record[key] for key in ("campaign_id", "cycle_id", "producer_id")}
        binding.update(route_id=continuation["route_id"], route_hash=continuation["route_hash"],
                       cycle_record_digest=T.cycle_identity_digest(record))
        # The finish that closed the continuation proves it: the cycle's document gains its record first.
        done = P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(done["status"], "already-sealed")
        document = json.loads((Path(result["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([row["route_id"] for row in document["routes"]], [route["route_id"], continuation["route_id"]])
        self.assertIn("artifacts/plans/cycle/continued.md",
                      [row["locator"]["path"] for row in document["artifact_revisions"]])
        again = P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(again["manifest_digest"], done["manifest_digest"])

    def test_a2_refinalize_resumes_the_same_revision_after_a_crash(self):
        _, _, result = self.closed("a2-crash")
        cycle_id = result["cycle_id"]
        manifest = Path(result["cycle_dir"]) / "manifest.json"
        before = manifest.read_bytes()
        # Crash after the copy and the journal, before manifest.json is replaced.
        self.edit(result, "plans/cycle/plan.md", b"edited once\n")
        real_atomic = P._write_atomic

        def refuse_manifest(path, data, mode=0o644):
            if Path(path).name == "manifest.json":
                raise OSError("simulated crash before the swap")
            return real_atomic(path, data, mode)

        with mock.patch.object(P, "_write_atomic", refuse_manifest), self.assertRaises(OSError):
            P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(manifest.read_bytes(), before)
        planned = [json.loads(p.read_bytes())["manifest_revision_id"]
                   for p in L.manifest_snapshot_dir(self.root, cycle_id).iterdir()
                   if p.read_bytes() != before]
        self.assertEqual(len(planned), 1)
        resumed = P.finalize(self.root, cycle_id=cycle_id)
        self.assertFalse(resumed["refreshed"], resumed)
        published = json.loads(manifest.read_bytes())
        self.assertEqual(published["manifest_revision_id"], planned[0])
        self.assertFalse(P.journal_path(self.root, cycle_id).exists())
        self.assertEqual(adm.load_index(self.root).manifests[cycle_id]["manifest_digest"],
                         m_digest(published))
        self.assertEqual(P.read_cycle_record(self.root, cycle_id)["manifest_digest"], m_digest(published))
        # Crash after manifest.json was replaced, before the index and record followed.
        self.edit(result, "plans/cycle/plan.md", b"edited twice\n")
        with self.assertRaises(adm.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=cycle_id, crash_after_manifest=True)
        crashed = json.loads(manifest.read_bytes())
        self.assertNotEqual(adm.load_index(self.root).manifests[cycle_id]["manifest_digest"], m_digest(crashed))
        P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(json.loads(manifest.read_bytes())["manifest_revision_id"], crashed["manifest_revision_id"])
        self.assertEqual(adm.load_index(self.root).manifests[cycle_id]["manifest_digest"], m_digest(crashed))
        self.assertEqual(len(self.snapshot_names(result)), 3)
        self.assertTrue(adm.verify_index(self.root).ok)

    def test_a2_refinalize_publishes_next_revision_and_keeps_the_rest(self):
        files = {"plans/cycle/plan.md": b"plan body\n", "plans/cycle/notes.md": b"notes\n",
                 "plans/cycle/drop.md": b"to be dropped\n"}
        _, _, result = self.closed("a2-refresh", files)
        cycle_id = result["cycle_id"]
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        before = json.loads(manifest_path.read_text(encoding="utf-8"))
        record_before = P.read_cycle_record(self.root, cycle_id)
        artifacts = Path(result["cycle_dir"]) / "artifacts"
        self.edit(result, "plans/cycle/notes.md", b"notes, changed\n")
        (artifacts / "plans/cycle/drop.md").unlink()
        self.write_output(result, "plans/cycle/new.md", b"new file\n")
        self.write_output(result, "plans/cycle/.cache/blob", b"hidden")
        os.symlink(Path(self._tmp.name), artifacts / "plans/cycle/link")
        out = P.finalize(self.root, cycle_id=cycle_id)
        self.assertEqual(out["status"], "already-sealed")
        self.assertTrue(out["refreshed"])
        self.assertEqual(out["changes"], {"added": ["artifacts/plans/cycle/new.md"],
                                          "modified": ["artifacts/plans/cycle/notes.md"],
                                          "removed": ["artifacts/plans/cycle/drop.md"]})
        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        by_path = lambda doc: {r["locator"]["path"]: r for r in doc["artifact_revisions"]}
        old_rows, new_rows = by_path(before), by_path(after)
        plan = "artifacts/plans/cycle/plan.md"
        self.assertEqual(json.dumps(old_rows[plan], sort_keys=True), json.dumps(new_rows[plan], sort_keys=True))
        self.assertEqual(_row_bytes(before["artifacts"], "artifact_id", old_rows[plan]["artifact_id"]),
                         _row_bytes(after["artifacts"], "artifact_id", old_rows[plan]["artifact_id"]))
        notes = "artifacts/plans/cycle/notes.md"
        self.assertEqual(old_rows[notes]["artifact_id"], new_rows[notes]["artifact_id"])
        self.assertNotEqual(old_rows[notes]["artifact_revision_id"], new_rows[notes]["artifact_revision_id"])
        self.assertEqual(new_rows[notes]["revision_sequence"], 1)
        self.assertNotIn("artifacts/plans/cycle/drop.md", new_rows)
        self.assertNotIn(old_rows["artifacts/plans/cycle/drop.md"]["artifact_id"],
                         {a["artifact_id"] for a in after["artifacts"]})
        self.assertIn("artifacts/plans/cycle/new.md", new_rows)
        self.assertNotIn(".cache", json.dumps(after))
        self.assertNotIn("link", json.dumps(after["artifact_revisions"]))
        # Earlier events are untouched; only two revision records follow them.
        self.assertEqual(after["events"][:len(before["events"])], before["events"])
        tail = after["events"][len(before["events"]):]
        self.assertEqual([e["event_type"] for e in tail], ["artifact.revision.recorded"] * 2)
        # The close itself is not rewritten.
        for key in ("cycle", "routes", "manifest_id"):
            self.assertEqual(before[key], after[key], key)
        self.assertNotEqual(before["manifest_revision_id"], after["manifest_revision_id"])
        # The document reads only with its earlier copy beside it.
        self.assertFalse(M.validate(after).ok)
        self.assertFalse(M.validate_update(after, preserved=[], previous=before).ok)
        self.assertTrue(M.validate_update(after, preserved=[before], previous=before).ok)
        # Index, record and rebuild agree; the removed file's ID stays taken.
        index = adm.load_index(self.root)
        digest = m_digest(after)
        self.assertEqual(index.manifests[cycle_id]["manifest_digest"], digest)
        self.assertEqual(index.cycles[cycle_id]["manifest_digest"], digest)
        dropped_artifact = old_rows["artifacts/plans/cycle/drop.md"]["artifact_id"]
        self.assertIn(dropped_artifact, index.stable_ids)
        record = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual((record["state"], record["sealed_on"], record["manifest_digest"]),
                         ("sealed", record_before["sealed_on"], digest))
        self.assertEqual(len(self.snapshot_names(result)), 2)
        self.assertTrue(adm.verify_index(self.root).ok)
        rebuilt = adm.rebuild_index(self.root)
        self.assertIn(dropped_artifact, rebuilt.stable_ids)
        self.assertEqual(artifact_index.canonical_bytes(rebuilt), artifact_index.canonical_bytes(adm.load_index(self.root)))
        # Nothing changed, nothing written.
        watched = [manifest_path, P.cycle_record_path(self.root, cycle_id), adm._index_path(self.root),
                   *L.manifest_snapshot_dir(self.root, cycle_id).iterdir()]
        stamps = {p: p.stat().st_mtime_ns for p in watched}
        quiet = P.finalize(self.root, cycle_id=cycle_id)
        self.assertFalse(quiet["refreshed"])
        self.assertEqual(stamps, {p: p.stat().st_mtime_ns for p in watched})
        self.assertEqual(self.snapshot_names(result), sorted(p.name for p in stamps if p.parent.name == cycle_id))

    def test_a2_update_validator_keeps_earlier_records_and_resolves_only_this_cycle(self):
        files = {"plans/cycle/plan.md": b"plan body\n", "plans/cycle/extra.md": b"extra\n"}
        _, _, result = self.closed("a2-validator", files)
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        before = json.loads(manifest_path.read_text(encoding="utf-8"))
        (Path(result["cycle_dir"]) / "artifacts/plans/cycle/plan.md").unlink()
        (Path(result["cycle_dir"]) / "artifacts/plans/cycle/extra.md").unlink()
        self.write_output(result, "plans/cycle/other.md", b"other\n")
        P.finalize(self.root, cycle_id=result["cycle_id"])
        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Every file the first document had is gone, including the required primary.
        self.assertEqual([r["locator"]["path"] for r in after["artifact_revisions"]], ["artifacts/plans/cycle/other.md"])
        self.assertTrue(M.validate_update(after, preserved=[before], previous=before).ok)
        # A foreign cycle's copy resolves nothing.
        _, _, other = self.closed("a2-validator-other")
        foreign = json.loads((Path(other["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(M.validate_update(after, preserved=[foreign], previous=before).ok)
        # An earlier event may not change, vanish, or be followed by anything but a revision record.
        changed = json.loads(json.dumps(after))
        changed["events"][0]["payload"] = {"locator": "artifacts/other"}
        self.assertIn("update-earlier-events-changed",
                      {v.code for v in M.validate_update(changed, preserved=[before], previous=before).violations})
        missing = json.loads(json.dumps(after))
        del missing["events"][0]
        self.assertIn("update-earlier-events-changed",
                      {v.code for v in M.validate_update(missing, preserved=[before], previous=before).violations})
        stray = json.loads(json.dumps(after))
        stray["events"].append(dict(stray["events"][-1], event_id="evt_" + "9" * 32, stream_id="strm_" + "9" * 32,
                                    event_type="decision.recorded"))
        self.assertIn("update-event-type-not-allowed",
                      {v.code for v in M.validate_update(stray, preserved=[before], previous=before).violations})
        moved = json.loads(json.dumps(after))
        moved["cycle"]["state"] = "abandoned"
        self.assertIn("update-cycle-field-changed",
                      {v.code for v in M.validate_update(moved, preserved=[before], previous=before).violations})

    def test_a2_index_swaps_a_cycle_row_only_on_its_digest(self):
        _, _, result = self.closed("a2-index", {"plans/cycle/plan.md": b"plan body\n", "plans/cycle/drop.md": b"d\n"})
        _, _, other = self.closed("a2-index-other")
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        before = json.loads(manifest_path.read_text(encoding="utf-8"))
        (Path(result["cycle_dir"]) / "artifacts/plans/cycle/drop.md").unlink()
        self.edit(result, "plans/cycle/plan.md", b"changed\n")
        index_before = adm.load_index(self.root)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        cycle_id = result["cycle_id"]
        old_digest, new_digest = m_digest(before), m_digest(after)
        plain = artifact_index.check(index_before, after, idempotency_key=cycle_id, manifest_digest=new_digest)
        self.assertIn("index-cycle-id-duplicate", {v.code for v in plain.violations})
        wrong = artifact_index.check(index_before, after, idempotency_key=cycle_id, manifest_digest=new_digest,
                                     replaces_manifest_digest="sha256:" + "0" * 64)
        self.assertIn("manifest-revision-append-out-of-scope", {v.code for v in wrong.violations})
        right = artifact_index.check(index_before, after, idempotency_key=cycle_id, manifest_digest=new_digest,
                                     replaces_manifest_digest=old_digest)
        self.assertTrue(right.ok, right.violations)
        # A cycle never takes another cycle's ID, swap or not.
        other_doc = json.loads((Path(other["cycle_dir"]) / "manifest.json").read_text(encoding="utf-8"))
        stolen = json.loads(json.dumps(other_doc))
        stolen["artifacts"][0]["artifact_id"] = before["artifacts"][0]["artifact_id"]
        stolen["artifact_revisions"][0]["artifact_id"] = before["artifacts"][0]["artifact_id"]
        refused = artifact_index.check(index_before, stolen, idempotency_key=other["cycle_id"],
                                       manifest_digest="sha256:" + "1" * 64,
                                       replaces_manifest_digest=index_before.manifests[other["cycle_id"]]["manifest_digest"])
        self.assertIn("index-stable-id-duplicate", {v.code for v in refused.violations})
        # Applying keeps what the cycle owned: nothing is retired by dropping a row.
        applied = artifact_index.apply(index_before, after, cycle_path=index_before.cycles[cycle_id]["cycle_path"],
                                       manifest_digest=new_digest, idempotency_key=cycle_id)
        for stable_id in index_before.stable_ids:
            self.assertIn(stable_id, applied.stable_ids)
        self.assertEqual(applied.manifests[cycle_id]["manifest_digest"], new_digest)

    def test_a2_rebuild_index_knows_open_parents(self):
        self.activate()
        parent_route, parent_file, parent = self.begin(campaign_key="a2-open-parent")
        child_route, child_file = self.route(slug="a2-child", parent_cycle_id=parent["cycle_id"])
        child = P.begin(self.root, route_file=child_file, capability="autopilot-code", intensity="direct")
        self.write_output(parent)
        self.write_output(child)
        self.close(child_route, child_file)
        self.assertEqual(P.finalize(self.root, cycle_id=child["cycle_id"])["status"], "sealed")
        # The parent is still open and not in the index: a rebuild keeps the child.
        rebuilt = adm.rebuild_index(self.root)
        self.assertIn(child["cycle_id"], rebuilt.cycles)
        self.assertNotIn(parent["cycle_id"], rebuilt.cycles)
        self.assertTrue(adm.verify_index(self.root).ok)

    def test_a2_locked_index_read_once(self):
        # A first close reads the index once under the admission lock, and so does every
        # later refresh (§45 correction B: the new locked sections never re-read it).
        self.activate()
        route, route_file = self.route(slug="a2-first-read", campaign_key="a2-first-read")
        first = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(first)
        self.close(route, route_file)
        first_reads = []
        real_first = adm.load_index

        def counting_first(root):
            first_reads.append(1)
            return real_first(root)

        with mock.patch.object(adm, "load_index", counting_first):
            self.assertEqual(P.finalize(self.root, cycle_id=first["cycle_id"])["status"], "sealed")
        self.assertEqual(len(first_reads), 1, first_reads)
        _, _, result = self.closed("a2-index-read")
        self.edit(result, "plans/cycle/plan.md", b"edited\n")
        reads = []
        real_load = adm.load_index

        def counting(root):
            reads.append(1)
            return real_load(root)

        with mock.patch.object(adm, "load_index", counting):
            out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertTrue(out["refreshed"])
        self.assertLessEqual(len(reads), 1, reads)
        reads.clear()
        with mock.patch.object(adm, "load_index", counting):
            self.assertFalse(P.finalize(self.root, cycle_id=result["cycle_id"])["refreshed"])
        self.assertLessEqual(len(reads), 1, reads)


class A2ProofsAfterEditsTest(SealAbolitionBase):
    """§45 D-127: a closed cycle stays closed in every proof, whatever happens to its files."""

    def prepared(self, campaign_key="a2-proofs"):
        self.activate()
        route, route_file, result = self.begin(campaign_key=campaign_key)
        output = self.write_output(result, "plans/cycle/final_report.md", b"verified report\n")
        self.close(route, route_file)
        record = P.read_cycle_record(self.root, result["cycle_id"])
        binding = {key: record[key] for key in ("campaign_id", "cycle_id", "producer_id", "route_hash")}
        binding["cycle_record_digest"] = T.cycle_identity_digest(record)
        return route, route_file, result, output, binding

    def test_a2_verify_finalized_cycle_ignores_later_edits_and_names_the_recorded_revision(self):
        route, route_file, result, output, binding = self.prepared()
        cycle_id = result["cycle_id"]
        P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        first = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        # The report is edited, and a file appears: the proof does not look at them.
        output.write_bytes(b"verified report, edited later\n")
        self.write_output(result, "plans/cycle/later.md", b"later\n")
        manifest = Path(result["cycle_dir"]) / "manifest.json"
        before = manifest.read_bytes()
        self.assertEqual(P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding), first)
        self.assertEqual(P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)["status"],
                         "already-sealed")
        self.assertEqual(manifest.read_bytes(), before)
        # Closing again publishes the next document; the proof of the earlier revision stands.
        refreshed = P.finalize(self.root, cycle_id=cycle_id)
        self.assertTrue(refreshed["refreshed"])
        current = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(current["manifest_digest"], refreshed["manifest_digest"])
        recorded = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding,
                                            expected_manifest_digest=first["manifest_digest"])
        self.assertEqual((recorded["manifest_digest"], recorded["updated_since"], recorded["current_manifest_digest"]),
                         (first["manifest_digest"], True, refreshed["manifest_digest"]))
        # A cycle that moved to another campaign is the same cycle.
        moved = dict(binding, campaign_id="camp_" + "9" * 32)
        self.assertEqual(P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=moved)["status"],
                         "already-sealed")
        # What stays refused: another cycle's identity, a missing terminal record, a drifted index.
        with self.assertRaises(P.ProducerError) as ctx:
            P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=dict(binding, producer_id="prod_" + "8" * 32))
        self.assertEqual((ctx.exception.code, ctx.exception.detail), ("already-sealed-mismatch", "producer_id"))
        index = adm.load_index(self.root)
        payload = json.loads(json.dumps(artifact_index.to_payload(index)))
        payload["manifests"][cycle_id]["manifest_digest"] += "-foreign"
        adm._write_index(self.root, artifact_index.parse(payload))
        with self.assertRaises(P.ProducerError) as ctx:
            P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(ctx.exception.detail, "index")
        adm._write_index(self.root, index)
        outcome = R.outcome_path(route_file)
        saved = outcome.read_bytes()
        outcome.unlink()
        with self.assertRaises(P.ProducerError) as ctx:
            P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        self.assertEqual(ctx.exception.detail, "completion-evidence")
        outcome.write_bytes(saved)

    def test_a2_missing_snapshot_replays_terminal_only(self):
        route, route_file, result, output, binding = self.prepared("a2-no-copy")
        cycle_id = result["cycle_id"]
        P.finalize_exact_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        recorded = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding)
        output.unlink()
        self.write_output(result, "plans/cycle/other.md", b"other\n")
        refreshed = P.finalize(self.root, cycle_id=cycle_id)
        self.assertTrue(refreshed["refreshed"])
        # The copies are lost (a cycle edited before it was ever observed has none).
        import shutil
        shutil.rmtree(L.manifest_snapshot_dir(self.root, cycle_id))
        replay = P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding,
                                          expected_manifest_digest=recorded["manifest_digest"])
        self.assertEqual((replay["manifest_digest"], replay["updated_since"]), (recorded["manifest_digest"], True))
        # The terminal record itself is still held to its proof.
        outcome = R.outcome_path(route_file)
        outcome.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(P.ProducerError) as ctx:
            P.verify_finalized_cycle(self.root, cycle_id=cycle_id, expected_binding=binding,
                                     expected_manifest_digest=recorded["manifest_digest"])
        self.assertEqual(ctx.exception.code, "already-sealed-mismatch")

    def test_a2_receipt_accepts_preserved_revision(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="a2-receipt")
        self.write_output(result, "plans/cycle/plan.md", b"plan body\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        manifest_path = Path(result["cycle_dir"]) / "manifest.json"
        first = json.loads(manifest_path.read_text(encoding="utf-8"))

        def receipt_for(document, rel):
            revision = next(row for row in document["artifact_revisions"] if row["locator"]["path"] == rel)
            return RCPT.build_v3(
                completed_at="2026-10-01T00:00:00Z", repository_id=document["repository_id"],
                campaign_id=document["campaign"]["campaign_id"], cycle_id=document["cycle"]["cycle_id"],
                artifact_id=revision["artifact_id"], artifact_revision_id=revision["artifact_revision_id"],
                manifest_id=document["manifest_id"], manifest_revision_id=document["manifest_revision_id"])

        rel = "artifacts/plans/cycle/plan.md"
        early = receipt_for(first, rel)
        verdict = RCPT.resolve(self.root, early)
        self.assertEqual((verdict.state, verdict.detail), ("accepted", None), verdict)
        # The cycle publishes a later document: the earlier receipt was true when written.
        self.write_output(result, "plans/cycle/plan.md", b"plan body, edited\n")
        P.finalize(self.root, cycle_id=result["cycle_id"])
        second = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertNotEqual(first["manifest_revision_id"], second["manifest_revision_id"])
        old = RCPT.resolve(self.root, early)
        self.assertEqual((old.state, old.detail), ("accepted", "updated-since"), old)
        fresh = RCPT.resolve(self.root, receipt_for(second, rel))
        self.assertEqual((fresh.state, fresh.detail), ("accepted", None), fresh)
        # A revision no copy holds, and an artifact the revision never had, stay unresolved.
        unknown = dict(early, manifest_revision_id="mrev_" + "7" * 32)
        self.assertEqual(RCPT.resolve(self.root, unknown).state, "rejected")
        mismatched = dict(early, artifact_id="art_" + "6" * 32)
        refused = RCPT.resolve(self.root, mismatched)
        self.assertEqual(refused.state, "rejected")
        # Without its copy the old receipt has nothing to be read against.
        import shutil
        shutil.rmtree(L.manifest_snapshot_dir(self.root, result["cycle_id"]))
        self.assertEqual(RCPT.resolve(self.root, early).reason, "local-manifest-unregistered")


class A2EnvelopeReplayTest(TERM._TerminalCommitFixture):
    """The stored envelope is delivered as stored; the report it names may have moved on."""

    def seal_once(self):
        owner = TERM.owner_route_binding.OwnerRouteBinding(
            str(self.route_file), self.route["route_id"], self.route["route_hash"])
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            first = T.settle_terminal_commit(self.request(), T.TerminalCommitServices(
                close_route=lambda *a, **k: None, finalize_exact_cycle=lambda *a, **k: None,
                seal_envelope=T._default_seal_envelope))
        self.assertEqual(first.result, "completed")
        return owner

    def replay(self, owner):
        with mock.patch.object(T, "prove_terminal_authority", return_value=T.TerminalProof("proved")), \
             mock.patch.object(T, "validate_owner_route", return_value=owner), \
             mock.patch.object(T, "producer_lifecycle_applies", return_value=False), \
             mock.patch.object(T, "_route_module") as route_module:
            route_module.return_value.terminal_gate_observation.return_value = self.gates
            return T.settle_terminal_commit(self.request(), T.TerminalCommitServices())

    def test_a2_envelope_replay_after_report_edit_move_remove(self):
        owner = self.seal_once()
        slot = T._commit_state_path(self.request()).parent
        stored = (slot / "owner-envelope.txt").read_text()
        meta_before = (slot / "owner-envelope.json").read_bytes()
        unchanged = self.replay(owner)
        self.assertEqual((unchanged.result, unchanged.detail), ("completed", None))
        self.artifact.write_text("edited after the settlement\n", encoding="utf-8")
        edited = self.replay(owner)
        self.assertEqual((edited.result, edited.reason, edited.detail),
                         ("completed", None, "primary-changed-after-seal"))
        self.assertEqual(edited.envelope_text, stored)
        moved = self.artifact.with_name("moved-report.md")
        self.artifact.rename(moved)
        away = self.replay(owner)
        self.assertEqual((away.result, away.detail), ("completed", "primary-missing-after-seal"))
        self.assertEqual(away.envelope_text, stored)
        moved.unlink()
        gone = self.replay(owner)
        self.assertEqual((gone.result, gone.detail), ("completed", "primary-missing-after-seal"))
        self.assertEqual(gone.envelope_text, stored)
        # The envelope record is never rewritten with the new bytes.
        self.assertEqual((slot / "owner-envelope.txt").read_text(), stored)
        self.assertEqual((slot / "owner-envelope.json").read_bytes(), meta_before)
        # What stays refused: an envelope that is not the one sealed.
        (slot / "owner-envelope.txt").write_text("artifact: elsewhere\nverdict: PASS\nblocker: none\n")
        refused = self.replay(owner)
        self.assertEqual((refused.result, refused.detail), ("recoverable", "envelope-content-mismatch"))


class A2InlineFinishReplayTest(unittest.TestCase):
    def test_a2_inline_finished_replay_uses_recorded_revision(self):
        fixture = INLINE.PublicInlineFinishTest("test_public_finish_seals_and_exact_replay_returns_one_receipt")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        first = fixture.finish()
        self.assertEqual(first.returncode, 0, first.stderr)
        receipt = json.loads(first.stdout)
        cycle_id = fixture.cycle["cycle_id"]
        state_file = fixture.root / ".runtime/inline-finish/v1" / fixture.route["route_id"] / "finish.json"
        state_before = state_file.read_bytes()
        # After the finish the evidence file is edited and a new file appears; closing the
        # cycle again publishes the next manifest document.
        fixture.evidence.write_bytes(b"evidence, edited after the finish\n")
        (fixture.cycle_dir / "artifacts/documents/extra.md").write_bytes(b"extra\n")
        refreshed = INLINE.artifact_producer.finalize(fixture.root, cycle_id=cycle_id)
        self.assertTrue(refreshed["refreshed"], refreshed)
        self.assertNotEqual(refreshed["manifest_digest"], receipt["manifest_digest"])
        replay = fixture.finish()
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replayed = json.loads(replay.stdout)
        self.assertTrue(replayed["replay"])
        self.assertEqual(replayed["inline_finish_id"], receipt["inline_finish_id"])
        self.assertEqual(replayed["manifest_digest"], receipt["manifest_digest"])
        self.assertEqual(replayed["manifest_updated_since"], refreshed["manifest_digest"])
        # The evidence going away is no different, and the stored receipt is not rewritten.
        fixture.evidence.unlink()
        gone = fixture.finish()
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertEqual(json.loads(gone.stdout)["manifest_digest"], receipt["manifest_digest"])
        self.assertEqual(state_file.read_bytes(), state_before)


class A2SharedPublicationTest(SealAbolitionBase):
    _cycle = FX.ComponentSetPreservation._cycle
    _reference = FX.ComponentSetPreservation._reference
    _journals = FX.ComponentSetPreservation._journals

    def admit(self, cycle, generation, **kw):
        return P.admit_shared(self.root, cycle_id=cycle["cycle_id"], kind="spec",
                              source=f"gen{generation}", key="prd", **kw)

    def test_a2_shared_retry_survives_source_changes(self):
        cycle = self._cycle([["a"], ["a"]])
        first = self.admit(cycle, 0, base_revision="none")
        source = Path(cycle["cycle_dir"]) / "artifacts/spec/gen0"
        digest = P.read_cycle_record(self.root, cycle["cycle_id"])["manifest_digest"]
        latest = self._reference(first["shared_reference_id"])["latest_revision_id"]
        # The published PRD is edited, moved, then removed: the same call is the same publication.
        (source / "a/prd.md").write_text("edited after publication")
        for step in ("edited", "moved", "removed"):
            if step == "moved":
                source.rename(source.with_name("gen0-moved"))
            elif step == "removed":
                import shutil
                shutil.rmtree(source.with_name("gen0-moved"))
            retry = self.admit(cycle, 0, base_revision="none")
            self.assertEqual(retry["status"], "reused", step)
            self.assertEqual(retry["shared_reference_revision_id"], first["shared_reference_revision_id"], step)
            self.assertEqual(self._reference(first["shared_reference_id"])["latest_revision_id"], latest, step)
            self.assertEqual(self._journals(), [], step)
            self.assertEqual(P.read_cycle_record(self.root, cycle["cycle_id"])["manifest_digest"], digest, step)
        # Closing the cycle again records the changes; the retry still finds its publication.
        self.assertTrue(P.finalize(self.root, cycle_id=cycle["cycle_id"])["refreshed"])
        self.assertNotEqual(P.read_cycle_record(self.root, cycle["cycle_id"])["manifest_digest"], digest)
        again = self.admit(cycle, 0, base_revision="none")
        self.assertEqual((again["status"], again["shared_reference_revision_id"]),
                         ("reused", first["shared_reference_revision_id"]))
        # A new publication takes the source as it is now, and records the manifest it was taken from.
        (Path(cycle["cycle_dir"]) / "artifacts/spec/gen1/a/prd.md").write_text("gen1, edited before publishing")
        second = self.admit(cycle, 1, base_revision=first["shared_reference_revision_id"])
        self.assertEqual(second["status"], "admitted")
        self.assertEqual((Path(second["revision_dir"]) / "a/prd.md").read_text(), "gen1, edited before publishing")
        revision = json.loads((Path(second["revision_dir"]) / P.REVISION_RECORD_NAME).read_text())
        self.assertEqual(revision["source"]["manifest_digest"],
                         P.read_cycle_record(self.root, cycle["cycle_id"])["manifest_digest"])
        # A finished publication with a damaged record is still refused: its own bytes are checked.
        record_path = Path(first["revision_dir"]) / P.REVISION_RECORD_NAME
        record = json.loads(record_path.read_text())
        record["spec_base_revision_id"] = "rrev_" + "5" * 32
        record_path.write_text(json.dumps(record))
        with self.assertRaises(P.ProducerError):
            self.admit(cycle, 0, base_revision="none")

    def test_a2_uncommitted_shared_journal_still_checks_stable_input(self):
        cycle = self._cycle([["a"], ["a"]])
        first = self.admit(cycle, 0, base_revision="none")
        with mock.patch.object(P, "_commit_shared", side_effect=RuntimeError("crash before commit")):
            with self.assertRaises(RuntimeError):
                self.admit(cycle, 1, base_revision=first["shared_reference_revision_id"])
        self.assertEqual(len(self._journals()), 1)
        latest = self._reference(first["shared_reference_id"])["latest_revision_id"]
        source_file = Path(cycle["cycle_dir"]) / "artifacts/spec/gen1/a/prd.md"
        original = source_file.read_bytes()
        # The input the publication took no longer matches: it is not committed over a changed source.
        source_file.write_bytes(b"changed while the publication waited to commit")
        swept = P._recover_locked(self.root)
        self.assertTrue(swept["unresolved"], swept)
        self.assertEqual(self._reference(first["shared_reference_id"])["latest_revision_id"], latest)
        source_file.write_bytes(original)
        swept = P._recover_locked(self.root)
        self.assertEqual(swept["unresolved"], [])
        self.assertEqual(self._journals(), [])
        self.assertNotEqual(self._reference(first["shared_reference_id"])["latest_revision_id"], latest)


class A2CampaignCloseTest(SealAbolitionBase):
    def closed_campaign(self):
        self.activate()
        route, route_file, result = self.begin(campaign_key="a2-close")
        output = self.write_output(result)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=result["cycle_id"])
        return result, output, Path(result["cycle_dir"]).parent / "campaign.json"

    def test_a2_campaign_close_without_reason_after_edit(self):
        result, output, campaign = self.closed_campaign()
        record = json.loads(campaign.read_text())
        self.assertEqual(record["completion_criterion"]["statement"], CAMP.DEFAULT_COMPLETION_CRITERION)
        # A member's file was edited after its close, and a file added: neither stops the close.
        output.write_bytes(b"plan body, edited after the close\n")
        self.write_output(result, "plans/cycle/added.md", b"added\n")
        status = CAMP.status(self.root, campaign)
        self.assertNotIn("close_refusal", status)
        self.assertFalse(status["reason_required"])
        closed = CAMP.close(self.root, campaign)
        self.assertEqual(closed["status"], "satisfied")
        event = json.loads((campaign.parent / CAMP.EVENTS_DIR / "000001.json").read_text())
        self.assertEqual(event["payload"]["closure"]["reason"], CAMP.DEFAULT_COMPLETION_CRITERION)
        # The earlier fixed criterion sentence reads the same way.
        legacy = json.loads(campaign.read_text())
        CAMP.reopen(self.root, campaign, reason="legacy sentence check")
        legacy = json.loads(campaign.read_text())
        legacy["completion_criterion"] = {"statement": CAMP.LEGACY_DEFAULT_COMPLETION_CRITERION}
        P._write_campaign(self.root, legacy, exclusive=False)
        CAMP.close(self.root, campaign)
        event = json.loads((campaign.parent / CAMP.EVENTS_DIR / "000003.json").read_text())
        self.assertEqual(event["payload"]["closure"]["reason"], CAMP.LEGACY_DEFAULT_COMPLETION_CRITERION)
        # A reason given is recorded as given.
        CAMP.reopen(self.root, campaign, reason="again")
        CAMP.close(self.root, campaign, reason="explicit reason")
        event = json.loads((campaign.parent / CAMP.EVENTS_DIR / "000005.json").read_text())
        self.assertEqual(event["payload"]["closure"]["reason"], "explicit reason")

    def test_a2_open_route_is_the_only_close_refusal(self):
        result, output, campaign = self.closed_campaign()
        route, route_file = self.route(slug="a2-open-member", campaign_key="a2-close")
        member = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        status = CAMP.status(self.root, campaign)
        self.assertEqual(status["close_refusal"]["reason"], "campaign-cycle-provisional-active")
        # Once its route closes, the cycle that never closed is no longer in the way.
        self.close(route, route_file)
        status = CAMP.status(self.root, campaign)
        self.assertNotIn("close_refusal", status)
        rows = {row["cycle_id"]: row for row in status["cycles"]}
        self.assertEqual(rows[member["cycle_id"]]["state"], "open")
        self.assertIsNone(rows[member["cycle_id"]]["manifest_digest"])
        self.assertEqual(CAMP.close(self.root, campaign)["status"], "satisfied")


class FakeHistoryModule(types.ModuleType):
    """Same API shape as `artifact_history` (f0): make_event / publish_events_locked / publish_events."""

    class HistoryError(Exception):
        pass

    class HistoryPublishError(HistoryError):
        pass

    def __init__(self):
        super().__init__("artifact_history")
        self.made = []
        self.published = []
        self.fail_publish = False
        self.on_publish = None

    def make_event(self, **kwargs):
        self.made.append(dict(kwargs))
        for key in ("kind", "target_type", "target_id", "target_path", "operation", "field", "before",
                    "after", "reason", "transaction_id"):
            if key not in kwargs:
                raise self.HistoryError("event-invalid:" + key)
        return {"event_id": kwargs.get("event_id") or "hevt_" + "0" * 32, **kwargs}

    def publish_events_locked(self, root, events):
        if not adm.holds_lock(Path(root)):
            raise self.HistoryError("admission-lock-required")
        if self.on_publish is not None:
            self.on_publish(root, events)
        if self.fail_publish:
            raise self.HistoryPublishError("simulated publish failure")
        directory = Path(root) / ".runtime/artifact-producer/v1/history/2026-10"
        directory.mkdir(parents=True, exist_ok=True)
        for event in events:
            path = directory / (event["event_id"] + ".jsonl")
            raw = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
            if path.exists():
                if path.read_bytes() != raw:
                    raise self.HistoryPublishError("conflict")
                continue
            path.write_bytes(raw)
            self.published.append(event)
        if events:
            (directory.parent / "LATEST.json").write_text(
                json.dumps({"event_id": events[-1]["event_id"], "count": len(self.published)}), encoding="utf-8")
        return [event["event_id"] for event in events]

    def publish_events(self, root, events, **_kwargs):
        fd = adm._acquire_lock(Path(root), 5.0)
        try:
            return self.publish_events_locked(root, events)
        finally:
            adm._release_lock(Path(root), fd)


class B1RefreshBase(SealAbolitionBase):
    """§45 D-124/D-125: the bounded refresh of a closed cycle."""

    def setUp(self):
        super().setUp()
        self.history = FakeHistoryModule()
        patcher = mock.patch.dict(sys.modules, {"artifact_history": self.history})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The per-cycle interval has its own test; the others observe a cycle again at once.
        interval = mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL": "0"})
        interval.start()
        self.addCleanup(interval.stop)

    def closed(self, campaign_key="b1-stream", files=None, activate=True):
        if activate:
            self.activate()
        route, route_file = self.route(slug=campaign_key, campaign_key=campaign_key)
        result = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        for rel, data in (files or {"plans/cycle/plan.md": b"plan body\n"}).items():
            self.write_output(result, rel, data)
        self.close(route, route_file)
        sealed = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertEqual(sealed["status"], "sealed", sealed)
        # The close line has its own test; the refresh tests count only what a refresh sends.
        self.history.made.clear()
        self.history.published.clear()
        return result

    def edit(self, result, rel, data):
        (Path(result["cycle_dir"]) / "artifacts" / rel).write_bytes(data)

    def refresh(self, result, **kwargs):
        kwargs.setdefault("trigger", "turn-end")
        return P.refresh_cycle(self.root, result["cycle_id"], **kwargs)

    def manifest_path(self, result):
        return Path(result["cycle_dir"]) / "manifest.json"

    def snapshot_names(self, result):
        directory = L.manifest_snapshot_dir(self.root, result["cycle_id"])
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def tree_state(self, result):
        """(path -> (size, mtime_ns)) of everything a quiet refresh must leave alone."""
        cycle_id = result["cycle_id"]
        watched = [self.manifest_path(result), P.cycle_record_path(self.root, cycle_id), adm._index_path(self.root),
                   self.root / ".runtime/artifact-producer/v1/history",
                   L.manifest_snapshot_dir(self.root, cycle_id),
                   self.root / ".runtime/artifact-producer/v1/checkpoints"]
        state = {}
        for path in watched:
            for item in ([path] if path.is_file() else sorted(path.rglob("*")) if path.is_dir() else []):
                if item.is_file():
                    stat = item.stat()
                    state[str(item.relative_to(self.root))] = (stat.st_size, stat.st_mtime_ns)
        return state


class B1RefreshTest(B1RefreshBase):
    # -- A28-3 -----------------------------------------------------------
    def test_b1_taslp_25_plus26_modified4_and_noop(self):
        files = {f"plans/cycle/declared_{i:02d}.md": f"declared {i}\n".encode() for i in range(25)}
        result = self.closed("b1-taslp", files)
        cycle_id = result["cycle_id"]
        before = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        raw_before = self.manifest_path(result).read_bytes()
        record_before = P.read_cycle_record(self.root, cycle_id)
        for i in range(4):
            self.edit(result, f"plans/cycle/declared_{i:02d}.md", f"declared {i}, edited\n".encode())
        for i in range(26):
            self.write_output(result, f"plans/cycle/added_{i:02d}.md", f"added {i}\n".encode())
        out = self.refresh(result)
        self.assertEqual(out["status"], "emitted", out)
        after = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        self.assertEqual(len(after["artifact_revisions"]), 51)
        self.assertEqual(len(self.history.made), 30)
        self.assertEqual(len(self.history.published), 30)
        self.assertEqual(sorted(e["operation"] for e in self.history.published),
                         ["add"] * 26 + ["update"] * 4)
        kinds = {(e["kind"], e["target_type"], e["actor_by"]) for e in self.history.published}
        self.assertEqual(kinds, {("artifact", "artifact", "rule")})
        sample = next(e for e in self.history.published if e["operation"] == "update")
        self.assertTrue(sample["field"].startswith("artifacts/plans/cycle/declared_"))
        self.assertEqual(set(sample["before"]), {"digest", "bytes"})
        self.assertEqual(set(sample["after"]), {"digest", "bytes"})
        self.assertEqual(sample["reason"], "turn-end")
        # Rows that did not change are byte for byte the same; the close itself is not rewritten.
        old_rows = {r["locator"]["path"]: r for r in before["artifact_revisions"]}
        new_rows = {r["locator"]["path"]: r for r in after["artifact_revisions"]}
        for i in range(4, 25):
            rel = f"artifacts/plans/cycle/declared_{i:02d}.md"
            self.assertEqual(json.dumps(old_rows[rel], sort_keys=True), json.dumps(new_rows[rel], sort_keys=True))
            self.assertEqual(_row_bytes(before["artifacts"], "artifact_id", old_rows[rel]["artifact_id"]),
                             _row_bytes(after["artifacts"], "artifact_id", old_rows[rel]["artifact_id"]))
        for key in ("cycle", "routes", "manifest_id"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after["events"][:len(before["events"])], before["events"])
        record = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual((record["state"], record["cycle_state"], record["sealed_on"]),
                         ("sealed", "completed", record_before["sealed_on"]))
        # The earlier document stays as a preserved copy, byte for byte.
        copies = {p.name: p.read_bytes() for p in L.manifest_snapshot_dir(self.root, cycle_id).iterdir()}
        self.assertIn(raw_before, copies.values())
        self.assertEqual(len(copies), 2)
        self.assertTrue(adm.verify_index(self.root).ok)
        # The second refresh writes nothing at all.
        state = self.tree_state(result)
        made, published = len(self.history.made), len(self.history.published)
        quiet = self.refresh(result)
        self.assertEqual(quiet["status"], "unchanged", quiet)
        self.assertEqual(state, self.tree_state(result))
        self.assertEqual((made, published), (len(self.history.made), len(self.history.published)))

    def test_b1_noop_refresh_touches_nothing(self):
        result = self.closed("b1-noop", {"plans/cycle/a.md": b"a\n", "plans/cycle/b.md": b"b\n"})
        self.edit(result, "plans/cycle/b.md", b"b, edited\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        # A cycle named by the trigger leaves no bookkeeping behind when nothing changed.
        state = self.tree_state(result)
        for trigger in ("turn-end", "explicit", "supervisor-poll"):
            self.assertEqual(self.refresh(result, trigger=trigger)["status"], "unchanged")
        self.assertEqual(state, self.tree_state(result))
        latest = self.root / ".runtime/artifact-producer/v1/history/LATEST.json"
        self.assertTrue(latest.exists())
        stamp = latest.stat().st_mtime_ns
        self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertEqual(stamp, latest.stat().st_mtime_ns)
        # A change touches only the changed row.
        manifest = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        old_a = next(r for r in manifest["artifact_revisions"] if r["locator"]["path"].endswith("/a.md"))
        self.edit(result, "plans/cycle/b.md", b"b, edited again\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        again = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        new_a = next(r for r in again["artifact_revisions"] if r["locator"]["path"].endswith("/a.md"))
        self.assertEqual(json.dumps(old_a, sort_keys=True), json.dumps(new_a, sort_keys=True))
        self.assertEqual(len(self.snapshot_names(result)), 3)

    # -- correction B ----------------------------------------------------
    def test_b1_locked_index_read_once(self):
        result = self.closed("b1-index-read", {"plans/cycle/a.md": b"a\n"})
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        locked_reads, free_reads = [], []
        real_load = adm.load_index

        def counting(root):
            (locked_reads if adm.holds_lock(Path(root)) else free_reads).append(1)
            return real_load(root)

        with mock.patch.object(adm, "load_index", counting):
            self.assertEqual(self.refresh(result)["status"], "emitted")
        self.assertEqual(len(locked_reads), 1, locked_reads)
        locked_reads.clear()
        with mock.patch.object(adm, "load_index", counting):
            self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertLessEqual(len(locked_reads), 1, locked_reads)

    # -- lock choreography (D-124 "잠금") ----------------------------------
    def test_b1_scan_and_hash_hold_no_lock_and_publication_is_short(self):
        files = {f"plans/cycle/f{i}.md": f"f{i}\n".encode() for i in range(6)}
        result = self.closed("b1-locks", files)
        for i in range(3):
            self.edit(result, f"plans/cycle/f{i}.md", f"edited {i}\n".encode())
        seen = []
        real_stream = P._stream_file_facts

        def watching(path):
            seen.append((dispatch_lock_order.held(), adm.holds_lock(self.root)))
            fd = adm.try_acquire_lock(self.root)  # the admission lock is free to anyone while we hash
            self.assertIsNotNone(fd)
            adm._release_lock(self.root, fd)
            return real_stream(path)

        holds = []
        real_acquire, real_release = adm._acquire_lock, adm._release_lock
        started = {}

        def acquire(root, timeout, now=None):
            fd = real_acquire(root, timeout, now)
            started[fd] = time.monotonic()
            return fd

        def release(root, fd):
            holds.append(time.monotonic() - started.pop(fd, time.monotonic()))
            return real_release(root, fd)

        with mock.patch.object(P, "_stream_file_facts", watching), \
                mock.patch.object(adm, "_acquire_lock", acquire), mock.patch.object(adm, "_release_lock", release):
            self.assertEqual(self.refresh(result)["status"], "emitted")
        self.assertGreaterEqual(len(seen), 3)
        self.assertEqual({entry for entry in seen}, {((), False)})
        self.assertTrue(holds and max(holds) < 1.0, holds)

    def test_b1_history_goes_first_and_a_failed_history_leaves_the_manifest(self):
        result = self.closed("b1-history-first", {"plans/cycle/a.md": b"a\n"})
        raw = self.manifest_path(result).read_bytes()
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        order = []
        self.history.on_publish = lambda root, events: order.append(self.manifest_path(result).read_bytes() == raw)
        self.history.fail_publish = True
        out = self.refresh(result)
        self.assertEqual(out["status"], "skipped", out)
        self.assertEqual(out["reason"], "history-unavailable")
        self.assertEqual(self.manifest_path(result).read_bytes(), raw)
        self.assertEqual(len(self.snapshot_names(result)), 1)
        self.assertNotIn("history_pending", P.read_cycle_record(self.root, result["cycle_id"]))
        # The next trigger finds the same change again and publishes it.
        self.history.fail_publish = False
        self.assertEqual(self.refresh(result)["status"], "emitted")
        self.assertEqual(order, [True, True])
        self.assertNotEqual(self.manifest_path(result).read_bytes(), raw)
        self.assertEqual(len(self.history.published), 1)

    def test_b1_missing_recorder_keeps_lines_pending_and_the_next_trigger_delivers(self):
        result = self.closed("b1-pending", {"plans/cycle/a.md": b"a\n", "plans/cycle/b.md": b"b\n"})
        cycle_id = result["cycle_id"]
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        self.write_output(result, "plans/cycle/c.md", b"c\n")
        with mock.patch.dict(sys.modules, {"artifact_history": None}):  # import fails: not merged yet
            out = self.refresh(result)
            self.assertEqual(out["status"], "emitted", out)
            pending = P.read_cycle_record(self.root, cycle_id)["history_pending"]
            self.assertEqual(len(pending), 2)
            self.assertEqual({entry["operation"] for entry in pending}, {"add", "update"})
            for entry in pending:
                self.assertRegex(entry["event_id"], r"^hevt_[0-9a-f]{32}$")
                self.assertRegex(entry["transaction_id"], r"^htxn_[0-9a-f]{32}$")
            # Still no recorder: nothing is lost and nothing is rewritten.
            self.assertEqual(self.refresh(result)["status"], "unchanged")
            self.assertEqual(len(P.read_cycle_record(self.root, cycle_id)["history_pending"]), 2)
        # The recorder appears but fails: the lines stay.
        self.history.fail_publish = True
        self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertEqual(len(P.read_cycle_record(self.root, cycle_id)["history_pending"]), 2)
        self.history.fail_publish = False
        out = self.refresh(result)
        self.assertEqual(out["status"], "unchanged")
        self.assertEqual(len(self.history.published), 2)
        self.assertEqual({e["event_id"] for e in self.history.published}, {e["event_id"] for e in pending})
        self.assertNotIn("history_pending", P.read_cycle_record(self.root, cycle_id))
        state = self.tree_state(result)
        self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertEqual(state, self.tree_state(result))

    def test_b1_pending_survives_a_crash_after_the_manifest(self):
        result = self.closed("b1-crash-pending", {"plans/cycle/a.md": b"a\n"})
        cycle_id = result["cycle_id"]
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        with mock.patch.dict(sys.modules, {"artifact_history": None}):
            with self.assertRaises(adm.AdmissionRecoveryRequired):
                self.refresh(result, crash_after_manifest=True)
            P.recover(self.root)
            self.assertEqual(len(P.read_cycle_record(self.root, cycle_id).get("history_pending", [])), 1)
        self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertEqual(len(self.history.published), 1)

    def test_b1_first_close_records_one_line_or_leaves_it_pending(self):
        self.activate()
        route, route_file = self.route(slug="b1-close", campaign_key="b1-close")
        first = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(first)
        self.close(route, route_file)
        self.history.fail_publish = True  # a failing recorder never fails the close
        self.assertEqual(P.finalize(self.root, cycle_id=first["cycle_id"])["status"], "sealed")
        record = P.read_cycle_record(self.root, first["cycle_id"])
        self.assertEqual(len(record["history_pending"]), 1)
        line = record["history_pending"][0]
        self.assertEqual((line["kind"], line["target_type"], line["field"], line["operation"]),
                         ("lifecycle", "cycle", "state", "update"))
        document = json.loads(self.manifest_path(first).read_text(encoding="utf-8"))
        value = line["after"]["value"]
        self.assertEqual(set(value), {"state", "manifest_digest", "revision_id", "files", "excluded"})
        self.assertEqual((value["state"], value["manifest_digest"], value["revision_id"], value["files"]),
                         ("completed", m_digest(document), document["manifest_revision_id"], 1))
        self.history.fail_publish = False
        self.assertEqual(self.refresh(first)["status"], "unchanged")
        self.assertEqual([e["field"] for e in self.history.published], ["state"])
        self.assertNotIn("history_pending", P.read_cycle_record(self.root, first["cycle_id"]))
        # With the recorder present the close line is published at once.
        route2, route_file2 = self.route(slug="b1-close-two", campaign_key="b1-close")
        second = P.begin(self.root, route_file=route_file2, capability="autopilot-code", intensity="direct")
        self.write_output(second)
        self.close(route2, route_file2)
        P.finalize(self.root, cycle_id=second["cycle_id"])
        self.assertEqual(len(self.history.published), 2)
        self.assertNotIn("history_pending", P.read_cycle_record(self.root, second["cycle_id"]))

    # -- A28-4 -----------------------------------------------------------
    def test_b1_delete_everything_and_edit_twice_through_the_bounded_path(self):
        files = {"plans/cycle/plan.md": b"plan\n", "plans/cycle/notes.md": b"notes\n"}
        result = self.closed("b1-delete", files)
        cycle_id = result["cycle_id"]
        artifacts = Path(result["cycle_dir"]) / "artifacts"
        self.edit(result, "plans/cycle/notes.md", b"notes 1\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        self.edit(result, "plans/cycle/notes.md", b"notes 2\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        self.assertEqual(len(self.snapshot_names(result)), 3)
        before = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        required = next(r for r in before["artifact_revisions"] if r["locator"]["path"].endswith("/plan.md"))
        (artifacts / "plans/cycle/plan.md").unlink()
        out = self.refresh(result)
        self.assertEqual(out["changes"]["removed"], ["artifacts/plans/cycle/plan.md"])
        self.assertEqual([e["operation"] for e in self.history.published[-1:]], ["delete"])
        self.assertEqual(self.history.published[-1]["after"], {"value": None})
        (artifacts / "plans/cycle/notes.md").unlink()
        self.write_output(result, "plans/cycle/.cache/blob", b"hidden")
        self.assertEqual(self.refresh(result)["status"], "emitted")
        after = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        self.assertEqual(after["artifact_revisions"], [])
        self.assertEqual(after["events"][:len(before["events"])], before["events"])
        self.assertNotIn(".cache", json.dumps(after))
        self.assertNotIn(".cache", json.dumps(self.history.published))
        earlier = [json.loads(raw) for raw in
                   (p.read_text(encoding="utf-8") for p in L.manifest_snapshot_dir(self.root, cycle_id).iterdir())]
        self.assertTrue(M.validate_update(after, preserved=earlier, previous=before).ok)
        index = adm.load_index(self.root)
        self.assertIn(required["artifact_id"], index.stable_ids)
        rebuilt = adm.rebuild_index(self.root)
        self.assertEqual(artifact_index.canonical_bytes(rebuilt), artifact_index.canonical_bytes(adm.load_index(self.root)))
        self.assertIn(required["artifact_id"], rebuilt.stable_ids)
        # An empty file list is still a closed cycle; a later file is simply added.
        self.write_output(result, "plans/cycle/again.md", b"again\n")
        self.assertEqual(self.refresh(result)["status"], "emitted")

    # -- A28-9 -----------------------------------------------------------
    def test_b1_cycle_closed_by_an_older_release_refreshes_without_preparation(self):
        result = self.closed("b1-old-release", {"plans/cycle/a.md": b"a\n"})
        raw = self.manifest_path(result).read_bytes()
        shutil.rmtree(L.manifest_snapshot_dir(self.root, result["cycle_id"]))
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        out = self.refresh(result)
        self.assertEqual(out["status"], "emitted", out)
        copies = {p.name: p.read_bytes() for p in L.manifest_snapshot_dir(self.root, result["cycle_id"]).iterdir()}
        self.assertIn(raw, copies.values())
        self.assertEqual(len(copies), 2)
        self.assertTrue(adm.verify_index(self.root).ok)

    def test_b1_a_file_changed_after_the_scan_skips_this_publication_only(self):
        result = self.closed("b1-recheck", {"plans/cycle/a.md": b"a\n"})
        raw = self.manifest_path(result).read_bytes()
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        real_lock = P._refresh_lock

        def lock_after_a_write(root, cycle_id, *, timeout):
            self.edit(result, "plans/cycle/a.md", b"a, edited again while we scanned\n")
            return real_lock(root, cycle_id, timeout=timeout)

        with mock.patch.object(P, "_refresh_lock", lock_after_a_write):
            out = self.refresh(result)
        self.assertEqual((out["status"], out["reason"]), ("skipped", "superseded"))
        self.assertEqual(self.manifest_path(result).read_bytes(), raw)
        self.assertEqual(self.history.published, [])
        self.assertEqual(self.refresh(result)["status"], "emitted")

    def test_b1_a_busy_refresh_lock_defers_without_an_error(self):
        result = self.closed("b1-busy", {"plans/cycle/a.md": b"a\n"})
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        with P._refresh_lock(self.root, result["cycle_id"], timeout=0.0):
            out = self.refresh(result)
        self.assertEqual((out["status"], out["reason"]), ("skipped", "busy"))
        self.assertEqual(self.refresh(result)["status"], "emitted")

    def test_b1_unknown_and_open_cycles_are_skipped_not_failed(self):
        self.activate()
        route, route_file, opened = self.begin(campaign_key="b1-open")
        self.assertEqual(self.refresh(opened)["reason"], "cycle-not-closed")
        self.assertEqual(P.refresh_cycle(self.root, "cyc_" + "5" * 32)["reason"], "cycle-unknown")

    # -- budget (D-124 "비용 예산") ----------------------------------------
    def test_b1_walk_budget_leaves_a_cursor_and_never_reads_the_rest_as_deleted(self):
        files = {f"plans/cycle/f{i:02d}.md": f"f{i}\n".encode() for i in range(30)}
        result = self.closed("b1-walk", files)
        for i in range(30):
            self.edit(result, f"plans/cycle/f{i:02d}.md", f"f{i} edited\n".encode())
        (Path(result["cycle_dir"]) / "artifacts/plans/cycle/f29.md").unlink()
        runs, removed = 0, []
        while True:
            runs += 1
            out = self.refresh(result, budget=P.RefreshBudget(max_walk_entries=12))
            removed.extend(out.get("changes", {}).get("removed", []))
            if out.get("complete"):
                break
            self.assertEqual(out["status"], "emitted", out)
            self.assertIsNotNone(out["cursor"])
            self.assertLess(runs, 12)
        self.assertGreaterEqual(runs, 3)
        # Only the file whose absence was looked at directly leaves the rows, once.
        self.assertEqual(removed, ["artifacts/plans/cycle/f29.md"])
        document = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        rows = {r["locator"]["path"]: r for r in document["artifact_revisions"]}
        self.assertEqual(len(rows), 29)
        for i in range(29):
            data = f"f{i} edited\n".encode()
            self.assertEqual(rows[f"artifacts/plans/cycle/f{i:02d}.md"]["content_digest"],
                             "sha256:" + hashlib.sha256(data).hexdigest())
        self.assertTrue(adm.verify_index(self.root).ok)
        # Everything seen, nothing left to do: a full pass is quiet again.
        self.assertEqual(self.refresh(result, budget=P.RefreshBudget(max_walk_entries=12))["status"], "unchanged")

    def test_b1_hash_budget_stops_between_files_and_a_changed_file_is_always_finished(self):
        files = {f"plans/cycle/f{i}.bin": bytes([i]) * (600 * 1024) for i in range(4)}
        result = self.closed("b1-hash", files)
        for i in range(4):
            self.edit(result, f"plans/cycle/f{i}.bin", bytes([i + 10]) * (600 * 1024))
        out = self.refresh(result, budget=P.RefreshBudget(max_hash_bytes=1024 * 1024))
        self.assertFalse(out["complete"], out)
        self.assertLessEqual(len(out["changes"]["modified"]), 2)
        self.assertGreaterEqual(len(out["changes"]["modified"]), 1)
        for _ in range(6):
            if self.refresh(result, budget=P.RefreshBudget(max_hash_bytes=1024 * 1024)).get("complete"):
                break
        document = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        for row in document["artifact_revisions"]:
            index = int(row["locator"]["path"].split("/f")[-1].split(".")[0])
            self.assertEqual(row["content_digest"],
                             "sha256:" + hashlib.sha256(bytes([index + 10]) * (600 * 1024)).hexdigest())

    def test_b1_a_300_mib_file_is_reflected_in_one_run_by_streaming(self):
        result = self.closed("b1-300mib", {"plans/cycle/small.md": b"small\n", "plans/cycle/model.bin": b"x\n"})
        big = Path(result["cycle_dir"]) / "artifacts/plans/cycle/model.bin"
        size = 300 * 1024 * 1024
        with open(big, "wb") as handle:
            handle.truncate(size)
        expected = hashlib.sha256()
        zeros = bytes(1024 * 1024)
        for _ in range(300):
            expected.update(zeros)
        tracemalloc.start()
        try:
            out = self.refresh(result)  # the default budget is 256 MiB: smaller than this one file
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(out["status"], "emitted", out)
        self.assertLess(peak, 64 * 1024 * 1024, peak)
        document = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        row = next(r for r in document["artifact_revisions"] if r["locator"]["path"].endswith("model.bin"))
        self.assertEqual((row["content_digest"], row["byte_size"]), ("sha256:" + expected.hexdigest(), size))

    def test_b1_15300_temporary_files_finish_inside_the_budget_and_never_reach_history(self):
        result = self.closed("b1-temp", {"plans/cycle/plan.md": b"plan\n"})
        tmp = Path(result["cycle_dir"]) / "artifacts/plans/cycle/scratch"
        tmp.mkdir(parents=True)
        for i in range(15300):
            (tmp / f"t{i}.tmp").write_bytes(b"")
        cache = Path(result["cycle_dir"]) / "artifacts/plans/cycle/.pytest_cache"
        cache.mkdir()
        (cache / "x").write_bytes(b"x")
        self.edit(result, "plans/cycle/plan.md", b"plan, edited\n")
        started = time.monotonic()
        out = self.refresh(result)
        self.assertLess(time.monotonic() - started, 60.0)
        self.assertEqual(out["status"], "emitted", out)
        self.assertTrue(out["complete"])
        self.assertLessEqual(out["walked"], 20000)
        self.assertGreaterEqual(out["walked"], 15300)
        self.assertEqual([e["field"] for e in self.history.published], ["artifacts/plans/cycle/plan.md"])
        self.assertNotIn(".tmp", json.dumps(json.loads(self.manifest_path(result).read_text(encoding="utf-8"))))
        # A tree larger than the walk budget stops and continues, and never asks for a lock meanwhile.
        out = self.refresh(result, budget=P.RefreshBudget(max_walk_entries=5000))
        self.assertFalse(out["complete"])
        self.assertIsNotNone(out["cursor"])

    # -- root sweep (D-124 "순환 커서") ------------------------------------
    def test_b1_sweep_cursor_observes_every_closed_cycle_with_no_cutoff(self):
        old = {}
        for i in range(14):
            old[i] = self.closed(f"b1-sweep-{i:02d}", {"plans/cycle/plan.md": b"plan\n"}, activate=(i == 0))
        # Three of them were closed more than three days ago.
        for i in (0, 5, 13):
            record = P.read_cycle_record(self.root, old[i]["cycle_id"])
            record["sealed_on"] = "2026-09-20T00:00:00Z"
            P._write_cycle_record(self.root, record, exclusive=False)
        for i in range(14):
            self.edit(old[i], "plans/cycle/plan.md", f"plan edited {i}\n".encode())
        env = mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL": "0"})
        env.start()
        self.addCleanup(env.stop)
        calls = 0
        while calls < 60:
            calls += 1
            out = P.refresh_sweep(self.root, trigger="turn-end", budget=P.RefreshBudget(max_walk_entries=7))
            if all(json.loads(self.manifest_path(old[i]).read_text(encoding="utf-8"))["artifact_revisions"][0][
                    "content_digest"] == "sha256:" + hashlib.sha256(f"plan edited {i}\n".encode()).hexdigest()
                   for i in range(14)):
                break
        else:
            self.fail("the cursor did not reach every closed cycle")
        self.assertGreater(calls, 2)
        cursor = self.root / ".runtime/artifact-producer/v1/checkpoints/refresh/cursor.json"
        self.assertTrue(cursor.is_file())
        # The cycle a trigger names is seen first, whatever the cursor says.
        self.edit(old[3], "plans/cycle/plan.md", b"named cycle edited\n")
        self.edit(old[12], "plans/cycle/plan.md", b"far cycle edited\n")
        out = P.refresh_sweep(self.root, trigger="turn-end", first_cycle_id=old[12]["cycle_id"],
                              budget=P.RefreshBudget(max_walk_entries=4))
        self.assertEqual(out["refreshed"][0], old[12]["cycle_id"])

    def test_b1_checkpoint_command_reaches_a_closed_cycle_and_the_sweep(self):
        env = mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL": "0"})
        env.start()
        self.addCleanup(env.stop)
        one = self.closed("b1-cp-one", {"plans/cycle/plan.md": b"plan\n"})
        two = self.closed("b1-cp-two", {"plans/cycle/plan.md": b"plan\n"}, activate=False)
        self.edit(one, "plans/cycle/plan.md", b"one edited\n")
        self.edit(two, "plans/cycle/plan.md", b"two edited\n")
        out = P.checkpoint(self.root, cycle_id=one["cycle_id"], trigger="explicit")
        self.assertEqual((out["status"], out["cycle_id"]), ("emitted", one["cycle_id"]))
        # An automatic trigger sees its own cycle and then the rest of the root.
        self.edit(one, "plans/cycle/plan.md", b"one edited again\n")
        out = P.checkpoint(self.root, cycle_id=one["cycle_id"], trigger="turn-end")
        self.assertEqual(out["status"], "emitted")
        self.assertIn(two["cycle_id"], out["refresh"]["refreshed"])

    # -- explicit callers ------------------------------------------------
    def test_b1_explicit_reclose_scans_before_it_takes_the_lock(self):
        result = self.closed("b1-reclose", {"plans/cycle/a.md": b"a\n", "plans/cycle/b.md": b"b\n"})
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        hashed = []
        real_stream = P._stream_file_facts

        def watching(path):
            hashed.append(adm.holds_lock(self.root))
            return real_stream(path)

        with mock.patch.object(P, "_stream_file_facts", watching):
            out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertTrue(out["refreshed"], out)
        self.assertTrue(hashed)
        self.assertEqual(set(hashed), {False})

    def test_b1_cursor_walks_nested_directories_without_skipping_or_repeating(self):
        files = {f"plans/d{d}/sub/f{i}.md": f"d{d} f{i}\n".encode() for d in range(5) for i in range(4)}
        files.update({f"plans/d{d}.md": f"top {d}\n".encode() for d in range(5)})
        result = self.closed("b1-nested", files)
        for rel in files:
            self.edit(result, rel, b"edited " + rel.encode())
        hashed = []
        real_stream = P._stream_file_facts

        def counting(path):
            hashed.append(Path(path).relative_to(result["cycle_dir"]).as_posix())
            return real_stream(path)

        with mock.patch.object(P, "_stream_file_facts", counting):
            for _ in range(30):
                if self.refresh(result, budget=P.RefreshBudget(max_walk_entries=6)).get("complete"):
                    break
            else:
                self.fail("the walk never completed")
        self.assertEqual(sorted(hashed), sorted("artifacts/" + rel for rel in files))  # each file read exactly once
        document = json.loads(self.manifest_path(result).read_text(encoding="utf-8"))
        for row in document["artifact_revisions"]:
            rel = row["locator"]["path"][len("artifacts/"):]
            self.assertEqual(row["content_digest"], "sha256:" + hashlib.sha256(b"edited " + rel.encode()).hexdigest())
        self.assertEqual(len(document["artifact_revisions"]), len(files))

    def test_b1_failures_never_fail_the_command_that_triggered_the_refresh(self):
        result = self.closed("b1-nofail", {"plans/cycle/a.md": b"a\n"})
        raw = self.manifest_path(result).read_bytes()
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        # Whatever goes wrong inside a refresh is a skipped result, not an exception.
        with mock.patch.object(P, "_bounded_scan", side_effect=P.ProducerError("artifacts-dir-missing", "x")):
            out = self.refresh(result)
        self.assertEqual((out["status"], out["reason"]), ("skipped", "artifacts-dir-missing"))
        with mock.patch.object(adm, "_acquire_lock", side_effect=adm.AdmissionBusy("busy")):
            out = self.refresh(result)
        self.assertEqual((out["status"], out["reason"]), ("skipped", "busy"))
        self.assertEqual(self.manifest_path(result).read_bytes(), raw)
        # The checkpoint command a trigger runs returns a result too.
        with mock.patch.object(P, "refresh_cycle", side_effect=RuntimeError("boom")):
            sweep = P.refresh_sweep(self.root, trigger="turn-end")
        self.assertEqual(sweep["refreshed"], [])
        # A recorder that raises never fails an explicit re-close: the lines wait in the record.
        self.history.fail_publish = True
        out = P.finalize(self.root, cycle_id=result["cycle_id"])
        self.assertTrue(out["refreshed"], out)
        self.assertEqual(len(P.read_cycle_record(self.root, result["cycle_id"])["history_pending"]), 1)
        self.history.fail_publish = False
        self.assertEqual(self.refresh(result)["status"], "unchanged")
        self.assertEqual(len(self.history.published), 1)
        self.assertNotIn("history_pending", P.read_cycle_record(self.root, result["cycle_id"]))

    def test_b1_real_history_recorder_takes_the_same_lines(self):
        real = Path(__file__).resolve().parents[2] / "artifact-meta-1001/utilities/artifact_history.py"
        if not real.is_file():
            self.skipTest("the f0 recorder worktree is not present")
        saved_path = list(sys.path)
        try:
            spec = importlib.util.spec_from_file_location("artifact_history", real)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"the f0 recorder does not import here: {exc}")
        finally:
            sys.path[:] = saved_path
        with mock.patch.dict(sys.modules, {"artifact_history": module}):
            result = self.closed("b1-real-recorder", {"plans/cycle/a.md": b"a\n"})
            self.edit(result, "plans/cycle/a.md", b"a, edited\n")
            self.write_output(result, "plans/cycle/b.md", b"b\n")
            self.assertEqual(self.refresh(result)["status"], "emitted")
            events = list(module.iter_events(self.root))
            self.assertEqual(sorted((e["kind"], e["operation"], e["field"]) for e in events),
                             [("artifact", "add", "artifacts/plans/cycle/b.md"),
                              ("artifact", "update", "artifacts/plans/cycle/a.md"),
                              ("lifecycle", "update", "state")])
            self.assertTrue((self.root / ".runtime/artifact-producer/v1/history/LATEST.json").is_file())
            self.assertNotIn("history_pending", P.read_cycle_record(self.root, result["cycle_id"]))

    def test_b1_automatic_trigger_waits_for_the_interval_after_a_publication(self):
        result = self.closed("b1-interval", {"plans/cycle/a.md": b"a\n"})
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_CHECKPOINT_MIN_INTERVAL": "900"}):
            self.edit(result, "plans/cycle/a.md", b"a, edited\n")
            self.assertEqual(self.refresh(result, now=1_000_000.0)["status"], "emitted")
            self.edit(result, "plans/cycle/a.md", b"a, edited twice\n")
            out = self.refresh(result, now=1_000_100.0)
            self.assertEqual((out["status"], out["reason"]), ("skipped", "min-interval"))
            self.assertEqual(self.refresh(result, now=1_000_100.0, trigger="explicit")["status"], "emitted")
            self.edit(result, "plans/cycle/a.md", b"a, edited three times\n")
            self.assertEqual(self.refresh(result, now=1_001_200.0)["status"], "emitted")

    def test_b1_explicit_refresh_ignores_the_interval_and_the_budget(self):
        result = self.closed("b1-explicit", {"plans/cycle/a.md": b"a\n"})
        self.edit(result, "plans/cycle/a.md", b"a, edited\n")
        self.assertEqual(self.refresh(result, trigger="explicit")["status"], "emitted")
        self.edit(result, "plans/cycle/a.md", b"a, edited twice\n")
        out = self.refresh(result, trigger="explicit")
        self.assertEqual(out["status"], "emitted", out)
        self.assertTrue(out["complete"])


def m_digest(document):
    return M.manifest_digest(document)


if __name__ == "__main__":
    unittest.main()
