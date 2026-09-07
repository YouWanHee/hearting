#!/usr/bin/env python3
"""Regression tests for installer-owned PATH launcher migration."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: E402
import installer  # noqa: E402
import distribution  # noqa: E402

_REAL_RESOLVER = (
    Path(__file__).resolve().parent.parent / "memory" / "store_resolve.py"
)


class LauncherMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.source = self.root / "nas/hearting"
        for _name, rel_source in bootstrap.LAUNCHERS:
            path = self.source / rel_source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o755)
        # F-80: launchers resolve through `resolve_launcher_source` (primary checkout),
        # not `resolve_source` (whichever tree is running install).
        self.resolve = mock.patch.object(
            bootstrap.paths,
            "resolve_launcher_source",
            side_effect=lambda relpath: self.source / relpath,
        )
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.managed = mock.patch.object(distribution, "is_managed", return_value=False)
        self.managed.start()
        self.addCleanup(self.managed.stop)

    def _target(self, name):
        return self.home / ".local/bin" / name

    def _legacy_source(self, name, checkout="agent_setting"):
        rel_source = dict(bootstrap.LAUNCHERS)[name]
        return self.home / checkout / rel_source

    def test_dry_run_and_install_migrate_exact_legacy_launchers(self):
        self._target("fleet").parent.mkdir(parents=True)
        for name, _rel_source in bootstrap.LAUNCHERS:
            self._target(name).symlink_to(self._legacy_source(name))

        dry_run = {row["name"]: row for row in bootstrap.install_launchers(
            home=self.home, dry_run=True
        )}
        self.assertEqual(
            {row["status"] for row in dry_run.values()}, {"planned-migration"}
        )
        self.assertEqual(
            Path(os.readlink(self._target("fleet"))), self._legacy_source("fleet")
        )

        installed = {row["name"]: row for row in bootstrap.install_launchers(
            home=self.home
        )}
        self.assertEqual(
            {row["status"] for row in installed.values()}, {"migrated-legacy"}
        )
        for name, rel_source in bootstrap.LAUNCHERS:
            self.assertEqual(
                self._target(name).resolve(), (self.source / rel_source).resolve()
            )

        repeated = bootstrap.install_launchers(home=self.home)
        self.assertEqual({row["status"] for row in repeated}, {"unchanged"})

    def test_prior_canonical_checkout_is_also_a_safe_migration_source(self):
        target = self._target("fleet")
        target.parent.mkdir(parents=True)
        target.symlink_to(self._legacy_source("fleet", checkout="hearting"))
        result = {row["name"]: row for row in bootstrap.install_launchers(home=self.home)}
        self.assertEqual(result["fleet"]["status"], "migrated-legacy")
        self.assertEqual(
            target.resolve(), (self.source / dict(bootstrap.LAUNCHERS)["fleet"]).resolve()
        )

    def test_active_arbitrary_checkout_is_a_safe_migration_source(self):
        prior_source = self.root / "mounted/team/hearting"
        rel_source = dict(bootstrap.LAUNCHERS)["fleet"]
        prior_launcher = prior_source / rel_source
        prior_launcher.parent.mkdir(parents=True)
        prior_launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        activation = self.home / ".claude/.harness/activation.json"
        activation.parent.mkdir(parents=True)
        activation.write_text(json.dumps({
            "schema": 2,
            "runtime": "claude",
            "scope": "global",
            "source_root": str(prior_source),
        }), encoding="utf-8")
        target = self._target("fleet")
        target.parent.mkdir(parents=True)
        target.symlink_to(prior_launcher)

        result = {row["name"]: row for row in bootstrap.install_launchers(
            home=self.home
        )}

        self.assertEqual(result["fleet"]["status"], "migrated-legacy")
        self.assertEqual(target.resolve(), (self.source / rel_source).resolve())

    def test_foreign_entries_are_preserved_while_missing_launchers_are_created(self):
        fleet = self._target("fleet")
        fleet.parent.mkdir(parents=True)
        fleet.write_text("user-owned\n", encoding="utf-8")
        hearting = self._target("hearting")
        foreign = self.home / "somewhere-else/hearting"
        hearting.symlink_to(foreign)

        rows = {row["name"]: row for row in bootstrap.install_launchers(home=self.home)}
        self.assertEqual(rows["fleet"]["status"], "skipped-collision")
        self.assertEqual(rows["hearting"]["status"], "skipped-collision")
        self.assertEqual(fleet.read_text(encoding="utf-8"), "user-owned\n")
        self.assertEqual(Path(os.readlink(hearting)), foreign)
        self.assertEqual(rows["harness"]["status"], "created")
        self.assertEqual(rows["mem"]["status"], "created")


class InstallerCollisionExitTest(unittest.TestCase):
    def test_install_returns_failure_when_a_foreign_launcher_is_preserved(self):
        args = SimpleNamespace(
            runtimes=["claude"], target=None, scope="global",
            plugin=False, dry_run=False, report_bundle_root=None,
        )
        driver = mock.Mock()
        driver.install.return_value = {"actions": [], "blocked": False}
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                installer, "get_driver", return_value=driver
            ))
            stack.enter_context(mock.patch.object(
                installer.routing_config, "ensure", return_value={
                "status": "preserved", "path": "/tmp/config",
                "enabled": ["claude"],
            }))
            stack.enter_context(mock.patch.object(
                installer.report_bundle_config, "ensure", return_value={
                    "status": "preserved", "path": "/tmp/report-bundle.json",
                    "root": "/tmp/reports",
                }))
            stack.enter_context(mock.patch.object(
                installer.bootstrap, "restore_memory", return_value={
                "action": "skipped", "detail": "present",
            }))
            stack.enter_context(mock.patch.object(
                installer.bootstrap, "install_launchers", return_value=[{
                "name": "fleet", "target": "/tmp/fleet", "source": "/src/fleet",
                "status": "skipped-collision", "detail": "foreign",
            }]))
            result = installer.cmd_install(args)
        self.assertEqual(result["exit"], installer.EXIT_FAIL)
        self.assertFalse(result["checks"][-1]["ok"])


class RestoreMemoryTest(unittest.TestCase):
    """Synthetic-only: never touches a live store. See core/MEMORY.md 7.0."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(parents=True)
        # Fixture "installed source" tree: proves restore_memory loads the
        # resolver through paths.resolve_source (the installed copy), not
        # merely whatever happens to be importable on sys.path.
        self.fixture_source = self.root / "fixture-source"
        fixture_resolver = self.fixture_source / "tools/memory/store_resolve.py"
        fixture_resolver.parent.mkdir(parents=True)
        shutil.copyfile(_REAL_RESOLVER, fixture_resolver)
        fixture_mem = self.fixture_source / "tools/memory/mem.py"
        fixture_mem.write_text(
            "#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8"
        )

        self.env_patch = mock.patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for variable, relative in {
            "HOME": "home", "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data",
            "XDG_STATE_HOME": "state", "MEM_WRITE_EVENTS": "state/write.jsonl",
            "MEM_RECALL_EVENTS": "state/recall.jsonl", "MEM_RECALL_RECEIPTS": "state/receipts",
        }.items():
            os.environ[variable] = str(self.root / relative)
            self.assertTrue(Path(os.environ[variable]).is_relative_to(self.root))
        original_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, original_cwd)

        self.resolve_patch = mock.patch.object(
            bootstrap.paths,
            "resolve_source",
            side_effect=lambda relpath: self.fixture_source / relpath,
        )
        self.resolve_patch.start()
        self.addCleanup(self.resolve_patch.stop)

    def _populate(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def test_non_empty_explicit_argument_wins_verbatim(self):
        explicit = self.root / "explicit-store"
        result = bootstrap.restore_memory(mem_store=str(explicit))
        self.assertEqual(result["action"], "skipped")
        self.assertIn("no dump.jsonl", result["detail"])
        self.assertFalse(explicit.exists())

    def test_explicit_nonexistent_argument_retains_first_install_import(self):
        explicit = self.root / "fresh-install-store"
        explicit.mkdir()
        self._populate(explicit / "dump.jsonl")
        with mock.patch.object(
            bootstrap.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as run:
            result = bootstrap.restore_memory(mem_store=str(explicit))
        self.assertEqual(result["action"], "imported")
        run.assert_called_once()

    def test_none_argument_delegates_to_resolver_populated_compat_recovery(self):
        # Bundle AGENT_HOME/memory and XDG memory dir both exist but empty;
        # the populated ~/.claude/memory compatibility store must still win.
        (self.home / "bundle" / "memory").mkdir(parents=True)
        os.environ["AGENT_HOME"] = str(self.home / "bundle")
        legacy = self.home / ".claude" / "memory"
        self._populate(legacy / "memory.db")
        result = bootstrap.restore_memory(mem_store=None)
        self.assertEqual(result["action"], "skipped")
        self.assertIn("memory.db already present", result["detail"])

    def test_empty_string_argument_delegates_like_none(self):
        legacy = self.home / ".claude" / "memory"
        self._populate(legacy / "memory.db")
        result = bootstrap.restore_memory(mem_store="")
        self.assertEqual(result["action"], "skipped")
        self.assertIn("memory.db already present", result["detail"])

    def test_empty_environment_mem_store_is_unset_not_current_directory(self):
        os.environ["MEM_STORE"] = ""
        cwd_sentinel = self.root / "memory.db"
        cwd_sentinel.write_text("untouched fixture")
        original_exists = Path.exists
        def guarded_exists(path):
            if path.absolute() in {cwd_sentinel, self.root / "dump.jsonl"}:
                raise AssertionError("empty override inspected cwd")
            return original_exists(path)
        with mock.patch.object(Path, "exists", guarded_exists):
            result = bootstrap.restore_memory(mem_store="")
        self.assertEqual(result["action"], "skipped")
        self.assertEqual(cwd_sentinel.read_text(), "untouched fixture")

    def test_empty_argument_plus_non_empty_environment_uses_environment(self):
        env_store = self.root / "env-store"
        self._populate(env_store / "dump.jsonl")
        os.environ["MEM_STORE"] = str(env_store)
        with mock.patch.object(
            bootstrap.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ):
            result = bootstrap.restore_memory(mem_store="")
        self.assertEqual(result["action"], "imported")

    def test_xdg_selection_when_no_compat_directory_exists(self):
        xdg = self.root / "xdg-data"
        os.environ["XDG_DATA_HOME"] = str(xdg)
        self._populate(xdg / "hearting" / "memory" / "dump.jsonl")
        with mock.patch.object(
            bootstrap.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ):
            result = bootstrap.restore_memory(mem_store=None)
        self.assertEqual(result["action"], "imported")

    def test_distinct_database_conflict_skips_before_any_side_effect(self):
        self._populate(self.home / ".claude" / "memory" / "memory.db")
        self._populate(self.home / "hearting" / "memory" / "memory.db")
        with mock.patch.object(bootstrap.subprocess, "run") as run:
            result = bootstrap.restore_memory(mem_store=None)
        self.assertEqual(result["action"], "skipped")
        self.assertIn("memory store resolution error", result["detail"])
        run.assert_not_called()
        # No target directory was created and no dump was probed/imported.
        self.assertFalse((self.home / "hearting" / "current").exists())

    def test_dump_only_first_install_behavior(self):
        legacy = self.home / ".claude" / "memory"
        self._populate(legacy / "dump.jsonl")
        with mock.patch.object(
            bootstrap.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as run:
            result = bootstrap.restore_memory(mem_store=None)
        self.assertEqual(result["action"], "imported")
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
