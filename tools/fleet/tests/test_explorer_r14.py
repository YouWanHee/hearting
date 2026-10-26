"""Focused renderer regressions for the r14 frame and dynamic data layouts."""
import unittest
from pathlib import Path

from tools.fleet.explorer.frame import draw, measure_pane_max
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import ExplorerViewModel, load_fixture


FIXTURE = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"


class R14Renderer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vm = load_fixture(FIXTURE)

    def test_navigation_has_six_views_and_one_compute_destination(self):
        frame = draw(self.vm, NavState(), cols=100, rows=45)
        tabs = [hit.action[1] for hit in frame.hits
                if hit.action and hit.action[0] == "tab"]
        self.assertEqual(tabs.count("COMPUTE"), 1)  # the persistent band opens the same pane via root
        self.assertTrue(any(hit.action == ("root", "COMPUTE") for hit in frame.hits))
        rows = frame.text_rows()
        views_y = next(i for i, row in enumerate(rows) if row.lstrip().startswith("OVERVIEW"))
        sessions_y = next(i for i, row in enumerate(rows) if "SESSIONS" in row)
        self.assertGreater(sessions_y - views_y, 6)
        self.assertIn("┌", rows[3])
        self.assertEqual(rows[2].strip(), "")
        self.assertIn("┴", rows[-4])

    def test_peers_and_all_renderer_views_stay_inside_four_viewports(self):
        views = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE",
                 "MEMORY", "MODELS", "SYSTEM")
        for cols in (80, 100, 140, 168):
            for view in views:
                for usage_open in (False, True):
                    with self.subTest(cols=cols, view=view, usage_open=usage_open):
                        frame = draw(self.vm, NavState(view=view, usage_open=usage_open),
                                     cols=cols, rows=45)
                        self.assertEqual(len(frame.cells), 45)
                        self.assertTrue(all(len(row) == cols for row in frame.cells))
                        self.assertTrue(all((hit.y == -1 and hit.action[0] == "parent"
                                             and hit.x0 == hit.x1 == 0) or
                                            (0 <= hit.y < 45 and 0 <= hit.x0 < hit.x1 <= cols)
                                            for hit in frame.hits))

    def test_peer_selection_has_session_hits_and_filters_links(self):
        state = NavState(view="PEERS", opened="pfocus:PEERS=a3")
        frame = draw(self.vm, state, cols=140, rows=45)
        selected = [hit for hit in frame.hits if hit.action == ("fold", "pfocus:PEERS", "a3")]
        self.assertTrue(selected)

    def test_main_only_models_are_shown_from_vm_values(self):
        from tools.fleet.explorer.view_model import ExplorerViewModel
        data = self.vm.to_mapping()
        data["models"] = {"profiles": ("balanced",), "profile_map": {},
                           "main_only": {"z-harness": "private-model-a",
                                         "a-harness": "private-model-b"},
                           "shown": {"private-model-a": "Visible A",
                                     "private-model-b": "Visible B"}}
        frame = draw(ExplorerViewModel.from_mapping(data), NavState(view="MODELS"),
                     cols=140, rows=45)
        rows = frame.text_rows()
        self.assertTrue(any("main session only" in row for row in rows))
        self.assertTrue(any("Visible A" in row for row in rows))
        self.assertTrue(any("Visible B" in row for row in rows))

    def test_compute_process_rows_use_only_vm_process_facts(self):
        from tools.fleet.explorer.view_model import ExplorerViewModel
        data = self.vm.to_mapping()
        data["system"] = {**data["system"],
                           "gpu_processes": ({"host": self.vm.hosts[0]["name"], "gpu": 0,
                                              "session_id": "full-session-id",
                                              "cwd": "/work/project-x",
                                              "command": "python3 -u /opt/run_eval.py --secret arg",
                                              "vram": "12 GB", "age": "4m"},),
                           "gpu_process_summary": {"count": 1, "unregistered": 0},
                           "unregistered_gpu_processes": (("host", 0,
                                                          "python3 -u /opt/run_eval.py --secret arg",
                                                          1, "1m"),)}
        frame = draw(ExplorerViewModel.from_mapping(data), NavState(view="COMPUTE"),
                     cols=140, rows=45)
        rows = frame.text_rows()
        process_row = next(row for row in rows if "full-session-id" in row)
        self.assertIn("project-x/", process_row)
        self.assertIn("run_eval.py", process_row)
        self.assertEqual("\n".join(rows).count("run_eval.py"), 1)

    def test_compute_block_anchors_above_footer_and_tree_scroll_keeps_full_identity_map(self):
        for cols in (80, 100, 140, 168):
            for rows in (24, 40, 60):
                for usage_open in (False, True):
                    with self.subTest(cols=cols, rows=rows, usage_open=usage_open):
                        frame = draw(self.vm, NavState(usage_open=usage_open), cols=cols, rows=rows)
                        self.assertIn("PgUp/Dn", frame.text_rows()[-2])
                        self.assertIn("Home/End", frame.text_rows()[-2])
                        self.assertIn("wheel", frame.text_rows()[-2])
                        footer = rows - 4
                        compute_hit = next(hit for hit in frame.hits
                                           if hit.action == ("root", "COMPUTE"))
                        compute_rows = 2 + min(5, len(self.vm.compute))
                        self.assertEqual(compute_hit.y, footer - compute_rows)
                        compute_hits = [hit for hit in frame.hits
                                        if hit.action == ("root", "COMPUTE")]
                        self.assertEqual(max(hit.y for hit in compute_hits), footer - 1)
                        self.assertTrue(all(hit.y < footer for hit in compute_hits))
                        self.assertTrue(any(hit.action == ("tab", "COMPUTE") for hit in frame.hits))
                        tree = frame.tree_scroll
                        self.assertEqual(tree[3], compute_hit.y)
                        tree_hits = [hit for hit in frame.hits
                                     if hit.action[:1] in (("scope",), ("route",))]
                        self.assertTrue(all(tree[1] <= hit.y < tree[3] for hit in tree_hits))
                        self.assertTrue(all(0 <= hit.y < rows and 0 <= hit.x0 < hit.x1 <= cols
                                            for hit in frame.hits if hit.y >= 0))

                        if rows >= 40 and tree[5] > 0:
                            state, _ = reduce(
                                NavState(), {"type": "wheel", "direction": -1,
                                             "x": 2, "y": tree[1]}, frame)
                            self.assertEqual(dict(state.scroll)["tree"], 3)
                            bottom = draw(self.vm, NavState(scroll=(("tree", tree[5]),)),
                                          cols=cols, rows=rows)
                            bottom_tree = bottom.tree_scroll
                            bottom_hits = [hit for hit in bottom.hits
                                           if hit.action[:1] in (("scope",), ("route",))]
                            self.assertTrue(bottom_hits)
                            self.assertEqual(max(hit.y for hit in bottom_hits), bottom_tree[3] - 1)

    def test_page_keys_scroll_unclipped_body_rows_and_clamp_resize(self):
        frame = draw(self.vm, NavState(), cols=120, rows=24)
        self.assertTrue(frame.pane_scroll[5] > 0)
        moved, _ = reduce(NavState(), {"type": "key", "key": "pagedown"}, frame)
        self.assertGreater(dict(moved.scroll)["pane"], 0)
        scrolled = draw(self.vm, moved, cols=120, rows=24)
        self.assertGreater(scrolled.pane_scroll[4], 0)
        self.assertTrue(all(hit.y < 20 for hit in scrolled.hits if hit.y >= 0))
        top, _ = reduce(moved, {"type": "key", "key": "home"}, scrolled)
        self.assertEqual(dict(top.scroll)["pane"], 0)
        end_offset = frame.pane_scroll[5]
        bottom, _ = reduce(NavState(), {"type": "key", "key": "end",
                                        "max_scroll": end_offset}, frame)
        self.assertEqual(dict(bottom.scroll)["pane"], end_offset)
        resized = draw(self.vm, bottom, cols=168, rows=60)
        self.assertLessEqual(resized.pane_scroll[4], resized.pane_scroll[5])

    def test_huge_stale_offset_clamps_without_offset_sized_canvas(self):
        from unittest.mock import patch
        import tools.fleet.explorer.frame as frame

        real_grid = frame.CellGrid
        allocated = []

        class ScreenGridGuard(real_grid):
            def __init__(self, cols, rows, *args, **kwargs):
                allocated.append((cols, rows))
                if rows > 24:
                    raise AssertionError(f"dense canvas allocation scales with stale offset: {rows}")
                super().__init__(cols, rows, *args, **kwargs)

        stale = NavState(view="QUESTIONS", scroll=(("pane", 1 << 20),))
        with patch.object(frame, "CellGrid", ScreenGridGuard), patch.object(frame, "_draw_page"):
            rendered = frame.draw(self.vm, stale, cols=120, rows=24)

        self.assertEqual(allocated, [(120, 24)])
        self.assertEqual(rendered.pane_scroll[4], 0)
        self.assertEqual(rendered.pane_scroll[5], 0)
        self.assertFalse(rendered.pane_scroll[7])

    def test_end_measures_finite_questions_past_8192_rows(self):
        questions = tuple({"tag": f"q{index:04d}", "h": "claude", "kind": "wait",
                           "wait": "1m", "project": "p", "title": "q", "now": "n",
                           "pane": "p"} for index in range(1400))
        vm = ExplorerViewModel.from_mapping({"questions": questions})
        state = NavState(view="QUESTIONS")

        maximum = measure_pane_max(vm, state, cols=60, rows=24)
        self.assertGreater(maximum, 8192)

        end = draw(vm, NavState(view="QUESTIONS", scroll=(("pane", maximum),)),
                   cols=60, rows=24)
        self.assertEqual(end.pane_scroll[4], maximum)
        self.assertEqual(end.pane_scroll[5], maximum)
        self.assertFalse(end.pane_scroll[7])
        self.assertIn("q1399", "\n".join(end.text_rows()))
        self.assertTrue(all(0 <= hit.y < 20 for hit in end.hits if hit.y >= 0))

    def test_keyboard_session_selection_reveals_its_identity_in_scrolled_tree(self):
        frame = draw(self.vm, NavState(scroll=(("tree", 6),)), cols=120, rows=40)
        order = tuple("session:" + str(item.get("tag")) for item in self.vm.sessions)
        state, _ = reduce(NavState(scroll=(("tree", 6),)),
                          {"type": "key", "key": "down", "order": order}, frame)
        tree = frame.tree_scroll
        indices = tree[8]
        selected_index = indices[state.scope]
        offset = dict(state.scroll).get("tree", tree[4])
        self.assertLessEqual(offset, selected_index)
        self.assertLess(selected_index, offset + tree[7])
        repainted = draw(self.vm, state, cols=120, rows=40)
        self.assertTrue(any(hit.action == ("scope", state.scope.partition(":")[2])
                            for hit in repainted.hits))


if __name__ == "__main__":
    unittest.main()
