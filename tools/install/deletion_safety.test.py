#!/usr/bin/env python3
"""Incident regressions for invalid requests, profile races, and uninstall CAS."""

from __future__ import annotations

from argparse import Namespace
from contextlib import ExitStack
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_launcher  # noqa: E402
import fixture_env  # noqa: E402
import installer  # noqa: E402
import manifest  # noqa: E402
import distribution  # noqa: E402
import runtime_activation  # noqa: E402
import safe_fs  # noqa: E402


def _leaf_signature(path: Path) -> tuple[object, ...]:
    info = os.lstat(path)
    kind = stat.S_IFMT(info.st_mode)
    content = None
    if stat.S_ISREG(info.st_mode):
        content = hashlib.sha256(path.read_bytes()).hexdigest()
    elif stat.S_ISLNK(info.st_mode):
        content = os.readlink(path)
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        kind,
        content,
    )


def _tree_signature(root: Path) -> tuple[tuple[object, ...], ...]:
    if not root.exists():
        return ()
    paths = [root, *sorted(root.rglob("*"), key=os.fspath)]
    return tuple((str(path.relative_to(root)), *_leaf_signature(path)) for path in paths)


def _profile_install_worker(
    fixture: str,
    profile_root: str,
    codex_home: str,
    bin_dir: str,
    vendor: str,
    queue: multiprocessing.Queue,
) -> None:
    os.environ.update(
        {
            "HEARTING_FIXTURE_ROOT": fixture,
            "HOME": str(Path(fixture) / "home"),
            "ZDOTDIR": profile_root,
            "SHELL": "/bin/zsh",
            "CODEX_HOME": codex_home,
            "HARNESS_BIN_DIR": bin_dir,
            "PATH": str(Path(vendor).parent),
        }
    )
    try:
        result = codex_launcher.install(
            codex_home=Path(codex_home),
            bin_dir=Path(bin_dir),
            real_command=vendor,
            profile_policy="manage",
        )
        queue.put(("ok", result["status"]))
    except Exception as exc:  # noqa: BLE001 - child result is asserted by parent.
        queue.put(("blocked", type(exc).__name__))


def _crash_lock_worker(target: str, ready: multiprocessing.Event) -> None:
    with safe_fs.TargetLock(target):
        ready.set()
        os._exit(91)


class DeletionSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.fixture = self.base / "fixture"
        self.repo = Path(__file__).resolve().parents[2]
        self.environment = fixture_env.patched_environment(
            self.fixture,
            self.repo,
            base={"PATH": os.environ.get("PATH", "")},
        )
        self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)

    def _runtime_args(self, **changes: object) -> Namespace:
        values: dict[str, object] = {
            "runtime": ["codex"],
            "runtime_command": "activate",
            "mode": "linked",
            "source": str(self.repo),
            "scope": "global",
            "strict": False,
            "report_bundle_root": None,
        }
        values.update(changes)
        return Namespace(**values)

    def test_invalid_requests_do_not_capture_lock_or_mutate_ambient_zdotdir(self) -> None:
        outside = self.base / "outside-zdotdir"
        outside.mkdir()
        canary = outside / ".zshrc"
        canary.write_bytes(b"synthetic outside canary\n")
        canary.chmod(0o640)
        os.environ.update({"SHELL": "/bin/zsh", "ZDOTDIR": str(outside)})

        lock_root = safe_fs._lock_root()
        invalid = (
            self._runtime_args(runtime=["invalid-runtime"]),
            self._runtime_args(mode="invalid-mode"),
            self._runtime_args(scope="project"),
            self._runtime_args(source=str(self.fixture / "missing-source")),
        )
        for args in invalid:
            with self.subTest(args=vars(args)):
                canary_before = _leaf_signature(canary)
                fixture_before = _tree_signature(self.fixture)
                locks_before = _tree_signature(lock_root)
                with ExitStack() as stack:
                    launcher_capture = stack.enter_context(
                        mock.patch.object(
                            codex_launcher,
                            "capture_snapshot",
                            wraps=codex_launcher.capture_snapshot,
                        )
                    )
                    runtime_capture = stack.enter_context(
                        mock.patch.object(
                            runtime_activation,
                            "capture_runtime_state",
                            wraps=runtime_activation.capture_runtime_state,
                        )
                    )
                    mutation_spies = [
                        stack.enter_context(mock.patch.object(os, name, wraps=getattr(os, name)))
                        for name in ("unlink", "remove", "rmdir", "replace", "rename")
                    ]
                    mutation_spies.append(
                        stack.enter_context(mock.patch.object(shutil, "rmtree", wraps=shutil.rmtree))
                    )
                    mutation_spies.extend(
                        stack.enter_context(
                            mock.patch.object(tempfile, name, wraps=getattr(tempfile, name))
                        )
                        for name in ("mkstemp", "mkdtemp")
                    )
                    result = installer.cmd_runtime(args)

                self.assertEqual(result["exit"], installer.EXIT_BLOCKED)
                self.assertIn("invalid-before-mutation", json.dumps(result))
                launcher_capture.assert_not_called()
                runtime_capture.assert_not_called()
                for spy in mutation_spies:
                    spy.assert_not_called()
                self.assertEqual(_leaf_signature(canary), canary_before)
                self.assertEqual(_tree_signature(self.fixture), fixture_before)
                self.assertEqual(_tree_signature(lock_root), locks_before)

    def _run_profile_race(self, *, existing: bool) -> None:
        race_root = self.fixture / ("file-preimage" if existing else "missing-preimage")
        profile_root = race_root / "zdot"
        profile_root.mkdir(parents=True)
        profile = profile_root / ".zshrc"
        original = b"pre-existing profile\n"
        if existing:
            profile.write_bytes(original)
            profile.chmod(0o640)
        vendor = race_root / "vendor" / "codex"
        vendor.parent.mkdir(parents=True)
        vendor.write_bytes(b"#!/bin/sh\nexit 0\n")
        vendor.chmod(0o755)

        queue: multiprocessing.Queue = multiprocessing.Queue()
        processes = []
        for index in range(4):
            codex_home = race_root / f"codex-home-{index}"
            bin_dir = race_root / f"bin-{index}"
            process = multiprocessing.Process(
                target=_profile_install_worker,
                args=(
                    str(self.fixture),
                    str(profile_root),
                    str(codex_home),
                    str(bin_dir),
                    str(vendor),
                    queue,
                ),
            )
            processes.append(process)
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        results = [queue.get(timeout=2) for _ in processes]
        self.assertEqual(sum(status == "ok" for status, _ in results), 1)
        self.assertTrue(profile.is_file())
        payload = profile.read_bytes()
        if existing:
            self.assertTrue(payload.startswith(original))
        self.assertEqual(payload.count(codex_launcher.PROFILE_START), 1)
        self.assertEqual(payload.count(codex_launcher.PROFILE_END), 1)

    @unittest.skipIf(safe_fs.fcntl is None, "POSIX flock required")
    def test_four_codex_homes_serialize_file_and_missing_profile_preimages(self) -> None:
        self._run_profile_race(existing=True)
        self._run_profile_race(existing=False)

    @unittest.skipIf(safe_fs.fcntl is None, "POSIX flock required")
    def test_crash_releases_target_lock_without_replacing_lock_inode(self) -> None:
        target = self.fixture / "crash-target"
        ready = multiprocessing.Event()
        process = multiprocessing.Process(
            target=_crash_lock_worker, args=(str(target), ready)
        )
        process.start()
        self.assertTrue(ready.wait(5))
        process.join(5)
        self.assertEqual(process.exitcode, 91)
        lock = safe_fs.lock_path(target)
        before = lock.stat()
        with safe_fs.TargetLock(target):
            during = lock.stat()
        self.assertEqual(
            (before.st_dev, before.st_ino), (during.st_dev, during.st_ino)
        )

    def _uninstall_fixture(self, *, modified_copy: bool, repointed_link: bool) -> tuple[dict, Path, Path]:
        runtime_home = self.fixture / "opencode-home"
        runtime_home.mkdir(parents=True, exist_ok=True)
        copy_path = runtime_home / "models.conf"
        canonical = b"canonical model config\n"
        copy_path.write_bytes(b"user modification\n" if modified_copy else canonical)
        source = self.fixture / "source" / "skill"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"skill\n")
        link = runtime_home / "skills" / "demo"
        link.parent.mkdir(parents=True)
        if repointed_link:
            successor = self.fixture / "user-successor"
            successor.write_bytes(b"successor\n")
            link.symlink_to(successor)
        else:
            link.symlink_to(source)
        manifest_path = self.fixture / "state" / "manifest.json"
        manifest._write_manifest(
            manifest_path,
            {
                "schema": 1,
                "runtime": "opencode",
                "scope": "global",
                "version": "fixture",
                "timestamp": "fixture",
                "files": {"models.conf": hashlib.sha256(canonical).hexdigest()},
            },
        )
        args = Namespace(
            runtimes=["opencode"], target="opencode", scope="global", dry_run=False
        )
        plan = {
            "opencode": [
                {"action": "symlink", "dest": str(link), "source": str(source)}
            ]
        }
        with (
            mock.patch.object(runtime_activation, "validate_scope"),
            mock.patch.object(
                runtime_activation,
                "deactivate",
                return_value={"status": "not-active", "removed": []},
            ),
            mock.patch.object(manifest, "_manifest_path", return_value=manifest_path),
            mock.patch.object(installer.paths, "runtime_home", return_value=runtime_home),
            mock.patch.object(installer.projector, "plan", return_value=plan),
        ):
            result = installer.cmd_uninstall(args)
        return result, copy_path, link

    def test_uninstall_preserves_modified_copy_once_file(self) -> None:
        result, copy_path, link = self._uninstall_fixture(
            modified_copy=True, repointed_link=False
        )
        self.assertEqual(result["exit"], installer.EXIT_BLOCKED)
        self.assertIn("expected-state-mismatch", json.dumps(result))
        self.assertEqual(copy_path.read_bytes(), b"user modification\n")
        self.assertTrue(link.is_symlink())

    def test_uninstall_preserves_repointed_projection(self) -> None:
        result, copy_path, link = self._uninstall_fixture(
            modified_copy=False, repointed_link=True
        )
        successor = os.readlink(link)
        result_text = json.dumps(result)
        self.assertEqual(result["exit"], installer.EXIT_BLOCKED)
        self.assertIn("expected-state-mismatch", result_text)
        self.assertEqual(copy_path.read_bytes(), b"canonical model config\n")
        self.assertEqual(os.readlink(link), successor)

    def test_corrupt_manifest_fails_closed(self) -> None:
        manifest_path = self.fixture / "corrupt-manifest.json"
        manifest_path.write_bytes(b"{not-json")
        with self.assertRaisesRegex(ValueError, "ownership-unproved"):
            manifest.load_ownership_manifest(manifest_path, "codex", "global")


