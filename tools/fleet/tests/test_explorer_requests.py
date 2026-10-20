import unittest
from tools.fleet.explorer.requests import build_tabs, select_fallback

class RequestTabsTests(unittest.TestCase):
    def test_owner_replacement_joins_exact_proven_cycle(self):
        tabs = build_tabs([
            {"parent_sid":"sid", "attempt_id":"a1", "route_id":"owner", "status":"killed"},
            {"parent_sid":"sid", "attempt_id":"a2", "route_id":"owner", "status":"done"}],
            [{"route_id":"owner", "parent_cycle_id":"cyc", "frame_route_id":"frame"}],
            [{"cycle_id":"cyc", "route_id":"frame", "title":"요청"}])
        self.assertEqual(len(tabs), 1)
        self.assertTrue(tabs[0].verified)
        self.assertTrue(tabs[0].complete)
        self.assertEqual(tabs[0].cycle_id, "cyc")

    def test_frame_only_and_same_slug_different_cycle_stay_separate(self):
        tabs = build_tabs([{"sid":"s", "attempt_id":"a", "route_id":"r1"},
                           {"sid":"s", "attempt_id":"b", "route_id":"r2"}],
                          [{"route_id":"r1", "parent_cycle_id":"c1", "frame_route_id":"f1"},
                           {"route_id":"r2", "parent_cycle_id":"c2", "frame_route_id":"f2"}],
                          [{"cycle_id":"c1", "route_id":"f1"}, {"cycle_id":"c2", "route_id":"f2"}])
        self.assertEqual(len(tabs), 2)
        self.assertNotEqual(tabs[0].identity, tabs[1].identity)

    def test_unverified_and_fallback(self):
        tabs = build_tabs([{"sid":"s", "attempt_id":"a", "route_id":"r"}], [], [])
        self.assertFalse(tabs[0].verified)
        self.assertEqual(tabs[0].label, "연결 미확인")
        self.assertEqual(select_fallback(tabs, "gone"), tabs[0].identity)
