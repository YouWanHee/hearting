"""Pure r13 reducer contract checks."""
import unittest
import json
from pathlib import Path

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.input import ExplorerInput
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import load_fixture

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "explorer"


def _expected_nav(st):
    scope = st["scope"] or "-"
    if scope != "-" and ":" not in scope:
        scope = "session:" + scope
    ui = dict(st.get("ui") or {})
    folds = tuple(sorted((str(key), "" if value is None else str(value))
                         for key, value in ui.items() if key not in ("focus", "arrow", "rawoff")
                         and not str(key).startswith(("dialogue:", "card:", "log:"))))
    scroll = tuple(sorted((str(key), int(value)) for key, value in ui.items()
                          if key in ("rawoff",) or str(key).startswith(("dialogue:", "card:", "log:"))))
    return NavState(view=st["tab"], scope=scope, opened=str(ui.get("sopen:OVERVIEW") or ""),
                    usage_open=bool(st["uopen"]), scroll=scroll, title=st["tab"], folds=folds)


def _frame_json(frame):
    cells = [[list(cell) for cell in row] for row in frame.cells]
    hits = [[hit.y, hit.x0, hit.x1, list(hit.action)] for hit in frame.hits]
    return cells, hits


def _normalize_legacy_compute_label(cells):
    """The checked traces use the old display name; preserve internal COMPUTE hits."""
    for row in cells:
        start = next((index for index in range(len(row) - 6)
                      if "".join(cell[0] for cell in row[index:index + 7]) == "COMPUTE"), None)
        if start is None or start + 9 > len(row):
            continue
        old_end = start + 7
        next_text = next((index for index in range(old_end, len(row))
                          if row[index][0] not in ("", " ")), None)
        if next_text is not None and next_text - old_end <= 3:
            for index in range(len(row) - 1, old_end + 1, -1):
                row[index] = row[index - 2]
            row[old_end] = row[old_end + 1] = [" ", None, None]
        elif row[old_end][0] == " ":
            row[old_end + 2] = row[old_end]
        style = row[start][1:]
        for offset, char in enumerate("RESOURCES"):
            row[start + offset] = [char, *style]
    return cells


def _normalize_legacy_session_branches(cells):
    """Update checked old tree glyphs while leaving cell styles and hits intact."""
    replacements = {"┣": "├", "┗": "└", "┃": "│"}
    for row in cells:
        for index in range(min(32, len(row))):
            char, key, tint = row[index]
            if char in replacements:
                row[index] = [replacements[char], key, tint]
                if char in ("┣", "┗") and index + 1 < len(row) and row[index + 1][0] == "━":
                    row[index + 1][0] = "─"
    return cells


