#!/usr/bin/env python3
"""Codex nudge integration with real controllers and synthetic worker effects.

All mutable state lives under /var/tmp. No provider CLI or real mem.py runs.
TEST_SOURCE_ROOT permits a private candidate to exercise the source checkout.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

SOURCE = Path(os.environ.get("TEST_SOURCE_ROOT", Path(__file__).resolve().parents[3]))
COPIES = (
    "adapters/codex/bin/preflight.sh", "adapters/codex/bin/distill-nudge-launch.py",
    "utilities/memory_session_completion.py", "core/CORE.md",
    "adapters/codex/utilities/agent-home.sh", "utilities/artifact-root.sh",
    "utilities/memory-store.sh", "hooks/core-first-guard.sh",
)
WORKER_FLAGS = {"AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_CHILD": "1",
                "AGENT_DISPATCH_DEPTH": "0", "OPENCODE_DISPATCH_SLUG": "fixture",
                "FLEET_TITLE_REFRESH": "1", "MEM_DISTILL": "1"}
WORKER = r'''#!/usr/bin/python3
import json, os, pathlib, sys, time
sid,cwd,mode=sys.argv[1:]
path=pathlib.Path(os.environ['FAKE_EVENTS'])
def record(event, **extra):
    with path.open('a') as stream:
        stream.write(json.dumps({'event':event,'mode':mode,'sid':sid,'cwd':cwd,
            'pid':os.getpid(),'at':time.monotonic(),**extra})+'\n')
record('worker-start', completion=os.environ.get('MEM_SESSION_COMPLETION'),
       enable=os.environ.get('CODEX_DISTILL_ENABLE'),
       apply=os.environ.get('CODEX_DISTILL_APPLY'),
       accepted=os.environ.get('CODEX_DISTILL_CONTRACT_ACCEPTED'))
if mode=='increment' and os.environ.get('FAKE_HOLD')=='1':
    deadline=time.monotonic()+10
    while not pathlib.Path(os.environ['FAKE_RELEASE']).exists():
        if time.monotonic()>=deadline:
            record('worker-expired');sys.exit(98)
        time.sleep(.01)
code=int(os.environ.get('FAKE_INCREMENT_EXIT' if mode=='increment' else 'FAKE_CURATE_EXIT','0'))
if not code:
    pathlib.Path(os.environ['FAKE_MARKER']).write_text(mode+'\n')
record('worker-finish', exit_code=code)
sys.exit(code)
'''
SIDE_EFFECT = r'''import json,os,sys,time
with open(os.environ['FAKE_EVENTS'],'a') as stream:
    stream.write(json.dumps({'event':EVENT,'args':sys.argv[1:],'at':time.monotonic(),'pid':os.getpid()})+'\n')
raise SystemExit(int(os.environ.get(EXIT_VARIABLE,'0')))
'''
HOOK_RUNNER = r'''import importlib.util,os,sys,types
from pathlib import Path
# Fixture environment is established by the parent before any source import.
# Unrelated observers/candidate lookup are stubs; main and preflight I/O are real.
fleet=types.ModuleType('fleet');fleet.__path__=[];sys.modules['fleet']=fleet
for name,attrs in {
 'fleet.token_accounting':{'record_accounting':lambda *a,**k:None},
 'fleet.token_budget':{'DIRECTIVE_TEXTS':{}},
 'session_summary_trigger':{'launch_trigger':lambda *a,**k:None},
 'herdr_session_projection':{'project':lambda *a,**k:None},
}.items():
 module=types.ModuleType(name);module.__dict__.update(attrs);sys.modules[name]=module
source=Path(os.environ['TEST_SOURCE_ROOT'])
spec=importlib.util.spec_from_file_location('fixture_prompt',source/'adapters/codex/hooks/userprompt-lifecycle.py')
hook=importlib.util.module_from_spec(spec);sys.modules[spec.name]=hook;spec.loader.exec_module(hook)
hook.interaction_session_id=lambda payload:''
hook.peer_notice=lambda *a:None
hook.sd111_first_prompt_sweep=lambda *a:None
hook.candidate_context=lambda *a:'fixture-candidate-headline [fixture-id]'
hook.local_evidence_context=lambda *a:''
hook.token_budget_context=lambda *a:''
hook.PREFLIGHT=Path(os.environ['TEST_PREFLIGHT'])
actual=hook.run_preflight
def run(*args,**kwargs):
    if args[0]=='briefing':return ''
    assert args[0]=='turn-nudge',args
    return actual(*args,**kwargs)
hook.run_preflight=run
raise SystemExit(hook.main())
'''


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class NudgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-nudge-test-", dir="/var/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fixture = self.base / "source"
        self.fixture.mkdir(mode=0o700)
        for relative in COPIES:
            target = self.fixture / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(SOURCE / relative, target)
        for relative in ("roles", "capabilities", "tools/memory"):
            (self.fixture / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
        worker = self.fixture / "adapters/codex/bin/distill-worker.sh"
        worker.write_text(WORKER); worker.chmod(0o700)
        (self.fixture / "tools/memory/mem.py").write_text(
            "EVENT='initial-sync'\nEXIT_VARIABLE='FAKE_SYNC_EXIT'\n" + SIDE_EFFECT)
        (self.fixture / "utilities/memory-post-curation-sync.py").write_text(
            "EVENT='post-sync'\nEXIT_VARIABLE='FAKE_POST_EXIT'\n" + SIDE_EFFECT)
        self.hook_runner = self.base / "hook-runner.py"
        self.hook_runner.write_text(HOOK_RUNNER)
        self.sid = "nudge-fixture-session"
        self.cwd = self.base / "project"; self.cwd.mkdir(mode=0o700)
        self.paths = {key: self.base / value for key, value in {
            "HOME":"home", "XDG_CONFIG_HOME":"config", "XDG_DATA_HOME":"data",
            "XDG_STATE_HOME":"state", "XDG_CACHE_HOME":"cache", "XDG_RUNTIME_DIR":"runtime",
            "CODEX_HOME":"codex", "CLAUDE_CONFIG_DIR":"claude", "TMPDIR":"tmp",
            "MEM_STORE":"store", "MEM_PROJECTS":"projects", "CODEX_SESSIONS":"sessions",
            "MEM_PROFILE":"profiles", "AGENT_MODEL_GOVERNOR_ROOT":"governor",
            "AGENT_ARTIFACT_ROOT":"artifacts", "FLEET_RUN_DIR":"fleet",
        }.items()}
        for path in self.paths.values(): path.mkdir(mode=0o700)
        # Explicit allowlist, not os.environ.copy(): no live auth/config/model paths.
        self.env = {"PATH":"/usr/bin:/bin", "LANG":"C.UTF-8", "PYTHONDONTWRITEBYTECODE":"1",
            "AGENT_HOME":str(self.fixture), "TEST_SOURCE_ROOT":str(SOURCE),
            "TEST_PREFLIGHT":str(self.fixture / "adapters/codex/bin/preflight.sh"),
            "AGENT_DISPATCH_JOBS":str(self.base / "jobs.log"),
            "MEM_WRITE_EVENTS":str(self.base / "write-events.jsonl"),
            "MEM_RECALL_EVENTS":str(self.base / "recall-events.jsonl"),
            "MEM_RECALL_RECEIPTS":str(self.base / "recall-receipts"),
            "MEM_SESSION_COMPLETION_RECEIPTS":str(self.base / "receipts"),
            "MEM_SYNC_REMOTE":"0", "MEM_DUMP_PUSH":"0", "MEM_DUMP_COMMIT":"0",
            "MEM_SESSION_COMPLETION_TIMEOUT":"30", "MEM_NUDGE_INTERVAL":"10",
            "CODEX_DISTILL_ENABLE":"1", "AGENT_MODEL_WORKERS_DISABLED":"1",
            "FAKE_EVENTS":str(self.base / "events.jsonl"),
            "FAKE_RELEASE":str(self.base / "release"), "FAKE_MARKER":str(self.base / "marker"),
            **{key:str(path) for key,path in self.paths.items()}}
        self.children = []
        self.addCleanup(self.cleanup_processes)
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.completion = load_module("memory_session_completion", self.fixture / "utilities/memory_session_completion.py")
            self.launcher = load_module("fixture_nudge_launcher", self.fixture / "adapters/codex/bin/distill-nudge-launch.py")
        self.addCleanup(self.assert_no_database)

    def assert_no_database(self):
        self.assertFalse(list(self.base.rglob("*.db")), "fixture called a real database writer")
        self.assertFalse(list(self.base.rglob("*.sqlite*")))

    def cleanup_processes(self):
        # Let a held synthetic worker exit normally first. Kill only receipt-
        # proven process identities created inside this test's private state.
        Path(self.env["FAKE_RELEASE"]).touch()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            row = self.receipt()
            if not row or row["state"] in ("completed", "failed"): break
            time.sleep(.02)
        row = self.receipt()
        for key in ("command", "runner"):
            identity = row.get(key) if row else None
            if identity and identity["pid"] != os.getpid() and self.completion.identity_alive(identity):
                try: os.killpg(identity["pid"], signal.SIGTERM)
                except ProcessLookupError: pass
        for child in self.children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=3)

    def receipt(self):
        return self.completion.read_receipt("codex-nudge", self.sid, self.env)

    def events(self):
        path = Path(self.env["FAKE_EVENTS"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value: return value
            time.sleep(.02)
        self.fail("synthetic lifecycle did not reach its bounded expected state")

    def terminal(self):
        return self.wait_for(lambda: (r if (r := self.receipt()) and r["state"] in ("completed", "failed") else None))

    def state_path(self):
        return self.paths["MEM_STORE"] / (".codex-turn-state-" + self.sid)

    def prompt(self, **extra):
        payload = json.dumps({"hook_event_name":"UserPromptSubmit", "session_id":self.sid,
                              "cwd":str(self.cwd), "prompt":"synthetic prompt"})
        before = time.monotonic()
        result = subprocess.run([sys.executable, "-B", str(self.hook_runner)], cwd=self.cwd,
            env=dict(self.env, **extra), input=payload, text=True, capture_output=True, timeout=2)
        return result, time.monotonic() - before

    def direct(self, *args, **extra):
        return subprocess.run([sys.executable, "-B", str(self.fixture / "adapters/codex/bin/distill-nudge-launch.py"),
            *args, self.sid, str(self.cwd)], cwd=self.cwd, env=dict(self.env, **extra),
            text=True, capture_output=True, timeout=3)

    def session_end(self, **extra):
        child = subprocess.Popen([str(self.fixture / "adapters/codex/bin/preflight.sh"),
            "session-end", str(self.cwd), self.sid], cwd=self.cwd, env=dict(self.env, **extra),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.children.append(child)
        return child

    def start_held(self, **extra):
        self.state_path().write_text("9\n")
        result, duration = self.prompt(FAKE_HOLD="1", **extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(duration, 1.0)
        self.assertIn("fixture-candidate-headline [fixture-id]", result.stdout)
        self.wait_for(lambda: self.events())
        self.assertEqual(self.events()[0]["event"], "worker-start")
        return self.receipt()

    def test_tenth_prompt_returns_candidates_before_worker_and_next_counter(self):
        receipt = self.start_held()
        self.assertEqual(receipt["state"], "started")
        self.assertRegex(receipt["input_generation"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.state_path().read_text(), "0\n")
        self.assertFalse(Path(self.env["FAKE_MARKER"]).exists())
        result, duration = self.prompt(FAKE_HOLD="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(duration, 1.0)
        self.assertIn("fixture-id", result.stdout)
        self.assertEqual(self.state_path().read_text(), "1\n")
        self.assertEqual(len(self.events()), 1)
        Path(self.env["FAKE_RELEASE"]).touch()
        terminal = self.terminal()
        self.assertEqual(terminal["state"], "completed")
        self.assertEqual(terminal["memory_apply"], "not-asserted")
        self.assertEqual(self.events()[0]["mode"], "increment")
        self.assertEqual(self.events()[0]["sid"], self.sid)
        self.assertEqual(self.events()[0]["cwd"], str(self.cwd))
        self.assertEqual([self.events()[0][key] for key in ("enable","apply","accepted")], ["1"]*3)
        self.assertEqual(self.events()[0]["completion"], "1")

    def test_active_second_firing_is_deduplicated_then_new_generation_runs(self):
        first = self.start_held()
        self.state_path().write_text("9\n")
        result, _ = self.prompt(FAKE_HOLD="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state_path().read_text(), "0\n")
        self.assertEqual(self.receipt()["input_generation"], first["input_generation"])
        self.assertEqual(len(self.events()), 1)
        Path(self.env["FAKE_RELEASE"]).touch(); self.terminal()
        self.state_path().write_text("9\n")
        result, _ = self.prompt()
        self.assertEqual(result.returncode, 0, result.stderr)
        final = self.terminal()
        self.assertNotEqual(final["input_generation"], first["input_generation"])
        self.assertEqual(len([r for r in self.events() if r["event"]=="worker-start"]), 2)

    def test_all_worker_flags_skip_prompt_and_launcher_without_state(self):
        for key, value in WORKER_FLAGS.items():
            with self.subTest(marker=key):
                result, _ = self.prompt(**{key:value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.direct(**{key:value}).returncode, 0)
                self.assertFalse(self.state_path().exists())
                self.assertIsNone(self.receipt())
                self.assertFalse(self.events())
        self.assertEqual(self.direct(MEM_SESSION_COMPLETION="1").returncode, 0)
        self.assertIsNone(self.receipt())

    def test_optout_launcher_and_missing_wait_create_nothing(self):
        self.assertEqual(self.direct(CODEX_DISTILL_ENABLE="0").returncode, 0)
        self.assertEqual(self.direct("--wait").returncode, 0)
        self.assertIsNone(self.receipt())
        self.assertFalse((self.base / "receipts").exists())
        self.assertFalse(self.events())

    def test_session_end_waits_for_increment_before_all_completion_phases(self):
        self.start_held()
        child = self.session_end(MEM_SESSION_COMPLETION="1")
        time.sleep(.15)
        self.assertIsNone(child.poll())
        self.assertEqual([r["event"] for r in self.events()], ["worker-start"])
        Path(self.env["FAKE_RELEASE"]).touch()
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr)
        sequence = [(r["event"], r.get("mode")) for r in self.events()]
        self.assertEqual(sequence, [("worker-start","increment"),("worker-finish","increment"),
            ("initial-sync",None),("worker-start","curate"),("worker-finish","curate"),("post-sync",None)])
        self.assertEqual(Path(self.env["FAKE_MARKER"]).read_text(), "curate\n")

    def test_failed_increment_retains_marker_and_curator_can_retry(self):
        Path(self.env["FAKE_MARKER"]).write_text("prior-frontier\n")
        self.start_held(FAKE_INCREMENT_EXIT="7")
        Path(self.env["FAKE_RELEASE"]).touch()
        row = self.terminal()
        self.assertEqual((row["state"],row["reason"],row["exit_code"]), ("failed","command-failed",7))
        self.assertEqual(Path(self.env["FAKE_MARKER"]).read_text(), "prior-frontier\n")
        child = self.session_end(MEM_SESSION_COMPLETION="1")
        _, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(Path(self.env["FAKE_MARKER"]).read_text(), "curate\n")
        self.assertEqual([r["mode"] for r in self.events() if r["event"]=="worker-start"], ["increment","curate"])

    def test_initial_sync_warning_remains_visible_after_curator_and_post_sync(self):
        child = self.session_end(FAKE_SYNC_EXIT="1")
        _, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 1)
        self.assertIn("memory sync status=1", stderr)
        self.assertEqual([r["event"] for r in self.events()], ["initial-sync","worker-start","worker-finish","post-sync"])

    def test_immediate_session_end_joins_without_waiting_for_worker_start(self):
        self.state_path().write_text("9\n")
        result, _ = self.prompt(FAKE_HOLD="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Do not await worker-start or runner publication before SessionEnd.
        child = self.session_end(MEM_SESSION_COMPLETION="1")
        self.wait_for(lambda: self.events())
        self.assertEqual([r["event"] for r in self.events()], ["worker-start"])
        self.assertIsNone(child.poll())
        Path(self.env["FAKE_RELEASE"]).touch()
        _, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr)
        events = self.events()
        self.assertLess(next(i for i,r in enumerate(events) if r["event"]=="worker-finish"),
                        next(i for i,r in enumerate(events) if r["event"]=="initial-sync"))
        self.assertEqual(events[-1]["event"], "post-sync")

    def test_unpublished_startup_waits_beyond_one_second_until_finite_deadline(self):
        unpublished = {"state":"started", "launcher":{"pid":1}, "runner":None, "command":None}
        published = dict(unpublished, runner={"pid":2})
        clock = [0.0]
        def tick(seconds): clock[0] += seconds
        def observed_receipt(*args):
            if clock[0] < 1.35: return unpublished
            if clock[0] < 1.5: return published
            return {"state":"completed"}
        with mock.patch.object(self.launcher, "read_receipt", side_effect=observed_receipt), \
                mock.patch.object(self.launcher, "identity_state", side_effect=lambda identity: "dead" if identity["pid"]==1 else "alive"), \
                mock.patch.object(self.launcher.time, "monotonic", side_effect=lambda:clock[0]), \
                mock.patch.object(self.launcher.time, "sleep", side_effect=tick):
            self.launcher.wait_for_nudge(self.sid, timeout=2)
        self.assertGreaterEqual(clock[0], 1.5)
        self.assertLess(clock[0], 2)
        clock[0] = 0.0
        with mock.patch.object(self.launcher,"read_receipt",return_value=unpublished), \
                mock.patch.object(self.launcher,"identity_state",return_value="dead"), \
                mock.patch.object(self.launcher.time,"monotonic",side_effect=lambda:clock[0]), \
                mock.patch.object(self.launcher.time,"sleep",side_effect=tick):
            with self.assertRaisesRegex(self.completion.CompletionError,"nudge-completion-timeout"):
                self.launcher.wait_for_nudge(self.sid,timeout=2)
        self.assertGreaterEqual(clock[0],2)
        self.assertLess(clock[0],2.1)

    def test_wait_rejects_unknown_or_dead_identity_and_deadline(self):
        receipt = {"state":"started", "launcher":{"pid":1}, "runner":{"pid":2}, "command":None}
        for state in ("unknown", "dead"):
            with self.subTest(state=state), mock.patch.object(self.launcher,"read_receipt",return_value=receipt), \
                    mock.patch.object(self.launcher,"identity_state",return_value=state):
                with self.assertRaisesRegex(self.completion.CompletionError,"nudge-completion-unavailable"):
                    self.launcher.wait_for_nudge(self.sid, timeout=.1)
        with mock.patch.object(self.launcher,"read_receipt",return_value=receipt), \
                mock.patch.object(self.launcher,"identity_state",return_value="alive"):
            before=time.monotonic()
            with self.assertRaisesRegex(self.completion.CompletionError,"nudge-completion-timeout"):
                self.launcher.wait_for_nudge(self.sid, timeout=.08)
            self.assertLess(time.monotonic()-before,.3)

    def test_wait_worker_flags_skip_receipt_access(self):
        for key,value in WORKER_FLAGS.items():
            with self.subTest(marker=key), mock.patch.dict(os.environ,dict(self.env,**{key:value}),clear=True), \
                    mock.patch.object(sys,"argv",["launcher","--wait",self.sid,str(self.cwd)]), \
                    mock.patch.object(self.launcher,"read_receipt",side_effect=AssertionError("worker read receipt")):
                self.assertEqual(self.launcher.main(),0)


if __name__ == "__main__":
    unittest.main()
