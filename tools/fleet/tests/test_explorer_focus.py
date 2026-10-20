import dataclasses
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.fleet import route
from tools.fleet.explorer import focus as focus_module
from tools.fleet.explorer.focus import FocusReader, SelectionPump, status_text
from tools.fleet.explorer.output import OutputReader
from tools.fleet.explorer.types import FocusKey, FocusRequest, FocusResult, Health
from tools.fleet.tests import explorer_fixture as ef
from tools.fleet.tests.explorer_fixture import collect, materialize, tab

OTHER_SID = "4d26e1a2-3052-440f-ba62-1cdb2da7b7d6"


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


class GatedProvider:
    """Records every request it is asked to read; each read blocks until released."""

    def __init__(self, fail=False):
        self.requests = []
        self.entered = threading.Semaphore(0)
        self.gate = threading.Semaphore(0)
        self.fail = fail

    def __call__(self, request):
        self.requests.append(request)
        self.entered.release()
        self.gate.acquire(timeout=3)
        if self.fail:
            raise RuntimeError("reader down")
        return FocusResult(request.key, request.epoch, request.snapshot_generation,
                           output=(request.key.sid,), health=Health("success", time.time()))


class StaleResults(unittest.TestCase):
    def test_a_late_result_for_an_earlier_selection_is_dropped_a_b_a(self):
        provider = GatedProvider()
        pump = SelectionPump(provider, interval=0.1)
        a, b = FocusKey("claude", "A"), FocusKey("claude", "B")
        pump.select(a, 1)
        pump.request()
        self.assertTrue(provider.entered.acquire(timeout=2))          # the read of A (epoch 1) is in flight
        pump.select(b, 2)
        pump.request()
        pump.select(a, 3)
        pump.request()
        provider.gate.release()                                        # the old A read finishes now
        self.assertTrue(provider.entered.acquire(timeout=2))          # the coalesced follow-up starts
        self.assertIsNone(pump.poll())                                 # the late epoch-1 result is not shown
        self.assertEqual([r.epoch for r in provider.requests], [1, 3])
        provider.gate.release()
        result = wait_for(pump.poll)
        self.assertEqual((result.key, result.epoch), (a, 3))
        pump.stop()

    def test_selections_during_a_read_coalesce_into_one_follow_up(self):
        provider = GatedProvider()
        pump = SelectionPump(provider, interval=0.1)
        pump.select(FocusKey("claude", "A"), 1)
        pump.request()
        self.assertTrue(provider.entered.acquire(timeout=2))
        for sid in ("B", "C", "D"):
            pump.select(FocusKey("claude", sid), 1)
            pump.request()
        provider.gate.release()
        self.assertTrue(provider.entered.acquire(timeout=2))
        provider.gate.release()
        result = wait_for(pump.poll)
        self.assertEqual(result.key.sid, "D")
        time.sleep(0.2)
        self.assertEqual([r.key.sid for r in provider.requests], ["A", "D"])    # B and C were never read
        pump.stop()

    def test_nothing_is_read_before_a_selection_exists(self):
        provider = GatedProvider()
        pump = SelectionPump(provider, interval=0.1)
        pump.request()
        wait_for(lambda: not pump._pump.running)
        self.assertEqual(provider.requests, [])
        self.assertIsNone(pump.poll())
        self.assertEqual(pump.status().state, "idle")
        pump.stop()

    def test_returning_to_a_selection_shows_its_last_good_value_marked_stale(self):
        provider = GatedProvider()
        for _ in range(4):
            provider.gate.release()
        pump = SelectionPump(provider, interval=0.1)
        a, b = FocusKey("claude", "A"), FocusKey("claude", "B")
        pump.select(a, 1)
        pump.request()
        first = wait_for(pump.poll)
        self.assertEqual(first.key, a)
        pump.select(b, 1)
        self.assertIsNone(pump.poll())                                  # B has nothing yet: reading
        self.assertEqual(pump.status().state, "loading")
        pump.select(a, 1)
        again = pump.poll()
        self.assertEqual((again.key, again.epoch), (a, 1))              # the earlier A value, flagged by status
        self.assertEqual(pump.status().state, "stale")
        pump.stop()


