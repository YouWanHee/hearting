#!/usr/bin/env python3
"""Exact native background tidy transport, with all state in isolated fixtures."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT / "utilities")]
from tidy_isolation import isolated_env
import session_tidy as st
import session_tidy_clear as clear
import session_tidy_native as native
import pane_ownership

spec = importlib.util.spec_from_file_location("native_test_steward", ROOT / "utilities/peer-steward.py")
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


class NativeCase(unittest.TestCase):
    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.env = self.iso.patched_environ()
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.own = pane_ownership._process(os.getpid())
        self.target = {"pid": self.own[0], "start": self.own[5], "job": "1234abcd",
                       "home": str(self.iso.claude_dir), "binary": os.readlink(f"/proc/{self.own[0]}/exe")}
        self.registry = self.iso.claude_dir / "sessions" / f"{self.own[0]}.json"
        self.registry.parent.mkdir()
        self.record = {"pid": self.own[0], "procStart": self.own[5], "jobId": "1234abcd",
                       "kind": "bg", "sessionId": "session-A", "status": "idle"}
        self.publish()
        self.seat = st.Seat("native", st._digest("native", "claude", self.target["home"],
                                               self.target["job"], self.target["start"]),
                            harness="claude", native=self.target)
        self.cwd = str(self.iso.home)
        self.patch(native, "_caller_runtime", return_value=(self.own, False))
        self.patch(pane_ownership, "verified_pane", return_value="")

    def patch(self, obj, name, **kwargs):
        p = mock.patch.object(obj, name, **kwargs)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def publish(self, **fields):
        self.record.update(fields)
        self.registry.write_text(json.dumps(self.record))

    def book(self, *, continuing=False):
        st.record_event(self.seat, "claude", "session-A", "start", source="startup", cwd=self.cwd)
        st.write_card(self.seat, "claude", "session-A", "진행 중인 일: 임시 정리 시험")
        with mock.patch.object(clear, "_start_helper", return_value=os.getpid()), \
             mock.patch.object(clear, "herdr_command", return_value=None):
            self.assertEqual(clear.schedule_for_enqueue(self.seat, "claude", "session-A", self.cwd), "clear=scheduled")
        req = clear.read_reservation(self.seat.key)
        path = clear.reservation_path(self.seat.key)
        if continuing:
            clear._finish(self.seat.key, req["nonce"], "cleared", observed="session-B")
            self.publish(sessionId="session-B")
            # The real start hook consumes the native seat's card.
            self.assertIn("진행 중인 일", st.run_hook("claude", "start", "session-B", source="clear", cwd=self.cwd))
            req = clear.read_reservation(self.seat.key)
        return req, path


class NativeIdentityTest(NativeCase):
    def test_inherited_pane_does_not_borrow_its_card(self):
        os.environ["HERDR_PANE_ID"] = "another:pane"
        seat = st.resolve_seat("claude", self.cwd, sid="session-A")
        self.assertEqual(seat, self.seat)
        self.assertEqual(seat.pane, "")
        self.assertNotEqual(seat.key, st._digest("pane", "another:pane"))

    def test_other_jobs_and_restart_have_distinct_seats(self):
        a = st.resolve_seat("claude", self.cwd)
        self.publish(jobId="8765abcd")
        b = st.resolve_seat("claude", self.cwd)
        self.assertNotEqual(a.key, b.key)
        self.publish(procStart="different-start")
        self.assertEqual(st.resolve_seat("claude", self.cwd).kind, "project")

    def test_shared_daemon_and_other_harnesses_stay_project(self):
        with mock.patch.object(native, "_caller_runtime", return_value=(self.own, True)):
            self.assertIsNone(native.caller_target("claude"))
        for harness in ("codex", "opencode"):
            self.assertIsNone(native.caller_target(harness))
            self.assertEqual(st.resolve_seat(harness, self.cwd).kind, "project")

    def test_promoted_spare_needs_a_live_exact_background_declaration(self):
        with mock.patch.object(native, "_caller_runtime", return_value=(self.own, True)), \
             mock.patch.object(Path, "read_bytes", return_value=b"claude bg-spare\0--bg-spare\0claim.sock\0"):
            self.assertEqual(native.caller_target("claude"), self.target)
            self.publish(kind="spare")
            self.assertIsNone(native.caller_target("claude"))

    def test_native_process_titles_are_services_not_physical_pane_owners(self):
        for title in (b"claude bg-spare", b"claude bg-pty-host"):
            with mock.patch.object(Path, "read_bytes", return_value=title + b"\0--internal\0"):
                self.assertEqual(pane_ownership._runtime(self.own[0]), ("claude", True))

    def test_live_requires_pid_start_job_kind_and_no_symlink(self):
        for fields in ({"pid": 0}, {"procStart": "wrong"}, {"jobId": "wrong"},
                       {"kind": "foreground"}, {"sessionId": ""}):
            original = dict(self.record)
            self.publish(**fields)
            self.assertIsNone(native.live(self.target), fields)
            self.record = original
            self.publish()
        other = self.registry.with_suffix(".other")
        self.registry.rename(other)
        self.registry.symlink_to(other)
        self.assertIsNone(native.live(self.target))

    def test_pid_identity_is_rechecked_after_registry_read(self):
        with mock.patch.object(native, "_process", side_effect=[self.own, None]):
            self.assertIsNone(native.live(self.target))

    def test_ambient_old_id_is_replaced_but_explicit_foreign_id_is_refused(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "old-id"
        self.assertEqual(st.resolve_caller("claude", None, self.cwd)[2], "session-A")
        self.assertIsNone(st.resolve_caller("claude", "other-session", self.cwd))

    def test_worker_hooks_still_write_nothing(self):
        os.environ["AGENT_DISPATCH_DEPTH"] = "1"
        self.assertEqual(st.run_hook("claude", "start", "session-A", cwd=self.cwd), "")
        self.assertFalse(st._ledger_path(self.seat).exists())

    def test_native_card_survives_clear_and_is_delivered_once(self):
        req, _ = self.book()
        self.publish(sessionId="session-B")
        first = st.run_hook("claude", "start", "session-B", source="clear", cwd=self.cwd)
        self.assertIn("진행 중인 일", first)
        self.assertEqual(clear.read_reservation(self.seat.key)["observed"]["sid"], "session-B")
        self.assertNotIn("진행 중인 일", st.run_hook("claude", "prompt", "session-B", cwd=self.cwd))
        self.assertEqual(st.resolve_seat("claude", self.cwd).key, self.seat.key)


class NativeScreenTest(unittest.TestCase):
    def screen(self, body):
        snap = native.Snapshot()
        snap.feed("\x1b[2J\x1b[H" + body + "\r\n────────────────\r\n")
        return snap.lines()

    def test_faint_placeholder_is_empty_but_rgb_text_is_a_draft(self):
        self.assertIsNone(peer._screen_ready("claude", self.screen("❯ \x1b[2mSuggestion\x1b[22m")))
        self.assertEqual(peer._screen_ready("claude", self.screen("❯ \x1b[38;2;2;136;136mTyped draft")), "draft")
        self.assertEqual(peer._screen_ready("claude", self.screen("❯ \x1b[38;5;2mTyped draft")), "draft")

    def test_cursor_repaints_and_multiline_draft(self):
        self.assertEqual(peer._screen_ready("claude", self.screen("❯\r\n  second line")), "draft")
        lines = self.screen("❯ stale\x1b[3G\x1b[K")
        self.assertIsNone(peer._screen_ready("claude", lines))

    def test_unknown_controls_or_missing_repaint_are_refused(self):
        snap = native.Snapshot(); snap.feed("❯ ")
        self.assertIsNone(snap.lines())
        self.assertIsNone(self.screen("❯ \x1b[2S"))
        self.assertIsNone(self.screen("❯ \x1b[38:2:1:2:3m"))

    def test_form_guard_remains_shared(self):
        self.assertEqual(peer._screen_ready("claude", self.screen("Enter to confirm\r\n❯")), "form-open")

    def test_incomplete_new_frame_is_not_certified_by_an_older_finished_frame(self):
        fd, writer = os.pipe()
        self.addCleanup(os.close, fd)
        self.addCleanup(os.close, writer)
        os.write(writer, "\x1b[2J\x1b[H\x1b[?2026hAttaching…\x1b[?2026l\x1b[?2026h\x1b[H❯".encode())
        self.assertIsNone(native.View(fd).read(timeout=0.3))

    def test_same_view_reads_a_draft_arriving_after_initial_repaint(self):
        fd, writer = os.pipe()
        self.addCleanup(os.close, fd)
        self.addCleanup(os.close, writer)
        os.write(writer, "\x1b[?2026h\x1b[2J\x1b[H❯ \r\n────────────────\x1b[?2026l".encode())
        view = native.View(fd)
        self.assertIsNone(peer._screen_ready("claude", view.read(timeout=1)))
        os.write(writer, b"\x1b[?2026h\x1b[1;3Htyped\x1b[?2026l")
        self.assertEqual(peer._screen_ready("claude", view.read(timeout=1)), "draft")


class NativeCommandTest(NativeCase):
    def prepare_transport(self):
        proc = mock.Mock(); proc.poll.return_value = None
        @contextlib.contextmanager
        def attached(_target):
            yield 99, proc
        self.attach = self.patch(native, "attachment", side_effect=attached)
        self.view = self.patch(native.View, "read", return_value=[[("❯", False)], [("─", False)]])
        proxy = SimpleNamespace(**vars(os))
        self.write = proxy.write = mock.Mock()
        self.patch(native, "os", new=proxy)
        self.patch(native, "idle_reason", return_value="")
        return proc

    def run_command(self, req, path, *, continuing=False, ready=None):
        return native.command(req, path, req["nonce"], continuing=continuing,
                              screen_ready=ready or (lambda *_: None))

    def test_clear_once_and_observe_exact_new_session(self):
        req, path = self.book(); self.prepare_transport()
        def sent(fd, text):
            self.assertEqual(text, b"/clear\r")
            self.publish(sessionId="session-B")
            return len(text)
        self.write.side_effect = sent
        result = self.run_command(req, path)
        self.assertEqual((result["cleared"], result["new_session"]), ("true", "session-B"))
        self.write.assert_called_once()

    def test_draft_and_form_never_send(self):
        req, path = self.book(); self.prepare_transport()
        for reason in ("draft", "form-open", "draft-unknown"):
            self.assertEqual(self.run_command(req, path, ready=lambda *_, r=reason: r)["reason"], reason)
        self.write.assert_not_called()

    def test_new_prompt_or_superseded_card_never_sends(self):
        req, path = self.book(); self.prepare_transport()
        st.run_hook("claude", "prompt", "session-A", cwd=self.cwd)
        self.assertEqual(self.run_command(req, path)["reason"], "new-input")
        self.write.assert_not_called()

    def test_changed_target_is_refused_before_attach(self):
        req, path = self.book()
        self.publish(jobId="another-job")
        with mock.patch.object(native, "attachment") as attach:
            self.assertEqual(self.run_command(req, path)["reason"], "target-changed")
            attach.assert_not_called()

    def test_partial_send_is_unverified_and_never_retried(self):
        req, path = self.book(); self.prepare_transport()
        self.write.return_value = 1
        self.assertEqual(self.run_command(req, path)["cleared"], "unverified")
        self.write.assert_called_once()

    def test_continue_once_after_real_card_receipt(self):
        req, path = self.book(continuing=True); self.prepare_transport()
        def sent(fd, text):
            self.assertEqual(text, (clear.CONTINUE_TEXT + "\r").encode())
            st.run_hook("claude", "prompt", "session-B", cwd=self.cwd)
            return len(text)
        self.write.side_effect = sent
        self.assertEqual(self.run_command(req, path, continuing=True)["continued"], "true")
        self.assertEqual(self.run_command(req, path, continuing=True)["reason"], "superseded")
        self.write.assert_called_once()

    def test_draft_after_continue_claim_releases_only_unsent_claim(self):
        req, path = self.book(continuing=True); self.prepare_transport()
        ready = mock.Mock(side_effect=[None, "draft"])
        self.assertEqual(self.run_command(req, path, continuing=True, ready=ready)["reason"], "draft")
        self.assertEqual(clear.read_reservation(self.seat.key)["continued"]["state"], "pending")
        self.write.assert_not_called()

    def test_another_clear_after_send_cannot_confirm_the_continue_prompt(self):
        req, path = self.book(continuing=True); self.prepare_transport()
        def sent(fd, text):
            self.publish(sessionId="session-C")
            st.run_hook("claude", "prompt", "session-C", cwd=self.cwd)
            return len(text)
        self.write.side_effect = sent
        self.assertEqual(self.run_command(req, path, continuing=True)["continued"], "unverified")
        self.write.assert_called_once()

    def test_prompt_during_final_screen_read_cancels_clear_and_continue(self):
        for continuing in (False, True):
            with self.subTest(continuing=continuing):
                req, path = self.book(continuing=continuing)
                self.prepare_transport()
                lines = [[("❯", False)], [("─", False)]]
                count = 0
                def read(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == 2:
                        st.run_hook("claude", "prompt", "session-B" if continuing else "session-A", cwd=self.cwd)
                    return lines
                self.view.side_effect = read
                result = self.run_command(req, path, continuing=continuing)
                self.assertEqual(result["reason"], "new-input")
                self.write.assert_not_called()
                if continuing:
                    self.assertEqual(clear.read_reservation(self.seat.key)["continued"]["state"], "pending")

    def test_native_retire_does_not_acquire_cross_pane_authority(self):
        import dispatch_seat_handover as handover
        self.assertIsNone(handover.record_retire_handover("before", "claude", "session-A", "claude"))


if __name__ == "__main__":
    unittest.main()
