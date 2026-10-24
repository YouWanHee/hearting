import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
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
        card = "[실행 확인]\nTask: fix\nWhy: typo\nRoute: direct\nScope: greet\nDone: test\n진행 / 수정 / 중단"
        outputs = [json.dumps({"session_id": "s1", "type": "assistant", "message": {"role": "assistant", "content": card}}),
                   json.dumps({"session_id": "s1", "type": "assistant", "message": {"role": "assistant", "content": "Hello; test_greet passed"}})]
        old = fj.run
        try:
            fj.run = lambda *a, **k: {"exit": 0, "seconds": 0.1, "output": outputs.pop(0)}
            result = fj.headless_turn("claude", "claude", "task", Path("/tmp/world/repos/demo"), Path("/tmp/world"), {}, 5, [])
        finally: fj.run = old
        self.assertEqual(result["status"], "candidate-complete")
        self.assertEqual(result["canned_answers"], 1)

    def test_unexpected_question_stops_without_answer(self):
        old = fj.run
        try:
            fj.run = lambda *a, **k: {"exit": 0, "seconds": 0, "output": json.dumps({"session_id":"s", "type":"assistant", "message":{"role":"assistant", "content":"Which file?"}})}
            result = fj.headless_turn("claude", "claude", "task", Path("/tmp/world/repos/demo"), Path("/tmp/world"), {}, 5, [])
        finally: fj.run = old
        self.assertEqual(result["status"], "unexpected-question")
        self.assertEqual(result["canned_answers"], 0)

    def test_confirmation_answers_are_capped_at_three(self):
        card = "[실행 확인]\nTask: fix\nWhy: typo\nRoute: direct\nScope: greet\nDone: test\n진행 / 수정 / 중단"
        old = fj.run
        try:
            fj.run = lambda *a, **k: {"exit": 0, "seconds": 0, "output": json.dumps({"session_id":"s", "type":"assistant", "message":{"role":"assistant", "content":card}})}
            result = fj.headless_turn("claude", "claude", "task", Path("/tmp/world/repos/demo"), Path("/tmp/world"), {}, 5, [])
        finally: fj.run = old
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
        self.assertIn("session tokens=unknown", markdown)
        self.assertIn("attribution uncertain", markdown)
        self.assertIn("Headless uncovered", markdown)
        self.assertIn("Isolation checks", markdown)

    def test_success_judgment_accepts_non_main_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)
            fixture=Path(__file__).parent/"fixtures/first-journey"
            shutil.copy(fixture/"greet.py",repo/"greet.py"); shutil.copy(fixture/"test_greet.py",repo/"test_greet.py")
            subprocess.run(["git","init","-q"],cwd=repo,check=True)
            subprocess.run(["git","config","user.name","Test"],cwd=repo,check=True)
            subprocess.run(["git","config","user.email","test@example.invalid"],cwd=repo,check=True)
            subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","commit","-qm","baseline"],cwd=repo,check=True)
            subprocess.run(["git","switch","-qc","feature"],cwd=repo,check=True)
            (repo/"greet.py").write_text('def greet(): return "Hello"\nif __name__ == "__main__": print(greet())\n')
            result=fj.judge_repo(repo,(fixture/"test_greet.py").read_bytes())
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
        test = (fixture / "test_greet.py").read_text()
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
