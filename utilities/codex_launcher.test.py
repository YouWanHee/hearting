#!/usr/bin/env python3
"""Unit tests for interactive/pass-through Codex launcher routing."""

from __future__ import annotations

import importlib.util
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("codex-launcher.py")
SPEC = importlib.util.spec_from_file_location("codex_launcher_runtime", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "install"))
import codex_launcher as installer_launcher  # noqa: E402
import fixture_env  # noqa: E402


class CodexLauncherRuntimeTest(unittest.TestCase):
    def _state_fixture(self, root: Path, real: Path, *, lock_mode: int | None = 0o600) -> Path:
        home = root / ".codex"
        harness = home / ".harness"
        harness.mkdir(parents=True)
        home.chmod(0o700)
        ingress = harness / "bin" / "codex"
        ingress.parent.mkdir()
        ingress.write_bytes(b"#!/bin/sh\n# protected ingress\n")
        ingress.chmod(0o755)
        (harness / "codex-launcher.json").write_text(json.dumps({
            "schema": 2, "phase": "installed", "real_command": str(real),
            "ingress_path": str(ingress), "wrapper_path": str(ingress),
        }), encoding="utf-8")
        (harness / "codex-launcher.json").chmod(0o600)
        if lock_mode is not None:
            lock = harness / "codex-launcher.lock"
            lock.write_bytes(b"")
            lock.chmod(lock_mode)
        return home

    def test_all_passthrough_commands_preserve_argv_after_vendor_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "vendor" / "codex"
            real.parent.mkdir()
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            home = self._state_fixture(root, real)
            replacement = root / "vendor" / "codex-v2"
            replacement.write_text("#!/bin/sh\n", encoding="utf-8")
            replacement.chmod(0o755)
            real.unlink()
            real.symlink_to(replacement)
            forms = [[name, "--fixture", "value"] for name in sorted(launcher.PASSTHROUGH_COMMANDS)]
            forms += [["login", "--device-auth"], ["logout"], ["update"], ["doctor"],
                      ["--help"], ["--version"], ["--remote", "unix:///private.sock"],
                      ["-h"], ["-V"]]
            for args in forms:
                with self.subTest(args=args), mock.patch.dict(
                    os.environ, {"CODEX_HOME": str(home), "HOME": str(root)}, clear=False
                ), mock.patch.object(launcher.os, "execv") as execv:
                    launcher.sys.argv = ["codex-launcher.py", *args]
                    self.assertIsNone(launcher.main())
                    execv.assert_called_once_with(str(real), [str(real), *args])
                    execv.reset_mock()
            self.assertFalse((home / ".harness" / "managed-sessions").exists())

    def test_interactive_forms_keep_pinned_environment_after_vendor_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._state_fixture(root, root / "vendor" / "codex")
            real = root / "vendor" / "codex"
            real.parent.mkdir(exist_ok=True)
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            active = root / "bundle"
            (active / "core").mkdir(parents=True)
            (active / "core" / "CORE.md").write_text("core\n", encoding="utf-8")
            (active / "utilities").mkdir()
            (active / "utilities" / "codex-managed-entry.py").write_text("# entry\n", encoding="utf-8")
            (home / "hearting").symlink_to(active, target_is_directory=True)
            (home / ".harness" / "activation.json").write_text(json.dumps({
                "schema": 2, "runtime": "codex", "mode": "linked",
                "active_root": str(active), "active_revision": "rev-test",
            }), encoding="utf-8")
            (home / ".harness" / "activation.json").chmod(0o600)
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            (home / "auth.json").chmod(0o600)
            for args in ([], ["resume", "--last"], ["fork"]):
                with self.subTest(args=args), mock.patch.dict(
                    os.environ, {"CODEX_HOME": str(home), "HOME": str(root)}, clear=False
                ), mock.patch.object(launcher.os, "execv") as execv:
                    launcher.sys.argv = ["codex-launcher.py", *args]
                    launcher.main()
                    command = execv.call_args.args[1]
                    self.assertEqual(command[command.index("--codex") + 1], str(real))
                    self.assertEqual(os.environ["AGENT_HOME"], str(active))
                    self.assertEqual(os.environ["AGENT_RUNTIME_IDENTITY"], "linked:rev-test:-")
                    execv.reset_mock()
    def test_only_interactive_surfaces_are_managed(self) -> None:
        managed = (
            [],
            ["hello"],
            ["resume", "--last"],
            ["fork"],
            ["--model", "gpt-test", "resume", "thread-id"],
        )
        passed_through = (
            ["exec", "task"],
            ["--model", "gpt-test", "exec", "task"],
            ["plugin", "list"],
            ["app-server", "--help"],
            ["--help"],
            ["resume", "--help"],
            ["--remote", "unix:///tmp/codex.sock"],
        )
        for args in managed:
            with self.subTest(args=args):
                self.assertTrue(launcher.should_manage(list(args)))
        for args in passed_through:
            with self.subTest(args=args):
                self.assertFalse(launcher.should_manage(list(args)))

    def test_bypass_environment_is_explicit(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_CODEX_LAUNCHER_BYPASS": "1"}):
            self.assertFalse(launcher.should_manage(["resume", "--last"]))

    def test_workspace_honors_global_cd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(launcher.Path, "cwd", return_value=root):
                self.assertEqual(
                    launcher.workspace(["-C", "nested", "resume"]),
                    root / "nested",
                )
                self.assertEqual(
                    launcher.workspace(["--cd=other", "fork"]),
                    root / "other",
                )

    def test_managed_command_uses_private_per_session_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            home.mkdir()
            agent_home = root / "hearting"
            entry = agent_home / "utilities" / "codex-managed-entry.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("# fixture\n", encoding="utf-8")
            real = root / "codex-real"
            real.write_text("# fixture\n", encoding="utf-8")
            command = launcher.managed_command(
                ["resume", "--last"],
                home,
                real,
                {"active_root": agent_home},
            )
            self.assertEqual(command[1], str(entry))
            self.assertEqual(command[command.index("--codex") + 1], str(real))
            state_dir = Path(command[command.index("--state-dir") + 1])
            self.assertEqual(state_dir.parent, home / ".harness" / "managed-sessions")
            self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                command[command.index("--jobs") + 1],
                str(home / ".harness" / "dispatch" / "jobs.log"),
            )
            self.assertEqual(command[-3:], ["--", "resume", "--last"])

    def test_managed_command_preserves_feature_opt_out_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            home.mkdir()
            agent_home = root / "hearting"
            entry = agent_home / "utilities" / "codex-managed-entry.py"
            entry.parent.mkdir(parents=True)
            entry.write_text("# fixture\n", encoding="utf-8")
            real = root / "codex-real"
            real.write_text("# fixture\n", encoding="utf-8")
            original = [
                "-c",
                "features.default_mode_request_user_input=false",
                "resume",
                "--last",
            ]
            command = launcher.managed_command(
                original, home, real, {"active_root": agent_home}
            )
            separator = command.index("--")
            self.assertEqual(command[separator + 1 :], original)

    def test_auth_readiness_preserves_first_login_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertFalse(launcher.managed_auth_ready(home))
            auth = home / "auth.json"
            auth.write_text("{}\n", encoding="utf-8")
            auth.chmod(0o600)
            self.assertTrue(launcher.managed_auth_ready(home))
            auth.chmod(0o644)
            self.assertFalse(launcher.managed_auth_ready(home))

    def test_packaged_runtime_is_resolved_once_across_activation_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            harness = home / ".harness"
            bundles = harness / "bundles"
            bundles.mkdir(parents=True)

            def packaged(name: str) -> Path:
                source = bundles / name / "source"
                (source / "core").mkdir(parents=True)
                (source / "core" / "CORE.md").write_text("core\n", encoding="utf-8")
                entry = source / "utilities" / "codex-managed-entry.py"
                entry.parent.mkdir()
                entry.write_text(f"# {name}\n", encoding="utf-8")
                (source.parent / "bundle.json").write_text(
                    '{"checksum":"sum-%s","source_revision":"rev-%s"}\n'
                    % (name, name),
                    encoding="utf-8",
                )
                return source

            first = packaged("first")
            second = packaged("second")
            projection = home / "hearting"
            projection.symlink_to(first, target_is_directory=True)
            activation = harness / "activation.json"

            def select(name: str, source: Path) -> None:
                activation.write_text(
                    '{"schema":2,"runtime":"codex","mode":"packaged",'
                    '"active_root":"%s","active_revision":"rev-%s",'
                    '"bundle_checksum":"sum-%s"}\n' % (source, name, name),
                    encoding="utf-8",
                )
                activation.chmod(0o600)

            select("first", first)
            binding = launcher.pinned_runtime(home)
            projection.unlink()
            projection.symlink_to(second, target_is_directory=True)
            select("second", second)

            real = root / "codex-real"
            real.write_text("fixture\n", encoding="utf-8")
            command = launcher.managed_command([], home, real, binding)
            self.assertEqual(
                Path(command[1]), first / "utilities" / "codex-managed-entry.py"
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                launcher.export_runtime_binding(binding)
                self.assertEqual(os.environ["AGENT_HOME"], str(first))
                self.assertEqual(os.environ["AGENT_RUNTIME_ROOT"], str(first))
                self.assertEqual(
                    os.environ["AGENT_RUNTIME_IDENTITY"],
                    "packaged:rev-first:sum-first",
                )

    def test_state_rejects_a_wrapper_real_command(self) -> None:
        # Binding to another install's ingress would exec this launcher forever.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            state_dir = home / ".harness"
            state_dir.mkdir(parents=True)
            home.chmod(0o700)
            wrapper = root / "other" / "codex"
            wrapper.parent.mkdir()
            wrapper.write_bytes(
                b"#!/bin/sh\nexec python3 $HOME/.codex/hearting/utilities/"
                b"codex-launcher.py \"$@\"\n# hearting ingress\n"
            )
            wrapper.chmod(0o755)
            state = state_dir / "codex-launcher.json"
            state.write_text(
                '{"schema": 1, "phase": "installed", "real_command": "%s"}' % wrapper,
                encoding="utf-8",
            )
            state.chmod(0o600)
            with self.assertRaises(launcher.LauncherError):
                launcher._state(home)

    def test_reentry_guard_detects_a_circular_binding(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"AGENT_CODEX_LAUNCHER_GUARD_PID": str(os.getpid())},
        ):
            with mock.patch.object(launcher.sys, "argv", ["codex-launcher.py", "--version"]):
                self.assertEqual(launcher.main(), 69)

    def test_private_runtime_home_falls_back_to_global_binding_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_home = root / "private-codex"
            private_home.mkdir()
            default_home = root / ".codex"
            state = default_home / ".harness" / "codex-launcher.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(launcher.Path, "home", return_value=root):
                self.assertEqual(launcher.launcher_state_home(private_home), default_home)

            private_state = private_home / ".harness" / "codex-launcher.json"
            private_state.parent.mkdir()
            private_state.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(launcher.Path, "home", return_value=root):
                self.assertEqual(launcher.launcher_state_home(private_home), private_home)

    def test_reader_never_mutates_state_for_managed_invocations(self) -> None:
        """The alternative framing's write-trace falsifier, without a live home:

        exec/admin/--version must invoke the real CLI with byte-exact argv while
        every mutating filesystem primitive (`os.mkdir`, write-capable `open`,
        `os.chmod`, `os.unlink`, `Path.mkdir`, write-capable `os.fdopen`) raises
        if the reader ever calls it, and `os.open` is only ever asked for a
        read-only, non-creating descriptor.
        """
        forms = [
            ["exec", "task with spaces", "", "--flag=\"quoted\"", "héllo"],
            ["plugin", "list"],
            ["--version"],
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "vendor" / "codex"
            real.parent.mkdir()
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            home = self._state_fixture(root, real)
            lock = home / ".harness" / "codex-launcher.lock"
            before_bytes = lock.read_bytes()
            before_mode = lock.stat().st_mode & 0o777
            observed_flags: list[int] = []
            real_os_open = os.open
            real_path_open = launcher.Path.open
            real_os_fdopen = os.fdopen

            def spy_os_open(path, flags, *args, **kwargs):
                observed_flags.append(flags)
                return real_os_open(path, flags, *args, **kwargs)

            def guarded_path_open(self_path, mode="r", *args, **kwargs):
                if any(char in mode for char in "wax+"):
                    raise AssertionError(f"unexpected write-capable open: {self_path} mode={mode!r}")
                return real_path_open(self_path, mode, *args, **kwargs)

            def guarded_os_fdopen(fd, mode="r", *args, **kwargs):
                if any(char in mode for char in "wax+"):
                    raise AssertionError(f"unexpected write-capable os.fdopen mode={mode!r}")
                return real_os_fdopen(fd, mode, *args, **kwargs)

            for args in forms:
                with self.subTest(args=args), mock.patch.dict(
                    os.environ, {"CODEX_HOME": str(home), "HOME": str(root)}, clear=False
                ), mock.patch.object(launcher.os, "execv") as execv, mock.patch.object(
                    launcher.os, "mkdir", side_effect=AssertionError("unexpected os.mkdir")
                ), mock.patch.object(
                    launcher.os, "chmod", side_effect=AssertionError("unexpected os.chmod")
                ), mock.patch.object(
                    launcher.os, "unlink", side_effect=AssertionError("unexpected os.unlink")
                ), mock.patch.object(
                    launcher.Path, "mkdir", side_effect=AssertionError("unexpected Path.mkdir")
                ), mock.patch.object(
                    launcher.Path, "open", guarded_path_open
                ), mock.patch.object(
                    launcher.os, "fdopen", side_effect=guarded_os_fdopen
                ), mock.patch.object(launcher.os, "open", side_effect=spy_os_open):
                    launcher.sys.argv = ["codex-launcher.py", *args]
                    self.assertIsNone(launcher.main())
                    execv.assert_called_once_with(str(real), [str(real), *args])
            self.assertTrue(observed_flags)
            for flags in observed_flags:
                self.assertFalse(flags & os.O_CREAT)
                self.assertFalse(flags & os.O_WRONLY)
                self.assertFalse(flags & os.O_RDWR)
                self.assertFalse(flags & os.O_TRUNC)
                self.assertFalse(flags & os.O_APPEND)
            self.assertEqual(lock.read_bytes(), before_bytes)
            self.assertEqual(lock.stat().st_mode & 0o777, before_mode)

    def test_private_codex_home_reads_seeded_global_binding_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "vendor" / "codex"
            real.parent.mkdir()
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            global_home = self._state_fixture(root, real)
            private_home = root / "private-codex"
            private_home.mkdir()
            with mock.patch.object(launcher.Path, "home", return_value=root), mock.patch.dict(
                os.environ, {"CODEX_HOME": str(private_home)}, clear=False
            ), mock.patch.object(launcher.os, "execv") as execv:
                launcher.sys.argv = ["codex-launcher.py", "exec", "task"]
                self.assertIsNone(launcher.main())
                execv.assert_called_once_with(str(real), [str(real), "exec", "task"])
            self.assertFalse((private_home / ".harness").exists())
            self.assertFalse((global_home / ".harness" / "managed-sessions").exists())


class LauncherLockReaderTest(unittest.TestCase):
    """Direct coverage of `_launcher_lock`'s safety and rejection surface."""

    def _harness(self, root: Path) -> Path:
        home = root / ".codex"
        harness = home / ".harness"
        harness.mkdir(parents=True)
        home.chmod(0o700)
        return home

    def _lock(self, home: Path, mode: int, *, content: bytes = b"") -> Path:
        lock = home / ".harness" / "codex-launcher.lock"
        lock.write_bytes(content)
        lock.chmod(mode)
        return lock

    def test_accepts_0600_and_0400_seeded_locks(self) -> None:
        for mode in (0o600, 0o400):
            with self.subTest(mode=oct(mode)):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    home = self._harness(root)
                    self._lock(home, mode)
                    handle = launcher._launcher_lock(home)
                    try:
                        self.assertFalse(handle.closed)
                    finally:
                        launcher._unlock(handle)

    def test_rejects_group_or_other_permissions(self) -> None:
        for mode in (0o640, 0o604, 0o660, 0o666, 0o460):
            with self.subTest(mode=oct(mode)):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    home = self._harness(root)
                    self._lock(home, mode)
                    with self.assertRaises(launcher.LauncherError):
                        launcher._launcher_lock(home)

    def test_rejects_wrong_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._harness(root)
            self._lock(home, 0o600)
            with mock.patch.object(launcher.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaises(launcher.LauncherError):
                    launcher._launcher_lock(home)

    def test_rejects_nonregular_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._harness(root)
            lock = home / ".harness" / "codex-launcher.lock"
            os.mkfifo(lock)
            os.chmod(lock, 0o600)
            with self.assertRaises(launcher.LauncherError):
                launcher._launcher_lock(home)

    def test_rejects_unsafe_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            home.mkdir()
            harness = home / ".harness"
            real_harness = root / "elsewhere-harness"
            real_harness.mkdir()
            harness.symlink_to(real_harness, target_is_directory=True)
            with self.assertRaises(launcher.LauncherError):
                launcher._launcher_lock(home)

    def test_rejects_symlink_lock_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._harness(root)
            target = root / "target-lock"
            target.write_bytes(b"original")
            target.chmod(0o600)
            lock = home / ".harness" / "codex-launcher.lock"
            lock.symlink_to(target)
            with self.assertRaises(launcher.LauncherError):
                launcher._launcher_lock(home)
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_rejects_missing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._harness(root)
            with self.assertRaises(launcher.LauncherError):
                launcher._launcher_lock(home)

    def test_missing_lock_makes_main_exit_69_without_exec_or_lock_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "vendor" / "codex"
            real.parent.mkdir()
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            harness = CodexLauncherRuntimeTest()
            home = harness._state_fixture(root, real, lock_mode=None)
            lock = home / ".harness" / "codex-launcher.lock"
            self.assertFalse(lock.exists())
            with mock.patch.dict(
                os.environ, {"CODEX_HOME": str(home), "HOME": str(root)}, clear=False
            ), mock.patch.object(launcher.os, "execv") as execv:
                launcher.sys.argv = ["codex-launcher.py", "exec", "task"]
                self.assertEqual(launcher.main(), 69)
                execv.assert_not_called()
            self.assertFalse(lock.exists())

    def test_transient_pathname_replacement_is_retried_then_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._harness(root)
            path = self._lock(home, 0o600, content=b"stale")

            real_flock = launcher.fcntl.flock
            calls = {"n": 0}

            def replace_then_flock(fd, operation):
                calls["n"] += 1
                if calls["n"] == 1 and operation == launcher.fcntl.LOCK_SH:
                    path.unlink()
                    path.write_bytes(b"successor")
                    path.chmod(0o600)
                return real_flock(fd, operation)

            with mock.patch.object(launcher.fcntl, "flock", side_effect=replace_then_flock):
                handle = launcher._launcher_lock(home)
            try:
                self.assertEqual(handle.read(), b"successor")
            finally:
                launcher._unlock(handle)
            self.assertGreaterEqual(calls["n"], 3)

    def test_persistent_pathname_replacement_exhausts_retries_and_closes_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._harness(root)
            path = self._lock(home, 0o600, content=b"v0")

            real_flock = launcher.fcntl.flock
            generation = {"n": 0}

            def replace_every_time(fd, operation):
                if operation == launcher.fcntl.LOCK_SH:
                    generation["n"] += 1
                    path.unlink()
                    path.write_bytes(f"v{generation['n']}".encode())
                    path.chmod(0o600)
                return real_flock(fd, operation)

            open_fds: list[int] = []
            real_os_open = launcher.os.open

            def spy_open(p, flags, *a, **kw):
                fd = real_os_open(p, flags, *a, **kw)
                open_fds.append(fd)
                return fd

            with mock.patch.object(
                launcher.fcntl, "flock", side_effect=replace_every_time
            ), mock.patch.object(launcher.os, "open", side_effect=spy_open):
                with self.assertRaises(launcher.LauncherError):
                    launcher._launcher_lock(home)
            # One directory descriptor for `.harness` (opened once, held for
            # the whole call) plus one lock open per retry.
            self.assertEqual(len(open_fds), launcher._LOCK_OPEN_RETRIES + 1)
            for fd in open_fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    def test_symlink_replacement_rejects_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._harness(root)
            path = self._lock(home, 0o600, content=b"v0")
            foreign = root / "foreign-lock"
            foreign.write_bytes(b"foreign")
            foreign.chmod(0o600)

            real_flock = launcher.fcntl.flock

            def replace_with_symlink(fd, operation):
                if operation == launcher.fcntl.LOCK_SH:
                    path.unlink()
                    path.symlink_to(foreign)
                return real_flock(fd, operation)

            with mock.patch.object(launcher.fcntl, "flock", side_effect=replace_with_symlink):
                with self.assertRaises(launcher.LauncherError):
                    launcher._launcher_lock(home)
            self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_fcntl_unavailable_is_a_documented_fail_closed_hard_failure(self) -> None:
        """`fcntl` import failure (e.g. an unsupported platform) fails closed
        rather than silently skipping the lock. This is a defensible policy
        for a lock-dependent reader -- no grant or argv behavior changes --
        but the branch had no direct test before this.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._harness(root)
            self._lock(home, 0o600)
            with mock.patch.object(launcher, "fcntl", None):
                with self.assertRaises(launcher.LauncherError):
                    launcher._launcher_lock(home)

    def test_real_concurrent_thread_replaces_lock_during_acquire(self) -> None:
        """A genuine background thread renames the lock file mid-acquire.

        Unlike `test_transient_pathname_replacement_is_retried_then_valid`
        (which replaces the file synchronously inside a mocked `flock`, on
        the same thread), this drives the replacement from an actual second
        thread performing real filesystem operations concurrently, so the
        race is a real concurrent event rather than a single-threaded
        stand-in.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._harness(root)
            path = self._lock(home, 0o600, content=b"v0")

            about_to_flock = threading.Event()
            replaced = threading.Event()
            real_flock = launcher.fcntl.flock

            def wait_then_flock(fd, operation):
                if operation == launcher.fcntl.LOCK_SH:
                    about_to_flock.set()
                    self.assertTrue(replaced.wait(timeout=10.0), "replacer thread never ran")
                return real_flock(fd, operation)

            def replacer() -> None:
                self.assertTrue(about_to_flock.wait(timeout=10.0), "flock never attempted")
                path.unlink()
                path.write_bytes(b"v1-from-thread")
                path.chmod(0o600)
                replaced.set()

            thread = threading.Thread(target=replacer, daemon=True)
            thread.start()
            try:
                with mock.patch.object(launcher.fcntl, "flock", side_effect=wait_then_flock):
                    handle = launcher._launcher_lock(home)
                try:
                    self.assertEqual(handle.read(), b"v1-from-thread")
                finally:
                    launcher._unlock(handle)
            finally:
                thread.join(timeout=10.0)
                self.assertFalse(thread.is_alive(), "replacer thread did not join")

    def test_directory_replacement_does_not_redirect_to_a_foreign_lock(self) -> None:
        """A genuine background thread swaps `.harness` for a replacement
        directory holding a different lock, timed to land only after
        `_open_harness_dirfd` already opened and validated the original
        directory. The held descriptor must keep resolving the lock inside
        the originally-validated directory, proving the directory-level
        replacement window (finding (a)) is closed.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = self._harness(root)
            self._lock(home, 0o600, content=b"genuine")

            dirfd_opened = threading.Event()
            swapped = threading.Event()
            real_open_harness_dirfd = launcher._open_harness_dirfd

            def spy_open_harness_dirfd(home_arg):
                dirfd = real_open_harness_dirfd(home_arg)
                dirfd_opened.set()
                self.assertTrue(swapped.wait(timeout=10.0), "swapper thread never ran")
                return dirfd

            def swapper() -> None:
                self.assertTrue(dirfd_opened.wait(timeout=10.0), "dirfd never opened")
                hijacked = home / ".harness-hijacked"
                (home / ".harness").rename(hijacked)
                replacement = home / ".harness"
                replacement.mkdir()
                foreign = replacement / "codex-launcher.lock"
                foreign.write_bytes(b"foreign")
                foreign.chmod(0o600)
                swapped.set()

            thread = threading.Thread(target=swapper, daemon=True)
            thread.start()
            try:
                with mock.patch.object(
                    launcher, "_open_harness_dirfd", side_effect=spy_open_harness_dirfd
                ):
                    handle = launcher._launcher_lock(home)
                try:
                    self.assertEqual(handle.read(), b"genuine")
                finally:
                    launcher._unlock(handle)
            finally:
                thread.join(timeout=10.0)
                self.assertFalse(thread.is_alive(), "swapper thread did not join")

    def test_directory_swap_never_returns_foreign_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            genuine = root / "vendor" / "genuine-codex"
            foreign = root / "vendor" / "foreign-codex"
            genuine.parent.mkdir()
            for command in (genuine, foreign):
                command.write_text("#!/bin/sh\n", encoding="utf-8")
                command.chmod(0o755)
            home = self._harness(root)
            original = home / ".harness"
            lock = self._lock(home, 0o600)
            ingress = original / "bin" / "codex"
            ingress.parent.mkdir()
            ingress.write_bytes(b"#!/bin/sh\n")
            ingress.chmod(0o755)
            state = original / "codex-launcher.json"
            state.write_bytes(json.dumps({
                "schema": 2, "phase": "installed", "real_command": str(genuine),
                "ingress_path": str(ingress), "wrapper_path": str(ingress),
            }).encode())
            state.chmod(0o600)
            original_lock_identity = (lock.stat().st_dev, lock.stat().st_ino)
            real_state = launcher._state
            swapped = False

            def racy_state(home_arg, *, dirfd=None):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    replacement = home / ".harness-replacement"
                    original.rename(replacement)
                    new_harness = home / ".harness"
                    new_harness.mkdir()
                    new_ingress = new_harness / "codex"
                    new_ingress.write_bytes(b"#!/bin/sh\n")
                    new_ingress.chmod(0o755)
                    (new_harness / "codex-launcher.json").write_bytes(json.dumps({
                        "schema": 2, "phase": "installed", "real_command": str(foreign),
                        "ingress_path": str(new_ingress), "wrapper_path": str(new_ingress),
                    }).encode())
                    (new_harness / "codex-launcher.json").chmod(0o600)
                    os.link(replacement / "codex-launcher.lock", new_harness / "codex-launcher.lock")
                return real_state(home_arg) if dirfd is None else real_state(home_arg, dirfd=dirfd)

            replacement_lock = home / ".harness" / "codex-launcher.lock"
            with mock.patch.object(launcher, "_state", side_effect=racy_state):
                try:
                    value = launcher._read_state_locked(home)
                except launcher.LauncherError:
                    return
            self.assertEqual(str(genuine), value["real_command"])
            self.assertNotEqual(str(foreign), value["real_command"])
            self.assertEqual(original_lock_identity, (replacement_lock.stat().st_dev, replacement_lock.stat().st_ino))

    def test_lock_fdopen_failure_closes_owned_fds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._harness(Path(temporary))
            self._lock(home, 0o600)
            opened: list[int] = []
            real_open = launcher.os.open

            def capture_open(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                opened.append(fd)
                return fd

            with mock.patch.object(launcher.os, "open", side_effect=capture_open), mock.patch.object(
                launcher.os, "fdopen", side_effect=KeyboardInterrupt("injected fdopen failure")
            ):
                with self.assertRaises(KeyboardInterrupt):
                    launcher._launcher_lock(home)
            self.assertGreaterEqual(len(opened), 2)
            for fd in opened:
                with self.assertRaises(OSError) as raised:
                    os.fstat(fd)
                self.assertEqual(errno.EBADF, raised.exception.errno)

    def test_unlock_failure_closes_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = self._harness(Path(temporary))
            self._lock(home, 0o600)
            handle = launcher._launcher_lock(home)
            fd = handle.fileno()
            real_flock = launcher.fcntl.flock

            def fail_unlock(number, operation):
                if operation == launcher.fcntl.LOCK_UN:
                    raise KeyboardInterrupt("injected unlock failure")
                return real_flock(number, operation)

            with mock.patch.object(launcher.fcntl, "flock", side_effect=fail_unlock):
                with self.assertRaises(KeyboardInterrupt):
                    launcher._unlock(handle)
            self.assertTrue(handle.closed)
            with self.assertRaises(OSError) as raised:
                os.fstat(fd)
            self.assertEqual(errno.EBADF, raised.exception.errno)

    def test_state_post_fstat_growth_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "vendor" / "codex"
            real.parent.mkdir()
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            home = CodexLauncherRuntimeTest()._state_fixture(root, real)
            state = home / ".harness" / "codex-launcher.json"
            original_size = state.stat().st_size
            real_fstat = launcher.os.fstat
            grew = False

            def grow_after_stat(fd):
                nonlocal grew
                info = real_fstat(fd)
                try:
                    if Path(os.readlink(f"/proc/self/fd/{fd}")) == state and not grew:
                        with state.open("ab") as stream:
                            stream.write(b" " * (32769 - original_size))
                        grew = True
                except OSError:
                    pass
                return info

            with mock.patch.object(launcher.os, "fstat", side_effect=grow_after_stat):
                with self.assertRaises(launcher.LauncherError):
                    launcher._state(home)
            self.assertTrue(grew)
            self.assertGreater(state.stat().st_size, 32768)

    def test_borrowed_dirfd_survives_launcher_lock_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "vendor" / "codex"
            real.parent.mkdir()
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            home = CodexLauncherRuntimeTest()._state_fixture(root, real)
            borrowed = os.open(home / ".harness", os.O_RDONLY | os.O_DIRECTORY)
            try:
                handle = launcher._launcher_lock(home, dirfd=borrowed)
                launcher._unlock(handle)
                launcher._state(home, dirfd=borrowed)
                os.fstat(borrowed)
            finally:
                os.close(borrowed)

    def test_read_state_locked_retries_when_lock_replaced_during_read(self) -> None:
        """A genuine background thread replaces the lock file after
        `_state` has already read from it but before `_read_state_locked`'s
        post-read identity check runs, proving the acquire-and-read
        acceptance window (finding (b)) is closed by a retry rather than
        trusting a read that raced a live replacement.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "vendor" / "codex"
            real.parent.mkdir()
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            real.chmod(0o755)
            home = root / ".codex"
            harness = home / ".harness"
            harness.mkdir(parents=True)
            home.chmod(0o700)
            ingress = harness / "bin" / "codex"
            ingress.parent.mkdir()
            ingress.write_bytes(b"#!/bin/sh\n")
            ingress.chmod(0o755)
            (harness / "codex-launcher.json").write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "phase": "installed",
                        "real_command": str(real),
                        "ingress_path": str(ingress),
                        "wrapper_path": str(ingress),
                    }
                ),
                encoding="utf-8",
            )
            (harness / "codex-launcher.json").chmod(0o600)
            lock = harness / "codex-launcher.lock"
            lock.write_bytes(b"v0")
            lock.chmod(0o600)

            replaced_once = threading.Event()
            real_state = launcher._state
            call_count = {"n": 0}

            def racy_state(home_arg, *, dirfd=None):
                call_count["n"] += 1
                value = real_state(home_arg) if dirfd is None else real_state(home_arg, dirfd=dirfd)
                if not replaced_once.is_set():
                    replaced = threading.Event()

                    def replacer() -> None:
                        lock.unlink()
                        lock.write_bytes(b"v1")
                        lock.chmod(0o600)
                        replaced.set()

                    thread = threading.Thread(target=replacer, daemon=True)
                    thread.start()
                    self.assertTrue(replaced.wait(timeout=10.0), "replacer thread never ran")
                    thread.join(timeout=10.0)
                    self.assertFalse(thread.is_alive(), "replacer thread did not join")
                    replaced_once.set()
                return value

            with mock.patch.object(launcher, "_state", side_effect=racy_state):
                value = launcher._read_state_locked(home)
            self.assertEqual(str(real), value["real_command"])
            self.assertGreaterEqual(call_count["n"], 2)