class FirstResultAndFailure(unittest.TestCase):
    def test_reading_state_before_the_first_result(self):
        provider = GatedProvider()
        pump = SelectionPump(provider, interval=0.1)
        pump.select(FocusKey("claude", "A"), 1)
        pump.request()
        self.assertTrue(provider.entered.acquire(timeout=2))
        health = pump.status()
        self.assertEqual((health.state, status_text(health)), ("loading", "읽는 중"))
        provider.gate.release()
        pump.stop()

    def test_first_failure_says_no_success_on_record(self):
        provider = GatedProvider(fail=True)
        provider.gate.release()
        pump = SelectionPump(provider, interval=0.1)
        pump.select(FocusKey("claude", "A"), 1)
        pump.request()
        health = wait_for(lambda: pump.status() if pump.status().state == "failed" else None)
        self.assertEqual(health.state, "failed")
        self.assertIsNone(health.last_success_at)
        self.assertEqual(status_text(health), "성공 기록 없음 · RuntimeError: reader down")
        self.assertIsNone(pump.poll())
        pump.stop()

    def test_failure_after_success_keeps_the_last_good_value_with_its_age(self):
        provider = GatedProvider()
        provider.gate.release()
        pump = SelectionPump(provider, interval=0.1)
        pump.select(FocusKey("claude", "A"), 1)
        pump.request()
        good = wait_for(pump.poll)
        self.assertEqual(pump.status().state, "success")
        provider.fail = True
        provider.gate.release()
        pump.request()
        stale = wait_for(lambda: pump.status() if pump.status().state == "stale" else None)
        self.assertEqual(pump.poll(), good)                              # the value stays on screen
        self.assertEqual(stale.last_success_at, good.health.last_success_at)
        self.assertEqual(status_text(stale, now=good.health.last_success_at + 42),
                         "직전 값 42초 전 · RuntimeError: reader down")
        pump.stop()

    def test_status_text_for_a_current_result_is_empty(self):
        self.assertEqual(status_text(Health("success", 1.0)), "")


class SelectionScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def recorded(self, **kwargs):
        loaded, gated = [], []
        real_gate = route.gate_mark

        def loader(path, expect_id=None):
            loaded.append(os.path.basename(path))
            return self.fx.route_loader(path, expect_id=expect_id)

        def gate(record, node, **kw):
            gated.append((record["route_id"], node))
            return real_gate(record, node, **kw)

        with mock.patch.object(route, "gate_mark", gate):
            result = collect(self.fx, route_loader=loader, **kwargs)
        return result, loaded, gated

    def test_only_the_selected_sessions_owner_routes_are_read(self):
        _result, loaded, _gated = self.recorded()
        self.assertEqual(sorted(loaded), sorted(f"{r}.json" for r in (
            "rt-8fd88141c5f7c09d", "rt-b45cc23fa142a3f1", "rt-bca10c8fd3987697", "rt-caad53bdc377c0d7",
            "rt-c76bc6d1526e284e", "rt-652177b283574d4b", "rt-9c7de648f511ddf5")))
        self.assertNotIn("rt-1bf455f25a62f4fe.json", loaded)           # another session's owner route

    def test_other_session_selection_reads_nothing_of_the_main_session(self):
        result, loaded, gated = self.recorded(sid=OTHER_SID)
        self.assertEqual(loaded, ["rt-1bf455f25a62f4fe.json"])
        self.assertEqual(len(result.tabs), 1)
        self.assertFalse(result.tabs[0].verified)
        self.assertEqual(gated, [])

    def test_overview_selection_checks_gates_only_for_the_latest_failures(self):
        _result, _loaded, gated = self.recorded(request_id="")
        self.assertEqual(sorted(gated), sorted([("rt-caad53bdc377c0d7", "execute"),
                                                ("rt-c76bc6d1526e284e", "plan"),
                                                ("rt-c76bc6d1526e284e", "execute")]))

    def test_selected_request_reads_its_own_nodes_and_frame_route_only(self):
        b6 = tab(collect(self.fx), "b6-report")
        result, loaded, gated = self.recorded(request_id=b6.identity)
        self.assertIn("rt-e91bca1524cc3585.json", loaded)               # the selected request's frame route
        self.assertEqual(sum(name.endswith(".json") for name in loaded), 8)
        self.assertFalse({"rt-4a5faafa1a56654b.json", "rt-3942cabe32950573.json"} & set(loaded))   # hidden tabs' frames
        self.assertEqual({node for route_id, node in gated if route_id == "rt-bca10c8fd3987697"},
                         {"plan", "execute", "impl-review", "test", "report"})
        self.assertFalse([g for g in gated if g[0] in ("rt-b45cc23fa142a3f1", "rt-8fd88141c5f7c09d")])
        self.assertEqual(result.output, ())                             # no raw output is opened here

    def test_unexpanded_sessions_do_not_widen_the_read(self):
        reader = FocusReader(self.fx.jobs_path, self.fx.artifact_root, route_loader=self.fx.route_loader,
                             live_jobs=self.fx.live_jobs, state_roots=self.fx.state_roots, now=self.fx.now)
        from tools.fleet.explorer.types import FocusRequest
        a = reader(FocusRequest(FocusKey("claude", self.fx.sid), 1, 1, ()))
        b = reader(FocusRequest(FocusKey("claude", self.fx.sid), 2, 1, ("some-other-session",)))
        self.assertEqual([t.identity for t in a.tabs], [t.identity for t in b.tabs])
        self.assertEqual(len(reader.cache._items) > 0, True)


