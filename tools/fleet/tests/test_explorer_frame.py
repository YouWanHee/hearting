import copy
import dataclasses
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.fleet.explorer import frame as frame_module
from tools.fleet.explorer.frame import compose
from tools.fleet.explorer.output import OutputLine, cell_width, render_output
from tools.fleet.explorer.state import ExplorerState, initial_state
from tools.fleet.explorer.system import status_lines
from tools.fleet.explorer.types import Rect
from tools.fleet.tests.explorer_fixture import cells, load_screens, materialize, scene_result, tab

BORDER = "┃"


def widths_ok(test, frame, cols):
    test.assertEqual(frame.geometry, (cols, len(frame.lines)))
    test.assertEqual([i for i, line in enumerate(frame.lines) if cell_width(line) != cols], [])


class Geometry(unittest.TestCase):
    def test_rect_uses_half_open_geometry(self):
        rect = Rect(2, 3, 4, 5)
        self.assertTrue(rect.contains(2, 3))
        self.assertFalse(rect.contains(6, 3))
        self.assertFalse(rect.contains(2, 8))

    def test_b_frame_geometry_and_cell_width_at_canonical_sizes(self):
        for cols in (100, 168):
            frame = compose(cols=cols, rows=45, state=ExplorerState(tab="t"), providers={"header": ["작업 장부"]})
            self.assertEqual(len(frame.lines), 45)
            widths_ok(self, frame, cols)
            ledger, output = frame.region("ledger"), frame.region("output")
            self.assertEqual((frame.region("request").y, frame.region("tabs").y), (2, 3))
            self.assertEqual((ledger.y, frame.region("legend").y, frame.region("separator").y,
                              frame.region("events").y, frame.region("keys").y), (4, 40, 42, 43, 44))
            if cols == 100:
                self.assertEqual((ledger, output), (Rect(27, 4, 73, 17), Rect(27, 22, 73, 18)))
                self.assertEqual(cells(frame.lines[21], 27), "─" * 73)
            else:
                self.assertEqual((ledger, output), (Rect(41, 4, 62, 36), Rect(104, 4, 64, 36)))
                self.assertEqual({cells(frame.lines[y], 103, 104) for y in range(4, 40)}, {"│"})
                self.assertEqual({cells(frame.lines[y], 103, 104) for y in (2, 3)}, {" "})

    def test_navigation_border_is_the_only_cell_it_costs_and_runs_the_full_body(self):
        frame = compose(cols=168, rows=45)
        nav = frame.region("nav")
        self.assertEqual(nav, Rect(0, 2, 40, 38))
        for y in range(2, 40):
            self.assertEqual(cells(frame.lines[y], 40, 41), BORDER)
        self.assertNotIn(BORDER, frame.lines[40] + frame.lines[44])

    def test_hiding_the_navigation_gives_the_body_the_whole_width(self):
        shown = compose(cols=168, rows=45, state=ExplorerState())
        hidden = compose(cols=168, rows=45, state=ExplorerState(nav_visible=False))
        self.assertEqual(hidden.region("nav").w, 0)
        self.assertEqual(hidden.region("request"), Rect(0, 2, 168, 1))
        widths_ok(self, hidden, 168)
        self.assertNotIn(BORDER, "".join(hidden.lines))
        self.assertIn(BORDER, "".join(shown.lines))

    def test_below_100_columns_the_body_is_alone_until_e_opens_the_navigation_full_width(self):
        state = initial_state(80)
        body = compose(cols=80, rows=30, state=state)
        self.assertEqual((body.region("nav").w, body.region("request").w), (0, 80))
        widths_ok(self, body, 80)
        nav = compose(cols=80, rows=30, state=dataclasses.replace(state, nav_visible=True))
        self.assertEqual((nav.region("nav").w, nav.region("request").w), (80, 0))
        widths_ok(self, nav, 80)

    def test_hiding_the_event_row_gives_its_rows_to_the_body(self):
        shown = compose(cols=100, rows=45, state=ExplorerState())
        hidden = compose(cols=100, rows=45, state=ExplorerState(events_visible=False))
        self.assertEqual((hidden.region("events").h, hidden.region("separator").h), (0, 0))
        self.assertEqual(hidden.region("keys").y, 44)
        self.assertEqual(hidden.region("ledger").h + hidden.region("output").h + 1,
                         shown.region("ledger").h + shown.region("output").h + 1 + 2)
        widths_ok(self, hidden, 100)

    def test_tiny_terminals_never_raise_or_overflow(self):
        for cols, rows in ((1, 1), (10, 3), (40, 8), (99, 12), (100, 6), (200, 60)):
            with self.subTest(cols=cols, rows=rows):
                frame = compose(cols=cols, rows=rows)
                self.assertEqual(len(frame.lines), rows)
                widths_ok(self, frame, cols)

    def test_wide_and_narrow_characters_never_break_the_width_rule(self):
        session = {"session_id": "s", "harness": "claude", "cwd": "/w/한글프로젝트", "title": "한글 ⚑ 이모지 🙂 title",
                   "liveness": "working", "session_tag": "ab"}
        snapshot = SimpleNamespace(sessions=[session], jobs=[])
        for cols in (100, 168):
            widths_ok(self, compose(snapshot, None, ExplorerState(), cols, 45), cols)


class ErrorBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.scene = load_screens()["scenes"]["first"]
        cls.result = scene_result(cls.fx, cls.scene)
        cls.b6 = tab(cls.result, "b6-report").identity
        cls.fleet = tab(cls.result, "fleet-explorer").identity
        cls.state = ExplorerState(selection="request:" + cls.b6, tab=cls.b6, stage="test",
                                  tab_order=tuple(t.identity for t in cls.result.tabs))

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def frame(self, state=None, **kw):
        kw.setdefault("now", self.fx.now)
        return compose(self.fx.snapshot, self.result, state or self.state, 168, 45, **kw)

    @staticmethod
    def broken(*_args):
        raise RuntimeError("fixture failure")

    def rows(self, frame, name):
        rect = frame.region(name)
        return [cells(line, rect.x, rect.x + rect.w).rstrip() for line in frame.lines[rect.y:rect.y + rect.h]]

    def test_a_failing_navigation_keeps_its_previous_rows_and_age_and_leaves_every_other_region_alone(self):
        healthy = self.frame(now=self.fx.now - 300)
        broken = self.frame(providers={"nav": self.broken}, last_good=healthy)
        self.assertEqual(broken.errors, (("nav", "RuntimeError: fixture failure"),))
        nav = self.rows(broken, "nav")
        self.assertEqual(nav[0], "그리지 못함 · 직전 화면 5분 전")
        self.assertEqual(nav[1], "직전 화면")
        self.assertEqual(nav[2], self.rows(healthy, "nav")[0])
        for name in ("header", "request", "tabs", "ledger", "output", "legend", "events", "keys"):
            self.assertEqual(self.rows(broken, name), self.rows(healthy, name), name)

    def test_a_region_that_never_drew_says_so_instead_of_staying_blank(self):
        broken = self.frame(providers={"events": self.broken})
        self.assertEqual(self.rows(broken, "events")[0], "그리지 못함 · 직전 화면 없음")
        self.assertTrue(self.rows(broken, "nav")[0].startswith("탐색"))

    def test_the_failure_is_remembered_not_the_banner(self):
        healthy = self.frame(now=self.fx.now - 600)
        first = self.frame(providers={"nav": self.broken}, last_good=healthy, now=self.fx.now - 300)
        second = self.frame(providers={"nav": self.broken}, last_good=first)
        self.assertEqual(self.rows(second, "nav")[0], "그리지 못함 · 직전 화면 10분 전")
        self.assertEqual(self.rows(second, "nav")[2], self.rows(healthy, "nav")[0])

    def test_a_tab_renderer_exception_shows_that_tabs_previous_screen_and_drops_only_its_output_pane(self):
        healthy = self.frame(now=self.fx.now - 120)
        broken = self.frame(providers={"ledger": self.broken}, last_good=healthy)
        ledger = broken.region("ledger")
        self.assertEqual(broken.errors, (("ledger", "RuntimeError: fixture failure"),))
        self.assertEqual((broken.region("output").w, broken.region("output").h), (0, 0))
        self.assertEqual(ledger.w, 127)                                   # no split once the tab is gone
        text = [cells(line, 41).rstrip() for line in broken.lines[ledger.y:ledger.y + 4]]
        self.assertEqual(text[:3], ["그리지 못함 · 직전 화면 2분 전", "직전 화면", "b6-report · 완료 근거"])
        for name in ("nav", "header", "request", "tabs", "legend", "events", "keys"):
            self.assertEqual(self.rows(broken, name), self.rows(healthy, name), name)

    def test_each_tab_has_its_own_previous_screen(self):
        other = dataclasses.replace(self.state, selection="request:" + self.fleet, tab=self.fleet, stage="")
        on_b6 = self.frame(now=self.fx.now - 600)
        on_fleet = self.frame(other, last_good=on_b6, now=self.fx.now - 60)
        self.assertEqual(dict(on_fleet.last_good).keys() >= {f"ledger:{self.b6}:ledger"}, True)
        broken_b6 = self.frame(providers={"ledger": self.broken}, last_good=on_fleet)
        shown = [cells(line, 41).rstrip() for line in broken_b6.lines[4:7]]
        self.assertEqual(shown, ["그리지 못함 · 직전 화면 10분 전", "직전 화면", "b6-report · 완료 근거"])
        broken_fleet = self.frame(other, providers={"ledger": self.broken}, last_good=on_fleet)
        self.assertEqual(cells(broken_fleet.lines[4], 41).rstrip(), "그리지 못함 · 직전 화면 1분 전")
        self.assertNotIn("b6-report · 완료 근거", "".join(broken_fleet.lines[4:8]))

    def test_an_output_failure_does_not_erase_the_ledger(self):
        healthy = self.frame(now=self.fx.now - 30)
        broken = self.frame(providers={"output": self.broken}, last_good=healthy)
        self.assertEqual(self.rows(broken, "ledger"), self.rows(healthy, "ledger"))
        self.assertEqual(broken.region("output"), healthy.region("output"))
        output = [cells(line, 104).rstrip() for line in broken.lines[4:6]]
        self.assertEqual(output[0], "그리지 못함 · 직전 화면 30초 전")

    def test_the_event_row_keeps_updating_while_other_regions_fail(self):
        first = self.frame(now=self.fx.now - 60)
        newer = dataclasses.replace(self.result, events=self.result.events + (
            {"time_kst": "2026-10-20T21:30:00+09:00", "kind": "registry", "tag": "3e", "status": "open",
             "route_node": "report", "harness": "codex", "attempt_id": "att-new"},))
        second = compose(self.fx.snapshot, newer, self.state, 168, 45, last_good=first, now=self.fx.now,
                         providers={"nav": self.broken, "ledger": self.broken, "header": self.broken})
        self.assertTrue(self.rows(second, "events")[0].startswith("21:30 [3e] report 시작 CX · 21:18"))
        self.assertEqual({name for name, _msg in second.errors}, {"nav", "ledger", "header"})
        self.assertEqual(self.rows(second, "keys"), self.rows(first, "keys"))

    def test_plain_mapping_last_good_is_accepted(self):
        broken = self.frame(providers={"nav": self.broken}, last_good={"nav": ["기존 탐색"]})
        self.assertEqual(self.rows(broken, "nav")[2], "기존 탐색")
        self.assertEqual(self.rows(broken, "nav")[0], "그리지 못함 · 직전 화면 나이 ?")

    def test_explicit_rows_and_callables_replace_a_default_region(self):
        frame = self.frame(providers={"keys": ["q 끝"], "legend": lambda rect: [f"{rect.w}x{rect.h}"]})
        self.assertEqual(self.rows(frame, "keys")[0], "q 끝")
        self.assertEqual(self.rows(frame, "legend")[0], "168x2")


