#!/usr/bin/env python3
"""Durable registered-batch observer ownership and restart projection tests."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import dispatch_batch_obligations as obligations  # noqa: E402


class BatchObligationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.jobs.write_text("fixture\n", encoding="utf-8")
        self.args = Namespace(
            jobs=self.jobs, sealed_batch_id="batch-fixture",
            parent_attempt_id="att-parent", parent_session_id=None,
            attempt_id=["att-child"], delivery_parent_id="att-parent",
            control_socket=self.base / "control.sock", queue_socket=None,
            thread_id=None, interval=0.1, timeout=1.0,
        )

    def test_exact_identity_and_settled_result_are_immutable(self):
        duty_id, record = obligations.create(self.args)
        self.assertEqual(record["identity"]["attempt_ids"], ["att-child"])
        settled = {"state": "ready", "children": [{"attempt_id": "att-child", "status": "done"}]}
        obligations.update(self.jobs, duty_id, state="delivery-pending", outcome=settled)
        obligations.update(self.jobs, duty_id, state="delivery-pending", reason="receiver-unavailable")
        self.assertEqual(obligations.read(self.jobs, duty_id)["outcome"], settled)
        with self.assertRaises(obligations.BatchObligationError):
            obligations.update(self.jobs, duty_id, outcome={"state": "failed"})

    def test_concurrent_resume_claim_has_one_observer(self):
        duty_id, _record = obligations.create(self.args)
        owner = obligations.acquire(self.jobs, duty_id)
        self.assertIsNotNone(owner)
        self.assertIsNone(obligations.acquire(self.jobs, duty_id))
        obligations.release(owner)
        retry = obligations.acquire(self.jobs, duty_id)
        self.assertIsNotNone(retry)
        obligations.release(retry)

    def test_startup_resumes_only_unfinished_records_with_same_identity(self):
        duty_id, _record = obligations.create(self.args)
        calls = []

        class Launched:
            pid = 901234

        with mock.patch.object(obligations.subprocess, "Popen", side_effect=lambda *a, **k: calls.append((a, k)) or Launched()):
            self.assertEqual(obligations.ensure_observers(self.jobs), 1)
        command = calls[0][0][0]
        self.assertIn("--resume-obligation", command)
        self.assertIn(duty_id, command)
        self.assertEqual(command[command.index("--jobs") + 1], str(self.jobs))
        self.assertEqual(obligations.read(self.jobs, duty_id)["identity"]["attempt_ids"], ["att-child"])

    def test_verified_service_replaces_exact_live_observer_and_passes_control_binding(self):
        duty_id, _record = obligations.create(self.args)
        obligations.update(self.jobs, duty_id, state="delivery-pending",
                           observer={"pid": 321, "pid_start": "654"})
        root = obligations._root(self.jobs)
        lock_fd = os.open(root / (duty_id + ".lease"), os.O_RDWR | os.O_CREAT, 0o600)
        binding = "/fixture/managed/current"
        target = Path("/fixture/releases/B/utilities/codex-managed-completion.py")
        environment = {"AGENT_HOME": "/fixture/releases/B"}
        calls = []

        class Launched:
            pid = 9002

        try:
            with mock.patch("peer_obligations.service_script",
                            return_value=(target, environment, binding)) as service_script, \
                 mock.patch("peer_obligations.replace_managed_observer",
                            return_value="replaced") as replace, \
                 mock.patch.object(obligations, "acquire", side_effect=[None, lock_fd]), \
                 mock.patch.object(obligations.subprocess, "Popen",
                                   side_effect=lambda *a, **kw: calls.append((a, kw)) or Launched()):
                self.assertEqual(obligations.ensure_observers(self.jobs, service_current=binding), 1)
            service_script.assert_called_once_with(
                "utilities/codex-managed-completion.py", binding)
            replace.assert_called_once()
            self.assertEqual(replace.call_args.args[0:2], (321, "654"))
            argv = calls[0][0][0]
            self.assertEqual(Path(argv[1]), target)
            self.assertEqual(argv[-2:], ["--service-current", binding])
            self.assertEqual(calls[0][1]["env"], environment)
            self.assertNotIn("service_current", obligations.read(self.jobs, duty_id)["arguments"])
        finally:
            try:
                os.close(lock_fd)
            except OSError:
                pass

    def test_malformed_present_observer_start_is_not_treated_as_missing(self):
        duty_id, _record = obligations.create(self.args)
        outcome = {"state": "ready", "children": [
            {"attempt_id": "att-child", "status": "done"},
        ]}
        obligations.update(self.jobs, duty_id, state="delivery-pending", outcome=outcome,
                           observer={"pid": 321, "pid_start": ""})
        binding = "/fixture/managed/current"
        with mock.patch("peer_obligations.service_script",
                        return_value=(Path("/fixture/current/codex-managed-completion.py"),
                                      {}, binding)), \
             mock.patch.object(obligations, "acquire", return_value=None), \
             mock.patch("peer_obligations.replace_managed_observer") as replace, \
             mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertEqual(obligations.ensure_observers(
                self.jobs, service_current=binding), 0)
        replace.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(obligations.read(self.jobs, duty_id)["observer"],
                         {"pid": 321, "pid_start": ""})

    def test_complete_duty_is_not_replayed(self):
        duty_id, _record = obligations.create(self.args)
        obligations.update(self.jobs, duty_id, state="complete", outcome={"state": "ready"},
                           delivery="accepted")
        with mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertEqual(obligations.ensure_observers(self.jobs), 0)
        launch.assert_not_called()

    def test_absent_compatibility_slots_are_known_empty(self):
        roots = (self.base / "release" / ".dispatch",
                 self.base / "xdg-state" / "hearting" / "dispatch")
        with mock.patch("dispatch_contract.resolve_agent_home", return_value=self.base / "release"), \
             mock.patch("dispatch_contract.dispatch_state_roots", return_value=roots):
            self.assertEqual(obligations.pending_observer_signature(), ())
            self.assertFalse(obligations.has_unowned_work())

        unused_jobs = self.base / "unused" / "jobs.log"
        unused_jobs.parent.mkdir()
        self.assertEqual(obligations.pending_observer_signature(unused_jobs), ())
        self.assertFalse(obligations.has_unowned_work(unused_jobs))

    def test_empty_existing_registry_and_sidecar_state_are_known_empty(self):
        empty_jobs = self.base / "empty" / "jobs.log"
        empty_jobs.parent.mkdir(parents=True)
        empty_jobs.touch()
        (empty_jobs.parent / "supervisor-state" / "obligations").mkdir(parents=True)
        self.assertEqual(obligations.pending_observer_signature(empty_jobs), ())
        self.assertFalse(obligations.has_unowned_work(empty_jobs))

    def test_missing_registry_keeps_durable_pending_records_visible(self):
        duty_id, _record = obligations.create(self.args)
        obligations.update(self.jobs, duty_id, state="delivery-pending",
                           reason="awaiting-recipient")
        self.jobs.unlink()

        signature = obligations.pending_observer_signature(self.jobs)
        self.assertEqual(len(signature), 1)
        self.assertEqual(signature[0][:3], (str(self.jobs), duty_id, "delivery-pending"))
        self.assertTrue(obligations.has_unowned_work(self.jobs))
        with self.assertRaises(obligations.BatchObligationError):
            obligations._root(self.jobs)

    def test_unreadable_malformed_and_symlink_state_remains_unknown(self):
        root = obligations._root(self.jobs)
        root.mkdir(parents=True)
        with mock.patch.object(obligations.os, "scandir", side_effect=PermissionError("protected")):
            self.assertEqual(obligations.pending_observer_signature(self.jobs),
                             ((str(self.jobs), "unavailable"),))
            self.assertTrue(obligations.has_unowned_work(self.jobs))
            snapshot = obligations.pending_observer_snapshot(self.jobs)
            self.assertFalse(snapshot["cacheable"])
            self.assertEqual(snapshot["paths_by_jobs"][str(self.jobs)], None)

        malformed = root / "batch-malformed.json"
        malformed.write_text("{not-json", encoding="utf-8")
        self.assertEqual(obligations.pending_observer_signature(self.jobs),
                         ((str(self.jobs), malformed.name, "unreadable"),))
        self.assertTrue(obligations.has_unowned_work(self.jobs))
        self.assertFalse(obligations.pending_observer_snapshot(self.jobs)["cacheable"])

        protected_jobs = self.base / "protected" / "jobs.log"
        protected_jobs.parent.mkdir(parents=True)
        protected_jobs.touch()
        protected_root = protected_jobs.parent / "supervisor-state" / "obligations"
        target = self.base / "protected-target"
        target.mkdir()
        protected_root.parent.mkdir()
        protected_root.symlink_to(target, target_is_directory=True)
        self.assertEqual(obligations.pending_observer_signature(protected_jobs),
                         ((str(protected_jobs), "unavailable"),))
        self.assertFalse(obligations.pending_observer_snapshot(protected_jobs)["cacheable"])

    def test_pending_snapshot_reuses_only_stable_readable_directory_pair(self):
        duty_id, _record = obligations.create(self.args)
        obligations.update(self.jobs, duty_id, state="delivery-pending",
                           reason="awaiting-recipient")
        original = obligations._read_only_records
        key = str(self.jobs.resolve(strict=False))

        with mock.patch.object(obligations, "_read_only_records", wraps=original) as scan:
            snapshot = obligations.pending_observer_snapshot(self.jobs)
            self.assertTrue(snapshot["cacheable"])
            self.assertIn(duty_id, [item[1] for item in snapshot["signature"]])
            self.assertEqual(snapshot["root_stamps"][key], obligations._directory_stamp(
                snapshot["roots_by_jobs"][key]))
            scan.assert_called_once_with(self.jobs)

    def test_sidecar_created_between_scan_and_after_stamp_forces_refresh(self):
        original = obligations._read_only_records
        created = []

        def scan_then_publish(jobs):
            records = original(jobs)
            if not created:
                duty_id, _record = obligations.create(self.args)
                obligations.update(jobs, duty_id, state="delivery-pending",
                                   reason="accepted-during-snapshot")
                created.append(duty_id)
            return records

        with mock.patch.object(obligations, "_read_only_records", side_effect=scan_then_publish):
            stale = obligations.pending_observer_snapshot(self.jobs)
        self.assertFalse(stale["cacheable"])
        self.assertNotIn(created[0], [item[1] for item in stale["signature"]])

        refreshed = obligations.pending_observer_snapshot(self.jobs)
        self.assertTrue(refreshed["cacheable"])
        self.assertIn(created[0], [item[1] for item in refreshed["signature"]])

    def test_unowned_work_probe_is_read_only_and_uses_exact_observer_identity(self):
        duty_id, _record = obligations.create(self.args)
        path = obligations._record_path(self.jobs, duty_id)
        before = path.read_bytes()
        self.assertTrue(obligations.has_unowned_work(self.jobs))
        self.assertEqual(path.read_bytes(), before)

        obligations.update(self.jobs, duty_id, observer={
            "pid": os.getpid(), "pid_start": obligations._pid_start(os.getpid())})
        self.assertFalse(obligations.has_unowned_work(self.jobs))
        obligations.update(self.jobs, duty_id, state="complete")
        self.assertFalse(obligations.has_unowned_work(self.jobs))

    def test_owner_settlement_scan_reuses_unchanged_registry_but_rechecks_leases(self):
        from dispatch_contract import SUPERVISOR_LEASE_KIND

        row = (
            "t\topen\t/repo\t/wt\towner\t"
            f"worker_type=owner,launch_started=1,supervisor_lease={SUPERVISOR_LEASE_KIND},"
            "supervisor_lease_file=attempt.lock,log_file=attempt.log,attempt_id=att-owner\n"
        )
        self.jobs.write_text(row, encoding="utf-8")
        context = obligations.OwnerSettlementReadContext()
        original_read_text = Path.read_text
        reads = []

        def count_registry_read(path, *args, **kwargs):
            if path == self.jobs:
                reads.append(path)
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", count_registry_read), \
             mock.patch("dispatch_contract.supervisor_lease_is_held", return_value=True) as lease, \
             mock.patch("dispatch_completion_join.settle_finished_attempt") as settle:
            obligations._resume_owner_settlements(self.jobs, context=context)
            obligations._resume_owner_settlements(self.jobs, context=context)
            self.assertEqual(len(reads), 1)
            self.assertEqual(lease.call_count, 2)
            settle.assert_not_called()

            # A registry change forces a fresh full read on the next pass.
            with self.jobs.open("a", encoding="utf-8") as handle:
                handle.write("t\tdone\t/repo\t/wt\tchild\tattempt_id=att-child\n")
            obligations._resume_owner_settlements(self.jobs, context=context)
            self.assertEqual(len(reads), 2)

            # Lease release is checked even while the registry stamp is stable.
            lease.return_value = False
            obligations._resume_owner_settlements(self.jobs, context=context)
            settle.assert_called_once()
            obligations._resume_owner_settlements(self.jobs, context=context)
            self.assertEqual(len(reads), 4, "an unresolved unowned row stays retryable")

    def test_parent_ended_duty_waits_dormant_until_recipient_returns(self):
        args = Namespace(**vars(self.args))
        args.parent_session_id = "parent-thread"
        args.parent_attempt_id = None
        args.thread_id = "parent-thread"
        args.delivery_parent_id = "parent-thread"
        args.parent_lifetime = {"pid": 123, "pid_start": "456", "pane": "w1:p2",
                                "session_id": "parent-thread", "pid_namespace": "ns"}
        duty_id, _record = obligations.create(args)
        obligations.update(self.jobs, duty_id, state="unknown", reason="parent-session-ended",
                           parent_lifetime=args.parent_lifetime)
        with mock.patch.object(obligations, "_parent_duty_has_recipient", return_value=False), \
             mock.patch.object(obligations.subprocess, "Popen") as launch:
            self.assertEqual(obligations.ensure_observers(self.jobs), 0)
        launch.assert_not_called()
        with mock.patch.object(obligations, "_parent_duty_has_recipient", return_value=True), \
             mock.patch.object(obligations.subprocess, "Popen", return_value=type("P", (), {"pid": 9001})()):
            self.assertEqual(obligations.ensure_observers(self.jobs), 1)


if __name__ == "__main__":
    unittest.main()
