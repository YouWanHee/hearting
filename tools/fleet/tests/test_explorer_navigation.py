import copy
import tempfile
import unittest
from types import SimpleNamespace

from tools.fleet.explorer.nav import build_nav, nav_lines, role_name, visible_rows
from tools.fleet.explorer.state import ExplorerState
from tools.fleet.tests.explorer_fixture import collect, load_screens, materialize, tab


def lines_for(fx, result, width, height=38, **state):
    request = tab(result, "b6-report").identity
    view = ExplorerState(selection="request:" + request, tab=request, **state)
    rows = build_nav(fx.snapshot, result, view, width=width)
    text, _offset = nav_lines(visible_rows(rows, view), view, width, height)
    return rows, text


class CanonicalNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.base = collect(cls.fx)
        request = tab(cls.base, "b6-report")
        cls.result = collect(cls.fx, request_id=request.identity, stage_id="test")
        cls.screens = load_screens()["scenes"]["first"]["widths"]

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def approved(self, cols):
        rows = self.screens[str(cols)]["rows"][2:]
        return [row.split("┃", 1)[0].rstrip() for row in rows[:31]]

    def test_100_column_rows_equal_the_approved_navigation(self):
        _rows, text = lines_for(self.fx, self.result, 26)
        want = [row for row in self.approved(100)[:23]]
        self.assertEqual([line.rstrip() for line in text], want[:len(text)])
        self.assertEqual(len(text), 23)

    def test_168_column_rows_open_the_system_section(self):
        _rows, text = lines_for(self.fx, self.result, 40)
        self.assertEqual([line.rstrip() for line in text], self.approved(168)[:len(text)])
        self.assertEqual(text[-1], "탐색 1–27 / 27 (접힘 제외)")

    def test_named_values(self):
        _rows, text = lines_for(self.fx, self.result, 40)
        joined = "\n".join(text)
        for want in ("▾ 세션 13 · worker 6", " ▾ hearting ⠇2 ·1 ?2", "  ⠇ CL [b6]⚑ ▸? baseline 경로 재제출",
                     "  ⠇ CL [3e] ▾9 P3+P5 구현", "    ▶ b6-report", "      ▸ 단계 5", "    ▸ fleet-explorer",
                     "    ▸ 그 외 요청 7", " ▸ SR_CorrNet ⠇1 ·2 ◑1", " ▸ home-os ·1 [34]⚑", "▸ 남은 일 5",
                     "▸ 관계 ⚑2 관찰6 최근필드16", "탐색 1–27 / 27 (접힘 제외)"):
            self.assertIn(want, joined)

    def test_requests_are_the_selected_one_plus_the_newest_open_one_and_the_rest_are_counted(self):
        rows, _text = lines_for(self.fx, self.result, 40)
        shown = [r.label for r in rows if r.kind == "request" and r.depth == 3]
        self.assertEqual(shown, ["b6-report", "fleet-explorer"])
        hidden = [r.label for r in rows if r.kind == "request" and r.depth == 4]
        self.assertEqual(len(hidden), 7)
        self.assertEqual(len(hidden) + len(shown), 9)

    def test_stage_count_is_known_only_for_the_request_whose_ledger_was_read(self):
        rows, _text = lines_for(self.fx, self.result, 40)
        counts = {r.identity.split(":", 1)[1]: r.label for r in rows if r.kind == "stage-group"}
        b6 = tab(self.base, "b6-report").identity
        fleet = tab(self.base, "fleet-explorer").identity
        self.assertEqual(counts[b6], "단계 5")
        self.assertEqual(counts[fleet], "단계")

    def test_unselected_unexpanded_sessions_show_unknown_request_counts(self):
        rows, text = lines_for(self.fx, self.result, 40)
        sessions = [r for r in rows if r.kind == "session"]
        self.assertEqual(len(sessions), 13)
        other = [r.label for r in sessions if "[3e]" not in r.label]
        self.assertTrue(all(" ▸? " in f" {label} " for label in other), other)
        plain = build_nav(self.fx.snapshot)               # no selected scope read at all
        self.assertTrue(all(" ▸? " in f" {r.label} " for r in plain if r.kind == "session"))

    def test_expanding_a_session_changes_its_token_only(self):
        session = next(r for r in build_nav(self.fx.snapshot, self.result)
                       if r.kind == "session" and "[b6]" in r.label)
        view = ExplorerState(nav_open=(session.identity,))
        rows = build_nav(self.fx.snapshot, self.result, view)
        again = next(r for r in rows if r.identity == session.identity)
        self.assertIn("▾?", again.label)
        self.assertTrue(again.open)

    def test_project_aggregate_counts_only_main_sessions_and_collapsed_projects_name_stewards(self):
        rows, _text = lines_for(self.fx, self.result, 40)
        by = {r.identity: r.label for r in rows if r.kind == "project"}
        self.assertEqual(by["project:hearting"], "hearting ⠇2 ·1 ?2")
        self.assertEqual(by["project:SR_CorrNet"], "SR_CorrNet ⠇1 ·2 ◑1")
        self.assertEqual(by["project:home-os"], "home-os ·1 [34]⚑")
        self.assertEqual(by["project:cairn"], "cairn ?1")

    def test_a_project_name_that_does_not_fit_drops_its_date_prefix_first(self):
        _rows, narrow = lines_for(self.fx, self.result, 26)
        _rows, wide = lines_for(self.fx, self.result, 40)
        self.assertIn(" ▸ TASLP_SR-CorrNet ?1", [line.rstrip() for line in narrow])
        self.assertIn(" ▸ 2025-12_TASLP_SR-CorrNet ?1", [line.rstrip() for line in wide])

    def test_section_rows(self):
        rows, _text = lines_for(self.fx, self.result, 40)
        sections = [r.identity for r in rows if r.kind == "section"]
        self.assertEqual(sections, ["section:sessions", "section:todo", "section:relations", "section:system"])
        system = [r.label for r in rows if r.parent == "section:system"]
        self.assertEqual(system, ["CL 5h 29% · CX 주 8% · OC 5h 0%", "배분 CL4 CX1 OC0 · 5/30",
                                  "moving4 > GPU 2/2", "cnn > GPU 2/2", "xavier > GPU 2/2",
                                  "larynx > GPU 0/2", "mpwav > GPU 2/2"])

    def test_remaining_work_count_is_unknown_until_it_was_read(self):
        todo = next(r for r in build_nav(self.fx.snapshot) if r.identity == "section:todo")
        self.assertEqual(todo.label, "남은 일 ?")


