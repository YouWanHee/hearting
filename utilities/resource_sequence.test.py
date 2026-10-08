#!/usr/bin/env python3
"""Sequential supervised resources retain one stage and its output requirement."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('sequence_fixture', HERE / 'workflow_supervisor.test.py')
FIX = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = FIX
spec.loader.exec_module(FIX)
SUP, WS = FIX.SUP, FIX.WS
import dispatch_resource_wait as WAIT


class ResourceSequenceTest(FIX.WorkflowFixture):
    _resume_fixture = FIX.TestSupervisorAdvance._resume_fixture
    _settle_resource_owner = FIX.TestSupervisorAdvance._settle_resource_owner

    def fixture(self):
        route, path, jobs, registry, output = self._resume_fixture(ordinary=True)
        row = json.loads(registry.read_text())['runs']['fixture-run']
        row['parent_attempt_id'] = 'att-parent'
        registry.write_text(json.dumps({'schema_version': 1, 'runs': {row['run_id']: row}}))
        ledger = SUP.ledger_for(route, jobs)
        arm = ledger.root / 'armed/full-run.json'
        armed = json.loads(arm.read_text())
        armed['resource_binding'] = WAIT.resource_body_digest(row)
        armed['successor_log'] = str(ledger.root / 'resource/verification-start.log')
        arm.write_text(json.dumps(armed))
        (output / 'run.json').unlink()
        return route, path, jobs, registry, output, ledger

    def next_body(self, registry, name, output, *, final=False):
        old = json.loads(registry.read_text())['runs']['fixture-run']
        keep = ('cwd', 'route', 'node', 'jobs', 'parent_attempt_id', 'owner_wait', 'config_ref',
                'config_sha256', 'source_commit', 'source_dirty', 'source_git_state', 'config_layout')
        log = self.base / (name + '.log')
        code = ('from pathlib import Path; Path(' + repr(str(output / 'run.json')) + ').write_text("final")'
                if final else 'pass')
        return {**{key: old[key] for key in keep if key in old}, 'run_id': name,
                'command': [sys.executable, '-c', code], 'log': str(log), 'sentinel': str(log) + '.exit',
                'status': 'launching', 'workflow_state': 'READY', 'resource_policy': 'supervised-owner'}

    def arm(self, path, registry, *, node='full-run', run_id='fixture-run', extra=()):
        argv = ['arm', '--route', str(path), '--node', node, '--predecessor-kind', 'resource',
                '--predecessor-id', run_id, '--resource-registry', str(registry),
                '--successor-external', '--successor-log',
                str(SUP.ledger_for(SUP.load_route(path), self.base / 'jobs.log').root / 'resource/verification-start.log'), *extra]
        # The initial inherited fixture uses its own original arm helper.
        if run_id == 'fixture-run':
            return FIX.WorkflowFixture.arm(self, path, registry, node=node, extra=extra)
        with contextlib.redirect_stdout(io.StringIO()):
            return SUP.main(argv)

    def launch(self, route, path, jobs, registry, output, body):
        import artifact_producer
        runner = SUP.runner()
        args = SimpleNamespace(jobs=str(jobs), run_id=body['run_id'], node=body['node'])
        payloads = []
        real_popen = subprocess.Popen
        def popen(*a, **kw):
            proc = real_popen(*a, **kw)
            payloads.append(proc)
            return proc
        def arm(argv, **kwargs):
            return subprocess.CompletedProcess(argv, SUP.main(argv[2:]))
        watch = mock.Mock()
        watch.poll.return_value = None
        with mock.patch.object(WAIT, 'supervisor', return_value=SUP), \
                mock.patch.object(artifact_producer, 'prepare_route_artifact_env',
                    return_value={'AGENT_ARTIFACT_OUTPUT_DIR': str(output)}), \
                mock.patch.object(runner, 'register_registry'), \
                mock.patch.object(runner, 'start_watch', return_value=(watch, SUP.RR.proc_identity(os.getpid()))), \
                mock.patch.object(runner.subprocess, 'run', side_effect=arm), \
                mock.patch.object(runner.subprocess, 'Popen', side_effect=popen), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            runner.start_verified(registry, args, route, path, dict(body))
        for proc in payloads:
            self.assertEqual(proc.wait(timeout=5), 0)
        return payloads, json.loads(out.getvalue().splitlines()[-1])

    def test_intermediate_success_waits_without_marker_claim_or_completion(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        result = SUP.poll_once(route, ledger)[0]
        self.assertEqual(result['action'], 'wait-next-resource')
        self.assertTrue(result['evidence']['succeeded'])
        self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')
        self.assertEqual(ledger.claims(), {})
        self.assertFalse((jobs.parent / 'completion' / route['route_id'] / 'full-run.json').exists())
        journal = ledger.journal_path.read_bytes()
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        self.assertEqual(ledger.journal_path.read_bytes(), journal)
        self.assertEqual(self._settle_resource_owner(route, path, jobs)[0].result, 'recoverable')
        self.assertEqual(ledger.journal_path.read_bytes(), journal)

    def test_three_sequential_runs_and_old_replay_preserve_history_then_advance_once(self):
        import dispatch_owner_input as INPUT
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        old = json.loads(registry.read_text())['runs']['fixture-run']
        old_sentinel = Path(old['sentinel']).read_bytes()
        first = self.next_body(registry, 'middle', output)
        procs, receipt = self.launch(route, path, jobs, registry, output, first)
        self.assertEqual(len(procs), 1)
        self.assertTrue(receipt['payload_spawned'])
        self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'wait-next-resource')
        first_armed = SUP.read_armed(ledger)['full-run']
        procs, receipt = self.launch(route, path, jobs, registry, output, first)
        self.assertEqual(procs, [])
        self.assertTrue(receipt['replayed'])
        self.assertEqual(SUP.read_armed(ledger)['full-run'], first_armed)
        final = self.next_body(registry, 'final', output, final=True)
        procs, receipt = self.launch(route, path, jobs, registry, output, final)
        self.assertEqual(len(procs), 1)
        self.assertEqual(json.loads(registry.read_text())['runs']['fixture-run'], old)
        self.assertEqual(Path(old['sentinel']).read_bytes(), old_sentinel)
        @contextlib.contextmanager
        def locked(*a):
            yield None, {'target': 'same', 'thread_id': 'same-native', 'requests': []}
        with mock.patch.object(INPUT, '_locked', locked), \
                mock.patch.object(INPUT, '_target', return_value=(None, 'same')), \
                mock.patch.object(SUP, '_start_successor', return_value={'started': False, 'surface': 'external'}) as start:
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'advanced')
            self.assertEqual(SUP.poll_once(route, ledger)[0]['action'], 'settled')
            self.assertEqual(start.call_count, 1)
        self.assertEqual(len(ledger.claims()), 1)
        self.assertEqual((output / 'run.json').read_text(), 'final')
        self.assertNotEqual(ledger.state()['workflow_state'], 'COMPLETE')
        final_armed = SUP.read_armed(ledger)['full-run']
        procs, receipt = self.launch(route, path, jobs, registry, output, first)
        self.assertEqual(procs, [])
        self.assertTrue(receipt['replayed'])
        self.assertEqual(SUP.read_armed(ledger)['full-run'], final_armed)

    def test_new_registry_can_follow_but_live_or_unverifiable_prior_cannot(self):
        for invalid in (None, 'live', 'reused', 'sentinel', 'failed'):
            with self.subTest(invalid=invalid), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                SUP.poll_once(route, ledger)
                body = self.next_body(registry, 'next', output)
                old = json.loads(registry.read_text())['runs']['fixture-run']
                if invalid == 'live':
                    old.update(SUP.RR.proc_identity(os.getpid()), status='running')
                elif invalid == 'reused':
                    old.update(SUP.RR.proc_identity(os.getpid()), starttime='wrong')
                elif invalid == 'sentinel':
                    Path(old['sentinel']).write_text('7')
                elif invalid == 'failed':
                    old.update(status='failed', exit_code=7)
                if invalid:
                    registry.write_text(json.dumps({'schema_version': 1, 'runs': {old['run_id']: old}}))
                target = self.base / 'other-registry.json'
                target.write_text(json.dumps({'schema_version': 1, 'runs': {body['run_id']: body}}))
                prior_arm = SUP.read_armed(ledger)['full-run']
                if invalid:
                    with self.assertRaisesRegex(ValueError, 'resource-watch-binding-conflict'):
                        self.arm(path, target, run_id=body['run_id'], extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                    self.assertEqual(SUP.read_armed(ledger)['full-run'], prior_arm)
                else:
                    self.arm(path, target, run_id=body['run_id'], extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                    self.assertEqual(SUP.read_armed(ledger)['full-run']['resource_registry'], str(target))

    @contextlib.contextmanager
    def subfixture(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=self.base) as directory, mock.patch.object(self, 'base', Path(directory)):
            yield

    def test_existing_missing_output_failure_can_follow_but_other_failure_cannot(self):
        for other in (False, True):
            with self.subTest(other=other), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                evidence = SUP.poll_once(route, ledger)[0]['evidence']
                evidence.pop('awaiting_next_resource', None)
                ledger.record('full-run', 'FAILED_RETRYABLE', evidence=evidence, actor='old-watch')
                ledger.set_workflow_state('FAILED_RETRYABLE', evidence={'node': 'full-run'}, actor='old-watch')
                if other:
                    ledger.record('run-verify', 'FAILED_TERMINAL', evidence={'reason': 'real-failure'})
                body = self.next_body(registry, 'next', output)
                if other:
                    with self.assertRaisesRegex(ValueError, 'resource-watch-binding-conflict'):
                        self.launch(route, path, jobs, registry, output, body)
                else:
                    self.launch(route, path, jobs, registry, output, body)
                    self.assertEqual(ledger.state()['workflow_state'], 'RUNNING')
                    self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'RUNNING')

    def test_parent_close_blocks_registration_without_changing_prior_binding(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        body = self.next_body(registry, 'next', output)
        ledger.record('full-run', 'RUNNING', evidence={'parent_close': {'preserve_resource': True}})
        before = registry.read_bytes(), SUP.read_armed(ledger)
        with self.assertRaisesRegex(ValueError, 'resource-parent-close-requested'):
            self.launch(route, path, jobs, registry, output, body)
        self.assertEqual((registry.read_bytes(), SUP.read_armed(ledger)), before)

    def test_same_registry_live_reused_failed_owner_and_reused_log_refuse_before_reservation(self):
        for invalid in ('live', 'reused', 'failed', 'owner', 'log'):
            with self.subTest(invalid=invalid), self.subfixture():
                route, path, jobs, registry, output, ledger = self.fixture()
                SUP.poll_once(route, ledger)
                body = self.next_body(registry, 'next', output)
                old = json.loads(registry.read_text())['runs']['fixture-run']
                if invalid in ('live', 'reused'):
                    old.update(SUP.RR.proc_identity(os.getpid()), status='running')
                    if invalid == 'reused':
                        old['starttime'] = 'wrong'
                elif invalid == 'failed':
                    old['status'] = 'failed'
                elif invalid == 'owner':
                    body['owner_wait'] = {'parent_attempt_id': 'foreign', 'session_id': 'foreign'}
                else:
                    body.update(log=old['log'], sentinel=old['sentinel'])
                registry.write_text(json.dumps({'schema_version': 1, 'runs': {old['run_id']: old}}))
                before = registry.read_bytes(), SUP.read_armed(ledger)
                with self.assertRaisesRegex(ValueError, 'resource-route-body-conflict'):
                    self.launch(route, path, jobs, registry, output, body)
                self.assertEqual((registry.read_bytes(), SUP.read_armed(ledger)), before)

    def test_concurrent_different_registries_admit_only_one_next_predecessor(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        def arm_next(name):
            body = self.next_body(registry, name, output)
            target = self.base / (name + '-registry.json')
            target.write_text(json.dumps({'schema_version': 1, 'runs': {name: body}}))
            try:
                self.arm(path, target, run_id=name, extra=('--jobs', str(jobs), '--artifact-base', str(output)))
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(arm_next, ['next-a', 'next-b']))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(ledger.claims(), {})

    def test_intermediate_controller_receipt_resumes_same_owner_without_successors(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        armed = SUP.read_armed(ledger)['full-run']
        row = json.loads(registry.read_text())['runs']['fixture-run']
        args = SimpleNamespace(parent_attempt_id='att-parent', route_id=route['route_id'], route_hash=route['route_hash'], jobs=str(jobs))
        control = SimpleNamespace(thread_id='same-native', pending=lambda: False)
        state = self.base / 'state.json'
        WAIT.JOIN.write_supervisor_state(state, 'att-parent', set(), phase='running-turn')
        context = (SUP, route, ledger, [(armed, row)])
        with mock.patch.object(WAIT, 'context', return_value=context), mock.patch.object(WAIT, 'supervisor', return_value=SUP):
            prompt = WAIT.wait(args, state, control, set(), lambda _: None,
                               sleep=lambda _: self.fail('intermediate result must return without another wait'))
        receipt = WAIT.JOIN.read_supervisor_phase_state(state, 'att-parent').resource['outbox']['receipt']
        self.assertEqual(receipt['state'], 'succeeded')
        self.assertEqual(receipt['reason'], 'awaiting-next-resource')
        self.assertFalse(receipt['verification_pass'])
        self.assertEqual(receipt['successors'], [])
        self.assertIn('same-native', prompt)

    def test_late_final_output_resolves_running_wait_without_claiming_successor(self):
        route, path, jobs, registry, output, ledger = self.fixture()
        SUP.poll_once(route, ledger)
        original = registry.read_bytes()
        (output / 'run.json').write_text('final')
        with ledger.lock():
            SUP.reconcile_resource_artifacts(route, ledger, 'att-parent', jobs)
        self.assertEqual(ledger.state()['nodes']['full-run']['state'], 'STAGE_SUCCEEDED')
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(registry.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
