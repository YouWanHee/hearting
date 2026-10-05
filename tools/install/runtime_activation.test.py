#!/usr/bin/env python3
import os
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_activation as activation  # noqa: E402
import installer  # noqa: E402
import fixture_env  # noqa: E402


class RuntimeSnapshotTest(unittest.TestCase):
    # destructive-ok: reason=exercise added and deleted source fingerprints; boundary=entry.py and new_entry.py under this test TemporaryDirectory
    def test_launch_revision_tracks_source_but_not_unversioned_work_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            (root / "utilities").mkdir()
            source = root / "utilities" / "entry.py"
            source.write_text("original\n")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org", "commit", "-qm", "base")
            initial = activation.source_revision(root, runtime_launch=True)
            (root / "final_report.md").write_text("work output\n")
            self.assertEqual(activation.source_revision(root, runtime_launch=True), initial)
            # Both modes now share one root-level filter (`_release_content_skip`):
            # an untracked top-level work output is not release content in either
            # mode, so the default call agrees with the launch call here too.
            self.assertEqual(activation.source_revision(root), initial)
            addition = root / "utilities" / "new_entry.py"
            addition.write_text("new runtime code\n")
            self.assertNotEqual(activation.source_revision(root, runtime_launch=True), initial)
            addition.unlink()
            source.write_text("changed\n")
            self.assertNotEqual(activation.source_revision(root, runtime_launch=True), initial)
            source.unlink()
            self.assertNotEqual(activation.source_revision(root, runtime_launch=True), initial)
    def test_release_revision_ignores_runtime_grounding_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "RELEASE_VERSION").write_text("v1.2.3\n", encoding="utf-8")
            (root / "core").mkdir()
            (root / "core" / "CORE.md").write_text("stable\n", encoding="utf-8")
            expected = activation.source_revision(root)

            for marker in (
                ".capability-grounding",
                ".route-grounding",
                ".core-grounding",
                ".spec-grounding",
            ):
                marker_root = root / marker
                marker_root.mkdir()
                (marker_root / "session.json").write_text("{}\n", encoding="utf-8")
                self.assertEqual(activation.source_revision(root), expected, marker)

            (root / "core" / "CORE.md").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(activation.source_revision(root), expected)

    def test_runtime_activate_defaults_to_packaged_snapshot(self):
        args = installer.build_parser().parse_args(
            ["runtime", "activate", "--runtime", "codex"]
        )
        self.assertEqual(args.mode, "packaged")

    def test_snapshot_and_restore_preserve_live_managed_session_sockets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".harness"
            managed = state / "managed-sessions" / "live"
            managed.mkdir(parents=True)
            current = state / "activation.json"
            current.write_text("before\n", encoding="utf-8")
            socket_path = managed / "app-server.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            try:
                record = activation._copy_snapshot(
                    state, root / "backup", 0,
                    preserve_names=("managed-sessions",),
                )
                self.assertFalse(
                    (Path(record["backup"]) / "managed-sessions").exists())
                current.write_text("after\n", encoding="utf-8")
                (state / "new-projection").write_text("remove me\n", encoding="utf-8")
                record["postimage"] = activation.safe_fs.capture_state(
                    state, exclude_names=("managed-sessions",)
                ).public()
                activation._restore([record])
                self.assertEqual(current.read_text(encoding="utf-8"), "before\n")
                self.assertFalse((state / "new-projection").exists())
                self.assertTrue(socket_path.is_socket())
            finally:
                listener.close()

    def test_preserved_runtime_directory_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / ".harness"
            state.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (state / "managed-sessions").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(activation.ActivationError):
                activation._copy_snapshot(
                    state, root / "backup", 0,
                    preserve_names=("managed-sessions",),
                )


class ReleaseContentSkipTest(unittest.TestCase):
    """`_release_content_skip` is the one filter cleanliness, copy, and the
    bundle key share: a root-level untracked item (e.g. an unversioned
    `dist/` build output) is not release content anywhere."""

    def _git_checkout(self, root: Path) -> Path:
        checkout = root / "checkout"
        checkout.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(checkout), *args], check=True, capture_output=True)

        git("init", "-q")
        (checkout / "utilities").mkdir()
        (checkout / "utilities" / "tool.py").write_text("print(1)\n", encoding="utf-8")
        git("add", ".")
        git(
            "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org",
            "commit", "-qm", "base",
        )
        return checkout

    def test_packaged_ignores_root_untracked_release_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = self._git_checkout(root)
            dist = checkout / "dist"
            dist.mkdir()
            (dist / "a.tgz").write_bytes(b"binary")

            revision = activation.source_revision(checkout)
            self.assertNotIn("+dirty:", revision)

            state_home = root / "codex-home"
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = str(state_home)
            try:
                bundle_source = activation._build_bundle("codex", checkout, revision, "global")
            finally:
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous

            self.assertFalse((bundle_source / "dist").exists())
            self.assertTrue((bundle_source / "utilities" / "tool.py").is_file())

    def test_source_dir_untracked_file_stays_dirty_in_both_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = self._git_checkout(root)
            baseline_launch = activation.source_revision(checkout, runtime_launch=True)
            baseline_default = activation.source_revision(checkout)
            (checkout / "utilities" / "new.py").write_text("print(2)\n", encoding="utf-8")
            self.assertNotEqual(
                activation.source_revision(checkout, runtime_launch=True), baseline_launch
            )
            self.assertNotEqual(activation.source_revision(checkout), baseline_default)