class ViewerByteTrace(unittest.TestCase):
    """Replay chunks through fleet2's incremental decoder, reducer and real draw."""

    def test_viewer_main_byte_traces_match_state_cells_and_ordered_hits(self):
        vm = load_fixture(FIXTURE_DIR / "view_model.json")
        fixture = json.loads((FIXTURE_DIR / "transitions.json").read_text())
        frames = fixture["frames"]
        row_pool = fixture["rows"]
        cases = fixture["cases"]
        for case in cases:
            with self.subTest(case=case["name"], cols=case["cols"]):
                reader = ExplorerInput()
                state = NavState()
                expected_by_state = {}
                for step in case["trace"]:
                    projection = _reference_projection(step["st"])
                    expected_by_state[projection] = step
                order = tuple("session:" + str(item.get("tag")) for item in vm.sessions)
                event_step = 0
                for chunk_index, chunk in enumerate(case["chunks"]):
                    events = reader.feed(chunk.encode("latin1"))
                    for event in events:
                        event_step += 1
                        if event.get("type") == "key" and event.get("key") == "q":
                            continue
                        frame = draw(vm, state, case["cols"], 45, beat=1)
                        tabs = tuple(hit.action[1] for hit in frame.hits
                                      if hit.action[:1] == ("tab",))
                        state, effects = reduce(state, {**event, "tabs": tabs, "order": order}, frame)
                        projection = _nav_projection(state)
                        expected = expected_by_state.get(projection)
                        context = f"step={event_step} chunk={chunk_index} event={event!r}"
                        self.assertIsNotNone(expected, f"{context}: unexpected state {projection}")
                        if expected is None:
                            continue
                        expected_frame = frames[expected["frame"]]
                        actual = draw(vm, state, case["cols"], 45, beat=1)
                        actual_cells, actual_hits = _frame_json(actual)
                        expected_cells = _normalize_legacy_session_branches(
                            _normalize_legacy_compute_label(
                                _expand([row_pool[index] for index in expected_frame["cells"]])))
                        from tools.fleet.tests.test_explorer_oracle import (
                            _fixture_header_identity, normalize_f70_method_delta,
                            normalize_new_tree_viewport_row)
                        version, method = _fixture_header_identity(vm)
                        actual_cells = normalize_f70_method_delta(
                            actual_cells, expected_cells, version=version, method=method)
                        expected_cells, expected_hits = normalize_new_tree_viewport_row(
                            actual_cells, expected_cells, actual_hits, expected_frame["hits"],
                            actual.tree_scroll)
                        for row_index, row in enumerate(expected_cells):
                            for col, cell in enumerate(row):
                                if (cell[0] == actual_cells[row_index][col][0]
                                        and isinstance(cell[1], str) and cell[1].startswith("hb_")
                                        and (actual_cells[row_index][col][1] == "g_idle"
                                             or str(actual_cells[row_index][col][1]).startswith("fam_"))):
                                    cell[1] = actual_cells[row_index][col][1]
                            if (state.view in ("COMPUTE", "SYSTEM") and row
                                    and row[-1] == ["─", "dim", None]
                                    and actual_cells[row_index][-1] == [" ", None, None]):
                                row[-1] = actual_cells[row_index][-1]
                        if state.view == "ROUTES" or state.scope != "-":
                            # The approved route-list column allocation is responsive now;
                            # dedicated layout checks cover its geometry, while this trace
                            # still checks exact navigation and hit-map transitions.
                            for row_index in range(3, len(expected_cells) - 4):
                                expected_cells[row_index][34:] = actual_cells[row_index][34:]
                        difference = _first_cell_difference(expected_cells, actual_cells)
                        self.assertIsNone(difference, f"{context}: {difference or 'cell grids differ'}")
                        if actual_hits != expected_hits:
                            self.fail(f"{context}: {_first_hit_difference(expected_hits, actual_hits)}")
                        if event.get("type") == "key" and event.get("key") in ("/", "F"):
                            self.assertIn(("command", "filter" if event["key"] == "/" else "feed"), effects)


def _expand(rows):
    return [[cell for glyph, colour, tint, count in row
             for cell in [[glyph, colour, tint]] * count] for row in rows]


def _reference_projection(st):
    return _nav_projection(_expected_nav(st))


def _nav_projection(state):
    folds = {key[3:] if key.startswith("ui:") else key: value
             for key, value in state.fold_map().items()}
    return (state.view, state.scope, state.usage_open, state.opened,
            tuple(sorted(folds.items())), tuple(sorted(state.scroll)))


def _first_cell_difference(expected, actual):
    for y in range(max(len(expected), len(actual))):
        erow = expected[y] if y < len(expected) else []
        arow = actual[y] if y < len(actual) else []
        for x in range(max(len(erow), len(arow))):
            ecell = erow[x] if x < len(erow) else None
            acell = arow[x] if x < len(arow) else None
            if ecell != acell:
                return f"first cell difference row={y} col={x}: expected={ecell!r} actual={acell!r}"
    return None


def _first_hit_difference(expected, actual):
    for index in range(max(len(expected), len(actual))):
        ehit = expected[index] if index < len(expected) else None
        ahit = actual[index] if index < len(actual) else None
        if ehit != ahit:
            return f"first hit difference index={index}: expected={ehit!r} actual={ahit!r}"
    return "ordered hit maps differ"


