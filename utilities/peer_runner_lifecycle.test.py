#!/usr/bin/env python3
"""Managed service provenance through the existing lifecycle callbacks."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools" / "install"))

import peer_obligations as obligations  # noqa: E402
import runtime_activation  # noqa: E402

_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "peer_steward_under_runner_lifecycle_test", ROOT / "utilities/peer-steward.py")
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)


class LifecycleBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.data = self.base / "data"
        self.releases = self.data / "releases"
        self.state = self.base / "state"
        self.jobs = self.base / "dispatch" / "jobs.log"
        self.external_python = self.base / "external-python"
        for path in (self.home, self.releases, self.state, self.external_python):
            path.mkdir(parents=True, exist_ok=True)
        self.a = self._release("A")
        self.b = self._release("B")
        self.current = self.data / "current"
        self._point_current(self.a)
        self.env = self._environment(self.a)
        obligations._RUNNERS.clear()
        self.addCleanup(obligations._RUNNERS.clear)

    def _release(self, version):
        root = self.releases / version
        for relative in ("core", "utilities", "tools/install", "tools/fleet",
                         "hooks", "adapters/codex/hooks", "adapters/opencode/plugins"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
        (root / "harness-manifest.json").write_text("{}\n", encoding="utf-8")
        (root / "core/CORE.md").write_text("fixture\n", encoding="utf-8")
        for relative in ("utilities/peer_obligations.py", "utilities/peer-steward.py",
                         "tools/install/distribution.py", "hooks/herdr-session-projection.py",
                         "adapters/codex/hooks/herdr_session_projection.py",
                         "adapters/opencode/plugins/hearting-guards.js"):
            shutil.copy2(ROOT / relative, root / relative)
        (root / "utilities/peer-message.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def peer_state_root():\n"
            "    return Path(os.environ['AGENT_PEER_LEDGER_ROOT'])\n",
            encoding="utf-8")
        (root / "tools/fleet/herdr_projection.py").write_text(
            "def project(*args, **kwargs):\n    return True\n", encoding="utf-8")
        return root

    def _environment(self, code_root):
        return {
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.base / "xdg-data"),
            "XDG_STATE_HOME": str(self.base / "xdg-state"),
            "HARNESS_DATA_ROOT": str(self.data),
            "AGENT_PEER_LEDGER_ROOT": str(self.state),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_HOME": str(code_root),
            "HERDR_PANE_ID": "fixture-pane",
            "AGENT_RUNTIME_ROOT": str(self.a),
            "PYTHONPATH": os.pathsep.join((str(self.a / "utilities"), str(self.external_python))),
            "PATH": os.environ.get("PATH", ""),
        }

    def _point_current(self, target):
        self.current.unlink(missing_ok=True)
        self.current.symlink_to(target, target_is_directory=True)

    def _load_python_callback(self, name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _import_with_tools(self, root, name, path):
        saved = {key: sys.modules.get(key) for key in ("fleet", "fleet.herdr_projection")}
        sys.modules.pop("fleet", None)
        sys.modules.pop("fleet.herdr_projection", None)
        sys.path.insert(0, str(root / "tools"))
        try:
            return self._load_python_callback(name, path)
        finally:
            sys.path.remove(str(root / "tools"))
            sys.modules.pop("fleet", None)
            sys.modules.pop("fleet.herdr_projection", None)
            for key, value in saved.items():
                if value is not None:
                    sys.modules[key] = value

    def _claude_callback(self):
        module = self._import_with_tools(
            self.a, "fixture_claude_projection", self.a / "hooks/herdr-session-projection.py")
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(subprocess, "Popen") as launch, \
             mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps({
                 "session_id": "sid-a", "hook_event_name": "SessionStart"}))):
            self.assertEqual(module.main(), 0)
        return launch.call_args.args[0], launch.call_args.kwargs

    def _codex_callback(self):
        module = self._import_with_tools(
            self.a, "fixture_codex_projection",
            self.a / "adapters/codex/hooks/herdr_session_projection.py")
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(module.subprocess, "Popen") as launch:
            self.assertTrue(module.project({"hook_event_name": "SessionStart"}, "sid-a"))
        return launch.call_args.args[0], launch.call_args.kwargs

    def _opencode_callback(self):
        if not shutil.which("node"):
            self.skipTest("node is unavailable for the OpenCode callback fixture")
        source = (self.a / "adapters/opencode/plugins/hearting-guards.js").read_text()
        start = source.index("function reconnectPeerObligations() {")
        end = source.index("\n}", start) + 2
        function = source[start:end]
        script = f'''import path from "node:path";
import {{ realpathSync }} from "node:fs";
const root = process.env.FIXTURE_ROOT;
const pluginRoot = root;
const calls = [];
const spawn = (exe, args, options) => {{
  calls.push({{exe, args, options}});
  return {{on() {{ return this }}, unref() {{}}}};
}};
{function}
reconnectPeerObligations();
console.log(JSON.stringify(calls[0]));
'''
        env = dict(self.env, FIXTURE_ROOT=str(self.a))
        run = subprocess.run(["node", "--input-type=module", "-e", script], env=env,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        captured = json.loads(run.stdout)
        return [captured["exe"], *captured["args"]], captured["options"]

    def _activation_callback(self):
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(runtime_activation.subprocess, "Popen") as launch:
            self.assertTrue(runtime_activation.reconnect_peer_obligations(self.b))
        return launch.call_args.args[0], launch.call_args.kwargs

    def _open_duty(self, suffix):
        store = obligations.ObligationStore(self.state)
        store.create("retire-fixture-" + suffix, "retire", {"fixture": suffix},
                     {"target": "unobserved-fixture"})

    def _run_captured_cli(self, command, options, *, expected_root):
        callback_env = options.get("env") or self.env
        code_root = Path(command[1]).resolve().parent.parent
        cli_args = command[2:]
        self.assertEqual(cli_args[:1], ["__ensure-obligations"])
        self.assertIn("--lifecycle-root", cli_args)
        self._open_duty("callback-" + code_root.name)
        original_file = obligations.__file__
        obligations._RUNNERS.clear()
        try:
            with mock.patch.object(obligations, "__file__",
                                   str(code_root / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, callback_env, clear=True), \
                 mock.patch.object(peer_steward, "_ensure_watch_observers"), \
                 mock.patch.object(peer_steward, "_resume_message_obligation"), \
                 mock.patch.object(peer_steward.dispatch_batch_obligations, "ensure_observers"), \
                 mock.patch.object(obligations.subprocess, "Popen") as runner_launch:
                self.assertEqual(peer_steward.main(cli_args), 0)
            argv = runner_launch.call_args.args[0]
            self.assertEqual(Path(argv[1]), expected_root / "utilities/peer-steward.py")
            self.assertEqual(argv[-2:], ["--service-current", str(self.current)])
            child_env = runner_launch.call_args.kwargs["env"]
            self.assertEqual(child_env["AGENT_HOME"], str(expected_root))
            self.assertEqual(child_env["AGENT_DISPATCH_JOBS"], str(self.jobs))
            self.assertEqual(child_env["AGENT_RUNTIME_ROOT"], str(self.a))
            paths = child_env["PYTHONPATH"].split(os.pathsep)
            self.assertIn(str(expected_root / "utilities"), paths)
            self.assertIn(str(self.external_python), paths)
        finally:
            obligations.__file__ = original_file
            obligations._RUNNERS.clear()

    def test_all_lifecycle_callbacks_use_the_same_managed_binding(self):
        self._point_current(self.b)
        with mock.patch.dict(os.environ, self.env, clear=True):
            callbacks = [self._claude_callback(), self._codex_callback(),
                         self._opencode_callback(), self._activation_callback()]
        for command, options in callbacks:
            with self.subTest(callback=command[1]):
                self._run_captured_cli(command, options, expected_root=self.b)

    def test_normal_producer_reuses_locked_binding_but_direct_and_checkout_stay_fixed(self):
        self._point_current(self.b)
        lock_path = obligations.ObligationStore(self.state).root / obligations.RUNNER_LOCK_NAME
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self.env, clear=True):
            self.assertFalse(obligations.ensure_normal_runner(
                self.state, lifecycle_root=str(self.a)))
        original_inode = (lock_path.stat().st_dev, lock_path.stat().st_ino)
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"schema": 1, "install_data_root": str(self.data),
                                   "service_current": str(self.current)})

        store = obligations.ObligationStore(self.state)
        store.create("ordinary-producer", "message", {"fixture": "normal"},
                     {"ref": "a" * 32, "body_digest": "b" * 64})
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertTrue(obligations.ensure_normal_runner(self.state))
            argv = launch.call_args.args[0]
            self.assertEqual(Path(argv[1]), self.b / "utilities/peer-steward.py")
            self.assertEqual(argv[-2:], ["--service-current", str(self.current)])
            child_env = launch.call_args.kwargs["env"]
            self.assertEqual(child_env["AGENT_HOME"], str(self.b))
            self.assertEqual(child_env["AGENT_DISPATCH_JOBS"], str(self.jobs))
        self.assertEqual((lock_path.stat().st_dev, lock_path.stat().st_ino), original_inode)
        self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8")), payload)

        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as direct:
            self.assertTrue(obligations.ensure_runner(self.state))
            direct_argv = direct.call_args.args[0]
            self.assertEqual(Path(direct_argv[1]), self.a / "utilities/peer-steward.py")
            self.assertNotIn("--service-current", direct_argv)

        checkout = self.base / "checkout"
        (checkout / "utilities").mkdir(parents=True)
        (checkout / "utilities/peer-steward.py").touch()
        checkout_env = self._environment(checkout)
        with mock.patch.object(obligations, "__file__",
                               str(checkout / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, checkout_env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as fixed:
            self.assertTrue(obligations.ensure_normal_runner(
                self.state, lifecycle_root=str(checkout)))
            fixed_argv = fixed.call_args.args[0]
            self.assertEqual(Path(fixed_argv[1]), checkout / "utilities/peer-steward.py")
            self.assertNotIn("--service-current", fixed_argv)

    def test_fixed_invocations_checkouts_foreign_installs_and_overrides_stay_fixed(self):
        self._point_current(self.b)
        cases = []
        checkout = self.base / "checkout"
        checkout.mkdir()
        for relative in ("utilities", "tools/install", "core"):
            (checkout / relative).mkdir(parents=True, exist_ok=True)
        (checkout / "core/CORE.md").touch()
        (checkout / "harness-manifest.json").touch()
        for relative in ("utilities/peer_obligations.py", "utilities/peer-steward.py",
                         "tools/install/distribution.py"):
            shutil.copy2(ROOT / relative, checkout / relative)
        (checkout / "utilities/peer-message.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def peer_state_root():\n"
            "    return Path(os.environ['AGENT_PEER_LEDGER_ROOT'])\n",
            encoding="utf-8")
        foreign_data = self.base / "foreign-data"
        foreign = foreign_data / "releases" / "Foreign"
        for relative in ("utilities", "tools/install", "core"):
            (foreign / relative).mkdir(parents=True, exist_ok=True)
        (foreign / "RELEASE_VERSION").touch()
        (foreign / "harness-manifest.json").touch()
        (foreign / "core/CORE.md").touch()
        for relative in ("utilities/peer_obligations.py", "utilities/peer-steward.py",
                         "tools/install/distribution.py"):
            shutil.copy2(ROOT / relative, foreign / relative)
        (foreign / "utilities/peer-message.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def peer_state_root():\n"
            "    return Path(os.environ['AGENT_PEER_LEDGER_ROOT'])\n",
            encoding="utf-8")

        cases.extend((
            ("direct-pin", self.a, self._environment(self.a), ["__ensure-obligations"]),
            ("checkout", checkout, self._environment(checkout),
             ["__ensure-obligations", "--lifecycle-root", str(checkout)]),
            ("foreign-install", foreign, self._environment(foreign),
             ["__ensure-obligations", "--lifecycle-root", str(foreign)]),
            ("override-mismatch", self.a, self._environment(self.b),
             ["__ensure-obligations", "--lifecycle-root", str(self.a)]),
        ))
        for name, code_root, env, cli_args in cases:
            with self.subTest(case=name), \
                 mock.patch.object(obligations, "__file__",
                                   str(code_root / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(peer_steward, "_ensure_watch_observers"), \
                 mock.patch.object(peer_steward, "_resume_message_obligation"), \
                 mock.patch.object(peer_steward.dispatch_batch_obligations, "ensure_observers"), \
                 mock.patch.object(obligations.subprocess, "Popen") as launch:
                self._open_duty(name)
                self.assertEqual(peer_steward.main(cli_args), 0)
                argv = launch.call_args.args[0]
                self.assertEqual(Path(argv[1]), code_root / "utilities/peer-steward.py")
                self.assertNotIn("--service-current", argv)

    def test_broken_pointer_keeps_binding_and_retries_from_the_running_release(self):
        self.current.unlink()
        self.current.symlink_to(self.releases / "missing", target_is_directory=True)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self.env, clear=True):
            binding = obligations.managed_service_binding(self.a)
            self.assertEqual(binding, str(self.current))
            self.assertIsNone(obligations.active_service_root(binding))
            self._open_duty("broken-pointer")
            with mock.patch.object(obligations.subprocess, "Popen") as launch:
                self.assertTrue(obligations.ensure_runner(self.state, service_current=binding))
            argv = launch.call_args.args[0]
            self.assertEqual(Path(argv[1]), self.a / "utilities/peer-steward.py")
            self.assertEqual(argv[-2:], ["--service-current", str(self.current)])
            self._point_current(self.b)
            self.assertEqual(obligations.active_service_root(binding), self.b)

    def test_exec_error_preserves_the_lock_duty_environment_and_retry_binding(self):
        import fcntl

        self._point_current(self.b)
        store = obligations.ObligationStore(self.state)
        duty = store.create("message-exec-error", "message", {"fixture": "exec-error"},
                            {"ref": "c" * 32})
        lock_path = store.root / obligations.RUNNER_LOCK_NAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited = os.get_inheritable(fd)
        try:
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self.env, clear=True), \
                 mock.patch.object(os, "execve", side_effect=OSError("fixture exec failure")):
                before_env = dict(os.environ)
                self.assertFalse(obligations.exec_service_runner(
                    fd, self.state, str(self.current)))
                self.assertEqual(dict(os.environ), before_env)
                self.assertEqual(os.get_inheritable(fd), inherited)
                self.assertEqual(store.get(duty["id"]), duty)
                probe = os.open(lock_path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
                self.assertEqual(obligations.active_service_root(str(self.current)), self.b)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
