#!/usr/bin/env python3
"""Native process/session boundaries and the shared controller, without model calls."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import selectors
from types import SimpleNamespace
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import opencode_session_runtime as runtime
import dispatch_parent_completion as parent
from dispatch_contract import hold_supervisor_lease

spec = importlib.util.spec_from_file_location(
    "shared_controller_fixture", ROOT / "utilities/claude_session_supervisor.test.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class NativeSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "native.py"
        self.args = SimpleNamespace(
            worktree=str(self.root), jobs=str(self.root / "jobs.log"),
            parent_attempt_id="att-native", turn_timeout=2,
            opencode_command=shlex.join([sys.executable, str(self.script)]),
            model="opencode-go/glm-5.3-flash", variant="runtime-default", opencode_agent="build")
        self.events = []

    def program(self, body):
        self.script.write_text("import json, sys, time\n"
            "prompt = sys.stdin.read()\n"
            "def event(kind, **values):\n"
            " print(json.dumps(dict(type=kind, sessionID='ses_native', **values)), flush=True)\n"
            + body)

    def test_exact_session_resume_and_only_final_step_is_returned(self):
        self.program("""
session = sys.argv[sys.argv.index('--session') + 1] if '--session' in sys.argv else None
with open('trace', 'a') as f: f.write(json.dumps([session, prompt]) + '\\n')
event('step_start', part={})
event('text', part={'text': 'intermediate deliberation'})
event('tool_use', part={'tool':'bash', 'callID':'call-exact', 'state':{'status':'completed', 'output':'private'}})
event('step_finish', part={'reason':'tool-calls'})
event('step_start', part={})
event('text', part={'text':'artifact: -\\nverdict: PASS\\nblocker: none'})
event('step_finish', part={'reason':'stop'})
""")
        first, code = runtime.run_turn(self.args, "first", emit=self.events.append)
        second, code2 = runtime.run_turn(self.args, "second", emit=self.events.append)
        self.assertEqual((code, code2), (0, 0))
        self.assertEqual(first, second)
        self.assertEqual(first['result'], 'artifact: -\nverdict: PASS\nblocker: none')
        self.assertEqual([json.loads(x) for x in (self.root/'trace').read_text().splitlines()],
                         [[None, 'first'], ['ses_native', 'second']])
        self.assertEqual([e['type'] for e in self.events],
                         ['dispatch.supervisor.session', 'tool_use', 'dispatch.supervisor.session', 'tool_use'])
        self.assertNotIn('private', json.dumps(self.events))

    def test_session_switch_is_rejected_and_original_binding_preserved(self):
        runtime.bind_session(self.args, 'ses_original')
        self.program("event('step_start', part={})\n")
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, 'identity-mismatch'):
            runtime.run_turn(self.args, 'continue', emit=self.events.append)
        self.assertEqual(runtime.read_binding(self.args), 'ses_original')

    def test_foreign_or_dangling_native_binding_is_not_adopted(self):
        runtime.bind_session(self.args, 'ses_original')
        path = runtime.binding_path(self.args)
        value = json.loads(path.read_text())
        value['attempt_id'] = 'att-other'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, 'binding-invalid'):
            runtime.read_binding(self.args)
        path.unlink()
        path.symlink_to(self.root/'missing')
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, 'binding-unsafe'):
            runtime.read_binding(self.args)

    def test_exit_before_final_stop_is_not_success(self):
        self.program("event('text', part={'text':'artifact: -\\nverdict: PASS\\nblocker: none'})\n")
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, 'final-stop-missing'):
            runtime.run_turn(self.args, 'start', emit=self.events.append)

    def test_native_capacity_error_survives_process_exit(self):
        from dispatch_supervisor_terminal import classify_session_result
        self.program("event('error', error={'message':'rate limited', 'status':429})\nsys.exit(1)\n")
        result, code = runtime.run_turn(self.args, 'start', emit=self.events.append)
        terminal = classify_session_result(result, code, runtime='opencode')
        self.assertEqual(terminal.failure_class, 'capacity')
        self.assertEqual(terminal.terminal_event, 'opencode-result')

    def test_timeout_reaps_the_native_process(self):
        self.args.turn_timeout = .15
        self.program("from pathlib import Path\nimport os\nPath('pid').write_text(str(os.getpid()))\nevent('step_start', part={})\ntime.sleep(10)\n")
        with self.assertRaisesRegex(runtime.OpenCodeTransportError, 'turn-timeout'):
            runtime.run_turn(self.args, 'start', emit=self.events.append)
        pid = int((self.root/'pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class ActiveTurnDeadlineTest(unittest.TestCase):
    """The native turn timeout is a no-progress window for a general owner
    turn (any event restarts it); silence, a real exit, a session mismatch and
    a sealed handoff keep their existing behaviour. These drive the real
    supervisor `run_turn`, which is what decides the policy."""

    WINDOW = 0.6
    READINESS_HANG_GUARD = 5
    CHATTY = """
