#!/usr/bin/env python3
"""Exercise durable correction admission and real App Server pipe boundaries."""
import importlib.util
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'utilities'))
import dispatch_owner_input as I
from dispatch_contract import hold_supervisor_lease, supervisor_lease_path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


C = load('input_codex_supervisor', ROOT / 'utilities/codex-app-server-supervisor.py')
FIXTURE = load('input_supervisor_fixture', ROOT / 'utilities/codex_app_server_supervisor.test.py')


class OwnerInputTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / 'jobs.log'
        self.attempt = FIXTURE.PARENT
        self.lease = supervisor_lease_path(self.jobs, self.attempt)
        self.jobs.write_text(FIXTURE.owner_row(self.lease))
        self.held = hold_supervisor_lease(self.jobs, self.attempt, self.lease)
        self.held.__enter__()
        self.addCleanup(self.held.__exit__, None, None, None)
        self.events = []
        self.control = I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)

    def state(self):
        return I.inspect(self.jobs, self.attempt)['requests']

    def test_duplicate_and_changed_request_id_use_one_durable_record(self):
        errors = []
        def send():
            try:
                I.submit(self.jobs, self.attempt, 'reuse the existing implementation', 'same')
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=send) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.state()), 1)
        with self.assertRaisesRegex(I.InputError, 'content-conflict'):
            I.submit(self.jobs, self.attempt, 'different', 'same')
        self.assertEqual(self.state()[0]['state'], 'queued')

    def test_terminal_admission_race_and_completed_receipt_are_separate(self):
        I.submit(self.jobs, self.attempt, 'reuse')
        self.assertFalse(self.control.terminal_boundary())
        prompt = self.control.prepare('current context')
        self.assertIn('reuse', prompt)
        self.control.started('turn-1')
        self.assertEqual(self.state()[0]['state'], 'accepted')
        self.control.completed('turn-1')
        self.assertEqual(self.state()[0]['state'], 'turn-completed')
        self.assertTrue(self.control.terminal_boundary())
        with self.assertRaisesRegex(I.InputError, 'unavailable'):
            I.submit(self.jobs, self.attempt, 'late correction')
        self.assertEqual(I.inspect(self.jobs, self.attempt)['applied'], 'not-verified')

    def test_restart_recovers_unsent_input_but_never_replays_unknown_send(self):
        I.submit(self.jobs, self.attempt, 'first', 'first')
        self.control.take('active-turn')
        I.submit(self.jobs, self.attempt, 'second', 'second')
        recovered = I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
        self.assertEqual([r['state'] for r in self.state()], ['delivery-unknown', 'queued'])
        prompt = recovered.prepare('resume')
        self.assertNotIn('"text": "first"', prompt)
        self.assertIn('"text": "second"', prompt)
        with self.assertRaisesRegex(I.InputError, 'consumer-changed'):
            self.control.prepare('stale consumer')

    def test_thread_change_cannot_silently_retarget_queued_input(self):
        I.submit(self.jobs, self.attempt, 'old thread')
        I.OwnerInput(self.jobs, self.attempt, 'thread-2', 'codex-active-turn', self.events.append)
        self.assertEqual(self.state()[0]['state'], 'undelivered')
        self.assertEqual(self.state()[0]['reason'], 'owner-thread-changed')

    def test_unknown_send_and_unsent_input_survive_shutdown(self):
        I.submit(self.jobs, self.attempt, 'unknown', 'unknown')
        self.control.take('active-turn')
        I.submit(self.jobs, self.attempt, 'unsent', 'unsent')
        self.control.close()
        self.assertEqual([r['state'] for r in self.state()], ['delivery-unknown', 'undelivered'])
        self.assertTrue(I.unresolved(self.jobs, self.attempt))

    def test_old_supervisor_is_rejected_without_queueing(self):
        I._path(self.jobs, self.attempt).unlink()
        with self.assertRaisesRegex(I.InputError, 'unsupported'):
            I.submit(self.jobs, self.attempt, 'new input')
        self.assertFalse(I._path(self.jobs, self.attempt).exists())

    def test_first_native_session_receipt_binds_the_returned_identity(self):
        control=I.OwnerInput(self.jobs,self.attempt,'pending-native-session','opencode-next-turn',self.events.append)
        I.submit(self.jobs,self.attempt,'initial correction')
        control.prepare('first turn')
        control.bind_initial_thread('ses_returned')
        control.started('1')
        self.assertEqual(self.state()[0]['thread_id'],'ses_returned')

    def test_rejection_defers_to_same_owner_next_turn(self):
        I.submit(self.jobs, self.attempt, 'deferred')
        server = SimpleNamespace(next_id=1, send=lambda value: None)
        self.control.active_turn = 'one'
        self.control.tick(server)
        self.assertTrue(self.control.response({'id': 1, 'error': {'message': 'no active turn'}}))
        self.assertEqual(self.state()[0]['state'], 'queued')
        self.assertIn('deferred', self.control.prepare('next existing resume'))

    def test_same_contract_for_cli_transports(self):
        for harness in ('claude', 'opencode'):
            with self.subTest(harness=harness):
                control = I.OwnerInput(self.jobs, self.attempt, 'thread-1', harness+'-next-turn', self.events.append)
                I.submit(self.jobs, self.attempt, 'correction '+harness, harness)
                prompt = control.prepare('same-session continuation')
                self.assertIn('correction '+harness, prompt)
                control.started(harness+'-turn')
                control.completed(harness+'-turn')
                self.assertEqual(self.state()[-1]['state'], 'turn-completed')

    def test_undelivered_notice_uses_existing_carrier_and_survives_terminal_result(self):
        import dispatch_supervision as supervision
        import dispatch_completion_join as join
        I._path(self.jobs, self.attempt).unlink()
        self.jobs.write_text(self.jobs.read_text().rstrip() +
            ",parent_sid=01a0971c-b5b2-7a62-ab10-0dfb6d2b4e79,"
            "parent_completion_delivery=codex-managed-gateway,managed_sealed_batch_id=batch-test\n")
        control = I.OwnerInput(self.jobs, self.attempt, 'thread-1', 'codex-active-turn', self.events.append)
        I.submit(self.jobs, self.attempt, 'preserve this correction')
        control.take('active-turn')
        control.close()
        records = list((self.root/'pending-delivery').glob('*/*.json'))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertTrue(supervision.notice_is_current(record))
        self.assertIn('correct --jobs', supervision.render_text(record['receipt']))
        self.jobs.write_text(self.jobs.read_text().replace('\topen\t', '\tdone\t'))
        before = self.jobs.read_bytes()
        join.materialize_after_terminal_close(self.jobs, self.attempt)
        self.assertEqual(self.jobs.read_bytes(), before)
        self.assertTrue(supervision.notice_is_current(record))
        self.assertEqual(len(list((self.root/'pending-delivery').glob('*/*.json'))), 1)

    def test_actual_consumer_death_preserves_unknown_send_without_replay(self):
        child_root = self.root/'dead-consumer'; child_root.mkdir()
        jobs = child_root/'jobs.log'
        lease = supervisor_lease_path(jobs, self.attempt)
        jobs.write_text(FIXTURE.owner_row(lease))
        script = child_root/'consumer.py'
        script.write_text("""import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import dispatch_owner_input as I
from dispatch_contract import hold_supervisor_lease,supervisor_lease_path
jobs=Path(sys.argv[2]); attempt=sys.argv[3]
with hold_supervisor_lease(jobs,attempt,supervisor_lease_path(jobs,attempt)):
 c=I.OwnerInput(jobs,attempt,'thread-1','codex-active-turn',lambda v:None)
 I.submit(jobs,attempt,'one correction')
 c.take('active-turn')
 print('sending',flush=True)
 sys.stdin.read()
""")
        child = subprocess.Popen([sys.executable,str(script),str(ROOT/'utilities'),str(jobs),self.attempt],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'sending')
            before = I._path(jobs,self.attempt).read_bytes()
            child.kill(); child.wait(timeout=5)
            observed = I.inspect(jobs,self.attempt)
            self.assertFalse(observed['supervisor_live'])
            self.assertEqual(observed['requests'][0]['delivery_observation'], 'delivery-unknown')
            self.assertTrue(I.unresolved(jobs,self.attempt))
            self.assertEqual(I._path(jobs,self.attempt).read_bytes(), before)
            with self.assertRaisesRegex(I.InputError,'unavailable'):
                I.submit(jobs,self.attempt,'another correction')
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=5)
            child.stdin.close(); child.stdout.close()

    def test_cli_supervisor_delivers_correction_before_terminal_for_both_transports(self):
        fixture = load('owner_input_cli_fixture', ROOT/'utilities/claude_session_supervisor.test.py')
        for harness in ('claude', 'opencode'):
            with self.subTest(harness=harness):
                f = fixture.ClaudeSessionSupervisorTest('test_no_child_finishes_without_resume')
                f.setUp()
                try:
                    f.jobs.write_text(fixture.owner_row(f.lease).replace('harness=claude', 'harness='+harness))
                    native = f.base/'input-runtime.py'
                    native.write_text("""import json,sys,time
from pathlib import Path
prompt=sys.stdin.read()
trace=Path('input-trace')
first=not trace.exists()
with trace.open('a') as out: out.write(json.dumps({'args':sys.argv,'prompt':prompt})+'\\n')
if first:
 Path('input-ready').touch()
 deadline=time.monotonic()+8
 while not Path('input-release').exists() and time.monotonic()<deadline: time.sleep(.01)
text='artifact: -\\nverdict: PASS\\nblocker: none'
if '--format' in sys.argv:
 for kind,part in [('step_start',{}),('text',{'text':text}),('step_finish',{'reason':'stop'})]:
  print(json.dumps({'type':kind,'sessionID':'ses_input','part':part}),flush=True)
else:
 print(json.dumps({'type':'result','is_error':False,'result':text}),flush=True)
""")
                    command = f.command()
                    option = '--claude-command' if harness=='claude' else '--opencode-command'
                    if option in command:
                        command[command.index(option)+1] = shlex.join([sys.executable,str(native)])
                    else:
                        command += [option,shlex.join([sys.executable,str(native)])]
                    if harness=='opencode': command += ['--runtime-harness','opencode']
                    process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE,text=True,env=f.child_env())
                    process.stdin.write('initial work'); process.stdin.close(); process.stdin=None
                    try:
                        deadline=time.monotonic()+8
                        while not (f.base/'input-ready').exists() and time.monotonic()<deadline:
                            if process.poll() is not None: break
                            time.sleep(.01)
                        self.assertTrue((f.base/'input-ready').exists())
                        I.submit(f.jobs,fixture.PARENT,'reuse existing source; do not repeat planning','correction')
                        (f.base/'input-release').touch()
                        out,err=process.communicate(timeout=10)
                        self.assertEqual(process.returncode,0,out+err)
                        turns=[json.loads(line) for line in (f.base/'input-trace').read_text().splitlines()]
                        self.assertEqual(len(turns),2)
                        self.assertIn('reuse existing source',turns[1]['prompt'])
                        self.assertIn('--resume' if harness=='claude' else '--session',turns[1]['args'])
                        receipt=I.inspect(f.jobs,fixture.PARENT)
                        self.assertEqual(receipt['requests'][0]['state'],'turn-completed')
                        self.assertIn('completed-supervisor',f.jobs.read_text())
                    finally:
                        if process.poll() is None: process.kill(); process.communicate(timeout=5)
                finally:
                    f.tearDown()
                    f.doCleanups()

    def test_pending_input_yields_serial_successor_decision_to_existing_owner(self):
        import dispatch_subsession_advance as advance
        from unittest import mock
        I.submit(self.jobs,self.attempt,'change the remaining implementation')
        row=SimpleNamespace(attempt_id='att-slice',status='done',metadata={
            'session_chain_id':'chain-1','subsession_mode':'serial'})
        receipt={'state':'ready','children':[]}
        with mock.patch.object(advance,'advance_chain_step') as launch:
            result=advance.drive_serial_chain(jobs=self.jobs,parent_attempt_id=self.attempt,
                attempts={'att-slice'},receipt=receipt,refresh=lambda attempts:[row],
                join=lambda attempts:receipt,reconcile=lambda rows,attempts:False,max_reparks=1,
                allow_advance=lambda:not self.control.pending())
        launch.assert_not_called()
        self.assertEqual(result.receipt,receipt)
        self.assertIsNone(result.refusal)
        self.assertTrue(self.control.pending())

    def test_real_pipe_cli_submission_reaches_only_exact_active_turn(self):
        script = self.root / 'server.py'
        ready = self.root / 'ready'
        script.write_text('''import json,sys
from pathlib import Path
def send(x): print(json.dumps(x),flush=True)
for line in sys.stdin:
 v=json.loads(line); m=v['method']
 if m=='turn/start':
  send({'id':v['id'],'result':{'turn':{'id':'active-one'}}})
  Path(sys.argv[1]).write_text('ready')
 elif m=='turn/steer':
  assert v['params']['threadId']=='thread-1'
  assert v['params']['expectedTurnId']=='active-one'
  assert 'reuse existing code' in v['params']['input'][0]['text']
  send({'id':v['id'],'result':{'turnId':'active-one'}})
  send({'method':'item/completed','params':{'turnId':'active-one','item':{'type':'agentMessage','text':'correction received'}}})
  send({'method':'turn/completed','params':{'turn':{'id':'active-one','status':'completed'}}})
''')
        server = C.AppServer([sys.executable, str(script), str(ready)], str(self.root), dict(os.environ))
        self.addCleanup(server.close)
        server.input_control = self.control
        args = SimpleNamespace(worktree=str(self.root), sandbox='read-only', network_access=False,
                               writable_root=[], approval='never', model=None, reasoning=None, owner_input=self.control)
        failures = []
        def submit():
            deadline = time.monotonic()+10
            while not ready.exists() and time.monotonic()<deadline:
                time.sleep(.01)
            try:
                body = self.root / 'correction.txt'; body.write_text('reuse existing code')
                result = subprocess.run([sys.executable, str(ROOT/'utilities/capability-route.py'), 'correct',
                                         '--jobs', str(self.jobs), '--attempt-id', self.attempt,
                                         '--message-file', str(body)], capture_output=True, text=True, timeout=10)
                if result.returncode:
                    failures.append(result.stdout+result.stderr)
                    server.process.terminate()
            except Exception as exc:
                failures.append(str(exc)); server.process.terminate()
        sender = threading.Thread(target=submit); sender.start()
        try:
            final, _ = C.run_turn(server, thread_id='thread-1', prompt='wait for correction', args=args)
        finally:
            sender.join(timeout=12)
        self.assertEqual(failures, [])
        self.assertEqual(final, 'correction received')
        self.assertEqual(self.state()[0]['state'], 'turn-completed')
        self.assertEqual(self.state()[0]['turn_id'], 'active-one')


if __name__ == '__main__':
    unittest.main()