class LauncherLockConcurrencyTest(unittest.TestCase):
    """Real installer `_LauncherLock` vs. this reader's `_launcher_lock`.

    Blocking `LOCK_SH` is intentional (see `_launcher_lock`'s docstring): a
    reader behind a held `LOCK_EX` waits rather than failing fast. Every wait
    below is bounded by an explicit timeout and every spawned thread is always
    joined — on the happy path and on a stalled reader/writer alike — so a
    stuck lock fails this test loudly instead of hanging the suite.
    """

    _WATCHDOG_SECONDS = 10.0

    def test_reader_blocks_behind_a_held_exclusive_lock_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            (home / ".harness").mkdir(parents=True)
            home.chmod(0o700)
            path = installer_launcher.lock_path(home)
            path.write_bytes(b"")
            path.chmod(0o600)

            writer_holds = threading.Event()
            release_writer = threading.Event()
            writer_done = threading.Event()

            def hold_writer() -> None:
                lock = installer_launcher._LauncherLock(path)
                lock.__enter__()
                writer_holds.set()
                release_writer.wait(timeout=self._WATCHDOG_SECONDS)
                lock.__exit__(None, None, None)
                writer_done.set()

            writer = threading.Thread(target=hold_writer, daemon=True)
            writer.start()
            try:
                self.assertTrue(
                    writer_holds.wait(timeout=self._WATCHDOG_SECONDS), "writer never acquired LOCK_EX"
                )

                reader_started = threading.Event()
                reader_done = threading.Event()
                reader_result: dict[str, object] = {}

                def read_while_locked() -> None:
                    reader_started.set()
                    try:
                        reader_result["handle"] = launcher._launcher_lock(home)
                    except BaseException as exc:  # noqa: BLE001 - surfaced on the main thread
                        reader_result["error"] = exc
                    finally:
                        reader_done.set()

                reader_thread = threading.Thread(target=read_while_locked, daemon=True)
                reader_thread.start()
                try:
                    self.assertTrue(reader_started.wait(timeout=self._WATCHDOG_SECONDS))
                    # The reader must still be blocked on LOCK_SH: it cannot finish
                    # while the writer holds LOCK_EX.
                    self.assertFalse(reader_done.wait(timeout=0.3))
                    release_writer.set()
                    self.assertTrue(writer_done.wait(timeout=self._WATCHDOG_SECONDS))
                    self.assertTrue(
                        reader_done.wait(timeout=self._WATCHDOG_SECONDS), "reader never unblocked"
                    )
                finally:
                    reader_thread.join(timeout=self._WATCHDOG_SECONDS)
                    self.assertFalse(reader_thread.is_alive(), "reader thread did not join")
            finally:
                release_writer.set()
                writer.join(timeout=self._WATCHDOG_SECONDS)
                self.assertFalse(writer.is_alive(), "writer thread did not join")

            self.assertNotIn("error", reader_result)
            launcher._unlock(reader_result["handle"])

    def test_two_shared_readers_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            (home / ".harness").mkdir(parents=True)
            home.chmod(0o700)
            lock = home / ".harness" / "codex-launcher.lock"
            lock.write_bytes(b"")
            lock.chmod(0o600)

            both_open = threading.Barrier(2, timeout=self._WATCHDOG_SECONDS)
            handles: list = []
            errors: list[BaseException] = []

            def reader() -> None:
                try:
                    handle = launcher._launcher_lock(home)
                    handles.append(handle)
                    both_open.wait()
                except BaseException as exc:  # noqa: BLE001 - surfaced on the main thread
                    errors.append(exc)

            threads = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=self._WATCHDOG_SECONDS)
                self.assertFalse(thread.is_alive(), "shared reader stalled")

            self.assertEqual(errors, [])
            self.assertEqual(len(handles), 2)
            for handle in handles:
                launcher._unlock(handle)

    def test_subprocess_reader_blocks_behind_a_held_exclusive_lock_then_succeeds(self) -> None:
        """Cross-process serialization: the writer's `LOCK_EX` and the
        reader's `LOCK_SH` in a genuinely separate reader process, not a
        thread sharing this interpreter's GIL and file-descriptor table.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / ".codex"
            (home / ".harness").mkdir(parents=True)
            home.chmod(0o700)
            path = installer_launcher.lock_path(home)
            path.write_bytes(b"")
            path.chmod(0o600)

            writer_holds = threading.Event()
            release_writer = threading.Event()
            writer_done = threading.Event()

            def hold_writer() -> None:
                lock = installer_launcher._LauncherLock(path)
                lock.__enter__()
                writer_holds.set()
                release_writer.wait(timeout=self._WATCHDOG_SECONDS)
                lock.__exit__(None, None, None)
                writer_done.set()

            writer = threading.Thread(target=hold_writer, daemon=True)
            writer.start()
            reader_script = (
                "import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('codex_launcher_runtime', sys.argv[1])\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "handle = mod._launcher_lock(mod.Path(sys.argv[2]))\n"
                "print('ACQUIRED', flush=True)\n"
                "mod._unlock(handle)\n"
            )
            proc = None
            try:
                self.assertTrue(
                    writer_holds.wait(timeout=self._WATCHDOG_SECONDS), "writer never acquired LOCK_EX"
                )
                proc = subprocess.Popen(
                    [sys.executable, "-c", reader_script, str(MODULE_PATH), str(home)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                # The reader process must still be blocked on LOCK_SH: no
                # output yet while the writer holds LOCK_EX.
                with self.assertRaises(subprocess.TimeoutExpired):
                    proc.communicate(timeout=0.5)
                release_writer.set()
                self.assertTrue(writer_done.wait(timeout=self._WATCHDOG_SECONDS))
                out, err = proc.communicate(timeout=self._WATCHDOG_SECONDS)
                self.assertEqual(0, proc.returncode, err)
                self.assertIn("ACQUIRED", out)
                proc = None
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=self._WATCHDOG_SECONDS)
                release_writer.set()
                writer.join(timeout=self._WATCHDOG_SECONDS)
                self.assertFalse(writer.is_alive(), "writer thread did not join")


class InstallerLifecycleLockInvariantTest(unittest.TestCase):
    """`state exists ⇒ lock exists`, exercised through the real installer lifecycle.

    This does not edit installer production code or its own test file; it only
    drives the installer's public `install`/`uninstall` through this reader's
    module to prove the invariant the reader now depends on.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.codex_home = self.home / ".codex"
        self.bin_dir = self.home / ".local" / "bin"
        self.real = self.root / "runtime" / "codex-real"
        self.real.parent.mkdir(parents=True)
        self.real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.real.chmod(0o755)
        self.codex_home.mkdir(parents=True)
        self.codex_home.chmod(0o775)
        self.bin_dir.mkdir(parents=True)
        self.target = self.bin_dir / "codex"
        self.target.symlink_to(self.real)
        hermetic = fixture_env.build_environment(
            self.root,
            Path(__file__).resolve().parents[1],
            base={"PATH": os.environ.get("PATH", "")},
        )
        hermetic.update(
            {
                "CODEX_HOME": str(self.codex_home),
                "HARNESS_BIN_DIR": str(self.bin_dir),
                "PATH": str(self.bin_dir),
                "SHELL": "/bin/codex-launcher-test-unsupported-shell",
            }
        )
        fixture_env.prepare_environment(hermetic)
        self.environment = mock.patch.dict(os.environ, hermetic, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_state_exists_implies_lock_exists_across_install_repair_and_this_reader(self) -> None:
        created = installer_launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(created["status"], "created")
        self.assertTrue(installer_launcher.state_path(self.codex_home).is_file())
        self.assertTrue(installer_launcher.lock_path(self.codex_home).is_file())

        # This reader must be able to consume the lock the installer just published.
        handle = launcher._launcher_lock(self.codex_home)
        launcher._unlock(handle)

        self.target.unlink()
        self.target.symlink_to(self.real)
        repaired = installer_launcher.install(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(repaired["status"], "repaired")
        self.assertTrue(installer_launcher.state_path(self.codex_home).is_file())
        self.assertTrue(installer_launcher.lock_path(self.codex_home).is_file())

        restored = installer_launcher.uninstall(codex_home=self.codex_home, bin_dir=self.bin_dir)
        self.assertEqual(restored["status"], "restored")
        self.assertFalse(installer_launcher.state_path(self.codex_home).is_file())
        self.assertFalse(installer_launcher.lock_path(self.codex_home).exists())
        with self.assertRaises(launcher.LauncherError):
            launcher._launcher_lock(self.codex_home)


class InteractivePermissionModeTest(unittest.TestCase):
    """User decision 2026-09-03 — a managed interactive Codex session starts in bypass."""

    def _mode_env(self, value=None):
        env = {} if value is None else {"AGENT_CODEX_INTERACTIVE_PERMISSION_MODE": value}
        return mock.patch.dict(os.environ, env, clear=False)

    def _cleared(self):
        return mock.patch.dict(
            os.environ, {"AGENT_CODEX_INTERACTIVE_PERMISSION_MODE": ""}, clear=False
        )

    def test_default_mode_is_bypass_and_only_inherit_opts_out(self) -> None:
        with self._cleared():
            self.assertEqual(launcher.interactive_permission_mode(), "bypass")
        for value, expected in (("inherit", "inherit"), ("INHERIT", "inherit"),
                                (" inherit ", "inherit"), ("bypass", "bypass"),
                                ("yolo", "bypass"), ("", "bypass"), ("off", "bypass")):
            with self.subTest(value=value), self._mode_env(value):
                self.assertEqual(launcher.interactive_permission_mode(), expected)

    def test_bare_and_resume_invocations_gain_the_flag_in_front(self) -> None:
        with self._cleared():
            for args in ([], ["hello"], ["resume", "--last"], ["fork"],
                         ["--model", "gpt-test", "resume", "thread-id"]):
                with self.subTest(args=args):
                    applied = launcher.apply_interactive_permission_mode(list(args))
                    # In front, so it stays a root option ahead of any subcommand.
                    self.assertEqual(applied, [launcher.BYPASS_FLAG, *args])

    def test_inherit_leaves_the_invocation_byte_identical(self) -> None:
        with self._mode_env("inherit"):
            for args in ([], ["resume", "--last"], ["hello"]):
                with self.subTest(args=args):
                    self.assertEqual(
                        launcher.apply_interactive_permission_mode(list(args)), list(args)
                    )

    def test_a_caller_selected_posture_is_never_overridden(self) -> None:
        explicit = (
            ["-s", "read-only"],
            ["--sandbox", "workspace-write"],
            ["--sandbox=read-only"],
            ["-a", "on-request"],
            ["--ask-for-approval", "never"],
            ["--ask-for-approval=on-request"],
            ["--approve-for-me"],
            [launcher.BYPASS_FLAG],
            ["--yolo"],
            ["-p", "hardened"],
            ["--profile", "hardened"],
            ["-c", "approval_policy=never"],
            ["-c", "sandbox_mode=read-only"],
            ["--config", "sandbox_permissions=[]"],
            ["--config=approval_policy=never"],
            ["-capproval_policy=never"],
            ["resume", "--last", "-s", "read-only"],
        )
        with self._cleared():
            for args in explicit:
                with self.subTest(args=args):
                    self.assertTrue(launcher.selects_own_posture(list(args)))
                    self.assertEqual(
                        launcher.apply_interactive_permission_mode(list(args)), list(args)
                    )

    def test_unrelated_options_do_not_look_like_a_posture(self) -> None:
        with self._cleared():
            for args in (["--model", "gpt-test"], ["-m", "gpt-test"], ["-c", "model=\"o3\""],
                         ["--config", "features.hooks=true"], ["--search"],
                         ["-i", "shot.png"], ["--cd", "/tmp"], ["--no-alt-screen"]):
                with self.subTest(args=args):
                    self.assertFalse(launcher.selects_own_posture(list(args)))
                    self.assertEqual(
                        launcher.apply_interactive_permission_mode(list(args))[0],
                        launcher.BYPASS_FLAG,
                    )

    def test_a_value_that_merely_looks_like_a_flag_is_not_read_as_one(self) -> None:
        """`--model --sandbox` is a model NAMED `--sandbox`; value slots are skipped."""
        with self._cleared():
            self.assertFalse(launcher.selects_own_posture(["--model", "--sandbox"]))

    def test_everything_after_a_bare_double_dash_is_the_prompt(self) -> None:
        with self._cleared():
            self.assertFalse(launcher.selects_own_posture(["--", "-s", "read-only"]))

    def test_passthrough_surfaces_never_reach_the_default(self) -> None:
        """`codex exec` is the registered dispatch surface (stage-dispatch SD-125 (5)):
        approval_policy=never with a real sandbox, and no bypass flag. It must stay
        outside `should_manage`, which is what keeps it away from this default."""
        for args in (["exec", "task"], ["--model", "gpt-test", "exec", "task"],
                     ["review", "--help"], ["app-server"], ["--remote", "unix:///tmp/x"]):
            with self.subTest(args=args):
                self.assertFalse(launcher.should_manage(list(args)))

    def test_managed_command_carries_the_flag_through_to_the_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent_home = root / "runtime"
            (agent_home / "utilities").mkdir(parents=True)
            (agent_home / "utilities" / "codex-managed-entry.py").write_text("", "utf-8")
            home = root / ".codex"
            (home / ".harness").mkdir(parents=True)
            binding = {"active_root": agent_home, "mode": "linked",
                       "revision": "rev-test", "identity": "linked:rev-test:-"}
            with self._cleared():
                applied = launcher.apply_interactive_permission_mode(["resume", "--last"])
            command = launcher.managed_command(applied, home, Path("/usr/bin/codex"), binding)
            trailing = command[command.index("--") + 1:]
            self.assertEqual(trailing, [launcher.BYPASS_FLAG, "resume", "--last"])


if __name__ == "__main__":
    unittest.main()
