#!/usr/bin/env python3
import importlib.util, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
R=load("route",ROOT/"utilities/capability-route.py")
TX=load("spec_transaction",ROOT/"utilities/spec-transaction.py")
# Owner finding D1 (2026-09-07): `spec-transaction.py` resolves its spec bucket from
# AGENT_ARTIFACT_* (not from --artifact-root), so a child inheriting a dispatch
# environment wrote a LIVE cycle bucket. Every child gets a scrubbed environment.
HERMETIC_ENV={k:v for k,v in os.environ.items() if not k.startswith("AGENT_ARTIFACT_")}

def dispatch(worktree):
 return {"tuples":[{"parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write","child_harness":"codex","launch_authority":"conductor","status":"supported","probe_source":"fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"","checked_worktree":str(Path(worktree).resolve()),"failure_scope":"none","codex_command":"ok","retry_on_isolated_worktree":0}],"native_subagent":[]}

class SpecTransactionTest(unittest.TestCase):
 def fixture(self, root: Path, *, component=""):
  artifact=root/".agent_reports"; spec=artifact/"spec"/component; spec.mkdir(parents=True)
  subprocess.run(["git","init","-q",str(root)],check=True)
  subprocess.run(["git","-C",str(root),"config","user.email","fixture@example.com"],check=True)
  subprocess.run(["git","-C",str(root),"config","user.name","Fixture"],check=True)
  (root/"README").write_text("x\n"); subprocess.run(["git","-C",str(root),"add","README"],check=True); subprocess.run(["git","-C",str(root),"commit","-qm","init"],check=True)
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  route=R.compile_route("autopilot-spec","update","strong",root,artifact,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(root))
  route_path=root/"route.json"; route_path.write_text(json.dumps(route))
  return artifact,spec,route_path

 def command(self, root, artifact, route, code, *, spec_root=None, events=None):
  command=[sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--artifact-root",str(artifact),"--worktree",str(root),"--route",str(route),"--node","prd-transaction"]
  if spec_root is not None: command.extend(["--spec-root",str(spec_root)])
  if events is not None: command.extend(["--events",str(events)])
  return command+["--",sys.executable,"-c",code]

 def test_blocked_wait_rereads_and_snapshots_each_exact_preimage(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("v0\n"); events=root/"events.jsonl"
   code="import os,time; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n'); time.sleep(float(os.environ.get('HOLD','0')))"
   base=self.command(root,artifact,route,code,events=events)
   first=subprocess.Popen(base,env={**HERMETIC_ENV,"HOLD":".4"},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   deadline=time.time()+2
   while time.time()<deadline and (not events.exists() or '"status": "acquired"' not in events.read_text()): time.sleep(.02)
   second=subprocess.Popen(base,env={**HERMETIC_ENV,"HOLD":"0"},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   out1,err1=first.communicate(timeout=4); out2,err2=second.communicate(timeout=4)
   self.assertEqual(first.returncode,0,out1+err1); self.assertEqual(second.returncode,0,out2+err2)
   rows=[json.loads(line) for line in events.read_text().splitlines()]
   self.assertTrue(any(row["status"]=="BLOCKED" for row in rows))
   self.assertEqual((spec/"_internal/versions/v1/prd.md").read_text(),"v0\n")
   self.assertEqual((spec/"_internal/versions/v2/prd.md").read_text(),"v1\n")
   self.assertEqual((spec/"prd.md").read_text(),"v2\n")

 def test_unchanged_and_new_prd_do_not_create_snapshot(self):
  for existing,code in ((True,"pass"),(False,"from pathlib import Path; import os; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('new\\n')")):
   with self.subTest(existing=existing), tempfile.TemporaryDirectory() as td:
    root=Path(td); artifact,spec,route=self.fixture(root)
    if existing: (spec/"prd.md").write_text("same\n")
    result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True,env=HERMETIC_ENV)
    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    self.assertFalse((spec/"_internal/versions").exists())

 def test_empty_version_directory_cannot_bypass_snapshot_file(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   code="import os; from pathlib import Path; root=Path(os.environ['AGENT_SPEC_ROOT']); (root/'_internal/versions'/('v'+os.environ['AGENT_SPEC_NEXT_VERSION'])).mkdir(parents=True,exist_ok=True); (root/'prd.md').write_text('after\\n')"
   result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True,env=HERMETIC_ENV)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertEqual((spec/"_internal/versions/v1/prd.md").read_text(),"before\n")

 def test_mismatched_manual_snapshot_fails_closed(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   code="import os; from pathlib import Path; root=Path(os.environ['AGENT_SPEC_ROOT']); snap=root/'_internal/versions'/('v'+os.environ['AGENT_SPEC_NEXT_VERSION']); snap.mkdir(parents=True,exist_ok=True); (snap/'prd.md').write_text('wrong\\n'); (root/'prd.md').write_text('after\\n')"
   result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True,env=HERMETIC_ENV)
   self.assertEqual(result.returncode,65,result.stdout+result.stderr)
   self.assertIn("version-snapshot-mismatch",result.stdout)

 def test_failed_command_still_snapshots_changed_preimage(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   code="import os,sys; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('partial\\n'); sys.exit(7)"
   result=subprocess.run(self.command(root,artifact,route,code),text=True,capture_output=True,env=HERMETIC_ENV)
   self.assertEqual(result.returncode,7,result.stdout+result.stderr)
   self.assertEqual((spec/"_internal/versions/v1/prd.md").read_text(),"before\n")

 def test_spec_touch_required(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact=root/".agent_reports"; artifact.mkdir(); subprocess.run(["git","init","-q",str(root)],check=True)
   gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
   route=R.compile_route("autopilot-code","dev","direct",root,artifact,predicates=["atomic-outcome","known-scope","no-shared-contract","no-resource-run","no-artifact-handoff","no-independent-verifier","focused-verification"],inline_reason="atomic-direct",tracking="tracked",tracked_gate_evidence=gate)
   path=root/"route.json"; path.write_text(json.dumps(route)); result=subprocess.run([sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--artifact-root",str(artifact),"--worktree",str(root),"--route",str(path),"--node","inline","--",sys.executable,"-c","pass"],text=True,capture_output=True,env=HERMETIC_ENV)
   self.assertEqual(result.returncode,65); self.assertIn("spec-touch-not-declared",result.stdout)

  # -- the v{N} chain is canonical, not per-tree ------------------------------

 def test_next_version_continues_the_chain_across_legacy_and_shared(self):
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; cycle_spec=artifact/"campaigns/c/cycles/y/artifacts/spec"; cycle_spec.mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),1)
   (artifact/"spec/_internal/versions/v168").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),169,"an empty cycle bucket must not restart at v1 while legacy holds the chain")
   (artifact/"shared/spec/ref_x/revisions/rrev_x/_internal/versions/v200").mkdir(parents=True)
   (artifact/"shared/spec/ref_y/revisions/rrev_y/_internal/versions/v201").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),202,"every shared reference and revision carries history")
   (cycle_spec/"_internal/versions/v300").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec,artifact),301)
   (artifact/"spec/_internal/versions/v9_prd.md").mkdir(parents=True)   # not a v{N} directory name
   (artifact/"spec/_internal/versions/v400").write_text("file, not a version dir\n")
   self.assertEqual(TX.next_version(cycle_spec,artifact),301)

 def test_next_version_keeps_a_component_on_its_own_chain(self):
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; cycle_spec=artifact/"campaigns/c/cycles/y/artifacts/spec"; (cycle_spec/"comp").mkdir(parents=True)
   (artifact/"spec/_internal/versions/v168").mkdir(parents=True)
   (artifact/"spec/comp/_internal/versions/v5").mkdir(parents=True)
   (artifact/"shared/spec/ref_x/revisions/rrev_x/comp/_internal/versions/v7").mkdir(parents=True)
   self.assertEqual(TX.next_version(cycle_spec/"comp",artifact,"comp"),8)
   self.assertEqual(TX.next_version(cycle_spec,artifact),169)
   trees=TX.version_history_trees(cycle_spec/"comp",artifact,"comp")
   self.assertTrue(all(t.name=="comp" for t in trees),trees)

 def test_version_history_trees_dedupes_the_legacy_root(self):
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; legacy=artifact/"spec"; (legacy/"_internal/versions/v3").mkdir(parents=True)
   trees=TX.version_history_trees(legacy,artifact)
   self.assertEqual([t.resolve() for t in trees],[legacy.resolve()])
   self.assertEqual(TX.next_version(legacy,artifact),4)

 def test_chain_counts_residue_home_symlinked_bucket_and_legacy_cycles_layer(self):
  # Review round 1 (major 2, 3): trees the reader's enumeration cannot see still hold real v{N}.
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; open_bucket=artifact/"campaigns/2026-09-07_o/2026-09-07_o/artifacts/spec"; open_bucket.mkdir(parents=True)
   self.assertEqual(TX.next_version(open_bucket,artifact),1)
   # W7H --include-spec-top parked the legacy chain at artifacts/_internal/spec (no top-level spec/ left)
   (artifact/"campaigns/2026-09-05_support-residue/2026-06-16_support-residue-2/artifacts/_internal/spec/_internal/versions/v170").mkdir(parents=True)
   self.assertEqual(TX.next_version(open_bucket,artifact),171,"the residue home of a retired legacy spec/ is on the chain")
   # a sealed cycle whose bucket was moved and replaced by a symlink
   moved=Path(td)/"moved-bucket"; (moved/"_internal/versions/v180").mkdir(parents=True)
   link=artifact/"campaigns/2026-09-06_a/2026-09-06_a/artifacts/spec"; link.parent.mkdir(parents=True); link.symlink_to(moved)
   self.assertEqual(TX.next_version(open_bucket,artifact),181,"a symlinked bucket is followed")
   # a symlinked cycle directory
   moved_cycle=Path(td)/"moved-cycle"; (moved_cycle/"artifacts/spec/_internal/versions/v190").mkdir(parents=True)
   (artifact/"campaigns/2026-09-06_b").mkdir(); (artifact/"campaigns/2026-09-06_b/2026-09-06_b").symlink_to(moved_cycle)
   self.assertEqual(TX.next_version(open_bucket,artifact),191,"a symlinked cycle directory is followed")
   # the pre-W7I cycles/ layer
   (artifact/"campaigns/2026-08-01_old/cycles/cyc_1/artifacts/spec/_internal/versions/v200").mkdir(parents=True)
   self.assertEqual(TX.next_version(open_bucket,artifact),201,"the legacy cycles/ layer is on the chain")
   version,source=TX.chain_next(TX.version_history_trees(open_bucket,artifact))
   self.assertEqual((version,TX.version_source_layout(source,open_bucket,artifact)),(201,"cycle"))

 @unittest.skipIf(os.geteuid()==0,"permission bits do not bind root")
 def test_unwalkable_root_raises_a_typed_chain_error(self):
  # Review round 1 (blocking 1): the chain must fail closed, never count low.
  with tempfile.TemporaryDirectory() as td:
   artifact=Path(td)/".agent_reports"; open_bucket=artifact/"campaigns/2026-09-07_o/2026-09-07_o/artifacts/spec"; open_bucket.mkdir(parents=True)
   sealed=artifact/"campaigns/2026-09-06_a"; (sealed/"2026-09-06_a/artifacts/spec/_internal/versions/v9").mkdir(parents=True)
   os.chmod(sealed,0)
   try:
    with self.assertRaises(TX.VersionChainError) as ctx: TX.next_version(open_bucket,artifact)
    self.assertEqual(ctx.exception.reason,"version-chain-unenumerable")
   finally: os.chmod(sealed,0o755)
   self.assertEqual(TX.next_version(open_bucket,artifact),10)
   # owner finding F1: an unreadable `_internal/versions` of a walkable tree is typed as well
   versions=sealed/"2026-09-06_a/artifacts/spec/_internal/versions"; os.chmod(versions,0)
   try:
    with self.assertRaises(TX.VersionChainError): TX.next_version(open_bucket,artifact)
   finally: os.chmod(versions,0o755)

 @unittest.skipIf(os.geteuid()==0,"permission bits do not bind root")
 def test_unwalkable_root_is_a_typed_refusal(self):
  # Review round 1 (blocking 1, repro A): on a legacy-layout root nothing runs before the
  # transaction, so an unwalkable campaign dir used to surface as a bare traceback (exit 1)
  # inside the lock. In the cycle layout `check_write` refuses first (`cycle-unknown`) except
  # inside the lock-wait window, which this same path covers.
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,spec,route=self.fixture(root); (spec/"prd.md").write_text("before\n")
   broken=artifact/"campaigns/2026-01-01_broken"; (broken/"2026-01-01_broken/artifacts/spec").mkdir(parents=True); os.chmod(broken,0)
   events=root/"ev.jsonl"
   try: result=subprocess.run(self.command(root,artifact,route,"pass",events=events),text=True,capture_output=True,env=HERMETIC_ENV)
   finally: os.chmod(broken,0o755)
   self.assertEqual(result.returncode,65,result.stdout+result.stderr)
   rows=[json.loads(l) for l in events.read_text().splitlines()]
   self.assertEqual([(r["status"],r["reason"]) for r in rows if r["status"]=="blocked"],[("blocked","version-chain-unenumerable")])
   self.assertIn("PermissionError",[r for r in rows if r["status"]=="blocked"][0]["detail"])
   self.assertFalse(any(r["status"] in ("acquired","released") for r in rows),rows)
   self.assertFalse((spec/"_internal").exists(),"no snapshot on a refused run")
   self.assertEqual((artifact/".pipeline-lock").read_text(),"","the owner line is cleared")
   self.assertEqual((spec/"prd.md").read_text(),"before\n")

 def test_component_spec_root_owns_its_version_sequence(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td); artifact,component,route=self.fixture(root,component="component"); (component/"prd.md").write_text("before\n")
   code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('after\\n')"
   result=subprocess.run(self.command(root,artifact,route,code,spec_root=component),text=True,capture_output=True,env=HERMETIC_ENV)
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   self.assertEqual((component/"_internal/versions/v1/prd.md").read_text(),"before\n")
   self.assertFalse((artifact/"spec/_internal/versions/v1").exists())

class CycleLayoutTest(unittest.TestCase):
 """W7C cycle layout (defect K): the transaction seeds the empty cycle spec
 root from the latest shared revision so the snapshot comes from the tool,
 and a child that writes the legacy bucket is a typed failure."""

 def setUp(self):
  sys.path.insert(0,str(ROOT/"utilities"))
  import artifact_producer as P, artifact_lifecycle as L
  self.P,self.L=P,L
  self.PT=load("producer_test_for_spec_tx",ROOT/"utilities/artifact_producer.test.py")
  self._tmp=tempfile.TemporaryDirectory(); base=Path(self._tmp.name)
  self.repo=base/"repo"; self.repo.mkdir()
  subprocess.run(["git","init","-q",str(self.repo)],check=True)
  subprocess.run(["git","-C",str(self.repo),"config","user.email","f@x"],check=True)
  subprocess.run(["git","-C",str(self.repo),"config","user.name","F"],check=True)
  (self.repo/"README").write_text("x\n"); subprocess.run(["git","-C",str(self.repo),"add","README"],check=True); subprocess.run(["git","-C",str(self.repo),"commit","-qm","init"],check=True)
  self.artifact=self.repo/".agent_reports"; self.artifact.mkdir()
  home=base/"agent-home"; (home/"core").mkdir(parents=True); (home/"core"/"CORE.md").write_text("fixture\n")
  self._env={k:os.environ.get(k) for k in ("AGENT_HOME","AGENT_DISPATCH_JOBS","AGENT_ARTIFACT_CYCLE_DIR","AGENT_ARTIFACT_ROOT")}
  os.environ["AGENT_HOME"]=str(home)
  for k in ("AGENT_DISPATCH_JOBS","AGENT_ARTIFACT_CYCLE_DIR","AGENT_ARTIFACT_ROOT"): os.environ.pop(k,None)
  P.activate(self.artifact,repository_id="repo_"+"a"*32,artifact_root_id="root_"+"b"*32,w7={"campaign_id":"camp_"+"c"*32})
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  spec_route=R.compile_route("autopilot-spec","update","strong",self.repo,self.artifact,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=dispatch(self.repo),slug="spec-tx")
  self.spec_route=self.repo/"route.json"; self.spec_route.write_text(json.dumps(spec_route))

 def tearDown(self):
  for k,v in self._env.items():
   if v is None: os.environ.pop(k,None)
   else: os.environ[k]=v
  self._tmp.cleanup()

 def _cycle(self, slug):
  route=self.PT.compile_for("direct",self.artifact,"autopilot-code","dev",slug=slug)
  binding=self.L.admit_runtime_route(self.artifact,route)
  begun=self.P.begin(self.artifact,route_file=Path(binding.route_file),capability="autopilot-code",intensity="direct")
  return route,Path(binding.route_file),begun

 def _close(self, route, route_file):
  evidence=Path(self._tmp.name)/f"ev-{route['route_id']}.txt"; evidence.write_text("ok\n")
  for node in route["nodes"]:
   if node.get("terminal") is True: R.write_completion_marker(route,node,node["id"],evidence)
  R.close_route(route,route_file,commit="a"*40,summary="fixture")

 def _shared_v1(self):
  route,route_file,begun=self._cycle("seed-source")
  spec=Path(begun["cycle_dir"])/"artifacts"/"spec"; (spec/"_internal"/"versions"/"v1").mkdir(parents=True)
  (spec/"prd.md").write_text("v1\n"); (spec/"_internal"/"versions"/"v1"/"prd.md").write_text("v0\n"); (spec/"pipeline_state.yaml").write_text("s\n")
  self._close(route,route_file); self.P.finalize(self.artifact,cycle_id=begun["cycle_id"])
  return self.P.admit_shared(self.artifact,cycle_id=begun["cycle_id"],kind="spec",source="spec",key="spec")

 def _run(self, cycle_dir, code, events):
  env={**HERMETIC_ENV,"AGENT_ARTIFACT_CYCLE_DIR":str(cycle_dir),"AGENT_ARTIFACT_ROOT":str(self.artifact)}
  cmd=[sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--artifact-root",str(self.artifact),"--worktree",str(self.repo),"--route",str(self.spec_route),"--node","prd-transaction","--events",str(events),"--",sys.executable,"-c",code]
  return subprocess.run(cmd,text=True,capture_output=True,env=env)

 def test_cycle_spec_is_seeded_and_snapshot_comes_from_the_tool(self):
  self._shared_v1()
  _r,_f,begun=self._cycle("spec-edit"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev.jsonl"
  code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v2\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  seeded=[r for r in rows if r["status"]=="seeded"]
  self.assertEqual(len(seeded),1); self.assertEqual(seeded[0]["files"],3)
  spec=cycle_dir/"artifacts"/"spec"
  self.assertEqual((spec/"prd.md").read_text(),"v2\n")
  self.assertEqual((spec/"_internal"/"versions"/"v1"/"prd.md").read_text(),"v0\n")   # seeded history
  self.assertEqual((spec/"_internal"/"versions"/"v2"/"prd.md").read_text(),"v1\n")   # tool-written snapshot
  self.assertEqual((spec/"pipeline_state.yaml").read_text(),"s\n")                   # whole tree seeded (D-87)
  self.assertTrue(any(r["status"]=="snapshot" and r["version"]==2 for r in rows),rows)

 def test_sd_open_50_worker_research_residue_without_a_prd_still_seeds(self):
  # H1 (cairn v173 owner, 2026-09-07): the route's spec write scope let the
  # research worker write `_internal/research/**` before the transaction, so
  # the any-file predicate skipped the seed -> no pre-image, next_version=1,
  # no snapshot. The predicate is "prd.md absent"; residue is preserved.
  self._shared_v1()
  _r,_f,begun=self._cycle("spec-edit-residue"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev-residue.jsonl"
  spec=cycle_dir/"artifacts"/"spec"; note=spec/"_internal"/"research"/"note.md"; note.parent.mkdir(parents=True); note.write_text("worker wrote this first\n")
  code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  seeded=[r for r in rows if r["status"]=="seeded"]
  self.assertEqual(len(seeded),1,rows); self.assertEqual((seeded[0]["files"],seeded[0]["preexisting_files"],seeded[0]["kept_existing"]),(3,1,0))
  self.assertFalse(any(r["status"]=="seed-skipped" for r in rows),rows)
  self.assertEqual([r["next_version"] for r in rows if r["status"]=="acquired"],[2])
  self.assertEqual((spec/"_internal"/"versions"/"v2"/"prd.md").read_text(),"v1\n")   # pre-image snapshot from the tool
  self.assertEqual((spec/"prd.md").read_text(),"v2\n")
  self.assertEqual(note.read_text(),"worker wrote this first\n")                      # residue preserved
  # a prd.md already present still skips the seed, and says the prd is there
  _r,_f,begun2=self._cycle("spec-edit-present"); cycle2=Path(begun2["cycle_dir"]); events2=Path(self._tmp.name)/"ev-present.jsonl"
  spec2=cycle2/"artifacts"/"spec"; spec2.mkdir(parents=True); (spec2/"prd.md").write_text("v1\n"); (spec2/"_internal"/"versions"/"v1").mkdir(parents=True); (spec2/"_internal"/"versions"/"v1"/"prd.md").write_text("v0\n")
  result=self._run(cycle2,code,events2)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events2.read_text().splitlines()]
  # review finding 6: a present prd.md is a label, not an early return -- the
  # copy loop still fills what the bucket lacks (here pipeline_state.yaml) and
  # keeps the bucket's own prd.md and history untouched.
  seeded=[r for r in rows if r["status"]=="seeded"]
  self.assertEqual([(r["prd_present"],r["files"],r["kept_existing"],sorted(r["kept_existing_paths"])) for r in seeded],
                   [(True,1,2,["_internal/versions/v1/prd.md","prd.md"])])
  # the v{N} chain spans cycle buckets: the first cycle snapshotted v2, so this one is v3
  self.assertEqual((spec2/"prd.md").read_text(),"v3\n"); self.assertEqual((spec2/"_internal"/"versions"/"v3"/"prd.md").read_text(),"v1\n")

 def test_sd_open_50_partial_seed_is_completed_and_component_scope_judges_its_own_prd(self):
  # review finding 6: the predicate is a label, not an early return -- a bucket
  # that holds only prd.md (crash mid-seed) or another component's prd.md is
  # completed by the copy loop, and `kept_existing` keeps it idempotent.
  self._shared_v1()
  with tempfile.TemporaryDirectory() as td:
   base=Path(td)/"partial"; base.mkdir(); (base/"prd.md").write_text("v1\n")
   first=TX.seed_cycle_spec(base,self.artifact)
   self.assertEqual((first["status"],first["prd_present"],first["kept_existing"],first["kept_existing_paths"]),("seeded",True,1,["prd.md"]))
   self.assertTrue((base/"pipeline_state.yaml").is_file()); self.assertTrue((base/"_internal"/"versions"/"v1"/"prd.md").is_file())
   again=TX.seed_cycle_spec(base,self.artifact)
   self.assertEqual((again["status"],again["reason"],again["kept_existing"]),("seed-skipped","prd-present",3))
   scoped=Path(td)/"scoped"; (scoped/"componentB").mkdir(parents=True); (scoped/"componentB"/"prd.md").write_text("b\n")
   result=TX.seed_cycle_spec(scoped,self.artifact,spec_root=scoped/"componentA")
   self.assertEqual((result["status"],result["prd_present"],result["files"]),("seeded",False,3))
   self.assertEqual((scoped/"componentB"/"prd.md").read_text(),"b\n")

 def test_seed_unions_version_history_across_revisions(self):
  # Latest revision carries the PRD but no history (cairn's rrev_511a shape);
  # an earlier revision holds _internal/versions/v3. The counter must continue at 4.
  self._shared_v1()
  route,route_file,begun=self._cycle("seed-source-2")
  spec=Path(begun["cycle_dir"])/"artifacts"/"spec"; spec.mkdir(parents=True)
  (spec/"prd.md").write_text("v3\n"); (spec/"pipeline_state.yaml").write_text("s\n")
  self._close(route,route_file); self.P.finalize(self.artifact,cycle_id=begun["cycle_id"])
  # cairn's rrev_511a shape (3 files, history dropped) predates D-87; model it explicitly.
  self.P.admit_shared(self.artifact,cycle_id=begun["cycle_id"],kind="spec",source="spec",key="spec",drop_components=["_internal"],drop_reason="fixture: pre-D-87 shape")
  _r,_f,begun=self._cycle("spec-edit-3"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev3.jsonl"
  code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v4\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  seeded=[r for r in rows if r["status"]=="seeded"][0]
  self.assertEqual(seeded["history_versions"],1)
  spec=cycle_dir/"artifacts"/"spec"
  self.assertEqual((spec/"_internal"/"versions"/"v1"/"prd.md").read_text(),"v0\n")
  self.assertEqual((spec/"_internal"/"versions"/"v2"/"prd.md").read_text(),"v3\n")
  self.assertEqual((spec/"prd.md").read_text(),"v4\n")
  self.assertFalse(any(r["status"]=="version-history-absent" for r in rows))


 def test_legacy_only_chain_continues_in_the_first_cutover_cycle(self):
  # The cairn W13 shape: cutover active, no shared revision yet, the whole
  # chain lives in the legacy read-only bucket (v1..v168). The seed is
  # skipped and the counter used to restart at v1.
  legacy=self.artifact/"spec"; (legacy/"_internal"/"versions"/"v168").mkdir(parents=True); (legacy/"prd.md").write_text("v168 body\n")
  _r,_f,begun=self._cycle("first-cutover-cycle"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev5.jsonl"
  spec=cycle_dir/"artifacts"/"spec"; spec.mkdir(parents=True,exist_ok=True); (spec/"prd.md").write_text("v168 body\n")
  code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  self.assertEqual([r["reason"] for r in rows if r["status"]=="seed-skipped"],["prd-present"])
  self.assertEqual([r["next_version"] for r in rows if r["status"]=="acquired"],[169])
  self.assertFalse(any(r["status"]=="version-history-absent" for r in rows),rows)
  self.assertEqual((spec/"_internal"/"versions"/"v169"/"prd.md").read_text(),"v168 body\n")
  self.assertEqual((spec/"prd.md").read_text(),"v169\n")
  self.assertFalse((spec/"_internal"/"versions"/"v1").exists(),"the chain must not restart at v1")
  self.assertFalse((legacy/"_internal"/"versions"/"v169").exists(),"legacy stays read-only")


 def test_empty_first_cutover_bucket_with_no_shared_revision_continues_the_legacy_chain(self):
  # Exact cairn W13 shape: empty open bucket, no shared revision, legacy holds v1..v168.
  legacy=self.artifact/"spec"; (legacy/"_internal"/"versions"/"v168").mkdir(parents=True); (legacy/"prd.md").write_text("v168 body\n")
  _r,_f,begun=self._cycle("first-cutover-cycle-empty"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev6.jsonl"
  self.assertFalse((cycle_dir/"artifacts"/"spec").exists(),"the producer creates the bucket lazily; the transaction must cope with an absent spec root")
  code="import os; from pathlib import Path; r=Path(os.environ['AGENT_SPEC_ROOT']); r.mkdir(parents=True,exist_ok=True); (r/'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  self.assertEqual([r["reason"] for r in rows if r["status"]=="seed-skipped"],["no-shared-revision"])
  self.assertEqual([(r["next_version"],r["version_source_layout"]) for r in rows if r["status"]=="acquired"],[(169,"legacy")])
  released=[r for r in rows if r["status"]=="released"][0]
  self.assertEqual((released["version"],released["snapshot"]),(169,"not-required-new"))
  self.assertEqual((cycle_dir/"artifacts"/"spec"/"prd.md").read_text(),"v169\n")
  self.assertFalse((cycle_dir/"artifacts"/"spec"/"_internal").exists(),"no pre-image, no snapshot; the number alone continues")


 def test_unadmitted_sealed_cycle_still_advances_the_chain(self):
  # Cycle A snapshots v2 but is sealed without admit_shared; cycle B seeds
  # from the shared v1 revision and must not reuse v2.
  self._shared_v1()
  route,route_file,begun=self._cycle("spec-edit-a"); cycle_a=Path(begun["cycle_dir"]); ev_a=Path(self._tmp.name)/"ev-a.jsonl"
  code="import os; from pathlib import Path; Path(os.environ['AGENT_SPEC_ROOT'],'prd.md').write_text('v'+os.environ['AGENT_SPEC_NEXT_VERSION']+'\\n')"
  result=self._run(cycle_a,code,ev_a); self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertTrue((cycle_a/"artifacts"/"spec"/"_internal"/"versions"/"v2"/"prd.md").is_file())
  self._close(route,route_file); self.P.finalize(self.artifact,cycle_id=begun["cycle_id"])
  _r,_f,begun=self._cycle("spec-edit-b"); cycle_b=Path(begun["cycle_dir"]); ev_b=Path(self._tmp.name)/"ev-b.jsonl"
  result=self._run(cycle_b,code,ev_b); self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  rows=[json.loads(l) for l in ev_b.read_text().splitlines()]
  acquired=[r for r in rows if r["status"]=="acquired"]
  self.assertEqual([r["next_version"] for r in acquired],[3])
  self.assertEqual(acquired[0]["version_source_layout"],"cycle"); self.assertEqual(Path(acquired[0]["version_source"]),cycle_a/"artifacts"/"spec")
  spec_b=cycle_b/"artifacts"/"spec"
  self.assertEqual((spec_b/"_internal"/"versions"/"v3"/"prd.md").read_text(),"v1\n")
  self.assertFalse((spec_b/"_internal"/"versions"/"v2").exists(),"the seeded copy carries only shared history; v2 lives in cycle A")
  self.assertEqual((spec_b/"prd.md").read_text(),"v3\n")

 def test_refused_route_seeds_nothing(self):
  self._shared_v1()
  _r,_f,begun=self._cycle("spec-edit-4"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev4.jsonl"
  env={**HERMETIC_ENV,"AGENT_ARTIFACT_CYCLE_DIR":str(cycle_dir),"AGENT_ARTIFACT_ROOT":str(self.artifact)}
  cmd=[sys.executable,str(ROOT/"utilities/spec-transaction.py"),"run","--artifact-root",str(self.artifact),"--worktree",str(self.repo),"--route",str(self.spec_route),"--node","no-such-node","--events",str(events),"--",sys.executable,"-c","pass"]
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,65,result.stdout+result.stderr)
  self.assertEqual([p for p in (cycle_dir/"artifacts"/"spec").rglob("*") if p.is_file()],[])
  self.assertFalse(any(json.loads(l)["status"]=="seeded" for l in events.read_text().splitlines()))

 def test_child_writing_legacy_spec_is_a_typed_failure(self):
  self._shared_v1()
  legacy=self.artifact/"spec"; legacy.mkdir(); (legacy/"prd.md").write_text("stale\n")
  _r,_f,begun=self._cycle("spec-edit-2"); cycle_dir=Path(begun["cycle_dir"]); events=Path(self._tmp.name)/"ev2.jsonl"
  code=f"from pathlib import Path; Path({str(legacy/'prd.md')!r}).write_text('rogue\\n')"
  result=self._run(cycle_dir,code,events)
  self.assertEqual(result.returncode,65,result.stdout+result.stderr)
  rows=[json.loads(l) for l in events.read_text().splitlines()]
  blocked=[r for r in rows if r["status"]=="blocked" and r["reason"]=="legacy-spec-written"]
  self.assertEqual(len(blocked),1); self.assertEqual(blocked[0]["changed"],["prd.md"])


if __name__=="__main__":
 unittest.main()
