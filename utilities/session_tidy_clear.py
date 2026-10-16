#!/usr/bin/env python3
"""session-tidy auto-clear: the booking, the detached helper, the request check.

``session_tidy.py enqueue`` books one clear of the calling window
(:func:`schedule_for_enqueue`) and starts this file as a detached helper.  The helper
waits (one bounded ``herdr agent wait`` for ordinary tidy, at most 600 s) until the
window is idle and then asks ``peer-steward.py clear`` -- the only code that judges the
input box and types into the pane. Compact bookings poll idle in bounded intervals for
up to 10,800 s and also require read-only transcript evidence that the compacted turn
ended. Nothing waits for the memory tidy: the two run on their own.

The booking is ``clear/<seat>.json`` (0600, seat lock, atomic write): the exact pane,
harness, session id, card generation, prompt count, nonce and deadline (600 s for ordinary
tidy, 10,800 s for eligible automatic compaction). A newer
tidy replaces it (new nonce); a helper that finds another nonce, or no booking, stops
without writing anything.  ``enqueue --no-clear`` removes it.

A window is cleared only when nothing new could be lost: no prompt was submitted after
the card (the seat's ``prompt_seq``), the card is still the one booked, and
``peer-steward.py clear`` reads the pane as idle with no form open and an empty input box.
Anything it cannot decide is "not cleared" plus one result line (``write_notice``); a
success is silent.  The command is typed at most once and is never repeated.

A confirmed clear is followed by the continue prompt (:data:`CONTINUE_TEXT`, what the user
used to type by hand): the same helper asks ``peer-steward.py continue`` once, which types it
into the new session only when that session is the one the clear started, has no message yet,
is idle, shows no form and an empty input box, no prompt was submitted since the card and the
card is the booked one and was not handed to a peer.  The booking's ``continued`` record moves
``pending`` -> ``sending`` (by the steward, right before it types) -> its end, so the prompt is
typed at most once.  ``enqueue --no-continue`` books the clear alone.  A skip the person caused
(a prompt of their own, a draft, a new card) is silent; any other miss leaves one result line.

Test hooks (only inside ``HEARTING_TIDY_TEST_ROOT``, see ``session_tidy_runner``):
``HEARTING_TIDY_HERDR`` / ``HEARTING_TIDY_PEER_STEWARD`` stand in for the checked
commands, ``HEARTING_TIDY_CLEAR_DEADLINE`` / ``HEARTING_TIDY_CLEAR_OBSERVE`` /
``HEARTING_TIDY_CONTINUE_WINDOW`` for the timings.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import session_tidy as st  # noqa: E402

SCHEMA = 1
CLEAR_DEADLINE_SEC = 600.0
COMPACT_CLEAR_DEADLINE_SEC = 10_800.0
COMPACT_POLL_SEC = 1.0
OBSERVE_WAIT_SEC = 20.0
STEWARD_TIMEOUT_SEC = 180.0
# The built-in command that starts a fresh conversation in each harness.
CLEAR_COMMAND = {"claude": "/clear", "codex": "/clear", "opencode": "/new"}
# What the user typed by hand after a clear; the new session continues from the card it received.
CONTINUE_TEXT = "이어서해"
STATUS_ONLY_TEXT = "현재 카드 기준 현황만 짧게 표시하고 새 작업은 시작하지 말고 다음 입력을 기다리세요."
CONTINUE_WINDOW_SEC = 120.0
IDLE_CARD_WINDOW_SEC = 300.0

REASON_TEXT = {
    "new-input": "새 입력이 있었습니다",
    "timeout": "10분 안에 입력 대기 상태가 되지 않았습니다",
    "compact-timeout": "압축 뒤 작업이 끝났는지 3시간 안에 확인되지 않았습니다",
    "expired": "예약 시간이 지났습니다",
    "card-changed": "카드가 다시 쓰였습니다",
    "form-open": "선택·확인 창이 열려 있습니다",
    "draft": "입력창에 쓰던 글이 있습니다",
    "draft-unknown": "입력창이 비어 있는지 알 수 없습니다",
    "screen-unknown": "화면을 읽지 못했습니다",
    "target-changed": "그 자리의 세션이 바뀌었습니다",
    "herdr-not-found": "herdr를 찾지 못했습니다",
    "agent-not-found": "창을 찾지 못했습니다",
    "card-not-delivered": "새 세션이 카드를 받지 못했습니다",
    "card-taken": "카드가 이미 다른 세션에 전달됐습니다",
    "new-session-unknown": "새 세션을 확인하지 못했습니다",
    "native-attach-unavailable": "세션 전용 연결을 열지 못했습니다",
    "not-idle-native": "세션이 입력 대기 상태가 아닙니다",
    "partial-send": "명령 입력이 끝났는지 확인하지 못했습니다",
    "arrival-not-observed": "명령이 도착했는지 확인하지 못했습니다",
    "clear-in-flight": "이미 창 비우기를 전송 중입니다",
    "usage-unknown": "현재 입력 토큰을 확인하지 못했습니다",
    "idle-card-not-accepted": "유휴 정리 카드를 접수하지 못했습니다",
}
# Continue skips the person caused (or chose) themselves: no result line for these.
CONTINUE_QUIET = frozenset({"new-input", "draft", "card-changed", "handed-off", "no-card", "superseded", "off"})


# ---------------------------------------------------------------------------
# Test hooks (valid only inside a test root) -- the runner's rules, kept local so that
# peer-steward can import this module without pulling in the memory runner.
# ---------------------------------------------------------------------------

def _test_root() -> Optional[Path]:
    raw = os.environ.get("HEARTING_TIDY_TEST_ROOT")
    if not raw or not os.path.isabs(raw):
        return None
    try:
        root = Path(raw).resolve()
        state = st.state_root().resolve()
    except OSError:
        return None
    return root if (state == root or root in state.parents) else None


def _tunable(name: str, default: float) -> float:
    if _test_root() is None:
        return default
    try:
        return float(os.environ["HEARTING_TIDY_" + name])
    except (KeyError, ValueError):
        return default


def _injected(name: str) -> Optional[Path]:
    value, root = os.environ.get(name), _test_root()
    if not value or root is None:
        return None
    path = Path(value).resolve()
    return path if root in path.parents and path.is_file() else None


def herdr_command() -> Optional[str]:
    injected = _injected("HEARTING_TIDY_HERDR")
    return str(injected) if injected else shutil.which("herdr")


# ---------------------------------------------------------------------------
# The booking
# ---------------------------------------------------------------------------

def clear_dir() -> Path:
    return st.state_root() / "clear"


def reservation_path(seat_key: str) -> Path:
    return clear_dir() / f"{seat_key}.json"


def read_reservation(seat_key: str) -> Optional[dict]:
    data = st.read_json(reservation_path(seat_key))
    if isinstance(data, dict) and data.get("schema") == SCHEMA and data.get("nonce") and data.get("seat"):
        return data
    return None


def _write_reservation(data: dict) -> None:
    st.atomic_write_json(reservation_path(data["seat"]["key"]), data)


def _remove_reservation(seat_key: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(reservation_path(seat_key))


def cancel(seat: "st.Seat") -> None:
    """Cancel a booking. Automatic event identities remain tombstoned against hook replay."""
    with st.seat_lock(seat.key):
        req = read_reservation(seat.key)
        if req and req.get("origin") in ("compact", "idle") and (req.get("compact") or req.get("event_identity")):
            req.update(status="cancelled", reason="explicit-cancelled", finished=st.now_epoch())
            req["cancelled"] = {"at": st.now_epoch(), "identity": req.get("event_identity")
                                 or (req.get("compact") or {}).get("identity")}
            _write_reservation(req)
        else:
            _remove_reservation(seat.key)


def _proc_start(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return raw[raw.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return ""


def _helper_is_live(helper) -> bool:
    if not isinstance(helper, dict) or not isinstance(helper.get("pid"), int) or helper["pid"] <= 0:
        return False
    expected = str(helper.get("pid_start") or "")
    if not expected:
        # We have a launch receipt but cannot verify process identity; fail closed against
        # starting a second helper for the same booking.
        return True
    return _proc_start(helper["pid"]) == expected


def _ensure_helper(seat_key: str, nonce: str, *, lock_wait: Optional[float] = None) -> tuple[bool, str]:
    """Resume one durable reservation without replacing its nonce or transport claim."""
    try:
        with st.seat_lock(seat_key, wait=lock_wait):
            req = read_reservation(seat_key)
            if not req or req.get("nonce") != nonce or req.get("status") != "reserved":
                return False, "booking-changed"
            if _helper_is_live(req.get("helper")):
                return True, ""
            starting = req.get("helper_start") if isinstance(req.get("helper_start"), dict) else {}
            if starting.get("state") == "starting" \
                    and st.now_epoch() - float(starting.get("at", 0) or 0) < 10:
                return True, ""
            req["helper_start"] = {"state": "starting", "at": st.now_epoch()}
            _write_reservation(req)
    except (OSError, TimeoutError, st.StateError):
        return False, "reservation-lock-unavailable"
    try:
        pid = _start_helper(seat_key, nonce)
    except (OSError, subprocess.SubprocessError) as exc:
        reason = f"helper-not-started-{type(exc).__name__}"
        with contextlib.suppress(BaseException):
            with st.seat_lock(seat_key):
                req = read_reservation(seat_key)
                if req and req.get("nonce") == nonce and req.get("status") == "reserved":
                    req["helper_start"] = {"state": "failed", "at": st.now_epoch(), "reason": reason}
                    req["last_failure"] = {"stage": "helper-start", "reason": reason, "at": st.now_epoch()}
                    _write_reservation(req)
        return False, reason
    try:
        with st.seat_lock(seat_key, wait=lock_wait):
            req = read_reservation(seat_key)
            if req and req.get("nonce") == nonce and req.get("status") == "reserved":
                req["helper"] = {"pid": int(pid), "pid_start": _proc_start(int(pid))}
                req["helper_start"] = {"state": "started", "at": st.now_epoch()}
                _write_reservation(req)
    except (OSError, TimeoutError, st.StateError):
        # The detached helper records its own process identity before doing work.
        pass
    return True, ""


def reconcile_pending(seat: "st.Seat", *, lock_wait: float = 0.2) -> bool:
    """Let an ordinary lifecycle hook restart a missing helper for a durable booking."""
    req = read_reservation(seat.key)
    if not req or req.get("status") != "reserved":
        return False
    if not (req.get("helper_start") or req.get("helper") or req.get("origin") in ("compact", "idle")
            or req.get("operation_id") or req.get("queue_id")):
        return False
    ok, _reason = _ensure_helper(seat.key, str(req.get("nonce") or ""), lock_wait=lock_wait)
    return ok


def observe_idle_event(harness: str, sid: str, cwd: str, *, event_id: str, provider: str = "",
                       model: str = "", tokens: int, now: Optional[float] = None) -> str:
    """Durably book one token-qualified idle window for an exact main session turn."""
    import hashlib
    import session_tidy as st
    from session_tidy_policy import idle_policy
    now = st.now_epoch() if now is None else float(now)
    if harness not in ("claude", "codex", "opencode") or not sid or not event_id:
        return "booking=skipped reason=invalid-idle-identity"
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        return "booking=skipped reason=usage-unknown"
    policy, config_reason = idle_policy(harness, provider or None)
    if config_reason or policy is None:
        return f"booking=skipped reason={config_reason or 'config-invalid'}"
    if not policy["enabled"]:
        return "booking=off"
    if tokens < policy["min_tokens"]:
        return "booking=skipped reason=below-threshold"
    seat = st.resolve_seat(harness, cwd or None, sid=sid)
    # Idle automation requires a transport whose exact session and empty input
    # can be checked by peer-steward at send time.
    if not ((seat.kind == "pane" and seat.pane)
            or (harness == "claude" and seat.kind == "native" and seat.native)):
        return "booking=skipped reason=exact-pane-unavailable"
    identity = hashlib.sha256(
        f"{harness}\0{sid}\0{st.read_prompt_seq(seat)}\0{event_id}".encode("utf-8")).hexdigest()
    with st.seat_lock(seat.key):
        existing = read_reservation(seat.key)
        old_idle = (existing or {}).get("idle") if isinstance((existing or {}).get("idle"), dict) else {}
        if existing and old_idle.get("identity") == identity:
            if existing.get("status") == "reserved":
                nonce = str(existing.get("nonce") or "")
                # Duplicate Stop/status delivery never moves the original deadline.
            else:
                return f"booking=skipped reason=event-{existing.get('status') or 'terminal'}"
        else:
            if existing and existing.get("status") == "reserved":
                return "booking=skipped reason=booking-pending"
            if existing and existing.get("origin") == "idle" and existing.get("status") == "cleared":
                prior = existing.get("idle") if isinstance(existing.get("idle"), dict) else {}
                rearm_seq = int(prior.get("user_rearm_seq", 0) or 0)
                if (existing.get("new_session") != sid or rearm_seq <= int(prior.get("status_prompt_seq", 0) or 0)
                        or st.read_prompt_seq(seat) < rearm_seq):
                    return "booking=skipped reason=not-rearmed"
            sequence = st.read_prompt_seq(seat)
            card = st.read_latest_card(seat)
            card_generation = int(card.get("generation", 0) or 0) if card else 0
            due_at = now + float(policy["wait_seconds"])
            nonce = secrets.token_hex(8)
            booking = {
                "schema": SCHEMA, "nonce": nonce, "status": "reserved", "created": now,
                "deadline": due_at + IDLE_CARD_WINDOW_SEC,
                "seat": seat.as_dict(), "harness": harness, "sid": sid, "cwd": cwd,
                "card_generation": card_generation, "prompt_seq": sequence,
                "continue_off": False, "origin": "idle", "followup_kind": "status-only",
                "snapshot_state": "deferred", "idle": {
                    "identity": identity, "event_id": event_id, "provider": provider or "unknown",
                    "model": model or "unknown", "tokens": tokens, "min_tokens": policy["min_tokens"],
                    "wait_seconds": policy["wait_seconds"], "cache_margin_seconds": policy["cache_margin_seconds"],
                    "cache_ttl": "unknown", "due_at": due_at, "user_generation": sequence,
                }}
            _write_reservation(booking)
    started, reason = _ensure_helper(seat.key, nonce)
    if not started:
        return f"booking=pending reason={reason}"
    return "booking=scheduled"


def validate_idle_request(path, nonce: Optional[str] = None, *, now: Optional[float] = None,
                          _sending_claim: Optional[dict] = None):
    """Validate the due idle card request before the one main-session prompt."""
    now = st.now_epoch() if now is None else now
    req, seat, reason = _booking_at(Path(path))
    if req is None:
        return None, reason
    if (nonce and req.get("nonce") != nonce) or req.get("status") != "reserved" or req.get("origin") != "idle":
        return None, "superseded"
    idle = req.get("idle") if isinstance(req.get("idle"), dict) else {}
    if now < float(idle.get("due_at", 0) or 0):
        return None, "not-due"
    if now > float(req.get("deadline", 0) or 0):
        return None, "expired"
    if st.read_prompt_seq(seat) != int(req.get("prompt_seq", -1)):
        return None, "new-input"
    card = st.read_latest_card(seat)
    if (int(card.get("generation", 0) or 0) if card else 0) != int(req.get("card_generation", -1)):
        return None, "card-changed"
    held = req.get("idle_request") if isinstance(req.get("idle_request"), dict) else {}
    if _sending_claim is None and held:
        return None, "idle-request-already-claimed"
    if _sending_claim is not None and held != _sending_claim:
        return None, "booking-changed"
    return req, ""


def idle_card_request_text(req: dict) -> str:
    nonce = str(req.get("nonce") or "")
    return ("자동 유휴 정리 요청입니다. 지금까지의 이 대화만 바탕으로 session-tidy 카드의 "
            "진행 중, 기다리는 결정, 다음 할 일, 관련 경로를 사용자의 언어로 짧게 작성하세요. "
            "현재 session-tidy 안내에 따라 기존 card 작성 명령과 enqueue를 실행해 기억 정리를 접수하세요. "
            "별도 작업을 시작하거나 이어서 하지 말고, 카드가 접수되면 다음 입력을 기다리세요. "
            f"요청 식별자: {nonce}")


def claim_idle_card_request(path, nonce: Optional[str] = None):
    req, seat, reason = _booking_at(Path(path))
    if req is None:
        return None, reason
    with st.seat_lock(seat.key):
        req, reason = validate_idle_request(path, nonce)
        if req is None:
            return None, reason
        claim = {"state": "sending", "nonce": req["nonce"], "token": secrets.token_hex(12),
                 "at": st.now_epoch(), "text_digest": hashlib.sha256(
                     idle_card_request_text(req).encode("utf-8")).hexdigest()}
        req["idle_request"] = claim
        _write_reservation(req)
        return {**req, "idle_request": claim}, ""


def release_unsent_idle_request(claim: dict) -> bool:
    """Release only a claim that is proven not to have reached the pane."""
    key = str((claim.get("seat") or {}).get("key") or "")
    with st.seat_lock(key):
        req = read_reservation(key)
        if (not req or req.get("nonce") != claim.get("nonce")
                or req.get("idle_request") != claim.get("idle_request")
                or (req.get("idle_request") or {}).get("state") != "sending"):
            return False
        req.pop("idle_request", None)
        _write_reservation(req)
        return True


def finish_idle_card_request(claim: dict, *, accepted: bool, reason: str = "") -> bool:
    """Record the result of the one transport call; uncertainty is terminal and never re-sent."""
    key = str((claim.get("seat") or {}).get("key") or "")
    with st.seat_lock(key):
        req = read_reservation(key)
        held = (req or {}).get("idle_request") if isinstance((req or {}).get("idle_request"), dict) else {}
        if not req or req.get("nonce") != claim.get("nonce") or held.get("state") not in ("sending", "arrived"):
            return False
        if accepted and held.get("state") == "arrived":
            req["idle_request"] = {**held, "state": "sent", "sent_at": st.now_epoch()}
        else:
            req["idle_request"] = {**held, "state": "unverified", "reason": reason or "arrival-not-observed",
                                    "finished": st.now_epoch()}
        _write_reservation(req)
        return accepted and held.get("state") == "arrived"


def observe_prompt_locked(seat: "st.Seat", harness: str, sid: str, prompt: str, now: float, sequence: int) -> bool:
    """Bind a transport-generated prompt to its live claim; prompt text alone grants no authority."""
    if not prompt:
        return False
    req = read_reservation(seat.key)
    if not req or req.get("status") not in ("reserved", "cleared"):
        return False
    request = req.get("idle_request") if isinstance(req.get("idle_request"), dict) else {}
    if (req.get("status") == "reserved" and req.get("origin") == "idle"
            and request.get("state") == "sending" and req.get("harness") == harness and req.get("sid") == sid
            and sequence == int(req.get("prompt_seq", -1)) + 1
            and now - float(request.get("at", 0) or 0) <= STEWARD_TIMEOUT_SEC
            and hashlib.sha256(prompt.encode("utf-8")).hexdigest() == request.get("text_digest")):
        request = {**request, "state": "arrived", "arrival": {"harness": harness, "sid": sid,
                                                                  "at": now, "prompt_seq": sequence}}
        req["idle_request"] = request
        req["prompt_seq"] = sequence
        _write_reservation(req)
        return True
    continued = req.get("continued") if isinstance(req.get("continued"), dict) else {}
    if (req.get("status") == "cleared" and req.get("followup_kind") == "status-only"
            and continued.get("kind") == "status-only" and continued.get("state") == "sending"
            and sequence == int(req.get("prompt_seq", -1)) + 1
            and (req.get("new_session") == sid
                 or (harness == "opencode" and req.get("new_session") in (None, "", "-")))
            and req.get("harness") == harness
            and hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            == hashlib.sha256(STATUS_ONLY_TEXT.encode("utf-8")).hexdigest()):
        req["new_session"] = sid
        continued["status_prompt_seq"] = sequence
        continued["status_prompt_observed_at"] = now
        req["continued"] = continued
        idle = req.get("idle") if isinstance(req.get("idle"), dict) else {}
        idle["status_prompt_seq"] = sequence
        req["idle"] = idle
        _write_reservation(req)
        return True
    return False


def note_real_prompt_locked(seat: "st.Seat", harness: str, sid: str, sequence: int, now: float) -> None:
    """Invalidate a pending idle request or rearm only after a real successor prompt."""
    req = read_reservation(seat.key)
    if not req:
        return
    if req.get("status") == "reserved" and req.get("origin") == "idle":
        req.update(status="cancelled", reason="new-input", finished=now)
        req["cancelled"] = {"at": now, "identity": (req.get("idle") or {}).get("identity")}
        _write_reservation(req)
        return
    continued = req.get("continued") if isinstance(req.get("continued"), dict) else {}
    if (req.get("status") == "cleared" and req.get("followup_kind") == "status-only"
            and continued.get("state") == "sent" and req.get("harness") == harness
            and req.get("new_session") == sid):
        idle = req.get("idle") if isinstance(req.get("idle"), dict) else {}
        idle["user_rearm_seq"] = sequence
        idle["user_rearm_at"] = now
        req["idle"] = idle
        _write_reservation(req)


def idle_context_for_enqueue(seat: "st.Seat", harness: str, sid: str) -> Optional[dict]:
    req = read_reservation(seat.key)
    request = (req or {}).get("idle_request") if isinstance((req or {}).get("idle_request"), dict) else {}
    arrival = request.get("arrival") if isinstance(request.get("arrival"), dict) else {}
    if (not req or req.get("status") != "reserved" or req.get("origin") != "idle"
            or req.get("harness") != harness or req.get("sid") != sid
            or request.get("state") not in ("arrived", "sent")
            or arrival.get("sid") != sid or st.read_prompt_seq(seat) != int(arrival.get("prompt_seq", -1))):
        return None
    return {"nonce": req["nonce"], "identity": (req.get("idle") or {}).get("identity"),
            "arrival_seq": int(arrival.get("prompt_seq", -1))}


def idle_clear_armed(seat: "st.Seat", harness: str, sid: str) -> bool:
    """Suppress compact clear after an idle status reply until real user input rearms it."""
    req = read_reservation(seat.key)
    if (not req or req.get("origin") != "idle" or req.get("status") != "cleared"
            or req.get("followup_kind") != "status-only" or req.get("harness") != harness
            or req.get("new_session") != sid):
        return True
    idle = req.get("idle") if isinstance(req.get("idle"), dict) else {}
    return int(idle.get("user_rearm_seq", 0) or 0) > int(idle.get("status_prompt_seq", 0) or 0)


def _accept_idle_enqueue(seat: "st.Seat", harness: str, sid: str, cwd: str, idle_context: dict,
                         *, operation_id: Optional[str], queue_id: Optional[str],
                         lock_wait: Optional[float]) -> str:
    try:
        with st.seat_lock(seat.key, wait=lock_wait):
            req = read_reservation(seat.key)
            request = (req or {}).get("idle_request") if isinstance((req or {}).get("idle_request"), dict) else {}
            arrival = request.get("arrival") if isinstance(request.get("arrival"), dict) else {}
            card = st.read_latest_card(seat)
            if (not req or req.get("nonce") != idle_context.get("nonce") or req.get("status") != "reserved"
                    or req.get("origin") != "idle" or req.get("harness") != harness or req.get("sid") != sid
                    or st.now_epoch() > float(req.get("deadline", 0) or 0)
                    or request.get("state") not in ("arrived", "sent") or arrival.get("sid") != sid
                    or int(arrival.get("prompt_seq", -1)) != int(idle_context.get("arrival_seq", -2))
                    or st.read_prompt_seq(seat) != int(idle_context.get("arrival_seq", -1))
                    or not card or (card.get("author") or {}).get("harness") != harness
                    or (card.get("author") or {}).get("sid") != sid
                    or int(card.get("prompt_seq", -1)) != st.read_prompt_seq(seat)
                    or int(card.get("generation", 0) or 0) <= int(req.get("card_generation", 0) or 0)):
                return "clear=skipped reason=idle-card-not-accepted"
            req.update(card_generation=int(card["generation"]), prompt_seq=st.read_prompt_seq(seat),
                       queue_id=queue_id, operation_id=operation_id or (req.get("idle") or {}).get("identity"),
                       memory_state="queued", snapshot_state="pending", followup_kind="status-only",
                       continue_off=False, cwd=cwd)
            req["idle_request"] = {**request, "state": "accepted", "accepted_at": st.now_epoch()}
            _write_reservation(req)
            nonce = str(req["nonce"])
    except (OSError, TimeoutError, st.StateError):
        return "clear=pending reason=reservation-lock-unavailable"
    started, reason = _ensure_helper(seat.key, nonce, lock_wait=lock_wait)
    return "clear=scheduled" if started else f"clear=pending reason={reason}"


def _helper_env(environ=None) -> dict:
    """The runner's clean environment plus where herdr listens.

    ``clean_env`` drops every ``HERDR_*`` name so a detached tidy never inherits the
    caller's pane; this helper still has to reach the same herdr server, and without
    ``HERDR_SOCKET_PATH`` herdr falls back to a default path that may not be the one
    in use ("server not running", measured on a live pane 2026-10-01).
    """
    from session_tidy_runner import clean_env
    environ = os.environ if environ is None else environ
    env = clean_env(environ)
    socket_path = environ.get("HERDR_SOCKET_PATH")
    if socket_path:
        env["HERDR_SOCKET_PATH"] = socket_path
    return env


def _start_helper(seat_key: str, nonce: str) -> int:
    """Start ``run`` in its own session so neither /clear, a closing pane nor a process-group
    cleanup of the caller can take it down."""
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "run", "--seat", seat_key, "--nonce", nonce],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True, env=_helper_env(), cwd=str(st.ensure_dir(st.state_root())))
    return proc.pid


def schedule_for_enqueue(seat: "st.Seat", harness: str, sid: str, cwd: str, *, opt_out: bool = False,
                         no_continue: bool = False, now: Optional[float] = None,
                         compact_context: Optional[dict] = None,
                         lock_wait: Optional[float] = None,
                         expected_booking_nonce: Optional[str] = None,
                         compact_started_at: Optional[float] = None,
                         inherit_continue_off: bool = False,
                         operation_id: Optional[str] = None,
                         queue_id: Optional[str] = None,
                         idle_context: Optional[dict] = None) -> str:
    """Book the clear and start its helper; the ``clear=...`` words ``enqueue`` prints.

    ``clear=scheduled``      the window is cleared by itself once it is idle, then continued;
    ``clear=scheduled continue=off``  ``--no-continue``: cleared, nothing typed after it;
    ``clear=off``            ``--no-clear``: a pending booking is cancelled too;
    ``clear=skipped reason`` a prompt was submitted after the card: nothing is booked;
    ``clear=manual hint=C``  outside herdr (no pane, no herdr): the user types ``C``.
    """
    now = st.now_epoch() if now is None else now
    hint = CLEAR_COMMAND.get(harness, "/clear")
    if idle_context is not None:
        if opt_out:
            cancel(seat)
            return "clear=off"
        return _accept_idle_enqueue(seat, harness, sid, cwd, idle_context,
                                    operation_id=operation_id, queue_id=queue_id, lock_wait=lock_wait)
    compact = None
    if compact_context is not None:
        card = st.read_latest_card(seat)
        metadata = (card or {}).get("compact")
        author = (card or {}).get("author") or {}
        if (harness not in ("claude", "opencode") or compact_context.get("trigger") != "auto"
                or seat.kind not in ("pane", "native") or not metadata
                or metadata.get("identity") != compact_context.get("identity")
                or (author.get("harness"), author.get("sid")) != (harness, sid)):
            return "clear=skipped reason=compact-ineligible"
        compact = {key: compact_context.get(key) for key in
                   ("identity", "trigger", "transcript", "transcript_identity", "summary_id", "parent_id",
                    "tail_start_id", "compaction_part_id", "boundary_offset")
                   if compact_context.get(key) is not None}
    if opt_out:
        cancel(seat)
        return "clear=off"
    native = seat.kind == "native" and seat.native and harness == "claude"
    if not native and (seat.kind != "pane" or not seat.pane or harness not in CLEAR_COMMAND or herdr_command() is None):
        if compact:
            return "clear=skipped reason=outside-herdr"
        cancel(seat)
        return f"clear=manual hint={hint}"
    with st.seat_lock(seat.key, wait=lock_wait):
        existing = read_reservation(seat.key)
        if compact and existing and (existing.get("compact") or {}).get("identity") == compact.get("identity"):
            if existing.get("status") == "reserved":
                nonce = str(existing.get("nonce") or "")
                if queue_id and not existing.get("queue_id"):
                    existing.update(queue_id=queue_id, operation_id=operation_id or compact.get("identity"),
                                    memory_state="queued")
                    _write_reservation(existing)
            elif existing.get("status") == "cancelled":
                return "clear=off"
            else:
                return f"clear=completed reason={existing.get('status') or 'terminal'}"
        else:
            nonce = ""
        if compact and existing and existing.get("status") == "reserved" \
                and (existing.get("harness"), existing.get("sid")) != (harness, sid):
            return "clear=skipped reason=booking-target-changed"
        claim = existing.get("clear_claim") if existing and existing.get("status") == "reserved" \
            and isinstance(existing.get("clear_claim"), dict) else {}
        if compact and claim.get("state") == "sending" \
                and now - float(claim.get("at", 0) or 0) < STEWARD_TIMEOUT_SEC:
            # One clear is being typed right now; a compaction must not revive it with a new nonce.
            return "clear=skipped reason=clear-in-flight"
        if compact and expected_booking_nonce is not None:
            current_nonce = existing.get("nonce") if existing and existing.get("status") == "reserved" else ""
            if current_nonce != expected_booking_nonce:
                return "clear=skipped reason=booking-changed"
        if compact and compact_started_at is not None and existing \
                and float(existing.get("created", 0) or 0) > compact_started_at:
            return "clear=skipped reason=booking-changed"
        card = st.read_latest_card(seat)
        if compact and (not card or (card.get("compact") or {}).get("identity") != compact.get("identity")):
            return "clear=skipped reason=card-changed"
        seq = st.read_prompt_seq(seat)
        inherited = (inherit_continue_off and expected_booking_nonce is not None
                     and existing and existing.get("nonce") == expected_booking_nonce)
        if expected_booking_nonce is None:
            inherited = bool(existing and existing.get("status") == "reserved"
                             and float(existing.get("deadline", 0) or 0) >= now
                             and existing.get("harness") == harness and existing.get("sid") == sid
                             and int(existing.get("prompt_seq", -1)) == seq
                             and existing.get("continue_off"))
        inherit_stop = bool(compact and inherited and existing.get("status") == "reserved"
                            and float(existing.get("deadline", 0) or 0) >= now
                            and existing.get("harness") == harness and existing.get("sid") == sid
                            and int(existing.get("prompt_seq", -1)) == seq
                            and existing.get("continue_off"))
        author = (card or {}).get("author") or {}
        stale = bool(card) and (author.get("harness"), author.get("sid")) == (harness, sid) \
            and card.get("prompt_seq") is not None and int(card["prompt_seq"]) != seq
        if stale:
            if existing:
                existing.update(status="cancelled", reason="new-input", finished=now)
                existing["cancelled"] = {"at": now, "identity": existing.get("event_identity")
                                          or (existing.get("compact") or {}).get("identity")}
                _write_reservation(existing)
            return "clear=skipped reason=new-input"
        if not nonce:
            nonce = secrets.token_hex(8)
            duration = COMPACT_CLEAR_DEADLINE_SEC if compact else _tunable("CLEAR_DEADLINE", CLEAR_DEADLINE_SEC)
            booking = {
                "schema": SCHEMA, "nonce": nonce, "status": "reserved", "created": now,
                "deadline": now + duration,
                "seat": seat.as_dict(),
                "harness": harness, "sid": sid, "cwd": cwd,
                "card_generation": int(card["generation"]) if card else 0, "prompt_seq": seq,
                "continue_off": bool(no_continue or inherit_stop),
                "snapshot_state": ("deferred" if compact else "pending")
                if seat.kind in ("pane", "native") else "not-applicable"}
            if compact:
                identity = operation_id or compact.get("identity")
                booking.update(origin="compact", compact=compact, event_identity=identity,
                               operation_id=identity)
            if queue_id:
                booking.update(queue_id=queue_id, memory_state="queued")
            _write_reservation(booking)
    started, start_reason = _ensure_helper(seat.key, nonce, lock_wait=lock_wait)
    if not started:
        return f"clear=pending reason={start_reason}"
    return "clear=scheduled continue=off" if (no_continue or inherit_stop) else "clear=scheduled"


def observe_runtime_status(sid: str, status: str, *, observed_at=None, cwd: str = "") -> bool:
    """Attach OpenCode's native status to the exact pending compact booking for this main sid."""
    status = str(status or "").lower()
    if status not in ("idle", "busy", "working", "retry") or not sid:
        return False
    import session_tidy as st
    observed = st.now_epoch() if observed_at is None else float(observed_at)
    if observed > 10_000_000_000:   # JavaScript Date.now() is milliseconds.
        observed /= 1000.0
    try:
        seat = st.resolve_seat("opencode", cwd or None, sid=sid)
        with st.seat_lock(seat.key, wait=0.2):
            req = read_reservation(seat.key)
            if (not req or req.get("status") != "reserved" or req.get("origin") != "compact"
                    or req.get("harness") != "opencode" or req.get("sid") != sid
                    or observed < float(req.get("created", 0) or 0)):
                return False
            req["runtime_status"] = {"sid": sid, "status": status, "observed_at": observed}
            _write_reservation(req)
            return True
    except (OSError, ValueError, st.StateError):
        return False