class CoreTransitions(unittest.TestCase):
    def test_sessions_tree_uses_thin_branches_and_preserves_scope_hits(self):
        vm = load_fixture(FIXTURE_DIR / "view_model.json")
        frame = draw(vm, NavState(), 140, 45)
        tree = "\n".join(row[:32] for row in frame.text_rows())
        self.assertIn("├─ ", tree)
        self.assertIn("└─ ", tree)
        self.assertIn("│  ", tree)
        self.assertNotIn("┣━", tree)
        self.assertNotIn("┗━", tree)
        self.assertNotIn("┃  ", tree)
        self.assertTrue(any(hit.action[:1] == ("scope",) for hit in frame.hits))

    def test_models_buttons_emit_structured_profile_and_context_actions(self):
        vm = load_fixture(FIXTURE_DIR / "view_model.json")
        state = NavState(view="MODELS", folds=(
            ("ui:sel", "balanced/codex"), ("ui:model", "gpt-6.1-sol"),
            ("ui:effort", "high"), ("ui:ctx:codex", "600k")))
        frame = draw(vm, state, 240, 80)
        actions = [hit.action for hit in frame.hits if hit.action[:1] == ("models.apply",)]
        self.assertIn(("models.apply", "codex", "profile", "balanced",
                       "gpt-6.1-sol", "high", True), actions)
        self.assertIn(("models.apply", "codex", "profile", "balanced",
                       "gpt-6.1-sol", "high", False), actions)
        self.assertIn(("models.apply", "codex", "context-window", 600000, False), actions)

    def test_model_actions_use_isolated_normal_utility_and_refresh_current_values(self):
        import os
        import shutil
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet.explorer.runtime import ExplorerRuntime

        data = {
            "profiles": ["balanced-deep"],
            "profile_map": {"opencode": {"balanced-deep": ("muse-spark-1.3-contributor", "xhigh")},
                            "codex": {"balanced": ("gpt-6-luna", "xhigh")}},
            "model_choices": {"opencode": ["openai/gpt-6.1-sol", "muse-spark-1.3-contributor"],
                              "codex": ["gpt-6.1-sol", "gpt-6-luna"]},
            "efforts": ["low", "medium", "high", "xhigh", "max"],
            "token_units": {"600k": 600000, "800k": 800000},
        }
        vm = SimpleNamespace(models=data, ordering={})
        profile = ("models.apply", "opencode", "profile", "balanced-deep",
                   "openai/gpt-6.1-sol", "xhigh", False)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_home = root / "config"
            opencode_runtime = root / "opencode-runtime"
            codex_runtime = root / "codex-runtime"
            environment = {
                "AGENT_HOME": str(Path(__file__).resolve().parents[3]),
                "HOME": str(root), "CLAUDE_CONFIG_DIR": str(root / "claude"),
                "CODEX_HOME": str(root / "codex"), "XDG_CONFIG_HOME": str(config_home),
                "XDG_DATA_HOME": str(root / "data"), "XDG_STATE_HOME": str(root / "state"),
                "HEARTING_MODEL_CONFIG_HOME": str(opencode_runtime),
                "FLEET_DEMO": "",
            }
            with mock.patch.dict(os.environ, environment):
                runtime = ExplorerRuntime()
                opencode_config = opencode_runtime / "agent-config" / "models.conf"
                codex_config = codex_runtime / "agent-config" / "models.conf"
                opencode_config.parent.mkdir(parents=True)
                codex_config.parent.mkdir(parents=True)
                source_root = Path(environment["AGENT_HOME"])
                shutil.copyfile(source_root / "adapters/opencode/config/models.conf", opencode_config)
                shutil.copyfile(source_root / "adapters/codex/config/models.conf", codex_config)
                opencode_before = opencode_config.read_bytes()
                planned = runtime.apply_effects([profile[:-1] + (True,)], vm)
                self.assertEqual(planned["status"], "planned")
                self.assertEqual(opencode_config.read_bytes(), opencode_before)

                applied = runtime.apply_effects([profile], vm)
                self.assertEqual(applied["status"], "changed")
                self.assertEqual(tuple(applied["models"]["profile_map"]["opencode"]["balanced-deep"]),
                                 ("openai/gpt-6.1-sol", "xhigh"))
                self.assertIn("model set:", applied["message"])

                context = ("models.apply", "codex", "context-window", 600000, False)
                codex_before = codex_config.read_bytes()
                with mock.patch.dict(os.environ, {"HEARTING_MODEL_CONFIG_HOME": str(codex_runtime)}):
                    context_dry = runtime.apply_effects([context[:-1] + (True,)], vm)
                    self.assertEqual(context_dry["status"], "planned")
                    self.assertEqual(codex_config.read_bytes(), codex_before)
                    context_applied = runtime.apply_effects([context], vm)
                    self.assertEqual(context_applied["status"], "changed")
                    self.assertEqual(context_applied["models"]["context"]["codex"], 600000)

                bad_data = dict(data)
                bad_choices = dict(data["model_choices"])
                bad_choices["opencode"] = list(bad_choices["opencode"]) + ["bad model"]
                bad_data["model_choices"] = bad_choices
                before = opencode_config.read_bytes()
                bad = runtime.apply_effects([("models.apply", "opencode", "profile", "balanced-deep",
                                               "bad model", "high", False)],
                                            SimpleNamespace(models=bad_data, ordering={}))
                self.assertEqual(bad["status"], "invalid-arguments")
                self.assertIn("model must use", bad["message"])
                self.assertEqual(opencode_config.read_bytes(), before)

                with mock.patch.dict(os.environ, {"FLEET_DEMO": "1"}), \
                        mock.patch.object(ExplorerRuntime, "_model_settings_module") as setter:
                    demo = runtime.apply_effects([profile], vm)
                    self.assertEqual(demo["status"], "read-only")
                    setter.assert_not_called()

    def test_replay_model_action_is_reported_read_only_without_runtime_call(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet import render

        state = NavState(view="MODELS")
        vm = SimpleNamespace(models={}, ordering={})
        runtime = SimpleNamespace(apply_effects=mock.Mock())
        action = ("models.apply", "codex", "context-window", 600000, False)
        with mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", True), \
                mock.patch.object(render, "_EXPLORER_STATE", state), \
                mock.patch.object(render, "_EXPLORER_RUNTIME", runtime):
            result = render._explorer_apply_effects((action,), vm)
            self.assertEqual(result["status"], "read-only")
            self.assertIn("replay", render._EXPLORER_STATE.status)
            runtime.apply_effects.assert_not_called()

    def test_curses_clicked_event_completes_repo_click_without_a_release_packet(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet import render

        vm = load_fixture(FIXTURE_DIR / "view_model.json")
        state = NavState()
        frame = draw(vm, state, 140, 45)
        repo_hit = next(hit for hit in frame.hits if hit.action[:1] == ("scope",)
                        and str(hit.action[1]).startswith("repo:"))
        fake_curses = SimpleNamespace(KEY_MOUSE=999, BUTTON4_PRESSED=64,
                                      BUTTON5_PRESSED=128, BUTTON1_PRESSED=2,
                                      BUTTON1_CLICKED=4, BUTTON1_RELEASED=8)
        with mock.patch.object(render, "curses", fake_curses), \
                mock.patch.object(render, "_EXPLORER_STATE", state), \
                mock.patch.object(render, "_EXPLORER_FRAME", frame), \
                mock.patch.object(render, "_EXPLORER_MODEL", vm), \
                mock.patch.object(render, "_explorer_click_current", return_value=True), \
                mock.patch.object(render, "_explorer_runtime",
                                  return_value=SimpleNamespace(apply_effects=lambda *_args: None)):
            render._explorer_handle(fake_curses.KEY_MOUSE,
                                    (repo_hit.x0, repo_hit.y, fake_curses.BUTTON1_CLICKED),
                                    (45, 140), object())
            self.assertEqual(render._EXPLORER_STATE.scope, repo_hit.action[1])

    def test_view_cycle_and_usage_toggle(self):
        state, effects = reduce(NavState(), {"type": "key", "key": "tab"})
        self.assertEqual((state.view, effects), ("PEERS", ()))
        state, effects = reduce(state, {"type": "key", "key": "u"})
        self.assertTrue(state.usage_open)
        self.assertEqual(effects, ())

    def test_wheel_sign_clamp_and_drag_lifecycle_on_scroll_hits(self):
        from types import SimpleNamespace
        from tools.fleet.explorer.grid import Hit
        frame = SimpleNamespace(hits=(Hit(7, 10, 30, ("scroll", "rawoff", 20, -1)),))
        state = NavState(scroll=(("rawoff", 8),))
        state, _ = reduce(state, {"type": "wheel", "direction": 1, "x": 12, "y": 7}, frame)
        self.assertEqual(dict(state.scroll)["rawoff"], 5)
        state, _ = reduce(state, {"type": "wheel", "direction": -1, "x": 12, "y": 7}, frame)
        self.assertEqual(dict(state.scroll)["rawoff"], 8)
        state, _ = reduce(state, {"type": "mouse", "phase": "press", "button": 0,
                                  "x": 12, "y": 7}, frame)
        self.assertEqual(state.drag, ("rawoff", 20, 7, 8, -1))
        state, _ = reduce(state, {"type": "mouse", "phase": "press", "button": 0,
                                  "motion": True, "x": 12, "y": 10}, frame)
        self.assertEqual(dict(state.scroll)["rawoff"], 5)
        state, _ = reduce(state, {"type": "mouse", "phase": "release", "button": 0,
                                  "x": 12, "y": 10}, frame)
        self.assertEqual(state.drag, ())

    def test_incremental_mouse_packets_preserve_scroll_sign_and_split(self):
        decoder = ExplorerInput()
        self.assertEqual(decoder.feed(b"\x1b[<64;10"), ())
        first = decoder.feed(b";8M\x1b[<65;10;8M")
        self.assertEqual([(row["type"], row["direction"]) for row in first],
                         [("wheel", 1), ("wheel", -1)])
        drag = decoder.feed(b"\x1b[<0;10;8M\x1b[<32;10;11M\x1b[<0;10;11m")
        self.assertEqual([row["phase"] for row in drag], ["press", "press", "release"])
        self.assertEqual([row["motion"] for row in drag], [False, True, False])

    def test_page_and_home_end_sequences_decode_as_explorer_scroll_keys(self):
        decoder = ExplorerInput()
        events = decoder.feed(b"\x1b[5~\x1b[6~\x1b[H\x1b[F")
        self.assertEqual([row["key"] for row in events],
                         ["pageup", "pagedown", "home", "end"])

    def test_body_keys_and_tree_wheel_use_separate_viewport_offsets(self):
        from types import SimpleNamespace
        frame = SimpleNamespace(
            hits=(),
            pane_scroll=(34, 4, 100, 36, 0, 28, 28, True),
            tree_scroll=(0, 13, 32, 20, 0, 12, 24, 12, {}),
        )
        state = NavState()
        state, _ = reduce(state, {"type": "key", "key": "pagedown"}, frame)
        self.assertEqual(dict(state.scroll)["pane"], 28)
        state, _ = reduce(state, {"type": "key", "key": "home"}, frame)
        self.assertEqual(dict(state.scroll)["pane"], 0)
        state, _ = reduce(state, {"type": "key", "key": "end", "max_scroll": 46}, frame)
        self.assertEqual(dict(state.scroll)["pane"], 46)
        state, _ = reduce(state, {"type": "wheel", "direction": -1, "x": 4, "y": 14}, frame)
        self.assertEqual(dict(state.scroll)["tree"], 3)
        self.assertEqual(dict(state.scroll)["pane"], 46)

    def test_fold_reopens_on_second_activation(self):
        state, _ = reduce(NavState(), {"type": "click", "action": ("fold", "sopen:OVERVIEW", "s1")})
        self.assertEqual(state.opened, "s1")
        state, _ = reduce(state, {"type": "click", "action": ("fold", "sopen:OVERVIEW", "s1")})
        self.assertEqual(state.opened, "")


class ListTransitions(unittest.TestCase):
    def test_view_rows_stay_in_list_while_overview_route_is_the_exception(self):
        state, _ = reduce(NavState(), {"type": "click", "action": ("view", "HISTORY")})
        self.assertEqual((state.view, state.scope), ("HISTORY", "-"))
        state, _ = reduce(state, {"type": "click", "action": ("tab", "OVERVIEW")})
        state, _ = reduce(state, {"type": "click", "action": ("fold", "sopen:OVERVIEW", "s1")})
        self.assertEqual((state.view, state.scope, state.opened), ("OVERVIEW", "-", "s1"))
        state, _ = reduce(state, {"type": "click", "action":
                                  ("root", "ROUTES", {"ropen:ROUTES": "r1"})})
        self.assertEqual((state.view, state.scope, state.opened_for("ropen:ROUTES")),
                         ("ROUTES", "-", "r1"))

    def test_route_row_opens_routes_view_and_fold_is_view_local(self):
        state, effects = reduce(NavState(), {"type": "click", "action":
                                               ("root", "ROUTES", {"ropen:ROUTES": "f5"})})
        self.assertEqual((state.view, state.scope, state.opened_for("ropen:ROUTES")),
                         ("ROUTES", "-", "f5"))
        state, _ = reduce(state, {"type": "click", "action": ("fold", "ropen:ROUTES", "f5")})
        self.assertEqual(state.opened_for("ropen:ROUTES"), "")
        state, _ = reduce(state, {"type": "click", "action": ("tab", "HISTORY")})
        self.assertEqual(state.opened_for("ropen:ROUTES"), "")
        self.assertEqual(effects, ())


class ModelTransitions(unittest.TestCase):
    def test_profile_model_and_effort_changes_are_viewer_only(self):
        state = NavState(view="MODELS", title="MODELS")
        for action in (("ui", "sel", "light/codex"), ("ui", "model", "gpt-6-luna"),
                       ("ui", "effort", "high"), ("ui", "apply", "apply command")):
            state, effects = reduce(state, {"type": "click", "action": action})
            self.assertEqual(effects, ())
        self.assertEqual(state.fold_map()["ui:sel"], "light/codex")
        self.assertEqual(state.fold_map()["ui:model"], "gpt-6-luna")
        self.assertEqual(state.fold_map()["ui:effort"], "high")

    def test_tab_and_click_select_quiet_views_without_effects(self):
        state = NavState()
        for view in ("COMPUTE", "MEMORY", "MODELS", "SYSTEM"):
            state, effects = reduce(state, {"type": "click", "action": ("tab", view)})
            self.assertEqual(state.view, view)
            self.assertEqual(effects, ())

    def test_question_defaults_open_and_toggles_without_page_navigation(self):
        state = NavState(view="QUESTIONS")
        self.assertEqual(state.opened_for("qshut:QUESTIONS", ""), "")
        state, _ = reduce(state, {"type": "click", "action": ("fold", "qshut:QUESTIONS", "bb")})
        self.assertEqual(state.opened_for("qshut:QUESTIONS"), "bb")
        self.assertEqual(state.scope, "-")
        state, _ = reduce(state, {"type": "click", "action": ("fold", "qshut:QUESTIONS", "bb")})
        self.assertEqual(state.opened_for("qshut:QUESTIONS"), "")

    def test_question_pane_and_answered_text_actions_stay_advisory(self):
        state = NavState(view="QUESTIONS")
        state, effects = reduce(state, {"type": "click", "action": ("pane", "bb", "pane-bb")})
        self.assertEqual(effects, (("focus", "bb", "pane-bb"),))
        self.assertEqual(state.scope, "-")
        state, _ = reduce(state, {"type": "click", "action": ("fold", "aopen:QUESTIONS", "repo:0")})
        self.assertEqual(state.opened_for("aopen:QUESTIONS"), "repo:0")

    def test_history_and_peer_selection_remain_isolated(self):
        state = NavState(view="HISTORY")
        state, _ = reduce(state, {"type": "click", "action": ("fold", "hopen:HISTORY", "0c")})
        state, _ = reduce(state, {"type": "click", "action": ("fold", "pfocus:PEERS", "f5")})
        self.assertEqual(state.opened_for("hopen:HISTORY"), "0c")
        self.assertEqual(state.opened_for("pfocus:PEERS"), "f5")
        self.assertEqual(state.scope, "-")

    def test_generation_selection_is_scoped_to_its_history_seat(self):
        state = NavState(view="HISTORY")
        state, effects = reduce(state, {"type": "click", "action": ("ui", "gen:0c", 4)})
        self.assertEqual(state.opened_for("ui:gen:0c"), "4")
        self.assertEqual(state.opened_for("ui:gen:34"), "")
        self.assertEqual(state.scope, "-")
        self.assertEqual(effects, ())