class ProjectName(unittest.TestCase):
    def test_path_rules_need_no_io_and_a_caller_resolver_wins(self):
        from tools.fleet.explorer.nav import project_name
        self.assertEqual(project_name("/w/personal/hearting"), "hearting")
        self.assertEqual(project_name("/w/personal/hearting-wt/framed-x-1a2b3c4d5e"), "hearting")
        self.assertEqual(project_name("/w/proj_worktrees/feat"), "proj")
        self.assertEqual(project_name("/w/repo/.worktrees/feat"), "repo")
        self.assertEqual(project_name("/w/app.broken-1"), "app")
        self.assertEqual(project_name(""), "(unknown)")
        self.assertEqual(project_name("/w/app", lambda cwd: "resolved:" + cwd), "resolved:/w/app")

    def test_a_snapshot_resolver_is_used_for_project_rows(self):
        session = dict(session_id="s", harness="claude", cwd="/w/app", liveness="idle", session_tag="aa")
        snapshot = SimpleNamespace(sessions=[session], jobs=[], project_of=lambda cwd: "git:" + cwd)
        from tools.fleet.explorer.frame import compose
        frame = compose(snapshot, None, ExplorerState(), 168, 45)
        self.assertEqual([r.label for r in frame.nav_rows if r.kind == "project"], ["git:/w/app ·1"])


