#!/usr/bin/env python3
"""session-tidy auto-clear: the booking, the detached helper, the request check.

``session_tidy.py enqueue`` books one clear of the calling window
(:func:`schedule_for_enqueue`) and starts this file as a detached helper.  The helper
waits (one bounded ``herdr agent wait``, at most 600 s) until the window is idle and
then asks ``peer-steward.py clear`` -- the only code that judges the input box and types
into the pane.  Nothing waits for the memory tidy: the two run on their own.

The booking is ``clear/<seat>.json`` (0600, seat lock, atomic write): the exact pane,
harness, session id, card generation, prompt count, nonce and 600 s deadline.  A newer
tidy replaces it (new nonce); a helper that finds another nonce, or no booking, stops
without writing anything.  ``enqueue --no-clear`` removes it.

A window is cleared only when nothing new could be lost: no prompt was submitted after
the card (the seat's ``prompt_seq``), the card is still the one booked, and
``peer-steward.py clear`` reads the pane as idle with no form open and an empty input box.
Anything it cannot decide is "not cleared" plus one result line (``write_notice``); a
success is silent.  The command is typed at most once and is never repeated.

Test hooks (only inside ``HEARTING_TIDY_TEST_ROOT``, see ``session_tidy_runner``):
``HEARTING_TIDY_HERDR`` / ``HEARTING_TIDY_PEER_STEWARD`` stand in for the checked
commands, ``HEARTING_TIDY_CLEAR_DEADLINE`` / ``HEARTING_TIDY_CLEAR_OBSERVE`` for the timings.
"""

from __future__ import annotations

import argparse
import contextlib
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
OBSERVE_WAIT_SEC = 20.0
STEWARD_TIMEOUT_SEC = 180.0
# The built-in command that starts a fresh conversation in each harness.
CLEAR_COMMAND = {"claude": "/clear", "codex": "/clear", "opencode": "/new"}

REASON_TEXT = {
    "new-input": "새 입력이 있었습니다",
    "timeout": "10분 안에 입력 대기 상태가 되지 않았습니다",
    "expired": "예약 시간이 지났습니다",
    "card-changed": "카드가 다시 쓰였습니다",
    "form-open": "선택·확인 창이 열려 있습니다",
    "draft": "입력창에 쓰던 글이 있습니다",
    "draft-unknown": "입력창이 비어 있는지 알 수 없습니다",
    "screen-unknown": "화면을 읽지 못했습니다",
    "target-changed": "그 자리의 세션이 바뀌었습니다",
    "herdr-not-found": "herdr를 찾지 못했습니다",
    "agent-not-found": "창을 찾지 못했습니다",
}


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
    """Drop a pending booking; its helper finds nothing and stops silently."""
    with st.seat_lock(seat.key):
        _remove_reservation(seat.key)


