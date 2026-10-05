#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

P = Path(__file__).with_name("dispatch_lifecycle.py")
SPEC = importlib.util.spec_from_file_location(
    "dispatch_lifecycle", P
)
L = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = L
SPEC.loader.exec_module(L)


class LifecycleTest(unittest.TestCase):
    def test_namespace_detection_supports_host_and_remounted_proc(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            status = base / "status"
            comm = base / "comm"
            status.write_text("Name:\tpython\nNSpid:\t400\n", encoding="utf-8")
            comm.write_text("systemd\n", encoding="utf-8")
            self.assertFalse(L.pid_namespace_scoped(status, comm))
            status.write_text("Name:\tpython\nNSpid:\t400\t1\n", encoding="utf-8")
            self.assertTrue(L.pid_namespace_scoped(status, comm))
            evidence = L.pid_namespace_evidence(status, comm)
            self.assertEqual(evidence["lifecycle_selector_source"], "nspid-vector")
            self.assertEqual(evidence["lifecycle_nspid_width"], "2")
            status.write_text("Name:\tpython\nNSpid:\t1\n", encoding="utf-8")
            comm.write_text("bwrap\n", encoding="utf-8")
            self.assertTrue(L.pid_namespace_scoped(status, comm))

    def test_selection_preserves_detached_compatibility(self):
        self.assertEqual(
            L.select_launch_lifecycle({}, namespace_scoped=False), L.DETACHED
        )
        self.assertEqual(
            L.select_launch_lifecycle({}, namespace_scoped=True), L.FOREGROUND_SCOPED
        )

    def test_override_promoted_for_transient_pid1_class_scope(self):
        # A-1
        self.assertEqual(
            L.select_launch_lifecycle(
                {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
                evidence={
                    "lifecycle_selector_source": "pid1-class",
                    "lifecycle_pid1_class": "non-system-init",
                    "lifecycle_nspid_width": "1",
                },
            ),
            L.FOREGROUND_SCOPED,
        )

    def test_override_retained_for_host_like_scope_no_sandbox(self):
        # A-2 (anchor falsifier #1)
        self.assertEqual(
            L.select_launch_lifecycle(
                {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
                evidence={"lifecycle_selector_source": "host-like"},
            ),
            L.DETACHED,
        )

    def test_override_rejected_for_host_like_scope_workspace_write_sandbox(self):
        # A-3
        self.assertEqual(
            L.select_launch_lifecycle(
                {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
                evidence={"lifecycle_selector_source": "host-like"},
                parent_sandbox="workspace-write",
            ),
            L.FOREGROUND_SCOPED,
        )

    def test_override_rejected_for_env_derived_sandbox_label(self):
        # A-4
        self.assertEqual(
            L.select_launch_lifecycle(
                {
                    "AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1",
                    "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
                },
                evidence={"lifecycle_selector_source": "host-like"},
                parent_sandbox=None,
            ),
            L.FOREGROUND_SCOPED,
        )

    def test_override_rejected_for_every_transient_selector_source(self):
        # A-5
        for source in ("nspid-vector", "pid1-class", "proc-unreadable"):
            self.assertEqual(
                L.select_launch_lifecycle(
                    {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
                    evidence={"lifecycle_selector_source": source},
                ),
                L.FOREGROUND_SCOPED,
                source,
            )

    def test_selection_namespace_scoped_keyword_unchanged_without_override(self):
        # A-6
        self.assertEqual(
            L.select_launch_lifecycle({}, namespace_scoped=True), L.FOREGROUND_SCOPED
        )
        self.assertEqual(
            L.select_launch_lifecycle({}, namespace_scoped=False), L.DETACHED
        )

    def test_wrapper_reselection_promotes_detached_without_failed_attempt(self):
        resolution = L.reconcile_launch_lifecycle(
            L.DETACHED,
            {},
            evidence={
                "lifecycle_selector_source": "pid1-class",
                "lifecycle_nspid_width": "1",
                "lifecycle_pid1_class": "non-system-init",
            },
        )
        self.assertEqual(resolution.requested, L.DETACHED)
        self.assertEqual(resolution.effective, L.FOREGROUND_SCOPED)
        self.assertEqual(resolution.reselection, "promoted-wrapper-scope")
        self.assertEqual(
            resolution.metadata()["lifecycle_selector_source"], "pid1-class"
        )

    def test_wrapper_reselection_retains_override_only_for_host_like_scope(self):
        # A-8
        resolution = L.reconcile_launch_lifecycle(
            L.DETACHED,
            {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
            evidence={"lifecycle_selector_source": "host-like"},
        )
        self.assertEqual(resolution.effective, L.DETACHED)
        self.assertEqual(resolution.reselection, "retained-wrapper-scope")
        self.assertEqual(resolution.override, "honored")
        self.assertEqual(resolution.metadata()["launch_lifecycle_override"], "honored")

    def test_wrapper_reselection_rejects_override_for_transient_scope(self):
        # A-7
        resolution = L.reconcile_launch_lifecycle(
            L.DETACHED,
            {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
            evidence={"lifecycle_selector_source": "pid1-class"},
        )
        self.assertEqual(resolution.effective, L.FOREGROUND_SCOPED)
        self.assertEqual(resolution.reselection, "override-rejected-transient-scope")
        self.assertEqual(resolution.override, "rejected")
        self.assertEqual(
            resolution.metadata()["launch_lifecycle_override"], "rejected"
        )

    def test_wrapper_reselection_reports_rejection_even_without_delta(self):
        # A-9
        resolution = L.reconcile_launch_lifecycle(
            L.FOREGROUND_SCOPED,
            {"AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN": "1"},
            evidence={"lifecycle_selector_source": "pid1-class"},
        )
        self.assertEqual(resolution.effective, L.FOREGROUND_SCOPED)
        self.assertEqual(resolution.reselection, "override-rejected-transient-scope")

    def test_wrapper_reselection_promotes_without_override_unchanged(self):
        # A-10 (regression, verbatim intent of prior :55-68 test)
        resolution = L.reconcile_launch_lifecycle(
            L.DETACHED,
            {},
            evidence={"lifecycle_selector_source": "pid1-class"},
        )
        self.assertEqual(resolution.effective, L.FOREGROUND_SCOPED)
        self.assertEqual(resolution.reselection, "promoted-wrapper-scope")
        self.assertEqual(resolution.override, "absent")

    def test_foreground_wait_reports_success_and_child_signal(self):
        success = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
        self.assertEqual(L.wait_foreground(success, 2), L.ForegroundResult(0, ""))
        signaled = subprocess.Popen(
            ["sh", "-c", "kill -TERM $$"], start_new_session=True
        )
        outcome = L.wait_foreground(signaled, 2)
        self.assertEqual(outcome.failure, f"signal-{signal.SIGTERM}")

    def test_foreground_wait_forwards_wrapper_signal(self):
        # The wrapper itself was asked to stop: `interrupted`, not the
        # `signal-N` a worker that died of a signal on its own reports above.
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signum=signum):
                proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
                timer = threading.Timer(0.05, lambda: os.kill(os.getpid(), signum))
                timer.start()
                try:
                    outcome = L.wait_foreground(proc, 2, poll_interval=0.01)
                    self.assertEqual(outcome.failure, L.FOREGROUND_INTERRUPTED)
                    self.assertEqual(outcome.failure, "interrupted")
                    self.assertTrue(outcome.group_empty)
                    self.assertIsNotNone(proc.poll())
                finally:
                    timer.cancel()
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()

    def test_foreground_wait_reports_interrupted_when_the_child_exits_first(self):
        # The forwarded signal ends the child before the loop's own check: the
        # after-loop branch still reports the wrapper's stop request.
        proc = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
        proc.wait()
        received_late = {"done": False}
        real_group_empty = L._group_empty

        def group_empty_with_signal(pgid):
            if not received_late["done"]:
                received_late["done"] = True
                os.kill(os.getpid(), signal.SIGTERM)
            return real_group_empty(pgid)

        with mock.patch.object(L, "process_start_ticks", return_value="1"), \
             mock.patch.object(L, "_group_empty", side_effect=group_empty_with_signal), \
             mock.patch.object(L, "_terminate_group", return_value="signalled"):
            outcome = L.wait_foreground(proc, 2, poll_interval=0.01)
        self.assertEqual(outcome.failure, "interrupted")

    def test_foreground_timeout_terminates_group(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        outcome = L.wait_foreground(proc, 0.05, poll_interval=0.01)
        self.assertEqual(outcome.failure, "timeout")
        self.assertTrue(outcome.group_empty)
        self.assertIsNotNone(proc.poll())

    def test_foreground_parent_identity_loss_terminates_child_group(self):
        parent = subprocess.Popen(["sleep", "60"])
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        parent_start = L.process_start_ticks(parent.pid)
        timer = threading.Timer(0.05, parent.kill)
        timer.start()
        try:
            outcome = L.wait_foreground(
                child,
                3,
                parent_pid=parent.pid,
                parent_pid_start=parent_start,
                poll_interval=0.01,
            )
            self.assertEqual(outcome.failure, "parent-terminated")
            self.assertIsNotNone(child.poll())
        finally:
            timer.cancel()
            if parent.poll() is None:
                parent.kill()
            parent.wait()
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_foreground_parent_callback_loss_terminates_child_group(self):
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        checks = iter((True, True, False))
        try:
            outcome = L.wait_foreground(
                child,
                3,
                parent_pid=999999,
                parent_pid_start="unobservable",
                parent_is_live=lambda: next(checks, False),
                poll_interval=0.01,
            )
            self.assertEqual(outcome.failure, "parent-terminated")
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_unverifiable_group_is_never_reported_empty(self):
        observation = type("Observation", (), {"state": "unverifiable"})()
        with mock.patch.object(L, "process_group_observation", return_value=observation):
            self.assertIsNone(L._group_empty(123))

    def test_missing_leader_has_no_numeric_group_signal_authority(self):
        proc = mock.Mock(pid=437)
        with mock.patch.object(
            L, "signal_exact_process_group", return_value="leader-gone"
        ) as exact, mock.patch.object(L.os, "killpg") as killpg:
            result = L._terminate_group(proc, signal.SIGTERM, "42")
        self.assertEqual(result, "leader-gone")
        exact.assert_called_once_with(437, "42", signal.SIGTERM)
        killpg.assert_not_called()

    def test_identity_unavailable_still_reaps_direct_child(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        with mock.patch.object(L, "process_start_ticks", return_value=None):
            outcome = L.wait_foreground(proc, 2)
        self.assertEqual(outcome.failure, "process-identity-unavailable")
        self.assertFalse(outcome.group_empty)
        self.assertIsNotNone(proc.poll())

    def test_exceptional_observation_path_does_not_leave_direct_child(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        with mock.patch.object(L, "_group_empty", side_effect=RuntimeError("fixture")):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                L.wait_foreground(proc, 2, poll_interval=0.01)
        self.assertIsNotNone(proc.poll())

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "SIGHUP unavailable")
    def test_foreground_installs_and_restores_sighup_forwarding(self):
        proc = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
        with mock.patch.object(L.signal, "getsignal", return_value=signal.SIG_DFL), \
             mock.patch.object(L.signal, "signal") as install:
            outcome = L.wait_foreground(proc, 2)
        self.assertEqual(outcome.exit_code, 0)
        sighup_calls = [call for call in install.call_args_list if call.args[0] == signal.SIGHUP]
        self.assertEqual(len(sighup_calls), 2)


# A stand-in wrapper: on SIGINT/SIGTERM it "cleans up" for CLEANUP seconds,
# writes how many stop requests it saw, and exits 0 -- what a foreground
# wrapper does while it stops its worker and closes the row.
FAKE_WRAPPER = r"""
import os, signal, sys, time
seen = []
def stop(signum, _frame):
    seen.append(signum)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
cleanup = float(sys.argv[1])
marker = sys.argv[2]
open(marker + ".ready", "w").close()
while not seen:
    time.sleep(0.01)
deadline = time.monotonic() + cleanup
while time.monotonic() < deadline:
    time.sleep(0.01)
with open(marker, "w") as out:
    out.write(",".join(str(s) for s in seen))
print("worker_failure=interrupted", flush=True)
print("cleanup-said-to-stderr", file=sys.stderr, flush=True)
"""


class ForwardingTerminationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "fake_wrapper.py"
        self.script.write_text(FAKE_WRAPPER, encoding="utf-8")
        self.marker = self.root / "seen"

    def command(self, cleanup):
        return [sys.executable, str(self.script), str(cleanup), str(self.marker)]

    def send_after_ready(self, signals, *, gap=0.0):
        """Send stop requests to THIS process once the wrapper can take them."""
        sent = []
        ready = Path(str(self.marker) + ".ready")

        def fire():
            deadline = time.monotonic() + 20
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            for index, signum in enumerate(signals):
                if index:
                    time.sleep(gap)
                sent.append(time.monotonic())
                os.kill(os.getpid(), signum)
        thread = threading.Thread(target=fire, daemon=True)
        thread.start()
        return thread, sent

    def test_plain_run_captures_both_streams_without_signals(self):
        run = L.run_forwarding_termination(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
            capture=True, timeout=10,
        )
        self.assertEqual((run.returncode, run.stdout, run.stderr), (3, "out\n", "err\n"))
        self.assertIsNone(run.received_signal)
        self.assertFalse(run.cleanup_incomplete)

    def test_uncaptured_run_returns_no_text(self):
        run = L.run_forwarding_termination([sys.executable, "-c", "pass"], capture=False, timeout=None)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, None, None))

    def test_outer_timeout_keeps_subprocess_run_meaning(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            L.run_forwarding_termination(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture=True, timeout=0.3,
            )
        self.assertLess(time.monotonic() - started, 5)

    def test_signal_is_forwarded_and_the_wrapper_cleanup_is_awaited(self):
        before = signal.getsignal(signal.SIGINT)
        thread, sent = self.send_after_ready([signal.SIGINT])
        run = L.run_forwarding_termination(self.command(1.0), capture=True, timeout=30, grace=10)
        ended = time.monotonic()
        thread.join()
        self.assertEqual(run.received_signal, signal.SIGINT)
        self.assertFalse(run.cleanup_incomplete)
        self.assertEqual(run.returncode, 0)
        self.assertIn("worker_failure=interrupted", run.stdout)
        self.assertIn("cleanup-said-to-stderr", run.stderr)
        self.assertEqual(self.marker.read_text(), str(int(signal.SIGINT)))
        self.assertGreaterEqual(ended - sent[0], 1.0)
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_foreground_outer_timeout_forwards_TERM_and_waits_for_receipt_cleanup(self):
        run = L.run_forwarding_termination(self.command(.2), capture=True, timeout=.3,
                                          grace=2, terminate_on_timeout=True)
        self.assertTrue(run.timed_out)
        self.assertEqual(run.received_signal, signal.SIGTERM)
        self.assertFalse(run.cleanup_incomplete)
        self.assertTrue(self.marker.is_file())
        self.assertIn('worker_failure=interrupted', run.stdout)

    def test_foreground_outer_timeout_reports_incomplete_without_reexecution(self):
        run = L.run_forwarding_termination(self.command(2), capture=True, timeout=.3,
                                          grace=.1, terminate_on_timeout=True)
        self.assertTrue(run.timed_out)
        self.assertTrue(run.cleanup_incomplete)
        self.assertEqual(run.returncode, -signal.SIGKILL)
        self.assertFalse(self.marker.exists())

    def test_repeated_signals_are_forwarded_without_restarting_the_deadline(self):
        # Wrapper cleanup takes 3s; the grace is 1.5s. Stop requests arrive at
        # 0s, 0.6s and 1.2s: a deadline restarted by each would fire at 2.7s.
        thread, sent = self.send_after_ready(
            [signal.SIGINT, signal.SIGTERM, signal.SIGINT], gap=0.6)
        run = L.run_forwarding_termination(self.command(3.0), capture=True, timeout=30, grace=1.5)
        ended = time.monotonic()
        thread.join()
        self.assertEqual(len(sent), 3)
        self.assertEqual(run.received_signal, signal.SIGINT)
        self.assertTrue(run.cleanup_incomplete)
        self.assertEqual(run.returncode, -signal.SIGKILL)
        self.assertFalse(self.marker.exists())
        self.assertGreaterEqual(ended - sent[0], 1.45)
        self.assertLess(ended - sent[0], 2.4)

    def test_repeated_signals_reach_the_wrapper(self):
        thread, _sent = self.send_after_ready([signal.SIGINT, signal.SIGTERM], gap=0.2)
        run = L.run_forwarding_termination(self.command(1.0), capture=True, timeout=30, grace=10)
        thread.join()
        self.assertFalse(run.cleanup_incomplete)
        self.assertEqual(
            self.marker.read_text(), f"{int(signal.SIGINT)},{int(signal.SIGTERM)}")

    def test_a_stop_before_launch_launches_nothing(self):
        def stopped_popen(*_args, **_kwargs):
            raise AssertionError("must not launch after a stop request")

        real_signal = L.signal.signal
        real_getsignal = L.signal.getsignal
        before = real_getsignal(signal.SIGINT)
        installed = {}

        def capture(signum, handler):
            installed[signum] = handler
            return real_signal(signum, handler)

        with mock.patch.object(L.signal, "signal", side_effect=capture), \
             mock.patch.object(L.subprocess, "Popen", side_effect=stopped_popen) as popen:
            # The stop request lands while the handlers are being installed.
            def getsignal(signum):
                handler = installed.get(signal.SIGINT)
                if handler is not None and signum == signal.SIGHUP:
                    handler(signal.SIGINT, None)
                return real_getsignal(signum)

            with mock.patch.object(L.signal, "getsignal", side_effect=getsignal):
                run = L.run_forwarding_termination(["true"], capture=True, timeout=5)
        popen.assert_not_called()
        self.assertEqual(run.received_signal, signal.SIGINT)
        self.assertEqual((run.stdout, run.stderr), ("", ""))
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_handlers_are_left_alone_off_the_main_thread(self):
        result = {}

        def work():
            result["run"] = L.run_forwarding_termination(
                [sys.executable, "-c", "print('ok')"], capture=True, timeout=10)

        thread = threading.Thread(target=work)
        thread.start()
        thread.join(10)
        self.assertEqual(result["run"].stdout, "ok\n")


if __name__ == "__main__":
    unittest.main()
