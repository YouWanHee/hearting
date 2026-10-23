"""Pure r13 reducer contract checks."""
import unittest

from tools.fleet.explorer.state import NavState, reduce


class CoreTransitions(unittest.TestCase):
    def test_view_cycle_and_usage_toggle(self):
        state, effects = reduce(NavState(), {"type": "key", "key": "tab"})
        self.assertEqual((state.view, effects), ("PEERS", ()))
        state, effects = reduce(state, {"type": "key", "key": "u"})
        self.assertTrue(state.usage_open)
        self.assertEqual(effects, ())

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
