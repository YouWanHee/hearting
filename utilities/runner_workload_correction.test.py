#!/usr/bin/env python3
"""Synthetic read-path measurement for the observed message/batch workload shape."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_registry_cache  # noqa: E402
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
                     self.home / ".cache", self.base / "release",
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

    def test_synthetic_20_second_operational_shape(self):
        registered_ids, _carrier, _attempt = self._seed_workload()
        self.assertGreaterEqual(self.jobs.stat().st_size, 22_000_000)
        self.assertLess(self.jobs.stat().st_size, 24_000_000)
        self.assertEqual(len(list(peer_message._pending_dir().glob("*.json"))), 188)
        self.assertEqual(sum(
            1 for duty_id, _ref, _target in self.message_duties
            if self.store.get(duty_id)["state"] in {"pending", "unknown"}), 193)
        self.assertEqual(sum(
            1 for duty_id in registered_ids
            if self.store.get(duty_id)["state"] in {"pending", "unknown"}), 6)

        original_roots = peer_message.steward_marker_roots
        original_pending = peer_message._read_pending
        original_transfer = peer_message._read_transfer_record
        original_write = peer_obligations._write_atomic
        original_read_text = Path.read_text
        original_registry_lines = dispatch_registry_cache.registry_lines
        counts = {"passes": 0, "root_discovery": 0, "root_candidates": 0,
                  "pending_reads": 0,
                  "transfer_reads": 0, "target_queries": 0, "atomic_updates": 0,
                  "jobs_file_reads": 0, "registry_line_calls": 0}
        measuring = [False]
        event = {}
        sleep_delays = []
        window = 20.0
        real_sleep = time.sleep

        def count_roots():
            roots = original_roots()
            if measuring[0]:
                counts["root_discovery"] += 1
                counts["root_candidates"] += len(roots[0])
            return roots

        def count_pending(ref, **kwargs):
            if measuring[0]:
                counts["pending_reads"] += 1
            return original_pending(ref, **kwargs)

        def count_transfer(ref, **kwargs):
            if measuring[0]:
                counts["transfer_reads"] += 1
            return original_transfer(ref, **kwargs)

        def count_write(path, value):
            if measuring[0] and Path(path).parent == self.store.root:
                counts["atomic_updates"] += 1
            return original_write(path, value)

        def count_read_text(path, *args, **kwargs):
            if measuring[0] and Path(path).resolve(strict=False) == self.jobs.resolve(strict=False):
                counts["jobs_file_reads"] += 1
            return original_read_text(path, *args, **kwargs)

        def count_registry(jobs):
            if measuring[0]:
                counts["registry_line_calls"] += 1
            return original_registry_lines(jobs)

        def synthetic_native_provider(target):
            if measuring[0]:
                counts["target_queries"] += 1
                if event and target == event["target"] and "processed_at" not in event:
                    event["processed_at"] = time.monotonic()
            return ("unknown", {"harness": "opencode", "session_id": "changed-session",
                                "name": target, "pane": f"synthetic-pane-{target[-3:]}"},
                    0, "target-identity-changed")

        def synthetic_read_pass():
            counts["passes"] += 1
            context = PeerReadContext()
            for duty in self.store.list():
                intent = duty.get("intent") or {}
                if intent.get("kind") == "registered-batch":
                    identity = intent.get("identity") or {}
                    context.current_attempt_row(Path(identity["jobs"]), identity["attempt_id"])
                    continue
                if intent.get("kind") != "message":
                    continue
                ref = intent.get("ref") or (duty.get("observation") or {}).get("ref")
                roots = context.transfer_roots(peer_message.steward_marker_roots)
                try:
                    row = peer_message._read_pending(ref, roots=roots)
                except (OSError, ValueError):
                    row = None
                if row is None:
                    self.store.update(duty["id"], state="unknown", skip_unchanged=True,
                                      observation={"ref": ref, "reason": "pending-row-missing"})
                    continue
                # Exercise exact notice validation against the synthetic jobs
                # registry. Its result is observed only; it never settles or
                # acknowledges the synthetic duty.
                if row.get("dispatch_notice"):
                    peer_obligations.registered_delivery_settled(row)
                if row.get("state") in {"queued", "unverified"}:
                    self.store.update(duty["id"], state="unknown", skip_unchanged=True,
                        observation={"ref": ref, "transport_state": row["state"],
                                     "reason": row.get("receipt", "delivery-unconfirmed")})
                elif row.get("state") == "pending":
                    _state, ident, _code, reason = synthetic_native_provider(
                        intent.get("target") or (row.get("to") or {}).get("name") or "")
                    self.store.update(duty["id"], state="unknown", skip_unchanged=True,
                        observation={"ref": ref, "reason": reason,
                                     "observed_pane": ident.get("pane", "")})

        def process_ticks():
            fields = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
            return fields[fields.rfind(")") + 2:].split()

        hz = os.sysconf("SC_CLK_TCK")
        before = process_ticks()
        start_tick = before[19]
        cpu_before = int(before[11]) + int(before[12])
        started = time.monotonic()
        deadline = started + window
        elapsed = 0.0
        try:
            with mock.patch.object(peer_message, "steward_marker_roots", side_effect=count_roots), \
                 mock.patch.object(peer_message, "_read_pending", side_effect=count_pending), \
                 mock.patch.object(peer_message, "_read_transfer_record", side_effect=count_transfer), \
                 mock.patch.object(peer_obligations, "_write_atomic", side_effect=count_write), \
                 mock.patch.object(peer_steward, "_reconcile_obligations",
                                   side_effect=AssertionError("measurement must stay read-only")), \
                 mock.patch.object(peer_steward, "cmd_obligation_runner",
                                   side_effect=AssertionError("measurement must stay synthetic")), \
                 mock.patch.object(peer_message, "deliver_pending_codex",
                                   side_effect=AssertionError("measurement must not send")), \
                 mock.patch.object(peer_obligations, "acknowledge_registered_delivery",
                                   side_effect=AssertionError("measurement must not ACK")), \
                 mock.patch.object(dispatch_registry_cache, "registry_lines",
                                   side_effect=count_registry), \
                 mock.patch.object(Path, "read_text", count_read_text), \
                 mock.patch.object(peer_steward.time, "sleep", side_effect=real_sleep):
                measuring[0] = True
                delay = 1.0
                while True:
                    previously_processed = "processed_at" in event
                    synthetic_read_pass()
                    if not previously_processed and "processed_at" in event:
                        delay = 1.0
                    if time.monotonic() >= deadline:
                        break
                    sleep_delays.append(delay)
                    if len(sleep_delays) == 3 and not event:
                        measuring[0] = False
                        duty_id, ref, target = self._message(193)
                        event.update({"duty_id": duty_id, "ref": ref, "target": target,
                                      "created_at": time.monotonic(), "trigger_delay": delay})
                        # New synthetic input arrives during a stable wait and
                        # is read on the next bounded wake interval.
                        measuring[0] = True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    real_sleep(min(delay, remaining))
                    if time.monotonic() >= deadline:
                        break
                    delay = min(15.0, delay * 1.7)
                measuring[0] = False
                elapsed = time.monotonic() - started
                after = process_ticks()
                cpu_after = int(after[11]) + int(after[12])
        finally:
            measuring[0] = False

        self.assertEqual(after[19], start_tick)
        cpu_percent = (cpu_after - cpu_before) * 100.0 / (hz * elapsed)
        self.assertGreaterEqual(elapsed, 19.0)
        self.assertGreaterEqual(counts["passes"], 4)
        self.assertEqual(counts["root_discovery"], counts["passes"])
        self.assertGreater(counts["root_candidates"], 0)
        self.assertEqual(counts["root_candidates"] % counts["root_discovery"], 0)
        self.assertGreaterEqual(counts["pending_reads"], 193 * counts["passes"])
        self.assertLessEqual(counts["pending_reads"], 194 * counts["passes"])
        self.assertGreaterEqual(counts["transfer_reads"], 188 * counts["passes"])
        self.assertLessEqual(counts["target_queries"], 74 * counts["passes"])
        self.assertEqual(counts["atomic_updates"], 1,
                         "only the newly accepted duty needs its first checkpoint write")
        self.assertEqual(counts["jobs_file_reads"], 1)
        self.assertIn("processed_at", event)
        self.assertLessEqual(event["processed_at"] - event["created_at"],
                             event["trigger_delay"] + 1.0)
        self.assertEqual(self.store.get(event["duty_id"])["state"], "unknown")
        self.assertEqual(self.store.get(event["duty_id"])["observation"]["reason"],
                         "target-identity-changed")
        self.assertTrue(all(self.store.get(duty_id)["state"] == "unknown"
                            for duty_id, _ref, _target in self.message_duties))
        self.assertEqual(sum(
            self.store.get(duty_id)["state"] in {"pending", "unknown"}
            for duty_id in registered_ids), 6)
        notice_entry = self.message_duties[187]
        notice_row = peer_message._read_pending(notice_entry[1])
        self.assertEqual(notice_row["state"], "unverified")
        print(
            "synthetic-read-workload-correction "
            f"window={elapsed:.2f}s HZ={hz} one_core=100% child_cpu=excluded(no child spawned) "
            f"registry_bytes={self.jobs.stat().st_size} readable_transfers=188 "
            f"recipient_sid_chars={len(self.active_session_id)} "
            "unknown_messages=193 pending_registered_batches=6 completed_records=702 "
            f"passes={counts['passes']} root_discovery={counts['root_discovery']} "
            f"roots_per_scan={counts['root_candidates'] // counts['root_discovery']} "
            f"pending_reads={counts['pending_reads']} transfer_reads={counts['transfer_reads']} "
            f"target_queries={counts['target_queries']} atomic_updates={counts['atomic_updates']} "
            f"jobs_file_reads={counts['jobs_file_reads']} registry_line_calls={counts['registry_line_calls']} "
            f"event_resume={event['processed_at'] - event['created_at']:.3f}s "
            f"cpu={cpu_percent:.3f}% fake_native_provider=identity-change transport=unused"
        )


if __name__ == "__main__":
    unittest.main()
