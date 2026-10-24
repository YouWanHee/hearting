import importlib.util
import json
import os
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
        self.assertEqual(fj.auth_file(Path("/tmp/auth"), "opencode"), Path("/tmp/auth/opencode/opencode/auth.json"))

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
        self.assertIn("Attribution may include other work", markdown)

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