class ReleaseScanSelectsRouteRecordsTest(unittest.TestCase):
    """One sidecar must not disable release pruning (measured 2026-09-06).

    `_open_route_launch_homes` globbed `*.json` in the routes directory and
    skipped only `.outcome.json`. The later `.gate-release.json` sidecar was
    therefore read as a route record, came back undecidable, and made the whole
    scan unreliable -- and an unreliable scan marks EVERY release in use. On
    this machine that held 20 releases and 606 MB with zero open attempts,
    behind one valid 354-byte sidecar.

    The fix selects by the name `canonical_route_path()` writes, so a sidecar
    shape nobody has invented yet cannot re-break it.
    """

    ROUTE_ID = "rt-da62cded1408b893"

    def _routes_dir(self, base: Path) -> Path:
        routes = base / ".agent_reports" / ".runtime" / "routes"
        routes.mkdir(parents=True)
        return routes

    def _record(self, path: Path, launch_home: str) -> None:
        path.write_text(json.dumps({
            "route_id": path.stem, "schema_version": 2,
            "launch_compatibility_tuple": {
                "launch_home": {"kind": "launch_home", "path": launch_home}},
        }), encoding="utf-8")

    def _scan(self, base: Path):
        with mock.patch.object(
            distribution, "_open_route_artifact_roots",
            return_value=([str(base / ".agent_reports")], [], ""),
        ):
            return distribution._open_route_launch_homes({})

    def test_a_gate_release_sidecar_does_not_make_the_scan_unreliable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"))
            (routes / f"{self.ROUTE_ID}.gate-release.json").write_text(json.dumps({
                "route_id": self.ROUTE_ID, "schema_version": 1,
                "gate_releases": [{"gate": "frame-review", "decision": "proceed"}],
            }), encoding="utf-8")
            results, reason = self._scan(base)
            self.assertEqual(reason, "", "one sidecar must not poison the scan")
            self.assertEqual([route_id for route_id, _ in results], [self.ROUTE_ID])

    def test_every_sidecar_shape_beside_a_route_record_is_ignored(self):
        # Not a denylist of the shapes we happen to know: anything that is not
        # `<route_id>.json` is not a route record.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"))
            # Sidecars of a DIFFERENT route: an `.outcome.json` beside this
            # route's own record would (correctly) mark it closed, which is a
            # separate rule and not what this test is about.
            other = "rt-0000000000000000"
            for name in (
                f"{other}.outcome.json",
                f"{self.ROUTE_ID}.gate-release.json",
                f"{other}.superseded-20260904T000000Z.outcome.json",
                f"{self.ROUTE_ID}.some-future-sidecar.json",
                "notes.json",
                "rt-NOTHEX.json",
            ):
                (routes / name).write_text("{ not a route record", encoding="utf-8")
            results, reason = self._scan(base)
            self.assertEqual(reason, "")
            self.assertEqual([route_id for route_id, _ in results], [self.ROUTE_ID])

    def test_a_genuinely_unparsable_route_record_still_fails_closed(self):
        # The undecidable-is-in-use rule is the point of the scan and must
        # survive: only the SELECTION narrowed, not the judgement.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            (routes / f"{self.ROUTE_ID}.json").write_text("{ truncated", encoding="utf-8")
            results, reason = self._scan(base)
            self.assertTrue(reason.startswith("route-record-unparsable:"), reason)
            self.assertEqual(results, [])

    def test_an_unrecognised_open_route_record_is_never_skipped_silently(self):
        # Skipping a real route record removes a release's protection, and that
        # is the direction that deletes data. A name the reader does not know is
        # adjudicated by content, not waved through.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"))
            legacy = routes / "2026-08-13_wwd-eval-labels.json"
            self._record(legacy, str(base / "other-release"))
            results, reason = self._scan(base)
            self.assertTrue(
                reason.startswith("route-record-unrecognised-name:"), reason)
            self.assertEqual(results, [])

    def test_a_closed_record_under_a_legacy_name_costs_nothing(self):
        # 79 such records exist on this machine, all closed. A closed record's
        # launch_home is stale by definition, so it must not poison the scan.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routes = self._routes_dir(base)
            self._record(routes / f"{self.ROUTE_ID}.json", str(base / "release"))
            legacy = routes / "2026-08-13_wwd-eval-labels.json"
            self._record(legacy, str(base / "other-release"))
            legacy.with_name(legacy.stem + ".outcome.json").write_text("{}", encoding="utf-8")
            results, reason = self._scan(base)
            self.assertEqual(reason, "")
            self.assertEqual([route_id for route_id, _ in results], [self.ROUTE_ID])


