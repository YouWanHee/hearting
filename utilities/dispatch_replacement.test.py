#!/usr/bin/env python3
"""Proof, race and crash falsifiers for automatic replacement (no model calls)."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import dispatch_replacement as R
import dispatch_contract as D
import route_identity


class ReplacementTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.jobs=self.root/'jobs.log';self.jobs.touch()
        self.route={'route_id':'rt-test','route_hash':'sha256:test','artifact_root':str(self.root),
                    'cwd':str(self.root),'capability':'autopilot-code','nodes':[]}
        self.path=self.root/'route.json';self.path.write_text(json.dumps(self.route))
        self.route_mock=mock.patch.object(R,'_route',return_value=(self.path,self.route));self.route_mock.start();self.addCleanup(self.route_mock.stop)
        self.logical=mock.patch.object(R,'_logical_key',side_effect=lambda r,m:{'root_route_id':'rt-root','node':'__owner__' if m.get('worker_type')=='owner' else m['route_node']});self.logical.start();self.addCleanup(self.logical.stop)
        self.reuse=mock.patch.object(R,'_reuse_snapshot',return_value={'completed':[],'cycle_id':'cyc-test','producer_id':'prod-test','gate_releases':[]});self.reuse.start();self.addCleanup(self.reuse.stop)
        self.quiet=mock.patch.object(D,'attempt_process_quiescence',return_value=SimpleNamespace(state='quiescent',reason='process-absent'));self.quiet.start();self.addCleanup(self.quiet.stop)
        self.absent=mock.patch.object(R,'_terminal_absent',return_value=True);self.absent.start();self.addCleanup(self.absent.stop)
        self.meta={'attempt_schema_version':'2','dispatch_depth':'1','transport':'headless',
             'execution_surface':'registered-headless','registered_worker':'1','fallback_hop':'same-harness-headless',
             'attempt_id':'att-source','route_id':'rt-test','route_hash':'sha256:test','route_node':'frame',
             'worker_type':'frame','parent_sid':'parent','harness':'codex','note':'dead-exact-pid',
             'failure_class':'contract','launch_outcome':'never-launched'}
        self.args=args=SimpleNamespace(attempt_id='att-source',jobs_path=self.jobs,worktree=str(self.root),route_id='rt-test',route_node='frame',replacement_input_argv=['--start','--attempt-id','att-source','--prompt-text','the raw task'])
        fragment=R.seal_launch_input(args,'codex','the raw task')
        self.meta.update(D.parse_registry_metadata(fragment));self.write(self.meta)

    def write(self,meta,status='done',append=False):
        line='now\t'+status+'\t'+str(self.root)+'\t'+str(self.root)+'\ttask\t'+','.join(k+'='+v for k,v in meta.items())+'\n'
        with self.jobs.open('a' if append else 'w') as f:f.write(line)

    def claim(self):return R.claim(self.jobs,'att-source')

    def test_concurrent_consumers_share_one_claim_and_original_failure(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            records=list(pool.map(lambda _:self.claim(),range(16)))
        self.assertEqual(len({r['replacement_attempt_id'] for r in records}),1)
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),1)
        meta=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        self.assertEqual((meta['note'],meta['failure_class']),('dead-exact-pid','contract'))
        self.assertEqual(len(self.jobs.read_text().splitlines()),1)

    def test_crash_after_record_before_annotation_reuses_claim(self):
        with mock.patch.object(R,'_bind_source',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):self.claim()
        self.assertNotIn('replacement_family_id',self.jobs.read_text())
        saved=next((R._directory(self.jobs)/'claims').glob('*.json')).read_text()
        result=self.claim()
        self.assertEqual(json.loads(saved),result)
        self.assertIn('replacement_family_id',self.jobs.read_text())

    def test_live_or_unknown_never_claims(self):
        for state in ['live','unverifiable']:
            with self.subTest(state=state),mock.patch.object(D,'attempt_process_quiescence',return_value=SimpleNamespace(state=state,reason='proof')):
                with self.assertRaisesRegex(D.DispatchContractError,'proof'):self.claim()
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_semantic_result_and_invalid_handoff_never_replace(self):
        with mock.patch.object(R,'_terminal_absent',return_value=False):
            with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'replacement-result-settlement-required')
        for note in ['dead-worker-fail','completed-review-blocking','dead-invalid-envelope','cancelled-by-user']:
            self.write({**self.meta,'note':note})
            with self.subTest(note=note),self.assertRaises(D.DispatchContractError):self.claim()
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_owner_open_child_vetoes_even_if_process_seems_quiet(self):
        self.write({**self.meta,'worker_type':'owner'})
        self.write({**self.meta,'attempt_id':'att-child','parent_attempt_id':'att-source'},'open',append=True)
        with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'replacement-owner-child-unsettled')

    def test_terminal_fence_vetoes_claim(self):
        with mock.patch.object(D,'ensure_terminal_claim_absent',side_effect=D.DispatchContractError('terminal-claim-pending')):
            with self.assertRaises(D.DispatchContractError):self.claim()
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_missing_or_changed_input_never_claims(self):
        path=R._directory(self.jobs)/'inputs/att-source.json';path.write_text('{}')
        with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'replacement-input-unproven')
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_replacement_failure_cannot_claim_a_second_replacement(self):
        record=self.claim();self.write({**self.meta,'attempt_id':record['replacement_attempt_id'],
             'automatic_retry_of':'att-source','replacement_family_id':record['family_id']},append=True)
        with self.assertRaises(D.DispatchContractError) as caught:R.claim(self.jobs,record['replacement_attempt_id'])
        self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),1)

    def test_registration_admission_requires_exact_claim_and_fresh_death(self):
        record=self.claim();candidate={**self.meta,'attempt_id':record['replacement_attempt_id'],'automatic_retry_of':'att-source'}
        source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        replay=R.launch_input(self.jobs,'att-source',source)
        args=SimpleNamespace(**vars(self.args));args.attempt_id=record['replacement_attempt_id']
        args.replacement_input_argv=R._replacement_argv(record,source,replay)
        candidate.update(D.parse_registry_metadata(R.seal_launch_input(args,'codex','the raw task')))
        lines=self.jobs.read_text().splitlines()
        self.assertEqual(R.admission(self.jobs,lines,candidate),record)
        for change in [{'attempt_id':'att-invented'},{'parent_sid':'other'},{'route_hash':'sha256:other'}]:
            with self.subTest(change=change),self.assertRaises(D.DispatchContractError):R.admission(self.jobs,lines,{**candidate,**change})
        with mock.patch.object(D,'attempt_process_quiescence',return_value=SimpleNamespace(state='live',reason='live-now')):
            with self.assertRaises(D.DispatchContractError):R.admission(self.jobs,lines,candidate)

    def test_legacy_retry_consumes_same_budget(self):
        self.write({**self.meta,'automatic_retry_of':'att-earlier'})
        with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')

    def test_register_then_start_has_same_sealed_input(self):
        args=SimpleNamespace(**vars(self.args))
        args.replacement_input_argv=['--register','--attempt-id','att-source',
                                     '--prompt-file','irrelevant-input-path']
        self.assertEqual(R.seal_launch_input(args,'codex','the raw task'),
                         ',replacement_input_digest='+self.meta['replacement_input_digest'])
        args.replacement_input_argv=['--start','--worktree',str(self.root),
                                     '--jobs',str(self.jobs),'--prompt-text','the raw task']
        self.assertEqual(R.seal_launch_input(args,'codex','the raw task'),
                         ',replacement_input_digest='+self.meta['replacement_input_digest'])
        with self.assertRaises(D.DispatchContractError):R.seal_launch_input(args,'codex','changed')

    def test_claim_publication_crash_blocks_legacy_retry(self):
        for fail_before_record in [True,False]:
            with self.subTest(before_record=fail_before_record):
                if fail_before_record:
                    real_once=R._once
                    def crash(path,value):
                        if path.parent.name=='claims':raise RuntimeError('crash')
                        return real_once(path,value)
                    patch=mock.patch.object(R,'_once',side_effect=crash)
                else:patch=mock.patch.object(R,'_bind_source',side_effect=RuntimeError('crash'))
                with patch,self.assertRaises(RuntimeError):self.claim()
                lines=self.jobs.read_text().splitlines()
                for key in ['automatic_retry_of','prior_attempt_id']:
                    with self.assertRaises(D.DispatchContractError) as caught:
                        R.admission(self.jobs,lines,{**self.meta,'attempt_id':'att-other',key:'att-source'})
                    self.assertEqual(caught.exception.reason,'replacement-claim-pending')
        self.assertEqual(self.claim()['replacement_attempt_id'],self.claim()['replacement_attempt_id'])

    def test_candidate_task_and_resolved_permissions_are_bound(self):
        record=self.claim();source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        replay=R.launch_input(self.jobs,'att-source',source)
        candidate={**self.meta,'attempt_id':record['replacement_attempt_id'],'automatic_retry_of':'att-source'}
        args=SimpleNamespace(**vars(self.args));args.attempt_id=candidate['attempt_id']
        args.replacement_input_argv=R._replacement_argv(record,source,replay)
        candidate.update(D.parse_registry_metadata(R.seal_launch_input(args,'codex','other task')))
        with self.assertRaises(D.DispatchContractError) as caught:
            R.admission(self.jobs,self.jobs.read_text().splitlines(),candidate)
        self.assertEqual(caught.exception.reason,'replacement-task-mismatch')

    def test_changed_reused_evidence_refuses_actual_spawn(self):
        record=self.claim()
        with mock.patch.object(R,'_reuse_snapshot',return_value={'changed':'gate'}):
            with self.assertRaises(D.DispatchContractError) as caught:
                R.validate_claim_source(self.jobs,self.jobs.read_text().splitlines(),record)
        self.assertEqual(caught.exception.reason,'replacement-reuse-evidence-drift')

    def test_quick_owner_uses_explicit_route_without_owner_environment(self):
        self.write({**self.meta,'worker_type':'owner','route_file':str(self.path)})
        record=self.claim();source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        cmd=R._command(self.jobs,record,source,R.launch_input(self.jobs,'att-source',source))
        self.assertEqual(cmd[cmd.index('--route-file')+1],str(self.path))
        with mock.patch.object(R,'_authorized'),mock.patch('dispatch_replacement_batch.command',return_value=None):
            result=R.advance(self.jobs,'att-source',run=lambda command,**kw: (
                self.assertFalse(any(k.startswith('AGENT_OWNER_ROUTE_') for k in kw['env']))
                or SimpleNamespace(returncode=0)))
        self.assertEqual(result['reason'],'replacement-launch-pending')

    def test_exhausted_sd106_budget_cannot_start_new_family(self):
        for addition in [{'recovery_exhausted':'1'}, {'start_permitted':'0'},
                         {'recovery_id':'rid-dead'}]:
            self.write({**self.meta,**addition})
            if addition.get('recovery_id'):
                D._write_recovery_attention(self.jobs,'att-source','rid-dead','exhausted')
            with self.subTest(addition=addition),self.assertRaises(D.DispatchContractError) as caught:self.claim()
            self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')
        self.assertFalse((R._directory(self.jobs)/'claims').exists())

    def test_sd106_attention_before_row_publication_already_consumes_budget(self):
        D._write_recovery_attention(self.jobs,'att-source','rid-dead','exhausted')
        self.assertNotIn('recovery_id',self.jobs.read_text())
        with self.assertRaises(D.DispatchContractError) as caught:self.claim()
        self.assertEqual(caught.exception.reason,'automatic-replacement-exhausted')

    def test_success_addition_and_same_gate_release_are_monotonic(self):
        old={'cycle_id':'cyc','producer_id':'prod','completed':[{'node':'done','marker_digest':'a'}],
             'gates':[{'gate':'frame-review','status':'blocked','epoch':1,'raised_at':'t','artifact':'brief'}]}
        new={**old,'completed':old['completed']+[{'node':'sibling','marker_digest':'b'}],
             'gates':[{**old['gates'][0],'status':'proceed','answers':{'direction':'yes'}}]}
        self.assertTrue(R._reuse_preserved(old,new))
        self.assertFalse(R._reuse_preserved(old,{**new,'completed':[]}))
        self.assertFalse(R._reuse_preserved(old,{**new,'gates':[{**new['gates'][0],'epoch':2}]}))
        self.assertFalse(R._reuse_preserved(new,{**new,'gates':[{**new['gates'][0],'answers':{'direction':'no'}}]}))

    def test_gate_authority_comes_from_journal_not_sidecar(self):
        import workflow_state as WS
        route={**self.route,'human_gate_bindings':[{'gate':'frame-review'}]}
        ledger=WS.WorkflowLedger(route['route_id'],route['route_hash'],jobs=self.jobs)
        ledger.journal_path.parent.mkdir(parents=True)
        def entry(state,evidence):return {'route_id':route['route_id'],'route_hash':route['route_hash'],
                                          'workflow_state':state,'evidence':evidence}
        raised=entry('BLOCKED_HUMAN_GATE',{'gate':'frame-review'})
        ledger.journal_path.write_text(json.dumps(raised)+'\n')
        self.assertEqual(R._gate_snapshot(self.jobs,route)[0]['status'],'blocked')
        released=entry('RUNNING',{'released_gate':'frame-review','decision':'proceed','answers':{'direction':'yes'}})
        with ledger.journal_path.open('a') as f:f.write(json.dumps(released)+'\n')
        self.assertEqual(R._gate_snapshot(self.jobs,route)[0]['status'],'proceed')
        for decision in ['revise','stop']:
            event=entry('CANCELLED',{'gate':'frame-review','abandon_reason':'operator-decision'}) if decision=='stop' else entry('RUNNING',{'released_gate':'frame-review','decision':'revise'})
            ledger.journal_path.write_text(json.dumps(raised)+'\n'+json.dumps(event)+'\n')
            with self.subTest(decision=decision),self.assertRaises(D.DispatchContractError):R._gate_snapshot(self.jobs,route)
        ledger.journal_path.write_text('{torn')
        with self.assertRaises(D.DispatchContractError):R._gate_snapshot(self.jobs,route)

    def test_registered_only_successor_resumes_original_transaction(self):
        record=self.claim()
        self.write({**self.meta,'attempt_id':record['replacement_attempt_id'],
                    'replacement_original_attempt_id':'att-source','note':'registered','launch_claimed':'0'},
                   'open',append=True)
        calls=[]
        def crashed_launcher(command,**kwargs):
            calls.append(command);return SimpleNamespace(returncode=75)
        with mock.patch.object(R,'_authorized'),mock.patch('dispatch_replacement_batch.command',return_value=None):
            result=R.advance(self.jobs,record['replacement_attempt_id'],run=crashed_launcher)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][calls[0].index('--attempt-id')+1],record['replacement_attempt_id'])
        self.assertEqual(result['reason'],'replacement-launch-pending')
        self.assertEqual(len(list((R._directory(self.jobs)/'claims').glob('*.json'))),1)

    def test_concurrent_actual_spawn_releases_one_fenced_process(self):
        record=self.claim();aid=record['replacement_attempt_id']
        source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        replay=R.launch_input(self.jobs,'att-source',source)
        args=SimpleNamespace(**vars(self.args));args.attempt_id=aid
        args.replacement_input_argv=R._replacement_argv(record,source,replay)
        candidate={**self.meta,'attempt_id':aid,'automatic_retry_of':'att-source'}
        for key in ('note','failure_class','launch_outcome'):candidate.pop(key,None)
        candidate.update(D.parse_registry_metadata(R.seal_launch_input(args,'codex','the raw task')))
        row='now\topen\t'+str(self.root)+'\t'+str(self.root)+'\ttask\t'+','.join(k+'='+v for k,v in candidate.items())
        self.assertTrue(D.claim_attempt_row(self.jobs,aid,row,launch=False))
        counter=self.root/'spawned';children=[]
        def spawn(fd):
            child=subprocess.Popen([sys.executable,str(Path(__file__).with_name('launch-fence.py')),
                    '--parent-pid',str(os.getpid()),'--gate-fd',str(fd),'--',sys.executable,'-c',
                    'from pathlib import Path; Path('+repr(str(counter))+').open("a").write("started\\n")'],
                    pass_fds=(fd,),start_new_session=True)
            children.append(child);return child
        def attempt(_):
            try:return D.spawn_claimed_attempt(self.jobs,aid,parent_binding=None,spawn=spawn,
                        launch_metadata={'launch_lifecycle':'detached'})
            except D.DispatchContractError:return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(attempt,range(12)))
        for child in children:child.wait(timeout=10)
        self.assertEqual(len(children),1)
        self.assertEqual(counter.read_text().splitlines(),['started'])
        self.assertEqual(R._rows(self.jobs.read_text().splitlines())['att-source'][1]['note'],'dead-exact-pid')

    def test_quick_and_advanced_owner_route_lookup(self):
        current=self.root/'current.json';current.write_text(json.dumps(self.route))
        self.route_mock.stop()
        with mock.patch.object(D,'_route_module',return_value=SimpleNamespace(verify_route=lambda route:None)):
            with mock.patch('owner_route_binding.resolve_owner_route_lifecycle',return_value=(None,None)):
                path,_=R._route(self.jobs,'att-source',{**self.meta,'worker_type':'owner','route_file':str(self.path)})
                self.assertEqual(path,self.path)
            with mock.patch('owner_route_binding.resolve_owner_route_lifecycle',return_value=(SimpleNamespace(route_file=str(current)),None)):
                path,_=R._route(self.jobs,'att-source',{**self.meta,'worker_type':'owner','owner_route_file':str(self.path)})
                self.assertEqual(path,current)

    def test_default_tuple_and_option_order_are_normalized_for_replay(self):
        args=SimpleNamespace(attempt_id='att-defaults',jobs_path=self.jobs,worktree=str(self.root),
                  route_id='rt-test',route_node='frame',parent_session_id='parent',sandbox='workspace-write',
                  replacement_input_argv=['--start','--slug','task','--route-file',str(self.path)])
        fragment=R.seal_launch_input(args,'codex','task')
        source={**self.meta,'attempt_id':args.attempt_id,'route_file':str(self.path),**D.parse_registry_metadata(fragment)}
        replay=R.launch_input(self.jobs,args.attempt_id,source)
        record={'route_file':str(self.path),'route_id':'rt-test','route_hash':'sha256:test',
                'replacement_attempt_id':'att-defaults-new','original_attempt_id':args.attempt_id}
        argv=R._replacement_argv(record,source,replay)
        args.attempt_id=record['replacement_attempt_id'];args.replacement_input_argv=argv
        fragment=R.seal_launch_input(args,'codex','task')
        candidate=R.launch_input(self.jobs,args.attempt_id,D.parse_registry_metadata(fragment))
        self.assertEqual(candidate['argv'],argv)
        self.assertEqual(candidate['resolved'],replay['resolved'])

    def test_config_permission_escalation_changes_sealed_authority(self):
        args=SimpleNamespace(**vars(self.args));args.attempt_id='att-permission';args.permission_mode='config'
        args.resolved_permission_posture={'mode':'allowlist','mode_flag':'acceptEdits',
                 'allowed_tools':['Read'],'inherited_default_mode':'default','reason':'config'}
        fragment=R.seal_launch_input(args,'claude','task')
        original=R.launch_input(self.jobs,args.attempt_id,D.parse_registry_metadata(fragment))
        args.resolved_permission_posture['reason']='diagnostic-only'
        self.assertEqual(R.seal_launch_input(args,'claude','task'),fragment)
        args.resolved_permission_posture.update(mode='bypass',mode_flag='bypassPermissions')
        with self.assertRaises(D.DispatchContractError):R.seal_launch_input(args,'claude','task')
        source={**self.meta,'attempt_id':'att-permission','harness':'claude',
                **D.parse_registry_metadata(fragment)}
        self.write(source)
        record=R.claim(self.jobs,'att-permission')
        args.attempt_id=record['replacement_attempt_id']
        args.replacement_input_argv=R._replacement_argv(record,source,original)
        candidate_fragment=R.seal_launch_input(args,'claude','task')
        candidate=R.launch_input(self.jobs,args.attempt_id,D.parse_registry_metadata(candidate_fragment))
        self.assertEqual(original['resolved'],candidate['resolved'])
        self.assertNotEqual(original['applied_permissions'],candidate['applied_permissions'])
        metadata={**source,'attempt_id':args.attempt_id,'automatic_retry_of':'att-permission',
                  **D.parse_registry_metadata(candidate_fragment)}
        with self.assertRaises(D.DispatchContractError) as caught:
            R.admission(self.jobs,self.jobs.read_text().splitlines(),metadata)
        self.assertEqual(caught.exception.reason,'replacement-input-tuple-mismatch')
        self.assertEqual(caught.exception.detail,'applied_permissions')

    def test_replay_uses_raw_task_and_new_identity(self):
        record=self.claim();source=R._rows(self.jobs.read_text().splitlines())['att-source'][1]
        cmd=R._command(self.jobs,record,source,R.launch_input(self.jobs,'att-source',source))
        self.assertEqual(cmd[cmd.index('--attempt-id')+1],record['replacement_attempt_id'])
        self.assertEqual(cmd[cmd.index('--automatic-retry-of')+1],'att-source')
        self.assertNotIn('--prompt-text',cmd)
        self.assertEqual(Path(cmd[cmd.index('--prompt-file')+1]).read_text(),'the raw task')
        self.assertEqual(cmd.count('--start'),1)


if __name__=='__main__':unittest.main()
