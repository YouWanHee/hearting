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
        self.assertEqual(codex_effective_cwd(
            ["codex", "-C", "project"], "/tmp/start/project", "/tmp/start/project"),
            "/tmp/start/project")

    def test_prompt_and_other_runtime_arguments_are_not_root_options(self):
        self.assertEqual(codex_effective_cwd(
            ["codex", "exec", "--", "please", "--cd", "/tmp/wrong"], "/tmp/real"),
            "/tmp/real")
        self.assertEqual(codex_effective_cwd(
            ["claude", "--cd", "/tmp/wrong"], "/tmp/real"), "/tmp/real")

    def test_prompt_subcommand_and_separator_do_not_supply_root_options(self):
        cases = (
            ["codex", "please", "--cd", "/tmp/wrong"],
            ["codex", "exec", "--cd", "/tmp/wrong"],
            ["codex", "exec", "--", "--cd", "/tmp/wrong"],
            ["codex", "--", "--cd", "/tmp/wrong"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                self.assertEqual(codex_effective_cwd(argv, "/tmp/real"), "/tmp/real")

    def test_attached_short_and_equals_long_root_options_are_parsed(self):
        self.assertEqual(codex_effective_cwd(["codex", "-C/tmp/project"], "/tmp/shell"),
                         "/tmp/project")
        self.assertEqual(codex_effective_cwd(["codex", "--cd=/tmp/project"], "/tmp/shell"),
                         "/tmp/project")


if __name__ == "__main__":
    unittest.main()