class SurfaceSkewTest(unittest.TestCase):
    """The four install surfaces are updated by two different commands and drift apart.

    Regression bar for 2026-08-19: a runtime activation advanced ~/.claude while the
    managed release tree stayed behind, so `fleet` — which resolves through
    ~/.local/share/hearting/current — kept running the old code while every existing
    check reported success. The release surface appeared in no diagnostic at all.
    """

    def _surface(self, root: Path, subtree_body: str, marker: str = None) -> Path:
        (root / "tools").mkdir(parents=True)
        (root / "tools" / "fleet.py").write_text(subtree_body, encoding="utf-8")
        for name in ("adapters", "capabilities", "core", "hooks", "roles", "utilities"):
            (root / name).mkdir()
            (root / name / "shared.md").write_text("same everywhere\n", encoding="utf-8")
        if marker is not None:
            (root / ".hearting-release.json").write_text(
                '{"version": "%s"}\n' % marker, encoding="utf-8"
            )
        return root

    def test_identical_content_across_surface_kinds_is_not_skew(self):
        with tempfile.TemporaryDirectory() as temporary:
            # Same code, different surface kind: only the release carries the release
            # marker, exactly as on disk. A whole-tree digest calls that a difference and
            # would report permanent false skew — which is why subtrees are compared.
            release = self._surface(Path(temporary) / "release", "same\n", marker="v1.0.0")
            bundle = self._surface(Path(temporary) / "bundle", "same\n")
            self.assertNotEqual(
                activation._tree_digest(release), activation._tree_digest(bundle),
                "whole-tree digests differ across kinds — the reason subtrees are used",
            )
            self.assertEqual(
                activation._surface_digests(release), activation._surface_digests(bundle)
            )

    def test_release_behind_a_runtime_is_reported_and_names_the_subtree(self):
        with tempfile.TemporaryDirectory() as temporary:
            release = self._surface(Path(temporary) / "release", "old\n", marker="v1.0.0")
            current = self._surface(Path(temporary) / "runtime", "new\n")
            original = activation._load_json

            def fake_load(path):
                if path.name == "activation.json":
                    return {"active_root": str(current), "active_revision": "abc123"}
                return original(path)

            activation._load_json = fake_load
            try:
                report = activation.surface_skew(release)
            finally:
                activation._load_json = original

            self.assertFalse(report["ok"])
            self.assertEqual([entry["subtree"] for entry in report["skewed"]], ["tools"])
            groups = report["skewed"][0]["groups"]
            self.assertIn(["release"], groups)
            self.assertIn(sorted(activation.RUNTIMES), groups)
            self.assertIn("release", report["compared"])

    def test_loop_runtime_logs_do_not_read_as_skew(self):
        """A bundle copied from a checkout carries git-ignored loop logs; a release never
        does. Digesting them reported permanent `adapters` skew with identical code."""
        with tempfile.TemporaryDirectory() as temporary:
            release = self._surface(Path(temporary) / "release", "same\n", marker="v1.0.0")
            bundle = self._surface(Path(temporary) / "bundle", "same\n")
            # `loops/` itself is tracked (README, .gitignore, drill cases), so it exists in
            # BOTH trees; only the run output inside it is ephemeral and bundle-only.
            for root in (release, bundle):
                (root / "adapters" / "loops").mkdir(parents=True)
                (root / "adapters" / "loops" / "README.md").write_text(
                    "tracked\n", encoding="utf-8"
                )
            (bundle / "adapters" / "loops" / "oncall.log").write_text(
                "run output\n", encoding="utf-8"
            )
            self.assertEqual(
                activation._surface_digests(release)["adapters"],
                activation._surface_digests(bundle)["adapters"],
            )
            # A tracked `.log` fixture is source and must still count.
            fixture = bundle / "adapters" / "tests" / "fixtures"
            fixture.mkdir(parents=True)
            (fixture / "jobs_route.log").write_text("fixture\n", encoding="utf-8")
            self.assertNotEqual(
                activation._surface_digests(release)["adapters"],
                activation._surface_digests(bundle)["adapters"],
            )

    def test_default_tree_digest_is_unchanged_by_the_skip_hook(self):
        """`_bundle_checksum` persists this value in bundle metadata; adding the optional
        filter must not move it for callers that pass no filter."""
        with tempfile.TemporaryDirectory() as temporary:
            root = self._surface(Path(temporary) / "tree", "body\n")
            loops = root / "adapters" / "loops"
            loops.mkdir(parents=True)
            (loops / "oncall.log").write_text("run output\n", encoding="utf-8")
            with_log = activation._tree_digest(root)
            (loops / "oncall.log").write_text("different\n", encoding="utf-8")
            self.assertNotEqual(with_log, activation._tree_digest(root))

    def test_absent_surface_is_not_skew(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "never-installed"
            original = activation._load_json
            activation._load_json = lambda path: None
            try:
                report = activation.surface_skew(missing)
            finally:
                activation._load_json = original
            self.assertTrue(report["ok"])
            self.assertEqual(report["compared"], [])
            self.assertTrue(all(not s["present"] for s in report["surfaces"]))


class BundleRuntimeStateTest(unittest.TestCase):
    """A release bundle is immutable; runtime state written inside it is a finding."""

    def _bundle(self, root: Path) -> Path:
        source = root / "runtime-home" / ".harness" / "bundles" / "release-v1-aaa" / "source"
        (source / "utilities").mkdir(parents=True)
        return source

    def test_reports_state_written_inside_the_active_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self._bundle(Path(temporary))
            governor = ".runtime/model-worker-governor"
            (source / ".agent_reports" / governor).mkdir(parents=True)
            (source / "utilities" / ".agent_reports" / governor).mkdir(parents=True)
            found = activation.bundle_runtime_state(source)
            self.assertEqual(
                found,
                sorted(
                    [
                        str(source / ".agent_reports"),
                        str(source / "utilities" / ".agent_reports"),
                    ]
                ),
            )

    def test_clean_bundle_reports_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self._bundle(Path(temporary))
            self.assertEqual(activation.bundle_runtime_state(source), [])

    def test_a_linked_checkout_is_never_scanned(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            (checkout / ".agent_reports").mkdir(parents=True)
            self.assertEqual(activation.bundle_runtime_state(checkout), [])

    def test_nested_state_is_not_double_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = self._bundle(Path(temporary))
            nested = source / ".agent_reports" / "inner" / ".claude_reports"
            nested.mkdir(parents=True)
            self.assertEqual(
                activation.bundle_runtime_state(source),
                [str(source / ".agent_reports")],
            )


class LinkedReleaseBundleTest(unittest.TestCase):
    """Defect Q: a packaged bundle copied from an immutable managed release gave
    one content two paths, and `resolve_agent_home()` ranks the runtime's own
    bundle pointer above the managed `current` pointer -- so codex/opencode
    resolved AGENT_HOME to the bundle while the registry sealed the release, and
    every launch without an explicit AGENT_HOME failed launch-runtime-root-mismatch."""

    def _release(self, root: Path, version: str = "v9.9.9") -> Path:
        release = root / "releases" / version
        (release / "core").mkdir(parents=True)
        (release / "core" / "CORE.md").write_text("release core\n", encoding="utf-8")
        (release / "utilities").mkdir()
        (release / "utilities" / "tool.py").write_text("print(1)\n", encoding="utf-8")
        (release / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
        return release

    def _checkout(self, root: Path) -> Path:
        checkout = root / "checkout"
        (checkout / "core").mkdir(parents=True)
        (checkout / "core" / "CORE.md").write_text("dev core\n", encoding="utf-8")
        return checkout

    def _build(self, source: Path, state_home: Path, runtime: str = "codex"):
        import os
        previous = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(state_home)
        try:
            revision = activation.source_revision(source)
            return activation._build_bundle(runtime, source, revision, "global"), revision
        finally:
            if previous is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous

    def test_a_managed_release_is_linked_not_copied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, revision = self._build(release, root / "codex-home")
            self.assertTrue(revision.startswith("release:"), revision)
            self.assertTrue(bundle_source.is_symlink())
            self.assertEqual(
                Path(os.path.realpath(bundle_source)), Path(os.path.realpath(release))
            )
            # The link is the whole bundle payload: nothing was duplicated.
            self.assertFalse((bundle_source.parent / "source" / "utilities").is_symlink())
            self.assertTrue((bundle_source / "utilities" / "tool.py").is_file())

    def test_the_bundle_pointer_and_the_release_are_one_object(self):
        # The exact condition defect Q needed: two paths, one content. After the
        # fix the two candidate AGENT_HOME values name one filesystem object, so
        # every consumer that resolves a path (launch tuple, runtime-root guard)
        # reaches the same identity and the mismatch cannot be constructed.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, _ = self._build(release, root / "codex-home")
            self.assertNotEqual(str(bundle_source), str(release))
            self.assertTrue(activation._same_tree(bundle_source, release))
            self.assertEqual(
                (bundle_source / "core" / "CORE.md").read_text(encoding="utf-8"),
                (release / "core" / "CORE.md").read_text(encoding="utf-8"),
            )

    def test_a_dev_checkout_is_still_copied(self):
        # A checkout is mutable, so the bundle must still freeze it.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = self._checkout(root)
            bundle_source, revision = self._build(checkout, root / "codex-home")
            self.assertFalse(revision.startswith("release:"), revision)
            self.assertFalse(bundle_source.is_symlink())
            self.assertTrue((bundle_source / "core" / "CORE.md").is_file())
            checkout_core = checkout / "core" / "CORE.md"
            checkout_core.write_text("mutated\n", encoding="utf-8")
            self.assertEqual(
                (bundle_source / "core" / "CORE.md").read_text(encoding="utf-8"),
                "dev core\n",
            )

    def test_rebuilding_reuses_the_link_and_repairs_a_repointed_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, _ = self._build(release, root / "codex-home")
            again, _ = self._build(release, root / "codex-home")
            self.assertEqual(bundle_source, again)
            elsewhere = self._release(root, "v8.8.8")
            # destructive-ok: reason=repoint the bundle link so the repair path has something stale to fix; boundary=the one symlink this test just created under its own temporary codex home
            bundle_source.unlink()
            os.symlink(elsewhere, bundle_source, target_is_directory=True)
            repaired, _ = self._build(release, root / "codex-home")
            self.assertTrue(activation._same_tree(repaired, release))

    def test_discarding_a_linked_bundle_never_removes_the_release(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, _ = self._build(release, root / "codex-home")
            bundles = bundle_source.parent.parent
            # destructive-ok: reason=prove the linked release survives losing its bundle store; boundary=the bundle root inside this test's own temporary directory
            shutil.rmtree(bundles)
            self.assertFalse(bundles.exists())
            self.assertTrue(release.is_dir())
            self.assertTrue((release / "utilities" / "tool.py").is_file())

    def test_a_linked_bundle_is_not_scanned_as_bundle_residue(self):
        # bundle_runtime_state walks the active bundle to find runtime state
        # written inside an immutable tree. A linked bundle owns no tree, so
        # walking it would descend into the release and report the release's own
        # contents as this bundle's residue.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            (release / ".agent_reports").mkdir()
            bundle_source, _ = self._build(release, root / "codex-home")
            self.assertEqual(activation.bundle_runtime_state(bundle_source), [])
            # A copied bundle keeps reporting exactly as before. The residue has
            # to be written after the build: `_bundle_ignore` never copies it in.
            checkout = self._checkout(root)
            copied, _ = self._build(checkout, root / "codex-home-2")
            (copied / ".agent_reports").mkdir()
            self.assertEqual(
                activation.bundle_runtime_state(copied), [str(copied / ".agent_reports")]
            )

    def test_linked_bundle_checksum_still_asserts_content(self):
        # Review S1: verifying only the link made `bundle_stale` unfalsifiable —
        # status compares this value against the one it came from — so a release
        # replaced in place under the same version tag would have read as fresh.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, _ = self._build(release, root / "codex-home")
            self.assertIsInstance(activation._bundle_checksum(bundle_source), str)
            (release / "utilities" / "tool.py").write_text("print(2)\n", encoding="utf-8")
            self.assertIsNone(activation._bundle_checksum(bundle_source))

    def test_linked_bundle_checksum_tracks_the_link_not_a_tree_walk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            bundle_source, _ = self._build(release, root / "codex-home")
            checksum = activation._bundle_checksum(bundle_source)
            self.assertIsInstance(checksum, str)
            (release / "utilities" / "__pycache__").mkdir()
            (release / "utilities" / "__pycache__" / "tool.pyc").write_bytes(b"\x00")
            self.assertEqual(activation._bundle_checksum(bundle_source), checksum)
            elsewhere = self._release(root, "v7.7.7")
            # destructive-ok: reason=repoint the bundle link to make the checksum stale; boundary=the one symlink this test just created under its own temporary codex home
            bundle_source.unlink()
            os.symlink(elsewhere, bundle_source, target_is_directory=True)
            self.assertIsNone(activation._bundle_checksum(bundle_source))


class RuntimeActivationOwnershipTest(unittest.TestCase):
    def test_capture_excludes_runtime_local_harness_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            with fixture_env.patched_environment(root / "fixture", source):
                state_dir = activation.paths.harness_state_dir("codex")
                local_dispatch = state_dir / "dispatch" / "jobs.log"
                managed = state_dir / "managed-sessions" / "live.json"
                sibling = state_dir / "unknown-sibling" / "value"
                local_dispatch.parent.mkdir(parents=True)
                managed.parent.mkdir(parents=True)
                sibling.parent.mkdir(parents=True)
                local_dispatch.write_text("local row\n", encoding="utf-8")
                managed.write_text("session\n", encoding="utf-8")
                sibling.write_text("unknown\n", encoding="utf-8")
                snapshot = activation.capture_runtime_state("codex")
                self.addCleanup(activation.discard_runtime_state, snapshot)
                destinations = {Path(item["dest"]) for item in snapshot["records"]}
                self.assertNotIn(state_dir, destinations)
                self.assertNotIn(local_dispatch, destinations)
                self.assertNotIn(managed, destinations)
                self.assertNotIn(sibling, destinations)
                self.assertIn(state_dir / "transactions", destinations)


class RuntimeActivationConflictPreflightTest(unittest.TestCase):
    def test_codex_conflict_refuses_before_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            with fixture_env.patched_environment(root / "fixture", source):
                config = activation.paths.runtime_home("codex") / "config.toml"
                config.parent.mkdir(parents=True)
                config.write_text(
                    '[plugins."hearting-codex@fixture"]\nenabled = true\n',
                    encoding="utf-8",
                )
                before = config.read_bytes()
                with self.assertRaisesRegex(activation.ActivationError, "hearting-codex"):
                    activation.validate_request(
                        "codex", "activate", mode="linked", source=str(source)
                    )
                self.assertEqual(config.read_bytes(), before)

    def test_refresh_conflict_refuses_before_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            with fixture_env.patched_environment(root / "fixture", source):
                state_path = activation._state_path("codex")
                state_path.parent.mkdir(parents=True)
                state_path.write_text(
                    json.dumps({"mode": "linked", "source_root": str(source)}),
                    encoding="utf-8",
                )
                config = activation.paths.runtime_home("codex") / "config.toml"
                config.write_text(
                    '[plugins."hearting-codex"]\n', encoding="utf-8"
                )
                with self.assertRaisesRegex(activation.ActivationError, "hearting-codex"):
                    activation.validate_request("codex", "refresh")

class ClaudeHookGroupIdentityTest(unittest.TestCase):
    """A hook group the user tuned is the same group, not a foreign one."""

    PROJECTION = {"type": "command", "command": "python3 \"$HOME/.claude/hooks/projection.py\"", "timeout": 5}
    NUDGE = {"type": "command", "command": "bash \"$HOME/.claude/hooks/nudge.sh\"", "timeout": 10}

    def _release(self, root: Path, name: str, prompt_groups: list) -> Path:
        release = root / name
        (release / "adapters/claude").mkdir(parents=True)
        (release / "adapters/claude/settings.json").write_text(json.dumps({
            "hooks": {"UserPromptSubmit": prompt_groups},
            "statusLine": {"type": "command", "command": activation.CLAUDE_STATUSLINE_COMMAND},
            "autoMemoryEnabled": False,
            "env": {key: "1" for key in activation.CLAUDE_MANAGED_ENV_KEYS},
        }), encoding="utf-8")
        return release

    def _merge(self, config: Path, release: Path, previous_release: Path) -> dict:
        previous = {"managed_config": {"claude_hooks": json.loads(
            (previous_release / "adapters/claude/settings.json").read_text(encoding="utf-8")
        )["hooks"]}}
        original = activation._config_path
        activation._config_path = lambda *_args, **_kwargs: config
        try:
            return activation._merge_claude_settings(release, previous)
        finally:
            activation._config_path = original

    def test_a_tuned_group_is_kept_once_and_a_retired_one_leaves(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = self._release(root, "old", [{"hooks": [self.PROJECTION]}, {"hooks": [self.NUDGE]}])
            new = self._release(root, "new", [{"hooks": [self.PROJECTION]}])
            tuned_projection = {"hooks": [dict(self.PROJECTION, timeout=30, **{"async": True})]}
            tuned_nudge = {"hooks": [dict(self.NUDGE, timeout=30)]}
            user_group = {"hooks": [{"type": "command", "command": "my-own-hook"}]}
            config = root / "settings.json"
            config.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
                tuned_projection, tuned_nudge, user_group,
            ]}}), encoding="utf-8")

            result = self._merge(config, new, old)

            groups = json.loads(config.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
            # The tuned projection group stays as the user left it and the
            # release copy is not appended beside it.
            self.assertEqual(groups, [tuned_projection, user_group])
            self.assertEqual(result["added"], 0)

            # A second install of the same release changes nothing.
            self._merge(config, new, new)
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"],
                [tuned_projection, user_group],
            )

    def test_an_untuned_group_still_takes_the_new_release_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = self._release(root, "old", [{"hooks": [self.PROJECTION]}])
            changed = {"hooks": [dict(self.PROJECTION, timeout=12)]}
            new = self._release(root, "new", [changed])
            config = root / "settings.json"
            config.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
                {"hooks": [self.PROJECTION]},
            ]}}), encoding="utf-8")

            self._merge(config, new, old)

            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"],
                [changed],
            )

    def test_a_group_the_user_trimmed_does_not_run_its_remaining_hook_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = {"matcher": "*", "hooks": [self.PROJECTION, self.NUDGE]}
            old = self._release(root, "old", [pair])
            new = self._release(root, "new", [pair])
            trimmed = {"matcher": "*", "hooks": [dict(self.PROJECTION, timeout=30)]}
            config = root / "settings.json"
            config.write_text(json.dumps({"hooks": {"UserPromptSubmit": [trimmed]}}), encoding="utf-8")

            self._merge(config, new, old)

            groups = json.loads(config.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
            # The kept hook stays tuned and only the missing one comes back.
            self.assertEqual(groups, [trimmed, {"matcher": "*", "hooks": [self.NUDGE]}])

    def test_uninstall_removes_tuned_and_trimmed_copies_but_not_user_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = [{"matcher": "*", "hooks": [self.PROJECTION, self.NUDGE]}]
            user_group = {"hooks": [{"type": "command", "command": "my-own-hook"}]}
            mixed = {"matcher": "*", "hooks": [dict(self.NUDGE, timeout=1), {"type": "command", "command": "mine-too"}]}
            config = root / "settings.json"
            config.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
                {"matcher": "*", "hooks": [dict(self.PROJECTION, timeout=30)]}, mixed, user_group,
            ]}}), encoding="utf-8")
            original = activation._config_path
            activation._config_path = lambda *_args, **_kwargs: config
            try:
                activation._unmerge_claude_settings(
                    {"managed_config": {"claude_hooks": {"UserPromptSubmit": managed}}}, "global"
                )
            finally:
                activation._config_path = original

            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"],
                [{"matcher": "*", "hooks": [{"type": "command", "command": "mine-too"}]}, user_group],
            )


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_tmp(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class ClaudeManagedEnvTest(unittest.TestCase):
    """Claude's settings `env` blanks inherited caller names and foreign ids; activation installs it without clobbering."""

    KEYS = ("AGENT_DISPATCH_CALLER_HARNESS", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID")

    def _release(self, root: Path) -> Path:
        release = root / "release"
        (release / "adapters/claude").mkdir(parents=True)
        shipped = json.loads((REPO_ROOT / "adapters/claude/settings.json").read_text(encoding="utf-8"))
        (release / "adapters/claude/settings.json").write_text(json.dumps({
            "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "true"}]}]},
            "statusLine": {"type": "command", "command": activation.CLAUDE_STATUSLINE_COMMAND},
            "autoMemoryEnabled": False,
            "env": shipped.get("env", {}),
        }), encoding="utf-8")
        return release

    def _merge(self, config: Path, release: Path, previous=None) -> dict:
        original = activation._config_path
        activation._config_path = lambda *_args, **_kwargs: config
        try:
            return activation._merge_claude_settings(release, previous)
        finally:
            activation._config_path = original

    def test_the_shipped_settings_manage_the_four_identity_keys(self):
        self.assertEqual(set(activation.CLAUDE_MANAGED_ENV_KEYS), set(self.KEYS))

    def test_merge_adds_the_keys_and_keeps_an_unrelated_user_env_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            config = root / "settings.json"
            config.write_text(json.dumps({"env": {"MY_OWN": "keep"}}), encoding="utf-8")
            result = self._merge(config, release)
            env = json.loads(config.read_text(encoding="utf-8"))["env"]
            for key in self.KEYS:
                self.assertEqual(env[key], "", key)
            self.assertEqual(env["MY_OWN"], "keep")
            self.assertEqual(result["conflicts"], [])

    def test_a_user_changed_value_is_a_reported_conflict_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            config = root / "settings.json"
            config.write_text(json.dumps({"env": {"AGENT_DISPATCH_CALLER_HARNESS": "mine"}}),
                              encoding="utf-8")
            result = self._merge(config, release)
            self.assertIn("env.AGENT_DISPATCH_CALLER_HARNESS", result["conflicts"])
            self.assertEqual(
                json.loads(config.read_text(encoding="utf-8"))["env"]["AGENT_DISPATCH_CALLER_HARNESS"],
                "mine")


class CodexIdentityConfigTest(unittest.TestCase):
    """Codex clears inherited identity through one delimited block in `$CODEX_HOME/config.toml`."""

    BEGIN = "# >>> hearting harness identity (managed by runtime activation; edit outside this block) >>>"
    END = "# <<< hearting harness identity <<<"
    EXCLUDED = ("AGENT_DISPATCH_CALLER_HARNESS", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                "OPENCODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION")

    def _release(self, root: Path) -> Path:
        release = root / "release"
        fragment = release / "adapters/codex/config"
        fragment.mkdir(parents=True)
        (fragment / "harness-identity.toml").write_bytes(
            (REPO_ROOT / "adapters/codex/config/harness-identity.toml").read_bytes())
        return release

    def _call(self, name, config: Path, *args):
        original = activation._config_path
        activation._config_path = lambda *_a, **_k: config
        try:
            return getattr(activation, name)(*args)
        finally:
            activation._config_path = original

    def _merge(self, config, release, previous=None):
        return self._call("_merge_codex_identity_config", config, release, previous, "global")

    def _unmerge(self, config, state, dry_run=False):
        return self._call("_unmerge_codex_identity_config", config, state, "global", dry_run)

    def _effective(self, config: Path):
        import tomllib
        policy = tomllib.loads(config.read_text(encoding="utf-8")).get("shell_environment_policy", {})
        return policy.get("set", {}), policy.get("filters", {})

    def _assert_clears_inherited(self, config: Path):
        values, filters = self._effective(config)
        self.assertNotIn("AGENT_DISPATCH_CALLER_HARNESS", values)
        self.assertNotIn("AGENT_DISPATCH_CURRENT_HARNESS", filters)
        for key in self.EXCLUDED:
            self.assertEqual(filters.get(key), "exclude", key)

    def test_an_absent_config_gets_the_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "home" / "config.toml"
            result = self._merge(config, self._release(root))
            self.assertEqual(result["kind"], "codex-config-merged")
            self.assertEqual(result["conflicts"], [])
            self.assertIn(self.BEGIN, config.read_text(encoding="utf-8"))
            self._assert_clears_inherited(config)

    def test_the_block_is_appended_after_user_content_and_the_rest_stays_runtime_owned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            original = 'model = "x"\n\n[projects."/a"]\ntrust_level = "trusted"\n'
            config.write_text(original, encoding="utf-8")
            self._merge(config, self._release(root))
            text = config.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(original))
            self._assert_clears_inherited(config)
            self.assertTrue((root / "config.toml.pre-harness-identity").is_file())
            self.assertEqual((root / "config.toml.pre-harness-identity").read_text(encoding="utf-8"), original)

    def test_a_second_merge_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text('model = "x"\n', encoding="utf-8")
            release = self._release(root)
            first = self._merge(config, release)
            once = config.read_bytes()
            self._merge(config, release, {"managed_config": {"codex_identity": first["managed_config"]}})
            self.assertEqual(config.read_bytes(), once)

    def test_a_user_policy_with_legacy_arrays_or_a_partial_filters_table_is_a_conflict_and_unchanged(self):
        for label, body in (
            ("exclude", '[shell_environment_policy]\nexclude = ["AWS_*"]\n'),
            ("include_only", '[shell_environment_policy]\ninclude_only = ["PATH"]\n'),
            ("inline filters", '[shell_environment_policy]\nfilters = { "FOO" = "exclude" }\n'),
            ("partial filters", '[shell_environment_policy.filters]\n"FOO" = "exclude"\n'),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config.toml"
                config.write_text(body, encoding="utf-8")
                result = self._merge(config, self._release(root))
                self.assertTrue(result["conflicts"], label)
                self.assertEqual(config.read_text(encoding="utf-8"), body)

    def test_a_user_table_that_already_has_the_effective_values_is_satisfied_without_a_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            body = ('[shell_environment_policy]\ninherit = "core"\n'
                    '[shell_environment_policy.set]\nFOO = "1"\n'
                    '[shell_environment_policy.filters]\n'
                    + "".join('"%s" = "exclude"\n' % key for key in self.EXCLUDED))
            config.write_text(body, encoding="utf-8")
            result = self._merge(config, self._release(root))
            self.assertEqual(result["conflicts"], [])
            self.assertIsNone(result["managed_config"])
            self.assertEqual(config.read_text(encoding="utf-8"), body)

    def test_a_user_set_table_is_not_a_conflict_and_stays_untouched(self):
        # The block defines only `filters`; a user `set` (even one naming a caller on purpose)
        # lives beside it and Codex applies `set` after the exclusions.
        for body in ('[shell_environment_policy]\nset = { FOO = "1" }\n',
                     '[shell_environment_policy.set]\nAGENT_DISPATCH_CALLER_HARNESS = "claude"\n'):
            with self.subTest(body), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config.toml"
                config.write_text(body, encoding="utf-8")
                result = self._merge(config, self._release(root))
                self.assertEqual(result["conflicts"], [])
                self.assertTrue(config.read_text(encoding="utf-8").startswith(body))
                self.assertEqual(self._effective(config)[0], self._effective(
                    _write_tmp(root / "user-only.toml", body))[0])
                _, filters = self._effective(config)
                for key in self.EXCLUDED:
                    self.assertEqual(filters.get(key), "exclude", key)

    def test_a_block_from_the_previous_declaration_is_replaced_not_stacked(self):
        # An earlier build of this block also set the caller name; refresh swaps in the current text.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            release = self._release(root)
            old_inserted = (
                "\n" + self.BEGIN + '\n[shell_environment_policy.set]\nAGENT_DISPATCH_CALLER_HARNESS = "codex"\n'
                '[shell_environment_policy.filters]\n"CLAUDE_CODE_SESSION_ID" = "exclude"\n' + self.END + "\n")
            config.write_text('model = "x"\n' + old_inserted, encoding="utf-8")
            previous = {"managed_config": {"codex_identity": {"inserted": old_inserted}}}
            result = self._merge(config, release, previous)
            self.assertEqual(result["conflicts"], [])
            text = config.read_text(encoding="utf-8")
            self.assertEqual(text.count(self.BEGIN), 1)
            self.assertNotIn("AGENT_DISPATCH_CALLER_HARNESS = ", text)
            self._assert_clears_inherited(config)

    def test_a_user_policy_table_without_set_or_filters_gets_the_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text('[shell_environment_policy]\ninherit = "all"\n', encoding="utf-8")
            result = self._merge(config, self._release(root))
            self.assertEqual(result["conflicts"], [])
            self._assert_clears_inherited(config)

    def test_deactivate_removes_only_the_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            original = 'model = "x"\n\n[projects."/a"]\ntrust_level = "trusted"\n'
            config.write_text(original, encoding="utf-8")
            result = self._merge(config, self._release(root))
            changed = self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}})
            self.assertEqual(changed, [str(config)])
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_deactivate_removes_a_config_activation_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "home" / "config.toml"
            result = self._merge(config, self._release(root))
            changed = self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}})
            self.assertEqual(changed, [str(config)])
            self.assertFalse(config.exists())

    def test_deactivate_keeps_an_empty_config_the_user_had(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text("", encoding="utf-8")
            result = self._merge(config, self._release(root))
            self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}})
            self.assertTrue(config.is_file())
            self.assertEqual(config.read_text(encoding="utf-8"), "")

    def test_deactivate_leaves_a_block_the_user_edited(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            result = self._merge(config, self._release(root))
            edited = config.read_text(encoding="utf-8").replace('"CLAUDECODE"', '"MINE"')
            config.write_text(edited, encoding="utf-8")
            self.assertEqual(
                self._unmerge(config, {"managed_config": {"codex_identity": result["managed_config"]}}), [])
            self.assertEqual(config.read_text(encoding="utf-8"), edited)

    def test_invalid_toml_takes_the_existing_activation_error_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text("not = [valid", encoding="utf-8")
            with self.assertRaisesRegex(activation.ActivationError, "invalid Codex config"):
                self._merge(config, self._release(root))
            self.assertEqual(config.read_text(encoding="utf-8"), "not = [valid")

    def test_a_release_without_the_fragment_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            empty_release = root / "old-release"
            empty_release.mkdir()
            result = self._merge(config, empty_release)
            self.assertIsNone(result["managed_config"])
            self.assertFalse(config.exists())

    def test_health_reports_missing_then_clean_and_names_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = self._release(root)
            config = root / "config.toml"
            config.write_text('model = "x"\n', encoding="utf-8")
            self.assertEqual(self._call("_codex_identity_health", config, release, "global"), (True, []))
            self._merge(config, release)
            self.assertEqual(self._call("_codex_identity_health", config, release, "global"), (False, []))
            config.write_text('[shell_environment_policy]\nexclude = ["A"]\n', encoding="utf-8")
            missing, conflicts = self._call("_codex_identity_health", config, release, "global")
            self.assertTrue(conflicts)


class OpencodeTuiSeedTest(unittest.TestCase):
    """The TUI identity entry registers through an absent-only tui.json seed."""

    def _release(self, root: Path) -> Path:
        release = root / "release"
        template_dir = release / "adapters" / "opencode" / "tui"
        template_dir.mkdir(parents=True)
        (template_dir / "tui.json").write_bytes(
            (REPO_ROOT / "adapters/opencode/tui/tui.json").read_bytes())
        return release

    def _seed(self, home, release, previous=None):
        original = activation.paths.runtime_home
        activation.paths.runtime_home = lambda runtime, scope="global": home
        try:
            return activation._seed_opencode_tui_entry(release, previous, "global")
        finally:
            activation.paths.runtime_home = original

    def _unseed(self, home, state, dry_run=False):
        original = activation.paths.runtime_home
        activation.paths.runtime_home = lambda runtime, scope="global": home
        try:
            return activation._unseed_opencode_tui_entry(state, "global", dry_run=dry_run)
        finally:
            activation.paths.runtime_home = original

    def test_absent_config_gets_the_seed_and_a_second_seed_is_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            release = self._release(root)
            first = self._seed(home, release)
            self.assertEqual(first["status"], "seeded")
            target = home / "tui.json"
            self.assertEqual(target.read_bytes(),
                             (REPO_ROOT / "adapters/opencode/tui/tui.json").read_bytes())
            second = self._seed(home, release, {"managed_config": {"opencode_tui": first["managed_config"]}})
            self.assertEqual(second["status"], "present")
            self.assertEqual(target.read_bytes(),
                             (REPO_ROOT / "adapters/opencode/tui/tui.json").read_bytes())

    def test_an_existing_user_file_stays_byte_identical_and_unowned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(parents=True)
            target = home / "tui.json"
            body = '{"plugin": ["user-entry.ts"], "theme": "user-theme"}\n'
            target.write_text(body, encoding="utf-8")
            release = self._release(root)
            result = self._seed(home, release)
            self.assertEqual(result["status"], "user-managed")
            self.assertIsNone(result["managed_config"])
            self.assertEqual(target.read_text(encoding="utf-8"), body)

    def test_same_bytes_without_a_prior_record_are_still_unowned(self):
        # F3: byte equality alone never creates ownership, so uninstall
        # must not select the file.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(parents=True)
            target = home / "tui.json"
            target.write_bytes((REPO_ROOT / "adapters/opencode/tui/tui.json").read_bytes())
            release = self._release(root)
            result = self._seed(home, release)
            self.assertEqual(result["status"], "user-managed")
            self.assertIsNone(result["managed_config"])
            self.assertEqual(self._unseed(home, {"managed_config": {}}, dry_run=True), [])
            self.assertTrue(target.is_file())

    def test_a_symlink_is_never_claimed_or_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(parents=True)
            real = root / "real-tui.json"
            real.write_text('{"plugin": []}\n', encoding="utf-8")
            (home / "tui.json").symlink_to(real)
            release = self._release(root)
            result = self._seed(home, release)
            self.assertEqual(result["status"], "user-managed")
            self.assertIsNone(result["managed_config"])
            state = {"managed_config": {"opencode_tui": {"seeded": real.read_text(encoding="utf-8")}}}
            self.assertEqual(self._unseed(home, state), [])
            self.assertTrue((home / "tui.json").is_symlink())

    def test_unseed_removes_only_the_exact_bytes_it_wrote(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            release = self._release(root)
            first = self._seed(home, release)
            state = {"managed_config": {"opencode_tui": first["managed_config"]}}
            self.assertEqual(self._unseed(home, state, dry_run=True), [str(home / "tui.json")])
            self.assertTrue((home / "tui.json").is_file())
            self.assertEqual(self._unseed(home, state), [str(home / "tui.json")])
            self.assertFalse((home / "tui.json").exists())
            again = self._seed(home, release)
            edited = (home / "tui.json").read_text(encoding="utf-8") + "\n"
            (home / "tui.json").write_text(edited, encoding="utf-8")
            kept = {"managed_config": {"opencode_tui": again["managed_config"]}}
            self.assertEqual(self._unseed(home, kept), [])
            self.assertEqual((home / "tui.json").read_text(encoding="utf-8"), edited)


class RetiredBundleCollectionTest(unittest.TestCase):
    """Superseded bundles go once nothing can use them; everything else stays, named."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        patched = fixture_env.patched_environment(
            self.root, Path(__file__).resolve().parents[2]
        )
        env = patched.__enter__()
        self.addCleanup(patched.__exit__, None, None, None)
        self.codex_home = Path(env["CODEX_HOME"])
        self.claude_home = Path(env["CLAUDE_CONFIG_DIR"])
        self.bundles = self.codex_home / ".harness" / "bundles"
        # Both runtimes are activated from somewhere outside any bundle unless
        # a case says otherwise: without a record nothing is collected at all.
        for runtime in ("codex", "claude"):
            elsewhere = self.root / "active" / runtime
            self._activate(runtime, elsewhere, elsewhere)

    def _release(self, version: str) -> Path:
        release = self.root / "releases" / version
        (release / "core").mkdir(parents=True)
        (release / "core" / "CORE.md").write_text(version + " core\n", encoding="utf-8")
        (release / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
        return release

    def _checkout(self, name: str) -> Path:
        checkout = self.root / "checkouts" / name
        (checkout / "core").mkdir(parents=True)
        (checkout / "core" / "CORE.md").write_text(name + " core\n", encoding="utf-8")
        (checkout / "utilities").mkdir()
        (checkout / "utilities" / "tool.py").write_text("print(1)\n", encoding="utf-8")
        return checkout

    def _build(self, source: Path, runtime: str = "codex") -> Path:
        """Publish a bundle exactly as activation does; return the bundle directory."""
        revision = activation.source_revision(source)
        return activation._build_bundle(runtime, source, revision, "global").parent

    def _copy(self, name: str) -> Path:
        bundle = self._build(self._checkout(name))
        self.assertFalse((bundle / "source").is_symlink())
        return bundle

    def _dangling(self, version: str = "v1.0.0", runtime: str = "codex") -> Path:
        """A linked bundle whose release has since been moved away."""
        release = self._release(version)
        bundle = self._build(release, runtime)
        release.rename(self.root / ("pruned-" + version + "-" + runtime))
        return bundle

    def _raw(self, name: str, *, valid: bool = True, **overrides) -> Path:
        """A hand-made copied bundle, for names and shapes publish never produces."""
        bundle = self.bundles / name
        (bundle / "source").mkdir(parents=True)
        (bundle / "source" / "file.txt").write_text(name + "\n", encoding="utf-8")
        if valid:
            metadata = {
                "schema": activation.SCHEMA,
                "runtime": "codex",
                "source_revision": "raw:" + name,
                "checksum": activation._tree_digest(bundle / "source"),
            }
            metadata.update(overrides)
            (bundle / "bundle.json").write_text(json.dumps(metadata), encoding="utf-8")
        return bundle

    def _activate(self, runtime: str, active_root: Path, source_root: Path, **extra) -> None:
        state = activation.paths.harness_state_dir(runtime) / "activation.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        record = {"active_root": str(active_root), "source_root": str(source_root)}
        record.update(extra)
        state.write_text(json.dumps(record), encoding="utf-8")

    def _collect(self, runtime: str = "codex", *, protected=(), in_use=None, **kwargs) -> dict:
        if in_use is None:
            def in_use(_path):
                return False, ""
        kwargs.setdefault("external", lambda: (protected, in_use))
        # The floor that keeps the newest bundles has its own cases; everywhere
        # else a bundle is judged on its merits.
        kwargs.setdefault("keep_recent", 0)
        return activation.collect_retired_bundles(runtime, **kwargs)

    # --- what goes ---------------------------------------------------------

    def test_linked_bundle_goes_with_its_release_and_the_link_is_never_followed(self):
        old_release = self._release("v1.0.0")
        new_release = self._release("v2.0.0")
        old = self._build(old_release)
        new = self._build(new_release)
        self._activate("codex", new / "source", new_release)
        pruned = self.root / "pruned-v1.0.0"
        old_release.rename(pruned)  # what release pruning leaves: a dangling link
        report = self._collect()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["removed"], [old.name])
        self.assertEqual(report["kept"], {new.name: "referenced"})
        self.assertFalse(old.exists())
        self.assertTrue((pruned / "core" / "CORE.md").is_file())
        self.assertTrue((new_release / "core" / "CORE.md").is_file())

    def test_clean_retired_copy_goes_and_disposable_markers_do_not_keep_it(self):
        retired = self._copy("retired")
        active = self._copy("active")
        self._activate("codex", active / "source", self.root / "checkouts" / "active")
        (retired / "source" / "utilities" / "__pycache__").mkdir()
        (retired / "source" / "utilities" / "__pycache__" / "tool.pyc").write_bytes(b"x")
        (retired / "source" / ".route-grounding").mkdir()
        (retired / "source" / ".route-grounding" / "marker.json").write_text("{}")
        report = self._collect()
        self.assertEqual(report["removed"], [retired.name])
        self.assertEqual(report["kept"], {active.name: "referenced"})
        self.assertFalse(retired.exists())

    def test_only_the_named_runtime_is_collected_and_a_second_pass_is_a_noop(self):
        codex_bundle = self._dangling("v1.0.0", "codex")
        claude_bundle = self._dangling("v1.0.1", "claude")
        self.assertEqual(self._collect("codex")["removed"], [codex_bundle.name])
        self.assertTrue(claude_bundle.is_dir())
        self.assertEqual(self._collect("codex")["removed"], [])
        self.assertEqual(self._collect("claude")["removed"], [claude_bundle.name])

    def test_dry_run_reports_without_removing(self):
        clean = self._copy("clean")
        edited = self._copy("edited")
        (edited / "source" / "core" / "CORE.md").write_text("hand edit\n", encoding="utf-8")
        report = self._collect(dry_run=True)
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["removed"], [clean.name])
        self.assertEqual(report["kept"], {edited.name: "checksum-mismatch"})
        self.assertTrue(clean.is_dir())

    # --- what stays: the bundle's own contents ------------------------------

    def test_linked_bundle_of_a_present_release_is_kept(self):
        old_release = self._release("v1.0.0")
        new_release = self._release("v2.0.0")
        old = self._build(old_release)
        new = self._build(new_release)
        self._activate("codex", new / "source", new_release)
        report = self._collect()
        self.assertEqual(report["removed"], [])
        self.assertEqual(
            report["kept"], {old.name: "release-present", new.name: "referenced"}
        )

    def test_copy_holding_runtime_state_is_kept_and_named(self):
        dispatch = self._copy("dispatch")
        reports = self._copy("reports")
        (dispatch / "source" / ".dispatch").mkdir()
        (dispatch / "source" / ".dispatch" / "jobs.log").write_text("row\n")
        (reports / "source" / "utilities" / ".agent_reports").mkdir()
        report = self._collect()
        self.assertEqual(report["removed"], [])
        self.assertEqual(
            report["kept"],
            {
                dispatch.name: "runtime-state:.dispatch",
                reports.name: "runtime-state:utilities/.agent_reports",
            },
        )
        self.assertTrue((dispatch / "source" / ".dispatch" / "jobs.log").is_file())

    def test_anything_beside_source_and_metadata_keeps_the_whole_bundle(self):
        # Removal takes the bundle directory, so what sits next to `source/`
        # goes with it -- and neither the residue scan nor the checksum, which
        # both read `source/` only, would ever have seen it.
        copied = self._copy("copied")
        linked = self._dangling()
        (copied / "notes.txt").write_text("the only copy\n", encoding="utf-8")
        (linked / ".agent_reports").mkdir()
        (linked / ".agent_reports" / "report.md").write_text("work\n", encoding="utf-8")
        report = self._collect()
        self.assertEqual(report["removed"], [])
        self.assertEqual(
            report["kept"],
            {
                copied.name: "unexpected-entry:notes.txt",
                linked.name: "unexpected-entry:.agent_reports",
            },
        )
        self.assertTrue((copied / "notes.txt").is_file())
        self.assertTrue((linked / ".agent_reports" / "report.md").is_file())

    def test_copy_whose_published_files_changed_is_kept(self):
        edited = self._copy("edited")
        (edited / "source" / "core" / "CORE.md").write_text("hand edit\n", encoding="utf-8")
        report = self._collect()
        self.assertEqual(report["kept"], {edited.name: "checksum-mismatch"})
        self.assertEqual(
            (edited / "source" / "core" / "CORE.md").read_text(encoding="utf-8"),
            "hand edit\n",
        )

    def test_entries_publish_did_not_write_are_kept(self):
        self.bundles.mkdir(parents=True)
        self._raw("no-metadata", valid=False)
        corrupt = self._raw("corrupt", valid=False)
        (corrupt / "bundle.json").write_text("{not json", encoding="utf-8")
        binary = self._raw("binary", valid=False)
        (binary / "bundle.json").write_bytes(b"\xff\xfe")
        self._raw("foreign-runtime", runtime="claude")
        self._raw("old-schema", schema=1)
        self._raw("mislabeled", source_link=True)
        (self.bundles / ".staging-deadbeef").mkdir()
        (self.bundles / "stray-file").write_text("x")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.bundles / "linked-entry").symlink_to(elsewhere, target_is_directory=True)
        before = sorted(item.name for item in self.bundles.iterdir())
        report = self._collect()
        self.assertEqual(report["removed"], [])
        self.assertEqual(
            report["kept"],
            {
                ".staging-deadbeef": "staging",
                "binary": "metadata-unreadable",
                "corrupt": "metadata-unreadable",
                "foreign-runtime": "metadata-foreign",
                "linked-entry": "not-a-bundle-directory",
                "mislabeled": "metadata-mismatch",
                "no-metadata": "metadata-missing",
                "old-schema": "metadata-foreign",
                "stray-file": "not-a-bundle-directory",
            },
        )
        self.assertEqual(sorted(item.name for item in self.bundles.iterdir()), before)
        self.assertTrue(elsewhere.is_dir())

    # --- what stays: references --------------------------------------------

    def test_activation_and_launcher_references_keep_a_bundle(self):
        by_other_runtime = self._raw("abc")
        by_launcher = self._raw("abc-2")
        unreferenced = self._raw("abc-3")
        # Another runtime was activated from inside this runtime's bundle.
        self._activate("claude", self.root / "claude-active", by_other_runtime / "source")
        report = self._collect(protected=[by_launcher / "source" / "tools" / "fleet.sh"])
        # `abc` must not be protected by a path under `abc-2` or `abc-3`, nor the reverse.
        self.assertEqual(report["removed"], [unreferenced.name])
        self.assertEqual(report["kept"], {"abc": "referenced", "abc-2": "referenced"})

    def test_a_projection_into_a_bundle_keeps_it_whatever_the_record_names(self):
        # The record's roots point elsewhere, but the home is still projected
        # into this bundle: the links are what the runtime actually runs through.
        projected = self._raw("projected")
        by_discovery = self._raw("by-discovery")
        pointer = self.codex_home / "hearting"
        pointer.symlink_to(projected / "source", target_is_directory=True)
        skill = self.codex_home / "skills" / "one"
        skill.parent.mkdir(parents=True)
        skill.symlink_to(by_discovery / "source" / "file.txt")
        elsewhere = self.root / "active" / "codex"
        self._activate(
            "codex", elsewhere, elsewhere,
            owned_paths=[{"dest": str(pointer), "kind": "symlink", "source": "ignored"}],
            discovery_paths=[str(skill)],
        )
        report = self._collect()
        self.assertEqual(report["removed"], [])
        self.assertEqual(
            report["kept"], {"by-discovery": "referenced", "projected": "referenced"}
        )

    def test_another_runtimes_projection_is_guarded_without_its_record(self):
        # Claude's home still runs through an old Codex bundle, but Claude's
        # record is gone -- or was written with roots only and lists no paths.
        old = self._raw("old")
        pointer = self.claude_home / "hearting"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.symlink_to(old / "source", target_is_directory=True)
        record = activation.paths.harness_state_dir("claude") / "activation.json"
        record.rename(record.with_suffix(".away"))
        report = self._collect()
        self.assertEqual((report["removed"], report["kept"]), ([], {"old": "referenced"}))
        elsewhere = self.root / "active" / "claude"
        self._activate("claude", elsewhere, elsewhere)
        report = self._collect()
        self.assertEqual((report["removed"], report["kept"]), ([], {"old": "referenced"}))
        self.assertTrue(old.is_dir())

    def test_a_retired_link_left_in_a_discovery_directory_guards_its_bundle(self):
        # A skill a later version no longer ships: no record lists it, the
        # current layout does not define it, and Claude's own pointer already
        # names the new root. The link is still there and still resolves into
        # an old Codex bundle.
        old = self._raw("old")
        record = activation.paths.harness_state_dir("claude") / "activation.json"
        record.rename(record.with_suffix(".away"))
        layout = {
            Path(item["dest"]) for item in
            activation._linked_entries("claude", activation.paths.agent_home(), "global")
        }
        discovery = next(path.parent for path in layout if path.parent != self.claude_home)
        leftover = discovery / "retired-only-capability"
        self.assertNotIn(leftover, layout)
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.symlink_to(old / "source" / "file.txt")
        (self.claude_home / "hearting").symlink_to(
            self.root / "active" / "claude", target_is_directory=True
        )
        report = self._collect()
        self.assertEqual((report["removed"], report["kept"]), ([], {"old": "referenced"}))
        self.assertTrue(old.is_dir())

    def test_a_retired_link_guards_its_bundle_where_the_current_source_projects_nothing(self):
        # A whole surface the current source has stopped filling: no layout
        # destination lives in the directory, so nothing but the directory's
        # own name says to look there. One level down counts as well -- a mode
        # file sits under its group.
        flat = self._raw("flat")
        nested = self._raw("nested")
        unreferenced = self._raw("unreferenced")
        parents = {
            Path(item["dest"]).parent for item in
            activation._linked_entries("claude", activation.paths.agent_home(), "global")
        }
        unused = [
            self.claude_home / name for name in activation._DISCOVERY_DIRECTORIES
            if not any(
                parent == self.claude_home / name or self.claude_home / name in parent.parents
                for parent in parents
            )
        ]
        self.assertGreaterEqual(len(unused), 2, "Claude fills every discovery directory")
        leftover = unused[0] / "retired-only-command.md"
        leftover.parent.mkdir(parents=True)
        leftover.symlink_to(flat / "source" / "file.txt")
        grouped = unused[-1] / "retired-group" / "retired-only-mode.md"
        grouped.parent.mkdir(parents=True)
        grouped.symlink_to(nested / "source" / "file.txt")
        report = self._collect()
        self.assertEqual(report["removed"], [unreferenced.name])
        self.assertEqual(report["kept"], {"flat": "referenced", "nested": "referenced"})

    def test_every_projected_destination_lies_where_the_guard_looks(self):
        # The guard finds a retired link by the name of its directory. A new
        # surface in the layout has to be named there too, or its leftovers
        # would be found only for as long as the source still ships them.
        for runtime in activation.RUNTIMES:
            home = activation.paths.runtime_home(runtime, "global")
            entries = activation._linked_entries(runtime, activation.paths.agent_home(), "global")
            self.assertTrue(entries)
            for item in entries:
                parts = Path(item["dest"]).relative_to(home).parts
                if len(parts) == 1:
                    continue
                self.assertIn(parts[0], activation._DISCOVERY_DIRECTORIES, item["dest"])
                self.assertLessEqual(len(parts), 3, item["dest"])

    @unittest.skipUnless(Path("/proc/self/cwd").exists(), "needs /proc")
    def test_a_process_started_through_a_projection_before_it_moved_keeps_copies(self):
        # `python3 ~/.codex/hearting/tool.py` shows the pointer in its argv and
        # nothing else. After an activation moves the pointer, no path string
        # says which bundle that process is running from.
        import datetime as dt

        copied = self._copy("copied")
        linked = self._dangling()
        target = self.root / "active" / "codex"
        target.mkdir(parents=True)
        (target / "tool.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        pointer = self.codex_home / "hearting"
        pointer.symlink_to(target, target_is_directory=True)
        process = subprocess.Popen([sys.executable, str(pointer / "tool.py")])
        try:
            later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
            self._activate("codex", target, target, activated_at=later.isoformat())
            report = self._collect()
            # The activation happened after the process started: keep copies.
            self.assertEqual(report["removed"], [linked.name])
            self.assertEqual(
                report["kept"], {copied.name: f"live-process-via-projection:{process.pid}"}
            )
            earlier = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
            self._activate("codex", target, target, activated_at=earlier.isoformat())
            # Started after the last activation: it runs from where the pointer is now.
            self.assertEqual(self._collect()["removed"], [copied.name])
        finally:
            process.kill()
            process.wait()

    def _moved_pointer(self) -> Path:
        """Point Codex at a root with a sleeping tool, activated five minutes from now."""
        import datetime as dt

        target = self.root / "active" / "codex"
        target.mkdir(parents=True)
        (target / "tool.py").write_text(
            "import os, sys, time\n"
            "if len(sys.argv) > 1:\n"
            "    os.chdir('/')\n"
            "    open(sys.argv[1], 'w').close()\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        (self.codex_home / "hearting").symlink_to(target, target_is_directory=True)
        (self.codex_home / "skills").mkdir()
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
        self._activate("codex", target, target, activated_at=later.isoformat())
        return target

    @unittest.skipUnless(Path("/proc/self/cwd").exists(), "needs /proc")
    def test_a_relative_argv_through_a_projection_keeps_copies(self):
        # `cd ~/.codex; python3 hearting/tool.py`: the argv names the pointer
        # only from where the process stands. The last form leaves the pointer
        # again with `..`, so its normalised path never mentions it.
        copied = self._copy("copied")
        self._moved_pointer()
        for script in (
            "hearting/tool.py",
            "./hearting/tool.py",
            "skills/../hearting/tool.py",
            "hearting/../codex/tool.py",
        ):
            process = subprocess.Popen([sys.executable, script], cwd=self.codex_home)
            try:
                self.assertIsNone(process.poll(), script)
                report = self._collect()
                self.assertEqual(
                    (report["removed"], report["kept"]),
                    ([], {copied.name: f"live-process-via-projection:{process.pid}"}),
                    script,
                )
            finally:
                process.kill()
                process.wait()
        self.assertEqual(self._collect()["removed"], [copied.name])

    @unittest.skipUnless(Path("/proc/self/cwd").exists(), "needs /proc")
    def test_a_relative_argv_is_still_read_after_the_process_changed_directory(self):
        # The directory the argv was relative to is gone from `cwd` once the
        # process moves; the `PWD` it was started with still names it.
        copied = self._copy("copied")
        self._moved_pointer()
        moved = self.root / "moved"
        process = subprocess.Popen(
            [sys.executable, "hearting/tool.py", str(moved)],
            cwd=self.codex_home,
            env={**os.environ, "PWD": str(self.codex_home)},
        )
        try:
            deadline = time.time() + 20
            while not moved.exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(moved.exists())
            self.assertEqual(os.readlink(f"/proc/{process.pid}/cwd"), "/")
            report = self._collect()
            self.assertEqual(
                (report["removed"], report["kept"]),
                ([], {copied.name: f"live-process-via-projection:{process.pid}"}),
            )
        finally:
            process.kill()
            process.wait()

    @unittest.skipUnless(Path("/proc/self/cwd").exists(), "needs /proc")
    def test_a_projection_is_matched_under_the_resolved_path_of_its_home(self):
        # The home is configured through a link, as with a relocated `$HOME`.
        # A process `cwd` is the resolved directory, never the configured one.
        copied = self._copy("copied")
        self._moved_pointer()
        alias = self.root / "alias-home"
        alias.symlink_to(self.codex_home, target_is_directory=True)
        configured = activation.paths.runtime_home

        def runtime_home(runtime, scope="global"):
            return alias if runtime == "codex" else configured(runtime, scope)

        process = subprocess.Popen([sys.executable, "hearting/tool.py"], cwd=self.codex_home)
        try:
            with mock.patch.object(activation.paths, "runtime_home", runtime_home):
                _guarded, projections = activation._activation_guard("global")
                holders = activation._bundle_process_holders(self.bundles, projections)
            self.assertIn(str(alias / "hearting"), projections["codex"][0])
            self.assertIn(str(self.codex_home / "hearting"), projections["codex"][0])
            self.assertEqual(holders.get(activation._VIA_PROJECTION), str(process.pid))
        finally:
            process.kill()
            process.wait()
        self.assertTrue(copied.is_dir())

    def test_the_newest_bundles_are_never_collected(self):
        # The floor release pruning keeps, for the same reason: what an
        # activation just retired may still be running something that started
        # a moment earlier through a pointer.
        now = time.time()
        for age, name in enumerate(("newest", "previous", "older", "oldest")):
            bundle = self._raw(name)
            os.utime(bundle, (now - age * 3600, now - age * 3600))
        report = activation.collect_retired_bundles(
            "codex", external=lambda: ((), lambda _path: (False, ""))
        )
        self.assertEqual(report["removed"], ["older", "oldest"])
        self.assertEqual(report["kept"], {"newest": "recent", "previous": "recent"})
        report = self._collect(keep_recent=1)
        self.assertEqual((report["removed"], report["kept"]), (["previous"], {"newest": "recent"}))

    def test_a_home_without_a_usable_activation_record_is_left_alone(self):
        copied = self._copy("copied")
        state = activation.paths.harness_state_dir("codex") / "activation.json"
        for content, detail in (
            (None, "no activation record"),
            ("{}", "no activation record"),
            (json.dumps({"active_root": "/somewhere"}), "activation record has no source_root"),
        ):
            if content is None:
                state.rename(state.with_suffix(".away"))
            else:
                state.write_text(content, encoding="utf-8")
            report = self._collect()
            self.assertEqual((report["status"], report["detail"]), ("skipped", detail))
            self.assertEqual(report["removed"], [])
            self.assertTrue(copied.is_dir())

    @unittest.skipUnless(Path("/proc/self/cwd").exists(), "needs /proc")
    def test_a_process_holding_a_bundle_by_cwd_env_or_argv_keeps_it(self):
        by_cwd = self._copy("by-cwd")
        by_env = self._copy("by-env")
        by_argv = self._copy("by-argv")
        by_relative_argv = self._copy("by-relative-argv")
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        processes = [
            subprocess.Popen(sleeper, cwd=by_cwd / "source"),
            subprocess.Popen(
                sleeper, env={**os.environ, "HOLDER_HOME": str(by_env / "source")}
            ),
            subprocess.Popen([*sleeper, str(by_argv / "source" / "utilities" / "tool.py")]),
            subprocess.Popen(
                [*sleeper, f"bundles/{by_relative_argv.name}/source/utilities/tool.py"],
                cwd=self.bundles.parent,
            ),
        ]
        try:
            report = self._collect()
            self.assertEqual(report["removed"], [])
            self.assertEqual(
                report["kept"],
                {
                    by_cwd.name: f"live-process:{processes[0].pid}",
                    by_env.name: f"live-process:{processes[1].pid}",
                    by_argv.name: f"live-process:{processes[2].pid}",
                    by_relative_argv.name: f"live-process:{processes[3].pid}",
                },
            )
        finally:
            for process in processes:
                process.kill()
                process.wait()
        report = self._collect()
        self.assertEqual(
            sorted(report["removed"]),
            sorted([by_cwd.name, by_env.name, by_argv.name, by_relative_argv.name]),
        )

    def test_an_unreadable_process_table_keeps_every_candidate(self):
        linked = self._dangling()
        copied = self._copy("copied")
        with mock.patch.object(activation, "_bundle_process_holders", return_value=None):
            report = self._collect()
        self.assertEqual(report["removed"], [])
        self.assertEqual(
            report["kept"], {linked.name: "proc-unreadable", copied.name: "proc-unreadable"}
        )

    def test_a_dispatch_reference_keeps_a_bundle_and_an_undecidable_one_is_in_use(self):
        copied = self._copy("copied")
        linked = self._dangling("v1.0.0")
        asked = []

        def in_use(path):
            asked.append(Path(path))
            return True, "open-route:rt-1"

        report = self._collect(in_use=in_use)
        self.assertEqual(
            report["kept"], {copied.name: "open-route:rt-1", linked.name: "open-route:rt-1"}
        )
        # A copy answers for itself; a link answers for the release it names,
        # which may have vanished without pruning ever proving it unused.
        self.assertEqual(
            sorted(asked), sorted([copied / "source", self.root / "releases" / "v1.0.0"])
        )

        def broken(_path):
            raise RuntimeError("registry vanished")

        report = self._collect(in_use=broken)
        self.assertEqual(
            set(report["kept"].values()), {"reference-check-failed:RuntimeError"}
        )
        self.assertTrue(copied.is_dir() and linked.is_dir())

    def test_a_reference_that_appears_before_the_delete_is_seen(self):
        # The first verdict found the bundle free; by the time it is sealed a
        # launch has registered against it, or a process has started from it.
        by_registry = self._raw("by-registry")
        by_process = self._raw("by-process")
        answers = {str(by_registry / "source"): [(False, ""), (True, "open-attempt:att-1")]}

        def in_use(path):
            queue = answers.get(str(path))
            return queue.pop(0) if queue else (False, "")

        # One scan to choose candidates, then one before each removal attempt;
        # `by-process` sorts first, so its re-read is the second scan.
        scans = iter([{}, {"by-process": "4242"}, {}])
        with mock.patch.object(
            activation, "_bundle_process_holders", side_effect=lambda *_args: next(scans)
        ):
            report = self._collect(in_use=in_use)
        self.assertEqual(report["removed"], [])
        self.assertEqual(
            report["kept"],
            {"by-process": "live-process:4242", "by-registry": "open-attempt:att-1"},
        )
        self.assertTrue(by_registry.is_dir() and by_process.is_dir())

    # --- the seal ------------------------------------------------------------

    def _disturb_once(self, hook_name: str, before: bool, disturb):
        """Run `disturb(path)` around the first `safe_fs.<hook_name>` call per bundle."""
        real = getattr(activation.safe_fs, hook_name)
        seen = set()

        def wrapper(target, *args, **kwargs):
            path = Path(target)
            first = path.parent == self.bundles and path not in seen
            seen.add(path)
            if first and before:
                disturb(path)
            result = real(target, *args, **kwargs)
            if first and not before:
                disturb(path)
            return result

        return mock.patch.object(activation.safe_fs, hook_name, side_effect=wrapper)

    def test_state_that_arrives_between_the_verdict_and_the_seal_keeps_the_bundle(self):
        # The window the first verdict leaves open: it passed, then a writer
        # dropped a registry the checksum is blind to. The seal would accept
        # it, so the verdict is taken again on what was sealed.
        copied = self._copy("copied")

        def late_registry(bundle):
            (bundle / "source" / ".dispatch").mkdir()
            (bundle / "source" / ".dispatch" / "jobs.log").write_text("row\n")

        with self._disturb_once("capture_state", True, late_registry):
            report = self._collect()
        self.assertEqual(report["kept"], {copied.name: "runtime-state:.dispatch"})
        self.assertTrue((copied / "source" / ".dispatch" / "jobs.log").is_file())

    def test_a_directory_swapped_in_under_the_name_is_judged_on_its_own(self):
        # An eligible linked bundle is replaced by a real directory holding
        # data. The old metadata said "link, nothing to hash"; the new
        # directory must not be removed on that say-so.
        linked = self._dangling()
        aside = self.root / "moved-aside"

        def swap(bundle):
            bundle.rename(aside)
            (bundle / "source").mkdir(parents=True)
            (bundle / "source" / "work.txt").write_text("unrelated work\n", encoding="utf-8")
            (bundle / "bundle.json").write_text(
                json.dumps({
                    "schema": activation.SCHEMA, "runtime": "codex",
                    "source_revision": "raw:swapped", "checksum": "0" * 64,
                }),
                encoding="utf-8",
            )

        with self._disturb_once("capture_state", True, swap):
            report = self._collect()
        self.assertEqual(report["kept"], {linked.name: "checksum-mismatch"})
        self.assertEqual(
            (linked / "source" / "work.txt").read_text(encoding="utf-8"), "unrelated work\n"
        )

    def test_a_bundle_that_changes_after_the_seal_is_not_removed(self):
        copied = self._copy("copied")
        linked = self._dangling()

        def late_arrival(bundle):
            # Past every check, immediately before the delete.
            (bundle / "bundle.json").write_text(
                (bundle / "bundle.json").read_text(encoding="utf-8") + "\n", encoding="utf-8"
            )

        with self._disturb_once("authority", False, late_arrival):
            report = self._collect()
        self.assertEqual(report["removed"], [])
        self.assertEqual(
            report["kept"],
            {
                copied.name: "remove-refused:expected-state-mismatch",
                linked.name: "remove-refused:expected-state-mismatch",
            },
        )
        self.assertTrue((copied / "source" / "core" / "CORE.md").is_file())
        self.assertTrue(linked.is_dir())

    # --- the lock ------------------------------------------------------------

    def test_references_are_read_under_every_runtimes_activation_lock(self):
        self._copy("copied")
        order = []
        real_locks = activation.safe_fs.TargetLocks

        def recording(targets, **kwargs):
            order.append(("lock", sorted(Path(item) for item in targets), kwargs))
            return real_locks(targets, **kwargs)

        def external():
            order.append(("external", None, None))
            return (), lambda _path: (False, "")

        with mock.patch.object(activation.safe_fs, "TargetLocks", side_effect=recording):
            self._collect(external=external)
        # Once to choose candidates, once more before the one removal.
        self.assertEqual([name for name, *_ in order], ["lock", "external", "external"])
        records = [activation._state_path(runtime) for runtime in activation.RUNTIMES]
        self.assertEqual(order[0][1], sorted([self.bundles, *records]))
        self.assertEqual(order[0][2], {"blocking": False})
        # Each record is in the set the matching runtime's own activation locks.
        for runtime, record in zip(activation.RUNTIMES, records):
            self.assertIn(record, activation._activation_owned_paths(runtime))
        self.assertIn(self.bundles, activation._activation_owned_paths("codex"))

    def test_collection_steps_aside_while_any_runtime_is_activating(self):
        copied = self._copy("copied")
        for busy in (
            self.bundles,
            activation._state_path("claude"),
            activation._state_path("opencode"),
        ):
            with activation.safe_fs.TargetLocks([busy]):
                report = self._collect()
            self.assertEqual(
                (report["status"], report["detail"]),
                ("skipped", "an activation is in progress"),
            )
            self.assertTrue(copied.is_dir())
        self.assertEqual(self._collect()["removed"], [copied.name])

    def test_no_runtime_can_start_activating_while_collection_runs(self):
        # The window the single container lock left open: another runtime being
        # activated from one of this runtime's retired bundles after the last
        # reference read. Its activation lock is held for the whole pass.
        self._copy("copied")
        seen = {}

        def external():
            for runtime in ("claude", "opencode"):
                try:
                    with activation.safe_fs.TargetLocks(
                        [activation._state_path(runtime)], blocking=False
                    ):
                        seen[runtime] = "free"
                except activation.safe_fs.SafetyError as exc:
                    seen[runtime] = exc.code
            return (), lambda _path: (False, "")

        report = self._collect(external=external)
        self.assertEqual(seen, {"claude": "target-busy", "opencode": "target-busy"})
        self.assertEqual(len(report["removed"]), 1)
        # ... and they are released afterwards.
        with activation.safe_fs.TargetLocks(
            [activation._state_path("claude")], blocking=False
        ):
            pass

    def test_an_unrecovered_activation_transaction_holds_collection(self):
        copied = self._copy("copied")
        pending = activation.paths.harness_state_dir("claude") / "transactions" / "tx-1"
        pending.mkdir(parents=True)
        (pending / "journal.json").write_text("{}", encoding="utf-8")
        report = self._collect()
        self.assertEqual(
            (report["status"], report["detail"]),
            ("skipped", "claude has an unrecovered activation transaction"),
        )
        self.assertTrue(copied.is_dir())

    # --- the budget ------------------------------------------------------------

    def test_copy_budget_defers_the_rest_and_a_later_call_finishes(self):
        names = sorted(self._copy(name).name for name in ("one", "two", "three"))
        first = self._collect(copy_budget=1)
        self.assertEqual(first["removed"], names[:1])
        self.assertEqual(first["deferred"], 2)
        self.assertEqual(first["kept"], {})
        second = self._collect(copy_budget=1)
        self.assertEqual(second["removed"], names[1:2])
        self.assertEqual(second["deferred"], 1)
        third = self._collect()
        self.assertEqual(third["removed"], names[2:])
        self.assertEqual(self._collect()["removed"], [])

    def test_kept_copies_cannot_starve_the_ones_behind_them(self):
        # Two copies that will be kept sort ahead of a clean one. Each call
        # examines one and remembers where it stopped, so the clean copy is
        # reached instead of the same two being examined forever.
        for name in ("a-edited", "b-edited"):
            bundle = self._raw(name)
            (bundle / "source" / "file.txt").write_text("hand edit\n", encoding="utf-8")
        clean = self._raw("c-clean")
        outcomes = [self._collect(copy_budget=1) for _ in range(3)]
        self.assertEqual([item["removed"] for item in outcomes], [[], [], ["c-clean"]])
        self.assertEqual(outcomes[0]["kept"], {"a-edited": "checksum-mismatch"})
        self.assertEqual(outcomes[1]["kept"], {"b-edited": "checksum-mismatch"})
        self.assertEqual([item["deferred"] for item in outcomes], [2, 2, 2])
        self.assertFalse(clean.exists())

    def test_the_budget_bounds_the_work_of_one_call(self):
        for index in range(6):
            bundle = self._raw(f"edited-{index}")
            (bundle / "source" / "file.txt").write_text("hand edit\n", encoding="utf-8")
        seals, reads = [], []
        real_capture = activation.safe_fs.capture_state

        def counting(path, *args, **kwargs):
            # Bundle directories only: writing the resume cursor captures too.
            if Path(path).parent == self.bundles and Path(path).is_dir():
                seals.append(Path(path).name)
            return real_capture(path, *args, **kwargs)

        def external():
            reads.append(1)
            return (), lambda _path: (False, "")

        with mock.patch.object(activation.safe_fs, "capture_state", side_effect=counting):
            report = self._collect(external=external, copy_budget=2)
        # Kept or removed, an examined copy is paid for; the rest wait.
        self.assertEqual(seals, ["edited-0", "edited-1"])
        self.assertEqual(len(reads), 3)  # one to choose candidates, one per examined copy
        self.assertEqual(report["deferred"], 4)
        self.assertEqual(report["removed"], [])

    def test_the_budget_never_delays_a_linked_bundle(self):
        linked = self._dangling()
        copied = self._copy("copied")
        report = self._collect(copy_budget=0)
        self.assertEqual(report["removed"], [linked.name])
        self.assertEqual(report["deferred"], 1)
        self.assertTrue(copied.is_dir())

    # --- never raises ----------------------------------------------------------

    def test_it_never_raises(self):
        self.assertEqual(self._collect(), {
            "runtime": "codex", "status": "ok", "removed": [], "kept": {}, "deferred": 0,
        })  # no container yet
        self.assertEqual(self._collect("nonesuch")["status"], "skipped")
        self.assertEqual(
            activation.collect_retired_bundles("codex", "project")["status"], "skipped"
        )
        copied = self._copy("copied")

        def broken_external():
            raise RuntimeError("distribution layer unavailable")

        report = self._collect(external=broken_external)
        self.assertEqual(report["status"], "skipped")
        self.assertIn("RuntimeError", report["detail"])
        self.assertTrue(copied.is_dir())

    def test_an_unreadable_reference_source_keeps_everything(self):
        copied = self._copy("copied")
        other = activation.paths.harness_state_dir("claude") / "activation.json"
        for payload in (b"{not json", b"\xff"):
            other.write_bytes(payload)  # another runtime's record: a reference source
            report = self._collect()
            self.assertEqual(report["status"], "skipped")
            self.assertEqual(report["removed"], [])
            self.assertTrue(copied.is_dir())
        elsewhere = self.root / "active" / "claude"
        self._activate("claude", elsewhere, elsewhere)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "relative/claude"}):
            report = self._collect()
        self.assertEqual(report["status"], "skipped")
        self.assertTrue(copied.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
