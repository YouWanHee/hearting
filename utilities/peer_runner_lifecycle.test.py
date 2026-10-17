#!/usr/bin/env python3
"""Managed service provenance through the existing lifecycle callbacks."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import fcntl
import hashlib
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
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

        mismatch_env = self._environment(self.b)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, mismatch_env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as mismatch:
            self.assertTrue(obligations.ensure_normal_runner(self.state))
            mismatch_argv = mismatch.call_args.args[0]
            self.assertEqual(Path(mismatch_argv[1]), self.a / "utilities/peer-steward.py")
            self.assertNotIn("--service-current", mismatch_argv)

        foreign_data = self.base / "foreign-data"
        foreign_releases = foreign_data / "releases"
        foreign_releases.mkdir(parents=True)
        foreign = foreign_releases / "Foreign"
        shutil.copytree(self.b, foreign)
        (foreign_data / "current").symlink_to(foreign, target_is_directory=True)
        foreign_env = self._environment(foreign)
        foreign_env["HARNESS_DATA_ROOT"] = str(foreign_data)
        with mock.patch.object(obligations, "__file__",
                               str(foreign / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, foreign_env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as other_install:
            self.assertTrue(obligations.ensure_normal_runner(self.state))
            foreign_argv = other_install.call_args.args[0]
            self.assertEqual(Path(foreign_argv[1]), foreign / "utilities/peer-steward.py")
            self.assertNotIn("--service-current", foreign_argv)

        lock_path.write_text("not-json", encoding="utf-8")
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(obligations.subprocess, "Popen") as malformed:
            self.assertTrue(obligations.ensure_normal_runner(self.state))
            malformed_argv = malformed.call_args.args[0]
            self.assertEqual(Path(malformed_argv[1]), self.a / "utilities/peer-steward.py")
            self.assertNotIn("--service-current", malformed_argv)
        self.assertEqual(lock_path.read_text(encoding="utf-8"), "not-json")

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

    def test_service_script_uses_only_the_verified_lifecycle_binding(self):
        self._point_current(self.b)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self._environment(self.a), clear=True):
            binding = obligations.managed_service_binding(self.a)
            script, environment, observed_binding = obligations.service_script(
                "utilities/peer-steward.py", binding)
            self.assertEqual(script, self.b / "utilities/peer-steward.py")
            self.assertEqual(environment["AGENT_HOME"], str(self.b))
            self.assertEqual(observed_binding, binding)

            fixed_script, fixed_environment, fixed_binding = obligations.service_script(
                "utilities/peer-steward.py")
            self.assertEqual(fixed_script, self.a / "utilities/peer-steward.py")
            self.assertIsNone(fixed_environment)
            self.assertIsNone(fixed_binding)

    def test_activation_reconnect_passes_binding_to_watch_and_batch_families(self):
        from types import SimpleNamespace

        self._point_current(self.b)
        with mock.patch.object(obligations, "__file__",
                               str(self.a / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, self._environment(self.a), clear=True), \
             mock.patch.object(peer_steward, "_watch_reconciliation_needed", return_value=True), \
             mock.patch.object(peer_steward, "_ensure_watch_observers") as ensure_watches, \
             mock.patch.object(peer_steward.dispatch_batch_obligations,
                               "ensure_observers") as ensure_batches, \
             mock.patch.object(obligations.subprocess, "Popen"):
            self.assertEqual(peer_steward.cmd_ensure_obligations(
                SimpleNamespace(lifecycle_root=str(self.a))), 0)
        ensure_watches.assert_called_once_with(service_current=str(self.current))
        ensure_batches.assert_called_once_with(service_current=str(self.current))

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_takeover_signals_only_an_exact_same_install_observer_holding_its_duty_lock(self):
        self._point_current(self.b)
        script = self.a / "utilities/peer-steward.py"
        ready = self.base / "legacy-observer-ready"
        script.write_text(
            "import fcntl, os, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(os.environ['FIXTURE_READY'], 'w').write('ready')\n"
            "time.sleep(30)\n", encoding="utf-8")
        lock_path = self.state / "watch.lease"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        required = ("__watch-run", "--watch-id", "watch-exact",
                    "--target", "fixture-target", "--server", "default",
                    "--expected-harness", "codex",
                    "--expected-session-id", "sid-watch")
        command = [sys.executable, str(script), *required, "--lock-fd", str(fd)]
        env = dict(self._environment(self.a), FIXTURE_READY=str(ready))
        process = subprocess.Popen(command, pass_fds=(fd,), env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        os.close(fd)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "legacy observer did not acquire its duty lock")
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self._environment(self.a), clear=True):
                binding = obligations.managed_service_binding(self.a)
                with mock.patch.object(os, "pidfd_open", None):
                    self.assertFalse(obligations.replace_managed_observer(
                        process.pid, obligations._proc_start_identity(process.pid), lock_path,
                        "utilities/peer-steward.py", required, binding,
                        lock_fd_option="--lock-fd", root=self.a))
                with mock.patch.object(signal, "pidfd_send_signal", None):
                    self.assertFalse(obligations.replace_managed_observer(
                        process.pid, obligations._proc_start_identity(process.pid), lock_path,
                        "utilities/peer-steward.py", required, binding,
                        lock_fd_option="--lock-fd", root=self.a))
                self.assertIsNone(process.poll(), "unsupported pidfd must preserve the live duty")
                result = obligations.replace_managed_observer(
                    process.pid, obligations._proc_start_identity(process.pid), lock_path,
                    "utilities/peer-steward.py", required, binding,
                    lock_fd_option="--lock-fd", root=self.a)
            self.assertEqual(result, "replaced")
            self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_lifecycle_replaces_exact_legacy_runner_and_starts_current_under_same_lock(self):
        self._point_current(self.b)
        store = obligations.ObligationStore(self.state)
        duty = store.create("legacy-runner-duty", "message", {"fixture": "legacy"},
                            {"ref": "a" * 32})
        script = self.a / "utilities/peer-steward.py"
        ready = self.base / "legacy-runner-ready"
        script.write_text(
            "import fcntl, os, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(os.environ['FIXTURE_READY'], 'w').write('ready')\n"
            "time.sleep(30)\n", encoding="utf-8")
        lock_path = store.root / obligations.RUNNER_LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        state_root = str(store.root.parents[1])
        process = subprocess.Popen(
            [sys.executable, str(script), "__obligation-runner", "--lock-fd", str(fd),
             "--state-root", state_root], pass_fds=(fd,),
            env=dict(self._environment(self.a), FIXTURE_READY=str(ready)),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(fd)
        launched = {}

        def capture_launch(*args, **kwargs):
            lock_fd = kwargs["pass_fds"][0]
            launched["lock_path"] = Path(os.path.realpath(f"/proc/self/fd/{lock_fd}"))
            return SimpleNamespace(poll=lambda: None)

        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "legacy runner did not acquire runner-current.lock")
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ, self._environment(self.a), clear=True), \
                 mock.patch.object(obligations, "_RUNNERS", []), \
                 mock.patch.object(obligations.subprocess, "Popen",
                                   side_effect=capture_launch) as launch:
                self.assertTrue(obligations.ensure_normal_runner(
                    self.state, lifecycle_root=self.a, reconciliation_required=True))
            self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
            argv = launch.call_args.args[0]
            self.assertEqual(Path(argv[1]), self.b / "utilities/peer-steward.py")
            self.assertEqual(argv[-2:], ["--service-current", str(self.current)])
            self.assertEqual(launch.call_args.kwargs["env"]["AGENT_HOME"], str(self.b))
            self.assertEqual(launched["lock_path"], lock_path.resolve())
            self.assertEqual(store.get(duty["id"]), duty)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_update_takes_over_live_batch_and_old_caller_cannot_duplicate_its_lease(self):
        import dispatch_batch_obligations as batch

        self._point_current(self.b)
        self.jobs.parent.mkdir(parents=True, exist_ok=True)
        self.jobs.write_text(
            "t\tdone\t/repo\t/wt\tchild\tattempt_schema_version=2,attempt_id=att-child,"
            "parent_sid=sid-parent,dispatch_depth=1,execution_surface=registered-headless,"
            "registered_worker=1,launch_claimed=1\n", encoding="utf-8")
        args = SimpleNamespace(jobs=self.jobs, sealed_batch_id="sealed-fixture",
                               parent_attempt_id="att-parent", parent_session_id=None,
                               attempt_id=["att-child"], control_socket=None,
                               queue_socket=None, thread_id=None, interval=1.0, timeout=2.0)
        duty_id, _created = batch.create(args)
        batch.update(self.jobs, duty_id, state="delivery-pending", reason="observing",
                     outcome={"state": "ready", "children": [{"attempt_id": "att-child"}]})
        before = batch.read(self.jobs, duty_id)
        jobs_digest = __import__("hashlib").sha256(self.jobs.read_bytes()).hexdigest()
        state_root = batch._root(self.jobs)
        lease_path = state_root / (duty_id + ".lease")
        old_ready = self.base / "old-batch-ready"
        current_ready = self.base / "current-batch-ready"
        observer_source = (
            "import fcntl, os, sys, time\n"
            "fd = int(sys.argv[sys.argv.index('--obligation-lock-fd') + 1])\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(os.environ['FIXTURE_READY'], 'w').write('ready')\n"
            "time.sleep(30)\n")
        old_script = self.a / "utilities/codex-managed-completion.py"
        current_script = self.b / "utilities/codex-managed-completion.py"
        old_script.write_text(observer_source, encoding="utf-8")
        current_script.write_text(
            "import os, sys, time\n"
            "open(os.environ['FIXTURE_READY'], 'w').write('ready')\n"
            "time.sleep(30)\n", encoding="utf-8")
        fd = os.open(lease_path, os.O_CREAT | os.O_RDWR, 0o600)
        old_command = [sys.executable, str(old_script), "--jobs", str(self.jobs),
                       "--resume-obligation", duty_id, "--obligation-lock-fd", str(fd)]
        old_environment = dict(self._environment(self.a), FIXTURE_READY=str(old_ready))
        old_process = subprocess.Popen(
            old_command, pass_fds=(fd,), env=old_environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(fd)
        current_process = None
        real_popen = subprocess.Popen
        captured = {}

        def launch_current(*command_args, **kwargs):
            command = command_args[0]
            captured["command"] = list(command)
            captured["environment"] = dict(kwargs["env"])
            captured["lease_path"] = Path(os.path.realpath(
                f"/proc/self/fd/{kwargs['pass_fds'][0]}"))
            nonlocal current_process
            current_process = real_popen(
                command, **kwargs)
            return current_process

        try:
            deadline = time.monotonic() + 5
            while not old_ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(old_ready.exists(), "legacy batch observer did not acquire its lease")
            batch.update(self.jobs, duty_id, observer={
                "pid": old_process.pid, "pid_start": batch._pid_start(old_process.pid)})
            before = batch.read(self.jobs, duty_id)
            with mock.patch.object(obligations, "__file__",
                                   str(self.a / "utilities/peer_obligations.py")), \
                 mock.patch.dict(os.environ,
                                 dict(self._environment(self.a), FIXTURE_READY=str(current_ready)),
                                 clear=True), \
                 mock.patch.object(obligations, "_RUNNERS", []), \
                 mock.patch.object(batch.subprocess, "Popen", side_effect=launch_current):
                binding = obligations.managed_service_binding(self.a)
                self.assertEqual(batch.ensure_observers(
                    self.jobs, service_current=binding), 1)
            self.assertEqual(old_process.wait(timeout=5), -signal.SIGTERM)
            deadline = time.monotonic() + 5
            while not current_ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(current_ready.exists(), "current batch observer did not acquire its lease")
            self.assertEqual(Path(captured["command"][1]), current_script)
            self.assertEqual(captured["command"][-2:], ["--service-current", str(self.current)])
            self.assertEqual(captured["environment"]["AGENT_HOME"], str(self.b))
            self.assertEqual(captured["lease_path"], lease_path.resolve())

            # The old caller's existing lease-first path cannot spawn a sibling
            # while the current observer owns the same duty.
            def old_caller_ensure():
                inherited = batch.acquire(self.jobs, duty_id)
                if inherited is None:
                    return 0
                try:
                    os.set_inheritable(inherited, True)
                    subprocess.Popen(
                        [sys.executable, str(old_script), "--jobs", str(self.jobs),
                         "--resume-obligation", duty_id,
                         "--obligation-lock-fd", str(inherited)],
                        pass_fds=(inherited,))
                    return 1
                finally:
                    os.close(inherited)

            with mock.patch.object(batch.subprocess, "Popen") as old_launch:
                self.assertEqual(old_caller_ensure(), 0)
            old_launch.assert_not_called()
            after = batch.read(self.jobs, duty_id)
            for field in ("identity", "arguments", "outcome", "state", "delivery"):
                self.assertEqual(after[field], before[field], field)
            self.assertEqual(hashlib.sha256(self.jobs.read_bytes()).hexdigest(), jobs_digest)
        finally:
            if current_process is not None and current_process.poll() is None:
                current_process.terminate()
            if current_process is not None:
                current_process.wait(timeout=5)
            if old_process.poll() is None:
                old_process.terminate()
            old_process.wait(timeout=5)

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