def compact_observation(req: dict) -> dict:
    """Read the booking's exact compact transcript anchor; unknown evidence never permits clear."""
    anchor = req.get("compact") if isinstance(req.get("compact"), dict) else {}
    harness, sid = str(req.get("harness") or ""), str(req.get("sid") or "")
    source = str(anchor.get("transcript") or "")
    native_idle = False
    if harness == "opencode":
        status = req.get("runtime_status") if isinstance(req.get("runtime_status"), dict) else {}
        native_idle = (status.get("sid") == sid and status.get("status") == "idle"
                       and float(status.get("observed_at", 0) or 0) >= float(req.get("created", 0) or 0))
    try:
        import tidy_transcripts as transcripts
        return transcripts.compact_observation(harness, sid, source, anchor, native_idle=native_idle)
    except BaseException as exc:
        return {"state": "unknown", "reason": f"compact-reader-{type(exc).__name__}", "source": source}


def _refresh_compact_card(req: dict, observation: dict) -> None:
    """Collect current owners outside the lock, then publish against the booked card/input."""
    import session_tidy as st
    import dispatch_seat_handover as handover
    seat = st.seat_from_dict(req["seat"])
    identity = str((req.get("compact") or {}).get("identity") or "")
    try:
        bindings = handover.collect_snapshot_bindings(req["harness"], req["sid"])
        with st.seat_lock(seat.key):
            held = read_reservation(seat.key)
            if not held or held.get("nonce") != req.get("nonce") or held.get("status") != "reserved":
                return
            handover.publish_snapshot_locked(
                seat, req["harness"], req["sid"], bindings,
                expected_prompt_seq=int(req.get("prompt_seq", -1)),
                expected_card_generation=int(req.get("card_generation", -1)))
            held["snapshot_state"] = "ready"
            held["snapshot_count"] = len(bindings)
            _write_reservation(held)
            st.update_compact_card_locked(seat, int(req.get("card_generation", 0) or 0), identity,
                                          recent=str(observation.get("recent") or ""),
                                          source=str(observation.get("source") or ""),
                                          recent_partial=bool(observation.get("recent_partial")))
    except BaseException:
        # The original complete summary remains authoritative; best-effort enrichment
        # and the extra snapshot do not turn missing transcript detail into a clear gate.
        return


