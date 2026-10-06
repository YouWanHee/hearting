#!/usr/bin/env python3
"""The three bootstraps carry the one shared dispatch source, byte for byte."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sync_bootstrap", ROOT / "tools/sync-bootstrap-dispatch.py")
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)


class BootstrapDispatchTest(unittest.TestCase):
    def test_every_bootstrap_block_is_the_source(self):
        body = (ROOT / S.SOURCE).read_text(encoding="utf-8").rstrip()
        for target in S.TARGETS:
            with self.subTest(target=target):
                text = (ROOT / target).read_text(encoding="utf-8")
                block = text.split(S.BEGIN, 1)[1].split(S.END, 1)[0].strip("\n")
                self.assertEqual(block, body)

    def test_a_bootstrap_without_one_marker_pair_is_refused(self):
        for text in ("no markers", f"{S.BEGIN}\n{S.BEGIN}\n{S.END}", f"{S.END}\n{S.BEGIN}"):
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                S.render(text, "body", "fixture")
        self.assertEqual(S.render(f"a\n{S.BEGIN}\nold\n{S.END}\nb", "new\n", "fixture"),
                         f"a\n{S.BEGIN}\nnew\n{S.END}\nb")


if __name__ == "__main__":
    unittest.main()