def _proc_start(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return raw[raw.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return ""


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
                         now: Optional[float] = None) -> str:
    """Book the clear and start its helper; the ``clear=...`` words ``enqueue`` prints.

    ``clear=scheduled``      the window is cleared by itself once it is idle;
    ``clear=off``            ``--no-clear``: a pending booking is cancelled too;
    ``clear=skipped reason`` a prompt was submitted after the card: nothing is booked;
    ``clear=manual hint=C``  outside herdr (no pane, no herdr): the user types ``C``.
    """
    now = st.now_epoch() if now is None else now
    hint = CLEAR_COMMAND.get(harness, "/clear")
    if opt_out:
        cancel(seat)
        return "clear=off"
    if seat.kind != "pane" or not seat.pane or harness not in CLEAR_COMMAND or herdr_command() is None:
        cancel(seat)
        return f"clear=manual hint={hint}"
    with st.seat_lock(seat.key):
        card = st.read_latest_card(seat)
        seq = st.read_prompt_seq(seat)
        author = (card or {}).get("author") or {}
        stale = bool(card) and (author.get("harness"), author.get("sid")) == (harness, sid) \
            and card.get("prompt_seq") is not None and int(card["prompt_seq"]) != seq
        if stale:
            _remove_reservation(seat.key)
            return "clear=skipped reason=new-input"
        nonce = secrets.token_hex(8)
        _write_reservation({
            "schema": SCHEMA, "nonce": nonce, "status": "reserved", "created": now,
            "deadline": now + _tunable("CLEAR_DEADLINE", CLEAR_DEADLINE_SEC),
            "seat": {"kind": seat.kind, "key": seat.key, "pane": seat.pane,
                     "harness": seat.harness, "project_key": seat.project_key},
            "harness": harness, "sid": sid, "cwd": cwd,
            "card_generation": int(card["generation"]) if card else 0, "prompt_seq": seq})
    try:
        pid = _start_helper(seat.key, nonce)
    except (OSError, subprocess.SubprocessError):
        with st.seat_lock(seat.key):
            held = read_reservation(seat.key)
            if held and held.get("nonce") == nonce:
                _remove_reservation(seat.key)
        return f"clear=manual reason=helper-not-started hint={hint}"
    with st.seat_lock(seat.key):
        held = read_reservation(seat.key)
        if held and held.get("nonce") == nonce:
            held["helper"] = {"pid": pid, "pid_start": _proc_start(pid)}
            _write_reservation(held)
    return "clear=scheduled"


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
    _write_reservation(req)


def validate_request(path, nonce: Optional[str] = None, *, now: Optional[float] = None):
    """``(booking, "")`` when the booking at ``path`` still allows a clear, else ``(None, reason)``.

    ``superseded`` (another nonce, no booking, already finished) is the only silent reason;
    the others are results worth one line.  Shared by the helper and ``peer-steward.py clear``
    so both judge the same facts: the file is the seat's own booking, it is unexpired, no
    prompt was submitted since it was made, and the card is still the booked generation.
    """
    now = st.now_epoch() if now is None else now
    path = Path(path)
    req = st.read_json(path)
    if not isinstance(req, dict) or req.get("schema") != SCHEMA or not isinstance(req.get("seat"), dict):
        return None, "request-unreadable"
    seat_fields = req["seat"]
    key = str(seat_fields.get("key") or "")
    try:
        in_place = path.parent.resolve() == clear_dir().resolve()
    except OSError:
        in_place = False
    if not re.fullmatch(r"[0-9a-f]{8,64}", key) or path.name != f"{key}.json" or not in_place:
        return None, "request-unreadable"
    if (nonce and req.get("nonce") != nonce) or req.get("status") != "reserved":
        return None, "superseded"
    if now > float(req.get("deadline", 0) or 0):
        return None, "expired"
    seat = st.Seat(str(seat_fields.get("kind") or ""), key, str(seat_fields.get("pane") or ""),
                   str(seat_fields.get("harness") or ""), str(seat_fields.get("project_key") or ""))
    if st.read_prompt_seq(seat) != int(req.get("prompt_seq", -1)):
        return None, "new-input"
    card = st.read_latest_card(seat)
    if (int(card["generation"]) if card else 0) != int(req.get("card_generation", -1)):
        return None, "card-changed"
    return req, ""


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


def _run_steward(path: Path, nonce: str, pane: str) -> dict:
    """``peer-steward.py clear`` -- its ``cleared=...`` line as a dict (``cleared`` is ``failed`` on a crash)."""
    injected = _injected("HEARTING_TIDY_PEER_STEWARD")
    base = [str(injected)] if injected else [sys.executable, str(HERE / "peer-steward.py")]
    try:
        done = subprocess.run([*base, "clear", pane, "--request", str(path), "--nonce", nonce],
                              capture_output=True, text=True, timeout=STEWARD_TIMEOUT_SEC, env=_helper_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return {"cleared": "failed", "reason": f"peer-steward-unavailable-{type(exc).__name__}"}
    for line in done.stdout.splitlines():
        if line.startswith("cleared="):
            fields = dict(part.partition("=")[::2] for part in line.split())
            if fields.get("cleared") in ("true", "skipped", "failed", "unverified"):
                return fields
    return {"cleared": "failed", "reason": f"peer-steward-exit-{done.returncode}"}


def _notice_text(outcome: str, reason: str, harness: str) -> str:
    hint = CLEAR_COMMAND.get(harness, "/clear")
    why = REASON_TEXT.get(reason, reason or "알 수 없음")
    if outcome == "unverified":
        return f"[정리] 창 비우기를 요청했지만 결과를 확인하지 못했습니다. 창 상태를 확인하고 필요하면 {hint} 하세요."
    if outcome == "failed":
        return f"[정리] 창 비우기에 실패했습니다({why}). 필요하면 {hint} 하세요."
    return f"[정리] 창을 자동으로 비우지 않았습니다({why}). 필요하면 {hint} 하세요."


def _finish(seat_key: str, nonce: str, outcome: str, reason: str = "", *, observed: str = "") -> bool:
    """Record how the booking ended -- only while it is still this helper's -- and leave the
    one result line for anything but a clear.  The notice is written after the seat lock is
    released (``write_notice`` takes it itself)."""
    with st.seat_lock(seat_key):
        req = read_reservation(seat_key)
        if not req or req.get("nonce") != nonce or req.get("status") not in ("reserved", "unverified"):
            return False
        req.update(status=outcome, reason=reason, finished=st.now_epoch())
        if observed:
            req["new_session"] = observed
        _write_reservation(req)
    if outcome != "cleared":
        with contextlib.suppress(BaseException):
            st.write_notice(st.Seat(str(req["seat"].get("kind") or ""), seat_key, str(req["seat"].get("pane") or ""),
                                    str(req["seat"].get("harness") or ""), str(req["seat"].get("project_key") or "")),
                            _notice_text(outcome, reason, str(req.get("harness") or "")),
                            author_harness=str(req.get("harness") or ""), author_sid=str(req.get("sid") or ""))
    return True


def run_helper(seat_key: str, nonce: str, *, wait: Optional[Callable[[str, int], str]] = None,
               steward: Optional[Callable[[Path, str, str], dict]] = None,
               sleep: Callable[[float], None] = time.sleep) -> str:
    """Wait for idle, ask ``peer-steward.py clear`` once, record the end.  Returns the outcome word."""
    wait = wait or _herdr_wait_idle
    steward = steward or _run_steward
    path = reservation_path(seat_key)
    with st.seat_lock(seat_key):
        req = read_reservation(seat_key)
        if not req or req.get("nonce") != nonce or req.get("status") != "reserved":
            return "superseded"
        helper = {"pid": os.getpid(), "pid_start": _proc_start(os.getpid())}
        if req.get("helper") != helper:
            req["helper"] = helper
            _write_reservation(req)
    pane = str((req.get("seat") or {}).get("pane") or "")
    remaining = float(req.get("deadline", 0)) - st.now_epoch()
    if remaining <= 0:
        return "skipped" if _finish(seat_key, nonce, "skipped", "expired") else "superseded"
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
        return "cleared"
    if outcome == "unverified":
        # Typed once and never again; give the new conversation's start hook a short bounded
        # window to show up before calling it unconfirmed.
        deadline = time.monotonic() + _tunable("CLEAR_OBSERVE", OBSERVE_WAIT_SEC)
        while time.monotonic() < deadline:
            held = read_reservation(seat_key)
            seen = (held or {}).get("observed") or {}
            if seen.get("sid"):
                _finish(seat_key, nonce, "cleared", observed=str(seen["sid"]))
                return "cleared"
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
