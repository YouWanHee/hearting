#!/usr/bin/env python3
"""Native Git protection regression, plus profile/fallback projection checks."""
import argparse
import importlib.util
import json
import queue
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import codex_permission_profile as profiles
from execution_access import AccessContext, build_grant, load_request


def require_native_runtime(testcase):
    reason = None
    if sys.platform != "linux":
        reason = "native Codex permission profiles require Linux"
    elif not shutil.which("codex"):
        reason = "Codex CLI is unavailable"
    elif not profiles.named_profiles_available():
        reason = "Codex CLI does not support named permission profiles"
    if reason:
        testcase.skipTest(reason)


class PermissionProfileTest(unittest.TestCase):
    def setUp(self):
        # Codex's built-in :workspace profile makes /tmp and the launch
        # TMPDIR writable. Keep these sibling fixtures under the checkout so
        # native denial checks measure the named profile's read-only rules.
        checkout_root = Path(__file__).resolve().parents[1]
        self.temp = tempfile.TemporaryDirectory(
            prefix=".hearting-codex-git-", dir=checkout_root)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.wt = self.base / "worktree"
        self.artifact = self.base / "artifacts"
        self.dispatch_state = self.base / "dispatch-state"
        self.agent_home = self.base / "agent-home"
        for path in (self.artifact, self.dispatch_state, self.agent_home):
            path.mkdir(mode=0o700)
        self.git_safety_env = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(self.repo),
            "GIT_CONFIG_KEY_1": "safe.directory",
            "GIT_CONFIG_VALUE_1": str(self.wt),
        }
        self.env = {"PATH": os.environ["PATH"], **self.git_safety_env}
        for key, name in (("HOME", "home"), ("CODEX_HOME", "codex-home"),
                          ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"),
                          ("XDG_RUNTIME_DIR", "runtime"), ("TMPDIR", "tmp"),
                          ("CODEX_APP_SERVER_SOCKET_DIR", "sockets")):
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

    def assert_outside_native_temp_roots(self, path):
        target = Path(path).resolve()
        temp_roots = {Path("/tmp").resolve()}
        if self.env.get("TMPDIR"):
            temp_roots.add(Path(self.env["TMPDIR"]).resolve())
        for root in temp_roots:
            self.assertNotEqual(target, root, f"fixture target is a native writable temp root: {root}")
            self.assertNotIn(root, target.parents,
                             f"fixture target {target} overlaps native writable temp root {root}")

    def profile(self, network=False, *, native=False):
        build = lambda: profiles.commit_profile_config(
            str(self.wt), list(map(str, self.roots)), "workspace-write", network)
        with mock.patch.dict(os.environ, self.git_safety_env):
            if native:
                return build()
            with mock.patch.object(profiles, "named_profiles_available", return_value=True):
                return build()

    def execution_grant(self, *, read_roots=(), network=False):
        request_path = self.base / "execution-access.json"
        request_path.write_text(json.dumps({
            "schema_version": 1,
            "writable_roots": [str(self.wt)],
            "read_roots": [str(path) for path in read_roots],
            "network": {
                "required": network,
                "reason": "native loopback permission observation" if network else "",
                "hosts": [],
            },
            "enforcement_required": "os-sandbox",
            "justification": {str(self.wt): "native permission-profile fixture"},
        }), encoding="utf-8")
        context = AccessContext.build(
            worktree=self.wt, artifact_root=self.artifact,
            dispatch_state_root=self.dispatch_state, agent_home=self.agent_home,
            environ=self.env,
        )
        request = load_request(request_path, context=context)
        return build_grant(
            request, runtime="codex-exec", effective_sandbox="workspace-write",
            default_writable_roots=(self.wt,), network_available=network,
        )

    def test_only_complete_existing_git_grant_uses_profile(self):
        config = self.profile()
        self.assertEqual(config["default_permissions"], profiles.PROFILE_NAME)
        body = config["permissions"][profiles.PROFILE_NAME]
        self.assertEqual(body["extends"], ":workspace")
        fs = body["filesystem"]
        self.assertEqual({path for path, access in fs.items() if access == "write"},
                         {str(self.repo / ".git"), *map(str, self.roots)})
        for name in ("config", "hooks", "info", "HEAD", "index", "worktrees"):
            self.assertEqual(fs[str(self.repo / ".git" / name)], "read")
        for name in ("config.worktree", "modules", "MERGE_HEAD", "sequencer"):
            self.assertNotIn(str(self.repo / ".git" / name), fs)
        self.assertEqual(fs[str(self.repo / ".git/worktrees/worktree")], "write")
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

    def test_owner_turn_profiles_keep_safe_default_and_commit_carveouts(self):
        with mock.patch.object(profiles, "named_profiles_available", return_value=True):
            config = profiles.owner_turn_profile_config(
                str(self.wt), list(map(str, self.roots)), "workspace-write",
            )
        self.assertEqual(config["default_permissions"], profiles.OWNER_OBSERVE_PROFILE_NAME)
        profiles_by_name = config["permissions"]
        self.assertEqual(set(profiles_by_name), {
            profiles.OWNER_OBSERVE_PROFILE_NAME,
            profiles.PROFILE_NAME,
            profiles.OWNER_COMMIT_NO_NETWORK_PROFILE_NAME,
        })
        observe = profiles_by_name[profiles.OWNER_OBSERVE_PROFILE_NAME]
        self.assertEqual(observe["extends"], ":read-only")
        self.assertEqual(observe["filesystem"], {})
        self.assertFalse(observe["network"]["enabled"])
        commit = profiles_by_name[profiles.PROFILE_NAME]
        offline = profiles_by_name[profiles.OWNER_COMMIT_NO_NETWORK_PROFILE_NAME]
        self.assertEqual(commit["filesystem"], offline["filesystem"])
        self.assertTrue(commit["network"]["enabled"])
        self.assertFalse(offline["network"]["enabled"])
        for name in ("config", "hooks", "HEAD", "index", "worktrees"):
            self.assertEqual(commit["filesystem"][str(self.repo / ".git" / name)], "read")
        argv = profiles.config_arguments(config)
        import tomllib
        restored = {}
        for flag, value in zip(argv[::2], argv[1::2]):
            self.assertEqual(flag, "-c")
            fragment = tomllib.loads(value)
            for key, item in fragment.items():
                if isinstance(item, dict):
                    restored.setdefault(key, {}).update(item)
                else:
                    restored[key] = item
        self.assertEqual(restored, config)

    def test_turn_permissions_version_probe_falls_back_for_old_or_unknown_cli(self):
        def version(output, returncode=0):
            return mock.patch.object(profiles.subprocess, "run", return_value=mock.Mock(
                returncode=returncode, stdout=output, stderr=""))

        with version("codex-cli 0.160.0"):
            self.assertTrue(profiles.turn_permissions_available())
        with version("codex-cli 0.159.9"):
            self.assertFalse(profiles.turn_permissions_available())
        with version("Python 3.12.8"):
            self.assertFalse(profiles.turn_permissions_available())
        with version("codex-cli 0.163.0", returncode=1):
            self.assertFalse(profiles.turn_permissions_available())

    def test_existing_optional_metadata_is_carved_out_without_legacy_root_grant(self):
        common = self.repo / ".git"
        (common / "config.worktree").write_text("[core]\n")
        (common / "modules").mkdir()
        fs = self.profile()["permissions"][profiles.PROFILE_NAME]["filesystem"]
        self.assertEqual(fs[str(common / "config.worktree")], "read")
        self.assertEqual(fs[str(common / "modules")], "read")
        self.assertNotIn(str(common / "MERGE_HEAD"), fs)
        self.assertNotIn(str(common), set(map(str, self.roots)))

    def test_native_commit_and_protected_paths(self):
        require_native_runtime(self)
        self.assertEqual(self.repo.parent, self.wt.parent)
        self.assert_outside_native_temp_roots(self.repo / "source.txt")
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

    def test_native_packed_refs_and_sequencer_cleanup(self):
        require_native_runtime(self)
        self.assertEqual(self.repo.parent, self.wt.parent)
        self.assert_outside_native_temp_roots(self.repo / ".git")
        common = self.repo / ".git"
        start = self.git("symbolic-ref", "--short", "HEAD")
        for branch in ("victim", "victim2"):
            self.git("branch", branch)
        self.git("tag", "victim-tag")
        self.git("switch", "-qc", "pickme")
        (self.repo / "picked.txt").write_text("picked\n")
        self.git("add", "picked.txt")
        subprocess.run(["git", "commit", "-qm", "picked"], cwd=self.repo,
                       env={**self.env, "GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z"}, check=True)
        self.git("switch", "-q", start)
        self.git("switch", "-qc", "conflictme")
        (self.repo / "source.txt").write_text("conflict\n")
        self.git("add", "source.txt")
        self.git("commit", "-qm", "conflict")
        self.git("switch", "-q", start)
        self.git("worktree", "add", "-qb", "sibling", str(self.base / "sibling"))
        self.git("pack-refs", "--all")
        victim_ref = "refs/heads/victim2"
        self.assertIn(victim_ref, (common / "packed-refs").read_text())
        # Recreate the former named profile: common Git metadata read-only,
        # with only the existing per-worktree and objects/refs/logs grants.
        old_profile = self.profile()
        old_filesystem = {str(common): "read"}
        old_filesystem.update({str(Path(root).resolve()): "write" for root in self.roots})
        old_profile["permissions"][profiles.PROFILE_NAME]["filesystem"] = old_filesystem
        old_result = subprocess.run(
            ["codex", "sandbox", "-P", profiles.PROFILE_NAME,
             "-C", str(self.wt), *profiles.config_arguments(old_profile), "--",
             "git", "update-ref", "-d", victim_ref],
            env=self.env, text=True, capture_output=True, timeout=30)
        old_output = old_result.stdout + old_result.stderr
        self.assertNotEqual(old_result.returncode, 0, old_output)
        self.assertRegex(
            old_output,
            r"(?is)(packed-refs\.lock.*permission denied|permission denied.*packed-refs\.lock)",
            old_output)
        self.assertIn(victim_ref, self.git("for-each-ref", "--format=%(refname)"))
        self.assertFalse((common / "packed-refs.lock").exists())
        (self.wt / ".codex").mkdir()
        (self.wt / ".codex/config.toml").write_text("# protected\n")
        before = {name: (common / name).read_bytes() for name in ("config", "HEAD", "index")}
        before["sibling_HEAD"] = (common / "worktrees/sibling/HEAD").read_bytes()
        probe = self.base / "packed_probe.py"
        probe.write_text('''import os, subprocess, sys, time
from pathlib import Path
wt, repo = map(Path, sys.argv[1:3])
common = repo / '.git'
def git(*args, code=0):
    p = subprocess.run(['git', *args], cwd=wt, capture_output=True, text=True)
    assert p.returncode == code, (args, p.returncode, p.stdout, p.stderr)
    assert 'packed-refs.lock' not in p.stderr, (args, p.stderr)
    return p.stdout.strip()
def marker(name):
    p = Path(git('rev-parse', '--git-path', name))
    return (p if p.is_absolute() else wt / p).exists()
def blocked(fn):
    try: fn()
    except OSError: return
    raise AssertionError('protected mutation succeeded')
git('pack-refs', '--all')
git('branch', '-D', 'victim')
git('tag', '-d', 'victim-tag')
git('update-ref', '-d', 'refs/heads/victim2')
git('cherry-pick', 'pickme')
assert not marker('CHERRY_PICK_HEAD')
(wt/'after.txt').write_text('after pick\\n')
git('add', 'after.txt')
git('commit', '-qm', 'after pick')
assert not git('log', '-1', '--format=%aI').startswith('2001-')
git('revert', '--no-edit', 'HEAD~1')
assert not marker('REVERT_HEAD')
(wt/'source.txt').write_text('worker side\\n')
git('add', 'source.txt')
git('commit', '-qm', 'worker side')
git('cherry-pick', 'conflictme', code=1)
assert marker('CHERRY_PICK_HEAD')
(wt/'source.txt').write_text('resolved\\n')
git('add', 'source.txt')
git('-c', 'core.editor=true', 'cherry-pick', '--continue')
assert not marker('CHERRY_PICK_HEAD')
(wt/'final.txt').write_text('final\\n')
git('add', 'final.txt')
git('commit', '-qm', 'later ordinary commit')
assert not git('log', '-1', '--format=%aI').startswith('2001-')
blocked(lambda: (common/'config').write_text('unsafe'))
blocked(lambda: (common/'config').unlink())
(common/'config.new').write_text('unsafe')
blocked(lambda: os.replace(common/'config.new', common/'config'))
(common/'config.new').unlink()
blocked(lambda: (common/'hooks/pre-commit').write_text('unsafe'))
blocked(lambda: os.rename(common/'hooks', common/'hooks.moved'))
blocked(lambda: (common/'info/exclude').write_text('unsafe'))
blocked(lambda: (common/'HEAD').write_text('unsafe'))
blocked(lambda: (common/'index').write_bytes(b'unsafe'))
blocked(lambda: (common/'worktrees/sibling/HEAD').write_text('unsafe'))
blocked(lambda: (common/'worktrees/sibling/config.worktree').write_text('unsafe'))
blocked(lambda: (common/'worktrees/evil').mkdir())
blocked(lambda: (repo/'source.txt').write_text('unsafe'))
blocked(lambda: (wt/'.codex/config.toml').write_text('unsafe'))
(wt/'probe-ready').write_text('ready')
time.sleep(4)
''')
        proc = subprocess.Popen(["codex", "sandbox", "-P", profiles.PROFILE_NAME,
            "-C", str(self.wt), *profiles.config_arguments(self.profile()), "--",
            "python3", str(probe), str(self.wt), str(self.repo)], env=self.env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 45
            while not (self.wt / "probe-ready").exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.wt / "probe-ready").exists(), "sandbox exited before probe-ready")
            self.assertEqual(self.git("status", "--porcelain"), "")
            self.assertEqual(self.git("status", "--porcelain", cwd=self.base / "sibling"), "")
            self.assertIsNone(proc.poll(), "sandbox exited before concurrent status checks completed")
            stdout, stderr = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, stdout + stderr)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        for name, original in before.items():
            path = common / ("worktrees/sibling/HEAD" if name == "sibling_HEAD" else name)
            self.assertEqual(path.read_bytes(), original, name)
        for ref in ("refs/heads/victim", "refs/heads/victim2", "refs/tags/victim-tag"):
            self.assertNotIn(ref, self.git("for-each-ref", "--format=%(refname)"))
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "later ordinary commit")
        self.assertEqual((self.repo / "source.txt").read_text(), "base\n")
        self.assertEqual((self.wt / ".codex/config.toml").read_text(), "# protected\n")
        for path in (common, *(common / name for name in ("refs", "objects", "logs", "worktrees/worktree"))):
            self.assertFalse({".agents", ".aws", ".codex", ".git"} & {p.name for p in path.iterdir()})
        self.assertFalse((common / "packed-refs.lock").exists())

    def test_native_app_server_uses_same_profile_without_provider(self):
        require_native_runtime(self)
        spec = importlib.util.spec_from_file_location("c4_supervisor", Path(__file__).with_name("codex-app-server-supervisor.py"))
        supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supervisor)
        with mock.patch.object(profiles, "named_profiles_available", return_value=True):
            owner_profiles = profiles.owner_turn_profile_config(
                str(self.wt), list(map(str, self.roots)), "workspace-write",
            )
        self.assertIsNotNone(owner_profiles)
        # Existing user sandbox configuration must not defeat the invocation
        # profile. This is our private fixture config, never the real config.
        (Path(self.env["CODEX_HOME"]) / "config.toml").write_text('sandbox_mode="workspace-write"\n')
        server = supervisor.AppServer(["codex", "app-server", "--listen", "stdio://",
                                      *profiles.config_arguments(owner_profiles)], str(self.wt), self.env)
        try:
            server.request("initialize", {"clientInfo": {"name": "hearting-fixture", "version": "1"},
                                          "capabilities": {"experimentalApi": True}})
            server.notification("initialized")
            thread = server.request("thread/start", {"cwd": str(self.wt), "ephemeral": True,
                                                      "approvalPolicy": "never"})
            thread_id = thread["thread"]["id"]
            self.assertEqual(thread["activePermissionProfile"]["id"],
                             profiles.OWNER_OBSERVE_PROFILE_NAME)
            denied_path = self.wt / "read-only-denied.txt"
            denied_code = f"from pathlib import Path; Path({str(denied_path)!r}).write_text('denied')"
            denied = server.request("command/exec", {
                "cwd": str(self.wt), "command": ["python3", "-c", denied_code],
            })
            self.assertNotEqual(denied["exitCode"], 0, denied["stderr"])
            self.assertFalse(denied_path.exists())

            def select_profile(profile_name):
                server.request("thread/settings/update", {
                    "threadId": thread_id, "permissions": profile_name,
                })
                deadline = time.monotonic() + 10
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.fail("Codex did not acknowledge the named profile update")
                    if server.pending:
                        event = server.pending.pop(0)
                    else:
                        try:
                            line = server.messages.get(timeout=remaining)
                        except queue.Empty:
                            self.fail("Codex did not acknowledge the named profile update")
                        self.assertTrue(line, "Codex closed before the profile update")
                        event = json.loads(line)
                    params = event.get("params", {})
                    if (event.get("method") == "thread/settings/updated"
                            and params.get("threadId") == thread_id):
                        active = params["threadSettings"]["activePermissionProfile"] or {}
                        self.assertEqual(active.get("id"), profile_name)
                        return profile_name

            selected_profile = select_profile(profiles.PROFILE_NAME)
            code = "from pathlib import Path; import subprocess; Path('source.txt').write_text('rpc edit\\n'); subprocess.run(['git','add','source.txt'],check=True); subprocess.run(['git','commit','-qm','rpc commit'],check=True)"
            # command/exec is sessionless: explicitly use the profile that the
            # real thread-settings notification acknowledged above.
            result = server.request("command/exec", {
                "cwd": str(self.wt), "command": ["python3", "-c", code],
                "permissionProfile": selected_profile,
            })
            self.assertEqual(result["exitCode"], 0, result["stderr"])
            self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.wt), "rpc commit")
            selected_profile = select_profile(profiles.OWNER_OBSERVE_PROFILE_NAME)
            denied = server.request("command/exec", {
                "cwd": str(self.wt), "command": ["python3", "-c", denied_code],
                "permissionProfile": selected_profile,
            })
            self.assertNotEqual(denied["exitCode"], 0, denied["stderr"])
            self.assertFalse(denied_path.exists())
        finally:
            server.close()

    def test_native_workspace_write_and_granted_external_read_root(self):
        require_native_runtime(self)
        readonly = self.base / "readonly-data"
        self.assertEqual(readonly.parent, self.wt.parent)
        self.assert_outside_native_temp_roots(readonly)
        readonly.mkdir(mode=0o700)
        (readonly / "visible.txt").write_text("read granted\n", encoding="utf-8")
        grant = self.execution_grant(read_roots=(readonly,))
        self.assertEqual(grant.read_roots, (readonly.resolve(),))
        self.assertEqual(grant.unwritable_read_roots, (readonly.resolve(),))
        self.assertEqual(grant.read_enforcement, "os-sandbox")

        profile = self.profile(native=True)
        self.assertEqual(profile["permissions"][profiles.PROFILE_NAME]["network"], {"enabled": False})
        probe = '''from pathlib import Path
import sys
readonly, worktree = map(Path, sys.argv[1:])
assert (readonly / "visible.txt").read_text() == "read granted\\n"
(worktree / "native-task-write.txt").write_text("workspace write allowed\\n")
try:
    (readonly / "must-not-write.txt").write_text("forbidden\\n")
except OSError:
    pass
else:
    raise AssertionError("native sandbox allowed a write to the granted read-only root")
'''
        result = subprocess.run(
            ["codex", "sandbox", "-P", profiles.PROFILE_NAME, "-C", str(self.wt),
             *profiles.config_arguments(profile), "--", "python3", "-c", probe,
             str(readonly), str(self.wt)],
            env=self.env, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.wt / "native-task-write.txt").read_text(), "workspace write allowed\n")
        self.assertFalse((readonly / "must-not-write.txt").exists())

    def test_native_network_profile_allows_and_denies_loopback(self):
        require_native_runtime(self)

        def observe_network(allowed):
            profile = self.profile(network=allowed, native=True)
            grant = self.execution_grant(network=allowed)
            if allowed:
                self.assertEqual(grant.network, "enforced")
            else:
                self.assertEqual(grant.network, "not-requested")

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.25)
            port = listener.getsockname()[1]
            received = []
            server_errors = []
            stop_server = threading.Event()

            def serve_once():
                while not stop_server.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        if not stop_server.is_set():
                            server_errors.append(str(exc))
                        return
                    with connection:
                        connection.settimeout(5)
                        try:
                            connection.sendall(b"hearting-loopback-ok")
                            if connection.recv(16) != b"ack":
                                server_errors.append("client acknowledgement missing")
                            else:
                                received.append(True)
                        except OSError as exc:
                            server_errors.append(str(exc))
                    return

            server_thread = threading.Thread(target=serve_once, name="codex-loopback-fixture")
            server_thread.start()
            probe = '''import errno, json, socket, sys
port = int(sys.argv[1])
denied_errnos = {
    errno.EACCES, errno.EPERM, errno.ECONNREFUSED,
    errno.ENETUNREACH, errno.EHOSTUNREACH,
}
try:
    connection = socket.create_connection(("127.0.0.1", port), timeout=15)
except OSError as exc:
    if not isinstance(exc, TimeoutError) and exc.errno not in denied_errnos:
        raise
    reason = "ETIMEDOUT" if isinstance(exc, TimeoutError) else errno.errorcode[exc.errno]
    print("HEARTING_NETWORK_RESULT=" + json.dumps(
        {"outcome": "denied", "reason": reason, "errno": exc.errno}), flush=True)
else:
    with connection:
        connection.settimeout(15)
        if connection.recv(64) != b"hearting-loopback-ok":
            raise AssertionError("loopback response mismatch")
        connection.sendall(b"ack")
    print("HEARTING_NETWORK_RESULT=" + json.dumps(
        {"outcome": "connected", "handshake": "ok"}), flush=True)
'''
            result = None
            try:
                result = subprocess.run(
                    ["codex", "sandbox", "-P", profiles.PROFILE_NAME, "-C", str(self.wt),
                     *profiles.config_arguments(profile), "--", "python3", "-c", probe,
                     str(port)],
                    env=self.env, text=True, capture_output=True, timeout=120,
                )
            finally:
                stop_server.set()
                server_thread.join(timeout=12)
                thread_stopped = not server_thread.is_alive()
                if thread_stopped:
                    listener.close()
                self.assertTrue(thread_stopped, "loopback fixture thread did not stop")

            self.assertEqual(
                result.returncode, 0,
                "native sandbox command failed before a child connection observation:\n" + result.stderr,
            )
            marker = "HEARTING_NETWORK_RESULT="
            observations = [line[len(marker):] for line in result.stdout.splitlines()
                            if line.startswith(marker)]
            self.assertEqual(
                len(observations), 1,
                "expected one structured child connection result; stdout was:\n" + result.stdout,
            )
            observed = json.loads(observations[0])
            self.assertEqual(server_errors, [])
            if allowed:
                self.assertEqual(observed, {"outcome": "connected", "handshake": "ok"})
                self.assertEqual(received, [True])
            else:
                self.assertEqual(observed.get("outcome"), "denied", observed)
                self.assertIn(observed.get("reason"), {
                    "EACCES", "EPERM", "ECONNREFUSED", "ENETUNREACH",
                    "EHOSTUNREACH", "ETIMEDOUT",
                })
                self.assertEqual(received, [])

        observe_network(True)
        observe_network(False)

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
        self.assertNotIn("approvalPolicy", server.request.call_args.args[1])
        args.native_permission_profile = None
        with self.assertRaises(Captured):
            supervisor.run_turn(server, thread_id="fixture", prompt="fixture", args=args)
        self.assertEqual(server.request.call_args.args[1]["sandboxPolicy"]["type"], "workspaceWrite")