class SourceCacheBehaviour(unittest.TestCase):
    def test_unchanged_files_are_not_read_twice_and_a_changed_one_is(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root)
            reader = FocusReader(fx.jobs_path, fx.artifact_root, route_loader=fx.route_loader,
                                 live_jobs=fx.live_jobs, state_roots=fx.state_roots, now=fx.now)
            from tools.fleet.explorer.types import FocusRequest
            request = FocusRequest(FocusKey("claude", fx.sid), 1, 1)
            reader(request)
            first = reader.cache.reads
            self.assertGreater(first, 0)
            reader(request)
            self.assertEqual(reader.cache.reads, first)
            path = os.path.join(fx.state_roots[0], "peer-steward", "obligations",
                                "registered-batch-0c2c4c06739f317fd30ccce06ddf5b4c.json")
            with open(path) as handle:
                record = json.load(handle)
            record["delivery"] = "acked"
            record["state"] = "complete"
            with open(path, "w") as handle:
                json.dump(record, handle)
            changed = reader(request)
            self.assertEqual(reader.cache.reads, first + 1)
            self.assertEqual(len(changed.obligations), 4)

    def test_cache_is_bounded(self):
        cache = focus_module.SourceCache(limit=2)
        with tempfile.TemporaryDirectory() as root:
            for n in range(5):
                path = os.path.join(root, f"{n}.json")
                with open(path, "w") as handle:
                    json.dump({"n": n}, handle)
                cache.read_json(path)
            self.assertLessEqual(len(cache._items), 2)
            self.assertIsNone(cache.read_json(os.path.join(root, "missing.json")))