def _prepare_handover_snapshot(req: dict) -> bool:
    """Take the dispatch snapshot after durable queueing, outside the short hook's lock."""
    seat = st.seat_from_dict(req["seat"])
    if seat.kind not in ("pane", "native"):
        return True
    import dispatch_seat_handover as handover
    try:
        bindings = handover.collect_snapshot_bindings(str(req.get("harness") or ""), str(req.get("sid") or ""))
        with st.seat_lock(seat.key):
            held = read_reservation(seat.key)
            if not held or held.get("nonce") != req.get("nonce") or held.get("status") != "reserved":
                return False
            count = handover.publish_snapshot_locked(
                seat, str(req.get("harness") or ""), str(req.get("sid") or ""), bindings,
                expected_prompt_seq=int(req.get("prompt_seq", -1)),
                expected_card_generation=int(req.get("card_generation", -1)))
            held["snapshot_state"] = "ready"
            held["snapshot_count"] = count
            held.pop("last_failure", None)
            _write_reservation(held)
        return True
    except BaseException as exc:
        with contextlib.suppress(BaseException):
            with st.seat_lock(seat.key):
                held = read_reservation(seat.key)
                if held and held.get("nonce") == req.get("nonce") and held.get("status") == "reserved":
                    held["snapshot_state"] = "pending"
                    held["last_failure"] = {"stage": "snapshot", "reason": type(exc).__name__,
                                            "at": st.now_epoch()}
                    _write_reservation(held)
        return False


