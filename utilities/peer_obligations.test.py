#!/usr/bin/env python3
"""Temporary-root checks for durable peer obligations and native readiness."""

from __future__ import annotations

import fcntl
import importlib.util
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import peer_obligations as obligations
import dispatch_batch_obligations as batch_obligations

_HERE = Path(__file__).resolve().parent
_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "peer_steward_under_obligation_test", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)


class ObligationStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = self.root / "route-jobs.tsv"
        self.jobs.touch()
        self._saved_env = dict(os.environ)
        os.environ.update({
            "HOME": str(self.root / "home"),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "HARNESS_STATE_ROOT": str(self.root / "harness-state"),
            "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-ledger"),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "CODEX_HOME": str(self.root / "home" / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / "home" / ".claude"),
        })
        self.store = obligations.ObligationStore(self.root)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)
        self.temp.cleanup()

    def test_list_reuses_json_generation_and_observes_external_changes(self):
        first = self.store.create("cached-one", "message", {}, {"ref": "a" * 32})
        second = self.store.create("cached-two", "retire", {}, {"target": "pane"})
        self.store.reset_discovery_metrics()

        listed = self.store.list(states={"pending"})
        self.assertEqual({row["id"] for row in listed}, {first["id"], second["id"]})
        listed[0]["intent"]["mutated"] = True
        self.assertEqual(len(self.store.list(states={"pending"})), 2)
        metrics = self.store.discovery_metrics()
        self.assertEqual(metrics["json_reads"], 2)
        self.assertEqual(metrics["json_parses"], 2)
        self.assertGreater(metrics["stat_calls"], 2)

        path = self.store._record_path(first["id"])
        old_mtime = path.stat().st_mtime_ns
        raw = path.read_text(encoding="utf-8")
        self.assertIn('"state":"pending"', raw)
        changed = raw.replace('"state":"pending"', '"state":"unknown"')
        self.assertEqual(len(raw), len(changed))
        path.write_text(changed, encoding="utf-8")
        os.utime(path, ns=(old_mtime, old_mtime))
        self.assertNotIn(first["id"], {row["id"] for row in self.store.list(states={"pending"})})
        self.assertIn(first["id"], {row["id"] for row in self.store.list(states={"unknown"})})
        self.assertNotIn("mutated", self.store.get(first["id"])["intent"])

        peer = obligations.ObligationStore(self.root)
        peer.create("cached-three", "message", {}, {"ref": "c" * 32})
        self.assertIn("cached-three", {row["id"] for row in self.store.list(states={"pending"})})
        self.store._record_path(second["id"]).unlink()
        self.assertNotIn(second["id"], {row["id"] for row in self.store.list(states={"pending"})})

        refreshed = self.store.get(first["id"])
        self.store.update(first["id"], state="complete")
        self.assertEqual(self.store.list(states={"complete"})[0]["state"], "complete")
        self.assertEqual(refreshed["state"], "unknown")

    def test_runner_pass_shares_discovery_but_next_pass_observes_external_rewrite(self):
        duty = self.store.create("pass-snapshot", "retire", {}, {"target": "pane"})
        self.store.begin_read_pass()
        self.assertEqual(self.store.list(states={"pending"}, kinds={"retire"})[0]["id"], duty["id"])
        path = self.store._record_path(duty["id"])
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace('"state":"pending"', '"state":"unknown"'), encoding="utf-8")
        self.assertEqual(self.store.list(states={"pending"}, kinds={"retire"})[0]["id"], duty["id"])
        self.store.end_read_pass()
        self.assertEqual(self.store.list(states={"pending"}, kinds={"retire"}), [])
        self.assertEqual(self.store.list(states={"unknown"}, kinds={"retire"})[0]["state"], "unknown")

    def test_skip_unchanged_preserves_default_revision_updates(self):
        duty = self.store.create("message-noop", "message", {}, {})
        observation = {"ref": "r" * 32, "reason": "target-identity-changed"}
        updated = self.store.update(duty["id"], state="unknown", observation=observation)

        same = self.store.update(duty["id"], state="unknown", observation=observation,
                                 skip_unchanged=True)
        self.assertEqual(same, updated)

        default_update = self.store.update(duty["id"], state="unknown", observation=observation)
        self.assertEqual(default_update["revision"], updated["revision"] + 1)
        self.assertGreaterEqual(default_update["updated_at"], updated["updated_at"])

    def test_inherited_batch_lease_requires_flock_and_exact_observer_row(self):
        from types import SimpleNamespace

        jobs = self.jobs
        jobs.parent.mkdir(parents=True, exist_ok=True)
        jobs.touch(exist_ok=True)
        args = SimpleNamespace(
            jobs=jobs, sealed_batch_id="sealed-lease-fixture",
            parent_attempt_id="att-lease-parent", parent_session_id=None,
            attempt_id=["att-lease-child"], control_socket=None, queue_socket=None,
            thread_id=None, interval=1.0, timeout=30.0,
        )
        duty_id, _record = batch_obligations.create(args)
        lease_path = batch_obligations._root(jobs) / (duty_id + ".lease")
        lease_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self.assertIsNone(batch_obligations.acquire(jobs, duty_id, fd))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(batch_obligations.acquire(jobs, duty_id, fd), fd)
            claimed = batch_obligations.update_for_observer(
                jobs, duty_id, fd, claim=True, reason="observing")
            self.assertIsNotNone(claimed)
            self.assertTrue(batch_obligations.observer_owns_current(jobs, duty_id, fd))

            batch_obligations.update(jobs, duty_id, state="unknown",
                                     observer={"pid": 1, "pid_start": "1"})
            before = batch_obligations.read(jobs, duty_id)
            self.assertFalse(batch_obligations.observer_owns_current(jobs, duty_id, fd))
            self.assertIsNone(batch_obligations.update_for_observer(
                jobs, duty_id, fd, claim=True,
                expected_observer={"pid": os.getpid(),
                                   "pid_start": batch_obligations._pid_start(os.getpid())},
                reason="replacement-race"))
            self.assertIsNone(batch_obligations.update_for_observer(
                jobs, duty_id, fd, state="complete", reason="stale-observer"))
            self.assertEqual(batch_obligations.read(jobs, duty_id), before)
        finally:
            batch_obligations.release(fd, lease_path)

        reused = os.open(jobs, os.O_RDONLY)
        try:
            self.assertIsNone(batch_obligations.acquire(jobs, duty_id, reused))
            batch_obligations.release(reused, lease_path)
            os.fstat(reused)
        finally:
            os.close(reused)

    def test_post_spawn_record_failure_keeps_child_lease_locked(self):
        from types import SimpleNamespace

        jobs = self.jobs
        jobs.parent.mkdir(parents=True, exist_ok=True)
        jobs.touch(exist_ok=True)
        args = SimpleNamespace(
            jobs=jobs, sealed_batch_id="sealed-spawn-failure",
            parent_attempt_id="att-spawn-parent", parent_session_id=None,
            attempt_id=["att-spawn-child"], control_socket=None, queue_socket=None,
            thread_id=None, interval=1.0, timeout=30.0,
        )
        duty_id, _record = batch_obligations.create(args)
        lease_path = batch_obligations._root(jobs) / (duty_id + ".lease")
        real_popen = subprocess.Popen
        children = []

        def launch_child(_command, **kwargs):
            child = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            return child

        try:
            with mock.patch.object(obligations, "service_script",
                                   return_value=(Path("/fixture/sidecar.py"), {}, None)), \
                 mock.patch.object(batch_obligations, "_jobs_paths", return_value=[jobs]), \
                 mock.patch.object(batch_obligations, "_resume_owner_settlements",
                                   return_value=[]), \
                 mock.patch.object(batch_obligations, "_write",
                                   side_effect=batch_obligations.BatchObligationError(
                                       "simulated-record-update-failure")), \
                 mock.patch.object(batch_obligations.subprocess, "Popen",
                                   side_effect=launch_child):
                self.assertEqual(batch_obligations.ensure_observers(jobs), 0)

            self.assertEqual(len(children), 1)
            probe = os.open(lease_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)

            children[0].terminate()
            children[0].wait(timeout=5)
            probe = os.open(lease_path, os.O_RDWR)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            finally:
                os.close(probe)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_retry_handoff_keeps_the_lease_with_the_successor(self):
        from types import SimpleNamespace

        jobs = self.jobs
        jobs.parent.mkdir(parents=True, exist_ok=True)
        jobs.touch(exist_ok=True)
        args = SimpleNamespace(
            jobs=jobs, sealed_batch_id="sealed-handoff",
            parent_attempt_id="att-handoff-parent", parent_session_id=None,
            attempt_id=["att-handoff-child"], control_socket=None, queue_socket=None,
            thread_id=None, interval=1.0, timeout=30.0,
        )
        duty_id, _record = batch_obligations.create(args)
        lease_path = batch_obligations._root(jobs) / (duty_id + ".lease")
        fd = batch_obligations.acquire(jobs, duty_id)
        self.assertIsNotNone(fd)
        child = None
        children = []
        try:
            self.assertIsNotNone(batch_obligations.update_for_observer(
                jobs, duty_id, fd, claim=True, reason="observing"))
            real_popen = subprocess.Popen

            def launch_child(_command, **kwargs):
                probe = os.open(batch_obligations._record_path(jobs, duty_id).with_suffix(".update.lock"), os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
                process = real_popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
                children.append(process)
                return process

            with mock.patch.object(obligations, "service_script",
                                   return_value=(Path("/fixture/sidecar.py"), {}, None)), \
                 mock.patch.object(batch_obligations.subprocess, "Popen",
                                   side_effect=launch_child):
                self.assertTrue(batch_obligations.handoff_observer(
                    jobs, duty_id, fd))
            child = children[0]
            owner = batch_obligations.read(jobs, duty_id)["observer"]
            self.assertEqual(owner["pid"], child.pid)
            self.assertTrue(owner["pid_start"].isdecimal())
            self.assertTrue(batch_obligations._fd_holds_lease(fd, lease_path))

            os.close(fd)
            fd = None
            probe = os.open(lease_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)

            child.terminate()
            child.wait(timeout=5)
            probe = os.open(lease_path, os.O_RDWR)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            finally:
                os.close(probe)
        finally:
            if fd is not None:
                batch_obligations.release(fd, lease_path)
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_current_runner_starts_once_while_legacy_runner_lock_is_held(self):
        self.store.create("message-reconnect", "message", {"session_id": "parent"},
                          {"ref": "a" * 32})
        with (self.store.root / "runner.lock").open("w") as legacy, \
             mock.patch.object(obligations.subprocess, "Popen") as spawn:
            fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(obligations.ensure_runner(self.root))
            spawn.assert_called_once()
            argv = spawn.call_args.args[0]
            self.assertEqual(argv[-2:], ["--state-root", str(self.root)])
            with (self.store.root / obligations.RUNNER_LOCK_NAME).open("w") as current:
                fcntl.flock(current, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(obligations.ensure_runner(self.root))
                spawn.assert_called_once()

    def test_retire_lock_waits_for_legacy_and_releases_after_processing_error(self):
        self.store.root.mkdir(parents=True)
        with (self.store.root / "runner.lock").open("w") as legacy:
            fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with obligations.legacy_runner_lock(self.store) as acquired:
                self.assertFalse(acquired)
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with obligations.legacy_runner_lock(self.store) as acquired:
                self.assertTrue(acquired)
                raise RuntimeError("fixture")
        with obligations.legacy_runner_lock(self.store) as acquired:
            self.assertTrue(acquired)

    def test_create_is_idempotent_and_keeps_body_out_of_duty_record(self):
        intent = {"ref": "a" * 32, "body_digest": "b" * 64}
        identity = {"server": "fixture-server", "pane": "w1:p7", "session_id": "sid-1"}
        first = self.store.create("message-" + "a" * 32, "message", identity, intent)
        again = self.store.create("message-" + "a" * 32, "message", identity, intent)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["intent"], again["intent"])
        self.assertNotIn("text", first["intent"])

    def test_immutable_identity_conflict_is_refused(self):
        self.store.create("retire-1", "retire", {"pane": "w1:p1"}, {"target": "worker"})
        with self.assertRaisesRegex(obligations.ObligationError, "intent-conflict"):
            self.store.create("retire-1", "retire", {"pane": "w1:p2"}, {"target": "worker"})

    def test_result_is_immutable_across_later_transport_updates(self):
        self.store.create("batch-1", "registered-batch", {"parent": "att-parent"},
                          {"attempt_ids": ["att-child"]})
        self.store.update("batch-1", result="succeeded", delivery="pending")
        updated = self.store.update("batch-1", observation={"reason": "receiver-unavailable"},
                                    delivery="pending")
        self.assertEqual(updated["result"], "succeeded")
        with self.assertRaisesRegex(obligations.ObligationError, "result-conflict"):
            self.store.update("batch-1", result="failed")

    def test_ordinary_pane_without_registry_row_is_valid_native_evidence(self):
        jobs = self.root / "dispatch" / "jobs.log"
        work, state = obligations.bound_work_for_pane(
            "w1:p4", "codex", "thread-1", jobs=jobs)
        self.assertEqual((work, state), ((), "observed"))
        ready = obligations.pane_readiness(
            server="fixture-server", pane="w1:p4", harness="codex",
            session_id="thread-1", pid_birth="pid:9@start:4",
            identity_verified=True, native_turn="idle",
            bound_work=work, bindings_state=state,
        )
        self.assertEqual((ready.state, ready.scope, ready.outcome),
                         ("ready", "native-turn", None))

    def test_a_received_gate_does_not_close_a_later_terminal_completion(self):
        from types import SimpleNamespace
        import dispatch_pending_delivery as pending
        identity = {"jobs": str(self.jobs), "attempt_id": "att-owner",
                    "session_id": "sid", "harness": "claude"}
        duty = self.store.create("registered-batch-fixture", "registered-batch", identity,
                                 {"carrier": "claude-parent-runtime"})
        owner = "retained-rewake:" + duty["id"]
        observation = {"transfer_ref": "a" * 32, "recipient_sid": "sid",
                       "delivery_id": "delivery-gate", "storage_recipient": "sid", "claim_owner": owner}
        self.store.update(duty["id"], observation=observation)
        def transfer(ref, delivery_id, attempt_id="att-owner"):
            return {"state": "received", "receipt": "exact-peer-ref", "ref": ref,
                   "refs": [duty["id"]],
                   "to": {"harness": "claude", "session_id": "sid"},
                   "dispatch_notice": {"duty_id": duty["id"], "jobs": identity["jobs"],
                       "attempt_id": attempt_id, "carrier": "claude-parent-runtime",
                       "recipient_sid": "sid", "storage_recipient": "sid",
                       "delivery_id": delivery_id, "claim_owner": owner}}
        row = SimpleNamespace(status="done", attempt_id="att-owner",
                              metadata={"parent_sid": "sid", "parent_completion_delivery": "claude-parent-runtime",
                                        "delivery_intent": "1",
                                        "delivery_id": "delivery-final",
                                        "delivery_receipt_digest": "digest-final",
                                        "delivery_row_revision": "rev-final"})
        def record(delivery_id, state, digest, revision):
            return {"delivery_id": delivery_id, "recipient_digest": pending.recipient_digest("sid"),
                    "recipient_kind": "claude-parent-runtime", "state": state,
                    "attempt_ids": ["att-owner"], "receipt_digest": digest,
                    "row_revisions": {"att-owner": revision}, "receipt": {"kind": "completion"},
                    "session_generation": "", "session_generation_supported": "0",
                    "parent_attempt_id": "att-parent", "route_id": "rt-fixture",
                    "route_node": "report", "lineage": []}
        gate = record("delivery-gate", "claimed", "digest-gate", "rev-gate")
        final = record("delivery-final", "claimed", "digest-final", "rev-final")
        records = {"delivery-gate": gate, "delivery-final": final}
        def read_record(_root, _storage, delivery_id):
            return records.get(delivery_id)
        def acknowledge(_root, _storage, delivery_id, **_kwargs):
            records[delivery_id]["state"] = "acked"
            return records[delivery_id]
        message = transfer("a" * 32, "delivery-gate")
        with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
                mock.patch("dispatch_seat_handover.effective_parent", return_value="sid"), \
                mock.patch("dispatch_pending_delivery.read", side_effect=read_record), \
                mock.patch("dispatch_pending_delivery.received_ack_identity",
                           side_effect=lambda value, _storage, _did: {"delivery_id": value["delivery_id"]}), \
                mock.patch("dispatch_pending_delivery.ack_received", side_effect=acknowledge) as ack:
            self.assertTrue(obligations.acknowledge_registered_delivery(message, roots=[self.root]))
            ack.assert_called_once()
            self.assertEqual(self.store.get(duty["id"])["state"], "pending")
            self.store.update(duty["id"], observation={**observation, "transfer_ref": "b" * 32,
                                                      "delivery_id": "delivery-final"})
            self.assertTrue(obligations.registered_delivery_settled(message, roots=[self.root]))
            obligations.acknowledge_registered_delivery(message, roots=[self.root])
            self.assertEqual(self.store.get(duty["id"])["state"], "pending")
            ack.reset_mock()

            for invalid in (
                {**message, "ref": "z" * 32},
                {**message, "to": {"harness": "claude", "session_id": "other"}},
                {**message, "to": {"harness": "codex", "session_id": "sid"}},
                transfer("b" * 32, "delivery-gate", attempt_id="att-other"),
                transfer("b" * 32, "delivery-missing"),
                {**message, "dispatch_notice": {
                    **message["dispatch_notice"], "storage_recipient": "other"}},
            ):
                self.assertFalse(obligations.acknowledge_registered_delivery(invalid, roots=[self.root]))
            self.assertFalse(obligations.acknowledge_registered_delivery(
                message, roots=[self.root / "foreign-artifacts"]))
            self.assertEqual(ack.call_count, 0)
            with mock.patch("dispatch_seat_handover.effective_parent", return_value="successor"):
                self.assertFalse(obligations.acknowledge_registered_delivery(message, roots=[self.root]))
            self.assertEqual(ack.call_count, 0)

            successor_duty = self.store.create(
                "registered-batch-successor", "registered-batch", identity,
                {"carrier": "claude-parent-runtime"})
            successor_owner = "retained-rewake:" + successor_duty["id"]
            records["delivery-successor"] = record(
                "delivery-successor", "claimed", "digest-successor", "rev-successor")
            successor_transfer = {"state": "received", "receipt": "exact-native-history",
                "ref": "c" * 32, "refs": [successor_duty["id"]],
                "to": {"harness": "claude", "session_id": "successor"},
                "dispatch_notice": {"duty_id": successor_duty["id"], "jobs": identity["jobs"],
                    "attempt_id": "att-owner", "carrier": "claude-parent-runtime",
                    "recipient_sid": "successor", "storage_recipient": "sid",
                    "delivery_id": "delivery-successor", "claim_owner": successor_owner}}
            with mock.patch("dispatch_seat_handover.effective_parent", return_value="successor"):
                self.assertTrue(obligations.acknowledge_registered_delivery(
                    successor_transfer, roots=[self.root]))
            self.assertEqual(records["delivery-successor"]["state"], "acked")
            ack.reset_mock()

            final_transfer = transfer("b" * 32, "delivery-final")
            final["receipt_digest"] = "tampered-digest"
            self.assertFalse(obligations.acknowledge_registered_delivery(final_transfer, roots=[self.root]))
            self.assertEqual(ack.call_count, 0)
            final["receipt_digest"] = "digest-final"
            real_update = obligations.ObligationStore.update
            failed_update = {"remaining": 1}
            def update_once(store, duty_id, **kwargs):
                if failed_update["remaining"] and kwargs.get("state") == "complete":
                    failed_update["remaining"] -= 1
                    raise OSError("simulated duty persistence interruption")
                return real_update(store, duty_id, **kwargs)
            with mock.patch.object(obligations.ObligationStore, "update", autospec=True,
                                   side_effect=update_once):
                self.assertTrue(obligations.acknowledge_registered_delivery(final_transfer,
                                                                            roots=[self.root]))
            self.assertEqual(self.store.get(duty["id"])["state"], "pending")
            self.assertEqual(final["state"], "acked")
            self.assertTrue(obligations.acknowledge_registered_delivery(final_transfer, roots=[self.root]))
            self.assertEqual(ack.call_count, 1)
            self.assertEqual(self.store.get(duty["id"])["state"], "complete")

    def test_registered_completion_producer_uses_shared_normal_runner_helper(self):
        from types import SimpleNamespace
        carrier = "fixture-parent-runtime"
        jobs = self.root / "dispatch" / "jobs.log"
        row = SimpleNamespace(status="running", metadata={
            "parent_completion_delivery": carrier})
        with mock.patch("dispatch_completion_join.current_attempt_row", return_value=row), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value="sid-parent"), \
             mock.patch("harness_capabilities.parent_completion",
                        return_value={"carrier": carrier}), \
             mock.patch.object(obligations, "ensure_normal_runner") as ensure:
            obligations.retain_registered_completion(
                jobs, "att-fixture", "sid-parent", carrier)
        duty = obligations.ObligationStore(jobs.parent).list()[0]
        self.assertEqual(duty["intent"]["identity"]["attempt_id"], "att-fixture")
        ensure.assert_called_once_with(jobs.parent)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_unsupported_observer_releases_same_lock_without_losing_accepted_duties(self):
        self._check_observer_handoff(supported=False)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_supported_observer_keeps_its_pid_and_same_lock(self):
        self._check_observer_handoff(supported=True)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_claude_only_observer_is_replaced_for_shared_activation_delivery(self):
        self._check_observer_handoff(supported=False, native_only=True)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux pidfd handoff")
    def test_a_legacy_observer_keeps_its_pid_while_current_runner_starts(self):
        self._check_observer_handoff(supported=False, legacy=True)

    def _check_observer_handoff(self, supported, legacy=False, native_only=False):
        duty = self.store.create("registered-batch-fixture", "registered-batch",
                                 {"session_id": "parent"}, {"carrier": "claude-parent-runtime"})
        prior = self.store.create("message-fixture", "message", {"session_id": "other"}, {"ref": "old"})
        script = self.root / "peer-steward.py"
        source = "import time\nprint('ready', flush=True)\ntime.sleep(30)\n"
        if supported:
            source = ("def _resume_registered_obligation():\n"
                      "    from dispatch_session_sweep import addressed_records\n" + source)
        elif native_only:
            source = "def _resume_registered_obligation(): pass\n" + source
        script.write_text(source)
        lock_path = self.store.root / ("runner.lock" if legacy else obligations.RUNNER_LOCK_NAME)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        process = subprocess.Popen([sys.executable, str(script), "__obligation-runner",
                                    "--state-root", str(self.root), "--lock-fd", str(fd)],
                                   pass_fds=(fd,), stdout=subprocess.PIPE, text=True)
        os.close(fd)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with mock.patch.object(obligations.subprocess, "Popen") as spawned, \
                    mock.patch.object(obligations, "_RUNNERS", []):
                self.assertTrue(obligations.ensure_runner(self.root))
                if legacy:
                    self.assertIsNone(process.poll())
                    spawned.assert_called_once()
                    self.assertEqual(len(spawned.call_args.kwargs["pass_fds"]), 1)
                    self.assertNotEqual(lock_path.name, obligations.RUNNER_LOCK_NAME)
                elif supported:
                    self.assertIsNone(process.poll())
                    spawned.assert_not_called()
                else:
                    # A caller without a verified managed lifecycle binding
                    # cannot replace a live process, even if it looks legacy.
                    self.assertIsNone(process.poll())
                    spawned.assert_not_called()
            self.assertEqual(self.store.get(duty["id"]), duty)
            self.assertEqual(self.store.get(prior["id"]), prior)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()

    def test_observer_error_after_fulfillment_cannot_reopen_duty(self):
        self.store.create("retire-done", "retire", {"pane": "w1:p1"},
                          {"target": "worker"})
        done = self.store.update("retire-done", state="complete", result="normal-exit",
                                 observation={"phase": "complete"}, cleanup="complete")
        after_error = self.store.update("retire-done", observer_error="observer-unavailable")
        self.assertEqual(after_error, done)

    def test_conditional_observation_preserves_claim_and_terminal_cancellation(self):
        self.store.create("retire-race", "retire", {"pane": "w1:p1"}, {"target": "peer"})
        claimed = self.store.claim_phase("retire-race", {"waiting"}, "exit-requested",
                                        extra={"foreground": {"pid": 42}})
        stale = self.store.update("retire-race", state="pending", expected_phases={"waiting"},
                                  observation={"phase": "waiting", "reason": "stale"})
        self.assertEqual(stale, claimed)
        cancelled = self.store.update("retire-race", state="cancelled",
                                      observation={"phase": "waiting", "reason": "cancelled"})
        self.assertEqual(self.store.update("retire-race", state="unknown", expected_phases={"waiting"},
                                          observation={"phase": "waiting"}), cancelled)
        self.assertIsNone(self.store.claim_phase("retire-race", {"waiting"}, "exit-requested"))

    def test_unreadable_binding_source_is_unknown(self):
        jobs = self.root / "not-a-file"
        jobs.mkdir()
        work, state = obligations.bound_work_for_pane(
            "w1:p4", "codex", "thread-1", jobs=jobs)
        self.assertEqual((work, state), ((), "unknown"))
        ready = obligations.pane_readiness(
            server="fixture-server", pane="w1:p4", harness="codex",
            session_id="thread-1", pid_birth="pid:9@start:4",
            identity_verified=True, native_turn="idle",
            bound_work=work, bindings_state=state,
        )
        self.assertEqual((ready.state, ready.reason),
                         ("unknown", "registered-bindings-unavailable"))

    def test_foreground_tag_binds_executor_even_when_it_also_owns_children(self):
        from dispatch_contract import ObservedAttemptLiveness
        jobs = self.root / "jobs.log"
        for harness in ("claude", "codex", "opencode"):
            jobs.write_text(
                f"now\topen\trepo\t-\texec\tattempt_id=att-exec,harness={harness},parent_sid=other\n"
                f"now\topen\trepo\t-\tchild\tattempt_id=att-child,parent_sid=sid,parent_harness={harness}\n")
            with mock.patch("dispatch_contract.observed_attempt_liveness", return_value=
                            ObservedAttemptLiveness("alive", "fixture", "live", "fixture")):
                work, state = obligations.bound_work_for_pane(
                    "pane", harness, "sid", jobs=jobs, execution_attempt_ids=("att-exec",))
            self.assertEqual(state, "observed")
            self.assertEqual([(w.attempt_id, w.pane_relation) for w in work],
                             [("att-exec", "executor"), ("att-child", "parent")])
            # Input readiness ignores parent rows, so it never observes them.
            with mock.patch("dispatch_contract.observed_attempt_liveness", return_value=
                            ObservedAttemptLiveness("alive", "fixture", "live", "fixture")), \
                 mock.patch("dispatch_terminal_commit.owner_completion_state",
                            wraps=__import__("dispatch_terminal_commit").owner_completion_state) as closure:
                work, state = obligations.bound_work_for_pane(
                    "pane", harness, "sid", jobs=jobs, execution_attempt_ids=("att-exec",),
                    include_parent=False)
            self.assertEqual([(w.attempt_id, w.pane_relation) for w in work], [("att-exec", "executor")])
            self.assertEqual(closure.call_count, 1)

    def test_foreground_tag_read_is_birth_checked_and_does_not_use_parent_tag(self):
        with mock.patch("dispatch_contract._runtime_ancestry_proc_stat", return_value={"start": 7}), \
             mock.patch.object(Path, "read_bytes", return_value=b"AGENT_DISPATCH_ATTEMPT_ID=att-own\0"):
            self.assertEqual(obligations.execution_attempts_for_processes([{"pid": 123}]), ("att-own",))
        with mock.patch("dispatch_contract._runtime_ancestry_proc_stat",
                        side_effect=[{"start": 7}, {"start": 8}]), \
             mock.patch.object(Path, "read_bytes", return_value=b"AGENT_DISPATCH_ATTEMPT_ID=att-own\0"):
            self.assertEqual(obligations.execution_attempts_for_processes([{"pid": 123}]), ())
        with mock.patch("dispatch_contract._runtime_ancestry_proc_stat", return_value={"start": 7}), \
             mock.patch.object(Path, "read_bytes", side_effect=PermissionError()):
            with self.assertRaisesRegex(obligations.ObligationError, "pane-execution-unavailable"):
                obligations.execution_attempts_for_processes([{"pid": 123}])


_FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
mode = os.environ.get("FAKE_HERDR_MODE", "idle")
target = argv[2] if len(argv) > 2 else "-"
info = {"result": {"agent": {"agent": "claude", "agent_session": {"value": "sid-fake"},
        "agent_status": "idle", "name": target, "pane_id": "w1:p9",
        "window_id": os.environ.get("FAKE_HERDR_WINDOW_ID", "window-fixture")},
        "type": "agent_info"}}
verb = argv[1] if len(argv) > 1 else ""
if verb == "process-info":
    print(json.dumps({"result": {"process_info": {
        "pane_id": argv[-1], "shell_pid": os.getppid(),
        "foreground_process_group_id": os.getppid(),
        "foreground_processes": [{"pid": os.getppid(), "argv": ["zsh"]}]}}}))
    sys.exit(0)
if verb == "wait" and mode == "held":
    with open(os.environ["FAKE_HERDR_FIFO"], "r") as fh:
        fh.read()
print(json.dumps(info))
sys.exit(0)
'''

_SCRUB_PREFIXES = ("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_",
                   "AGENT_THREAD_", "HERDR_")
_SCRUB_KEYS = ("AGENT_SESSION_ID", "AGENT_SESSION_ROLE", "CLAUDE_CODE_SESSION_ID",
               "CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
               "OPENCODE_SESSION_ID", "OPENCODE_DISPATCH_SLUG",
               "AGENT_HERDR_SESSION")


class StewardRecoveryIntegrationTest(unittest.TestCase):
    """Actual steward/controller recovery: fixture transport, death, restart, display."""


def _fixture_env(root: Path, jobs: Path, state: Path, bindir: Path,
                 fifo: Path, session: str, mode: str) -> dict:
    """Fixture transport with inherited runtime/session/dispatch identities removed."""
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith(_SCRUB_PREFIXES):
            env.pop(key, None)
    for key in _SCRUB_KEYS:
        env.pop(key, None)
    env["AGENT_DISPATCH_JOBS"] = str(jobs)
    env["AGENT_PEER_LEDGER_ROOT"] = str(state)
    env["HOME"] = str(root / "home")
    env["CLAUDE_CODE_SESSION_ID"] = session
    env["FAKE_HERDR_MODE"] = mode
    env["FAKE_HERDR_FIFO"] = str(fifo)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return env


class StewardRecoveryIntegrationTest(unittest.TestCase):
    """Actual steward/controller recovery: fixture transport, death, restart, display."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.touch()
        self.state = self.root / "peer-state"
        self.bindir = self.root / "fakebin"
        self.bindir.mkdir()
        fake = self.bindir / "herdr"
        fake.write_text(_FAKE_HERDR, encoding="utf-8")
        fake.chmod(0o755)
        self.fifo = self.root / "release.fifo"
        os.mkfifo(self.fifo)
        self.session = "steward-obligation-1"
        self.saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)
        self._children: list[int] = []
        self.addCleanup(self._reap_children)
        self._old_herdr_session = peer_steward._HERDR_SESSION
        peer_steward._HERDR_SESSION = None
        self.addCleanup(self._restore_herdr_session)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self.saved_environ)

    def _restore_herdr_session(self):
        peer_steward._HERDR_SESSION = self._old_herdr_session

    def _reap_children(self):
        for pid in self._children:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.monotonic() + 10
        remaining = set(self._children)
        while remaining and time.monotonic() < deadline:
            for pid in tuple(remaining):
                try:
                    waited, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    remaining.discard(pid)
                    continue
                except OSError:
                    continue
                if waited:
                    remaining.discard(pid)
            time.sleep(0.02)

    def _apply_fixture_env(self, mode="held"):
        os.environ.clear()
        os.environ.update(_fixture_env(
            self.root, self.jobs, self.state, self.bindir,
            self.fifo, self.session, mode))

    def _arm_watch(self, mode="held"):
        env = _fixture_env(self.root, self.jobs, self.state, self.bindir,
                           self.fifo, self.session, mode)
        proc = subprocess.run(
            [sys.executable, str(_HERE / "peer-steward.py"), "watch", "peer-a"],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields = {}
        for token in proc.stdout.split():
            key, separator, value = token.partition("=")
            if separator:
                fields[key] = value
        return fields["watch_id"], int(fields["pid"]), env

    @staticmethod
    def _wait_for_death(pid: int, seconds: float = 10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.02)
        raise AssertionError(f"fixture observer {pid} never died")

    def test_watch_duty_survives_observer_death_and_resumes_same_id(self):
        watch_id, pid, _env = self._arm_watch(mode="held")
        arm_path = self.state / "peer-watches" / f"{watch_id}.json"
        first = json.loads(arm_path.read_text(encoding="utf-8"))
        self.assertEqual(first["watch_id"], watch_id)
        os.kill(pid, signal.SIGKILL)
        self._wait_for_death(pid)

        self._apply_fixture_env(mode="held")
        spawned = []
        real_popen = subprocess.Popen

        def tracked_spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        def reap_spawned():
            for process in spawned:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)

        self.addCleanup(reap_spawned)
        with mock.patch.object(peer_steward.subprocess, "Popen", side_effect=tracked_spawn):
            peer_steward._ensure_watch_observers()

        resumed = json.loads(arm_path.read_text(encoding="utf-8"))
        self.assertEqual(resumed["watch_id"], watch_id,
                         "restart replaces only the observer, never the duty")
        self.assertGreaterEqual(resumed.get("observer_generation", 1), 2)
        new_pid = int((resumed.get("watcher") or {})["pid"])
        self.assertNotEqual(new_pid, pid)
        try:
            os.kill(new_pid, 0)
        except OSError:
            self.fail("resumed observer is not alive")
        # Death never fulfills the duty: no terminal receipt was written.
        self.assertFalse((self.state / "peer-watches" / f"{watch_id}.receipt.json").exists())

    def test_pending_duties_reach_the_read_only_collector_body_free(self):
        watch_id, pid, _env = self._arm_watch(mode="held")
        self._children.append(pid)
        store = obligations.ObligationStore(self.state)
        store.create("message-" + "r" * 32, "message",
                     {"server": "default", "pane": "w1:p9",
                      "harness": "claude", "session_id": "sid-fake"},
                     {"ref": "r" * 32, "body_digest": "d" * 64,
                      "target": "peer-a",
                      "to": {"harness": "claude", "session_id": "sid-fake"}})
        tools = str(_HERE.parent / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.collectors import peer_messages
        result = peer_messages.collect(state_roots=[str(self.state)])
        pending = result["by_session"][("claude", "sid-fake")]["pending_obligations"]
        kinds = {item["kind"] for item in pending}
        self.assertIn("watch", kinds)
        self.assertIn("message", kinds)
        for item in pending:
            self.assertLessEqual(set(item), {"kind", "state", "age_min", "ref"})
        os.kill(pid, signal.SIGKILL)
        self._wait_for_death(pid)
        self._children.remove(pid)

    def test_bound_registered_row_overrides_idle_pane_through_real_jobs(self):
        from dispatch_contract import (ATTEMPT_SCHEMA_VERSION, claim_attempt_row,
                                       process_launch_identity, ObservedAttemptLiveness)
        metadata = {"attempt_schema_version": str(ATTEMPT_SCHEMA_VERSION),
                    "dispatch_depth": "1", "transport": "headless",
                    "execution_surface": "registered-headless", "registered_worker": "1",
                    "fallback_hop": "same-harness-headless", "harness": "codex",
                    "worker_type": "dev/backend", "owner_route_id": "route-fixture",
                    "owner_route_hash": "sha256:" + "a" * 64, "route_node": "execute",
                    "attempt_id": "att-fixture-1", "parent_sid": "sid-1",
                    "parent_harness": "codex", **process_launch_identity(os.getpid())}
        self.assertNotIn("parent_pane", metadata)
        row = "2026-10-09T00:00:00Z\topen\trepo\t-\tslug\t" + ",".join(
            f"{key}={value}" for key, value in metadata.items())
        self.assertTrue(claim_attempt_row(self.jobs, "att-fixture-1", row, launch=True))
        for process_state, expected in (("live", "pending"), ("unverifiable", "unknown")):
            with self.subTest(process_state=process_state), mock.patch(
                    "dispatch_contract.observed_attempt_liveness", return_value=ObservedAttemptLiveness(
                        "alive" if process_state == "live" else "unverifiable",
                        "fixture", process_state, "fixture")):
                work, state = obligations.bound_work_for_pane(
                    "w1:p4", "codex", "sid-1", jobs=self.jobs)
                self.assertEqual((len(work), state), (1, "observed"))
                ready = obligations.pane_readiness(
                    server="fixture-server", pane="w1:p4", harness="codex",
                    session_id="sid-1", pid_birth="pid:9@start:4",
                    identity_verified=True, native_turn="idle", bound_work=work,
                    bindings_state=state)
                self.assertEqual(ready.state, expected)
                self.assertIsNone(ready.outcome)
        for harness, sid in (("codex", "unrelated"), ("claude", "sid-1")):
            self.assertEqual(obligations.bound_work_for_pane(
                "w1:p4", harness, sid, jobs=self.jobs), ((), "observed"))
        with mock.patch("dispatch_seat_handover._handover_binding_keys_for_session",
                        return_value=(("route-fixture", "sha256:" + "a" * 64, "execute"),)), \
             mock.patch("dispatch_seat_handover.effective_parent", return_value="successor"), \
             mock.patch("dispatch_seat_handover.effective_parent_harness", return_value="claude"):
            work, state = obligations.bound_work_for_pane(
                "w2:p7", "claude", "successor", jobs=self.jobs)
            self.assertEqual((len(work), state), (1, "observed"))


if __name__ == "__main__":
    unittest.main()
