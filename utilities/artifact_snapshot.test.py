#!/usr/bin/env python3
import json, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
HELPER=ROOT/"utilities/artifact-snapshot.py"


class ArtifactSnapshotTest(unittest.TestCase):
 def route(self, root: Path, *, route_id="rt-one", capability="autopilot-refine", intensity="standard") -> Path:
  path=root/f"{route_id}.json"
  path.write_text(json.dumps({"route_id":route_id,"route_hash":f"sha256:{route_id}","capability":capability,"effective_intensity":intensity,"nodes":[{"id":"transaction","write_scope":["target-artifact"]}]}))
  return path

 def run_helper(self, artifact: Path, target: Path, route: Path, route_id: str, node: str="transaction"):
  return subprocess.run([sys.executable,str(HELPER),"prepare","--artifact-root",str(artifact),"--target",str(target),"--route",str(route),"--route-id",route_id,"--node",node],text=True,capture_output=True)

 def draft_route(self, root: Path, *, route_id="rt-draft", intensity="standard") -> Path:
  path=root/f"{route_id}.json"
  nodes=[
   {"id":"frame","write_scope":["shards/frame/**"]},
   {"id":"frame-alternative","write_scope":["shards/frame-alternative/**"]},
   {"id":"strategy","write_scope":["analysis/**","strategy/**","assets/source/**"]},
   {"id":"review","write_scope":["reviews/strategy/**"]},
   {"id":"draft-production","write_scope":["draft/**"]},
   {"id":"finalize","write_scope":["final/**","pipeline_summary.md"]},
  ]
  path.write_text(json.dumps({"route_id":route_id,"route_hash":f"sha256:{route_id}","capability":"autopilot-draft","effective_intensity":intensity,"nodes":nodes}))
  return path

 def test_same_route_reuses_one_version_and_preserves_relative_paths(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; doc=artifact/"documents/cycle/doc.md"; appendix=artifact/"documents/cycle/parts/appendix.md"
   appendix.parent.mkdir(parents=True); doc.write_text("doc-before\n"); appendix.write_text("appendix-before\n")
   route=self.route(root)
   first=self.run_helper(artifact,doc,route,"rt-one"); second=self.run_helper(artifact,appendix,route,"rt-one"); repeat=self.run_helper(artifact,doc,route,"rt-one")
   self.assertEqual((first.returncode,second.returncode,repeat.returncode),(0,0,0),first.stderr+second.stderr+repeat.stderr)
   version=artifact/"documents/cycle/_internal/versions/v1"
   self.assertEqual((version/"doc.md").read_text(),"doc-before\n")
   self.assertEqual((version/"parts/appendix.md").read_text(),"appendix-before\n")
   self.assertEqual(len(list((version.parent).glob("v*"))),1)
   self.assertEqual(json.loads(repeat.stdout)["snapshot"],"matched")

 def test_next_route_allocates_next_version(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; doc=artifact/"research/topic/report.md"; doc.parent.mkdir(parents=True); doc.write_text("v0\n")
   one=self.route(root,route_id="rt-one"); self.assertEqual(self.run_helper(artifact,doc,one,"rt-one").returncode,0)
   doc.write_text("v1\n"); two=self.route(root,route_id="rt-two"); self.assertEqual(self.run_helper(artifact,doc,two,"rt-two").returncode,0)
   self.assertEqual((doc.parent/"_internal/versions/v1/report.md").read_text(),"v0\n")
   self.assertEqual((doc.parent/"_internal/versions/v2/report.md").read_text(),"v1\n")

 def test_direct_refine_and_new_target_do_not_snapshot(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; doc=artifact/"documents/cycle/doc.md"; doc.parent.mkdir(parents=True); doc.write_text("before\n")
   direct=self.route(root,intensity="direct"); result=self.run_helper(artifact,doc,direct,"rt-one")
   self.assertEqual(result.returncode,0,result.stderr); self.assertIn("minor-direct-edit",result.stdout); self.assertFalse((doc.parent/"_internal").exists())
   major=self.route(root,route_id="rt-two"); new=doc.parent/"new.md"; result=self.run_helper(artifact,new,major,"rt-two")
   self.assertEqual(result.returncode,0,result.stderr); self.assertIn("new-target",result.stdout); self.assertFalse((doc.parent/"_internal").exists())

 def test_draft_refinement_snapshots_existing_file(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; draft=artifact/"documents/cycle/draft/manuscript.md"; draft.parent.mkdir(parents=True); draft.write_text("draft\n")
   route=self.route(root,capability="autopilot-draft",intensity="direct"); result=self.run_helper(artifact,draft,route,"rt-one")
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual((artifact/"documents/cycle/_internal/versions/v1/draft/manuscript.md").read_text(),"draft\n")

 def test_unowned_container_and_mismatched_preimage_fail_closed(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; legacy=artifact/"rebuttal/rebuttal.md"; legacy.parent.mkdir(parents=True); legacy.write_text("legacy\n")
   route=self.route(root); result=self.run_helper(artifact,legacy,route,"rt-one")
   self.assertEqual(result.returncode,65); self.assertIn("target-container-unowned",result.stderr)
   doc=artifact/"documents/cycle/doc.md"; doc.parent.mkdir(parents=True); doc.write_text("before\n")
   self.assertEqual(self.run_helper(artifact,doc,route,"rt-one").returncode,0)
   snapshot=doc.parent/"_internal/versions/v1/doc.md"; snapshot.write_text("corrupt\n")
   result=self.run_helper(artifact,doc,route,"rt-one")
   self.assertEqual(result.returncode,65); self.assertIn("snapshot-preimage-mismatch",result.stderr)

 def test_existing_legacy_sibling_layout_is_preserved(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; doc=artifact/"documents/legacy/doc.md"; doc.parent.mkdir(parents=True); doc.write_text("current\n"); (doc.parent/"doc_v1.md").write_text("old\n")
   route=self.route(root); result=self.run_helper(artifact,doc,route,"rt-one")
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual((doc.parent/"doc_v2.md").read_text(),"current\n")
   self.assertFalse((doc.parent/"_internal").exists())

 def test_draft_support_scopes_skip_snapshot(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; route=self.draft_route(root)
   cases=[("frame","shards/frame/direction-brief.md"),
          ("frame-alternative","shards/frame-alternative/direction-brief.md"),
          ("strategy","strategy/plan.md"),
          ("strategy","analysis/source-map.md"),
          ("review","reviews/strategy/verdict.md"),
          ("draft-production","draft/cheatsheet.md"),
          ("finalize","final/final.md"),
          ("finalize","pipeline_summary.md")]
   for node,rel in cases:
    target=artifact/rel; target.parent.mkdir(parents=True,exist_ok=True); target.write_text("support\n")
    result=self.run_helper(artifact,target,route,"rt-draft",node=node)
    self.assertEqual(result.returncode,0,result.stderr)
    self.assertIn("support-artifact",result.stdout)
   self.assertEqual([path for path in artifact.rglob("_internal")],[])

 def test_cycle_layout_support_artifact_skips_snapshot(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"
   target=artifact/"campaigns/2026-09-14_c/cycles/cyc_x/artifacts/shards/frame/direction-brief.md"
   target.parent.mkdir(parents=True); target.write_text("support\n")
   route=self.draft_route(root)
   result=self.run_helper(artifact,target,route,"rt-draft",node="frame")
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertIn("support-artifact",result.stdout)

 def test_cycle_layout_document_still_snapshots(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"
   target=artifact/"campaigns/c/cycles/y/artifacts/documents/name/draft/manuscript.md"
   target.parent.mkdir(parents=True); target.write_text("doc-before\n")
   route=self.draft_route(root)
   result=self.run_helper(artifact,target,route,"rt-draft",node="draft-production")
   self.assertEqual(result.returncode,0,result.stderr)
   self.assertEqual((target.parents[1]/"_internal/versions/v1/draft/manuscript.md").read_text(),"doc-before\n")

 def test_malformed_and_undeclared_support_paths_still_fail_closed(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; route=self.draft_route(root)
   malformed=artifact/"documents/loose.md"; malformed.parent.mkdir(parents=True); malformed.write_text("x\n")
   result=self.run_helper(artifact,malformed,route,"rt-draft",node="strategy")
   self.assertEqual(result.returncode,65); self.assertIn("target-container-unowned",result.stderr)
   undeclared=artifact/"shards/other/x.md"; undeclared.parent.mkdir(parents=True); undeclared.write_text("x\n")
   result=self.run_helper(artifact,undeclared,route,"rt-draft",node="strategy")
   self.assertEqual(result.returncode,65); self.assertIn("target-container-unowned",result.stderr)

 def test_shared_and_outside_root_still_fail_closed(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; route=self.draft_route(root)
   shared=artifact/"shared/spec/ref/revisions/1/prd.md"; shared.parent.mkdir(parents=True); shared.write_text("x\n")
   result=self.run_helper(artifact,shared,route,"rt-draft",node="finalize")
   self.assertEqual(result.returncode,65); self.assertIn("target-shared-immutable",result.stderr)
   outside=root/"outside.md"; outside.write_text("x\n")
   result=self.run_helper(artifact,outside,route,"rt-draft",node="finalize")
   self.assertEqual(result.returncode,65); self.assertIn("target-outside-artifact-root",result.stderr)


if __name__=="__main__": unittest.main()