def note_start_locked(seat: "st.Seat", harness: str, sid: str, now: float) -> None:
    """A session started at a pane that has a booking: remember it as the cleared window's
    successor (caller holds the seat lock).  ``peer-steward.py clear`` reads this as the
    hook-side proof that the new conversation really began."""
    req = read_reservation(seat.key)
    if not req or req.get("status") not in ("reserved", "unverified") or req.get("harness") != harness:
        return
    if sid == req.get("sid") or float(req.get("created", 0)) > now:
        return
    if (req.get("observed") or {}).get("sid") == sid:
        return
    req["observed"] = {"sid": sid, "harness": harness, "at": now}
    if req.get("status") == "unverified":
        # Codex starts the new thread's SessionStart only with its first message, so the successor
        # can show up after the helper stopped waiting: the clear did happen, drop the doubt.
        req.update(status="cleared", new_session=sid, reason="")
        _drop_unverified_notice(seat, req)
    _write_reservation(req)


def _drop_unverified_notice(seat: "st.Seat", req: dict) -> None:
    """Remove the "could not confirm" line this booking left (caller holds the seat lock)."""
    text = _notice_text("unverified", "", str(req.get("harness") or ""))
    path = st._notices_path(seat)
    data = st.read_json(path)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return
    kept = [i for i in items if not (isinstance(i, dict) and i.get("text") == text
                                     and (i.get("author") or {}).get("sid") == req.get("sid"))]
    if len(kept) != len(items):
        st.atomic_write_json(path, {"schema": st.SCHEMA, "items": kept})


