#!/usr/bin/env python3
import importlib.util, io, json, os, subprocess, sys, tempfile, time, unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
S=importlib.util.spec_from_file_location("route",ROOT/"utilities/capability-route.py"); R=importlib.util.module_from_spec(S); S.loader.exec_module(R)
F_SPEC=importlib.util.spec_from_file_location("fallback",ROOT/"utilities/stage-dispatch-fallback.py"); F=importlib.util.module_from_spec(F_SPEC); F_SPEC.loader.exec_module(F)

import contextlib


@contextlib.contextmanager
def dispatch_defaults_config_text(text):
 """Point the sealed dispatch-defaults loader at a fixture config."""
 with tempfile.TemporaryDirectory() as td:
  path=Path(td)/"dispatch-defaults.yaml"; path.write_text(text,encoding="utf-8")
  previous=os.environ.get("DISPATCH_DEFAULTS_CONFIG")
  os.environ["DISPATCH_DEFAULTS_CONFIG"]=str(path)
  try: yield path
  finally:
   if previous is None: os.environ.pop("DISPATCH_DEFAULTS_CONFIG",None)
   else: os.environ["DISPATCH_DEFAULTS_CONFIG"]=previous


class FallbackTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); base=Path(self.tmp.name); self.repo=base/"repo"; self.repo.mkdir()
  subprocess.run(["git","init","-q",str(self.repo)],check=True); subprocess.run(["git","-C",str(self.repo),"config","user.email","fixture@example.com"],check=True); subprocess.run(["git","-C",str(self.repo),"config","user.name","Fixture"],check=True)
  (self.repo/"x").write_text("x"); subprocess.run(["git","-C",str(self.repo),"add","x"],check=True); subprocess.run(["git","-C",str(self.repo),"commit","-qm","init"],check=True)
  self.art=base/".agent_reports"; self.art.mkdir(); self.jobs=base/"jobs.log"
  self.previous_dispatch_defaults=os.environ.get("DISPATCH_DEFAULTS_CONFIG")
  os.environ["DISPATCH_DEFAULTS_CONFIG"]=str(ROOT/"profiles/dispatch-defaults.yaml")
  self.owner=subprocess.Popen(["sleep","60"])
 def tearDown(self):
  if self.owner.poll() is None:self.owner.kill()
  self.owner.wait();self.tmp.cleanup()
  if self.previous_dispatch_defaults is None:
   os.environ.pop("DISPATCH_DEFAULTS_CONFIG",None)
  else:
   os.environ["DISPATCH_DEFAULTS_CONFIG"]=self.previous_dispatch_defaults
 def seed_parent(self,harness="codex",sandbox="workspace-write"):
  """Append the live dispatch-depth-1 owner row the depth-2 launch resolves.

  Appends rather than short-circuits on an existing file: a dry-run now
  resolves this row exactly as --start does, so a test that writes its own
  registry rows still needs the parent present.
  """
  if "worker_type=owner" in (self.jobs.read_text() if self.jobs.exists() else ""):return
  start=(Path("/proc")/str(self.owner.pid)/"stat").read_text().split()[21]
  with self.jobs.open("a",encoding="utf-8") as fh:
   fh.write(
    f"2026-07-23T00:00:00Z\topen\t{self.repo}\t{self.repo}\towner\t"
    "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
    f"harness={harness},runtime_sandbox={sandbox},"
    "execution_surface=registered-headless,registered_worker=1,"
    "fallback_hop=same-harness-headless,worker_type=owner,"
    f"attempt_id=att-fallback-parent,pid={self.owner.pid},pid_start={start}\n")
 def tuple(self,child,status):
  return {"parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write","child_harness":child,"launch_authority":"conductor","status":status,"probe_source":"fixture","probe_time":"2026-07-16T00:00:00Z","failure_class":"nested-network-unconfirmed" if status!="supported" else "","checked_worktree":str(self.repo.resolve()),"failure_scope":"runtime-global" if status!="supported" else "none","codex_command":"ok" if child=="codex" else "not-applicable","retry_on_isolated_worktree":0}
 def cli_verdict(self,stdout):
  """This wrapper's OWN verdict block, parsed as key=value.

  A successful chain prints two blocks: this wrapper's verdict, then the child
  wrapper's relayed output, which repeats several keys with the child's values
  (`attempt_id=-` under `preview=1`, its own `job_registry`, ...). Folding the
  whole stream into one dict makes the last producer win and silently answers a
  question about this wrapper with the child's value. The second `check=` line
  is the block boundary; before it is this wrapper's own verdict.
  """
  lines=stdout.splitlines()
  starts=[i for i,line in enumerate(lines) if line.startswith("check=")]
  own=lines[:starts[1]] if len(starts)>1 else lines
  return dict(line.split("=",1) for line in own if "=" in line)
 def launch_roots_env(self):
  """The env keys `launch_compatibility_tuple` reads. Seal and launch share it."""
  return {"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),
          "AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),
          "AGENT_DISPATCH_JOBS":str(self.jobs)}
 def route(self,native="unsupported",same_status="unsupported",intensity="strong"):
  """Compile the fixture route under the SAME runtime root the launch uses.

  `launch_compatibility_tuple` seals the runtime root, the artifact root and
  the registry path that `resolve_agent_home()`/`resolve_global_registry()`
  answer at compile time. Sealing them from whatever this test process
  inherited (a developer's installed release and their live
  `~/.codex/.harness/dispatch/jobs.log`, or -- with nothing installed, as under
  the isolated suite profile and CI -- the nonexistent
  `$XDG_DATA_HOME/hearting/current`) and then launching through `run_chain`,
  which names this checkout and the fixture registry, is exactly the
  two-sources-one-value shape the dry run's `launch-runtime-root-mismatch`
  check exists to refuse. It was refusing correctly; the fixture was sealing
  and launching against different roots. Pinning the compile to `run_chain`'s
  own env also stops the fixture from reading the installed release and the
  live registry -- runtime-owned state -- at all.
  """
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec","workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  evidence={"tuples":[self.tuple("codex",same_status),self.tuple("claude","supported")],"native_subagent":[{
   "harness":"codex","transport":"headless",
   "execution_surface":"codex-native-subagent","registered_worker":False,
   "status":native,"check_source":"fixture"}]}
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   route=R.compile_route("autopilot-code","dev",intensity,self.repo,self.art,signals=["shared-contract"],transport="headless",tracking="tracked",tracked_gate_evidence=gate,dispatch_evidence=evidence)
  plan=next(node for node in route.get("nodes",[]) if node.get("id")=="plan")
  if plan.get("depends_on"):
   # These fallback-ranking fixtures do not test the frame entry gate.
   plan["depends_on"]=[]
   route["route_hash"]=R.route_hash(route)
   route["route_id"]=R.ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
  path=Path(self.tmp.name)/"route.json"; path.write_text(json.dumps(route),encoding="utf-8"); return path
 def seed_plan_marker(self,route):
  """Review placement fixtures have a genuine current producer input (SD-161)."""
  plan=next(n for n in route["nodes"] if n["id"]=="plan")
  evidence=self.art/"fixture-plan.md"
  evidence.write_text("Fixture plan input.\n")
  with mock.patch.dict(os.environ,self.launch_roots_env()), \
       mock.patch.object(R,"_launch_open_cycle_checkpoint"):
   R.complete_node(route,plan,"plan",evidence,attempt_id="att-fixture-plan",
     explicit_attempt_metadata={"attempt_schema_version":2,"dispatch_depth":2,
       "transport":"headless","execution_surface":"inline",
       "registered_worker":False,"fallback_hop":"inline"})
  return evidence
 def seed_predecessor_markers(self,path,node_id):
  """Publish real linked inline markers so dry-run tests exercise their
  intended launch-selection behavior with the same gate state as --start."""
  route=json.loads(Path(path).read_text(encoding="utf-8"))
  nodes={node["id"]:node for node in route.get("nodes",[])}
  target=nodes.get(node_id,{})
  entry_gates={
   binding.get("gate") for binding in route.get("human_gate_bindings",[])
   if binding.get("node")==node_id and binding.get("position","entry")=="entry"
   and any(node.get("continuation",{}).get("kind")=="human-gate"
           and node.get("continuation",{}).get("gate")==binding.get("gate")
           for node in route.get("nodes",[]))
  }
  if entry_gates:
   import workflow_state as WS
   self.jobs.parent.mkdir(parents=True,exist_ok=True)
   self.jobs.touch(exist_ok=True)
   ledger=WS.WorkflowLedger(route["route_id"],route["route_hash"],jobs=self.jobs)
   for gate in sorted(entry_gates):
    if ledger.read_only_state().get("workflow_state")=="RUNNING":
     continue
    with ledger.lock():
     ledger.set_workflow_state("READY",evidence={},actor="dry-run-fixture")
     ledger.set_workflow_state(
      "BLOCKED_HUMAN_GATE",evidence={"gate":gate,"artifact":"fixture.md"},
      actor="dry-run-fixture",
     )
     ledger.set_workflow_state(
      "RUNNING",evidence={"released_gate":gate,"released_by":"fixture",
                           "actor_kind":"user","decision":"proceed"},
      actor="dry-run-fixture",
     )
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   for dep in target.get("depends_on",[]):
    predecessor=nodes.get(dep)
    if predecessor is None:
     continue
    evidence=self.art/"_internal"/"dry-run-predecessors"/f"{dep}.md"
    evidence.parent.mkdir(parents=True,exist_ok=True)
    evidence.write_text(f"fixture predecessor {dep}\n",encoding="utf-8")
    R._publish_completion_locked(
     route,predecessor,dep,evidence,jobs=self.jobs,
     attempt_id=f"att-dry-run-{route['route_id']}-{dep}",
     attempt_metadata={
      "attempt_schema_version":2,
      "dispatch_depth":predecessor.get("dispatch_depth",2),
      "transport":"interactive","execution_surface":"inline",
      "registered_worker":False,"fallback_hop":"inline",
     },
    )
 def run_chain(self,path,*extra,seed=True,**envkw):
  self.seed_predecessor_markers(path,"plan")
  if seed:self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node","plan","--slug","fallback-plan","--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author","--model-role","deep maker","--jobs",str(self.jobs),"--dry-run",*extra]
  clean={k:v for k,v in os.environ.items() if not k.startswith("AGENT_DISPATCH_CURRENT_")}
  env={**clean,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",**envkw}
  return subprocess.run(cmd,text=True,capture_output=True,env=env)
 @contextlib.contextmanager
 def dispatch_env(self,**envkw):
  """The same env `run_chain` injects into its subprocess, applied to THIS
  process instead (B47-1/2/5/6/7/10 fixtures). Must wrap both the route
  compile (`self.route()`) and `run_inline()` -- the grounding tuple baked
  into route.json at compile time must see the same `AGENT_HOME`/
  `AGENT_DISPATCH_JOBS`/`AGENT_ARTIFACT_ROOT` that `_dispatch()` resolves
  against later, or the dry-run's own root-mismatch check (correctly) refuses.
  Also strips any ambient `AGENT_DISPATCH_CURRENT_*` this test process happens
  to carry, matching `run_chain`'s `clean` filter.
  """
  removed={k:os.environ.pop(k) for k in list(os.environ) if k.startswith("AGENT_DISPATCH_CURRENT_")}
  env={**self.launch_roots_env(),
       "AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",**envkw}
  try:
   with mock.patch.dict(os.environ,env):
    yield
  finally:
   os.environ.update(removed)
 def run_inline(self,path,*extra,seed=True):
  """In-process `_dispatch()` call -- never spawns a subprocess, so it also
  lets `mock.patch.object(F, ...)` (e.g. `_usage_states`) reach the code
  under test. Call inside `with self.dispatch_env(): ...`.
  """
  self.seed_predecessor_markers(path,"plan")
  if seed:self.seed_parent()
  argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan","--slug","fallback-plan",
        "--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author",
        "--model-role","deep maker","--jobs",str(self.jobs),"--dry-run",*extra]
  with mock.patch.object(sys,"argv",argv):
   observation=F.LAUNCH_TUPLE.ReportOnlyObservation()
   code=F._dispatch(observation)
  return code,observation
 def run_inline_main(self,path,*extra,seed=True):
  """Like `run_inline()` but through `F.main()` -- exercises the try/finally
  report-only wrapper (B47-5), not just `_dispatch()`."""
  self.seed_predecessor_markers(path,"plan")
  if seed:self.seed_parent()
  argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan","--slug","fallback-plan",
        "--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author",
        "--model-role","deep maker","--jobs",str(self.jobs),"--dry-run",*extra]
  with mock.patch.object(sys,"argv",argv):
   code=F.main()
  return code
 def ledger_rows(self,route_id):
  path=self.jobs.parent/"launch-tuple"/f"{route_id}.jsonl"
  if not path.is_file():return []
  return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
 def report_rows(self,route_id):
  path=self.jobs.parent/"launch-tuple"/"_report"/f"{route_id}.jsonl"
  if not path.is_file():return []
  return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
 def run_register(self,path):
  self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node","plan","--slug","fallback-plan","--parent","owner","--capability-mode","dev","--worker-mode","plan/plan-author","--model-role","deep maker","--jobs",str(self.jobs),"--register"]
  env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent"}
  return subprocess.run(cmd,text=True,capture_output=True,env=env)
 def run_review_inline(self,path,node_id="plan-check",worker_mode="qa/plan-review",model_role="fast reviewer",seed=True):
  """`run_inline` for a capped review node (C-14). In-process like run_inline:
  the subprocess `run_chain` path additionally binds a launch runtime root,
  which is a separate axis this fixture does not need to exercise."""
  self.seed_predecessor_markers(path,node_id)
  if seed:self.seed_parent()
  if node_id=="plan-check":self.seed_plan_marker(json.loads(path.read_text()))
  argv=["stage-dispatch-fallback.py","--route",str(path),"--node",node_id,"--slug",f"fallback-{node_id}",
        "--parent","owner","--capability-mode","dev","--worker-mode",worker_mode,
        "--model-role",model_role,"--jobs",str(self.jobs),"--dry-run"]
  printed=[]
  with mock.patch.object(sys,"argv",argv), \
       mock.patch("builtins.print",side_effect=lambda *a,**k:printed.append(" ".join(map(str,a)))):
   observation=F.LAUNCH_TUPLE.ReportOnlyObservation()
   try:
    code=F._dispatch(observation)
   except SystemExit as exc:
    code=exc.code
  return code,printed
 def seed_review_rounds(self,route_id,node_id,count):
  """Prior closed rounds in the production registry shape (`route_id=`).

  SD-153: a review verdict must be a real blocking finding, not a crash --
  `completed-review-blocking` spends the round budget the way `dead-worker-
  fail` (a crash, never a review verdict) no longer does on its own.
  """
  with self.jobs.open("a",encoding="utf-8") as fh:
   for i in range(1,count+1):
    fh.write(
     f"2026-08-29T00:00:0{i}Z\tdone\t{self.repo}\t{self.repo}\tround-{node_id}-{i}\t"
     "attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
     f"route_id={route_id},route_node={node_id},note=completed-review-blocking,"
      f"attempt_id=att-{node_id}-round-{i}\n")

 def continuation_review_route(self,resume_from_node="plan-check"):
  """Publish a real continuation that retains a runnable plan-check node."""
  evidence={"tuples":[self.tuple("codex","supported"),self.tuple("claude","supported")],
   "native_subagent":[{"harness":"codex","transport":"headless",
    "execution_surface":"codex-native-subagent","registered_worker":False,
    "status":"supported","check_source":"fixture"}]}
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   source=R.compose_route(
    capability="autopilot-code",capability_mode="dev",shape="staged",
    graph="frame,plan,plan-check",slug="continuation-plan-check",
    cwd=self.repo,artifact_root=self.art,intensity="standard",
    signals=["shared-contract"],tracking="tracked",parent_harness="codex",
    dispatch_evidence=evidence,unassigned=True,
   )
  R.write_once(R.canonical_route_path(self.art,source["route_id"]),source)
  boundary=next(i for i,node in enumerate(source["nodes"]) if node["id"]==resume_from_node)
  for node in source["nodes"][:boundary]:
   attempt_id=f"att-source-{node['id']}"
   evidence=self.art/"_internal"/"continuation-source"/f"{node['id']}.md"
   evidence.parent.mkdir(parents=True,exist_ok=True)
   evidence.write_text(f"source completion for {node['id']}\n",encoding="utf-8")
   metadata={
    "attempt_schema_version":2,"dispatch_depth":node["dispatch_depth"],
    "transport":"headless","execution_surface":"registered-headless",
    "registered_worker":"1","fallback_hop":"same-harness-headless",
   }
   with mock.patch.dict(os.environ,self.launch_roots_env()), \
        mock.patch.object(R,"_launch_open_cycle_checkpoint"):
    R.complete_node(source,node,node["id"],evidence,attempt_id=attempt_id,
                    explicit_attempt_metadata=metadata)
   jobs=self.jobs
   link_path=R._attempt_completion_path(source,node["id"],attempt_id,jobs=jobs)
   link=json.loads(link_path.read_text(encoding="utf-8"))
   link.update({"verdict":"PASS","quiescence_proof_digest":"sha256:"+"a"*64,
                "last_turn_id":f"turn-{node['id']}"})
   R.atomic_write(link_path,link)
   with jobs.open("a",encoding="utf-8") as handle:
    handle.write("\t".join([
     "2026-09-01T00:00:00Z","done",str(self.repo),str(self.repo),"source-prefix",
     f"route_id={source['route_id']},route_node={node['id']},attempt_id={attempt_id}",
    ])+"\n")
  import workflow_state as WS
  ledger=WS.WorkflowLedger(source["route_id"],source["route_hash"],jobs=self.jobs)
  for node in source["nodes"][:boundary]:
   continuation=node.get("continuation") or {}
   if continuation.get("kind")!="human-gate":
    continue
   gate=continuation["gate"]
   if WS.human_gate_resolution(ledger.journal(),gate)["status"]!="not-raised":
    continue
   with ledger.lock():
    if ledger.state()["workflow_state"]=="CREATED":
     ledger.set_workflow_state("READY",evidence={},actor="continuation-fixture")
    ledger.set_workflow_state("BLOCKED_HUMAN_GATE",
     evidence={"gate":gate,"artifact":"fixture.md"},actor="continuation-fixture")
    ledger.set_workflow_state("RUNNING",evidence={"released_gate":gate,
     "decision":"proceed","released_by":"fixture-user","actor_kind":"user"},
     actor="continuation-fixture")
  first=R.build_continuation_route(
   source,resume_from_node=resume_from_node,requested_boundary=resume_from_node,
   reason="resume-review-boundary",artifact_root=self.art,
  )
  first_path=R.canonical_route_path(self.art,first["route_id"])
  R.publish_continuation_route(first,source,first_path)
  return source,first,first_path

 def run_continuation_review_action(self,path,action,reviewed_evidence=None):
  self.seed_predecessor_markers(path,"plan")
  self.seed_predecessor_markers(path,"plan-check")
  route=json.loads(Path(path).read_text(encoding="utf-8"))
  if any(node["id"]=="plan" for node in route["nodes"]):
   self.seed_plan_marker(route)
   if reviewed_evidence is None:
    marker_path=R.completion_dir(route["route_id"],jobs=self.jobs)/"plan.json"
    reviewed_evidence=json.loads(marker_path.read_text(encoding="utf-8"))["evidence"]["path"]
  self.seed_parent()
  argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan-check",
        "--slug",f"continuation-plan-check-{action}","--parent","owner",
        "--capability-mode","dev","--worker-mode","qa/plan-review",
        "--model-role","fast reviewer","--jobs",str(self.jobs),f"--{action}"]
  if reviewed_evidence:
   argv.extend(["--reviewed-evidence",str(reviewed_evidence)])
  spawned=[]; printed=[]
  from types import SimpleNamespace
  real_run=subprocess.run
  def run(cmd,**kwargs):
   if any(str(part).endswith("/bin/dispatch-headless.py") for part in cmd):
    spawned.append(cmd)
    receipt=("check=ok\nregistered=0\nstarted=0\nchild_spawned=0\n" if "--dry-run" in cmd else
             "check=ok\nregistered=1\nstarted=1\nchild_spawned=1\nattempt_id=att-mocked-child\n")
    return SimpleNamespace(returncode=0,stdout=receipt,stderr="")
   return real_run(cmd,**kwargs)
  with mock.patch.object(sys,"argv",argv), \
       mock.patch("builtins.print",side_effect=lambda *a,**k:printed.append(" ".join(map(str,a)))), \
       mock.patch("subprocess.run",side_effect=run), \
       mock.patch.object(F,"watch_launched_attempt",return_value=("observed",{})):
   observation=F.LAUNCH_TUPLE.ReportOnlyObservation()
   try:
    code=F._dispatch(observation)
   except SystemExit as exc:
    code=exc.code
  return code,printed,spawned

 def seed_bound_review_rounds(self,route,node_id,count,reviewed_evidence):
  """Write valid completed verdict rows and their exact review-input bindings."""
  from review_input import _file,seal_binding
  candidate=_file(reviewed_evidence)
  with self.jobs.open("a",encoding="utf-8") as handle:
   for index in range(1,count+1):
    attempt_id=f"att-bound-{route['route_id']}-{node_id}-{index}"
    metadata={"attempt_id":attempt_id,"route_id":route["route_id"],
     "route_hash":route["route_hash"],"route_node":node_id}
    input_digest=seal_binding(self.jobs,metadata,candidate)
    fields={**metadata,"attempt_schema_version":"2","dispatch_depth":"2",
     "registered_worker":"1","worker_type":"review",
     "note":"completed-review-blocking","review_input_digest":input_digest}
    encoded=",".join(f"{key}={value}" for key,value in fields.items())
    handle.write("\t".join([
     f"2026-09-01T00:00:{index:02d}Z","done",str(self.repo),str(self.repo),
     f"bound-plan-check-{index}",encoded,
    ])+"\n")

 def test_continuation_plan_check_dry_run_and_start_share_inherited_budget(self):
  with self.dispatch_env():
   source,first,path=self.continuation_review_route(resume_from_node="plan-check")
   cap=F.DISPATCH_NODE.max_review_rounds(first["effective_intensity"])
   source_plan_marker=json.loads(
    (R.completion_dir(source["route_id"],jobs=self.jobs)/"plan.json").read_text())
   reviewed_evidence=source_plan_marker["evidence"]["path"]
   self.seed_bound_review_rounds(source,"plan-check",cap,reviewed_evidence)
   # The child route has a new ID, but every surface must count its source
   # generation before admitting a review attempt. Start's adapter call is
   # mocked so a regression cannot contact a model or append a real job.
   results={action:self.run_continuation_review_action(path,action,reviewed_evidence)
            for action in ("dry-run","start")}
  for action,(code,printed,spawned) in results.items():
   with self.subTest(action=action):
    output="\n".join(printed)
    self.assertEqual(code,65,output)
    self.assertIn("reason=review-round-budget-exhausted",output)
    self.assertIn(f"round={cap+1}",output)
    self.assertIn("child_spawned=0",output)
    self.assertEqual(spawned,[],"an over-budget continuation must not launch a child")

 def test_continuation_plan_check_start_launch_is_mocked_when_within_budget(self):
  with self.dispatch_env():
   source,first,path=self.continuation_review_route(resume_from_node="plan-check")
   source_plan_marker=json.loads(
    (R.completion_dir(source["route_id"],jobs=self.jobs)/"plan.json").read_text())
   reviewed_evidence=source_plan_marker["evidence"]["path"]
   dry=self.run_continuation_review_action(path,"dry-run",reviewed_evidence)
   start=self.run_continuation_review_action(path,"start",reviewed_evidence)
  self.assertEqual(dry[0],0,"\n".join(dry[1]))
  self.assertEqual(len(dry[2]),1,"dry-run must reach only the mocked launch preflight")
  self.assertIn("--dry-run",dry[2][0])
  self.assertEqual(start[0],0,"\n".join(start[1]))
  self.assertEqual(len(start[2]),1,"start must reach only the mocked child launcher")
  self.assertNotIn("--dry-run",start[2][0])
 def test_review_round_cap_rejects_the_over_budget_round(self):
  # This wrapper carries ordinary standard+ depth-2 work and had no cap check
  # at all, so the C-14 budget was unreachable on the path most dispatches
  # take (rt-88b775ac ran plan-check r1..r4 unimpeded).
  with self.dispatch_env():
   path=self.route(same_status="supported")
   route=json.loads(path.read_text())
   cap=F.DISPATCH_NODE.max_review_rounds(route["effective_intensity"])
   self.seed_parent()
   self.seed_review_rounds(route["route_id"],"plan-check",cap)
   code,printed=self.run_review_inline(path)
  self.assertEqual(code,65,printed)
  self.assertIn("reason=review-round-budget-exhausted",printed)
  self.assertIn("required_action=resolve-review-findings",printed)
  self.assertIn(".owner-closure.md","\n".join(printed))
  self.assertIn(f"round={cap+1}",printed)
  self.assertIn(f"max_round={cap}",printed)
  self.assertIn("child_spawned=0",printed)
 def test_review_round_cap_allows_a_round_within_budget(self):
  with self.dispatch_env():
   path=self.route(same_status="supported")
   route=json.loads(path.read_text())
   cap=F.DISPATCH_NODE.max_review_rounds(route["effective_intensity"])
   self.seed_parent()
   self.seed_review_rounds(route["route_id"],"plan-check",cap-1)
   code,printed=self.run_review_inline(path)
  self.assertNotIn("reason=review-round-budget-exhausted",printed)

 def test_a_correction_round_keeps_one_identity_across_dry_run_register_and_start(self):
  # The round salt is part of the admission ticket: the row `--register` writes must not move the
  # round its own `--start` derives, and the start must claim that row instead of refusing it as live.
  with self.dispatch_env():
   path=self.route(same_status="supported",intensity="standard")
   route=json.loads(path.read_text())
   self.seed_parent()
   self.seed_review_rounds(route["route_id"],"plan-check",1)
   self.seed_predecessor_markers(path,"plan-check")
   self.seed_plan_marker(route)
   real_run=subprocess.run
   def act(action):
    seen=[];printed=[]
    def run(cmd,**kwargs):
     if any(str(part).endswith("/bin/dispatch-headless.py") for part in cmd):
      seen.append(cmd)
      receipt=("check=ok\nregistered=1\nstarted=1\nchild_spawned=1\n" if action=="start"
               else "check=ok\nregistered=0\nstarted=0\nchild_spawned=0\n")
      return SimpleNamespace(returncode=0,stdout=receipt,stderr="")
     return real_run(cmd,**kwargs)
    argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan-check","--slug","fallback-plan-check",
          "--parent","owner","--capability-mode","dev","--worker-mode","qa/plan-review",
          "--model-role","fast reviewer","--jobs",str(self.jobs),"--"+action]
    with mock.patch.object(sys,"argv",argv), \
         mock.patch("builtins.print",side_effect=lambda *a,**k:printed.append(" ".join(map(str,a)))), \
         mock.patch("subprocess.run",side_effect=run), \
         mock.patch.object(F,"watch_launched_attempt",return_value=("observed",{})):
     try:
      code=F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
     except SystemExit as exc:
      code=exc.code
    self.assertEqual(code,0,printed)
    self.assertTrue(seen,printed)
    return seen[0][seen[0].index("--attempt-id")+1],seen[0]
   dry,_=act("dry-run")
   registered,command=act("register")
   # What the adapter's --register leaves behind: one open row it never claimed for launch.
   parent=command[command.index("--parent-attempt-id")+1] if "--parent-attempt-id" in command else "att-fallback-parent"
   with self.jobs.open("a",encoding="utf-8") as fh:
    fh.write(f"2026-08-29T00:01:00Z\topen\t{self.repo}\t{self.repo}\tfallback-plan-check\t"
             "attempt_schema_version=2,dispatch_depth=2,registered_worker=1,"
             f"route_id={route['route_id']},route_node=plan-check,parent_attempt_id={parent},"
             f"launch_claimed=0,attempt_id={registered}\n")
   started,_=act("start")
  self.assertEqual(dry,registered)
  self.assertEqual(registered,started)
  first_round=F.attempt_identity(SimpleNamespace(slug="fallback-plan-check",parent="owner",parent_attempt_id=parent),
                                 route,{"id":"plan-check"},{"child_harness":"codex"},1)
  self.assertNotEqual(started,first_round)
 def test_review_round_cap_correction_round_attaches_protocol_block_to_prompt_file(self):
  # Plan-correction B2 (third surface): unlike dispatch-node.py (which builds
  # its own prompt) and dispatch-batch.py (which gets the block for free by
  # re-invoking dispatch-node.py for the actual leg -- see the comment beside
  # this call site in stage-dispatch-fallback.py), THIS wrapper calls the
  # adapter directly, so it must attach the identical
  # `DISPATCH_NODE.round_protocol_block` output itself. Proves the wiring
  # (the `--prompt-file` rewrite), not just the shared function (already
  # unit-tested by review_round_cap.test.py::RoundProtocolParityTest).
  with self.dispatch_env():
   path=self.route(same_status="supported")
   route=json.loads(path.read_text())
   self.seed_predecessor_markers(path,"plan-check")
   self.seed_parent()
   self.seed_review_rounds(route["route_id"],"plan-check",1)
   self.seed_plan_marker(route)
   prompt_file=self.repo/"plan-check-prompt.md"
   prompt_file.write_text("BASE PROMPT TEXT\n",encoding="utf-8")
   argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan-check","--slug","fallback-plan-check",
         "--parent","owner","--capability-mode","dev","--worker-mode","qa/plan-review",
         "--model-role","fast reviewer","--jobs",str(self.jobs),
         "--prompt-file",str(prompt_file),"--dry-run"]
   with mock.patch.object(sys,"argv",argv):
    observation=F.LAUNCH_TUPLE.ReportOnlyObservation()
    code=F._dispatch(observation)
   node=next(n for n in route["nodes"] if n["id"]=="plan-check")
   round_rows=F.DISPATCH_NODE.prior_round_attempts(
    self.jobs,route["route_id"],"plan-check",
    route=route if node.get("kind")=="review-worker" else None)
   budget=F.DISPATCH_NODE.admit_round(route,node,self.jobs,owner_attempt_id="att-fallback-parent").budget
   expected_block=F.DISPATCH_NODE.round_protocol_block(
    budget,round_rows,F.worker_type_for_kind(node["kind"]),node["id"])
  self.assertEqual(code,0)
  self.assertNotEqual(expected_block,"")
  augmented=sorted(Path(self.jobs).parent.glob("round-protocol-plan-check-*.md"))
  self.assertEqual(len(augmented),1,augmented)
  self.assertEqual(augmented[0].read_text(encoding="utf-8"),"BASE PROMPT TEXT\n"+expected_block)

 def _producer_revision_entry(self,surface):
  with self.dispatch_env():
   path=self.route(same_status="supported",intensity="standard")
   route=json.loads(path.read_text())
   self.seed_parent()
   evidence=self.seed_plan_marker(route)
   self.seed_review_rounds(route["route_id"],"plan-check",1)
   evidence.write_text("Corrected plan v2.\n")
   plan=next(n for n in route["nodes"] if n["id"]=="plan")
   marker_path=R.completion_dir(route["route_id"],jobs=self.jobs)/"plan.json"
   self.assertEqual(R.gate_currency(route,plan,marker_path).state,"revised-unrecorded")
   observed=[];real_run=subprocess.run
   def run(cmd,**kwargs):
    if any(str(part).endswith("/bin/dispatch-headless.py") for part in cmd):
     observed.append(cmd)
     return SimpleNamespace(returncode=0,stdout="check=ok\nregistered=0\nstarted=0\nchild_spawned=0\n",stderr="")
    return real_run(cmd,**kwargs)
   if surface=="chain":
    argv=["stage-dispatch-fallback.py","--route",str(path),"--node","plan-check",
          "--slug","revised-plan-check","--parent","owner","--jobs",str(self.jobs),"--dry-run"]
   else:
    argv=["dispatch-node.py","--route",str(path),"--node","plan-check","--adapter","codex",
          "--slug","revised-plan-check","--parent","owner","--jobs",str(self.jobs),"--action","dry-run"]
   with mock.patch.object(sys,"argv",argv),mock.patch.object(subprocess,"run",side_effect=run), \
        contextlib.redirect_stdout(io.StringIO()) as output:
    if surface=="chain":
     code=F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
    else:
     with self.assertRaises(SystemExit) as stopped:F.DISPATCH_NODE.main()
     code=stopped.exception.code
   self.assertEqual(code,0,output.getvalue())
   self.assertEqual(len(observed),1)
   self.assertEqual(observed[0][observed[0].index("--reviewed-evidence")+1],str(evidence))
   self.assertEqual(R.gate_currency(route,plan,marker_path).state,"revised-unrecorded")
   self.assertFalse((marker_path.parent/"plan.2.json").exists())
   if surface=="chain": argv[argv.index("--dry-run")]="--start"
   else: argv[argv.index("dry-run")]="start"
   with mock.patch.object(sys,"argv",argv),mock.patch.object(subprocess,"run",side_effect=run), \
        mock.patch.object(F,"watch_launched_attempt",return_value=("observed",{})), \
        contextlib.redirect_stdout(io.StringIO()) as output:
    if surface=="chain": code=F._dispatch(F.LAUNCH_TUPLE.ReportOnlyObservation())
    else:
     with self.assertRaises(SystemExit) as stopped:F.DISPATCH_NODE.main()
     code=stopped.exception.code
   self.assertEqual(code,0,output.getvalue())
   self.assertEqual(R.gate_currency(route,plan,marker_path).state,"current")
   link_path=marker_path.parent/"plan.att-fixture-plan.attempt.json"
   original_link=link_path.read_bytes();link=json.loads(original_link)
   for wrong in (marker_path.parent/"other-node.json",self.art/"plan.json"):
    link["completion_marker"]=str(wrong);link_path.write_text(json.dumps(link))
    self.assertNotEqual(R.gate_currency(route,plan,marker_path).state,"current")
   link_path.write_bytes(original_link)
   self.assertEqual(R.gate_currency(route,plan,marker_path).state,"current")
   self.assertEqual(json.loads(marker_path.read_text())["revision"]["recorded_by"],"runtime-auto")
 def test_sd161_chain_auto_revises_producer_before_resolving_review_input(self):
  self._producer_revision_entry("chain")
 def test_sd161_node_auto_revises_producer_before_resolving_review_input(self):
  self._producer_revision_entry("node")

 def test_cross_harness_direct_precedes_inline(self):
  result=self.run_chain(self.route()); self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertIn("selected_hop=cross-harness-headless",result.stdout); self.assertIn("child_harness=claude",result.stdout)
  self.assertIn("launch_authority=conductor",result.stdout); self.assertIn("broker_lifecycle=retired",result.stdout)
 def test_allocation_receipt_row_is_written_beside_the_stdout_verdict(self):
  # 2026-08-29: the rank/headroom verdict used to exist only on stdout, so a
  # configured policy could sit inert for weeks with no way to tell. Driving
  # `_emit_child_success` directly (the one success path every launch shape
  # shares) must print the verdict AND leave a ledger row keyed by the attempt,
  # carrying the sealed strategy, the preferred harness, and the inert-key
  # finding. The CLI pairing lives in the next test.
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  node=next(n for n in route["nodes"] if n["id"]=="plan")
  allocation={**route["dispatch_allocation"],"strategy":"capacity-aware"}
  context={"strategy":"capacity-aware","window":30,"usage_gate_used_percent":85,
           "allocation":allocation,"preferred":"codex",
           "counts":{"claude":3,"codex":1,"opencode":0},"states":{"claude":"ok","codex":"ok","opencode":"unknown"},
           "rank":["claude","codex"],"capacity":{"claude":79.0,"codex":74.0,"opencode":None},
           "quality_band":"primary","relief_promoted":False,"parent_cross":"not-applicable",
           "parent_cross_cause":"-","sole_gate":"ok","affinity":"diverse","owner_family":None,
           "quality_peer_set":None,"eligible":["claude","codex"],"limited":[]}
  row=self.tuple("claude","supported")
  args=SimpleNamespace(action="dry-run",slug="fallback-plan",jobs=self.jobs,route=path)
  import io,contextlib as _cl
  out=io.StringIO()
  with mock.patch.dict(os.environ,{"AGENT_HOME":str(ROOT),"AGENT_DISPATCH_JOBS":str(self.jobs)}), _cl.redirect_stdout(out):
   F._emit_child_success(args,route,node,context,row,attempt_id="att-receipt",fallback_hop="cross-harness-headless")
  receipt=dict(line.split("=",1) for line in out.getvalue().splitlines() if "=" in line)
  self.assertEqual(receipt["allocation_rank"],"claude,codex")
  self.assertEqual(receipt["allocation_preferred"],"codex")
  # The shipped default now seals `harness_weights`; under capacity-aware it is inert too.
  self.assertEqual(receipt["allocation_inert_keys"],"depth_affinity_weight,harness_weights,usage_gate_used_percent,usage_headroom_exponent")
  self.assertTrue(receipt["allocation_receipt"].startswith("al-"),receipt)
  ledger=Path(self.tmp.name)/"allocation"/f"{route['route_id']}.jsonl"
  self.assertEqual(receipt["allocation_ledger"],str(ledger)); self.assertTrue(ledger.is_file())
  rows=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
  self.assertEqual(len(rows),1,rows); ledger_row=rows[0]
  self.assertEqual(ledger_row["event_id"],receipt["allocation_receipt"])
  self.assertEqual(ledger_row["child_harness"],"claude"); self.assertEqual(ledger_row["attempt_id"],"att-receipt")
  self.assertEqual(ledger_row["route_node"],"plan"); self.assertEqual(ledger_row["unit"],"plan/plan-author")
  self.assertEqual(ledger_row["action"],"dry-run"); self.assertEqual(ledger_row["writer"],"stage-dispatch-fallback.py")
  self.assertEqual(ledger_row["strategy"],"capacity-aware"); self.assertEqual(ledger_row["preferred"],"codex")
  self.assertIs(ledger_row["preferred_honored"],False)
  self.assertEqual(sorted(ledger_row["inert_keys"]),["depth_affinity_weight","harness_weights","usage_gate_used_percent","usage_headroom_exponent"])
  self.assertEqual(ledger_row["rank"],["claude","codex"]); self.assertEqual(ledger_row["fallback_hop"],"cross-harness-headless")
  self.assertEqual(ledger_row["capacity"]["codex"],74.0); self.assertEqual(ledger_row["counts"]["claude"],3)
  # A missing allocation context (no sealed policy) still leaves the child evidence.
  out=io.StringIO()
  with mock.patch.dict(os.environ,{"AGENT_HOME":str(ROOT),"AGENT_DISPATCH_JOBS":str(self.jobs)}), _cl.redirect_stdout(out):
   F._emit_child_success(args,route,node,None,self.tuple("codex","supported"),attempt_id="att-bare",fallback_hop="same-harness-headless")
  rows=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
  self.assertEqual(len(rows),2); self.assertEqual(rows[1]["child_harness"],"codex"); self.assertIsNone(rows[1]["strategy"])
  self.assertIn("allocation_receipt=",out.getvalue())
 def test_allocation_receipt_row_pairs_with_the_cli_verdict(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  result=self.run_chain(path)
  # The MA-W1-147 skip guard is gone: `route()` now seals the same runtime,
  # artifact and registry roots `run_chain` launches with, so the CLI pairing
  # is actually observed instead of skipped.
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  receipt=self.cli_verdict(result.stdout)
  self.assertIn("allocation_rank",receipt,result.stdout)
  self.assertEqual(receipt["allocation_preferred"],"codex")
  self.assertEqual(receipt["allocation_inert_keys"],"-")
  self.assertTrue(receipt["allocation_receipt"].startswith("al-"),receipt)
  ledger=Path(self.tmp.name)/"allocation"/f"{route['route_id']}.jsonl"
  self.assertEqual(receipt["allocation_ledger"],str(ledger))
  self.assertTrue(ledger.is_file())
  rows=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
  self.assertEqual(len(rows),1,rows); row=rows[0]
  self.assertEqual(row["event_id"],receipt["allocation_receipt"])
  self.assertEqual(row["child_harness"],receipt["child_harness"])
  self.assertEqual(row["route_node"],"plan"); self.assertEqual(row["unit"],"plan/plan-author")
  self.assertEqual(row["action"],"dry-run"); self.assertEqual(row["writer"],"stage-dispatch-fallback.py")
  self.assertEqual(row["strategy"],route["dispatch_allocation"]["strategy"])
  self.assertEqual(row["preferred"],"codex"); self.assertEqual(row["inert_keys"],{})
  self.assertEqual(row["rank"],receipt["allocation_rank"].split(","))
  self.assertEqual(row["fallback_hop"],receipt["selected_hop"])
  self.assertEqual(row["attempt_id"],receipt["attempt_id"])
  self.assertIn(row["preferred_honored"],(True,False))
  self.assertEqual(row["preferred_honored"],row["child_harness"]=="codex")
  for harness in ("claude","codex"): self.assertIn(harness,row["counts"])
 def test_wrapper_command_projects_selected_lifecycle_to_codex_and_claude(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text()); node=next(n for n in route["nodes"] if n["id"]=="plan")
  args=SimpleNamespace(action="dry-run",slug="stage",parent="owner",mode="dev/refactor",qa="standard",worker_role=None,model_role="deep maker",prompt_file=None,jobs=self.jobs,route=path,launch_lifecycle="foreground-scoped",foreground_timeout=123.0)
  for ordinal,harness in ((1,"codex"),(2,"claude")):
   row=self.tuple(harness,"supported")
   command=F.wrapper_command(args,route,node,row,ordinal,"att-test")
   self.assertEqual(command[command.index("--worker-type")+1],"stage")
   self.assertEqual(command[command.index("--assigned-contract")+1],"code-plan")
   self.assertNotIn("--worker-role",command)
   self.assertIn("--launch-lifecycle",command)
   self.assertEqual(command[command.index("--launch-lifecycle")+1],"foreground-scoped")
   self.assertEqual(command[command.index("--foreground-timeout")+1],"123.0")
  args.launch_lifecycle="detached"
  command=F.wrapper_command(args,route,node,self.tuple("codex","supported"),1,"att-test")
  self.assertNotIn("--foreground-timeout",command)
  command=F.wrapper_command(args,route,node,self.tuple("opencode","supported"),1,"att-test")
  self.assertNotIn("--launch-lifecycle",command)
  frame=next(n for n in route["nodes"] if n["id"]=="frame")
  command=F.wrapper_command(args,route,frame,self.tuple("codex","supported"),1,"att-test")
  self.assertEqual(command[command.index("--worker-type")+1],"support")
  # unit-io stage: the readable contract stays the entry capability; the
  # plan/frame unit persona carries the stage contract (same as design build).
  self.assertEqual(command[command.index("--assigned-contract")+1],"autopilot-code")
 def test_fallback_argv_omits_default_qa(self):
  # --qa is not a user-facing axis (CONVENTIONS §1.1): the wrapper derives it
  # from --intensity (dispatch_mode_contract.resolve_qa), so this dispatcher
  # must not forward a hardcoded default when the caller omitted it.
  path=self.route(same_status="supported"); route=json.loads(path.read_text()); node=next(n for n in route["nodes"] if n["id"]=="plan")
  args=SimpleNamespace(action="dry-run",slug="stage",parent="owner",mode="dev/refactor",qa=None,worker_role=None,model_role="deep maker",prompt_file=None,jobs=self.jobs,route=path,launch_lifecycle="detached",foreground_timeout=123.0)
  command=F.wrapper_command(args,route,node,self.tuple("codex","supported"),1,"att-test")
  self.assertNotIn("--qa",command)
  self.assertEqual(command[command.index("--intensity")+1],route["effective_intensity"])
 def test_explicit_parent_mismatch_fails_before_registration(self):
  path=self.route(same_status="supported")
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node","plan","--slug","fallback-plan","--parent","wrong-owner","--capability-mode","dev","--worker-mode","plan/plan-author","--jobs",str(self.jobs),"--register"]
  env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"real-owner"}
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,73,result.stdout+result.stderr)
  self.assertIn("reason=parent-identity-mismatch",result.stdout)
  self.assertFalse(self.jobs.exists())
 def test_failed_same_and_cross_degrade_in_order(self):
  path=self.route(native="supported"); same="codex/headless/workspace-write/codex/conductor"; cross="codex/headless/workspace-write/claude/conductor"
  result=self.run_chain(path,"--failed-tuple",same,"--failed-tuple",cross); self.assertEqual(result.returncode,79,result.stdout+result.stderr); self.assertIn("skipped-child-proof-missing",result.stdout); self.assertIn("selected_hop=inline",result.stdout)
  route=json.loads(path.read_text()); route["dispatch_evidence"]["native_subagent"][0]["status"]="unsupported"
  # Only depth-2 nodes carry a fallback chain. The depth-1 frame bootstrap legs
  # deliberately have no `fallback_hops` key at all -- recovery from a dead
  # frame leg is an explicit depth-0 relaunch, never a machine hop -- so degrade
  # every chain that exists instead of assuming every node has one.
  for node in route["nodes"]:
   if "fallback_hops" in node: node["fallback_hops"][2]["candidates"][0]["status"]="unsupported"
  route["route_hash"]=R.route_hash(route); route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]; path.write_text(json.dumps(route))
  result=self.run_chain(path,"--failed-tuple",same,"--failed-tuple",cross); self.assertEqual(result.returncode,79,result.stdout+result.stderr); self.assertIn("selected_hop=inline",result.stdout)
  self.assertIn("route_reuse=required",result.stdout)
  self.assertIn("route_id="+route["route_id"],result.stdout)
 def test_healthy_child_returns_a_launch_receipt_inside_the_confirm_window(self):
  # Regression for the 2026-08-14 candidate 6 defect: `--start` observed a
  # healthy detached child for the FULL no-progress budget
  # (progress_window_seconds * watchdog_max_windows = 300*12 = 1h by default),
  # so the owner's foreground call died before the launch receipt was printed.
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=300.0,
                       watchdog_max_windows=12,direct_timeout=45.0)
  self.assertEqual(F.launch_confirm_deadline_seconds(args),45.0)
  route={"route_id":"rt-fixture"}
  node={"id":"plan"}
  seed=mock.Mock(returncode=0,stdout="",stderr="")
  alive=mock.Mock(returncode=0,stdout="action=observed\n",stderr="")
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=0.4,
                       watchdog_max_windows=12,direct_timeout=0.2)
  started=time.monotonic()
  with mock.patch.object(F.subprocess,"run",side_effect=[seed]+[alive]*400):
   state,_=F.watch_launched_attempt(
    args,route,node,"att-healthy",{"child_pid":"1","child_pid_start":"2"})
  elapsed=time.monotonic()-started
  self.assertEqual(state,"observed")
  # Without the confirm bound this would run 0.4*12 = 4.8s, not <= ~0.2s.
  self.assertLess(elapsed,1.5)

 def test_confirm_window_never_exceeds_the_no_progress_budget(self):
  # A tiny explicit budget still wins over a larger spawn-confirm window, and a
  # disabled/non-positive confirm value falls back to the full budget.
  small=SimpleNamespace(progress_window_seconds=1.0,watchdog_max_windows=2,
                        direct_timeout=45.0)
  self.assertEqual(F.launch_confirm_deadline_seconds(small),2.0)
  disabled=SimpleNamespace(progress_window_seconds=1.0,watchdog_max_windows=2,
                           direct_timeout=0.0)
  self.assertEqual(F.launch_confirm_deadline_seconds(disabled),2.0)
  absent=SimpleNamespace(progress_window_seconds=300.0,watchdog_max_windows=12)
  self.assertEqual(F.launch_confirm_deadline_seconds(absent),
                   F.DIRECT_TIMEOUT_DEFAULT)

 def _live_row(self,attempt="att-live",route_id="rt-fixture",node="plan"):
  proc=subprocess.Popen(["sleep","30"],start_new_session=True)
  self.addCleanup(lambda:(proc.kill() if proc.poll() is None else None,proc.wait()))
  start=(Path("/proc")/str(proc.pid)/"stat").read_text().split()[21]
  self.jobs.write_text(
   f"2026-07-24T00:00:00Z\topen\t/repo\t/wt\t{node}\t"
   f"route_id={route_id},route_node={node},attempt_id={attempt},"
   f"pid={proc.pid},pid_start={start},pgid={proc.pid},"
   f"pid_observer_ns={os.readlink('/proc/self/ns/pid')}\n")
  return proc
 def test_sd_open_38_seed_phase_regression_is_not_a_launch_failure(self):
  # #9 root cause: the worker heartbeated past `launch` before the launcher's
  # seed ran; the seed's `progress-phase-regression` exit became
  # `progress-watchdog-fail-closed` with `watchdog_action=unknown`.
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=0.4,watchdog_max_windows=12,direct_timeout=0.2)
  route={"route_id":"rt-fixture"};node={"id":"plan"}
  regressed=mock.Mock(returncode=65,stdout="check=failed\nreason=progress-phase-regression\ndetail=analysis->launch\n",stderr="")
  alive=mock.Mock(returncode=0,stdout="check=ok\naction=observe\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[regressed]+[alive]*50) as run:
   state,fields=F.watch_launched_attempt(args,route,node,"att-seed",{"child_pid":"1","child_pid_start":"2"})
  self.assertEqual(state,"observed")
  # review finding 9: the demoted seed rides on the receipt as an advisory
  self.assertEqual((fields["watchdog_verdict"],fields["watchdog_advisory_tool"],fields["watchdog_advisory_reason"]),
                   ("advisory","heartbeat-seed","progress-phase-regression"))
  seed_argv=run.call_args_list[0].args[0]
  self.assertIn("--if-absent",seed_argv)
  self.assertEqual(seed_argv[seed_argv.index("--phase")+1],"launch")
 def test_sd_open_38_progress_tool_failure_with_a_live_child_is_advisory(self):
  # cairn W15b: `--start` said progress-watchdog-fail-closed while the row was
  # open and the claude child alive; all three stages finished normally.
  proc=self._live_row(attempt="att-tool")
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=0.4,watchdog_max_windows=12,direct_timeout=0.2)
  route={"route_id":"rt-fixture"};node={"id":"plan"}
  seed=mock.Mock(returncode=0,stdout="check=ok\n",stderr="")
  crashed=mock.Mock(returncode=65,stdout="check=failed\nreason=progress-error\ndetail=boom\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed]+[crashed]*50):
   state,fields=F.watch_launched_attempt(args,route,node,"att-tool",{"child_pid":str(proc.pid),"child_pid_start":"2"})
  self.assertEqual(state,"observed")
  self.assertEqual(fields["watchdog_verdict"],"advisory")
  self.assertEqual((fields["watchdog_advisory_tool"],fields["watchdog_advisory_reason"]),("watchdog","progress-error"))
  # a seed-tool crash with a live child is advisory as well
  seed_crash=mock.Mock(returncode=65,stdout="check=failed\nreason=progress-error\ndetail=boom\n",stderr="")
  alive=mock.Mock(returncode=0,stdout="check=ok\naction=observe\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed_crash]+[alive]*50):
   state,fields=F.watch_launched_attempt(args,route,node,"att-tool",{"child_pid":str(proc.pid),"child_pid_start":"2"})
  self.assertEqual(state,"observed")
  self.assertEqual((fields["watchdog_verdict"],fields["watchdog_advisory_tool"]),("advisory","heartbeat-seed"))
  # a genuine watchdog verdict (tool ran, action=fail-closed-*) is still a verdict
  identity=mock.Mock(returncode=0,stdout="check=ok\naction=fail-closed-identity\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed,identity]):
   state,fields=F.watch_launched_attempt(args,route,node,"att-tool",{"child_pid":str(proc.pid),"child_pid_start":"2"})
  self.assertEqual(state,"fail-closed")
  # no live child and no terminal row: the tool failure stays fail-closed
  proc.kill();proc.wait()
  with mock.patch.object(F.subprocess,"run",side_effect=[seed]+[crashed]*50):
   state,fields=F.watch_launched_attempt(args,route,node,"att-tool",{"child_pid":str(proc.pid),"child_pid_start":"2"})
  self.assertEqual(state,"fail-closed")
  self.assertEqual(fields.get("reason"),"progress-error")
 def test_process_exit_without_terminal_record_does_not_authorize_retry(self):
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=1,watchdog_max_windows=2,direct_timeout=0.1)
  seed=mock.Mock(returncode=0,stdout="",stderr="")
  exited=mock.Mock(returncode=0,stdout="action=process-exited\nterminal_action=process-exited\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed]+[exited]*20):
   state,fields=F.watch_launched_attempt(args,{"route_id":"rt-fixture"},{"id":"plan"},"att-process-exit",{})
  self.assertEqual(state,"observed")
  self.assertEqual(fields["terminal_action"],"process-exited")

 def test_watchdog_cache_cannot_override_current_terminal_row(self):
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=1,watchdog_max_windows=2,direct_timeout=0.1)
  for cached in ("process-exited", "dead-no-progress", "dead-capacity"):
   for note, expected in (("completed-marker", "terminal"),
                          ("completed-review-blocking", "terminal"),
                          ("dead-worker-fail", "fallback"),
                          ("dead-capacity", "capacity")):
    with self.subTest(cached=cached,note=note):
     self.jobs.write_text(
      "2026-07-24T00:00:00Z\tdone\t/repo\t/wt\tplan-check\t"
      "route_id=rt-fixture,route_node=plan-check,attempt_id=att-terminal,"
      f"launch_outcome=reaped-before-publish,note={note}\n")
     seed=mock.Mock(returncode=0,stdout="",stderr="")
     stale=mock.Mock(returncode=0,stdout=f"action={cached}\nterminal_action={cached}\n",stderr="")
     with mock.patch.object(F.subprocess,"run",side_effect=[seed,stale]):
      state,fields=F.watch_launched_attempt(args,{"route_id":"rt-fixture"},{"id":"plan-check"},"att-terminal",{})
     self.assertEqual(state,expected)
     self.assertEqual(fields["note"],note)

 def test_cached_failure_without_terminal_record_cannot_authorize_retry(self):
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=1,watchdog_max_windows=2,direct_timeout=0.1)
  for cached in ("dead-no-progress", "dead-capacity", "registry-terminal"):
   with self.subTest(cached=cached):
    seed=mock.Mock(returncode=0,stdout="",stderr="")
    stale=mock.Mock(returncode=0,stdout=f"action={cached}\nterminal_action={cached}\n",stderr="")
    with mock.patch.object(F.subprocess,"run",side_effect=[seed,stale]):
     state,_=F.watch_launched_attempt(args,{"route_id":"rt-fixture"},{"id":"plan"},"att-missing",{})
    self.assertEqual(state,"fail-closed")

 def test_completed_but_live_or_unverifiable_attempt_cannot_authorize_retry(self):
  proc=self._live_row(attempt="att-settling")
  self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t").rstrip("\n") + ",note=completed-marker\n")
  args=SimpleNamespace(jobs=self.jobs,progress_window_seconds=1,watchdog_max_windows=2,direct_timeout=0.1)
  seed=mock.Mock(returncode=0,stdout="",stderr="")
  stale=mock.Mock(returncode=0,stdout="action=dead-capacity\nterminal_action=dead-capacity\n",stderr="")
  with mock.patch.object(F.subprocess,"run",side_effect=[seed]+[stale]*20):
   state,fields=F.watch_launched_attempt(args,{"route_id":"rt-fixture"},{"id":"plan"},"att-settling",{})
  self.assertEqual(state,"observed")
  self.assertEqual(fields["process_state"],"live")
  self.assertIsNone(proc.poll())
  with mock.patch.object(F.subprocess,"run",side_effect=[seed,stale]), mock.patch.object(
       F,"attempt_process_quiescence",return_value=SimpleNamespace(state="unverifiable",reason="observer-unavailable")):
   state,fields=F.watch_launched_attempt(args,{"route_id":"rt-fixture"},{"id":"plan"},"att-settling",{})
  self.assertEqual(state,"fail-closed")
  self.assertEqual(fields["process_reason"],"observer-unavailable")

 def test_completed_row_is_draining_until_exact_process_exits(self):
  proc=subprocess.Popen(["sleep","30"],start_new_session=True)
  try:
   start=(Path("/proc")/str(proc.pid)/"stat").read_text().split()[21]
   self.jobs.write_text(
    "2026-07-24T00:00:00Z\tdone\t/repo\t/wt\tplan\t"
    "route_id=rt-q,route_node=plan,attempt_id=att-q,"
    f"pid={proc.pid},pid_start={start},pgid={proc.pid},"
    f"pid_observer_ns={os.readlink('/proc/self/ns/pid')},note=completed-marker\n")
   state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan","att-q")
   self.assertEqual(state,"draining")
   self.assertEqual(fields["process_state"],"live")
   proc.terminate();proc.wait(timeout=5)
   state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan","att-q")
   self.assertEqual(state,"terminal")
   self.assertEqual(fields["process_state"],"quiescent")
  finally:
   if proc.poll() is None:proc.kill()
   proc.wait()
 def test_finished_blocking_review_row_is_terminal_not_fallback(self):
  # OPERATIONS §5.10: a reviewer's blocking-findings completion is a stage
  # result for the owner, never a launch failure -- the wrapper must neither
  # descend to the next hop (a second review the owner never asked for, spent
  # from the round budget) nor fail closed on the terminal race.
  self.jobs.write_text(
   "2026-07-24T00:00:00Z\tdone\t/repo\t/wt\tplan-check\t"
   "route_id=rt-q,route_node=plan-check,attempt_id=att-rb,worker_type=review,"
   "launch_outcome=reaped-before-publish,note=completed-review-blocking\n")
  state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan-check","att-rb")
  self.assertEqual(state,"terminal")
  self.assertEqual(fields["note"],"completed-review-blocking")
  self.assertEqual(fields["review_verdict"],"FAIL")
  self.assertEqual(fields["process_state"],"quiescent")
  self.jobs.write_text(self.jobs.read_text().replace("completed-review-blocking","dead-worker-fail"))
  state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan-check","att-rb")
  self.assertEqual(state,"fallback")
  self.assertNotIn("review_verdict",fields)
 def test_finished_verdict_row_stops_the_chain_instead_of_retrying(self):
  # home-os rt-96dd5b62 (2026-09-27): a foreground reviewer's blocking FAIL came
  # back as worker_failure=completed-review-blocking, the chain retried the same
  # unchanged plan on the next hop, and the round budget was spent twice before
  # the owner could correct it. A verdict row is this round's result.
  base=("2026-09-27T00:00:00Z\t{status}\t/repo\t/wt\tplan-check\t"
        "route_id=rt-q,route_node=plan-check,attempt_id={aid},worker_type={wt},note={note}{extra}\n")
  rows=[base.format(status="done",aid="att-fail",wt="review",note="completed-review-blocking",extra=""),
        base.format(status="done",aid="att-crash",wt="review",note="dead-worker-fail",extra=",failure_class=fail"),
        base.format(status="done",aid="att-stage",wt="stage",note="dead-worker-fail",extra=",failure_class=fail"),
        base.format(status="done",aid="att-envelope",wt="review",note="dead-invalid-envelope",extra=""),
        base.format(status="open",aid="att-live",wt="review",note="-",extra="")]
  self.jobs.write_text("".join(rows))
  self.assertEqual(F.finished_verdict_row(self.jobs,"rt-q","plan-check","att-fail")["note"],"completed-review-blocking")
  self.assertEqual(F.finished_verdict_row(self.jobs,"rt-q","plan-check","att-stage")["note"],"dead-worker-fail")
  for aid in ("att-crash","att-envelope","att-live","att-missing"):
   with self.subTest(aid=aid):
    self.assertIsNone(F.finished_verdict_row(self.jobs,"rt-q","plan-check",aid))
 def test_launched_report_carries_the_verdict_once(self):
  args=SimpleNamespace(jobs=self.jobs)
  out=io.StringIO()
  with mock.patch.object(F,"_emit_child_success") as emitted, contextlib.redirect_stdout(out):
   rc=F._report_launched(args,{"route_id":"rt-q"},{"id":"plan-check"},{},{"child_harness":"codex"},
                         {"fallback_hop":"same-harness-headless"},1,"att-fail",["1:k:direct:exit-0:attempt-att-fail"],
                         {},"",terminal_note="completed-review-blocking",review_verdict="FAIL")
  self.assertEqual(rc,0)
  emitted.assert_called_once()
  lines=out.getvalue().splitlines()
  self.assertEqual(lines[0],"check=ok")
  self.assertIn("review_verdict=FAIL",lines)
  self.assertIn("terminal_note=completed-review-blocking",lines)
  self.assertEqual(sum(line.startswith("selected_hop=") for line in lines),1)
 def test_terminal_fallback_consumes_portable_receipt_after_observer_exit(self):
  import dispatch_contract as D
  for harness in ("claude","codex","opencode"):
   for lifecycle,outcome in (("foreground-scoped","governed-process-reaped"),
                             ("detached","governed-process-group-drained")):
    for note,expected in (("completed-marker","terminal"),
                          ("completed-review-blocking","terminal"),
                          ("dead-capacity","capacity"),
                          ("dead-worker-fail","fallback")):
     with self.subTest(harness=harness,lifecycle=lifecycle,note=note):
      metadata={"route_id":"rt-q","route_node":"plan-check","attempt_id":"att-receipted",
       "attempt_schema_version":"2","dispatch_depth":"2","transport":"headless",
       "execution_surface":"registered-headless","registered_worker":"1",
       "fallback_hop":"same-harness-headless","harness":harness,"note":note,
       "pid":"437","pgid":"437","pid_start":"20","pid_scope":"namespace-local",
       "pid_ns":"pid:[foreign-fixture]","pid_observer_ns":"pid:[foreign-fixture]",
       "launch_lifecycle":lifecycle,"launch_outcome":outcome,
       "group_reap_proof":D.GROUP_REAP_PROOF,"group_reap_pgid":"437",
       "attempt_descendant_proof":D.ATTEMPT_DESCENDANT_PROOF,
       "attempt_descendant_observer_ns":"pid:[foreign-fixture]"}
      def write(status="done"):
       self.jobs.write_text("2026-09-11T00:00:00Z\t"+status+"\t/repo\t/wt\treview\t"+
        ",".join(f"{k}={v}" for k,v in metadata.items())+"\n")
      write()
      state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan-check","att-receipted")
      self.assertEqual(state,expected,fields)
      self.assertEqual(fields["process_state"],"quiescent")
      write("open")
      self.assertIsNone(F.terminal_attempt_state(self.jobs,"rt-q","plan-check","att-receipted"))
      metadata.pop("group_reap_proof")
      write()
      state,fields=F.terminal_attempt_state(self.jobs,"rt-q","plan-check","att-receipted")
      self.assertEqual(state,"fail-closed",fields)
 def test_attempt_identity_is_stable_across_actions(self):
  path=self.route(); first=self.run_chain(path); second=self.run_chain(path)
  def attempt(result):
   # Report the chain's own refusal instead of a bare StopIteration: when the
   # dry run fails there is no attempt_id= line, and the reason is the finding.
   self.assertEqual(result.returncode,0,result.stdout+result.stderr)
   attempt_id=self.cli_verdict(result.stdout).get("attempt_id","-")
   self.assertTrue(attempt_id.startswith("att-"),result.stdout)
   return attempt_id
  self.assertEqual(attempt(first),attempt(second))
 def test_attempt_identity_includes_exact_parent_generation(self):
  route={"route_id":"rt-parent-generation"};node={"id":"plan"};row={"child_harness":"codex"}
  one=SimpleNamespace(slug="stage",parent="owner",parent_attempt_id="att-parent-one")
  two=SimpleNamespace(slug="stage",parent="owner",parent_attempt_id="att-parent-two")
  self.assertEqual(F.attempt_identity(one,route,node,row,1),F.attempt_identity(one,route,node,row,1))
  self.assertNotEqual(F.attempt_identity(one,route,node,row,1),F.attempt_identity(two,route,node,row,1))
  self.assertNotEqual(
   F.capacity_attempt_identity(one,route,node,row,1,"model-a"),
   F.capacity_attempt_identity(two,route,node,row,1,"model-a"),
  )
 def test_a_later_round_keeps_the_same_harness_under_its_own_identity(self):
  # OpenCode r4: round 2 of `test` on the pinned harness got round 1's (finished) identity,
  # was refused as an identity conflict and fell through to another harness.
  route={"route_id":"rt-rounds"};node={"id":"test"};row={"child_harness":"opencode"}
  args=SimpleNamespace(slug="stage",parent="owner",parent_attempt_id="att-owner")
  first=F.attempt_identity(args,route,node,row,1)
  self.assertEqual(F.attempt_identity(args,route,node,row,1,1),first)
  self.assertNotEqual(F.attempt_identity(args,route,node,row,1,2),first)
  self.assertEqual(F.attempt_identity(args,route,node,row,1,2),F.attempt_identity(args,route,node,row,1,2))
 def test_start_does_not_count_the_row_it_registered_itself_as_a_live_round(self):
  def row(aid,status="open",**extra):
   meta={"route_id":"rt-own","route_node":"test","attempt_id":aid,"parent_attempt_id":"att-owner",
         "launch_claimed":"0",**extra}
   return "2026-10-02T00:00:00Z\t"+status+"\t/repo\t/repo\tstage\t"+",".join(f"{k}={v}" for k,v in meta.items())
  route={"route_id":"rt-own"};node={"id":"test"}
  args=lambda action,slug="stage",parent="att-owner":SimpleNamespace(action=action,jobs=self.jobs,slug=slug,parent_attempt_id=parent)
  self.jobs.write_text(row("att-registered")+"\n")
  self.assertEqual(F.own_registered_attempt(args("start"),route,node),"att-registered")
  for other in (args("register"),args("dry-run"),args("start",slug="other"),args("start",parent="att-other")):
   self.assertIsNone(F.own_registered_attempt(other,route,node))
  for line in (row("att-registered",launch_claimed="1"),row("att-registered",pid="12"),row("att-registered","done")):
   self.jobs.write_text(line+"\n")
   self.assertIsNone(F.own_registered_attempt(args("start"),route,node))
  self.jobs.write_text(row("att-a")+"\n"+row("att-b")+"\n")
  self.assertIsNone(F.own_registered_attempt(args("start"),route,node))
 def test_legacy_parent_generation_conflict_is_typed_without_reusing_identity(self):
  route={"route_id":"rt-parent-generation"};node={"id":"plan"};row={"child_harness":"codex"}
  old=SimpleNamespace(slug="stage",parent="owner",parent_attempt_id="att-parent-old")
  legacy=F.legacy_attempt_identity(old,route,node,row,1)
  self.jobs.write_text(
   "2026-08-13T00:00:00Z\tdone\t/repo\t/wt\tstage\t"
   f"attempt_schema_version=2,attempt_id={legacy},parent_attempt_id=att-parent-old\n",
   encoding="utf-8",
  )
  self.assertEqual(
   F.legacy_parent_generation_conflict(self.jobs,legacy,"att-parent-new"),
   "attempt-identity-parent-generation-conflict",
  )
 def test_parallel_register_is_rejected_without_creating_a_row(self):
  path=self.route(); first=self.run_register(path); second=self.run_register(path)
  self.assertEqual(first.returncode,65,first.stdout+first.stderr)
  self.assertEqual(second.returncode,65,second.stdout+second.stderr)
  self.assertIn("reason=parallel-group-batch-required",first.stdout)
  self.assertEqual(len(self.jobs.read_text().splitlines()),1)
  self.assertIn("att-fallback-parent",self.jobs.read_text())
 def test_registry_prevents_explicitly_classified_tuple_retry(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  pipe=f"capability=autopilot-code,route_id={route['route_id']},route_node=plan,parent=owner,attempt_id=att-prior000000,parent_harness=codex,parent_transport=headless,parent_sandbox=workspace-write,child_harness=codex,launch_authority=conductor,note=dead-launch-error,failure_class=launch-tuple"
  self.jobs.write_text(f"2026-07-16T00:00:00Z\tdone\t/repo\t{self.repo}\tfallback-plan\t{pipe}\n")
  result=self.run_chain(path); self.assertEqual(result.returncode,0,result.stdout+result.stderr); self.assertIn("selected_hop=cross-harness-headless",result.stdout); self.assertIn("skipped-prior-unchanged-failure",result.stdout)
 def test_registry_worker_deaths_do_not_spend_a_launch_tuple(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  base=(f"capability=autopilot-code,route_id={route['route_id']},route_node=plan,"
        "parent=owner,parent_harness=codex,parent_transport=headless,"
        "parent_sandbox=workspace-write,child_harness=codex,launch_authority=conductor")
  self.jobs.write_text(
   f"2026-07-16T00:00:00Z\tdone\t/repo\t{self.repo}\tworker-fail\t"
   f"{base},attempt_id=att-worker-fail,note=dead-worker-fail,failure_class=fail\n"
   f"2026-07-16T00:00:01Z\tdone\t/repo\t{self.repo}\tworker-dead\t"
   f"{base},attempt_id=att-worker-dead,note=dead-exact-pid\n",
   encoding="utf-8")
  self.assertEqual(F.registry_failures(self.jobs,route["route_id"],"plan"),{})
  result=self.run_chain(path)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  self.assertRegex(result.stdout,r"selected_hop=(same|cross)-harness-headless")
  self.assertNotIn("skipped-prior-unchanged-failure",result.stdout)
 def test_invalid_model_role_is_structured_and_preserved(self):
  path=self.route(same_status="supported")
  cross="codex/headless/workspace-write/claude/conductor"
  result=self.run_chain(path,"--model-role","not-a-role","--failed-tuple",cross)
  self.assertEqual(result.returncode,64,result.stdout+result.stderr)
  self.assertIn("reason=route-model-role-override",result.stdout)
  self.assertIn("expected=deep maker",result.stdout)
  self.assertNotIn("Traceback",result.stdout+result.stderr)
 def test_legacy_route_is_read_only(self):
  path=self.route(); route=json.loads(path.read_text()); route["broker_contract_version"]=2; route.pop("dispatch_contract_version")
  for row in route["dispatch_evidence"]["tuples"]: row["launch_authority"]="ancestor-broker"; row["broker_root"]="/tmp/legacy"
  for node in route["nodes"]:
   for hop in node.get("fallback_hops",[])[:2]:
    for row in hop.get("candidates",[]): row["launch_authority"]="ancestor-broker"; row["broker_root"]="/tmp/legacy"
  route["route_hash"]=R.route_hash(route); route["route_id"]="rt-"+route["route_hash"].split(":",1)[1][:16]; path.write_text(json.dumps(route))
  result=self.run_chain(path); self.assertEqual(result.returncode,76,result.stdout+result.stderr); self.assertIn("reason=legacy-broker-route-read-only",result.stdout)
 def run_node(self,path,node,action,*extra,**envkw):
  self.seed_predecessor_markers(path,node)
  self.seed_parent()
  if node=="plan-check":self.seed_plan_marker(json.loads(path.read_text()))
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),"--route",str(path),"--node",node,"--slug","fallback-"+node,"--parent","owner","--capability-mode","dev","--jobs",str(self.jobs),"--"+action,*extra]
  clean={k:v for k,v in os.environ.items() if not k.startswith("AGENT_DISPATCH_CURRENT_")}
  env={**clean,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),"AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),"AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner","AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",**envkw}
  return subprocess.run(cmd,text=True,capture_output=True,env=env)
 def hop(self,result):
  return next((line.split("=",1)[1] for line in result.stdout.splitlines()
               if line.startswith(("selected_hop=","reason="))),"-")
 def test_dry_run_and_start_agree_on_a_wrong_parent_runtime(self):
  # 2026-08-04 cairn: dry-run reported `check=ok,
  # selected_hop=same-harness-headless` for a route whose sealed parent could
  # never resolve, and only --start descended to inline. The sealed harness
  # here is codex while the running owner is claude -- the transport-axis
  # incident with the harness field substituted, which still compiles.
  path=self.route(same_status="supported")
  wrong={"AGENT_DISPATCH_CURRENT_HARNESS":"claude",
         "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
         "AGENT_DISPATCH_CURRENT_SANDBOX":"adapter-default"}
  # plan-check is now a parallel-group anchor (W3); use the non-group `test`
  # node so register/dry-run parity is exercised on a plain single checker.
  dry=self.run_node(path,"test","dry-run",**wrong)
  reg=self.run_node(path,"test","register",**wrong)
  self.assertEqual((dry.returncode,self.hop(dry)),(reg.returncode,self.hop(reg)),
                   dry.stdout+reg.stdout)
  self.assertEqual(dry.returncode,79,dry.stdout+dry.stderr)
  self.assertIn("selected_hop=inline",dry.stdout)
  self.assertIn("dispatch-evidence-parent-runtime-mismatch",dry.stdout)
  self.assertNotIn("check=ok",dry.stdout)
  # the ledger must name the real cause, not the inline hop's compile-time
  # `runtime-unavailable` constant
  self.assertIn("last_direct_failure_reason=dispatch-evidence-parent-runtime-mismatch",dry.stdout)
 def test_dry_run_resolves_the_live_parent_attempt_like_start(self):
  # Identity matches the tuple, but no owner row with that identity is live:
  # only --start used to notice.
  path=self.route(same_status="supported")
  self.seed_parent(harness="claude",sandbox="adapter-default")
  right={"AGENT_DISPATCH_CURRENT_HARNESS":"codex",
         "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
         "AGENT_DISPATCH_CURRENT_SANDBOX":"workspace-write",
         "HARNESS_CAPACITY_SCORES":"claude:80,codex:20"}
  dry=self.run_node(path,"plan-check","dry-run",**right)
  self.assertNotIn("check=ok",dry.stdout)
  self.assertIn("parent-attempt-not-found",dry.stdout)
  self.assertEqual(dry.returncode,79,dry.stdout+dry.stderr)
 def test_matching_parent_runtime_uses_balanced_checked_headless_band(self):
  path=self.route(same_status="supported")
  right={"AGENT_DISPATCH_CURRENT_HARNESS":"codex",
         "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
         "AGENT_DISPATCH_CURRENT_SANDBOX":"workspace-write",
         "HARNESS_CAPACITY_SCORES":"claude:80,codex:20"}
  dry=self.run_node(path,"plan-check","dry-run",**right)
  self.assertEqual(dry.returncode,0,dry.stdout+dry.stderr)
  self.assertIn("selected_hop=cross-harness-headless",dry.stdout)
  self.assertIn("child_harness=claude",dry.stdout)
  self.assertIn("attempt_count.codex=1",dry.stdout)

 def test_three_harness_stage_ranking_uses_only_recent_attempt_counts(self):
  node={
   "harness_affinity":"diverse",
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"codex","status":"supported"},
     {"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{
   "strategy":"least-recent-attempts","window":30,
   "harness_order":["claude","codex","opencode"],
  }}
  self.jobs.write_text(
   "2026-08-09T00:00:00Z\tdone\t/r\t/w\ta\t"
   "attempt_schema_version=2,registered_worker=1,attempt_id=att-count-a,harness=claude\n"
   "2026-08-09T00:00:01Z\tdone\t/r\t/w\tb\t"
   "attempt_schema_version=2,registered_worker=1,attempt_id=att-count-b,harness=codex\n",
   encoding="utf-8")
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  selected=[hop["candidates"][0]["child_harness"] for hop in hops[:3]]
  self.assertEqual(selected,["opencode","claude","codex"])
  self.assertEqual(context["counts"],{"claude":1,"codex":1,"opencode":0})
 def test_capacity_aware_stage_keeps_opencode_outside_primary_band(self):
  node={
   "harness_affinity":"diverse",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":35},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"codex","status":"supported"},
     {"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{"strategy":"capacity-aware","window":30,
                                 "harness_order":["claude","codex","opencode"]}}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":60,"codex":80,"opencode":100}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  selected=[hop["candidates"][0]["child_harness"] for hop in hops[:3]]
  self.assertEqual(selected,["codex","claude","opencode"])
  self.assertFalse(context["relief_promoted"])
 def test_balanced_stage_fallback_orders_ungated_relief_before_a_gated_primary(self):
  node={
   "harness_affinity":"diverse",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":0},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"codex","status":"supported"},
     {"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{"strategy":"balanced","window":30,
                                 "usage_gate_used_percent":90,
                                 "harness_order":["claude","codex","opencode"]}}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":5,"codex":5,"opencode":80}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  # Ungated relief (opencode) leads; the gated primaries are demoted, not
  # dropped -- they stay reachable later in rank as fallback hops.
  self.assertEqual(context["rank"][0],"opencode")
  self.assertEqual(set(context["rank"]),{"claude","codex","opencode"})
 def _depth_affinity_node(self,depth):
  return {
   "harness_affinity":"diverse",
   "dispatch_depth":depth,
   "harness_policy":{"primary":["claude","codex"],"relief":[],
                     "last_resort":["opencode"],"promote_relief_below":0},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"codex","status":"supported"},
     {"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
 def _depth_affinity_route(self,order,*,affinity=True):
  allocation={"strategy":"balanced","window":30,"usage_gate_used_percent":90,
              "harness_order":list(order)}
  if affinity:
   allocation.update({"depth_affinity":{"owner":"claude","worker":"codex"},
                      "depth_affinity_weight":0.65,"usage_headroom_exponent":2})
  return {"dispatch_allocation":allocation}
 def _depth_affinity_rank(self,order,depth,*,affinity=True):
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":80,"codex":80,"opencode":80}):
   _hops,context=F.ordered_fallback_hops(
    self._depth_affinity_route(order,affinity=affinity),
    self._depth_affinity_node(depth),self.jobs)
  return context["rank"]
 def test_depth_affinity_leads_at_its_own_depth_and_flips_at_the_other(self):
  # Equal headroom and an empty registry, so the neutral order is exactly the
  # declared one. The preference is read from the node's own dispatch_depth:
  # owner->claude at depth 1, worker->codex at depth 2. Both declared orders are
  # exercised so each depth is shown flipping a neutral head, not just agreeing
  # with it.
  self.assertEqual(self._depth_affinity_rank(["claude","codex","opencode"],2,
                                             affinity=False)[0],"claude")
  self.assertEqual(self._depth_affinity_rank(["claude","codex","opencode"],2)[0],"codex")
  self.assertEqual(self._depth_affinity_rank(["claude","codex","opencode"],1)[0],"claude")
  self.assertEqual(self._depth_affinity_rank(["codex","claude","opencode"],1,
                                             affinity=False)[0],"codex")
  self.assertEqual(self._depth_affinity_rank(["codex","claude","opencode"],1)[0],"claude")
  self.assertEqual(self._depth_affinity_rank(["codex","claude","opencode"],2)[0],"codex")
 def test_explicit_capacity_bias_beats_configured_depth_affinity(self):
  # D6/A1: `preferred_for_depth` returns None while a valid HARNESS_CAPACITY_BIAS
  # is set, so the configured preference is neutralized at its single source and
  # the resulting order is identical to the same inputs with the keys absent.
  # No consumer re-reads the env var for this feature, and no re-hoist was added.
  for order in (["claude","codex","opencode"],["codex","claude","opencode"]):
   for bias in ("claude","codex"):
    with mock.patch.dict(os.environ,{"HARNESS_CAPACITY_BIAS":bias}):
     configured=self._depth_affinity_rank(order,2)
     absent=self._depth_affinity_rank(order,2,affinity=False)
    self.assertEqual(configured,absent)
    self.assertEqual(configured[0],bias)
 def test_depth_affinity_cannot_lift_a_gated_harness_over_an_ungated_peer(self):
  # DP-24: the gate bit is the outermost element of the balanced sort key, so a
  # depth preference for a gated harness never crosses the class boundary.
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":80,"codex":5,"opencode":80}):
   _hops,context=F.ordered_fallback_hops(
    self._depth_affinity_route(["claude","codex","opencode"]),
    self._depth_affinity_node(2),self.jobs)
  self.assertEqual(context["rank"][0],"claude")
  self.assertEqual(context["rank"][-1],"codex")
  self.assertEqual(set(context["rank"]),{"claude","codex","opencode"})
 def test_harness_weights_reach_the_stage_fallback_ranking(self):
  # OpenCode sits in the same band as the peers and the declared order would
  # put it first at equal headroom; `allocation.harness_weights` moves it back.
  node=self._depth_affinity_node(2)
  node["harness_policy"]={"primary":["claude","codex","opencode"],"relief":[],
                          "last_resort":[],"promote_relief_below":0}
  def rank(weights):
   route=self._depth_affinity_route(["opencode","claude","codex"],affinity=False)
   if weights:
    route["dispatch_allocation"]["harness_weights"]=weights
   with mock.patch.object(F,"_usage_states",return_value={
       "claude":"ok","codex":"ok","opencode":"ok"}), \
       mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
        "claude":80,"codex":80,"opencode":80}):
    _hops,context=F.ordered_fallback_hops(route,node,self.jobs)
   return context["rank"]
  self.assertEqual(rank(None)[0],"opencode")
  self.assertEqual(rank({"opencode":0.3})[0],"claude")
  self.assertEqual(rank({"opencode":0.3})[-1],"opencode")
 def test_balanced_stage_affinity_cannot_lift_a_gated_harness(self):
  node={
   "harness_affinity":"claude",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":0},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"codex","status":"supported"},
     {"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{"strategy":"balanced","window":30,
                                 "usage_gate_used_percent":90,
                                 "harness_order":["claude","codex","opencode"]}}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":5,"codex":5,"opencode":80}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  # The sealed affinity (claude, gated) is not lifted over the ungated
  # relief, but it is still hoisted to the head of its own gate class.
  self.assertNotEqual(context["rank"][0],"claude")
  self.assertEqual(context["rank"][0],"opencode")
  gated_tail=[h for h in context["rank"] if h!="opencode"]
  self.assertEqual(gated_tail[0],"claude")
 def test_balanced_stage_all_gated_affinity_cannot_beat_global_headroom(self):
  node={
   "harness_affinity":"claude",
   "harness_policy":{"primary":["claude"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":0},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{"strategy":"balanced","window":30,
                                 "usage_gate_used_percent":90,
                                 "harness_order":["claude","opencode"]}}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":2,"opencode":9}):
   _hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  self.assertEqual(context["rank"][:2],["opencode","claude"])
 def _pinned_balanced_node(self):
  return {
   "kind":"pipeline-stage","harness_affinity":"claude",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":0},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"codex","status":"supported"},
     {"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
 def _pinned_balanced_route(self,pin="claude",target="worker"):
  route={"dispatch_allocation":{"strategy":"balanced","window":30,
                                 "usage_gate_used_percent":90,
                                 "harness_order":["claude","codex","opencode"]}}
  if pin:
   route["selection_pins"]={"contract_version":1,target:{"harness":pin,"model":None,"effort":None}}
  return route
 def test_a_sealed_worker_pin_stays_first_even_when_the_usage_gate_would_drop_it(self):
  # The same node and scores as the affinity test above, but the route seals `--pin worker=claude`:
  # claude is gated (5% headroom, not a limit) and still the first hop; the others keep their order.
  node=self._pinned_balanced_node()
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":5,"codex":5,"opencode":80}):
   hops,context=F.ordered_fallback_hops(self._pinned_balanced_route(),node,self.jobs)
   self.assertEqual(context["rank"][0],"claude")
   self.assertEqual(hops[0]["candidates"][0]["child_harness"],"claude")
   self.assertEqual(set(context["rank"]),{"claude","codex","opencode"})
   # no pin, or a pin for another target: today's order (the gated affinity does not lead)
   for route in (self._pinned_balanced_route(pin=None),
                 self._pinned_balanced_route(pin="claude",target="owner"),
                 self._pinned_balanced_route(pin="claude",target="frame")):
    with self.subTest(route=sorted(route.get("selection_pins",{}))):
     _hops,other=F.ordered_fallback_hops(route,node,self.jobs)
     self.assertEqual(other["rank"][0],"opencode")
 def test_a_sealed_worker_pin_that_is_at_its_limit_goes_to_the_tail_as_today(self):
  node=self._pinned_balanced_node()
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"limited","codex":"ok","opencode":"ok"}), \
      mock.patch.object(F.CAPACITY,"capacity_scores",return_value={
       "claude":80,"codex":50,"opencode":50}):
   hops,context=F.ordered_fallback_hops(self._pinned_balanced_route(),node,self.jobs)
  self.assertNotIn("claude",context["rank"][:2])
  self.assertEqual(context["limited"],["claude"])
  skipped=[h["candidates"][0] for h in hops if h["candidates"] and h["candidates"][0].get("_allocation_skip")]
  self.assertEqual([(c["child_harness"],c["_allocation_skip"]) for c in skipped],[("claude","usage-limited")])
 def test_a_pinned_review_on_a_non_peer_harness_is_a_degraded_sole_gate_not_a_refusal(self):
  parent={"parent_harness":"claude","parent_transport":"headless","parent_sandbox":"workspace-write"}
  node={
   "kind":"review-worker","harness_affinity":"opencode",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":35},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {**parent,"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {**parent,"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{"strategy":"balanced","window":30,"usage_gate_used_percent":90,
                                 "harness_order":["claude","codex","opencode"]},
   "owner_harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                           "last_resort":[],"promote_relief_below":35},
   "selection_pins":{"contract_version":1,"worker":{"harness":"opencode","model":None,"effort":None}},
   "nodes":[
    {"id":"plan","model_profile":"balanced-deep",
     "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                       "last_resort":[],"promote_relief_below":35}},
   ]}
  scores={"claude":80,"codex":80,"opencode":5}   # the pinned opencode is gated, the peers are not
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()), \
       mock.patch.object(F.CAPACITY,"capacity_scores",return_value=scores):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=parent)
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"opencode")
  self.assertEqual(context["sole_gate"],"degraded")
  # the same route without the pin keeps the quality-peer harness first
  del route["selection_pins"]
  node["harness_affinity"]="diverse"
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()), \
       mock.patch.object(F.CAPACITY,"capacity_scores",return_value=scores):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=parent)
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"claude")
  self.assertEqual(context["sole_gate"],"ok")
 def _shadowed_claude_node(self):
  # D7's live case reproduced on the third resolver: ordinal 1 seals a
  # foreign-parent (claude) same-harness claude row that would otherwise
  # shadow ordinal 2's checked codex-parent claude row.
  return {
   "harness_affinity":"diverse",
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported",
      "parent_harness":"claude","parent_transport":"headless","parent_sandbox":"workspace-write"},
    ]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {"child_harness":"claude","status":"supported",
      "parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write"},
     {"child_harness":"codex","status":"supported",
      "parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write"},
    ]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
 def test_foreign_parent_row_does_not_claim_the_harness_slot(self):
  node=self._shadowed_claude_node()
  route={"dispatch_allocation":{
   "strategy":"least-recent-attempts","window":30,
   "harness_order":["claude","codex","opencode"],
  }}
  actual_parent={"parent_harness":"codex","parent_transport":"headless","parent_sandbox":"workspace-write"}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=actual_parent)
  ranked_by_harness={h["candidates"][0]["child_harness"]:h["candidates"][0] for h in hops[:len(context["rank"])]}
  self.assertEqual(ranked_by_harness["claude"]["parent_harness"],"codex")
  trailing=[c for h in hops[len(context["rank"]):] for c in h.get("candidates",[]) if c]
  self.assertTrue(any(
   c.get("parent_harness")=="claude" and c.get("child_harness")=="claude" for c in trailing
  ))
 def test_parent_identity_none_preserves_todays_chain(self):
  node=self._shadowed_claude_node()
  route={"dispatch_allocation":{
   "strategy":"least-recent-attempts","window":30,
   "harness_order":["claude","codex","opencode"],
  }}
  with mock.patch.object(F,"_usage_states",return_value={
      "claude":"ok","codex":"ok","opencode":"ok"}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  ranked_by_harness={h["candidates"][0]["child_harness"]:h["candidates"][0] for h in hops[:len(context["rank"])]}
  self.assertEqual(ranked_by_harness["claude"]["parent_harness"],"claude")
 def _gate_node(self,kind="review-worker",affinity="diverse",profiles=("deep","balanced-deep")):
  parent={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"}
  return {
   "kind":kind,
   "harness_affinity":affinity,
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":35},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {**parent,"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {**parent,"child_harness":"codex","status":"supported"},
     {**parent,"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
 def _gate_route(self,owner="opencode",limited=()):
  return {"dispatch_allocation":{
    "strategy":"least-recent-attempts","window":30,
    "harness_order":["claude","codex","opencode"],
   },
   "owner_harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                           "last_resort":[],"promote_relief_below":35},
   "nodes":[
    {"id":"plan","model_profile":"balanced-deep",
     "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                       "last_resort":[],"promote_relief_below":35}},
    {"id":"test","model_profile":"light",
     "harness_policy":{"primary":["claude","codex","opencode"],"relief":[],
                       "last_resort":[],"promote_relief_below":35}},
   ]}
 def _usage_states(self,limited=()):
  return {h:("limited" if h in limited else "ok")
          for h in ("claude","codex","opencode")}
 def test_sd160_same_family_affinity_keeps_sole_gate_diagnostic(self):
  node=self._gate_node(affinity="opencode")
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"opencode")
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["parent_cross_cause"],"-")
 def _counts(self,**counts):
  return {h:counts.get(h,0) for h in ("claude","codex","opencode")}
 def _sd160_review_context(self,*,codex=71,counts=(3,27),affinity="unspecified",
                           limited=(),kind="review-worker",legacy_cross=False):
  parent={"parent_harness":"codex","parent_transport":"headless",
          "parent_sandbox":"workspace-write"}
  node=self._gate_node(kind=kind,affinity=affinity)
  node.update(id="review",unit="research/plan-review",dispatch_depth=2,
              model_profile="balanced-deep",parent_cross_preference=legacy_cross)
  node["harness_policy"]={"primary":["claude","codex"],"relief":[],
                          "last_resort":[],"promote_relief_below":0}
  node["fallback_hops"]=[
   {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
    {**parent,"child_harness":"codex","status":"supported"}]},
   {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
    {**parent,"child_harness":"claude","status":"supported"}]},
  ]
  route=self._gate_route()
  route["dispatch_allocation"].update(strategy="balanced",usage_gate_used_percent=85,
    depth_affinity={"owner":"claude","worker":"codex"},
    depth_affinity_weight=0.65,usage_headroom_exponent=2)
  before=json.dumps((route,node),sort_keys=True)
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states(limited)), \
       mock.patch.object(F,"attempt_counts",return_value=self._counts(claude=counts[0],codex=counts[1])), \
       mock.patch.object(F.CAPACITY,"capacity_scores",return_value={"claude":8.,"codex":float(codex),"opencode":0.}):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=parent)
  self.assertEqual(json.dumps((route,node),sort_keys=True),before)
  return route,node,hops,context

 def test_sd160_real_incidents_keep_capacity_order_for_independent_review(self):
  for codex,counts,affinity in [(71,(3,27),"unspecified"),(77,(9,21),"diverse")]:
   for kind,legacy in [("review-worker",False),("pipeline-stage",True)]:
    with self.subTest(codex=codex,kind=kind,legacy=legacy):
     _,_,hops,context=self._sd160_review_context(codex=codex,counts=counts,
       affinity=affinity,kind=kind,legacy_cross=legacy)
     self.assertEqual(context["rank"],["codex","claude"])
     self.assertEqual(hops[0]["fallback_hop"],"same-harness-headless")
     self.assertEqual(context["parent_cross"],"not-applicable")
     self.assertEqual(context["sole_gate"],"ok")

 def test_sd160_review_affinity_cannot_lift_gated_harness(self):
  _,_,_,context=self._sd160_review_context(affinity="claude")
  self.assertEqual(context["rank"],["codex","claude"])
  # Both candidates gated: preserve the maximum-headroom scarcity fallback.
  _,_,_,context=self._sd160_review_context(codex=12,affinity="claude")
  self.assertEqual(context["rank"],["codex","claude"])

 def test_sd160_review_hard_limit_still_excludes_roomy_harness(self):
  _,_,hops,context=self._sd160_review_context(limited=("codex",))
  self.assertEqual(context["rank"],["claude","codex"])
  self.assertEqual(hops[1]["candidates"][0]["_allocation_skip"],"usage-limited")
  self.assertEqual(context["sole_gate"],"ok")

 def test_sd160_final_fallback_receipt_records_actual_child_without_cross_degradation(self):
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  node=next(n for n in route["nodes"] if n["id"]=="plan-check")
  _,_,_,context=self._sd160_review_context()
  # Model a failed ranked head followed by a successful later candidate.
  # Historical context must not revive obsolete same-harness degradation.
  context.update(parent_cross="degraded",parent_cross_cause="affinity-pinned")
  args=SimpleNamespace(action="start",slug="actual-review",jobs=self.jobs,route=path)
  for harness,sole in [("claude","ok"),("codex","ok"),("opencode","degraded")]:
   with self.subTest(harness=harness):
    out=io.StringIO()
    with mock.patch.dict(os.environ,{"AGENT_HOME":str(ROOT),"AGENT_DISPATCH_JOBS":str(self.jobs)}), contextlib.redirect_stdout(out):
     F._emit_child_success(args,route,node,context,self.tuple(harness,"supported"),
       attempt_id="att-final-"+harness,fallback_hop="same-harness-headless" if harness=="codex" else "cross-harness-headless")
    receipt=self.cli_verdict(out.getvalue())
    self.assertEqual(receipt["parent_cross"],"not-applicable")
    self.assertEqual(receipt["parent_cross_cause"],"-")
    self.assertEqual(receipt["sole_gate"],sole)
    saved=[json.loads(line) for line in Path(receipt["allocation_ledger"]).read_text().splitlines()]
    actual=next(row for row in saved if row["attempt_id"]=="att-final-"+harness)
    self.assertEqual(actual["child_harness"],harness)
    self.assertEqual(actual["unit"],node["unit"])
    self.assertEqual(actual["capacity"],context["capacity"])
    self.assertEqual(actual["parent_cross"],"not-applicable")
    self.assertEqual(actual["sole_gate"],sole)
  ledger=Path(self.tmp.name)/"degradations"/f"{route['route_id']}.jsonl"
  rows=[json.loads(line) for line in ledger.read_text().splitlines()]
  self.assertFalse([row for row in rows if row.get("reason")=="parent-cross-same-harness"])
  self.assertEqual(len([row for row in rows if row.get("reason")=="sole-gate-non-peer-harness"]),1)
 def test_sd160_existing_eligibility_quality_and_allocation_precedence(self):
  parent={"parent_harness":"opencode","parent_transport":"headless",
          "parent_sandbox":"workspace-write"}
  def ranked(node,route,*,counts=None,identity=parent,states=None):
   with mock.patch.object(F,"_usage_states",
                          return_value=states or self._usage_states()), \
        mock.patch.object(F,"attempt_counts",
                          return_value=counts or self._counts()):
    _hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=identity)
   return context

  with self.subTest(step=1,rule="explicit target"):
   with dispatch_defaults_config_text(
     "schema_version: 1\ndepth1_owner: [claude, codex]\nopencode:\n  relief_only: true\n"
     "capabilities:\n  autopilot-code:\n    plan: codex\n    execute: diverse\n"
     "    test: diverse\n    report: claude\n"):
    nodes=[{"id":"plan","dispatch_depth":2,"model_profile":"deep"}]
    R._seal_dispatch_defaults(nodes,"autopilot-code")
    self.assertEqual(nodes[0]["harness_affinity"],"codex")
   pinned=self._gate_node(affinity="codex")
   pinned["harness_policy"]={"primary":["claude"],"relief":["opencode"],
                             "last_resort":["codex"],"promote_relief_below":0}
   lower=self._counts(claude=1,codex=9,opencode=0)
   context=ranked(pinned,self._gate_route(),counts=lower,identity=None)
   self.assertEqual(context["rank"][0],"codex")
   unpinned=self._gate_node()
   unpinned["harness_policy"]=pinned["harness_policy"]
   context=ranked(unpinned,self._gate_route(),counts=lower,identity=None)
   self.assertNotEqual(context["rank"][0],"codex")

  with self.subTest(step=2,rule="hard eligibility"):
   node=self._gate_node(affinity="codex")
   node["fallback_hops"][1]["candidates"][0]["status"]="unsupported"
   context=ranked(node,self._gate_route())
   self.assertNotIn("codex",context["rank"])
   self.assertEqual(context["rank"][0],"claude")

  with self.subTest(step=3,rule="same-family affinity"):
   context=ranked(self._gate_node(affinity="opencode"),self._gate_route())
   self.assertEqual(context["rank"][0],"opencode")
   self.assertEqual(context["parent_cross"],"not-applicable")
   self.assertEqual(context["parent_cross_cause"],"-")

  with self.subTest(step=4,rule="quality peer over non-peer"):
   context=ranked(self._gate_node(),self._gate_route(),
                  counts=self._counts(claude=5,codex=6,opencode=0))
   self.assertEqual(context["rank"],["claude","codex","opencode"])
   self.assertEqual(context["parent_cross"],"not-applicable")

  with self.subTest(step=5,rule="band over least-recent"):
   node=self._gate_node()
   node["harness_policy"]={"primary":["claude"],"relief":["codex"],
                           "last_resort":["opencode"],"promote_relief_below":0}
   route=self._gate_route()
   route["dispatch_allocation"]["strategy"]="capacity-aware"
   with mock.patch.object(F.CAPACITY,"capacity_scores",
                          return_value={"claude":90.0,"codex":90.0,"opencode":90.0}):
    context=ranked(node,route,counts=self._counts(claude=9,codex=4,opencode=0),
                   identity=None)
   self.assertEqual(context["rank"],["claude","codex","opencode"])
   self.assertEqual(context["quality_band"],"primary")

  with self.subTest(step=6,rule="least-recent over declared order"):
   node=self._gate_node()
   context=ranked(node,self._gate_route(),
                  counts=self._counts(claude=3,codex=1,opencode=0),
                  identity=None)
   self.assertEqual(context["rank"],["opencode","codex","claude"])

  with self.subTest(step=7,rule="declared order tie-break"):
   route=self._gate_route()
   context=ranked(node,route,identity=None)
   self.assertEqual(context["rank"],["claude","codex","opencode"])
   route=self._gate_route()
   route["dispatch_allocation"]["harness_order"]=["opencode","codex","claude"]
   context=ranked(node,route,identity=None)
   self.assertEqual(context["rank"],["opencode","codex","claude"])
 def test_sd160_quality_peer_order_preserves_allocation_order(self):
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  selected=[hop["candidates"][0]["child_harness"] for hop in hops[:len(context["rank"])]]
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(selected,["claude","codex","opencode"])
  self.assertEqual([h for h in selected if h in {"claude","codex"}],
                   ["claude","codex"])
  self.assertEqual([h for h in selected if h not in {"claude","codex"}],
                   ["opencode"])
 def test_sd160_affinity_head_is_normal_on_owner_family(self):
  node=self._gate_node(affinity="opencode")
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"opencode")
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["parent_cross_cause"],"-")
 def test_sd160_cross_usage_limited_does_not_degrade_parent_family(self):
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",
       return_value=self._usage_states(limited=("claude","codex"))):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["parent_cross_cause"],"-")
 def test_sd160_same_harness_receipt_and_allocation_ledger_at_the_cli(self):
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec",
        "workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  evidence={"tuples":[self.tuple("codex","supported"),self.tuple("claude","unsupported")],
            "native_subagent":[{"harness":"codex","transport":"headless",
                                "execution_surface":"codex-native-subagent",
                                "registered_worker":False,"status":"unsupported",
                                "check_source":"fixture"}]}
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   route=R.compile_route("autopilot-code","dev","strong",self.repo,self.art,
     signals=["shared-contract"],transport="headless",tracking="tracked",
     tracked_gate_evidence=gate,dispatch_evidence=evidence)
  path=Path(self.tmp.name)/"evidence-pair-route.json"
  path.write_text(json.dumps(route),encoding="utf-8")
  self.seed_predecessor_markers(path,"plan-check")
  self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),
       "--route",str(path),"--node","plan-check","--slug","fb-evidence-pair",
       "--parent","owner","--capability-mode","dev","--worker-mode","qa/plan-review",
       "--model-role","fast reviewer","--jobs",str(self.jobs),"--dry-run"]
  env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),
       "AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),
       "AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner",
       "AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",
       "AGENT_DISPATCH_CURRENT_HARNESS":"codex",
       "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
       "AGENT_DISPATCH_CURRENT_SANDBOX":"workspace-write"}
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  receipt=self.cli_verdict(result.stdout)
  self.assertEqual(receipt["child_harness"],"codex")
  self.assertEqual(receipt["parent_cross"],"not-applicable")
  self.assertEqual(receipt["parent_cross_cause"],"-")
  self.assertEqual(receipt["sole_gate"],"ok")
  ledger=Path(self.tmp.name)/"degradations"/f"{route['route_id']}.jsonl"
  rows=[json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
  self.assertFalse([row for row in rows if row.get("reason")=="parent-cross-same-harness"])
  allocation=Path(self.tmp.name)/"allocation"/f"{route['route_id']}.jsonl"
  saved=[json.loads(line) for line in allocation.read_text().splitlines()]
  self.assertEqual(saved[-1]["child_harness"],"codex")
  self.assertEqual(saved[-1]["parent_cross"],"not-applicable")
  self.assertEqual(saved[-1]["sole_gate"],"ok")
 def test_ac12_sole_gate_receipt_and_ledger_evidence_pair_at_the_cli(self):
  gate={"spec_read":{"satisfied":True,"source":"fixture"},"drift_verdict":"within-spec",
        "workflow_mode":"tracked","artifact_guard":{"satisfied":True,"source":"fixture"}}
  evidence={"tuples":[self.tuple("opencode","supported"),
                      self.tuple("codex","unsupported"),
                      self.tuple("claude","unsupported")],
            "native_subagent":[{"harness":"codex","transport":"headless",
                                "execution_surface":"codex-native-subagent",
                                "registered_worker":False,"status":"unsupported",
                                "check_source":"fixture"}]}
  with mock.patch.dict(os.environ,self.launch_roots_env()):
   route=R.compile_route("autopilot-code","dev","strong",self.repo,self.art,
     signals=["shared-contract"],transport="headless",tracking="tracked",
     tracked_gate_evidence=gate,dispatch_evidence=evidence)
  path=Path(self.tmp.name)/"sole-gate-route.json"
  path.write_text(json.dumps(route),encoding="utf-8")
  self.seed_predecessor_markers(path,"plan-check")
  self.seed_parent()
  cmd=[sys.executable,str(ROOT/"utilities/stage-dispatch-fallback.py"),
       "--route",str(path),"--node","plan-check","--slug","fb-sole-gate",
       "--parent","owner","--capability-mode","dev","--worker-mode","qa/plan-review",
       "--model-role","fast reviewer","--jobs",str(self.jobs),"--dry-run"]
  env={**os.environ,"AGENT_HOME":str(ROOT),"AGENT_ARTIFACT_ROOT":str(self.art),
       "AGENT_MODEL_GOVERNOR_ROOT":str(self.art/".runtime/model-worker-governor"),
       "AGENT_DISPATCH_JOBS":str(self.jobs),"AGENT_DISPATCH_SELF_SLUG":"owner",
       "AGENT_DISPATCH_ATTEMPT_ID":"att-fallback-parent",
       "AGENT_DISPATCH_CURRENT_HARNESS":"codex",
       "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
       "AGENT_DISPATCH_CURRENT_SANDBOX":"workspace-write"}
  result=subprocess.run(cmd,text=True,capture_output=True,env=env)
  self.assertEqual(result.returncode,0,result.stdout+result.stderr)
  receipt=self.cli_verdict(result.stdout)
  self.assertEqual(receipt["sole_gate"],"degraded")
  self.assertEqual(receipt["child_harness"],"opencode")
  self.assertEqual(receipt["parent_cross"],"not-applicable")
  ledger=Path(self.tmp.name)/"degradations"/f"{route['route_id']}.jsonl"
  self.assertTrue(ledger.is_file(),result.stdout)
  rows=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()]
  sole=[row for row in rows if row.get("reason")=="sole-gate-non-peer-harness"]
  self.assertEqual(len(sole),1,rows)
  self.assertEqual(sole[0]["sole_gate"],"degraded")
  self.assertEqual(sole[0]["leg_class"],"peer")
  self.assertEqual(sole[0]["route_node"],"plan-check")
  self.assertEqual(sole[0]["writer"],"stage-dispatch-fallback.py")
 def test_ac17_parent_identity_absent_marks_not_applicable(self):
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  baseline=[]
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs)
  baseline=[hop["candidates"][0]["child_harness"] for hop in hops[:len(context["rank"])]]
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["sole_gate"],"ok")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops2,context2=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual([hop["candidates"][0]["child_harness"] for hop in hops2[:len(context2["rank"])]],
                   ["claude","codex","opencode"])
  self.assertIsNone(F._persist_parent_cross_ledger(None, route, node, None))
 def test_ac18_non_target_node_keeps_six_repeat_rotation(self):
  parent={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"}
  node={
   "kind":"pipeline-stage",
   "harness_affinity":"diverse",
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {**parent,"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {**parent,"child_harness":"codex","status":"supported"},
     {**parent,"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{
    "strategy":"least-recent-attempts","window":30,
    "harness_order":["claude","codex","opencode"],
   }}
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(context["parent_cross"],"not-applicable")
  selected=[hop["candidates"][0]["child_harness"] for hop in hops[:len(context["rank"])]]
  self.assertEqual(set(selected),{"claude","codex","opencode"})
 def test_sd160_opencode_owner_retains_quality_peer_sole_gate(self):
  node=self._gate_node()
  route=self._gate_route(owner="opencode")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["sole_gate"],"ok")
  self.assertEqual(context["quality_peer_families"],["claude","codex"])
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"claude")
 def test_ac100b_sole_gate_degraded_when_no_quality_peer_eligible(self):
  node=self._gate_node()
  node["harness_policy"]={"primary":["claude","codex"],"relief":["opencode"],
                          "last_resort":[],"promote_relief_below":0}
  route={"dispatch_allocation":{
    "strategy":"least-recent-attempts","window":30,
    "harness_order":["claude","codex","opencode"],
   },
   "owner_harness_policy":{"primary":["claude"],"relief":["opencode"],
                           "last_resort":[],"promote_relief_below":0},
   "nodes":[
    {"id":"plan","model_profile":"balanced-deep",
     "harness_policy":{"primary":["codex"],"relief":["opencode"],
                       "last_resort":[],"promote_relief_below":0}},
   ]}
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,
     parent_identity={"parent_harness":"opencode","parent_transport":"headless","parent_sandbox":"workspace-write"})
  self.assertEqual(context["quality_peer_families"],[])
  self.assertEqual(context["sole_gate"],"degraded")
 def test_sd160_sole_gate_reorder_can_choose_owner_family_normally(self):
  parent={"parent_harness":"claude","parent_transport":"headless","parent_sandbox":"workspace-write"}
  node={
   "kind":"review-worker",
   "harness_affinity":"diverse",
   "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                     "last_resort":[],"promote_relief_below":35},
   "fallback_hops":[
    {"ordinal":1,"fallback_hop":"same-harness-headless","candidates":[
     {**parent,"child_harness":"claude","status":"supported"}]},
    {"ordinal":2,"fallback_hop":"cross-harness-headless","candidates":[
     {**parent,"child_harness":"opencode","status":"supported"}]},
    {"ordinal":3,"fallback_hop":"native-subagent","candidates":[]},
    {"ordinal":4,"fallback_hop":"inline","candidates":[]},
   ],
  }
  route={"dispatch_allocation":{
    "strategy":"least-recent-attempts","window":30,
    "harness_order":["claude","codex","opencode"],
   },
   "owner_harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                           "last_resort":[],"promote_relief_below":35},
   "nodes":[
    {"id":"plan","model_profile":"balanced-deep",
     "harness_policy":{"primary":["claude","codex"],"relief":["opencode"],
                       "last_resort":[],"promote_relief_below":35}},
   ]}
  self.jobs.write_text(
   "2026-08-09T00:00:00Z\tdone\t/r\t/w\ta\t"
   "attempt_schema_version=2,registered_worker=1,attempt_id=att-claude,harness=claude\n",
   encoding="utf-8")
  with mock.patch.object(F,"_usage_states",return_value=self._usage_states()):
   hops,context=F.ordered_fallback_hops(route,node,self.jobs,parent_identity=parent)
  self.assertEqual(hops[0]["candidates"][0]["child_harness"],"claude")
  self.assertEqual(context["parent_cross"],"not-applicable")
  self.assertEqual(context["parent_cross_cause"],"-")
  self.assertEqual(context["sole_gate"],"ok")
 def test_g3_verdicts_recomputed_for_actual_launched_child(self):
  context={
   "parent_cross":"ok","parent_cross_cause":"-","sole_gate":"ok",
   "affinity":None,"rank":["claude","codex"],
   "eligible":["claude","codex"],"limited":[],
   "owner_family":"claude","quality_peer_set":frozenset({"claude","codex"}),
  }
  reopened=F._recompute_verdicts_for_child(context,"opencode")
  self.assertEqual(reopened["parent_cross"],"not-applicable")
  self.assertEqual(reopened["sole_gate"],"degraded")
  same_family=F._recompute_verdicts_for_child(context,"claude")
  self.assertEqual(same_family["parent_cross"],"not-applicable")
  self.assertEqual(same_family["sole_gate"],"ok")
  self.assertEqual(same_family["parent_cross_cause"],"-")
  cross=F._recompute_verdicts_for_child(context,"codex")
  self.assertEqual(cross["parent_cross"],"not-applicable")
  self.assertEqual(cross["sole_gate"],"ok")
 def test_foreign_only_evidence_still_traces_the_parent_runtime_mismatch(self):
  # Keeping the foreign row in the trailing band (rather than dropping it) is
  # what keeps this trace meaningful: the sealed evidence is for parent codex,
  # the live owner runs as claude.
  path=self.route(same_status="supported")
  wrong={"AGENT_DISPATCH_CURRENT_HARNESS":"claude",
         "AGENT_DISPATCH_CURRENT_TRANSPORT":"headless",
         "AGENT_DISPATCH_CURRENT_SANDBOX":"adapter-default"}
  dry=self.run_node(path,"plan-check","dry-run",**wrong)
  self.assertEqual(dry.returncode,79,dry.stdout+dry.stderr)
  self.assertIn("skipped-dispatch-evidence-parent-runtime-mismatch",dry.stdout)
  self.assertIn("last_direct_failure_reason=dispatch-evidence-parent-runtime-mismatch",dry.stdout)
 def test_registry_infrastructure_failure_is_not_a_candidate_failure(self):
  # An unwritable registry is a hard stop at --start. Treating it as one more
  # exhausted candidate would descend to inline and recreate the divergence
  # the dry-run parent check exists to remove.
  path=self.route(same_status="supported"); route=json.loads(path.read_text())
  node=next(n for n in route["nodes"] if n["id"]=="plan-check")
  row=node["fallback_hops"][0]["candidates"][0]
  args=SimpleNamespace(action="dry-run",parent="owner",jobs=self.jobs,
                       inherited_jobs=str(self.jobs))
  with mock.patch.object(F,"resolve_live_parent_attempt",
                         side_effect=F.DispatchContractError("global-registry-unwritable","x")):
   reason=F.parent_runtime_failure(args,route,row,None)
  self.assertEqual(reason,"global-registry-unwritable")
  self.assertNotIn(reason,F.CANDIDATE_SCOPED_PARENT_FAILURES)
 def test_partial_parent_runtime_identity_fails_closed(self):
  path=self.route(same_status="supported")
  result=self.run_node(path,"plan-check","dry-run",
                       AGENT_DISPATCH_CURRENT_HARNESS="codex")
  self.assertEqual(result.returncode,73,result.stdout+result.stderr)
  self.assertIn("reason=dispatch-evidence-parent-runtime-incomplete",result.stdout)
 def test_native_hop_accepts_only_a_live_route_owned_exact_child(self):
  path=self.route(native="supported");route=json.loads(path.read_text());attempt="att-nativeproof001"
  proc=subprocess.Popen(["sleep","30"])
  try:
   start=(Path("/proc")/str(proc.pid)/"stat").read_text().split()[21]
   pipe=(f"attempt_schema_version=2,route_id={route['route_id']},route_node=plan,attempt_id={attempt},"
         f"dispatch_depth=2,transport=headless,harness=codex,execution_surface=codex-native-subagent,"
         f"registered_worker=0,fallback_hop=native-subagent,"
         f"pid={proc.pid},pid_start={start}")
   self.jobs.write_text(f"2026-07-16T00:00:00Z\topen\t/repo\t{self.repo}\tnative\t{pipe}\n")
   same="codex/headless/workspace-write/codex/conductor";cross="codex/headless/workspace-write/claude/conductor"
   result=self.run_chain(path,"--failed-tuple",same,"--failed-tuple",cross,"--native-attempt-id",attempt)
   self.assertEqual(result.returncode,78,result.stdout+result.stderr)
   self.assertIn("child_proof=registry-exact-pid",result.stdout)
  finally:
   proc.terminate();proc.wait()
 def test_direct_env_strips_owner_route_binding_but_keeps_node_binding(self):
  extra={"AGENT_OWNER_ROUTE_FILE":"/tmp/owner-route.json","AGENT_OWNER_ROUTE_ID":"rt-owner",
         "AGENT_OWNER_ROUTE_HASH":"sha256:owner","AGENT_DISPATCH_BROKER_TOKEN":"x",
         "AGENT_ROUTE_FILE":"/tmp/node-route.json","AGENT_ROUTE_ID":"rt-node"}
  with mock.patch.dict(os.environ,extra):
   result=F.direct_env()
  self.assertNotIn("AGENT_OWNER_ROUTE_FILE",result)
  self.assertNotIn("AGENT_OWNER_ROUTE_ID",result)
  self.assertNotIn("AGENT_OWNER_ROUTE_HASH",result)
  self.assertNotIn("AGENT_DISPATCH_BROKER_TOKEN",result)
  self.assertEqual(result["AGENT_ROUTE_FILE"],"/tmp/node-route.json")
  self.assertEqual(result["AGENT_ROUTE_ID"],"rt-node")

 def test_prelaunch_process_block_reasons_are_consumed_by_every_launcher(self):
  """The sibling-gate reasons must reach exit 78 / child_spawned=0 everywhere.

  They do not share the `predecessor-process-` prefix the launchers used to
  match on, so a prefix test would have silently dropped them to exit 65 --
  "the wrapper refused" instead of "nothing spawned, waiting may help". Every
  launcher matches the shared tuple instead, and none may go back.
  """
  for reason in ("prior-attempt-still-live","prior-attempt-unverifiable",
                 "predecessor-process-draining","predecessor-process-unverifiable"):
   self.assertIn(reason,F.PRELAUNCH_PROCESS_BLOCK_REASONS)
  launchers=[ROOT/"utilities/stage-dispatch-fallback.py",ROOT/"utilities/dispatch-batch.py"]
  launchers+=[ROOT/"adapters"/name/"bin/dispatch-headless.py"
              for name in ("claude","codex","opencode")]
  for path in launchers:
   source=path.read_text(encoding="utf-8")
   self.assertIn("PRELAUNCH_PROCESS_BLOCK_REASONS",source,path)
   self.assertNotIn('startswith("predecessor-process-")',source,path)

 # SD-154/B-2 defect #2 -----------------------------------------------------
 def test_a_sd154_2_route_state_refusal_does_not_descend_to_inline(self):
  """A wrapper that reports a route-state reason (13.59.3 rule 6) stops with
  `child_spawned=0` -- it never falls to `selected_hop=inline`/
  `runtime-unavailable`, the luna observation's actual mis-classification.

  Uses the revised-evidence member of the set (not the gate's own missing-
  dependency reason, which `dispatch_completion_marker.test.py`'s static
  guardian keeps out of every file but `dispatch_contract.py` and the
  adapters' generic relay).
  """
  for stub_reason in (
      "completion-evidence-revised-unrecorded",
      "completion-evidence-superseded",
  ):
   with self.subTest(reason=stub_reason):
    self.assertIn(stub_reason,F.ROUTE_STATE_REFUSAL_REASONS)
    path=self.route()
    real_run=subprocess.run
    def fake_run(cmd,*args,**kwargs):
     if any("dispatch-headless.py" in str(part) for part in cmd):
      return SimpleNamespace(
       returncode=65,
       stdout=f"check=failed\nreason={stub_reason}\ndetail=plan\nnext_action=repair-route-state\n",
       stderr="",
      )
     return real_run(cmd,*args,**kwargs)
    buf=io.StringIO()
    with self.dispatch_env():
     with mock.patch.object(F.subprocess,"run",side_effect=fake_run):
      with contextlib.redirect_stdout(buf):
       try:
        code,_observation=self.run_inline(path)
       except SystemExit as exc:
        code=exc.code
    output=buf.getvalue()
    fields=F.output_fields(output)
    self.assertEqual(code,65,output)
    self.assertEqual(fields.get("reason"),stub_reason,output)
    self.assertEqual(fields.get("child_spawned"),"0",output)
    self.assertEqual(fields.get("next_action"),"repair-route-state",output)
    self.assertNotIn("selected_hop=inline",output)
    self.assertNotIn("runtime-unavailable",output)

 # M1's real (not stubbed) `completion_marker_gate` half lives in
 # `dispatch_completion_marker.test.py::CompletionMarkerTest::
 # test_a_sd154_2_real_gate_reports_next_action_for_route_state_refusal`,
 # which runs the real wrapper `--start` subprocess -- this file's own
 # helpers drive the wrapper as `--dry-run` with real predecessor markers,
 # so they share its read-only currency/readiness gate. This class's test
 # above proves the OTHER half of the same regression: a fallback chain that
 # receives that exact reason from a wrapper never descends to inline.


