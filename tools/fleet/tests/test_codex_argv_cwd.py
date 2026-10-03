import unittest

from tools.fleet.collectors.procscan import codex_effective_cwd


class CodexArgvCwdTest(unittest.TestCase):
    def test_absolute_root_options_override_observed_cwd(self):
        observed = "/tmp/shell"
        for argv in (
            ["codex", "--cd", "/tmp/project"],
            ["codex", "-C", "/tmp/project"],
            ["codex", "--cd=/tmp/project"],
        ):
            self.assertEqual(codex_effective_cwd(argv, observed), "/tmp/project")

    def test_relative_uses_launch_cwd_and_unknown_keeps_observed(self):
        self.assertEqual(codex_effective_cwd(
            ["codex", "-C", "project"], "/tmp/changed", "/tmp/start"),
            "/tmp/start/project")
        self.assertEqual(codex_effective_cwd(
            ["codex", "-C", "project"], "/tmp/observed", None), "/tmp/observed")

    def test_prompt_and_other_runtime_arguments_are_not_root_options(self):
        self.assertEqual(codex_effective_cwd(
            ["codex", "exec", "--", "please", "--cd", "/tmp/wrong"], "/tmp/real"),
            "/tmp/real")
        self.assertEqual(codex_effective_cwd(
            ["claude", "--cd", "/tmp/wrong"], "/tmp/real"), "/tmp/real")


if __name__ == "__main__":
    unittest.main()
