#!/usr/bin/env python3
"""Real ordinary-runner discovery outages and Fleet duplicate observations.

All state is private. Native delivery and external recovery endpoints are fake;
locks, stores, read passes, discovery, ledger parsing and readiness threads are real.
"""
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(os.environ.get("AGENT_HOME", Path(__file__).resolve().parent.parent)).resolve()
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
spec = importlib.util.spec_from_file_location(
    "runner_observation_fixture", ROOT / "utilities/dispatch_lineage_observation.test.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
peer, steward = fixture.peer, fixture.steward
pending, handover, tidy, dispatch = fixture.pending, fixture.handover, fixture.tidy, fixture.dispatch

for module, relative in ((steward, "utilities/peer-steward.py"),
                         (handover, "utilities/dispatch_seat_handover.py"),
                         (dispatch, "tools/fleet/collectors/dispatch.py"),
                         (tidy, "utilities/session_tidy.py")):
    assert Path(module.__file__).resolve() == ROOT / relative


class RunnerDiscoveryObservationTest(peer._TmpRootMixin, unittest.TestCase):
    notice = peer.SessionNoticeIntegrationCorrectionTest.notice
    edge = peer.SessionNoticeIntegrationCorrectionTest.edge
    store = peer.SessionNoticeIntegrationCorrectionTest.store
    duties = peer.SessionNoticeIntegrationCorrectionTest.duties
    transport = peer.SessionNoticeIntegrationCorrectionTest.transport
    prepared_notice = peer.SessionNoticeIntegrationCorrectionTest.prepared_notice
    pending_message = peer.ObligationStartupTest.pending_message

    def setUp(self):
        super().setUp()
        os.environ["AGENT_HOME"] = str(ROOT)
        handover._LINEAGE_GRAPH_CACHE.clear()

    def exercise(self, failure="read-none", *, legacy=False, service=False,
                 replace_lock=False, historical=False, no_resend=None):
        store, message = self.pending_message()
        if historical or no_resend and no_resend != "fenced":
            notice, ref, _ = self.prepared_notice("claude", "A", "B", "runner-outage")
            transfer = steward.peer_message._read_pending(ref)
            if no_resend in {"queued", "unverified"}:
                steward.peer_message._save_pending(dict(transfer, state=no_resend))
            elif no_resend == "missing":
                steward.peer_message._pending_path(ref).unlink()
        else:
            with contextlib.ExitStack() as stack:
                if legacy:
                    stack.enter_context(mock.patch.object(
                        steward.peer_obligations, "retain_session_notice", return_value=False))
                notice = self.notice(key="runner-outage")
            self.edge("claude", "A", "B")
            if no_resend == "fenced":
                claim = pending.claim(self.tmp_root, "A", notice["delivery_id"],
                    claim_owner="old-native-queue", lease_seconds=1,
                    require_generation_proof=False, jobs=self.jobs_path)
                pending.prepare_submission_fence(self.tmp_root, "A", notice["delivery_id"],
                    claim_owner=claim["claim_owner"],
                    expected_identity=pending.submission_fence_identity(claim, "B"))
        self.assertEqual(len(self.duties()), 0 if legacy else 1)
        other = tidy.Seat("pane", "unrelated-valid-seat")
        tidy._append_ledger_entry(other, dict(event="start", harness="claude", sid="other"))
        failed_path = tidy._ledger_path(other)
        directory = failed_path.parent
        ledger_before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
        notice_before = pending.read(self.tmp_root, "A", notice["delivery_id"])
        identity = pending.received_ack_identity(notice_before, "A", notice["delivery_id"])
        handover._LINEAGE_GRAPH_CACHE.clear()  # Cold graph; writer retention used the healthy generation.
        unavailable = True
        ordinary_passes, discoveries, messages, delays, workers, contexts = [], [], [], [], [], []
        watch_calls, batch_calls, external_passes, read_contexts = [], [], [], []
        started = threading.Event()
        real_read, real_stat, real_lstat, real_iterdir = tidy.read_bytes, Path.stat, Path.lstat, Path.iterdir
        real_begin = steward.peer_obligations.ObligationStore.begin_read_pass
        real_discovery, real_readiness = steward._discover_session_notice_duties, steward._runner_readiness_loop
        real_reconcile = steward._reconcile_obligations
        context_type = steward.dispatch_batch_obligations.OwnerSettlementReadContext
        main_thread = threading.current_thread()
        lock_path = store.root / steward.peer_obligations.RUNNER_LOCK_NAME

        def read(path):
            if unavailable and path == failed_path:
                if failure == "read-none":
                    return None
                if failure == "truncated":
                    return b'{"event":"succession",'
                if failure == "relation":
                    return b'{"event":"succession","source":"tidy-clear","sid":"B"}\n'
            return real_read(path)

        def stat(path, *args, **kwargs):
            if unavailable and failure == "directory-stat" and path == directory:
                raise PermissionError("fixture lineage directory stat")
            return real_stat(path, *args, **kwargs)

        def lstat(path, *args, **kwargs):
            if unavailable and failure == "ledger-stat" and path == failed_path:
                raise PermissionError("fixture one ledger stat")
            return real_lstat(path, *args, **kwargs)

        def iterdir(path):
            if unavailable and failure == "enumeration" and path == directory:
                raise PermissionError("fixture lineage enumeration")
            return real_iterdir(path)

        def begin(current):
            real_begin(current)
            if threading.current_thread() is main_thread:
                ordinary_passes.append(current._active_list_context)

        def discover(current):
            observed = real_discovery(current)
            discoveries.append(observed)
            return observed

        def readiness(root, fd, incarnation, stop):
            workers.append((threading.current_thread(), incarnation, stop))
            started.set()
            real_readiness(root, fd, incarnation, stop)

        def make_context():
            context = context_type()
            context.close = mock.Mock(wraps=context.close)
            contexts.append(context)
            return context

        def resume_message(row, current, **kwargs):
            messages.append(row["id"])
            current.update(row["id"], state="complete", cleanup="complete")

        def reconcile(current, duties, **kwargs):
            read_contexts.append(kwargs["read_context"])
            real_reconcile(current, duties, **kwargs)

        def snapshot():
            self.assertTrue(started.wait(3), "existing readiness worker started")
            external_passes.append(len(external_passes) + 1)
            active = not legacy and len(external_passes) < 4
            return {"watch_root": self.tmp_root / "watch", "jobs_stamps": {},
                    "watches": (("watch-external", {}),) if active else (),
                    "batches": {"signature": (("batch-external",),) if active else ()}}

        def service_exec(fd, root, current, **kwargs):
            self.assertTrue(all(stop.is_set() and not thread.is_alive()
                                for thread, _, stop in workers))
            contexts[-1].close.assert_called_once()
            self.assertTrue(steward._runner_lock_current(store, fd))
            self.assertEqual(kwargs["herdr_session"], "runner-endpoint")
            started.clear()
            return False  # The normal unchanged-service path returns to this runner.

        def receive_exact():
            duty = self.duties()[0]
            text = steward.peer_message._read_pending(duty["observation"]["transfer_ref"])["text"]
            steward.peer_message.receive_peer_message(text, {"harness": "claude", "session_id": "B"})

        def wait(delay):
            nonlocal unavailable
            delays.append(delay)
            self.assertLessEqual(len(delays), 3, "ordinary runner must make progress or honor replacement")
            self.assertEqual(steward._HERDR_SESSION, "runner-endpoint")
            # The same runner holds its actual lock throughout uncertain observation.
            with lock_path.open("r+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(store.get(message["id"])["state"], "complete")
            if len(delays) <= 2:
                native.assert_not_called()
                self.assertEqual(handover._LINEAGE_GRAPH_CACHE, {})
            if historical and len(delays) == 1:
                receive_exact()  # Exact historical receipt settles while authority is unknown.
            if len(delays) == 2:
                if historical:
                    self.assertEqual(pending.read(self.tmp_root, "A", notice["delivery_id"])["state"], "acked")
                unavailable = False  # No ledger bytes, size or mtime change.
            if replace_lock or no_resend and len(delays) == 3:
                lock_path.unlink()  # Private replacement exercises the existing runner exit.
                lock_path.touch()
            elif len(delays) == 3:
                receive_exact()

        try:
            with lock_path.open("w") as lock, self.transport("B") as native, contextlib.ExitStack() as stack:
                for patch in (
                    mock.patch.object(tidy, "read_bytes", side_effect=read),
                    mock.patch.object(Path, "stat", new=stat),
                    mock.patch.object(Path, "lstat", new=lstat),
                    mock.patch.object(Path, "iterdir", new=iterdir),
                    mock.patch.object(steward.peer_obligations.ObligationStore, "begin_read_pass", new=begin),
                    mock.patch.object(steward, "_discover_session_notice_duties", side_effect=discover),
                    mock.patch.object(steward, "_reconcile_obligations", side_effect=reconcile),
                    mock.patch.object(steward, "_runner_readiness_loop", side_effect=readiness),
                    mock.patch.object(steward, "_resume_message_obligation", side_effect=resume_message),
                    mock.patch.object(steward, "_external_obligation_snapshot", side_effect=snapshot),
                    mock.patch.object(steward, "_external_obligation_snapshot_unchanged", return_value=True),
                    mock.patch.object(steward, "_ensure_watch_observers", side_effect=lambda **kw: watch_calls.append(kw)),
                    mock.patch.object(steward.dispatch_batch_obligations, "ensure_observers",
                                      side_effect=lambda **kw: batch_calls.append(kw)),
                    mock.patch.object(steward.dispatch_batch_obligations, "OwnerSettlementReadContext",
                                      side_effect=make_context),
                    mock.patch.object(steward, "_publish_watch_readiness"),
                    mock.patch.object(steward, "_runner_readiness_fds", return_value=[]),
                    mock.patch.object(steward, "_HERDR_SESSION", "runner-endpoint"),
                    mock.patch.object(steward.time, "sleep", side_effect=wait),
                ):
                    stack.enter_context(patch)
                execute = stack.enter_context(mock.patch.object(
                    steward.peer_obligations, "exec_service_runner", side_effect=service_exec))
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertEqual(steward.cmd_obligation_runner(SimpleNamespace(
                    state_root=self.tmp_root, lock_fd=lock.fileno(),
                    service_current="/fixture/current" if service else None)), 0)
                self.assertEqual(native.call_count, 0 if historical or no_resend or replace_lock else 1)
                self.assertEqual(execute.call_count, len(discoveries) if service else 0)
                self.assertTrue(all(not thread.is_alive() and stop.is_set() for thread, _, stop in workers))
                with lock_path.open("r+") as contender:
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            # Test-only cleanup if the subject aborts; the assertions above still fail.
            for thread, _, stop in workers:
                stop.set()
                thread.join(3)
        self.assertEqual(messages, [message["id"]])
        self.assertEqual(discoveries[:1 if replace_lock else 2], [False] * (1 if replace_lock else 2))
        self.assertEqual(delays[:1 if replace_lock else 2], [1.0] if replace_lock else [1.0, 1.7])
        self.assertEqual(len(ordinary_passes), len(discoveries))
        self.assertEqual(len({id(context) for context in ordinary_passes}), len(discoveries))
        self.assertEqual(len({id(context) for context in read_contexts}), len(discoveries))
        self.assertEqual(len(batch_calls), len(discoveries))
        self.assertEqual(len(watch_calls), min(3, len(discoveries)) if not legacy else 0)
        self.assertEqual(len(workers), len(discoveries) + 1 if service else 1)
        self.assertEqual(len({incarnation for _, incarnation, _ in workers}), 1)
        marker = json.loads((store.root / "readiness-v1/runner.json").read_text())
        self.assertEqual(marker["incarnation"], workers[0][1])
        self.assertEqual(marker["pid"], os.getpid())
        for context in contexts:
            context.close.assert_called_once()
        self.assertEqual(ledger_before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in ledger_before})
        final = pending.read(self.tmp_root, "A", notice["delivery_id"])
        self.assertEqual(pending.received_ack_identity(final, "A", notice["delivery_id"]), identity)
        if replace_lock or no_resend:
            self.assertEqual(final, notice_before)
            self.assertTrue(self.duties(), "replacement never settles the retained notice")
        else:
            self.assertEqual(final["state"], "acked")
            self.assertFalse(self.duties())
            self.assertTrue(all(discoveries[2:]))
        print(json.dumps(dict(failure=failure, legacy=legacy, service=service, historical=historical,
            no_resend=no_resend, replace_lock=replace_lock, discovery=discoveries, delays=delays,
            ordinary_passes=len(ordinary_passes), readiness_workers=len(workers),
            external_passes=len(external_passes), ledger_unchanged=True, final_state=final["state"])))

    def test_read_none_among_usable_ledgers(self):
        self.exercise()

    def test_truncated_relation_among_usable_ledgers(self):
        self.exercise("truncated")

    def test_malformed_relation_among_usable_ledgers(self):
        self.exercise("relation")

    def test_directory_stat_failure(self):
        self.exercise("directory-stat")

    def test_one_ledger_stat_failure(self):
        self.exercise("ledger-stat")

    def test_directory_enumeration_failure(self):
        self.exercise("enumeration")

    def test_unknown_discovery_keeps_legacy_notice_without_a_retained_duty(self):
        self.exercise(legacy=True)

    def test_service_replacement_joins_readiness_and_closes_contexts(self):
        self.exercise(service=True)

    def test_replaced_runner_lock_stops_during_unknown_discovery(self):
        self.exercise(replace_lock=True)

    def test_historical_exact_receipt_settles_during_discovery_outage(self):
        self.exercise(historical=True)

    def test_prepared_notice_is_not_resent(self):
        self.exercise(no_resend="pending")

    def test_queued_notice_is_not_resent(self):
        self.exercise(no_resend="queued")

    def test_ambiguous_notice_is_not_resent(self):
        self.exercise(no_resend="unverified")

    def test_missing_prepared_notice_is_not_resent(self):
        self.exercise(no_resend="missing")

    def test_fenced_notice_is_not_resent(self):
        self.exercise(no_resend="fenced")


class FleetDuplicateObservationTest(peer._TmpRootMixin, unittest.TestCase):
    notice = fixture.ObservationTest.notice
    edge = fixture.ObservationTest.edge
    handoff = fixture.ObservationTest.handoff
    registered_record = fixture.ObservationTest.registered_record
    snapshot = fixture.ObservationTest.snapshot

    def setUp(self):
        super().setUp()
        os.environ["AGENT_HOME"] = str(ROOT)
        handover._LINEAGE_GRAPH_CACHE.clear()

    def assert_both_orders(self, record, storage, duplicate, expected, *, live=()):
        first = pending.record_path(self.tmp_root, storage, record["delivery_id"])
        other = self.tmp_root / "second-root"
        other.mkdir()
        jobs = other / "jobs.log"
        jobs.touch()
        second = pending.record_path(other, storage, record["delivery_id"])
        second.parent.mkdir(parents=True)
        second.write_text(json.dumps(duplicate))
        expired = first.parent / "legacy-expired.json"
        expired.write_text('{"state":"expired"}')
        retained = other / "supervisor-state/obligations/batch-retained.json"
        retained.parent.mkdir(parents=True)
        retained.write_text('{"state":"unknown"}')
        paths = (first, second, expired, retained, self.jobs_path, jobs)
        before = {p: self.snapshot(p) for p in paths}
        results = []
        for roots in ([self.jobs_path, jobs], [jobs, self.jobs_path]):
            counts = dispatch._pending_delivery_counts([str(p) for p in roots], session_observation={
                "complete": True, "herdr_complete": True, "process_complete": True,
                "native_sid_complete": True, "live": set(live)})
            self.assertEqual(counts, dict(pending=2, expired=1, retained_batch_duties=1,
                                          no_recipient=expected))
            results.append(counts)
        self.assertEqual(before, {p: self.snapshot(p) for p in paths})
        print(json.dumps(dict(orders=results, bytes_mtime_unchanged=True)))

    def dead_notice(self):
        record = self.notice(sid="dead-A")
        tidy._append_ledger_entry(tidy.Seat("pane", "dead-A"),
                                 dict(event="start", harness="claude", sid="dead-A"))
        return record

    def test_unresolved_duplicate_body_is_unknown_in_both_orders(self):
        record = self.dead_notice()
        duplicate = dict(record, receipt=None)
        self.assertIsNone(handover.record_recipient_binding(duplicate, "dead-A", harness="claude"))
        self.assert_both_orders(record, "dead-A", duplicate, None)

    def test_malformed_notice_body_is_unknown_in_both_orders(self):
        record = self.dead_notice()
        duplicate = dict(record, receipt={"kind": "notice"})
        self.assertIsNone(handover.record_recipient_binding(duplicate, "dead-A", harness="claude"))
        self.assert_both_orders(record, "dead-A", duplicate, None)

    def test_valid_identical_dead_duplicates_count_once(self):
        record = self.dead_notice()
        self.assert_both_orders(record, "dead-A", record, 1)

    def test_valid_identical_live_duplicates_keep_known_zero(self):
        record = self.dead_notice()
        self.assert_both_orders(record, "dead-A", record, 0, live={('claude', 'dead-A')})

    def conflicting_registered(self, *, different_harness):
        first, record, _ = self.registered_record("duplicate", "codex")
        second = dict(first, attempt_id="att-" + "e" * 32, route_id="rt-" + "e" * 16)
        with self.jobs_path.open("a") as stream:
            stream.write("now\trunning\t/w\t/w\ttask\t" +
                         ",".join(k + "=" + v for k, v in second.items()) + "\n")
        target_harness = "opencode" if different_harness else "codex"
        target_sid = "duplicate-Z" if different_harness else "different-holder"
        tidy._append_ledger_entry(tidy.Seat("pane", "different-binding"), dict(
            event="handover", source="retire", harness=target_harness, sid=target_sid,
            **{"from": "duplicate-B"}, ts=3, bindings=[handover.binding_of(self.jobs_path, second)]))
        duplicate = dict(record, attempt_ids=[second["attempt_id"]])
        self.assertEqual(handover.record_recipient_binding(record, "duplicate-A", harness="claude"),
                         ("codex", "duplicate-Z"))
        self.assertEqual(handover.record_recipient_binding(duplicate, "duplicate-A", harness="claude"),
                         (target_harness, target_sid))
        return first, second, record, duplicate

    def test_distinct_valid_sid_bindings_for_one_identity_stay_unknown(self):
        _, _, record, duplicate = self.conflicting_registered(different_harness=False)
        self.assert_both_orders(record, "duplicate-A", duplicate, None,
                                live={("codex", "duplicate-Z")})

    def test_same_sid_on_distinct_valid_harnesses_stays_unknown(self):
        _, _, record, duplicate = self.conflicting_registered(different_harness=True)
        self.assert_both_orders(record, "duplicate-A", duplicate, None,
                                live={("codex", "duplicate-Z"), ("opencode", "duplicate-Z")})

    def test_mixed_aggregate_duplicate_stays_unknown(self):
        first, second, record, _ = self.conflicting_registered(different_harness=True)
        duplicate = dict(record, attempt_ids=[first["attempt_id"], second["attempt_id"]])
        self.assertIsNone(handover.record_recipient_binding(duplicate, "duplicate-A", harness="claude"))
        self.assert_both_orders(record, "duplicate-A", duplicate, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