class PrimaryCommitProfileTest(unittest.TestCase):
    """A primary checkout gets a commit-only `.git` profile, never a plain root."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-codex-primary-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.git_dir = self.repo / ".git"
        self.art = self.base / "artifacts"

    def profile(self, *, available=True, primary_commit=True, sandbox="workspace-write", cwd=None):
        with mock.patch.object(profiles, "named_profiles_available", return_value=available):
            return profiles.commit_profile_config(
                str(cwd or self.repo), [str(self.art)], sandbox, False,
                primary_commit=primary_commit)

    def test_commit_target_gets_commit_only_git_profile(self):
        body = self.profile()["permissions"][profiles.PROFILE_NAME]
        self.assertEqual(body["extends"], ":workspace")
        fs = body["filesystem"]
        self.assertEqual(fs[str(self.art)], "write")
        self.assertEqual(fs[str(self.git_dir)], "write")
        self.assertEqual(fs[str(self.git_dir / "config")], "read")
        self.assertEqual(fs[str(self.git_dir / "hooks")], "read")
        self.assertEqual(fs[str(self.git_dir / "info")], "read")

    def test_only_existing_optional_paths_are_protected(self):
        # An absent path would be materialized as an empty placeholder by
        # Codex, which breaks git; `config.worktree` is absent in a fresh repo.
        fs = self.profile()["permissions"][profiles.PROFILE_NAME]["filesystem"]
        self.assertNotIn(str(self.git_dir / "config.worktree"), fs)
        (self.git_dir / "config.worktree").write_text("")
        shutil.rmtree(self.git_dir / "info")
        fs = self.profile()["permissions"][profiles.PROFILE_NAME]["filesystem"]
        self.assertEqual(fs[str(self.git_dir / "config.worktree")], "read")
        self.assertNotIn(str(self.git_dir / "info"), fs)

    def test_non_target_unsupported_and_other_sandboxes_get_nothing(self):
        self.assertIsNone(self.profile(primary_commit=False))
        self.assertIsNone(self.profile(available=False))
        self.assertIsNone(self.profile(sandbox="read-only"))
        self.assertIsNone(self.profile(sandbox="danger-full-access"))
        # The default caller (no keyword) is unchanged for a primary checkout.
        with mock.patch.object(profiles, "named_profiles_available", return_value=True):
            self.assertIsNone(profiles.commit_profile_config(
                str(self.repo), [str(self.art)], "workspace-write", False))

    def test_primary_profile_never_lists_git_as_a_plain_writable_root(self):
        # Unsupported runtimes keep the legacy projection: no profile at all,
        # so the caller never receives a config that mentions the primary .git.
        self.assertIsNone(self.profile(available=False))
        config = self.profile()
        paths = config["permissions"][profiles.PROFILE_NAME]["filesystem"]
        self.assertEqual([p for p, access in paths.items()
                          if access == "write" and Path(p) == self.git_dir], [str(self.git_dir)])
        self.assertTrue(all(access == "read" for p, access in paths.items()
                            if Path(p).parent == self.git_dir))

    def test_native_primary_commit_and_protected_paths(self):
        require_native_runtime(self)
        for key, value in (("user.email", "fixture@example.com"), ("user.name", "Fixture")):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value], check=True)
        (self.repo / "source.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        probe = self.base / "probe.py"
        probe.write_text('''from pathlib import Path
import subprocess,sys
repo=Path(sys.argv[1])
(repo/'source.txt').write_text('owner edit\\n')
subprocess.run(['git','add','source.txt'],cwd=repo,check=True)
subprocess.run(['git','commit','-qm','owner commit'],cwd=repo,check=True)
for path in [repo/'.git/config',repo/'.git/hooks/pre-commit',repo/'.git/info/exclude']:
    try: path.open('a').write('unsafe')
    except OSError: pass
    else: raise AssertionError('unexpected write: '+str(path))
''')
        env = {"PATH": os.environ["PATH"]}
        for key, name in (("HOME", "home"), ("CODEX_HOME", "codex-home"),
                          ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"), ("TMPDIR", "tmp")):
            (self.base / name).mkdir(mode=0o700)
            env[key] = str(self.base / name)
        config_bytes = (self.git_dir / "config").read_bytes()
        result = subprocess.run(["codex", "sandbox", "-P", profiles.PROFILE_NAME,
            "-C", str(self.repo), *profiles.config_arguments(self.profile()), "--",
            "python3", str(probe), str(self.repo)], env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = subprocess.run(["git", "-C", str(self.repo), "log", "-1", "--format=%s"],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(log, "owner commit")
        self.assertEqual((self.git_dir / "config").read_bytes(), config_bytes)
        self.assertFalse((self.git_dir / "hooks" / "pre-commit").exists())


class PrimaryCommitBuilderTest(unittest.TestCase):
    """The wrapper passes the commit-grant target to both profile builders."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "primary_commit_wrapper", root / "adapters/codex/bin/dispatch-headless.py")
        cls.wrapper = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.wrapper
        spec.loader.exec_module(cls.wrapper)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-codex-builder-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.base / "state").mkdir()
        (self.base / "art").mkdir()
        for key in ("AGENT_DISPATCH_JOBS",):
            os.environ.pop(key, None)

    def args(self, delivery, worker_type="owner", **extra):
        values = dict(
            resolved_completion_delivery=delivery, worktree=str(self.repo),
            jobs_path=self.base / "state" / "jobs.log", attempt_id="att-fixture",
            command_attempt_id=None, artifact_root=str(self.base / "art"),
            report_bundle_root=None, owner_route_binding=None, max_continuations=None,
            nested_headless_network=False, dispatch_depth=1, route_id=None,
            agent_home=str(self.base / "home"), worker_type=worker_type, write_scope=None,
            sandbox="workspace-write", launch_lifecycle="detached", parent_harness="codex",
            parent_transport="headless", parent_sandbox="workspace-write",
            resolved_model_settings={"source": "inherit", "context_window": "800000"}, approval="never",
            execution_access_grant=None, state_root=None)
        values.update(extra)
        return argparse.Namespace(**values)

    def build(self, args, *, available=True):
        with mock.patch.object(profiles, "named_profiles_available", return_value=available):
            return self.wrapper.shell_command(args, self.base / "prompt.txt", self.base / "log.jsonl")

    def test_one_shot_primary_owner_and_commit_stage_get_profile_without_git_add_dir(self):
        for args in (self.args("one-shot"),
                     self.args("one-shot", worker_type="stage", commit_expected=True)):
            command = self.build(args)
            self.assertIn("default_permissions=", command)
            self.assertIn("permissions." + profiles.PROFILE_NAME + "=", command)
            self.assertNotIn("--sandbox", command)
            self.assertNotIn(f"--add-dir {self.repo / '.git'} ", command)
            self.assertIn(str(self.repo / ".git") + "/config", command)

    def test_one_shot_no_commit_stage_and_unsupported_runtime_keep_legacy_projection(self):
        for command in (self.build(self.args("one-shot", worker_type="stage", commit_expected=False)),
                        self.build(self.args("one-shot"), available=False)):
            self.assertNotIn("default_permissions=", command)
            self.assertIn("--sandbox workspace-write", command)
            self.assertNotIn(str(self.repo / ".git"), command)

    def test_supervisor_command_carries_commit_target_flag(self):
        for args, flagged in ((self.args("app-server-supervised"), True),
                              (self.args("app-server-supervised", worker_type="stage",
                                         commit_expected=True), True),
                              (self.args("app-server-supervised", worker_type="stage",
                                         commit_expected=False), False)):
            self.assertEqual("--primary-git-commit" in self.build(args).split(), flagged)

    def test_supervisor_passes_flag_to_profile_builder(self):
        spec = importlib.util.spec_from_file_location(
            "primary_commit_supervisor", Path(__file__).with_name("codex-app-server-supervisor.py"))
        supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supervisor)
        seen = []

        class Stop(Exception):
            pass

        def capture(*args, **kwargs):
            seen.append(kwargs.get("primary_commit"))
            raise Stop

        for flag in ([], ["--primary-git-commit"]):
            budget = mock.MagicMock(limit=1, source="fixture", declared_nodes=0,
                                    retry_slots=0, ordinary=1, reserved=0, stall=0)
            with mock.patch.object(supervisor, "commit_profile_config", side_effect=capture), \
                 mock.patch.object(supervisor, "resolve_continuation_budget", return_value=budget), \
                 mock.patch.object(supervisor.sys, "stdin", mock.MagicMock(read=lambda: "prompt")), \
                 mock.patch.object(supervisor, "emit"):
                with self.assertRaises(Stop):
                    supervisor.main(["--worktree", str(self.repo), "--parent-attempt-id", "att-x",
                                     "--sandbox", "workspace-write", "--lease-file",
                                     str(self.base / "lease"), "--jobs", str(self.base / "jobs.log"),
                                     *flag])
        self.assertEqual(seen, [False, True])


if __name__ == "__main__":
    unittest.main()
