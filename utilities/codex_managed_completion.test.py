#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "utilities" / "codex-managed-completion.py"
PARENT = "att-parent"
SESSION = "thread-managed-parent"


def load_completion_module():
    sys.path.insert(0, str(ROOT / "utilities"))
    spec = importlib.util.spec_from_file_location(
        "codex_managed_completion_unit", SIDECAR
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def row(
    attempt_id: str,
    *,
    harness: str,
    status: str = "done",
    parent: str = PARENT,
) -> str:
    return (
        f"2026-07-27T00:00:00Z\t{status}\t/repo\t/wt\tchild\t"
        "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        f"attempt_id={attempt_id},parent_attempt_id={parent},"
        f"harness={harness},note=RAW_CHILD_SENTINEL\n"
    )


def session_row(attempt_id: str, *, harness: str) -> str:
    return (
        f"2026-07-27T00:00:00Z\tdone\t/repo\t/wt\tchild\t"
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,"
        "launch_claimed=1,launch_outcome=never-launched,"
        "parent_completion_delivery=codex-managed-gateway,"
        f"attempt_id={attempt_id},parent_sid={SESSION},harness={harness},"
        "note=RAW_SESSION_CHILD_SENTINEL\n"
    )


class ControlServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.request: dict[str, Any] | None = None
        self.called = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            data = bytearray()
            while b"\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
            request = json.loads(bytes(data).split(b"\n", 1)[0])
            if request.get("op") == "status":
                response = {
                    "schema_version": 1, "status": "ready",
                    "capabilities": {"human_gate_delivery": {
                        "version": 1, "thread_id": SESSION, "epoch": 1,
                    }},
                }
            else:
                self.request = request
                self.called.set()
                response = {
                    "schema_version": 1,
                    "status": "accepted",
                    "delivery_id": "dlv-fixture",
                    "action": "start",
                    "replay": False,
                }
            connection.sendall(
                (json.dumps(response, separators=(",", ":")) + "\n").encode()
            )
            connection.close()
            if request.get("op") != "status":
                return

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=2)


class ReplacementAuthorityTest(unittest.TestCase):
    def test_requires_current_epoch_generation_and_exact_parent(self):
        from argparse import Namespace
        module = load_completion_module()
        args = Namespace(jobs=Path("/tmp/jobs.log"), parent_session_id="sid", control_socket=Path("/tmp/socket"),
                         gateway_epoch=7, binding_generation=3)
        metadata = {"dispatch_depth": "1", "parent_sid": "sid", "parent_completion_delivery": module.MANAGED_SESSION_PARENT_DELIVERY}
        status = {"status": "ready", "tui_connected": True, "thread_id": "sid", "epoch": 7, "binding_generation": 3}
        with mock.patch.object(module, "gateway_request", return_value=status):
            self.assertTrue(module.replacement_authority(args, args.jobs, "att-old", metadata))
            self.assertFalse(module.replacement_authority(args, args.jobs, "att-old", dict(metadata, parent_sid="foreign")))
            status["binding_generation"] = 4
            self.assertFalse(module.replacement_authority(args, args.jobs, "att-old", metadata))
            status["binding_generation"] = 3
            status["tui_connected"] = False
            self.assertFalse(module.replacement_authority(args, args.jobs, "att-old", metadata))


class ManagedCompletionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.control_path = self.base / "control.sock"
        self.join = self.base / "fake_join.py"
        self.join_calls = self.base / "join-calls.log"
        self.join.write_text(
            """\
import json, sys, pathlib
pathlib.Path(__file__).with_name('join-calls.log').open('a').write('x\\n')
mode = sys.argv[1]
if '--parent-session-id' in sys.argv:
    identity_key = 'parent_session_id'
    parent = sys.argv[sys.argv.index('--parent-session-id') + 1]
else:
    identity_key = 'parent_attempt_id'
    parent = sys.argv[sys.argv.index('--parent-attempt-id') + 1]
attempts = [sys.argv[i + 1] for i, value in enumerate(sys.argv) if value == '--attempt-id']
state = 'timeout' if mode == 'timeout' else 'ready'
children = [
    {
        'attempt_id': attempt,
        'status': 'open' if mode in {'timeout', 'terminal'} else 'done',
        'readiness': 'ready' if state == 'ready' else 'pending',
        'reason': (
            'terminal-observed' if mode == 'terminal'
            else 'closure-blocked:completion-attempt-not-current' if mode == 'closure-blocked'
            else 'registry-closed' if state == 'ready'
            else 'process-alive'
        ),
        'required_action': (
            'complete-open' if mode in {'timeout', 'terminal'}
            else 'inspect-recovery' if mode == 'closure-blocked'
            else 'advance-completed'
        ),
        'slug': 'child',
    }
    for attempt in attempts
]
print(json.dumps({
    'schema_version': 2,
    'state': state,
    identity_key: parent,
    'children': children,
}))
raise SystemExit(3 if state == 'timeout' else 0)
""",
            encoding="utf-8",
        )

    def command(
        self,
        attempts: list[str],
        *,
        mode: str = "ready",
        batch: str = "batch-1",
        timeout: str = "0.1",
    ) -> list[str]:
        command = [
            sys.executable,
            str(SIDECAR),
            "--control-socket",
            str(self.control_path),
            "--jobs",
            str(self.jobs),
            "--parent-attempt-id",
            PARENT,
            "--sealed-batch-id",
            batch,
            "--interval",
            "0.01",
            "--timeout",
            timeout,
            "--join-command",
            f"{sys.executable} {self.join} {mode}",
        ]
        for attempt in attempts:
            command += ["--attempt-id", attempt]
        return command

    def session_command(
        self,
        attempts: list[str],
        *,
        retry_window: float = 0.0,
        launch_ready_timeout: float = 1.0,
    ) -> list[str]:
        command = [
            sys.executable,
            str(SIDECAR),
            "--control-socket",
            str(self.control_path),
            "--jobs",
            str(self.jobs),
            "--parent-session-id",
            SESSION,
            "--thread-id",
            SESSION,
            "--sealed-batch-id",
            "batch-session",
            "--interval",
            "0.01",
            "--timeout",
            "0.1",
            "--launch-ready-timeout",
            str(launch_ready_timeout),
            "--delivery-retry-window",
            str(retry_window),
            "--delivery-retry-interval",
            "0.02",
            "--join-command",
            f"{sys.executable} {self.join} ready",
        ]
        for attempt in attempts:
            command += ["--attempt-id", attempt]
        return command

    def test_human_gate_watcher_rechecks_exact_batch_and_uses_gateway_identity(self) -> None:
        module = load_completion_module()
        self.jobs.write_text("fixture\n", encoding="utf-8")
        record_path = self.base / "delivery.json"
        receipt = {
            "kind": "human-gate", "owner_attempt_id": "att-owner",
            "sealed_batch_id": "batch-session",
        }
        record = {"delivery_id": "delivery-pending", "receipt_digest": "sha256:x",
                  "receipt": receipt}
        record_path.write_text(json.dumps(record), encoding="utf-8")
        args = type("Args", (), {
            "jobs": self.jobs, "parent_session_id": SESSION,
            "sealed_batch_id": "batch-session", "control_socket": self.control_path,
            "interval": 0.01,
        })()
        watcher = module.HumanGateWatcher(
            args, {"att-owner"}, {"epoch": 9}
        )
        claimed = dict(record)
        with mock.patch.object(module.human_gate_receipt, "validate_pending_record") as validate, \
             mock.patch.object(module, "negotiate_human_gate", return_value={"epoch": 9}), \
             mock.patch.object(module.human_gate_receipt, "gateway_delivery_id",
                               return_value="hg-dlv-exact"), \
             mock.patch.object(module.pending_delivery, "claim", return_value=claimed), \
             mock.patch.object(module.pending_delivery, "ack") as ack, \
             mock.patch.object(module, "gateway_request",
                               return_value={"schema_version": 1, "status": "accepted"}) as send:
            watcher._one(record_path)
        self.assertEqual(validate.call_count, 2)
        for call in validate.call_args_list:
            self.assertEqual(call.kwargs["expected_attempts"], {"att-owner"})
            self.assertEqual(call.kwargs["expected_epoch"], 9)
            self.assertEqual(call.kwargs["expected_sealed_batch_id"], "batch-session")
        self.assertEqual(send.call_args.args[1]["delivery_id"], "hg-dlv-exact")
        ack.assert_called_once_with(
            self.jobs.parent, SESSION, "delivery-pending", acked_by=watcher.owner
        )

    def test_watcher_error_diagnostics_are_bounded(self) -> None:
        module = load_completion_module()
        args = type("Args", (), {"jobs": self.jobs, "parent_session_id": SESSION,
                                  "interval": 0.01})()
        watcher = module.HumanGateWatcher(args, {PARENT}, {"epoch": 1})
        for index in range(100):
            watcher._record_error(f"malformed-{index}")
        self.assertLessEqual(len(watcher.errors), 32)
        self.assertEqual(watcher.error_count_total, 100)
        self.assertLessEqual(len(watcher.error_counts), 64)
        self.assertEqual(sum(watcher.error_counts.values()), 100)

    def test_codex_and_claude_children_share_one_bounded_receipt(self) -> None:
        attempts = ["att-codex", "att-claude"]
        self.jobs.write_text(
            row(attempts[0], harness="codex")
            + row(attempts[1], harness="claude"),
            encoding="utf-8",
        )
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = subprocess.run(
            self.command(attempts),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        children = server.request["receipt"]["children"]
        self.assertEqual(server.request["receipt"]["schema_version"], 2)
        self.assertEqual(server.request["receipt"]["job_registry"], str(self.jobs))
        self.assertEqual(
            {child["harness"] for child in children},
            {"codex", "claude"},
        )
        self.assertEqual(
            {child["required_action"] for child in children},
            {"inspect-done-failure"},
        )
        self.assertEqual(
            {child["delivery_classification"] for child in children},
            {"attention"},
        )
        timing = server.request["receipt"]["delivery_timing"]
        self.assertEqual(timing["delivery_timing_schema_version"], 1)
        self.assertIsInstance(timing["last_child_terminal_ns"], int)
        self.assertIsInstance(timing["join_completed_ns"], int)
        self.assertEqual(
            set(timing),
            {
                "delivery_timing_schema_version",
                "last_child_terminal_ns",
                "join_completed_ns",
                "same_thread_resume_ns",
                "exact_harvest_ns",
                "next_stage_start_ns",
                "final_report_marker_ns",
                "owner_terminal_envelope_ns",
            },
        )
        encoded = json.dumps(server.request)
        self.assertNotIn("RAW_CHILD_SENTINEL", encoded)
        self.assertLessEqual(
            len(
                json.dumps(
                    server.request["receipt"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
            2048,
        )

    def test_symlinked_registry_is_rejected_before_gateway(self) -> None:
        target = self.base / "real-jobs.log"
        target.write_text(row("att-one", harness="codex"), encoding="utf-8")
        self.jobs.symlink_to(target)
        result = subprocess.run(
            self.command(["att-one"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("jobs-path-invalid", result.stdout)
        self.assertFalse(self.control_path.exists())

    def test_timeout_retains_completion_carrier_without_terminal_delivery(self) -> None:
        attempts = ["att-a", "att-b"]
        self.jobs.write_text(row(attempts[0], harness="codex", status="open")
                             + row(attempts[1], harness="claude", status="open"), encoding="utf-8")
        before = self.jobs.read_bytes()
        # `--timeout` also seeds the sidecar's own unfinishable-watch deadline
        # (item 8) now, distinct from an ordinary per-join timeout the fake
        # join script below ignores entirely (it always returns immediately)
        # -- a large value here keeps this within-process-lifetime assertion
        # meaningful instead of exercising the deadline this test is not
        # about (see test_sidecar_stops_at_own_deadline for that).
        process = subprocess.Popen(self.command(attempts, mode="timeout", timeout="30"),
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.7)
            self.assertIsNone(process.poll())
            self.assertEqual(self.jobs.read_bytes(), before)
            self.assertFalse(self.control_path.exists())
        finally:
            process.terminate()
            out, err = process.communicate(timeout=5)
        self.assertNotIn('"status": "delivered"', out)

    def test_sidecar_stops_at_own_deadline(self) -> None:
        # plan.md item 8, defect (3): `--timeout` is now also the sidecar's
        # own unfinishable-watch deadline -- a join that never settles must
        # not keep this process running forever, unlike the ordinary
        # "keeps retaining the completion carrier" case above.
        attempts = ["att-a", "att-b"]
        self.jobs.write_text(row(attempts[0], harness="codex", status="open")
                             + row(attempts[1], harness="claude", status="open"), encoding="utf-8")
        result = subprocess.run(
            self.command(attempts, mode="timeout", timeout="0.05"),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "retryable")
        self.assertEqual(payload["reason"], "watch-deadline")
        # No gateway was ever contacted -- the deadline path returns before
        # `normalize_receipt`/`deliver_with_retry` run at all.
        self.assertFalse(self.control_path.exists())

    def test_sidecar_stops_when_receiver_gone_and_rows_terminal(self) -> None:
        # plan.md item 8, defect (4): the gateway is unreachable (ENOENT --
        # no server was ever started at this control-socket path) and every
        # monitored attempt is already terminal, so the sidecar gives up on
        # the very first join timeout instead of waiting out its own
        # deadline (deliberately set far larger than this test's runtime).
        attempts = ["att-a", "att-b"]
        self.jobs.write_text(row(attempts[0], harness="codex", status="done")
                             + row(attempts[1], harness="claude", status="done"), encoding="utf-8")
        result = subprocess.run(
            self.command(attempts, mode="timeout", timeout="30"),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "retryable")
        self.assertEqual(payload["reason"], "receiver-unavailable")

    def test_sidecar_delivers_closure_blocked_receipt_once_without_relaunching_join(self) -> None:
        # item 8, unfinishable-watch: when the join itself already returns a
        # typed ready receipt for a proven-permanent block (closure-blocked),
        # the sidecar must deliver it on the very first join call and exit --
        # not wait out its own --timeout deadline (set far larger than this
        # test's runtime) or call the join a second time.
        attempts = ["att-a"]
        self.jobs.write_text(row(attempts[0], harness="codex", status="done"), encoding="utf-8")
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = subprocess.run(
            self.command(attempts, mode="closure-blocked", timeout="30"),
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "accepted")
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        child = server.request["receipt"]["children"][0]
        self.assertEqual(child["reason"], "closure-blocked:completion-attempt-not-current")
        self.assertEqual(child["required_action"], "inspect-recovery")
        self.assertEqual(
            self.join_calls.read_text(encoding="utf-8").count("x\n"), 1,
        )

    def test_terminal_observed_open_child_keeps_actionable_status(self) -> None:
        self.jobs.write_text(
            row("att-terminal", harness="codex", status="open"),
            encoding="utf-8",
        )
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = subprocess.run(
            self.command(["att-terminal"], mode="terminal"),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        child = server.request["receipt"]["children"][0]
        self.assertEqual(child["status"], "open")
        self.assertEqual(child["reason"], "terminal-failure-or-unclosed")
        self.assertEqual(child["required_action"], "complete-open")

    def test_foreign_or_missing_attempt_fails_before_gateway(self) -> None:
        self.jobs.write_text(
            row("att-current", harness="codex")
            + row(
                "att-foreign",
                harness="claude",
                parent="att-foreign-parent",
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            self.command(["att-current", "att-foreign"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "rejected")
        self.assertFalse(self.control_path.exists())

    def test_duplicate_attempt_argument_fails_closed(self) -> None:
        self.jobs.write_text(
            row("att-one", harness="codex"), encoding="utf-8"
        )
        result = subprocess.run(
            self.command(["att-one", "att-one"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("attempt-set-invalid", result.stdout)

    def test_direct_managed_sibling_children_use_hashed_parent(self) -> None:
        attempts = ["att-direct-codex", "att-direct-claude", "att-direct-opencode"]
        self.jobs.write_text(
            session_row(attempts[0], harness="codex")
            + session_row(attempts[1], harness="claude")
            + session_row(attempts[2], harness="opencode"),
            encoding="utf-8",
        )
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)
        result = subprocess.run(
            self.session_command(attempts),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        assert server.request is not None
        self.assertEqual(server.request["thread_id"], SESSION)
        self.assertTrue(
            server.request["parent_attempt_id"].startswith("parent-session-")
        )
        encoded = json.dumps(server.request)
        self.assertNotIn("RAW_SESSION_CHILD_SENTINEL", encoded)
        self.assertEqual(
            {child["harness"] for child in server.request["receipt"]["children"]},
            {"codex", "claude", "opencode"},
        )

    def test_retryable_disconnect_reconnect_sends_once_after_server_appears(self) -> None:
        self.jobs.write_text(
            session_row("att-reconnect", harness="codex"),
            encoding="utf-8",
        )
        holder: dict[str, ControlServer] = {}

        def delayed_server() -> None:
            import time
            time.sleep(0.08)
            holder["server"] = ControlServer(self.control_path)

        thread = threading.Thread(target=delayed_server)
        thread.start()
        result = subprocess.run(
            self.session_command(["att-reconnect"], retry_window=1.0),
            text=True,
            capture_output=True,
            timeout=5,
        )
        thread.join(timeout=2)
        server = holder["server"]
        self.addCleanup(server.close)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))
        self.assertEqual(server.request["sealed_batch_id"], "batch-session")

    def test_prelaunched_sidecar_waits_for_atomic_worker_claim(self) -> None:
        unclaimed = session_row("att-prelaunch", harness="codex").replace(
            "\tdone\t", "\topen\t"
        ).replace(
            "launch_claimed=1,launch_outcome=never-launched,",
            "launch_claimed=0,",
        )
        claimed = unclaimed.replace("launch_claimed=0", "launch_claimed=1")
        self.jobs.write_text(unclaimed, encoding="utf-8")
        server = ControlServer(self.control_path)
        self.addCleanup(server.close)

        def claim_worker() -> None:
            import time
            time.sleep(0.08)
            self.jobs.write_text(claimed, encoding="utf-8")

        thread = threading.Thread(target=claim_worker)
        thread.start()
        result = subprocess.run(
            self.session_command(["att-prelaunch"]),
            text=True,
            capture_output=True,
            timeout=5,
        )
        thread.join(timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(server.called.wait(2))

    def test_never_launched_registration_fails_before_gateway(self) -> None:
        never_launched = session_row(
            "att-never-launched", harness="claude"
        ).replace("launch_claimed=1", "launch_claimed=0")
        self.jobs.write_text(never_launched, encoding="utf-8")
        result = subprocess.run(
            self.session_command(
                ["att-never-launched"], launch_ready_timeout=0.1
            ),
            text=True,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 65)
        self.assertIn("registered-child-never-launched", result.stdout)
        self.assertFalse(self.control_path.exists())


class NormalizeReceiptStageAdvanceNegotiationTest(unittest.TestCase):
    """SD-110 A-18: an un-negotiated (default) call to `normalize_receipt`
    takes the literal, unmodified v2 path -- golden-byte identical to the
    pre-SD-110 receipt."""

    def setUp(self):
        self.module = load_completion_module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name) / "jobs.tsv"
        self.jobs.write_text(row("att-child", harness="codex"), encoding="utf-8")

    def _raw_receipt(self):
        return {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": PARENT,
            "children": [
                {
                    "attempt_id": "att-child",
                    "status": "done",
                    "readiness": "ready",
                    "reason": "registry-closed",
                    "required_action": "advance-completed",
                }
            ],
        }

    def _normalize(self, **extra):
        return self.module.normalize_receipt(
            self._raw_receipt(),
            jobs=self.jobs,
            parent_attempt_id=PARENT,
            parent_session_id=None,
            delivery_parent_id=PARENT,
            attempts={"att-child"},
            **extra,
        )

    @staticmethod
    def _without_live_timing(value):
        # `delivery_timing` embeds `time.monotonic_ns()` observed at call
        # time, so two independent calls legitimately differ there. The
        # byte-identity claim under test is about `schema_version` and the
        # `stage_advance` key, not wall-clock timing -- mask it out.
        masked = dict(value)
        masked["delivery_timing"] = "MASKED"
        return masked

    def test_attention_cannot_borrow_failure_from_another_batch(self):
        self.jobs.write_text(self.jobs.read_text() + row("att-foreign", harness="codex"))
        receipt = self._raw_receipt()
        receipt["replacement_attention"] = [{"source_attempt_id": "att-foreign", "state": "needs-attention",
            "node": "__owner__", "reason": "replacement-input-unproven"}]
        with self.assertRaisesRegex(ValueError, "replacement-attention-scope"):
            self.module.normalize_receipt(receipt, jobs=self.jobs, parent_attempt_id=PARENT,
                parent_session_id=None, delivery_parent_id=PARENT, attempts={"att-child"})

    def test_default_call_is_byte_identical_to_pre_sd110(self):
        normalized = self._normalize()
        golden = json.dumps(self._without_live_timing(normalized), sort_keys=True)
        negotiated_but_recordless = self._normalize(accept_stage_advance=True)
        self.assertEqual(normalized["schema_version"], 2)
        self.assertNotIn("stage_advance", normalized)
        self.assertEqual(
            json.dumps(
                self._without_live_timing(negotiated_but_recordless), sort_keys=True
            ),
            golden,
        )
        # SD-119: the chain-advance path exists but a join with no
        # sub-session chain metadata is a no-op -- this receipt never carries
        # a chain key, byte-identical to pre-SD-119.
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_subsession_advance as subsession_advance
        from types import SimpleNamespace

        no_chain = subsession_advance.coordinate_chain_advance_from_joined_rows(
            self.jobs, PARENT, {"att-child": SimpleNamespace(
                attempt_id="att-child", status="done", metadata={},
            )},
        )
        self.assertIsNone(no_chain)
        self.assertNotIn("chain_id", json.dumps(normalized, sort_keys=True))

    def test_negotiated_advanced_record_attaches_v3_block(self):
        record = {
            "schema_version": 1,
            "stage_advance_id": "sadv-" + "0" * 64,
            "route_id": "rt-0000000000000000",
            "route_hash": "sha256:" + "0" * 64,
            "predecessor_node": "plan",
            "predecessor_terminal_attempt_id": "att-plan",
            "successor_node": "execute",
            "successor_attempt_id": "att-execute",
            "claim_key": ["sha256:" + "0" * 64, "execute", 0],
            "brief_template_digest": "sha256:" + "1" * 64,
            "outcome": "advanced",
            "reason": "",
            "registered": True,
            "started": True,
            "child_spawned": True,
        }
        normalized = self._normalize(
            accept_stage_advance=True, stage_advance_record=record
        )
        self.assertEqual(normalized["schema_version"], 3)
        self.assertEqual(normalized["stage_advance"], record)


class NativeQueueDeliveryTest(unittest.TestCase):
    def test_queue_acceptance_uses_exact_id_and_restarts_only_while_pending(self) -> None:
        from types import SimpleNamespace

        module = load_completion_module()
        receipt = {"schema_version": 2, "state": "ready", "children": []}
        delivery_id = "delivery-ledger-one"
        delivery_material = "\0".join((
            "codex-native-queue-v1", SESSION, delivery_id,
        ))
        client_id = "delivery-" + hashlib.sha256(
            delivery_material.encode("utf-8")
        ).hexdigest()[:32]
        item = {"id": "queue-item-1", "clientUserMessageId": client_id}
        row = SimpleNamespace(
            attempt_id="att-one", raw="terminal-row",
            metadata={"delivery_id": delivery_id,
                      "delivery_recipient_kind": "codex-native-queue",
                      "delivery_row_revision": "revision",
                      "delivery_receipt_digest": "sha256:record-digest"},
        )
        ledger_record = {
            "delivery_id": delivery_id, "recipient_kind": "codex-native-queue",
            "attempt_ids": ["att-one"], "receipt_digest": "sha256:record-digest",
            "row_revisions": {"att-one": "revision"}, "state": "pending",
        }
        args = SimpleNamespace(
            queue_socket=Path("/tmp/app-server.sock"),
            thread_id=SESSION,
            sealed_batch_id="batch-exact",
            jobs=Path("/tmp/jobs.log"),
            timeout=3.0,
            interval=0.01,
            delivery_retry_interval=0.01,
        )
        with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                "thread": {"id": SESSION, "cwd": "/tmp/repo"}}), \
             mock.patch.object(module, "current_session_children", return_value=[row]), \
             mock.patch.object(module.pending_delivery, "read", return_value=ledger_record), \
             mock.patch.object(module.pending_delivery, "claim") as claim, \
             mock.patch.object(module.pending_delivery, "release_claim") as release, \
             mock.patch.object(module.pending_delivery, "mark_sent_ambiguous") as ambiguous, \
             mock.patch.object(module.pending_delivery, "ack") as ack, \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=[{
                 "status": "queued", "queued_submission_id": item["id"]},
                 {"status": "consumed"}]) as send, \
             mock.patch.object(module.codex_queue_delivery, "list_queue", side_effect=[[item], []]), \
             mock.patch.object(module.codex_queue_delivery,
                               "find_turn_by_client_message_id", return_value={"id": "turn-1"}), \
             mock.patch.object(module.codex_queue_delivery, "_latest_turn_status", return_value="interrupted"), \
             mock.patch.object(module.codex_queue_delivery, "poll_owned_item") as restart, \
             mock.patch.object(module.time, "sleep"):
            result = module.deliver_to_native_queue(
                args, receipt, {"att-one"}
            )
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(send.call_args.kwargs["client_message_id"].startswith("delivery-"))
        self.assertIn("AGENT_HARNESS_COMPLETION_V1", send.call_args.kwargs["message"])
        self.assertEqual(claim.call_count, 2)
        self.assertEqual(release.call_count, 2)
        ambiguous.assert_called_once()
        ack.assert_called_once()
        self.assertEqual(send.call_count, 1)
        restart.assert_not_called()  # Transport owns exact restart decisions.


    def test_accepted_send_is_not_repeated_during_history_outage(self) -> None:
        from types import SimpleNamespace

        module = load_completion_module()
        receipt = {"schema_version": 2, "state": "ready", "children": []}
        delivery_id = "delivery-ledger-one"
        delivery_material = "\0".join((
            "codex-native-queue-v1", SESSION, delivery_id,
        ))
        client_id = "delivery-" + hashlib.sha256(
            delivery_material.encode("utf-8")
        ).hexdigest()[:32]
        item = {"id": "queue-item-1", "clientUserMessageId": client_id}
        row = SimpleNamespace(
            attempt_id="att-one", raw="terminal-row",
            metadata={"delivery_id": delivery_id,
                      "delivery_recipient_kind": "codex-native-queue",
                      "delivery_row_revision": "revision",
                      "delivery_receipt_digest": "sha256:record-digest"},
        )
        ledger_record = {
            "delivery_id": delivery_id, "recipient_kind": "codex-native-queue",
            "attempt_ids": ["att-one"], "receipt_digest": "sha256:record-digest",
            "row_revisions": {"att-one": "revision"}, "state": "pending",
        }
        args = SimpleNamespace(
            queue_socket=Path("/tmp/app-server.sock"),
            thread_id=SESSION,
            sealed_batch_id="batch-exact",
            jobs=Path("/tmp/jobs.log"),
            timeout=3.0,
            interval=0.01,
            delivery_retry_interval=0.01,
        )
        with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                "thread": {"id": SESSION, "cwd": "/tmp/repo"}}), \
             mock.patch.object(module, "current_session_children", return_value=[row]), \
             mock.patch.object(module.pending_delivery, "read", return_value=ledger_record), \
             mock.patch.object(module.pending_delivery, "claim") as claim, \
             mock.patch.object(module.pending_delivery, "release_claim") as release, \
             mock.patch.object(module.pending_delivery, "mark_sent_ambiguous") as ambiguous, \
             mock.patch.object(module.pending_delivery, "ack") as ack, \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once", side_effect=[{
                 "status": "queued", "queued_submission_id": item["id"]},
                 {"status": "consumed"}]) as send, \
             mock.patch.object(module.codex_queue_delivery, "list_queue", side_effect=[[item], []]), \
             mock.patch.object(module.codex_queue_delivery,
                               "find_turn_by_client_message_id", side_effect=[
                                   module.codex_queue_delivery.QueueDeliveryError("history-unavailable"),
                                   module.codex_queue_delivery.QueueDeliveryError("history-unavailable"),
                                   {"id": "turn-1"}]), \
             mock.patch.object(module.codex_queue_delivery, "_latest_turn_status", return_value="interrupted"), \
             mock.patch.object(module.codex_queue_delivery, "poll_owned_item", return_value={"pending": False, "started": False}) as restart, \
             mock.patch.object(module.time, "sleep"):
            result = module.deliver_to_native_queue(
                args, receipt, {"att-one"}
            )
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(send.call_args.kwargs["client_message_id"].startswith("delivery-"))
        self.assertIn("AGENT_HARNESS_COMPLETION_V1", send.call_args.kwargs["message"])
        self.assertEqual(claim.call_count, 4)
        self.assertEqual(release.call_count, 4)
        self.assertEqual(ambiguous.call_count, 3)
        ack.assert_called_once()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(restart.call_count, 2)  # Consumed item absent, history temporarily unavailable.


    def test_refused_transport_releases_its_exact_claim_before_returning(self) -> None:
        from types import SimpleNamespace

        module = load_completion_module()
        receipt = {"schema_version": 2, "state": "ready", "children": []}
        delivery_id = "delivery-ledger-one"
        delivery_material = "\0".join((
            "codex-native-queue-v1", SESSION, delivery_id,
        ))
        client_id = "delivery-" + hashlib.sha256(
            delivery_material.encode("utf-8")
        ).hexdigest()[:32]
        item = {"id": "queue-item-1", "clientUserMessageId": client_id}
        row = SimpleNamespace(
            attempt_id="att-one", raw="terminal-row",
            metadata={"delivery_id": delivery_id,
                      "delivery_recipient_kind": "codex-native-queue",
                      "delivery_row_revision": "revision",
                      "delivery_receipt_digest": "sha256:record-digest"},
        )
        ledger_record = {
            "delivery_id": delivery_id, "recipient_kind": "codex-native-queue",
            "attempt_ids": ["att-one"], "receipt_digest": "sha256:record-digest",
            "row_revisions": {"att-one": "revision"}, "state": "pending",
        }
        args = SimpleNamespace(
            queue_socket=Path("/tmp/app-server.sock"),
            thread_id=SESSION,
            sealed_batch_id="batch-exact",
            jobs=Path("/tmp/jobs.log"),
            timeout=3.0,
            interval=0.01,
            delivery_retry_interval=0.01,
        )
        with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                "thread": {"id": SESSION, "cwd": "/tmp/repo"}}), \
             mock.patch.object(module, "current_session_children", return_value=[row]), \
             mock.patch.object(module.pending_delivery, "read", return_value=ledger_record), \
             mock.patch.object(module.pending_delivery, "claim") as claim, \
             mock.patch.object(module.pending_delivery, "release_claim") as release, \
             mock.patch.object(module.pending_delivery, "mark_sent_ambiguous") as ambiguous, \
             mock.patch.object(module.pending_delivery, "ack") as ack, \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once",
                 side_effect=module.codex_queue_delivery.QueueDeliveryError("queue-start-refused")):
            with self.assertRaisesRegex(module.codex_queue_delivery.QueueDeliveryError, "queue-start-refused"):
                module._native_queue_delivery_once(args, receipt, {"att-one"})
        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs["claim_owner"], claim.call_args.kwargs["claim_owner"])
        ack.assert_not_called()

    def test_partial_ledger_ack_closes_the_same_batch_without_resending(self) -> None:
        from types import SimpleNamespace

        module = load_completion_module()
        receipt = {"schema_version": 2, "state": "ready", "children": []}
        ids = ["delivery-ledger-a", "delivery-ledger-b"]
        material = "\0".join(("codex-native-queue-v1", SESSION, *ids))
        client_id = "delivery-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        rows = [SimpleNamespace(
            attempt_id=f"att-{index}", raw=f"row-{index}",
            metadata={"delivery_id": delivery_id,
                      "delivery_recipient_kind": "codex-native-queue",
                      "delivery_row_revision": "revision",
                      "delivery_receipt_digest": f"digest-{index}"},
        ) for index, delivery_id in enumerate(ids)]
        records = [{
            "delivery_id": delivery_id, "recipient_kind": "codex-native-queue",
            "attempt_ids": [f"att-{index}"], "receipt_digest": f"digest-{index}",
            "row_revisions": {f"att-{index}": "revision"},
            "state": "acked" if index == 0 else "sent-ambiguous",
            "acked_by": f"codex-native-queue:{client_id}" if index == 0 else None,
        } for index, delivery_id in enumerate(ids)]
        read_records = [*records, *records]
        args = SimpleNamespace(
            queue_socket=Path("/tmp/app-server.sock"), thread_id=SESSION,
            sealed_batch_id="batch-exact", jobs=Path("/tmp/jobs.log"),
            timeout=2.0, interval=0.01, delivery_retry_interval=0.01,
        )
        with mock.patch.object(module.codex_queue_delivery, "_rpc", return_value={
                 "thread": {"id": SESSION, "cwd": "/tmp/repo"}}), \
             mock.patch.object(module, "current_session_children", return_value=rows), \
             mock.patch.object(module.pending_delivery, "read", side_effect=read_records), \
             mock.patch.object(module.pending_delivery, "ack") as ack, \
             mock.patch.object(module.codex_queue_delivery, "send_at_least_once") as send:
            result = module.deliver_to_native_queue(args, receipt, {"att-0", "att-1"})
        self.assertEqual(result["status"], "accepted")
        ack.assert_called_once_with(
            args.jobs.resolve(strict=False).parent, SESSION, ids[1],
            acked_by=f"codex-native-queue:{client_id}",
            expected_states=("pending", "claimed", "sent-ambiguous"),
        )
        send.assert_not_called()

    def test_native_queue_notice_stays_unacked_while_exact_item_is_pending(self) -> None:
        module = load_completion_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs.log"
            jobs.write_text("", encoding="utf-8")
            delivery_id = "dlv-notice-exact"
            receipt = {
                "schema_version": 1, "kind": "supervision",
                "owner_attempt_id": "att-owner", "sealed_batch_id": "batch-exact",
            }
            path = root / "delivery-notice.json"
            path.write_text(json.dumps({
                "delivery_id": delivery_id, "state": "pending", "receipt": receipt,
            }), encoding="utf-8")
            args = SimpleNamespace(
                queue_socket=Path("/tmp/app-server.sock"), jobs=jobs,
                parent_session_id=SESSION, sealed_batch_id="batch-exact",
                interval=0.1,
            )
            watcher = module.NoticeWatcher(args, {"att-owner"}, capability=None)
            item = {"id": "queue-item", "clientUserMessageId": delivery_id}
            with mock.patch.object(module.notice_receipt, "validate_pending_record"), \
                 mock.patch.object(module.notice_receipt, "context", return_value={
                     "additionalContext": {"notice": {"value": "typed notice"}},
                 }), \
                 mock.patch.object(module.notice_receipt, "gateway_delivery_id", return_value="notice-id"), \
                 mock.patch.object(module.pending_delivery, "claim", return_value={
                     "delivery_id": delivery_id, "state": "claimed", "receipt": receipt,
                 }), \
                 mock.patch.object(module.pending_delivery, "mark_sent_ambiguous") as ambiguous, \
                 mock.patch.object(module.pending_delivery, "ack") as ack, \
                 mock.patch.object(module.codex_queue_delivery, "send_at_least_once") as send, \
                 mock.patch.object(module.codex_queue_delivery, "list_queue", return_value=[item]):
                watcher._one(path)
            send.assert_called_once_with(
                args.queue_socket, thread_id=SESSION, client_message_id=delivery_id,
                message="typed notice", timeout=5.0,
            )
            ambiguous.assert_called_once()
            ack.assert_not_called()


if __name__ == "__main__":
    unittest.main()