def _booking_at(path: Path):
    """``(booking, seat, "")`` for the seat's own booking file at ``path``, else ``(None, None, reason)``."""
    req = st.read_json(path)
    if not isinstance(req, dict) or req.get("schema") != SCHEMA or not isinstance(req.get("seat"), dict):
        return None, None, "request-unreadable"
    seat_fields = req["seat"]
    key = str(seat_fields.get("key") or "")
    try:
        in_place = path.parent.resolve() == clear_dir().resolve()
    except OSError:
        in_place = False
    if not re.fullmatch(r"[0-9a-f]{8,64}", key) or path.name != f"{key}.json" or not in_place:
        return None, None, "request-unreadable"
    seat = st.seat_from_dict(seat_fields)
    return req, seat, ""


def _unchanged_since_card(req: dict, seat: "st.Seat") -> str:
    """"" while no prompt was submitted since the booking and the card is the booked one, else the reason."""
    if st.read_prompt_seq(seat) != int(req.get("prompt_seq", -1)):
        return "new-input"
    card = st.read_latest_card(seat)
    if (int(card["generation"]) if card else 0) != int(req.get("card_generation", -1)):
        return "card-changed"
    return ""


def validate_request(path, nonce: Optional[str] = None, *, now: Optional[float] = None,
                    require_ready: bool = True, _sending_claim: Optional[dict] = None):
    """``(booking, "")`` when the booking at ``path`` still allows a clear, else ``(None, reason)``.

    ``superseded`` (another nonce, no booking, already finished) is the only silent reason;
    the others are results worth one line.  Shared by the helper and ``peer-steward.py clear``
    so both judge the same facts: the file is the seat's own booking, it is unexpired, no
    prompt was submitted since it was made, and the card is still the booked generation.
    """
    now = st.now_epoch() if now is None else now
    req, seat, reason = _booking_at(Path(path))
    if req is None:
        return None, reason
    if (nonce and req.get("nonce") != nonce) or req.get("status") != "reserved":
        return None, "superseded"
    held_claim = req.get("clear_claim") if isinstance(req.get("clear_claim"), dict) else {}
    claimed = (_sending_claim is not None and held_claim.get("state") == "sending"
               and held_claim == _sending_claim and req.get("nonce") == _sending_claim.get("nonce"))
    if held_claim and not claimed:
        return None, "superseded"
    if now > float(req.get("deadline", 0) or 0):
        return None, "expired"
    reason = _unchanged_since_card(req, seat)
    if reason:
        return None, reason
    if req.get("origin") == "idle" \
            and ((req.get("idle_request") or {}).get("state") != "accepted"):
        return None, "idle-card-not-accepted"
    if req.get("origin") == "compact" and require_ready:
        observation = compact_observation(req)
        if observation.get("state") != "ready":
            return None, "compact-unknown" if observation.get("state") == "unknown" else "compact-pending"
    return req, ""


