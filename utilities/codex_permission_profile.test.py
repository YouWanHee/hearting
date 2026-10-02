#!/usr/bin/env python3
"""Native Git protection regression, plus profile/fallback projection checks."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import codex_permission_profile as profiles


class PermissionProfileTest(unittest.TestCase):
    def setUp(self):
        # /tmp is already writable in :workspace. Use another disposable root
        # so a write to the primary checkout actually tests an outside path.
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-codex-git-", dir="/var/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.wt = self.base / "worktree"
        self.env = {"PATH": os.environ["PATH"]}
        for key, name in (("HOME", "home"), ("CODEX_HOME", "codex-home"),
                          ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"),
                          ("TMPDIR", "tmp"), ("CODEX_APP_SERVER_SOCKET_DIR", "sockets")):
            path = self.base / name
            path.mkdir(mode=0o700)
            self.env[key] = str(path)
        self.git("init", "-q", str(self.repo), cwd=self.base)
        self.git("config", "user.email", "fixture@example.com")
        self.git("config", "user.name", "Fixture")
        (self.repo / "source.txt").write_text("base\n")
        self.git("add", "source.txt")
        self.git("commit", "-qm", "base")
        self.git("worktree", "add", "-qb", "worker", str(self.wt))
        self.roots = [self.repo / ".git/worktrees/worktree", *(
            self.repo / ".git" / name for name in ("objects", "refs", "logs"))]

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def profile(self):
        with mock.patch.object(profiles, "named_profiles_available", return_value=True):
            return profiles.commit_profile_config(str(self.wt), list(map(str, self.roots)),
                                                  "workspace-write", False)

    def test_only_complete_existing_git_grant_uses_profile(self):
        config = self.profile()
        self.assertEqual(config["default_permissions"], profiles.PROFILE_NAME)
        body = config["permissions"][profiles.PROFILE_NAME]
        self.assertEqual(body["extends"], ":workspace")
        self.assertEqual({path for path, access in body["filesystem"].items() if access == "write"}, set(map(str, self.roots)))
        self.assertEqual(body["filesystem"][str(self.repo / ".git")], "read")
        with mock.patch.object(profiles, "named_profiles_available", return_value=True):
            self.assertIsNone(profiles.commit_profile_config(str(self.wt), [], "workspace-write", False))
            self.assertIsNone(profiles.commit_profile_config(str(self.wt), list(map(str, self.roots)), "danger-full-access", False))
        with mock.patch.object(profiles, "named_profiles_available", return_value=False):
            self.assertIsNone(profiles.commit_profile_config(str(self.wt), list(map(str, self.roots)), "workspace-write", False))

    def test_cli_config_roundtrips_absolute_dot_paths(self):
        import tomllib
        config = self.profile()
        argv = profiles.config_arguments(config)
        restored = {}
        for flag, value in zip(argv[::2], argv[1::2]):
            self.assertEqual(flag, "-c")
            restored.update(tomllib.loads(value))
        self.assertEqual(restored, config)

    def test_native_commit_and_protected_paths(self):
        if sys.platform != "linux" or not shutil.which("codex") or not profiles.named_profiles_available():
            self.skipTest("native Codex named-permissions sandbox unavailable")
        probe = self.base / "probe.py"
        probe.write_text('''from pathlib import Path
import subprocess,sys
wt,repo=map(Path,sys.argv[1:])
(wt/'source.txt').write_text('worker edit\\n')
subprocess.run(['git','add','source.txt'],cwd=wt,check=True)
subprocess.run(['git','commit','-qm','worker commit'],cwd=wt,check=True)
for path in [repo/'.git/config',repo/'.git/hooks/pre-commit',repo/'source.txt',wt/'.codex/config.toml']:
    try: path.write_text('unsafe')
    except OSError: pass
    else: raise AssertionError('unexpected write: '+str(path))
''')
        (self.wt / ".codex").mkdir()
        (self.wt / ".codex/config.toml").write_text("# protected\n")
        config_bytes = (self.repo / ".git/config").read_bytes()
        result = subprocess.run(["codex", "sandbox", "-P", profiles.PROFILE_NAME,
            "-C", str(self.wt), *profiles.config_arguments(self.profile()), "--",
            "python3", str(probe), str(self.wt), str(self.repo)], env=self.env,
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "worker commit")
        self.assertEqual((self.repo / ".git/config").read_bytes(), config_bytes)
        self.assertFalse((self.repo / ".git/hooks/pre-commit").exists())
        self.assertEqual((self.repo / "source.txt").read_text(), "base\n")
        self.assertEqual((self.wt / ".codex/config.toml").read_text(), "# protected\n")
        self.assertFalse((self.wt / ".dispatch").exists())

    def test_native_app_server_uses_same_profile_without_provider(self):
        if sys.platform != "linux" or not shutil.which("codex") or not profiles.named_profiles_available():
            self.skipTest("native Codex named-permissions sandbox unavailable")
        spec = importlib.util.spec_from_file_location("c4_supervisor", Path(__file__).with_name("codex-app-server-supervisor.py"))
        supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supervisor)
        # Existing user sandbox configuration must not defeat the invocation
        # profile. This is our private fixture config, never the real config.
        (Path(self.env["CODEX_HOME"]) / "config.toml").write_text('sandbox_mode="workspace-write"\n')
        server = supervisor.AppServer(["codex", "app-server", "--listen", "stdio://",
                                      *profiles.config_arguments(self.profile())], str(self.wt), self.env)
        try:
            server.request("initialize", {"clientInfo": {"name": "hearting-fixture", "version": "1"},
                                          "capabilities": {"experimentalApi": True}})
            server.notification("initialized")
            thread = server.request("thread/start", {"cwd": str(self.wt), "ephemeral": True,
                                                      "approvalPolicy": "never"})
            self.assertEqual(thread["activePermissionProfile"]["id"], profiles.PROFILE_NAME)
            code = "from pathlib import Path; import subprocess; Path('source.txt').write_text('rpc edit\\n'); subprocess.run(['git','add','source.txt'],check=True); subprocess.run(['git','commit','-qm','rpc commit'],check=True)"
            result = server.request("command/exec", {"cwd": str(self.wt), "command": ["python3", "-c", code]})
            self.assertEqual(result["exitCode"], 0, result["stderr"])
            self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "rpc commit")
        finally:
            server.close()

    def test_turn_does_not_replace_active_profile_with_legacy_policy(self):
        spec = importlib.util.spec_from_file_location("c4_supervisor_turn", Path(__file__).with_name("codex-app-server-supervisor.py"))
        supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supervisor)
        class Captured(Exception): pass
        server = mock.Mock()
        server.request.side_effect = Captured
        args = argparse.Namespace(worktree=str(self.wt), approval="never", model=None,
            reasoning=None, sandbox="workspace-write", network_access=False,
            writable_root=list(map(str, self.roots)), native_permission_profile=self.profile())
        with self.assertRaises(Captured):
            supervisor.run_turn(server, thread_id="fixture", prompt="fixture", args=args)
        self.assertNotIn("sandboxPolicy", server.request.call_args.args[1])
        args.native_permission_profile = None
        with self.assertRaises(Captured):
            supervisor.run_turn(server, thread_id="fixture", prompt="fixture", args=args)
        self.assertEqual(server.request.call_args.args[1]["sandboxPolicy"]["type"], "workspaceWrite")


if __name__ == "__main__":
    unittest.main()