class FailureIsolation(unittest.TestCase):
    def test_unreadable_registry_fails_the_read_and_keeps_the_pump_alive(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root)
            broken = os.path.join(root, "state", "jobs.log.d")
            os.mkdir(broken)
            reader = FocusReader(broken, fx.artifact_root, route_loader=fx.route_loader, state_roots=fx.state_roots)
            pump = SelectionPump(reader, interval=0.1)
            pump.select(FocusKey("claude", fx.sid), 1)
            pump.request()
            health = wait_for(lambda: pump.status() if pump.status().state == "failed" else None)
            self.assertEqual(status_text(health).split(" · ")[0], "성공 기록 없음")
            reader.jobs_path = fx.jobs_path
            reader.live_jobs = fx.live_jobs
            pump.request()
            result = wait_for(pump.poll)
            self.assertEqual(len(result.tabs), 9)
            self.assertEqual(pump.status().state, "success")
            pump.stop()

    def test_a_missing_registry_means_no_requests_not_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root)
            result = collect(fx, jobs_path=os.path.join(root, "state", "absent.log"))
            self.assertEqual((result.tabs, result.health.state), ((), "success"))
            self.assertEqual((result.header.total, result.header.open), (0, 0))

    def test_a_ledger_fault_keeps_tabs_and_marks_the_result_partial(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root)
            b6 = tab(collect(fx), "b6-report")
            with mock.patch.object(focus_module, "build_request_ledger", side_effect=RuntimeError("boom")):
                result = collect(fx, request_id=b6.identity)
            self.assertEqual(len(result.tabs), 9)
            self.assertIsNone(result.request_ledger)
            self.assertEqual((result.health.state, result.health.error), ("partial", "ledger: RuntimeError"))

    def test_obligation_fault_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root)
            with mock.patch.object(focus_module, "read_obligations", side_effect=OSError("nas")):
                result = collect(fx)
            self.assertIsNone(result.obligations)
            self.assertIn("남은 일 ? · 읽지 못함", result.overview)
            self.assertEqual(result.health.state, "partial")


class LiveEvidenceIsOnlyPassedThrough(unittest.TestCase):
    def test_liveness_and_process_proof_come_from_the_live_job(self):
        with tempfile.TemporaryDirectory() as root:
            fx = materialize(root)
            with_live = collect(fx, request_id=tab(collect(fx), "b6-report").identity)
            without = collect(fx, request_id=tab(collect(fx), "b6-report").identity, live_jobs={})
            self.assertEqual(with_live.request_ledger.others, (("owner-fleet", "idle", "pending"),))
            self.assertEqual(without.request_ledger.others,
                             (("b-p3", "unknown", "pending"), ("herdr-tmux", "unknown", "pending"),
                              ("owner-fleet", "unknown", "pending"), ("fleet-explorer", "unknown", "pending")))
            self.assertEqual(with_live.header, without.header)


class CountingReader(OutputReader):
    """An output reader that counts every source it resolves, reads or reads further back."""

    def __init__(self, **options):
        super().__init__(**options)
        self.earlier_calls = []

    def read(self, scope_key, title, plans, *, stamped=False, earlier=False, now=None):
        self.earlier_calls.append(earlier)
        return super().read(scope_key, title, plans, stamped=stamped, earlier=earlier, now=now)


class OutputIsReadForTheVisibleSelectionOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fx = materialize(cls.dir.name)
        ef.materialize_output(cls.fx)
        cls.b6 = tab(collect(cls.fx), "b6-report").identity
        cls.fleet = tab(collect(cls.fx), "fleet-explorer").identity

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def pump(self, reader):
        focus = FocusReader(jobs_path=self.fx.jobs_path, artifact_root=self.fx.artifact_root,
                            route_loader=self.fx.route_loader, live_jobs=self.fx.live_jobs,
                            snapshot=self.fx.snapshot, state_roots=self.fx.state_roots,
                            now=self.fx.now, output_reader=reader)
        return focus, SelectionPump(focus, interval=0.05)

    def run_pump(self, pump, key, expanded=()):
        pump.select(key, 1, expanded)
        pump.request()
        return wait_for(pump.poll)

    def test_a_hidden_tab_is_never_resolved_or_read(self):
        reader = CountingReader(tz=ef.KST)
        _focus, pump = self.pump(reader)
        with ef.fixture_env(self.fx):
            shown = self.run_pump(pump, FocusKey("claude", self.fx.sid, self.b6, "test"))
            self.assertEqual(shown.output_title, "test · CX · 원래 출력")
            counts = (reader.resolved, reader.reads)
            self.assertEqual(counts, (1, 1))
            # Only the overview is selected now: no tab is visible, so nothing is resolved or read.
            overview = self.run_pump(pump, FocusKey("claude", self.fx.sid, "", ""))
            self.assertEqual(overview.output, ())
            time.sleep(0.2)
            self.assertEqual((reader.resolved, reader.reads), counts)
        pump.stop()

    def test_other_tabs_of_the_session_are_not_read_while_one_is_shown(self):
        reader = CountingReader(tz=ef.KST)
        _focus, pump = self.pump(reader)
        with ef.fixture_env(self.fx):
            self.run_pump(pump, FocusKey("claude", self.fx.sid, self.fleet, ""))
            first = reader.resolved
            self.assertEqual(first, 2)                       # the supervisor and the one stage of fleet-explorer
            for _ in range(3):
                pump.request()
                time.sleep(0.15)
            self.assertEqual(reader.resolved, first)         # the scope is continued from its cursors
        pump.stop()

    def test_a_late_output_for_an_earlier_tab_is_not_shown_for_the_new_one(self):
        gate, entered = threading.Event(), threading.Semaphore(0)

        class Slow(OutputReader):
            def read(inner, scope_key, title, plans, **options):
                if scope_key.request_id == self.b6:
                    entered.release()
                    gate.wait(2)
                return super().read(scope_key, title, plans, **options)

        reader = Slow(tz=ef.KST)
        _focus, pump = self.pump(reader)
        with ef.fixture_env(self.fx):
            pump.select(FocusKey("claude", self.fx.sid, self.b6, "test"), 1)
            pump.request()
            self.assertTrue(entered.acquire(timeout=2))
            pump.select(FocusKey("claude", self.fx.sid, self.fleet, ""), 1)
            pump.request()
            gate.set()
            result = wait_for(lambda: (lambda r: r if r is not None and r.key.request_id == self.fleet else None)(pump.poll()))
        self.assertEqual(result.output_title, "fleet-explorer · 감독 CL + 단계 CX")
        self.assertTrue(all("[test]" not in row.text for row in result.output))
        pump.stop()

    def test_scrolling_back_reads_older_records_once_and_only_when_asked(self):
        reader = CountingReader(tz=ef.KST)
        focus, pump = self.pump(reader)
        key = FocusKey("claude", self.fx.sid, self.b6, "test")
        with ef.fixture_env(self.fx):
            self.run_pump(pump, key)
            pump.request()
            wait_for(lambda: len(reader.earlier_calls) >= 2)
            self.assertEqual(set(reader.earlier_calls), {False})
            self.assertTrue(pump.load_earlier())
            wait_for(lambda: True in reader.earlier_calls)
            self.assertEqual(reader.earlier_calls.count(True), 1)
            pump.request()
            time.sleep(0.2)
        self.assertEqual(reader.earlier_calls.count(True), 1)
        pump.stop()

    def test_an_output_failure_leaves_tabs_ledger_overview_and_sources_intact(self):
        class Broken(OutputReader):
            def read(inner, *args, **kwargs):
                raise PermissionError("nas down")

        with ef.fixture_env(self.fx):
            result = collect(self.fx, request_id=self.b6, stage_id="test", output=Broken())
        self.assertEqual(len(result.tabs), 9)
        self.assertEqual([row.route_node for row in result.ledger], ["plan", "execute", "impl-review", "test", "report"])
        self.assertEqual(result.header.total, 9)
        self.assertEqual(result.output, ())
        self.assertEqual(result.output_health.state, "failed")
        self.assertEqual(result.output_health.error, "PermissionError")
        self.assertEqual(result.source_health("output").state, "failed")
        self.assertEqual(result.health.state, "partial")
        self.assertIn("output: PermissionError", result.health.error)
        self.assertIsNotNone(result.obligations)

    def test_a_failed_output_is_drawn_as_a_status_row_beside_the_untouched_ledger(self):
        from tools.fleet.explorer.frame import compose
        from tools.fleet.explorer.state import ExplorerState
        class Broken(OutputReader):
            def read(inner, *args, **kwargs):
                raise PermissionError("nas down")

        with ef.fixture_env(self.fx):
            result = collect(self.fx, request_id=self.b6, stage_id="test", output=Broken())
        state = ExplorerState(tab=self.b6, stage="test", tab_order=tuple(t.identity for t in result.tabs))
        frame = compose(self.fx.snapshot, result, state, 168, 45, now=self.fx.now)
        text = "\n".join(frame.lines)
        self.assertIn("test        CX", text)                       # the ledger is still there
        self.assertIn("CL 5h29%", text)                             # so is the usage gauge
        self.assertEqual(frame.errors, ())
        self.assertIn("성공 기록 없음 · PermissionError", text)


