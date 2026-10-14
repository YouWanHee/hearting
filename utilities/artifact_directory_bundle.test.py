import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import artifact_directory_bundle as B


class DirectoryBundleTests(unittest.TestCase):
    def test_descriptor_closed_canonical_vector(self):
        body = {"schema": B.SCHEMA, "directory": {"kind": "cycle-relative", "path": "artifacts/raw-results"},
                "file_count": 1, "total_bytes": 0, "tree_digest": "sha256:" + "0" * 64,
                "digest_algorithm": B.ALGORITHM}
        data = B.canonical_descriptor_bytes(body)
        self.assertEqual(data, json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(B.parse_descriptor(data), body)
        for bad in (data[:-1] + b",\"extra\":1}", data[:-2] + b"}}",
                    data.replace(b'"file_count":1', b'"file_count":true')):
            with self.assertRaises(B.DirectoryBundleError):
                B.parse_descriptor(bad)
        with self.assertRaises(B.DirectoryBundleError):
            B.parse_descriptor(b'{"schema":"x","schema":"y"}')

    def test_selector_outermost_hidden_empty_and_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "artifacts"
            (a / ".hidden" / "raw-results" / "nested" / "raw-results").mkdir(parents=True)
            (a / "raw-results").mkdir()
            (a / "raw-results-file").write_text("ordinary")
            got = B.select_outermost(a)
            self.assertEqual(got, ("artifacts/.hidden/raw-results", "artifacts/raw-results"))
            (a / "raw-results" / "x.txt").write_bytes(b"abc")
            tree = B.scan_tree(Path(tmp), "artifacts/raw-results")
            self.assertEqual(tree.body["file_count"], 1)
            self.assertEqual(tree.leaves[0].digest, "sha256:" + hashlib.sha256(b"abc").hexdigest())

    def test_tree_digest_unicode_order_same_size_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "artifacts" / "raw-results"
            base.mkdir(parents=True)
            (base / "é").write_bytes(b"a")
            (base / "z").write_bytes(b"b")
            one = B.scan_tree(Path(tmp), "artifacts/raw-results")
            (base / "z").write_bytes(b"c")
            two = B.scan_tree(Path(tmp), "artifacts/raw-results")
            self.assertNotEqual(one.body["tree_digest"], two.body["tree_digest"])
            self.assertEqual([x.path for x in one.leaves], ["z", "é"])

    def test_tree_race_refuses_unsealed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "artifacts" / "raw-results"
            base.mkdir(parents=True)
            (base / "empty").write_bytes(b"")
            with self.assertRaises(B.DirectoryBundleError):
                B.scan_tree(Path(tmp), "artifacts/raw-results/../raw-results")

    def test_streaming_hash_and_copy_memory_bound(self):
        self.assertEqual(B.CHUNK_BYTES, 1024 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source"
            dst = Path(tmp) / "destination"
            src.write_bytes(b"streamed payload")
            digest, size = B.copy_file_streaming(src, dst, expected_size=16)
            self.assertEqual(dst.read_bytes(), b"streamed payload")
            self.assertEqual(size, 16)
            self.assertEqual(digest, "sha256:" + hashlib.sha256(b"streamed payload").hexdigest())

    def test_selector_rejects_raw_symlink_and_tree_rejects_special(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = Path(tmp) / "artifacts"
            artifacts.mkdir()
            target = artifacts / "elsewhere"
            target.mkdir()
            (artifacts / "raw-results").symlink_to(target, target_is_directory=True)
            with self.assertRaises(B.DirectoryBundleError):
                B.select_outermost(artifacts)

    def test_bundle_cardinality_1_100_10000(self):
        # Descriptor has a constant artifact/revision/event cardinality independent of leaves.
        results = []
        for count in (1, 100, 10000):
            leaves = [[f"f{i:05d}", "sha256:" + "0" * 64, 1] for i in range(count)]
            body = {"schema": B.SCHEMA, "directory": {"kind": "cycle-relative", "path": "artifacts/raw-results"},
                    "file_count": count, "total_bytes": count,
                    "tree_digest": "sha256:" + hashlib.sha256(B._canonical(leaves)).hexdigest(),
                    "digest_algorithm": B.ALGORITHM}
            results.append((1, 1, 1, len(B.canonical_descriptor_bytes(body))))
        self.assertEqual([x[:3] for x in results], [(1, 1, 1)] * 3)


if __name__ == "__main__":
    unittest.main()
