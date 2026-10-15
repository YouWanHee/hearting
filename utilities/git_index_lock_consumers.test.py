#!/usr/bin/env python3
"""Consumer-path and native adapter fixture regressions for index-lock failures."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or "node"
MODULE_PATH = ROOT / "utilities" / "git_index_lock.py"
SPEC = importlib.util.spec_from_file_location("git_index_lock", MODULE_PATH)
git_index_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(git_index_lock)


class GitIndexLockConsumerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-lock-consumer-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo with ' quote"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "base")
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        index = self.git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip()
        self.lock = Path(index + ".lock")
        self.lock.write_bytes(b"adapter fixture lock")
        self.lock.chmod(0)
        self.result = self.git("commit", "-m", "expected failure", check=False)
        self.command = "git -C " + shlex.quote(str(self.repo)) + " commit -m 'expected failure'"

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.repo), *args], cwd=self.root,
                              capture_output=True, check=check)

    @staticmethod
    def response_text(value):
        return value.decode("utf-8", errors="replace")

    def codex_payload(self):
        return {
            "session_id": "session-fixture",
            "turn_id": "turn-fixture",
            "tool_use_id": "call-fixture",
            "cwd": str(self.root),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": self.command},
            "tool_response": {
                "exit_code": self.result.returncode,
                "stdout": self.response_text(self.result.stdout),
                "stderr": self.response_text(self.result.stderr),
            },
        }

    def run_bridge(self, harness, payload):
        return subprocess.run(
            ["python3", str(ROOT / "hooks/git-index-lock-context.py"), "--harness", harness],
            cwd=ROOT, input=json.dumps(payload), text=True, capture_output=True, check=False,
        )

    def test_codex_original_bash_event_adds_context_only_to_bound_nonzero(self):
        payload = self.codex_payload()
        output = self.run_bridge("codex", payload)
        self.assertEqual(output.returncode, 0, output.stderr)
        event = json.loads(output.stdout)
        context = event["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(event["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(str(self.lock), context)
        self.assertIn("Ownership: unknown", context)

        payload["tool_response"]["exit_code"] = 0
        self.assertEqual(self.run_bridge("codex", payload).stdout, "")
        payload["tool_response"]["exit_code"] = 128
        payload["tool_response"]["stderr"] = (
            "fatal: Unable to create '/tmp/unrelated/index.lock': File exists.\n")
        self.assertEqual(self.run_bridge("codex", payload).stdout, "")

    def test_codex_unified_exec_uses_its_native_cmd_and_workdir_fields(self):
        payload = self.codex_payload()
        payload["tool_name"] = "exec_command"
        payload["tool_input"] = {
            "cmd": "git commit -m 'expected failure'",
            "workdir": str(self.repo),
        }
        payload["cwd"] = str(self.root)
        output = self.run_bridge("codex", payload)
        self.assertEqual(output.returncode, 0, output.stderr)
        event = json.loads(output.stdout)
        self.assertIn(str(self.lock), event["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Ownership: unknown", event["hookSpecificOutput"]["additionalContext"])

    def test_codex_native_text_response_without_terminal_status_is_not_reclassified(self):
        payload = self.codex_payload()
        payload["tool_response"] = self.response_text(self.result.stderr)
        output = self.run_bridge("codex", payload)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout, "")
        payload["tool_response"] = "command completed without an index-lock failure"
        self.assertEqual(self.run_bridge("codex", payload).stdout, "")

    def test_claude_post_tool_use_failure_uses_native_error_fields(self):
        payload = {
            "session_id": "claude-session-fixture",
            "cwd": str(self.root),
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_use_id": "claude-call-fixture",
            "tool_input": {"command": self.command},
            "error": f"Exit code {self.result.returncode}\n{self.response_text(self.result.stderr)}",
        }
        output = self.run_bridge("claude", payload)
        self.assertEqual(output.returncode, 0, output.stderr)
        event = json.loads(output.stdout)
        self.assertEqual(event["hookSpecificOutput"]["hookEventName"], "PostToolUseFailure")
        self.assertIn(str(self.lock), event["hookSpecificOutput"]["additionalContext"])
        self.assertIn("Ownership: unknown", event["hookSpecificOutput"]["additionalContext"])
        payload["tool_name"] = "Read"
        self.assertEqual(self.run_bridge("claude", payload).stdout, "")

    def test_native_configurations_wire_only_the_original_shell_result_events(self):
        codex = json.loads((ROOT / "adapters/codex/hooks/hooks.json").read_text(encoding="utf-8"))
        codex_hooks = codex["hooks"]["PostToolUse"]
        self.assertTrue(any(
            group.get("matcher") == "^Bash$"
            and any("git-index-lock-context.py" in hook.get("command", "")
                    for hook in group.get("hooks", []))
            for group in codex_hooks))
        claude = json.loads((ROOT / "adapters/claude/settings.json").read_text(encoding="utf-8"))
        for event_name in ("PostToolUse", "PostToolUseFailure"):
            self.assertTrue(any(
                group.get("matcher") == "Bash"
                and any("git-index-lock-context.py" in hook.get("command", "")
                        for hook in group.get("hooks", []))
                for group in claude["hooks"][event_name]))

    def test_actual_opencode_plugin_callback_appends_context_and_preserves_exit_metadata(self):
        script = f'''import {{ AgentHarnessGuards }} from {json.dumps((ROOT / "adapters/opencode/plugins/hearting-guards.js").as_uri())};
const hooks = await AgentHarnessGuards({{ directory: {json.dumps(str(self.repo))},
  worktree: {json.dumps(str(self.repo))}, client: {{}} }});
const input = {{ tool: "bash", sessionID: "ses_lock_fixture", args: {{
  command: "git commit -m 'expected failure'", workdir: {json.dumps(str(self.repo))}
}} }};
const output = {{ args: input.args, output: {json.dumps(self.response_text(self.result.stderr))},
  metadata: {{ exitCode: {self.result.returncode},
    stdout: {json.dumps(self.response_text(self.result.stdout))},
    stderr: {json.dumps(self.response_text(self.result.stderr))} }} }};
await hooks["tool.execute.after"](input, output);
console.log(JSON.stringify({{ output: output.output, metadata: output.metadata }}));
'''
        env = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "FLEET_SESSION_REGISTRY_DIR": str(self.root / "state/sessions"),
            "AGENT_HOME": str(ROOT),
            "AGENT_SESSION_ROLE": "worker",
            "OPENCODE_VERSION": "1.19.0",
        }
        run = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                             env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        result = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(result["metadata"]["exitCode"], self.result.returncode)
        self.assertIn(self.response_text(self.result.stderr), result["output"])
        self.assertIn(str(self.lock), result["output"])
        self.assertIn("Ownership: unknown", result["output"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
