#!/usr/bin/env python3
"""Actual controller/worker/applier/sync pipeline, synthetic models and bare Git."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
MEM = ROOT / "tools/memory/mem.py"
HELPER = ROOT / "utilities/memory-post-curation-sync.py"
FLAGS = {"AGENT_SESSION_ROLE": "worker", "AGENT_DISPATCH_CHILD": "1",
         "AGENT_DISPATCH_DEPTH": "0", "OPENCODE_DISPATCH_SLUG": "synthetic",
         "FLEET_TITLE_REFRESH": "1", "MEM_DISTILL": "1"}


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


syncer = module("post_sync_test_module", HELPER)
completion = module("memory_session_completion", ROOT / "utilities/memory_session_completion.py")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-post-sync-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.env = {"PATH": str(self.base / "bin") + os.pathsep + os.defpath,
                    "HOME": str(self.base / "home"), "AGENT_HOME": str(ROOT),
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1",
                    "MEM_SYNC_REMOTE": "0", "MEM_DUMP_PUSH": "0",
                    "MEM_POST_CURATION_SYNC_TIMEOUT": "10", "MEM_DISTILL_ENABLE": "1",
                    "CODEX_DISTILL_ENABLE": "1", "CODEX_DISTILL_APPLY": "1",
                    "CODEX_DISTILL_CONTRACT_ACCEPTED": "1", "CODEX_DISTILL_TIMEOUT_CURATE": "10",
                    "MEM_DISTILL_TIMEOUT_CURATE": "10", "OPENCODE_DISTILL_ENABLE": "1",
                    "OPENCODE_DISTILL_APPLY": "1", "OPENCODE_DISTILL_TIMEOUT": "10"}
        for key, suffix in {"XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data",
                            "XDG_STATE_HOME": "state", "CODEX_HOME": "codex-home",
                            "CLAUDE_CONFIG_DIR": "claude-home", "MEM_STORE": "store",
                            "MEM_PROJECTS": "projects", "CODEX_SESSIONS": "sessions",
                            "MEM_RECALL_RECEIPTS": "recall-receipts",
                            "MEM_SESSION_COMPLETION_RECEIPTS": "receipts",
                            "AGENT_MODEL_GOVERNOR_ROOT": "governor",
                            "AGENT_ARTIFACT_ROOT": "artifacts", "MEM_PROFILE": "profiles"}.items():
            self.env[key] = str(self.base / suffix)
            Path(self.env[key]).mkdir(mode=0o700)
        Path(self.env["HOME"]).mkdir()
        (self.base / "bin").mkdir()
        self.env["MEM_WRITE_EVENTS"] = str(self.base / "events/write.jsonl")
        self.env["MEM_RECALL_EVENTS"] = str(self.base / "events/recall.jsonl")
        self.env["MEM_SYNC_DIR"] = str(self.base / "exchange")
        self.env["OPENCODE_EXPORT_FILE"] = str(self.base / "export.json")
        self.env["TEST_CALL"] = str(self.base / "model-call.json")
        self.env["TEST_ACTION"] = json.dumps({"action": "add", "tier": "durable", "type": "user-correction",
            "body": "POSTSYNCNONCE deployment is ap-northeast-2.", "headline": "POSTSYNCNONCE region",
            "aliases": ["POSTSYNCNONCE", "deployment"], "entities": ["POSTSYNCNONCE"],
            "topics": ["deployment"], "artifact_refs": []})
        self.sid = "synthetic-post-sync"
        script = "#!" + sys.executable + "\n" + r"""
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
Path(os.environ['TEST_CALL']).write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),
    'role':os.environ.get('AGENT_SESSION_ROLE'),'distill':os.environ.get('MEM_DISTILL')}))
if os.environ.get('TEST_RESTORE_REMOTE'):
    remote = Path(os.environ['MEM_SYNC_REMOTE_URL'])
    remote.with_suffix('.offline').rename(remote)
if os.environ.get('TEST_OFFLINE'):
    remote = Path(os.environ['MEM_SYNC_REMOTE_URL'])
    remote.rename(remote.with_suffix('.offline'))
if os.environ.get('TEST_FAIL'):
    raise SystemExit(17)
action = '' if os.environ.get('TEST_EMPTY') else os.environ['TEST_ACTION'] + '\n'
if name == 'codex':
    Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(action)
else:
    sys.stdout.write(action)
