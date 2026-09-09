#!/usr/bin/env python3
"""Release-evidence retention tests (core/OPERATIONS.md "Release pruning is
evidence-bound"): semantic-equality carry-forward proof, single-open-strategy
race/kind safety, and the lossless archive + sealed CAS receipt path.

Every fixture runs under `fixture_env.patched_environment`, a private,
isolated HOME/XDG/HARNESS_STATE_ROOT tree; nothing here touches a live
release, a live process, or v2.116.1/PID 3150216 (out of scope).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import distribution  # noqa: E402
import fixture_env  # noqa: E402


def _dump(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _sidecar(root: Path, *, route_id="rt-test", node_id="execute",
            attempt_id="att-test-1", seq="3", marker_root=None) -> dict:
    marker_root = marker_root if marker_root is not None else root
    return {
        "schema_version": 2, "route_id": route_id, "node_id": node_id,
        "attempt_id": attempt_id, "dispatch_depth": 2, "transport": "headless",
        "execution_surface": "registered-headless", "registered_worker": True,
        "fallback_hop": "none", "evidence_sha256": "a" * 64,
        "completion_marker": str(marker_root / "completion" / route_id / f"{node_id}.json"),
        "completion_marker_history": str(
            marker_root / "completion" / route_id / f"{node_id}.{seq}.json"
        ),
    }


class HermeticCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._ctx = fixture_env.patched_environment(self.root, self.root)
        self._ctx.__enter__()
        self.stable_root = distribution.stable_state_root(os.environ)

    def tearDown(self):
        self._ctx.__exit__(None, None, None)
        self._tmp.cleanup()

    def promote(self):
        distribution._append_migration_journal(self.stable_root, {
            "record_version": distribution.MIGRATION_ALIAS_RECORD_VERSION,
            "status": "completed",
            "legacy_jobs_identity": {"path": "/nonexistent/jobs.log"},
        })

    def candidate(self, name="v-old") -> Path:
        path = distribution.data_root() / "releases" / name
        (path / ".dispatch").mkdir(parents=True, exist_ok=True)
        return path


class SafeRelativeComponentsTest(HermeticCase):
    def test_rejects_unsafe_shapes(self):
        for bad in ("", "/abs", "a/../b", "./a", "a/./b", "a\x00b", "a\\b", "a//b"):
            with self.subTest(bad=bad):
                with self.assertRaises(distribution._DeltaVerdictError) as ctx:
                    distribution._safe_relative_components(bad)
                self.assertEqual(ctx.exception.code, "delta-unsafe-relative")

    def test_accepts_ordinary_relative(self):
        self.assertEqual(distribution._safe_relative_components("a/b/c"), ["a", "b", "c"])


class OpenContainedRegularTest(HermeticCase):
    def test_21_absent_is_typed_absent(self):
        root = self.root / "root"
        root.mkdir()
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._open_contained_regular(root, "missing.txt")
        self.assertEqual(ctx.exception.code, "delta-absent")

    def test_21_parent_symlink_is_unsafe_path_not_archive_candidate(self):
        root = self.root / "root"
        real_parent = self.root / "real-parent"
        real_parent.mkdir(parents=True)
        (real_parent / "f.txt").write_bytes(b"x")
        root.mkdir()
        (root / "sub").symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._open_contained_regular(root, "sub/f.txt")
        self.assertEqual(ctx.exception.code, "delta-unsafe-path")

    def test_21_leaf_symlink_is_unsafe_path(self):
        root = self.root / "root"
        root.mkdir()
        (root / "real.txt").write_bytes(b"x")
        (root / "link.txt").symlink_to(root / "real.txt")
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._open_contained_regular(root, "link.txt")
        self.assertEqual(ctx.exception.code, "delta-unsafe-path")

    def test_21_broken_symlink_is_unsafe_path_not_missing(self):
        root = self.root / "root"
        root.mkdir()
        (root / "broken.txt").symlink_to(root / "does-not-exist")
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._open_contained_regular(root, "broken.txt")
        self.assertEqual(ctx.exception.code, "delta-unsafe-path")

    def test_21_non_regular_target_is_kind_mismatch_never_missing_or_archivable(self):
        root = self.root / "root"
        root.mkdir()
        fifo_path = root / "pipe"
        os.mkfifo(fifo_path)
        try:
            # D5: a writer-less FIFO must never hang -- the leaf is opened
            # O_NONBLOCK, so ENXIO (no writer) surfaces immediately as a typed
            # `delta-unreadable`/`delta-kind-mismatch`, never a block.
            try:
                fd, st = distribution._open_contained_regular(root, "pipe")
            except distribution._DeltaVerdictError as exc:
                self.assertIn(exc.code, ("delta-unreadable", "delta-kind-mismatch"))
            else:
                try:
                    self.assertFalse(stat.S_ISREG(st.st_mode))
                finally:
                    os.close(fd)
        finally:
            # destructive-ok: reason=remove one disposable FIFO created by this test inside a temp fixture root; boundary=one exact path under self.root
            fifo_path.unlink()

    def test_21_directory_target_is_kind_mismatch(self):
        root = self.root / "root"
        (root / "sub").mkdir(parents=True)
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._open_contained_regular(root, "sub")
        self.assertEqual(ctx.exception.code, "delta-kind-mismatch")

    def test_22_in_place_modification_race_is_delta_race(self):
        root = self.root / "root"
        root.mkdir()
        target = root / "f.txt"
        target.write_bytes(b"content")
        real_fstat = os.fstat
        state = {"n": 0}

        def patched_fstat(fd):
            state["n"] += 1
            actual = real_fstat(fd)
            if state["n"] == 2:
                # The post-read re-stat (call #2) observes a size change --
                # an in-place modification during the read.
                return os.stat_result((
                    actual.st_mode, actual.st_ino, actual.st_dev, actual.st_nlink,
                    actual.st_uid, actual.st_gid, actual.st_size + 1,
                    actual.st_atime, actual.st_mtime, actual.st_ctime,
                ))
            return actual

        os.fstat = patched_fstat
        try:
            with self.assertRaises(distribution._DeltaVerdictError) as ctx:
                distribution._read_contained_bytes(root, "f.txt")
            self.assertEqual(ctx.exception.code, "delta-race")
        finally:
            os.fstat = real_fstat

    def test_22_rename_replace_race_is_delta_race(self):
        root = self.root / "root"
        root.mkdir()
        target = root / "f.txt"
        target.write_bytes(b"original")
        real_fstat = os.fstat
        state = {"n": 0}

        def patched_fstat(fd):
            state["n"] += 1
            actual = real_fstat(fd)
            if state["n"] == 3:
                # The reopen-identity-bind fstat (call #3, inside the second
                # `_open_contained_regular`) observes a different inode --
                # a rename/replace between the first close and the reopen.
                return os.stat_result((
                    actual.st_mode, actual.st_ino + 1, actual.st_dev, actual.st_nlink,
                    actual.st_uid, actual.st_gid, actual.st_size,
                    actual.st_atime, actual.st_mtime, actual.st_ctime,
                ))
            return actual

        os.fstat = patched_fstat
        try:
            with self.assertRaises(distribution._DeltaVerdictError) as ctx:
                distribution._read_contained_bytes(root, "f.txt")
            self.assertEqual(ctx.exception.code, "delta-race")
        finally:
            os.fstat = real_fstat

    def test_leaf_replaced_by_fifo_during_open_is_typed_not_a_hang(self):
        root = self.root / "root"
        root.mkdir()
        target = root / "f.txt"
        target.write_bytes(b"original")
        real_open = os.open
        state = {"count": 0}

        def patched_open(path, flags, *args, **kwargs):
            if path == "f.txt" and kwargs.get("dir_fd") is not None and not (
                flags & getattr(os, "O_DIRECTORY", 0)
            ):
                state["count"] += 1
                if state["count"] == 1:
                    # destructive-ok: reason=swap a disposable fixture leaf for a FIFO to prove the open path never hangs; boundary=one exact path under self.root
                    target.unlink()
                    os.mkfifo(target)
            return real_open(path, flags, *args, **kwargs)

        os.open = patched_open
        try:
            try:
                distribution._read_contained_bytes(root, "f.txt")
            except distribution._DeltaVerdictError as exc:
                self.assertIn(exc.code, ("delta-kind-mismatch", "delta-race", "delta-unreadable"))
            else:
                self.fail("expected a typed refusal, not silent success")
        finally:
            os.open = real_open
            if target.exists() and stat.S_ISFIFO(os.lstat(target).st_mode):
                # destructive-ok: reason=clean up the disposable fixture FIFO; boundary=one exact path under self.root
                target.unlink()


class SidecarStrictParseTest(unittest.TestCase):
    def _valid(self) -> dict:
        return _sidecar(Path("/tmp/root"))

    def test_duplicate_key_is_rejected_at_parse(self):
        raw = (b'{"schema_version":2,"schema_version":2,"route_id":"r","node_id":"n",'
              b'"attempt_id":"a","dispatch_depth":2,"transport":"headless",'
              b'"execution_surface":"registered-headless","registered_worker":true,'
              b'"fallback_hop":"none","evidence_sha256":"' + b"a" * 64 + b'",'
              b'"completion_marker":"/x","completion_marker_history":"/y"}')
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._parse_sidecar_strict(raw)
        self.assertEqual(ctx.exception.code, "delta-duplicate-key")

    def test_unknown_field_is_rejected(self):
        data = {**self._valid(), "extra": "field"}
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._parse_sidecar_strict(_dump(data))
        self.assertEqual(ctx.exception.code, "delta-schema-unknown-field")

    def test_missing_field_is_rejected(self):
        data = self._valid()
        del data["fallback_hop"]
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._parse_sidecar_strict(_dump(data))
        self.assertEqual(ctx.exception.code, "delta-schema-unknown-field")

    def test_wrong_type_marker_is_rejected(self):
        data = {**self._valid(), "completion_marker": 12345}
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._parse_sidecar_strict(_dump(data))
        self.assertEqual(ctx.exception.code, "delta-schema-type")

    def test_bool_is_not_an_int(self):
        data = {**self._valid(), "dispatch_depth": True}
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._parse_sidecar_strict(_dump(data))
        self.assertEqual(ctx.exception.code, "delta-schema-type")

    def test_wrong_schema_version_is_rejected(self):
        data = {**self._valid(), "schema_version": 1}
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._parse_sidecar_strict(_dump(data))
        self.assertEqual(ctx.exception.code, "delta-schema-type")

    def test_malformed_json_is_rejected(self):
        with self.assertRaises(distribution._DeltaVerdictError) as ctx:
            distribution._parse_sidecar_strict(b"{not json")
        self.assertEqual(ctx.exception.code, "delta-malformed-json")


class SemanticReanchorEqualTest(unittest.TestCase):
    def setUp(self):
        self.old_root = Path("/old/dispatch")
        self.new_root = Path("/new/dispatch")

    def _reanchored(self) -> dict:
        src = _sidecar(self.old_root)
        tgt = dict(src)
        tgt["completion_marker"] = str(self.new_root / "completion/rt-test/execute.json")
        tgt["completion_marker_history"] = str(self.new_root / "completion/rt-test/execute.3.json")
        return src, tgt

    def test_20_exact_reanchor_is_semantic_equal(self):
        src, tgt = self._reanchored()
        ok, code = distribution._semantic_reanchor_equal(
            _dump(src), _dump(tgt), self.old_root, self.new_root, "completion/rt-test/execute.a.attempt.json"
        )
        self.assertTrue(ok, code)
        self.assertEqual(code, "semantic-equal")

    def test_20a_unchanged_forged_marker_is_source_unowned(self):
        src, tgt = self._reanchored()
        src["completion_marker"] = "/unrelated/forged"
        tgt["completion_marker"] = "/unrelated/forged"  # target left byte-identical to forged source
        ok, code = distribution._semantic_reanchor_equal(
            _dump(src), _dump(tgt), self.old_root, self.new_root, "rel"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "delta-source-unowned")

    def test_20b_target_only_forged_reanchor(self):
        src, tgt = self._reanchored()
        tgt["completion_marker"] = "/unrelated/forged"
        ok, code = distribution._semantic_reanchor_equal(
            _dump(src), _dump(tgt), self.old_root, self.new_root, "rel"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "delta-forged-reanchor")

    def test_20c_content_change_outside_markers_is_rejected(self):
        src, tgt = self._reanchored()
        tgt["evidence_sha256"] = "b" * 64
        ok, code = distribution._semantic_reanchor_equal(
            _dump(src), _dump(tgt), self.old_root, self.new_root, "rel"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "delta-content-change")

    def test_20c_malformed_json_is_rejected(self):
        src, tgt = self._reanchored()
        ok, code = distribution._semantic_reanchor_equal(
            b"{not json", _dump(tgt), self.old_root, self.new_root, "rel"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "delta-malformed-json")

    def test_20e_source_non_canonical_serialization_is_rejected(self):
        src, tgt = self._reanchored()
        # write_once's exact serialization is indent=2; a compact re-encode of
        # the identical value is a different byte string.
        noncanonical = json.dumps(src, ensure_ascii=False).encode("utf-8")
        ok, code = distribution._semantic_reanchor_equal(
            noncanonical, _dump(tgt), self.old_root, self.new_root, "rel"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "delta-source-serialization")

    def test_20f_sequence_mismatch_is_forged_reanchor(self):
        src, tgt = self._reanchored()
        tgt["completion_marker_history"] = str(self.new_root / "completion/rt-test/execute.9.json")
        ok, code = distribution._semantic_reanchor_equal(
            _dump(src), _dump(tgt), self.old_root, self.new_root, "rel"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "delta-forged-reanchor")

    def test_20f_target_serialization_mismatch_is_rejected(self):
        src, tgt = self._reanchored()
        compact_tgt = json.dumps(tgt, ensure_ascii=False).encode("utf-8")
        ok, code = distribution._semantic_reanchor_equal(
            _dump(src), compact_tgt, self.old_root, self.new_root, "rel"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "delta-serialization-mismatch")

    @staticmethod
    def _reversed_dump(value: dict) -> bytes:
        """Same content, insertion order reversed (round 2 D4)."""
        return _dump(dict(reversed(list(value.items()))))

    def test_20g_key_order_matrix_only_forward_forward_is_equal(self):
        # Round-1 test-stage Finding 1's exact four-combination matrix: a
        # value-identical target with valid reanchored markers, source and
        # target key order independently forward/reversed. Before D4, all
        # four returned "semantic-equal"; only forward/forward should.
        src, tgt = self._reanchored()
        combos = {
            ("forward", "forward"): (_dump(src), _dump(tgt), True, "semantic-equal"),
            ("forward", "reversed"): (
                _dump(src), self._reversed_dump(tgt), False, "delta-serialization-mismatch",
            ),
            ("reversed", "forward"): (
                self._reversed_dump(src), _dump(tgt), False, "delta-source-key-order",
            ),
            ("reversed", "reversed"): (
                self._reversed_dump(src), self._reversed_dump(tgt), False, "delta-source-key-order",
            ),
        }
        for combo, (src_bytes, tgt_bytes, expect_ok, expect_code) in combos.items():
            with self.subTest(combo=combo):
                ok, code = distribution._semantic_reanchor_equal(
                    src_bytes, tgt_bytes, self.old_root, self.new_root, "rel"
                )
                self.assertEqual(ok, expect_ok, code)
                self.assertEqual(code, expect_code)

    def test_20h_source_key_order_negative_isolated(self):
        """Independent source-order negative: only the source is reordered."""
        src, tgt = self._reanchored()
        ok, code = distribution._semantic_reanchor_equal(
            self._reversed_dump(src), _dump(tgt), self.old_root, self.new_root, "rel"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "delta-source-key-order")

    def test_20i_target_key_order_negative_isolated(self):
        """Independent target-order negative: only the target is reordered."""
        src, tgt = self._reanchored()
        ok, code = distribution._semantic_reanchor_equal(
            _dump(src), self._reversed_dump(tgt), self.old_root, self.new_root, "rel"
        )
        self.assertFalse(ok)
        self.assertEqual(code, "delta-serialization-mismatch")


class MigrationDeletionPreconditionTest(HermeticCase):
    def evidence_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-B", distribution.__file__, "release-evidence", *args, "--json"],
            env=dict(os.environ), cwd=self.root, text=True, capture_output=True, timeout=30)

    def test_20_exact_two_key_reanchor_is_prunable_via_carry_forward(self):
        self.promote()
        candidate = self.candidate()
        old_dispatch = candidate / ".dispatch"
        sidecar = _sidecar(old_dispatch, marker_root=old_dispatch)
        relative = "completion/rt-test/execute.att-test-1.attempt.json"
        sidecar_path = old_dispatch / relative
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_bytes(_dump(sidecar))
        (old_dispatch / "logs").mkdir()
        (old_dispatch / "logs" / "steady.log").write_bytes(b"unchanged bytes")

        self.assertTrue(distribution._succeed_dispatch_state(candidate))
        ok, reason = distribution._migration_deletion_precondition(candidate, os.environ)
        self.assertTrue(ok, reason)

    def test_23_grown_log_is_archived_then_reconciles_prunable(self):
        self.promote()
        candidate = self.candidate()
        old_dispatch = candidate / ".dispatch"
        original_log = b"x" * 292
        (old_dispatch / "logs").mkdir()
        (old_dispatch / "logs" / "dispatch-session-sweep.log").write_bytes(original_log)
        # The successor root already has a *different*, longer log that does
        # not contain the original bytes -- carry-forward's additive
        # `destination.exists() -> continue` will never overwrite it.
        successor_log_dir = self.stable_root / "logs"
        successor_log_dir.mkdir(parents=True, exist_ok=True)
        (successor_log_dir / "dispatch-session-sweep.log").write_bytes(b"y" * 533842)

        self.assertTrue(distribution._succeed_dispatch_state(candidate))
        ok, reason = distribution._migration_deletion_precondition(candidate, os.environ)
        self.assertFalse(ok)
        self.assertIn("evidence-unarchived", reason)

        verdicts = distribution._migration_delta_verdicts(candidate, os.environ)
        unproven = [v for v in verdicts if v["verdict"] in ("missing-target", "differing")]
        self.assertEqual(len(unproven), 1)
        self.assertEqual(unproven[0]["relative"], "logs/dispatch-session-sweep.log")
        receipt = distribution._archive_release_evidence(candidate, os.environ, unproven, expect=None)
        self.assertEqual(receipt["entries"][0]["source_bytes"], 292)

        ok, reason = distribution._migration_deletion_precondition(candidate, os.environ)
        self.assertTrue(ok, reason)
        archived = candidate.parent  # sanity: candidate itself untouched
        self.assertTrue(candidate.is_dir())
        self.assertEqual(
            (self.stable_root / "logs" / "dispatch-session-sweep.log").read_bytes(),
            b"y" * 533842,
        )  # successor never overwritten

    def _archived_fixture(self):
        self.promote()
        candidate = self.candidate()
        old_dispatch = candidate / ".dispatch"
        (old_dispatch / "logs").mkdir()
        (old_dispatch / "logs" / "grown.log").write_bytes(b"z" * 50)
        successor_dir = self.stable_root / "logs"
        successor_dir.mkdir(parents=True, exist_ok=True)
        (successor_dir / "grown.log").write_bytes(b"different" * 10)
        self.assertTrue(distribution._succeed_dispatch_state(candidate))
        verdicts = distribution._migration_delta_verdicts(candidate, os.environ)
        unproven = [v for v in verdicts if v["verdict"] in ("missing-target", "differing")]
        receipt = distribution._archive_release_evidence(candidate, os.environ, unproven, expect=None)
        return candidate, unproven, receipt

    def test_24_deleted_archive_file_blocks_and_preserves_candidate(self):
        candidate, unproven, receipt = self._archived_fixture()
        archive_root = distribution._release_evidence_archive_root(os.environ, candidate)
        # destructive-ok: reason=simulate archive-file loss inside a disposable test fixture archive; boundary=one exact path under self.root
        (archive_root / "files" / "logs" / "grown.log").unlink()
        self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, unproven))
        ok, reason = distribution._migration_deletion_precondition(candidate, os.environ)
        self.assertFalse(ok)
        self.assertTrue(candidate.is_dir())

    def test_24b_tampered_archive_file_blocks(self):
        candidate, unproven, receipt = self._archived_fixture()
        archive_root = distribution._release_evidence_archive_root(os.environ, candidate)
        target = archive_root / "files" / "logs" / "grown.log"
        target.write_bytes(b"tampered")
        self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, unproven))

    def test_24c_receipt_tamper_variants_are_rejected(self):
        candidate, unproven, receipt = self._archived_fixture()
        archive_root = distribution._release_evidence_archive_root(os.environ, candidate)
        receipt_path = archive_root / "receipt.json"

        tampered = dict(receipt)
        tampered["inventory_digest"] = "sha256:" + "0" * 64
        receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, unproven))

        tampered = dict(receipt)
        tampered["entries"] = list(receipt["entries"]) + [dict(receipt["entries"][0])]
        receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, unproven))

        tampered = dict(receipt)
        tampered["surviving_root"] = "/somewhere/else"
        receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, unproven))

    def test_24d_stale_receipt_plus_new_differing_file_is_rejected(self):
        candidate, unproven, receipt = self._archived_fixture()
        old_dispatch = candidate / ".dispatch"
        (old_dispatch / "logs" / "another.log").write_bytes(b"newly different" * 3)
        (self.stable_root / "logs" / "another.log").write_bytes(b"successor value")
        # The receipt only proves the original entry set; a newly-differing
        # file changes the *current* unproven set out from under it.
        current_verdicts = distribution._migration_delta_verdicts(candidate, os.environ)
        current_unproven = [v for v in current_verdicts if v["verdict"] in ("missing-target", "differing")]
        self.assertEqual({v["relative"] for v in current_unproven}, {"logs/grown.log", "logs/another.log"})
        self.assertFalse(
            distribution._release_evidence_receipt_accepted(candidate, os.environ, current_unproven)
        )
        ok, reason = distribution._migration_deletion_precondition(candidate, os.environ)
        self.assertFalse(ok)
        self.assertTrue(candidate.is_dir())

    def test_24e_successor_change_requires_fresh_observation_not_new_archive(self):
        candidate, unproven, receipt = self._archived_fixture()
        successor = self.stable_root / "logs" / "grown.log"
        successor.write_bytes(b"a completely new successor value")
        # A verdict collected before a concurrent change must still fail.
        self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, unproven))
        current = distribution._migration_delta_verdicts(candidate, os.environ)
        self.assertTrue(distribution._release_evidence_receipt_accepted(candidate, os.environ, current))
        # Absence is a current observation too; a later file appearance races it.
        # destructive-ok: reason=simulate a mutable successor disappearing in an isolated fixture; boundary=one fixture successor log
        successor.unlink()
        missing = distribution._migration_delta_verdicts(candidate, os.environ)
        self.assertEqual(missing[0]["verdict"], "missing-target")
        self.assertTrue(distribution._release_evidence_receipt_accepted(candidate, os.environ, missing))
        successor.write_bytes(b"appeared after observation")
        self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, missing))
        # Unsafe current targets are still blocked before any archive approval.
        foreign = self.root / "foreign.log"
        foreign.write_bytes(b"keep this foreign file")
        successor.unlink()  # same owned fixture leaf as above
        successor.symlink_to(foreign)
        check = self.evidence_cli("reconcile", "--release", candidate.name)
        self.assertEqual(check.returncode, 0, check.stderr)
        result = json.loads(check.stdout)[candidate.name]
        self.assertFalse(result["evidence_accepted"])
        self.assertEqual(result["verdicts"][0]["verdict"], "blocked")
        self.assertEqual(foreign.read_bytes(), b"keep this foreign file")

    def test_23b_supported_archive_then_520_byte_append_keeps_original_proof(self):
        self.promote()
        candidate = self.candidate("v-archive-append")
        relative = "logs/dispatch-session-sweep.log"
        source = candidate / ".dispatch" / relative
        source.parent.mkdir()
        source.write_bytes(b"x" * 292)
        successor = self.stable_root / relative
        successor.parent.mkdir(parents=True, exist_ok=True)
        successor.write_bytes(b"y" * 588282)
        plan_path = self.root / "append-plan.json"
        planned = self.evidence_cli("plan", "--release", candidate.name, "--out", str(plan_path))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertEqual(len(plan["entries"]), 1)
        applied = self.evidence_cli("apply", "--plan", str(plan_path),
            "--expect", plan["plan_digest"], "--expect-receipt", "absent")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        receipt = json.loads(applied.stdout)
        archive = distribution._release_evidence_archive_root(os.environ, candidate)
        before = ArchiveAdversarialTest.inventory(archive)
        with successor.open("ab") as handle:
            handle.write(b"z" * 520)
        checked = self.evidence_cli("reconcile", "--release", candidate.name)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        result = json.loads(checked.stdout)[candidate.name]
        self.assertEqual(result["unproven_count"], 1)
        self.assertTrue(result["evidence_accepted"])
        self.assertTrue(result["prunable"])
        self.assertEqual(source.read_bytes(), b"x" * 292)
        self.assertEqual((archive / "files" / relative).read_bytes(), b"x" * 292)
        self.assertEqual(successor.read_bytes(), b"y" * 588282 + b"z" * 520)
        self.assertEqual(before, ArchiveAdversarialTest.inventory(archive))
        self.assertEqual(receipt, json.loads((archive / "receipt.json").read_text()))
        self.assertTrue(candidate.is_dir())  # evidence checking never prunes

    def test_24g_plan_apply_successor_append_still_refuses_without_archive(self):
        self.promote()
        candidate = self.candidate("v-archive-stale")
        source = candidate / ".dispatch/logs/old.log"
        source.parent.mkdir()
        source.write_bytes(b"original" * 10)
        successor = self.stable_root / "logs/old.log"
        successor.parent.mkdir(parents=True, exist_ok=True)
        successor.write_bytes(b"successor before plan")
        plan_path = self.root / "stale-plan.json"
        planned = self.evidence_cli("plan", "--release", candidate.name, "--out", str(plan_path))
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        archive = distribution._release_evidence_archive_root(os.environ, candidate)
        before = ArchiveAdversarialTest.inventory(archive.parent)
        with successor.open("ab") as handle:
            handle.write(b"a" * 520)
        applied = self.evidence_cli("apply", "--plan", str(plan_path),
            "--expect", plan["plan_digest"], "--expect-receipt", "absent")
        self.assertNotEqual(applied.returncode, 0)
        self.assertEqual(json.loads(applied.stdout)["status"], "failed")
        self.assertIn("archive-successor-changed", json.loads(applied.stdout)["error"])
        self.assertEqual(before, ArchiveAdversarialTest.inventory(archive.parent))
        self.assertEqual(source.read_bytes(), b"original" * 10)
        self.assertEqual(successor.read_bytes(), b"successor before plan" + b"a" * 520)

    def test_24h_archive_identity_and_current_inventory_still_fail_closed(self):
        candidate, unproven, receipt = self._archived_fixture()
        archive = distribution._release_evidence_archive_root(os.environ, candidate)
        entry = unproven[0]
        for changed in ([entry, dict(entry)],
                        [dict(entry, source_bytes=49)],
                        [dict(entry, source_sha256="f" * 64)],
                        [entry, dict(entry, relative="logs/new.log")]):
            self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, changed))
        for path in (candidate / ".dispatch/logs/grown.log", archive / "files/logs/grown.log"):
            original = path.read_bytes()
            try:
                path.write_bytes(b"!" * len(original))  # equal length is not full-byte preservation
                current = distribution._migration_delta_verdicts(candidate, os.environ)
                self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, current))
            finally:
                path.write_bytes(original)
        receipt_path = archive / "receipt.json"
        original = receipt_path.read_bytes()
        try:
            tampered = json.loads(original)
            tampered["entries"][0]["source_bytes"] = 49
            tampered["inventory_digest"] = "sha256:" + hashlib.sha256(
                distribution._canonical_json_bytes(tampered["entries"])).hexdigest()
            receipt_path.write_text(json.dumps(tampered))
            # Even a matching fabricated size in the caller's inventory
            # cannot override the actual full source/archive byte lengths.
            self.assertFalse(distribution._release_evidence_receipt_accepted(
                candidate, os.environ, [dict(entry, source_bytes=49)]))
        finally:
            receipt_path.write_bytes(original)
        self.assertTrue(distribution._release_evidence_receipt_accepted(candidate, os.environ, unproven))

    def test_24f_receipt_cas_conflict_writes_nothing_new(self):
        candidate, unproven, receipt = self._archived_fixture()
        archive_root = distribution._release_evidence_archive_root(os.environ, candidate)
        before = sorted(p.relative_to(archive_root).as_posix() for p in archive_root.rglob("*") if p.is_file())
        before_bytes = {p: (archive_root / p).read_bytes() for p in before}
        with self.assertRaises(distribution.DistributionError) as ctx:
            distribution._archive_release_evidence(
                candidate, os.environ, unproven, expect="sha256:" + "1" * 64
            )
        self.assertIn("archive-receipt-conflict", str(ctx.exception))
        after = sorted(p.relative_to(archive_root).as_posix() for p in archive_root.rglob("*") if p.is_file())
        self.assertEqual(before, after)
        for path in before:
            self.assertEqual(before_bytes[path], (archive_root / path).read_bytes())

    def test_25_archive_retry_is_idempotent(self):
        candidate, unproven, receipt = self._archived_fixture()
        again = distribution._archive_release_evidence(
            candidate, os.environ, unproven, expect=receipt["inventory_digest"]
        )
        self.assertEqual(again["inventory_digest"], receipt["inventory_digest"])

    def test_25_archive_content_conflict_at_same_relative(self):
        candidate, unproven, receipt = self._archived_fixture()
        # Mutate the *source* after a successful archive: a retry now sees a
        # different digest than what is already sealed at the same relative
        # path, which is a real conflict -- not an idempotent no-op. The
        # stale caller-supplied ``unproven`` entry (still carrying the old
        # source_sha256/source_bytes) is caught by the source-binding check
        # before any archive-tree comparison is even reached (Repair B1).
        (candidate / ".dispatch" / "logs" / "grown.log").write_bytes(b"mutated-source-content")
        with self.assertRaises(distribution.DistributionError) as ctx:
            distribution._archive_release_evidence(
                candidate, os.environ, unproven, expect=receipt["inventory_digest"]
            )
        self.assertIn("archive-source-changed", str(ctx.exception))

    def test_25b_archive_content_conflict_with_fresh_verdicts_at_same_relative(self):
        """The same real conflict, but re-derived via a fresh verdict pass (no
        stale caller-supplied source_sha256/source_bytes) so it is caught by
        the archive-tree comparison itself, not the source-binding check."""
        candidate, unproven, receipt = self._archived_fixture()
        (candidate / ".dispatch" / "logs" / "grown.log").write_bytes(b"mutated-source-content")
        fresh_verdicts = distribution._migration_delta_verdicts(candidate, os.environ)
        fresh_unproven = [e for e in fresh_verdicts if e["verdict"] in ("missing-target", "differing")]
        with self.assertRaises(distribution.DistributionError) as ctx:
            distribution._archive_release_evidence(
                candidate, os.environ, fresh_unproven, expect=receipt["inventory_digest"]
            )
        self.assertIn("archive-conflict", str(ctx.exception))

    def test_27_failed_apply_leaves_no_trace(self):
        candidate, unproven, receipt = self._archived_fixture()
        archive_root = distribution._release_evidence_archive_root(os.environ, candidate)
        before_files = sorted(p.as_posix() for p in archive_root.rglob("*"))
        before_digest = {p: hashlib.sha256((archive_root / p).read_bytes()).hexdigest()
                         for p in before_files if (archive_root / p).is_file()}
        with self.assertRaises(distribution.DistributionError):
            distribution._archive_release_evidence(
                candidate, os.environ, unproven, expect="sha256:" + "9" * 64
            )
        after_files = sorted(p.as_posix() for p in archive_root.rglob("*"))
        self.assertEqual(before_files, after_files)
        for relative, digest in before_digest.items():
            self.assertEqual(hashlib.sha256((archive_root / relative).read_bytes()).hexdigest(), digest)

    def test_28_conflict_on_second_entry_leaves_archive_tree_untouched(self):
        """Repair B1: an absent-then-conflicting pair in one call must never
        land the absent one and then fail on the conflicting one -- either
        the whole call lands, or the archive tree gains nothing new."""
        self.promote()
        candidate = self.candidate()
        old_dispatch = candidate / ".dispatch"
        (old_dispatch / "logs").mkdir()
        (old_dispatch / "logs" / "a-new.log").write_bytes(b"a-new-source")
        (old_dispatch / "logs" / "z-existing.log").write_bytes(b"z-source-now")
        # Both need a "differing" verdict (not "equal"): pre-seed each
        # successor with content that differs from the source *before*
        # `_succeed_dispatch_state`'s additive carry-forward runs, so the
        # carry-forward's "already present wins" rule leaves both mismatched.
        successor_dir = self.stable_root / "logs"
        successor_dir.mkdir(parents=True, exist_ok=True)
        (successor_dir / "a-new.log").write_bytes(b"a-new-successor-differs")
        (successor_dir / "z-existing.log").write_bytes(b"z-successor-differs")
        self.assertTrue(distribution._succeed_dispatch_state(candidate))
        verdicts = distribution._migration_delta_verdicts(candidate, os.environ)
        unproven = [v for v in verdicts if v["verdict"] in ("missing-target", "differing")]
        self.assertEqual({v["relative"] for v in unproven}, {"logs/a-new.log", "logs/z-existing.log"})

        archive_root = distribution._release_evidence_archive_root(os.environ, candidate)
        # Pre-seed the archive with a *conflicting* z-existing.log, sorted
        # after a-new.log alphabetically -- reproducing the "first entry
        # absent, second entry conflicting" adversarial ordering.
        files_root = archive_root / "files" / "logs"
        files_root.mkdir(parents=True)
        os.chmod(archive_root, 0o700)
        os.chmod(archive_root / "files", 0o700)
        os.chmod(files_root, 0o700)
        (files_root / "z-existing.log").write_bytes(b"z-source-different-from-now")

        with self.assertRaises(distribution.DistributionError) as ctx:
            distribution._archive_release_evidence(candidate, os.environ, unproven, expect=None)
        self.assertIn("archive-conflict", str(ctx.exception))
        # The absent a-new.log must never have been created by the same call
        # that went on to fail on z-existing.log.
        self.assertFalse((files_root / "a-new.log").exists())
        self.assertEqual((files_root / "z-existing.log").read_bytes(),
                         b"z-source-different-from-now")
        self.assertFalse((archive_root / "receipt.json").exists())

    def test_29_source_changed_between_plan_and_apply_is_refused(self):
        """Repair B1: a source file mutated after `plan` (292 -> 293 bytes,
        matching the real v2.94.1 dispatch-session-sweep.log incident) must
        never be silently archived under the new bytes at `apply` time."""
        self.promote()
        candidate = self.candidate("v-drift")
        old_dispatch = candidate / ".dispatch"
        (old_dispatch / "logs").mkdir()
        original = b"x" * 292
        (old_dispatch / "logs" / "dispatch-session-sweep.log").write_bytes(original)
        (self.stable_root / "logs").mkdir(parents=True, exist_ok=True)
        (self.stable_root / "logs" / "dispatch-session-sweep.log").write_bytes(b"y" * 533842)
        self.assertTrue(distribution._succeed_dispatch_state(candidate))

        plan_path = self.root / "drift-plan.json"
        plan = distribution._release_evidence_plan("v-drift", plan_path)
        self.assertEqual(plan["entries"][0]["source_bytes"], 292)

        # The source changes underneath the already-written plan.
        (old_dispatch / "logs" / "dispatch-session-sweep.log").write_bytes(b"x" * 293)

        with self.assertRaises(distribution.DistributionError) as ctx:
            distribution._release_evidence_apply(plan_path, plan["plan_digest"], None)
        self.assertIn("archive-source-changed", str(ctx.exception))

        archive_root = distribution._release_evidence_archive_root(os.environ, candidate)
        self.assertFalse(archive_root.exists())
        checked = distribution._release_evidence_check("v-drift")
        self.assertFalse(checked["v-drift"]["prunable"])

    def test_30_archive_publish_is_fsynced_before_return(self):
        """Repair B4: the archived file, its new directory levels, the files
        root, the archive root, and the published receipt are all fsynced
        under the lock, before any receipt/prune decision can rely on them."""
        calls = []
        real_fsync = os.fsync

        def counting_fsync(fd):
            calls.append(fd)
            return real_fsync(fd)

        self.promote()
        candidate = self.candidate("v-fsync")
        old_dispatch = candidate / ".dispatch"
        (old_dispatch / "nested" / "deep").mkdir(parents=True)
        (old_dispatch / "nested" / "deep" / "leaf.log").write_bytes(b"fsync-me")
        successor_dir = self.stable_root / "nested" / "deep"
        successor_dir.mkdir(parents=True, exist_ok=True)
        (successor_dir / "leaf.log").write_bytes(b"fsync-me-successor-differs")
        self.assertTrue(distribution._succeed_dispatch_state(candidate))
        verdicts = distribution._migration_delta_verdicts(candidate, os.environ)
        unproven = [v for v in verdicts if v["verdict"] in ("missing-target", "differing")]
        self.assertEqual(len(unproven), 1)

        original = os.fsync
        os.fsync = counting_fsync
        try:
            distribution._archive_release_evidence(candidate, os.environ, unproven, expect=None)
        finally:
            os.fsync = original
        # At least: the archived file's own handle, the new "nested" and
        # "nested/deep" directory levels, files_root, archive_root, and the
        # receipt file itself.
        self.assertGreaterEqual(len(calls), 6)

    def test_31_apply_rejects_a_release_name_outside_the_canonical_child_set(self):
        """Repair B5: `plan["release"]` must be an exact, existing child of
        the canonical managed releases root -- the plan-digest check alone
        does not prove this, since an attacker who controls both a hand-built
        plan and its own --expect can make any release value self-consistent."""
        self.promote()
        candidate = self.candidate("v-escape")
        old_dispatch = candidate / ".dispatch"
        (old_dispatch / "logs").mkdir()
        (old_dispatch / "logs" / "a.log").write_bytes(b"a")
        self.assertTrue(distribution._succeed_dispatch_state(candidate))

        releases_root = distribution.data_root() / "releases"
        secret = self.root / "outside-secret"
        secret.mkdir()
        (secret / "marker.txt").write_bytes(b"must-not-be-touched")

        for bad_release in ("../outside-secret", str(secret), "/etc"):
            plan = {"schema_version": 1, "release": bad_release, "entries": []}
            plan["plan_digest"] = "sha256:" + hashlib.sha256(
                distribution._canonical_json_bytes(
                    {k: v for k, v in plan.items() if k != "plan_digest"})
            ).hexdigest()
            plan_path = self.root / "escape-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.subTest(bad_release=bad_release):
                with self.assertRaises(distribution.DistributionError) as ctx:
                    distribution._release_evidence_apply(plan_path, plan["plan_digest"], None)
                self.assertIn("release not found", str(ctx.exception))
        self.assertEqual((secret / "marker.txt").read_bytes(), b"must-not-be-touched")

        # A symlink planted directly under the releases root, aliasing a real
        # release name, must also be refused rather than followed.
        alias = releases_root / "v-escape-alias"
        os.symlink(secret, alias)
        plan = {"schema_version": 1, "release": "v-escape-alias", "entries": []}
        plan["plan_digest"] = "sha256:" + hashlib.sha256(
            distribution._canonical_json_bytes(
                {k: v for k, v in plan.items() if k != "plan_digest"})
        ).hexdigest()
        plan_path = self.root / "escape-plan-symlink.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(distribution.DistributionError) as ctx:
            distribution._release_evidence_apply(plan_path, plan["plan_digest"], None)
        self.assertIn("release not found", str(ctx.exception))

    def test_26_unchanged_guards_still_govern_deletion(self):
        # A live-process hold or an unresolved projection reference is
        # untouched by any of this file's additions -- sanity-check the
        # functions are still present with their documented behavior on an
        # obviously-not-held candidate.
        self.promote()
        candidate = self.candidate()
        held, _ = distribution._release_held_by_live_process(candidate)
        self.assertFalse(held)
        self.assertFalse(distribution._release_projection_referenced(candidate))


class ArchiveAdversarialTest(HermeticCase):
    def fixture(self):
        self.promote()
        candidate = self.candidate("v-adversarial")
        (candidate / ".dispatch/logs").mkdir()
        (self.stable_root / "logs").mkdir(parents=True, exist_ok=True)
        for name in ("a.log", "z.log"):
            (candidate / ".dispatch/logs" / name).write_bytes(b"source-original")
            (self.stable_root / "logs" / name).write_bytes(b"successor")
        entries = [e for e in distribution._migration_delta_verdicts(candidate, os.environ)
                   if e["verdict"] in ("missing-target", "differing")]
        return candidate, entries, distribution._release_evidence_archive_root(os.environ, candidate)

    @staticmethod
    def inventory(root):
        if not root.exists():
            return {}
        return {p.relative_to(root).as_posix(): (stat.S_IFMT(p.lstat().st_mode),
                    p.read_bytes() if stat.S_ISREG(p.lstat().st_mode) else None)
                for p in root.rglob("*")}

    def test_receipt_fifo_and_symlink_refuse_without_mutation(self):
        candidate, entries, archive = self.fixture()
        archive.mkdir(parents=True)
        receipt = archive / "receipt.json"
        os.mkfifo(receipt)
        before = self.inventory(archive)
        with self.assertRaises(distribution.DistributionError):
            distribution._archive_release_evidence(candidate, os.environ, entries, expect="absent")
        self.assertEqual(before, self.inventory(archive))
        # destructive-ok: reason=replace the FIFO created by this negative fixture; boundary=exact private fixture receipt
        receipt.unlink()
        foreign = self.root / "foreign.json"; foreign.write_bytes(b"foreign")
        receipt.symlink_to(foreign)
        before = self.inventory(archive)
        with self.assertRaises(distribution.DistributionError):
            distribution._archive_release_evidence(candidate, os.environ, entries, expect="absent")
        self.assertEqual(before, self.inventory(archive))
        self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_root_ancestor_symlink_is_refused_by_the_shared_reader(self):
        actual = self.root / "actual"
        (actual / "nested").mkdir(parents=True)
        (actual / "nested/leaf").write_bytes(b"foreign bytes")
        alias = self.root / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(distribution._DeltaVerdictError):
            distribution._read_contained_bytes(alias / "nested", "leaf")

    def test_second_successor_changed_refuses_before_directory_creation(self):
        candidate, entries, archive = self.fixture()
        (self.stable_root / "logs/z.log").write_bytes(b"new successor")
        before = self.inventory(archive.parent)
        with self.assertRaisesRegex(distribution.DistributionError, "successor-changed"):
            distribution._archive_release_evidence(candidate, os.environ, entries, expect="absent")
        self.assertEqual(before, self.inventory(archive.parent))

    def test_second_archive_kind_conflict_is_prevalidated(self):
        candidate, entries, archive = self.fixture()
        (archive / "files/logs").mkdir(parents=True)
        os.mkfifo(archive / "files/logs/z.log")
        before = self.inventory(archive)
        with self.assertRaises(distribution.DistributionError):
            distribution._archive_release_evidence(candidate, os.environ, entries, expect="absent")
        self.assertEqual(before, self.inventory(archive))

    def test_same_bytes_replaced_source_identity_is_not_resealed(self):
        from unittest.mock import patch
        candidate, entries, archive = self.fixture()
        original = distribution._evidence_recheck
        changed = False
        def race(observations):
            nonlocal changed
            if not changed:
                changed = True
                path = candidate / ".dispatch/logs/z.log"
                successor = path.with_name("replacement")
                successor.write_bytes(path.read_bytes())
                # destructive-ok: reason=inject a same-byte inode replacement in an owned fixture; boundary=exact private source and replacement leaves
                os.replace(successor, path)
            return original(observations)
        before = self.inventory(archive.parent)
        with patch.object(distribution, "_evidence_recheck", side_effect=race):
            with self.assertRaisesRegex(distribution.DistributionError, "identity-conflict"):
                distribution._archive_release_evidence(candidate, os.environ, entries, expect="absent")
        self.assertEqual(before, self.inventory(archive.parent))

    def test_receipt_publish_race_preserves_foreign_successor(self):
        from unittest.mock import patch
        candidate, entries, archive = self.fixture()
        original = os.link
        def race(src, dst, *args, **kwargs):
            if dst == "receipt.json":
                (archive / "receipt.json").write_bytes(b"foreign successor")
            return original(src, dst, *args, **kwargs)
        with patch.object(distribution.os, "link", side_effect=race):
            with self.assertRaises((OSError, distribution.DistributionError)):
                distribution._archive_release_evidence(candidate, os.environ, entries, expect="absent")
        self.assertEqual((archive / "receipt.json").read_bytes(), b"foreign successor")
        self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, entries))

    def test_ancestor_swap_refuses_before_any_outside_staging_mutation(self):
        from unittest.mock import patch
        candidate, entries, archive = self.fixture()
        outside = self.root / "outside"
        (outside / "logs").mkdir(parents=True)
        publish, open_file, fsync = distribution._evidence_publish_new, os.open, os.fsync
        mutations = []
        swapped = False
        def race(path, raw, observations):
            nonlocal swapped
            if not swapped:
                swapped = True
                (archive / "files").rename(archive / "files-original")
                (archive / "files").symlink_to(outside, target_is_directory=True)
            return publish(path, raw, observations)
        def opening(path, flags, *args, **kwargs):
            fd = open_file(path, flags, *args, **kwargs)
            actual = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if flags & os.O_CREAT and actual.is_relative_to(outside):
                mutations.append(("create", str(actual)))
            return fd
        def syncing(fd):
            actual = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if stat.S_ISREG(os.fstat(fd).st_mode) and actual.is_relative_to(outside):
                mutations.append(("fsync", str(actual), actual.read_bytes()))
            return fsync(fd)
        before = self.inventory(outside)
        with patch.object(distribution, "_evidence_publish_new", side_effect=race), \
                patch.object(os, "open", side_effect=opening), \
                patch.object(os, "fsync", side_effect=syncing):
            with self.assertRaises(distribution.DistributionError):
                distribution._archive_release_evidence(candidate, os.environ, entries, expect="absent")
        self.assertTrue(swapped)
        self.assertEqual(mutations, [], "transient outside writes must not hide behind cleanup")
        self.assertEqual(before, self.inventory(outside))
        self.assertFalse((archive / "receipt.json").exists())

    def test_same_inode_changed_receipt_survives_failed_directory_fsync(self):
        from unittest.mock import patch
        candidate, entries, archive = self.fixture()
        link, fsync = os.link, os.fsync
        inodes, armed = [], False
        foreign = b"FOREIGN SAME INODE RECEIPT"
        def linked(src, dst, *args, **kwargs):
            nonlocal armed
            result = link(src, dst, *args, **kwargs)
            if dst == "receipt.json":
                receipt = archive / dst
                inodes.append(receipt.stat().st_ino)
                receipt.write_bytes(foreign)
                inodes.append(receipt.stat().st_ino)
                armed = True
            return result
        def syncing(fd):
            nonlocal armed
            if armed:
                armed = False
                raise OSError("injected receipt-directory fsync failure")
            return fsync(fd)
        with patch.object(os, "link", side_effect=linked), patch.object(os, "fsync", side_effect=syncing):
            with self.assertRaisesRegex(distribution.DistributionError, "ownership-conflict"):
                distribution._archive_release_evidence(candidate, os.environ, entries, expect="absent")
        self.assertEqual(len(inodes), 2)
        self.assertEqual(inodes[0], inodes[1])
        self.assertEqual((archive / "receipt.json").read_bytes(), foreign)
        self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, entries))
        for entry in entries:
            self.assertEqual((candidate / ".dispatch" / entry["relative"]).read_bytes(), b"source-original")

    def test_same_inode_changed_staging_survives_failed_file_fsync(self):
        from unittest.mock import patch
        target = self.root / "leaf.json"
        fsync, seen = os.fsync, []
        foreign = b"FOREIGN SAME INODE TEMP"
        def syncing(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode) and not seen:
                path = Path(os.readlink(f"/proc/self/fd/{fd}"))
                inode = path.stat().st_ino
                path.write_bytes(foreign)
                seen.append((path, inode, path.stat().st_ino))
                raise OSError("injected after in-place foreign staging write")
            return fsync(fd)
        with patch.object(os, "fsync", side_effect=syncing):
            with self.assertRaisesRegex(distribution.DistributionError, "staging-ownership-conflict"):
                distribution._evidence_publish_new(target, b"original", {target: None})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], seen[0][2])
        self.assertEqual(seen[0][0].read_bytes(), foreign)
        self.assertFalse(target.exists())

    def test_receipt_fsync_failure_cannot_leave_new_approval(self):
        from unittest.mock import patch
        candidate, entries, archive = self.fixture()
        link, fsync = os.link, os.fsync
        armed = False
        def linked(src, dst, *args, **kwargs):
            nonlocal armed
            result = link(src, dst, *args, **kwargs)
            if dst == "receipt.json":
                armed = True
            return result
        def fail_once(fd):
            nonlocal armed
            if armed:
                armed = False
                raise OSError("injected receipt-directory fsync failure")
            return fsync(fd)
        with patch.object(distribution.os, "link", side_effect=linked), patch.object(distribution.os, "fsync", side_effect=fail_once):
            with self.assertRaises(OSError):
                distribution._archive_release_evidence(candidate, os.environ, entries, expect="absent")
        self.assertFalse((archive / "receipt.json").exists())
        self.assertFalse(distribution._release_evidence_receipt_accepted(candidate, os.environ, entries))
        receipt = distribution._archive_release_evidence(candidate, os.environ, entries, expect="absent")
        self.assertTrue(distribution._release_evidence_receipt_accepted(candidate, os.environ, entries))
        self.assertEqual(len(receipt["entries"]), 2)


class CliSmokeTest(HermeticCase):
    def test_check_plan_apply_reconcile_round_trip(self):
        self.promote()
        candidate = self.candidate("v-cli")
        old_dispatch = candidate / ".dispatch"
        (old_dispatch / "logs").mkdir()
        (old_dispatch / "logs" / "grown.log").write_bytes(b"cli-fixture" * 5)
        successor = self.stable_root / "logs"
        successor.mkdir(parents=True, exist_ok=True)
        (successor / "grown.log").write_bytes(b"successor-value")
        self.assertTrue(distribution._succeed_dispatch_state(candidate))

        checked = distribution._release_evidence_check("v-cli")
        self.assertFalse(checked["v-cli"]["prunable"])

        plan_path = self.root / "plan.json"
        plan = distribution._release_evidence_plan("v-cli", plan_path)
        self.assertTrue(plan_path.is_file())

        receipt = distribution._release_evidence_apply(plan_path, plan["plan_digest"], None)
        self.assertEqual(receipt["release"], "v-cli")

        reconciled = distribution._release_evidence_reconcile("v-cli")
        self.assertTrue(reconciled["v-cli"]["prunable"], reconciled)

    def test_apply_rejects_wrong_plan_digest(self):
        self.promote()
        candidate = self.candidate("v-cli2")
        old_dispatch = candidate / ".dispatch"
        (old_dispatch / "logs").mkdir()
        (old_dispatch / "logs" / "grown.log").write_bytes(b"content")
        (self.stable_root / "logs").mkdir(parents=True, exist_ok=True)
        (self.stable_root / "logs" / "grown.log").write_bytes(b"other-content")
        self.assertTrue(distribution._succeed_dispatch_state(candidate))
        plan_path = self.root / "plan2.json"
        distribution._release_evidence_plan("v-cli2", plan_path)
        with self.assertRaises(distribution.DistributionError):
            distribution._release_evidence_apply(plan_path, "sha256:" + "0" * 64, None)


if __name__ == "__main__":
    unittest.main()