class Purity(unittest.TestCase):
    def test_compose_reads_nothing_and_does_not_import_the_renderer(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root)
            result = scene_result(fx, load_screens()["scenes"]["first"])
            before = copy.deepcopy((fx.snapshot.sessions, fx.snapshot.jobs))
            state = ExplorerState(tab=tab(result, "b6-report").identity)
            with mock.patch("builtins.open", side_effect=AssertionError("file read")), \
                    mock.patch("os.listdir", side_effect=AssertionError("listdir")), \
                    mock.patch("os.scandir", side_effect=AssertionError("scandir")), \
                    mock.patch.object(sqlite3, "connect", side_effect=AssertionError("db")), \
                    mock.patch.object(subprocess, "Popen", side_effect=AssertionError("process")):
                for cols in (100, 168):
                    compose(fx.snapshot, result, state, cols, 45, now=fx.now)
            self.assertEqual((fx.snapshot.sessions, fx.snapshot.jobs), before)
        self.assertFalse(hasattr(frame_module, "render"))
        source = Path(frame_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_build_lines", source)
        self.assertNotIn("import render", source)


class StatusBand(unittest.TestCase):
    def test_unknown_values_are_question_marks_not_zero(self):
        first, second = status_lines(SimpleNamespace(sessions=[], jobs=[]))
        self.assertEqual(first, "hearting │ ⠇도는 중 0 · 쉼 0 · 끝남 0 · ?확인 불가 0 · 종료 0")
        self.assertEqual(second, "jobs ?·herdr ?·compute ? │ 사용량 ? │ GPU ?")

    def test_only_abnormal_states_are_added_to_the_first_row(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root)
            normal, _ = status_lines(fx.snapshot)
            self.assertNotIn("⚠", normal)
            hosts = copy.deepcopy(fx.snapshot.compute_hosts)
            hosts["hosts"][0]["reachable"] = False
            down = SimpleNamespace(**{**vars(fx.snapshot), "compute_hosts": hosts})
            first, second = status_lines(down)
            self.assertTrue(first.endswith(" · ⚠ host 응답 없음 1"))
            self.assertTrue(second.endswith("│ GPU 6/8"))

    def test_counts_come_only_from_each_jobs_own_liveness(self):
        jobs = [{"liveness": value} for value in ("working", "working", "idle", "done", "dead", "blocked", "weird")]
        first, _second = status_lines(SimpleNamespace(sessions=[], jobs=jobs))
        self.assertEqual(first, "hearting │ ⠇도는 중 2 · 쉼 1 · ◑답 대기 1 · 끝남 1 · ?확인 불가 1 · 종료 1")


class EventsAndSystemPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        cls.events = tuple(cls.fx.source["events"])

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def test_events_are_newest_first_one_per_source_record_and_five_wide_three_narrow(self):
        from tools.fleet.explorer.events import build_events, event_bar, event_count
        lines = build_events(self.events + self.events)                       # a repeated source row counts once
        self.assertEqual(lines[:5], ("21:18 [3e] execute 시작 CX", "21:16 [3e] material-strategy 시작 CX",
                                     "21:10 [3e] owner 시작 CL", "21:08 [3e] 메시지 알림", "21:02 [3e] execute 종료 CX"))
        self.assertEqual(len(lines), len(self.events))
        self.assertEqual((event_count(100), event_count(168)), (3, 5))
        self.assertEqual(event_bar(self.events, 168).count(" · "), 4)
        self.assertEqual(event_bar(self.events, 100).count(" · "), 2)

    def test_an_event_is_dropped_whole_rather_than_cut_to_fit(self):
        from tools.fleet.explorer.events import event_bar
        bar = event_bar(self.events, 70, limit=5)
        self.assertTrue(bar.endswith("material-strategy 시작 CX"), bar)
        self.assertLessEqual(cell_width(bar), 70)

    def test_an_event_the_source_does_not_describe_is_not_invented(self):
        from tools.fleet.explorer.events import event_text
        self.assertEqual(event_text({"time_kst": "2026-10-20T09:05:00+09:00", "kind": "registry", "status": "done"}),
                         "09:05 시도 종료")
        self.assertEqual(event_text({"kind": "peer-ledger", "record_type": "x"}), "--:-- 메시지")

    def test_no_events_says_so(self):
        frame = compose(self.fx.snapshot, None, ExplorerState(), 100, 45)
        self.assertEqual(frame.lines[43].rstrip(), "사건 없음")

    def test_system_page_lists_usage_running_counts_hosts_gpus_owners_and_the_job_owned_process(self):
        from tools.fleet.explorer.system import system_lines
        lines = system_lines(self.fx.snapshot)
        self.assertEqual(lines[:3], ("시스템 · 사용량 / 배분 / host > GPU", "CL 5h 29% · CX 주 8% · OC 5h 0%",
                                     "실행 CL 4 · CX 1 · OC 0 / 동시 5/30"))
        self.assertIn("moving4 CPU 18% · MEM 57914/128276MiB", lines)
        self.assertIn("  GPU0 · 쉼 · VRAM 2/12212MiB", lines)
        self.assertEqual(lines[lines.index("  GPU0 · 쉼 · VRAM 2/12212MiB") + 1], "    세션 귀속 없음")
        self.assertIn("    claude:4bf921ef, codex:01a127de", lines)
        self.assertEqual(lines[-2:], ("mpwav GPU0 PID 2106548 → job:att-d9f6cfaf", "  세션 [01] · 주인 확인됨"))
        self.assertEqual(len(lines), 30)

    def test_an_unreachable_host_and_an_unowned_process_are_shown_as_such(self):
        from tools.fleet.explorer.system import system_lines
        hosts = copy.deepcopy(self.fx.snapshot.compute_hosts)
        hosts["hosts"][1]["reachable"] = False
        stray = [{"host": "mpwav", "gpu_indexes": [1], "pid": 7, "owner_kind": None, "owner_label": None,
                  "session_owner": None}]
        snapshot = SimpleNamespace(**{**vars(self.fx.snapshot), "compute_hosts": hosts, "unregistered_gpu": stray})
        lines = system_lines(snapshot)
        self.assertIn("cnn 응답 없음", lines)
        self.assertEqual(lines[-2:], ("mpwav GPU1 PID 7 → 주인 없음", "  주인 확인 불가"))
        self.assertEqual(system_lines(SimpleNamespace(sessions=[], jobs=[]))[-1], "host > GPU ? · 읽지 못함")


class OutputRows(unittest.TestCase):
    def test_output_is_one_row_per_record_and_clips_by_display_cells(self):
        rows = render_output([OutputLine("한글 record with long body", 3), "second\nphysical line"], 14, 2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(cell_width(row) <= 14 for row in rows))
        self.assertIn("…", rows[0])
        self.assertNotIn("physical line", rows[1])


if __name__ == "__main__":
    unittest.main()