from pathlib import Path
import os
Path('pid').write_text(str(os.getpid()))
ack = int(os.environ['HEARTING_ACK_FD'])
time.sleep(float(os.environ.get('HEARTING_START_DELAY', '0')))
ack_delay = float(os.environ.get('HEARTING_ACK_DELAY', '0'))
def paced(kind, **values):
    global ack_delay
    event(kind, **values)
    os.read(ack, 1)
    if ack_delay:
        time.sleep(ack_delay)
        ack_delay = 0
paced('step_start', part={})
for _ in range(6):
    paced('tool_use', part={'tool':'bash', 'callID':'c', 'state':{'status':'running'}})
paced('text', part={'text':'artifact: -\\nverdict: PASS\\nblocker: none'})
paced('step_finish', part={'reason':'stop'})
"""

    program = NativeSessionTest.program

    def setUp(self):
        NativeSessionTest.setUp(self)
        self.args.turn_timeout = self.WINDOW
        self.args.runtime_harness = "opencode"
        self.supervisor = fixture.supervisor
        patch = unittest.mock.patch.object(self.supervisor, "emit")
        patch.start()
        self.addCleanup(patch.stop)

    def turn(self, **kwargs):
        return self.supervisor.run_turn(self.args, "sess", "go", resume=False, **kwargs)

    def virtual_turn(self, *, startup_delay=0, ack_delay=0, **kwargs):
        clock = SimpleNamespace(now=0.0)
        original_selector = selectors.DefaultSelector
        readiness_hang_guard = self.READINESS_HANG_GUARD

        class Clock:
            @staticmethod
            def monotonic():
                return clock.now

        class ControlledSelector:
            def __init__(self):
                self.inner = original_selector()

            def __enter__(self):
                self.inner.__enter__()
                return self

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

            def register(self, *args, **kw):
                return self.inner.register(*args, **kw)

            def select(self, timeout=None):
                # Real startup/ACK scheduling has its own hang guard. The
                # production timeout argument controls only the virtual clock.
                ready = self.inner.select(readiness_hang_guard)
                # A ready chunk advances virtual time by less than the idle window;
                # silence consumes the whole remaining deadline. Real readiness and
                # the real subprocess pipe remain in use.
                clock.now += min(0.3, timeout or 0.0) if ready else (timeout or 0.0)
                return ready

            def close(self):
                return self.inner.close()

        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        original_popen = runtime.subprocess.Popen

        def controlled_popen(*args, **kw):
            env = dict(os.environ)
            env['HEARTING_ACK_FD'] = str(read_fd)
            env['HEARTING_START_DELAY'] = str(startup_delay)
            env['HEARTING_ACK_DELAY'] = str(ack_delay)
            kw.update(pass_fds=(read_fd,), env=env)
            return original_popen(*args, **kw)

        real_read = os.read

        def acknowledged_read(fd, size):
            chunk = real_read(fd, size)
            if chunk and fd != read_fd:
                os.write(write_fd, b'!')
            return chunk

        with unittest.mock.patch.object(runtime, 'time', Clock()), \
             unittest.mock.patch.object(runtime.selectors, 'DefaultSelector', ControlledSelector), \
             unittest.mock.patch.object(runtime.subprocess, 'Popen', controlled_popen), \
             unittest.mock.patch.object(runtime.os, 'read', acknowledged_read):
            return self.turn(**kwargs), clock

    def test_native_live_turn_with_events_survives_past_the_window(self):
        self.program(self.CHATTY)
        (result, code), clock = self.virtual_turn()
        self.assertGreater(clock.now, self.WINDOW)
        self.assertEqual(code, 0)
        self.assertEqual(result["result"], "artifact: -\nverdict: PASS\nblocker: none")

    def test_real_startup_and_ack_delays_do_not_consume_virtual_deadline_under_load(self):
        # Delay both real readiness boundaries beyond the semantic window.
        # The load process stays owned by this fixture and is always reaped.
        load = subprocess.Popen([sys.executable, '-c', 'while True: pass'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.program(self.CHATTY)
            (result, code), clock = self.virtual_turn(
                startup_delay=self.WINDOW * 1.5, ack_delay=self.WINDOW * 1.5)
            self.assertGreater(clock.now, self.WINDOW)
            self.assertEqual(code, 0)
            self.assertEqual(result['result'], 'artifact: -\nverdict: PASS\nblocker: none')
            with self.assertRaises(ProcessLookupError):
                os.kill(int((self.root / 'pid').read_text()), 0)
        finally:
            load.terminate()
            load.wait(timeout=5)

    def test_native_silence_for_the_whole_window_still_times_out_and_reaps(self):
        self.program("from pathlib import Path\nimport os\nPath('pid').write_text(str(os.getpid()))\n"
                     "event('step_start', part={})\ntime.sleep(10)\n")
        with self.assertRaisesRegex(self.supervisor.SupervisorError, "opencode-turn-timeout"):
            self.virtual_turn()
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "pid").read_text()), 0)

    def test_native_post_eof_wait_stays_bounded(self):
        self.program("from pathlib import Path\nimport os\nPath('pid').write_text(str(os.getpid()))\n"
                     "event('step_start', part={})\nos.close(1)\ntime.sleep(10)\n")
        started = time.monotonic()
        with self.assertRaisesRegex(self.supervisor.SupervisorError, "opencode-turn-timeout"):
            self.turn()
        self.assertLess(time.monotonic() - started, 5)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "pid").read_text()), 0)

    def test_real_exit_before_final_stop_still_fails(self):
        self.program("event('text', part={'text':'x'})\n")
        with self.assertRaisesRegex(self.supervisor.SupervisorError, "final-stop-missing"):
            self.turn()

    def test_session_mismatch_still_fails(self):
        runtime.bind_session(self.args, "ses_original")
        self.program("event('step_start', part={})\n")
        with self.assertRaisesRegex(self.supervisor.SupervisorError, "identity-mismatch"):
            self.turn()

    def test_a_sealed_handoff_keeps_the_fixed_deadline_even_with_events(self):
        self.program(self.CHATTY)
        with self.assertRaisesRegex(self.supervisor.SupervisorError, "opencode-turn-timeout"):
            self.virtual_turn(handoff_intent={"intent_id": "handoff"})


class SharedControllerTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ClaudeSessionSupervisorTest('test_resume_uses_same_session_once_after_join')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_actual_lease_is_required_for_every_harness(self):
        f = self.f
        request = SimpleNamespace(parent_attempt_id=fixture.PARENT, jobs=str(f.jobs))
        for harness, delivery in [('claude','session-resume-supervised'),
                                 ('codex','app-server-supervised'),
                                 ('opencode','session-resume-supervised')]:
            with self.subTest(harness=harness):
                f.jobs.write_text(fixture.owner_row(f.lease).replace('harness=claude', 'harness='+harness)
                                  .replace('completion_delivery=session-resume-supervised', 'completion_delivery='+delivery))
                self.assertFalse(parent.parent_supervisor_is_live(request))
                with hold_supervisor_lease(f.jobs, fixture.PARENT, f.lease):
                    self.assertTrue(parent.parent_supervisor_is_live(request))
                    other = SimpleNamespace(parent_attempt_id='att-other', jobs=str(f.jobs))
                    self.assertFalse(parent.parent_supervisor_is_live(other))
                self.assertFalse(parent.parent_supervisor_is_live(request))

    def test_common_join_resumes_the_same_native_session_and_commits_once(self):
        f = self.f
        native = f.base/'native.py'
        native.write_text("""