class LaunchTupleReportOnlyTest(unittest.TestCase):
 """SD-114 (2)-C: P1/P2/P3 producer wiring + report-only stage 1
 byte-identical guarantee. Uses `FallbackTest.run_inline`/`dispatch_env`
 in-process (never a subprocess) because these fixtures patch `F` internals,
 which only reaches an in-process call. (The `AGENT_HOME`-vs-installed-release
 grounding digest mismatch this class used to route around was a fixture
 defect, not a runtime one: `route()`/`dispatch_env` now seal and launch
 against one root, so `run_chain`-based tests hold too.)"""

 # A fully-specified but harness-mismatched parent identity forces every
 # candidate down either P2 (codex, unsupported by the default fixture) or
 # P3 (claude, supported but sealed to a different runtime) regardless of
 # live capacity/usage-based re-ranking -- both hop kinds are always
 # visited by the candidate loop, so this is deterministic across runs.
 WRONG_PARENT = {"AGENT_DISPATCH_CURRENT_HARNESS": "opencode",
                 "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                 "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write"}

 def setUp(self):
  self.base = FallbackTest("test_cross_harness_direct_precedes_inline")
  self.base.setUp()
  self.addCleanup(self.base.tearDown)

 def _route_and_ledger(self, **usage_kwargs):
  with self.base.dispatch_env(**self.WRONG_PARENT):
   path = self.base.route(**usage_kwargs)
   route_id = json.loads(path.read_text())["route_id"]
  return path, route_id

 # `_allocation_skip` is only ever assigned to a row that already matched
 # the sealed parent identity (`ordered_fallback_hops`'s `headless` dict) --
 # a mismatched parent routes the row to `trailing_rows` before usage
 # limiting is even considered, so P1's fixture needs a MATCHING identity
 # (unlike P2/P3, which need the mismatch). Both harnesses are marked
 # usage-limited so nothing ever reaches a real wrapper subprocess spawn.
 RIGHT_PARENT = {"AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                 "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                 "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write"}

 def test_b47_1_allocation_skip_producer_fires(self):
  # P1: a live-usage-limited candidate has exact pre-launch tuple evidence.
  with self.base.dispatch_env(**self.RIGHT_PARENT):
   path = self.base.route(same_status="supported")
   route_id = json.loads(path.read_text())["route_id"]
  with self.base.dispatch_env(**self.RIGHT_PARENT), \
       mock.patch.object(F, "_usage_states",
                         return_value=self.base._usage_states(limited=("codex", "claude"))):
   code, observation = self.base.run_inline(path)
  self.assertEqual(code, 79)
  rows = self.base.ledger_rows(route_id)
  by_key = {r["tuple_key"]: r for r in rows}
  self.assertEqual(by_key["codex/headless/workspace-write/codex/conductor"]["rejection_class"],
                    "allocation-skip")
  self.assertEqual(by_key["codex/headless/workspace-write/codex/conductor"]["evidence_ref"],
                    "usage-limited")
  self.assertEqual(observation.unrecorded, 0)

 def test_b47_1_candidate_unsupported_producer_fires(self):
  # P2: codex is `status="unsupported"` by the default fixture.
  path, route_id = self._route_and_ledger()
  with self.base.dispatch_env(**self.WRONG_PARENT):
   code, observation = self.base.run_inline(path)
  self.assertEqual(code, 79)
  rows = self.base.ledger_rows(route_id)
  by_class = {r["rejection_class"]: r for r in rows}
  self.assertIn("candidate-unsupported", by_class)
  self.assertEqual(by_class["candidate-unsupported"]["tuple_key"],
                    "codex/headless/workspace-write/codex/conductor")
  self.assertEqual(observation.unrecorded, 0)

 def test_b47_1_sealed_parent_not_live_producer_fires(self):
  # P3: claude is supported, but WRONG_PARENT seals a different runtime, so
  # `parent_runtime_failure()` returns the CANDIDATE_SCOPED
  # `dispatch-evidence-parent-runtime-mismatch`.
  path, route_id = self._route_and_ledger()
  with self.base.dispatch_env(**self.WRONG_PARENT):
   code, observation = self.base.run_inline(path)
  self.assertEqual(code, 79)
  rows = self.base.ledger_rows(route_id)
  by_class = {r["rejection_class"]: r for r in rows}
  self.assertIn("sealed-parent-not-live", by_class)
  self.assertEqual(by_class["sealed-parent-not-live"]["tuple_key"],
                    "codex/headless/workspace-write/claude/conductor")
  self.assertEqual(by_class["sealed-parent-not-live"]["evidence_ref"],
                    "dispatch-evidence-parent-runtime-mismatch")
  self.assertEqual(observation.unrecorded, 0)

 def test_b47_2_registry_failures_returns_nonempty(self):
  # Regression pin (unrelated legacy axis, §5.4): `registry_failures()`
  # itself must remain fully functional and unchanged by this cycle.
  route = json.loads(self.base.route().read_text())
  pipe = (f"capability=autopilot-code,route_id={route['route_id']},route_node=plan,"
          "parent=owner,attempt_id=att-prior000000,parent_harness=codex,"
          "parent_transport=headless,parent_sandbox=workspace-write,"
          "child_harness=codex,launch_authority=conductor,note=dead-launch-error,"
          "failure_class=launch-tuple")
  self.base.jobs.write_text(
   f"2026-07-16T00:00:00Z\tdone\t/repo\t{self.base.repo}\tfallback-plan\t{pipe}\n")
  failures = F.registry_failures(self.base.jobs, route["route_id"], "plan")
  self.assertNotEqual(failures, {})
  self.assertIn("codex/headless/workspace-write/codex/conductor", failures)

 def test_b47_5_report_only_stdout_byte_identical(self):
  path, route_id = self._route_and_ledger()
  with self.base.dispatch_env(**self.WRONG_PARENT):
   self.assertEqual(self.base.ledger_rows(route_id), [])
   buf1 = io.StringIO()
   with contextlib.redirect_stdout(buf1):
    code1, observation1 = self.base.run_inline(path)
   self.assertNotIn("launch-tuple", buf1.getvalue())
   buf2 = io.StringIO()
   with contextlib.redirect_stdout(buf2):
    code2, observation2 = self.base.run_inline(path)
  self.assertEqual(code1, code2)
  self.assertEqual(buf1.getvalue(), buf2.getvalue(),
                    "selection output must be byte-identical whether or not "
                    "the launch-tuple ledger already marks candidates spent")
  self.assertEqual(set(observation2.spent),
                    {"codex/headless/workspace-write/codex/conductor",
                     "codex/headless/workspace-write/claude/conductor"})

 def test_b47_5_report_written_on_unarmed_preprocessing_failure_and_armed_chain_exhausted(self):
  # gap1 correction 3 (owner_arbitration.md 🟡-2 / plan.md §10-R5): this name
  # used to say "every exit path" but only ever drove two of the source's
  # twelve (plan.md §4 (2)-C's E1-E12 table). It exercises exactly:
  #  - E1, unarmed: a genuine preprocessing failure (parent identity partially
  #    exported, raised before `observation.arm()` is ever reached) writes
  #    zero report rows.
  #  - E12, armed: a fully-armed chain-exhausted exit writes exactly one.
  # It does NOT drive:
  #  - E8 (normal success, `return 0` after a real wrapper spawn): every
  #    fixture in this class deliberately usage-limits or parent-mismatches
  #    every candidate so nothing ever reaches a real wrapper subprocess
  #    spawn (see the class docstring) -- reaching E8 needs `FallbackTest`'s
  #    subprocess-spawning fixtures (e.g. `run_chain`), not this in-process one.
  #  - E10 (native-subagent, `return 78`): every route this class compiles
  #    passes `native="unsupported"` (the `route()` default), so the
  #    same/cross-harness-headless hops never exhaust into the
  #    native-subagent hop. Reaching E10 needs a `native="supported"` route,
  #    which no fixture here builds.
  with self.base.dispatch_env(AGENT_DISPATCH_CURRENT_HARNESS="codex"):
   path = self.base.route(same_status="supported")
   route_id = json.loads(path.read_text())["route_id"]
   code = self.base.run_inline_main(path)
  self.assertEqual(code, 73)
  self.assertEqual(self.base.report_rows(route_id), [])

  path2, route_id2 = self._route_and_ledger()
  with self.base.dispatch_env(**self.WRONG_PARENT):
   code2 = self.base.run_inline_main(path2)
  self.assertEqual(code2, 79)
  reports = self.base.report_rows(route_id2)
  self.assertEqual(len(reports), 1)
  self.assertEqual(reports[0]["route_id"], route_id2)
  self.assertEqual(reports[0]["spent_seen"], 0)

 def test_b47_5_stage_two_or_join_absent(self):
  source = (Path(F_SPEC.origin)).read_text(encoding="utf-8")
  self.assertNotIn("LAUNCH_TUPLE.spent_tuples", source[:source.index("def registry_failures")])
  registry_failures_source = source[
   source.index("def registry_failures"):source.index("def registry_rows")
  ]
  self.assertNotIn("spent_tuples", registry_failures_source)
  self.assertNotIn("LAUNCH_TUPLE", registry_failures_source)
  self.assertNotIn("launch-tuple/", registry_failures_source)
  # `failed_tuples` gains new members through exactly the same call sites
  # as before this cycle -- three `.add(` calls and the initial
  # `set(...) | set(...)` construction, never through `observation.spent`.
  self.assertEqual(source.count("failed_tuples.add("), 3)
  self.assertNotIn("failed_tuples |=", source)
  self.assertNotIn("failed_tuples.update(", source)

 def test_b47_6_unknown_failure_class_suppresses_nothing(self):
  # An arbitrary/unknown `rejection_class` string persisted directly to the
  # ledger (bypassing the writer's own closed-enum refusal) must still
  # suppress zero candidates -- stage 1 never reads the ledger for
  # selection at all, regardless of its content.
  path, route_id = self._route_and_ledger()
  state_root = self.base.jobs.parent
  root = state_root / "launch-tuple"
  root.mkdir(parents=True)
  garbage = {"schema_version": 1, "route_id": route_id, "route_node": "plan",
             "route_hash": json.loads(path.read_text())["route_hash"],
             "owner_attempt_id": "att-fallback-parent",
             "tuple_key": "codex/headless/workspace-write/codex/conductor",
             "rejection_class": "totally-unknown-value",
             "evidence_ref": "garbage", "observed_at": time.time(),
             "event_id": "lt-garbage"}
  (root / f"{route_id}.jsonl").write_text(json.dumps(garbage) + "\n", encoding="utf-8")
  with self.base.dispatch_env(**self.WRONG_PARENT):
   buf = io.StringIO()
   with contextlib.redirect_stdout(buf):
    code, observation = self.base.run_inline(path)
  self.assertEqual(code, 79)
  self.assertIn("codex/headless/workspace-write/codex/conductor", observation.spent)
  self.assertIn("skipped-nested-network-unconfirmed", buf.getvalue())
  self.assertNotIn("skipped-totally-unknown-value", buf.getvalue())

 def test_b47_7_axis_absent_corpus_selection_unchanged(self):
  # gap1 correction 2 (owner_arbitration.md 🟡-1 / plan.md §4 (2)-C): B47-7's
  # predicate is "a legacy registry corpus with the `launch_tuple_verdict`
  # axis absent entirely selects identically once that axis exists." That
  # corpus is `registry_failures()`'s own axis -- pre-revision
  # `failure_class=launch-tuple` rows, same shape as B47-2's fixture -- never
  # B47-5's spent-tuple ledger. This builds and asserts on that corpus on its
  # own instead of re-invoking test_b47_5_report_only_stdout_byte_identical,
  # which carries no registry-failure row and so never exercises this axis.
  path, route_id = self._route_and_ledger()
  same_key = "codex/headless/workspace-write/codex/conductor"
  pipe = (f"capability=autopilot-code,route_id={route_id},route_node=plan,"
          "parent=owner,attempt_id=att-prior-legacy0,parent_harness=codex,"
          "parent_transport=headless,parent_sandbox=workspace-write,"
          "child_harness=codex,launch_authority=conductor,note=dead-launch-error,"
          "failure_class=launch-tuple")
  with self.base.jobs.open("a", encoding="utf-8") as fh:
   fh.write(f"2026-07-16T00:00:00Z\tdone\t/repo\t{self.base.repo}\tfallback-plan\t{pipe}\n")
  legacy_failures = F.registry_failures(self.base.jobs, route_id, "plan")
  self.assertIn(same_key, legacy_failures)

  with self.base.dispatch_env(**self.WRONG_PARENT):
   self.assertEqual(self.base.ledger_rows(route_id), [],
                     "launch_tuple_verdict axis must be absent for the first run")
   buf1 = io.StringIO()
   with contextlib.redirect_stdout(buf1):
    code1, observation1 = self.base.run_inline(path)
   buf2 = io.StringIO()
   with contextlib.redirect_stdout(buf2):
    code2, observation2 = self.base.run_inline(path)
  self.assertNotEqual(self.base.ledger_rows(route_id), [],
                       "the second run must have populated the launch_tuple_verdict axis")
  self.assertEqual(code1, code2)
  self.assertEqual(buf1.getvalue(), buf2.getvalue(),
                    "the legacy registry corpus's selection must stay unchanged "
                    "once the launch_tuple_verdict axis exists alongside it")
  for output in (buf1.getvalue(), buf2.getvalue()):
   self.assertIn("skipped-prior-unchanged-failure", output)
  for observation in (observation1, observation2):
   self.assertIn(same_key, observation.failed_tuples)

 def test_b47_10_consumer_only_skip_writes_no_evidence(self):
  path, route_id = self._route_and_ledger()
  cross_key = "codex/headless/workspace-write/claude/conductor"
  with self.base.dispatch_env():
   code, observation = self.base.run_inline(path, "--failed-tuple", cross_key)
  self.assertEqual(code, 79)
  rows = self.base.ledger_rows(route_id)
  self.assertEqual({r["tuple_key"] for r in rows},
                    {"codex/headless/workspace-write/codex/conductor"})
  self.assertEqual({r["rejection_class"] for r in rows}, {"candidate-unsupported"})


if __name__=="__main__": unittest.main()