def claim_clear(path, nonce: Optional[str] = None):
    """Atomically consume the one clear send before the steward/native writer acts."""
    req, seat, reason = _booking_at(Path(path))
    if req is None:
        return None, reason
    with st.seat_lock(seat.key):
        req, reason = validate_request(path, nonce)
        if req is None:
            return None, reason
        claim = {"state": "sending", "nonce": req["nonce"], "token": secrets.token_hex(12),
                 "at": st.now_epoch()}
        req["clear_claim"] = claim
        _write_reservation(req)
        return {**req, "clear_claim": claim}, ""


def validate_continue(path, nonce: Optional[str] = None, *, now: Optional[float] = None,
                      _sending_claim: Optional[dict] = None):
    """``(booking, "")`` when the cleared window may still get the continue prompt, else ``(None, reason)``.

    Shared by the helper and ``peer-steward.py continue``: the seat's own booking, cleared, its
    continue still ``pending`` and in time, no prompt submitted since the card (``/clear`` itself
    is none: measured on Claude 2.1.287 and Codex 0.160.0), the card still the booked generation
    and not handed to a peer.  ``superseded`` and ``off`` mean nothing is pending.
    """
    now = st.now_epoch() if now is None else now
    req, seat, reason = _booking_at(Path(path))
    if req is None:
        return None, reason
    if (nonce and req.get("nonce") != nonce) or req.get("status") != "cleared":
        return None, "superseded"
    if req.get("continue_off"):
        return None, "off"
    held = req.get("continued") if isinstance(req.get("continued"), dict) else {}
    claimed = (_sending_claim is not None and held.get("state") == "sending"
               and req.get("nonce") == _sending_claim.get("nonce")
               and req["seat"]["key"] == (_sending_claim.get("seat") or {}).get("key")
               and held == _sending_claim.get("continued"))
    if not claimed and (held.get("state") != "pending" or _sending_claim is not None):
        return None, "superseded"
    if now > float(held.get("deadline", 0) or 0):
        return None, "expired"
    reason = _unchanged_since_card(req, seat)
    if reason:
        return None, reason
    card = st.read_latest_card(seat)
    if not card:
        return None, "no-card"
    if card.get("handed_off"):
        return None, "handed-off"
    return req, ""


def claim_continue(path, nonce: Optional[str] = None):
    """``pending`` -> ``sending``, once, right before the continue prompt is typed: ``(booking, "")``
    or ``(None, reason)``.  Everything ``validate_continue`` checks is checked again under the seat lock."""
    req, seat, reason = _booking_at(Path(path))
    if req is None:
        return None, reason
    with st.seat_lock(seat.key):
        req, reason = validate_continue(path, nonce)
        if req is None:
            return None, reason
        req["continued"] = {**req["continued"], "state": "sending", "at": st.now_epoch()}
        _write_reservation(req)
    return req, ""


def release_unsent_continue(claim):
    """Return only this unsent claim to the same booking's pending state."""
    key = claim["seat"]["key"]
    with st.seat_lock(key):
        req = read_reservation(key)
        if (not req or req.get("nonce") != claim.get("nonce")
                or req.get("continued") != claim.get("continued")
                or (req.get("continued") or {}).get("state") != "sending"):
            return False
        req["continued"] = {**req["continued"], "state": "pending"}
        _write_reservation(req)
        return True


# ---------------------------------------------------------------------------
# The detached helper
# ---------------------------------------------------------------------------

def _herdr_wait_idle(pane: str, timeout_ms: int) -> str:
    """One event-driven ``herdr agent wait``: ``idle``/``done``, or the reason it did not end that way."""
    exe = herdr_command()
    if not exe:
        return "herdr-not-found"
    command = [exe, "agent", "wait", pane, "--until", "idle", "--until", "done", "--timeout", str(max(1, timeout_ms))]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout_ms / 1000.0 + 30)
    except (OSError, subprocess.SubprocessError):
        return "herdr-invocation-failed"
    for stream in (done.stdout, done.stderr):
        try:
            payload = json.loads(stream)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "error")
            return {"timeout": "timeout", "agent_not_found": "agent-not-found"}.get(code, code.replace("_", "-"))
        agent = (payload.get("result") or {}).get("agent")
        if isinstance(agent, dict):
            state = str(agent.get("agent_status") or "unknown")
            return state if state in ("idle", "done") else f"state-{state}"
    return "herdr-protocol-error"


