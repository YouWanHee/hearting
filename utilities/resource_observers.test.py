#!/usr/bin/env python3
"""Old-release adoption and auxiliary execution preserve the primary resource."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OBS = load('resource-observers')
FIX = load('workflow_supervisor.test')
SUP = FIX.SUP
R = OBS.load_tool('resource-runner')


class ObserverTest(FIX.WorkflowFixture):
    _resume_fixture = FIX.TestSupervisorAdvance._resume_fixture

    @contextlib.contextmanager
    def subfixture(self):
        with tempfile.TemporaryDirectory(dir=self.base) as directory, mock.patch.object(self, 'base', Path(directory)):
            yield

    def fixture(self):
        route, path, jobs, registry, output = self._resume_fixture(ordinary=True)
        row = json.loads(registry.read_text())['runs']['fixture-run']
        row.update(**SUP.RR.proc_identity(os.getpid()), status='running', exit_code=None)
        row.pop('ended_at', None)
        Path(row['sentinel']).unlink(missing_ok=True)
        registry.write_text(json.dumps({'schema_version': 1, 'runs': {'fixture-run': row}}))
        return route, path, jobs, registry, output, row

    def start_side(self, registry, route, path, jobs, command, *, run_id='score', log=None):
        args = SimpleNamespace(run_id=run_id, route=str(path), node='full-run', cwd=str(self.base),
                               log=str(log or (self.base / (run_id + '.log'))), jobs=str(jobs))
        spawn = subprocess.Popen
        def launch(*a, **kw):
            proc = spawn(*a, **kw)
            self.addCleanup(self.cleanup_process, proc)
            return proc
        with mock.patch.object(R.subprocess, 'Popen', side_effect=launch), \
             mock.patch.object(R, 'observation_runtime', return_value=OBS), \
             mock.patch.object(OBS, 'ensure_resource', return_value={'started': True}), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            R.start_observer(registry, args, route, path, command)
        return json.loads(out.getvalue())

    def cleanup_process(self, proc):
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=3)

    def reap(self, registry, row):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.waitpid(row['pid'], os.WNOHANG)
            except ChildProcessError:
                pass
            current = json.loads(registry.read_text())['runs'][row['run_id']]
            if R.read_sentinel(row['sentinel']) is not None and SUP.RR.classify_identity(current)[0] == 'exited':
                return R.settle(registry, row['run_id'], current)[0]
            time.sleep(.01)
        self.fail('observer did not exit')

    def test_success_failure_and_replay_preserve_stage_and_deliver_to_each_harness(self):
        import resource_progress as progress
        import session_notice
        for harness, code in [('claude', 0), ('codex', 7), ('opencode', 0)]:
            with self.subTest(harness=harness), self.subfixture():
                route, path, jobs, registry, output, primary = self.fixture()
                primary.update(session={'harness': harness, 'id': 'owner'},
                               notify={'harness': harness, 'id': 'supervisor'})
                registry.write_text(json.dumps({'schema_version': 1, 'runs': {'fixture-run': primary}}))
                ledger = SUP.ledger_for(route, jobs)
                armed_before = SUP.read_armed(ledger)
                journal_before = ledger.journal_path.read_bytes()
                side = self.start_side(registry, route, path, jobs, ['/bin/sh', '-c', f'exit {code}'])
                self.assertEqual(side['observer_of'], 'fixture-run')
                self.assertEqual(side['node'], primary['node'])
                self.assertNotEqual(side['sentinel'], primary['sentinel'])
                ended = self.reap(registry, side)
                self.assertEqual(ended['exit_code'], code)
                replay = self.start_side(registry, route, path, jobs, side['command'])
                self.assertTrue(replay['replayed'])
                self.assertFalse(replay['payload_spawned'])
                data = json.loads(registry.read_text())
                self.assertEqual(data['runs']['fixture-run'], primary)
                self.assertFalse(Path(primary['sentinel']).exists())
                self.assertEqual(SUP.read_armed(ledger), armed_before)
                self.assertEqual(ledger.journal_path.read_bytes(), journal_before)
                # A live side run cannot block admission of the next primary; evidence paths stay protected.
                candidate = {'log': str(self.base / 'next.log')}
                R.route_authority.require_resource_predecessors([side], candidate)
                with self.assertRaises(ValueError):
                    R.route_authority.require_resource_predecessors([side], {'log': side['log']})
                notices = []
                def receive(h, s, **kw):
                    notices.append((h, s, kw))
                    return {'status': 'accepted'}
                with mock.patch.object(session_notice, 'notify', side_effect=receive):
                    SUP.RR.observe_run_if_due(side['run_id'], ended, registry, interval_seconds=0)
                self.assertEqual({(h, s) for h, s, _ in notices}, {(harness, 'owner'), (harness, 'supervisor')})
                self.assertIn(f'exit={code}', ' '.join(n['text'] for _, _, n in notices))
                record = progress.read_public_run_record(route['artifact_root'], side['run_id'])
                self.assertEqual(record['status'], 'succeeded' if code == 0 else 'failed')

    def test_missing_primary_and_primary_evidence_alias_are_refused(self):
        route, path, jobs, registry, output, primary = self.fixture()
        with self.assertRaisesRegex(ValueError, 'body-conflict'):
            self.start_side(registry, route, path, jobs, ['/bin/true'], log=primary['log'])
        registry.write_text(json.dumps({'runs': {}}))
        with self.assertRaisesRegex(ValueError, 'requires-live-resource'):
            self.start_side(registry, route, path, jobs, ['/bin/true'])
        self.assertFalse((self.base / 'score.log').exists())


class AdoptionTest(unittest.TestCase):
    def test_compute_adoption_does_not_inherit_installer_project_identity(self):
        import resource_progress as progress
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compute = OBS.load_tool('compute-hosts')
            meta = {'run_id': 'legacy', 'artifact_root': str(root / 'original'),
                    'provenance': {'session': {'harness': 'claude', 'id': 'original-session'}}}
            with mock.patch.dict(os.environ, {'AGENT_ARTIFACT_ROOT': str(root / 'installer'),
                       'AGENT_ARTIFACT_CAMPAIGN_ID': 'installer-campaign', 'AGENT_ARTIFACT_CYCLE_ID': 'installer-cycle'}), \
                 mock.patch.object(progress, 'observe_experiment', side_effect=lambda record, *a, **kw: record):
                record = compute._observe_compute_experiment(meta, root, 100)
            self.assertEqual(record['artifact_root'], str(root / 'original'))
            self.assertIsNone(record['campaign_id'])
            self.assertIsNone(record['cycle_id'])
            self.assertEqual(record['session']['id'], 'original-session')

    def test_reconcile_adopts_both_registries_and_preserves_source_bytes(self):
        import resource_run_registry as registry_api
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'compute' / 'old-running'
            run.mkdir(parents=True)
            meta = run / 'meta.json'
            meta.write_text(json.dumps({'run_id': run.name, 'provenance': {'session': {'harness': 'codex', 'id': 'old'}}}))
            registry = root / 'resource-runs.json'
            registry.write_text(json.dumps({'schema_version': 1, 'runs': {
                'live-resource': {'pid': os.getpid(), 'status': 'running'}}}))
            index = root / 'index.json'
            registry_api.register_registry(registry, index_path=index)
            before = meta.read_bytes(), registry.read_bytes()
            compute = OBS.load_tool('compute-hosts')
            with mock.patch.dict(os.environ, {'AGENT_RESOURCE_RUN_INDEX': str(index), 'AGENT_DISPATCH_DEPTH': '1'}), \
                 mock.patch.object(OBS, 'load_tool', return_value=compute), \
                 mock.patch.object(compute, 'load_config', return_value={'run_root': root / 'compute'}), \
                 mock.patch.object(compute, '_refuse_namespace_self'), \
                 mock.patch.object(compute, '_spawn_completion_watch', return_value=True) as attach, \
                 mock.patch.object(OBS, 'ensure_resource', return_value={'started': True}) as side:
                result = OBS.reconcile()
            self.assertEqual({row['kind'] for row in result['results']}, {'compute', 'resource'})
            self.assertEqual(attach.call_args.kwargs['environ']['AGENT_DISPATCH_DEPTH'], '0')
            side.assert_called_once_with(registry, 'live-resource')
            self.assertEqual(before, (meta.read_bytes(), registry.read_bytes()))

    def test_old_watcher_is_replaced_once_without_touching_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = []
            for release in ('old', 'new'):
                script = root / release / 'compute-hosts.py'
                script.parent.mkdir()
                script.write_text('import time\ntime.sleep(60)\n')
                scripts.append(script)
            source = root / 'run'
            source.mkdir()
            old = subprocess.Popen([sys.executable, str(scripts[0]), 'watch-run', str(source)])
            payload = subprocess.Popen(['/bin/sleep', '60'])
            children = [old, payload]
            def spawn(*a, **kw):
                proc = subprocess.Popen(*a, **kw)
                children.append(proc)
                return proc
            try:
                command = [sys.executable, str(scripts[1]), 'watch-run', str(source)]
                result = OBS.ensure_watch(command, source / '.watch.lock', spawn=spawn)
                self.assertTrue(result['started'])
                self.assertEqual(old.wait(timeout=3), -signal.SIGTERM)
                again = OBS.ensure_watch(command, source / '.watch.lock', spawn=spawn)
                self.assertTrue(again['reused'])
                self.assertEqual(result['pid'], again['pid'])
                self.assertIsNone(payload.poll())
            finally:
                for proc in children:
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=3)

    def test_hash_bound_request_discovery_and_invalid_digest(self):
        import resource_progress as progress
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'output'
            output.mkdir()
            request = root / 'request.json'
            request.write_text(json.dumps({'output': str(output)}))
            digest = hashlib.sha256(request.read_bytes()).hexdigest()
            run = {'cwd': str(root), 'command': ['python', 'train.py', '--request', str(request),
                                               '--request-sha256', digest]}
            self.assertEqual(progress.discover_output_root(run), str(output))
            request.write_text(json.dumps({'output': str(root)}))
            self.assertIsNone(progress.discover_output_root(run))


if __name__ == '__main__':
    unittest.main()
