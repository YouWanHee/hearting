from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools/memory/tests"))

from tidy_isolation import isolated_env
import pane_ownership
import session_tidy as st
import session_tidy_clear as clear
import session_tidy_idle as idle


class IdleObserverTests(unittest.TestCase):
    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.cwd = self.iso.root / "project"
        self.cwd.mkdir()
        self.pane = "idle:test-pane"
        self.extra = {"HERDR_PANE_ID": self.pane, "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
                      "XDG_CONFIG_HOME": str(self.iso.xdg_config),
                      "HEARTING_TIDY_HERDR": str(self.iso.root / "fake-herdr")}
        fake = self.iso.root / "fake-herdr"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        self.pane_patch = mock.patch.object(pane_ownership, "verified_pane", side_effect=lambda pane, *a, **kw: pane or "")
        self.pane_patch.start()
        self.addCleanup(self.pane_patch.stop)
        self.started = []

    def transcript(self, sid: str, tokens: int | None):
        path = self.iso.root / f"{sid}.jsonl"
        usage = {} if tokens is None else {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                                           "cache_read_input_tokens": 0, "output_tokens": 900000}
        path.write_text(json.dumps({"type": "assistant", "sessionId": sid,
                                    "message": {"usage": usage}}) + "\n", encoding="utf-8")
        return str(path)

    def observe_claude(self, sid="sid-A", tokens=200000, *, event_id="turn-A", now=1000, provider=None):
        payload = {"hook_event_name": "Stop", "session_id": sid, "cwd": str(self.cwd),
                   "transcript_path": self.transcript(sid, tokens), "turn_id": event_id}
        if provider:
            payload["provider"] = provider
        return idle.observe_payload("claude", payload, now=now)

    def test_stop_payload_uses_exact_turn_and_duplicate_does_not_extend_due_time(self):
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(clear, "herdr_command", return_value=str(self.iso.root / "fake-herdr")), \
             mock.patch.object(clear, "_start_helper", side_effect=lambda key, nonce: self.started.append(nonce) or 12345):
            self.assertEqual(self.observe_claude(), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            first = clear.read_reservation(seat.key)
            self.assertEqual(first["origin"], "idle")
            self.assertEqual(first["idle"]["due_at"], 4240)
            self.assertEqual(first["idle"]["tokens"], 200000)
            self.assertEqual(first["idle"]["cache_ttl"], "unknown")

            self.assertEqual(self.observe_claude(now=1100), "booking=scheduled")
            replay = clear.read_reservation(seat.key)
            self.assertEqual((replay["nonce"], replay["idle"]["due_at"]),
                             (first["nonce"], first["idle"]["due_at"]))
            self.assertEqual(self.started, [first["nonce"]])

    def test_threshold_unknown_and_worker_signals_fail_closed(self):
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            for tokens, expected in ((199999, "below-threshold"), (200000, "scheduled"), (200001, "scheduled")):
                result = self.observe_claude(tokens=tokens, event_id=f"turn-{tokens}")
                self.assertIn(expected, result)
                seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
                clear.cancel(seat)
            self.assertEqual(self.observe_claude(tokens=None, event_id="missing"),
                             "booking=skipped reason=usage-unknown")
            os.environ["AGENT_SESSION_ROLE"] = "worker"
            self.assertEqual(self.observe_claude(event_id="worker-turn"), "booking=skipped reason=worker")

    def test_provider_override_sets_wait_without_claiming_a_cache_ttl(self):
        config = self.iso.xdg_config / "hearting" / "session-tidy.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"idle": {"wait_seconds": 100}, "overrides": {
            "claude": {"anthropic": {"wait_seconds": 10, "min_tokens": 200000}}}}), encoding="utf-8")
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            self.assertEqual(self.observe_claude(provider="anthropic"), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            booking = clear.read_reservation(seat.key)
            self.assertEqual(booking["idle"]["due_at"], 1010)
            self.assertEqual(booking["idle"]["cache_ttl"], "unknown")

    def test_opencode_session_idle_reads_latest_completed_message_and_provider(self):
        con = sqlite3.connect(self.iso.opencode_db)
        con.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT, time_updated INTEGER)")
        con.execute("INSERT INTO message VALUES (?, ?, ?, ?)", (
            "assistant-1", "op-sid", json.dumps({"id": "assistant-1", "role": "assistant",
                "finish": "stop", "providerID": "provider-x", "modelID": "model-y",
                "tokens": {"input": 190000, "output": 999999, "cache": {"read": 10000, "write": 0}}}), 10))
        con.commit()
        con.close()
        payload = {"event": "session.idle", "session_id": "op-sid", "cwd": str(self.cwd)}
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            self.assertEqual(idle.observe_payload("opencode", payload, now=2000), "booking=scheduled")
            seat = st.resolve_seat("opencode", str(self.cwd), sid="op-sid")
            booking = clear.read_reservation(seat.key)
            self.assertEqual((booking["idle"]["tokens"], booking["idle"]["provider"],
                              booking["idle"]["model"]), (200000, "provider-x", "model-y"))

    def test_codex_stop_uses_only_the_exact_rollout_latest_input_usage(self):
        rollout = self.iso.codex_home / "sessions" / "rollout.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text("\n".join((
            json.dumps({"type": "session_meta", "payload": {"id": "cx-sid", "source": {}}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count",
                         "info": {"last_token_usage": {"input_tokens": 200000,
                                                             "cached_input_tokens": 190000}}}}),
        )) + "\n", encoding="utf-8")
        payload = {"thread_id": "cx-sid", "cwd": str(self.cwd), "transcript_path": str(rollout),
                   "turn_id": "cx-turn"}
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            self.assertEqual(idle.observe_payload("codex", payload, now=3000), "booking=scheduled")
            seat = st.resolve_seat("codex", str(self.cwd), sid="cx-sid")
            booking = clear.read_reservation(seat.key)
            self.assertEqual(booking["idle"]["tokens"], 200000)
            self.assertEqual(booking["idle"]["provider"], "unknown")

    def test_transport_claim_and_prompt_arrival_are_required_for_idle_enqueue(self):
        with self.iso.patched_environ(self.extra), mock.patch.object(clear, "herdr_command", return_value="fake"), \
             mock.patch.object(clear, "_start_helper", return_value=12345):
            self.assertEqual(self.observe_claude(), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            before = clear.read_reservation(seat.key)
            path = clear.reservation_path(seat.key)
            with mock.patch.object(st, "now_epoch", return_value=4241):
                claim, reason = clear.claim_idle_card_request(path, before["nonce"])
                self.assertEqual(reason, "")
                text = clear.idle_card_request_text(claim)
                sequence = st.read_prompt_seq(seat) + 1
                st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd), prompt_text=text, now=4241)
                self.assertTrue(clear.finish_idle_card_request(claim, accepted=True))
                context = clear.idle_context_for_enqueue(seat, "claude", "sid-A")
                self.assertEqual(context["identity"], before["idle"]["identity"])
                with st.seat_lock(seat.key):
                    st.write_card(seat, "claude", "sid-A", "진행 중: 카드", cwd=str(self.cwd),
                                  prompt_seq=sequence, now=4242)
                result = clear._accept_idle_enqueue(seat, "claude", "sid-A", str(self.cwd), context,
                                                   operation_id=context["identity"], queue_id="tidy-idle-test",
                                                   lock_wait=None)
                self.assertEqual(result, "clear=scheduled")
                accepted = clear.read_reservation(seat.key)
                self.assertEqual((accepted["idle_request"]["state"], accepted["queue_id"],
                                  accepted["followup_kind"]), ("accepted", "tidy-idle-test", "status-only"))

    def test_fake_clock_runs_one_idle_cycle_and_rearms_only_after_user_input(self):
        import dispatch_seat_handover as handover
        import session_tidy_runner as runner
        clock = [1000.0]
        sends = []
        queue_written = [False]
        prompt_text = "사용자가 새로 입력한 요청"
        payload = {"hook_event_name": "Stop", "session_id": "sid-A", "cwd": str(self.cwd),
                   "transcript_path": self.transcript("sid-A", 200000), "turn_id": "turn-A"}
        config = self.iso.xdg_config / "hearting" / "session-tidy.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"idle": {"wait_seconds": 10}}), encoding="utf-8")
        with self.iso.patched_environ(self.extra), \
             mock.patch.object(clear, "herdr_command", return_value=str(self.iso.root / "fake-herdr")), \
             mock.patch.object(clear, "_start_helper", side_effect=lambda *_: 12345), \
             mock.patch.object(st, "now_epoch", side_effect=lambda: clock[0]), \
             mock.patch.object(clear, "_run_idle_card_request") as request, \
             mock.patch.object(runner, "start_detached", return_value=12345), \
             mock.patch.object(handover, "collect_snapshot_bindings", return_value=[]), \
             mock.patch.object(st, "resolve_seat", side_effect=lambda harness, cwd=None, *a, **kw:
                               st.Seat("pane", st._digest("pane", self.pane), self.pane, harness, "")):
            request_text = {"text": ""}

            def send_once(path, nonce, pane):
                sends.append(("idle-request", nonce))
                claim, reason = clear.claim_idle_card_request(path, nonce)
                self.assertEqual(reason, "")
                request_text["text"] = clear.idle_card_request_text(claim)
                st.run_hook("claude", "prompt", "sid-A", cwd=str(self.cwd),
                            prompt_text=request_text["text"], now=clock[0])
                self.assertTrue(clear.finish_idle_card_request(claim, accepted=True))
                return {"accepted": "true"}

            request.side_effect = send_once
            self.assertEqual(idle.observe_payload("claude", payload, now=clock[0]), "booking=scheduled")
            seat = st.resolve_seat("claude", str(self.cwd), sid="sid-A")
            nonce = clear.read_reservation(seat.key)["nonce"]

            def sleep(seconds):
                clock[0] += seconds
                req = clear.read_reservation(seat.key)
                idle_request = req.get("idle_request") if req else None
                if idle_request and idle_request.get("state") == "sent" and not queue_written[0]:
                    sequence = st.read_prompt_seq(seat)
                    with st.seat_lock(seat.key):
                        st.write_card(seat, "claude", "sid-A", "진행 중: idle test", cwd=str(self.cwd),
                                      prompt_seq=sequence, now=clock[0])
                    context = clear.idle_context_for_enqueue(seat, "claude", "sid-A")
                    item = runner.enqueue_item(seat, "claude", "sid-A", str(self.cwd),
                                               operation_id=context["identity"], start=False)
                    line = clear.schedule_for_enqueue(
                        seat, "claude", "sid-A", str(self.cwd), idle_context=context,
                        operation_id=context["identity"], queue_id=item["id"])
                    self.assertEqual(line, "clear=scheduled")
                    runner._start_queued(item)
                    queue_written[0] = True

            def continue_status(path, continued_nonce, _pane):
                claim, reason = clear.claim_continue(path, continued_nonce)
                self.assertEqual(reason, "")
                st.run_hook("claude", "prompt", "sid-B", cwd=str(self.cwd),
                            prompt_text=clear.STATUS_ONLY_TEXT, now=clock[0])
                return {"continued": "true"}

            result = clear.run_helper(
                seat.key, nonce, wait=lambda *_: "idle", sleep=sleep,
                steward=lambda *_: sends.append(("clear", nonce)) or
                    {"cleared": "true", "new_session": "sid-B"},
                continuer=continue_status)
            self.assertEqual(result, "continued")
            self.assertTrue(queue_written[0])
            self.assertEqual([kind for kind, _identity in sends], ["idle-request", "clear"])
            request.assert_called_once()
            settled = clear.read_reservation(seat.key)
            self.assertEqual(settled["continued"]["kind"], "status-only")
            self.assertEqual(settled["continued"]["status_prompt_seq"], 2)
            self.assertFalse(clear.idle_clear_armed(seat, "claude", "sid-B"))

            successor_payload = {"hook_event_name": "Stop", "session_id": "sid-B", "cwd": str(self.cwd),
                                 "transcript_path": self.transcript("sid-B", 200000), "turn_id": "status-turn"}
            self.assertEqual(idle.observe_payload("claude", successor_payload, now=clock[0]),
                             "booking=skipped reason=not-rearmed")
            st.run_hook("claude", "prompt", "sid-B", cwd=str(self.cwd), prompt_text=prompt_text,
                        now=clock[0] + 1)
            self.assertTrue(clear.idle_clear_armed(seat, "claude", "sid-B"))
            self.assertEqual(idle.observe_payload(
                "claude", {**successor_payload, "turn_id": "user-turn"}, now=clock[0] + 2), "booking=scheduled")


if __name__ == "__main__":
    unittest.main()
