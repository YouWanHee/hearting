"""A row's minted fallback tag never takes another row's collector tag.

Measured 2026-10-27 (Cairn report, v3.54.1): an OpenCode row with no session id yet sat before the
Cairn Claude row tagged ``39``. Its process fallback hashed to ``39`` too, so OVERVIEW showed
``[39] opencode`` and dropped the real ``[39]`` session and its route as a duplicate.
"""
import time
import unittest

from tools.fleet.explorer import live

# The measured pair: this OpenCode process seed hashes to "39".
_OPENCODE = {"harness": "opencode", "pid": 2099088, "proc_start": "138584073", "session_id": None,
             "cwd": "/home/nas/user/Uihyeop/NN_Zoo/BC_ResNet-wt/mac-demo-simple-ui-20261026"}
_CLAUDE = {"harness": "claude", "pid": 2100610, "proc_start": "138585008",
           "session_id": "9922f71a-2d7c-4e6f-aaa9-606f83f1ffc3", "session_tag": "39",
           "cwd": "/home/nas/user/Uihyeop/personal/cairn"}


class FallbackTagCollisionTest(unittest.TestCase):
    def rows(self, sessions):
        rows, _index = live._session_records({"sessions": sessions}, time.time(), None)
        return {row["harness"]: row["tag"] for row in rows}

    def test_measured_seed_hashes_to_the_collector_tag(self):
        self.assertEqual(live._tag_for(_OPENCODE, set()), "39")

    def test_collector_tag_keeps_its_row_when_a_fallback_row_comes_first(self):
        tags = self.rows([_OPENCODE, _CLAUDE])
        self.assertEqual(tags.get("claude"), "39")
        self.assertIn("opencode", tags)
        self.assertNotEqual(tags["opencode"], "39")

    def test_order_does_not_change_the_assignment(self):
        self.assertEqual(self.rows([_OPENCODE, _CLAUDE]), self.rows([_CLAUDE, _OPENCODE]))


if __name__ == "__main__":
    unittest.main()
