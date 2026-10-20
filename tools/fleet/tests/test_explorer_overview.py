import unittest
from types import SimpleNamespace
from tools.fleet.explorer.overview import build_overview

class OverviewTests(unittest.TestCase):
    def test_only_observed_edges_and_pending_delivery_are_shown(self):
        snapshot = SimpleNamespace(route_entities=[{"kind":"parent", "source":"a", "target":"b"}], pending_delivery={"state":"pending"}, observations={"obligations":[{"kind":"review", "owner":None}]})
        rows = build_overview(snapshot)
        self.assertTrue(any("a → b" in row for row in rows))
        self.assertTrue(any("전달 대기" in row for row in rows))
        self.assertTrue(any("미귀속" in row for row in rows))