class PreviousConversations(unittest.TestCase):
    """The entrance to earlier conversations exists only for the focused, open session and only when
    its seat's lineage read found some; a failed read is a ``?`` with the reason, never a made-up row."""

    SESSION = "session:claude:sid-a"

    def snapshot(self):
        main = dict(session_id="sid-a", harness="claude", cwd="/work/repo", title="backend",
                    liveness="working", session_tag="aa")
        return SimpleNamespace(sessions=[main], jobs=[])

    def result(self, lineage=None, health=None):
        from tools.fleet.explorer.types import FocusKey, FocusResult
        return FocusResult(FocusKey("claude", "sid-a"), 1, 1, lineage=lineage, lineage_health=health)

    ENTRIES = (
        {"harness": "claude", "sid": "sid-old-111122223333", "source": "clear", "title": "이전 카드 제목"},
        {"harness": "codex", "sid": "cx-older-9999", "source": "resume", "title": ""},
    )

    def kinds(self, rows, kind):
        return [r for r in rows if r.kind == kind]

    def test_no_read_means_no_row(self):
        self.assertEqual(self.kinds(build_nav(self.snapshot(), self.result()), "previous-generation"), [])
        self.assertEqual(self.kinds(build_nav(self.snapshot()), "previous-generation"), [])

    def test_no_earlier_conversation_means_no_row(self):
        rows = build_nav(self.snapshot(), self.result(lineage=()))
        self.assertEqual(self.kinds(rows, "previous-generation"), [])

    def test_the_entrance_is_closed_by_default_and_counts_the_generations(self):
        rows = build_nav(self.snapshot(), self.result(lineage=self.ENTRIES))
        entrance = self.kinds(rows, "previous-generation")
        self.assertEqual(len(entrance), 1)
        self.assertEqual((entrance[0].parent, entrance[0].depth, entrance[0].label, entrance[0].marker),
                         (self.SESSION, 3, "이전 대화 2", "▸"))
        self.assertIsNone(entrance[0].target_ref)
        self.assertEqual(self.kinds(rows, "generation"), [])

    def test_opening_it_lists_one_row_per_generation_with_harness_sid_source_and_card_title(self):
        state = ExplorerState(nav_open=("previous-generation:claude:sid-a",))
        rows = build_nav(self.snapshot(), self.result(lineage=self.ENTRIES), state)
        entrance = self.kinds(rows, "previous-generation")[0]
        self.assertEqual((entrance.marker, entrance.open), ("▾", True))
        generations = self.kinds(rows, "generation")
        self.assertEqual([g.label for g in generations], ["CL sid-old- clear 이전 카드 제목", "CX cx-older resume"])
        self.assertEqual([g.identity for g in generations], [
            "generation:claude:sid-a:claude:sid-old-111122223333", "generation:claude:sid-a:codex:cx-older-9999"])
        self.assertTrue(all(g.parent == entrance.identity and g.depth == 4 and g.target_ref is None
                            for g in generations))

    def test_the_rows_are_never_kill_targets(self):
        state = ExplorerState(nav_open=("previous-generation:claude:sid-a",))
        rows = build_nav(self.snapshot(), self.result(lineage=self.ENTRIES), state)
        for row in rows:
            if row.kind in ("previous-generation", "generation"):
                self.assertIsNone(row.target_ref)

    def test_a_failed_read_is_a_question_mark_with_the_reason(self):
        from tools.fleet.explorer.types import Health
        rows = build_nav(self.snapshot(), self.result(health=Health("failed", None, "OSError")))
        entrance = self.kinds(rows, "previous-generation")
        self.assertEqual([r.label for r in entrance], ["이전 대화 ? · OSError"])
        self.assertIsNone(entrance[0].open)

    def test_a_folded_session_has_no_entrance(self):
        state = ExplorerState(nav_fold=(self.SESSION,))
        rows = build_nav(self.snapshot(), self.result(lineage=self.ENTRIES), state)
        self.assertEqual(self.kinds(rows, "previous-generation"), [])

    def test_a_session_that_is_not_the_focus_has_no_entrance(self):
        from tools.fleet.explorer.types import FocusKey, FocusResult
        other = FocusResult(FocusKey("claude", "someone-else"), 1, 1, lineage=self.ENTRIES)
        rows = build_nav(self.snapshot(), other)
        self.assertEqual(self.kinds(rows, "previous-generation"), [])


