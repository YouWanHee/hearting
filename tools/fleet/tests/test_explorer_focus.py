import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools.fleet import route
from tools.fleet.explorer import focus as focus_module
from tools.fleet.explorer.focus import FocusReader, SelectionPump, status_text
from tools.fleet.explorer.types import FocusKey, FocusResult, Health
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


if __name__ == "__main__":
    unittest.main()
