#!/usr/bin/env python3
import os
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
