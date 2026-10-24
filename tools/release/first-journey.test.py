import importlib.util
import json
import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("first_journey", Path(__file__).with_name("first-journey.py"))
fj = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fj)


class JourneyTests(unittest.TestCase):
    def test_clean_environment_is_allowlisted(self):
        env = fj.clean_env(Path("/tmp/world"), [Path("/bin")], {"CODEX_HOME": "/tmp/world/.codex"})
        self.assertEqual(set(env), {"HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                                    "XDG_CACHE_HOME", "TMPDIR", "PATH", "LANG", "TERM", "USER",
                                    "DISABLE_AUTOUPDATER", "CODEX_HOME"})
        self.assertNotIn("HERDR_SOCKET", env)
        self.assertNotIn("CLAUDECODE", env)

    def test_safe_root_rejects_home_and_nas(self):
        with self.assertRaises(ValueError):
            fj.safe_root(str(Path.home()), "root", [Path.home(), Path("/home/nas")])
        if fj.platform.system() == "Linux":
            self.assertEqual(fj.safe_root("/tmp", "local-root", [Path.home(), Path("/home/nas")]), Path("/tmp"))

    def test_auth_allowlists_are_native_credential_files(self):
        self.assertEqual(fj.AUTH_NAMES["claude"], [".credentials.json"])
        self.assertEqual(fj.AUTH_NAMES["codex"], ["auth.json"])
        self.assertEqual(fj.AUTH_NAMES["opencode"], ["auth.json"])
        self.assertEqual(fj.auth_file(Path("/tmp/auth"), "opencode"), Path("/tmp/auth/opencode/auth.json"))

    def test_confirmation_card_gets_one_answer_then_completion(self):
        card = "[실행 확인]\n작업: fix\n이유: typo\n경로: direct\n범위: greet\n완료: test\n\n→ 진행 / 수정: <틀린 부분> / 중단"
        outputs = [json.dumps({"session_id": "s1", "type": "assistant", "message": {"role": "assistant", "content": card}}),
                   json.dumps({"session_id": "s1", "type": "assistant", "message": {"role": "assistant", "content": "Done."}})]
        calls = []
        judged = iter([False, True])
        old = fj.run
        old_sandbox = fj.sandbox
        try:
            fj.run = lambda argv, **k: calls.append(argv) or {"exit": 0, "seconds": 0.1, "output": outputs.pop(0)}
            fj.sandbox = lambda argv, *a, **k: argv
            result = fj.headless_turn("claude", "claude", "task", Path("/tmp/world/repos/demo"), Path("/tmp/world"), {}, 30, [],
                                      judge=lambda: next(judged))
        finally: fj.run = old; fj.sandbox = old_sandbox
        self.assertEqual(result["status"], "candidate-complete")
        self.assertEqual(result["canned_answers"], 1)
        self.assertEqual(calls[1][-3:], ["--resume", "s1", "진행"])

    def test_card_detection_matches_canonical_cards_only(self):
        korean = ("[실행 확인]\n\n작업: 오타 수정\n이유: 출력 오타\n경로: autopilot-code · direct — 작은 수정\n"
                  "범위: greet.py\n완료: 집중 시험 통과\n\n→ 진행 / 수정: <틀린 부분> / 중단")
        english = ("**[Run confirmation]**\n\n**Task:** Fix the typo\n**Why:** wrong output\n**Route:** autopilot-code · direct\n"
                   "**Scope:** greet.py\n**Done:** focused test passes\n\n→ Proceed / Revise: <what is wrong> / Stop")
        self.assertEqual(fj.card_answer(korean), "진행")
        self.assertEqual(fj.card_answer(english), "Proceed")
        self.assertIsNone(fj.card_answer("Should I proceed with the fix?"))
        self.assertIsNone(fj.card_answer("Task: fix\nScope: greet\n→ Proceed / Stop"))

    def test_codex_turns_resume_the_exact_thread(self):
        first = fj.turn_argv("codex", "codex", Path("/tmp/w/repos/codex"), None, "task")
        later = fj.turn_argv("codex", "codex", Path("/tmp/w/repos/codex"), "t-1", "Proceed")
        self.assertEqual(first, ["codex", "exec", "--json", "--cd", "/tmp/w/repos/codex", "task"])
        self.assertEqual(later, ["codex", "exec", "resume", "--json", "t-1", "Proceed"])
        self.assertNotIn("--sandbox", first + later)

    def test_unexpected_question_stops_without_answer(self):
        old = fj.run
        old_sandbox = fj.sandbox
        try:
            fj.run = lambda *a, **k: {"exit": 0, "seconds": 0, "output": json.dumps({"session_id":"s", "type":"assistant", "message":{"role":"assistant", "content":"Which file?"}})}
            fj.sandbox = lambda argv, *a, **k: argv
            result = fj.headless_turn("claude", "claude", "task", Path("/tmp/world/repos/demo"), Path("/tmp/world"), {}, 30, [])
        finally: fj.run = old; fj.sandbox = old_sandbox
        self.assertEqual(result["status"], "unexpected-question")
        self.assertEqual(result["canned_answers"], 0)

    def test_confirmation_answers_are_capped_at_three(self):
        card = "[Run confirmation]\nTask: fix\nWhy: typo\nRoute: direct\nScope: greet\nDone: test\n→ Proceed / Revise: <x> / Stop"
        old = fj.run
        old_sandbox = fj.sandbox
        try:
            fj.run = lambda *a, **k: {"exit": 0, "seconds": 0, "output": json.dumps({"session_id":"s", "type":"assistant", "message":{"role":"assistant", "content":card}})}
            fj.sandbox = lambda argv, *a, **k: argv
            result = fj.headless_turn("claude", "claude", "task", Path("/tmp/world/repos/demo"), Path("/tmp/world"), {}, 30, [])
        finally: fj.run = old; fj.sandbox = old_sandbox
        self.assertEqual(result["canned_answers"], 3)
        self.assertEqual(result["status"], "confirmation-cap")

    def test_unknown_telemetry_is_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = fj.telemetry(Path(tmp), "opencode")
        self.assertIsNone(result["total"])
        self.assertNotEqual(result["total"], 0)

    def test_codex_telemetry_uses_last_cumulative_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "home/.codex/rollout-1.jsonl"; path.parent.mkdir(parents=True)
            path.write_text("\n".join(json.dumps({"type":"event_msg", "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":n,"output_tokens":0,"total_tokens":n}}}}) for n in (10,20)))
            result = fj.telemetry(Path(tmp), "codex")
        self.assertEqual(result["total"], 20)

    def test_codex_telemetry_sums_sessions_not_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, counts in (("rollout-a.jsonl", (10, 20)), ("rollout-b.jsonl", (7,))):
                path = Path(tmp) / "home/.codex/sessions" / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(json.dumps({"type":"event_msg", "payload":{"type":"token_count", "info":{"total_token_usage":{"input_tokens":n,"output_tokens":0,"total_tokens":n}}}}) for n in counts))
            result = fj.telemetry(Path(tmp), "codex")
        self.assertEqual((result["total"], result["sessions"]), (27, 2))

    def test_opencode_telemetry_reads_session_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "home/.local/share/opencode/opencode.db"; db.parent.mkdir(parents=True)
            con = fj.sqlite3.connect(db)
            con.execute("create table session (id text, tokens_input integer, tokens_output integer, tokens_reasoning integer)")
            con.execute("insert into session values ('s', 100, 20, 5)"); con.commit(); con.close()
            result = fj.telemetry(Path(tmp), "opencode")
        self.assertEqual((result["status"], result["total"], result["sessions"]), ("measured", 125, 1))

    def test_claude_usage_deduplicates_message_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"home/.claude/projects/p/a.jsonl"; path.parent.mkdir(parents=True)
            row={"type":"assistant","sessionId":"s","message":{"id":"m","usage":{"input_tokens":5,"output_tokens":2}}}
            path.write_text(json.dumps(row)+"\n"+json.dumps(row))
            result=fj.telemetry(Path(tmp),"claude")
        self.assertEqual(result["input"],5)

    def test_model_run_count_does_not_repeat_claude_in_all_profile(self):
        self.assertEqual(fj.TASK_HARNESSES["claude-only"], ["claude"])
        self.assertEqual(fj.TASK_HARNESSES["all"], ["codex", "opencode"])

    def test_report_has_json_and_markdown_with_unknown_account_usage(self):
        record = {"profile": "all", "source": "published", "isolation": {"level": "env-home-only", "d3": "not-proven"},
                  "elapsed_seconds": 1.25, "steps": [{"name": "install", "exit": 0, "seconds": 1}],
                  "harnesses": [{"name": "claude", "model": {"status": "skipped(model-free)", "turns": 0,
                                                                      "canned_answers": 0, "session_tokens": "unknown"}}],
                  "failures": [], "account_usage": {"before": "unknown", "after": "unknown"}}
        with tempfile.TemporaryDirectory() as tmp:
            fj.write_report(record, Path(tmp))
            value = json.loads((Path(tmp) / "journey.json").read_text())
            markdown = (Path(tmp) / "journey.md").read_text()
        self.assertEqual(value["account_usage"]["before"], "unknown")
        self.assertIn("session tokens unknown", markdown)
        self.assertIn("attribution uncertain", markdown)
        self.assertIn("Headless uncovered", markdown)
        self.assertIn("Isolation checks", markdown)

    def test_success_judgment_accepts_non_main_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            fixture=Path(__file__).parent/"fixtures/first-journey"
            shutil.copy(fixture/"greet.py",repo/"greet.py"); shutil.copy(fixture/"focused_test.py.txt",repo/"test_greet.py")
            subprocess.run(["git","init","-q"],cwd=repo,check=True)
            subprocess.run(["git","config","user.name","Test"],cwd=repo,check=True)
            subprocess.run(["git","config","user.email","test@example.invalid"],cwd=repo,check=True)
            subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","commit","-qm","baseline"],cwd=repo,check=True)
            subprocess.run(["git","switch","-qc","feature"],cwd=repo,check=True)
            (repo/"greet.py").write_text('def greet(): return "Hello"\nif __name__ == "__main__": print(greet())\n')
            result=fj.judge_repo(repo,(fixture/"focused_test.py.txt").read_bytes())
        self.assertTrue(result["code_fixed"])
        self.assertIn("refs/heads/feature",result["local_refs"])

    def test_success_judgment_rejects_edited_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            (repo/"greet.py").write_text('def greet(): return "Hello"\nif __name__ == "__main__": print(greet())\n')
            (repo/"test_greet.py").write_text('import unittest\nfrom greet import greet\nclass T(unittest.TestCase):\n def test_it(self): self.assertEqual(greet(), "wrong")\n')
            subprocess.run(["git","init","-q"],cwd=repo,check=True)
            subprocess.run(["git","config","user.name","Test"],cwd=repo,check=True)
            subprocess.run(["git","config","user.email","test@example.invalid"],cwd=repo,check=True)
            subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","commit","-qm","edited test"],cwd=repo,check=True)
            result=fj.judge_repo(repo,b"original baseline test bytes")
        self.assertFalse(result["code_fixed"])
        self.assertTrue(any(row["reason"] == "baseline-test-edited" for row in result["candidates"]))

    def test_baseline_fixture_expects_hello_but_greet_is_typo(self):
        fixture = Path(__file__).parent / "fixtures/first-journey"
        source = (fixture / "greet.py").read_text()
        test = (fixture / "focused_test.py.txt").read_text()
        self.assertIn('return "Helo"', source)
        self.assertIn('self.assertEqual(greet(), "Hello")', test)

    def test_sandbox_requires_bwrap_on_linux(self):
        if fj.platform.system() != "Linux":
            self.skipTest("Linux namespace check")
        old = fj.shutil.which
        try:
            fj.shutil.which = lambda command: None if command == "bwrap" else old(command)
            with self.assertRaisesRegex(RuntimeError, "isolation-unavailable"):
                fj.sandbox(["true"], Path("/tmp"), {})
        finally:
            fj.shutil.which = old

    def test_sandbox_keeps_world_path_and_hides_home_and_nas(self):
        if fj.platform.system() != "Linux" or not fj.shutil.which("bwrap"):
            self.skipTest("Linux bwrap check")
        world = Path("/tmp/first-journey-unit")
        joined = " ".join(fj.sandbox(["true"], world, {"HOME": str(world / "home")}))
        self.assertIn(f"--bind {world} {world}", joined)
        self.assertIn("--tmpfs /home/nas", joined)
        self.assertIn("--tmpfs /home ", joined)
        self.assertNotIn("/journey ", joined)
        self.assertIn(f"--setenv HOME {world}/home", joined)

    def test_sandbox_read_only_binds_resolver_target_to_etc_resolv_conf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "resolver-target"; target.write_text("nameserver 192.0.2.1\n")
            resolver = root / "resolv.conf"; resolver.symlink_to(target)
            old_system = fj.platform.system
            old_which = fj.shutil.which
            try:
                fj.platform.system = lambda: "Linux"
                fj.shutil.which = lambda command: "/usr/bin/bwrap" if command == "bwrap" else old_which(command)
                args = fj.sandbox(["true"], root / "world", {}, system_files={"resolv.conf": resolver})
            finally:
                fj.platform.system = old_system
                fj.shutil.which = old_which
        pairs = [args[i + 1:i + 3] for i, arg in enumerate(args[:-2]) if arg == "--ro-bind"]
        self.assertIn([str(target), "/etc/resolv.conf"], pairs)

    def test_run_kills_and_reaps_child_after_keyboard_interrupt(self):
        if fj.platform.system() != "Linux": self.skipTest("process groups are exercised on Linux")
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            script = "import os,time; open(%r,'w').write(str(os.getpid())); time.sleep(60)" % str(pidfile)
            previous = signal.getsignal(signal.SIGUSR1)
            signal.signal(signal.SIGUSR1, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            def interrupt_when_started():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not pidfile.exists(): time.sleep(0.01)
                os.kill(os.getpid(), signal.SIGUSR1)
            timer = threading.Timer(0.05, interrupt_when_started)
            timer.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    fj.run(["python3", "-c", script], env=dict(os.environ), timeout=30)
            finally:
                timer.cancel(); signal.signal(signal.SIGUSR1, previous)
            child_pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError): os.kill(child_pid, 0)

    def test_invalid_auth_json_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store.json"; store.write_text('{"token":"original"}')
            target = Path(tmp) / "world-auth.json"; target.write_text('{"token":"')
            previous = fj._ACTIVE_AUTH
            try:
                fj._ACTIVE_AUTH = ("claude", store, target, store.read_bytes())
                fj.persist_active_auth()
            finally:
                fj._ACTIVE_AUTH = previous
            self.assertEqual(store.read_text(), '{"token":"original"}')

    def test_incomplete_credentials_never_replace_store_and_complete_fixtures_do(self):
        rejected = {
            "claude": (b"{}", b'{"claudeAiOauth":{"accessToken":"fake-access"}}'),
            "codex": (b"{}", b'{"tokens":{"access_token":"","refresh_token":"fake-refresh"}}'),
            "opencode": (b"{}",),
        }
        complete = {
            "claude": b'{"claudeAiOauth":{"accessToken":"fake-access","refreshToken":"fake-refresh"}}',
            "codex": b'{"tokens":{"access_token":"fake-access","refresh_token":"fake-refresh"}}',
            "opencode": b'{"account":{"type":"fake"}}',
        }
        for harness, candidates in rejected.items():
            for candidate in candidates:
                with self.subTest(harness=harness, candidate=candidate), tempfile.TemporaryDirectory() as tmp:
                    store = Path(tmp) / "store.json"
                    original = b'{"keep":"original-bytes"}'
                    store.write_bytes(original)
                    target = Path(tmp) / "world-auth.json"
                    target.write_bytes(candidate)
                    previous = fj._ACTIVE_AUTH
                    try:
                        fj._ACTIVE_AUTH = (harness, store, target, original)
                        self.assertFalse(fj.credential_complete(harness, candidate))
                        fj.persist_active_auth()
                    finally:
                        fj._ACTIVE_AUTH = previous
                    self.assertEqual(store.read_bytes(), original)

        for harness, candidate in complete.items():
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp) / "store.json"
                original = b'{"keep":"original-bytes"}'
                store.write_bytes(original)
                target = Path(tmp) / "world-auth.json"
                target.write_bytes(candidate)
                previous = fj._ACTIVE_AUTH
                try:
                    fj._ACTIVE_AUTH = (harness, store, target, original)
                    self.assertTrue(fj.credential_complete(harness, candidate))
                    fj.persist_active_auth()
                finally:
                    fj._ACTIVE_AUTH = previous
                self.assertEqual(store.read_bytes(), candidate)

    def test_unsupported_transport_does_not_execute_task_argv(self):
        calls = []
        previous = fj.run
        previous_sandbox = fj.sandbox
        try:
            fj.run = lambda argv, **kwargs: calls.append(argv) or {
                "exit": 0, "seconds": 0, "output": "Usage: opencode run [prompt]"}
            fj.sandbox = lambda argv, *a, **k: argv
            transport = fj.probe_transport("opencode", "opencode", Path("/tmp/world"), {}, [], 5)
            outcome = fj.headless_turn("opencode", "opencode", "Fix the task", Path("/tmp/repo"),
                                       Path("/tmp/world"), {}, 5, [], transport_supported=transport["supported"])
        finally:
            fj.run = previous; fj.sandbox = previous_sandbox
        self.assertFalse(transport["supported"])
        self.assertEqual(outcome["status"], "unsupported-headless-transport")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-2:], ["run", "--help"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
