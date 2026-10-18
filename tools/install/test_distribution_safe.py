#!/usr/bin/env python3

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import distribution
import fixture_env


class StandaloneDistributionSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        source = base / "source"
        source.mkdir()
        self.environment = fixture_env.build_environment(
            base / "fixture", source, base={"PATH": os.environ.get("PATH", "")}
        )
        fixture_env.prepare_environment(self.environment)
        self.environment["HARNESS_TEST_PLATFORM"] = "linux"
        self.environment["HARNESS_SCHEDULER_NO_ACTIVATE"] = "1"
        self.patch = mock.patch.dict(os.environ, self.environment, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    # destructive-ok: reason=install a same-byte foreign successor for CAS verification; boundary=two exact symlink/file fixture leaves below HEARTING_FIXTURE_ROOT
    def test_rollback_preserves_exact_successors(self) -> None:
        root = Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "successors"
        root.mkdir()

        pointer = root / "current"
        pointer_pre = distribution._capture_leaf(pointer)
        pointer_post = distribution._atomic_symlink(
            pointer, root / "release-a", expected=pointer_pre
        )
        pointer_temp = root / "pointer-successor"
        pointer_temp.symlink_to(root / "release-b")
        os.replace(pointer_temp, pointer)
        with self.assertRaisesRegex(distribution.DistributionError, "concurrent-successor"):
            distribution._restore_link(pointer, pointer_pre, pointer_post)
        self.assertEqual(pointer.readlink(), root / "release-b")

        state = root / "state.json"
        state_pre = distribution._capture_leaf(state)
        payload = b'{"state":"managed"}\n'
        state_post = distribution._atomic_bytes(state, payload, expected=state_pre)
        state_temp = root / "state-successor"
        state_temp.write_bytes(payload)
        os.replace(state_temp, state)
        with self.assertRaisesRegex(distribution.DistributionError, "concurrent-successor"):
            distribution._restore_bytes(state, state_pre, state_post, None)
        self.assertEqual(state.read_bytes(), payload)

    def test_release_keep_message_names_the_actual_evidence(self) -> None:
        # Only an open attempt row is a "live dispatch attempt". A route record without a
        # closing outcome, or evidence that could not be read, must be named as what it is,
        # and the route line says what settles it (2026-10-06: four releases kept for weeks by
        # route records with no attempt row anywhere, announced as live attempts).
        release = Path("/releases/v9")
        attempt = distribution._release_keep_message(release, "open-attempt:att-123")
        self.assertIn("still referenced by a live dispatch attempt (open-attempt:att-123)", attempt)
        legacy = distribution._release_keep_message(release, "legacy-open-row-in-release-registry")
        self.assertIn("live dispatch attempt", legacy)
        route = distribution._release_keep_message(release, "open-route:rt-abc")
        self.assertNotIn("live dispatch attempt", route)
        self.assertIn("route record that has no closing outcome (open-route:rt-abc)", route)
        self.assertIn("capability-route.py close", route)
        for why in ("registry-unreadable:/x/jobs.log", "registry-row-unparsable:/x/jobs.log",
                    "route-launch-home-unresolvable:rt-abc", "route-discovery-unreliable:/x/bindings"):
            with self.subTest(why=why):
                line = distribution._release_keep_message(release, why)
                self.assertNotIn("live dispatch attempt", line)
                self.assertIn(f"cannot be proved unused ({why})", line)
        for line in (attempt, legacy, route):
            self.assertTrue(line.startswith(f"harness release: {release} "), line)

    def test_target_lock_domain_ignores_runtime_homes(self) -> None:
        target = Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "shared" / "current"
        target.parent.mkdir()
        key = distribution.hashlib.sha256(os.fsencode(str(target))).hexdigest()
        lock = distribution._standalone_lock_root() / f"{key}.lock"
        with distribution._target_lock(target):
            first = os.lstat(lock)
        os.environ["CODEX_HOME"] = str(
            Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "other-codex"
        )
        os.environ["HARNESS_STATE_ROOT"] = str(
            Path(os.environ["HEARTING_FIXTURE_ROOT"]) / "other-state"
        )
        with distribution._target_lock(target):
            second = os.lstat(lock)
        self.assertEqual((first.st_dev, first.st_ino), (second.st_dev, second.st_ino))

    def test_foreign_scheduler_unit_is_preserved(self) -> None:
        service, _timer = distribution._systemd_paths()
        service.parent.mkdir(parents=True)
        service.write_text("foreign\n", encoding="utf-8")
        before = distribution._capture_leaf(service)
        with self.assertRaisesRegex(distribution.DistributionError, "ownership-unproved"):
            distribution.disable_auto_update()
        self.assertEqual(distribution._capture_leaf(service), before)
        self.assertEqual(service.read_text(encoding="utf-8"), "foreign\n")

    def test_systemd_probe_reads_the_last_result_from_the_service(self) -> None:
        # The timer only schedules the run; Result and ExecMainStatus of the
        # last update belong to the service it starts.
        service, timer = distribution._systemd_paths()
        replies = {
            timer.name: "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
                        "LastTriggerUSec=Wed 2026-10-07 03:06:00 KST\n",
            service.name: "Result=exit-code\nExecMainCode=1\nExecMainStatus=1\n",
        }
        asked = []

        def probe(command):
            asked.append(command[3])
            return True, replies[command[3]]

        with mock.patch.object(distribution, "_probe_command", side_effect=probe):
            result = distribution._probe_systemd()
        self.assertEqual(asked, [timer.name, service.name])
        self.assertEqual(result["last_result"], "exit-code")
        self.assertEqual(result["exit_status"], 1)


class DispatchMigrationReanchorTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.candidate = base / "release"
        self.source_root = self.candidate / ".dispatch"
        self.environment = {"HARNESS_STATE_ROOT": str(base / "state")}
        self.target_root = distribution.stable_state_root(self.environment)
        relative = Path("completion/rt-test/plan.att-test.attempt.json")
        self.source = self.source_root / relative
        self.target = self.target_root / relative
        self.source.parent.mkdir(parents=True)
        self.link = {
            "schema_version": 2,
            "route_id": "rt-test", "node_id": "plan", "attempt_id": "att-test",
            "dispatch_depth": 1, "transport": "headless",
            "execution_surface": "headless", "registered_worker": True,
            "fallback_hop": "", "evidence_sha256": "a" * 64,
            "completion_marker": str(self.source.parent / "plan.json"),
            "completion_marker_history": str(self.source.parent / "plan.1.json"),
            # Normal owner-closure sidecars extend the original field set.
            "stage_authority": "owner-closure",
            "owner_closure_proof": {"reviews": ["review-a"], "accepted": True},
            "future_metadata": {"label": "추가 필드", "revision": 1},
        }
        self.source.write_bytes(self._serialize(self.link))
        for name in ("plan.json", "plan.1.json"):
            (self.source.parent / name).write_text("{}\n", encoding="utf-8")
        result = distribution.run_dispatch_state_migration(
            self.source_root, environ=self.environment
        )
        self.assertEqual(result["status"], "completed")

    @staticmethod
    def _serialize(value: object) -> bytes:
        return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def test_reanchored_sidecar_allows_deletion_with_extension_fields(self) -> None:
        self.assertNotEqual(self.source.read_bytes(), self.target.read_bytes())
        expected = dict(self.link)
        for key in ("completion_marker", "completion_marker_history"):
            expected[key] = str(self.target.parent / Path(self.link[key]).name)
        self.assertEqual(self.target.read_bytes(), self._serialize(expected))
        for pass_number in range(2):
            with self.subTest(pass_number=pass_number):
                # Retry must retain the correctly reanchored target as well.
                with mock.patch.dict(os.environ, self.environment, clear=True):
                    self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
                self.assertEqual(
                    distribution._migration_deletion_precondition(
                        self.candidate, self.environment
                    ),
                    (True, ""),
                )
                self.assertEqual(self.source.read_bytes(), self._serialize(self.link))

    def test_observation_collision_is_archived_without_replacing_current_bytes(self) -> None:
        relative = Path("logs/dispatch-session-sweep.log")
        source = self.source_root / relative
        target = self.target_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"legacy observation\n")
        target.write_bytes(b"current observation\n")
        root_digest = hashlib.sha256(
            os.fsencode(str(self.source_root.resolve()))
        ).hexdigest()
        content_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        archived = (Path(self.environment["HARNESS_STATE_ROOT"])
                    / "dispatch-observation-archive" / root_digest
                    / relative / content_digest)

        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
            self.assertEqual(
                distribution._migration_deletion_precondition(
                    self.candidate, self.environment
                ),
                (True, ""),
            )
            self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
        self.assertEqual(target.read_bytes(), b"current observation\n")
        self.assertEqual(archived.read_bytes(), b"legacy observation\n")

    def test_observation_written_after_carry_is_archived_by_final_delta_check(self) -> None:
        relative = Path("logs/dispatch-session-sweep.log")
        source = self.source_root / relative
        target = self.target_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"legacy observation\n")
        target.write_bytes(b"current observation\n")
        root_digest = hashlib.sha256(
            os.fsencode(str(self.source_root.resolve()))
        ).hexdigest()
        archive_root = (Path(self.environment["HARNESS_STATE_ROOT"])
                        / "dispatch-observation-archive" / root_digest
                        / relative)

        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
            source.write_bytes(b"legacy observation plus late write\n")
            self.assertEqual(
                distribution._migration_deletion_precondition(
                    self.candidate, self.environment
                ),
                (True, ""),
            )
        digests = {hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in archive_root.iterdir()}
        self.assertEqual(
            digests,
            {
                hashlib.sha256(b"legacy observation\n").hexdigest(),
                hashlib.sha256(b"legacy observation plus late write\n").hexdigest(),
            },
        )
        self.assertEqual(target.read_bytes(), b"current observation\n")

    def test_completion_proof_collision_still_blocks_release_deletion(self) -> None:
        relative = Path("completion/proof.json")
        source = self.source_root / relative
        target = self.target_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b'{"proof":"legacy"}\n')
        target.write_bytes(b'{"proof":"current"}\n')
        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
            allowed, reason = distribution._migration_deletion_precondition(
                self.candidate, self.environment
            )
        self.assertFalse(allowed)
        self.assertIn("delta-digest-mismatch", reason)
        self.assertEqual(source.read_bytes(), b'{"proof":"legacy"}\n')
        self.assertEqual(target.read_bytes(), b'{"proof":"current"}\n')
        self.assertFalse(
            (Path(self.environment["HARNESS_STATE_ROOT"])
             / "dispatch-observation-archive").exists()
        )

    def test_corrupt_archive_and_archive_write_failure_keep_the_release(self) -> None:
        relative = Path("logs/dispatch-session-sweep.log")
        source = self.source_root / relative
        target = self.target_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"legacy observation\n")
        target.write_bytes(b"current observation\n")
        root_digest = hashlib.sha256(
            os.fsencode(str(self.source_root.resolve()))
        ).hexdigest()
        content_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        archived = (Path(self.environment["HARNESS_STATE_ROOT"])
                    / "dispatch-observation-archive" / root_digest
                    / relative / content_digest)

        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertTrue(distribution._succeed_dispatch_state(self.candidate))
            archived.write_bytes(b"corrupt archive")
            self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
            self.assertFalse(distribution._migration_deletion_precondition(
                self.candidate, self.environment
            )[0])
            archived.write_bytes(b"legacy observation\n")
            # destructive-ok: reason=inject a missing archive object before the copy-failure case; boundary=one object below this test's temporary state root
            archived.unlink()
            with mock.patch.object(distribution, "_atomic_migrate_copy", return_value=False):
                self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
        self.assertEqual(source.read_bytes(), b"legacy observation\n")
        self.assertEqual(target.read_bytes(), b"current observation\n")

    def test_symlink_in_dispatch_tree_keeps_source_and_refuses_carry(self) -> None:
        relative = Path("logs/dispatch-session-sweep.log")
        source = self.source_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        outside = self.candidate.parent / "outside-observation.log"
        outside.write_bytes(b"outside bytes\n")
        source.symlink_to(outside)
        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
            allowed, reason = distribution._migration_deletion_precondition(
                self.candidate, self.environment
            )
        self.assertFalse(allowed)
        self.assertIn("delta-unreadable", reason)
        self.assertTrue(source.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside bytes\n")

    def test_symlinked_live_sidecar_is_never_reanchored_outside_target_root(self) -> None:
        relative = Path("completion/rt-test/plan.att-test.attempt.json")
        source = self.source_root / relative
        target = self.target_root / relative
        outside = self.candidate.parent / "outside-sidecar.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            json.dumps({"completion_marker": str(source.parent / "plan.json")}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        outside.write_text('{"completion_marker":"outside"}\n', encoding="utf-8")
        # destructive-ok: reason=replace the migrated fixture destination with a symlink attack; boundary=one destination below this test's temporary state root
        target.unlink()
        target.symlink_to(outside)
        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertFalse(distribution._succeed_dispatch_state(self.candidate))
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"),
                         '{"completion_marker":"outside"}\n')

    def test_other_sidecar_changes_still_block_deletion(self) -> None:
        original = self.target.read_bytes()
        mutations = {
            "evidence": {"evidence_sha256": "b" * 64},
            "boolean_type": {"registered_worker": 1},
            "extension": {"owner_closure_proof": {"reviews": [], "accepted": True}},
            "history": {"completion_marker_history": str(self.target.parent / "plan.2.json")},
            "wrong_root": {"completion_marker": str(self.source.parent / "plan.json")},
        }
        for name, changes in mutations.items():
            with self.subTest(change=name):
                changed = json.loads(original)
                changed.update(changes)
                self.target.write_bytes(self._serialize(changed))
                self.assertEqual(
                    distribution._migration_deletion_precondition(self.candidate, self.environment),
                    (False, "dispatch-state-migration-blocked-live-attempt:delta-digest-mismatch"),
                )
        self.target.write_bytes(original)
        marker = self.target.parent / "plan.json"
        marker.write_text('{"changed": true}\n', encoding="utf-8")
        self.assertFalse(distribution._migration_deletion_precondition(
            self.candidate, self.environment
        )[0], "ordinary files must still be byte-identical")

    def test_ambiguous_source_is_not_normalized_into_deletion_proof(self) -> None:
        original = self.source.read_bytes()
        duplicate = original.replace(b'"schema_version": 2,', b'"schema_version": 1, "schema_version": 2,')
        escaping = dict(self.link)
        escaping["completion_marker"] = str(self.source_root / ".." / "outside.json")
        rounded = dict(self.link, future_metadata=0.12345678901234568)
        cases = {
            "duplicate_key": duplicate,
            "non_object": b"[ ]\n",
            "escaping_path": self._serialize(escaping),
            "lossy_number": self._serialize(rounded).replace(
                b"0.12345678901234568", b"0.12345678901234567890123456789"
            ),
        }
        for name, raw in cases.items():
            with self.subTest(source=name):
                self.source.write_bytes(raw)
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    for key in ("completion_marker", "completion_marker_history"):
                        relative = distribution._relative_to_release(Path(parsed[key]), self.source_root)
                        parsed[key] = str(self.target_root / relative)
                self.target.write_bytes(self._serialize(parsed))
                self.assertFalse(distribution._migration_deletion_precondition(
                    self.candidate, self.environment
                )[0])


class ReleasePycachePruneTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.share = self.root / "share" / "hearting"
        self.releases = self.share / "releases"
        self.releases.mkdir(parents=True)
        self.state = self.root / "state" / "hearting"
        self.pycache = self.state / "pycache"

    def _cache_for(self, release: Path) -> Path:
        return self.pycache.joinpath(*self.releases.parts[1:], release.name)

    def _patch_cleanup(self, stack: ExitStack, *, in_use=(False, ""), route=([], "")) -> None:
        for name, value in {
            "data_root": self.share,
            "state_root": self.state,
            "_stable_registry_snapshot": [],
            "_open_route_launch_homes": route,
            "_release_in_use": in_use,
            "_release_held_by_live_process": (False, ""),
            "_release_projection_referenced": False,
            "_succeed_dispatch_state": True,
            "_migration_deletion_precondition": (True, ""),
            "_retention_containment_precondition": (True, ""),
        }.items():
            if callable(value):
                stack.enter_context(mock.patch.object(
                    distribution, name, side_effect=value
                ))
            else:
                stack.enter_context(mock.patch.object(
                    distribution, name, return_value=value
                ))

    def _create_release(self, name: str, timestamp: int) -> Path:
        release = self.releases / name
        release.mkdir()
        (release / "cache_probe.py").write_text("VALUE = 42\n", encoding="utf-8")
        os.utime(release, (timestamp, timestamp))
        return release

    def test_prune_removes_only_corresponding_default_pycache(self) -> None:
        old = self._create_release("v-old", 1)
        middle = self._create_release("v-middle", 2)
        newest = self._create_release("v-newest", 3)
        import_env = os.environ.copy()
        import_env["PYTHONPYCACHEPREFIX"] = str(self.pycache)
        import_env.pop("PYTHONDONTWRITEBYTECODE", None)
        imported = subprocess.run(
            [sys.executable, "-S", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import cache_probe",
             str(old)],
            capture_output=True, text=True, env=import_env, check=False,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        old_cache = self._cache_for(old)
        self.assertTrue(list(old_cache.rglob("*.pyc")))
        for release in (middle, newest):
            cache = self._cache_for(release)
            cache.mkdir(parents=True)
            (cache / "retained.pyc").write_bytes(b"retained")
        sibling = self.pycache.joinpath(*self.releases.parts[1:-1])
        sibling = sibling / "releases-neighbor" / "v-unrelated"
        sibling.mkdir(parents=True)
        (sibling / "unrelated.pyc").write_bytes(b"unrelated")

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            distribution._cleanup_releases(set())

        self.assertFalse(old.exists())
        self.assertFalse(old_cache.exists())
        self.assertTrue(middle.exists() and self._cache_for(middle).exists())
        self.assertTrue(newest.exists() and self._cache_for(newest).exists())
        self.assertTrue((sibling / "unrelated.pyc").is_file())

    def test_orphan_cache_waits_for_live_or_unresolved_release_evidence(self) -> None:
        cache = self._cache_for(self.releases / "gone")
        cache.mkdir(parents=True)
        (cache / "old.pyc").write_bytes(b"orphan")

        def prove_use(_release, _stable, route_snapshot):
            if route_snapshot[1]:
                return True, route_snapshot[1]
            return True, "open-attempt:att-live"

        for route in (([], ""), ([], "route-discovery-unreliable:/fixture")):
            with self.subTest(route=route):
                with ExitStack() as stack:
                    self._patch_cleanup(stack, in_use=prove_use, route=route)
                    distribution._cleanup_releases(set())
                self.assertTrue((cache / "old.pyc").is_file())

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            distribution._cleanup_releases(set())
        self.assertFalse(cache.exists())

    def test_failed_cache_removal_is_retried_on_the_next_cleanup(self) -> None:
        old = self._create_release("v-old", 1)
        self._create_release("v-middle", 2)
        self._create_release("v-newest", 3)
        cache = self._cache_for(old)
        cache.mkdir(parents=True)
        (cache / "old.pyc").write_bytes(b"old")
        real_rmtree = distribution.shutil.rmtree

        def fail_cache(path, *args, **kwargs):
            if Path(path) == cache:
                raise PermissionError("fixture cache failure")
            return real_rmtree(path, *args, **kwargs)

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            stack.enter_context(mock.patch.object(
                distribution.shutil, "rmtree", side_effect=fail_cache
            ))
            distribution._cleanup_releases(set())
        self.assertFalse(old.exists())
        self.assertTrue((cache / "old.pyc").is_file())

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            distribution._cleanup_releases(set())
        self.assertFalse(cache.exists())

    def test_release_removal_failure_keeps_its_derived_cache(self) -> None:
        old = self._create_release("v-old", 1)
        self._create_release("v-middle", 2)
        self._create_release("v-newest", 3)
        cache = self._cache_for(old)
        cache.mkdir(parents=True)
        (cache / "old.pyc").write_bytes(b"old")

        def fail_release(path, *_args, **_kwargs):
            if Path(path) == old:
                raise PermissionError("fixture release failure")
            return None

        with ExitStack() as stack:
            self._patch_cleanup(stack)
            stack.enter_context(mock.patch.object(
                distribution.shutil, "rmtree", side_effect=fail_release
            ))
            distribution._cleanup_releases(set())
        self.assertTrue(old.is_dir())
        self.assertTrue((cache / "old.pyc").is_file())


class ActivationFailureDiagnosticTest(unittest.TestCase):
    def test_stdout_json_precedence_and_last_lines_entry(self) -> None:
        self.assertEqual(
            distribution._activation_failure_detail(
                '{"error":"typed error", "detail":"detail", "lines":["old", "last"]}',
                "stderr terminal",
            ),
            "typed error",
        )
        self.assertEqual(
            distribution._activation_failure_detail(
                '{"error":12, "detail": "", "lines":["old", "last"]}',
                "stderr terminal",
            ),
            "last",
        )

    def test_malformed_json_uses_terminal_stderr_then_stdout(self) -> None:
        self.assertEqual(
            distribution._activation_failure_detail('{"error":', "first\nstderr terminal\n"),
            "stderr terminal",
        )
        self.assertEqual(
            distribution._activation_failure_detail("stdout terminal\n", "\n"),
            "stdout terminal",
        )

    def test_tail_bound_controls_redaction_and_unicode_limit(self) -> None:
        stdout = "prefix " + ("x" * 70000) + "\n" + (
            "Error: token = abc password:'def' secret: ghi "
            "authorization=Bearer%20abc api-key=q "
            "https://user:pass@example.test/path?token=abc&x=yz\n"
        )
        detail = distribution._activation_failure_detail(stdout, "")
        self.assertLessEqual(len(detail), 1000)
        self.assertNotIn("abc", detail)
        self.assertNotIn("def", detail)
        self.assertNotIn("ghi", detail)
        self.assertNotIn("user:pass@", detail)
        self.assertIn("[REDACTED]", detail)
        self.assertIn("example.test/path", detail)
        self.assertIn("?token=[REDACTED]&x=[REDACTED]", detail)
        self.assertNotIn("\n", detail)
        self.assertEqual(
            distribution._activation_failure_detail("\u0000\u200bterminal\tpath", ""),
            "terminal path",
        )


if __name__ == "__main__":
    unittest.main()
