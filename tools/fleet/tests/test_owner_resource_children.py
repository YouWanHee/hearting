"""Resource execution must remain visible while its exact model owner parks."""
import copy
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fleet import fleet, projection, render, route
from fleet.model import DispatchJob, ResourceJob


def flatten(lines):
    return "\n".join("".join(text for text, _key in line) for line in lines if line)


class OwnerResourceChildrenTest(unittest.TestCase):
    def setUp(self):
        self.record = {
            "schema_version": 2, "cwd": "/work/project", "artifact_root": "/work/project/.agent_reports",
            "capability": "autopilot-lab", "capability_mode": "eval",
            "effective_intensity": "standard", "nodes": [
                {"id": "eval-smoke", "depends_on": []},
                {"id": "eval-run", "depends_on": ["eval-smoke"]},
                {"id": "report", "depends_on": ["eval-run"]},
            ],
        }
        self.record["route_hash"] = route.route_hash(self.record)
        self.record["route_id"] = "rt-" + self.record["route_hash"].split(":", 1)[1][:16]
        self.rid = self.record["route_id"]
        self.owner = DispatchJob(
            key="lab", slug="same-slug-owner", cwd="/work/project", pid=40,
            harness="claude", model="opus", liveness="idle", status="open",
            depth=1, dispatch_depth=1, worker_type="owner", assigned_contract="autopilot-lab",
            intensity="standard", attempt_id="att-owner-current", elapsed_min=700,
            owner_route_file="/routes/lab.json", owner_route_id=self.rid,
            owner_route_hash=self.record["route_hash"],
            summary="eval-smoke old model summary", summary_ts=100,
            state_evidence={"inputs": {"observed_liveness": {"state": "parked-supervised"}}},
        )
        self.resource = ResourceJob(
            run_id="declared-run", cwd="/work/project", node="eval-run", route="/routes/lab.json",
            parent_attempt_id=self.owner.attempt_id, elapsed_min=410, liveness="working",
            registry_status="running", pid=41, starttime="123", command_hash="a" * 64,
        )
        self.evidence = {self.rid: {"eval-smoke": {"status": "done"}}}
        self.render_patch = mock.patch.object(render, "_COMPUTE_HOSTS", None)
        self.render_patch.start()
        render._ROUTE_FOLD.clear()

    def tearDown(self):
        self.render_patch.stop()
        render.set_process_view(False)
        render.set_show_all(False)
        render._ROUTE_FOLD.clear()

    def attach(self, jobs=None, resources=None, records=None):
        projection.attach_projections(
            [], jobs if jobs is not None else [self.owner],
            resources=resources if resources is not None else [self.resource],
            route_records=records if records is not None else {self.rid: self.record},
            node_evidence=self.evidence, spec_markers={}, capability_groundings={}, now=1000,
        )

    def test_legacy_resource_lights_current_node_for_every_harness_without_rewriting_summary(self):
        original = copy.deepcopy(self.record)
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                self.owner.harness = harness
                self.attach()
                work = self.owner.work_projection
                self.assertEqual((work.route_node, work.stage_label, work.node_state),
                                 ("eval-run", "eval-run", "active"))
                self.assertEqual([n.id for n in work.active_nodes], ["eval-run"])
                self.assertEqual(self.owner.resource_wait,
                                 {"state": "resource-parked", "nodes": ["eval-run"],
                                  "run_ids": ["declared-run"]})
                self.assertEqual(self.owner.summary, "eval-smoke old model summary")
                self.assertEqual(self.owner.summary_ts, 100)
                self.assertEqual(work.progress.done, 1)
        self.assertEqual(original, self.record)

    def test_same_cwd_and_slug_cannot_attach_to_a_different_attempt(self):
        retry = replace(self.owner, attempt_id="att-other", state_evidence={})
        self.attach(jobs=[retry, self.owner])
        self.assertEqual(retry.resource_children, [])
        self.assertEqual(self.owner.resource_children, [self.resource])
        self.assertIsNone(retry.resource_wait)
        # A route is shared by retries, but the parked activity belongs to its exact owner only.
        self.assertIsNotNone(self.owner.resource_wait)

    def test_duplicate_exact_parent_rows_and_missing_parent_never_guess(self):
        duplicate = replace(self.owner)
        self.attach(jobs=[self.owner, duplicate])
        self.assertFalse(self.owner.resource_children)
        self.assertFalse(duplicate.resource_children)
        self.attach(resources=[replace(self.resource, parent_attempt_id=None)])
        self.assertFalse(self.owner.resource_children)

    def test_explicit_route_conflicts_keep_child_visible_but_do_not_claim_current_stage(self):
        for changes in ({"route": "/routes/other.json"}, {"route_id": "rt-other"},
                        {"route_hash": "sha256:foreign"}, {"route_node": "report"},
                        {"node": "unknown-node"}):
            with self.subTest(changes=changes):
                child = replace(self.resource, **changes)
                self.attach(resources=[child])
                self.assertEqual(self.owner.resource_children, [child])
                self.assertIsNone(self.owner.work_projection.route_node)
                self.assertIsNone(self.owner.resource_wait)

    def test_invalid_owner_binding_keeps_observed_child_without_overriding_unknown(self):
        self.attach(records={self.rid: {**self.record, "route_hash": "sha256:foreign"}})
        self.assertEqual(self.owner.resource_children, [self.resource])
        self.assertTrue(self.owner.work_projection.ambiguity)
        self.assertIsNone(self.owner.resource_wait)

    def test_invalid_other_owner_cannot_light_the_valid_owners_route(self):
        invalid = replace(self.owner, attempt_id="att-invalid", owner_route_hash="sha256:foreign")
        child = replace(self.resource, parent_attempt_id=invalid.attempt_id)
        self.attach(jobs=[invalid, self.owner], resources=[child])
        self.assertTrue(invalid.work_projection.ambiguity)
        self.assertIsNone(self.owner.work_projection.route_node)
        self.assertIsNone(self.owner.resource_wait)

    def test_explicit_modern_resource_tuple_uses_the_same_projection(self):
        child = replace(self.resource, route=None, node=None, route_file=self.resource.route,
                        route_id=self.rid, route_hash=self.record["route_hash"], route_node="eval-run")
        self.attach(resources=[child])
        self.assertEqual(self.owner.work_projection.route_node, "eval-run")
        self.assertEqual(self.owner.resource_wait["nodes"], ["eval-run"])

    def test_terminal_or_unobservable_resources_do_not_keep_owner_active(self):
        for state in ("exited", "stale"):
            with self.subTest(state=state):
                self.attach(resources=[replace(self.resource, liveness=state)])
                self.assertEqual(self.owner.resource_children[0].liveness, state)
                self.assertIsNone(self.owner.work_projection.route_node)
                self.assertIsNone(self.owner.resource_wait)
                self.assertEqual(render._resource_child_rows(self.owner), [])
                render.set_show_all(True)
                self.assertIn(state, flatten(render._resource_child_rows(self.owner)))
                render.set_show_all(False)
        self.attach(resources=[])
        self.assertEqual(self.owner.resource_children, [])

    def test_working_owner_and_plain_idle_owner_are_not_labeled_parked(self):
        for liveness, evidence in (("working", self.owner.state_evidence), ("idle", {})):
            with self.subTest(liveness=liveness):
                self.owner.liveness, self.owner.state_evidence = liveness, evidence
                self.attach()
                self.assertEqual(self.owner.work_projection.route_node, "eval-run")
                self.assertIsNone(self.owner.resource_wait)

    def test_parallel_resources_keep_all_nodes_in_sealed_order(self):
        record = copy.deepcopy(self.record)
        record["nodes"].insert(2, {"id": "eval-alt", "depends_on": ["eval-smoke"]})
        record["route_hash"] = route.route_hash(record)
        record["route_id"] = "rt-" + record["route_hash"].split(":", 1)[1][:16]
        self.owner.owner_route_id = record["route_id"]
        self.owner.owner_route_hash = record["route_hash"]
        second = replace(self.resource, run_id="alt-run", node="eval-alt")
        self.attach(resources=[second, self.resource], records={record["route_id"]: record})
        self.assertEqual(self.owner.resource_wait["nodes"], ["eval-run", "eval-alt"])
        self.assertEqual([n.id for n in self.owner.work_projection.active_nodes],
                         ["eval-run", "eval-alt"])

    def test_group_process_and_json_show_exact_child_and_current_wait(self):
        self.attach()
        for process in (False, True):
            render.set_process_view(process)
            for layout, width in (("wide", 168), ("narrow", 80), ("stack", 60)):
                with self.subTest(process=process, layout=layout):
                    lines = render._build_lines([], [self.owner], "both", width < 80, 0,
                                                layout=layout, term_width=width,
                                                resources=[self.resource], governor=None)
                    text = flatten(lines)
                    self.assertIn("resource eval-run", text)
                    self.assertIn("working  6h 50m", text)
                    self.assertNotIn("old model summary", text)
                    self.assertEqual(text.count("resource eval-run"), 1)
                    child_line = next(line for line in lines if line and "resource eval-run" in flatten([line]))
                    self.assertLessEqual(sum(render._dw(t) for t, _ in child_line), width)
                    self.assertIn("resource-parked", text)
        snap = json.loads(fleet._snapshot_json([], [self.owner], [self.resource]))
        self.assertEqual(len(snap["jobs"]), 1)
        job = snap["jobs"][0]
        self.assertEqual(job["resource_children"][0]["run_id"], "declared-run")
        self.assertEqual(job["resource_wait"]["state"], "resource-parked")
        self.assertEqual(job["summary"], "eval-smoke old model summary")
        self.assertEqual(job["liveness"], "idle")
        nodes = snap["route"][0]["nodes"]
        self.assertEqual(next(n for n in nodes if n["id"] == "eval-run")["state"], "active")

    def test_long_resource_node_cannot_clip_liveness_or_elapsed_at_card_border(self):
        self.attach(resources=[replace(self.resource, node="very-long-resource-node-" * 8)])
        for width in (60, 80, 168):
            for tint in (False, True):
                with self.subTest(width=width, tint=tint), mock.patch.object(render, "_TINT_OK", tint):
                    rows = render._resource_child_rows(self.owner, term_width=width, in_card=True)
                    framed = [render._frame_dispatch_line(
                        row, render._dispatch_box_width(width), "mid", "frm_idle") for row in rows]
                    self.assertIn("working  6h 50m", flatten(framed))
                    self.assertLessEqual(sum(render._dw(t) for t, _ in framed[0]), width)


    def test_declared_progress_stays_on_the_resource_row_in_every_view(self):
        self.attach()
        progress = {"completed": 12, "total": 50, "unit": "epoch", "age_s": 120}
        for process in (False, True):
            render.set_process_view(process)
            for layout, width in (("wide", 168), ("narrow", 80), ("stack", 60)):
                with self.subTest(process=process, width=width):
                    self.resource.progress = None
                    baseline = render._build_lines([], [self.owner], "both", width < 80, 0,
                        layout=layout, term_width=width, resources=[self.resource], governor=None)
                    self.resource.progress = progress
                    actual = render._build_lines([], [self.owner], "both", width < 80, 0,
                        layout=layout, term_width=width, resources=[self.resource], governor=None)
                    self.assertEqual(len(actual), len(baseline))
                    line = next(line for line in actual if line and "resource eval-run" in flatten([line]))
                    self.assertIn("2m ago", flatten([line]))
                    self.assertIn("working  6h 50m", flatten([line]))
                    if width >= 80:
                        self.assertIn("12/50 epoch", flatten([line]))
                    self.assertLessEqual(sum(render._dw(t) for t, _ in line), width)
                    self.assertEqual(flatten(actual).count("2m ago"), 1)

    def test_counter_without_total_and_old_age_do_not_change_resource_state(self):
        self.attach()
        self.resource.progress = {"completed": 12, "unit": "item", "age_s": 9900}
        text = flatten(render._resource_child_rows(self.owner, term_width=168))
        self.assertIn("12 item · 2h 45m ago", text)
        self.assertEqual(self.resource.liveness, "working")
        self.assertEqual(self.owner.resource_wait["state"], "resource-parked")
        self.resource.progress = None
        self.assertNotIn("ago", flatten(render._resource_child_rows(self.owner, term_width=168)))

    def test_long_counter_unit_keeps_age_and_row_width(self):
        self.attach(resources=[replace(self.resource, node="very-long-resource-node-" * 8)])
        self.owner.resource_children[0].progress = {"completed": 1234567890,
            "total": 1234567890, "unit": "한글" * 20, "age_s": 120}
        for width in (60, 80, 168):
            with self.subTest(width=width):
                rows = render._resource_child_rows(self.owner, term_width=width, in_card=True)
                self.assertEqual(len(rows), 1)
                self.assertIn("2m ago", flatten(rows))
                self.assertIn("working  6h 50m", flatten(rows))
                self.assertLessEqual(sum(render._dw(t) for t, _ in rows[0]),
                                     render._dispatch_box_width(width) - 1)


if __name__ == "__main__":
    unittest.main()
