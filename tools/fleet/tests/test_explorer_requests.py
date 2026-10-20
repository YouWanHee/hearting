import tempfile
import unittest

from tools.fleet.explorer.output import cell_width
from tools.fleet.explorer.requests import (
    build_tabs, header_text, open_result_lines, overflow_lines, select_fallback, short_label,
    tab_bar, visible_tabs)
from tools.fleet.explorer.types import AttemptRow
from tools.fleet.tests.explorer_fixture import collect, materialize, screen_main, tab

B6_CYCLE = "cyc_e24de1108c407808898103a46f3b3ea0"
OPENRIG_CYCLE = "cyc_41e03dac1f6d3fc630c71aca16e37f30"
BP3_CYCLE = "cyc_cdca42c0fbf24f2d1854fc79c94cbbe2"


def row(attempt_id, line, worker_type, route_id=None, owner_route_id=None, route_node=None,
        status="done", slug="slug", timestamp="2026-10-20T09:00:00Z"):
    return AttemptRow(attempt_id, line, status, slug, "claude", worker_type=worker_type,
                      route_id=route_id, owner_route_id=owner_route_id, route_node=route_node,
                      timestamp=timestamp)


class FixtureTabs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.result = collect(cls.fx)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_nine_requests_in_start_order_with_canonical_names(self):
        self.assertEqual(
            [(t.label, "끝남" if t.complete else "열림") for t in self.result.tabs],
            [("openrig 조사", "끝남"), ("b6-report", "끝남"), ("P2+P4", "끝남"), ("TUI 조사", "끝남"),
             ("b-p3", "열림"), ("herdr-tmux", "열림"), ("steward", "끝남"), ("owner-fleet", "열림"),
             ("fleet-explorer", "열림")])
        self.assertTrue(all(t.verified for t in self.result.tabs))

    def test_header_counts_latest_result_of_open_requests_only(self):
        self.assertEqual(header_text(self.result.header), "요청 9 · 열림 4 · 최신 실패 3 · 검토 0")
        self.assertEqual((self.result.header.total, self.result.header.open,
                          self.result.header.latest_failed, self.result.header.review), (9, 4, 3, 0))

    def test_tab_bar_matches_the_canonical_screens(self):
        b6, fleet = tab(self.result, "b6-report"), tab(self.result, "fleet-explorer")
        tabs = self.result.tabs
        self.assertEqual(tab_bar(tabs, active=b6.identity), screen_main("first", 100, 3, 1)[0])
        self.assertEqual(tab_bar(tabs, active=b6.identity), "개요 │ ▶b6-report │ b-p3 │ herdr │ owner │ fleet │ ‹+4 끝남›")
        self.assertEqual(tab_bar(tabs, active="", selected=b6.identity), screen_main("overview", 100, 3, 1)[0])
        self.assertEqual(tab_bar(tabs, active=fleet.identity, keep=[b6.identity]),
                         "개요 │ b6 │ b-p3 │ herdr │ owner │ ▶fleet-explorer │ ‹+4 끝남›")

    def test_closed_tabs_fold_but_open_ones_never_hide(self):
        b6 = tab(self.result, "b6-report")
        visible, hidden = visible_tabs(self.result.tabs, [b6.identity])
        self.assertEqual([t.label for t in visible],
                         ["b6-report", "b-p3", "herdr-tmux", "owner-fleet", "fleet-explorer"])
        self.assertEqual([t.label for t in hidden], ["openrig 조사", "P2+P4", "TUI 조사", "steward"])
        self.assertTrue(all(t.complete for t in hidden))

    def test_narrow_bar_shortens_names_only(self):
        b6 = tab(self.result, "b6-report")
        narrow = tab_bar(self.result.tabs, active=b6.identity, width=50)
        self.assertLessEqual(cell_width(narrow), 50)
        self.assertIn("▶b6-report", narrow)
        self.assertIn("‹+4 끝남›", narrow)

    def test_overflow_index_matches_the_canonical_screen(self):
        b6 = tab(self.result, "b6-report")
        lines = overflow_lines(self.result.tabs, self.result.header, keep=[b6.identity],
                               failures=open_result_lines(self.result.open_results))
        self.assertEqual(list(lines), screen_main("overflow", 100, 4, len(lines)))

    def test_frame_only_request_is_its_own_closed_tab(self):
        p2p4 = tab(self.result, "P2+P4")
        self.assertEqual(p2p4.attempt_ids, ("att-63452b62d90b2504878f64b9775e382a",))
        self.assertEqual((p2p4.complete, p2p4.verified, p2p4.owner_route_ids), (True, True, ()))
        self.assertEqual(p2p4.route_id, p2p4.frame_route_id)

    def test_leg0_owner_and_frame_share_one_tab_through_the_cycle(self):
        bp3 = tab(self.result, "b-p3")
        self.assertEqual(bp3.cycle_id, BP3_CYCLE)
        self.assertIn("att-163aac809e58bb1ef10ab702a634a07b", bp3.attempt_ids)       # frame
        self.assertIn("att-69814a640b08e076be18a3d648b63a4e", bp3.attempt_ids)       # -leg0-owner
        self.assertEqual(bp3.owner_route_ids, ("rt-caad53bdc377c0d7",))
        stage_rows = [r for r in bp3.attempt_ids if r.startswith(("att-edb35d80", "att-5daffad0", "att-e6e42dfc"))]
        self.assertEqual(len(stage_rows), 3)

    def test_owner_replacement_in_one_cycle_is_one_tab(self):
        openrig = tab(self.result, "openrig 조사")
        self.assertEqual(openrig.cycle_id, OPENRIG_CYCLE)
        self.assertEqual(openrig.owner_route_ids, ("rt-8fd88141c5f7c09d", "rt-b45cc23fa142a3f1"))
        self.assertEqual(openrig.route_id, "rt-b45cc23fa142a3f1")                    # the replacement owner
        owners = [a for a in openrig.attempt_ids if a.startswith(("att-94110dad", "att-126c433a"))]
        self.assertEqual(len(owners), 2)
        self.assertEqual(len([t for t in self.result.tabs if t.cycle_id == OPENRIG_CYCLE]), 1)

    def test_finished_owner_absent_from_the_snapshot_is_restored_from_the_registry(self):
        b6 = tab(self.result, "b6-report")
        self.assertIn("att-00aa67225bfa238981c45727573b54e5", b6.attempt_ids)
        self.assertNotIn("att-00aa67225bfa238981c45727573b54e5", self.fx.live_jobs)
        self.assertNotIn("att-00aa67225bfa238981c45727573b54e5", [j["attempt_id"] for j in self.fx.snapshot.jobs])
        self.assertTrue(b6.complete)