class ReleaseHeldByLiveProcessTest(unittest.TestCase):
    """A release a live process runs out of is in use, however it was launched.

    Measured 2026-09-06: three managed codex sessions had
    `AGENT_HOME=<releases/v2.110.1>` and had been alive two days, while jobs.log
    held only `done` rows for it and no activation named it. Restoring release
    pruning without this source would have deleted that tree out from under
    them. The scan bug had been shielding them since 2026-09-04.
    """

    def test_this_process_holds_its_own_interpreter_tree(self):
        # A positive case that needs no fixture process: this very interpreter's
        # cmdline names the test file, so the repo root is "held".
        held, why = distribution._release_held_by_live_process(
            Path(__file__).resolve().parents[2])
        self.assertTrue(held)
        self.assertTrue(why.startswith("live-process:"), why)

    def test_an_unrelated_tree_is_not_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            held, why = distribution._release_held_by_live_process(Path(tmp))
            self.assertFalse(held, why)

    def test_an_unreadable_process_does_not_mark_every_release_in_use(self):
        # Some of our own processes deny /proc entirely (`(sd-pam)`), and pid 1
        # belongs to another user. If either were treated as undecidable, every
        # release would be in use -- the exact bug this change repairs. They are
        # skipped and counted instead, so the scan still reaches a verdict.
        #
        # The uid filter is noise reduction, not a correctness gate: with
        # unreadable processes skipped, ownership changes no verdict. Saying so
        # here rather than pretending this test proves it.
        with tempfile.TemporaryDirectory() as tmp:
            held, why = distribution._release_held_by_live_process(Path(tmp))
        self.assertFalse(held, why)


if __name__ == "__main__":
    unittest.main()
