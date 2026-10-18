#!/usr/bin/env python3
"""Isolated real-runner regression for the observed message/batch workload."""

from __future__ import annotations

import hashlib
import fcntl
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_registry_cache  # noqa: E402
import dispatch_batch_obligations  # noqa: E402
import dispatch_completion_join  # noqa: E402
import peer_obligations  # noqa: E402
from dispatch_contract import process_namespace_identity  # noqa: E402
from peer_read_context import PeerReadContext  # noqa: E402

_STEWARD_SPEC = importlib.util.spec_from_file_location(
    "runner_workload_correction_peer_steward", ROOT / "utilities/peer-steward.py")
peer_steward = importlib.util.module_from_spec(_STEWARD_SPEC)
_STEWARD_SPEC.loader.exec_module(peer_steward)
peer_message = peer_steward.peer_message


class RunnerWorkloadCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-runner-workload-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.dispatch = self.base / "dispatch-state"
        self.dispatch.mkdir()
        self.home = self.base / "home"
        for path in (self.home, self.home / ".config", self.home / ".local/share",
                     self.home / ".local/state", self.home / ".cache", self.base / "release",
                     self.base / "codex", self.base / "claude", self.base / "opencode"):
            path.mkdir(parents=True, exist_ok=True)
        self.jobs = self.dispatch / "jobs.log"
        self.jobs.touch()

        self.saved_env = dict(os.environ)
        for key in tuple(os.environ):
            if key.startswith(("AGENT_DISPATCH_", "AGENT_SESSION_", "AGENT_RUNTIME_",
                               "AGENT_THREAD_", "AGENT_HERDR_", "HERDR_", "CLAUDE_",
                               "CODEX_", "OPENCODE_")):
                os.environ.pop(key, None)
        os.environ.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_STATE_HOME": str(self.home / ".local/state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "AGENT_HOME": str(self.base / "release"),
            "AGENT_PEER_LEDGER_ROOT": str(self.dispatch),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "CODEX_HOME": str(self.base / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.base / "claude"),
            "OPENCODE_CONFIG_DIR": str(self.base / "opencode"),
        })
        self.addCleanup(self._restore_environment)
        dispatch_registry_cache._CACHE.pop(str(self.jobs), None)
        self.addCleanup(lambda: dispatch_registry_cache._CACHE.pop(str(self.jobs), None))
        self.store = peer_obligations.ObligationStore(self.dispatch)

    def _restore_environment(self):
        os.environ.clear()
        os.environ.update(self.saved_env)

    @staticmethod
    def _session_id(label, index):
        digest = hashlib.sha256(f"synthetic-{label}-{index}".encode()).hexdigest()
        return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"

    def _message(self, index, *, state="pending", receipt="target-form-open",
                 dispatch_binding=None, initial_observation=None):
        sender = {"harness": "claude", "session_id": self._session_id("sender", index),
                  "name": f"source-{index:03d}"}
        session_id = (self.active_session_id if dispatch_binding else
                      self._session_id("recipient", index))
        recipient = {"harness": "codex", "session_id": session_id,
                     "name": f"recipient-{index:03d}"}
        duty_id = "message-" + hashlib.sha256(f"synthetic-duty-{index}".encode()).hexdigest()[:32]
        refs = [dispatch_binding["duty_id"]] if dispatch_binding else []
        body = "synthetic operational-shape message " + ("context evidence " * (index % 9 + 1))
        # Fixture production uses no existing-message lookup. Every resulting
        # pending/transfer row is still written and validated by production code.
        with mock.patch.object(peer_message, "_pending_rows", return_value=()):
            _text, ref = peer_message.prepare_peer_message(
                body, sender, recipient, defer=True, refs=refs, with_trailer=False)
        pending_path = peer_message._pending_path(ref)
        if state != "pending" or dispatch_binding:
            row = json.loads(pending_path.read_text(encoding="utf-8"))
            row["state"] = state
            row["receipt"] = receipt
            if dispatch_binding:
                row["dispatch_notice"] = dict(dispatch_binding)
            peer_message._save_pending(row)
        duty = self.store.create(
            duty_id, "message",
            {"server": "default", "harness": recipient["harness"],
             "session_id": recipient["session_id"]},
            {"ref": ref, "target": recipient["name"], "to": recipient, "from": sender},
        )
        if initial_observation is not None:
            self.store.update(duty_id, state="unknown", observation=initial_observation)
        return duty_id, ref, recipient["name"]

    def _seed_workload(self):
        from harness_capabilities import parent_completion

        carrier = parent_completion("codex")["carrier"]
        discovered_roots, _source = peer_message.steward_marker_roots()
        self.assertTrue(discovered_roots)
        for discovered in discovered_roots:
            self.assertTrue(Path(discovered).resolve(strict=False).is_relative_to(self.base.resolve()),
                            f"synthetic root escaped its temp fixture: {discovered}")
        namespace = process_namespace_identity()
        pid_start = peer_obligations._proc_start_identity(os.getpid())
        self.assertIsNotNone(pid_start)
        active_attempt = "att-" + "a" * 32
        self.active_session_id = self._session_id("registered-parent", 0)
        registered_ids = []
        for index in range(6):
            attempt = active_attempt if index == 5 else "att-" + hashlib.sha256(
                f"missing-owner-row-{index}".encode()).hexdigest()[:32]
            duty_id = "registered-batch-" + hashlib.sha256(
                f"synthetic-registered-batch-{index}".encode()).hexdigest()[:32]
            self.store.create(duty_id, "registered-batch", {
                "jobs": str(self.jobs.resolve()), "attempt_id": attempt,
                "harness": "codex", "server": "default",
            }, {"carrier": carrier})
            if index < 5:
                self.store.update(duty_id, state="unknown",
                                  observation={"reason": "pending-row-missing"})
            else:
                self.store.update(duty_id, observation={
                    "holder": [str(os.getpid()), str(pid_start), namespace]})
            registered_ids.append(duty_id)

        padding_line = "job\topen\t/repo\t2026-10-18T00:00:00Z\tworker\t" + "x" * 4700 + "\n"
        padding = padding_line * 4894
        metadata = ",".join((
            f"attempt_id={active_attempt}", "attempt_schema_version=2",
            f"parent_sid={self.active_session_id}", "parent_harness=codex",
            f"parent_completion_delivery={carrier}",
            f"parent_runtime_pid={os.getpid()}",
            f"parent_runtime_pid_start={pid_start}", f"parent_runtime_ns={namespace}",
            "delivery_intent=1", "route_id=synthetic-route",
        ))
        active_row = (f"job\topen\t/repo\t2026-10-18T00:00:00Z\towner\t{metadata}\n")
        self.jobs.write_text(padding + active_row, encoding="utf-8")

        # Operational message census shape: 114 queued (112 hook-context and
        # two context-API), 73 pending, one unverified in herdr delivery, and
        # five absent rows. The sole dispatch_notice is on that unverified row.
        dispatch_index = registered_ids[5]
        binding = {
            "duty_id": dispatch_index, "jobs": str(self.jobs.resolve()),
            "attempt_id": active_attempt, "carrier": carrier,
            "recipient_sid": self.active_session_id,
            "storage_recipient": self.active_session_id,
            "delivery_id": "delivery-" + hashlib.sha256(b"synthetic-delivery").hexdigest()[:32],
            "claim_owner": "retained-rewake:" + dispatch_index,
        }
        self.message_duties = []
        for index in range(188):
            if index < 112:
                state, receipt = "queued", "hook-context-emitted"
            elif index < 114:
                state, receipt = "queued", "context-api-accepted"
            elif index == 187:
                state, receipt = "unverified", "herdr-delivery-inflight"
            else:
                state, receipt = "pending", "target-form-open"
            linked = binding if index == 187 else None
            duty_id, ref, target = self._message(
                index, state=state, receipt=receipt, dispatch_binding=linked)
            if state in {"queued", "unverified"}:
                observation = {"ref": ref, "transport_state": state, "reason": receipt}
            elif state == "pending":
                observation = {"ref": ref, "reason": "target-identity-changed",
                               "observed_pane": f"synthetic-pane-{index:03d}"}
            else:
                observation = None
            if observation is not None:
                self.store.update(duty_id, state="unknown", observation=observation)
            self.message_duties.append((duty_id, ref, target))

        # Five accepted message obligations have no readable own pending row.
        for index in range(188, 193):
            ref = hashlib.sha256(f"missing-own-pending-{index}".encode()).hexdigest()[:32]
            duty_id = "message-" + ref
            self.store.create(duty_id, "message", {"server": "default"},
                              {"ref": ref, "target": f"missing-{index}"})
            self.store.update(duty_id, state="unknown",
                              observation={"ref": ref, "reason": "pending-row-missing"})
            self.message_duties.append((duty_id, ref, f"missing-{index}"))

        # Existing completed rows are included because the real store scan reads
        # them even though only unfinished duties are returned to the runner.
        for kind, count, prefix in (("message", 362, "completed-message"),
                                    ("registered-batch", 253, "completed-batch"),
                                    ("retire", 87, "completed-retire")):
            for index in range(count):
                duty_id = prefix + "-" + hashlib.sha256(
                    f"{prefix}-{index}".encode()).hexdigest()[:32]
                duty = self.store.create(duty_id, kind, {}, {})
                self.store.update(duty["id"], state="complete")
        cancelled = self.store.create("cancelled-retire-synthetic", "retire", {}, {})
        self.store.update(cancelled["id"], state="cancelled")
        return registered_ids, carrier, active_attempt

    def test_real_runner_20_second_operational_shape(self):
        registered_ids, _carrier, _attempt = self._seed_workload()
        # The active row carries this host's pid, start tick and namespace, so
        # only the ~23 MB padding is fixed; the exact byte count varies by host.
        self.assertGreaterEqual(self.jobs.stat().st_size, 23_017_000)
        self.assertLess(self.jobs.stat().st_size, 23_300_000)
        self.assertEqual(len(list(peer_message._pending_dir().glob("*.json"))), 188)
        self.assertEqual(len(list(peer_message._transfer_path("0" * 32).parent.glob("*.json"))), 188)
        self.assertEqual(len(list(self.store.root.glob("*.json"))), 902)
        self.assertEqual(len(self.store.list(states={"complete"})), 702)
        self.assertEqual(len(self.store.list(states={"cancelled"})), 1)
        self.assertEqual(sum(self.store.get(duty_id)["state"] in {"pending", "unknown"}
                             for duty_id, _ref, _target in self.message_duties), 193)
        registered_states = [self.store.get(duty_id)["state"] for duty_id in registered_ids]
        self.assertEqual(registered_states.count("unknown"), 5)
        self.assertEqual(registered_states.count("pending"), 1)
        initial_rows = [peer_message._read_pending(ref)
                        for _duty_id, ref, _target in self.message_duties[:188]]
        self.assertEqual(sum(row["state"] == "queued" for row in initial_rows), 114)
        self.assertEqual(sum(row["state"] == "pending" for row in initial_rows), 73)
        self.assertEqual(sum(row["state"] == "unverified" for row in initial_rows), 1)
        notice_indices = [index for index, row in enumerate(initial_rows)
                          if row.get("dispatch_notice")]
        self.assertEqual(notice_indices, [187])
        self.assertEqual(initial_rows[notice_indices[0]]["state"], "unverified")
        observations = [self.store.get(duty_id).get("observation") or {}
                        for duty_id, _ref, _target in self.message_duties]
        expected_reasons = {
            "hook-context-emitted": 112,
            "target-identity-changed": 73,
            "context-api-accepted": 2,
            "herdr-delivery-inflight": 1,
            "pending-row-missing": 5,
        }
        for reason, expected_count in expected_reasons.items():
            self.assertEqual(sum(observation.get("reason") == reason
                                 for observation in observations), expected_count)
        self.assertEqual(len(self.active_session_id), 36)

        fixture_root = self.base.resolve()
        discovered_roots = set()
        write_targets = set()
        observed_roots = set()

        def assert_fixture_path(path, label):
            resolved = Path(path).expanduser().resolve(strict=False)
            self.assertTrue(resolved.is_relative_to(fixture_root),
                            f"{label} escaped the temporary fixture: {resolved}")
            observed_roots.add(str(resolved))
            return resolved

        initial_roots, _source = peer_message.steward_marker_roots()
        for root in initial_roots:
            discovered_roots.add(str(assert_fixture_path(root, "initial peer-state root")))

        counts = {
            "passes": 0, "passes_before_input": 0, "passes_after_input": 0,
            "message_resumes": 0, "registered_resumes": 0,
            "store_list_calls": 0, "store_record_reads": 0,
            "root_discoveries": 0, "root_candidates": 0,
            "pending_reads": 0, "transfer_record_reads": 0,
            "native_lookups": 0, "attempt_row_lookups": 0,
            "context_attempt_row_lookups": 0, "registry_line_calls": 0,
            "jobs_file_reads": 0, "obligation_writes": 0,
            "store_update_calls": 0,
            "input_obligation_writes": 0, "input_pending_writes": 0,
            "input_transfer_writes": 0, "write_open_calls": 0,
            "replace_calls": 0, "unlink_calls": 0,
        }
        monitoring = [False]
        phase = ["setup"]
        event = {}
        sleep_delays = []
        window = 20.0
        real_sleep = time.sleep
        real_os_open = os.open
        real_os_replace = os.replace
        real_os_unlink = os.unlink
        real_path_read_text = Path.read_text
        original_roots = peer_message.steward_marker_roots
        original_state_root = peer_message.peer_state_root
        original_watch_root = peer_steward._watch_root
        original_jobs_paths = dispatch_batch_obligations._jobs_paths
        original_observer_root = dispatch_batch_obligations._observer_root
        original_obligations_root = peer_obligations.obligations_root
        original_pending = peer_message._read_pending
        original_transfer = peer_message._read_transfer_record
        original_write = peer_obligations._write_atomic
        original_read_record = peer_obligations._read_record
        original_store_list = peer_obligations.ObligationStore.list
        original_store_update = peer_obligations.ObligationStore.update
        original_registry_lines = dispatch_registry_cache.registry_lines
        original_attempt_lookup = dispatch_completion_join.current_attempt_row
        original_context_lookup = PeerReadContext.current_attempt_row
        original_reconcile = peer_steward._reconcile_obligations
        original_resume_message = peer_steward._resume_message_obligation
        original_resume_registered = peer_steward._resume_registered_obligation
        original_save_pending = peer_message._save_pending
        original_prepare_message = peer_message._prepare_peer_message
        previous_kill = os.kill

        def checked_roots():
            roots, source = original_roots()
            for root in roots:
                discovered_roots.add(str(assert_fixture_path(root, "discovered peer-state root")))
            if monitoring[0] and phase[0] == "runner":
                counts["root_discoveries"] += 1
                counts["root_candidates"] += len(roots)
            return roots, source

        def checked_state_root():
            return assert_fixture_path(original_state_root(), "peer state root")

        def checked_watch_root():
            return assert_fixture_path(original_watch_root(), "watch root")

        def checked_jobs_paths(jobs, *, strict=True):
            paths = original_jobs_paths(jobs, strict=strict)
            for path in paths:
                assert_fixture_path(path, "dispatch registry")
            return paths

        def checked_observer_root(jobs):
            root = original_observer_root(jobs)
            if root is not None:
                assert_fixture_path(root, "batch obligation root")
            return root

        def checked_obligations_root(root=None):
            return assert_fixture_path(original_obligations_root(root), "peer obligation root")

        def checked_pending(ref, **kwargs):
            assert_fixture_path(peer_message._pending_path(ref), "pending-row read")
            if monitoring[0] and phase[0] == "runner":
                counts["pending_reads"] += 1
            return original_pending(ref, **kwargs)

        def checked_transfer(ref, *, roots=None):
            assert_fixture_path(peer_message._transfer_path(ref), "transfer-row read")
            for root in roots or ():
                assert_fixture_path(root, "transfer candidate root")
            if monitoring[0] and phase[0] == "runner":
                counts["transfer_record_reads"] += 1
            return original_transfer(ref, roots=roots)

        def checked_write(path, value):
            target = assert_fixture_path(path, "obligation write")
            if monitoring[0]:
                if phase[0] == "input":
                    counts["input_obligation_writes"] += 1
                elif phase[0] == "runner":
                    counts["obligation_writes"] += 1
                write_targets.add(str(target))
            return original_write(path, value)

        def checked_read_record(path):
            assert_fixture_path(path, "obligation record read")
            if monitoring[0] and phase[0] == "runner":
                counts["store_record_reads"] += 1
            return original_read_record(path)

        def checked_store_list(store, *args, **kwargs):
            assert_fixture_path(store.root, "obligation listing root")
            if monitoring[0] and phase[0] == "runner":
                counts["store_list_calls"] += 1
            return original_store_list(store, *args, **kwargs)

        def checked_store_update(store, duty_id, **kwargs):
            if monitoring[0] and phase[0] == "runner":
                counts["store_update_calls"] += 1
            return original_store_update(store, duty_id, **kwargs)

        def checked_registry_lines(jobs):
            assert_fixture_path(jobs, "jobs registry read")
            if monitoring[0] and phase[0] == "runner":
                counts["registry_line_calls"] += 1
            return original_registry_lines(jobs)

        def checked_attempt_lookup(jobs, attempt_id, *, read_context=None):
            assert_fixture_path(jobs, "attempt-row lookup")
            if monitoring[0] and phase[0] == "runner":
                counts["attempt_row_lookups"] += 1
            return original_attempt_lookup(jobs, attempt_id, read_context=read_context)

        def checked_context_lookup(context, jobs, attempt_id):
            assert_fixture_path(jobs, "cached attempt-row lookup")
            if monitoring[0] and phase[0] == "runner":
                counts["context_attempt_row_lookups"] += 1
            return original_context_lookup(context, jobs, attempt_id)

        def checked_reconcile(store, duties=None, *, read_context=None):
            if monitoring[0] and phase[0] == "runner":
                counts["passes"] += 1
                counts["passes_after_input" if event else "passes_before_input"] += 1
            return original_reconcile(store, duties, read_context=read_context)

        def checked_resume_message(duty, store, *, read_context=None):
            if monitoring[0] and phase[0] == "runner":
                counts["message_resumes"] += 1
            return original_resume_message(duty, store, read_context=read_context)

        def checked_resume_registered(duty, store, *, read_context=None):
            if monitoring[0] and phase[0] == "runner":
                counts["registered_resumes"] += 1
            return original_resume_registered(duty, store, read_context=read_context)

        def fake_native_provider(target):
            if monitoring[0] and phase[0] == "runner":
                counts["native_lookups"] += 1
                if event and target == event["target"] and "processed_at" not in event:
                    event["processed_at"] = time.monotonic()
            return ("unknown", {"harness": "opencode", "session_id": "synthetic-changed-session",
                                "name": target, "pane": f"synthetic-pane-{target[-3:]}"},
                    0, "target-identity-changed")

        def checked_path_read_text(path, *args, **kwargs):
            target = Path(path).resolve(strict=False)
            if target == self.jobs.resolve(strict=False):
                assert_fixture_path(target, "jobs.log read")
                if monitoring[0] and phase[0] == "runner":
                    counts["jobs_file_reads"] += 1
            return real_path_read_text(path, *args, **kwargs)

        def checked_os_open(path, flags, *args, **kwargs):
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if flags & write_flags and monitoring[0]:
                target = assert_fixture_path(path, "write open")
                counts["write_open_calls"] += 1
                write_targets.add(str(target))
            return real_os_open(path, flags, *args, **kwargs)

        def checked_replace(source, destination, *args, **kwargs):
            if monitoring[0]:
                src = assert_fixture_path(source, "atomic write source")
                dst = assert_fixture_path(destination, "atomic write destination")
                counts["replace_calls"] += 1
                write_targets.update((str(src), str(dst)))
            return real_os_replace(source, destination, *args, **kwargs)

        def checked_unlink(path, *args, **kwargs):
            if monitoring[0]:
                target = assert_fixture_path(path, "write cleanup target")
                counts["unlink_calls"] += 1
                write_targets.add(str(target))
            return real_os_unlink(path, *args, **kwargs)

        def checked_save_pending(row):
            assert_fixture_path(peer_message._pending_path(row["ref"]), "pending-row write")
            if monitoring[0] and phase[0] == "input":
                counts["input_pending_writes"] += 1
            return original_save_pending(row)

        def checked_prepare_message(*args, **kwargs):
            result = original_prepare_message(*args, **kwargs)
            assert_fixture_path(peer_message._transfer_path(result[1]), "transfer-row write")
            if monitoring[0] and phase[0] == "input":
                counts["input_transfer_writes"] += 1
            return result

        def checked_kill(pid, sig):
            if sig != 0:
                raise AssertionError(f"operational signal prohibited in fixture: {pid} {sig}")
            return previous_kill(pid, sig)

        class MeasurementWindowEnded(Exception):
            pass

        lock_path = self.store.root / peer_obligations.RUNNER_LOCK_NAME
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner_args = SimpleNamespace(state_root=str(self.dispatch), lock_fd=lock_fd,
                                      service_current=None)

        def process_ticks():
            fields = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
            return fields[fields.rfind(")") + 2:].split()

        def controlled_sleep(delay):
            requested = max(0.0, float(delay))
            sleep_delays.append(requested)
            sleep_started = time.monotonic()
            wake_at = sleep_started + requested
            if len(sleep_delays) == 3 and not event:
                real_sleep(min(requested * 0.25, max(0.0, deadline - time.monotonic())))
                phase[0] = "input"
                duty_id, ref, target = self._message(193)
                event.update({"duty_id": duty_id, "ref": ref, "target": target,
                              "created_at": time.monotonic(), "pass_at_create": counts["passes"],
                              "trigger_delay": max(0.0, wake_at - time.monotonic())})
                phase[0] = "runner"
                remaining = min(max(0.0, wake_at - time.monotonic()),
                                max(0.0, deadline - time.monotonic()))
                if remaining:
                    real_sleep(remaining)
            else:
                remaining = min(requested, max(0.0, deadline - time.monotonic()))
                if remaining:
                    real_sleep(remaining)
            if time.monotonic() >= deadline:
                raise MeasurementWindowEnded()

        ticks = os.sysconf("SC_CLK_TCK")
        monitoring[0] = True
        phase[0] = "runner"
        before = None
        after = None
        runner_ended_at_window = False
        runner_return = None
        try:
            with ExitStack() as patch_stack:
                patchers = (
                    mock.patch.object(peer_message, "steward_marker_roots", side_effect=checked_roots),
                    mock.patch.object(peer_message, "peer_state_root", side_effect=checked_state_root),
                    mock.patch.object(peer_steward, "_watch_root", side_effect=checked_watch_root),
                    mock.patch.object(dispatch_batch_obligations, "_jobs_paths",
                                      side_effect=checked_jobs_paths),
                    mock.patch.object(dispatch_batch_obligations, "_observer_root",
                                      side_effect=checked_observer_root),
                    mock.patch.object(peer_obligations, "obligations_root",
                                      side_effect=checked_obligations_root),
                    mock.patch.object(peer_message, "_read_pending", side_effect=checked_pending),
                    mock.patch.object(peer_message, "_read_transfer_record", side_effect=checked_transfer),
                    mock.patch.object(peer_message, "_save_pending", side_effect=checked_save_pending),
                    mock.patch.object(peer_message, "_prepare_peer_message",
                                      side_effect=checked_prepare_message),
                    mock.patch.object(peer_obligations, "_write_atomic", side_effect=checked_write),
                    mock.patch.object(peer_obligations, "_read_record", side_effect=checked_read_record),
                    mock.patch.object(peer_obligations.ObligationStore, "list", checked_store_list),
                    mock.patch.object(peer_obligations.ObligationStore, "update", checked_store_update),
                    mock.patch.object(dispatch_registry_cache, "registry_lines",
                                      side_effect=checked_registry_lines),
                    mock.patch.object(dispatch_completion_join, "registry_lines",
                                      side_effect=checked_registry_lines),
                    mock.patch.object(dispatch_completion_join, "current_attempt_row",
                                      side_effect=checked_attempt_lookup),
                    mock.patch.object(PeerReadContext, "current_attempt_row", checked_context_lookup),
                    mock.patch.object(peer_steward, "_reconcile_obligations",
                                      side_effect=checked_reconcile),
                    mock.patch.object(peer_steward, "_resume_message_obligation",
                                      side_effect=checked_resume_message),
                    mock.patch.object(peer_steward, "_resume_registered_obligation",
                                      side_effect=checked_resume_registered),
                    mock.patch.object(peer_steward, "_retire_target",
                                      side_effect=fake_native_provider),
                    mock.patch.object(Path, "read_text", checked_path_read_text),
                    mock.patch.object(os, "open", side_effect=checked_os_open),
                    mock.patch.object(os, "replace", side_effect=checked_replace),
                    mock.patch.object(os, "unlink", side_effect=checked_unlink),
                    mock.patch.object(os, "kill", side_effect=checked_kill),
                    mock.patch.object(peer_steward.time, "sleep", side_effect=controlled_sleep),
                    mock.patch.object(peer_message, "deliver_pending_codex",
                                      side_effect=AssertionError("fixture attempted a native send")),
                    mock.patch.object(peer_steward, "cmd_prompt",
                                      side_effect=AssertionError("fixture attempted a prompt send")),
                    mock.patch.object(peer_obligations, "acknowledge_registered_delivery",
                                      side_effect=AssertionError("fixture attempted an ACK")),
                    mock.patch.object(peer_obligations, "ensure_normal_runner",
                                      side_effect=AssertionError("fixture attempted service activation")),
                    mock.patch.object(peer_obligations, "ensure_runner",
                                      side_effect=AssertionError("fixture attempted service activation")),
                    mock.patch.object(peer_obligations, "exec_service_runner",
                                      side_effect=AssertionError("fixture attempted service activation")),
                    mock.patch.object(peer_steward, "_ensure_watch_observers",
                                      side_effect=AssertionError("fixture attempted watch activation")),
                    mock.patch.object(dispatch_batch_obligations, "ensure_observers",
                                      side_effect=AssertionError("fixture attempted batch activation")),
                    mock.patch.object(peer_steward.subprocess, "run",
                                      side_effect=AssertionError("fixture attempted external transport")),
                )
                for patcher in patchers:
                    patch_stack.enter_context(patcher)
                started = time.monotonic()
                deadline = started + window
                before = process_ticks()
                start_tick = before[19]
                cpu_before = int(before[11]) + int(before[12])
                try:
                    runner_return = peer_steward.cmd_obligation_runner(runner_args)
                except MeasurementWindowEnded:
                    runner_ended_at_window = True
                after = process_ticks()
                cpu_after = int(after[11]) + int(after[12])
                elapsed = time.monotonic() - started
        finally:
            monitoring[0] = False
            phase[0] = "finished"
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertTrue(runner_ended_at_window, f"runner returned early with {runner_return!r}")
        self.assertEqual(after[19], start_tick)
        self.assertGreaterEqual(elapsed, 19.0)
        self.assertLessEqual(elapsed, 21.0)
        cpu_percent = (cpu_after - cpu_before) * 100.0 / (ticks * elapsed)
        self.assertGreaterEqual(counts["passes"], 4)
        self.assertEqual(counts["passes_before_input"], 3)
        self.assertEqual(counts["passes_after_input"], counts["passes"] - 3)
        self.assertEqual(counts["message_resumes"], 193 * 3 + 194 * counts["passes_after_input"])
        self.assertEqual(counts["registered_resumes"], 6 * counts["passes"])
        self.assertEqual(counts["store_list_calls"], 2 * counts["passes"])
        self.assertEqual(counts["pending_reads"], 193 * 3 + 194 * counts["passes_after_input"])
        self.assertEqual(counts["transfer_record_reads"], 188 * 3 + 189 * counts["passes_after_input"])
        self.assertEqual(counts["native_lookups"], 73 * 3 + 74 * counts["passes_after_input"])
        self.assertEqual(counts["attempt_row_lookups"], 7 * counts["passes"])
        self.assertEqual(counts["context_attempt_row_lookups"], 6 * counts["passes"])
        self.assertEqual(counts["root_discoveries"], counts["passes"])
        self.assertEqual(counts["root_candidates"], 3 * counts["passes"])
        self.assertEqual(counts["registry_line_calls"], 2 * counts["passes"])
        self.assertEqual(counts["jobs_file_reads"], 1)
        self.assertEqual(counts["obligation_writes"], 1,
                         "only the new input's real reconciliation should checkpoint")
        self.assertEqual(counts["input_obligation_writes"], 1)
        self.assertEqual(counts["input_pending_writes"], 1)
        self.assertEqual(counts["input_transfer_writes"], 1)
        expected_record_reads = (2 * (902 * counts["passes_before_input"]
                                      + 903 * counts["passes_after_input"])
                                 + counts["store_update_calls"] + counts["passes"])
        self.assertEqual(counts["store_record_reads"], expected_record_reads)
        self.assertEqual(counts["store_update_calls"], 1)
        self.assertTrue(discovered_roots)
        self.assertTrue(write_targets)
        self.assertTrue(all(Path(path).resolve(strict=False).is_relative_to(fixture_root)
                            for path in discovered_roots | write_targets | observed_roots))
        self.assertEqual(event["pass_at_create"], 3)
        self.assertIn("processed_at", event)
        self.assertLessEqual(event["processed_at"] - event["created_at"],
                             event["trigger_delay"] + 1.0)
        self.assertEqual(self.store.get(event["duty_id"])["state"], "unknown")
        self.assertEqual(self.store.get(event["duty_id"])["observation"]["reason"],
                         "target-identity-changed")
        self.assertTrue(all(self.store.get(duty_id)["state"] == "unknown"
                            for duty_id, _ref, _target in self.message_duties))
        final_registered = [self.store.get(duty_id)["state"] for duty_id in registered_ids]
        self.assertEqual(final_registered.count("unknown"), 5)
        self.assertEqual(final_registered.count("pending"), 1)
        notice_row = peer_message._read_pending(self.message_duties[187][1])
        self.assertEqual(notice_row["state"], "unverified")
        self.assertEqual(sum(bool(row.get("dispatch_notice")) for row in initial_rows), 1)
        print(
            "real-runner-workload-correction "
            f"window={elapsed:.2f}s HZ={ticks} one_core=100% child_cpu=excluded(no child spawned) "
            f"registry_bytes={self.jobs.stat().st_size} readable_transfers=188 recipient_sid_chars=36 "
            "unknown_messages=193 pending_registered_batches=6 completed_records=702 cancelled_records=1 "
            f"passes={counts['passes']} warmup_before_input={counts['passes_before_input']} "
            f"progress_after_input={counts['passes_after_input']} requested_sleeps={sleep_delays} "
            f"store_list_calls={counts['store_list_calls']} store_record_reads={counts['store_record_reads']} "
            f"message_resumes={counts['message_resumes']} registered_resumes={counts['registered_resumes']} "
            f"pending_reads={counts['pending_reads']} transfer_record_reads={counts['transfer_record_reads']} "
            f"native_lookups_fake={counts['native_lookups']} root_discoveries={counts['root_discoveries']} "
            f"root_candidates={counts['root_candidates']} attempt_row_lookups={counts['attempt_row_lookups']} "
            f"context_attempt_lookups={counts['context_attempt_row_lookups']} "
            f"registry_line_calls={counts['registry_line_calls']} jobs_file_reads={counts['jobs_file_reads']} "
            f"store_update_calls={counts['store_update_calls']} "
            f"unchanged_checkpoint_skips={counts['message_resumes'] - counts['store_update_calls']} "
            f"input_obligation_writes={counts['input_obligation_writes']} "
            f"runner_obligation_writes={counts['obligation_writes']} "
            f"input_pending_writes={counts['input_pending_writes']} "
            f"input_transfer_writes={counts['input_transfer_writes']} "
            f"write_open_calls={counts['write_open_calls']} replace_calls={counts['replace_calls']} "
            f"unlink_calls={counts['unlink_calls']} fixture_paths={len(observed_roots)} "
            f"event_resume={event['processed_at'] - event['created_at']:.3f}s "
            f"event_wait_remaining={event['trigger_delay']:.3f}s cpu={cpu_percent:.3f}% "
            "fake_provider=target-identity-changed; real holder /proc check; sends/ACK/ensure fail-fast"
        )

if __name__ == "__main__":
    unittest.main()