class UnconfirmedJoins(unittest.TestCase):
    def test_missing_cycle_record_leaves_owner_and_frame_unconfirmed(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root, drop_cycles={BP3_CYCLE})
            result = collect(fx)
            unconfirmed = [t for t in result.tabs if not t.verified]
            self.assertEqual([t.label for t in unconfirmed], ["연결 미확인", "연결 미확인"])
            self.assertEqual(sorted(t.route_id for t in unconfirmed), ["rt-3942cabe32950573", "rt-caad53bdc377c0d7"])
            self.assertTrue(all(t.cycle_id is None and t.reason for t in unconfirmed))
            self.assertEqual(len([t for t in result.tabs if t.verified]), 8)
            owner_tab = next(t for t in unconfirmed if t.route_id == "rt-caad53bdc377c0d7")
            self.assertFalse(owner_tab.complete)                  # the owner is still open
            self.assertEqual(len(owner_tab.attempt_ids), 4)       # owner + its three stage attempts

    def test_unreadable_owner_route_cannot_prove_a_join(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root, drop_routes={"rt-bca10c8fd3987697"})
            result = collect(fx)
            self.assertEqual(len(result.tabs), 10)
            broken = [t for t in result.tabs if not t.verified]
            self.assertEqual([t.route_id for t in broken], ["rt-bca10c8fd3987697"])

    def test_similar_slug_with_a_different_cycle_stays_separate(self):
        rows = [row("att-a", 1, "owner", owner_route_id="r1", slug="framed-x-owner"),
                row("att-b", 2, "owner", owner_route_id="r2", slug="framed-x-owner"),
                row("att-fa", 3, "frame", route_id="f1", slug="framed-x-frame"),
                row("att-fb", 4, "frame", route_id="f2", slug="framed-x-frame")]
        routes = {"r1": {"route_id": "r1", "parent_cycle_id": "c1"}, "r2": {"route_id": "r2", "parent_cycle_id": "c2"}}
        cycles = {"c1": {"cycle_id": "c1", "route_id": "f1", "title": "one"},
                  "c2": {"cycle_id": "c2", "route_id": "f2", "title": "two"}}
        tabs = build_tabs(rows, routes, cycles)
        self.assertEqual(sorted((t.label, t.attempt_ids) for t in tabs),
                         [("one", ("att-a", "att-fa")), ("two", ("att-b", "att-fb"))])

    def test_same_cycle_joins_even_when_the_slugs_differ(self):
        rows = [row("att-a", 1, "owner", owner_route_id="r1", slug="alpha-owner"),
                row("att-b", 2, "owner", owner_route_id="r2", slug="beta-owner"),
                row("att-f", 3, "frame", route_id="f1", slug="gamma-frame")]
        routes = {"r1": {"route_id": "r1", "parent_cycle_id": "c1"}, "r2": {"route_id": "r2", "parent_cycle_id": "c1"}}
        cycles = {"c1": {"cycle_id": "c1", "route_id": "f1", "title": "joined"}}
        tab_, = build_tabs(rows, routes, cycles)
        self.assertEqual(tab_.attempt_ids, ("att-a", "att-b", "att-f"))
        self.assertEqual(tab_.owner_route_ids, ("r1", "r2"))

    def test_a_cycle_without_a_frame_route_proves_nothing(self):
        rows = [row("att-a", 1, "owner", owner_route_id="r1"), row("att-f", 2, "frame", route_id="f1")]
        routes = {"r1": {"route_id": "r1", "parent_cycle_id": "c1"}}
        for cycle in ({"cycle_id": "c1"},                                           # no frame route recorded
                      {"cycle_id": "c1", "route_id": "f1", "route_capability": "autopilot-code"}):
            tabs = build_tabs(rows, routes, {cycle["cycle_id"]: cycle})
            self.assertEqual([t.verified for t in tabs], [False, False])
            self.assertEqual({t.label for t in tabs}, {"연결 미확인"})
        # a record under another id than the owner route's pointer proves nothing for the owner
        tabs = build_tabs(rows, routes, {"other": {"cycle_id": "other", "route_id": "f1", "title": "solo"}})
        self.assertEqual(sorted((t.label, t.attempt_ids) for t in tabs),
                         [("solo", ("att-f",)), ("연결 미확인", ("att-a",))])

    def test_open_state_follows_the_latest_owner_row(self):
        rows = [row("att-a", 1, "owner", owner_route_id="r1", status="killed"),
                row("att-b", 2, "owner", owner_route_id="r1", status="open")]
        routes = {"r1": {"route_id": "r1", "parent_cycle_id": "c1"}}
        cycles = {"c1": {"cycle_id": "c1", "route_id": "f1", "title": "t"}}
        self.assertFalse(build_tabs(rows, routes, cycles)[0].complete)
        rows[1] = row("att-b", 2, "owner", owner_route_id="r1", status="done")
        self.assertTrue(build_tabs(rows, routes, cycles)[0].complete)


class SmallHelpers(unittest.TestCase):
    def test_short_label_rule(self):
        self.assertEqual([short_label(n) for n in ("b6-report", "b-p3", "herdr-tmux", "owner-fleet", "fleet-explorer", "steward")],
                         ["b6", "b-p3", "herdr", "owner", "fleet", "steward"])

    def test_vanished_selection_moves_to_a_stable_neighbour_or_overview(self):
        tabs = build_tabs([row("att-a", 1, "frame", route_id="f1"), row("att-b", 2, "frame", route_id="f2")], {}, {})
        ids = [t.identity for t in tabs]
        self.assertEqual(select_fallback(tabs, ids[1]), ids[1])
        self.assertEqual(select_fallback(tabs, "gone", previous=["x", "gone", ids[0]]), ids[0])
        self.assertEqual(select_fallback(tabs, "gone"), "")
        self.assertEqual(select_fallback(tabs, ""), "")


if __name__ == "__main__":
    unittest.main()