import json, os, sys
session = sys.argv[sys.argv.index('--session') + 1] if '--session' in sys.argv else 'ses_actual'
prompt = sys.stdin.read()
with open(sys.argv[sys.argv.index('--fixture-state') + 1]) as s:
 state = json.load(s)
 assert state['phase'] == 'running-turn', state
 if '--session' in sys.argv:
  assert state['outbox']['attempt_ids'] and not state['outbox'].get('consumed_attempt_ids'), state
with open(os.environ['FAKE_TRACE'], 'a') as f:
 f.write(json.dumps({'event':'native-turn', 'session':session, 'resume':'--session' in sys.argv, 'prompt':prompt}) + '\\n')
text = 'artifact: -\\nverdict: PASS\\nblocker: none' if '--session' in sys.argv else 'artifact: -\\nverdict: BLOCKED\\nblocker: registered child running; report follows on wake'
for kind, part in [('step_start',{}), ('text',{'text':text}), ('step_finish',{'reason':'stop'})]:
 print(json.dumps({'type':kind,'sessionID':session,'part':part}))
""")
        f.jobs.write_text(fixture.owner_row(f.lease).replace('harness=claude', 'harness=opencode')
                          + fixture.child_row())
        command = f.command() + ['--runtime-harness','opencode', '--opencode-command',
                                  shlex.join([sys.executable, str(native), '--fixture-state', str(f.state)])]
        result = subprocess.run(command, input='initial', text=True, capture_output=True,
                                env=f.child_env(FAKE_TRACE=str(f.trace)), timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        trace = [json.loads(line) for line in f.trace.read_text().splitlines()]
        self.assertEqual([t['event'] for t in trace], ['native-turn','join-start','join-end','native-turn'])
        turns = [t for t in trace if t['event']=='native-turn']
        self.assertEqual([t['session'] for t in turns], ['ses_actual','ses_actual'])
        self.assertEqual([t['resume'] for t in turns], [False, True])
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(sum(e['type']=='result' for e in events), 1)
        self.assertEqual(events[-1]['runtime'], 'opencode')
        self.assertIn('note=completed-supervisor', f.jobs.read_text())
        log = f.base/'attempt.opencode.jsonl'
        log.write_text(result.stdout)
        checked = subprocess.run([sys.executable, str(ROOT/'utilities/codex_dispatch_terminal.py'),
                                  '--worktree',str(f.base),'--artifact-root-metadata',str(f.artifact_root),str(log)],
                                 capture_output=True,text=True,env=f.child_env())
        self.assertEqual(checked.returncode, 0, checked.stdout+checked.stderr)
        self.assertIn('\tvalid\texact-opencode-result\tPASS', checked.stdout)

    def test_real_adapter_selects_controller_only_for_bound_owner(self):
        spec = importlib.util.spec_from_file_location('oc_adapter', ROOT/'adapters/opencode/bin/dispatch-headless.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        args = SimpleNamespace(owner_route_binding=SimpleNamespace(route_file='/route.json',route_id='rt-exact',route_hash='sha256:exact'),
            jobs_path=self.f.jobs, attempt_id=fixture.PARENT, worktree=str(self.f.base),agent='build',
            resolved_model_settings={'source':'profile','model':'opencode-go/glm-5.3-flash','variant':'runtime-default'})
        command = shlex.split(module.shell_command(args, self.f.base/'prompt', self.f.base/'log'))
        self.assertIn('--runtime-harness', command)
        self.assertEqual(command[command.index('--parent-attempt-id')+1], fixture.PARENT)
        self.assertEqual(command[command.index('--lease-file')+1], str(self.f.lease))
        args.attempt_id = None  # Public dry-run allocates neither row nor identity.
        preview = module.shell_command(args, self.f.base/'prompt', self.f.base/'log')
        self.assertIn('--parent-attempt-id unassigned', preview)
        self.assertIn('preview-only.lease', preview)
        args.owner_route_binding = None
        self.assertTrue(module.shell_command(args, self.f.base/'prompt', self.f.base/'log').startswith('opencode run '))


    def test_input_queued_before_the_first_turn_binds_the_native_session(self):
        import dispatch_owner_input as owner_input
        f = self.f
        native = f.base/'native.py'
        native.write_text("""
