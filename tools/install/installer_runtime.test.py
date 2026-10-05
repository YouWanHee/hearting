#!/usr/bin/env python3
"""Installer-level fault injection at the Codex launcher retirement boundary.

`runtime_activation.test.py` and `runtime-activation.test.sh` already prove
`runtime_activation.py`'s own multi-runtime rollback and the end-to-end
protected-ingress lifecycle against a full adapter fixture. This module
isolates `installer.py`'s `cmd_runtime` rollback wiring instead: it stubs the
runtime-projection collaborator (`runtime_activation.capture_runtime_state` /
`activate` / `refresh` / `restore_runtime_state` / `discard_runtime_state`) so
a failure can be injected precisely after legacy launcher removal
(`HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER=1`) and asserts the launcher state is
restored to its exact pre-transaction bytes.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import installer  # noqa: E402
import codex_launcher  # noqa: E402
import runtime_activation  # noqa: E402
import fixture_env  # noqa: E402


def _write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _dangling_linked_bundle(home: Path, runtime: str) -> Path:
    """A linked bundle whose release is gone -- what release pruning leaves behind."""
    bundle = home / ".harness" / "bundles" / "release-v1.0.0-aaaaaaaaaaaa"
    bundle.mkdir(parents=True)
    # The collector leaves a home without an activation record alone.
    (home / ".harness" / "activation.json").write_text(
        json.dumps({"active_root": str(home / "active"), "source_root": str(home / "active")}),
        encoding="utf-8",
    )
    (bundle / "source").symlink_to(home / "pruned-release", target_is_directory=True)
    # The two newest bundle directories are never collected; make this one older.
    for name in ("newer-1", "newer-2"):
        (bundle.parent / name).mkdir()
    (bundle / "bundle.json").write_text(
        json.dumps({
            "schema": runtime_activation.SCHEMA,
            "runtime": runtime,
            "source_revision": "release:v1.0.0:aaaaaaaaaaaa",
            "source_link": True,
            "checksum": "0" * 64,
        }),
        encoding="utf-8",
    )
    os.utime(bundle, (0, 0))
    return bundle


@contextmanager
def _stubbed_runtime_projection():
    fake_report = {"runtime": "codex", "freshness": "fresh", "next_action": "none"}
    with mock.patch.multiple(
        runtime_activation,
        validate_request=mock.DEFAULT,
        capture_runtime_state=mock.DEFAULT,
        seal_runtime_state=mock.DEFAULT,
        restore_runtime_state=mock.DEFAULT,
        discard_runtime_state=mock.DEFAULT,
        activate=mock.DEFAULT,
        refresh=mock.DEFAULT,
    ) as mocks:
        mocks["capture_runtime_state"].return_value = {
            "fake": "snapshot",
            "_sealed": True,
        }
        mocks["activate"].return_value = dict(fake_report)
        mocks["refresh"].return_value = dict(fake_report)
        yield mocks


class LauncherCommitBoundaryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.vendor_bin = root / "vendor-bin"
        self.home.mkdir()
        self.vendor_bin.mkdir()
        self.codex_home = self.home / ".codex"
        self.vendor_codex = self.vendor_bin / "codex"
        _write_executable(self.vendor_codex)
        # Synthetic user-owned surfaces the plan names explicitly
        # (credentials, sessions, config): no launcher install, refresh,
        # uninstall, or rollback transaction in this module may ever touch
        # these bytes.
        self.codex_home.mkdir(parents=True)
        (self.codex_home / "sessions").mkdir()
        self.protected_files = {
            self.codex_home / "auth.json": "protected-credential\n",
            self.codex_home / "sessions" / "existing-session.jsonl": "protected-session\n",
            self.codex_home / "config.toml": "protected-config\n",
        }
        for path, content in self.protected_files.items():
            path.write_text(content, encoding="utf-8")
        env = fixture_env.build_environment(
            root,
            Path(__file__).resolve().parents[2],
            base={"PATH": os.environ.get("PATH", "")},
        )
        env.update({
            "CODEX_HOME": str(self.codex_home),
            "PATH": str(self.vendor_bin) + os.pathsep + env.get("PATH", ""),
            # An unsupported shell name makes `_profile_path()` return None
            # unconditionally, so no launcher/uninstall transaction in this
            # module ever resolves a profile path from the ambient
            # SHELL/ZDOTDIR/XDG_CONFIG_HOME of the process running the test
            # (which would otherwise point outside the private fixture home
            # at a real, unrelated shell startup file). Shell-specific
            # profile-mapping behavior is exhaustively covered by
            # `codex_launcher.test.py`, not this module.
            "SHELL": "/bin/installer-runtime-test-unsupported-shell",
        })
        env.pop("HARNESS_BIN_DIR", None)
        fixture_env.prepare_environment(env)
        self._env_patch = mock.patch.dict(os.environ, env, clear=True)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        os.environ.pop("HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER", None)
        self.addCleanup(os.environ.pop, "HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER", None)
        os.environ.pop("HARNESS_INSTALLER_FAIL_AFTER_UNINSTALL_LAUNCHER", None)
        self.addCleanup(
            os.environ.pop, "HARNESS_INSTALLER_FAIL_AFTER_UNINSTALL_LAUNCHER", None
        )

    def _assert_protected_surfaces_untouched(self):
        for path, content in self.protected_files.items():
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                content,
                f"protected user-owned surface was mutated: {path}",
            )

    def _install_legacy_launcher(self):
        result = codex_launcher.install(dry_run=False)
        self.assertEqual(result["status"], "created")

    def _args(self, command):
        return Namespace(
            runtime=["codex"],
            runtime_command=command,
            mode="linked",
            source=str(Path(self._tmp.name) / "source"),
            scope="global",
            strict=False,
            report_bundle_root=None,
        )

    def _uninstall_args(self):
        return Namespace(
            runtimes=["codex"],
            target="codex",
            scope="global",
            dry_run=False,
        )

    def test_injected_failure_after_launcher_retirement_restores_exact_prior_state(self):
        self._install_legacy_launcher()
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        # Activation itself retires the pre-existing launcher, so seed it
        # again to exercise rollback at the retirement boundary.
        self._install_legacy_launcher()
        self.assertEqual(first["exit"], installer.EXIT_OK)

        state_path = codex_launcher.state_path(self.codex_home)
        self.assertTrue(state_path.exists())
        wrapper_target = codex_launcher.wrapper_path(codex_launcher.default_bin_dir())
        self.assertTrue(wrapper_target.exists())
        committed_state_bytes = state_path.read_bytes()
        committed_wrapper_bytes = wrapper_target.read_bytes()

        os.environ["HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER"] = "1"
        with _stubbed_runtime_projection() as mocks:
            second = installer.cmd_runtime(self._args("refresh"))
            restore_runtime_state = mocks["restore_runtime_state"]

        self.assertEqual(second["exit"], installer.EXIT_BLOCKED)
        self.assertIn("injected failure after launcher commit boundary", second["lines"][-1])
        restore_runtime_state.assert_called_once_with(
            {"fake": "snapshot", "_sealed": True}
        )

        # The installer's rollback restores the legacy wrapper and state to
        # their exact pre-refresh bytes.
        self.assertEqual(state_path.read_bytes(), committed_state_bytes)
        self.assertEqual(wrapper_target.read_bytes(), committed_wrapper_bytes)
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertTrue(status["installed"])
        self.assertEqual(status["real_command"], str(self.vendor_codex))
        self._assert_protected_surfaces_untouched()

    def test_no_injection_leaves_refresh_committed(self):
        self._install_legacy_launcher()
        for command in ("activate", "refresh"):
            with _stubbed_runtime_projection() as mocks:
                result = installer.cmd_runtime(self._args(command))
                discard_runtime_state = mocks["discard_runtime_state"]
                restore_runtime_state = mocks["restore_runtime_state"]
            self.assertEqual(result["exit"], installer.EXIT_OK)
            discard_runtime_state.assert_called_once_with(
                {"fake": "snapshot", "_sealed": True}
            )
            restore_runtime_state.assert_not_called()
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertFalse(status["installed"])
        self._assert_protected_surfaces_untouched()

    def test_activate_collects_retired_bundles_after_commit(self):
        bundle = _dangling_linked_bundle(self.codex_home, "codex")
        with _stubbed_runtime_projection():
            result = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertEqual(result["retired_bundles"]["removed"], [bundle.name])
        self.assertIn("codex: retired-bundles removed=1 kept=2 deferred=0", result["lines"])
        self.assertFalse(bundle.exists())
        self._assert_protected_surfaces_untouched()

    def test_collection_runs_only_after_the_rollback_snapshot_is_discarded(self):
        order = []

        def collect(runtime, *_args, **_kwargs):
            order.append("collect")
            return {"runtime": runtime, "status": "ok", "removed": [], "kept": {}, "deferred": 0}

        with _stubbed_runtime_projection() as mocks:
            mocks["discard_runtime_state"].side_effect = lambda _snapshot: order.append("discard")
            with mock.patch.object(
                runtime_activation, "collect_retired_bundles", side_effect=collect
            ) as collector:
                result = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertEqual(order, ["discard", "collect"])
        self.assertEqual(
            collector.call_args.kwargs["copy_budget"], installer.ACTIVATION_COPY_BUDGET
        )

    def test_a_rolled_back_invocation_collects_nothing(self):
        self._install_legacy_launcher()
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(first["exit"], installer.EXIT_OK)
        self._install_legacy_launcher()
        bundle = _dangling_linked_bundle(self.codex_home, "codex")
        os.environ["HARNESS_INSTALLER_FAIL_AFTER_LAUNCHER"] = "1"
        with _stubbed_runtime_projection():
            second = installer.cmd_runtime(self._args("refresh"))
        self.assertEqual(second["exit"], installer.EXIT_BLOCKED)
        self.assertNotIn("retired_bundles", second)
        self.assertTrue(bundle.is_dir())

    def test_a_later_runtime_that_blocks_rolls_back_before_any_collection(self):
        bundle = _dangling_linked_bundle(self.codex_home, "codex")
        args = self._args("activate")
        args.runtime = ["codex", "claude"]
        with _stubbed_runtime_projection() as mocks:
            mocks["activate"].side_effect = [
                {"runtime": "codex", "freshness": "fresh", "next_action": "none"},
                runtime_activation.ActivationError("claude blocked"),
            ]
            with mock.patch.object(
                runtime_activation, "collect_retired_bundles"
            ) as collector:
                result = installer.cmd_runtime(args)
            restored = mocks["restore_runtime_state"].call_count
        self.assertEqual(result["exit"], installer.EXIT_BLOCKED)
        self.assertEqual(restored, 2)  # the runtime that had already committed, too
        collector.assert_not_called()
        self.assertTrue(bundle.is_dir())

    def test_collection_trouble_never_changes_a_committed_exit_code(self):
        bundle = _dangling_linked_bundle(self.codex_home, "codex")
        # Another runtime's record is a reference source; unreadable bytes
        # there used to escape as UnicodeDecodeError after the commit.
        other = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".harness" / "activation.json"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"\xff")
        with _stubbed_runtime_projection():
            result = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertEqual(result["retired_bundles"]["status"], "skipped")
        self.assertTrue(bundle.is_dir())
        with _stubbed_runtime_projection(), mock.patch.object(
            runtime_activation, "collect_retired_bundles",
            side_effect=ValueError("relative runtime home"),
        ):
            result = installer.cmd_runtime(self._args("refresh"))
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertEqual(result["retired_bundles"]["status"], "skipped")
        self.assertIn("ValueError", result["retired_bundles"]["detail"])

    def test_activate_removes_dangling_managed_node_links(self):
        # HARNESS_BIN_DIR is unset in this fixture, so the launcher dir is the
        # private HOME's ~/.local/bin -- the same fallback a real host uses.
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        stale = bin_dir / "npx"
        stale.symlink_to(
            Path(self._tmp.name) / "gone" / "hearting" / "node" / "current" / "bin" / "npx"
        )
        with _stubbed_runtime_projection():
            result = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertFalse(stale.is_symlink())
        self.assertEqual(result["node_launchers"]["status"], "repaired")
        self.assertIn(
            "environment: host.node-launchers -> repaired "
            f"(removed dangling managed npx from {bin_dir})",
            result["lines"],
        )
        self._assert_protected_surfaces_untouched()

    def test_reinstall_after_activate_is_idempotent_at_installer_level(self):
        self._install_legacy_launcher()
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(first["exit"], installer.EXIT_OK)
        state_path = codex_launcher.state_path(self.codex_home)
        wrapper_target = codex_launcher.wrapper_path(codex_launcher.default_bin_dir())
        self.assertFalse(state_path.exists())
        self.assertFalse(wrapper_target.exists())

        # Reinstall: activate again over an already-activated runtime.
        with _stubbed_runtime_projection() as mocks:
            second = installer.cmd_runtime(self._args("activate"))
            mocks["restore_runtime_state"].assert_not_called()

        self.assertEqual(second["exit"], installer.EXIT_OK)
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertFalse(status["installed"])
        self._assert_protected_surfaces_untouched()

    def test_full_uninstall_removes_managed_launcher(self):
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(first["exit"], installer.EXIT_OK)
        self._install_legacy_launcher()
        wrapper_target = codex_launcher.wrapper_path(codex_launcher.default_bin_dir())
        self.assertTrue(wrapper_target.exists())

        result = installer.cmd_uninstall(self._uninstall_args())
        self.assertEqual(result["exit"], installer.EXIT_OK)
        self.assertFalse(wrapper_target.exists())
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertFalse(status["installed"])
        self._assert_protected_surfaces_untouched()

    def test_partial_uninstall_fault_injection_restores_exact_launcher_state(self):
        with _stubbed_runtime_projection():
            first = installer.cmd_runtime(self._args("activate"))
        self.assertEqual(first["exit"], installer.EXIT_OK)
        self._install_legacy_launcher()

        state_path = codex_launcher.state_path(self.codex_home)
        wrapper_target = codex_launcher.wrapper_path(codex_launcher.default_bin_dir())
        committed_state_bytes = state_path.read_bytes()
        committed_wrapper_bytes = wrapper_target.read_bytes()

        os.environ["HARNESS_INSTALLER_FAIL_AFTER_UNINSTALL_LAUNCHER"] = "1"
        result = installer.cmd_uninstall(self._uninstall_args())

        self.assertEqual(result["exit"], installer.EXIT_BLOCKED)
        self.assertIn(
            "injected failure after uninstall launcher commit boundary",
            result["lines"][-1],
        )

        # The launcher's own uninstall() call ran and committed (removed the
        # wrapper); the installer's rollback must have restored it — a
        # partial uninstall must never leave the protected ingress removed
        # while the rest of the runtime's uninstall never ran.
        self.assertTrue(wrapper_target.exists())
        self.assertEqual(state_path.read_bytes(), committed_state_bytes)
        self.assertEqual(wrapper_target.read_bytes(), committed_wrapper_bytes)
        status = codex_launcher.status(codex_home=self.codex_home)
        self.assertTrue(status["installed"])
        self.assertEqual(status["real_command"], str(self.vendor_codex))
        self._assert_protected_surfaces_untouched()


class StatusVersionSkewTest(unittest.TestCase):
    def _status_args(self, runtimes):
        return Namespace(runtimes=runtimes, target=None, scope="user", plugin=False)

    def _fake_activation(self, source_root):
        return {"freshness": "fresh", "mode": "packaged", "source_root": source_root}

    def _fake_driver(self, version):
        driver = mock.Mock()
        driver.status.return_value = {"channel": "dev", "version": version, "drift_count": 0}
        return driver

    def test_status_reports_skew_when_runtimes_disagree(self):
        args = self._status_args(["claude", "codex"])
        drivers = {"claude": self._fake_driver("1.0.0"), "codex": self._fake_driver("2.0.0")}
        with mock.patch.object(runtime_activation, "status",
                               side_effect=lambda rt, scope: self._fake_activation(f"/src/{rt}")), \
             mock.patch.object(installer, "get_driver", side_effect=lambda rt: drivers[rt]):
            result = installer.cmd_status(args)
        self.assertTrue(any(line.startswith("version-skew:") for line in result["lines"]))
        self.assertTrue(any(line.startswith("next:") for line in result["lines"]))
        skew_checks = [c for c in result["checks"] if c["id"] == "runtime.version-skew"]
        self.assertEqual(len(skew_checks), 1)
        self.assertFalse(skew_checks[0]["ok"])
        self.assertEqual(result["version_skew"]["versions"], ["1.0.0", "2.0.0"])

    def test_status_exit_code_is_unchanged_under_skew(self):
        args = self._status_args(["claude", "codex"])
        drivers = {"claude": self._fake_driver("1.0.0"), "codex": self._fake_driver("2.0.0")}
        with mock.patch.object(runtime_activation, "status",
                               side_effect=lambda rt, scope: self._fake_activation(f"/src/{rt}")), \
             mock.patch.object(installer, "get_driver", side_effect=lambda rt: drivers[rt]):
            result = installer.cmd_status(args)
        self.assertEqual(result["exit"], installer.EXIT_OK)

    def test_status_is_silent_when_versions_agree(self):
        args = self._status_args(["claude", "codex"])
        drivers = {"claude": self._fake_driver("1.0.0"), "codex": self._fake_driver("1.0.0")}
        with mock.patch.object(runtime_activation, "status",
                               side_effect=lambda rt, scope: self._fake_activation(f"/src/{rt}")), \
             mock.patch.object(installer, "get_driver", side_effect=lambda rt: drivers[rt]):
            result = installer.cmd_status(args)
        self.assertFalse(any(line.startswith("version-skew:") for line in result["lines"]))
        self.assertNotIn("version_skew", result)

    def test_status_does_not_call_surface_skew(self):
        args = self._status_args(["claude", "codex"])
        drivers = {"claude": self._fake_driver("1.0.0"), "codex": self._fake_driver("1.0.0")}
        with mock.patch.object(runtime_activation, "status",
                               side_effect=lambda rt, scope: self._fake_activation(f"/src/{rt}")), \
             mock.patch.object(installer, "get_driver", side_effect=lambda rt: drivers[rt]), \
             mock.patch.object(runtime_activation, "surface_skew") as fake_skew:
            installer.cmd_status(args)
            fake_skew.assert_not_called()


class UpdateSkipHintTest(unittest.TestCase):
    def setUp(self):
        # `cmd_update` sweeps the launcher dir and collects retired bundles in
        # every runtime home, so the whole environment stays inside the case.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patched = fixture_env.patched_environment(
            Path(tmp.name), Path(__file__).resolve().parents[2]
        )
        env = patched.__enter__()
        self.addCleanup(patched.__exit__, None, None, None)
        self.bin_dir = Path(env["HARNESS_BIN_DIR"])
        self.codex_home = Path(env["CODEX_HOME"])

    def _update_args(self):
        return Namespace(dry_run=False, scope="global", plugin=False, reapply=False,
                         version="latest", runtimes=["claude", "codex", "opencode"],
                         auto=False, force_prune_unproven=False)

    def _managed_result(self, skipped):
        return {
            "status": "updated", "version": "9.9.9", "runtimes": [],
            "session_action": {}, "skipped": skipped, "release_root": "/releases/9.9.9",
        }

    def test_each_closed_reason_gets_its_own_hint(self):
        skipped = {"claude": "missing", "codex": "linked", "opencode": "foreign"}
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result(skipped)):
            result = installer.cmd_update(self._update_args())
        hints = result["skipped_hints"]
        self.assertEqual(set(hints), {"claude", "codex", "opencode"})
        self.assertEqual(len(set(hints.values())), 3)
        for rt in ("claude", "codex", "opencode"):
            self.assertIn({"id": f"update.skipped.{rt}", "ok": False,
                           "detail": f"{skipped[rt]}: {hints[rt]}"}, result["checks"])

    def test_release_skipped_map_is_untouched(self):
        skipped = {"claude": "missing"}
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result(skipped)):
            result = installer.cmd_update(self._update_args())
        self.assertEqual(result["release"]["skipped"], skipped)

    def test_update_exit_code_is_unchanged(self):
        skipped = {"claude": "missing"}
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result(skipped)):
            result = installer.cmd_update(self._update_args())
        self.assertEqual(result["exit"], installer.EXIT_OK)

    def test_unknown_reason_keeps_legacy_line_format(self):
        skipped = {"claude": "some-new-reason"}
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result(skipped)):
            result = installer.cmd_update(self._update_args())
        self.assertIn("skipped: claude (some-new-reason)", result["lines"])
        self.assertNotIn("skipped_hints", result)

    def test_sound_launcher_dir_adds_a_row_but_no_line(self):
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result({})):
            result = installer.cmd_update(self._update_args())
        self.assertEqual(result["environment"], [{
            "id": "host.node-launchers", "status": "ok",
            "detail": f"no dangling managed node links in {self.bin_dir}",
        }])
        self.assertFalse([line for line in result["lines"] if line.startswith("environment:")])

    def test_update_removes_dangling_managed_node_links(self):
        self.bin_dir.mkdir(parents=True)
        stale = self.bin_dir / "node"
        stale.symlink_to(
            self.bin_dir.parent / "gone" / "hearting" / "node" / "current" / "bin" / "node"
        )
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result({})):
            result = installer.cmd_update(self._update_args())
        self.assertFalse(stale.is_symlink())
        self.assertEqual(result["environment"][0]["status"], "repaired")
        self.assertIn(
            "environment: host.node-launchers -> repaired "
            f"(removed dangling managed node from {self.bin_dir})",
            result["lines"],
        )
        self.assertEqual(result["exit"], installer.EXIT_OK)


    def test_update_collects_retired_bundles_in_every_runtime_home(self):
        bundle = _dangling_linked_bundle(self.codex_home, "codex")
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result({})):
            result = installer.cmd_update(self._update_args())
        self.assertFalse(bundle.exists())
        rows = {row["runtime"]: row for row in result["retired_bundles"]}
        self.assertEqual(set(rows), {"claude", "codex", "opencode"})
        self.assertEqual(rows["codex"]["removed"], [bundle.name])
        self.assertEqual(rows["claude"]["removed"], [])
        self.assertIn("codex: retired-bundles removed=1 kept=2 deferred=0", result["lines"])
        self.assertEqual(result["exit"], installer.EXIT_OK)

    def test_a_failed_reference_setup_skips_collection_and_keeps_the_update(self):
        bundle = _dangling_linked_bundle(self.codex_home, "codex")
        with mock.patch.object(installer.distribution, "is_managed", return_value=True), \
             mock.patch.object(installer.distribution, "update", return_value=self._managed_result({})), \
             mock.patch.object(installer.distribution, "reference_checker",
                               side_effect=RuntimeError("registry vanished")):
            result = installer.cmd_update(self._update_args())
        self.assertTrue(bundle.is_dir())
        rows = {row["runtime"]: row for row in result["retired_bundles"]}
        self.assertEqual(rows["codex"]["status"], "skipped")
        self.assertIn("registry vanished", rows["codex"]["detail"])
        self.assertEqual(result["exit"], installer.EXIT_OK)


class BundleReferenceSurfaceTest(unittest.TestCase):
    """What the distribution layer hands the bundle collector."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        patched = fixture_env.patched_environment(
            self.root, Path(__file__).resolve().parents[2]
        )
        env = patched.__enter__()
        self.addCleanup(patched.__exit__, None, None, None)
        self.bin_dir = Path(env["HARNESS_BIN_DIR"])

    def test_launcher_destinations_name_the_link_and_what_it_resolves_to(self):
        release = self.root / "release"
        (release / "tools" / "fleet").mkdir(parents=True)
        (release / "tools" / "fleet" / "fleet.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        bundle = self.root / "bundle"
        bundle.mkdir()
        (bundle / "source").symlink_to(release, target_is_directory=True)
        literal = bundle / "source" / "tools" / "fleet" / "fleet.sh"
        self.bin_dir.mkdir(parents=True)
        (self.bin_dir / "fleet").symlink_to(literal)
        found = installer.distribution.launcher_destinations()
        # Resolving alone would land in the release and hide the bundle passed through.
        self.assertIn(literal, found)
        self.assertIn(release / "tools" / "fleet" / "fleet.sh", found)

    def test_reference_checker_fails_closed_and_names_an_open_route(self):
        dist = installer.distribution
        with mock.patch.object(
            dist, "_stable_registry_snapshot",
            side_effect=dist.DistributionError("registry-unreadable:jobs.log"),
        ):
            check = dist.reference_checker()
        self.assertEqual(
            check(self.root / "anything"),
            (True, "reference-scan-failed:registry-unreadable:jobs.log"),
        )
        held = self.root / "held"
        free = self.root / "free"
        (held / "utilities").mkdir(parents=True)
        free.mkdir()
        with mock.patch.object(dist, "_stable_registry_snapshot", return_value=[]), \
             mock.patch.object(dist, "_open_route_launch_homes",
                               return_value=([("rt-1", str(held / "utilities"))], "")):
            check = dist.reference_checker()
        self.assertEqual(check(held), (True, "open-route:rt-1"))
        self.assertEqual(check(free), (False, ""))


if __name__ == "__main__":
    unittest.main()
