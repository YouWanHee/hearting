#!/usr/bin/env python3
"""SD154/161 merged dry-run proof reaches each actual wrapper main."""
import contextlib, importlib.util, io, json, os, sys, unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'utilities'))
spec=importlib.util.spec_from_file_location('preview_fallback_fixture',ROOT/'utilities/stage_dispatch_fallback.test.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
import review_input

class WrapperPreviewTest(unittest.TestCase):
 def setUp(self):
  self.f=fixture.FallbackTest('runTest');self.f.setUp();self.addCleanup(self.f.tearDown)
 def test_replacement_preview_keeps_same_round_and_strict_normal_gate(self):
  from types import SimpleNamespace
  args=SimpleNamespace(action='dry-run',route_file='route.json',automatic_retry_of='att-source')
  with mock.patch.object(review_input,'_dispatch_node_module',side_effect=AssertionError('new round')):
   self.assertEqual(review_input.preview_request_nodes(args,self.f.jobs),frozenset())
 def test_all_three_mains_prove_changed_plan_without_publishing(self):
  f=self.f
  with f.dispatch_env():
   path=f.route(same_status='supported',intensity='standard');route=json.loads(path.read_text())
   f.seed_parent();evidence=f.seed_plan_marker(route);f.seed_review_rounds(route['route_id'],'plan-check',1)
   evidence.write_text('Corrected input, preview only.\n')
   marker=fixture.R.completion_dir(route['route_id'],jobs=f.jobs)/'plan.json'
   before={str(p):p.read_bytes() for p in marker.parent.iterdir() if p.is_file()}
   before_jobs=f.jobs.read_bytes()
   for harness in ('codex','claude','opencode'):
    with self.subTest(harness=harness),contextlib.ExitStack() as stack:
     spec=importlib.util.spec_from_file_location('preview_main_'+harness,ROOT/f'adapters/{harness}/bin/dispatch-headless.py')
     w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
     stack.enter_context(mock.patch.object(w,'resolve_artifact_root',return_value=str(f.art)))
     # The route tuple is constructed by the fixture compiler; native runtime
     # discovery is outside this no-model test. The real main completion gate,
     # common input resolver and readonly admission are deliberately unmocked.
     stack.enter_context(mock.patch.object(w,'validate_route_record',side_effect=lambda args:setattr(args,'route_validation',{}) or 0))
     stack.enter_context(mock.patch.object(w,'headless_attempt_policy',return_value={
      'fallback_hop':'same-harness-headless','fallback_ordinal':1,'quick':False,
      'terminal_attempt_limit':None,'replacement_attempt_limit':0,'replacement_notes':frozenset()}))
     stack.enter_context(mock.patch.object(w.shutil,'which',return_value='/bin/true'))
     stack.enter_context(mock.patch.object(w,'shell_command',return_value='true'))
     for name in ('check_runtime_projection','ensure_runtime_home_projection'):
      if hasattr(w,name): stack.enter_context(mock.patch.object(w,name,return_value=0 if name=='check_runtime_projection' else None))
     spawn=stack.enter_context(mock.patch.object(w,'spawn_claimed_attempt',side_effect=AssertionError('model spawned')))
     argv=['dispatch-headless.py','--dry-run','--worktree',str(f.repo),'--jobs',str(f.jobs),
      '--slug','preview-'+harness,'--parent','owner','--parent-attempt-id','att-fallback-parent',
      '--capability','autopilot-code','--capability-mode','dev','--worker-type','review',
      '--worker-mode','qa/plan-review','--unit','qa/plan-review','--dispatch-depth','1','--intensity','standard',
      '--route-file',str(path),'--route-id',route['route_id'],'--route-hash',route['route_hash'],'--route-node','plan-check',
      '--model-profile',next(n for n in route['nodes'] if n['id']=='plan-check')['model_profile'],
      '--model-role',next(n for n in route['nodes'] if n['id']=='plan-check')['role'],
      '--parent-harness','claude','--parent-transport','headless','--parent-sandbox','default']
     with contextlib.redirect_stdout(io.StringIO()) as out:
      code=w.main(argv)
     self.assertEqual(code,0,out.getvalue());spawn.assert_not_called()
     self.assertEqual(before_jobs,f.jobs.read_bytes())
     self.assertEqual(before,{str(p):p.read_bytes() for p in marker.parent.iterdir() if p.is_file()})
   self.assertEqual(fixture.R.gate_currency(route,next(n for n in route['nodes'] if n['id']=='plan'),marker).state,'revised-unrecorded')

if __name__=='__main__':unittest.main()