"""
        for name in ("codex", "claude", "opencode"):
            path = self.base / "bin" / name
            path.write_text(script)
            path.chmod(0o700)
        self.env["OPENCODE_BIN"] = str(self.base / "bin/opencode")
        self.env["MEM_DISTILL_WORKER"] = str(ROOT / "adapters/claude/bin/mem-distill-worker.sh")
        self.env["MEM_APPLIER"] = str(ROOT / "tools/memory/apply-distill-actions.py")
        self.env["MODEL_WORKER_GOVERNOR"] = str(ROOT / "utilities/model-worker-governor.py")

    def call(self, argv, env=None, rc=0, data=None, timeout=20, cwd=None):
        result = subprocess.run([str(x) for x in argv], env=env or self.env, cwd=cwd or self.project,
                                input=data, capture_output=True, text=True, timeout=timeout)
        if rc is not None:
            self.assertEqual(result.returncode, rc, (argv, result.stdout, result.stderr))
        return result

    def mem(self, *args, rc=0):
        return self.call([sys.executable, MEM, *args], rc=rc)

    def prepare(self, runtime, remote=False):
        self.runtime = runtime
        text = json.loads(self.env["TEST_ACTION"])["body"]
        if runtime == "codex":
            path = Path(self.env["CODEX_SESSIONS"]) / (self.sid + ".jsonl")
            path.write_text(json.dumps({"type":"event_msg","payload":{"type":"user_message","id":"u1","message":text}}) + "\n")
        elif runtime == "claude":
            path = Path(self.env["MEM_PROJECTS"]) / "fixture" / (self.sid + ".jsonl")
            path.parent.mkdir()
            path.write_text(json.dumps({"type":"user","uuid":"u1","message":{"role":"user","content":text}}) + "\n")
        else:
            Path(self.env["OPENCODE_EXPORT_FILE"]).write_text(json.dumps({"messages":[
                {"info":{"id":"u1","role":"user"},"parts":[{"type":"text","text":text}]}]}))
        if remote:
            self.remote = self.base / "remote.git"
            self.call(["git", "init", "-q", "--bare", self.remote])
            self.env.update(MEM_SYNC_REMOTE="1", MEM_SYNC_REMOTE_URL=str(self.remote),
                            MEM_SYNC_REF="refs/heads/hearting-memory-v2-shared")
            joined = json.loads(self.mem("migration", "join", "--apply", "--json").stdout)
            self.assertTrue(joined["remote_allowed"], joined)
        else:
            self.mem("index")

    def controller(self, expected=0):
        runtime = self.runtime
        preflight = ROOT / "adapters" / runtime / "bin/preflight.sh"
        if runtime == "codex":
            launched = completion.launch("codex", self.sid, str(self.project), str(preflight),
                ["session-end", str(self.project), self.sid], timeout=30, env=self.env)
            self.assertEqual(launched["state"], "started")
            deadline = time.monotonic() + 32
            try:
                while time.monotonic() < deadline:
                    receipt = completion.read_receipt("codex", self.sid, self.env)
                    if receipt and receipt["state"] in ("completed", "failed"):
                        self.assertEqual(receipt["exit_code"], expected, receipt)
                        return receipt
                    time.sleep(0.05)
                self.fail("completion deadline exceeded")
            finally:
                receipt = completion.read_receipt("codex", self.sid, self.env)
                if receipt and receipt["state"] not in ("completed", "failed"):
                    item = receipt.get("runner")
                    if item and completion._alive(item) is True:
                        os.kill(item["pid"], signal.SIGTERM)
        elif runtime == "opencode":
            return self.call([preflight, "session-end", self.project, self.sid], rc=expected, timeout=30, cwd=ROOT)
        else:
            self.call(["bash", ROOT / "adapters/claude/hooks/mem-distill-dispatch.sh"],
                      data=json.dumps({"cwd":str(self.project),"session_id":self.sid}))
            lock = Path(self.env["MEM_STORE"]) / (".distill-lock-" + self.sid)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if Path(self.env["TEST_CALL"]).exists() and not lock.exists():
                    return None
                time.sleep(0.05)
            self.fail("Claude detached controller did not complete")

    def stored(self):
        result = json.loads(self.mem("recall", "POSTSYNCNONCE", "--full", "--json").stdout)["results"]
        self.assertEqual(len(result), 1, result)
        self.assertEqual(result[0]["body"], json.loads(self.env["TEST_ACTION"])["body"])
        self.assertTrue(result[0]["id"])
        self.assertEqual((Path(self.env["MEM_STORE"]) / (".distill-state-" + self.sid)).read_text().strip(), "u1")
        self.assertEqual(self.mem("distill", self.sid, "--source", self.runtime).stdout, "")
        call = json.loads(Path(self.env["TEST_CALL"]).read_text())
        self.assertEqual((call["role"], call["distill"]), ("worker", "1"))
        return result[0]

    def test_codex_uploads_curator_write_before_completion(self):
        self.check_remote("codex")

    def test_claude_uploads_curator_write_before_controller_release(self):
        self.check_remote("claude")

    def test_opencode_uploads_curator_write_before_controller_return(self):
        self.check_remote("opencode")

    def check_remote(self, runtime):
        self.prepare(runtime, remote=True)
        self.controller()
        record = self.stored()
        found = self.call(["git", "--git-dir", self.remote, "grep", "-l", "POSTSYNCNONCE",
                           self.env["MEM_SYNC_REF"], "--", "protocol/v2/ops"]).stdout
        self.assertTrue(found.strip(), "controller did not publish the curator operation")
        doctor = json.loads(self.mem("doctor", "--json").stdout)
        self.assertEqual(doctor["status"], "remote-confirmed", doctor)
        self.assertEqual(doctor["sync"]["outbox_ids"], [])
        self.assertIn(record["id"], self.mem("show", record["id"]).stdout)

    def test_local_only_completion_refreshes_dump_without_remote_opt_in(self):
        self.prepare("opencode")
        self.controller()
        self.stored()
        doctor = json.loads(self.mem("doctor", "--json").stdout)
        self.assertEqual(doctor["status"], "local-only", doctor)
        self.assertIn("POSTSYNCNONCE", (Path(self.env["MEM_STORE"]) / "dump.jsonl").read_text())
        self.assertFalse((self.base / "exchange").exists())

    def test_offline_post_sync_reports_failure_and_keeps_record_marker_and_outbox(self):
        self.prepare("codex", remote=True)
        self.env["TEST_OFFLINE"] = "1"
        receipt = self.controller(expected=1)
        self.assertEqual(receipt["state"], "failed")
        self.stored()
        doctor = json.loads(self.mem("doctor", "--json", rc=1).stdout)
        self.assertEqual(doctor["sync"]["status"], "queued-offline", doctor)
        self.assertTrue(doctor["sync"]["outbox_ids"])

    def test_initial_sync_failure_is_not_hidden_by_successful_final_sync(self):
        self.prepare("codex", remote=True)
        self.remote.rename(self.remote.with_suffix(".offline"))
        self.env["TEST_RESTORE_REMOTE"] = "1"
        receipt = self.controller(expected=1)
        self.assertEqual(receipt["state"], "failed")
        self.stored()
        doctor = json.loads(self.mem("doctor", "--json").stdout)
        self.assertEqual(doctor["status"], "remote-confirmed", doctor)
        self.assertEqual(doctor["sync"]["outbox_ids"], [])

    def test_curator_failure_preserves_earlier_exit_and_still_attempts_final_sync(self):
        self.prepare("codex")
        self.env.update(CODEX_DISTILL_CONTRACT_ACCEPTED="0", MEM_POST_CURATION_SYNC_TIMEOUT="nan")
        result = self.call([ROOT / "adapters/codex/bin/preflight.sh", "session-end", self.project, self.sid], rc=69)
        self.assertIn("invalid-timeout", result.stderr)
        self.assertIn("post-curation", result.stderr)
        self.assertFalse((Path(self.env["MEM_STORE"]) / (".distill-state-" + self.sid)).exists())
        self.assertEqual(json.loads(self.mem("recall", "POSTSYNCNONCE", "--full", "--json").stdout)["results"], [])

    def test_empty_curator_still_attempts_final_sync_without_fabricating_memory(self):
        self.prepare("opencode")
        self.env.update(TEST_EMPTY="1", MEM_POST_CURATION_SYNC_TIMEOUT="nan")
        result = self.controller(expected=2)
        self.assertIn("invalid-timeout", result.stderr)
        self.assertEqual((Path(self.env["MEM_STORE"]) / (".distill-state-" + self.sid)).read_text().strip(), "u1")
        self.assertEqual(json.loads(self.mem("recall", "POSTSYNCNONCE", "--full", "--json").stdout)["results"], [])

    def test_worker_flags_make_every_controller_and_helper_inert(self):
        for runtime in ("codex", "opencode", "claude"):
            for key, value in FLAGS.items():
                with self.subTest(runtime=runtime, flag=key):
                    env = dict(self.env, **{key:value})
                    if runtime == "claude":
                        command = ["bash", ROOT / "adapters/claude/hooks/mem-distill-dispatch.sh"]
                        data = json.dumps({"cwd":str(self.project),"session_id":self.sid})
                    else:
                        command = [ROOT / "adapters" / runtime / "bin/preflight.sh", "session-end", self.project, self.sid]
                        data = None
                    self.call(command, env=env, data=data)
                    self.call([sys.executable, HELPER, "/missing-cwd"], env=env)
        self.assertEqual(list(Path(self.env["MEM_STORE"]).iterdir()), [])
        self.assertFalse(Path(self.env["TEST_CALL"]).exists())
        self.assertFalse(Path(self.env["MEM_WRITE_EVENTS"]).exists())

    def test_native_discarded_stderr_still_leaves_bounded_private_failure_evidence(self):
        self.prepare("opencode")
        self.env["MEM_POST_CURATION_SYNC_TIMEOUT"] = "nan"
        result = subprocess.run([str(ROOT / "adapters/opencode/bin/preflight.sh"), "session-end",
                                 str(self.project), self.sid], env=self.env, cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.stored()
        path = Path(self.env["XDG_STATE_HOME"]) / "agent-memory/post-curation-failures/opencode.json"
        receipt = json.loads(path.read_text())
        self.assertEqual(receipt["reason"], "invalid-timeout")
        self.assertEqual(receipt["phase"], "post-curation-sync")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(str(self.project), path.read_text())
        self.assertNotIn(self.sid, path.read_text())
        self.assertNotIn("POSTSYNCNONCE", path.read_text())

    def test_failure_receipt_replacement_is_bounded_and_rejects_foreign_links(self):
        result = {"status_schema":1,"phase":"post-curation-sync","status":"hard-failure",
                  "exit_code":2,"reason":"synthetic-failure"}
        syncer.record_failure(str(self.project), "codex", self.sid, result, self.env)
        root = Path(self.env["XDG_STATE_HOME"]) / "agent-memory/post-curation-failures"
        path = root / "codex.json"
        syncer.record_failure(str(self.project), "codex", self.sid + "-second", result, self.env)
        self.assertEqual([p.name for p in root.iterdir()], ["codex.json"])
        self.assertLess(path.stat().st_size, 4096)
        outside = self.base / "foreign"
        outside.write_text("preserve-this-fixture")
        outside.chmod(0o600)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises((ValueError,OSError)):
            syncer.record_failure(str(self.project), "codex", self.sid, result, self.env)
        path.unlink()
        os.link(outside,path)
        with self.assertRaises((ValueError,OSError)):
            syncer.record_failure(str(self.project), "codex", self.sid, result, self.env)
        self.assertEqual(outside.read_text(),"preserve-this-fixture")
        self.assertEqual([p.name for p in root.iterdir()], ["codex.json"])

    def test_finite_timeout_invalid_results_and_failure_diagnostics(self):
        for value in ("nan", "inf", "0", "301", ""):
            with self.subTest(timeout=value):
                result = syncer.run_sync(self.project, dict(self.env, MEM_POST_CURATION_SYNC_TIMEOUT=value))
                self.assertEqual((result["exit_code"], result["reason"]), (2,"invalid-timeout"))
        for payload in ({"status":"hard-failure","exit_code":0},
                        {"status":"remote-confirmed","exit_code":1},
                        {"status":"local-only"}):
            result = syncer.run_sync(self.project, self.env, [sys.executable,"-c","print(" + repr(json.dumps(payload)) + ")"])
            self.assertEqual(result["exit_code"], 2, result)
        result = syncer.run_sync(self.project, self.env,
            [sys.executable,"-c","import sys; print('SYNTHETIC_PRIVATE_PAYLOAD'); print('SYNTHETIC_PRIVATE_ERROR',file=sys.stderr);sys.exit(7)"])
        self.assertEqual(result["exit_code"],7)
        self.assertNotIn("SYNTHETIC_PRIVATE",json.dumps(result))

    def test_timeout_and_fast_orphan_are_cleaned_without_escaping_completion_group(self):
        for slow in (False, True):
            with self.subTest(slow=slow):
                info = self.base / ("child-slow" if slow else "child-fast")
                code = f"""import os,time,json
from pathlib import Path
p=os.fork()
if p == 0:
    q=os.fork()
    if q != 0: os._exit(0)
    Path({str(info)!r}).write_text(json.dumps({{'pid':os.getpid(),'pgid':os.getpgrp()}}))
    time.sleep(20)
    os._exit(0)
while not Path({str(info)!r}).exists(): time.sleep(0.001)
"""
                code += "time.sleep(20)" if slow else "print(json.dumps({'status':'local-only','exit_code':0}))"
                started = time.monotonic()
                result = syncer.run_sync(self.project, dict(self.env, MEM_POST_CURATION_SYNC_TIMEOUT="1"),
                                         [sys.executable,"-c",code])
                self.assertLess(time.monotonic()-started, 3)
                self.assertEqual(result["exit_code"],124 if slow else 0,result)
                child = json.loads(info.read_text())
                self.assertEqual(child["pgid"],os.getpgrp())
                deadline = time.monotonic()+1
                while time.monotonic()<deadline:
                    row = syncer.process(child["pid"])
                    if row is None or row[2] == "Z": break
                    time.sleep(0.01)
                else: self.fail("orphaned child survived cleanup")


if __name__ == "__main__":
    unittest.main()