class Lineage(unittest.TestCase):
    """The earlier generations of the focused main session, read from the seat's own records."""

    SID_OLD, SID_NEW = "sid-old-0001", "sid-new-0002"

    def setUp(self):
        import sys
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cwd = root / "project"
        self.cwd.mkdir()
        env = {"XDG_STATE_HOME": str(root / "state"), "HOME": str(root / "home"), "MEM_STORE": str(root / "mem"),
               "HERDR_PANE_ID": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        utilities = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.realpath(__file__))))), "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        import session_tidy
        self.st = session_tidy
        self.seat = session_tidy.resolve_seat("claude", str(self.cwd), env={}, sid=self.SID_NEW)
        self.transcript = root / "previous.jsonl"
        with self.transcript.open("w", encoding="utf-8") as handle:
            for record in ({"type": "user", "message": {"role": "user", "content": "지난 질문"}},
                           {"type": "assistant", "message": {"role": "assistant",
                                                              "content": [{"type": "text", "text": "지난 답"}]}}):
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.snapshot = SimpleNamespace(sessions=[{"harness": "claude", "session_id": self.SID_NEW,
                                                   "cwd": str(self.cwd), "liveness": "working"}], jobs=[])
        self.jobs = root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")

    def two_generations(self):
        st, seat = self.st, self.seat
        st.record_event(seat, "claude", self.SID_OLD, "start", source="startup", transcript=str(self.transcript),
                        cwd=str(self.cwd), now=100)
        author = dataclasses.replace(seat, sid=self.SID_OLD)          # a project seat keeps one card per author
        st.write_card(author, "claude", self.SID_OLD, "지난 일 정리\n세부 내용", cwd=str(self.cwd), now=101)
        st.record_event(seat, "claude", self.SID_NEW, "start", source="clear", cwd=str(self.cwd), now=200)
        st._append_ledger_entry(seat, {"ts": 201, "event": "handover", "harness": "claude", "sid": self.SID_NEW,
                                       "from": {"harness": "claude", "sid": self.SID_OLD}, "source": "clear"})

    def collect(self, expanded=("claude:sid-new-0002",), generation="", **options):
        request = FocusRequest(FocusKey("claude", self.SID_NEW, generation=generation), 1, 1, tuple(expanded))
        return focus_module.collect_focus(request, jobs_path=str(self.jobs), artifact_root=self.temp.name,
                                          snapshot=self.snapshot, state_roots=[], **options)

    def test_two_generations_give_one_earlier_conversation_with_its_card_title_and_source(self):
        self.two_generations()
        result = self.collect()
        self.assertEqual(result.lineage_health.state, "success")
        self.assertEqual(len(result.lineage), 1)
        earlier = result.lineage[0]
        self.assertEqual((earlier["harness"], earlier["sid"], earlier["source"]), ("claude", self.SID_OLD, "startup"))
        self.assertEqual(earlier["title"], "지난 일 정리")
        self.assertEqual(earlier["transcript"], {"path": str(self.transcript), "status": "available"})

    def test_the_navigation_shows_the_entrance_and_then_the_generation_row(self):
        from tools.fleet.explorer.nav import build_nav
        from tools.fleet.explorer.state import ExplorerState
        self.two_generations()
        result = self.collect()
        closed = build_nav(self.snapshot, result)
        entrance = [row for row in closed if row.kind == "previous-generation"]
        self.assertEqual([row.label for row in entrance], ["이전 대화 1"])
        opened = build_nav(self.snapshot, result, ExplorerState(nav_open=(entrance[0].identity,)))
        rows = [row for row in opened if row.kind == "generation"]
        self.assertEqual([row.label for row in rows], ["CL sid-old- startup 지난 일 정리"])

    def test_selecting_the_generation_shows_its_transcript_tail_in_the_output_pane(self):
        self.two_generations()
        result = self.collect(generation=f"claude:{self.SID_OLD}", output_reader=OutputReader())
        self.assertEqual(result.output_title, "이전 대화 · CL sid-old- · 원래 출력")
        self.assertEqual([(row.text) for row in result.output],
                         ["[이전 sid-old-] CL « 지난 질문", "[이전 sid-old-] CL » 지난 답"])
        self.assertEqual(result.output_health.state, "success")

    def test_a_generation_whose_transcript_is_gone_is_cleaned_not_an_error(self):
        self.two_generations()
        self.transcript.unlink()
        result = self.collect(generation=f"claude:{self.SID_OLD}", output_reader=OutputReader())
        self.assertEqual(result.output_notes, ("원천 정리됨",))
        self.assertEqual(result.output, ())

    def test_a_seat_with_one_generation_has_no_entrance(self):
        self.st.record_event(self.seat, "claude", self.SID_NEW, "start", source="startup", cwd=str(self.cwd), now=200)
        result = self.collect()
        self.assertEqual(result.lineage, ())
        from tools.fleet.explorer.nav import build_nav
        self.assertEqual([r for r in build_nav(self.snapshot, result) if r.kind == "previous-generation"], [])

    def test_an_unreadable_lineage_is_unknown_with_the_reason_and_the_rest_still_reads(self):
        with mock.patch.object(self.st, "seat_lineage", side_effect=OSError("nas")):
            result = self.collect()
        self.assertIsNone(result.lineage)
        self.assertEqual((result.lineage_health.state, result.lineage_health.error), ("failed", "OSError"))
        self.assertEqual(result.health.state, "success")
        from tools.fleet.explorer.nav import build_nav
        labels = [r.label for r in build_nav(self.snapshot, result) if r.kind == "previous-generation"]
        self.assertEqual(labels, ["이전 대화 ? · OSError"])

    def test_a_seat_that_cannot_be_made_says_why(self):
        with mock.patch.object(self.st, "resolve_seat", side_effect=ValueError("no key")):
            result = self.collect()
        self.assertIsNone(result.lineage)
        self.assertEqual(result.lineage_health.error, "seat 없음 · ValueError")

    def test_a_session_that_is_not_expanded_is_never_read(self):
        self.two_generations()
        with mock.patch.object(self.st, "seat_lineage", wraps=self.st.seat_lineage) as read, \
                mock.patch.object(self.st, "resolve_seat", wraps=self.st.resolve_seat) as make:
            hidden = self.collect(expanded=())
            elsewhere = self.collect(expanded=("claude:another-session",))
        self.assertEqual((read.call_count, make.call_count), (0, 0))
        self.assertIsNone(hidden.lineage)
        self.assertIsNone(elsewhere.lineage)
        self.assertIsNone(hidden.lineage_health)

    def test_the_lineage_is_read_on_the_pump_worker_not_by_compose(self):
        from tools.fleet.explorer.frame import compose
        from tools.fleet.explorer.state import ExplorerState
        self.two_generations()
        result = self.collect()
        with mock.patch.object(self.st, "seat_lineage", side_effect=AssertionError("read on draw")), \
                mock.patch.object(self.st, "resolve_seat", side_effect=AssertionError("read on draw")):
            frame = compose(self.snapshot, result, ExplorerState(focus="claude:" + self.SID_NEW), 168, 45)
        self.assertIn("이전 대화 1", "\n".join(frame.lines))


if __name__ == "__main__":
    unittest.main()
