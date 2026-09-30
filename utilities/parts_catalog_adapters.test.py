#!/usr/bin/env python3
"""SD-165 (A165-1): the three adapters print the same part catalog and forward
`capability:stage` graph tokens to the one compiler unmodified."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = [sys.executable, str(ROOT / "utilities" / "capability-route.py")]
SURFACES = {
    "claude": [sys.executable, str(ROOT / "adapters" / "claude" / "bin" / "capability-route.py")],
    "codex": ["sh", str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh")],
    "opencode": ["sh", str(ROOT / "adapters" / "opencode" / "bin" / "preflight.sh")],
}
AUDIT_BORROW = "inspect,autopilot-research:retrieval,autopilot-research:synthesis,report"


class AdapterParityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        # Dev activation: every surface resolves this checkout, never an installed release.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
        self.env.update(AGENT_HOME=str(ROOT), AGENT_DISPATCH_JOBS=str(base / "jobs.log"),
                        DISPATCH_DEFAULTS_CONFIG=str(ROOT / "profiles" / "dispatch-defaults.yaml"))
        (base / "jobs.log").write_text("", encoding="utf-8")
        self.repo, self.artifacts = base / "repo", base / "artifacts"
        self.repo.mkdir()
        self.artifacts.mkdir()
        for argv in (["git", "init", "-q", str(self.repo)],
                     ["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"],
                     ["git", "-C", str(self.repo), "config", "user.name", "Test"]):
            subprocess.run(argv, check=True)
        (self.repo / "README").write_text("fixture\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        self.evidence = base / "dispatch-evidence.json"
        self.evidence.write_text(json.dumps({"tuples": [{
            "parent_harness": "claude", "parent_transport": "headless", "parent_sandbox": "adapter-default",
            "child_harness": "codex", "launch_authority": "conductor", "status": "supported",
            "probe_source": "fixture-probe", "probe_time": "2026-07-16T00:00:00Z", "failure_class": "",
            "checked_worktree": str(self.repo.resolve()), "failure_scope": "none", "codex_command": "ok",
            "retry_on_isolated_worktree": 0}]}))

    def run_surface(self, argv, *args):
        result = subprocess.run([*argv, *args], text=True, capture_output=True, env=self.env, cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_stages_output_is_identical_on_every_adapter(self):
        for args in (("stages", "--capability", "autopilot-lab", "--json"),
                     ("stages", "--capability", "autopilot-lab"), ("stages", "--json")):
            expected = self.run_surface(PORTABLE, *args).stdout
            self.assertIn("autopilot-lab:diagnose", expected)
            for name, argv in SURFACES.items():
                with self.subTest(adapter=name, args=args):
                    self.assertEqual(self.run_surface(argv, *args).stdout, expected)

    def test_compose_forwards_part_tokens_unmodified(self):
        args = ("compose", "--shape", "staged", "--capability", "audit", "--graph", AUDIT_BORROW,
                "--slug", "sd165-parity", "--unassigned", "--cwd", str(self.repo),
                "--artifact-root", str(self.artifacts), "--dispatch-evidence", str(self.evidence),
                "--parent-harness", "claude", "--spec-read", "not-applicable", "--explain")
        expected = json.loads(self.run_surface(PORTABLE, *args).stdout)
        self.assertEqual([node["id"] for node in expected["nodes"]], [
            "inspect", "autopilot-research-retrieval", "autopilot-research-retrieval-alternative",
            "autopilot-research-synthesis", "report"])
        for name, argv in SURFACES.items():
            with self.subTest(adapter=name):
                result = self.run_surface(argv, *args)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertIn("빌린 부품 autopilot-research:retrieval·autopilot-research:synthesis", result.stderr)


if __name__ == "__main__":
    unittest.main()