class RoleNameAndStewardFlag(unittest.TestCase):
    def label(self, **fields):
        session = dict(session_id="s1", harness="codex", cwd="/work/repo", liveness="idle",
                       session_tag="t1", **fields)
        rows = build_nav(SimpleNamespace(sessions=[session], jobs=[]))
        return next(r.label for r in rows if r.kind == "session")

    def test_role_comes_from_title_then_summary_and_is_never_guessed(self):
        self.assertEqual(self.label(title="reviewer"), "· CX [t1] ▸? reviewer")
        self.assertEqual(self.label(summary="from summary"), "· CX [t1] ▸? from summary")
        self.assertEqual(self.label(), "· CX [t1] ▸? 역할 미확인")
        self.assertEqual(role_name({"slug": "framed-something-owner"}), "역할 미확인")

    def test_flag_is_the_steward_fact_not_a_word_in_the_role(self):
        self.assertEqual(self.label(title="⚑ 감시 정리", steward=False), "· CX [t1] ▸? ⚑ 감시 정리")
        self.assertEqual(self.label(title="reviewer", steward=True), "· CX [t1]⚑ ▸? reviewer")
        self.assertEqual(self.label(steward=True), "· CX [t1]⚑ ▸? 역할 미확인")


class FoldAndSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.result = collect(cls.fx, request_id=tab(collect(cls.fx), "b6-report").identity)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_folding_a_project_hides_its_sessions_and_keeps_the_rows_in_the_list(self):
        view = ExplorerState(nav_fold=("project:hearting",))
        rows = build_nav(self.fx.snapshot, self.result, view)
        shown = [r.identity for r in visible_rows(rows, view)]
        self.assertIn("project:hearting", shown)
        self.assertFalse([i for i in shown if i.startswith("session:") and "b68ba0fa" in i])
        self.assertTrue([r for r in rows if r.kind == "session" and "[3e]" in r.label])   # still in the list

    def test_opening_a_collapsed_project_shows_its_sessions(self):
        view = ExplorerState(nav_open=("project:cairn",))
        rows = build_nav(self.fx.snapshot, self.result, view)
        shown = [r.label for r in visible_rows(rows, view)]
        self.assertIn("? CL [a7] ▸? 케언 서비스 복구", shown)

    def test_search_keeps_matches_with_their_ancestors(self):
        view = ExplorerState(search="케언")
        rows = build_nav(self.fx.snapshot, self.result, view)
        shown = [r.label for r in visible_rows(rows, view)]
        self.assertEqual(shown, ["세션 13 · worker 6", "cairn ?1", "? CL [a7] ▸? 케언 서비스 복구"])

    def test_scrolling_the_viewport_does_not_change_the_row_set(self):
        view = ExplorerState(selection="section:system", scrolls=(("nav", 99),))
        rows = visible_rows(build_nav(self.fx.snapshot, self.result, view, width=40), view)
        text, offset = nav_lines(rows, view, 40, 12)
        self.assertEqual(len(text), 12)
        self.assertTrue(text[-1].startswith("탐색 "))
        self.assertGreater(offset, 0)
        self.assertTrue(any(line.startswith("▶ ") or line.startswith("▶") for line in text[2:-1]))

    def test_build_does_not_mutate_the_snapshot(self):
        before = copy.deepcopy((self.fx.snapshot.sessions, self.fx.snapshot.jobs))
        build_nav(self.fx.snapshot, self.result, ExplorerState(), width=40)
        self.assertEqual((self.fx.snapshot.sessions, self.fx.snapshot.jobs), before)


if __name__ == "__main__":
    unittest.main()
