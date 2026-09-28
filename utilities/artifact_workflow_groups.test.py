#!/usr/bin/env python3
"""Focused producer workflow-group contract tests."""
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import artifact_producer as P  # noqa: E402
import artifact_workflow_groups as W  # noqa: E402

spec = importlib.util.spec_from_file_location("workflow_groups_producer_fixture",
                                              Path(__file__).with_name("artifact_producer.test.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class WorkflowGroupsTest(fixture.ProducerTestBase):
    def _cycles(self, count=3):
        self.activate()
        rows = []
        for index in range(count):
            route, route_file = self.route(slug=f"workflow-{index}", campaign_key="workflow-test")
            begun = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                            intensity="direct", campaign_key="workflow-test")
            payload = self.write_output(begun, f"plans/workflow-{index}.md",
                                        f"cycle {index}\n".encode())
            relative = payload.relative_to(self.root).as_posix()
            artifact_id = "art_" + f"{index + 1:032x}"
            revision_id = "arev_" + f"{index + 1:032x}"
            digest = W._digest(payload.read_bytes())
            interim = {
                "artifact_root_id": fixture.ROOT_ID,
                "repository_id": fixture.REPO_ID,
                "campaign": {"campaign_id": begun["campaign_id"]},
                "cycle": {"cycle_id": begun["cycle_id"], "campaign_id": begun["campaign_id"], "state": "open"},
                "artifacts": [{"artifact_id": artifact_id, "cycle_id": begun["cycle_id"]}],
                "artifact_revisions": [{"artifact_id": artifact_id, "artifact_revision_id": revision_id,
                                        "content_digest": digest,
                                        "locator": {"kind": "cycle-relative", "path": f"artifacts/plans/workflow-{index}.md"}}],
            }
            manifest_path = P.producer_dir(self.root) / "open-manifests" / f"{begun['cycle_id']}.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(interim), encoding="utf-8")
            rows.append({"id": begun["cycle_id"], "campaign": begun["campaign_id"],
                         "path": relative, "file": payload, "manifest": manifest_path})
        return rows

    @staticmethod
    def _proposal(rows, relations=None, title="Test workflow", group_id=None):
        group = {"title": title,
                 "members": [{"cycle_id": row["id"], "stage_label": f"Stage {index}"}
                             for index, row in enumerate(rows)],
                 "relations": relations or []}
        if group_id:
            group["group_id"] = group_id
        return {"groups": [group]}

    @staticmethod
    def _relation(source, target, kind="precedes"):
        if kind == "parallel" and source["id"] > target["id"]:
            source, target = target, source
        return {"from_cycle_id": source["id"], "to_cycle_id": target["id"],
                "kind": kind, "rationale": "The second cycle uses the first cycle's evidence.",
                "evidence_refs": [{"path": source["path"]}, {"path": target["path"]}]}

    def test_prepare_apply_idempotence_and_historical_staleness(self):
        rows = self._cycles(2)
        camp = rows[0]["campaign"]
        protected = [P.campaign_dir(self.root, camp) / "campaign.json",
                     *[Path(row["file"]).parent.parent.parent / ".cycle.json" for row in rows],
                     *[row["manifest"] for row in rows]]
        before = {path: path.read_bytes() for path in protected}
        proposal = self._proposal(rows, [self._relation(rows[0], rows[1])])
        plan = W.prepare(self.root, camp, proposal)
        declaration = W.declaration_path(self.root, camp)
        self.assertFalse(declaration.exists())
        self.assertEqual(W.apply(self.root, plan)["status"], "applied")
        self.assertEqual(W.apply(self.root, plan)["status"], "already-applied")
        self.assertEqual(W.verify(self.root, camp)["stale_evidence"], [])
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        rows[0]["file"].write_text("changed\n", encoding="utf-8")
        verified = W.verify(self.root, camp)
        self.assertEqual(verified["stale_evidence"], [rows[0]["path"]])
        self.assertEqual(verified["groups"], 1)

    def test_merge_preserves_existing_and_stale_plan_conflicts(self):
        rows = self._cycles(3)
        camp = rows[0]["campaign"]
        first = W.prepare(self.root, camp, self._proposal(rows[:2], [self._relation(rows[0], rows[1])]))
        W.apply(self.root, first)
        gid = first["document"]["groups"][0]["group_id"]
        second_proposal = self._proposal([rows[2]], title="Another workflow")
        second = W.prepare(self.root, camp, second_proposal)
        W.apply(self.root, second)
        groups = W._load_existing(W.declaration_path(self.root, camp))["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["group_id"], gid)
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.apply(self.root, first)
        self.assertEqual(caught.exception.code, "declaration-preimage-conflict")

    def test_merge_plan_cannot_silently_remove_existing_relation(self):
        rows = self._cycles(2)
        camp = rows[0]["campaign"]
        first = W.prepare(self.root, camp, self._proposal(rows, [self._relation(rows[0], rows[1])]))
        W.apply(self.root, first)
        unsafe = W.prepare(self.root, camp, {"groups": []})
        unsafe["document"]["groups"][0]["relations"] = []
        unsafe["after_sha256"] = W._digest(W._bytes(unsafe["document"]))
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.apply(self.root, unsafe)
        self.assertEqual(caught.exception.code, "merge-removal-forbidden")
        self.assertEqual(len(W._load_existing(W.declaration_path(self.root, camp))["groups"][0]["relations"]), 1)

    def test_indirect_parallel_and_direction_cycle_refused(self):
        rows = self._cycles(3)
        relations = [self._relation(rows[0], rows[1]), self._relation(rows[1], rows[2]),
                     self._relation(rows[0], rows[2], "parallel")]
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.prepare(self.root, rows[0]["campaign"], self._proposal(rows, relations))
        self.assertEqual(caught.exception.code, "parallel-direction-conflict")
        relations[-1] = self._relation(rows[2], rows[0])
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.prepare(self.root, rows[0]["campaign"], self._proposal(rows, relations))
        self.assertEqual(caught.exception.code, "relation-cycle")

    def test_shared_evidence_is_read_once_within_total_budget(self):
        rows = self._cycles(3)
        first = self._relation(rows[0], rows[1])
        second = self._relation(rows[0], rows[2])
        first["evidence_refs"] = [{"path": rows[0]["path"]}]
        second["evidence_refs"] = [{"path": rows[0]["path"]}]
        with mock.patch.object(W, "MAX_EVIDENCE_TOTAL", rows[0]["file"].stat().st_size):
            with mock.patch.object(W, "_bind_evidence", wraps=W._bind_evidence) as bound:
                plan = W.prepare(self.root, rows[0]["campaign"], self._proposal(rows, [first, second]))
                self.assertEqual(bound.call_count, 1)
                W.apply(self.root, plan)
                self.assertEqual(bound.call_count, 2)

    def test_missing_and_outside_evidence_refused_before_write(self):
        rows = self._cycles(2)
        relation = self._relation(rows[0], rows[1])
        relation["evidence_refs"][0]["path"] = "campaigns/../../escape.md"
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.prepare(self.root, rows[0]["campaign"], self._proposal(rows, [relation]))
        self.assertEqual(caught.exception.code, "evidence-path-invalid")
        relation["evidence_refs"][0]["path"] = rows[0]["path"] + ".missing"
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.prepare(self.root, rows[0]["campaign"], self._proposal(rows, [relation]))
        self.assertEqual(caught.exception.code, "file-missing")
        self.assertFalse(W.declaration_path(self.root, rows[0]["campaign"]).exists())

    def test_begin_inherits_only_explicit_same_campaign_group_context(self):
        rows = self._cycles(2)
        camp = rows[0]["campaign"]
        plan = W.prepare(self.root, camp, self._proposal(rows, [self._relation(rows[0], rows[1])]))
        W.apply(self.root, plan)
        gid = plan["document"]["groups"][0]["group_id"]
        source_record = P.read_cycle_record(self.root, rows[1]["id"])
        inherited = P._env_for(self.root, source_record)
        self.assertEqual(inherited["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"], gid)
        route, route_file = self.route(slug="workflow-continued", campaign_key="workflow-test")
        with mock.patch.dict(os.environ, inherited):
            begun = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                            intensity="direct", campaign_key="workflow-test")
        self.assertEqual(begun["env"]["AGENT_ARTIFACT_WORKFLOW_GROUP_ID"], gid)
        doc = W._load_existing(W.declaration_path(self.root, camp))
        self.assertEqual(len(doc["groups"][0]["members"]), 3)
        route, route_file = self.route(slug="workflow-independent", campaign_key="workflow-test",
                                      parent_cycle_id=rows[0]["id"])
        ungrouped = P.begin(self.root, route_file=route_file, capability="autopilot-code",
                            intensity="direct", campaign_key="workflow-test",
                            parent_cycle_id=rows[0]["id"])
        self.assertNotIn("AGENT_ARTIFACT_WORKFLOW_GROUP_ID", ungrouped["env"])
        doc = W._load_existing(W.declaration_path(self.root, camp))
        self.assertEqual(len(doc["groups"][0]["members"]), 3)

    def test_replace_withdraws_group_and_new_evidence_drift_blocks_apply(self):
        rows = self._cycles(2)
        camp = rows[0]["campaign"]
        proposal = self._proposal(rows, [self._relation(rows[0], rows[1])])
        plan = W.prepare(self.root, camp, proposal)
        rows[0]["file"].write_text("changed before apply\n", encoding="utf-8")
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W.apply(self.root, plan)
        self.assertEqual(caught.exception.code, "evidence-not-current-manifest")
        self.assertFalse(W.declaration_path(self.root, camp).exists())
        rows[0]["file"].write_text("cycle 0\n", encoding="utf-8")
        W.apply(self.root, plan)
        withdrawn = W.prepare(self.root, camp, {"groups": []}, replace=True)
        self.assertEqual(W.apply(self.root, withdrawn)["status"], "applied")
        self.assertEqual(W.verify(self.root, camp)["groups"], 0)

    def test_duplicate_json_key_is_rejected(self):
        with self.assertRaises(W.WorkflowGroupError) as caught:
            W._json(b'{"groups":[],"groups":[]}')
        self.assertEqual(caught.exception.code, "json-duplicate-key")


if __name__ == "__main__":
    unittest.main()
