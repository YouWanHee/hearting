#!/usr/bin/env python3
"""Incomplete authority observations and Fleet's current-recipient join.

Uses real ledger, pending-record, courier and preparation paths; native I/O is fake.
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(os.environ.get("AGENT_HOME", Path(__file__).resolve().parent.parent)).resolve()
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]


def fixture(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


peer = fixture("lineage_peer_fixture", "utilities/peer_steward.test.py")
steward = peer.peer_steward
import dispatch_pending_delivery as pending
import dispatch_seat_handover as handover
import dispatch_session_sweep as sweep
import session_tidy as tidy
from peer_read_context import PeerReadContext
from fleet.collectors import dispatch

for module, relative in ((handover, "utilities/dispatch_seat_handover.py"),
                         (tidy, "utilities/session_tidy.py"),
                         (steward, "utilities/peer-steward.py"),
                         (dispatch, "tools/fleet/collectors/dispatch.py")):
    assert Path(module.__file__).resolve() == ROOT / relative


class ObservationTest(peer._TmpRootMixin, unittest.TestCase):
    notice = peer.SessionNoticeIntegrationCorrectionTest.notice
    edge = peer.SessionNoticeIntegrationCorrectionTest.edge
    store = peer.SessionNoticeIntegrationCorrectionTest.store
    duties = peer.SessionNoticeIntegrationCorrectionTest.duties
    transport = peer.SessionNoticeIntegrationCorrectionTest.transport
    prepared_notice = peer.SessionNoticeIntegrationCorrectionTest.prepared_notice
    handoff = peer.ExplicitHolderAuthorityTest.handoff
    ambiguity = peer.ExplicitHolderAuthorityTest.ambiguity

    def setUp(self):
        super().setUp()
        os.environ["AGENT_HOME"] = str(ROOT)
        handover._LINEAGE_GRAPH_CACHE.clear()

    @contextlib.contextmanager
    def unreadable(self, path, *, enabled=lambda: True):
        real_open = os.open

        def open_file(candidate, *args, **kwargs):
            if Path(candidate) == path and enabled():
                raise PermissionError("fixture ledger unreadable")
            return real_open(candidate, *args, **kwargs)

        with mock.patch.object(tidy.os, "open", side_effect=open_file):
            yield

    def counts(self, live=()):
        return dispatch._pending_delivery_counts([str(self.jobs_path)], session_observation={
            "complete": True, "herdr_complete": True, "process_complete": True,
            "native_sid_complete": True, "live": set(live)})

    def snapshot(self, path):
        return path.read_bytes(), path.stat().st_mtime_ns

    def test_failed_one_of_two_ledgers_is_not_cached_and_same_context_recovers(self):
        notice = self.notice()
        path = tidy._ledger_path(self.edge("claude", "A", "B"))
        other = tidy.Seat("pane", "other-valid")
        tidy._append_ledger_entry(other, dict(event="start", harness="claude", sid="other", ts=1))
        before = self.snapshot(path)
        context = PeerReadContext()
        handover._LINEAGE_GRAPH_CACHE.clear()  # Cold observation after writer-side retention.
        with self.unreadable(path):
            for _ in range(2):
                self.assertIsNone(handover.record_recipient(notice, "A", harness="claude",
                                                            read_context=context))
            self.assertEqual(handover._LINEAGE_GRAPH_CACHE, {})
            self.assertEqual(context._lineage_graphs, {})
            self.assertIsNone(self.counts()["no_recipient"])
        with mock.patch.object(tidy, "_read_ledger_lines", wraps=tidy._read_ledger_lines) as reads:
            for _ in range(10):
                self.assertEqual(handover.record_recipient(notice, "A", harness="claude",
                                                           read_context=context), "B")
            self.assertEqual(reads.call_count, 2)
        self.assertEqual(self.snapshot(path), before)
        with self.transport("A") as native:
            steward._resume_session_notice_obligations(self.duties(), self.store())
        native.assert_not_called()
        with self.transport("B") as native:
            steward._resume_session_notice_obligations(self.duties(), self.store())
        native.assert_called_once()

    def test_healthy_337_ledgers_are_read_once_for_100_resolutions(self):
        notice = self.notice()
        directory = tidy.state_root() / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(337):
            rows = [dict(event="start", harness="claude", sid=f"legacy-{index}-{n}")
                    for n in range(37)]
            rows.append(dict(event="succession", source="tidy-clear", harness="claude",
                             sid="B" if index == 0 else f"new-{index}",
                             **{"from": "A" if index == 0 else f"old-{index}"}))
            (directory / f"seat-{index}.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows))
        context = PeerReadContext()
        started = time.monotonic()
        with mock.patch.object(tidy, "_read_ledger_lines", wraps=tidy._read_ledger_lines) as reads:
            for _ in range(100):
                self.assertEqual(handover.record_recipient(notice, "A", harness="claude",
                                                           read_context=context), "B")
            self.assertEqual(reads.call_count, 337)
        print(json.dumps({"ledgers": 337, "rows": 337 * 38, "resolutions": 100,
                          "reads": reads.call_count, "elapsed": time.monotonic() - started}))

    def test_absent_and_empty_legacy_state_differ_from_failed_stat_and_enumeration(self):
        notice = self.notice()
        self.assertEqual(handover.record_recipient(notice, "A", harness="claude"), "A")
        empty = tidy._ledger_path(tidy.Seat("pane", "empty-legacy"))
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_bytes(b"")
        self.assertEqual(handover.record_recipient(notice, "A", harness="claude"), "A")
        path = tidy._ledger_path(self.edge("claude", "A", "B"))
        directory = path.parent
        before = self.snapshot(path)
        for method, target in (("stat", directory), ("iterdir", directory), ("lstat", path)):
            real = getattr(Path, method)

            def fail(candidate, *args, **kwargs):
                if candidate == target:
                    raise PermissionError("fixture " + method)
                return real(candidate, *args, **kwargs)

            context = PeerReadContext()
            with self.subTest(method=method), mock.patch.object(Path, method, new=fail):
                self.assertIsNone(handover.record_recipient(notice, "A", harness="claude",
                                                            read_context=context))
                self.assertIsNone(self.counts()["no_recipient"])
            self.assertEqual(handover.record_recipient(notice, "A", harness="claude",
                                                       read_context=context), "B")
            self.assertEqual(self.snapshot(path), before)
        legacy = tidy.Seat("pane", "unrelated-legacy")
        tidy._append_ledger_entry(legacy, {"old-field": "no SID or event"})
        self.assertEqual(handover.record_recipient(notice, "A", harness="claude"), "B")

    def test_malformed_relation_observations_retry_without_any_file_change(self):
        notice = self.notice()
        path = tidy._ledger_path(self.edge("claude", "A", "B"))
        before = self.snapshot(path)
        relation = dict(event="succession", source="tidy-clear", harness="claude",
                        sid="B", **{"from": {"harness": "claude", "sid": "A"}})
        bad_rows = [b'{"event":"succession",', b'[]\n', b'\xff\n']
        for field in ("sid", "harness", "from", "source"):
            row = dict(relation)
            del row[field]
            bad_rows.append(json.dumps(row).encode())
        bad_rows.append(json.dumps(dict(relation, source={})).encode())
        real_read = tidy.read_bytes
        for bad in bad_rows:
            context = PeerReadContext()
            handover._LINEAGE_GRAPH_CACHE.clear()
            with self.subTest(bad=bad), mock.patch.object(tidy, "read_bytes",
                    side_effect=lambda candidate: bad if candidate == path else real_read(candidate)):
                self.assertIsNone(handover.record_recipient(notice, "A", harness="claude",
                                                            read_context=context))
            self.assertEqual(handover.record_recipient(notice, "A", harness="claude",
                                                       read_context=context), "B")
            self.assertEqual(self.snapshot(path), before)

    def test_unavailable_registered_explicit_holder_denies_authority_and_sweeps(self):
        import route_authority
        for harness, recipient_harness in (("claude", "claude"), ("codex", "codex"),
                                           ("opencode", "opencode"), ("claude", "codex")):
            tag = harness + recipient_harness
            meta, record, seat = self.handoff(tag, harness, recipient_harness)
            self.edge(recipient_harness, tag + "-Z", tag + "-Y")
            path = tidy._ledger_path(seat)
            before = self.snapshot(path)
            context = PeerReadContext()
            handover._LINEAGE_GRAPH_CACHE.clear()
            with self.subTest(harness=harness, recipient_harness=recipient_harness), self.unreadable(path):
                self.assertIsNone(handover.record_recipient(record, tag + "-A", harness=harness,
                                                            read_context=context))
                self.assertEqual(handover.effective_parent(meta, self.jobs_path, read_context=context), "")
                for sid in (tag + "-A", tag + "-B", tag + "-Z", tag + "-Y"):
                    self.assertFalse(handover.owns(meta, sid, self.jobs_path, read_context=context))
                    self.assertFalse(route_authority.decide_continuation(action="handover",
                        jobs=self.jobs_path, metadata=meta, status="running",
                        facts={"eligible": True, "actor": sid}).allowed)
            self.assertEqual(handover.record_recipient(record, tag + "-A", harness=harness,
                                                       read_context=context), tag + "-Y")
            self.assertEqual(self.snapshot(path), before)
        notice = self.notice()
        path = tidy._ledger_path(self.edge("claude", "A", "B"))
        handover._LINEAGE_GRAPH_CACHE.clear()
        with self.unreadable(path):
            for sid in ("A", "B"):
                self.assertEqual(sweep.sweep_deliver(self.tmp_root, "claude-parent-runtime", sid)[0], [])
        self.assertEqual(pending.read(self.tmp_root, "A", notice["delivery_id"])["state"], "pending")

    def test_unbound_registered_parent_stays_unresolved_during_read_failure(self):
        meta = dict(attempt_id="att-" + "a" * 32, parent_sid="A", parent_harness="claude",
                    dispatch_depth="1", worker_type="frame", route_id="rt-" + "a" * 16,
                    route_hash="sha256:" + "b" * 64, route_node="execute")
        self.jobs_path.write_text("now\trunning\t/w\t/w\ttask\t" +
                                 ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        record = {"attempt_ids": [meta["attempt_id"]]}
        path = tidy._ledger_path(self.edge("claude", "A", "B"))
        context = PeerReadContext()
        with self.unreadable(path):
            self.assertIsNone(handover.record_recipient(record, "A", harness="claude", read_context=context))
            self.assertEqual(handover.effective_parent(meta, self.jobs_path, read_context=context), "")
            for sid in ("A", "B"):
                self.assertFalse(handover.owns(meta, sid, self.jobs_path, read_context=context))
        self.assertEqual(handover.record_recipient(record, "A", harness="claude", read_context=context), "B")

    def test_real_preparation_and_last_native_guard_refuse_unavailable_lineage(self):
        for boundary in ("readiness", "last-guard"):
            old, current = boundary + "-A", boundary + "-B"
            notice = self.notice(sid=old, key=old)
            path = tidy._ledger_path(self.edge("claude", old, current))
            failed = False
            calls = []

            def outage():
                nonlocal failed
                failed = True
                handover._LINEAGE_GRAPH_CACHE.clear()

            def readiness(*args, **kwargs):
                if boundary == "readiness":
                    outage()
                return SimpleNamespace(state="ready")

            def guard(*args, **kwargs):
                calls.append(True)
                if boundary == "last-guard" and len(calls) == 2:
                    outage()
                return None, {}

            with self.subTest(boundary=boundary), self.unreadable(path, enabled=lambda: failed), \
                    self.transport(current, readiness=readiness) as native, \
                    mock.patch.object(steward, "_claude_idle_prompt_guard", side_effect=guard):
                steward._resume_session_notice_obligations(self.duties(), self.store())
            self.assertTrue(failed)
            native.assert_not_called()
            row = pending.read(self.tmp_root, old, notice["delivery_id"])
            self.assertEqual(row["state"], "sent-ambiguous" if boundary == "last-guard" else "pending")

    def test_deferred_begin_write_and_codex_before_add_refuse_then_recover(self):
        import codex_queue_delivery
        import codex_queue_dispatch
        for harness in ("claude", "opencode", "codex"):
            old, current = harness + "-A", harness + "-B"
            notice, ref, recipient = self.prepared_notice(harness, old, current, old)
            path = tidy._ledger_path(self.edge(harness, old, current))
            record = pending.read(self.tmp_root, old, notice["delivery_id"])
            if harness != "codex":
                reserved = steward.peer_message.reserve_pending_herdr(ref, recipient)
                handover._LINEAGE_GRAPH_CACHE.clear()
                with self.subTest(harness=harness), self.unreadable(path):
                    self.assertIsNone(steward.peer_message.begin_pending_write(reserved, recipient=recipient))
                self.assertEqual(steward.peer_message.begin_pending_write(reserved, recipient=recipient)["state"],
                                 "unverified")
                self.assertIsNone(steward.peer_message.begin_pending_write(reserved, recipient=recipient))
            else:
                added = []

                def send(*args, **kwargs):
                    if kwargs["allow_add"]:
                        handover._LINEAGE_GRAPH_CACHE.clear()
                        with self.unreadable(path):
                            kwargs["before_add"]()
                        added.append(current)
                    return {"status": "queued"}

                with mock.patch.object(codex_queue_dispatch, "resolve_queue_socket",
                                       return_value=Path("/tmp/fake-queue.sock")), \
                        mock.patch.object(codex_queue_delivery, "send_at_least_once", side_effect=send):
                    result = steward.peer_message.deliver_pending_codex(ref)
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(added, [])
            self.assertEqual(pending.read(self.tmp_root, old, notice["delivery_id"]), record)

    def test_historical_receipt_and_ack_retry_settle_during_current_observer_outage(self):
        for ack_error in (False, True):
            old, current = "ack-A-" + str(ack_error), "ack-B-" + str(ack_error)
            notice = self.notice(sid=old, key=old)
            path = tidy._ledger_path(self.edge("claude", old, current))
            with self.transport(current) as native:
                steward._resume_session_notice_obligations(self.duties(), self.store())
            native.assert_called_once()
            duty = next(d for d in self.duties() if d["intent"]["identity"]["storage_sid"] == old)
            ref = duty["observation"]["transfer_ref"]
            transfer = steward.peer_message._read_pending(ref)
            handover._LINEAGE_GRAPH_CACHE.clear()
            with self.unreadable(path), mock.patch.object(pending, "ack_received",
                    side_effect=OSError("fixture ACK write") if ack_error else None,
                    wraps=None if ack_error else pending.ack_received):
                steward.peer_message.receive_peer_message(transfer["text"],
                    {"harness": "claude", "session_id": current})
            with self.unreadable(path), mock.patch.object(steward, "cmd_prompt") as send:
                steward._resume_session_notice_obligations(self.duties(), self.store())
            send.assert_not_called()
            self.assertEqual(pending.read(self.tmp_root, old, notice["delivery_id"])["state"], "acked")

    def test_prepared_queued_ambiguous_missing_and_fenced_transfers_are_not_resent_during_outage(self):
        for state in ("pending", "queued", "unverified", "missing", "fenced"):
            old, current = state + "-A", state + "-B"
            if state == "fenced":
                notice = self.notice(sid=old, key=old)
                row = pending.claim(self.tmp_root, old, notice["delivery_id"], claim_owner="old-queue",
                    lease_seconds=1, require_generation_proof=False, jobs=self.jobs_path)
                identity = pending.submission_fence_identity(row, old)
                pending.prepare_submission_fence(self.tmp_root, old, notice["delivery_id"],
                    claim_owner=row["claim_owner"], expected_identity=identity)
            else:
                notice, ref, _recipient = self.prepared_notice("claude", old, current, old)
                transfer = steward.peer_message._read_pending(ref)
                if state in {"queued", "unverified"}:
                    steward.peer_message._save_pending(dict(transfer, state=state))
                elif state == "missing":
                    steward.peer_message._pending_path(ref).unlink()
            path = tidy._ledger_path(self.edge("claude", old, current))
            handover._LINEAGE_GRAPH_CACHE.clear()
            before = pending.read(self.tmp_root, old, notice["delivery_id"])
            with self.subTest(state=state), self.unreadable(path), self.transport(current) as native:
                steward._resume_session_notice_obligations(self.duties(), self.store())
            native.assert_not_called()
            self.assertEqual(pending.read(self.tmp_root, old, notice["delivery_id"]), before)

    def test_pending_record_failures_invalidate_mixed_counts_and_recover(self):
        notices = [self.notice(sid=sid, key=sid) for sid in ("dead-A", "dead-B")]
        for sid in ("dead-A", "dead-B"):
            tidy._append_ledger_entry(tidy.Seat("pane", sid), dict(event="start", harness="claude", sid=sid))
        paths = [pending.record_path(self.tmp_root, sid, notice["delivery_id"])
                 for sid, notice in zip(("dead-A", "dead-B"), notices)]
        before = [self.snapshot(path) for path in paths]
        self.assertEqual(self.counts()["no_recipient"], 2)
        real_read = Path.read_text
        for bad in (PermissionError("fixture pending read"), "not-json", "[]", "{}",
                    '{"state":[]}', '{"state":"unrecognized"}'):
            def read(path, *args, **kwargs):
                if path == paths[0]:
                    if isinstance(bad, Exception):
                        raise bad
                    return bad
                return real_read(path, *args, **kwargs)

            with self.subTest(bad=str(bad)), mock.patch.object(Path, "read_text", new=read):
                counts = self.counts()
                self.assertIsNone(counts["no_recipient"])
                self.assertEqual(counts["pending"], 1)
            self.assertEqual(self.counts()["no_recipient"], 2)
            self.assertEqual([self.snapshot(path) for path in paths], before)

    def test_legacy_terminal_rows_need_only_the_existing_state_discriminator(self):
        notice = self.notice(sid="dead-A")
        path = pending.record_path(self.tmp_root, "dead-A", notice["delivery_id"])
        before = self.snapshot(path)
        real = Path.read_text
        for state in ("acked", "rejected", "expired"):
            def read(candidate, *args, **kwargs):
                return json.dumps({"state": state}) if candidate == path else real(candidate, *args, **kwargs)

            with self.subTest(state=state), mock.patch.object(Path, "read_text", new=read):
                counts = self.counts()
                self.assertEqual(counts, dict(pending=0, expired=int(state == "expired"),
                                             retained_batch_duties=0, no_recipient=0))
            self.assertEqual(self.snapshot(path), before)

    def test_pending_empty_directory_and_failed_enumerations_are_distinct(self):
        root = self.tmp_root / "pending-delivery"
        root.mkdir(exist_ok=True)
        self.assertEqual(self.counts()["no_recipient"], 0)
        notice = self.notice(sid="dead-A")
        tidy._append_ledger_entry(tidy.Seat("pane", "dead-A"), dict(event="start", harness="claude", sid="dead-A"))
        path = pending.record_path(self.tmp_root, "dead-A", notice["delivery_id"])
        before = self.snapshot(path)
        for method, target in (("stat", root), ("iterdir", root), ("stat", path.parent), ("iterdir", path.parent)):
            real = getattr(Path, method)

            def fail(candidate, *args, **kwargs):
                if candidate == target:
                    raise PermissionError("fixture pending enumeration")
                return real(candidate, *args, **kwargs)

            with self.subTest(method=method, target=str(target)), mock.patch.object(Path, method, new=fail):
                self.assertIsNone(self.counts()["no_recipient"])
            self.assertEqual(self.counts()["no_recipient"], 1)
            self.assertEqual(self.snapshot(path), before)

    def test_one_unavailable_pending_directory_keeps_other_legacy_counts(self):
        paths = []
        for sid in ("dead-A", "dead-B"):
            notice = self.notice(sid=sid, key=sid)
            tidy._append_ledger_entry(tidy.Seat("pane", sid), dict(event="start", harness="claude", sid=sid))
            paths.append(pending.record_path(self.tmp_root, sid, notice["delivery_id"]))
        real = Path.stat

        def fail(path, *args, **kwargs):
            if path == paths[0].parent:
                raise PermissionError("fixture one recipient directory")
            return real(path, *args, **kwargs)

        before = [self.snapshot(path) for path in paths]
        with mock.patch.object(Path, "stat", new=fail):
            counts = self.counts()
            self.assertEqual(counts["pending"], 1)
            self.assertIsNone(counts["no_recipient"])
        self.assertEqual(self.counts()["no_recipient"], 2)
        self.assertEqual([self.snapshot(path) for path in paths], before)

    def test_invalid_duplicate_is_observed_before_deduplication(self):
        notice = self.notice(sid="dead-A")
        tidy._append_ledger_entry(tidy.Seat("pane", "dead-A"), dict(event="start", harness="claude", sid="dead-A"))
        path = pending.record_path(self.tmp_root, "dead-A", notice["delivery_id"])
        duplicate = path.parent.parent / "zz-invalid-copy" / path.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(b"not JSON")
        before = [self.snapshot(p) for p in (path, duplicate)]
        self.assertIsNone(self.counts()["no_recipient"])
        self.assertEqual([self.snapshot(p) for p in (path, duplicate)], before)

    def registered_record(self, tag, target_harness):
        meta, _record, seat = self.handoff(tag, "claude", target_harness)
        parent = "att-" + "b" * 32
        receipt = dict(schema_version=2, state="delivered", parent_attempt_id=parent,
            job_registry=str(self.jobs_path), children=[dict(attempt_id=meta["attempt_id"], status="done",
            readiness="ready", required_action="inspect-done-failure", reason="terminal-failure-or-unclosed",
            harness="claude")])
        record = pending.create(root=self.tmp_root, recipient_kind="claude-parent-runtime",
            recipient_key=tag + "-A", delivery_id="delivery-" + meta["attempt_id"][4:],
            session_generation="", session_generation_supported="0", attempt_ids=[meta["attempt_id"]],
            parent_attempt_id=parent, route_id=meta["route_id"], route_node="execute", receipt=receipt,
            receipt_digest=pending._canonical_receipt_digest(receipt), row_revisions={meta["attempt_id"]: "original"})
        return meta, record, seat

    def test_fleet_uses_coherent_current_harness_sid_and_successors(self):
        live, paths = set(), []
        for harness in ("codex", "opencode"):
            tag = "fleet-" + harness
            meta, record, _seat = self.registered_record(tag, harness)
            self.edge(harness, tag + "-Z", tag + "-Y")
            self.assertEqual(handover.record_recipient(record, tag + "-A", harness="claude"), tag + "-Y")
            self.assertEqual(handover.effective_parent_harness(meta, self.jobs_path), harness)
            paths.append(pending.record_path(self.tmp_root, tag + "-A", record["delivery_id"]))
            live.add((harness, tag + "-Y"))
        before = [self.snapshot(path) for path in paths]
        self.assertEqual(self.counts(live)["no_recipient"], 0)
        self.assertEqual(self.counts({("claude", sid) for _, sid in live})["no_recipient"], 2)
        self.assertEqual(self.counts()["no_recipient"], 2)
        self.assertEqual([self.snapshot(path) for path in paths], before)
        self.ambiguity("codex", "fleet-codex-Y", "fork")
        self.assertIsNone(self.counts(live)["no_recipient"])

    def test_same_sid_on_different_harnesses_and_mixed_aggregate(self):
        first, record, _ = self.registered_record("same", "codex")
        second = dict(first, attempt_id="att-" + "e" * 32, route_id="rt-" + "e" * 16)
        with self.jobs_path.open("a") as stream:
            stream.write("now\trunning\t/w\t/w\ttask\t" +
                         ",".join(k + "=" + v for k, v in second.items()) + "\n")
        seat = tidy.Seat("pane", "same-other")
        tidy._append_ledger_entry(seat, dict(event="handover", source="retire", harness="opencode",
            sid="same-Z", **{"from": "same-B"}, ts=3, bindings=[handover.binding_of(self.jobs_path, second)]))
        aggregate = dict(record, attempt_ids=[first["attempt_id"], second["attempt_id"]])
        self.assertIsNone(handover.record_recipient(aggregate, "same-A", harness="claude"))
        self.assertEqual(self.counts({("codex", "same-Z")})["no_recipient"], 0)
        self.assertEqual(self.counts({("opencode", "same-Z")})["no_recipient"], 1)
        path = pending.record_path(self.tmp_root, "same-A", record["delivery_id"])
        path.write_text(json.dumps(aggregate))
        before = self.snapshot(path)
        self.assertIsNone(self.counts({("codex", "same-Z"), ("opencode", "same-Z")})["no_recipient"])
        self.assertEqual(self.snapshot(path), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