def _ask_steward(command: str, word: str, path: Path, nonce: str, pane: str) -> dict:
    """``peer-steward.py <command>`` -- its ``<word>=...`` line as a dict (``failed`` on a crash)."""
    injected = _injected("HEARTING_TIDY_PEER_STEWARD")
    base = [str(injected)] if injected else [sys.executable, str(HERE / "peer-steward.py")]
    try:
        done = subprocess.run([*base, command, pane, "--request", str(path), "--nonce", nonce],
                              capture_output=True, text=True, timeout=STEWARD_TIMEOUT_SEC, env=_helper_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return {word: "failed", "reason": f"peer-steward-unavailable-{type(exc).__name__}"}
    for line in done.stdout.splitlines():
        if line.startswith(word + "="):
            fields = dict(part.partition("=")[::2] for part in line.split())
            if fields.get(word) in ("true", "accepted", "skipped", "failed", "unverified", "queued"):
                return fields
    return {word: "failed", "reason": f"peer-steward-exit-{done.returncode}"}


def _run_steward(path: Path, nonce: str, pane: str) -> dict:
    """``peer-steward.py clear`` -- its ``cleared=...`` line as a dict."""
    return _ask_steward("clear", "cleared", path, nonce, pane)


def _run_continue(path: Path, nonce: str, pane: str) -> dict:
    """``peer-steward.py continue`` -- its ``continued=...`` line as a dict."""
    return _ask_steward("continue", "continued", path, nonce, pane)


def _run_idle_card_request(path: Path, nonce: str, pane: str) -> dict:
    """Ask the exact booked main session to write/enqueue its own current card once."""
    return _ask_steward("idle-card-request", "accepted", path, nonce, pane)


def followup_text(req: dict) -> str:
    return STATUS_ONLY_TEXT if req.get("followup_kind") == "status-only" else CONTINUE_TEXT


def _notice_text(outcome: str, reason: str, harness: str) -> str:
    hint = CLEAR_COMMAND.get(harness, "/clear")
    why = REASON_TEXT.get(reason, reason or "알 수 없음")
    if outcome == "unverified":
        return f"[정리] 창 비우기를 요청했지만 결과를 확인하지 못했습니다. 창 상태를 확인하고 필요하면 {hint} 하세요."
    if outcome == "failed":
        return f"[정리] 창 비우기에 실패했습니다({why}). 필요하면 {hint} 하세요."
    return f"[정리] 창을 자동으로 비우지 않았습니다({why}). 필요하면 {hint} 하세요."


def _continue_notice_text(outcome: str, reason: str, prompt_text: str = CONTINUE_TEXT) -> str:
    said = f'"{prompt_text}"'
    if outcome == "unverified":
        return f"[정리] 새 세션에 {said}를 넣었지만 전달을 확인하지 못했습니다. 진행되지 않았다면 {said}라고 입력하세요."
    why = REASON_TEXT.get(reason, reason or "알 수 없음")
    if reason.startswith("not-idle-"):
        why = "새 세션이 입력 대기 상태가 되지 않았습니다"
    verb = "넣지 못했습니다" if outcome == "failed" else "넣지 않았습니다"
    return f"[정리] 창은 비웠지만 {said}를 {verb}({why}). 이어서 하려면 {said}라고 입력하세요."


def _booked_seat(req: dict, seat_key: str) -> "st.Seat":
    fields = req.get("seat") or {}
    return st.seat_from_dict({**fields, "key": seat_key})


def _finish(seat_key: str, nonce: str, outcome: str, reason: str = "", *, observed: str = "") -> bool:
    """Record how the booking ended -- only while it is still this helper's -- and leave the
    one result line for anything but a clear.  The notice is written after the seat lock is
    released (``write_notice`` takes it itself)."""
    with st.seat_lock(seat_key):
        req = read_reservation(seat_key)
        if not req or req.get("nonce") != nonce or req.get("status") not in ("reserved", "unverified"):
            return False
        now = st.now_epoch()
        req.update(status=outcome, reason=reason, finished=now)
        claim = req.get("clear_claim")
        if isinstance(claim, dict) and claim.get("state") == "sending":
            req["clear_claim"] = {**claim, "state": "sent" if outcome in ("cleared", "unverified") else "finished",
                                  "outcome": outcome, "finished": now}
        if observed:
            req["new_session"] = observed
        if outcome == "cleared" and not req.get("continue_off"):
            # Written before anything is typed: the new Codex thread's first hook finds its window by it.
            kind = req.get("followup_kind") or "continue"
            req["continued"] = {"state": "pending", "kind": kind,
                                "deadline": now + _tunable("CONTINUE_WINDOW", CONTINUE_WINDOW_SEC)}
        _write_reservation(req)
    if outcome != "cleared":
        with contextlib.suppress(BaseException):
            st.write_notice(_booked_seat(req, seat_key), _notice_text(outcome, reason, str(req.get("harness") or "")),
                            author_harness=str(req.get("harness") or ""), author_sid=str(req.get("sid") or ""))
    return True


def _finish_continue(seat_key: str, nonce: str, outcome: str, reason: str = "") -> bool:
    """Record how the continue ended -- only while it is still this booking's and open -- and leave
    one result line for a miss the person did not cause."""
    with st.seat_lock(seat_key):
        req = read_reservation(seat_key)
        held = (req or {}).get("continued") if isinstance((req or {}).get("continued"), dict) else {}
        if not req or req.get("nonce") != nonce or req.get("status") != "cleared" \
                or held.get("state") not in ("pending", "sending"):
            return False
        if outcome == "queued":
            if held.get("state") != "pending":
                return False    # A newer callback's sending claim is never rewound.
            req["continued"] = {**held, "state": "pending", "reason": reason}
            _write_reservation(req)
            return True
        req["continued"] = {**held, "state": "sent" if outcome == "true" else outcome, "reason": reason,
                            "finished": st.now_epoch()}
        _write_reservation(req)
    if outcome != "true" and not (outcome == "skipped" and reason in CONTINUE_QUIET):
        with contextlib.suppress(BaseException):
            prompt_text = STATUS_ONLY_TEXT if held.get("kind") == "status-only" else CONTINUE_TEXT
            st.write_notice(_booked_seat(req, seat_key), _continue_notice_text(outcome, reason, prompt_text),
                            author_harness=str(req.get("harness") or ""), author_sid=str(req.get("sid") or ""))
    return True


def _continue(seat_key: str, nonce: str, continuer: Callable[[Path, str, str], dict]) -> str:
    """After a confirmed clear: ask ``peer-steward.py continue`` once and record its end.
    ``continued`` when the prompt went in, else ``cleared`` (the clear itself stands)."""
    path = reservation_path(seat_key)
    req, reason = validate_continue(path, nonce)
    if req is None and reason in ("superseded", "off"):
        return "cleared"                # nothing pending: --no-continue, a newer tidy, --no-clear
    if req is None:
        verdict = {"continued": "skipped", "reason": reason}
    else:
        verdict = continuer(path, nonce, str((req.get("seat") or {}).get("pane") or ""))
    outcome = verdict.get("continued", "failed")
    _finish_continue(seat_key, nonce, outcome, verdict.get("reason", ""))
    return "continued" if outcome == "true" else "cleared"


def _wait_idle_card_acceptance(seat_key: str, nonce: str, sleep: Callable[[float], None]) -> str:
    """Wait on the existing booking until its one main-session card enqueue is durable."""
    path = reservation_path(seat_key)
    while True:
        held = read_reservation(seat_key)
        if not held or held.get("nonce") != nonce or held.get("status") != "reserved":
            return "superseded"
        request = held.get("idle_request") if isinstance(held.get("idle_request"), dict) else {}
        now = st.now_epoch()
        if now > float(held.get("deadline", 0) or 0):
            return "skipped" if _finish(seat_key, nonce, "skipped", "idle-card-not-accepted") else "superseded"
        if request.get("state") == "accepted":
            if held.get("snapshot_state") == "pending" and not _prepare_handover_snapshot(held):
                sleep(min(1.0, max(0.0, float(held.get("deadline", 0) or 0) - now)))
                continue
            return "accepted"
        if request.get("state") in ("sending", "arrived"):
            sleep(min(1.0, max(0.0, float(held.get("deadline", 0) or 0) - now)))
            continue
        # An accepted transport prompt must be followed by the exact UserPromptSubmit
        # receipt. Anything else is uncertain; never send that request again.
        if request.get("state") in ("unverified", "failed"):
            return "unverified"
        sleep(min(1.0, max(0.0, float(held.get("deadline", 0) or 0) - now)))


def _idle_due_and_request(seat_key: str, nonce: str, sleep: Callable[[float], None]) -> str:
    path = reservation_path(seat_key)
    while True:
        req = read_reservation(seat_key)
        if not req or req.get("nonce") != nonce or req.get("status") != "reserved":
            return "superseded"
        request = req.get("idle_request") if isinstance(req.get("idle_request"), dict) else {}
        if request.get("state") in ("sending", "arrived", "sent", "accepted"):
            accepted = _wait_idle_card_acceptance(seat_key, nonce, sleep)
            if accepted == "unverified":
                return "unverified" if _finish(seat_key, nonce, "unverified", "arrival-not-observed") else "superseded"
            return accepted
        if request.get("state") == "unverified":
            return "unverified" if _finish(seat_key, nonce, "unverified", "arrival-not-observed") else "superseded"
        idle = req.get("idle") if isinstance(req.get("idle"), dict) else {}
        due = float(idle.get("due_at", 0) or 0)
        now = st.now_epoch()
        if now > float(req.get("deadline", 0) or 0):
            return "skipped" if _finish(seat_key, nonce, "skipped", "expired") else "superseded"
        if now < due:
            # The same absolute due_at survives interruptions and duplicate events.
            sleep(min(30.0, due - now))
            continue
        checked, reason = validate_idle_request(path, nonce)
        if checked is None:
            if reason == "superseded":
                return "superseded"
            return "skipped" if _finish(seat_key, nonce, "skipped", reason) else "superseded"
        seat = checked.get("seat") or {}
        target = str(seat.get("pane") or (f"native:{(seat.get('native') or {}).get('job')}"
                                           if seat.get("kind") == "native" else ""))
        verdict = _run_idle_card_request(path, nonce, target)
        if verdict.get("accepted") != "true":
            held = read_reservation(seat_key) or {}
            request = held.get("idle_request") if isinstance(held.get("idle_request"), dict) else {}
            if request.get("state") in ("sending", "arrived", "accepted", "unverified"):
                if request.get("state") == "unverified":
                    return "unverified" if _finish(seat_key, nonce, "unverified",
                                                    request.get("reason") or verdict.get("reason")) else "superseded"
                return "pending"
            if verdict.get("accepted") == "queued" and st.now_epoch() < float(req.get("deadline", 0) or 0):
                sleep(min(5.0, max(0.0, float(req.get("deadline", 0) or 0) - st.now_epoch())))
                continue
            reason = verdict.get("reason") or "idle-card-not-accepted"
            return "skipped" if _finish(seat_key, nonce, "skipped", reason) else "superseded"
        accepted = _wait_idle_card_acceptance(seat_key, nonce, sleep)
        if accepted == "accepted":
            return accepted
        if accepted == "unverified":
            return "unverified" if _finish(seat_key, nonce, "unverified", "arrival-not-observed") else "superseded"
        # If the transport could not prove its exact prompt arrival, its claim is terminal.
        # No retry can distinguish an unseen send from one that arrived late.
        return accepted if accepted in ("superseded", "pending", "unverified") else (
            "skipped" if _finish(seat_key, nonce, "skipped", verdict.get("reason") or "idle-card-not-accepted")
            else "superseded")


def run_helper(seat_key: str, nonce: str, *, wait: Optional[Callable[[str, int], str]] = None,
               steward: Optional[Callable[[Path, str, str], dict]] = None,
               continuer: Optional[Callable[[Path, str, str], dict]] = None,
               sleep: Callable[[float], None] = time.sleep) -> str:
    """Wait for safe readiness, consume one clear send, and continue after a confirmed clear.

    Ordinary tidy keeps its single idle wait. Compact bookings poll in bounded intervals and
    require transcript completion on final validation. Returns ``continued`` only when both
    the clear and continue prompt were confirmed.
    """
    wait = wait or _herdr_wait_idle
    steward = steward or _run_steward
    continuer = continuer or _run_continue
    path = reservation_path(seat_key)
    with st.seat_lock(seat_key):
        req = read_reservation(seat_key)
        if not req or req.get("nonce") != nonce or req.get("status") != "reserved":
            return "superseded"
        helper = {"pid": os.getpid(), "pid_start": _proc_start(os.getpid())}
        if req.get("helper") != helper:
            req["helper"] = helper
            _write_reservation(req)
    if req.get("snapshot_state") == "pending":
        while True:
            if _prepare_handover_snapshot(req):
                break
            checked, reason = validate_request(path, nonce, require_ready=False)
            if checked is None:
                if reason == "superseded":
                    return "superseded"
                return "skipped" if _finish(seat_key, nonce, "skipped", reason) else "superseded"
            remaining = float(req.get("deadline", 0) or 0) - st.now_epoch()
            if remaining <= 0:
                return "skipped" if _finish(seat_key, nonce, "skipped", "snapshot-unavailable") else "superseded"
            sleep(min(1.0, remaining))
            req = read_reservation(seat_key) or req
    pane = str((req.get("seat") or {}).get("pane") or "")
    if req.get("origin") == "idle":
        accepted = _idle_due_and_request(seat_key, nonce, sleep)
        if accepted != "accepted":
            return accepted
        req = read_reservation(seat_key) or req
    if req.get("origin") == "compact":
        while True:
            req, reason = validate_request(path, nonce, require_ready=False)
            if req is None:
                if reason == "superseded":
                    return "superseded"
                held = read_reservation(seat_key) or {}
                if reason == "expired" and held.get("origin") == "compact":
                    reason = "compact-timeout"
                return "skipped" if _finish(seat_key, nonce, "skipped", reason) else "superseded"
            remaining = float(req.get("deadline", 0)) - st.now_epoch()
            if remaining <= 0:
                return "skipped" if _finish(seat_key, nonce, "skipped", "compact-timeout") else "superseded"
            timeout_ms = max(1, int(min(COMPACT_POLL_SEC, remaining) * 1000))
            if (req.get("seat") or {}).get("kind") == "native":
                from session_tidy_native import wait_idle
                state = wait_idle(req["seat"].get("native"), req["sid"], timeout_ms)
            else:
                state = wait(pane, timeout_ms)
            if state in ("idle", "done"):
                observation = compact_observation(req)
                if observation.get("state") == "ready":
                    _refresh_compact_card(req, observation)
                    refreshed = read_reservation(seat_key) or {}
                    if refreshed.get("snapshot_state") != "ready":
                        remaining = float(refreshed.get("deadline", 0) or 0) - st.now_epoch()
                        if remaining <= 0:
                            return "skipped" if _finish(
                                seat_key, nonce, "skipped", "snapshot-unavailable") else "superseded"
                        sleep(min(COMPACT_POLL_SEC, remaining))
                        continue
                    req, reason = validate_request(path, nonce)
                    if req is not None:
                        break
                    if reason == "superseded":
                        return "superseded"
            else:
                # A transient herdr read failure is retried inside the same absolute deadline.
                state = "pending"
            remaining = float((read_reservation(seat_key) or {}).get("deadline", 0)) - st.now_epoch()
            if remaining <= 0:
                return "skipped" if _finish(seat_key, nonce, "skipped", "compact-timeout") else "superseded"
            sleep(min(COMPACT_POLL_SEC, remaining))
    else:
        remaining = float(req.get("deadline", 0)) - st.now_epoch()
        if remaining <= 0:
            return "skipped" if _finish(seat_key, nonce, "skipped", "expired") else "superseded"
        if (req.get("seat") or {}).get("kind") == "native":
            from session_tidy_native import wait_idle
            state = wait_idle(req["seat"].get("native"), req["sid"], int(remaining * 1000))
        else:
            state = wait(pane, int(remaining * 1000))
        if state not in ("idle", "done"):
            reason = "timeout" if state.startswith(("timeout", "state-")) else state
            return "skipped" if _finish(seat_key, nonce, "skipped", reason) else "superseded"
    req, reason = validate_request(path, nonce)
    if req is None:
        if reason == "superseded":
            return "superseded"
        return "skipped" if _finish(seat_key, nonce, "skipped", reason) else "superseded"
    verdict = steward(path, nonce, pane)
    outcome, reason = verdict.get("cleared", "failed"), verdict.get("reason", "")
    if outcome == "true":
        _finish(seat_key, nonce, "cleared", observed=verdict.get("new_session", ""))
        return _continue(seat_key, nonce, continuer)
    if outcome == "unverified":
        # Typed once and never again; give the new conversation's start hook a short bounded
        # window to show up before calling it unconfirmed.
        deadline = time.monotonic() + _tunable("CLEAR_OBSERVE", OBSERVE_WAIT_SEC)
        while time.monotonic() < deadline:
            held = read_reservation(seat_key)
            seen = (held or {}).get("observed") or {}
            if seen.get("sid"):
                _finish(seat_key, nonce, "cleared", observed=str(seen["sid"]))
                return _continue(seat_key, nonce, continuer)
            sleep(1.0)
        return "unverified" if _finish(seat_key, nonce, "unverified", reason) else "superseded"
    final = "skipped" if outcome == "skipped" else "failed"
    return final if _finish(seat_key, nonce, final, reason) else "superseded"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="session_tidy_clear.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="wait for idle and clear the booked window (started detached by enqueue)")
    run.add_argument("--seat", required=True)
    run.add_argument("--nonce", required=True)
    args = parser.parse_args(argv)
    try:
        run_helper(args.seat, args.nonce)
    except BaseException:  # noqa: BLE001 - a detached helper has nobody to tell
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