import json, os, sys
prompt = sys.stdin.read()
with open(os.environ['FAKE_TRACE'], 'a') as f:
 f.write(json.dumps({'args':sys.argv[1:], 'prompt':prompt}) + '\\n')
text = 'artifact: -\\nverdict: PASS\\nblocker: none'
for kind, part in [('step_start',{}), ('text',{'text':text}), ('step_finish',{'reason':'stop'})]:
 print(json.dumps({'type':kind,'sessionID':'ses_actual','part':part}))
""")
        f.jobs.write_text(fixture.owner_row(f.lease).replace('harness=claude', 'harness=opencode'))
        owner_input.initialize_owner_input(f.jobs, fixture.PARENT, 'opencode-next-turn')
        owner_input.submit(f.jobs, fixture.PARENT, 'early word', 'early')
        command = f.command() + ['--runtime-harness', 'opencode', '--opencode-command', shlex.join([sys.executable, str(native)])]
        result = subprocess.run(command, input='initial', text=True, capture_output=True,
                                env=f.child_env(FAKE_TRACE=str(f.trace)), timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        turns = [json.loads(line) for line in f.trace.read_text().splitlines()]
        self.assertEqual(len(turns), 1)
        self.assertIn('early word', turns[0]['prompt'])
        self.assertNotIn('--session', turns[0]['args'])
        receipt = owner_input.inspect(f.jobs, fixture.PARENT)
        self.assertEqual(receipt['requests'][0]['state'], 'turn-completed')
        self.assertEqual(receipt['thread_id'], 'ses_actual')


if __name__ == '__main__':
    unittest.main()
